# -*- coding: utf-8 -*-
"""开盘低开反转 · 日内做T 决策核（**纯函数，无 IO**）—— 2026-09-22。

规则（冻结；依据 `doc/solutions/2026-09-22_开盘低开反转日内T_设计规格.md` §1）:

    mkt_gap(T) < 0      且      rel_i(T) ≤ −1.0%
    gap_i(T) = open_i(T) / prev_close_i(T−1) − 1
    mkt_gap(T) = 池内 gap 的中位数
    rel_i(T) = gap_i(T) − mkt_gap(T)
    动作：T 日 09:30 买 → 10:00 卖（30 分钟持仓）

证据：样本外 2019-01~2025-03 +0.5245%/腿（t=8.07）；独立 400 只池 +0.5275%（比 1.01）；
退市股子集更强（+0.7447%）；市场代理用**随机 10 只**即可（Stage14）。

## 纪律（勿破坏）
- 本模块**不做任何 IO**：不读文件、不下单、**不写持仓**。
  `holdings.json` 的唯一合法写入口是 `src/holdings_repo.py`（规格 §5.2）；
  T 腿由调用方自行记账（原 Renko 做T引擎与其 t_entry_price 内存态已于 2026-09-22 删除）。
- 卖出侧必须挂 `T_LEG_CLOSE`，且**不得**走 `awaiting_buyback`（那是先卖后买）。
- `min_pool` 之下**fail-closed 返回 NO-TRADE**：池子太薄时中位数不可靠，
  宁可不动（Stage14 实测 10 只已足够，此处取更保守的 5）。
"""
from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

# ── 规则常量（冻结；改动等于换策略，须重做预注册检验）──
REL_THRESHOLD = -0.010      # rel ≤ −1.0%
MKT_MUST_BE_NEGATIVE = True
MIN_POOL = 5                # 池内有效样本下限，低于则 NO-TRADE（fail-closed）

NO_TRADE = 'no_trade'
TRADE = 'trade'


def _valid(x) -> bool:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return bool(np.isfinite(v) and v > 0)


def compute_gaps(prev_close: Mapping[str, float],
                 open_px: Mapping[str, float]) -> dict[str, float]:
    """逐票 gap = open/prev_close − 1。任一价缺失/非正 ⇒ 该票**不进结果、也不进中位数**。"""
    out: dict[str, float] = {}
    for code, op in open_px.items():
        pc = prev_close.get(code)
        if _valid(op) and _valid(pc):
            out[code] = float(op) / float(pc) - 1.0
    return out


def market_gap(gaps: Mapping[str, float]) -> float | None:
    """池内 gap 中位数；有效样本 < MIN_POOL ⇒ None（调用方据此 NO-TRADE）。"""
    vals = [g for g in gaps.values() if np.isfinite(g)]
    if len(vals) < MIN_POOL:
        return None
    return float(np.median(vals))


def decide_one(gap: float, mg: float) -> tuple[bool, str]:
    """单票判定。返回 (是否出手, 原因)。"""
    if mg is None or not np.isfinite(mg):
        return False, 'pool_too_thin_or_no_median'
    if MKT_MUST_BE_NEGATIVE and not (mg < 0):
        return False, f'mkt_gap>=0 ({mg:+.4%})'
    if not np.isfinite(gap):
        return False, 'gap_invalid'
    rel = gap - mg
    if rel <= REL_THRESHOLD:
        return True, f'rel={rel:+.4%} <= {REL_THRESHOLD:+.2%}'
    return False, f'rel={rel:+.4%} > {REL_THRESHOLD:+.2%}'


def evaluate(prev_close: Mapping[str, float],
             open_px: Mapping[str, float],
             codes: Sequence[str] | None = None,
             median_codes: Sequence[str] | None = None) -> dict:
    """池级评估（**这是给自动盘调的入口**）。

    参数
        prev_close   : {code: 上一交易日收盘价}
        open_px      : {code: T 日开盘价（09:25 竞价价 = 09:30 开盘价）}
        codes        : 关注名单（默认 = open_px 的键）；只对该名单产 decision
        median_codes : **取 mkt_gap 中位数的名单**（默认 = open_px 的键）。
                       ⚠️ 应与 `codes` 分开传：生产只订阅自己那几十只，而「大盘低开」的中位数
                       若用**自己的池**算就是**自指**（2026-09-24 实测：与 981 面板中位在 31 天里
                       9 天符号相反，腿集交集仅 36/141）。传一个**独立的市场代理池**即可落地
                       （Stage14：随机 10 只即够；Stage18 冻死 L20）。

    返回
        {
          'mkt_gap': float|None,          # None ⇒ 池太薄/无有效样本
          'pool_n':   int,                # 进中位数的有效票数
          'pool_gaps': {code: gap},
          'decisions': [{code, gap, rel, decision, reason}, ...]   # 仅 codes 内
          'tradable': [code, ...],        # decision == 'trade' 的 code
        }
    """
    gaps = compute_gaps(prev_close, open_px)
    if median_codes is None:
        mgap = gaps
    else:
        _keep = set(median_codes)
        mgap = {c: g for c, g in gaps.items() if c in _keep}
    mg = market_gap(mgap)
    watch = list(codes) if codes is not None else list(open_px)
    decisions = []
    for code in watch:
        gap = gaps.get(code, float('nan'))
        ok, why = decide_one(gap, mg)
        decisions.append({
            'code': code,
            'gap': None if not np.isfinite(gap) else float(gap),
            'rel': None if (mg is None or not np.isfinite(gap)) else float(gap - mg),
            'decision': TRADE if ok else NO_TRADE,
            'reason': why,
        })
    return {
        'mkt_gap': None if mg is None else float(mg),
        'pool_n': len(mgap),        # = **进中位数**的有效票数（不是全部 gap 数）
        'pool_gaps': gaps,
        'decisions': decisions,
        'tradable': [d['code'] for d in decisions if d['decision'] == TRADE],
    }


def size_cap_by_auction(auction_amount: float, participation: float = 0.10) -> float:
    """单腿规模上限 = 参与率 × 该票**集合竞价成交额**（规格 §7 / 容量表）。

    ⚠️ 约束是竞价量而非 ADV：竞价只占全日成交额中位 ~1.30%。
    调用方须自行取该票当日的竞价成交额（本模块不做 IO）。
    """
    if not _valid(auction_amount):
        return 0.0
    return float(auction_amount) * float(participation)
