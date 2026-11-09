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
#    → http://localhost:8000

# 2. check what is wired up on this machine
python3 -m apex doctor

# 3. backtest the production engine, prop rules attached
python3 -m apex backtest --symbols R_25,R_50,R_75 --bars 4000 --prop ftmo_2step
#    → runs/bt_<ts>/report.md  (trades.csv, equity.csv, summary.json)

# 4. how often does a challenge survive? (many seeds, honest statistics)
python3 -m apex propsim --profile ftmo_2step --seeds 12 --bars 4000

# 5. record real ticks, then replay them (the only honest cost study)
python3 -m apex fetch --symbol R_75 --bars 5000
python3 -m apex serve --demo --record-ticks data/ticks.csv      # or a live mode
python3 -m apex backtest --symbols R_75 --ticks data/ticks.csv

# 6. tests
python3 -m unittest discover -s tests      # 169 tests
```

---

## Measured on this build

These numbers are reproducible with the commands shown and exist to calibrate the harness — they
are **not** a performance claim.

| Check | Command | Result |
| --- | --- | --- |
| Test suite | `python3 -m unittest discover -s tests` | **169 tests pass** in ~10 s |
| Random-walk tape (no edge exists) | `backtest --trend-strength 0.35`, 6 seeds | mean **avgR −0.004**, 3/6 seeds positive |
| Structured tape (edge exists) | `backtest --trend-strength 2.5`, 6 seeds | mean **avgR +0.187**, **6/6 seeds positive**, median maxDD 2.9% |
| Prop guard, daily-loss hole | `propsim --bars 8000` before/after the fix | breach rate **50% → 0%** |
| Prop pass rate (8% target, 28 synthetic days) | `propsim --profile abutron_inhouse --seeds 8` | pass 0%, breach 0%, median profit +1.6% |

The random-walk row is the important one: a system with look-ahead would look profitable there
too. It does not. The structured row shows the pipeline can capture real persistence when the tape
provides it. The prop rows show the guard prevents self-inflicted breaches while an 8% target
remains unreachable without a genuine edge — exactly the honest state of affairs.

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
| Dashboard with rejection reasons and prop progress | **Working** | `apex/api/` |
| Walk-forward sweep, prop pass-rate simulation | **Working** | `apex/research/optimize.py` |
| Restart safety: broker book adopted/flattened on boot | **Working** | `engine.boot_sync`, `tests/test_live.py` |
| Durable risk/prop state across restarts | **Working** | journal `runtime_state`, `engine.export_state` |
| Supervised Deriv socket: staleness → reconnect → resync | **Working** | `FeedSupervisor` in `apex/live.py` |
| MT5 broker-server-time → UTC conversion | **Working** | `MT5Connection.detect_offset`, `tests/test_mt5_live.py` |
| Economic-calendar blackout windows | **Working** (you supply the file) | `apex/news.py`; `apex news --file ...` |
| Tick capture + tick-level replay | **Working** | `--record-ticks`, `backtest --ticks` |
| Partial fills and pending orders | **Working** | `Ack.state`, `tests/test_mt5_live.py` |
| Operator-token auth + traversal-safe static serving | **Working** | `tests/test_api.py` |

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
| `apex serve --demo` | paper engine + dashboard (default preview) |
| `apex serve --mode deriv\|mt5` | live-data engine, dry-run unless `--live`; `--token`, `--news`, `--boot-policy` |
| `apex backtest` | event-driven backtest on the production engine |
| `apex sweep` | parameter sweep with walk-forward reporting |
| `apex propsim` | challenge pass-rate simulation over many seeds |
| `apex profiles` | list prop rule sets and their implied risk |
| `apex doctor` | readiness matrix: credentials, adapters, journal, engine self-test |
| `apex fetch` | download candles from Deriv **or** FP Markets MT5 to `data/<SYM>_<TF>.csv`; backtests use them automatically |
| `apex news --file cal.json` | inspect an economic calendar and the blackout windows it implies |
| `apex report --run runs/bt_x` | summarise a run |
| `apex ai` | check the optional local LLM reviewer |

`--prop none` disables the guard; `--prop config/my_firm.json` loads your own terms.

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
  risk.py            sizing and capital protection
  prop.py            prop-firm rule guard, pacing, phases
  news.py            economic calendar → prop blackout windows
  store.py           sqlite journal + attribution metrics + durable state
  backtest.py        event-driven runner (same engine)
  live.py            feeds → engine runner, boot sync, socket supervision
  data/              deriv (ws), mt5, synthetic/replay feed, ws client
  execution/         port interface, sim broker, deriv, mt5, router
  research/          sweep + prop pass-rate simulation
  ai/reviewer.py     optional Ollama reviewer (advisory or veto)
  api/               dashboard server + static UI
  cli.py             command line
docs/                architecture, audit response, prop playbook, deployment, research
deploy/              systemd unit; Dockerfile at the repo root
tests/               166 unit + integration tests
```

---

## Running it unattended

Four things decide whether a desk can be left alone overnight. All four are implemented and tested.

| Risk | Behaviour |
| --- | --- |
| Process restarts with live positions | `boot_sync` reads the broker book and **adopts** it (default; `--boot-policy flatten` to close instead), then reconciles every 30 s. Adopted positions keep their stops and are managed normally. |
| Restart refunds the daily budget | Risk and prop state are journalled to `runtime_state` and restored on start: halt kind, day trades, day P&L, prop phase and day counters survive the crash. |
| The socket dies silently | A Deriv feed with no message for 90 s is *dead*, not quiet: backoff 2→4→…→60 s, resubscribe, resync `warmup_bars` of candles, then carry on. Events are logged. |
| Anyone can flatten the book | Control endpoints require `X-Apex-Token` when `--token`/`APEX_DASHBOARD_TOKEN` is set, cross-origin POSTs are refused, the server binds `127.0.0.1` by default, and static paths cannot escape the asset directory. |

```bash
python3 -m apex serve --mode deriv --config config/deriv_prop.json \
  --token "$APEX_DASHBOARD_TOKEN" --news config/news_sample.json --record-ticks data/ticks.csv
```

Deployment artefacts: `Dockerfile` (dependency-free, non-root, healthcheck) and
`deploy/apex.service` (systemd, `Restart=always`, state restored from the journal).

---

## What is *not* claimed

* **No profitability claim.** Backtests here run on a synthetic tape. They measure system
  behaviour and cost sensitivity, not future returns. With zero real edge, a scalper loses to
  spread and slippage — the harness shows that plainly instead of hiding it.
* **No prop-pass guarantee.** `propsim` reports a pass *rate* under variation because a single
  passing backtest is noise.
* **No live validation yet.** Deriv and MT5 adapters are implemented to spec and tested against
  fakes. Real fills, real rejections and real contract P&L must be confirmed on demo accounts.
* **No HFT.** This is minute-scale discretionary-quality automation, not colocated market making.

Before risking money: read [`docs/LIVE_DEPLOYMENT.md`](docs/LIVE_DEPLOYMENT.md) and calibrate the
cost model against your own broker statements. Trading leveraged products can lose more than your
deposit. This software is provided as engineering, not as financial advice.
