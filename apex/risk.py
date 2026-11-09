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


def session_day(ts: float, reset_hour_utc: int = 0) -> int:
    """Firm trading-day index.

    A prop firm's day does not start at UTC midnight; FTMO and most others reset
    at 21:00/22:00 UTC (midnight server time). The risk governor and the prop
    guard MUST agree on this boundary, otherwise the risk halt clears hours
    before the prop day ends and the account gets a second full daily budget
    inside the same firm day.
    """
    return int((ts - int(reset_hour_utc) * 3600) // 86400)


def utc_day(ts: float) -> int:
    """Backwards-compatible alias for a UTC-midnight day index."""
    return session_day(ts, 0)


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
    max_lots_per_trade: float = 1000.0   # instrument specs cap this properly
    max_margin_usage_pct: float = 35.0
    min_free_margin_pct: float = 25.0
    max_spread_atr: float = 0.40
    session_start_utc: int = 0              # hour
    session_end_utc: int = 24
    daily_reset_hour_utc: int = 0           # firm day boundary (0 = UTC midnight)
    trade_on_weekends: bool = False
    kill_switch: bool = False
    max_correlated_positions: int = 2
    # Aggregate risk, in money. Position counts say nothing about whether four
    # open trades each risk 0.5% or 4%. 0 disables the cap.
    max_heat_pct: float = 0.0           # total open risk as % of equity
    max_group_heat_pct: float = 0.0     # per correlated group (needs groups=)
    # Per-instrument trading windows in UTC hours. Keys may be a symbol
    # ("XAUUSD") or a correlation group ("metals"). Falls back to the global
    # session_start_utc/session_end_utc pair.
    session_hours: dict[str, tuple[int, int]] = field(default_factory=dict)


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
            day=session_day(now if now is not None else time.time(),
                            self.cfg.daily_reset_hour_utc),
            day_start_equity=initial_equity,
            peak_equity=initial_equity,
        )

    # ------------------------------------------------------------------ #
    def on_equity(self, equity: float, ts: float) -> None:
        st = self.state
        day = session_day(ts, self.cfg.daily_reset_hour_utc)
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
        capped = False
        if margin > acct.free_margin * self.cfg.max_margin_usage_pct / 100.0:
            lots = spec.round_lots(
                lots * (acct.free_margin * self.cfg.max_margin_usage_pct / 100.0) / max(margin, 1e-9))
            capped = True
            if lots < spec.min_lot:
                return Decision.block("MARGIN", "insufficient free margin at minimum lot",
                                      {"margin_needed": round(margin, 2),
                                       "free_margin": round(acct.free_margin, 2)})
        # Report the risk the position will ACTUALLY carry. If a cap (lot, margin,
        # instrument maximum) reduced the size, R-multiples must be computed
        # against the real exposure - otherwise every capped trade looks like a
        # smaller winner and a smaller loser than it really was.
        actual_risk = lots * per_lot_risk
        return Decision.allow(lots, {"risk_money": round(actual_risk, 2),
                                     "intended_risk": round(risk_money, 2),
                                     "size_capped": capped or lots < raw - 1e-9,
                                     "risk_pct": risk_pct,
                                     "stop_distance": stop_dist,
                                     "per_lot_risk": round(per_lot_risk, 6),
                                     "value_per_point": spec.value_per_point_per_lot,
                                     "margin": round(margin, 2),
                                     "equity": round(acct.equity, 2)})

    # ------------------------------------------------------------------ #
    def session_window(self, symbol: str,
                       groups: Optional[dict[str, str]] = None) -> tuple[int, int]:
        """Resolve the trading window: symbol override, then group, then global."""
        cfg = self.cfg
        if cfg.session_hours:
            for key in (symbol, (groups or {}).get(symbol)):
                if key and key in cfg.session_hours:
                    lo, hi = cfg.session_hours[key]
                    return int(lo), int(hi)
        return int(cfg.session_start_utc), int(cfg.session_end_utc)

    @staticmethod
    def open_risk_money(positions: list[Position]) -> float:
        """Money at risk if every open stop filled, as last recorded at entry."""
        total = 0.0
        for p in positions:
            try:
                total += float(p.meta.get("risk_money") or 0.0)
            except (TypeError, ValueError, AttributeError):
                continue
        return total

    def evaluate(self, cand: Candidate, spec: SymbolSpec, acct: AccountState,
                 positions: list[Position], now: float,
                 risk_pct: Optional[float] = None,
                 groups: Optional[dict[str, str]] = None) -> Decision:
        st, cfg = self.state, self.cfg
        if cfg.kill_switch:
            return self._reject("KILL_SWITCH", "kill switch engaged")
        if st.halted:
            return self._reject("HALTED", st.halt_reason)
        if now < st.cooldown_until:
            return self._reject("COOLDOWN", f"cooldown for {st.cooldown_until - now:.0f}s")
        hour = int((now % 86400) // 3600)
        start_h, end_h = self.session_window(cand.symbol, groups)
        if not (start_h <= hour < end_h):
            return self._reject("SESSION_CLOSED",
                                f"outside session {start_h}-{end_h} UTC for {cand.symbol}")
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

        # aggregate heat, in money: the count caps above cannot see this
        equity = max(acct.equity, 1e-9)
        actual_risk = float(sized.detail.get("risk_money") or 0.0)
        if cfg.max_heat_pct > 0 and positions:
            heat = (self.open_risk_money(positions) + actual_risk) / equity * 100.0
            if heat > cfg.max_heat_pct:
                self._reject("PORTFOLIO_HEAT",
                             f"heat {heat:.2f}% > {cfg.max_heat_pct:.2f}%")
                return Decision.block(
                    "PORTFOLIO_HEAT",
                    f"aggregate open risk would be {heat:.2f}% of equity "
                    f"(cap {cfg.max_heat_pct:.2f}%)",
                    {"heat_pct": round(heat, 3), "cap_pct": cfg.max_heat_pct,
                     "new_risk": round(actual_risk, 2)})
        if cfg.max_group_heat_pct > 0 and positions and groups:
            mine = groups.get(cand.symbol)
            if mine:
                same = [p for p in positions if groups.get(p.symbol) == mine]
                gheat = (self.open_risk_money(same) + actual_risk) / equity * 100.0
                if gheat > cfg.max_group_heat_pct:
                    self._reject("GROUP_HEAT",
                                 f"group {mine} heat {gheat:.2f}% > "
                                 f"{cfg.max_group_heat_pct:.2f}%")
                    return Decision.block(
                        "GROUP_HEAT",
                        f"group '{mine}' risk would be {gheat:.2f}% of equity "
                        f"(cap {cfg.max_group_heat_pct:.2f}%)",
                        {"group": mine, "heat_pct": round(gheat, 3),
                         "cap_pct": cfg.max_group_heat_pct})
        # register entry
        st.day_trades += 1
        return sized

    # ------------------------------------------------------------------ #
    # state persistence (a restart must not refund the daily budget)
    # ------------------------------------------------------------------ #
    def export_state(self) -> dict:
        st = self.state
        return {"day": st.day, "day_start_equity": st.day_start_equity,
                "day_pnl": st.day_pnl, "day_trades": st.day_trades,
                "peak_equity": st.peak_equity,
                "consecutive_losses": st.consecutive_losses,
                "cooldown_until": st.cooldown_until,
                "halted": st.halted, "halt_reason": st.halt_reason,
                "halt_kind": st.halt_kind}

    def import_state(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        for key in ("day", "day_start_equity", "day_pnl", "day_trades", "peak_equity",
                    "consecutive_losses", "cooldown_until", "halted", "halt_reason",
                    "halt_kind"):
            if key in data:
                setattr(self.state, key, data[key])

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
