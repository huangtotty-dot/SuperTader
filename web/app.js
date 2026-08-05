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

    // 行情条 + 大盘趋势 + 成本历史（静态，一次拉取）
    apiCall("load_quotes").then(q => renderQuotes(q || {}, null)).catch(() => {});
    apiCall("load_market_score", date).then(ms => renderMarket(ms || {})).catch(() => {});
    apiCall("load_cost_history").then(ch => { state.costHistory = ch || {}; renderCost(ch || {}); }).catch(() => {});

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

    // 实时行情条（10s 刷新）
    apiCall("load_quotes").then(q => renderQuotes(q || {}, null)).catch(() => {});
    // 今日盘中 S 曲线刷新
    if (live.market_intraday && live.market_intraday.length) {
      apiCall("load_market_score", date).then(ms => renderMarket(ms || {})).catch(() => {});
    }

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
}

/* ---- ECharts 辅助 ---- */
const echarts = window.echarts;
const echInstances = {};
function echRender(id, opt) {
  if (!echarts) return;
  let inst = echInstances[id];
  if (!inst) {
    const el = document.getElementById(id);
    if (!el) return;
    inst = echarts.init(el);
    echInstances[id] = inst;
  }
  inst.setOption(opt, { notMerge: true });
}
function echClear(id) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = '<div class="empty">无数据</div>';
  if (echInstances[id]) { echInstances[id].dispose(); delete echInstances[id]; }
}
window.addEventListener("resize", () => {
  Object.values(echInstances).forEach(i => i.resize());
});
const ECH_BASE = {
  backgroundColor: "transparent",
  textStyle: { color: "#8b949e" },
  grid: { left: 48, right: 20, top: 30, bottom: 34 },
  tooltip: { backgroundColor: "#161b22", borderColor: "#30363d", textStyle: { color: "#c9d1d9", fontSize: 12 } },
  xAxis: {
    axisLine: { lineStyle: { color: "#30363d" } },
    axisLabel: { color: "#8b949e", fontSize: 10 },
    axisTick: { show: false },
  },
  yAxis: {
    axisLine: { show: false },
    axisLabel: { color: "#8b949e", fontSize: 10 },
    splitLine: { lineStyle: { color: "rgba(139,148,158,.15)" } },
  },
};

/* ---- 大盘趋势打分 ---- */
function renderMarket(ms) {
  if (!ms || ((!ms.history || !ms.history.length) && (!ms.intraday || !ms.intraday.length))) {
    echClear("echMarket"); echClear("echIntraday");
    return;
  }
  // 更新卡片标题含大盘状态
  const card = document.getElementById("echMarket");
  if (card) {
    const t = card.closest(".card").querySelector(".card-title");
    if (t && ms.last_regime) t.textContent = `大盘跨日 S 打分 · ${ms.last_regime}${ms.days_in_regime ? " " + ms.days_in_regime + "天" : ""}`;
  }
  // 跨日 S
  const hist = ms.history || [];
  if (hist.length >= 2) {
    echRender("echMarket", {
      ...ECH_BASE,
      xAxis: { ...ECH_BASE.xAxis, type: "category", data: hist.map(h => h.date.slice(5)) },
      yAxis: { ...ECH_BASE.yAxis },
      tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: p => {
        const i = p[0].dataIndex, h = hist[i];
        return `${h.date}<br/>S=${fmt(h.S, 1)}${h.sadj != null ? ` · sadj=${fmt(h.sadj, 1)}` : ""}<br/>regime=${h.regime || "—"}`;
      }},
      series: [
        { name: "S", type: "line", data: hist.map(h => h.S), symbol: "circle", symbolSize: 5,
          lineStyle: { color: "#58a6ff", width: 2 }, itemStyle: { color: p => {
            const r = hist[p.dataIndex].regime;
            return r === "uni_up" ? "#f85149" : r === "uni_down" ? "#3fb950" : r === "range" ? "#d29922" : "#58a6ff";
          }} },
        { name: "Sadj", type: "line", data: hist.map(h => h.sadj != null ? h.sadj : null),
          lineStyle: { color: "rgba(139,148,158,.5)", width: 1, type: "dashed" }, symbol: "none" },
      ],
    });
  } else echClear("echMarket");

  // 今日盘中 S
  const iday = ms.intraday || [];
  if (iday.length >= 2) {
    echRender("echIntraday", {
      ...ECH_BASE,
      xAxis: { ...ECH_BASE.xAxis, type: "category", data: iday.map(p => (p.time || "").slice(0, 5)) },
      yAxis: { ...ECH_BASE.yAxis },
      tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: p => {
        const i = p[0].dataIndex, d = iday[i];
        return `${d.ts}<br/>S=${fmt(d.score, 1)}<br/>${d.regime_name || ""}`;
      }},
      series: [{ name: "盘中S", type: "line", data: iday.map(p => p.score), smooth: true,
        symbol: "circle", symbolSize: 4, lineStyle: { color: "#58a6ff", width: 2 },
        areaStyle: { color: "rgba(88,166,255,.15)" } }],
    });
  } else {
    echClear("echIntraday");
  }
}

