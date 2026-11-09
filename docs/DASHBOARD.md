# The portals

`python3 -m apex serve` serves seven pages from one stdlib HTTP server. They are views over the
engine's own snapshot: there is no second strategy path, no client-side P&L arithmetic, and no
JavaScript dependency that needs a network.

```bash
python3 -m apex serve --demo --port 8000 --token apex-demo
#   http://localhost:8000            Overview
#   http://localhost:8000/positions  Positions
#   … /signals  /risk  /journal  /research  /system
```

---

## Page by page

### Overview — `/`
The first screen answers "is the desk behaving?" Six KPIs (equity, day P&L, open risk at stop,
closed trades, average R / profit factor, prop progress), then the decision engine table
(price, regime, M5/M15/H1 trend, conviction, top agent votes, current state), the prop challenge
progress, the aggregate heat bar, the open book, the ranked rejection reasons, the equity canvas,
the last trades, the event log and the router counters.

### Positions — `/positions`
Open positions with entry, stop, target, unrealised P&L and age; **pending orders** (accepted, not
filled — with expiry, so nothing is silently forgotten); exposure per instrument with margin and
free margin; and the closed-trade blotter with a symbol filter.

### Signals — `/signals`
One card per instrument: regime label, conviction bar, trend agreement across timeframes, ATR/RSI/ADX,
and the individual agent votes as signed bars. Below: every coded rejection for this run with counts,
and the recent signal log (time, side, conviction, decision, reason).

This is the page that answers "why didn't it trade?" — with a code, not a shrug.

### Risk & Prop — `/risk`
Daily-loss consumption, drawdown against the cap, heat against the money cap, loss-streak cooldown,
session windows, the prop challenge table (progress, daily floor, total floor, trading days,
consistency, pacing) and the control buttons.

### Journal — `/journal`
The sqlite journal, not the session: equity curve, Sharpe/Sortino/profit factor/expectancy/max DD,
session-versus-journal side by side (never averaged), attribution by strategy, symbol, broker, exit
reason and regime, journal size, and the maintenance command to prune it.

### Research — `/research`
Backtests, parameter sweeps and prop simulations found under `runs/`, with the synthetic-tape caveat
printed **above** the numbers and the exact command needed to reproduce the latest run. A negative
result is displayed as a negative result.

### System — `/system`
Deployment state: `apex verify` checklist (fills, prices, P&L, rejections, reconciliation, kill
switch, stops, pending orders, heartbeat), safety flags (boot policy, persistence, reconciliation
age, local-only stops, news blackout), broker router, accounts, instrument states, and the operator
token / control buttons.

---

## Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/state` | none | full snapshot, `Cache-Control: no-store` |
| GET | `/api/trades` | none | closed-trade blotter (last 200) |
| GET | `/api/health` | none | `{ok, uptime_s, version}` |
| GET | `/api/verify` | none | `apex verify` checklist against the runner journal, cached 30 s |
| GET | `/api/research` | none | `runs/**/summary.json`, `sweep.json`, `propsim_*.json`; read-only, size-capped |
| GET | `/api/stream` | none | server-sent snapshot, 2 s |
| POST | `/api/control/kill` | token | flatten + block new orders |
| POST | `/api/control/flatten` | token | close every position at the touch |
| POST | `/api/control/rearm` | token | release the kill switch |

The token comes from `--token` or `APEX_DASHBOARD_TOKEN` and is sent as `X-Apex-Token: …` (a bearer
token is also accepted). Cross-origin POSTs are refused.

With **no** token configured, `control_authorized()` allows a loopback client only
(`127.0.0.1`/`::1`): binding to `0.0.0.0` without a token leaves the panel observable but makes
its buttons refuse to act, and `apex serve` says so at startup. `GET /api/trades` honours
`?limit=` (1…1000). A handler that raises answers `500` with `{"error": …}` instead of dropping the
connection, so a broken snapshot reads as an error and not as a dead server.

---

## Design constraints

* **Offline assets.** Bootstrap 5.3.3 is vendored in `apex/api/static/vendor/bootstrap/`. The test
  suite fails if any page references `http://`, `https://`, a CDN or an SRI attribute; a preview
  iframe with no egress and a VPS with a dead uplink must both render the risk page.
* **Mobile first.** Collapsing navbar, `table-responsive` wrappers, `col-6 col-md-4 col-xl-2` KPIs,
  canvas that redraws on resize. No hover-only affordance.
* **Honest blanks.** Missing values render as `—`. Unknown never becomes `0.00`.
* **One snapshot.** Every page renders from `GET /api/state` (plus `/api/trades`, `/api/verify`,
  `/api/research` where relevant) and re-paints on a 2 s timer. Full repaint beats incremental
  patching: stale rows are a class of bug, not a feature.
* **Controls are explicit.** Kill and flatten ask for confirmation, then report the server's answer
  as a toast. The server journals every control action as an event.

---

## Adding a page

1. Add the route to `PAGES` in `apex/api/server.py` (`"mypage": "mypage.html"`).
2. Copy any existing HTML shell (they differ only by `data-page`, title and the navbar's active
   link) and add the page to the navbar generator used to produce them.
3. Add `pageMypage(state)` to `apex/api/static/portal.js` and register it in `PAGES`.
4. If the page needs data the snapshot does not carry, add it to the snapshot (engine or
   `live.snapshot()`), not to the page. The dashboard must never compute what the engine decided.
5. Add the route to `tests/test_api.py::test_every_portal_is_served_and_bootstrap_based`.
