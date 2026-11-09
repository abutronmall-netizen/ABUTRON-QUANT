"""Unified broker router.

The audit's P0-4 was "FP Markets is outside the active Deriv-only router". Here
routing is by (broker, account) with per-broker adapters and a symbol allow-list,
so Deriv synthetics and FP Markets MT5 instruments flow through one order path
with one idempotency and reconciliation policy.

Guarantees:
  * idempotency  - a client_id is executed at most once, retries are safe;
  * no silent drops - every order produces an Ack and an event;
  * reconciliation - internal bookkeeping is compared with broker truth;
  * kill switch  - one flag blocks all new risk and can flatten everything.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..models import AccountState, Ack, Order, Position, Quote, Side, SymbolSpec
from .base import ExecutionPort, SymbolRegistry
from .sim import SimBroker


@dataclass
class Route:
    broker: str
    account: str
    port: ExecutionPort
    symbols: list[str] = field(default_factory=list)   # empty -> adapter decides
    enabled: bool = True
    label: str = ""

    def handles(self, symbol: str) -> bool:
        return self.enabled and (not self.symbols or symbol in self.symbols)


class BrokerRouter:
    def __init__(self, routes: list[Route], registry: Optional[SymbolRegistry] = None,
                 store=None, max_retries: int = 2, retry_sleep: float = 0.25):
        if not routes:
            raise ValueError("router needs at least one route")
        self.routes = routes
        self.registry = registry or SymbolRegistry()
        self.store = store
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep
        self.killed = False
        self._acks: dict[str, Ack] = {}
        self.stats = {"submitted": 0, "filled": 0, "rejected": 0, "retried": 0,
                      "closed": 0, "modifications": 0, "duplicates": 0, "errors": 0,
                      "partial": 0, "pending": 0, "cancels": 0}

    # ------------------------------------------------------------------ #
    def route_for(self, symbol: str, broker: Optional[str] = None,
                  account: Optional[str] = None) -> Optional[Route]:
        for r in self.routes:
            if broker and r.broker != broker:
                continue
            if account and r.account != account:
                continue
            if r.handles(symbol):
                return r
        return None

    def route_by_account(self, account: str) -> Optional[Route]:
        for r in self.routes:
            if r.account == account:
                return r
        return None

    def spec(self, symbol: str, broker: Optional[str] = None) -> SymbolSpec:
        r = self.route_for(symbol, broker)
        b = r.broker if r else (broker or "sim")
        return self.registry.spec(b, symbol) if hasattr(self.registry, "spec") else SymbolSpec(symbol, b)

    def account(self, route: Optional[Route] = None) -> AccountState:
        r = route or self.routes[0]
        return r.port.account()

    def accounts(self) -> list[AccountState]:
        out = []
        for r in self.routes:
            try:
                out.append(r.port.account())
            except Exception as exc:            # a dead adapter must not stop the desk
                out.append(AccountState(broker=r.broker, account=r.account, ts=time.time(),
                                        equity=0.0, balance=0.0, free_margin=0.0))
                self.stats["errors"] += 1
                self._event("error", f"{r.broker}:{r.account} account read failed: {exc}")
        return out

    # ------------------------------------------------------------------ #
    def submit(self, order: Order) -> Ack:
        if self.killed:
            return Ack(ok=False, client_id=order.client_id, ts=time.time(),
                       reason="kill switch engaged", broker=order.broker, account=order.account)
        if order.client_id in self._acks:
            self.stats["duplicates"] += 1
            return self._acks[order.client_id]

        route = self.route_for(order.symbol, order.broker, order.account)
        if route is None:
            ack = Ack(ok=False, client_id=order.client_id, ts=time.time(),
                      reason=f"no route for {order.symbol} on {order.broker or 'any'}")
            self._record(order, ack)
            self.stats["rejected"] += 1
            return ack

        self.stats["submitted"] += 1
        ack = None
        for attempt in range(self.max_retries + 1):
            try:
                ack = route.port.submit(order)
            except Exception as exc:
                ack = Ack(ok=False, client_id=order.client_id, ts=time.time(),
                          reason=f"adapter error: {exc}", retryable=True,
                          broker=route.broker, account=route.account)
                self.stats["errors"] += 1
            if ack.ok or not ack.retryable:
                break
            self.stats["retried"] += 1
            if attempt < self.max_retries and self.retry_sleep:
                time.sleep(self.retry_sleep * (attempt + 1))

        if ack is not None and ack.ok:
            self.stats["filled"] += 1
            if getattr(ack, "state", "filled") == "partial":
                self.stats["partial"] += 1
            elif getattr(ack, "state", "filled") == "pending":
                self.stats["pending"] += 1
        else:
            self.stats["rejected"] += 1
        self._record(order, ack, route)
        return ack

    def close(self, position: Position, reason: str, price: Optional[float] = None,
              volume: Optional[float] = None) -> Ack:
        route = self.route_by_account(position.account) or self.route_for(position.symbol)
        if route is None:
            return Ack(ok=False, client_id=f"close-{position.id}", ts=time.time(),
                       reason="no route for close")
        try:
            try:
                ack = route.port.close(position, reason, price, volume=volume)
            except TypeError:                     # adapter without volume support
                if volume is not None and volume < position.lots - 1e-9:
                    return Ack(ok=False, client_id=f"close-{position.id}", ts=time.time(),
                               reason="adapter does not support partial closes")
                try:
                    ack = route.port.close(position, reason, price)
                except TypeError:                 # adapter without explicit price
                    ack = route.port.close(position, reason)
        except Exception as exc:
            ack = Ack(ok=False, client_id=f"close-{position.id}", ts=time.time(),
                      reason=f"close error: {exc}", retryable=True)
            self.stats["errors"] += 1
        if ack.ok:
            self.stats["closed"] += 1
            if ack.lots and ack.lots < position.lots - 1e-9:
                self.stats["partial"] = self.stats.get("partial", 0) + 1
        self._event("close", f"{position.symbol} {ack.lots or position.lots} lots -> {reason} "
                             f"@ {ack.filled_price}")
        return ack

    def order_status(self, broker: str, account: str, order_id: str) -> Optional[dict]:
        """Ask the venue what happened to an order it accepted but did not fill."""
        route = self.route_by_account(account) or self.route_for("", broker)
        if route is None:
            return None
        fn = getattr(route.port, "order_status", None)
        if fn is None:
            return None
        try:
            return fn(order_id)
        except Exception as exc:
            self.stats["errors"] += 1
            self._event("error", f"order_status failed for {order_id}: {exc}")
            return None

    def cancel(self, broker: str, account: str, order_id: str) -> Ack:
        route = self.route_by_account(account) or self.route_for("", broker)
        if route is None:
            return Ack(ok=False, client_id=f"cancel-{order_id}", ts=time.time(),
                       reason="no route for cancel")
        fn = getattr(route.port, "cancel", None)
        if fn is None:
            return Ack(ok=False, client_id=f"cancel-{order_id}", ts=time.time(),
                       reason="adapter cannot cancel orders")
        self.stats["cancels"] = self.stats.get("cancels", 0) + 1
        try:
            ack = fn(order_id)
        except Exception as exc:
            return Ack(ok=False, client_id=f"cancel-{order_id}", ts=time.time(),
                       reason=f"cancel error: {exc}")
        self._event("order", f"CANCEL {order_id}: {'ok' if ack.ok else ack.reason}")
        return ack

    def modify(self, position: Position, sl: Optional[float], tp: Optional[float]) -> Ack:
        route = self.route_by_account(position.account) or self.route_for(position.symbol)
        if route is None:
            return Ack(ok=False, client_id=f"mod-{position.id}", ts=time.time(), reason="no route")
        ack = route.port.modify(position, sl, tp)
        if ack.ok:
            self.stats["modifications"] += 1
        return ack

    def quote(self, symbol: str) -> Optional[Quote]:
        r = self.route_for(symbol)
        return r.port.quote(symbol) if r else None

    # ------------------------------------------------------------------ #
    def positions(self, broker: Optional[str] = None) -> list[Position]:
        out: list[Position] = []
        for r in self.routes:
            if broker and r.broker != broker:
                continue
            try:
                out.extend(r.port.positions())
            except Exception:
                self.stats["errors"] += 1
        return out

    def reconcile(self, internal: list[Position]) -> list[dict]:
        """Compare engine bookkeeping against broker truth. Non-empty == alert."""
        diffs: list[dict] = []
        # Match on (symbol, ticket), not on the broker *label*. The engine labels a
        # routed position with the route name ("deriv") while a simulated port
        # reports its own ("sim"); keying on the label produced a discrepancy on
        # every paper position, which is how desks learn to ignore reconciliation.
        broker_pos = {(p.symbol, p.id): p for p in self.positions()}
        internal_keys = {(p.symbol, p.id) for p in internal}
        for p in internal:
            other = broker_pos.get((p.symbol, p.id))
            if other is None:
                diffs.append({"type": "missing_at_broker", "symbol": p.symbol,
                              "id": p.id, "lots": p.lots})
            elif abs(float(other.lots) - float(p.lots)) > 1e-6:
                diffs.append({"type": "lots_mismatch", "symbol": p.symbol, "id": p.id,
                              "lots": p.lots, "broker_lots": other.lots})
        for key, p in broker_pos.items():
            if key not in internal_keys:
                diffs.append({"type": "unknown_at_engine", "symbol": p.symbol,
                              "id": p.id, "lots": p.lots})
        if diffs:
            self._event("reconcile", f"{len(diffs)} discrepancy(ies)")
        return diffs

    def kill(self, flatten: bool = False) -> list[Ack]:
        self.killed = True
        acks: list[Ack] = []
        if flatten:
            for p in self.positions():
                acks.append(self.close(p, "kill_switch"))
        return acks

    def rearm(self) -> None:
        self.killed = False

    # ------------------------------------------------------------------ #
    def _record(self, order: Order, ack: Optional[Ack], route: Optional[Route] = None) -> None:
        if ack is None:
            return
        self._acks[order.client_id] = ack
        if self.store is not None:
            try:
                self.store.record_order(order, ack)
            except Exception:
                pass
        status = "FILL" if ack.ok else "REJECT"
        self._event("order", f"{status} {order.side.value} {order.lots} {order.symbol} "
                             f"[{order.strategy}] {ack.reason or ''}".strip())

    def _event(self, kind: str, message: str) -> None:
        if self.store is not None:
            try:
                self.store.record_event(kind, message)
            except Exception:
                pass

    def snapshot(self) -> dict:
        return {
            "killed": self.killed,
            "stats": dict(self.stats),
            "routes": [{"broker": r.broker, "account": r.account, "enabled": r.enabled,
                        "symbols": r.symbols, "label": r.label or r.broker} for r in self.routes],
        }
