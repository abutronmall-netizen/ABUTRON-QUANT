# Changelog

All notable changes to this repository. Versions are internal milestones, not
released packages.

## 2.6.0 — several venues on one engine, and a live adapter that has touched a socket

Asked for: multi-broker, multi-symbol, multi-trade execution, and a fix for the biggest item on
the honest-gaps list — that the Deriv order path had only ever run against a fake client object.

**Multi-venue (`--venue deriv --venue mt5`).** `LiveConfig.mode` selected exactly one real venue, so
"multi-broker" meant paper only. Now the runner takes a `venues` list: each venue gets its own feed
loop in its own thread (Deriv asyncio, MT5 polling), and they share the engine, the broker router,
the risk budget, the prop guard, the kill switch and the journal. `--max-open N` is the ceiling
across every venue, because heat is a property of the desk and not of a broker.

Rules enforced at startup rather than documented and hoped for:

* **one symbol, one broker** — the engine keeps one state per symbol; the same name on two venues
  raised nothing before and would have fused two instruments into one indicator/position state.
  It now refuses with the clashing symbols named;
* **each venue owns its universe** — `--symbols` used to default to the Deriv volatility list for
  every broker, so `--mode mt5` armed the terminal with `R_10..R_100`, which it cannot quote. The
  default is now per broker (Deriv: 5 indices, MT5: 13 FX/metals/indices), per-venue overrides are
  `--symbols deriv:R_10,R_25 mt5:EURUSD,USDJPY`, and MT5 refuses synthetics by name with a message
  that says what to do;
* `--venue` with `--demo` is a contradiction and exits 2;
* a venue whose loop dies is journalled and leaves the other venue running;
* in paper mode each book now gets its own account id (`PAPER-DERIV`, `PAPER-MT5`) so a fill can be
  attributed to the book it came from.

**`apex rehearse` — the live adapter, over a socket, with `dry_run=False`.** `apex/rehearsal.py`
runs a loopback WebSocket venue that answers the documented Deriv v3 subset (authorize, balance,
ticks, ticks_history, proposal, buy, portfolio, proposal_open_contract, sell), then drives the
production `DerivBroker` across it: handshake → authorize → balance → tick subscription → `proposal`
→ `buy` → `portfolio` → `sell`, asserting the SL/TP fields on the proposal and the adapter's
documented refusal to modify a multiplier contract. Report at `runs/rehearsal/rehearsal.json`.

It proves framing, request correlation, the submit path, portfolio → Position mapping and the close
path — none of which a fake client object exercises. It does **not** prove that Deriv behaves like
this server, and the report's `caveat` field says that. `apex rehearse --venue mt5` exits 2 with the
reason (the adapter needs a Windows terminal) rather than pretending.

**A shutdown race found by the new tests.** `LiveRunner.shutdown()` closed the journal underneath a
running venue loop; every clean stop could raise `sqlite3.ProgrammingError: Cannot operate on a
closed database` in the loop thread. Shutdown now joins the loop thread first.

Dashboard: a **Books** card on System (one row per book: venue, paper/live, account, symbol count,
universe), and `venues`/`books` in `/api/state` and the heartbeat file.

Suite 309 → 324 tests. The rehearsal runs in ~0.1 s and is now a CI step.

## 2.5.2 — a test that only passed where the work had been done

With the dependency guard fixed, CI reached the test step for the first time
since v2.2 and failed in all three Pythons while the same 309 tests were green
on my machine. The cause was mine, from 2.5.0:
`test_slippage_endpoint_always_answers` asserted that `/api/slippage` always
carries an `overall` key, which held only because `runs/live/apex.db` existed on
my machine from the dashboard I had been testing against. On a clean checkout
there is no journal, the endpoint correctly answers `{"verdict": "no data"}`,
and the key was missing.

