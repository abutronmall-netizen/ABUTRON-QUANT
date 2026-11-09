"""Slippage study, journal backup/restore, and the mail transport.

The first two exist because "the demo week looked fine" is not a measurement:
one turns journal rows into a cost distribution, the other makes the evidence
recoverable. The third closes a path that shipped untested.
"""
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from apex.backup import BackupError, create_backup, restore_backup
from apex.slippage import SlippageConfig, as_text, study
from apex.store import Store


# --------------------------------------------------------------------------- #
def make_journal(rows: list[dict]) -> str:
    """A journal with `orders` rows shaped the way the store writes them."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex.db")
    store = Store(path)
    for row in rows:
        store.conn.execute(
            "INSERT INTO orders (client_id,ts,broker,account,symbol,side,lots,strategy,ok,"
            "filled_price,reason,order_json,ack_json,requested_price) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row["client_id"], row.get("ts", time.time()), row.get("broker", "deriv"),
             "ACC-1", row["symbol"], row.get("side", "BUY"), 1.0, "scalp_balanced",
             1 if row.get("ok", True) else 0, row.get("filled"), "",
             json.dumps({"price": row.get("requested"), "sl": row.get("sl")}), "{}",
             row.get("requested")))
    store.conn.commit()
    store.close()
    return path


class TestSlippageStudy(unittest.TestCase):
    def test_buy_filled_above_the_request_is_adverse(self):
        path = make_journal([
            {"client_id": "a", "symbol": "R_25", "requested": 2500.0, "filled": 2500.25, "sl": 2490.0},
            {"client_id": "b", "symbol": "R_25", "requested": 2500.0, "filled": 2500.50, "sl": 2490.0},
            {"client_id": "c", "symbol": "R_50", "requested": 250.0, "filled": 250.025, "sl": 249.0},
            {"client_id": "d", "symbol": "R_50", "requested": 250.0, "filled": 250.025, "sl": 249.0},
            {"client_id": "e", "symbol": "R_75", "requested": 48000.0, "filled": 48048.0, "sl": 47900.0},
        ])
        rep = study(SlippageConfig(db_path=path))
        self.assertEqual(rep["fills"], 5)
        self.assertAlmostEqual(rep["overall"]["median_bps"], 1.0, places=2)
        self.assertEqual(rep["verdict"], "under-modelled")   # 1.0 bps > 0.3 modelled
        self.assertGreater(rep["overall"]["mean_r_cost"], 0)
        self.assertIn("R_25", rep["by_symbol"])
        self.assertIn("median", as_text(rep))

    def test_sell_filled_below_the_request_is_adverse(self):
        path = make_journal([
            {"client_id": f"s{i}", "symbol": "R_25", "side": "SELL",
             "requested": 2500.0, "filled": 2499.0, "sl": 2510.0} for i in range(6)
        ])
        rep = study(SlippageConfig(db_path=path))
        self.assertAlmostEqual(rep["overall"]["median_bps"], 4.0, places=2)

    def test_a_fortunate_fill_is_negative_and_inside_the_model(self):
        path = make_journal([
            {"client_id": f"f{i}", "symbol": "R_25", "requested": 2500.0,
             "filled": 2499.9, "sl": 2490.0} for i in range(6)
        ])
        rep = study(SlippageConfig(db_path=path))
        self.assertLess(rep["overall"]["median_bps"], 0)
        self.assertEqual(rep["verdict"], "inside model")

    def test_too_few_fills_is_not_a_measurement(self):
        path = make_journal([
            {"client_id": "x", "symbol": "R_25", "requested": 2500.0, "filled": 2501.0},
        ])
        rep = study(SlippageConfig(db_path=path))
        self.assertEqual(rep["verdict"], "insufficient sample")
        self.assertIn("noise", rep["detail"])

    def test_unfilled_and_priceless_orders_are_ignored(self):
        path = make_journal([
            {"client_id": "ok", "symbol": "R_25", "requested": 2500.0, "filled": 2500.1},
            {"client_id": "rejected", "symbol": "R_25", "requested": 2500.0,
             "filled": None, "ok": False},
            {"client_id": "noprice", "symbol": "R_25", "requested": None, "filled": 2500.1},
        ])
        rep = study(SlippageConfig(db_path=path))
        self.assertEqual(rep["fills"], 1)

    def test_missing_journal_is_a_report_not_a_crash(self):
        rep = study(SlippageConfig(db_path="/tmp/does-not-exist-anywhere.db"))
        self.assertEqual(rep["fills"], 0)
        self.assertIn("no journal", rep["detail"])
        # a missing journal still answers with the full shape
        self.assertEqual(rep["verdict"], "no data")
        self.assertEqual(rep["overall"]["n"], 0)
        self.assertEqual((rep["by_symbol"], rep["by_broker"], rep["by_side"]), ({}, {}, {}))
        self.assertEqual(rep["recent"], [])

    def test_the_study_never_writes_to_the_journal(self):
        path = make_journal([{"client_id": "a", "symbol": "R_25",
                              "requested": 2500.0, "filled": 2500.1}])
        before = os.path.getsize(path)
        mtime = os.path.getmtime(path)
        study(SlippageConfig(db_path=path))
        self.assertEqual(os.path.getsize(path), before)
        self.assertEqual(os.path.getmtime(path), mtime)


class TestBackupRestore(unittest.TestCase):
    def _journal(self):
        path = make_journal([{"client_id": f"o{i}", "symbol": "R_25",
                              "requested": 2500.0, "filled": 2500.1} for i in range(4)])
        return path

    def test_backup_writes_a_manifest_that_matches_the_file(self):
        src = self._journal()
        out = tempfile.mkdtemp()
        res = create_backup(src, out, keep=5)
        self.assertTrue(os.path.exists(res["path"]))
        self.assertTrue(os.path.exists(res["manifest"]))
        self.assertEqual(res["counts"]["orders"], 4)
        with open(res["manifest"], encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.assertEqual(manifest["sha256"], res["sha256"])
        self.assertEqual(manifest["counts"]["orders"], 4)

    def test_restore_round_trips_every_row(self):
        src = self._journal()
        out = tempfile.mkdtemp()
        res = create_backup(src, out)
        target = os.path.join(tempfile.mkdtemp(), "restored", "apex.db")
        restored = restore_backup(res["path"], target)
        self.assertTrue(restored["verified"])
        self.assertEqual(restored["counts"]["orders"], 4)
        conn = sqlite3.connect(target)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0], 4)
        finally:
            conn.close()

    def test_restore_refuses_to_clobber_without_force(self):
        src = self._journal()
        res = create_backup(src, tempfile.mkdtemp())
        with self.assertRaises(BackupError) as ctx:
            restore_backup(res["path"], src)
        self.assertIn("--force", str(ctx.exception))

    def test_a_tampered_backup_is_refused(self):
        src = self._journal()
        res = create_backup(src, tempfile.mkdtemp())
        with open(res["path"], "ab") as fh:
            fh.write(b"garbage")
        with self.assertRaises(BackupError) as ctx:
            restore_backup(res["path"], os.path.join(tempfile.mkdtemp(), "x.db"))
        self.assertIn("sha256", str(ctx.exception))

    def test_retention_keeps_only_the_newest(self):
        src = self._journal()
        out = tempfile.mkdtemp()
        for i in range(4):
            create_backup(src, out, keep=2, now=1700000000 + i * 60)
        kept = sorted(f for f in os.listdir(out) if f.endswith(".db"))
        self.assertEqual(len(kept), 2)

    def test_missing_journal_is_a_clear_error(self):
        with self.assertRaises(BackupError):
            create_backup("/tmp/nope-not-a-journal.db", tempfile.mkdtemp())


class TestMailTransport(unittest.TestCase):
    """The SMTP path shipped with no test at all until this one."""

    def test_smtp_delivery_is_attempted_and_reported(self):
        from apex.alerts import AlertConfig, Notifier

        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=0):
                sent["host"] = host

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                sent["tls"] = True

            def login(self, user, password):
                sent["login"] = (user, password)

            def send_message(self, msg):
                sent["subject"] = msg["Subject"]
                sent["to"] = msg["To"]

        cfg = AlertConfig(smtp_host="smtp.example.com", smtp_to=["ops@example.com"],
                          smtp_user="apex", smtp_password="secret")
        with mock.patch("smtplib.SMTP", FakeSMTP):
            self.assertTrue(Notifier(cfg).alert("risk", "halted"))
        self.assertEqual(sent["host"], "smtp.example.com")
        self.assertTrue(sent["tls"])
        self.assertEqual(sent["login"], ("apex", "secret"))
        self.assertIn("risk", sent["subject"])

    def test_a_broken_smtp_server_never_raises(self):
        from apex.alerts import AlertConfig, Notifier

        class Boom:
            def __init__(self, *a, **k):
                raise OSError("connection refused")

        cfg = AlertConfig(smtp_host="smtp.example.com", smtp_to=["ops@example.com"])
        with mock.patch("smtplib.SMTP", Boom):
            self.assertFalse(Notifier(cfg).alert("risk", "halted"))


if __name__ == "__main__":
    unittest.main()
