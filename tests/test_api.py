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

    def test_health_reports_the_package_version(self):
        from apex import __version__
        code, body, _ = self._req("/api/health")
        self.assertEqual(code, 200)
        payload = json.loads(body)
        self.assertEqual(payload["version"], __version__)

    def test_health_and_static_assets(self):
        self.assertEqual(self._req("/api/health")[0], 200)
        code, body, headers = self._req("/static/theme.css")
        self.assertEqual(code, 200)
        self.assertIn("text/css", headers.get("Content-Type", ""))
        self.assertIn(b"--", body)

    def test_every_portal_is_served_and_bootstrap_based(self):
        """All six portals answer on clean URLs and load Bootstrap from the repo."""
        routes = {"/": "Overview", "/overview": "Overview", "/positions": "positions",
                  "/signals": "signals", "/risk": "risk", "/journal": "journal",
                  "/research": "research", "/system": "system"}
        for route, needle in routes.items():
            code, body, headers = self._req(route)
            self.assertEqual(code, 200, route)
            self.assertIn("text/html", headers.get("Content-Type", ""), route)
            text = body.decode()
            self.assertIn("ABUTRON QUANT", text, route)
            self.assertIn("/static/vendor/bootstrap/bootstrap.min.css", text, route)
            self.assertIn("/static/vendor/bootstrap/bootstrap.bundle.min.js", text, route)
            self.assertIn("/static/portal.js", text, route)
            if route != "/":
                self.assertIn(needle, text, route)

    def test_portals_have_no_network_dependencies(self):
        """The preview iframe has no internet: no CDN, font or telemetry URLs."""
        from apex.api.server import PAGES, STATIC_DIR
        import os
        html_files = sorted({name for name in PAGES.values() if name.endswith(".html")})
        for name in html_files:
            with open(os.path.join(STATIC_DIR, name), encoding="utf-8") as fh:
                text = fh.read()
            for bad in ("http://", "https://", "//cdn", "integrity="):
                self.assertNotIn(bad, text, f"{name} references {bad}")
        with open(os.path.join(STATIC_DIR, "portal.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertNotIn("cdn.jsdelivr", js)
        for path in ("/static/vendor/bootstrap/bootstrap.min.css",
                     "/static/vendor/bootstrap/bootstrap.bundle.min.js",
                     "/static/portal.js", "/static/theme.css"):
            code, body, headers = self._req(path)
            self.assertEqual(code, 200, path)
            self.assertGreater(len(body), 500, path)

    def test_research_endpoint_lists_artefacts(self):
        code, body, _ = self._req("/api/research")
        self.assertEqual(code, 200)
        report = json.loads(body)
        for key in ("runs", "sweeps", "propsims", "caveat"):
            self.assertIn(key, report)
        self.assertTrue(report["caveat"])

    def test_verify_endpoint_degrades_without_a_journal(self):
        code, body, _ = self._req("/api/verify")
        self.assertEqual(code, 200)
        report = json.loads(body)
        self.assertIn("checks", report)
        self.assertIn("ok", report)

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
