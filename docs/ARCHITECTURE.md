# Architecture

## Design rules

1. **One decision path.** Backtest, paper and live run `TradingEngine`.
2. **One object model.** `Bar`, `Quote`, `Candidate`, `Order`, `Ack`, `Position`, `TradeRecord`.
   No module defines its own trade representation.
3. **Advisory intelligence, authoritative risk.** Agents propose; `RiskEngine` sizes; `PropGuard`
   may only reduce or block; the router may only accept or reject. Nothing downstream can enlarge
   a position.
4. **No silent anything.** Every rejected idea carries a code; every order carries an ack; every
   reconciliation difference becomes an event.
5. **Stdlib only on the hot path.** Indicators, WebSocket, journal and dashboard are dependency
   free so the system is portable and auditable; `MetaTrader5` is the single optional native
   dependency and is isolated.

## Data flow

```
feed (deriv ws | mt5 poll | synthetic | replay)
  │  Bar (closed only)                    Quote (ticks / bar close)
  ▼                                            ▼
SymbolState                                 _manage_open_positions
  entry IndicatorSet (M5)                     bracket / trail / time stop
  context IndicatorSet (M15, H1)                        │
  BarAggregator per context tf                          ▼
  │                                              close_position → journal
  ▼
MarketContext → regime.detect()
  │
  ▼
agents (trend, meanrev, breakout, momentum, carry, volfilter) → Vote[]
  │
  ▼
portfolio.compute_conviction(votes, weights[regime]) → (conviction, side)
  │
  ▼
strategy.build_candidate() → composition rule → anti-chase → SL/TP/RR checks
  │
  ▼
portfolio.PortfolioManager.arbitrate() → position/symbol/strategy/correlation limits
  │
  ▼
RiskEngine.evaluate() → fixed-fractional size, halts, session, spread, margin
  │
  ▼
PropGuard.check() → daily budget, exposure-scaled lot cap, consistency, news, target lock
  │
  ▼
BrokerRouter.submit(Order) → ExecutionPort (sim | deriv | mt5)
  │
  ▼
Ack → Position → store → dashboard
  │
  └── on close → TradeRecord → RiskEngine.on_trade_closed → PropGuard → ReflectionMemory
```

## Why the composition rule exists

Conviction alone is dangerous. On a noisy tape, micro-momentum scores flip sign bar to bar, so a
threshold on the aggregate will happily trade noise and pay spread. The rule is:

> at least one **structural** agent (trend, mean-reversion, breakout) must contribute ≥ 0.10 in the
> trade's direction. Momentum may confirm; it may not originate.

This is deliberately *not* the old multi-timeframe AND gate: one structural agent at modest
strength suffices, and higher timeframes contribute graded credit rather than vetoes.

## Backtest integrity (three bugs found and fixed during the rebuild)

These are documented because they are the failure modes that make a backtest look profitable
while the deployed system loses.

1. **Phantom stops.** Positions were checked against the high/low of the very bar they were opened
   in — price action that happened *before* the position existed. Every large bar produced an
   instant stop-out. Fixed by tagging `entry_bar_ts` and skipping that bar.
2. **Stale price reference.** Entries were decided on the previous bar's close and filled at the
   current close, so the engine "knew" the bar's direction before entering. Positive edge of
   +6 bps per trade appeared from nothing. Fixed with an explicit `engine.set_quote()` step
   before `on_bar()`.
3. **Close-price stop fills.** A stop crossed mid-bar was filled at the bar's close, producing
   −2.3R losses against a −1R stop. Fixed by resolving intrabar brackets at the trigger price
   *before* managing survivors with the closing quote.

Each has a regression test: `tests/test_engine.py::test_entry_uses_current_bar_price_not_a_stale_quote`,
`test_stop_losses_are_bounded`, and the integration invariants.

## Cost model

The paper broker applies, per fill:

* **spread** — half the instrument spread on entry and exit (`SymbolSpec.typical_spread`, or a
  calibrated bps fallback);
* **slippage** — always adverse: `slippage_bps` on entries, `stop_slippage_bps` on stop exits;
* **commission** — per lot per side;
* **margin** — notional / leverage, or an explicit per-lot requirement;
* **rejections** — wide spread, duplicate symbol, insufficient margin.

On an M5 tape with ~6 bps ATR, a 1 bps round-trip cost is ~0.17 ATR of a 1.0 ATR stop. That is
enough to turn a zero-edge system negative, which is why instrument and session selection matter
more than parameter tuning. `apex/research/optimize.py` scores configurations with drawdown
penalised quadratically and prefers prop-surviving parameter sets.

## Regime classification

Derived from the entry timeframe plus M15/H1 trend:

