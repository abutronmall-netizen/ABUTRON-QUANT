"""Optional local LLM reviewer: advisory, veto, and fail-open behaviour.

A model that can block trades is a second risk engine. These tests pin the two
properties that keep it harmless: it can only *reduce* risk, and it never blocks
trading when it is unreachable or returns garbage.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apex.ai.reviewer import OllamaReviewer, ReviewerConfig
from apex.models import Candidate, Side
from tests.test_live import FakePort, build


class StubOllama:
    """Minimal Ollama surface: /api/tags and /api/generate."""

    def __init__(self, response: dict, status: int = 200, tags: dict | None = None):
        self.response = response
        self.status = status
        self.tags = tags or {"models": [{"name": "qwen2.5:1.5b"}]}
        self.received = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):        # keep test output clean
                return

            def do_GET(self):                 # noqa: N802
                body = json.dumps(outer.tags).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):                # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                outer.received.append(json.loads(self.rfile.read(length) or b"{}"))
                body = json.dumps(outer.response).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def generate_response(approve: bool = True, confidence: float = 0.8) -> dict:
    inner = json.dumps({"approve": approve, "confidence": confidence, "risk_note": "ok"})
    return {"response": inner, "done": True}


class TestReviewer(unittest.TestCase):
    def make(self, **kw) -> tuple[OllamaReviewer, StubOllama]:
        stub = StubOllama(**kw)
        self.addCleanup(stub.close)
        return OllamaReviewer(ReviewerConfig(url=stub.url, timeout_s=2.0)), stub

    def test_health_reports_models(self):
        rev, _ = self.make(response=generate_response())
        ok, detail = rev.health()
        self.assertTrue(ok)
        self.assertIn("qwen", detail)

    def test_review_parses_strict_json(self):
        rev, stub = self.make(response=generate_response(approve=False, confidence=0.91))
        out = rev.review({"symbol": "R_25", "conviction": 0.4})
        self.assertFalse(out["approve"])
        self.assertAlmostEqual(out["confidence"], 0.91)
        self.assertEqual(stub.received[-1]["format"], "json")
        self.assertEqual(stub.received[-1]["options"]["temperature"], 0.0)

    def test_advisory_mode_never_blocks(self):
        rev, _ = self.make(response=generate_response(approve=False, confidence=0.99))
        rev.cfg.mode = "advisory"
        allowed, out = rev.gate({"symbol": "R_25"})
        self.assertTrue(allowed)
        self.assertFalse(out["approve"])

    def test_veto_mode_blocks_a_confident_rejection(self):
        rev, _ = self.make(response=generate_response(approve=False, confidence=0.9))
        rev.cfg.mode = "veto"
        allowed, out = rev.gate({"symbol": "R_25"})
        self.assertFalse(allowed)
        self.assertEqual(out["risk_note"], "ok")

    def test_veto_mode_ignores_a_hesitant_rejection(self):
        rev, _ = self.make(response=generate_response(approve=False, confidence=0.3))
        rev.cfg.mode = "veto"
        self.assertTrue(rev.gate({"symbol": "R_25"})[0])

    def test_unreachable_reviewer_fails_open(self):
        rev = OllamaReviewer(ReviewerConfig(url="http://127.0.0.1:9", timeout_s=0.5, mode="veto"))
        out = rev.review({"symbol": "R_25"})
        self.assertTrue(out["approve"])
        self.assertIn("unavailable", out["risk_note"])
        self.assertTrue(rev.gate({"symbol": "R_25"})[0])

    def test_garbage_response_fails_open(self):
        stub = StubOllama(response={"response": "I think this is fine!"})
        self.addCleanup(stub.close)
        rev = OllamaReviewer(ReviewerConfig(url=stub.url, timeout_s=2.0, mode="veto"))
        self.assertTrue(rev.gate({"symbol": "R_25"})[0])


class StubReviewer:
    def __init__(self, allowed: bool, note: str = "looks fine", raises: bool = False):
        self.allowed = allowed
        self.note = note
        self.raises = raises
        self.payloads = []

    def gate(self, payload):
        self.payloads.append(payload)
        if self.raises:
            raise RuntimeError("model crashed")
        return self.allowed, {"approve": self.allowed, "confidence": 0.9,
                              "risk_note": self.note}


def sized_candidate() -> Candidate:
    return Candidate(symbol="R_25", side=Side.BUY, conviction=0.7, price=2400.0,
                     sl=2390.0, tp=2420.0, ttl_s=60, strategy="scalp_balanced",
                     regime="trend_up")


class TestEngineIntegration(unittest.TestCase):
    """The reviewer may only shrink risk, and must never be able to break the desk."""

    def engine(self, reviewer, mode: str = "veto"):
        engine, _, _ = build(FakePort())
        engine.boot_sync("adopt")
        engine.cfg.ai_review = True
        engine.cfg.ai_mode = mode
        engine.reviewer = reviewer
        return engine

    def enter(self, engine):
        spec = engine.router.spec("R_25", "deriv")
        engine._try_enter(sized_candidate(), spec, 1_700_000_000.0, {})
        return engine

    def test_veto_blocks_the_entry(self):
        rev = StubReviewer(allowed=False, note="range extreme")
        engine = self.enter(self.engine(rev))
        self.assertEqual(engine.positions, {})
        self.assertEqual(engine.rejections.get("AI_VETO"), 1)
        self.assertEqual(len(rev.payloads), 1)
        self.assertIn("votes", rev.payloads[0])

    def test_advisory_review_allows_the_entry_even_on_objection(self):
        engine = self.enter(self.engine(StubReviewer(allowed=False), mode="advisory"))
        self.assertEqual(len(engine.positions), 1)
        self.assertIsNone(engine.rejections.get("AI_VETO"))

    def test_veto_mode_is_what_allows_blocking(self):
        engine, _, _ = build(FakePort())
        engine.boot_sync("adopt")
        engine.cfg.ai_review = True
        engine.cfg.ai_mode = "veto"
        engine.reviewer = StubReviewer(allowed=False, note="counter-trend")
        engine = self.enter(engine)
        self.assertEqual(engine.positions, {})
        self.assertEqual(engine.rejections.get("AI_VETO"), 1)

    def test_crashing_reviewer_fails_open(self):
        engine = self.enter(self.engine(StubReviewer(allowed=False, raises=True)))
        self.assertEqual(len(engine.positions), 1)

    def test_reviewer_is_not_called_when_disabled(self):
        rev = StubReviewer(allowed=False)
        engine, _, _ = build(FakePort())
        engine.reviewer = rev
        engine.cfg.ai_review = False
        self.enter(engine)
        self.assertEqual(rev.payloads, [])
        self.assertEqual(len(engine.positions), 1)


if __name__ == "__main__":
    unittest.main()
