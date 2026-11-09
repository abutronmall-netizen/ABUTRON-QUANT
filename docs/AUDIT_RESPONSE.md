# Audit response — defect by defect

The source audit of ABUTRON APEX (9 Nov 2026) listed four P0s, three P1s and one P2. This
document maps each to the code that answers it and the test that proves the answer holds.

---

## P0-1 — APEX 1.1.0 staged, an older scanner ran production

**Defect.** Regime detection, agent voting, portfolio weighting and reflection memory existed in
`/opt/abutron-apex11-staging/` but the production path executed `apex_ema_rsi_mtf_research_v1`.

**Fix.** There is exactly one engine class and no alternate strategy path:

```
apex/engine.py::TradingEngine
    on_bar → indicators → regime → agents → conviction
           → arbitration → risk → prop guard → router
```

`backtest.py`, `live.py` and the dashboard all instantiate `TradingEngine`. The agent set, the
weights, the thresholds and the guard that the backtester measures are the ones that would trade.

**Evidence.**
* `tests/test_integration.py::test_runs_and_closes_everything` — backtest drives the real engine.
* `tests/test_engine.py::test_engine_takes_trades_and_journals_them`.
* `apex/engine.py` imports `build_candidate` from `strategy.py`; there is no other entry generator
  in the package (`grep -rn "def build_candidate" apex/` returns one hit).

**Residual risk.** The APEX 1.1.0 source was not available here, so its specific agent formulas
were not ported line for line. What was rebuilt is the architecture it described, with the
composition problem (below) fixed.

---

## P0-2 — the signal path produced zero qualified entries

**Defect.** M5 entries required M15, H1 *and* H4 to agree. That conjunction is rare, so the router
reported `NO_SCAN_CANDIDATES` while the platform looked healthy.

**Fix — three parts.**

1. **Agreement became a score.** `compute_conviction()` sums weighted votes; opposition divides,
   support accumulates, and the threshold is per regime. Higher timeframes contribute graded
   credit (`+0.30 / +0.20`), with "unknown" treated as neutral instead of a veto.
2. **A composition rule replaced the timeframe AND gate.** At least one *structural* agent
   (trend, mean-reversion or breakout) must lean the same way; micro-momentum may only time an
   entry, never originate one. This stops noise-churn without requiring universal agreement.
3. **Every rejection is coded and counted.** `NO_STRUCTURE`, `LOW_CONVICTION`, `OVEREXTENDED`,
   `SPREAD_TOO_WIDE`, `RR_TOO_LOW`, `PROP_DAY_BUDGET`, … The dashboard ranks them; the backtest
   report tabulates them; `signal_stats()` persists them. A silent dry spell is impossible.

**Evidence.**
* `tests/test_integration.py::test_decisions_are_explained` — rejections and signal statuses are
  always present.
* `python3 -m apex backtest --bars 4000` prints a `rejections:` line for every run.
* The synthetic harness can reproduce the failure mode deliberately: at trend strength 0.35 (a
  pure random walk) the engine correctly refuses to trade a perceived edge and bleeds costs —
  visible as `avgR ≈ −0.1`, which is the honest result, not a bug.

**Second-order fix.** A subtle variant of the same bug appeared *during* this rebuild: the
anti-chase filter measured distance from session VWAP, which grows monotonically on a trending
day and blocks every entry after the first hour. It now measures distance from the fast EMA.
Another: entries were priced off the previous bar's close while filling at the current one, which
manufactured edge. See `docs/ARCHITECTURE.md` §"Backtest integrity".

---

## P0-3 — the native execution interface was never completed

**Defect.** Orders could not reliably reach a broker; the runtime ran in reconciliation mode.

**Fix.** `apex/execution/router.py` gives every order:

* **idempotency** — a `client_id` executes at most once; duplicates return the original ack;
* **retries** — only for retryable failures (requote, price-changed, timeout), with backoff;
* **explicit failure** — every submit returns an `Ack` and writes an event; nothing is dropped;
* **reconciliation** — internal positions are compared with broker truth, and discrepancies are
  raised as events (`missing_at_broker`, `unknown_at_engine`);
* **kill switch** — blocks all new risk and can flatten the book.

**Evidence.**
* `tests/test_execution.py::test_idempotent_client_id`, `test_retry_on_retryable_failure`,
  `test_no_route_is_reported_not_dropped`, `test_kill_switch_blocks_and_flattens`,
  `test_reconcile_detects_divergence`.
* `tests/test_engine.py::test_reconciliation_clean_after_trading`.

