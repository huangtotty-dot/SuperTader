// 历史成交页渲染冒烟：用 DOM 桩在 node 里真跑 renderTradeHistory，检查**每张图都渲染了**。
//
// 为什么要这个：2026-10-10 出过一次「三张图空白」——renderTradeHistory 里一个 const
// 声明晚于使用（TDZ ReferenceError），函数跑到中间就抛了，后面的图**静默留白**，
// 界面上没有任何提示。node --check 查不出这种错（语法合法），只有真跑一遍才发现。
//
// 用法：node tests/phase3/th_ui_harness.js <ledger.json>
// 退出码：0 = 全部图渲染；1 = 有图没渲染 / 抛异常。失败时打印每张图的状态。

const fs = require('fs');
const path = require('path');

const APP = process.env.TH_APP || path.join(__dirname, '..', '..', 'web', 'app.js');
const ledPath = process.argv[2];
if (!ledPath) { console.error('用法: node th_ui_harness.js <ledger.json>'); process.exit(2); }
const led = JSON.parse(fs.readFileSync(ledPath, 'utf8'));
// renderTradeHistory 在 !imported 时会走「尚未导入」分支直接返回 ⇒ 会表现成「五张图都没画」，
// 看起来像 UI bug。这里显式挡一道，让 fixture 的问题一眼可辨。
if (!led.imported) {
  console.error('台账缺 imported=true —— 这不是 UI bug，是 fixture 没按 GUI 落盘的样子构造。');
  process.exit(2);
}

// ---- 把源码里的注释/字符串/正则换成等长空格（保留行号与下标），便于安全地配平大括号 ----
// 正则字面量里可能含引号（如 /[&<>"']/g），不识别就会把引号当字符串起点、整段弄乱。
// 用标准启发式判断 `/` 是正则还是除号：看前一个有意义字符。
function regexAllowed(prev) {
  if (prev === '') return true;
  return '(,=:[!&|?{};+-*%^~<>'.includes(prev);
}
function blank(code) {
  const out = [];
  let i = 0;
  const n = code.length;
  let prev = '';                       // 上一个有意义字符（跳过空白）
  const push = (str) => { out.push(str); };
  while (i < n) {
    const c = code[i];
    if (c === '/' && code[i + 1] === '/') {
      let j = code.indexOf('\n', i); if (j < 0) j = n;
      push(' '.repeat(j - i)); i = j;
    } else if (c === '/' && code[i + 1] === '*') {
      let j = code.indexOf('*/', i + 2); j = j < 0 ? n : j + 2;
      push(code.slice(i, j).replace(/[^\n]/g, ' ')); i = j;
    } else if (c === '/' && regexAllowed(prev)) {
      let j = i + 1, inClass = false;
      while (j < n) {
        const d = code[j];
        if (d === '\\') { j += 2; continue; }
        if (d === '[') inClass = true;
        else if (d === ']') inClass = false;
        else if (d === '/' && !inClass) { j++; break; }
        else if (d === '\n') break;                 // 未闭合，当除号处理
        j++;
      }
      push(code.slice(i, j).replace(/[^\n]/g, ' ')); i = j;
    } else if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (j < n) {
        if (code[j] === '\\') { j += 2; continue; }
        if (code[j] === c) { j++; break; }
        j++;
      }
      push(' '.repeat(j - i)); i = j;
    } else {
      push(c); i++;
      if (!/\s/.test(c)) prev = c;
    }
  }
  return out.join('');
}

const src = fs.readFileSync(APP, 'utf8');
const srcBlank = blank(src);

// 按 `function name(` 定位，在**去注释源码**上配平大括号，再用**同一区间**切原源码
function extract(name) {
  const re = new RegExp('\\bfunction\\s+' + name + '\\s*\\(');
  const m = re.exec(srcBlank);
  if (!m) throw new Error('app.js 里找不到函数: ' + name);
  let depth = 0, started = false;
  for (let i = m.index; i < srcBlank.length; i++) {
    if (srcBlank[i] === '{') { depth++; started = true; }
    else if (srcBlank[i] === '}') { depth--; if (started && depth === 0) return src.slice(m.index, i + 1); }
  }
  throw new Error('大括号不配平: ' + name);
}

