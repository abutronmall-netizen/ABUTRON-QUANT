"""Execution port + symbol registry.

Every broker adapter implements `ExecutionPort`. The engine, the backtester and
the live router all talk to that interface and nothing else, which is what makes
"backtested" and "live" the same code path (audit defect P0-3).
"""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from ..models import AccountState, Ack, Order, Position, Quote, SymbolSpec


@runtime_checkable
class ExecutionPort(Protocol):
    name: str

    def account(self) -> AccountState: ...
    def positions(self) -> list[Position]: ...
    def quote(self, symbol: str) -> Optional[Quote]: ...
    def spec(self, symbol: str) -> SymbolSpec: ...
    def submit(self, order: Order) -> Ack: ...
    def close(self, position: Position, reason: str,
              price: Optional[float] = None, volume: Optional[float] = None) -> Ack: ...
    def modify(self, position: Position, sl: Optional[float], tp: Optional[float]) -> Ack: ...


# --------------------------------------------------------------------------- #
# Instrument defaults
# --------------------------------------------------------------------------- #
def _deriv_index(symbol: str, digits: int = 2) -> SymbolSpec:
    return SymbolSpec(
        symbol=symbol, broker="deriv", digits=digits, pip=10 ** -digits,
        contract_size=1.0, value_per_point_per_lot=1.0,
        # 1 lot == 1 USD of P&L per index point, so a 0.5% risk on a ~2 point
        # ATR stop sizes in the tens of lots. This is a sizing convention, not a
        # contract count: DerivBroker converts lots to multiplier-contract stake.
        min_lot=0.01, max_lot=500.0, lot_step=0.01,
        commission_per_lot=0.0, leverage=100.0, typical_spread=0.0,
        min_stop_distance=0.0,
    )


def _fx(symbol: str, vpp: float = 100_000.0, digits: int = 5, spread: float = 0.00012,
        min_lot: float = 0.01, max_lot: float = 20.0) -> SymbolSpec:
    return SymbolSpec(
        symbol=symbol, broker="mt5", digits=digits, pip=10 ** -(digits - 1),
        contract_size=vpp, value_per_point_per_lot=vpp, min_lot=min_lot, max_lot=max_lot,
        lot_step=0.01, commission_per_lot=3.5, leverage=500.0, typical_spread=spread,
        min_stop_distance=spread,
    )


def _index(symbol: str, vpp: float = 1.0, digits: int = 1, spread: float = 0.8,
           min_lot: float = 0.1, max_lot: float = 50.0) -> SymbolSpec:
    return SymbolSpec(
        symbol=symbol, broker="mt5", digits=digits, pip=10 ** -digits,
        contract_size=vpp, value_per_point_per_lot=vpp, min_lot=min_lot, max_lot=max_lot,
        lot_step=0.01, commission_per_lot=0.0, leverage=200.0, typical_spread=spread,
        min_stop_distance=spread,
    )


DERIV_SYMBOLS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    "BOOM300N", "BOOM500", "BOOM1000", "CRASH300N", "CRASH500", "CRASH1000",
    "JD10", "JD25", "JD50", "JD75", "JD100",
]

MT5_SYMBOLS = {
    # FX
    "EURUSD": _fx("EURUSD", 100_000, 5, 0.00012),
    "GBPUSD": _fx("GBPUSD", 100_000, 5, 0.00016),
    "USDJPY": _fx("USDJPY", 100_000, 3, 0.014),
    "AUDUSD": _fx("AUDUSD", 100_000, 5, 0.00016),
    "USDCAD": _fx("USDCAD", 100_000, 5, 0.00018),
    "EURJPY": _fx("EURJPY", 100_000, 3, 0.018),
    # metals
    "XAUUSD": SymbolSpec("XAUUSD", "mt5", digits=2, pip=0.01, contract_size=100,
                         value_per_point_per_lot=100.0, min_lot=0.01, max_lot=20.0,
                         lot_step=0.01, commission_per_lot=3.0, leverage=500.0,
                         typical_spread=0.22, min_stop_distance=0.30),
    "XAGUSD": SymbolSpec("XAGUSD", "mt5", digits=3, pip=0.001, contract_size=5000,
                         value_per_point_per_lot=5000.0, min_lot=0.01, max_lot=20.0,
                         lot_step=0.01, commission_per_lot=0.0, leverage=200.0,
                         typical_spread=0.025, min_stop_distance=0.04),
    # indices / crypto CFDs commonly offered by FP Markets
    "US30": _index("US30", 1.0, 1, 2.0),
    "NAS100": _index("NAS100", 1.0, 1, 1.5),
    "SPX500": _index("SPX500", 1.0, 2, 0.6),
    "GER40": _index("GER40", 1.0, 1, 1.2),
    "UK100": _index("UK100", 1.0, 1, 1.5),
    "BTCUSD": SymbolSpec("BTCUSD", "mt5", digits=2, pip=0.01, contract_size=1,
                         value_per_point_per_lot=1.0, min_lot=0.01, max_lot=10.0,
                         lot_step=0.01, leverage=100.0, typical_spread=30.0,
                         min_stop_distance=50.0),
}


class SymbolRegistry:
    """Resolves specs per broker, with broker-specific overrides from config."""

    def __init__(self, overrides: Optional[dict] = None):
        self.overrides: dict[str, dict] = overrides or {}

    def spec(self, broker: str, symbol: str) -> SymbolSpec:
        key = f"{broker}:{symbol}"
        if key in self.overrides:
            base = self._base(broker, symbol)
            for k, v in self.overrides[key].items():
                if hasattr(base, k):
                    setattr(base, k, v)
            return base
        return self._base(broker, symbol)

    def _base(self, broker: str, symbol: str) -> SymbolSpec:
        if broker == "deriv" or symbol.startswith(("R_", "1HZ", "BOOM", "CRASH", "JD", "stpRNG")):
            digits = 2
            return _deriv_index(symbol, digits)
        if broker == "mt5" and symbol in MT5_SYMBOLS:
            base = MT5_SYMBOLS[symbol]
            return SymbolSpec(**{f: getattr(base, f) for f in base.__dataclass_fields__})
        # broker-native symbol variants (e.g. EURUSD.m, XAUUSD.a)
        stripped = symbol.split(".")[0].split("#")[0]
        if stripped in MT5_SYMBOLS:
            base = MT5_SYMBOLS[stripped]
            d = {f: getattr(base, f) for f in base.__dataclass_fields__}
            d["symbol"] = symbol
            return SymbolSpec(**d)
        return SymbolSpec(symbol=symbol, broker=broker)
