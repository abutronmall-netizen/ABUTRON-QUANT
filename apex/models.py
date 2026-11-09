"""Core domain model for ABUTRON QUANT.

Every module (backtest, live engine, dashboard, prop guard) speaks these types,
so there is exactly one definition of a signal, an order, a fill and a trade.
That is deliberate: the single biggest defect in the audited APEX deployment was
multiple generations of incompatible trade objects.
"""
from __future__ import annotations

import itertools
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# primitives
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    def flip(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class OrderKind(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class Broker(str, Enum):
    DERIV = "deriv"
    MT5 = "mt5"
    SIM = "sim"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING = "trailing"
    TIME_STOP = "time_stop"
    SIGNAL_FLIP = "signal_flip"
    MANUAL = "manual"
    PROP_FLAT = "prop_flatten"
    EOD_FLAT = "eod_flatten"
    KILL_SWITCH = "kill_switch"
    CONTRACT_EXPIRY = "contract_expiry"


_counter = itertools.count(1)


def new_client_id(prefix: str = "aq") -> str:
    """Idempotency key. Executors must treat duplicates as no-ops."""
    return f"{prefix}-{int(time.time()*1000)}-{next(_counter)}-{uuid.uuid4().hex[:6]}"


@dataclass(slots=True)
class Bar:
    symbol: str
    ts: float              # epoch seconds, bar OPEN time
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return self.close - self.open

    def to_row(self) -> list[float]:
        return [self.ts, self.open, self.high, self.low, self.close, self.volume]

    @staticmethod
    def from_row(symbol: str, row: list[float]) -> "Bar":
        return Bar(symbol, float(row[0]), float(row[1]), float(row[2]),
                   float(row[3]), float(row[4]), float(row[5]) if len(row) > 5 else 0.0)


@dataclass(slots=True)
class Quote:
    symbol: str
    ts: float
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(slots=True)
class SymbolSpec:
    """Instrument contract details, resolved per broker.

    value_per_point_per_lot: account-currency P&L for a 1.0 move in price on 1 lot.
    For EURUSD 1 lot = 100_000 units  -> 100_000 per 1.0, i.e. $10/pip.
    For Deriv synthetic multipliers 1 lot = $1 per index point.
    """
    symbol: str
    broker: str
    digits: int = 2
    pip: float = 0.01
    contract_size: float = 1.0
    value_per_point_per_lot: float = 1.0
    min_lot: float = 0.01
    max_lot: float = 50.0
    lot_step: float = 0.01
    commission_per_lot: float = 0.0
    margin_per_lot: float = 0.0       # 0 -> derive from notional/leverage
    typical_spread: float = 0.0
    min_stop_distance: float = 0.0
    leverage: float = 100.0

    def round_lots(self, lots: float) -> float:
        step = self.lot_step or 0.01
        lots = max(0.0, min(lots, self.max_lot))
        return round(round(lots / step) * step, 8)

    def round_price(self, p: float) -> float:
        return round(p, self.digits)


# --------------------------------------------------------------------------- #
# intelligence
# --------------------------------------------------------------------------- #
class RegimeLabel(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    SQUEEZE = "squeeze"
    EXPANSION = "expansion"
    CHAOS = "chaos"


@dataclass(slots=True)
class Regime:
    label: RegimeLabel
    atr: float
    atr_pct: float          # ATR as fraction of price
    adx: float
    efficiency: float       # Kaufman efficiency ratio, 0..1
    bb_width_pct: float
    vol_percentile: float   # 0..1 vs rolling history
    slope: float            # normalised EMA slope

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["label"] = self.label.value
        return d


@dataclass(slots=True)
class Vote:
    """One agent's opinion. Signed score in [-1, +1]."""
    agent: str
    score: float
    confidence: float
    reasons: list[str] = field(default_factory=list)

    @property
    def side(self) -> Optional[Side]:
        if self.score > 0:
            return Side.BUY
        if self.score < 0:
            return Side.SELL
        return None


@dataclass(slots=True)
class Candidate:
    """A tradeable idea, before risk sizing / prop filtering."""
    symbol: str
    side: Side
    conviction: float              # 0..1, weighted agreement across agents
    price: float
    sl: float
    tp: float
    ttl_s: float
    strategy: str
    regime: str
    votes: list[Vote] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_distance(self) -> float:
        return abs(self.price - self.sl)

    @property
    def reward_distance(self) -> float:
        return abs(self.tp - self.price)

    @property
    def rr(self) -> float:
        return self.reward_distance / self.risk_distance if self.risk_distance else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "side": self.side.value, "conviction": round(self.conviction, 4),
            "price": self.price, "sl": self.sl, "tp": self.tp, "rr": round(self.rr, 2),
            "ttl_s": self.ttl_s, "strategy": self.strategy, "regime": self.regime,
            "votes": [{"agent": v.agent, "score": round(v.score, 3),
                       "confidence": round(v.confidence, 3), "reasons": v.reasons} for v in self.votes],
            "meta": self.meta,
        }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Order:
    symbol: str
    side: Side
    lots: float
    broker: str
    account: str
    strategy: str = "unspecified"
    kind: OrderKind = OrderKind.MARKET
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    ttl_s: float = 300.0
    client_id: str = field(default_factory=new_client_id)
    reduce_only: bool = False
    tag: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Ack:
    ok: bool
    client_id: str
    ts: float
    order_id: str = ""
    filled_price: float = 0.0
    lots: float = 0.0
    commission: float = 0.0
    reason: str = ""
    retryable: bool = False
    broker: str = ""
    account: str = ""
    state: str = "filled"        # filled | partial | pending | rejected
    requested_lots: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Position:
    id: str
    symbol: str
    side: Side
    lots: float
    entry: float
    sl: Optional[float]
    tp: Optional[float]
    opened_ts: float
    broker: str
    account: str
    strategy: str
    contract_id: str = ""              # broker-native id (Deriv contract_id / MT5 ticket)
    max_favorable: float = 0.0         # best excursion in price terms
    max_adverse: float = 0.0
    commission: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def unrealised(self, price: float, vpp: float) -> float:
        return (price - self.entry) * self.side.sign * self.lots * vpp

    def unrealised_r(self, price: float, risk_per_unit: float) -> float:
        """risk_per_unit = money risked per 1.0 price move."""
        if risk_per_unit <= 0:
            return 0.0
        return (price - self.entry) * self.side.sign / risk_per_unit

    def hit(self, price: float) -> Optional[ExitReason]:
        if self.sl is not None:
            if self.side is Side.BUY and price <= self.sl:
                return ExitReason.STOP_LOSS
            if self.side is Side.SELL and price >= self.sl:
                return ExitReason.STOP_LOSS
        if self.tp is not None:
            if self.side is Side.BUY and price >= self.tp:
                return ExitReason.TAKE_PROFIT
            if self.side is Side.SELL and price <= self.tp:
                return ExitReason.TAKE_PROFIT
        return None


@dataclass(slots=True)
class TradeRecord:
    trade_id: str
    symbol: str
    side: Side
    lots: float
    entry: float
    exit: float
    opened_ts: float
    closed_ts: float
    pnl: float
    commission: float
    strategy: str
    broker: str
    account: str
    exit_reason: ExitReason
    conviction: float = 0.0
    regime: str = ""
    r_multiple: float = 0.0
    mfe: float = 0.0
    mae: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def won(self) -> bool:
        return self.pnl > 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["exit_reason"] = self.exit_reason.value
        return d


@dataclass(slots=True)
class AccountState:
    broker: str
    account: str
    currency: str = "USD"
    balance: float = 10_000.0
    equity: float = 10_000.0
    margin_used: float = 0.0
    free_margin: float = 10_000.0
    leverage: float = 100.0
    ts: float = 0.0
    live: bool = False


@dataclass(slots=True)
class Decision:
    """Result of a risk / prop gate. Never silently drops an idea."""
    allowed: bool
    reason: str = ""
    lots: float = 0.0
    code: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def allow(lots: float, detail: Optional[dict] = None) -> "Decision":
        return Decision(True, "ok", lots=lots, code="OK", detail=detail or {})

    @staticmethod
    def block(code: str, reason: str, detail: Optional[dict] = None) -> "Decision":
        return Decision(False, reason, 0.0, code, detail or {})


def as_json(obj: Any) -> str:
    return json.dumps(obj, default=str, separators=(",", ":"))
