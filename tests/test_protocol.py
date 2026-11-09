"""Protocol-level tests: WebSocket framing, Deriv JSON-RPC flow, adapter safety."""
import asyncio
import json
import struct
import unittest

from apex.data.deriv import DerivAPI, DerivConfig, DerivError
from apex.data.ws import decode_frame, encode_frame
from apex.execution.deriv import DerivBroker
from apex.models import Order, Side


class TestWebSocketFraming(unittest.TestCase):
    def test_roundtrip_small(self):
        raw = encode_frame(b'{"hello":"world"}')
        parsed, need = decode_frame(raw)
        self.assertEqual(need, 0)
        opcode, payload, consumed = parsed
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload, b'{"hello":"world"}')
        self.assertEqual(consumed, len(raw))

    def test_roundtrip_medium_length(self):
        body = b"x" * 4000
        raw = encode_frame(body)
        self.assertEqual(raw[1] & 0x7F, 126)
        parsed, _ = decode_frame(raw)
        self.assertEqual(parsed[1], body)

    def test_roundtrip_large_length(self):
        body = b"y" * 70000
        raw = encode_frame(body)
        self.assertEqual(raw[1] & 0x7F, 127)
        parsed, _ = decode_frame(raw)
        self.assertEqual(len(parsed[1]), 70000)

    def test_partial_frame_reports_need(self):
        raw = encode_frame(b"abcdefghij")
        parsed, need = decode_frame(raw[:5])
        self.assertIsNone(parsed)
        self.assertGreater(need, 0)

    def test_client_frames_are_masked(self):
        raw = encode_frame(b"ping")
        self.assertTrue(raw[1] & 0x80)

    def test_unmasked_server_frame_decodes(self):
        # build a server->client frame (no mask bit)
        payload = b'{"msg_type":"tick"}'
        frame = bytes([0x81, len(payload)]) + payload
        parsed, _ = decode_frame(frame)
        self.assertEqual(parsed[1], payload)


# --------------------------------------------------------------------------- #
class FakeWS:
    """Canned Deriv responder: enough to exercise request/response correlation."""

    def __init__(self, url):
        self.url = url
        self.sent = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def connect(self):
        return None

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        rid = msg.get("req_id")
        if "authorize" in msg:
            body = {"authorize": {"loginid": "CR123", "currency": "USD"}}
        elif "ticks_history" in msg:
            g = msg.get("granularity", 300)
            body = {"candles": [
                {"epoch": 1_700_000_000 + i * g, "open": 100 + i, "high": 101 + i,
                 "low": 99 + i, "close": 100.5 + i} for i in range(5)],
                "msg_type": "candles"}
        elif "balance" in msg:
            body = {"balance": {"balance": 1234.56, "currency": "USD"}}
        elif "proposal" in msg:
            body = {"proposal": {"id": "prop-1", "ask_price": msg.get("amount", 10)}}
        elif "buy" in msg:
            body = {"buy": {"contract_id": 778899, "buy_price": 10.0, "payout": 11.2}}
        elif "sell" in msg:
            body = {"sell": {"sold_for": 9.4}}
        elif "portfolio" in msg:
            body = {"portfolio": {"contracts": []}}
        elif "ticks" in msg:
            body = {"subscription": {"id": "sub-1"}, "tick": {"symbol": msg["ticks"],
                                                               "quote": 100.0, "epoch": 1},
                    "msg_type": "tick"}
            await self.queue.put(json.dumps(body))          # subscription stream tick
            await self.queue.put(json.dumps(
                {"subscription": {"id": "sub-1"},
                 "tick": {"symbol": msg["ticks"], "quote": 101.0, "epoch": 2}}))
        elif "error_test" in msg:
            await self.queue.put(json.dumps({"error": {"code": "BadRequest", "message": "nope"},
                                             "req_id": rid}))
            return
        else:
            body = {}
        body["req_id"] = rid
        await self.queue.put(json.dumps(body))

    async def messages(self):
        while not self.closed:
            yield await self.queue.get()

    async def close(self):
        self.closed = True


