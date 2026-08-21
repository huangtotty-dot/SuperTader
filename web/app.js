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
  const map = { signal: "signal", approaching: "approach", weak: "weak", insufficient_data: "nodata", pending: "nodata" };
  const label = { signal: "signal", approaching: "approaching", weak: "weak", insufficient_data: "无数据", pending: "等待扫描" }[v] || v;
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
// fix P0-2: 解析 scan_time（"YYYY-MM-DD HH:MM:SS" 或 ISO），返回距今秒数；无法解析返回 null
function scanAgeSec(scanTime) {
  if (!scanTime) return null;
  const t = Date.parse(String(scanTime).replace(" ", "T"));
  if (isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

/* ================= 数据加载 ================= */
let state = { date: null, payload: null, trend: [] };
let userPinnedDate = false;   // 用户手动选了历史日期时置 true，防止自动切回今天覆盖用户选择

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
    // 持仓日线体检（超买/顶背离）
    apiCall("load_ob_analysis").then(ob => renderOB(ob || {})).catch(() => {});
    apiCall("load_quotes").then(q => renderQuotes(q || {}, null)).catch(() => {});
    apiCall("load_position_manager").then(pm => renderPositionManager(pm || {})).catch(() => {});
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

// fix P1-6: 实时轮询失败计数，连续失败 ≥3 次在状态栏红色告警，恢复后自动消除
let liveFailCnt = 0, pbFailCnt = 0;
function updateRefreshAlarm() {
  const interrupted = liveFailCnt >= 3 || pbFailCnt >= 3;
  if (interrupted) {
    statusEl(`⚠ 实时刷新中断（live 连续失败 ${liveFailCnt} 次 / pb ${pbFailCnt} 次）· ${nowTime()}`, "err");
  } else if (window._refreshAlarmWas) {
    statusEl(`实时刷新已恢复 · ${nowTime()}`, "ok");
  }
  window._refreshAlarmWas = interrupted;
}

async function refreshLive(reset) {
  const date = state.date;
  if (!date) return;
  try {
    const live = await apiCall("load_live", date);
    liveFailCnt = 0;   // fix P1-6: 主链路成功即清零
    renderLive(live, date === todayStr());

    // 实时行情条（10s 刷新）+ 存 quotes 供 PB 价格更新
    apiCall("load_quotes").then(q => {
      renderQuotes(q || {}, null);
      state.quotes = (q && q.quotes) || [];
      if (state.payload) {
        // PB 表价格实时更新
        const pbRows = state.payload.position_builder;
        if (pbRows && pbRows.rows) {
          const pxMap = {};
          state.quotes.forEach(x => pxMap[x.code] = x.price);
          pbRows.rows.forEach(r => { if (pxMap[r.code] != null) r.price = pxMap[r.code]; });
          renderPB(pbRows);
        }
      }
    }).catch(() => {});
    // 今日盘中 S 曲线刷新
    if (live.market_intraday && live.market_intraday.length) {
      apiCall("load_market_score", date).then(ms => renderMarket(ms || {})).catch(() => {});
    }

    // 加仓条件全满足检测（与飞书推送无关，纯 GUI 报警）
    const aw = live.add_watch || (state.payload && state.payload.add_watch) || {};
    // fix P0-4: 满分告警需 box_boost（突破加成），或真实 met==total 且 not_applicable 为空（排除盘中占位改写）
    const allMet = Object.entries(aw).filter(([code, v]) => {
      if (code === "_progress" || !v) return false;
      const conds = v.conditions || [];
      const notApp = v.not_applicable || [];
      const realFull = conds.length > 0 && (v.met_count || 0) >= conds.length && notApp.length === 0;
      return v.box_boost === true || realFull;
    });
    if (allMet.length && !window._awAlerted) window._awAlerted = {};
    allMet.forEach(([code, v]) => {
      const key = date + "_" + code;
      if (!window._awAlerted[key]) {
        window._awAlerted[key] = true;
        pushAlert({
          scan_time: nowTime(), code, name: v.name, type: "加仓条件",
          decision: "JIACANG_COND", composite_score: v.met_count * 25,
          suggested_qty: null, suggested_price: null,
        });
      }
    });

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
    // fix P1-6: refresh_pb 成败纳入轮询告警计数
    apiCall("refresh_pb", date).then(pb => {
      pbFailCnt = 0;
      // 关键：把带技术标签的结果合并回 state.payload，否则后续 load_quotes 路径
      // 用无 tags 的旧 payload 重新 renderPB，会覆盖掉已显示的技术标签（一会有一会无）
      if (pb && pb.rows && state.payload) state.payload.position_builder = pb;
      renderPB(pb || {});
      updateRefreshAlarm();
    }).catch(() => {
      pbFailCnt++;
      updateRefreshAlarm();
    });
    updateRefreshAlarm();   // fix P1-6: 主链路成功后收敛告警状态
  } catch (e) {
    // fix P1-6: 实时轮询失败计数（仍不打断主界面），≥3 次状态栏红色告警
    liveFailCnt++;
    updateRefreshAlarm();
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

  // 盘中状态卡
  const trendCards = Object.keys(trends).map(code => {
    const t = trends[code];
    const st = t.state || "—";
    const cls = st === "STRONG_BULL" || st === "BULL" ? "regime-uni_up"
      : st === "STRONG_BEAR" || st === "BEAR" ? "regime-uni_down"
      : "regime-range";
    const bCnt = buyCount[code] || 0, sCnt = sellCount[code] || 0;
    const cdTxt = [];
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
  if (s.type === "建仓" || s.type === "加仓" || s.type === "加仓条件") {
    icon = s.type === "建仓" ? "🔵" : s.type === "加仓条件" ? "✅" : "🟡";
    label = s.type;
    alertKind = s.type === "建仓" ? "JIANCANG" : s.type === "加仓条件" ? "JIACANG" : "JIACANG";
    scoreTxt = `${fmt(s.composite_score, 0)}分`;
    extraTxt = s.suggested_qty ? ` 建议 ${fmt(s.suggested_qty, 0)}股@${fmt(s.suggested_price, 3)}` : "";
  } else {
    icon = DEC_ICON[s.decision] || "🔔";
    label = DEC_CN[s.decision] || s.decision;
    alertKind = s.decision;
    scoreTxt = `${fmt(s.score, 1)}分`;
    extraTxt = s.message ? ` <span style="font-size:12px">${esc(s.message)}</span>` : "";
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
  // fix 2026-08-20: 只显示有效持仓(qty>0)，已清仓的(qty=0)不进对照表
  const codes = Object.keys(cur).filter(c => (cur[c] && (cur[c].qty || 0) > 0));
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

/* ---- 仓位管理器 ---- */
function renderPositionManager(pm) {
  const el = document.getElementById("positionManagerBody");
  if (!el) return;
  const rows = (pm && pm.rows) || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty">无持仓数据</div>';
    return;
  }
  const totalCap = pm.total_capital || 0;
  const sumMkt = pm.sum_mkt || 0;
  const sumPct = pm.sum_pct || 0;
  const rowsHtml = rows.map(r => {
    const badge = r.over
      ? `<span class="badge t-short">超配 应减</span>`
      : r.under
        ? `<span class="badge t-long">欠配 可加</span>`
        : `<span class="badge chop">正常</span>`;
    const gapCls = r.gap_pct > 5 ? "down" : r.gap_pct < -5 ? "up" : "cell-dim";
    const pctCls = r.pct >= (r.target_pct * 100 * 0.9) ? "down" : "cell-dim";
    // 分批加仓：欠配分3批(不足3手则2批/1批)
    const batches = (r.add_batches || []);
    const batchTxt = batches.length
      ? batches.map((b, i) => `<span class="badge ${i === 0 ? 't-long' : 'chop'}" title="第${i + 1}次加仓">${i + 1}次 ${fmt(b, 0)}股</span>`).join(" ")
      : (r.gap_qty < 0 ? '<span class="cell-dim">—</span>' : '<span class="cell-dim">已达标</span>');
    return `<tr>
      <td>${esc(r.name)} <span class="mono cell-dim">${esc(r.code)}</span></td>
      <td class="num">${fmt(r.total_qty, 0)}</td>
      <td class="num">${fmt(r.price, 2)}</td>
      <td class="num">${fmt(r.mkt_val, 0)}</td>
      <td class="num cell-dim">${fmt(r.target_val, 0)} (${(r.target_pct * 100).toFixed(0)}%)</td>
      <td class="num ${pctCls}">${fmt(r.pct, 1)}%</td>
      <td class="num ${gapCls}">${r.gap_pct >= 0 ? "+" : ""}${fmt(r.gap_pct, 1)}%<br>
        <span class="mono cell-dim" style="font-size:10px">${r.gap_qty > 0 ? "可加" : r.gap_qty < 0 ? "应减" : ""}${Math.abs(r.gap_qty || 0)}股</span></td>
      <td style="min-width:130px">${batchTxt}</td>
      <td>${badge}</td>
    </tr>`;
  }).join("");

  // 总仓位进度条
  const barW = Math.min(100, sumPct);
  el.innerHTML = `
    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px">
        <div style="display:flex;gap:16px;font-size:12px">
          <span>总资金 <b class="mono">${fmt(totalCap, 0)}</b></span>
          <span>总市值 <b class="mono">${fmt(sumMkt, 0)}</b></span>
          <span>总仓位 <b class="mono ${sumPct >= 100 ? 'down' : ''}">${fmt(sumPct, 1)}%</b></span>
        </div>
        <span class="cell-dim mono" style="font-size:10px">目标=总资金×个股配比 · 数据随行情刷新</span>
      </div>
      <div class="h-bar-track" style="height:6px;margin-top:6px">
        <div class="h-bar-fill ${sumPct >= 100 ? 'down' : 'up'}" style="width:${barW}%"></div>
      </div>
    </div>
    <table class="h-table">
      <thead><tr>
        <th>股票</th><th class="num">股数</th><th class="num">现价</th>
        <th class="num">当前市值</th><th class="num">目标市值</th>
        <th class="num">资金占比</th><th class="num" title="偏差百分比 + 折算股数(取整一手100股)">偏差(股)</th>
        <th class="num" title="欠配股数分3次加仓">分批加仓</th><th>状态</th>
      </tr></thead>
      <tbody>${rowsHtml}</tbody>
    </table>`;
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
  // 突破箱体列（第一优先级）— W33 A1: 键名改为 c2_box_breakout
  const boxKey = "c2_box_breakout";
  // 开盘预热提示：今天 09:30-09:45 期间 5 分钟线不足
  const now = new Date();
  const inWarmup = state.date === todayStr() &&
    (now.getHours() === 9 && now.getMinutes() < 45);
  window._pbWarmup = inWarmup;

  // 前后对比 → 变化文字
  const prev = window._pbLast || {};
  const currSig = (pb.rows || []).filter(r => r.verdict === "signal").map(r => r.code).sort().join(",");
  const currApp = (pb.rows || []).filter(r => r.verdict === "approaching").map(r => r.code).sort().join(",");
  window._pbLast = { sig: currSig, app: currApp, ts: pb.refreshed_at || "" };
  let delta = "";
  if (prev.sig !== undefined && prev.sig !== currSig) {
    const prevS = (prev.sig || "").split(",").filter(Boolean);
    const currS = currSig.split(",").filter(Boolean);
    const added = currS.filter(c => !prevS.includes(c));
    const removed = prevS.filter(c => !currS.includes(c));
    if (added.length) delta += ` 🆕 新增signal: ${added.join(",")}`;
    if (removed.length) delta += ` ⬇ 退出signal: ${removed.join(",")}`;
  }
  if (!delta && prev.app !== undefined && prev.app !== currApp) {
    const prevA = (prev.app || "").split(",").filter(Boolean);
    const currA = currApp.split(",").filter(Boolean);
    const moved = currA.filter(c => !prevA.includes(c)).length + prevA.filter(c => !currA.includes(c)).length;
    if (moved) delta = ` 📊 approaching 变动 ${moved} 只`;
  }
  if (!delta && prev.sig !== undefined) delta = " ✓ 无变化";
  const rows = (pb.rows || []).map(r => {
    const conds = r.conditions || {};
    // fix P1-3: insufficient_data 行圆点区显示『—』而非 5 个空心圆
    const isNoData = r.verdict === "insufficient_data";
    const condStr = isNoData
      ? `<span class="cell-dim">—</span>`
      : Object.keys(condLabels).map(k =>
          conds[k] === undefined ? `<span class="cell-dim">·</span>`
            : conds[k] ? `<span class="on">●</span>` : `<span class="off">○</span>`).join("");
    const condTitle = Object.keys(condLabels).map(k =>
      `${condLabels[k]}:${conds[k] ? "通过" : "未过"}`).join(" · ");
    const boxMet = conds[boxKey] || false;
    const boxStr = boxMet ? `<span class="badge signal">🚀突破</span>` : `<span class="off">—</span>`;
    // 方案A (2026-08-15): 建仓条件=时机门控（市场有方向/多头结构/回撤到位）→ "X/3"（金叉为加分不计）
    const iceKeys = ["t_regime", "t_trend", "t_drawdown"];
    const metCount = iceKeys.filter(k => conds[k]).length;
    const metCls = metCount >= 3 ? "up" : metCount >= 2 ? "warn" : "cell-dim";
    // fix P1-3: 行内灰色小字显示 r.errors 首条（hover 看全部）
    const errTxt = (r.errors && r.errors.length)
      ? `<div class="cell-dim" style="font-size:10px;line-height:1.4" title="${esc(r.errors.join("；"))}">⚠ ${esc(r.errors[0])}</div>`
      : "";
    // fix P0-2: 展示每行 scan_time，距今 >10 分钟整行置灰并加『陈旧』标记
    const ageSec = scanAgeSec(r.scan_time);
    const isStale = ageSec !== null && ageSec > 600;
    const scanTimeTxt = r.scan_time ? String(r.scan_time).slice(11, 19) : "";
    const staleBadge = isStale ? ` <span class="badge nodata">陈旧</span>` : "";
    const tagsTxt = r.tags && r.tags.length
      ? r.tags.map(t => tagBadge(t)).join(" ")
      : '<span class="cell-dim" style="font-size:10px">…</span>';
    // W33 A1: 通道徽章 + 择时状态
    const chTxt = r.channel === "both" ? "🧊🚀" : r.channel === "iceberg" ? "🧊" : r.channel === "breakout" ? "🚀" : "";
    const chBadge = chTxt ? `<span class="badge" style="background:#3a3a5a;color:#ddd">${chTxt}</span>` : "";
    const apTxt = r.approach_status === "immediate" ? "即时可建" : r.approach_status === "intraday_pending" ? "待日内确认" : r.approach_status === "next_day_pending" ? "待次日确认" : "";
    const apBadge = apTxt ? `<span class="badge" style="background:#5a4a2a;color:#ffd98a">${apTxt}</span>` : "";
    // 建仓/加仓时机门控徽章（timing_gate: 多头追强/空头抄底/震荡降频）
    const _tm = r.timing || {};
    let tmBadge = "";
    if (_tm.regime) {
      const _regimeCn = {trend_up: "多头", trend_dn: "空头", range: "震荡"}[_tm.regime] || _tm.regime;
      const _goCls = _tm.go ? "#1f6f3f" : "#7a2a2a";
      const _goTxt = _tm.go ? "GO" : "NO-GO";
      tmBadge = `<span class="badge" title="时机门控: ${esc(_tm.reason || "")}" style="background:${_goCls};color:#fff">${esc(_regimeCn)}·${_goTxt}</span>`;
    }
    // 30/60分钟线背离（2026-08-19 验证后收窄：仅 60min 连续底背离高亮，其余仅供参考）
    const dv = r.divergence || {};
    const dvd = r.divergence_detail || {};
    const dvParts = ["m30", "m60"].map(k => {
      const v = dv[k];
      const dd = dvd[k];
      if (!v) return "";
      const freq = k === "m30" ? "30" : "60";
      const icon = v === "顶背离" ? "⛔" : "✅";
      const consec = !!(dd && dd.consec);
      // 60分钟连续底背离：唯一验证有效的信号，高亮
      if (k === "m60" && dd && dd.type === "底背离" && consec) {
        return `<span class="badge t-long" title="60分钟连续底背离（180天验证：命中率60% vs 基线47%，样本40）">60分✅连续底</span>`;
      }
      // 其余连续背离：标注"连续"（信息性，验证命中率≈随机基线，仅供参考）
      if (consec) {
        const lbl = v === "顶背离" ? "连续顶" : "连续底";
        return `<span class="badge" title="${k.toUpperCase()} ${v}（连续背离；验证：命中率≈随机基线，仅供参考）">${freq}分${icon}${lbl}</span>`;
      }
      return `<span class="badge" title="${k.toUpperCase()} ${v}（验证：命中率≈随机基线，仅供参考）">${freq}分${icon}</span>`;
    }).filter(Boolean);
    const dvCell = dvParts.length
      ? `<td style="text-align:center">${dvParts.join(" ")}</td>`
      : `<td class="cell-dim" style="text-align:center">—</td>`;
    return `
      <tr id="pb-row-${esc(r.code || '')}" ondblclick="openStockChart('${esc(r.code||'')}','${esc(r.name||r.code||'')}')" style="cursor:pointer;${isStale ? "opacity:.45;" : ""}" title="双击看K线${isStale ? "（数据陈旧，距今超过10分钟）" : ""}">
        <td>${esc(r.name || "")} <span class="mono cell-dim">${esc(r.code || "")}</span>
          ${chBadge}${apBadge}${tmBadge}${r.in_holdings ? `<span class="badge hold">持仓</span>` : ""}${errTxt}</td>
        <td>${verdictBadge(r.verdict)}</td>
        <td class="num"><b>${r.composite_score}</b></td>
        <td class="num ${metCls}"><b>${isNoData ? "—" : metCount + "/3"}</b></td>
        <td class="num">${fmt(r.price)}</td>
        <td style="text-align:center">${boxStr}</td>
        <td style="max-width:220px;line-height:1.7">${tagsTxt}</td>
        <td><span class="cond" title="${esc(condTitle)}">${condStr}</span></td>
        ${dvCell}
        <td class="num">${fmt(r.suggested_qty, 0)}</td>
        <td class="num">${fmt(r.suggested_price)}</td>
        <td class="num">${fmt(r.capital_required, 0)}</td>
        <td class="cell-dim">${esc(r.scan_type || "")}${r._scans > 1 ? `×${r._scans}` : ""}${scanTimeTxt ? ` <span class="mono">${esc(scanTimeTxt)}</span>` : ""}${staleBadge}</td>
        <td>${!r.in_holdings ? `<button class="mini-btn" style="font-size:10px;padding:0 5px;color:#3fb950" onclick="confirmPosition('${esc(r.code||'')}',document.getElementById('pb-row-${esc(r.code||'')}'))" title="人工确认建仓（回写 signal_history）">✓确认</button><button class="mini-btn" style="font-size:10px;padding:0 5px;color:#f85149" onclick="removeFromWatchlist('${esc(r.code||'')}',document.getElementById('pb-row-${esc(r.code||'')}'))" title="从股池移除">✕</button>` : ""}</td>
      </tr>`;
  }).join("");

  // fix P0-1: counts 已是按 code 去重的股票数，徽章统一『只』口径并加 tooltip
  // fix P1-7: insufficient_data/pending 用独立灰色 nodata 徽章，不再落入 weak
  const badgeClsOf = k => k === "signal" ? "signal" : k === "approaching" ? "approach"
    : (k === "insufficient_data" || k === "pending") ? "nodata" : "weak";
  const c = k => counts[k] ? `<span class="badge ${badgeClsOf(k)}" title="按股票数统计（按 code 去重）">${k}: ${counts[k]}只</span>` : "";
  // fix P0-2: refreshed_at 语义为数据最后写入时间，直接展示
  const refreshed = pb.refreshed_at ? `<span class="live-dot"></span> ${esc(pb.refreshed_at)}${delta ? ` <span class="aw-delta">${delta}</span>` : ""}` : "";
  el.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:8px;margin-bottom:6px;flex-wrap:wrap;align-items:center">
        ${c("signal")}${c("approaching")}${c("weak")}${c("insufficient_data")}
        <span class="cell-dim mono" style="font-size:10px;margin-left:auto" title="数据最后写入时间（前端盘中每10s拉取）">${refreshed}</span>
      </div>
      ${pb.note ? `<div class="cell-dim" style="font-size:11px;margin-bottom:6px">⚠ ${esc(pb.note)}</div>` : ""}
      ${inWarmup ? `<div class="warmup-banner">⏳ 开盘预热中：5分钟K线累积中（需≥3根），09:45 后出完整信号</div>` : ""}
  ${pb.progress ? `<div class="cell-dim" style="font-size:10px;margin-bottom:6px">扫描进度: <b>${pb.progress.scanned}/${pb.progress.total_candidates}</b> 只已扫${pb.progress.pending ? ` · <b class="warn">${pb.progress.pending}</b> 只待扫描` : ''} · 已扫描 <b>${pb.progress.online_fetched}</b> 只 · 无数据 <b class="warn" title="insufficient_data：5分钟K线/快照不足，未参与条件判定">${pb.progress.no_data}</b> 只<span class="cell-dim">（K线/快照不足）</span></div>` : ""}
      <table>
        <thead><tr>
          <th>股票</th><th>判定</th><th class="num">得分</th><th class="num">通过</th><th class="num">价</th>
          <th title="突破箱体=第一优先级">突破</th>
          <th title="通道/箱体/背离等技术形态">技术标签</th>
          <th title="时机门控：市场有方向/多头结构/回撤到位（GO→signal，震荡→降频）">时机条件</th>
          <th title="背离显示连续标记：60分连续底背离高亮（180天验证有区分度）；其余连续/单次背离命中率≈随机基线，仅供参考">背离</th>
          <th class="num">建议股数</th><th class="num">建议价</th><th class="num">所需资金</th><th>扫描</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="14" class="empty">无扫描结果</td></tr>'}</tbody>
      </table>
      <div class="cell-dim" style="font-size:11px;margin-top:8px">●=通过 ○=未通过 · 时机条件：${Object.values(condLabels).join(" / ")} · GO(多头追强/空头抄底)→signal，震荡→降频 · 盘中每10s刷新</div>
    </div>`;
  // fix P1-2: 页脚文案与后端实际逻辑一致（≥80 且突破箱体→signal，突破另需≥40）
}

/* ---- ⑦ 加仓观察 ---- */
function renderAddWatch(aw) {
  const el = document.getElementById("addWatchBody");
  // fix P0-13 防护：过滤 `_progress` 等下划线伪股票键，避免混入卡片与『共 N 只』计数
  const codes = Object.keys(aw || {}).filter(c => c[0] !== "_");
  if (!codes.length) {
    el.innerHTML = '<div class="empty">无加仓观察数据（盘中实时计算，需分钟快照；历史日依赖 daily_review）</div>';
    return;
  }

  // fix P2-1: 前后对比改为按 code→met_count 字典 diff（原字符位置比对在列表变化/两位数时会错乱）
  const currMap = {};
  codes.forEach(c => { currMap[c] = (aw[c] || {}).met_count || 0; });
  const prevMap = (window._awLast || {}).metMap || null;
  window._awLast = { metMap: currMap, ts: nowTime() };
  let awDelta = "";
  if (prevMap) {
    const ups = [], downs = [];
    codes.forEach(c => {
      const p = prevMap[c];
      if (p === undefined || p === currMap[c]) return;
      (currMap[c] > p ? ups : downs).push(c);
    });
    if (ups.length) awDelta += ` 🟢 ${ups.join(",")} 条件改善`;
    if (downs.length) awDelta += ` 🔴 ${downs.join(",")} 条件退化`;
  }
  if (!awDelta && prevMap) awDelta = " ✓ 无变化";

  // 汇总统计
  let holdCnt = 0, breakCnt = 0, nearCnt = 0, noEventCnt = 0;
  codes.forEach(code => {
    const w = aw[code];
    const evts = w.events || [];
    const hasHold = evts.some(e => e.status === "守住");
    const hasBreak = evts.some(e => e.status === "破位");
    if (hasHold) holdCnt++;
    if (hasBreak) breakCnt++;
    if (!hasHold && !hasBreak && (w.near || []).length > 0) nearCnt++;
    if (!hasHold && !hasBreak && !(w.near || []).length) noEventCnt++;
  });
  const total = codes.length;
  const bar = (cnt, cls, label) =>
    cnt > 0 ? `<span class="aw-bar-seg ${cls}" style="width:${(cnt/total*100).toFixed(1)}%" title="${label}: ${cnt}/${total}">${cnt}</span>` : "";

  const now = nowTime();
  window._awLastRefresh = now;
  const summaryHtml = `
    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <span class="cell-dim">回踩支撑满足程度（共 ${total} 只）</span>
        <span class="cell-dim mono" style="font-size:10px" title="随盘中实时数据自动刷新"><span class="live-dot"></span> ${now}${awDelta ? ` <span class="aw-delta">${awDelta}</span>` : ""}</span>
      </div>
      <div class="aw-bar">
        ${bar(holdCnt, "aw-hold", "守住支撑")}
        ${bar(breakCnt, "aw-break", "破位")}
        ${bar(nearCnt, "aw-near", "近阈未触")}
        ${bar(noEventCnt, "aw-none", "无事件")}
      </div>
      <div style="display:flex;gap:14px;margin-top:6px;font-size:11px">
        <span><span class="aw-dot hold"></span>守住 ${holdCnt}只</span>
        <span><span class="aw-dot break"></span>破位 ${breakCnt}只</span>
        <span><span class="aw-dot near"></span>近阈 ${nearCnt}只</span>
        <span><span class="aw-dot none"></span>无事件 ${noEventCnt}只</span>
      </div>
      <div class="cell-dim" style="font-size:10px;margin-top:4px">回踩支撑不破是加仓前提 · 守住=触及支撑且收盘站回 · 盘中实时计算</div>
      ${aw._progress ? `<div class="cell-dim" style="font-size:10px;margin-top:2px">快照覆盖: <b>${aw._progress.snapshots_ok}/${aw._progress.total_holdings}</b> 只${aw._progress.snapshots_miss > 0 ? ` · <span class="warn">${aw._progress.snapshots_miss} 只缺快照</span>` : ""}</div>` : ""}
    </div>`;

  const cards = codes.map(code => {
    const w = aw[code];
    const conds = w.conditions || [];
    const met = w.met_count || 0;
    const totalConds = conds.length || 4;
    const metPct = totalConds ? met / totalConds * 100 : 0;
    const metCls = metPct >= 75 ? "up" : metPct >= 50 ? "warn" : "down";

    // 分组：左侧=冰点(4日线+5分钟确认)，右侧=突破箱体
    const rightIdx = conds.findIndex(c => c.name === "右侧突破箱体");
    const leftConds = rightIdx >= 0 ? conds.slice(0, rightIdx) : conds;
    const rightConds = rightIdx >= 0 ? conds.slice(rightIdx) : [];
    const condRow = c =>
      `<div class="aw-cond ${c.met ? "met" : "wait"}" title="${esc(c.detail)}">
        <span class="aw-cond-icon">${c.met ? "✓" : "○"}</span>
        <span>${esc(c.name)}</span>
        <span class="aw-cond-detail">${esc(c.detail)}</span>
      </div>`;
    const leftRows = leftConds.map(condRow).join("");
    const rightRows = rightConds.map(condRow).join("");

    // 可加仓徽章：左侧冰点(日线+5分钟) 或 右侧突破箱体 任一满足
    const canAdd = w.left_iceberg || w.right_breakout;
    const addBadge = canAdd
      ? `<span class="badge signal" style="margin-left:6px">🔼 可加仓</span>`
      : "";
    // 加仓时机门控徽章（timing_gate）
    const _atm = w.timing || {};
    let tmBadge = "";
    if (_atm.regime) {
      const _rc = {trend_up: "多头", trend_dn: "空头", range: "震荡"}[_atm.regime] || _atm.regime;
      const _cls = _atm.go ? "#1f6f3f" : "#7a2a2a";
      const _t = _atm.go ? "GO" : "NO-GO";
      tmBadge = `<span class="badge" title="时机门控: ${esc(_atm.reason || "")}" style="background:${_cls};color:#fff;margin-left:6px">${esc(_rc)}·${_t}</span>`;
    }

    return `
      <div class="card" style="margin-bottom:10px;padding:12px 14px">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px">
          <div>
            <b>${esc(w.name || code)}</b> <span class="mono cell-dim">${esc(code)}</span>${addBadge}${tmBadge}
            <span class="aw-score ${metCls}">${met}/${totalConds} 条件满足</span>
          </div>
          <div class="cell-dim mono" style="font-size:11px">低${fmt(w.day_low, 3)} 收${fmt(w.close, 3)} VWAP${fmt(w.vwap, 3)}</div>
        </div>
        <div class="aw-group-title">左侧·情绪冰点</div>
        <div class="aw-cond-list">${leftRows || '<div class="cell-dim" style="font-size:10px">无</div>'}</div>
        ${rightRows ? `<div class="aw-group-title" style="margin-top:6px">右侧·突破箱体</div>
        <div class="aw-cond-list">${rightRows}</div>` : ""}
      </div>`;
  }).join("");
  el.innerHTML = summaryHtml + cards;
}

// ---- 盘后重跑：重新跑一遍建仓扫描+加仓观察（日线口径）----
let pbRecomputeRunning = false;
async function recomputePB() {
  if (pbRecomputeRunning) return;
  if (!state.date) return;
  const btns = document.querySelectorAll(".recompute-pb-btn");
  pbRecomputeRunning = true;
  btns.forEach(b => { b.disabled = true; b.textContent = "⏳ 重跑中..."; });
  statusEl("盘后重跑中（建仓扫描+加仓观察）...", "ok");
  try {
    const r = await apiCall("recompute_pb", state.date);
    if (r && r.position_builder) {
      renderPB(r.position_builder);
      renderAddWatch(r.add_watch || {});
      if (state.payload) {
        state.payload.position_builder = r.position_builder;
        state.payload.add_watch = r.add_watch;
      }
      statusEl("盘后重跑完成", "ok");
    } else {
      statusEl("重跑失败: 无返回", "err");
    }
  } catch (e) {
    statusEl("重跑失败: " + e.message, "err");
  }
  pbRecomputeRunning = false;
  btns.forEach(b => { b.disabled = false; b.textContent = "🔄 盘后重跑"; });
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

/* ---- 个股技术分析弹窗 ---- */
let stockChartData = null;
let stockChartPeriod = "daily";
let stockChartInst = null;
let stockChartTimer = null;
let stockChartCode = null;
let stockChartName = "";
const MA_COLORS = ["#e6c07b", "#56b4e9", "#e91e63", "#9b59b6", "#2ecc71", "#f39c12", "#3498db"];

async function loadStockChartNow() {
  if (!stockChartCode) return;
  try {
    const d = await apiCall("load_stock_chart", stockChartCode);
    if (d && d.available) {
      stockChartData = d;
      renderStockSummary(d);
      renderStockChart();
    }
  } catch (e) { /* 静默 */ }
}

async function openStockChart(code, name) {
  const modal = document.getElementById("stockModal");
  if (!modal) return;
  modal.style.display = "flex";
  stockChartCode = code;
  stockChartName = name;
  document.getElementById("stockModalTitle").textContent = `${name} (${code}) 技术分析`;
  document.getElementById("stockChart").innerHTML = '<div class="empty">加载中...</div>';
  // 10s 实时刷新
  if (stockChartTimer) clearInterval(stockChartTimer);
  stockChartTimer = setInterval(loadStockChartNow, 10000);
  await loadStockChartNow();
}
function toggleChartHelp() {
  const el = document.getElementById("chartHelp");
  el.style.display = el.style.display === "none" ? "block" : "none";
}
function toggleMaxStockChart() {
  const modal = document.getElementById("stockModal");
  const box = modal.querySelector(".modal");
  const isMax = box.classList.contains("modal-max");
  if (isMax) {
    box.classList.remove("modal-max");
  } else {
    box.classList.add("modal-max");
  }
  // 最大化后 resize 图表
  setTimeout(() => {
    if (stockChartInst) stockChartInst.resize();
  }, 60);
}
function closeStockChart() {
  document.getElementById("stockModal").style.display = "none";
  if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }
  if (stockChartTimer) { clearInterval(stockChartTimer); stockChartTimer = null; }
  stockChartCode = null;
}
function switchStockPeriod(p) {
  stockChartPeriod = p;
  document.querySelectorAll(".stock-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.period === p));
  renderStockChart();
}
function renderStockSummary(d) {
  const cur = d.current_price;
  // 今日涨跌幅（日线最后一根 较 前一根）
  const _dl = (d.period_data && d.period_data.daily.ohlc) || [];
  const _n = _dl.length;
  let _chgPct = null;
  if (_n >= 2) {
    const _prev = _dl[_n - 2][1];
    if (_prev) _chgPct = (_dl[_n - 1][1] - _prev) / _prev * 100;
  }
  const _chgHtml = _chgPct != null
    ? ` <b class="${_chgPct >= 0 ? 'up' : 'down'}">${_chgPct >= 0 ? '+' : ''}${fmt(_chgPct, 2)}%</b>`
    : "";
  const sup = d.levels.supports, res = d.levels.resistances;
  const supTxt = sup.map(s => `<b class="down">${s.price}</b>`).join(" / ") || "—";
  const resTxt = res.map(r => `<b class="up">${r.price}</b>`).join(" / ") || "—";
  const boxes = d.boxes || [];
  const boxTxt = boxes.length
    ? boxes.map(b => `<b class="${b.rel === 0 ? 'warn' : 'cell-dim'}">${b.low}~${b.high}${b.rel === 0 ? '(当前)' : ''}</b>`).join(" · ")
    : "<span class='cell-dim'>无</span>";
  const ch = d.channel || {};
  const chTxt = ch.direction === "up" ? `<b class="up">上行 ↗</b>`
    : ch.direction === "down" ? `<b class="down">下行 ↘</b>` : `<span class="cell-dim">震荡 →</span>`;
  const chDesc = ch.direction === "flat" ? ""
    : `<span class="cell-dim" style="font-size:11px">${ch.direction === "up" ? "上行" : "下行"}通道 斜率${ch.norm_slope_pct != null ? (ch.norm_slope_pct >= 0 ? '+' : '') + fmt(ch.norm_slope_pct, 2) : '—'}%/日 · 40日${ch.ret_40d >= 0 ? '+' : ''}${fmt(ch.ret_40d, 1)}% · 现价位于通道${ch.pos_pct != null ? fmt(ch.pos_pct, 0) : '—'}%</span>`
      + (ch.reversal ? `<span class="badge signal" style="margin-left:6px">↺ 通道反转${ch.reversal === "up_to_down" ? " 上→下" : " 下→上"}</span>` : "")
  document.getElementById("stockSummary").innerHTML = `
    <span class="ss-item">当前价: <b class="mono">${fmt(cur, 3)}</b>${_chgHtml}</span>
    <span class="ss-item">通道: ${chTxt} ${chDesc}</span>
    <span class="ss-item">关键压力: ${resTxt}</span>
    <span class="ss-item">关键支撑: ${supTxt}</span>
    <span class="ss-item">箱体: ${boxTxt}</span>
    <span class="ss-item cell-dim mono" style="font-size:10px">10s实时更新 · <span class="live-dot"></span>${nowTime()}</span>`;
}
function renderStockChart() {
  const data = stockChartData;
  if (!data) return;
  const period = data.period_data[stockChartPeriod];
  const levels = data.levels;
  const cur = data.current_price;
  // 显示开关
  const showLevels = (document.getElementById("tgLevels") || {}).checked !== false;
  const showBoxes = (document.getElementById("tgBoxes") || {}).checked !== false;
  const showChannel = (document.getElementById("tgChannel") || {}).checked !== false;
  const showMA = (document.getElementById("tgMA") || {}).checked !== false;

  // markLine: 支撑/压力 — 去重降密度 + 精简标签
  function dedupeLines(items) {
    const seen = {};
    return items.filter(it => {
      const k = Math.round(it.yAxis * 100);
      if (seen[k]) return false;
      seen[k] = true;
      return true;
    }).slice(0, 5);  // 最多 5 条，避免线太密集
  }
  const supportLines = dedupeLines(levels.supports.map(s => ({
    yAxis: s.price, lineStyle: { color: "#3fb950", type: "dashed", width: 1 },
    label: { formatter: `${s.price}`, color: "#3fb950", fontSize: 9, position: "insideEndBottom" },
  })));
  const resistanceLines = dedupeLines(levels.resistances.map(r => ({
    yAxis: r.price, lineStyle: { color: "#f85149", type: "dashed", width: 1 },
    label: { formatter: `${r.price}`, color: "#f85149", fontSize: 9, position: "insideEndTop" },
  })));
  const currentLine = { yAxis: cur, lineStyle: { color: "#e3b341", width: 1, type: "solid" },
    label: { formatter: `${cur}`, color: "#e3b341", fontSize: 9, position: "insideEndTop" } };

  // 箱体标注（半透明矩形）：当前箱体橙色高亮，历史箱体灰色
  const boxes = data.boxes || [];
  const boxIdx = period.dates;  // 当前周期的日期数组
  const boxAreas = boxes
    .filter(b => b.low && b.high)
    .map(b => {
      const i0 = boxIdx.indexOf(b.start);
      const i1 = boxIdx.indexOf(b.end);
      if (i0 < 0 || i1 < 0) return null;
      const isCur = b.rel === 0;
      const tag = isCur ? "当前箱体" : (b.rel === -1 ? "上方箱体" : "下方箱体");
      return [{ name: `${tag} ${fmt(b.low, 2)}~${fmt(b.high, 2)} (${b.days || ""}天)`,
                xAxis: i0, yAxis: b.low, itemStyle: { color: isCur ? "rgba(210,153,34,.35)" : "rgba(139,148,158,.12)",
                  borderColor: isCur ? "#d29922" : "#484f58", borderWidth: isCur ? 2 : 1, borderType: isCur ? "solid" : "dashed" } },
              { xAxis: i1, yAxis: b.high }];
    })
    .filter(Boolean);

  const maSeries = period.ma.map((maArr, i) => ({
    name: `MA${[5,10,20,30,60,180,365][i]}`, type: "line", data: maArr, symbol: "none",
    lineStyle: { width: 1, color: MA_COLORS[i] }, connectNulls: true,
  }));

  if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }
  const el = document.getElementById("stockChart");
  stockChartInst = echarts.init(el);
  window.stockChartInst = stockChartInst;  // 暴露供调试/检查

  stockChartInst.setOption({
    backgroundColor: "transparent",
    animation: false,
    legend: { top: 0, textStyle: { color: "#8b949e", fontSize: 10 }, type: "scroll" },
    ...(data.channel && data.channel.direction !== "flat" ? { title: [{
      text: (data.channel.direction === "up" ? "↗ 上行通道" : "↘ 下行通道")
        + (data.channel.reversal ? "  ↺ 通道反转" : ""),
      left: 6, top: 2,
      textStyle: {
        color: data.channel.direction === "up" ? "#f85149" : "#3fb950",
        fontSize: 11, fontWeight: "bold",
      },
    }] } : {}),
    tooltip: { trigger: "axis", axisPointer: { type: "cross" }, backgroundColor: "#161b22",
      borderColor: "#30363d", textStyle: { color: "#c9d1d9", fontSize: 11 },
      formatter: function (ps) {
        const k = ps.find(p => p.seriesName === "K线");
        if (!k) return "";
        const i = k.dataIndex;
        const ohlc = period.ohlc[i];
        if (!ohlc) return "";
        const o = ohlc[0], c = ohlc[1], lo = ohlc[2], hi = ohlc[3];
        const prevC = i > 0 ? period.ohlc[i - 1][1] : null;
        const chgPct = prevC ? (c - prevC) / prevC * 100 : null;
        const up = chgPct != null && chgPct >= 0;
        const col = up ? "#f85149" : "#3fb950";
        const chgTxt = chgPct == null ? "—" : `${up ? "+" : ""}${chgPct.toFixed(2)}%`;
        const vol = period.volume[i];
        const volTxt = vol != null
          ? (vol >= 1e8 ? (vol / 1e8).toFixed(2) + "亿" : (vol / 1e4).toFixed(2) + "万") : "—";
        const maIdx = [5, 10, 20, 30, 60, 180, 365];
        const maParts = (period.ma || []).map((arr, j) =>
          arr[i] != null ? `${maIdx[j]}:${arr[i]}` : null).filter(Boolean);
        let html = `<div style="font-weight:bold;margin-bottom:3px">${period.dates[i]}</div>`
          + `<div>开 <b>${o}</b>　收 <b style="color:${col}">${c}</b>　涨跌 <b style="color:${col}">${chgTxt}</b></div>`
          + `<div>高 <b>${hi}</b>　低 <b>${lo}</b>　量 <b>${volTxt}</b></div>`;
        if (maParts.length) html += `<div style="color:#8b949e">${maParts.join("　")}</div>`;
        return html;
      } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid: [
      { left: 60, right: 20, top: 34, height: "44%" },   // 主图: K线+MA+BOLL+支撑压力+箱体
      { left: 60, right: 20, top: "52%", height: "10%" }, // 成交量(独立窗口)
      { left: 60, right: 20, top: "64%", height: "11%" }, // MACD
      { left: 60, right: 20, top: "77%", height: "11%" }, // RSI
    ],
    xAxis: [
      { type: "category", data: period.dates, gridIndex: 0, axisLine: { lineStyle: { color: "#30363d" } },
        axisLabel: { show: false } },
      { type: "category", data: period.dates, gridIndex: 1, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#30363d" } } },
      { type: "category", data: period.dates, gridIndex: 2, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#30363d" } } },
      { type: "category", data: period.dates, gridIndex: 3, axisLabel: { color: "#8b949e", fontSize: 9 }, axisLine: { lineStyle: { color: "#30363d" } } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, axisLabel: { color: "#8b949e", fontSize: 9 },
        splitLine: { lineStyle: { color: "rgba(139,148,158,.12)" } } },
      { scale: true, gridIndex: 1, axisLabel: { color: "#8b949e", fontSize: 9 },
        splitLine: { show: false }, axisLabel: { show: false } },
      { scale: true, gridIndex: 2, axisLabel: { show: false }, splitLine: { show: false } },
      { min: 0, max: 100, gridIndex: 3, axisLabel: { color: "#8b949e", fontSize: 9 }, splitLine: { show: false } },
    ],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2, 3], start: 55, end: 100 },
      { type: "slider", xAxisIndex: [0, 1, 2, 3], bottom: 0, height: 18, start: 55, end: 100 },
    ],
    series: [
      { name: "K线", type: "candlestick", data: period.ohlc, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: "#f85149", color0: "#3fb950", borderColor: "#f85149", borderColor0: "#3fb950" },
        markArea: boxAreas.length && showBoxes ? {
          silent: true, data: boxAreas,
        } : undefined,
        markLine: { symbol: "none",
          data: [
            ...(showLevels ? [...resistanceLines, ...supportLines] : []), currentLine,
          ],
          label: { position: "insideEndTop" } } },
      // 通道色带：上轨实线/下轨虚线 + 区域填充。红涨绿跌：上行=红，下行=绿
      ...(showChannel && data.channel && data.channel.up_line.length ? [{
        name: "通道上轨", type: "line", xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        data: period.dates.map((_, i) => {
          const t = i / (period.dates.length - 1 || 1);
          return +(data.channel.up_line[0] + (data.channel.up_line[1] - data.channel.up_line[0]) * t).toFixed(3);
        }),
        lineStyle: { color: data.channel.direction === "up" ? "#f85149" : "#3fb950",
          width: 1.5, type: "solid" },
        markArea: { silent: true, data: [[{
          // 色带必须覆盖整段真实上下轨：上界=上轨线最大端值，下界=下轨线最小端值
          yAxis: Math.max(data.channel.up_line[0], data.channel.up_line[1]),
          xAxis: 0,
          name: data.channel.direction === "up" ? "↗ 上行通道" : "↘ 下行通道",
          itemStyle: { color: data.channel.direction === "up" ? "rgba(248,81,73,.10)" : "rgba(63,185,80,.10)",
            borderColor: data.channel.direction === "up" ? "rgba(248,81,73,.4)" : "rgba(63,185,80,.4)",
            borderWidth: 1, borderType: "solid" },
        }, { yAxis: Math.min(data.channel.dn_line[0], data.channel.dn_line[1]),
            xAxis: period.dates.length - 1 }]] },
      }, {
        name: "通道下轨", type: "line", xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        data: period.dates.map((_, i) => {
          const t = i / (period.dates.length - 1 || 1);
          return +(data.channel.dn_line[0] + (data.channel.dn_line[1] - data.channel.dn_line[0]) * t).toFixed(3);
        }),
        lineStyle: { color: data.channel.direction === "up" ? "#f85149" : "#3fb950",
          width: 1, type: "dashed" },
      }] : []),
      ...(showMA ? maSeries.map(s => ({ ...s, xAxisIndex: 0, yAxisIndex: 0 })) : []),
      // BOLL 叠加主图
      { name: "BOLL中", type: "line", data: period.boll.mid, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        lineStyle: { color: "rgba(139,148,158,.5)", width: 1 } },
      { name: "BOLL上", type: "line", data: period.boll.up, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        lineStyle: { color: "rgba(139,148,158,.3)", width: 1 } },
      { name: "BOLL下", type: "line", data: period.boll.dn, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        lineStyle: { color: "rgba(139,148,158,.3)", width: 1 } },
      // 成交量独立窗口
      { name: "成交量", type: "bar", data: period.volume, xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: { color: "rgba(88,166,255,.35)" }, barWidth: "60%" },
      // MACD 窗口
      { name: "MACD-DIF", type: "line", data: period.macd.dif, xAxisIndex: 2, yAxisIndex: 2,
        symbol: "none", lineStyle: { color: "#58a6ff", width: 1 } },
      { name: "MACD-DEA", type: "line", data: period.macd.dea, xAxisIndex: 2, yAxisIndex: 2,
        symbol: "none", lineStyle: { color: "#f85149", width: 1 } },
      { name: "MACD柱", type: "bar", data: period.macd.hist, xAxisIndex: 2, yAxisIndex: 2,
        itemStyle: { color: p => p.value >= 0 ? "#f85149" : "#3fb950" } },
      // RSI 窗口
      { name: "RSI", type: "line", data: period.rsi, xAxisIndex: 3, yAxisIndex: 3,
        symbol: "none", lineStyle: { color: "#bc8cff", width: 1 },
        markLine: { symbol: "none", data: [{ yAxis: 30, lineStyle: { color: "rgba(139,148,158,.4)", type: "dashed" } },
          { yAxis: 70, lineStyle: { color: "rgba(139,148,158,.4)", type: "dashed" } }] } },
    ],
  });
}

/* ---- 选股猎手 ---- */
let hunterLoaded = false;
function trendBadge(t) {
  if (t === "up") return `<span class="badge t-long" title="上行通道">上行↗</span>`;
  if (t === "down") return `<span class="badge t-short" title="下行通道">下行↘</span>`;
  return `<span class="badge chop">震荡→</span>`;
}
// 技术标签徽章
function tagBadge(t) {
  const colorMap = {
    up: "t-long", down: "t-short", warn: "approach", neutral: "chop",
  };
  const iconMap = {
    "上行": "↗", "下行": "↘", "震荡": "→",
    "箱体上沿": "🟠", "箱体下沿": "🔵", "箱体内部": "⚪",
    "向上突破": "🚀", "跌破下沿": "⚠️", "筑底": "🧱", "筑顶": "⛰️",
    "顶背离": "⛔", "底背离": "✅", "超买": "⚠", "超卖": "📉",
  };
  const icon = iconMap[t.label] || "";
  return `<span class="badge ${colorMap[t.color] || "chop"}" title="${esc(t.label)}">${icon}${esc(t.label)}</span>`;
}
// 建仓信号符合度徽章（时机门控：市场有方向/多头结构/回撤到位/金叉加分；盘后计算，实盘盘中显示"—"）
function buildBadge(s) {
  if (s.build_go === undefined) return '<span class="cell-dim" title="实盘盘中不计算，盘后点击今日数据显示">—</span>';
  const regimeCn = {trend_up: "多头", trend_dn: "空头", range: "震荡"}[s.build_regime] || (s.build_regime || "");
  const metTxt = `${regimeCn}·符合${s.build_met}/4`;
  if (s.build_go) {
    return `<span class="badge" title="建仓符合度: ${esc(s.build_reason || "")}" style="background:#1f6f3f;color:#fff">${metTxt}·GO</span>`;
  }
  return `<span class="badge" title="建仓符合度: ${esc(s.build_reason || "")}" style="background:#5a5a6a;color:#ccc">${metTxt}</span>`;
}
// 板块分页翻页
function hunterPage(category, dir) {
  if (!window._hunterPage) window._hunterPage = {};
  window._hunterPage[category] = (window._hunterPage[category] || 0) + dir;
  // 重渲染整个猎手表
  if (state.payload && state.payload.position_builder) {
    const data = window._hunterData;
    if (data) renderHunter(data);
  }
}
// 展开板块：批量拉个股通道标注 + 算板块阶段
async function toggleSectorExpand(tr, category) {
  const wrap = tr.nextElementSibling;
  wrap.classList.toggle("open");
  if (!wrap.classList.contains("open")) return;
  // 找该板块成分股代码
  const tbody = tr.parentElement;
  const sectorStocks = (window._hunterSectors || {})[category] || [];
  const stocks = sectorStocks || [];
  const codes = (stocks || []).map(s => s.code);
  if (!codes.length) return;
  const stageEl = tbody.querySelector(".h-stage-badge");
  try {
    const r = await apiCall("load_stock_tags_batch", codes);
    const tagsMap = (r && r.tags) || {};
    // 更新展开行标签列
    wrap.querySelectorAll(".h-expand-row").forEach(row => {
      const code = row.querySelector("td").textContent.trim();
      const info = tagsMap[code];
      if (info && info.tags) {
        row.querySelectorAll("td")[2].innerHTML = info.tags.map(t => tagBadge(t)).join(" ");
      }
    });
    // 板块阶段判定（用 trend）
    const vals = Object.values(tagsMap).map(x => x && x.trend).filter(Boolean);
    const up = vals.filter(v => v === "up").length;
    const down = vals.filter(v => v === "down").length;
    if (vals.length >= 5) {
      const ratio = up / (up + down || 1);
      const stage = ratio >= 0.6 ? '<span class="badge signal">🔥 强势上攻</span>'
        : ratio >= 0.4 ? '<span class="badge approach">➡️ 分化震荡</span>'
        : '<span class="badge t-short">📉 弱势下行</span>';
      if (stageEl) stageEl.innerHTML = stage;
    } else if (stageEl) {
      stageEl.innerHTML = `<span class="cell-dim">样本少</span>`;
    }
  } catch (e) { /* 静默 */ }
}
let hunterHistoryDates = [];

async function initHunterDates() {
  const sel = document.getElementById("hunterDate");
  if (!sel) return;
  try {
    const dates = await apiCall("available_hunter_dates");
    hunterHistoryDates = dates || [];
    sel.innerHTML = '<option value="">最新</option>' +
      hunterHistoryDates.map(d => `<option value="${d.date}">${d.date}</option>`).join("");
    sel.addEventListener("change", () => {
      if (sel.value) loadHunterHistory(sel.value);
    });
  } catch (e) { /* 静默 */ }
}

// ---- 主要指数概览（点击看K线，与个股一致）----
async function loadIndices() {
  const body = document.getElementById("indicesBody");
  const meta = document.getElementById("indicesMeta");
  if (!body) return;
  try {
    const d = await apiCall("load_indices");
    const idx = (d && d.indices) || [];
    if (!idx.length) {
      body.innerHTML = '<div class="empty">指数行情不可用</div>';
      return;
    }
    if (meta) meta.textContent = d.ts ? `更新于 ${d.ts}` : "";
    const regimeTxt = d.regime ? (d.days_in_regime ? ` · 大盘 ${esc(d.regime)} 第${d.days_in_regime}天` : ` · 大盘 ${esc(d.regime)}`) : "";
    body.innerHTML = `
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
        ${idx.map(i => {
          const cls = i.change_pct > 0.05 ? "up" : i.change_pct < -0.05 ? "down" : "cell-dim";
          return `<div class="idx-card" style="cursor:pointer;min-width:130px;text-align:center"
              onclick="openStockChart('${i.symbol}','${esc(i.name)}')" title="点击查看K线">
            <div style="font-size:13px;color:var(--text-dim)">${esc(i.name)}</div>
            <div class="mono" style="font-size:16px;font-weight:700">${fmt(i.price, 2)}</div>
            <div class="mono ${cls}" style="font-size:11px">${i.change_pct >= 0 ? '+' : ''}${fmt(i.change_pct, 2)}%</div>
          </div>`;
        }).join("")}
        <span class="cell-dim" style="font-size:11px">${regimeTxt}</span>
      </div>`;
  } catch (e) {
    body.innerHTML = `<div class="empty">指数加载失败: ${esc(e.message)}</div>`;
  }
}

// 重新生成选中日期结果（拉行情+评分）
async function regenerateHunter() {
  const sel = document.getElementById("hunterDate");
  const date = sel.value;
  if (!date) { statusEl("请先选择日期", "err"); return; }
  const btn = document.getElementById("hunterRegenBtn");
  const el = document.getElementById("hunterBody");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 重新生成中(约80s)..."; }
  el.innerHTML = '<div class="empty">⏳ 正在重新生成该日结果（重新拉行情+评分，约80秒）...</div>';
  try {
    const h = await apiCall("load_hunter", date);
    if (h && h.available) {
      renderHunter(h, true);
      hunterLoaded = true;
      // 仅重新生成"今日"时同步扫描突破箱体（历史日期后端也按最新日线扫，会误导）
      if (!date || date === todayStr()) loadBreakoutStocks(true);
      statusEl(`${date} 已重新生成`, "ok");
    } else {
      el.innerHTML = `<div class="empty">${esc((h && h.error) || '生成失败')}</div>`;
    }
  } catch (e) {
    el.innerHTML = `<div class="empty">重新生成失败: ${esc(e.message)}</div>`;
  }
  if (btn) { btn.disabled = false; btn.textContent = "🔄 重新生成"; }
}

async function loadHunterHistory(date) {
  // 支持扫描任意日期：后台运行 + 进度条（热快照日期也走全量，带建仓符合度）
  const el = document.getElementById("hunterBody");
  const btn = document.getElementById("hunterRunBtn");
  el.innerHTML = '<div class="empty">加载历史数据...</div>';
  try {
    await apiCall("run_hunter", date);
    let p = await apiCall("hunter_progress");
    renderHunterProgress(el, p);
    let guard = 0;
    while ((p.running || !p.ready) && guard < 600) {
      await sleep(1000);
      p = await apiCall("hunter_progress");
      renderHunterProgress(el, p);
      guard++;
    }
    const h = await apiCall("load_hunter", date);
    if (h.available) {
      renderHunter(h, true);
      hunterLoaded = true;
      statusEl(`已加载 ${date} 选股猎手数据`, "ok");
    } else {
      el.innerHTML = `<div class="empty">${esc(h.error || '无数据')}</div>`;
    }
  } catch (e) {
    el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
  }
}
async function showSectorHistory(sector) {
  const overlay = document.createElement("div");
  overlay.className = "modal-mask";
  overlay.innerHTML = `<div class="modal" style="width:520px;max-height:80vh">
    <div class="modal-title"><span>${esc(sector)} · 板块历史</span>
      <button class="mini-btn" onclick="this.closest('.modal-mask').remove()">×</button></div>
    <div class="modal-body" style="font-size:12px">加载中...</div>
  </div>`;
  document.body.appendChild(overlay);
  const body = overlay.querySelector(".modal-body");
  try {
    const sh = await apiCall("sector_history", sector);
    const pts = sh.points || [];
    body.innerHTML = pts.length
      ? `<table class="h-table"><thead><tr><th>日期</th><th class="num">热度</th><th class="num">均分</th><th class="num">涨停</th><th class="num">股票数</th></tr></thead>
         <tbody>${pts.map(p => `<tr>
           <td class="mono">${esc(p.date)}</td>
           <td class="num ${p.heat >= 60 ? 'up' : p.heat >= 40 ? 'warn' : 'cell-dim'}">${fmt(p.heat, 1)}</td>
           <td class="num">${fmt(p.avg, 2)}</td>
           <td class="num">${p.limit_up || 0}</td>
           <td class="num">${p.count || 0}</td>
         </tr>`).join("")}</tbody></table>`
      : '<div class="empty">该板块无历史数据</div>';
  } catch (e) {
    body.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
  }
}
async function addNewWatchlist() {
  const code = document.getElementById("pbSearchCode").value.trim();
  const name = document.getElementById("pbSearchName").value.trim();
  if (!code) { statusEl("请输入股票代码", "err"); return; }
  if (!/^\d{6}$/.test(code)) { statusEl("代码格式应为6位数字", "err"); return; }
  // 若名称空则尝试自动取
  let finalName = name;
  if (!finalName) {
    try {
      const r = await apiCall("search_stock", code);
      const hit = (r.results || []).find(x => x.code === code);
      if (hit) finalName = hit.name;
    } catch (e) { /* 静默 */ }
  }
  const ok = await addToWatchlist(code, finalName || code, null);
  if (ok) {
    document.getElementById("pbSearchCode").value = "";
    document.getElementById("pbSearchName").value = "";
  }
}
async function addToWatchlist(code, name, btn) {
  try {
    const r = await apiCall("add_to_watchlist", code, name);
    if (r && r.ok) {
      if (btn) { btn.textContent = "✓已加"; btn.style.color = "#3fb950"; btn.style.borderColor = "#3fb950"; }
      statusEl(`${esc(code)} ${esc(name)} 已加入建仓股池`, "ok");
      // 自动刷新建仓扫描表
      if (state.date) apiCall("refresh_pb", state.date).then(pb => renderPB(pb || {})).catch(() => {});
    } else {
      statusEl(`加入失败: ${r ? r.error : '未知'}`, "err");
    }
  } catch (e) {
    statusEl(`加入失败: ${e.message}`, "err");
  }
}
async function confirmPosition(code, rowEl) {
  try {
    const inp = prompt(`确认建仓 ${code}：输入确认价（回车用建议价）`);
    const price = (inp !== null && inp.trim() !== "") ? parseFloat(inp) : null;
    const r = await apiCall("confirm_position", code, price, null);
    if (r && r.ok) {
      statusEl(`${code} 已确认建仓 @${r.entry.confirm_price}×${r.entry.confirm_qty}（signal_history 已回写）`, "ok");
      if (rowEl) rowEl.style.opacity = "0.5";
      if (state.date) apiCall("refresh_pb", state.date).then(pb => renderPB(pb || {})).catch(() => {});
    } else {
      statusEl(`确认失败: ${r ? r.error : '未知'}`, "err");
    }
  } catch (e) {
    statusEl(`确认失败: ${e.message}`, "err");
  }
}

async function removeFromWatchlist(code, rowEl) {
  try {
    const r = await apiCall("remove_from_watchlist", code);
    if (r && r.ok) {
      if (rowEl) rowEl.remove();
      statusEl(`${esc(code)} 已从建仓股池移除`, "ok");
      if (state.date) apiCall("refresh_pb", state.date).then(pb => renderPB(pb || {})).catch(() => {});
    } else {
      statusEl(`移除失败: ${r ? r.error : '未知'}`, "err");
    }
  } catch (e) {
    statusEl(`移除失败: ${e.message}`, "err");
  }
}
let hunterRunning = false;
// 选股猎手运行进度条（轮询 hunter_progress）
function renderHunterProgress(el, p) {
  const total = p.total || 0;
  const pct = total ? Math.min(100, Math.round(p.done / total * 100)) : 0;
  const phase = p.phase || "准备中";
  const pctTxt = total ? `${p.done}/${total} 只 (${pct}%)` : "进行中...";
  // total=0（如缓存命中/阶段间）显示不确定进度条 + 阶段名，避免卡"准备中"
  const barInner = total
    ? `<div style="height:100%;width:${pct}%;background:#3a8f6a;border-radius:4px;transition:width .5s"></div>`
    : `<div style="height:100%;width:40%;background:#3a8f6a;border-radius:4px;animation:hunterInd 1.2s infinite linear"></div>`;
  el.innerHTML = `
    <div style="padding:14px;max-width:520px;margin:0 auto">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span>⏳ 选股猎手运行中：<b>${esc(phase)}</b></span>
        <span class="mono cell-dim" style="font-size:11px">${pctTxt}</span>
      </div>
      <div style="height:8px;background:var(--bg2,#2a2a3a);border-radius:4px;overflow:hidden">
        ${barInner}
      </div>
      <div class="cell-dim" style="font-size:10px;margin-top:6px">${esc(p.msg || "")}${p.running ? "（首次运行需拉取全量日线约2分钟，之后同日期秒级）" : ""}</div>
    </div>`;
}
// 不确定进度条动画（total=0 阶段）
const _hunterStyle = document.createElement("style");
_hunterStyle.textContent = "@keyframes hunterInd{0%{transform:translateX(-100%)}100%{transform:translateX(300%)}}";
document.head.appendChild(_hunterStyle);
async function loadHunter(force) {
  if (!force && hunterLoaded) return;
  if (hunterRunning) return;
  const el = document.getElementById("hunterBody");
  const btn = document.getElementById("hunterRunBtn");
  hunterRunning = true;
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 运行中..."; }
  const date = state.date || todayStr();
  try {
    await apiCall("run_hunter", date);   // 后台启动，立即返回
    // 轮询进度直至完成
    let p = await apiCall("hunter_progress");
    renderHunterProgress(el, p);
    let guard = 0;
    while ((p.running || !p.ready) && guard < 600) {  // 最多等 10 分钟
      await sleep(1000);
      p = await apiCall("hunter_progress");
      renderHunterProgress(el, p);
      guard++;
    }
    const h = await apiCall("load_hunter", date);
    renderHunter(h);
    hunterLoaded = true;
    // 突破箱体扫描（后台并行，后端当日缓存）
    loadBreakoutStocks(true);
  } catch (e) {
    el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
  }
  hunterRunning = false;
  if (btn) { btn.disabled = false; btn.textContent = "🔄 刷新数据"; }
}

// ---- 突破箱体股票（单独拎出）----
let breakoutLoaded = false;
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
async function loadBreakoutStocks(force) {
  const body = document.getElementById("hunterBreakoutBody");
  if (!body) return;
  if (!force && breakoutLoaded) return;
  const btn = document.getElementById("hunterBreakoutBtn");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 扫描中..."; }
  body.innerHTML = '<div class="empty">⏳ 准备扫描...</div>';
  try {
    const st = await apiCall("start_breakout_scan");
    if (st.status === "done") {
      renderBreakout(st);
      breakoutLoaded = true;
      if (btn) { btn.disabled = false; btn.textContent = "🔄 重新扫描"; }
      return;
    }
    if (st.status === "error") {
      body.innerHTML = `<div class="empty">扫描失败: ${esc(st.error || '未知')}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = "🔄 重新扫描"; }
      return;
    }
    await pollBreakoutScan(body, btn);   // running → 轮询进度
  } catch (e) {
    const cur = document.getElementById("hunterBreakoutBody");
    if (cur) cur.innerHTML = `<div class="empty">突破箱体扫描失败: ${esc(e.message)}</div>`;
    if (btn) { btn.disabled = false; btn.textContent = "🔄 重新扫描"; }
  }
}
async function pollBreakoutScan(body, btn) {
  const t0 = Date.now();
  const MAX = 300000; // 5 分钟兜底
  while (Date.now() - t0 < MAX) {
    await sleep(800);
    if (!document.body.contains(body)) return;   // 标签已切换
    let st;
    try { st = await apiCall("get_breakout_scan"); } catch (e) { break; }
    if (st.status === "done") {
      renderBreakout(st);
      breakoutLoaded = true;
      if (btn) { btn.disabled = false; btn.textContent = "🔄 重新扫描"; }
      return;
    }
    if (st.status === "error") {
      body.innerHTML = `<div class="empty">扫描失败: ${esc(st.error || '未知')}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = "🔄 重新扫描"; }
      return;
    }
    if (st.status === "running") {
      const pct = st.total ? Math.max(2, Math.round(st.done / st.total * 100)) : 0;
      const found = st.found || 0;
      const preview = (st.stocks || []).slice(0, 5).map(x => esc(x.name)).join('、');
      body.innerHTML = `
        <div class="bk-scan">
          <div class="bk-scan-row"><span>⏳ 突破箱体扫描中</span>
            <span class="cell-dim mono">${st.done}/${st.total} 只 · ${pct}%</span></div>
          <div class="h-bar-track" style="height:6px;margin-top:4px">
            <div class="h-bar-fill up" style="width:${pct}%"></div></div>
          <div class="bk-scan-row" style="margin-top:6px">
            <span class="cell-dim">已发现 <b class="up">${found}</b> 只</span>
            ${preview ? `<span class="cell-dim" style="margin-left:8px">${preview}${found > 5 ? '…' : ''}</span>` : ''}
          </div>
        </div>`;
    }
  }
  body.innerHTML = '<div class="empty">扫描超时，请点击"重新扫描"重试</div>';
  if (btn) { btn.disabled = false; btn.textContent = "🔄 重新扫描"; }
}
function renderBreakout(b) {
  const body = document.getElementById("hunterBreakoutBody");
  if (!body) return;
  const stocks = (b && b.stocks) || [];
  const meta = document.getElementById("hunterBreakoutMeta");
  if (meta) meta.textContent = `· 共 ${stocks.length} 只`;
  if (!stocks.length) {
    body.innerHTML = '<div class="empty">今日池内暂无突破箱体的股票</div>';
    return;
  }
  body.innerHTML = `<table class="h-table"><thead><tr>
    <th>代码</th><th>名称</th><th class="num">现价</th><th>趋势</th><th>技术标签</th>
  </tr></thead><tbody>
  ${stocks.map(s => `<tr class="h-expand-row" ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')">
    <td class="mono cell-dim" title="双击看技术分析">${esc(s.code)}</td>
    <td title="双击看技术分析">${esc(s.name)} <button class="mini-btn" style="font-size:10px;padding:0 5px"
      onclick="event.stopPropagation();addToWatchlist('${esc(s.code)}','${esc(s.name)}',this)" title="加入建仓股池监控买点">+股池</button></td>
    <td class="num">${s.price != null && s.price !== '' ? fmt(s.price, 2) : '—'}</td>
    <td>${trendBadge(s.trend)}</td>
    <td>${(s.tags || []).map(t => tagBadge(t)).join(" ")}</td>
  </tr>`).join("")}
  </tbody></table>
  <div class="cell-dim" style="font-size:10px;margin-top:3px">双击看技术分析 · 加入建仓股池监控买点 · 当日缓存</div>`;
}
function renderHunter(h) {
  const el = document.getElementById("hunterBody");
  if (!h || !h.available) {
    el.innerHTML = `<div class="empty">选股猎手数据不可用${h && h.error ? ': ' + esc(h.error) : ''}</div>`;
    return;
  }

  const sumRows = h.summary_rows || [];
    const ss = h.sector_stocks || {};
    // 存储板块成分股供展开标注/翻页
    window._hunterSectors = ss || {};
    window._hunterData = h;
    const trends = h.concept_trends || {};
    function trendSpark(pts, key, color) {
      if (!pts || pts.length < 2) return '<span class="cell-dim">—</span>';
      const vals = pts.map(p => p[key]).filter(v => v != null);
      if (vals.length < 2) return '<span class="cell-dim">—</span>';
      const W = 60, H = 20, minV = Math.min(...vals), maxV = Math.max(...vals), span = (maxV - minV) || 1;
      const xs = vals.map((_, i) => i / (vals.length - 1) * W);
      const ys = vals.map(v => H - 2 - (v - minV) / span * (H - 4));
      const line = xs.map((x, i) => x + ',' + ys[i]).join(' ');
      return '<svg width="' + W + '" height="' + H + '" style="vertical-align:middle"><polyline points="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.5"/></svg>';
    }

  // Sheet1: 可展开行
  const sumBody = sumRows.map((r, i) => {
    const category = r["板块"] || "";
    const heatInfo = {}; // unused
    const trendRaw = r["趋势"] || "";
    const trendIcon = (trendRaw.match(/[🔥📈➡️📉🧊]/) || [""])[0];
    const trendNum = (trendRaw.match(/[+-]?\d+/) || [""])[0];
    const heatChange = trendNum !== "" ? parseInt(trendNum) : null;
    const heatCls = heatChange != null ? (heatChange > 5 ? "up" : heatChange < -5 ? "down" : "warn") : "cell-dim";

    const stocks = ss[category] || [];
    const d5Hits = stocks.filter(s => s.d5 > 0).length;
    const d6Hits = stocks.filter(s => s.d6 > 0).length;

    // 展开的个股明细（全部成分股，分页50）
    const PAGE = 50;
    const pageIdx = window._hunterPage && window._hunterPage[category] || 0;
    const allStocks = stocks;
    const paged = allStocks.slice(pageIdx * PAGE, (pageIdx + 1) * PAGE);
    const stockRows = paged.map(s => {
      const d5Cls = s.d5 >= 8 ? "up" : s.d5 >= 5 ? "warn" : s.d5 > 0 ? "cell-dim" : "";
      const d6Cls = s.d6 >= 8 ? "up" : s.d6 >= 5 ? "warn" : s.d6 > 0 ? "cell-dim" : "";
      const trendTxt = s.tags && s.tags.length
        ? s.tags.map(t => tagBadge(t)).join(" ")
        : '<span class="cell-dim" style="font-size:10px">…</span>';
      return `<tr class="h-expand-row" ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')">
        <td class="mono cell-dim" title="双击看技术分析">${esc(s.code)}</td>
        <td title="双击看技术分析">${esc(s.name)} <button class="mini-btn" style="font-size:10px;padding:0 5px"
          onclick="event.stopPropagation();addToWatchlist('${esc(s.code)}','${esc(s.name)}',this)"
          title="加入建仓股池监控买点">+股池</button></td>
        <td>${trendTxt}</td>
        <td class="num ${s.score >= 70 ? 'up' : s.score >= 50 ? 'warn' : 'cell-dim'}"><b>${s.score}</b></td>
        <td class="num ${d5Cls}">${s.d5 || "—"}</td>
        <td class="num ${d6Cls}">${s.d6 || "—"}</td>
        <td class="num">${s.d9 || "—"}</td>
        <td class="num ${clsOf(s.change_pct)}">${s.change_pct >= 0 ? '+' : ''}${fmt(s.change_pct, 1)}%</td>
        <td>${s.limit_up ? '<span class="badge signal">涨停</span>' : ''}</td>
        <td>${buildBadge(s)}</td>
      </tr>`;
    }).join("");
    const pageInfo = allStocks.length > PAGE
      ? `<div class="h-pager"><button class="mini-btn" onclick="hunterPage('${esc(category)}',-1)" ${pageIdx===0?'disabled':''}>←</button>
         ${pageIdx + 1}/${Math.ceil(allStocks.length / PAGE)} 页（共${allStocks.length}只）
         <button class="mini-btn" onclick="hunterPage('${esc(category)}',1)" ${(pageIdx+1)*PAGE>=allStocks.length?'disabled':''}>→</button></div>` : "";

    const rank = i + 1;
    const medal = rank === 1 ? "🥇" : rank === 2 ? "🥈" : rank === 3 ? "🥉" : "";
    const scoreVal = r["平均分"] || 0;
    const scorePct = Math.min(100, Math.max(0, scoreVal));
    const scoreCls = scoreVal >= 70 ? "h-hot" : scoreVal >= 40 ? "h-warm" : "h-cool";
    const barW = Math.max(2, scorePct);

    const heatVal = r["热度"] != null ? r["热度"] : 0;
    const heatBarW = Math.max(2, Math.min(100, heatVal));

    return `<tbody class="h-sector-group" data-category="${esc(category)}">
      <tr class="h-main-row" onclick="toggleSectorExpand(this,'${esc(category)}')" style="cursor:pointer">
        <td class="num mono" style="font-size:14px;font-weight:700">${medal || rank}</td>
        <td>
          <div class="h-name">${esc(category)}
            <a href="#" onclick="event.stopPropagation();showSectorHistory('${esc(category)}');return false;"
               title="查看板块历史情况" style="text-decoration:none;color:var(--accent)">🔗</a>
          </div>
          <div class="h-sub">${r["细分数量"]||0}个细分 · ${r["股票数"]||(ss[category]||[]).length||0}只
            ${d5Hits > 0 ? ` · <b class="up">D5×${d5Hits}</b>` : ""}
            ${d6Hits > 0 ? ` · <b class="warn">D6×${d6Hits}</b>` : ""}
          </div>
        </td>
        <td style="width:140px">
          <div class="h-score-big ${scoreCls}">${fmt(scoreVal, 1)}<span class="h-score-unit">分</span></div>
          <div class="h-bar-track"><div class="h-bar-fill ${scoreCls}" style="width:${barW}%"></div></div>
        </td>
        <td class="num">${r["涨停数"]||0}</td>
        <td style="min-width:70px"><span class="h-stage-badge cell-dim">阶段…</span></td>
        <td style="width:100px">
          <span class="mono cell-dim" style="font-size:10px">${trendSpark(trends[category]||[], 'heat', '#d29922')}</span><div class="h-heat-val ${heatCls}">${heatVal > 0 ? fmt(heatVal, 0) : "—"}</div>
          <div class="h-bar-track h-bar-sm"><div class="h-bar-fill ${heatCls}" style="width:${heatBarW}%"></div></div>
        </td>
        <td style="min-width:70px">
          <span class="h-trend-badge ${heatChange != null ? (heatChange > 0 ? 'h-trend-up' : heatChange < 0 ? 'h-trend-down' : 'h-trend-flat') : 'h-trend-flat'}">
            ${trendIcon || '—'} ${heatChange != null ? (heatChange>=0?'+':'')+fmt(heatChange,0) : ''}
          </span>
        </td>
        <td class="cell-dim" style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r["前三强"]||'')}">${esc(r["前三强"]||'—')}</td>
      </tr>
      <tr class="h-expand-wrap"><td colspan="8" style="padding:0">
        <div class="h-expand">
          <table><thead><tr>
            <th>代码</th><th>名称</th><th>技术标签</th><th class="num">得分</th>
            <th class="num" title="潜在突破10日">D5</th>
            <th class="num" title="潜在突破5日">D6</th>
            <th class="num">D9</th>
            <th class="num">涨跌</th><th>状态</th>
            <th title="建仓信号符合度（时机门控：市场有方向/多头结构/回撤到位/金叉加分；盘后计算，GO=符合）">建仓</th>
          </tr></thead>
          <tbody>${stockRows}</tbody></table>
          ${pageInfo}
          <div class="cell-dim" style="font-size:10px;margin-top:3px">D5=潜在突破10日(≥8强) · D6=潜在突破5日(≥8强) · D9=活跃程度 · 点击行收起</div>
        </div>
      </td></tr>
    </tbody>`;
  }).join("");

  // 突破箱体扫描仅对"今日"数据有意义（后端按最新日线收盘扫描，当日缓存）
  const isHistory = h.is_history || (h.date && h.date !== todayStr());
  const breakoutCard = isHistory ? "" : `
    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div class="card-title">🚀 突破箱体（现价向上突破最近箱体上沿 ≤8%）
        <button class="mini-btn" id="hunterBreakoutBtn" style="margin-left:8px" onclick="loadBreakoutStocks(true)">🔄 重新扫描</button>
        <span class="cell-dim" id="hunterBreakoutMeta" style="font-size:11px"></span>
      </div>
      <div id="hunterBreakoutBody"><div class="empty">点击"运行今日数据"后自动扫描突破箱体的股票</div></div>
    </div>`;

  el.innerHTML = `
    <div class="cell-dim" style="margin-bottom:8px;display:flex;gap:16px;flex-wrap:wrap">
      <span>打分池: <b>${h.pool_size}</b> 只</span>
      <span>概念: <b>${sumRows.length}</b> 个</span>
      <span class="mono"><span class="live-dot"></span> ${esc(h.refreshed_at || '')}</span>
    </div>

    ${breakoutCard}

    <div class="card" style="overflow-x:auto">
    <table class="h-table">
      <thead><tr>
        <th class="num">#</th><th>板块</th><th class="num">均分</th><th class="num">涨停</th>
        <th class="num">热度</th><th>趋势</th><th>前三强</th>
      </tr></thead>
      ${sumBody}
    </table></div>

    <div class="cell-dim" style="font-size:10px;margin-top:6px">数据源: stock_hunter (韭研概念打分) · 点击板块展开个股明细(D5/D6异常) · 热度趋势vs前日</div>`;
}

/* ---- 持仓日线体检 ---- */
function renderOB(ob) {
  const el = document.getElementById("obBody");
  if (!ob || !ob.stocks || !ob.stocks.length) {
    el.innerHTML = '<div class="empty">无持仓体检数据</div>';
    return;
  }
  const rows = ob.stocks.map(s => {
    if (s.error) return `<tr><td colspan="10" class="cell-dim">${esc(s.code)}: ${esc(s.error)}</td></tr>`;
    const ob_ = s.overbought || {};
    const dv = s.divergence || {};
    const mark = (cond) => cond ? `<span class="badge signal">⚠</span>` : `<span class="badge chop">✓</span>`;
    const advCls = s.risk === "高" ? "up" : s.risk === "中" ? "warn" : "cell-dim";
    const trendTxt = s.trend === "down" ? `<b class="down">下行↘</b>`
      : s.trend === "up" ? `<b class="up">上行↗</b>` : `<span class="cell-dim">震荡→</span>`;
    const dvTxt = dv.count ? [
      dv.macd ? "MACD" : "", dv.rsi ? "RSI" : "", dv.kdj ? "KDJ" : "", dv.vol ? "量价" : ""
    ].filter(Boolean).join("/") : "无";
    return `<tr ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')" style="cursor:pointer" title="双击看K线">
      <td>${esc(s.name)} <span class="mono cell-dim">${esc(s.code)}</span></td>
      <td class="num">${fmt(s.price, 2)}</td>
      <td>${trendTxt}</td>
      <td class="num ${ob_.rsi ? 'up' : 'cell-dim'}">${fmt(ob_.rsi, 0)} ${mark(ob_.rsi)}</td>
      <td class="num ${ob_.kdj ? 'up' : 'cell-dim'}">${fmt(ob_.kdj, 0)} ${mark(ob_.kdj)}</td>
      <td class="num ${ob_.cci ? 'up' : 'cell-dim'}">${fmt(ob_.cci, 0)} ${mark(ob_.cci)}</td>
      <td>${mark(ob_.boll)}</td>
      <td class="${dv.count ? 'warn' : 'cell-dim'}">${dv.count ? dvTxt : "无"}</td>
      <td class="num ${advCls}">${s.risk === "高" ? "高" : s.risk === "中" ? "中" : "低"}</td>
      <td><span class="${advCls}">${esc(s.advice)}</span></td>
    </tr>`;
  }).join("");
  el.innerHTML = `
    <div class="card" style="overflow-x:auto">
      <table><thead><tr>
        <th>股票</th><th class="num">现价</th><th>趋势</th>
        <th class="num" title="RSI>70超买">RSI</th>
        <th class="num" title="KDJ J>100超买">KDJ-J</th>
        <th class="num" title="CCI>100超买">CCI</th>
        <th title="收盘破BOLL上轨">BOLL</th>
        <th title="MACD/RSI/KDJ/量价顶背离">顶背离</th>
        <th class="num">风险</th>
        <th>风险提醒</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="cell-dim" style="font-size:10px;margin-top:4px">目的：发现超买+趋势下行风险 → 提醒减仓/回避（非建仓建议） · 超买≥2或顶背离→高风险 · 双击行看K线</div>
    </div>`;
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
  // 切到选股猎手：已加载则显示，否则提示点击按钮
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
async function maybeAutoSwitchToToday() {
  // 启动时今天尚无盘后数据被挪到末尾、看板停在历史日时，一旦今天数据出现自动切回
  if (userPinnedDate) return;                 // 用户手动固定了历史日，不强切
  if (state.date === todayStr()) return;      // 已是今天
  try {
    const dates = await apiCall("available_dates");
    if (dates && dates[0] === todayStr() && state.date !== todayStr()) {
      statusEl("今日盘内数据已就绪，自动切到今天", "ok");
      userPinnedDate = false;
      await loadAndRender(todayStr(), true);  // 自动切回今天 → isToday=true → live 启动
    }
  } catch (e) { /* 静默 */ }
}
function startPoll() {
  stopPoll();
  pollTimer = setInterval(() => {
    if (state.date) {
      loadAndRender(state.date, true);
      maybeAutoSwitchToToday();
    }
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
    if (dateSelect.value) {
      // 用户手动选日期（非初始默认赋值）→ 记录，避免自动切回今天覆盖用户选择
      if (dateSelect.value !== todayStr()) userPinnedDate = true;
      loadAndRender(dateSelect.value, false);
    }
  });
  refreshBtn.addEventListener("click", () => {
    if (dateSelect.value) loadAndRender(dateSelect.value, false);
  });
  // 选股猎手运行按钮 + 历史日期下拉
  const hunterBtn = document.getElementById("hunterRunBtn");
  if (hunterBtn) hunterBtn.addEventListener("click", () => loadHunter(true));
  const hunterRegen = document.getElementById("hunterRegenBtn");
  if (hunterRegen) hunterRegen.addEventListener("click", regenerateHunter);
  initHunterDates();
  loadIndices();
  setInterval(loadIndices, 60000);  // 指数行情每分钟刷新
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
  // 启动时今天可能尚无数据导致停在历史日 → 立即检查一次，今天就绪则自动切回
  maybeAutoSwitchToToday();
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
