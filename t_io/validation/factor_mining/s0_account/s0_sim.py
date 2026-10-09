# -*- coding: utf-8 -*-
"""
S0 步骤3/3 · 账户级 N=4 槽仿真（核心交付）
==========================================
规则（owner 拍板口径，doc/solutions/2026-10-09 设计文档 §4.2）：
  - 初始资金 100 万，4 槽均分（单槽目标 = 计划日收盘 NAV / 4，整百股）
  - 每日收盘按复合分截面排名（值大=看好，rank 1 最优）
  - 缓冲带：rank<=4 才换入；持仓 rank>6（或分数缺失）且持有>=3 交易日才调出
  - 最短持有 3 交易日（仅约束排名调出；止盈/止损为强制风险退出，不受限）
  - T+1 开盘成交（面板 open 价）；信号一律收盘评估、次日开盘执行
  - 换股先卖后买；**当日卖出回款次日可用**（保守口径，比实盘 T+0 可用更严）
  - 费用：每边 0.0345%（往返 0.069%，core/cost_model.py 口径）
  - 止盈三臂（收盘评估 vs 持仓成本，次日开盘执行）：
      A 固定三档 {+5%:卖1/3, +8%:再卖1/3, +10%:清仓}
      B 纯 TRAIL（峰值收盘浮盈>=+8% 激活，收盘自峰值回撤 3% 清仓）
      C 混合（+5%/+8% 两档各卖 1/3，剩余由 TRAIL 管，激活/回撤同 B）
  - 止损：收盘浮亏 <= -12% 清仓（兜底线；三区间架构未施工，报告注明）
  - 强制退出空出的槽位经由常规排名换入补足（无额外冷却，T+1 已天然隔日）

运行矩阵：score_eq / score_icir / rev10_z × A/B/C 共 9 臂
基线：等权全宇宙 buy&hold（F3 过滤宇宙日度等权 close-close，无费）

输出（results/）：
  nav_curves.csv / metrics_summary.csv / trades_{run}.csv / contrib_{run}.csv
"""
from __future__ import annotations

import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"

INIT_CASH = 1_000_000.0
N_SLOTS = 4
TOP_N = 4                 # 换入阈值 rank<=4
BUFFER_M = 6              # 调出阈值 rank>6
MIN_HOLD = 3              # 最短持有（交易日，仅约束排名调出）
FEE = 0.000345            # 每边 0.0345%
STOP_LOSS = -0.12         # 兜底止损
TP_TIERS = [(0.05, 1 / 3), (0.08, 1 / 3), (0.10, 1.0)]   # 臂A
TRAIL_ACT = 0.08
TRAIL_DD = 0.03

SCORES = ["score_eq", "score_icir", "rev10_z"]
ARMS = ["A", "B", "C"]


