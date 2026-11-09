"""Signal layer: agents, conviction, arbitration, reflection weights.

These are the parts that decide *whether* there is a trade. They were the least
tested module in the first build and the one that produced the audit's "zero
qualified entries" defect, so they get explicit invariants rather than smoke
tests.
"""
import unittest

from apex.agents import MarketContext, build_agents
from apex.data.feed import SyntheticConfig, SyntheticFeed
from apex.indicators import IndicatorSet
from apex.models import Candidate, ExitReason, Position, Side, TradeRecord, Vote
from apex.portfolio import PortfolioManager, PortfolioLimits, ReflectionMemory, compute_conviction
from apex.regime import detect


def series(n: int = 250, seed: int = 3, trend: float = 2.5):
    return SyntheticFeed("R_25", SyntheticConfig(base_price=2400.0, seed=seed,
                                                 trend_strength=trend, vol_bps=6.0)).bars(n)


def context(trend: float = 2.5, seed: int = 3, n: int = 250):
    bars = series(n=n, seed=seed, trend=trend)
    ind = IndicatorSet()
    for b in bars:
        ind.update(b)
    regime = detect(ind, {})
    return MarketContext(symbol="R_25", quote=None, bars={"M5": ind}, regime=regime,
                         now=bars[-1].ts), ind


class TestAgents(unittest.TestCase):
    def test_every_agent_returns_a_bounded_vote(self):
        ctx, _ = context()
        agents = build_agents()
        self.assertGreaterEqual(len(agents), 4)
        seen = set()
        for agent in agents:
            vote = agent.vote(ctx)
            self.assertIsInstance(vote, Vote)
            self.assertGreaterEqual(vote.score, -1.0)
            self.assertLessEqual(vote.score, 1.0)
            self.assertGreaterEqual(vote.confidence, 0.0)
            self.assertLessEqual(vote.confidence, 1.0)
            self.assertTrue(vote.reasons or vote.score == 0.0)
            self.assertNotIn(vote.agent, seen)
            seen.add(vote.agent)

    def test_agents_are_silent_before_warmup(self):
        ind = IndicatorSet()
        for b in series(n=5, seed=1):
            ind.update(b)
        ctx = MarketContext(symbol="R_25", quote=None, bars={"M5": ind},
                            regime=detect(ind, {}), now=0.0)
        for agent in build_agents():
            self.assertEqual(agent.vote(ctx).score, 0.0,
                             f"{agent.name} voted before indicators were ready")

    def test_trend_agent_follows_a_persistent_tape(self):
        ctx, ind = context(trend=3.0, seed=5)
        trend = next(a for a in build_agents() if a.name == "trend")
        vote = trend.vote(ctx)
        if ind.trend_dir != 0:
            self.assertEqual(1 if vote.score > 0 else -1, ind.trend_dir)

    def test_volatility_filter_is_never_directional(self):
        ctx, _ = context()
        filt = next(a for a in build_agents() if a.name == "volfilter")
        self.assertEqual(filt.vote(ctx).score, 0.0)


class TestConviction(unittest.TestCase):
    def weights(self, **kw):
        base = {"trend": 1.0, "meanrev": 1.0, "breakout": 1.0, "momentum": 1.0}
        base.update(kw)
        return base

    def test_agreement_beats_a_lone_voice(self):
        lone, side, _ = compute_conviction([Vote("trend", 0.9, 0.9)], self.weights())
        crowd, _, _ = compute_conviction([Vote("trend", 0.9, 0.9), Vote("breakout", 0.8, 0.8),
                                          Vote("momentum", 0.7, 0.7)], self.weights())
        self.assertGreater(crowd, lone)
        self.assertEqual(side, Side.BUY)

    def test_disagreement_is_penalised_superlinearly(self):
        clean, _, _ = compute_conviction([Vote("trend", 0.8, 0.8), Vote("breakout", 0.7, 0.7)],
                                         self.weights())
        contested, _, _ = compute_conviction([Vote("trend", 0.8, 0.8), Vote("breakout", 0.7, 0.7),
                                              Vote("meanrev", -0.9, 0.9)], self.weights())
        self.assertLess(contested, clean)
        self.assertGreater(contested, 0.0)      # still positive, just smaller

    def test_balanced_opposition_yields_no_direction(self):
        conv, side, _ = compute_conviction([Vote("trend", 0.0, 0.5), Vote("breakout", 0.0, 0.5)],
                                           self.weights())
        self.assertEqual(conv, 0.0)
        self.assertIsNone(side)

    def test_weights_change_the_outcome(self):
        votes = [Vote("trend", 0.45, 0.6), Vote("meanrev", -0.40, 0.6)]
        trend_heavy = compute_conviction(votes, self.weights(trend=3.0, meanrev=0.2))[0]
        mr_heavy = compute_conviction(votes, self.weights(trend=0.2, meanrev=3.0))[0]
        self.assertGreater(trend_heavy, mr_heavy)


