# -*- coding: utf-8 -*-
"""P3 · LLM 臂：由 DeepSeek 提议**用算子库写的新因子**。

## 与 GP 臂的分工（这是换 LLM 的意义所在）

- **GP 臂**：在「22 个既有因子的算术组合」空间里搜 → 够不到新的时序结构
- **LLM 臂**：让模型**写代码**，用 `factor_ops` 的原语（`ts_corr/ts_rank/decay_linear/
  downvol_share/smart_money_dev/…`）构造新的日内结构 → **GP 够不到的空间**

两臂**共用同一套裁决**（z-score 穿越入场 + 固定出场 + 0.136% 成本 + 密度闸门），
所以结果可直接比较。

## 安全（exec 模型生成的代码）

`_sandbox_ns()` 只暴露 `factor_ops` 的公开符号 + numpy；用受限 globals 执行。
**这不是强沙箱**（Python 层面无法完全隔离），仅作最低限度约束：
不给 `open/os/sys/import`，且只在本地研究环境运行。**不要在生产进程里跑本模块。**

## 用法

  export DEEPSEEK_API_KEY=sk-xxx
  python llm_mine.py --codes <逗号> --n-propose 6 --rounds 3

  # 无 key 时用桩提案器验证管线（不发网络请求）：
  python llm_mine.py --stub --codes 600176,000988,300054
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
           os.path.join(ROOT, 't_io', 'validation', 'macd_divergence_t'), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import eval_factor as E  # noqa: E402
import factor_ops as fo  # noqa: E402

MIN_DENSITY = 0.3
DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'


def _api_names():
    return [k for k in dir(fo) if not k.startswith('_') and callable(getattr(fo, k))]


def _sandbox_ns():
    ns = {k: getattr(fo, k) for k in _api_names()}
    ns['np'] = np
    return ns


def prompt_for(n, history):
    api = ', '.join(_api_names())
    good = '\n'.join(f'  适应度 {h["fitness"]:+.4f}  {h["code"][:120]}'
                     for h in history[-5:] if h.get('fitness', PENALTY) > PENALTY) or '  （暂无）'
    return f"""你在为一个 **A股日内做T** 的因子挖掘项目提议候选因子。

## 硬约束
- 交易成本 **0.136%/往返**（买卖双边）。因子必须靶向**量级 ≥ 0.2%/笔**的机会，
  统计显著但量级小的请直接放弃。
- 样本：39 只 A 股 × 1 年 1min。**严格因果**：bar i 的函数值只能用到 ≤ i 的数据。

## 你的输出格式（严格遵守）
只输出一个 python 代码块，定义一个函数 `f(ctx)`：

```python
def f(ctx):
    # ctx 提供：t(时间标签list), o,h,l,c,v,amt, vwap, ret, hi_so_far, lo_so_far,
    #           prev_close, daily_atr, n
    # 可用算子：{api}
    # 返回：长度 == ctx['n'] 的 numpy 数组（因子值）
    return ...
```

## 可用的算子签名要点
- `ts_mean/ts_std/ts_sum/ts_max/ts_min(x, n)`、`ts_corr(x, y, n)`、`ts_rank(x, n)`
- `delay(x, n)`、`delta(x, n)`、`decay_linear(x, n)`、`ema(x, n)`
- `range_pos(ctx)`、`dist_high(ctx)`、`dist_low(ctx)`、`vwap_dev(ctx)`、
  `vol_ratio(ctx, n)`、`downvol_share(ctx, n)`、`realized_skew(ctx, n)`、
  `volume_quantile(ctx)`、`volume_concentration(ctx)`、`tail_vol_share(ctx)`、
  `smart_money_dev(ctx, n)`
- 逐元素：`abs_ neg log_ sqrt_ sign_ add sub mul div where_`

## 已测过、**不要重复提议**的（全部费后为负）
vwap_dev / range_pos / dist_high / dist_low / ema_dev / cpv / rv / rskew / downvol /
vol_q / vol_hhi / tail_vol / vol_ratio / smart_dev / mom30 / mom5 / rev1 / amp30 / decay_ret

## 历史（前几轮已提议过的，避免重复，可在此基础上变异）
{good}

请给出 **{n} 个互不相同**的候选，每个一个代码块，块前用一行 `### 因子名：<名字>` 标注。
"""
PENALTY = -10.0


def deepseek_propose(prompt, key) -> str:
    import requests
    r = requests.post(DEEPSEEK_URL,
                      headers={'Authorization': f'Bearer {key}',
                               'Content-Type': 'application/json'},
                      json={'model': 'deepseek-chat',
                            'messages': [{'role': 'user', 'content': prompt}],
                            'temperature': 1.0, 'stream': False},
                      timeout=180)
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']


def stub_propose(prompt, key=None) -> str:
    """桩提案器：不发网络请求，返回几条已知可跑通/不可跑通的式子，用于验证管线。"""
    return """### 因子名：stub_a
