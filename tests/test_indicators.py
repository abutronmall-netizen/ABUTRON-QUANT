import math
import unittest

from apex.data.feed import BarAggregator, SyntheticConfig, SyntheticFeed
from apex.indicators import ATR, EMA, RSI, ADX, IndicatorSet, linreg_slope
from apex.models import Bar


def bars(prices, symbol="T"):
    out = []
    for i, p in enumerate(prices):
        out.append(Bar(symbol, i * 300.0, p, p + 1, p - 1, p))
    return out


class TestPrimitives(unittest.TestCase):
    def test_ema_converges_to_constant(self):
        e = EMA(9)
        for _ in range(200):
            v = e.update(100.0)
        self.assertAlmostEqual(v, 100.0, places=6)

    def test_ema_seed_is_sma(self):
        e = EMA(3)
        vals = [1.0, 2.0, 3.0]
        for v in vals:
            out = e.update(v)
        self.assertAlmostEqual(out, 2.0)

    def test_rsi_bounds_and_direction(self):
        r = RSI(14)
        for p in [100 + i for i in range(40)]:
            r.update(p)
        self.assertGreater(r.value, 95)
        r2 = RSI(14)
        for p in [100 - i for i in range(40)]:
            r2.update(p)
        self.assertLess(r2.value, 5)

    def test_rsi_all_up_is_100(self):
        r = RSI(5)
        for p in range(50):
            r.update(100 + p)
        self.assertEqual(r.value, 100.0)

    def test_atr_positive_and_tracks_range(self):
        a = ATR(14)
        for b in bars([100.0] * 30):
            a.update(b)
        self.assertAlmostEqual(a.value, 2.0, places=6)

    def test_adx_high_in_trend_low_in_chop(self):
        trend = [100 + i * 2 for i in range(60)]
        adx = ADX(14)
        for b in bars(trend):
            adx.update(b)
        self.assertGreater(adx.adx, 40)

        adx2 = ADX(14)
        for i in range(60):
            p = 100 + (2 if i % 2 else -2)
            adx2.update(Bar("T", i * 300, p, p + 1, p - 1, p))
        self.assertLess(adx2.adx, 40)

    def test_linreg_slope(self):
        self.assertAlmostEqual(linreg_slope([1, 2, 3, 4, 5]), 1.0)
        self.assertAlmostEqual(linreg_slope([5, 5, 5, 5]), 0.0)


class TestIndicatorSet(unittest.TestCase):
    def setUp(self):
        gen = SyntheticFeed("R_25", SyntheticConfig(base_price=3000, vol_bps=6, seed=3))
        self.series = gen.bars(400)

    def test_not_ready_until_warm(self):
        ind = IndicatorSet()
        for b in self.series[:5]:
            ind.update(b)
        self.assertFalse(ind.ready())
        for b in self.series[5:60]:
            ind.update(b)
        self.assertTrue(ind.ready())

    def test_bb_band_ordering(self):
        ind = IndicatorSet()
        for b in self.series:
            ind.update(b)
        self.assertLess(ind.bb_dn, ind.bb_mid)
        self.assertLess(ind.bb_mid, ind.bb_up)
        self.assertGreater(ind.bb_width, 0)

    def test_vol_percentile_bounds(self):
        ind = IndicatorSet()
        for b in self.series:
            ind.update(b)
        self.assertGreaterEqual(ind.vol_pct, 0.0)
        self.assertLessEqual(ind.vol_pct, 1.0)

    def test_vwap_close_to_price_in_balanced_tape(self):
        ind = IndicatorSet()
        for b in self.series:
            ind.update(b)
        self.assertGreater(ind.vwap_v, 0)
        self.assertLess(abs(ind.close - ind.vwap_v) / ind.close, 0.2)


class TestAggregator(unittest.TestCase):
    def test_m5_to_m15_aggregation(self):
        agg = BarAggregator(900)
        closed = []
        for i in range(9):                       # 3 M15 bars worth of M5
            b = Bar("X", i * 300, 100 + i, 101 + i, 99 + i, 100.5 + i, 10)
            c = agg.update(b)
            if c:
                closed.append(c)
        self.assertEqual(len(closed), 2)
        self.assertEqual(closed[0].open, 100)
        self.assertEqual(closed[0].close, 102.5)   # 3rd M5 bar in the bucket
        self.assertEqual(closed[0].high, 103)
        self.assertEqual(closed[0].low, 99)
        self.assertAlmostEqual(closed[0].volume, 30)
        self.assertEqual(closed[0].ts, 0)
        self.assertEqual(closed[1].ts, 900)


if __name__ == "__main__":
    unittest.main()
