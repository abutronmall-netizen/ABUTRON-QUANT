"""The single trading decision engine.

One loop, one decision path, one set of objects:

    bars -> indicators -> regime -> agent votes -> conviction
         -> portfolio arbitration -> risk sizing -> prop-firm guard
         -> broker router -> fill -> position -> exit -> journal -> reflection

Backtest, paper and live all execute this class. There is no "research strategy"
and "production strategy" split - the exact defect (P0-1) that left APEX 1.1.0
staged and unconnected while an older scanner ran production.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .agents import MarketContext
from .data.feed import BarAggregator
from .execution.pending import PendingBook, PendingOrder
from .execution.router import BrokerRouter
from .indicators import IndicatorSet
from .models import (
    Ack,
    Bar,
    Candidate,
    Decision,
    ExitReason,
    Order,
    Position,
    Quote,
    Side,
    TradeRecord,
    new_client_id,
)
from .portfolio import PortfolioManager, ReflectionMemory
from .prop import PropGuard
from .regime import detect
from .risk import RiskConfig, RiskEngine, session_day
from .store import Store
from .strategy import StrategyProfile, build_candidate, manage_position
from .watchdog import clear_flatten_request, flatten_requested

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}


@dataclass
class EngineConfig:
    entry_tf: str = "M5"
    context_tfs: list[str] = field(default_factory=lambda: ["M15", "H1"])
    warmup_bars: int = 60
    max_bars_hold: int = 12
    record_all_signals: bool = True
    flatten_at_eod: bool = False
    reconcile_every: int = 200
    eod_hour_utc: int = 21
    adopt_on_boot: bool = True
    ai_review: bool = False          # optional local LLM review of sized entries
    ai_mode: str = "advisory"        # advisory (log only) | veto (may block)
    scale_out_r: float = 0.0         # take this fraction off at +R (0 disables)
    scale_out_frac: float = 0.5      # fraction of the position to take off
    equity_every: int = 1
    correlation_groups: dict[str, str] = field(default_factory=lambda: {
        "R_10": "vol10", "1HZ10V": "vol10",
        "R_25": "vol25", "1HZ25V": "vol25",
        "R_50": "vol50", "1HZ50V": "vol50",
        "R_75": "vol75", "1HZ75V": "vol75",
        "R_100": "vol100", "1HZ100V": "vol100",
        "BOOM300N": "boom", "BOOM500": "boom", "BOOM1000": "boom",
        "CRASH300N": "crash", "CRASH500": "crash", "CRASH1000": "crash",
        "EURUSD": "eur", "GBPUSD": "gbp", "EURJPY": "eur",
        "XAUUSD": "metals", "XAGUSD": "metals",
        "US30": "us_index", "NAS100": "us_index", "SPX500": "us_index",
        "GER40": "eu_index", "UK100": "eu_index",
    })


class SymbolState:
    __slots__ = ("symbol", "broker", "account", "entry", "context", "aggs",
                 "bar_count", "last_quote", "last_candidate", "last_status", "last_reason")

    def __init__(self, symbol: str, broker: str, account: str, cfg: EngineConfig):
        self.symbol = symbol
        self.broker = broker
        self.account = account
        self.entry = IndicatorSet()
        self.context = {tf: IndicatorSet() for tf in cfg.context_tfs}
        self.aggs = {tf: BarAggregator(TF_SECONDS[tf]) for tf in cfg.context_tfs}
        self.bar_count = 0
        self.last_quote: Optional[Quote] = None
        self.last_candidate: Optional[Candidate] = None
        self.last_status = "INIT"
        self.last_reason = ""


class TradingEngine:
    def __init__(self, router: BrokerRouter, symbols_by_broker: dict[str, list[str]],
                 cfg: Optional[EngineConfig] = None,
                 strategy: Optional[StrategyProfile] = None,
                 risk: Optional[RiskConfig] = None,
                 prop: Optional[PropGuard] = None,
                 store: Optional[Store] = None,
                 memory: Optional[ReflectionMemory] = None,
                 now: Optional[float] = None,
                 on_event: Optional[Callable[[str, str], None]] = None,
                 reviewer=None):
        self.router = router
        self.cfg = cfg or EngineConfig()
        self.strategy = strategy or StrategyProfile()
        self.store = store
        self.memory = memory or ReflectionMemory()
        self.pm = PortfolioManager()
        self.risk = risk or RiskConfig()
        self.prop = prop
        self.reviewer = reviewer
        # one day boundary for risk and prop, taken from the challenge rules
        if self.prop is not None:
            self.risk.daily_reset_hour_utc = self.prop.p.daily_reset_hour_utc
        self.on_event = on_event

        initial = router.account(router.routes[0] if router.routes else None).equity \
            if router.routes else 10_000.0
        self.risk_engine = RiskEngine(self.risk, initial_equity=initial, now=now or time.time())

        self.states: dict[str, SymbolState] = {}
        for broker, syms in symbols_by_broker.items():
            route = next((r for r in router.routes if r.broker == broker), None)
            acct = route.account if route else "unknown"
            for s in syms:
                self.states[s] = SymbolState(s, broker, acct, self.cfg)

        self.positions: dict[str, Position] = {}
        self.trades: list[TradeRecord] = []
        self.pending_signal_log: list[dict] = []
        self.rejections: dict[str, int] = {}
        self.candidates_seen = 0
        self.orders_sent = 0
        self.orders_filled = 0
        self.cycling = False
        self.paused = False
        self._last_equity_ts = 0.0
        self._bars_processed = 0
        self._started = now or time.time()
        self._flattened_today: Optional[int] = None
        self._last_diffs: list[dict] = []
        self._last_watchdog_check = 0.0
        self.pending = PendingBook()
        self._last_pending_poll = 0.0

    # ------------------------------------------------------------------ #
    # events
    # ------------------------------------------------------------------ #
    def _event(self, kind: str, msg: str) -> None:
        if self.store is not None:
            try:
                self.store.record_event(kind, msg)
            except Exception:
                pass
        if self.on_event:
            try:
                self.on_event(kind, msg)
            except Exception:
                pass

    def _reject(self, code: str, detail: str = "", cand: Optional[Candidate] = None) -> None:
        self.rejections[code] = self.rejections.get(code, 0) + 1
        if self.store is not None and self.cfg.record_all_signals:
            self.store.record_signal(cand, code, detail, {"code": code})

    # ------------------------------------------------------------------ #
    # market data callbacks
    # ------------------------------------------------------------------ #
    def set_quote(self, quote: Quote) -> None:
        """Update the decision price without triggering position management.

        Backtests call this immediately before `on_bar` so an entry is priced at
        the same close it will fill against. Without it the engine decides on a
        stale quote and every backtest silently buys after the move has happened.
        """
        st = self.states.get(quote.symbol)
        if st is not None:
            st.last_quote = quote

    def on_quote(self, quote: Quote) -> None:
        st = self.states.get(quote.symbol)
        if st is None:
            return
        st.last_quote = quote
        self._manage_open_positions(quote)

    def on_bar(self, bar: Bar) -> None:
        st = self.states.get(bar.symbol)
        if st is None:
            return
        self._bars_processed += 1
        st.bar_count += 1

        # higher-timeframe aggregation (M5 -> M15 -> H1), same objects as live
        for tf, agg in st.aggs.items():
            closed = agg.update(bar)
            if closed is not None:
                st.context[tf].update(closed)
        st.entry.update(bar)

        # synthetic quote at bar close for brokers without a tick feed
        if st.last_quote is None or st.last_quote.ts < bar.ts:
            spread = self.router.spec(bar.symbol, st.broker).typical_spread
            mid = bar.close
            st.last_quote = Quote(bar.symbol, bar.ts + TF_SECONDS[self.cfg.entry_tf],
                                  mid - spread / 2, mid + spread / 2)

        if self.paused:
            return
        self._mark_equity(bar.ts)
        if self.cfg.flatten_at_eod and self._is_eod(bar.ts):
            self._flatten("eod_flatten", bar.ts)

        if st.bar_count < self.cfg.warmup_bars or not st.entry.ready():
            st.last_status = "WARMUP"
            return

        self._evaluate_symbol(st, bar)

        if self.cfg.reconcile_every and self._bars_processed % self.cfg.reconcile_every == 0:
            self.reconcile()

    def _is_eod(self, ts: float) -> bool:
        day = session_day(ts, self.risk.daily_reset_hour_utc)
        hour = int((ts % 86400) // 3600)
        return hour >= self.cfg.eod_hour_utc and self._flattened_today != day

    # ------------------------------------------------------------------ #
    def _evaluate_symbol(self, st: SymbolState, bar: Bar) -> None:
        ctx = MarketContext(
            symbol=st.symbol, quote=st.last_quote,
            bars={"M5": st.entry, **{tf: st.context[tf] for tf in self.cfg.context_tfs}},
            regime=detect(st.entry, st.context),
            entry_tf=self.cfg.entry_tf, now=bar.ts,
        )
        spec = self.router.spec(st.symbol, st.broker)
        weights = self.memory.weights_for(ctx.regime.label.value)
        cand, status, diag = build_candidate(ctx, spec, self.strategy, weights, self.memory, self.pm)

        st.last_status = status
        st.last_candidate = cand
        st.last_reason = diag.get("threshold", "")

        if cand is None:
            if self.store is not None and self.cfg.record_all_signals and status != "WARMUP":
                self.store.record_signal(None, status, diag.get("reason", status), diag,
                                         ts=bar.ts, symbol=st.symbol)
            self.rejections[status] = self.rejections.get(status, 0) + 1
            return

        self.candidates_seen += 1
        arb = self.pm.arbitrate([cand], list(self.positions.values()),
                                self.cfg.correlation_groups)
        if not arb.accepted:
            reason = arb.rejected[0][1] if arb.rejected else "arbitration rejected"
            if self.store is not None and self.cfg.record_all_signals:
                self.store.record_signal(cand, "ARBITRATION", reason, diag, ts=bar.ts)
            self.rejections["ARBITRATION"] = self.rejections.get("ARBITRATION", 0) + 1
            return

        self._try_enter(cand, spec, bar.ts, diag)

    # ------------------------------------------------------------------ #
    def _try_enter(self, cand: Candidate, spec, ts: float, diag: dict) -> None:
        route = self.router.route_for(cand.symbol)
        if route is None:
            self._reject("NO_ROUTE", f"no broker route for {cand.symbol}", cand)
            return
        acct = route.port.account()

        risk_pct = self.risk.risk_per_trade_pct
        if self.prop is not None:
            risk_pct = self.prop.pacing(ts)["recommended_risk_pct"]

        decision = self.risk_engine.evaluate(cand, spec, acct, list(self.positions.values()),
                                             ts, risk_pct=risk_pct,
                                             groups=self.cfg.correlation_groups)
        if not decision.allowed:
            if self.store is not None and self.cfg.record_all_signals:
                self.store.record_signal(cand, decision.code, decision.reason, decision.detail, ts=ts)
            return

        if self.prop is not None:
            open_risk = sum(float(p.meta.get("risk_money") or 0.0)
                            for p in self.positions.values())
            notional_per_lot = max(spec.contract_size, 1.0) * cand.price
            decision = self.prop.check(cand, decision, ts, open_risk_money=open_risk,
                                       notional_per_lot=notional_per_lot)
            if not decision.allowed:
                if self.store is not None and self.cfg.record_all_signals:
                    self.store.record_signal(cand, decision.code, decision.reason, decision.detail, ts=ts)
                return

        lots = decision.lots
        if lots <= 0:
            self._reject("ZERO_LOTS", "prop/risk produced zero size", cand)
            return

        if self.reviewer is not None and self.cfg.ai_review:
            allowed, note = self._ai_review(cand, lots, decision)
            if not allowed:
                if self.store is not None and self.cfg.record_all_signals:
                    self.store.record_signal(cand, "AI_VETO", "reviewer veto", note, ts=ts)
                self._reject("AI_VETO", str(note.get("risk_note", "")), cand)
                return

        # The price the order is expected to fill at: the touch on the traded
        # side, not the mid. Deriv's adapter refuses an order without a price at
        # all, and without this the journal cannot answer "prices agree".
        st = self.states.get(cand.symbol)
        q = st.last_quote if st is not None else None
        ref_price = (q.ask if cand.side is Side.BUY else q.bid) if q is not None else cand.price
        order = Order(
            symbol=cand.symbol, side=cand.side, lots=lots, broker=route.broker,
            account=route.account, strategy=self.strategy.name, price=float(ref_price),
            sl=cand.sl, tp=cand.tp,
            ttl_s=cand.ttl_s, tag=f"conv={cand.conviction:.2f}",
            meta={"conviction": cand.conviction, "regime": cand.regime,
                  "decision_mid": cand.price},
        )
        ack = self.router.submit(order)
        self.orders_sent += 1
        if not ack.ok:
            self._reject("EXEC_REJECT", ack.reason, cand)
            if self.store is not None:
                self.store.record_signal(cand, "EXEC_REJECT", ack.reason, {"ack": ack.reason}, ts=ts)
            return

        if getattr(ack, "state", "filled") == "pending" or (ack.lots or 0.0) <= 0.0:
            self._track_pending(order, ack, cand, decision, ts)
            self._reject("PENDING_FILL", "order accepted but not filled", cand)
            return

        self.orders_filled += 1
        fill_lots = ack.lots or lots
        risk_money = decision.detail.get("risk_money") or abs(cand.price - cand.sl) * lots * spec.value_per_point_per_lot
        if fill_lots + 1e-9 < lots:
            # partial fill: the position carries less risk than the plan
            risk_money = risk_money * fill_lots / lots
            self._event("order", f"partial fill {fill_lots}/{lots} {cand.symbol}")
        pos = Position(
            id=ack.order_id or new_client_id("pos"), symbol=cand.symbol, side=cand.side,
            lots=fill_lots, entry=ack.filled_price or cand.price,
            sl=cand.sl, tp=cand.tp, opened_ts=ts, broker=route.broker, account=route.account,
            strategy=self.strategy.name, contract_id=ack.order_id or "",
            commission=ack.commission,
            meta={"risk_money": risk_money, "conviction": cand.conviction, "regime": cand.regime,
                  "votes": [{"agent": v.agent, "score": round(v.score, 3)} for v in cand.votes],
                  "atr": cand.meta.get("atr"), "entry_bar": self.states[cand.symbol].bar_count,
                  "entry_bar_ts": ts,
                  "sl_initial": cand.sl, "rr": cand.rr, "client_id": order.client_id},
        )
        self.positions[pos.id] = pos
        if self.prop is not None:
            self.prop.on_fill(ts)
        if self.store is not None:
            self.store.record_signal(cand, "ENTERED", "filled",
                                     {**diag, "lots": lots, "fill": ack.filled_price,
                                      "risk_money": round(risk_money, 2)}, ts=ts)
        self._event("entry", f"{cand.side.value} {lots} {cand.symbol} @ {ack.filled_price:.2f} "
                             f"SL {cand.sl:.2f} TP {cand.tp:.2f} conv {cand.conviction:.2f}")

    # ------------------------------------------------------------------ #
    def _manage_open_positions(self, quote: Quote) -> None:
        for pos in list(self.positions.values()):
            if pos.symbol != quote.symbol:
                continue
            st = self.states.get(pos.symbol)
            spec = self.router.spec(pos.symbol, pos.broker)
            price = quote.bid if pos.side is Side.BUY else quote.ask
            if price <= 0:
                continue
            # excursion tracking
            favour = (price - pos.entry) * pos.side.sign
            pos.max_favorable = max(pos.max_favorable, favour)
            pos.max_adverse = min(pos.max_adverse, favour)

            atr = st.entry.atr_v if st else 0.0
            bars_held = (st.bar_count - pos.meta.get("entry_bar", 0)) if st else 0
            action = manage_position(pos, price, atr, bars_held, self.strategy, spec)

            if (self.cfg.scale_out_r > 0 and not pos.meta.get("scaled_out")
                    and action.exit_reason is None):
                init_risk = abs(pos.entry - float(pos.meta.get("sl_initial") or pos.sl or pos.entry))
                r_now = ((price - pos.entry) * pos.side.sign / init_risk) if init_risk else 0.0
                if r_now >= self.cfg.scale_out_r:
                    part = round(pos.lots * min(max(self.cfg.scale_out_frac, 0.05), 0.95), 8)
                    min_lot = spec.min_lot if spec else 0.0
                    if part >= min_lot and pos.lots - part >= min_lot:
                        self.close_position(pos, ExitReason.TAKE_PROFIT, price,
                                            f"scale-out at {r_now:+.2f}R", volume=part)
                        continue

            if action.exit_reason is not None:
                reason = action.exit_reason
                if reason is ExitReason.STOP_LOSS:
                    init = pos.meta.get("sl_initial")
                    moved = init is not None and pos.sl is not None and abs(pos.sl - init) > 1e-9
                    profitable = (price - pos.entry) * pos.side.sign > 0
                    if moved and profitable:
                        reason = ExitReason.TRAILING
                self.close_position(pos, reason, price, action.detail)
            elif action.new_sl is not None and action.new_sl != pos.sl:
                self._update_stop(pos, action.new_sl)

    # ------------------------------------------------------------------ #
    # pending orders: an accepted order is live risk until the venue says no
    # ------------------------------------------------------------------ #
    def _track_pending(self, order: Order, ack: Ack, cand: Candidate,
                       decision: Decision, ts: float) -> None:
        risk_money = float(decision.detail.get("risk_money") or
                           abs(cand.price - cand.sl) * (order.lots or 0.0) *
                           self.router.spec(cand.symbol, order.broker).value_per_point_per_lot)
        po = PendingOrder(
            client_id=order.client_id, order_id=ack.order_id, broker=order.broker,
            account=order.account, symbol=order.symbol, side=cand.side.value,
            lots=order.lots, sl=cand.sl, tp=cand.tp, entry_hint=cand.price,
            created_ts=ts, expires_ts=ts + max(60.0, order.ttl_s), risk_money=risk_money,
            strategy=self.strategy.name,
            meta={"conviction": cand.conviction, "regime": cand.regime,
                  "votes": [{"agent": v.agent, "score": round(v.score, 3)} for v in cand.votes],
                  "atr": cand.meta.get("atr"), "rr": cand.rr, "client_id": order.client_id},
        )
        self.pending.add(po)
        self._event("order", f"pending {cand.side.value} {order.lots} {cand.symbol} "
                             f"ref {ack.order_id or order.client_id} (tracked)")

    def poll_pending(self, now: Optional[float] = None, force: bool = False) -> dict:
        """Resolve tracked pending orders. Idempotent and safe to call often."""
        now = now if now is not None else time.time()
        if not force and now - self._last_pending_poll < 5:
            return {"filled": 0, "cancelled": 0, "expired": 0, "checked": 0}
        self._last_pending_poll = now
        out = {"filled": 0, "cancelled": 0, "expired": 0, "checked": 0}
        for po in self.pending.items():
            out["checked"] += 1
            po.checks += 1
            status = self.router.order_status(po.broker, po.account, po.order_id) or {}
            state = str(status.get("state") or "unknown")
            if state == "filled":
                self._materialise_pending(po, status, now)
                out["filled"] += 1
                continue
            if state in ("cancelled", "rejected"):
                self.pending.remove(po.client_id)
                self._reject("PENDING_CANCELLED", f"order {po.order_id} {state}")
                self._event("order", f"pending {po.symbol} {state} at venue")
                out["cancelled"] += 1
                continue
            if po.expired(now):
                self.router.cancel(po.broker, po.account, po.order_id)
                self.pending.remove(po.client_id)
                self._reject("PENDING_EXPIRED", f"order {po.order_id} not filled in time")
                self._event("order", f"pending {po.symbol} expired, cancel requested")
                out["expired"] += 1
        return out

    def _materialise_pending(self, po: PendingOrder, status: dict, now: float) -> None:
        lots = float(status.get("filled_lots") or po.lots)
        price = float(status.get("filled_price") or 0.0) or po.entry_hint or self._mark_price_guess(po.symbol)
        risk = po.risk_money * (lots / po.lots) if po.lots else po.risk_money
        pos = Position(
            id=str(status.get("position_id") or po.order_id or po.client_id),
            symbol=po.symbol, side=Side(po.side), lots=lots, entry=price,
            sl=po.sl, tp=po.tp, opened_ts=now, broker=po.broker, account=po.account,
            strategy=po.strategy, contract_id=str(po.order_id),
            commission=float(status.get("commission") or 0.0),
            meta={**po.meta, "risk_money": risk, "sl_initial": po.sl,
                  "entry_bar_ts": now, "late_fill": True},
        )
        self.positions[pos.id] = pos
        self.pending.remove(po.client_id)
        self.orders_filled += 1
        if self.prop is not None:
            self.prop.on_fill(now)
        self._event("entry", f"LATE FILL {po.side} {lots} {po.symbol} @ {price:.2f} "
                             f"(order {po.order_id})")
        if self.store is not None:
            self.store.record_event("entry", f"late fill {po.symbol} {lots} @ {price}")

    def _mark_price_guess(self, symbol: str) -> float:
        q = self.router.quote(symbol)
        if q is not None:
            return q.mid
        st = self.states.get(symbol)
        return st.last_quote.mid if st and st.last_quote else 0.0

    def _ai_review(self, cand: Candidate, lots: float, decision: Decision) -> tuple[bool, dict]:
        """Optional model review of an already-sized trade.

        It sees only what the engine has already decided, so it can reduce risk
        but never create it. Any failure fails open: a dead model must not be
        able to stop a desk.
        """
        payload = {
            "symbol": cand.symbol, "side": cand.side.value, "lots": lots,
            "price": cand.price, "sl": cand.sl, "tp": cand.tp, "rr": round(cand.rr, 2),
            "conviction": round(cand.conviction, 3), "regime": cand.regime,
            "risk_money": decision.detail.get("risk_money"),
            "votes": [{"agent": v.agent, "score": round(v.score, 2)} for v in cand.votes],
        }
        try:
            allowed, note = self.reviewer.gate(payload)
        except Exception as exc:                      # never block on the reviewer
            return True, {"risk_note": f"reviewer error ({type(exc).__name__})"}
        if not allowed and self.cfg.ai_mode != "veto":
            # Defensive: advisory means advisory even if the reviewer object
            # disagrees. A component that can only shrink risk must not be able
            # to block risk by mistake.
            self._event("ai", f"advisory objection {cand.symbol} "
                              f"({note.get('risk_note', '')}) - entry allowed")
            return True, note
        if not allowed:
            self._event("risk", f"reviewer veto {cand.symbol} "
                                f"({note.get('risk_note', '')})")
        elif note.get("risk_note"):
            self._event("ai", f"{cand.symbol} review: {note['risk_note']}")
        return bool(allowed), note or {}

    def _update_stop(self, pos: Position, new_sl: float) -> bool:
        """Propagate a tightened stop to the broker, or enforce it locally.

        Some venues (Deriv multiplier contracts) cannot modify an open contract.
        The engine still has to honour the tightened stop, so it tracks it
        locally - and says so, because a local-only stop dies with the process.
        """
        ack = self.router.modify(pos, new_sl, pos.tp)
        if ack.ok:
            pos.sl = new_sl
            return True
        pos.sl = new_sl
        pos.meta["local_stop"] = True
        if not pos.meta.get("local_stop_warned"):
            pos.meta["local_stop_warned"] = True
            self._event("warn", f"{pos.symbol}: broker rejected stop update "
                                f"({ack.reason}); stop enforced locally only")
        return False

    def close_position(self, pos: Position, reason: ExitReason, price: float,
                       detail: str = "", volume: Optional[float] = None) -> Optional[TradeRecord]:
        """Close all of a position, or `volume` lots of it (a scale-out).

        A partial close realises P&L and shrinks the position; it does not end
        the trade's life, so the position stays open with proportionally less
        risk and the remainder is closed normally later.
        """
        lots_before = float(pos.lots)
        partial = volume is not None and 0 < float(volume) < lots_before - 1e-9
        ack = self.router.close(pos, reason.value, price,
                               volume=float(volume) if partial else None)
        if not ack.ok:
            self._event("error", f"close failed for {pos.symbol}: {ack.reason}")
            return None
        spec = self.router.spec(pos.symbol, pos.broker)
        exit_px = ack.filled_price or price
        lots_closed = min(lots_before, float(ack.lots or lots_before))
        pnl = (exit_px - pos.entry) * pos.side.sign * lots_closed * spec.value_per_point_per_lot
        commission = (pos.commission or 0.0) + (ack.commission or 0.0)
        pnl -= (ack.commission or 0.0)
        risk_money = pos.meta.get("risk_money") or 0.0
        r_mult = pnl / risk_money if risk_money else 0.0
        trade = TradeRecord(
            trade_id=new_client_id("tr"), symbol=pos.symbol, side=pos.side, lots=lots_closed,
            entry=pos.entry, exit=exit_px, opened_ts=pos.opened_ts, closed_ts=ack.ts,
            pnl=round(pnl, 2), commission=round(commission, 4), strategy=pos.strategy,
            broker=pos.broker, account=pos.account, exit_reason=reason,
            conviction=pos.meta.get("conviction", 0.0), regime=pos.meta.get("regime", ""),
            r_multiple=round(r_mult, 3),
            mfe=round(pos.max_favorable, 5), mae=round(pos.max_adverse, 5),
            meta={"detail": detail, "votes": pos.meta.get("votes", []),
                  "entry": pos.entry, "sl_initial": pos.meta.get("sl_initial"),
                  "rr": pos.meta.get("rr"), "partial": partial,
                  "open_position": pos.id},
        )
        ended = lots_closed >= lots_before - 1e-9
        if ended:
            self.positions.pop(pos.id, None)
        else:
            pos.lots = round(lots_before - lots_closed, 8)
            pos.meta["scaled_out"] = True
            pos.meta["realised_pnl"] = round(
                float(pos.meta.get("realised_pnl") or 0.0) + pnl, 4)
            if risk_money:
                pos.meta["risk_money"] = risk_money * pos.lots / lots_before
        self.trades.append(trade)
        self.risk_engine.on_trade_closed(trade.pnl, ack.ts)
        if self.prop is not None:
            self.prop.on_trade_closed(trade, ack.ts)
        if self.memory is not None and ended:
            self.memory.update(trade)
        if self.store is not None:
            self.store.record_trade(trade)
        if partial:
            self._event("exit", f"{trade.symbol} SCALE-OUT {lots_closed:g}/{lots_before:g} lots "
                                f"pnl {trade.pnl:+.2f} ({trade.r_multiple:+.2f}R), "
                                f"{pos.lots:g} left")
        else:
            self._event("exit", f"{trade.symbol} {reason.value} pnl {trade.pnl:+.2f} "
                                f"({trade.r_multiple:+.2f}R)")
        return trade

    def on_bar_brackets(self, bar: Bar) -> None:
        """Intrabar stop/target detection (backtest accuracy).

        Stops are checked before targets: a bar that touches both is assumed to
        have hit the stop first, so results are never optimistic.
        """
        for pos in list(self.positions.values()):
            if pos.symbol != bar.symbol or pos.sl is None and pos.tp is None:
                continue
            # A position cannot be stopped out by price action that happened
            # before it existed. Skipping the entry bar removes a phantom-stop
            # bias that silently destroys every bar-close backtest.
            if pos.meta.get("entry_bar_ts") == bar.ts:
                continue
            sl_hit = pos.sl is not None and (
                (pos.side is Side.BUY and bar.low <= pos.sl) or
                (pos.side is Side.SELL and bar.high >= pos.sl))
            tp_hit = pos.tp is not None and (
                (pos.side is Side.BUY and bar.high >= pos.tp) or
                (pos.side is Side.SELL and bar.low <= pos.tp))
            if sl_hit:
                self.close_position(pos, ExitReason.STOP_LOSS, pos.sl, "intrabar stop")
            elif tp_hit:
                self.close_position(pos, ExitReason.TAKE_PROFIT, pos.tp, "intrabar target")

    def _flatten(self, reason: str, ts: float) -> None:
        for pos in list(self.positions.values()):
            q = self.states[pos.symbol].last_quote if pos.symbol in self.states else None
            price = (q.bid if pos.side is Side.BUY else q.ask) if q else pos.entry
            self.close_position(pos, ExitReason.EOD_FLAT if reason == "eod_flatten"
                                else ExitReason.PROP_FLAT, price, reason)
        self._flattened_today = session_day(ts, self.risk.daily_reset_hour_utc)

    def flatten_all(self, reason: str = "manual") -> None:
        for pos in list(self.positions.values()):
            q = self.router.quote(pos.symbol)
            price = (q.bid if pos.side is Side.BUY else q.ask) if q else pos.entry
            self.close_position(pos, ExitReason.MANUAL, price, reason)

    # ------------------------------------------------------------------ #
    def _mark_equity(self, ts: float) -> None:
        if ts - self._last_equity_ts < 1 and self._last_equity_ts:
            return
        self._last_equity_ts = ts
        primary = self.router.routes[0] if self.router.routes else None
        for r in self.router.routes:
            try:
                acct = r.port.account()
            except Exception:
                continue
            if self.store is not None:
                self.store.record_equity(ts, acct.broker, acct.account, acct.equity, acct.balance)
            # The prop challenge and the risk governor belong to ONE account.
            # Averaging several broker accounts together (the earlier behaviour)
            # produced nonsense floors and false breaches.
            if primary is not None and r is primary:
                self.risk_engine.on_equity(acct.equity, ts)
                if self.prop is not None:
                    self.prop.on_equity(ts, acct.equity, acct.balance)

        if self.prop is not None and self.positions:
            reason = self.prop.should_flatten(ts, True)
            if reason:
                for pos in list(self.positions.values()):
                    q = self.states[pos.symbol].last_quote if pos.symbol in self.states else None
                    price = (q.bid if pos.side is Side.BUY else q.ask) if q else pos.entry
                    self.close_position(pos, ExitReason.PROP_FLAT, price, reason)

    # ------------------------------------------------------------------ #
    def _mark_price(self, pos: Position) -> float:
        q = self.router.quote(pos.symbol)
        if q is None and pos.symbol in self.states:
            q = self.states[pos.symbol].last_quote
        if q is None:
            return pos.entry
        return q.bid if pos.side is Side.BUY else q.ask

    def cycle_guard(self) -> None:
        """Called by runners: enforces the risk engine's halt on live positions."""
        self._watchdog_check()
        if len(self.pending):
            self.poll_pending()
        if self.risk_engine.state.halted and self.positions:
            self._event("risk", f"halt: {self.risk_engine.state.halt_reason} -> flattening")
            self.flatten_all("risk_halt")
        if self.router.killed and self.positions:
            self.flatten_all("kill_switch")

    # ------------------------------------------------------------------ #
    # boot reconciliation: what the broker holds is the truth
    # ------------------------------------------------------------------ #
    def adopt_positions(self, positions: list[Position], ts: Optional[float] = None) -> list[Position]:
        """Take ownership of positions that exist at the broker but not here.

        After a restart (or a crash) the engine's book is empty while the broker
        still holds live risk. Ignoring that is how a desk ends up with an
        unmanaged position and a duplicated entry on the same symbol.
        """
        adopted: list[Position] = []
        ts = ts if ts is not None else time.time()
        known = {(p.broker, p.symbol, p.contract_id or p.id) for p in self.positions.values()}
        for p in positions:
            key = (p.broker, p.symbol, p.contract_id or p.id)
            if key in known or p.id in self.positions:
                continue
            spec = self.router.spec(p.symbol, p.broker)
            dist = abs(p.entry - p.sl) if p.sl is not None else 0.0
            p.meta.setdefault("risk_money",
                              round(dist * p.lots * spec.value_per_point_per_lot, 2))
            p.meta.setdefault("sl_initial", p.sl)
            p.meta.setdefault("rr", 0.0)
            p.meta["adopted"] = True
            p.meta.setdefault("entry_bar_ts", p.opened_ts)
            p.broker = p.broker or self.states[p.symbol].broker if p.symbol in self.states else p.broker
            self.positions[p.id] = p
            known.add(key)
            adopted.append(p)
            self._event("reconcile",
                        f"adopted {p.side.value} {p.lots} {p.symbol} @ {p.entry:g} "
                        f"sl={p.sl} ref={p.contract_id or p.id}")
        return adopted

    def boot_sync(self, policy: str = "adopt", ts: Optional[float] = None) -> dict:
        """Reconcile on startup: adopt broker positions or flatten everything.

        A watchdog flatten request always wins over adoption: a crash that left
        unprotected positions must end flat, not inherited.
        """
        req = flatten_requested(self.store)
        if req is not None:
            policy = "flatten"
            self._event("control", f"watchdog flatten request honoured at boot "
                                   f"({req.get('reason')})")
        try:
            broker_positions = self.router.positions()
        except Exception as exc:                      # a dead adapter must not block boot
            self._event("error", f"boot reconcile failed: {exc}")
            return {"adopted": [], "flattened": [], "diffs": [], "error": str(exc)}
        adopted: list[Position] = []
        flattened: list[str] = []
        if policy == "flatten":
            for p in broker_positions:
                ack = self.router.close(p, "boot_flatten")
                if ack.ok:
                    flattened.append(p.symbol)
        elif policy == "adopt" and self.cfg.adopt_on_boot:
            adopted = self.adopt_positions(broker_positions, ts)
        diffs = self.reconcile()
        if adopted or flattened or diffs:
            self._event("reconcile",
                        f"boot: {len(adopted)} adopted, {len(flattened)} flattened, "
                        f"{len(diffs)} diff(s)")
        return {"adopted": [p.id for p in adopted], "flattened": flattened, "diffs": diffs}

    # ------------------------------------------------------------------ #
    # durable state
    # ------------------------------------------------------------------ #
    def export_state(self) -> dict:
        return {"risk": self.risk_engine.export_state(),
                "prop": self.prop.export_state() if self.prop is not None else None,
                "positions": [{"id": p.id, "symbol": p.symbol, "side": p.side.value,
                               "lots": p.lots, "entry": p.entry, "sl": p.sl, "tp": p.tp,
                               "opened_ts": p.opened_ts, "broker": p.broker,
                               "account": p.account, "strategy": p.strategy,
                               "contract_id": p.contract_id, "meta": p.meta}
                              for p in self.positions.values()],
                "pending": self.pending.export()}

    def import_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        self.risk_engine.import_state(data.get("risk") or {})
        if self.prop is not None and data.get("prop"):
            self.prop.import_state(data["prop"])
        for row in data.get("positions") or []:
            try:
                pos = Position(id=row["id"], symbol=row["symbol"], side=Side(row["side"]),
                               lots=float(row["lots"]), entry=float(row["entry"]),
                               sl=row.get("sl"), tp=row.get("tp"),
                               opened_ts=float(row.get("opened_ts") or 0.0),
                               broker=row.get("broker", ""), account=row.get("account", ""),
                               strategy=row.get("strategy", self.strategy.name),
                               contract_id=row.get("contract_id", ""),
                               meta=row.get("meta") or {})
            except Exception:
                continue
            self.positions[pos.id] = pos
        try:
            self.pending = PendingBook(data.get("pending") or [])
        except Exception:
            self.pending = PendingBook()
        self._event("info", f"state restored: {len(self.positions)} position(s), "
                            f"{len(self.pending)} pending order(s), "
                            f"halted={self.risk_engine.state.halted}")

    def save_state(self, key: str = "engine") -> bool:
        if self.store is None:
            return False
        try:
            self.store.save_state(key, self.export_state())
            return True
        except Exception:
            return False

    def load_state(self, key: str = "engine") -> bool:
        if self.store is None:
            return False
        data = self.store.load_state(key)
        if not data:
            return False
        self.import_state(data)
        return True

    def _watchdog_check(self, ts: Optional[float] = None) -> None:
        """Honour a flatten request written by the out-of-process watchdog."""
        if self.store is None:
            return
        now = ts if ts is not None else time.time()
        if now - self._last_watchdog_check < 5:
            return
        self._last_watchdog_check = now
        req = flatten_requested(self.store, now)
        if req is None:
            return
        self._event("control", f"watchdog flatten request: {req.get('reason')}")
        if self.positions:
            self.flatten_all("watchdog")
        try:
            self.router.kill(flatten=True)
        except Exception:
            pass
        self.risk_engine.halt("watchdog flatten request", kind="manual")
        clear_flatten_request(self.store)
        self.save_state()

    def reconcile(self) -> list[dict]:
        diffs = self.router.reconcile(list(self.positions.values()))
        self._last_diffs = diffs
        for d in diffs:
            self._event("reconcile", f"{d['type']} {d['symbol']} id={d['id']}")
        return diffs

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        accounts = []
        for r in self.router.routes:
            try:
                accounts.append(asdict(self.router.account(r)))
            except Exception:
                pass
        total_equity = sum(a.get("equity", 0.0) for a in accounts) or 0.0
        unrealised = 0.0
        for p in self.positions.values():
            spec = self.router.spec(p.symbol, p.broker)
            q = self.router.quote(p.symbol) or (self.states[p.symbol].last_quote
                                                if p.symbol in self.states else None)
            if q is not None:
                px = q.bid if p.side is Side.BUY else q.ask
                unrealised += p.unrealised(px, spec.value_per_point_per_lot)

        # The dashboard reads this from another thread while the engine trades.
        # Take copies first: iterating a dict that another thread is mutating
        # raises "dictionary changed size during iteration" and the operator gets
        # a blank page at exactly the wrong moment.
        positions = list(self.positions.values())
        states = list(self.states.values())
        pending = list(self.pending.items())
        heat = self.risk_engine.open_risk_money(positions)
        return {
            "ts": time.time(),
            "runtime_s": round(time.time() - self._started, 1),
            "heat_money": round(heat, 2),
            "heat_pct": round(heat / total_equity * 100.0, 3) if total_equity else 0.0,
            "pending": [{"symbol": p.symbol, "side": p.side, "lots": p.lots,
                         "order_id": p.order_id, "expires_ts": p.expires_ts}
                        for p in pending],
            "accounts": accounts,
            "total_equity": round(total_equity, 2),
            "unrealised": round(unrealised, 2),
            "positions": [{
                "id": p.id, "symbol": p.symbol, "side": p.side.value, "lots": p.lots,
                "entry": p.entry, "sl": p.sl, "tp": p.tp, "broker": p.broker,
                "strategy": p.strategy, "opened_ts": p.opened_ts,
                "unrealised": round(p.unrealised(
                    self._mark_price(p), self.router.spec(p.symbol, p.broker).value_per_point_per_lot), 2),
                "conviction": p.meta.get("conviction"),
                "regime": p.meta.get("regime"),
            } for p in positions],
            "symbols": [{
                "symbol": s.symbol, "broker": s.broker, "status": s.last_status,
                "bars": s.bar_count,
                "price": s.last_quote.mid if s.last_quote else None,
                "regime": detect(s.entry, s.context).as_dict() if s.entry.ready() else None,
                "conviction": s.last_candidate.conviction if s.last_candidate else None,
                "votes": ({v.agent: round(v.score, 3) for v in s.last_candidate.votes}
                          if s.last_candidate else None),
                "trend": {"M5": s.entry.trend_dir,
                          **{tf: ind.trend_dir for tf, ind in s.context.items()}},
                "atr": round(s.entry.atr_v, 5) if s.entry.atr_v else None,
                "rsi": round(s.entry.rsi_v, 1) if s.entry.rsi_v is not None else None,
            } for s in states],
            "reconcile_diffs": len(self._last_diffs),
            "local_stops": sum(1 for p in self.positions.values() if p.meta.get("local_stop")),
            "risk": self.risk_engine.snapshot(list(self.positions.values())),
            "prop": self.prop.status(time.time()) if self.prop is not None else None,
            "router": self.router.snapshot(),
            "rejections": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])[:12]),
            "counters": {"candidates": self.candidates_seen, "orders_sent": self.orders_sent,
                         "orders_filled": self.orders_filled, "bars": self._bars_processed,
                         "trades_closed": len(self.trades)},
            "memory": self.memory.report() if self.memory else {},
            "recent_trades": [t.as_dict() for t in self.trades[-15:]][::-1],
        }