| Label | Trigger (abridged) | Favoured agents |
| --- | --- | --- |
| `chaos` | ATR percentile > 0.88, efficiency < 0.30 | none (threshold raised to 0.75) |
| `expansion` | ATR percentile > 0.80, efficiency ≥ 0.42, ADX ≥ 20 | breakout, momentum |
| `trend_up/down` | efficiency ≥ 0.38, ADX ≥ 20, |EMA slope| > 1.2e-4 | trend, momentum |
| `squeeze` | ATR percentile < 0.28, ADX < 19 | breakout |
| `range` | otherwise | mean-reversion |

A trend label is downgraded to `range` when both M15 and H1 oppose it — a *downgrade*, not a veto.

## Reflection memory

For each closed trade, every agent with |score| ≥ 0.05 that voted in the trade's direction receives
`realised = ±R`. Per agent: `ewma_r ← ewma_r + 0.12 (realised − ewma_r)`. Weight:
`w ← clip(w · exp(0.55 · 0.12 · realised), 0.2, 3.0)`, per regime. Persisted to
`reflection.json`; loaded at start; corrupt state is ignored rather than fatal.

## Extension points

* **New agent** — subclass `Agent`, return a `Vote`, register in `AGENT_REGISTRY`, add a prior in
  `DEFAULT_WEIGHTS`.
* **New broker** — implement `ExecutionPort` (`account`, `positions`, `quote`, `spec`, `submit`,
  `close`, `modify`) and add a `Route`.
* **New instrument** — add a `SymbolSpec` to `SymbolRegistry` overrides (JSON) or `MT5_SYMBOLS`.
* **New prop firm** — a `PropProfile` in JSON; no code required.
* **New strategy family** — add a `StrategyProfile` preset; the orchestration stays identical.


---

## Operational layer (what makes it runnable, not just testable)

```
            ┌─────────────── process start ───────────────┐
            │ store.load_state("engine")                  │  risk halt, day counters,
            │   ↳ RiskEngine.import_state                 │  prop phase, open positions
            │   ↳ PropGuard.import_state                  │
            │ engine.boot_sync(policy)                    │  broker positions: adopt | flatten
            └───────────────────┬─────────────────────────┘
                                │
   Deriv WS ──▶ FeedSupervisor ─┤  staleness 90 s → backoff → resubscribe → resync bars
   MT5 ticks ──▶ offset→UTC ────┤  server time converted at the adapter boundary
   Calendar ───▶ NewsCalendar ──┤  windows → PropGuard.news_windows (PROP_NEWS)
                                ▼
                        TradingEngine (unchanged single path)
                                │
                         every 30 s: router.reconcile(engine.positions)
```

Around that loop sit four things that exist only because the loop can fail:

```
  heartbeat.json ──▶ Watchdog (separate process)
                       stale → alert → flatten request ──▶ honoured at next boot
  journal ─────────▶ apex verify    (§3 of the deployment checklist, mechanically)
  events ──────────▶ Notifier       (webhook/SMTP, throttled per kind)
  ticks/bars ──────▶ FeedMonitor    (bar gaps, venue clock drift)
```

`PendingBook` closes the last hole in the order path: an order the venue accepted but did not fill
is risk, so it is tracked with an expiry, polled through the adapter's `order_status`, adopted as a
real position when it fills and cancelled when it goes stale. `EngineConfig.scale_out_r` lets a
position give back part of itself at a target without ending the trade; a partial close realises
P&L and shrinks `pos.lots` and `risk_money` proportionally. Aggregate risk is capped in money
(`max_heat_pct`, `max_group_heat_pct`) because a position count cannot see the difference between
four 0.5% trades and four 4% trades.

Optional reviewer: `OllamaReviewer` is called *after* risk and prop have sized a trade, receives
the candidate, the lots and the agent votes, and can only return "allow" or "block". The engine
itself enforces the configured mode (advisory mode cannot block), and every failure path fails
open. It is off by default and it never sees an unsigned idea.

Design rules enforced here:

* **The broker is the truth.** On boot the engine adopts what the broker holds; the journal is a
  cache of intent. A position can be in the journal and missing at the broker, but never the
  reverse without an alert.
* **State is durable or it does not exist.** Anything that changes a limit (halt, day counters,
  prop phase) is written to `runtime_state` on save and restored before the first bar.
* **A silent feed is a dead feed.** Liveness is message recency, not socket state — and a live
  feed that starves you is also a failure, so gaps and clock drift are measured, not assumed.
* **An alarm that always fires is an alarm nobody reads.** Reconciliation keys on
  `(symbol, ticket)`, not on the broker label: the label differs between the engine's route name
  and a paper adapter's own name, and that mismatch once made every paper position look like a
  discrepancy.
* **The watchdog is not the engine.** It cannot open risk, only reduce it, and it runs as its own
  process precisely because the failure it handles is "the engine is gone".
* **Auth is on the mutation surface only.** Reads stay open for the dashboard; POSTs need a token
  and a same-origin request.

Rules that are deliberately *not* here: distributed consensus, a message bus, multi-process
sharding. One account, one challenge, one process — a second replica of a trading engine is a
second set of orders.
