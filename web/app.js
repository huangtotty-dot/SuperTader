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
    // 独立配置（账户总资金+已实现亏损），不随 holdings.json 更新丢失
    portfolioCfg = (payload.portfolio_config) || { accounts: {}, realized_loss: {} };
    renderAll(payload);

    // K1 做T盈亏（独立于 daily_review，读 closure_audit）
    apiCall("load_trade_pnl", date).then(tp => renderKPI(payload.kpi, tp || {})).catch(() => {});
    // 集合竞价 + 行情条 + 大盘趋势 + 成本历史（静态，一次拉取）
    apiCall("load_auction", date).then(a => renderAuction(a || {})).catch(() => {});
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

    // 建仓/加仓扫描表实时刷新（position_builder 盘中每5分钟有新数据）
    apiCall("refresh_pb", date).then(pb => renderPB(pb || {})).catch(() => {});
  } catch (e) {
    // 静默：实时轮询失败不影响主界面
  }
}

/* ================= 各区块渲染 ================= */
function renderAll(p, tradePnl) {
  renderKPI(p.kpi, tradePnl);
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
    // 不可见时也 init（ECharts 容错零宽），切换 tab 时 resize
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
// 渲染到概览(ech-sm)和图表tab(Full)两个容器
function tabEch(baseId, opt) {
  echRender(baseId, opt);
  echRender(baseId + "Full", opt);
}
function tabClear(baseId) {
  echClear(baseId);
  echClear(baseId + "Full");
}
// tab 切换时 resize 所有 ECharts
function resizeAllEch() {
  Object.values(echInstances).forEach(i => i.resize());
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
    tabClear("echMarket"); echClear("echIntraday");
    return;
  }
  // 跨日 S
  const hist = ms.history || [];
  if (hist.length >= 2) {
    tabEch("echMarket", {
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
  } else tabClear("echMarket");

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

/* ---- 持仓收益趋势 ---- */
function renderCost(ch) {
  const dates = (ch && ch.dates) || [];
  const stocks = (ch && ch.stocks) || {};
  const codes = Object.keys(stocks);
  const palette = ["#58a6ff","#f85149","#3fb950","#d29922","#bc8cff","#39c5cf","#ff7b72","#7ee787"];
  if (!codes.length) { tabClear("echCost"); document.getElementById("costMatrix").innerHTML = ""; return; }

  // 收益趋势图：每股 P&L 金额（元）
  if (dates.length >= 2) {
    const series = codes.map((code, i) => {
      const pts = stocks[code].points || [];
      const data = dates.map(d => {
        const p = pts.find(x => x.date === d);
        return p ? p.pnl_amt : null;
      });
      // 只画有点的股票
      if (data.every(v => v == null)) return null;
      return {
        name: (stocks[code].name || code), type: "line", symbol: "circle",
        symbolSize: 4, lineStyle: { color: palette[i % palette.length], width: 2 },
        itemStyle: { color: palette[i % palette.length] },
        data,
      };
    }).filter(Boolean);

    tabEch("echCost", {
      ...ECH_BASE,
      grid: { left: 56, right: 20, top: 40, bottom: 34 },
      legend: { type: "scroll", textStyle: { color: "#8b949e", fontSize: 10 }, top: 4 },
      xAxis: { ...ECH_BASE.xAxis, type: "category", data: dates.map(d => d.slice(5)) },
      yAxis: { ...ECH_BASE.yAxis, name: "元", nameTextStyle: { color: "#8b949e", fontSize: 10 },
        axisLabel: { color: "#8b949e", fontSize: 10, formatter: v => (v >= 0 ? "+" : "") + fmt(v, 0) } },
      tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: params => {
        const d = dates[params[0].dataIndex];
        let html = `<b>${d}</b>`;
        params.forEach(p => {
          const code = codes[p.seriesIndex] || "";
          const pt = (stocks[code] || {}).points.find(x => x.date === d);
          if (!pt) { html += `<br/>${p.marker}${p.seriesName}: —`; return; }
          const pnl = pt.pnl_amt, pct = pt.pnl_pct;
          const cls = pnl == null ? "" : (pnl >= 0 ? "color:#f85149" : "color:#3fb950");
          html += `<br/>${p.marker}${p.seriesName}: <span style="${cls}">${pnl != null ? (pnl >= 0 ? "+" : "") + fmt(pnl, 0) : "—"}</span>
            <span style="color:#8b949e;font-size:10px">${pct != null ? (pct >= 0 ? "+" : "") + fmt(pct, 1) + "%" : "—"} · 本${fmt(pt.cost, 3)} · ${pt.qty}股${pt.src === "人工校准" ? " ✎校准" : ""}</span>`;
        });
        return html;
      }},
      series,
    });
  } else tabClear("echCost");

  // 成本矩阵（保留）
  const mtx = document.getElementById("costMatrix");
  if (mtx && dates.length) {
    const longPeriod = dates.length > 30;
    const dateLabel = d => longPeriod ? (d.slice(0, 7).replace("-", "月") + "月") : d.slice(5);
    const head = `<tr><th>股票</th>${dates.map(d => `<th class="num">${esc(dateLabel(d))}</th>`).join("")}</tr>`;
    const rows = codes.map(code => {
      const st = stocks[code];
      return `<tr><td>${esc(st.name || code)} <span class="mono cell-dim">${esc(code)}</span></td>` +
        dates.map(d => {
          const p = st.points.find(x => x.date === d);
          if (!p) return `<td class="num cell-dim">—</td>`;
          const pnl = p.pnl_amt;
          const pnlCls = pnl == null ? "" : (pnl >= 0 ? "up" : "down");
          return `<td class="num"><span class="${pnlCls}">${pnl != null ? (pnl >= 0 ? "+" : "") + fmt(pnl, 0) : "—"}</span></td>`;
        }).join("") + `</tr>`;
    }).join("");
    const scrollWrap = dates.length > 30 ? 'style="overflow-x:auto;max-width:100%"' : "";
    mtx.innerHTML = `
      <div class="card-title" style="margin-top:10px">收益矩阵（浮盈/浮亏=昨收价×股数−成本×股数）<button class="mini-btn" id="calibBtn2">✎ 校准成本</button></div>
      <div class="cell-dim" style="font-size:10px;margin-bottom:4px">数据自 ${esc(dates[0])} 起（共${dates.length}天），持续累积中 · >30天切换月视图</div>
      <div ${scrollWrap}><table><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
    const cb2 = document.getElementById("calibBtn2");
    if (cb2) cb2.addEventListener("click", () => {
      if (!state.costHistory) {
        apiCall("load_cost_history").then(ch2 => { state.costHistory = ch2 || {}; openCalibModal(ch2 || {}); }).catch(() => {});
      } else openCalibModal(state.costHistory);
    });
  }

  // 输出：概览 tab 用 echCost 容器 + 图表 tab 用 echCostFull 容器
  const c1 = document.getElementById("echCost");
  const c2 = document.getElementById("echCostFull");
  const html = `<div class="cost-grid">${cards}</div>`;
  if (c1) c1.innerHTML = html;
  if (c2) c2.innerHTML = html;
}

/* ---- 盘中实时 ---- */
function renderLive(live, isToday) {
  const el = document.getElementById("liveBody");
  const tag = document.getElementById("liveTag");
  if (!isToday) {
    el.innerHTML = '<div class="empty">选择今天（' + state.date + '）查看盘中实时数据</div>';
    tag.style.display = "none";
    const prev = document.getElementById("livePreview");
    if (prev) prev.innerHTML = '<div class="empty">仅今日可用盘中实时数据</div>';
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
    const isNear = s.near && !isSig;  // 近阈但未触发
    const nearInfo = isNear
      ? `距买阈${fmt((s.buy_threshold||0)-(s.buy_score||0),0)}/卖阈${fmt((s.sell_threshold||0)-(s.sell_score||0),0)}` : "";
    return `
      <tr class="${isSig ? "live-signal-row sig" : isNear ? "live-signal-row near" : ""}">
        <td class="mono">${esc((s.scan_time || "").slice(11, 19))}</td>
        <td>${esc(s.name || s.code || "")} <span class="mono cell-dim">${esc(s.code || "")}</span></td>
        <td class="num">${fmt(s.price, 3)}</td>
        <td class="num ${clsOf((s.buy_score || 0) - (s.buy_threshold || 36))}">${fmt(s.buy_score, 1)}</td>
        <td class="num ${clsOf((s.sell_score || 0) - (s.sell_threshold || 55))}">${fmt(s.sell_score, 1)}</td>
        <td><span class="badge ${isSig ? "signal" : isNear ? "approach" : "chop"}">${isNear ? "接近" : esc(dec)}</span></td>
        <td class="cell-dim" style="font-size:11px">${esc(nearInfo || s.reason || "")}</td>
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

  // 概览仪表盘简版
  const prev = document.getElementById("livePreview");
  if (prev) {
    const sigs = live.signals || [];
    const nonHolds = sigs.filter(s => s.decision !== "HOLD").slice(0, 5);
    const trends = (live.intraday_state || {}).trend_regimes || {};
    const buys = Object.values(trends).filter(t => (t.state || "").includes("BULL")).length;
    const bears = Object.values(trends).filter(t => (t.state || "").includes("BEAR")).length;
    prev.innerHTML = (isToday
      ? `<div class="card" style="margin-bottom:8px;padding:8px 12px">
          <span class="cell-dim">趋势: </span><span class="up">${buys}只偏多</span>
          <span class="cell-dim"> / </span><span class="down">${bears}只偏空</span>
          <span class="cell-dim"> · 最近信号: </span>
          ${nonHolds.length ? nonHolds.map(s =>
            `<span class="badge signal">${esc(s.decision)} ${esc(s.code)} ${fmt(s.buy_score||s.sell_score,0)}分</span>`).join(" ")
            : '<span class="cell-dim">无</span>'}
        </div>`
      : ""); // 历史日留空（在 renderLive 主调用时已被 if(!isToday) 直接返回填充"非今日日期"）
  }
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

function playAlert(kind) {
  try {
    const sw = document.getElementById("alertSound");
    if (sw && !sw.checked) return;   // 报警音开关关闭
    const ctx = ensureAudio();
    if (!ctx) return;
    const t0 = ctx.currentTime + 0.02;
    if (kind === "PANIC_SELL") {       // 五连最长促音（660Hz）
      for (let i = 0; i < 5; i++) beepTone(ctx, 660, t0 + i * 0.2, 0.14);
    } else if (kind === "SELL_HIGH") { // 高频三连（880Hz）
      for (let i = 0; i < 3; i++) beepTone(ctx, 880, t0 + i * 0.2, 0.12);
    } else if (kind === "JIANCANG") {  // 建仓 四连促音（520Hz）
      for (let i = 0; i < 4; i++) beepTone(ctx, 520, t0 + i * 0.18, 0.14);
    } else {                           // 买入/加仓 低频三连（440Hz）
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
  // 建仓/加仓信号：s.type ∈ {"建仓","加仓"}，用 score=composite_score；盘中信号用 decision
  let icon, label, scoreTxt, extraTxt, alertKind;
  if (s.type === "建仓" || s.type === "加仓") {
    icon = s.type === "建仓" ? "🔵" : "🟡";
    label = s.type;
    alertKind = s.type === "建仓" ? "JIANCANG" : "JIACANG";
    scoreTxt = `${fmt(s.composite_score, 0)}分`;
    extraTxt = s.suggested_qty ? ` 建议 ${fmt(s.suggested_qty, 0)}股@${fmt(s.suggested_price, 3)}` : "";
  } else {
    icon = DEC_ICON[s.decision] || "🔔";
    label = DEC_CN[s.decision] || s.decision;
    alertKind = s.decision;
    scoreTxt = `${fmt(s.score, 1)}分`;
    extraTxt = "";
  }
  item.innerHTML = `
    <span>${icon} ${label}信号 ${esc(s.name || s.code || "")}(${esc(s.code || "")})
    <span style="font-size:16px">${scoreTxt}</span> ${extraTxt} ${time}</span>
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
  playAlert(alertKind);
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
    // 盘中交易信号（decision_trace）
    const r = await apiCall("poll_new_signals", date);
    if (r && !r.baseline && r.signals && r.signals.length) {
      r.signals.forEach(pushAlert);
    }
    // 建仓/加仓信号（position_builder intraday signal）
    const p = await apiCall("poll_new_position_signals", date);
    if (p && !p.baseline && p.signals && p.signals.length) {
      p.signals.forEach(pushAlert);
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
  if (meta) meta.textContent = `显示 ${shown.length}/${consoleBuf.length} 行`;
  const auto = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;

  // 卡片流：时间左列 + 级别芯片 + 消息内容，关键行左色条
  box.innerHTML = shown.map(l => {
    const lv = l.level || "";
    const lvCls = lv === "ERROR" ? "clv-err" : lv === "WARNING" ? "clv-warn" : "";
    const keyCls = l.key ? "con-key" : "con-noise";
    // 截断长消息（>120字符），hover 展开
    const msgText = l.msg || "";
    const truncated = msgText.length > 120;
    const displayMsg = truncated ? msgText.slice(0, 120) + "…" : msgText;
    return `<div class="con-line ${keyCls} ${lvCls}">
      <span class="con-time">${esc(l.t)}</span>
      ${lv ? `<span class="con-lv ${lvCls}">${esc(lv)}</span>` : ""}
      <span class="con-msg"${truncated ? ` title="${esc(msgText)}"` : ""}>${esc(displayMsg)}</span>
    </div>`;
  }).join("");
  if (auto) box.scrollTop = box.scrollHeight;
}

/* ---- ① KPI ---- */
function renderKPI(kpi, tradePnl) {
  const el = document.getElementById("kpiCards");
  if (!kpi || Object.keys(kpi).length === 0) {
    el.innerHTML = '<div class="empty">无 KPI 数据</div>';
    return;
  }
  const K2 = kpi.K2_cost_change || {};
  const K3 = kpi.K3_base_drift || {};
  const K4 = kpi.K4_rolling_wr || {};
  const K5 = kpi.K5_qty0_suppressed || {};

  // K1：做T闭环盈亏（优先用 tradePnl，回退到 kpi 内嵌 K1）
  const K1 = kpi.K1_closed_pnl || {};
  const tp = tradePnl || {};
  const totalPnl = tp.total_pnl != null ? tp.total_pnl : K1.total_est_pnl;
  const byCode = (tp.by_code && Object.keys(tp.by_code).length) ? tp.by_code : K1.by_code || {};
  const pnlCls = clsOf(totalPnl);
  const k1Val = totalPnl == null
    ? `<span class="neutral">无闭环</span>`
    : totalPnl === 0
      ? `<span class="neutral">0</span>`
      : `<span class="${pnlCls}">${totalPnl >= 0 ? "+" : ""}${fmt(totalPnl, 0)}</span>`;
  const k1Sub = Object.keys(byCode).length
    ? Object.entries(byCode).map(([c, v]) => `${c}: ${v.pnl != null ? (v.pnl >= 0 ? "+" : "") + fmt(v.pnl, 0) : "—"}`).join(" · ")
    : tp.note || K1.source || "当日 0 闭环";
  const k1Card = `
    <div class="kpi-card">
      <div class="kpi-label"><span>K1 做T盈亏</span><span class="flag">${tp.source || "快照"}</span></div>
      <div class="kpi-value">${k1Val}</div>
      <div class="kpi-sub">${esc(k1Sub)}</div>
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
  if (!codes.length) { tabClear("echSignal"); return; }
  codes.sort((a, b) =>
    ((sigStat[b].sell_signals || 0) + (sigStat[b].buy_signals || 0))
    - ((sigStat[a].sell_signals || 0) + (sigStat[a].buy_signals || 0)));
  const names = codes.map(c => (nameMap || {})[c] || c);
  tabEch("echSignal", {
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
  if (!points || !points.length) { tabClear("echK4"); return; }
  const latest = points[points.length - 1];
  const buyWr = latest.buy_wr == null ? 0 : Math.round(latest.buy_wr * 100);
  const sellWr = latest.sell_wr == null ? 0 : Math.round(latest.sell_wr * 100);
  const buyN = latest.buy_n || 0, sellN = latest.sell_n || 0;
  const buyCls = buyWr < 30 ? "#d29922" : buyWr >= 50 ? "#3fb950" : "#f85149";
  const sellCls = sellWr >= 50 ? "#3fb950" : sellWr < 30 ? "#d29922" : "#f85149";

  const levelColors = [[.3, "#f85149"], [.5, "#d29922"], [1, "#3fb950"]];
  const mkGauge = (value, name, color, cy) => ({
    type: "gauge", center: ["50%", cy], radius: "72%",
    startAngle: 210, endAngle: -30, min: 0, max: 100, splitNumber: 5,
    progress: { show: true, width: 8, roundCap: true, itemStyle: { color } },
    axisLine: { lineStyle: { width: 8, color: [levelColors] } },
    axisTick: { show: false }, splitLine: { show: false },
    axisLabel: { show: true, distance: -2, color: "#8b949e", fontSize: 8 },
    anchor: { show: false },
    title: { offsetCenter: [0, "78%"], color: "#8b949e", fontSize: 11 },
    detail: { offsetCenter: [0, "52%"], valueAnimation: true, color,
      fontSize: 16, fontFamily: "Consolas, monospace", formatter: "{value}%" },
    data: [{ value, name }],
  });

  tabEch("echK4", {
    backgroundColor: "transparent",
    series: [
      mkGauge(buyWr, "买信号胜率", buyCls, "28%"),
      mkGauge(sellWr, "卖信号胜率", sellCls, "72%"),
    ],
    graphic: [
      { type: "text", left: "center", top: "5%",
        style: { text: "做T买卖胜率", fill: "#c9d1d9", fontSize: 12, fontWeight: "bold", textAlign: "center" } },
      { type: "text", left: "center", bottom: 8,
        style: { text: `近20笔滚动 · 买${buyWr}%(${buyN}笔) · 卖${sellWr}%(${sellN}笔) · 买信号≥50%为优 · 卖信号≥50%为优`,
          fill: "#8b949e", fontSize: 10, textAlign: "center" } },
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
    const metCount = Object.values(conds).filter(Boolean).length;
    const metCls = metCount >= 4 ? "up" : metCount >= 2 ? "warn" : "cell-dim";
    return `
      <tr>
        <td>${esc(r.name || "")} <span class="mono cell-dim">${esc(r.code || "")}</span>
          ${r.in_holdings ? `<span class="badge hold">持仓</span>` : ""}</td>
        <td>${verdictBadge(r.verdict)}</td>
        <td class="num"><b>${r.composite_score}</b></td>
        <td class="num ${metCls}"><b>${metCount}/5</b></td>
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
      <div style="display:flex;gap:8px;margin-bottom:6px;flex-wrap:wrap;">
        ${c("signal")}${c("approaching")}${c("weak")}${c("insufficient_data")}
      </div>
      ${pb.note ? `<div class="cell-dim" style="font-size:11px;margin-bottom:6px">⚠ ${esc(pb.note)}</div>` : ""}
      <table>
        <thead><tr>
          <th>股票</th><th>判定</th><th class="num">得分</th><th class="num">通过</th><th class="num">价</th>
          <th title="MACD/BOLL/RSI/缩量/支撑">五条件</th>
          <th class="num">建议股数</th><th class="num">建议价</th><th class="num">所需资金</th><th>扫描</th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="9" class="empty">无扫描结果</td></tr>'}</tbody>
      </table>
      <div class="cell-dim" style="font-size:11px;margin-top:8px">●=通过 ○=未通过 · 五条件：${Object.values(condLabels).join(" / ")} · 每条件 20 分，≥70 signal · 盘中每10s刷新</div>
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

/* ---- 集合竞价 ---- */
function renderAuction(a) {
  const el = document.getElementById("auctionBody");
  if (!a || !a.available) {
    el.innerHTML = '<div class="empty">当日无集合竞价数据（采集器待运行或历史日未回填）</div>';
    return;
  }
  const rows = a.rows || {};
  const codes = Object.keys(rows);
  if (!codes.length) { el.innerHTML = '<div class="empty">无竞价数据</div>'; return; }
  // 按竞价涨跌排序
  codes.sort((a, b) => (rows[b].pct || 0) - (rows[a].pct || 0));

  const tableRows = codes.map(code => {
    const r = rows[code];
    const pctCls = clsOf(r.pct);
    const volTxt = r.vol_vs_yday != null
      ? `${r.vol_vs_yday.toFixed(1)}%` : (r.vol_hand != null ? `${fmt(r.vol_hand, 0)}手` : "—");
    return `<tr>
      <td>${esc(r.name || code)} <span class="mono cell-dim">${esc(code)}</span></td>
      <td class="num ${pctCls}"><b>${r.pct >= 0 ? "+" : ""}${fmt(r.pct, 2)}%</b></td>
      <td class="num">${fmt(r.price, 3)}</td>
      <td class="num">${fmt(r.pre_close, 3)}</td>
      <td class="num mono">${volTxt}</td>
    </tr>`;
  }).join("");

  const biasCls = a.bias === "偏多" ? "up" : a.bias === "偏空" ? "down" : "warn";
  const gapsWarn = a.has_gaps ? `<span class="warn">⚠ 缺失时段: ${(a.gaps || []).join(", ")}</span>` : "";

  el.innerHTML = `
    <div class="card">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px">
        <span>竞价基调: <b class="${biasCls}">${a.bias}</b></span>
        <span class="cell-dim">池同向率: <b class="up">${a.same_dir.up}↑</b> / <b class="down">${a.same_dir.down}↓</b> / ${a.same_dir.flat}平</span>
        <span class="cell-dim mono">${esc(a.slot_used || "")}</span>
        ${gapsWarn}
      </div>
      <table>
        <thead><tr><th>股票</th><th class="num">竞价涨跌</th><th class="num">竞价价</th><th class="num">昨收</th><th class="num">竞价量/昨量</th></tr></thead>
        <tbody>${tableRows}</tbody>
      </table>
      <div class="cell-dim" style="font-size:10px;margin-top:4px">竞价量/昨量=竞价成交量÷昨日全天成交量；09:20/09:22轨迹待采集器上线后补齐</div>
    </div>`;
}

/* ---- 行情条（卡片网格） ---- */
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

  // 卡片网格
  const cards = q.quotes.map(x => {
    const chgCls = clsOf(x.change);
    const pnlTxt = x.pnl_pct == null ? "—" : `${x.pnl_pct >= 0 ? "+" : ""}${fmt(x.pnl_pct, 1)}%`;
    const pnlCls = clsOf(x.pnl_pct);
    const pnlAmt = x.price && x.cost && x.qty ? (x.price - x.cost) * x.qty : null;
    const pnlAmtTxt = pnlAmt == null ? "—" : `${pnlAmt >= 0 ? "+" : ""}${fmt(pnlAmt, 0)}`;
    return `<div class="quote-card">
      <div class="qc-top">
        <span class="qc-name">${esc(x.name)}</span>
        <span class="qc-code mono">${esc(x.code)}</span>
      </div>
      <div class="qc-price ${chgCls}">${fmt(x.price, 3)}</div>
      <div class="qc-chg ${chgCls}">${x.change >= 0 ? "+" : ""}${fmt(x.change, 2)} ${x.change_pct >= 0 ? "+" : ""}${fmt(x.change_pct, 2)}%</div>
      <div class="qc-meta">
        <span>成本 <b class="mono">${fmt(x.cost, 3)}</b></span>
        <span>浮盈 <b class="${pnlCls}">${pnlTxt}</b> <span class="mono cell-dim">${pnlAmtTxt}</span></span>
      </div>
    </div>`;
  }).join("");

  // 汇总
  let tv = 0, tc = 0, tp = 0;
  q.quotes.forEach(x => {
    if (x.price && x.cost) {
      tv += x.price * (x.qty || 0);
      tc += x.cost * (x.qty || 0);
      tp += (x.price - x.cost) * (x.qty || 0);
    }
  });
  const sumCls = clsOf(tp);
  const sumPct = tc ? `${tp >= 0 ? "+" : ""}${fmt(tp / tc * 100, 1)}%` : "—";
  const html = `<div class="quote-grid">${cards}</div>
    <div class="quote-summary" style="margin-top:10px">
      总市值 <b class="num">${fmt(tv, 0)}</b>
      总成本 <b class="num">${fmt(tc, 0)}</b>
      总盈亏 <b class="num ${sumCls}">${tp >= 0 ? "+" : ""}${fmt(tp, 0)}（${sumPct}）</b>
    </div>
    <div class="cell-dim" style="font-size:11px;margin-top:6px">腾讯实时行情 · 盘中每10s刷新${srcBadge}</div>`;

  updateSidebarSummary(q.quotes);
  if (bar) bar.innerHTML = srcBadge;
  if (body) body.innerHTML = html;
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

/* ================= Tab 切换 ================= */
function switchTab(tab) {
  document.querySelectorAll(".tab-page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".sidebar-item").forEach(s => s.classList.remove("active"));
  const page = document.getElementById("tab-" + tab);
  const sideItem = document.querySelector(`.sidebar-item[data-tab="${tab}"]`);
  if (page) page.classList.add("active");
  if (sideItem) sideItem.classList.add("active");
  // 延迟 resize 让 tab 的 display:block 先生效
  setTimeout(resizeAllEch, 80);
}

/* ================= 侧栏汇总 ================= */
let portfolioCfg = { accounts: {}, realized_loss: {} };  // 独立配置文件

function updateSidebarSummary(quotes) {
  if (!quotes || !quotes.length) return;
  let tv = 0, tc = 0, trl = 0, totalCap = 0;
  const accounts = portfolioCfg.accounts || {};
  const rl = portfolioCfg.realized_loss || {};
  quotes.forEach(x => {
    const qty = x.qty || 0;
    if (x.price && x.cost) {
      tv += x.price * qty;
      tc += x.cost * qty;
    }
    trl += rl[x.code] || 0;
  });
  Object.values(accounts).forEach(a => {
    totalCap += (a.total_capital || 0);
  });
  const pnl = tv - tc;
  document.getElementById("sumValue").textContent = tv ? fmt(tv, 0) : "—";
  document.getElementById("sumCapital").textContent = totalCap ? fmt(totalCap, 0) : "未设置";
  document.getElementById("sumPos").textContent = totalCap ? Math.round(tv / totalCap * 100) + "%" : "—";
  document.getElementById("sumUnreal").innerHTML = `<span class="${clsOf(pnl)}">${pnl >= 0 ? "+" : ""}${fmt(pnl, 0)}</span>`;
  document.getElementById("sumRealLoss").innerHTML = trl ? `<span class="${trl > 0 ? 'down' : 'neutral'}">${fmt(trl, 0)}</span>` : `<span class="neutral">0</span>`;
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
  // 侧栏 tab 切换
  document.querySelectorAll(".sidebar-item[data-tab]").forEach(si => {
    si.addEventListener("click", () => {
      switchTab(si.dataset.tab);
      // 图表 tab：切过去时初始化未渲染的 ECharts（首次 lazy init）
      if (si.dataset.tab === "charts") setTimeout(resizeAllEch, 120);
    });
  });
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
