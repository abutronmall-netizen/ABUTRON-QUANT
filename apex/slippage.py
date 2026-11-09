"""Slippage study — what the venue did to the price the engine asked for.

This is the question a demo week is supposed to answer: the backtest models a
constant cost, the venue charges a real one. Both numbers are already in the
journal (`orders.requested_price` from v2.3.1 and `orders.filled_price` from the
ack), so the study is arithmetic over data the desk already has.

Deliberately read-only: the file is opened with sqlite's `mode=ro`, so grading a
live journal can never create a table, migrate a column, or checkpoint a WAL out
from under the engine.

Sign convention: **adverse is positive**. A buy filled above its requested price
is `+x` bps; a sell filled below is also `+x` bps. A negative number means a
fortunate fill, which happens and is not evidence of skill.

The R-cost column is the number that matters for sizing: `bps / 10_000` of the
price, divided by the stop distance, is the fraction of one R paid to the venue.
At a 1.0 ATR stop and ~6 bps M5 ATR, 2 bps of slippage is a third of an R — the
reason the README refuses to publish a profit figure.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

#: The backtest's own cost model (`BacktestRequest.slippage_bps` and
#: `stop_slippage_bps`). A study is only useful as a comparison against it.
MODELLED_ENTRY_BPS = 0.3
MODELLED_STOP_BPS = 1.0


@dataclass
class SlippageConfig:
    db_path: str = "runs/live/apex.db"
    since_s: Optional[float] = None
    modelled_bps: float = MODELLED_ENTRY_BPS
    #: below this many fills, a median is noise and the report says so
    min_fills: int = 5


def _connect_ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise FileNotFoundError(f"no journal at {path}")
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _loads(raw: Any) -> dict:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _reference_prices(conn: sqlite3.Connection, since: float) -> dict[str, list[tuple[float, float]]]:
    """Decision mid per symbol, used only when an order carries no price."""
    out: dict[str, list[tuple[float, float]]] = {}
    try:
        rows = conn.execute(
            "SELECT ts,symbol,price FROM signals WHERE ts >= ? AND price > 0 ORDER BY ts",
            (since,)).fetchall()
    except sqlite3.DatabaseError:
        return out
    for r in rows:
        out.setdefault(str(r["symbol"]), []).append((float(r["ts"]), float(r["price"])))
    return out


def _fills(conn: sqlite3.Connection, since: float) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM orders WHERE ts >= ? AND ok = 1 ORDER BY ts", (since,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["order_json"] = _loads(d.get("order_json"))
        out.append(d)
    return out


def _entry_observation(o: dict, ref: dict[str, list[tuple[float, float]]]) -> Optional[dict]:
    requested = float(o.get("requested_price") or 0.0) or float(o["order_json"].get("price") or 0.0)
    filled = float(o.get("filled_price") or 0.0)
    if requested <= 0:
        # 2.3.x journals wrote the request into order_json; older ones only have
        # the decision mid in a signal row a moment earlier.
        for sts, price in reversed(ref.get(str(o.get("symbol")), [])):
            if 0 <= float(o.get("ts") or 0) - sts <= 120:
                requested = price
                break
    if requested <= 0 or filled <= 0:
        return None
    side = str(o.get("side") or "").upper()
    adverse = (filled - requested) if side.startswith("B") else (requested - filled)
    bps = adverse / requested * 1e4

    # R cost: how much of the stop distance the fill move consumed.
    stop = float(o["order_json"].get("sl") or 0.0)
    risk_px = abs(requested - stop) if stop > 0 else 0.0
    r_cost = (bps / 1e4 * requested / risk_px) if risk_px > 0 else None
    return {
        "ts": float(o.get("ts") or 0.0),
        "symbol": str(o.get("symbol") or ""),
        "broker": str(o.get("broker") or ""),
        "side": "BUY" if side.startswith("B") else "SELL",
        "requested": requested,
        "filled": filled,
        "bps": bps,
        "r_cost": r_cost,
    }


def _summary(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "mean_bps": 0.0, "median_bps": 0.0, "p25_bps": 0.0, "p75_bps": 0.0,
                "worst_bps": 0.0, "best_bps": 0.0, "worse_than_modelled_pct": 0.0,
                "mean_r_cost": None, "total_r_cost": None}
    series = sorted(r["bps"] for r in rows)
    rs = [r["r_cost"] for r in rows if r["r_cost"] is not None]
    return {
        "n": len(rows),
        "mean_bps": round(sum(series) / len(series), 3),
        "median_bps": round(_quantile(series, 0.5), 3),
        "p25_bps": round(_quantile(series, 0.25), 3),
        "p75_bps": round(_quantile(series, 0.75), 3),
        "worst_bps": round(series[-1], 3),
        "best_bps": round(series[0], 3),
        "worse_than_modelled_pct": round(
            100.0 * sum(1 for s in series if s > MODELLED_ENTRY_BPS) / len(series), 1),
        "mean_r_cost": round(sum(rs) / len(rs), 4) if rs else None,
        "total_r_cost": round(sum(rs), 4) if rs else None,
    }


def _group(rows: list[dict], key: str) -> dict[str, dict]:
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row[key] or "n/a", []).append(row)
    return {name: _summary(bucket) for name, bucket in sorted(buckets.items())}


def study(cfg: Optional[SlippageConfig] = None) -> dict:
    """Measure adverse fill slippage against the backtest's cost model."""
    cfg = cfg or SlippageConfig()
    out: dict[str, Any] = {
        "db": cfg.db_path,
        "modelled_bps": cfg.modelled_bps,
        "modelled_stop_bps": MODELLED_STOP_BPS,
        "generated_ts": None,
        "fills": 0,
        "verdict": "no data",
        "detail": "",
    }
    if not os.path.exists(cfg.db_path):
        out["detail"] = f"no journal at {cfg.db_path}"
        return out

    import time as _time
    out["generated_ts"] = _time.time()
    since = cfg.since_s if cfg.since_s is not None else 0.0
    try:
        conn = _connect_ro(cfg.db_path)
    except (sqlite3.DatabaseError, FileNotFoundError) as exc:
        out["detail"] = f"cannot read journal: {exc}"
        return out
    try:
        ref = _reference_prices(conn, since)
        observations = [obs for obs in (_entry_observation(o, ref) for o in _fills(conn, since))
                        if obs is not None]
    finally:
        conn.close()

    overall = _summary(observations)
    out["fills"] = overall["n"]
    out["overall"] = overall
    out["by_symbol"] = _group(observations, "symbol")
    out["by_broker"] = _group(observations, "broker")
    out["by_side"] = _group(observations, "side")
    out["recent"] = sorted(observations, key=lambda r: -r["ts"])[:25]

    if overall["n"] == 0:
        out["verdict"] = "no fills"
        out["detail"] = "no filled order carries both a requested and a filled price yet"
        return out
    if overall["n"] < cfg.min_fills:
        out["verdict"] = "insufficient sample"
        out["detail"] = (f"{overall['n']} fill(s) — a median over fewer than "
                         f"{cfg.min_fills} is noise, not a measurement")
        return out

    median = overall["median_bps"]
    ratio = (median / cfg.modelled_bps) if cfg.modelled_bps > 0 else float("inf")
    under = median > cfg.modelled_bps
    out["verdict"] = "under-modelled" if under else "inside model"
    r_cost = overall["mean_r_cost"]
    out["detail"] = (
        f"median adverse {median:+.2f} bps over {overall['n']} fill(s); "
        f"the backtest assumes {cfg.modelled_bps:.2f} bps "
        f"({ratio:.1f}x{' higher' if under else ' — measured is better'})"
        + (f"; costs {r_cost:.3f}R per trade at the recorded stops" if r_cost is not None else "")
    )
    return out


