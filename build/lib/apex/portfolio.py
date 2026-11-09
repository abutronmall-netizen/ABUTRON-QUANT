"""Portfolio arbitration + adaptive agent weighting (ReflectionMemory).

This is the piece the audit said existed in source but never ran: regime-aware
agent weighting that learns from *realised* trade outcomes, and a portfolio
layer that decides which of several competing ideas actually reaches the broker.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import Candidate, Position, RegimeLabel, Side, TradeRecord, Vote

#: Prior reliability per agent per regime. 1.0 = neutral. Bootstrapped from the
#: structural logic of each agent (trend agents should not be trusted in chaos).
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    RegimeLabel.TREND_UP.value:    {"trend": 1.35, "meanrev": 0.55, "breakout": 1.00, "momentum": 1.15, "carry": 0.30, "volfilter": 1.00},
    RegimeLabel.TREND_DOWN.value:  {"trend": 1.35, "meanrev": 0.55, "breakout": 1.00, "momentum": 1.15, "carry": 0.30, "volfilter": 1.00},
    RegimeLabel.RANGE.value:       {"trend": 0.60, "meanrev": 1.35, "breakout": 0.45, "momentum": 0.90, "carry": 0.30, "volfilter": 1.00},
    RegimeLabel.SQUEEZE.value:     {"trend": 0.50, "meanrev": 0.90, "breakout": 1.20, "momentum": 0.80, "carry": 0.30, "volfilter": 0.80},
    RegimeLabel.EXPANSION.value:   {"trend": 1.10, "meanrev": 0.45, "breakout": 1.40, "momentum": 1.25, "carry": 0.30, "volfilter": 1.00},
    RegimeLabel.CHAOS.value:       {"trend": 0.45, "meanrev": 0.60, "breakout": 0.50, "momentum": 0.70, "carry": 0.20, "volfilter": 0.70},
}

#: Minimum conviction by regime. Chaos is deliberately near-impossible; that is a
#: risk decision, not a signal-starvation bug, and it is logged as such.
MIN_CONVICTION: dict[str, float] = {
    RegimeLabel.TREND_UP.value: 0.30,
    RegimeLabel.TREND_DOWN.value: 0.30,
    RegimeLabel.RANGE.value: 0.32,
    RegimeLabel.SQUEEZE.value: 0.34,
    RegimeLabel.EXPANSION.value: 0.30,
    RegimeLabel.CHAOS.value: 0.75,
}


def compute_conviction(votes: list[Vote], weights: dict[str, float]) -> tuple[float, Side | None, dict]:
    """Signed weighted aggregate -> (conviction 0..1, side or None, breakdown)."""
    signed = 0.0
    support = 0.0
    opposition = 0.0
    for v in votes:
        w = weights.get(v.agent, 1.0)
        contrib = w * v.score * (0.4 + 0.6 * v.confidence)
        signed += contrib
        if abs(v.score) < 1e-9:
            continue
        if v.score > 0:
            support += max(contrib, 0.0)
        else:
            opposition += max(-contrib, 0.0)
    if abs(signed) < 1e-9:
        return 0.0, None, {"signed": 0.0, "support": support, "opposition": opposition}
    side = Side.BUY if signed > 0 else Side.SELL
    directional = support if side is Side.BUY else opposition
    against = opposition if side is Side.BUY else support
    # Penalise disagreement super-linearly: contested setups are how prop accounts die.
    conv = directional * (1.0 / (1.0 + 2.2 * against)) * (1.0 / (1.0 + 0.25 * directional))
    conv = max(0.0, min(1.0, conv))
    return conv, side, {"signed": round(signed, 4), "support": round(support, 4),
                        "opposition": round(opposition, 4)}


class ReflectionMemory:
    """Scores agents on realised outcomes and adapts their weights.

    ewma_r is an exponentially weighted mean of R-multiples contributed by the
    agent, per regime. Weights follow exp(k * ewma_r) and are clipped, so a bad
    run demotes an agent without ever silencing the whole desk.
    """

    def __init__(self, path: Optional[str] = None, lr: float = 0.12, k: float = 0.55,
                 clip: tuple[float, float] = (0.2, 3.0)):
        self.path = path
        self.lr = lr
        self.k = k
        self.clip = clip
        self.weights: dict[str, dict[str, float]] = {k2: dict(v) for k2, v in DEFAULT_WEIGHTS.items()}
        self.stats: dict[str, dict[str, float]] = {}
        self.updates = 0
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            self.weights.update({k: dict(v) for k, v in blob.get("weights", {}).items()})
            self.stats = blob.get("stats", {})
            self.updates = blob.get("updates", 0)
        except (OSError, ValueError):
            pass  # corrupt memory must never stop trading decisions

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"weights": self.weights, "stats": self.stats, "updates": self.updates},
                      fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------ #
    def weights_for(self, regime: str) -> dict[str, float]:
        base = self.weights.get(regime) or self.weights.get(RegimeLabel.RANGE.value, {})
        return dict(base)

    def update(self, trade: TradeRecord) -> None:
        """Attribute the outcome to every agent that supported the direction."""
        regime = trade.regime or RegimeLabel.RANGE.value
        wr = self.weights.setdefault(regime, dict(DEFAULT_WEIGHTS[RegimeLabel.RANGE.value]))
        r = max(-2.5, min(2.5, trade.r_multiple))
        votes = trade.meta.get("votes") or []
        if not votes:
            return
        for v in votes:
            agent = v.get("agent")
            score = float(v.get("score", 0.0))
            if not agent or abs(score) < 0.05:
                continue
            agreement = 1.0 if (score > 0) == (trade.side is Side.BUY) else -1.0
            realised = agreement * r
            st = self.stats.setdefault(agent, {"n": 0.0, "ewma_r": 0.0, "sum_r": 0.0})
            st["n"] += 1
            st["ewma_r"] = st["ewma_r"] + self.lr * (realised - st["ewma_r"])
            st["sum_r"] += realised
            cur = wr.get(agent, 1.0)
            wr[agent] = max(self.clip[0], min(self.clip[1], cur * math.exp(self.k * self.lr * realised)))
        self.updates += 1
        self.save()

    def report(self) -> dict:
        agents = {a: {k2: round(v2, 4) for k2, v2 in st.items()} for a, st in self.stats.items()}
        return {"updates": self.updates,
                "agents": agents,
                "stats": agents,          # dashboard/API contract
                "weights": {r: {a: round(w, 3) for a, w in ws.items()}
                            for r, ws in self.weights.items()}}


@dataclass
class PortfolioLimits:
    max_open_positions: int = 4
    max_per_symbol: int = 1
    max_per_strategy: int = 3
    max_correlated: int = 2
    max_gross_risk_pct: float = 2.0


@dataclass
class ArbitrationResult:
    accepted: list[Candidate] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)


class PortfolioManager:
    """Turns many agent opinions into at most a handful of orders."""

    def __init__(self, min_conviction: Optional[dict[str, float]] = None,
                 limits: Optional[PortfolioLimits] = None):
        self.min_conviction = dict(MIN_CONVICTION)
        if min_conviction:
            self.min_conviction.update(min_conviction)
        self.limits = limits or PortfolioLimits()

    def conviction_gate(self, cand: Candidate) -> tuple[bool, str]:
        need = self.min_conviction.get(cand.regime, 0.32)
        if cand.conviction < need:
            return False, f"conviction {cand.conviction:.3f} < {need:.2f} ({cand.regime})"
        return True, "ok"

    def arbitrate(self, candidates: Iterable[Candidate], positions: list[Position],
                  correlation_groups: Optional[dict[str, str]] = None,
                  regime: Optional[str] = None) -> ArbitrationResult:
        res = ArbitrationResult()
        corr = correlation_groups or {}
        held_symbols = {p.symbol for p in positions}
        per_symbol: dict[str, int] = {}
        per_strategy: dict[str, int] = {}
        per_corr: dict[str, int] = {}
        for p in positions:
            per_symbol[p.symbol] = per_symbol.get(p.symbol, 0) + 1
            per_strategy[p.strategy] = per_strategy.get(p.strategy, 0) + 1
            g = corr.get(p.symbol, p.symbol)
            per_corr[g] = per_corr.get(g, 0) + 1

        pool = sorted(candidates, key=lambda c: c.conviction, reverse=True)
        for cand in pool:
            ok, why = self.conviction_gate(cand)
            if not ok:
                res.rejected.append((cand, why))
                continue
            if len(positions) + len(res.accepted) >= self.limits.max_open_positions:
                res.rejected.append((cand, "portfolio max_open_positions"))
                continue
            if cand.symbol in held_symbols or per_symbol.get(cand.symbol, 0) >= self.limits.max_per_symbol:
                res.rejected.append((cand, "symbol already held"))
                continue
            if per_strategy.get(cand.strategy, 0) >= self.limits.max_per_strategy:
                res.rejected.append((cand, "strategy position cap"))
                continue
            g = corr.get(cand.symbol, cand.symbol)
            if per_corr.get(g, 0) >= self.limits.max_correlated:
                res.rejected.append((cand, f"correlation group '{g}' cap"))
                continue
            per_symbol[cand.symbol] = per_symbol.get(cand.symbol, 0) + 1
            per_strategy[cand.strategy] = per_strategy.get(cand.strategy, 0) + 1
            per_corr[g] = per_corr.get(g, 0) + 1
            res.accepted.append(cand)
        return res
