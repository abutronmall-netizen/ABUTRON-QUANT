"""Event-driven backtester that runs the production engine.

It does not contain its own strategy logic. It wires a synthetic (or recorded)
market into the same TradingEngine used live, with the same risk engine, prop
guard, router and journal. Backtest numbers therefore describe the system that
would actually trade, including spread, slippage and commission.
"""
from __future__ import annotations

import csv
import json
import os
import time
import zlib
from dataclasses import asdict, dataclass, field
from typing import Optional

from .data.deriv import bars_from_csv
from .data.feed import SyntheticConfig, SyntheticFeed, TickReplayFeed, ticks_from_csv
from .engine import EngineConfig, TradingEngine
from .execution.base import SymbolRegistry
from .execution.router import BrokerRouter, Route
from .execution.sim import SimBroker, SimConfig
from .models import Bar, Quote
from .portfolio import ReflectionMemory
from .prop import PropGuard, PropProfile, get_profile, plan_for
from .risk import RiskConfig
from .store import Store, compute_metrics
from .strategy import StrategyProfile
from .symbols import DEFAULT_SYMBOLS_BY_BROKER
from .timeutil import tf_seconds

#: The synthetic tape must not move when the clock does: a published reference
#: run is only useful if `--seed 7` reproduces it tomorrow. 2023-11-14 22:13 UTC,
#: aligned to any timeframe we trade.
ANCHOR_TS = 1_700_000_000.0

#: plausible anchors so synthetic series start in the right price neighbourhood
BASE_PRICES = {
    "R_10": 6500.0, "R_25": 3200.0, "R_50": 240.0, "R_75": 48000.0, "R_100": 1800.0,
    "1HZ10V": 8200.0, "1HZ25V": 4200.0, "1HZ50V": 320.0, "1HZ75V": 62000.0, "1HZ100V": 2100.0,
    "BOOM300N": 9000.0, "BOOM500": 12000.0, "BOOM1000": 15000.0,
    "CRASH300N": 7000.0, "CRASH500": 5000.0, "CRASH1000": 4000.0,
    "EURUSD": 1.0850, "GBPUSD": 1.2700, "USDJPY": 152.40, "AUDUSD": 0.6600,
    "USDCAD": 1.3600, "EURJPY": 165.20,
    "XAUUSD": 2380.0, "XAGUSD": 28.40,
    "US30": 39500.0, "NAS100": 18200.0, "SPX500": 5250.0, "GER40": 18400.0,
    "UK100": 8200.0, "BTCUSD": 68000.0,
}

#: per-bar volatility (bps) that roughly matches each instrument's character
VOL_BPS = {
    "R_10": 3.0, "R_25": 5.0, "R_50": 7.0, "R_75": 8.0, "R_100": 10.0,
    "1HZ10V": 3.0, "1HZ25V": 5.0, "1HZ50V": 7.0, "1HZ75V": 8.0, "1HZ100V": 10.0,
    "BOOM300N": 9.0, "BOOM500": 9.0, "BOOM1000": 9.0,
    "CRASH300N": 9.0, "CRASH500": 9.0, "CRASH1000": 9.0,
    "EURUSD": 4.0, "GBPUSD": 5.0, "USDJPY": 5.0, "AUDUSD": 4.5,
    "USDCAD": 4.0, "EURJPY": 5.0,
    "XAUUSD": 7.0, "XAGUSD": 9.0,
    "US30": 6.0, "NAS100": 7.0, "SPX500": 5.0, "GER40": 6.0,
    "UK100": 5.0, "BTCUSD": 15.0,
}


@dataclass
class BacktestRequest:
    symbols_by_broker: dict[str, list[str]] = field(
        default_factory=lambda: dict(DEFAULT_SYMBOLS_BY_BROKER))
    bars: int = 4000
    tf: str = "M5"
    seed: int = 7
    initial_balance: float = 10_000.0
    strategy: str = "scalp_balanced"
    strategy_profile: Optional[StrategyProfile] = None
    prop_profile: Optional[str] = None
    risk: RiskConfig = field(default_factory=RiskConfig)
    warmup_bars: int = 60
    slippage_bps: float = 0.3
    stop_slippage_bps: float = 1.0
    commission_scale: float = 1.0
    record: bool = True
    out_dir: str = "runs/latest"
    use_prop_plan: bool = True
    start_ts: Optional[float] = None
    #: when `start_ts` is unset the synthetic tape is generated to *end* here
    #: instead of at "now", so a seeded run reproduces across days and machines
    anchor_ts: float = ANCHOR_TS
    max_bars_hold: int = 12
    # synthetic-market structure knobs (validation only; real data comes from feeds)
    trend_strength: float = 0.35
    vol_scale: float = 1.0
    spread_bps: Optional[float] = None
    data_dir: str = "data"            # cached candles written by `apex fetch`
    prefer_cached: bool = True
    tick_path: str = ""               # recorded ticks (ts,symbol,bid,ask) to replay
    news_calendar: str = ""           # JSON calendar for prop blackout windows


