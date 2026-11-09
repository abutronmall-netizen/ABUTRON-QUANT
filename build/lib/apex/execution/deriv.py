"""Deriv execution adapter (multiplier contracts on synthetic indices).

Deriv does not sell CFDs on its synthetics; it sells *contracts*. The adapter maps
the engine's uniform `lots` concept onto contract stake so that risk sizing stays
identical across brokers:

    money per 1.0 price point  ==  lots * value_per_point_per_lot
    multiplier contract P&L/point  ==  stake * multiplier / entry_price
    => stake = lots * value_per_point_per_lot * entry_price / multiplier

Everything is reconciled back from Deriv's own portfolio, because contract P&L is
authoritative and CFD maths is only a sizing approximation.

`dry_run=True` (the default) never transmits an order. Enable live trading only
after a demo account has produced broker-confirmed fills and P&L statements.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Optional

from ..data.deriv import DerivAPI, DerivConfig, DerivError
from ..models import (AccountState, Ack, Order, Position, Quote, Side, SymbolSpec,
                      new_client_id)
from .base import SymbolRegistry


class AsyncRunner:
    """Runs one asyncio loop in a daemon thread and exposes sync calls to it."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True, name="deriv-loop")
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def call(self, coro, timeout: float = 30.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def stop(self) -> None:
        async def _drain() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        try:
            asyncio.run_coroutine_threadsafe(_drain(), self.loop).result(timeout=3)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=3)


