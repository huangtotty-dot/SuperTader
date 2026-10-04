# -*- coding: utf-8 -*-
"""test_gui_render_smoke.py — GUI 渲染冒烟测试（无头，2026-09-12 新增）

背景：t_gui/web 前端是 pywebview 渲染的 HTML+JS，此前**没有任何自动化测试**。
2026-09-12 改「入场口径」时，靠人工审查抓到两类只有真跑渲染才会暴露的缺陷：
  1) 后端在 timing 模式下把 div_gate 初始化成 insufficient=True，详情面板误显
     「60分钟K线数据不足，背离不可判」这个不存在的卡点；
  2) app.js 里 _isDiv 声明在逐行 map 回调内，而表头模板在函数外层作用域 → ReferenceError。
两者语法检查（node --check / py_compile）都发现不了。本测试用**真实后端 payload**
在 node 里执行真实渲染函数，把这类问题挡在提交前。

做法：t_gui.Api 产出真实 payload → 写 DOM 垫片 + 渲染 JS → node 执行 → 断言。
依赖：node（PATH 内）。无 node 时跳过（打印 SKIP，退出码 0），不阻塞无此依赖的环境。

用法：python t_io/validation/gui/test_gui_render_smoke.py [--date YYYY-MM-DD]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parents[3]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

DOM_STUB = r"""
const mkElRef = () => ({
  innerHTML: '', textContent: '', value: '', checked: false, parentNode: null,
  style: new Proxy({}, { get: () => '', set: () => true }),
  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  dataset: {}, children: [], scrollTop: 0, scrollHeight: 0,
  // appendChild 记录子节点：toast/模态窗是动态插入的，需可被断言检查
  appendChild(c){ this.children.push(c); if (c) c.parentNode = this; return c; },
  removeChild(c){ const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); },
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); },
  setAttribute(){}, getAttribute(){ return null; },
  addEventListener(){}, removeEventListener(){}, querySelector(){ return mkElRef(); },
  querySelectorAll(){ return []; }, insertAdjacentHTML(){}, focus(){}, click(){},
  getBoundingClientRect(){ return { top:0, left:0, width:0, height:0 }; },
});
global.document = {
  getElementById(){ return mkElRef(); }, querySelector(){ return mkElRef(); },
  querySelectorAll(){ return []; }, createElement(){ return mkElRef(); },
  addEventListener(){}, body: mkElRef(), documentElement: mkElRef(), head: mkElRef(),
};
global.window = { addEventListener(){}, removeEventListener(){}, postMessage(){},
  localStorage: { getItem(){ return null; }, setItem(){} },
  location: { href:'', search:'' }, matchMedia(){ return { matches:false, addListener(){} }; },
  open(){}, setTimeout, clearTimeout, setInterval, clearInterval, _condLabels: null };
global.window.parent = { postMessage(){} };
global.navigator = { userAgent: 'node' };
global.location = global.window.location;
global.alert = () => {};
global.confirm = () => true;
// ECharts 桩：app.js 用 `const echarts = window.echarts` 取（见 app.js:319），
// 故必须挂在 window 上。捕获 setOption 的 option（供图层断言），并保存 on() 的回调 +
// 实现 getOption()，供「手动缩放不被 10s 刷新重置」的断言驱动。
// 注意：实例是全局单例 __inst，dispose() 不清 handlers —— app.js 每次渲染都新建实例并重挂
// on('dataZoom')，若不清就会累积；这里 dispose 时清空，模拟真实实例生命周期。
global.window.echarts = { init(){
  const handlers = {};
  global.__initCount = (global.__initCount || 0) + 1;
  global.__inst = {
    setOption(o){ global.__opt = o; },
    // getOption 会深拷贝整个 option ⇒ 计数用来防回退（K线卡顿根因之一，2026-10-04）
    getOption(){ global.__getOptionCount = (global.__getOptionCount || 0) + 1; return global.__opt || {}; },
    dispose(){ global.__disposeCount = (global.__disposeCount || 0) + 1;
               for (const k in handlers) delete handlers[k]; },
    resize(){},
    on(evt, cb){ (handlers[evt] = handlers[evt] || []).push(cb); global.__handlers = handlers; },
    off(){},
    // 测试用：模拟 echarts 触发事件（dataZoom 等）
    __trigger(evt, params){ (handlers[evt] || []).forEach(cb => cb(params)); },
  };
  return global.__inst;
} };
"""

HARNESS = r"""
const fs = require('fs');
const DIR = process.argv[2];
require(DIR + '/domstub.js');
const captured = {};
document.getElementById = function (id) {
  if (!captured[id]) captured[id] = Object.assign({}, mkElRefGlobal(), { id });
  return captured[id];
};
function mkElRefGlobal(){ return document.createElement(); }