**Residual risk.** End-to-end confirmation against a live broker has not been performed. The
adapters are tested against faithful fakes (`FakeMT5`, `FakeWS`); demo accounts are the next gate.

---

## P0-4 — FP Markets sat outside a Deriv-only router

**Defect.** The native router was Deriv-specific, so an MT5 connection could exist without
producing an executable APEX trade.

**Fix.** Routing is data, not code: a `Route(broker, account, port, symbols)` list. Both adapters
implement the same `ExecutionPort`. Symbol specs resolve per broker (pip size, contract size,
value per point, commission, stop minimums, leverage), and the prop guard scales lot caps by
*exposure* so the same "2 lot" rule means the same risk on EURUSD and on Deriv R_75.

**Evidence.**
* `tests/test_integration.py::test_two_brokers_route_independently` — one backtest, two brokers.
* `tests/test_prop.py::test_lot_cap_scales_by_exposure`.
* `tests/test_execution.py::TestMT5Adapter` — payload, retcode mapping, dry-run safety.

---

## P1 — legacy scanner and AI dependencies remained

**Fix.** The package contains one engine, one agent set, one strategy profile family and one
config schema. The optional LLM reviewer (`apex/ai/reviewer.py`) is outside the decision loop:
`advisory` annotates, `veto` can only block, it fails open on timeout, and it never sees
credentials. `doctor` reports every component's state so nothing is "installed but unknown".

---

## P1 — AI research disconnected from trade results

**Fix.** `ReflectionMemory` consumes closed trades: each agent that supported the direction gets
the realised R-multiple, an EWMA per regime, and a clipped exponential weight update. Weights are
persisted and shown in the dashboard. This is a closed learning loop over *actual* outcomes, not
a research notebook.

**Evidence.** `tests/test_integration.py` runs produce `reflection.json`; the dashboard's "Agent
weights" panel shows `n` and `ewma_r` per agent once trades close.

**Honest caveat.** With a few dozen trades the weights are still mostly priors. Reflexivity is
real: an agent that gets lucky early gains weight. Shrinkage and clipping bound the damage; they
do not eliminate the statistical problem.

---

## P1 — no reliable strategy-to-broker P&L attribution

**Fix.** The journal records every signal (status + reason + diagnostics), order (with ack),
trade (with strategy, broker, account, instrument, regime, conviction, MFE/MAE, R-multiple,
exit reason) and equity tick. `compute_metrics()` slices by strategy, symbol, broker, exit reasonand
regime. The report writes all of it to `report.md` / `summary.json` / `trades.csv`.

**Evidence.** `tests/test_store.py`, `tests/test_integration.py::test_every_trade_has_bounded_risk`.

---

## P2 — several versions of workers and strategies coexisted

**Fix.** One package, one version string (`apex.__version__`), one schema, one CLI. `doctor`
prints it. There is no staging directory: a change is either in the engine or it is not running.

---

## Recommended development order (from the audit) — status

1. **Make one engine authoritative.** Done.
2. **Complete Deriv and FP Markets order interfaces; confirm broker-recorded orders on demo.**
   Code complete; **demo confirmation outstanding** — see `docs/LIVE_DEPLOYMENT.md` §Validation.
3. **Complete end-to-end performance measurement.** Done (journal + attribution + dashboard).

The audit's closing question — *what value do more models add while the loop is broken?* — is
answered structurally: the reviewer is optional, and the only model that changes behaviour is the
one scoring real outcomes.

---

## Phase 2 — the operational defects the audit implied but did not name

The audit was an analysis of a trading failure. Running the rebuilt engine unattended exposed a
second class: failures that look like success from the dashboard.

| Defect | Consequence before the fix | Fix | Test |
| --- | --- | --- | --- |
| The process restarts with positions open | Engine thinks it is flat, opens a second position on the same symbol, leaves the first unmanaged | `boot_sync()` adopts the broker book (or flattens it), idempotently | `test_adopts_position_the_broker_already_holds` |
| Risk/prop state lived only in memory | A crash at 14:00 refunds the daily loss budget and the challenge counters | `runtime_state` journal + `import_state` before the first bar | `test_state_survives_restart` |
| Risk reset at UTC midnight, prop at the firm's hour | Two daily budgets inside one firm day | Shared `session_day(ts, reset_hour)` | `test_risk_day_boundary_uses_firm_reset_hour` |
| Deriv socket dies quietly | Dashboard green, nothing trades, nobody is told | Staleness → backoff → resubscribe → resync | `test_reconnects_with_exponential_backoff` |
| MT5 timestamps are server time | Session windows and EOD flat fire 2–3 h early | Offset detected and stripped at the adapter | `test_detects_a_two_hour_ahead_server` |
| Control endpoints unauthenticated | Anyone who can reach the port flattens the account | Token + same-origin check + localhost default | `test_control_requires_token` |
| Static handler stripped `..` | `//etc/passwd` escaped the asset root (real vulnerability) | Resolve-then-verify path containment | `test_path_traversal_is_blocked` |
| `PROP_NEWS` had no data source | A documented rule that never fired | Economic calendar + `--news` | `test_guard_blocks_inside_a_news_window` |
| Slippage studied only on synthetic quotes | Cost model disconnected from reality | Tick capture + replay through the same engine | `test_tick_replay_drives_the_production_engine` |
| LLM reviewer documented but unwired — and broken | Two years of "AI layer" that never ran | Wired into the entry path, fail-open, advisory by default, `str.format` bug fixed | `tests/test_ai.py` |

