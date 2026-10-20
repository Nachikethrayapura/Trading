"use strict";
// Replay player for the standalone demo build. Plays recorded bot states through the normal
// dashboard renderer (app.js), rebuilding the equity curve, trade log and stats up to each frame.

window.OPTIONSBOT_DEMO = (() => {
  const data = JSON.parse(document.getElementById("demo-data").textContent);
  const frames = data.frames;
  let index = 0;
  let playing = true;
  let delay = 2000;
  let timer = null;
  let renderFn = null;

  function stats(closed, curve) {
    const pnls = closed.map((p) => p.realized_pnl || 0);
    const wins = pnls.filter((x) => x > 0);
    const losses = pnls.filter((x) => x <= 0);
    const sum = (xs) => xs.reduce((a, b) => a + b, 0);
    let peak = -Infinity, maxDd = 0;
    for (const p of curve) { peak = Math.max(peak, p.equity); maxDd = Math.max(maxDd, peak - p.equity); }
    return {
      trades: pnls.length, wins: wins.length, losses: losses.length,
      win_rate: pnls.length ? wins.length / pnls.length : null, total_pnl: sum(pnls),
      avg_win: wins.length ? sum(wins) / wins.length : null, avg_loss: losses.length ? sum(losses) / losses.length : null,
      profit_factor: losses.length && sum(losses) < 0 ? sum(wins) / -sum(losses) : null,
      expectancy: pnls.length ? sum(pnls) / pnls.length : null, max_drawdown: maxDd,
    };
  }

  function stateAt(k) {
    const f = frames[k];
    const t = f.time;
    const curve = data.equity.filter((p) => p.ts <= t);
    const closed = data.closed.filter((p) => p.exit_time <= t).reverse(); // newest first
    const events = data.events.filter((e) => e.time <= t);
    return { ...f, equity_curve: curve, closed: closed.slice(0, 50), events, stats: stats(closed, curve),
             paused: !playing, demo: true, backtest: data.backtest || null,
             recorded_span: [frames[0].time, frames[frames.length - 1].time] };
  }

  function show() {
    renderFn(stateAt(index));
    const range = document.getElementById("demo-pos");
    range.value = String(index);
    document.getElementById("demo-label").textContent =
      `${frames[index].time.slice(0, 10)} ${frames[index].time.slice(11, 16)} IST · tick ${index + 1}/${frames.length}`;
  }

  function schedule() {
    clearTimeout(timer);
    if (!playing) return;
    timer = setTimeout(() => {
      index = (index + 1) % frames.length;
      show();
      schedule();
    }, delay);
  }

  function mountBar() {
    const bar = document.createElement("div");
    bar.className = "demo-bar";
    bar.innerHTML = `
      <label for="demo-pos">Replay</label>
      <input id="demo-pos" type="range" min="0" max="${frames.length - 1}" value="0">
      <span id="demo-label" class="pos-label"></span>
      <label for="demo-speed">Speed</label>
      <select id="demo-speed">
        <option value="4000">Slow</option><option value="2000" selected>Normal</option><option value="600">Fast</option>
      </select>`;
    const main = document.querySelector("main");
    main.insertBefore(bar, main.firstChild);
    document.getElementById("demo-pos").oninput = (e) => { index = Number(e.target.value); show(); schedule(); };
    document.getElementById("demo-speed").onchange = (e) => { delay = Number(e.target.value); schedule(); };
  }

  return {
    start(render) {
      renderFn = render;
      mountBar();
      index = Math.max(0, Math.min(frames.length - 1, data.start_index || 0));
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) playing = false;
      show();
      schedule();
    },
    toggle() {
      playing = !playing;
      show();
      schedule();
    },
  };
})();
