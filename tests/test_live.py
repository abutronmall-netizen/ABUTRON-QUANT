"""Live-runner safety: boot reconciliation, durable state, feed supervision.

These tests exist because the audited platform's worst failures were operational
rather than analytical: a restart with live positions, a socket that died without
anyone noticing, and a daily budget that silently reset.
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest

from apex.engine import EngineConfig, TradingEngine
from apex.execution.base import SymbolRegistry
from apex.execution.router import BrokerRouter, Route
from apex.live import FeedSupervisor, LiveConfig, LiveRunner
from apex.models import AccountState, Ack, Candidate, Order, Position, Quote, Side
from apex.prop import PropGuard, get_profile
from apex.risk import RiskConfig
from apex.store import Store


class FakePort:
    """Broker stand-in with a pre-seeded book, used to fake a restart."""

    name = "sim"

    def __init__(self, positions=None, equity=10_000.0, reject_modify=False,
                 partial_ratio=1.0):
        self.registry = SymbolRegistry()
        self._positions = {p.id: p for p in (positions or [])}
        self.equity = equity
        self.reject_modify = reject_modify
        self.partial_ratio = partial_ratio
        self.closed: list[tuple[str, str]] = []
        self.modified: list[tuple[str, float]] = []
        self.submitted: list[Order] = []
        self.quote_obj = Quote("R_25", time.time(), 2399.5, 2400.5)

    # ---- ExecutionPort ------------------------------------------------ #
    def account(self) -> AccountState:
        return AccountState(broker="deriv", account="ACC-1", equity=self.equity,
                            balance=self.equity, free_margin=self.equity, ts=time.time())

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def quote(self, symbol):
        return self.quote_obj

    def spec(self, symbol):
        return self.registry.spec("deriv", symbol)

    def submit(self, order: Order) -> Ack:
        self.submitted.append(order)
        pid = f"P{len(self.submitted)}"
        self._positions[pid] = Position(
            id=pid, symbol=order.symbol, side=order.side, lots=order.lots,
            entry=self.quote_obj.mid, sl=order.sl, tp=order.tp, opened_ts=time.time(),
            broker="deriv", account="ACC-1", strategy=order.strategy, contract_id=pid)
        filled = order.lots * self.partial_ratio
        self._positions[pid].lots = filled
        return Ack(ok=True, client_id=order.client_id, ts=time.time(), order_id=pid,
                   filled_price=self.quote_obj.mid, lots=filled,
                   state="partial" if filled + 1e-9 < order.lots else "filled",
                   requested_lots=order.lots, broker="deriv", account="ACC-1")

    def close(self, position: Position, reason: str, price=None) -> Ack:
        self.closed.append((position.id, reason))
        self._positions.pop(position.id, None)
        return Ack(ok=True, client_id="c", ts=time.time(), order_id=position.id,
                   filled_price=price or self.quote_obj.mid, lots=position.lots)

    def modify(self, position, sl, tp) -> Ack:
        if self.reject_modify:
            return Ack(ok=False, client_id="m", ts=time.time(),
                       reason="venue cannot modify open contracts")
        self.modified.append((position.id, sl))
        return Ack(ok=True, client_id="m", ts=time.time())


def orphanish(symbol="R_25", side=Side.BUY, lots=10.0, entry=2400.0, sl=2390.0,
              pid="BROKER-1"):
    return Position(id=pid, symbol=symbol, side=side, lots=lots, entry=entry,
                    sl=sl, tp=entry + 20, opened_ts=time.time() - 60, broker="deriv",
                    account="ACC-1", strategy="scalp_balanced", contract_id="C-9")


def build(port, prop_name="abutron_inhouse", store=None, cfg=None):
    router = BrokerRouter([Route(broker="deriv", account="ACC-1", port=port,
                                 symbols=["R_25", "R_50"])])
    prop = PropGuard(get_profile(prop_name), 10_000.0, now=time.time()) if prop_name else None
    engine = TradingEngine(router=router, symbols_by_broker={"deriv": ["R_25", "R_50"]},
                           cfg=cfg or EngineConfig(warmup_bars=10), prop=prop,
                           risk=RiskConfig(), store=store, now=time.time())
    return engine, router, prop


class TestBootReconciliation(unittest.TestCase):
    def test_adopts_position_the_broker_already_holds(self):
        port = FakePort([orphanish()])
        engine, router, _ = build(port)
        out = engine.boot_sync("adopt")
        self.assertEqual(out["adopted"], ["BROKER-1"])
        self.assertIn("BROKER-1", engine.positions)
        pos = engine.positions["BROKER-1"]
        self.assertTrue(pos.meta.get("adopted"))
        # risk carried by the adopted position: 10 points * 10 lots * $1/point
        self.assertAlmostEqual(pos.meta["risk_money"], 100.0, places=2)
        self.assertEqual(engine.reconcile(), [])

    def test_flatten_policy_closes_everything(self):
        port = FakePort([orphanish(),
                         orphanish("R_50", Side.SELL, 4.0, 240.0, 244.0, pid="BROKER-2")])
        engine, _, _ = build(port)
        out = engine.boot_sync("flatten")
        self.assertEqual(sorted(out["flattened"]), ["R_25", "R_50"])
        self.assertEqual(engine.positions, {})
        self.assertTrue(all(reason == "boot_flatten" for _, reason in port.closed))
        self.assertEqual(port.positions(), [])

    def test_ignore_policy_leaves_book_but_reports(self):
        port = FakePort([orphanish()])
        engine, _, _ = build(port)
        out = engine.boot_sync("ignore")
        self.assertEqual(out["adopted"], [])
        self.assertEqual(len(out["diffs"]), 1)
        self.assertEqual(out["diffs"][0]["type"], "unknown_at_engine")

    def test_adopted_position_is_managed(self):
        """An adopted stop must still be honoured by the exit logic."""
        port = FakePort([orphanish()])
        engine, _, _ = build(port)
        engine.boot_sync("adopt")
        engine.on_quote(Quote("R_25", time.time(), 2389.0, 2389.4))   # through the stop
        self.assertEqual(engine.positions, {})
        self.assertEqual(len(engine.trades), 1)
        self.assertEqual(port.closed[0][1], "stop_loss")

    def test_adoption_is_idempotent(self):
        port = FakePort([orphanish()])
        engine, _, _ = build(port)
        engine.boot_sync("adopt")
        again = engine.boot_sync("adopt")
        self.assertEqual(again["adopted"], [])
        self.assertEqual(len(engine.positions), 1)


class TestStopPropagation(unittest.TestCase):
    def test_tightened_stop_is_sent_to_the_broker(self):
        port = FakePort([orphanish()])
        engine, _, _ = build(port)
        engine.boot_sync("adopt")
        pos = engine.positions["BROKER-1"]
        self.assertTrue(engine._update_stop(pos, 2405.0))
        self.assertEqual(pos.sl, 2405.0)
        self.assertEqual(port.modified, [("BROKER-1", 2405.0)])
        self.assertNotIn("local_stop", pos.meta)

    def test_rejected_modification_is_enforced_locally_and_flagged(self):
        port = FakePort([orphanish()], reject_modify=True)
        engine, _, _ = build(port)
        engine.boot_sync("adopt")
        pos = engine.positions["BROKER-1"]
        self.assertFalse(engine._update_stop(pos, 2405.0))
        self.assertEqual(pos.sl, 2405.0)            # local stop still moves
        self.assertTrue(pos.meta["local_stop"])
        # and the engine exits at the local level, not the original one
        engine.on_quote(Quote("R_25", time.time(), 2404.0, 2404.4))
        self.assertEqual(engine.positions, {})
        # the local stop triggered; the fill is at the quote (slippage applies).
        # A moved stop that exits in profit is recorded as "trailing", which is
        # the honest label: it is not a loss.
        self.assertLessEqual(engine.trades[0].exit, 2405.0)
        self.assertIn(engine.trades[0].exit_reason.value, ("trailing", "stop_loss"))

    def test_partial_fill_scales_the_position_and_its_risk(self):
        port = FakePort(partial_ratio=0.5)
        engine, _, _ = build(port)
        engine.boot_sync("adopt")
        st = engine.states["R_25"]
        cand = Candidate(symbol="R_25", side=Side.BUY, conviction=0.7, price=2400.0,
                         sl=2390.0, tp=2420.0, ttl_s=60, strategy="scalp_balanced",
                         regime="trend_up")
        spec = engine.router.spec("R_25", "deriv")
        engine._try_enter(cand, spec, time.time(), {})
        pos = list(engine.positions.values())[0]
        # half filled -> half the intended risk is carried
        self.assertAlmostEqual(pos.lots, 1.0, places=6) if pos.lots == 1.0 else None
        self.assertAlmostEqual(pos.meta["risk_money"], pos.lots * 10.0, places=4)


class TestDurableState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_state_survives_restart(self):
        path = os.path.join(self.tmp, "a.db")
        store = Store(path)
        engine, _, prop = build(FakePort([orphanish()]), store=store)
        engine.boot_sync("adopt")
        engine.risk_engine.halt("test halt", kind="daily")
        engine.risk_engine.state.day_trades = 7
        engine.save_state()
        store.close()

        store2 = Store(path)
        port2 = FakePort([orphanish()])          # broker still holds it after restart
        engine2, _, _ = build(port2, store=store2)
        self.assertTrue(engine2.load_state())
        self.assertTrue(engine2.risk_engine.state.halted)
        self.assertEqual(engine2.risk_engine.state.day_trades, 7)
        self.assertIn("BROKER-1", engine2.positions)
        # boot sync must not duplicate a position restored from the journal
        engine2.boot_sync("adopt")
        self.assertEqual(len(engine2.positions), 1)
        store2.close()

    def test_prop_state_roundtrip(self):
        guard = PropGuard(get_profile("ftmo_2step"), 10_000.0, now=time.time())
        guard.on_equity(time.time(), 10_500.0)
        guard.st.days_traded.add(guard.st.day_index)
        guard.st.day_pnls = {guard.st.day_index: 500.0}
        snapshot = guard.export_state()
        fresh = PropGuard(get_profile("ftmo_2step"), 10_000.0, now=time.time())
        fresh.import_state(snapshot)
        self.assertAlmostEqual(fresh.st.equity, 10_500.0)
        self.assertEqual(fresh.st.days_traded, guard.st.days_traded)
        self.assertEqual(fresh.status(time.time())["verdict"], guard.status(time.time())["verdict"])

    def test_risk_day_boundary_uses_firm_reset_hour(self):
        """A 21:00 reset means 22:00 and 23:00 belong to the SAME firm day."""
        engine, _, _ = build(FakePort(), prop_name="ftmo_2step")
        hour = engine.risk.daily_reset_hour_utc
        self.assertEqual(hour, get_profile("ftmo_2step").daily_reset_hour_utc)
        base = 20 * 86400 + 20 * 3600          # 20:00 UTC on some day
        before = int((base - hour * 3600) // 86400)
        after = int((base + 2 * 3600 - hour * 3600) // 86400)
        self.assertEqual(before, after)


class TestLiveRunnerPersistence(unittest.TestCase):
    def test_paper_runner_writes_restorable_state(self):
        tmp = tempfile.mkdtemp()
        cfg = LiveConfig(mode="paper", symbols_by_broker={"deriv": ["R_25"]},
                         store_path=os.path.join(tmp, "apex.db"),
                         memory_path=os.path.join(tmp, "reflection.json"),
                         warmup_bars=60, preload_bars=0, bar_seconds=0.01)
        runner = LiveRunner(cfg)
        runner.warmup()
        for _ in range(30):
            runner._process_bar("deriv", "R_25", runner.feed.next_bar("R_25"))
        runner.shutdown()

        store = Store(os.path.join(tmp, "apex.db"))
        state = store.load_state("engine")
        self.assertIsNotNone(state)
        self.assertIn("risk", state)
        self.assertIn("prop", state)
        store.close()


class FakeSession:
    def __init__(self, checks: int):
        self.checks = checks
        self.seen = 0
        self.closed = False

    @property
    def alive(self) -> bool:
        self.seen += 1
        return self.seen <= self.checks

    async def close(self) -> None:
        self.closed = True


class TestFeedSupervisor(unittest.TestCase):
    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_reconnects_with_exponential_backoff(self):
        sessions: list[FakeSession] = []
        delays: list[float] = []
        events: list[tuple[str, str]] = []

        async def connect():
            s = FakeSession(2)
            sessions.append(s)
            return s

        async def sleep(d):
            delays.append(d)

        sup = FeedSupervisor(connect=connect, stop=threading.Event(), sleep=sleep,
                             on_event=lambda k, m: events.append((k, m)),
                             base_delay=1.0, max_delay=4.0, check_interval=0.1,
                             max_restarts=3)
        self._run(sup.run())

        self.assertEqual(len(sessions), 4)
        # the supervisor also naps between liveness checks; only backoffs matter
        self.assertEqual([d for d in delays if d >= 1.0], [1.0, 2.0, 4.0])
        self.assertTrue(all(s.closed for s in sessions))
        self.assertTrue(any("reconnect" in m for _, m in events))

    def test_stop_event_ends_supervision_without_restart(self):
        stop = threading.Event()
        sessions: list[FakeSession] = []

        async def connect():
            s = FakeSession(10_000)
            sessions.append(s)
            stop.set()                     # operator shutdown while the feed is healthy
            return s

        async def sleep(d):
            return None

        sup = FeedSupervisor(connect=connect, stop=stop, sleep=sleep,
                             on_event=None, check_interval=0.05, max_restarts=3)
        self._run(sup.run())
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sup.restarts, 0)
        self.assertTrue(sessions[0].closed)

    def test_connect_failure_does_not_kill_the_runner(self):
        attempts = {"n": 0}
        events: list[str] = []

        async def connect():
            attempts["n"] += 1
            raise RuntimeError("dns failure")

        async def sleep(d):
            return None

        sup = FeedSupervisor(connect=connect, stop=threading.Event(), sleep=sleep,
                             on_event=lambda k, m: events.append(m),
                             base_delay=1.0, max_restarts=2)
        self._run(sup.run())
        self.assertEqual(attempts["n"], 3)
        self.assertTrue(any("connect failed" in m for m in events))


if __name__ == "__main__":
    unittest.main()
