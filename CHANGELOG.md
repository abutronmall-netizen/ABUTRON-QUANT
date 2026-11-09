# Changelog

All notable changes to this repository. Versions are internal milestones, not
released packages.

## 2.4.0 — the portals, the whole volatility curve, and a tape that repeats

Three things a desk needs before it can be believed: one place to look, one list of instruments,
and numbers that do not change when you re-run them.

**Every portal is now a Bootstrap page (`apex serve`).** The single hand-rolled page is gone.
Seven pages — Overview, Positions, Signals, Risk & Prop, Journal, Research, System — share one
theme and one renderer (`apex/api/static/portal.js`), served on clean URLs (`/`, `/positions`,
`/signals`, `/risk`, `/journal`, `/research`, `/system`).
- Bootstrap **5.3.3 is vendored** at `apex/api/static/vendor/bootstrap/` (css 233 kB, bundle js
  81 kB, MIT licence). No CDN, no build step, no network call: the dashboard must work in an
  offline preview iframe and on a desk with a dead uplink.
- Mobile-first: collapsing navbar, `table-responsive` tables, fluid KPI grid, canvas equity that
  redraws on resize. The same markup is legible on a phone.
- Every previous panel is preserved — decision engine, prop challenge, open positions, coded
  rejections, risk governor, router, closed trades, event log, equity — and the two missing
  portals are added: **Journal** (equity curve, Sharpe/Sortino/PF, attribution by strategy,
  symbol, broker, exit reason and regime, session-vs-journal side by side) and **Research**
  (backtest runs, sweeps, prop simulations read from `runs/`, with the synthetic-tape caveat
  printed above the numbers).
- Control stays separated from observation: kill/flatten/re-arm need `X-Apex-Token` and a
  same-origin POST; the token lives in `localStorage` and is asked for once.

**New read-only endpoints for the pages.** `/api/verify` runs the deployment checklist against the
runner's own journal (cached 30 s) instead of making the operator run a CLI command; `/api/research`
lists `runs/**/summary.json`, `sweep.json` and `propsim_*.json` (nested one level, size-capped,
never executing anything). `/api/health` now reports `__version__` rather than a hardcoded string.

**The traded universe lives in one place (`apex/symbols.py`).** `R_10` and `R_100` were tradeable
in the backtester but absent from every default, so the desk's default book was the middle of the
curve — the hardest subset to trade, chosen by accident.
- `VOLATILITY_INDICES = [R_10, R_25, R_50, R_75, R_100]`; `DEFAULT_SYMBOLS` is all five.
- `DEFAULT_SYMBOLS_BY_BROKER` is used by `LiveConfig`, `DEFAULT_CONFIG`, `BacktestRequest`,
  `SweepRequest` and the CLI, so live, backtest, sweep and prop-sim trade the same book.
- `parse_symbols()` accepts `R_10,R_50`, `R10 R50` or a list, de-duplicates and normalises the
  volatility spelling while leaving broker suffixes (`EURUSD.m`) untouched.
- Sample configs updated to the five-index book.

**A seeded run now repeats.** The synthetic tape used to start at `time.time()`, so `--seed 7`
produced a different tape every minute and a published reference run could not be checked.
`BacktestRequest.anchor_ts` (default `backtest.ANCHOR_TS`) fixes the tape; `--anchor-ts` overrides
it. Tick replay instead starts its clock at the first recorded tick, so a 2026 recording is not
judged against a 2023 session clock (`_clock_start`). Two test regressions cover both.

**Snapshot fields the portals need.** `risk.snapshot(positions)` now also returns heat in money and
percent, the session windows, open-position count, and `max_open_positions` /
`cooldown_after_loss_s` / `cooldown_remaining_s`; `live.snapshot()` adds `equity_history`,
`db_path` and `heartbeat_age_s`.

**Published numbers refreshed, honestly.** With the anchored tape the default five-index book
returns 48 trades / −6.7% / PF 0.49 / maxDD 8.23% over 3000 M5 bars, and the middle-three book
59 trades / −6.6% / PF 0.60. The previous "structured tape: 6/6 seeds positive" claim no longer
holds (4/6, mean avgR +0.05) and has been removed rather than left standing. `propsim` at
`abutron_inhouse` over 6 seeds: pass 0%, breach 16.7%.

