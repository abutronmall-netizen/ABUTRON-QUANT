"""`apex verify` — turn the demo gate into a command.

`docs/LIVE_DEPLOYMENT.md` §3 lists six things a human must eyeball before going
live, and §3b four more for crash safety. Eyeballing is how the original
platform "passed" validation. This module reads the journal the engine already
writes and answers each question mechanically.

Deliberately read-only: it never connects to a broker and never changes state.
Checks are graded `fail` (blocker), `warn` (look at it) or `info` (context), so a
paper run can be verified without pretending paper slippage is real.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from .store import Store

NON_REJECTIONS = {"allowed", "filled", "approved", "ENTERED", "HALTED",
                  "PROP_CONSISTENCY", "WEEKEND", "LATE_FILL"}
KNOWN_REJECTIONS = {
    "LOW_CONVICTION", "OVEREXTENDED", "NO_DIRECTION", "NO_STRUCTURE", "ARBITRATION",
    "SPREAD_TOO_WIDE", "HTF_OPPOSED", "LOT_BELOW_MIN", "PROP_DAY_BUDGET",
    "PROP_OPEN_RISK", "PROP_NEWS", "PROP_NO_STOP", "PROP_BREACHED", "KILL_SWITCH",
    "COOLDOWN", "MAX_TRADES_DAY", "SYMBOL_HELD", "SESSION_CLOSED", "BAD_SIZING",
    "MARGIN", "PENDING_FILL", "PENDING_CANCELLED", "PENDING_EXPIRED", "AI_VETO",
}


@dataclass
class VerifyConfig:
    db_path: str = "runs/live/apex.db"
    heartbeat_path: Optional[str] = None
    live: bool = False
    stale_after_s: float = 30.0
    since_s: Optional[float] = None
    slippage_bps_limit: float = 3.0
    pnl_tolerance: float = 0.05


def _check(name: str, ok: bool, detail: str, severity: str = "fail",
           data: Optional[dict] = None) -> dict:
    return {"name": name, "ok": bool(ok), "severity": severity,
            "detail": detail, "data": data or {}}


# --------------------------------------------------------------------------- #
def verify_run(cfg: Optional[VerifyConfig] = None,
               clock=time.time) -> dict:
    cfg = cfg or VerifyConfig()
    now = clock()
    since = cfg.since_s if cfg.since_s is not None else 0.0
    checks: list[dict] = []

    if not os.path.exists(cfg.db_path):
        return {"ok": False, "db": cfg.db_path,
                "checks": [_check("journal", False, f"no journal at {cfg.db_path}")],
                "counts": {}, "generated_ts": now}

    store = Store(cfg.db_path)
    try:
        checks.extend(_checks(store, cfg, since, now))
        counts = _counts(store, since)
        trades = store.trades(since=since)
        counts["trades"] = len(trades)
        counts["open_positions"] = len((_engine_state(store) or {}).get("positions") or [])
        counts["pending"] = len((_engine_state(store) or {}).get("pending") or [])
    finally:
        store.close()

    blocking = [c for c in checks if not c["ok"] and c["severity"] == "fail"]
    warnings = [c for c in checks if not c["ok"] and c["severity"] == "warn"]
    return {"ok": not blocking, "db": cfg.db_path, "checks": checks, "counts": counts,
            "failures": [c["name"] for c in blocking],
            "warnings": [c["name"] for c in warnings], "generated_ts": now}


def _engine_state(store: Store) -> Optional[dict]:
    try:
        return store.load_state("engine")
    except Exception:
        return None


def _orders(store: Store, since: float) -> list[dict]:
    rows = store.conn.execute(
        "SELECT * FROM orders WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for key in ("order_json", "ack_json"):
            try:
                d[key] = json.loads(d.get(key) or "{}")
            except ValueError:
                d[key] = {}
        out.append(d)
    return out


def _events(store: Store, since: float) -> list[dict]:
    rows = store.conn.execute(
        "SELECT * FROM events WHERE ts >= ? ORDER BY ts", (since,)).fetchall()
    return [dict(r) for r in rows]


def _counts(store: Store, since: float) -> dict:
    def one(sql: str, args=()) -> int:
        row = store.conn.execute(sql, args).fetchone()
        return int(row[0] or 0) if row else 0
    return {
        "orders": one("SELECT COUNT(*) FROM orders WHERE ts >= ?", (since,)),
        "filled_orders": one("SELECT COUNT(*) FROM orders WHERE ts >= ? AND ok=1", (since,)),
        "rejected_orders": one("SELECT COUNT(*) FROM orders WHERE ts >= ? AND ok=0", (since,)),
        "signals": one("SELECT COUNT(*) FROM signals WHERE ts >= ?", (since,)),
        "events": one("SELECT COUNT(*) FROM events WHERE ts >= ?", (since,)),
        "equity_points": one("SELECT COUNT(*) FROM equity WHERE ts >= ?", (since,)),
    }


# --------------------------------------------------------------------------- #
def _checks(store: Store, cfg: VerifyConfig, since: float, now: float) -> list[dict]:
    checks = []
    orders = _orders(store, since)
    events = _events(store, since)
    state = _engine_state(store) or {}
    filled = [o for o in orders if o["ok"]]
    trades = store.trades(since=since)
    positions = state.get("positions") or []
    pending = state.get("pending") or []

    # 1. fills exist and are accounted for -----------------------------------
    unaccounted = max(0, len(filled) - (len(trades) + len(positions) + len(pending)))
    if not filled:
        checks.append(_check("fills", False, "no filled orders in the journal "
                                              "(has the engine actually traded?)"))
    else:
        checks.append(_check(
            "fills", unaccounted == 0,
            f"{len(filled)} filled order(s), {len(trades)} closed trade(s), "
            f"{len(positions)} open, {len(pending)} pending"
            + (f" — {unaccounted} unaccounted fill(s)" if unaccounted else ""),
            data={"filled": len(filled), "trades": len(trades),
                  "open": len(positions), "pending": len(pending),
                  "unaccounted": unaccounted}))

    # 2. prices agree: systematic slippage bias ------------------------------
    # Reference price: the order's own limit price when it has one, otherwise the
    # engine's decision price for that symbol from a nearby signal row.
    ref = {}
    for row in store.conn.execute(
            "SELECT ts,symbol,price FROM signals WHERE ts >= ? AND price > 0 "
            "ORDER BY ts", (since,)).fetchall():
        ref.setdefault(row[1], []).append((float(row[0]), float(row[2])))
    slips = []
    for o in filled:
        req = float(o["order_json"].get("price") or 0.0)
        got = float(o["filled_price"] or 0.0)
        if req <= 0:
            for sts, spx in reversed(ref.get(o["symbol"], [])):
                if 0 <= float(o["ts"]) - sts <= 120:
                    req = spx
                    break
        if req <= 0 or got <= 0:
            continue
        adverse = (got - req) if str(o["side"]).upper().startswith("B") else (req - got)
        slips.append(adverse / req * 1e4)
    if len(slips) < 3:
        checks.append(_check("prices", True,
                             f"too few fills to judge slippage ({len(slips)})",
                             severity="info"))
    else:
        mean = sum(slips) / len(slips)
        var = sum((s - mean) ** 2 for s in slips) / max(1, len(slips) - 1)
        stderr = (var ** 0.5) / (len(slips) ** 0.5)
        biased = mean > cfg.slippage_bps_limit and mean > 2 * stderr
        checks.append(_check(
            "prices", not biased,
            f"mean adverse slippage {mean:+.2f} bps over {len(slips)} fill(s) "
            f"(±{stderr:.2f}, limit {cfg.slippage_bps_limit:.1f})"
            + ("" if cfg.live else " — paper fills execute at the touch, judge this on demo"),
            severity="fail" if cfg.live else "warn",
            data={"n": len(slips), "mean_bps": round(mean, 3),
                  "stderr_bps": round(stderr, 3), "biased": biased}))

    # 3. P&L agrees with the equity curve -----------------------------------
    equity = store.equity_curve(limit=100000)
    if not trades or len(equity) < 2:
        checks.append(_check("pnl", True, "not enough data for a P&L comparison",
                             severity="info"))
    else:
        pnl = sum(float(t["pnl"] or 0.0) for t in trades) - \
            sum(float(t["commission"] or 0.0) for t in trades)
        delta = float(equity[-1]["equity"]) - float(equity[0]["equity"])
        tol = max(1.0, abs(pnl) * cfg.pnl_tolerance)
        checks.append(_check(
            "pnl", abs(pnl - delta) <= tol,
            f"closed P&L {pnl:+.2f} vs equity change {delta:+.2f} "
            f"(tolerance ±{tol:.2f})" +
            ("" if cfg.live else " — a paper restart resets the simulated balance, "
                                 "so this only reconciles across a continuous account"),
            severity="fail" if cfg.live else "info",
            data={"pnl": round(pnl, 2), "equity_delta": round(delta, 2)}))

    # 4. rejections are typed and explainable --------------------------------
    stats = store.signal_stats(since=since)
    unknown = {k: v for k, v in stats.items()
               if k not in KNOWN_REJECTIONS and k not in NON_REJECTIONS}
    rejections = {k: v for k, v in stats.items() if k in KNOWN_REJECTIONS}
    if not stats:
        checks.append(_check("rejections", True, "no signals yet", severity="info"))
    else:
        checks.append(_check(
            "rejections", not unknown,
            f"{len(rejections)} rejection code(s), {sum(rejections.values())} rejection(s)"
            + (f"; unknown: {unknown}" if unknown else ""),
            data={"codes": rejections, "unknown": unknown}))

    # 5. reconciliation clean -------------------------------------------------
    reconcile_events = [e for e in events if e["kind"] == "reconcile"]
    diffs = [e for e in reconcile_events if "discrepanc" in (e["message"] or "").lower()]
    boot = [e for e in reconcile_events if "boot" in (e["message"] or "").lower()]
    checks.append(_check(
        "reconcile", not diffs,
        f"{len(boot)} boot sync(s), {len(diffs)} event(s) reporting discrepancies",
        data={"boots": len(boot), "diff_events": len(diffs)}))

    # 6. kill switch exercised ----------------------------------------------
    kills = [e for e in events if "kill switch" in (e["message"] or "").lower()]
    checks.append(_check(
        "killswitch", bool(kills),
        f"{len(kills)} kill-switch event(s); exercise it on demo before going live",
        severity="info" if not kills else "fail",
        data={"events": len(kills)}))

    # §3b: stops, pending age, heartbeat ------------------------------------
    unstops = [p for p in positions
               if not p.get("sl") and not (p.get("meta") or {}).get("local_stop")]
    checks.append(_check(
        "stops", not unstops,
        f"{len(positions)} open position(s), {len(unstops)} without a stop"
        + (f": {[p.get('symbol') for p in unstops]}" if unstops else ""),
        data={"open": len(positions), "unprotected": len(unstops)}))

    stale_pending = [p for p in pending if float(p.get("expires_ts") or 0.0) and
                     float(p["expires_ts"]) < now]
    checks.append(_check(
        "pending", not stale_pending,
        f"{len(pending)} tracked pending order(s), {len(stale_pending)} past expiry",
        severity="warn",
        data={"pending": len(pending), "stale": len(stale_pending)}))

    if cfg.heartbeat_path:
        from .watchdog import heartbeat_age
        age = heartbeat_age(cfg.heartbeat_path)
        if age is None:
            checks.append(_check("heartbeat", False,
                                 f"no readable heartbeat at {cfg.heartbeat_path}"))
        else:
            fresh = age <= cfg.stale_after_s
            checks.append(_check(
                "heartbeat", fresh,
                f"heartbeat {age:.1f}s old" +
                ("" if fresh else f" (stale > {cfg.stale_after_s:.0f}s)"),
                data={"age_s": round(age, 1)}))
    return checks


# --------------------------------------------------------------------------- #
def render_verify(report: dict) -> str:
    checks = report["checks"]
    passed = sum(1 for c in checks if c["ok"])
    warn = sum(1 for c in checks if not c["ok"] and c["severity"] == "warn")
    info = sum(1 for c in checks if not c["ok"] and c["severity"] == "info")
    tally = f"{passed}/{len(checks)} passed"
    if warn:
        tally += f" · {warn} warning(s)"
    if info:
        tally += f" · {info} informational"
    lines = [f"# apex verify — {os.path.basename(report.get('db', ''))}", "",
             f"**{'PASS' if report['ok'] else 'FAIL'}** · {tally}", ""]
    if report.get("failures"):
        lines += [f"Blockers: {', '.join(report['failures'])}", ""]
    if report.get("warnings"):
        lines += [f"Warnings: {', '.join(report['warnings'])}", ""]
    lines += ["| check | result | detail |", "| --- | --- | --- |"]
    icon = {True: "PASS", False: "FAIL"}
    for c in report["checks"]:
        mark = icon[c["ok"]]
        if not c["ok"] and c["severity"] in ("warn", "info"):
            mark = "WARN" if c["severity"] == "warn" else "INFO"
        lines.append(f"| `{c['name']}` | {mark} | {c['detail']} |")
    counts = report.get("counts") or {}
    if counts:
        lines += ["", "Counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))]
    return "\n".join(lines) + "\n"
