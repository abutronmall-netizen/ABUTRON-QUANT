"""One definition of a timeframe's length.

This used to be copy-pasted in `strategy`, `backtest` and `cli`; three copies of
the same dict is three places to forget when a timeframe is added.
"""
from __future__ import annotations

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800,
              "H1": 3600, "H4": 14400, "D1": 86400}


def tf_seconds(tf: str) -> int:
    return TF_SECONDS.get(str(tf).upper(), 300)
