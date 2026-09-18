# -*- coding: utf-8 -*-
"""volatility_screen 单元测试 —— 内置 ic_layer 迷你桩（契约对齐用）。

ic_layer.py 本体由任务2开发，本测试只按契约 API 提供桩实现：
    load_panel / evaluate_factor / decile_analysis / mc_baseline
桩注入 sys.modules 后再 import volatility_screen，保证集成面与真实模块一致。

运行：python t_io/validation/factor_mining/test_volatility_screen.py
（纯 assert + main runner，不依赖 pytest；若装了 pytest 也可直接跑）

纪律：测试数据全部为合成数据，绝不触碰生产 t_io/state/。
"""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import pandas as pd

# ── 迷你桩：ic_layer（契约实现，非任务2真身） ────────────────────────────
_stub = types.ModuleType("ic_layer")


def _stub_load_panel(panel_dir):
    raise NotImplementedError("桩不支持从磁盘加载；测试直接构造 panel")


def _fwd_returns(factor, panel, horizon):
    p = panel.sort_values(["symbol", "date"]).reset_index(drop=True)
    p["fwd"] = p.groupby("symbol", sort=False)["close"].shift(-horizon) / p["close"] - 1.0
    m = factor.merge(p[["symbol", "date", "fwd"]], on=["symbol", "date"], how="left")
    return m.dropna(subset=["value", "fwd"])


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """spearman = rank 后 pearson（禁 scipy 依赖）。"""
    return float(a.rank(method="average").corr(b.rank(method="average"), method="pearson"))


def _stub_evaluate_factor(factor, panel, horizons=(1, 3, 5), min_coverage=30):
    assert set(factor.columns) >= {"date", "symbol", "value"}
    out = {}
    for h in horizons:
        m = _fwd_returns(factor, panel, h)
        ic_by_date = m.groupby("date").apply(
            lambda d: _spearman(d["value"], d["fwd"]) if len(d) >= min_coverage else np.nan,
            include_groups=False)
        ic = ic_by_date.dropna()
        out[h] = {
            "rank_ic_mean": float(ic.mean()) if len(ic) else np.nan,
            "icir": float(ic.mean() / ic.std()) if len(ic) > 1 and ic.std() > 0 else np.nan,
            "win_rate": float((ic > 0).mean()) if len(ic) else np.nan,
            "n_dates": int(len(ic)),
        }
    return out


def _stub_decile_analysis(factor, panel, horizon, n_groups=10):
    m = _fwd_returns(factor, panel, horizon)
    m = m.copy()
    m["grp"] = m.groupby("date")["value"].transform(
        lambda s: pd.qcut(s.rank(method="first"), n_groups, labels=False))
    ret = m.groupby("grp")["fwd"].mean()
    return {"horizon": horizon, "group_mean_ret": ret.to_dict(),
            "monotonic_spearman": _spearman(pd.Series(ret.index, dtype=float),
                                            pd.Series(ret.values, dtype=float))}


def _stub_mc_baseline(factor, panel, horizon, n=200, seed=42):
    rng = np.random.default_rng(seed)
    m = _fwd_returns(factor, panel, horizon)
    real = _spearman(m["value"], m["fwd"])
    cnt = 0
    for _ in range(n):
        perm = rng.permutation(m["value"].to_numpy())
        if _spearman(pd.Series(perm), m["fwd"]) >= real:
            cnt += 1
    return {"horizon": horizon, "real_ic": float(real), "mc_p": (cnt + 1) / (n + 1), "n": n}


_stub.load_panel = _stub_load_panel
_stub.evaluate_factor = _stub_evaluate_factor
_stub.decile_analysis = _stub_decile_analysis
_stub.mc_baseline = _stub_mc_baseline
sys.modules["ic_layer"] = _stub

sys.path.insert(0, str(Path(__file__).resolve().parent))
import volatility_screen as vs  # noqa: E402


