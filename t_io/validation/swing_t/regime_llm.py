# -*- coding: utf-8 -*-
"""波段做T · LLM 状态识别挖掘臂（DeepSeek）—— **日线**。

## 与日内 LLM 臂的区别（决定度量必须换）

  日内（`factor_mining/llm_mine.py`）：适应度 = 单笔往返净收益
  本模块：适应度 = **叠加 vs 纯买入持有**（收益增量 / 回撤改善，对照实测天花板）

## owner 规格里的一处张力（留给搜索探索）

  "单边上涨段只加不减（防卖飞）" ⟷ "减少波动、拿得住"
  只加不减 ⇒ 活动仓在上涨中不断累积 ⇒ 反转时正好满仓 ⇒ **回撤更深**。
  这正是 8 个基线状态因子**全部加深回撤**的机制。
  ⇒ 要挖的不是"上涨就加"，而是 **"上涨的哪个阶段还能加"**。

## 输出契约（模型只需写这个）

  def score(ctx) -> np.ndarray    # 日线因子值，长度 == ctx['n']
  策略层再把 score 按**自身历史的因果分位**切成 up/range/down（见 regime_mine.make_regime_fn）。

用法：
  export DEEPSEEK_API_KEY=sk-xxx
  python regime_llm.py --codes <逗号> --rounds 4 --n-propose 6
  python regime_llm.py --stub          # 无 key 时验证管线
"""
import argparse
import glob
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
for _p in (ROOT, os.path.join(ROOT, 't_io', 'validation', 't0_schemes'),
           os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t'),
           os.path.join(ROOT, 't_io', 'validation', 'factor_mining'), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import factor_ops as fo  # noqa: E402
import overlay_sim as OS  # noqa: E402
import regime_mine as RM  # noqa: E402

PENALTY = -99.0
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'


def _api_names():
    return [k for k in dir(fo) if not k.startswith('_') and callable(getattr(fo, k))]


def _sandbox_ns():
    ns = {k: getattr(fo, k) for k in _api_names()}
    ns['np'] = np
    return ns


PROMPT = """你在为一个 **A股波段做T** 项目挖「**状态识别**」因子。

## 背景（务必理解，不要提"择时信号"）
底仓**全程不动**（"拿住"）；**活动仓 = 底仓的 30%**：
  · **震荡段** → 活动仓低吸高抛
  · **单边上涨段** → 活动仓**只加不减**（防卖飞）
  · 单边下跌 → 不动
你要挖的是**判断当前处于哪种状态**，不是买卖点。

## 已知张力（可尝试解决）
"上涨只加不减"会让活动仓在上涨中不断累积，反转时正好满仓 ⇒ **回撤更深**。
实测 8 个经典状态度量（MA斜率/效率比/通道位置/波动扩张/日线MACD/动量/振幅比）
**全部把回撤做得更差**（−0.29~−0.91pp，而前视天花板是 +2.29pp），收益侧最多只抓到 7.9%。
**提示方向**：能否识别"上涨的哪一段"（早期可加 / 末期该停）？能否用**回撤/波动状态**而非仅价格方向？

## 你的输出（严格）
只输出一个 python 代码块：
```python
def score(ctx):
    # 日线 ctx：t,o,h,l,c,v,amt,vwap,ret,hi_so_far,lo_so_far,prev_close,daily_atr,n
    # 可用算子：{api}
    # 返回长度 == ctx['n'] 的 numpy 数组（**状态评分**，高分=更可能是单边上涨）
    return ...
```
**严格因果**：第 i 天只能用到 ≤ i 的数据。

## 已测过、不要重复
ma_slope / trend_eff / dist_ma / donchian / vol_ratio5_20 / macd_d / ret20 / amp_ratio

{hist}
请给出 **{n} 个互不相同**的候选，每个前一行 `### 因子名：<名字>`。
"""


def deepseek_propose(prompt, key):
    import requests
    r = requests.post(DEEPSEEK_URL,
                      headers={'Authorization': f'Bearer {key}',
                               'Content-Type': 'application/json'},
                      json={'model': 'deepseek-chat',
                            'messages': [{'role': 'user', 'content': prompt}],
                            'temperature': 1.0, 'stream': False}, timeout=180)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']


def stub_propose(prompt, key=None):
    """桩：一条**合法**（验证打分路径）+ 一条**故意报错**（验证异常被兜住）。"""
    return """### 因子名：stub_valid
```python
def score(ctx):
    return div(ctx['c'], ts_mean(ctx['c'], 20))
```
### 因子名：stub_if
```python
def score(ctx):
    return sub(ts_mean(ctx['c'], 5), ts_mean(ctx['c'], 60))
```
### 因子名：stub_bad
```python
def score(ctx):
    return undefined_thing(ctx)
```
"""


def extract_blocks(text):
    out = []
    for m in re.finditer(r'###\s*因子名[:：]\s*(\S+)\s*```(?:python)?\s*(.*?)```', text, re.S):
        out.append((m.group(1), m.group(2).strip()))
    return out


def build_score_fn(code):
    ns = _sandbox_ns()
    exec(compile(code, '<llm>', 'exec'), ns)
    fn = ns.get('score')
    if not callable(fn):
        raise ValueError('未定义 score(ctx)')
    return fn


def fitness(fn, codes, act=0.3, grid=0.02):
    """→ (收益增量pp, 回撤改善pp)。"""
    tot_d, tot_dd = [], []
    for c in codes:
        bars = OS.daily_bars(c)
        if len(bars) < 80:
            continue
        ctx = RM.daily_ctx(bars)
        try:
            fv = np.asarray(fn(ctx), float).ravel()
        except Exception:
            return PENALTY, PENALTY
        if fv.shape[0] != len(bars) or not np.any(np.isfinite(fv)):
            return PENALTY, PENALTY
        no, nb, _t = OS.simulate(bars, act_frac=act, grid=grid,
                                 regime_fn=RM.make_regime_fn(fv))
        so, sb = OS.stats(no), OS.stats(nb)
        tot_d.append((so['total'] - sb['total']) * 100)
        tot_dd.append((so['maxdd'] - sb['maxdd']) * 100)
    if not tot_d:
        return PENALTY, PENALTY
    return float(np.mean(tot_d)), float(np.mean(tot_dd))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--rounds', type=int, default=4)
    ap.add_argument('--n-propose', type=int, default=6)
    ap.add_argument('--act', type=float, default=0.3)
    ap.add_argument('--grid', type=float, default=0.02)
    ap.add_argument('--stub', action='store_true')
    ap.add_argument('--out', default=os.path.join(HERE, 'regime_llm_ledger.jsonl'))
    args = ap.parse_args()
    key = os.environ.get('DEEPSEEK_API_KEY')
    if not args.stub and not key:
        print('[llm] 未设 DEEPSEEK_API_KEY；用 --stub 可验证管线'); return
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in glob.glob(os.path.join(OS.v2.CSV_DIR, '*_1year_1min.csv'))})[:15]
    print(f'[regime-llm] codes={len(codes)} 提案器={"stub" if args.stub else "deepseek-chat"} '
          f'act={args.act:.0%} grid={args.grid:.1%}  天花板={RM.CEIL_RET}/{RM.CEIL_DD}pp')

    hist, seen, rows = [], set(), []
    t0 = time.time()
    fh = open(args.out, 'a', encoding='utf-8')
    for rnd in range(args.rounds):
        h = '\n'.join(f'  收益 {r["d_ret"]:+.2f}pp 回撤 {r["d_dd"]:+.2f}pp  {r["code"][:90]}'
                      for r in sorted(rows, key=lambda x: -x['d_ret'])[:5]) or '  （暂无）'
        prompt = PROMPT.format(api=', '.join(_api_names()), hist=f'## 已提过的（含分数）\n{h}\n', n=args.n_propose)
        try:
            text = stub_propose(prompt) if args.stub else deepseek_propose(prompt, key)
        except Exception as e:
            print(f'  [warn] 提案失败: {type(e).__name__} {e}'); break
        blocks = extract_blocks(text)
        print(f'\n--- 第 {rnd + 1} 轮：{len(blocks)} 条 ---')
        for name, code in blocks:
            if code in seen:
                continue
            seen.add(code)
            try:
                fn = build_score_fn(code)
                d_ret, d_dd = fitness(fn, codes, act=args.act, grid=args.grid)
                err = None
            except Exception as e:
                d_ret = d_dd = PENALTY; err = f'{type(e).__name__}: {e}'
            rec = {'round': rnd + 1, 'name': name, 'code': code,
                   'd_ret': round(d_ret, 3), 'd_dd': round(d_dd, 3), 'error': err}
            rows.append(rec); fh.write(json.dumps(rec, ensure_ascii=False) + '\n'); fh.flush()
            tag = (f'收益捕获 {d_ret / RM.CEIL_RET:.0%} / 回撤捕获 {d_dd / RM.CEIL_DD:.0%}'
                   if d_ret > PENALTY else '拒(报错/*)')
            print(f'   {name[:22]:24} 收益 {d_ret:+7.2f}pp  回撤 {d_dd:+6.2f}pp   {tag}'
                  + (f'  [{err}]' if err else ''))
    fh.close()
    ok = [r for r in rows if r['d_ret'] > PENALTY]
    if ok:
        b = max(ok, key=lambda r: r['d_ret'])
        print(f'\n[regime-llm] 最优(按收益): {b["name"]}  {b["d_ret"]:+.2f}pp / 回撤 {b["d_dd"]:+.2f}pp'
              f'   收益捕获 {b["d_ret"] / RM.CEIL_RET:.0%}')
        print(f'[regime-llm] 代码:\n{b["code"]}')
    print(f'[regime-llm] 共 {len(rows)} 条，用时 {time.time() - t0:.0f}s -> {args.out}')


if __name__ == '__main__':
    main()
