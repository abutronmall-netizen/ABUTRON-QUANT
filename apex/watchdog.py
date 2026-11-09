"""Out-of-process watchdog.

Why this exists: some stops can only be held locally (Deriv multiplier contracts
cannot be modified). If the engine dies, those stops die with it and the broker
is left holding an unprotected position. Nothing inside the engine can fix that —
it is dead. So a second, tiny process watches a heartbeat file and reacts.

Reaction ladder on a stale heartbeat:

1. **alert** — webhook/SMTP, so a human knows.
2. **flatten request** — written to the journal (`watchdog_flatten_request`).
   Any engine that starts later honours it *before* adopting positions, so a
   restart after a crash does not silently inherit unprotected risk.
3. **direct flatten** — optional, when the watchdog is given a broker port
   (`--mode deriv|mt5 --live`). Closes what the broker actually holds.

The watchdog never trades, never opens anything and never competes with the
engine: flattening is the only write it can perform against the broker.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .alerts import Notifier, notifier_from_config

FLATTEN_REQUEST_KEY = "watchdog_flatten_request"
REQUEST_TTL_S = 6 * 3600          # a stale request must not kill tomorrow's session


@dataclass
class WatchdogConfig:
    heartbeat_path: str = "runs/live/heartbeat.json"
    stale_after_s: float = 30.0
    check_interval_s: float = 5.0
    flatten_on_stale: bool = True
    alerts: dict = field(default_factory=dict)
    max_flatten_attempts: int = 3


def read_heartbeat(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def heartbeat_age(path: str, clock: Callable[[], float] = time.time) -> Optional[float]:
    hb = read_heartbeat(path)
    if not hb or "ts" not in hb:
        return None
    try:
        return clock() - float(hb["ts"])
    except (TypeError, ValueError):
        return None


class Watchdog:
    def __init__(self, cfg: Optional[WatchdogConfig] = None, store=None,
                 notifier: Optional[Notifier] = None,
                 flattener: Optional[Callable[[], list]] = None,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg or WatchdogConfig()
        self.store = store
        self.notifier = notifier or notifier_from_config({"alerts": self.cfg.alerts}, clock=clock)
        self.flattener = flattener
        self.clock = clock
        self._episode = False          # currently inside a stale episode
        self._attempts = 0
        self._flatten_done = False
        self.stats = {"checks": 0, "stale_episodes": 0, "alerts": 0, "flattens": 0}

    # ------------------------------------------------------------------ #
    def check(self) -> dict:
        """One evaluation. Never raises: a watchdog that dies is worse than none."""
        self.stats["checks"] += 1
        age = heartbeat_age(self.cfg.heartbeat_path, self.clock)
        stale = age is None or age > self.cfg.stale_after_s
        if not stale:
            if self._episode:
                self._episode = False
                self._attempts = 0
                self._flatten_done = False
                self._alert("info", "engine heartbeat recovered")
            return {"stale": False, "age": age, "action": "none"}

        if not self._episode:
            self._episode = True
            self.stats["stale_episodes"] += 1
            hb = read_heartbeat(self.cfg.heartbeat_path) or {}
            detail = {"age_s": None if age is None else round(age, 1),
                      "positions": hb.get("positions", []),
                      "local_stops": hb.get("local_stops", 0),
                      "mode": hb.get("mode"), "live_orders": hb.get("live_orders")}
            self._alert("error", f"engine heartbeat stale "
                                 f"({'missing' if age is None else f'{age:.0f}s'})", detail)
            self._request_flatten(detail)

        action = "alert"
        if (self.cfg.flatten_on_stale and self.flattener is not None
                and not self._flatten_done
                and self._attempts < self.cfg.max_flatten_attempts):
            self._attempts += 1
            action = "flatten"
            try:
                acks = self.flattener() or []
                self.stats["flattens"] += len(acks)
                self._flatten_done = True        # do not re-close what is already flat
                ok = sum(1 for a in acks if getattr(a, "ok", False))
                self._alert("error", f"watchdog flattened {ok}/{len(acks)} position(s)")
            except Exception as exc:
                self._alert("error", f"watchdog flatten failed: {type(exc).__name__}: {exc}")
                action = "flatten_failed"
        return {"stale": True, "age": age, "action": action}

    def run(self, max_checks: Optional[int] = None,
            sleep: Optional[Callable[[float], None]] = None) -> None:
        sleep = sleep or time.sleep
        checks = 0
        while max_checks is None or checks < max_checks:
            self.check()
            checks += 1
            if max_checks is None or checks < max_checks:
                sleep(self.cfg.check_interval_s)

    # ------------------------------------------------------------------ #
    def _alert(self, level: str, message: str, detail: Optional[dict] = None) -> None:
        try:
            if self.notifier.alert("watchdog", message, level=level, detail=detail):
                self.stats["alerts"] += 1
        except Exception:
            pass

    def _request_flatten(self, detail: dict) -> None:
        if self.store is None:
            return
        try:
            self.store.save_state(FLATTEN_REQUEST_KEY,
                                  {"ts": self.clock(), "reason": "heartbeat stale",
                                   "detail": detail})
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# engine-side handling of a flatten request
# --------------------------------------------------------------------------- #
def flatten_requested(store, now: Optional[float] = None,
                      ttl_s: float = REQUEST_TTL_S) -> Optional[dict]:
    """Return the pending request if it is recent enough to act on."""
    if store is None:
        return None
    try:
        req = store.load_state(FLATTEN_REQUEST_KEY)
    except Exception:
        return None
    if not req:
        return None
    ts = float(req.get("ts") or 0.0)
    if (now if now is not None else time.time()) - ts > ttl_s:
        return None
    return req


def clear_flatten_request(store) -> None:
    if store is None:
        return
    try:
        store.save_state(FLATTEN_REQUEST_KEY, {"ts": 0, "reason": "cleared"})
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# direct broker flattening (used when --live credentials are supplied to the watchdog)
# --------------------------------------------------------------------------- #
def flatten_port(port, reason: str = "watchdog") -> list:
    """Close everything a broker port reports. Never opens anything."""
    acks = []
    for pos in list(port.positions()):
        try:
            acks.append(port.close(pos, reason))
        except Exception:
            continue
    return acks


def broker_flattener(live_cfg) -> Optional[Callable[[], list]]:
    """Build a flatten callable for the configured broker, or None in paper mode."""
    if not getattr(live_cfg, "live_orders", False):
        return None
    from .data.deriv import DerivConfig
    from .data.mt5 import MT5Config
    from .execution.base import SymbolRegistry
    from .execution.deriv import DerivBroker
    from .execution.mt5 import MT5Broker

    registry = SymbolRegistry()
    if live_cfg.mode == "deriv":
        broker = DerivBroker(account="WATCHDOG", cfg=DerivConfig(
            app_id=live_cfg.deriv_app_id, token=live_cfg.deriv_token),
            registry=registry, dry_run=False)
    elif live_cfg.mode == "mt5":
        broker = MT5Broker(account="WATCHDOG", cfg=MT5Config(
            login=live_cfg.mt5_login, password=live_cfg.mt5_password,
            server=live_cfg.mt5_server, path=live_cfg.mt5_path),
            registry=registry, dry_run=False)
    else:
        return None
    try:
        broker.connect()
    except Exception:
        return None
    return lambda: flatten_port(broker, "watchdog")
