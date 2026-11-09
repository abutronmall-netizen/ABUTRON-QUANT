import unittest

from apex.models import Candidate, Decision, Side
from apex.prop import (DrawdownType, PropChallenge, PropGuard, PropProfile, Verdict,
                       ftmo_phases, get_profile, plan_for)

T0 = 1_700_000_000.0
DAY = 86400.0


def cand(price=100.0, sl=99.0, side=Side.BUY):
    return Candidate(symbol="R_25", side=side, conviction=0.5, price=price, sl=sl,
                     tp=price + 2, ttl_s=300, strategy="t", regime="trend_up")


def allow(lots=1.0, per_lot_risk=1.0):
    return Decision.allow(lots, {"per_lot_risk": per_lot_risk})


class TestProfile(unittest.TestCase):
    def test_registry(self):
        for name in ("ftmo_2step", "fundednext_starter", "the5ers_high_stakes", "abutron_inhouse"):
            self.assertEqual(get_profile(name).name, name)

    def test_plan_is_conservative(self):
        p = get_profile("ftmo_2step")
        plan = plan_for(p, 100_000)
        self.assertLessEqual(plan["risk_per_trade_pct"], p.max_daily_loss_pct / 6 + 1e-9)
        self.assertGreaterEqual(plan["required_daily_profit"], 0)


class TestPropGuard(unittest.TestCase):
    def make(self, **kw):
        profile = PropProfile(name="test", profit_target_pct=8.0, max_daily_loss_pct=4.0,
                              max_total_loss_pct=8.0, min_trading_days=3, max_calendar_days=30,
                              consistency_pct=0.0, max_lots=2.0, risk_per_trade_pct=0.5)
        for k, v in kw.items():
            setattr(profile, k, v)
        return PropGuard(profile, 10_000.0, now=T0)

    # ---- breach detection ------------------------------------------- #
    def test_breach_on_total_loss(self):
        g = self.make()
        g.on_equity(T0 + 60, 9_150.0)
        self.assertTrue(g.st.breach)
        self.assertEqual(g.status(T0 + 60)["verdict"], Verdict.BREACHED.value)

    def test_breach_on_daily_loss(self):
        g = self.make()
        g.on_equity(T0 + 60, 9_550.0)      # -4.5% on day one
        self.assertIn("daily", g.st.breach)

    def test_trailing_vs_static_drawdown(self):
        g = self.make(drawdown_type=DrawdownType.TRAILING)
        g.on_equity(T0 + 60, 11_000.0)
        g.on_equity(T0 + 120, 10_050.0)    # -8.6% from peak but above static floor
        self.assertTrue(g.st.breach)

        g2 = self.make(drawdown_type=DrawdownType.STATIC)
        g2.on_equity(T0 + 60, 11_000.0)
        g2.on_equity(T0 + 120, 10_050.0)
        self.assertFalse(g2.st.breach)

    def test_calendar_limit(self):
        g = self.make()
        g.on_equity(T0 + 31 * DAY, 10_050.0)
        self.assertIn("time limit", g.st.breach)

    # ---- passing ---------------------------------------------------- #
    def test_pass_requires_min_days(self):
        g = self.make(min_trading_days=3)
        g.on_equity(T0 + 60, 10_900.0)     # +9% target hit, day 1
        self.assertFalse(g.st.passed)
        self.assertEqual(g.st.flat_required, "target_hit_min_days")
        for d in (0, 1, 2):
            g.st.days_traded.add(g._day_index(T0 + d * DAY))
            g.on_equity(T0 + d * DAY + 60, 10_900.0)
        self.assertTrue(g.st.passed)
        self.assertEqual(g.status(T0 + 3 * DAY)["verdict"], Verdict.PASSED.value)

    def test_consistency_rule_blocks_pass(self):
        g = self.make(min_trading_days=1, consistency_pct=40.0)
        # one huge day = 100% of profit -> cannot pass
        g.on_equity(T0 + 60, 10_900.0)
        self.assertFalse(g.st.passed)
        self.assertEqual(g.status(T0 + 60)["verdict"], Verdict.TARGET_HIT.value)

    # ---- gating ----------------------------------------------------- #
    def test_blocks_after_breach(self):
        g = self.make()
        g.on_equity(T0 + 60, 9_000.0)
        d = g.check(cand(), allow(), T0 + 60)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "PROP_BREACHED")

    def test_daily_budget_hard_stop(self):
        g = self.make()                     # 4% daily limit -> 400 USD
        g.on_equity(T0 + 60, 9_680.0)       # 3.2% used = 80% of budget
        d = g.check(cand(), allow(1.0, 1.0), T0 + 60)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "PROP_DAY_BUDGET")

    def test_single_trade_risk_cap(self):
        g = self.make()
        g.on_equity(T0 + 60, 9_900.0)       # 1% used, 300 left
        # ask for a 1-lot trade risking 1000 -> must be cut to <= 35% of 300 = 105
        d = g.check(cand(), allow(1000.0, 1.0), T0 + 60)
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.lots * 1.0, 105.0 + 1e-6)

    def test_aggregate_open_risk_cap(self):
        g = self.make()
        # 60% of the 400 USD daily budget is the cap for total open risk.
        d = g.check(cand(), allow(50.0, 1.0), T0 + 60, open_risk_money=300.0)
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "PROP_OPEN_RISK")

        d2 = g.check(cand(), allow(50.0, 1.0), T0 + 60, open_risk_money=200.0)
        self.assertTrue(d2.allowed)
        self.assertLessEqual(d2.lots, 40.0 + 1e-6)   # 240-200 = 40 USD of room

    def test_lot_cap_scales_by_exposure(self):
        g = self.make(max_lots=2.0)
        fx = g.check(cand(price=1.08, sl=1.0775), allow(5.0, 10.0), T0 + 60,
                     notional_per_lot=108_000.0)
        self.assertTrue(fx.allowed)
        self.assertLessEqual(fx.lots, 2.0)
        deriv = g.check(cand(price=3200, sl=3197.5), allow(20.0, 1.0), T0 + 60,
                        notional_per_lot=3200.0)
        self.assertTrue(deriv.allowed)
        self.assertGreater(deriv.lots, 2.0)   # exposure-equivalent cap, not raw units

    def test_news_blackout(self):
        g = PropGuard(get_profile("ftmo_2step"), 10_000, now=T0,
                      block_news_windows=[(T0 + 3600, 1)])
        d = g.check(cand(), allow(), T0 + 3600)
        self.assertEqual(d.code, "PROP_NEWS")

    def test_stop_loss_required(self):
        g = self.make()
        c = cand()
        c.sl = c.price
        d = g.check(c, allow(), T0 + 60)
        self.assertEqual(d.code, "PROP_NO_STOP")

    def test_flatten_before_daily_reset(self):
        g = self.make(daily_reset_hour_utc=0)
        end_of_day = T0 - (T0 % DAY) + DAY - 300
        self.assertEqual(g.should_flatten(end_of_day, True), "eod")
        self.assertIsNone(g.should_flatten(T0 + 3600, True))

    def test_pacing_recommends_bounded_risk(self):
        g = self.make()
        g.on_equity(T0 + 60, 9_900.0)
        pac = g.pacing(T0 + 60)
        self.assertGreaterEqual(pac["recommended_risk_pct"], 0.1)
        self.assertLessEqual(pac["recommended_risk_pct"], g.p.max_risk_per_trade_pct)
        self.assertGreater(pac["daily_target"], 0)


