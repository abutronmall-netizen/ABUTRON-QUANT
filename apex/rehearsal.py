"""Rehearse the live Deriv adapter against a loopback venue.

The honest gap this closes: the Deriv order path had only ever run against a
fake client object. `dry_run=False` — the code that would send a real contract —
had never opened a socket, performed a handshake, framed a request or read a
reply. A fake client cannot catch a broken frame mask, a missing `req_id`, a
payload key the venue rejects, or a portfolio row the adapter cannot map.

This module starts a real TCP WebSocket server on loopback that speaks the
documented Deriv v3 request/response subset (authorize, balance, ticks,
ticks_history, proposal, buy, portfolio, proposal_open_contract, sell), then
drives the *production* ``DerivBroker`` over that socket with ``dry_run=False``.

What it proves: handshake, framing, request correlation, the submit path
(proposal -> buy -> ack), portfolio -> Position mapping, the documented refusal
to modify SL/TP on a multiplier contract, and the close path (sell).

What it does not prove, and says so in the report: that Deriv's own servers
behave like this one. Only a demo account does that. A loopback rehearsal is the
strongest thing that can run in CI on a box with no outbound network.

    apex rehearse                     # run it, print the steps
    apex rehearse --json              # machine-readable report
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from .data.ws import GUID, decode_frame, encode_frame

CAVEAT = (
    "Loopback venue: this rehearses the adapter against the documented Deriv wire "
    "subset, not against Deriv. Protocol, framing and order plumbing are proven; "
    "venue behaviour and fills are not."
)

#: symbols the venue will quote and accept orders for
REHEARSAL_SYMBOLS = ("R_10", "R_25", "R_50", "R_75", "R_100")


# --------------------------------------------------------------------------- #
# the venue
# --------------------------------------------------------------------------- #
@dataclass
class VenueConfig:
    symbols: tuple[str, ...] = REHEARSAL_SYMBOLS
    balance: float = 10_000.0
    currency: str = "USD"
    login: str = "REH-001"
    seed: int = 3
    tick_interval: float = 0.05
    fail_on: str = ""            # e.g. "buy" or "proposal": the venue refuses it
    reject_code: str = "ContractBuyValidationError"
    reject_message: str = "rehearsal: venue refused this request on purpose"


class LoopbackVenue:
    """A WebSocket server that answers the Deriv requests this desk sends."""

    def __init__(self, cfg: Optional[VenueConfig] = None):
        self.cfg = cfg or VenueConfig()
        self.port = 0
        self.requests: list[str] = []
        self.proposals: dict[str, dict] = {}
        self.contracts: dict[str, dict] = {}
        self.balance = float(self.cfg.balance)
        self._seq = 0
        self._server: Optional[asyncio.AbstractServer] = None
        self._tick_tasks: list[asyncio.Task] = []
        self._writers: set[Any] = set()
        self._price = {s: 1000.0 * (1 + 0.25 * i) for i, s in enumerate(self.cfg.symbols)}

    # -- lifecycle ---------------------------------------------------------- #
    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def close(self) -> None:
        for task in self._tick_tasks:
            task.cancel()
        for writer in list(self._writers):
            try:
                writer.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()

    # -- connection --------------------------------------------------------- #
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except Exception:
            writer.close()
            return
        key = ""
        for line in head.decode(errors="replace").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        if not key:
            writer.close()
            return
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        writer.write(
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        await writer.drain()
        self._writers.add(writer)
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    parsed, _ = decode_frame(buf)
                    if parsed is None:
                        break
                    opcode, payload, consumed = parsed
                    buf = buf[consumed:]
                    if opcode == 0x8:                      # close
                        return
                    if opcode == 0x9:                      # ping -> pong
                        writer.write(encode_frame(payload, opcode=0xA, mask=False))
                        await writer.drain()
                        continue
                    if opcode not in (0x1, 0x0):
                        continue
                    try:
                        msg = json.loads(payload.decode())
                    except (ValueError, UnicodeDecodeError):
                        continue
                    await self._dispatch(msg, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:
                pass

    # -- request handling --------------------------------------------------- #
    async def _dispatch(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        method = next((k for k in msg if k not in ("req_id", "passthrough")), "")
        self.requests.append(method)
        if self.cfg.fail_on and method == self.cfg.fail_on:
            out: dict = {"error": {"code": self.cfg.reject_code,
                                   "message": self.cfg.reject_message},
                         "msg_type": "error"}
        else:
            try:
                out = self._answer(method, msg)
            except Exception as exc:                     # a venue bug must look like an error
                out = {"error": {"code": "RehearsalError", "message": str(exc)},
                       "msg_type": "error"}
            out.setdefault("msg_type", method)
        if msg.get("req_id") is not None:
            out["req_id"] = msg["req_id"]
        writer.write(encode_frame(json.dumps(out).encode(), mask=False))
        await writer.drain()
        if method == "ticks" and "error" not in out:
            self._start_ticks(str(msg.get("ticks", "")), writer, msg.get("req_id"))

    def _answer(self, method: str, msg: dict) -> dict:
        if method == "ping":
            return {"ping": "pong"}
        if method == "authorize":
            return {"authorize": {"loginid": self.cfg.login, "currency": self.cfg.currency,
                                  "balance": round(self.balance, 2), "is_virtual": 1,
                                  "scopes": ["read", "trade"]}}
        if method == "balance":
            return {"balance": {"balance": round(self.balance, 2),
                                "currency": self.cfg.currency,
                                "loginid": self.cfg.login}}
        if method == "ticks":
            symbol = str(msg.get("ticks", ""))
            return {"tick": {"symbol": symbol, "quote": round(self._price.get(symbol, 1000.0), 4),
                             "epoch": int(time.time()), "id": f"tick-{self._seq}"},
                    "subscription": {"id": f"SUB-{symbol}"}}
        if method == "ticks_history":
            symbol = str(msg.get("ticks_history", ""))
            count = int(msg.get("count", 10) or 10)
            granularity = int(msg.get("granularity", 300) or 300)
            return {"candles": self._candles(symbol, count, granularity), "echo_req": msg}
        if method == "proposal":
            return {"proposal": self._proposal(msg)}
        if method == "buy":
            return {"buy": self._buy(str(msg.get("buy", "")))}
        if method == "portfolio":
            return {"portfolio": {"contracts": list(self.contracts.values())}}
        if method == "proposal_open_contract":
            cid = str(msg.get("contract_id", ""))
            contract = self.contracts.get(cid)
            if contract is None:
                raise KeyError(f"no open contract {cid}")
            return {"proposal_open_contract": contract}
        if method == "sell":
            return {"sell": self._sell(str(msg.get("sell", "")))}
        if method == "forget":
            return {"forget": 1}
        raise KeyError(f"unhandled request {method!r}")

    # -- domain ------------------------------------------------------------- #
    def _candles(self, symbol: str, count: int, granularity: int) -> list[dict]:
        base = self._price.get(symbol, 1000.0)
        end = int(time.time())
        out = []
        for i in range(count, 0, -1):
            wave = math.sin((i + self.cfg.seed) / 7.0)
            close = base * (1 + 0.0005 * wave)
            out.append({"epoch": end - i * granularity, "open": round(close, 4),
                        "high": round(close * 1.0004, 4), "low": round(close * 0.9996, 4),
                        "close": round(close, 4)})
        return out

    def _proposal(self, msg: dict) -> dict:
        self._seq += 1
        symbol = str(msg.get("symbol", ""))
        if symbol and symbol not in self._price:
            raise KeyError(f"unknown symbol {symbol}")
        stake = float(msg.get("amount", 1.0) or 1.0)
        pid = f"PROP-{self._seq}"
        spot = round(self._price.get(symbol, 1000.0), 4)
        self.proposals[pid] = {"symbol": symbol, "stake": stake, "spot": spot,
                               "contract_type": msg.get("contract_type", ""),
                               "multiplier": msg.get("multiplier"),
                               "limit_order": msg.get("limit_order") or {}}
        return {"id": pid, "ask_price": round(stake, 2), "spot": spot,
                "payout": round(stake * 1.03, 2), "longcode": f"rehearsal {symbol}",
                "display_value": str(stake),
                "date_start": int(time.time())}

    def _buy(self, proposal_id: str) -> dict:
        prop = self.proposals.get(proposal_id)
        if prop is None:
            raise KeyError(f"unknown proposal {proposal_id}")
        self._seq += 1
        cid = str(9000 + self._seq)
        price = round(prop["stake"], 2)
        self.balance = round(self.balance - price, 2)
        self.contracts[cid] = {
            "contract_id": cid, "symbol": prop["symbol"],
            "contract_type": prop["contract_type"],
            "buy_price": price, "current_spot": prop["spot"],
            "multiplier": prop["multiplier"] or 100.0,
            "purchase_time": int(time.time()), "payout": round(price * 1.03, 2),
            "is_sold": 0,
        }
        return {"contract_id": cid, "buy_price": price,
                "balance_after": self.balance, "payout": self.contracts[cid]["payout"],
                "transaction_id": 500000 + self._seq, "start_time": int(time.time()),
                "longcode": f"rehearsal {prop['symbol']}"}

    def _sell(self, contract_id: str) -> dict:
        contract = self.contracts.pop(contract_id, None)
        if contract is None:
            raise KeyError(f"no open contract {contract_id}")
        sold_for = round(float(contract["buy_price"]) * 0.995, 2)   # a small, honest loss
        self.balance = round(self.balance + sold_for, 2)
        return {"sold_for": sold_for, "balance_after": self.balance,
                "transaction_id": 600000 + self._seq}

    # -- tick stream -------------------------------------------------------- #
    def _start_ticks(self, symbol: str, writer: asyncio.StreamWriter, req_id) -> None:
        if not symbol:
            return

        async def pump() -> None:
            try:
                while True:
                    await asyncio.sleep(self.cfg.tick_interval)
                    self._seq += 1
                    price = self._price.get(symbol, 1000.0) * (1 + 0.0002 * math.sin(self._seq / 5))
                    msg = {"tick": {"symbol": symbol, "quote": round(price, 4),
                                    "epoch": int(time.time()), "id": f"tick-{self._seq}"},
                           "subscription": {"id": f"SUB-{symbol}"},
                           "msg_type": "tick", "req_id": req_id}
                    writer.write(encode_frame(json.dumps(msg).encode(), mask=False))
                    await writer.drain()
            except (ConnectionError, asyncio.CancelledError, RuntimeError):
                return

        self._tick_tasks.append(asyncio.create_task(pump()))


class _ServerThread:
    """Run the venue's event loop beside the synchronous adapter code."""

    def __init__(self, venue: LoopbackVenue):
        self.venue = venue
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None

    def start(self, timeout: float = 10.0) -> int:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="apex-rehearsal-venue")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("the rehearsal venue did not start")
        if self._error is not None:
            raise RuntimeError(f"the rehearsal venue failed: {self._error}")
        return self.venue.port

    def _run(self) -> None:
        assert self.loop is not None
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.venue.start())
        except BaseException as exc:                    # surface it to the caller
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        self.loop.run_forever()

    def stop(self) -> None:
        if self.loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.venue.close(), self.loop).result(timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)


