# -*- coding: utf-8 -*-
"""gp_miner 单元测试（任务 S2-6）。

覆盖：表达式解析求值对照 / 字符串进化一步 / 适应度数值手算对照 /
MC 日块打乱不跨日块 / 台账落盘回读 / 硬闸与公式。

运行：python t_io/validation/factor_mining/test_gp_miner.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np

from t_io.validation.factor_mining import gp_ops
from t_io.validation.factor_mining import gp_miner as gm
from t_io.validation.factor_mining.gp_vendor.genetic import SymbolicRegressor
from t_io.validation.factor_mining.gp_vendor.fitness import make_fitness


def _synthetic_panel(n_days=20, L=60, seed=7, skill=0.8):
    """合成面板：close 日内形态逐日缩放，label 与该形态线性相关（预埋已知信号）。

    构造：F = open_ret(close, open) = c/o[0]-1；每天的形态斜率 s_d 随机；
    R[d,t] = skill * F[d,t] + noise —— 则 z(F) 与 R 的时序 IC 显著为正。
    """
    rng = np.random.RandomState(seed)
    base = np.linspace(0.0, 0.02, L)                    # 日内基础形态
    dates = [f"2026-03-{d + 1:02d}" for d in range(n_days)]
    day_terms, R = [], np.full((n_days, L), np.nan)
    for d in range(n_days):
        s = rng.uniform(0.5, 2.0)
        c = 100.0 * (1.0 + s * base + rng.normal(0, 1e-4, L))
        o = np.full(L, 100.0)
        o[0] = c[0] * 0.999
        h = c * 1.001
        terms = {"open": o, "high": h, "low": c * 0.999, "close": c,
                 "volume": np.full(L, 1e5), "vwap": c.copy(),
                 "amount": np.full(L, 1e7)}
        day_terms.append(terms)
        F = c / o[0] - 1.0
        R[d] = skill * F + rng.normal(0, 2e-3, L)
        R[d, -gm.LABEL_H:] = np.nan                     # 尾部截断（模拟真实 label）
    term_mats = {k: np.array([t[k] for t in day_terms])
                 for k in ("open", "high", "low", "close", "volume", "vwap", "amount")}
    return gm.StockPanel("SYNTH", dates, R, term_mats)


def _manual_zscore(M, look=14, min_hist=5):
    """独立重实现的同日同时刻 z-score（与 eval_factor.zscore_matrix 公式对照）。"""
    n = M.shape[0]
    Z = np.full_like(M, np.nan)
    for k in range(n):
        a = max(0, k - look)
        if k - a < min_hist:
            continue
        w = M[a:k]
        m = np.nanmean(w, axis=0)
        sd = np.nanstd(w, axis=0, ddof=1)
        ok = np.isfinite(m) & np.isfinite(sd) & (sd > 1e-12) & np.isfinite(M[k])
        Z[k][ok] = (M[k][ok] - m[ok]) / sd[ok]
    return Z


# ── 1. 表达式解析与求值对照 ────────────────────────────────────────────────
def test_expr_eval_matches_gp_ops():
    _gp_fns, ns = gm.build_gp_function_set()
    ev = gm.ExprEvaluator(ns)
    rng = np.random.RandomState(0)
    c = 100 * np.cumprod(1 + rng.normal(0, 1e-3, 50))
    terms = {"open": c * 0.999, "high": c * 1.001, "low": c * 0.998,
             "close": c, "volume": np.full(50, 1e5), "vwap": c,
             "amount": np.full(50, 1e6)}
    got = ev.eval_day("ts_mean_w5(close)", terms)
    want = gp_ops.ts_mean(c, 5)
    assert np.allclose(got, want, equal_nan=True), "ts_mean_w5 求值与 gp_ops 不一致"
    got2 = ev.eval_day("ts_delta_w10(ts_mean_w5(close))", terms)
    want2 = gp_ops.ts_delta(gp_ops.ts_mean(c, 5), 10)
    assert np.allclose(got2, want2, equal_nan=True), "嵌套表达式求值不一致"
    assert ev.eval_day("ts_mean_w5(nosuch)", terms) is None, "未知终端应返回 None"
    bad = ev.eval_day("ts_corr_w5(close, volume)", terms)
    assert bad is not None and len(bad) == 50
    # 标量退化 → 常数列
    const = ev.eval_day("close", {"close": np.array([3.0])})
    assert const is not None
    print("  [1] 表达式解析/求值对照 OK")


# ── 2. 字符串进化一步 ──────────────────────────────────────────────────────
def test_evolution_one_step():
    gp_fns, ns = gm.build_gp_function_set()
    seen = {}

    def metric(_y, y_pred, _w):
        e = str(y_pred[0])
        seen.setdefault(e, float(len(e) % 7) / 7.0)
        return seen[e]

    X, y = gm.make_terminal_X()
    cb_fired = []
    est = SymbolicRegressor(population_size=20, generations=1, init_depth=(2, 4),
                            tournament_size=10, max_samples=1.0,
                            parsimony_coefficient=0.0, stopping_criteria=1.0,
                            const_range=None, n_jobs=1, function_set=gp_fns,
                            metric=make_fitness(function=metric, greater_is_better=True),
                            random_state=0, verbose=0)
    est.fit(X, y, callback=lambda: cb_fired.append(1))
    assert len(cb_fired) == 1, "callback 应每代触发一次"
    assert len(seen) >= 20, f"一代应至少评估种群规模个表达式，实际 {len(seen)}"
    ev = gm.ExprEvaluator(ns)
    rng = np.random.RandomState(1)
    c = 100 * np.cumprod(1 + rng.normal(0, 1e-3, 30))
    terms = {"open": c, "high": c, "low": c, "close": c,
             "volume": np.full(30, 1e5), "vwap": c, "amount": np.full(30, 1e6)}
    n_ok = sum(ev.eval_day(e, terms) is not None for e in list(seen)[:20])
    assert n_ok >= 10, f"初代表达式应大多可求值，实际 {n_ok}/20"
    print(f"  [2] 进化一步 OK（{len(seen)} 个唯一表达式，{n_ok}/20 可求值）")


# ── 3. 适应度数值手算对照 ──────────────────────────────────────────────────
def test_fitness_handcalc():
    # 3a. pearson_ic 与 np.corrcoef 对照 + NaN 纪律
    rng = np.random.RandomState(3)
    a = rng.normal(size=200)
    b = 0.6 * a + rng.normal(size=200)
    want = float(np.corrcoef(a, b)[0, 1])
    assert abs(gm.pearson_ic(a, b) - want) < 1e-12
    assert np.isnan(gm.pearson_ic(a[:10], b[:10])), "对数 <30 应为 NaN"
    assert np.isnan(gm.pearson_ic(np.ones(100), b[:100])), "零方差应为 NaN"

    # 3b. 端到端：合成面板 + 预埋信号，独立重算 z 与 IC 对照
    panel = _synthetic_panel()
    _gp_fns, ns = gm.build_gp_function_set()
    ev = gm.ExprEvaluator(ns)
    fit = gm.FitnessEvaluator([panel], ev, trig_gate=False, min_stocks=1)
    det = fit._eval("open_ret(close,open)")
    assert det["fitness"] > 0, f"预埋正信号应得正适应度，实际 {det}"
    F = ev.eval_matrix("open_ret(close,open)", panel)
    Z = _manual_zscore(F)                                # 独立重实现
    rows = panel.is_rows()
    pairs = np.array([(z, r) for z, r in zip(Z[rows].ravel(), panel.R[rows].ravel())
                      if np.isfinite(z) and np.isfinite(r)])
    manual_ic = float(np.corrcoef(pairs[:, 0], pairs[:, 1])[0, 1])
    assert abs(det["mean_ic"] - manual_ic) < 1e-9, \
        f"适应度 IC {det['mean_ic']} 与手算 {manual_ic} 不一致"
    assert det["mean_ic"] > 0.3, f"预埋 skill=0.8 的信号 IC 应显著为正，实际 {det['mean_ic']}"
    # 3c. 一致性惩罚公式：fitness = mean_ic − λ·neg_frac（单票 IC>0 → neg_frac=0）
    assert abs(det["fitness"] - det["mean_ic"]) < 1e-12
    # 3d. cache 与 token 硬闸（memo 为 float32，复算允许 1e-7 级浮点差）
    again = fit.metric(None, np.array(["open_ret(close,open)"], dtype=object), None)
    assert abs(again - det["fitness"]) < 1e-7, f"cache 复算漂移 {again} vs {det['fitness']}"
    long_expr = "ts_mean_w5(" * 11 + "close" + ")" * 11
    assert fit._eval(long_expr)["fitness"] == gm.FitnessEvaluator.DEATH
    print(f"  [3] 适应度手算对照 OK（预埋 IC={det['mean_ic']:.3f}）")


# ── 4. MC 日块打乱不跨日块 ─────────────────────────────────────────────────
def test_mc_day_block_shuffle():
    n_days, L = 12, 40
    R = np.repeat(np.arange(n_days, dtype=float)[:, None], L, axis=1)  # 行号编码
    rows = np.arange(n_days)
    rng = np.random.RandomState(42)
    sh = gm.day_block_shuffle(R, rows, rng)
    # 每行内部不变（日内形态保留）
    assert all(np.all(sh[i] == sh[i, 0]) for i in range(n_days)), "行内被搅乱"
    # 行集合不变（只置换不混入）
    assert sorted(sh[:, 0].tolist()) == list(range(n_days)), "行集合改变"
    # 行只来自 IS 行集合：构造 IS/OOS 混合场景
    R2 = np.repeat(np.arange(20, dtype=float)[:, None], 5, axis=1)
    is_rows = np.arange(12)                        # 前 12 日为 IS
    sh2 = gm.day_block_shuffle(R2, is_rows, rng)
    assert set(sh2[:, 0].tolist()) <= set(range(12)), "打乱跨越了日块集合边界"
    # 打乱确实改变顺序（seed 固定下几乎必然）
    assert not np.array_equal(sh[:, 0], R[:, 0]), "顺序未被置换"
    print("  [4] MC 日块打乱不跨日块 OK")


# ── 5. 台账落盘回读 ────────────────────────────────────────────────────────
def test_ledger_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.json"
        cb = gm.GPCallback(None, None, [], path)
        cb.ledger["ts_mean_w5(close)"] = {
            "expr": "ts_mean_w5(close)", "token_len": 2, "fitness": 0.01234,
            "is_ic": 0.015, "is_ic_std": 0.004, "neg_frac": 0.1, "trig_rate": 0.02,
            "n_stocks": 39, "in_pool": True, "oos_ic": 0.009,
            "mc": {"n": 30, "null_mean": 0.001, "null_q95": 0.008,
                   "real_ic": 0.015, "verdict": "PASS"},
            "fee": {"n": 500, "net_mean": 0.05, "rand_mean": -0.02, "delta": 0.07},
            "sign_hint": 1, "final_review": "python gp_miner.py --review ..."}
        cb.records.append({"gen": 4, "pool": 1})
        cb.dump()
        back = json.loads(path.read_text(encoding="utf-8"))
        assert back["meta"]["is"] == [gm.IS_START, gm.IS_END]
        assert back["meta"]["oos"] == [gm.OOS_START, gm.OOS_END]
        c = back["candidates"][0]
        for k in ("expr", "fitness", "is_ic", "oos_ic", "mc", "fee", "final_review"):
            assert k in c, f"台账缺字段 {k}"
        assert c["mc"]["verdict"] == "PASS" and c["fee"]["n"] == 500
        assert abs(c["fitness"] - 0.01234) < 1e-9
    print("  [5] 台账落盘回读 OK")


# ── 6. 算子集与终端 ────────────────────────────────────────────────────────
def test_function_set():
    gp_fns, ns = gm.build_gp_function_set()
    names = [f.name for f in gp_fns]
    assert "cs_rank" not in names, "cs_rank 默认应排除"
    assert len(names) == len(gp_ops.build_function_set()) - 1
    gp_fns2, _ = gm.build_gp_function_set(include_csrank=True)
    assert len(gp_fns2) == len(gp_ops.build_function_set())
    X, y = gm.make_terminal_X()
    assert X.shape == (1, len(gm.TERMINALS)) and X.dtype == object
    assert set(X[0]) == {"open", "high", "low", "close", "volume", "vwap", "amount"}
    print(f"  [6] 算子集 {len(names)} 个（去 cs_rank）OK")


# ── 7. gp_fast_ops 与 gp_ops 逐点对照 ──────────────────────────────────────
def test_fast_ops_match_gp_ops():
    from t_io.validation.factor_mining import gp_fast_ops as gf
    rng = np.random.RandomState(11)
    n = 300
    c = 100 * np.cumprod(1 + rng.normal(0, 2e-3, n))
    o = c * (1 + rng.normal(0, 5e-4, n))
    h = np.maximum(c, o) * 1.001
    v = np.abs(rng.normal(1e5, 3e4, n))
    amt = c * v
    c[37] = np.nan                       # 植入 NaN 验证传播口径
    cases_1 = {"open": o, "high": h, "low": c * 0.99, "close": c,
               "volume": v, "vwap": c, "amount": amt}
    for name, func, arity in gp_ops.build_function_set():
        if name == "cs_rank":
            continue
        fast = gf.build_fast_namespace()[name]
        args1 = {"ts_mean": (c,), "ts_std": (c,), "ts_max": (h,), "ts_min": (c,),
                 "ts_rank": (c,), "ts_delta": (c,), "ts_delay": (c,),
                 "ts_argmax": (h,), "decay_linear": (c,),
                 "ts_corr": (c, v), "ts_cov": (c, v), "amount_ratio": (amt,),
                 "amihud": (c, amt), "realized_skew": (c,),
                 "smart_money_dev": (c, v)}
        base = name.rsplit("_w", 1)[0]
        if name in ("vwap_dev",):
            a1 = (c, amt, v)
        elif name in ("day_vwap",):
            a1 = (amt, v)
        elif name in ("open_ret",):
            a1 = (c, o)
        elif name in ("tail30_ret",):
            a1 = (c,)
        else:
            a1 = args1[base]
        got = fast(*[np.asarray(x, float) for x in a1])
        want = func(*[np.asarray(x, float) for x in a1])
        tol = 1e-9 if base == "smart_money_dev" else 1e-10
        assert np.allclose(got, want, atol=tol, rtol=1e-9, equal_nan=True), \
            f"{name} 与 gp_ops 不一致: maxdiff=" \
            f"{np.nanmax(np.abs(got - want)):.2e}"
    # 2D 沿 axis=-1 == 逐行 1D
    M = np.stack([c, c * 1.01])
    got2 = gf.ts_mean(M, 10)
    assert np.allclose(got2[0], gf.ts_mean(c, 10), equal_nan=True)
    assert np.allclose(got2[1], gf.ts_mean(c * 1.01, 10), equal_nan=True)
    print("  [7] gp_fast_ops 全部 79 算子与 gp_ops 逐点对照 OK")


# ── 8. fast_zscore 与 eval_factor.zscore_matrix 对照 ───────────────────────
def test_fast_zscore_matches_eval_factor():
    import eval_factor as ef
    rng = np.random.RandomState(13)
    M = rng.normal(size=(40, 60))
    M[3, 7] = np.nan
    M[10, :] = np.nan                      # 整行缺失
    want = ef.zscore_matrix(M)
    got = gm.fast_zscore(M)
    assert np.allclose(got, want, atol=1e-12, equal_nan=True), \
        f"fast_zscore 与 eval_factor 不一致 maxdiff={np.nanmax(np.abs(got - want)):.2e}"
    print("  [8] fast_zscore 与 eval_factor.zscore_matrix 对照 OK")


if __name__ == "__main__":
    test_function_set()
    test_expr_eval_matches_gp_ops()
    test_evolution_one_step()
    test_fitness_handcalc()
    test_mc_day_block_shuffle()
    test_ledger_roundtrip()
    test_fast_ops_match_gp_ops()
    test_fast_zscore_matches_eval_factor()
    print("== gp_miner 全部测试通过 ==")
