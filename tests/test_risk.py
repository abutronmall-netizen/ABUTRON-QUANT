import unittest

from apex.execution.base import SymbolRegistry
from apex.models import AccountState, Candidate, Position, Side
from apex.risk import RiskConfig, RiskEngine


def acct(equity=10_000.0, free=None):
    free = equity if free is None else free
    return AccountState(broker="sim", account="A", equity=equity, balance=equity,
                        free_margin=free)


def cand(symbol="R_25", price=3000.0, sl=2997.5, side=Side.BUY, spread_atr=0.05):
    return Candidate(symbol=symbol, side=side, conviction=0.5, price=price, sl=sl,
                     tp=price + 2 * (price - sl), ttl_s=300, strategy="t", regime="trend_up",
                     meta={"spread_atr": spread_atr})


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.reg = SymbolRegistry()
        self.re = RiskEngine(RiskConfig(risk_per_trade_pct=0.5), initial_equity=10_000, now=0)
        self.spec = self.reg.spec("deriv", "R_25")

    def test_fixed_fractional_sizing(self):
        d = self.re.size(cand(), self.spec, acct())
        self.assertTrue(d.allowed)
        # risk 50 USD over a 2.5 point stop at 1 USD/point/lot -> 20 lots
        self.assertAlmostEqual(d.lots, 20.0, places=6)
        self.assertAlmostEqual(d.detail["risk_money"], 50.0)

    def test_risk_scales_with_equity(self):
        d1 = self.re.size(cand(), self.spec, acct(10_000))
        d2 = self.re.size(cand(), self.spec, acct(20_000))
        self.assertAlmostEqual(d2.lots, d1.lots * 2, places=4)

    def test_below_min_lot_is_rejected_not_rounded_up(self):
        re_ = RiskEngine(RiskConfig(risk_per_trade_pct=0.001), initial_equity=10_000, now=0)
        d = re_.size(cand(price=1.08, sl=1.075), self.reg.spec("mt5", "EURUSD"), acct())
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "LOT_BELOW_MIN")

    def test_margin_cap(self):
        d = self.re.size(cand(), self.spec, acct(10_000, free=100))
        self.assertTrue(d.allowed)
        self.assertLess(d.lots, 20)

    def test_zero_stop_rejected(self):
        d = self.re.size(cand(sl=3000.0), self.spec, acct())
        self.assertFalse(d.allowed)
        self.assertEqual(d.code, "BAD_SIZING")


class TestRiskGates(unittest.TestCase):
    def setUp(self):
        self.reg = SymbolRegistry()
        self.spec = self.reg.spec("deriv", "R_25")
        self.cfg = RiskConfig(risk_per_trade_pct=0.5, max_daily_loss_pct=3.0,
                              max_total_drawdown_pct=8.0, max_trades_per_day=3,
                              max_consecutive_losses=2, cooldown_after_loss_s=60,
                              session_start_utc=0, session_end_utc=24, trade_on_weekends=True)
        self.re = RiskEngine(self.cfg, initial_equity=10_000, now=1_700_000_000)

    def _eval(self, now=1_700_000_100, eq=10_000, positions=None):
        return self.re.evaluate(cand(), self.spec, acct(eq), positions or [], now)

    def test_kill_switch_blocks(self):
        self.re.cfg.kill_switch = True
        self.assertEqual(self._eval().code, "KILL_SWITCH")

    def test_daily_loss_halts(self):
        self.re.on_equity(9_600, 1_700_000_100)     # -4% on the day
        self.assertTrue(self.re.state.halted)
        self.assertEqual(self.re.state.halt_kind, "daily")
        self.assertFalse(self._eval(eq=9_600).allowed)

    def test_max_drawdown_halts_and_is_sticky(self):
        self.re.on_equity(12_000, 1_700_000_100)
        self.re.on_equity(10_900, 1_700_000_200)     # -9.2% from peak
        self.assertTrue(self.re.state.halted)
        self.assertEqual(self.re.state.halt_kind, "drawdown")
        # next day: daily/streak halts clear, drawdown halts do not
        self.re.on_equity(10_900, 1_700_000_100 + 86400)
        self.assertTrue(self.re.state.halted)

    def test_daily_halt_clears_next_day(self):
        self.re.on_equity(9_500, 1_700_000_100)
        self.assertTrue(self.re.state.halted)
        self.re.on_equity(9_500, 86_400 * 20_000)
        self.assertFalse(self.re.state.halted)

    def test_consecutive_losses_halt(self):
        self.re.on_trade_closed(-10, 1_700_000_100)
        self.re.on_trade_closed(-10, 1_700_000_200)
        self.assertTrue(self.re.state.halted)
        self.assertEqual(self.re.state.halt_kind, "streak")

    def test_cooldown_after_loss(self):
        self.re.on_trade_closed(-10, 1_700_000_100)
        self.assertEqual(self._eval(now=1_700_000_120).code, "COOLDOWN")
        self.assertTrue(self._eval(now=1_700_000_300).allowed)

    def test_max_trades_per_day(self):
        for i in range(3):
            self.assertTrue(self._eval(now=1_700_000_200 + i).allowed)
        self.assertEqual(self._eval(now=1_700_000_300).code, "MAX_TRADES_DAY")

    def test_symbol_already_held(self):
        pos = Position(id="1", symbol="R_25", side=Side.BUY, lots=1, entry=3000, sl=2990,
                       tp=3020, opened_ts=0, broker="sim", account="A", strategy="t")
        self.assertEqual(self._eval(positions=[pos]).code, "SYMBOL_HELD")

    def test_session_filter(self):
        self.re.cfg.session_start_utc = 8
        self.re.cfg.session_end_utc = 16
        self.assertEqual(self._eval(now=1_700_000_000 + 3 * 3600).code, "SESSION_CLOSED")

    def test_spread_gate(self):
        d = self.re.evaluate(cand(spread_atr=0.9), self.spec, acct(), [], 1_700_000_100)
        self.assertEqual(d.code, "SPREAD")

    def test_rejections_are_counted(self):
        self.re.cfg.kill_switch = True
        self._eval()
        self._eval()
        self.assertEqual(self.re.state.rejections.get("KILL_SWITCH"), 2)


if __name__ == "__main__":
    unittest.main()
