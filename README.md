# ABUTRON QUANT

**One decision engine. Two brokers. One set of prop-firm rules.**

A quantitative scalping desk for **Deriv synthetic indices** and **FP Markets MT5**, with a
market regime engine, multi-agent signal arbitration, a hard risk governor, and a prop-firm
rule guard that sizes, blocks and flattens according to a challenge's actual terms.

This repository is a rebuild, not a patch. The audit of ABUTRON APEX described a platform with
working market data and seven MT5 workers but **no completed signal-to-trade cycle** — staged
agents that never ran, a Deriv-only router, and zero attributable broker P&L. Everything here
is organised around closing that loop and making it observable.

```
                    ┌──────────────────────────────────────────────────────────┐
  Deriv WS ────────►│                                                          │
  (V10 … Boom/Crash)│   bars → indicators → regime → agent votes → conviction   │
                    │                    │                                     │
  FP Markets MT5 ──►│                    ▼                                     │
  (FX, metals, idx) │   portfolio arbitration → risk sizing → prop guard        │
                    │                    │                                     │
  Synthetic feed ──►│                    ▼                                     │
  (tests / paper)   │   unified broker router (idempotent, retried, reconciled) │
                    │                    │                                     │
                    └────────────────────┼─────────────────────────────────────┘
                                         ▼
                   Deriv contracts · MT5 tickets · paper fills
                                         │
                                         ▼
              journal (sqlite) → attribution → reflection memory → dashboard
```

---

## Quickstart

No dependencies. Python 3.10+.

```bash
# 1. see the desk running with a synthetic tape and a paper broker
python3 -m apex serve --demo --port 8000 --preload 1200 --speed 0.8
#    → http://localhost:8000  — seven portals (Overview, Positions, Signals,
#      Risk & Prop, Journal, Research, System), Bootstrap 5, responsive

# 2. check what is wired up on this machine
python3 -m apex doctor

# 3. backtest the production engine on the whole volatility curve, prop rules attached
python3 -m apex backtest --bars 4000 --prop ftmo_2step
#    default book: R_10, R_25, R_50, R_75, R_100 (--symbols overrides it)
#    → runs/bt_<ts>/report.md  (trades.csv, equity.csv, summary.json)

# 4. how often does a challenge survive? (many seeds, honest statistics)
python3 -m apex propsim --profile ftmo_2step --seeds 12 --bars 4000

# 5. record real ticks, then replay them (the only honest cost study)
python3 -m apex fetch --symbol R_75 --bars 5000
python3 -m apex serve --demo --record-ticks data/ticks.csv      # or a live mode
python3 -m apex backtest --symbols R_75 --ticks data/ticks.csv

# 6. what the venue actually did to your fills (read-only, works on paper too)
python3 -m apex slippage --run runs/live
#    median vs the backtest's 0.3 bps model, IQR, and the cost in R per trade

# 7. snapshot the journal before you touch anything
python3 -m apex backup --run runs/live --out backups --keep 14
python3 -m apex restore --backup backups/apex-<stamp>.db --db runs/live/apex.db --force

# 8. tests
python3 -m unittest discover -s tests      # 309 tests
```

---

## The portals

`apex serve` is not one page with everything on it. It is seven, all served by the same stdlib
server, all reading the same snapshot, all built on **Bootstrap 5.3.3 vendored in the repository** —
no CDN, no build step, nothing fetched at runtime, because a desk that needs the internet to show
you its own risk is a desk you cannot trust on a bad connection.

