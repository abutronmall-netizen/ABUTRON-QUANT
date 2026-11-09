import os
import tempfile
import unittest

from apex.models import Ack, ExitReason, Order, Side, TradeRecord
from apex.store import Store, compute_metrics


def trade(i, pnl, r=None, strategy="s1", symbol="R_25", broker="deriv",
          regime="trend_up", exit_reason=ExitReason.TAKE_PROFIT):
    return {
        "trade_id": f"t{i}", "symbol": symbol, "side": Side.BUY.value, "lots": 1.0,
        "entry": 100.0, "exit": 100.0 + pnl, "opened_ts": 1_700_000_000 + i * 300,
        "closed_ts": 1_700_000_000 + i * 300 + 60, "pnl": pnl, "commission": 0.0,
        "strategy": strategy, "broker": broker, "account": "A",
        "exit_reason": exit_reason.value, "conviction": 0.5, "regime": regime,
        "r_multiple": r if r is not None else pnl / 50.0, "mfe": 0.0, "mae": 0.0, "meta": {},
    }


class TestMetrics(unittest.TestCase):
    def test_empty(self):
        m = compute_metrics([])
        self.assertEqual(m["trades"], 0)
        self.assertEqual(m["win_rate"], 0.0)

    def test_basic_stats(self):
        trades = [trade(1, 100, 2.0), trade(2, -50, -1.0), trade(3, 100, 2.0), trade(4, -50, -1.0)]
        m = compute_metrics(trades, initial_equity=10_000)
        self.assertEqual(m["trades"], 4)
        self.assertEqual(m["wins"], 2)
        self.assertEqual(m["win_rate"], 50.0)
        self.assertAlmostEqual(m["pnl"], 100.0)
        self.assertAlmostEqual(m["profit_factor"], 2.0)
        self.assertAlmostEqual(m["avg_r"], 0.5)
        self.assertAlmostEqual(m["expectancy"], 25.0)
        self.assertAlmostEqual(m["best"], 100.0)
        self.assertAlmostEqual(m["worst"], -50.0)

    def test_drawdown_measured_from_initial_equity(self):
        trades = [trade(1, 10), trade(2, -500), trade(3, -500)]
        m = compute_metrics(trades, initial_equity=10_000)
        self.assertGreater(m["max_drawdown_pct"], 9.0)
        self.assertLess(m["max_drawdown_pct"], 12.0)

    def test_groups(self):
        trades = [trade(1, 10, strategy="a", symbol="R_25", regime="range"),
                  trade(2, -5, strategy="b", symbol="R_75", regime="trend_up")]
        m = compute_metrics(trades)
        self.assertEqual(set(m["by_strategy"]), {"a", "b"})
        self.assertEqual(m["by_symbol"]["R_25"]["trades"], 1)
        self.assertEqual(m["by_regime"]["range"]["pnl"], 10)

    def test_profit_factor_infinite_without_losses(self):
        m = compute_metrics([trade(1, 10), trade(2, 20)])
        self.assertEqual(m["profit_factor"], float("inf"))


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.store.close()

    def test_roundtrip_trade(self):
        t = TradeRecord(trade_id="T1", symbol="R_25", side=Side.BUY, lots=1.0, entry=100.0,
                        exit=101.0, opened_ts=1.0, closed_ts=2.0, pnl=1.0, commission=0.0,
                        strategy="s", broker="deriv", account="A",
                        exit_reason=ExitReason.TAKE_PROFIT, r_multiple=2.0,
                        meta={"votes": [{"agent": "trend", "score": 0.4}]})
        self.store.record_trade(t)
        rows = self.store.trades()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "R_25")
        self.assertEqual(rows[0]["r_multiple"], 2.0)

    def test_orders_and_events(self):
        o = Order(symbol="R_25", side=Side.BUY, lots=1.0, broker="deriv", account="A")
        ack = Ack(ok=True, client_id=o.client_id, ts=5.0, order_id="X", filled_price=100.0, lots=1.0)
        self.store.record_order(o, ack)
        self.store.record_event("entry", "opened R_25")
        self.assertEqual(self.store.recent_events()[0]["message"], "opened R_25")

    def test_signal_stats(self):
        for status in ("NO_STRUCTURE", "NO_STRUCTURE", "ENTERED"):
            self.store.record_signal(None, status, "x", {}, symbol="R_25")
        self.assertEqual(self.store.signal_stats()["NO_STRUCTURE"], 2)
        self.assertEqual(self.store.signal_stats()["ENTERED"], 1)

    def test_metrics_from_journal(self):
        for i in range(3):
            self.store.record_trade(TradeRecord(
                trade_id=f"T{i}", symbol="R_25", side=Side.BUY, lots=1.0, entry=100.0,
                exit=101.0, opened_ts=1.0 + i, closed_ts=2.0 + i, pnl=10.0, commission=0.0,
                strategy="s", broker="deriv", account="A", exit_reason=ExitReason.TAKE_PROFIT))
        m = self.store.metrics()
        self.assertEqual(m["trades"], 3)
        self.assertEqual(m["win_rate"], 100.0)

    def test_equity_curve_ordered(self):
        for i in range(5):
            self.store.record_equity(1_700_000_000 + i, "deriv", "A", 10_000 + i, 10_000)
        curve = self.store.equity_curve()
        self.assertEqual([c["equity"] for c in curve], [10_000, 10_001, 10_002, 10_003, 10_004])


if __name__ == "__main__":
    unittest.main()
