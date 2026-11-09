"""The tradable universe, in one place.

The volatility indices are the desk's home market: Deriv quotes R_10..R_100
continuously, the spread is fixed, and there is no session to miss. R_10 and
R_100 are the two extremes — the least and most volatile synthetic — and they
are part of the default book because a scalper that only trades the middle of
the curve is choosing the hardest subset on purpose.

Per-broker lists exist because FP Markets symbols are suffixed by the broker
(`EURUSD.m`); the descriptor resolves a base name at connect time.
"""
from __future__ import annotations

import re

_VOL_INDEX = re.compile(r"^[Rr][ _-]?(10|25|50|75|100|200|250|300|500|1000)$")

VOLATILITY_INDICES = ["R_10", "R_25", "R_50", "R_75", "R_100"]

#: the default Deriv book: every volatility index, low to high
DEFAULT_SYMBOLS = list(VOLATILITY_INDICES)

FX_MAJORS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "EURJPY"]
METALS = ["XAUUSD", "XAGUSD"]
INDICES = ["US30", "NAS100", "SPX500", "GER40", "UK100"]

DEFAULT_SYMBOLS_BY_BROKER = {
    "deriv": list(DEFAULT_SYMBOLS),
    "mt5": list(FX_MAJORS + METALS + INDICES),
}


def normalise_symbol(symbol: str) -> str:
    """`r10` and `R 10` are the same instrument as `R_10`; `EURUSD.m` is not touched."""
    sym = str(symbol).strip()
    match = _VOL_INDEX.match(sym)
    return f"R_{match.group(1)}" if match else sym


def parse_symbols(value) -> list[str]:
    """Accept 'R_10,R_50', 'R10 R50' or ['R_10', 'R_50'] and return a clean,
    de-duplicated list in the order given."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(v) for v in value]
    else:
        raw = re.split(r"[,\s]+", str(value))
    out: list[str] = []
    for item in raw:
        sym = normalise_symbol(item)
        if sym and sym not in out:
            out.append(sym)
    return out
