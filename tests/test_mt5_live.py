"""FP Markets specifics that only bite in production: server time and fill state."""
import time
import unittest
from types import SimpleNamespace

from apex.data.mt5 import MT5Config, MT5Connection
from apex.execution.base import SymbolRegistry
from apex.execution.mt5 import MT5Broker
from apex.models import Order, Side


class FakeMT5:
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
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_PRICE_CHANGED = 10020
    TRADE_RETCODE_TIMEOUT = 10012

    def __init__(self, server_offset_s: float = 0.0, volume_ratio: float = 1.0,
                 retcode: int = 10009):
        self.server_offset_s = server_offset_s
        self.volume_ratio = volume_ratio
        self.retcode = retcode
        self.sent = []

    # ---- module surface ---------------------------------------------- #
    def initialize(self, **kw): return True
    def shutdown(self): return True
    def last_error(self): return (0, "ok")
    def account_info(self):
        return SimpleNamespace(login=1, currency="USD", balance=10_000.0, equity=10_000.0,
                               margin=0.0, margin_free=10_000.0, leverage=500)
    def symbols_get(self):
        return [SimpleNamespace(name="EURUSD.m")]
    def symbol_select(self, name, enable=True): return True
    def symbol_info(self, name):
        return SimpleNamespace(name="EURUSD.m", digits=5, point=0.00001, trade_stops_level=10,
                               volume_min=0.01, volume_max=20.0, volume_step=0.01,
                               trade_contract_size=100_000, visible=True)
    def symbol_info_tick(self, name):
        return SimpleNamespace(bid=1.08500, ask=1.08514,
                               time=int(time.time() + self.server_offset_s))
    def copy_rates_from_pos(self, symbol, tf, start, count):
        base = (int(time.time() + self.server_offset_s) // 300 - count) * 300
        return [{"time": base + i * 300, "open": 1.08 + i * 1e-5, "high": 1.08 + i * 1e-5 + 1e-4,
                 "low": 1.08 + i * 1e-5 - 1e-4, "close": 1.08 + i * 1e-5, "tick_volume": 10}
                for i in range(count)]
    def positions_get(self): return []
    def order_send(self, request):
        self.sent.append(request)
        if self.retcode != self.TRADE_RETCODE_DONE:
            return SimpleNamespace(retcode=self.retcode, comment="pending", order=900, deal=0,
                                   price=request["price"], volume=0.0)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, comment="done", order=900,
                               deal=901, price=request["price"],
                               volume=request["volume"] * self.volume_ratio)


class TestServerTime(unittest.TestCase):
    def test_detects_a_two_hour_ahead_server(self):
        conn = MT5Connection(MT5Config(), module=FakeMT5(server_offset_s=7200))
        conn.detect_offset()
        self.assertAlmostEqual(conn.offset_hours, 2.0, places=2)
        self.assertAlmostEqual(conn.to_utc(time.time() + 7200), time.time(), delta=2)

    def test_detects_three_hour_dst_server(self):
        conn = MT5Connection(MT5Config(), module=FakeMT5(server_offset_s=3 * 3600))
        conn.detect_offset()
        self.assertAlmostEqual(conn.offset_hours, 3.0, places=2)

    def test_config_override_wins(self):
        conn = MT5Connection(MT5Config(server_utc_offset_hours=1.5),
                             module=FakeMT5(server_offset_s=7200))
        conn.detect_offset()
        self.assertAlmostEqual(conn.offset_hours, 1.5, places=2)

    def test_stale_weekend_tick_is_not_treated_as_a_timezone(self):
        fake = FakeMT5(server_offset_s=0)
        fake.symbol_info_tick = lambda name: SimpleNamespace(bid=1.0, ask=1.1,
                                                             time=int(time.time()) - 49 * 3600)
        conn = MT5Connection(MT5Config(), module=fake)
        self.assertEqual(conn.detect_offset(), 0.0)

    def test_quotes_and_bars_are_converted_to_utc(self):
        fake = FakeMT5(server_offset_s=7200)
        conn = MT5Connection(MT5Config(), module=fake)
        conn.connect()
        q = conn.tick("EURUSD")
        self.assertAlmostEqual(q.ts, time.time(), delta=3)
        bars = conn.rates("EURUSD", "M5", 5)
        self.assertEqual(len(bars), 5)
        # timestamps must be in the past once converted, never 2h in the future
        self.assertLess(bars[-1].ts, time.time() + 5)


class TestFillState(unittest.TestCase):
    def _broker(self, **kw):
        fake = FakeMT5(**kw)
        conn = MT5Connection(MT5Config(login=1, password="x", server="FP-Demo"), module=fake)
        broker = MT5Broker(account="FP-1", registry=SymbolRegistry(), conn=conn, dry_run=False)
        broker.connect()
        return broker, fake

    def test_full_fill(self):
        broker, _ = self._broker()
        ack = broker.submit(Order(symbol="EURUSD", side=Side.BUY, lots=0.10, broker="mt5",
                                  account="FP-1", sl=1.08, tp=1.09))
        self.assertTrue(ack.ok)
        self.assertEqual(ack.state, "filled")
        self.assertAlmostEqual(ack.lots, 0.10)

    def test_partial_fill_reports_the_dealt_volume(self):
        broker, _ = self._broker(volume_ratio=0.5)
        ack = broker.submit(Order(symbol="EURUSD", side=Side.BUY, lots=0.10, broker="mt5",
                                  account="FP-1", sl=1.08, tp=1.09))
        self.assertTrue(ack.ok)
        self.assertEqual(ack.state, "partial")
        self.assertAlmostEqual(ack.lots, 0.05)
        self.assertAlmostEqual(ack.requested_lots, 0.10)

    def test_pending_order_does_not_become_a_position(self):
        broker, _ = self._broker(retcode=10008)      # TRADE_RETCODE_PLACED
        ack = broker.submit(Order(symbol="EURUSD", side=Side.BUY, lots=0.10, broker="mt5",
                                  account="FP-1", sl=1.08, tp=1.09))
        self.assertTrue(ack.ok)
        self.assertEqual(ack.state, "pending")
        self.assertEqual(ack.lots, 0.0)
        self.assertEqual(broker.positions(), [])


if __name__ == "__main__":
    unittest.main()
