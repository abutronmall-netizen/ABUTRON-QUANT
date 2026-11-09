"""Economic calendar: parsing, blackout windows, prop-guard integration."""
import json
import os
import tempfile
import time
import unittest

from apex.models import Candidate, Decision, Side
from apex.news import NewsCalendar, NewsEvent, demo_calendar, parse_ts
from apex.prop import PropGuard, get_profile


class TestParsing(unittest.TestCase):
    def test_epoch_and_iso_timestamps(self):
        self.assertEqual(parse_ts(1_700_000_000), 1_700_000_000.0)
        self.assertEqual(parse_ts("2023-11-14T22:13:20Z"), 1_700_000_000.0)
        self.assertEqual(parse_ts("2023-11-14 22:13:20"), 1_700_000_000.0)

    def test_loads_list_and_object_shapes(self):
        rows = [{"ts": 1_700_000_000, "currency": "usd", "impact": "HIGH", "title": "CPI"}]
        tmp = tempfile.mkdtemp()
        for payload in (rows, {"events": rows}):
            path = os.path.join(tmp, f"cal_{len(os.listdir(tmp))}.json")
            with open(path, "w") as fh:
                json.dump(payload, fh)
            cal = NewsCalendar.load(path)
            self.assertEqual(len(cal), 1)
            self.assertEqual(cal.events[0].currency, "USD")
            self.assertEqual(cal.events[0].impact, "high")

    def test_bad_rows_are_skipped_not_fatal(self):
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "cal.json")
        with open(path, "w") as fh:
            json.dump([{"ts": "not-a-date"}, {"ts": 1_700_000_000}, {"nope": 1}], fh)
        self.assertEqual(len(NewsCalendar.load(path)), 1)


class TestWindows(unittest.TestCase):
    def setUp(self):
        self.t = 1_700_000_000.0
        self.cal = NewsCalendar([
            NewsEvent(self.t, "USD", "high", "CPI"),
            NewsEvent(self.t + 3600, "EUR", "medium", "PMI"),
            NewsEvent(self.t + 7200, "EUR", "high", "ECB"),
        ])

    def test_high_impact_only_by_default(self):
        wins = self.cal.windows(self.t - 86400, self.t + 86400, minutes=2)
        self.assertEqual(len(wins), 2)
        self.assertEqual(wins[0], (self.t - 120, self.t + 120))

    def test_window_padding_scales_with_minutes(self):
        wins = self.cal.windows(self.t - 60, self.t + 60, minutes=15)
        self.assertEqual(wins[0], (self.t - 900, self.t + 900))

    def test_currency_filter(self):
        wins = self.cal.windows(self.t - 86400, self.t + 86400, minutes=2, currencies=["EUR"])
        self.assertEqual(len(wins), 1)
        self.assertEqual(wins[0][0], self.t + 7200 - 120)

    def test_range_clipping(self):
        self.assertEqual(self.cal.windows(self.t + 1000, self.t + 2000), [])

    def test_blackout_and_next_event(self):
        self.assertTrue(self.cal.in_blackout(self.t + 60, minutes=2))
        self.assertFalse(self.cal.in_blackout(self.t + 600, minutes=2))
        self.assertEqual(self.cal.next_event(self.t + 1).title, "ECB")

    def test_demo_calendar_is_in_the_future(self):
        cal = demo_calendar(now=self.t)
        self.assertEqual(len(cal.windows(self.t, self.t + 86400)), len(cal))


class TestPropIntegration(unittest.TestCase):
    def test_guard_blocks_inside_a_news_window(self):
        now = 1_700_000_000.0
        guard = PropGuard(get_profile("ftmo_2step"), 10_000.0, now=now,
                          block_news_windows=[(now - 120, now + 120)])
        cand = Candidate(symbol="R_25", side=Side.BUY, conviction=0.6, price=2400.0,
                         sl=2390.0, tp=2420.0, ttl_s=60, strategy="scalp_balanced",
                         regime="trend_up")
        risk = Decision.allow(1.0, {"risk_money": 20.0, "stop_distance": 10.0,
                                    "per_lot_risk": 10.0})
        blocked = guard.check(cand, risk, now, open_risk_money=0.0,
                              notional_per_lot=2400.0)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.code, "PROP_NEWS")
        # outside the window the same candidate is accepted
        after = now + 600
        guard.on_equity(after, 10_000.0)
        ok = guard.check(cand, risk, after, open_risk_money=0.0, notional_per_lot=2400.0)
        self.assertTrue(ok.allowed)


if __name__ == "__main__":
    unittest.main()
