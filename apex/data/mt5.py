"""MetaTrader 5 adapter (FP Markets).

`MetaTrader5` is a Windows-only native package that talks to a running terminal.
This module isolates every platform quirk behind a small typed surface so the
rest of the engine never imports it, and so the whole system still runs on a
Linux VPS with a paper broker or the Deriv feed.

Deployment reality (documented again in docs/LIVE_DEPLOYMENT.md):
  * the terminal must be running and logged in to the FP Markets account;
  * `initialize(login=..., password=..., server=...)` is only needed if the
    terminal is not already logged in;
  * `Algo Trading` must be enabled on the terminal, and the account must allow
    automated trading;
  * hedging vs netting changes how many positions a symbol can hold.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from ..models import Bar, Quote

TIMEFRAMES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}


def mt5_installed() -> bool:
    """Is the Windows-only `MetaTrader5` package importable here?

    Checked without importing it: the package does native initialisation on
    import on some builds, and a CLI that only wants to refuse clearly should
    not start a terminal session to find out.
    """
    import importlib.util
    try:
        return importlib.util.find_spec("MetaTrader5") is not None
    except (ImportError, ValueError):
        return False


class MT5Error(RuntimeError):
    pass


class MT5Unavailable(MT5Error):
    pass


@dataclass
class MT5Config:
    login: int = 0
    password: str = ""
    server: str = ""
    path: str = ""              # optional terminal64.exe path
    portable: bool = False
    magic: int = 20261109
    deviation: int = 20         # max slippage in points
    filling_type: str = "IOC"   # IOC | FOK | RETURN
    server_utc_offset_hours: Optional[float] = None   # None -> auto-detect on connect


class MT5Connection:
    """Thin, defensive wrapper around the MetaTrader5 module."""

    def __init__(self, cfg: Optional[MT5Config] = None, module: Any = None):
        self.cfg = cfg or MT5Config()
        self._mt5 = module
        self.connected = False
        self.last_error = ""
        # MT5 timestamps are in BROKER SERVER time (usually UTC+2/+3), not UTC.
        # Everything above this layer is UTC, so the offset is removed here.
        # Sign convention: offset_s = server_time - utc_time (positive = ahead),
        # matching the `server_utc_offset_hours` config field.
        self.offset_s: float = float((self.cfg.server_utc_offset_hours or 0.0) * 3600)

    # ------------------------------------------------------------------ #
    def detect_offset(self, symbol: Optional[str] = None) -> float:
        """Infer the broker's UTC offset from a live tick.

        A tick stamped an hour in the future is not a clock bug in the engine: it
        is server time. We snap to 15-minute granularity and refuse to guess from
        stale ticks (weekend, market closed) or absurd deltas.
        """
        if self.cfg.server_utc_offset_hours is not None:
            self.offset_s = float(self.cfg.server_utc_offset_hours * 3600)
            return self.offset_s
        try:
            symbols = [symbol] if symbol else [s.name for s in (self.mt5.symbols_get() or [])][:20]
            for sym in symbols:
                t = self.mt5.symbol_info_tick(sym)
                if t is None or not getattr(t, "time", 0):
                    continue
                delta = float(t.time) - time.time()
                if abs(delta) > 18 * 3600:      # stale tick or wrong symbol: not a timezone
                    continue
                snapped = round(delta / 900.0) * 900.0
                if abs(snapped) <= 15 * 3600:
                    self.offset_s = snapped
                    return self.offset_s
        except Exception:
            pass
        return self.offset_s

    def to_utc(self, ts: float) -> float:
        """Server timestamp -> UTC epoch."""
        return float(ts) - self.offset_s

    def to_server(self, ts: float) -> float:
        """UTC epoch -> server timestamp."""
        return float(ts) + self.offset_s

    @property
    def offset_hours(self) -> float:
        return round(self.offset_s / 3600.0, 2)

    # ------------------------------------------------------------------ #
    @property
    def mt5(self) -> Any:
        if self._mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore
            except ImportError as exc:      # pragma: no cover - platform specific
                raise MT5Unavailable(
                    "MetaTrader5 package not installed. On Windows: "
                    "`pip install MetaTrader5`. On Linux/containers, run the MT5 "
                    "bridge on a Windows host and point the engine at it."
                ) from exc
            self._mt5 = mt5
        return self._mt5

    def connect(self) -> bool:
        mt5 = self.mt5
        kwargs: dict[str, Any] = {}
        if self.cfg.login:
            kwargs.update(login=self.cfg.login, password=self.cfg.password,
                          server=self.cfg.server)
        if self.cfg.path:
            kwargs["path"] = self.cfg.path
        if self.cfg.portable:
            kwargs["portable"] = True
        if not mt5.initialize(**kwargs):
            self.last_error = str(mt5.last_error())
            raise MT5Error(f"initialize failed: {self.last_error}")
        info = mt5.account_info()
        if info is None:
            raise MT5Error(f"account_info unavailable: {mt5.last_error()}")
        self.connected = True
        self.detect_offset()
        return True

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        finally:
            self.connected = False

    # ------------------------------------------------------------------ #
    def account_info(self) -> Any:
        info = self.mt5.account_info()
        if info is None:
            raise MT5Error(f"account_info failed: {self.mt5.last_error()}")
        return info

    def ensure_symbol(self, symbol: str) -> Any:
        mt5 = self.mt5
        info = mt5.symbol_info(symbol)
        if info is None:
            # brokers suffix instruments (.m, .a, #, etc.) - try to resolve
            all_syms = mt5.symbols_get() or []
            for s in all_syms:
                if s.name.split(".")[0].split("#")[0] == symbol.split(".")[0]:
                    symbol = s.name
                    info = s
                    break
        if info is None:
            raise MT5Error(f"symbol '{symbol}' not found on this server")
        if not info.visible:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        return info

    def rates(self, symbol: str, timeframe: str = "M5", count: int = 500) -> list[Bar]:
        mt5 = self.mt5
        self.ensure_symbol(symbol)
        tf = getattr(mt5, f"TIMEFRAME_{timeframe}")
        arr = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if arr is None:
            raise MT5Error(f"copy_rates failed for {symbol}: {mt5.last_error()}")
        out: list[Bar] = []
        for r in arr:
            out.append(Bar(symbol=symbol, ts=self.to_utc(float(r["time"])), open=float(r["open"]),
                           high=float(r["high"]), low=float(r["low"]),
                           close=float(r["close"]), volume=float(r["tick_volume"])))
        return out

    def tick(self, symbol: str) -> Optional[Quote]:
        mt5 = self.mt5
        self.ensure_symbol(symbol)
        t = mt5.symbol_info_tick(symbol)
        if t is None:
            return None
        return Quote(symbol=symbol, ts=self.to_utc(float(t.time)),
                     bid=float(t.bid), ask=float(t.ask))


class MT5Feed:
    """Polling feed: MT5 is pull-based, so we poll ticks and aggregate bars."""

    def __init__(self, conn: MT5Connection, symbols: list[str], tf: str = "M5",
                 poll_interval: float = 0.5):
        self.conn = conn
        self.symbols = symbols
        self.tf = tf
        self.poll_interval = poll_interval
        self.on_quote = None
        self.on_bar = None
        self._last_bar_ts: dict[str, float] = {}

    def poll_once(self) -> list[Quote]:
        quotes = []
        for s in self.symbols:
            q = self.conn.tick(s)
            if q is None:
                continue
            quotes.append(q)
            if self.on_quote:
                self.on_quote(q)
        return quotes
