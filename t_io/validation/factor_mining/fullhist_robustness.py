# -*- coding: utf-8 -*-
"""短名单因子全历史稳健性补测（任务S0R）。

对 F3 短名单四因子（REV10 / GAP / AMOUNT_CHG_inv / PRICE_POS60_inv，
方向同 F3 结论、统一为做多方向）在**全历史**（1992-06 ~ 2026-09，前复权
面板 969 万行）复算 RankIC h=1/3/5 + MC，并做分时段稳定性切片，检验
F3「近 3 年强反转」结论的 regime 依赖。

口径与 F3 预注册完全一致（不为结果好看而调）：
    前瞻收益 buy_open（T+1 开盘买、持有 h 日后开盘卖），h=1/3/5
    过滤器：上市>=120交易日 + AMOUNT20>=5000万 + 剔停牌 + 剔ST（universe 当前名称）
    MC：每日截面内 shuffle，seed=42，真实 RankIC > 零假设 95% 分位为 pass
    差异：全历史 MC 降为 n=100（F3 为 n=200）——9.7M 行单次迭代 ~1-2s，
          n=200 单因子超 300s 命令上限；n=100 对 rank≈1.00/0.00 的
          强显著/强反向结论无影响，特此注明。

复用（只读 import，不修改）：
    daily_selection_screen.compute_factor_series / build_tradable_mask /
        apply_filter / to_factor_frame
    ic_layer.evaluate_factor / mc_baseline / load_panel

CLI（分步，单命令 <300s，中间结果全部落盘）：
    python fullhist_robustness.py --prep                 # 面板+因子+过滤快照
    python fullhist_robustness.py --eval --only=REV10,GAP
    python fullhist_robustness.py --eval --only=AMOUNT_CHG_inv,PRICE_POS60_inv
    python fullhist_robustness.py --mc --only=REV10      # 每因子一条命令
    python fullhist_robustness.py --report               # 切片+对照+汇总（无需面板）
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ic_layer import evaluate_factor, load_panel, mc_baseline  # noqa: E402
from volatility_screen import load_universe, _prep_panel  # noqa: E402
from daily_selection_screen import (  # noqa: E402
    apply_filter,
    build_tradable_mask,
    compute_factor_series,
    to_factor_frame,
)

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "fullhist_robustness_2026-10-09"

HORIZONS = (1, 3, 5)
MC_HORIZON = 5
MC_N = 100          # 全历史降采样（F3 近3年为 n=200），见模块 docstring
MC_SEED = 42

# 短名单四因子：快照列名 -> 报告名（方向已统一为做多）
FACTORS = ["REV10", "GAP", "AMOUNT_CHG_inv", "PRICE_POS60_inv"]

# F3 近 3 年（2023-09-01 ~ 2026-09-17）结论参照值（buy_open RankIC 均值）
F3_REF = {
    "REV10":          {"h1": 0.036, "h3": 0.045, "h5": 0.051},
    "GAP":            {"h1": 0.017, "h3": 0.021, "h5": 0.018},
    "AMOUNT_CHG_inv": {"h1": 0.041, "h3": 0.046, "h5": 0.050},  # 原列取反
    "PRICE_POS60_inv": {"h1": 0.036, "h3": 0.049, "h5": 0.056},  # 原列取反
}
NEAR3Y_START = "2023-09-01"

# 分时段切片：~3 年一段，1996 起 10 段（1992-1995 留作滚动窗口 warmup +
# 早期股票数量过少，切片从 1996 起）
SEGMENTS = [(y, y + 2) for y in range(1996, 2024, 3)]  # 10 段，末段 2023-2026
SEGMENTS[-1] = (2023, 2026)

PANEL_SNAP = RESULTS_DIR / "panel_processed.parquet"
FACTOR_SNAP = RESULTS_DIR / "factors_filtered.parquet"


def _parse(argv):
    opts = {"only": None}
    flags = set()
    for a in argv:
        if a.startswith("--only="):
            opts["only"] = a.split("=", 1)[1].split(",")
        else:
            flags.add(a)
    return flags, opts


# ---------------------------------------------------------------------------
# Step 1: 面板 + 因子 + 过滤快照
# ---------------------------------------------------------------------------
def step_prep() -> None:
    t0 = time.perf_counter()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[out] {RESULTS_DIR}", flush=True)

    print(f"[load] panel <- {PANEL_DIR}", flush=True)
    panel = _prep_panel(load_panel(str(PANEL_DIR)))
    print(f"[load] {panel['symbol'].nunique()} symbols, {len(panel)} rows, "
          f"{panel['date'].min().date()} ~ {panel['date'].max().date()} "
          f"({time.perf_counter() - t0:.0f}s)", flush=True)
    panel.to_parquet(PANEL_SNAP, index=False)
    print(f"[save] 处理后面板 -> {PANEL_SNAP}", flush=True)

    universe = load_universe(PANEL_DIR)
    print(f"[universe] {'缺失' if universe is None else f'{len(universe)} 行'}", flush=True)
    print("[factor] 计算因子（全历史向量化）...", flush=True)
    factors = compute_factor_series(panel)
    mask = build_tradable_mask(panel, factors, universe)
    print(f"[filter] 可交易 {int(mask.sum())}/{len(mask)} 行 ({mask.mean():.1%}), "
          f"ST剔除={mask.attrs.get('st_excluded')}", flush=True)
    factors_f = apply_filter(factors, mask)
    # 短名单方向统一：取反列（在原 10 因子之外追加，不动原列）
    factors_f["AMOUNT_CHG_inv"] = -factors_f["AMOUNT_CHG"]
    factors_f["PRICE_POS60_inv"] = -factors_f["PRICE_POS60"]
    factors_f.to_parquet(FACTOR_SNAP, index=False)
    print(f"[save] 因子快照 -> {FACTOR_SNAP}  总耗时 {time.perf_counter() - t0:.0f}s",
          flush=True)


def _load_snaps():
    panel = pd.read_parquet(PANEL_SNAP)
    panel["date"] = pd.to_datetime(panel["date"])
    factors_f = pd.read_parquet(FACTOR_SNAP)
    factors_f["date"] = pd.to_datetime(factors_f["date"])
    return panel, factors_f


# ---------------------------------------------------------------------------
# Step 2: 全历史 RankIC h=1/3/5（每日 IC 序列落盘供切片）
# ---------------------------------------------------------------------------
def step_eval(only) -> None:
    names = only or FACTORS
    panel, factors_f = _load_snaps()
    print(f"[snap] panel {len(panel)} 行, factors {len(factors_f)} 行", flush=True)
    for name in names:
        t1 = time.perf_counter()
        print(f"[eval] {name} ...", flush=True)
        fdf = to_factor_frame(factors_f, name)
        ev = evaluate_factor(fdf, panel, horizons=HORIZONS, min_coverage=30)
        # 每日 IC 序列（buy_open 主口径）落盘，供分时段切片
        for h in HORIZONS:
            ic = ev[h]["buy_open"]["ic_series"]
            ic.to_frame("ic").to_csv(
                RESULTS_DIR / f"ic_daily_{name}_h{h}.csv", encoding="utf-8")
        row = {"factor": name}
        for h in HORIZONS:
            bo = ev[h]["buy_open"]
            row[f"ic_h{h}"] = bo["rank_ic_mean"]
            row[f"icir_h{h}"] = bo["icir"]
            row[f"win_h{h}"] = bo["win_rate"]
            row[f"n_days_h{h}"] = bo["n_days"]
        (RESULTS_DIR / f"eval_{name}.json").write_text(
            json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  h1 IC={row['ic_h1']:+.4f} | h3 IC={row['ic_h3']:+.4f} | "
              f"h5 IC={row['ic_h5']:+.4f} ICIR={row['icir_h5']:+.2f} "
              f"win={row['win_h5']:.1%} days={row['n_days_h5']} "
              f"[{time.perf_counter() - t1:.0f}s]", flush=True)


# ---------------------------------------------------------------------------
# Step 3: MC 零假设（n=100, 每因子一条命令）
# ---------------------------------------------------------------------------
def step_mc(only) -> None:
    names = only or FACTORS
    panel, factors_f = _load_snaps()
    for name in names:
        t1 = time.perf_counter()
        print(f"[mc] {name} (n={MC_N}, seed={MC_SEED}, h={MC_HORIZON}) ...", flush=True)
        fdf = to_factor_frame(factors_f, name)
        mc = mc_baseline(fdf, panel, horizon=MC_HORIZON, n=MC_N, seed=MC_SEED)
        mc["mc_n"] = MC_N
        mc["horizon"] = MC_HORIZON
        (RESULTS_DIR / f"mc_{name}.json").write_text(
            json.dumps(mc, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  null={mc['null_mean']:+.4f}±{mc['null_std']:.4f} "
              f"mc_rank={mc['mc_rank']:.2f} pass={mc['pass']} "
              f"[{time.perf_counter() - t1:.0f}s]", flush=True)


# ---------------------------------------------------------------------------
# Step 4: 汇总 + 分时段切片 + 近3年 vs 全历史对照（无需面板）
# ---------------------------------------------------------------------------
def _rolling_3y_mean(ic: pd.Series) -> pd.Series:
    """滚动 3 年（756 交易日）窗口 IC 均值，按年末采样。"""
    r = ic.rolling(756, min_periods=500).mean()
    year_end = r.groupby(r.index.year).tail(1)
    return year_end


# 分段可靠性阈值：有效 IC 天数 >= 100 才计入稳健性判断
SEG_MIN_DAYS = 100


def step_report() -> None:
    # 0) 面板密度（结论的关键背景：截面厚度随年份的分布）
    p = pd.read_parquet(PANEL_SNAP, columns=["symbol", "date"])
    p["date"] = pd.to_datetime(p["date"])
    density = p.groupby(p["date"].dt.year)["symbol"].nunique().rename("symbols")
    density.to_frame().to_csv(RESULTS_DIR / "panel_density.csv", encoding="utf-8-sig")
    print("== 面板密度（每年在册股票数）==", flush=True)
    print(density.to_string(), flush=True)

    # 1) 总表
    rows = []
    daily = {}
    for name in FACTORS:
        row = json.loads((RESULTS_DIR / f"eval_{name}.json").read_text(encoding="utf-8"))
        mcp = RESULTS_DIR / f"mc_{name}.json"
        if mcp.exists():
            mc = json.loads(mcp.read_text(encoding="utf-8"))
            row["mc_rank_h5"] = mc["mc_rank"]
            row["mc_pass_h5"] = mc["pass"]
            row["mc_n"] = mc["mc_n"]
        rows.append(row)
        ic = pd.read_csv(RESULTS_DIR / f"ic_daily_{name}_h5.csv",
                         index_col=0, parse_dates=True)["ic"]
        daily[name] = ic
    summary = pd.DataFrame(rows)
    summary.to_csv(RESULTS_DIR / "summary_fullhist.csv", index=False, encoding="utf-8-sig")
    print("\n== 全历史总表（buy_open RankIC）==", flush=True)
    print(summary.round(4).to_string(index=False), flush=True)

    # 1.5) 每日截面覆盖数（以 REV10 有效值计，四因子同口径）
    snap = pd.read_parquet(FACTOR_SNAP, columns=["symbol", "date", "REV10"])
    snap["date"] = pd.to_datetime(snap["date"])
    cov_daily = snap.dropna(subset=["REV10"]).groupby("date").size()

    # 2) 分时段切片（IC h5）+ 可靠性标记
    seg_rows = []
    for (y0, y1) in SEGMENTS:
        seg = {"segment": f"{y0}-{y1}"}
        end = "2026-09-17" if y1 == 2026 else f"{y1}-12-31"
        cov = cov_daily.loc[f"{y0}-01-01":end]
        seg["avg_xsec"] = float(cov.mean()) if len(cov) else 0.0
        for name in FACTORS:
            s = daily[name].loc[f"{y0}-01-01":end].dropna()
            seg[f"{name}_ic"] = s.mean() if len(s) else np.nan
            seg[f"{name}_win"] = (s > 0).mean() if len(s) else np.nan
        seg["n_days"] = int(len(daily["REV10"].loc[f"{y0}-01-01":end].dropna()))
        seg["reliable"] = bool(seg["n_days"] >= SEG_MIN_DAYS)
        seg_rows.append(seg)
    seg_df = pd.DataFrame(seg_rows)
    seg_df.to_csv(RESULTS_DIR / "segments_ic_h5.csv", index=False, encoding="utf-8-sig")
    ic_cols = [f"{n}_ic" for n in FACTORS]
    show = seg_df[["segment", "n_days", "avg_xsec", "reliable"] + ic_cols]
    print("\n== 分时段 IC h5（~3年/段；n_days<100 为样本不足，不参与稳健性判断）==",
          flush=True)
    print(show.round(4).to_string(index=False), flush=True)
    rel = seg_df[seg_df["reliable"]]
    rev_sign = np.sign(rel["REV10_ic"].dropna())
    print(f"\nREV10 可靠时段全同号: {(rev_sign == rev_sign.iloc[0]).all()} "
          f"(可靠段符号: {[f'{s:+.4f}' for s in rel['REV10_ic']]})", flush=True)

    # 3) 近3年 vs 全历史对照 + 分位
    cmp_rows = []
    pct_rows = []
    for name in FACTORS:
        ic = daily[name].dropna()
        near = ic.loc[NEAR3Y_START:]
        near_mean = float(near.mean())
        full_mean = float(ic.mean())
        roll = _rolling_3y_mean(ic).dropna()
        pct = float((roll < near_mean).mean()) if len(roll) else np.nan
        cmp_rows.append({
            "factor": name,
            "full_ic_h5": full_mean,
            "near3y_recalc": near_mean,
            "f3_ref_h5": F3_REF[name]["h5"],
            "diff_near_minus_full": near_mean - full_mean,
            "near3y_pct_of_rolling3y": pct,
            "n_days_full": len(ic),
            "n_days_near": len(near),
        })
        pct_rows.append({"factor": name, "rolling3y_latest": float(roll.iloc[-1]),
                         "rolling3y_min": float(roll.min()), "rolling3y_max": float(roll.max())})
    cmp_df = pd.DataFrame(cmp_rows)
    cmp_df.to_csv(RESULTS_DIR / "near3y_vs_fullhist.csv", index=False, encoding="utf-8-sig")
    print("\n== 近3年 vs 全历史（IC h5）==", flush=True)
    print(cmp_df.round(4).to_string(index=False), flush=True)

    # 4) REV10 滚动 3 年 IC 轨迹（regime 检测）
    rev_roll = _rolling_3y_mean(daily["REV10"].dropna()).dropna()
    rev_roll.to_frame("rev10_rolling3y_ic").to_csv(
        RESULTS_DIR / "rev10_rolling3y_ic.csv", encoding="utf-8-sig")
    print("\n== REV10 滚动3年 IC h5 轨迹（年末采样）==", flush=True)
    print(rev_roll.round(4).to_string(), flush=True)

    print(f"\n[done] 产物 -> {RESULTS_DIR}", flush=True)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    flags, opts = _parse(sys.argv[1:])
    if "--prep" in flags:
        step_prep()
    elif "--eval" in flags:
        step_eval(opts["only"])
    elif "--mc" in flags:
        step_mc(opts["only"])
    elif "--report" in flags:
        step_report()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