/* ---- 持仓成本变化 ---- */
function renderCost(ch) {
  const dates = (ch && ch.dates) || [];
  const stocks = (ch && ch.stocks) || {};
  const codes = Object.keys(stocks);
  const palette = ["#58a6ff", "#f85149", "#3fb950", "#d29922", "#bc8cff", "#39c5cf", "#ff7b72", "#7ee787"];
  const codeColor = {};
  codes.forEach((c, i) => codeColor[c] = palette[i % palette.length]);

  // ECharts 成本多线
  if (codes.length && dates.length >= 2) {
    const series = codes.map(code => {
      const st = stocks[code];
      return {
        name: (st.name || code), type: "line", symbol: "circle", symbolSize: 5,
        itemStyle: { color: codeColor[code], borderWidth: 0 },
        lineStyle: { color: codeColor[code], width: 2 },
        data: dates.map(d => {
          const pt = st.points.find(x => x.date === d);
          return pt ? pt.cost : null;
        }),
      };
    });
    echRender("echCost", {
      ...ECH_BASE,
      legend: { type: "scroll", textStyle: { color: "#8b949e", fontSize: 10 }, top: 0 },
      xAxis: { ...ECH_BASE.xAxis, type: "category", data: dates.map(d => d.slice(5)) },
      yAxis: { ...ECH_BASE.yAxis },
      tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: params => {
        const d = dates[params[0].dataIndex];
        let html = `<b>${d}</b>`;
        params.forEach(p => {
          const code = codes[p.seriesIndex] || "";
          const pt = (stocks[code] || {}).points.find(x => x.date === d);
          html += `<br/>${p.marker}${p.seriesName}: ${fmt(p.value, 3)}${pt && pt.src ? ` <span style="color:#58a6ff">${pt.src}</span>` : ""}`;
        });
        return html;
      }},
      series,
    });
  } else echClear("echCost");

  // 成本矩阵
  const mtx = document.getElementById("costMatrix");
  if (!mtx) return;
  if (!codes.length || !dates.length) {
    mtx.innerHTML = '<div class="empty">无持仓成本历史（t_io/state 无快照）</div>';
    return;
  }
  const head = `<tr><th>股票</th>${dates.map(d => `<th class="num">${esc(d.slice(5))}</th>`).join("")}</tr>`;
  const rows = codes.map(code => {
    const st = stocks[code];
    return `<tr><td>${esc(st.name || code)} <span class="mono cell-dim">${esc(code)}</span></td>` +
      dates.map(d => {
        const p = st.points.find(x => x.date === d);
        if (!p) return `<td class="num cell-dim">—</td>`;
        const badge = p.src === "人工校准"
          ? `<span class="calib-badge 人工校准">✎校准</span>` : "";
        return `<td class="num">${fmt(p.cost, 3)}${badge}</td>`;
      }).join("") + `</tr>`;
  }).join("");
  mtx.innerHTML = `
    <div class="card-title" style="margin-top:8px">成本矩阵（✎校准=人工确认 · 其余=收盘快照）</div>
    <table><thead>${head}</thead><tbody>${rows}</tbody></table>`;
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
  const codes = Object.keys(sigStat || {});
  if (!codes.length) { echClear("echSignal"); return; }
  codes.sort((a, b) =>
    ((sigStat[b].sell_signals || 0) + (sigStat[b].buy_signals || 0))
    - ((sigStat[a].sell_signals || 0) + (sigStat[a].buy_signals || 0)));
  const names = codes.map(c => (nameMap || {})[c] || c);
  echRender("echSignal", {
    ...ECH_BASE,
    grid: { left: 90, right: 40, top: 20, bottom: 26 },
    xAxis: { type: "value", axisLabel: { color: "#8b949e", fontSize: 10 }, splitLine: { lineStyle: { color: "rgba(139,148,158,.15)" } } },
    yAxis: { type: "category", data: names, axisLine: { lineStyle: { color: "#30363d" } }, axisLabel: { color: "#8b949e", fontSize: 10 } },
    tooltip: { ...ECH_BASE.tooltip, trigger: "axis", axisPointer: { type: "shadow" } },
    series: [
      { name: "买入", type: "bar", data: codes.map(c => sigStat[c].buy_signals || 0),
        itemStyle: { color: "#3fb950", borderRadius: [0, 3, 3, 0] },
        label: { show: true, position: "right", color: "#8b949e", fontSize: 10 } },
      { name: "卖出", type: "bar", data: codes.map(c => sigStat[c].sell_signals || 0),
        itemStyle: { color: "#f85149", borderRadius: [0, 3, 3, 0] },
        label: { show: true, position: "right", color: "#8b949e", fontSize: 10 } },
    ],
  });
}

