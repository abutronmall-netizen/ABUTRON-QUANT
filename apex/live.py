"""Live runner: wires feeds to the trading engine.

Three modes, one engine:

  * ``deriv`` — Deriv WebSocket ticks -> M5 bars -> engine -> Deriv contracts
  * ``mt5``   — FP Markets terminal polling -> M5 bars -> engine -> MT5 orders
  * ``paper`` — synthetic or replayed data with the simulated broker (default)

Live modes refuse to transmit orders unless ``--live`` is passed explicitly and
the adapter's ``dry_run`` flag is cleared. This is intentional: the audit's
worst failure mode was ambiguity about whether a component was actually trading.
"""
from __future__ import annotations

import asyncio
import zlib
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .backtest import BASE_PRICES, VOL_BPS
from .data.deriv import DerivAPI, DerivConfig, DerivFeed, SYMBOL_NAMES, fetch_candles_sync
from .data.feed import LiveBarBuilder, SyntheticConfig, SyntheticFeed
from .data.mt5 import MT5Config, MT5Connection, MT5Feed
from .engine import EngineConfig, TradingEngine
from .execution.base import SymbolRegistry
from .execution.deriv import DerivBroker
from .execution.mt5 import MT5Broker
from .execution.router import BrokerRouter, Route
from .execution.sim import SimBroker, SimConfig
from .models import Quote
from .portfolio import ReflectionMemory
from .prop import PropGuard, get_profile, plan_for
from .risk import RiskConfig
from .store import Store, compute_metrics
from .strategy import StrategyProfile


@dataclass
class LiveConfig:
    mode: str = "paper"                       # paper | deriv | mt5
    symbols_by_broker: dict[str, list[str]] = field(
        default_factory=lambda: {"deriv": ["R_25", "R_50", "R_75"]})
    tf: str = "M5"
    strategy: str = "scalp_balanced"
    prop_profile: Optional[str] = "abutron_inhouse"
    initial_balance: float = 10_000.0
    store_path: str = "runs/live/apex.db"
    memory_path: str = "runs/live/reflection.json"
    deriv_app_id: str = "1089"
    deriv_token: str = ""
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""
    mt5_path: str = ""
    live_orders: bool = False                 # must be explicitly true to trade
    warmup_bars: int = 80
    preload_bars: int = 0          # paper mode: replay N bars so the desk opens with history
    bar_seconds: float = 3.0       # wall-clock seconds per bar in paper mode
    bar_seconds_override: Optional[float] = None   # demo speed-up for paper mode
    seed: int = 11
    trend_strength: float = 1.2
    risk: RiskConfig = field(default_factory=RiskConfig)
    boot_policy: str = "adopt"          # adopt | flatten | ignore  (broker positions on start)
    persist_state: bool = True          # keep risk/prop/position state across restarts
    news_calendar: str = ""             # JSON calendar of blackout windows
    mt5_server_offset_hours: Optional[float] = None   # None -> auto-detect
    reconcile_every_s: float = 30.0
    record_ticks: str = ""              # write ticks to this CSV for replay research
    ai_review: bool = False             # optional local LLM review (advisory by default)
    ai_mode: str = "advisory"           # advisory | veto
    ai_url: str = "http://127.0.0.1:11434"
    ai_model: str = "qwen2.5:1.5b"


