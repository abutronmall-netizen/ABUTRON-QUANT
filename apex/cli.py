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
from .timeutil import tf_seconds

_tf_seconds = tf_seconds   # backwards-compatible alias


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
        preload_bars=args.preload, bar_seconds=args.speed,
        trend_strength=args.trend_strength or cfg["trend_strength"],
        risk=risk_from(cfg),
        boot_policy=(args.boot_policy if args.boot_policy != "adopt"
                     else cfg.get("boot_policy", "adopt")),
        persist_state=(not args.no_persist) and cfg.get("persist_state", True),
        news_calendar=args.news or cfg.get("news_calendar", ""),
        record_ticks=args.record_ticks or cfg.get("record_ticks", ""),
        ai_review=bool(args.ai and args.ai != "off") or bool(cfg.get("ai", {}).get("enabled")),
        ai_mode=(args.ai if args.ai in ("advisory", "veto") else cfg.get("ai", {}).get("mode", "advisory")),
        ai_url=cfg.get("ai", {}).get("url", "http://127.0.0.1:11434"),
        ai_model=cfg.get("ai", {}).get("model", "qwen2.5:1.5b"),
        heartbeat_path=(args.heartbeat if args.heartbeat is not None
                        else (cfg.get("heartbeat_path") or
                              (os.path.join(os.path.dirname(cfg["store_path"] or "runs/live/x"),
                                            "heartbeat.json")))),
        db_retention_days=(args.db_retention_days if args.db_retention_days is not None
                           else cfg.get("db_retention_days", 0.0)),
        alerts={**(cfg.get("alerts") or {}),
                **({"webhook_url": args.alert_webhook} if args.alert_webhook else {})},
    )
    if mode != "paper" and not args.live:
        print("[!] live-orders OFF: the engine will simulate fills and will not transmit orders.")
    runner = LiveRunner(live)
    runner.warmup()
    runner.start_background()

    token = args.token if args.token is not None else os.environ.get("APEX_DASHBOARD_TOKEN", "")
    state = DashboardState(runner, token=token)
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
    print(f"[apex] dashboard on http://{args.host}:{args.port}"
          + ("  (control endpoints require the operator token)" if token else ""))
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
        prefer_cached=not args.synthetic_only,
        tick_path=args.ticks or "",
        news_calendar=args.news or "",
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

    # restart safety surface
    try:
        st = Store("runs/_doctor.db")
        st.save_state("doctor", {"ok": True})
        ok = st.load_state("doctor") == {"ok": True}
        st.close()
        rows.append(("state store", "OK" if ok else "FAIL",
                     "risk/prop state survives restart" if ok else "round-trip failed"))
    except Exception as exc:
        rows.append(("state store", "FAIL", f"{type(exc).__name__}: {exc}"))
    cal = cfg.get("news_calendar", "")
    if cal:
        try:
            from .news import NewsCalendar
            n = len(NewsCalendar.load(cal))
            rows.append(("news calendar", "OK", f"{n} events in {cal}"))
        except Exception as exc:
            rows.append(("news calendar", "FAIL", f"{cal}: {exc}"))
    else:
        rows.append(("news calendar", "N/A", "no --news file: PROP_NEWS stays inert"))
    token = os.environ.get("APEX_DASHBOARD_TOKEN", "")
    rows.append(("dashboard token", "OK" if token else "MISSING",
                 "operator token set" if token else "set APEX_DASHBOARD_TOKEN before exposing the UI"))
    rows.append(("boot policy", "OK", cfg.get("boot_policy", "adopt")
                 + (" · state persisted" if cfg.get("persist_state", True) else " · NOT persisted")))

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
    """Download candles to data/<SYMBOL>_<TF>.csv (Deriv or FP Markets MT5)."""
    from .data.deriv import bars_to_csv, fetch_candles_sync
    cfg = load_config(args.config)
    creds = broker_credentials(cfg)
    out = args.out or f"data/{args.symbol}_{args.tf}.csv"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if args.broker == "mt5":
        from .data.mt5 import MT5Config, MT5Connection
        conn = MT5Connection(MT5Config(login=creds["mt5_login"], password=creds["mt5_password"],
                                       server=creds["mt5_server"], path=creds["mt5_path"]))
        try:
            conn.connect()
        except Exception as exc:
            print(f"[fetch] MT5 unavailable: {exc}")
            return 2
        bars = conn.rates(args.symbol, args.tf, args.bars)
        print(f"[fetch] mt5 offset {conn.offset_hours}h (server -> UTC)")
    else:
        bars = fetch_candles_sync(args.symbol, tf_seconds(args.tf), args.bars,
                                  app_id=creds["deriv_app_id"], token=creds["deriv_token"])
    bars_to_csv(bars, out)
    span = (bars[-1].ts - bars[0].ts) / 86400 if bars else 0
    print(f"[fetch] {len(bars)} bars {args.symbol} {args.tf} ({span:.1f} days) -> {out}")
    return 0


