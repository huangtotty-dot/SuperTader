# -*- coding: utf-8 -*-
"""ic_layer 配套测试（脚本式断言，直接 python 运行，不依赖 pytest）。

覆盖：
1. 无未来函数对齐性：完美预知因子（value = 真实前瞻收益）应得 RankIC≈1；
   若错位一天，IC 应≈0 —— 这是前瞻收益口径的硬验证。
2. min_coverage 跳过逻辑。
3. symbol 归一化（SHSE.600519 / 600519.SH -> 600519）。
4. 已知 IC 回收、噪声因子 MC 不过闸、decile 单调性（与 __main__ 自测互补）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from t_io.validation.factor_mining.ic_layer import (
    _make_synthetic, _norm_symbol, decile_analysis, evaluate_factor, mc_baseline,
)

sys.stdout.reconfigure(encoding="utf-8")


def _toy_panel(n_symbols=50, n_days=60, seed=3):
    """收益完全已知的玩具面板：r[d] = open(d)/open(d-1)-1 预存。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-06", periods=n_days)
    r = 0.01 * rng.standard_normal((n_days, n_symbols))
    open_px = 20.0 * np.exp(np.cumsum(r, axis=0))
    rows = []
    for j in range(n_symbols):
        rows.append(pd.DataFrame({
            "symbol": f"{600000 + j}", "date": dates, "open": open_px[:, j],
            "high": open_px[:, j] * 1.01, "low": open_px[:, j] * 0.99,
            "close": open_px[:, j] * 1.002,
            "volume": 1000000, "amount": 2e7,
        }))
    return pd.concat(rows, ignore_index=True), dates, r


def test_alignment_no_lookahead():
    """完美预知因子：value(t) = open(t+2)/open(t+1)-1 应回收 IC≈1。"""
    panel, dates, r = _toy_panel()
    n_days, n_sym = r.shape
    recs = []
    for t in range(n_days - 2):
        fwd = np.exp(r[t + 2]) - 1.0  # open(t+2)/open(t+1)-1
        for j in range(n_sym):
            recs.append((dates[t], f"{600000 + j}", fwd[j]))
    oracle = pd.DataFrame(recs, columns=["date", "symbol", "value"])
    ev = evaluate_factor(oracle, panel, horizons=(1,), min_coverage=10)
    ic = ev[1]["buy_open"]["rank_ic_mean"]
    assert ic > 0.99, f"完美预知因子 IC 应≈1，实际 {ic:.4f}（对齐口径有误！）"
    # 错位一天：value(t) = open(t+3)/open(t+2)-1，与 h=1 前瞻无关，IC 应≈0
    recs2 = []
    for t in range(n_days - 3):
        fwd = np.exp(r[t + 3]) - 1.0
        for j in range(n_sym):
            recs2.append((dates[t], f"{600000 + j}", fwd[j]))
    shifted = pd.DataFrame(recs2, columns=["date", "symbol", "value"])
    ic2 = evaluate_factor(shifted, panel, horizons=(1,), min_coverage=10)[1][
        "buy_open"]["rank_ic_mean"]
    assert abs(ic2) < 0.15, f"错位一天因子 IC 应≈0，实际 {ic2:.4f}"
    print(f"[pass] 对齐性: oracle IC={ic:.4f}, 错位 IC={ic2:.4f}")


def test_min_coverage():
    """当日覆盖 < min_coverage 的日期必须被跳过。"""
    panel, dates, r = _toy_panel(n_symbols=50, n_days=40)
    rng = np.random.default_rng(1)
    recs = []
    for t in range(38):
        keep = 5 if t < 20 else 50  # 前 20 日只有 5 只，后 20 日 50 只
        for j in range(keep):
            recs.append((dates[t], f"{600000 + j}", rng.standard_normal()))
    f = pd.DataFrame(recs, columns=["date", "symbol", "value"])
    ev = evaluate_factor(f, panel, horizons=(1,), min_coverage=30)
    n_days = ev[1]["buy_open"]["n_days"]
    assert n_days <= 20, f"低覆盖日期未被跳过: n_days={n_days}"
    cov = ev[1]["buy_open"]["coverage_mean"]
    assert cov == 50, f"保留日覆盖应为 50，实际 {cov}"
    print(f"[pass] min_coverage: n_days={n_days}, coverage_mean={cov}")


def test_symbol_norm():
    s = pd.Series(["SHSE.600519", "SZSE.000001", "600519.SH", "300750"])
    out = _norm_symbol(s).tolist()
    assert out == ["600519", "000001", "600519", "300750"], out
    print(f"[pass] symbol 归一化: {out}")


def test_synthetic_recovery():
    panel, known, noise, ic_target = _make_synthetic()
    ev = evaluate_factor(known, panel, horizons=(1,))
    est = ev[1]["buy_open"]["rank_ic_mean"]
    assert abs(est - ic_target) / ic_target < 0.20, f"IC 回收误差过大: {est:.4f}"
    mc_noise = mc_baseline(noise, panel, horizon=1, n=100, seed=42)
    assert not mc_noise["pass"], "纯噪声因子不应过 MC 闸"
    mc_known = mc_baseline(known, panel, horizon=1, n=100, seed=42)
    assert mc_known["pass"], "预埋因子应过 MC 闸"
    dec = decile_analysis(known, panel, horizon=1, n_groups=10)
    assert dec["monotonicity"] > 0.5, f"单调性方向错误: {dec['monotonicity']:.3f}"
    assert dec["group_ret"].shape[1] == 10
    assert dec["long_short"].iloc[-1] > 1.0, "多空净值应>1"
    print(f"[pass] 合成回收: IC={est:.4f}(目标{ic_target}), "
          f"噪声mc_rank={mc_noise['mc_rank']:.2f}, 单调性={dec['monotonicity']:.3f}")


if __name__ == "__main__":
    test_alignment_no_lookahead()
    test_min_coverage()
    test_symbol_norm()
    test_synthetic_recovery()
    print("== ic_layer 全部测试通过 ==")