class LiveRunner:
    def __init__(self, cfg: LiveConfig):
        self.cfg = cfg
        self.registry = SymbolRegistry()
        self.store = Store(cfg.store_path)
        self.memory = ReflectionMemory(path=cfg.memory_path)
        self.routes: list[Route] = []
        self.brokers: dict[str, object] = {}
        self.feed = None
        self.deriv_api: Optional[DerivAPI] = None
        self.mt5_feed: Optional[MT5Feed] = None
        self._stop = threading.Event()
        self._async_stop: Optional[asyncio.Event] = None
        self._builders: dict[str, LiveBarBuilder] = {}
        self.session_start = time.time()
        self._thread: Optional[threading.Thread] = None

        self.strategy = StrategyProfile.preset(cfg.strategy)
        self.strategy.entry_tf = cfg.tf
        self.risk_cfg = cfg.risk

        if cfg.prop_profile:
            profile = get_profile(cfg.prop_profile)
            plan = plan_for(profile, cfg.initial_balance)
            self.risk_cfg.risk_per_trade_pct = plan["risk_per_trade_pct"]
            self.prop = PropGuard(profile, cfg.initial_balance, now=time.time())
        else:
            self.prop = None

        self._build_brokers()
        self.router = BrokerRouter(self.routes, registry=self.registry, store=self.store)
        reviewer = None
        if cfg.ai_review:
            from .ai.reviewer import OllamaReviewer, ReviewerConfig
            reviewer = OllamaReviewer(ReviewerConfig(url=cfg.ai_url, model=cfg.ai_model,
                                                     mode=cfg.ai_mode))
        self.engine = TradingEngine(
            router=self.router, symbols_by_broker=cfg.symbols_by_broker,
            cfg=EngineConfig(entry_tf=cfg.tf, warmup_bars=cfg.warmup_bars,
                             # live reconcile is time-based, not bar-based
                             reconcile_every=0, adopt_on_boot=True,
                             ai_review=cfg.ai_review, ai_mode=cfg.ai_mode),
            strategy=self.strategy, risk=self.risk_cfg, prop=self.prop,
            store=self.store, memory=self.memory, now=time.time(), reviewer=reviewer,
        )
        self._last_reconcile = 0.0
        self.news = None
        self.ticks = None
        if cfg.news_calendar:
            from .news import NewsCalendar
            try:
                self.news = NewsCalendar.load(cfg.news_calendar)
            except Exception as exc:
                self.store.record_event("warn", f"news calendar unreadable: {exc}")
        if cfg.record_ticks:
            from .data.feed import TickRecorder
            self.ticks = TickRecorder(cfg.record_ticks)
        if cfg.persist_state:
            self._restore_state()

    # ------------------------------------------------------------------ #
    def _restore_state(self) -> None:
        """Rehydrate risk/prop/positions after a restart.

        Without this a crash at 14:00 silently resets the daily loss budget and
        the challenge counters, which is exactly how a compliant day becomes a
        breached account.
        """
        try:
            if self.engine.load_state("engine"):
                self.store.record_event(
                    "info", f"state restored: {len(self.engine.positions)} position(s), "
                            f"halted={self.engine.risk_engine.state.halted}")
        except Exception as exc:
            self.store.record_event("warn", f"state restore failed: {exc}")

    def boot_sync(self) -> dict:
        """Adopt or flatten whatever the broker already holds (run before trading)."""
        out = self.engine.boot_sync(self.cfg.boot_policy)
        self._last_reconcile = time.time()
        if out.get("adopted"):
            self.store.record_event("reconcile", f"boot adopted {len(out['adopted'])} position(s)")
        return out

    def _maybe_reconcile(self) -> None:
        if not self.cfg.reconcile_every_s:
            return
        if time.time() - self._last_reconcile < self.cfg.reconcile_every_s:
            return
        self._last_reconcile = time.time()
        if self.news is not None and self.prop is not None:
            now = time.time()
            try:
                self.prop.news_windows = self.news.windows(
                    now - 3600, now + 14 * 86400,
                    minutes=self.prop.p.news_blackout_minutes)
            except Exception:
                pass
        try:
            diffs = self.engine.reconcile()
            if diffs:
                self.store.record_event("reconcile", f"{len(diffs)} discrepancy(ies)") 
        except Exception as exc:
            self.store.record_event("warn", f"reconcile failed: {exc}")

    # ------------------------------------------------------------------ #
    def _build_brokers(self) -> None:
        cfg = self.cfg
        if cfg.mode == "deriv":
            broker = DerivBroker(
                account="DERIV-LIVE", cfg=DerivConfig(app_id=cfg.deriv_app_id, token=cfg.deriv_token),
                registry=self.registry, dry_run=not cfg.live_orders)
            broker.connect()
            self.deriv_api = broker.api
            self.brokers["deriv"] = broker
            self.routes.append(Route(broker="deriv", account=broker.account_id, port=broker,
                                     symbols=cfg.symbols_by_broker.get("deriv", []),
                                     label="Deriv synthetics"))
            self.feed = DerivFeed(broker.api, cfg.symbols_by_broker.get("deriv", []))
        elif cfg.mode == "mt5":
            broker = MT5Broker(account="FP-MARKETS",
                               cfg=MT5Config(login=cfg.mt5_login, password=cfg.mt5_password,
                                             server=cfg.mt5_server, path=cfg.mt5_path,
                                             server_utc_offset_hours=cfg.mt5_server_offset_hours),
                               registry=self.registry, dry_run=not cfg.live_orders)
            broker.connect()
            self.brokers["mt5"] = broker
            self.routes.append(Route(broker="mt5", account=broker.account_id, port=broker,
                                     symbols=cfg.symbols_by_broker.get("mt5", []),
                                     label="FP Markets MT5"))
            self.mt5_feed = MT5Feed(broker.conn, cfg.symbols_by_broker.get("mt5", []), tf=cfg.tf)
        else:
            for broker, symbols in cfg.symbols_by_broker.items():
                sim = SimBroker(self.registry, SimConfig(initial_balance=cfg.initial_balance,
                                                         seed=cfg.seed), account="PAPER-001")
                self.brokers[broker] = sim
                self.routes.append(Route(broker=broker, account=sim.account_id, port=sim,
                                         symbols=symbols, label="Paper"))
            self.feed = _PaperFeed(cfg)

    # ------------------------------------------------------------------ #
    def warmup(self) -> None:
        """Seed indicators so the engine can trade from the first live bar."""
        n = self.cfg.warmup_bars
        # Warmup builds indicator state only. Trading during warmup would fire
        # orders before the broker adapters have a live quote - the exact class
        # of silent failure this rebuild is meant to eliminate.
        self.engine.paused = True
        for broker, symbols in self.cfg.symbols_by_broker.items():
            for sym in symbols:
                bars = self._history(broker, sym, n)
                for b in bars:
                    self.engine.set_quote(Quote(sym, b.ts, b.close, b.close))
                    self.engine.on_bar(b)
        self.engine.paused = False

        if self.cfg.mode != "paper":
            self.boot_sync()
            if self.news is not None and self.prop is not None:
                now = time.time()
                self.prop.news_windows = self.news.windows(
                    now - 3600, now + self.prop.p.max_calendar_days * 86400,
                    minutes=self.prop.p.news_blackout_minutes)

        if self.cfg.preload_bars and self.cfg.mode == "paper":
            assert isinstance(self.feed, _PaperFeed)
            for broker, symbols in self.cfg.symbols_by_broker.items():
                for sym in symbols:
                    for _ in range(self.cfg.preload_bars):
                        self._process_bar(broker, sym, self.feed.next_bar(sym))
            self.engine.flatten_all("preload_end") if False else None

    def _history(self, broker: str, symbol: str, n: int):
        if self.cfg.mode == "deriv":
            try:
                return fetch_candles_sync(symbol, 300, n, app_id=self.cfg.deriv_app_id,
                                          token=self.cfg.deriv_token)
            except Exception as exc:
                self.store.record_event("warn", f"deriv history failed for {symbol}: {exc}")
        if self.cfg.mode == "mt5" and self.mt5_feed is not None:
            try:
                return self.mt5_feed.conn.rates(symbol, self.cfg.tf, n)
            except Exception as exc:
                self.store.record_event("warn", f"mt5 history failed for {symbol}: {exc}")
        gen = SyntheticFeed(symbol, SyntheticConfig(
            base_price=BASE_PRICES.get(symbol, 1000.0),
            tf_seconds=_tf(self.cfg.tf),
            vol_bps=VOL_BPS.get(symbol, 6.0),
            seed=self.cfg.seed * 31 + zlib.crc32(symbol.encode()) % 1000,
            trend_strength=self.cfg.trend_strength,
        ))
        return gen.bars(n)

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        if self.cfg.mode == "deriv":
            asyncio.run(self._run_deriv())
        elif self.cfg.mode == "mt5":
            self._run_mt5()
        else:
            self._run_paper()

    def start_background(self) -> None:
        self._thread = threading.Thread(target=self.run, daemon=True, name="apex-live")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    def _process_bar(self, broker: str, sym: str, bar) -> None:
        """One bar through the full live path (quote -> engine -> brackets -> manage)."""
        port = self.brokers.get(broker)
        spec = self.registry.spec(broker, sym)
        half = (spec.typical_spread or bar.close * 0.00005) / 2
        q = Quote(sym, bar.ts, bar.close - half, bar.close + half)
        if isinstance(port, SimBroker):
            port.set_time(bar.ts)
            port.set_quote(q)
        if self.ticks is not None:
            self.ticks.write(q)
        self.engine.set_quote(q)
        self.engine.on_bar(bar)
        self.engine.on_bar_brackets(bar)
        self.engine.on_quote(q)
        self.engine.cycle_guard()

    def _run_paper(self) -> None:
        assert isinstance(self.feed, _PaperFeed)
        period = self.cfg.bar_seconds_override or self.cfg.bar_seconds
        while not self._stop.is_set():
            t0 = time.time()
            for broker, symbols in self.cfg.symbols_by_broker.items():
                for sym in symbols:
                    self._process_bar(broker, sym, self.feed.next_bar(sym))
            elapsed = time.time() - t0
            self._maybe_reconcile()
            self._stop.wait(max(0.02, period - elapsed))

    def _run_mt5(self) -> None:
        assert self.mt5_feed is not None
        builders = {s: LiveBarBuilder(s, _tf(self.cfg.tf)) for s in self.cfg.symbols_by_broker.get("mt5", [])}
        while not self._stop.is_set():
            try:
                quotes = self.mt5_feed.poll_once()
            except Exception as exc:
                self.store.record_event("error", f"mt5 poll failed: {exc}")
                time.sleep(2)
                continue
            for q in quotes:
                self.engine.on_quote(q)
                closed = builders[q.symbol].on_tick(q.ts, q.mid)
                if closed is not None:
                    self.engine.set_quote(q)
                    self.engine.on_bar(closed)
            self.engine.cycle_guard()
            self._maybe_reconcile()
            time.sleep(max(0.2, 1.0 / 4))

    async def _make_deriv_session(self):
        """Open one Deriv session (socket + subscriptions). Idempotent."""
        from .data.deriv import DerivConfig
        api = DerivAPI(DerivConfig(app_id=self.cfg.deriv_app_id, token=self.cfg.deriv_token))
        feed = DerivFeed(api, self.cfg.symbols_by_broker.get("deriv", []))
        feed.on_quote = self._on_deriv_quote
        await feed.start()
        self.deriv_api, self.feed = api, feed
        self.store.record_event("info", f"deriv feed up: {self.cfg.symbols_by_broker.get('deriv')}")
        return _DerivSession(api, feed)

    def _on_deriv_quote(self, q: Quote) -> None:
        self.engine.on_quote(q)
        if self.ticks is not None:
            self.ticks.write(q)
        b = self._builders.get(q.symbol)
        if b is None:
            return
        closed = b.on_tick(q.ts, q.mid)
        if closed is not None:
            self.engine.set_quote(q)
            self.engine.on_bar(closed)

    def _resync(self) -> None:
        """Refill indicators after a reconnect so the desk does not trade blind."""
        self.engine.paused = True
        try:
            for sym in self.cfg.symbols_by_broker.get("deriv", []):
                for bar in self._history("deriv", sym, self.cfg.warmup_bars):
                    self.engine.set_quote(Quote(sym, bar.ts, bar.close, bar.close))
                    self.engine.on_bar(bar)
        finally:
            self.engine.paused = False
        self.store.record_event("info", "indicators resynced after reconnect")

    async def _run_deriv(self) -> None:
        self._async_stop = asyncio.Event()
        self._builders = {s: LiveBarBuilder(s, _tf(self.cfg.tf))
                          for s in self.cfg.symbols_by_broker.get("deriv", [])}

        async def connect():
            session = await self._make_deriv_session()
            self._resync()
            return session

        supervisor = FeedSupervisor(
            connect=connect,
            stop=self._stop,
            on_event=lambda kind, msg: self.store.record_event(kind, msg),
            on_restart=self._resync,
        )
        try:
            await supervisor.run()
        finally:
            if self.deriv_api is not None:
                try:
                    await self.deriv_api.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        snap = self.engine.snapshot()
        snap["mode"] = self.cfg.mode
        snap["live_orders"] = self.cfg.live_orders
        snap["session_start"] = self.session_start
        # Session-scoped metrics: the dashboard must describe what the operator is
        # watching right now, not rows left over from a previous run in the journal.
        snap["signal_stats"] = dict(self.engine.rejections)
        snap["metrics"] = compute_metrics([t.as_dict() for t in self.engine.trades],
                                          initial_equity=self.cfg.initial_balance)
        snap["journal_metrics"] = self.store.metrics()
        snap["events"] = self.store.recent_events(40)
        snap["signals"] = self.store.recent_signals(30)
        snap["symbol_names"] = {s: SYMBOL_NAMES.get(s, s) for s in self.engine.states}
        snap["safety"] = {
            "boot_policy": self.cfg.boot_policy,
            "persist_state": self.cfg.persist_state,
            "reconcile_every_s": self.cfg.reconcile_every_s,
            "last_reconcile": round(self._last_reconcile, 0),
            "reconnects": getattr(self.deriv_api, "reconnects", 0) if self.deriv_api else 0,
            "ticks_recorded": self.ticks.written if self.ticks is not None else 0,
        }
        if self.news is not None:
            now = time.time()
            nxt = self.news.next_event(now)
            snap["news"] = {
                "events": len(self.news),
                "blackout": self.news.in_blackout(now,
                                                  minutes=(self.prop.p.news_blackout_minutes
                                                           if self.prop else 2)),
                "next": ({"ts": nxt.ts, "currency": nxt.currency, "title": nxt.title}
                         if nxt else None),
            }
        return snap

    def shutdown(self) -> None:
        self.stop()
        if self.ticks is not None:
            try:
                self.ticks.close()
            except Exception:
                pass
        if self.cfg.persist_state:
            try:
                self.engine.save_state("engine")
            except Exception:
                pass
        try:
            self.store.close()
        except Exception:
            pass