def cand(symbol="R_25", conviction=0.6, strategy="scalp_balanced"):
    return Candidate(symbol=symbol, side=Side.BUY, conviction=conviction, price=2400.0,
                     sl=2390.0, tp=2420.0, ttl_s=60, strategy=strategy, regime="trend_up")


def pos(symbol="R_50", strategy="scalp_balanced"):
    return Position(id="p1", symbol=symbol, side=Side.BUY, lots=1.0, entry=2400.0, sl=2390.0,
                    tp=2420.0, opened_ts=0.0, broker="deriv", account="A", strategy=strategy)


class TestArbitration(unittest.TestCase):
    def setUp(self):
        self.pm = PortfolioManager()

    def test_accepts_a_clean_candidate(self):
        res = self.pm.arbitrate([cand()], [])
        self.assertEqual(len(res.accepted), 1)
        self.assertEqual(res.rejected, [])

    def test_blocks_a_second_position_on_the_same_symbol(self):
        res = self.pm.arbitrate([cand("R_50", 0.9)], [pos("R_50")])
        self.assertEqual(res.accepted, [])
        self.assertIn("held", res.rejected[0][1])

    def test_low_conviction_is_rejected_with_a_reason(self):
        res = self.pm.arbitrate([cand(conviction=0.05)], [])
        self.assertEqual(res.accepted, [])
        self.assertTrue(res.rejected[0][1])

    def test_correlation_group_cap(self):
        pm = PortfolioManager(limits=PortfolioLimits(max_correlated=1))
        res = pm.arbitrate([cand("R_75", 0.9)], [pos("R_50")],
                           correlation_groups={"R_50": "vol", "R_75": "vol"})
        self.assertEqual(res.accepted, [])
        self.assertIn("correlation", res.rejected[0][1])

    def test_ranking_prefers_conviction(self):
        res = self.pm.arbitrate([cand("R_25", 0.4), cand("R_75", 0.9)], [])
        self.assertEqual(res.accepted[0].symbol, "R_75")


class TestReflection(unittest.TestCase):
    def trade(self, pnl: float, agent: str = "trend", score: float = 0.8) -> TradeRecord:
        return TradeRecord(trade_id="t", symbol="R_25", side=Side.BUY, lots=1.0, entry=2400.0,
                           exit=2400.0 + pnl, opened_ts=0.0, closed_ts=1.0, pnl=pnl,
                           commission=0.0, strategy="scalp_balanced", broker="deriv",
                           account="A", exit_reason=ExitReason.TAKE_PROFIT,
                           r_multiple=pnl / 10.0, regime="trend_up",
                           meta={"votes": [{"agent": agent, "score": score}],
                                 "regime": "trend_up"})

    def test_wins_raise_weights_and_losses_lower_them(self):
        mem = ReflectionMemory(lr=0.2)
        start = mem.weights_for("trend_up")["trend"]
        for _ in range(5):
            mem.update(self.trade(+10.0))
        up = mem.weights_for("trend_up")["trend"]
        self.assertGreater(up, start)
        for _ in range(10):
            mem.update(self.trade(-10.0))
        self.assertLess(mem.weights_for("trend_up")["trend"], up)

    def test_trades_without_votes_are_ignored(self):
        mem = ReflectionMemory()
        before = mem.report()
        t = self.trade(+10.0)
        t.meta = {}
        mem.update(t)
        self.assertEqual(mem.report()["updates"], before["updates"])

    def test_report_contract_used_by_the_dashboard(self):
        mem = ReflectionMemory()
        mem.update(self.trade(+10.0))
        rep = mem.report()
        self.assertEqual(rep["updates"], 1)
        self.assertIn("trend", rep["stats"])
        self.assertIn("ewma_r", rep["stats"]["trend"])


if __name__ == "__main__":
    unittest.main()
