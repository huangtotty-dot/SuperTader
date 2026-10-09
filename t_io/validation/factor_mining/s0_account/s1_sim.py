# -*- coding: utf-8 -*-
"""
S1 账户仿真 · 单次运行 CLI（预注册修正案落地版）
================================================
相对 s0_sim.py 的修正案差异（预注册，见报告 §S1）：
  ① 宇宙卫生：加载 s1_hygiene_mask.pkl，卫生外宇宙分数置 NaN
     （<2元剔除 + 近60日 rolling max/min-1 >60% 剔除）；
  ② 回款口径：当日可用（proceeds_lag=0，owner 拍板，对齐 A 股资金 T+0 可用）；
  ③ 参数化：N(槽数=换入阈值) / M(调出缓冲) / min_hold / 止盈臂 全部 CLI 传入；
  ④ 主臂 score_eq（ICIR 臂保留 --score score_icir，结论标注存疑）。

其余规则与 S0 完全一致：初始资金 100 万、槽位均分整百股、T+1 开盘成交、
信号收盘评估次日开盘执行、每边费 0.0345%、兜底止损 -12%、止盈三臂 A/B/C 定义不变。

用法：
  python s1_sim.py --n 4 --m 6 --min-hold 3 --tp-arm C --out results/s1_runs/xxx.json
  python s1_sim.py --emit-grid results/grid_spec.json     # 生成 108 组合扫描清单

输出：--out 指定路径落盘**单行 JSON**（参数 + 指标 + 净值路径）；
净值曲线 csv 落 results/s1_nav/；--save-trades 时落 results/s1_trades/。
"""
from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "results"
NAV_DIR = OUT_DIR / "s1_nav"
TRD_DIR = OUT_DIR / "s1_trades"

INIT_CASH = 1_000_000.0
FEE = 0.000345            # 每边 0.0345%
STOP_LOSS = -0.12         # 兜底止损
TP_TIERS = [(0.05, 1 / 3), (0.08, 1 / 3), (0.10, 1.0)]   # 臂A
TRAIL_ACT = 0.08
TRAIL_DD = 0.03

GRID = dict(n=[3, 4, 5], m=[6, 8, 12], min_hold=[1, 2, 3, 5], tp_arm=["A", "B", "C"])


# ---------------------------------------------------------------------------
# 参数化单臂仿真（逻辑同 s0_sim.run_sim，常量全部参数化；回款当日可用）
# ---------------------------------------------------------------------------
def s1_run_sim(name: str, arm: str, score: np.ndarray, opn: np.ndarray,
               cls: np.ndarray, col_of: dict, dates: pd.DatetimeIndex,
               n_slots: int = 4, buffer_m: int = 6, min_hold: int = 3,
               proceeds_lag: int = 0) -> dict:
    top_n = n_slots                    # 换入阈值 rank<=N（S0 口径 N=TOP_N=4）
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
    pending = 0.0                      # 次日可用回款（lag=1 时；lag=0 恒为 0）
    pos: dict[int, dict] = {}
    sell_plan: list[tuple[int, int, str]] = []
    buy_plan: list[int] = []
    buy_plan_target: dict[int, float] = {}
    nav_hist = np.full(n_days, np.nan)
    expo_hist = np.zeros(n_days)
    slots_hist = np.zeros(n_days)
    trades: list[dict] = []
    flows: dict[str, float] = {}
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
                continue                       # 停牌/缺价：卖单顺延
            q = min(q, p["qty"])
            proceeds = q * px * (1.0 - FEE)
            if proceeds_lag == 0:
                cash += proceeds               # S1 口径：当日可用
            else:
                pending += proceeds
            fee_total += q * px * FEE
            code = col_of[c]
            flows[code] = flows.get(code, 0.0) + proceeds
            p["qty"] -= q
            trades.append(dict(date=dates[d], code=code, side="sell", qty=q,
                               px=px, reason=reason))
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
        exits: dict[int, tuple[int, str]] = {}
        for c, p in list(pos.items()):
            px = day_close[c]
            if not np.isfinite(px):
                continue
            ret = px / p["cost"] - 1.0
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
                continue
            held = d - p["entry_d"]
            if held >= min_hold and rank_row[c] > buffer_m:
                exits[c] = (p["qty"], "ROT")
                rot_count += 1
        for c, (q, reason) in exits.items():
            sell_plan.append((c, q, reason))

        # 4) 换入计划
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


def metrics(nav: pd.Series, **extra) -> dict:
    nav = nav.dropna()
    ret = nav.pct_change().dropna()
    n = len(nav)
    ann = (nav.iloc[-1] / nav.iloc[0]) ** (252.0 / max(n - 1, 1)) - 1.0
    mdd = float((nav / nav.cummax() - 1.0).min())
    sharpe = float(ret.mean() / ret.std() * np.sqrt(252.0)) if ret.std() > 0 else np.nan
    win = float((ret > 0).mean())
    out = dict(final_nav=float(nav.iloc[-1]), ann_ret=float(ann), max_dd=mdd,
               sharpe=sharpe, win_rate=win, n_days=n)
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# 数据加载（缓存复用，模块级 lazy）
# ---------------------------------------------------------------------------
_CACHE: dict = {}


