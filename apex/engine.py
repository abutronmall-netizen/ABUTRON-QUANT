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
from .execution.router import BrokerRouter, Route
from .indicators import IndicatorSet
from .models import (AccountState, Bar, Candidate, Decision, ExitReason, Order, Position,
                     Quote, Side, TradeRecord, new_client_id)
from .portfolio import PortfolioManager, ReflectionMemory
from .prop import PropGuard, PropProfile
from .regime import detect
from .risk import RiskConfig, RiskEngine
from .store import Store
from .strategy import StrategyProfile, build_candidate, manage_position

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
                 on_event: Optional[Callable[[str, str], None]] = None):
        self.router = router
        self.cfg = cfg or EngineConfig()
        self.strategy = strategy or StrategyProfile()
        self.store = store
        self.memory = memory or ReflectionMemory()
        self.pm = PortfolioManager()
        self.risk = risk or RiskConfig()
        self.prop = prop
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

    def _is_eod(self, ts: float) -> bool:
        day = int(ts // 86400)
        hour = int((ts % 86400) // 3600)
        return hour >= 21 and self._flattened_today != day

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
                                             ts, risk_pct=risk_pct)
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

        order = Order(
            symbol=cand.symbol, side=cand.side, lots=lots, broker=route.broker,
            account=route.account, strategy=self.strategy.name, sl=cand.sl, tp=cand.tp,
            ttl_s=cand.ttl_s, tag=f"conv={cand.conviction:.2f}",
            meta={"conviction": cand.conviction, "regime": cand.regime},
        )
        ack = self.router.submit(order)
        self.orders_sent += 1
        if not ack.ok:
            self._reject("EXEC_REJECT", ack.reason, cand)
            if self.store is not None:
                self.store.record_signal(cand, "EXEC_REJECT", ack.reason, {"ack": ack.reason}, ts=ts)
            return

        self.orders_filled += 1
        risk_money = decision.detail.get("risk_money") or abs(cand.price - cand.sl) * lots * spec.value_per_point_per_lot
        pos = Position(
            id=ack.order_id or new_client_id("pos"), symbol=cand.symbol, side=cand.side,
            lots=ack.lots or lots, entry=ack.filled_price or cand.price,
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
                ack = self.router.modify(pos, action.new_sl, pos.tp)
                if ack.ok:
                    pos.sl = action.new_sl

    def close_position(self, pos: Position, reason: ExitReason, price: float,
                       detail: str = "") -> Optional[TradeRecord]:
        ack = self.router.close(pos, reason.value, price)
        if not ack.ok:
            self._event("error", f"close failed for {pos.symbol}: {ack.reason}")
            return None
        spec = self.router.spec(pos.symbol, pos.broker)
        exit_px = ack.filled_price or price
        pnl = (exit_px - pos.entry) * pos.side.sign * pos.lots * spec.value_per_point_per_lot
        commission = (pos.commission or 0.0) + (ack.commission or 0.0)
        pnl -= (ack.commission or 0.0)
        risk_money = pos.meta.get("risk_money") or 0.0
        r_mult = pnl / risk_money if risk_money else 0.0
        trade = TradeRecord(
            trade_id=new_client_id("tr"), symbol=pos.symbol, side=pos.side, lots=pos.lots,
            entry=pos.entry, exit=exit_px, opened_ts=pos.opened_ts, closed_ts=ack.ts,
            pnl=round(pnl, 2), commission=round(commission, 4), strategy=pos.strategy,
            broker=pos.broker, account=pos.account, exit_reason=reason,
            conviction=pos.meta.get("conviction", 0.0), regime=pos.meta.get("regime", ""),
            r_multiple=round(r_mult, 3),
            mfe=round(pos.max_favorable, 5), mae=round(pos.max_adverse, 5),
            meta={"detail": detail, "votes": pos.meta.get("votes", []),
                  "entry": pos.entry, "sl_initial": pos.meta.get("sl_initial"),
                  "rr": pos.meta.get("rr")},
        )
        self.positions.pop(pos.id, None)
        self.trades.append(trade)
        self.risk_engine.on_trade_closed(trade.pnl, ack.ts)
        if self.prop is not None:
            self.prop.on_trade_closed(trade, ack.ts)
        if self.memory is not None:
            self.memory.update(trade)
        if self.store is not None:
            self.store.record_trade(trade)
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
        self._flattened_today = int(ts // 86400)

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
        if self.risk_engine.state.halted and self.positions:
            self._event("risk", f"halt: {self.risk_engine.state.halt_reason} -> flattening")
            self.flatten_all("risk_halt")
        if self.router.killed and self.positions:
            self.flatten_all("kill_switch")

    def reconcile(self) -> list[dict]:
        diffs = self.router.reconcile(list(self.positions.values()))
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

        return {
            "ts": time.time(),
            "runtime_s": round(time.time() - self._started, 1),
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
            } for p in self.positions.values()],
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
            } for s in self.states.values()],
            "risk": self.risk_engine.snapshot(),
            "prop": self.prop.status(time.time()) if self.prop is not None else None,
            "router": self.router.snapshot(),
            "rejections": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])[:12]),
            "counters": {"candidates": self.candidates_seen, "orders_sent": self.orders_sent,
                         "orders_filled": self.orders_filled, "bars": self._bars_processed,
                         "trades_closed": len(self.trades)},
            "memory": self.memory.report() if self.memory else {},
            "recent_trades": [t.as_dict() for t in self.trades[-15:]][::-1],
        }
