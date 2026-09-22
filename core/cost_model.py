# -*- coding: utf-8 -*-
"""交易成本单一真源 —— 全仓实验与生产共用（2026-09-22 建）。

## 费率口径（owner 实际费率）

    佣金    万0.854 = 0.00854%（单边）；单笔 ≥10 万时 5 元最低佣金不生效
    印花税  0.05%（仅卖出；2023-08-28 起，ETF 免征）
    过户费  0.001%（双边）

往返：股票 **0.06908%**　ETF **0.01908%**

⚠️ 5 元最低佣金边界：单笔 < 58,548 元时佣金按 5 元实收，等效单边
   5/金额。3 万元/笔 ⇒ 往返 ≈ 0.085%；1 万元/笔 ⇒ 往返 ≈ 0.1%。
   本模块不建模该情形（owner 单笔 ≥10 万），引用低成本结论时须声明单笔规模。

## 历史口径

`LEGACY = (0.00121, 0.00015)` 即往返 0.136%，内含 **2023 年前已废止的
0.1% 印花税** 与万1.5 佣金假设。全仓 2026-09-22 之前的所有做T实验（含
P1/GP/LLM 三臂 79 条因子、B7 全管线）都用这一口径 —— **仅供回归对照，
勿用于新实验**。

## 环境变量

`ST_COST_VENUE` ∈ {stock, etf, legacy} 覆盖默认 venue，用于回归对照与
成本敏感性分析，无需改代码。
"""
from __future__ import annotations

import os

COMMISSION = 0.0000854      # 万0.854，单边
STAMP_TAX_SELL = 0.0005     # 0.05%，仅股票卖出
TRANSFER_FEE = 0.00001      # 0.001%，双边

FEE_BUY_STOCK = COMMISSION + TRANSFER_FEE                      # 0.0000954
FEE_SELL_STOCK = COMMISSION + TRANSFER_FEE + STAMP_TAX_SELL    # 0.0005954
FEE_BUY_ETF = COMMISSION + TRANSFER_FEE                        # 0.0000954
FEE_SELL_ETF = COMMISSION + TRANSFER_FEE                       # 0.0000954

LEGACY_FEE_S, LEGACY_FEE_B = 0.00121, 0.00015                  # 往返 0.136%（勿用）

ROUND_TRIP = {
    'stock': FEE_BUY_STOCK + FEE_SELL_STOCK,     # 0.0006908
    'etf': FEE_BUY_ETF + FEE_SELL_ETF,           # 0.0001908
    'legacy': LEGACY_FEE_S + LEGACY_FEE_B,       # 0.00136
}

VENUES = tuple(ROUND_TRIP)


def venue() -> str:
    """当前 venue（环境变量 ST_COST_VENUE 覆盖，非法值回落 stock）。"""
    v = os.environ.get('ST_COST_VENUE', 'stock').strip().lower()
    return v if v in VENUES else 'stock'


def fees(venue_: str | None = None) -> tuple[float, float]:
    """返回 (fee_sell, fee_buy) —— 与 run_experiment 的 FEE_S/FEE_B 同序。"""
    v = venue_ or venue()
    if v == 'etf':
        return FEE_SELL_ETF, FEE_BUY_ETF
    if v == 'legacy':
        return LEGACY_FEE_S, LEGACY_FEE_B
    return FEE_SELL_STOCK, FEE_BUY_STOCK


def leg_pnl(direction: str, fill: float, out: float,
            fee_s: float | None = None, fee_b: float | None = None) -> float:
    """单条当日闭合往返腿的费后收益率（单位：%）。

    与 `run_experiment._leg_pnl` 同构（保持乘法形式，不改既有口径的数学形状）：
        正T(long)  买现金→卖：净 = (卖×(1−费卖) − 买×(1+费买)) / 买
        反T(short) 卖底仓→买回：净 = (卖×(1−费卖) − 买回×(1+费买)) / 卖
    """
    if fee_s is None or fee_b is None:
        fs, fb = fees()
        fee_s = fs if fee_s is None else fee_s
        fee_b = fb if fee_b is None else fee_b
    if direction == 'long':
        return 100.0 * (out * (1 - fee_s) - fill * (1 + fee_b)) / fill
    return 100.0 * (fill * (1 - fee_s) - out * (1 + fee_b)) / fill


def round_trip(venue_: str | None = None) -> float:
    """往返成本（小数），用于量级闸门与捕获率保本线的换算。"""
    return ROUND_TRIP[venue_ or venue()]


if __name__ == '__main__':
    for v in VENUES:
        fs, fb = fees(v)
        print(f'{v:8s} 买 {fb*100:.5f}%  卖 {fs*100:.5f}%  往返 {ROUND_TRIP[v]*100:.5f}%')
