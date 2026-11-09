"""Configuration loading.

Precedence: CLI flags > environment variables > config file > defaults.
Secrets are read from the environment only; they are never written into config
files or logs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .risk import RiskConfig


DEFAULT_CONFIG: dict[str, Any] = {
    "mode": "paper",
    "symbols_by_broker": {"deriv": ["R_25", "R_50", "R_75"]},
    "tf": "M5",
    "strategy": "scalp_balanced",
    "prop_profile": "abutron_inhouse",
    "initial_balance": 10_000.0,
    "warmup_bars": 80,
    "seed": 11,
    "trend_strength": 1.2,
    "live_orders": False,
    "store_path": "runs/live/apex.db",
    "memory_path": "runs/live/reflection.json",
    "news_calendar": "",
    "boot_policy": "adopt",
    "persist_state": True,
    "record_ticks": "",
    "dashboard": {"token_env": "APEX_DASHBOARD_TOKEN", "host": "127.0.0.1", "port": 8000},
    "risk": {},
    "deriv": {"app_id": "1089", "token_env": "DERIV_TOKEN"},
    "mt5": {"login_env": "MT5_LOGIN", "password_env": "MT5_PASSWORD",
            "server_env": "MT5_SERVER", "path_env": "MT5_PATH"},
    "ai": {"enabled": False, "mode": "advisory", "url": "http://127.0.0.1:11434",
           "model": "qwen2.5:1.5b", "timeout_s": 6},
}


def load_config(path: Optional[str] = None) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))       # deep copy
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        cfg = _deep_merge(cfg, user)
    elif path:
        raise FileNotFoundError(f"config not found: {path}")
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def risk_from(cfg: dict) -> RiskConfig:
    data = {k: v for k, v in (cfg.get("risk") or {}).items()
            if k in RiskConfig.__dataclass_fields__}
    sessions = data.get("session_hours")
    if isinstance(sessions, dict):
        clean = {}
        for key, value in sessions.items():
            try:
                lo, hi = int(value[0]), int(value[1])
            except (TypeError, ValueError, IndexError):
                continue
            clean[str(key)] = (max(0, lo), min(24, hi))
        data["session_hours"] = clean
    return RiskConfig(**data)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def broker_credentials(cfg: dict) -> dict[str, Any]:
    d = cfg.get("deriv") or {}
    m = cfg.get("mt5") or {}
    return {
        "deriv_app_id": str(d.get("app_id", "1089")),
        "deriv_token": env(d.get("token_env", "DERIV_TOKEN")),
        "mt5_login": int(env(m.get("login_env", "MT5_LOGIN"), "0") or 0),
        "mt5_password": env(m.get("password_env", "MT5_PASSWORD")),
        "mt5_server": env(m.get("server_env", "MT5_SERVER")),
        "mt5_path": env(m.get("path_env", "MT5_PATH")),
    }
