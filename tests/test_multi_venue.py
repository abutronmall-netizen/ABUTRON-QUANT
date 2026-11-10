"""The rehearsal: the real Deriv adapter over a real socket, and the multi-venue book.

Two claims this file is responsible for:

  1. ``DerivBroker`` with ``dry_run=False`` — the code that would send a real
     contract — completes a handshake, an order, a portfolio read and a close
     against a server it has to actually talk to.
  2. One engine can run several venues at once: two routes, two books, one risk
     budget, and a refusal to let one symbol exist on two brokers.
"""
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace

from apex.rehearsal import LoopbackVenue, VenueConfig, as_text, rehearse_deriv


class TestDerivRehearsal(unittest.TestCase):
    def test_the_production_adapter_trades_over_a_socket(self):
        report = rehearse_deriv(symbol="R_25")
        self.assertTrue(report["ok"], as_text(report))
        self.assertFalse(report["dry_run"], "the rehearsal must exercise the transmitting path")
        names = {s["name"] for s in report["steps"]}
        for expected in ("connect", "account", "ticks", "submit", "positions", "close"):
            self.assertIn(expected, names)
        # the venue saw the whole order lifecycle, in order
        for method in ("proposal", "buy", "portfolio", "sell"):
            self.assertIn(method, report["requests"])
        self.assertLess(report["elapsed_s"], 20.0)

    def test_sl_and_tp_reach_the_venue_proposal(self):
        report = rehearse_deriv(symbol="R_50")
        step = next(s for s in report["steps"] if s["name"] == "sl_tp_forwarded")
        self.assertTrue(step["ok"], step["detail"])
        self.assertIn("stop_loss", step["detail"])
        self.assertIn("take_profit", step["detail"])

    def test_the_documented_limitation_is_still_true(self):
        """If someone implements Deriv SL/TP modification, this test should fail."""
        report = rehearse_deriv()
        step = next(s for s in report["steps"] if s["name"] == "modify_refused")
        self.assertTrue(step["ok"], step["detail"])

    def test_a_venue_that_refuses_the_order_fails_the_rehearsal(self):
        cfg = VenueConfig(fail_on="buy", reject_code="ContractBuyValidationError")
        report = rehearse_deriv(venue_cfg=cfg)
        self.assertFalse(report["ok"])
        submit = next(s for s in report["steps"] if s["name"] == "submit")
        self.assertFalse(submit["ok"])
        self.assertIn("ContractBuyValidationError", submit["detail"])

    def test_unknown_symbol_is_rejected_by_the_venue_not_ignored(self):
        report = rehearse_deriv(symbol="ZZ_999")
        self.assertFalse(report["ok"])
        self.assertTrue(any(not s["ok"] for s in report["steps"]))

    def test_the_report_lands_on_disk_with_its_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = rehearse_deriv(out_dir=tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, "rehearsal.json")))
            with open(os.path.join(tmp, "rehearsal.json"), encoding="utf-8") as fh:
                written = json.load(fh)
            self.assertIn("not against Deriv", written["caveat"])
            self.assertEqual(written["ok"], report["ok"])


class TestVenue(unittest.TestCase):
    def test_the_venue_only_answers_symbols_it_quotes(self):
        venue = LoopbackVenue(replace(VenueConfig(), symbols=("R_25",)))
        with self.assertRaises(KeyError):
            venue._proposal({"symbol": "EURUSD", "amount": 1.0})

    def test_sell_of_an_unknown_contract_is_an_error(self):
        venue = LoopbackVenue()
        with self.assertRaises(KeyError):
            venue._sell("nope")


