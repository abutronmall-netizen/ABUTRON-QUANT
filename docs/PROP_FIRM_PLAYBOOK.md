# Prop-firm account passing system

> A challenge is not a trading problem. It is a **constraint satisfaction** problem: reach a
> profit target before breaching a daily or total loss limit, while respecting minimum days, a
> consistency rule, news windows and flat rules. The best signal on earth fails if it is sized
> like a retail account.

This system does not promise a pass. It guarantees three things instead:

1. you cannot breach a limit **because of a sizing error** — the guard blocks before the loss;
2. every rule is encoded once, in data, and applied identically to backtest and live;
3. you can measure a rule set's survivability across many market paths before paying a fee.

---

## The rules implemented

| Rule | Field | Behaviour |
| --- | --- | --- |
| Profit target | `profit_target_pct` | phase passes at target **and** `min_trading_days`; consistency must also hold |
| Max daily loss | `max_daily_loss_pct` | measured from day-start equity at the firm's reset hour; hard-stop new risk at 80% of the budget |
| Max total loss | `max_total_loss_pct` + `drawdown_type` | `static` (from initial balance) or `trailing` (from equity high-water mark) |
| Minimum trading days | `min_trading_days` | a target hit before this freezes the account in `TARGET_HIT`, does not pass it |
| Calendar limit | `max_calendar_days` | breach on timeout, as firms do |
| Consistency | `consistency_pct` | best day's profit ≤ X% of total profit; also blocks adding risk to a day that already dominates |
| Stop-loss mandatory | `stop_loss_required` | entry rejected without a bracket |
| News blackout | `news_blackout_minutes` | entry rejected inside the window |
| Weekend / EOD flat | `weekend_flat`, `daily_reset_hour_utc` | force-flatten before the reset boundary and into the Friday close |
| Lot cap | `max_lots` | **exposure-scaled** (see below) |
| Trades per day | `max_trades_per_day` | overtrading is the most common self-inflicted breach |

Built-in profiles: `ftmo_2step`, `fundednext_starter`, `the5ers_high_stakes`, `abutron_inhouse`
(conservative, used by the demo), plus multi-phase `ftmo_phases()` (evaluation → verification →
funded). Any of them can be overridden by a JSON file:

```bash
python3 -m apex backtest --prop config/prop_ftmo_custom.json --bars 4000
```

Always re-read your firm's current terms: profiles are data from publicly documented rules and
drift over time.

---

## The maths that decides most challenges

### 1. Daily-loss budget

```
day_loss_limit   = day_start_equity × max_daily_loss_pct / 100
remaining_today  = equity − daily_floor
per_lot_risk     = |entry − stop| × value_per_point_per_lot
```

Three constraints are applied before an order can pass:

* **hard stop** — if ≥ 80% of the day's budget is consumed, no new risk today;
* **single-trade cap** — `lots ≤ 0.35 × remaining_today / per_lot_risk`, so one stop-out cannot
  eat the day;
* **book cap** — `open_risk + new_risk ≤ 0.60 × remaining_today`.

Without the per-trade cap, six losses at 0.4% risk breach a 4% daily limit in an afternoon. This
is exactly what happened in an early build of this repo until the guard was corrected — the
`propsim` breach rate went from 50% to 0%.

### 2. Risk per trade

`plan_for(profile, equity)` chooses:

```
risk_pct = min(max_risk_per_trade_pct,
               max_daily_loss_pct / 6,
               max_total_loss_pct / 12)
```

Six consecutive losses use the whole daily budget; twelve use the whole account. Anything more
aggressive is arithmetic denial.

### 3. Pacing

```
remaining_target = target − profit
daily_target     = remaining_target / days_left
multiplier       = clip(1 + 0.6 × (behind / daily_target − 0.5), 0.6, 1.6)
risk_pct         = min(max_risk, profile_risk × multiplier)
```

Being behind schedule nudges risk up — bounded by the firm's cap and never above 75% of the
daily-loss budget. Being ahead does not increase risk; near the target, size is cut to 40%
("capital preservation mode").

### 4. Consistency

If today's profit is already ≥ `consistency_pct` of total profit, new entries are blocked for the
rest of the day. Firms that use this rule fail accounts that pass on one heroic day.

### 5. Exposure-scaled lot caps

A "2 lot" cap means two standard FX lots of exposure. Deriv synthetics quote 1 lot as $1 per index
point, so a raw cap would shrink a Deriv position ~30× and quietly convert 0.4% risk into 0.005%.
The cap is therefore scaled: `cap = max_lots × max(1, 100_000 / notional_per_lot)`.

---

## Measuring survivability instead of hoping

```bash
python3 -m apex propsim --profile ftmo_2step --seeds 12 --bars 4000
```

Output: pass rate, breach rate, median profit, median drawdown, and every seed's outcome with the
breach reason. `runs/propsim/propsim_<profile>.json` holds the detail.

Reading it correctly:

* **Breach rate is the number that matters most.** A 0% breach rate at a 0% pass rate means the
  guard is doing its job and the *strategy* has no edge — a signal problem, not a risk problem.
* A high pass rate on synthetic data with strong trend persistence is **not** evidence about real
  markets. It shows the pipeline can capture a real edge when one exists.
* Run `propsim` after every parameter change. If pass rate is unchanged but breach rate rises, the
  change made the system more fragile.

## Before you buy a challenge — checklist

- [ ] `apex doctor` is green for the adapter you will trade.
- [ ] The strategy has been run on **your** broker's data with **your** spread and commission, not
      synthetic fills.
- [ ] `propsim` shows a breach rate near zero across at least 10 seeds.
- [ ] Daily loss limit in config ≤ the firm's, and `risk_off_after_daily_loss_pct` ≤ half of it.
- [ ] News calendar loaded as `block_news_windows` (or you accept gap risk).
- [ ] Weekend/EOD flat verified with the broker's server time, not your local clock.
- [ ] Kill switch tested: `POST /api/control/kill` flattens and blocks.
- [ ] You accept that the target is ~8–10% and the failure mode is a single bad day.

## Operating a live challenge

1. Start in `dry_run` on the demo, run for a week, compare journalled fills with the broker
   statement line by line.
2. Switch to live with `--live` and `live_orders: true`. Start at half the planned risk for the
   first three trading days.
3. Watch three dashboard panels: **daily loss used**, **total loss room**, **open risk at stop**.
   If any is above 60%, reduce risk; do not "trade back" a loss.
4. On a daily halt, the engine flattens and stops for the day. That is the system working.
   Do not re-arm to recover.
5. When the target is hit, the guard stops opening risk and waits for `min_trading_days`.

## Hard truths

* Passing requires a genuine edge. The guard prevents self-inflicted failures; it cannot create
  expectancy. On a random tape this system loses to costs, and the harness shows it.
* More trades means more exposure to costs. For a 1.0 ATR stop with a ~2 bps round trip, scalping
  needs a hit-rate above ~40% just to break even.
* The most common real-world breach is not a stop-out — it is a cluster of correlated positions
  during a news spike. Limits, correlation groups and news windows exist for that reason.