def load_data(score_name: str) -> dict:
    key = score_name
    if key in _CACHE:
        return _CACHE[key]
    with open(OUT_DIR / "pivots.pkl", "rb") as f:
        piv = pickle.load(f)
    with open(OUT_DIR / "s1_hygiene_mask.pkl", "rb") as f:
        mask_df = pickle.load(f)
    scores_df = pd.read_parquet(OUT_DIR / "scores.parquet")
    sdates = pd.DatetimeIndex(sorted(scores_df["date"].unique()))
    cols = piv["close"].columns
    col_of = {i: c for i, c in enumerate(cols)}
    opn = piv["open"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    cls = piv["close"].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    sc = piv[score_name].reindex(index=sdates, columns=cols).to_numpy(np.float64)
    mask = mask_df.reindex(index=sdates, columns=cols).fillna(False).to_numpy(bool)
    sc_clean = np.where(mask, sc, np.nan)          # 修正案①：卫生后宇宙
    data = dict(opn=opn, cls=cls, score=sc_clean, col_of=col_of, dates=sdates)
    _CACHE[key] = data
    return data


def emit_grid(path: Path) -> None:
    combos = []
    for n in GRID["n"]:
        for m in GRID["m"]:
            for h in GRID["min_hold"]:
                for arm in GRID["tp_arm"]:
                    tag = f"s1_eq_N{n}_M{m}_H{h}_{arm}"
                    combos.append(dict(
                        run_id=tag, score="score_eq", n=n, m=m, min_hold=h,
                        tp_arm=arm,
                        cmd=(f"python s1_sim.py --n {n} --m {m} --min-hold {h} "
                             f"--tp-arm {arm} --out results/s1_runs/{tag}.json"),
                        out=f"results/s1_runs/{tag}.json"))
    spec = dict(
        version="S1 grid v1 (pre-registered 2026-10-09)",
        note="等权复合 score_eq 为主臂；回款当日可用；宇宙=卫生后；"
             "ICIR 臂存疑不进主网格；OOS 分段在扫描汇总阶段做",
        n_combos=len(combos), combos=combos)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[grid] {len(combos)} combos -> {path}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4, help="槽数=换入阈值 rank<=N")
    ap.add_argument("--m", type=int, default=6, help="调出缓冲 rank>M")
    ap.add_argument("--min-hold", type=int, default=3, help="最短持有交易日")
    ap.add_argument("--tp-arm", choices=["A", "B", "C"], default="C")
    ap.add_argument("--score", default="score_eq",
                    choices=["score_eq", "score_icir", "rev10_z"])
    ap.add_argument("--out", type=str, default=None, help="单行 JSON 输出路径")
    ap.add_argument("--tag", type=str, default=None)
    ap.add_argument("--save-trades", action="store_true")
    ap.add_argument("--emit-grid", type=str, default=None,
                    help="生成 grid_spec.json 后退出")
    args = ap.parse_args()

    if args.emit_grid:
        emit_grid(Path(args.emit_grid))
        return

    t0 = time.time()
    tag = args.tag or f"s1_{args.score.replace('score_', '')}_N{args.n}_M{args.m}_H{args.min_hold}_{args.tp_arm}"
    data = load_data(args.score)
    res = s1_run_sim(tag, args.tp_arm, data["score"], data["opn"], data["cls"],
                     data["col_of"], data["dates"],
                     n_slots=args.n, buffer_m=args.m, min_hold=args.min_hold,
                     proceeds_lag=0)

    NAV_DIR.mkdir(parents=True, exist_ok=True)
    nav_path = NAV_DIR / f"nav_{tag}.csv"
    res["nav"].to_csv(nav_path, encoding="utf-8-sig")
    if args.save_trades:
        TRD_DIR.mkdir(parents=True, exist_ok=True)
        res["trades"].to_csv(TRD_DIR / f"trades_{tag}.csv",
                             index=False, encoding="utf-8-sig")
        contrib = pd.Series(res["flows"], name="contrib").sort_values(ascending=False)
        contrib.index.name = "code"
        contrib.to_csv(TRD_DIR / f"contrib_{tag}.csv", encoding="utf-8-sig")

    rec = metrics(res["nav"], fee_total=res["fee_total"],
                  rot_count=res["rot_count"], n_trades=res["n_trades"],
                  avg_exposure=res["avg_exposure"], avg_slots=res["avg_slots"])
    rec.update(dict(tag=tag, score=args.score, n=args.n, m=args.m,
                    min_hold=args.min_hold, tp_arm=args.tp_arm,
                    hygiene="px>=2 & roll60dd<=60%", proceeds="same_day(lag=0)",
                    nav_path=str(nav_path)))
    out_path = Path(args.out) if args.out else (OUT_DIR / "s1_runs" / f"{tag}.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[s1_sim] {tag}: nav={rec['final_nav']:.3f} ann={rec['ann_ret']:+.2%} "
          f"mdd={rec['max_dd']:+.2%} sharpe={rec['sharpe']:.2f} "
          f"rot={rec['rot_count']} expo={rec['avg_exposure']:.0%} "
          f"fee={rec['fee_total']:,.0f} -> {out_path} "
          f"({time.time()-t0:.0f}s)", flush=True)


if __name__ == "__main__":
    main()