# ── 合成面板 ─────────────────────────────────────────────────────────────
def make_panel(n_symbols: int = 8, n_days: int = 160, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=n_days)
    rows = []
    for i in range(n_symbols):
        sym = f"SHSE.{600000 + i}" if i % 2 == 0 else f"SZSE.{300000 + i}"
        vol_scale = 0.01 + 0.01 * (i % 4)          # 不同波动水平
        liq_scale = 1e8 * (1 + i)                   # 不同流动性水平
        ret = rng.normal(0, vol_scale, n_days)
        close = 10 * np.exp(np.cumsum(ret))
        high = close * (1 + np.abs(rng.normal(0, vol_scale / 2, n_days)))
        low = close * (1 - np.abs(rng.normal(0, vol_scale / 2, n_days)))
        open_ = close * (1 + rng.normal(0, 0.002, n_days))
        volume = rng.integers(1e6, 5e6, n_days).astype(float)
        amount = volume * close
        amount = np.where(amount < liq_scale, liq_scale, amount)
        rows.append(pd.DataFrame({
            "symbol": sym, "date": dates, "open": open_, "high": high,
            "low": low, "close": close, "volume": volume, "amount": amount}))
    return pd.concat(rows, ignore_index=True)


def make_universe(panel: pd.DataFrame, st_symbols=()) -> pd.DataFrame:
    syms = sorted(panel["symbol"].unique())
    return pd.DataFrame({
        "symbol": syms,
        "sec_name": ["ST测试" if s in st_symbols else f"测试股{i}" for i, s in enumerate(syms)],
        "listed_date": "2010-01-01",
    })


# ── 测试用例 ─────────────────────────────────────────────────────────────
def test_factor_shape_and_nan_warmup():
    panel = make_panel()
    f = vs.compute_factor_series(panel)
    assert len(f) == len(panel)
    assert list(f.columns) == ["symbol", "date"] + vs.FACTOR_NAMES
    # 每只票前 20 天 ATR20 应为 NaN（首日振幅即 NaN，20 日窗口需 20 个非 NaN 值）
    one = f[f["symbol"] == f["symbol"].iloc[0]].reset_index(drop=True)
    assert one["ATR20"].iloc[:20].isna().all()
    assert not np.isnan(one["ATR20"].iloc[20])
    # AMP 第 0 天 NaN（无昨日收盘），第 1 天起有值
    assert np.isnan(one["AMP"].iloc[0]) and not np.isnan(one["AMP"].iloc[1])


def test_no_lookahead():
    """截断面板重算，重叠区间的因子值必须逐位一致（无未来函数核心测试）。"""
    panel = make_panel(n_symbols=4, n_days=120)
    full = vs.compute_factor_series(panel)
    cut_date = sorted(panel["date"].unique())[99]
    part = vs.compute_factor_series(panel[panel["date"] <= cut_date])
    m = full.merge(part, on=["symbol", "date"], suffixes=("_full", "_part"))
    for c in vs.FACTOR_NAMES:
        a, b = m[f"{c}_full"], m[f"{c}_part"]
        both = a.notna() | b.notna()
        assert (a[both].isna() == b[both].isna()).all(), f"{c} NaN 模式不一致"
        ok = a.notna() & b.notna()
        assert np.allclose(a[ok], b[ok], rtol=1e-10, atol=1e-12), f"{c} 存在未来函数"


def test_known_value_atr20():
    """手工构造等振幅序列，验证 ATR20 数值口径 = mean((h-l)/prev_close, 20)。"""
    dates = pd.bdate_range("2025-01-02", periods=25)
    close = np.full(25, 10.0)
    high = close * 1.05
    low = close * 0.95
    panel = pd.DataFrame({"symbol": "SHSE.600001", "date": dates, "open": close,
                          "high": high, "low": low, "close": close,
                          "volume": 1e6, "amount": 1e8})
    f = vs.compute_factor_series(panel)
    # (h-l)/prev_close = (10.5-9.5)/10 = 0.1，20 日均值 = 0.1
    assert abs(f["ATR20"].iloc[20] - 0.1) < 1e-9
    # TR_SHARE = ATR20/close = 0.1/10 = 0.01
    assert abs(f["TR_SHARE"].iloc[20] - 0.01) < 1e-9


