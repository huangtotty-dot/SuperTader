# -*- coding: utf-8 -*-
"""弱转强 · 盘中选股 决策核（**纯函数，无 IO**）—— 2026-10-10。

信号（冻结，依据 `t_io/validation/weak_strong/exp5~6` 定稿，两段样本均成立）:

    weak_W1(D−1):  prev_close < MA20                    日线超跌（安全边际=便宜）
    strong10(D) :  bar0.low  >= prev_close              跳空高开、缺口未回补
                 AND bar0.close >  prev_close           站上昨收
                 AND bar0.close >  bar0.amount/bar0.volume   站上分时均价(VWAP)
    gap          =  bar0.close / prev_close − 1         （入场=10:00 收盘，无前视）

    排序：gap 降序 → top-N 等权（exp9 实测 N=4 是 Sharpe 顶点，净年化 +113.6%/Sharpe 2.38）。
    板块过滤（exp12/13，可选）：只在 allowed_boards 里选（owner「猎手靠前板块」输入）。

**GM 8棒/天口径**：bar0 = 10:00 棒（覆盖 09:30~10:00 整窗）。tushare 9棒口径下的
`minlow2 = min(09:30瞬时棒.low, 10:00棒.low)` 恰好等于 GM 的 `bar0.low`（10:00 棒窗口
已含 09:30）；`vwap2` 等价 `bar0.amount/bar0.volume`（09:30 竞价量额可忽略）。故本核的
bar0 语义 =「09:30~10:00 那一根」，两种数据源同构。

## 纪律（勿破坏）
- 本模块**不做任何 IO**：不读文件、不下单、不写持仓。
- **fail-closed**：任一票缺 prev_close/ma20/bar 关键价 ⇒ 该票不进候选、不产生下单；
  `min_pool` 之下或 top_n≤0 ⇒ 返回空 picks（宁可不买）。
- 改动常量/信号口径 = 换策略，须重做 exp 系列检验。
"""
from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

# ── 信号常量（冻结）──
MIN_POOL = 3                # 有效候选下限，低于则 fail-closed 不下单
DEFAULT_TOP_N = 4           # 每日按 gap 取最强前 4（exp9 Sharpe 顶点）

NO_TRADE = 'no_trade'
TRADE = 'trade'


def _valid(x) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(v) and v > 0)


def _bar_field(bar, key: str):
    if not isinstance(bar, Mapping):
        return float('nan')
    v = bar.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float('nan')


def signal_ok(prev_close: float, ma20: float, bar: Mapping[str, float]) -> tuple[bool, str, float]:
    """单票弱转强判定。返回 (是否触发, 原因, gap)。任一口径缺失 → (False, 原因, nan)。"""
    if not _valid(prev_close):
        return False, 'prev_close_missing', float('nan')
    low = _bar_field(bar, 'low')
    close = _bar_field(bar, 'close')
    amount = _bar_field(bar, 'amount')
    volume = _bar_field(bar, 'volume')
    if not (_valid(low) and _valid(close)):
        return False, 'bar_missing', float('nan')
    vwap = amount / volume if (_valid(amount) and _valid(volume)) else float('nan')
    weak = _valid(ma20) and (prev_close < ma20)
    if not weak:
        return False, 'not_weak(prev_close>=ma20)', float('nan')
    if not (low >= prev_close):
        return False, 'gap_not_held(low<prev_close)', float('nan')
    if not (close > prev_close):
        return False, 'not_above_prev_close', float('nan')
    if not (_valid(vwap) and close > vwap):
        return False, 'not_above_vwap', float('nan')
    return True, 'ok', float(close / prev_close - 1.0)


def evaluate(prev_close: Mapping[str, float],
             ma20: Mapping[str, float],
             bars: Mapping[str, Mapping[str, float]],
             board: Mapping[str, str] | None = None,
             allowed_boards: Sequence[str] | None = None,
             top_n: int = DEFAULT_TOP_N) -> dict:
    """池级评估（**这是给自动盘调的入口**）。

    参数
        prev_close      : {code: D−1 收盘}
        ma20            : {code: D−1 的 20 日均线}（weak_W1 判据）
        bars            : {code: {low, close, amount, volume}} —— 09:30~10:00 那根棒
        board           : {code: 板块名}（缺省=不启用板块过滤）
        allowed_boards  : 允许的板块集合（缺省/空=None=不过滤；非 None 则只在这些板块里选）
        top_n           : 每日按 gap 降序取前 N（≤0 ⇒ 不下单）

    返回
        {
          'pool_n': int,           # 有完整输入(prev_close+bar)的票数
          'signals': [...],        # 全部触发信号（含被板块过滤掉的），gap 降序
          'picks':   [...],        # 过滤后 top-N（TRADE 决策）
          'rejected_board': int,   # 被板块过滤丢掉的信号数
        }
    """
    allow = None if (allowed_boards is None or len(allowed_boards) == 0) else set(allowed_boards)
    board = board or {}

    signals = []
    pool_n = 0
    for code, pc in prev_close.items():
        bar = bars.get(code)
        if not (_valid(pc) and isinstance(bar, Mapping)):
            continue
        pool_n += 1
        ok, why, gap = signal_ok(float(pc), float(ma20.get(code) or float('nan')), bar)
        if not ok:
            continue
        b = str(board.get(code) or '')
        if allow is not None and b not in allow:
            signals.append({'code': code, 'gap': gap, 'board': b,
                            'decision': NO_TRADE, 'reason': 'board_filtered'})
            continue
        signals.append({'code': code, 'gap': gap, 'board': b,
                        'decision': TRADE, 'reason': why})

    signals.sort(key=lambda s: (-(s['gap'] if np.isfinite(s['gap']) else -1e9), s['code']))
    tradable = [s for s in signals if s['decision'] == TRADE]
    picks = tradable[:max(0, int(top_n))] if pool_n >= MIN_POOL else []
    return {
        'pool_n': pool_n,
        'signals': signals,
        'picks': picks,
        'rejected_board': sum(1 for s in signals if s['decision'] == NO_TRADE),
        'n_tradable': len(tradable),
    }
