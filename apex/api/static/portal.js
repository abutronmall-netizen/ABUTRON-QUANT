/* ABUTRON QUANT — portals.
 *
 * One file, six pages, no build step and no external JavaScript: Bootstrap's
 * bundle provides the navbar/modal behaviour and everything else here talks to
 * the same JSON API the engine writes. The pages are views over a single
 * snapshot; nothing is computed client-side that the engine did not already
 * decide (a dashboard that disagrees with the engine is worse than none).
 *
 * Rendering is a full re-paint of #page every couple of seconds. It is cheap
 * (one snapshot, a few hundred rows) and it removes a whole class of stale-row
 * bugs that incremental updates introduce.
 */
(() => {
  "use strict";

  /* ------------------------------------------------------------------ *
   * tiny DOM / format core
   * ------------------------------------------------------------------ */
  const $ = (sel, root = document) => root.querySelector(sel);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const isNum = (v) => v !== null && v !== undefined && v !== "" && !Number.isNaN(Number(v));
  const num = (v, dp = 2) => isNum(v)
    ? Number(v).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })
    : "—";
  const money = (v, ccy = "USD", dp = 2) => {
    if (!isNum(v)) return "—";
    try {
      return new Intl.NumberFormat(undefined, {
        style: "currency", currency: ccy || "USD",
        minimumFractionDigits: dp, maximumFractionDigits: dp,
      }).format(Number(v));
    } catch (_) { return num(v, dp) + " " + (ccy || ""); }
  };
  const pct = (v, dp = 2) => (isNum(v) ? num(v, dp) + "%" : "—");
  const signed = (v, dp = 2) => (isNum(v) ? (Number(v) > 0 ? "+" : "") + num(v, dp) : "—");
  const signCls = (v) => (!isNum(v) ? "" : Number(v) > 0 ? "good" : Number(v) < 0 ? "bad" : "muted");
  const ts2date = (ts) => new Date(Number(ts) * 1000);
  const dt = (ts) => (isNum(ts) ? ts2date(ts).toLocaleString([], {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  }) : "—");
  const clock = (ts) => (isNum(ts) ? ts2date(ts).toLocaleTimeString([], { hour12: false }) : "—");
  const ago = (ts) => {
    if (!isNum(ts)) return "—";
    const s = Math.max(0, Date.now() / 1000 - Number(ts));
    if (s < 90) return `${Math.round(s)}s ago`;
    if (s < 5400) return `${Math.round(s / 60)}m ago`;
    if (s < 172800) return `${(s / 3600).toFixed(1)}h ago`;
    return `${Math.round(s / 86400)}d ago`;
  };
  const dur = (s) => (!isNum(s) ? "—" : Number(s) < 90 ? `${Math.round(s)}s`
    : Number(s) < 5400 ? `${Math.round(Number(s) / 60)}m` : `${(Number(s) / 3600).toFixed(1)}h`);
  const pill = (text, kind = "") => `<span class="pill ${kind}">${esc(text)}</span>`;
  const bar = (fraction, kind = "", title = "") => {
    const pctW = Math.max(0, Math.min(100, Number(fraction || 0) * 100));
    return `<div class="aq-bar ${kind}" title="${esc(title)}"><span style="width:${pctW.toFixed(1)}%"></span></div>`;
  };
  const kpi = (label, value, sub = "", klass = "") => `
    <div class="col-6 col-md-4 col-xl-2">
      <div class="kpi"><span class="k">${esc(label)}</span>
        <b class="mono ${klass}">${value}</b>
        <em class="mono">${sub}</em></div>
    </div>`;
  const card = (title, bodyHtml, opts = {}) => `
    <div class="${opts.cols || "col-12 col-xl-6"}">
      <section class="card ${opts.cardClass || ""}">
        <header class="card-header d-flex justify-content-between align-items-center gap-2">
          <span>${esc(title)}</span><span class="d-flex gap-2 align-items-center">${opts.aside || ""}</span>
        </header>
        <div class="card-body ${opts.tight ? "tight" : ""}">${bodyHtml}</div>
      </section>
    </div>`;
  const table = (headers, rows, opts = {}) => {
    if (!rows.length) return `<p class="muted mb-0 px-2 py-3">${esc(opts.empty || "nothing here yet")}</p>`;
    const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
    const body = rows.map((r) => `<tr>${r.map((c) => `<td>${c ?? ""}</td>`).join("")}</tr>`).join("");
    return `<div class="table-responsive table-scroll ${opts.tall ? "tall" : ""}">
      <table class="table table-sm table-hover align-middle mb-0">
        <thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  };
  const regimeKind = (label) => ({
    trend_up: "good", trend_down: "bad", expansion: "good",
    squeeze: "warn", chaos: "bad", range: "",
  }[label] || "");
  const regimeName = (label) => String(label || "—").replace(/_/g, " ");
  const trendChip = (dir) => {
    const d = Number(dir || 0);
    if (d > 0) return pill("▲ up", "good");
    if (d < 0) return pill("▼ down", "bad");
    return pill("• flat");
  };
  const sidePill = (side) => (String(side).toUpperCase().startsWith("B")
    ? pill("BUY", "good") : pill("SELL", "bad"));
  const statusPill = (status) => {
    const st = String(status || "").toUpperCase();
    if (!st) return "";
    const kind = /ENTERED|PROFIT|TARGET|LIVE|OK|READY/.test(st) ? "good"
      : /PENDING|COOLDOWN|BLACKOUT|HEAT|LATE_FILL/.test(st) ? "warn"
      : /HALT|BREACH|KILL|REJECT|CANCEL|EXPIRED/.test(st) ? "bad" : "";
    return pill(st.replace(/_/g, " ").toLowerCase(), kind);
  };

  /* ------------------------------------------------------------------ *
   * API + operator token
   * ------------------------------------------------------------------ */
  const TOKEN_KEY = "apex.operator.token";
  const token = () => { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch (_) { return ""; } };
  const setToken = (v) => {
    try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); } catch (_) {}
  };

  async function getJSON(path) {
    const res = await fetch(path, { cache: "no-store", credentials: "same-origin" });
    if (!res.ok) throw new Error(`${path} → HTTP ${res.status}`);
    return res.json();
  }
  async function post(path) {
    const res = await fetch(path, {
      method: "POST", cache: "no-store",
      headers: { "X-Apex-Token": token(), "Content-Type": "application/json" },
      body: "{}",
    });
    let body = {};
    try { body = await res.json(); } catch (_) {}
    if (res.status === 401) throw new Error("operator token required");
    if (!res.ok) throw new Error(body.error || body.reason || `HTTP ${res.status}`);
    return body;
  }

  function toast(message, kind = "info") {
    let host = $("#aqToasts");
    if (!host) {
      host = document.createElement("div");
      host.id = "aqToasts";
      host.className = "toast-container position-fixed bottom-0 end-0 p-3";
      document.body.appendChild(host);
    }
    const id = "t" + Math.random().toString(36).slice(2);
    host.insertAdjacentHTML("beforeend", `
      <div id="${id}" class="toast align-items-center border-0" role="status">
        <div class="d-flex">
          <div class="toast-body"><span class="pill ${kind}">${esc(kind)}</span> ${esc(message)}</div>
          <button type="button" class="btn-close btn-close-white me-2 m-auto"
                  data-bs-dismiss="toast" aria-label="Close"></button>
        </div>
      </div>`);
    const node = document.getElementById(id);
    new bootstrap.Toast(node, { delay: 4500 }).show();
    node.addEventListener("hidden.bs.toast", () => node.remove());
  }

  function openTokenDialog() {
    let modal = $("#tokenModal");
    if (!modal) {
      document.body.insertAdjacentHTML("beforeend", `
        <div class="modal fade" id="tokenModal" tabindex="-1" aria-hidden="true">
          <div class="modal-dialog modal-dialog-centered">
            <div class="modal-content">
              <div class="modal-header"><h5 class="modal-title">Operator token</h5>
                <button class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
              <div class="modal-body">
                <p class="small muted">Control endpoints (kill, flatten, re-arm) require the token the
                server was started with (<code>--token</code> / <code>APEX_DASHBOARD_TOKEN</code>).
                It is kept in this browser only; reads never need it.</p>
                <label class="form-label small" for="tokenInput">Token</label>
                <input id="tokenInput" class="form-control mono" autocomplete="off"
                       placeholder="apex-demo" value="${esc(token())}">
              </div>
              <div class="modal-footer">
                <button class="btn btn-outline-secondary btn-sm" id="tokenClear">Clear</button>
                <button class="btn btn-primary btn-sm" id="tokenSave"
                        data-bs-dismiss="modal">Save</button>
              </div>
            </div>
          </div>
        </div>`);
      modal = $("#tokenModal");
      $("#tokenSave").addEventListener("click", () => {
        setToken(($("#tokenInput") || {}).value || "");
        toast(token() ? "operator token saved" : "operator token cleared", "info");
        renderLock();
      });
      $("#tokenClear").addEventListener("click", () => {
        setToken("");
        renderLock();
      });
    }
    new bootstrap.Modal(modal).show();
  }

  /* ------------------------------------------------------------------ *
   * controls
   * ------------------------------------------------------------------ */
  async function control(action) {
    const label = { kill: "kill switch (flatten + block)", flatten: "flatten all", rearm: "re-arm" }[action] || action;
    if (action !== "rearm" && !window.confirm(`Confirm: ${label}?`)) return;
    try {
      const out = await post(`/api/control/${action}`);
      const ok = out.ok !== false;
      toast(`${action}: ${ok ? "done" : (out.reason || out.error || "refused")}`, ok ? "good" : "bad");
      refresh(true);
    } catch (err) {
      toast(`${action}: ${err.message}`, "bad");
      if (/token/i.test(err.message)) openTokenDialog();
    }
  }

  function wireControls() {
    const map = { btnKill: "kill", btnFlatten: "flatten", btnRearm: "rearm" };
    Object.entries(map).forEach(([id, action]) => {
      const el = document.getElementById(id);
      if (el) el.addEventListener("click", () => control(action));
    });
    const tok = $("#btnToken");
    if (tok) tok.addEventListener("click", openTokenDialog);
  }

  function renderLock() {
    const locked = !token();
    document.querySelectorAll("[data-control]").forEach((el) => {
      el.classList.toggle("disabled", false);        // keep usable; the server decides
      el.title = locked ? "needs the operator token" : el.dataset.title || "";
    });
    const badge = $("#lockPill");
    if (badge) {
      badge.className = "pill " + (locked ? "warn" : "good");
      badge.textContent = locked ? "controls: token needed" : "controls: unlocked";
    }
  }

  /* ------------------------------------------------------------------ *
   * state + shell
   * ------------------------------------------------------------------ */
  const S = { state: null, verify: null, trades: null, research: null,
              researchAt: 0, error: null, paintedAt: 0 };
  const PAGE = (document.body.dataset.page || "overview").toLowerCase();
  let timer = null;

  async function refresh(force = false) {
    try {
      S.state = await getJSON("/api/state");
      S.error = null;
      if (PAGE === "positions" && (force || !S.trades)) {
        try { S.trades = await getJSON("/api/trades"); } catch (_) { S.trades = S.trades || []; }
      }
      if (PAGE === "journal" && (force || !S.trades)) {
        try { S.trades = await getJSON("/api/trades"); } catch (_) { S.trades = S.trades || []; }
      }
      if (PAGE === "research" && (force || !S.research || Date.now() - S.researchAt > 15000)) {
        try { S.research = await getJSON("/api/research"); S.researchAt = Date.now(); }
        catch (_) { S.research = S.research || null; }
      }
      if (PAGE === "system" && (force || !S.verify)) {
        try { S.verify = await getJSON("/api/verify"); } catch (_) { S.verify = null; }
      }
    } catch (err) {
      S.error = err.message;
    }
    paint();
  }

  function renderShell(s) {
    const mode = String(s.mode || "paper").toUpperCase();
    const modePill = $("#modePill");
    if (modePill) {
      const live = s.live_orders === true;
      modePill.className = "pill " + (live ? "live" : "");
      modePill.textContent = live ? `${mode} · LIVE ORDERS` : `${mode} · ${s.live_orders === false ? "DRY RUN" : "SIM"}`;
    }
    const safe = s.safety || {};
    const bits = [];
    if (safe.boot_policy) bits.push(`boot:${safe.boot_policy}`);
    if (safe.persist_state === false) bits.push("no-persist");
    if (s.reconcile_diffs) bits.push(`${s.reconcile_diffs} diff`);
    if (s.local_stops) bits.push(`${s.local_stops} local stop${s.local_stops > 1 ? "s" : ""}`);
    if (s.pending && s.pending.length) bits.push(`${s.pending.length} pending`);
    const feed = s.feed_health || {};
    if (feed.gaps) bits.push(`${feed.gaps} feed gap${feed.gaps > 1 ? "s" : ""}`);
    if (safe.reconnects) bits.push(`${safe.reconnects} reconnect${safe.reconnects > 1 ? "s" : ""}`);
    const safetyPill = $("#safetyPill");
    if (safetyPill) {
      const risky = !!s.reconcile_diffs || !!s.local_stops || !!feed.gaps;
      safetyPill.className = "pill " + (risky ? "warn" : "good");
      safetyPill.textContent = bits.length ? bits.join(" · ") : "safety ok";
    }
    const clockEl = $("#clock");
    if (clockEl) clockEl.textContent = clock(s.server_ts || Date.now() / 1000);
    const halted = s.risk && s.risk.halted;
    const killed = s.router && s.router.killed;
    const haltPill = $("#haltPill");
    if (haltPill) {
      haltPill.className = "pill " + (halted || killed ? "bad" : "good");
      haltPill.textContent = killed ? "KILL SWITCH" : halted
        ? `halted: ${s.risk.halt_kind || "risk"}` : "trading enabled";
    }
    renderLock();
  }

  /* ------------------------------------------------------------------ *
   * shared fragments
   * ------------------------------------------------------------------ */
  const equitySeries = (s) => {
    const hist = (s.equity_history || []).filter((r) => isNum(r.equity));
    if (hist.length > 1) {
      return hist.map((r) => ({ ts: r.ts, v: Number(r.equity), label: r.account || r.broker }));
    }
    const curve = ((s.metrics || {}).equity_curve || []).slice(-600);
    const base = Number((s.accounts || [{}])[0].equity || 0) - Number(((s.metrics || {}).pnl) || 0);
    return curve.map((v, i) => ({ ts: i, v: base + Number(v), label: "session" }));
  };

  function drawEquity(canvas, series, opts = {}) {
    if (!canvas || !series.length) return;
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || 600;
    const h = canvas.clientHeight || 180;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const vals = series.map((p) => p.v);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.12;
    lo -= pad; hi += pad;
    const x = (i) => (series.length === 1 ? 0 : (i / (series.length - 1)) * (w - 8) + 4);
    const y = (v) => h - 6 - ((v - lo) / (hi - lo)) * (h - 16);

    // grid
    ctx.strokeStyle = "rgba(255,255,255,.06)";
    ctx.lineWidth = 1;
    for (let g = 1; g <= 3; g += 1) {
      const gy = (h / 4) * g;
      ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(w, gy); ctx.stroke();
    }
    const up = vals[vals.length - 1] >= vals[0];
    const stroke = up ? "#35d07f" : "#ff5d6c";
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, up ? "rgba(53,208,127,.28)" : "rgba(255,93,108,.28)");
    grad.addColorStop(1, "rgba(0,0,0,0)");

    ctx.beginPath();
    ctx.moveTo(x(0), y(vals[0]));
    vals.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    const last = vals.length - 1;
    ctx.lineTo(x(last), h); ctx.lineTo(x(0), h); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    ctx.moveTo(x(0), y(vals[0]));
    vals.forEach((v, i) => ctx.lineTo(x(i), y(v)));
    ctx.strokeStyle = stroke; ctx.lineWidth = 1.6; ctx.stroke();

    ctx.fillStyle = "#8fa3b8";
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText(num(hi, 0), 6, 14);
    ctx.fillText(num(lo, 0), 6, h - 6);
    if (opts.lastLabel !== false) {
      const txt = num(vals[last], 2);
      const tw = ctx.measureText(txt).width;
      ctx.fillStyle = stroke;
      ctx.fillText(txt, Math.max(4, w - tw - 6), y(vals[last]) - 6);
    }
  }

  const positionsTable = (s, opts = {}) => table(
    ["Instrument", "Side", "Lots", "Entry", "Stop", "Target", "P&L", "Broker", "Age"],
    (s.positions || []).map((p) => {
      const name = (s.symbol_names || {})[p.symbol] || p.symbol;
      const stop = isNum(p.sl) ? num(p.sl) : `<span class="bad">none</span>`;
      return [
        `<div class="fw-semibold">${esc(name)}</div><div class="muted small mono">${esc(p.symbol)}</div>`,
        sidePill(p.side), num(p.lots, 2), num(p.entry), stop, num(p.tp),
        `<span class="${signCls(p.unrealised)} mono">${signed(p.unrealised)}</span>`,
        esc(p.broker || ""), `<span class="muted small">${ago(p.opened_ts)}</span>`,
      ];
    }),
    { empty: opts.empty || "flat — no open positions", tall: opts.tall });

  const tradesTable = (trades, s, opts = {}) => table(
    ["Closed", "Instrument", "Side", "Lots", "Entry", "Exit", "P&L", "R", "Exit reason", "Regime"],
    trades.map((t) => [
      `<span class="muted small">${dt(t.closed_ts)}</span>`,
      `<div class="fw-semibold">${esc(((s && s.symbol_names) || {})[t.symbol] || t.symbol)}</div>
       <div class="muted small mono">${esc(t.symbol)}</div>`,
      sidePill(t.side), num(t.lots, 2), num(t.entry), num(t.exit),
      `<span class="${signCls(t.pnl)} mono">${signed(t.pnl)}</span>`,
      `<span class="${signCls(t.r_multiple)} mono">${signed(t.r_multiple, 2)}R</span>`,
      pill(String(t.exit_reason || "").replace(/_/g, " "),
        /stop|flip|expired/.test(t.exit_reason) ? "bad" : /target|trailing/.test(t.exit_reason) ? "good" : ""),
      `<span class="muted small">${esc(regimeName(t.regime))}</span>`,
    ]),
    { empty: opts.empty || "no closed trades yet", tall: opts.tall });

  const rejectionsPanel = (map, limit = 9) => {
    const entries = Object.entries(map || {}).sort((a, b) => b[1] - a[1]).slice(0, limit);
    if (!entries.length) return `<p class="muted mb-0 px-2 py-3">no rejections recorded</p>`;
    const max = entries[0][1] || 1;
    return entries.map(([code, n]) => `
      <div class="aq-rej">
        <span class="code mono">${esc(code)}</span>
        <span class="flex-grow-1">${bar(n / max, "bad")}</span>
        <span class="n mono">${num(n, 0)}</span>
      </div>`).join("");
  };

  const heatPanel = (s) => {
    const r = s.risk || {};
    const cap = Number(r.max_heat_pct || 0);
    const heat = Number(r.heat_pct || 0);
    const frac = cap > 0 ? heat / cap : 0;
    return `
      <div class="d-flex justify-content-between small mb-1">
        <span class="muted">Aggregate heat</span>
        <span class="mono">${pct(heat)} ${cap > 0 ? `<span class="muted">of ${pct(cap)} cap</span>` : '<span class="muted">(no cap set)</span>'}</span>
      </div>
      ${bar(frac, cap > 0 && frac > 0.85 ? "bad" : "good", "open risk at stop / equity")}
      <div class="d-flex justify-content-between small mt-2">
        <span class="muted">Open risk at stop</span>
        <span class="mono">${money(r.heat_money, "USD")}</span>
      </div>
      <div class="d-flex justify-content-between small">
        <span class="muted">Positions / max</span>
        <span class="mono">${num(r.open_positions, 0)} / ${num((r.limits || {}).max_open_positions, 0)}</span>
      </div>`;
  };

  const propPanel = (s) => {
    const p = s.prop;
    if (!p) return `<p class="muted mb-0">no challenge attached (run with <code>--prop &lt;profile&gt;</code>)</p>`;
    const verdictKind = p.breach ? "bad" : p.passed ? "good" : /active|on track/i.test(p.verdict || "") ? "good" : "warn";
    const rows = [
      ["Profile", esc(p.profile || ""), esc(String(p.phase || ""))],
      ["Progress", `${pct(p.profit_pct)} of ${pct(p.target_pct)}`, bar((p.profit_pct || 0) / (p.target_pct || 1), "good")],
      ["Daily loss used", pct(p.daily_loss_used_pct), bar((p.daily_loss_used_pct || 0) / 100, "bad")],
      ["Daily floor", money(p.daily_floor), `<span class="muted small">${money(p.daily_loss_room)} room</span>`],
      ["Total floor", money(p.total_floor), `<span class="muted small">${money(p.total_loss_room)} room</span>`],
      ["Trading days", `${num(p.days_traded, 0)} / ${num(p.min_trading_days, 0)}`, pct((p.days_traded || 0) / Math.max(1, p.min_trading_days || 1))],
      ["Consistency", pct(((p.consistency || {}).best_day_share_pct), 1),
        (p.consistency || {}).ok ? pill("within rule", "good") : pill(`rule ${pct((p.consistency || {}).rule_pct, 0)}`, "warn")],
      ["Pacing (risk plan)", pct(((p.pacing || {}).recommended_risk_pct), 2) + " per trade",
        `<span class="muted small">target ${money(((p.pacing || {}).daily_target))}/day</span>`],
    ];
    const body = `<div class="table-responsive"><table class="table table-sm mb-0"><tbody>
      ${rows.map(([k, v, extra]) => `<tr><td class="muted" style="width:38%">${esc(k)}</td>
        <td class="mono">${v}</td><td style="width:34%">${extra || ""}</td></tr>`).join("")}
      </tbody></table></div>
      ${p.breach ? `<div class="alert alert-danger py-2 mt-2 mb-0 small">${esc(p.breach)}</div>` : ""}
      ${p.flat_required ? `<div class="alert alert-warning py-2 mt-2 mb-0 small">flat required: ${esc(p.flat_required)}</div>` : ""}`;
    return { body, kind: verdictKind };
  };

  const eventsPanel = (events, limit = 14) => {
    const list = (events || []).slice(0, limit);
    if (!list.length) return `<p class="muted mb-0 px-2 py-3">no events yet</p>`;
    return list.map((e) => `
      <div class="aq-event ${esc(e.kind || "")}">
        <b>${esc(e.kind || "info")}</b>
        <span class="muted mono ms-1">${clock(e.ts)}</span>
        <div>${esc(e.message || "")}</div>
      </div>`).join("");
  };

  /* ------------------------------------------------------------------ *
   * page: overview
   * ------------------------------------------------------------------ */
  function pageOverview(s) {
    const acct = (s.accounts || [])[0] || {};
    const ccy = acct.currency || "USD";
    const risk = s.risk || {};
    const m = s.metrics || {};
    const prop = propPanel(s);
    const realised = Number(m.pnl || 0);
    const unreal = Number(s.unrealised || 0);
    const kpis = [
      kpi("Equity", money(s.total_equity, ccy), `${signed(unreal)} unrealised`, signCls(unreal)),
      kpi("Day P&L", money(risk.day_pnl, ccy), `${num(risk.day_trades, 0)} trades today`, signCls(risk.day_pnl)),
      kpi("Open risk", pct(risk.heat_pct), money(risk.heat_money, ccy) + " at stop"),
      kpi("Trades closed", num((s.counters || {}).trades_closed, 0), `${pct(m.win_rate, 1)} win`),
      kpi("Avg R · PF", `${signed(m.avg_r)}R`, `PF ${num(m.profit_factor)}`, signCls(m.avg_r)),
      kpi("Prop", s.prop ? `${num(s.prop.profit_pct)}%` : "off",
        s.prop ? `${num(s.prop.target_pct)}% target` : "no challenge"),
    ].join("");

    const engineRows = (s.symbols || []).map((x) => {
      const reg = x.regime || {};
      const conv = Number(x.conviction || 0);
      const votes = Object.entries(x.votes || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
      const voteHtml = votes.slice(0, 3).map(([agent, score]) => `
        <span class="aq-vote"><span class="name">${esc(agent)}</span>
          <span class="bar"><i class="${score < 0 ? "neg" : ""}"
            style="left:${score < 0 ? (50 + score * 50) : 50}%;width:${Math.abs(score) * 50}%"></i></span>
          <span class="mono">${signed(score, 2)}</span></span>`).join("");
      return [
        `<div class="fw-semibold">${esc((s.symbol_names || {})[x.symbol] || x.symbol)}</div>
         <div class="muted small mono">${esc(x.symbol)} · ${esc(x.broker || "")}</div>`,
        `<span class="mono aq-quote">${num(x.price)}</span>`,
        pill(regimeName(reg.label), regimeKind(reg.label)),
        `<div class="d-flex gap-1">${trendChip((x.trend || {}).M5)}${trendChip((x.trend || {}).M15)}${trendChip((x.trend || {}).H1)}</div>`,
        `<div class="d-flex align-items-center gap-2"><span class="mono">${
          isNum(x.conviction) ? num(conv, 2) : '<span class="muted">—</span>'}</span>
          <span class="flex-grow-1" style="min-width:56px">${bar(conv, conv >= 0.35 ? "good" : "")}</span></div>`,
        `<div class="d-flex flex-column gap-1">${voteHtml ||
          '<span class="muted small">no candidate this bar</span>'}</div>`,
        statusPill(x.status),
      ];
    });

    return `
      <div class="row g-3">${kpis}</div>

      <div class="row g-3 mt-1">
        ${card("Decision engine", table(
          ["Instrument", "Price", "Regime", "Trend M5·M15·H1", "Conviction", "Agent votes", "State"],
          engineRows, { empty: "waiting for the first bars", tall: false }),
          { cols: "col-12 col-xl-8", aside: pill(`${(s.symbols || []).length} instruments`, "") })}
        ${card("Prop challenge", `
          <div class="d-flex justify-content-between align-items-center mb-2">
            <span class="muted small">verdict</span>${pill(s.prop ? s.prop.verdict : "not attached", prop.kind || "")}
          </div>${typeof prop === "string" ? prop : prop.body}
          <hr class="my-2" style="border-color:var(--aq-line)">
          <div class="d-flex justify-content-between small mb-1"><span class="muted">Risk governor</span>
            <span class="mono">${num((risk.limits || {}).risk_per_trade_pct)}% / trade</span></div>
          ${heatPanel(s)}
          <div class="d-flex justify-content-between small mt-2"><span class="muted">Drawdown</span>
            <span class="mono">${pct(risk.drawdown_pct)} of ${pct((risk.limits || {}).max_total_drawdown_pct)}</span></div>
          ${bar((risk.drawdown_pct || 0) / ((risk.limits || {}).max_total_drawdown_pct || 100), "bad")}`,
          { cols: "col-12 col-xl-4" })}
      </div>

      <div class="row g-3">
        ${card("Open positions", positionsTable(s), {
          cols: "col-12 col-xl-8",
          aside: s.pending && s.pending.length ? pill(`${s.pending.length} pending`, "warn") : "",
        })}
        ${card("Why trades were not taken", rejectionsPanel(s.rejections), { cols: "col-12 col-xl-4" })}
      </div>

      <div class="row g-3">
        ${card("Equity", `<canvas id="eqCanvas" class="aq-canvas"></canvas>
          <div class="d-flex justify-content-between small muted mt-1">
            <span>${esc((s.safety || {}).boot_policy ? "boot " + (s.safety || {}).boot_policy : "")}</span>
            <span>realised ${signed(realised)} · ${num((s.journal_metrics || {}).trades, 0)} journal trades</span>
          </div>`, { cols: "col-12 col-xl-8" })}
        ${card("Closed trades", tradesTable((s.recent_trades || []).slice(0, 12), s),
          { cols: "col-12 col-xl-4", tight: true })}
      </div>

      <div class="row g-3">
        ${card("Event log", eventsPanel(s.events, 12), { cols: "col-12 col-xl-6" })}
        ${card("Router", routerPanel(s), { cols: "col-12 col-xl-6" })}
      </div>`;
  }

  const routerPanel = (s) => {
    const r = s.router || {};
    const st = r.stats || {};
    const rows = (r.routes || []).map((route) => [
      `<span class="fw-semibold">${esc(route.label || route.broker)}</span>
       <div class="muted small mono">${esc(route.account)}</div>`,
      route.enabled ? pill("enabled", "good") : pill("disabled", "bad"),
      `<span class="muted small mono">${esc((route.symbols || []).join(" · ") || "all")}</span>`,
    ]);
    return `
      <div class="d-flex justify-content-between mb-2">
        <span class="muted small">Kill switch</span>
        ${r.killed ? pill("ENGAGED", "bad") : pill("released", "good")}
      </div>
      <div class="row row-cols-3 row-cols-md-6 g-2 text-center mb-2">
        ${["submitted", "filled", "rejected", "retried", "closed", "errors"].map((k) => `
          <div class="col"><div class="muted small text-uppercase" style="font-size:.62rem;letter-spacing:.1em">${k}</div>
          <div class="mono fw-semibold">${num(st[k], 0)}</div></div>`).join("")}
      </div>
      ${table(["Route", "State", "Instruments"], rows, { empty: "no routes" })}`;
  };

  /* ------------------------------------------------------------------ *
   * page: positions
   * ------------------------------------------------------------------ */
  function pagePositions(s) {
    const acct = (s.accounts || [])[0] || {};
    const ccy = acct.currency || "USD";
    const open = s.positions || [];
    const riskMoney = open.reduce((acc, p) => acc + Number(((p.meta || {}).risk_money) || 0), 0);
    const exposure = {};
    open.forEach((p) => { exposure[p.symbol] = (exposure[p.symbol] || 0) + Number(p.lots || 0); });
    const exposureRows = Object.entries(exposure).map(([sym, lots]) => [
      `<span class="fw-semibold">${esc((s.symbol_names || {})[sym] || sym)}</span>
       <span class="muted small mono">${esc(sym)}</span>`,
      num(lots, 2),
      money(Number(lots) * 100, ccy) + " <span class='muted small'>notional (approx)</span>",
    ]);
    const pendingRows = (s.pending || []).map((p) => [
      esc(p.symbol), sidePill(p.side), num(p.lots, 2),
      `<span class="mono">${esc(p.order_id || "—")}</span>`,
      `<span class="muted small">${p.expires_ts ? ago(p.expires_ts) : "—"}</span>`,
      pill(p.expires_ts && p.expires_ts * 1000 < Date.now() ? "stale" : "live",
        p.expires_ts && p.expires_ts * 1000 < Date.now() ? "bad" : "good"),
    ]);
    const trades = (S.trades || (s.recent_trades || []).slice().reverse());
    const symbols = [...new Set(trades.map((t) => t.symbol))].sort();

    return `
      <div class="row g-3">
        ${kpi("Open positions", num(open.length, 0), `${num((s.risk || {}).open_positions, 0)} counted by risk`)}
        ${kpi("Unrealised", money(s.unrealised, ccy), "marked at the touch", signCls(s.unrealised))}
        ${kpi("Risk at stop", money(riskMoney || (s.risk || {}).heat_money, ccy), pct((s.risk || {}).heat_pct))}
        ${kpi("Pending orders", num((s.pending || []).length, 0), "accepted, not filled")}
        ${kpi("Closed (session)", num(((S.trades || []).length), 0), `${pct((s.metrics || {}).win_rate, 1)} win`)}
        ${kpi("Kill switch", (s.router || {}).killed ? "ENGAGED" : "released",
          (s.router || {}).killed ? "new orders blocked" : "orders allowed",
          (s.router || {}).killed ? "bad" : "good")}
      </div>

      <div class="row g-3 mt-1">
        ${card("Open positions", positionsTable(s, { tall: true }), { cols: "col-12 col-xl-8" })}
        ${card("Exposure by instrument", `
          ${table(["Instrument", "Lots", "Notional"], exposureRows, { empty: "no exposure" })}
          <hr class="my-2" style="border-color:var(--aq-line)">
          <div class="d-flex justify-content-between small"><span class="muted">Margin used</span>
            <span class="mono">${money(acct.margin_used, ccy)}</span></div>
          <div class="d-flex justify-content-between small"><span class="muted">Free margin</span>
            <span class="mono">${money(acct.free_margin, ccy)}</span></div>
          <div class="d-flex justify-content-between small"><span class="muted">Leverage</span>
            <span class="mono">1:${num(acct.leverage, 0)}</span></div>`,
          { cols: "col-12 col-xl-4" })}
      </div>

      <div class="row g-3">
        ${card("Pending orders", table(
          ["Instrument", "Side", "Lots", "Order id", "Expires", "State"], pendingRows,
          { empty: "no accepted-but-unfilled orders (nothing is being forgotten)" }),
          { cols: "col-12" })}
      </div>

      <div class="row g-3">
        ${card("Closed trades", tradesTable(trades.slice().reverse(), s, { tall: true }), {
          cols: "col-12",
          aside: `<span class="muted small">${symbols.length} instruments</span>` +
            (symbols.length ? `<select id="tradeFilter" class="form-select form-select-sm" style="width:auto">
              <option value="">all symbols</option>
              ${symbols.map((x) => `<option>${esc(x)}</option>`).join("")}</select>` : ""),
        })}
      </div>`;
  }

  /* ------------------------------------------------------------------ *
   * page: signals
   * ------------------------------------------------------------------ */
  function pageSignals(s) {
    const cards = (s.symbols || []).map((x) => {
      const reg = x.regime || {};
      const votes = Object.entries(x.votes || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
      const voteRows = votes.map(([agent, score]) => `
        <div class="aq-vote mb-1"><span class="name">${esc(agent)}</span>
          <span class="bar"><i class="${score < 0 ? "neg" : ""}"
            style="left:${score < 0 ? (50 + score * 50) : 50}%;width:${Math.abs(score) * 50}%"></i></span>
          <span class="mono" style="width:3.2rem;text-align:right">${signed(score, 2)}</span></div>`).join("");
      const conv = Number(x.conviction || 0);
      return `
        <div class="col-12 col-md-6 col-xxl-4">
          <section class="card aq-symbol-card h-100">
            <header class="card-header">
              <span>${esc((s.symbol_names || {})[x.symbol] || x.symbol)}
                <span class="muted mono ms-1">${esc(x.symbol)}</span></span>
              ${pill(regimeName(reg.label), regimeKind(reg.label))}
            </header>
            <div class="card-body">
              <div class="d-flex justify-content-between align-items-baseline mb-2">
                <span class="mono aq-quote">${num(x.price)}</span>
                <span class="muted small">${statusPill(x.status)} ${num(x.bars, 0)} bars</span>
              </div>
              <div class="d-flex justify-content-between small mb-1">
                <span class="muted">Conviction</span><span class="mono">${num(conv, 2)}</span></div>
              ${bar(conv, conv >= 0.35 ? "good" : "", "weighted votes × disagreement penalty")}
              <div class="d-flex gap-1 my-2">${trendChip((x.trend || {}).M5)}${trendChip((x.trend || {}).M15)}${trendChip((x.trend || {}).H1)}</div>
              <div class="d-flex justify-content-between small muted mb-1">
                <span>ATR ${num(x.atr, 4)}</span><span>RSI ${num(x.rsi, 1)}</span>
                <span>ADX ${num(reg.adx, 1)}</span>
              </div>
              ${voteRows || '<p class="muted small mb-0">no candidate this bar</p>'}
            </div>
          </section>
        </div>`;
    }).join("");

    const signalRows = (s.signals || []).map((sig) => [
      `<span class="muted small">${clock(sig.ts)}</span>`,
      `<span class="fw-semibold">${esc(sig.symbol || "")}</span>`,
      sig.side ? sidePill(sig.side) : "",
      num(sig.conviction, 2),
      num(sig.price),
      statusPill(sig.status),
      `<span class="muted small">${esc(sig.reason || "")}</span>`,
    ]);

    return `
      <div class="row g-3">${cards || '<p class="muted">no instruments configured</p>'}</div>

      <div class="row g-3 mt-1">
        ${card("Rejection reasons (this run)", rejectionsPanel(s.rejections, 12), { cols: "col-12 col-xl-5" })}
        ${card("Recent signal log", table(
          ["Time", "Instrument", "Side", "Conviction", "Price", "Decision", "Reason"],
          signalRows, { empty: "no signals recorded", tall: true }), { cols: "col-12 col-xl-7" })}
      </div>`;
  }

  /* ------------------------------------------------------------------ *
   * page: risk
   * ------------------------------------------------------------------ */
  function pageRisk(s) {
    const r = s.risk || {};
    const lim = r.limits || {};
    const acct = (s.accounts || [])[0] || {};
    const ccy = acct.currency || "USD";
    const prop = propPanel(s);
    const halt = r.halted || (s.router || {}).killed;
    const used = Number(r.day_pnl || 0) < 0 ? Math.abs(Number(r.day_pnl)) / Math.max(1, Number(r.day_start_equity || 1)) * 100 : 0;
    const dayFrac = used / Math.max(0.01, Number(lim.max_daily_loss_pct || 3));
    const ddFrac = Number(r.drawdown_pct || 0) / Math.max(0.01, Number(lim.max_total_drawdown_pct || 8));
    const sessions = Object.entries(r.session_hours || {});
    const cooldown = Number(r.cooldown_remaining_s || 0);

    return `
      <div class="row g-3">
        ${kpi("State", halt ? "HALTED" : "trading", halt ? esc(r.halt_reason || "risk halt") : "all limits inside",
          halt ? "bad" : "good")}
        ${kpi("Day P&L", money(r.day_pnl, ccy), `limit ${pct(lim.max_daily_loss_pct)}`, signCls(r.day_pnl))}
        ${kpi("Day budget used", pct(used), `${money(r.day_start_equity, ccy)} day start`)}
        ${kpi("Drawdown", pct(r.drawdown_pct), `limit ${pct(lim.max_total_drawdown_pct)}`)}
        ${kpi("Heat", pct(r.heat_pct), Number(r.max_heat_pct) ? `cap ${pct(r.max_heat_pct)}` : "no cap")}
        ${kpi("Cooldown", cooldown > 0 ? dur(cooldown) : "—", `${num(r.consecutive_losses, 0)} losses in a row`)}
      </div>

      <div class="row g-3 mt-1">
        ${card("Capital protection", `
          <div class="small d-flex justify-content-between"><span class="muted">Daily loss</span>
            <span class="mono">${pct(used)} / ${pct(lim.max_daily_loss_pct)}</span></div>
          ${bar(dayFrac, dayFrac > 0.8 ? "bad" : "good")}
          <div class="small d-flex justify-content-between mt-2"><span class="muted">Max drawdown</span>
            <span class="mono">${pct(r.drawdown_pct)} / ${pct(lim.max_total_drawdown_pct)}</span></div>
          ${bar(ddFrac, ddFrac > 0.8 ? "bad" : "good")}
          <div class="small d-flex justify-content-between mt-2"><span class="muted">Aggregate heat</span>
            <span class="mono">${money(r.heat_money, ccy)} <span class="muted">${pct(r.heat_pct)}</span></span></div>
          ${bar(Number(r.max_heat_pct) ? Number(r.heat_pct) / Number(r.max_heat_pct) : 0,
            Number(r.max_heat_pct) ? "good" : "", r.max_heat_pct ? "heat vs cap" : "no cap configured")}
          <hr class="my-3" style="border-color:var(--aq-line)">
          <div class="row row-cols-2 row-cols-md-3 g-2 small">
            ${[["Risk per trade", pct(lim.risk_per_trade_pct)],
               ["Max trades / day", `${num(r.day_trades, 0)} / ${num(lim.max_trades_per_day, 0)}`],
               ["Max open", num(lim.max_open_positions, 0)],
               ["Loss streak limit", "3 (configurable)"],
               ["Cooldown after loss", dur(lim.cooldown_after_loss_s)],
               ["Group heat cap", Number(r.max_group_heat_pct) ? pct(r.max_group_heat_pct) : "off"],
            ].map(([k, v]) => `<div class="col"><div class="muted">${esc(k)}</div>
              <div class="mono">${esc(v)}</div></div>`).join("")}
          </div>
          <hr class="my-3" style="border-color:var(--aq-line)">
          <div class="small muted mb-1">Session windows (UTC)</div>
          <div class="d-flex flex-wrap gap-2">
            ${pill(`global ${num(r.session_start_utc, 0)}–${num(r.session_end_utc, 0)}h`, "")}
            ${sessions.map(([k, v]) => pill(`${k} ${num(v[0], 0)}–${num(v[1], 0)}h`, "")).join("")}
          </div>`, { cols: "col-12 col-xl-6" })}
        ${card("Prop challenge", `${typeof prop === "string" ? prop : prop.body}`, { cols: "col-12 col-xl-6" })}
      </div>

      <div class="row g-3">
        ${card("Controls", `
          <p class="small muted">Flatten closes every position at the touch; Kill flattens and blocks new
          orders until Re-arm. Both require the operator token and are journaled as control events.</p>
          <div class="d-flex flex-wrap gap-2">
            <button class="btn btn-outline-warning btn-sm" onclick="AQ.control('flatten')">Flatten all</button>
            <button class="btn btn-outline-danger btn-sm" onclick="AQ.control('kill')">Kill switch</button>
            <button class="btn btn-outline-success btn-sm" onclick="AQ.control('rearm')">Re-arm</button>
          </div>
          <div class="small muted mt-3">
            ${(s.router || {}).killed ? pill("kill switch ENGAGED", "bad") : pill("kill switch released", "good")}
            ${r.local_stops || s.local_stops ? pill(`${num(s.local_stops, 0)} local-only stop(s)`, "warn") : ""}
          </div>`, { cols: "col-12 col-xl-5" })}
        ${card("Recent risk & control events",
          eventsPanel((s.events || []).filter((e) => /risk|control|reconcile|breach/.test(e.kind || "")), 12),
          { cols: "col-12 col-xl-7" })}
      </div>`;
  }

  /* ------------------------------------------------------------------ *
   * page: journal
   * ------------------------------------------------------------------ */
  function pageJournal(s) {
    const m = s.journal_metrics || {};
    const sess = s.metrics || {};
    const acct = (s.accounts || [])[0] || {};
    const ccy = acct.currency || "USD";
    const group = (label, key) => {
      const rows = Object.entries(m[key] || {}).sort((a, b) => b[1].pnl - a[1].pnl).map(([name, g]) => [
        esc(name), num(g.trades, 0),
        `<span class="${signCls(g.pnl)} mono">${signed(g.pnl)}</span>`,
        num((g.wins / Math.max(1, g.trades)) * 100, 1) + "%",
        signed(g.r_sum / Math.max(1, g.trades), 2) + "R",
      ]);
      return card(label, table(["Name", "Trades", "P&L", "Win %", "Avg R"], rows,
        { empty: "no closed trades" }), { cols: "col-12 col-md-6 col-xxl-4" });
    };
    const trades = (S.trades || []).slice().reverse();

    return `
      <div class="row g-3">
        ${kpi("Journal trades", num(m.trades, 0), `${num(m.days, 0)} trading days`)}
        ${kpi("Net P&L", money(m.pnl, ccy), "closed, after commission", signCls(m.pnl))}
        ${kpi("Profit factor", num(m.profit_factor), `expectancy ${signed(m.expectancy)}`)}
        ${kpi("Win rate", pct(m.win_rate, 1), `${signed(m.avg_r)}R average`)}
        ${kpi("Max drawdown", pct(m.max_drawdown_pct), "on closed equity")}
        ${kpi("Sharpe · Sortino", `${num(m.sharpe, 2)} · ${num(m.sortino, 2)}`, "annualised, daily returns")}
      </div>

      <div class="row g-3 mt-1">
        ${card("Equity curve", `<canvas id="eqCanvas" class="aq-canvas" style="height:clamp(180px,30vh,300px)"></canvas>
          <div class="d-flex justify-content-between small muted mt-1">
            <span>journal ${num((s.journal_bytes || 0) / 1e6, 2)} MB · ${num(m.trades, 0)} trades</span>
            <span>session: ${num(sess.trades, 0)} trades · ${signed(sess.pnl)} ${esc(ccy)}</span>
          </div>`, { cols: "col-12 col-xl-8" })}
        ${card("Session vs journal", `
          <div class="table-responsive"><table class="table table-sm mb-0"><tbody>
            ${[["Trades", num(sess.trades, 0), num(m.trades, 0)],
               ["P&L", signed(sess.pnl), signed(m.pnl)],
               ["Win rate", pct(sess.win_rate, 1), pct(m.win_rate, 1)],
               ["Avg R", signed(sess.avg_r), signed(m.avg_r)],
               ["Profit factor", num(sess.profit_factor), num(m.profit_factor)],
               ["Max DD", pct(sess.max_drawdown_pct), pct(m.max_drawdown_pct)],
            ].map(([k, a, b]) => `<tr><td class="muted">${k}</td>
              <td class="mono text-end">${a}</td><td class="mono text-end">${b}</td></tr>`).join("")}
          </tbody></table></div>
          <p class="small muted mt-2 mb-0">Session = this process. Journal = everything the sqlite file
          has seen, including earlier runs. They must not be averaged together.</p>`,
          { cols: "col-12 col-xl-4" })}
      </div>

      <h2 class="aq-section-title">Attribution</h2>
      <div class="row g-3">
        ${group("By strategy", "by_strategy")}
        ${group("By instrument", "by_symbol")}
        ${group("By broker", "by_broker")}
        ${group("By exit reason", "by_exit")}
        ${group("By regime", "by_regime")}
        ${card("Journal maintenance", `
          <p class="small muted mb-2">The journal keeps every trade forever and prunes the
          high-frequency tables. Run it from the shell:</p>
          <pre class="small mb-2" style="white-space:pre-wrap">python3 -m apex prune --db ${esc((S.state || {}).db_path || "runs/live/apex.db")} --days 14</pre>
          <div class="small d-flex justify-content-between"><span class="muted">File size</span>
            <span class="mono">${num((s.journal_bytes || 0) / 1e6, 2)} MB</span></div>
          <div class="small d-flex justify-content-between"><span class="muted">Equity points</span>
            <span class="mono">${num((s.equity_history || []).length, 0)}</span></div>
          <div class="small d-flex justify-content-between"><span class="muted">Events</span>
            <span class="mono">${num((s.events || []).length, 0)} recent</span></div>`,
          { cols: "col-12 col-xxl-4" })}
      </div>

      <h2 class="aq-section-title">Trades and events</h2>
      <div class="row g-3">
        ${card("Latest closed trades", tradesTable(trades.slice(0, 120), s, { tall: true }),
          { cols: "col-12 col-xl-7", aside: pill(`${trades.length} shown`, "") })}
        ${card("Event log", eventsPanel(s.events, 30), { cols: "col-12 col-xl-5" })}
      </div>`;
  }

  /* ------------------------------------------------------------------ *
   * page: research
   * ------------------------------------------------------------------ */
  function pageResearch(s) {
    const r = S.research;
    if (!r) return `<div class="alert alert-secondary">reading <code>runs/</code>…</div>`;
    const runs = r.runs || [];
    const sweeps = r.sweeps || [];
    const sims = r.propsims || [];
    const latest = runs[0];
    const best = runs.slice().sort((a, b) => (b.metrics.profit_factor || 0) - (a.metrics.profit_factor || 0))[0];

    const runRows = runs.map((run) => {
      const m = run.metrics || {};
      const req = run.request || {};
      const syms = Object.values(req.symbols_by_broker || {}).flat();
      return [
        `<div class="fw-semibold mono">${esc(run.name)}</div>
         <div class="muted small">${ago(run.mtime)}</div>`,
        `<span class="mono small">${esc(syms.join(" · ") || "—")}</span>
         <div class="muted small">${num(req.bars, 0)} bars · ${esc(req.tf || "")} · seed ${num(req.seed, 0)}</div>`,
        num(m.trades, 0),
        pct(m.win_rate, 1),
        `<span class="${signCls(m.pnl)} mono">${signed(m.pnl)}</span>`,
        num(m.profit_factor),
        `<span class="${signCls(m.avg_r)} mono">${signed(m.avg_r, 3)}</span>`,
        `<span class="${Number(m.max_drawdown_pct) > 8 ? "bad" : ""} mono">${pct(m.max_drawdown_pct)}</span>`,
      ];
    });

    const sweepRows = sweeps.map((sw) => {
      const top = (sw.results || [])[0] || {};
      return [
        `<span class="fw-semibold mono">${esc(sw.name)}</span>`,
        num(sw.combinations, 0),
        num(top.score, 3),
        signed(top.avg_r, 3),
        pct(top.max_dd),
        `<span class="muted small">${ago(sw.mtime)}</span>`,
      ];
    });

    const sweepDetail = sweeps[0] ? table(
      ["#", "Configuration", "Score", "Avg R", "Max DD", "Trades", "Prop pass"],
      (sweeps[0].results || []).map((row, i) => [
        num(i + 1, 0),
        `<span class="mono small">${esc(JSON.stringify(row.params || {}))}</span>`,
        num(row.score, 3), signed(row.avg_r, 3), pct(row.max_dd),
        num(row.trades, 1), pct(row.pass_rate, 1),
      ]), { empty: "no sweep results" }) : `<p class="muted small mb-0">no sweep artefacts</p>`;

    const simRows = sims.map((sim) => [
      `<span class="fw-semibold">${esc(sim.profile || "")}</span>
       <div class="muted small mono">${esc(sim.name)}/${esc(sim.file || "")}</div>`,
      num(sim.runs, 0),
      pct(sim.pass_rate_pct, 1),
      pct(sim.breach_rate_pct, 1),
      pct(sim.median_profit_pct),
      pct(sim.median_dd_pct),
      `<span class="muted small">${Object.entries(sim.verdicts || {})
        .map(([k, v]) => `${esc(k)}:${v}`).join(" · ")}</span>`,
    ]);

    const simDetail = sims[0] ? table(
      ["Seed", "Verdict", "Profit %", "Max DD %", "Daily loss used %", "Days", "Trades", "Breach"],
      (sims[0].outcomes || []).map((o) => [
        num(o.seed, 0),
        pill(o.verdict || "", /passed|target_hit/.test(o.verdict || "") ? "good"
          : /breach|failed/.test(o.verdict || "") ? "bad" : ""),
        `<span class="${signCls(o.profit_pct)} mono">${pct(o.profit_pct)}</span>`,
        pct(o.max_dd_pct), pct(o.daily_loss_used_pct),
        num(o.days_traded, 0), num(o.trades, 0),
        o.breach ? `<span class="bad small">${esc(o.breach)}</span>` : '<span class="muted">—</span>',
      ]), { empty: "no outcomes" }) : `<p class="muted small mb-0">no prop simulations</p>`;

    const reproduce = latest
      ? (() => {
          const req = latest.request || {};
          const syms = Object.values(req.symbols_by_broker || {}).flat().join(",");
          return `python3 -m apex backtest --symbols ${esc(syms || "R_10,R_25,R_50,R_75,R_100")} \\
  --bars ${num(req.bars, 0)} --tf ${esc(req.tf || "M5")} --seed ${num(req.seed, 0)} \\
  --strategy ${esc(req.strategy || "scalp_balanced")} --out runs/$(date +%s)`;
        })()
      : "python3 -m apex backtest --bars 3000 --seed 7";

    return `
      <div class="aq-lock">
        <b class="d-block mb-1">Synthetic control tape — not a performance claim</b>
        <span class="small">${esc(r.caveat || "")}</span>
      </div>

      <div class="row g-3">
        ${kpi("Artefacts", num(runs.length + sweeps.length + sims.length, 0),
          `${num(runs.length, 0)} backtest${runs.length === 1 ? "" : "s"} · ` +
          `${num(sweeps.length, 0)} sweep${sweeps.length === 1 ? "" : "s"} · ` +
          `${num(sims.length, 0)} prop sim${sims.length === 1 ? "" : "s"}`)}
        ${kpi("Latest P&L", latest ? signed(latest.metrics.pnl) : "—",
          latest ? `${num(latest.metrics.trades, 0)} trades` : "run a backtest",
          latest ? signCls(latest.metrics.pnl) : "")}
        ${kpi("Best profit factor", best ? num(best.metrics.profit_factor) : "—",
          best ? esc(best.name) : "—")}
        ${kpi("Reproducible", "yes",
          "fixed tape anchor + fixed seed", "good")}
      </div>

      <div class="row g-3 mt-1">
        ${card("Backtest runs", table(
          ["Run", "Book", "Trades", "Win %", "P&L", "PF", "Avg R", "Max DD"],
          runRows, { empty: "no summary.json under runs/", tall: true }),
          { cols: "col-12 col-xxl-8" })}
        ${card("Reproduce a run", `
          <p class="small muted">Every artefact is a file under <code>runs/</code>. Re-running with the
          same seed and the same anchor produces the same tape on any day — the anchor is fixed
          precisely so a published number can be checked.</p>
          <pre class="small mb-2" style="white-space:pre-wrap">${reproduce}</pre>
          <div class="small d-flex justify-content-between"><span class="muted">Runs root</span>
            <span class="mono">${esc(r.root)}</span></div>
          <hr class="my-2" style="border-color:var(--aq-line)">
          <p class="small muted mb-0">A negative backtest is information, not a bug: it is why the
          system ships with a hard risk governor instead of a promise.</p>`,
          { cols: "col-12 col-xxl-4" })}
      </div>

      <div class="row g-3">
        ${card("Prop-firm simulations", table(
          ["Profile", "Runs", "Pass %", "Breach %", "Med. profit", "Med. DD", "Verdicts"],
          simRows, { empty: "no propsim_*.json artefacts" }), { cols: "col-12" })}
      </div>

      <div class="row g-3">
        ${card(`Prop outcomes${sims[0] ? " · " + esc(sims[0].profile) : ""}`, simDetail,
          { cols: "col-12 col-xl-6", tight: true })}
        ${card("Parameter sweeps", table(
          ["Sweep", "Combos", "Best score", "Avg R", "Max DD", "When"],
          sweepRows, { empty: "no sweep.json artefacts" }), { cols: "col-12 col-xl-6" })}
      </div>

      <div class="row g-3">
        ${card("Top sweep configurations", sweepDetail, { cols: "col-12" })}
      </div>`;
  }

  /* ------------------------------------------------------------------ *
   * page: system
   * ------------------------------------------------------------------ */
  function pageSystem(s) {
    const acct = (s.accounts || [])[0] || {};
    const ccy = acct.currency || "USD";
    const feed = s.feed_health || {};
    const safe = s.safety || {};
    const v = S.verify;
    const verifyRows = ((v && v.checks) || []).map((c) => [
      `<span class="mono">${esc(c.name)}</span>`,
      c.ok ? pill("PASS", "good") : pill(c.severity === "fail" ? "FAIL" : c.severity === "warn" ? "WARN" : "INFO",
        c.severity === "fail" && !c.ok ? "bad" : c.severity === "warn" && !c.ok ? "warn" : ""),
      `<span class="small">${esc(c.detail || "")}</span>`,
    ]);
    const verifyBanner = v
      ? `<div class="d-flex justify-content-between align-items-center mb-2">
          ${v.ok ? pill("checklist PASS", "good") : pill("checklist FAIL", "bad")}
          <span class="muted small mono">${esc((v.db || "").split("/").pop())} · ${v.generated_ts ? ago(v.generated_ts) : ""}</span>
        </div>${table(["Check", "Result", "Detail"], verifyRows, { empty: "no checks" })}`
      : `<p class="muted small mb-0">verify unavailable (no journal attached)</p>`;

    return `
      <div class="row g-3">
        ${kpi("Mode", String(s.mode || "paper").toUpperCase(), s.live_orders ? "live orders ON" : "orders simulated",
          s.live_orders ? "warn" : "good")}
        ${kpi("Equity", money(s.total_equity, ccy), `${(s.accounts || []).length} account(s)`)}
        ${kpi("Feed bars", num(feed.bars, 0), `${num(feed.gaps, 0)} gaps`)}
        ${kpi("Clock drift", feed.last_drift_s === null || feed.last_drift_s === undefined ? "not measured"
          : `${signed(feed.last_drift_s, 0)}s`, `${num(feed.drift_warnings, 0)} warnings`)}
        ${kpi("Reconnects", num(safe.reconnects, 0), "socket supervision")}
        ${kpi("Journal", num((s.journal_bytes || 0) / 1e6, 2) + " MB", `${num(safe.ticks_recorded, 0)} ticks recorded`)}
      </div>

      <div class="row g-3 mt-1">
        ${card("Deployment checklist (apex verify)", verifyBanner, { cols: "col-12 col-xl-7" })}
        ${card("Safety flags", `
          <div class="table-responsive"><table class="table table-sm mb-0"><tbody>
            ${[["Boot policy", esc(safe.boot_policy || "—")],
               ["State persistence", safe.persist_state ? "on" : "off"],
               ["Reconcile every", `${num(safe.reconcile_every_s, 0)}s`],
               ["Last reconcile", safe.last_reconcile ? ago(safe.last_reconcile) : "—"],
               ["Reconcile diffs", num(s.reconcile_diffs, 0)],
               ["Local-only stops", num(s.local_stops, 0)],
               ["Heartbeat", s.heartbeat_age_s === undefined ? "see watchdog" : `${num(s.heartbeat_age_s, 1)}s`],
               ["Pending orders", num((s.pending || []).length, 0)],
               ["Feed gap (max)", `${num(feed.max_gap_bars, 2)} bars`],
               ["News blackout", s.news
                 ? (s.news.blackout ? pill("ACTIVE", "bad") : pill("clear", "good"))
                 : '<span class="muted">no calendar</span>'],
               ["Next event", s.news && s.news.next
                 ? `${esc(s.news.next.currency)} ${esc(s.news.next.title)} · ${dt(s.news.next.ts)}`
                 : '<span class="muted">—</span>'],
            ].map(([k, val]) => `<tr><td class="muted">${esc(k)}</td><td class="mono">${val}</td></tr>`).join("")}
          </tbody></table></div>`, { cols: "col-12 col-xl-5" })}
      </div>

      <div class="row g-3">
        ${card("Broker router", routerPanel(s), { cols: "col-12 col-xl-6" })}
        ${card("Accounts", table(
          ["Broker", "Account", "Equity", "Balance", "Free margin", "Leverage"],
          (s.accounts || []).map((a) => [
            esc(a.broker), `<span class="mono">${esc(a.account)}</span>`,
            money(a.equity, a.currency || ccy), money(a.balance, a.currency || ccy),
            money(a.free_margin, a.currency || ccy), `1:${num(a.leverage, 0)}`,
          ]), { empty: "no accounts" }), { cols: "col-12 col-xl-6" })}
      </div>

      <div class="row g-3">
        ${card("Instruments", table(
          ["Symbol", "Name", "Broker", "State", "Bars", "Price"],
          (s.symbols || []).map((x) => [
            `<span class="mono">${esc(x.symbol)}</span>`,
            esc((s.symbol_names || {})[x.symbol] || ""),
            esc(x.broker || ""),
            pill(x.status || "", /live|ok|ready/i.test(x.status || "") ? "good" : ""),
            num(x.bars, 0), num(x.price),
          ]), { empty: "no instruments" }), { cols: "col-12 col-xl-7" })}
        ${card("Operator", `
          <p class="small muted">Control endpoints require the token the server was started with.
          Reads (this page included) never do.</p>
          <div class="d-flex flex-wrap gap-2">
            <button class="btn btn-outline-light btn-sm" onclick="AQ.openToken()">Set operator token</button>
            <button class="btn btn-outline-warning btn-sm" onclick="AQ.control('flatten')">Flatten</button>
            <button class="btn btn-outline-danger btn-sm" onclick="AQ.control('kill')">Kill</button>
            <button class="btn btn-outline-success btn-sm" onclick="AQ.control('rearm')">Re-arm</button>
          </div>
          <hr class="my-3" style="border-color:var(--aq-line)">
          <div class="small muted mb-1">API</div>
          <div class="mono small">
            GET /api/state · /api/trades · /api/health · /api/verify · /api/stream<br>
            POST /api/control/kill · /flatten · /rearm
          </div>
          <div class="mt-3 small muted">
            Docs: <code>docs/LIVE_DEPLOYMENT.md</code> · <code>docs/PROP_FIRM_PLAYBOOK.md</code>
          </div>`, { cols: "col-12 col-xl-5" })}
      </div>`;
  }

  const PAGES = {
    overview: pageOverview,
    positions: pagePositions,
    signals: pageSignals,
    risk: pageRisk,
    journal: pageJournal,
    research: pageResearch,
    system: pageSystem,
  };

  /* ------------------------------------------------------------------ *
   * paint loop
   * ------------------------------------------------------------------ */
  function paint() {
    const host = $("#page");
    if (!host) return;
    if (!S.state) {
      host.innerHTML = `<div class="alert alert-secondary">connecting to the engine…</div>`;
      return;
    }
    renderShell(S.state);
    if (S.error) {
      host.insertAdjacentHTML("afterbegin",
        `<div class="alert alert-danger py-2">snapshot error: ${esc(S.error)}</div>`);
    }
    const render = PAGES[PAGE] || PAGES.overview;
    try {
      host.innerHTML = render(S.state);
      const canvas = $("#eqCanvas");
      if (canvas) drawEquity(canvas, equitySeries(S.state));
      const filter = $("#tradeFilter");
      if (filter) {
        filter.addEventListener("change", () => {
          const sym = filter.value;
          const rows = document.querySelectorAll("#page tbody tr");
          const trades = (S.trades || []);
          rows.forEach((row, i) => {
            const t = trades[trades.length - 1 - i];
            if (!t) return;
            row.style.display = !sym || t.symbol === sym ? "" : "none";
          });
        });
      }
    } catch (err) {
      host.innerHTML = `<div class="alert alert-danger">render error: ${esc(err.message)}</div>`;
    }
    S.paintedAt = Date.now();
  }

  /* ------------------------------------------------------------------ *
   * boot
   * ------------------------------------------------------------------ */
  window.AQ = { control, openToken: openTokenDialog, refresh };

  document.addEventListener("DOMContentLoaded", () => {
    wireControls();
    renderLock();
    refresh(true);
    timer = setInterval(() => refresh(false), 2000);
    window.addEventListener("resize", () => {
      const canvas = $("#eqCanvas");
      if (canvas && S.state) drawEquity(canvas, equitySeries(S.state));
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "r" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); refresh(true); }
    });
  });

  window.addEventListener("beforeunload", () => { if (timer) clearInterval(timer); });
})();
