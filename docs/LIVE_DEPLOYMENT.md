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

* symbols resolve and ticks arrive (Overview portal: the decision-engine prices move);
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

Item 1-5 are read from the journal automatically:

```bash
python3 -m apex verify --run runs/deriv --live
```

It reports PASS/FAIL per item — fills accounted for, systematic slippage bias, P&L vs the equity
curve, unknown rejection codes, reconciliation discrepancies, unprotected stops, stale pending
orders and heartbeat freshness. Exit code 1 means a blocker; `--strict` also fails on warnings,
`--json` is for CI. It cannot test the kill switch (that is item 6, do it by hand) or judge fills
it has never seen — it grades the evidence, not the market.

Only after all six: set `live_orders: true` and keep `--live`. Start at half the planned risk for
three trading days.

---

## 3b. Restart and crash safety (verify before going live)

A desk is judged by what happens at 03:00 when the process dies with a position open.

1. **Open a demo position**, then `kill -9` the engine.
2. Restart it. The log must show `boot: N adopted, 0 flattened` and the dashboard must list the
   position with its stop. If it lists nothing, the position is unmanaged — stop and investigate.
3. **Restart twice.** The second boot must adopt nothing new (`adopt` is idempotent).
4. **Check the daily budget survived.** After a loss-halt day, restart: `risk.halt_kind` must still
   be set and `day_trades` must not reset to 0.
5. **Kill the network** (unplug / firewall the host). Within ~90 s the log must show
   `feed socket died`, then `reconnect in 2s`, and after restoration `indicators resynced`. A feed
   that stays alive but starves you is a different failure: `FeedMonitor` warns with the gap in
   bars (`3.0-bar gap in the feed`), and a venue clock that is not UTC warns with the offset.
6. **Kill the process, not the host.** `kill -9` the engine while a position is open, then start the
   watchdog (`python3 -m apex watchdog --heartbeat runs/live/heartbeat.json --once`). Within 30 s it
   must report `stale` and write a flatten request. Start the engine again: it must log
   `watchdog flatten request honoured at boot` and come up **flat**, with risk halted. Release with
   `POST /api/control/rearm` when you are satisfied.
7. `--boot-policy flatten` is the alternative policy: on start it closes everything the broker
   holds. Use it when you would rather be flat than adopted.

```bash
# verify all of the above without a broker
python3 -m unittest tests.test_live -v
```

## 3c. Operator access

* Bind to `127.0.0.1` (default) and put a reverse proxy with authentication in front.
* Set `APEX_DASHBOARD_TOKEN` (or `--token`). Every POST (`kill`, `rearm`, `flatten`) then requires
  `X-Apex-Token`; the portals ask for it once (the **Token** button in the navbar) and keep it in
  that browser only. **Without a token the control endpoints answer a loopback client only** — if
  you bind `--host 0.0.0.0` with no token, `apex serve` prints a warning, and a remote POST is
  refused with 401 rather than flattening the book. The System portal shows the same
  `apex verify` checklist without a shell.
* Cross-origin POSTs are rejected outright, so a malicious page cannot flatten your book.
* The static file handler refuses any path that resolves outside `apex/api/static`.

## 3d. Calendar, ticks and server time

```bash
# blackout windows from a JSON calendar (schema in apex/news.py, sample in config/)
python3 -m apex news --file data/calendar.json --days 3
python3 -m apex serve --mode mt5 --config config/mt5_fpmarkets.json --news data/calendar.json

# record real ticks for cost research, then replay them
python3 -m apex serve --mode deriv --record-ticks data/ticks.csv
python3 -m apex backtest --symbols R_75 --ticks data/ticks.csv
```

MT5 timestamps arrive in **broker server time**. The adapter detects the offset from a live tick
(snapped to 15 minutes, ignoring stale weekend ticks) and converts everything to UTC, so session
windows and the prop day boundary are correct. Pin it if you prefer:
`"mt5_server_offset_hours": 2` in config. Verify on the dashboard: a tick must never appear in the
future, and the prop day must reset at your firm's hour, not eight hours early.

## 3e. Alerting and the watchdog

An engine that halts at 02:00 and tells nobody is a closed shop.