# ---------------------------------------------------------------------------
# 单臂仿真
# ---------------------------------------------------------------------------
def run_sim(name: str, arm: str, score: np.ndarray, opn: np.ndarray,
            cls: np.ndarray, col_of: dict, dates: pd.DatetimeIndex,
            proceeds_lag: int = 1) -> dict:
    n_days, n_sym = score.shape
    # 预计算排名（值大者 rank 小）；NaN -> rank=inf
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
    pending = 0.0                      # 当日卖出回款，次日可用
    pos: dict[int, dict] = {}          # col -> 持仓
    sell_plan: list[tuple[int, int, str]] = []   # (col, qty, reason)
    buy_plan: list[int] = []           # col 按 rank 升序
    nav_hist = np.full(n_days, np.nan)
    expo_hist = np.zeros(n_days)       # 收盘持仓市值 / NAV
    slots_hist = np.zeros(n_days)      # 收盘持仓槽数
    trades: list[dict] = []
    flows: dict[str, float] = {}       # 单票现金流（贡献分解）
    fee_total = 0.0
    rot_count = 0

    def holdings_value(d: int) -> float:
        v = 0.0
        for c, p in pos.items():
            px = cls[d, c]
            if np.isfinite(px):
                v += p["qty"] * px
        return v

    for d in range(n_days):
        # ── 开盘：回款到账 → 先卖 → 后买 ──
        cash += pending
        pending = 0.0
        day_open = opn[d]
        for c, q, reason in sell_plan:
            p = pos.get(c)
            px = day_open[c]
            if p is None or not np.isfinite(px) or px <= 0:
                continue                       # 停牌/缺价：卖单顺延（次日重新计划）
            q = min(q, p["qty"])
            proceeds = q * px * (1.0 - FEE)
            if proceeds_lag == 0:
                cash += proceeds           # 当日可用（设计文档 §3.3 口径，敏感臂）
            else:
                pending += proceeds        # 次日可用（任务书口径，主臂）
            fee_total += q * px * FEE
            code = col_of[c]
            flows[code] = flows.get(code, 0.0) + proceeds
            p["qty"] -= q
            trades.append(dict(date=dates[d], code=code, side="sell", qty=q,
                               px=px, reason=reason))
            if p["qty"] <= 0:
                del pos[c]
        sell_plan = []
        # 买入（现金受「次日可用」约束：今日卖出回款不可用）
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
            code = col_of[c]
            flows[code] = flows.get(code, 0.0) - cost_amt
            pos[c] = dict(qty=q, cost=px, entry_d=d, tiers=0, peak=-np.inf)
            trades.append(dict(date=dates[d], code=code, side="buy", qty=q,
                               px=px, reason="ROT_IN"))
        buy_plan = []

        # ── 收盘：标记净值、更新峰值、评估止盈止损、生成次日计划 ──
        day_close = cls[d]
        for c, p in pos.items():
            px = day_close[c]
            if np.isfinite(px):
                p["peak"] = max(p["peak"], px)
        nav = cash + pending + holdings_value(d)
        nav_hist[d] = nav
        hv = holdings_value(d)
        expo_hist[d] = hv / nav if nav > 0 else 0.0
        slots_hist[d] = len(pos)

        rank_row = rank[d]
        exits: dict[int, tuple[int, str]] = {}   # col -> (qty, reason)
        for c, p in list(pos.items()):
            px = day_close[c]
            if not np.isfinite(px):
                continue
            ret = px / p["cost"] - 1.0
            held = d - p["entry_d"]
            # 1) 兜底止损（强制，不受最短持有约束）
            if ret <= STOP_LOSS:
                exits[c] = (p["qty"], "SL12")
                continue
            # 2) 止盈臂
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
            else:  # C 混合：前两档固定，剩余 TRAIL
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
        # 3) 排名调出（缓冲带 + 最短持有）
        for c, p in pos.items():
            if c in exits and exits[c][0] >= p["qty"]:
                continue                            # 已计划全退
            held = d - p["entry_d"]
            if held >= MIN_HOLD and rank_row[c] > BUFFER_M:
                exits[c] = (p["qty"], "ROT")
                rot_count += 1
        for c, (q, reason) in exits.items():
            sell_plan.append((c, q, reason))

        # 4) 换入计划：空槽数 = 4 - 计划后仍持仓数；rank<=4 候选按排名
        remaining = sum(1 for c, p in pos.items()
                        if not (c in exits and exits[c][0] >= p["qty"]))
        free_slots = N_SLOTS - remaining
        buy_plan_target = {}
        if free_slots > 0 and d + 1 < n_days:
            slot_target = nav / N_SLOTS
            held_cols = set(pos.keys())
            cand = np.flatnonzero(rank_row <= TOP_N)
            cand = sorted((c for c in cand if c not in held_cols),
                          key=lambda c: rank_row[c])
            for c in cand[:free_slots]:
                buy_plan.append(int(c))
                buy_plan_target[int(c)] = slot_target

    # ── 期末清算浮盈进贡献 ──
    for c, p in pos.items():
        px = cls[n_days - 1, c]
        if np.isfinite(px):
            flows[col_of[c]] = flows.get(col_of[c], 0.0) + p["qty"] * px

    nav_s = pd.Series(nav_hist, index=dates, name=name)
    return dict(nav=nav_s, trades=pd.DataFrame(trades), flows=flows,
                fee_total=fee_total, rot_count=rot_count,
                n_trades=len(trades),
                avg_exposure=float(expo_hist.mean()),
                avg_slots=float(slots_hist.mean()))


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def metrics(nav: pd.Series, fee_total: float = np.nan,
            rot_count: int = 0, n_trades: int = 0,
            avg_exposure: float = np.nan, avg_slots: float = np.nan) -> dict:
    nav = nav.dropna()
    ret = nav.pct_change().dropna()
    n = len(nav)
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (252.0 / max(n - 1, 1)) - 1.0
    mdd = float((nav / nav.cummax() - 1.0).min())
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252.0)) if ret.std() > 0 else np.nan
    win = float((ret > 0).mean())
    return dict(final_nav=float(nav.iloc[-1]), ann_ret=ann, max_dd=mdd,
                sharpe=sharpe, win_rate=win, n_days=n,
                fee_total=fee_total, rot_count=rot_count, n_trades=n_trades,
                avg_exposure=avg_exposure, avg_slots=avg_slots)