function renderK4Trend(points) {
  if (!points || !points.length) { echClear("echK4"); return; }
  echRender("echK4", {
    ...ECH_BASE,
    yAxis: { ...ECH_BASE.yAxis, min: 0, max: 1, axisLabel: { color: "#8b949e", fontSize: 10, formatter: v => Math.round(v * 100) + "%" } },
    xAxis: { ...ECH_BASE.xAxis, type: "category", data: points.map(p => p.date.slice(5)) },
    tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: params => {
      const p = points[params[0].dataIndex];
      let html = `<b>${p.date}</b>`;
      params.forEach(x => {
        const wr = x.seriesName === "买胜率" ? p.buy_wr : p.sell_wr;
        const n = x.seriesName === "买胜率" ? p.buy_n : p.sell_n;
        html += `<br/>${x.marker}${x.seriesName}: ${wr == null ? "—" : Math.round(wr * 100) + "%"} (n=${n || 0})`;
      });
      return html;
    }},
    series: [
      { name: "买胜率", type: "line", data: points.map(p => p.buy_wr), symbol: "circle", symbolSize: 5,
        lineStyle: { color: "#3fb950", width: 2 }, itemStyle: { color: "#3fb950" },
        markLine: { silent: true, symbol: "none", lineStyle: { color: "rgba(139,148,158,.4)", type: "dashed" },
          data: [{ yAxis: 0.5, label: { formatter: "50%", color: "#8b949e", fontSize: 9 } }] } },
      { name: "卖胜率", type: "line", data: points.map(p => p.sell_wr), symbol: "circle", symbolSize: 5,
        lineStyle: { color: "#f85149", width: 2 }, itemStyle: { color: "#f85149" } },
    ],
  });
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

/* ---- 行情条 ---- */
function renderQuotes(q, market) {
  const bar = document.getElementById("quoteBar");
  const body = document.getElementById("quotesBody");
  if (!q || !q.quotes || !q.quotes.length) {
    if (bar) bar.innerHTML = "";
    if (body) body.innerHTML = '<div class="empty">无持仓行情</div>';
    return;
  }
  const srcBadge = q.source === "live"
    ? `<span class="quote-src">${esc(q.ts)}</span>`
    : `<span class="quote-src warn">离线/回退昨收</span>`;
  const cells = q.quotes.map(x => {
    const chgCls = clsOf(x.change);
    const pnlTxt = x.pnl_pct == null ? "—" : `${x.pnl_pct >= 0 ? "+" : ""}${fmt(x.pnl_pct, 1)}%`;
    return `<span class="quote-cell">
      <span class="q-name">${esc(x.name)}</span><span class="q-code">${esc(x.code)}</span>
      <span class="q-price ${chgCls}">${fmt(x.price, 3)}</span>
      <span class="q-chg ${chgCls}">${x.change >= 0 ? "+" : ""}${fmt(x.change, 2)} ${x.change_pct >= 0 ? "+" : ""}${fmt(x.change_pct, 2)}%</span>
      <span class="q-cost">本 ${fmt(x.cost, 3)}</span>
      <span class="q-pnl ${clsOf(x.pnl_pct)}">${pnlTxt}</span>
    </span>`;
  }).join("");
  if (bar) bar.innerHTML = cells + srcBadge;
  if (body) body.innerHTML = `<div class="quote-cell" style="margin:0">${cells}</div>${srcBadge}
    <div class="cell-dim" style="font-size:11px;margin-top:6px">现价/涨跌来自腾讯实时行情 · 本=持仓成本 · 浮盈%=(现价-成本)/成本 · 盘中每 10s 刷新</div>`;
}