```bash
python3 -m apex serve --mode deriv --config config/deriv_prop.json --live \
  --heartbeat runs/live/heartbeat.json --alert-webhook "$APEX_ALERT_WEBHOOK"

# second process, own service unit (deploy/apex-watchdog.service)
python3 -m apex watchdog --heartbeat runs/live/heartbeat.json \
  --stale 30 --interval 5 --webhook "$APEX_ALERT_WEBHOOK"
```

Channels are configured in the JSON config (webhook and/or SMTP):

```json
"alerts": {
  "webhook_url": "",
  "smtp_host": "", "smtp_port": 587,
  "smtp_user": "", "smtp_password": "",
  "smtp_to": ["desk@example.com"],
  "min_interval_s": 60
}
```

Kinds that alert: `risk`, `reconcile`, `error`, `control`, `breach`, `watchdog`. Sends are
throttled per kind, timeout quickly and never raise into the trading loop.

The watchdog is the only component that can act on a dead engine. It **never opens a trade**: on a
stale heartbeat it alerts, writes a flatten request, and — only when started with `--live` and a
broker mode — closes what the broker actually holds. A restart after a crash therefore cannot
silently inherit unprotected positions: the request is honoured *before* adoption.

## 4. Operations

### Start (systemd unit is in the repo)

```ini
# deploy/apex.service ships with the repo; this is the essential part
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

Bind the dashboard to localhost behind a reverse proxy with authentication, and set
`APEX_DASHBOARD_TOKEN`. It can flatten the book; it is an operator console, not a public page.

Container alternative:

```bash
docker build -t abutron-quant .
docker run --rm -p 127.0.0.1:8000:8000 -v $PWD/runs:/app/runs \
  -e APEX_DASHBOARD_TOKEN="$APEX_DASHBOARD_TOKEN" abutron-quant
```

The image runs as a non-root user, keeps `runs/` on a volume (so a restart keeps the journal and
therefore the risk state) and defaults to paper mode.

### Monitoring (minimum viable)

* daily: `equity` ticks present, `orders` all acked, no `reconcile` events;
* daily: prop panel — daily loss used < 40%, total room > 60%;
* weekly: attribution by strategy/symbol/exit reason on the **Journal** portal, or `apex report`;
  re-read **Research** after any parameter change and compare the same anchored seeds.

### Incident response

| Symptom | First action |
| --- | --- |
| Unexpected position at broker | `/api/control/kill` (flattens), then reconcile |
| Repeated `EXEC_REJECT` | check `orders.reason`; spread/session/margin are the usual causes |
| Risk engine `HALTED` | read `halt_kind`: `daily`/`streak` clear next day, `drawdown` needs a human |
| Prop `AT_RISK` | stop trading; the guard already blocks at 80% of the daily budget |
| Feed silent > 2 bars | you should see `feed socket died` + reconnect attempts in Events; if not, the supervisor is not running |
| `boot: 1 adopted` after restart | expected when a position was open; confirm its stop matches the broker |
| `broker rejected stop update` warning | Deriv multiplier contracts cannot be modified; the stop is enforced locally — keep the process supervised |

---

## 5. Known limits of the adapters

* **Deriv**: SL/TP are attached at proposal time where the account type supports it. Open
  contracts cannot be modified, so a tightened stop is tracked **locally** and exits are executed
  with an early sell; the engine warns when that happens, because a local stop is lost if the
  process dies (boot adoption and reconciliation are the mitigation). Contract P&L must be
  reconciled from the portfolio endpoint.
* **MT5**: `order_send` filling mode differs by symbol; `IOC` is the default and is configurable
  (`filling_type`). Partial fills are acknowledged by volume but not split into multiple engine
  positions.
* Neither adapter retries indefinitely: retryable retcodes are attempted twice, then surfaced.
* Partial fills are honoured (`Ack.state == "partial"`, risk recalculated on the dealt volume) and
  pending orders are never turned into positions (`TRADE_RETCODE_PLACED` → `state="pending"`).

## 6. Cost calibration

The defaults are conservative guesses. After a demo week, compute your real cost and set it:

```python
# per side, in bps of price, from your statement
spread_bps = mean(your_fill_price − mid_at_signal) / price * 1e4
```

Pass it to the backtester (`BacktestRequest(spread_bps=...)`, `slippage_bps=...`) and re-run
`sweep` and `propsim`. A strategy that only works at zero cost is not a strategy.
