# -*- coding: utf-8 -*-
"""S3-A 隔夜对冲可行性：统计分解 + 模拟对冲（只做可行性核算，不建生产 harness）。

口径见 doc/experiment/2026-10-10_S3a_隔夜对冲可行性.md §2~§5（预注册冻结）。

输入（只读）：
  results/s3_beta/base/base_positions.csv   732 底仓（c1500_T 买 → c1500_exit 平）
  results/s3_beta/base/beta_daily.csv       腿账日现金流
  results/s3_beta/min_index_s3.parquet      o930（隔夜/日内切分用）
  t_io/cache/daily_kline/index_*.json       指数日线 OHLC 缓存

输出 results/s3_hedge/：
  index_selection.csv  positions_hedged.csv  nightly_decomp.csv
  combined_daily.csv   hedge_metrics.json

运行：python s3a_hedge.py
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
BASE = HERE / "results" / "s3_beta" / "base"
IDX_DIR = ROOT / "t_io" / "cache" / "daily_kline"
OUT = HERE / "results" / "s3_hedge"

POCKET = 250_000.0
POST = "2026-02-24"
COST_BPS = [2.0, 5.0, 10.0]          # 乐观/中性/悲观，按对冲名义本金/夜
ETF_BORROW_ANN = 0.08                # ETF 融券对照 8%/年
WARMUP = 20                          # 扩展窗 β 最少历史夜数

# 候选指数代理：文件名 -> (标签, 期指映射)
INDICES = {
    "index_sh000001.json": ("SSE", "无（仅ETF）"),
    "index_sh000300.json": ("CSI300", "IF"),
    "index_sh000905.json": ("CSI500", "IC"),
    "index_sh000852.json": ("CSI1000", "IM"),
    "index_sz399006.json": ("ChiNext", "无（仅ETF）"),
}


def load_index(fname: str) -> pd.DataFrame:
    rows = json.loads((IDX_DIR / fname).read_text(encoding="utf-8"))["rows"]
    df = pd.DataFrame(rows)
    return df.set_index("date")[["open", "close"]].astype(float).sort_index()


def ols_beta(y: np.ndarray, x: np.ndarray) -> tuple[float, float, float]:
    """返回 (beta, r2, corr)；含截距 OLS。"""
    if len(y) < 3 or np.std(x) == 0:
        return np.nan, np.nan, np.nan
    xm, ym = x.mean(), y.mean()
    cov = float(((x - xm) * (y - ym)).sum())
    var = float(((x - xm) ** 2).sum())
    beta = cov / var
    corr = float(np.corrcoef(x, y)[0, 1])
    return beta, corr ** 2, corr


def tstat(s: pd.Series) -> float:
    s = s.dropna()
    if len(s) < 2 or s.std(ddof=1) == 0:
        return float("nan")
    return float(s.mean() / (s.std(ddof=1) / np.sqrt(len(s))))


def main() -> None:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    pos = pd.read_csv(BASE / "base_positions.csv", encoding="utf-8-sig",
                      dtype={"code": str})
    pos["code"] = pos["code"].str.zfill(6)
    daily = pd.read_csv(BASE / "beta_daily.csv", encoding="utf-8-sig")
    pos["r_gross"] = pos["sell_px"] / pos["buy_px"] - 1.0
    pos["r_net"] = pos["pnl"] / pos["cash_out"]
    pos["n_cal_days"] = (pd.to_datetime(pos["exit_date"])
                         - pd.to_datetime(pos["entry_date"])).dt.days

    idx = {lab: load_index(f) for f, (lab, _fut) in INDICES.items()}
    idx_cal = sorted(idx["CSI1000"].index)          # 指数交易日历（各指数一致）
    cal_rank = {d: i for i, d in enumerate(idx_cal)}
    # 交易夜数：entry→exit 跨越的交易日间隔（1 = 标准 T 收盘买 T+1 收盘平）
    pos["n_nights"] = [cal_rank.get(e, -1) - cal_rank.get(s, -1)
                       if s in cal_rank and e in cal_rank else np.nan
                       for s, e in zip(pos["entry_date"], pos["exit_date"])]

    # ── 单仓对齐指数区间收益（close_entry → close_exit；停牌顺延按实际区间）──
    for lab, d in idx.items():
        c = d["close"]
        pos[f"r_{lab}"] = [float(c.get(e, np.nan) / c.get(s, np.nan) - 1.0)
                           if s in c.index and e in c.index else np.nan
                           for s, e in zip(pos["entry_date"], pos["exit_date"])]
    n_bad = int(pos["r_CSI1000"].isna().sum())

    # ── 组合夜收益（entry_date 分组等权 ≡ 市值加权，base_alloc 各 5 万）──
    grp = pos.groupby("entry_date")
    night = grp.agg(n_pos=("code", "count"), r_p=("r_gross", "mean"),
                    r_p_net=("r_net", "mean"), notional=("cash_out", "sum"))
    for lab in INDICES.values():
        night[f"r_{lab[0]}"] = grp[f"r_{lab[0]}"].mean()

    # ── ① 指数选择：组合层回归 R² ──
    sel_rows = []
    for fname, (lab, fut) in INDICES.items():
        b, r2, corr = ols_beta(night["r_p"].values, night[f"r_{lab}"].values)
        bp, r2p, _ = ols_beta(pos["r_gross"].dropna().values,
                              pos.loc[pos["r_gross"].notna(), f"r_{lab}"].values)
        sel_rows.append(dict(index=lab, fut=fut, beta_port=b, r2_port=r2,
                             corr_port=corr, beta_pos=bp, r2_pos=r2p))
    sel = pd.DataFrame(sel_rows).sort_values("r2_port", ascending=False)
    sel.to_csv(OUT / "index_selection.csv", index=False, encoding="utf-8-sig")
    best = sel.iloc[0]["index"]
    rb = f"r_{best}"

    # ── ② 隔夜/日内切分（1 夜持仓子集，用 o930）──
    mi = pd.read_parquet(HERE / "results" / "s3_beta" / "min_index_s3.parquet")
    o930 = {(c, d): v for c, d, v in zip(mi["code"], mi["date"], mi["o930"])}
    one = pos[pos["n_nights"] == 1].copy()
    one["o930_exit"] = [o930.get((c, d), np.nan)
                        for c, d in zip(one["code"], one["exit_date"])]
    one = one[np.isfinite(one["o930_exit"]) & (one["o930_exit"] > 0)]
    one["r_on"] = one["o930_exit"] / one["buy_px"] - 1.0     # close_T → 09:30_{T+1}
    one["r_id"] = one["sell_px"] / one["o930_exit"] - 1.0    # 09:30 → close_{T+1}
    split = dict(n_1night=int(len(one)),
                 mean_total_bp=float(one["r_gross"].mean() * 1e4),
                 mean_overnight_bp=float(one["r_on"].mean() * 1e4),
                 mean_intraday_bp=float(one["r_id"].mean() * 1e4),
                 t_overnight=tstat(one["r_on"]), t_intraday=tstat(one["r_id"]))

    # 指数隔夜收益（1 夜子集，close_entry → open_exit）
    o_best = idx[best]["open"]
    one["r_idx_on"] = [float(o_best.get(e, np.nan)) / float(idx[best]["close"].get(s, np.nan)) - 1.0
                       if s in idx[best]["close"].index and e in o_best.index else np.nan
                       for s, e in zip(one["entry_date"], one["exit_date"])]
    on_grp = one.groupby("entry_date").agg(r_on=("r_on", "mean"),
                                           r_id=("r_id", "mean"),
                                           r_idx_on=("r_idx_on", "mean"))
    b_on, r2_on, _ = ols_beta(on_grp["r_on"].values, on_grp["r_idx_on"].values)

    # ── ③ 对冲模拟（主标的 best，组合层）──
    night = night.sort_index()
    b_is, r2_is, corr_is = ols_beta(night["r_p"].values, night[rb].values)
    # 扩展窗 β：仅用 t 之前的夜
    y, x = night["r_p"].values, night[rb].values
    b_ew = np.full(len(night), np.nan)
    for i in range(WARMUP, len(night)):
        b_ew[i], _, _ = ols_beta(y[:i], x[:i])
    night["beta_ew"] = b_ew
    night["resid_h0"] = night["r_p"]
    night["resid_cc_is"] = night["r_p"] - b_is * night[rb]
    night["resid_cc_ew"] = night["r_p"] - night["beta_ew"] * night[rb]

    def resid_stats(s: pd.Series, label: str) -> dict:
        s = s.dropna()
        worst20 = float(s.rolling(20).sum().min() * 1e4) if len(s) >= 20 else np.nan
        return dict(variant=label, n_nights=int(len(s)),
                    mean_bp=float(s.mean() * 1e4), std_bp=float(s.std(ddof=1) * 1e4),
                    t=tstat(s), worst20_bp=worst20,
                    share_pos=float((s > 0).mean()))
    vstats = [resid_stats(night["resid_h0"], "H0_不对冲"),
              resid_stats(night["resid_cc_is"], "H-CC-IS_全样本β"),
              resid_stats(night["resid_cc_ew"], "H-CC-EW_扩展窗β")]

    # H-CO-IS：仅隔夜段对冲（任务指定口径），残余 = 对冲后隔夜 + T+1 日内
    on_grp["resid_co"] = (on_grp["r_on"] - b_on * on_grp["r_idx_on"]) + on_grp["r_id"]
    vstats.append(resid_stats(on_grp["resid_co"], "H-CO-IS_仅隔夜对冲"))

    # ── 单仓对冲损益（H-CC-EW 口径落回单仓，供合并账）──
    ew_map = night["beta_ew"]
    pos["beta_ew"] = pos["entry_date"].map(ew_map)
    pos["hedge_ret"] = (pos["beta_ew"] * pos[f"r_{best}"]).fillna(0.0)
    pos["r_hedged"] = pos["r_net"] - pos["hedge_ret"]   # 净口径 − 指数对冲（warmup 夜不对冲）
    pos.to_csv(OUT / "positions_hedged.csv", index=False, encoding="utf-8-sig")
    night.to_csv(OUT / "nightly_decomp.csv", encoding="utf-8-sig")

    # ── ④ 合并账：腿 + 对冲后底仓，按日归集 ──
    dd = daily[["date", "leg_pnl"]].copy()
    hedged_mask = pos["beta_ew"].notna()                 # warmup 夜不对冲也不收对冲成本
    for cb in COST_BPS:
        pos[f"r_hc{cb:g}"] = pos["r_hedged"] - np.where(hedged_mask, cb / 1e4, 0.0)
    # ETF 融券对照：8%/年 ≈ 3.17bp/夜（融券利息按日历天计）
    etf_bp = ETF_BORROW_ANN / 252 * 1e4
    pos["r_hc_etf"] = pos["r_hedged"] - np.where(
        hedged_mask, etf_bp / 1e4 * pos["n_cal_days"], 0.0)
    for col in [f"r_hc{cb:g}" for cb in COST_BPS] + ["r_hc_etf", "r_net"]:
        pnl_col = pos[col] * pos["cash_out"]
        dd[col.replace("r_", "pnl_")] = dd["date"].map(
            pnl_col.groupby(pos["exit_date"]).sum()).fillna(0.0)
    dd.to_csv(OUT / "combined_daily.csv", index=False, encoding="utf-8-sig")

    def span_stats(d: pd.DataFrame, col: str, d0: str, d1: str) -> dict:
        w = d[(d["date"] >= d0) & (d["date"] <= d1)]
        n = len(w)
        tot = float(w["leg_pnl"].sum() + w[col].sum())
        return dict(n_days=int(n), total_pnl=tot,
                    ann_pocket=tot / POCKET * 252.0 / max(n, 1))

    combined = {}
    for cb in COST_BPS:
        col = f"pnl_hc{cb:g}"
        combined[f"H-CC-EW_cost{cb:g}bp"] = dict(
            FULL=span_stats(dd, col, "0000-01-01", "9999-12-31"),
            POST=span_stats(dd, col, POST, "9999-12-31"))
    col = "pnl_hc_etf"
    combined["H-CC-EW_etf融券8pct对照"] = dict(
        FULL=span_stats(dd, col, "0000-01-01", "9999-12-31"),
        POST=span_stats(dd, col, POST, "9999-12-31"))
    combined["H0_不对冲基线"] = dict(
        FULL=span_stats(dd, "pnl_net", "0000-01-01", "9999-12-31"),
        POST=span_stats(dd, "pnl_net", POST, "9999-12-31"))

    # ── 判定闸 ──
    neu = combined["H-CC-EW_cost5bp"]
    gates = dict(
        G1_neutral_ann_ge_3pct=bool(neu["FULL"]["ann_pocket"] >= 0.03),
        G2_neutral_post_nonneg=bool(neu["POST"]["ann_pocket"] >= 0.0),
        G3_systematic_r2_ge_50pct=bool(r2_is >= 0.50),
    )

    metrics = dict(
        params=dict(pocket=POCKET, post=POST, cost_bps=COST_BPS,
                    etf_borrow_ann=ETF_BORROW_ANN, warmup=WARMUP),
        data=dict(n_positions=int(len(pos)), n_nights=int(len(night)),
                  n_1night=int((pos["n_nights"] == 1).sum()),
                  idx_align_fail=n_bad,
                  window=[str(night.index.min()), str(night.index.max())]),
        index_selection=sel_rows,
        best_index=best,
        decomp=dict(beta_port=b_is, r2_port=r2_is, corr_port=corr_is,
                    systematic_share=r2_is, idio_share=1 - r2_is,
                    overnight_intraday_split=split,
                    overnight_hedge=dict(beta_on=b_on, r2_on=r2_on)),
        hedge_variants=vstats,
        combined=combined,
        gates=gates,
        note="H-CC-IS 含前视为可行性上界；H-CC-EW 为可执行口径（前 20 夜 warmup 无对冲）。",
        elapsed=round(time.time() - t0, 1))
    (OUT / "hedge_metrics.json").write_text(json.dumps(
        metrics, ensure_ascii=False, indent=2, default=float), encoding="utf-8")

    pd.set_option("display.width", 250)
    print(sel.to_string(index=False, float_format=lambda v: f"{v:,.4f}"), flush=True)
    print(f"\nbest={best}  β={b_is:.3f}  R²={r2_is:.3f}  corr={corr_is:.3f}", flush=True)
    print(f"split(1夜): total={split['mean_total_bp']:.1f}bp "
          f"on={split['mean_overnight_bp']:.1f}bp(t={split['t_overnight']:.2f}) "
          f"id={split['mean_intraday_bp']:.1f}bp(t={split['t_intraday']:.2f})", flush=True)
    print(pd.DataFrame(vstats).to_string(index=False,
          float_format=lambda v: f"{v:,.2f}"), flush=True)
    for k, v in combined.items():
        print(f"{k:28s} FULL ann={v['FULL']['ann_pocket']:+.3%} "
              f"POST ann={v['POST']['ann_pocket']:+.3%}", flush=True)
    print(f"[gates] {gates}  ({metrics['elapsed']}s)", flush=True)


if __name__ == "__main__":
    main()
