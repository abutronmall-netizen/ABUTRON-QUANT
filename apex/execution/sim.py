"""Deterministic-ish paper broker.

Models the things that quietly kill scalping systems and are almost always
missing from backtests: spread crossing, slippage (worse on stop exits), per-lot
commission, rejected orders and a real margin check. The same broker object is
used for paper trading on live feeds and for historical backtests.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Optional

from ..models import AccountState, Ack, Order, OrderKind, Position, Quote, Side, SymbolSpec, new_client_id
from .base import SymbolRegistry


@dataclass
class SimConfig:
    initial_balance: float = 10_000.0
    currency: str = "USD"
    leverage: float = 100.0
    slippage_bps: float = 0.6           # adverse slippage on market entries
    stop_slippage_bps: float = 2.5      # stops fill worse than the trigger
    reject_probability: float = 0.0     # broker-side rejections
    max_spread_bps: float = 25.0        # reject when spread explodes
    allow_duplicate_symbols: bool = False
    seed: int = 7


class SimBroker:
    name = "sim"

    def __init__(self, registry: Optional[SymbolRegistry] = None, cfg: Optional[SimConfig] = None,
                 account: str = "SIM-001"):
        self.registry = registry or SymbolRegistry()
        self.cfg = cfg or SimConfig()
        self.account_id = account
        self.rng = random.Random(self.cfg.seed)
        self.balance = self.cfg.initial_balance
        self._positions: dict[str, Position] = {}
        self.quotes: dict[str, Quote] = {}
        self._last_close: dict[str, float] = {}
        self.fills: list[Ack] = []
        self.rejections: list[Ack] = []
        self._seq = 0
        self._now: Optional[float] = None

    def set_time(self, ts: float) -> None:
        """Backtests advance broker time with the bars; live leaves it as wall clock."""
        self._now = ts

    def _ts(self) -> float:
        return self._now if self._now is not None else time.time()

    # ------------------------------------------------------------------ #
    def set_quote(self, q: Quote) -> None:
        self.quotes[q.symbol] = q

    def quote(self, symbol: str) -> Optional[Quote]:
        return self.quotes.get(symbol)

    def spec(self, symbol: str) -> SymbolSpec:
        return self.registry.spec(self.name.replace("sim", "deriv"), symbol) \
            if symbol.startswith(("R_", "1HZ", "BOOM", "CRASH", "JD")) \
            else self.registry.spec("mt5", symbol)

    # ------------------------------------------------------------------ #
    def _price(self, symbol: str, side: Side, entry: bool) -> Optional[float]:
        q = self.quotes.get(symbol)
        if q is None:
            return None
        base = q.ask if side is Side.BUY else q.bid
        bps = (self.cfg.slippage_bps if entry else self.cfg.stop_slippage_bps) / 10_000.0
        slip = base * bps * (0.5 + self.rng.random())     # always adverse
        return base + side.sign * slip

    def _spread_ok(self, symbol: str) -> bool:
        q = self.quotes.get(symbol)
        if q is None or q.mid <= 0:
            return False
        return (q.spread / q.mid) * 10_000.0 <= self.cfg.max_spread_bps

    # ------------------------------------------------------------------ #
    def account(self) -> AccountState:
        unreal = 0.0
        margin = 0.0
        for p in self._positions.values():
            q = self.quotes.get(p.symbol)
            spec = self.spec(p.symbol)
            if q is not None:
                price = q.bid if p.side is Side.BUY else q.ask
                unreal += p.unrealised(price, spec.value_per_point_per_lot)
            notional = p.lots * max(spec.contract_size, 1.0) * p.entry
            margin += (p.lots * spec.margin_per_lot) if spec.margin_per_lot \
                else notional / max(spec.leverage, 1.0)
        eq = self.balance + unreal
        return AccountState(broker="sim", account=self.account_id, currency=self.cfg.currency,
                            balance=round(self.balance, 2), equity=round(eq, 2),
                            margin_used=round(margin, 2), free_margin=round(eq - margin, 2),
                            leverage=self.cfg.leverage, ts=self._ts(), live=False)

    def positions(self) -> list[Position]:
        """ExecutionPort surface."""
        return list(self._positions.values())

    positions_list = positions

    # ------------------------------------------------------------------ #
    def submit(self, order: Order) -> Ack:
        ts = self._ts()
        if order.lots <= 0:
            return self._reject(order, "invalid lots", ts)
        if not self._spread_ok(order.symbol):
            return self._reject(order, "market/spread unavailable", ts, retryable=True)
        if not self.cfg.allow_duplicate_symbols and any(
                p.symbol == order.symbol for p in self._positions.values()):
            return self._reject(order, "duplicate symbol", ts)
        if self.cfg.reject_probability and self.rng.random() < self.cfg.reject_probability:
            return self._reject(order, "broker rejected", ts, retryable=True)

        spec = self.spec(order.symbol)
        price = order.price if order.kind is OrderKind.LIMIT and order.price else \
            self._price(order.symbol, order.side, entry=True)
        if price is None:
            return self._reject(order, "no quote", ts, retryable=True)

        acct = self.account()
        notional = order.lots * max(spec.contract_size, 1.0) * price
        margin = (order.lots * spec.margin_per_lot) if spec.margin_per_lot \
            else notional / max(spec.leverage, 1.0)
        if margin > acct.free_margin:
            return self._reject(order, f"insufficient margin ({margin:.2f} > {acct.free_margin:.2f})", ts)

        commission = order.lots * spec.commission_per_lot
        self.balance -= commission
        self._seq += 1
        pid = f"SIM-{self._seq:06d}"
        pos = Position(
            id=pid, symbol=order.symbol, side=order.side, lots=order.lots,
            entry=price, sl=order.sl, tp=order.tp, opened_ts=ts, broker="sim",
            account=self.account_id, strategy=order.strategy, contract_id=pid,
            commission=commission,
            meta={"client_id": order.client_id, "tag": order.tag, **order.meta},
        )
        self._positions[pid] = pos
        ack = Ack(ok=True, client_id=order.client_id, ts=ts, order_id=pid,
                  filled_price=round(price, spec.digits), lots=order.lots,
                  commission=round(commission, 4), broker="sim", account=self.account_id)
        self.fills.append(ack)
        return ack

    def _reject(self, order: Order, reason: str, ts: float, retryable: bool = False) -> Ack:
        ack = Ack(ok=False, client_id=order.client_id, ts=ts, reason=reason,
                  retryable=retryable, broker="sim", account=self.account_id)
        self.rejections.append(ack)
        return ack

    # ------------------------------------------------------------------ #
    def close(self, position: Position, reason: str, price: Optional[float] = None,
              volume: Optional[float] = None) -> Ack:
        ts = self._ts()
        spec = self.spec(position.symbol)
        px = price if price is not None else self._price(position.symbol, position.side.flip(), entry=False)
        if px is None:
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts, reason="no quote",
                       broker="sim", account=self.account_id, retryable=True)
        lots = position.lots if volume is None else min(float(volume), position.lots)
        if lots <= 0:
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts, reason="zero volume",
                       broker="sim", account=self.account_id)
        pnl = (px - position.entry) * position.side.sign * lots * spec.value_per_point_per_lot
        commission = lots * spec.commission_per_lot
        self.balance += pnl - commission
        book = self._positions.get(position.id)
        partial = lots < position.lots - 1e-9
        if book is not None and partial:
            book.lots = round(book.lots - lots, 8)
        else:
            self._positions.pop(position.id, None)
        ack = Ack(ok=True, client_id=new_client_id("close"), ts=ts, order_id=f"CLS-{position.id}",
                  filled_price=round(px, spec.digits), lots=lots,
                  commission=round(commission, 4), broker="sim", account=self.account_id,
                  raw={"reason": reason, "pnl": round(pnl - commission, 2),
                       "partial": partial})
        self.fills.append(ack)
        return ack

    def modify(self, position: Position, sl: Optional[float], tp: Optional[float]) -> Ack:
        p = self._positions.get(position.id)
        if p is None:
            return Ack(ok=False, client_id=new_client_id("mod"), ts=self._ts(),
                       reason="position not found", broker="sim", account=self.account_id)
        if sl is not None:
            p.sl = sl
        if tp is not None:
            p.tp = tp
        return Ack(ok=True, client_id=new_client_id("mod"), ts=self._ts(), order_id=p.id,
                   broker="sim", account=self.account_id)

    # ------------------------------------------------------------------ #
    def reconcile(self) -> list[dict]:
        return []      # sim broker cannot diverge from itself

    def flatten_all(self, reason: str = "manual") -> list[Ack]:
        return [self.close(p, reason) for p in list(self._positions.values())]