def as_text(report: dict) -> str:
    """The human report: one block an operator can paste into a journal."""
    lines = [
        f"slippage study — {report.get('db')}",
        f"  fills measured : {report.get('fills', 0)}",
        f"  verdict        : {report.get('verdict')}",
        f"  detail         : {report.get('detail')}",
    ]
    overall = report.get("overall") or {}
    if overall.get("n"):
        lines += [
            f"  adverse bps    : mean {overall['mean_bps']:+.2f} · median {overall['median_bps']:+.2f}"
            f" · IQR {overall['p25_bps']:+.2f}…{overall['p75_bps']:+.2f}"
            f" · worst {overall['worst_bps']:+.2f}",
            f"  worse than model: {overall['worse_than_modelled_pct']:.1f}% of fills",
        ]
        if overall.get("mean_r_cost") is not None:
            lines.append(f"  cost in R      : {overall['mean_r_cost']:+.3f}R per trade"
                         f" (total {overall['total_r_cost']:+.3f}R)")
    for label, key in (("by symbol", "by_symbol"), ("by broker", "by_broker"),
                       ("by side", "by_side")):
        groups = report.get(key) or {}
        if groups:
            lines.append(f"  {label}:")
            for name, s in groups.items():
                lines.append(f"    {name:<10} n={s['n']:<4} median {s['median_bps']:+.2f} bps")
    return "\n".join(lines)