@dataclass
class BacktestResult:
    metrics: dict
    prop: Optional[dict]
    rejections: dict
    trades: list[dict]
    equity_curve: list[dict]
    signal_stats: dict
    config: dict
    elapsed_s: float
    out_dir: str = ""

    def summary_line(self) -> str:
        m = self.metrics
        return (f"{m['trades']:>4} trades | win {m['win_rate']:>5.1f}% | pnl {m['pnl']:>9.2f} | "
                f"PF {m['profit_factor']:>4.2f} | avgR {m['avg_r']:>+5.2f} | "
                f"maxDD {m['max_drawdown_pct']:>5.2f}%")


def _tape_start(req: BacktestRequest) -> float:
    """First bar of the synthetic tape.

    An explicit `start_ts` wins; otherwise the tape ends at `anchor_ts` and runs
    backwards `bars` steps, which is what keeps a fixed seed on a fixed tape.
    """
    step = tf_seconds(req.tf)
    base = float(req.start_ts if req.start_ts is not None else req.anchor_ts)
    return (int(base) // step) * step - req.bars * step


def _series_for(symbol: str, req: BacktestRequest) -> list[Bar]:
    """Real cached candles when available, otherwise the synthetic control tape."""
    if req.prefer_cached and req.data_dir:
        cached = os.path.join(req.data_dir, f"{symbol}_{req.tf}.csv")
        if os.path.exists(cached):
            bars = bars_from_csv(symbol, cached)
            if len(bars) >= 200:
                return bars[:req.bars] if len(bars) > req.bars else bars
    base = BASE_PRICES.get(symbol, 1000.0)
    vol = VOL_BPS.get(symbol, 6.0) * req.vol_scale
    if symbol.startswith(("BOOM", "CRASH")):
        cfg = SyntheticConfig(base_price=base, tf_seconds=tf_seconds(req.tf), vol_bps=vol,
                              seed=req.seed * 7919 + zlib.crc32(symbol.encode()) % 100000,
                              jump_probability=0.01, jump_bps=250.0,
                              trend_strength=req.trend_strength)
        return SyntheticFeed(symbol, cfg, start_ts=_tape_start(req)).bars(req.bars)
    start = _tape_start(req)
    cfg = SyntheticConfig(base_price=base, tf_seconds=tf_seconds(req.tf), vol_bps=vol,
                          seed=req.seed * 7919 + zlib.crc32(symbol.encode()) % 100000,
                          trend_strength=req.trend_strength)
    return SyntheticFeed(symbol, cfg, start_ts=start).bars(req.bars)


def _clock_start(req: BacktestRequest) -> float:
    """The engine clock starts where the data starts.

    Tick replay uses whatever the file contains; a synthetic run uses the fixed
    anchor. Never `time.time()` — a risk engine judging a 2023 tape against
    today's session clock rejects everything, and a backtest that silently
    trades nothing looks like a strategy with no setups.
    """
    if req.tick_path and req.start_ts is None:
        try:
            rows = ticks_from_csv(req.tick_path)
            if rows:
                return float(rows[0][0])
        except OSError:
            pass
    return _tape_start(req)


def run_backtest(req: BacktestRequest) -> BacktestResult:
    t0 = time.time()
    registry = SymbolRegistry()
    brokers: dict[str, SimBroker] = {}
    routes: list[Route] = []
    all_bars: dict[str, list[Bar]] = {}

    for broker, symbols in req.symbols_by_broker.items():
        sim = SimBroker(registry, SimConfig(
            initial_balance=req.initial_balance, slippage_bps=req.slippage_bps,
            stop_slippage_bps=req.stop_slippage_bps, seed=req.seed,
        ), account=f"{broker.upper()}-BT")
        brokers[broker] = sim
        routes.append(Route(broker=broker, account=sim.account_id, port=sim, symbols=symbols))
        for s in symbols:
            all_bars[s] = _series_for(s, req)

    store = Store(os.path.join(req.out_dir, "apex.db")) if req.record else None
    router = BrokerRouter(routes, registry=registry, store=store, retry_sleep=0.0)

    strategy = req.strategy_profile or StrategyProfile.preset(req.strategy)
    strategy.entry_tf = req.tf

    prop: Optional[PropGuard] = None
    risk_cfg = req.risk
    if req.prop_profile:
        profile: PropProfile = get_profile(req.prop_profile)
        plan = plan_for(profile, req.initial_balance)
        if req.use_prop_plan:
            risk_cfg.risk_per_trade_pct = plan["risk_per_trade_pct"]
            risk_cfg.max_trades_per_day = max(risk_cfg.max_trades_per_day,
                                              plan["max_trades_per_day"])
            risk_cfg.max_daily_loss_pct = min(risk_cfg.max_daily_loss_pct,
                                              profile.max_daily_loss_pct * 0.9)
        news_windows = None
        if req.news_calendar:
            from .news import NewsCalendar
            try:
                cal = NewsCalendar.load(req.news_calendar)
                news_windows = cal.windows(0, 2 ** 40, minutes=profile.news_blackout_minutes)
            except Exception:
                news_windows = None
        prop = PropGuard(profile, req.initial_balance, now=_clock_start(req),
                         block_news_windows=news_windows)

    memory = ReflectionMemory(path=os.path.join(req.out_dir, "reflection.json") if req.record else None)

    engine = TradingEngine(
        router=router, symbols_by_broker=req.symbols_by_broker,
        cfg=EngineConfig(entry_tf=req.tf, warmup_bars=req.warmup_bars,
                         max_bars_hold=req.max_bars_hold),
        strategy=strategy, risk=risk_cfg, prop=prop, store=store, memory=memory,
        now=_clock_start(req),
    )

    if req.tick_path:
        _run_tick_replay(req, engine, brokers)
        merged = []
    else:
        merged = sorted(((b.ts, sym, b) for sym, bars in all_bars.items() for b in bars),
                        key=lambda t: (t[0], t[1]))
    for _ts, sym, bar in merged:
        sim = brokers[engine.states[sym].broker]
        spec = registry.spec(engine.states[sym].broker, sym)
        # Cost model: use the instrument's quoted spread where the registry knows
        # it; otherwise fall back to a calibrated bps figure. These defaults are
        # deliberately conservative for retail scalping - calibrate them against
        # your broker's statements before trusting any P&L number.
        spread = spec.typical_spread or bar.close * (req.spread_bps or 0.5) / 10_000.0
        half = spread / 2.0
        sim.set_time(bar.ts)
        sim.set_quote(Quote(sym, bar.ts + tf_seconds(req.tf), bar.close - half, bar.close + half))
        # Order matters for fill realism:
        #  1. ingest the bar (indicators, regime, new entries at the close);
        #  2. resolve stops/targets that were touched *inside* the bar at their
        #     trigger price, not at the close;
        #  3. only then manage surviving positions with the closing quote.
        engine.set_quote(Quote(sym, bar.ts, bar.close - half, bar.close + half))
        engine.on_bar(bar)
        engine.on_bar_brackets(bar)
        engine.on_quote(Quote(sym, bar.ts, bar.close - half, bar.close + half))
        engine.cycle_guard()

    engine.flatten_all("backtest_end")
    metrics = compute_metrics([t.as_dict() for t in engine.trades],
                              store.equity_curve() if store else None,
                              initial_equity=req.initial_balance)
    prop_status = prop.status(engine._last_equity_ts or time.time()) if prop else None
    signal_stats = store.signal_stats() if store else {}

    result = BacktestResult(
        metrics=metrics,
        prop=prop_status,
        rejections=dict(engine.rejections),
        trades=[t.as_dict() for t in engine.trades],
        equity_curve=(store.equity_curve() if store else []),
        signal_stats=signal_stats,
        config={
            "request": {**asdict(req), "risk": risk_cfg.__dict__},
            "strategy": strategy.__dict__,
            "symbols": req.symbols_by_broker,
            "prop_profile": req.prop_profile,
        },
        elapsed_s=round(time.time() - t0, 2),
        out_dir=req.out_dir,
    )
    if req.record:
        _write_artifacts(result, req)
    if store:
        store.close()
    return result


def _write_artifacts(result: BacktestResult, req: BacktestRequest) -> None:
    os.makedirs(req.out_dir, exist_ok=True)
    with open(os.path.join(req.out_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"metrics": result.metrics, "prop": result.prop,
                   "rejections": result.rejections, "signal_stats": result.signal_stats,
                   "config": result.config, "elapsed_s": result.elapsed_s},
                  fh, indent=2, default=str)
    with open(os.path.join(req.out_dir, "trades.csv"), "w", newline="", encoding="utf-8") as fh:
        if result.trades:
            w = csv.DictWriter(fh, fieldnames=list(result.trades[0].keys()))
            w.writeheader()
            for t in result.trades:
                w.writerow({k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                            for k, v in t.items()})
    with open(os.path.join(req.out_dir, "equity.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "equity", "balance"])
        for row in result.equity_curve:
            w.writerow([row["ts"], row["equity"], row["balance"]])
    with open(os.path.join(req.out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(render_markdown(result))


def _run_tick_replay(req: BacktestRequest, engine, brokers: dict) -> None:
    """Drive the engine with recorded bid/ask ticks instead of synthetic bars.

    This is the only way to study spread and stop slippage honestly: positions
    are managed at the quoted side, brackets trigger on the tick that crosses
    them, and bars are only used for the decision timeframe.
    """
    try:
        rows = ticks_from_csv(req.tick_path)
    except OSError as exc:
        raise ValueError(f"cannot read ticks at {req.tick_path}: {exc}") from exc
    if not rows:
        raise ValueError(f"no ticks found in {req.tick_path}")
    feed = TickReplayFeed(rows, tf_seconds(req.tf))
    last_sym = None
    for quote, bar in feed:
        st = engine.states.get(quote.symbol)
        if st is None:
            continue
        sim = brokers[st.broker]
        sim.set_time(quote.ts)
        sim.set_quote(quote)
        engine.on_quote(quote)                 # position management at real spread
        if bar is not None:
            engine.set_quote(quote)
            engine.on_bar(bar)
            engine.on_bar_brackets(bar)
        if last_sym != quote.symbol:
            engine.cycle_guard()
            last_sym = quote.symbol
    engine.cycle_guard()


def render_markdown(result: BacktestResult) -> str:
    m = result.metrics
    lines = [
        "# ABUTRON QUANT — backtest report", "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}", "",
        "## Headline", "",
        "| Metric | Value |", "| --- | --- |",
        f"| Trades | {m['trades']} |",
        f"| Win rate | {m['win_rate']}% |",
        f"| Net P&L | {m['pnl']} |",
        f"| Expectancy / trade | {m['expectancy']} |",
        f"| Profit factor | {m['profit_factor']} |",
        f"| Average R | {m['avg_r']} |",
        f"| Max drawdown | {m['max_drawdown_pct']}% |",
        f"| Sharpe (annualised) | {m['sharpe']} |",
        f"| Sortino | {m['sortino']} |",
        f"| Trading days | {m.get('days', 0)} |", "",
    ]
    if result.prop:
        p = result.prop
        lines += ["## Prop-firm evaluation", "",
                  f"Profile: **{p['profile']}** — verdict **{p['verdict'].upper()}**", "",
                  "| Check | Value |", "| --- | --- |",
                  f"| Profit / target | {p['profit_pct']}% / {p['target_pct']}% "
                  f"({p['target_progress_pct']}%) |",
                  f"| Daily loss used | {p['daily_loss_used_pct']}% |",
                  f"| Loss room (total) | {p['total_loss_room']} |",
                  f"| Days traded | {p['days_traded']} / {p['min_trading_days']} min |",
                  f"| Consistency | {p['consistency']['best_day_share_pct']}% "
                  f"(rule {p['consistency']['rule_pct']}%) |",
                  f"| Breach | {p['breach'] or 'none'} |", ""]
    if result.rejections:
        lines += ["## Why trades did / did not happen", "",
                  "| Status | Count |", "| --- | --- |"]
        for k, v in sorted(result.rejections.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {k} | {v} |")
        lines.append("")
    for title, key in (("By instrument", "by_symbol"), ("By exit reason", "by_exit"),
                       ("By regime", "by_regime")):
        if m.get(key):
            lines += [f"## {title}", "", "| Key | Trades | Win % | P&L | Avg R |", "| --- | --- | --- | --- | --- |"]
            for k, g in sorted(m[key].items(), key=lambda kv: -kv[1]["trades"]):
                lines.append(f"| {k} | {g['trades']} | {g['win_rate']} | {g['pnl']} | {g['avg_r']} |")
            lines.append("")
    lines += ["---", "",
              "Synthetic-data results measure *system behaviour*, not future returns. "
              "They are not a claim of profitability and not evidence of a prop-firm pass.", ""]
    return "\n".join(lines)