def cmd_news(args) -> int:
    from .news import NewsCalendar
    cal = NewsCalendar.load(args.file)
    now = time.time()
    up = [e for e in cal.events if now - args.minutes * 60 <= e.ts <= now + args.days * 86400]
    print(f"{len(cal)} events in {args.file}; {len(up)} in the next {args.days} day(s)\n")
    for e in up:
        print(f"  {time.strftime('%Y-%m-%d %H:%M', time.gmtime(e.ts))}Z  "
              f"{e.currency:>4} {e.impact:<6} {e.title}")
    wins = cal.windows(now, now + args.days * 86400, minutes=args.minutes)
    print(f"\n{len(wins)} blackout window(s) of +/-{args.minutes} min (high impact only)")
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


def cmd_verify(args) -> int:
    from .verify import VerifyConfig, render_verify, verify_run
    cfg = VerifyConfig(db_path=args.db or os.path.join(args.run, "apex.db"),
                       heartbeat_path=args.heartbeat, live=bool(args.live),
                       stale_after_s=args.stale,
                       since_s=(time.time() - args.days * 86400) if args.days else None,
                       slippage_bps_limit=args.slippage_bps)
    report = verify_run(cfg)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_verify(report))
    if not report["ok"]:
        return 1
    return 1 if (args.strict and report["warnings"]) else 0


def cmd_prune(args) -> int:
    db = args.db or os.path.join(args.run, "apex.db")
    if not os.path.exists(db):
        print(f"no journal at {db}")
        return 1
    store = Store(db)
    try:
        before = store.size_bytes()
        out = store.prune(args.days, keep_trades=not args.drop_trades)
        vac = store.vacuum() if not args.no_vacuum else {"reclaimed": 0}
        after = store.size_bytes()
        print(f"{db}: {before/1e6:.2f} MB -> {after/1e6:.2f} MB "
              f"(reclaimed {max(0, before - after)/1e6:.2f} MB)")
        print(f"  pruned {out}")
        if not args.no_vacuum and not vac.get("ok", True):
            print(f"  vacuum failed: {vac.get('error')}")
    finally:
        store.close()
    return 0


