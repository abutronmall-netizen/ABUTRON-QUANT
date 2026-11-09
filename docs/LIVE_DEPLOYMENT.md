# Live deployment

Nothing in this repository has transmitted a real order. This document is the exact path from
"runs on my laptop" to "a demo account confirms fills", and the gates to pass before live money.

---

## 0. Readiness

```bash
python3 -m apex doctor
python3 -m unittest discover -s tests
```

`doctor` prints a matrix: config, Python, Deriv token, Deriv adapter, MT5 package, MT5 credentials,
journal writability, engine self-test, prop profiles. It must be green for the broker you intend
to use. MT5 will show `N/A` on Linux — that is expected, not a failure.

Placement:

* **Deriv**: any host with outbound WebSocket access to `wss://ws.derivws.com`.
* **FP Markets MT5**: a **Windows** host (VM/server) running the terminal. The Python package
  binds to a running terminal; it is not a remote protocol. Do not put the terminal on a laptop
  that sleeps.

---

## 1. Credentials

Copy `.env.example` to `.env`, fill it, and export it in the environment that runs the engine:

```bash
set -a; . ./.env; set +a
```

Secrets are read only from the environment. They never appear in config files, logs, journal or
the dashboard.

### Deriv

1. Log into Deriv → **Settings → API token**.
2. Create a token with `read` + `trade`. Use a **demo** account first.
3. Export `DERIV_TOKEN`. Keep the default `DERIV_APP_ID=1089`; register your own app ID for
   production.
4. Multiplier contracts require a multiplier-enabled account. If proposals are rejected, check the
   account type before debugging code.

### FP Markets MT5

1. Install the terminal, log into the **demo** account, enable **Algo Trading** in the toolbar.
2. `pip install MetaTrader5` (Windows only).
3. Export `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER` (e.g. `FP-Markets-Demo`). `MT5_PATH` is only
   needed if the terminal is not already running.
4. Confirm the symbols exist: brokers suffix instruments (`EURUSD.m`, `XAUUSD.a`). The adapter
   resolves a base name to the first matching symbol; pin exact names in config to be safe.
5. Note hedging vs netting: on a netting account, one position per symbol is a hard constraint —
   the engine enforces one anyway, so behaviour is identical.

---

## 2. Dry run (no orders leave the process)

```bash
python3 -m apex serve --mode deriv --config config/deriv_prop.json --prop ftmo_2step
python3 -m apex serve --mode mt5   --config config/mt5_fpmarkets.json
```

With `live_orders: false` (default), the adapters simulate fills. Market data is real; orders are
not transmitted. Use this to verify:

* symbols resolve and ticks arrive (dashboard "Decision engine" prices move);
* regime and agent votes make sense for the session;
* rejections are explainable (`NO_STRUCTURE`, `SPREAD_TOO_WIDE`, `PROP_DAY_BUDGET`, …);
* the prop panel shows the right floors for your profile.

---

## 3. Demo validation — the gate that the original platform never passed

Run with `--live` **on a demo account** for at least a week and compare against the broker.

```bash
python3 -m apex serve --mode deriv --config config/deriv_prop.json --live
```

What to verify, in order:

1. **Fills exist.** Dashboard router stats show `submitted` = `filled`, and the broker's own
   history shows the same orders.
2. **Prices agree.** `runs/deriv/apex.db` → `orders.filled_price` vs broker statement. Expect
   small slippage, not systematic differences. A consistent bias means the price basis differs
   (bid/ask vs mid, or a symbol alias).
3. **P&L agrees.** `trades.pnl` must reconcile with the broker within commission. Deriv contract
   P&L is authoritative: if they disagree, the adapter's `lots → stake` conversion is wrong for
   your multiplier tier — calibrate it before going further.
4. **Rejections are handled.** Force one: request an absurd lot size on demo. Confirm the ack is
   logged and the engine does not retry forever.
5. **Reconciliation is clean.** No `missing_at_broker` / `unknown_at_engine` events. If they
   appear, stop and investigate; that is the exact class of bug the audit found in the runtime.
6. **Kill switch works.** `POST /api/control/kill` → positions flat, new orders blocked,
   `re-arm` restores. Test it before you need it.

Only after all six: set `live_orders: true` and keep `--live`. Start at half the planned risk for
three trading days.

---

## 4. Operations

### Start (systemd example)

```ini
[Unit]
Description=ABUTRON QUANT
After=network-online.target

[Service]
WorkingDirectory=/opt/abutron-quant
EnvironmentFile=/opt/abutron-quant/.env
ExecStart=/usr/bin/python3 -m apex serve --mode deriv --config config/deriv_prop.json --live
Restart=always
RestartSec=10
User=abutron

[Install]
WantedBy=multi-user.target
```

Bind the dashboard to localhost behind a reverse proxy with authentication. It can flatten the
book; it is an operator console, not a public page.

### Monitoring (minimum viable)

* daily: `equity` ticks present, `orders` all acked, no `reconcile` events;
* daily: prop panel — daily loss used < 40%, total room > 60%;
* weekly: attribution by strategy/symbol/exit reason in the dashboard or `apex report`.

### Incident response

| Symptom | First action |
| --- | --- |
| Unexpected position at broker | `/api/control/kill` (flattens), then reconcile |
| Repeated `EXEC_REJECT` | check `orders.reason`; spread/session/margin are the usual causes |
| Risk engine `HALTED` | read `halt_kind`: `daily`/`streak` clear next day, `drawdown` needs a human |
| Prop `AT_RISK` | stop trading; the guard already blocks at 80% of the daily budget |
| Feed silent > 2 bars | restart; the engine will not trade stale data beyond its warmup checks |

---

## 5. Known limits of the adapters

* **Deriv**: SL/TP are attached at proposal time where the account type supports it; the adapter
  does not implement contract modification, so exits are managed with an early sell. Contract P&L
  must be reconciled from the portfolio endpoint.
* **MT5**: `order_send` filling mode differs by symbol; `IOC` is the default and is configurable
  (`filling_type`). Partial fills are acknowledged by volume but not split into multiple engine
  positions.
* Neither adapter retries indefinitely: retryable retcodes are attempted twice, then surfaced.

## 6. Cost calibration

The defaults are conservative guesses. After a demo week, compute your real cost and set it:

```python
# per side, in bps of price, from your statement
spread_bps = mean(your_fill_price − mid_at_signal) / price * 1e4
```

Pass it to the backtester (`BacktestRequest(spread_bps=...)`, `slippage_bps=...`) and re-run
`sweep` and `propsim`. A strategy that only works at zero cost is not a strategy.
