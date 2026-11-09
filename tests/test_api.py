"""Dashboard API: read paths stay open, operator paths require a token."""
import json
import threading
import unittest
import urllib.error
import urllib.request

from apex.api.server import DashboardState, serve


class TestDashboardAPI(unittest.TestCase):
    token = "s3cret"

    def setUp(self):
        self.state = DashboardState(snapshot_fn=lambda: {"mode": "paper", "total_equity": 1.0},
                                    token=self.token)
        self.httpd = serve(self.state, host="127.0.0.1", port=0)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _req(self, path, method="GET", headers=None, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    # ------------------------------------------------------------------ #
    def test_state_is_public_but_not_cached(self):
        code, body, headers = self._req("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["mode"], "paper")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_health_and_static_assets(self):
        self.assertEqual(self._req("/api/health")[0], 200)
        code, body, headers = self._req("/static/style.css")
        self.assertEqual(code, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        self.assertIn(b"--", body)

    def test_unknown_path_is_404(self):
        self.assertEqual(self._req("/api/nope")[0], 404)

    def test_path_traversal_is_blocked(self):
        self.assertEqual(self._req("/static/../../etc/passwd")[0], 404)

    def test_control_requires_token(self):
        self.assertEqual(self._req("/api/control/kill", "POST", body=b"{}")[0], 401)
        self.assertEqual(self._req("/api/control/kill", "POST", body=b"{}",
                                   headers={"X-Apex-Token": "wrong"})[0], 401)

    def test_control_accepts_bearer_token(self):
        self.assertEqual(self._req("/api/control/rearm", "POST", body=b"{}",
                                   headers={"Authorization": "Bearer " + self.token})[0], 200)

    def test_control_accepts_header_token(self):
        code, body, _ = self._req("/api/control/flatten", "POST", body=b"{}",
                                  headers={"X-Apex-Token": self.token})
        self.assertEqual(code, 200)
        self.assertIn("ok", json.loads(body))

    def test_cross_origin_control_is_rejected(self):
        code, _, _ = self._req("/api/control/kill", "POST", body=b"{}",
                               headers={"X-Apex-Token": self.token,
                                        "Origin": "https://evil.example"})
        self.assertEqual(code, 403)

    def test_no_token_configured_leaves_control_open_for_localhost(self):
        state = DashboardState(snapshot_fn=lambda: {"mode": "paper"})
        httpd = serve(state, host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/control/rearm",
                                         data=b"{}", method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
