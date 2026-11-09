"""End-to-end backtests: the full engine on synthetic data."""
import json
import os
import shutil
import tempfile
import unittest

from apex.backtest import BacktestRequest, run_backtest
from apex.data.deriv import bars_to_csv
from apex.data.feed import synth_series
from apex.prop import get_profile
from apex.risk import RiskConfig


class TestBacktestIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _req(self, **kw):
        base = dict(symbols_by_broker={"deriv": ["R_25", "R_50"]}, bars=900, seed=3,
                    out_dir=os.path.join(self.tmp, "run"), trend_strength=3.0)
        base.update(kw)
        return BacktestRequest(**base)

    def test_runs_and_closes_everything(self):
        res = run_backtest(self._req())
        self.assertGreater(res.metrics["trades"], 0)
        self.assertEqual(len(res.equity_curve) > 0, True)

    def test_artifacts_written(self):
        res = run_backtest(self._req())
        for name in ("summary.json", "trades.csv", "equity.csv", "report.md"):
            path = os.path.join(res.out_dir, name)
            self.assertTrue(os.path.exists(path), f"{name} missing")
        with open(os.path.join(res.out_dir, "summary.json")) as fh:
            blob = json.load(fh)
        self.assertIn("metrics", blob)
        self.assertEqual(blob["metrics"]["trades"], res.metrics["trades"])

    def test_deterministic_for_same_seed(self):
        a = run_backtest(self._req(record=False, out_dir=os.path.join(self.tmp, "a")))
        b = run_backtest(self._req(record=False, out_dir=os.path.join(self.tmp, "b")))
        self.assertAlmostEqual(a.metrics["pnl"], b.metrics["pnl"], places=6)
        self.assertEqual(a.metrics["trades"], b.metrics["trades"])

    def test_different_seeds_give_different_tapes(self):
        a = run_backtest(self._req(seed=1, record=False, out_dir=os.path.join(self.tmp, "a")))
        b = run_backtest(self._req(seed=2, record=False, out_dir=os.path.join(self.tmp, "b")))
        self.assertNotAlmostEqual(a.metrics["pnl"], b.metrics["pnl"], places=3)

    def test_every_trade_has_bounded_risk(self):
        res = run_backtest(self._req(bars=1500, trend_strength=2.0))
        for t in res.trades:
            self.assertGreaterEqual(t["r_multiple"], -1.6, t)
            self.assertLessEqual(t["r_multiple"], 3.5, t)
            self.assertGreater(t["lots"], 0)
            self.assertGreater(t["entry"], 0)
            self.assertIn(t["exit_reason"],
                          ("stop_loss", "take_profit", "trailing", "time_stop",
                           "signal_flip", "manual", "prop_flatten", "eod_flatten"))

    def test_decisions_are_explained(self):
        res = run_backtest(self._req())
        # the engine must always be able to say why it did not trade
        self.assertGreater(sum(res.rejections.values()), 0)
        self.assertGreater(len(res.signal_stats), 0)

    def test_prop_profile_attached_and_judged(self):
        res = run_backtest(self._req(prop_profile="ftmo_2step", bars=1200))
        self.assertIsNotNone(res.prop)
        self.assertEqual(res.prop["profile"], "ftmo_2step")
        self.assertIn(res.prop["verdict"], ("active", "at_risk", "target_hit", "passed", "breached"))
        self.assertLessEqual(res.prop["daily_loss_used_pct"], 100.0)
        if res.prop["verdict"] == "breached":
            self.assertTrue(res.prop["breach"])

    def test_risk_per_trade_is_respected(self):
        risk = RiskConfig(risk_per_trade_pct=0.25)
        res = run_backtest(self._req(risk=risk, bars=1200, trend_strength=2.0))
        losses = [abs(t["pnl"]) for t in res.trades if t["pnl"] < 0]
        if losses:
            # no single loss may dominate the account
            self.assertLess(max(losses), 10_000 * 0.25 / 100 * 3)

    def test_cached_candles_are_replayed(self):
        from apex.data.feed import synth_series
        data_dir = os.path.join(self.tmp, "data")
        os.makedirs(data_dir, exist_ok=True)
        bars_to_csv(synth_series("R_75", n_bars=900, base_price=48_000, seed=5),
                    os.path.join(data_dir, "R_75_M5.csv"))
        res = run_backtest(self._req(symbols_by_broker={"deriv": ["R_75"]}, bars=800,
                                     data_dir=data_dir, prefer_cached=True, record=False))
        self.assertGreater(res.metrics["trades"], 0)
        res_synth = run_backtest(self._req(symbols_by_broker={"deriv": ["R_75"]}, bars=800,
                                           data_dir=data_dir, prefer_cached=False,
                                           record=False, seed=11))
        self.assertNotEqual(res.metrics["pnl"], res_synth.metrics["pnl"])

    def test_two_brokers_route_independently(self):
        res = run_backtest(self._req(
            symbols_by_broker={"deriv": ["R_25"], "mt5": ["EURUSD"]}, bars=1200,
            trend_strength=2.5))
        brokers = set(res.metrics["by_broker"])
        self.assertTrue(brokers.issubset({"deriv", "mt5", "sim"}))
        self.assertGreater(res.metrics["trades"], 0)


if __name__ == "__main__":
    unittest.main()
