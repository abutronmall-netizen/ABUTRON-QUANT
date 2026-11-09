"""Market data feeds and bar aggregation.

`SyntheticFeed` is a regime-switching simulator with intraday volatility seasonality
and jump risk. It exists so the whole platform - engine, risk, prop guard, router,
dashboard - can be exercised end to end without credentials, and so unit tests are
deterministic. It is clearly not a profitability model of any real instrument.

`ReplayFeed` replays recorded bars through the identical event interface as live
data, which is what keeps backtest and production on one code path.
"""
from __future__ import annotations

import math
import random
import time
import zlib
from dataclasses import dataclass
from typing import Iterator, Optional

from ..models import Bar, Quote


# --------------------------------------------------------------------------- #
class BarAggregator:
    """Aggregate base-timeframe bars into higher timeframes (M5 -> M15 -> H1)."""

    def __init__(self, seconds: int):
        self.seconds = seconds
        self.current: Optional[Bar] = None
        self.last_closed: Optional[Bar] = None

    def update(self, bar: Bar) -> Optional[Bar]:
        bucket = int(bar.ts // self.seconds) * self.seconds
        if self.current is None or self.current.ts != bucket:
            closed = self.current
            self.current = Bar(bar.symbol, bucket, bar.open, bar.high, bar.low, bar.close, bar.volume)
            self.last_closed = closed
            return closed
        c = self.current
        c.high = max(c.high, bar.high)
        c.low = min(c.low, bar.low)
        c.close = bar.close
        c.volume += bar.volume
        return None


# --------------------------------------------------------------------------- #
@dataclass
class SyntheticConfig:
    base_price: float = 10_000.0
    tf_seconds: int = 300
    vol_bps: float = 8.0            # base per-bar volatility in bps
    tick_vol_bps: float = 2.0
    trend_strength: float = 0.35
    jump_probability: float = 0.002
    jump_bps: float = 120.0
    session_volatility: bool = True
    seed: int = 42


class SyntheticFeed:
    """Regime-switching price generator producing consistent OHLC bars."""

    def __init__(self, symbol: str, cfg: Optional[SyntheticConfig] = None,
                 start_ts: Optional[float] = None):
        self.symbol = symbol
        self.cfg = cfg or SyntheticConfig()
        self.rng = random.Random((self.cfg.seed * 7919 + zlib.crc32(symbol.encode())) & 0x7FFFFFFF)
        self.ts = start_ts if start_ts is not None else (int(time.time()) // 300) * 300 - 90 * 86400
        self.price = self.cfg.base_price * (1 + self.rng.uniform(-0.02, 0.02))
        self.regime = "range"
        self.regime_left = 0
        self.drift = 0.0
        self.vol_mult = 1.0

    # ------------------------------------------------------------------ #
    def _switch_regime(self) -> None:
        r = self.rng.random()
        if r < 0.30:
            self.regime, self.drift, self.vol_mult = "trend", self.cfg.trend_strength, 1.0
        elif r < 0.60:
            self.regime, self.drift, self.vol_mult = "range", 0.0, 0.85
        elif r < 0.80:
            self.regime, self.drift, self.vol_mult = "squeeze", 0.0, 0.45
        elif r < 0.95:
            self.regime, self.drift, self.vol_mult = "expansion", self.cfg.trend_strength * 1.4, 1.7
        else:
            self.regime, self.drift, self.vol_mult = "chaos", 0.0, 2.6
        if self.regime == "trend":
            self.drift *= self.rng.choice((-1, 1))
        self.regime_left = self.rng.randint(30, 160)

    def _session_mult(self, ts: float) -> float:
        if not self.cfg.session_volatility:
            return 1.0
        hour = (ts % 86400) / 3600.0
        # London + New York overlap peaks; Asia quieter
        return 0.55 + 0.75 * math.exp(-((hour - 14.0) ** 2) / 26.0) + 0.25 * math.exp(-((hour - 8.0) ** 2) / 18.0)

    # ------------------------------------------------------------------ #
    def next_bar(self) -> Bar:
        cfg = self.cfg
        if self.regime_left <= 0:
            self._switch_regime()
        self.regime_left -= 1

        ts = self.ts
        o = self.price
        sess = self._session_mult(ts)
        sigma = cfg.vol_bps / 10_000.0 * self.vol_mult * sess
        drift = self.drift / 10_000.0 * sess * (1 if self.regime == "trend" else 0.4)

        n = 6
        hi = lo = o
        p = o
        for _ in range(n):
            shock = self.rng.gauss(0, 1)
            p *= math.exp(drift / n + sigma / math.sqrt(n) * shock)
            hi = max(hi, p)
            lo = min(lo, p)
        if self.rng.random() < cfg.jump_probability:
            jump = (1 if self.rng.random() < 0.5 else -1) * cfg.jump_bps / 10_000.0
            p *= (1 + jump)
            hi = max(hi, p)
            lo = min(lo, p)

        bar = Bar(self.symbol, float(ts), o, max(hi, o, p), min(lo, o, p), p,
                  volume=abs(p - o) / max(o, 1e-9) * 1e6 + self.rng.random() * 500)

        self.price = p
        self.ts = ts + cfg.tf_seconds
        return bar

    def bars(self, n: int) -> list[Bar]:
        return [self.next_bar() for _ in range(n)]

    def quote_from(self, bar: Bar, spread_bps: float = 0.35) -> Quote:
        mid = bar.close
        half = mid * spread_bps / 10_000.0 / 2
        return Quote(self.symbol, bar.ts + self.cfg.tf_seconds, mid - half, mid + half)


class ReplayFeed:
    """Deterministic replay of stored bars, merged across symbols by timestamp."""

    def __init__(self, series: dict[str, list[Bar]]):
        self.series = {k: sorted(v, key=lambda b: b.ts) for k, v in series.items()}
        self._idx = {k: 0 for k in series}

    def symbols(self) -> list[str]:
        return list(self.series.keys())

    def iter_bars(self) -> Iterator[Bar]:
        while True:
            best: Optional[Bar] = None
            best_sym = ""
            for sym, bars in self.series.items():
                i = self._idx[sym]
                if i < len(bars) and (best is None or bars[i].ts < best.ts):
                    best, best_sym = bars[i], sym
            if best is None:
                return
            self._idx[best_sym] += 1
            yield best

    def __len__(self) -> int:
        return sum(len(v) for v in self.series.values())


def synth_series(symbol: str, n_bars: int = 3000, tf_seconds: int = 300,
                 base_price: float = 10_000.0, seed: int = 42,
                 vol_bps: float = 8.0) -> list[Bar]:
    cfg = SyntheticConfig(base_price=base_price, tf_seconds=tf_seconds, vol_bps=vol_bps,
                          seed=seed)
    return SyntheticFeed(symbol, cfg).bars(n_bars)


class LiveBarBuilder:
    """Build bars from a tick stream (Deriv) or polled quotes (MT5).

    Emits a finished Bar on bucket rollover and the in-progress bar on demand so
    the engine can decide on closed bars only - the same contract the backtester
    honours.
    """

    def __init__(self, symbol: str, seconds: int = 300):
        self.symbol = symbol
        self.seconds = seconds
        self.current: Optional[Bar] = None
        self.last_price: float = 0.0

    def on_tick(self, ts: float, price: float, volume: float = 1.0) -> Optional[Bar]:
        bucket = int(ts // self.seconds) * self.seconds
        self.last_price = price
        if self.current is None:
            self.current = Bar(self.symbol, float(bucket), price, price, price, price, volume)
            return None
        if bucket > self.current.ts:
            closed = self.current
            self.current = Bar(self.symbol, float(bucket), price, price, price, price, volume)
            return closed
        c = self.current
        c.high = max(c.high, price)
        c.low = min(c.low, price)
        c.close = price
        c.volume += volume
        return None

    def force_close(self) -> Optional[Bar]:
        b = self.current
        self.current = None
        return b


# --------------------------------------------------------------------------- #
# tick capture and replay
# --------------------------------------------------------------------------- #
class TickRecorder:
    """Append live quotes to a CSV so research can replay real spreads.

    Format: ``ts,symbol,bid,ask``. One file, all symbols; the replay feed groups
    by symbol, so a single recording session is directly usable by the backtester.
    """

    def __init__(self, path: str):
        self.path = path
        self.written = 0
        import os
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        # Check the header by content, not by file size: a previous process may
        # still be buffering, and appending a second header corrupts the feed.
        has_header = False
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    has_header = fh.readline().strip().startswith("ts,")
            except OSError:
                has_header = False
        self._new = not has_header
        self._fh = open(path, "a", encoding="utf-8")
        if self._new:
            self._fh.write("ts,symbol,bid,ask\n")
            self._fh.flush()

    def write(self, quote: "Quote") -> None:
        self._fh.write(f"{quote.ts:.3f},{quote.symbol},{quote.bid:.8f},{quote.ask:.8f}\n")
        self.written += 1
        if self.written % 500 == 0:
            self._fh.flush()

    def close(self) -> None:
        try:
            self._fh.flush()
            self._fh.close()
        except Exception:
            pass

    def __enter__(self) -> "TickRecorder":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def ticks_from_csv(path: str, symbols: Optional[list[str]] = None) -> list[tuple[float, str, float, float]]:
    """Read a recorded tick file into ``(ts, symbol, bid, ask)`` rows."""
    import csv
    rows: list[tuple[float, str, float, float]] = []
    keep = set(symbols) if symbols else None
    with open(path, "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                sym = row["symbol"]
                if keep is not None and sym not in keep:
                    continue
                rows.append((float(row["ts"]), sym, float(row["bid"]), float(row["ask"])))
            except (KeyError, TypeError, ValueError):
                continue
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


class TickReplayFeed:
    """Replay recorded ticks through the same code path as a live feed."""

    def __init__(self, rows: list[tuple[float, str, float, float]], tf_seconds: int = 300):
        self.rows = rows
        self.tf_seconds = tf_seconds
        self._builders: dict[str, "LiveBarBuilder"] = {}

    @property
    def symbols(self) -> list[str]:
        return sorted({r[1] for r in self.rows})

    def builder(self, symbol: str) -> "LiveBarBuilder":
        if symbol not in self._builders:
            self._builders[symbol] = LiveBarBuilder(symbol, self.tf_seconds)
        return self._builders[symbol]

    def __iter__(self):
        for ts, sym, bid, ask in self.rows:
            q = Quote(sym, ts, bid, ask)
            yield q, self.builder(sym).on_tick(ts, q.mid)

    def __len__(self) -> int:
        return len(self.rows)
