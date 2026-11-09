"""Deriv API client and market-data feed (WebSocket v3).

Covers what a scalping desk actually needs from Deriv:
  * historical candles (`ticks_history`, style=candles) for backtests and warmup;
  * live tick subscriptions for V10/V25/V50/V75/V100, 1HZ, Boom/Crash, Jump indices;
  * account balance / authorisation for execution and reconciliation.

Notes that matter in production:
  * Granularities are restricted to 60/120/180/300/600/900/1800/3600/7200/14400/
    28800/86400 seconds. 300s (M5) is the scalping default.
  * Synthetic indices tick 24/7 but Boom/Crash have asymmetric jumps; the engine
    treats them as CHAOS-prone and requires a much higher conviction.
  * `app_id` identifies the application, not the account. Tokens are per account.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..models import Bar, Quote
from .ws import WebSocketClient

DERIV_WS = "wss://ws.derivws.com/websockets/v3?app_id={app_id}&l=EN&brand=deriv"
VALID_GRANULARITIES = {60, 120, 180, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400}

#: Deriv symbol -> display name, used by the dashboard
SYMBOL_NAMES = {
    "R_10": "Volatility 10", "R_25": "Volatility 25", "R_50": "Volatility 50",
    "R_75": "Volatility 75", "R_100": "Volatility 100",
    "1HZ10V": "Volatility 10 (1s)", "1HZ25V": "Volatility 25 (1s)",
    "1HZ50V": "Volatility 50 (1s)", "1HZ75V": "Volatility 75 (1s)",
    "1HZ100V": "Volatility 100 (1s)",
    "BOOM300N": "Boom 300", "BOOM500": "Boom 500", "BOOM1000": "Boom 1000",
    "CRASH300N": "Crash 300", "CRASH500": "Crash 500", "CRASH1000": "Crash 1000",
    "JD10": "Jump 10", "JD25": "Jump 25", "JD50": "Jump 50",
    "JD75": "Jump 75", "JD100": "Jump 100",
}


class DerivError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass
class DerivConfig:
    app_id: str = "1089"           # public test app id; replace with your own
    token: str = ""
    currency: str = "USD"
    endpoint: str = ""
    stale_after_s: float = 90.0    # no message for this long -> socket is dead
    max_reconnects: int = 50

    @property
    def url(self) -> str:
        if self.endpoint:
            return self.endpoint
        return DERIV_WS.format(app_id=self.app_id)


class DerivAPI:
    """Async JSON-RPC over WebSocket with request correlation and subscriptions."""

    def __init__(self, cfg: Optional[DerivConfig] = None, ws_factory=None):
        self.cfg = cfg or DerivConfig()
        self._ws_factory = ws_factory or WebSocketClient
        self._ws: Optional[WebSocketClient] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._subs: dict[str, list[Callable[[dict], None]]] = {}
        self._tick_subs: dict[str, list[Callable[[dict], None]]] = {}
        self._sub_ids: dict[str, str] = {}
        self.authorized = False
        self.logged_in = ""
        self.last_balance = 0.0
        self.closed = True                 # becomes False once connected
        self.last_message_ts = 0.0
        self.reconnects = 0
        self.currency = self.cfg.currency

    # ------------------------------------------------------------------ #
    @property
    def alive(self) -> bool:
        """A socket that streams nothing for `stale_after_s` is dead, not quiet."""
        if self.closed or self._ws is None:
            return False
        return (time.time() - self.last_message_ts) < self.cfg.stale_after_s

    async def connect(self) -> None:
        self._ws = self._ws_factory(self.cfg.url)
        await self._ws.connect()
        self.closed = False
        self.last_message_ts = time.time()
        self._reader_task = asyncio.create_task(self._read_loop())
        if self.cfg.token:
            await self.authorize(self.cfg.token)

    async def close(self) -> None:
        self.closed = True
        if self._reader_task:
            self._reader_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws.messages():
                self.last_message_ts = time.time()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                await self._dispatch(msg)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(DerivError("DISCONNECTED", "socket closed"))
            self._pending.clear()

    async def _dispatch(self, msg: dict) -> None:
        if "error" in msg:
            err = msg["error"]
            rid = msg.get("req_id")
            if rid in self._pending and not self._pending[rid].done():
                self._pending[rid].set_exception(
                    DerivError(err.get("code", "ERROR"), err.get("message", "unknown")))
            return
        rid = msg.get("req_id")
        if rid in self._pending and not self._pending[rid].done():
            self._pending[rid].set_result(msg)
        # Tick streams are routed by symbol, not by subscription id, so the very
        # first tick after subscribing cannot be lost to a race between the
        # response and callback registration.
        tick = msg.get("tick")
        if isinstance(tick, dict):
            for cb in self._tick_subs.get(tick.get("symbol", ""), []):
                try:
                    cb(msg)
                except Exception:
                    pass
        sub = msg.get("subscription", {}).get("id")
        if sub and sub in self._subs:
            for cb in self._subs[sub]:
                try:
                    cb(msg)
                except Exception:
                    pass

    async def request(self, payload: dict, timeout: float = 20.0) -> dict:
        if self._ws is None:
            raise DerivError("NOT_CONNECTED", "call connect() first")
        self._req_id += 1
        rid = self._req_id
        payload = dict(payload)
        payload["req_id"] = rid
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send(json.dumps(payload))
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(rid, None)

    # ------------------------------------------------------------------ #
    async def authorize(self, token: str) -> dict:
        res = await self.request({"authorize": token})
        self.authorized = True
        self.logged_in = res.get("authorize", {}).get("loginid", "")
        self.currency = res.get("authorize", {}).get("currency", self.currency)
        self.cfg.token = token
        return res

    async def get_balance(self) -> float:
        res = await self.request({"balance": 1})
        self.last_balance = float(res.get("balance", {}).get("balance", 0.0))
        return self.last_balance

    async def candles(self, symbol: str, granularity: int = 300, count: int = 500,
                      end: str = "latest") -> list[Bar]:
        if granularity not in VALID_GRANULARITIES:
            raise DerivError("BAD_GRANULARITY", f"{granularity} not allowed")
        res = await self.request({
            "ticks_history": symbol, "end": end, "count": min(count, 5000),
            "granularity": granularity, "style": "candles",
        })
        out = []
        for c in res.get("candles", []):
            out.append(Bar(symbol, float(c["epoch"]), float(c["open"]), float(c["high"]),
                           float(c["low"]), float(c["close"])))
        return out

    async def subscribe_ticks(self, symbol: str, cb: Callable[[dict], None]) -> str:
        self._tick_subs.setdefault(symbol, []).append(cb)      # register first
        try:
            res = await self.request({"ticks": symbol, "subscribe": 1})
        except DerivError:
            self._tick_subs[symbol].remove(cb)
            raise
        sid = res.get("subscription", {}).get("id", "")
        if sid:
            self._sub_ids[symbol] = sid
        return sid

    async def unsubscribe(self, symbol: str) -> None:
        self._tick_subs.pop(symbol, None)
        sid = self._sub_ids.pop(symbol, None)
        if sid:
            self._subs.pop(sid, None)
            try:
                await self.request({"forget": sid})
            except DerivError:
                pass

    # -------------------------- trading ------------------------------- #
    async def proposal(self, symbol: str, contract_type: str, stake: float,
                       duration: int = 5, duration_unit: str = "m",
                       multiplier: Optional[float] = None,
                       stop_loss: Optional[float] = None,
                       take_profit: Optional[float] = None,
                       currency: Optional[str] = None) -> dict:
        payload: dict[str, Any] = {
            "proposal": 1, "symbol": symbol, "contract_type": contract_type,
            "amount": round(stake, 2), "basis": "stake",
            "currency": currency or self.currency,
        }
        if multiplier is not None:
            payload["multiplier"] = multiplier
            if stop_loss is not None or take_profit is not None:
                limit = {}
                if stop_loss is not None:
                    limit["stop_loss"] = round(stop_loss, 4)
                if take_profit is not None:
                    limit["take_profit"] = round(take_profit, 4)
                payload["limit_order"] = limit
        else:
            payload.update({"duration": duration, "duration_unit": duration_unit})
        res = await self.request(payload)
        return res.get("proposal", {})

    async def buy(self, proposal_id: str, price: float) -> dict:
        res = await self.request({"buy": proposal_id, "price": round(price, 2)})
        return res.get("buy", {})

    async def sell(self, contract_id: str, price: float = 0.0) -> dict:
        res = await self.request({"sell": contract_id, "price": round(price, 2)})
        return res.get("sell", {})

    async def portfolio(self) -> list[dict]:
        res = await self.request({"portfolio": 1})
        return res.get("portfolio", {}).get("contracts", [])

    async def open_contract(self, contract_id: str) -> dict:
        res = await self.request({"proposal_open_contract": 1, "contract_id": contract_id})
        return res.get("proposal_open_contract", {})


class DerivFeed:
    """Live tick -> Quote, with on-demand bar aggregation per timeframe."""

    def __init__(self, api: DerivAPI, symbols: list[str]):
        self.api = api
        self.symbols = symbols
        self.last_quote: dict[str, Quote] = {}
        self.on_quote: Optional[Callable[[Quote], None]] = None

    async def start(self) -> None:
        for s in self.symbols:
            await self.api.subscribe_ticks(s, self._handle_tick)

    def _handle_tick(self, msg: dict) -> None:
        t = msg.get("tick") or {}
        symbol = t.get("symbol")
        quote = t.get("quote")
        epoch = t.get("epoch")
        if not symbol or quote is None:
            return
        # Deriv publishes a single mid quote. The synthetic spread is configured
        # per instrument in the registry and applied by the broker adapter; for
        # signal generation a zero-spread quote is correct (no double counting).
        q = Quote(symbol=symbol, ts=float(epoch or time.time()), bid=float(quote), ask=float(quote))
        self.last_quote[symbol] = q
        if self.on_quote:
            self.on_quote(q)


# --------------------------------------------------------------------------- #
# sync helpers for CLI / backtest data caching
# --------------------------------------------------------------------------- #
def fetch_candles_sync(symbol: str, granularity: int = 300, count: int = 3000,
                       app_id: str = "1089", token: str = "") -> list[Bar]:
    async def _run() -> list[Bar]:
        api = DerivAPI(DerivConfig(app_id=app_id, token=token))
        await api.connect()
        try:
            bars: list[Bar] = []
            remaining = count
            end = "latest"
            while remaining > 0:
                batch = await api.candles(symbol, granularity, min(remaining, 5000), end=end)
                if not batch:
                    break
                bars = batch + bars
                remaining -= len(batch)
                end = str(int(batch[0].ts) - 1)
                if len(batch) < 2:
                    break
            return bars
        finally:
            await api.close()
    return asyncio.run(_run())


def bars_to_csv(bars: list[Bar], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("ts,open,high,low,close,volume\n")
        for b in bars:
            fh.write(f"{b.ts:.0f},{b.open},{b.high},{b.low},{b.close},{b.volume}\n")


def bars_from_csv(symbol: str, path: str) -> list[Bar]:
    out: list[Bar] = []
    with open(path, "r", encoding="utf-8") as fh:
        header = fh.readline()
        if "ts" not in header:
            fh.seek(0)
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) < 5:
                continue
            try:
                out.append(Bar(symbol, *[float(x) for x in parts[:6]]))
            except ValueError:
                continue
    return out
