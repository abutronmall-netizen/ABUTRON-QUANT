"""Incremental technical indicators.

Pure standard library on purpose: the production audit showed a system that could
not be reproduced outside one hand-built server. A scalping engine is I/O bound,
not CPU bound, so dependency-free indicators cost nothing and make the whole
system auditable and portable (Windows MT5 boxes, Linux VPS, CI).

All indicators are incremental: `IndicatorSet.update(bar)` is O(1) amortised, so
live streaming and 1e6-bar backtests run the identical code path.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Optional, Sequence

from .models import Bar


def sma(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    m = sum(values) / n
    return math.sqrt(sum((v - m) ** 2 for v in values) / (n - 1))


def linreg_slope(values: Sequence[float]) -> float:
    """Least-squares slope per step."""
    n = len(values)
    if n < 2:
        return 0.0
    xm = (n - 1) / 2.0
    ym = sum(values) / n
    num = sum((i - xm) * (v - ym) for i, v in enumerate(values))
    den = sum((i - xm) ** 2 for i in range(n))
    return num / den if den else 0.0


class EMA:
    __slots__ = ("n", "k", "value", "seeded", "_seed")

    def __init__(self, n: int):
        self.n = n
        self.k = 2.0 / (n + 1.0)
        self.value: Optional[float] = None
        self.seeded = False
        self._seed: list[float] = []

    def update(self, x: float) -> float:
        if self.value is None:
            self._seed.append(x)
            if len(self._seed) >= self.n:
                self.value = sum(self._seed) / len(self._seed)
                self.seeded = True
                self._seed.clear()
            return self.value if self.value is not None else x
        self.value += self.k * (x - self.value)
        return self.value


class RSI:
    __slots__ = ("n", "avg_gain", "avg_loss", "prev", "value")

    def __init__(self, n: int = 14):
        self.n = n
        self.avg_gain: Optional[float] = None
        self.avg_loss: Optional[float] = None
        self.prev: Optional[float] = None
        self.value: Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        if self.prev is None:
            self.prev = close
            return None
        change = close - self.prev
        self.prev = close
        gain, loss = max(change, 0.0), max(-change, 0.0)
        if self.avg_gain is None:
            self.avg_gain, self.avg_loss = gain, loss
            return None
        a = 1.0 / self.n
        self.avg_gain += a * (gain - self.avg_gain)
        self.avg_loss += a * (loss - self.avg_loss)
        if self.avg_loss == 0:
            self.value = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            self.value = 100.0 - 100.0 / (1.0 + rs)
        return self.value


class ATR:
    __slots__ = ("n", "value", "prev_close", "_seed")

    def __init__(self, n: int = 14):
        self.n = n
        self.value: Optional[float] = None
        self.prev_close: Optional[float] = None
        self._seed: list[float] = []

    def update(self, bar: Bar) -> Optional[float]:
        if self.prev_close is None:
            self.prev_close = bar.close
            return None
        tr = max(bar.high - bar.low,
                 abs(bar.high - self.prev_close),
                 abs(bar.low - self.prev_close))
        self.prev_close = bar.close
        if self.value is None:
            self._seed.append(tr)
            if len(self._seed) >= self.n:
                self.value = sum(self._seed) / len(self._seed)
                self._seed.clear()
            return self.value
        self.value += (tr - self.value) / self.n
        return self.value


class ADX:
    """Wilder ADX with +DI / -DI."""
    __slots__ = ("n", "prev_high", "prev_low", "prev_close",
                 "smooth_tr", "smooth_pdm", "smooth_ndm", "adx", "count", "di_plus", "di_minus")

    def __init__(self, n: int = 14):
        self.n = n
        self.prev_high: Optional[float] = None
        self.prev_low: Optional[float] = None
        self.prev_close: Optional[float] = None
        self.smooth_tr = 0.0
        self.smooth_pdm = 0.0
        self.smooth_ndm = 0.0
        self.adx: Optional[float] = None
        self.count = 0
        self.di_plus = 0.0
        self.di_minus = 0.0

    def update(self, bar: Bar) -> Optional[float]:
        if self.prev_close is None:
            self.prev_high, self.prev_low, self.prev_close = bar.high, bar.low, bar.close
            return None
        up = bar.high - self.prev_high
        dn = self.prev_low - bar.low
        pdm = up if (up > dn and up > 0) else 0.0
        ndm = dn if (dn > up and dn > 0) else 0.0
        tr = max(bar.high - bar.low, abs(bar.high - self.prev_close), abs(bar.low - self.prev_close))
        self.prev_high, self.prev_low, self.prev_close = bar.high, bar.low, bar.close
        self.count += 1
        if self.count <= self.n:
            self.smooth_tr += tr
            self.smooth_pdm += pdm
            self.smooth_ndm += ndm
            if self.count < self.n:
                return None
        else:
            self.smooth_tr = self.smooth_tr - self.smooth_tr / self.n + tr
            self.smooth_pdm = self.smooth_pdm - self.smooth_pdm / self.n + pdm
            self.smooth_ndm = self.smooth_ndm - self.smooth_ndm / self.n + ndm
        if self.smooth_tr <= 0:
            return self.adx
        self.di_plus = 100.0 * self.smooth_pdm / self.smooth_tr
        self.di_minus = 100.0 * self.smooth_ndm / self.smooth_tr
        dx = 100.0 * abs(self.di_plus - self.di_minus) / (self.di_plus + self.di_minus) \
            if (self.di_plus + self.di_minus) else 0.0
        if self.adx is None:
            self.adx = dx
        else:
            self.adx += (dx - self.adx) / self.n
        return self.adx


class Donchian:
    __slots__ = ("n", "highs", "lows", "upper", "lower")

    def __init__(self, n: int = 20):
        self.n = n
        self.highs: deque[float] = deque(maxlen=n)
        self.lows: deque[float] = deque(maxlen=n)
        self.upper = 0.0
        self.lower = 0.0

    def update(self, bar: Bar) -> tuple[float, float]:
        self.highs.append(bar.high)
        self.lows.append(bar.low)
        self.upper = max(self.highs)
        self.lower = min(self.lows)
        return self.upper, self.lower


class VWAP:
    """Session VWAP, resets on UTC day change."""
    __slots__ = ("day", "pv", "vol")

    def __init__(self):
        self.day: Optional[int] = None
        self.pv = 0.0
        self.vol = 0.0

    def update(self, bar: Bar) -> float:
        day = int(bar.ts // 86400)
        if self.day != day:
            self.day, self.pv, self.vol = day, 0.0, 0.0
        typical = (bar.high + bar.low + bar.close) / 3.0
        v = bar.volume if bar.volume > 0 else 1.0
        self.pv += typical * v
        self.vol += v
        return self.pv / self.vol if self.vol else typical


class IndicatorSet:
    """Everything one timeframe needs, updated once per closed bar."""

    def __init__(self, n_fast: int = 9, n_slow: int = 21, n_rsi: int = 14,
                 n_atr: int = 14, n_adx: int = 14, n_bb: int = 20,
                 n_don: int = 20, vol_window: int = 200):
        self.ema_fast = EMA(n_fast)
        self.ema_slow = EMA(n_slow)
        self.rsi = RSI(n_rsi)
        self.atr = ATR(n_atr)
        self.adx = ADX(n_adx)
        self.bb_n = n_bb
        self.closes: deque[float] = deque(maxlen=n_bb)
        self.don = Donchian(n_don)
        self.vwap = VWAP()
        self.atr_hist: deque[float] = deque(maxlen=vol_window)
        self.bb_width_hist: deque[float] = deque(maxlen=vol_window)
        self.n_bars = 0

        # latest snapshot
        self.close = 0.0
        self.ef = 0.0
        self.es = 0.0
        self.rsi_v: Optional[float] = None
        self.atr_v: float = 0.0
        self.adx_v: float = 0.0
        self.don_upper = 0.0
        self.don_lower = 0.0
        self.vwap_v = 0.0
        self.bb_mid = 0.0
        self.bb_up = 0.0
        self.bb_dn = 0.0
        self.bb_width = 0.0
        self.vol_pct = 0.5
        self.slope = 0.0

    # ------------------------------------------------------------------ #
    def update(self, bar: Bar) -> "IndicatorSet":
        self.n_bars += 1
        self.close = bar.close
        self.ef = self.ema_fast.update(bar.close)
        self.es = self.ema_slow.update(bar.close)
        r = self.rsi.update(bar.close)
        if r is not None:
            self.rsi_v = r
        a = self.atr.update(bar)
        if a is not None:
            self.atr_v = a
            self.atr_hist.append(a)
        d = self.adx.update(bar)
        if d is not None:
            self.adx_v = d
        self.don_upper, self.don_lower = self.don.update(bar)
        self.vwap_v = self.vwap.update(bar)
        self.closes.append(bar.close)

        if len(self.closes) >= self.bb_n:
            self.bb_mid = sma(self.closes)
            sd = stdev(self.closes)
            self.bb_up = self.bb_mid + 2 * sd
            self.bb_dn = self.bb_mid - 2 * sd
            self.bb_width = (self.bb_up - self.bb_dn) / self.bb_mid if self.bb_mid else 0.0
            self.bb_width_hist.append(self.bb_width)

        if len(self.closes) >= 10:
            self.slope = linreg_slope(list(self.closes)[-10:]) / max(self.close, 1e-9)

        if self.atr_hist:
            cur = self.atr_v
            self.vol_pct = sum(1 for x in self.atr_hist if x <= cur) / len(self.atr_hist)
        return self

    # ------------------------------------------------------------------ #
    def ready(self) -> bool:
        return self.n_bars >= max(self.bb_n, 30)

    @property
    def ema_spread(self) -> float:
        return (self.ef - self.es) / self.close if self.close else 0.0

    @property
    def trend_dir(self) -> int:
        if not self.ready():
            return 0
        return 1 if self.ef > self.es else (-1 if self.ef < self.es else 0)

    @property
    def bb_z(self) -> float:
        """Z-score of price inside the Bollinger band (0 = mid, +/-1 = band)."""
        if not self.ready() or self.bb_up <= self.bb_dn:
            return 0.0
        return (self.close - self.bb_mid) / ((self.bb_up - self.bb_dn) / 4.0)

    @property
    def vwap_dist_atr(self) -> float:
        return (self.close - self.vwap_v) / self.atr_v if self.atr_v else 0.0

    @property
    def efficiency(self) -> float:
        """Kaufman efficiency ratio over the recent window: 0 chop .. 1 clean trend."""
        if len(self.closes) < 10:
            return 0.0
        vals = list(self.closes)[-10:]
        net = abs(vals[-1] - vals[0])
        gross = sum(abs(vals[i] - vals[i - 1]) for i in range(1, len(vals)))
        return net / gross if gross else 0.0
