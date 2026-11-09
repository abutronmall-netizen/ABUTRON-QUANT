"""Market regime detection.

The audit found the production scanner had one strategy for every condition, so
it spent most of its life reporting HOLD. Regime detection is what lets the same
engine select *which* edge is applicable right now instead of requiring every
timeframe to agree before anything can happen.
"""
from __future__ import annotations

from .indicators import IndicatorSet
from .models import Regime, RegimeLabel


def detect(primary: IndicatorSet, higher: dict[str, IndicatorSet]) -> Regime:
    """Classify the current market using the entry timeframe plus context."""
    price = primary.close or 1.0
    atr = primary.atr_v
    atr_pct = atr / price if price else 0.0
    adx = primary.adx_v or 0.0
    eff = primary.efficiency
    bw = primary.bb_width
    vol_pct = primary.vol_pct
    slope = primary.slope

    htf_agree = 0
    htf_total = 0
    for tf in ("M15", "H1"):
        s = higher.get(tf)
        if s is not None and s.ready():
            htf_total += 1
            if s.trend_dir == primary.trend_dir:
                htf_agree += 1

    # --- classification order matters: chaos first, then trend, then range ---
    if vol_pct > 0.88 and eff < 0.30:
        label = RegimeLabel.CHAOS
    elif vol_pct > 0.80 and eff >= 0.42 and adx >= 20:
        label = RegimeLabel.EXPANSION
    elif eff >= 0.38 and adx >= 20 and abs(slope) > 0.00012:
        label = RegimeLabel.TREND_UP if slope > 0 else RegimeLabel.TREND_DOWN
    elif vol_pct < 0.28 and adx < 19:
        label = RegimeLabel.SQUEEZE
    else:
        label = RegimeLabel.RANGE

    # A trend label is downgraded when higher timeframes are clearly fighting it.
    if label in (RegimeLabel.TREND_UP, RegimeLabel.TREND_DOWN) and htf_total:
        want = 1 if label is RegimeLabel.TREND_UP else -1
        if htf_agree == 0:
            label = RegimeLabel.RANGE

    return Regime(
        label=label,
        atr=atr,
        atr_pct=atr_pct,
        adx=adx,
        efficiency=eff,
        bb_width_pct=bw,
        vol_percentile=vol_pct,
        slope=slope,
    )
