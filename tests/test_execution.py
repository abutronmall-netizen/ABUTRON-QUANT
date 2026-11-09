import unittest
from types import SimpleNamespace

from apex.execution.base import SymbolRegistry
from apex.execution.mt5 import MT5Broker
from apex.execution.router import BrokerRouter, Route
from apex.execution.sim import SimBroker, SimConfig
from apex.models import Order, OrderKind, Position, Quote, Side
from apex.data.mt5 import MT5Config, MT5Connection


def make_sim(balance=10_000.0, **kw):
    reg = SymbolRegistry()
    sim = SimBroker(reg, SimConfig(initial_balance=balance, seed=1, **kw), account="SIM-1")
    sim.set_quote(Quote("R_25", 0.0, 2999.9, 3000.1))
    return sim, reg


class TestSimBroker(unittest.TestCase):
    def test_buy_fills_at_ask_or_worse(self):
        sim, _ = make_sim()
        ack = sim.submit(Order(symbol="R_25", side=Side.BUY, lots=1, broker="sim",
                               account="SIM-1", sl=2990, tp=3020))
        self.assertTrue(ack.ok)
        self.assertGreaterEqual(ack.filled_price, 3000.1)

    def test_sell_fills_at_bid_or_worse(self):
        sim, _ = make_sim()
        ack = sim.submit(Order(symbol="R_25", side=Side.SELL, lots=1, broker="sim", account="SIM-1"))
        self.assertTrue(ack.ok)
        self.assertLessEqual(ack.filled_price, 2999.9)

    def test_equity_reflects_unrealised(self):
        sim, _ = make_sim()
        sim.submit(Order(symbol="R_25", side=Side.BUY, lots=10, broker="sim", account="SIM-1"))
        sim.set_quote(Quote("R_25", 1.0, 3009.9, 3010.1))
        a = sim.account()
        self.assertGreater(a.equity, a.balance)

    def test_close_books_pnl_and_commission(self):
        sim, _ = make_sim()
        sim.set_quote(Quote("XAUUSD", 0.0, 2379.8, 2380.2))
        ack = sim.submit(Order(symbol="XAUUSD", side=Side.BUY, lots=0.1, broker="sim",
                               account="SIM-1", sl=2370, tp=2400))
        pos = sim.positions_list()[0]
        sim.set_quote(Quote("XAUUSD", 2.0, 2390.0, 2390.4))
        close = sim.close(pos, "test")
        self.assertTrue(close.ok)
        expected = (10_000 + (close.filled_price - pos.entry) * 0.1 * 100
                    - close.commission - pos.commission)      # both legs pay commission
        self.assertAlmostEqual(sim.balance, expected, delta=0.06)  # price rounding on the fill

    def test_rejects_duplicate_symbol(self):
        sim, _ = make_sim()
        sim.submit(Order(symbol="R_25", side=Side.BUY, lots=1, broker="sim", account="SIM-1"))
        ack = sim.submit(Order(symbol="R_25", side=Side.BUY, lots=1, broker="sim", account="SIM-1"))
        self.assertFalse(ack.ok)
        self.assertIn("duplicate", ack.reason)

    def test_rejects_without_quote(self):
        sim, _ = make_sim()
        ack = sim.submit(Order(symbol="R_75", side=Side.BUY, lots=1, broker="sim", account="SIM-1"))
        self.assertFalse(ack.ok)

    def test_rejects_absurd_spread(self):
        sim, _ = make_sim(max_spread_bps=1.0)
        sim.set_quote(Quote("R_25", 0.0, 2900.0, 3100.0))
        ack = sim.submit(Order(symbol="R_25", side=Side.BUY, lots=1, broker="sim", account="SIM-1"))
        self.assertFalse(ack.ok)

    def test_lot_rounding_respects_step(self):
        reg = SymbolRegistry()
        spec = reg.spec("mt5", "EURUSD")
        self.assertAlmostEqual(spec.round_lots(0.0134), 0.01)
        self.assertAlmostEqual(spec.round_lots(10_000), 20.0)


class TestRouter(unittest.TestCase):
    def setUp(self):
        sim, self.reg = make_sim()
        self.sim = sim
        self.router = BrokerRouter([Route(broker="sim", account="SIM-1", port=sim,
                                          symbols=["R_25"])], registry=self.reg, retry_sleep=0)

    def _order(self, **kw):
        return Order(symbol="R_25", side=Side.BUY, lots=1, broker="sim", account="SIM-1", **kw)

    def test_idempotent_client_id(self):
        o = self._order()
        a1 = self.router.submit(o)
        a2 = self.router.submit(o)
        self.assertTrue(a1.ok)
        self.assertEqual(a1.order_id, a2.order_id)
        self.assertEqual(self.router.stats["duplicates"], 1)
        self.assertEqual(len(self.sim.positions_list()), 1)

    def test_no_route_is_reported_not_dropped(self):
        ack = self.router.submit(Order(symbol="EURUSD", side=Side.BUY, lots=1,
                                       broker="mt5", account="NOPE"))
        self.assertFalse(ack.ok)
        self.assertIn("no route", ack.reason)
        self.assertEqual(self.router.stats["rejected"], 1)

    def test_kill_switch_blocks_and_flattens(self):
        self.router.submit(self._order())
        self.router.kill(flatten=True)
        self.assertEqual(len(self.sim.positions_list()), 0)
        ack = self.router.submit(self._order())
        self.assertFalse(ack.ok)
        self.assertIn("kill", ack.reason)

    def test_reconcile_detects_divergence(self):
        self.router.submit(self._order())
        ghost = Position(id="ghost", symbol="R_25", side=Side.BUY, lots=1, entry=3000,
                         sl=None, tp=None, opened_ts=0, broker="sim", account="SIM-1",
                         strategy="x")
        diffs = self.router.reconcile([ghost])
        kinds = {d["type"] for d in diffs}
        self.assertIn("unknown_at_engine", kinds)
        self.assertIn("missing_at_broker", kinds)

    def test_retry_on_retryable_failure(self):
        calls = {"n": 0}
        port = self.router.routes[0].port

        class Flaky:
            name = "sim"
            def submit(self, order):
                calls["n"] += 1
                from apex.models import Ack
                if calls["n"] < 2:
                    return Ack(ok=False, client_id=order.client_id, ts=5, reason="transient",
                               retryable=True)
                return Ack(ok=True, client_id=order.client_id, ts=6, order_id="OK-1",
                           filled_price=3000.0, lots=order.lots)
            def account(self): return port.account()
            def positions(self): return []
            def quote(self, s): return None
            def spec(self, s): return port.spec(s)
            def close(self, p, r, price=None): return None
            def modify(self, p, sl, tp): return None

        router = BrokerRouter([Route(broker="sim", account="SIM-1", port=Flaky())],
                              registry=self.reg, retry_sleep=0)
        ack = router.submit(self._order())
        self.assertTrue(ack.ok)
        self.assertEqual(router.stats["retried"], 1)


