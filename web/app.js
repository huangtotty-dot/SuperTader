/* 做T复盘决策看板 - 前端渲染逻辑 */
"use strict";

/* ================= 条件详情面板 iframe 桥 =================
   条件详情面板通过 postMessage 请求本页面调用 get_signal_condition_detail() 并回传。 */
window.addEventListener("message", (e) => {
  if (!e.data || e.data.type !== "condition-detail-request") return;
  const { code, date } = e.data;
  Promise.resolve()
    .then(() => (window.pywebview && window.pywebview.api)
      ? window.pywebview.api.get_signal_condition_detail(code, date)
      : null)
    .then(detail => {
      if (e.source && e.source.postMessage) {
        e.source.postMessage({ type: "condition-detail-response", detail: detail || {} }, "*");
      }
    })
    .catch(() => {
      if (e.source && e.source.postMessage) {
        e.source.postMessage({ type: "condition-detail-response", detail: {} }, "*");
      }
    });
});

/* ================= 打开条件详情面板 ================= */
function openConditionDetail(code, name) {
  const url = `condition_detail_panel.html?code=${encodeURIComponent(code)}&date=${encodeURIComponent(state.date || new Date().toISOString().split('T')[0])}`;

  // 创建 modal 弹窗
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999;';

  const modal = document.createElement('div');
  modal.style.cssText = 'background:white;border-radius:10px;width:90%;max-width:650px;height:80vh;position:relative;box-shadow:0 10px 40px rgba(0,0,0,0.2);display:flex;flex-direction:column;';

  const header = document.createElement('div');
  header.style.cssText = 'padding:12px 16px;border-bottom:1px solid #e0e4e8;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;';
  header.innerHTML = `<div style="font-weight:700;">${esc(name || code)} - 建仓条件详情</div><button style="background:none;border:none;font-size:24px;cursor:pointer;padding:0;width:32px;height:32px;display:flex;align-items:center;justify-content:center;">×</button>`;
  header.querySelector('button').onclick = () => overlay.remove();

  const iframe = document.createElement('iframe');
  iframe.src = url;
  iframe.style.cssText = 'width:100%;flex:1;border:none;';

  modal.appendChild(header);
  modal.appendChild(iframe);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}

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
// A股配色：盈利红 / 亏损绿（与 .up/.down 一致）
function pnlColor(v) {
  const n = Number(v);
  if (v === null || v === undefined || isNaN(n) || n === 0) return "#8b949e";
  return n > 0 ? "#e8531f" : "#229652";
}
function dayTypeBadge(t) {
  if (!t) return "";
  const map = { bull_day: "bull", bear_day: "bear", chop_day: "chop", reversal_day: "reversal" };
  const cls = map[t] || "chop";
  const label = { bull_day: "bull", bear_day: "bear", chop_day: "chop", reversal_day: "reversal" }[t] || t;
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}
function verdictBadge(v) {
  const map = { signal: "signal", approaching: "approach", weak: "weak", insufficient_data: "nodata", pending: "nodata", archived: "nodata" };
  const label = { signal: "signal", approaching: "approaching", weak: "weak", insufficient_data: "无数据", pending: "等待扫描", archived: "已停用" }[v] || v;
  // 2026-08-27: watch_signal（震荡市观察态：多头结构+浅回撤，只喂样本不出建议）独立样式，不再落入 weak
  if (v === "watch_signal") {
    return `<span class="badge" title="震荡市观察态：个股结构+回撤达标但市场无方向，signal 结构性不可达；不推飞书不出建议" style="background:#2a4a5a;color:#8ad4ff">观察</span>`;
  }
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
  if (!el) return;  // 防御：元素不存在时静默忽略
  el.className = "status " + (cls || "");
  el.textContent = msg;
}
// 轻提示：右上角浮层，约 4s 自动消失、不阻断操作，可堆叠。
// 用于"添加成功"这类无需用户确认的回执（顶部 #status 离操作区太远，等于没反馈）
function toast(msg, cls) {
  let host = document.getElementById("toastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "toastHost";
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = "toast " + (cls || "");
  el.textContent = msg;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 320);
  }, 4000);
}
// 结果模态窗：必须点掉才能继续。用于"失败"这类需要用户看清原因的情况。
// bodyHtml 由调用方拼装（需自行 esc 用户数据）
function showResultModal(title, bodyHtml, isErr) {
  const overlay = document.createElement("div");
  overlay.className = "modal-mask";
  overlay.innerHTML = `<div class="modal" style="width:520px;max-height:80vh">
    <div class="modal-title"><span class="${isErr ? "down" : "up"}">${esc(title)}</span>
      <button class="mini-btn" onclick="this.closest('.modal-mask').remove()">×</button></div>
    <div class="modal-body" style="font-size:12px;line-height:1.7">${bodyHtml}</div>
  </div>`;
  document.body.appendChild(overlay);
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
let state = { date: null, payload: null, trend: [], accountsDetail: {},
              tradeHistory: null, accountList: [] };   // accountList: 历史成交为源的账户列表
let userPinnedDate = false;   // 用户手动选了历史日期时置 true，防止自动切回今天覆盖用户选择

// ===== 投递层保护（2026-10-10）：超时 + 在飞上限 =====
// 为什么要这两条（实测证据，见 t_io/logs/gui_thread_dumps.log 解析）：
//   · pywebview 对**每次** js_api 调用 `Thread(target=_call).start()` —— 无池无上限；
//   · 每个返回值都要经 `window.evaluate_js` → `self.webview.Invoke(...)`（**同步**编组回 UI 线程）
//     → `semaphore.acquire()`（**无超时**，edgechromium.py:160）。
//   · 实测一次卡死现场 68 个线程里 **25 个堵在这条返回路径上**（21 个躺在 semaphore.acquire，
//     4 个躺在 Invoke），另有 12 个在 pandas、4 个在网络。
//   ⇒ 只要某个返回值没送达，那个线程**永不退出**（线程数曾达 114）。这就是"卡死"而非"慢"：
//     挂住的调用连 `finally` 都跑不到，**根本不会出现在 gui_slow_calls.log 里**
//     （所以日志只有 43 条慢调用、进程里却有上百线程）。
// 两条对策：
//   1) **超时**：调用超过 _API_TIMEOUT_MS 就放弃等待。否则 `await` 永不 resolve，
//      配上「非重叠轮询」会让该轮询器**永久停摆**（我上一轮改动引入的风险）。
//   2) **在飞上限**：全局最多 _API_MAX_INFLIGHT 个并发。到达率不必超过服务率就能靠堆积
//      把队列撑爆——限流把队列钉成有界。
const _API_TIMEOUT_MS = 20000;
// 少数**本质长时**的后端方法不能套 20s：`recompute_pb` 要重放整日建仓扫描（自身注释即写
// 「约需 1-2 分钟」）⇒ 套 20s 必然报「重跑失败: 超时」而后端其实已经算完、结果被丢弃。
// 这类方法要么走轮询（如全池突破扫描 get_breakout_scan），要么在此显式放宽上限（仅防后端僵死）。
const _API_TIMEOUT_OVERRIDE = { recompute_pb: 300000 };
const _API_MAX_INFLIGHT = 6;
let _apiInflight = 0;
let _apiTimeoutCnt = 0;
let _lastReportAt = 0;      // 上报节流（见 apiCall 末尾注释）

async function apiCall(name, ...args) {
  if (!window.pywebview || !window.pywebview.api) {
    throw new Error("未运行在 pywebview 环境（请用 python t_gui.py 启动）");
  }
  // pywebview 的 `window.pywebview.api` 先建成**空对象**（api.js），稍后由 finish.js 挂上方法。
  // 若调用落在该窗口内 ⇒ 报 "window.pywebview.api[name] is not a function"（桥接就绪竞态）。
  // 此处等到方法真正可调用再发起（最多 4s），避免首帧误报「无法连接后端」。
  if (typeof window.pywebview.api[name] !== "function") {
    const _deadline = Date.now() + 4000;
    while (typeof (window.pywebview.api || {})[name] !== "function" && Date.now() < _deadline) {
      await new Promise(r => setTimeout(r, 100));
    }
    if (typeof window.pywebview.api[name] !== "function") {
      throw new Error(`后端方法 ${name} 不可用（pywebview 桥接未就绪，或后端进程未重启）`);
    }
  }
  // 等空位（最多 30s，避免无界堆积；等不到就照常放行，宁可直接超时也不要静默吞掉）
  const _qDeadline = Date.now() + 30000;
  while (_apiInflight >= _API_MAX_INFLIGHT && Date.now() < _qDeadline) {
    await new Promise(r => setTimeout(r, 40));
  }
  _apiInflight++;
  const _t0 = performance.now();
  const _cap = _API_TIMEOUT_OVERRIDE[name] || _API_TIMEOUT_MS;
  let _timedOut = false;
  try {
    return await Promise.race([
      window.pywebview.api[name](...args),
      new Promise((_, rej) => setTimeout(() => {
        _timedOut = true;
        rej(new Error(`后端 ${name} 超时 ${_cap}ms（投递层积压，该次结果已丢弃）`));
      }, _cap)),
    ]);
  } finally {
    _apiInflight--;
    const _dt = performance.now() - _t0;
    // ⚠️ 上报本身是个 js_api 调用（会新起线程 + 占用 UI 线程返回通道）。冻结时"每次调用都慢"
    // ⇒ 每次都再触发一次上报 = **把队列翻倍**。故全局节流：2s 内至多上报一次。
    // ⚠️ 注意写法：**绝不能在 finally 里 `return`** —— JS 中 finally 的 return 会**覆盖**
    // try 的返回值，那样几乎每次 apiCall 都会返回 undefined、整个界面作废。故用 if 包住。
    const _now = Date.now();
    if (_now - _lastReportAt >= 2000) {
      _lastReportAt = _now;
      if (_timedOut) {
        _apiTimeoutCnt++;
        // 超时单独记一笔（这类调用不会走慢调用上报：挂住的调用连 finally 都跑不到，日志里是隐形的）
        try { window.pywebview.api.report_client_diag("api_timeout", _dt, String(name)); } catch (e) { }
      } else if (_dt >= 800 && name !== "report_slow_call") {
        try { window.pywebview.api.report_slow_call(name, Math.round(_dt)); } catch (e) { /* 静默 */ }
      }
    }
  }
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
    // 账户详细信息（统一源头）
    state.accountsDetail = (payload.accounts_detail) || {};
    renderAll(payload);

    // K1 KPI（manual 做T 已于 2026-09-14 下线）；2026-10-10 KPI 区块整体删除，不再渲染
    // 行情条 + 大盘趋势（静态，一次拉取）
    // 持仓日线体检（超买/顶背离）
    loadOBAnalysis();
    loadMarketBeacon();          // 🧭 市场风向标（独立 section，自带调度）
    apiCall("load_quotes").then(q => updateSidebarSummary((q && q.quotes) || [])).catch(() => {});
    apiCall("load_position_manager").then(pm => renderPositionManager(pm || {})).catch(() => {});
    apiCall("load_market_score").then(ms => renderMarket(ms || {})).catch(() => {});
    loadAutoPnl();
    refreshAccountLabels();   // 全站账户列表（历史成交为源）：一次拉取，导入后刷新

    // 今日则进入实时模式
    const isToday = date === todayStr();
    // 指数板只在今日展示（避免看历史日期时混入实时行情）；背离条是全局告警，两种情形都刷新
    const _ibw = document.getElementById("indexBoardWrap");
    if (_ibw) _ibw.style.display = isToday ? "" : "none";
    if (isToday) {
      await refreshLive(true);       // 立即拉一次 live + console（内含 refreshIndexBoard）
      startLivePoll();               // 10s 实时
      startSignalPoll();             // 5s 信号检测 + 报警
      ensureAudio();                 // 尝试解锁音频
    } else {
      stopLivePoll();
      stopSignalPoll();
      renderLive(null, false);       // 显示"非今日"
      refreshIndexDivergence();      // 全局背离条：非今日视图也反映实时告警
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

// 桥接就绪判定（2026-10-10）：pywebview 先建 `api: {}` 空对象再挂方法，
// 仅判 `window.pywebview.api` 真值会在「空对象窗口」内误判为已就绪 ⇒ 改判**具体方法**是否已挂上。
function _apiReady() {
  return !!(window.pywebview && window.pywebview.api
    && typeof window.pywebview.api.available_dates === "function");
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
    // 指数板 + 背离角标（2026-09-28）：随后端 10s live 轮询一起刷新
    refreshIndexBoard();

    // 实时行情（10s 刷新）：更新侧栏汇总 + 存 quotes 供 PB 价格更新
    apiCall("load_quotes").then(q => {
      updateSidebarSummary((q && q.quotes) || []);
      state.quotes = (q && q.quotes) || [];
      if (state.payload) {
        // PB 表价格/涨幅实时更新（2026-10-12 owner：「建仓信号扫描没有显示个股实时涨幅」。
        // 根因=此处原先**只搬了 price、把 change_pct 丢了**，而 scan trace 里本就没有涨跌幅
        // ⇒ 不叠加就永远没有。现已收进 `applyQuotesToPB`，与 refresh_pb 路径共用。）
        const pbRows = state.payload.position_builder;
        if (pbRows && pbRows.rows) {
          renderPB(applyQuotesToPB(pbRows));
        }
      }
      // 持仓体检表「现价」列同款：OB 表 45~60s 才整体重拉，现价用 10s quotes 就地叠加
      applyQuotesToOB();
      // 风向标「价/涨幅」列：面板就绪后自身轮询降到 60s，挂在 10s live 轮询上才能 ~15s 刷新
      // （内部有 tab 可见性 + 15s TTL + 单飞闸，不在该页/在飞中都是零成本）
      loadBeaconQuotes();
      // 猎手展开页的实时价/涨幅：只刷**当前可见**的展开板块（TTL 15s 节流；
      // 猎手页没打开/没展开任何板块时是零成本 —— 别为看不见的东西出网）
      if (hunterOpenSectors.size && _hunterTabActive()) {
        loadHunterQuotes();          // 一次覆盖全部展开板块（不再每板块一次）
      }
    }).catch(() => {});
    // 2026-10-10: 「今日盘中 S」已下线，连带的 10s 重拉一并删除（跨日 S 是日频，改日期时拉一次即可）


    // 建仓/加仓扫描表实时刷新（position_builder 盘中每5分钟有新数据）
    // fix P1-6: refresh_pb 成败纳入轮询告警计数
    apiCall("refresh_pb", date).then(pb => {
      pbFailCnt = 0;
      // 关键：把带技术标签的结果合并回 state.payload，否则后续 load_quotes 路径
      // 用无 tags 的旧 payload 重新 renderPB，会覆盖掉已显示的技术标签（一会有一会无）
      if (pb && pb.rows && state.payload) state.payload.position_builder = pb;
      renderPB(applyQuotesToPB(pb || {}));   // 叠加实时价/涨幅，别让新 payload 把它洗掉
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

/* ================= 🧭 市场风向标（人气板块 × 龙头/中军） ================= */
// ⚠️ **描述性看盘，无前瞻信息量** —— 依据见后端 `t_gui.load_market_beacon` 的 docstring：
//    实测「龙头/中军」对后续收益**无区分度**（977 池 × 2009 日、行业口径、同板块同日配对、
//    按日聚类 t、分半窗符号翻转；涨幅头名 vs 杂毛 H=5 仅 −0.021pp t=−0.40，成交额头名反而
//    −0.302pp t=−5.7）。本面板只回答「今天钱在哪个板块、谁在场」，**不是买卖依据**。
//
// 列沿用「建仓信号扫描」那套（判定/得分/通过/日线阶段/20日线观察/价涨幅/技术标签/时机条件/
// 30min判定/仓位结构/30m MA5/依据），另加「梯队」。**刻意不含「池」与「扫描」两列**：
// 本面板不是扫描产物 —— 没有池归属、也没有 scan_time（后端返回里就没有这两个字段）。
// date：缓存归属日期。风向标必须跟着**全局选中日**走（与选股猎手同一天），
// 否则猎手跑历史日、风向标却总挑"≤今天的最新一天" ⇒ 两边排名对不上（2026-10-18 owner 报）。
let _beacon = { ts: 0, data: null, inflight: false, date: null };
// 风向标的"日期跟随"（2026-10-18）：
//   · 默认跟随全局选中日 `state.date`（与「选股猎手」主按钮的运行日期同源）；
//   · 猎手「重新生成」看历史日时，用 dateOverride **临时**把风向标钉到同一天；
//   · 全局日期一变，覆盖立即作废，回到 state.date。
let _beaconDateOverride = null;
let _beaconGlobalDate = null;
const _beaconOpen = new Set();   // 已展开的板块**名称** —— 必须跨重渲染保持（未齐时每 3s 重渲染）；
                                 // 2026-10-18 由下标改名称：猎手重跑后排名变化，按下标会让展开的板块串位。
const BEACON_TTL_MS = 60000;      // 后端一次 ~5s（88 只 × 日线缓存 + 标签批算）⇒ 不能跟 10s 轮询同频
const BEACON_TTL_PENDING_MS = 3000;  // 未齐（重列/标签还在补）时短轮询，见 _beaconReady
let _beaconQ = { cache: {}, last: 0, inflight: false };
const BEACON_Q_TTL_MS = 15000;

/* 面板是否"已经齐了"：重列算完 **且** 每行都拿到标签、30min 判定与结构层都不在 pending。
   没齐就短轮询 —— 后端是 SWR（首帧只给轻列），实测 t+8s 出判定、t+12s 出标签。
   ⚠️ 别用固定 60s TTL：那会让首帧的空壳**钉住整整一分钟**（本仓"标签一会儿有一会儿无"同源）。
   2026-10-13：纳入 m30_verdict/structure —— 原来只看 tags，标签一到就掉 60s 轮询，
   「30min 判定/仓位结构」列要在预热完成后多等至多 60s 才显示。 */
function _beaconReady(d) {
  if (!d || d.available === false) return true;
  if (d.pending || d.enrich_pending) return false;
  const rows = (d.sectors || []).reduce((a, s) => a.concat(s.rows || []), []);
  return rows.length > 0 && rows.every(r => (r.tags || []).length > 0
    && r.m30_verdict && r.m30_verdict.level && r.m30_verdict.level !== "pending"
    && r.structure && !r.structure.pending);
}

/* 自带调度：本面板**不依赖任何轮询去驱动**（原来长在 #liveBody 里，被 10s 重渲染拖着走）。
   齐了就 60s 一次，没齐 3s 一次。 */
let _beaconTimer = null;
function _scheduleBeacon() {
  if (_beaconTimer) clearTimeout(_beaconTimer);
  const ttl = _beaconReady(_beacon.data) ? BEACON_TTL_MS : BEACON_TTL_PENDING_MS;
  _beaconTimer = setTimeout(() => loadMarketBeacon(true), ttl);
}

async function loadMarketBeacon(force, dateOverride) {
  const el = document.getElementById("beaconBody");
  if (!el) return;
  if (chartModalOpen) {                    // 弹窗打开：10s 后再试，别和 K 线抢 UI 线程
    if (_beaconTimer) clearTimeout(_beaconTimer);
    _beaconTimer = setTimeout(() => loadMarketBeacon(true), 10000);
    return;
  }
  const now = Date.now();
  // 全局日期一变 ⇒ 历史视图的日期覆盖作废（见 _beaconDateOverride 注释）
  if ((state.date || null) !== _beaconGlobalDate) {
    _beaconGlobalDate = state.date || null;
    _beaconDateOverride = null;
  }
  if (dateOverride !== undefined) _beaconDateOverride = dateOverride || null;
  const _curDate = _beaconDateOverride || state.date || null;
  const ttl = _beaconReady(_beacon.data) ? BEACON_TTL_MS : BEACON_TTL_PENDING_MS;
  if (!force && _beacon.data && _beacon.date === _curDate && (now - _beacon.ts) < ttl) {
    renderMarketBeacon(_beacon.data);         // TTL 内复用（会走"没变就不重建"的闸）
    _scheduleBeacon();
    return;
  }
  if (_beacon.inflight) {
    // 正在飞的那次可能是**另一个日期**（切日/猎手历史日）⇒ 完成后尽快补一次，
    // 别等 finally 里按 60s TTL 排下一轮。
    if (_beacon.date !== _curDate) {
      if (_beaconTimer) clearTimeout(_beaconTimer);
      _beaconTimer = setTimeout(() => loadMarketBeacon(true), 800);
    }
    return;
  }
  _beacon.inflight = true;
  try {
    // 传全局选中日 ⇒ 与「选股猎手」同一天（后端取 ≤date 的最近一日）
    const d = await apiCall("load_market_beacon", _curDate);
    _beacon = { ts: Date.now(), data: d || {}, inflight: false, date: _curDate };
    renderMarketBeacon(d || {});
  } catch (e) {
    _beacon.inflight = false;
    // ⚠️ 这里原来写的是 `if (!_beacon.data)` —— 而 data 在 renderMarketBeacon **之前**就赋了值
    //    ⇒ 一旦渲染抛异常，异常被吞、界面永远停在"加载中…"、**一条线索都不留**。
    //    改成：没数据报错，有数据但渲染又炸则单独标出来（两种都要看得见）。
    const why = (e && e.message) ? e.message : String(e);
    el.innerHTML = `<div class="empty">${_beacon.data ? "风向标渲染失败" : "风向标加载失败"}：${esc(why)}</div>`;
    console.error("[风向标]", e);
  } finally {
    _scheduleBeacon();
  }
}

function beaconRowHtml(s, labels) {
  const _sc = (s.build_score != null) ? s.build_score : null;
  const _ceil = (s.build_ceiling != null) ? s.build_ceiling : 100;
  const _reach = (s.build_reachable != null) ? s.build_reachable : true;
  const scoreTxt = _sc == null ? '<span class="cell-dim">—</span>'
    : (_reach ? `<b>${_sc}</b>` : `<b>${_sc}</b><span class="cell-dim" style="font-size:10px">/${_ceil}🔒</span>`);
  const _met = (s.build_met != null) ? `${s.build_met}/3` : "—";
  const tagsTxt = (s.tags && s.tags.length)
    ? s.tags.map(t => tagBadge(t)).join(" ")
    : '<span class="cell-dim" style="font-size:10px" title="技术标签后台批算，约 10~20s 自填">⏳ 标签</span>';
  const _mv = s.m30_verdict || {};
  const _MVCLS = { high: "badge signal", watch: "badge approach", bull: "badge bear" };
  const m30Txt = !_mv.level || _mv.level === "pending"
    ? '<span class="cell-dim" title="30min 判定后台计算中">⏳</span>'
    : `<span class="${_MVCLS[_mv.level] || "cell-dim"}" title="${esc(_mv.reason || "")}">${esc(_mv.label || _mv.level)}</span>`;
  const _rowBg = tierRowStyle(s.tier)
    || ((_sc === 0) ? "background:rgba(248,81,73,.12);"
      : (_sc !== null && _sc > 0) ? "background:rgba(63,185,80,.07);" : "");
  return `<tr class="h-expand-row" style="${_rowBg}" ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')">
    <td>${esc(s.name)} <span class="mono cell-dim">${esc(s.code)}</span></td>
    <td style="text-align:center">${tierBadge(s.tier, s.tier_static, s.tier_reason)}</td>
    <td>${verdictBadge(s.build_verdict)}</td>
    <td class="num">${scoreTxt}</td>
    <td class="num">${_met}</td>
    ${dayStageCellHtml(s.structure)}
    ${ma20FlagCellHtml(s.build_above_ma20)}
    <td class="num h-price" data-hcode="${esc(s.code)}" title="实时价/涨幅（10s 刷新）；未取到显示 ⏳ 而不是 0.00%">${hunterPriceHtml(null)}</td>
    <td class="h-tags" style="max-width:200px;line-height:1.7">${tagsTxt}</td>
    ${condCellHtml(s.build_conds, labels, s.build_verdict === "insufficient_data", s.build_veto)}
    <td style="text-align:center" title="${esc(_mv.reason || "")}">${m30Txt}</td>
    ${structCellHtml(s.structure)}
    ${ma5CellHtml(s.structure)}
    <td class="cell-dim" style="max-width:240px;font-size:11px">${esc(s.build_reason || "—")}</td>
  </tr>`;
}

let _beaconEl = null, _beaconSig = null;
function renderMarketBeacon(d) {
  const el = document.getElementById("beaconBody");
  if (!el) return;
  // ⚠️ **没变就不重建**：3s 轮询下每次都重建 23 张卡 + 332 行表 ⇒ 即使折叠着也看得见跳闪。
  //    容器换过（页面上别处重渲染过）则必须重建 —— 所以签名要和**元素身份**一起判。
  // 2026-10-18：签名必须带**有序板块列表**。原来只有 `JSON.stringify(d).length` —— 猎手重跑后
  // 名次/均分变了但 JSON 长度常常一模一样 ⇒ 这里早返回、风向标停在旧排名（owner 报"排名没有对齐"）。
  const _ord = ((d && d.sectors) || [])
    .map(s => `${s.name}:${s.avg}:${s.limitup}:${(s.rows || []).length}`).join(",");
  const sig = [d && d.date, d && d.pending ? "P" : "", d && d.enrich_pending ? "E" : "",
               ((d && d.sectors) || []).length, JSON.stringify(d || {}).length, _ord].join("|");
  if (el === _beaconEl && sig === _beaconSig) return;
  _beaconEl = el; _beaconSig = sig;
  if (!d || d.available === false) {
    el.innerHTML = `<div class="empty">${esc((d && d.error) || "无数据")}</div>`;
    return;
  }
  if (d.pending) {                     // 骨架还没算完（后台，实测 ~6s）
    el.innerHTML = '<div class="empty">板块骨架计算中…（全部板块要读全池日线缓存，约 6 秒；算完自动填）</div>';
    return;
  }
  const secs = d.sectors || [];
  if (!secs.length) { el.innerHTML = '<div class="empty">当日无「有分层」的人气板块</div>'; return; }
  const total = secs.reduce((a, s) => a + (s.rows || []).length, 0);
  const head = `<thead><tr>
    <th>股票</th><th>梯队</th><th>判定</th><th class="num">得分</th><th class="num">通过</th>
    <th>日线阶段</th><th title="是否站上 20 日线（观察列，不驱动判定）。⚠️ 得分里的 −70 扣分走的是 5 日线口径，与本列无关 ⇒ 本列不显示 −70">20日线<br><span style="font-size:9px;opacity:.6">观察</span></th><th class="num">价<br><span style="font-size:9px;opacity:.6">涨幅</span></th>
    <th>技术标签</th><th>时机条件</th><th>30min判定</th><th>仓位结构</th><th>30m MA5<br><span style="font-size:9px;opacity:.6">仅展示</span></th><th>依据</th>
  </tr></thead>`;
  // 板块名自带信息：龙头名字直接写在标题行 ⇒ **不展开也能一眼看到"谁在场"**
  // 2026-10-18 owner：「市场风向标使用方格显示」—— 每个板块一张**方格卡**（grid），
  // 点格子展开成整行（grid-column:1/-1）显示明细表；展开状态仍由 `_beaconOpen` 跨重绘保持。
  const _dayTxt = d.date && String(d.date).length >= 8
    ? `${String(d.date).slice(0, 4)}-${String(d.date).slice(4, 6)}-${String(d.date).slice(6, 8)}`
    : (d.date || "—");
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:8px;margin:2px 0 8px;flex-wrap:wrap">
      <button class="mini-btn" onclick="beaconToggleAll(true)">全部展开</button>
      <button class="mini-btn" onclick="beaconToggleAll(false)">全部收起</button>
      <span class="cell-dim">数据日 <b>${esc(_dayTxt)}</b>（与选股猎手同口径）</span>
      <span class="cell-dim">共 <b>${secs.length}</b> 个板块 · <b>${total}</b> 只代表股（默认收起，点方格展开）</span>
      <span class="cell-dim" style="font-size:11px">
        <span style="color:#f85149">■</span> 前 10 名
        <span style="color:#2ea043;margin-left:6px">■</span> 其余
      </span>
    </div>
    <div class="beacon-grid">` + secs.map((sec, i) => {
    const leads = (sec.rows || []).filter(r => r.tier === "龙头").map(r => r.name);
    const mids = (sec.rows || []).filter(r => r.tier === "中军").length;
    // 展开状态按**板块名**记（不是下标）：猎手重跑后排名会变，按下标记会让"展开的板块"跳到别人身上。
    const _open = _beaconOpen.has(sec.name);
    // 2026-10-18 owner：前 10 名红色、其余绿色（列表是后端按板块均分降序 ⇒ i<10 即前10名）
    const _rankCls = i < 10 ? " beacon-top" : " beacon-rest";
    return `<div class="card beacon-tile${_rankCls}${_open ? " open" : ""}" id="beaconCard_${i}" data-name="${esc(sec.name)}">
      <div class="beacon-tile-head" onclick="beaconToggle(${i})" title="点击展开/收起该板块明细">
        <div class="beacon-line1">
          <span class="bk-arrow" id="beaconArrow_${i}">${_open ? "▾" : "▸"}</span>
          <span class="beacon-rank" title="板块排名（与选股猎手同一天、同一均分口径）">${i === 0 ? "🥇" : i === 1 ? "🥈" : i === 2 ? "🥉" : i + 1}</span>
          <b class="beacon-name">${esc(sec.name)}</b>
          <span class="beacon-score" title="板块均分（与选股猎手同口径）">${fmt(sec.avg, 2)}</span>
        </div>
        <div class="beacon-meta">
          涨停 <b>${fmt(sec.limitup, 0)}</b> · 成分 ${fmt(sec.members, 0)} · 代表 ${sec.rows.length}${mids ? ` · 中军 ${mids}` : ""}
        </div>
        <div class="beacon-leads">
          ${leads.length ? `龙头 <b>${esc(leads.join("/"))}</b>` : '<span title="该板块当日无最强一档（不硬凑）">无龙头</span>'}
        </div>
      </div>
      <div id="beaconSec_${i}" class="beacon-detail" style="display:${_open ? "" : "none"}">
        <div style="overflow-x:auto"><table class="h-table">${head}<tbody>${sec.rows.map(s => beaconRowHtml(s, d.cond_labels)).join("")}</tbody></table></div>
      </div>
    </div>`;
  }).join("") + `</div>`;
  loadBeaconQuotes();
}

/* 板块折叠：默认**全部收起** —— 全部板块有 20+ 个、300+ 行，默认展开就是一堵墙。 */
function beaconToggle(i, force) {
  const box = document.getElementById(`beaconSec_${i}`);
  const arw = document.getElementById(`beaconArrow_${i}`);
  const card = document.getElementById(`beaconCard_${i}`);
  if (!box) return;
  const show = (force === undefined) ? (box.style.display === "none") : !!force;
  box.style.display = show ? "" : "none";
  if (arw) arw.textContent = show ? "▾" : "▸";
  // 方格布局：展开的板块跨满整行（grid-column:1/-1），否则 300+ 行的明细表塞不进方格
  if (card) card.classList.toggle("open", show);
  // 展开状态按**板块名**记（排名变化后不会串到别的板块）
  const name = card ? card.getAttribute("data-name") : null;
  if (name) { if (show) _beaconOpen.add(name); else _beaconOpen.delete(name); }
}

function beaconToggleAll(open) {
  document.querySelectorAll("#beaconBody [id^='beaconSec_']").forEach(b => {
    beaconToggle(b.id.replace("beaconSec_", ""), open);
  });
}

/* 实时价：与猎手同款（单飞 + TTL + **非空才认**，避免冷启动回空把 TTL 刷掉导致整列干等）。 */
async function loadBeaconQuotes(force) {
  if (_beaconQ.inflight) return;
  const now = Date.now();
  if (!force && now - _beaconQ.last < BEACON_Q_TTL_MS) { _applyBeaconQuotes(); return; }
  if (!_tabActive("tab-signals")) return;     // 看不见就不出网（⚠️ 风向标在 tab-signals 里；
                                              // 原来写成 "tab-live"——**根本不存在这个页面 id**，
                                              // 恒 false ⇒ 价/涨幅列永远 ⏳，2026-10-13 修）
  const tds = document.querySelectorAll("#beaconBody td.h-price[data-hcode]");
  if (!tds.length) return;
  const codes = Array.from(tds).map(td => td.getAttribute("data-hcode"));
  _beaconQ.inflight = true;
  try {
    const r = await apiCall("load_quotes_for", codes);
    const qm = (r && r.quotes) || {};
    if (Object.keys(qm).length) { Object.assign(_beaconQ.cache, qm); _beaconQ.last = Date.now(); }
    _applyBeaconQuotes();
  } catch (e) { /* 静默：拿不到就保持 ⏳，不刷成 — */ }
  finally { _beaconQ.inflight = false; }
}

function _applyBeaconQuotes() {
  document.querySelectorAll("#beaconBody td.h-price[data-hcode]").forEach(td => {
    const q = _beaconQ.cache[td.getAttribute("data-hcode")];
    if (q) td.innerHTML = hunterPriceHtml(q);
  });
}

function _tabActive(id) {
  const p = document.getElementById(id);
  return !!(p && p.classList && p.classList.contains("active"));
}

/* ================= 各区块渲染 ================= */
function renderAll(p, tradePnl) {
  // 2026-10-10: KPI 日快照 / 各股信号数 / K4 胜率 区块已按 owner 需求删除。
  // 2026-10-10: 「shadow 近阈 · 仓控与推送」整块按 owner 需求删除（renderShadow 已移除）。
  // 2026-08-23: 持仓对照/盘中结算/系统看板已从"每日复盘"tab移除
  renderPB(p.position_builder);
  // P4-3: 概览页自动盘持仓卡片 + 全页 KILL_SWITCH 横幅
  loadAutoStatus();
}

/* ---- ECharts 辅助 ---- */
const echarts = window.echarts;
const echInstances = {};
function echRender(id, opt) {
  if (!echarts) return;
  const el = document.getElementById(id);
  if (!el) return;
  let inst = echInstances[id];
  // ⚠️ 2026-10-13：容器被 `innerHTML` 重建后，缓存实例仍绑在**已脱离文档的旧节点**上；
  //   再 setOption 只会画进那个看不见的节点 ⇒ 界面留白。
  //   实测症状：交割单台账「逐股盈亏」图**先显示再消失** —— `renderTradeHistory` 每次都
  //   先 `thBars.innerHTML = ""` 重建容器（598 行），而容器 id 是按下标生成的
  //   (`echThBar_0/1`)，第二次渲染 id 不变 ⇒ 命中陈旧实例。
  //   修法：实例的 DOM 与当前节点不一致 ⇒ 弃掉重建（不 dispose 会同时泄漏旧的 canvas）。
  if (inst && typeof inst.getDom === "function" && inst.getDom() !== el) {
    try { inst.dispose(); } catch (e) { /* 已销毁，忽略 */ }
    inst = null;
    delete echInstances[id];
  }
  if (!inst) {
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
// 单张图渲染失败不能连累同页其它图。
// 2026-10-10 事故：renderTradeHistory 里一个变量声明晚于使用（TDZ ReferenceError），
// 函数在中间就抛了，后面三张图**静默留白**、界面上没有任何提示，排查花了一轮来回。
// 用 echGuard 把每张图各自包起来：出错在控制台点名是哪张，其余照画。
function echGuard(name, fn) {
  try { fn(); }
  catch (e) { console.error(`[图表] ${name} 渲染失败：`, e); }
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
  // 年限宽度图 / 站上20线占比图 是独立实例（不在 echInstances 里）
  if (ma365Inst) { try { ma365Inst.resize(); } catch (e) { } }
  if (ma20Inst) { try { ma20Inst.resize(); } catch (e) { } }
}
window.addEventListener("resize", () => {
  Object.values(echInstances).forEach(i => i.resize());
});
// 2026-10-18：窗口恢复/切回页面时，零尺寸的图要自己重画（不能只等下一个 10s 轮询）。
let _turnoverResizeTimer = null;
window.addEventListener("resize", () => {
  if (_turnoverResizeTimer) clearTimeout(_turnoverResizeTimer);
  _turnoverResizeTimer = setTimeout(() => {
    _turnoverResizeTimer = null;
    _turnoverSizeRetryN = 0;
    drawTurnoverHist();
  }, 350);
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    _turnoverSizeRetryN = 0;
    setTimeout(drawTurnoverHist, 150);
  }
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
  if (!ms || !ms.history || !ms.history.length) {
    tabClear("echMarket");
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
          lineStyle: { color: "#ff8c5a", width: 2 }, itemStyle: { color: p => {
            const r = hist[p.dataIndex].regime;
            return r === "uni_up" ? "#f85149" : r === "uni_down" ? "#3fb950" : r === "range" ? "#d29922" : "#ff8c5a";
          }} },
        { name: "Sadj", type: "line", data: hist.map(h => h.sadj != null ? h.sadj : null),
          lineStyle: { color: "rgba(139,148,158,.5)", width: 1, type: "dashed" }, symbol: "none" },
      ],
    });
  } else tabClear("echMarket");
}

/* ---- 历史成交盈亏（2026-10-10 需求3）：券商「历史成交」→ 逐股已实现盈亏 · 做T收益 ----
   全站账户列表的唯一来源就是这份台账；改完要连带刷新账户相关的各面板。 */
async function loadTradeHistory() {
  try {
    const d = await apiCall("load_trade_history");
    renderTradeHistory(d || {});
  } catch (e) { /* 桥接未就绪时忽略 */ }
}

async function importTradeHistory() {
  const meta = document.getElementById("thMeta");
  if (meta) meta.textContent = "解析中…（多份大文件约需数秒）";
  try {
    const d = await apiCall("import_trade_history");
    if (!d || !d.imported) {
      if (d && d.cancelled) { loadTradeHistory(); return; }
      if (meta) meta.innerHTML = `<span class="down">导入失败：${esc((d && d.error) || "未知错误")}</span>`;
      return;
    }
    renderTradeHistory(d);
    refreshAccountLabels();   // 账户名以新台账为准，持仓表等要跟着更新
  } catch (e) {
    if (meta) meta.innerHTML = `<span class="down">导入失败：${esc(e.message)}</span>`;
  }
}

// 全站账户列表（历史成交为源）——启动拉一次，导入后刷新
async function refreshAccountLabels() {
  try {
    const d = await apiCall("load_accounts");
    state.accountList = (d && d.accounts) || [];
  } catch (e) { /* 忽略 */ }
}

/* 按资金账号切一份台账视图：stocks 直接筛；monthly/t_monthly 筛完按月合并；
   cum 是账户内累计值，跨账户同日相加即为合并账户的累计。 */
function _jgdSlice(d, acc) {
  const keep = r => !acc || (r.account || "") === acc;
  const byMonth = rows => {
    const m = {};
    rows.filter(keep).forEach(r => {
      const o = m[r.month] || (m[r.month] = {
        month: r.month, realized: 0, fees: 0, n_trades: 0, n_pairs: 0, n_days: 0 });
      o.realized += r.realized || 0;
      o.fees += r.fees || 0;
      o.n_trades += r.n_trades || 0;
      o.n_pairs += r.n_pairs || 0;
      o.n_days += r.n_days || 0;
      o.total_pnl = (o.total_pnl || 0) + (r.total_pnl || 0);
    });
    return Object.keys(m).sort().map(k => m[k]);
  };
  // ⚠️ d.cum 的每一行**已经是该账户的累计值**（后端按账户各自累计），这里只能筛选+排序，
  // 绝不能再累加一遍——否则单账户视图会二次累计（380→622 会画成 380→1002）。
  const cum = (d.cum || []).filter(keep)
    .slice().sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  const stocks = (d.stocks || []).filter(keep);
  return {
    stocks,
    monthly: byMonth(d.monthly || []),
    tMonthly: byMonth(d.t_monthly || []),
    cum,
    totalRealized: stocks.reduce((a, s) => a + (s.realized || 0), 0),
    totalUnreal: stocks.reduce((a, s) => a + (s.unrealized || 0), 0),
    totalPnl: stocks.reduce((a, s) => a + (s.total_pnl || 0), 0),
    totalFees: stocks.reduce((a, s) => a + (s.fees || 0), 0),
    totalT: (d.t_stocks || []).filter(keep).reduce((a, s) => a + (s.pnl || 0), 0),
    totalTNet: (d.t_stocks || []).filter(keep).reduce((a, s) => a + (s.pnl || 0), 0)
               - stocks.reduce((a, s) => a + (s.fees || 0), 0),
    tByStock: (d.t_stocks || []).filter(keep),
  };
}

function renderTradeHistory(d) {
  const meta = document.getElementById("thMeta");
  const body = document.getElementById("thBody");
  if (!meta && !body) return;
  const sel = document.getElementById("thAccount");
  if (!d || !d.imported) {
    if (meta) meta.textContent = "尚未导入历史成交 —— 点右上「📥 导入历史成交」选择券商导出的 xls/xlsx（可多选，多期/多账户会自动合并去重）";
    ["echThMonth", "echThFee", "echThT", "echThTNet", "echThCum", "echThCumPct"].forEach(echClear);
    ["echThFeeTotal", "echThTTotal", "echThTNetTotal", "echThCumPctTotal"].forEach(id => {
      const el = document.getElementById(id); if (el) el.textContent = "";
    });
    if (sel) { sel.style.display = "none"; sel.innerHTML = ""; }
    const bb = document.getElementById("thBars"); if (bb) bb.innerHTML = "";
    const ab = document.getElementById("thAccounts");
    if (ab) ab.innerHTML = "";
    if (body) body.innerHTML = "";
    return;
  }
  state.tradeHistory = d;
  // 账户下拉：只有多账户时才出现（单账户/PDF 交割单没有 account 字段）
  const accs = (d.accounts || []).map(a => a.account || "").filter(Boolean);
  if (sel) {
    if (accs.length > 1) {
      if (sel.dataset.built !== accs.join("|")) {
        sel.innerHTML = `<option value="">全部账户（${accs.length}）</option>`
          + accs.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join("");
        sel.dataset.built = accs.join("|");
      }
      sel.style.display = "";
    } else {
      sel.style.display = "none";
    }
  }
  const acc = sel && sel.style.display !== "none" ? sel.value : "";
  const v = _jgdSlice(d, acc);
  const r = d.range || {};
  const f = (x) => (x >= 0 ? "+" : "") + fmt(x, 0);
  if (meta) {
    const nsrc = (d.sources || []).length;
    meta.innerHTML = `来源 <span class="mono">${esc(d.source || "—")}</span>`
      + (nsrc > 1 ? ` <span class="badge" style="background:#2a4a5a;color:#8ad4ff">${nsrc} 份合并</span>` : "")
      + (d.dup_dropped ? ` <span class="cell-dim">（重叠去重 ${d.dup_dropped} 笔）</span>` : "")
      + (acc ? ` · 账户 <b>${esc(acc)}</b>` : "")
      + ` · ${esc(r.start || "")} ~ ${esc(r.end || "")} · ${r.n_trades || 0} 笔 · `
      + `已实现 <span class="${clsOf(v.totalRealized)}"><b>${f(v.totalRealized)}</b></span> · `
      + `浮动 <span class="${clsOf(v.totalUnreal)}">${f(v.totalUnreal)}</span> · `
      + `合计 <b class="${clsOf(v.totalPnl)}">${f(v.totalPnl)}</b> · `
      + `做T收益 <span class="${clsOf(v.totalT)}"><b>${f(v.totalT)}</b></span> · `
      + `做T净额(扣费) <b class="${clsOf(v.totalTNet)}">${f(v.totalTNet)}</b>`
      + (v.totalFees ? ` · 交易费用 <b style="color:#d29922">${fmt(v.totalFees, 0)}</b>` : "")
      + (acc ? "" : (d.total_misc ? ` · 其他收支 <span class="${clsOf(d.total_misc)}">${f(d.total_misc)}</span>` : ""))
      + (d.imported_at ? ` · 导入于 ${esc(d.imported_at)}` : "")
      + (d.data_through
          ? `<br>已记忆到 <b>${esc(d.data_through)}</b> —— 下次请导入该日**之后**的历史成交`
            + (d.prev_data_through && d.advanced === false
                ? ` <span class="badge" style="background:#5a3a2a;color:#ffb454">本次未含新交易日（上次已到 ${esc(d.prev_data_through)}）</span>`
                : (d.advanced && d.prev_data_through
                    ? ` <span class="badge" style="background:#1e3a2a;color:#3fb950">已新增至 ${esc(d.data_through)}</span>`
                    : ""))
          : "");
  }
  // 0) 各账户汇总（含盈亏百分比）——始终展示全部账户，选中的高亮
  const accBox = document.getElementById("thAccounts");
  if (accBox) {
    const accts = d.accounts || [];
    if (!accts.length) {
      accBox.innerHTML = "";
    } else {
      const pctOf = a => (a.pnl_pct >= 0 ? "+" : "") + fmt(a.pnl_pct, 2) + "%";
      const rows = accts.map(a => {
        const on = acc && a.account === acc;
        return `<tr${on ? ' style="background:rgba(138,212,255,.08)"' : ""}>
          <td>${esc(a.account || "（未标注账户）")}</td>
          <td class="num ${clsOf(a.realized)}">${f(a.realized)}</td>
          <td class="num ${clsOf(a.unrealized || 0)}">${a.unrealized ? f(a.unrealized) : "—"}</td>
          <td class="num ${clsOf(a.total_pnl || 0)}"><b>${f(a.total_pnl || 0)}</b> <span class="cell-dim" style="font-size:10px">(${pctOf({pnl_pct: a.total_pct})})</span></td>
          <td class="num ${clsOf(a.t_pnl || 0)}">${a.t_pnl ? f(a.t_pnl) : "—"}</td>
          <td class="num cell-dim">${fmt(a.fees || 0, 0)}</td>
          <td class="num cell-dim">${fmt(a.buy_amt || 0, 0)}</td>
          <td class="num">${a.n_trades || 0}</td>
          <td class="num">${a.n_stocks || 0}</td>
          <td class="num cell-dim">${a.open_qty ? fmt(a.open_qty, 0) + "（成本 " + fmt(a.open_cost || 0, 0) + "）" : "—"}</td>
        </tr>`;
      }).join("");
      accBox.innerHTML = `<div class="card-title">各账户汇总（口径同券商：合计＝已实现＋浮动；收益率＝合计 ÷ 累计买入成本）</div>
        <table><thead><tr><th>账户</th><th class="num">已实现</th><th class="num">浮动</th>
        <th class="num">合计（收益率）</th>
        <th class="num">做T收益</th><th class="num">交易费用</th><th class="num">累计买入成本</th>
        <th class="num">笔数</th><th class="num">股票数</th><th class="num">在场股数</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
    }
  }
  const stocks = v.stocks;
  // 1) 逐股盈亏条形图 —— **每个账户一张**（不再把多账户并成一张）
  //    口径同券商：柱子画「合计=已实现+浮动」，tooltip 拆开给。同花顺的「盈亏」也是这个口径。
  const bars = document.getElementById("thBars");
  if (bars) {
    const acctsAll = (d.accounts || []);
    const groups = acctsAll.length > 1
      ? acctsAll.map(a => ({ key: a.account || "", label: a.account || "（未标注账户）", rows: a }))
      : [{ key: "", label: "", rows: acctsAll[0] || {} }];
    // 已选的账户只影响下方月度/做T等图；逐股图始终**每账户一张**，天然分开
    bars.innerHTML = groups.map((g, gi) => {
      const sub = stocks.filter(x => (x.account || "") === g.key);
      if (!sub.length) return "";
      const head = g.label
        ? `<div class="card-title">${esc(g.label)} · 逐股盈亏（合计＝已实现＋浮动）
             <span class="cell-dim" style="font-weight:normal">已实现 ${f(g.rows.realized || 0)} · 浮动 ${f(g.rows.unrealized || 0)} · 合计 <b>${f(g.rows.total_pnl || 0)}</b> (${(g.rows.total_pct || 0).toFixed(2)}%) · 做T ${f(g.rows.t_pnl || 0)} · 在场 ${fmt(g.rows.open_qty || 0, 0)} 股</span></div>`
        : `<div class="card-title">逐股盈亏（合计＝已实现＋浮动）</div>`;
      const h = Math.max(240, 22 * sub.length + 40);
      return `<div class="card" style="margin-bottom:12px">${head}<div id="echThBar_${gi}" class="echart" style="height:${h}px"></div></div>`;
    }).join("");
    groups.forEach((g, gi) => echGuard(`${g.label || "逐股"} 条形图`, () => {
      const sub = stocks.filter(x => (x.account || "") === g.key);
      if (!sub.length) return;
      const asc = sub.slice().sort((a, b) => a.total_pnl - b.total_pnl);
      echRender(`echThBar_${gi}`, {
        ...ECH_BASE,
        grid: { left: 96, right: 74, top: 10, bottom: 26 },
        tooltip: { ...ECH_BASE.tooltip, trigger: "axis", axisPointer: { type: "shadow" },
          formatter: p => {
            const s = asc[p[0].dataIndex];
            return `<b>${esc(s.name)}</b> <span style="color:#8b949e">${esc(s.code)}</span>`
              + `<br>合计: <b style="color:${pnlColor(s.total_pnl)}">${f(s.total_pnl)}</b>`
              + `<br><span style="color:#8b949e">已实现 ${f(s.realized)}`
              + (s.open_qty ? ` · 浮动 ${f(s.unrealized)}（${fmt(s.open_qty, 0)} 股 @${fmt(s.last_price, 3)}）` : "（已清仓）")
              + `</span><br>买入 ${fmt(s.buy_amt, 0)} / 卖出 ${fmt(s.sell_amt, 0)} · 费 ${fmt(s.fees, 0)}`
              + `<br>成交 ${s.n_trades} 笔 · ${esc(s.first)} ~ ${esc(s.last)}`;
          } },
        xAxis: { ...ECH_BASE.xAxis, type: "value",
          axisLabel: { color: "#8b949e", fontSize: 10, formatter: x => (x >= 0 ? "+" : "") + fmt(x, 0) } },
        yAxis: { ...ECH_BASE.yAxis, type: "category", data: asc.map(s => s.name || s.code),
          axisLabel: { color: "#8b949e", fontSize: 10 } },
        series: [{ type: "bar", data: asc.map(s => ({ value: s.total_pnl,
            itemStyle: { color: pnlColor(s.total_pnl) } })),
          label: { show: true, position: "right", color: "#8b949e", fontSize: 10,
            formatter: p => (p.value >= 0 ? "+" : "") + fmt(p.value, 0) } }],
      });
    }));
  }

  // 2) 累计已实现时间曲线
  const cum = v.cum;
  if (cum.length >= 2) {
    echRender("echThCum", {
      ...ECH_BASE,
      tooltip: { ...ECH_BASE.tooltip, trigger: "axis",
        formatter: p => `<b>${cum[p[0].dataIndex].date}</b><br>累计已实现: `
          + `<b style="color:${pnlColor(cum[p[0].dataIndex].cum_realized)}">${f(cum[p[0].dataIndex].cum_realized)}</b>` },
      xAxis: { ...ECH_BASE.xAxis, type: "category", data: cum.map(x => x.date.slice(5)) },
      yAxis: { ...ECH_BASE.yAxis, axisLabel: { color: "#8b949e", fontSize: 10,
        formatter: v => (v >= 0 ? "+" : "") + fmt(v, 0) } },
      series: [{ type: "line", smooth: true, symbolSize: 4, data: cum.map(x => x.cum_realized),
        lineStyle: { color: "#8ad4ff", width: 2 }, itemStyle: { color: "#8ad4ff" },
        areaStyle: { color: "rgba(138,212,255,.12)" },
        markLine: { silent: true, symbol: "none",
          lineStyle: { color: "#8b949e", type: "dashed" }, data: [{ yAxis: 0 }] } }],
    });
  } else echClear("echThCum");

  // 2b) 累计收益趋势 —— 两账户合计（选了单账户就画那个账户）
  //
  // 【两条线分别是什么】（2026-10-14 owner 问「黄线代表什么」—— 它此前**没有图例**，见下）
  //   · 蓝实线 `累计收益率(左·%)` —— 左轴。口径：累计已实现 ÷ **累计买入成本**（与账户汇总同源）。
  //   · 黄虚线 `累计总盈亏(右·元)` —— 右轴。口径：**含浮动**的累计总盈亏（元）= 已实现 + 持仓浮盈浮亏。
  //   两条都不是同一条线的两种刻度：蓝线是"相对投入成本的比例"，黄线是"绝对金额且含未实现部分"。
  //   所以会出现"蓝线接近 0 而黄线大幅为负"—— 那是浮亏（卖出前不体现在已实现里）。
  //
  // ⚠️ **历史 bug（已于 2026-10-14 修）**：`legend.data` 里写的是 `"累计总盈亏(右)"`，
  //    而下面 series 的 `name` 是 `"累计盈亏(右)"` —— **两个字符串不同** ⇒ ECharts 按 name 匹配，
  //    匹配不到就**整条从图例里去掉**（且不报错）⇒ 图上只有一条线有图例，黄线成了"无名线"。
  //    修法：两者统一成 `"累计总盈亏(右·元)"`。**改 series.name 时必须同步改 legend.data**（反之亦然），
  //    否则会再次静默丢图例 —— 这类"名字对不上就消失"的细节，`node --check` 查不出来。
  echGuard("累计收益趋势", () => {
    const curve = (acc ? v.cum : (d.cum_all || [])).filter(x => x.cum_total != null);
    const cpTotal = document.getElementById("thCumPctTotal");
    if (cpTotal) {
      const last = curve[curve.length - 1];
      cpTotal.textContent = last
        ? `${acc ? "账户 " + esc(acc) : "两账户合计"} · 末值 ${((last.pct_total ?? last.pct) >= 0 ? "+" : "") + fmt(last.pct_total ?? last.pct, 2)}%`
          + `（总盈亏 ${f(last.cum_total)} / 累计买入 ${fmt(last.cum_buy || 0, 0)}）`
        : "";
    }
    if (curve.length >= 2) {
      echRender("echThCumPct", {
        ...ECH_BASE,
        grid: { left: 56, right: 62, top: 34, bottom: 34 },
        // ⚠️ legend.data 与 series[].name 必须**逐字相同**（ECharts 按 name 匹配；
        //    对不上就静默丢掉该图例项，本图 2026-10-14 前就是这样丢了黄线的图例）。改一处必改另一处。
        legend: { data: ["累计收益率(左·%)", "累计总盈亏(右·元)"], textStyle: { color: "#8b949e", fontSize: 11 }, top: 0 },
        tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: p => {
          const r = curve[p[0].dataIndex];
          return `<b>${r.date}</b><br>累计收益: <b style="color:${pnlColor(r.pct_total ?? r.pct)}">`
            + `${((r.pct_total ?? r.pct) >= 0 ? "+" : "") + fmt(r.pct_total ?? r.pct, 2)}%</b>`
            + `<br>累计总盈亏: <b style="color:${pnlColor(r.cum_total)}">${f(r.cum_total)}</b>`
            + `<br><span style="color:#8b949e">已实现 ${f(r.cum_realized)} · 累计买入 ${fmt(r.cum_buy || 0, 0)}</span>`;
        } },
        xAxis: { ...ECH_BASE.xAxis, type: "category", data: curve.map(x => x.date.slice(5)) },
        yAxis: [
          { ...ECH_BASE.yAxis, name: "%", nameTextStyle: { color: "#8b949e", fontSize: 10 },
            axisLabel: { color: "#8b949e", fontSize: 10, formatter: v => (v >= 0 ? "+" : "") + fmt(v, 2) + "%" } },
          { ...ECH_BASE.yAxis, name: "元", nameTextStyle: { color: "#8b949e", fontSize: 10 },
            splitLine: { show: false },
            axisLabel: { color: "#8b949e", fontSize: 10, formatter: v => (v >= 0 ? "+" : "") + fmt(v, 0) } },
        ],
        series: [
          { name: "累计收益率(左·%)", type: "line", yAxisIndex: 0, smooth: true, symbolSize: 3,
            data: curve.map(x => x.pct_total ?? x.pct), lineStyle: { color: "#8ad4ff", width: 2 }, itemStyle: { color: "#8ad4ff" },
            markLine: { silent: true, symbol: "none", lineStyle: { color: "#8b949e", type: "dashed" },
              data: [{ yAxis: 0 }] } },
          // 黄色虚线 = 右轴(元)的**累计总盈亏（含浮动）**。颜色 #d29922（琥珀）刻意与蓝线拉
          // 开色相：蓝=比例、黄=金额，两个口径别混读（详见上方注释块与「历史 bug」）。
          { name: "累计总盈亏(右·元)", type: "line", yAxisIndex: 1, smooth: true, symbol: "none",
            data: curve.map(x => x.cum_total ?? x.cum_realized), lineStyle: { color: "rgba(210,153,34,.8)", width: 1, type: "dashed" },
            itemStyle: { color: "#d29922" } },
        ],
      });
    } else echClear("echThCumPct");
  });

  // 3) 月度盈亏 —— **含浮动**（总盈亏＝Σ发生金额＋月末持仓市值变动）
  //    只看已实现会漏掉浮亏：实测 2026-09 已实现 +3,933，含浮动是 −17,709。
  const mon = v.monthly;
  const tmon = v.tMonthly;      // ⚠️ 必须在下面各图之前声明：const 有 TDZ，
                                // 声明前引用会抛 ReferenceError 并把后面几张图整段吃掉
  echGuard("月度盈亏", () => {
    if (mon.length) {
      echRender("echThMonth", {
        ...ECH_BASE,
        grid: { left: 62, right: 20, top: 34, bottom: 40 },
        legend: { data: ["总盈亏(含浮动)", "已实现"], textStyle: { color: "#8b949e", fontSize: 11 }, top: 0 },
        tooltip: { ...ECH_BASE.tooltip, trigger: "axis", axisPointer: { type: "shadow" },
          formatter: p => { const m = mon[p[0].dataIndex];
            return `<b>${m.month}</b><br>总盈亏: <b style="color:${pnlColor(m.total_pnl || 0)}">${f(m.total_pnl || 0)}</b>`
              + `<br>已实现: <span style="color:${pnlColor(m.realized)}">${f(m.realized)}</span>`
              + `<br><span style="color:#8b949e">费用 ${fmt(m.fees || 0, 0)} · 成交 ${m.n_trades} 笔</span>`; } },
        xAxis: { ...ECH_BASE.xAxis, type: "category", data: mon.map(m => m.month),
          axisLabel: { color: "#8b949e", fontSize: 9, rotate: 45 } },
        yAxis: { ...ECH_BASE.yAxis, axisLabel: { color: "#8b949e", fontSize: 10,
          formatter: v => (v >= 0 ? "+" : "") + fmt(v, 0) } },
        series: [
          { name: "总盈亏(含浮动)", type: "bar", barGap: "-20%",
            data: mon.map(m => ({ value: m.total_pnl || 0,
              itemStyle: { color: pnlColor(m.total_pnl || 0) } })),
            markLine: { silent: true, symbol: "none", lineStyle: { color: "#8b949e", type: "dashed" },
              data: [{ yAxis: 0 }] } },
          { name: "已实现", type: "bar",
            data: mon.map(m => ({ value: m.realized,
              itemStyle: { color: pnlColor(m.realized), opacity: 0.45 } })) },
        ],
      });
    } else echClear("echThMonth");
  });

  echGuard("月度做T净收益", () => {
    // 3b) 月度做T**净**收益 = 当月做T价差 − 当月全部交易费用
    const tnetTitle = document.getElementById("echThTNetTotal");
    const tnetRows = tmon.map(m => ({ month: m.month, net: (m.realized || 0) - (m.fees || 0),
                                      t: m.realized || 0, f: m.fees || 0, n: m.n_pairs || 0 }));
    if (tnetTitle) {
      const total = tnetRows.reduce((a, x) => a + x.net, 0);
      tnetTitle.textContent = tnetRows.length
        ? `合计 ${(total >= 0 ? "+" : "") + fmt(total, 0)} 元` + (total < 0 ? "（做T抵不过手续费）" : "")
        : "";
    }
    if (tnetRows.length) {
      echRender("echThTNet", {
        ...ECH_BASE,
        tooltip: { ...ECH_BASE.tooltip, trigger: "axis", axisPointer: { type: "shadow" },
          formatter: p => { const x = tnetRows[p[0].dataIndex];
            return `<b>${x.month}</b><br>做T净收益: <b style="color:${pnlColor(x.net)}">${f(x.net)}</b>`
              + `<br><span style="color:#8b949e">做T价差 ${f(x.t)} − 费用 ${fmt(x.f, 0)}</span>`
              + `<br>做T日 ${x.n} 个`; } },
        xAxis: { ...ECH_BASE.xAxis, type: "category", data: tnetRows.map(m => m.month),
          axisLabel: { color: "#8b949e", fontSize: 9, rotate: 45 } },
        yAxis: { ...ECH_BASE.yAxis, axisLabel: { color: "#8b949e", fontSize: 10,
          formatter: v => (v >= 0 ? "+" : "") + fmt(v, 0) } },
        series: [{ type: "bar", data: tnetRows.map(m => ({ value: m.net,
          itemStyle: { color: pnlColor(m.net) } })),
          label: { show: true, position: "top", color: "#8b949e", fontSize: 9,
            formatter: p => p.value ? fmt(p.value, 0) : "" },
          markLine: { silent: true, symbol: "none", lineStyle: { color: "#8b949e", type: "dashed" },
            data: [{ yAxis: 0 }] } }],
      });
    } else echClear("echThTNet");
  });

  echGuard("月度交易费用", () => {
    // 4) 月度交易费用柱状图（印花税+佣金+过户费+结算费，全月全部成交口径）
    const feeTitle = document.getElementById("echThFeeTotal");
    const bd = d.fee_breakdown || {};
    if (feeTitle) {
      feeTitle.textContent = v.totalFees
        ? `累计 ${fmt(v.totalFees, 0)} 元（印花税 ${fmt(bd.stamp || 0, 0)} / 佣金 ${fmt(bd.commission || 0, 0)} / 过户费 ${fmt(bd.transfer || 0, 0)}）`
        : "";
    }
    if (mon.length && v.totalFees) {
      echRender("echThFee", {
        ...ECH_BASE,
        tooltip: { ...ECH_BASE.tooltip, trigger: "axis", axisPointer: { type: "shadow" },
          formatter: p => { const m = mon[p[0].dataIndex];
            const per = m.n_trades ? (m.fees / m.n_trades) : 0;
            return `<b>${m.month}</b><br>交易费用: <b style="color:#d29922">${fmt(m.fees || 0, 0)}</b> 元`
              + `<br>成交 ${m.n_trades} 笔 · 单笔均 ${fmt(per, 2)} 元`; } },
        xAxis: { ...ECH_BASE.xAxis, type: "category", data: mon.map(m => m.month),
          axisLabel: { color: "#8b949e", fontSize: 9, rotate: 45 } },
        yAxis: { ...ECH_BASE.yAxis, axisLabel: { color: "#8b949e", fontSize: 10,
          formatter: v => fmt(v, 0) } },
        series: [{ type: "bar", data: mon.map(m => ({ value: m.fees || 0,
          itemStyle: { color: "#d29922" } })),
          label: { show: true, position: "top", color: "#8b949e", fontSize: 9,
            formatter: p => p.value ? fmt(p.value, 0) : "" } }],
      });
    } else echClear("echThFee");
  });

  echGuard("月度做T收益", () => {
    // 5) 月度做T收益柱状图（同账户同票同日买卖配对，扣费净价差）
    const tTitle = document.getElementById("echThTTotal");
    if (tTitle) {
      const nPair = tmon.reduce((a, m) => a + (m.n_pairs || 0), 0);
      tTitle.textContent = tmon.length
        ? `累计 ${(v.totalT >= 0 ? "+" : "") + fmt(v.totalT, 0)} 元 / ${nPair} 个做T日`
        : "（无同日买卖配对，不计做T）";
    }
    if (tmon.length) {
      echRender("echThT", {
        ...ECH_BASE,
        tooltip: { ...ECH_BASE.tooltip, trigger: "axis", axisPointer: { type: "shadow" },
          formatter: p => { const m = tmon[p[0].dataIndex];
            return `<b>${m.month}</b><br>做T收益: <b style="color:${pnlColor(m.realized)}">${f(m.realized)}</b>`
              + `<br>做T日 ${m.n_pairs} 个`; } },
        xAxis: { ...ECH_BASE.xAxis, type: "category", data: tmon.map(m => m.month),
          axisLabel: { color: "#8b949e", fontSize: 9, rotate: 45 } },
        yAxis: { ...ECH_BASE.yAxis, axisLabel: { color: "#8b949e", fontSize: 10,
          formatter: x => (x >= 0 ? "+" : "") + fmt(x, 0) } },
        series: [{ type: "bar", data: tmon.map(m => ({ value: m.realized,
          itemStyle: { color: pnlColor(m.realized) } })),
          markLine: { silent: true, symbol: "none",
            lineStyle: { color: "#8b949e", type: "dashed" }, data: [{ yAxis: 0 }] } }],
      });
    } else echClear("echThT");
  });

  // 明细表（按已实现排序，含仍持仓）
  if (!body) return;
  const tMap = {};
  v.tByStock.forEach(t => { tMap[`${t.account || ""}|${t.code}`] = t; });
  const rows = stocks.map(s => {
    const t = tMap[`${s.account || ""}|${s.code}`];
    return `<tr>
      <td>${esc(s.name)} <span class="mono cell-dim">${esc(s.code)}</span>${s.account ? ` <span class="cell-dim" style="font-size:10px">${esc(s.account)}</span>` : ""}</td>
      <td class="num ${clsOf(s.realized)}">${f(s.realized)}</td>
      <td class="num ${clsOf(s.unrealized || 0)}">${s.unrealized ? f(s.unrealized) : "—"}</td>
      <td class="num ${clsOf(s.total_pnl || 0)}"><b>${f(s.total_pnl || 0)}</b></td>
      <td class="num ${t ? clsOf(t.pnl) : ""}">${t ? f(t.pnl) : "—"}</td>
      <td class="num cell-dim">${t ? t.n_pairs : "—"}</td>
      <td class="num">${s.pnl_pct >= 0 ? "+" : ""}${fmt(s.pnl_pct, 2)}%</td>
      <td class="num cell-dim">${fmt(s.buy_amt, 0)}</td>
      <td class="num cell-dim">${fmt(s.sell_amt, 0)}</td>
      <td class="num cell-dim">${fmt(s.fees, 0)}</td>
      <td class="num">${s.n_trades}</td>
      <td class="num">${s.open_qty ? fmt(s.open_qty, 0) : "—"}</td>
      <td class="mono cell-dim" style="font-size:11px">${esc(s.first)} ~ ${esc(s.last)}</td>
    </tr>`;
  }).join("");
  body.innerHTML = `<div class="card-title">逐股明细（已实现降序，共 ${stocks.length} 只）</div>
    <table><thead><tr><th>股票</th><th class="num">已实现</th><th class="num">浮动</th>
    <th class="num">合计</th><th class="num">做T收益</th>
    <th class="num">做T日</th><th class="num">收益率</th>
    <th class="num">买入额</th><th class="num">卖出额</th><th class="num">费用</th>
    <th class="num">笔数</th><th class="num">仍持有</th><th>区间</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

/* ---- 近60日成交额柱状图（2026-09-30）----
   放在「盘中状态」卡顶部。⚠️ renderLive 每 10s 重建 innerHTML ⇒ 容器每次都是**新节点**，
   必须 dispose 旧实例再 init，否则每 10s 泄漏一个孤立的 ECharts 实例。 */
let turnoverHistInst = null;
let turnoverHistSig = "";
let turnoverHistNode = null;   // 实例绑定的 DOM 节点；节点被重建（renderLive 每 10s 覆写 innerHTML）即需重画
const TURNOVER_UP = "#f85149";     // 放量（与 .h-bar-fill.up 同色）
const TURNOVER_DOWN = "#3fb950";   // 缩量
const TURNOVER_LIVE = "#d29922";   // 进行中：今日未走完，**不参与**放量/缩量判定（否则盘中看着像大幅缩量）
// 5 日均量线（2026-10-15 加）。**刻意不用**上面三色：那三色是"状态"语义（放量/缩量/进行中），
// 均线是"另一个系列"，拿状态色当系列色是 anti-pattern。选蓝色相与三色分离；
// 该值经 dataviz 校验器在暗底(surface #1a1a19)下通过亮度带 L 0.48–0.67 / 色度 / 对比度三项。
const TURNOVER_MA5 = "#388bfd";
const TURNOVER_MA5_N = 5;          // 均线窗口（交易日）

// 把失败原因**写在卡片里**：此前各条失败路径都是静默 return ⇒ 界面只剩空白，
// 分不清是"没数据""没 echarts"还是"后端没这个接口"。空白本身不携带任何信息。
let _turnoverRetry = null;   // 成交额图 warming 时的重试定时器
let _turnoverSizeRetry = null;      // 容器零尺寸（面板隐藏/窗口最小化）时的重试
let _turnoverSizeRetryN = 0;
/* ⚠️ 2026-10-18 修复：错误信息**绝不能写进图表容器**。
   一旦 `box.innerHTML = ...` 把 ECharts 的 canvas 抹掉，实例还活着、节点也没换
   ⇒ 下次可见时走"同节点 → 原地 setOption"分支，永远画不出东西，错误文案也永远留在那儿
   （owner 截图：盘中状态成交额图停在"容器尺寸为 0 (0×0)"，切回 tab 也不恢复）。
   现在错误写到卡片里的独立 `#turnoverHistMsg`，图表容器保持干净。 */
function _turnoverFail(msg) {
  const msgEl = document.getElementById("turnoverHistMsg");
  if (msgEl) {
    msgEl.innerHTML = `<span class="down">成交额图不可用：</span>${esc(msg)}`;
    return;
  }
  const box = document.getElementById("turnoverHistChart");
  if (box && !turnoverHistInst) {      // 没有实例才允许写进容器（否则会抹掉 canvas）
    box.innerHTML = `<div class="empty" style="font-size:11px">成交额图不可用：${esc(msg)}</div>`;
  }
}
function _turnoverMsgClear() {
  const msgEl = document.getElementById("turnoverHistMsg");
  if (msgEl) msgEl.innerHTML = "";
}
function _turnoverInfo(text) {   // 中性提示（加载中）——同样不许写进图表容器
  const msgEl = document.getElementById("turnoverHistMsg");
  if (msgEl) msgEl.innerHTML = `<span class="cell-dim">${esc(text)}</span>`;
}
function _scheduleTurnoverSizeRetry() {
  if (_turnoverSizeRetry || _turnoverSizeRetryN >= 20) return;   // 最多约 30s，之后靠 10s 轮询
  _turnoverSizeRetryN++;
  _turnoverSizeRetry = setTimeout(() => { _turnoverSizeRetry = null; drawTurnoverHist(); }, 1500);
}

async function drawTurnoverHist() {
  const box = document.getElementById("turnoverHistChart");
  if (!box) return;
  if (!window.echarts) { _turnoverFail("ECharts 未加载"); return; }
  // 面板隐藏（别的 tab / 窗口最小化）时容器宽高为 0：**连 API 都不用打**，
  // 等可见时 switchTab / visibilitychange / 短重试会自动重画。
  if (!box.clientWidth || !box.clientHeight) {
    _turnoverFail(`容器尺寸为 0（${box.clientWidth}×${box.clientHeight}）`);
    _scheduleTurnoverSizeRetry();
    return;
  }
  let d = null;
  try {
    // 2026-10-16 owner：「要紧密，这样可以显示更多」+「增加到一年」⇒ 250 个交易日 ≈ 1 年。
    // ⚠️ 后端**北交所腿只够 188 个交易日**（新浪 30min datalen 硬上限 1500 根），
    //    更早的日子 amount 只有沪深两腿、少算约 120 亿/日（~0.6%）⇒ 由后端逐日标
    //    `bse_missing`，图上必须**看得出来**（图例下方那条提示），不许静默。
    d = await apiCall("load_turnover_history", 250);
  } catch (e) {
    // 最常见：后端进程还是旧代码（没有 load_turnover_history 这个接口）⇒ 必须重启 t_gui.py
    _turnoverFail(`接口调用失败（${e && e.message ? e.message : e}）· 若刚改过 t_gui.py 请重启它`);
    return;
  }
  if (document.getElementById("turnoverHistChart") !== box) return;   // 期间又被重建（下一次会画）
  if (!d || !d.available || !(d.days || []).length) {
    if (d && d.warming) {
      // 后端还在算（冷启动/超时占位）→ 显示加载中并稍后自填，不当错误。
      // ⚠️ 写到 #turnoverHistMsg，**不能写进图表容器**（会抹掉 ECharts 的 canvas）。
      _turnoverInfo("成交额图加载中…");
      if (!_turnoverRetry) {
        _turnoverRetry = setTimeout(() => { _turnoverRetry = null; drawTurnoverHist(); }, 8000);
      }
      return;
    }
    _turnoverFail((d && d.error) || "后端返回空");
    return;
  }
  if (_turnoverRetry) { clearTimeout(_turnoverRetry); _turnoverRetry = null; }
  const days = d.days;
  const last = days[days.length - 1];
  // ── 5 日均量（2026-10-15）──
  // ⚠️ 只统计**已完成**的交易日：今日那根是"进行中"的半日额，把它算进均线会让均线盘中一路
  //    下坠、今天看着像大幅缩量（正是 TURNOVER_LIVE 那条注释在防的同一个坑）。
  //    因此今日 x 上的均线值 = "前 5 个完整交易日的均量"，正好当横比基准用。
  const ma5 = [];
  {
    const done = [];
    for (let i = 0; i < days.length; i++) {
      if (!days[i].in_progress) done.push(days[i].amount);
      ma5.push(done.length >= TURNOVER_MA5_N
        ? done.slice(-TURNOVER_MA5_N).reduce((a, b) => a + b, 0) / TURNOVER_MA5_N / 1e8
        : null);                                   // 前 4 根凑不满窗口 → null，不画半截均线
    }
  }
  // ── 北交所腿的覆盖披露（2026-10-16）──────────────────────────────
  // 为什么必须显示：北交所源（新浪 bj899050 30min）**硬上限 188 个交易日**，而图要给一年。
  // 缺的那段 amount 只有沪深两腿 ⇒ 少约 120 亿/日（~0.6%）。0.6% 在图上只有 ~1 像素，
  // **静默**过去就会被读成"那阵子缩量" —— 本仓最忌讳的就是这种"看不出是缺数据"。
  // ⚠️ 必须在下面的 `sig` 早返回**之前**设置：否则签名没变时早返回，提示会是空的。
  {
    const noteEl = document.getElementById("turnoverBseNote");
    if (noteEl) {
      const _notes = [];
      const miss = days.filter(x => x.bse_missing).length;
      if (!d.bse_included) {
        _notes.push(`<span style="color:#d29922">⚠ 北交所腿不可得（该段仅沪深两市）</span>`);
      } else if (miss > 0) {
        _notes.push(`<span style="color:#d29922" title="北交所源=新浪 bj899050 30分钟线，datalen 硬上限 1500 根 = 188 个交易日；再往上取会返反爬桩。缺的这段成交额只含沪深两市。">⚠ 最早 ${miss} 日缺北交所（源上限 188 日）· 那几日少算约 120 亿/日（~0.6%）</span>`);
      }
      // 沪深两腿不可信的日子（2026-10-16）：腾讯回退时会把 volume 塞进 amount，量级差 ~1000 倍，
      // 相加会让当天**腰斩**（owner 截图：图上一半高、环比 +99%）。后端已整根略去、不画半截柱，
      // 并把算对过的历史值**冻结**起来 ⇒ 这里如实报个数。
      if (d.hs_leg_unreliable_days > 0) {
        _notes.push(`<span style="color:#d29922" title="沪深两腿里有一腿的量级像“成交量”而非“成交额”（腾讯 ifzq 指数日线没有成交额字段，回退时用 volume 顶替）⇒ 该日整根已略去。历史值一旦算对会被冻结，不再被降级计算覆盖。">⚠ ${d.hs_leg_unreliable_days} 日沪深数据不可信·已略去</span>`);
      }
      noteEl.innerHTML = _notes.length ? "　" + _notes.join("　") : "";
    }
  }

  // 签名含**容器宽度**：面板隐藏时首次渲染拿到 0 宽，切到该 tab 后宽度变化必须触发重画，
  // 否则会永远停在 0 宽的"空图"上。宽度变化 = 需要重画。
  // 签名带 MA5：均线只由**已完成日**决定（盘中恒定），在这里带上是为防"历史某日被修正"时漏重画。
  const sig = `${days.length}|${last.date}|${Math.round(last.amount)}|`
    + `${Math.round(ma5[ma5.length - 1] || 0)}|${box.clientWidth}`;
  // ⚠️ 必须带上节点判等：renderLive 每 10s 覆写 #liveBody.innerHTML，`box` 每次都是**新节点**，
  // 而旧实例仍绑在**已脱离文档**的旧节点上。若只看数据签名就早返回，新节点永远拿不到图
  // ⇒ 成交额图渲染几秒后消失（2026-10-04 修）。节点变了即强制重画。
  const _sameNode = !!(turnoverHistInst && box === turnoverHistNode);
  // 2026-10-18：canvas 还在不在也要判。旧版本把错误文案写进容器 ⇒ canvas 被 innerHTML 抹掉、
  // 实例却活着 ⇒ 同节点 setOption 永远画不出来。这里若发现 canvas 没了，强制重建实例。
  const _canvasOk = !!(box.querySelector && box.querySelector("canvas"));
  if (_sameNode && _canvasOk && sig === turnoverHistSig) {
    if (turnoverHistInst.getWidth() !== box.clientWidth) {
      try { turnoverHistInst.resize(); } catch (e) { }
    }
    return;                                   // 同节点 + 数据与尺寸都没变 → 什么都不做（防闪烁）
  }
  if (!box.clientWidth || !box.clientHeight) {
    // 容器零尺寸时 ECharts 会画出"空图"且**不报错**。此时**不记签名**，
    // 等面板可见（宽度非 0）再画 —— 记了签名就再也不会重试了。
    // 另外排一个短重试：窗口从最小化恢复 / tab 切回来时不必等下一个 10s 轮询。
    _turnoverFail(`容器尺寸为 0（${box.clientWidth}×${box.clientHeight}）`);
    _scheduleTurnoverSizeRetry();
    return;
  }
  // 2026-10-15 闪烁修复（关键）：**同节点时不再 dispose + init**。
  // dispose 会把容器里的 canvas 整个拆掉、init 再建一个新的 ⇒ 每次数据变化都闪一下；
  // 原来只在 renderLive 重建 DOM（10s 一次）时走这条路，现在节点已被 renderLive 回收复用，
  // 数据每 10s 都在变，若仍 dispose+init 就是"每 10s 稳定闪一次"。改为原地 setOption：
  // 实例与 canvas 都活着，只重画内容 ⇒ 不闪。
  if (turnoverHistInst && (!_sameNode || !_canvasOk)) {
    try { turnoverHistInst.dispose(); } catch (e) { }
    turnoverHistInst = null;
    turnoverHistNode = null;
  }
  // 成功路径：清掉错误文案 + 重置零尺寸重试计数
  _turnoverMsgClear();
  _turnoverSizeRetryN = 0;
  if (_turnoverSizeRetry) { clearTimeout(_turnoverSizeRetry); _turnoverSizeRetry = null; }
  if (!turnoverHistInst) {
    // 清掉可能残留的历史错误文案（旧版本会把错误写进容器，echarts.init 是**追加**，
    // 不清的话文字会压在图上）
    box.innerHTML = "";
    turnoverHistInst = echarts.init(box);
    turnoverHistNode = box;
  }
  const data = days.map(x => ({
    value: Math.round(x.amount / 1e8),
    itemStyle: {
      color: x.in_progress ? TURNOVER_LIVE
        : (x.delta_pct == null ? "#8b949e" : (x.delta_pct > 0 ? TURNOVER_UP : TURNOVER_DOWN)),
    },
  }));
  // `notMerge: true`：整体替换配置。**关键**——`title` 在非 stale 时是 `undefined`，
  // 而 setOption 的默认合并语义**不会清掉**已存在的 title ⇒ 快照标题会粘在图上不走。
  // notMerge 只是重算配置并重绘画布，**不拆实例/canvas**，所以不引起闪烁。
  turnoverHistInst.setOption({
    // 冷启动快照（2026-10-10）：stale 时在图上点名"这是快照、非实时"，并让出顶部空间。
    // 注意：stale 快照的 `available` 为 true，所以上面「warming → 8s 重试」分支不会触发——
    // 必须靠这条标题让用户知道看到的不是实时值（安全契约：不得静默当实时）。
    title: d.stale
      ? {
        text: `⚠ 快照 ${d.snapshot_ts || "—"}（后台刷新中）`, left: 46, top: 0,
        textStyle: { fontSize: 10, color: "#d29922", fontWeight: "normal" },
      }
      : undefined,
    grid: { left: 46, right: 34, top: d.stale ? 22 : 8, bottom: 18 },   // right 34 = 给均线末端直接标注留位
    tooltip: {
      trigger: "axis",
      formatter: (ps) => {
        const x = days[ps[0].dataIndex];
        const dt = x.delta_pct == null ? "—" : `${x.delta_pct >= 0 ? "+" : ""}${x.delta_pct.toFixed(2)}%`;
        let s = `${x.date}${x.in_progress ? "（进行中）" : ""}<br>`
          + `全市场 ${fmtAmount(x.amount)}<br>环比前一交易日 ${dt}`;
        // 该日北交所腿缺失 ⇒ 逐日点名（否则这天的柱子会看起来"缩量"，其实是少了一腿）
        if (x.bse_missing) s += `<br><span style="color:#d29922">⚠ 该日缺北交所（少算约 120 亿）</span>`;
        const m = ma5[ps[0].dataIndex];
        if (m != null) {
          s += `<br><span style="color:${TURNOVER_MA5}">${TURNOVER_MA5_N}日均量</span> ${m.toFixed(0)}亿`;
          if (x.in_progress) {
            // 半日额除以全日均量必然大幅为负 ⇒ 不做偏离数，只说明均线口径
            s += `<br><span style="opacity:.7">（均线不含今日；今日未走完，暂不可比）</span>`;
          } else {
            const dev = (x.amount / 1e8 / m - 1) * 100;
            s += `<br>偏离均线 ${dev >= 0 ? "+" : ""}${dev.toFixed(1)}%`;
          }
        }
        return s;
      },
    },
    xAxis: {
      type: "category", data: days.map(x => x.date.slice(5)),
      // 日期标签数**随根数自适应**（2026-10-16）：写死 interval:9 是按 60 根调的，
      // 换成 250 根后只显示 6 个标签、还挤在左侧。目标 ≈12 个标签 ⇒ interval = ceil(n/12)-1。
      // （ECharts 语义：interval=n 表示每隔 n 个类目显示一个 ⇒ 显示数 ≈ n_cat/(n+1)。）
      axisLabel: {
        fontSize: 9, color: "#8b949e",
        interval: Math.max(0, Math.ceil(days.length / 12) - 1),
      },
      axisTick: { show: false }, axisLine: { lineStyle: { color: "rgba(139,148,158,.3)" } },
    },
    yAxis: {
      type: "value",
      axisLabel: {
        fontSize: 9, color: "#8b949e",
        formatter: (v) => (v >= 10000 ? (v / 10000).toFixed(1) + "万亿" : v + "亿"),
      },
      splitLine: { lineStyle: { color: "rgba(139,148,158,.15)" } },
    },
    series: [
      {
        // 2026-10-16 owner：「柱之间要紧密，这样可以显示更多」⇒ `barCategoryGap: "0%"`（默认 20%）。
        // 250 根下类目宽 ≈6px，紧密后每根都画得出来、不留空档。
        // `barMaxWidth` 保留：它只在**类目很宽**（根数少/面板超宽）时生效，防柱子胖成色块。
        type: "bar", data, barMaxWidth: 12, barCategoryGap: "0%",
      },
      {
        // 5 日均量线。**共用同一个 y 轴（亿元）**——不是双轴图（anti-pattern 明令禁止双 y 轴；
        // 两者单位相同，本就不需要第二条轴）。
        name: `${TURNOVER_MA5_N}日均量`,
        type: "line",
        data: ma5,
        smooth: false,
        symbol: "none",
        connectNulls: false,               // 前 4 根凑不满窗口 ⇒ 线段从第 5 根起，不往前接
        lineStyle: { width: 2, color: TURNOVER_MA5 },   // 2px 线（规范线宽）
        itemStyle: { color: TURNOVER_MA5 },
        z: 3,                              // 压住柱顶
        silent: true,                      // 悬停由柱的统一 axis tooltip 承担，线不抢命中
        endLabel: {                        // 末端直接标注 ⇒ 身份不靠颜色单独承担
          show: true, formatter: `MA${TURNOVER_MA5_N}`,
          color: TURNOVER_MA5, fontSize: 9, distance: 4,
        },
      },
    ],
  }, { notMerge: true });
  turnoverHistSig = sig;                       // **画成功之后**才记签名
}

/* ══ DOM 节点回收（2026-10-15）══════════════════════════════════════════════
   renderLive / renderHunter 每轮都 `el.innerHTML = ...` 重建整块，图表容器每次都是**新节点**，
   而 ECharts 实例绑在节点上 ⇒ 不回收就得 dispose+init，容器里的 canvas 被拆掉再建 = 肉眼可见的闪。
   做法：重建**前**把节点摘下来（脱离文档，但节点与实例都活着），重建后按占位放回。
   ⚠️ 两侧必须配对使用：摘了不放回 = 图凭空消失。 */
function _keepNode(id) {
  const n = document.getElementById(id);
  if (n && n.parentNode) n.parentNode.removeChild(n);
  return n;
}
function _putBackNode(slotId, node, fallbackHtml) {
  const s = document.getElementById(slotId);
  if (!s) return;
  if (node) s.parentNode.replaceChild(node, s);
  else s.outerHTML = fallbackHtml;             // 首次渲染没有旧节点 ⇒ 就地新建同 id 容器
}

/* ---- 猎手「每日建仓符合(GO)只数」图（2026-10-15）----
   owner：每日建仓符合的股票数量做成柱状图 + 5日均线，显示 60 天。
   ⚠️ **0 柱是设计使然不是 bug**：`timing_decision` 在 regime=range 时 go 恒 False（震荡市降频），
      实测近 60 日 range 占 36 天全 0。故 tooltip **必须**带 regime 说明，否则看着像图坏了。
   ⚠️ 首次计算要 20~25 分钟（全池 1530 只 × 60 日，逐日落盘）⇒ 未就绪时显示进度并轮询。 */
const HUNTER_GO_BAR = "#e8531f";   // = 主题 --red。与 MA5 蓝经 dataviz 校验器在浅底全项通过(CVD ΔE 30.4)
const HUNTER_GO_MA = "#388bfd";    // = 成交额图那条均线的蓝，同一套视觉语言
const HUNTER_GO_MA_N = 5;
const HUNTER_GO_REGIME_CN = {
  trend_up: "多头趋势", trend_dn: "空头趋势", range: "震荡市（降频不建仓）",
};
let hunterGoInst = null, hunterGoNode = null, hunterGoSig = "";
let _hunterGoTimer = null;
// 「符合(GO)个股」明细：当前选中日 + 已取回的按日缓存（同一天不重复取）
let _hunterGoDays = [];          // 最近一次画图用的 days（点柱子时按 dataIndex 反查日期）
let _hunterGoSel = null;         // 当前展示的日期
const _hunterGoCache = {};       // date -> {available, regime, go, n_ok, stocks}

function hunterGoListHtml() {
  if (!_hunterGoSel) return "";
  const r = _hunterGoCache[_hunterGoSel];
  if (!r) return `<span class="cell-dim">⏳ 取 ${esc(_hunterGoSel)} 明细…</span>`;
  if (!r.available) return `<span class="cell-dim">${esc(r.error || "无明细")}</span>`;
  if (!r.stocks.length) {
    return `<span class="cell-dim">${esc(r.date)}：<b>0 只</b>符合`
      + `（${esc(HUNTER_GO_REGIME_CN[r.regime] || r.regime || "—")}${r.regime === "range" ? " ⇒ 策略降频，当日不建仓" : ""}）</span>`;
  }
  const grp = {};
  for (const s of r.stocks) (grp[s.sector || "未分类"] ||= []).push(s);
  const secs = Object.keys(grp).sort((a, b) => grp[b].length - grp[a].length || a.localeCompare(b));
  return `<div style="margin-bottom:3px">
      <b>${esc(r.date)}</b> 符合 <b>${r.go}</b> 只 · ${secs.length} 个板块
      <span class="cell-dim">（${esc(HUNTER_GO_REGIME_CN[r.regime] || r.regime || "—")}；池内可算 ${r.n_ok || 0} 只）</span>
    </div>`
    + secs.map(sec => `<div style="margin:2px 0">
        <span class="badge" style="background:var(--bg-soft);color:var(--text-dim)">${esc(sec)} ${grp[sec].length}</span>
        ${grp[sec].map(s => `<a href="javascript:void(0)" onclick="openStockChart('${esc(s.code)}','${esc(s.name)}')"
            style="margin-right:9px;text-decoration:none;color:inherit"
            title="点开 ${esc(s.code)} 日线图">${esc(s.code)} <b>${esc(s.name)}</b></a>`).join("")}
      </div>`).join("");
}

function renderHunterGoList() {
  const el = document.getElementById("hunterGoList");
  if (el) el.innerHTML = hunterGoListHtml();
}

async function selectHunterGoDay(date) {
  if (!date) return;
  _hunterGoSel = date;
  renderHunterGoList();
  if (_hunterGoCache[date]) return;                  // 已取过
  try {
    _hunterGoCache[date] = await apiCall("load_hunter_go_stocks", date);
  } catch (e) {
    _hunterGoCache[date] = { available: false, date, stocks: [], error: (e && e.message) || String(e) };
  }
  renderHunterGoList();
}

function _hunterGoFail(msg) {
  const box = document.getElementById("hunterGoChart");
  if (box) box.innerHTML = `<div class="empty" style="font-size:11px">${esc(msg)}</div>`;
}

function hunterGoMa(go) {
  const out = [];
  for (let i = 0; i < go.length; i++) {
    if (i < HUNTER_GO_MA_N - 1) { out.push(null); continue; }
    let s = 0;
    for (let k = i - HUNTER_GO_MA_N + 1; k <= i; k++) s += go[k];
    out.push(s / HUNTER_GO_MA_N);
  }
  return out;
}

/* ---- 板块星轨图（2026-10-17）：近十日 排名(位置) × 平均分(星大小) × 轨迹(排名变化) ----
   口径见后端 `Api.load_sector_constellation` 的 docstring：排名只在当日集合内可比、
   平均分不能跨"入池日"直接比 —— 这两条会随图显示在 #sectorStarsNote 里，别删。 */
const SECTOR_STAR_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"];
const SECTOR_STAR_MUTED = "#b9c2cf";     // 未高亮板块：中性灰，作为背景星
const SECTOR_STAR_HL = 6;                // 高亮条数（浅色下对比度 <3:1 的三个槽位靠直标+表格兜底）

/* 「连续提升」筛选（2026-10-17 owner）：只看最近 10 日排名持续提升的板块。
   判定在后端 `load_sector_constellation`（sector.sustained）：
   至少 5 天名次变好 且 首尾净提升 ≥5 名 —— 严格"天天变好"在这套数据里一天都没有，
   实测最长的连续严格提升只有 3 天，所以口径必须放宽到"过半天数在变好 + 最终确实上去了"。 */
let _starSustainedOnly = false;
let _starFocusOnly = false;                 // 「该关注」= 排名 ≥ STAR_FOCUS_RANK_MIN
let _starLast = null;

/* 「该关注」的阈值与依据（2026-10-17 实验，判词见 doc/research/2026-10-17_板块排名_前向收益_星空图判词.md）：
   H=5 超额：1-3名 −1.07%(t−2.18) · 4-6名 −0.68%(t−2.20) · 7-10名 −0.53%(t−2.13)
             · **11-15名 +0.88%(t+2.92)** · 16名以后 +0.74%(t+2.87)
   非重叠检验 11 名后 +1.26%(t+2.27)；按板块大小分半两个半边都成立。
   ⇒ 排名 1-10 是**规避**区，≥11 才是唯一有正超额的一段。 */
const STAR_FOCUS_RANK_MIN = 11;

function toggleStarSustained() {
  _starSustainedOnly = !_starSustainedOnly;
  if (_starSustainedOnly) _starFocusOnly = false;      // 两个筛选互斥
  if (_starLast) renderSectorStars(_starLast);
}

function toggleStarFocus() {
  _starFocusOnly = !_starFocusOnly;
  if (_starFocusOnly) _starSustainedOnly = false;
  if (_starLast) renderSectorStars(_starLast);
}

/* 猎手/重新生成跑完后调用：数据源 `daily_summary.json` 已被重写，星轨图必须跟着重画。
   只在猎手页可见时画 —— 隐藏容器拿到 0 尺寸，画了也是空白；切回该 tab 时 switchTab
   里本来就会再调一次 `loadSectorStars()`。 */
function refreshSectorStarsIfVisible() {
  const act = document.querySelector(".sidebar-item.active");
  const box = document.getElementById("echSectorStars");
  if (!box || !document.body.contains(box)) return;
  if (act && act.dataset.tab !== "hunter") return;
  loadSectorStars();
  // 猎手跑完也会回写当日 bar（MA5/MA20 随之改变）⇒ 下方「站上5/20日线」小题板一起刷新
  loadSectorMaBreadth();
}

async function loadSectorStars(force) {
  const box = document.getElementById("echSectorStars");
  if (!box) return;
  if (!window.echarts) { echClear("echSectorStars"); return; }
  try {
    const d = await apiCall("load_sector_constellation", 10, state.date || null);
    echGuard("板块星轨图", () => renderSectorStars(d));
  } catch (e) {
    console.error("[图表] 板块星轨图 取数失败：", e);
    echClear("echSectorStars");
    const n = document.getElementById("sectorStarsNote");
    if (n) n.textContent = `取数失败：${e.message}`;
  }
}

function renderSectorStars(d) {
  const noteEl = document.getElementById("sectorStarsNote");
  const tblEl = document.getElementById("sectorStarsTable");
  if (!d || !d.available) {
    echClear("echSectorStars");
    if (noteEl) noteEl.textContent = (d && d.error) || "无板块排名记录";
    if (tblEl) tblEl.innerHTML = "";
    return;
  }
  _starLast = d;
  const ymds = d.dates.map(x => x.ymd);
  const labels = d.dates.map(x => x.label);
  // 两个筛选互斥：连续提升 / 该关注（排名 ≥11）。开启时只画命中的板块，其余整条隐藏。
  const sust = d.sectors.filter(s => s.sustained);
  const focus = d.sectors.filter(s => (s.last_rank || 0) >= STAR_FOCUS_RANK_MIN);
  const shownSecs = _starSustainedOnly ? sust : (_starFocusOnly ? focus : d.sectors);
  const _sbtn = document.getElementById("oscSustainedBtn");
  if (_sbtn) {
    _sbtn.textContent = `📈 连续提升 (${sust.length})`;
    _sbtn.classList.toggle("primary", _starSustainedOnly);
  }
  const _fbtn = document.getElementById("oscFocusBtn");
  if (_fbtn) {
    _fbtn.textContent = `🔍 该关注 (${focus.length})`;
    _fbtn.classList.toggle("primary", _starFocusOnly);
  }
  const hl = shownSecs.slice(0, SECTOR_STAR_HL);
  const rest = shownSecs.slice(SECTOR_STAR_HL);
  const maxS = Math.max(1e-9, d.max_score || 1);
  // 面积编码：size ∝ √score（面积≈得分），最小 8px 满足标记下限
  const sizeOf = (s, isHl) => (isHl ? 8 + 10 * Math.sqrt(Math.max(0, s) / maxS) : 8);
  const at = (sec, xi) => sec.points.find(q => q.ymd === ymds[xi]) || null;

  const mk = (sec, color, isHl) => ({
    name: sec.name,
    type: "line",
    data: ymds.map((ymd, xi) => {
      const p = at(sec, xi);
      if (!p) return null;                       // 当天未上榜 ⇒ 断开轨迹（不连假线）
      return {
        value: [xi, p.rank],
        symbolSize: sizeOf(p.score, isHl),
        itemStyle: { color, borderColor: "#fff", borderWidth: 1 },
      };
    }),
    connectNulls: false,
    showSymbol: true,
    symbol: "circle",
    // 2026-10-17 owner：日与日之间**用曲线**（原来直线）。0.4 的张力不会过冲太夸张，
    // 又足以让轨迹读成"走势"而不是折线；`smoothMonotone` 会给最小值/最大值那行保形，
    // 但对**排名**这种 1..N 的离散量反而会让"掉出名次又回来"被画平 ⇒ 不用它。
    smooth: 0.4,
    lineStyle: { color, width: isHl ? 2 : 1, opacity: isHl ? 1 : 0.5 },
    itemStyle: { color },
    emphasis: { focus: "series", lineStyle: { width: isHl ? 3 : 2 } },
    // 2026-10-17 owner：「显示不全，需要显示全」⇒ **所有板块**都在末端直标。
    // 25 个板块五天全在榜 ⇒ 末日名次互不相同 ⇒ 右端标签各占一行、不会重叠。
    // 高亮的用正文墨色加粗，其余用灰色小字（保持层级，但仍能认出是谁）。
    endLabel: { show: true, formatter: sec.name,
                color: isHl ? "#1f2937" : "#9ca3af",
                fontSize: isHl ? 11 : 10, fontWeight: isHl ? 600 : 400,
                distance: 8, align: "left" },
    z: isHl ? 3 : 1,
  });

  const hlSet = new Set(hl.map(s => s.name));
  const series = [...hl.map((s, i) => mk(s, SECTOR_STAR_COLORS[i], true)),
                  ...rest.map(s => mk(s, SECTOR_STAR_MUTED, false))];

  echRender("echSectorStars", {
    backgroundColor: "transparent",
    textStyle: { color: "#6b7280" },
    // 图例只列高亮的 6 个（其余灰星不进图例，避免 20+ 项噪音）
    legend: {
      data: hl.map(s => s.name), top: 0, itemWidth: 14, itemHeight: 8,
      textStyle: { color: "#6b7280", fontSize: 11 }, icon: "roundRect",
    },
    // 右边距留给**全部**板块的末端直标（最长 5 个汉字 ≈ 60px）+ 一点余量
    grid: { left: 56, right: 150, top: 34, bottom: 30 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#ffffff", borderColor: "#d3d9e3", borderWidth: 1,
      textStyle: { color: "#1f2937", fontSize: 12 },
      extraCssText: "box-shadow:0 6px 18px rgba(40,60,90,.14);",
      axisPointer: { type: "line", lineStyle: { color: "#9ca3af", type: "dashed" } },
      formatter: params => {
        const xi = (params && params[0] && params[0].dataIndex) || 0;
        const rows = shownSecs
          .map(s => { const p = at(s, xi); return p ? { s, p } : null; })
          .filter(Boolean).sort((a, b) => a.p.rank - b.p.rank);
        let out = `<b>${esc(labels[xi])}</b>`
          + `<span style="color:#6b7280;font-size:11px">　${rows.length} 个板块上榜</span>`;
        rows.slice(0, 8).forEach(({ s, p }) => {
          const dot = hlSet.has(s.name)
            ? `<span style="color:${SECTOR_STAR_COLORS[hl.findIndex(x => x.name === s.name)]}">●</span>`
            : '<span style="color:#b9c2cf">·</span>';
          out += `<br>${dot} ${esc(s.name)}　<b>#${p.rank}</b>　`
            + `${p.score.toFixed(2)} 分　涨停 ${p.lu}`;
        });
        if (rows.length > 8) {
          out += `<br><span style="color:#9ca3af;font-size:11px">…及其余 ${rows.length - 8} 个</span>`;
        }
        return out;
      },
    },
    xAxis: {
      type: "category", data: labels, boundaryGap: true,
      axisLine: { lineStyle: { color: "#d3d9e3" } },
      axisTick: { show: false },
      axisLabel: { color: "#6b7280", fontSize: 11 },
    },
    yAxis: {
      // ⚠️ 上下各留 1 个名次的余量（0 … max+1），刻度才是整数 1..N —— 用 0.5 起始会让刻度落在
      //    0.5/1.5/2.5…，整数过滤把它们全滤掉、纵轴一个数字都不显示。
      type: "value", inverse: true, min: 0, max: (d.max_rank || 1) + 1, interval: 1,
      // 轴名不画：面板副标题已写「位置＝当日排名」；`nameLocation:end` 会压在最后一个刻度上。
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: "#eef1f6" } },
      axisLabel: {
        color: "#9ca3af", fontSize: 10,
        formatter: v => (v >= 1 && v <= (d.max_rank || 1) ? v : ""),
      },
    },
    series,
  });

  if (noteEl) {
    const moved = d.sectors.filter(s => s.days >= 2)
      .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta)).slice(0, 3)
      .map(s => `${esc(s.name)} ${s.delta > 0 ? "+" : ""}${s.delta}`).join(" · ");
    let line;
    if (_starSustainedOnly) {
      line = `● <b>只看连续提升</b>：${sust.length} 个板块（口径：${d.dates.length} 日里至少 `
        + `${Math.ceil(d.dates.length / 2)} 天名次变好、且净提升 ≥ 5 名）—— `
        + (sust.map(s => `${esc(s.name)} <b>+${s.delta}</b>（改善 ${s.ups} 天）`).join(" · ") || "无")
        + `<br><span class="cell-dim">⚠ 实验结论：这条轨迹**没有**前瞻增量（前进 vs 不变 t&lt;1），仅作观察。</span>`;
    } else if (_starFocusOnly) {
      // 依据直接写在这儿 —— 这个按钮的意义就是"照实验结论筛"，必须让人看见凭什么
      line = `● <b>该关注</b>：${focus.length} 个板块（最新排名 ≥ ${STAR_FOCUS_RANK_MIN} 名）—— `
        + (focus.map(s => `${esc(s.name)} <b>#${s.last_rank}</b>${s.delta != null ? `（${d.dates.length}日 ${s.delta > 0 ? "+" : ""}${s.delta}）` : ""}`).join(" · ") || "无")
        + `<br>📊 <b>依据</b>（2026-10-17 实验：60 个交易日 · 板块内等权 · 已去大盘 beta · 持有 5 日）：`
        + `排名 <b>1-3 名 −1.07%</b>(t−2.18) · <b>4-6 名 −0.68%</b>(t−2.20) · <b>7-10 名 −0.53%</b>(t−2.13) `
        + `⇒ 追前面的会跑输；<b>11-15 名 +0.88%</b>(t+2.92)、16 名以后 +0.74%(t+2.87) ⇒ 只有这一段有正超额。`
        + `非重叠检验 +1.26%(t2.27)，按板块大小分半两个半边都成立。`
        + `<br><span class="cell-dim">⚠ 这是<b>相对</b>优势：样本期大盘下行，三档绝对收益全为负（−0.8%~−2.3%）。`
        + `这张图只选板块、不择时；且样本仅 60 天单一 regime，别当铁律。</span>`;
    } else {
      line = `● 图上共 <b>${d.sectors.length}</b> 个板块（<b>全部</b>已画出）：前 ${SECTOR_STAR_HL} 名上色直标，`
        + `其余为灰星背景；星越大＝当日平均分越高（面积≈得分）。　排名变动最大：${moved || "—"}`;
    }
    noteEl.innerHTML = line + `<br>⚠ ${esc(d.note || "")}`;
  }
  if (tblEl) {
    // 表格视图（无障碍兜底）：行=板块，列=日期，格子=当日名次/分数
    // 2026-10-17 owner：「只显示部分，需要显示全部」⇒ 表格列出**全部**板块（原来截前 12）。
    // 外层给了 max-height + 滚动，所以列全不会把面板撑爆。
    const rows = shownSecs.map(s => `<tr>
      <td>${esc(s.name)}</td>
      ${ymds.map((ymd, xi) => {
        const p = at(s, xi);
        return `<td class="num">${p ? `#${p.rank} <span class="cell-dim">${p.score.toFixed(1)}</span>` : "—"}</td>`;
      }).join("")}
      <td class="num">${s.delta == null ? "—" : (s.delta > 0 ? "+" : "") + s.delta}</td>
    </tr>`).join("");
    tblEl.innerHTML = `<table><thead><tr><th>板块</th>`
      + labels.map(l => `<th class="num">${esc(l)}</th>`).join("")
      + `<th class="num">五日变化</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
}

/* ---- 板块「站上5日线 / 20日线」成分股占比（2026-10-20 owner：星空图下方多坐标轴）----
   一个 ECharts 实例里每个板块一个 grid（小多图），**共享 0~100 的 y 尺度** ⇒ 格子之间能直接
   对读"哪个板块在走强/走弱"，这比 25 张各自缩放的小图有意义得多。
   数据 = 后端 `load_sector_ma_breadth`（全池日线缓存后台扫一次 ≈6s，磁盘缓存按数据日复用；
   首次 `warming`，3s 短轮询）。与上方星轨图**同截止日、同窗口、同板块顺序**。 */
const SECMA_5 = "#388bfd";      // 站上5日线（蓝；dataviz 校验 dark：与橙 ΔE 32.1/35.6 全项过）
const SECMA_20 = "#e3b341";     // 站上20日线（琥珀）
let sectorMaInst = null, sectorMaNode = null, sectorMaSig = "";
let sectorMaData = null, sectorMaTimer = null, sectorMaInflight = false;

// 窗口宽度变化 ⇒ 列数/格子尺寸都变，必须**重算布局**（不是单纯 resize）。
// 抽稀后重画；数据没变时 setOption 很便宜（25 格 × 2 条线）。
let _sectorMaResizeTimer = null;
window.addEventListener("resize", () => {
  if (_sectorMaResizeTimer) clearTimeout(_sectorMaResizeTimer);
  _sectorMaResizeTimer = setTimeout(() => {
    _sectorMaResizeTimer = null;
    if (sectorMaData) renderSectorMaBreadth(sectorMaData);
  }, 300);
});

async function loadSectorMaBreadth(force) {
  const box = document.getElementById("echSectorMa");
  if (!box) return;
  if (sectorMaInflight) return;
  sectorMaInflight = true;
  let nextMs = 0;
  try {
    const d = await apiCall("load_sector_ma_breadth", 10, state.date || null);
    sectorMaData = d || null;
    renderSectorMaBreadth(d);
    if (d && d.warming) nextMs = 3000;          // 首次计算/数据日变了 → 短轮询
    else if (d && d.stale) nextMs = 60000;
  } catch (e) {
    const meta = document.getElementById("sectorMaMeta");
    if (meta) meta.innerHTML = `<span class="down">取数失败：${esc(e.message || e)}</span>`;
  } finally {
    sectorMaInflight = false;
    if (sectorMaTimer) clearTimeout(sectorMaTimer);
    sectorMaTimer = nextMs ? setTimeout(() => loadSectorMaBreadth(), nextMs) : null;
  }
}

function renderSectorMaBreadth(d) {
  const el = document.getElementById("echSectorMa");
  const meta = document.getElementById("sectorMaMeta");
  if (!el) return;
  const secs = (d && d.sectors) || [];
  if (!secs.length) {
    if (meta) {
      const pr = (d && d.progress) || {};
      meta.innerHTML = (d && d.warming)
        ? `⏳ 首次计算中…（全池有概念标签的个股，实测 6~8s）`
          + (pr.total ? `　<b>${pr.done || 0}/${pr.total}</b>` : "")
        : `<span class="down">${esc((d && d.error) || "暂无数据")}</span>`;
    }
    if (sectorMaInst) { try { sectorMaInst.dispose(); } catch (e) { } sectorMaInst = null; sectorMaNode = null; }
    sectorMaSig = "";
    el.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const w = el.clientWidth || 0;
  if (!w) return;        // 隐藏 tab：容器宽 0，等 switchTab 里重画（同星轨图）
  if (meta) {
    const cov = (d && d.coverage) || {};
    meta.innerHTML = `数据日 <b>${esc(d.latest_date || "—")}</b>`
      + ` · 截至 <b>${esc(state.date || d.latest_date || "—")}</b>（与上方星轨同截止日）`
      + ` · ${secs.length} 个板块 · 样本 <b>${cov.scanned || "—"}</b> 只（末行=最新交易日，停牌不进分母）`
      + (d.computed_at ? ` · 计算于 ${esc(d.computed_at)}` : "")
      + `<br>口径：站上 N 日线 = 当日收盘 &gt; N 日简单均线（含当日），与得分里 −70 悬崖 / 20日线观察列同源；`
      + `每只个股只归一个韭研主板块，不重复计数。`;
  }
  // 列数随宽度自适应；每格高度固定 132px（含标题），整体高度随之变化
  const cols = w >= 1200 ? 4 : (w >= 880 ? 3 : 2);
  const cellH = 132, padL = 30, padR = 10, padT = 34, padB = 18, topPad = 22;
  const rows = Math.ceil(secs.length / cols);
  const H = topPad + rows * cellH + 6;
  // 签名带**整条轨迹的粗略校验和**：后端重算后中间值可能变而末值不变，只比末值会漏重画
  const sig = secs.map(s => `${s.name}:${s.points.length}:`
    + s.points.reduce((a, p) => a + (p.pct5 || 0) + (p.pct20 || 0), 0).toFixed(1)).join("|")
    + `|${w}|${cols}`;
  if (sectorMaInst && sectorMaNode === el && sig === sectorMaSig) return;
  if (el.style.height !== H + "px") el.style.height = H + "px";
  const cellW = w / cols;
  const grids = [], xAxes = [], yAxes = [], series = [], titles = [];
  secs.forEach((s, i) => {
    const r = Math.floor(i / cols), c = i % cols;
    const x0 = c * cellW, y0 = topPad + r * cellH;
    grids.push({ left: x0 + padL, top: y0 + padT,
                 width: Math.max(40, cellW - padL - padR), height: cellH - padT - padB });
    xAxes.push({
      type: "category", gridIndex: i, boundaryGap: false,
      data: s.points.map(p => String(p.date).slice(5)),
      axisLine: { lineStyle: { color: "#30363d" } }, axisTick: { show: false },
      // 2026-10-20 owner：「横轴需要有日期」——**每一格都标**（原来只有最后一行标）。
      // 10 个点按 ~3 个间隔抽样，不至于糊成一片；鼠标所指的那天再由 axisPointer 的
      // 胶囊标签精确显示（snap 到最近的 K 线，见 tooltip.axisPointer）。
      axisLabel: { show: true, color: "#6e7681", fontSize: 8, hideOverlap: true,
        interval: Math.max(1, Math.ceil(s.points.length / 3) - 1) },
      axisPointer: { show: true, snap: true,
        label: { show: true, backgroundColor: "#30363d", borderColor: "#484f58",
                 borderWidth: 1, color: "#e6edf3", fontSize: 9, padding: [2, 5] } },
      splitLine: { show: false },
    });
    yAxes.push({
      type: "value", gridIndex: i, min: 0, max: 100, interval: 50,
      // 只在最左列标 0/50/100：所有格子共享同一尺度，标一次就够
      axisLabel: { show: c === 0, color: "#6e7681", fontSize: 8, formatter: "{value}" },
      axisLine: { show: false }, axisTick: { show: false },
      splitLine: { lineStyle: { color: "rgba(139,148,158,.14)" } },
    });
    titles.push({
      text: `${i + 1}. ${s.name}`,
      subtext: `5日 ${fmt(s.last5, 0)}% · 20日 ${fmt(s.last20, 0)}% · ${s.n || "—"}只`,
      left: x0 + padL - 2, top: y0 + 2,
      textStyle: { color: "#c9d1d9", fontSize: 11, fontWeight: 600 },
      subtextStyle: { color: "#8b949e", fontSize: 9 },
    });
    /* 2026-10-20 owner：「对应的天的竖线有交点，显示出具体的数值」。
       ① 竖线 = tooltip.axisPointer（line + snap，对齐到最近的 K 线）；
       ② 交点 = 每个数据点画实心小圆（原来 symbol:none 看不到交点），悬停时放大加白描边；
       ③ 具体数值 = axisPointer 的日期胶囊 + tooltip 里两项百分比 + 下方 y 轴读数（0~100 网格）。 */
    const mk = (vals, color, name) => ({
      name, type: "line", xAxisIndex: i, yAxisIndex: i,
      data: vals, smooth: 0.25, connectNulls: false,
      showSymbol: true, symbol: "circle", symbolSize: 3.2,
      lineStyle: { width: 1.5, color },
      itemStyle: { color, borderColor: "#0d1117", borderWidth: 1 },
      // 不加 focus:"series" —— 那会把另一条线在悬停时变暗，两条线的对比正是这张图的意义
      emphasis: { scale: 2.2,
                  itemStyle: { color, borderColor: "#fff", borderWidth: 1.5 } },
    });
    series.push(mk(s.points.map(p => p.pct5), SECMA_5, "站上5日线"));
    series.push(mk(s.points.map(p => p.pct20), SECMA_20, "站上20日线"));
  });
  if (!sectorMaInst || sectorMaNode !== el || !el.querySelector("canvas")) {
    if (sectorMaInst) { try { sectorMaInst.dispose(); } catch (e) { } }
    el.innerHTML = "";
    sectorMaInst = echarts.init(el);
    sectorMaNode = el;
  }
  sectorMaInst.setOption({
    backgroundColor: "transparent",
    animation: false,
    // 图例按**系列名**筛选 ⇒ 点一下可以把全部板块的 5日线/20日线一起关掉（不是 50 个条目）
    legend: { data: ["站上5日线", "站上20日线"], top: 2, right: 10,
      itemWidth: 14, itemHeight: 8, icon: "roundRect", itemGap: 12,
      textStyle: { color: "#8b949e", fontSize: 10 } },
    title: titles,
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    tooltip: {
      trigger: "axis",
      // 竖线对齐到最近的 K 线（snap），并在横轴上挂一个日期胶囊 —— 这是"对应那天的竖线"
      axisPointer: { type: "line", snap: true,
        lineStyle: { color: "#8b949e", type: "dashed", width: 1 },
        label: { show: true, backgroundColor: "#30363d", borderColor: "#484f58",
                 borderWidth: 1, color: "#e6edf3", fontSize: 10, padding: [2, 5] } },
      backgroundColor: "rgba(22,27,34,.96)", borderColor: "#30363d", borderWidth: 1,
      borderRadius: 8, textStyle: { color: "#e6edf3", fontSize: 11 },
      formatter: (ps) => {
        if (!ps || !ps.length) return "";
        const si = Math.floor((ps[0].seriesIndex || 0) / 2);   // 每板块 2 条系列
        const s = secs[si];
        if (!s) return "";
        const p = s.points[ps[0].dataIndex];
        if (!p) return "";
        return `<div style="font-weight:bold;margin-bottom:3px">${esc(s.name)} · ${esc(p.date)}</div>`
          + `<span style="color:${SECMA_5}">●</span> 站上5日线 <b>${fmt(p.pct5, 1)}%</b>`
          + `<br><span style="color:${SECMA_20}">●</span> 站上20日线 <b>${fmt(p.pct20, 1)}%</b>`
          + `<br><span style="opacity:.7">样本 ${p.n} 只（当日有行情）</span>`;
      },
    },
    series,
  }, { notMerge: true });
  sectorMaSig = sig;
  sectorMaInst.resize();
}

async function drawHunterGoChart() {
  const box = document.getElementById("hunterGoChart");
  if (!box) return;
  if (!window.echarts) { _hunterGoFail("ECharts 未加载"); return; }
  let d = null;
  try {
    d = await apiCall("load_hunter_go_history", 60);
  } catch (e) {
    _hunterGoFail(`接口调用失败（${e && e.message ? e.message : e}）· 若刚改过 t_gui.py 请重启它`);
    return;
  }
  if (document.getElementById("hunterGoChart") !== box) return;   // 期间被重建
  const days = (d && d.days) || [];
  if (!days.length) {
    // 无数据：计算中→显示进度并轮询；否则报错
    const pr = (d && d.progress) || {};
    if (d && d.computing) {
      _hunterGoFail(`⏳ 首次计算中 ${pr.done || 0}/${pr.total || 60} 天（全池逐日回算，约 20~25 分钟；算完自动出现）`);
      if (!_hunterGoTimer) {
        _hunterGoTimer = setInterval(() => { drawHunterGoChart(); }, 20000);
      }
      return;
    }
    _hunterGoFail((d && d.error) || "暂无数据");
    return;
  }
  if (_hunterGoTimer) { clearInterval(_hunterGoTimer); _hunterGoTimer = null; }

  _hunterGoDays = days;
  // 首次（或选中的那天已不在窗口内）默认展示**最新交易日**的明细
  if (!_hunterGoSel || !days.some(x => x.date === _hunterGoSel)) {
    selectHunterGoDay(days[days.length - 1].date);
  }

  const go = days.map(x => Number(x.go) || 0);
  const ma5 = hunterGoMa(go);
  const _isToday = (s) => s === todayStr();
  const sig = `${days.length}|${days[days.length - 1].date}|${go.join(",")}|${box.clientWidth}`;
  const _same = !!(hunterGoInst && box === hunterGoNode);
  if (_same && sig === hunterGoSig) {
    if (hunterGoInst.getWidth() !== box.clientWidth) {
      try { hunterGoInst.resize(); } catch (e) { }
    }
    return;
  }
  if (!box.clientWidth || !box.clientHeight) {
    // 本页未激活（宽 0）。**不记签名** ⇒ 切到本页时 switchTab 会再调一次。
    _hunterGoFail("⏳ 本页未激活，切到「概念评分排名」即自动绘制");
    return;
  }
  if (hunterGoInst && !_same) {
    try { hunterGoInst.dispose(); } catch (e) { }
    hunterGoInst = null; hunterGoNode = null;
  }
  if (!hunterGoInst) {
    hunterGoInst = echarts.init(box);
    hunterGoNode = box;
    // 点柱子 → 看那天"符合(GO)的是哪些票、各属什么板块"。
    // ⚠️ **只注册一次**：`setOption` 不会清事件，每次重画都 `on()` 会叠成 N 个处理器。
    hunterGoInst.on("click", (pt) => {
      const x = _hunterGoDays[pt && pt.dataIndex];
      if (x) selectHunterGoDay(x.date);
    });
  }
  hunterGoInst.setOption({
    grid: { left: 40, right: 34, top: 8, bottom: 18 },
    tooltip: {
      trigger: "axis",
      formatter: (ps) => {
        const x = days[ps[0].dataIndex];
        const i = ps[0].dataIndex;
        let s = `${x.date}${_isToday(x.date) ? "（今日，盘中数值会变）" : ""}<br>`
          + `建仓符合 <b>${go[i]}</b> 只 / 池内可算 ${x.n_ok || 0} 只<br>`
          + `<span style="opacity:.8">市场 ${HUNTER_GO_REGIME_CN[x.regime] || x.regime || "—"}</span>`;
        if (ma5[i] != null) s += `<br><span style="color:${HUNTER_GO_MA}">${HUNTER_GO_MA_N}日均</span> ${ma5[i].toFixed(1)} 只`;
        return s;
      },
    },
    xAxis: {
      type: "category", data: days.map(x => x.date.slice(5)),
      axisLabel: { fontSize: 9, interval: 9, color: "#6b7280" },
      axisTick: { show: false }, axisLine: { lineStyle: { color: "rgba(107,114,128,.3)" } },
    },
    yAxis: {
      type: "value", minInterval: 1,
      axisLabel: { fontSize: 9, color: "#6b7280" },
      splitLine: { lineStyle: { color: "rgba(107,114,128,.15)" } },
    },
    series: [
      { type: "bar", data: go, barMaxWidth: 12, itemStyle: { color: HUNTER_GO_BAR }, silent: true },
      {
        name: `${HUNTER_GO_MA_N}日均`, type: "line", data: ma5, smooth: false, symbol: "none",
        connectNulls: false, lineStyle: { width: 2, color: HUNTER_GO_MA },
        itemStyle: { color: HUNTER_GO_MA }, z: 3, silent: true,
        endLabel: {
          show: true, formatter: `MA${HUNTER_GO_MA_N}`,
          color: HUNTER_GO_MA, fontSize: 9, distance: 4,
        },
      },
    ],
  }, { notMerge: true });
  hunterGoSig = sig;
}

/* ---- 年限宽度：每日站上 365 日均线的股票占比（2026-10-18）----
   数据 = 后端 load_ma365_breadth（后台解析全池日线缓存 ≈10~20s，磁盘缓存按数据日复用）。
   柱=当日占比（红=较前日升、绿=降，A 股惯例）；蓝线=5 日均值；灰虚线=50% 参考。
   本面板**独立调度**（不挂在 renderLive 的 10s 重建里），未就绪 2.5s 短轮询、就绪 5 分钟一次。 */
let ma365Data = null, ma365Inst = null, ma365Node = null, ma365Sig = "";
let ma365Timer = null, ma365Inflight = false;
let ma365Range = "recent";        // "recent"=近120日（本地缓存现算）| "hist"=自2024-09-01（GM 补算档）
let ma365DataRange = null;        // 当前 ma365Data 属于哪一档
const MA365_UP = "#f85149", MA365_DOWN = "#3fb950", MA365_MA = "#388bfd";

function startMa365Breadth() {
  if (ma365Timer) clearTimeout(ma365Timer);
  ma365Timer = setTimeout(loadMa365Breadth, 600);
}

/* 区间切换：近120日（本地缓存现算，可能 10~20s）/ 自2024-09-01（GM 补算档，首次需脚本预计算） */
function setMa365Range(r) {
  ma365Range = r === "hist" ? "hist" : "recent";
  ma365Data = null;
  ma365DataRange = null;
  const a = document.getElementById("ma365RangeRecent");
  const b = document.getElementById("ma365RangeHist");
  if (a) a.classList.toggle("primary", ma365Range === "recent");
  if (b) b.classList.toggle("primary", ma365Range === "hist");
  const meta = document.getElementById("ma365Meta");
  if (meta) meta.textContent = ma365Range === "hist" ? "加载历史区间…" : "加载近120日…";
  // 站上20线占比与年限宽度**同一档区间**（owner 2026-10-20：周期保持一致）⇒ 一起给加载态
  const meta20 = document.getElementById("ma20BreadthMeta");
  if (meta20) meta20.textContent = ma365Range === "hist" ? "加载历史区间…" : "加载近120日…";
  if (ma365Timer) { clearTimeout(ma365Timer); ma365Timer = null; }
  loadMa365Breadth();
}

function _ma365MetaHtml(d, extra) {
  const last = (d && d.last) || {};
  const cov = (d && d.coverage) || {};
  const bits = [];
  if (d && d.historical && d.start) {
    bits.push(`区间 <b>${esc(d.start)} → ${esc(d.latest_date || "")}</b>`);
  } else if (d && d.latest_date) {
    bits.push(`数据日 <b>${esc(d.latest_date)}</b>`);
  }
  if (last && last.pct != null) {
    bits.push(`最新 <b>${fmt(last.pct, 2)}%</b>（${last.above}/${last.total} 只）`);
  }
  if (cov.cohort) {
    bits.push(`样本 <b>${cov.cohort}</b> 只（≥365日历史且有当日行情 / 缓存 ${cov.files || "—"} 只）`);
  } else if (cov.got) {
    bits.push(`样本 <b>${cov.got}</b> 只（GM 补算 ${cov.got}/${cov.requested || "—"}）`);
  }
  if (d && d.computed_at) bits.push(`计算于 ${esc(d.computed_at)}`);
  if (extra) bits.push(extra);
  return bits.join(" · ");
}

async function loadMa365Breadth() {
  if (ma365Inflight) return;
  const el = document.getElementById("ma365Chart");
  if (!el) return;
  ma365Inflight = true;
  let nextMs = 300000;
  try {
    const d = ma365Range === "hist"
      ? await apiCall("load_ma365_breadth", 0, "2024-09-01")
      : await apiCall("load_ma365_breadth", 120);
    if (d && d.available && (d.series || []).length) {
      ma365Data = d;
      ma365DataRange = ma365Range;
      renderMa365(d);
      renderMa20Breadth(d);      // 同一份 payload 的 series20（同区间、同刷新节奏）
      // `ma20_warming`：旧缓存没有 series20 / 历史档还没补算 ⇒ 5s 后再问，
      // 否则历史档的"计算中"要等一小时（正常历史档按 1 小时复用）。
      nextMs = d.ma20_warming ? 5000
        : (d.historical ? 3600000 : (d.stale ? 30000 : 300000));
    } else if (d && d.historical) {
      const meta = document.getElementById("ma365Meta");
      if (meta) meta.innerHTML = `<span class="down">${esc(d.error || "历史档未计算")}</span>`;
      renderMa20Breadth(d || {});
      nextMs = 3600000;
    } else if (d && d.warming) {
      const p = d.progress || {};
      const meta = document.getElementById("ma365Meta");
      if (meta) meta.innerHTML = `⏳ 首次计算中…（全部个股日线缓存约 5500 个，实测 10~20s）`
        + (p.total ? `　<b>${p.done || 0}/${p.total}</b>` : "");
      renderMa20Breadth(d || {});
      nextMs = 2500;
    } else {
      const meta = document.getElementById("ma365Meta");
      if (meta) meta.innerHTML = `<span class="down">年限宽度取数失败：${esc((d && d.error) || "未知")}</span>`;
      renderMa20Breadth(d || {});
      nextMs = 30000;
    }
  } catch (e) {
    nextMs = 30000;
  } finally {
    ma365Inflight = false;
    if (ma365Timer) clearTimeout(ma365Timer);
    ma365Timer = setTimeout(loadMa365Breadth, nextMs);
  }
}

function renderMa365(d) {
  const el = document.getElementById("ma365Chart");
  if (!el) return;
  const series = d.series || [];
  const last = d.last || series[series.length - 1] || {};
  const meta = document.getElementById("ma365Meta");
  if (meta) meta.innerHTML = _ma365MetaHtml(d);
  const sig = `${d.historical ? "H:" + (d.start || "") : "R"}|${d.latest_date}|${series.length}`
    + `|${last.date}|${last.pct}|${(d.coverage || {}).cohort || (d.coverage || {}).got || ""}`;
  const _canvasOk = !!(el.querySelector && el.querySelector("canvas"));
  if (ma365Inst && ma365Node === el && _canvasOk && sig === ma365Sig) return;
  if (ma365Inst && (ma365Node !== el || !_canvasOk)) {
    try { ma365Inst.dispose(); } catch (e) { }
    ma365Inst = null; ma365Node = null;
  }
  if (!ma365Inst) {
    el.innerHTML = "";
    ma365Inst = echarts.init(el);
    ma365Node = el;
  }
  ma365Sig = sig;
  ma365Inst.setOption(_breadthChartOption(series, "站上年限", "50%=一半股票站上年限"));
  ma365Inst.resize();
}

/* 宽度图共用骨架（年限宽度 / 站上20线占比 2026-10-20）：
   柱=每日占比（较前日升=红、降=绿，A 股惯例）· 蓝线=5 日均值 · 灰虚线=50% 参考。
   `label` 进 tooltip 与图例（"站上年限" / "站上20日线"），`note` 是 5 日均值那行的尾注。
   ⚠️ 两张图必须同骨架同配色 —— 它们是同一套指标的不同窗口，视觉上要能直接对读。 */
function _breadthChartOption(series, label, note) {
  const dates = series.map(x => String(x.date).slice(5));
  const bars = series.map((x, i) => {
    const prev = i > 0 ? series[i - 1].pct : x.pct;
    return { value: x.pct,
      itemStyle: { color: x.pct >= prev ? MA365_UP : MA365_DOWN, borderRadius: [3, 3, 0, 0] } };
  });
  const ma5 = series.map((x, i) => {
    const w = series.slice(Math.max(0, i - 4), i + 1);
    return +(w.reduce((a, b) => a + (b.pct || 0), 0) / w.length).toFixed(2);
  });
  return {
    backgroundColor: "transparent",
    animation: false,
    grid: { left: 46, right: 16, top: 26, bottom: 24 },
    legend: { top: 0, left: 6, itemGap: 10, itemWidth: 14, itemHeight: 8,
      textStyle: { color: "#8b949e", fontSize: 10 } },
    tooltip: {
      trigger: "axis", axisPointer: { type: "shadow" },
      backgroundColor: "rgba(22,27,34,.96)", borderColor: "#30363d", borderWidth: 1,
      borderRadius: 8, textStyle: { color: "#e6edf3", fontSize: 11 },
      formatter: (ps) => {
        const i = ps && ps[0] ? ps[0].dataIndex : -1;
        const x = series[i];
        if (!x) return "";
        const prev = i > 0 ? series[i - 1] : null;
        const chg = prev && prev.pct != null ? x.pct - prev.pct : null;
        return `<div style="font-weight:bold;margin-bottom:3px">${esc(x.date)}</div>`
          + `${label} <b style="color:${x.pct >= (prev ? prev.pct : x.pct) ? MA365_UP : MA365_DOWN}">${fmt(x.pct, 2)}%</b>`
          + `<br>站上 ${x.above} / ${x.total} 只`
          + (chg != null ? `<br>较前日 ${chg >= 0 ? "+" : ""}${chg.toFixed(2)}pp` : "")
          + `<br><span style="color:#8b949e">5日均值 ${ma5[i]}% · ${note}</span>`;
      },
    },
    xAxis: {
      type: "category", data: dates,
      axisLine: { lineStyle: { color: "#e4e8ef" } },
      axisLabel: { color: "#8b949e", fontSize: 9,
        interval: Math.max(0, Math.ceil(dates.length / 12) - 1) },
    },
    yAxis: {
      type: "value", min: 0, max: 100,
      axisLabel: { color: "#8b949e", fontSize: 9, formatter: "{value}%" },
      splitLine: { lineStyle: { color: "rgba(139,148,158,.2)" } },
    },
    series: [
      { name: `${label}占比`, type: "bar", data: bars,
        barMaxWidth: series.length > 300 ? 7 : 12, barCategoryGap: "25%" },
      { name: "5日均值", type: "line", data: ma5, symbol: "none", connectNulls: true,
        lineStyle: { width: 1.5, color: MA365_MA }, itemStyle: { color: MA365_MA } },
      { name: "50%参考", type: "line", data: [], silent: true,
        markLine: { symbol: "none", silent: true,
          data: [{ yAxis: 50, lineStyle: { color: "#8b949e", type: "dashed", width: 1 },
                   label: { formatter: "50%", fontSize: 9, color: "#8b949e",
                            position: "insideEndTop" } }] } },
    ],
  };
}

/* ---- 站上20线公司占比（2026-10-20 owner：「在年限宽度下方加上 站上20线公司占比，
   周期和年限宽度保持一致」）----
   数据同源：`load_ma365_breadth` 一次调用同时回 `series`(MA365) 与 `series20`(MA20)
   ⇒ 区间档位（近120日/自2024-09-01）与刷新节奏天然与上方那张一致，也不会为多一张图
   再把 5500 个日线缓存扫第二遍。样本口径与年限宽度**同一批**（≥365 根且末行=最新交易日），
   两张图的分母因此可直接对读。 */
let ma20Inst = null, ma20Node = null, ma20Sig = "";

function _ma20MetaHtml(d, series, last) {
  const bits = [];
  if (d && d.historical && d.start) {
    bits.push(`区间 <b>${esc(d.start)} → ${esc((last && last.date) || d.latest_date || "")}</b>`);
  } else if (d && d.latest_date) {
    bits.push(`数据日 <b>${esc(d.latest_date)}</b>`);
  }
  if (last && last.pct != null) {
    bits.push(`最新 <b>${fmt(last.pct, 2)}%</b>（${last.above}/${last.total} 只）`);
  }
  const cov = (d && d.coverage) || {};
  if (cov.cohort) bits.push(`样本 <b>${cov.cohort}</b> 只（与年限宽度同一批）`);
  else if (cov.got) bits.push(`样本 <b>${cov.got}</b> 只`);
  if (d && d.computed_at) bits.push(`计算于 ${esc(d.computed_at)}`);
  bits.push("区间随上方「年限宽度」");
  return bits.join(" · ");
}

function renderMa20Breadth(d) {
  const el = document.getElementById("ma20BreadthChart");
  const meta = document.getElementById("ma20BreadthMeta");
  if (!el) return;
  const series = (d && d.series20) || [];
  if (!series.length) {
    const _warming = !!(d && (d.ma20_warming || d.warming));
    if (meta) {
      meta.innerHTML = _warming
        ? "⏳ 计算中：与「年限宽度」同一次后台扫描（全池日线缓存，约 10~20s），算完自动出现"
        : `<span class="down">${esc((d && d.error) || "暂无数据")}</span>`;
    }
    if (ma20Inst) { try { ma20Inst.dispose(); } catch (e) { } ma20Inst = null; ma20Node = null; }
    ma20Sig = "";
    el.innerHTML = _warming ? '<div class="empty">⏳ 计算中…</div>'
      : '<div class="empty">暂无数据</div>';
    return;
  }
  const last = (d && d.last20) || series[series.length - 1] || {};
  if (meta) meta.innerHTML = _ma20MetaHtml(d, series, last);
  const sig = `${d.historical ? "H:" + (d.start || "") : "R"}|${d.latest_date}|${series.length}`
    + `|${last.date}|${last.pct}`;
  const _canvasOk = !!(el.querySelector && el.querySelector("canvas"));
  if (ma20Inst && ma20Node === el && _canvasOk && sig === ma20Sig) return;
  if (ma20Inst && (ma20Node !== el || !_canvasOk)) {
    try { ma20Inst.dispose(); } catch (e) { }
    ma20Inst = null; ma20Node = null;
  }
  if (!ma20Inst) {
    el.innerHTML = "";
    ma20Inst = echarts.init(el);
    ma20Node = el;
  }
  ma20Sig = sig;
  ma20Inst.setOption(_breadthChartOption(series, "站上20日线", "50%=一半股票站上20日线"));
  ma20Inst.resize();
}

/* ---- 盘中实时 ---- */
/* 「无趋势状态」不能只是一句空话：`intraday_state.json` 由 core/signal_engine 写，
   2026-09-14 之后引擎没再跑过 ⇒ 文件一直停在旧日期。这里把**源文件的日期**摆出来，
   一眼分清"今天没趋势"还是"数据源已经一个月没更新"。 */
function _trendEmptyHtml(st) {
  const d = (st && st.date) ? String(st.date) : "";
  if (!d) return '<div class="empty">无趋势状态（intraday_state.json 为空）</div>';
  let days = null;
  try {
    const t = new Date(d + "T00:00:00").getTime();
    if (!isNaN(t)) days = Math.floor((Date.now() - t) / 86400000);
  } catch (e) { /* 忽略 */ }
  const stale = days != null && days > 1;
  return `<div class="empty">无趋势状态${stale
    ? `<span class="cell-dim" style="font-size:11px">　（intraday_state.json 最后更新 ${esc(d)}，`
      + `已 ${days} 天未刷新 —— 盘中状态引擎未运行）</span>`
    : ""}</div>`;
}

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

  // ── 闪烁修复（2026-10-15）────────────────────────────────────────────────
  // 下面这句 innerHTML 每 10s 把整卡重建，`#turnoverHistChart` 每次都是**新节点**，
  // 而 ECharts 实例绑在节点上 ⇒ 每次都得 dispose+init，容器里的 canvas 被拆掉再建 = 肉眼可见的闪。
  // 做法：先把图节点**摘下来**（脱离文档但对象活着、实例也活着），重建后原样**放回占位处**。
  // 这样 10s 一轮里节点与实例都不换，drawTurnoverHist 走"同节点 → 原地 setOption"分支，不闪。
  // 与 2026-10-13「市场风向标搬出 #liveBody」是同一病根，这里用了代价更小的保守修法。
  const _keepChart = _keepNode("turnoverHistChart");

  el.innerHTML = `
    <div class="card">
      <div class="card-title">盘中状态
        <span class="cell-dim" style="font-weight:normal;font-size:11px">上：近1年全市场成交额 · 下：个股趋势（intraday_state.json · 10s 刷新）</span>
      </div>
      <div id="turnoverHistMsg" class="cell-dim" style="font-size:11px"></div>
      <div id="turnoverHistSlot"></div>
      <div class="cell-dim" style="font-size:10px;margin:-2px 0 8px">
        <span style="color:${TURNOVER_UP}">■</span> 放量（较前一交易日）
        <span style="color:${TURNOVER_DOWN};margin-left:8px">■</span> 缩量
        <span style="color:${TURNOVER_LIVE};margin-left:8px">■</span> 今日进行中（不判放缩）
        <span style="color:${TURNOVER_MA5};margin-left:8px">━</span> ${TURNOVER_MA5_N}日均量（不含今日）
        <span id="turnoverBseNote"></span>
      </div>
      ${trendCards || _trendEmptyHtml(liveState)}
    </div>`;

  // 把（上面摘下来的）图节点放回占位处。首次渲染没有旧节点 ⇒ 就地新建一个同 id 的容器。
  _putBackNode("turnoverHistSlot", _keepChart, '<div id="turnoverHistChart" style="height:130px"></div>');
  // 近1年成交额柱状图，容器节点已复用 ⇒ 同节点走原地 setOption，不再每次 dispose+init
  drawTurnoverHist();

  // 🧭 市场风向标已**搬出** #liveBody（独立 section，自带调度）—— 原来长在这里，
  //   被 refreshLive 每 10s 整体覆写 ⇒ 点开板块后一直跳闪（2026-10-13 实测）。

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
    alertKind = "JIACANG";
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
  // 「完成后才排下一次」（2026-10-10，见 _startStaggered 的说明）：pollSignals 是 async，
  // setInterval 不 await，投递慢时会并发叠加（实测 20s ÷ 5s = 叠 4 个）。
  const tick = async () => {
    try {
      // 上限保护同 _startStaggered
      await Promise.race([pollSignals(), new Promise(r => setTimeout(r, 30000))]);
    } catch (e) { /* 静默 */ }
    if (sigTimer !== null) sigTimer = setTimeout(tick, 5000);
  };
  sigTimer = setTimeout(tick, 5000);
}
function stopSignalPoll() {
  if (sigTimer) { clearTimeout(sigTimer); sigTimer = null; }
}

/* 2026-10-18：休眠/长时间挂起恢复（或 JS 主线程被饿死 >60s）后，所有 setTimeout 会**同时到期**
   ⇒ live/console/signal/beacon/hunter 十几个接口在 1 秒内一起打到 pywebview 桥上，
   而 `evaluate_js` 是串行的 ⇒ 恢复瞬间反而更像"卡死"。
   处置：重新起一遍各轮询（先 stop 再 start 会清掉旧定时器、按原相位重新错开），
   风向标延后 2.5s 再拉，别和恢复瞬间的 UI 重建抢。 */
let _lastWakeRecovery = 0;
function _onWakeFromSleep(gapMs) {
  const now = Date.now();
  if (now - _lastWakeRecovery < 30000) return;      // 防抖：每次休眠只处理一次
  _lastWakeRecovery = now;
  try {
    // 实时轮询只在看"今天"时存在（历史日视图 loadAndRender 会 stopLivePoll）
    if (state.date === todayStr()) {
      stopLivePoll(); startLivePoll();
      stopSignalPoll(); startSignalPoll();
    }
    stopPoll(); startPoll();
    if (_beaconTimer) { clearTimeout(_beaconTimer); _beaconTimer = null; }
    setTimeout(() => { try { loadMarketBeacon(true); } catch (e) { /* 静默 */ } }, 2500);
    if (stockChartCode) startStockChartLive();
    statusEl(`已从休眠/挂起恢复（间隔 ${Math.round(gapMs / 1000)}s）· 已重新错峰轮询`, "ok");
  } catch (e) { /* 静默 */ }
}
async function pollSignals() {
  const date = state.date;
  if (!date) return;
  try {
    // 建仓/加仓信号（position_builder intraday signal）
    // （盘中做T信号轮询 poll_new_signals 已随 manual 做T 于 2026-09-14 下线）
    const p = await apiCall("poll_new_position_signals", date);
    if (p && !p.baseline && p.signals && p.signals.length) {
      p.signals.forEach(pushAlert);
    }
  } catch (e) { /* 静默 */ }
}

/* ---- ② 图表 ---- */

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
    // 从 accountsDetail 获取账户 broker 信息
    const acctDetail = state.accountsDetail && state.accountsDetail[s.account] ? state.accountsDetail[s.account] : {};
    const brokerTip = acctDetail.broker ? `title="${esc(acctDetail.broker)}"` : "";
    return `
      <tr>
        <td>${esc(nm)} <span class="mono cell-dim">${esc(code)}</span> ${tmBadge}</td>
        <td class="cell-dim" ${brokerTip}>${esc(s.account || "")} ${esc(s.type || "")}</td>
        <td class="num">${fmt(s.qty, 0)}</td>
        <td class="num"><span class="hl">${fmt(s.base, 0)}</span></td>
        <td class="num cell-dim">${fmt(prev.base, 0)}</td>
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
          <th>股票</th><th>账户/类型</th><th class="num">持股</th><th class="num">目标底仓</th>
          <th class="num">前日底仓</th><th class="num">K3漂移</th><th>归因</th>
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
      <td class="num" title="可T仓位：股数×ρ；ρ=clamp(风险预算R/近250日median日内振幅,15%,50%)，R 见 config.t_budget_risk_pct（2026-09-11 实验口径）">
        <b>${fmt(r.t_suggest, 0)}</b>股<br>
        <span class="mono cell-dim" style="font-size:10px">ρ${((r.t_ratio || 0.3) * 100).toFixed(0)}% · 振幅${((r.t_amp || 0) * 100).toFixed(1)}%</span></td>
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
        <th class="num" title="可T仓位=股数×ρ；ρ 由风险预算 R/日内振幅 定（实验）">可T仓位</th>
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

/* ---- ⑥ 建仓扫描 ---- */
/* 仓位结构列（2026-10-11）：**建仓表 / 持仓体检 / 猎手板块个股 共用同一真源与同一渲染**
   （后端 `r.structure` / `s.structure`，均由 `_pb_struct_get` 出）。
   ⚠️ 是**结构参考、不是买卖信号**——机械按「阶段2加仓/阶段3减仓」做仓位择时经实测无正贡献；
   「破前低就清仓」−30pp 强有害；唯一有证据的动作是「破位后在**下档支撑**挂限价补仓」
   （改善成本结构、不提高期望）。tooltip = 后端 `format_structure` 全文（单一真源）。 */
function structSummaryHtml(st) {
  st = st || {};
  if (st.pending || (!st.ok && !st.reason)) {
    return `<span class="cell-dim" title="结构层后台计算中（整池首轮约10~30s，之后按30min时段刷新）">⏳</span>`;
  }
  if (!st.ok) {
    return `<span class="cell-dim" title="${esc(st.reason || "结构层不可用")}">—</span>`;
  }
  const broke = !!st.broke_stop;
  const badge = broke
    ? (st.refill_valid ? `<span class="badge signal">⚠️已破位</span>`
                       : `<span class="badge weak">已破位·超期</span>`)
    : `<span class="badge hold">持有</span>`;
  const stop = (st.stop_level == null) ? "—" : st.stop_level;
  const disc = (st.discount_pct == null) ? "" : `｜折价${st.discount_pct}%`;
  const stale = (st.data_asof && st.data_asof.stale)
    ? ` <span style="color:#ffb454" title="${esc(st.data_asof.stale_reason || "")}">⚠️陈旧</span>` : "";
  return `${badge}<div class="cell-dim" style="font-size:11px">止损${esc(String(stop))}｜${esc(st.stage10 || "")}｜30m${esc(st.mid_trend_30 || "")}${esc(disc)}${stale}</div>`;
}
function structCellHtml(st) {
  return `<td style="text-align:center;line-height:1.6" title="${esc((st || {}).text || "仓位结构不可用")}">${structSummaryHtml(st)}</td>`;
}

/* 30min MA5 状态（**纯展示列**，owner 要求看盘感）。
   ⚠️ **实测无前瞻信息量**：977只×361日，状态/跃迁 × 站上/跌破 四个口径，在「同日同 bar-of-day」
   对照下 |diff| ≤ 0.03pp、|t| ≤ 2.4（n=35万~130万）。朴素对照给出的「−0.343pp t=−25.5」是
   **日内时段伪影**，勿采信。⇒ 本列**不参与任何判定/评分/择时**，只是把原始状态摆出来。
   依据 doc/research/2026-10-11_30min站上MA5_作仓位依据实验.md */
const MA5_WARN = "⚠️ 本项实测无前瞻信息量（状态与跃迁、站上与否四个口径，同日同bar-of-day对照下 |diff| ≤ 0.03pp、|t| ≤ 2.4）⇒ 仅供参考，不参与任何判定。依据：doc/research/2026-10-11_30min站上MA5_作仓位依据实验.md";
function ma5CellHtml(st, cls) {
  st = st || {};
  const _c = cls ? ` class="${cls}"` : "";
  if (st.pending || (!st.ok && !st.reason)) {
    return `<td${_c} style="text-align:center"><span class="cell-dim" title="结构层后台计算中">⏳</span></td>`;
  }
  if (!st.ok || st.ma5_30min == null) {
    return `<td${_c} style="text-align:center" title="${esc((st.reason || "无 30min 数据") + "　" + MA5_WARN)}"><span class="cell-dim">—</span></td>`;
  }
  const up = !!st.above_ma5_30;
  const n = st.bars_since_ma5_cross;
  const title = `30min MA5 = ${st.ma5_30min}　现价${up ? "站上" : "跌破"}${n == null ? "" : `，距上次翻转 ${n} 根 30min`}。　${MA5_WARN}`;
  return `<td${_c} style="text-align:center;line-height:1.5" title="${esc(title)}"><b class="${up ? "up" : "down"}">${up ? "站上" : "跌破"}</b><div class="cell-dim" style="font-size:10px">${n == null ? "" : n + "根"}</div></td>`;
}

/* 日线四阶段（2026-10-11）：摆**状态**，紧挨「得分/通过」便于对照。
   ⚠️ 实测横截面 IC **与直觉相反**：②抬升 IC −0.031~−0.042(t=−12~−15)、
   ④下跌 IC +0.020~+0.034(t=+7~+12) —— 抬升/追高前瞻更差、下跌/超跌前瞻更好，分半窗两段同号。
   效应量小（IC≈0.04，低于往返成本）⇒ 只作相对强弱参照，不是独立交易依据。
   依据 doc/research/2026-10-11_日线四阶段_得分区分度实验.md */
const DAY_STAGE_TIP = "日线四阶段（描述状态）：②抬升=多头排列(收盘>MA20>MA60)且MA20上行；③派发=MA20>MA60但收盘丢了MA20；④下跌=空头排列且MA20下行；①蓄势=其余（均线缠绕）。"
  + "⚠️ 实测(977只×2265日/163万股票·日)横截面IC与直觉相反：②抬升 −0.031~−0.042(t=−12~−15)、④下跌 +0.020~+0.034(t=+7~+12)，分半窗两段同号；效应量小(IC≈0.04)，仅作相对强弱参照。";
function dayStageCellHtml(st) {
  st = st || {};
  if (st.pending || (!st.ok && !st.reason)) {
    return `<td style="text-align:center"><span class="cell-dim" title="结构层后台计算中">⏳</span></td>`;
  }
  if (!st.ok || !st.stage_daily) {
    return `<td style="text-align:center" title="${esc((st.reason || "日线数据不足") + " " + DAY_STAGE_TIP)}"><span class="cell-dim">—</span></td>`;
  }
  return `<td style="text-align:center" title="${esc(st.stage_daily + "。" + DAY_STAGE_TIP)}"><span style="font-size:12px">${esc(st.stage_daily)}</span></td>`;
}

/* 实时涨幅（2026-10-12，owner 要求）：**建仓表** 与 **猎手个股行** 共用同一渲染。
   两处数据源不同（建仓表=load_quotes 每 10s 叠加；猎手=后端本轮行情快照的 change_pct），
   但展示口径必须一致 ⇒ 收成一个 helper，别各写一份。
   ⚠️ 取不到必须显示 "—" 而不是 0.00%：本仓踩过"把无数据画成平盘"的坑
      （盘前假当日根就让 600362 误报破 5 日线），涨跌幅尤其不能猜。 */
function pctHtml(v, tip) {
  if (v === null || v === undefined || v === "" || isNaN(Number(v))) {
    return `<span class="cell-dim" title="${esc(tip ? tip + "；本行暂未取到" : "暂未取到")}">—</span>`;
  }
  const n = Number(v);
  const cls = n > 0.05 ? "up" : n < -0.05 ? "down" : "cell-dim";
  // ⚠️ 用 toFixed(2) 而**不是** `fmt(n,2)`：后者是 `maximumFractionDigits`（最多两位），
  //    会把 +3.50% 打成 +3.5%、0 打成 +0%。涨跌幅一列必须**定宽两位**，否则数字对不齐、
  //    且 0 与 0.00 看起来像两种东西（冒烟测试实测踩到）。
  const txt = `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
  return tip ? `<span class="${cls}" title="${esc(tip)}">${txt}</span>`
             : `<span class="${cls}">${txt}</span>`;
}

/* 「5日线」观察列（2026-10-11，owner 要求）：显示**得分**里那个 −70 扣分的触发标志，
   便于看"被砍到 0 的那批票后来怎么样"——它是决定是否保留该扣分的观察依据。
   ⚠️ 实测（977只×2265日）「站上MA5」的横截面 IC 为**负**（−0.018, t=−6.9，两半窗一致），
   即被扣分的那批（未站上）后续反而略好。扣分按 owner 决定**暂不修改**。 */
/* 【20日线观察】（2026-10-14 owner：由「5日线观察」改口径）。
   ⚠️ **刻意不显示 −70**：得分里那个 −70 悬崖扣分走的是 **5 日线**（owner 决定保留），
   与本列看的 20 日线**不是同一条线** —— 挂在一起就是误导。 */
const MA20_OBS_TIP = "是否站上 20 日线。**观察列，不驱动任何判定**。"
  + "⚠️ 「得分」里那个 −70 悬崖扣分走的是 **5 日线**口径（owner 决定保留），与本列无关，故本列不显示 −70。";
/* `detail`（可选，2026-10-14）：把 MA20 具体数值等明细挂到**所有**分支的 tooltip 尾部。
   持仓体检列要显示 MA20 现值；建仓表/猎手表不传 → 行为与原来完全一致。 */
/* 【均线状态列·通用】（2026-10-19 起先只做 5 日线；2026-10-20 owner：选股猎手该列改用 **20 日线**）。
   口径：`v` 决定颜色/文字（站上=绿、未站上=`belowColor`、无数据=— 不猜），
   `otherV` 只是**另一条**均线的状态，降级进 tooltip、不占列。
   ⚠️ 得分里那个 −70 悬崖走的是 **5 日线**；本列看哪条线由调用方传 `n` 决定，别在 tooltip 里
   把两条线说成同源（那正是误导）。 */
function maStateCellHtml(v, n, otherV, otherN, missingTip, belowColor, belowText) {
  const _bc = belowColor || "#f85149";
  const _bt = belowText || "破位";
  const _o = (otherV === true) ? "站上" : (otherV === false) ? "破位" : "—";
  const _tip = `现价 vs ${n} 日简单均线。${otherN} 日线：${_o}。观察列，不参与其它判定。`;
  if (v === true) return `<td style="text-align:center" title="${esc("站上 " + n + " 日线。" + _tip)}"><b style="color:#3fb950">站上</b></td>`;
  if (v === false) return `<td style="text-align:center" title="${esc("未站上 " + n + " 日线。" + _tip)}"><b style="color:${_bc}">${_bt}</b></td>`;
  return `<td style="text-align:center" title="${esc(missingTip || "无该特征（旧数据/未扫描）")}"><span class="cell-dim">—</span></td>`;
}

function ma20FlagCellHtml(v, missingTip, detail) {
  const _d = detail ? "　" + detail + "。" : "";
  if (v === true) return `<td style="text-align:center" title="${esc("站上 20 日线。" + _d + MA20_OBS_TIP)}"><span class="cell-dim">站上</span></td>`;
  if (v === false) return `<td style="text-align:center" title="${esc("未站上 20 日线。" + _d + MA20_OBS_TIP)}"><b class="down">破位</b></td>`;
  return `<td style="text-align:center" title="${esc(missingTip || "无该特征（旧数据/未扫描）")}"><span class="cell-dim">—</span></td>`;
}

/* 时机条件列（2026-10-16 owner：「选股猎手的时机条件列与建仓信号扫描的该列保持一致」）。
   **建仓信号扫描 / 选股猎手 共用本渲染**（同 `structCellHtml`/`dayStageCellHtml`/`pctHtml` 的做法），
   别再各写一份 —— 此前猎手侧**硬编码只列 3 个条件键**，与真源脱钩，代价是：
     · 10-15 给真源 `t_gui.COND_LABELS` 加了 `t_ma20`/`t_ma5` 两条**硬约束**（正是"解释 NO-GO
       为什么 NO-GO"的两条），猎手侧漏改 ⇒ **同一只票在猎手表里 4 个圆点、在建仓表里 7 个**；
     · tooltip 也不同源（猎手拿 `build_reason` 当 title，与建仓表的逐条「通过/未过」不是一回事）。
   `labels` 一律传**后端下发的 `cond_labels`**（`t_gui.COND_LABELS` 单一真源），前端不再维护副本。
   `isNoData` ⇒ 整格 `—`：**数据不足 ≠ 全部未通过**（画成 6 个空心圆是在编造结论）。 */
function condCellHtml(conds, labels, isNoData, vetoList) {
  const _c = conds || {};
  const _keys = Object.keys(labels || {});
  if (isNoData) {
    return `<td><span class="cond" title="数据不足：未参与条件判定">—</span></td>`;
  }
  const dots = _keys.map(k =>
    _c[k] === undefined ? `<span class="cell-dim">·</span>`
      : _c[k] ? `<span class="on">●</span>` : `<span class="off">○</span>`).join("");
  const vetoed = _c.t_veto === false;
  const vetoDot = vetoed
    ? `<span title="否决因子触发：${esc((vetoList || []).join("、") || "爆量/偏离MA60")}">🚫</span>`
    : `<span class="cell-dim" title="否决因子（爆量≥3倍20日均量 / 偏离MA60>+20%）：未触发">·</span>`;
  const title = _keys.map(k => `${labels[k]}:${_c[k] ? "通过" : "未过"}`).join(" · ")
    + ` · 否决因子:${vetoed ? "触发" : "未触发"}`;
  return `<td><span class="cond" title="${esc(title)}">${dots}${vetoDot}</span></td>`;
}


/* 板块内 龙头/中军/杂毛（2026-10-11，owner 需求）：**描述性方位标签**，不是信号。
   规则：龙头=板块内当日涨幅第1；中军=其余成交额前3（资金核心）；杂毛=其余；板块<8只不标。
   ⚠️ **实测无前瞻区分度**（977池×2009日，行业口径、同板块同日配对、按日聚类 t、分半窗）：
      涨幅头名 vs 杂毛 H=5 仅 −0.021pp(t=−0.40)；成交额头名 **−0.302pp(t=−5.7)**；
      连板龙头 H=1 −0.112pp(t=−1.71)；**分半窗符号翻转**(h1 +0.282 / h2 −0.217)。
      效应量 ±0.05~0.13pp 远低于往返成本 ⇒ 只看方位，勿当依据。
   依据 doc/research/2026-10-11_板块内龙头中军杂毛_分层实验.md */
/* 「还在更新」的统一标记（2026-10-11，owner：展开板块后各列空着，不知道是不是在更新）。
   ⚠️ 必须与「确实没有数据」区分开：前者显示 ⏳ 且带说明，后者才是 "—"。 */
const UPDATING_HTML = '<span class="cell-dim" style="font-size:11px" title="正在更新…（数据尚未返回，稍后自动填上）">⏳</span>';

const TIER_TIP = "板块内方位标签（描述性，非信号）：龙头=本板块当日涨幅第1；中军=其余成交额前3（资金核心）；杂毛=其余。"
  + "⚠️ 实测该分层**无前瞻区分度**（四种定义×四个持有期≈全无差异，分半窗符号翻转，效应量远低于交易成本）⇒ 仅供盘面方位感，不参与任何判定与排序。";
// 2026-10-20：静态 龙头/中军（后端 tier_static，机器人/军工板块整治的研究定性标签）。
// ◆ 前缀 + tooltip 带定性理由，与动态当日分层徽章区分；静态标签与当日涨跌无关。
const TIER_STATIC_TIP = "研究定性标签（2026-10-20 板块整治，机器人/军工）：按产业链地位核实，与当日涨跌无关。";
function tierBadge(t, isStatic, reason) {
  const st = isStatic ? "◆" : "";
  const tip = isStatic
    ? `【研究定性·${t}】${reason ? reason + "。" : ""}${TIER_STATIC_TIP}`
    : `【${t}】${TIER_TIP}`;
  if (t === "龙头") return `<span style="display:inline-block;padding:1px 8px;border-radius:10px;background:rgba(248,81,73,.22);border:1px solid #f85149;color:#ff9d95;font-weight:700;font-size:11px;letter-spacing:1px" title="${esc(tip)}">${st}龙头</span>`;
  if (t === "中军") return `<span style="display:inline-block;padding:1px 8px;border-radius:10px;background:rgba(88,166,255,.20);border:1px solid #58a6ff;color:#9ecbff;font-weight:700;font-size:11px;letter-spacing:1px" title="${esc(tip)}">${st}中军</span>`;
  if (t === "杂毛") return `<span style="display:inline-block;padding:1px 7px;border-radius:10px;background:rgba(139,148,158,.13);color:#8b949e;font-size:10px" title="${esc(tip)}">杂毛</span>`;
  return '<span class="cell-dim">—</span>';
}
/* 整行强调：龙头/中军用左侧色条 + 淡底色 —— 扫一列股票时能一眼挑出来 */
function tierRowStyle(t) {
  if (t === "龙头") return "box-shadow:inset 4px 0 0 #f85149;background:rgba(248,81,73,.10);";
  if (t === "中军") return "box-shadow:inset 3px 0 0 #58a6ff;background:rgba(88,166,255,.07);";
  return "";
}

/* 把实时行情叠加到建仓表行（价 + 涨幅）。**两处都必须调**：
   ① 10s 的 `load_quotes` 路径；② `refresh_pb` 回来的新 payload —— 后者会**整体替换**
   `state.payload.position_builder`，不叠的话刚填上的实时值会被下一次渲染洗掉（本文件既有
   同类教训：技术标签曾因同样的"覆盖再渲染"一会儿有一会儿无，见 refresh_pb 处注释）。
   ⚠️ 不新增涨跌幅到 scan trace：那是扫描时刻的快照，与"实时"语义不同，两者别混。 */
function applyQuotesToPB(pb) {
  const qs = state.quotes || [];
  if (!pb || !pb.rows || !qs.length) return pb;
  const pxMap = {};
  qs.forEach(x => { pxMap[x.code] = { price: x.price, pct: x.change_pct }; });
  pb.rows.forEach(r => {
    const q = pxMap[r.code];
    if (!q) return;
    if (q.price != null) r.price = q.price;
    r.change_pct = q.pct;
  });
  return pb;
}

/* 持仓体检表「现价」列的 10s 级 DOM 叠加（2026-10-13）：OB 表自身 45~60s 才整体重拉一次，
   现价原先冻结到手动「🔄 刷新」。数据本来就在 `state.quotes`（10s live 轮询顺手拉的），
   直接 patching 单元格，零后端成本。单元格由 renderOB 打 data-obcode 标记。 */
function applyQuotesToOB() {
  const qs = state.quotes || [];
  if (!qs.length) return;
  const pxMap = {};
  qs.forEach(x => { if (x.price != null) pxMap[x.code] = x.price; });
  document.querySelectorAll("#obBody td[data-obcode]").forEach(td => {
    const px = pxMap[String(td.getAttribute("data-obcode") || "").split("_")[0]];
    if (px != null) td.textContent = fmt(px, 2);
  });
}

function renderPB(pb) {
  window._pbLastPayload = pb;  // P3-2 池筛选：切换下拉时用最近一次 payload 重渲染
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
  // P3-2 池分管：按下拉筛选（全部/manual/auto），未标注按 manual
  const _poolFilter = window._pbPoolFilter || "all";
  const srcRows = (pb.rows || []).filter(
    r => _poolFilter === "all" || (r.pool || "manual") === _poolFilter);
  const rows = srcRows.map(r => {
    const poolBadge = (r.pool === "auto")
      ? `<span class="badge" style="background:#2a4a5a;color:#8ad4ff" title="自动盘池（goldminer auto 侧管理，manual 不扫描）">自动</span>`
      : `<span class="badge" style="background:#3a3a3a;color:#bbb" title="人工池（superTrader manual 侧管理）">人工</span>`;
    const conds = r.conditions || {};
    // fix P1-3: insufficient_data 行圆点区显示『—』而非 5 个空心圆
    // （2026-10-16：圆点/tooltip 的渲染已收进 `condCellHtml`，与猎手表共用，见该函数注释）
    const isNoData = r.verdict === "insufficient_data";
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
    // P1(2026-08-25): verdict/score 脱钩展示——range 市 signal 结构性不可达（t_regime 那30分锁死，
    //   score 上限70）。用 score_ceiling/signal_reachable 诚实呈现，避免"70分"看着像"离signal一步"。
    const _ceiling = (r.score_ceiling != null) ? r.score_ceiling : 100;
    const _reachable = (r.signal_reachable != null) ? r.signal_reachable : true;
    // 2026-10-15：得分口径已两度重构（10-11 移出市场项；10-15 移除随 regime 变的「回撤到位」、
    //   加入「站上MA20(权重3)」与「关键支撑企稳」），**得分与大盘 regime 完全无关** ⇒
    //   旧文案「震荡市 市场有方向那30分锁死、上限70」早已不成立，删掉。
    const scoreCellTitle = _reachable
      ? "综合得分(0~100) = −多头结构 −MACD金叉 −站上MA5 + 3×站上MA20 + 支撑企稳 + 日线阶段分"
        + "（不站上MA5扣70、不站上MA20扣40；GO ⟹ 两条都站上 ⟹ 得分>0）"
      : `综合得分 · 当前市况(震荡)下 signal 结构性不可达（t_regime 不过）；`
        + `得分本身**与大盘无关**、仍可 0~100`;
    const scoreCellHtml = _reachable
      ? `<b>${r.composite_score}</b>`
      : `<b>${r.composite_score}</b><span class="cell-dim" style="font-size:10px">/${_ceiling}🔒</span>`;
    // 2026-10-19 owner：建仓信号扫描**整行底色**改为「站上 5 日均线=绿 / 其它=红」，
    //   与得分里 −70 悬崖的口径一致（原来按得分 <20 红/≥20 绿，会和规则脱节）。
    //   无 MA5 特征（旧数据/未扫描）不着色，避免把"无数据"画成"破位"。
    const _tf = r.timing_features || {};
    const _above5 = _tf.above_ma5;
    const _scoreBg = (_above5 === true) ? "background:rgba(63,185,80,.10);"
      : (_above5 === false) ? "background:rgba(248,81,73,.12);" : "";
    // 建仓/加仓时机门控徽章（timing_gate: 多头追强/空头抄底/震荡降频）
    const _tm = r.timing || {};
    let tmBadge = "";
    if (_tm.regime) {
      const _regimeCn = {trend_up: "多头", trend_dn: "空头", range: "震荡"}[_tm.regime] || _tm.regime;
      const _goCls = _tm.go ? "#1f6f3f" : "#7a2a2a";
      const _goTxt = _tm.go ? "GO" : "NO-GO";
      tmBadge = `<span class="badge" title="时机门控: ${esc(_tm.reason || "")}" style="background:${_goCls};color:#fff">${esc(_regimeCn)}·${_goTxt}</span>`;
    }
    // W35(2026-08-25) 日内右侧确认徽章：GO 后是否已过 15分钟确认（站上EMA8+放量+站上VWAP）。
    //   未确认 → GO 被降级为 approaching(待日内确认)，此徽章说明差在哪。
    const _ic = r.intraday_confirm;
    let icBadge = "";
    if (_ic) {
      if (_ic.insufficient) {
        icBadge = `<span class="badge" title="日内确认: ${esc(_ic.detail || "")}" style="background:#4a4a5a;color:#bbb">确认·数据不足</span>`;
      } else if (_ic.passed) {
        icBadge = `<span class="badge" title="日内确认: ${esc(_ic.detail || "")}" style="background:#1f6f3f;color:#fff">日内✓确认</span>`;
      } else {
        icBadge = `<span class="badge" title="日内确认未过(GO已降级为待确认): ${esc(_ic.detail || "")}" style="background:#7a5a2a;color:#ffd98a">待日内确认</span>`;
      }
    }
    // 2026-08-27: 否决因子徽章（爆量≥3倍20日均量 / 偏离MA60>+20%，仅 trend_up 追强侧触发）。
    //   conditions.t_veto 仅在触发时由后端写入（false）；未触发时键不存在，不显示。
    let vetoBadge = "";
    if (conds.t_veto === false) {
      const _vt = ((r.timing || {}).veto || []).join("、") || (_tm.reason || "");
      vetoBadge = `<span class="badge" title="否决因子触发（建仓被压为 approaching）：${esc(_vt)}" style="background:#7a2a2a;color:#ffd98a">🚫否决</span>`;
    }
    // 卡点直观化(2026-08-25)：一句话"卡在哪、差多少" + hover 完整差距清单。
    //   signal 无卡点；pending/archived/无数据 不显示（那是未扫描态，非条件卡）。
    //   点击卡点标签打开条件详情面板
    let blockLine = "";
    if (r.block_reason && r.verdict !== "signal" && r.verdict !== "pending"
        && r.verdict !== "archived" && r.verdict !== "insufficient_data") {
      const bk = Array.isArray(r.blockers) ? r.blockers : [];
      const full = bk.length
        ? bk.map(b => `• ${b.label}：${b.gap_txt}（需${b.need}）`).join("\n")
        : r.block_reason;
      const code = r.code || '';
      const name = r.name || code;
      blockLine = `<div class="cell-dim" style="font-size:11px;line-height:1.5;color:#ffb454;margin-top:2px;cursor:pointer;" title="${esc(full)}" onclick="event.stopPropagation();openConditionDetail('${esc(code)}','${esc(name)}')">🔸 ${esc(r.block_reason)}${bk.length > 1 ? ` <span style="color:#888">(+${bk.length - 1}项)</span>` : ""}</div>`;
    }
    // 追踪中徽章：在建仓股池监控、尚未触发 signal/archived 的股票明确标出（弱/等待/无数据也显示在监控）
    const monitorBadge = (!r.in_holdings && r.verdict !== "signal" && r.verdict !== "archived")
      ? `<span class="badge monitor" title="在建仓股池中监控，等待触发买点">追踪中</span>`
      : "";
    // 30min 判定（2026-10-09）：与「持仓 30min 风险体检」**同口径**（m30_features.verdict_from_features）。
    // 读后端 `r.m30_verdict`（主线程零计算；未热=⏳计算中，10s 轮询自动补）。原 30/60min 背离列已由本列取代。
    const _M30VCLS = { high: "badge signal", watch: "badge approach", bull: "badge bear" };
    const mv = r.m30_verdict || {};
    const mvCls = _M30VCLS[mv.level];
    const mvCell = `<td style="text-align:center" title="${esc(mv.reason || "30min 判定不可用")}">${
      mvCls ? `<span class="${mvCls}">${esc(mv.label || "")}</span>`
            : `<span class="cell-dim">${esc(mv.label || "—")}</span>`}</td>`;
    // 仓位结构（2026-10-11）：读后端 `r.structure`（主线程零计算；未热=⏳，10s 轮询自动补）。
    // 渲染与持仓体检/猎手板块**共用** `structCellHtml`（同一真源、同一外观）。
    const stCell = structCellHtml(r.structure);
    return `
      <tr id="pb-row-${esc(r.code || '')}" ondblclick="openStockChart('${esc(r.code||'')}','${esc(r.name||r.code||'')}')" style="cursor:pointer;${_scoreBg}${isStale ? "opacity:.45;" : ""}" title="双击看K线${isStale ? "（数据陈旧，距今超过10分钟）" : ""}">
        <td>${esc(r.name || "")} <span class="mono cell-dim">${esc(r.code || "")}</span>
          ${monitorBadge}${chBadge}${apBadge}${tmBadge}${icBadge}${vetoBadge}${r.in_holdings ? `<span class="badge hold">持仓</span>` : ""}${errTxt}${blockLine}</td>
        <td style="text-align:center">${poolBadge}</td>
        <td>${verdictBadge(r.verdict)}</td>
        <td class="num" title="${esc(scoreCellTitle)}">${scoreCellHtml}</td>
        <td class="num ${metCls}"><b>${isNoData ? "—" : metCount + "/3"}</b></td>
        ${dayStageCellHtml(r.structure)}
        ${ma20FlagCellHtml(_tf.above_ma20)}
        <td class="num">${fmt(r.price)}<div style="font-size:10px">${pctHtml(r.change_pct, "实时涨幅（随行情每 10s 刷新）")}</div></td>
        <td style="text-align:center">${boxStr}</td>
        <td style="max-width:220px;line-height:1.7">${tagsTxt}</td>
        ${condCellHtml(conds, condLabels, isNoData, (r.timing || {}).veto)}
        ${mvCell}
        ${stCell}
        ${ma5CellHtml(r.structure)}
        <td class="cell-dim">${esc(r.scan_type || "")}${r._scans > 1 ? `×${r._scans}` : ""}${scanTimeTxt ? ` <span class="mono">${esc(scanTimeTxt)}</span>` : ""}${staleBadge}</td>
        <td>${!r.in_holdings ? `<button class="mini-btn" style="font-size:10px;padding:0 5px;color:#3fb950" onclick="confirmPosition('${esc(r.code||'')}',document.getElementById('pb-row-${esc(r.code||'')}'))" title="人工确认建仓（回写 signal_history）">✓确认</button><button class="mini-btn" style="font-size:10px;padding:0 5px;color:#f85149" onclick="removeFromWatchlist('${esc(r.code||'')}',document.getElementById('pb-row-${esc(r.code||'')}'))" title="从股池移除">✕</button>` : ""}</td>
      </tr>`;
  }).join("");

  // fix P0-1: counts 已是按 code 去重的股票数，徽章统一『只』口径并加 tooltip
  // fix P1-7: insufficient_data/pending 用独立灰色 nodata 徽章，不再落入 weak
  const badgeClsOf = k => k === "signal" ? "signal" : k === "approaching" ? "approach"
    : (k === "insufficient_data" || k === "pending" || k === "archived") ? "nodata" : "weak";
  const c = k => counts[k] ? `<span class="badge ${badgeClsOf(k)}" title="按股票数统计（按 code 去重）">${k}: ${counts[k]}只</span>` : "";
  // 2026-08-27: watch_signal 独立计数徽章（此前在计数里消失，只落 weak 样式）
  const cWatch = counts["watch_signal"]
    ? `<span class="badge" style="background:#2a4a5a;color:#8ad4ff" title="震荡市观察态：结构+回撤达标但市场无方向，只喂样本不推送">观察: ${counts["watch_signal"]}只</span>` : "";
  // fix P0-2: refreshed_at 语义为数据最后写入时间，直接展示
  const refreshed = pb.refreshed_at ? `<span class="live-dot"></span> ${esc(pb.refreshed_at)}${delta ? ` <span class="aw-delta">${delta}</span>` : ""}` : "";
  el.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:8px;margin-bottom:6px;flex-wrap:wrap;align-items:center">
        ${c("signal")}${c("approaching")}${c("weak")}${cWatch}${c("insufficient_data")}${c("archived")}
        <span class="cell-dim mono" style="font-size:10px;margin-left:auto" title="数据最后写入时间（前端盘中每10s拉取）">${refreshed}</span>
      </div>
      ${pb.note ? `<div class="cell-dim" style="font-size:11px;margin-bottom:6px">⚠ ${esc(pb.note)}</div>` : ""}
      ${inWarmup ? `<div class="warmup-banner">⏳ 开盘预热中：5分钟K线累积中（需≥3根），09:45 后出完整信号</div>` : ""}
  ${pb.progress ? `<div class="cell-dim" style="font-size:10px;margin-bottom:6px">扫描进度: <b>${pb.progress.scanned}/${pb.progress.total_candidates}</b> 只已扫${pb.progress.pending ? ` · <b class="warn">${pb.progress.pending}</b> 只待扫描` : ''} · 已扫描 <b>${pb.progress.online_fetched}</b> 只 · 无数据 <b class="warn" title="insufficient_data：5分钟K线/快照不足，未参与条件判定">${pb.progress.no_data}</b> 只<span class="cell-dim">（K线/快照不足）</span></div>` : ""}
      <table>
        <thead><tr>
          <th>股票</th><th title="P3-2 池分管：人工=manual侧扫描 / 自动=auto侧管理">池</th><th>判定</th><th class="num">得分</th><th class="num">通过</th>
          <th title="日线四阶段（描述状态，紧挨得分便于对照）：②抬升/③派发/④下跌/①蓄势。⚠️ 实测横截面IC与直觉相反——抬升/追高前瞻更差(IC −0.031~−0.042, t=−12~−15)、下跌/超跌前瞻更好(IC +0.020~+0.034, t=+7~+12)，分半窗两段同号；效应量小(IC≈0.04)仅作相对强弱参照">日线阶段</th>
          <th title="是否站上 20 日线（观察列，不驱动判定）。⚠️ 行底色才是 5 日线口径（站上=绿、其它=红，得分 −70 悬崖同源）；本列只看 20 日线。">20日线<br><span style="font-size:9px;opacity:.6">观察</span></th>
          <th class="num" title="实时价 + 实时涨幅（随行情每 10s 刷新）。取不到显示 — 而不是 0.00%：不把「无数据」画成「平盘」">价<br><span style="font-size:9px;opacity:.6">涨幅</span></th>
          <th title="突破箱体=第一优先级">突破</th>
          <th title="通道/箱体/背离等技术形态。背离标签已**自带周期**（如「日线顶背离」），与右侧『30min判定』的 30 分钟口径不同源 —— 悬停标签可见确认日期">技术标签</th>
          <th title="时机门控：市场有方向/多头结构/回撤到位（GO→signal，震荡→降频）">时机条件</th>
          <th title="30分钟综合判定（与『持仓 30min 风险体检』同口径）：🔴减仓/避高=30min 顶背离(T1/T2)；🟠盯紧=T3顶分型+T4均线压制同现；🟢偏好=无顶信号且有新鲜底背离(≤16根)；⚪中性=其余。依据 981只×540日 离线验证；T3/T4 无前瞻区分度、单个不报警">30min判定</th>
          <th title="仓位结构（30min/10min，供仓位管理参考）：止损=10min 最近已确认摆动低点；阶段=10min 四阶段(①蓄势/②抬升/③派发/④下跌)；30m=30min MA65 中期方向；折价=现价/止损-1（到-4.3%记满）。破位时给出三档下档支撑挂单价与5日成交率估计。⚠️ 是**结构参考、不是买卖信号**——机械按阶段加减仓经实测无正贡献(纯择时-1.4~+2.7pp不显著)，「破前低就清仓」-30pp强有害；唯一有证据的是破位后在下档支撑挂限价补仓(改善成本结构、不提高期望)。悬停看明细">仓位结构</th>
          <th title="30min MA5 状态（**纯展示**）：现价相对 5×30min 均线 站上/跌破，及距上次翻转几根。⚠️ 实测**无前瞻信息量**（状态/跃迁 × 站上/跌破 四口径，同日同bar-of-day对照 |diff|≤0.03pp、|t|≤2.4）⇒ 仅供参考，不参与任何判定。朴素对照的 −0.343pp(t=−25.5) 是日内时段伪影">30m MA5<br><span style="font-size:9px;opacity:.6">仅展示</span></th>
          <th>扫描</th><th></th>
        </tr></thead>
        <tbody>${rows || '<tr><td colspan="16" class="empty">无扫描结果</td></tr>'}</tbody>
      </table>
      <div class="cell-dim" style="font-size:11px;margin-top:8px">●=通过 ○=未通过 · 时机条件：${Object.values(condLabels).join(" / ")} · 第5位🚫=否决因子(爆量≥3倍或偏离MA60超+20%) · GO(多头追强/空头抄底)→signal，震荡→降频 · 观察=震荡市结构达标(不推送) · 盘中每10s刷新</div>
    </div>`;
  // fix P1-2: 页脚文案与后端实际逻辑一致（≥80 且突破箱体→signal，突破另需≥40）
}

/* ---- P3-2 池分管：切换池筛选下拉时重渲染建仓表 ---- */
function setPoolFilter(v) {
  window._pbPoolFilter = v;
  if (window._pbLastPayload) renderPB(window._pbLastPayload);
}

/* ---- P4-3 自动盘页：读 t_io/bridge ---- */
async function loadAutoStatus() {
  try {
    const d = await apiCall("load_auto_status");
    renderAutoStatus(d);
  } catch (e) { /* pywebview 未就绪时忽略 */ }
}

function renderAutoStatus(d) {
  const hb = d && d.heartbeat;
  // KILL_SWITCH 全页顶部红色横幅（P4-3）
  const ksb = document.getElementById("killSwitchBanner");
  if (ksb) {
    if (d && d.kill_switch) {
      ksb.innerHTML = '⛔ KILL_SWITCH 已触发 —— 自动盘已停止下单/买入，请人工检查';
      ksb.style.display = "block";
    } else {
      ksb.style.display = "none";
    }
  }
  const el = document.getElementById("autoStatusBody");
  if (!el) return;
  if (!hb) {
    el.innerHTML = '<div class="empty">无自动盘心跳（auto 引擎未运行，或 t_io/bridge/heartbeat.json 不存在）</div>';
    renderAutoPositions(null);
    return;
  }
  const counts = d.events || {};
  const countBadge = k => counts[k] ? `<span class="badge" style="background:#2a4a5a;color:#8ad4ff">${k}: ${counts[k]}</span>` : "";
  const latest = (d.latest || []).map(e => {
    const color = e.event === "reject" ? "#f85149" : e.event === "risk" ? "#ffb454"
      : e.event === "fill" ? "#3fb950" : "#8ad4ff";
    const tm = (e.time || "").slice(11, 19);
    const extra = e.side ? ` ${e.side}${e.qty ? ` ${e.qty}` : ""}${e.price ? `@${e.price}` : ""}` : "";
    return `<div style="font-size:11px;line-height:1.7;border-bottom:1px solid #333;display:flex;gap:6px;align-items:center">
      <span class="badge" style="background:${color};color:#111">${esc(e.event)}</span>
      <span class="mono cell-dim">${esc(tm)}</span><span>${esc(e.code || "")}</span>
      <span class="cell-dim">${esc(e.detail || "")}${extra}</span></div>`;
  }).join("") || '<div class="cell-dim">今日无 order/fill/reject/risk 事件</div>';
  const regime = esc(hb.index_regime || "—");
  const cash = hb.cash != null ? fmt(hb.cash, 0) : "—";
  const posCount = Object.keys(hb.positions || {}).length;
  el.innerHTML = `
    <div class="card">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        <span class="live-dot"></span> 心跳 <span class="mono">${esc(hb.time || "")}</span> bar=${esc(hb.bar || "")}
        ${countBadge("order")}${countBadge("fill")}${countBadge("reject")}${countBadge("risk")}
        <span class="cell-dim mono" style="margin-left:auto">现金 ${cash} · 持仓 ${posCount} 只 · 大盘 ${regime} ${hb.index_score != null ? esc(hb.index_score) : ""}</span>
      </div>
      <div class="cell-dim" style="font-size:11px;margin-bottom:4px">最新 10 条 order/fill/reject/risk 事件（t_io/bridge/events_*.jsonl）</div>
      ${latest}
    </div>`;
  renderAutoPositions(hb.positions);
}

function renderAutoPositions(positions) {
  // 双台账并列（P4-3）：自动盘 tab 全量 + overview 紧凑卡片，来源标注「自动」
  const els = ["autoPositionsBody", "autoOverviewBody"].map(
    id => document.getElementById(id)).filter(Boolean);
  if (!els.length) return;
  if (!positions || !Object.keys(positions).length) {
    const empty = '<div class="empty">无自动盘持仓数据（auto 引擎未运行或空仓）</div>';
    els.forEach(el => { el.innerHTML = empty; });
    return;
  }
  const srcBadge = '<span class="badge" style="background:#2a4a5a;color:#8ad4ff" title="自动盘（goldminer auto 侧管理）">自动</span>';
  const rows = Object.entries(positions).map(([code, p]) => {
    // 成本来源提示：引擎 heartbeat 的 cost 在加仓后不重新加权，故口径以 fill 派生为准
    const costTip = p.cost_src === "fill派生"
      ? '成交流派生的加权均价（不含手续费，同券商「持仓均价」）· 引擎 heartbeat 加仓后不重算'
      : "引擎 heartbeat 快照（成交流数量对不上，无法派生）";
    return `<tr>
      <td>${esc(p.name || code)} <span class="mono cell-dim">${esc(code)}</span></td>
      <td>${srcBadge}</td>
      <td class="num">${fmt(p.qty, 0)}</td>
      <td class="num" title="${esc(costTip)}">${fmt(p.cost, 3)}</td>
      <td class="num">${fmt(p.last_price, 3)}</td>
      <td class="num">${fmt(p.market_value, 0)}</td>
      <td class="num ${clsOf(p.unreal_pnl)}">${p.unreal_pnl == null ? "—" : (p.unreal_pnl >= 0 ? "+" : "") + fmt(p.unreal_pnl, 0)}</td>
    </tr>`;
  }).join("");
  const html = `<table><thead><tr><th>股票</th><th>来源</th><th class="num">持仓</th><th class="num">成本</th>
    <th class="num">最新价</th><th class="num">市值</th><th class="num">浮动盈亏</th></tr></thead><tbody>${rows}</tbody></table>`;
  els.forEach(el => { el.innerHTML = html; });
}

/* ---- 自动盘盈亏账单（2026-10-10 需求5）：每次建仓→清仓为一周期 ---- */
async function loadAutoPnl() {
  try {
    const d = await apiCall("load_auto_pnl");
    renderAutoPnl(d || {});
  } catch (e) { /* 桥接未就绪/后端异常时忽略 */ }
}

function renderAutoPnl(d) {
  state.autoLedger = d || {};
  renderAutoLedger(d || {});
  const body = document.getElementById("autoPnlBody");
  if (!body) return;
  if (d.error) { body.innerHTML = `<div class="empty">盈亏账单读取失败: ${esc(d.error)}</div>`; return; }
  const s = d.summary || {};
  updateSidebarAuto(s);
  const sumEl = document.getElementById("autoPnlSummary");
  if (sumEl) {
    const f = (v, cls) => v == null ? "—" : `<span class="${cls || ""}">${(v >= 0 ? "+" : "") + fmt(v, 0)}</span>`;
    sumEl.innerHTML = `总资产 <b>${fmt(s.total_assets || 0, 0)}</b> · `
      + `已实现 ${f(s.realized_pnl, clsOf(s.realized_pnl))} · `
      + `在场浮盈 ${f(s.unreal_pnl, clsOf(s.unreal_pnl))} · `
      + `持仓市值 ${fmt(s.market_value || 0, 0)}（成本 ${fmt(s.cost_basis || 0, 0)}）· `
      + `现金 ${s.cash == null ? "—" : fmt(s.cash, 0)} · `
      + `在场 ${s.positions_count || 0} 只`;
  }
  const stocks = d.stocks || [];
  if (!stocks.length) { body.innerHTML = '<div class="empty">无自动盘成交记录（t_io/bridge/events_*.jsonl 为空）</div>'; return; }
  const rows = stocks.map(st => {
    const cycles = (st.cycles || []).map(c => {
      const open = c.status === "open";
      const cls = clsOf(c.realized_pnl + (open ? (c.unreal_pnl || 0) : 0));
      const pnlShow = open
        ? `浮盈 <span class="${clsOf(c.unreal_pnl)}">${(c.unreal_pnl >= 0 ? "+" : "") + fmt(c.unreal_pnl, 0)}</span>`
        : `<span class="${clsOf(c.realized_pnl)}">${(c.realized_pnl >= 0 ? "+" : "") + fmt(c.realized_pnl, 0)}</span> (${c.pnl_pct >= 0 ? "+" : ""}${c.pnl_pct}%)`;
      return `<div class="pnl-cycle">
        <span class="mono cell-dim">${esc(c.start)} → ${open ? '<span class="warn">在场</span>' : esc(c.end || "")}</span>
        <span class="cell-dim">${c.days}天</span>
        <span>${c.buy_qty}买/${c.sell_qty}卖</span>
        <span class="cell-dim">均买${fmt(open && c.cost_price != null ? c.cost_price : c.avg_buy, 3)}${c.avg_sell ? " / 均卖" + fmt(c.avg_sell, 3) : ""} 费${fmt(c.fees, 0)}</span>
        <span>${pnlShow}</span>
        ${open && c.open_qty ? `<span class="cell-dim">在场${c.open_qty}股</span>` : ""}
        ${c.irregular ? '<span class="badge" style="background:#5a3a2a;color:#ffb454" title="该周期含非策略/孤儿成交（pos_after 与逐笔不连续）">含非策略成交</span>' : ""}
      </div>`;
    }).join("");
    const tot = st.realized_pnl + (st.unreal_pnl || 0);
    return `<tr>
      <td>${esc(st.name || st.code)} <span class="mono cell-dim">${esc(st.code)}</span></td>
      <td class="num ${clsOf(st.realized_pnl)}">${(st.realized_pnl >= 0 ? "+" : "") + fmt(st.realized_pnl, 0)}</td>
      <td class="num ${clsOf(st.unreal_pnl)}">${st.unreal_pnl ? (st.unreal_pnl >= 0 ? "+" : "") + fmt(st.unreal_pnl, 0) : "—"}</td>
      <td class="num">${st.open_qty || "—"}</td>
      <td class="num ${clsOf(tot)}">${(tot >= 0 ? "+" : "") + fmt(tot, 0)}</td>
      <td>${cycles}</td>
    </tr>`;
  }).join("");
  body.innerHTML = `<table><thead><tr>
    <th>股票</th><th class="num">已实现</th><th class="num">在场浮盈</th><th class="num">在场股数</th><th class="num">合计</th>
    <th>周期明细（建仓→清仓 / 天数 / 量 / 均价 / 盈亏）</th>
  </tr></thead><tbody>${rows}</tbody></table>`;
}

/* ---- 自动盘成交流水 · 每日盈亏（2026-10-10 需求2）：自动盘无交割单，靠事件总线反推 ---- */
const AUTO_FILLS_SHOW = 200;   // 流水表只显示最近 N 笔
let _autoFillsOpen = false;    // 逐笔流水默认折叠（owner 2026-10-17）；展开状态跨重渲染保留

function renderAutoLedger(d) {
  const sumEl = document.getElementById("autoLedgerSummary");
  const bodyEl = document.getElementById("autoFillsBody");
  if (!sumEl && !bodyEl) return;
  const fills = (d && d.fills) || [];
  const daily = (d && d.daily) || [];
  if (sumEl) {
    if (!fills.length && !daily.length) {
      sumEl.textContent = "无自动盘成交流水（t_io/bridge/events_*.jsonl 为空）";
    } else {
      const realSum = fills.reduce((a, f) => a + (f.side === "SELL" ? (f.net || 0) : 0), 0);
      const buySum = fills.reduce((a, f) => a + (f.side === "BUY" ? (f.net || 0) : 0), 0);
      sumEl.innerHTML = `逐笔 ${fills.length} 笔 · 买入净流出 <span class="down mono">${fmt(buySum, 0)}</span> · `
        + `卖出净流入 <span class="up mono">+${fmt(realSum, 0)}</span> · 收盘快照 ${daily.length} 日`;
    }
  }
  // 每日权益 / 累计已实现 / 现金
  if (!daily.length) {
    echClear("echAutoEquity");
  } else {
    const dates = daily.map(r => r.date.slice(5));
    const suspectIdx = daily.map((r, i) => (r.suspect ? i : -1)).filter(i => i >= 0);
    echRender("echAutoEquity", {
      ...ECH_BASE,
      tooltip: { ...ECH_BASE.tooltip, trigger: "axis", formatter: p => {
        const i = p[0].dataIndex, r = daily[i];
        const line = (n, v, c) => v == null ? "" :
          `<br>${n}: <span style="color:${c}">${(v >= 0 ? "+" : "") + fmt(v, 0)}</span>`;
        return `${r.date}` + line("权益", r.equity, "#8ad4ff")
          + line("现金", r.cash, "#8b949e") + line("市值", r.market_value, "#8b949e")
          + line("当日盈亏", r.day_pnl, pnlColor(r.day_pnl))
          + line("累计已实现", r.realized_cum, pnlColor(r.realized_cum))
          + (r.suspect ? '<br><span style="color:#ffb454">⚠ 当日权益跳变 &gt;15%：现金变动与策略成交流水对不上（常见成因＝仿真终端手工单/孤儿成交）</span>' : "");
      } },
      legend: { data: ["权益", "累计已实现", "现金"], textStyle: { color: "#8b949e", fontSize: 11 }, top: 0 },
      xAxis: { ...ECH_BASE.xAxis, type: "category", data: dates },
      yAxis: { ...ECH_BASE.yAxis },
      series: [
        { name: "权益", type: "line", smooth: true, symbolSize: 5, data: daily.map(r => r.equity),
          lineStyle: { color: "#8ad4ff" }, itemStyle: { color: "#8ad4ff" },
          markPoint: suspectIdx.length ? { symbol: "pin", symbolSize: 34,
            itemStyle: { color: "#ffb454" }, label: { formatter: "⚠", color: "#111" },
            data: suspectIdx.map(i => ({ coord: [dates[i], daily[i].equity], name: "记账异常" })) } : undefined },
        { name: "累计已实现", type: "line", smooth: true, symbolSize: 5,
          data: daily.map(r => r.realized_cum), lineStyle: { color: "#3fb950" }, itemStyle: { color: "#3fb950" } },
        { name: "现金", type: "line", smooth: true, symbolSize: 0, showSymbol: false,
          data: daily.map(r => r.cash), lineStyle: { color: "#8b949e", type: "dashed", width: 1 },
          itemStyle: { color: "#8b949e" } },
      ],
    });
  }
  if (!bodyEl) return;
  if (!fills.length) { bodyEl.innerHTML = '<div class="empty">无成交流水</div>'; return; }
  const shown = fills.slice(0, AUTO_FILLS_SHOW);
  const rows = shown.map(f => {
    const buy = f.side === "BUY";
    return `<tr>
      <td class="mono cell-dim">${esc(f.date)} ${esc(f.time || "")}</td>
      <td>${esc(f.name || f.code)} <span class="mono cell-dim">${esc(f.code)}</span></td>
      <td><span class="badge" style="background:${buy ? "#2a4a5a" : "#3a2a2a"};color:${buy ? "#8ad4ff" : "#ffb454"}">${buy ? "买入" : "卖出"}</span></td>
      <td class="num">${fmt(f.qty, 0)}</td>
      <td class="num">${fmt(f.price, 3)}</td>
      <td class="num">${fmt(f.amount, 0)}</td>
      <td class="num ${clsOf(f.net)}">${(f.net >= 0 ? "+" : "") + fmt(f.net, 0)}</td>
      <td class="num cell-dim">${fmt(f.pos_after, 0)}</td>
    </tr>`;
  }).join("");
  // 2026-10-17 owner：逐笔流水**默认折叠**（表格本体很长，日常看的是上面的每日盈亏与汇总）。
  // 用原生 <details>：不引入新样式/脚本；`_autoFillsOpen` 记住展开状态，
  // 否则每次数据刷新重渲染都会把它重新收起。
  bodyEl.innerHTML = `<details${_autoFillsOpen ? " open" : ""} ontoggle="_autoFillsOpen=this.open">`
    + `<summary class="card-title" style="cursor:pointer;user-select:none">`
    + `逐笔流水（近 ${shown.length}/${fills.length} 笔，倒序）`
    + `<span class="cell-dim" style="font-weight:normal;font-size:11px;margin-left:8px">`
    + `点击展开 / 收起 · 净发生金额＝成交金额∓手续费；余额＝该笔成交后持仓</span></summary>`
    + `<table style="margin-top:6px"><thead><tr><th>时间</th><th>股票</th><th>方向</th><th class="num">数量</th><th class="num">均价</th>`
    + `<th class="num">成交金额</th><th class="num">净发生金额</th><th class="num">余额</th></tr></thead><tbody>${rows}</tbody></table></details>`;
}

/* ---- 自动盘买入人工确认闸（2026-08-30 建仓/加仓/底仓回补人工把关） ---- */
let _shownReqs = {};   // {code: request_id} 已弹出未处理的请求（防重复弹窗）
const BUY_CONFIRM_KIND = { build: "建仓", add: "加仓", topup: "底仓回补" };

function renderBuyConfirmBadge(n) {
  const el = document.getElementById("buyConfirmBadge");
  if (!el) return;
  if (n > 0) { el.textContent = n; el.style.display = "inline-block"; }
  else el.style.display = "none";
}

function closeBuyConfirmModal() {
  const m = document.getElementById("buyConfirmModal");
  if (m) m.style.display = "none";
}

async function respondBuyConfirm(code, requestId, decision) {
  try {
    const r = await apiCall("respond_buy_confirm", code, requestId, decision);
    if (!r || !r.ok) { statusEl("确认回复失败: " + ((r && r.error) || ""), "err"); return; }
    delete _shownReqs[code];
    const item = document.querySelector(`.buy-confirm-item[data-code="${code}"]`);
    if (item) item.remove();
    const modal = document.getElementById("buyConfirmModal");
    const list = document.getElementById("buyConfirmList");
    if (list && !list.querySelectorAll(".buy-confirm-item").length) closeBuyConfirmModal();
    pollBuyConfirm();
  } catch (e) { statusEl("确认回复失败: " + e.message, "err"); }
}

async function pollBuyConfirm() {
  try {
    const d = await apiCall("load_buy_confirm_pending");
    const pending = (d && d.pending) || [];
    const answered = (d && d.answered) || {};
    const modal = document.getElementById("buyConfirmModal");
    const list = document.getElementById("buyConfirmList");
    let changed = false;
    for (const req of pending) {
      const code = req.code;
      if (answered[code] === req.request_id) { delete _shownReqs[code]; continue; }
      if (_shownReqs[code] === req.request_id) continue;
      _shownReqs[code] = req.request_id;
      changed = true;
      if (!list) break;
      const kind = BUY_CONFIRM_KIND[req.kind] || req.action || "买入";
      const reasons = (req.reasons || []).map(esc).join("<br>")
        || '<span class="cell-dim">无理由</span>';
      const item = document.createElement("div");
      item.className = "buy-confirm-item";
      item.dataset.code = code;
      item.dataset.requestId = req.request_id;
      item.innerHTML = `
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
          <span class="badge" style="background:#ffb454;color:#111">${esc(kind)}</span>
          <b>${esc(req.name || code)}</b> <span class="mono cell-dim">${esc(code)}</span>
        </div>
        <div class="cell-dim" style="font-size:12px;margin-bottom:6px">
          预计买入 <b>${fmt(req.qty, 0)}</b> 股 @ <b>${req.price != null ? fmt(req.price, 3) : "—"}</b>
          ${req.pos_qty > 0 ? ` · 现持仓 <b>${fmt(req.pos_qty, 0)}</b>` : ""}
        </div>
        <div class="cell-dim" style="font-size:11px;margin-bottom:10px">${reasons}</div>
        <div style="display:flex;gap:8px">
          <button class="mini-btn primary" style="flex:1" data-confirm="confirm">✅ 确认买入</button>
          <button class="mini-btn danger" style="flex:1" data-confirm="reject">❌ 拒绝</button>
        </div>`;
      list.appendChild(item);
    }
    const remaining = pending.filter(
      r => answered[r.code] !== r.request_id && _shownReqs[r.code] === r.request_id).length;
    renderBuyConfirmBadge(remaining);
    if (changed && modal && list && list.querySelectorAll(".buy-confirm-item").length) {
      modal.style.display = "flex";
    }
  } catch (e) { /* pywebview 未就绪时忽略 */ }
}

function initBuyConfirmEvents() {
  const modal = document.getElementById("buyConfirmModal");
  if (!modal) return;
  modal.addEventListener("click", async (e) => {
    const btn = e.target.closest("button[data-confirm]");
    if (!btn) return;
    const item = btn.closest(".buy-confirm-item");
    if (!item) return;
    await respondBuyConfirm(item.dataset.code, item.dataset.requestId, btn.dataset.confirm);
  });
}

/* ---- 每日选股记录（2026-10-17）：盘前候选 → 10:00 三关漏斗 → 选中 + 逐只原因 ----
   owner：要能回答「每天早上候选了多少、最后选了哪些、原因是什么」，且能选日期回看。
   数据源见 t_gui.Api.load_osc_day：优先 decision_{date}.json（新落盘），
   否则从 backtrace/fills/策略日志读时还原（缺的字段如实显示「未记录」，绝不显示成 0）。 */
let _oscDayCache = {};        // date -> payload（同一天不重复取）
let _oscDayCur = null;        // 当前渲染的 payload（筛选时复用，不重新请求）

const OSC_REASON_CN = {
  "ok": "三关全过",
  "not_weak(prev_close>=ma20)": "非超跌(昨收≥MA20)",
  "gap_not_held(low<prev_close)": "①破昨收",
  "not_above_prev_close": "②未站上昨收",
  "not_above_vwap": "③未站上VWAP",
  "bar_missing": "10:00棒缺失",
  "prev_close_missing": "昨收缺失",
  "board_filtered": "板块过滤",
};
const OSC_STATUS_CN = { picked: "✓ 选中", spare: "候补", failed: "未过" };

function oscReasonCn(r) { return OSC_REASON_CN[r] || r || "—"; }

async function loadOscDay(force) {
  const _di = document.getElementById("oscDayDate");
  const date = (_di && _di.value) || state.date;      // 面板日期优先，回退全局选中日（同 #pbScanDate）
  const el = document.getElementById("oscDayBody");
  if (!date) { if (el) el.innerHTML = '<div class="empty">未选定日期</div>'; return; }
  if (!force && _oscDayCache[date]) { renderOscDay(_oscDayCache[date]); return; }
  if (el) el.innerHTML = '<div class="empty">读取中…</div>';
  try {
    const d = await apiCall("load_osc_day", date);
    _oscDayCache[date] = d;
    renderOscDay(d);
  } catch (e) {
    if (el) el.innerHTML = `<div class="empty">读取失败: ${esc(e.message)}</div>`;
  }
}

function _oscVal(v) { return (v === null || v === undefined) ? '<span class="cell-dim">未记录</span>' : v; }

function _oscStockRows(rows, buys) {
  if (!rows || !rows.length) return '<tr><td colspan="7" class="cell-dim">无</td></tr>';
  buys = buys || {};
  return rows.map(s => {
    // 10:00 决策 ≠ 成交：可能被拒/资金不足/顺移。买侧结果取自 fills.buys。
    const b = buys[s.code];
    const deal = b
      ? (b.filled_qty ? `成交 ${b.filled_qty}股` : `${esc(b.status || "?")}${b.defer_reason ? "·" + esc(b.defer_reason) : ""}`)
      : "—";
    return `<tr style="cursor:pointer" ondblclick="openStockChart('${esc(s.code)}','${esc(s.name || s.code)}')" title="双击看K线">
    <td>${esc(s.name || s.code)} <span class="mono cell-dim">${esc(s.code)}</span></td>
    <td class="cell-dim">${esc(s.board || "—")}</td>
    <td class="num">${s.gap != null ? pctHtml(s.gap * 100, "10:00 缺口") : "—"}</td>
    <td class="num">${s.rank != null ? s.rank : "—"}</td>
    <td>${esc(OSC_STATUS_CN[s.status] || s.status || "—")}</td>
    <td class="cell-dim">${esc(oscReasonCn(s.reason))}</td>
    <td class="cell-dim">${deal}</td>
  </tr>`; }).join("");
}

function filterOscStocks() {
  const body = document.getElementById("oscStocksBody");
  if (!body || !_oscDayCur) return;
  const kw = ((document.getElementById("oscStockFilter") || {}).value || "").trim().toLowerCase();
  const rc = ((document.getElementById("oscReasonFilter") || {}).value || "");
  let rows = _oscDayCur.stocks || [];
  if (kw) rows = rows.filter(s => (s.code || "").includes(kw)
    || (s.name || "").includes(kw) || (s.board || "").includes(kw));
  if (rc) rows = rows.filter(s => (s.reason || "") === rc);
  body.innerHTML = _oscStockRows(rows, _oscDayCur.buys);
  const cnt = document.getElementById("oscStockCount");
  if (cnt) cnt.textContent = `${rows.length}/${(_oscDayCur.stocks || []).length} 只`;
}

function renderOscDay(d) {
  const el = document.getElementById("oscDayBody");
  if (!el) return;
  _oscDayCur = d || {};
  if (!d || !d.available) {
    const miss = (d && d.missing && d.missing.length) ? `（缺：${esc(d.missing.join("、"))}）` : "";
    el.innerHTML = `<div class="empty">${esc((d && d.date) || "")} 无选股记录${miss}</div>`;
    return;
  }
  const pm = d.premarket || {};
  const fn = d.funnel || {};
  const src = d.source === "recorded"
    ? '<span class="badge" style="background:#1f6f3f;color:#fff" title="当日 10:00 落盘的结构化记录">已记录</span>'
    : '<span class="badge" style="background:#7a5c1f;color:#fff" title="当日未落盘，由审计/成交/策略日志还原；缺失项显示「未记录」">还原</span>';
  const reasonOpts = ["", ...Object.keys(d.reason_counts || {})]
    .map(k => `<option value="${esc(k)}">${k ? esc(oscReasonCn(k)) : "全部原因"}</option>`).join("");
  el.innerHTML = `
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <b>${esc(d.date || "")}</b> ${src}
      <span class="cell-dim">盘前候选 <b>${_oscVal(pm.candidates_n)}</b> 只</span>
      <span class="cell-dim">拿到10:00棒 <b>${_oscVal(fn.pool_n)}</b></span>
      <span class="cell-dim">三关全过 <b>${_oscVal(fn.all)}</b></span>
      <span class="cell-dim">最终选中 <b>${(d.picks || []).length}</b></span>
      ${pm.universe ? `<span class="cell-dim">宇宙: ${esc(pm.universe === "full_market" ? "全市场" : "猎手池")}</span>` : ""}
    </div>
    ${fn.pool_n != null ? `<div class="cell-dim" style="font-size:12px;margin-bottom:8px">
      漏斗（各关<b>独立</b>通过数，不是上一关的幸存者）：
      超跌 ${_oscVal(fn.weak)} · ①全程不破昨收 <b>${_oscVal(fn.g1)}</b>/${fn.pool_n}
      · ②站上昨收 <b>${_oscVal(fn.g2)}</b>/${fn.pool_n}
      · ③站上VWAP <b>${_oscVal(fn.g3)}</b>/${fn.pool_n}
      ${d.bars && d.bars.got != null ? ` · 10:00棒 ${d.bars.got}/${d.bars.want}` : ""}
    </div>` : ""}
    ${pm.board_dist && Object.keys(pm.board_dist).length ? `<div class="cell-dim" style="font-size:12px;margin-bottom:8px">板块分布：
      ${Object.entries(pm.board_dist).map(([b, n]) => `${esc(b)} ${n}只`).join(" / ")}</div>` : ""}
    ${d.partial && d.missing && d.missing.length ? `<div class="cell-dim" style="font-size:11px;margin-bottom:6px">
      ⚠ 该日缺：${esc(d.missing.join("、"))}（显示为「未记录」；这些数据从下一交易日起自动落盘）</div>` : ""}
    <div class="card-title" style="margin-top:6px">最终选中（gap 降序）· 成交列取自 fills</div>
    <table><thead><tr><th>股票</th><th>板块</th><th class="num">gap</th><th class="num">排名</th><th>状态</th><th>原因</th><th>成交</th></tr></thead>
      <tbody>${_oscStockRows(d.picks || [], d.buys)}</tbody></table>
    <div class="card-title" style="margin-top:14px;display:flex;gap:8px;align-items:center">
      全部候选
      <span id="oscStockCount" class="cell-dim" style="font-weight:normal"></span>
      <input type="text" id="oscStockFilter" class="mini-input" placeholder="筛选 代码/名称/板块"
             style="width:160px;margin-left:auto" oninput="filterOscStocks()">
      <select id="oscReasonFilter" class="mini-btn" style="font-size:11px" onchange="filterOscStocks()">${reasonOpts}</select>
    </div>
    <table><thead><tr><th>股票</th><th>板块</th><th class="num">gap</th><th class="num">排名</th><th>状态</th><th>原因</th><th>成交</th></tr></thead>
      <tbody id="oscStocksBody">${_oscStockRows(d.stocks || [], d.buys)}</tbody></table>
    ${(d.stocks || []).length && d.source !== "recorded"
      ? '<div class="cell-dim" style="font-size:11px;margin-top:4px">⚠ 还原模式下逐只明细不完整（只有当日策略日志记下的「最接近」若干只 + 已触发的 picks/spares），完整名单从下一交易日起自动记录。</div>' : ""}`;
  filterOscStocks();
}

async function refreshPBNow() {
  if (pbRefreshRunning) return;
  const _di = document.getElementById("pbScanDate");
  const date = (_di && _di.value) || state.date;      // 与 recomputePB 同口径
  if (!date) return;
  const btn = document.getElementById("pbRefreshBtn");
  pbRefreshRunning = true;
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 刷新中"; }
  try {
    const pb = await apiCall("refresh_pb", date);
    // 叠加已轮询到的最新价/涨幅（同 app.js:397 的周期刷新口径）——
    // 不叠的话 `refresh_pb` 的返回会把「价/涨幅」列洗成空，与"刷新"的意图刚好相反。
    renderPB(applyQuotesToPB(pb || {}));
    statusEl(`已刷新建仓扫描（${date}｜刷新于 ${(pb && pb.refreshed_at) || "?"}）`, "ok");
  } catch (e) {
    statusEl(`刷新建仓扫描失败: ${e.message}`, "err");
  } finally {
    pbRefreshRunning = false;
    if (btn) { btn.disabled = false; btn.textContent = "🔄 刷新"; }
  }
}

// ---- 建仓扫描：按面板所选日期重放（日线口径，支持历史任意一天）----
let pbRecomputeRunning = false;
function _pbSyncScanDate() {   // 面板日期选择器跟随全局选中日
  const el = document.getElementById("pbScanDate");
  if (el && state.date && el.value !== state.date) el.value = state.date;
}
function _oscSyncDayDate() {   // 每日选股记录的面板日期，同款跟随全局选中日
  const el = document.getElementById("oscDayDate");
  if (el && state.date && el.value !== state.date) el.value = state.date;
}
async function recomputePB() {
  if (pbRecomputeRunning) return;
  const _di = document.getElementById("pbScanDate");
  const date = (_di && _di.value) || state.date;      // 面板内日期优先，回退全局选中日
  if (!date) return;
  const btns = document.querySelectorAll(".recompute-pb-btn");
  pbRecomputeRunning = true;
  btns.forEach(b => { b.disabled = true; b.textContent = "⏳ 重跑中..."; });
  statusEl(`重放 ${date} 建仓扫描（日线口径，约需1-2分钟）...`, "ok");
  try {
    // 面板选定日期 != 当前视图 → 先切到该日整日视图（各面板 + 10s 轮询口径一致）
    if (date !== state.date) {
      userPinnedDate = (date !== todayStr());
      const _sel = document.getElementById("dateSelect");
      if (_sel && Array.from(_sel.options).some(o => o.value === date)) _sel.value = date;
      try { await loadAndRender(date, false); } catch (e) { /* 无复盘数据也能扫 */ }
    }
    const r = await apiCall("recompute_pb", date);
    if (r && r.position_builder) {
      renderPB(r.position_builder);
      if (state.payload) {
        state.payload.position_builder = r.position_builder;
      }
      const cnt = r.position_builder.counts || {};
      const cntTxt = Object.keys(cnt).filter(k => cnt[k] > 0)
        .map(k => `${k}:${cnt[k]}`).join(" ");
      statusEl(r.error
        ? `⚠ 重放 ${date} 建仓扫描异常: ${r.error}（已返回现有结果）`
        : `重放完成（${date}｜${cntTxt || "无"}，刷新于 ${r.position_builder.refreshed_at || "?"}）`,
        r.error ? "err" : "ok");
    } else {
      statusEl("重跑失败: 无返回", "err");
    }
  } catch (e) {
    // 不谎报失败：超时只说明**前端**不再等，后端仍在算且会写 trace（同 pollBreakoutScan 的原则）。
    const _to = /超时/.test(e.message || "");
    statusEl(_to
      ? `重跑超时（${date}）：后端可能仍在计算，结果会写入 trace —— 稍后刷新即可看到`
      : "重跑失败: " + e.message, "err");
  }
  pbRecomputeRunning = false;
  btns.forEach(b => { b.disabled = false; b.textContent = "🔄 扫描该日"; });
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
// 2026-10-20：K 线弹窗打开期间**暂停后台重面板轮询**（auto/ob/beacon/live）。
// 根因：pywebview 的 evaluate_js 串行，load_auto_pnl 这类 3~5s 的大响应会把
// K 线首帧/实时尾堵在队列里；弹窗打开时背后的面板没人看，暂停最划算。
let chartModalOpen = false;
let stockChartName = "";
// 用户手动缩放（滚轮/拖拽 slider）后记住视窗，否则 10s 自动刷新重建图表时会弹回默认窗口。
// 存 dataZoom 的百分比 {start, end}；null = 尚未手动缩放，用默认窗口。
let stockChartZoom = null;
// 后端 payload 的数据身份版本号；轮询时带上它，后端数据没变就只回 {"unchanged":true}，
// 省掉 300KB 过桥 + JSON 解析 + 重画（实测后者是每 10s 周期性卡顿的主因）。
let stockChartVersion = null;
// 最近一次**程序化**设进 dataZoom 的视窗：dataZoom 事件里用它区分「程序设置」与「用户缩放」。
let stockChartAppliedZoom = null;
// 2026-10-20：用户正在拖动/滚轮缩放时，实时尾（10 分 1s）不要重绘 ——
// setOption 会把滑杆弹回，表现为“切到 10 分钟线后缩放不工作”。
let stockChartInteractingUntil = 0;
// 2026-10-20：setOption 之后 ECharts 会补发一次 dataZoom 事件（百分比可能是默认 0/100）。
// 若把它当成"用户缩放"，就会把 stockChartZoom 覆盖成默认值 ⇒ 下一次重绘把用户缩放弹回。
// 每次程序化 setOption 后短暂忽略 dataZoom 事件（用户手势在 500ms 内不会开始）。
let stockChartZoomGuardUntil = 0;
// 2026-10-20 诊断：把缩放/重绘事件写进 t_io/logs/chart_zoom.log（失败静默）
function _chartLog(event, detail) {
  try { window.pywebview.api.report_chart_event(event, String(detail || "")); } catch (e) { /* 静默 */ }
}
// MA 线配色（2026-10-17）：K线图改深底后，原浅底那组（#e6c07b 等）亮度过高、
// 在深底上发白刺眼。换成按深底重新选的一组（dataviz 校验器 dark 全项通过：
// 亮度带 / 彩度 / CVD ΔE≥8.4 / 对比度≥3:1）。
const MA_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9", "#e66767"];

/* 默认可见根数。取值贴着后端黄金分割回看期（t_gui._FIB_LOOKBACK）——daily 120 与回看期
   相等，锚点必然落在默认视窗内；weekly/monthly 因 bar 更少而略小，万一锚点更早由下面的
   放宽分支兜底。核心目的：后端 800 根日线全画出来蜡烛只有 ~3px，形态根本看不清。 */
// 2026-10-20 owner：日线默认 **近 6 个月**（120 根）。
//   · 曾经是"全部"（约 800 根）—— 那是为了回「日线只能显示一部分」做的过度修正，
//     800 根蜡烛只有 ~3px、形态根本看不清，owner 现在改口要 6 个月；
//   · 120 同时等于后端黄金分割的回看期（`t_gui._FIB_LOOKBACK["daily"]=120`），
//     锚点必然落在默认视窗内，不会出现"标了锚点却看不见"；
//   · 想更长/更短仍可点「近3月 / 近1年 / 全部」。
// K 线弹窗的分钟档（2026-10-20 加 5 分）——**与后端 t_gui._MIN_PERIODS 同一份清单**，
// 加档位时两边都要动；漏改的症状是"某个 Tab 永远空白"。
const MIN_PERIODS = ["min5", "min10", "min30", "min60"];
const DEFAULT_BARS = { daily: 120, weekly: 104, monthly: 60,
                       min5: 160, min10: 160, min30: 160, min60: 160 };
// 快捷区间：按周期给不同标签；bars=null 表示全部。每个周期的默认项都在表内（高亮用）。
// 分钟档后端保留根数（2026-10-30 起 5/10 分加深，见 t_gui.MIN_BARS_CHART）：
//   5分 3200 根 ≈ 近3月（48 根/日）；10分 1600 根 ≈ 近3月（24 根/日）；
//   30分 320 根 ≈ 8根/日×40日；60分 320 根 ≈ 4根/日×80日。
const RANGE_PRESETS = {
  daily: [{ label: "近3月", bars: 60 }, { label: "近6月", bars: 120 },
          { label: "近1年", bars: 250 }, { label: "全部", bars: null }],
  weekly: [{ label: "近1年", bars: 52 }, { label: "近2年", bars: 104 }, { label: "全部", bars: null }],
  monthly: [{ label: "近1年", bars: 12 }, { label: "近3年", bars: 36 },
            { label: "近5年", bars: 60 }, { label: "全部", bars: null }],
  // 5分：后端保留 3200 根 ≈ 66 个交易日（48 根/日）
  min5: [{ label: "近2日", bars: 96 }, { label: "近4日", bars: 192 },
         { label: "近1周", bars: 240 }, { label: "近3月", bars: 3120 }, { label: "全部", bars: null }],
  // 10分：后端保留 1600 根 ≈ 66 个交易日（24 根/日）
  min10: [{ label: "近3日", bars: 72 }, { label: "近1周", bars: 120 },
          { label: "近2周", bars: 240 }, { label: "近3月", bars: 1560 }, { label: "全部", bars: null }],
  min30: [{ label: "近1周", bars: 40 }, { label: "近2周", bars: 80 },
          { label: "近1月", bars: 160 }, { label: "全部", bars: null }],
  min60: [{ label: "近1周", bars: 20 }, { label: "近2周", bars: 40 },
          { label: "近1月", bars: 80 }, { label: "全部", bars: null }],
};
// 各周期各自记住用户选的区间。**未设 = 用默认根数；null = 全部**（两者必须可区分，
// 否则选了「全部」会退回默认窗口）。跨会话保留，避免每次打开重选。
let stockChartRange = {};
try {
  const _savedRange = JSON.parse(localStorage.getItem("stockChartRange") || "null");
  if (_savedRange && typeof _savedRange === "object") {
    stockChartRange = _savedRange;
    // 2026-10-20：默认已回到「近6月=120」（见 DEFAULT_BARS 上方注释）⇒ 原先那条
    // 「把存着的 120 删掉」的迁移已经多余，去掉：显式选过 120 的用户保持 120 即可。
    // 显式选过「全部」的存的是 null，仍然照旧保留（null 与"未设"必须可区分）。
  }
} catch (e) { /* 坏数据忽略，用默认 */ }

// 根数 → dataZoom 起始百分比。bars 为 null(全部) 或 n<=bars 时整体可见(0)。
function barsToZStart(n, bars) {
  if (!bars || n <= bars) return 0;
  return (n - bars) / (n - 1) * 100;
}
// 该周期当前生效的根数：用户选过就用选的，否则用默认。
function activeBars(period) {
  const sel = stockChartRange[period];
  return sel === undefined ? DEFAULT_BARS[period] : sel;
}
function renderRangeButtons(period) {
  const host = document.getElementById("stockRangeGroup");
  if (!host) return;
  const cur = activeBars(period);
  const presets = RANGE_PRESETS[period] || RANGE_PRESETS.daily;
  host.innerHTML = presets.map(p =>
    `<button class="range-btn${(p.bars === cur || (p.bars === null && cur === null)) ? " active" : ""}"`
    + ` data-bars="${p.bars === null ? "all" : p.bars}" onclick="setChartRange('${period}',${p.bars === null ? "null" : p.bars})">${p.label}</button>`
  ).join("")
    // 2026-10-20 owner：「无法使用缩放放大/缩小」——滑杆两端手柄在"全部"视图下贴边不好抓，
    // 这里给一组**明确按钮**（＋放大 / －缩小 / ⟲重置），任何设备都能用。
    + `<button class="range-btn" onclick="zoomStockChart(0.7)" title="放大（显示更少根数）">＋</button>`
    + `<button class="range-btn" onclick="zoomStockChart(1.4)" title="缩小（显示更多根数）">－</button>`
    + `<button class="range-btn" onclick="resetStockChartZoom()" title="恢复默认窗口">⟲</button>`;
}

/* 以当前视窗中心为锚点缩放（factor<1 放大、>1 缩小），并记住供后续重绘沿用。 */
function zoomStockChart(factor) {
  const cur = stockChartZoom || { start: 0, end: 100 };
  const span = Math.max(0.5, Math.min(100, (cur.end - cur.start) * Number(factor || 1)));
  const center = (cur.start + cur.end) / 2;
  let start = center - span / 2, end = center + span / 2;
  if (start < 0) { end -= start; start = 0; }
  if (end > 100) { start -= (end - 100); end = 100; }
  start = Math.max(0, start);
  stockChartZoom = { start: start, end: end };
  _chartLog("zoom_btn", JSON.stringify({factor: factor, zoom: stockChartZoom}));
  renderStockChart();
}
function resetStockChartZoom() {
  stockChartZoom = null;
  _chartLog("zoom_reset", "");
  renderStockChart();
}
function setChartRange(period, bars) {
  stockChartRange[period] = bars;
  stockChartZoom = null;                    // 换了区间，旧的缩放作废
  _chartLog("set_range", JSON.stringify({period: period, bars: bars}));
  try { localStorage.setItem("stockChartRange", JSON.stringify(stockChartRange)); } catch (e) {}
  renderRangeButtons(period);
  renderStockChart();
}

// 是否请求 30/60分：日线/周/月视图不取（分钟线逐只打 tushare，是首帧主延迟）；
// 仅当切到分钟 Tab，或本票已加载过分时（轮询时保持），才请求。
function _wantMinutes() {
  const pd = stockChartData && stockChartData.period_data;
  if (pd && pd.min30) return true;
  return MIN_PERIODS.includes(stockChartPeriod);
}

async function loadStockChartNow() {
  if (!stockChartCode) return;
  try {
    // 2026-10-20：把当前周期一起传下去 ⇒ 分钟档按需只抓当前 Tab 的那一档
    _chartLog("load_req", JSON.stringify({code: stockChartCode, version: stockChartVersion,
      period: stockChartPeriod, wantMin: _wantMinutes()}));
    const d = await apiCall("load_stock_chart", stockChartCode, stockChartVersion,
                            _wantMinutes(), stockChartPeriod);
    _chartLog("load_resp", JSON.stringify({unchanged: !!(d && d.unchanged),
      available: !!(d && d.available), version: (d && d.version) || "",
      dailyN: (d && d.period_data && d.period_data.daily && (d.period_data.daily.dates || []).length) || 0}));
    if (d && d.unchanged) {
      // 数据没变：不重建图表，只刷新「10s实时更新」那行时间戳（renderStockSummary 只是拼串，很轻）
      if (stockChartData) renderStockSummary(stockChartData);
      return;
    }
    if (d && d.available) {
      stockChartData = d;
      stockChartVersion = d.version || null;
      renderStockSummary(d);
      const _tr0 = performance.now();
      if (Date.now() < stockChartInteractingUntil) {
        // 用户正在缩放/拖动：先更新数据，1.6s 后再重绘，别把滑杆弹回
        setTimeout(() => { if (stockChartCode) renderStockChart(false); }, 1600);
      } else {
        // 20s 整图轮询也只换数据：dataZoom 只在打开/切周期/点区间时设置
        renderStockChart(false);
      }
      const _tr = performance.now() - _tr0;
      // 2026-10-20：区分「后端慢」与「前端渲染慢」——>=500ms 单独记一行
      if (_tr >= 500) {
        try { window.pywebview.api.report_slow_call("chart_render", Math.round(_tr)); } catch (e) { /* 静默 */ }
      }
      // 整图可能比实时尾旧（分钟源/缓存/版本握手三层闸）⇒ 立刻用实时尾覆盖当前周期
      startStockChartLive();
    } else if (d && d.error) {
      // 明确失败（代码不存在/退市/无数据）⇒ 把原因显示出来，别让弹窗永远停在「加载中...」
      if (stockChartInst) { try { stockChartInst.dispose(); } catch (e) { } stockChartInst = null; }
      const el = document.getElementById("stockChart");
      if (el) {
        el.innerHTML = `<div class="empty" style="color:#f85149;padding-top:120px">${esc(d.error)}</div>`;
      }
      const ss = document.getElementById("stockSummary");
      if (ss) ss.innerHTML = "";
      stopStockChartLive();
    }
  } catch (e) {
    // 2026-10-20：原来静默吞错 ⇒ 后端异常时弹窗永远停在「加载中...」，被误判为"慢"。
    const el = document.getElementById("stockChart");
    if (el) el.innerHTML = `<div class="empty" style="color:#f85149;padding-top:120px">K线加载失败：${esc((e && (e.message || e)) || "未知错误")}</div>`;
    try { window.pywebview.api.report_slow_call("chart_error", 0); } catch (e2) { /* 静默 */ }
  }
}

/* ---- 盘中实时尾（2026-10-17）----
   整图 `load_stock_chart` 在盘中被三层缓存挡住，看不到**正在形成的那根**：
   ① tushare 分钟源盘中只回到上一交易日；② 分钟线内存缓存原按 30 分钟时段；
   ③ 版本握手只认 (末日期:根数)，价格在动但签名不变 ⇒ 回 unchanged、不重画。
   这里每 3s（交易时段）单独要一次当前周期，后端用腾讯分钟 K 线+实时快照重算，
   回整段序列；前端只替换该周期、保留缩放。非交易时段降到 30s。 */
let stockChartLiveTimer = null;
let stockChartLiveInflight = false;
let stockChartLiveSig = {};          // period -> 上次画过的 sig
let stockChartLiveWarm = 0;          // 连续 warming 次数（快重试要有上限，别把桥打满）

function _chartLiveIntervalMs() {
  const d = new Date();
  const wd = d.getDay();
  const hm = d.getHours() * 100 + d.getMinutes();
  const open = wd >= 1 && wd <= 5
    && ((hm >= 915 && hm <= 1135) || (hm >= 1255 && hm <= 1505));
  if (!open) return 30000;
  // 分钟图 3s（正在形成的那根就是实时价）；日/周/月 5s（当日一根动得慢，
  // 而后端日线序列是 800 根全量重算+序列化，约 250ms，别按 3s 打）
  const isDaily = (stockChartPeriod === "daily" || stockChartPeriod === "weekly"
                   || stockChartPeriod === "monthly");
  // 2026-10-20 owner：「盘中打开 K 线重点看 10 分钟线，实时性非常重要」——
  // 10 分单独提到 **1s**（后端 SWR TTL 0.8s + 腾讯 m5 合成 ~0.3s，1s 能拿到新价）；
  // 30/60 分仍 2s（持仓）/3s（非持仓），日线 3.5/5s。
  // 5/10 分：0.5s 一跳（2026-10-20 二轮，owner「极其实时」）。
  // 后端 worker 已拆成并行两跳（≈200ms），SWR TTL 0.4s ⇒ 每跳都拿到新一帧；
  // 而**行情没动时后端只回 sig（<1KB，不带 102KB 序列）**，所以快轮询不会把桥压垮。
  if (!isDaily && (stockChartPeriod === "min10" || stockChartPeriod === "min5")) return 500;
  // 2026-10-19 owner：**持仓股优先**（30min 体检表里的票就是要盯的）——
  // 分钟 2s / 日线 3.5s，比普通票快一档；后端 SWR + 快照修正末根，2s 内有新价即可见。
  const _c = String(stockChartCode || "").split("_")[0];
  const _holding = !!_c && (state.quotes || [])
    .some(q => String((q && q.code) || "").split("_")[0] === _c);
  if (_holding) return isDaily ? 3500 : 2000;
  return isDaily ? 5000 : 3000;
}

function startStockChartLive() {
  stopStockChartLive();
  if (!stockChartCode) return;
  stockChartLiveTimer = setTimeout(tickStockChartLive, 600);
}

function stopStockChartLive() {
  if (stockChartLiveTimer) { clearTimeout(stockChartLiveTimer); stockChartLiveTimer = null; }
}

async function tickStockChartLive() {
  stockChartLiveTimer = null;
  const modal = document.getElementById("stockModal");
  if (!stockChartCode || !stockChartData || !modal || modal.style.display === "none") return;
  // 窗口最小化/被遮挡时停跳（夜里弹窗忘了关，别每 3s 叫醒后端）
  if (document.visibilityState === "hidden") {
    stockChartLiveTimer = setTimeout(tickStockChartLive, 10000);
    return;
  }
  if (stockChartLiveInflight) {          // 上一跳还没回来：让开，别叠请求
    stockChartLiveTimer = setTimeout(tickStockChartLive, 1000);
    return;
  }
  stockChartLiveInflight = true;
  const code = stockChartCode, period = stockChartPeriod;
  let nextMs = _chartLiveIntervalMs();
  try {
    // 把**本地已持有的 sig** 传上去：后端据此在"没变"时只回 sig/quote（不带 102KB 序列）
    const r = await apiCall("load_chart_realtime", code, period,
                            stockChartLiveSig[period] || "");
    if (code !== stockChartCode || !r) return;         // 期间换票/关窗
    if (r.quote && r.quote.price) {
      stockChartData.current_price = r.quote.price;
      stockChartData.live = { ts: r.ts, quote: r.quote, session: !!r.session };
    }
    if (r.available && r.series && r.sig && period === stockChartPeriod) {
      if (stockChartLiveSig[period] !== r.sig) {
        const cur = (stockChartData.period_data && stockChartData.period_data[period]) || {};
        const _curN = (cur.dates || []).length;
        const _newN = (r.series.dates || []).length;
        if (_curN >= 120 && _newN * 2 < _curN) {
          // 2026-10-20：实时尾返回的序列**明显更短**（如日线 801→40，来自短日线缓存）
          // 绝不整段替换，否则图会被越刷越短，最终只能在 40 根里缩放。
          _chartLog("live_skip_short", JSON.stringify({period: period,
            curN: _curN, newN: _newN, sig: r.sig}));
          nextMs = 600;
        } else {
        // 整段替换（含 MA/MACD/RSI/BOLL/fib）；保留前端可能有的额外字段
        stockChartData.period_data[period] = Object.assign({}, cur, r.series);
        if (Date.now() < stockChartInteractingUntil) {
          // 用户正在缩放/拖动：只更新数据，**不重绘**，否则滑杆会被 setOption 弹回。
          // 不写 sig ⇒ 交互结束后下一跳会自然重绘并补上最新帧。
          _chartLog("live_skip_interacting", JSON.stringify({period: period, sig: r.sig}));
          nextMs = 600;
        } else {
          stockChartLiveSig[period] = r.sig;
          _chartLog("live_render", JSON.stringify({period: period, sig: r.sig,
            zoom: stockChartZoom}));
          renderStockChart(false);        // 实时尾：只换数据，不碰 dataZoom
        }
        }   // end: 长度正常，允许替换
      }
    }
    // 后端是 SWR：warming=后台还在首次取数；stale=回的是上一帧、正在刷新。
    // 这两种情况快速补拉一次（后端算完就进缓存），别按 3s/5s 干等。
    if (r.warming) {
      stockChartLiveWarm++;
      nextMs = stockChartLiveWarm <= 5 ? 400 : 1500;   // 快重试最多 5 次
    } else {
      stockChartLiveWarm = 0;
      if (r.stale) nextMs = Math.min(nextMs, 1200);
    }
    renderStockSummary(stockChartData);
  } catch (e) { /* 静默：下一跳重试 */ }
  finally {
    stockChartLiveInflight = false;
    if (stockChartCode) stockChartLiveTimer = setTimeout(tickStockChartLive, nextMs);
  }
}

async function openStockChart(code, name) {
  const modal = document.getElementById("stockModal");
  if (!modal) return;
  modal.style.display = "flex";
  chartModalOpen = true;
  stockChartCode = code;
  _chartLog("open", JSON.stringify({code: code, period: stockChartPeriod}));
  stockChartName = name;
  // 换票先清空旧图数据：否则新代码加载失败时，实时尾会拿着上一只票的序列乱补
  stockChartData = null;
  // 换股票：回到默认/所选区间的视窗，不沿用上一只的缩放
  stockChartZoom = null;
  stockChartLiveSig = {};
  stockChartLiveWarm = 0;
  renderRangeButtons(stockChartPeriod);
  // 换票时收起并清空资料面板（下次点开重新拉）
  const _prof = document.getElementById("stockProfile");
  if (_prof) { _prof.style.display = "none"; document.getElementById("stockProfileBody").innerHTML = ""; }
  stockProfileCode = null;
  stockProfileLiveLoaded = false;
  // 多空分布面板同理：换票后旧票的数字必须立刻消失（缓存也按 code 失效）
  const _flowEl = document.getElementById("stockFlow");
  if (_flowEl) { _flowEl.style.display = "none"; document.getElementById("stockFlowBody").innerHTML = ""; }
  _flowCache = { code: null, step: 0, data: null, ts: 0 };
  document.getElementById("stockModalTitle").textContent = `${name} (${code}) 技术分析`;
  // 必须先 dispose 再清 innerHTML：实例复用后它绑在旧 DOM 上，节点被换掉会让 setOption 报错
  if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }
  stockChartVersion = null;
  document.getElementById("stockChart").innerHTML = '<div class="empty">加载中...</div>';
  // 10s 实时刷新
  if (stockChartTimer) clearInterval(stockChartTimer);
  // 2026-10-18：10s → 20s。价格与最后一根由「实时尾」(3s/5s) 负责；整图轮询只剩
  // 结构（支撑/箱体/通道/新 bar 计数）的同步，没必要每 10s 打一次慢接口。
  stockChartTimer = setInterval(loadStockChartNow, 20000);
  const _open0 = performance.now();
  await loadStockChartNow();
  const _openMs = performance.now() - _open0;
  // 2026-10-20：端到端「点开→出图」耗时（含桥传输+渲染），>=800ms 落慢日志
  if (_openMs >= 800) {
    try { window.pywebview.api.report_slow_call("chart_open_total", Math.round(_openMs)); } catch (e) { /* 静默 */ }
  }
  startStockChartLive();          // 盘中实时尾（3s）
  // 2026-10-20 owner：「业务简介特别的慢」——**开图就在后台预取公司资料**。
  // 用户通常是先看图、过几秒才点「📋 公司资料」，那时缓存已经写好了 ⇒ 点开即出。
  // 只 kick 后台线程、立即返回；失败无所谓（打开资料面板时还会再兜底抓一次）。
  try { apiCall("warm_stock_profile", code).catch(() => {}); } catch (e) { /* 忽略 */ }
}
/* ---- 手动盘「K线速查」（2026-10-18）----
   输入公司/基金/指数名称或代码 → 后端 search_instrument（本地韭研名表优先 + 腾讯 smartbox）
   → 可画 K 线的标的列表。场外基金/港美股无 K 线，后端过滤并在 hint 里注明条数。 */
async function searchKlineInstrument() {
  const inp = document.getElementById("klQuery");
  const out = document.getElementById("klResults");
  const hint = document.getElementById("klHint");
  const btn = document.getElementById("klSearchBtn");
  const q = ((inp && inp.value) || "").trim();
  if (!q) { if (hint) hint.textContent = "请输入名称或代码"; return; }
  if (hint) hint.textContent = "搜索中…";
  if (btn) btn.disabled = true;
  try {
    const r = await apiCall("search_instrument", q);
    renderKlineSearch(r || {});
  } catch (e) {
    if (out) out.innerHTML = `<div class="empty">搜索失败：${esc((e && e.message) || e)}</div>`;
    if (hint) hint.textContent = "";
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderKlineSearch(r) {
  const out = document.getElementById("klResults");
  const hint = document.getElementById("klHint");
  const rows = (r && r.results) || [];
  const note = (r && (r.error || r.note)) || "";
  if (!out) return;
  if (!rows.length) {
    out.innerHTML = `<div class="empty">${esc(note || "未找到可画 K 线的标的")}</div>`;
    if (hint) hint.textContent = "";
    return;
  }
  // 唯一命中 ⇒ 直接开图（用户输入精确名称/代码时少点一次）
  if (rows.length === 1) {
    const x = rows[0];
    if (hint) hint.textContent = `已打开 ${x.name}（${x.code}）${note ? " · " + note : ""}`;
    openStockChart(x.code, x.name);
    out.innerHTML = "";
    return;
  }
  out.innerHTML = rows.map(x => {
    const title = x.note ? `${x.note} · ${x.code} · ${x.type}` : `${x.code} · ${x.type}`;
    return `<button class="mini-btn kl-item" data-code="${esc(x.code)}" data-name="${esc(x.name)}"`
      + ` title="${esc(title)}" onclick="openStockChart(this.dataset.code,this.dataset.name)">`
      + `${esc(x.name)} <span class="mono cell-dim" style="font-size:10px">${esc(x.code)}</span>`
      + ` <span class="kl-type">${esc(x.type)}</span></button>`;
  }).join("");
  if (hint) hint.textContent = note;
}

function toggleChartHelp() {
  const el = document.getElementById("chartHelp");
  el.style.display = el.style.display === "none" ? "block" : "none";
}

/* ---- 公司资料（K线弹窗「📋 公司资料」）----
   两层：本地表先出（秒开、零网络），再自动拉一次东财所属板块覆盖。
   每个代码的实时层只自动拉一次（stockProfileLiveLoaded），避免反复点开反复打东财风控。 */
let stockProfileCode = null;
let stockProfileLiveLoaded = false;

function toggleStockProfile() {
  const el = document.getElementById("stockProfile");
  if (!el) return;
  const show = el.style.display === "none";
  el.style.display = show ? "block" : "none";
  if (show) loadStockProfile();
}

async function loadStockProfile() {
  const body = document.getElementById("stockProfileBody");
  if (!body || !stockChartCode) return;
  if (stockProfileCode !== stockChartCode) {
    stockProfileCode = stockChartCode;
    stockProfileLiveLoaded = false;
  }
  // 1) 本地层
  try {
    const d = await apiCall("load_stock_profile", stockChartCode, false);
    if (stockProfileCode !== stockChartCode) return;   // 期间切了票
    renderStockProfile(body, d, false);
  } catch (e) {
    body.innerHTML = `<div class="empty">资料读取失败: ${esc(e.message)}</div>`;
    return;
  }
  // 2) 实时层（每票只自动拉一次）
  if (stockProfileLiveLoaded) return;
  stockProfileLiveLoaded = true;
  body.insertAdjacentHTML("beforeend", '<div class="prof-loading">⏳ 正在拉取东财所属板块…</div>');
  try {
    const d2 = await apiCall("load_stock_profile", stockChartCode, true);
    if (stockProfileCode !== stockChartCode) return;
    renderStockProfile(body, d2, true);
  } catch (e) { /* 保留本地层 */ }
}

/* 业务简介 HTML：长文优先，兜底本地一句话。 */
function stockBizHtml(d) {
  const p = d && d.profile;
  if (p && p.brief) {
    let h = `<b>业务简介</b>`
      + (p.brief_title ? ` <span class="prof-note">${esc(p.brief_title)}</span>` : "")
      + `：${esc(p.brief)}`;
    if (p.scope) {
      h += `<details class="prof-details"><summary>经营范围（${p.scope.length} 字）</summary>`
        + `<div>${esc(p.scope)}</div></details>`;
    }
    return h;
  }
  const pend = d && d.profile_pending;
  return `<b>业务简介</b>：`
    + (d && d.business_summary ? esc(d.business_summary)
                               : '<span class="prof-none">本地表无记录</span>')
    // 不再承诺"约 2 秒"——实际取决于网络，说死了就是假承诺
    + (pend ? ' <span class="prof-note">⏳ 正在联网补全…</span>' : "");
}

/* 后台补完后再去取，只替换「业务简介」那一块（不重渲染整个弹窗）。
   2026-10-20 owner：「业务简介特别的慢」。原来只**重试一次、固定等 2.6 秒** ——
   而后端那一跳实测 140~1000ms 就能回来 ⇒ 白等 2 秒多；万一慢一点/失败一次，
   面板就永远停在「⏳」上（那个"约 2 秒后自动补上"其实是假承诺）。
   改成**递增间隔多次重试**（累计约 6.7s），拿到就停、换票就停。 */
const _BIZ_RETRY_MS = [400, 600, 900, 1200, 1600, 2000];
let _bizRetryTimer = null;
let _bizRetryIdx = 0;

function _scheduleBizProfileRetry(code) {
  if (_bizRetryTimer) clearTimeout(_bizRetryTimer);
  _bizRetryIdx = 0;
  const _next = () => {
    if (!code || code !== stockChartCode || _bizRetryIdx >= _BIZ_RETRY_MS.length) return;
    const _wait = _BIZ_RETRY_MS[_bizRetryIdx++];
    _bizRetryTimer = setTimeout(async () => {
      _bizRetryTimer = null;
      if (!code || code !== stockChartCode) return;
      try {
        const d = await apiCall("load_stock_profile", code, false);
        if (code !== stockChartCode) return;
        const el = document.getElementById("profBiz");
        if (el && d && d.profile && d.profile.brief) { el.innerHTML = stockBizHtml(d); return; }
      } catch (e) { /* 继续下一轮 */ }
      _next();
    }, _wait);
  };
  _next();
}

function renderStockProfile(body, d, live) {
  if (!body) return;
  if (!d || d.ok === false) {
    body.innerHTML = `<div class="empty">资料读取失败: ${esc((d && d.error) || "未知")}</div>`;
    return;
  }
  const chips = (arr, descs) => (arr || []).map(x => {
    // 2026-10-19：概念注释（后端 concept_descs）——有解释的 chip 悬停显示，并加点状下划线提示可悬停
    const tip = descs && descs[x];
    return `<span class="prof-chip"${tip ? ` title="${esc(tip)}" style="cursor:help;border-bottom:1px dotted"` : ""}>${esc(x)}</span>`;
  }).join("");
  const none = '<span class="prof-none">无</span>';
  let h = `<div class="prof-head"><b>${esc(d.name || stockChartName || d.code)}</b>`
        + `<span class="prof-code">${esc(d.code)}</span>`;
  if (d.boards_source === "em") h += '<span class="prof-src">东财实时</span>';
  else if (!live) h += '<span class="prof-src prof-src-local">本地表</span>';
  h += "</div>";
  // 业务简介（2026-10-17）：优先用东财 F10「核心题材」补全的长文（后端缓存），
  // 本地那句"主营专用设备制造业"只在没抓到简介时兜底。挂 id 供后台补完后定点替换。
  h += `<div class="prof-sec" id="profBiz">${stockBizHtml(d)}</div>`;
  if (d && d.profile_pending) _scheduleBizProfileRetry(d.code || stockChartCode);
  h += `<div class="prof-sec"><b>所属行业/板块</b>：`
     + ((d.sector_list && d.sector_list.length) ? chips(d.sector_list) : none) + "</div>";
  h += `<div class="prof-sec"><b>韭研概念</b>：`
     // 2026-10-20：研究核实的静态 龙头/中军徽章（机器人/军工板块整治起），悬停显示定性理由
     + (d.local_role ? `<span style="display:inline-block;margin-right:6px;padding:1px 8px;border-radius:10px;font-weight:700;font-size:11px;letter-spacing:1px;`
       + (d.local_role === "龙头"
         ? `background:rgba(248,81,73,.22);border:1px solid #f85149;color:#ff9d95`
         : `background:rgba(88,166,255,.20);border:1px solid #58a6ff;color:#9ecbff`)
       + `" title="${esc("【研究定性·" + d.local_role + "】" + (d.local_role_reason ? d.local_role_reason + "。" : "") + "按产业链地位核实，与当日涨跌无关。")}">◆${esc(d.local_role)}</span>` : "")
     + ((d.local_concepts && d.local_concepts.length) ? chips(d.local_concepts, d.concept_descs) : none) + "</div>";
  if (live) {
    h += `<div class="prof-sec"><b>所属板块</b>`
       + `<span class="prof-note">${esc(d.boards_note || "")}</span>：`;
    if (d.boards_source === "em" && d.boards && d.boards.length) {
      const order = ["概念", "地域", "其他"];
      const groups = {};
      d.boards.forEach(b => { (groups[b.kind] = groups[b.kind] || []).push(b.name); });
      h += order.filter(k => groups[k]).map(k =>
        `<div class="prof-grp"><span class="prof-kind">${esc(k)}</span>${chips(groups[k])}</div>`
      ).join("");
    } else {
      h += `<span class="prof-none">${esc(d.error || "取不到（东财风控或网络）")}</span>`;
    }
    h += "</div>";
  }
  body.innerHTML = h;
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
  // ⚠️ 2026-10-20 owner：「点最大化后图形没有自适应填满」——`.modal-lg` 的尺寸变化是
  //    **CSS transition .25s**，原来这里只等 **60ms** 就 resize ⇒ ECharts 量到的是
  //    过渡中途的半大尺寸，之后没有任何事件再纠正它（`.stock-chart` 不在 `echInstances`
  //    的窗口 resize 监听里）⇒ 图就停在"放大了一点但没填满"。
  //    根治：给图表容器挂 **ResizeObserver**（见 `_attachChartResize`），尺寸**只要变了**
  //    就重算，跟过渡时长/触发原因（最大化、还原、窗口拉伸）全都无关。
  //    这里的 60ms 只作为老 WebView2 不支持 RO 时的兜底。
  setTimeout(() => {
    if (stockChartInst) stockChartInst.resize();
    // resize 改了画布高度 ⇒ 现价胶囊的像素位置也要跟着重算（否则会停在旧高度上）
    if (_nowTagState.price != null) {
      _scheduleNowTag(stockChartInst, _nowTagState.price, _nowTagState.color);
    }
  }, 60);
}
function closeStockChart() {
  document.getElementById("stockModal").style.display = "none";
  if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }
  // 关窗时断开尺寸监听（否则它还会对着已 dispose 的实例触发，且随 open/close 累积）
  if (_chartRO) { try { _chartRO.disconnect(); } catch (e) { /* 忽略 */ } _chartRO = null; }
  if (stockChartTimer) { clearInterval(stockChartTimer); stockChartTimer = null; }
  stopStockChartLive();
  chartModalOpen = false;      // 恢复后台面板轮询（下一次 tick 自动续上）
  stockChartCode = null;
  stockProfileCode = null;
  stockProfileLiveLoaded = false;
}
function switchStockPeriod(p) {
  stockChartPeriod = p;
  stockChartZoom = null;                    // 周期换了，根数尺度不同，旧缩放作废
  // 多空分布是**某根 10 分 K 线**的结论 ⇒ 周期一换就作废（别让旧数字挂在新周期上）
  const _flowEl = document.getElementById("stockFlow");
  if (_flowEl) _flowEl.style.display = "none";
  _chartLog("switch_period", JSON.stringify({period: p}));
  stockChartLiveWarm = 0;
  document.querySelectorAll(".stock-tab").forEach(t =>
    t.classList.toggle("active", t.dataset.period === p));
  renderRangeButtons(p);
  const pd = stockChartData && stockChartData.period_data;
  if (MIN_PERIODS.includes(p) && !(pd && pd[p])) {
    // 分钟线**按需拉**（日线视图首帧不再阻塞等它）：先提示，再取数（后端此时会带上分时）
    const el = document.getElementById("stockChart");
    if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }   // 实例绑旧 DOM，先释放
    if (el) el.innerHTML = '<div class="empty">加载分时数据中…</div>';
    loadStockChartNow().then(() => startStockChartLive());
    return;
  }
  renderStockChart();
  // 切周期后立刻要一次实时尾：缓存里的该周期可能是上一交易日的（分钟档尤其）
  startStockChartLive();
}
function renderStockSummary(d) {
  const cur = d.current_price;
  // 今日涨跌幅（日线最后一根 较 前一根）
  const _dl = (d.period_data && d.period_data.daily.ohlc) || [];
  const _n = _dl.length;
  let _chgPct = null;
  // 盘中优先用实时快照的涨跌幅：整图里的末根可能还是**上一交易日**的，
  // 用它算出来的"今日涨跌"会整体错一天（实测这类错读很伤）。
  if (d.live && d.live.quote && typeof d.live.quote.change_pct === "number") {
    _chgPct = d.live.quote.change_pct;
  } else if (_n >= 2) {
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
  // 2026-10-20 owner：「文字看不懂，更没法用来决策」⇒ 通道那串不再甩术语和裸数字：
  // 先说**40 日涨跌**（人话：这段时间涨了多少），再说现价在通道的**位置百分比对应哪条轨**。
  // 斜率属于诊断量，降级进 tooltip，不占视线。
  const _chPos = ch.pos_pct == null ? "" :
    `现价在通道 <b>${fmt(ch.pos_pct, 0)}%</b> 处（0%=下轨、100%=上轨${
      ch.pos_pct <= 25 ? " → 贴近下轨" : ch.pos_pct >= 75 ? " → 贴近上轨" : " → 中段"}）`;
  const _chSlopeTip = "通道斜率"
    + (ch.norm_slope_pct != null ? (ch.norm_slope_pct >= 0 ? " +" : " ") + fmt(ch.norm_slope_pct, 2) + "%/日" : " —");
  const chDesc = ch.direction === "flat" ? ""
    : `<span class="cell-dim" style="font-size:11px" title="${esc(_chSlopeTip)}">`
      + `近40日 <b class="${ch.ret_40d >= 0 ? "up" : "down"}">${ch.ret_40d >= 0 ? "+" : ""}${fmt(ch.ret_40d, 1)}%</b>`
      + (_chPos ? `；${_chPos}` : "") + `</span>`
      + (ch.reversal ? `<span class="badge signal" style="margin-left:6px">↺ 通道反转${ch.reversal === "up_to_down" ? " 上→下" : " 下→上"}</span>` : "")
  // 2026-10-20：分钟图的 MA5 是「5 根该周期 K 线」，与日线 5 日线不同；给一行口径说明。
  // 同日晚上 owner 去掉 MA5 的特殊金线后，那个「把 MA5 说成金线」的前缀已失效 ⇒ 只说两条虚线。
  const _pcnHint = ({ min5: "5分", min10: "10分", min30: "30分", min60: "60分" })[stockChartPeriod];
  const _ma5Hint = _pcnHint
    ? `<span class="ss-item cell-dim" style="font-size:10px">MA5(${_pcnHint})=5 根该周期K · 蓝虚线=日线MA5 · 紫虚线=日线MA10</span>`
    : "";
  // 2026-10-20：把「当前周期实际有多少根数据」写出来，避免"看起来只有一部分"时
  // 无法区分是数据缺失还是缩放窗口。
  const _pdCur = d.period_data && d.period_data[stockChartPeriod];
  const _rangeHint = (_pdCur && (_pdCur.dates || []).length)
    ? `<span class="ss-item cell-dim" style="font-size:10px">本周期 ${_pdCur.dates.length} 根 · ${esc(_pdCur.dates[0])} ~ ${esc(_pdCur.dates[_pdCur.dates.length - 1])}</span>`
    : "";
  document.getElementById("stockSummary").innerHTML = `
    <span class="ss-item">当前价: <b class="mono">${fmt(cur, 3)}</b>${_chgHtml}</span>
    ${_ma5Hint}
    ${_rangeHint}
    <span class="ss-item">通道: ${chTxt} ${chDesc}</span>
    <span class="ss-item">关键压力: ${resTxt}</span>
    <span class="ss-item">关键支撑: ${supTxt}</span>
    <span class="ss-item">箱体: ${boxTxt}</span>
    <span class="ss-item cell-dim mono" style="font-size:10px">${d.live && d.live.ts
      ? `${d.live.session ? "盘中实时" : "实时快照"} ${esc(d.live.ts)}`
      : `10s更新 · ${nowTime()}`} · <span class="live-dot"></span></span>`;
}
/* ---- 现价胶囊（2026-10-20 owner：「价格显示的位置会遮挡 K 线的更新」）----
   原实现：markLine 的 `insideEndTop` 标签 —— 画在**网格内部右端**，正压最新那几根 K 线，
   而盘中 0.5s 刷一次 ⇒ 一直挡着最该看的地方。
   现实现：网格 right 20→58 腾出右侧留白，胶囊画在**留白里**（ECharts graphic），
   竖直位置跟现价走（convertToPixel），水平固定在网格外 ⇒ 不再占用任何数据区。
   ⚠️ 两个坑：
     ① 主 setOption 带 `lazyUpdate:true`，**同一 tick 里** convertToPixel 拿到的还是旧坐标
        ⇒ 必须等一帧（rAF）再定位，否则胶囊会跳一下；
     ② 用户缩放/滚动后 y 轴范围变了，像素位置跟着变 ⇒ dataZoom 回调里要重新定位。
   模块级保存最后的价格/颜色，供 dataZoom 回调复用（处理器只注册一次，闭包拿不到新值）。 */
let _nowTagState = { price: null, color: "#f85149", inst: null, top: null, text: null };

function _updateNowTag(inst, price, color) {
  if (!inst || price == null || price === "" || isNaN(Number(price))) return;
  const _p = Number(price);
  const _txt = String(_p);
  const _col = color || "#f85149";
  let yPx = null;
  try { yPx = inst.convertToPixel({ yAxisIndex: 0 }, _p); } catch (e) { yPx = null; }
  const _h = 18, _w = 52;
  // 主图 grid：top=50、height=50% ⇒ 可见区间 [50, 50+0.5H]
  const _H = inst.getHeight ? inst.getHeight() : 0;
  // 主图 grid：top=50、height=48%（2026-10-20 让出 2% 给 MACD）⇒ 可见带 [50, 50+0.48H]
  const _lo = 50, _lo2 = _H > 0 ? 50 + _H * 0.48 : 1e9;
  // ⚠️ 现价**不在可视 y 区间**时（用户缩放去看历史某段，现价在窗口外）必须**隐藏**，
  //    不能把它 clamp 到边缘 —— 那等于谎报"现价就在这个价位"。
  const _ok = yPx != null && isFinite(yPx) && yPx >= _lo && yPx <= _lo2;
  const _top = _ok ? Math.round(Math.min(_lo2 - _h, Math.max(_lo, yPx - _h / 2))) : _lo;
  // 价格没动 ⇒ 像素位置/文字/颜色全都没变：**跳过这一次 setOption**。
  // 分钟图 0.5s 一帧，若每帧都额外 setOption 一次，等于把整画布重绘成本翻倍；
  // 加上实例身份判断，换票/重建实例后不会被"看起来没变"的旧状态挡住。
  if (_nowTagState.inst === inst && _nowTagState.top === _top
      && _nowTagState.text === _txt && _nowTagState.color === _col
      && _nowTagState.visible === _ok) {
    return;
  }
  _nowTagState = { price: _p, color: _col, inst: inst, top: _top, text: _txt, visible: _ok };
  inst.setOption({ graphic: [{
    id: "nowTag", type: "group", silent: true, visible: _ok,
    right: 3, top: _top,
    children: [
      { type: "rect", shape: { x: 0, y: 0, width: _w, height: _h, r: 3 },
        style: { fill: _col } },
      { type: "text", x: _w / 2, y: _h / 2,
        style: { text: _txt, fill: "#fff",
                 font: "10px 'Segoe UI', 'Microsoft YaHei', sans-serif",
                 align: "center", verticalAlign: "middle" } },
    ],
  }] });
}

/* 图表容器尺寸自适应（2026-10-20 owner：「最大化后图形没有自适应填满」）。

   根因：`.modal-lg` 的宽高变化是 **CSS transition .25s**，而 `toggleMaxStockChart`
   只等 60ms 就 `resize()` ⇒ ECharts 量到的是过渡中途的半大尺寸；之后没有任何事件
   再纠正（`.stock-chart` 不在 `echInstances` 的 window.resize 监听里）⇒ 停在那儿。

   这里改用 **ResizeObserver**：只认容器的实际尺寸，跟"过渡多久、谁触发的"全都无关；
   rAF 节流 —— 过渡期间尺寸每帧都变，逐帧 resize 跟得上动画，也不会把主线程打爆。 */
let _chartRO = null;
function _attachChartResize(el) {
  try {
    if (_chartRO) { try { _chartRO.disconnect(); } catch (e) { /* 忽略 */ } _chartRO = null; }
    if (!el || typeof ResizeObserver !== "function") return;   // 老 WebView2：退回 60ms 兜底
    let _raf = 0;
    _chartRO = new ResizeObserver(() => {
      if (_raf) return;
      _raf = requestAnimationFrame(() => {
        _raf = 0;
        const inst = stockChartInst;
        if (!inst) return;
        // 弹窗隐藏时容器尺寸变 0 ⇒ 别拿 0 去 resize（ECharts 会画成空画布并报尺寸告警）；
        // 重新显示时 RO 还会再触发一次，那时尺寸就正常了。
        if (!el.clientWidth || !el.clientHeight) return;
        try { inst.resize(); } catch (e) { return; }
        // 画布尺寸变了 ⇒ 现价胶囊的像素位置也要重算
        if (_nowTagState.price != null) {
          _scheduleNowTag(inst, _nowTagState.price, _nowTagState.color);
        }
      });
    });
    _chartRO.observe(el);
  } catch (e) { /* 忽略：保留 toggleMax 里的 60ms 兜底 */ }
}

function _scheduleNowTag(inst, price, color) {
  const _run = () => { try { _updateNowTag(inst, price, color); } catch (e) { /* 忽略 */ } };
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(_run);
  else setTimeout(_run, 16);
}

/* 缺口色块（单一真源）：相邻两根**价格区间不重叠** = 跳空；只画**未完全回补**的。
   · 上跳：今低 > 昨高且幅度 ≥ 0.3%；下跳对称。
   · 回补终点：后续某根 low ≤ 昨高（上跳）/ high ≥ 昨低（下跳）⇒ 色块到那根为止。
   · 只画最近 `lookback` 根、最多 `maxN` 块，避免历史缺口把图糊满。
   2026-10-21：从 renderStockChart 里抽出来 —— 六宫格要用**同一套口径**（owner 报
   「多窗口里缺口信息没有显示」，各写一份必然漂移）。 */
function buildGapAreas(ohlc, lookback, minPct, maxN) {
  const out = [];
  if (!ohlc || ohlc.length < 2) return out;
  const n = ohlc.length;
  const from = Math.max(1, n - (lookback || 250));
  const MIN = (minPct == null) ? 0.003 : minPct;
  for (let i = from; i < n; i++) {
    const o = ohlc[i], pv = ohlc[i - 1];
    if (!o || !pv) continue;
    const lo = o[2], hi = o[3], plo = pv[2], phi = pv[3];
    let up = null, loB = 0, hiB = 0;
    if (phi > 0 && lo > phi && (lo - phi) / phi >= MIN) { up = true; loB = phi; hiB = lo; }
    else if (plo > 0 && hi < plo && (plo - hi) / plo >= MIN) { up = false; loB = hi; hiB = plo; }
    if (up === null) continue;
    let end = n - 1;
    for (let j = i + 1; j < n; j++) {
      const b = ohlc[j];
      if (!b) continue;
      if (up ? b[2] <= loB : b[3] >= hiB) { end = j; break; }   // 完全回补 ⇒ 色块到此为止
    }
    out.push([{
      xAxis: i, yAxis: loB,
      itemStyle: {
        color: up ? "rgba(248,81,73,.13)" : "rgba(63,185,80,.13)",
        borderColor: up ? "rgba(248,81,73,.5)" : "rgba(63,185,80,.5)",
        borderWidth: 1, borderType: "dashed",
      },
      name: up ? "↑缺口" : "↓缺口",
      // 小缺口（<1%）不写字，只留色块 —— 否则密集区一串标签互相压
      label: { show: (hiB - loB) / (loB || 1) >= 0.01, fontSize: 9,
               color: up ? "rgba(248,81,73,.9)" : "rgba(63,185,80,.9)" },
    }, { xAxis: end, yAxis: hiB }]);
  }
  const MAX = maxN || 24;
  if (out.length > MAX) out.splice(0, out.length - MAX);
  return out;
}

function renderStockChart(applyZoom = true) {
  // 2026-10-20：实时尾（1~5s）重绘时**不要**再写 dataZoom —— setOption 里的
  // start/end 会把用户正在拖动的滑杆弹回，表现为「看一会儿就缩放不了」。
  // 只有打开/切周期/点区间/点缩放按钮时才 applyZoom=true。
  const data = stockChartData;
  if (!data) return;
  const period = data.period_data[stockChartPeriod];
  // 30分/60分 取不到分时数据时后端不放该键（指数/em 标的、tushare 不可用且无本地缓存）
  // ⇒ 给明确提示而不是抛异常
  if (!period || !(period.dates || []).length) {
    if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }
    document.getElementById("stockChart").innerHTML =
      '<div class="empty" style="color:#8b949e;padding-top:120px">该周期无分时数据（tushare 取不到，且本地无该标的缓存）</div>';
    return;
  }
  // 2026-10-20：这两个**必须在函数最前面**——后面的「日线MA5参考线」会先用到，
  // 放在 maSeries 附近声明会触发 TDZ（Cannot access '_isIntradayPeriod' before initialization）。
  const _periodCn = ({ daily: "日", weekly: "周", monthly: "月",
    min5: "5分", min10: "10分", min30: "30分", min60: "60分" })[stockChartPeriod] || stockChartPeriod;
  const _isIntradayPeriod = MIN_PERIODS.includes(stockChartPeriod);
  // 首次建实例 / 显式要求时才写 dataZoom；实时尾与 20s 轮询都不碰（防弹回）
  const _hadInst = !!stockChartInst;
  const _applyZoom = !!applyZoom || !_hadInst;
  const levels = data.levels;
  const cur = data.current_price;
  // 显示开关
  const showLevels = (document.getElementById("tgLevels") || {}).checked !== false;
  const showBoxes = (document.getElementById("tgBoxes") || {}).checked !== false;
  const showChannel = (document.getElementById("tgChannel") || {}).checked !== false;
  const showMA = (document.getElementById("tgMA") || {}).checked !== false;
  const showFib = (document.getElementById("tgFib") || {}).checked !== false;
  // 2026-10-17 owner：缺口**取消开关、默认显示**（工具栏不再有该勾选项）。
  // 仍保留这个常量而不是硬编码 true：将来若要再挂回开关，只需加回 checkbox 即可自动生效
  // （元素不存在时 `|| {}` 使 `checked !== false` 恒真）。
  const showGaps = (document.getElementById("tgGaps") || {}).checked !== false;
  // 背离标注（2026-10-19 owner）：后端按周期算好顶/底背离事件，前端直接画在对应 K 线上。
  const showDiv = (document.getElementById("tgDiv") || {}).checked !== false;

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
  // 现价线（2026-10-17）：原来是不管涨跌都金色的细实线，几乎读不出来。
  // 改成**按当日涨跌着色**的虚线 + 右端价格胶囊，一眼看到"现在在哪、比昨天红还是绿"。
  const _oC = (period.ohlc && period.ohlc.length) ? period.ohlc[period.ohlc.length - 1] : null;
  const _pC = (period.ohlc && period.ohlc.length > 1) ? period.ohlc[period.ohlc.length - 2][1] : null;
  const _nowUp = _oC && _pC != null ? _oC[1] >= _pC : true;
  const _nowCol = _nowUp ? "#f85149" : "#3fb950";
  // 2026-10-20 owner：「价格显示的位置会遮挡 K 线的更新」——原来价格胶囊是 markLine 的
  // `insideEndTop` 标签，画在网格**内部右端**，正好压住最新那几根 K 线；盘中每 0.5s 刷一次
  // 等于一直挡着最该看的那几根。现在**只留虚线**，价格胶囊改画在网格右侧留白里（见
  // `_updateNowTag` + grid.right 20→58），不再占数据区。
  const currentLine = { yAxis: cur,
    lineStyle: { color: _nowCol, width: 1, type: "dashed", opacity: .85 },
    label: { show: false } };

  // 2026-10-20（北方华创案例）：分钟图的「MA5」= 5 根该周期 K 线（10min→50分钟），
  // 与「日线 5 日线」不是同一条线。分钟 tab 额外画一条**日线 MA5**蓝色虚线参考线，
  // 这样「日线站上 MA5、10 分钟落在 MA5 下方」能同时看见，不再误读。
  const _dma5Arr = (((data.period_data || {}).daily || {}).ma
    ? data.period_data.daily.ma[0] : null);
  const _dma5 = (_dma5Arr && _dma5Arr.length) ? _dma5Arr[_dma5Arr.length - 1] : null;
  // 2026-10-20 owner：「10 分钟周期内也显示 10 日均线」——同样画成日线 MA10 紫色虚线，
  // 与金色周期 MA10（10 根 10min=100 分钟）区分开。
  const _dma10Arr = (((data.period_data || {}).daily || {}).ma
    ? data.period_data.daily.ma[1] : null);
  const _dma10 = (_dma10Arr && _dma10Arr.length) ? _dma10Arr[_dma10Arr.length - 1] : null;
  const dailyMa10Line = (_isIntradayPeriod && _dma10 != null) ? {
    yAxis: _dma10,
    lineStyle: { color: "#bc8cff", type: "dashed", width: 1.4, opacity: .95 },
    label: { formatter: `日线MA10 ${fmt(_dma10, 2)}`, color: "#bc8cff",
             fontSize: 9, position: "insideStartBottom" },
  } : null;
  const dailyMa5Line = (_isIntradayPeriod && _dma5 != null) ? {
    yAxis: _dma5,
    lineStyle: { color: "#58a6ff", type: "dashed", width: 1.4, opacity: .95 },
    label: { formatter: `日线MA5 ${fmt(_dma5, 2)}`, color: "#58a6ff",
             fontSize: 9, position: "insideStartTop" },
  } : null;
  // 2026-10-20 owner：「除了日线图，其它周期的图也画出 20 日均线」——
  // 与上面两条同口径，画的是**日线 MA20**（20 个交易日）的水平参考线，不是「20 根本周期 K 线」
  // 的周期 MA20（那条 maSeries 里一直有，别混淆）。**周/月/10/30/60 分全画**；
  // 日线 tab 本身就是真正的 MA20 曲线，不重复叠线。
  // 颜色选 teal：与蓝(日线MA5)/紫(日线MA10)/金(周期MA5)/绿(周期MA20) 五个在用色
  // 两两 CVD ΔE≥12.7、常视觉 ΔE≥16.9（dataviz 校验器 dark 口径实测），且带直标"日线MA20"。
  const _dma20Arr = (((data.period_data || {}).daily || {}).ma
    ? data.period_data.daily.ma[2] : null);
  const _dma20 = (_dma20Arr && _dma20Arr.length) ? _dma20Arr[_dma20Arr.length - 1] : null;
  const dailyMa20Line = (stockChartPeriod !== "daily" && _dma20 != null) ? {
    yAxis: _dma20,
    lineStyle: { color: "#2dd4bf", type: "dashed", width: 1.4, opacity: .95 },
    label: { formatter: `日线MA20 ${fmt(_dma20, 2)}`, color: "#2dd4bf",
             // 与日线MA10 同用左下角 ⇒ 用 distance 再往下让（markLine 标签只认
             // `distance`，`offset` 在这条渲染路径里不被读取），两条价位接近时不叠字。
             fontSize: 9, position: "insideStartBottom", distance: 17 },
  } : null;

  // 黄金分割（斐波那契回撤/扩展）：跟随当前 Tab；后端已按周期各算一份
  const fib = (period.fib && period.fib.available) ? period.fib : null;
  const fibActive = !!(fib && showFib);
  // 回撤位=区间内潜在支撑阻力(虚线)；扩展位=突破后目标(点线)；0.618 黄金位加粗标 ★
  // 标签置于左端(insideStartTop)，与支撑压力的右端标签错开，避免重叠
  const fibLines = fibActive ? fib.levels.map(lv => {
    const col = lv.side === "support" ? "#3fb950" : "#f85149";
    const isExt = lv.kind === "extension";
    // 关键位 = 黄金位 + 38.2/50（最常看的三条）；其余只画线、调淡，避免标签堆叠
    const isKey = lv.golden || lv.ratio === 0.382 || lv.ratio === 0.5;
    return {
      yAxis: lv.price,
      lineStyle: { color: col, type: isExt ? "dotted" : "dashed",
        width: lv.golden ? 1.8 : (isKey ? 1.2 : 1),
        opacity: lv.golden ? 1 : (isKey ? .85 : .45) },
      // 标签只写比例：价格可从左侧 Y 轴读到，精确值与距现价 % 见悬停 tooltip。
      // 之前写全「78.6% 4057.239」→ 5~7 条长标签堆在左端，太密看不清。
      label: { formatter: `${lv.golden ? "★" : ""}${lv.label}`,
        color: col, fontSize: 9, fontWeight: isKey ? "bold" : "normal",
        position: "insideStartTop",
        // 底衬：标签会压到K线柱体上，无底衬读不清。图表在 .modal 的白底上，
        // 故用白底 + 同色描边（深色底衬在浅色主题里是突兀的黑块）
        backgroundColor: "rgba(255,255,255,.92)", borderColor: col, borderWidth: 1,
        borderRadius: 3, padding: [1, 3] },
    };
  }) : [];
  // 锚点连线：标出这段比例位取自哪一段摆动，便于人工核查
  const fibAnchor = fibActive
    && fib.swing.low.index < period.dates.length && fib.swing.high.index < period.dates.length
    ? {
      name: "黄金分割锚点", type: "line", xAxisIndex: 0, yAxisIndex: 0,
      data: period.dates.map((_, i) => i === fib.swing.low.index ? fib.swing.low.price
        : i === fib.swing.high.index ? fib.swing.high.price : null),
      connectNulls: true, silent: true, z: 2,
      symbol: "circle", symbolSize: 5,
      lineStyle: { color: "#8b949e", width: 1, type: "dotted", opacity: .55 },
      itemStyle: { color: "#8b949e" },
    } : null;

  // 初始视窗：按「根数」而非百分比 —— 后端有 800 根日线，按百分比会让每根蜡烛只剩 ~3px。
  //   ① 用户手动缩放过 → 原样沿用（10s 刷新重建图表时不能被弹回默认窗口）
  //   ② 用户显式选过区间 → 严格按所选根数（显式选择优先，不为锚点放宽）
  //   ③ 默认 → 按 DEFAULT_BARS 根；万一锚点更早再放宽到能看见它
  //      （锚点连线落在视窗外则比例位来源无从核对）
  const nBars = period.dates.length;
  const rangeSel = stockChartRange[stockChartPeriod];   // undefined=默认 null=全部
  let zStart;
  if (stockChartZoom) {
    zStart = stockChartZoom.start;
  } else if (rangeSel !== undefined) {
    zStart = barsToZStart(nBars, rangeSel);
  } else {
    zStart = barsToZStart(nBars, DEFAULT_BARS[stockChartPeriod] || DEFAULT_BARS.daily);
    if (fibActive && nBars > 1) {
      const aMin = Math.min(fib.swing.low.index, fib.swing.high.index);
      const pad = Math.max(3, nBars * 0.02);
      zStart = Math.min(zStart, Math.max(0, (aMin - pad) / (nBars - 1) * 100));
    }
  }
  const zEnd = stockChartZoom ? stockChartZoom.end : 100;

  // 标题：通道(左) + 黄金分割锚点说明(右)
  const titles = [];
  if (data.channel && data.channel.direction !== "flat") {
    titles.push({
      text: (data.channel.direction === "up" ? "↗ 上行通道" : "↘ 下行通道")
        + (data.channel.reversal ? "  ↺ 通道反转" : ""),
      left: 6, top: 2,
      textStyle: { color: data.channel.direction === "up" ? "#f85149" : "#3fb950",
        fontSize: 11, fontWeight: "bold" },
    });
  }
  if (fibActive) {
    const sw = fib.swing, up = fib.direction === "up";
    titles.push({
      text: `黄金分割 · ${up ? "上涨" : "下跌"}摆动${fib.fallback ? "(区间降级)" : ""} `
        + `${up ? `${sw.low.date} 低 → ${sw.high.date} 高` : `${sw.high.date} 高 → ${sw.low.date} 低`}`
        + ` (+${sw.amplitude_pct}%)`,
      right: 6, top: 2,
      textStyle: { color: "#8b949e", fontSize: 10 },
    });
  }

  // 箱体标注（半透明矩形）：根据质量评分着色 — P2前端适配
  // 绿色:quality_score>=7 | 黄色:5-7 | 红色:<5(信号级) | 灰色:无评分(历史)
  const boxes = data.boxes || [];
  const boxIdx = period.dates;  // 当前周期的日期数组
  // 箱体起止是**日线口径的 YYYY-MM-DD**，而分钟周期的时间轴是 "YYYY-MM-DD HH:MM" ⇒
  // 精确 indexOf 在分钟图上必然落空、箱体静默消失。按**日期前缀**取落在区间内的首/末根。
  const _dayOf = (s) => String(s).slice(0, 10);
  const _boxRange = (dates, start, end) => {
    const s = _dayOf(start), e = _dayOf(end);
    let i0 = -1, i1 = -1;
    for (let i = 0; i < dates.length; i++) {
      const d = _dayOf(dates[i]);
      if (d < s) continue;
      if (d > e) break;
      if (i0 < 0) i0 = i;
      i1 = i;
    }
    return i0 < 0 ? null : [i0, i1];
  };
  /* 缺口（2026-10-17 owner：「用颜色将区域显示出来」）—— 逻辑已抽到 `buildGapAreas`，
     六宫格复用同一套口径（2026-10-21 owner：多窗口里也要看到缺口）。 */
  const gapAreas = showGaps ? buildGapAreas(period.ohlc) : [];

  const boxAreas = boxes
    .filter(b => b.low && b.high)
    .map(b => {
      const _r = _boxRange(boxIdx, b.start, b.end);
      if (!_r) return null;
      const i0 = _r[0], i1 = _r[1];
      const isCur = b.rel === 0;

      // P2前端适配：根据quality_score判定颜色
      let boxColor, borderColor, borderWidth;
      let qualityLabel = "";

      if (b.quality_score !== undefined) {
        // 有质量评分（新字段）
        qualityLabel = ` [${b.quality_score.toFixed(1)}/10]`;
        if (b.quality_score >= 7) {
          // 绿色：高质量，非常可靠
          boxColor = isCur ? "rgba(63, 185, 80, .35)" : "rgba(63, 185, 80, .15)";
          borderColor = isCur ? "#3fb950" : "#238636";
          borderWidth = isCur ? 2 : 1;
        } else if (b.quality_score >= 5) {
          // 黄色：中等质量，可参考
          boxColor = isCur ? "rgba(227, 179, 65, .35)" : "rgba(227, 179, 65, .15)";
          borderColor = isCur ? "#e3b341" : "#d29922";
          borderWidth = isCur ? 2 : 1;
        } else {
          // 红色：低质量或信号级，谨慎
          boxColor = isCur ? "rgba(248, 81, 73, .35)" : "rgba(248, 81, 73, .12)";
          borderColor = isCur ? "#f85149" : "#da3633";
          borderWidth = isCur ? 2 : 1;
        }
      } else {
        // 无质量评分（历史数据兼容）
        boxColor = isCur ? "rgba(210,153,34,.35)" : "rgba(139,148,158,.12)";
        borderColor = isCur ? "#d29922" : "#484f58";
        borderWidth = isCur ? 2 : 1;
      }

      // 显示标签：包含完整的质量信息
      const displayLabel = b.display || `${fmt(b.low, 2)}~${fmt(b.high, 2)}`;
      const tag = isCur ? "当前箱体" : (b.rel === -1 ? "上方箱体" : "下方箱体");

      return [{ name: `${tag} ${displayLabel} (${b.days || ""}天)${qualityLabel}`,
                xAxis: i0, yAxis: b.low, itemStyle: { color: boxColor,
                  borderColor: borderColor, borderWidth: borderWidth, borderType: isCur ? "solid" : "dashed" } },
              { xAxis: i1, yAxis: b.high }];
    })
    .filter(Boolean);

  // 2026-10-20 owner：「K线中显示5日均线纯属多余，去掉」——**撤掉当天上午加的 MA5 特殊标记**
  // （金色加粗+微光 / z 抬到蜡烛之上 / 线尾「MA5(日) xx」胶囊）。
  // 理由：MA5 本来就在图例和 tooltip 里，再单独描一条金线加个数字胶囊只是噪声，
  // 而且那个胶囊正好挂在右端、跟现价胶囊抢位置。
  // ⚠️ **MA5 这条均线本身保留**（它是标准均线之一），只是恢复成与其它均线同款细线。
  //    「分钟周期 MA5 = 5 根该周期 K 线」这个口径仍由 tooltip 和「日线MA5」蓝虚线解释。
  /* 成交额（亿元）+ 它的 5/10 均线（2026-10-20 owner：量能窗口要「美化、醒目」）。
     单根柱子只能看出"今天放没放量"，**均线才看得出量能趋势**（东财的量能面板就是
     柱子 + 两条均线）。单位统一换成亿 —— 和 y 轴刻度同一口径，不用心算。
     没有成交额（老 payload）时整块为 null，下面的 series 自动回落成交量。 */
  const _amtYi = (Array.isArray(period.amount) && period.amount.length === period.dates.length)
    ? period.amount.map(v => (v == null || isNaN(Number(v))) ? null : Number(v) / 1e8)
    : null;
  const _maOf = (arr, n) => {
    if (!arr) return null;
    const out = new Array(arr.length).fill(null);
    let sum = 0, cnt = 0;
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i];
      if (v != null) { sum += v; cnt++; }
      if (i >= n) { const old = arr[i - n]; if (old != null) { sum -= old; cnt--; } }
      if (i >= n - 1 && cnt === n) out[i] = Math.round(sum / n * 100) / 100;
    }
    return out;
  };
  const _amtMa5 = _maOf(_amtYi, 5);
  const _amtMa10 = _maOf(_amtYi, 10);

  const maSeries = period.ma.map((maArr, i) => {
    const _color = MA_COLORS[i];
    return {
      name: `MA${[5,10,20,30,60,180,365][i]}`, type: "line", data: maArr, symbol: "none",
      z: 2,
      lineStyle: { width: 1, color: _color },
      // ⚠️ **必须同时给 itemStyle.color**（2026-10-16 owner 报"图例颜色和线不一样"）：
      //    ECharts 的**图例图标**取 `itemStyle.color`，只给 `lineStyle.color` 时图例回落到
      //    默认调色板 ⇒ 图例是 A 色、线是 B 色。凡是想让图例跟着线走的 series 都要两处同色。
      itemStyle: { color: _color }, connectNulls: true,
    };
  });

  // 复用实例：原来每次渲染都 dispose+init 整个 ECharts（每 10s 一次 + 每次勾选图层一次），
  // 1650 根K线 × ~16 条 series 重建一遍 ⇒ 明显卡顿。改为只 setOption，用 replaceMerge:["series"]
  // 处理勾选导致的 series 数量变化，实例/DOM/dataZoom/交互状态全部保留。
  // dispose 只发生在三处：closeStockChart、openStockChart 换票、缺数据守卫。
  const el = document.getElementById("stockChart");
  const _firstInit = !stockChartInst;
  if (_firstInit) {
    // 清掉占位文本（'加载中...' / '该周期无分时数据'）：echarts.init 是**追加**一个 div，
    // 不清的话占位文字会一直留在图下面。仅首帧清 —— 复用时清会毁掉画布。
    el.innerHTML = "";
    stockChartInst = echarts.init(el);
    window.stockChartInst = stockChartInst;  // 暴露供调试/检查
    // 容器尺寸一变就 resize（最大化 / 还原 / 窗口拉伸都覆盖；见 _attachChartResize 注释）
    _attachChartResize(el);
  }

  // 背离点：后端按周期给 {index,type(顶/底),price,consec,dif}；只画落在当前序列内的。
  const divEvents = (showDiv && data.divergence && data.divergence[stockChartPeriod]) || [];
  const divByIndex = {};
  divEvents.forEach(e => {
    if (e && e.index >= 0 && e.index < period.dates.length) divByIndex[e.index] = e;
  });
  const divSeries = Object.keys(divByIndex).length ? [{
    name: "背离", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, z: 12,
    data: Object.values(divByIndex).map(e => {
      const top = e.type === "顶";
      return {
        value: [e.index, e.price],
        // 顶背离：红色倒三角压在峰顶上方；底背离：绿色正三角托在谷底下方。
        symbol: "triangle", symbolRotate: top ? 180 : 0, symbolSize: 12,
        symbolOffset: [0, top ? -14 : 14],
        itemStyle: { color: top ? "#f85149" : "#3fb950", borderColor: "#fff", borderWidth: 1 },
        label: {
          show: true, formatter: (top ? "顶背离" : "底背离")
            + (e.source === "hist_fade" ? "·柱" : "") + (e.consec ? "·连" : ""),
          position: top ? "top" : "bottom", distance: 4, fontSize: 9, fontWeight: "bold",
          color: top ? "#f85149" : "#3fb950",
          backgroundColor: "rgba(255,255,255,.92)",
          borderColor: top ? "#f85149" : "#3fb950", borderWidth: 1,
          borderRadius: 3, padding: [1, 3],
        },
      };
    }),
  }] : [];

  /* ---- 成交买卖点 B/S 标记（2026-10-21 owner）----
     后端 payload.trades = [{ts, side:"B"/"S", qty, price, account}]（当前图表 code 的
     券商真实成交行，时间升序）。叠在 K 线主图的 scatter 系列（不用 markPoint ——
     bar 索引映射更稳）。
     归桶口径：分钟 K 线时间是**右端**（14:55 的 5min 根覆盖 14:50~14:55），
     成交按 (bar_t - period分钟, bar_t] 归桶（14:55:27 ⇒ 15:00 那根）；
     日线按日期精确落；周/月落进 (上一根日期, 本根日期] 区间。
     序列范围外的成交不画；time 为空的老行（ts 只带日期）分钟图跳过。 */
  const _trades = Array.isArray(data.trades) ? data.trades : [];
  const _pMin = ({ min5: 5, min10: 10, min30: 30, min60: 60 })[stockChartPeriod] || 0;
  const tradeByIndex = {};   // "barIndex|B"/"barIndex|S" → {side, qty, amt, count, accounts}
  if (_trades.length) {
    const _dates = period.dates, _n = _dates.length;
    // 预计算每根的 (日期, 右端分钟数)；日/周/月只用日期比，右端分钟恒 0 不参与。
    const _barDate = new Array(_n), _barEnd = new Array(_n);
    for (let i = 0; i < _n; i++) {
      const s = String(_dates[i]);
      _barDate[i] = s.slice(0, 10);
      _barEnd[i] = _pMin
        ? Number(s.slice(11, 13)) * 60 + Number(s.slice(14, 16))
          + (Number(s.slice(17, 19)) || 0) / 60
        : 0;
    }
    let bi = 0;   // 双指针：trades 与 bar 同为升序，单边推进不回退
    for (const t of _trades) {
      const ts = String((t && t.ts) || "");
      const tDate = ts.slice(0, 10);
      if (!tDate) continue;
      if (_pMin) {
        if (ts.length < 19) continue;              // 老行无成交时间 ⇒ 分钟图跳过
        const tMin = Number(ts.slice(11, 13)) * 60 + Number(ts.slice(14, 16))
          + (Number(ts.slice(17, 19)) || 0) / 60;
        while (bi < _n && (_barDate[bi] < tDate
          || (_barDate[bi] === tDate && _barEnd[bi] < tMin))) bi++;
        if (bi >= _n) break;                        // 之后的成交更晚，必然也出界
        // 落在 (bar_t - p, bar_t] 之外（如早于当日首根 / 午休空档）⇒ 不属于任何桶
        if (_barDate[bi] !== tDate || _barEnd[bi] - _pMin >= tMin) continue;
      } else {
        while (bi < _n && _barDate[bi] < tDate) bi++;
        if (bi >= _n) break;
        if (stockChartPeriod === "daily") {
          if (_barDate[bi] !== tDate) continue;     // 日线按日期精确落
        } else if (bi === 0) {
          // 周/月首根起点未知 ⇒ 按周期估下界（周 7 天 / 月 31 天），早于它的不画
          const _lb = new Date(_barDate[0] + "T00:00:00");
          _lb.setDate(_lb.getDate() - (stockChartPeriod === "weekly" ? 7 : 31));
          const _lbS = _lb.getFullYear() + "-" + String(_lb.getMonth() + 1).padStart(2, "0")
            + "-" + String(_lb.getDate()).padStart(2, "0");
          if (tDate < _lbS) continue;
        }
      }
      const side = t.side === "S" ? "S" : "B";
      const k = bi + "|" + side;
      const q = Number(t.qty) || 0, p = Number(t.price) || 0;
      // 同一根 bar 同方向多笔 ⇒ 聚合成一个标记：数量合计、均价按数量加权、笔数进 tooltip
      const agg = tradeByIndex[k]
        || (tradeByIndex[k] = { side, qty: 0, amt: 0, count: 0, accounts: {} });
      agg.qty += q; agg.amt += p * q; agg.count += 1;
      if (t.account) agg.accounts[t.account] = true;
    }
  }
  // 买=红 ▲ 托在该根 low 下方；卖=绿 ▼ 压在该根 high 上方。
  // 配色依据：与全图涨跌语义同源的 #f85149/#3fb950（涨红跌绿 A 股惯例，K线柱体、
  // 背离三角、支撑压力线同一对色，已在该深底画布 #0d1117 上通过 dataviz 校验），
  // 白描边保证压在红/绿蜡烛上仍读得出。
  const tradeSeries = Object.keys(tradeByIndex).length ? [{
    name: "成交", type: "scatter", xAxisIndex: 0, yAxisIndex: 0, z: 13,
    data: Object.keys(tradeByIndex).map(k => {
      const i = Number(k.split("|")[0]);
      const a = tradeByIndex[k];
      const buy = a.side === "B";
      const bar = period.ohlc[i] || [];
      const anchor = buy ? bar[2] : bar[3];          // ohlc=[open,close,low,high]
      if (anchor == null) return null;
      return {
        value: [i, anchor],
        symbol: "triangle", symbolRotate: buy ? 0 : 180, symbolSize: 11,
        symbolOffset: [0, buy ? 13 : -13],
        itemStyle: { color: buy ? "#f85149" : "#3fb950",
                     borderColor: "#fff", borderWidth: 1 },
      };
    }).filter(Boolean),
  }] : [];

  if (_applyZoom) stockChartAppliedZoom = { start: zStart, end: zEnd };
  stockChartZoomGuardUntil = Date.now() + 500;   // 忽略本次 setOption 补发的 dataZoom 事件
  _chartLog("render", JSON.stringify({
    applyZoom: !!applyZoom, _applyZoom: _applyZoom, hadInst: _hadInst,
    period: stockChartPeriod, zoom: stockChartZoom, zStart: zStart, zEnd: zEnd,
    n: nBars, first: (period.dates || [])[0], last: (period.dates || []).slice(-1)[0],
  }));
  stockChartInst.setOption({
    // 2026-10-17 owner：K线图用**深色画布**（独立的深色主题，不是把浅色反相）。
    backgroundColor: "#0d1117",
    // 2026-10-17：首帧播一次入场动画（只有初次 init 时），后续 10s 重绘保持静态，避免闪。
    animation: _firstInit, animationDuration: 340, animationEasing: "cubicOut",
    // 2026-10-16 owner：「打开K线图只默认开启 5日/10日/20日均线」。
    // ⚠️ 两条纪律：
    //   ① `selected` **只列要关掉的项** —— ECharts 对未列出的项默认 true，所以 K线/成交量/
    //      MACD/RSI/支撑压力/通道 不受影响（列全了反而会把它们一起关掉）。
    //   ② `selected` **只在首帧给** —— 图每 10s 会 `setOption` 重绘一次，每次都带
    //      `selected` 会把**用户手动勾的图例选择冲掉**。首帧 = `echarts.init` 那一次，
    //      而"换票/关窗"都会 dispose ⇒ 每次**重新打开**都回到这套默认，正是 owner 要的。
    legend: {
      // 2026-10-17：原 top:0 且无 left/right —— 和左上角的「通道/黄金分割」标题**叠在一起**，
      // 一长串图例挤成一坨（用户截图里的"显示不够好"有一半是这个）。挪到标题下方并限宽。
      top: 20, left: 8, right: 8, itemGap: 9, itemWidth: 14, itemHeight: 8,
      textStyle: { color: "#8b949e", fontSize: 10 }, type: "scroll",
      ...(_firstInit ? {
        selected: {
          MA30: false, MA60: false, MA180: false, MA365: false,
          BOLL中: false, BOLL上: false, BOLL下: false,
        },
      } : {}),
    },
    // 恒常给 title（哪怕空数组）：实例复用后不再 dispose，缺省时旧标题会残留在图上
    title: titles,
    tooltip: { trigger: "axis", axisPointer: { type: "cross" }, backgroundColor: "rgba(22,27,34,.96)",
      borderColor: "#30363d", borderWidth: 1, borderRadius: 8, padding: [8, 10],
      extraCssText: "box-shadow:0 10px 28px rgba(15,23,42,.28);backdrop-filter:blur(2px);",
      textStyle: { color: "#e6edf3", fontSize: 11 },
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
        // 成交额（2026-10-20 owner 要的是**成交额**不是成交量）：有就用它，没有才回落"手"
        const _amtV = (period.amount && period.amount[i] != null) ? Number(period.amount[i]) : null;
        const amtTxt = (_amtV != null && !isNaN(_amtV)) ? (_amtV / 1e8).toFixed(2) + " 亿" : null;
        // 2026-10-20 owner：「10 分图上的文字看不懂，更没法用来决策」⇒ 每个数字都带**标签+单位**，
        // 且把「本周期均线」与「日线虚线」分成两组讲清口径，再给两句能直接读的判定。
        // ⚠️ `_dma5/_dma10/_dma20` 是**最新**日线的值（不是这根 K 线当天的）⇒ 只在
        //    悬停的是最后一个交易日的桶时才显示，否则等于把未来的线画到过去上。
        // 价格**定宽**输出（与本仓「涨跌幅必须两位」同一条纪律）：`fmt` 是最大小数位，
        // 会把 58.10 打成 58.1、几行数字对不齐；ETF/低价标的（<5 元）用三位小数，别把 0.612 压成 0.61。
        const _px = (v) => (v == null || v === "" || isNaN(Number(v))) ? "—"
          : Number(v).toFixed(Math.abs(Number(v)) < 5 ? 3 : 2);
        const _maLab = ["MA5", "MA10", "MA20", "MA30", "MA60", "MA180", "MA365"];
        const _maCells = (period.ma || []).map((arr, j) =>
          arr[i] != null ? `${_maLab[j]} <b>${_px(arr[i])}</b>` : null).filter(Boolean);
        const _ma5 = (period.ma && period.ma[0]) ? period.ma[0][i] : null;
        const _ma20 = (period.ma && period.ma[2]) ? period.ma[2][i] : null;
        // 这根 K 线自身的强度：收盘落在当根区间的哪个位置（0%=最低，100%=最高）
        const _posBar = (hi != null && lo != null && hi > lo) ? (c - lo) / (hi - lo) * 100 : null;
        const _posBarTxt = _posBar == null ? ""
          : `收在当根区间 <b>${_posBar.toFixed(0)}%</b>`
            + (_posBar >= 70 ? "（贴住上沿，盘中偏强）"
              : _posBar <= 30 ? "（贴住下沿，盘中偏弱）" : "（中段）");
        const _vsMa = (ma, name) => (ma == null) ? ""
          : `　·　收在 ${name} <b>${c >= ma ? "上方" : "下方"} ${Math.abs((c - ma) / ma * 100).toFixed(2)}%</b>`;
        const _dayTxt = _dayOf(period.dates[i]);
        const _lastDaily = (((data.period_data || {}).daily || {}).dates || []).slice(-1)[0];
        const _dailyRef = (_dma5 != null && _lastDaily && _dayTxt === _dayOf(_lastDaily))
          ? `<div style="color:#8b949e;font-size:10px;margin-top:3px">日线参考（图上蓝/紫/青虚线，取最新收盘）：`
            + `MA5 <b>${_px(_dma5)}</b>`
            + (_dma10 != null ? ` · MA10 <b>${_px(_dma10)}</b>` : "")
            + (_dma20 != null ? ` · MA20 <b>${_px(_dma20)}</b>` : "") + `</div>`
          : "";
        let html = `<div style="font-weight:bold;margin-bottom:3px">${period.dates[i]}`
          + `<span style="font-weight:400;opacity:.65"> · ${_periodCn}K</span></div>`
          + `<div>开 <b>${_px(o)}</b>　高 <b>${_px(hi)}</b>　低 <b>${_px(lo)}</b>　`
          + `收 <b style="color:${col}">${_px(c)}</b>　涨跌 <b style="color:${col}">${chgTxt}</b></div>`
          + (amtTxt ? `<div>成交额 <b>${amtTxt}</b></div>`
                     : `<div>成交 <b>${volTxt}</b> 手</div>`)
          + (_posBarTxt ? `<div style="color:#8b949e">${_posBarTxt}${_vsMa(_ma5, "MA5")}${_vsMa(_ma20, "MA20")}</div>` : "");
        if (_maCells.length) {
          html += `<div style="border-top:1px solid #30363d;margin-top:4px;padding-top:4px;color:#8b949e;font-size:10px">`
            + `均线（本周期口径：MA5 = 5 根${_periodCn}K${_isIntradayPeriod
              ? "，" + ({"min5": 25, "min10": 50, "min30": 150, "min60": 300})[stockChartPeriod] + " 分钟" : ""}）</div>`
            + `<div>${_maCells.join("　")}</div>`;
        }
        html += _dailyRef;

        // 背离标注：与图上的三角标记同源（backend divergence[period]）
        const _divE = divByIndex[i];
        if (_divE) {
          const _dc = _divE.type === "顶" ? "#f85149" : "#3fb950";
          html += `<div style="color:${_dc};font-weight:bold;margin-top:2px">`
            + `${_divE.type}背离${_divE.source === "hist_fade" ? "（柱线动量衰减）" : ""}`
            + `${_divE.consec ? "（连续）" : ""} · DIF ${_divE.dif}`
            + (_divE.hist != null ? ` · 柱 ${_divE.hist}` : "") + `</div>`;
        }

        // 成交买卖点（2026-10-21）：与图上 ▲/▼ 三角同源（payload.trades 按桶聚合）。
        // 同根同向多笔已合并 ⇒ 显示合计数量、数量加权均价、笔数与账户。
        const _trB = tradeByIndex[i + "|B"], _trS = tradeByIndex[i + "|S"];
        if (_trB || _trS) {
          const _trRow = (a, buy) => {
            const avg = a.qty ? a.amt / a.qty : 0;
            const accs = Object.keys(a.accounts).join("/");
            return `<div style="color:${buy ? "#f85149" : "#3fb950"};font-weight:bold">`
              + `${buy ? "▲ 买入" : "▼ 卖出"} <b>${a.qty}</b> 股 @ <b>${_px(avg)}</b>`
              + `<span style="font-weight:400;opacity:.7">`
              + ` · ${a.count > 1 ? a.count + " 笔" : "1 笔"}${accs ? " · " + accs : ""}</span></div>`;
          };
          html += `<div style="border-top:1px solid #30363d;margin-top:4px;padding-top:4px">`
            + (_trB ? _trRow(_trB, true) : "") + (_trS ? _trRow(_trS, false) : "") + `</div>`;
        }

        // P2前端优化：显示当前日期所在的箱体质量信息
        // 比到「天」：分钟周期的 curDate 带时间("2026-09-30 15:00")，直接与日线口径的
        // b.end("2026-09-30") 比字符串会因空格排在末尾而判 false ⇒ 末根K线漏判箱体。
        const curDate = _dayOf(period.dates[i]);
        const boxesAtDate = boxes.filter(b => {
          try {
            return curDate >= _dayOf(b.start) && curDate <= _dayOf(b.end);
          } catch (e) {
            return false;
          }
        });

        if (boxesAtDate.length > 0) {
          html += `<div style="border-top:1px solid #30363d;margin-top:4px;padding-top:4px;color:#8b949e;font-size:10px">`;
          boxesAtDate.forEach(b => {
            const qs = b.quality_score !== undefined ? `质量${b.quality_score.toFixed(1)}/10` : "质量未知";
            const wg = b.width_grade || "—";
            const tg = b.touch_grade || "—";
            const disp = b.display ? `${b.display}` : `${wg}箱 触及${tg}`;
            html += `<div>📦 ${disp}</div>`;
          });
          html += `</div>`;
        }

        // 黄金分割：精确价格 / 支撑阻力 / 距现价 % —— 图上标签已精简为只写比例，细节放这里
        if (fibActive) {
          html += `<div style="border-top:1px solid #30363d;margin-top:4px;padding-top:4px;font-size:10px">`
            + `<div style="color:#8b949e">黄金分割 · ${fib.direction === "up" ? "上涨" : "下跌"}摆动</div>`;
          fib.levels.forEach(lv => {
            const c = lv.side === "support" ? "#3fb950" : "#f85149";
            const d = (cur && lv.price) ? (lv.price - cur) / cur * 100 : null;
            html += `<div style="color:${c}">${lv.golden ? "★" : ""}${lv.label}`
              + ` <b>${lv.price}</b>`
              + (d != null ? ` <span style="opacity:.65">${d >= 0 ? "+" : ""}${d.toFixed(2)}%</span>` : "")
              + ` ${lv.side === "support" ? "支撑" : "阻力"}</div>`;
          });
          html += `</div>`;
        }

        return html;
      } },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    // 2026-10-17：整套轴/网格原来用的是 **GitHub 深色 token**（#30363d/#8b949e），
    // 而这个弹窗是**白底** ⇒ 轴线和标签都发灰、图像"平"。改成浅底墨色。
    // right: 20→58（2026-10-20）：给「现价胶囊」腾出**网格外**的位置，四个 grid 必须同值
    // 否则四个窗口的 x 轴刻度会错位（它们共享 dataZoom，左边界同源）。
    // 2026-10-20：量能窗口 9%→10%、MACD 10%→12%（主图让出 2%）—— MACD 要同时放
    // 柱 + 两条线 + 零轴，10% 太挤，柱子几乎贴着线看不出形态。
    grid: [
      { left: 60, right: 58, top: 50, height: "48%" },   // 主图: K线+MA+BOLL+支撑压力+箱体
      { left: 60, right: 58, top: "58%", height: "10%" }, // 成交额(独立窗口)
      { left: 60, right: 58, top: "69%", height: "12%" }, // MACD
      { left: 60, right: 58, top: "82%", height: "10%" }, // RSI
    ],
    xAxis: [
      { type: "category", data: period.dates, gridIndex: 0, axisLine: { lineStyle: { color: "#2b333d" } },
        axisLabel: { show: false } },
      { type: "category", data: period.dates, gridIndex: 1, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#2b333d" } } },
      { type: "category", data: period.dates, gridIndex: 2, axisLabel: { show: false }, axisLine: { lineStyle: { color: "#2b333d" } } },
      { type: "category", data: period.dates, gridIndex: 3, axisLabel: { color: "#8b949e", fontSize: 9 }, axisLine: { lineStyle: { color: "#2b333d" } } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, axisLabel: { color: "#8b949e", fontSize: 9 },
        splitLine: { lineStyle: { color: "rgba(139,148,158,.14)" } } },
      // 成交额窗口的 y 轴：有成交额时显示「亿」刻度（否则保持不显示，避免把手当亿）
      { scale: true, gridIndex: 1,
        axisLabel: (Array.isArray(period.amount) && period.amount.length)
          ? { color: "#8b949e", fontSize: 9,
              formatter: v => (Math.abs(v) >= 10 ? v.toFixed(0) : v.toFixed(1)) + "亿" }
          : { show: false },
        splitLine: { show: false } },
      { scale: true, gridIndex: 2, axisLabel: { show: false }, splitLine: { show: false } },
      { min: 0, max: 100, gridIndex: 3, axisLabel: { color: "#6e7681", fontSize: 9 }, splitLine: { show: false } },
    ],
    ...(_applyZoom ? { dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2, 3], start: zStart, end: zEnd,
        // 2026-10-20：显式打开滚轮缩放/拖动平移，避免被外层滚动容器吞掉
        zoomOnMouseWheel: true, moveOnMouseMove: true, moveOnMouseWheel: false,
        preventDefaultMouseMove: true },
      { type: "slider", xAxisIndex: [0, 1, 2, 3], bottom: 0, height: 16, start: zStart, end: zEnd,
        // 2026-10-17 视觉：滑块默认灰扑扑的一坨，给它统一的浅底 + 品牌色手柄
        backgroundColor: "#161b22", borderColor: "#30363d",
        fillerColor: "rgba(248,81,73,.15)", handleStyle: { color: "#30363d", borderColor: "#f0a05a" },
        moveHandleStyle: { color: "#e8531f", opacity: .35 },
        textStyle: { color: "#6e7681", fontSize: 9 }, dataBackground: { lineStyle: { color: "#30363d" }, areaStyle: { color: "#161b22" } } },
    ] } : {}),
    series: [
      { name: "K线", type: "candlestick", data: period.ohlc, xAxisIndex: 0, yAxisIndex: 0,
        barMaxWidth: 12,   // 根数很少时别把蜡烛拉成巨型方块；根数多时仍自动变细
        // 浅底上 #f85149/#3fb950 偏浅 ⇒ 换成对比更够的一组（涨红跌绿仍是 A 股惯例）
        itemStyle: { color: "#f85149", color0: "#3fb950",
                     borderColor: "#ff6b62", borderColor0: "#56d364", borderWidth: 1 },
        // 箱体 + 缺口共用一个 markArea（ECharts 每个 series 只认一个）
        markArea: (() => {
          const _d = [...(showBoxes ? boxAreas : []), ...gapAreas];
          return _d.length ? { silent: true, data: _d } : undefined;
        })(),
        markLine: { symbol: "none",
          data: [
            ...(showLevels ? [...resistanceLines, ...supportLines] : []), currentLine,
            ...(dailyMa5Line ? [dailyMa5Line] : []),
            ...(dailyMa10Line ? [dailyMa10Line] : []),
            ...(dailyMa20Line ? [dailyMa20Line] : []),
          ],
          label: { position: "insideEndTop" } } },
      ...divSeries,
      ...tradeSeries,
      ...(fibAnchor ? [fibAnchor] : []),
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
        lineStyle: { color: "rgba(139,148,158,.5)", width: 1 },
        itemStyle: { color: "rgba(139,148,158,.5)" } },
      { name: "BOLL上", type: "line", data: period.boll.up, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        lineStyle: { color: "rgba(139,148,158,.3)", width: 1 },
        itemStyle: { color: "rgba(139,148,158,.3)" } },
      { name: "BOLL下", type: "line", data: period.boll.dn, xAxisIndex: 0, yAxisIndex: 0, symbol: "none",
        lineStyle: { color: "rgba(139,148,158,.3)", width: 1 },
        itemStyle: { color: "rgba(139,148,158,.3)" } },
      // 黄金分割比例位：用**独立载体 series** 承载 markLine。
      // 挂在 K线 自身的 markLine 上会被K线柱体压住，标签读不清（实测 markLine.zlevel
      // 也无效）；改挂到 K线 之后的独立 series 并把 z 抬高 ⇒ 标签才画在柱体之上。
      ...(fibLines.length ? [{
        name: "黄金分割", type: "line", data: [], xAxisIndex: 0, yAxisIndex: 0,
        silent: true, symbol: "none", z: 10,
        markLine: { symbol: "none", silent: true, z: 10, data: fibLines,
          label: { position: "insideStartTop" } },
      }] : []),
      // 成交额独立窗口（2026-10-20 owner：「显示成交额，我不要成交量」+「更美化、醒目」）
      // 后端下发 amount（元）；老 payload 没有时回落成交量（手），图例名会把口径写清楚，
      // 免得把「手」当「亿元」看（这正是 owner 报「量能显然不对」的由来）。
      (() => {
        const _hasAmt = Array.isArray(period.amount) && period.amount.length === period.dates.length;
        const _vals = _hasAmt ? _amtYi : period.volume;
        return { name: _hasAmt ? "成交额(亿)" : "成交量(手)", type: "bar",
          xAxisIndex: 1, yAxisIndex: 1, barWidth: "60%", barMaxWidth: 12,
          // 2026-10-17：原来一律是半透明橙色 ⇒ 看不出放量是买还是卖。改为**按当日涨跌着色**。
          // 2026-10-20：不透明度 .55→.82 提亮（深底上 .55 发灰、量能看不清），圆角 2px。
          itemStyle: { borderRadius: [2, 2, 0, 0] },
          data: _vals.map((v, i) => {
            const o = period.ohlc[i];
            const prev = i > 0 ? period.ohlc[i - 1][1] : null;
            const up = !o ? true : (prev == null ? o[1] >= o[0] : o[1] >= prev);
            return { value: v, itemStyle: { color: up ? "rgba(248,81,73,.82)" : "rgba(63,185,80,.82)" } };
          }) };
      })(),
      // 量能均线（只在有成交额时出现）：柱看"这一根"，线看"趋势"。
      // 配色跑过 dataviz 校验器（dark：#c98500↔#9085e9，CVD ΔE 27.3 / 常视觉 27.5，全通过）。
      ...(_amtMa5 ? [
        { name: "成交额MA5", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: _amtMa5,
          symbol: "none", connectNulls: true, z: 3,
          lineStyle: { width: 1.3, color: "#c98500", opacity: .95 }, itemStyle: { color: "#c98500" } },
        { name: "成交额MA10", type: "line", xAxisIndex: 1, yAxisIndex: 1, data: _amtMa10,
          symbol: "none", connectNulls: true, z: 2,
          lineStyle: { width: 1.3, color: "#9085e9", opacity: .9 }, itemStyle: { color: "#9085e9" } },
      ] : []),
      // MACD 窗口（2026-10-20 owner：更美化、醒目）
      // ⚠️ 原来 DIF `#ff8c5a` / DEA `#f85149` 是**两个几乎一样的暖红** —— 深底细线上
      //    根本分不出哪条是哪条（这是真正的可读性缺陷，不只是"不好看"）。
      //    换成 dataviz 校验器验过的 蓝 `#3987e5` ↔ 品红 `#d55181`（dark：CVD ΔE 15.9、
      //    常视觉 26.5，亮度带/对比度全 PASS）。柱仍按正负红绿，与全图涨跌语义一致。
      { name: "MACD-DIF", type: "line", data: period.macd.dif, xAxisIndex: 2, yAxisIndex: 2,
        symbol: "none", z: 3, lineStyle: { color: "#3987e5", width: 1.4 } },
      { name: "MACD-DEA", type: "line", data: period.macd.dea, xAxisIndex: 2, yAxisIndex: 2,
        symbol: "none", z: 3, lineStyle: { color: "#d55181", width: 1.4 } },
      { name: "MACD柱", type: "bar", data: period.macd.hist, xAxisIndex: 2, yAxisIndex: 2,
        z: 1, itemStyle: { color: p => p.value >= 0 ? "rgba(248,81,73,.9)" : "rgba(63,185,80,.9)",
          borderRadius: [1, 1, 0, 0] },
        // 零轴：MACD 的正负分界，没这条基准线时柱子的"翻红/翻绿"只能靠猜
        markLine: { silent: true, symbol: "none",
          data: [{ yAxis: 0, lineStyle: { color: "rgba(139,148,158,.45)", type: "dashed", width: 1 },
                   label: { show: false } }] } },
      // RSI 窗口
      { name: "RSI", type: "line", data: period.rsi, xAxisIndex: 3, yAxisIndex: 3,
        symbol: "none", lineStyle: { color: "#bc8cff", width: 1 },
        markLine: { symbol: "none", data: [{ yAxis: 30, lineStyle: { color: "rgba(139,148,158,.4)", type: "dashed" } },
          { yAxis: 70, lineStyle: { color: "rgba(139,148,158,.4)", type: "dashed" } }] } },
    ],
  }, { replaceMerge: ["series", "title"], lazyUpdate: true });

  // 现价胶囊：**等一帧**再定位 —— 主 setOption 是 lazyUpdate，同一 tick 里 convertToPixel
  // 拿到的还是上一帧的 y 轴刻度，直接定位会看到胶囊先跳一下再归位。
  _scheduleNowTag(stockChartInst, cur, _nowCol);

  // 记住用户手动缩放。注意：**不要用 getOption()** —— 它会深拷贝整个 option
  // （1650 根K线 × ~16 条 series），而拖动/滚轮缩放会连续触发本事件 ⇒ 实测就是卡顿主因之一。
  // dataZoom 事件本身带 start/end 百分比，直接读。
  // 程序化 setOption 也会触发本事件；用**模块级** stockChartAppliedZoom 记住刚设进去的值来区分，
  // 不能用本函数的闭包变量 —— 处理器只在首次 init 时注册一次，闭包里的值早就是旧的。
  if (_firstInit && stockChartInst && typeof stockChartInst.on === "function") {
    try {
      stockChartInst.on("dataZoom", (ev) => {
        const z = (ev && ev.batch && ev.batch[0]) || ev || {};
        if (typeof z.start !== "number" || typeof z.end !== "number") return;
        const _guard = Date.now() < stockChartZoomGuardUntil;
        _chartLog("dz", JSON.stringify({start: z.start, end: z.end, guard: _guard,
          applied: stockChartAppliedZoom, zoom: stockChartZoom}));
        if (_guard) return;                                 // 程序化 setOption 的回声，忽略
        const a = stockChartAppliedZoom;
        if (a && Math.abs(z.start - a.start) < 0.01 && Math.abs(z.end - a.end) < 0.01) {
          _chartLog("dz_skip_applied", "");
          return;
        }
        stockChartZoom = { start: z.start, end: z.end };
        _chartLog("dz_accept", JSON.stringify(stockChartZoom));
        stockChartInteractingUntil = Date.now() + 1500;   // 交互期间暂停实时重绘
        // 缩放改了 y 轴范围 ⇒ 现价胶囊的**像素**位置也得跟着重算（价格本身没变）
        if (_nowTagState.price != null) {
          _scheduleNowTag(stockChartInst, _nowTagState.price, _nowTagState.color);
        }
      });
    } catch (e) { /* 桩环境 on 不接受回调，忽略 */ }
  }

  // 多空分布（2026-10-20 owner）：十字光标点某根 K 线 → 显示这根里的主动买/主动卖分布。
  // ⚠️ 挂 **zr(画布)** 的 click 而不是 series 的 click：K线/均线/成交量/指标分属不同 grid，
  //    只挂 series 会在副图与空白处漏事件（数据区比蜡烛宽得多）。
  if (_firstInit) {
    try {
      stockChartInst.getZr().on("click", (ev) => {
        const idx = _chartClickIndex(ev);
        if (idx >= 0) showOrderFlowAt(idx);
      });
    } catch (e) { /* 桩环境忽略 */ }
  }
}

/* ---- 10 分周期「多空分布」（2026-10-20 owner）----
   点某根 K 线 → 这根里的主动买(多单) / 主动卖(空单) 分布。
   ⚠️ **只有当天**：腾讯逐笔明细的日期参数实测全被忽略（`&d=`/`&date=`/`&t=` 都回当天），
      历史桶靠后端每天落盘累积（`core/order_flow.py`）。所以点老 K 线时就明说"没有分笔"，
      绝不拿别的口径的数糊上去 —— 与本仓「取不到就显示 —」的一贯做法一致。 */
const FLOW_TTL_MS = 30000;
let _flowCache = { code: null, step: 0, data: null, ts: 0 };

/* 画布点击 → 当前周期的数据下标。-1 = 没点在任何一根上。 */
function _chartClickIndex(ev) {
  if (!stockChartInst || !ev) return -1;
  const pd = stockChartData && stockChartData.period_data
    && stockChartData.period_data[stockChartPeriod];
  const dates = (pd && pd.dates) || [];
  if (!dates.length) return -1;
  try {
    let v = stockChartInst.convertFromPixel({ xAxisIndex: 0 }, [ev.offsetX, ev.offsetY]);
    if (Array.isArray(v)) v = v[0];
    if (v === null || v === undefined) return -1;
    // 类目轴：不同 ECharts 版本可能回下标（number）或类目值（string），两种都收
    if (typeof v === "string" && isNaN(Number(v))) {
      const i = dates.indexOf(v);
      return i;
    }
    const i = Math.round(Number(v));
    return (i >= 0 && i < dates.length) ? i : -1;
  } catch (e) { return -1; }
}

async function showOrderFlowAt(index) {
  const el = document.getElementById("stockFlow");
  const body = document.getElementById("stockFlowBody");
  if (!el || !body) return;
  el.style.display = "block";
  // 只有 5 分 / 10 分两档有分桶（后端按同名周期聚合分笔）；其余周期给明确提示，别静默无反应
  const _step = ({ min5: 5, min10: 10 })[stockChartPeriod];
  if (!_step) {
    body.innerHTML = `<div class="cell-dim">多空分布只做 <b>5 分 / 10 分</b>周期（按同名周期的桶聚合分笔）；`
      + `当前是 <b>${esc(({ daily: "日", weekly: "周", monthly: "月", min30: "30分", min60: "60分" })[stockChartPeriod] || stockChartPeriod)}</b>，`
      + `切到「5分」或「10分」再点 K 线。</div>`;
    return;
  }
  const pd = stockChartData && stockChartData.period_data
    && stockChartData.period_data[stockChartPeriod];
  const label = ((pd && pd.dates) || [])[index];
  if (!label) return;
  const code = stockChartCode;
  body.innerHTML = '<div class="cell-dim">正在取分笔…（首次要翻约 68 页 ≈ 1~2s，之后走增量）</div>';
  try {
    const now = Date.now();
    let r = _flowCache.data;
    if (!r || _flowCache.code !== code || _flowCache.step !== _step
        || now - _flowCache.ts > FLOW_TTL_MS) {
      r = await apiCall("load_order_flow", code, false, _step);
      _flowCache = { code, step: _step, data: r, ts: Date.now() };
    }
    renderOrderFlow(r, label);
  } catch (e) {
    body.innerHTML = `<div class="down">多空分布取数失败：${esc((e && e.message) || e)}</div>`;
  }
}

function renderOrderFlow(r, label) {
  const body = document.getElementById("stockFlowBody");
  if (!body) return;
  if (!r || !r.ok) {
    body.innerHTML = `<div class="cell-dim">多空分布：${esc((r && r.note) || "无数据")}</div>`;
    return;
  }
  const day = String(label).slice(0, 10);
  const hm = String(label).slice(11, 16);
  const b = (r.bars || []).find(x => x.time === hm);
  if (day !== r.date || !b) {
    body.innerHTML = `<div class="cell-dim">多空分布：<b>${esc(label)}</b> 这根没有分笔 —— `
      + `腾讯逐笔只提供<b>当天（${esc(r.date)}）</b>明细，历史要多等几天（后端每天落盘累积）。</div>`;
    return;
  }
  const buy = b.buy_vol, sell = b.sell_vol, neu = b.neutral_vol;
  const tot = buy + sell;
  const br = tot ? (buy / tot * 100) : 0;
  // 净额优先用后端算好的；缺失时用买额−卖额兜底（不为 0 冒充"平盘"）
  const netAmt = (typeof b.net_amt === "number") ? b.net_amt
    : ((b.buy_amt || 0) - (b.sell_amt || 0));
  const netWan = netAmt / 1e4;
  const tierTxt = ["超大单", "大单", "中单", "小单"].map(k => {
    const t = (b.tiers || {})[k] || { buy: 0, sell: 0 };
    const net = (t.buy - t.sell) / 1e4;
    const cls = net > 0 ? "up" : net < 0 ? "down" : "cell-dim";
    return `<span class="${cls}">${k} ${net >= 0 ? "+" : ""}${fmt(net, 0)}万</span>`;
  }).join(" · ");
  body.innerHTML =
    `<div style="font-weight:700;margin-bottom:4px">📊 ${esc(r.date)} ${esc(b.time)} 这根 ${r.step || 10} 分钟的多空分布`
    + ` <span class="cell-dim" style="font-weight:400;font-size:11px">（主动买=多单 / 主动卖=空单 · 逐笔 ${b.n} 笔）</span></div>`
    + `<div style="display:flex;height:8px;border-radius:4px;overflow:hidden;background:#30363d;margin:4px 0 6px">`
    + `<div style="width:${br.toFixed(1)}%;background:#e8531f"></div>`
    + `<div style="flex:1;background:#229652"></div></div>`
    + `<div>主动买 <b class="up">${fmt(buy, 0)}</b> 手（${br.toFixed(1)}%）　`
    + `主动卖 <b class="down">${fmt(sell, 0)}</b> 手（${(100 - br).toFixed(1)}%）　`
    + `<span class="cell-dim">中性 ${fmt(neu, 0)} 手</span></div>`
    + `<div style="margin-top:2px">净额 <b class="${netWan > 0 ? "up" : netWan < 0 ? "down" : "cell-dim"}">`
    + `${netWan >= 0 ? "+" : ""}${fmt(netWan, 0)} 万</b>`
    + `<span class="cell-dim">（买额 − 卖额）</span></div>`
    + `<div style="margin-top:2px">分档净额：${tierTxt}</div>`
    + `<div class="cell-dim" style="margin-top:4px;font-size:11px">`
    + `分档口径：单笔成交额 超大单≥100万 / 大单20~100万 / 中单4~20万 / 小单&lt;4万；`
    + `⚠️ 腾讯「分笔」是约 <b>3 秒聚合的一笔</b>（非交易所逐笔），故分档是近似；`
    + `但 24 根桶的合计<b>与 K 线成交量逐根相等</b>。`
    + `中性含 09:25 集合竞价与 14:57~15:00 收盘竞价（方向无意义，不并入买卖）。`
    + `单位：手/万元；数据源=腾讯分笔（<b>只有当天</b>，历史靠每日落盘累积）。</div>`;
}

/* ---- 选股猎手 ---- */
let hunterLoaded = false;
function trendBadge(t) {
  if (t === "up") return `<span class="badge t-long" title="30分钟趋势 BULL">上行↗</span>`;
  if (t === "down") return `<span class="badge t-short" title="30分钟趋势 BEAR">下行↘</span>`;
  return `<span class="badge chop" title="30分钟趋势 RANGE">震荡→</span>`;
}
// 技术标签徽章（desc 悬停解释文案，与 t_gui._stock_tags_one 标签一一对应）
const TAG_DESC = {
  "上行": "30分钟线趋势=BULL（ADX(14)>22 且上行、EMA20>EMA60 且价在 EMA20 上、Supertrend 未破）：趋势偏多，回踩低吸",
  "下行": "30分钟线趋势=BEAR（ADX(14)>22 且上行、EMA20<EMA60 且价在 EMA20 下、Supertrend 走空）：趋势偏空，反弹减仓、不抄底",
  "震荡": "30分钟线趋势=RANGE（ADX 闸门未开或方向未确认）：无明确方向，按箱体高抛低吸",
  "箱体上沿": "现价处于最近箱体上沿（位置>85%），接近突破点，注意压力",
  "箱体下沿": "现价处于最近箱体下沿（位置<15%），接近支撑/跌破点",
  "箱体内部": "现价在箱体中段运行，方向未定，箱体内高抛低吸",
  "向上突破": "现价向上突破最近箱体上沿（≤8%），第一优先级买入信号(报买入)，回踩上沿不破可加仓",
  "完全突破": "现价明显高于最近箱体上沿（>8%），已完全脱离箱体，突破确认强势（追高需设止损）",
  "跌破下沿": "现价跌破最近箱体下沿，弱势信号，减仓/回避",
  "筑底": "近20日横盘缩量、价格靠近箱体下沿，可能筑底，可关注低吸",
  "筑底中": "近20日横盘、价格靠近箱体下沿，筑底进行中（尚未缩量确认）",
  "筑顶": "近20日横盘、价格靠近箱体上沿，可能筑顶，警惕回落",
  "日线顶背离": "【日线】MACD：价格创新高但 DIF 走弱，上涨动能衰减，警惕回调（背离≠立刻反转）。"
    + "⚠️ 标签带「日线」是要和右边「30min判定」的 30 分钟口径区分开；日线单次背离本身未验证，只作参考",
  "日线底背离": "【日线】MACD：价格创新低但 DIF 走强，下跌动能衰减，可能反弹（背离≠立刻反转）。"
    + "⚠️ 与 30 分钟口径不同源；日线单次背离本身未验证，只作参考",
  "超买": "RSI(14)>70，短期涨幅过大，追高风险高，等回踩",
  "超卖": "RSI(14)<30，短期跌幅过大，可能有反弹，但趋势向下时不抢反弹",
  "破5日线": "【日线口径】今收 < 日线5日均线（破五日线只卖不买）。"
    + "⚠️ 这是**日线**的5日线，不是30分钟图上的 MA5 —— 两者经常一个在上一个在下。",
  "破10日线": "【日线口径】今收 < 日线10日均线，中期走弱。"
    + "⚠️ 这是**日线**的10日线，不是30分钟图上的 MA10。",
};
function tagBadge(t) {
  const colorMap = {
    up: "t-long", down: "t-short", warn: "approach", neutral: "chop",
  };
  const iconMap = {
    "上行": "↗", "下行": "↘", "震荡": "→", "站上5日线": "⬆️",
    "箱体上沿": "🟠", "箱体下沿": "🔵", "箱体内部": "⚪",
    "向上突破": "🚀", "完全突破": "🔺", "跌破下沿": "⚠️", "筑底": "🧱", "筑顶": "⛰️",
    "日线顶背离": "⛔", "日线底背离": "✅", "超买": "⚠", "超卖": "📉",
  };
  const icon = iconMap[t.label] || "";
  // 2026-10-13：破(N)日线 带上**实测数字**（后端 `cur`/`ma`，日线口径）。
  //   owner 三次拿 30 分钟图的 MA5 当"5日线"对账（600362 最近一次：图上 30min MA5=40.94
  //   「站上」，日线 5 日线=41.712「在下方」）。有数字可当场核对，不必反过来怀疑系统。
  const _num = (t.ma != null && t.cur != null && isFinite(t.ma) && isFinite(t.cur))
    ? `　实测：现价 ${t.cur} < 日线均线 ${t.ma}` : "";
  // t.title（2026-10-17）：后端给的可选补充说明 —— 目前用于背离标签带出
  // 「确认于 N 根前（日期）」，否则用户无法判断这条顶背离是今天的还是两周前的。
  const desc = (TAG_DESC[t.label] || t.label) + _num + (t.title ? "　" + t.title : "");
  return `<span class="badge ${colorMap[t.color] || "chop"}" title="${esc(desc)}">${icon}${esc(t.label)}</span>`;
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
// 展开状态/标签缓存模块级保存——分页重渲染后自动恢复展开，避免跳页收起
const hunterOpenSectors = new Set();
const hunterTagsCache = {};

const hunterTagsRetry = {};      // 空结果重试计数（防死循环）
const hunterStructRetry = {};    // 30m MA5（仓位结构）的重试计数（2026-10-17）
const hunterStructCache = {};    // category -> {code: structure}（30m MA5 列，与标签分开存）
/* 猎手展开行的**实时**行情缓存（2026-10-12）。
   ⚠️ 为什么必须有它：猎手 payload 里的 `change_pct` / `build_price` 是**扫描时**快照
   （实测 300821 显示 10-09 收盘的 +20.01%，当天实时只有 +5.83%）⇒ 直接把 payload 的值
   当"当前涨幅"显示就是**错的**。这里按展开页取实时值就地补，带 TTL 便于 10s 轮询刷新。 */
const hunterQuoteCache = {};   // 平铺：code -> {price, change_pct}（一次请求覆盖所有展开板块）
let hunterQuoteInflight = false;   // **全局单飞**：展开与 10s 轮询会同时触发，必须拦
const HUNTER_QUOTE_TTL_MS = 15000;
/* 诊断钩子：`_diagReport` 定义在 `DOMContentLoaded` 的箭头函数**内部**（顶层调不到），
   故在那里把它赋给这个顶层变量。行情这条链"到底断在后端返空还是 DOM 没命中"，
   光看界面分不出来 —— 这条诊断就是为分辨它而留（2026-10-12）。 */
let _hqDiag = null;
/* 只取**当前渲染出来的行**（分页 → 一页 50 行）。板块可达 200 只，全量取数要等很久，
   而用户此刻只看得到这一页。翻页时 `hunterPage` 会重渲染，本函数会被再次调用去补下一页。 */
function _renderedCodesFor(category) {
  const out = [];
  const el = document.getElementById("hunterBody");
  if (!el) return out;
  el.querySelectorAll("tbody.h-sector-group").forEach(tb => {
    if (tb.getAttribute("data-category") !== category) return;
    tb.querySelectorAll(".h-expand-row").forEach(row => {
      const td = row.querySelector("td");
      if (td) out.push(td.textContent.trim());
    });
  });
  return out;
}

async function loadSectorTags(category) {
  // 2026-10-11 提速（owner：展开后列更新很慢）：
  // ① 只取**当前页**（≤50）而不是整个板块（可达 200）；
  // ② 逐 code 增量取，已缓存的跳过（翻页回来不再重复拉）；
  // ③ 后端走 offline（本地日线缓存，零网络）——见 t_gui._stock_tags_one。
  const want = _renderedCodesFor(category);
  if (!want.length) return {};
  const have = hunterTagsCache[category] || {};
  const codes = want.filter(c => !(c in have));
  const _sc = hunterStructCache[category] || {};
  const _structMissing = want.some(c => !(_sc[c] && !_sc[c].pending));   // 30m MA5 还没齐
  if (!codes.length && !_structMissing) return have;
  const _ask = codes.length ? codes : want;
  try {
    // offline=true（2026-10-11）：猎手是**批量视图**，标签走"本地日线缓存"零网络路径
    //（原来逐只 GM 取日线，而 GM 池 max_workers=1 ⇒ 全串行，实测 150 只 44s → 9s）。
    // 建仓表/持仓体检仍走在线路径，行为不变。
    const r = await apiCall("load_stock_tags_batch", _ask, true);
    const tagsMap = (r && r.tags) || {};
    // 30m MA5（2026-10-17）：仓位结构层，与持仓30min体检/风向标**同源**。
    // 走**独立缓存** —— 标签是异步批算（冷启动先空），结构是另一步，两者到达时间不同步。
    const structMap = (r && r.struct) || {};
    if (Object.keys(structMap).length) {
      hunterStructCache[category] = Object.assign(hunterStructCache[category] || {}, structMap);
    }
    // 2026-10-08: 后端**冷启动会先回空 map**（改后台算后），空 map 若被缓存 ⇒ 该板块标签永远空。
    // 故**只缓存非空**；空则稍后重试，等后台算完再填。
    if (Object.keys(tagsMap).length) {
      hunterTagsCache[category] = Object.assign(have, tagsMap);   // 增量并入（翻页不丢已取的）
    } else if ((hunterTagsRetry[category] || 0) < 8) {
      hunterTagsRetry[category] = (hunterTagsRetry[category] || 0) + 1;
      setTimeout(() => {
        if (hunterOpenSectors.has(category)) loadSectorTags(category);
      }, 4000);
    }
    // 30m MA5（仓位结构）比标签慢：只要还有 pending 就 5s 后重试，有界 12 次
    try {
      const _pend = Object.values(structMap).some(v => (v || {}).pending);
      const _rn = hunterStructRetry[category] || 0;
      if (_pend && _rn < 12) {
        hunterStructRetry[category] = _rn + 1;
        setTimeout(() => {
          if (!hunterOpenSectors.has(category)) return;
          apiCall("load_stock_tags_batch", _renderedCodesFor(category), true).then(r2 => {
            const sm2 = (r2 && r2.struct) || {};
            if (!Object.keys(sm2).length) return;
            hunterStructCache[category] = Object.assign(hunterStructCache[category] || {}, sm2);
            const el2 = document.getElementById("hunterBody");
            if (el2) el2.querySelectorAll("tbody.h-sector-group").forEach(tb => {
              if (tb.getAttribute("data-category") === category) applySectorTags(tb, category);
            });
            if (Object.values(sm2).some(v => (v || {}).pending)) loadSectorTags(category);
          }).catch(() => {});
        }, 5000);
      }
    } catch (e) { /* 忽略 */ }
    // 标签到位后，若该板块仍展开则刷新当前可见行（分页跳页后也生效）
    const el = document.getElementById("hunterBody");
    if (el && hunterOpenSectors.has(category)) {
      el.querySelectorAll("tbody.h-sector-group").forEach(tb => {
        if (tb.getAttribute("data-category") === category) applySectorTags(tb, category);
      });
    }
    return tagsMap;
  } catch (e) { return {}; }
}

/* 取**所有展开板块**当前渲染行的并集 code（去重）。一次请求覆盖全部展开板块 ——
   原先是"每个板块各发一次"，展开 5 个板块就是 5 次并发，抢前端在飞位（上限 6）互相排队，
   而这个 GUI 的单次 js_api 基线开销就有 ~1s ⇒ 展开越多越慢。并成一次 = 与展开数无关。 */
function _allRenderedHunterCodes() {
  const out = [];
  const seen = new Set();
  const el = document.getElementById("hunterBody");
  if (!el) return out;
  hunterOpenSectors.forEach(cat => {
    _renderedCodesFor(cat).forEach(c => { if (c && !seen.has(c)) { seen.add(c); out.push(c); } });
  });
  return out;
}

/* 涨幅 >9% 的**特殊标记**（2026-10-20 owner：「选股猎手中涨幅大于 9% 的股票用特殊 logo 标注出来」）。
   数据源**只认真实行情**（`hunterQuoteCache`）：猎手 payload 里的 `change_pct` 是**扫描时**快照，
   可能隔了几天，拿旧值打「大涨」标记就是误导（300821 旧 +20.01% vs 实时 +5.83%）——
   与 `hunterPriceHtml` 同一条铁律：给不出来就什么都不标，不猜。
   阈值 9%：主板涨停 10% / 创业板·科创板 20% ⇒ 9% 统一当「接近涨停」观察线，不按板块区分。 */
const HOT_PCT = 9;
function hotBadgeHtml(pct) {
  if (pct === null || pct === undefined || pct === "") return "";
  const n = Number(pct);
  if (isNaN(n) || n <= HOT_PCT) return "";
  return `<span class="badge hot" title="实时涨幅 +${n.toFixed(2)}% &gt; 9%（接近涨停）">🔥</span>`;
}

/* 猎手展开行「价 / 实时涨幅」格的**内容**（初始渲染与实时补值**共用同一个函数**
   ⇒ 两条路径的口径不可能不一致）。
   ⚠️ `q` 为空 = 行情还没到 ⇒ **绝不回落到扫描时的旧值**：猎手 payload 是"跑扫描那一刻"的快照，
      可能隔了几天。owner 实测 300821 显示 **+20.01%**（10-09 涨停那天的），而当天实时只有 **+5.83%** ——
      把旧值当现值渲染就是严重误导。所以给不出来就明说 ⏳/—。 */
function hunterPriceHtml(q) {
  if (!q || q.price == null) {
    return '<span class="h-px">—</span>'
      + '<div style="font-size:10px" class="h-pct"><span class="cell-dim">⏳</span></div>';
  }
  return `<span class="h-px">${hotBadgeHtml(q.change_pct)}${fmt(q.price)}</span>`
    + `<div style="font-size:10px" class="h-pct">${pctHtml(q.change_pct, "实时涨幅（随行情每 10s 刷新）")}</div>`;
}

/* 猎手页当前是否可见（10s 刷实时行情前判一下，别给看不见的面板出网）。 */
function _hunterTabActive() {
  const p = document.getElementById("tab-hunter");
  return !!(p && p.classList && p.classList.contains("active"));
}

/* 取**实时**价/涨幅（2026-10-12 改：一次覆盖全部展开板块）。
   ⚠️ 三条约束都在这里解决，别退回"每板块一次"：
     ① **一次请求**覆盖所有展开板块（`_allRenderedHunterCodes` 并集去重）⇒ 与展开数无关；
     ② **全局单飞**：展开与 10s 轮询会同时触发，不拦就会并发，而这个 GUI 每次 js_api 基线
        ~1s、前端在飞上限 6 ⇒ 一并发就互相排队，越点越慢；
     ③ **全局 TTL**：10s 轮询下 15s 内不重复取，避免自己和上一轮打架。 */
let hunterQuoteLast = 0;
let hunterQuoteRetry = 0;
async function loadHunterQuotes(force) {
  if (hunterQuoteInflight) return;                 // ② 全局单飞
  const now = Date.now();
  if (!force && now - hunterQuoteLast < HUNTER_QUOTE_TTL_MS) { _applyHunterQuotes(); return; }
  const codes = _allRenderedHunterCodes();
  if (!codes.length) return;
  const t0 = Date.now();
  hunterQuoteInflight = true;
  try {
    const r = await apiCall("load_quotes_for", codes);
    // 契约：`{quotes: {code: {price, change_pct}}, count}`（与 load_quotes 同款包法）。
    // ⚠️ 后端**必须**包 `quotes` 键 —— 曾经返回裸字典 ⇒ 这里永远读到 {} ⇒ 整列永远 ⏳。
    const qm = (r && r.quotes) || {};
    const got = Object.keys(qm).length;
    // ⚠️ 只认**非空**结果：冷启动/网络抖动会回空 map，若拿空当成功会把 TTL 刷新掉、
    //    之后 15s 内不再重试 ⇒ 整块面板干等。
    if (got) {
      Object.assign(hunterQuoteCache, qm);         // 平铺：code -> {price, change_pct}
      hunterQuoteLast = Date.now();
      hunterQuoteRetry = 0;
    } else if (hunterQuoteRetry < 3) {
      hunterQuoteRetry++;
      if (_hqDiag) _hqDiag("hunter_hq_js", Date.now() - t0,
                           `req=${codes.length} got=0 将重试${hunterQuoteRetry}`);
      setTimeout(() => { if (hunterOpenSectors.size) loadHunterQuotes(true); }, 3000);
      return;                                       // 本轮不 apply（拿不到就别刷成 —）
    }
    const patched = _applyHunterQuotes();
    if (_hqDiag) _hqDiag("hunter_hq_js", Date.now() - t0,
                         `req=${codes.length} got=${got} patched=${patched}`);
  } catch (e) {
    // 静默会让人无从判断（本仓踩过"告警从未发出"的坑）⇒ 只记诊断，不打断面板
    if (_hqDiag) _hqDiag("hunter_hq_js", Date.now() - t0,
                         `EXC ${String(e && e.message || e).slice(0, 50)}`);
  }
  finally { hunterQuoteInflight = false; }
}

/* 就地补价/涨幅到**所有**展开行。**用 data-hcode 定位**，不按 `td.textContent`
   （列序一改就填错位，本文件在标签列上吃过这个亏，见 applySectorTags 注释）。 */
function _applyHunterQuotes() {
  const el = document.getElementById("hunterBody");
  if (!el) return 0;
  let n = 0;
  el.querySelectorAll(".h-expand-row td.h-price[data-hcode]").forEach(td => {
    // 与初始渲染共用 `hunterPriceHtml`；取不到就回落成 ⏳/—（**不回落到扫描时的旧值**）
    td.innerHTML = hunterPriceHtml(hunterQuoteCache[td.getAttribute("data-hcode")]);
    n++;
  });
  return n;   // 返回命中格数 ⇒ 诊断能分辨"没拿到值"与"没命中 DOM"
}

function applySectorTags(tbody, category) {
  // 2026-10-18：方格布局后，阶段徽章在 `.hunter-tile-head`（tbody 外面），明细在
  // `.hunter-detail`；旧表格结构（.h-expand-wrap）保留兜底，别让别的调用点静默失效。
  const card = (tbody.closest && tbody.closest(".hunter-tile")) || null;
  const wrap = (card && card.querySelector(".hunter-detail"))
    || tbody.querySelector(".h-expand-wrap");
  const stageEl = (card && card.querySelector(".h-stage-badge"))
    || tbody.querySelector(".h-stage-badge");
  const tagsMap = hunterTagsCache[category] || {};
  if (!wrap) return;
  // 状态条：仅当**本次渲染出来的行全部拿到标签**才撤（增量取数下"有任意一条"不等于取完）
  const _rows = Array.from(wrap.querySelectorAll(".h-expand-row"));
  const _allTagged = _rows.length > 0 && _rows.every(r => {
    const td = r.querySelector("td");
    return td && tagsMap[td.textContent.trim()];
  });
  if (_allTagged) {
    wrap.querySelectorAll(".h-updating-strip").forEach(x => x.remove());
    wrap.querySelectorAll(".h-updating-foot").forEach(x => x.remove());
  }
  // 更新展开行标签列
  wrap.querySelectorAll(".h-expand-row").forEach(row => {
    const code = row.querySelector("td").textContent.trim();
    const info = tagsMap[code];
    if (info && info.tags) {
      // 2026-10-08: 按 class 定位标签列（此前按 tds[2] 固定下标，列序改成建仓表同款后填错位）
      const _tagsTd = row.querySelector("td.h-tags");
      if (_tagsTd) _tagsTd.innerHTML = info.tags.map(t => tagBadge(t)).join(" ");
    }
    // 2026-10-17：30m MA5 走的是**仓位结构**缓存（比标签慢一拍）⇒ 这里同款回填
    const _st = (hunterStructCache[category] || {})[code];
    if (_st) {
      const _ma5Td = row.querySelector("td.h-ma5");
      if (_ma5Td) _ma5Td.outerHTML = ma5CellHtml(_st, "h-ma5");
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
}

async function toggleSectorExpand(head, category) {
  // 2026-10-18：方格布局 —— head 是 `.hunter-tile-head`（不再是 `<tr>`），
  // 明细是同一张卡里的 `.hunter-detail`；展开时卡片跨整行（CSS `.hunter-tile.open`）。
  const card = head.closest ? head.closest(".hunter-tile") : null;
  const wrap = card ? card.querySelector(".hunter-detail") : head.nextElementSibling;
  if (!wrap) return;
  const opening = !wrap.classList.contains("open");
  wrap.classList.toggle("open", opening);
  if (card) card.classList.toggle("open", opening);
  const arw = card && card.querySelector(".bk-arrow");
  if (arw) arw.textContent = opening ? "▾" : "▸";
  if (opening) hunterOpenSectors.add(category); else hunterOpenSectors.delete(category);
  if (!opening) return;
  const tbody = wrap.querySelector("tbody.h-sector-group");
  // 实时价/涨幅：**先发**，与标签**并行**跑（2026-10-12）。⚠️ 上一版误把它放在 `await` 之后 ⇒
  // 白等标签 4~7 秒才发请求，等于没优化。必须在这行 await **之前**。
  loadHunterQuotes();
  await loadSectorTags(category);
  if (tbody) applySectorTags(tbody, category);
}
let hunterHistoryDates = [];

/* 历史日期控件（2026-10-17 改造）：原来是个原生 `<select>`，在 WebView2 里**鼠标点不开**
   （owner 反馈；后端 `available_hunter_dates` 实测返回 95 个日期，不是没数据）。
   改用与「建仓信号扫描」同款、已验证可用的 `<input type="date">`。
   本函数只负责给个合理默认值（最新可用日期），不再填充下拉项。 */
async function initHunterDates() {
  const el = document.getElementById("hunterDate");
  if (!el || el.value) return;                 // 已有值（用户手选过）就别覆盖
  try {
    const dates = await apiCall("available_hunter_dates");
    hunterHistoryDates = dates || [];
    el.value = (hunterHistoryDates[0] && hunterHistoryDates[0].date) || state.date || todayStr();
  } catch (e) {
    // 桥接失败也要可用：退回当前选中日，别留一个空控件
    el.value = state.date || todayStr();
  }
}

// ---- 主要指数实时状态板（2026-09-28；点击看K线，与个股一致）----
// 成交额格式化：≥1万亿用「万亿」，否则用「亿」
function fmtAmount(v) {
  if (v == null || isNaN(v)) return "—";
  if (v >= 1e12) return (v / 1e12).toFixed(2) + "万亿";
  return Math.round(v / 1e8).toLocaleString("en-US") + "亿";
}
// 两市成交额卡片（2026-09-30）：市场级汇总，非指数 ⇒ **不挂 openStockChart**。
// 后端已按「盘中比昨日同期 / 收盘比全日」算好，此处只渲染并如实标出 basis。
function turnoverCard(t) {
  if (!t || !t.available) {
    const why = (t && t.error) || "不可用";
    return `<div class="idx-card idx-card-err" style="min-width:150px;text-align:center"
        title="${esc("两市成交额：" + why)}">
      <div style="font-size:13px;color:var(--text-dim)">两市成交额</div>
      <div class="mono" style="font-size:13px;color:var(--text-dim)">—</div>
      <div style="font-size:10px;color:var(--text-dim)">${esc(why)}</div>
    </div>`;
  }
  const pct = t.pct;
  const cls = pct > 0.05 ? "up" : pct < -0.05 ? "down" : "cell-dim";
  const pctTxt = (pct == null) ? "—" : `${pct >= 0 ? "+" : ""}${fmt(pct, 2)}%`;
  const basis = t.basis === "同期" ? "vs昨日同期" : "vs昨日全日";
  // 分腿拆解（后端 by_leg）：无北交所时如实标出，避免把"少算 120 亿"当正常
  const LEG = { sh000001: "沪", sz399106: "深", bj899050: "北" };
  const byLeg = t.by_leg || {};
  const legTxt = Object.keys(LEG).filter(k => byLeg[k] != null)
    .map(k => `${LEG[k]} ${Math.round(byLeg[k] / 1e8).toLocaleString("en-US")}`).join(" + ");
  const tip = `今日 ${t.day} · 昨日 ${t.prev_day}\n`
    + (legTxt ? `分腿(亿)：${legTxt}\n` : "")
    + `今日${t.basis} ${Math.round(t.amount / 1e8).toLocaleString("en-US")}亿\n`
    + `昨日${t.basis} ${Math.round(t.amount_prev / 1e8).toLocaleString("en-US")}亿\n`
    + (t.basis === "同期"
        ? `（盘中口径：两边都只算到 ${t.as_of}）`
        : "（已收盘口径：两边都是全日）")
    + (t.bse_included ? "\n口径：沪(上证指数)+深(深证综指)+北(北证50，全北交所)" : "");
  // 北交所不可得 ⇒ 降级为沪深口径，明示（否则数值会比同花顺低 ~120 亿）
  const warn = t.bse_included === false
    ? `<div style="font-size:10px;color:var(--warn,#d29922)">沪深口径·北交所不可得</div>` : "";
  // 冷启动快照：加了"（快照）"标记 + tooltip 给出快照时刻（安全契约：不得静默当实时）
  const stale = t.stale
    ? `<div style="font-size:10px;color:var(--warn,#d29922)"
         title="${esc("冷启动快照，非实时；后台刷新中")}">快照 ${esc(t.snapshot_ts || "—")}</div>` : "";
  return `<div class="idx-card${t.stale ? " idx-card-stale" : ""}" style="min-width:150px;text-align:center" title="${esc(tip)}">
    <div style="font-size:13px;color:var(--text-dim)">两市成交额${t.bse_included === false ? "（缺北）" : ""}${t.stale ? "（快照）" : ""}</div>
    <div class="mono" style="font-size:16px;font-weight:700">${fmtAmount(t.amount)}</div>
    <div class="mono ${cls}" style="font-size:11px">${pctTxt}
      <span style="color:var(--text-faint);font-size:10px">${basis}</span></div>
    ${warn}
    ${stale}
  </div>`;
}
// 指数列表来自后端 core.board_index.GUI_INDEX_BOARD（单一真源），实时源掘金（不可用降级腾讯）。
async function loadIndices() {
  const body = document.getElementById("indicesBody");
  const meta = document.getElementById("indexBoardMeta");
  if (!body) return;
  try {
    // 成交额与指数并行取（成交额后端 60s 去重，不随 10s 轮询打 GM）
    const [d, turn] = await Promise.all([
      apiCall("load_indices"),
      apiCall("load_market_turnover").catch(() => null),
    ]);
    const idx = (d && d.indices) || [];
    if (!idx.length) {
      body.innerHTML = '<div class="empty">指数行情不可用</div>';
      return;
    }
    const reg = d.regime
      ? `大盘 ${esc(d.regime)}${d.days_in_regime ? " 第" + d.days_in_regime + "天" : ""}` : "";
    // 冷启动快照（2026-10-10）：后端从磁盘 last-good 快照秒返时必带 stale=true。
    // **必须显式标注**，绝不能把过期价静默渲染成实时值（交易台安全契约）。约 10s 内被下一轮轮询替换。
    const staleTxt = d.stale
      ? `⚠ 快照 ${esc(d.snapshot_ts || "—")}（后台刷新中）`
      : (d.ts ? "更新于 " + d.ts : "");
    if (meta) {
      meta.textContent = [staleTxt, reg].filter(Boolean).join(" · ");
      meta.classList.toggle("meta-stale", !!d.stale);
    }

    // 有效背离角标：同一指数多周期时优先显示「有边际」的那条
    const divBySym = {};
    (state.indexDivergences || []).forEach(a => {
      const cur = divBySym[a.symbol];
      if (!cur || (a.significant && !cur.significant)) divBySym[a.symbol] = a;
    });

    body.innerHTML = `
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        ${idx.map(i => {
          if (i.error) {
            // 取不到也占位（后端有意不下发 price）→ 置灰 + 显式原因，避免"少了一个"看不懂
            return `<div class="idx-card idx-card-err" style="min-width:132px;text-align:center"
                title="${esc(i.name + "：" + i.error)}">
              <div style="font-size:13px;color:var(--text-dim)">${esc(i.name)}</div>
              <div class="mono" style="font-size:13px;color:var(--text-dim)">—</div>
              <div style="font-size:10px;color:var(--text-dim)">${esc(i.error)}</div>
            </div>`;
          }
          const pct = i.change_pct;
          const cls = pct > 0.05 ? "up" : pct < -0.05 ? "down" : "cell-dim";
          const pctTxt = (pct == null) ? "—" : `${pct >= 0 ? '+' : ''}${fmt(pct, 2)}%`;
          const div = divBySym[i.symbol];
          const badge = div
            ? `<div class="div-badge ${div.type === "顶" ? "div-badge-top" : "div-badge-bottom"}"
                 title="${esc(div.freq + div.type + "背离｜" + div.evidence + "｜" + div.disclaimer)}">${div.type}背离 ${esc(div.freq)}</div>`
            : "";
          const srcTxt = i.source === "tencent" ? "腾讯" : (i.source === "em" ? "东财" : "");
          return `<div class="idx-card" style="cursor:pointer;min-width:132px;text-align:center;position:relative"
              onclick="openStockChart('${i.symbol}','${esc(i.name)}')" title="点击查看K线">
            <div style="font-size:13px;color:var(--text-dim)">${esc(i.name)}${srcTxt ? `<span class="idx-src">${srcTxt}</span>` : ""}</div>
            <div class="mono" style="font-size:16px;font-weight:700">${fmt(i.price, 2)}</div>
            <div class="mono ${cls}" style="font-size:11px">${pctTxt}</div>
            ${badge}
          </div>`;
        }).join("")}
        ${turnoverCard(turn)}
      </div>`;
  } catch (e) {
    body.innerHTML = `<div class="empty">指数加载失败: ${esc(e.message)}</div>`;
  }
}

// ---- 指数背离常驻提醒（2026-09-28）----
// ⚠️ 观察提示，非交易信号：每条固定带证据分级 + 免责说明（口径见 analysis/index_divergence.py）。
// 常驻不自动消失（区别于 pushAlert 的 10s 淡出）；只有**新事件**才闪烁+响铃。
let _divSeenKeys = null;   // 首次装载不闪（避免整页刷新把所有事件都闪一遍）
async function refreshIndexDivergence() {
  const banner = document.getElementById("divergenceBanner");
  if (!banner) return;
  let res = null;
  try {
    res = await apiCall("load_index_divergence");
  } catch (e) {
    return;
  }
  const alerts = (res && res.alerts) || [];
  const watching = (res && res.watching) || [];
  state.indexDivergences = alerts;      // 角标只用提醒集（= 新事件）
  if (!alerts.length && !watching.length) {
    banner.style.display = "none";
    banner.innerHTML = "";
    return;
  }
  const hasNew = _divSeenKeys !== null && alerts.some(a => !_divSeenKeys.has(a.key));
  const item = (a, muted) => `<span class="div-item ${a.type === "顶" ? "div-top" : "div-bottom"}${muted ? " div-muted" : ""}"
      title="${esc(a.time + "｜已过 " + a.bars_ago + " 根 bar｜证据 " + a.tier + " " + a.evidence + "｜" + a.disclaimer)}">
      ${muted ? "观察中 " : ""}${esc(a.index)}·${esc(a.freq)}·${esc(a.type)} <em>${esc(a.tier)}</em>
    </span>`;
  banner.style.display = "block";
  banner.innerHTML = `
    <div class="div-strip ${hasNew ? "div-flash" : ""}">
      <span class="div-strip-title">📉 指数背离 <span class="cell-dim">观察项 · 非交易信号</span></span>
      ${alerts.map(a => item(a, false)).join("")}
      ${watching.map(a => item(a, true)).join("")}
      <span class="cell-dim" style="font-size:10px">背离需 3 根 bar 确认（30min 线即峰值后 1.5 小时）；「观察中」为宽窗口内的旧事件，不推送</span>
    </div>`;
  _divSeenKeys = new Set(alerts.map(a => a.key));
  if (hasNew) { try { playAlert("SELL_HIGH"); } catch (e) {} }
}

// 指数板刷新入口：先取背离（供角标），再渲染板
async function refreshIndexBoard() {
  try { await refreshIndexDivergence(); } catch (e) {}
  try { await loadIndices(); } catch (e) {}
}

// 重新生成选中日期结果（拉行情+评分）
async function regenerateHunter() {
  const sel = document.getElementById("hunterDate");
  const date = sel.value;
  if (!date) { statusEl("请先选择日期", "err"); return; }
  const btn = document.getElementById("hunterRegenBtn");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 重新生成中..."; }
  // 走后台线程 + 进度条（复用 loadHunterHistory），避免同步阻塞桥线程；
  // 拉取已加硬限时兜底（后端 420s 预算），卡死也能带结果/报错返回
  await loadHunterHistory(date);
  // 2026-10-10: 突破/站上5日线扫描不再随「重新生成」自动触发（owner 需求6），由其卡片按钮各自发起。
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
    let h = await apiCall("load_hunter", date);
    // 后端已改为「起后台跑 + 返回 running」；若仍 running 就接着等（不显示"无数据"）
    let hg = 0;
    while (h && h.running && hg < 600) {
      await sleep(1000);
      const p2 = await apiCall("hunter_progress");
      renderHunterProgress(el, p2);
      if (!p2.running) h = await apiCall("load_hunter", date);
      hg++;
    }
    if (h.available) {
      renderHunter(h, true);
      hunterLoaded = true;
      statusEl(`已加载 ${date} 选股猎手数据`, "ok");
      // 2026-10-13：猎手跑完会重写 daily_summary.json ⇒ 立即强刷风向标，
      // 让其板块排名与猎手对齐（后端骨架缓存键已含该文件 mtime，会即刻重算）。
      // 2026-10-18：**把 date 一起传过去** —— 历史日「重新生成」时风向标必须跟到同一天，
      // 否则它按全局日期挑"≤ 今天的最新一天"，两边名次对不上。
      loadMarketBeacon(true, date);
      refreshSectorStarsIfVisible();   // 同上：重新生成也重写了 daily_summary.json
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
// 扫描结论 → 人话（勿显示英文枚举给用户）
const PB_VERDICT_TXT = {
  signal: "报买(signal)",
  approaching: "接近买点",
  watch_signal: "观察信号",
  weak: "监控中（未达买点）",
  insufficient_data: "分钟数据不足（未开盘或数据未就绪）",
};
function localToday() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
async function addNewWatchlist() {
  const codeEl = document.getElementById("pbSearchCode");
  const nameEl = document.getElementById("pbSearchName");
  const btn = document.getElementById("pbAddBtn");
  const code = codeEl.value.trim();
  const name = nameEl.value.trim();
  if (!code) { statusEl("请输入股票代码", "err"); codeEl.focus(); return; }
  if (!/^\d{6}$/.test(code)) {
    statusEl("代码格式应为6位数字", "err");
    showResultModal("添加失败",
      `<div>代码 <b class="mono">${esc(code)}</b> 格式不对：应为 6 位数字。</div>`, true);
    codeEl.focus();
    return;
  }
  // 立即反馈：按钮置灰 + 文案变化，同时防重复点击
  const prevTxt = btn.textContent;
  btn.disabled = true;
  btn.textContent = "添加中...";
  try {
    // 名称空则自动取（带进度提示 + 2.5s 超时兜底，避免网络卡住时界面毫无反应）
    let finalName = name;
    if (!finalName) {
      statusEl(`正在获取 ${code} 名称...`, "ok");
      try {
        const r = await Promise.race([
          apiCall("search_stock", code),
          new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 2500))
        ]);
        const hit = (r.results || []).find(x => x.code === code);
        if (hit) finalName = hit.name;
      } catch (e) { /* 名称获取失败/超时则用代码兜底 */ }
    }

    // 一次调用完成「写股池 + 立即单股扫描」，并回传成败明细
    let r;
    try {
      r = await apiCall("add_and_scan", code, finalName || code);
    } catch (e) {
      r = { ok: false, error: e.message };
    }

    if (!r || !r.ok) {
      const why = (r && r.error) || "未知原因";
      statusEl(`加入失败: ${why}`, "err");
      showResultModal("添加失败",
        `<div>股票 <b class="mono">${esc(code)}</b> ${esc(finalName || "")} 未能加入建仓股池。</div>
         <div class="down" style="margin-top:8px">原因：${esc(why)}</div>`, true);
      return;   // 失败不清空输入框，便于改正后重试
    }

    // 成功 → 清空输入框并聚焦回代码框（此前 addToWatchlist 无返回值，这段判断从不成立）
    codeEl.value = "";
    nameEl.value = "";
    codeEl.focus();
    const nm = (r.name && r.name !== code) ? ` ${r.name}` : "";
    statusEl(`${code}${nm} 已加入建仓股池并扫描`, "ok");
    if (state.date) apiCall("refresh_pb", state.date).then(pb => renderPB(pb || {})).catch(() => {});

    const sc = r.scan || {};
    if (r.scan_error) {
      // 加进股池了但没扫成功 → 该股不会出现在下表，必须讲清楚
      showResultModal("已加入股池，但扫描失败",
        `<div><b class="mono">${esc(code)}</b>${esc(nm)} 已写入建仓股池。</div>
         <div class="warn" style="margin-top:8px">但立即扫描失败：${esc(r.scan_error)}</div>
         <div class="cell-dim" style="margin-top:8px">该股需点「🔄 盘后重跑」后才会出现在下表。</div>`, true);
    } else {
      let msg = `✓ ${code}${nm} 已加入并扫描 · ${PB_VERDICT_TXT[sc.verdict] || sc.verdict || "—"}`
        + `（${sc.composite_score != null ? sc.composite_score : "—"}分）`;
      const why = sc.block_reason || sc.reason;
      if (why) msg += `\n${sc.block_reason ? "卡点" : "说明"}：${why}`;
      if (r.scan_date && state.date && state.date !== localToday()) {
        msg += `\n已按今日(${r.scan_date})扫描；当前在看 ${state.date}，切回今日可见该行`;
      }
      toast(msg, "ok");
    }
  } finally {
    btn.disabled = false;
    btn.textContent = prevTxt;
  }
}
// 列表行内「+股池」按钮：只加不扫（避免在列表里误触发大量网络请求），返回是否成功
async function addToWatchlist(code, name, btn) {
  try {
    const r = await apiCall("add_to_watchlist", code, name);
    if (r && r.ok) {
      if (btn) { btn.textContent = "✓已加"; btn.style.color = "#3fb950"; btn.style.borderColor = "#3fb950"; }
      statusEl(`${code} ${name} 已加入建仓股池`, "ok");
      toast(`✓ ${code} ${name} 已加入建仓股池\n需扫描后才会出现在下表（或在该股行点「🔄」）`, "ok");
      // 自动刷新建仓扫描表
      if (state.date) apiCall("refresh_pb", state.date).then(pb => renderPB(pb || {})).catch(() => {});
      return true;
    }
    const why = (r && r.error) || "未知原因";
    statusEl(`加入失败: ${why}`, "err");
    showResultModal("加入股池失败",
      `<div>股票 <b class="mono">${esc(code)}</b> ${esc(name)} 未能加入建仓股池。</div>
       <div class="down" style="margin-top:8px">原因：${esc(why)}</div>`, true);
    return false;
  } catch (e) {
    statusEl(`加入失败: ${e.message}`, "err");
    showResultModal("加入股池失败",
      `<div>股票 <b class="mono">${esc(code)}</b> 未能加入建仓股池。</div>
       <div class="down" style="margin-top:8px">原因：${esc(e.message)}</div>`, true);
    return false;
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
    let h = await apiCall("load_hunter", date);
    let hg = 0;
    while (h && h.running && hg < 600) {   // 后端「起后台跑+返回 running」→ 这里接着等
      await sleep(1000);
      const p2 = await apiCall("hunter_progress");
      renderHunterProgress(el, p2);
      if (!p2.running) h = await apiCall("load_hunter", date);
      hg++;
    }
    if (h && h.available === false && h.running) {
      el.innerHTML = '<div class="empty">扫描超时，请稍后重试</div>';
    } else {
      renderHunter(h);
      hunterLoaded = true;
      // 猎手跑完会重写 daily_summary.json ⇒ 风向标骨架缓存按 mtime 已失效。
      // 这里主动拉一次并**钉到同一天**，别让用户等最多 60s 的前端 TTL（否则看起来"排名没对齐"）。
      try { loadMarketBeacon(true, date); } catch (e) { /* 静默 */ }
      // 2026-10-20：星轨图的数据源**也是** daily_summary.json，不重拉就停在跑之前的旧轨迹
      // （2026-10-20 owner 报「跑完猎手星轨图没更新」——它原来只在切 tab / 改日期时刷新）。
      // 隐藏时画会拿到 0 尺寸 ⇒ 只在猎手页画；其它页切回来时 switchTab 会再拉一次。
      refreshSectorStarsIfVisible();
    }
    // 2026-10-10: 突破箱体 / 站上5日线 扫描不再自动触发，一律由各自卡片按钮发起（owner 需求6）
  } catch (e) {
    el.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
  }
  hunterRunning = false;
  if (btn) { btn.disabled = false; btn.textContent = "🔄 刷新数据"; }
}

// ---- 突破箱体股票（单独拎出）----
let breakoutLoaded = false;
let breakoutLoadedDate = null;
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
async function loadBreakoutStocks(force, date) {
  const body = document.getElementById("hunterBreakoutBody");
  if (!body) return;
  const _di = document.getElementById("hunterBreakoutDate");
  const _date = date || (_di && _di.value) || todayStr();
  if (!force && breakoutLoaded && breakoutLoadedDate === _date) return;
  const btn = document.getElementById("hunterBreakoutBtn");
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 扫描中..."; }
  body.innerHTML = '<div class="empty">⏳ 准备扫描...</div>';
  try {
    const st = await apiCall("start_breakout_scan", !!force, _date);
    if (st.status === "done") {
      renderBreakout(st);
      if (!st.fetch_failed) { breakoutLoaded = true; breakoutLoadedDate = _date; }  // 取数失败→不记已加载，允许自动重试
      if (btn) { btn.disabled = false; btn.textContent = "🔄 扫描该日"; }
      return;
    }
    if (st.status === "error") {
      body.innerHTML = `<div class="empty">扫描失败: ${esc(st.error || '未知')}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = "🔄 扫描该日"; }
      return;
    }
    await pollBreakoutScan(body, btn, _date);   // running → 轮询进度
  } catch (e) {
    const cur = document.getElementById("hunterBreakoutBody");
    if (cur) cur.innerHTML = `<div class="empty">突破箱体扫描失败: ${esc(e.message)}</div>`;
    if (btn) { btn.disabled = false; btn.textContent = "🔄 扫描该日"; }
  }
}
async function pollBreakoutScan(body, btn, date) {
  const t0 = Date.now();
  // 全池 5000+ 只由后端 GM 批量拉取（900 只/批）。原先 5 分钟兜底会在扫完前放弃，
  // 而按钮是 force 重扫 ⇒ 用户一点就把已扫完的结果作废、永远看不到结果。
  // 实测全池约 1~2 分钟，故给足 20 分钟上限（仅防后端僵死），超时也**不谎报失败**。
  const MAX = 1200000;
  while (Date.now() - t0 < MAX) {
    await sleep(800);
    if (!document.body.contains(body)) return;   // 标签已切换
    let st;
    try { st = await apiCall("get_breakout_scan", date); } catch (e) { break; }
    if (st.status === "done") {
      renderBreakout(st);
      if (!st.fetch_failed) breakoutLoaded = true;   // 取数失败→不记已加载，允许自动重试
      if (btn) { btn.disabled = false; btn.textContent = "🔄 扫描该日"; }
      return;
    }
    if (st.status === "error") {
      body.innerHTML = `<div class="empty">扫描失败: ${esc(st.error || '未知')}</div>`;
      if (btn) { btn.disabled = false; btn.textContent = "🔄 扫描该日"; }
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
  // 走到这里只可能是后端僵死（正常全池 1~2 分钟）。**不谎报"超时失败"**——
  // 说清仍在后台跑、并提示点按钮会重新扫描（不是"重试失败"）。
  const mins = Math.round((Date.now() - t0) / 60000);
  body.innerHTML = `<div class="empty">⏳ 扫描仍在后台运行（已 ${mins} 分钟）<br>
    <span class="cell-dim" style="font-size:11px">后端线程未结束；点「🔄 扫描该日」会作废本轮并重开</span></div>`;
  if (btn) { btn.disabled = false; btn.textContent = "🔄 扫描该日"; }
}
function renderBreakout(b) {
  const body = document.getElementById("hunterBreakoutBody");
  if (!body) return;
  const stocks = (b && b.stocks) || [];
  const restNd = (b && b.rest_no_data) || 0;   // 非北交所无日线（取数失败/未就绪）
  const bjNd = (b && b.bj_no_data) || 0;       // 北交所无日线（环境限制）
  const stale = (b && b.stale) || 0;           // 有历史但无当日 bar
  const meta = document.getElementById("hunterBreakoutMeta");
  const _parts = [];
  if (b && b.date) _parts.push('· ' + esc(b.date));
  _parts.push(`· 共 ${stocks.length} 只`);
  if (restNd) _parts.push(`· <span class="warn">⚠ ${restNd} 只取数失败</span>`);
  if (bjNd) _parts.push(`· ${bjNd} 只北交所无日线`);
  if (meta) meta.innerHTML = _parts.join(" ");
  // 整池不可用（取数失败/非交易日/行情未就绪）⇒ **绝不显示"暂无突破"**（那是假 0）
  if (b && b.fetch_failed) {
    const why = restNd > 0
      ? `非北交所 ${restNd} 只无日线 —— 掘金终端可能未连接/行情未就绪`
      : `全池 ${stale} 只无当日行情 —— 可能非交易日或当日行情未就绪`;
    body.innerHTML = `<div class="empty" style="color:var(--warn,#d29922)">
      ⚠ 本次扫描**取数失败，结果不可信**：${esc(why)}<br>
      <span class="cell-dim" style="font-size:11px">已跳过判定且未缓存；点「🔄 扫描该日」重试（节假日/盘前无当日行情属正常）。</span></div>`;
    return;
  }
  if (!stocks.length) {
    body.innerHTML = '<div class="empty">池内暂无「当日有效突破」的股票'
      + (bjNd ? `<div class="cell-dim" style="font-size:10px;margin-top:4px">${bjNd} 只北交所股在当前环境取不到日线，已跳过</div>` : "")
      + (restNd ? `<div class="cell-dim" style="font-size:10px;margin-top:2px;color:var(--warn,#d29922)">另有 ${restNd} 只非北交所取数失败，可能漏报</div>` : "")
      + "</div>";
    return;
  }
  // 按行业分组（2026-09-29 owner：平铺不直观）。组按只数降序，组内按超出幅度降序。
  // 每组一个 <tbody> ⇒ 折叠只需切换该 tbody 的 class（表格允许多个 tbody）。
  const groups = {};
  stocks.forEach(s => {
    const k = s.industry || "未分类";
    (groups[k] = groups[k] || []).push(s);
  });
  const gkeys = Object.keys(groups).sort(
    (a, b) => groups[b].length - groups[a].length || a.localeCompare(b, "zh"));

  const bodyHtml = gkeys.map(k => {
    const arr = groups[k].slice().sort(
      (x, y) => (y.pct_above || 0) - (x.pct_above || 0));
    return `<tbody class="bk-group">
    <tr class="bk-group-head" onclick="toggleBkGroup(this)" title="点击收起/展开">
      <td colspan="8"><span class="bk-arrow">▼</span> <b>${esc(k)}</b>
        <span class="cell-dim">· ${arr.length} 只</span></td>
    </tr>
    ${arr.map(s => `<tr class="bk-member h-expand-row" ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')">
      <td class="mono cell-dim" title="双击看技术分析">${esc(s.code)}</td>
      <td title="双击看技术分析">${esc(s.name)} <button class="mini-btn" style="font-size:10px;padding:0 5px"
        onclick="event.stopPropagation();addToWatchlist('${esc(s.code)}','${esc(s.name)}',this)" title="加入建仓股池监控买点">+股池</button></td>
      <td class="num">${s.box_top != null ? fmt(s.box_top, 2) : '—'}</td>
      <td class="num cell-dim">${s.prev_close != null ? fmt(s.prev_close, 2) : '—'}</td>
      <td class="num">${s.price != null ? fmt(s.price, 2) : '—'}</td>
      <td class="num up">+${s.pct_above != null ? fmt(s.pct_above, 2) : '—'}%</td>
      <td>${(s.tags || []).map(t => tagBadge(t)).join(" ")}</td>
      <td class="bk-concepts-cell">${conceptsCell(s)}</td>
    </tr>`).join("")}
  </tbody>`;
  }).join("");

  body.innerHTML = `<table class="h-table"><thead><tr>
    <th>代码</th><th>名称</th><th class="num">箱体上沿</th><th class="num">昨收</th>
    <th class="num">现价</th><th class="num">超出</th><th>标签</th><th>概念</th>
  </tr></thead>${bodyHtml}</table>
  <div class="cell-dim" style="font-size:10px;margin-top:3px">共 ${gkeys.length} 个行业 · 组按只数降序 · 组内按超出幅度降序 · 双击看技术分析</div>
  <div class="cell-dim" style="font-size:10px;margin-top:2px">概念为离线口径（韭研概念 + 东财板块概念段），仅覆盖约一半股票；无概念的显示「—」，双击看技术分析可点「📋 公司资料」拉东财全量板块</div>
  ${bjNd ? `<div class="cell-dim" style="font-size:10px;margin-top:2px">另有 ${bjNd} 只北交所股无日线、未参与判定（当前环境取不到）</div>` : ""}
  ${restNd ? `<div class="cell-dim" style="font-size:10px;margin-top:2px;color:var(--warn,#d29922)">⚠ 另有 ${restNd} 只非北交所取数失败、未参与判定，结果可能漏报</div>` : ""}`;
}
function toggleBkGroup(headEl) {
  const tb = headEl.closest("tbody");
  if (!tb) return;
  const collapsed = tb.classList.toggle("bk-collapsed");
  const arrow = headEl.querySelector(".bk-arrow");
  if (arrow) arrow.textContent = collapsed ? "▸" : "▼";
}
// 概念列（2026-09-30）。离线口径（韭研概念 + 东财板块概念段）只盖约一半股票 ⇒
// 没数据时显示「—」而不是空白，避免看起来像渲染失败；全量概念在 title 里，
// 想要东财全量可用双击弹窗的「📋 公司资料」。
function conceptsCell(s) {
  const cs = (s && s.concepts) || [];
  if (!cs.length) {
    return '<span class="cell-dim" title="离线无该股概念数据（该口径约覆盖一半股票）">—</span>';
  }
  // 2026-10-19：逐概念 chip——有注释的概念（后端 concept_descs，医药板块二级概念起）
  // 悬停显示「概念：解释」并加点状下划线；无注释的保持原样，整格 title 仍是全量串兜底。
  const descs = (s && s.concept_descs) || {};
  const full = cs.join(" · ");
  const inner = cs.map(c => {
    const tip = descs[c];
    return tip
      ? `<span title="${esc(c + "：" + tip)}" style="cursor:help;border-bottom:1px dotted">${esc(c)}</span>`
      : `<span>${esc(c)}</span>`;
  }).join('<span class="cell-dim"> · </span>');
  return `<span class="bk-concepts" title="${esc(full)}">${inner}</span>`;
}
/* 韭研 D1~D11 打分规则（2026-10-17 显示到面板上）。
   真源 = `stock_hunter/modules/reporter.py` 的「0-说明」表 + `stock_hunter/config.json`
   的 `scoring.dimensions`（D3/D10 未启用，故不出现在这里）。改口径时**两处一起改**。 */
const DIM_RULES = [
  ["D1", "强势形态且新高", "8", "最高价创近150日新高"],
  ["D2", "强势形态", "6", "近5日涨幅>20% 且 最高价是近20日最高"],
  ["D4", "首板资金池", "3", "当日首板涨停（前日非涨停）"],
  ["D5", "潜在突破10日", "2", "最高价>近10日最高"],
  ["D6", "潜在突破5日", "1", "最高价>近5日最高 且 当日非涨停（满足D5则不计）"],
  ["D7", "持续性", "5", "二板及以上（连板天数≥2）"],
  ["D8", "情绪分数", "6", "当日一字板涨停"],
  ["D9", "活跃程度", "3", "近10日有涨停板"],
  ["D11", "刚站上5日线", "2", "昨收<昨MA5 且 今收>今MA5（状态跃迁，不是「是否站上」）"],
  ["D12", "趋势性", "8", "当日收盘站上 20 日均线（中期趋势向上；池内约 19% 命中，owner 2026-10-17 新增，权重最高档）"],
  ["大额", "大成交额", "3", "当日成交额≥50亿"],
];
const DIM_NAME = {};
DIM_RULES.forEach(r => { DIM_NAME[r[0]] = r[1]; });

function renderHunter(h) {
  // 「建仓符合度是否已算」：`build_n` 由后端在**算完之后**回填（算了多少只）。
  // =0 说明这一步还没跑到/没跑完 ⇒ 各行显示 ⏳（更新中）而不是 —（无数据）。
  const _buildReady = (h.build_n || 0) > 0;
  const el = document.getElementById("hunterBody");
  if (!h || !h.available) {
    el.innerHTML = `<div class="empty">选股猎手数据不可用${h && h.error ? ': ' + esc(h.error) : ''}</div>`;
    return;
  }

  const sumRows = h.summary_rows || [];
    const ss = h.sector_stocks || {};
    // 时机条件列的标签真源（后端 `t_gui.COND_LABELS` 经 payload 下发）——猎手侧**不再自己列条件键**，
    // 见 `condCellHtml` 注释（硬编码 3 键曾被 10-15 的真源扩到 6 条时漏改）。
    const condLabels = h.cond_labels || {};
    // 存储板块成分股供展开标注/翻页
    window._hunterSectors = ss || {};
    window._hunterData = h;
    const trends = h.concept_trends || {};
    // 2026-10-19：二级概念注释 {概念: 解释}（后端全表合并下发）——细分分组头悬停显示。
    // 分组键经过 normGk 归并（半导体设备-测试设备→半导体设备），查注释时先精确、
    // 再按「-」首段回退匹配，保证带子级后缀的概念也能命中。
    const conceptDescs = h.concept_descs || {};
    const gkDesc = gk => {
      if (conceptDescs[gk]) return conceptDescs[gk];
      const hit = Object.keys(conceptDescs).find(k => k.split("-")[0] === gk);
      return hit ? conceptDescs[hit] : "";
    };
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
    // 细分按第一层级归并计数（半导体设备-测试设备 → 半导体设备），与下方展开分组一致
    const _fl = new Set();
    for (const s of stocks) {
      for (const c of ((s.concepts && s.concepts.length) ? s.concepts : ["未分类"])) _fl.add(c.split("-")[0]);
    }
    const firstLevelCount = _fl.size;

    // 展开的个股明细（全部成分股，分页50），按细分（韭研概念）分组显示下一级分类
    const PAGE = 50;
    const pageIdx = window._hunterPage && window._hunterPage[category] || 0;
    const allStocks = stocks;
    const hunterRow = s => {
      // 本行是否属于"还没算到"（而不是"算了但该股无数据"）
      if (!_buildReady && s.build_verdict === undefined) s._buildMissing = true;
      // 2026-10-08 owner：猎手个股行改用**建仓表同款列**（判定/得分/通过X/3/价/技术标签/时机条件/理由）
      // `s._buildMissing`：本行**建仓符合度还没算到**（≠ 算了但无数据）⇒ 显示 ⏳ 而不是 —
      const _pend = !!s._buildMissing;
      const _v = s.build_verdict;
      const verdictTxt = _v ? verdictBadge(_v) : (_pend ? UPDATING_HTML : '<span class="cell-dim">—</span>');
      const _sc = (s.build_score != null) ? s.build_score : null;
      const _ceil = (s.build_ceiling != null) ? s.build_ceiling : 100;
      const _reach = (s.build_reachable != null) ? s.build_reachable : true;
      const scoreTxt = _sc == null ? (_pend ? UPDATING_HTML : '<span class="cell-dim">—</span>')
        : (_reach ? `<b>${_sc}</b>` : `<b>${_sc}</b><span class="cell-dim" style="font-size:10px">/${_ceil}🔒</span>`);
      const _metTxt = (s.build_met != null) ? `${s.build_met}/3` : (_pend ? UPDATING_HTML : "—");
      const tagsTxt = s.tags && s.tags.length
        ? s.tags.map(t => tagBadge(t)).join(" ")
        : `<span class="cell-dim" style="font-size:11px" title="技术标签按板块后台批量计算（约 30~60 秒），完成后自动填上">⏳ 标签</span>`;
      // 2026-10-20 owner：选股猎手个股行也按 5 日线着色——**站上=绿、其它=黄**。
      // 未知（建仓符合度还没算到）才回退梯队/得分色，避免把「没数据」画成「未站上」。
      const _ab5 = s.build_above_ma5;
      const _ma5Bg = (_ab5 === true) ? "background:rgba(63,185,80,.10);"
        : (_ab5 === false) ? "background:rgba(210,153,34,.14);" : "";
      const _rowBg = _ma5Bg || tierRowStyle(s.tier)
        || ((_sc === 0) ? "background:rgba(248,81,73,.12);"
          : (_sc !== null && _sc > 0) ? "background:rgba(63,185,80,.07);" : "");
      const _px = (s.build_price != null) ? fmt(s.build_price, 2) : "—";
      // 日线阶段：由后端 `_hunter_build_conformance` **白给**（它本就要算日线特征）⇒ 立即出、零额外取数。
      // 猎手池 5000+ 只，逐只出网取 30min/10min 要 ~1.5s/只（400 只 >5min），故本表**不挂**那两列。
      const _STG = { 1: "①蓄势", 2: "②抬升", 3: "③派发", 4: "④下跌" };
      const _stageTxt = s.build_stage
        ? `<span style="font-size:12px">${esc(_STG[s.build_stage] || "—")}</span>`
        : '<span class="cell-dim">—</span>';
      return `<tr class="h-expand-row" style="${_rowBg}" ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')">
        <td class="mono cell-dim" title="双击看技术分析">${esc(s.code)}</td>
        <td title="双击看技术分析">${esc(s.name)} <button class="mini-btn" style="font-size:10px;padding:0 5px"
          onclick="event.stopPropagation();addToWatchlist('${esc(s.code)}','${esc(s.name)}',this)"
          title="加入建仓股池监控买点">+股池</button></td>
        <td style="text-align:center;white-space:nowrap">${tierBadge(s.tier, s.tier_static, s.tier_reason)}</td>
        <td>${verdictTxt}</td>
        <td class="num">${scoreTxt}</td>
        ${(() => {
          // D1~D11 打分明细（2026-10-17）。后端只下发**非零**维度 ⇒ 大多数票是空的。
          const dims = s.dims || {};
          const ks = Object.keys(dims);
          if (!ks.length) return '<td class="num cell-dim" title="当日没有命中任何 D 维度">0</td>';
          const full = ks.map(k => `${k} ${dims[k]}`).join(" · ");
          const brief = ks.slice(0, 3).map(k => `${k} ${dims[k]}`).join(" · ");
          return `<td class="num" title="${esc(full + "　（合计 " + (s.score || 0) + " 分）")}">`
            + `<b>${s.score || 0}</b>`
            + `<div class="cell-dim" style="font-size:9px;line-height:1.35">${esc(brief)}${ks.length > 3 ? " …" : ""}</div>`
            + `</td>`;
        })()}
        <td class="num">${_metTxt}</td>
        <td style="text-align:center" title="${esc("日线阶段" + "。" + DAY_STAGE_TIP)}">${_stageTxt}</td>
        ${maStateCellHtml(s.build_above_ma20, 20, s.build_above_ma5, 5, undefined, "#d29922", "未站上")}
        ${ma5CellHtml((hunterStructCache[category] || {})[s.code], "h-ma5")}
        <td class="num h-price" data-hcode="${esc(s.code)}" title="**实时**价/涨幅（展开时取、10s 刷新）。⚠️ 刻意不显示扫描时的旧值：扫描可能隔了几天，旧涨幅会严重误导（实测 300821 旧值 +20.01% vs 实时 +5.83%）">${hunterPriceHtml(null)}</td>
        <td class="h-tags">${tagsTxt}</td>
        ${condCellHtml(s.build_conds, condLabels, s.build_verdict === "insufficient_data", s.build_veto)}
        <td class="cell-dim" style="max-width:240px;font-size:11px">${esc(s.build_reason || "")}</td>
      </tr>`;
    };
    // 板块 → 细分（按第一层级归并：半导体设备-测试设备 汇入 半导体设备；多概念股多组重复）→ 个股。
    // 2026-09-01 修复：subGroups 基于**全部股票**(allStocks) 构造，分页按**子分组**（每页整组，子分组头唯一不跨页重复）
    // ——此前基于 paged(当前页50只) 构造，半导体165只分4页 → "半导体设备"子分组头每页重复。
    // 概念归一化（2026-09-01）：大小写统一（端侧AI芯片/端侧ai芯片→端侧AI芯片）；
    // 语义归并（存储接口芯片/存储主控芯片→存储芯片；其他算力芯片→算力芯片）
    const normGk = c => {
      const gk = c.split("-")[0];
      const lower = gk.toLowerCase();
      if (lower === "端侧ai芯片") return "端侧AI芯片";
      if (lower === "存储接口芯片" || lower === "存储主控芯片") return "存储芯片";
      if (lower === "其他算力芯片") return "算力芯片";
      return gk;
    };
    const subGroups = {};
    const subSeen = {};  // gk -> Set(code)：同一子分组内 code 唯一（数据源同 code 多行/多概念 → 不重复）
    for (const s of allStocks) {
      const cons = (s.concepts && s.concepts.length) ? s.concepts : ["未分类"];
      for (const c of cons) {
        const gk = normGk(c);
        (subSeen[gk] = subSeen[gk] || new Set());
        if (subSeen[gk].has(s.code)) continue;
        subSeen[gk].add(s.code);
        (subGroups[gk] = subGroups[gk] || []).push(s);
      }
    }
    const groupKeys = Object.keys(subGroups)
      .sort((a, b) => (subGroups[b].length - subGroups[a].length) || a.localeCompare(b));
    const gStart = pageIdx * PAGE;
    const gPaged = groupKeys.slice(gStart, gStart + PAGE);
    const stockRows = gPaged.map(gk => {
      const gs = subGroups[gk];
      // 2026-10-12 owner：「二级板块没有显示得分情况」。根因=原来的 **`gAvg` 取的是 `s.score`**，
      // 那是**韭研 D1–D11 总得分**（全池均值仅 ~0.8）⇒ 组头显示"均分1"，
      // 与它正下方那一列「得分」（建仓综合得分 0~100）**根本不是同一个数**，看着就像没得分。
      // 改为取**行内同一个 `build_score`**，并显式区分「还没算到(⏳)」与「算了但无值(—)」。
      const _scored = gs.filter(s => typeof s.build_score === "number");
      const gScore = _scored.length
        ? Math.round(_scored.reduce((a, s) => a + s.build_score, 0) / _scored.length) : null;
      const gUp = gs.filter(s => s.limit_up).length;
      const gScoreTxt = gScore !== null
        ? `均分 <b title="本二级板块内「得分」列（建仓综合得分）的平均值">${gScore}</b>`
          + (_scored.length < gs.length
             ? `<span class="cell-dim" style="font-size:10px">（${_scored.length}/${gs.length}只已算）</span>` : "")
        : (gs.some(s => !_buildReady && s.build_verdict === undefined)
           // ⚠️ 不能读 `s._buildMissing`：那是 `hunterRow()` **渲染时才置位**的，
           //    而组头在 `gs.map(hunterRow)` **之前**算 ⇒ 首帧会读到 stale/undefined。
           //    这里用与 hunterRow 完全相同的谓词直接判。
           ? '均分 <span class="cell-dim" title="建仓符合度后台计算中，算完自动填上">⏳更新中</span>'
           : '均分 <span class="cell-dim" title="该二级板块内没有可用的建仓得分">—</span>');
      const head = `<tr class="h-subgroup-head"><td colspan="14">
        <span class="h-subgroup-name"${gkDesc(gk) ? ` title="${esc(gk + "：" + gkDesc(gk))}" style="cursor:help"` : ""}>📂 ${esc(gk)}</span>
        <span class="cell-dim" style="font-size:10px">${gs.length}只 · ${gScoreTxt}${gUp ? ` · 涨停${gUp}` : ""}</span>
      </td></tr>`;
      return head + gs.map(hunterRow).join("");
    }).join("");
    const gTotal = groupKeys.length;
    const pageInfo = gTotal > PAGE
      ? `<div class="h-pager"><button class="mini-btn" onclick="hunterPage('${esc(category)}',-1)" ${gStart===0?'disabled':''}>←</button>
         ${gStart + 1}-${Math.min(gStart + PAGE, gTotal)}/${gTotal} 子分组（${allStocks.length}只）
         <button class="mini-btn" onclick="hunterPage('${esc(category)}',1)" ${gStart + PAGE >= gTotal ? 'disabled' : ''}>→</button></div>` : "";

    const rank = i + 1;
    const medal = rank === 1 ? "🥇" : rank === 2 ? "🥈" : rank === 3 ? "🥉" : "";
    const scoreVal = r["平均分"] || 0;
    const scorePct = Math.min(100, Math.max(0, scoreVal));
    const scoreCls = scoreVal >= 70 ? "h-hot" : scoreVal >= 40 ? "h-warm" : "h-cool";
    const barW = Math.max(2, scorePct);

    const heatVal = r["热度"] != null ? r["热度"] : 0;
    const heatBarW = Math.max(2, Math.min(100, heatVal));

    // 板块内 D 维度「突出项」（2026-10-18 owner：每个板块打出分后要显示哪些 D 项突出）。
    // 数据 = 每只票的 `dims`（韭研 D1~D12 的**非零项**）；板块均分就是这些分的平均。
    // 排序：先按**贡献总分**（对均分影响最大），再按命中只数（广度），取前 3。
    const _dimHits = {}, _dimPts = {};
    let _hasAnyDims = false;
    for (const _s of stocks) {
      const _dm = _s.dims || {};
      const _ks = Object.keys(_dm);
      if (_ks.length) _hasAnyDims = true;
      for (const _k of _ks) {
        const _v = Number(_dm[_k]) || 0;
        _dimHits[_k] = (_dimHits[_k] || 0) + 1;
        _dimPts[_k] = (_dimPts[_k] || 0) + _v;
      }
    }
    const _dimTot = Math.max(1, stocks.length);
    const dimTop = Object.keys(_dimHits)
      .sort((a, b) => (_dimPts[b] - _dimPts[a]) || (_dimHits[b] - _dimHits[a])
        || a.localeCompare(b))
      .slice(0, 3)
      .map(k => ({ k, n: _dimHits[k], pts: _dimPts[k], name: DIM_NAME[k] || k }));
    const dimChips = dimTop.map(x =>
      `<span class="h-dim-chip" title="${esc(x.name)} · ${x.n} 只命中（${Math.round(x.n / _dimTot * 100)}%）`
      + ` · 合计 ${x.pts} 分">${esc(x.k)} <b>×${x.n}</b><i>${x.pts}分</i></span>`).join("");

    // 2026-10-18 owner：「选股猎手的显示方式和市场风向标风格保持一致」——
    // 板块汇总从"全宽横条"改成**方格卡**（同 .beacon-grid 视觉），点格子跨整行展开个股明细。
    // 前 10 名红色、其余绿色（summary_rows 由后端按平均分降序 ⇒ i<10 即前10）。
    const _open = hunterOpenSectors.has(category);
    const _rankCls = i < 10 ? " beacon-top" : " beacon-rest";
    return `<div class="card hunter-tile${_rankCls}${_open ? " open" : ""}" id="hunterCard_${i}" data-category="${esc(category)}">
      <div class="hunter-tile-head" onclick="toggleSectorExpand(this,'${esc(category)}')" title="点击展开/收起个股明细">
        <div class="hunter-line1">
          <span class="bk-arrow" id="hunterArrow_${i}">${_open ? "▾" : "▸"}</span>
          <span class="hunter-rank">${medal || rank}</span>
          <b class="hunter-name">${esc(category)}</b>
          <a href="#" class="hunter-link" onclick="event.stopPropagation();showSectorHistory('${esc(category)}');return false;"
             title="查看板块历史情况">🔗</a>
          <span class="hunter-score ${scoreCls}">${fmt(scoreVal, 1)}<span class="hunter-score-unit">分</span></span>
        </div>
        <div class="h-bar-track"><div class="h-bar-fill ${scoreCls}" style="width:${barW}%"></div></div>
        <div class="hunter-meta">
          ${firstLevelCount}个细分 · ${r["股票数"]||(ss[category]||[]).length||0}只
          ${d5Hits > 0 ? ` · <b class="up">D5×${d5Hits}</b>` : ""}
          ${d6Hits > 0 ? ` · <b class="warn">D6×${d6Hits}</b>` : ""}
          · 涨停 <b>${r["涨停数"]||0}</b>
        </div>
        <div class="hunter-dims" title="本板块最突出的 D 维度（按贡献总分降序；悬停看命中只数与占比）">
          ${dimChips || (_hasAnyDims
            ? '<span class="cell-dim" style="font-size:10px">无 D 命中</span>'
            : '<span class="cell-dim" style="font-size:10px">D 明细未生成（重新运行猎手）</span>')}
        </div>
        <div class="hunter-meta2">
          <span class="h-stage-badge cell-dim">阶段…</span>
          <span class="h-trend-badge ${heatChange != null ? (heatChange > 0 ? 'h-trend-up' : heatChange < 0 ? 'h-trend-down' : 'h-trend-flat') : 'h-trend-flat'}">
            ${trendIcon || '—'} ${heatChange != null ? (heatChange>=0?'+':'')+fmt(heatChange,0) : ''}
          </span>
          <span class="mono cell-dim" style="font-size:10px" title="近几日热度">${trendSpark(trends[category]||[], 'heat', '#d29922')}</span>
          <span class="h-heat-val ${heatCls}">热度 ${heatVal > 0 ? fmt(heatVal, 0) : "—"}</span>
        </div>
        <div class="hunter-leads" title="${esc(r["前三强"]||'')}">${
          (() => { const _lds = stocks.filter(x => x.tier === "龙头");
                   return _lds.length
                     ? `<div style="margin-bottom:2px">${tierBadge("龙头")} <b>${_lds.map(x => esc(x.name)).join(" / ")}</b></div>`
                     : ""; })()
        }${stocks.slice(0, 3).map(s =>
          `<span class="cell-dim">${esc(s.name)}</span><button class="mini-btn" style="font-size:9px;padding:0 4px;margin:0 2px 0 1px"
            onclick="event.stopPropagation();addToWatchlist('${esc(s.code)}','${esc(s.name)}',this)"
            title="加入建仓股池监控买点">+股池</button>`).join(" ") || esc(r["前三强"] || "—")}</div>
      </div>
      <div class="hunter-detail${_open ? " open" : ""}">
        <div class="h-expand">
          ${(hunterTagsCache[category] || _buildReady) ? "" :
            `<div class="h-updating-strip cell-dim" style="font-size:11px;padding:4px 8px;background:rgba(210,153,34,.10);border-left:3px solid #d29922">
               ⏳ 正在更新：${(!_buildReady) ? "建仓符合度（判定/得分/通过/日线阶段）" : ""}${(!_buildReady && !hunterTagsCache[category]) ? " 与 " : ""}${hunterTagsCache[category] ? "" : "技术标签"} —— 后台算完自动填上，无需操作
             </div>`}
          <table><thead><tr>
            <th>代码</th><th>名称</th>
            <th title="板块内梯队（**描述性方位标签，非信号**）：龙头=本板块当日涨幅第1；中军=其余成交额前3（资金核心）；杂毛=其余；板块<8只不标。⚠️ 实测该分层无前瞻区分度（四种定义×四个持有期≈全无差异、分半窗符号翻转、效应量远低于交易成本）⇒ 只看方位，不参与任何判定与排序。依据 doc/research/2026-10-11_板块内龙头中军杂毛_分层实验.md">梯队</th>
            <th title="与建仓信号扫描同口径（go→signal / approaching / watch_signal / weak）">判定</th>
            <th class="num" title="综合得分（与建仓表同口径；破5日线扣70且不给signal）">得分</th>
            <th class="num" title="韭研 D1~D12 打分合计（选股猎手的原始评分，满分 47；D3/D10 未在 config 启用）。⚠️ 与左边「得分」不是一回事：那个是建仓综合得分 0~100。下方小字＝命中的维度与分数。&#10;D1 强势形态且新高 8 · D2 强势形态 6 · D4 首板资金池 3 · D5 潜在突破10日 2 · D6 潜在突破5日 1 · D7 持续性(二板+) 5 · D8 情绪分数(一字板) 6 · D9 活跃程度(近10日有涨停) 3 · D11 刚站上5日线 2 · D12 趋势性(站上20日线) 8 · 大额(成交额≥50亿) 3">D分<br><span style="font-size:9px;opacity:.6">D1~D12</span></th>
            <th class="num" title="时机门控通过数（市场有方向/多头结构/回撤到位）">通过</th>
            <th title="日线四阶段（描述状态，紧挨得分/通过便于对照）：②抬升/③派发/④下跌/①蓄势。⚠️ 实测横截面IC与直觉相反——抬升/追高前瞻更差(IC −0.031~−0.042, t=−12~−15)、下跌/超跌前瞻更好(IC +0.020~+0.034, t=+7~+12)，分半窗两段同号；效应量小(IC≈0.04)仅作相对强弱参照">日线阶段</th>
            <th title="现价 vs 20 日简单均线（观察列，不驱动判定）。选股猎手口径：**站上=绿、其它=黄**（建仓信号扫描用绿/红）；5 日线状态放在单元格 tooltip。⚠️「得分」里那个 −70 悬崖扣分走的是 **5 日线**口径（owner 决定保留），与本列不是同一条线；行底色也仍按 5 日线">20日线<br><span style="font-size:9px;opacity:.6">规则口径</span></th>
            <th title="30 分钟 K 线的 5 根均线（现价站上/跌破 + 距上次翻转多少根）。**与「持仓 30 分钟风险体检」同源**（position_structure，同一缓存）。⚠️ 实测无前瞻信息量 ⇒ 只展示，不参与任何判定">30m MA5<br><span style="font-size:9px;opacity:.6">仅展示</span></th>
            <th class="num" title="**实时**价 + 实时涨幅（展开时取、10s 刷新；取不到显示 ⏳/—）。⚠️ 刻意不显示扫描时的旧价旧涨幅——扫描可能隔了几天，旧涨幅会严重误导">价<br><span style="font-size:9px;opacity:.6">实时涨幅</span></th>
            <th>技术标签</th>
            <th title="时机条件：市场有方向/多头结构/回撤到位 · 末位🚫=否决因子（爆量≥3倍或偏离MA60>+20%）">时机条件</th>
            <th>理由</th>
          </tr></thead>
          <tbody class="h-sector-group" data-category="${esc(category)}">${stockRows}</tbody></table>
          ${pageInfo}
          <div class="cell-dim" style="font-size:10px;margin-top:3px">悬停技术标签看解释 · 点击方格收起
            ${hunterTagsCache[category] ? "" : ` · <b class="h-updating-foot warn">⏳ 技术标签更新中（本板块 ${(ss[category]||[]).length} 只，约 30~60 秒）</b>`}</div>
        </div>
      </div>
    </div>`;
  }).join("");

  // 2026-10-09 owner：突破箱体支持**任选交易日**扫描，结果按日保存（breakout_{date}.json）
  const breakoutCard = `
    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div class="card-title">🚀 突破箱体·有效突破（昨收 ≤ 箱体上沿 &lt; 今价，幅度 0.3~8%）
        <input type="date" id="hunterBreakoutDate" value="${esc(h.date || todayStr())}"
          style="margin-left:8px;background:transparent;color:inherit;border:1px solid var(--border-soft);border-radius:4px;padding:1px 4px;font-size:12px"
          title="选择任意交易日扫描（结果按日保存）；选日期不会自动扫，点右侧按钮">
        <button class="mini-btn" id="hunterBreakoutBtn" style="margin-left:8px"
          onclick="loadBreakoutStocks(true, document.getElementById('hunterBreakoutDate').value)">🔄 扫描该日</button>
        <span class="cell-dim" id="hunterBreakoutMeta" style="font-size:11px"></span>
      </div>
      <div id="hunterBreakoutBody"><div class="empty">选日期后点「🔄 扫描该日」（全池 5000+ 只，约 1~2 分钟）；结果会按日保存，再选同一天直接秒回。</div></div>
    </div>`;

  // ── 猎手总览（2026-10-15，owner：「选股猎手完成后在头部有一个总览」）──────────────
  // 口径：**只给结论**（三个数 + 最强三块 + 两个计数），**刻意不重复下表**——下表已经把
  // 全部概念逐行列出来了，总览再堆一遍就等于没有总览（沿用 owner「要结论不要信号堆料」）。
  // GO 计数：`build_go === true`（`buildBadge` 同源判据）。**未算出时显示 ⏳ 而不是 0**——
  // `_buildReady` 为假时 `build_go` 全是 undefined，此时报 0 是撒谎。
  const _goCount = Object.keys(ss).reduce((n, k) =>
    n + ((ss[k] || []).filter(s => s && s.build_go === true).length), 0);
  const _trendNum = (v) => {                       // 与下表 trendNum 同一正则口径
    const m = String(v == null ? "" : v).match(/[+-]?\d+/);
    return m ? parseInt(m[0], 10) : null;
  };
  const _top3 = sumRows.slice()
    .sort((a, b) => (Number(b["平均分"]) || 0) - (Number(a["平均分"]) || 0)).slice(0, 3);
  const _heatUp = sumRows.filter(r => { const n = _trendNum(r["趋势"]); return n != null && n > 0; }).length;
  const _heatDn = sumRows.filter(r => { const n = _trendNum(r["趋势"]); return n != null && n < 0; }).length;
  const overviewCard = `
    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:8px;align-items:baseline">
        <div style="font-size:13px">
          <span class="cell-dim">打分池</span> <b>${h.pool_size}</b> 只
          <span class="cell-dim" style="margin-left:12px">概念</span> <b>${sumRows.length}</b> 个
          <span class="cell-dim" style="margin-left:12px">GO</span>
          <b style="color:${(_buildReady && _goCount) ? "var(--green)" : "var(--text-faint)"}"
             title="建仓符合度判定为 GO 的个股数（全概念合计）">${_buildReady ? _goCount : "⏳"}</b> 只
        </div>
        <div class="cell-dim mono" style="font-size:11px">
          <span class="live-dot"></span> ${esc(h.refreshed_at || '')}
        </div>
      </div>
      <div style="margin-top:8px;font-size:12px;display:flex;gap:18px;flex-wrap:wrap;align-items:baseline">
        <span class="cell-dim">最强</span>
        ${_top3.map((r, i) => `<span title="按均分排序">
            <span class="cell-dim">${i + 1}</span>
            <b>${esc(r["板块"] || "")}</b>
            <span class="cell-dim">${Number(r["平均分"]) || 0}分 · 涨停${Number(r["涨停数"]) || 0}</span>
          </span>`).join("") || '<span class="cell-dim">—</span>'}
      </div>
      <div class="cell-dim" style="margin-top:6px;font-size:11px">
        热度 <span class="up">↑${_heatUp}</span> 个概念
        <span class="down" style="margin-left:6px">↓${_heatDn}</span>
        <span style="margin-left:14px"
          title="${_buildReady
            ? `已算出 ${h.build_n || 0} 只；其余 ${Math.max(0, (h.build_total || 0) - (h.build_n || 0))} 只无本地日线缓存 ⇒ 判定/得分列显示 “—”`
            : "建仓符合度后台计算中…"}">建仓符合度 ${h.build_n || 0}/${h.build_total || 0}${_buildReady ? "" : " ⏳"}</span>
      </div>
    </div>`;

  // 保住「GO 只数图」的 DOM 节点（同 renderLive 的闪烁修法）：下面 innerHTML 每轮重建，
  // 节点一换就得 dispose+init，canvas 被拆掉再建 = 闪。
  const _keepGoChart = _keepNode("hunterGoChart");

  el.innerHTML = `
    ${overviewCard}
    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div class="card-title">📊 每日建仓符合（GO）只数 · 近 60 日
        <span class="cell-dim" style="font-weight:normal;font-size:11px">柱=当日 GO 只数（池内可算 ${h.pool_size || 0} 只口径）· 线=5日均线 · 悬停看当日市场态势</span>
      </div>
      <div id="hunterGoSlot"></div>
      <div class="cell-dim" style="font-size:10px;margin-top:4px">
        <span style="color:${HUNTER_GO_BAR}">■</span> 当日 GO 只数
        <span style="color:${HUNTER_GO_MA};margin-left:8px">━</span> ${HUNTER_GO_MA_N}日均线
        <span style="margin-left:8px">· <b>震荡市 GO 恒为 0</b>（策略降频，非故障）—— 悬停看当日 regime</span>
        <span style="margin-left:8px">· 👆 <b>点柱子</b>看那天符合的个股与板块</span>
      </div>
      <div id="hunterGoList" class="cell-dim"
        style="font-size:11px;margin-top:8px;padding-top:7px;border-top:1px solid var(--border-soft)"></div>
    </div>
    ${(_buildReady && (h.build_n || 0) >= (h.build_total || 0)) ? "" : `
      <div class="cell-dim" style="font-size:11px;margin-bottom:8px;padding:5px 9px;background:rgba(210,153,34,.12);border-left:3px solid #d29922">
        ${_buildReady
          ? `ℹ️ 建仓符合度 <b>${h.build_n}/${h.build_total}</b> 只（其余无本地日线缓存 ⇒ 判定/得分/通过 显示 “—”）`
          : `⏳ <b>建仓符合度本次未算出</b>（判定/得分/通过/日线阶段 显示 ⏳）—— 点右上「🔄 刷新数据」重跑`}
      </div>`}

    ${breakoutCard}

    <div class="card" style="margin-bottom:10px;padding:10px 14px">
      <div class="card-title">📐 D1~D12 打分规则
        <span class="cell-dim" style="font-weight:normal;font-size:11px">韭研原始评分（满分 47 · D3/D10 未启用）· 与「得分」列的建仓综合得分 0~100 是两套口径</span>
      </div>
      <div class="d-rules-grid">${DIM_RULES.map(r => `<span class="d-rule" title="${esc(r[3])}"><b>${r[0]}</b>${r[1]}<i>${r[2]}</i></span>`).join("")}</div>
      <div class="cell-dim" style="margin-top:4px;font-size:11px">
        每个方格里的 <b>D..×N</b> = 本板块最突出的 3 个维度（按贡献总分降序；悬停看命中只数与合计分）；
        个股明细「D分」列下方小字 = 该股命中的维度与分数。
      </div>
    </div>

    <div class="cell-dim" style="font-size:11px;margin:2px 0 8px">
      <span style="color:#f85149">■</span> 前 10 名
      <span style="color:#2ea043;margin-left:6px">■</span> 其余
      <span style="margin-left:10px">共 <b>${sumRows.length}</b> 个概念 · 点击方格展开个股明细</span>
    </div>
    <div class="hunter-grid">${sumBody}</div>

    <div class="cell-dim" style="font-size:10px;margin-top:6px">数据源: stock_hunter (韭研概念打分) · 点击方格展开个股明细(D5/D6异常) · 热度趋势vs前日</div>`;

  // GO 图容器放回占位处（复用旧节点 ⇒ 同节点走原地 setOption，不闪）
  _putBackNode("hunterGoSlot", _keepGoChart, '<div id="hunterGoChart" style="height:130px"></div>');
  drawHunterGoChart();
  // 明细列表随 innerHTML 一起被重建 ⇒ 用缓存重填（选中日与数据都在模块级，不丢）
  renderHunterGoList();

  // 分页/重渲染后恢复此前展开的板块（避免跳页收起），并重填已缓存的技术标签
  if (hunterOpenSectors.size) {
    el.querySelectorAll("tbody.h-sector-group").forEach(tb => {
      const cat = tb.getAttribute("data-category");
      if (hunterOpenSectors.has(cat)) {
        const card = tb.closest && tb.closest(".hunter-tile");
        const wrap = card && card.querySelector(".hunter-detail");
        if (wrap) wrap.classList.add("open");
        if (card) {
          card.classList.add("open");
          const arw = card.querySelector(".bk-arrow");
          if (arw) arw.textContent = "▾";
        }
        applySectorTags(tb, cat);
        loadSectorTags(cat);      // 翻页后补当前页缺的 code（已缓存的跳过）
      }
    });
  }
  // 2026-10-09: **预热前几个（最大的）板块的技术标签**——否则展开那一刻才拉，冷启动先回空、
  // 要等后台算完（约 4~20s）。预热后用户展开即命中缓存。
  // 2026-10-11: 仓位结构同批预热**只取前 2 个板块**——它每只要多打一次 5min（~0.5s/只，
  // 比标签贵得多），且本机 I/O 是瓶颈；不预热也能用（展开即取）。
  try {
    const _big = Object.keys(ss).sort((a, b) => ((ss[b] || []).length - (ss[a] || []).length));
    _big.slice(0, 6).forEach(cat => { if (!hunterTagsCache[cat]) loadSectorTags(cat); });
  } catch (e) { /* 静默 */ }
}

/* ---- 持仓日线体检 ---- */
// 持仓体检加载器：后端只用已预热的图表缓存算（见 t_gui.load_ob_analysis），未就绪的持仓
// 会返回 pending>0 ⇒ 稍后自动重拉补全，避免冷启动时把主线程同步冻住 14s。
let _obRetryTimer = null;
async function loadOBAnalysis() {
  if (_obRetryTimer) { clearTimeout(_obRetryTimer); _obRetryTimer = null; }
  if (chartModalOpen) {                    // 弹窗打开：暂停体检重算
    _obRetryTimer = setTimeout(loadOBAnalysis, 10000);
    return null;
  }
  try {
    const ob = await apiCall("load_ob_analysis");
    renderOB(ob || {});
    // 2026-10-13：**总是**排下一次（原来全就绪后永久停轮 ⇒ 现价/判定/标签冻结到手动刷新）。
    // 30min 判定/仓位结构未热（后台预热）→ 3s；体检后台算未回 → 5s；仅图表未热 → 12s；
    // 全就绪 → 60s 低频自刷（后端 45s TTL + 缓存路径就地覆盖 structure/price，成本极低）。
    let delay = 60000;
    if (ob && (ob.pending || ob.m30_pending || ob.struct_pending)) {
      if ((ob.m30_pending || ob.struct_pending) && !ob.pending) delay = 3000;
      else if (ob.pending) delay = 5000;
      else delay = 12000;
    }
    _obRetryTimer = setTimeout(loadOBAnalysis, delay);
    return ob;                              // 2026-10-13：供「🔄 刷新」判断成败（原来吞错无法回报）
  } catch (e) { /* 静默：体检非关键路径 */ return null; }
}

// 持仓体检 · 「🔄 刷新」（2026-10-13 owner）：**只重拉最新状态并就地重绘**，不做任何重算。
let obRefreshRunning = false;
async function refreshOBNow() {
  if (obRefreshRunning) return;
  const btn = document.getElementById("obRefreshBtn");
  obRefreshRunning = true;
  if (btn) { btn.disabled = true; btn.textContent = "⏳ 刷新中"; }
  try {
    const ob = await loadOBAnalysis();
    if (ob) {
      statusEl(`已刷新持仓体检${ob.pending ? "（后台仍在算，稍后自动补齐）" : ""}`, "ok");
    } else {
      statusEl("刷新持仓体检失败（后端未返回，详见日志）", "err");
    }
  } finally {
    obRefreshRunning = false;
    if (btn) { btn.disabled = false; btn.textContent = "🔄 刷新"; }
  }
}

let _obLast = null;      // 最近一次体检结果（多窗口看盘的选股列表用它）
function renderOB(ob) {
  if (ob && ob.stocks && ob.stocks.length) _obLast = ob;   // 多窗口看盘的选股列表用它
  const el = document.getElementById("obBody");
  if (!ob || !ob.stocks || !ob.stocks.length) {
    el.innerHTML = `<div class="empty">${ob && ob.pending
      ? "持仓体检生成中…（图表预热未完成，稍后自动填充）" : "无持仓体检数据"}</div>`;
    return;
  }
  const _VCLS = { high: "badge signal", watch: "badge approach", bull: "badge bear" };
  const rows = ob.stocks.map(s => {
    if (s.error) return `<tr><td colspan="14" class="cell-dim">${esc(s.code)}: ${esc(s.error)}</td></tr>`;
    const ob_ = s.overbought || {};
    const vd = s.verdict || {};           // 2026-10-09：30min 综合判定 {level,label,reason}
    const dv = s.divergence || {};        // 30min {type,bars_ago}（进 tooltip）
    const tf = s.top_features || {};      // 30min 顶部特征 T1–T4（进 tooltip）
    const rsiHot = (ob_.rsi || 0) > 70;
    const rsiExtreme = (ob_.rsi || 0) > 80;
    const kdjHot = (ob_.kdj || 0) > 100;
    const cciHot = (ob_.cci || 0) > 100;
    const trendTxt = s.trend === "down" ? `<b class="down">下行↘</b>`
      : s.trend === "up" ? `<b class="up">上行↗</b>` : `<span class="cell-dim">震荡→</span>`;
    const trendSrc = s.trend_src === "daily" ? ` <span class="cell-dim" style="font-size:10px">(日线)</span>` : "";
    const vCls = _VCLS[vd.level];
    const vCell = vCls ? `<span class="${vCls}">${esc(vd.label || "")}</span>`
                       : `<span class="cell-dim">${esc(vd.label || "—")}</span>`;
    const tfTip = (tf.ok && tf.fired && tf.fired.length) ? `顶部特征：${tf.fired.join("、")}` : "无顶部特征";
    const dvTip = dv.type ? `${dv.type}${dv.bars_ago != null ? `·${dv.bars_ago}根前` : ""}` : "无背离";
    const _obBg = ((s.tags || []).some(t => (t.label || "") === "破5日线"))
      ? "background:rgba(248,81,73,.10);" : "";
    return `<tr ondblclick="openStockChart('${esc(s.code)}','${esc(s.name)}')" style="cursor:pointer;${_obBg}" title="双击看K线">
      <td>${esc(s.name)} <span class="mono cell-dim">${esc(s.code)}</span></td>
      <td class="num" data-obcode="${esc(s.code)}">${fmt(s.price, 2)}</td>
      <td title="30min 三层状态机趋势${s.trend_src === 'daily' ? '（30min不可用，已回退日线）' : ''}">${trendTxt}${trendSrc}</td>
      ${dayStageCellHtml(s.structure)}
      ${ma20FlagCellHtml(s.above_ma20,
        "日线不足 20 根或未预热 —— 显示 — 而不当作「未站上」",
        s.ma20_daily ? `MA20=${s.ma20_daily}，现价=${s.price}` : "")}
      <td class="num ${rsiHot ? 'up' : 'cell-dim'}" title="日线 RSI>70 提示 / >80 高危">${fmt(ob_.rsi, 0)} ${rsiHot ? `<span class="badge ${rsiExtreme ? 'signal' : 'approach'}">⚠</span>` : ''}</td>
      <td class="num ${kdjHot ? 'up' : 'cell-dim'}" title="日线 KDJ-J>100 属动能(不计风险)">${fmt(ob_.kdj, 0)}</td>
      <td class="num ${cciHot ? 'up' : 'cell-dim'}" title="日线 CCI>100 属动能(不计风险)">${fmt(ob_.cci, 0)}</td>
      <td>${ob_.boll ? '<span class="badge chop" title="日线破BOLL上轨=突破动能">破</span>' : '<span class="cell-dim">—</span>'}</td>
      <td style="max-width:200px;line-height:1.7">${(s.tags || []).map(t => tagBadge(t)).join(" ") || '<span class="cell-dim">—</span>'}</td>
      <td title="30min 综合判定（趋势+顶部/底背离特征）&#10;${esc(tfTip)}&#10;${esc(dvTip)}">${vCell}</td>
      ${structCellHtml(s.structure)}
      ${ma5CellHtml(s.structure)}
      <td class="cell-dim" style="max-width:280px;line-height:1.7">${esc(vd.reason || "—")}</td>
    </tr>`;
  }).join("");
  el.innerHTML = `
    <div class="card" style="overflow-x:auto">
      <div style="font-size:11px;line-height:1.9;margin-bottom:6px" class="cell-dim">
        <b>30min 判定</b>（只看这一列）：<span class="badge signal">🔴 减仓/避高</span>＝出现 <b>T1顶背离</b> 或 <b>T2量价背离</b>（981只×540日验证：H=4 下跌占比 +29/+32pp） ·
        <span class="badge approach">🟠 盯紧</span>＝T3顶分型 与 T4均线压制 <b>同现</b>（弱信号） ·
        <span class="badge bear">🟢 偏好</span>＝无顶信号且<u>有</u>新鲜底背离(≤16根/2日) ·
        <span class="cell-dim">⚪ 中性</span>＝其余（<b>单个</b> T3/T4 不报警：实测无前瞻区分度）
      </div>
      <table><thead><tr>
        <th>股票</th><th class="num">现价</th><th>趋势(30m)</th>
        <th title="日线四阶段（描述状态）：②抬升/③派发/④下跌/①蓄势。⚠️ 实测横截面IC与直觉相反——抬升/追高前瞻更差(t=−12~−15)、下跌/超跌前瞻更好(t=+7~+12)，效应量小(IC≈0.04)仅作相对强弱参照">日线阶段</th>
        <th title="是否站上 20 日线（观察列，不驱动判定）。口径与「建仓信号扫描」/「选股猎手」同款：收盘价 > 20 日简单均线。⚠️ 本表不做 −70 悬崖扣分（那是建仓表 5 日线的口径），本列纯展示">20日线<br><span style="font-size:9px;opacity:.6">观察</span></th>
        <th class="num" title="日线 RSI>70超买">RSI</th>
        <th class="num" title="日线 KDJ J>100超买">KDJ-J</th>
        <th class="num" title="日线 CCI>100超买">CCI</th>
        <th title="日线收盘破BOLL上轨">BOLL</th>
        <th title="与建仓扫描同口径（通道/箱体/筑底顶/背离/超买/破均线）">技术标签</th>
        <th title="30min 趋势+顶部/底背离特征 → 重视程度（读一列即可）">30min判定</th>
        <th title="仓位结构（30min/10min，与建仓信号扫描**同一真源**，供仓位管理参考）：止损=10min 最近已确认摆动低点；阶段=10min 四阶段；30m=30min MA65 中期方向；折价=现价/止损-1（到-4.3%记满）。破位时给三档下档支撑挂单价与5日成交率。⚠️ 是**结构参考、不是买卖信号**。悬停看明细">仓位结构</th>
        <th title="30min MA5 状态（**纯展示**）：站上/跌破 5×30min 均线，及距上次翻转几根。⚠️ 实测**无前瞻信息量**（同日同bar-of-day对照 |diff|≤0.03pp）⇒ 仅供参考，不参与任何判定">30m MA5<br><span style="font-size:9px;opacity:.6">仅展示</span></th>
        <th title="判定依据">依据</th>
      </tr></thead><tbody>${rows}</tbody></table>
      <div class="cell-dim" style="font-size:10px;margin-top:4px">口径(2026-10-09)：判定=30min顶部/底背离特征综合（依据 t_io/validation/m30_top 981只×540日离线验证）· 趋势=30min三层状态机 · RSI/KDJ/CCI/BOLL 为日线超买指标（不驱动判定）· <b>20日线</b>=收盘价 vs 20 日简单均线（2026-10-14 新增，与建仓表/猎手表同口径；**观察列，不驱动判定**）· 双击行看K线</div>
    </div>`;
}

/* ================= 每日大盘复盘（LLM，2026-08-23 新增） ================= */
let _reviewPoll = null;
const LLM_PROVIDER_BASE = {
  deepseek: "https://api.deepseek.com",
  qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  openai: "https://api.openai.com/v1",
  moonshot: "https://api.moonshot.cn/v1",
  custom: "",
};

function renderMarkdown(md) {
  /* 2026-08-23: 可视化——标题层级/加粗/表格(表头+斑马纹+数字右对齐) + 一句话结论卡片 + 涨跌红绿 + 触发项琥珀 */
  if (!md) return '<div class="empty">无内容</div>';
  const inline = (t) => esc(t)
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\*(.+?)\*/g, "<i>$1</i>")
    .replace(/`(.+?)`/g, '<code class="rv-code">$1</code>');
  const cellTd = (c, isHeader) => {
    const txt = inline(c);
    const s = String(c).trim();
    let numCls = "", style = "";
    const num = !isNaN(parseFloat(s.replace(/[%+−\-\s]/g, ""))) && s !== "";
    if (num) numCls = "num";
    if (!isHeader && /%/.test(s)) {                       // 涨跌幅：A股红涨绿跌
      const n = parseFloat(s.replace(/[%,+\s]/g, ""));
      if (!isNaN(n)) style = n > 0 ? "color:#f85149;font-weight:600" : n < 0 ? "color:#3fb950" : "";
    } else if (!isHeader && /(破MA20|破MA60|量比|铁顶|铁底|分水岭|生命线|支撑|压力)/.test(s)) {
      style = "color:#9a6200;font-weight:500";             // 关键位/触发项：深琥珀(2026-10-09 原 #d29922 淡黄难读)
    }
    return `<td${numCls ? ` class="${numCls}"` : ""}${style ? ` style="${style}"` : ""}>${txt}</td>`;
  };
  const lines = String(md).split("\n");
  const html = [];
  let tableRows = [];
  const flushTable = () => {
    if (!tableRows.length) return;
    const rows = tableRows.map((cells, i) =>
      `<tr${i === 0 ? ' class="rv-th"' : (i % 2 === 0 ? ' class="rv-alt"' : "")}>${cells.map(c => cellTd(c, i === 0)).join("")}</tr>`).join("");
    html.push(`<table class="rv-table"><tbody>${rows}</tbody></table>`);
    tableRows = [];
  };
  let conclusionPending = false;
  lines.forEach(line => {
    const t = line.trim();
    if (t.startsWith("|")) {
      const cells = t.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      if (!cells.every(c => /^[-: ]+$/.test(c))) tableRows.push(cells);
      return;
    }
    flushTable();
    if (t === "---" || /^={3,}$/.test(t)) html.push('<hr class="rv-hr">');
    else if (t.startsWith("## 0") || t.includes("一句话结论")) {
      html.push(`<h3 class="rv-h3">${inline(t.replace(/^#+\s*/, ""))}</h3>`);
      conclusionPending = true;
    }
    else if (conclusionPending && t) {                     // 一句话结论正文 → 醒目卡片
      html.push(`<div class="rv-conclusion">${inline(t)}</div>`);
      conclusionPending = false;
    }
    else if (t.startsWith("#### ")) html.push(`<h5 class="rv-h5">${inline(t.slice(5))}</h5>`);
    else if (t.startsWith("### ")) html.push(`<h4 class="rv-h4">${inline(t.slice(4))}</h4>`);
    else if (t.startsWith("## ")) html.push(`<h3 class="rv-h3">${inline(t.slice(3))}</h3>`);
    else if (t.startsWith("# ")) html.push(`<h2 class="rv-h2">${inline(t.slice(2))}</h2>`);
    else if (t.startsWith("- ")) html.push(`<div class="rv-li">• ${inline(t.slice(2))}</div>`);
    else if (/^\d+[\.、]/.test(t)) html.push(`<div class="rv-li">${inline(t)}</div>`);
    else if (t) html.push(`<div class="rv-p">${inline(t)}</div>`);
    else html.push('<div style="height:6px"></div>');
  });
  flushTable();
  return `<div class="rv">${html.join("\n")}</div>`;
}

/* LLM 深度复盘 → 分卡片渲染（2026-08-29）：按 ## 章节拆分为独立卡片，
   一句话结论/操作含义用醒目样式突出。
   （2026-08-31）**加粗** 即 LLM 标记的重点，直接渲染为红色高亮。 */
function renderReviewAsCards(md) {
  if (!md) return '<div class="empty">无复盘内容</div>';
  const lines = String(md).split("\n");
  const sections = [];
  let cur = { title: "", lines: [] };
  const push = () => {
    if (cur.title || cur.lines.length) sections.push({ ...cur });
    cur = { title: "", lines: [] };
  };
  lines.forEach(line => {
    const t = line.trim();
    if (/^##\s+/.test(t)) {
      push();
      cur.title = t.replace(/^##\s+/, "");
    } else {
      cur.lines.push(line);
    }
  });
  push();

  const inline = (t) => esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<span class="rv-red">$1</span>')  // 加粗=重点，标红
    .replace(/\*(.+?)\*/g, "<i>$1</i>")
    .replace(/`(.+?)`/g, '<code class="rv-code">$1</code>')
    // 重点标红：结论性/风险性/操作性短语（2026-08-29 用户要求）
    .replace(/(一句话结论|操作含义|次日推演|关键位|共振结论|注意|警惕|风险|机会|突破|跌破|放量|缩量|上涨|下跌|买入|卖出|持有|减仓|加仓|支撑|压力)/g,
             '<span class="rv-red">$1</span>');

  const renderBody = (lines) => {
    const html = [];
    let tableRows = [];
    const flushTable = () => {
      if (!tableRows.length) return;
      const rows = tableRows.map((cells, i) => {
        const tds = cells.map((c, j) => {
          const txt = inline(c);
          const s = String(c).trim();
          let style = "";
          if (i > 0 && /%/.test(s)) {
            const n = parseFloat(s.replace(/[%,+\s]/g, ""));
            if (!isNaN(n)) style = n > 0 ? "color:#f85149;font-weight:600" : n < 0 ? "color:#3fb950" : "";
          }
          return `<td${i === 0 ? ' style="font-weight:700"' : ""}${style ? ` style="${style}"` : ""}>${txt}</td>`;
        }).join("");
        return `<tr${i === 0 ? ' class="rv-th"' : (i % 2 === 0 ? ' class="rv-alt"' : "")}>${tds}</tr>`;
      }).join("");
      html.push(`<table class="rv-table"><tbody>${rows}</tbody></table>`);
      tableRows = [];
    };
    lines.forEach(line => {
      const t = line.trim();
      if (t.startsWith("|")) {
        const cells = t.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        if (!cells.every(c => /^[-: ]+$/.test(c))) tableRows.push(cells);
        return;
      }
      flushTable();
      if (t === "---" || /^={3,}$/.test(t)) html.push('<hr class="rv-hr">');
      else if (t.startsWith("### ")) html.push(`<h4 class="rv-h4">${inline(t.slice(4))}</h4>`);
      else if (t.startsWith("#### ")) html.push(`<h5 class="rv-h5">${inline(t.slice(5))}</h5>`);
      else if (t.startsWith("- ")) html.push(`<div class="rv-li">• ${inline(t.slice(2))}</div>`);
      else if (/^\d+[\.、]/.test(t)) html.push(`<div class="rv-li">${inline(t)}</div>`);
      else if (t) html.push(`<div class="rv-p">${inline(t)}</div>`);
      else html.push('<div style="height:6px"></div>');
    });
    flushTable();
    return html.join("\n");
  };

  const isKey = (title) => /一句话结论|操作含义|次日推演|关键位|共振结论/.test(title);
  const cards = sections.map(sec => {
    const body = renderBody(sec.lines);
    if (!body.trim()) return "";
    const key = isKey(sec.title);
    return `<div class="card rv-sec-card ${key ? "rv-sec-key" : ""}">
      <div class="rv-sec-title">${esc(sec.title)}</div>
      <div class="rv-sec-body">${body}</div>
    </div>`;
  }).filter(Boolean);
  return cards.length ? `<div class="rv-cards-grid">${cards.join("")}</div>` : '<div class="empty">无复盘内容</div>';
}

/* ===== 复盘可视化：解析横评表→条形图、关键位表→卡片（2026-08-23） ===== */
function parseCrossTable(md) {
  const lines = String(md).split("\n");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i].trim();
    if (l.startsWith("|") && l.includes("指数") && l.includes("当日")) {
      const rows = [];
      let j = i + 1;
      while (j < lines.length && lines[j].trim().startsWith("|")) {
        const cells = lines[j].trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
        if (!cells.every(c => /^[-: ]+$/.test(c))) rows.push(cells);
        j++;
      }
      if (rows.length >= 2) return rows;
    }
  }
  return [];
}

function renderReviewCharts(md, meta) {
  const wrap = document.getElementById("reviewChartWrap");
  if (!wrap) return;
  const rows = parseCrossTable(md);
  const crossRows = (meta && meta.cross && meta.cross.rows) || rows;
  const extra = (meta && meta.extra) || {};
  renderVibeCards(crossRows, extra);
  if (rows.length < 2) { wrap.style.display = "none"; return; }
  const header = rows[0];
  const iOf = n => header.indexOf(n);
  const iChg = iOf("当日%"), i5 = iOf("近5日%"), iPos = iOf("20日位置");
  const data = rows.slice(1).filter(r => r[0]);
  const names = data.map(r => r[0]);
  const num = (v, dflt) => { const n = parseFloat(String(v || "").replace(/[%+\s]/g, "")); return isNaN(n) ? dflt : n; };
  const chg1 = data.map(r => num(r[iChg], 0));
  const chg5 = data.map(r => num(r[i5], 0));
  const pos = data.map(r => num(r[iPos], 50));

  wrap.style.display = "";
  let chart = echInstances["reviewChart"];
  if (!chart) chart = echarts.init(document.getElementById("reviewChart"));
  echInstances["reviewChart"] = chart;
  const barColor = v => v >= 0 ? "#f85149" : "#3fb950";   // A股红涨绿跌
  chart.setOption({
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "#161b22", borderColor: "#30363d", textStyle: { color: "#c9d1d9", fontSize: 12 } },
    legend: { top: 0, textStyle: { color: "#8b949e" }, data: ["当日%", "近5日%"] },
    grid: { left: 82, right: 44, top: 30, bottom: 26 },
    xAxis: { type: "value", axisLabel: { color: "#8b949e", fontSize: 10 }, splitLine: { lineStyle: { color: "rgba(139,148,158,.15)" } } },
    yAxis: { type: "category", data: names, axisLabel: { color: "#8b949e", fontSize: 11 }, axisLine: { lineStyle: { color: "#30363d" } } },
    series: [
      { name: "当日%", type: "bar", barWidth: 9, data: chg1.map(v => ({ value: v, itemStyle: { color: barColor(v) } })), label: { show: true, position: "right", color: "#c9d1d9", fontSize: 10 } },
      { name: "近5日%", type: "bar", barWidth: 9, data: chg5.map(v => ({ value: v, itemStyle: { color: "rgba(255,140,90,.75)" } })), label: { show: true, position: "right", color: "#8b949e", fontSize: 10 } },
    ],
  }, true);
  chart.resize();
  renderKeyLevels(md);
}

function renderKeyLevels(md) {
  const el = document.getElementById("reviewKeyLevels");
  if (!el) return;
  const lines = String(md).split("\n");
  let inTable = false, rows = [];
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i].trim();
    if (l.startsWith("|") && l.includes("类型") && l.includes("位置")) { inTable = true; continue; }
    if (inTable && l.startsWith("|")) {
      const cells = l.replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      if (!cells.every(c => /^[-: ]+$/.test(c)) && cells[0]) rows.push(cells);
    } else if (inTable && rows.length) { break; }
  }
  if (!rows.length) { el.innerHTML = ""; return; }
  const cards = rows.map(r => `<div style="flex:1;min-width:130px;padding:8px 10px;background:var(--bg-soft);border:1px solid var(--border);border-radius:6px">
    <div style="font-size:11px;color:#9a6200;font-weight:600">${esc(r[0])}</div>
    <div style="font-size:15px;font-weight:700;color:#e6edf3;margin:3px 0;font-family:var(--mono)">${esc(r[1])}</div>
    <div style="font-size:10px;color:var(--text-dim)">${esc(r[2] || "")}</div>
  </div>`).join("");
  el.innerHTML = `<div style="font-size:12px;font-weight:700;color:var(--accent);margin-bottom:6px">关键位一览</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px">${cards}</div>`;
}

/* 复盘总结卡（2026-08-23）：一句话结论 + 操作含义要点，置顶醒目 */
function renderSummary(md) {
  const el = document.getElementById("reviewSummary");
  if (!el) return;
  const lines = String(md).split("\n");
  let conclusion = "", inOp = false, opPoints = [];
  for (let i = 0; i < lines.length; i++) {
    const t = lines[i].trim();
    if (/^##\s*0/.test(t) || t.includes("一句话结论")) {
      for (let j = i + 1; j < lines.length; j++) {
        const s = lines[j].trim();
        if (!s) continue;
        conclusion = s.replace(/\*\*/g, "");
        break;
      }
    }
    if (/^##\s*7/.test(t) || t.includes("操作含义")) inOp = true;
    else if (/^##\s*/.test(t)) inOp = false;
    if (inOp && t.startsWith("- ")) opPoints.push(t.slice(2).replace(/\*\*/g, ""));
  }
  let html = "";
  if (conclusion) html += `<div class="rv-summary-concl">📌 ${esc(conclusion)}</div>`;
  if (opPoints.length) html += `<div class="rv-summary-ops">${opPoints.slice(0, 4).map(p => `<div class="rv-summary-op">• ${esc(p)}</div>`).join("")}</div>`;
  el.innerHTML = html ? `<div class="rv-summary">${html}</div>` : "";
}

/* 玻璃卡可视化：指数横评 / 市场情绪 / 板块强弱（参考 Vibe-Research，2026-08-23） */
function renderVibeCards(crossRows, extra) {
  const el = document.getElementById("reviewVibe");
  if (!el) return;
  const parts = [];
  // 1) 指数横评卡（6 指数，Vibe 指数卡风格）
  if (crossRows && crossRows.length >= 2) {
    // 兼容两种格式：md解析(数组 [[h..],[r..]]) 或 meta.cross.rows(对象行 [{f:v}])
    const isArr = Array.isArray(crossRows[0]);
    const header = isArr ? crossRows[0] : Object.keys(crossRows[0]);
    const rows = isArr ? crossRows.slice(1) : crossRows;
    const iOf = n => header.indexOf(n);
    const iChg = iOf("当日%"), i5 = iOf("近5日%"), iPos = iOf("20日位置");
    const cell = (r, idx) => (Array.isArray(r) ? (idx >= 0 ? r[idx] : r[0]) : (idx >= 0 ? r[header[idx]] : r[header[0]]));
    const numV = (v, d) => { const n = parseFloat(String(v || "").replace(/[%+\s]/g, "")); return isNaN(n) ? d : n; };
    const cards = rows.filter(r => cell(r, 0)).map(r => {
      const name = cell(r, 0), price = cell(r, 1);
      const chg = numV(cell(r, iChg), 0), chg5 = numV(cell(r, i5), 0), pos = numV(cell(r, iPos), 50);
      const c = chg > 0 ? "var(--red)" : chg < 0 ? "var(--green)" : "var(--text-dim)";
      const c5 = chg5 > 0 ? "var(--red)" : chg5 < 0 ? "var(--green)" : "var(--text-dim)";
      return `<div class="rv-idx"><div class="rv-idx-name">${esc(name)}</div>
        <div class="rv-idx-price">${esc(price || "—")}</div>
        <div class="rv-idx-chg" style="color:${c}">${chg > 0 ? "+" : ""}${chg.toFixed(2)}%</div>
        <div class="rv-idx-dim">5日 <span style="color:${c5}">${chg5 > 0 ? "+" : ""}${chg5.toFixed(2)}%</span> · 20日位${pos.toFixed(0)}%</div></div>`;
    }).join("");
    parts.push(`<div class="rv-vibe-title">指数横评</div><div class="rv-idx-grid">${cards}</div>`);
  }
  // 2) 市场情绪卡
  const emo = extra.情绪 || {};
  if (Object.keys(emo).length) {
    const fmtPct = v => v == null ? "—" : (Math.round(v * 1000) / 10) + "%";
    const cells = [
      ["涨停", emo.涨停数, "var(--red)"],
      ["跌停", emo.跌停数, "var(--green)"],
      ["炸板率", emo.炸板率 != null ? fmtPct(emo.炸板率) : "—", "var(--yellow)"],
      ["情绪分S", emo.情绪分S, "var(--accent)"],
      ["题材TOP3", (emo.题材TOP3 || []).join(" / "), "var(--yellow)"],
      ["系统性风险", emo.系统性风险 ? "有" : "无", emo.系统性风险 ? "var(--red)" : "var(--green)"],
    ];
    const cards = cells.map(([k, v, color]) =>
      `<div class="rv-kpi"><div class="rv-kpi-k">${esc(k)}</div><div class="rv-kpi-v" style="${color ? "color:" + color : ""}">${esc(String(v != null ? v : "—"))}</div></div>`).join("");
    parts.push(`<div class="rv-vibe-title">市场情绪</div><div class="rv-kpi-grid">${cards}</div>`);
  }
  // 3) 板块强弱
  const sec = extra.板块 || {};
  if (sec.强势TOP5 && sec.强势TOP5.length) {
    const chip = ([n, v], hot) => `<span class="rv-chip ${hot ? "hot" : "cold"}">${esc(n)} ${v}</span>`;
    parts.push(`<div class="rv-vibe-title">板块强弱</div>
      <div><span class="rv-chip-label" style="color:var(--red)">强势</span> ${sec.强势TOP5.map(x => chip(x, true)).join(" ")}
      <span class="rv-chip-label" style="color:var(--green)">弱势</span> ${(sec.弱势BOTTOM5 || []).map(x => chip(x, false)).join(" ")}</div>`);
  }
  el.innerHTML = parts.join("");
}

async function renderDailyReview() {
  try {
    const cfg = await apiCall("get_llm_config");
    if (cfg && cfg.base_url) document.getElementById("llmBaseUrl").value = cfg.base_url || "";
    if (cfg && cfg.model) document.getElementById("llmModel").value = cfg.model || "";
    if (cfg && cfg.api_key) document.getElementById("llmApiKey").value = cfg.api_key || "";
    if (cfg && cfg.reasoning_effort) document.getElementById("llmEffort").value = cfg.reasoning_effort || "";
  } catch (e) { /* 静默 */ }
  const d = document.getElementById("reviewDate");
  if (d && !d.value) d.value = new Date().toISOString().slice(0, 10);
  refreshDailyReview();
  loadReviewHistory();
}

async function loadReviewHistory() {
  /* 历史复盘存档列表（2026-08-23） */
  try {
    const r = await apiCall("get_daily_review_list");
    const sel = document.getElementById("reviewHistory");
    if (!sel || !r || !r.dates) return;
    const current = sel.value;
    sel.innerHTML = '<option value="">历史复盘…</option>'
      + r.dates.slice().reverse().map(d => `<option value="${d}">${d}</option>`).join("");
    if (current) sel.value = current;
  } catch (e) { /* 静默 */ }
}

function loadReviewFromHistory() {
  const v = document.getElementById("reviewHistory").value;
  if (!v) return;
  document.getElementById("reviewDate").value = v;
  refreshDailyReview();
}

function toggleLlmConfig() {
  const el = document.getElementById("llmConfigCard");
  if (el) el.style.display = el.style.display === "none" ? "" : "none";
}

function copyReviewText() {
  const el = document.getElementById("reviewResult");
  if (!el || !el.innerText.trim()) { statusEl("无复盘文本可复制", "err"); return; }
  navigator.clipboard.writeText(el.innerText).then(() => statusEl("复盘文本已复制", "ok"))
    .catch(() => statusEl("复制失败，请手动选择文本", "err"));
}

/* 数据健康条（2026-08-29）：显示各数据源状态，点击展开缺失原因 */
function renderDataHealth(health) {
  const el = document.getElementById("dataHealthBar");
  if (!el) return;
  if (!health || !Object.keys(health).length) { el.innerHTML = ""; return; }
  const items = Object.entries(health).map(([k, v]) => {
    const ok = v && v.ok;
    const reason = (v && v.reason) || "";
    const icon = ok ? "✅" : "⚠️";
    const title = reason ? ` title="${esc(reason)}"` : "";
    return `<span class="dh-item ${ok ? "ok" : "warn"}"${title}>${icon} ${esc(k)}</span>`;
  }).join("");
  el.innerHTML = `<span style="color:var(--text-dim)">数据状态：</span>${items}`;
}

/* 两融余额面板（2026-08-29）：当日值 + 日变动 + 30日变动 + 近30日走势 */
function renderMarginBalance(margin) {
  const el = document.getElementById("marginBalanceBody");
  if (!el) return;
  clearPanelInstances("mb-");
  if (!margin || margin.missing) {
    el.innerHTML = `<div class="empty">两融数据缺失${margin && margin.reason ? "：" + esc(margin.reason) : ""}</div>`;
    return;
  }
  const latest = margin.latest || 0;
  const c1 = margin.change_1d || 0;
  const c30 = margin.change_30d || 0;
  const fmt = v => (v / 1e12).toFixed(2) + "万亿";
  const fmtSign = v => (v >= 0 ? "+" : "") + (v / 1e12).toFixed(3) + "万亿";
  const color1 = c1 >= 0 ? "var(--red)" : "var(--green)";
  const color30 = c30 >= 0 ? "var(--red)" : "var(--green)";
  el.innerHTML = `
    <div class="mb-kpis">
      <div class="mb-kpi"><div class="mb-k">最新余额</div><div class="mb-v">${fmt(latest)}</div></div>
      <div class="mb-kpi"><div class="mb-k">日变动</div><div class="mb-v" style="color:${color1}">${fmtSign(c1)}</div></div>
      <div class="mb-kpi"><div class="mb-k">30日变动</div><div class="mb-v" style="color:${color30}">${fmtSign(c30)}</div></div>
    </div>
    <div id="mb-chart" class="echart" style="height:180px;margin-top:8px"></div>`;
  const series = (margin.series || []).map(r => [r.date.slice(5), r.balance / 1e12]);
  if (series.length < 2) return;
  const first = series[0][1], last = series[series.length - 1][1];
  const up = last >= first;
  echRender("mb-chart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", backgroundColor: "#161b22", borderColor: "#30363d", textStyle: { color: "#c9d1d9", fontSize: 11 },
      formatter: p => `${p[0].axisValue}<br/>余额: ${p[0].data[1].toFixed(3)} 万亿` },
    grid: { left: 40, right: 16, top: 10, bottom: 24 },
    xAxis: { type: "category", data: series.map(r => r[0]), axisLabel: { color: "#8b949e", fontSize: 9 } },
    yAxis: { type: "value", scale: true, axisLabel: { color: "#8b949e", fontSize: 9, formatter: v => v.toFixed(2) + "万亿" }, splitLine: { lineStyle: { color: "rgba(139,148,158,.12)" } } },
    series: [{ type: "line", data: series.map(r => r[1]), smooth: true, symbol: "none",
      lineStyle: { width: 1.8, color: up ? "#f85149" : "#3fb950" },
      areaStyle: { color: up ? "rgba(248,81,73,.15)" : "rgba(63,185,80,.15)" } }],
  });
}

function providerChanged() {
  const p = document.getElementById("llmProvider").value;
  if (p in LLM_PROVIDER_BASE) document.getElementById("llmBaseUrl").value = LLM_PROVIDER_BASE[p];
}

async function saveModelConfig() {
  const base_url = document.getElementById("llmBaseUrl").value.trim();
  const model = document.getElementById("llmModel").value.trim();
  const api_key = document.getElementById("llmApiKey").value.trim();
  const effort = document.getElementById("llmEffort").value;
  if (!base_url || !model || !api_key) { statusEl("模型配置需填全 base_url / model / api_key", "err"); return; }
  const r = await apiCall("save_llm_config", base_url, model, api_key, effort);
  statusEl(r && r.ok ? "模型配置已保存" : "保存失败", r && r.ok ? "ok" : "err");
}

function showReviewError(msg) {
  /* 2026-08-23: 错误完整显示在结果区（可选中复制），status 只留短提示 */
  const st = document.getElementById("reviewStatus");
  if (st) st.textContent = "复盘失败（详见下方错误，可复制）";
  const el = document.getElementById("reviewResult");
  if (el) el.innerHTML = `<div style="color:#f85149;white-space:pre-wrap;word-break:break-all">❌ ${esc(msg || "未知错误")}</div>`;
}

async function runDailyReview() {
  const date = document.getElementById("reviewDate").value || new Date().toISOString().slice(0, 10);
  const status = document.getElementById("reviewStatus");
  status.textContent = "开始复盘…";
  try {
    const r = await apiCall("run_daily_review", date,
      document.getElementById("llmBaseUrl").value.trim(),
      document.getElementById("llmModel").value.trim(),
      document.getElementById("llmApiKey").value.trim(),
      document.getElementById("llmEffort").value);
    if (r && r.status === "error") { showReviewError(r.message || "未知错误"); return; }
    window._reviewStart = Date.now();   // 等待计时起点
    status.textContent = "复盘中（收集数据 + 调用模型）…";
    pollReviewProgress();
  } catch (e) {
    showReviewError("启动失败: " + e.message);
  }
}

async function pollReviewProgress() {
  if (_reviewPoll) return;
  const status = document.getElementById("reviewStatus");
  const el = document.getElementById("reviewResult");
  _reviewPoll = setInterval(async () => {
    try {
      const p = await apiCall("daily_review_progress");
      if (p.error) { showReviewError(p.error); clearInterval(_reviewPoll); _reviewPoll = null; return; }
      // 流式显示：数据收集阶段提示 + 模型输出过程（原始文本）
      if (p.output && el) {
        el.innerHTML = `<div style="white-space:pre-wrap;font-family:var(--mono);font-size:11px;color:var(--text);user-select:text">${esc(p.output)}</div>`;
        if (p.running) {
          const wait = Math.round((Date.now() - (window._reviewStart || Date.now())) / 1000);
          if (p.output.includes("🧠 模型思考中")) {
            status.textContent = `模型思考中…（推理模型，已 ${wait}s）`;
          } else {
            // 模型是否已开始输出：检测"正在调用模型"之后是否有内容
            const callIdx = p.output.lastIndexOf("正在调用模型");
            const modelStarted = callIdx >= 0 && p.output.length > callIdx + 60;
            if (!modelStarted) status.textContent = `模型请求已发送，等待响应…（已 ${wait}s）`;
            else status.textContent = "模型生成中…";
          }
        }
      }
      if (!p.running) {
        clearInterval(_reviewPoll); _reviewPoll = null;
        status.textContent = "复盘完成";
        await refreshDailyReview();
      }
    } catch (e) { /* 静默 */ }
  }, 800);
}

/* 面板 ECharts 实例清理（每次重渲染前释放旧的，避免复用已脱离 DOM 的实例） */
function clearPanelInstances(prefix) {
  Object.keys(echInstances).forEach(k => {
    if (k.startsWith(prefix)) {
      try { echInstances[k].dispose(); } catch (e) {}
      delete echInstances[k];
    }
  });
}

/* 涨停/跌停面板（2026-08-29）——当日比例 + 近一月走势 */
function renderZtDtPanel(emo, hist) {
  const el = document.getElementById("ztDtBody");
  if (!el) return;
  clearPanelInstances("ztd-");
  const zt = emo.涨停数, dt = emo.跌停数, zbl = emo.炸板率;
  const hasToday = zt != null || dt != null;
  const series = (hist && hist.series) || [];
  let todayHtml = "";
  if (hasToday) {
    const total = (zt || 0) + (dt || 0) || 1;
    const ztPct = Math.round((zt || 0) / total * 100);
    const zblTxt = zbl != null ? (Math.round(zbl * 1000) / 10) + "%" : "—";
    todayHtml = `<div class="ztd-today">
      <div class="ztd-stat"><span class="ztd-dot up"></span>涨停 <b>${zt != null ? zt : "—"}</b></div>
      <div class="ztd-stat"><span class="ztd-dot down"></span>跌停 <b>${dt != null ? dt : "—"}</b></div>
      <div class="ztd-stat"><span class="ztd-dot warn"></span>炸板率 <b>${zblTxt}</b></div>
      <div class="ztd-barwrap">
        <div class="ztd-bar"><div class="ztd-bar-up" style="width:${ztPct}%"></div><div class="ztd-bar-down" style="width:${100 - ztPct}%"></div></div>
        <div class="ztd-bar-label"><span>涨停 ${ztPct}%</span><span>跌停 ${100 - ztPct}%</span></div>
      </div>
    </div>`;
  }
  if (series.length < 2) {
    el.innerHTML = todayHtml + '<div class="empty">暂无近一月涨跌停历史数据</div>';
    return;
  }
  el.innerHTML = todayHtml + `<div class="ztd-sub">近一月涨停 / 跌停走势</div><div id="ztd-chart" class="echart" style="height:220px"></div>`;
  const x = series.map(r => String(r.date).slice(5));
  const zts = series.map(r => r.zt || 0);
  const dts = series.map(r => -(r.dt || 0));
  echRender("ztd-chart", {
    backgroundColor: "transparent",
    tooltip: { trigger: "axis", backgroundColor: "#161b22", borderColor: "#30363d", textStyle: { color: "#c9d1d9", fontSize: 11 } },
    legend: { top: 0, textStyle: { color: "#8b949e" }, data: ["涨停", "跌停"] },
    grid: { left: 40, right: 16, top: 26, bottom: 24 },
    xAxis: { type: "category", data: x, axisLabel: { color: "#8b949e", fontSize: 10 } },
    yAxis: { type: "value", axisLabel: { color: "#8b949e", fontSize: 10 }, splitLine: { lineStyle: { color: "rgba(139,148,158,.12)" } } },
    series: [
      { name: "涨停", type: "bar", barWidth: 7, data: zts, itemStyle: { color: "#f85149" } },
      { name: "跌停", type: "bar", barWidth: 7, data: dts, itemStyle: { color: "#3fb950" } },
    ],
  });
}

async function refreshDailyReview() {
  const date = document.getElementById("reviewDate").value || new Date().toISOString().slice(0, 10);
  const el = document.getElementById("reviewResult");
  if (!el) return;
  try {
    const r = await apiCall("get_daily_review", date);
    const hist = await apiCall("get_zt_dt_history", 30).catch(() => null);
    const margin = await apiCall("get_margin_balance", 30).catch(() => null);
    const emo = (r && r.exists && r.data && r.data.extra && r.data.extra.情绪) || {};
    const health = (r && r.exists && r.data && r.data.data_health) || {};
    renderDataHealth(health);                      // 数据健康条
    renderZtDtPanel(emo, hist);                    // 涨停/跌停面板
    renderMarginBalance(margin);                   // 两融余额面板（独立刷新，不依赖 LLM）
    if (r && r.exists) {
      renderSummary(r.text);                     // 复盘总结卡（置顶）
      renderReviewCharts(r.text, r.data || {});   // 可视化（玻璃卡 + 条形图 + 关键位）
      el.innerHTML = renderReviewAsCards(r.text);  // LLM 深度复盘 → 分卡片渲染
    } else {
      const wrap = document.getElementById("reviewChartWrap");
      if (wrap) wrap.style.display = "none";
      el.innerHTML = '<div class="empty">该日期暂无复盘结果，点「开始大盘复盘」生成</div>';
    }
  } catch (e) { el.innerHTML = '<div class="empty">加载失败</div>'; }
  loadReviewHistory();
}

/* ================= Tab 切换 ================= */
function switchTab(tab) {
  document.querySelectorAll(".tab-page").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".sidebar-item").forEach(s => s.classList.remove("active"));
  const page = document.getElementById("tab-" + tab);
  const sideItem = document.querySelector(`.sidebar-item[data-tab="${tab}"]`);
  if (page) page.classList.add("active");
  if (sideItem) sideItem.classList.add("active");
  // 延迟 resize 让 tab 的 display:block 先生效。
  // ⚠️ 两张"按需绘制"的图也必须在**这个延迟里**重画，不能同步调用 —— 本函数前脚才加上
  //    `.active`，同一 tick 里量 clientWidth 仍是 0（`display:none` 时宽高都是 0），
  //    同步调用必然拿 0 尺寸、画不出来（2026-10-15 实测：盘中状态成交额图停在"容器尺寸为 0"）。
  setTimeout(() => {
    resizeAllEch();
    if (tab === "hunter") { drawHunterGoChart(); renderHunterGoList(); loadSectorStars(); loadSectorMaBreadth(); }
    if (tab === "signals") {
      _turnoverSizeRetryN = 0; drawTurnoverHist();
      if (ma365Inst) { try { ma365Inst.resize(); } catch (e) { } }
      if (ma20Inst) { try { ma20Inst.resize(); } catch (e) { } }
      // 首次进 tab 才拉（默认在总览页，别让 5500 个日线缓存的解析和首屏抢 CPU）；
      // 已有数据则就地重画（节点若被重建过，renderMa365 / renderMa20Breadth 会自行重建实例）。
      if (!ma365Data || ma365DataRange !== ma365Range) loadMa365Breadth();
      else { renderMa365(ma365Data); renderMa20Breadth(ma365Data); }
    }
  }, 80);
  // 2026-10-10: 切到选股猎手不再自动跑扫描（owner 需求6）——一律点「▶ 运行今日数据」触发。
  // 切到每日复盘：懒加载模型配置 + 已有复盘
  if (tab === "archive") renderDailyReview();
  // 图表分析：交割单台账懒加载（未导入过则只显示「点导入」提示，不弹窗）
  if (tab === "charts") loadTradeHistory();
  // P4-3 切到自动盘：加载 t_io/bridge 状态 + 每日选股记录
  if (tab === "auto") {
    loadAutoStatus();
    loadOscDay(true);                                           // 每日选股记录（日期跟随全局选中日，强制刷新）
    if (state.autoLedger) renderAutoLedger(state.autoLedger);   // 已有数据先渲染，避免空白
  }
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
    if (a && a.paper) return;   // 2026-10-10: 手动盘=非 paper 账户（自动盘另列）
    totalCap += (a.total_capital || 0);
  });
  const pnl = tv - tc;
  document.getElementById("sumValue").textContent = tv ? fmt(tv, 0) : "—";
  document.getElementById("sumCapital").textContent = totalCap ? fmt(totalCap, 0) : "未设置";
  document.getElementById("sumPos").textContent = totalCap ? Math.round(tv / totalCap * 100) + "%" : "—";
  document.getElementById("sumUnreal").innerHTML = `<span class="${clsOf(pnl)}">${pnl >= 0 ? "+" : ""}${fmt(pnl, 0)}</span>`;
  document.getElementById("sumRealLoss").innerHTML = trl ? `<span class="${trl > 0 ? 'down' : 'neutral'}">${fmt(trl, 0)}</span>` : `<span class="neutral">0</span>`;
}

// 侧栏「自动盘」三行（数据来自 load_auto_pnl 的 summary）
function updateSidebarAuto(summary) {
  const s = summary || {};
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    if (val == null) { el.textContent = "—"; return; }
    el.innerHTML = cls ? `<span class="${cls}">${(val >= 0 ? "+" : "") + fmt(val, 0)}</span>` : fmt(val, 0);
  };
  set("sumAutoTotal", s.total_assets);
  set("sumAutoMv", s.market_value);
  set("sumAutoUnreal", s.unreal_pnl, clsOf(s.unreal_pnl));
  set("sumAutoReal", s.realized_pnl, clsOf(s.realized_pnl));
}

/* ================= 初始化 ================= */
let dateSelect, refreshBtn, autoPoll;
let pollTimer = null;      // 60s 盘后轮询
// 盘中轮询定时器：统一由 _startStaggered/_stopStaggered 管理（错开相位，见 startLivePoll）
const _liveTimers = {};    // name -> { kick: timeoutId, interval: intervalId }
// ⚠️ 2026-10-10 **改为「完成后才排下一次」**，这是本次卡顿修复的核心。
// 原来用 `setInterval(fn, periodMs)`，而 fn 是 async（refreshLive…）——
// setInterval **不 await**，所以只要单次耗时 > 间隔就会**并发叠加**。实测投递层单次往返
// 16-20s 时：load_console(2s 间隔) 叠 8 个、signals(5s) 叠 4 个、live/auto/buyConfirm(10s) 各叠 2 个
// ⇒ 常驻 18+ 个调用同时在飞，全挤进 pywebview 的 UI 线程队列 ⇒ 每次更慢 ⇒ 叠得更多。
// 这是**正反馈**，也是日志里"每次调用都慢、且延迟稳定停在 20s 平台"的成因。
// 改成完成后 setTimeout 排下一次：每个轮询器至多 1 个在飞，队列有界，反馈环断开。
// 代价：慢时轮询频率自然降下来（这正是我们想要的背压），不再是固定的"到点就打"。
function _startStaggered(name, fn, periodMs, offsetMs) {
  _stopStaggered(name);
  const slot = { kick: null, interval: null, stopped: false };
  const tick = async () => {
    if (slot.stopped) return;
    try {
      // 上限保护：单次若卡死（后端挂住不返回），也必须能重新排期——否则该轮询永久停摆。
      // 取 30s：远大于正常往返，只在真挂住时兜底。
      await Promise.race([Promise.resolve(fn()), new Promise(r => setTimeout(r, 30000))]);
    } catch (e) { /* 静默：轮询失败不该打断循环 */ }
    if (slot.stopped) return;
    slot.interval = setTimeout(tick, periodMs);   // ← 上一次跑完才排下一次
  };
  // 先延迟 offset 再起跑：只挪相位，不改「首帧等一个周期」的原有语义
  slot.kick = setTimeout(() => {
    slot.kick = null;
    if (slot.stopped) return;
    slot.interval = setTimeout(tick, 0);
  }, offsetMs);
  _liveTimers[name] = slot;
}
function _stopStaggered(name) {
  const s = _liveTimers[name];
  if (!s) return;
  s.stopped = true;                       // 让已在飞的 tick 返回后不再自我续期
  if (s.kick) clearTimeout(s.kick);
  if (s.interval) clearTimeout(s.interval);   // 现在存的是 timeoutId（原为 intervalId）
  delete _liveTimers[name];
}

function stopPoll() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }   // 存的是 timeoutId
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
  // 同样是「完成后才排下一次」（2026-10-10，见 _startStaggered 的说明）：
  // loadAndRender 会扇出十几个接口，若 60s 到点就打、不等上次跑完，慢时会层层叠加。
  const tick = async () => {
    if (state.date) {
      try {
        // 上限保护同 _startStaggered：卡死也要能重新排期
        await Promise.race([(async () => {
          await loadAndRender(state.date, true);
          await maybeAutoSwitchToToday();
        })(), new Promise(r => setTimeout(r, 60000))]);
      } catch (e) { /* 静默 */ }
    }
    if (pollTimer !== null) pollTimer = setTimeout(tick, 60000);
  };
  pollTimer = setTimeout(tick, 60000);
}
function stopLivePoll() {
  ["live", "console", "auto", "buyConfirm"].forEach(_stopStaggered);
  document.getElementById("liveTag").style.display = "none";
}
function startLivePoll() {
  stopLivePoll();
  document.getElementById("liveTag").style.display = "";
  document.getElementById("liveTag").textContent = "LIVE 10s";
  // 这几个定时器原来在同一条语句流里 setInterval，**相位完全相同** ⇒ 每 10s 所有请求在同一
  // 瞬间一起打后端（突发尖峰，pywebview 线程上是串行的）。错开相位把尖峰摊平，周期不变。
  // ⚠️ 回调**必须 return promise**，否则 _startStaggered 的「完成后才排下一次」拿不到完成信号，
  // 背压失效（2026-10-10 改非重叠轮询时踩到的坑）。
  _startStaggered("live", () => {
    if (chartModalOpen) return;        // K 线弹窗打开：暂停背后的大盘/持仓刷新
    if (state.date) return refreshLive(false);
  }, 10000, 0);
  // P4-3: 自动盘 10s 轮询（自动盘 tab 或概览页（含自动盘持仓卡片）激活时拉取 bridge）
  _startStaggered("auto", () => {
    if (chartModalOpen) return;        // K 线弹窗打开：暂停 auto_pnl/auto_status（3~5s 重活）
    const act = document.querySelector(".sidebar-item.active");
    const t = act && act.dataset.tab;
    if (t !== "auto" && t !== "overview") return;
    // 2026-10-10: auto tab 也要流水/每日盈亏
    return Promise.all([loadAutoStatus(), loadAutoPnl()]);
  }, 10000, 2200);
  // 人工确认闸 10s 全局轮询（与 tab 无关：买入确认时效敏感，任何 tab 都弹窗打扰）
  _startStaggered("buyConfirm", pollBuyConfirm, 10000, 3600);
  pollBuyConfirm();
}

async function init() {
  // 首先获取 DOM 元素（此时 DOM 已完全加载）
  // 2026-10-20：启动标记（0ms）写进慢调用日志 ⇒ 能确认 WebView2 是否加载了新版 app.js
  try { window.pywebview.api.report_slow_call("app_boot_20261020m", 0); } catch (e) { /* 静默 */ }
  dateSelect = document.getElementById("dateSelect");
  refreshBtn = document.getElementById("refreshBtn");
  autoPoll = document.getElementById("autoPoll");

  if (!dateSelect || !refreshBtn || !autoPoll) {
    statusEl("⚠️ HTML 结构不完整（缺少关键元素），请检查 index.html", "err");
    return;
  }

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
      const _pd = document.getElementById("pbScanDate");   // 建仓面板日期跟随全局选中日
      if (_pd) _pd.value = dateSelect.value;
      const _od = document.getElementById("oscDayDate");   // 每日选股记录面板同款跟随
      if (_od) _od.value = dateSelect.value;
      // 板块星轨图是「截至所选日期」的，切日期时若正在猎手页则重画（隐藏时画会拿到 0 尺寸）
      const _act = document.querySelector(".sidebar-item.active");
      if (_act && _act.dataset.tab === "hunter") { loadSectorStars(); loadSectorMaBreadth(); }
      loadAndRender(dateSelect.value, false);
    }
  });
  refreshBtn.addEventListener("click", () => {
    if (dateSelect.value) loadAndRender(dateSelect.value, false);
  });
  // 自动盘买入人工确认闸：确认/拒绝按钮事件委托 + 初始拉取（全局轮询在 startLivePoll）
  initBuyConfirmEvents();
  // 选股猎手运行按钮 + 历史日期下拉
  const hunterBtn = document.getElementById("hunterRunBtn");
  if (hunterBtn) hunterBtn.addEventListener("click", () => loadHunter(true));
  const hunterRegen = document.getElementById("hunterRegenBtn");
  if (hunterRegen) hunterRegen.addEventListener("click", regenerateHunter);
  initHunterDates();
  // 2026-09-01: 选股猎手 tab 主要指数已删除，移除 loadIndices 初始化与 60s 轮询（loadIndices 函数保留供兼容）
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
  statusEl("初始化中...");
  try {
    const timeoutPromise = new Promise((_, reject) =>
      setTimeout(() => reject(new Error("available_dates 请求超时（后端未响应）")), 10000)
    );
    dates = await Promise.race([apiCall("available_dates"), timeoutPromise]);
  } catch (e) {
    statusEl("无法连接后端：" + e.message + "（请确保用 python t_gui.py 启动）", "err");
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
  _pbSyncScanDate();   // 建仓面板日期选择器默认=当前选中日
  _oscSyncDayDate();   // 每日选股记录面板日期默认=当前选中日
  startPoll();
  // 📈 年限宽度懒启动：默认页面是「总览」，面板在「手动盘」里 ⇒ 切到该 tab 再拉
  //（首次要全量解析 5500 个日线缓存 ≈10~20s，别让它和首屏抢 CPU）。
  // 启动时今天可能尚无数据导致停在历史日 → 立即检查一次，今天就绪则自动切回
  maybeAutoSwitchToToday();
}

document.addEventListener("DOMContentLoaded", () => {
  // 顶部横幅（告警/指数背离）是 fixed 浮层，会压住首屏内容（2026-10-09 owner：提示栏挡视野）。
  // 依其实测高度设 `--top-banner-h`（含与 topbar 的 6px 间隙），由 `.content` 顶部留白消费。
  const _tb = document.getElementById("topBanners");
  if (_tb) {
    const _reserve = () => {
      const h = _tb.offsetHeight;
      document.documentElement.style.setProperty("--top-banner-h", h > 0 ? (h + 6) + "px" : "0px");
    };
    if (window.ResizeObserver) new ResizeObserver(_reserve).observe(_tb);
    _reserve();
  }
  // ===== 投递层诊断（2026-10-10）=====
  // 结论已拿到（见 t_io/logs/gui_thread_dumps.log 与 tmp/parse_thread_dump.py）：
  // 一次卡死现场 68 线程里 **25 个堵在 pywebview 的返回路径**（21 个在
  // `edgechromium.py:160 semaphore.acquire()`、4 个在 `:152 self.webview.Invoke`），
  // 另有 12 个在 pandas、4 个在网络。pinpoint 已足够，故**大幅降载**，避免埋点自己
  // 给被测的队列添堵（每个 js_api 调用都会新起一个线程 + 占用一次 UI 线程返回通道）：
  //  · jsblock：保留（纯 JS 侧，零桥接调用，不添载）。
  //  · ping   ：2s → **10s**，且改走 `apiCall` ⇒ **受在飞上限约束**，不再绕过限流。
  //  · dump_threads：**取消自动触发**（一次写 100KB+ 栈、且它本身是个 js_api 调用）。
  //    需要时手工调 `dump_threads()`（或临时把下面的开关打开）。
  const _AUTO_DUMP = false;   // 需要再抓栈现场时改 true
  // ⚠️ 必须节流：jsblock 在卡顿时可能**每 250ms 就触发一次**，而每次上报都是一次 js_api 调用
  // （新起线程 + 占用 UI 线程返回通道）⇒ 埋点自己会变成主要负载来源。2s 内至多一次。
  let _lastDiagAt = 0;
  _hqDiag = _diagReport;   // 给顶层（猎手实时行情那条链）用，见 hunterQuoteCache 处注释
  function _diagReport(kind, value, note) {
    const now = Date.now();
    if (now - _lastDiagAt < 2000) return;
    _lastDiagAt = now;
    try {
      if (window.pywebview && window.pywebview.api
        && typeof window.pywebview.api.report_client_diag === "function") {
        // fire-and-forget，且**不占** apiCall 的在飞名额（诊断不该挤掉业务调用）
        window.pywebview.api.report_client_diag(kind, Math.round(value), note || "");
      }
    } catch (e) { /* 静默 */ }
  }
  let _lastTick = performance.now();
  setInterval(() => {
    const now = performance.now();
    const gap = now - _lastTick;
    _lastTick = now;
    // 250ms 定时器被拖到 >1000ms ⇒ 主线程至少堵了 (gap-250)ms
    if (gap > 1000) _diagReport("jsblock", gap - 250, "JS main thread blocked");
    // >60s 的间隔只可能是休眠/挂起（普通渲染不会卡这么久）⇒ 恢复后重新错峰，避免惊群
    if (gap > 60000) _onWakeFromSleep(gap);
  }, 250);
  let _dumpedAt = 0;
  setInterval(async () => {
    const t0 = performance.now();
    try {
      await apiCall("ping");     // 走 apiCall ⇒ 受 _API_MAX_INFLIGHT / 超时约束
    } catch (e) { return; }
    const dt = performance.now() - t0;
    if (dt > 300) _diagReport("ping", dt, "empty-endpoint round trip");
    if (_AUTO_DUMP && dt > 3000 && Date.now() - _dumpedAt > 60000) {
      _dumpedAt = Date.now();
      try { window.pywebview.api.dump_threads(`ping=${Math.round(dt)}ms`); } catch (e) { }
    }
  }, 10000);

  // pywebview 桥接脚本可能在 DOMContentLoaded 之后才注入，等待 pywebviewready / 轮询兜底
  statusEl("尝试连接后端...");
  if (_apiReady()) {
    init();
    return;
  }
  let booted = false;
  const doBoot = () => {
    if (booted) return;
    if (_apiReady()) { booted = true; init(); }
  };
  window.addEventListener("pywebviewready", doBoot);
  const t = setInterval(() => { doBoot(); if (booted) clearInterval(t); }, 250);
  const timeout = setTimeout(() => {
    if (!booted) {
      clearInterval(t);
      statusEl("⚠️ 后端连接失败（可能 t_gui.py 未启动或已崩溃，请检查进程）", "err");
    }
  }, 8000);
  setTimeout(() => {
    if (!booted) {
      clearInterval(t);
      clearTimeout(timeout);
      doBoot();
    }
  }, 8000);
});

/* ══════════════════════════════════════════════════════════════════════
   多窗口看盘（2026-10-21 owner）
   「持仓 30 分钟风险体检」上的按钮 → 3×2 六宫格，每格自选一只票的 **5 分 K**，
   开着就每 4 秒刷新一次（收盘后后端不再出网，自然降频）。

   数据走 `load_multi_chart(codes, "min5", 90)`：一次请求拿 6 只票的实时尾，
   每只**只回 90 根**（单图是 3200 根深档）⇒ 6 格一轮约 60KB，而不是 6×100KB。
   后端复用单图那套 SWR（缓存新鲜秒回、过期后台刷、本次先回旧帧）⇒ 任何一格卡住
   都不会拖住别的格，也不会把主线程冻住。
   ══════════════════════════════════════════════════════════════════════ */
const MW_N = 6;                 // 3×2
const MW_LS_KEY = "multiWatchCodes";
const MW_PERIOD_MS = 4000;      // 刷新间隔（6 只票 × 每次约 1s 计算，4s 是 CPU 与实时性的折中）
const MW_TAIL = 48;             // 每格只画最后 48 根 = 5 分 K 的一个交易日（owner：别显示太多 K 线）
let mwCodes = [], mwInsts = [], mwTimer = null, mwBusy = false;

/* 选股候选：**优先体检持仓**（带名称），体检还没回来时退回 `state.quotes`
   （那是 10s 轮询顺手拉的持仓+股池行情，通常比体检先到）。
   2026-10-21 owner 报「点了多窗口看盘无法选择股票」—— 根因就是只认 `_obLast`，
   而体检首帧常常是 pending（`ob.stocks` 为空）⇒ 下拉里只剩一个占位项。 */
function _mwHoldings() {
  const out = [];
  const seen = new Set();
  const push = (code, name) => {
    const c = String(code || "").split("_")[0].trim();
    const m = c.match(/^(\d{6})$/);
    if (!m || seen.has(m[1])) return;
    seen.add(m[1]);
    out.push({ code: m[1], name: String(name || "") });
  };
  const st = (_obLast && _obLast.stocks) || [];
  st.forEach(s => push(s && s.code, (s && s.name) || ""));
  (state.quotes || []).forEach(q => push(q && q.code, (q && (q.name || q.名称)) || ""));
  return out;
}

function _mwLoadCodes() {
  let saved = [];
  try { saved = JSON.parse(localStorage.getItem(MW_LS_KEY) || "[]"); } catch (e) { saved = []; }
  if (!Array.isArray(saved) || saved.length !== MW_N) {
    saved = _mwHoldings().slice(0, MW_N).map(h => h.code);
  }
  while (saved.length < MW_N) saved.push("");
  return saved.slice(0, MW_N);
}

function _mwSaveCodes() {
  try { localStorage.setItem(MW_LS_KEY, JSON.stringify(mwCodes)); } catch (e) { /* 忽略 */ }
}

function openMultiWatch() {
  const win = document.getElementById("multiWatchWin");
  if (!win) return;
  win.style.display = "flex";
  mwCodes = _mwLoadCodes();
  _mwBuildCells();
  _mwBindDrag(win);
  mwTick();
  if (mwTimer) clearInterval(mwTimer);
  mwTimer = setInterval(mwTick, MW_PERIOD_MS);
}

function closeMultiWatch() {
  const win = document.getElementById("multiWatchWin");
  if (win) win.style.display = "none";
  if (mwTimer) { clearInterval(mwTimer); mwTimer = null; }
  if (mwRO) { try { mwRO.disconnect(); } catch (e) { /* 忽略 */ } mwRO = null; }
  mwInsts.forEach(i => { try { i.dispose(); } catch (e) { /* 忽略 */ } });
  mwInsts = [];
}

/* 浮窗拖动（2026-10-21）：按标题栏拖。用 px 定位（拖过之后再也不用 right/bottom），
   否则 right/top 混着改会跳。 */
function _mwBindDrag(win) {
  const bar = document.getElementById("mwTitle");
  if (!bar || bar._mwDragBound) return;
  bar._mwDragBound = true;
  bar.addEventListener("mousedown", (ev) => {
    if (ev.target && ev.target.tagName === "BUTTON") return;   // 别抢刷新/关闭按钮
    const r = win.getBoundingClientRect();
    const dx = ev.clientX - r.left, dy = ev.clientY - r.top;
    win.style.right = "auto"; win.style.left = r.left + "px"; win.style.top = r.top + "px";
    const move = (e) => {
      const w = win.offsetWidth, h = win.offsetHeight;
      // 夹在窗口内，别拖丢了
      const x = Math.max(0, Math.min(window.innerWidth - w, e.clientX - dx));
      const y = Math.max(0, Math.min(window.innerHeight - 40, e.clientY - dy));
      win.style.left = x + "px"; win.style.top = y + "px";
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    ev.preventDefault();
  });
}

/* 浮窗右下角可缩放（CSS resize:both）⇒ 里面的 6 个小图要跟着重排 */
let mwRO = null;
function _mwBindResize() {
  const grid = document.getElementById("multiWatchGrid");
  if (!grid || typeof ResizeObserver !== "function") return;
  if (mwRO) { try { mwRO.disconnect(); } catch (e) { /* 忽略 */ } }
  let _raf = 0;
  mwRO = new ResizeObserver(() => {
    if (_raf) return;
    _raf = requestAnimationFrame(() => {
      _raf = 0;
      mwInsts.forEach(i => { try { i && i.resize(); } catch (e) { /* 忽略 */ } });
    });
  });
  mwRO.observe(grid);
}

function mwRefreshAll() { mwTick(); }

function _mwBuildCells() {
  const host = document.getElementById("multiWatchGrid");
  if (!host) return;
  mwInsts.forEach(i => { try { i.dispose(); } catch (e) { /* 忽略 */ } });
  mwInsts = [];
  const hs = _mwHoldings();
  // 候选写进共享 datalist：输入框有提示、也能直接敲别的代码
  const dl = document.getElementById("mwNames");
  if (dl) {
    dl.innerHTML = hs.map(h => `<option value="${esc(h.code)}">${esc(h.name || h.code)}</option>`).join("");
  }
  host.innerHTML = mwCodes.map((c, i) => {
    // 下拉**只显示名称**（owner：不要显示代码）；已保存但不持仓的票补一个占位项
    const opts = ['<option value="">— 选股 —</option>']
      .concat(hs.map(h => `<option value="${esc(h.code)}"${h.code === c ? " selected" : ""}>`
        + `${esc(h.name || h.code)}</option>`));
    if (c && !hs.some(h => h.code === c)) {
      opts.push(`<option value="${esc(c)}" selected>${esc(c)}</option>`);
    }
    return `<div class="mw-cell">
      <div class="mw-head">
        <select class="mw-sel" title="从「持仓 30 分钟风险体检」的股票里选"
                onchange="mwPick(${i}, this.value)">${opts.join("")}</select>
        <span class="mw-px" id="mwPx${i}"></span>
      </div>
      <div class="mw-chart" id="mwChart${i}"></div>
    </div>`;
  }).join("");
  for (let i = 0; i < MW_N; i++) {
    const el = document.getElementById("mwChart" + i);
    if (el && window.echarts) {
      try {
        mwInsts[i] = echarts.init(el);
        mwInsts[i].setOption({ backgroundColor: "#0d1117", animation: false });
      } catch (e) { mwInsts[i] = null; }
    }
  }
  _mwBindResize();   // 浮窗右下角一缩放，6 个小图跟着重排
}

function mwPick(i, raw) {
  // 宽容解析：用户可能敲 "600000"、"sh600000"、"600000 浦发银行"
  const m = String(raw || "").match(/(\d{6})/);
  const code = m ? m[1] : "";
  mwCodes[i] = code;
  _mwSaveCodes();
  // 下拉回填成裸码（与格子里的图对齐）；下拉里没有该码时它自然显示空，不影响出图
  const sel = document.querySelectorAll(".mw-sel")[i];
  if (sel) sel.value = code;
  // 换票先清掉旧图，别让上一只票的线留在格子里
  const inst = mwInsts[i];
  if (inst) {
    try { inst.clear(); inst.setOption({ backgroundColor: "#0d1117", animation: false }); }
    catch (e) { /* 忽略 */ }
  }
  const px = document.getElementById("mwPx" + i);
  if (px) px.innerHTML = "";
  if (code) mwTick();
}

/* 六宫格里的迷你 K 线：K 线 + **日线 MA5/10/20**（虚线，与单图同款）+ 缺口色块 + 成交额。
   刻意不画 MACD/RSI/箱体 —— 每格只有 ~1/6 屏宽，塞满就成了噪声；要看细节点开大图。
   2026-10-21 owner：① 缺口没显示 ② 5日/10日/20日均线没显示 ⇒ 都补上，
   且缺口与单图**共用** `buildGapAreas`，口径不会漂。 */
function _mwOption(s, dma) {
  const gaps = buildGapAreas(s.ohlc, 250, 0.003, 8);   // 一格只画最近 8 块，别糊满
  const _dmaLine = (y, color, txt) => (y == null || !isFinite(y)) ? null : {
    yAxis: y,
    lineStyle: { color, type: "dashed", width: 1, opacity: .9 },
    label: { formatter: `${txt} ${Number(y).toFixed(2)}`, color, fontSize: 8,
             position: "insideStartTop" },
  };
  const _dmaData = [
    _dmaLine(dma && dma.ma5, "#58a6ff", "日MA5"),
    _dmaLine(dma && dma.ma10, "#bc8cff", "日MA10"),
    _dmaLine(dma && dma.ma20, "#2dd4bf", "日MA20"),
  ].filter(Boolean);
  const dates = s.dates || [], ohlc = s.ohlc || [], ma = s.ma || [];
  const amtYi = (s.amount && s.amount.length === dates.length)
    ? s.amount.map(v => (v == null || isNaN(Number(v))) ? null : +(Number(v) / 1e8).toFixed(2))
    : null;
  const vals = amtYi || s.volume || [];
  const volSer = vals.map((v, i) => {
    const o = ohlc[i];
    const prev = i > 0 ? ohlc[i - 1][1] : null;
    const up = !o ? true : (prev == null ? o[1] >= o[0] : o[1] >= prev);
    return { value: v, itemStyle: { color: up ? "rgba(248,81,73,.8)" : "rgba(63,185,80,.8)" } };
  });
  const line = (arr, color, w) => ({
    type: "line", data: arr || [], symbol: "none", connectNulls: true,
    lineStyle: { width: w, color }, itemStyle: { color },
  });
  return {
    animation: false, backgroundColor: "#0d1117",
    grid: [{ left: 46, right: 8, top: 8, height: "62%" },
           { left: 46, right: 8, top: "74%", height: "18%" }],
    xAxis: [
      { type: "category", data: dates, axisLine: { lineStyle: { color: "#2b333d" } },
        axisLabel: { show: false } },
      { type: "category", data: dates, gridIndex: 1, axisLine: { lineStyle: { color: "#2b333d" } },
        axisLabel: { color: "#8b949e", fontSize: 8,
                     interval: Math.max(1, Math.floor(dates.length / 4)) } },
    ],
    yAxis: [
      { scale: true, splitNumber: 3, axisLabel: { color: "#8b949e", fontSize: 8 },
        splitLine: { lineStyle: { color: "rgba(139,148,158,.12)" } } },
      { scale: true, gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
    ],
    series: [
      { name: "K线", type: "candlestick", data: ohlc, barMaxWidth: 10,
        // 缺口色块 + 日线均线（与单图同一套口径/颜色）
        markArea: gaps.length ? { silent: true, data: gaps } : undefined,
        markLine: _dmaData.length ? { silent: true, symbol: "none", data: _dmaData } : undefined,
        itemStyle: { color: "#f85149", color0: "#3fb950",
                     borderColor: "#ff6b62", borderColor0: "#56d364", borderWidth: 1 } },
      line(ma[0], "#e3b341", 1.2),   // 本周期 MA5（5 根 5 分 K = 25 分钟）
      { name: "量能", type: "bar", xAxisIndex: 1, yAxisIndex: 1, data: volSer, barMaxWidth: 10 },
    ],
  };
}

async function mwTick() {
  if (mwBusy) return;                       // 单飞：上一轮没回来就别叠
  // 窗口最小化/切后台时跳过：6 只票每轮 ~6s 的计算量，没必要喂给看不见的窗口
  if (document.visibilityState === "hidden") return;
  const codes = mwCodes.filter(Boolean);
  if (!codes.length) return;
  mwBusy = true;
  try {
    const r = await apiCall("load_multi_chart", codes, "min5", MW_TAIL);
    const items = (r && r.items) || [];
    items.forEach(it => {
      const i = mwCodes.indexOf(it.code);
      if (i < 0) return;
      const px = document.getElementById("mwPx" + i);
      const q = it.quote || {};
      if (px && q.price != null) {
        const p = q.change_pct;
        const cls = (p == null || p === 0) ? "cell-dim" : (p > 0 ? "up" : "down");
        px.innerHTML = `<span class="${cls}">${fmt(q.price)}`
          + (p == null ? "" : ` ${p >= 0 ? "+" : ""}${Number(p).toFixed(2)}%`) + `</span>`;
      }
      if (!it.ok) return;
      const s = it.series || {};
      if (!s.dates || !s.dates.length) return;
      let inst = mwInsts[i];
      if (!inst) {
        const el = document.getElementById("mwChart" + i);
        if (!el || !window.echarts) return;
        inst = mwInsts[i] = echarts.init(el);
      }
      try { inst.setOption(_mwOption(s, it.dma), { notMerge: true }); } catch (e) { /* 单格失败不影响别的格 */ }
    });
  } catch (e) { /* 静默：下一跳重试 */ }
  finally { mwBusy = false; }
}
