# -*- coding: utf-8 -*-
"""S3-β 独立做T账户臂 · step2: 隔夜滚动底仓 + OGR 日内腿 仿真引擎。

口径见 doc/experiment/2026-10-10_S3_β独立账户臂_预注册与基线.md §2（预注册冻结）。

结构（T+1 合规）：
  T 收盘  按候选名单（mkt_gap<0 日 rel 最深 Top-N，剔除 ETF，a930≥MIN_A930）买入底仓；
  T+1 早盘 对持仓票重算核原版 OGR 触发，触发则现金加仓 q 股（09:30 open）、
           10:00 卖出等量**旧底仓**（c1000），合法 T+0；
  T+1 收盘 底仓退出：close_flat（基线，全平）/ keep_if_candidate（仍候选则续持）。

分账：β 权益 = 现金 + Σ 股数×c1500（逐日 MTM）；腿账 = 腿现金流；底仓账 = β − 腿账。
成本：core/cost_model.py stock venue（fb=0.00954% / fs=0.05954%）；
      --min-commission 5 开启 5 元最低佣金敏感性（非基线）。
容量：腿资金 ≤ size_cap_by_auction(a930, participation)，超限降级，<MIN_LEG 放弃。

运行（单窗秒级，无断点需求；--start/--end 可切片）：
  python s3_beta_sim.py --tag base
  python s3_beta_sim.py --tag keep --exit-rule keep_if_candidate
  python s3_beta_sim.py --tag mincomm --min-commission 5
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import open_gap_reversal as OGR          # noqa: E402
from core import cost_model as CM                  # noqa: E402

FEE_S, FEE_B = CM.fees('stock')                    # 0.0005954 / 0.0000954
INIT_TOTAL = 1_000_000.0                           # 组合本金（ann 的 1M 口径）
CUT = "2025-06-11"                                 # H1/H2 切半（延续 S2）
POST = "2026-02-24"                                # 衰减归因修正拐点


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--tag", default="base")
    p.add_argument("--n", type=int, default=4)
    p.add_argument("--pocket", type=float, default=250_000.0)
    p.add_argument("--leg-frac", type=float, default=0.25)
    p.add_argument("--min-a930", type=float, default=1_000_000.0)
    p.add_argument("--participation", type=float, default=0.10)
    p.add_argument("--min-leg", type=float, default=10_000.0)
    p.add_argument("--exit-rule", choices=["close_flat", "keep_if_candidate"],
                   default="close_flat")
    p.add_argument("--min-commission", type=float, default=None,
                   help="5 元最低佣金敏感性；默认 None = 纯 cost_model 费率")
    p.add_argument("--start", default="2025-03-31")
    p.add_argument("--end", default="2026-09-17")
    return p.parse_args()


def fee_buy(amount: float, min_comm: float | None) -> float:
    f = amount * CM.TRANSFER_FEE
    c = amount * CM.COMMISSION
    if min_comm is not None:
        c = max(c, min_comm)
    return f + c


def fee_sell(amount: float, min_comm: float | None) -> float:
    f = amount * (CM.TRANSFER_FEE + CM.STAMP_TAX_SELL)
    c = amount * CM.COMMISSION
    if min_comm is not None:
        c = max(c, min_comm)
    return f + c


def leg_t(legs: pd.DataFrame) -> float:
    if len(legs) < 2:
        return float("nan")
    s = legs["net_bp"]
    return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))) if s.std(ddof=1) > 0 else float("nan")


def window_stats(legs: pd.DataFrame, daily: pd.DataFrame, pocket: float) -> dict:
    """legs/daily 已按窗口切好。daily 需含 leg_pnl, base_pnl, beta_pnl。"""
    n_days = int(len(daily))
    leg_pnl = float(daily["leg_pnl"].sum())
    base_pnl = float(daily["base_pnl"].sum())
    beta_pnl = float(daily["beta_pnl"].sum())
    cum = daily["beta_pnl"].cumsum()
    mdd = float((cum - cum.cummax()).min()) / pocket if n_days else np.nan
    dr = daily["beta_pnl"] / pocket
    sharpe = float(dr.mean() / dr.std() * np.sqrt(252.0)) if n_days > 1 and dr.std() > 0 else np.nan
    out = dict(
        n_days=n_days,
        legs=int(len(legs)),
        leg_days=int(daily.loc[daily["leg_pnl"] != 0, "date"].nunique()) if n_days else 0,
        win=float((legs["net_bp"] > 0).mean()) if len(legs) else np.nan,
        bp_mean=float(legs["net_bp"].mean()) if len(legs) else np.nan,
        bp_wmean=float((legs["net_bp"] * legs["notional"]).sum() / legs["notional"].sum())
        if len(legs) else np.nan,
        t_leg=leg_t(legs),
        leg_pnl=leg_pnl, base_pnl=base_pnl, beta_pnl=beta_pnl,
        beta_ann_pocket=beta_pnl / pocket * 252.0 / max(n_days, 1),
        beta_ann_1M=beta_pnl / INIT_TOTAL * 252.0 / max(n_days, 1),
        leg_ann_pocket=leg_pnl / pocket * 252.0 / max(n_days, 1),
        base_ann_pocket=base_pnl / pocket * 252.0 / max(n_days, 1),
        sharpe=sharpe, mdd=mdd,
    )
    return out


def main() -> None:
    t0 = time.time()
    a = parse_args()
    out = HERE / "results" / "s3_beta" / a.tag
    out.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(HERE / "results" / "s3_beta" / "min_index_s3.parquet")
    df = df[(df["date"] >= a.start) & (df["date"] <= a.end)]
    days = sorted(df["date"].unique())
    last_day = days[-1]

    by_day = {d: g for d, g in df.groupby("date")}
    mi_key = {(c, d): (o, c10, c15, a9) for c, d, o, c10, c15, a9 in
              zip(df["code"], df["date"], df["o930"], df["c1000"],
                  df["c1500"], df["a930"])}
    # 每日 mkt_gap（981 全池含 ETF，与 S2 面板口径一致；核内 MIN_POOL=5）
    mg_by_day = {}
    for d, g in by_day.items():
        gaps = dict(zip(g["code"], g["gap"]))
        mg_by_day[d] = OGR.market_gap({c: v for c, v in gaps.items()
                                       if np.isfinite(v)})

    base_alloc = a.pocket / a.n / (1.0 + a.leg_frac)
    is_etf = lambda c: c.startswith("5")                      # noqa: E731

    cash = a.pocket
    book: dict[str, dict] = {}     # code -> {shares, cash_out, entry_date, entry_px, rel0, last_px}
    legs_recs, pos_recs, daily_recs = [], [], []
    cov = dict(candidate_days=0, candidates=0, base_buys=0, base_sells=0,
               legs=0, downgraded=0, dropped=0, suspended_skip=0,
               no_trigger=0, mkt_not_neg_days=0)

    for d in days:
        g = by_day[d]
        gaps = dict(zip(g["code"], g["gap"]))
        mg = mg_by_day[d]
        leg_pnl_d = 0.0
        n_legs_d = 0

        # ── 早盘：对持有底仓的票判定 OGR 触发并开腿 ──
        for code, pos in list(book.items()):
            px = mi_key.get((code, d))
            if px is None:
                cov["suspended_skip"] += 1
                continue
            o930, c1000, _c15, a9 = px
            pos["last_px"] = _c15
            ok, _why = OGR.decide_one(gaps.get(code, np.nan), mg)
            if not ok:
                cov["no_trigger"] += 1
                continue
            if not (np.isfinite(o930) and np.isfinite(c1000) and o930 > 0):
                continue
            notional = a.leg_frac * pos["shares"] * o930
            cap = OGR.size_cap_by_auction(a9, a.participation)
            downgraded = False
            if notional > cap:
                notional = cap
                downgraded = True
            if notional < a.min_leg:
                cov["dropped"] += 1
                continue
            q = notional / o930
            fb = fee_buy(notional, a.min_commission)
            sell_amt = q * c1000
            fs = fee_sell(sell_amt, a.min_commission)
            pnl = (sell_amt - fs) - (notional + fb)
            net = (c1000 * (1 - fs / sell_amt)) / (o930 * (1 + fb / notional)) - 1.0
            cash += pnl
            leg_pnl_d += pnl
            n_legs_d += 1
            cov["legs"] += 1
            cov["downgraded"] += int(downgraded)
            legs_recs.append(dict(
                date=d, code=code, shares=q, buy_px=o930, sell_px=c1000,
                notional=notional, gross_bp=(c1000 / o930 - 1.0) * 1e4,
                net_bp=net * 1e4, pnl=pnl, gap=gaps.get(code, np.nan),
                rel=gaps.get(code, np.nan) - mg, mkt_gap=mg, a930=a9,
                cap=cap, downgraded=downgraded,
                fee_buy=fb, fee_sell=fs, entry_date=pos["entry_date"]))

        # ── 收盘：形成候选名单（用当日已收盘数据；最后一日不再建仓）──
        cands: list[tuple[str, float]] = []
        if mg is not None and mg < 0 and d != last_day:
            gg = g[(~g["code"].map(is_etf)) & g["gap"].notna()
                   & (g["a930"] >= a.min_a930)].copy()
            gg["rel"] = gg["gap"] - mg
            gg = gg.sort_values("rel").head(a.n)
            cands = list(zip(gg["code"], gg["rel"]))
            cov["candidate_days"] += 1
            cov["candidates"] += len(cands)
        elif mg is not None and not (mg < 0):
            cov["mkt_not_neg_days"] += 1
        cand_set = {c for c, _ in cands}
        rel_map = dict(cands)

        # ── 收盘：底仓退出 ──
        for code, pos in list(book.items()):
            px = mi_key.get((code, d))
            if px is None:
                continue                                # 停牌：继续持有
            c15 = px[2]
            keep = (a.exit_rule == "keep_if_candidate" and code in cand_set
                    and d != last_day)
            if keep:
                continue
            amt = pos["shares"] * c15
            fs = fee_sell(amt, a.min_commission)
            proceeds = amt - fs
            cash += proceeds
            cov["base_sells"] += 1
            pos_recs.append(dict(
                code=code, entry_date=pos["entry_date"], exit_date=d,
                shares=pos["shares"], buy_px=pos["entry_px"], sell_px=c15,
                cash_out=pos["cash_out"], proceeds=proceeds,
                pnl=proceeds - pos["cash_out"], rel_at_entry=pos["rel0"],
                exit_rule=a.exit_rule))
            del book[code]

        # ── 收盘：买入新候选底仓（skip 已持有的 keep 票）──
        for code, rel0 in cands:
            if code in book:
                continue
            px = mi_key.get((code, d))
            if px is None or not np.isfinite(px[2]) or px[2] <= 0:
                continue
            c15 = px[2]
            shares = base_alloc / (c15 * (1.0 + FEE_B))   # 现金口径 ≈ BASE_ALLOC
            cost = shares * c15
            fb = fee_buy(cost, a.min_commission)
            cash_out = cost + fb
            if cash_out > cash:                           # 现金保护（理论上不超）
                shares *= cash / cash_out
                cost = shares * c15
                fb = fee_buy(cost, a.min_commission)
                cash_out = cost + fb
            cash -= cash_out
            cov["base_buys"] += 1
            book[code] = dict(shares=shares, cash_out=cash_out, entry_date=d,
                              entry_px=c15, rel0=rel0, last_px=c15)

        equity = cash + sum(p["shares"] * p["last_px"] for p in book.values())
        daily_recs.append(dict(date=d, leg_pnl=leg_pnl_d, n_legs=n_legs_d,
                               n_base_held=len(book), cash=cash, equity=equity,
                               mkt_gap=mg, n_cand=len(cands)))

    daily = pd.DataFrame(daily_recs)
    daily["beta_pnl"] = daily["equity"].diff().fillna(daily["equity"] - a.pocket)
    daily["base_pnl"] = daily["beta_pnl"] - daily["leg_pnl"]
    legs = pd.DataFrame(legs_recs)
    pos = pd.DataFrame(pos_recs)

    legs.to_csv(out / "legs_beta.csv", index=False, encoding="utf-8-sig")
    pos.to_csv(out / "base_positions.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(out / "beta_daily.csv", index=False, encoding="utf-8-sig")

    # ── 归因：FULL / H1 / H2 / POST ──
    spans = dict(FULL=(days[0], last_day),
                 H1=(days[0], "2025-06-10"), H2=(CUT, last_day),
                 POST=(POST, last_day))
    rows = []
    for span, (d0, d1) in spans.items():
        lg = legs[(legs["date"] >= d0) & (legs["date"] <= d1)] if len(legs) else legs
        dl = daily[(daily["date"] >= d0) & (daily["date"] <= d1)]
        rows.append(dict(span=span, start=d0, end=d1,
                         **window_stats(lg, dl, a.pocket)))
    attr = pd.DataFrame(rows)
    attr.to_csv(out / "attribution.csv", index=False, encoding="utf-8-sig")

    full = rows[0]
    post = rows[3]
    gates = dict(
        G1_beta_ann_ge_3pct=bool(full["beta_ann_pocket"] >= 0.03),
        G2_leg_bp_ge_15=bool(full["bp_wmean"] >= 15.0),
        G3_post_nonneg=bool(post["beta_pnl"] >= 0.0),
        G4_capacity="PENDING (第三路)",
    )
    metrics = dict(
        tag=a.tag, params=dict(N=a.n, pocket=a.pocket, leg_frac=a.leg_frac,
                               min_a930=a.min_a930, participation=a.participation,
                               min_leg=a.min_leg, exit_rule=a.exit_rule,
                               min_commission=a.min_commission,
                               fee_buy=FEE_B, fee_sell=FEE_S,
                               start=a.start, end=a.end, cut=CUT, post=POST),
        coverage=cov, attribution=rows, gates=gates,
        elapsed=round(time.time() - t0, 1))
    (out / "metrics.json").write_text(json.dumps(
        metrics, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    pd.set_option("display.width", 250)
    print(f"[{a.tag}] cov={cov}", flush=True)
    print(attr.to_string(index=False, float_format=lambda x: f"{x:,.4f}"), flush=True)
    print(f"[gates] {gates}", flush=True)
    print(f"({metrics['elapsed']}s)", flush=True)


if __name__ == "__main__":
    main()
