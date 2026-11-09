"""Entry construction and in-trade management for quick scalping.

The engine is regime-driven (see regime.py) and agent-driven (agents.py); this
module turns a conviction into a *tradeable* structure: entry, stop, target,
time-stop and trailing rules, all expressed in ATR so they adapt to volatility
instead of using fixed pip distances that break on Deriv synthetics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .agents import MarketContext, build_agents
from .models import Candidate, ExitReason, Position, Side, SymbolSpec, Vote
from .portfolio import PortfolioManager, ReflectionMemory, compute_conviction


@dataclass
class StrategyProfile:
    name: str = "scalp_balanced"
    entry_tf: str = "M5"
    sl_atr: float = 1.0
    tp_atr: float = 1.8
    min_rr: float = 1.10
    max_spread_atr: float = 0.40
    trailing_atr: float = 1.0
    trail_after_r: float = 0.9
    breakeven_at_r: float = 0.6
    time_stop_bars: int = 8
    max_extension_atr: float = 2.2      # don't chase price this far past VWAP/EMA
    agents: list[str] = field(default_factory=list)   # empty -> all
    min_conviction: dict[str, float] = field(default_factory=dict)
    require_htf_not_opposed: bool = False

    @staticmethod
    def preset(name: str) -> "StrategyProfile":
        presets = {
            # fast and frequent: small targets, tight stops, short time stop
            "scalp_fast": StrategyProfile(
                name="scalp_fast", sl_atr=0.9, tp_atr=1.3, min_rr=1.05,
                trailing_atr=0.8, trail_after_r=0.7, breakeven_at_r=0.5,
                time_stop_bars=5, max_spread_atr=0.45,
            ),
            "scalp_balanced": StrategyProfile(name="scalp_balanced"),
            # prop-firm mode: fewer, cleaner, wider stops, no counter-trend fades in trends
            "prop_conservative": StrategyProfile(
                name="prop_conservative", sl_atr=1.25, tp_atr=2.0, min_rr=1.4,
                trailing_atr=1.2, trail_after_r=1.0, breakeven_at_r=0.8,
                time_stop_bars=10, max_spread_atr=0.30, max_extension_atr=1.6,
                min_conviction={"trend_up": 0.38, "trend_down": 0.38, "range": 0.42,
                                "squeeze": 0.45, "expansion": 0.38, "chaos": 1.1},
                require_htf_not_opposed=True,
            ),
        }
        if name not in presets:
            raise KeyError(f"unknown strategy profile '{name}'")
        return presets[name]


def _spread_ok(ctx: MarketContext, spec: SymbolSpec, cfg: StrategyProfile) -> tuple[bool, float]:
    atr = ctx.primary.atr_v or 0.0
    if ctx.quote is None or atr <= 0:
        return True, 0.0
    ratio = ctx.quote.spread / atr
    return ratio <= cfg.max_spread_atr, ratio


def build_candidate(ctx: MarketContext, spec: SymbolSpec, cfg: StrategyProfile,
                    weights: dict[str, float], memory: ReflectionMemory,
                    pm: PortfolioManager) -> tuple[Optional[Candidate], str, dict]:
    """Return (candidate, status_code, diagnostics).

    `status_code` is a stable machine-readable string. The dashboard counts these,
    so 'no trades today' is never unexplained again.
    """
    diag: dict = {"symbol": ctx.symbol, "regime": ctx.regime.label.value}
    m5 = ctx.primary
    if not m5.ready():
        return None, "WARMUP", diag
    if ctx.quote is None:
        return None, "NO_QUOTE", diag

    ok_spread, spread_ratio = _spread_ok(ctx, spec, cfg)
    diag["spread_atr"] = round(spread_ratio, 3)
    if not ok_spread:
        return None, "SPREAD_TOO_WIDE", diag

    agents = build_agents(cfg.agents or None)
    votes: list[Vote] = [a.vote(ctx) for a in agents]
    diag["votes"] = {v.agent: round(v.score, 3) for v in votes}
    weight_table = weights or memory.weights_for(ctx.regime.label.value)
    conv, side, breakdown = compute_conviction(votes, weight_table)
    diag.update(breakdown)
    diag["conviction"] = round(conv, 4)

    if side is None or conv <= 0:
        return None, "NO_DIRECTION", diag

    # anti-chase: never enter a move already extended far from its mean.
    # Measured against the slow EMA (fast-reacting mean), NOT session VWAP: on a
    # trending day distance-from-VWAP grows monotonically with time, which would
    # block every entry after the first hour (a subtler version of the original
    # "zero qualified candidates" bug).
    ext = abs(m5.close - m5.ef) / max(m5.atr_v, 1e-12)
    diag["extension_atr"] = round(ext, 2)
    if ext > cfg.max_extension_atr:
        return None, "OVEREXTENDED", diag

    if cfg.require_htf_not_opposed:
        for tf in ("M15", "H1"):
            s = ctx.tf(tf)
            if s is not None and s.trend_dir == -side.sign:
                return None, "HTF_OPPOSED", diag

    # Composition gate: timing agents (momentum) may not originate a trade on
    # their own. At least one structural agent (trend / mean-reversion /
    # breakout) must lean the same way. This is not a multi-timeframe AND gate:
    # a single structural agent at moderate strength is enough.
    structural = {v.agent: v.score for v in votes
                  if v.agent in ("trend", "meanrev", "breakout")}
    support = sum(score for score in structural.values() if score * side.sign > 0)
    diag["structural_support"] = round(support, 3)
    if support < 0.10:
        return None, "NO_STRUCTURE", diag

    need = dict(pm.min_conviction)
    need.update(cfg.min_conviction)
    threshold = need.get(ctx.regime.label.value, 0.32)
    diag["threshold"] = threshold
    if conv < threshold:
        return None, "LOW_CONVICTION", diag

    atr = m5.atr_v
    if atr <= 0:
        return None, "NO_ATR", diag

    # stop/target from ATR, validated against broker minimum stop distance
    entry = ctx.quote.ask if side is Side.BUY else ctx.quote.bid
    dist_sl = max(cfg.sl_atr * atr, spec.min_stop_distance * 1.05)
    dist_tp = max(cfg.tp_atr * atr, dist_sl * cfg.min_rr)
    sl = entry - side.sign * dist_sl
    tp = entry + side.sign * dist_tp
    sl, tp = spec.round_price(sl), spec.round_price(tp)

    r_dist = abs(entry - sl)
    if r_dist < spec.min_stop_distance:
        return None, "STOP_TOO_TIGHT", diag
    rr = abs(tp - entry) / r_dist
    if rr < cfg.min_rr:
        return None, "RR_TOO_LOW", diag

    cand = Candidate(
        symbol=ctx.symbol, side=side, conviction=conv, price=entry, sl=sl, tp=tp,
        ttl_s=cfg.time_stop_bars * _tf_seconds(cfg.entry_tf), strategy=cfg.name,
        regime=ctx.regime.label.value, votes=votes,
        meta={"atr": round(atr, 6), "rr": round(rr, 3), "spread": ctx.quote.spread,
              "extension_atr": round(ext, 2), "support": breakdown.get("support"),
              "opposition": breakdown.get("opposition"), "threshold": threshold},
    )
    return cand, "CANDIDATE", diag


def _tf_seconds(tf: str) -> int:
    return {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400}.get(tf, 300)


@dataclass
class ManageAction:
    exit_reason: Optional[ExitReason] = None
    new_sl: Optional[float] = None
    detail: str = ""


def manage_position(pos: Position, price: float, atr: float, bars_held: int,
                    cfg: StrategyProfile, spec: SymbolSpec,
                    flip_against: bool = False) -> ManageAction:
    """Decide whether to exit or tighten. Called on every quote (bid/ask mid)."""
    # hard bracket first
    hit = pos.hit(price)
    if hit is not None:
        return ManageAction(exit_reason=hit, detail="bracket hit")

    risk = abs(pos.entry - (pos.sl if pos.sl is not None else pos.entry))
    if risk <= 0 or atr <= 0:
        return ManageAction(detail="no risk reference")

    r_now = (price - pos.entry) * pos.side.sign / risk

    # time stop: scalps that do not work quickly are wrong, not "early"
    if bars_held >= cfg.time_stop_bars:
        return ManageAction(exit_reason=ExitReason.TIME_STOP, detail=f"{bars_held} bars, {r_now:+.2f}R")

    # signal flip: if the engine now leans hard the other way, cut
    if flip_against and r_now < 0.35:
        return ManageAction(exit_reason=ExitReason.SIGNAL_FLIP, detail=f"flip at {r_now:+.2f}R")

    # breakeven
    new_sl = None
    if r_now >= cfg.breakeven_at_r:
        be = spec.round_price(pos.entry)
        better = (pos.side is Side.BUY and (pos.sl is None or be > pos.sl)) or \
                 (pos.side is Side.SELL and (pos.sl is None or be < pos.sl))
        if better:
            new_sl = be

    # ATR trailing stop once the trade has paid for itself
    if r_now >= cfg.trail_after_r:
        trail = spec.round_price(price - pos.side.sign * cfg.trailing_atr * atr)
        better = (pos.side is Side.BUY and (pos.sl is None or trail > pos.sl)) or \
                 (pos.side is Side.SELL and (pos.sl is None or trail < pos.sl))
        if better:
            new_sl = trail

    return ManageAction(new_sl=new_sl, detail=f"{r_now:+.2f}R")