Layout: `apex/api/static/` gains `portal.js` (renderer), `theme.css` (Bootstrap overrides) and seven
HTML shells; `app.js` and `style.css` are deleted; `apex/symbols.py` is new.

Tests: 276 (was 259). New coverage: symbol defaults across live/config/backtest/CLI/sweep, tape
determinism, tick-replay clock, every portal route, Bootstrap assets present and served, portals
free of network URLs, `/api/research` and `/api/verify` shapes.

## 2.3.1 — order prices and reconciliation keys, properly

Two defects from 2.3.0 were only half-fixed. Both are about the evidence an
operator is asked to trust.

**The requested price never existed.** `Store.record_order` was changed to write
`order.price` — but the engine never set a price on the order in the first place,
so every journal row still had `null`. Worse, `DerivBroker.submit` refuses an
order with no price ("entry price required for Deriv contracts"), so live Deriv
submission could not have worked at all. Now:
- `_try_enter` puts the expected fill price on the order: the touch on the traded
  side (ask for a buy, bid for a sell), with the decision mid kept in `meta`.
- `orders.requested_price` is a first-class column, migrated onto existing
  journals on open (`ALTER TABLE`, idempotent, rows preserved).
- `apex verify` reads that column first, so "prices agree" is answerable directly
  from SQL rather than reconstructed from a nearby signal row.

**Reconciliation had the right key and the wrong data behind it.** Keying on
`(symbol, ticket)` stopped the false alarms, but `DerivBroker.positions()` reported
`lots = float(contract["buy_price"])` — a *stake* treated as a lot count — so every
live Deriv position would have reported `lots_mismatch` instead. It now inverts
`_stake_for` (stake × multiplier / (value-per-point × spot)), flags the result
`lots_estimated`, and reconciliation compares estimated sizes to 2% instead of
exact. It also stops labelling every contract `BUY`: `MULTDOWN` is a sell.

