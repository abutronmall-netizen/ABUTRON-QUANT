"""Operations that keep a desk alive: alerting, watchdog, pending orders, verify,
partial closes, portfolio heat, per-symbol sessions and journal maintenance.

Every one of these exists because of a specific finding in the audit of the
previous deployment. The tests assert the *behaviour that was missing*, not the
shape of the code.
"""
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from unittest import mock

from apex.alerts import AlertConfig, Notifier, notifier_from_config
from apex.engine import EngineConfig, TradingEngine
from apex.execution.base import SymbolRegistry
from apex.execution.deriv import DerivBroker
from apex.execution.router import BrokerRouter, Route
from apex.execution.sim import SimBroker, SimConfig
from apex.live import FeedMonitor, LiveConfig
from apex.models import (
    AccountState,
    Ack,
    Candidate,
    ExitReason,
    Order,
    Position,
    Quote,
    Side,
    SymbolSpec,
    TradeRecord,
    Vote,
)
from apex.risk import RiskConfig, RiskEngine
from apex.store import Store
from apex.verify import VerifyConfig, render_verify, verify_run
from apex.watchdog import FLATTEN_REQUEST_KEY, Watchdog, WatchdogConfig, flatten_requested
from tests.test_live import FakePort
from tests.test_live import build as build_engine


# --------------------------------------------------------------------------- #
# alerting
# --------------------------------------------------------------------------- #
class TestAlerting(unittest.TestCase):
    def test_webhook_posts_json_and_never_raises(self):
        n = Notifier(AlertConfig(webhook_url="https://example.invalid/hook"))
        sent = []

        class Resp(io.BytesIO):
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=0):
            sent.append(json.loads(req.data.decode()))
            return Resp(b"{}")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertTrue(n.alert("risk", "halted"))
        self.assertEqual(sent[0]["kind"], "risk")
        self.assertEqual(sent[0]["message"], "halted")

        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertFalse(n.alert("risk", "halted again"))

    def test_throttle_is_per_kind(self):
        n = Notifier(AlertConfig(webhook_url="https://example.invalid/hook",
                                 min_interval_s=60), clock=lambda: 1000.0)
        calls = []
        n._sender = lambda url, body, headers: calls.append(1)
        self.assertTrue(n.alert("risk", "one"))
        self.assertFalse(n.alert("risk", "one again"))
        self.assertTrue(n.alert("error", "different kind"))
        self.assertEqual(len(calls), 2)

    def test_engine_kinds_are_mapped_and_noise_is_dropped(self):
        n = Notifier(AlertConfig(webhook_url="https://example.invalid/hook"))
        seen = []

        def capture(url, body, headers):
            payload = json.loads(body.decode())
            seen.append((payload["kind"], payload["level"]))

        n._sender = capture
        n.from_event("risk", "daily limit")
        n.from_event("error", "feed died")
        n.from_event("order", "filled")          # not alert-worthy, no channel
        self.assertEqual(seen, [("risk", "warn"), ("error", "error")])

    def test_disabled_config_builds_a_silent_notifier(self):
        n = notifier_from_config({"alerts": {}})
        self.assertFalse(n.alert("risk", "nothing to send"))


