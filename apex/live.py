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
from .store import Store
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
        self.engine = TradingEngine(
            router=self.router, symbols_by_broker=cfg.symbols_by_broker,
            cfg=EngineConfig(entry_tf=cfg.tf, warmup_bars=cfg.warmup_bars),
            strategy=self.strategy, risk=self.risk_cfg, prop=self.prop,
            store=self.store, memory=self.memory, now=time.time(),
        )

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
                                             server=cfg.mt5_server, path=cfg.mt5_path),
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
            time.sleep(max(0.2, 1.0 / 4))

    async def _run_deriv(self) -> None:
        assert self.feed is not None and self.deriv_api is not None
        self._async_stop = asyncio.Event()
        builders = {s: LiveBarBuilder(s, _tf(self.cfg.tf))
                    for s in self.cfg.symbols_by_broker.get("deriv", [])}

        def on_quote(q: Quote) -> None:
            self.engine.on_quote(q)
            b = builders.get(q.symbol)
            if b is None:
                return
            closed = b.on_tick(q.ts, q.mid)
            if closed is not None:
                self.engine.set_quote(q)
                self.engine.on_bar(closed)

        self.feed.on_quote = on_quote
        await self.feed.start()
        self.store.record_event("info", f"deriv feed started: {self.cfg.symbols_by_broker.get('deriv')}")
        try:
            while not self._stop.is_set():
                self.engine.cycle_guard()
                await asyncio.sleep(1.0)
        finally:
            for s in self.cfg.symbols_by_broker.get("deriv", []):
                await self.feed.api.unsubscribe(s)
            await self.deriv_api.close()

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        snap = self.engine.snapshot()
        snap["mode"] = self.cfg.mode
        snap["live_orders"] = self.cfg.live_orders
        snap["signal_stats"] = self.store.signal_stats()
        snap["metrics"] = self.store.metrics()
        snap["events"] = self.store.recent_events(40)
        snap["signals"] = self.store.recent_signals(30)
        snap["symbol_names"] = {s: SYMBOL_NAMES.get(s, s) for s in self.engine.states}
        return snap

    def shutdown(self) -> None:
        self.stop()
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
