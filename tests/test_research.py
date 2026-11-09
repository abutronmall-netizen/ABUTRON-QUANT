"""Optimisation harness: grid expansion, scoring, sweep and prop simulation.

The first build mutated `StrategyProfile.preset` globally during a sweep, which
silently changed the strategy for anything else running in the same process.
The regression test here is explicit: a sweep must leave the class untouched.
"""
import json
import os
import tempfile
import unittest

from apex.backtest import BacktestResult
from apex.research.optimize import (
    DEFAULT_GRID,
    PropSimRequest,
    SweepRequest,
    _apply_params,
    expand_grid,
    prop_pass_sim,
    score_result,
    sweep,
)
from apex.strategy import StrategyProfile


def fake_result(avg_r: float, trades: int = 40, dd: float = 3.0,
                verdict: str = "active", daily_used: float = 0.0) -> BacktestResult:
    return BacktestResult(
        metrics={"avg_r": avg_r, "trades": trades, "max_drawdown_pct": dd, "pnl": 0.0,
                 "win_rate": 50.0, "profit_factor": 1.0},
        prop={"verdict": verdict, "daily_loss_used_pct": daily_used,
              "profit_pct": 0.0, "target_pct": 8.0} if verdict else None,
        rejections={}, trades=[], equity_curve=[], signal_stats={}, config={}, elapsed_s=0.0,
    )


class TestGrid(unittest.TestCase):
    def test_expansion_is_deterministic_and_bounded(self):
        a = expand_grid(DEFAULT_GRID, 8, seed=3)
        b = expand_grid(DEFAULT_GRID, 8, seed=3)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 8)
        for row in a:
            self.assertEqual(set(row), set(DEFAULT_GRID))

    def test_full_product_when_under_the_limit(self):
        rows = expand_grid({"sl_atr": [1.0, 1.2], "tp_atr": [2.0]}, 10)
        self.assertEqual(len(rows), 2)

    def test_apply_params_copies_instead_of_mutating(self):
        base = StrategyProfile.preset("scalp_balanced")
        before = base.sl_atr
        derived = _apply_params(base, {"sl_atr": 9.9})
        self.assertAlmostEqual(derived.sl_atr, 9.9)
        self.assertAlmostEqual(base.sl_atr, before)
        self.assertIsNot(derived, base)


class TestScoring(unittest.TestCase):
    def test_too_few_trades_is_disqualified(self):
        self.assertLess(score_result(fake_result(1.0, trades=3)), -1e8)

    def test_drawdown_is_penalised_quadratically(self):
        calm = score_result(fake_result(0.10, dd=2.0))
        wild = score_result(fake_result(0.10, dd=8.0))
        self.assertGreater(calm, wild)

    def test_breach_is_worse_than_no_challenge(self):
        clean = score_result(fake_result(0.10, verdict=None))
        breached = score_result(fake_result(0.10, verdict="breached"))
        self.assertLess(breached, clean)

    def test_passing_is_rewarded(self):
        self.assertGreater(score_result(fake_result(0.10, verdict="passed")),
                           score_result(fake_result(0.10, verdict=None)))

    def test_daily_loss_usage_costs_score(self):
        self.assertGreater(score_result(fake_result(0.10, daily_used=0.0)),
                           score_result(fake_result(0.10, daily_used=70.0)))


class TestSweep(unittest.TestCase):
    def test_sweep_runs_and_reports_sorted_rows(self):
        tmp = tempfile.mkdtemp()
        preset_before = StrategyProfile.preset("scalp_balanced")
        req = SweepRequest(symbols_by_broker={"deriv": ["R_25"]}, bars=400, seeds=[1],
                           max_combinations=2, out_dir=tmp,
                           grid={"sl_atr": [1.0, 1.3], "tp_atr": [1.8, 2.2]})
        out = sweep(req)
        self.assertEqual(len(out["results"]), 2)
        scores = [r["score"] for r in out["results"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        for row in out["results"]:
            for key in ("score", "avg_r", "max_dd", "trades", "pass_rate", "params"):
                self.assertIn(key, row)
        self.assertTrue(os.path.exists(os.path.join(tmp, "sweep.json")))
        with open(os.path.join(tmp, "sweep.json")) as fh:
            blob = json.load(fh)
        self.assertEqual(len(blob["results"]), 2)

        # the regression that started this test: no global mutation
        preset_after = StrategyProfile.preset("scalp_balanced")
        self.assertEqual(preset_before.sl_atr, preset_after.sl_atr)
        self.assertEqual(preset_before.tp_atr, preset_after.tp_atr)
        self.assertEqual(preset_before.time_stop_bars, preset_after.time_stop_bars)


class TestPropSim(unittest.TestCase):
    def test_simulation_reports_rates_and_verdicts(self):
        tmp = tempfile.mkdtemp()
        out = prop_pass_sim(PropSimRequest(profile="abutron_inhouse",
                                           symbols_by_broker={"deriv": ["R_25"]},
                                           bars=400, seeds=2, out_dir=tmp))
        for key in ("pass_rate_pct", "breach_rate_pct", "median_profit_pct",
                    "median_dd_pct", "verdicts"):
            self.assertIn(key, out)
        self.assertGreaterEqual(out["pass_rate_pct"], 0.0)
        self.assertLessEqual(out["pass_rate_pct"], 100.0)
        self.assertGreaterEqual(out["breach_rate_pct"], 0.0)
        self.assertLessEqual(out["breach_rate_pct"], 100.0)
        self.assertEqual(sum(out["verdicts"].values()), 2)
        self.assertTrue(os.path.exists(os.path.join(tmp, "propsim_abutron_inhouse.json")))


if __name__ == "__main__":
    unittest.main()