class DerivBroker:
    name = "deriv"

    def __init__(self, account: str, cfg: Optional[DerivConfig] = None,
                 registry: Optional[SymbolRegistry] = None,
                 api: Optional[DerivAPI] = None, dry_run: bool = True,
                 multiplier: float = 100.0,
                 runner: Optional[AsyncRunner] = None):
        self.account_id = account
        self.cfg = cfg or DerivConfig()
        self.registry = registry or SymbolRegistry()
        self.multiplier = multiplier
        self.dry_run = dry_run
        self._runner = runner or AsyncRunner()
        self.api = api  # injectable for tests
        self._connected = False
        self._dry_positions: dict[str, Position] = {}
        self._dry_balance = 10_000.0
        self._seq = 0

    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        if self.api is not None:
            self._connected = True
            return True
        self.api = DerivAPI(self.cfg)
        self._runner.call(self.api.connect())
        self._connected = True
        return True

    def spec(self, symbol: str) -> SymbolSpec:
        return self.registry.spec("deriv", symbol)

    def quote(self, symbol: str) -> Optional[Quote]:
        return None      # quotes come from the feed, not the order adapter

    # ------------------------------------------------------------------ #
    def account(self) -> AccountState:
        if self.dry_run or self.api is None:
            equity = self._dry_balance + sum(
                p.unrealised(p.entry, 1.0) for p in self._dry_positions.values())
            return AccountState(broker="deriv", account=self.account_id, equity=equity,
                                balance=self._dry_balance, free_margin=equity,
                                ts=time.time(), live=False)
        bal = self._runner.call(self.api.get_balance())
        return AccountState(broker="deriv", account=self.account_id,
                            currency=self.api.currency, balance=bal, equity=bal,
                            free_margin=bal, ts=time.time(), live=True)

    def positions(self) -> list[Position]:
        if self.dry_run or self.api is None:
            return list(self._dry_positions.values())
        contracts = self._runner.call(self.api.portfolio())
        out = []
        for c in contracts:
            symbol = c.get("symbol", "")
            stake = float(c.get("buy_price") or 0.0)
            spot = float(c.get("current_spot") or 0.0)
            lots, estimated = self._lots_from_contract(symbol, stake, spot,
                                                       c.get("multiplier"))
            out.append(Position(
                id=str(c.get("contract_id")), symbol=symbol,
                # The portfolio endpoint does not say whether the contract is long
                # or short. Guessing BUY here used to mislabel every MULTDOWN.
                side=(Side.SELL if str(c.get("contract_type", "")).upper().startswith("MULTDOWN")
                      else Side.BUY),
                lots=lots, entry=spot or stake, sl=None, tp=None,
                opened_ts=float(c.get("purchase_time", time.time())),
                broker="deriv", account=self.account_id, strategy="deriv",
                contract_id=str(c.get("contract_id")),
                # `estimated` tells reconciliation to compare lots loosely: a
                # Deriv contract has a stake, not a lot count, and buy_price is
                # the stake. Treating it as lots made every live position look
                # like a size mismatch.
                meta={"raw": c, "stake": stake, "lots_estimated": estimated},
            ))
        return out

    def _lots_from_contract(self, symbol: str, stake: float, spot: float,
                            multiplier) -> tuple[float, bool]:
        """Invert `_stake_for`: stake -> notional lots. Always an estimate."""
        try:
            mult = float(multiplier or self.multiplier)
        except (TypeError, ValueError):
            mult = self.multiplier
        spec = self.spec(symbol) if symbol else None
        vpp = spec.value_per_point_per_lot if spec is not None else 0.0
        if stake > 0 and spot > 0 and vpp > 0 and mult > 0:
            return round(stake * mult / (vpp * spot), 6), True
        return 0.0, True

    # ------------------------------------------------------------------ #
    def _stake_for(self, order: Order, price: float) -> float:
        spec = self.spec(order.symbol)
        stake = order.lots * spec.value_per_point_per_lot * price / max(self.multiplier, 1e-9)
        return round(max(stake, 0.35), 2)      # Deriv minimum stake

    def submit(self, order: Order) -> Ack:
        ts = time.time()
        entry = order.price or 0.0
        if entry <= 0:
            return Ack(ok=False, client_id=order.client_id, ts=ts,
                       reason="entry price required for Deriv contracts",
                       broker="deriv", account=self.account_id, retryable=True)
        stake = self._stake_for(order, entry)

        if self.dry_run or self.api is None:
            self._seq += 1
            pid = f"DRY-DV-{self._seq}"
            pos = Position(id=pid, symbol=order.symbol, side=order.side, lots=order.lots,
                           entry=entry, sl=order.sl, tp=order.tp, opened_ts=ts,
                           broker="deriv", account=self.account_id,
                           strategy=order.strategy, contract_id=pid,
                           meta={"dry_run": True, "stake": stake,
                                 "multiplier": self.multiplier})
            self._dry_positions[pid] = pos
            return Ack(ok=True, client_id=order.client_id, ts=ts, order_id=pid,
                       filled_price=entry, lots=order.lots, broker="deriv",
                       account=self.account_id, raw={"dry_run": True, "stake": stake})

        contract_type = "MULTUP" if order.side is Side.BUY else "MULTDOWN"
        try:
            prop = self._runner.call(self.api.proposal(
                order.symbol, contract_type, stake, multiplier=self.multiplier,
                stop_loss=order.sl, take_profit=order.tp, currency=self.cfg.currency))
            if not prop or "id" not in prop:
                return Ack(ok=False, client_id=order.client_id, ts=ts,
                           reason=f"proposal rejected: {prop}", broker="deriv",
                           account=self.account_id, retryable=True)
            buy = self._runner.call(self.api.buy(prop["id"], float(prop.get("ask_price", stake))))
        except DerivError as exc:
            return Ack(ok=False, client_id=order.client_id, ts=ts, reason=str(exc),
                       broker="deriv", account=self.account_id,
                       retryable=exc.code in ("RateLimit", "ContractBuyValidationError"))
        if not buy or "contract_id" not in buy:
            return Ack(ok=False, client_id=order.client_id, ts=ts,
                       reason=f"buy failed: {buy}", broker="deriv",
                       account=self.account_id, retryable=True)
        return Ack(ok=True, client_id=order.client_id, ts=ts,
                   order_id=str(buy["contract_id"]),
                   filled_price=float(buy.get("buy_price", entry)), lots=order.lots,
                   broker="deriv", account=self.account_id,
                   raw={"contract_id": buy["contract_id"], "stake": stake,
                        "payout": buy.get("payout")})

    def close(self, position: Position, reason: str, price: Optional[float] = None,
              volume: Optional[float] = None) -> Ack:
        ts = time.time()
        if volume is not None and 0 < float(volume) < position.lots - 1e-9:
            # A multiplier contract is sold whole. Pretending to scale out here
            # would leave the engine's book believing in a position the broker
            # still holds in full, so refuse and let the caller close outright.
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts,
                       reason="Deriv multiplier contracts cannot be partially closed; "
                              "close the full position instead",
                       broker="deriv", account=self.account_id, retryable=False)
        if self.dry_run or self.api is None:
            self._dry_positions.pop(position.id, None)
            return Ack(ok=True, client_id=new_client_id("close"), ts=ts,
                       order_id=f"CLS-{position.id}", filled_price=position.entry,
                       lots=position.lots, broker="deriv", account=self.account_id,
                       raw={"dry_run": True, "reason": reason})
        try:
            sold = self._runner.call(self.api.sell(position.contract_id or position.id, 0.0))
        except DerivError as exc:
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts, reason=str(exc),
                       broker="deriv", account=self.account_id, retryable=True)
        return Ack(ok=True, client_id=new_client_id("close"), ts=ts,
                   order_id=position.id, filled_price=float(sold.get("sold_for", 0.0)) or position.entry,
                   lots=position.lots, broker="deriv", account=self.account_id,
                   raw=sold)

    def modify(self, position: Position, sl: Optional[float], tp: Optional[float]) -> Ack:
        # Deriv multiplier contracts allow SL/TP updates only through the contract
        # update endpoint for some account types; unsupported here rather than faked.
        return Ack(ok=False, client_id=new_client_id("mod"), ts=time.time(),
                   reason="Deriv SL/TP modification not supported by this adapter; "
                          "the engine manages exits with early sell instead",
                   broker="deriv", account=self.account_id, retryable=False)
