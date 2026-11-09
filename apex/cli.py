"""Command line interface.

    python -m apex serve --demo                 # dashboard + paper engine (default preview)
    python -m apex backtest --symbols R_25,R_50,R_75 --bars 4000 --prop ftmo_2step
    python -m apex sweep --bars 2500 --seeds 3
    python -m apex propsim --profile ftmo_2step --seeds 12
    python -m apex doctor --config config/deriv_paper.json
    python -m apex fetch --symbol R_75 --bars 5000 --out data/R_75_M5.csv
    python -m apex report --run runs/latest
    python -m apex profiles
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from typing import Optional

from . import __version__
from .backtest import BacktestRequest, render_markdown, run_backtest
from .config import broker_credentials, load_config, risk_from
from .live import LiveConfig, LiveRunner
from .prop import PROFILES, get_profile, plan_for
from .risk import RiskConfig
from .store import Store
from .strategy import StrategyProfile


def _prop_arg(value):
    """Map CLI --prop values: None->config default, 'none'->disabled, path->JSON profile."""
    if value is None:
        return "__config__"
    if str(value).strip().lower() in ("none", "off", "no"):
        return None
    return value


def _split(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


def _symbol_map(broker: str, symbols: list[str]) -> dict[str, list[str]]:
    if broker == "both":
        return {"deriv": [s for s in symbols if s.startswith(("R_", "1HZ", "BOOM", "CRASH", "JD"))],
                "mt5": [s for s in symbols if not s.startswith(("R_", "1HZ", "BOOM", "CRASH", "JD"))]}
    return {broker: symbols}


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_serve(args) -> int:
    from .api.server import DashboardState, serve

    cfg = load_config(args.config)
    creds = broker_credentials(cfg)
    symbols = _split(args.symbols) if args.symbols else \
        cfg["symbols_by_broker"].get(args.broker, ["R_25", "R_50", "R_75"])

    mode = args.mode or ("paper" if args.demo else cfg["mode"])
    live = LiveConfig(
        mode=mode,
        symbols_by_broker=_symbol_map(args.broker, symbols),
        tf=args.tf or cfg["tf"],
        strategy=args.strategy or cfg["strategy"],
        prop_profile=(cfg.get("prop_profile") if _prop_arg(args.prop) == "__config__"
                      else _prop_arg(args.prop)),
        initial_balance=args.balance or cfg["initial_balance"],
        store_path=args.db or cfg["store_path"],
        memory_path=cfg["memory_path"],
        deriv_app_id=creds["deriv_app_id"], deriv_token=creds["deriv_token"],
        mt5_login=creds["mt5_login"], mt5_password=creds["mt5_password"],
        mt5_server=creds["mt5_server"], mt5_path=creds["mt5_path"],
        live_orders=bool(args.live),
        warmup_bars=cfg["warmup_bars"],
        seed=args.seed or cfg["seed"],
        preload_bars=args.preload, bar_seconds_override=args.speed,
        trend_strength=args.trend_strength or cfg["trend_strength"],
        risk=risk_from(cfg),
    )
    if mode != "paper" and not args.live:
        print("[!] live-orders OFF: the engine will simulate fills and will not transmit orders.")
    runner = LiveRunner(live)
    runner.warmup()
    runner.start_background()

    state = DashboardState(runner)
    stop = threading.Event()

    def _term(signum, frame):
        stop.set()
        runner.stop()
    signal.signal(signal.SIGINT, _term)
    signal.signal(signal.SIGTERM, _term)

    httpd = serve(state, host=args.host, port=args.port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print(f"[apex] mode={mode} symbols={live.symbols_by_broker} strategy={live.strategy} "
          f"prop={live.prop_profile or 'none'}")
    print(f"[apex] dashboard on http://{args.host}:{args.port}")
    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        runner.shutdown()
        httpd.shutdown()
    return 0


def cmd_backtest(args) -> int:
    symbols = _split(args.symbols)
    broker = args.broker
    out = args.out or f"runs/bt_{int(time.time())}"
    risk = RiskConfig()
    if args.risk_pct:
        risk.risk_per_trade_pct = args.risk_pct
    req = BacktestRequest(
        symbols_by_broker=_symbol_map(broker, symbols),
        bars=args.bars, tf=args.tf, seed=args.seed,
        initial_balance=args.balance, strategy=args.strategy,
        prop_profile=_prop_arg(args.prop) if args.prop is not None else None,
        risk=risk, out_dir=out,
        trend_strength=args.trend_strength,
        record=not args.no_record,
    )
    print(f"[backtest] {req.symbols_by_broker} bars={req.bars} strategy={req.strategy} "
          f"prop={req.prop_profile or 'none'} seed={req.seed}")
    res = run_backtest(req)
    print(res.summary_line())
    m = res.metrics
    print(f"  PF {m['profit_factor']} · expectancy {m['expectancy']} · "
          f"Sharpe {m['sharpe']} · Sortino {m['sortino']} · maxDD {m['max_drawdown_pct']}%")
    if res.prop:
        p = res.prop
        print(f"  prop {p['profile']}: {p['verdict'].upper()} · profit {p['profit_pct']}% / "
              f"{p['target_pct']}% · daily used {p['daily_loss_used_pct']}% · "
              f"days {p['days_traded']}/{p['min_trading_days']}"
              + (f" · BREACH: {p['breach']}" if p["breach"] else ""))
    top = sorted(res.rejections.items(), key=lambda kv: -kv[1])[:6]
    if top:
        print("  rejections:", ", ".join(f"{k}={v}" for k, v in top))
    print(f"  artifacts -> {out}/report.md")
    return 0


def cmd_sweep(args) -> int:
    from .research.optimize import DEFAULT_GRID, SweepRequest, sweep
    symbols = _split(args.symbols)
    req = SweepRequest(
        symbols_by_broker=_symbol_map(args.broker, symbols),
        bars=args.bars, strategy=args.strategy,
        prop_profile=_prop_arg(args.prop) if args.prop is not None else None,
        seeds=list(range(1, args.seeds + 1)), trend_strength=args.trend_strength,
        max_combinations=args.combos, out_dir=args.out,
    )
    if args.grid:
        req.grid = json.loads(args.grid)
    else:
        req.grid = DEFAULT_GRID
    print(f"[sweep] {req.max_combinations} combos x {len(req.seeds)} seeds")
    out = sweep(req)
    print("\nTop configurations by risk-adjusted score:")
    for row in out["results"][:8]:
        print(f"  score {row['score']:>7.3f}  avgR {row['avg_r']:>+6.3f}  dd {row['max_dd']:>5.2f}% "
              f" trades {row['trades']:>5.1f}  pass {row['pass_rate']:>5.1f}%  {row['params']}")
    print(f"\nfull results -> {req.out_dir}/sweep.json")
    return 0


def cmd_propsim(args) -> int:
    from .research.optimize import PropSimRequest, prop_pass_sim
    symbols = _split(args.symbols)
    req = PropSimRequest(profile=args.profile, symbols_by_broker=_symbol_map(args.broker, symbols),
                         bars=args.bars, seeds=args.seeds, strategy=args.strategy,
                         initial_balance=args.balance, trend_strength=args.trend_strength,
                         out_dir=args.out)
    print(f"[propsim] {req.profile} over {req.seeds} seeds, {req.bars} bars each")
    out = prop_pass_sim(req)
    print(f"  pass {out['pass_rate_pct']}%  breach {out['breach_rate_pct']}%  "
          f"median profit {out['median_profit_pct']}%  median DD {out['median_dd_pct']}%")
    print(f"  verdicts: {out['verdicts']}")
    print(f"  -> {req.out_dir}/propsim_{req.profile}.json")
    return 0


def cmd_profiles(args) -> int:
    print(f"{'profile':<24}{'target':>8}{'daily':>8}{'total':>8}{'minDays':>9}{'consist':>9}{'risk%':>8}")
    for name, fn in PROFILES.items():
        p = fn()
        plan = plan_for(p, 10_000)
        print(f"{p.name:<24}{p.profit_target_pct:>7.1f}%{p.max_daily_loss_pct:>7.1f}%"
              f"{p.max_total_loss_pct:>7.1f}%{p.min_trading_days:>9}{p.consistency_pct:>8.0f}%"
              f"{plan['risk_per_trade_pct']:>8.2f}")
    print("\nSet your own rules in JSON (docs/PROP_FIRM_PLAYBOOK.md) and pass the file path.")
    return 0


def cmd_doctor(args) -> int:
    """Readiness matrix: config, credentials, broker adapters, data, storage."""
    cfg = load_config(args.config)
    creds = broker_credentials(cfg)
    rows: list[tuple[str, str, str]] = []

    rows.append(("config", "OK", args.config or "defaults"))
    rows.append(("python", "OK", sys.version.split()[0]))

    # deriv credentials
    if creds["deriv_token"]:
        rows.append(("deriv token", "OK", "present in environment"))
    else:
        rows.append(("deriv token", "MISSING", "set DERIV_TOKEN for live Deriv trading"))
    rows.append(("deriv app id", "OK", creds["deriv_app_id"]))
    try:
        from .data.ws import WebSocketClient                    # noqa: F401
        from .data.deriv import DerivAPI                        # noqa: F401
        rows.append(("deriv adapter", "OK", "websocket client + API loaded"))
    except Exception as exc:
        rows.append(("deriv adapter", "FAIL", str(exc)))

    # mt5
    try:
        import MetaTrader5  # type: ignore  # noqa: F401
        rows.append(("mt5 package", "OK", "MetaTrader5 importable"))
    except Exception:
        rows.append(("mt5 package", "N/A", "not installed (Windows only); paper mode unaffected"))
    if creds["mt5_login"]:
        rows.append(("mt5 credentials", "OK", f"login {creds['mt5_login']}"))
    else:
        rows.append(("mt5 credentials", "MISSING", "set MT5_LOGIN / MT5_PASSWORD / MT5_SERVER"))

    # storage
    try:
        os.makedirs("runs", exist_ok=True)
        probe = Store("runs/_doctor.db")
        probe.record_event("doctor", "probe")
        probe.close()
        os.remove("runs/_doctor.db")
        rows.append(("journal", "OK", "sqlite writable"))
    except Exception as exc:
        rows.append(("journal", "FAIL", str(exc)))

    # engine self-test: 400-bar backtest must run and produce decisions
    try:
        r = run_backtest(BacktestRequest(symbols_by_broker={"deriv": ["R_25"]}, bars=400,
                                         record=False, out_dir="runs/_doctor"))
        rows.append(("engine self-test", "OK",
                     f"{r.metrics['trades']} trades, {sum(r.rejections.values())} coded decisions"))
    except Exception as exc:
        rows.append(("engine self-test", "FAIL", f"{type(exc).__name__}: {exc}"))

    # prop profiles
    rows.append(("prop profiles", "OK", ", ".join(PROFILES)))

    width = max(len(r[0]) for r in rows)
    print(f"\nABUTRON QUANT doctor  (v{__version__})\n")
    for name, status, detail in rows:
        mark = {"OK": "\033[32m✓\033[0m", "MISSING": "\033[33m•\033[0m",
                "N/A": "\033[90m–\033[0m", "FAIL": "\033[31m✗\033[0m"}.get(status, status)
        print(f"  {mark} {name.ljust(width)}  {status:<8} {detail}")
    print("\nLive trading requires: token/credentials, --live flag, "
          "and a demo account that has produced broker-confirmed fills.\n")
    return 0


def cmd_fetch(args) -> int:
    from .data.deriv import bars_to_csv, fetch_candles_sync
    cfg = load_config(args.config)
    creds = broker_credentials(cfg)
    granularity = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600}.get(args.tf, 300)
    print(f"[fetch] {args.symbol} {args.tf} {args.bars} candles from Deriv…")
    bars = fetch_candles_sync(args.symbol, granularity, args.bars,
                              app_id=creds["deriv_app_id"], token=creds["deriv_token"])
    if not bars:
        print("  no data returned — check the symbol and network access")
        return 1
    out = args.out or f"data/{args.symbol}_{args.tf}.csv"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    bars_to_csv(bars, out)
    print(f"  {len(bars)} bars -> {out}")
    return 0


def cmd_report(args) -> int:
    path = os.path.join(args.run, "summary.json")
    if not os.path.exists(path):
        print(f"no summary at {path}")
        return 1
    with open(path, "r", encoding="utf-8") as fh:
        blob = json.load(fh)
    m = blob["metrics"]
    print(f"\n{args.run}")
    print(f"  trades {m['trades']} · win {m['win_rate']}% · pnl {m['pnl']} · PF {m['profit_factor']}")
    print(f"  avgR {m['avg_r']} · maxDD {m['max_drawdown_pct']}% · Sharpe {m['sharpe']}")
    if blob.get("prop"):
        p = blob["prop"]
        print(f"  prop {p['profile']}: {p['verdict']} ({p['profit_pct']}% of {p['target_pct']}%)")
    rejections = blob.get("rejections") or {}
    if rejections:
        print("  rejections:", ", ".join(f"{k}={v}" for k, v in
                                         sorted(rejections.items(), key=lambda kv: -kv[1])[:8]))
    md = os.path.join(args.run, "report.md")
    if os.path.exists(md):
        print(f"  full report: {md}")
    return 0


def cmd_ai(args) -> int:
    from .ai.reviewer import OllamaReviewer, ReviewerConfig
    r = OllamaReviewer(ReviewerConfig(url=args.url, model=args.model))
    ok, msg = r.health()
    print(f"ollama at {args.url}: {'reachable' if ok else 'unreachable'} ({msg})")
    if ok and args.test:
        out = r.review({"symbol": "R_75", "side": "BUY", "conviction": 0.42,
                        "regime": "trend_up", "notes": "EMA alignment, ADX 27"})
        print(json.dumps(out, indent=2))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="apex", description="ABUTRON QUANT scalping desk")
    p.add_argument("--version", action="version", version=f"apex {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--config", default=None, help="JSON config path")
        sp.add_argument("--symbols", default="R_25,R_50,R_75")
        sp.add_argument("--broker", default="deriv", choices=["deriv", "mt5", "sim", "both"])
        sp.add_argument("--tf", default="M5")
        sp.add_argument("--strategy", default="scalp_balanced",
                        choices=["scalp_fast", "scalp_balanced", "prop_conservative"])
        sp.add_argument("--prop", default=None, help="prop profile name or JSON path")
        sp.add_argument("--balance", type=float, default=10_000.0)
        sp.add_argument("--trend-strength", type=float, default=1.2,
                        help="synthetic trend persistence (validation only)")
        sp.add_argument("--out", default=None)

    sp = sub.add_parser("serve", help="run engine + dashboard")
    common(sp)
    sp.add_argument("--demo", action="store_true", help="paper mode with synthetic feed")
    sp.add_argument("--mode", default=None, choices=["paper", "deriv", "mt5"])
    sp.add_argument("--port", type=int, default=8000)
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--seed", type=int, default=11)
    sp.add_argument("--speed", type=float, default=3.0,
                    help="wall-clock seconds per bar in paper mode (demo pacing)")
    sp.add_argument("--preload", type=int, default=0,
                    help="paper mode: replay N bars at start so the desk opens with history")
    sp.add_argument("--db", default=None)
    sp.add_argument("--live", action="store_true", help="TRANSMIT real orders (requires credentials)")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("backtest", help="event-driven backtest with the production engine")
    common(sp)
    sp.add_argument("--bars", type=int, default=4000)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--risk-pct", type=float, default=None)
    sp.add_argument("--no-record", action="store_true")
    sp.set_defaults(func=cmd_backtest)

    sp = sub.add_parser("sweep", help="walk-forward parameter sweep")
    common(sp)
    sp.add_argument("--bars", type=int, default=2500)
    sp.add_argument("--seeds", type=int, default=3)
    sp.add_argument("--combos", type=int, default=48)
    sp.add_argument("--grid", default=None, help='JSON grid, e.g. \'{"sl_atr":[1.0,1.3]}\'')
    sp.set_defaults(func=cmd_sweep)

    sp = sub.add_parser("propsim", help="prop-challenge pass-rate simulation")
    common(sp)
    sp.add_argument("--bars", type=int, default=2500)
    sp.add_argument("--seeds", type=int, default=12)
    sp.add_argument("--profile", default="ftmo_2step")
    sp.set_defaults(func=cmd_propsim)

    sp = sub.add_parser("profiles", help="list prop-firm rule sets")
    sp.set_defaults(func=cmd_profiles)

    sp = sub.add_parser("doctor", help="readiness checks")
    sp.add_argument("--config", default=None)
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("fetch", help="download candles from Deriv")
    sp.add_argument("--config", default=None)
    sp.add_argument("--symbol", default="R_75")
    sp.add_argument("--tf", default="M5")
    sp.add_argument("--bars", type=int, default=3000)
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("report", help="summarise a backtest run")
    sp.add_argument("--run", default="runs/latest")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("ai", help="check the optional local LLM reviewer")
    sp.add_argument("--url", default="http://127.0.0.1:11434")
    sp.add_argument("--model", default="qwen2.5:1.5b")
    sp.add_argument("--test", action="store_true")
    sp.set_defaults(func=cmd_ai)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
