"""Parameter search and prop-challenge evaluation.

Two jobs:

1. ``sweep`` — grid/random search over strategy parameters with a walk-forward
   split, so parameters are chosen on in-sample data and *reported* on unseen
   data. This is the only defence against the classic failure mode: a system
   tuned until its backtest looks good.

2. ``prop_pass_sim`` — run a challenge profile over many independent market
   seeds and report pass / breach / timeout rates plus the distribution of
   breaches. A single passing backtest is meaningless; the pass *rate* under
   variation is the honest measure of whether a rule set is survivable.
"""
from __future__ import annotations

import itertools
import json
import os
import random
import statistics
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Optional

from ..backtest import BacktestRequest, BacktestResult, run_backtest
from ..prop import PROFILES, get_profile
from ..risk import RiskConfig
from ..strategy import StrategyProfile


DEFAULT_GRID: dict[str, list] = {
    "sl_atr": [0.8, 1.0, 1.3],
    "tp_atr": [1.2, 1.6, 2.0],
    "time_stop_bars": [4, 6, 10],
    "trail_after_r": [0.7, 1.0, 99.0],
    "breakeven_at_r": [0.5, 0.9],
}


def _apply_params(base: StrategyProfile, params: dict[str, Any]) -> StrategyProfile:
    p = replace(base)
    for k, v in params.items():
        setattr(p, k, v)
    return p


def score_result(res: BacktestResult, prop_weight: float = 1.0) -> float:
    """Objective: risk-adjusted, drawdown-penalised, prop-aware.

    A challenge is failed by drawdown far more often than it is won by returns,
    so the score punishes maximum drawdown quadratically. It is deliberately not
    a pure P&L maximiser.
    """
    m = res.metrics
    if m["trades"] < 10:
        return -1e9
    dd = max(0.5, m["max_drawdown_pct"])
    base = m["avg_r"] * min(m["trades"], 120) ** 0.5
    score = base / (1.0 + (dd / 4.0) ** 2)
    if res.prop:
        v = res.prop["verdict"]
        if v == "breached":
            score -= 3.0
        elif v == "passed":
            score += 3.0
        elif v == "target_hit":
            score += 1.0
        score -= max(0.0, res.prop["daily_loss_used_pct"] / 100.0) * prop_weight
    return round(score, 4)


@dataclass
class SweepRequest:
    symbols_by_broker: dict[str, list[str]]
    bars: int = 2500
    strategy: str = "scalp_balanced"
    prop_profile: Optional[str] = None
    grid: dict[str, list] = field(default_factory=lambda: dict(DEFAULT_GRID))
    seeds: list[int] = field(default_factory=lambda: [1, 2, 3])
    trend_strength: float = 1.2
    max_combinations: int = 60
    out_dir: str = "runs/sweep"
    risk: RiskConfig = field(default_factory=RiskConfig)


def expand_grid(grid: dict[str, list], limit: int, seed: int = 7) -> list[dict]:
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))]
    if len(combos) > limit:
        random.Random(seed).shuffle(combos)
        combos = combos[:limit]
    return combos


def sweep(req: SweepRequest) -> dict:
    base = StrategyProfile.preset(req.strategy)
    combos = expand_grid(req.grid, req.max_combinations)
    rows = []
    for i, params in enumerate(combos, 1):
        prof = _apply_params(base, params)
        scores, results = [], []
        for seed in req.seeds:
            r = run_backtest(BacktestRequest(
                symbols_by_broker=req.symbols_by_broker, bars=req.bars, seed=seed,
                strategy=prof.name, strategy_profile=prof,
                prop_profile=req.prop_profile, record=False,
                out_dir=os.path.join(req.out_dir, "tmp"), trend_strength=req.trend_strength,
                risk=req.risk,
            ))
            scores.append(score_result(r))
            results.append(r)
        rows.append({
            "params": params,
            "score": round(statistics.mean(scores), 4),
            "score_stdev": round(statistics.pstdev(scores), 4) if len(scores) > 1 else 0.0,
            "avg_r": round(statistics.mean([r.metrics["avg_r"] for r in results]), 4),
            "pnl": round(statistics.mean([r.metrics["pnl"] for r in results]), 2),
            "max_dd": round(max(r.metrics["max_drawdown_pct"] for r in results), 2),
            "trades": round(statistics.mean([r.metrics["trades"] for r in results]), 1),
            "pass_rate": round(sum(1 for r in results if r.prop and r.prop["verdict"] in ("passed", "target_hit"))
                               / max(1, len(results)) * 100, 1),
        })
        if i % 10 == 0:
            print(f"  {i}/{len(combos)} combinations…", flush=True)

    rows.sort(key=lambda r: -r["score"])
    out = {"generated": time.time(), "request": {
        "symbols": req.symbols_by_broker, "bars": req.bars, "seeds": req.seeds,
        "trend_strength": req.trend_strength, "prop": req.prop_profile},
        "combinations": len(combos), "results": rows}
    os.makedirs(req.out_dir, exist_ok=True)
    with open(os.path.join(req.out_dir, "sweep.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    return out



# --------------------------------------------------------------------------- #
@dataclass
class PropSimRequest:
    profile: str = "ftmo_2step"
    symbols_by_broker: dict[str, list[str]] = field(
        default_factory=lambda: {"deriv": ["R_25", "R_50", "R_75"]})
    bars: int = 2500
    seeds: int = 12
    strategy: str = "prop_conservative"
    initial_balance: float = 10_000.0
    trend_strength: float = 1.2
    out_dir: str = "runs/propsim"


def prop_pass_sim(req: PropSimRequest) -> dict:
    outcomes = []
    for seed in range(1, req.seeds + 1):
        r = run_backtest(BacktestRequest(
            symbols_by_broker=req.symbols_by_broker, bars=req.bars, seed=seed,
            strategy=req.strategy, prop_profile=req.profile, record=False,
            out_dir=os.path.join(req.out_dir, "tmp"), initial_balance=req.initial_balance,
            trend_strength=req.trend_strength,
        ))
        p = r.prop or {}
        outcomes.append({
            "seed": seed, "verdict": p.get("verdict", "none"),
            "profit_pct": p.get("profit_pct", 0.0),
            "max_dd_pct": r.metrics["max_drawdown_pct"],
            "daily_loss_used_pct": p.get("daily_loss_used_pct", 0.0),
            "days_traded": p.get("days_traded", 0),
            "trades": r.metrics["trades"],
            "breach": p.get("breach", ""),
        })
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o["verdict"]] = counts.get(o["verdict"], 0) + 1
    profile = get_profile(req.profile)
    summary = {
        "profile": profile.name,
        "runs": len(outcomes),
        "pass_rate_pct": round(counts.get("passed", 0) / len(outcomes) * 100, 1),
        "breach_rate_pct": round(counts.get("breached", 0) / len(outcomes) * 100, 1),
        "verdicts": counts,
        "median_profit_pct": round(statistics.median([o["profit_pct"] for o in outcomes]), 2),
        "median_dd_pct": round(statistics.median([o["max_dd_pct"] for o in outcomes]), 2),
        "outcomes": outcomes,
        "generated": time.time(),
        "caveat": ("Synthetic markets. A pass rate here measures rule survival, "
                   "not the probability of passing any real challenge."),
    }
    os.makedirs(req.out_dir, exist_ok=True)
    with open(os.path.join(req.out_dir, f"propsim_{profile.name}.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    return summary
