"""Zero-dependency dashboard server (stdlib http.server + SSE).

The audit's complaint was not "we lack a UI", it was "nobody can tell what the
engine is doing or why". This dashboard answers, live:

  * what each instrument's regime, conviction and agent votes are;
  * which trade the engine is in, with stop, target and unrealised P&L;
  * why trades were *not* taken (coded rejection counters);
  * prop-challenge progress against every rule, with pacing;
  * per-strategy / per-broker / per-instrument attribution;
  * broker router state, kill switch and reconciliation alerts.
"""
from __future__ import annotations

import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlparse

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class DashboardState:
    """Holds the live runner and exposes a thread-safe snapshot."""

    def __init__(self, runner=None, snapshot_fn: Optional[Callable[[], dict]] = None,
                 token: str = ""):
        self.runner = runner
        self._snapshot_fn = snapshot_fn
        self.token = token or ""
        self._lock = threading.RLock()
        self._cache: dict = {}
        self._cache_ts = 0.0

    def snapshot(self, max_age: float = 1.0) -> dict:
        with self._lock:
            now = time.time()
            if now - self._cache_ts < max_age and self._cache:
                return self._cache
            try:
                if self._snapshot_fn is not None:
                    snap = self._snapshot_fn()
                elif self.runner is not None:
                    snap = self.runner.snapshot()
                else:
                    snap = {"error": "no engine attached"}
            except Exception as exc:            # never take the dashboard down
                snap = {"error": f"{type(exc).__name__}: {exc}", "ts": now}
            snap["server_ts"] = now
            self._cache, self._cache_ts = snap, now
            return snap

    # ---- controls ---------------------------------------------------- #
    def kill(self, flatten: bool = True) -> dict:
        if self.runner is None:
            return {"ok": False, "reason": "no engine"}
        acks = self.runner.router.kill(flatten=flatten)
        self.runner.store.record_event("control", f"kill switch engaged (flatten={flatten})")
        try:
            self.runner.engine.save_state("engine")
        except Exception:
            pass
        return {"ok": True, "closed": len(acks)}

    def rearm(self) -> dict:
        if self.runner is None:
            return {"ok": False, "reason": "no engine"}
        self.runner.router.rearm()
        self.runner.engine.risk_engine.resume()
        self.runner.store.record_event("control", "kill switch released, risk re-armed")
        return {"ok": True}

    def flatten(self) -> dict:
        if self.runner is None:
            return {"ok": False, "reason": "no engine"}
        self.runner.engine.flatten_all("manual_flatten")
        return {"ok": True}


def make_handler(state: DashboardState):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ABUTRON-QUANT/2.0"

        def log_message(self, fmt, *args):     # keep the console usable
            return

        # -------------------------------------------------------------- #
        def _send(self, code: int, body: bytes, ctype: str = "application/json",
                  extra: Optional[dict] = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self) -> None:              # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                return self._file("index.html", "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(state.snapshot())
            if path == "/api/trades":
                tr = state.runner.engine.trades if state.runner else []
                return self._json([t.as_dict() for t in tr][-200:])
            if path == "/api/health":
                return self._json({"ok": True, "uptime_s": round(time.time() - START, 1),
                                   "version": "2.1.0"})
            if path == "/api/stream":
                return self._stream()
            if path.startswith("/static/"):
                return self._file(path[len("/static/"):])
            if path == "/favicon.ico":
                return self._send(204, b"")
            return self._send(404, b'{"error":"not found"}')

        def _authorized(self) -> bool:
            """Operator endpoints mutate real positions: they need a token.

            With no token configured the endpoints are open, which is only
            acceptable because the dashboard binds to localhost by default.
            """
            if not state.token:
                return True
            supplied = (self.headers.get("X-Apex-Token")
                        or (self.headers.get("Authorization") or "").replace("Bearer ", ""))
            return hmac.compare_digest(str(supplied or ""), str(state.token))

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if not origin:
                return True
            host = self.headers.get("Host", "")
            return host and host in origin

        def do_POST(self) -> None:             # noqa: N802
            path = urlparse(self.path).path
            if not self._origin_ok():
                return self._json({"ok": False, "error": "cross-origin control rejected"}, 403)
            if not self._authorized():
                return self._json({"ok": False, "error": "token required"}, 401)
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    self.rfile.read(length)
                except Exception:
                    pass
            if path == "/api/control/kill":
                return self._json(state.kill(flatten=True))
            if path == "/api/control/rearm":
                return self._json(state.rearm())
            if path == "/api/control/flatten":
                return self._json(state.flatten())
            return self._send(404, b'{"error":"not found"}')

        # -------------------------------------------------------------- #
        def _file(self, name: str, ctype: Optional[str] = None) -> None:
            # Resolve, then prove the result is still inside the static root.
            # A naive `..` strip is not enough: "//etc/passwd" survives it and
            # os.path.join happily escapes the directory with an absolute path.
            name = name.lstrip("/\\").replace("\x00", "")
            root = os.path.abspath(STATIC_DIR)
            full = os.path.abspath(os.path.normpath(os.path.join(root, name)))
            if not (full == root or full.startswith(root + os.sep)) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = ctype or {
                ".html": "text/html; charset=utf-8", ".js": "application/javascript",
                ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json",
            }.get(os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as fh:
                body = fh.read()
            self._send(200, body, ctype)

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    payload = json.dumps(state.snapshot(), default=str)
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(2.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

    return Handler


START = time.time()


def serve(state: DashboardState, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.daemon_threads = True
    print(f"[dashboard] http://{host}:{port}")
    return httpd


def run_dashboard(state: DashboardState, host: str = "0.0.0.0", port: int = 8000) -> None:
    httpd = serve(state, host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
