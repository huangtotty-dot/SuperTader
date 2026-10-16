# -*- coding: utf-8 -*-
"""弱转强研究线 · 实验26b：B方案（受限顺移）**上线闸门指标** 补充验证（2026-10-16）
================================================================================
exp26 已给出三口径收益序列（A 幻想臂 / B 真实现状 / C 顺移）。本实验**不改任何
生产代码、不改 exp26 口径**，只补四件 owner 闸门需要而 exp26 没直接给出的东西：

1. **成交率提升**：现状实际成交腿数 vs 顺移后成交腿数（占 top-4 意图腿数比）。
2. **冻资变化**：封板腿 10:00 挂市价单 ⇒ 现状冻到 15:00（GM 仿真 15:00 status=12
   拒单释放，10-14 事故实证）；B方案 10:08 撤单 ⇒ 只冻 8 分钟。
   冻资名义 = 封板腿数 × osc_single_budget（10 万，params.py:238）。
3. **闸门判定（owner 2026-10-16 口径）**：Sharpe 下降 <10% 且年化收益不降。
   基准 = **B 真实现状**（生产里买不进 ⇒ 那笔仓位就是没有；exp26 已论证 A 是
   不可实现的幻想臂，只作上限参照）。
4. **稳健性**：封板容差 SEAL_TOL × 顺移入场价代理（10:30 收盘=保守 / 10:00
   收盘=乐观）扫描，看 C vs B 的结论是否依赖某个点估计。

口径与已知近似全部继承 exp26（30min 粒度、申万一级板块、同宇宙同成本同持有期），
偏差方向：**对 C 偏保守**（30min 粒度低估"短暂封板又打开"，顺移入场价代理偏晚）。

运行：
    python t_io/validation/weak_strong/exp26b_backfill_gate_metrics.py
产物：results/exp26b_backfill_gate_metrics.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from exp6_exit_rules import build_signal                                   # noqa: E402
from exp12_sector_filter import portfolio_stats, top_boards                # noqa: E402
from exp26_osc_backfill import (                                           # noqa: E402
    DATA_DIR, INDUSTRY_CSV, LU_THRESHOLD, MIN_POOL, TOP_N, BOARD_TOP,
    board_heat_lu, limit_up_price, sw_board_map,
)

OUT_DIR = HERE / "results"

# ── 生产参数（params.py，只读引用，不改）──
SINGLE_BUDGET = 100000.0     # osc_single_budget
FREEZE_MIN_STATUS_QUO = 300  # 10:00 挂单 → 15:00 拒单释放（10-14 bridge 事件实证）
FREEZE_MIN_BACKFILL = 8      # 10:00 挂单 → 10:08 撤单（osc_backfill_first_hhmm=1008）

# 稳健性扫描网格
SEAL_TOLS = [0.0, 0.001, 0.003, 0.005]
ENTRY_PROXIES = {"conservative_1030": "close_1030", "optimistic_1000": "sig_close"}


def build_candidates(seal_tol: float):
    """复刻 exp26 主循环前半段：信号 → 板块过滤 → 可成交性标注。返回逐日候选。"""
    panel = pd.read_parquet(DATA_DIR / "min30_panel_full.parquet")
    daily = pd.read_parquet(DATA_DIR / "daily_full.parquet")
    sig = build_signal(panel, daily)
    sw = sw_board_map()
    sig["board"] = sig["symbol"].str.split(".").str[0].map(sw)
    sig = sig.dropna(subset=["board"]).copy()
    sig["gap"] = sig["sig_close"] / sig["prev_close"] - 1.0
    heat = board_heat_lu(daily, sw)
    tops = top_boards(heat, BOARD_TOP)

    pan = panel.sort_values(["symbol", "dt"], kind="mergesort").reset_index(drop=True)
    pan["bar_idx"] = pan.groupby(["symbol", "date"]).cumcount()
    b2 = pan[pan["bar_idx"] == 2][["symbol", "date", "low", "close"]].rename(
        columns={"low": "low_1030", "close": "close_1030"})
    sig = sig.merge(b2, on=["symbol", "date"], how="left")
    sig["limit_px"] = [limit_up_price(pc, str(s).split(".")[0])
                       for pc, s in zip(sig["prev_close"], sig["symbol"])]
    sig["sealed"] = sig["low_1030"] >= sig["limit_px"] * (1 - seal_tol)
    dates = sorted(sig["date"].unique())
    return sig, tops, dates


def run_arms(sig, tops, dates, entry_col: str):
    """复刻 exp26 三口径逐日循环；C 的顺移入场价用 entry_col。返回 (A,B,C,per_day)。"""
    rows_a, rows_b, rows_c, per_day = [], [], [], []
    for dt, g in sig.groupby("date"):
        hot = tops.get(dt) or set()
        if not hot:
            continue
        cand = g[g["board"].isin(hot)].sort_values("gap", ascending=False)
        if len(cand) < MIN_POOL:
            continue
        top = cand.head(TOP_N)
        n_sealed = int(top["sealed"].sum())
        rows_a.append(top)
        keep = top[~top["sealed"]]
        rows_b.append(keep)
        need = TOP_N - len(keep)
        rest = cand.iloc[TOP_N:]
        add = rest[~rest["sealed"]].head(max(0, need)) if need > 0 else rest.head(0)
        rows_c.append(pd.concat([keep, add]))
        per_day.append({"date": str(dt), "n_top": len(top), "n_sealed": n_sealed,
                        "n_fill_quo": len(keep), "n_shifted": len(add),
                        "n_fill_backfill": len(keep) + len(add),
                        "sealed_gap_max": float(top[top["sealed"]]["gap"].max())
                        if n_sealed else None})

    A = pd.concat(rows_a, ignore_index=True) if rows_a else pd.DataFrame()
    B = pd.concat(rows_b, ignore_index=True) if rows_b else pd.DataFrame()
    C = pd.concat(rows_c, ignore_index=True) if rows_c else pd.DataFrame()
    for df_ in (A, B, C):
        if len(df_):
            df_["close"] = df_["sig_close"]
    if len(C):
        A_keys = set(zip(A["symbol"], A["date"])) if len(A) else set()
        shifted_mask = pd.Series([(s, d) not in A_keys
                                  for s, d in zip(C["symbol"], C["date"])], index=C.index)
        C.loc[shifted_mask, "close"] = C.loc[shifted_mask, entry_col].values
    return A, B, C, per_day


def _st(df_, dates):
    sub = df_.dropna(subset=["close_1030"]) if len(df_) else df_
    if not len(sub):
        return {"ann": float("nan"), "sharpe": float("nan"), "mdd": float("nan"),
                "n_trades": 0}
    return portfolio_stats(sub, dates, "all")


def main() -> None:
    t0 = time.time()
    print("构建信号与候选（exp26 同口径）…")
    # 基准网格点 seal_tol=0.001 先跑（冻资/成交率用它，与 exp26 判定一致）
    sig, tops, dates = build_candidates(seal_tol=0.001)
    A, B, C, per_day = run_arms(sig, tops, dates, "close_1030")

    # ── ① 成交率 ──
    n_intended = sum(d["n_top"] for d in per_day)
    n_fill_quo = sum(d["n_fill_quo"] for d in per_day)
    n_fill_bf = sum(d["n_fill_backfill"] for d in per_day)
    n_sealed = sum(d["n_sealed"] for d in per_day)
    fill_rate = {
        "intended_legs": n_intended,
        "status_quo_filled": n_fill_quo,
        "status_quo_rate": n_fill_quo / n_intended,
        "backfill_filled": n_fill_bf,
        "backfill_rate": n_fill_bf / n_intended,
        "uplift_legs": n_fill_bf - n_fill_quo,
        "unfilled_even_with_backfill": n_intended - n_fill_bf,
        "note": "顺移后仍缺口的腿 = 当日候补池耗尽（候选深度不足），非机制失败",
    }

    # ── ② 冻资 ──
    sealed_days = [d for d in per_day if d["n_sealed"] > 0]
    max_sealed_day = max((d["n_sealed"] for d in per_day), default=0)
    freeze = {
        "sealed_legs_total": n_sealed,
        "days_with_sealed": len(sealed_days),
        "max_sealed_in_one_day": max_sealed_day,
        "single_budget": SINGLE_BUDGET,
        "status_quo": {
            "freeze_minutes_per_leg": FREEZE_MIN_STATUS_QUO,
            "max_daily_frozen_nominal": max_sealed_day * SINGLE_BUDGET,
            "total_capital_time_yuan_min": n_sealed * SINGLE_BUDGET * FREEZE_MIN_STATUS_QUO,
        },
        "backfill_B": {
            "freeze_minutes_per_leg": FREEZE_MIN_BACKFILL,
            "max_daily_frozen_nominal": max_sealed_day * SINGLE_BUDGET,
            "total_capital_time_yuan_min": n_sealed * SINGLE_BUDGET * FREEZE_MIN_BACKFILL,
            "note": "名义峰值相同（10:00~10:08 仍冻），但 10:08 撤单释放；"
                    "顺移单只打给非封死候选，假设秒成，不再产生新冻结",
        },
        "capital_time_reduction": 1 - FREEZE_MIN_BACKFILL / FREEZE_MIN_STATUS_QUO,
    }

    # ── ③ 闸门（基准点：seal_tol=0.001, 入场=10:30 保守）──
    sa, sb, sc = (_st(A, dates), _st(B, dates), _st(C, dates))

    def gate(s_base, s_c):
        """owner 2026-10-16 口径：Sharpe 相对下降 <10% 且年化收益不降。"""
        d_sharpe = s_c["sharpe"] - s_base["sharpe"]
        d_sharpe_rel = d_sharpe / abs(s_base["sharpe"]) if s_base["sharpe"] else float("nan")
        d_ann = s_c["ann"] - s_base["ann"]
        return {"sharpe_base": s_base["sharpe"], "sharpe_c": s_c["sharpe"],
                "delta_sharpe": d_sharpe, "delta_sharpe_rel": d_sharpe_rel,
                "ann_base": s_base["ann"], "ann_c": s_c["ann"], "delta_ann": d_ann,
                "mdd_base": s_base["mdd"], "mdd_c": s_c["mdd"],
                "pass": bool(np.isfinite(d_sharpe_rel) and d_sharpe_rel > -0.10
                             and d_ann >= 0)}

    gate_vs_b = gate(sb, sc)
    gate_vs_a = gate(sa, sc)

    # ── ④ 稳健性网格（C vs B，两个入场代理 × 四个封板容差）──
    grid = []
    for tol in SEAL_TOLS:
        if abs(tol - 0.001) < 1e-12:
            sig_t, tops_t = sig, tops
        else:
            sig_t, tops_t, _ = build_candidates(seal_tol=tol)
        for proxy_name, entry_col in ENTRY_PROXIES.items():
            A_t, B_t, C_t, pd_t = run_arms(sig_t, tops_t, dates, entry_col)
            s_b, s_c = _st(B_t, dates), _st(C_t, dates)
            n_sealed_t = sum(int(x["n_sealed"]) for x in pd_t)
            g = gate(s_b, s_c)
            g.update({"seal_tol": tol, "entry_proxy": proxy_name,
                      "sealed_legs": n_sealed_t})
            grid.append(g)
            print(f"  tol={tol:.3f} entry={proxy_name:<18} "
                  f"B={s_b['sharpe']:.3f} C={s_c['sharpe']:.3f} "
                  f"Δrel={g['delta_sharpe_rel']:+.1%} 过闸={g['pass']}")

    # gap 分桶封板率（基准 tol）
    top_all = A.copy()
    buckets = {"gap<5%": (top_all["gap"] < 0.05),
               "gap 5~8%": ((top_all["gap"] >= 0.05) & (top_all["gap"] < 0.08)),
               "gap>=8%": (top_all["gap"] >= 0.08)}
    gap_bucket = {k: {"n": int(m.sum()), "sealed": int(top_all.loc[m, "sealed"].sum()),
                      "sealed_rate": float(top_all.loc[m, "sealed"].mean()) if m.sum() else None}
                  for k, m in buckets.items()}

    res = {
        "window": [str(dates[0]), str(dates[-1])], "n_days": len(dates),
        "baseline_point": {"seal_tol": 0.001, "entry_proxy": "conservative_1030"},
        "fill_rate": fill_rate,
        "freeze": freeze,
        "gap_bucket_sealed": gap_bucket,
        "gate_primary_C_vs_B": gate_vs_b,
        "gate_reference_C_vs_A_fantasy": {**gate_vs_a,
            "note": "A 是幻想臂（假设买不进的也按 10:00 收盘成交），仅作上限参照，不驱动闸门"},
        "robustness_grid_C_vs_B": grid,
        "robustness_summary": {
            "all_pass": bool(all(g["pass"] for g in grid)),
            "min_delta_sharpe_rel": min(g["delta_sharpe_rel"] for g in grid),
            "max_delta_sharpe_rel": max(g["delta_sharpe_rel"] for g in grid),
        },
        "known_approximations": [
            "可成交性=30min 棒贴涨停（方案原文要 5/15min，面板只有 30min ⇒ 对顺移偏保守）",
            "顺移入场价=10:30 收盘代理 10:08~10:14（偏晚 ⇒ 对 C 偏保守）",
            "稳健性网格含 10:00 收盘乐观端，C vs B 结论在两个端点间夹逼",
            "非封死候选假设市价单秒成（10-14 凌玮科技实证：市价单 10:00:02 成交）",
        ],
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fp = OUT_DIR / "exp26b_backfill_gate_metrics.json"
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2, default=float)

    print("\n" + "=" * 74)
    print(f"成交率：现状 {n_fill_quo}/{n_intended}={fill_rate['status_quo_rate']:.1%} ⇒ "
          f"顺移 {n_fill_bf}/{n_intended}={fill_rate['backfill_rate']:.1%} "
          f"（+{fill_rate['uplift_legs']} 腿；仍缺 {fill_rate['unfilled_even_with_backfill']} 腿=候补耗尽）")
    print(f"冻资：封板腿 {n_sealed} 条 / 涉及 {len(sealed_days)} 日 / 单日最多 {max_sealed_day} 条"
          f"（{max_sealed_day*SINGLE_BUDGET/1e4:.0f} 万）；"
          f"资本·时间占用 {n_sealed*SINGLE_BUDGET*FREEZE_MIN_STATUS_QUO/1e8:.2f} ⇒ "
          f"{n_sealed*SINGLE_BUDGET*FREEZE_MIN_BACKFILL/1e8:.3f} 亿元·分钟 "
          f"（−{freeze['capital_time_reduction']:.1%}）")
    print(f"gap 分桶封板率：" + "  ".join(
        f"{k}:{v['sealed']}/{v['n']}({(v['sealed_rate'] or 0):.0%})"
        for k, v in gap_bucket.items()))
    print(f"\n闸门（C vs B 真实现状）：ΔSharpe={gate_vs_b['delta_sharpe']:+.3f} "
          f"({gate_vs_b['delta_sharpe_rel']:+.1%})  Δann={gate_vs_b['delta_ann']*100:+.1f}pp "
          f"⇒ {'✅ 过闸' if gate_vs_b['pass'] else '❌ 破闸'}")
    print(f"参照（C vs A 幻想臂）：ΔSharpe={gate_vs_a['delta_sharpe']:+.3f} "
          f"({gate_vs_a['delta_sharpe_rel']:+.1%}) ⇒ {'✅' if gate_vs_a['pass'] else '❌（不驱动闸门）'}")
    print(f"稳健性：{len(grid)} 网格点 全部过闸={res['robustness_summary']['all_pass']} "
          f"ΔSharpe_rel ∈ [{res['robustness_summary']['min_delta_sharpe_rel']:+.1%}, "
          f"{res['robustness_summary']['max_delta_sharpe_rel']:+.1%}]")
    print(f"产物 {fp}  用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
