"""Signal agents.

Each agent looks at the market through one lens and returns a signed Vote in
[-1, +1]. Agents never place orders. They are advisory, and their historical
reliability is re-weighted by ReflectionMemory (see portfolio.py).

Design note — the "zero qualified candidates" defect:
The audited scanner required M5, M15, H1 and H4 to all agree before emitting an
entry. In practice that is a near-impossible conjunction, so the router shipped
0 trades while the platform "looked healthy". Here agreement is a *score*
(conviction), not a boolean gate: agents contribute partial credit, opposition
reduces conviction, and the threshold is configurable and regime-aware. Every
rejection is recorded with a machine-readable reason so a dry spell is always
explainable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .indicators import IndicatorSet
from .models import Quote, Regime, RegimeLabel, Vote


@dataclass(slots=True)
class MarketContext:
    symbol: str
    quote: Optional[Quote]
    bars: dict[str, IndicatorSet]          # "M5", "M15", "H1"
    regime: Regime
    entry_tf: str = "M5"
    now: float = 0.0

    @property
    def primary(self) -> IndicatorSet:
        return self.bars[self.entry_tf]

    def tf(self, name: str) -> Optional[IndicatorSet]:
        s = self.bars.get(name)
        return s if s is not None and s.ready() else None


def _clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _soft(x: float, lo: float, hi: float) -> float:
    """Linear 0..1 ramp between lo and hi, clamped. The 'no hard gate' primitive."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


class Agent:
    name = "agent"
    #: weight of this agent's own confidence in the vote
    base_confidence = 0.6

    def vote(self, ctx: MarketContext) -> Vote:  # pragma: no cover - interface
        raise NotImplementedError

    def _vote(self, score: float, reasons: list[str], confidence: Optional[float] = None) -> Vote:
        return Vote(self.name, _clamp(score), self.base_confidence if confidence is None else confidence,
                    reasons)


# --------------------------------------------------------------------------- #
class TrendAgent(Agent):
    """EMA 9/21 alignment with multi-timeframe *graded* confirmation."""
    name = "trend"

    def vote(self, ctx: MarketContext) -> Vote:
        m5 = ctx.primary
        if not m5.ready():
            return self._vote(0.0, ["insufficient bars"])
        dirn = m5.trend_dir
        if dirn == 0:
            return self._vote(0.0, ["EMA9 == EMA21"], confidence=0.2)

        reasons = [f"M5 EMA9{'>' if dirn > 0 else '<'}EMA21"]
        # strength from spread, ADX and efficiency
        strength = (_soft(abs(m5.ema_spread) / max(m5.atr_v / m5.close, 1e-9), 0.05, 1.2)
                    * _soft(m5.adx_v, 15, 35)
                    * _soft(m5.efficiency, 0.10, 0.55))
        reasons.append(f"spread={m5.ema_spread:.4%} adx={m5.adx_v:.0f} eff={m5.efficiency:.2f}")

        # graded multi-timeframe credit (NOT an AND gate)
        htf = 0.0
        for tf, w in (("M15", 0.30), ("H1", 0.20)):
            s = ctx.tf(tf)
            if s is None:
                htf += w * 0.5                      # unknown = neutral, not veto
                continue
            if s.trend_dir == dirn:
                htf += w
                reasons.append(f"{tf} agrees")
            elif s.trend_dir == -dirn:
                htf -= w
                reasons.append(f"{tf} opposes")
            else:
                reasons.append(f"{tf} flat")
        # map htf in [-0.5, +0.5] -> multiplier [0.5, 1.25]
        mult = 0.5 + 0.75 * (htf + 0.5)

        # pullback bonus: entering with trend after a mild retrace is the high-quality scalp
        if 0.6 <= _soft(m5.atr_v and abs(m5.close - m5.ef) / m5.atr_v or 0, 0.0, 2.0) <= 2.0:
            pass

        score = dirn * strength * mult
        return self._vote(score, reasons)


class MeanReversionAgent(Agent):
    """Band / VWAP stretch fade. Only meaningful when the market is not trending."""
    name = "meanrev"

    def vote(self, ctx: MarketContext) -> Vote:
        m5 = ctx.primary
        if not m5.ready() or m5.atr_v <= 0:
            return self._vote(0.0, ["insufficient bars"])
        z = m5.bb_z
        vd = m5.vwap_dist_atr
        rsi = m5.rsi_v if m5.rsi_v is not None else 50.0
        reasons = [f"bb_z={z:+.2f} vwap_dist={vd:+.2f}ATR rsi={rsi:.0f}"]

        # Penalise fading in a strongly trending / chaotic tape.
        regime_ok = _soft(1.0 - ctx.regime.efficiency, 0.15, 0.65)
        if ctx.regime.label in (RegimeLabel.TREND_UP, RegimeLabel.TREND_DOWN):
            regime_ok *= 0.35
        if ctx.regime.label is RegimeLabel.CHAOS:
            regime_ok *= 0.5

        # A stretch must come from price being extreme *within its own band* or
        # from an RSI extreme. VWAP distance is a secondary confirmation only:
        # on a trending day price sits on one side of VWAP for hours, so treating
        # it as a standalone trigger produces a permanent counter-trend bias.
        z_stretch = max(_soft(-z, 1.0, 2.6), _soft(z, 1.0, 2.6))
        rsi_stretch = max(_soft(28 - rsi, 0, 14), _soft(rsi - 72, 0, 14))
        side = 0
        if rsi <= 30 or z <= -1.4:
            side = 1
        elif rsi >= 70 or z >= 1.4:
            side = -1
        if side == 0:
            return self._vote(0.0, reasons + ["no stretch"], confidence=0.25)
        vwap_aligned = max(0.0, side * -vd)              # stretch away from VWAP
        stretch = 0.62 * max(z_stretch, rsi_stretch) + 0.38 * _soft(vwap_aligned, 0.8, 2.4)
        reasons.append("fade stretch")
        return self._vote(side * stretch * regime_ok, reasons)