def _tf(tf: str) -> int:
    return {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}.get(tf, 300)


class _PaperFeed:
    """Real-time synthetic tape for the paper/demo mode."""

    def __init__(self, cfg: LiveConfig):
        self.gens: dict[str, SyntheticFeed] = {}
        self.cfg = cfg
        for broker, symbols in cfg.symbols_by_broker.items():
            for s in symbols:
                self.gens[s] = SyntheticFeed(s, SyntheticConfig(
                    base_price=BASE_PRICES.get(s, 1000.0),
                    tf_seconds=_tf(cfg.tf),
                    vol_bps=VOL_BPS.get(s, 6.0),
                    seed=cfg.seed * 31 + zlib.crc32(s.encode()) % 1000,
                    trend_strength=cfg.trend_strength,
                ))

    def next_bar(self, symbol: str):
        return self.gens[symbol].next_bar()


# --------------------------------------------------------------------------- #
# connection supervision
# --------------------------------------------------------------------------- #
class _DerivSession:
    """One live Deriv socket + its subscriptions."""
    __slots__ = ("api", "feed")

    def __init__(self, api: DerivAPI, feed: DerivFeed):
        self.api = api
        self.feed = feed

    @property
    def alive(self) -> bool:
        return self.api.alive

    async def close(self) -> None:
        try:
            for sym in list(self.feed.symbols):
                await self.api.unsubscribe(sym)
        except Exception:
            pass
        try:
            await self.api.close()
        except Exception:
            pass


