# -*- coding: utf-8 -*-
"""S2-T1 step2: 裁窗 2025-03-31~2026-09-17 重跑 S1 终选配置（N4/M8/H1/TP=A, score_eq）
作为 α 基准，并落「每日开盘成交后持仓快照」供做T叠加层使用。

仿真逻辑逐行复刻 s1_sim.s1_run_sim（常量直接 import，不改 s1_* 文件），新增：
  · 每日 open 阶段结束后记录持仓 (date, col, code, qty, entry_d, day_open_px, slot_value,
    eligible=entry_d<d —— T+1 口径：当日新买槽位不可开出卖腿)；
  · 记录当日 open 买入后剩余现金（做T腿资金可行性披露用）；
  · 腿账与底仓分账：本脚本不做腿，只产出快照。

输出 results/s2_ogr/:
  nav_alpha.csv            α 基准净值（裁窗全段）
  holdings_daily.parquet   每日持仓快照
  cash_daily.csv           每日 open 后现金
  alpha_metrics.json       α 全窗指标
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

import s1_sim
from s1_sim import FEE, INIT_CASH, STOP_LOSS, TP_TIERS, TRAIL_ACT, TRAIL_DD

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "s2_ogr"

WIN_START = pd.Timestamp("2025-03-31")     # 30min 覆盖起点
WIN_END = pd.Timestamp("2026-09-17")       # 仿真主窗终点
CFG = dict(n_slots=4, buffer_m=8, min_hold=1, tp_arm="A")   # S1 终选


def run_with_holdings(arm, score, opn, cls, col_of, dates,
                      n_slots, buffer_m, min_hold):
    """逐行复刻 s1_run_sim（proceeds_lag=0），增加持仓快照记录。"""
    top_n = n_slots
    n_days, n_sym = score.shape
    rank = np.full_like(score, np.inf, dtype=np.float64)
    for d in range(n_days):
        s = score[d]
        ok = np.isfinite(s)
        if ok.sum() == 0:
            continue
        order = np.argsort(-s[ok], kind="mergesort")
        r = np.empty(ok.sum(), dtype=np.float64)
        r[order] = np.arange(1, ok.sum() + 1)
        row = np.full(n_sym, np.inf)
        row[np.flatnonzero(ok)] = r
        rank[d] = row

    cash = INIT_CASH
    pos: dict[int, dict] = {}
    sell_plan: list[tuple[int, int, str]] = []
    buy_plan: list[int] = []
    buy_plan_target: dict[int, float] = {}
    nav_hist = np.full(n_days, np.nan)
    expo_hist = np.zeros(n_days)
    fee_total = 0.0
    rot_count = 0
    hold_recs = []
    cash_hist = np.full(n_days, np.nan)

    def holdings_value(d):
        v = 0.0
        for c, p in pos.items():
            px = cls[d, c]
            if np.isfinite(px):
                v += p["qty"] * px
        return v

    for d in range(n_days):
        day_open = opn[d]
        for c, q, reason in sell_plan:
            p = pos.get(c)
            px = day_open[c]
            if p is None or not np.isfinite(px) or px <= 0:
                continue
            q = min(q, p["qty"])
            proceeds = q * px * (1.0 - FEE)
            cash += proceeds
            fee_total += q * px * FEE
            p["qty"] -= q
            if p["qty"] <= 0:
                del pos[c]
        sell_plan = []
        for c in buy_plan:
            px = day_open[c]
            if not np.isfinite(px) or px <= 0:
                continue
            target = buy_plan_target[c]
            q = int(min(target, cash) / (px * (1.0 + FEE)) / 100.0) * 100
            if q <= 0:
                continue
            cost_amt = q * px * (1.0 + FEE)
            cash -= cost_amt
            fee_total += q * px * FEE
            pos[c] = dict(qty=q, cost=px, entry_d=d, tiers=0, peak=-np.inf)
        buy_plan = []

        # ── S2 新增：open 阶段结束后的持仓快照（做T腿的底仓基础）──
        cash_hist[d] = cash
        for c, p in pos.items():
            px = day_open[c]
            if not np.isfinite(px) or px <= 0:
                continue
            hold_recs.append(dict(
                date=dates[d], col=c, code=col_of[c], qty=p["qty"],
                entry_d=int(p["entry_d"]), day_idx=d, open_px=float(px),
                slot_value=float(p["qty"] * px),
                eligible=bool(p["entry_d"] < d)))

        day_close = cls[d]
        for c, p in pos.items():
            px = day_close[c]
            if np.isfinite(px):
                p["peak"] = max(p["peak"], px)
        nav = cash + holdings_value(d)
        nav_hist[d] = nav
        hv = holdings_value(d)
        expo_hist[d] = hv / nav if nav > 0 else 0.0

        rank_row = rank[d]
        exits: dict[int, tuple[int, str]] = {}
        for c, p in list(pos.items()):
            px = day_close[c]
            if not np.isfinite(px):
                continue
            ret = px / p["cost"] - 1.0
            if ret <= STOP_LOSS:
                exits[c] = (p["qty"], "SL12")
                continue
            if arm == "A":
                q_sell = 0
                for k, (th, frac) in enumerate(TP_TIERS):
                    if k < p["tiers"]:
                        continue
                    if ret >= th:
                        p["tiers"] = k + 1
                        if frac >= 1.0:
                            q_sell = p["qty"]
                            break
                        q_sell += max(int(p["qty"] * frac / 100) * 100, 100)
                if q_sell > 0:
                    exits[c] = (min(q_sell, p["qty"]), "TP_FIX")
            elif arm == "B":
                act = p["peak"] / p["cost"] - 1.0 >= TRAIL_ACT
                if act and px <= p["peak"] * (1.0 - TRAIL_DD):
                    exits[c] = (p["qty"], "TRAIL")
            else:
                q_sell = 0
                for k, (th, frac) in enumerate(TP_TIERS[:2]):
                    if k < p["tiers"]:
                        continue
                    if ret >= th:
                        p["tiers"] = k + 1
                        q_sell += max(int(p["qty"] * frac / 100) * 100, 100)
                if q_sell > 0:
                    exits[c] = (min(q_sell, p["qty"]), "TP_FIX")
                if c not in exits or exits[c][0] < p["qty"]:
                    act = p["peak"] / p["cost"] - 1.0 >= TRAIL_ACT
                    if act and px <= p["peak"] * (1.0 - TRAIL_DD):
                        exits[c] = (p["qty"], "TRAIL")
        for c, p in pos.items():
            if c in exits and exits[c][0] >= p["qty"]:
                continue
            held = d - p["entry_d"]
            if held >= min_hold and rank_row[c] > buffer_m:
                exits[c] = (p["qty"], "ROT")
                rot_count += 1
        for c, (q, reason) in exits.items():
            sell_plan.append((c, q, reason))

        remaining = sum(1 for c, p in pos.items()
                        if not (c in exits and exits[c][0] >= p["qty"]))
        free_slots = n_slots - remaining
        buy_plan_target = {}
        if free_slots > 0 and d + 1 < n_days:
            slot_target = nav / n_slots
            held_cols = set(pos.keys())
            cand = np.flatnonzero(rank_row <= top_n)
            cand = sorted((c for c in cand if c not in held_cols),
                          key=lambda c: rank_row[c])
            for c in cand[:free_slots]:
                buy_plan.append(int(c))
                buy_plan_target[int(c)] = slot_target

    nav_s = pd.Series(nav_hist, index=dates, name="alpha")
    return dict(nav=nav_s, holdings=pd.DataFrame(hold_recs),
                cash=pd.Series(cash_hist, index=dates, name="cash_post_open"),
                fee_total=fee_total, rot_count=rot_count,
                avg_exposure=float(expo_hist.mean()))


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data = s1_sim.load_data("score_eq")
    dates = data["dates"]
    sl = np.flatnonzero((dates >= WIN_START) & (dates <= WIN_END))
    d2 = dates[sl]
    print(f"[win] {d2[0].date()}~{d2[-1].date()} n={len(d2)}", flush=True)
    res = run_with_holdings(CFG["tp_arm"], data["score"][sl], data["opn"][sl],
                            data["cls"][sl], data["col_of"], d2,
                            CFG["n_slots"], CFG["buffer_m"], CFG["min_hold"])
    res["nav"].to_csv(OUT / "nav_alpha.csv", encoding="utf-8-sig")
    res["cash"].to_csv(OUT / "cash_daily.csv", encoding="utf-8-sig")
    res["holdings"].to_parquet(OUT / "holdings_daily.parquet", index=False)
    rec = s1_sim.metrics(res["nav"], fee_total=res["fee_total"],
                         rot_count=res["rot_count"],
                         avg_exposure=res["avg_exposure"])
    n_elig = int(res["holdings"]["eligible"].sum())
    rec.update(dict(cfg=CFG, window=f"{d2[0].date()}~{d2[-1].date()}",
                    n_slot_days=len(res["holdings"]), n_eligible_slot_days=n_elig,
                    n_distinct_codes=int(res["holdings"]["code"].nunique())))
    (OUT / "alpha_metrics.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[alpha] nav={rec['final_nav']:.3f} ann={rec['ann_ret']:+.2%} "
          f"mdd={rec['max_dd']:+.2%} sharpe={rec['sharpe']:.2f} "
          f"slot_days={rec['n_slot_days']} elig={n_elig} "
          f"codes={rec['n_distinct_codes']} ({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