class TestMultiVenueBook(unittest.TestCase):
    def _args(self, **kw):
        import types
        base = dict(symbols=None, broker=None, mode=None)
        base.update(kw)
        return types.SimpleNamespace(**base)

    def test_venues_resolve_from_mode_and_list(self):
        from apex.live import LiveConfig
        self.assertEqual(LiveConfig(mode="paper").live_venues(), [])
        self.assertEqual(LiveConfig(mode="deriv").live_venues(), ["deriv"])
        self.assertEqual(LiveConfig(mode="multi").live_venues(), ["deriv", "mt5"])
        self.assertEqual(LiveConfig(mode="mt5", venues=["mt5"]).live_venues(), ["mt5"])
        self.assertEqual(LiveConfig(mode="deriv", venues=["deriv", "mt5"]).live_venues(),
                         ["deriv", "mt5"])
        # a venue name that is not a venue is dropped, not silently traded
        self.assertEqual(LiveConfig(mode="deriv", venues=["deriv", "cme"]).live_venues(),
                         ["deriv"])

    def test_per_venue_symbol_lists_stay_with_their_venue(self):
        from apex.cli import _book_for
        book = _book_for(self._args(symbols="deriv:R_10,R_50 mt5:EURUSD,USDJPY"), "both")
        self.assertEqual(book["deriv"], ["R_10", "R_50"])
        self.assertEqual(book["mt5"], ["EURUSD", "USDJPY"])

    def test_the_mt5_book_does_not_default_to_deriv_synthetics(self):
        from apex.cli import _book_for
        book = _book_for(self._args(symbols=None), "mt5")
        self.assertIn("EURUSD", book["mt5"])
        self.assertFalse([s for s in book["mt5"] if s.startswith("R_")])

    def test_one_symbol_on_two_brokers_is_refused(self):
        from apex.live import LiveConfig, LiveRunner
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LiveConfig(mode="paper", symbols_by_broker={"deriv": ["R_10"],
                                                              "mt5": ["R_10"]},
                             store_path=os.path.join(tmp, "a.db"),
                             memory_path=os.path.join(tmp, "a.json"),
                             persist_state=False)
            with self.assertRaises(ValueError) as ctx:
                LiveRunner(cfg)
            self.assertIn("one state per symbol", str(ctx.exception))

    def test_one_engine_trades_two_books_and_keeps_them_apart(self):
        from apex.live import LiveConfig, LiveRunner
        with tempfile.TemporaryDirectory() as tmp:
            cfg = LiveConfig(mode="paper", tf="M5",
                             symbols_by_broker={"deriv": ["R_10", "R_25"],
                                                "mt5": ["EURUSD", "XAUUSD"]},
                             store_path=os.path.join(tmp, "a.db"),
                             memory_path=os.path.join(tmp, "a.json"),
                             bar_seconds=0.02, seed=7, preload_bars=220,
                             warmup_bars=60, persist_state=False, max_open_positions=6)
            runner = LiveRunner(cfg)
            try:
                self.assertEqual([r.broker for r in runner.routes], ["deriv", "mt5"])
                accounts = {a.account for a in runner.router.accounts()}
                self.assertEqual(len(accounts), 2, "each book needs its own account")
                runner.warmup()
                runner.start_background()
                deadline = time.time() + 8
                while time.time() < deadline and len(runner.engine.trades) < 3:
                    time.sleep(0.05)
                snap = runner.snapshot()
                self.assertEqual(len(runner.engine.states), 4)
                self.assertGreater(snap["counters"]["orders_sent"], 0)
                brokers = {t.broker for t in runner.engine.trades}
                self.assertTrue(brokers <= {"deriv", "mt5"})
                # the risk budget is shared: never more open than the ceiling
                self.assertLessEqual(len(runner.engine.positions), 6)
            finally:
                runner.stop()
                runner.shutdown()

    def test_serve_refuses_a_venue_this_host_cannot_run(self):
        """Two venues means MT5, and MT5 means Windows: say so, exit 2, no traceback."""
        import contextlib
        import io
        import sys

        from apex.cli import main
        if sys.platform.startswith("win"):
            self.skipTest("on Windows the MT5 check passes")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["serve", "--venue", "deriv", "--venue", "mt5", "--port", "8096"])
        self.assertEqual(code, 2)
        self.assertIn("MetaTrader5", buf.getvalue())
        self.assertNotIn("Traceback", buf.getvalue())

    def test_serve_refuses_venues_with_a_paper_mode(self):
        import contextlib
        import io

        from apex.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["serve", "--demo", "--venue", "deriv"])
        self.assertEqual(code, 2)
        self.assertIn("--venue runs live venues", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
