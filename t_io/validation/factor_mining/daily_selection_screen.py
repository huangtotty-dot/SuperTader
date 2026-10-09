# -*- coding: utf-8 -*-
"""日级选股因子批量体检（任务F3）—— 选股因子驱动战略转向的首轮因子筛选。

因子清单（全部 t 日收盘可得，严格无未来函数）：
    REV5        -ret5           短期反转（5日）
    REV10       -ret10          短期反转（10日）
    MOM20       ret20           中期动量
    MOM60       ret60           长动量
    TURN_PROXY  volume/mean(volume,20)   量比
    AMP_POS     (close-low)/(high-low)   日内收盘位置
    GAP         open/Ref(close,1)-1      隔夜跳空
    VOLAT_RATIO std(ret,5)/std(ret,60)   波动突变
    PRICE_POS60 (close-min(low,60))/(max(high,60)-min(low,60))  60日通道位置
    AMOUNT_CHG  amount/mean(amount,20)   成交额突变

口径（与 volatility_screen 预注册口径对齐，不为结果好看而调）：
    前瞻收益 buy_open：T+1 开盘买、持有 h 日后开盘卖，h=1/3/5（close 口径参考）
    过滤器：上市>=120交易日 + AMOUNT20>=5000万 + 剔停牌(volume=0) + 剔ST（universe 当前名称）
    MC：n=200 seed=42，每日截面内 shuffle，真实 RankIC > 零假设 95% 分位为 pass
    主判窗 MC_HORIZON=5，辅判 h=1（--mc-h1 时加跑）
    冗余：MC pass 因子两两截面 Spearman 相关均值，|rho|>0.7 标注冗余

CLI：
    python daily_selection_screen.py                        # 全量（因子计算+全检+相关）
    python daily_selection_screen.py --only=REV5,REV10      # 只跑指定因子全检
    python daily_selection_screen.py --use-snapshot         # 复用因子快照
    python daily_selection_screen.py --skip-mc              # 跳过 MC（快速出 IC）
    python daily_selection_screen.py --corr                 # 只跑相关性（需 summary.json 已存在）
    python daily_selection_screen.py --since=2023-09-01     # 面板截断
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from ic_layer import decile_analysis, evaluate_factor, load_panel, mc_baseline
    from volatility_screen import (
        MIN_AMOUNT20,
        MIN_LISTED_DAYS,
        _prep_panel,
        _roll,
        load_universe,
        _norm_sym,
    )
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ic_layer import decile_analysis, evaluate_factor, load_panel, mc_baseline
    from volatility_screen import (
        MIN_AMOUNT20,
        MIN_LISTED_DAYS,
        _prep_panel,
        _roll,
        load_universe,
        _norm_sym,
    )

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "daily_selection_screen_2026-10-09"

HORIZONS = (1, 3, 5)
DECILE_HORIZON = 5
MC_HORIZON = 5
MC_N = 200
MC_SEED = 42
CORR_THRESHOLD = 0.7

FACTOR_NAMES = [
    "REV5", "REV10", "MOM20", "MOM60", "TURN_PROXY",
    "AMP_POS", "GAP", "VOLAT_RATIO", "PRICE_POS60", "AMOUNT_CHG",
]


# ════════════════════════════════════════════════════════════════════════
# 1. 因子计算（面板级向量化，全部 t 日收盘可得）
# ════════════════════════════════════════════════════════════════════════
def compute_factor_series(panel: pd.DataFrame) -> pd.DataFrame:
    df = _prep_panel(panel)
    g = df["symbol"]

    close_prev = df["close"].groupby(g, sort=False).shift(1)
    ret1 = df["close"] / close_prev - 1.0                      # 日收益

    ret5 = df["close"] / df["close"].groupby(g, sort=False).shift(5) - 1.0
    ret10 = df["close"] / df["close"].groupby(g, sort=False).shift(10) - 1.0
    ret20 = df["close"] / df["close"].groupby(g, sort=False).shift(20) - 1.0
    ret60 = df["close"] / df["close"].groupby(g, sort=False).shift(60) - 1.0

    vol20 = _roll(df["volume"], g, 20, "mean")
    amt20 = _roll(df["amount"], g, 20, "mean")
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    std5 = _roll(ret1, g, 5, "std")
    std60 = _roll(ret1, g, 60, "std")
    min_low60 = _roll(df["low"], g, 60, "min")
    max_high60 = _roll(df["high"], g, 60, "max")
    ch_range = (max_high60 - min_low60).replace(0, np.nan)

    out = df[["symbol", "date"]].copy()
    out["REV5"] = -ret5
    out["REV10"] = -ret10
    out["MOM20"] = ret20
    out["MOM60"] = ret60
    out["TURN_PROXY"] = df["volume"] / vol20.replace(0, np.nan)
    out["AMP_POS"] = (df["close"] - df["low"]) / hl_range
    out["GAP"] = df["open"] / close_prev - 1.0
    out["VOLAT_RATIO"] = std5 / std60.replace(0, np.nan)
    out["PRICE_POS60"] = (df["close"] - min_low60) / ch_range
    out["AMOUNT_CHG"] = df["amount"] / amt20.replace(0, np.nan)
    out["AMOUNT20"] = amt20                                    # 过滤器辅助列
    # 停牌日 volume=0：全部置 NaN
    out.loc[df["volume"] <= 0, FACTOR_NAMES] = np.nan
    return out


# ════════════════════════════════════════════════════════════════════════
# 2. 可交易性过滤器（与 volatility_screen 同口径）
# ════════════════════════════════════════════════════════════════════════
def build_tradable_mask(panel: pd.DataFrame, factors: pd.DataFrame,
                        universe: pd.DataFrame | None = None) -> pd.Series:
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
    mask.attrs["st_filter_active"] = bool(st_names) or (
        universe is not None and "sec_name" in universe.columns)
    return mask


def apply_filter(factors: pd.DataFrame, mask: pd.Series) -> pd.DataFrame:
    out = factors.copy()
    out.loc[~mask.to_numpy(), FACTOR_NAMES] = np.nan
    return out


def to_factor_frame(factors: pd.DataFrame, name: str) -> pd.DataFrame:
    f = factors[["date", "symbol"]].copy()
    f["value"] = factors[name]
    return f.dropna(subset=["value"]).reset_index(drop=True)


# ════════════════════════════════════════════════════════════════════════
# 3. 相关性冗余检查（MC pass 因子两两截面 Spearman 相关均值）
# ════════════════════════════════════════════════════════════════════════
def pairwise_corr(factors_filtered: pd.DataFrame, names: list[str],
                  max_days: int = 250) -> pd.DataFrame:
    """每日截面 spearman 相关，跨日取均值。最多取最近 max_days 个交易日。"""
    f = factors_filtered[["date", "symbol"] + names].dropna()
    days = np.sort(f["date"].unique())[-max_days:]
    f = f[f["date"].isin(days)]
    acc = pd.DataFrame(0.0, index=names, columns=names)
    cnt = 0
    for _, cs in f.groupby("date", sort=True):
        if len(cs) < 30:
            continue
        acc += cs[names].corr(method="spearman")
        cnt += 1
    return acc / max(cnt, 1)


# ════════════════════════════════════════════════════════════════════════
# 4. 汇总表 & JSON
# ════════════════════════════════════════════════════════════════════════
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
        row = {"factor": name}
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
        if "mc_h1" in res:
            row["mc_rank_h1"] = res["mc_h1"]["mc_rank"]
            row["mc_pass_h1"] = res["mc_h1"]["pass"]
        row["n_days"] = ev[1]["buy_open"]["n_days"]
        row["coverage"] = ev[1]["buy_open"]["coverage_mean"]
        rows.append(row)
    return pd.DataFrame(rows)


# ════════════════════════════════════════════════════════════════════════
# 5. 主流程
# ════════════════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    argv = argv or sys.argv[1:]
    only = None
    skip_mc = "--skip-mc" in argv
    mc_h1 = "--mc-h1" in argv
    use_snapshot = "--use-snapshot" in argv
    do_corr_only = "--corr" in argv
    since = None
    for a in argv:
        if a.startswith("--only="):
            only = a.split("=", 1)[1].split(",")
        elif a.startswith("--since="):
            since = a.split("=", 1)[1]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[out] {RESULTS_DIR}", flush=True)

    if do_corr_only:
        factors_f = pd.read_parquet(RESULTS_DIR / "factors_filtered.parquet")
        factors_f["date"] = pd.to_datetime(factors_f["date"])
        results = json.loads((RESULTS_DIR / "factor_health_all.json").read_text(encoding="utf-8"))
        passed = [n for n, r in results.items()
                  if (r.get("mc") or {}).get("pass") or (r.get("mc_h1") or {}).get("pass")]
        print(f"[corr] MC pass 因子: {passed}", flush=True)
        if len(passed) >= 2:
            cm = pairwise_corr(factors_f, passed)
            cm.to_csv(RESULTS_DIR / "passed_corr.csv", encoding="utf-8-sig")
            print(cm.round(3).to_string(), flush=True)
            pairs = [(a, b, float(cm.loc[a, b]))
                     for i, a in enumerate(passed) for b in passed[i + 1:]
                     if abs(cm.loc[a, b]) > CORR_THRESHOLD]
            print(f"[corr] |rho|>{CORR_THRESHOLD} 冗余对: {pairs}", flush=True)
            (RESULTS_DIR / "redundant_pairs.json").write_text(
                json.dumps(_jsonable(pairs), ensure_ascii=False, indent=2), encoding="utf-8")
        return

    t0 = time.perf_counter()
    print(f"[load] panel <- {PANEL_DIR}", flush=True)
    panel = _prep_panel(load_panel(str(PANEL_DIR)))
    if since:
        panel = panel[panel["date"] >= pd.Timestamp(since)].reset_index(drop=True)
        print(f"[trim] --since={since} -> {len(panel)} 行", flush=True)
    print(f"[load] {panel['symbol'].nunique()} symbols, {len(panel)} rows, "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()} "
          f"({time.perf_counter() - t0:.0f}s)", flush=True)

    snap = RESULTS_DIR / "factors_filtered.parquet"
    if use_snapshot and snap.exists():
        factors_f = pd.read_parquet(snap)
        factors_f["date"] = pd.to_datetime(factors_f["date"])
        print(f"[factor] 复用快照 ({len(factors_f)} 行)", flush=True)
    else:
        universe = load_universe(PANEL_DIR)
        print(f"[universe] {'缺失' if universe is None else f'{len(universe)} 行'}", flush=True)
        print("[factor] 计算因子 ...", flush=True)
        factors = compute_factor_series(panel)
        mask = build_tradable_mask(panel, factors, universe)
        print(f"[filter] 可交易 {int(mask.sum())}/{len(mask)} 行 ({mask.mean():.1%}), "
              f"ST剔除={mask.attrs.get('st_excluded')}", flush=True)
        factors_f = apply_filter(factors, mask)
        factors_f.to_parquet(snap, index=False)
        print(f"[save] 因子快照 -> {snap}", flush=True)

    # 汇总历史已完成的单因子结果（支持分批跑）
    results = {}
    for p in sorted(RESULTS_DIR.glob("check_*.json")):
        results[p.stem.replace("check_", "")] = json.loads(p.read_text(encoding="utf-8"))

    names = only or FACTOR_NAMES
    for name in names:
        t1 = time.perf_counter()
        print(f"[check] {name} ...", flush=True)
        fdf = to_factor_frame(factors_f, name)
        res = {
            "evaluate": evaluate_factor(fdf, panel, horizons=HORIZONS, min_coverage=30),
            "decile": decile_analysis(fdf, panel, horizon=DECILE_HORIZON, n_groups=10),
        }
        if not skip_mc:
            res["mc"] = mc_baseline(fdf, panel, horizon=MC_HORIZON, n=MC_N, seed=MC_SEED)
            if mc_h1:
                res["mc_h1"] = mc_baseline(fdf, panel, horizon=1, n=MC_N, seed=MC_SEED)
        results[name] = res
        (RESULTS_DIR / f"check_{name}.json").write_text(
            json.dumps(_jsonable(res), ensure_ascii=False), encoding="utf-8")
        bo = res["evaluate"][1]["buy_open"]
        line = (f"  h1 IC={bo['rank_ic_mean']:+.4f} ICIR={bo['icir']:+.2f} | "
                f"h5 IC={res['evaluate'][5]['buy_open']['rank_ic_mean']:+.4f} | "
                f"mono={res['decile']['monotonicity']:+.2f}")
        if not skip_mc:
            line += f" | MC(h5) pass={res['mc']['pass']} rank={res['mc']['mc_rank']:.2f}"
            if mc_h1:
                line += f" | MC(h1) pass={res['mc_h1']['pass']}"
        print(line + f"  [{time.perf_counter() - t1:.0f}s]", flush=True)

    (RESULTS_DIR / "factor_health_all.json").write_text(
        json.dumps(_jsonable(results), ensure_ascii=False), encoding="utf-8")
    summary = summarize(results)
    summary.to_csv(RESULTS_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print("[summary]", flush=True)
    print(summary.round(4).to_string(index=False), flush=True)
    print(f"[done] {RESULTS_DIR}  总耗时 {time.perf_counter() - t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