class TestDerivAPI(unittest.TestCase):
    def _api(self):
        holder = {}

        def factory(url):
            holder["ws"] = FakeWS(url)
            return holder["ws"]

        api = DerivAPI(DerivConfig(app_id="test", token="tok"), ws_factory=factory)
        return api, holder

    def test_candles_parsed_to_bars(self):
        async def run():
            api, holder = self._api()
            await api.connect()
            bars = await api.candles("R_75", 300, 5)
            await api.close()
            return bars, holder["ws"]
        bars, ws = asyncio.run(run())
        self.assertEqual(len(bars), 5)
        self.assertEqual(bars[0].symbol, "R_75")
        self.assertEqual(bars[0].open, 100.0)
        history = [m for m in ws.sent if "ticks_history" in m]
        self.assertEqual(history[0]["ticks_history"], "R_75")
        self.assertEqual(history[0]["style"], "candles")

    def test_authorize_sets_account(self):
        async def run():
            api, _ = self._api()
            await api.connect()
            out = {"login": api.logged_in, "auth": api.authorized, "bal": await api.get_balance()}
            await api.close()
            return out
        out = asyncio.run(run())
        self.assertEqual(out["login"], "CR123")
        self.assertTrue(out["auth"])
        self.assertAlmostEqual(out["bal"], 1234.56)

    def test_error_response_raises_deriv_error(self):
        async def run():
            api, _ = self._api()
            await api.connect()
            try:
                await api.request({"error_test": 1})
            finally:
                await api.close()
        with self.assertRaises(DerivError):
            asyncio.run(run())

    def test_invalid_granularity_rejected_before_network(self):
        async def run():
            api, _ = self._api()
            await api.connect()
            try:
                await api.candles("R_75", 301, 5)
            finally:
                await api.close()
        with self.assertRaises(DerivError):
            asyncio.run(run())

    def test_subscription_callback_wired(self):
        async def run():
            api, _ = self._api()
            await api.connect()
            got = []
            await api.subscribe_ticks("R_25", got.append)
            await asyncio.sleep(0.05)
            await api.close()
            return got
        got = asyncio.run(run())
        self.assertTrue(got)
        self.assertEqual(got[0]["tick"]["symbol"], "R_25")


class TestDerivBrokerSafety(unittest.TestCase):
    def test_dry_run_never_transmits(self):
        broker = DerivBroker(account="D-1", dry_run=True)
        ack = broker.submit(Order(symbol="R_75", side=Side.BUY, lots=2.0, broker="deriv",
                                  account="D-1", price=48000.0, sl=47900.0, tp=48200.0))
        self.assertTrue(ack.ok)
        self.assertEqual(len(broker.positions()), 1)
        self.assertEqual(ack.raw.get("stake"), 960.0)     # 2 lots * 1 * 48000 / 100

    def test_live_path_with_fake_api(self):
        from apex.execution.deriv import AsyncRunner
        api, _ = TestDerivAPI()._api()
        runner = AsyncRunner()
        runner.call(api.connect())          # same loop the broker will use
        broker = DerivBroker(account="D-2", api=api, dry_run=False, runner=runner)
        try:
            ack = broker.submit(Order(symbol="R_75", side=Side.SELL, lots=1.0, broker="deriv",
                                      account="D-2", price=48000.0))
        finally:
            runner.stop()
        self.assertTrue(ack.ok)
        self.assertEqual(ack.order_id, "778899")
        self.assertEqual(ack.raw["contract_id"], 778899)

    def test_entry_price_required(self):
        broker = DerivBroker(account="D-3", dry_run=True)
        ack = broker.submit(Order(symbol="R_75", side=Side.BUY, lots=1.0, broker="deriv",
                                  account="D-3"))
        self.assertFalse(ack.ok)
        self.assertTrue(ack.retryable)

    def test_portfolio_stake_is_not_treated_as_lots(self):
        """A Deriv contract has a stake, not lots. Reporting buy_price as lots made
        every live position look like a size mismatch against the engine."""
        class StubAPI:
            def portfolio(self):
                return [{"contract_id": 11, "symbol": "R_75", "buy_price": 100.0,
                         "current_spot": 50000.0, "multiplier": 100,
                         "contract_type": "MULTDOWN", "purchase_time": 1_700_000_000}]

        class StubRunner:
            def call(self, coro):        # the stub returns the payload directly
                return coro

        broker = DerivBroker(account="D-5", api=StubAPI(), dry_run=False,
                             runner=StubRunner(), multiplier=100.0)
        pos = broker.positions()[0]
        # stake 100 * multiplier 100 / (value-per-point 1 * spot 50000) = 0.2 lots
        self.assertAlmostEqual(pos.lots, 0.2, places=6)
        self.assertEqual(pos.entry, 50000.0)
        self.assertEqual(pos.side, Side.SELL)
        self.assertTrue(pos.meta["lots_estimated"])
        self.assertEqual(pos.meta["stake"], 100.0)

    def test_engine_order_price_satisfies_deriv_submission(self):
        """Deriv refuses an order with no price; the engine must always send one."""
        broker = DerivBroker(account="D-6", dry_run=True)
        ack = broker.submit(Order(symbol="R_75", side=Side.BUY, lots=1.0, broker="deriv",
                                  account="D-6", price=48000.0))
        self.assertTrue(ack.ok)

    def test_modify_is_honest_about_being_unsupported(self):
        broker = DerivBroker(account="D-4", dry_run=True)
        pos = broker.submit(Order(symbol="R_75", side=Side.BUY, lots=1.0, broker="deriv",
                                  account="D-4", price=48000.0)).order_id
        ack = broker.modify(broker.positions()[0], 47900, 48200)
        self.assertFalse(ack.ok)
        self.assertIn("not supported", ack.reason)


if __name__ == "__main__":
    unittest.main()
