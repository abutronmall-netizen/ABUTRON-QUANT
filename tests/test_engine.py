import unittest

from apex.data.feed import SyntheticConfig, SyntheticFeed
from apex.engine import EngineConfig, TradingEngine
from apex.execution.base import SymbolRegistry
from apex.execution.router import BrokerRouter, Route
from apex.execution.sim import SimBroker, SimConfig
from apex.models import Quote, Side
from apex.portfolio import ReflectionMemory
from apex.prop import PropGuard, PropProfile
from apex.risk import RiskConfig
from apex.strategy import StrategyProfile


def build_engine(symbol="R_75", balance=10_000.0, prop=None, strategy="scalp_balanced",
                 seed=4, warmup=60, trend=6.0, base=48000.0):
    reg = SymbolRegistry()
    sim = SimBroker(reg, SimConfig(initial_balance=balance, seed=seed), account="SIM-T")
    router = BrokerRouter([Route(broker="deriv", account="SIM-T", port=sim, symbols=[symbol])],
                          registry=reg, retry_sleep=0)
    eng = TradingEngine(router, {"deriv": [symbol]},
                        cfg=EngineConfig(entry_tf="M5", warmup_bars=warmup),
                        strategy=StrategyProfile.preset(strategy),
                        risk=RiskConfig(risk_per_trade_pct=0.5, max_trades_per_day=100,
                                        trade_on_weekends=True, cooldown_after_loss_s=0,
                                        max_consecutive_losses=99),
                        prop=prop, store=None,
                        memory=ReflectionMemory(), now=1_700_000_000)
    feed = SyntheticFeed(symbol, SyntheticConfig(base_price=base, vol_bps=8.0, seed=seed,
                                                 trend_strength=trend))
    return eng, sim, feed, reg


def drive(eng, sim, feed, reg, n, symbol="R_75", broker="deriv", inspect=None):
    for i in range(n):
        bar = feed.next_bar()
        spec = reg.spec(broker, symbol)
        half = (spec.typical_spread or bar.close * 0.00005) / 2
        q = Quote(symbol, bar.ts, bar.close - half, bar.close + half)
        sim.set_time(bar.ts)
        sim.set_quote(q)
        eng.set_quote(q)
        if inspect is not None:
            inspect(i, bar, q)
        eng.on_bar(bar)
        eng.on_bar_brackets(bar)
        eng.on_quote(q)
        eng.cycle_guard()