def test_suspension_day_nan():
    panel = make_panel(n_symbols=2, n_days=60)
    idx = panel[(panel["symbol"] == panel["symbol"].iloc[0])].index[30]
    panel.loc[idx, "volume"] = 0
    f = vs.compute_factor_series(panel)
    assert f.loc[idx, vs.FACTOR_NAMES].isna().all()


def test_tradable_mask():
    panel = make_panel(n_symbols=4, n_days=160)
    f = vs.compute_factor_series(panel)
    uni = make_universe(panel, st_symbols={panel["symbol"].unique()[1]})
    mask = vs.build_tradable_mask(panel, f, uni)
    df = vs._prep_panel(panel)
    # ST 股全灭
    st_sym = panel["symbol"].unique()[1]
    assert not mask[df["symbol"] == st_sym].any()
    # 上市未满 120 个交易日全灭
    first_sym = df["symbol"].unique()[0]
    first_sym_mask = mask[df["symbol"] == first_sym].reset_index(drop=True)
    assert not first_sym_mask.iloc[:119].any()
    assert first_sym_mask.iloc[119:].all()
    # AMOUNT20 < 5e7 灭：把一只票成交额打到地板
    panel2 = make_panel(n_symbols=2, n_days=160)
    sym0 = panel2["symbol"].unique()[0]
    panel2.loc[panel2["symbol"] == sym0, "amount"] = 1e5
    f2 = vs.compute_factor_series(panel2)
    mask2 = vs.build_tradable_mask(panel2, f2, None)
    df2 = vs._prep_panel(panel2)
    assert not mask2[df2["symbol"] == sym0].any()


def test_stub_contract_full_check():
    """桩契约冒烟：evaluate/decile/mc 三者返回结构可用。"""
    panel = make_panel(n_symbols=6, n_days=160)
    f = vs.compute_factor_series(panel)
    uni = make_universe(panel)
    mask = vs.build_tradable_mask(panel, f, uni)
    ff = vs.apply_filter(f, mask)
    res = vs.run_full_check(ff, panel, names=["ATR20"], mc_n=10, progress=False)
    r = res["ATR20"]
    assert set(r) == {"evaluate", "decile", "mc"}
    assert set(r["evaluate"]) == {1, 3, 5}
    assert "rank_ic_mean" in r["evaluate"][1]
    assert "group_mean_ret" in r["decile"]
    assert "mc_p" in r["mc"] and 0 < r["mc"]["mc_p"] <= 1


def test_candidate_pool():
    panel = make_panel(n_symbols=10, n_days=160)
    f = vs.compute_factor_series(panel)
    uni = make_universe(panel)
    mask = vs.build_tradable_mask(panel, f, uni)
    ff = vs.apply_filter(f, mask)
    # 假持仓文件放临时目录，绝不碰生产 t_io/state/
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "holdings.json"
        last = ff["date"].max()
        held_sym = ff[ff["date"] == last].sort_values("COMBO", ascending=False)["symbol"].iloc[0]
        hp.write_text(
            __import__("json").dumps({"600000": {"gm_symbol": held_sym, "name": "测试持仓"}},
                                     ensure_ascii=False), encoding="utf-8")
        pool = vs.candidate_pool(ff, holdings_path=hp, top_n=5)
    assert len(pool) == 5
    assert pool["COMBO"].is_monotonic_decreasing
    assert pool["held"].sum() == 1
    assert pool.loc[pool["held"], "held_name"].iloc[0] == "测试持仓"
    assert set(pool["board"]) <= {"沪主板", "创业板"}
    assert pool["AMOUNT20"].min() >= vs.MIN_AMOUNT20


def _run_all():
    sys.stdout.reconfigure(encoding="utf-8")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n全部 {len(tests)} 个测试通过")


if __name__ == "__main__":
    _run_all()
