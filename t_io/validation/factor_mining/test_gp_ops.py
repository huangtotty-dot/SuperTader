# -*- coding: utf-8 -*-
"""gp_ops 配套测试（脚本式断言，直接 python 运行，不依赖 pytest）。

覆盖（任务 S2-3 验收口径）：
1. 数值断言：合成序列手算对照，覆盖 16 个算子（≥8 达标）；
2. 复用一致性：包装算子在随机序列上与 factor_ops 参考实现逐值相等；
3. 前视检测（shift 不变性）：序列尾部追加 20 根，前 N 行输出不变
   （cs_rank 为横截面算子，不参与时序不变性测试，单独说明）；
4. NaN / 除零安全：窗内含 NaN -> NaN、分母为零 -> NaN、出口无 inf；
5. build_function_set：窗口展开、arity、签名兼容（(array,...)->array）。
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from t_io.validation.factor_mining import factor_ops as fo
from t_io.validation.factor_mining import gp_ops as gp

sys.stdout.reconfigure(encoding="utf-8")

TOL = 1e-12
_passed = 0


def _ok(cond, msg):
    global _passed
    assert cond, f"FAIL: {msg}"
    _passed += 1
    print(f"  ✓ {msg}")


def _close(a, b, msg, tol=TOL):
    """逐值相等（NaN 对 NaN 视为相等）。"""
    a, b = np.asarray(a, float), np.asarray(b, float)
    same = (np.isnan(a) & np.isnan(b)) | np.isclose(a, b, atol=tol, rtol=1e-9, equal_nan=True)
    _ok(bool(np.all(same)) and a.shape == b.shape, msg)


# ── 1. 数值断言（手算对照）─────────────────────────────────────────────────
print("── 1. 数值断言（手算对照）")

x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
_close(gp.ts_mean(x, 3), [np.nan, np.nan, 2.0, 3.0, 4.0], "ts_mean: 窗口3均值")
_close(gp.ts_std(x, 3), [np.nan, np.nan, 1.0, 1.0, 1.0], "ts_std: 窗口3 ddof=1")
_close(gp.ts_max(x, 3), [np.nan, np.nan, 3.0, 4.0, 5.0], "ts_max: 窗口3最大值")
_close(gp.ts_min(x, 3), [np.nan, np.nan, 1.0, 2.0, 3.0], "ts_min: 窗口3最小值")

_close(gp.ts_rank([3.0, 1.0, 2.0], 2), [np.nan, 0.5, 1.0], "ts_rank: (w<=x).mean() 口径")
_close(gp.ts_delta(x, 2), [np.nan, np.nan, 2.0, 2.0, 2.0], "ts_delta: x[i]-x[i-2]")
_close(gp.ts_delay(x, 2), [np.nan, np.nan, 1.0, 2.0, 3.0], "ts_delay: 右移2根")

# ts_argmax: i=2 窗 [1,3,2] 最大值距今 1 根；i=3 窗 [3,2,2] 最大值距今 2 根
_close(gp.ts_argmax([1.0, 3.0, 2.0, 2.0], 3), [np.nan, np.nan, 1.0, 2.0],
       "ts_argmax: 最大值距今 bar 数（argmax 取最旧）")

# decay_linear: 权重 [1,2,3]/6 -> (1+4+9)/6 = 7/3
_close(gp.decay_linear([1.0, 2.0, 3.0], 3), [np.nan, np.nan, 14.0 / 6.0],
       "decay_linear: 线性衰减权重均值")

_close(gp.ts_corr([1.0, 2.0, 3.0], [2.0, 4.0, 6.0], 3), [np.nan, np.nan, 1.0],
       "ts_corr: 完全正相关 = 1")
_close(gp.ts_corr([1.0, 2.0, 3.0], [3.0, 2.0, 1.0], 3), [np.nan, np.nan, -1.0],
       "ts_corr: 完全负相关 = -1")
# ts_cov: cov([1,2,3],[2,4,6], ddof=1) = (2+0+2)/2 = 2.0
_close(gp.ts_cov([1.0, 2.0, 3.0], [2.0, 4.0, 6.0], 3), [np.nan, np.nan, 2.0],
       "ts_cov: 窗口3协方差 ddof=1")

# vwap_dev: cum_v=[1,3], vwap=[10,10], dev=[0, 0.1]
_close(gp.vwap_dev([10.0, 11.0], [10.0, 20.0], [1.0, 2.0]), [0.0, 0.1],
       "vwap_dev: c/累计VWAP-1")
_close(gp.day_vwap([10.0, 20.0], [1.0, 2.0]), [10.0, 10.0],
       "day_vwap: 累计 Σamt/Σv")

_close(gp.open_ret([10.0, 11.0, 12.0], [10.0, 10.5, 11.0]), [0.0, 0.1, 0.2],
       "open_ret: c/o[0]-1")

# tail30_ret: c=1..32 -> out[30]=31/1-1=30, out[31]=32/2-1=15
c32 = np.arange(1.0, 33.0)
t30 = gp.tail30_ret(c32)
_ok(bool(np.all(np.isnan(t30[:30]))), "tail30_ret: 前30根为 NaN")
_close(t30[30:], [30.0, 15.0], "tail30_ret: 30根位移收益（B7 口径推广）")

# amihud: ret=[nan,0.1,0], illiq=[nan,0.1/220,0]; i=1 窗含 NaN->NaN；i=2 mean=0.1/440
_close(gp.amihud([10.0, 11.0, 11.0], [100.0, 220.0, 110.0], 2),
       [np.nan, np.nan, 0.1 / 440.0], "amihud: |ret|/amt 滚动均值")

# amount_ratio: ts_mean(amt,2)=[nan,3,5,7] delay1=[nan,nan,3,5] -> 6/3-1=1, 8/5-1=0.6
_close(gp.amount_ratio([2.0, 4.0, 6.0, 8.0], 2), [np.nan, np.nan, 1.0, 0.6],
       "amount_ratio: 当前额/过去n根均额(不含当前)-1")

# cs_rank: 秩/m；并列取平均秩；NaN 保持 NaN
_close(gp.cs_rank([10.0, 30.0, 20.0]), [1 / 3, 1.0, 2 / 3], "cs_rank: 基本分位")
_close(gp.cs_rank([5.0, 5.0, 1.0]), [2.5 / 3, 2.5 / 3, 1 / 3], "cs_rank: 并列平均秩")
_close(gp.cs_rank([1.0, np.nan, 2.0]), [0.5, np.nan, 1.0], "cs_rank: NaN 保持 NaN")
panel2d = np.array([[10.0, 30.0, 20.0], [1.0, 1.0, 1.0]])
_close(gp.cs_rank(panel2d), [[1 / 3, 1.0, 2 / 3], [2 / 3, 2 / 3, 2 / 3]],
       "cs_rank: 2D 面板逐行秩化（全并列行取平均秩 2/3）")

# ── 2. 复用一致性（对照 factor_ops 参考实现）──────────────────────────────
print("── 2. 复用一致性（对照 factor_ops）")
rng = np.random.default_rng(42)
N = 80
c = 20 * np.exp(np.cumsum(rng.standard_normal(N) * 0.01))
v = np.abs(rng.standard_normal(N)) * 1e4 + 1e3
amt = c * v
o = c * (1 + rng.standard_normal(N) * 0.002)

_close(gp.ts_mean(c, 10), fo.ts_mean(c, 10), "ts_mean == factor_ops")
_close(gp.ts_std(c, 10), fo.ts_std(c, 10), "ts_std == factor_ops")
_close(gp.ts_rank(c, 10), fo.ts_rank(c, 10), "ts_rank == factor_ops")
_close(gp.decay_linear(c, 10), fo.decay_linear(c, 10), "decay_linear == factor_ops")
_close(gp.ts_corr(c, v, 10), fo.ts_corr(c, v, 10), "ts_corr == factor_ops")

# day_vwap 对照 fo.day_ctx 的 vwap 口径
day = [{"t": f"09:{31 + i:02d}", "o": o[i], "h": c[i], "l": c[i], "c": c[i],
        "v": v[i], "amt": amt[i]} for i in range(N)]
ctx = fo.day_ctx(day)
_close(gp.day_vwap(amt, v), ctx["vwap"], "day_vwap == factor_ops.day_ctx(vwap) 口径")

# realized_skew / smart_money_dev 对照（用相同的 ret 构造）
ret = np.full(N, np.nan)
ret[1:] = c[1:] / c[:-1] - 1
_close(gp.realized_skew(c, 30), fo.realized_skew({"ret": ret, "n": N}, n=30),
       "realized_skew == factor_ops")
_close(gp.smart_money_dev(c, v, 60),
       fo.smart_money_dev({"c": c, "v": v, "ret": ret, "n": N}, n=60),
       "smart_money_dev == factor_ops")

# ── 3. 前视检测（shift 不变性：尾部追加 20 根，前 N 行不变）───────────────
print("── 3. 前视检测（shift 不变性）")
EXT = 20
c2 = np.concatenate([c, c[-1] * np.exp(np.cumsum(rng.standard_normal(EXT) * 0.01))])
v2 = np.concatenate([v, np.abs(rng.standard_normal(EXT)) * 1e4 + 1e3])
amt2 = c2 * v2
o2 = np.concatenate([o, c2[N:] * 0.999])

causal_cases = [
    ("ts_mean_w10", lambda: gp.ts_mean(c, 10), lambda: gp.ts_mean(c2, 10)),
    ("ts_std_w10", lambda: gp.ts_std(c, 10), lambda: gp.ts_std(c2, 10)),
    ("ts_max_w10", lambda: gp.ts_max(c, 10), lambda: gp.ts_max(c2, 10)),
    ("ts_min_w10", lambda: gp.ts_min(c, 10), lambda: gp.ts_min(c2, 10)),
    ("ts_rank_w10", lambda: gp.ts_rank(c, 10), lambda: gp.ts_rank(c2, 10)),
    ("ts_delta_w10", lambda: gp.ts_delta(c, 10), lambda: gp.ts_delta(c2, 10)),
    ("ts_delay_w10", lambda: gp.ts_delay(c, 10), lambda: gp.ts_delay(c2, 10)),
    ("ts_argmax_w10", lambda: gp.ts_argmax(c, 10), lambda: gp.ts_argmax(c2, 10)),
    ("decay_linear_w10", lambda: gp.decay_linear(c, 10), lambda: gp.decay_linear(c2, 10)),
    ("ts_corr_w10", lambda: gp.ts_corr(c, v, 10), lambda: gp.ts_corr(c2, v2, 10)),
    ("ts_cov_w10", lambda: gp.ts_cov(c, v, 10), lambda: gp.ts_cov(c2, v2, 10)),
    ("vwap_dev", lambda: gp.vwap_dev(c, amt, v), lambda: gp.vwap_dev(c2, amt2, v2)),
    ("smart_money_dev_w60", lambda: gp.smart_money_dev(c, v, 60),
     lambda: gp.smart_money_dev(c2, v2, 60)),
    ("amount_ratio_w10", lambda: gp.amount_ratio(amt, 10), lambda: gp.amount_ratio(amt2, 10)),
    ("day_vwap", lambda: gp.day_vwap(amt, v), lambda: gp.day_vwap(amt2, v2)),
    ("open_ret", lambda: gp.open_ret(c, o), lambda: gp.open_ret(c2, o2)),
    ("tail30_ret", lambda: gp.tail30_ret(c), lambda: gp.tail30_ret(c2)),
    ("amihud_w10", lambda: gp.amihud(c, amt, 10), lambda: gp.amihud(c2, amt2, 10)),
    ("realized_skew_w30", lambda: gp.realized_skew(c, 30), lambda: gp.realized_skew(c2, 30)),
]
for name, f_short, f_long in causal_cases:
    out_short = np.asarray(f_short(), float)
    prefix = np.asarray(f_long(), float)[:N]
    same = (np.isnan(prefix) & np.isnan(out_short)) | np.isclose(
        prefix, out_short, atol=TOL, rtol=1e-9, equal_nan=True)
    _ok(bool(np.all(same)), f"shift 不变性: {name}（前 {N} 行不受尾部追加影响）")

print("     （cs_rank 为横截面算子，无窗口概念，不参与时序 shift 测试）")

# ── 4. NaN / 除零安全 ─────────────────────────────────────────────────────
print("── 4. NaN / 除零安全")
xn = np.array([1.0, np.nan, 3.0, 4.0])
_close(gp.ts_mean(xn, 2), [np.nan, np.nan, np.nan, 3.5], "ts_mean: 窗内含 NaN -> NaN")
_close(gp.day_vwap([10.0, 20.0], [0.0, 2.0]), [np.nan, 15.0], "day_vwap: 累计量=0 -> NaN")
_close(gp.amihud([10.0, 11.0, 12.0], [100.0, 0.0, 120.0], 2),
       [np.nan, np.nan, np.nan], "amihud: amt=0 -> 该根 NaN 并污染窗口")
_close(gp.open_ret([10.0, 11.0], [0.0, 10.0]), [np.nan, np.nan],
       "open_ret: o[0]<=0 -> 全列 NaN")
_close(gp.ts_corr([1.0, 1.0, 1.0], [1.0, 2.0, 3.0], 3), [np.nan, np.nan, np.nan],
       "ts_corr: 零方差 -> NaN")

# 出口无 inf：构造会产生 inf 的输入，出口必须全是 NaN
hot = np.array([1e308, 1e-308, 1e308, 1e-308])
for name, out in [("ts_delta", gp.ts_delta(hot, 1)),
                  ("open_ret", gp.open_ret(hot, hot)),
                  ("amount_ratio", gp.amount_ratio(hot, 2))]:
    _ok(bool(not np.any(np.isinf(out))), f"出口无 inf: {name}")

# ── 5. build_function_set（gplearn 兼容签名）──────────────────────────────
print("── 5. build_function_set")
fns = gp.build_function_set()
_ok(len(fns) == 15 * 5 + 5, f"算子总数 = 15 窗口算子×5 窗口 + 5 无窗口 = {len(fns)}")
names = [n for n, _f, _a in fns]
_ok(len(set(names)) == len(names), "算子名唯一")
for name, fn, arity in fns:
    args = {1: (c,), 2: (c, v), 3: (c, amt, v)}[arity]
    out = fn(*args) if name != "cs_rank" else fn(c)
    _ok(np.asarray(out).shape[-1] == N and not np.any(np.isinf(np.asarray(out, float))),
        f"可调用且输出同长无 inf: {name} (arity={arity})")

print(f"\n全部通过：{_passed} 项断言 ✓")
