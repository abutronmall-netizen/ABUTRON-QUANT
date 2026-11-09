"""CLI surface: every documented command must at least run and exit cleanly."""
import contextlib
import io
import os
import tempfile
import unittest

from apex.cli import _prop_arg, _tf_seconds, build_parser, main


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

    def test_fetch_without_mt5_exits_with_a_clear_code(self):
        code, out = run(["fetch", "--broker", "mt5", "--symbol", "EURUSD", "--bars", "10"])
        self.assertEqual(code, 2)
        self.assertIn("MT5 unavailable", out)


if __name__ == "__main__":
    unittest.main()
