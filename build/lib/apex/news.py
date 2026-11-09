"""Economic calendar: turn scheduled events into prop-firm blackout windows.

The prop rules implement `news_blackout_minutes`, but a rule with no data source
is decoration. This module supplies the data and nothing else: it does not trade
differently around news, it only tells the guard when a position must not be
opened.

Accepted file formats (JSON):

    [{"ts": "2026-11-10T13:30:00Z", "currency": "USD", "impact": "high",
      "title": "CPI m/m"}]

    {"events": [ ... same ... ]}

`ts` may be an ISO-8601 string or an epoch number. Impact is free text
(`high`, `medium`, `low`); Windows default is `high` only, because blocking every
medium print leaves a scalper with no session at all.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional


def parse_ts(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            if fmt is None:
                dt = datetime.fromisoformat(text)
            else:
                dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(f"unparseable timestamp: {value!r}")


@dataclass
class NewsEvent:
    ts: float
    currency: str = "USD"
    impact: str = "high"
    title: str = ""

    def as_dict(self) -> dict:
        return {"ts": self.ts, "utc": datetime.fromtimestamp(self.ts, timezone.utc).isoformat(),
                "currency": self.currency, "impact": self.impact, "title": self.title}


@dataclass
class NewsCalendar:
    events: list[NewsEvent] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str) -> "NewsCalendar":
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        rows = raw.get("events", raw) if isinstance(raw, dict) else raw
        events = []
        for row in rows or []:
            try:
                events.append(NewsEvent(ts=parse_ts(row["ts"]),
                                        currency=str(row.get("currency", "USD")).upper(),
                                        impact=str(row.get("impact", "high")).lower(),
                                        title=str(row.get("title", ""))))
            except (KeyError, ValueError):
                continue
        events.sort(key=lambda e: e.ts)
        return cls(events)

    @classmethod
    def from_rows(cls, rows: Iterable[dict]) -> "NewsCalendar":
        return cls([NewsEvent(parse_ts(r["ts"]), str(r.get("currency", "USD")).upper(),
                              str(r.get("impact", "high")).lower(), str(r.get("title", "")))
                    for r in rows])

    # ------------------------------------------------------------------ #
    def windows(self, start: float, end: float, minutes: int = 2,
                impacts: tuple[str, ...] = ("high",),
                currencies: Optional[Iterable[str]] = None) -> list[tuple[float, float]]:
        """Blackout windows covering [start, end]. A window is a hard block."""
        pad = max(1, int(minutes)) * 60.0
        cur = {c.upper() for c in currencies} if currencies else None
        out: list[tuple[float, float]] = []
        for ev in self.events:
            if ev.impact not in impacts:
                continue
            if cur is not None and ev.currency not in cur:
                continue
            lo, hi = ev.ts - pad, ev.ts + pad
            if hi < start or lo > end:
                continue
            out.append((lo, hi))
        return sorted(out)

    def next_event(self, now: float, impacts: tuple[str, ...] = ("high",)) -> Optional[NewsEvent]:
        for ev in self.events:
            if ev.ts >= now and ev.impact in impacts:
                return ev
        return None

    def in_blackout(self, now: float, minutes: int = 2) -> bool:
        pad = max(1, int(minutes)) * 60.0
        return any(abs(ev.ts - now) <= pad for ev in self.events if ev.impact == "high")

    def window_at(self, now: float, minutes: int = 2) -> tuple[float, float]:
        """The currently binding window, for pacing decisions."""
        pad = max(1, int(minutes)) * 60.0
        for ev in self.events:
            if ev.impact == "high" and abs(ev.ts - now) <= pad:
                return (ev.ts - pad, ev.ts + pad)
        return (now, now)

    def __len__(self) -> int:
        return len(self.events)


def demo_calendar(now: Optional[float] = None, hours: int = 24) -> NewsCalendar:
    """A tiny synthetic calendar, for tests and dry runs only."""
    now = now if now is not None else time.time()
    base = int(now // 3600 + 1) * 3600
    return NewsCalendar([
        NewsEvent(ts=base + 3600 * i, currency="USD", impact="high", title=f"synthetic {i}")
        for i in range(max(1, hours // 4))
    ])