# --------------------------------------------------------------------------- #
# watchdog
# --------------------------------------------------------------------------- #
class TestWatchdog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.hb = os.path.join(self.tmp, "heartbeat.json")
        self.db = os.path.join(self.tmp, "apex.db")
        self.store = Store(self.db)
        self.now = 1_000_000.0

    def tearDown(self):
        self.store.close()

    def _beat(self, age_s: float):
        with open(self.hb, "w", encoding="utf-8") as fh:
            json.dump({"ts": self.now - age_s, "positions": [
                {"symbol": "R_25", "side": "BUY", "lots": 1.0, "local_stop": True}],
                "local_stops": 1}, fh)

    def _wd(self, **kw):
        cfg = WatchdogConfig(heartbeat_path=self.hb, stale_after_s=30.0, **kw)
        notifier = Notifier(AlertConfig(webhook_url="https://x.invalid",
                                        min_interval_s=0))
        notifier._sender = lambda url, body, headers: None
        return Watchdog(cfg, store=self.store, notifier=notifier,
                        clock=lambda: self.now)

    def test_fresh_heartbeat_does_nothing(self):
        self._beat(5)
        wd = self._wd()
        self.assertFalse(wd.check()["stale"])
        self.assertIsNone(flatten_requested(self.store, self.now))

    def test_missing_heartbeat_alerts_and_requests_flatten(self):
        wd = self._wd()
        out = wd.check()
        self.assertTrue(out["stale"])
        self.assertEqual(wd.stats["alerts"], 1)
        req = flatten_requested(self.store, self.now)
        self.assertIsNotNone(req)
        self.assertIn("heartbeat stale", req["reason"])
        self.assertEqual(req["detail"]["local_stops"], 0)

    def test_flattener_runs_once_per_episode_and_recovery_is_reported(self):
        calls = []
        wd = self._wd()
        wd.flattener = lambda: calls.append(1) or []
        self.assertEqual(wd.check()["action"], "flatten")
        wd.check()
        self.assertEqual(len(calls), 1)

        self._beat(0)                       # engine came back
        self.assertFalse(wd.check()["stale"])
        self.assertEqual(wd.stats["stale_episodes"], 1)

    def test_flatten_request_expires(self):
        store = self.store
        store.save_state(FLATTEN_REQUEST_KEY, {"ts": self.now - 7 * 3600, "reason": "old"})
        self.assertIsNone(flatten_requested(store, self.now))

    def test_engine_honours_a_flatten_request(self):
        port = FakePort()
        engine, _, _ = build_engine(port, prop_name=None, store=self.store)
        engine.positions["P1"] = Position(
            id="P1", symbol="R_25", side=Side.BUY, lots=1.0, entry=2400.0,
            sl=2390.0, tp=2420.0, opened_ts=self.now, broker="deriv", account="ACC-1",
            strategy="scalp_balanced", contract_id="P1", meta={"risk_money": 10.0})
        port._positions["P1"] = engine.positions["P1"]
        self.store.save_state(FLATTEN_REQUEST_KEY,
                              {"ts": time.time(), "reason": "heartbeat stale"})
        engine._watchdog_check(ts=time.time() + 10)
        self.assertEqual(engine.positions, {})
        self.assertTrue(engine.risk_engine.state.halted)
        self.assertIsNone(flatten_requested(self.store, time.time()))


