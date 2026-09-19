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
  innerHTML: '', textContent: '', value: '', checked: false,
  style: new Proxy({}, { get: () => '', set: () => true }),
  classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } },
  dataset: {}, children: [], scrollTop: 0, scrollHeight: 0,
  appendChild(){}, removeChild(){}, remove(){}, setAttribute(){}, getAttribute(){ return null; },
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
// 故必须挂在 window 上。只捕获 setOption 的 option，供「黄金分割图层」断言。
global.window.echarts = { init(){ return { setOption(o){ global.__opt = o; }, dispose(){}, resize(){}, on(){}, off(){} }; } };
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
  + ' __setChart: (d, p) => { stockChartData = d; stockChartPeriod = p; } };');
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
if (P.chart && !P.chart.err && P.chart.period_data) {
  console.log('== 黄金分割图层渲染 ==');
  // 图层开关默认 checked；DOM 垫片默认 false 会让所有图层被关掉，故显式打开
  ['tgLevels', 'tgBoxes', 'tgChannel', 'tgMA', 'tgFib'].forEach(id => {
    captured[id] = Object.assign({}, mkElRefGlobal(), { id, checked: true });
  });
  for (const per of ['daily', 'weekly', 'monthly']) {
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
} else {
  console.log('== 黄金分割图层渲染 == SKIP（无 chart payload）');
}

console.log(`\n结果: ${fails === 0 ? 'ALL PASS' : fails + ' FAILED'}`);
process.exit(fails ? 1 : 0);
"""


def _latest_trace_date():
    files = sorted((BASE / "t_io" / "traces").glob("position_builder_*.jsonl"))
    if not files:
        return None
    return files[-1].stem.replace("position_builder_", "")


def _synth_chart_payload():
    """合成 K 线 payload（走真实 _build_chart_from_df 序列化路径，**不联网**）。

    价格用「转折点+根数」线性拼接：段内单调 ⇒ 段内无分形极值，转折点必然成为
    摆动点 ⇒ 黄金分割锚点确定。总长 > 250 以覆盖 daily 的 lookback 分支。
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
    out = t_gui.Api()._build_chart_from_df(df, {"code": "TEST", "name": "合成样本"}, "TEST")
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