# --------------------------------------------------------------------------- #
class FakeMT5:
    """Minimal stand-in for the MetaTrader5 module (Windows-only in production)."""
    TIMEFRAME_M5 = 5
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_SLTP = 3
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_PRICE_CHANGED = 10020
    TRADE_RETCODE_TIMEOUT = 10012

    def __init__(self):
        self.sent = []
        self.next_retcode = self.TRADE_RETCODE_DONE

    def initialize(self, **kw): return True
    def shutdown(self): return True
    def last_error(self): return (0, "ok")

    def account_info(self):
        return SimpleNamespace(login=123456, currency="USD", balance=50_000.0, equity=50_100.0,
                               margin=250.0, margin_free=49_850.0, leverage=500)

    def symbol_info(self, name):
        return SimpleNamespace(name=name, digits=5, point=0.00001, trade_stops_level=10,
                               volume_min=0.01, volume_max=20.0, volume_step=0.01,
                               trade_contract_size=100_000, visible=True)

    def symbol_select(self, name, enable): return True

    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=1.08490, ask=1.08502, time=1_700_000_000)

    def positions_get(self):
        return [SimpleNamespace(ticket=9001, symbol="EURUSD", type=0, volume=0.5,
                                price_open=1.0800, sl=1.0750, tp=1.0900, time=1_700_000_000,
                                comment="AQ:scalp", magic=20261109, profit=12.5)]

    def order_send(self, request):
        self.sent.append(request)
        if self.next_retcode != self.TRADE_RETCODE_DONE:
            return SimpleNamespace(retcode=self.next_retcode, comment="rejected")
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done",
                               order=555, deal=777, price=request.get("price", 1.085),
                               volume=request.get("volume", 0.1))


class TestMT5Adapter(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMT5()
        conn = MT5Connection(MT5Config(login=123456, password="x", server="FP-Demo"),
                             module=self.fake)
        self.broker = MT5Broker(account="FP-1", registry=SymbolRegistry(), conn=conn,
                                dry_run=False)
        self.assertTrue(self.broker.connect())

    def test_account_maps_native_fields(self):
        a = self.broker.account()
        self.assertEqual(a.equity, 50_100.0)
        self.assertEqual(a.leverage, 500.0)
        self.assertTrue(a.live)

    def test_positions_map_to_domain(self):
        pos = self.broker.positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0].side, Side.BUY)
        self.assertEqual(pos[0].lots, 0.5)
        self.assertEqual(pos[0].contract_id, "9001")

    def test_order_send_payload(self):
        ack = self.broker.submit(Order(symbol="EURUSD", side=Side.BUY, lots=0.1, broker="mt5",
                                       account="FP-1", sl=1.0800, tp=1.0900,
                                       strategy="scalp_balanced"))
        self.assertTrue(ack.ok)
        req = self.fake.sent[-1]
        self.assertEqual(req["action"], FakeMT5.TRADE_ACTION_DEAL)
        self.assertEqual(req["volume"], 0.1)
        self.assertEqual(req["sl"], 1.08)
        self.assertEqual(req["tp"], 1.09)
        self.assertIn("scalp", req["comment"])

    def test_retcode_mapping(self):
        self.fake.next_retcode = FakeMT5.TRADE_RETCODE_REQUOTE
        ack = self.broker.submit(Order(symbol="EURUSD", side=Side.BUY, lots=0.1,
                                       broker="mt5", account="FP-1"))
        self.assertFalse(ack.ok)
        self.assertTrue(ack.retryable)

    def test_dry_run_never_sends(self):
        fake = FakeMT5()
        conn = MT5Connection(MT5Config(), module=fake)
        broker = MT5Broker(account="FP-DRY", registry=SymbolRegistry(), conn=conn, dry_run=True)
        broker.connect()
        ack = broker.submit(Order(symbol="EURUSD", side=Side.BUY, lots=1.0, broker="mt5",
                                  account="FP-DRY", price=1.085))
        self.assertTrue(ack.ok)
        self.assertEqual(fake.sent, [])
        self.assertEqual(len(broker.positions()), 1)


if __name__ == "__main__":
    unittest.main()