| Portal | URL | The question it answers |
| --- | --- | --- |
| **Overview** | `/` | What is the desk doing *now*? KPIs, decision engine, prop verdict, live risk, equity, rejections. |
| **Positions** | `/positions` | What am I holding, at what price, with which stop — and which accepted orders have not filled? |
| **Signals** | `/signals` | Why did (or didn't) this instrument trade? Regime, agent votes, conviction, coded rejection log. |
| **Risk & Prop** | `/risk` | How much of the daily/drawdown/heat budget is gone, what the challenge demands, and the kill controls. |
| **Journal** | `/journal` | What actually happened over time: equity curve, Sharpe/Sortino/PF, attribution by strategy, symbol, broker, exit and regime. |
| **Research** | `/research` | What did the backtests, sweeps and prop simulations say? Artefacts from `runs/`, with the synthetic-tape caveat attached. |
| **System** | `/system` | Is the plumbing healthy? Router, accounts, feed gaps, clock drift, heartbeat, and the `apex verify` checklist. |

Design rules that decided the rewrite:

* **Mobile first.** Bootstrap's grid, the collapsing navbar and `table-responsive` wrapping mean the
  same markup works on a phone and on a 4K desk. Nothing is hover-only; nothing needs a wide screen
  to be legible.
* **Numbers are honest or they are absent.** Missing data renders as `—`, never as `0`. Session-scoped
  metrics and journal-wide metrics are shown side by side and labelled, because averaging across
  runs is how a dashboard lies.
* **Control is separated from observation.** Reads (`/api/state`, `/api/trades`, `/api/verify`,
  `/api/research`, `/api/stream`) need nothing. Kill, flatten and re-arm need the operator token
  (`--token`), are same-origin only, and are journalled as control events. The browser keeps the
  token in `localStorage` and nothing else.
* **No hidden arithmetic.** The dashboard renders engine decisions; it never recomputes a P&L, a
  size, or a regime of its own.

Pages are plain HTML over the API: `/api/state` every two seconds (or `/api/stream` for SSE),
`/api/trades` for the blotter, `/api/verify` for the checklist, `/api/research` for `runs/`.

---

## Measured on this build

These numbers are reproducible with the commands shown and exist to calibrate the harness — they
are **not** a performance claim.

| Check | Command | Result |
| --- | --- | --- |
| Test suite | `python3 -m unittest discover -s tests` | **309 tests pass** in ~18 s |
| Lint contract | `ruff check apex tests` (F,E9,B,I; CI runs it) | clean |
| Same seed, same answer | `backtest --bars 3000 --seed 7`, twice | identical metrics (fixed tape anchor, `backtest.ANCHOR_TS`) |
| Default book, seed 7 | `backtest --bars 3000 --seed 7` | 48 trades, win 22.9%, P&L **−667.94**, PF 0.49, avgR −0.29, maxDD 8.23% → governor halts |
| Middle three, seed 7 | `backtest --symbols R_25,R_50,R_75 --bars 3000 --seed 7` | 59 trades, win 27.1%, P&L **−655.93**, PF 0.60, avgR −0.23, maxDD 8.15% |
| Near random walk, 6 seeds | `backtest --bars 4000 --trend-strength 0.35 --seed 1..6` | mean avgR **+0.06**, median **−0.12**, **2/6 seeds positive** |
| Structured tape, 6 seeds | same at `--trend-strength 2.5` | mean avgR **+0.05**, median **+0.04**, 4/6 seeds positive |
| Prop survivability (8% target) | `propsim --profile abutron_inhouse --bars 1500 --seeds 6` | pass **0%**, breach **16.7%**, median profit −1.5%, median DD 3.2% |

Read the two backtest rows first: **the default book loses money on the synthetic tape.** That is the
honest headline, and it is why this project ships a hard risk governor instead of a returns chart.
The seed-dispersion rows are the second lesson — one seed at 0.35 reaches +1.09R while the median is
−0.12R, so anyone quoting a single backtest is quoting a coin flip. The prop row says the same from
the other side: 0% pass with a 16.7% breach rate means the guard is the only thing keeping the
account intact.

Every run above starts from a fixed tape anchor (`--anchor-ts`, default `backtest.ANCHOR_TS`) rather
than the wall clock, so the numbers reproduce on another day. Before 2.4.0 the same seed produced a
different tape every minute — which is how a stale "reference" run can look healthy while the code
that produced it has moved on.

---

## What is actually working

| Capability | State | Evidence |
| --- | --- | --- |
| Regime detection, 4-agent voting, conviction scoring | **Working** | `apex/regime.py`, `apex/agents.py`; tests |
| Portfolio arbitration + adaptive agent weights | **Working** | `apex/portfolio.py`; weights update on realised R |
| ATR-based entries, bracket, trailing, time stops | **Working** | `apex/strategy.py`, engine tests |
| Fixed-fractional sizing, daily/streak/drawdown halts | **Working** | `apex/risk.py`; `tests/test_risk.py` |
| Prop-firm rule guard (8 rule classes, pacing, phases) | **Working** | `apex/prop.py`; `tests/test_prop.py` |
| Unified router: idempotency, retry, kill switch, reconciliation | **Working** | `apex/execution/router.py`; `tests/test_execution.py` |
| Deriv market data + contract execution | **Implemented to API spec, unverified live** | `apex/data/deriv.py`, `apex/execution/deriv.py`; protocol tests against a fake socket |
| FP Markets MT5 data + execution | **Implemented to API spec, unverified live** | `apex/data/mt5.py`, `apex/execution/mt5.py`; tests against a fake terminal |
| Journal + full attribution | **Working** | `apex/store.py`; dashboard, reports |
| Order prices journalled (`requested_price`) and reconciled by `(symbol, ticket)` | **Working** | `orders` table, `router.reconcile`; `tests/test_reconciliation` |
| Dashboard with rejection reasons and prop progress | **Working** | `apex/api/` |
| Walk-forward sweep, prop pass-rate simulation | **Working** | `apex/research/optimize.py` |
| One symbol universe: R_10…R_100 by default, per-broker books | **Working** | `apex/symbols.py`; `tests/test_symbols.py` |
| Reproducible research tape (fixed anchor, seeded) | **Working** | `BacktestRequest.anchor_ts`, `--anchor-ts`; `tests/test_symbols.py` |
| Seven Bootstrap portals, offline assets, mobile-first | **Working** | `apex/api/static/` (vendored BS 5.3.3); `tests/test_api.py` |
| Read-only `/api/verify`, `/api/research`, `/api/slippage` endpoints | **Working** | `apex/api/server.py`; `tests/test_api.py` |
| Live-vs-modelled slippage study (median, IQR, cost in R) | **Working** | `apex/slippage.py`, `apex slippage`, Research portal; `tests/test_slippage.py` |
| Journal backup with a verifiable manifest, and restore | **Working** | `apex/backup.py`, `apex backup/restore`; `tests/test_slippage.py` |
| Journal size tells the truth (main + WAL) and prune truncates the WAL | **Working** | `Store.size_parts`, `Store.prune`; `tests/test_ops.py` |
| Restart safety: broker book adopted/flattened on boot | **Working** | `engine.boot_sync`, `tests/test_live.py` |
| Durable risk/prop state across restarts | **Working** | journal `runtime_state`, `engine.export_state` |
| Supervised Deriv socket: staleness → reconnect → resync | **Working** | `FeedSupervisor` in `apex/live.py` |
| MT5 broker-server-time → UTC conversion | **Working** | `MT5Connection.detect_offset`, `tests/test_mt5_live.py` |
| Economic-calendar blackout windows | **Working** (you supply the file) | `apex/news.py`; `apex news --file ...` |
| Tick capture + tick-level replay | **Working** | `--record-ticks`, `backtest --ticks` |
| Partial fills and pending orders | **Working** | `Ack.state`, `tests/test_mt5_live.py` |
| Operator-token auth + traversal-safe static serving | **Working** | `tests/test_api.py` |
| Alerting: webhook/SMTP, throttled per kind, never blocks trading | **Working** | `apex/alerts.py`; `tests/test_ops.py` |
| Out-of-process watchdog: stale heartbeat → alert → flatten request | **Working** | `apex/watchdog.py`, `apex watchdog`; `deploy/apex-watchdog.service` |
| Pending orders tracked, adopted on late fill, cancelled on expiry | **Working** | `apex/execution/pending.py`; `tests/test_ops.py` |
| `apex verify`: the demo checklist as a pass/fail command | **Working** | `apex/verify.py`; `tests/test_ops.py` |
| Partial closes / scale-outs (all-or-nothing before) | **Working** (Deriv refuses, by design) | `engine.close_position(volume=)`, `core.scale_out_r` |
| Aggregate heat caps in money (portfolio and correlated group) | **Working** | `risk.py` `max_heat_pct`, `max_group_heat_pct` |
| Per-instrument session windows | **Working** | `risk.py` `session_hours` |
| Bounded journal: retention + vacuum | **Working** | `apex prune`, `--db-retention-days` |
| Bar-gap and venue clock-drift detection | **Working** | `FeedMonitor` in `apex/live.py` |

**Unverified means unverified.** No order has been transmitted to Deriv or FP Markets from this
repository. Protocol and adapter logic are tested against fakes; a demo account must confirm
real fills before any live flag is used. See `docs/LIVE_DEPLOYMENT.md`.

---

## The three audits this addresses

| Audit priority | Original defect | How this repo answers it | Where |
| --- | --- | --- | --- |
| **P0-1** | APEX 1.1.0 staged, production ran an older scanner | One engine class, used by backtest, paper and live. No second strategy path exists. | `apex/engine.py` |
| **P0-2** | Signal path produced 0 qualified entries | Multi-timeframe agreement became a *score*, not an AND gate. Every rejection is coded and counted. | `apex/portfolio.py`, dashboard |
| **P0-3** | Native execution interface never completed | Order state machine with idempotent client IDs, retries, reconciliation, kill switch. | `apex/execution/router.py` |
| **P0-4** | FP Markets outside a Deriv-only router | Routing is by `(broker, account, symbol)`; both adapters implement one port. | `apex/execution/base.py` |
| **P1** | AI research disconnected from results | Outcomes feed `ReflectionMemory`; LLM reviewer is optional and can only reduce risk. | `apex/portfolio.py`, `apex/ai/reviewer.py` |
| **P1** | No strategy → broker P&L attribution | Every signal, order, fill and trade carries strategy/broker/account/instrument/regime. | `apex/store.py` |
| **P2** | Version sprawl | Single package, single config schema, one journal per run. | repo |

Full mapping: [`docs/AUDIT_RESPONSE.md`](docs/AUDIT_RESPONSE.md).

---

## The engine in one paragraph

Each closed M5 bar updates an incremental indicator set and three higher-timeframe aggregations.
A regime classifier labels the tape (trend, range, squeeze, expansion, chaos) from efficiency,
ADX, ATR percentile and EMA slope. Four directional agents (trend, mean-reversion, breakout,
micro-momentum) plus a volatility filter each return a signed vote with reasons;
`ReflectionMemory` supplies regime-specific weights learned from realised R-multiples. The votes
become a conviction score; a composition rule requires at least one *structural* agent to agree
so micro-momentum cannot originate a trade alone. The portfolio layer picks the best idea within
position, symbol and correlation limits. The risk engine sizes it at a fixed fraction of equity
against the stop distance, then the prop guard may shrink or block it using the challenge's
daily-loss budget, consistency rule, pacing and news windows. The router submits with an
idempotency key, retries transient rejections, and reconciles broker truth against internal
bookkeeping. Everything is journalled.

---

## Deriv

* Synthetics: V10/V25/V50/V75/V100, 1-second variants, Boom/Crash, Jump indices.
* Market data: `ticks_history` candles for warmup/backtests, live tick subscriptions for bars.
* Execution: multiplier contracts (`MULTUP`/`MULTDOWN`), stake derived from the engine's risk
  sizing, exits by early sell. Deriv contract P&L is authoritative and is reconciled from the
  portfolio endpoint, not reconstructed from CFD maths.
* The WebSocket client is ~150 lines of RFC-6455 over asyncio — no third-party runtime dependency
  on the live data path.

```bash
export DERIV_TOKEN=...          # demo account token to start
python3 -m apex serve --mode deriv --config config/deriv_prop.json --prop ftmo_2step
```

## FP Markets MT5

* `MetaTrader5` is Windows-only and talks to a running terminal. The adapter is isolated behind
  `ExecutionPort`; on Linux the rest of the system still runs with the paper broker or Deriv.
* Handles broker symbol suffixes (`EURUSD.m`), stop-level minimums, contract size, filling modes,
  retcodes (requote/price-changed are retryable; the rest are not), and hedging vs netting.
* `dry_run=True` by default: orders are simulated through the same pipeline until you pass
  `--live` **and** set `live_orders` in config.

```bash
python3 -m apex serve --mode mt5 --config config/mt5_fpmarkets.json
```

## Prop-firm account passing system

Not a promise to pass a challenge — a **rule-compliant risk governor** plus the instrumentation
to find out, cheaply and honestly, whether a rule set is survivable.

* Eight rule classes enforced *before* the loss: profit target, max daily loss (from the correct
  day-start anchor), max total loss (static or trailing), minimum trading days, calendar limit,
  consistency rule, news blackout, weekend/EOD flat.
* Daily-loss budgeting: a single stop-out may consume at most 35% of the remaining daily budget;
  total open risk is capped at 60%; new risk is blocked at 80% of the limit.
* Pacing: computes the profit still required per remaining day and scales risk within the firm's
  `max_risk_per_trade_pct`.
* Lot caps are **exposure-scaled**, so a "2 lot" limit means the same exposure on EURUSD and on
  Deriv R_75 instead of silently shrinking one to nothing.
* Multi-phase orchestration: evaluation → verification → funded, with balance rollover.
* `propsim` reports pass/breach/timeout rates across seeds, plus the median drawdown.

```bash
python3 -m apex profiles
python3 -m apex propsim --profile fundednext_starter --seeds 12 --bars 4000
python3 -m apex backtest --prop config/prop_ftmo_custom.json --bars 4000
```

Details and the maths: [`docs/PROP_FIRM_PLAYBOOK.md`](docs/PROP_FIRM_PLAYBOOK.md).

---

## Commands

| Command | Purpose |
| --- | --- |
| `apex serve --demo` | paper engine + seven portals on `:8000` (see [The portals](#the-portals)) |
| `apex serve --mode deriv\|mt5` | live-data engine, dry-run unless `--live`; `--token`, `--news`, `--boot-policy` |
| `apex backtest` | event-driven backtest on the production engine |
| `apex sweep` | parameter sweep with walk-forward reporting |
| `apex propsim` | challenge pass-rate simulation over many seeds |
| `apex profiles` | list prop rule sets and their implied risk |
| `apex doctor` | readiness matrix: credentials, adapters, journal, engine self-test |
| `apex fetch` | download candles from Deriv **or** FP Markets MT5 to `data/<SYM>_<TF>.csv`; backtests use them automatically |
| `apex news --file cal.json` | inspect an economic calendar and the blackout windows it implies |
| `apex report --run runs/bt_x` | summarise a run |
| `apex ai --test` | check the optional local LLM reviewer (wired into entries, advisory by default) |
| `apex verify --run runs/live` | grade a journal against the demo checklist; `--live` judges slippage strictly |
| `apex slippage --run runs/live` | measured adverse fill bps vs the backtest model, with the cost in R |
| `apex backup --run runs/live --out backups` | consistent journal snapshot + sha256 manifest, retention |
| `apex restore --backup <file> --db <path> --force` | restore a snapshot (refuses a tampered or clobbered target) |
| `apex watchdog` | separate process: watch the heartbeat, alert, flatten if the engine dies |
| `apex prune --db runs/live/apex.db --days 14` | bound journal growth (keeps trades) |

`--prop none` disables the guard; `--prop config/my_firm.json` loads your own terms.
`--symbols` accepts `R_10,R_50` or `R10 R50` and defaults to the five volatility indices;
`--anchor-ts <epoch>` moves the synthetic tape anchor when you deliberately want a different one.

## Configuration

Precedence: CLI flags → environment → JSON config → defaults. Examples in `config/`.
Secrets only ever come from the environment (`.env.example`).

## Repository layout

```
apex/
  engine.py          the single decision loop
  regime.py          regime classification
  agents.py          trend / mean-reversion / breakout / momentum / vol filter
  portfolio.py       conviction, arbitration, ReflectionMemory
  strategy.py        entries, brackets, trailing, time stops
  risk.py            sizing, heat caps, sessions, capital protection
  prop.py            prop-firm rule guard, pacing, phases
  news.py            economic calendar → prop blackout windows
  store.py           sqlite journal + attribution metrics + durable state + retention
  backtest.py        event-driven runner (same engine)
  live.py            feeds → engine runner, boot sync, socket supervision, feed health
  alerts.py          webhook/SMTP notifier (throttled, never blocks)
  watchdog.py        heartbeat monitor; flatten request the engine honours
  verify.py          the deployment checklist, mechanically
  symbols.py         the traded universe: R_10-R_100 and per-broker books
  timeutil.py        the one definition of a timeframe
  data/              deriv (ws), mt5, synthetic/replay feed, ws client
  execution/         port interface, sim broker, deriv, mt5, router
  research/          sweep + prop pass-rate simulation
  ai/reviewer.py     optional Ollama reviewer (advisory or veto, always fail-open)
  api/               dashboard server; seven Bootstrap portals + vendored assets
  cli.py             command line
docs/                architecture, audit response, prop playbook, deployment, research
.github/workflows/   CI: tests on 3.10-3.12, doctor, artifact + packaging checks
deploy/              systemd unit; Dockerfile at the repo root
tests/               309 unit + integration tests
```

---

## Running it unattended

These are the restart and supervision paths. Six things decide whether a desk can be left
alone overnight; all six are implemented and tested.

| Risk | Behaviour |
| --- | --- |
| Process restarts with live positions | `boot_sync` reads the broker book and **adopts** it (default; `--boot-policy flatten` to close instead), then reconciles every 30 s. Adopted positions keep their stops and are managed normally. |
| Restart refunds the daily budget | Risk and prop state are journalled to `runtime_state` and restored on start: halt kind, day trades, day P&L, prop phase and day counters survive the crash. |
| The socket dies silently | A Deriv feed with no message for 90 s is *dead*, not quiet: backoff 2→4→…→60 s, resubscribe, resync `warmup_bars` of candles, then carry on. Events are logged. |
| Anyone can flatten the book | Control endpoints require `X-Apex-Token` when `--token`/`APEX_DASHBOARD_TOKEN` is set; **without** a token they answer a loopback client only, cross-origin POSTs are refused, `apex serve` warns loudly if it is bound to a public interface with no token, and static paths cannot escape the asset directory. |

```bash
python3 -m apex serve --mode deriv --config config/deriv_prop.json \
  --token "$APEX_DASHBOARD_TOKEN" --news config/news_sample.json --record-ticks data/ticks.csv
```

Deployment artefacts: `Dockerfile` (dependency-free, non-root, healthcheck) and
`deploy/apex.service` (systemd, `Restart=always`, state restored from the journal).

---

## When nobody is watching

Alerts, the watchdog and `apex verify` exist because the failures that end desks
are silent ones.

```bash
# the engine writes a heartbeat every second; point a second process at it
python3 -m apex serve --mode deriv --config config/deriv_prop.json \
  --token "$APEX_DASHBOARD_TOKEN" --alert-webhook "$APEX_ALERT_WEBHOOK"

python3 -m apex watchdog --heartbeat runs/live/heartbeat.json \
  --webhook "$APEX_ALERT_WEBHOOK"          # alert-only
python3 -m apex watchdog --heartbeat runs/live/heartbeat.json --mode mt5 --live
                                          # may also flatten at the broker

# grade a demo week before risking anything
python3 -m apex verify --run runs/live --live --json
```

| Situation | What happens |
| --- | --- |
| Risk halt, prop breach, kill switch, reconciliation drift, feed death, reconnect | written to the journal **and** pushed to your webhook/mail, throttled per kind |
| Engine process dies with positions open | watchdog alerts after 30 s, writes a flatten request; the next boot flattens instead of adopting |
| Broker accepts an order but does not fill it | tracked with its own expiry, polled at the venue, adopted on late fill, cancelled when stale |
| Aggregate open risk creeps up | `max_heat_pct` / `max_group_heat_pct` refuse the entry; position counts alone cannot see this |
| A bar or two never arrives | `FeedMonitor` warns once per episode with the gap size in bars |
| Venue clock is not UTC | warned with the offset; stops and session windows are UTC-based |
| The journal grows without limit | `apex prune` / `--db-retention-days` keep trades, drop per-second equity rows, vacuum |

---

## Optional AI review

`apex serve --ai advisory` (or `veto`) sends each **already-sized** entry to a local model
through Ollama. It sees the candidate, the sizing and the votes — never a blank slate — so it can
only reduce risk, not create ideas. Failures fail open, and in `advisory` mode the engine will not
let a reviewer block a trade even if it says so. Off by default: `apex ai --test` checks whether a
model is reachable.

---

## What is *not* claimed

* **No profitability claim.** Backtests here run on a synthetic tape. They measure system
  behaviour and cost sensitivity, not future returns. With zero real edge, a scalper loses to
  spread and slippage — the harness shows that plainly instead of hiding it.
* **No prop-pass guarantee.** `propsim` reports a pass *rate* under variation because a single
  passing backtest is noise.
* **No live validation yet.** Deriv and MT5 adapters are implemented to spec and tested against
  fakes. Real fills, real rejections and real contract P&L must be confirmed on demo accounts —
  `apex verify --live` is the command that grades that week, but it reads a journal; it cannot
  certify slippage you have not experienced.
* **No HFT.** This is minute-scale discretionary-quality automation, not colocated market making.

Before risking money: read [`docs/LIVE_DEPLOYMENT.md`](docs/LIVE_DEPLOYMENT.md) and calibrate the
cost model against your own broker statements. Trading leveraged products can lose more than your
deposit. This software is provided as engineering, not as financial advice.
