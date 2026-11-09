# Changelog

All notable changes to this repository. Versions are internal milestones, not
released packages.

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
