"""MT5 execution adapter for FP Markets.

Implements the same ExecutionPort as the paper broker, so an order travels the
identical engine -> risk -> prop -> router path whether it lands on a Deriv
synthetic or an FP Markets MT5 ticket. That is the fix for audit P0-3/P0-4.

Safety defaults: `dry_run=True` never calls order_send; it returns a simulated ack
so the full pipeline can be validated against a live demo account before any real
order is transmitted.
"""
from __future__ import annotations

import time
from typing import Optional

from ..data.mt5 import MT5Config, MT5Connection, MT5Error
from ..models import (AccountState, Ack, Order, Position, Quote, Side, SymbolSpec,
                      new_client_id)
from .base import SymbolRegistry


class MT5Broker:
    name = "mt5"

    def __init__(self, account: str, cfg: Optional[MT5Config] = None,
                 registry: Optional[SymbolRegistry] = None,
                 conn: Optional[MT5Connection] = None, dry_run: bool = True):
        self.account_id = account
        self.cfg = cfg or MT5Config()
        self.registry = registry or SymbolRegistry()
        self.conn = conn or MT5Connection(self.cfg)
        self.dry_run = dry_run
        self._connected = False
        self._dry_positions: dict[str, Position] = {}
        self._dry_balance = 0.0
        self._seq = 0

    # ------------------------------------------------------------------ #
    def connect(self) -> bool:
        """Connect for market data if possible; dry_run still simulates orders."""
        if self.conn._mt5 is None and self.dry_run:
            return True
        try:
            self._connected = self.conn.connect()
        except Exception:
            self._connected = False
        return self._connected

    def spec(self, symbol: str) -> SymbolSpec:
        base = self.registry.spec("mt5", symbol)
        if not self.dry_run and self.conn.connected:
            try:
                info = self.conn.ensure_symbol(symbol)
                base.digits = int(info.digits)
                base.pip = float(info.point) * 10
                base.min_stop_distance = float(info.trade_stops_level) * float(info.point)
                base.min_lot = float(info.volume_min)
                base.max_lot = float(info.volume_max)
                base.lot_step = float(info.volume_step)
                base.contract_size = float(info.trade_contract_size)
                base.value_per_point_per_lot = base.contract_size
                base.symbol = info.name
            except MT5Error:
                pass
        return base

    def quote(self, symbol: str) -> Optional[Quote]:
        if not self.conn.connected:
            return None
        try:
            return self.conn.tick(symbol)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    def account(self) -> AccountState:
        if self.unavailable():
            equity = 10_000.0 + sum(
                p.unrealised(p.entry, 1.0) for p in self._dry_positions.values())
            return AccountState(broker="mt5", account=self.account_id, equity=equity,
                                balance=10_000.0, free_margin=equity, ts=time.time(), live=False)
        info = self.conn.account_info()
        return AccountState(
            broker="mt5", account=self.account_id,
            currency=getattr(info, "currency", "USD"),
            balance=float(info.balance), equity=float(info.equity),
            margin_used=float(info.margin), free_margin=float(info.margin_free),
            leverage=float(info.leverage), ts=time.time(), live=not self.dry_run,
        )

    def positions(self) -> list[Position]:
        if self.dry_run or self.unavailable():
            return list(self._dry_positions.values())
        raw = self.conn.mt5.positions_get() or []
        out = []
        for p in raw:
            side = Side.BUY if p.type == 0 else Side.SELL
            out.append(Position(
                id=str(p.ticket), symbol=p.symbol, side=side, lots=float(p.volume),
                entry=float(p.price_open), sl=float(p.sl) or None, tp=float(p.tp) or None,
                opened_ts=float(p.time), broker="mt5", account=self.account_id,
                strategy=(p.comment or "mt5"), contract_id=str(p.ticket),
                meta={"magic": p.magic, "profit": float(p.profit)},
            ))
        return out

    def unavailable(self) -> bool:
        """True when orders must be simulated rather than transmitted."""
        return self.dry_run or not self.conn.connected

    # ------------------------------------------------------------------ #
    def submit(self, order: Order) -> Ack:
        ts = time.time()
        q = self.quote(order.symbol)
        if q is None and not self.unavailable():
            return Ack(ok=False, client_id=order.client_id, ts=ts, reason="no tick",
                       broker="mt5", account=self.account_id, retryable=True)
        price = q.ask if (q and order.side is Side.BUY) else (q.bid if q else order.price or 0.0)

        if self.unavailable():
            self._seq += 1
            pid = f"DRY-{self._seq}"
            pos = Position(id=pid, symbol=order.symbol, side=order.side, lots=order.lots,
                           entry=price, sl=order.sl, tp=order.tp, opened_ts=ts, broker="mt5",
                           account=self.account_id, strategy=order.strategy, contract_id=pid,
                           meta={"dry_run": True})
            self._dry_positions[pid] = pos
            return Ack(ok=True, client_id=order.client_id, ts=ts, order_id=pid,
                       filled_price=price, lots=order.lots, broker="mt5",
                       account=self.account_id, raw={"dry_run": True})

        mt5 = self.conn.mt5
        filling = {"IOC": mt5.ORDER_FILLING_IOC, "FOK": mt5.ORDER_FILLING_FOK,
                   "RETURN": mt5.ORDER_FILLING_RETURN}.get(self.cfg.filling_type, mt5.ORDER_FILLING_IOC)
        sym_info = self.conn.ensure_symbol(order.symbol)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": sym_info.name,
            "volume": float(order.lots),
            "type": mt5.ORDER_TYPE_BUY if order.side is Side.BUY else mt5.ORDER_TYPE_SELL,
            "price": float(price),
            "sl": float(order.sl) if order.sl else 0.0,
            "tp": float(order.tp) if order.tp else 0.0,
            "deviation": self.cfg.deviation,
            "magic": self.cfg.magic,
            "comment": f"AQ:{order.strategy}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": filling,
        }
        result = mt5.order_send(request)
        if result is None:
            return Ack(ok=False, client_id=order.client_id, ts=ts,
                       reason=f"order_send None: {mt5.last_error()}",
                       broker="mt5", account=self.account_id, retryable=True)
        placed = getattr(mt5, "TRADE_RETCODE_PLACED", 10008)
        if result.retcode == placed:
            # Accepted by the server but not dealt yet (e.g. RETURN filling):
            # the engine must not invent a position for an unfilled order.
            return Ack(ok=True, client_id=order.client_id, ts=ts, order_id=str(result.order),
                       filled_price=price, lots=0.0, state="pending",
                       requested_lots=order.lots, broker="mt5", account=self.account_id,
                       raw={"retcode": result.retcode})
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            retryable = result.retcode in (mt5.TRADE_RETCODE_REQUOTE,
                                           mt5.TRADE_RETCODE_PRICE_CHANGED,
                                           mt5.TRADE_RETCODE_TIMEOUT)
            return Ack(ok=False, client_id=order.client_id, ts=ts,
                       reason=f"retcode {result.retcode}: {result.comment}",
                       broker="mt5", account=self.account_id, retryable=retryable,
                       raw={"retcode": result.retcode})
        filled = float(getattr(result, "volume", order.lots) or order.lots)
        partial = filled + 1e-9 < float(order.lots)
        return Ack(ok=True, client_id=order.client_id, ts=ts, order_id=str(result.order),
                   filled_price=float(result.price), lots=filled,
                   state="partial" if partial else "filled", requested_lots=order.lots,
                   broker="mt5", account=self.account_id, raw={"deal": result.deal})

    # ------------------------------------------------------------------ #
    def order_status(self, order_id: str) -> Optional[dict]:
        """Resolve an order the server accepted before dealing (retcode PLACED)."""
        try:
            ticket = int(str(order_id))
        except (TypeError, ValueError):
            return None
        if self.unavailable():
            return None
        mt5 = self.conn.mt5
        # 1) it became a position -> filled
        try:
            rows = mt5.positions_get(ticket=ticket) or []
        except Exception:
            rows = []
        for p in rows:
            return {"state": "filled", "position_id": str(getattr(p, "ticket", ticket)),
                    "filled_lots": float(getattr(p, "volume", 0.0) or 0.0),
                    "filled_price": float(getattr(p, "price_open", 0.0) or 0.0), "ticket": ticket}
        # 2) still an open/pending order -> pending
        try:
            live = mt5.orders_get(ticket=ticket) or []
        except Exception:
            live = []
        if live:
            return {"state": "pending", "ticket": ticket}
        # 3) history verdict
        try:
            hist = mt5.history_orders_get(ticket=ticket) or []
        except Exception:
            hist = []
        for o in hist:
            vol_done = float(getattr(o, "volume_initial", 0.0) or 0.0)
            state = getattr(o, "state", None)
            filled_state = getattr(mt5, "ORDER_STATE_FILLED", 4)
            partial_state = getattr(mt5, "ORDER_STATE_PARTIAL", 3)
            cancelled_state = getattr(mt5, "ORDER_STATE_CANCELED", 2)
            if state == filled_state:
                return {"state": "filled", "filled_lots": vol_done,
                        "filled_price": float(getattr(o, "price_open", 0.0) or 0.0),
                        "ticket": ticket}
            if state == partial_state:
                return {"state": "filled",
                        "filled_lots": float(getattr(o, "volume_current", 0.0) or 0.0),
                        "filled_price": float(getattr(o, "price_open", 0.0) or 0.0),
                        "ticket": ticket, "partial": True}
            if state == cancelled_state:
                return {"state": "cancelled", "ticket": ticket}
        return {"state": "unknown", "ticket": ticket}

    def cancel(self, order_id: str) -> Ack:
        """Remove a still-pending order. Safe to call on anything already gone."""
        ts = time.time()
        client_id = new_client_id("cancel")
        if self.unavailable():
            return Ack(ok=True, client_id=client_id, ts=ts, order_id=str(order_id),
                       broker="mt5", account=self.account_id, raw={"dry_run": True})
        try:
            ticket = int(str(order_id))
        except (TypeError, ValueError):
            return Ack(ok=False, client_id=client_id, ts=ts, reason="bad order id",
                       broker="mt5", account=self.account_id)
        mt5 = self.conn.mt5
        try:
            result = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": ticket})
        except Exception as exc:
            return Ack(ok=False, client_id=client_id, ts=ts, reason=f"cancel error: {exc}",
                       broker="mt5", account=self.account_id, retryable=True)
        if result is None:
            return Ack(ok=False, client_id=client_id, ts=ts, reason="order_send None",
                       broker="mt5", account=self.account_id, retryable=True)
        ok = result.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL) \
            if hasattr(mt5, "TRADE_RETCODE_DONE_PARTIAL") else result.retcode == mt5.TRADE_RETCODE_DONE
        return Ack(ok=ok, client_id=client_id, ts=ts, order_id=str(ticket),
                   reason="" if ok else f"retcode {result.retcode}",
                   broker="mt5", account=self.account_id, raw={"retcode": result.retcode})

    # ------------------------------------------------------------------ #
    def close(self, position: Position, reason: str, price: Optional[float] = None,
              volume: Optional[float] = None) -> Ack:
        ts = time.time()
        lots = position.lots if volume is None else min(float(volume), position.lots)
        if lots <= 0:
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts, reason="zero volume",
                       broker="mt5", account=self.account_id)
        partial = lots < position.lots - 1e-9
        if self.unavailable():
            q = self.quote(position.symbol)
            px = position.entry if q is None else (q.bid if position.side is Side.BUY else q.ask)
            book = self._dry_positions.get(position.id)
            if book is not None and partial:
                book.lots = round(book.lots - lots, 8)
            else:
                self._dry_positions.pop(position.id, None)
            return Ack(ok=True, client_id=new_client_id("close"), ts=ts,
                       order_id=f"CLS-{position.id}", filled_price=px, lots=lots,
                       broker="mt5", account=self.account_id,
                       raw={"dry_run": True, "reason": reason, "partial": partial})
        mt5 = self.conn.mt5
        q = self.conn.tick(position.symbol)
        if q is None and price is None:
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts, reason="no tick",
                       broker="mt5", account=self.account_id, retryable=True)
        if price is None:
            price = q.bid if position.side is Side.BUY else q.ask  # type: ignore[union-attr]
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "position": int(position.contract_id or position.id),
            "symbol": position.symbol,
            "volume": float(lots),
            "type": mt5.ORDER_TYPE_SELL if position.side is Side.BUY else mt5.ORDER_TYPE_BUY,
            "price": float(price),
            "deviation": self.cfg.deviation,
            "magic": self.cfg.magic,
            "comment": f"AQclose:{reason}"[:31],
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return Ack(ok=False, client_id=new_client_id("close"), ts=ts,
                       reason=f"close failed: {getattr(result, 'retcode', 'None')} "
                              f"{getattr(result, 'comment', '')}",
                       broker="mt5", account=self.account_id, retryable=True)
        return Ack(ok=True, client_id=new_client_id("close"), ts=ts,
                   order_id=str(result.order), filled_price=float(result.price),
                   lots=float(result.volume), broker="mt5", account=self.account_id)

    def modify(self, position: Position, sl: Optional[float], tp: Optional[float]) -> Ack:
        ts = time.time()
        if self.unavailable():
            p = self._dry_positions.get(position.id)
            if p:
                p.sl = sl if sl is not None else p.sl
                p.tp = tp if tp is not None else p.tp
            return Ack(ok=True, client_id=new_client_id("mod"), ts=ts, order_id=position.id,
                       broker="mt5", account=self.account_id, raw={"dry_run": True})
        mt5 = self.conn.mt5
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "position": int(position.contract_id or position.id),
            "symbol": position.symbol,
            "sl": float(sl) if sl is not None else 0.0,
            "tp": float(tp) if tp is not None else 0.0,
            "magic": self.cfg.magic,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            return Ack(ok=False, client_id=new_client_id("mod"), ts=ts,
                       reason=f"modify failed: {getattr(result, 'retcode', 'None')}",
                       broker="mt5", account=self.account_id, retryable=False)
        return Ack(ok=True, client_id=new_client_id("mod"), ts=ts, order_id=str(result.order),
                   broker="mt5", account=self.account_id)
