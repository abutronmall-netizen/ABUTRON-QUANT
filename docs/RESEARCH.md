# Research workflow — how to use this without fooling yourself

The harness exists to answer three questions in order. Skipping a step is how systems end up
"healthy" with zero trades, or profitable in a notebook and losing at the broker.

1. **Does the machinery work?** (tests, invariant checks)
2. **Can it capture a real edge when one exists?** (structured synthetic data)
3. **Does it survive costs and rules on my broker's data?** (walk-forward, prop simulation)

---

## 1. Machinery

```bash
python3 -m unittest discover -s tests      # 276 tests
python3 -m apex doctor
python3 -m apex verify --run runs/live      # the same checklist the System portal shows
```

The integration tests assert the invariants that matter: no trading during warmup, entries priced
off the current bar, stops filled at the stop, every trade journalled, reconciliation clean,
everything closed at the end of a run.

## 2. Edge capture — the synthetic control

`SyntheticFeed` is a regime-switching generator with intraday volatility seasonality and jump risk.
Its `trend_strength` knob controls how much genuine persistence the tape has.

| `--trend-strength` | What the tape is | Expected result |
| --- | --- | --- |
| 0.35 | near random walk | negative expectancy — costs, no edge |
| 1.5 | mild structure | around breakeven |
| 2.5–3.0 | clear regimes | positive avg R on most seeds |

The tape is **anchored**, not clocked: `BacktestRequest.anchor_ts` (default
`backtest.ANCHOR_TS`) fixes where the synthetic series starts, so `--seed 7` reproduces the same
bars tomorrow, on another machine, in CI. `--anchor-ts <epoch>` moves the anchor deliberately; tick
replay instead starts its clock at the first recorded tick. Before 2.4.0 the tape began at
`time.time()`, which meant a published reference run could not be checked an hour later — if you
find an older report in `runs/`, assume its tape no longer exists.

This is the honesty test. If the engine were leaking future information, it would look profitable
on the near-random tape too. Measured on this build (default five-index book, 4000 bars, seeds 1–6):

```bash
for s in 1 2 3 4 5 6; do
  python3 -m apex backtest --bars 4000 --seed $s --trend-strength 0.35 --no-record
done
```

| `--trend-strength` | mean avg R | median avg R | seeds positive |
| --- | --- | --- | --- |
| 0.35 (near random walk) | +0.06 | **−0.12** | 2/6 |
| 2.5 (clear regimes) | +0.05 | +0.04 | 4/6 |

That is not a profit curve and should not be read as one. The mean at 0.35 is carried by a single
seed at +1.09R while the median is negative — the signature of an engine that is not cheating, and
of a scalper whose costs are roughly the size of whatever weak edge exists. The 2.5 row moves the
median above zero on more seeds and still does not survive as a claim: five thousand bars of a
generator we wrote is not a market.

Do not tune against these numbers. Anything you find at 0.35 that looks profitable is a bug in the
harness, and the correct response is to fix the harness, not to ship the configuration.

**Never quote these numbers as performance.** They describe the harness.

## 3. Cost sensitivity

Cost is the dominant term for a scalper:

```
cost_per_round_trip ≈ (spread_bps + slippage_entry_bps + stop_slippage_bps) / 10_000 × price
cost_in_R           ≈ cost_per_round_trip / stop_distance
```

With a 1.0 ATR stop and an M5 ATR of ~6 bps, a 2 bps round trip is 0.33R. Your hit rate must
exceed ~43% just to break even at 1.8R targets. Run the backtest with your measured cost:

```python
BacktestRequest(..., spread_bps=1.0, slippage_bps=0.3, stop_slippage_bps=1.0)
```

## 4. Parameter search without overfitting

```bash
python3 -m apex sweep --bars 2500 --seeds 3 --combos 48 --out runs/sweep
```

* Objective (`score_result`): `avg_R × √trades / (1 + (maxDD/4)²)`, with a penalty for prop
  breaches and daily-loss usage. Deliberately not a P&L maximiser.
* Every combination is evaluated on **3 independent seeds**, and the report includes the score's
  standard deviation. A high mean with high variance is a coin flip, not a finding.
* `sweep.json` keeps every combination. Look at the *neighbourhood*: if the top row is a lone spike
  surrounded by negative scores, it is noise.

Recommended discipline:

1. sweep on 60% of the data;
2. take the top 3 *stable* parameter sets;
3. evaluate them on the held-out 40% (`--seed` different, `--bars` different);
4. only then run `propsim`.

## 5. Prop survivability

```bash
python3 -m apex propsim --profile ftmo_2step --seeds 12 --bars 4000
```

Read breach rate first, pass rate second. A zero breach rate with a zero pass rate means the risk
guard works and the strategy has no edge. Fix the edge, not the guard.

## 6. Real data

```bash
python3 -m apex fetch --symbol R_75 --bars 5000 --out data/R_75_M5.csv
```

`fetch` writes Deriv candles to `data/<SYMBOL>_<TF>.csv`. The backtester picks cached candles up
**automatically**: if `data/R_75_M5.csv` exists it is replayed instead of the synthetic tape, so the
research path is the production path.

```bash
python3 -m apex fetch --symbol R_75 --tf M5 --bars 5000   # writes data/R_75_M5.csv
python3 -m apex backtest --symbols R_75 --bars 4000       # uses the real candles
python3 -m apex backtest --symbols R_75 --synthetic-only  # ignore the cache
```

For MT5 history use `MT5Connection.rates(symbol, "M5", 5000)` and write the same CSV layout
(`ts,open,high,low,close,volume`), or drive `TradingEngine` directly with `ReplayFeed`.

Because Deriv's synthetic indices are generated by a published random process, they are a decent
place to study *execution and risk* — but their randomness means a momentum edge on them is
unlikely to be durable. Treat them as a laboratory, not an income source.

## 6b. Tick replay (real spreads, real stops)

Fetched candles are still modelled fills. To study cost honestly, record live quotes and replay
them:

```bash
python3 -m apex serve --mode deriv --config config/deriv_prop.json --record-ticks data/ticks.csv
# ...let it run through a session...
python3 -m apex backtest --symbols R_75 --ticks data/ticks.csv --prop ftmo_2step
```

What changes in this mode:

* every quote carries the **actual bid/ask**, so `SPREAD_TOO_WIDE` fires on real spreads;
* positions are managed tick by tick, and brackets trigger on the tick that crosses them;
* bars are only used for the decision timeframe, exactly as in live trading.

Record at least a full session per symbol you intend to trade, and compare the spread distribution
with your broker's statement before trusting any P&L.

## 7. What would make this research-grade

* Tick-level replay with queue position and partial fills.
* Regime-conditional out-of-sample testing (per-regime sample sizes are small; 30 trades per regime
  is nothing).
* Deflated Sharpe / multiple-testing correction across the sweep. With 48 combinations, the best
  in-sample score is biased upward by roughly `σ × √(2 ln N)`.
* A live-vs-backtest slippage study from the journal (`orders.filled_price` vs signal price).

These are listed rather than implemented because inventing them half-way would produce false
confidence, which is the failure this whole repository is arguing against.