def cmd_watchdog(args) -> int:
    from .watchdog import Watchdog, WatchdogConfig, broker_flattener
    raw = load_config(args.config)
    creds = broker_credentials(raw)
    live_cfg = LiveConfig(
        mode=args.mode if args.mode != "none" else (raw.get("mode") or "paper"),
        symbols_by_broker=raw.get("symbols_by_broker") or {},
        live_orders=bool(args.live),
        deriv_app_id=creds["deriv_app_id"], deriv_token=creds["deriv_token"],
        mt5_login=creds["mt5_login"], mt5_password=creds["mt5_password"],
        mt5_server=creds["mt5_server"], mt5_path=creds["mt5_path"],
    )
    alerts = dict(raw.get("alerts") or {})
    if args.webhook:
        alerts["webhook_url"] = args.webhook
    flattener = None
    if not args.no_flatten and args.live and args.mode in ("deriv", "mt5"):
        flattener = broker_flattener(live_cfg)
        if flattener is None:
            print("watchdog: could not connect for flattening; alert-only")
    elif not args.no_flatten and args.mode in ("deriv", "mt5") and not args.live:
        print("watchdog: --live not set, so flattening is disabled (alert-only)")
    store = Store(args.db or os.path.join(args.run, "apex.db"))
    wd = Watchdog(WatchdogConfig(heartbeat_path=args.heartbeat,
                                 stale_after_s=args.stale,
                                 check_interval_s=args.interval,
                                 flatten_on_stale=not args.no_flatten,
                                 alerts=alerts),
                  store=store, flattener=flattener)
    try:
        if args.once:
            out = wd.check()
            print(json.dumps(out, indent=2, default=str))
            return 0 if not out["stale"] else 2
        print(f"watchdog: watching {args.heartbeat} every {args.interval:g}s "
              f"(stale > {args.stale:g}s, flatten={'off' if args.no_flatten else 'on'})")
        wd.run()
    except KeyboardInterrupt:
        print("watchdog: stopped")
    finally:
        store.close()
    return 0


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
    sp.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 only behind a proxy with auth")
    sp.add_argument("--seed", type=int, default=11)
    sp.add_argument("--speed", type=float, default=3.0,
                    help="wall-clock seconds per bar in paper mode (demo pacing)")
    sp.add_argument("--preload", type=int, default=0,
                    help="paper mode: replay N bars at start so the desk opens with history")
    sp.add_argument("--db", default=None)
    sp.add_argument("--live", action="store_true", help="TRANSMIT real orders (requires credentials)")
    sp.add_argument("--token", default=None,
                    help="operator token for control endpoints (default: $APEX_DASHBOARD_TOKEN)")
    sp.add_argument("--news", default=None, help="JSON economic calendar for prop blackout windows")
    sp.add_argument("--boot-policy", default="adopt", choices=["adopt", "flatten", "ignore"],
                    help="what to do with positions the broker already holds at startup")
    sp.add_argument("--record-ticks", default=None,
                    help="append live quotes to a CSV for tick-level replay")
    sp.add_argument("--no-persist", action="store_true",
                    help="do not restore risk/prop state from the journal on restart")
    sp.add_argument("--ai", default=None, choices=["off", "advisory", "veto"],
                    help="optional local LLM review of sized entries (default: config)")
    sp.add_argument("--heartbeat", default=None,
                    help="write a heartbeat file here (default: next to the journal)")
    sp.add_argument("--alert-webhook", default=None, help="POST alerts to this URL")
    sp.add_argument("--db-retention-days", type=float, default=None,
                    help="prune equity/signal/event rows older than N days at startup")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("backtest", help="event-driven backtest with the production engine")
    common(sp)
    sp.add_argument("--bars", type=int, default=4000)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--risk-pct", type=float, default=None)
    sp.add_argument("--synthetic-only", action="store_true",
                    help="ignore cached candles in data/ and use the synthetic tape")
    sp.add_argument("--no-record", action="store_true")
    sp.add_argument("--ticks", default=None,
                    help="replay recorded ticks (ts,symbol,bid,ask) instead of synthetic bars")
    sp.add_argument("--news", default=None, help="JSON economic calendar for prop blackout windows")
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

    sp = sub.add_parser("fetch", help="download candles (Deriv or FP Markets MT5)")
    sp.add_argument("--config", default=None)
    sp.add_argument("--broker", default="deriv", choices=["deriv", "mt5"])
    sp.add_argument("--symbol", default="R_75")
    sp.add_argument("--tf", default="M5")
    sp.add_argument("--bars", type=int, default=3000)
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("news", help="inspect an economic calendar and its blackout windows")
    sp.add_argument("--file", required=True, help="JSON calendar path")
    sp.add_argument("--days", type=int, default=3)
    sp.add_argument("--minutes", type=int, default=2)
    sp.set_defaults(func=cmd_news)

    sp = sub.add_parser("report", help="summarise a backtest run")
    sp.add_argument("--run", default="runs/latest")
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("ai", help="check the optional local LLM reviewer")
    sp.add_argument("--url", default="http://127.0.0.1:11434")
    sp.add_argument("--model", default="qwen2.5:1.5b")
    sp.add_argument("--test", action="store_true")
    sp.set_defaults(func=cmd_ai)

    sp = sub.add_parser("prune", help="bound journal growth (retention + vacuum)")
    sp.add_argument("--run", default="runs/live")
    sp.add_argument("--db", default=None)
    sp.add_argument("--days", type=float, default=14.0, help="retain this many days")
    sp.add_argument("--drop-trades", action="store_true", help="also delete old trades")
    sp.add_argument("--no-vacuum", action="store_true")
    sp.set_defaults(func=cmd_prune)

    sp = sub.add_parser("verify", help="automate the demo-validation checklist from a journal")
    sp.add_argument("--run", default="runs/live")
    sp.add_argument("--db", default=None, help="journal path (default: <run>/apex.db)")
    sp.add_argument("--heartbeat", default=None, help="heartbeat file to check for freshness")
    sp.add_argument("--days", type=float, default=0.0, help="only inspect the last N days")
    sp.add_argument("--live", action="store_true",
                    help="judge slippage/P&L strictly (real fills, not paper)")
    sp.add_argument("--slippage-bps", type=float, default=3.0)
    sp.add_argument("--stale", type=float, default=30.0)
    sp.add_argument("--strict", action="store_true", help="fail on warnings too")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("watchdog", help="watch the engine heartbeat, alert and flatten if it dies")
    sp.add_argument("--run", default="runs/live")
    sp.add_argument("--db", default=None)
    sp.add_argument("--heartbeat", default="runs/live/heartbeat.json")
    sp.add_argument("--mode", default="none", choices=["none", "deriv", "mt5", "sim"])
    sp.add_argument("--config", default=None, help="config supplying broker credentials")
    sp.add_argument("--stale", type=float, default=30.0)
    sp.add_argument("--interval", type=float, default=5.0)
    sp.add_argument("--webhook", default=None, help="alert webhook URL")
    sp.add_argument("--live", action="store_true",
                    help="allow the watchdog to close positions at the broker")
    sp.add_argument("--no-flatten", action="store_true", help="alert only, never touch the broker")
    sp.add_argument("--once", action="store_true", help="single check, exit 2 if stale")
    sp.set_defaults(func=cmd_watchdog)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
