"""Prop-firm account passing system.

A prop challenge is not a trading problem, it is a *constraint satisfaction*
problem: hit a profit target before hitting a daily or total loss limit, while
respecting minimum trading days, a consistency rule, news windows and weekend
flat rules. The best signal in the world fails a challenge if it is sized for a
retail account.

This module is the single authority on those constraints. It wraps the risk
engine: it can reduce size, block an entry, force a flatten, or lock the day.

All thresholds are per-profile data, never hard-coded in strategies, so the same
engine can trade FTMO, FundedNext, The5ers or a bespoke in-house challenge.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

from .models import Candidate, Decision, Position, TradeRecord
from .risk import session_day


class DrawdownType(str, Enum):
    STATIC = "static"          # measured from initial balance
    TRAILING = "trailing"      # measured from equity high-water mark


class Verdict(str, Enum):
    ACTIVE = "active"          # challenge running, inside limits
    AT_RISK = "at_risk"        # close to a limit
    TARGET_HIT = "target_hit"  # profit target reached, waiting for min days
    PASSED = "passed"
    BREACHED = "breached"


@dataclass
class PropProfile:
    name: str
    # structure
    profit_target_pct: float = 10.0
    max_daily_loss_pct: float = 5.0
    max_total_loss_pct: float = 10.0
    min_trading_days: int = 4
    max_calendar_days: int = 30
    drawdown_type: DrawdownType = DrawdownType.STATIC
    # rules
    consistency_pct: float = 0.0        # 0 = no rule; else best day <= X% of total profit
    max_lots: float = 2.0
    max_open_positions: int = 3
    stop_loss_required: bool = True
    news_blackout_minutes: int = 0
    weekend_flat: bool = False
    daily_reset_hour_utc: int = 0
    max_trades_per_day: int = 8
    # risk shaping for the challenge
    risk_per_trade_pct: float = 0.5
    max_risk_per_trade_pct: float = 1.0
    risk_off_after_daily_loss_pct: float = 2.5
    profit_lock_after_target: bool = True
    phase: int = 1
    notes: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["drawdown_type"] = self.drawdown_type.value
        return d


# --------------------------------------------------------------------------- #
# Profiles. Values reflect publicly documented rule sets as of 2026; always
# re-verify against the current terms of the firm you are actually trading with.
# --------------------------------------------------------------------------- #
def ftmo_2step(balance: Optional[float] = None) -> PropProfile:
    return PropProfile(
        name="ftmo_2step", profit_target_pct=10.0, max_daily_loss_pct=5.0,
        max_total_loss_pct=10.0, min_trading_days=4, max_calendar_days=30,
        drawdown_type=DrawdownType.STATIC, consistency_pct=0.0, max_lots=2.0,
        max_open_positions=3, news_blackout_minutes=2, weekend_flat=True,
        risk_per_trade_pct=0.45, max_risk_per_trade_pct=0.9,
        notes="Phase 1 target 10%, then phase 2 at 5% with same limits.",
    )


def fundednext_starter(balance: Optional[float] = None) -> PropProfile:
    return PropProfile(
        name="fundednext_starter", profit_target_pct=8.0, max_daily_loss_pct=5.0,
        max_total_loss_pct=10.0, min_trading_days=5, max_calendar_days=30,
        drawdown_type=DrawdownType.STATIC, consistency_pct=40.0, max_lots=2.0,
        max_open_positions=3, risk_per_trade_pct=0.4, max_risk_per_trade_pct=0.8,
        notes="Consistency: best day <= 40% of total profit. Enforced by the guard.",
    )


def the5ers_high_stakes() -> PropProfile:
    return PropProfile(
        name="the5ers_high_stakes", profit_target_pct=6.0, max_daily_loss_pct=3.0,
        max_total_loss_pct=6.0, min_trading_days=3, max_calendar_days=60,
        drawdown_type=DrawdownType.STATIC, consistency_pct=0.0, max_lots=1.5,
        max_open_positions=2, risk_per_trade_pct=0.35, max_risk_per_trade_pct=0.7,
        notes="Tight daily limit: pausing after 2.5% of daily loss is mandatory.",
    )


def abutron_inhouse(balance: Optional[float] = None) -> PropProfile:
    """Conservative in-house evaluation used for the default demo/backtests."""
    return PropProfile(
        name="abutron_inhouse", profit_target_pct=8.0, max_daily_loss_pct=4.0,
        max_total_loss_pct=8.0, min_trading_days=5, max_calendar_days=30,
        drawdown_type=DrawdownType.STATIC, consistency_pct=35.0, max_lots=1.0,
        max_open_positions=2, max_trades_per_day=6, risk_per_trade_pct=0.40,
        max_risk_per_trade_pct=0.80, risk_off_after_daily_loss_pct=2.0,
        notes="Built for the ABUTRON evaluation pipeline; mirrors the strictest rules of the big two.",
    )


PROFILES = {
    "ftmo_2step": ftmo_2step,
    "fundednext_starter": fundednext_starter,
    "the5ers_high_stakes": the5ers_high_stakes,
    "abutron_inhouse": abutron_inhouse,
}


def get_profile(name_or_dict) -> PropProfile:
    """Accept a profile name, a dict of overrides, or a path to a JSON file."""
    if isinstance(name_or_dict, PropProfile):
        return name_or_dict
    if isinstance(name_or_dict, str) and name_or_dict.lower().endswith(".json"):
        import json
        import os
        if not os.path.exists(name_or_dict):
            raise FileNotFoundError(f"prop profile file not found: {name_or_dict}")
        with open(name_or_dict, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return get_profile({k: v for k, v in data.items() if not k.startswith("_")})
    if isinstance(name_or_dict, dict):
        d = dict(name_or_dict)
        if isinstance(d.get("drawdown_type"), str):
            d["drawdown_type"] = DrawdownType(d["drawdown_type"])
        d.setdefault("name", "custom")
        return PropProfile(**{k: v for k, v in d.items()
                              if k in PropProfile.__dataclass_fields__})
    fn = PROFILES.get(str(name_or_dict))
    if fn is None:
        raise KeyError(f"unknown prop profile '{name_or_dict}'")
    return fn()


# --------------------------------------------------------------------------- #
@dataclass
class PropState:
    start_balance: float
    phase: int = 1
    day_index: int = 0
    day_start_equity: float = 0.0
    day_start_balance: float = 0.0
    day_peak_profit: float = 0.0        # best intraday profit, for consistency
    days_traded: set = field(default_factory=set)
    day_pnls: dict = field(default_factory=dict)
    peak_equity: float = 0.0
    equity: float = 0.0
    balance: float = 0.0
    total_trades: int = 0
    breach: str = ""
    passed: bool = False
    started_ts: float = 0.0
    last_ts: float = 0.0
    flat_required: str = ""


class PropGuard:
    """Enforces a challenge rule set on top of the risk engine."""

    def __init__(self, profile: PropProfile, initial_balance: float = 10_000.0,
                 now: Optional[float] = None, block_news_windows: Optional[list[tuple[float, float]]] = None):
        now = now if now is not None else time.time()
        self.p = profile
        self.news_windows = list(block_news_windows or [])
        self.st = PropState(
            start_balance=initial_balance, phase=profile.phase,
            day_index=self._day_index(now), day_start_equity=initial_balance,
            day_start_balance=initial_balance, peak_equity=initial_balance,
            equity=initial_balance, balance=initial_balance,
            started_ts=now, last_ts=now,
        )

    # ------------------------------------------------------------------ #
    def _day_index(self, ts: float) -> int:
        return session_day(ts, self.p.daily_reset_hour_utc)

    def _roll_day(self, ts: float) -> None:
        idx = self._day_index(ts)
        st = self.st
        if idx != st.day_index:
            # bank the finished day's profit for the consistency rule
            if st.day_pnls.get(st.day_index) is None:
                st.day_pnls[st.day_index] = st.day_start_equity * 0 + (st.equity - st.day_start_equity)
            st.day_index = idx
            st.day_start_equity = st.equity
            st.day_start_balance = st.balance
            st.day_peak_profit = 0.0
            st.flat_required = ""

    def on_equity(self, ts: float, equity: float, balance: Optional[float] = None) -> None:
        self._roll_day(ts)
        st = self.st
        st.equity = equity
        if balance is not None:
            st.balance = balance
        else:
            st.balance = max(st.balance, equity)
        st.peak_equity = max(st.peak_equity, equity)
        st.last_ts = ts
        st.day_peak_profit = max(st.day_peak_profit, equity - st.day_start_equity)
        self._check_breach(ts)

    # ------------------------------------------------------------------ #
    def _floor_equity(self) -> float:
        """Equity level at which the challenge is failed."""
        p, st = self.p, self.st
        if p.drawdown_type is DrawdownType.TRAILING:
            return st.peak_equity * (1.0 - p.max_total_loss_pct / 100.0)
        return st.start_balance * (1.0 - p.max_total_loss_pct / 100.0)

    def _daily_floor(self) -> float:
        base = self.st.day_start_equity
        return base * (1.0 - self.p.max_daily_loss_pct / 100.0)

    def _check_breach(self, ts: float) -> None:
        st, p = self.st, self.p
        if st.breach or st.passed:
            return
        if st.equity <= self._floor_equity():
            st.breach = (f"max total loss breached: equity {st.equity:.2f} <= floor "
                         f"{self._floor_equity():.2f} ({p.drawdown_type.value})")
            return
        if st.equity <= self._daily_floor():
            st.breach = (f"max daily loss breached: equity {st.equity:.2f} <= "
                         f"{self._daily_floor():.2f} (day start {st.day_start_equity:.2f})")
            return
        if p.max_calendar_days and (ts - st.started_ts) > p.max_calendar_days * 86400:
            st.breach = f"time limit exceeded ({p.max_calendar_days} calendar days)"
            return
        # profit target
        profit_pct = (st.equity - st.start_balance) / st.start_balance * 100.0
        if profit_pct >= p.profit_target_pct:
            if len(st.days_traded) >= p.min_trading_days:
                if p.consistency_pct <= 0 or self._consistency_ok():
                    st.passed = True
            elif not st.flat_required:
                st.flat_required = "target_hit_min_days"

    # ------------------------------------------------------------------ #
    # state persistence
    # ------------------------------------------------------------------ #
    def export_state(self) -> dict:
        st = self.st
        return {"start_balance": st.start_balance, "phase": st.phase,
                "day_index": st.day_index, "day_start_equity": st.day_start_equity,
                "day_start_balance": st.day_start_balance,
                "day_peak_profit": st.day_peak_profit,
                "days_traded": sorted(st.days_traded),
                "day_pnls": {str(k): v for k, v in st.day_pnls.items()},
                "peak_equity": st.peak_equity, "equity": st.equity, "balance": st.balance,
                "total_trades": st.total_trades, "breach": st.breach, "passed": st.passed,
                "started_ts": st.started_ts, "last_ts": st.last_ts,
                "flat_required": st.flat_required}

    def import_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        st = self.st
        for key in ("start_balance", "phase", "day_index", "day_start_equity",
                    "day_start_balance", "day_peak_profit", "peak_equity", "equity",
                    "balance", "total_trades", "breach", "passed", "started_ts",
                    "last_ts", "flat_required"):
            if key in data:
                setattr(st, key, data[key])
        if "days_traded" in data:
            st.days_traded = set(data["days_traded"] or [])
        if "day_pnls" in data:
            st.day_pnls = {int(k): v for k, v in (data["day_pnls"] or {}).items()}

    def _consistency_ok(self) -> bool:
        st, p = self.st, self.p
        pnls = dict(st.day_pnls)
        pnls[st.day_index] = st.equity - st.day_start_equity
        total = sum(v for v in pnls.values() if v > 0)
        if total <= 0:
            return False
        best = max(pnls.values())
        return (best / total * 100.0) <= p.consistency_pct

    # ------------------------------------------------------------------ #
    def check(self, cand: Candidate, risk_decision: Decision, now: float,
              open_risk_money: float = 0.0,
              notional_per_lot: float = 0.0) -> Decision:
        """Final gate. Wraps the risk engine's decision (never loosens it)."""
        st, p = self.st, self.p
        if st.breach:
            return Decision.block("PROP_BREACHED", st.breach)
        if st.passed:
            return Decision.block("PROP_PASSED", "challenge complete - trading locked")
        if st.flat_required:
            return Decision.block("PROP_FLAT", f"flatten required: {st.flat_required}")
        if p.stop_loss_required and (cand.sl is None or abs(cand.price - cand.sl) <= 0):
            return Decision.block("PROP_NO_STOP", "challenge requires a stop loss")
        if self._in_news_window(now):
            return Decision.block("PROP_NEWS", "high-impact news blackout window")
        if p.weekend_flat and self._is_weekend_end(now):
            return Decision.block("PROP_WEEKEND", "weekend flat rule - no new risk")
        if not risk_decision.allowed:
            return risk_decision

        lots = risk_decision.lots
        reasons = []

        # Lot cap, expressed as exposure rather than as a raw unit count.
        # A firm's "2 lots" means two standard FX lots of exposure; Deriv
        # synthetics price 1 lot as $1/point, so a raw cap would shrink a Deriv
        # position ~30x and quietly turn a 0.4% risk trade into 0.005%.
        cap = p.max_lots
        if notional_per_lot > 0:
            cap = p.max_lots * max(1.0, 100_000.0 / notional_per_lot)
        if lots > cap:
            lots = cap
            reasons.append(f"capped to firm exposure limit ({cap:.2f} lots here)")

        # ---- daily loss budget -------------------------------------------------
        # This is the rule that actually decides most challenges. It must be
        # enforced *before* the loss happens, not detected afterwards.
        day_loss_limit = st.day_start_equity * p.max_daily_loss_pct / 100.0
        day_loss_used = max(0.0, st.day_start_equity - st.equity)
        if day_loss_limit > 0:
            used_frac = day_loss_used / day_loss_limit
            # hard stop well before the firm's limit: never rely on the last 20%
            if used_frac >= 0.8:
                return Decision.block(
                    "PROP_DAY_BUDGET",
                    f"daily loss budget {used_frac:.0%} consumed - no new risk today")
            # soften before that
            off_at = max(0.05, p.risk_off_after_daily_loss_pct / p.max_daily_loss_pct)
            if used_frac >= off_at:
                lots *= max(0.15, 1.0 - (used_frac - off_at) / max(1e-9, 1.0 - off_at))
                reasons.append(f"day loss budget {used_frac:.0%} used")

            # a single stop-out may consume at most 35% of what is left today,
            # and total open risk may not exceed 60% of the remaining budget
            remaining = max(0.0, st.equity - self._daily_floor())
            per_lot_risk = max(0.0, float(risk_decision.detail.get("per_lot_risk", 0.0)))
            if per_lot_risk > 0:
                cap_by_trade = (remaining * 0.35) / per_lot_risk
                if lots > cap_by_trade:
                    lots = cap_by_trade
                    reasons.append("capped: one stop-out must not eat the day")
                budget_left = remaining * 0.60 - max(0.0, open_risk_money)
                if budget_left <= 0:
                    return Decision.block("PROP_OPEN_RISK",
                                          "open risk already uses the daily budget")
                cap_by_book = budget_left / per_lot_risk
                if lots > cap_by_book:
                    lots = cap_by_book
                    reasons.append("capped by aggregate open risk")
            if lots <= 0:
                return Decision.block("PROP_DAY_BUDGET", "daily loss budget exhausted")

        # near total-loss floor -> cut size hard
        total_remaining = st.equity - self._floor_equity()
        if total_remaining < st.start_balance * p.max_total_loss_pct / 100.0 * 0.35:
            lots *= 0.5
            reasons.append("close to total-loss floor: half size")

        # consistency: if today's profit already dominates, stop adding to it
        if p.consistency_pct > 0:
            day_profit = st.equity - st.day_start_equity
            total_profit = st.equity - st.start_balance
            if day_profit > 0 and total_profit > 0:
                share = day_profit / total_profit * 100.0
                if share >= p.consistency_pct:
                    return Decision.block(
                        "PROP_CONSISTENCY",
                        f"today is {share:.0f}% of total profit (rule {p.consistency_pct:.0f}%)")

        # target reached but waiting on minimum days -> protect, do not grow
        profit_pct = (st.equity - st.start_balance) / st.start_balance * 100.0
        if p.profit_lock_after_target and profit_pct >= p.profit_target_pct * 0.9:
            lots *= 0.4
            reasons.append("near target: capital preservation mode")

        # risk percentage shaping
        if lots < risk_decision.lots:
            return Decision(True, "reduced: " + "; ".join(reasons), lots=max(0.0, lots),
                            code="PROP_REDUCED",
                            detail={**risk_decision.detail, "prop_reductions": reasons})
        return Decision(True, "ok", lots=lots, code="PROP_OK",
                        detail={**risk_decision.detail, "prop_reductions": reasons})

    # ------------------------------------------------------------------ #
    def _in_news_window(self, now: float) -> bool:
        if not self.news_windows:
            return False
        pad = self.p.news_blackout_minutes * 60
        return any((t - pad) <= now <= (t + pad) for t, _ in self.news_windows)

    def _is_weekend_end(self, now: float) -> bool:
        """No new risk into the Friday close (UTC 19:00-24:00)."""
        g = time.gmtime(now)
        return g.tm_wday == 4 and g.tm_hour >= 19

    # ------------------------------------------------------------------ #
    def on_fill(self, ts: float) -> None:
        self.st.days_traded.add(self.st.day_index)
        self.st.total_trades += 1

    def on_trade_closed(self, trade: TradeRecord, ts: float) -> None:
        self.st.days_traded.add(self.st.day_index)
        self.st.balance += trade.pnl
        self.on_equity(ts, self.st.equity, self.st.balance)

    def should_flatten(self, now: float, has_positions: bool) -> Optional[str]:
        """Engine ABI: called each cycle; returns a reason to force-close everything."""
        st, p = self.st, self.p
        if not has_positions:
            st.flat_required = "" if st.flat_required == "eod" else st.flat_required
            return None
        if st.breach:
            return "prop_breach"
        if st.flat_required == "target_hit_min_days":
            return "target_hit"
        if self._is_weekend_end(now) and p.weekend_flat:
            return "weekend"
        # daily close-out: flatten 15 minutes before the reset boundary
        offset = p.daily_reset_hour_utc * 3600
        secs_into_day = (now - offset) % 86400
        if secs_into_day > 86400 - 900:
            return "eod"
        return None

    # ------------------------------------------------------------------ #
    def pacing(self, now: float) -> dict:
        """Where the account should be today, and how much risk that implies."""
        p, st = self.p, self.st
        profit = st.equity - st.start_balance
        target = st.start_balance * p.profit_target_pct / 100.0
        remaining = max(0.0, target - profit)
        days_used = max(1, int((now - st.started_ts) // 86400) + 1)
        days_left = max(1, p.max_calendar_days - days_used + 1)
        daily_target = remaining / days_left
        todays = st.equity - st.day_start_equity
        behind = max(0.0, daily_target - todays)
        # risk multiplier: behind schedule -> slightly larger, but never > max_risk
        mult = 1.0
        if daily_target > 0:
            mult = max(0.6, min(1.6, 1.0 + 0.6 * (behind / max(daily_target, 1e-9) - 0.5)))
        risk_pct = min(p.max_risk_per_trade_pct, p.risk_per_trade_pct * mult)
        return {
            "daily_target": round(daily_target, 2),
            "today_pnl": round(todays, 2),
            "behind_by": round(behind, 2),
            "days_used": days_used, "days_left": days_left,
            "recommended_risk_pct": round(risk_pct, 2),
        }

    def status(self, now: Optional[float] = None) -> dict:
        now = now if now is not None else self.st.last_ts or time.time()
        p, st = self.p, self.st
        profit = st.equity - st.start_balance
        target = st.start_balance * p.profit_target_pct / 100.0
        day_loss_limit = st.day_start_equity * p.max_daily_loss_pct / 100.0
        day_loss_used = max(0.0, st.day_start_equity - st.equity)
        total_room = st.equity - self._floor_equity()
        daily_room = st.equity - self._daily_floor()

        if st.breach:
            verdict = Verdict.BREACHED
        elif st.passed:
            verdict = Verdict.PASSED
        elif (st.equity - st.start_balance) >= target:
            verdict = Verdict.TARGET_HIT
        elif daily_room < day_loss_limit * 0.25 or total_room < st.start_balance * p.max_total_loss_pct / 100.0 * 0.3:
            verdict = Verdict.AT_RISK
        else:
            verdict = Verdict.ACTIVE

        return {
            "profile": p.name, "phase": p.phase, "verdict": verdict.value,
            "start_balance": round(st.start_balance, 2),
            "equity": round(st.equity, 2), "balance": round(st.balance, 2),
            "profit": round(profit, 2),
            "profit_pct": round(profit / st.start_balance * 100.0, 2) if st.start_balance else 0.0,
            "target_pct": p.profit_target_pct,
            "target_progress_pct": round(max(0.0, min(100.0, profit / target * 100.0)), 2) if target else 0.0,
            "daily_loss_used_pct": round(day_loss_used / day_loss_limit * 100.0, 2) if day_loss_limit else 0.0,
            "daily_loss_room": round(daily_room, 2),
            "total_loss_room": round(total_room, 2),
            "total_floor": round(self._floor_equity(), 2),
            "daily_floor": round(self._daily_floor(), 2),
            "days_traded": len(st.days_traded), "min_trading_days": p.min_trading_days,
            "days_left": max(0, p.max_calendar_days - int((now - st.started_ts) // 86400)),
            "consistency": self._consistency_snapshot(),
            "breach": st.breach, "flat_required": st.flat_required,
            "pacing": self.pacing(now),
            "rules": p.to_dict(),
        }

    def _consistency_snapshot(self) -> dict:
        st, p = self.st, self.p
        pnls = dict(st.day_pnls)
        pnls[st.day_index] = st.equity - st.day_start_equity
        total = sum(v for v in pnls.values() if v > 0)
        best = max(pnls.values()) if pnls else 0.0
        share = (best / total * 100.0) if total > 0 else 0.0
        return {"rule_pct": p.consistency_pct, "best_day_share_pct": round(share, 2),
                "ok": p.consistency_pct <= 0 or share <= p.consistency_pct}


class PropChallenge:
    """Multi-phase challenge orchestration (evaluation -> verification -> funded).

    A challenge is a sequence of identical-but-different rule sets, not a single
    account: FTMO phase 1 targets 10%, phase 2 targets 5%, the funded account has
    no target but keeps the loss limits. This object owns that progression so the
    engine only ever deals with one active `PropGuard`.
    """

    def __init__(self, phases: list[PropProfile], balance: float = 10_000.0,
                 now: Optional[float] = None):
        if not phases:
            raise ValueError("challenge needs at least one phase")
        self.phases = phases
        self.balance = balance
        self.index = 0
        self.history: list[dict] = []
        self.guard = PropGuard(phases[0], balance, now=now)
        self.started_ts = now if now is not None else time.time()

    # ------------------------------------------------------------------ #
    @property
    def phase(self) -> PropProfile:
        return self.phases[self.index]

    @property
    def phase_name(self) -> str:
        return f"{self.phase.name} (phase {self.index + 1}/{len(self.phases)})"

    def reset(self, balance: Optional[float] = None, now: Optional[float] = None) -> None:
        self.balance = self.balance if balance is None else balance
        self.guard = PropGuard(self.phase, self.balance, now=now)

    def sync(self, now: Optional[float] = None) -> Optional[str]:
        """Advance the challenge if the current phase has been passed.

        Returns a human-readable event, or None. The *next* phase starts from the
        current equity, which is how prop firms actually roll an account over.
        """
        if not self.guard.st.passed:
            return None
        result = self.guard.status(now)
        self.history.append({"phase": self.phase.name, "phase_index": self.index,
                             "verdict": result["verdict"], "profit_pct": result["profit_pct"],
                             "finished_ts": now or time.time()})
        if self.index + 1 >= len(self.phases):
            return f"challenge complete: {self.phase.name} funded (no further phases)"
        prev = self.phase.name
        self.index += 1
        self.balance = self.guard.st.equity
        self.guard = PropGuard(self.phase, self.balance, now=now)
        return f"phase passed: {prev} -> {self.phase_name} at balance {self.balance:.2f}"

    def check(self, cand: Candidate, decision: Decision, now: float,
              open_risk_money: float = 0.0, notional_per_lot: float = 0.0) -> Decision:
        return self.guard.check(cand, decision, now, open_risk_money=open_risk_money,
                                notional_per_lot=notional_per_lot)

    def status(self, now: Optional[float] = None) -> dict:
        out = self.guard.status(now)
        out["challenge"] = {"phase": self.phase_name, "index": self.index,
                            "phases": [p.name for p in self.phases],
                            "history": list(self.history)}
        return out


def ftmo_phases(balance: float = 10_000.0) -> list[PropProfile]:
    p1 = ftmo_2step(balance)
    p1.name, p1.phase = "ftmo_evaluation", 1
    p1.profit_target_pct = 10.0
    p2 = ftmo_2step(balance)
    p2.name, p2.phase = "ftmo_verification", 2
    p2.profit_target_pct = 5.0
    funded = ftmo_2step(balance)
    funded.name, funded.phase = "ftmo_funded", 3
    funded.profit_target_pct = 0.0          # no target: only the loss limits bind
    funded.profit_lock_after_target = False
    return [p1, p2, funded]


def plan_for(profile: PropProfile, equity: float) -> dict:
    """Concrete settings the engine should run with to pass this challenge."""
    # Risk scale so that N consecutive losses cannot eat the daily limit.
    safe_risk = min(profile.max_risk_per_trade_pct,
                    profile.max_daily_loss_pct / 6.0,
                    profile.max_total_loss_pct / 12.0)
    need_daily = equity * profile.profit_target_pct / 100.0 / max(1, profile.min_trading_days)
    return {
        "profile": profile.name,
        "risk_per_trade_pct": round(max(0.1, safe_risk), 2),
        "max_trades_per_day": min(profile.max_trades_per_day, profile.max_open_positions * 3),
        "required_daily_profit": round(need_daily, 2),
        "stop_loss_required": profile.stop_loss_required,
        "strategy_preset": "prop_conservative",
        "notes": profile.notes,
    }