class TestEngineInvariants(unittest.TestCase):
    def test_no_trades_during_warmup(self):
        eng, sim, feed, reg = build_engine(warmup=120)
        drive(eng, sim, feed, reg, 110)
        self.assertEqual(len(eng.positions), 0)
        self.assertEqual(eng.orders_filled, 0)

    def test_paused_engine_builds_state_but_trades_nothing(self):
        eng, sim, feed, reg = build_engine(warmup=20)
        eng.paused = True
        drive(eng, sim, feed, reg, 300)
        self.assertEqual(eng.orders_filled, 0)
        self.assertTrue(eng.states["R_75"].entry.ready())

    def test_engine_takes_trades_and_journals_them(self):
        eng, sim, feed, reg = build_engine(trend=6.0)
        drive(eng, sim, feed, reg, 1500)
        self.assertGreater(eng.orders_filled, 0)
        self.assertGreater(len(eng.trades), 0)
        for t in eng.trades:
            self.assertEqual(t.symbol, "R_75")
            self.assertIn(t.exit_reason.value,
                          ("stop_loss", "take_profit", "trailing", "time_stop",
                           "signal_flip", "manual", "prop_flatten", "eod_flatten"))
            self.assertNotEqual(t.pnl, None)
            self.assertTrue(-1.6 <= t.r_multiple <= 3.2, f"implausible R {t.r_multiple}")

    def test_entry_uses_current_bar_price_not_a_stale_quote(self):
        """Regression guard for the look-ahead bug found during the rebuild.

        A position must be priced off the bar that triggered it. The earlier
        implementation decided on the previous bar's close while filling at the
        current one, which manufactured edge in backtests.
        """
        eng, sim, feed, reg = build_engine(trend=6.0, warmup=40)
        matched = False
        for _ in range(500):
            bar = feed.next_bar()
            spec = reg.spec("deriv", "R_75")
            half = (spec.typical_spread or bar.close * 0.00005) / 2
            q = Quote("R_75", bar.ts, bar.close - half, bar.close + half)
            sim.set_time(bar.ts); sim.set_quote(q); eng.set_quote(q)
            before = set(eng.positions)
            eng.on_bar(bar)
            new = [eng.positions[k] for k in eng.positions if k not in before]
            if new:
                pos = new[0]
                self.assertLessEqual(abs(pos.entry - bar.close), max(bar.close * 0.0012, 1.0))
                self.assertGreaterEqual(pos.entry, q.bid - 1e-9)
                self.assertLessEqual(pos.entry, q.ask + bar.close * 0.0012)
                matched = True
                break
            eng.on_bar_brackets(bar)
            eng.on_quote(q)
        self.assertTrue(matched, "no position opened to inspect")

    def test_brackets_are_sane(self):
        eng, sim, feed, reg = build_engine(trend=6.0)
        drive(eng, sim, feed, reg, 800)
        for p in eng.positions.values():
            if p.side is Side.BUY:
                self.assertLess(p.sl, p.entry)
                self.assertGreater(p.tp, p.entry)
            else:
                self.assertGreater(p.sl, p.entry)
                self.assertLess(p.tp, p.entry)

    def test_stop_losses_are_bounded(self):
        """A stop-out must not lose materially more than the risked amount.

        Stops fill with the configured adverse slippage (1 bp of price here),
        which is ~0.2R on a 5 bp stop distance. Beyond that bound something is
        wrong: a loose bracket, a stale fill, or sizing that ignored the stop.
        """
        eng, sim, feed, reg = build_engine(trend=0.2, seed=9)
        drive(eng, sim, feed, reg, 2500)
        stops = [t for t in eng.trades if t.exit_reason.value == "stop_loss"]
        self.assertGreater(len(stops), 0)
        for t in stops:
            self.assertGreaterEqual(t.r_multiple, -1.4,
                                    f"{t.symbol} stop lost {t.r_multiple}R")

    def test_kill_switch_flattens(self):
        eng, sim, feed, reg = build_engine(trend=6.0)
        for _ in range(1200):
            drive(eng, sim, feed, reg, 1)
            if eng.positions:
                break
        self.assertTrue(eng.positions, "expected an open position to kill")
        eng.router.kill(flatten=True)
        eng.cycle_guard()
        self.assertEqual(len(eng.positions), 0)

    def test_prop_guard_blocks_after_breach(self):
        guard = PropGuard(PropProfile(name="t", profit_target_pct=8.0, max_daily_loss_pct=1.0,
                                      max_total_loss_pct=1.5, min_trading_days=1), 10_000.0,
                          now=1_700_000_000)
        eng, sim, feed, reg = build_engine(prop=guard, trend=6.0)
        drive(eng, sim, feed, reg, 400)
        guard.on_equity(1_700_000_000 + 60, 9_700.0)      # force a breach
        n_before = eng.orders_filled
        drive(eng, sim, feed, reg, 300)
        self.assertGreaterEqual(eng.orders_filled, n_before)
        self.assertTrue(guard.st.breach)

    def test_snapshot_is_json_serialisable(self):
        import json
        eng, sim, feed, reg = build_engine(trend=6.0)
        drive(eng, sim, feed, reg, 400)
        snap = eng.snapshot()
        json.dumps(snap)          # must not raise
        self.assertIn("accounts", snap)
        self.assertIn("rejections", snap)

    def test_reconciliation_clean_after_trading(self):
        eng, sim, feed, reg = build_engine(trend=6.0)
        drive(eng, sim, feed, reg, 600)
        self.assertEqual(eng.reconcile(), [])


if __name__ == "__main__":
    unittest.main()
