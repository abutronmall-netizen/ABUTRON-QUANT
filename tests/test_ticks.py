"""Tick capture and replay: the only honest way to study spread and slippage."""
import os
import tempfile
import unittest

from apex.backtest import BacktestRequest, run_backtest
from apex.data.feed import LiveBarBuilder, TickRecorder, TickReplayFeed, synth_series, ticks_from_csv
from apex.models import Quote


def write_ticks(path: str, symbol: str = "R_75", n_bars: int = 600, seed: int = 21,
                spread_bps: float = 0.4) -> str:
    bars = synth_series(symbol, n_bars=n_bars, base_price=48_000, seed=seed)
    with TickRecorder(path) as rec:
        for b in bars:
            for i in range(6):
                px = b.open + (b.close - b.open) * i / 5.0
                half = px * (spread_bps / 10_000.0) / 2.0
                rec.write(Quote(symbol, b.ts + i * 50, px - half, px + half))
    return path


class TestRecorder(unittest.TestCase):
    def test_round_trip_preserves_spread(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "R_75.csv")
        write_ticks(path, spread_bps=1.0)
        rows = ticks_from_csv(path)
        self.assertGreater(len(rows), 1000)
        ts, sym, bid, ask = rows[0]
        self.assertEqual(sym, "R_75")
        self.assertGreater(ask, bid)
        self.assertAlmostEqual((ask - bid) / ((ask + bid) / 2) * 10_000, 1.0, places=2)

    def test_symbol_filter(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "mixed.csv")
        with TickRecorder(path) as rec:
            rec.write(Quote("R_25", 1.0, 100.0, 100.1))
            rec.write(Quote("R_50", 2.0, 200.0, 200.2))
        self.assertEqual(len(ticks_from_csv(path)), 2)
        self.assertEqual(len(ticks_from_csv(path, ["R_50"])), 1)

    def test_appending_does_not_repeat_the_header(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "a.csv")
        for _ in range(2):
            with TickRecorder(path) as rec:
                rec.write(Quote("R_25", 1.0, 1.0, 1.1))
        with open(path) as fh:
            self.assertEqual(fh.read().count("ts,symbol"), 1)


class TestReplay(unittest.TestCase):
    def test_replay_builds_bars_from_ticks(self):
        tmp = tempfile.mkdtemp()
        rows = ticks_from_csv(write_ticks(os.path.join(tmp, "t.csv")))
        feed = TickReplayFeed(rows, tf_seconds=300)
        bars = [bar for _, bar in feed if bar is not None]
        self.assertGreater(len(bars), 100)
        self.assertTrue(all(b.high >= b.low for b in bars))
        self.assertEqual(feed.symbols, ["R_75"])

    def test_bar_builder_marks_the_close(self):
        b = LiveBarBuilder("R_75", 300)
        closed = []
        base = 1_700_000_100            # aligned to a 5-minute boundary
        for i in range(6):
            bar = b.on_tick(base + i * 60, 100 + i)
            if bar is not None:
                closed.append(bar)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].open, 100)
        self.assertEqual(closed[0].close, 104)


class TestTickBacktest(unittest.TestCase):
    def test_tick_replay_drives_the_production_engine(self):
        tmp = tempfile.mkdtemp()
        path = write_ticks(os.path.join(tmp, "R_75.csv"), n_bars=900)
        res = run_backtest(BacktestRequest(
            symbols_by_broker={"deriv": ["R_75"]}, bars=900, record=False,
            out_dir=os.path.join(tmp, "out"), tick_path=path,
            prop_profile="abutron_inhouse"))
        self.assertGreater(res.metrics["trades"], 0)
        self.assertGreater(len(res.trades), 0)
        # every fill must sit inside the recorded bid/ask of its bar, not on a
        # synthetic mid: replay is only useful if it keeps the real spread
        rows = ticks_from_csv(path)
        lo = min(r[2] for r in rows)
        hi = max(r[3] for r in rows)
        for t in res.trades:
            self.assertGreaterEqual(t["entry"], lo * 0.999)
            self.assertLessEqual(t["entry"], hi * 1.001)

    def test_missing_tick_file_is_a_clear_error(self):
        with self.assertRaises(ValueError):
            run_backtest(BacktestRequest(
                symbols_by_broker={"deriv": ["R_75"]}, bars=100, record=False,
                out_dir=tempfile.mkdtemp(), tick_path="/nonexistent/ticks.csv"))


if __name__ == "__main__":
    unittest.main()
