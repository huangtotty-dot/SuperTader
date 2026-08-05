/* 做T复盘决策看板 - 前端渲染逻辑 */
"use strict";

/* ================= 工具 ================= */
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}
function fmt(v, d) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (isNaN(n)) return esc(v);
  return n.toLocaleString("zh-CN", { maximumFractionDigits: d == null ? 2 : d });
}
function clsOf(v, pos = "up", neg = "down") {
  if (v === null || v === undefined) return "neutral";
  const n = Number(v);
  if (isNaN(n) || n === 0) return "neutral";
  return n > 0 ? pos : neg;
}
function dayTypeBadge(t) {
  if (!t) return "";
  const map = { bull_day: "bull", bear_day: "bear", chop_day: "chop", reversal_day: "reversal" };
  const cls = map[t] || "chop";
  const label = { bull_day: "bull", bear_day: "bear", chop_day: "chop", reversal_day: "reversal" }[t] || t;
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}
function verdictBadge(v) {
  const map = { signal: "signal", approaching: "approach", weak: "weak", insufficient_data: "nodata" };
  const label = { signal: "signal", approaching: "approaching", weak: "weak", insufficient_data: "无数据" }[v] || v;
  return `<span class="badge ${map[v] || 'weak'}">${esc(label)}</span>`;
}
function wrCell(w) {
  if (!w) return `<span class="cell-dim">—</span>`;
  const color = (w.wr === null || w.wr === undefined) ? "neutral"
    : (w.wr >= 0.5 ? "up" : "down");
  const wrTxt = w.wr === null || w.wr === undefined ? "—" : Math.round(w.wr * 100) + "%";
  return `<span class="num ${color}">${wrTxt}</span> <span class="cell-dim">${w.wins}W/${w.fails}F/${w.n}</span>`;
}
function statusEl(msg, cls) {
  const el = document.getElementById("status");
  el.className = "status " + (cls || "");
  el.textContent = msg;
}
function nowTime() {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/* ================= 数据加载 ================= */
let state = { date: null, payload: null, trend: [] };

async function apiCall(name, ...args) {
  if (!window.pywebview || !window.pywebview.api) {
    throw new Error("未运行在 pywebview 环境（请用 python t_gui.py 启动）");
  }
  return await window.pywebview.api[name](...args);
}

async function loadAndRender(date, silent) {
  if (!silent) statusEl("加载中...");
  try {
    const payload = await apiCall("load_day", date);
    if (payload.error) {
      statusEl(payload.error, "err");
      return;
    }
    state.date = payload.date;
    state.payload = payload;
    renderAll(payload);

    // 大盘趋势 + 成本历史（静态，一次拉取）
    apiCall("load_market_score", date).then(ms => renderMarket(ms || {})).catch(() => {});
    apiCall("load_cost_history").then(ch => renderCost(ch || {})).catch(() => {});

    // K4 跨日趋势
    apiCall("kpi_trend", 10).then(pts => {
      state.trend = pts || [];
      renderK4Trend(pts || []);
    }).catch(() => {});

    // 今日则进入实时模式
    const isToday = date === todayStr();
    if (isToday) {
      await refreshLive(true);       // 立即拉一次 live + console
      startLivePoll();               // 10s 实时
      startSignalPoll();             // 5s 信号检测 + 报警
      ensureAudio();                 // 尝试解锁音频
    } else {
      stopLivePoll();
      stopSignalPoll();
      renderLive(null, false);       // 显示"非今日"
      consoleBuf = []; consoleOffset = 0; consoleDate = null;
    }
    statusEl(`已加载 ${payload.date}${isToday ? " · 实时模式" : ""} · ${nowTime()}`, "ok");
  } catch (e) {
    statusEl("加载失败: " + e.message, "err");
  }
}

function todayStr() {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

async function refreshLive(reset) {
  const date = state.date;
  if (!date) return;
  try {
    const live = await apiCall("load_live", date);
    renderLive(live, date === todayStr());

    // console 增量拉取（reset=true 时从头读）
    const since = reset ? 0 : consoleOffset;
    const c = await apiCall("load_console", date, since);
    if (reset) { consoleBuf = []; consoleOffset = 0; consoleDate = date; }
    if (c.exists && c.lines && c.lines.length) {
      consoleOffset = c.offset;
      consoleDate = date;
      const keyOnly = (document.getElementById("consoleKeyOnly") || {}).checked !== false;
      appendConsole(c.lines, keyOnly);
    }
  } catch (e) {
    // 静默：实时轮询失败不影响主界面
  }
}

/* ================= 各区块渲染 ================= */
function renderAll(p) {
  renderKPI(p.kpi);
  renderSignalBar(p.sig_stat, p.name_map);
  renderPositions(p.positions, p.name_map);
  renderSettle(p.settle, p.sig_stat, p.name_map);
  renderShadow(p.shadow, p.qty_freeze);
  renderPB(p.position_builder);
  renderAddWatch(p.add_watch);
  renderStageBoard(p.stage_board);
  renderReport(p.report_md);
}

/* ---- 大盘趋势打分 ---- */
function renderMarket(ms) {
  const el = document.getElementById("marketBody");
  if (!ms || (!ms.history || !ms.history.length) && (!ms.intraday || !ms.intraday.length)) {
    el.innerHTML = '<div class="empty">无大盘趋势数据</div>';
    return;
  }
  let chips = "";
  if (ms.last_regime) {
    chips += `<span class="score-chip">大盘状态: <span class="regime-chip regime-${clsOf(ms.last_regime, "uni_up", "uni_down") === "down" ? "uni_down" : (ms.last_regime === "range" ? "range" : "uni_up")}">${esc(ms.last_regime)}</span></span>`;
  }
  if (ms.days_in_regime) chips += `<span class="score-chip">持续 ${ms.days_in_regime} 天</span>`;

  // 跨日 S 曲线（history）
  let histHtml = "";
  const hist = ms.history || [];
  if (hist.length >= 2) {
    histHtml = lineChart(hist, {
      x: h => h.date, y: h => h.S, yLabel: "S打分",
      color: h => (h.regime === "uni_up" ? "#f85149" : h.regime === "uni_down" ? "#3fb950" : h.regime === "range" ? "#d29922" : "#58a6ff"),
      xLabel: h => h.date.slice(5), title: h => `${h.date}  S=${fmt(h.S, 1)}  ${h.regime || ""}`,
    });
  } else {
    histHtml = '<div class="empty">跨日历史不足 2 点</div>';
  }

  // 今日盘中曲线（intraday）
  let intradayHtml = "";
  const iday = ms.intraday || [];
  if (iday.length >= 2) {
    intradayHtml = `<div class="card-title" style="margin-top:12px">今日盘中 S 变化（${iday.length} 点）</div>` +
      lineChart(iday, {
        x: p => p.time, y: p => p.score, yLabel: "S",
        color: p => (p.regime === "uni_up" ? "#f85149" : p.regime === "uni_down" ? "#3fb950" : "#58a6ff"),
        xLabel: p => (p.time || "").slice(0, 5), title: p => `${p.ts}  S=${fmt(p.score, 1)}  ${p.regime_name || ""}`,
      });
  } else if (iday.length === 1) {
    intradayHtml = `<div class="card-title" style="margin-top:12px">今日盘中：${esc(iday[0].ts)} S=${fmt(iday[0].score, 1)}</div>`;
  }

  el.innerHTML = `
    <div class="card">
      ${chips}
      <div class="card-title" style="margin-top:8px">跨日 S 打分（state.json history）</div>
      ${histHtml}
      ${intradayHtml}
      <div class="cell-dim" style="font-size:11px;margin-top:8px">S 为大盘态势打分（正=偏多 / 负=偏空）；选中今天时曲线随盘中 10s 刷新</div>
    </div>`;
}

/* ---- 持仓成本变化 ---- */
function renderCost(ch) {
  const el = document.getElementById("costBody");
  if (!ch || !ch.dates || !ch.dates.length) {
    el.innerHTML = '<div class="empty">无持仓成本历史（t_io/state 无快照）</div>';
    return;
  }
  const dates = ch.dates;
  const stocks = ch.stocks || {};
  const codes = Object.keys(stocks);

  // 折线（每股一条，最多 8 只）
  const palette = ["#58a6ff", "#f85149", "#3fb950", "#d29922", "#bc8cff", "#39c5cf", "#ff7b72", "#7ee787"];
  const codeColor = {};
  codes.forEach((c, i) => codeColor[c] = palette[i % palette.length]);

  let chartHtml = "";
  if (codes.length && dates.length >= 2) {
    const W = 720, H = 260, pad = 44, padT = 12, padB = 26;
    const iw = W - pad * 2, ih = H - padT - padB;
    const allCosts = codes.flatMap(c => stocks[c].points.map(p => p.cost));
    const minC = Math.min(...allCosts), maxC = Math.max(...allCosts);
    const span = (maxC - minC) || 1;
    const xs = dates.map((_, i) => pad + (dates.length === 1 ? iw / 2 : i / (dates.length - 1) * iw));
    const yOf = v => padT + (1 - (v - minC) / span) * ih;
    let g = "";
    [minC + span * .25, minC + span * .5, minC + span * .75].forEach(v => {
      const y = yOf(v);
      g += `<line x1="${pad}" y1="${y}" x2="${W - pad}" y2="${y}" stroke="rgba(139,148,158,.15)"/>`;
      g += `<text x="${pad - 6}" y="${y + 3}" fill="#8b949e" font-size="9" text-anchor="end">${fmt(v, 1)}</text>`;
    });
    const xl = dates.map((d, i) => `<text x="${xs[i]}" y="${H - 8}" fill="#8b949e" font-size="9" text-anchor="middle">${esc(d.slice(5))}</text>`).join("");
    const lines = codes.map(code => {
      const st = stocks[code];
      const pts = dates.map((d, i) => {
        const p = st.points.find(x => x.date === d);
        return p ? `${xs[i]},${yOf(p.cost)}` : "";
      }).filter(Boolean);
      if (pts.length < 1) return "";
      const dots = st.points.map(p => {
        const i = dates.indexOf(p.date);
        return `<circle cx="${xs[i]}" cy="${yOf(p.cost)}" r="3" fill="${codeColor[code]}"><title>${esc(st.name)} ${p.date} ${fmt(p.cost, 3)}</title></circle>`;
      }).join("");
      return `<polyline points="${pts.join(" ")}" fill="none" stroke="${codeColor[code]}" stroke-width="2" opacity=".9"/>${dots}`;
    }).join("");
    const legend = codes.map(code =>
      `<span style="margin-right:12px;font-size:12px;color:var(--text-dim)"><span class="dot" style="background:${codeColor[code]}"></span>${esc(stocks[code].name || code)}</span>`).join("");
    chartHtml = `<div class="legend">${legend}</div>
      <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;">${g}${xl}${lines}</svg>`;
  } else {
    chartHtml = '<div class="empty">快照天数不足 2 天，仅看矩阵</div>';
  }

  // 成本矩阵表
  const head = `<tr><th>股票</th>${dates.map(d => `<th class="num">${esc(d.slice(5))}</th>`).join("")}</tr>`;
  const rows = codes.map(code => {
    const st = stocks[code];
    return `<tr><td>${esc(st.name || code)} <span class="mono cell-dim">${esc(code)}</span></td>` +
      dates.map(d => {
        const p = st.points.find(x => x.date === d);
        return `<td class="num">${p ? fmt(p.cost, 3) : '<span class="cell-dim">—</span>'}</td>`;
      }).join("") + `</tr>`;
  }).join("");

  el.innerHTML = `
    <div class="card">
      ${chartHtml}
      <div class="card-title" style="margin-top:14px">成本矩阵（每日收盘快照，随复盘累积）</div>
      <table><thead>${head}</thead><tbody>${rows}</tbody></table>
    </div>`;
}

/* 通用 SVG 折线 */
function lineChart(points, o) {
  const W = 720, H = 240, pad = 44, padT = 12, padB = 26;
  const iw = W - pad * 2, ih = H - padT - padB;
  const vals = points.map(o.y).filter(v => v !== null && v !== undefined);
  if (!vals.length) return '<div class="empty">无有效数值</div>';
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const span = (maxV - minV) || 1;
  const padY = span * .15;
  const yMin = minV - padY, yMax = maxV + padY;
  const xs = points.map((p, i) => pad + (points.length === 1 ? iw / 2 : i / (points.length - 1) * iw));
  const yOf = v => padT + (1 - (v - yMin) / (yMax - yMin)) * ih;
  let g = "";
  for (let f = .25; f <= .75; f += .25) {
    const v = yMin + (yMax - yMin) * f;
    const y = yOf(v);
    g += `<line x1="${pad}" y1="${y}" x2="${W - pad}" y2="${y}" stroke="rgba(139,148,158,.15)"/>`;
    g += `<text x="${pad - 6}" y="${y + 3}" fill="#8b949e" font-size="9" text-anchor="end">${fmt(v, 1)}</text>`;
  }
  const line = points.map((p, i) => `${xs[i]},${yOf(o.y(p))}`).join(" ");
  const dots = points.map((p, i) =>
    `<circle cx="${xs[i]}" cy="${yOf(o.y(p))}" r="3" fill="${o.color(p)}"><title>${esc(o.title ? o.title(p) : "")}</title></circle>`).join("");
  const xl = points.map((p, i) =>
    `<text x="${xs[i]}" y="${H - 8}" fill="#8b949e" font-size="9" text-anchor="middle" transform="${points.length > 18 ? 'rotate(-45,' + xs[i] + ',' + (H - 8) + ')' : ''}">${esc(o.xLabel(p))}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;min-height:${H}px;">${g}${xl}<polyline points="${line}" fill="none" stroke="#58a6ff" stroke-width="2"/>${dots}</svg>`;
}

/* ---- 盘中实时 ---- */
function renderLive(live, isToday) {
  const el = document.getElementById("liveBody");
  const tag = document.getElementById("liveTag");
  if (!isToday) {
    el.innerHTML = '<div class="empty">选择今天（' + state.date + '）查看盘中实时数据</div>';
    tag.style.display = "none";
    return;
  }
  tag.style.display = "";
  tag.textContent = "LIVE";

  const liveState = live.intraday_state || {};
  const trends = liveState.trend_regimes || {};
  const cycle = liveState.cycle_count || {};
  const buyCount = liveState.buy_count || {};
  const sellCount = liveState.sell_count || {};
  const buyCd = liveState.buy_cooldown || {};
  const sellCd = liveState.sell_cooldown || {};

  // 盘中状态卡
  const trendCards = Object.keys(trends).map(code => {
    const t = trends[code];
    const st = t.state || "—";
    const cls = st === "STRONG_BULL" || st === "BULL" ? "regime-uni_up"
      : st === "STRONG_BEAR" || st === "BEAR" ? "regime-uni_down"
      : "regime-range";
    const bCnt = buyCount[code] || 0, sCnt = sellCount[code] || 0;
    const bc = buyCd[code], sc = sellCd[code];
    const cdTxt = [];
    if (bc) cdTxt.push(`买冷却 <span class="cool-tag">${esc(String(bc).slice(11, 19) || bc)}</span>`);
    if (sc) cdTxt.push(`卖冷却 <span class="cool-tag">${esc(String(sc).slice(11, 19) || sc)}</span>`);
    return `
      <div class="card" style="margin-bottom:8px;padding:10px 14px">
        <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:6px">
          <div><b>${esc(code)}</b> <span class="regime-chip-live ${cls}">${esc(st)}</span>
            <span class="cell-dim">conf ${fmt(t.confidence, 2)} · RSI ${fmt(t.last_rsi, 0)}</span></div>
          <div class="cell-dim mono">买×${bCnt} 卖×${sCnt} ${cdTxt.join(" ")}</div>
        </div>
      </div>`;
  }).join("");

  // 实时信号流
  const signals = live.signals || [];
  const sigRows = signals.map(s => {
    const dec = s.decision || "HOLD";
    const isSig = dec !== "HOLD";
    return `
      <tr class="${isSig ? "live-signal-row sig" : ""}">
        <td class="mono">${esc((s.scan_time || "").slice(11, 19))}</td>
        <td>${esc(s.name || s.code || "")} <span class="mono cell-dim">${esc(s.code || "")}</span></td>
        <td class="num">${fmt(s.price, 3)}</td>
        <td class="num ${clsOf((s.buy_score || 0) - 36)}">${fmt(s.buy_score, 1)}</td>
        <td class="num ${clsOf((s.sell_score || 0) - 55)}">${fmt(s.sell_score, 1)}</td>
        <td><span class="badge ${isSig ? "signal" : "chop"}">${esc(dec)}</span></td>
        <td class="cell-dim" style="font-size:11px">${esc(s.reason || "")}</td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="live-grid">
      <div class="card">
        <div class="card-title">盘中状态（intraday_state.json · 10s 刷新）</div>
        ${trendCards || '<div class="empty">无趋势状态</div>'}
      </div>
      <div class="card">
        <div class="card-title">实时信号流（decision_trace 尾部 20）· 信号 ${signals.filter(s => s.decision !== "HOLD").length} 条</div>
        <table>
          <thead><tr><th>时间</th><th>股票</th><th class="num">价</th><th class="num">买分</th><th class="num">卖分</th><th>决策</th><th>原因</th></tr></thead>
          <tbody>${sigRows || '<tr><td colspan="7" class="empty">暂无信号（盘中数据写入中）</td></tr>'}</tbody>
        </table>
      </div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="console-toggle">
        <span>实时 Console</span>
        <label><input type="checkbox" id="consoleKeyOnly" checked> 仅关键行</label>
        <span class="console-meta" id="consoleMeta"></span>
      </div>
      <div class="console-box" id="consoleBox"></div>
    </div>`;

  // 绑定 console 过滤切换
  const keyOnly = document.getElementById("consoleKeyOnly");
  if (keyOnly) keyOnly.addEventListener("change", () => reflowConsole());
}

/* ================= 报警音 + 闪烁横幅 ================= */
let audioCtx = null;
function ensureAudio() {
  try {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) audioCtx = new AC();
    }
    if (audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  } catch (e) { /* 无音频环境静默 */ }
  return audioCtx;
}

function beepTone(ctx, freq, when, dur) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "square";
  osc.frequency.value = freq;
  gain.gain.setValueAtTime(0.25, when);
  gain.gain.exponentialRampToValueAtTime(0.001, when + dur);
  osc.connect(gain); gain.connect(ctx.destination);
  osc.start(when); osc.stop(when + dur + 0.02);
}

function playAlert(decision) {
  try {
    const sw = document.getElementById("alertSound");
    if (sw && !sw.checked) return;   // 报警音开关关闭
    const ctx = ensureAudio();
    if (!ctx) return;
    const t0 = ctx.currentTime + 0.02;
    if (decision === "PANIC_SELL") {      // 五连最长促音（660Hz）
      for (let i = 0; i < 5; i++) beepTone(ctx, 660, t0 + i * 0.2, 0.14);
    } else if (decision === "SELL_HIGH") { // 高频三连（880Hz）
      for (let i = 0; i < 3; i++) beepTone(ctx, 880, t0 + i * 0.2, 0.12);
    } else {                               // 买入/加仓 低频三连（440Hz）
      for (let i = 0; i < 3; i++) beepTone(ctx, 440, t0 + i * 0.2, 0.12);
    }
  } catch (e) { /* 音频异常不影响横幅 */ }
}

const DEC_CN = { SELL_HIGH: "卖出", BUY_LOW: "买入", ADD_POS: "加仓", PANIC_SELL: "恐慌卖" };
const DEC_ICON = { SELL_HIGH: "🔴", BUY_LOW: "🟢", ADD_POS: "🔵", PANIC_SELL: "⛔" };

function pushAlert(s) {
  const banner = document.getElementById("alertBanner");
  if (!banner) return;
  banner.style.display = "flex";
  const time = (s.scan_time || "").slice(11, 19) || "";
  const item = document.createElement("div");
  item.className = "alert-item";
  const label = DEC_CN[s.decision] || s.decision;
  item.innerHTML = `
    <span>${DEC_ICON[s.decision] || "🔔"} ${label}信号 ${esc(s.name || s.code || "")}(${esc(s.code || "")})
    <span style="font-size:16px">${fmt(s.score, 1)}分</span> @ ${fmt(s.price, 3)} ${time}</span>
    <button class="close-btn" title="关闭">×</button>`;
  item.querySelector(".close-btn").addEventListener("click", () => {
    item.classList.add("fading");
    setTimeout(() => { item.remove(); if (!banner.children.length) banner.style.display = "none"; }, 800);
  });
  banner.appendChild(item);
  // 10s 自动淡出
  setTimeout(() => {
    if (item.isConnected) {
      item.classList.add("fading");
      setTimeout(() => { item.remove(); if (!banner.children.length) banner.style.display = "none"; }, 800);
    }
  }, 10000);
  // 报警音（方向区分）
  playAlert(s.decision);
}

/* ================= 增量信号轮询 ================= */
let sigTimer = null;
function startSignalPoll() {
  stopSignalPoll();
  sigTimer = setInterval(pollSignals, 5000);
}
function stopSignalPoll() {
  if (sigTimer) { clearInterval(sigTimer); sigTimer = null; }
}
async function pollSignals() {
  const date = state.date;
  if (!date) return;
  try {
    const r = await apiCall("poll_new_signals", date);
    if (r && !r.baseline && r.signals && r.signals.length) {
      r.signals.forEach(pushAlert);
    }
  } catch (e) { /* 静默 */ }
}

let consoleBuf = [];     // 全量行缓存
let consoleOffset = 0;
let consoleDate = null;

function appendConsole(rows, isKeyOnly) {
  consoleBuf.push(...rows);
  // 只保留最近 1000 行防内存
  if (consoleBuf.length > 1000) consoleBuf = consoleBuf.slice(-1000);
  reflowConsole(isKeyOnly);
}

function reflowConsole(isKeyOnly) {
  const box = document.getElementById("consoleBox");
  if (!box) return;
  const keyOnly = isKeyOnly !== undefined ? isKeyOnly
    : (document.getElementById("consoleKeyOnly") || {}).checked;
  const meta = document.getElementById("consoleMeta");
  let shown = consoleBuf;
  if (keyOnly) shown = shown.filter(l => l.key);
  if (meta) meta.textContent = `显示 ${shown.length}/${consoleBuf.length} 行 · 偏移 ${consoleOffset}`;
  const auto = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
  box.innerHTML = shown.map(l => {
    const lvCls = l.level ? "lv-" + l.level : "";
    return `<span class="console-line ${lvCls} ${l.key ? "key" : ""}">` +
      `<span class="ct">${esc(l.t)}</span>` +
      (l.level ? `<span class="clv">[${esc(l.level)}]</span>` : "") +
      esc(l.msg) + `</span>`;
  }).join("");
  if (auto) box.scrollTop = box.scrollHeight;
}

/* ---- ① KPI ---- */
function renderKPI(kpi) {
  const el = document.getElementById("kpiCards");
  if (!kpi || Object.keys(kpi).length === 0) {
    el.innerHTML = '<div class="empty">无 KPI 数据</div>';
    return;
  }
  const K1 = kpi.K1_closed_pnl || {};
  const K2 = kpi.K2_cost_change || {};
  const K3 = kpi.K3_base_drift || {};
  const K4 = kpi.K4_rolling_wr || {};
  const K5 = kpi.K5_qty0_suppressed || {};

  // K1
  const pnl = K1.total_est_pnl;
  const k1Val = pnl === null || pnl === undefined
    ? `<span class="neutral">无闭环</span>`
    : `<span class="${clsOf(pnl)}">${fmt(pnl)}</span>`;
  const k1Sub = pnl !== null && pnl !== undefined && K1.by_code && Object.keys(K1.by_code).length
    ? Object.entries(K1.by_code).map(([c, v]) => `${c}: ${fmt(v)}`).join(" · ")
    : (K1.source ? "" : "当日 0 闭环");
  const k1Card = `
    <div class="kpi-card">
      <div class="kpi-label"><span>K1 闭环净盈亏</span></div>
      <div class="kpi-value">${k1Val}</div>
      <div class="kpi-sub">${esc(k1Sub || "")}</div>
    </div>`;

  // K2
  const k2rows = Object.entries(K2.by_code || {}).filter(([, v]) => v && Math.abs(v.delta || 0) > 0.0001);
  const k2Card = `
    <div class="kpi-card">
      <div class="kpi-label"><span>K2 成本变化</span><span class="flag">${k2rows.length ? k2rows.length + "只变动" : ""}</span></div>
      <div class="kpi-value" style="font-size:18px">${k2rows.length
        ? k2rows.map(([c, v]) => `${esc(c)} <span class="${clsOf(v.delta)}">${v.delta > 0 ? "+" : ""}${fmt(v.delta)}</span>`).join("<br>")
        : `<span class="neutral">无成本变动</span>`}</div>
      <div class="kpi-sub">${esc(K2.note || "")}</div>
    </div>`;

  // K3
  const driftTotal = K3.drift_total;
  const k3rows = Object.entries(K3.by_code || {}).filter(([, v]) => v && (v.drift || 0) !== 0);
  const k3Card = `
    <div class="kpi-card">
      <div class="kpi-label"><span>K3 底仓漂移</span><span class="flag">净 ${driftTotal === null || driftTotal === undefined ? "—" : (driftTotal > 0 ? "+" : "") + fmt(driftTotal, 0)}</span></div>
      <div class="kpi-value" style="font-size:18px">${k3rows.length
        ? k3rows.map(([c, v]) => `${esc(c)} <span class="${clsOf(v.drift)}">${v.drift > 0 ? "+" : ""}${fmt(v.drift, 0)}股</span> <span class="cell-dim">${esc(v.attribution || "")}</span>`).join("<br>")
        : `<span class="neutral">无底仓漂移</span>`}</div>
      <div class="kpi-sub">drift_total = ${fmt(driftTotal, 0)}</div>
    </div>`;

  // K4
  const buy = K4.buy || {}, sell = K4.sell || {};
  const buyWr = buy.wr === null || buy.wr === undefined ? "—" : Math.round(buy.wr * 100) + "%";
  const sellWr = sell.wr === null || sell.wr === undefined ? "—" : Math.round(sell.wr * 100) + "%";
  const buyCls = buy.wr === null || buy.wr === undefined ? "neutral" : (buy.wr >= 0.5 ? "up" : (buy.wr < 0.3 ? "warn" : "down"));
  const k4Card = `
    <div class="kpi-card">
      <div class="kpi-label"><span>K4 滚动胜率</span></div>
      <div class="kpi-value">买 <span class="${buyCls}">${buyWr}</span></div>
      <div class="kpi-sub">卖 <span class="${clsOf((sell.wr || 0) - 0.5) === "down" ? "down" : "up"}">${sellWr}</span> · 买 n=${buy.n || 0} / 卖 n=${sell.n || 0}</div>
      <div class="kpi-sub cell-dim">${esc((K4.days_covered || []).join(" ~ ") || "")}</div>
    </div>`;

  // K5
  const k5Card = `
    <div class="kpi-card">
      <div class="kpi-label"><span>K5 qty=0 拦截</span><span class="flag">${K5.total ? "需关注" : ""}</span></div>
      <div class="kpi-value ${K5.total ? "warn" : "neutral"}">${K5.total || 0}</div>
      <div class="kpi-sub">${Object.entries(K5.by_code || {}).map(([c, n]) => `${esc(c)}×${n}`).join(" · ") || "无拦截"}</div>
    </div>`;

  el.innerHTML = k1Card + k2Card + k3Card + k4Card + k5Card;
}

/* ---- ② 图表 ---- */
function renderSignalBar(sigStat, nameMap) {
  const el = document.getElementById("signalBar");
  const codes = Object.keys(sigStat || {});
  if (!codes.length) { el.innerHTML = '<div class="empty">无数据</div>'; return; }
  const maxVal = Math.max(1, ...codes.map(c =>
    Math.max(sigStat[c].buy_signals || 0, sigStat[c].sell_signals || 0)));
  el.innerHTML = codes.map(code => {
    const s = sigStat[code];
    const b = s.buy_signals || 0, sl = s.sell_signals || 0;
    const nm = (nameMap || {})[code] || code;
    return `
      <div class="bar-row">
        <div class="bar-label" title="${esc(code)}">${esc(nm)}</div>
        <div style="display:flex;gap:4px;align-items:center;">
          <div class="bar-track" style="flex:${b + 1};" title="买入信号 ${b}"><div class="bar-fill buy" style="width:${b / maxVal * 100}%;"><span class="cnt">${b}</span></div></div>
          <div class="bar-track" style="flex:${sl + 1};" title="卖出信号 ${sl}"><div class="bar-fill sell" style="width:${sl / maxVal * 100}%;"><span class="cnt">${sl}</span></div></div>
        </div>
      </div>`;
  }).join("") + `<div class="cell-dim" style="font-size:11px;margin-top:6px">绿=买入信号数 · 红=卖出信号数</div>`;
}

function renderK4Trend(points) {
  const el = document.getElementById("k4Trend");
  if (!points || !points.length) {
    el.innerHTML = '<div class="empty">无跨日数据</div>';
    return;
  }
  const W = 420, H = 180, pad = 30, padT = 14, padB = 24;
  const iw = W - pad * 2, ih = H - padT - padB;
  const n = points.length;
  const xs = points.map((_, i) => pad + (n === 1 ? iw / 2 : i / (n - 1) * iw));
  const yOf = wr => padT + (1 - (wr == null ? 0 : wr)) * ih;

  let g = "";
  [0, 0.25, 0.5, 0.75, 1].forEach(v => {
    const y = yOf(v);
    g += `<line x1="${pad}" y1="${y}" x2="${W - pad}" y2="${y}" stroke="rgba(139,148,158,.18)" stroke-dasharray="2,4"/>`;
    g += `<text x="${pad - 6}" y="${y + 3}" fill="#8b949e" font-size="9" text-anchor="end">${Math.round(v * 100)}%</text>`;
  });
  const xl = points.map((p, i) =>
    `<text x="${xs[i]}" y="${H - 6}" fill="#8b949e" font-size="9" text-anchor="middle">${esc(p.date.slice(5))}</text>`).join("");
  const buyLine = points.map((p, i) => `${xs[i]},${yOf(p.buy_wr)}`).join(" ");
  const sellLine = points.map((p, i) => `${xs[i]},${yOf(p.sell_wr)}`).join(" ");
  const buyDots = points.map((p, i) => `<circle cx="${xs[i]}" cy="${yOf(p.buy_wr)}" r="3" fill="#3fb950"><title>${esc(p.date)} 买 ${p.buy_wr == null ? "—" : Math.round(p.buy_wr * 100) + "%"}</title></circle>`).join("");
  const sellDots = points.map((p, i) => `<circle cx="${xs[i]}" cy="${yOf(p.sell_wr)}" r="3" fill="#f85149"><title>${esc(p.date)} 卖 ${p.sell_wr == null ? "—" : Math.round(p.sell_wr * 100) + "%"}</title></circle>`).join("");

  el.innerHTML = `
    <div class="legend">
      <span><span class="dot" style="background:#3fb950"></span>买胜率</span>
      <span><span class="dot" style="background:#f85149"></span>卖胜率</span>
    </div>
    <svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;min-height:200px;">
      ${g}${xl}
      <polyline points="${buyLine}" fill="none" stroke="#3fb950" stroke-width="2"/>${buyDots}
      <polyline points="${sellLine}" fill="none" stroke="#f85149" stroke-width="2"/>${sellDots}
    </svg>`;
}

/* ---- ③ 持仓对照 ---- */
function renderPositions(pos, nameMap) {
  const el = document.getElementById("positionsBody");
  const cur = (pos && pos.current) || {};
  const codes = Object.keys(cur);
  if (!codes.length) {
    el.innerHTML = '<div class="empty">无持仓数据</div>';
    return;
  }
  const tMode = (pos && pos.t_mode) || {};
  const k2b = ((pos && pos.k2) || {}).by_code || {};
  const k3b = ((pos && pos.k3) || {}).by_code || {};
  const prevDate = (pos && pos.prev_date) || "—";

  const rows = codes.map(code => {
    const s = cur[code];
    const nm = s.name || (nameMap || {})[code] || code;
    const tm = tMode[code];
    const tmBadge = tm ? `<span class="badge ${tm === "long" ? "t-long" : "t-short"}">${tm === "long" ? "正T" : "反T"}</span>` : "";
    const prev = (pos && pos.snapshot_prev && pos.snapshot_prev[code]) || {};
    const k2v = k2b[code], k3v = k3b[code];
    const drift = k3v ? k3v.drift : null;
    const costDelta = k2v ? k2v.delta : null;
    return `
      <tr>
        <td>${esc(nm)} <span class="mono cell-dim">${esc(code)}</span> ${tmBadge}</td>
        <td class="cell-dim">${esc(s.account || "")} ${esc(s.type || "")}</td>
        <td class="num">${fmt(s.qty, 0)}</td>
        <td class="num"><span class="hl">${fmt(s.t_qty, 0)}</span><span class="cell-dim">/base ${fmt(s.base, 0)}</span></td>
        <td class="num cell-dim">${fmt(prev.t_qty, 0)}</td>
        <td class="num ${drift === null ? "neutral" : clsOf(drift)}">${drift === null ? "—" : (drift > 0 ? "+" : "") + fmt(drift, 0)}</td>
        <td class="num cell-dim">${esc(k3v && k3v.attribution ? k3v.attribution : "—")}</td>
        <td class="num">${fmt(s.cost, 3)}</td>
        <td class="num cell-dim">${fmt(prev.cost, 3)}</td>
        <td class="num ${costDelta === null ? "neutral" : clsOf(costDelta)}">${costDelta === null ? "—" : (costDelta > 0 ? "+" : "") + fmt(costDelta, 3)}</td>
        <td class="num cell-dim">${fmt(s.pre_close, 3)}</td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="card">
      <table>
        <thead><tr>
          <th>股票</th><th>账户/类型</th><th class="num">持股</th><th class="num">T仓(base)</th>
          <th class="num">前日T仓</th><th class="num">K3漂移</th><th>归因</th>
          <th class="num">成本</th><th class="num">前成本</th><th class="num">K2Δ</th><th class="num">昨收</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="cell-dim" style="font-size:11px;margin-top:8px">
        当前 holdings.json vs 前日快照（${esc(prevDate)}）· T模式来自 t_mode.json（正T=先买后卖 / 反T=先卖后买）
      </div>
    </div>`;
}

/* ---- ④ 信号结算 ---- */
function renderSettle(settle, sigStat, nameMap) {
  const el = document.getElementById("settleBody");
  const byCode = (settle && settle.by_code) || {};
  const sigs = sigStat || {};
  const codes = Object.keys(sigs).length ? Object.keys(sigs)
    : Object.keys(byCode);
  if (!codes.length) {
    el.innerHTML = '<div class="empty">无信号数据</div>';
    return;
  }
  // 按买卖信号数总和排序
  codes.sort((a, b) =>
    ((sigs[b] ? (sigs[b].sell_signals || 0) + (sigs[b].buy_signals || 0) : 0))
    - ((sigs[a] ? (sigs[a].sell_signals || 0) + (sigs[a].buy_signals || 0) : 0)));

  const rows = codes.map(code => {
    const nm = (nameMap || {})[code] || code;
    const s = sigs[code] || {};
    const sc = byCode[code] || {};
    const buyW = sc.BUY_LOW, sellW = sc.SELL_HIGH;
    return `
      <tr>
        <td>${esc(nm)} <span class="mono cell-dim">${esc(code)}</span></td>
        <td>${dayTypeBadge(s.day_type)}</td>
        <td class="num">${s.buy_signals || 0}</td>
        <td class="num cell-dim">${fmt(s.max_buy_score, 1)}</td>
        <td class="num">${s.sell_signals || 0}</td>
        <td class="num cell-dim">${fmt(s.max_sell_score, 1)}</td>
        <td>${wrCell(buyW)}</td>
        <td>${wrCell(sellW)}</td>
        <td class="num ${clsOf(s["day_ret%"])}">${fmt(s["day_ret%"])}%</td>
        <td class="num cell-dim">${fmt(s["振幅%"])}%</td>
      </tr>`;
  }).join("");

  el.innerHTML = `
    <div class="card">
      <table>
        <thead><tr>
          <th>股票</th><th>日型</th>
          <th class="num">买信号</th><th class="num">买max分</th>
          <th class="num">卖信号</th><th class="num">卖max分</th>
          <th>买结算</th><th>卖结算</th>
          <th class="num">日涨跌</th><th class="num">振幅</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="cell-dim" style="font-size:11px;margin-top:8px">结算 = close-only 口径：WIN/FAIL 为该方向信号当日结算胜负 · 买max分/卖max分为全 tick 最高分</div>
    </div>`;
}

/* ---- ⑤ shadow + 仓控 ---- */
function renderShadow(shadow, qtyFreeze) {
  const el = document.getElementById("shadowBody");
  const total = (shadow && shadow.total) || 0;
  const near = (shadow && shadow.near) || {};
  const qf = qtyFreeze || {};
  const suppressed = qf.suppressed || {};
  const pushes = qf.pushes || [];
  const silent = qf.silent_sell || {};

  let nearRows = "";
  Object.entries(near).forEach(([code, arr]) => {
    (arr || []).forEach(n => {
      nearRows += `
        <tr>
          <td>${esc(code)}</td>
          <td><span class="badge ${n.action === "BUY_LOW" ? "t-long" : "t-short"}">${esc(n.action || "")}</span></td>
          <td class="num">${n.n}</td>
          <td class="mono cell-dim">${esc(n.span || "")}</td>
          <td class="num warn">${fmt(n.min_dist, 2)}</td>
          <td class="cell-dim">${esc(n.miss_reason || "")}</td>
        </tr>`;
    });
  });

  let suppRows = "";
  Object.entries(suppressed).forEach(([code, arr]) => {
    (arr || []).forEach(s => {
      suppRows += `
        <tr class="freeze-row">
          <td>${esc(code)}</td>
          <td class="mono">${esc(s.ts || "")}</td>
          <td><span class="badge ${s.action === "BUY_LOW" ? "t-long" : "t-short"}">${esc(s.action || "")}</span></td>
          <td class="num warn">${fmt(s.score, 1)}</td>
        </tr>`;
    });
  });

  let pushRows = "";
  (pushes || []).forEach(p => {
    pushRows += `
      <tr>
        <td class="mono">${esc(p.ts || "")}</td>
        <td>${esc(p.code || "")}</td>
        <td><span class="badge ${p.action === "BUY_LOW" ? "t-long" : "t-short"}">${esc(p.action || "")}</span></td>
      </tr>`;
  });

  let silentRows = "";
  Object.entries(silent).forEach(([code, v]) => {
    silentRows += `<tr><td>${esc(code)}</td><td class="num">${v.n}</td><td class="num cell-dim">${fmt(v.max_score, 1)}</td></tr>`;
  });

  el.innerHTML = `
    <div class="cols">
      <div class="card">
        <div class="card-title">shadow 近阈信号（差 3 分内未触发）· 当日 shadow 总数 <b>${total}</b></div>
        <table>
          <thead><tr><th>代码</th><th>方向</th><th class="num">条数</th><th>时段</th><th class="num">min_dist</th><th>原因</th></tr></thead>
          <tbody>${nearRows || '<tr><td colspan="6" class="empty">无近阈信号</td></tr>'}</tbody>
        </table>
      </div>
      <div class="card">
        <div class="card-title">仓控拦截（qty=0 / 达阈被压）</div>
        <table>
          <thead><tr><th>代码</th><th>时间</th><th>方向</th><th class="num">分数</th></tr></thead>
          <tbody>${suppRows || '<tr><td colspan="4" class="empty">无拦截</td></tr>'}</tbody>
        </table>
        <div class="card-title" style="margin-top:14px">当日飞书推送</div>
        <table>
          <thead><tr><th>时间</th><th>代码</th><th>方向</th></tr></thead>
          <tbody>${pushRows || '<tr><td colspan="3" class="empty">无推送</td></tr>'}</tbody>
        </table>
        <div class="card-title" style="margin-top:14px">低于推送阈静默（silent）</div>
        <table>
          <thead><tr><th>代码</th><th class="num">条数</th><th class="num">max分</th></tr></thead>
          <tbody>${silentRows || '<tr><td colspan="3" class="empty">无</td></tr>'}</tbody>
        </table>
      </div>
    </div>`;
}

/* ---- ⑥ 建仓扫描 ---- */
function renderPB(pb) {
  const el = document.getElementById("pbBody");
  if (!pb || !pb.has_data) {
    el.innerHTML = '<div class="empty">当日无建仓扫描记录（position_builder 未运行）</div>';
    return;
  }
  const counts = pb.counts || {};
  const condLabels = pb.cond_labels || {};
  const rows = (pb.rows || []).map(r => {
    const conds = r.conditions || {};
    const condStr = Object.keys(condLabels).map(k =>
      conds[k] ? `<span class="on">●</span>` : `<span class="off">○</span>`).join("");
    const condTitle = Object.keys(condLabels).map(k =>
      `${condLabels[k]}:${conds[k] ? "通过" : "未过"}`).join(" · ");
    return `
      <tr>
        <td>${esc(r.name || "")} <span class="mono cell-dim">${esc(r.code || "")}</span>
          ${r.in_holdings ? `<span class="badge hold">持仓</span>` : ""}</td>
        <td>${verdictBadge(r.verdict)}</td>
        <td class="num"><b>${r.composite_score}</b></td>
        <td class="num">${fmt(r.price)}</td>
        <td><span class="cond" title="${esc(condTitle)}">${condStr}</span></td>
        <td class="num">${fmt(r.suggested_qty, 0)}</td>
        <td class="num">${fmt(r.suggested_price)}</td>
        <td class="num">${fmt(r.capital_required, 0)}</td>
        <td class="cell-dim">${esc(r.scan_type || "")}${r._scans > 1 ? `×${r._scans}` : ""}</td>
      </tr>`;
  }).join("");

  const c = k => counts[k] ? `<span class="badge ${k === "signal" ? "signal" : k === "approaching" ? "approach" : "weak"}">${k}: ${counts[k]}</span>` : "";
  el.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap;">
        ${c("signal")}${c("approaching")}${c("weak")}${c("insufficient_data")}
      </div>
      <table>
        <thead><tr>
          <th>股票</th><th>判定</th><th class="num">得分</th><th class="num">价</th>
          <th title="MACD/BOLL/RSI/缩量/支撑">五条件</th>
          <th class="num">建议股数</th><th class="num">建议价</th><th class="num">所需资金</th><th>扫描</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="9" class="empty">无扫描结果</td></tr>'}</tbody>
      </table>
      <div class="cell-dim" style="font-size:11px;margin-top:8px">●=通过 ○=未通过 · 五条件顺序：${Object.values(condLabels).join(" / ")} · 每条件 20 分，≥70 signal</div>
    </div>`;
}

/* ---- ⑦ 加仓观察 ---- */
function renderAddWatch(aw) {
  const el = document.getElementById("addWatchBody");
  const codes = Object.keys(aw || {});
  if (!codes.length) {
    el.innerHTML = '<div class="empty">无加仓观察数据</div>';
    return;
  }
  const cards = codes.map(code => {
    const w = aw[code];
    const supports = w.supports || {};
    const chips = Object.entries(supports).map(([k, v]) =>
      `<span class="support-chip" title="${esc(k)}">${esc(k)} ${fmt(v, 3)}</span>`).join("");
    const events = (w.events || []).map(e =>
      `<span class="badge event-${e.status || ""}">${esc(e.status || "")}·${esc(e.level || "")}@${fmt(e.support, 3)}（距${fmt(e["dist%"], 2)}%）</span>`).join(" ") || "—";
    const near = (w.near || []).map(n =>
      `<span class="badge event-${n.type === "刺穿破位" ? "破位" : n.type === "刺穿收回" ? "刺穿" : n.type === "临近未触" ? "临近" : ""}">${esc(n.type || "")}·${esc(n.level || "")}@${fmt(n.support, 3)}（${fmt(n["dist%"], 2)}%）</span>`).join(" ") || "—";
    return `
      <div class="card" style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:8px">
          <div><b>${esc(w.name || code)}</b> <span class="mono cell-dim">${esc(code)}</span></div>
          <div class="cell-dim mono">日低 ${fmt(w.day_low, 3)} · 收 ${fmt(w.close, 3)} · VWAP ${fmt(w.vwap, 3)}</div>
        </div>
        <div style="margin-bottom:6px"><span class="cell-dim">支撑位：</span>${chips}</div>
        <div style="margin-bottom:4px"><span class="cell-dim">回踩事件：</span>${events}</div>
        <div><span class="cell-dim">素材：</span>${near}</div>
      </div>`;
  }).join("");
  el.innerHTML = cards;
}

/* ---- ⑧ 阶段看板 ---- */
function renderStageBoard(stages) {
  const el = document.getElementById("stageBoardBody");
  if (!stages || !stages.length) {
    el.innerHTML = '<div class="empty">无阶段看板数据</div>';
    return;
  }
  const zones = ["已验收", "观察中", "优化管线中", "待启动"];
  const grouped = zones.map(z => [z, stages.filter(s => s.zone === z)]);
  el.innerHTML = `<div class="stage-zones">` + grouped.map(([z, items]) => `
    <div class="zone-card zone-${z}">
      <div class="zone-title">${esc(z)} <span class="cell-dim">(${items.length})</span></div>
      ${items.map(it => `
        <div class="zone-item">
          <div class="zone-name">${esc(it.name)} <span class="zone-since">since ${esc(it.since || "")}</span></div>
          ${it.note ? `<div class="zone-note">${esc(it.note)}</div>` : ""}
        </div>`).join("")}
    </div>`).join("") + `</div>`;
}

/* ---- ⑨ 复盘报告 ---- */
function renderReport(md) {
  const el = document.getElementById("reportBody");
  if (!md) {
    el.innerHTML = '<div class="empty">当日无复盘报告</div>';
    return;
  }
  if (window.marked && window.marked.parse) {
    el.innerHTML = window.marked.parse(md);
  } else {
    el.innerHTML = `<pre>${esc(md)}</pre>`;
  }
}

/* ================= 初始化 ================= */
const dateSelect = document.getElementById("dateSelect");
const refreshBtn = document.getElementById("refreshBtn");
const autoPoll = document.getElementById("autoPoll");
let pollTimer = null;      // 60s 盘后轮询
let liveTimer = null;      // 10s 盘中实时轮询

function stopPoll() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}
function startPoll() {
  stopPoll();
  pollTimer = setInterval(() => {
    if (state.date) loadAndRender(state.date, true);
  }, 60000);
}
function stopLivePoll() {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  document.getElementById("liveTag").style.display = "none";
}
function startLivePoll() {
  stopLivePoll();
  document.getElementById("liveTag").style.display = "";
  document.getElementById("liveTag").textContent = "LIVE 10s";
  liveTimer = setInterval(() => {
    if (state.date) refreshLive(false);
  }, 10000);
}

async function init() {
  // marked 兜底：vendor 缺失时不影响其它区块
  dateSelect.addEventListener("change", () => {
    if (dateSelect.value) loadAndRender(dateSelect.value, false);
  });
  refreshBtn.addEventListener("click", () => {
    if (dateSelect.value) loadAndRender(dateSelect.value, false);
  });
  autoPoll.addEventListener("change", () => {
    if (autoPoll.checked) {
      startPoll();
      if (state.date === todayStr()) { startLivePoll(); startSignalPoll(); }
    } else {
      stopPoll();
      stopLivePoll();
      stopSignalPoll();
    }
  });
  // 首次用户交互解锁音频（autoplay 政策兜底）
  document.addEventListener("pointerdown", () => ensureAudio(), { once: true });

  let dates;
  try {
    dates = await apiCall("available_dates");
  } catch (e) {
    statusEl("无法连接后端：" + e.message + "（请用 python t_gui.py 启动）", "err");
    return;
  }
  if (!dates || !dates.length) {
    statusEl("未找到 daily_review_*.json，请先运行 daily_review.py", "err");
    return;
  }
  dates.forEach(d => {
    const opt = document.createElement("option");
    opt.value = d; opt.textContent = d;
    dateSelect.appendChild(opt);
  });
  dateSelect.value = dates[0];
  document.getElementById("dateTag").textContent = dates[0];

  await loadAndRender(dates[0], false);
  startPoll();
}

document.addEventListener("DOMContentLoaded", () => {
  // pywebview 桥接脚本可能在 DOMContentLoaded 之后才注入，等待 pywebviewready / 轮询兜底
  if (window.pywebview && window.pywebview.api) {
    init();
    return;
  }
  let booted = false;
  const doBoot = () => {
    if (booted) return;
    if (window.pywebview && window.pywebview.api) { booted = true; init(); }
  };
  window.addEventListener("pywebviewready", doBoot);
  const t = setInterval(() => { doBoot(); if (booted) clearInterval(t); }, 250);
  setTimeout(() => { if (!booted) { clearInterval(t); doBoot(); } }, 6000);
});