Regression tests added for all of it: `tests/test_execution.py::TestReconciliation`
(paper labels, real size mismatch, estimated sizes, missing/unknown, ticket reuse
across symbols), `tests/test_store.py` (column + old-journal migration),
`tests/test_ops.py::TestOrderPriceOnTheWire`, `tests/test_protocol.py` (stake is
not lots, engine orders satisfy Deriv's price requirement).

Tests: 259.

## 2.3.0 — the desk can now look after itself

The remaining gaps from the audit list. Each item is behaviour that was missing,
not a refactor.

**Alerting (`apex/alerts.py`)**
- Webhook (urllib) and SMTP notifiers, per-kind throttle, never raises and never
  blocks a bar. Risk halts, prop breaches, kill switch, reconciliation drift,
  feed death and watchdog decisions reach a human instead of only a database.

**Watchdog (`apex/watchdog.py`, `apex watchdog`)**
- Separate process reading `runs/live/heartbeat.json`. On a stale heartbeat it
  alerts, writes a `watchdog_flatten_request` the engine honours **before**
  adopting positions at boot, and (only with `--live`) flattens the broker book.
- `deploy/apex-watchdog.service` for systemd.

**Pending orders (`apex/execution/pending.py`)**
- An accepted-but-unfilled order is live risk: it is tracked, polled through
  `order_status`, adopted when it finally fills and cancelled when it expires.
  MT5 gained `order_status`/`cancel`; the book survives restarts.

**`apex verify` (`apex/verify.py`)**
- Automates `docs/LIVE_DEPLOYMENT.md` §3: fills accounted for, systematic
  slippage bias, P&L vs equity curve, rejection codes, reconciliation, kill
  switch, unprotected stops, pending age, heartbeat freshness. `--live` grades
  slippage/P&L strictly; paper mode warns instead.

**Partial closes and scale-outs**
- `close(..., volume=)` through the port, router and engine; a partial close
  realises P&L and keeps the position with proportionally less risk.
  `--scale-out-r/--scale-out-frac` via `EngineConfig`; Deriv refuses partials
  instead of pretending, because a multiplier contract is sold whole.

**Risk: heat in money, sessions per instrument**
- `max_heat_pct` and `max_group_heat_pct` cap aggregate open risk, which position
  counts cannot see. `session_hours` gives per-symbol/per-group trading windows
  (FX vs metals vs indices) with the global window as fallback.

**Operations**
- `apex prune` + `--db-retention-days`: bounded journal growth with vacuum;
  trades are kept, per-second equity/signal rows are pruned.
- `FeedMonitor`: bar-gap and venue-clock-drift warnings (the two silent failures
  a socket-alive heartbeat cannot see).
- Dead code removed: `duration_min`, `ExitReason.CONTRACT_EXPIRY`,
  `bar_seconds_override`; three copies of `_tf_seconds` became `apex/timeutil.py`.

**Defects found while building this**
- `BrokerRouter.reconcile` keyed on the broker *label*, so every paper position
  reported `missing_at_broker` + `unknown_at_engine`. Now keyed on
  `(symbol, ticket)`, with a new `lots_mismatch` check. A reconciliation alarm
  that always fires is a reconciliation alarm nobody reads.
- `Store.record_order` did not persist the requested price, which made slippage
  analysis impossible from the journal. It does now.

Tests: 259 (2.3.1 adds the regression tests for the two defects above).

## 2.2.0 — verified signal layer, AI review, CI

- Signal layer tests: bounded agent votes, warmup silence, conviction
  disagreement penalty, arbitration caps, reflection-weight adaptation.
- **Fixed** `apex/ai/reviewer.py`: the prompt contained literal JSON braces and
  went through `str.format`, so every review raised `KeyError` before a request
  left the process.
- The reviewer is now wired into the decision path (it previously was not, despite
  being documented): advisory by default, veto optional, always fail-open, and the
  engine itself refuses to let advisory mode block a trade.
- Research harness tests: grid determinism, copy-not-mutate profiles, scoring
  shape, sweep/propsim output contracts.
- `apex report` covered by a test; CI workflow for Python 3.10-3.12 with doctor,
  artifact and packaging checks.

Tests: 209.

## 2.1.0 — operational layer

Everything needed to leave the desk running without a human watching it.

**Restart safety**
- `TradingEngine.boot_sync()` adopts (default) or flattens the broker's book on
  start; adoption is idempotent and adopted positions keep their stops.
- Risk, prop and position state are journalled (`runtime_state`) and restored
  before the first bar, so a crash cannot refund the daily loss budget.
- One day boundary shared by risk and prop (`session_day`), honouring the firm's
  UTC reset hour.

**Connection supervision**
- `FeedSupervisor`: staleness detection, exponential backoff (2→60 s),
  resubscribe and indicator resync after every reconnect.

**Broker correctness**
- MT5 server time → UTC conversion (auto-detected, snapped, stale-tick aware).
- Partial fills scale the position and its risk; `PLACED` orders stay pending.
- Stops a venue cannot hold are enforced locally and flagged.
- `apex fetch --broker mt5` for FP Markets history.

**Data and research**
- Economic calendar (`apex/news.py`) driving `PROP_NEWS` windows.
- Tick capture (`--record-ticks`) and tick-level replay (`backtest --ticks`).

**Security**
- Operator token on control endpoints, cross-origin POST rejection, localhost
  bind by default, traversal-safe static handler (a real vulnerability found by
  tests before release).

**Optional AI review**
- The local LLM reviewer is now actually wired into the entry path, advisory by
  default, veto optional, and always fail-open. Fixed a `str.format` bug that
  made every review raise.
- In advisory mode the engine refuses to let a reviewer block an entry, even one
  that returns a veto.

**Deployment**
- `Dockerfile`, `.dockerignore`, `deploy/apex.service`, CI workflow.

**Tests: 207** (was 111 at 2.0.0).

## 2.0.0 — rebuild

Clean-room rebuild of the audited APEX deployment: one decision engine for
backtest/paper/live, conviction scoring instead of an AND gate, a unified
Deriv + FP Markets router with idempotency and reconciliation, and a prop-firm
guard with daily loss budgeting, pacing and multi-phase challenges.

Four real bugs were found and fixed during the rebuild (lot-cap risk reporting,
Deriv attribute shadowing, tick-subscription routing, runner shutdown), each with
a regression test.