# --------------------------------------------------------------------------- #
# the rehearsal
# --------------------------------------------------------------------------- #
def rehearse_deriv(symbol: str = "R_25", out_dir: str = "",
                   event_timeout: float = 10.0,
                   venue_cfg: Optional[VenueConfig] = None) -> dict:
    """Drive the real Deriv adapter over a loopback socket and report every step."""
    from .data.deriv import DerivConfig
    from .execution.base import SymbolRegistry
    from .execution.deriv import DerivBroker
    from .models import Order, Side

    venue = LoopbackVenue(venue_cfg)
    server = _ServerThread(venue)
    started = time.time()
    steps: list[dict] = []
    broker: Optional[DerivBroker] = None
    report: dict[str, Any] = {
        "venue": "deriv", "endpoint": "", "symbol": symbol,
        "dry_run": False, "caveat": CAVEAT, "started_ts": started,
    }

    def step(name: str, ok: bool, detail: str = "", critical: bool = True) -> bool:
        steps.append({"name": name, "ok": bool(ok), "detail": detail,
                      "critical": bool(critical)})
        return bool(ok)

    try:
        port = server.start()
        report["endpoint"] = f"ws://127.0.0.1:{port}/websockets/v3"
        cfg = DerivConfig(endpoint=f"ws://127.0.0.1:{port}/websockets/v3?app_id=1089",
                          token="REHEARSAL-TOKEN", currency="USD")
        broker = DerivBroker(account="REH-001", cfg=cfg,
                             registry=SymbolRegistry(), dry_run=False)
        # 1. handshake over a real socket
        broker.connect()
        step("connect", broker.api is not None and not broker.api.closed,
             f"websocket handshake to {report['endpoint']}")
        step("authorize", bool(getattr(broker.api, "authorized", False)),
             f"session login {getattr(broker.api, 'logged_in', '') or 'anonymous'}",
             critical=False)

        # 2. account read
        acct = broker.account()
        step("account", abs(acct.balance - venue.cfg.balance) < 0.01,
             f"balance {acct.balance:.2f} {acct.currency} live={acct.live}")

        # 3. market data: one tick through the real feed subscription
        got: list[dict] = []
        broker._runner.call(broker.api.subscribe_ticks(symbol, got.append))  # noqa: SLF001
        deadline = time.time() + event_timeout
        while not got and time.time() < deadline:
            time.sleep(0.02)
        step("ticks", bool(got),
             f"first quote {got[0]['tick']['quote']}" if got else "no tick within timeout")

        # 4. the order path that would be real money
        price = float(got[0]["tick"]["quote"]) if got else 1000.0
        order = Order(symbol=symbol, side=Side.BUY, lots=1.0, broker="deriv",
                      account="REH-001", strategy="rehearsal", price=price,
                      sl=round(price * 0.995, 4), tp=round(price * 1.01, 4),
                      tag="rehearsal")
        ack = broker.submit(order)
        step("submit", ack.ok, f"ack ok={ack.ok} id={ack.order_id} reason={ack.reason or '-'}")
        step("sl_tp_forwarded",
             bool(venue.proposals.get(f"PROP-{1}", {}).get("limit_order")),
             f"proposal carried limit_order={venue.proposals.get('PROP-1', {}).get('limit_order')}",
             critical=False)

        # 5. portfolio -> Position mapping
        positions = broker.positions()
        mine = [p for p in positions if p.symbol == symbol]
        step("positions", len(mine) == 1,
             f"{len(mine)} position(s) mapped from the venue portfolio")
        if mine:
            step("position_side", mine[0].side is Side.BUY,
                 f"side {mine[0].side.value}, id {mine[0].id}", critical=False)

        # 6. the documented limitation: no SL/TP modification on a multiplier
        if mine:
            mod = broker.modify(mine[0], sl=price * 0.99, tp=None)
            step("modify_refused", not mod.ok and "not supported" in mod.reason.lower(),
                 mod.reason, critical=False)

        # 7. close path
        if mine:
            closed = broker.close(mine[0], "rehearsal")
            step("close", closed.ok, f"sell ok={closed.ok} id={closed.order_id}")
            step("flat_after_close", not broker.positions(),
                 "venue holds no contract after the close")
        step("session_clean", all(
            name in venue.requests for name in ("proposal", "buy", "portfolio", "sell")),
            "requests sent: " + ", ".join(dict.fromkeys(venue.requests)))
    except Exception as exc:
        step("rehearsal", False, f"{type(exc).__name__}: {exc}")
    finally:
        if broker is not None and broker.api is not None:
            try:
                broker._runner.call(broker.api.close())     # noqa: SLF001 - shutdown path
            except Exception:
                pass
        server.stop()

    critical = [s for s in steps if s["critical"]]
    report.update({
        "ok": all(s["ok"] for s in critical) and bool(steps),
        "steps": steps,
        "requests": list(dict.fromkeys(venue.requests)),
        "elapsed_s": round(time.time() - started, 3),
    })
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, "rehearsal.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        report["path"] = path
    return report


def as_text(report: dict) -> str:
    lines = [f"rehearsal — {report.get('venue')} adapter over a loopback socket",
             f"  endpoint   : {report.get('endpoint')}",
             f"  dry_run    : {report.get('dry_run')}  (the production submit path ran)"]
    for s in report.get("steps", []):
        mark = "ok  " if s["ok"] else ("FAIL" if s["critical"] else "warn")
        lines.append(f"  [{mark}] {s['name']:<18} {s['detail']}")
    verdict = "PASS" if report.get("ok") else "FAIL"
    lines.append(f"  verdict    : {verdict} in {report.get('elapsed_s')}s")
    lines.append(f"  caveat     : {report.get('caveat')}")
    return "\n".join(lines)
