"""The traded universe: R_10 and R_100 are first-class everywhere."""
import unittest

from apex.symbols import DEFAULT_SYMBOLS, DEFAULT_SYMBOLS_BY_BROKER, VOLATILITY_INDICES, parse_symbols


class TestSymbolUniverse(unittest.TestCase):
    def test_default_book_is_the_full_volatility_curve(self):
        self.assertEqual(VOLATILITY_INDICES, ["R_10", "R_25", "R_50", "R_75", "R_100"])
        self.assertEqual(DEFAULT_SYMBOLS, VOLATILITY_INDICES)

    def test_deriv_trades_every_volatility_index(self):
        deriv = DEFAULT_SYMBOLS_BY_BROKER["deriv"]
        for symbol in ("R_10", "R_25", "R_50", "R_75", "R_100"):
            self.assertIn(symbol, deriv)

    def test_mt5_book_keeps_fx_metals_and_indices(self):
        mt5 = DEFAULT_SYMBOLS_BY_BROKER["mt5"]
        for symbol in ("EURUSD", "GBPUSD", "USDJPY", "XAUUSD", "US30", "NAS100", "GER40"):
            self.assertIn(symbol, mt5)

    def test_parse_symbols_is_forgiving(self):
        self.assertEqual(parse_symbols("r_10, r_25 ,R_100"), ["R_10", "R_25", "R_100"])
        self.assertEqual(parse_symbols("R_10 R_10 R_50"), ["R_10", "R_50"])
        self.assertEqual(parse_symbols(""), [])
        self.assertEqual(parse_symbols(None), [])
        self.assertEqual(parse_symbols(["R_10", "r_10"]), ["R_10"])
        self.assertEqual(parse_symbols("R10"), ["R_10"])


class TestDefaultsUseTheUniverse(unittest.TestCase):
    def test_live_config_default(self):
        from apex.live import LiveConfig
        cfg = LiveConfig()
        self.assertEqual(cfg.symbols_by_broker["deriv"],
                         ["R_10", "R_25", "R_50", "R_75", "R_100"])

    def test_config_module_default(self):
        from apex.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG["symbols_by_broker"],
                         dict(DEFAULT_SYMBOLS_BY_BROKER))

    def test_backtest_request_default(self):
        from apex.backtest import BacktestRequest
        req = BacktestRequest()
        self.assertEqual(req.symbols_by_broker["deriv"],
                         ["R_10", "R_25", "R_50", "R_75", "R_100"])

    def test_cli_signals_default_is_the_curve(self):
        """`--symbols` no longer hard-codes the deriv list for every broker, so the
        guarantee now lives in the book that is actually built."""
        from apex.cli import _book_for, build_parser
        args = build_parser().parse_args(["serve", "--demo"])
        self.assertIsNone(args.symbols)
        book = _book_for(args, "deriv")
        self.assertEqual(book["deriv"], ["R_10", "R_25", "R_50", "R_75", "R_100"])

    def test_optimizer_default(self):
        from apex.research.optimize import SweepRequest
        req = SweepRequest()
        self.assertEqual(req.symbols_by_broker["deriv"],
                         ["R_10", "R_25", "R_50", "R_75", "R_100"])

    def test_backtest_has_market_data_for_the_new_indices(self):
        from apex.backtest import BASE_PRICES, VOL_BPS
        self.assertIn("R_10", BASE_PRICES)
        self.assertIn("R_100", BASE_PRICES)
        self.assertIn("R_10", VOL_BPS)
        self.assertIn("R_100", VOL_BPS)


class TestResearchReproducibility(unittest.TestCase):
    """A seeded run must give the same answer tomorrow, on any machine."""

    def test_tape_does_not_follow_the_wall_clock(self):
        from apex.backtest import ANCHOR_TS, BacktestRequest, _tape_start
        from apex.timeutil import tf_seconds
        req = BacktestRequest(symbols_by_broker={"deriv": ["R_50"]}, bars=500, tf="M5", seed=7)
        first = _tape_start(req)
        self.assertLess(first, ANCHOR_TS)
        self.assertEqual(first % tf_seconds("M5"), 0)
        self.assertEqual(len({_tape_start(req) for _ in range(3)}), 1)

    def test_the_tape_ends_at_the_anchor(self):
        import os
        import tempfile

        from apex.backtest import ANCHOR_TS, BacktestRequest, _series_for
        from apex.timeutil import tf_seconds
        step = tf_seconds("M5")
        req = BacktestRequest(symbols_by_broker={"deriv": ["R_50"]}, bars=80,
                              tf="M5", seed=3, record=False,
                              out_dir=os.path.join(tempfile.mkdtemp(), "o"))
        bars = _series_for("R_50", req)
        self.assertEqual(len(bars), 80)
        aligned = int(ANCHOR_TS) // step * step
        self.assertEqual(bars[-1].ts + step, aligned)
        self.assertEqual(bars[0].ts + 80 * step, aligned)

    def test_same_seed_same_metrics(self):
        import os
        import tempfile

        from apex.backtest import BacktestRequest, run_backtest
        tmp = tempfile.mkdtemp()
        outs = []
        for i in range(2):
            res = run_backtest(BacktestRequest(
                symbols_by_broker={"deriv": ["R_50"]}, bars=400, seed=11,
                record=False, out_dir=os.path.join(tmp, f"o{i}")))
            outs.append((res.metrics["trades"], round(res.metrics["pnl"], 6)))
        self.assertEqual(outs[0], outs[1])


if __name__ == "__main__":
    unittest.main()