class FeedSupervisor:
    """Keeps a socket feed alive: detect death, back off, reconnect, resync.

    The audited deployment failed the other way round: the process stayed up and
    the socket quietly died, so the dashboard looked healthy while nothing was
    trading. A feed that has been silent for longer than the adapter's staleness
    window is treated as dead and replaced.
    """

    def __init__(self, connect, stop, on_event=None, on_restart=None,
                 sleep=None, base_delay: float = 2.0, max_delay: float = 60.0,
                 check_interval: float = 1.0, max_restarts: Optional[int] = None,
                 stable_after: float = 30.0):
        self.connect = connect
        self.stop = stop
        self.on_event = on_event
        self.on_restart = on_restart
        self.sleep = sleep
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.check_interval = check_interval
        self.max_restarts = max_restarts
        # a connection that lived this long is "healthy": the next failure starts
        # the backoff again from base_delay instead of continuing to escalate
        self.stable_after = stable_after
        self.restarts = 0
        self.delays: list[float] = []

    def _event(self, kind: str, msg: str) -> None:
        if self.on_event:
            try:
                self.on_event(kind, msg)
            except Exception:
                pass

    async def _nap(self, seconds: float) -> None:
        if self.sleep is not None:
            await self.sleep(seconds)
            return
        try:
            await asyncio.wait_for(asyncio.to_thread(self.stop.wait, seconds), timeout=seconds + 0.5)
        except Exception:
            await asyncio.sleep(seconds)

    def _stopped(self) -> bool:
        return bool(self.stop.is_set()) if hasattr(self.stop, "is_set") else bool(self.stop)

    async def run(self) -> None:
        session = None
        attempt = 0
        while not self._stopped():
            started = time.monotonic()
            try:
                session = await self.connect()
                while not self._stopped() and session.alive:
                    await self._nap(self.check_interval)
                if self._stopped():
                    break
                uptime = time.monotonic() - started
                if uptime >= self.stable_after:
                    attempt = 0
                self._event("warn", f"feed socket died after {uptime:.0f}s: "
                                    f"no messages within the staleness window")
            except Exception as exc:
                self._event("error", f"feed connect failed: {exc}")
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass
                session = None
            attempt += 1
            self.restarts += 1
            if self.max_restarts is not None and attempt > self.max_restarts:
                self._event("error", f"giving up after {attempt} reconnect attempts")
                return
            delay = min(self.max_delay, self.base_delay * (2 ** min(attempt - 1, 5)))
            self.delays.append(delay)
            self._event("warn", f"reconnect in {delay:.0f}s (attempt {attempt})")
            await self._nap(delay)
            if self.on_restart is not None and not self._stopped():
                try:
                    self.on_restart()
                except Exception as exc:
                    self._event("warn", f"resync failed: {exc}")
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
