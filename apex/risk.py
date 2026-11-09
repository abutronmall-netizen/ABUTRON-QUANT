"""Risk engine: position sizing and hard capital protection.

Sizing is fixed-fractional on *stop distance in money*, so a 0.5% risk trade is
0.5% whether it is V75 on Deriv or EURUSD on FP Markets. Every rejection is coded
(`Decision.code`) and counted by the dashboard.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from .models import AccountState, Candidate, Decision, Position, SymbolSpec


def utc_day(ts: float) -> int:
    return int(ts // 86400)


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.50        # % of equity risked per trade
    max_daily_loss_pct: float = 3.0         # hard stop for the day
    daily_profit_lock_pct: float = 6.0      # stop trading after a great day
    max_total_drawdown_pct: float = 8.0     # from equity peak
    max_open_positions: int = 4
    max_trades_per_day: int = 20
    max_consecutive_losses: int = 3
    cooldown_after_loss_s: int = 120
    max_lots_per_trade: float = 100.0
    max_margin_usage_pct: float = 35.0
    min_free_margin_pct: float = 25.0
    max_spread_atr: float = 0.40
    session_start_utc: int = 0              # hour
    session_end_utc: int = 24
    trade_on_weekends: bool = False
    kill_switch: bool = False
    max_correlated_positions: int = 2


@dataclass
class RiskState:
    day: int = 0
    day_start_equity: float = 0.0
    day_pnl: float = 0.0
    day_trades: int = 0
    peak_equity: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    halt_kind: str = ""        # daily | streak | drawdown | manual
    rejections: dict[str, int] = field(default_factory=dict)
    last_close: dict[str, float] = field(default_factory=dict)


class RiskEngine:
    def __init__(self, cfg: Optional[RiskConfig] = None, initial_equity: float = 10_000.0,
                 now: Optional[float] = None):
        self.cfg = cfg or RiskConfig()
        self.state = RiskState(
            day=utc_day(now if now is not None else time.time()),
            day_start_equity=initial_equity,
            peak_equity=initial_equity,
        )

    # ------------------------------------------------------------------ #
    def on_equity(self, equity: float, ts: float) -> None:
        st = self.state
        day = utc_day(ts)
        if day != st.day:
            st.day = day
            st.day_start_equity = equity
            st.day_pnl = 0.0
            st.day_trades = 0
            st.cooldown_until = 0.0
            if not st.halted:
                st.consecutive_losses = 0
            elif st.halt_kind in ("daily", "streak"):
                # daily-loss and losing-streak halts are per-day by construction;
                # a max-drawdown halt is not, it needs a human decision.
                st.halted = False
                st.halt_reason = ""
                st.halt_kind = ""
                st.consecutive_losses = 0
        st.peak_equity = max(st.peak_equity, equity)
        st.day_pnl = equity - st.day_start_equity
        if st.day_start_equity > 0:
            dd = (st.peak_equity - equity) / st.peak_equity * 100.0
            daily = -st.day_pnl / st.day_start_equity * 100.0
            if daily >= self.cfg.max_daily_loss_pct and not st.halted:
                self.halt(f"daily loss limit {daily:.2f}% >= {self.cfg.max_daily_loss_pct}%",
                          kind="daily")
            if dd >= self.cfg.max_total_drawdown_pct and not st.halted:
                self.halt(f"max drawdown {dd:.2f}% >= {self.cfg.max_total_drawdown_pct}%",
                          kind="drawdown")

    def on_trade_closed(self, pnl: float, ts: float) -> None:
        st = self.state
        st.day_trades += 0  # closed trades are counted on entry
        if pnl < 0:
            st.consecutive_losses += 1
            st.cooldown_until = ts + self.cfg.cooldown_after_loss_s
            if st.consecutive_losses >= self.cfg.max_consecutive_losses:
                self.halt(f"{st.consecutive_losses} consecutive losses", kind="streak")
        else:
            st.consecutive_losses = 0
            st.cooldown_until = 0.0

    def halt(self, reason: str, kind: str = "manual") -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self.state.halt_kind = kind

    def resume(self) -> None:
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.halt_kind = ""
        self.state.consecutive_losses = 0
        self.state.cooldown_until = 0.0

    # ------------------------------------------------------------------ #
    def size(self, cand: Candidate, spec: SymbolSpec, acct: AccountState,
             risk_pct: Optional[float] = None) -> Decision:
        risk_pct = self.cfg.risk_per_trade_pct if risk_pct is None else risk_pct
        risk_money = acct.equity * risk_pct / 100.0
        stop_dist = cand.risk_distance
        if stop_dist <= 0 or spec.value_per_point_per_lot <= 0:
            return Decision.block("BAD_SIZING", "zero stop distance or value-per-point")
        per_lot_risk = stop_dist * spec.value_per_point_per_lot
        if per_lot_risk <= 0:
            return Decision.block("BAD_SIZING", "per-lot risk is zero")
        raw = risk_money / per_lot_risk
        lots = spec.round_lots(min(raw, self.cfg.max_lots_per_trade))
        if lots < spec.min_lot:
            return Decision.block(
                "LOT_BELOW_MIN",
                f"required {raw:.4f} lots < min {spec.min_lot} for {spec.symbol}",
                {"raw_lots": round(raw, 5), "risk_money": round(risk_money, 2),
                 "per_lot_risk": round(per_lot_risk, 4)},
            )
        # margin guard
        notional = lots * max(spec.contract_size, spec.value_per_point_per_lot) * cand.price
        margin = (lots * spec.margin_per_lot) if spec.margin_per_lot else \
            notional / max(spec.leverage, 1.0)
        if margin > acct.free_margin * self.cfg.max_margin_usage_pct / 100.0:
            lots = spec.round_lots(
                lots * (acct.free_margin * self.cfg.max_margin_usage_pct / 100.0) / max(margin, 1e-9))
            if lots < spec.min_lot:
                return Decision.block("MARGIN", "insufficient free margin at minimum lot",
                                      {"margin_needed": round(margin, 2),
                                       "free_margin": round(acct.free_margin, 2)})
        return Decision.allow(lots, {"risk_money": round(risk_money, 2),
                                     "risk_pct": risk_pct,
                                     "stop_distance": stop_dist,
                                     "per_lot_risk": round(per_lot_risk, 6),
                                     "value_per_point": spec.value_per_point_per_lot,
                                     "margin": round(margin, 2),
                                     "equity": round(acct.equity, 2)})

    # ------------------------------------------------------------------ #
    def evaluate(self, cand: Candidate, spec: SymbolSpec, acct: AccountState,
                 positions: list[Position], now: float,
                 risk_pct: Optional[float] = None) -> Decision:
        st, cfg = self.state, self.cfg
        if cfg.kill_switch:
            return self._reject("KILL_SWITCH", "kill switch engaged")
        if st.halted:
            return self._reject("HALTED", st.halt_reason)
        if now < st.cooldown_until:
            return self._reject("COOLDOWN", f"cooldown for {st.cooldown_until - now:.0f}s")
        hour = int((now % 86400) // 3600)
        if not (cfg.session_start_utc <= hour < cfg.session_end_utc):
            return self._reject("SESSION_CLOSED",
                                f"outside session {cfg.session_start_utc}-{cfg.session_end_utc} UTC")
        if not cfg.trade_on_weekends and time.gmtime(now).tm_wday >= 5:
            return self._reject("WEEKEND", "weekend trading disabled")
        if st.day_trades >= cfg.max_trades_per_day:
            return self._reject("MAX_TRADES_DAY", f"{st.day_trades} trades today")
        if len(positions) >= min(cfg.max_open_positions, cfg.max_correlated_positions + 2):
            return self._reject("MAX_POSITIONS", f"{len(positions)} positions open")
        if any(p.symbol == cand.symbol for p in positions):
            return self._reject("SYMBOL_HELD", f"{cand.symbol} already open")
        if st.day_start_equity > 0:
            daily_pct = st.day_pnl / st.day_start_equity * 100.0
            if daily_pct <= -cfg.max_daily_loss_pct:
                return self._reject("DAILY_LOSS", f"day P&L {daily_pct:.2f}%")
            if daily_pct >= cfg.daily_profit_lock_pct:
                return self._reject("PROFIT_LOCKED", f"day locked at +{daily_pct:.2f}%")
        dd = (st.peak_equity - acct.equity) / st.peak_equity * 100.0 if st.peak_equity else 0.0
        if dd >= cfg.max_total_drawdown_pct:
            return self._reject("MAX_DD", f"drawdown {dd:.2f}%")
        if acct.free_margin / max(acct.equity, 1e-9) * 100.0 < cfg.min_free_margin_pct:
            return self._reject("FREE_MARGIN", "free margin below floor")
        spread_atr = cand.meta.get("spread_atr")
        if spread_atr is not None and spread_atr > cfg.max_spread_atr:
            return self._reject("SPREAD", f"spread {spread_atr:.2f} ATR")

        sized = self.size(cand, spec, acct, risk_pct)
        if not sized.allowed and sized.code == "LOT_BELOW_MIN":
            self._reject(sized.code, sized.reason)
            return sized
        if not sized.allowed:
            self._reject(sized.code, sized.reason)
            return sized
        # register entry
        st.day_trades += 1
        return sized

    def _reject(self, code: str, reason: str) -> Decision:
        self.state.rejections[code] = self.state.rejections.get(code, 0) + 1
        return Decision.block(code, reason)

    # ------------------------------------------------------------------ #
    def snapshot(self) -> dict:
        st = self.state
        dd = (st.peak_equity - (st.day_start_equity + st.day_pnl)) / st.peak_equity * 100.0 \
            if st.peak_equity else 0.0
        return {
            "halted": st.halted, "halt_reason": st.halt_reason, "halt_kind": st.halt_kind,
            "day_pnl": round(st.day_pnl, 2), "day_trades": st.day_trades,
            "consecutive_losses": st.consecutive_losses,
            "peak_equity": round(st.peak_equity, 2),
            "drawdown_pct": round(dd, 2),
            "day_start_equity": round(st.day_start_equity, 2),
            "rejections": dict(st.rejections),
            "limits": {
                "risk_per_trade_pct": self.cfg.risk_per_trade_pct,
                "max_daily_loss_pct": self.cfg.max_daily_loss_pct,
                "max_total_drawdown_pct": self.cfg.max_total_drawdown_pct,
                "max_trades_per_day": self.cfg.max_trades_per_day,
            },
        }
