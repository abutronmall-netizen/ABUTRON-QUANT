"""SQLite journal: the performance attribution layer.

Audit P1: "No reliable strategy-to-broker P/L attribution". Every signal, order,
fill, trade and equity tick is written here with strategy, broker, account,
instrument and regime, so P/L can be sliced all the way back to the agent votes
that produced it.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from typing import Any, Iterable, Optional

from .models import Ack, Candidate, Order, Position, TradeRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, symbol TEXT, side TEXT, conviction REAL, price REAL, sl REAL, tp REAL,
  strategy TEXT, regime TEXT, status TEXT, reason TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);

CREATE TABLE IF NOT EXISTS orders (
  client_id TEXT PRIMARY KEY, ts REAL, broker TEXT, account TEXT, symbol TEXT, side TEXT,
  lots REAL, strategy TEXT, ok INTEGER, filled_price REAL, reason TEXT, order_json TEXT, ack_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_ts ON orders(ts);

CREATE TABLE IF NOT EXISTS trades (
  trade_id TEXT PRIMARY KEY, symbol TEXT, side TEXT, lots REAL, entry REAL, exit REAL,
  opened_ts REAL, closed_ts REAL, pnl REAL, commission REAL, strategy TEXT, broker TEXT,
  account TEXT, exit_reason TEXT, conviction REAL, regime TEXT, r_multiple REAL,
  mfe REAL, mae REAL, meta TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_closed ON trades(closed_ts);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);

CREATE TABLE IF NOT EXISTS equity (
  ts REAL, broker TEXT, account TEXT, equity REAL, balance REAL
);
CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity(ts);

CREATE TABLE IF NOT EXISTS runtime_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    ts    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, message TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


class Store:
    def __init__(self, path: str = "runs/apex.db"):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()

    # ------------------------------------------------------------------ #
    def record_signal(self, cand: Optional[Candidate], status: str, reason: str,
                      detail: Optional[dict] = None, ts: Optional[float] = None,
                      symbol: str = "", side: str = "", conviction: float = 0.0) -> None:
        row = (
            ts if ts is not None else time.time(),
            cand.symbol if cand else symbol,
            cand.side.value if cand else side,
            cand.conviction if cand else conviction,
            cand.price if cand else None, cand.sl if cand else None, cand.tp if cand else None,
            cand.strategy if cand else "", cand.regime if cand else "",
            status, reason, json.dumps(detail or {}, default=str),
        )
        with self._lock:
            self.conn.execute("INSERT INTO signals (ts,symbol,side,conviction,price,sl,tp,"
                              "strategy,regime,status,reason,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
            self.conn.commit()

    def record_order(self, order: Order, ack: Ack) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO orders (client_id,ts,broker,account,symbol,side,lots,"
                "strategy,ok,filled_price,reason,order_json,ack_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (order.client_id, ack.ts, order.broker, order.account, order.symbol,
                 order.side.value, order.lots, order.strategy, 1 if ack.ok else 0,
                 ack.filled_price, ack.reason,
                 json.dumps({"kind": order.kind.value, "sl": order.sl, "tp": order.tp,
                             "tag": order.tag, "meta": order.meta}, default=str),
                 json.dumps({"order_id": ack.order_id, "retryable": ack.retryable}, default=str)))
            self.conn.commit()

    def record_trade(self, t: TradeRecord) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO trades (trade_id,symbol,side,lots,entry,exit,opened_ts,"
                "closed_ts,pnl,commission,strategy,broker,account,exit_reason,conviction,regime,"
                "r_multiple,mfe,mae,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t.trade_id, t.symbol, t.side.value, t.lots, t.entry, t.exit, t.opened_ts,
                 t.closed_ts, t.pnl, t.commission, t.strategy, t.broker, t.account,
                 t.exit_reason.value, t.conviction, t.regime, t.r_multiple, t.mfe, t.mae,
                 json.dumps(t.meta, default=str)))
            self.conn.commit()

    def save_state(self, key: str, payload: dict, ts: Optional[float] = None) -> None:
        """Durable runtime state: a restart must not refund the daily budget."""
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO runtime_state(key, value, ts) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
                (key, json.dumps(payload, default=str), ts if ts is not None else time.time()))

    def load_state(self, key: str) -> Optional[dict]:
        row = self.conn.execute("SELECT value FROM runtime_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def record_equity(self, ts: float, broker: str, account: str, equity: float, balance: float) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO equity (ts,broker,account,equity,balance) VALUES (?,?,?,?,?)",
                              (ts, broker, account, equity, balance))
            self.conn.commit()

    def record_event(self, kind: str, message: str, ts: Optional[float] = None) -> None:
        with self._lock:
            self.conn.execute("INSERT INTO events (ts,kind,message) VALUES (?,?,?)",
                              (ts if ts is not None else time.time(), kind, message))
            self.conn.commit()

    # ------------------------------------------------------------------ #
    def trades(self, since: float = 0.0, limit: int = 100000) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM trades WHERE closed_ts >= ? ORDER BY closed_ts LIMIT ?",
                (since, limit)).fetchall()
        return [dict(r) for r in rows]

    def recent_signals(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["detail"] = json.loads(d.get("detail") or "{}")
            except ValueError:
                d["detail"] = {}
            out.append(d)
        return out

    def recent_events(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def equity_curve(self, limit: int = 5000) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts,broker,account,equity,balance FROM equity ORDER BY ts DESC LIMIT ?",
                (limit,)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def signal_stats(self, since: float = 0.0) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) c FROM signals WHERE ts >= ? GROUP BY status", (since,)).fetchall()
        return {r["status"]: r["c"] for r in rows}

    # ------------------------------------------------------------------ #
    def metrics(self, since: float = 0.0) -> dict[str, Any]:
        ts = self.trades(since=since)
        return compute_metrics(ts, self.equity_curve())


# --------------------------------------------------------------------------- #
def compute_metrics(trades: list[dict], equity: Optional[list[dict]] = None,
                    initial_equity: float = 10_000.0) -> dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "pnl": 0.0,
                "expectancy": 0.0, "profit_factor": 0.0, "avg_r": 0.0, "max_drawdown_pct": 0.0,
                "sharpe": 0.0, "sortino": 0.0, "by_strategy": {}, "by_symbol": {}, "by_broker": {},
                "by_exit": {}, "by_regime": {}, "avg_win": 0.0, "avg_loss": 0.0,
                "best": 0.0, "worst": 0.0, "equity_curve": equity or []}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    rs = [t.get("r_multiple") or 0.0 for t in trades]

    # daily returns from the equity curve when available, else from trades
    returns: list[float] = []
    if equity and len(equity) > 2:
        by_day: dict[int, float] = {}
        for row in equity:
            by_day[int(row["ts"] // 86400)] = row["equity"]
        days = sorted(by_day)
        for a, b in zip(days, days[1:]):
            prev = by_day[a]
            if prev:
                returns.append((by_day[b] - prev) / prev)
    else:
        by_day = {}
        for t in trades:
            by_day[int(t["closed_ts"] // 86400)] = by_day.get(int(t["closed_ts"] // 86400), 0.0) + t["pnl"]
        base = 10_000.0
        returns = [v / base for _, v in sorted(by_day.items())]

    def _stats(xs: list[float]) -> tuple[float, float]:
        if len(xs) < 2:
            return 0.0, 0.0
        m = sum(xs) / len(xs)
        var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
        sd = math.sqrt(var)
        if sd == 0:
            return 0.0, 0.0
        downside = [min(0.0, x - 0) for x in xs]
        dsd = math.sqrt(sum(d * d for d in downside) / len(xs)) or 1e-12
        return (m / sd) * math.sqrt(252), (m / dsd) * math.sqrt(252)

    sharpe, sortino = _stats(returns)

    # max drawdown on an equity path that starts at the account's initial balance
    peak = initial_equity
    eq = initial_equity
    max_dd = 0.0
    curve = [0.0]
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)
        curve.append(round(eq - initial_equity, 2))

    def _group(key: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for t in trades:
            k = str(t.get(key) or "n/a")
            g = out.setdefault(k, {"trades": 0, "pnl": 0.0, "wins": 0, "r_sum": 0.0})
            g["trades"] += 1
            g["pnl"] = round(g["pnl"] + t["pnl"], 2)
            g["wins"] += 1 if t["pnl"] > 0 else 0
            g["r_sum"] = round(g["r_sum"] + (t.get("r_multiple") or 0.0), 3)
        for g in out.values():
            g["win_rate"] = round(g["wins"] / g["trades"] * 100, 1) if g["trades"] else 0.0
            g["avg_r"] = round(g["r_sum"] / g["trades"], 3) if g["trades"] else 0.0
        return out

    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 2),
        "pnl": round(sum(pnls), 2),
        "expectancy": round(sum(pnls) / n, 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "avg_r": round(sum(rs) / n, 3),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "best": round(max(pnls), 2), "worst": round(min(pnls), 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(sharpe, 2), "sortino": round(sortino, 2),
        "by_strategy": _group("strategy"), "by_symbol": _group("symbol"),
        "by_broker": _group("broker"), "by_exit": _group("exit_reason"),
        "by_regime": _group("regime"),
        "equity_curve": curve[-2000:],
        "days": len({int(t["closed_ts"] // 86400) for t in trades}),
    }
