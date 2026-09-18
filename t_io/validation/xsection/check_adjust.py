# -*- coding: utf-8 -*-
"""截面面板复权口径生死核查（因子挖掘 P1-1）。

## 为什么必须核查

fetch_panel.py 拉取日线面板时**未显式传 adjust 参数**，依赖 gm SDK history_n 的
默认值。panel/（5674 只含退市）是因子挖掘的底座，若口径不是前复权，所有涉及
价格水平的因子（均线、乖离、动量、波动）在除权日会出现**伪信号**——这是生死问题。

## 方法（三方对比）

选 5 只有确定分红除权事件的大盘蓝筹：
  SHSE.600519 贵州茅台 / SHSE.601318 中国平安 / SZSE.000001 平安银行
  SHSE.600036 招商银行 / SHSE.601398 工商银行
1) 从 panel/shards 读出每只在面板内的完整价格序列；
2) 用 GM SDK 按 ADJUST_PREV（前复权）与 ADJUST_NONE（不复权）各拉一次同区间日线
   （若两者都不匹配，追加 ADJUST_POST 后复权做鉴别诊断）；
3) 按交易日对齐，计算面板与两种口径 close 的相对偏差匹配率（容差 0.5%，
   覆盖 0.01 元报价精度对低价股的影响）；
4) 同时从不复权序列自动检测除权事件（主板 ±10% 涨跌幅限制下，收盘对收盘
   跌幅超过 -11% 只能是除权除息），打印事件窗口 ±2 日的三方价格作证据。

判定：匹配率 ≥95% 者即为面板实际口径。

## 运行环境

必须在用户 python 3.11 下运行（gm SDK 只装在该环境，且该环境有 pyarrow）：
  "C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" check_adjust.py
代码仅用 pandas/numpy 标准能力（Spearman 等用 rank+corr 实现的纪律同样适用于
后续因子代码），不依赖 scipy。

输出：控制台结论 + check_adjust_evidence.md（同目录，可审计证据留档）。
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHARDS = os.path.join(HERE, 'panel', 'shards')
EVIDENCE_MD = os.path.join(HERE, 'check_adjust_evidence.md')
FIELDS = 'symbol,eob,open,high,low,close,volume,amount'

TARGETS = [
    ('SHSE.600519', '贵州茅台'),
    ('SHSE.601318', '中国平安'),
    ('SZSE.000001', '平安银行'),
    ('SHSE.600036', '招商银行'),
    ('SHSE.601398', '工商银行'),
]

TOL = 0.005          # 匹配容差 0.5%（覆盖 0.01 元报价精度）
PASS_FRAC = 0.95     # 匹配率判定阈值
EVENT_DIV = -0.005   # 除权事件检测：不复权日收益 - 前复权日收益 < -0.5%
                     # （两种口径只在除权日发生收益背离，背离幅度≈股息率）


def load_panel(symbols):
    """从 12 个分片中抽出目标 symbol 的面板序列（只读需要的列）。"""
    frames = []
    for fn in sorted(os.listdir(SHARDS)):
        if not fn.endswith('.parquet'):
            continue
        df = pd.read_parquet(os.path.join(SHARDS, fn),
                             columns=['symbol', 'eob', 'open', 'close', 'volume', 'amount'])
        df = df[df['symbol'].isin(symbols)]
        if len(df):
            frames.append(df)
    panel = pd.concat(frames, ignore_index=True)
    panel['date'] = pd.to_datetime(panel['eob']).dt.strftime('%Y-%m-%d')
    return panel.sort_values(['symbol', 'date']).reset_index(drop=True)


def gm_pull(history, sym, start, end, adjust, label):
    df = history(symbol=sym, frequency='1d', start_time=start, end_time=end,
                 fields=FIELDS, adjust=adjust, df=True)
    if df is None or not len(df):
        raise RuntimeError(f'GM {label} 拉取为空: {sym}')
    df = df.copy()
    df['date'] = pd.to_datetime(df['eob']).dt.strftime('%Y-%m-%d')
    return df[['date', 'open', 'close']].rename(
        columns={'open': f'open_{label}', 'close': f'close_{label}'})


def match_rate(merged, label):
    diff = (merged['close'] - merged[f'close_{label}']).abs() / merged[f'close_{label}']
    return float((diff <= TOL).mean()), float(diff.max()), float(diff.median())


def main():
    from core.market_data.gm_token import load_token
    from gm.api import set_token, history, ADJUST_PREV, ADJUST_NONE, ADJUST_POST
    set_token(load_token())

    syms = [s for s, _ in TARGETS]
    names = dict(TARGETS)
    print(f'[check] 读取面板分片 → {SHARDS}', flush=True)
    panel = load_panel(syms)
    print(f'[check] 面板命中 {panel["symbol"].nunique()}/{len(syms)} 只，'
          f'{len(panel)} 行', flush=True)

    lines, verdicts = [], {}
    for sym in syms:
        p = panel[panel['symbol'] == sym].reset_index(drop=True)
        start, end = p['date'].min(), p['date'].max()
        gm_none = gm_pull(history, sym, start, end, ADJUST_NONE, 'none')
        gm_prev = gm_pull(history, sym, start, end, ADJUST_PREV, 'prev')
        m = p.merge(gm_none, on='date').merge(gm_prev, on='date')
        n = len(m)
        fr_none, mx_none, md_none = match_rate(m, 'none')
        fr_prev, mx_prev, md_prev = match_rate(m, 'prev')

        # 鉴别诊断：两种口径都不匹配时才拉后复权
        fr_post = None
        if max(fr_none, fr_prev) < PASS_FRAC:
            gm_post = gm_pull(history, sym, start, end, ADJUST_POST, 'post')
            m = m.merge(gm_post, on='date')
            fr_post, mx_post, md_post = match_rate(m, 'post')

        cand = {'none': fr_none, 'prev': fr_prev}
        if fr_post is not None:
            cand['post'] = fr_post
        best = max(cand, key=cand.get)
        verdict = best if cand[best] >= PASS_FRAC else 'UNKNOWN'
        verdicts[sym] = verdict

        print(f'\n===== {sym} {names[sym]} =====', flush=True)
        print(f'  区间 {start} ~ {end}，对齐交易日 {n} 天', flush=True)
        print(f'  vs 不复权(NONE): 匹配率 {fr_none:.2%}  中位偏差 {md_none:.4%}  最大偏差 {mx_none:.4%}', flush=True)
        print(f'  vs 前复权(PREV): 匹配率 {fr_prev:.2%}  中位偏差 {md_prev:.4%}  最大偏差 {mx_prev:.4%}', flush=True)
        if fr_post is not None:
            print(f'  vs 后复权(POST): 匹配率 {fr_post:.2%}  中位偏差 {md_post:.4%}  最大偏差 {mx_post:.4%}', flush=True)
        print(f'  → 判定: {verdict}', flush=True)

        # 除权事件自动检测：不复权与前复权序列的日收益只在除权日背离
        ret_none = m['close_none'].pct_change()
        ret_prev = m['close_prev'].pct_change()
        div_gap = ret_none - ret_prev
        ev_idx = m.index[div_gap < EVENT_DIV].tolist()
        lines.append(f'## {sym} {names[sym]}（{start} ~ {end}，{n} 个交易日）\n')
        lines.append(f'- vs 不复权：匹配率 {fr_none:.2%}，中位偏差 {md_none:.4%}，最大偏差 {mx_none:.4%}')
        lines.append(f'- vs 前复权：匹配率 {fr_prev:.2%}，中位偏差 {md_prev:.4%}，最大偏差 {mx_prev:.4%}')
        if fr_post is not None:
            lines.append(f'- vs 后复权：匹配率 {fr_post:.2%}，中位偏差 {md_post:.4%}，最大偏差 {mx_post:.4%}')
        lines.append(f'- **判定：{verdict}**')
        lines.append(f'- 自动检测到除权事件 {len(ev_idx)} 次（收益背离法：不复权收益−前复权收益 < {EVENT_DIV:.1%}）\n')
        for i in ev_idx[-4:]:  # 每只留最近 4 个事件窗口
            ev_date = m.loc[i, 'date']
            lines.append(f'### 除权事件窗口 @{ev_date}（当日收益背离 {div_gap.iloc[i]:.2%}≈股息率）\n')
            lines.append('| date | 面板close | 不复权close | 前复权close | 不复权日收益 | 前复权日收益 |')
            lines.append('|---|---|---|---|---|---|')
            for j in range(max(0, i - 2), min(n, i + 3)):
                rn, rp = ret_none.iloc[j], ret_prev.iloc[j]
                lines.append(f'| {m.loc[j, "date"]} | {m.loc[j, "close"]:.2f} | '
                             f'{m.loc[j, "close_none"]:.2f} | {m.loc[j, "close_prev"]:.2f} | '
                             f'{"" if pd.isna(rn) else f"{rn:.2%}"} | '
                             f'{"" if pd.isna(rp) else f"{rp:.2%}"} |')
            lines.append('')
        time.sleep(0.2)

    all_same = len(set(verdicts.values())) == 1
    final = list(verdicts.values())[0] if all_same else 'INCONSISTENT'
    label_map = {'prev': '前复权', 'none': '不复权', 'post': '后复权',
                 'UNKNOWN': '无法判定', 'INCONSISTENT': '各标的口径不一致（异常）'}
    print('\n========================================', flush=True)
    print(f'[最终判定] 面板复权口径 = {final}（{label_map.get(final, final)}）', flush=True)
    for sym in syms:
        print(f'  {sym} {names[sym]}: {verdicts[sym]}', flush=True)

    header = [
        '# 面板复权口径核查证据（check_adjust.py 自动生成）',
        '',
        f'- 核查标的：{len(syms)} 只大盘蓝筹（有确定分红除权事件）',
        f'- 匹配容差：{TOL:.1%}，判定阈值：匹配率 ≥ {PASS_FRAC:.0%}',
        f'- 除权事件检测：不复权日收益 − 前复权日收益 < {EVENT_DIV:.1%}（两种口径只在除权日背离，背离≈股息率）',
        '',
        f'**最终判定：面板口径 = {final}（{label_map.get(final, final)}）**',
        '',
    ]
    with open(EVIDENCE_MD, 'w', encoding='utf-8') as f:
        f.write('\n'.join(header + lines))
    print(f'[check] 证据已留档 → {EVIDENCE_MD}', flush=True)


if __name__ == '__main__':
    main()