# --------------------------------------------------------------------------- #
# pending orders
# --------------------------------------------------------------------------- #
class PendingPort(FakePort):
    """Accepts orders but does not fill them until told to."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.status = {"state": "pending"}
        self.cancelled: list[str] = []
        self.pending_order = None

    def submit(self, order):
        self.pending_order = order
        return Ack(ok=True, client_id=order.client_id, ts=time.time(),
                   order_id="ORD-1", filled_price=self.quote_obj.mid, lots=0.0,
                   state="pending", requested_lots=order.lots, broker="deriv",
                   account="ACC-1")

    def order_status(self, order_id):
        return dict(self.status)

    def cancel(self, order_id):
        self.cancelled.append(order_id)
        return Ack(ok=True, client_id="cancel", ts=time.time(), order_id=order_id,
                   broker="deriv", account="ACC-1")


def _candidate(symbol="R_25", price=2400.0, sl=2390.0, tp=2420.0) -> Candidate:
    return Candidate(symbol=symbol, side=Side.BUY, conviction=0.55, price=price,
                     sl=sl, tp=tp, ttl_s=300.0, strategy="scalp_balanced",
                     regime="trend_up", votes=[Vote("trend", 0.4, 0.6)],
                     meta={"atr": 5.0, "spread_atr": 0.05})


class TestPendingOrders(unittest.TestCase):
    def setUp(self):
        self.port = PendingPort()
        self.engine, self.router, _ = build_engine(self.port, prop_name=None)
        self.ts = time.time()
        self.engine._try_enter(_candidate(), self.router.spec("R_25"),
                              self.ts, {"why": "test"})

    def test_accepted_order_is_tracked_not_forgotten(self):
        self.assertEqual(self.engine.positions, {})
        self.assertEqual(len(self.engine.pending), 1)
        po = self.engine.pending.items()[0]
        self.assertEqual(po.order_id, "ORD-1")
        self.assertGreater(po.risk_money, 0)
        self.assertEqual(po.side, "BUY")

    def test_late_fill_becomes_a_managed_position(self):
        self.port.status = {"state": "filled", "filled_lots": self.port.pending_order.lots,
                            "filled_price": 2400.5, "position_id": "TKT-9"}
        out = self.engine.poll_pending(now=self.ts + 10, force=True)
        self.assertEqual(out["filled"], 1)
        self.assertEqual(len(self.engine.pending), 0)
        pos = self.engine.positions["TKT-9"]
        self.assertEqual(pos.sl, 2390.0)
        self.assertTrue(pos.meta.get("late_fill"))
        self.assertAlmostEqual(pos.meta["risk_money"],
                               abs(2400.0 - 2390.0) * pos.lots, places=2)

    def test_cancelled_at_venue_is_dropped(self):
        self.port.status = {"state": "cancelled"}
        out = self.engine.poll_pending(now=self.ts + 1, force=True)
        self.assertEqual(out["cancelled"], 1)
        self.assertEqual(len(self.engine.pending), 0)
        self.assertEqual(self.engine.positions, {})

    def test_stale_pending_order_is_cancelled(self):
        out = self.engine.poll_pending(now=self.ts + 10_000, force=True)
        self.assertEqual(out["expired"], 1)
        self.assertEqual(self.port.cancelled, ["ORD-1"])

    def test_polling_is_throttled(self):
        first = self.engine.poll_pending(now=self.ts, force=True)
        second = self.engine.poll_pending(now=self.ts + 1)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(first["checked"], 1)

    def test_pending_book_survives_a_restart(self):
        engine2, _, _ = build_engine(self.port, prop_name=None)
        engine2.import_state(json.loads(json.dumps(self.engine.export_state())))
        self.assertEqual(len(engine2.pending), 1)
        self.assertEqual(engine2.pending.items()[0].order_id, "ORD-1")

    def test_adapter_without_order_status_is_reported_as_unknown(self):
        engine, _, _ = build_engine(FakePort(), prop_name=None)
        self.assertIsNone(engine.router.order_status("deriv", "ACC-1", "X"))


# --------------------------------------------------------------------------- #
# apex verify
# --------------------------------------------------------------------------- #
class TestVerify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "apex.db")
        self.store = Store(self.db)
        self.now = time.time()
        self.store.record_equity(self.now - 100, "deriv", "A", 10_000.0, 10_000.0)
        self.store.record_equity(self.now, "deriv", "A", 10_050.0, 10_050.0)
        self.store.record_trade(TradeRecord(
            trade_id="t1", symbol="R_25", side=Side.BUY, lots=1.0, entry=2400.0,
            exit=2410.0, opened_ts=self.now - 200, closed_ts=self.now - 100,
            pnl=50.0, commission=0.0, strategy="scalp_balanced", broker="deriv",
            account="A", exit_reason=ExitReason.TAKE_PROFIT, conviction=0.5,
            regime="trend_up", r_multiple=1.0, mfe=10.0, mae=-2.0))
        order = Order(symbol="R_25", side=Side.BUY, lots=1.0, broker="deriv",
                      account="A", strategy="scalp_balanced", price=2400.0)
        self.store.record_order(order, Ack(ok=True, client_id=order.client_id,
                                           ts=self.now - 200, order_id="1",
                                           filled_price=2400.2, lots=1.0))
        self.store.save_state("engine", {"positions": [], "pending": []})
        self.store.close()

    def test_clean_run_passes(self):
        report = verify_run(VerifyConfig(db_path=self.db))
        self.assertTrue(report["ok"], report["failures"])
        self.assertIn("PASS", render_verify(report))
        # paper runs cannot reconcile P&L across a simulated restart: informational
        self.assertEqual(next(c for c in report["checks"] if c["name"] == "pnl")["severity"], "info")

    def test_unprotected_position_fails_the_stop_check(self):
        store = Store(self.db)
        store.save_state("engine", {"positions": [
            {"id": "P", "symbol": "R_25", "side": "BUY", "lots": 1.0,
             "entry": 2400.0, "sl": None, "meta": {}}], "pending": []})
        store.close()
        report = verify_run(VerifyConfig(db_path=self.db))
        self.assertFalse(report["ok"])
        self.assertIn("stops", report["failures"])

    def test_reconcile_discrepancy_fails(self):
        store = Store(self.db)
        store.record_event("reconcile", "2 discrepancy(ies)")
        store.close()
        report = verify_run(VerifyConfig(db_path=self.db))
        self.assertIn("reconcile", report["failures"])

    def test_unknown_rejection_code_is_flagged_but_known_codes_are_not(self):
        store = Store(self.db)
        store.record_signal(_candidate(), "SPREAD_TOO_WIDE", "spread", {})
        store.close()
        self.assertTrue(verify_run(VerifyConfig(db_path=self.db))["ok"])
        store = Store(self.db)
        store.record_signal(_candidate(), "MYSTERY", "?", {})
        store.close()
        report = verify_run(VerifyConfig(db_path=self.db))
        self.assertIn("rejections", report["failures"])

    def test_systematic_adverse_slippage_fails_in_live_mode(self):
        store = Store(self.db)
        for i in range(6):
            order = Order(symbol="R_25", side=Side.BUY, lots=1.0, broker="deriv",
                          account="A", strategy="s", price=2400.0)
            store.record_order(order, Ack(ok=True, client_id=f"x{i}", ts=self.now - i,
                                          order_id=str(i), filled_price=2402.0, lots=1.0))
        store.close()
        live = verify_run(VerifyConfig(db_path=self.db, live=True))
        prices = next(c for c in live["checks"] if c["name"] == "prices")
        self.assertFalse(prices["ok"], prices)
        self.assertTrue(prices["data"]["biased"])
        paper = verify_run(VerifyConfig(db_path=self.db, live=False))
        paper_prices = next(c for c in paper["checks"] if c["name"] == "prices")
        self.assertEqual(paper_prices["severity"], "warn")
        self.assertNotIn("prices", paper["failures"] or [])

    def test_missing_journal_is_a_failure_not_a_crash(self):
        report = verify_run(VerifyConfig(db_path=os.path.join(self.tmp, "nope.db")))
        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["name"], "journal")


# --------------------------------------------------------------------------- #
# partial closes
# --------------------------------------------------------------------------- #
def _sim_engine(tmp=None):
    reg = SymbolRegistry()
    sim = SimBroker(registry=reg, cfg=SimConfig(seed=3, slippage_bps=0.0,
                                                stop_slippage_bps=0.0))
    now = 1_700_000_000.0
    sim.set_time(now)
    sim.set_quote(Quote("R_25", now, 2399.5, 2400.5))
    router = BrokerRouter([Route(broker="deriv", account=sim.account_id, port=sim,
                                 symbols=["R_25", "R_50"])])
    engine = TradingEngine(router=router, symbols_by_broker={"deriv": ["R_25", "R_50"]},
                           cfg=EngineConfig(warmup_bars=10), prop=None,
                           risk=RiskConfig(max_heat_pct=0.0), now=now)
    pos = Position(id="P1", symbol="R_25", side=Side.BUY, lots=10.0, entry=2400.0,
                   sl=2390.0, tp=2430.0, opened_ts=now, broker="deriv",
                   account=sim.account_id, strategy="scalp_balanced", contract_id="P1",
                   meta={"risk_money": 100.0, "sl_initial": 2390.0})
    engine.positions["P1"] = pos
    sim._positions["P1"] = replace(pos, meta=dict(pos.meta))
    return engine, sim, pos, now


class TestPartialClose(unittest.TestCase):
    def test_partial_close_keeps_the_position_with_less_lots(self):
        engine, sim, pos, now = _sim_engine()
        # make the sim's book hold the same lots the engine believes in
        sim._positions["P1"].lots = pos.lots
        trade = engine.close_position(pos, ExitReason.TAKE_PROFIT, 2405.0,
                                      "scale-out", volume=4.0)
        self.assertIsNotNone(trade)
        self.assertTrue(trade.meta["partial"])
        self.assertAlmostEqual(trade.lots, 4.0, places=6)
        self.assertAlmostEqual(trade.pnl, 4.0 * 5.0, places=2)   # 4 lots * 5 points
        self.assertIn("P1", engine.positions)
        self.assertAlmostEqual(engine.positions["P1"].lots, 6.0, places=6)
        self.assertTrue(engine.positions["P1"].meta.get("scaled_out"))
        self.assertAlmostEqual(engine.positions["P1"].meta["risk_money"], 60.0, places=2)

        full = engine.close_position(engine.positions["P1"], ExitReason.TAKE_PROFIT, 2410.0)
        self.assertAlmostEqual(full.lots, 6.0, places=6)
        self.assertEqual(engine.positions, {})

    def test_deriv_refuses_a_partial_close_instead_of_pretending(self):
        broker = DerivBroker(account="D", dry_run=True)
        pos = Position(id="P", symbol="R_25", side=Side.BUY, lots=4.0, entry=100.0,
                       sl=95.0, tp=110.0, opened_ts=0.0, broker="deriv", account="D",
                       strategy="scalp_balanced", contract_id="C1")
        ack = broker.close(pos, "scale-out", 101.0, volume=1.0)
        self.assertFalse(ack.ok)
        self.assertIn("partially", ack.reason)
        self.assertTrue(broker.close(pos, "manual", 101.0).ok)

    def test_router_refuses_partial_closes_on_adapters_that_cannot(self):
        port = FakePort()
        pos = Position(id="P", symbol="R_25", side=Side.BUY, lots=2.0, entry=2400.0,
                       sl=2390.0, tp=2430.0, opened_ts=0.0, broker="deriv",
                       account="ACC-1", strategy="scalp_balanced", contract_id="P")
        port._positions["P"] = pos
        router = BrokerRouter([Route(broker="deriv", account="ACC-1", port=port,
                                     symbols=["R_25"])])
        ack = router.close(pos, "scale-out", 2401.0, volume=1.0)
        self.assertFalse(ack.ok)
        self.assertIn("partial", ack.reason)
        self.assertTrue(router.close(pos, "manual", 2401.0).ok)

    def test_scale_out_trigger_fires_at_the_configured_r(self):
        engine, sim, pos, now = _sim_engine()
        engine.cfg.scale_out_r = 1.0
        engine.cfg.scale_out_frac = 0.5
        sim.set_quote(Quote("R_25", now, 2410.4, 2410.6))     # +1.05R
        engine.on_quote(Quote("R_25", now, 2410.4, 2410.6))
        self.assertIn("P1", engine.positions)
        self.assertAlmostEqual(engine.positions["P1"].lots, 5.0, places=6)
        self.assertEqual(len(engine.trades), 1)


# --------------------------------------------------------------------------- #
# the order carries the price it is expected to fill at
# --------------------------------------------------------------------------- #
class TestOrderPriceOnTheWire(unittest.TestCase):
    """Deriv refuses an order without a price, and "prices agree" needs one stored.

    Before this, `Order.price` was never set by the engine at all: live Deriv
    submission failed with "entry price required for Deriv contracts" and the
    journal could not answer the deployment checklist's step 2.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(os.path.join(self.tmp, "apex.db"))
        reg = SymbolRegistry()
        self.sim = SimBroker(registry=reg, cfg=SimConfig(seed=3, slippage_bps=0.0))
        self.now = 1_700_000_000.0
        self.sim.set_time(self.now)
        self.router = BrokerRouter([Route(broker="deriv", account=self.sim.account_id,
                                         port=self.sim, symbols=["R_25", "R_50"])],
                                   store=self.store)
        self.engine = TradingEngine(
            router=self.router, symbols_by_broker={"deriv": ["R_25", "R_50"]},
            cfg=EngineConfig(warmup_bars=10), prop=None,
            risk=RiskConfig(), store=self.store, now=self.now)
        q = Quote("R_25", self.now, 2399.5, 2400.5)
        self.sim.set_quote(q)
        self.engine.set_quote(q)

    def tearDown(self):
        self.store.close()

    def test_the_order_is_sent_at_the_touch_not_the_mid(self):
        self.engine._try_enter(_candidate(symbol="R_25", price=2400.0),
                              self.router.spec("R_25"), self.now, {"why": "test"})
        self.assertEqual(len(self.sim.fills), 1)
        expected = self.engine.states["R_25"].last_quote.ask
        self.assertAlmostEqual(expected, 2400.5, places=6)
        # the sim fills at the ask it was quoted, so entry == expected fill
        self.assertAlmostEqual(self.sim.fills[0].filled_price, 2400.5, places=6)
        self.assertEqual(len(self.engine.positions), 1)

    def test_the_journal_stores_the_requested_price(self):
        self.engine._try_enter(_candidate(symbol="R_25", price=2400.0),
                              self.router.spec("R_25"), self.now, {"why": "test"})
        row = self.store.conn.execute(
            "SELECT requested_price, filled_price, order_json FROM orders").fetchone()
        self.assertAlmostEqual(row["requested_price"], 2400.5, places=6)
        self.assertIsNotNone(row["filled_price"])
        self.assertAlmostEqual(json.loads(row["order_json"])["price"], 2400.5, places=6)

    def test_slippage_is_measured_against_the_stored_price(self):
        """No signal fallback needed: the journal answers on its own."""
        for _ in range(3):
            self.engine._try_enter(_candidate(symbol="R_25", price=2400.0),
                                  self.router.spec("R_25"), self.now, {"why": "test"})
            pos = next(iter(self.engine.positions.values()))
            self.engine.close_position(pos, ExitReason.MANUAL, 2401.0, "test")
        report = verify_run(VerifyConfig(db_path=os.path.join(self.tmp, "apex.db")))
        prices = next(c for c in report["checks"] if c["name"] == "prices")
        self.assertEqual(prices["data"]["n"], 3)
        self.assertLess(abs(prices["data"]["mean_bps"]), 1.0)


