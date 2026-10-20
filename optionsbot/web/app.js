"use strict";

const $ = (sel) => document.querySelector(sel);
let state = null;
const DEMO = window.OPTIONSBOT_DEMO || null; // set by the standalone replay build (python -m optionsbot.demo)
let activeSymbol = null;
const openDetails = new Set();

// ------------------------------------------------------------------ formatting
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x, d = 0) => (x == null ? "–" : Number(x).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const inr = (x, d = 0) => (x == null ? "–" : `₹${num(x, d)}`);
const signed = (x, d = 0) => (x == null ? "–" : `${x >= 0 ? "+" : "−"}₹${num(Math.abs(x), d)}`);
const pct = (x, d = 1) => (x == null ? "–" : `${(x * 100).toFixed(d)}%`);
const cls = (x) => (x == null ? "" : x >= 0 ? "pos" : "neg");
const time = (iso) => (iso ? iso.slice(11, 16) : "–");
const dateTime = (iso) => (iso ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : "–");
const pretty = (s) => String(s || "").replace(/_/g, " ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());

// ------------------------------------------------------------------ transport
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $("#conn").classList.add("on");
  ws.onmessage = (ev) => render(JSON.parse(ev.data));
  ws.onclose = () => {
    $("#conn").classList.remove("on");
    setTimeout(connect, 3000);
  };
}

async function post(url) {
  const res = await fetch(url, { method: "POST" });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

async function action(url, confirmText) {
  if (DEMO) return;
  if (confirmText && !confirm(confirmText)) return;
  try {
    await post(url);
    const res = await fetch("/api/state");
    render(await res.json());
  } catch (err) {
    alert(err.message);
  }
}

$("#btn-pause").onclick = () => (DEMO ? DEMO.toggle() : action(state?.paused ? "/api/bot/resume" : "/api/bot/pause"));
$("#btn-flatten").onclick = () => action("/api/flatten", "Close ALL open paper positions now?");
$("#auto-paper").onchange = (e) => action(`/api/bot/auto-paper/${e.target.checked}`);

// ------------------------------------------------------------------ tooltip
const tip = $("#tooltip");
function showTip(html, x, y) {
  tip.innerHTML = html;
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  const left = Math.min(x + 14, window.innerWidth - r.width - 8);
  const top = y - r.height - 12 < 0 ? y + 16 : y - r.height - 12;
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${top}px`;
}
const hideTip = () => { tip.hidden = true; };

// ------------------------------------------------------------------ render
function render(s) {
  if (!s || !s.symbols) return;
  state = s;
  renderHeader(s);
  renderKpis(s);
  renderSymbols(s);
  renderIdeas(s);
  renderPositions(s);
  renderEquity(s);
  renderEvents(s);
  renderClosed(s);
  renderBacktest(s);
}

function renderHeader(s) {
  const src = $("#source");
  src.textContent = s.demo ? (s.simulated ? "REPLAY · SIMULATED" : "RECORDED · NSE") : s.simulated ? "SIMULATED DATA" : `LIVE · ${s.provider}`;
  if (s.demo) { $("#conn").classList.add("on"); $("#conn").title = "Replaying recorded session"; }
  src.className = `badge ${s.simulated ? "sim" : "live"}`;
  $("#market").textContent = s.market_open ? "Market open" : "Market closed";
  $("#clock").textContent = `${s.time.slice(0, 10)} ${s.time.slice(11, 19)} IST${s.vix != null ? ` · India VIX ${num(s.vix, 2)}` : ""}`;
  $("#btn-pause").textContent = s.paused ? (s.demo ? "Play" : "Resume") : "Pause";
  $("#btn-pause").className = s.paused ? "primary" : "";
  document.body.classList.toggle("demo", Boolean(s.demo));
  $("#auto-paper").checked = s.auto_paper;

  const banners = [];
  if (s.demo && s.simulated) banners.push(`<div class="banner warn"><b>Replay of a recorded session</b> from the bot's built-in market simulator. Prices, OI and trades are synthetic. Run <code>python -m optionsbot</code> on your machine for live NSE data.</div>`);
  else if (s.demo) banners.push(`<div class="banner warn"><b>Recorded from live NSE data</b> (${dateTime(s.recorded_span[0])} → ${dateTime(s.recorded_span[1])} IST). This page replays what the bot saw; it does not refresh itself. Run <code>python -m optionsbot</code> for a live view.</div>`);
  else if (s.simulated) banners.push(`<div class="banner warn">Running on the <b>synthetic market simulator</b>: prices, OI and trades are not real. Start without <code>--simulate</code> for live NSE data.</div>`);
  if (!s.simulated && !s.market_open) banners.push(`<div class="banner warn"><b>Market closed.</b> NSE reports the capital market closed (holiday, weekend or outside 09:15–15:30 IST), so prices are from the last session and no new trades are entered.</div>`);
  if (s.paused && !s.demo) banners.push(`<div class="banner warn">Bot paused: analysis continues, but no new paper trades open. Exits still run.</div>`);
  if (s.risk.halted) banners.push(`<div class="banner error"><b>Circuit breaker tripped:</b> ${esc(s.risk.halt_reason)}. No new entries today.</div>`);
  for (const e of s.errors) banners.push(`<div class="banner error">${esc(e)}</div>`);
  $("#banners").innerHTML = banners.join("");
}

function meter(el, used, limit) {
  const p = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  el.style.width = `${p}%`;
  el.classList.toggle("hot", p >= 75);
}

function renderKpis(s) {
  const r = s.risk;
  $("#k-equity").textContent = inr(r.equity);
  $("#k-capital").textContent = `Capital ${inr(r.capital)} · ${signed(r.equity - r.capital)} all-time`;
  $("#k-day").innerHTML = `<span class="${cls(r.total)}">${signed(r.total)}</span>`;
  meter($("#m-day"), Math.max(0, -r.total), r.loss_limit);
  $("#k-day-sub").textContent = `Realised ${signed(r.realized)} · loss limit ${inr(r.loss_limit)} (${r.loss_used_pct}% used)`;
  $("#k-risk").textContent = inr(r.open_risk);
  meter($("#m-risk"), r.open_risk, r.open_risk_limit);
  $("#k-risk-sub").textContent = `Limit ${inr(r.open_risk_limit)} · ${s.positions.length}/${r.max_open_positions} positions · ${inr(r.risk_per_trade)}/trade`;
  const st = s.stats;
  $("#k-win").textContent = st.trades ? `${pct(st.win_rate, 0)} win` : "No trades yet";
  $("#k-win-sub").textContent = st.trades ? `${st.trades} trades · net ${signed(st.total_pnl)}` : "Paper trades appear as signals fire";
  $("#k-cb").innerHTML = r.halted ? `<span class="neg">⛔ Halted</span>` : `<span class="pos">✓ Armed</span>`;
  $("#k-cb-sub").textContent = r.halted ? r.halt_reason : `${r.consecutive_losses} consecutive losses today`;
}

function metric(label, value, sub = "") {
  return `<div class="metric"><div class="label">${label}</div><div class="value">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
}

function renderSymbols(s) {
  const symbols = Object.keys(s.symbols);
  if (!activeSymbol || !symbols.includes(activeSymbol)) activeSymbol = symbols[0];
  $("#tabs").innerHTML = symbols.map((sym) =>
    `<button type="button" role="tab" aria-selected="${sym === activeSymbol}" data-sym="${sym}">${sym}${s.symbols[sym].analytics ? ` · ${num(s.symbols[sym].analytics.spot, 2)}` : ""}</button>`).join("");
  $("#tabs").querySelectorAll("button").forEach((b) => (b.onclick = () => { activeSymbol = b.dataset.sym; renderSymbols(state); }));

  const d = s.symbols[activeSymbol];
  if (!d || d.error) {
    $("#symbol-panel").innerHTML = `<div class="empty">No data for ${esc(activeSymbol)}: ${esc(d?.error || "waiting for first update")}</div>`;
    return;
  }
  const a = d.analytics, v = d.view, rg = d.regime;
  const trendWord = (x) => (x > 0.3 ? "bullish" : x < -0.3 ? "bearish" : "neutral");
  const metrics = [
    metric("Spot", num(a.spot, 2), `Updated ${time(a.timestamp)}${d.stale ? " · STALE" : ""}`),
    metric("Expiry", a.expiry, `${a.dte} days · lot ${a.lot_size}`),
    metric("ATM IV", pct(a.atm_iv), `±${num(a.expected_move)} expected move`),
    metric("IV rank", v.iv_rank == null ? "–" : num(v.iv_rank), esc(v.iv_rank_source)),
    metric("Realised vol 20d", pct(v.realized_vol), `VRP ${v.vrp == null ? "–" : (v.vrp >= 0 ? "+" : "") + pct(v.vrp)}`),
    metric("PCR (OI)", num(a.pcr_oi, 2), `Volume PCR ${num(a.pcr_volume, 2)}`),
    metric("Max pain", num(a.max_pain), `${a.max_pain ? num(a.max_pain - a.spot) : "–"} from spot`),
    metric("OI walls", `${num(a.put_wall)} / ${num(a.call_wall)}`, "Put support / call resistance"),
    metric("Dealer gamma", pretty(a.gex_regime), `${num(a.net_gex_cr)} Cr per 1% · flip ${num(a.gamma_flip)}`),
    metric("25Δ skew", a.skew_25d == null ? "–" : `${num(a.skew_25d, 1)} pts`, "Put IV − call IV"),
    metric("Trend score", num(v.trend, 2), trendWord(v.trend)),
    metric("Direction", num(v.direction, 2), `Trend + OI flow (${num(a.oi_sentiment, 2)}) · ${trendWord(v.direction)}`),
  ];
  $("#symbol-panel").innerHTML = `
    <div class="analysis">
      <div>
        <div class="metrics">${metrics.join("")}</div>
        <div class="regime"><span class="tag">Regime: ${pretty(rg.label)}</span>
          <ul>${rg.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>
      </div>
      <div>
        <div class="panel-head" style="margin-bottom:4px"><h2>Open interest by strike</h2><span class="hint">${a.expiry} expiry · hover for detail</span></div>
        <div class="legend"><span><i class="swatch" style="background:var(--series-1)"></i>Call OI</span><span><i class="swatch" style="background:var(--series-2)"></i>Put OI</span><span>┆ spot · ▼ max pain</span></div>
        <div id="oi-chart" class="chart"></div>
      </div>
    </div>`;
  renderOiChart($("#oi-chart"), a);
}

function renderOiChart(el, a) {
  const rows = a.oi_profile;
  if (!rows.length) { el.innerHTML = `<div class="empty">No OI data</div>`; return; }
  const W = 720, H = 260, m = { l: 48, r: 8, t: 14, b: 34 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const maxOi = Math.max(1, ...rows.map((r) => Math.max(r.call_oi, r.put_oi)));
  const band = iw / rows.length;
  const bw = Math.max(1.5, (band - 4) / 2);
  const y = (v) => m.t + ih - (v / maxOi) * ih;
  const lo = rows[0].strike, hi = rows[rows.length - 1].strike;
  const xOf = (k) => m.l + ((k - lo) / Math.max(1, hi - lo)) * (iw - band) + band / 2;
  const ticks = [0, 0.5, 1].map((f) => f * maxOi);
  const fmtK = (v) => (v >= 1e5 ? `${(v / 1e5).toFixed(1)}L` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : `${Math.round(v)}`);
  const bar = (x, v, color) => {
    const h = Math.max(0, m.t + ih - y(v));
    if (h < 0.5) return "";
    const r = Math.min(4, bw / 2, h);
    const top = y(v);
    return `<path d="M${x},${m.t + ih} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${m.t + ih} Z" fill="${color}"/>`;
  };
  const labelEvery = Math.ceil(rows.length / 8);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Call and put open interest by strike">`;
  for (const t of ticks) svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(t)}" y2="${y(t)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(t) + 4}" text-anchor="end">${fmtK(t)}</text>`;
  rows.forEach((r, i) => {
    const cx = xOf(r.strike);
    svg += bar(cx - bw - 1, r.call_oi, "var(--series-1)") + bar(cx + 1, r.put_oi, "var(--series-2)");
    if (i % labelEvery === 0) svg += `<text x="${cx}" y="${H - m.b + 16}" text-anchor="middle">${num(r.strike)}</text>`;
  });
  const sx = xOf(Math.min(hi, Math.max(lo, a.spot)));
  svg += `<line x1="${sx}" x2="${sx}" y1="${m.t - 6}" y2="${m.t + ih}" stroke="var(--text-primary)" stroke-dasharray="3 3"/>`;
  svg += `<text x="${sx}" y="${m.t - 4}" text-anchor="middle" style="fill:var(--text-primary);font-weight:600">${num(a.spot)}</text>`;
  if (a.max_pain != null && a.max_pain >= lo && a.max_pain <= hi) {
    const mx = xOf(a.max_pain);
    svg += `<path d="M${mx - 5},${m.t + ih + 2} L${mx + 5},${m.t + ih + 2} L${mx},${m.t + ih + 9} Z" fill="var(--text-secondary)"/>`;
  }
  rows.forEach((r, i) => {
    svg += `<rect class="hit" data-i="${i}" x="${xOf(r.strike) - band / 2}" y="${m.t}" width="${band}" height="${ih}" fill="transparent"/>`;
  });
  svg += `</svg>`;
  el.innerHTML = svg;
  el.querySelectorAll(".hit").forEach((rect) => {
    rect.onmousemove = (ev) => {
      const r = rows[+rect.dataset.i];
      showTip(`<b>Strike ${num(r.strike)}</b><br>Call OI ${num(r.call_oi)} (${r.call_oi_chg >= 0 ? "+" : ""}${num(r.call_oi_chg)}) · IV ${r.call_iv ?? "–"}%<br>Put OI ${num(r.put_oi)} (${r.put_oi_chg >= 0 ? "+" : ""}${num(r.put_oi_chg)}) · IV ${r.put_iv ?? "–"}%`, ev.clientX, ev.clientY);
    };
    rect.onmouseleave = hideTip;
  });
}

function renderIdeas(s) {
  const cards = [];
  for (const [sym, d] of Object.entries(s.symbols)) {
    if (!d.ideas) continue;
    if (!d.ideas.length) {
      cards.push(`<div class="idea"><h3><span>${sym}: no trade</span><span class="status">Standing aside</span></h3><div class="blockers">${esc(d.regime.notes[d.regime.notes.length - 1] || "")}</div></div>`);
      continue;
    }
    d.ideas.forEach((idea, i) => {
      const key = `${sym}:${idea.strategy}:${i}`;
      const credit = idea.net_premium_per_lot >= 0;
      const lots = Math.max(idea.lots, 1);
      const canTrade = idea.status === "actionable" && !s.auto_paper;
      cards.push(`
        <article class="idea ${i === 0 ? "top" : ""}">
          <h3><span>${sym} · ${pretty(idea.strategy)} <small class="hint">${idea.bias.toLowerCase()} · exp ${idea.expiry}</small></span>
            <span class="status ${idea.status}">${pretty(idea.status)}</span></h3>
          <div class="legs">${idea.legs.map((l) => `${esc(l.label)} @ ${num(l.price, 2)} <span class="hint">Δ${num(l.delta, 2)}</span>`).join("<br>")}</div>
          <div class="nums">
            <div><span>${credit ? "Credit" : "Debit"} / lot</span><b>${inr(Math.abs(idea.net_premium_per_lot))}</b></div>
            <div><span>Max profit / lot</span><b class="pos">${inr(idea.max_profit_per_lot)}</b></div>
            <div><span>Max loss / lot</span><b class="neg">${inr(idea.max_loss_per_lot)}</b></div>
            <div><span>Prob. of profit</span><b>${pct(idea.pop, 0)}</b></div>
            <div><span>Exp. value / lot</span><b>${signed(idea.ev_per_lot)}</b></div>
            <div><span>Confidence</span><b>${pct(idea.confidence, 0)}</b></div>
            <div><span>Size</span><b>${idea.lots} lot${idea.lots === 1 ? "" : "s"}</b></div>
            <div><span>Take profit at</span><b>${signed(idea.take_profit * idea.lot_size * lots)}</b></div>
            <div><span>Stop loss at</span><b>${signed(idea.stop_loss * idea.lot_size * lots)}</b></div>
          </div>
          <div class="hint">Breakeven ${idea.breakevens.map((b) => num(b)).join(" / ")} · costs ≈ ${inr(idea.costs_per_lot)}/lot round trip</div>
          ${idea.blockers.length ? `<div class="blockers">Not entered: ${idea.blockers.map(esc).join(" · ")}</div>` : ""}
          <details data-key="${key}" ${openDetails.has(key) ? "open" : ""}><summary>Why this trade</summary><ul>${idea.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul></details>
          ${canTrade ? `<button class="primary" data-idea="${idea.id}" type="button">Paper trade ${idea.lots} lot${idea.lots === 1 ? "" : "s"}</button>` : ""}
        </article>`);
    });
  }
  $("#ideas").innerHTML = cards.join("") || `<div class="empty">Waiting for data…</div>`;
  $("#ideas").querySelectorAll("details").forEach((det) => {
    det.ontoggle = () => (det.open ? openDetails.add(det.dataset.key) : openDetails.delete(det.dataset.key));
  });
  $("#ideas").querySelectorAll("button[data-idea]").forEach((b) => (b.onclick = () => action(`/api/ideas/${b.dataset.idea}/paper`)));
}

function legsText(legs) {
  return legs.map((l) => `${l.side > 0 ? "B" : "S"} ${num(l.strike)}${l.option_type}`).join(" / ");
}

function renderPositions(s) {
  if (!s.positions.length) { $("#positions").innerHTML = `<div class="empty">No open positions</div>`; return; }
  $("#positions").innerHTML = `<table><thead><tr>
    <th>Opened</th><th>Symbol</th><th>Strategy</th><th>Legs</th><th class="num">Lots</th>
    <th class="num">Entry</th><th class="num">Now</th><th class="num">P&amp;L</th>
    <th class="num">Target</th><th class="num">Stop</th><th class="num">Short Δ</th><th class="num">Max loss</th><th></th></tr></thead><tbody>
    ${s.positions.map((p) => {
      const q = p.quantity;
      return `<tr><td>${dateTime(p.entry_time)}</td><td>${p.symbol}</td><td>${pretty(p.strategy)}<br><span class="hint">exp ${p.expiry}</span></td>
        <td>${legsText(p.legs)}</td><td class="num">${p.lots}</td>
        <td class="num">${num(p.entry_value, 2)}</td><td class="num">${num(p.current_value, 2)}</td>
        <td class="num ${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}</td>
        <td class="num">${signed(p.take_profit * q)}</td><td class="num">${signed(p.stop_loss * q)}</td>
        <td class="num">${num(p.max_short_delta, 2)}</td><td class="num">${inr(p.risk_inr)}</td>
        <td><button type="button" data-close="${p.id}">Close</button></td></tr>`;
    }).join("")}</tbody></table>`;
  $("#positions").querySelectorAll("button[data-close]").forEach((b) =>
    (b.onclick = () => action(`/api/positions/${b.dataset.close}/close`, "Close this paper position at market?")));
}

function renderEquity(s) {
  const el = $("#equity-chart");
  const pts = s.equity_curve;
  const st = s.stats;
  $("#stats").innerHTML = [
    metric("Net P&L", `<span class="${cls(st.total_pnl)}">${signed(st.total_pnl)}</span>`),
    metric("Profit factor", st.profit_factor == null ? "–" : num(st.profit_factor, 2)),
    metric("Avg win", inr(st.avg_win)), metric("Avg loss", st.avg_loss == null ? "–" : signed(st.avg_loss)),
    metric("Expectancy", st.expectancy == null ? "–" : signed(st.expectancy)), metric("Max drawdown", inr(st.max_drawdown)),
  ].join("");
  if (pts.length < 2) { el.innerHTML = `<div class="empty">The equity curve fills in as the bot runs</div>`; return; }
  $("#eq-hint").textContent = `${dateTime(pts[0].ts)} → ${dateTime(pts[pts.length - 1].ts)}`;
  lineChart(el, pts, s.risk.capital, "Paper account equity over time", dateTime);
}

function lineChart(el, pts, capital, label, fmtTs) {
  const W = 640, H = 220, m = { l: 64, r: 10, t: 12, b: 22 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const vals = pts.map((p) => p.equity).concat([capital]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo < 1) { hi += 500; lo -= 500; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const x = (i) => m.l + (i / (pts.length - 1)) * iw;
  const y = (v) => m.t + ih - ((v - lo) / (hi - lo)) * ih;
  const path = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.equity).toFixed(1)}`).join("");
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`;
  for (const f of [0, 0.5, 1]) {
    const v = lo + (hi - lo) * f;
    svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${inr(v)}</text>`;
  }
  svg += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(capital)}" y2="${y(capital)}" stroke="var(--text-muted)" stroke-dasharray="4 4"/>`;
  svg += `<text x="${m.l + 4}" y="${y(capital) + (y(capital) > m.t + ih - 16 ? -5 : 13)}">starting capital</text>`;
  svg += `<path d="${path}" fill="none" stroke="var(--series-1)" stroke-width="2" stroke-linejoin="round"/>`;
  svg += `<line class="cross" y1="${m.t}" y2="${m.t + ih}" stroke="var(--text-muted)" visibility="hidden"/>`;
  svg += `<circle class="dot" r="4" fill="var(--series-1)" stroke="var(--surface-1)" stroke-width="2" visibility="hidden"/>`;
  svg += `<rect class="hit" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  el.innerHTML = svg;
  const svgEl = el.querySelector("svg"), cross = el.querySelector(".cross"), dot = el.querySelector(".dot");
  const hit = el.querySelector(".hit");
  hit.onmousemove = (ev) => {
    const r = svgEl.getBoundingClientRect();
    const sx = ((ev.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(pts.length - 1, Math.round(((sx - m.l) / iw) * (pts.length - 1))));
    const p = pts[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(p.equity)); dot.setAttribute("visibility", "visible");
    showTip(`<b>${inr(p.equity)}</b><br>${fmtTs(p.ts)} · ${signed(p.equity - capital)} vs capital`, ev.clientX, ev.clientY);
  };
  hit.onmouseleave = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); hideTip(); };
}

function renderBacktest(s) {
  const panel = $("#backtest-panel");
  const bt = s.backtest;
  panel.hidden = !bt;
  if (!bt || panel.dataset.done) return; // static content: draw once
  panel.dataset.done = "1";
  const st = bt.stats;
  $("#bt-period").textContent = `${bt.period.start} → ${bt.period.end} · ${bt.symbols.join(", ")}`;
  $("#bt-kpis").innerHTML = [
    metric("Return", `<span class="${cls(bt.return_pct)}">${bt.return_pct >= 0 ? "+" : ""}${num(bt.return_pct, 2)}%</span>`, `CAGR ${num(bt.cagr_pct, 2)}%`),
    metric("Max drawdown", `${num(bt.max_drawdown_pct, 2)}%`, `of ${inr(bt.capital)} capital`),
    metric("Trades", num(st.trades), st.trades ? `${pct(st.win_rate, 0)} winners` : ""),
    metric("Profit factor", st.profit_factor == null ? "–" : num(st.profit_factor, 2), "gross wins ÷ gross losses"),
    metric("Avg win / loss", `${inr(st.avg_win)} / ${st.avg_loss == null ? "–" : inr(-st.avg_loss)}`),
  ].join("");
  lineChart($("#bt-chart"), bt.equity, bt.capital, "Backtest equity on real history", (t) => t.slice(0, 10));
  const rows = (bt.validation || []).map((r) => `<tr><td>${esc(r.label)}</td><td>${esc(r.period)}</td>
    <td class="num ${cls(r.return_pct)}">${r.return_pct >= 0 ? "+" : ""}${num(r.return_pct, 2)}%</td>
    <td class="num">${num(r.max_dd_pct, 2)}%</td><td class="num">${r.trades}</td><td class="num">${num(r.profit_factor, 2)}</td></tr>`).join("");
  $("#bt-validation").innerHTML = rows ? `<table><thead><tr><th>Settings</th><th>Period</th><th class="num">Return</th>
    <th class="num">Max DD</th><th class="num">Trades</th><th class="num">Profit factor</th></tr></thead><tbody>${rows}</tbody></table>` : "";
}

function renderEvents(s) {
  $("#events").innerHTML = s.events.length
    ? s.events.slice(0, 80).map((e) => `<li><time>${dateTime(e.time)}</time><span class="k">${esc(e.kind)}</span>${esc(e.message)}</li>`).join("")
    : `<li class="empty">No activity yet</li>`;
}

function renderClosed(s) {
  if (!s.closed.length) { $("#closed").innerHTML = `<div class="empty">No closed trades yet</div>`; return; }
  $("#closed").innerHTML = `<table><thead><tr><th>Closed</th><th>Symbol</th><th>Strategy</th><th>Legs</th><th class="num">Lots</th>
    <th class="num">Entry</th><th class="num">Exit</th><th class="num">Costs</th><th class="num">P&amp;L</th><th>Reason</th></tr></thead><tbody>
    ${s.closed.map((p) => `<tr><td>${dateTime(p.exit_time)}</td><td>${p.symbol}</td><td>${pretty(p.strategy)}</td><td>${legsText(p.legs)}</td>
      <td class="num">${p.lots}</td><td class="num">${num(p.entry_value, 2)}</td><td class="num">${num(p.exit_value, 2)}</td>
      <td class="num">${inr(p.entry_costs + p.exit_costs)}</td><td class="num ${cls(p.realized_pnl)}">${signed(p.realized_pnl)}</td>
      <td>${esc(p.exit_reason)}</td></tr>`).join("")}</tbody></table>`;
}

if (DEMO) {
  DEMO.start(render);
} else {
  fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
  connect();
}