---

## Phase 3 — the gaps that only show up when you leave it running (v2.3.0)

| Gap | Why it mattered | Fix | Test |
| --- | --- | --- | --- |
| An accepted-but-unfilled order was forgotten | It can fill hours later, unmanaged, next to a second entry on the same symbol | `PendingBook` + `order_status`/`cancel`; late fills become managed positions, stale ones are cancelled | `test_late_fill_becomes_a_managed_position` |
| Local-only stops died with the process | Deriv cannot modify a contract, so a crash leaves the broker's original stop and no watchdog | Heartbeat file + `apex watchdog`; a stale heartbeat writes a flatten request honoured *before* adoption | `test_engine_honours_a_flatten_request` |
| Halts and breaches reached only sqlite | The desk stops and nobody knows | `apex/alerts.py`: webhook/SMTP, per-kind throttle, never blocks | `test_webhook_posts_json_and_never_raises` |
| Demo validation was a human checklist | "Validated" was an opinion | `apex verify` grades fills, slippage bias, P&L, rejections, reconciliation, stops, pending age, heartbeat | `tests/test_ops.py::TestVerify` |
| `close()` was all-or-nothing | No scale-outs: winners were exited whole or not at all | `volume=` through port/router/engine; Deriv refuses partials instead of pretending | `test_partial_close_keeps_the_position_with_less_lots` |
| Only position *counts* were capped | Four 4% trades passed the same check as four 0.5% trades | `max_heat_pct`, `max_group_heat_pct` in money | `test_heat_cap_blocks_the_fifth_overlapping_risk` |
| One global session window | FX, metals and indices do not share hours | `session_hours` per symbol or correlation group, global as fallback | `test_symbol_session_overrides_the_global_window` |
| Journal grew without bound | A per-second equity table nobody prunes becomes an unbackupable file | `apex prune`, `--db-retention-days`, `VACUUM`; trades are kept | `test_prune_keeps_trades_and_drops_high_frequency_rows` |
| Two more silent feed failures | A live socket can still starve bars or report a non-UTC clock | `FeedMonitor`: gap size in bars, drift with the offset | `test_gap_in_bars_is_detected` |

Two defects were found *by* this work, both of the "alarm fatigue" kind. Both were only half-fixed
on the first pass, which is itself the lesson: a fix that stores a field the caller never populates
looks green and changes nothing.

* **Reconciliation compared the wrong things.** `router.reconcile` keyed on the broker *label*, so
  every paper position was reported as both `missing_at_broker` and `unknown_at_engine`. Fixing the
  key exposed the data underneath it: `DerivBroker.positions()` reported `lots` as
  `contract["buy_price"]`, i.e. a USD **stake** treated as a lot count, and labelled every contract
  `BUY` including `MULTDOWN`. Keys are now `(symbol, ticket)`; size is compared exactly for venues
  that report lots and within 2% where the venue only implies them (`lots_estimated`). A
  reconciliation alarm that always fires is worse than none — it teaches the operator to ignore the
  one that matters.
* **The journal had no requested price, because the order had none.** `Store.record_order` wrote
  `order.price`; the engine never set it. That made the audit's "prices agree" step unanswerable
  *and* meant live Deriv submission could never succeed, since the adapter refuses an order without
  an entry price. Orders now carry the expected fill price (the touch on the traded side), the
  journal stores it in a migrated `orders.requested_price` column, and `apex verify` measures
  slippage against it directly.

---

## What was deliberately **not** built

* A second "research" strategy path (that is how the original divergence started).
* Claims of profitability or a pass guarantee.
* Auto-deployment of a live account without an explicit flag, credential and demo validation.
* A cloud dependency for the dashboard, journal or data path.