const NEEDED = ['esc', 'fmt', 'clsOf', 'pnlColor', 'echGuard', '_jgdSlice', 'renderTradeHistory'];
const body = NEEDED.map(extract).join('\n\n');

// ---- DOM / ECharts 桩 ----
function mkEl(id) {
  return { id, innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
           classList: { add() {}, remove() {} }, querySelectorAll: () => [], querySelector: () => null };
}
const els = {};
const errors = [];
const rendered = [];
const cleared = [];

const sandbox = {
  document: {
    getElementById: (id) => (els[id] || (els[id] = mkEl(id))),
    querySelectorAll: () => [],
    querySelector: () => null,
  },
  console: { error: (...a) => errors.push(a.map(String).join(' ')), log: () => {}, warn: () => {} },
  echRender: (id) => rendered.push(id),
  echClear: (id) => cleared.push(id),
  ECH_BASE: { grid: {}, xAxis: {}, yAxis: {}, tooltip: {} },
  state: {},
};

const factory = new Function(...Object.keys(sandbox),
  body + '\nreturn { renderTradeHistory };');
const mod = factory(...Object.values(sandbox));

const EXPECT = ['echThMonth', 'echThTNet', 'echThFee', 'echThT', 'echThCumPct'];
const accts = (led.accounts || []).map((a) => a.account);

let bad = 0;
function run(mode, label) {
  rendered.length = 0; cleared.length = 0; errors.length = 0;
  // 模拟顶部的账户下拉（多账户时显示，选中值为 mode）
  const sel = sandbox.document.getElementById('thAccount');
  sel.style = { display: accts.length > 1 ? '' : 'none' };
  sel.value = mode || '';
  sel.dataset = { built: accts.join('|') };
  try {
    mod.renderTradeHistory(led);
  } catch (e) {
    console.log(`  ✗ ${label}: 抛异常 ${e.message}`);
    bad++; return;
  }
  const missing = EXPECT.filter((x) => !rendered.includes(x));
  const guarded = errors.length;
  if (missing.length) { console.log(`  ✗ ${label}: 未渲染 ${missing.join(', ')}`); bad++; }
  else console.log(`  ✓ ${label}: 五张图全部渲染${guarded ? `（${guarded} 张被 echGuard 兜住，见下）` : ''}`);
  errors.forEach((e) => console.log(`      [echGuard] ${e}`));
}

console.log(`历史成交页渲染冒烟（ledger: ${path.basename(ledPath)}, 账户 ${accts.length} 个）`);
run(null, '全部账户');
accts.forEach((a) => run(a, `账户 ${a}`));

// ---- 故障注入：让月度数组崩掉，验证 echGuard 不让它连累其它图 ----
const broken = JSON.parse(JSON.stringify(led));
broken.t_monthly = [{ month: null, realized: null, fees: null }];
const real = led.t_monthly; led.t_monthly = broken.t_monthly;
rendered.length = 0; errors.length = 0;
try {
  mod.renderTradeHistory(led);
  const stillOk = EXPECT.filter((x) => x !== 'echThT' && x !== 'echThTNet')
                        .every((x) => rendered.includes(x));
  console.log(stillOk
    ? '  ✓ 故障注入：做T 数据崩了，其余图仍照画（echGuard 生效）'
    : '  ✗ 故障注入：一张图崩掉连累了其它图');
  if (!stillOk) bad++;
} catch (e) {
  console.log(`  ✗ 故障注入：异常逃逸出 echGuard: ${e.message}`);
  bad++;
}
led.t_monthly = real;

console.log(bad ? `\n结果: ${bad} 项失败` : '\n结果: ALL PASS');
process.exit(bad ? 1 : 0);
