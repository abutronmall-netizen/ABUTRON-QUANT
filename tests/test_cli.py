"""CLI surface: every documented command must at least run and exit cleanly."""
import contextlib
import io
import os
import tempfile
import unittest

from apex.cli import _broker_key, _prop_arg, _tf_seconds, build_parser, main


def run(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


class TestParser(unittest.TestCase):
    def test_all_commands_exist(self):
        parser = build_parser()
        actions = [a for a in parser._actions if getattr(a, "choices", None)]
        names = set(actions[0].choices)
        for cmd in ("serve", "backtest", "sweep", "propsim", "profiles", "doctor",
                    "fetch", "news", "report", "ai"):
            self.assertIn(cmd, names)

    def test_serve_defaults_to_localhost_and_dry_run(self):
        args = build_parser().parse_args(["serve", "--demo"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertFalse(args.live)
        self.assertEqual(args.boot_policy, "adopt")
        self.assertFalse(args.no_persist)

    def test_prop_argument_semantics(self):
        self.assertEqual(_prop_arg(None), "__config__")
        self.assertIsNone(_prop_arg("none"))
        self.assertIsNone(_prop_arg("off"))
        self.assertEqual(_prop_arg("ftmo_2step"), "ftmo_2step")
        self.assertEqual(_prop_arg("config/prop_ftmo_custom.json"),
                         "config/prop_ftmo_custom.json")

    def test_timeframe_seconds(self):
        self.assertEqual(_tf_seconds("M5"), 300)
        self.assertEqual(_tf_seconds("H1"), 3600)
        self.assertEqual(_tf_seconds("??"), 300)


class TestCommands(unittest.TestCase):
    def test_profiles_lists_rule_sets(self):
        code, out = run(["profiles"])
        self.assertEqual(code, 0)
        for name in ("ftmo_2step", "abutron_inhouse"):
            self.assertIn(name, out)

    def test_news_reads_a_calendar(self):
        code, out = run(["news", "--file", "config/news_sample.json", "--days", "7"])
        self.assertEqual(code, 0)
        self.assertIn("blackout window", out)

    def test_doctor_runs(self):
        code, out = run(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("engine self-test", out)

    def test_backtest_writes_artifacts(self):
        tmp = tempfile.mkdtemp()
        code, out = run(["backtest", "--symbols", "R_25", "--bars", "600",
                         "--out", tmp, "--prop", "abutron_inhouse"])
        self.assertEqual(code, 0)
        self.assertIn("trades", out)
        self.assertTrue(os.path.exists(os.path.join(tmp, "report.md")))
        self.assertTrue(os.path.exists(os.path.join(tmp, "summary.json")))

    def test_report_summarises_a_written_run(self):
        tmp = tempfile.mkdtemp()
        code, _ = run(["backtest", "--symbols", "R_25", "--bars", "500", "--out", tmp])
        self.assertEqual(code, 0)
        code, out = run(["report", "--run", tmp])
        self.assertEqual(code, 0)
        self.assertIn("trades", out)
        self.assertIn("maxDD", out)
        self.assertEqual(run(["report", "--run", os.path.join(tmp, "nope")])[0], 1)

    def test_serve_accepts_the_ai_flag(self):
        args = build_parser().parse_args(["serve", "--demo", "--ai", "veto"])
        self.assertEqual(args.ai, "veto")

    def test_a_missing_config_is_a_message_and_exit_2(self):
        code, out = run(["serve", "--config", "/tmp/definitely-not-here.json"])
        self.assertEqual(code, 2)
        self.assertIn("config not found", out)
        self.assertNotIn("Traceback", out)

    def test_mt5_mode_refuses_on_a_host_without_the_package(self):
        try:
            import MetaTrader5  # noqa: F401
        except ImportError:
            pass
        else:                                   # pragma: no cover - Windows CI only
            self.skipTest("MetaTrader5 is installed here")
        code, out = run(["serve", "--mode", "mt5", "--port", "0"])
        self.assertEqual(code, 2)
        self.assertIn("--mode mt5", out)
        self.assertIn("--mode paper", out)

    def test_the_symbol_book_follows_the_mode(self):
        args = build_parser().parse_args(["serve", "--demo"])
        self.assertEqual(_broker_key(args), "deriv")
        args = build_parser().parse_args(["serve", "--mode", "mt5"])
        self.assertEqual(_broker_key(args), "mt5")
        args = build_parser().parse_args(["serve", "--mode", "mt5", "--broker", "deriv"])
        self.assertEqual(_broker_key(args), "deriv")
        args = build_parser().parse_args(["backtest"])
        self.assertEqual(_broker_key(args), "deriv")

    def test_slippage_reports_and_exits_nonzero_without_fills(self):
        tmp = tempfile.mkdtemp()
        code, out = run(["slippage", "--run", os.path.join(tmp, "nothing")])
        self.assertEqual(code, 2)
        self.assertIn("slippage study", out)
        self.assertIn("no journal", out)

    def test_backup_then_restore_round_trips(self):
        tmp = tempfile.mkdtemp()
        bt = os.path.join(tmp, "bt")
        run(["backtest", "--symbols", "R_25", "--bars", "300", "--out", bt])
        out_dir = os.path.join(tmp, "bk")
        code, out = run(["backup", "--run", bt, "--out", out_dir])
        self.assertEqual(code, 0)
        self.assertIn("sha256", out)
        backups = [f for f in os.listdir(out_dir) if f.endswith(".db")]
        self.assertEqual(len(backups), 1)
        target = os.path.join(tmp, "restored", "apex.db")
        code, out = run(["restore", "--backup", os.path.join(out_dir, backups[0]),
                         "--db", target])
        self.assertEqual(code, 0)
        self.assertIn("restored", out)
        self.assertTrue(os.path.exists(target))

    def test_restore_without_force_refuses(self):
        tmp = tempfile.mkdtemp()
        bt = os.path.join(tmp, "bt")
        run(["backtest", "--symbols", "R_25", "--bars", "200", "--out", bt])
        out_dir = os.path.join(tmp, "bk")
        run(["backup", "--run", bt, "--out", out_dir])
        name = [f for f in os.listdir(out_dir) if f.endswith(".db")][0]
        code, out = run(["restore", "--backup", os.path.join(out_dir, name), "--run", bt])
        self.assertEqual(code, 2)
        self.assertIn("--force", out)

    def test_fetch_without_mt5_exits_with_a_clear_code(self):
        code, out = run(["fetch", "--broker", "mt5", "--symbol", "EURUSD", "--bars", "10"])
        self.assertEqual(code, 2)
        self.assertIn("MT5 unavailable", out)


if __name__ == "__main__":
    unittest.main()