def main() -> None:
    t0 = time.time()
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))

    close_p = piv["close"]
    cols = close_p.columns
    col_of = {i: c for i, c in enumerate(cols)}
    opn = piv["open"].reindex(index=sdates).to_numpy(np.float64)
    cls = close_p.reindex(index=sdates).to_numpy(np.float64)

    navs = {}
    rows = []
    # 主臂 lag=1（任务书「回款次日可用」）+ 敏感臂 lag=0（设计文档 §3.3「当日可用」）
    for lag, tag in ((1, ""), (0, "_L0")):
        for sname in SCORES:
            sc = piv[sname].reindex(index=sdates, columns=cols).to_numpy(np.float64)
            for arm in ARMS:
                run = f"{sname}_{arm}{tag}"
                res = run_sim(run, arm, sc, opn, cls, col_of, sdates,
                              proceeds_lag=lag)
                navs[run] = res["nav"]
                m = metrics(res["nav"], res["fee_total"], res["rot_count"],
                            res["n_trades"], res["avg_exposure"],
                            res["avg_slots"])
                m["run"] = run
                rows.append(m)
                res["trades"].to_csv(OUT_DIR / f"trades_{run}.csv",
                                     index=False, encoding="utf-8-sig")
                contrib = (pd.Series(res["flows"], name="contrib")
                           .sort_values(ascending=False))
                contrib.index.name = "code"
                contrib.to_csv(OUT_DIR / f"contrib_{run}.csv", encoding="utf-8-sig")
                print(f"[sim] {run}: nav={m['final_nav']:.3f} ann={m['ann_ret']:+.2%} "
                      f"mdd={m['max_dd']:+.2%} sharpe={m['sharpe']:.2f} "
                      f"rot={m['rot_count']} expo={m['avg_exposure']:.0%} "
                      f"fee={res['fee_total']:,.0f} "
                      f"({time.time()-t0:.0f}s)", flush=True)

    # ── 基线：等权全宇宙 buy&hold（F3 过滤宇宙，无费）──
    sc_ok = np.isfinite(piv["score_eq"].reindex(index=sdates, columns=cols)
                        .to_numpy(np.float64))
    prev_cls = np.roll(cls, 1, axis=0)
    prev_cls[0] = np.nan
    dret = cls / prev_cls - 1.0
    ok = sc_ok & np.isfinite(dret)
    ew = np.where(ok, dret, np.nan)
    with np.errstate(invalid="ignore"):
        ew_ret = np.nanmean(ew, axis=1)
    ew_ret[0] = 0.0
    ew_nav = pd.Series(np.cumprod(1.0 + np.nan_to_num(ew_ret)),
                       index=sdates, name="ew_universe")
    navs["ew_universe"] = ew_nav
    m = metrics(ew_nav)
    m["run"] = "ew_universe"
    rows.append(m)
    print(f"[base] ew_universe: nav={m['final_nav']:.3f} ann={m['ann_ret']:+.2%} "
          f"mdd={m['max_dd']:+.2%}", flush=True)

    nav_df = pd.DataFrame(navs)
    nav_df.index.name = "date"
    nav_df.to_csv(OUT_DIR / "nav_curves.csv", encoding="utf-8-sig")
    ms = pd.DataFrame(rows).set_index("run")
    ms.to_csv(OUT_DIR / "metrics_summary.csv", encoding="utf-8-sig")
    (OUT_DIR / "sim_config.json").write_text(json.dumps(dict(
        init_cash=INIT_CASH, n_slots=N_SLOTS, top_n=TOP_N, buffer_m=BUFFER_M,
        min_hold=MIN_HOLD, fee_side=FEE, stop_loss=STOP_LOSS,
        tp_tiers=TP_TIERS, trail_act=TRAIL_ACT, trail_dd=TRAIL_DD,
        cash_rule="main: proceeds next day (lag=1); sensitivity: same day (lag=0)",
        signal_rule="close signal -> next open execution",
    ), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[save] nav_curves/metrics_summary 总耗时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
