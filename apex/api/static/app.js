/* ABUTRON QUANT dashboard — vanilla JS, no build step, no CDN. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d = 2) => (n === null || n === undefined || Number.isNaN(n))
    ? "—" : Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (n, d = 1) => (n === null || n === undefined) ? "—" : `${Number(n).toFixed(d)}%`;
  const money = (n, d = 2) => (n === null || n === undefined) ? "—"
    : `${n < 0 ? "−" : ""}$${Math.abs(Number(n)).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}`;
  const signed = (n, d = 2) => (n === null || n === undefined) ? "—"
    : `<span class="${n > 0 ? "up" : n < 0 ? "down" : ""}">${n > 0 ? "+" : n < 0 ? "−" : ""}$${Math.abs(Number(n)).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d })}</span>`;
  const cls = (n) => n > 0 ? "up" : n < 0 ? "down" : "";
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const timeStr = (ts) => ts ? new Date(ts * 1000).toLocaleTimeString([], { hour12: false }) : "—";
  const trendArrow = (v) => v > 0 ? '<span class="up">▲</span>' : v < 0 ? '<span class="down">▼</span>' : '<span class="muted">·</span>';

  let lastState = null;
  let equityHistory = [];

  /* ------------------------------------------------------------------ */
  async function poll() {
    try {
      const res = await fetch("/api/state", { cache: "no-store" });
      const s = await res.json();
      if (s && s.error) { showError(s.error); return; }
      lastState = s;
      clearError();
      render(s);
    } catch (e) {
      showError("dashboard offline — retrying");
    }
  }

  function showError(msg) {
    let el = $("errBanner");
    if (!el) {
      el = document.createElement("div");
      el.id = "errBanner";
      el.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:99;" +
        "background:#2a1116;border:1px solid #ff5d6c;color:#ffb3ba;padding:9px 14px;border-radius:8px;" +
        "font:600 11px/1.3 ui-monospace,monospace;letter-spacing:.04em";
      document.body.appendChild(el);
    }
    el.textContent = msg;
  }
  function clearError() { const el = $("errBanner"); if (el) el.remove(); }

  /* ------------------------------------------------------------------ */
  function render(s) {
    const mode = (s.mode || "paper").toUpperCase();
    const pill = $("modePill");
    pill.textContent = s.live_orders ? `${mode} · LIVE ORDERS` : `${mode} · ${s.live_orders === false ? "DRY RUN" : "PAPER"}`;
    pill.className = "pill " + (s.live_orders ? "live" : "paper");
    $("clock").textContent = new Date().toLocaleTimeString([], { hour12: false });

    // Safety chip: what an operator needs to know before leaving the desk alone.
    const safe = s.safety || {};
    const bits = [];
    if (safe.boot_policy) bits.push(`boot:${safe.boot_policy}`);
    if (safe.persist_state === false) bits.push("no-persist");
    if (s.reconcile_diffs) bits.push(`${s.reconcile_diffs} diff`);
    if (safe.ticks_recorded) bits.push(`ticks:${safe.ticks_recorded}`);
    if (s.heat_pct !== undefined && s.heat_pct !== null) bits.push(`heat:${s.heat_pct}%`);
    if (s.pending && s.pending.length) bits.push(`${s.pending.length} pending`);
    const feed = s.feed_health || {};
    if (feed.gaps) bits.push(`${feed.gaps} feed gap${feed.gaps > 1 ? "s" : ""}`);
    if (feed.drift_warnings) bits.push(`clock drift`);
    const spy = $("safetyPill");
    if (spy) {
      const news = s.news;
      spy.textContent = bits.length ? bits.join(" · ") : "safety ok";
      spy.className = "pill " + (news && news.blackout ? "warn" : "");
      spy.title = news && news.next
        ? `next high-impact event in ${Math.round((news.next.ts * 1000 - Date.now()) / 60000)} min (${news.next.currency})`
        : "no calendar loaded";
    }

    const acct = (s.accounts && s.accounts[0]) || {};
    const equity = s.total_equity || acct.equity || 0;
    const risk = s.risk || {};
    const prop = s.prop;

    $("kEquity").textContent = money(equity);
    $("kEquitySub").textContent = s.accounts && s.accounts.length > 1
      ? `${s.accounts.length} accounts · ${acct.currency || "USD"}` : (acct.currency || "USD");

    const dayPnl = risk.day_pnl ?? 0;
    $("kDayPnl").innerHTML = signed(dayPnl);
    $("kDaySub").textContent = risk.day_start_equity ? `base ${money(risk.day_start_equity, 0)}` : "";

    // open risk at stop
    let openRisk = 0;
    (s.positions || []).forEach(p => {
      const perLot = Math.abs((p.entry || 0) - (p.sl || p.entry || 0));
      openRisk += perLot * (p.lots || 0);
    });
    const openRiskPct = equity ? (openRisk / equity * 100) : 0;
    $("kOpenRisk").textContent = money(openRisk) ;
    $("kOpenRisk").nextElementSibling.textContent = `${pct(openRiskPct)} of equity at stop`;

    const c = s.counters || {};
    const m = s.metrics || {};
    $("kTrades").textContent = (m.trades ?? c.trades_closed ?? 0);
    $("kTradesSub").textContent = `${c.orders_filled ?? 0} fills · ${c.bars ?? 0} bars`;
    $("kWin").textContent = m.trades ? `${pct(m.win_rate)} · ${(m.avg_r > 0 ? "+" : "") + fmt(m.avg_r, 2)}R` : "—";
    $("kWinSub").textContent = m.trades ? `PF ${isFinite(m.profit_factor) ? fmt(m.profit_factor, 2) : "∞"} · ${m.trades} trades` : "no closed trades yet";

    if (prop) {
      $("kProp").textContent = (prop.verdict || "—").replace("_", " ").toUpperCase();
      $("kProp").className = "mono " + (prop.verdict === "passed" ? "up" : prop.verdict === "breached" ? "down" : prop.verdict === "at_risk" ? "warnc" : "");

      $("kPropSub").textContent = `${fmt(prop.profit_pct, 2)}% of ${fmt(prop.target_pct, 1)}% target · ${prop.days_traded}/${prop.min_trading_days} days`;
    } else {
      $("kProp").textContent = "OFF";
      $("kPropSub").textContent = "no challenge attached";
    }

    renderSignals(s);
    renderPositions(s);
    renderProp(prop);
    renderRejections(s.rejections || {});
    renderRisk(risk, s.router);
    renderRouter(s.router, s.accounts || []);
    renderTrades(s);
    renderWeights(s.memory);
    renderEvents(s.events || []);
    drawEquity(s);
    if (s.server_ts) equityHistory.push({ ts: s.server_ts, eq: equity });
  }

  /* ------------------------------------------------------------------ */
  function renderSignals(s) {
    const tbody = $("signalsTbl").querySelector("tbody");
    const syms = s.symbols || [];
    const names = s.symbol_names || {};
    if (!syms.length) { tbody.innerHTML = `<tr><td colspan="7" class="empty">warming up…</td></tr>`; return; }
    tbody.innerHTML = syms.map(x => {
      const r = x.regime || {};
      const conv = x.conviction ?? 0;
      const status = (x.status || "").toUpperCase();
      const statusCls = status === "CANDIDATE" || status === "ENTERED" ? "buy"
        : (status.startsWith("NO") || status === "LOW_CONVICTION" || status === "OVEREXTENDED") ? "neutral" : "neutral";
      const votes = x.votes || {};
      const voteHtml = Object.entries(votes).map(([k, v]) =>
        `<span class="vote ${v > 0.02 ? "pos" : v < -0.02 ? "neg" : "zero"}">${esc(k.slice(0, 3))} ${v > 0 ? "+" : ""}${fmt(v, 2)}</span>`
      ).join("") || '<span class="muted">—</span>';
      const trend = x.trend || {};
      return `<tr>
        <td><span class="sym">${esc(x.symbol)}</span><div class="muted" style="font-size:10px">${esc(names[x.symbol] || x.broker || "")}</div></td>
        <td class="num">${fmt(x.price, 2)}</td>
        <td><span class="badge neutral">${esc((r.label || "—").replace("_", " "))}</span>
            <div class="muted" style="font-size:10px">ADX ${fmt(r.adx, 0)} · eff ${fmt(r.efficiency, 2)} · vol ${fmt((r.vol_percentile || 0) * 100, 0)}%</div></td>
        <td class="num">${trendArrow(trend.M5)} ${trendArrow(trend.M15)} ${trendArrow(trend.H1)}
            <div class="muted" style="font-size:10px">RSI ${fmt(x.rsi, 0)}</div></td>
        <td><div class="conv"><div class="track"><div class="fill" style="width:${Math.min(100, conv * 100).toFixed(0)}%"></div></div>
            <span>${fmt(conv, 2)}</span></div></td>
        <td><div class="votes">${voteHtml}</div></td>
        <td><span class="badge ${statusCls}">${esc(status || "—")}</span></td>
      </tr>`;
    }).join("");
    const c = s.counters || {};
    $("engineHint").textContent = `${c.candidates ?? 0} candidates · ${c.orders_filled ?? 0} filled · ${c.bars ?? 0} bars`;
  }

  function renderPositions(s) {
    const tbody = $("positionsTbl").querySelector("tbody");
    const pos = s.positions || [];
    $("posHint").textContent = `${pos.length} open`;
    if (!pos.length) { tbody.innerHTML = `<tr><td colspan="8" class="empty">flat — no open risk</td></tr>`; return; }
    tbody.innerHTML = pos.map(p => `<tr>
      <td><span class="sym">${esc(p.symbol)}</span></td>
      <td><span class="badge ${p.side === "BUY" ? "buy" : "sell"}">${esc(p.side)}</span></td>
      <td class="num">${fmt(p.lots, 2)}</td>
      <td class="num">${fmt(p.entry, 2)}</td>
      <td class="num muted">${p.sl ? fmt(p.sl, 2) : "—"}</td>
      <td class="num muted">${p.tp ? fmt(p.tp, 2) : "—"}</td>
      <td class="num">${signed(p.unrealised, 2)}</td>
      <td class="muted">${esc(p.broker)}</td>
    </tr>`).join("");
  }

  /* ------------------------------------------------------------------ */
  function meter(label, value, max, text, kind) {
    const w = max > 0 ? Math.min(100, Math.max(0, value / max * 100)) : 0;
    return `<div class="meter">
      <div class="lab"><span>${esc(label)}</span><b>${esc(text)}</b></div>
      <div class="track"><div class="fill ${kind}" style="width:${w.toFixed(1)}%"></div></div>
    </div>`;
  }

  function renderProp(p) {
    const body = $("propBody");
    const pill = $("propVerdict");
    if (!p) {
      body.innerHTML = `<p class="muted note">No prop challenge attached. Run with <code>--prop ftmo_2step</code> to enable the rule guard.</p>`;
      pill.textContent = "OFF"; pill.className = "pill"; return;
    }
    const v = p.verdict;
    pill.textContent = v.replace("_", " ").toUpperCase();
    pill.className = "pill " + (v === "passed" ? "pass" : v === "breached" ? "fail"
      : v === "at_risk" ? "warn" : v === "target_hit" ? "pass" : "paper");

    const targetPct = p.target_pct || 1;
    const daily = p.daily_loss_used_pct || 0;
    const cons = p.consistency || {};
    const pac = p.pacing || {};
    const roomPct = p.start_balance ? (p.total_loss_room / p.start_balance * 100) : 0;
    const dailyKind = daily > 70 ? "bad" : daily > 40 ? "warn" : "ok";

    body.innerHTML = `
      ${meter("Profit target", p.profit_pct || 0, targetPct, `${fmt(p.profit_pct, 2)}% / ${fmt(targetPct, 1)}%`, "prop")}
      ${meter("Daily loss used", daily, 100, `${fmt(daily, 1)}%`, dailyKind)}
      ${meter("Total loss room", roomPct, (p.rules && p.rules.max_total_loss_pct) || 10,
              `${money(p.total_loss_room, 0)} left`, roomPct < 3 ? "bad" : roomPct < 6 ? "warn" : "ok")}
      <div class="rule-row"><span>Trading days</span><b>${p.days_traded} / ${p.min_trading_days} min</b></div>
      <div class="rule-row"><span>Calendar days left</span><b>${p.days_left}</b></div>
      <div class="rule-row"><span>Consistency</span><b class="${cons.ok ? "up" : "warnc"}">${fmt(cons.best_day_share_pct, 1)}% / ${fmt(cons.rule_pct, 0)}%</b></div>
      <div class="rule-row"><span>Today vs pace</span><b>${money(pac.today_pnl, 0)} / ${money(pac.daily_target, 0)}</b></div>
      <div class="rule-row"><span>Guarded risk / trade</span><b>${fmt(pac.recommended_risk_pct, 2)}%</b></div>
      <div class="note">${p.breach ? `<span class="down">Breach: ${esc(p.breach)}</span>` :
        `Daily floor ${money(p.daily_floor, 0)} · total floor ${money(p.total_floor, 0)} · ${esc(p.profile)}`}</div>`;
  }

  function renderRejections(rej) {
    const el = $("rejections");
    const rows = Object.entries(rej).sort((a, b) => b[1] - a[1]).slice(0, 8);
    if (!rows.length) { el.innerHTML = `<p class="muted note">No rejections recorded yet.</p>`; return; }
    const max = rows[0][1] || 1;
    el.innerHTML = rows.map(([k, v]) => `<div class="bar-row">
      <span class="name" title="${esc(k)}">${esc(k.replace(/_/g, " ").toLowerCase())}</span>
      <div class="track"><div class="fill" style="width:${(v / max * 100).toFixed(0)}%"></div></div>
      <span class="n">${v}</span></div>`).join("");
  }

  function renderRisk(r, router) {
    const el = $("riskBody");
    const lim = r.limits || {};
    const state = r.halted
      ? `<span class="down">HALTED — ${esc(r.halt_reason || "")}</span>`
      : `<span class="up">ARMED</span>`;
    el.innerHTML = `
      <div class="risk-row"><span>Risk governor</span><b>${state}</b></div>
      <div class="risk-row"><span>Trades today</span><b>${r.day_trades ?? 0} / ${lim.max_trades_per_day ?? "—"}</b></div>
      <div class="risk-row"><span>Consecutive losses</span><b>${r.consecutive_losses ?? 0}</b></div>
      <div class="risk-row"><span>Drawdown from peak</span><b class="${(r.drawdown_pct || 0) > 5 ? "down" : ""}">${pct(r.drawdown_pct)}</b></div>
      <div class="risk-row"><span>Risk / trade</span><b>${fmt(lim.risk_per_trade_pct, 2)}%</b></div>
      <div class="risk-row"><span>Daily stop</span><b>${fmt(lim.max_daily_loss_pct, 1)}%</b></div>
      <div class="risk-row"><span>Kill switch</span><b class="${router && router.killed ? "down" : "up"}">${router && router.killed ? "ENGAGED" : "clear"}</b></div>`;
  }

  function renderRouter(r, accounts) {
    const el = $("routerBody");
    if (!r) { el.innerHTML = `<p class="muted note">Router not attached.</p>`; return; }
    const st = r.stats || {};
    const routes = (r.routes || []).map(rt => {
      const acct = accounts.find(a => a.account === rt.account) || {};
      return `<div class="rule-row"><span>${esc(rt.label || rt.broker)} <span class="muted">(${esc(rt.account)})</span></span>
        <b>${acct.equity !== undefined ? money(acct.equity, 0) : "—"}</b></div>
        <div class="rule-row"><span class="muted" style="font-size:10px">${esc((rt.symbols || []).join(" "))}</span>
        <span class="muted" style="font-size:10px">${rt.enabled ? "enabled" : "disabled"}</span></div>`;
    }).join("");
    el.innerHTML = routes + `
      <div class="rule-row"><span>Submitted / filled</span><b>${st.submitted ?? 0} / ${st.filled ?? 0}</b></div>
      <div class="rule-row"><span>Rejected / retried</span><b>${st.rejected ?? 0} / ${st.retried ?? 0}</b></div>
      <div class="rule-row"><span>Adapter errors</span><b class="${(st.errors || 0) > 0 ? "down" : ""}">${st.errors ?? 0}</b></div>`;
  }

  function renderTrades(s) {
    const tbody = $("tradesTbl").querySelector("tbody");
    let trades = (s.recent_trades || []).slice();
    if (!trades.length && s.metrics && s.metrics.by_strategy) {
      tbody.innerHTML = `<tr><td colspan="10" class="empty">No closed trades yet — the engine is looking for structure.</td></tr>`;
      $("tradeHint").textContent = `${s.metrics.trades || 0} closed`;
      return;
    }
    tbody.innerHTML = trades.map(t => `<tr>
      <td class="num muted">${timeStr(t.closed_ts)}</td>
      <td><span class="sym">${esc(t.symbol)}</span></td>
      <td><span class="badge ${t.side === "BUY" ? "buy" : "sell"}">${esc(t.side)}</span></td>
      <td class="num">${fmt(t.lots, 2)}</td>
      <td class="num">${fmt(t.entry, 2)}</td>
      <td class="num">${fmt(t.exit, 2)}</td>
      <td class="num">${signed(t.pnl, 2)}</td>
      <td class="num ${cls(t.r_multiple)}">${(t.r_multiple > 0 ? "+" : "") + fmt(t.r_multiple, 2)}R</td>
      <td class="muted">${esc((t.exit_reason || "").replace(/_/g, " "))}</td>
      <td class="muted">${esc((t.regime || "").replace(/_/g, " "))}</td>
    </tr>`).join("");
    const m = s.metrics || {};
    $("tradeHint").textContent = `${m.trades || trades.length} closed · PF ${isFinite(m.profit_factor) ? fmt(m.profit_factor, 2) : "—"}`;
  }

  function renderWeights(mem) {
    const el = $("weights");
    if (!mem || !mem.stats || !Object.keys(mem.stats).length) {
      el.innerHTML = `<p class="muted note" style="grid-column:span 3">No outcomes yet — weights start at priors.</p>`;
      return;
    }
    el.innerHTML = Object.entries(mem.stats).map(([agent, st]) => {
      const r = st.ewma_r || 0;
      return `<div class="wcell"><span class="a">${esc(agent)}</span>
        <span class="v ${cls(r)}">${(r > 0 ? "+" : "") + fmt(r, 2)}R</span>
        <span class="muted" style="font-size:9.5px">n=${st.n}</span></div>`;
    }).join("");
  }

  function renderEvents(events) {
    const el = $("events");
    if (!events.length) { el.innerHTML = `<p class="muted note">No events yet.</p>`; return; }
    el.innerHTML = events.slice(0, 40).map(e => `<div class="ev ${esc(e.kind)}">
      <span class="t">${timeStr(e.ts)}</span><span class="k">${esc(e.kind)}</span>
      <span class="m">${esc(e.message)}</span></div>`).join("");
  }

  /* ------------------------------------------------------------------ */
  function drawEquity(s) {
    const cv = $("equity");
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    cv.width = w * dpr; cv.height = h * dpr;
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    let series = [];
    if (s.metrics && Array.isArray(s.metrics.equity_curve) && s.metrics.equity_curve.length > 2) {
      series = s.metrics.equity_curve;
    } else if (equityHistory.length > 2) {
      series = equityHistory.map(p => p.eq);
    }
    if (series.length < 2) {
      ctx.fillStyle = "#566173"; ctx.font = "12px ui-monospace,monospace";
      ctx.fillText("equity curve appears after the first closed trades", 14, h / 2);
      return;
    }
    series = series.slice(-400);
    const min = Math.min(...series), max = Math.max(...series);
    const span = Math.max(1e-9, max - min);
    const pad = 8;
    const x = i => pad + i * (w - 2 * pad) / (series.length - 1);
    const y = v => h - pad - (v - min) / span * (h - 2 * pad);

    // grid
    ctx.strokeStyle = "rgba(38,52,74,.55)"; ctx.lineWidth = 1;
    for (let g = 0; g <= 3; g++) {
      const yy = pad + g * (h - 2 * pad) / 3;
      ctx.beginPath(); ctx.moveTo(pad, yy); ctx.lineTo(w - pad, yy); ctx.stroke();
    }
    // zero line
    if (min < 0 && max > 0) {
      ctx.strokeStyle = "rgba(124,138,158,.5)"; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(pad, y(0)); ctx.lineTo(w - pad, y(0)); ctx.stroke();
      ctx.setLineDash([]);
    }
    const rising = series[series.length - 1] >= series[0];
    const stroke = rising ? "#3ddc97" : "#ff5d6c";
    const grad = ctx.createLinearGradient(0, pad, 0, h - pad);
    grad.addColorStop(0, rising ? "rgba(61,220,151,.28)" : "rgba(255,93,108,.25)");
    grad.addColorStop(1, "rgba(10,14,20,0)");

    ctx.beginPath();
    series.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
    ctx.lineTo(x(series.length - 1), h - pad); ctx.lineTo(x(0), h - pad); ctx.closePath();
    ctx.fillStyle = grad; ctx.fill();

    ctx.beginPath();
    series.forEach((v, i) => i ? ctx.lineTo(x(i), y(v)) : ctx.moveTo(x(i), y(v)));
    ctx.strokeStyle = stroke; ctx.lineWidth = 1.6; ctx.stroke();

    ctx.fillStyle = "#7c8a9e"; ctx.font = "10px ui-monospace,monospace";
    ctx.fillText(`${series[0] >= 0 ? "+" : ""}${fmt(series[0], 0)}`, pad + 2, h - 2);
    if (s.metrics) {
      ctx.fillText(`min ${fmt(min, 0)}  max ${fmt(max, 0)}  DD ${fmt(s.metrics.max_drawdown_pct, 2)}%`, w - 250, h - 2);
    }
  }

  /* ------------------------------------------------------------------ */
  function apexToken() { try { return localStorage.getItem("apexToken") || ""; } catch (e) { return ""; } }

  async function control(path) {
    // The operator token never lives in the page: it is asked for once and kept
    // in this browser only. A 401 means "you are not authorised to flatten".
    let token = apexToken();
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        const res = await fetch(path, {
          method: "POST",
          headers: token ? { "X-Apex-Token": token } : {},
        });
        if (res.status === 401) {
          token = prompt("Operator token required") || "";
          try { localStorage.setItem("apexToken", token); } catch (e) {}
          continue;
        }
        if (!res.ok) { showError("control rejected (" + res.status + ")"); return; }
        break;
      } catch (e) { showError("control failed"); return; }
    }
    setTimeout(poll, 400);
  }
  $("btnKill").addEventListener("click", () => {
    if (confirm("Engage kill switch and flatten all positions?")) control("/api/control/kill");
  });
  $("btnFlatten").addEventListener("click", () => {
    if (confirm("Close all open positions now?")) control("/api/control/flatten");
  });
  $("btnRearm").addEventListener("click", () => control("/api/control/rearm"));

  window.addEventListener("resize", () => lastState && drawEquity(lastState));
  poll();
  setInterval(poll, 2000);
})();