The endpoint's shape is now the guarantee rather than an accident of state:
`study()` always returns `overall`, `by_symbol`, `by_broker`, `by_side` and
`recent`, including on the no-journal, unreadable-journal and error paths, and
the API test asserts the shape plus `overall.n == fills` and a known verdict
instead of a hard-coded key list.

Reproduced and confirmed the way CI sees it — a fresh `git clone` of the branch
with no `runs/`, no `data/` and no `backups/` — then ran every CI step in that
clone: doctor, the reference backtest, backup, restore, slippage, verify, prune
and propsim all exit 0. The next push is the first real test of those steps in
nine releases; if one of them breaks, this changelog was wrong.

## 2.5.1 — the CI guard that was failing itself

GitHub Actions has been red on every push since v2.2 and I only read the
`package` job, which passes. The failing step was the dependency guard:

```sh
test ! -s requirements.txt
```

`requirements.txt` is a deliberate comment-only placeholder explaining that the
stack is stdlib-only. `-s` asks "is the file longer than zero bytes?", so a
321-byte file of comments counted as a third-party dependency and the job died
before lint, tests, doctor, backtest, backup, slippage or verify ever ran — on
all three Pythons, on every branch, for nine pushes.

The guard now judges what the file *says*, not how large it is: comments and
blank lines are allowed, any real requirement line fails the job and is printed.
Verified three ways locally: comment-only passes, missing file passes, a
`requests>=2` line fails and echoes the line.

Nothing else changes. The engine, journal, portals and the 2.5.0 study are
untouched; 309 tests still pass and lint is still clean.

## 2.5.0 — measure what the venue charges, and make the evidence recoverable

Two of the four items `docs/RESEARCH.md` §7 listed as "deliberately not
implemented" were code-only gaps, not research dreams. They are implemented.

**The slippage study (`apex/slippage.py`, `apex slippage`).** The journal has
carried `requested_price` since 2.3.1 and `filled_price` since the first release;
nobody had turned the pair into the number that decides whether a scalper is
viable. It now reports:
- adverse fill bps per fill (positive = worse), with **mean, median, IQR, worst**
  and the share of fills worse than the backtest's 0.3 bps model;
- the same broken down by instrument, broker and side;
- **cost in R**: `bps / 10_000 × price / stop_distance`, summed across the journal,
  because a third of an R per trade is what kills a 1.8R-target scalper;
- an explicit verdict — `inside model`, `under-modelled`, `insufficient sample`
  (fewer than 5 fills is noise, and the report says so rather than printing a
  confident median over three rows).

The journal is opened `mode=ro`: grading a live journal can never create a table,
migrate a column or checkpoint a WAL out from under the engine. `apex verify`'s
`prices` check now reports the median and the R cost alongside the mean, and the
Research portal carries a "fill slippage vs the cost model" card with the recent
fills next to it.

**Backup and restore (`apex/backup.py`, `apex backup` / `apex restore`).** The
desk could prune its journal but not snapshot it, and every "we have a copy"
story ends the same way: a `cp apex.db` taken mid-write, with a stale `-wal`
beside it.
- `create_backup` uses `sqlite3.Connection.backup()` — consistent while the engine
  writes — flips the copy to `journal_mode=DELETE` so it is one self-contained
  file, and writes a manifest with row counts, size and sha256.
- `restore_backup` verifies the sha256, refuses to overwrite an existing journal
  without `--force`, and deletes stale `-wal`/`-shm` beside the target first (a
  restored main file next to an old WAL quietly keeps the rows you replaced).
- retention: `--keep N` prunes the oldest snapshots with their manifests.

**The SMTP path had no test.** `tests/test_slippage.py::TestMailTransport` now
covers delivery (host, STARTTLS, login, subject) and a server that refuses the
connection (returns False, never raises). It shipped untested because "fail-open"
made it invisible; it is still fail-open, now on purpose.

Tests: 309 (was 290).

## 2.4.1 — the numbers an operator reads, and the paths that produced them

A pass over everything the 2.4.0 portals put in front of a person, plus the
command paths behind them. No strategy behaviour changed; every fix here is
either a lie removed from the screen or a dead end removed from the shell.