class TestPropChallenge(unittest.TestCase):
    def test_advances_through_phases(self):
        ch = PropChallenge(ftmo_phases(10_000), 10_000, now=T0)
        self.assertEqual(ch.index, 0)
        self.assertEqual(ch.phase.profit_target_pct, 10.0)
        # pass phase 1
        ch.guard.st.days_traded = {ch.guard._day_index(T0), ch.guard._day_index(T0 + DAY),
                                   ch.guard._day_index(T0 + 2 * DAY),
                                   ch.guard._day_index(T0 + 3 * DAY)}
        ch.guard.on_equity(T0 + 4 * DAY, 11_050.0)
        self.assertTrue(ch.guard.st.passed)
        evt = ch.sync(now=T0 + 4 * DAY)
        self.assertIn("phase passed", evt)
        self.assertEqual(ch.index, 1)
        self.assertEqual(ch.phase.profit_target_pct, 5.0)
        self.assertAlmostEqual(ch.balance, 11_050.0)

    def test_funded_phase_has_no_target(self):
        phases = ftmo_phases()
        self.assertEqual(phases[-1].profit_target_pct, 0.0)
        ch = PropChallenge(phases, 11_050, now=T0)
        ch.index = 2
        ch.reset(balance=11_050, now=T0)
        self.assertFalse(ch.guard.st.passed)


if __name__ == "__main__":
    unittest.main()