class BreakoutAgent(Agent):
    """Donchian break with volatility expansion and follow-through."""
    name = "breakout"

    def vote(self, ctx: MarketContext) -> Vote:
        m5 = ctx.primary
        if not m5.ready() or m5.atr_v <= 0 or m5.don_upper <= m5.don_lower:
            return self._vote(0.0, ["insufficient bars"])
        buf = 0.10 * m5.atr_v
        up = m5.close > m5.don_upper - buf
        dn = m5.close < m5.don_lower + buf
        reasons = [f"don={m5.don_lower:.5f}/{m5.don_upper:.5f} close={m5.close:.5f}"]
        vol_ok = _soft(m5.vol_pct, 0.45, 0.95)
        adx_ok = _soft(m5.adx_v, 16, 38)
        side = 1 if up and not dn else (-1 if dn and not up else 0)
        if side == 0:
            return self._vote(0.0, reasons + ["inside channel"], confidence=0.2)
        reasons += ["channel break", f"volpct={m5.vol_pct:.2f} adx={m5.adx_v:.0f}"]
        prior = m5.slope * side
        conf_bonus = 1.0 + 0.5 * _soft(prior, 0, 1e-3)
        return self._vote(side * (0.45 + 0.55 * vol_ok * adx_ok) * min(conf_bonus, 1.4), reasons)


class ScalpMomentumAgent(Agent):
    """Micro momentum: short-horizon drift, used to time quick scalps."""
    name = "momentum"

    def vote(self, ctx: MarketContext) -> Vote:
        m5 = ctx.primary
        if not m5.ready() or m5.atr_v <= 0:
            return self._vote(0.0, ["insufficient bars"])
        drift = m5.slope * m5.close / max(m5.atr_v, 1e-12)      # slope in ATR units per bar
        micro = (m5.close - m5.vwap_v) / max(m5.atr_v, 1e-12)
        score = _clamp(0.45 * _clamp(drift * 90, -1, 1) + 0.55 * _clamp(micro * 0.5, -1, 1))
        reasons = [f"drift={drift:+.3f}ATR micro={micro:+.2f}ATR"]
        if abs(score) < 0.12:
            return self._vote(0.0, reasons + ["no micro drift"], confidence=0.2)
        # This agent times entries; it must not *create* direction. When it
        # disagrees with the EMA structure it is diluted to a nudge, because
        # standalone micro-momentum on a noisy tape is pure cost-churn.
        dirn = 1 if m5.ema_spread > 0 else (-1 if m5.ema_spread < 0 else 0)
        aligned = (score > 0) == (dirn > 0)
        if dirn == 0 or not aligned:
            score *= 0.45
            reasons.append("against EMA structure -> damped")
        else:
            reasons.append("with EMA structure")
        return self._vote(score, reasons)


class CarryAgent(Agent):
    """Swap/rollover carry.

    Retained for parity with the APEX 1.1.0 agent set. It abstains unless the
    broker adapter supplies swap rates, because inventing a carry signal from
    price alone is how systems accumulate fake edges.
    """
    name = "carry"
    base_confidence = 0.3

    def vote(self, ctx: MarketContext) -> Vote:
        swap = ctx.quote.meta.get("swap_long") if hasattr(ctx.quote, "meta") else None
        if swap is None:
            return self._vote(0.0, ["swap data unavailable -> abstain"], confidence=0.1)
        return self._vote(_clamp(swap), [f"swap={swap}"])


class VolatilityFilterAgent(Agent):
    """Not directional: penalises entries when the tape is too quiet or too wild.

    Expressed as a signed vote of 0 with a confidence that reflects tradability,
    so it dilutes conviction rather than forcing a direction.
    """
    name = "volfilter"
    base_confidence = 0.4

    def vote(self, ctx: MarketContext) -> Vote:
        m5 = ctx.primary
        if not m5.ready():
            return self._vote(0.0, ["insufficient bars"], confidence=0.1)
        ok = _soft(m5.vol_pct, 0.12, 0.45) * (1.0 - _soft(m5.vol_pct, 0.90, 1.0))
        return self._vote(0.0, [f"vol_pct={m5.vol_pct:.2f} tradable={ok:.2f}"], confidence=ok)


DEFAULT_AGENTS: list[Agent] = [
    TrendAgent(), MeanReversionAgent(), BreakoutAgent(),
    ScalpMomentumAgent(), CarryAgent(), VolatilityFilterAgent(),
]

AGENT_REGISTRY: dict[str, type[Agent]] = {
    "trend": TrendAgent, "meanrev": MeanReversionAgent, "breakout": BreakoutAgent,
    "momentum": ScalpMomentumAgent, "carry": CarryAgent, "volfilter": VolatilityFilterAgent,
}


def build_agents(names: Optional[list[str]] = None) -> list[Agent]:
    if not names:
        return list(DEFAULT_AGENTS)
    return [AGENT_REGISTRY[n]() for n in names if n in AGENT_REGISTRY]