# --------------------------------------------------------------------------- #
# portfolio heat and per-symbol sessions
# --------------------------------------------------------------------------- #
def _spec(symbol="R_25") -> SymbolSpec:
    return SymbolRegistry().spec("deriv", symbol)


def _acct(equity=10_000.0) -> AccountState:
    return AccountState(broker="deriv", account="A", equity=equity, balance=equity,
                        free_margin=equity, ts=0.0)


class TestHeatAndSessions(unittest.TestCase):
    def _open(self, risk: float, symbol="R_50") -> Position:
        return Position(id=f"P-{symbol}", symbol=symbol, side=Side.BUY, lots=1.0,
                        entry=2400.0, sl=2390.0, tp=2430.0, opened_ts=0.0,
                        broker="deriv", account="A", strategy="scalp_balanced",
                        meta={"risk_money": risk})

    def test_heat_cap_blocks_the_fifth_overlapping_risk(self):
        cfg = RiskConfig(max_heat_pct=2.0, max_open_positions=10,
                         max_correlated_positions=10, risk_per_trade_pct=0.5)
        eng = RiskEngine(cfg, initial_equity=10_000.0, now=0.0)
        positions = [self._open(50.0, "R_25")]
        ok = eng.evaluate(_candidate(symbol="R_50"), _spec("R_50"), _acct(), positions,
                          now=10.0)
        self.assertTrue(ok.allowed)
        positions = [self._open(195.0, "R_25")]
        blocked = eng.evaluate(_candidate(symbol="R_50"), _spec("R_50"), _acct(),
                               positions, now=10.0)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.code, "PORTFOLIO_HEAT")
        self.assertGreater(blocked.detail["heat_pct"], 2.0)

    def test_heat_cap_disabled_by_default(self):
        cfg = RiskConfig(max_heat_pct=0.0)
        eng = RiskEngine(cfg, initial_equity=10_000.0, now=0.0)
        positions = [self._open(500.0, "R_25")]
        self.assertTrue(eng.evaluate(_candidate(symbol="R_50"), _spec("R_50"), _acct(),
                                     positions, now=10.0).allowed)

    def test_group_heat_cap_uses_the_correlation_map(self):
        cfg = RiskConfig(max_group_heat_pct=1.0, max_open_positions=10,
                         max_correlated_positions=10)
        eng = RiskEngine(cfg, initial_equity=10_000.0, now=0.0)
        groups = {"R_25": "vol25", "R_50": "vol50", "EURUSD": "eur"}
        positions = [self._open(90.0, "R_25")]
        self.assertTrue(eng.evaluate(_candidate(symbol="EURUSD"), _spec("EURUSD"),
                                     _acct(), positions, now=10.0, groups=groups).allowed)
        groups["EURUSD"] = "vol25"
        blocked = eng.evaluate(_candidate(symbol="EURUSD"), _spec("EURUSD"), _acct(),
                               positions, now=10.0, groups=groups)
        self.assertEqual(blocked.code, "GROUP_HEAT")

    def test_symbol_session_overrides_the_global_window(self):
        cfg = RiskConfig(session_start_utc=0, session_end_utc=24,
                         session_hours={"XAUUSD": (12, 20)})
        eng = RiskEngine(cfg, initial_equity=10_000.0, now=0.0)
        ts_0800 = 8 * 3600.0                     # 08:00 UTC
        gold = eng.evaluate(_candidate(symbol="XAUUSD"), _spec("XAUUSD"), _acct(), [],
                            now=ts_0800)
        self.assertEqual(gold.code, "SESSION_CLOSED")
        fx = eng.evaluate(_candidate(symbol="EURUSD"), _spec("EURUSD"), _acct(), [],
                          now=ts_0800)
        self.assertTrue(fx.allowed)
        self.assertEqual(eng.session_window("XAUUSD"), (12, 20))

    def test_group_session_applies_when_the_symbol_has_none(self):
        cfg = RiskConfig(session_hours={"metals": (13, 21)})
        eng = RiskEngine(cfg, initial_equity=10_000.0, now=0.0)
        groups = {"XAGUSD": "metals"}
        self.assertEqual(eng.session_window("XAGUSD", groups), (13, 21))
        self.assertEqual(eng.session_window("R_25", groups), (0, 24))