**The journal size was understated.** `Store.size_bytes()` looked at the main
sqlite file only. In WAL mode that is a fraction of the footprint: the dashboard
reported 0.95 MB for a 5 MB journal, and `apex prune --no-vacuum` printed
`1.28 MB -> 2.47 MB` — it appeared to *grow* the file while deleting 4 000 rows,
because the deletes sat in the write-ahead log. Now:
- `size_bytes()` sums main + `-wal` + `-shm`; `size_parts()` exposes the split and
  the Journal portal shows it.
- `prune()` ends with `PRAGMA wal_checkpoint(TRUNCATE)`, so deleted rows actually
  leave the disk; the CLI measures the file after the connection closes.

**A crashing handler looked like a dead server.** Any exception in `do_GET`
dropped the connection with no response. It now answers `500` with
`{"error": "TypeError: …"}`; `do_POST` is wrapped the same way, so a failed
flatten tells you why.

**`/api/trades` ignored `?limit=`.** The blotter asked for 500 and got whatever
the server felt like (200). Now the parameter is honoured and clamped to
1…1000.

**Control without a token was open to the network.** With no `--token` the kill
and flatten endpoints answered anyone who could reach the port; the code relied
on the operator binding to localhost. `control_authorized()` now requires a token
*or* a loopback client, and `apex serve` prints an explicit warning when it binds
to a public interface without one.

**The snapshot iterated live dictionaries.** `engine.snapshot()` walked
`positions`/`states`/`pending` directly while the trading loop mutated them — the
classic "dictionary changed size during iteration" waiting for a busy session.
It copies the books first.

**The dashboard re-wrote the journal on every tick.** `live.snapshot()` called
`store.metrics()` and `equity_curve()` on every poll (2 s per open tab, walking
every trade and equity row). The journal view is now cached for 5 s and survives
a locked database.

**Command-line dead ends.**
- `--broker` now follows `--mode`: `serve --mode mt5` used to build an engine
  around the *Deriv* symbol book while the route carried the MT5 one — a desk
  that looked connected and could never trade.
- `--mode mt5` on a host without the MetaTrader5 package exits 2 with the fix in
  the message instead of starting a silent, data-less desk.
- a missing `--config` prints `[!] config not found: …` and exits 2 (it was an
  uncaught traceback).
- `apex fetch` reports `Deriv unavailable: …` and exits 2 instead of dumping an
  SSL traceback; an empty candle response is an error, not a 0-bar CSV.
- `--anchor-ts` is threaded into `sweep` and `propsim`, which previously ignored
  it and always used the built-in anchor.

**Portals that stay live.** The trades blotter was loaded once and never
refreshed (10 s now), the verify checklist ditto (30 s), the symbol filter
survived neither a repaint (it does now), a slow snapshot could stack requests
(guarded), the nav marks the current page with `aria-current`, and the Journal
page shows main-file versus WAL instead of one misleading number.

**The wheel was shipping an unstyled dashboard.** `package-data` listed
`static/*`, which does not match `static/vendor/bootstrap/*`: a `pip install`
would have produced portals with no CSS and no navbar behaviour. The patterns are
explicit now and the CI package job asserts the five required assets are inside
the built wheel.

**Hygiene.** `ruff check apex tests` now passes, and CI runs it: the lint contract
is `F,E9,B,I` (undefined names, syntax, bugbear, import order). The previous
`pyproject.toml` also selected `E,W,UP`, which the code never satisfied, so the
config read like a guarantee it was not; `[tool.ruff.lint]` now carries a comment
explaining why the `UP` rewrite (`Optional[X]` → `X | None` in 200+ signatures) is
a separate change. Fixes in this pass: an unused `Ack` import that left a type
annotation as a string, dead locals in the agent/regime adapters that looked like
live logic, `zip(strict=)` where lengths must match, and the unused imports.

Tests: 290 (was 276).

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
