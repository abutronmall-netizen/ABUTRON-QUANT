"""Pending order book.

An order the broker *accepted* but did not fill is still live risk. The first
build treated `TRADE_RETCODE_PLACED` as a rejection and forgot about it, which
means an order placed while the market was closed could fill hours later with
nobody managing it, and possibly alongside a second entry on the same symbol.

This module keeps those orders, asks the venue what happened to them, and hands
the engine either a real position or a clean cancellation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PendingOrder:
    """Everything needed to turn a late fill into a managed position."""

    client_id: str
    order_id: str
    broker: str
    account: str
    symbol: str
    side: str                    # "BUY" | "SELL"
    lots: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    entry_hint: float = 0.0
    created_ts: float = field(default_factory=time.time)
    expires_ts: float = 0.0
    risk_money: float = 0.0
    strategy: str = ""
    checks: int = 0
    last_state: str = "pending"
    meta: dict = field(default_factory=dict)

    def expired(self, now: Optional[float] = None) -> bool:
        if not self.expires_ts:
            return False
        return (now if now is not None else time.time()) >= self.expires_ts

    def as_dict(self) -> dict:
        return {"client_id": self.client_id, "order_id": self.order_id,
                "broker": self.broker, "account": self.account, "symbol": self.symbol,
                "side": self.side, "lots": self.lots, "sl": self.sl, "tp": self.tp,
                "entry_hint": self.entry_hint, "created_ts": self.created_ts,
                "expires_ts": self.expires_ts, "risk_money": self.risk_money,
                "strategy": self.strategy, "checks": self.checks,
                "last_state": self.last_state, "meta": self.meta}

    @staticmethod
    def from_dict(row: dict) -> "PendingOrder":
        return PendingOrder(
            client_id=row.get("client_id", ""), order_id=row.get("order_id", ""),
            broker=row.get("broker", ""), account=row.get("account", ""),
            symbol=row.get("symbol", ""), side=row.get("side", "BUY"),
            lots=float(row.get("lots") or 0.0), sl=row.get("sl"), tp=row.get("tp"),
            entry_hint=float(row.get("entry_hint") or 0.0),
            created_ts=float(row.get("created_ts") or 0.0),
            expires_ts=float(row.get("expires_ts") or 0.0),
            risk_money=float(row.get("risk_money") or 0.0),
            strategy=row.get("strategy", ""), checks=int(row.get("checks") or 0),
            last_state=row.get("last_state", "pending"), meta=row.get("meta") or {})


class PendingBook:
    """Small, ordered, serialisable map of outstanding orders."""

    def __init__(self, rows: Optional[list[dict]] = None):
        self._rows: dict[str, PendingOrder] = {}
        for row in rows or []:
            try:
                po = PendingOrder.from_dict(row)
            except Exception:
                continue
            if po.client_id:
                self._rows[po.client_id] = po

    def add(self, po: PendingOrder) -> None:
        self._rows[po.client_id] = po

    def get(self, client_id: str) -> Optional[PendingOrder]:
        return self._rows.get(client_id)

    def remove(self, client_id: str) -> None:
        self._rows.pop(client_id, None)

    def items(self) -> list[PendingOrder]:
        return sorted(self._rows.values(), key=lambda p: p.created_ts)

    def export(self) -> list[dict]:
        return [p.as_dict() for p in self.items()]

    def __len__(self) -> int:
        return len(self._rows)