# --------------------------------------------------------------------------- #
# journal maintenance and feed health
# --------------------------------------------------------------------------- #
class TestJournalMaintenance(unittest.TestCase):
    def test_prune_keeps_trades_and_drops_high_frequency_rows(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "apex.db"))
        now = time.time()
        old = now - 30 * 86400
        store.record_equity(old, "deriv", "A", 10_000.0, 10_000.0)
        store.record_equity(now, "deriv", "A", 10_100.0, 10_100.0)
        store.record_event("info", "old", ts=old)
        store.record_signal(_candidate(), "SPREAD_TOO_WIDE", "old", {}, ts=old)
        store.record_trade(TradeRecord(
            trade_id="old", symbol="R_25", side=Side.BUY, lots=1.0, entry=1.0, exit=2.0,
            opened_ts=old, closed_ts=old, pnl=1.0, commission=0.0, strategy="s",
            broker="deriv", account="A", exit_reason=ExitReason.TAKE_PROFIT,
            conviction=0.5, regime="trend_up", r_multiple=1.0, mfe=0.0, mae=0.0))
        counts = store.prune(14.0)
        self.assertEqual(counts["equity"], 1)
        self.assertEqual(counts["signals"], 1)
        self.assertEqual(counts["events"], 1)
        self.assertEqual(len(store.trades()), 1)          # audit trail survives
        self.assertEqual(len(store.equity_curve()), 1)
        store.close()

    def test_prune_is_a_noop_when_retention_is_off(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "apex.db"))
        self.assertTrue(store.prune(0)["skipped"])
        store.close()

    def test_prune_truncates_the_wal(self):
        """Deleted rows must not survive on disk inside the write-ahead log."""
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "apex.db")
        store = Store(path)
        for i in range(400):
            store.record_equity(time.time() - 86400 * 30, "deriv", "A", 1.0 + i, 1.0)
        store.prune(1.0)
        wal = path + "-wal"
        self.assertTrue(os.path.exists(wal))
        self.assertEqual(os.path.getsize(wal), 0, "WAL still holds the deleted pages")
        store.close()

    def test_size_includes_the_wal(self):
        """WAL mode: the main file alone understates a busy journal."""
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "apex.db"))
        for i in range(500):
            store.record_equity(time.time(), "deriv", "A", 1.0 + i, 1.0)
        parts = store.size_parts()
        self.assertGreater(parts["main"], 0)
        self.assertEqual(store.size_bytes(), sum(parts.values()))
        self.assertGreaterEqual(store.size_bytes(), parts["main"])
        self.assertGreater(parts["wal"], 0, "a journal written in WAL mode has a WAL file")
        store.close()

    def test_vacuum_reports_ok(self):
        tmp = tempfile.mkdtemp()
        store = Store(os.path.join(tmp, "apex.db"))
        for _ in range(200):
            store.record_equity(time.time(), "deriv", "A", 1.0, 1.0)
        store.prune(0.000001)
        out = store.vacuum()
        self.assertTrue(out["ok"], out["error"])
        self.assertGreaterEqual(out["before"], 0)
        self.assertLessEqual(out["after"], store.size_bytes() + 1)
        store.close()


