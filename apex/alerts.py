"""Operator alerting.

A desk that stops trading and tells nobody is indistinguishable from a desk that
is working. Every critical event — halt, breach, kill switch, reconciliation
drift, feed death — goes through here.

Design constraints:

* **Never block trading.** A dead webhook must not stall a bar. All sends are
  best effort with a short timeout and swallowed errors.
* **Never page twice for the same thing.** Alerts are throttled per kind, so a
  flapping feed produces one message a minute, not two hundred.
* **No dependencies.** urllib + smtplib, standard library only.
"""
from __future__ import annotations

import json
import smtplib
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Callable, Optional

#: event kinds worth waking someone up for
DEFAULT_KINDS = ("risk", "reconcile", "error", "control", "breach", "watchdog")


@dataclass
class AlertConfig:
    webhook_url: str = ""
    webhook_headers: dict[str, str] = field(default_factory=dict)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "apex@localhost"
    smtp_to: list[str] = field(default_factory=list)
    smtp_starttls: bool = True
    timeout_s: float = 3.0
    min_interval_s: float = 60.0          # per kind
    kinds: tuple[str, ...] = DEFAULT_KINDS

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url or (self.smtp_host and self.smtp_to))


class Notifier:
    """Fan-out with per-kind throttling. Safe to call from the trading thread."""

    def __init__(self, cfg: Optional[AlertConfig] = None,
                 clock: Callable[[], float] = time.time,
                 sender: Optional[Callable[[str, bytes, dict], None]] = None,
                 mailer: Optional[Callable[[AlertConfig, str, str], None]] = None):
        self.cfg = cfg or AlertConfig()
        self.clock = clock
        self._sender = sender or _post_json
        self._mailer = mailer or _send_mail
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self.stats = {"sent": 0, "throttled": 0, "failed": 0, "skipped": 0}

    # ------------------------------------------------------------------ #
    def should_send(self, kind: str) -> bool:
        if not self.cfg.enabled:
            return False
        if self.cfg.kinds and kind not in self.cfg.kinds:
            return False
        now = self.clock()
        with self._lock:
            last = self._last.get(kind, 0.0)
            if now - last < self.cfg.min_interval_s:
                self.stats["throttled"] += 1
                return False
            self._last[kind] = now
        return True

    def alert(self, kind: str, message: str, level: str = "warn",
              detail: Optional[dict] = None) -> bool:
        """Send one alert. Returns True when at least one channel accepted it."""
        if not self.should_send(kind):
            if not self.cfg.enabled:
                self.stats["skipped"] += 1
            return False
        payload = {"ts": self.clock(), "kind": kind, "level": level,
                   "message": str(message)[:500], "detail": detail or {}}
        sent = False
        if self.cfg.webhook_url:
            try:
                body = json.dumps(payload, default=str).encode()
                self._sender(self.cfg.webhook_url, body, self.cfg.webhook_headers)
                sent = True
            except Exception:
                self.stats["failed"] += 1
        if self.cfg.smtp_host and self.cfg.smtp_to:
            try:
                self._mailer(self.cfg, f"[apex:{level}] {kind}", _format_body(payload))
                sent = True
            except Exception:
                self.stats["failed"] += 1
        if sent:
            self.stats["sent"] += 1
        return sent

    def from_event(self, kind: str, message: str) -> None:
        """Adapter for the engine's `on_event` callback."""
        level = "error" if kind == "error" else ("info" if kind in ("info", "entry", "exit") else "warn")
        self.alert(kind, message, level=level)


def _post_json(url: str, body: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=3) as resp:
        resp.read(1)


def _send_mail(cfg: AlertConfig, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(cfg.smtp_to)
    msg.set_content(body)
    with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=cfg.timeout_s) as smtp:
        if cfg.smtp_starttls:
            smtp.starttls()
        if cfg.smtp_user:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
        smtp.send_message(msg)


def _format_body(payload: dict) -> str:
    lines = [f"kind:    {payload['kind']}",
             f"level:   {payload['level']}",
             f"time:    {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(payload['ts']))}",
             "",
             payload["message"]]
    if payload.get("detail"):
        lines += ["", json.dumps(payload["detail"], indent=2, default=str)[:2000]]
    return "\n".join(lines)


def notifier_from_config(cfg: dict, clock: Callable[[], float] = time.time) -> Notifier:
    """Build a Notifier from the JSON config's `alerts` block."""
    block = (cfg or {}).get("alerts") or {}
    return Notifier(AlertConfig(
        webhook_url=block.get("webhook_url", ""),
        webhook_headers=block.get("webhook_headers", {}) or {},
        smtp_host=block.get("smtp_host", ""),
        smtp_port=int(block.get("smtp_port", 587)),
        smtp_user=block.get("smtp_user", ""),
        smtp_password=block.get("smtp_password", ""),
        smtp_from=block.get("smtp_from", "apex@localhost"),
        smtp_to=list(block.get("smtp_to", []) or []),
        smtp_starttls=bool(block.get("smtp_starttls", True)),
        min_interval_s=float(block.get("min_interval_s", 60)),
        kinds=tuple(block.get("kinds", DEFAULT_KINDS)),
    ), clock=clock)
