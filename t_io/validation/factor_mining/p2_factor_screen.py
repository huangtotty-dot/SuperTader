# -*- coding: utf-8 -*-
"""P2 因子体检（任务P3 · S2 阶段）—— 换手率因子族 + 行业动量纳入复合分前置筛选。

因子清单（全部 t 日收盘可得，严格无未来函数；方向预期为预注册注记）：
    TURN20        mean(turnover,20)            20日日均换手率（负向预期）
    PCT_TURN20    turnover 在 trailing20 日内分位（含自身）  当日换手率20日分位
    STR           std(turnover,20)/mean(turnover,20)  换手率20日变异系数（负向预期）
    GAP_cond_turn GAP × (1 − PCT_TURN20)       隔夜跳空×换手率条件（低换手跳空正向预期）
    IND_MOM20     行业(申万一级)成员 ret20 等权均值   行业20日动量（正向预期）
    IND_BREADTH   行业成员日上涨家数占比的20日均值      行业广度
    其中 turnover = volume / float_share_shares（PIT 流通股本，panel_aux）。
    行业聚合在全面板成员上计算（非仅过滤后宇宙）；UNCOVERED 标的行业因子置 NaN（剔除并注明）。

口径（同 F3 预注册 + S1 卫生宇宙）：
    前瞻收益 buy_open h=1/3/5；过滤器四道（上市>=120 + AMOUNT20>=5000万 + 剔停牌 + 剔ST）；
    叠加 S1 卫生掩码 s1_hygiene_mask.pkl（<2元 + 60日严格回撤>60% 剔除）；
    MC：n=200 seed=42，主判窗 h=5，rank>0.95 为 pass（反向显著以 mc_rank≈0 呈现）。

CLI：
    python p2_factor_screen.py --prep               # 计算因子+过滤+卫生，落快照
    python p2_factor_screen.py --only=TURN20        # 单因子全检（可逗号多个）
    python p2_factor_screen.py --only=STR --skip-mc
    python p2_factor_screen.py --corr               # P2显著因子 vs 原四因子冗余检查
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ic_layer import decile_analysis, evaluate_factor, load_panel, mc_baseline  # noqa: E402
from volatility_screen import (  # noqa: E402
    MIN_AMOUNT20,
    MIN_LISTED_DAYS,
    _norm_sym,
    _prep_panel,
    _roll,
    _roll_apply_last_pct,
    load_universe,
)

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel"
AUX_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel_aux"
S0_RESULTS = Path(__file__).resolve().parent / "s0_account" / "results"
F3_DIR = Path(__file__).resolve().parent / "results" / "daily_selection_screen_2026-10-09"
FULLHIST_SNAP = (Path(__file__).resolve().parent / "results"
                 / "fullhist_robustness_2026-10-09" / "factors_filtered.parquet")
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "p2_screen"

SINCE = "2023-09-01"
HORIZONS = (1, 3, 5)
DECILE_HORIZON = 5
MC_HORIZON = 5
MC_N = 200
MC_SEED = 42
CORR_THRESHOLD = 0.7
WIN = 20

P2_NAMES = ["TURN20", "PCT_TURN20", "STR", "GAP_cond_turn", "IND_MOM20", "IND_BREADTH"]

# 方向预期（预注册注记；+1=值大看好，-1=负向预期，反向显著时取反复用）
DIRECTION_EXPECT = {
    "TURN20": -1, "PCT_TURN20": -1, "STR": -1,
    "GAP_cond_turn": +1, "IND_MOM20": +1, "IND_BREADTH": +1,
}
# 原四因子方向统一（值大=看好，同 s0_prep）
CORE4_MAP = {"REV10": +1.0, "GAP": +1.0, "AMOUNT_CHG": -1.0, "PRICE_POS60": -1.0}


# ════════════════════════════════════════════════════════════════════════
# 1. P2 因子计算（面板级向量化）
# ════════════════════════════════════════════════════════════════════════
def compute_p2_factors(panel: pd.DataFrame) -> pd.DataFrame:
    df = _prep_panel(panel)
    g = df["symbol"]
    t0 = time.perf_counter()

    # ── 流通股本 merge → 真换手率 ──
    fs = pd.read_parquet(AUX_DIR / "float_share_daily.parquet",
                         columns=["symbol", "trade_date", "float_share_shares"])
    fs = fs.rename(columns={"trade_date": "date"})
    fs["symbol"] = _norm_sym(fs["symbol"])       # SHSE.600000 -> 600000（面板口径）
    df = df.merge(fs, on=["symbol", "date"], how="left", sort=False)
    del fs
    turn1 = df["volume"] / df["float_share_shares"].replace(0, np.nan)
    print(f"[factor] turnover merged, 覆盖 {turn1.notna().mean():.2%} "
          f"({time.perf_counter()-t0:.0f}s)", flush=True)

    close_prev = df["close"].groupby(g, sort=False).shift(1)
    ret1 = df["close"] / close_prev - 1.0
    ret20 = df["close"] / df["close"].groupby(g, sort=False).shift(20) - 1.0
    gap = df["open"] / close_prev - 1.0

    turn20 = _roll(turn1, g, WIN, "mean")
    pct_turn20 = _roll_apply_last_pct(turn1, g, WIN)
    str_cv = _roll(turn1, g, WIN, "std") / turn20.replace(0, np.nan)
    amt20 = _roll(df["amount"], g, WIN, "mean")
    print(f"[factor] 换手率族 done ({time.perf_counter()-t0:.0f}s)", flush=True)

    # ── 行业聚合（全面板成员，UNCOVERED 剔除）──
    im = pd.read_parquet(AUX_DIR / "industry_map.parquet",
                         columns=["symbol", "industry_sw1", "industry_source"])
    im["symbol"] = _norm_sym(im["symbol"])       # SHSE.600000 -> 600000（面板口径）
    im["industry_sw1"] = im["industry_sw1"].where(im["industry_source"] == "SW2021_L1")
    df = df.merge(im[["symbol", "industry_sw1"]], on="symbol", how="left", sort=False)
    del im
    n_uncovered = int(df["industry_sw1"].isna().sum())
    dind = [df["date"], df["industry_sw1"]]
    ind_mom20 = ret20.groupby(dind, sort=False).transform("mean")
    up1 = (ret1 > 0).astype(float).where(ret1.notna())
    breadth_d = up1.groupby(dind, sort=False).transform("mean")
    ind_breadth = _roll(breadth_d, g, WIN, "mean")
    has_ind = df["industry_sw1"].notna()
    ind_mom20 = ind_mom20.where(has_ind)
    ind_breadth = ind_breadth.where(has_ind)
    print(f"[factor] 行业族 done, UNCOVERED bar={n_uncovered} "
          f"({n_uncovered/len(df):.2%}) ({time.perf_counter()-t0:.0f}s)", flush=True)

    out = df[["symbol", "date"]].copy()
    out["TURN20"] = turn20
    out["PCT_TURN20"] = pct_turn20
    out["STR"] = str_cv
    out["GAP_cond_turn"] = gap * (1.0 - pct_turn20)
    out["IND_MOM20"] = ind_mom20
    out["IND_BREADTH"] = ind_breadth
    out["AMOUNT20"] = amt20                       # 过滤器辅助列
    # 停牌日 volume<=0：全部置 NaN
    out.loc[df["volume"] <= 0, P2_NAMES] = np.nan
    return out


# ════════════════════════════════════════════════════════════════════════
# 2. 过滤器（F3 四道）+ S1 卫生掩码
# ════════════════════════════════════════════════════════════════════════
def build_tradable_mask(panel: pd.DataFrame, factors: pd.DataFrame,
                        universe: pd.DataFrame | None = None) -> pd.Series:
    """同 daily_selection_screen.build_tradable_mask（复制以避免改其全局）。"""
    df = _prep_panel(panel)
    g = df["symbol"]
    listed_ok = g.groupby(g, sort=False).cumcount() >= (MIN_LISTED_DAYS - 1)
    liquid_ok = factors["AMOUNT20"] >= MIN_AMOUNT20
    alive_ok = df["volume"] > 0
    mask = listed_ok & liquid_ok & alive_ok
    st_names: set[str] = set()
    if universe is not None and "sec_name" in universe.columns:
        names = universe["sec_name"].astype(str)
        st_names = set(_norm_sym(
            universe.loc[names.str.contains("ST", case=False, na=False), "symbol"]))
        mask = mask & ~_norm_sym(df["symbol"]).isin(st_names).to_numpy()
    mask.attrs["st_excluded"] = len(st_names)
    return mask


def apply_hygiene(factors: pd.DataFrame) -> pd.DataFrame:
    """叠加 S1 卫生掩码（s1_hygiene_mask.pkl，date×symbol bool）。
    掩码头部 59 日（dd60 窗口不足=暖机段）全 False → 该段因子置 NaN。"""
    import pickle
    with open(S0_RESULTS / "s1_hygiene_mask.pkl", "rb") as f:
        mask_df = pickle.load(f)
    hyg = mask_df.stack().rename("hyg").reset_index()
    hyg.columns = ["date", "symbol", "hyg"]
    hyg["date"] = pd.to_datetime(hyg["date"])
    out = factors.merge(hyg, on=["date", "symbol"], how="left", sort=False)
    bad = ~out["hyg"].fillna(False).to_numpy(bool)
    out.loc[bad, P2_NAMES] = np.nan
    out = out.drop(columns=["hyg"])
    print(f"[hygiene] 卫生外置 NaN {int(bad.sum())} 行 ({bad.mean():.1%})", flush=True)
    return out


def to_factor_frame(factors: pd.DataFrame, name: str) -> pd.DataFrame:
    f = factors[["date", "symbol"]].copy()
    f["value"] = factors[name]
    return f.dropna(subset=["value"]).reset_index(drop=True)


# ════════════════════════════════════════════════════════════════════════
# 3. 冗余检查：MC 显著 P2 因子（方向统一）vs 原四因子
# ════════════════════════════════════════════════════════════════════════
def _sig_factor_names() -> list[str]:
    """从 check_*.json 汇总 MC 显著因子（pass 或反向显著）及其方向符号。"""
    sig = {}
    for p in sorted(RESULTS_DIR.glob("check_*.json")):
        name = p.stem.replace("check_", "")
        res = json.loads(p.read_text(encoding="utf-8"))
        mc = res.get("mc") or {}
        rank = mc.get("mc_rank")
        if rank is None:
            continue
        if mc.get("pass"):
            sig[name] = +1.0                       # 按原样（值大看好）
        elif rank <= 0.05:
            sig[name] = -1.0                       # 反向显著：取反复用
    return sig


def load_core4(since: str = "2024-03-05") -> pd.DataFrame:
    """原四因子快照。F3 目录快照缺失时回退全历史快照（同过滤口径，近窗取值一致；
    2026-10-10 注：F3 factors_filtered.parquet 被并发任务移走，以此为准）。"""
    src = F3_DIR / "factors_filtered.parquet"
    cols = ["date", "symbol"] + list(CORE4_MAP)
    if src.exists():
        f3 = pd.read_parquet(src, columns=cols)
    else:
        print(f"[warn] F3 快照缺失，回退全历史快照 {FULLHIST_SNAP}", flush=True)
        f3 = pd.read_parquet(FULLHIST_SNAP, columns=cols,
                             filters=[("date", ">=", pd.Timestamp(since))])
    f3["date"] = pd.to_datetime(f3["date"])
    return f3[f3["date"] >= pd.Timestamp(since)].reset_index(drop=True)


def corr_step() -> None:
    f3 = load_core4()
    p2 = pd.read_parquet(RESULTS_DIR / "factors_p2_filtered.parquet")
    p2["date"] = pd.to_datetime(p2["date"])

    sig = _sig_factor_names()
    print(f"[corr] MC 显著 P2 因子（方向统一符号）: {sig}", flush=True)
    if not sig:
        print("[corr] 无显著 P2 因子，退出", flush=True)
        return

    m = f3[["date", "symbol"] + list(CORE4_MAP)].merge(
        p2[["date", "symbol"] + list(sig)], on=["date", "symbol"], how="inner")
    for src, sgn in CORE4_MAP.items():
        m[src] = m[src] * sgn
    for name, sgn in sig.items():
        m[name] = m[name] * sgn
    m = m.rename(columns={"AMOUNT_CHG": "AMOUNT_CHG_inv",
                          "PRICE_POS60": "PRICE_POS60_inv"})
    names = ["REV10", "GAP", "AMOUNT_CHG_inv", "PRICE_POS60_inv"] + list(sig)
    m = m.dropna(subset=names)
    days = np.sort(m["date"].unique())[-250:]
    m = m[m["date"].isin(days)]
    acc = pd.DataFrame(0.0, index=names, columns=names)
    cnt = 0
    for _, cs in m.groupby("date", sort=True):
        if len(cs) < 30:
            continue
        acc += cs[names].corr(method="spearman")
        cnt += 1
    cm = acc / max(cnt, 1)
    cm.to_csv(RESULTS_DIR / "p2_vs_core_corr.csv", encoding="utf-8-sig")
    print(f"[corr] {cnt} 日均值（近250日）:", flush=True)
    print(cm.round(3).to_string(), flush=True)
    pairs = [(a, b, round(float(cm.loc[a, b]), 4))
             for i, a in enumerate(names) for b in names[i + 1:]
             if abs(cm.loc[a, b]) > CORR_THRESHOLD]
    print(f"[corr] |rho|>{CORR_THRESHOLD} 冗余对: {pairs}", flush=True)
    (RESULTS_DIR / "p2_redundant_pairs.json").write_text(
        json.dumps({"significant_direction_unified": sig, "n_days": cnt,
                    "redundant_pairs": pairs}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    # P2 因子之间相关（增强臂等权前的内部冗余提示）
    if len(sig) >= 2:
        sub = cm.loc[list(sig), list(sig)]
        sub.to_csv(RESULTS_DIR / "p2_internal_corr.csv", encoding="utf-8-sig")


# ════════════════════════════════════════════════════════════════════════
# 4. 汇总 & 主流程
# ══════════════════════════════════════════════════════════════════════
def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if np.isnan(obj) else float(obj)
    if isinstance(obj, (np.ndarray,)):
        return [_jsonable(v) for v in obj.tolist()]
    if isinstance(obj, pd.Series):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, pd.DataFrame):
        return {str(c): {str(i): _jsonable(v) for i, v in obj[c].items()} for c in obj.columns}
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, float) and np.isnan(obj):
        return None
    return obj


def summarize(results: dict) -> pd.DataFrame:
    rows = []
    for name, res in results.items():
        ev = {int(k): v for k, v in res["evaluate"].items()}
        row = {"factor": name, "dir_expect": DIRECTION_EXPECT.get(name)}
        for h in HORIZONS:
            bo = ev[h]["buy_open"]
            row[f"ic_h{h}"] = bo["rank_ic_mean"]
            row[f"icir_h{h}"] = bo["icir"]
            row[f"win_h{h}"] = bo["win_rate"]
        row["mono_h5"] = res["decile"]["monotonicity"]
        ls = res["decile"]["long_short"]
        row["ls_nav_h5"] = float(ls.iloc[-1] if isinstance(ls, pd.Series)
                                 else list(ls.values())[-1])
        if "mc" in res:
            row["mc_rank_h5"] = res["mc"]["mc_rank"]
            row["mc_pass_h5"] = res["mc"]["pass"]
        row["n_days"] = ev[1]["buy_open"]["n_days"]
        row["coverage"] = ev[1]["buy_open"]["coverage_mean"]
        rows.append(row)
    return pd.DataFrame(rows)


def prep(panel: pd.DataFrame | None = None) -> pd.DataFrame:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    snap = RESULTS_DIR / "factors_p2_filtered.parquet"
    t0 = time.perf_counter()
    if panel is None:
        panel = _prep_panel(load_panel(str(PANEL_DIR)))
        panel = panel[panel["date"] >= pd.Timestamp(SINCE)].reset_index(drop=True)
    print(f"[load] {panel['symbol'].nunique()} symbols, {len(panel)} rows "
          f"({time.perf_counter()-t0:.0f}s)", flush=True)
    universe = load_universe(PANEL_DIR)
    factors = compute_p2_factors(panel)
    mask = build_tradable_mask(panel, factors, universe)
    print(f"[filter] 四道后可交易 {int(mask.sum())}/{len(mask)} ({mask.mean():.1%}), "
          f"ST剔除={mask.attrs.get('st_excluded')}", flush=True)
    out = factors.copy()
    out.loc[~mask.to_numpy(), P2_NAMES] = np.nan
    out = apply_hygiene(out)
    out.to_parquet(snap, index=False)
    print(f"[save] {snap} ({time.perf_counter()-t0:.0f}s)", flush=True)
    return out


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = argv or sys.argv[1:]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if "--corr" in argv:
        corr_step()
        return

    snap = RESULTS_DIR / "factors_p2_filtered.parquet"
    if "--prep" in argv or not snap.exists():
        prep()
        if "--prep" in argv:
            return

    only = None
    skip_mc = "--skip-mc" in argv
    for a in argv:
        if a.startswith("--only="):
            only = a.split("=", 1)[1].split(",")

    factors_f = pd.read_parquet(snap)
    factors_f["date"] = pd.to_datetime(factors_f["date"])
    panel = _prep_panel(load_panel(str(PANEL_DIR)))
    panel = panel[panel["date"] >= pd.Timestamp(SINCE)].reset_index(drop=True)

    results = {}
    for p in sorted(RESULTS_DIR.glob("check_*.json")):
        results[p.stem.replace("check_", "")] = json.loads(p.read_text(encoding="utf-8"))

    for name in (only or P2_NAMES):
        t1 = time.perf_counter()
        print(f"[check] {name} ...", flush=True)
        fdf = to_factor_frame(factors_f, name)
        print(f"  有效行 {len(fdf)}, {fdf['date'].min().date()} ~ {fdf['date'].max().date()}",
              flush=True)
        res = {
            "evaluate": evaluate_factor(fdf, panel, horizons=HORIZONS, min_coverage=30),
            "decile": decile_analysis(fdf, panel, horizon=DECILE_HORIZON, n_groups=10),
        }
        if not skip_mc:
            res["mc"] = mc_baseline(fdf, panel, horizon=MC_HORIZON, n=MC_N, seed=MC_SEED)
        results[name] = res
        (RESULTS_DIR / f"check_{name}.json").write_text(
            json.dumps(_jsonable(res), ensure_ascii=False), encoding="utf-8")
        bo = res["evaluate"][1]["buy_open"]
        line = (f"  h1 IC={bo['rank_ic_mean']:+.4f} | "
                f"h5 IC={res['evaluate'][5]['buy_open']['rank_ic_mean']:+.4f} "
                f"ICIR={res['evaluate'][5]['buy_open']['icir']:+.2f} | "
                f"mono={res['decile']['monotonicity']:+.2f}")
        if not skip_mc:
            line += f" | MC(h5) pass={res['mc']['pass']} rank={res['mc']['mc_rank']:.2f}"
        print(line + f"  [{time.perf_counter()-t1:.0f}s]", flush=True)

    summary = summarize(results)
    summary.to_csv(RESULTS_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(summary.round(4).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()