class TestFeedMonitor(unittest.TestCase):
    def test_gap_in_bars_is_detected(self):
        events = []
        mon = FeedMonitor(bar_seconds=300, on_event=lambda k, m: events.append(m))
        mon.on_bar("R_25", 1_000_000.0)
        self.assertIsNone(mon.on_bar("R_25", 1_000_300.0))
        gap = mon.on_bar("R_25", 1_000_300.0 + 300 * 6)
        self.assertGreater(gap, 4)
        self.assertEqual(mon.stats["gaps"], 1)
        self.assertIn("gap", events[0])

    def test_gap_is_tracked_per_symbol(self):
        mon = FeedMonitor(bar_seconds=300, on_event=lambda k, m: None)
        mon.on_bar("R_25", 1_000_000.0)
        mon.on_bar("R_50", 1_000_000.0)
        self.assertIsNone(mon.on_bar("R_50", 1_000_300.0))

    def test_clock_drift_is_warned_once_per_window(self):
        now = [1_700_000_000.0]
        events = []
        mon = FeedMonitor(drift_warn_s=5.0, on_event=lambda k, m: events.append(m),
                          now=lambda: now[0])
        self.assertIsNotNone(mon.on_tick(now[0] + 60))
        self.assertIsNone(mon.on_tick(now[0] + 61))       # throttled
        now[0] += 601
        self.assertIsNotNone(mon.on_tick(now[0] + 60))
        self.assertEqual(mon.stats["drift_warnings"], 2)


class TestLiveConfigDurability(unittest.TestCase):
    def test_notifier_and_heartbeat_are_configurable(self):
        cfg = LiveConfig(alerts={"webhook_url": "https://x.invalid"},
                         heartbeat_path="/tmp/hb.json", db_retention_days=7.0)
        self.assertEqual(cfg.db_retention_days, 7.0)
        self.assertTrue(cfg.alerts)


if __name__ == "__main__":
    unittest.main()