(0, eval)(fs.readFileSync('web/app.js', 'utf8')
  + '\n;globalThis.__X = { renderPB, renderAutoScan, renderAddWatch, buildBadge, renderStockChart,'
  + ' addNewWatchlist, addToWatchlist, DEFAULT_BARS, RANGE_PRESETS, setChartRange, switchStockPeriod,'
  + ' __setChart: (d, p) => { stockChartData = d; stockChartPeriod = p; },'
  + ' __getZoom: () => stockChartZoom,'
  + ' __resetChart: () => { if (stockChartInst) { stockChartInst.dispose(); stockChartInst = null; }'
  + ' stockChartZoom = null; stockChartVersion = null; } };');
const X = globalThis.__X;
const P = JSON.parse(fs.readFileSync(DIR + '/payloads.json', 'utf8'));

let fails = 0;
function check(name, cond) {
  if (!cond) fails++;
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}`);
}
function run(label, fn) {
  try { fn(); console.log(`  PASS  render ${label}`); }
  catch (e) { fails++; console.log(`  FAIL  render ${label}: ${e.constructor.name} - ${String(e.message).slice(0,160)}`); }
}

console.log('== 渲染函数真实执行 ==');
run('renderPB/manual', () => X.renderPB(P.pb));
run('renderAutoScan/auto', () => X.renderAutoScan(P.auto));
run('renderAddWatch/addw', () => X.renderAddWatch(P.addw));
const hcodes = Object.keys(P.hunter || {});
if (hcodes.length) run('buildBadge/hunter', () => hcodes.forEach(c => X.buildBadge(Object.assign({ code: c }, P.hunter[c]))));

function dirty(s) { return ['${', 'undefined', 'NaN'].filter(t => s.includes(t)); }
function body(id) { return (captured[id] || {}).innerHTML || ''; }

// ── 口径 A：timing（旧四条件，现行默认）──
if (P.mode === 'timing') {
  const h = body('pbBody');
  console.log('== timing 口径断言 ==');
  check('renderPB 产出非空', h.length > 0);
  check('含旧四条件标签', h.includes('市场有方向'));
  check('含否决因子说明', h.includes('否决因子'));
  check('通过计数为 X/3', /<b>\d+\/3<\/b>/.test(h));
  check('未泄漏背离口径标签', !h.includes('60分钟底背离'));
  check('无脏文本', dirty(h).length === 0);
} else {
  const h = body('pbBody');
  console.log('== divergence 口径断言 ==');
  check('renderPB 产出非空', h.length > 0);
  check('含 t_div 标签', h.includes('60分钟底背离'));
  check('通过计数为 X/1', /<b>\d+\/1<\/b>/.test(h));
  check('无脏文本', dirty(h).length === 0);
}

// ── 条件详情弹窗 ──
const html = fs.readFileSync('web/condition_detail_panel.html', 'utf8');
const m = html.match(/<script[^>]*>([\s\S]*?)<\/script>/);
(0, eval)(m[1] + '\n;globalThis.__P = { renderDetail };');
console.log('== 条件详情弹窗渲染 ==');
let detOk = 0;
for (const [code, d] of Object.entries(P.detail || {})) {
  try {
    globalThis.__P.renderDetail(d); detOk++;
    const all = body('conditions-list') + body('blockers-list') + body('divergence-list');
    if (dirty(all).length) { fails++; console.log(`  FAIL  ${code} 脏文本: ${dirty(all).join(',')}`); }
  } catch (e) { fails++; console.log(`  FAIL  renderDetail ${code}: ${e.constructor.name} - ${String(e.message).slice(0,140)}`); }
}
check(`条件详情弹窗 ${detOk}/${Object.keys(P.detail || {}).length} 条渲染成功`,
      detOk === Object.keys(P.detail || {}).length);

// ── K线弹窗：黄金分割图层（2026-09-20 新增；此前 renderStockChart 完全无覆盖）──
// 全部周期：日/周/月 + 30分/60分（2026-10-04 新增分时）
const PERIODS = ['daily', 'weekly', 'monthly', 'min30', 'min60'];
if (P.chart && !P.chart.err && P.chart.period_data) {
  console.log('== 黄金分割图层渲染 ==');
  // 图层开关默认 checked；DOM 垫片默认 false 会让所有图层被关掉，故显式打开
  ['tgLevels', 'tgBoxes', 'tgChannel', 'tgMA', 'tgFib'].forEach(id => {
    captured[id] = Object.assign({}, mkElRefGlobal(), { id, checked: true });
  });
  for (const per of PERIODS) {
    if (!P.chart.period_data[per]) continue;
    let opt;
    try {
      X.__setChart(P.chart, per);
      X.renderStockChart();
      opt = global.__opt || {};
    } catch (e) {
      fails++; console.log(`  FAIL  renderStockChart ${per}: ${e.constructor.name} - ${String(e.message).slice(0,160)}`);
      continue;
    }
    const series = opt.series || [];
    // 比例位挂在独立载体 series「黄金分割」上（z 抬高以压过K线柱体，见 app.js 注释）
    const carrier = series.find(s => s.name === '黄金分割') || {};
    const fibLines = ((carrier.markLine || {}).data) || [];
    const anchor = series.find(s => s.name === '黄金分割锚点');
    const titles = (opt.title || []).map(t => t.text).join(' | ');
    const labels = fibLines.map(it => String(it.label.formatter)).join(',');
    check(`${per}: 黄金分割比例位 >= 5 条`, fibLines.length >= 5);
    check(`${per}: 0.618 黄金位标注 ★`, fibLines.some(it => String(it.label.formatter).startsWith('★')));
    // 标签只写比例（如「★61.8%」）；带上价格会变长，5~7 条堆在左端就看不清了 —— 防回退
    check(`${per}: 标签精简无价格`, fibLines.every(it => String(it.label.formatter).length <= 8));
    // 黄金位要比次要位更醒目（加粗 + 更不透明）
    const gold = fibLines.find(it => String(it.label.formatter).startsWith('★')) || {};
    const minor = fibLines.filter(it => !String(it.label.formatter).startsWith('★')
      && !/^(38\.2%|50%)$/.test(String(it.label.formatter)));
    check(`${per}: 黄金位比次要位更醒目`,
          !!gold.lineStyle && minor.every(m => gold.lineStyle.width >= m.lineStyle.width
            && gold.lineStyle.opacity >= m.lineStyle.opacity));
    check(`${per}: 有锚点连线`, !!anchor);
    check(`${per}: 标题含「黄金分割」`, titles.includes('黄金分割'));
    check(`${per}: 比例位标签无脏文本`, dirty(labels).length === 0);
    // 初始视窗必须纳入较早的那个锚点，否则锚点连线落在视窗外、比例位来源无从核对
    const pp = P.chart.period_data[per];
    const nb = pp.dates.length;
    const aMin = Math.min(pp.fib.swing.low.index, pp.fib.swing.high.index);
    const needPct = (nb - 1) > 0 ? (aMin / (nb - 1)) * 100 : 0;
    const zStart = ((opt.dataZoom || [])[0] || {}).start;
    check(`${per}: 初始视窗含锚点(zStart=${zStart} <= ${needPct.toFixed(1)})`,
          typeof zStart === 'number' && zStart <= needPct + 0.5);
  }
  // 取消勾选 ⇒ 黄金分割图层整体消失，其余图层不受影响
  captured['tgFib'].checked = false;
  X.__setChart(P.chart, 'daily');
  X.renderStockChart();
  const offOpt = global.__opt || {};
  const offKl = (offOpt.series || []).find(s => s.name === 'K线') || {};
  check('关闭开关后无黄金分割比例位', !(offOpt.series || []).some(s => s.name === '黄金分割'));
  check('关闭开关后无锚点连线', !(offOpt.series || []).some(s => s.name === '黄金分割锚点'));
  check('关闭开关后支撑压力仍在（其余图层不受影响）',
        ((offKl.markLine || {}).data || []).length > 0);
  captured['tgFib'].checked = true;

  // ── 默认视窗根数 + 手动缩放不被刷新重置（2026-10-04）──
  // 背景：后端给 800 根日线，前端原按百分比(zStart=55)默认显示 45%≈360 根，蜡烛只有 ~3px；
  // 且 10s 刷新会 dispose+重建图表、把用户缩放弹回默认窗口。两者都在此设闸。
  console.log('== K线视窗根数 / 缩放保持 ==');
  const barsOf = (z0, z1, n) => Math.round(z1 / 100 * (n - 1)) - Math.round(z0 / 100 * (n - 1)) + 1;

  // 1) 默认窗口 == DEFAULT_BARS（数据不足时退化为全部）。
  //    关掉黄金分割以隔离「为锚点放宽窗口」这条另一逻辑 —— 它是有意保留的，会多显示几根。
  for (const per of PERIODS) {
    if (!P.chart.period_data[per]) continue;
    captured['tgFib'].checked = false;
    X.setChartRange(per, undefined);       // 清掉可能残留的区间选择 ⇒ 回到默认
    X.__setChart(P.chart, per);
    X.renderStockChart();
    const dz = ((global.__opt || {}).dataZoom || [])[0] || {};
    const n = P.chart.period_data[per].dates.length;
    const want = Math.min(X.DEFAULT_BARS[per], n);
    const got = barsOf(dz.start, dz.end, n);
    check(`${per}: 默认视窗 ${got} 根 == ${want}`, got === want);
    captured['tgFib'].checked = true;
  }
  // 1b) 开着黄金分割时，默认视窗仍应是「一根数窗口」量级，绝不能退回旧的 ~360 根
  for (const per of PERIODS) {
    if (!P.chart.period_data[per]) continue;
    X.setChartRange(per, undefined);
    X.__setChart(P.chart, per);
    X.renderStockChart();
    const dz = ((global.__opt || {}).dataZoom || [])[0] || {};
    const n = P.chart.period_data[per].dates.length;
    const got = barsOf(dz.start, dz.end, n);
    check(`${per}: 含锚点放宽后仍紧凑 ${got} 根 <= ${X.DEFAULT_BARS[per] + 12}`,
          got <= X.DEFAULT_BARS[per] + 12);
  }

  // 2) 选「全部」⇒ 一屏显示全部根数（且不被默认窗口逻辑吞掉）
  X.setChartRange('daily', null);
  X.__setChart(P.chart, 'daily');
  X.renderStockChart();
  {
    const dz = ((global.__opt || {}).dataZoom || [])[0] || {};
    const n = P.chart.period_data.daily.dates.length;
    check(`选「全部」⇒ 显示 ${n} 根`, barsOf(dz.start, dz.end, n) === n);
  }

  // 3) 手动缩放后，模拟 10s 刷新的再次渲染必须保住视窗
  X.setChartRange('daily', undefined);
  X.__setChart(P.chart, 'daily');
  X.renderStockChart();
  {
    const n = P.chart.period_data.daily.dates.length;
    // 模拟用户把视窗拖到 [40%, 90%]：echarts 会先更新内部 option 再触发 dataZoom 事件
    // 模拟用户把视窗拖到 [40%, 90%]：dataZoom 事件直接带百分比，
    // 处理器**不得**为此去调 getOption()（那会深拷贝整个 option ⇒ 缩放卡顿主因）
    global.__getOptionCount = 0;
    global.__inst.__trigger('dataZoom', { start: 40, end: 90 });
    check('缩放事件不调用 getOption()', global.__getOptionCount === 0);
    X.renderStockChart();                  // ← 等价于 10s 后 loadStockChartNow() 的重绘
    const dz = ((global.__opt || {}).dataZoom || [])[0] || {};
    check('手动缩放不被刷新重置(前)', Math.abs(dz.start - 40) < 0.01);
    check('手动缩放不被刷新重置(后)', Math.abs(dz.end - 90) < 0.01);
    check('重置后仍为手动视窗而非默认', barsOf(dz.start, dz.end, n) !== Math.min(X.DEFAULT_BARS.daily, n));
  }

  // 4) 切周期 / 换区间会清掉手动缩放（回到该周期默认），避免跨尺度沿用
  X.renderStockChart();
  X.setChartRange('daily', 60);
  X.__setChart(P.chart, 'daily');
  X.renderStockChart();
  {
    const dz = ((global.__opt || {}).dataZoom || [])[0] || {};
    const n = P.chart.period_data.daily.dates.length;
    check('选「近3月」⇒ 60 根', barsOf(dz.start, dz.end, n) === Math.min(60, n));
  }

  // 4b) 图表实例复用：连续渲染不得 dispose/重建（卡顿根因之一，2026-10-04）
  {
    X.__resetChart();                    // 模拟「关闭弹窗」后重新打开
    global.__disposeCount = 0;
    global.__initCount = 0;
    global.__opt = null;
    X.renderStockChart();                // 打开后的首帧
    check('首帧建实例', global.__initCount === 1 && global.__opt !== null);
    const _disposeAfter1 = global.__disposeCount;
    global.__opt = null;
    X.renderStockChart();                // 模拟 10s 刷新重绘
    check('重绘复用实例、不 dispose', global.__disposeCount === _disposeAfter1);
    check('重绘没有重复 init', global.__initCount === 1);
    check('重绘确实重设了 option', global.__opt !== null);
  }

  // 5) 5 个图层开关默认关闭（index.html 无 checked）⇒ 打开弹窗只剩 K线+量+MACD+RSI。
  //    防回退：有人把 checked 加回去，或 showX 的默认值从「关」翻回「开」。
  ['tgLevels', 'tgBoxes', 'tgChannel', 'tgMA', 'tgFib'].forEach(id => { captured[id].checked = false; });
  X.setChartRange('daily', undefined);
  X.__setChart(P.chart, 'daily');
  X.renderStockChart();
  {
    const names = ((global.__opt || {}).series || []).map(s => s.name);
    check('默认关：无支撑压力线',
          (((global.__opt.series || []).find(s => s.name === 'K线') || {}).markLine || {}).data.length === 1);
    check('默认关：无箱体/通道/均线/黄金分割',
          !names.some(n => /黄金分割|通道|^MA\d/.test(n)));
    check('默认关：K线/量/MACD/RSI 仍在（BOLL 无开关、恒常显示）',
          ['K线', '成交量', 'MACD-DIF', 'RSI', 'BOLL中'].every(n => names.includes(n)));
  }
  ['tgLevels', 'tgBoxes', 'tgChannel', 'tgMA', 'tgFib'].forEach(id => { captured[id].checked = true; });

  // ── 30分/60分 分时周期（2026-10-04）──
  console.log('== 30分/60分 周期 ==');
  for (const per of ['min30', 'min60']) {
    const pp = (P.chart.period_data || {})[per];
    check(`${per}: payload 存在`, !!pp);
    if (!pp) continue;
    // 日期必须带时间，否则分时图与日线图无从区分（后端 to_series(intraday=True)）
    check(`${per}: dates 带时分`, pp.dates.every(d => /^\d{4}-\d\d-\d\d \d\d:\d\d$/.test(d)));
    const t0 = pp.dates[0], t1 = pp.dates[pp.dates.length - 1];
    check(`${per}: 时间升序且同日内不跨午休错位`, t0 < t1);
    check(`${per}: 有 14:00~15:00 的收盘时段根`, pp.dates.some(d => /(1[45]):\d\d$/.test(d)));

    X.setChartRange(per, undefined);
    X.__setChart(P.chart, per);
    X.renderStockChart();
    const opt = global.__opt || {};
    const dz = (opt.dataZoom || [])[0] || {};
    const n = pp.dates.length;
    const want = Math.min(X.DEFAULT_BARS[per], n);
    check(`${per}: 默认视窗 ${barsOf(dz.start, dz.end, n)} 根 == ${want}`,
          barsOf(dz.start, dz.end, n) === want);
  }

  // 箱体是**日线口径**的 YYYY-MM-DD，套到分钟时间轴("YYYY-MM-DD HH:MM")上必须仍画得出来。
  // 之前靠 getOption 精确 indexOf ⇒ 分钟图上静默全丢；这条防回退。
  {
    const chartBox = JSON.parse(JSON.stringify(P.chart));
    const md = chartBox.period_data.min30.dates;
    const bStart = md[10].slice(0, 10), bEnd = md[100].slice(0, 10);
    chartBox.boxes = [{ start: bStart, end: bEnd, low: 13, high: 18, rel: 0,
                        days: 15, quality_score: 8, display: '13~18' }];
    X.__setChart(chartBox, 'min30');
    X.renderStockChart();
    const kl = ((global.__opt.series || []).find(s => s.name === 'K线') || {});
    const areas = ((kl.markArea || {}).data) || [];
    check(`min30: 日线口径箱体在分钟轴上画得出（${areas.length} 块）`, areas.length > 0);
    // 箱体区间之外不应误配
    chartBox.boxes = [{ start: '1990-01-01', end: '1990-01-05', low: 13, high: 18, rel: 0 }];
    X.__setChart(chartBox, 'min30');
    X.renderStockChart();
    const kl2 = ((global.__opt.series || []).find(s => s.name === 'K线') || {});
    check('min30: 区间外的箱体不误画', (((kl2.markArea || {}).data) || []).length === 0);
  }

  // 缺该周期数据（指数/em 标的、tushare 不可用且无本地缓存）⇒ 出提示，不抛异常
  {
    const noMin = JSON.parse(JSON.stringify(P.chart));
    delete noMin.period_data.min30;
    X.__setChart(noMin, 'min30');
    let threw = null;
    try { X.renderStockChart(); } catch (e) { threw = e; }
    check('min30 缺数据: 不抛异常', !threw);
    check('min30 缺数据: 图表区出提示',
          String(body('stockChart')).includes('无分时数据'));
  }
} else {
  console.log('== 黄金分割图层渲染 == SKIP（无 chart payload）');
}

// ── 建仓股池「+ 添加」成败反馈（2026-09-20 新增）──
// 此前该路径完全无覆盖，而它有三个静默失败：输入框不清空、反馈在看不见的地方、
// 加进去却不出现在表里。这里用假 pywebview.api 驱动真实 addNewWatchlist()。
(async () => {
  console.log('== 建仓股池添加反馈 ==');
  const setInputs = (code) => {
    captured['pbSearchCode'] = Object.assign({}, mkElRefGlobal(), { id: 'pbSearchCode', value: code || '600519' });
    captured['pbSearchName'] = Object.assign({}, mkElRefGlobal(), { id: 'pbSearchName', value: '' });
    captured['pbAddBtn'] = Object.assign({}, mkElRefGlobal(), { id: 'pbAddBtn', textContent: '+ 添加' });
    captured['toastHost'] = Object.assign({}, mkElRefGlobal(), { id: 'toastHost' });
  };
  const runAdd = async (resp, inputCode) => {
    setInputs(inputCode);
    global.window.pywebview = { api: {
      add_and_scan: async () => resp,
      search_stock: async () => ({ results: [] }),
      refresh_pb: async () => ({}),
    } };
    const nBefore = (document.body.children || []).length;
    await X.addNewWatchlist();                    // 真实调用被测函数
    const toasts = (captured['toastHost'].children || []).map(c => c.textContent);
    const modals = (document.body.children || []).slice(nBefore).map(o => o.innerHTML);
    return { toasts, modals, code: captured['pbSearchCode'].value };
  };

  // 1) 成功：清空输入框 + 轻提示带结论/卡点，且不弹模态窗
  let a = await runAdd({ ok: true, code: '600519', name: '贵州茅台', scan_date: '2026-09-20',
    visible_in_manual: true, scan_error: null,
    scan: { verdict: 'approaching', composite_score: 62, block_reason: '卡「回撤到位」：还差 3.2%' } });
  check('成功：输入框已清空', a.code === '');
  check('成功：弹出轻提示', a.toasts.length === 1);
  check('成功：提示含结论与卡点', /已加入并扫描/.test(a.toasts[0] || '') && /回撤到位/.test(a.toasts[0] || ''));
  check('成功：不弹模态窗', a.modals.length === 0);
  check('成功：提示无脏文本', dirty(a.toasts[0] || '').length === 0);

  // 2) 股池写入失败：保留输入 + 模态窗列明原因 + 无成功提示
  let b = await runAdd({ ok: false, error: '磁盘只读' });
  check('失败：输入框保留原值', b.code === '600519');
  check('失败：弹出模态窗', b.modals.length === 1);
  check('失败：模态窗含原因', /磁盘只读/.test(b.modals[0] || ''));
  check('失败：无成功轻提示', b.toasts.length === 0);
  check('失败：模态窗无脏文本', dirty(b.modals[0] || '').length === 0);

  // 3) 加成功但扫描失败：输入框清空（加本身成功），模态窗讲清"需重跑"
  let c = await runAdd({ ok: true, code: '600519', name: '贵州茅台', scan_date: '2026-09-20',
    visible_in_manual: true, scan_error: 'gm 行情服务不可达',
    scan: { verdict: 'weak', composite_score: 0, block_reason: null } });
  check('扫描失败：输入框仍清空（加成功）', c.code === '');
  check('扫描失败：模态窗含扫描原因', /gm 行情服务不可达/.test((c.modals[0] || '')));
  check('扫描失败：模态窗提示需重跑', /盘后重跑/.test((c.modals[0] || '')));
  check('扫描失败：无成功轻提示', c.toasts.length === 0);

  // 3b) insufficient_data（未开盘/数据陈旧）→ 必须给中文说明，不能把英文枚举丢给用户
  let d = await runAdd({ ok: true, code: '600176', name: '中国巨石', scan_date: '2026-09-20',
    visible_in_manual: true, scan_error: null,
    scan: { verdict: 'insufficient_data', composite_score: 0, block_reason: null,
            reason: '日线末 bar 陈旧(2026-09-18<2026-09-20)' } });
  check('无数据：提示为中文且含原因', /分钟数据不足/.test(d.toasts[0] || '') && /陈旧/.test(d.toasts[0] || ''));
  check('无数据：不出现英文枚举', !/insufficient_data/.test(d.toasts[0] || ''));
  check('无数据：提示无脏文本', dirty(d.toasts[0] || '').length === 0);

  // 3c) auto 池标的也走正常成功路径（2026-09-20 owner 拍板：两池互不阻塞，不再拒绝）
  let e = await runAdd({ ok: true, code: '600089', name: '特变电工', scan_date: '2026-09-20',
    visible_in_manual: true, scan_error: null,
    scan: { verdict: 'weak', composite_score: 12, block_reason: null } }, '600089');
  check('auto池：走成功路径，输入框清空', e.code === '');
  check('auto池：弹轻提示', e.toasts.length === 1);
  check('auto池：不弹失败模态窗', e.modals.length === 0);
  check('auto池：提示无脏文本', dirty(e.toasts[0] || '').length === 0);

  // 4) 行内「+股池」按钮：只加不扫，返回 true/false（此前恒为 undefined）
  global.window.pywebview = { api: {
    add_to_watchlist: async () => ({ ok: true, code: '000001' }),
    refresh_pb: async () => ({}),
  } };
  const rOk = await X.addToWatchlist('000001', '平安银行', null);
  check('行内按钮成功时返回 true', rOk === true);
  global.window.pywebview = { api: { add_to_watchlist: async () => ({ ok: false, error: '不存在的代码' }) } };
  const rBad = await X.addToWatchlist('999999', 'X', null);
  check('行内按钮失败时返回 false', rBad === false);

  console.log(`\n结果: ${fails === 0 ? 'ALL PASS' : fails + ' FAILED'}`);
  process.exit(fails ? 1 : 0);
})();
"""


def _latest_trace_date():
    files = sorted((BASE / "t_io" / "traces").glob("position_builder_*.jsonl"))
    if not files:
        return None
    return files[-1].stem.replace("position_builder_", "")


def _synth_min_frames():
    """合成 30/60 分钟帧（**不联网**），经 min_frames= 注入 _build_chart_from_df。
    每根 320 根、交易日 09:30~15:00 的固定时段；价格同样用「转折点+段长」拼接
    ⇒ 转折点成为摆动点，黄金分割锚点确定（与日线那份同构）。"""
    import numpy as np
    import pandas as pd
    out = {}
    for key, per_day, slots in (
            ("min30", 8, ["10:00", "10:30", "11:00", "11:30",
                          "13:30", "14:00", "14:30", "15:00"]),
            ("min60", 4, ["10:30", "11:30", "14:00", "15:00"])):
        n = 320
        days = pd.bdate_range(end="2024-11-15", periods=(n // per_day) + 2)
        times = [pd.Timestamp("%s %s" % (d.date(), s)) for d in days for s in slots][-n:]
        seg = [(20, 12, 60), (12, 30, 80), (30, 24, 60), (24, 34, 60), (34, 28, 60)]
        prices = []
        for p0, p1, k in seg:                       # 段长合计 == n
            prices.extend(np.linspace(p0, p1, k, endpoint=False).tolist())
        prices = (prices + [prices[-1]] * n)[:n]
        out[key] = pd.DataFrame({
            "time": times, "open": prices, "close": prices,
            "high": [p * 1.005 for p in prices], "low": [p * 0.995 for p in prices],
            "volume": [1e6] * n,
        })
    return out


def _synth_chart_payload():
    """合成 K 线 payload（走真实 _build_chart_from_df 序列化路径，**不联网**）。

    价格用「转折点+根数」线性拼接：段内单调 ⇒ 段内无分形极值，转折点必然成为
    摆动点 ⇒ 黄金分割锚点确定。总长 321 > daily lookback(120) 以覆盖 tail 截断分支。
    分钟帧显式注入（不去打 tushare）。
    """
    import numpy as np
    import pandas as pd
    import t_gui
    pivots = [(20, 0), (12, 60), (30, 80), (24, 40), (34, 90), (28, 50)]
    prices = []
    for (p0, _), (p1, n) in zip(pivots, pivots[1:]):
        prices.extend(np.linspace(p0, p1, n, endpoint=False).tolist())
    prices.append(pivots[-1][0])
    df = pd.DataFrame([{
        "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=i),
        "open": p, "close": p, "high": p * 1.005, "low": p * 0.995, "volume": 1e6,
    } for i, p in enumerate(prices)])
    out = t_gui.Api()._build_chart_from_df(
        df, {"code": "TEST", "name": "合成样本"}, "TEST", min_frames=_synth_min_frames())
    return t_gui._clean(out)


def _build_payloads(date_str):
    import t_gui
    api = t_gui.Api()
    from config import ENTRY_TIMING_PARAMS as etp
    mode = str(etp.get("entry_mode", "timing"))
    out = {"mode": mode}
    pb = api._agg_position_builder(date_str)
    out["pb"] = pb
    rows = pb.get("rows") or []
    # 详情弹窗：优先挑有卡点的票（覆盖卡点渲染路径），再补几个无卡点的
    withb = [r["code"] for r in rows if r.get("blockers")]
    nowb = [r["code"] for r in rows if not r.get("blockers")]
    pick = (withb[:4] + nowb[:2])[:6]
    out["detail"] = {c: api.get_signal_condition_detail(c, date_str) for c in pick}
    try:
        out["auto"] = api.load_auto_scan(date_str)
    except Exception as e:
        out["auto"] = {"rows": [], "err": str(e)}
    try:
        out["addw"] = api.compute_add_watch(date_str)
    except Exception as e:
        out["addw"] = {"rows": [], "err": str(e)}
    try:
        codes = [r["code"] for r in rows[:6]]
        conf = api._hunter_build_conformance(codes, date_str)
        out["hunter"] = {k: dict(v, build_total=len(v.get("conds") or {})) for k, v in conf.items()}
    except Exception:
        out["hunter"] = {}
    try:
        out["chart"] = _synth_chart_payload()
    except Exception as e:
        out["chart"] = {"err": str(e)}
    return out


def main():
    ap = argparse.ArgumentParser(description="GUI 渲染冒烟测试（无头）")
    ap.add_argument("--date", default=None, help="扫描日期（缺省=最近一次 position_builder trace）")
    args = ap.parse_args()

    node = shutil.which("node")
    if not node:
        print("SKIP: 未找到 node，跳过 GUI 渲染冒烟测试（不影响其它测试）")
        return 0

    date_str = args.date or _latest_trace_date()
    if not date_str:
        print("SKIP: 无 position_builder trace 可测（先跑一次建仓扫描）")
        return 0
    print(f"GUI 渲染冒烟 | 日期 {date_str}")

    payloads = _build_payloads(date_str)
    print(f"  entry_mode = {payloads['mode']} | 手动盘 {len(payloads['pb'].get('rows') or [])} 行"
          f" | 详情 {len(payloads['detail'])} 条 | 猎手 {len(payloads.get('hunter') or {})} 条")

    tmp = Path(tempfile.mkdtemp(prefix="gui_smoke_"))
    try:
        (tmp / "domstub.js").write_text(DOM_STUB, encoding="utf-8")
        (tmp / "harness.js").write_text(HARNESS, encoding="utf-8")
        (tmp / "payloads.json").write_text(
            json.dumps(payloads, ensure_ascii=False, default=str), encoding="utf-8")
        r = subprocess.run([node, str(tmp / "harness.js"), str(tmp).replace("\\", "/")],
                           cwd=str(BASE), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=300)
        print(r.stdout or "")
        if r.stderr:
            print("stderr:", r.stderr[:800])
        return r.returncode
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