```python
def f(ctx):
    return ts_corr(ctx['ret'], ctx['v'], 15)
```
### 因子名：stub_b
```python
def f(ctx):
    return mul(sign_(ctx['ret']), volume_quantile(ctx))
```
### 因子名：stub_c
```python
def f(ctx):
    return div(1.0, add(abs_(vwap_dev(ctx)), 1e-6))   # 离谱但可跑
```
### 因子名：stub_broken
```python
def f(ctx):
    return undefined_symbol(ctx)   # 故意报错，验证异常被兜住
```
"""


def extract_blocks(text):
    """从模型输出里抽出 (名字, 代码)。"""
    out = []
    for m in re.finditer(r'###\s*因子名[:：]\s*(\S+)\s*```(?:python)?\s*(.*?)```', text, re.S):
        out.append((m.group(1), m.group(2).strip()))
    for m in re.finditer(r'```(?:python)?\s*(def f\(ctx\).*?)```', text, re.S):
        if not any(m.group(1).strip() == c for _n, c in out):
            out.append((f'anon{len(out)}', m.group(1).strip()))
    return out


def compile_fn(code):
    ns = _sandbox_ns()
    exec(compile(code, '<llm>', 'exec'), ns)
    fn = ns.get('f')
    if not callable(fn):
        raise ValueError('未定义 f(ctx)')
    return fn


def fitness_of(fn, panels):
    """对每票每日求值 → 复用 eval_factor 的机器 → 费后净均（取双向更优）。"""
    best, tot_days = PENALTY, 0
    per = {}
    for c, p in panels.items():
        keep, labels, L, _n = E.aligned_matrix(p)
        if not keep:
            continue
        M = np.full((len(keep), L), np.nan)
        for r, d in enumerate(keep):
            try:
                v = np.asarray(fn(p['days'][d]), float).ravel()
            except Exception:
                return PENALTY
            if v.shape[0] == L:
                M[r] = v
        per[c] = (p, keep, labels, L, M)
        tot_days += len(keep)
    if not per:
        return PENALTY
    for sign in (1, -1):
        legs = []
        for c, (p, keep, labels, L, M) in per.items():
            legs.extend(E.legs_hold_matrix(p, M, sign, keep, labels, L))
        if not legs or len(legs) / max(tot_days, 1) < MIN_DENSITY:
            continue
        net = float(np.mean([x['net'] for x in legs]))
        if net > best:
            best = net
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codes', default=None)
    ap.add_argument('--rounds', type=int, default=3)
    ap.add_argument('--n-propose', type=int, default=6)
    ap.add_argument('--stub', action='store_true', help='用桩提案器（无 key 时验证管线）')
    ap.add_argument('--out', default=os.path.join(HERE, 'llm_ledger.jsonl'))
    args = ap.parse_args()

    key = os.environ.get('DEEPSEEK_API_KEY')
    if not args.stub and not key:
        print('[llm] 未设 DEEPSEEK_API_KEY。用 --stub 可验证管线（不发网络请求）。')
        return
    codes = args.codes.split(',') if args.codes else sorted(
        {os.path.basename(f).replace('_1year_1min.csv', '').split('.')[0]
         for f in glob.glob(os.path.join(E.v2.CSV_DIR, '*_1year_1min.csv'))})[:8]
    E.v2.END = E.WIN_END
    panels = {}
    for c in codes:
        p = E.load_code(c)
        if p:
            panels[c] = p
    print(f'[llm] codes={len(panels)} 提案器={"stub" if args.stub else "deepseek-chat"} '
          f'轮数={args.rounds} 每轮={args.n_propose}')

    history, t0 = [], time.time()
    seen = set()
    fh = open(args.out, 'a', encoding='utf-8')
    for rnd in range(args.rounds):
        prompt = prompt_for(args.n_propose, history)
        try:
            text = stub_propose(prompt) if args.stub else deepseek_propose(prompt, key)
        except Exception as e:
            print(f'  [warn] 提案失败: {type(e).__name__} {e}')
            break
        blocks = extract_blocks(text)
        print(f'\n--- 第 {rnd + 1} 轮：提案 {len(blocks)} 条 ---')
        for name, code in blocks:
            if code in seen:
                continue
            seen.add(code)
            try:
                fn = compile_fn(code)
                fit = fitness_of(fn, panels)
                err = None
            except Exception as e:
                fit, err = PENALTY, f'{type(e).__name__}: {e}'
            rec = {'round': rnd + 1, 'name': name, 'code': code,
                   'fitness': round(float(fit), 4), 'error': err}
            history.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
            fh.flush()
            if err:
                tag = '编译/运行报错'
            elif fit <= PENALTY:
                tag = '拒：信号密度<0.3'      # 非报错——被 MIN_DENSITY 闸门拦下
            elif fit >= 0.2:
                tag = '过闸门'
            else:
                tag = '低于闸门'
            print(f'   {name:16} 适应度 {fit:+.4f}  {tag}' + (f'  [{err}]' if err else ''))
    fh.close()
    best = max(history, key=lambda h: h['fitness']) if history else None
    if best:
        print(f'\n[llm] 最优: {best["name"]} 适应度 {best["fitness"]:+.4f}')
        print(f'[llm] 代码:\n{best["code"]}')
    print(f'[llm] 用时 {time.time() - t0:.0f}s → {args.out}')


if __name__ == '__main__':
    main()