/* ---- 成本校准 modal ---- */
let costCalibData = null;  // {stocks, effective_today}

function openCalibModal(ch) {
  const modal = document.getElementById("calibModal");
  if (!modal) return;
  costCalibData = ch;
  document.getElementById("calibDate").textContent = "(" + todayStr() + ")";
  const codes = Object.keys((ch && ch.stocks) || {});
  const tbody = modal.querySelector("#calibTable tbody");
  tbody.innerHTML = codes.map(code => {
    const st = ch.stocks[code];
    const eff = (ch.effective_today || {})[code];
    const snap = st.points.length ? st.points[st.points.length - 1].cost : "";
    const srcBadge = eff && eff.src === "人工校准"
      ? `<span class="calib-badge 人工校准">人工校准</span>`
      : `<span class="calib-badge 快照">快照</span>`;
    return `<tr>
      <td>${esc(st.name || code)} <span class="mono cell-dim">${esc(code)}</span></td>
      <td class="num">${fmt(snap, 3)}</td>
      <td><input type="number" step="0.001" data-code="${esc(code)}" value="${eff ? eff.cost : ""}" placeholder="${fmt(snap, 3)}"></td>
      <td>${srcBadge}</td>
    </tr>`;
  }).join("");
  modal.style.display = "flex";
}

function closeCalibModal() {
  const modal = document.getElementById("calibModal");
  if (modal) modal.style.display = "none";
}

async function saveCalib() {
  const modal = document.getElementById("calibModal");
  const costs = {};
  modal.querySelectorAll("#calibTable tbody input[data-code]").forEach(inp => {
    const v = inp.value.trim();
    if (v !== "") {
      const n = parseFloat(v);
      if (!isNaN(n) && n > 0) costs[inp.dataset.code] = n;
    }
  });
  if (!Object.keys(costs).length) { closeCalibModal(); return; }
  try {
    const r = await apiCall("save_cost_calibration", todayStr(), costs);
    statusEl(r.ok ? "成本校准已保存" : "保存失败: " + (r.error || ""), r.ok ? "ok" : "err");
  } catch (e) {
    statusEl("保存失败: " + e.message, "err");
  }
  closeCalibModal();
  // 刷新成本曲线
  apiCall("load_cost_history").then(ch => {
    state.costHistory = ch || {};
    renderCost(ch || {});
  }).catch(() => {});
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
  dateSelect.addEventListener("change", () => {
    if (dateSelect.value) loadAndRender(dateSelect.value, false);
  });
  refreshBtn.addEventListener("click", () => {
    if (dateSelect.value) loadAndRender(dateSelect.value, false);
  });
  // 成本校准按钮
  const calibBtn = document.getElementById("calibBtn");
  if (calibBtn) calibBtn.addEventListener("click", () => {
    if (!state.costHistory) {
      apiCall("load_cost_history").then(ch => { state.costHistory = ch || {}; openCalibModal(ch || {}); }).catch(() => {});
    } else openCalibModal(state.costHistory);
  });
  const calibClose = document.getElementById("calibClose");
  if (calibClose) calibClose.addEventListener("click", closeCalibModal);
  const calibSave = document.getElementById("calibSave");
  if (calibSave) calibSave.addEventListener("click", saveCalib);
  document.getElementById("calibModal").addEventListener("click", e => {
    if (e.target.id === "calibModal") closeCalibModal();
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
