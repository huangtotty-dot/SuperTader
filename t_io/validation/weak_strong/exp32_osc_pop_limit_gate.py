# -*- coding: utf-8 -*-
"""弱转强研究线 · 实验32：OSC 早盘冲高卖**限价化**的上线闸门回测（2026-10-19）
=====================================================================================
背景（2026-10-19 实盘）：601118 冲高触发 pop_sell（10:00 棒 high=6.74 ≥ 6.53×1.03=6.73），
**市价单**成交 6.50 < 成本 6.59 ⇒ +3% 信号卖成 -1.4% 亏损单。owner 拍板方向 (a)：
改**限价单**（挂触发价×0.998 附近），不成交就撤单回到持有轨道。

闸门铁律（与替补机制 exp26b 同款，owner 2026-10-16 口径）：
    **Sharpe 相对下降 <10% 且年化收益不降，过闸才改生产。**
    基准 = 现口径（冲高触发 → 10:00 市价卖）。

两臂（同一批信号、同一 top-4 组合框架）：
  B0 现口径   : D+1 10:00 棒 high ≥ E×(1+3%) 触发 ⇒ 市价卖（成交价=10:00 棒 close 代理）；
                未触发 ⇒ 持满 5 日收盘。
  L  限价化   : 触发 ⇒ 挂限价 E×1.03×ratio（默认 ratio=0.998）；
                D+1 10:30 棒 high ≥ 限价 ⇒ 限价成交（成交价=限价）；
                未成交 ⇒ 撤单回持有轨道，持满 5 日收盘（与生产一致：pop 只判 days_held==0
                的次日早盘，撤单后**不再**重新挂 pop，交 14:50 止损/到期链路）。

口径与近似（全部披露，与 exp26b/exp31 同款）：
  · 30min 粒度：限价成交窗口 ≈ 10:00~10:30（生产实际 10:08 撤单，窗口更窄 ⇒
    回测成交率**偏乐观**，结论按此方向打折看）；
  · 组合框架复刻 exp12.portfolio_stats：日收益 = 当日在持腿等权均值 − DAILY_COST，
    top-4 按 gap_open 降序；pop 退出腿的收益在 D+1 实现、之后不再占用仓位；
  · 宇宙 = build_signal 全信号（未叠板块过滤，与 exp31 一致；板块过滤两臂同享，
    不影响对比方向）；
  · 滑点：B0 腿滑点 = 10:00close/触发价−1（实测均值约 −1.5%，exp31 fade 口径）；
    L 腿成交时滑点恒 = ratio−1（−0.2%），代价是成交率 <100%。

运行：python t_io/validation/weak_strong/exp32_osc_pop_limit_gate.py
产物：results/exp32_osc_pop_limit_gate.json
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
from exp6_exit_rules import build_signal, summarize                       # noqa: E402
from exp12_sector_filter import DAILY_COST, TRADING_DAYS                  # noqa: E402

DATA_DIR = HERE / "data"
OUT_DIR = HERE / "results"

POP_TARGET = 0.03          # 生产 pop_target_pct
TOP_N = 4                  # 生产 top_n
RATIOS = [1.0, 0.998, 0.995, 0.99]   # 限价=触发价×ratio 稳健性扫描（0.998=owner 拍板值）


def portfolio_with_pop(sig: pd.DataFrame, dates: list, day1_exit: np.ndarray,
                       top_n=TOP_N) -> dict:
    """复刻 exp12.portfolio_stats 的组合日收益框架，支持「D+1 提前退出」腿。

    day1_exit[i] 非 NaN ⇒ 第 i 腿在 D+1 以该收益出场（之后不再持有）；
    NaN ⇒ 走标准 5 日收盘路径（close1..close5 / sig_close 链，与 exp26b 一致 close:=sig_close）。
    """
    s = sig.copy()
    if top_n != "all":
        s["rank"] = s.groupby("date")["gap_open"].rank(method="first", ascending=False)
        sel = (s["rank"] <= top_n).values
    else:
        sel = np.ones(len(s), dtype=bool)
    day1_exit = np.asarray(day1_exit, dtype=float)[sel]
    s = s[sel].reset_index(drop=True)
    if len(s) == 0:
        return dict(ann=np.nan, sharpe=np.nan, mdd=np.nan, n_pos=0, n_trades=0)
    didx = {d: i for i, d in enumerate(dates)}
    entry_idx = s["date"].map(didx).values
    C = np.column_stack([s["sig_close"].values] +
                        [s[f"close{k}"].values for k in range(1, 6)])
    R = C[:, 1:] / C[:, :-1] - 1
    early = np.isfinite(day1_exit)
    n_days = len(dates)
    daily_ret = np.full(n_days, np.nan)
    n_pos = np.zeros(n_days, dtype=int)
    for t in range(n_days):
        k = t - entry_idx - 1
        hold = (entry_idx < t) & (t <= entry_idx + 5) & (~early) & (k >= 0)
        pop = (entry_idx < t) & (early) & (k == 0)
        rets = []
        if hold.any():
            rets.append(R[hold, k[hold]])
        if pop.any():
            rets.append(day1_exit[pop])
        if rets:
            daily_ret[t] = np.nanmean(np.concatenate(rets))
            n_pos[t] = int(hold.sum() + pop.sum())
    daily_ret = pd.Series(daily_ret, index=dates).dropna() - DAILY_COST
    nav = (1 + daily_ret).cumprod()
    ann = (1 + daily_ret.mean()) ** TRADING_DAYS - 1
    sharpe = daily_ret.mean() / daily_ret.std(ddof=1) * np.sqrt(TRADING_DAYS)
    mdd = (nav / nav.cummax() - 1).min()
    return dict(ann=float(ann), sharpe=float(sharpe), mdd=float(mdd),
                n_pos=float(n_pos.mean()), n_trades=int(len(s)))


def gate(s_base: dict, s_arm: dict) -> dict:
    """owner 口径：Sharpe 相对下降 <10% 且年化收益不降。"""
    d_sharpe = s_arm["sharpe"] - s_base["sharpe"]
    d_rel = d_sharpe / abs(s_base["sharpe"]) if s_base["sharpe"] else float("nan")
    d_ann = s_arm["ann"] - s_base["ann"]
    return {"sharpe_base": s_base["sharpe"], "sharpe_arm": s_arm["sharpe"],
            "delta_sharpe": d_sharpe, "delta_sharpe_rel": d_rel,
            "ann_base": s_base["ann"], "ann_arm": s_arm["ann"], "delta_ann": d_ann,
            "mdd_base": s_base["mdd"], "mdd_arm": s_arm["mdd"],
            "pass": bool(np.isfinite(d_rel) and d_rel > -0.10 and d_ann >= 0)}


def main() -> None:
    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    panel = pd.read_parquet(DATA_DIR / "min30_panel_full.parquet")
    daily = pd.read_parquet(DATA_DIR / "daily_full.parquet")
    sig = build_signal(panel, daily).reset_index(drop=True)
    sig["gap_open"] = sig["sig_close"] / sig["prev_close"] - 1

    # D+1 的 10:00/10:30 棒（exp31 同款 merge）
    pan = panel.sort_values(["symbol", "date", "dt"], kind="mergesort").reset_index(drop=True)
    pan["bar_idx"] = pan.groupby(["symbol", "date"]).cumcount()
    b1 = pan[pan["bar_idx"] == 1].set_index(["symbol", "date"])[["high", "close"]].rename(
        columns={"high": "b1_high", "close": "b1_close"})
    b2 = pan[pan["bar_idx"] == 2].set_index(["symbol", "date"])[["high"]].rename(
        columns={"high": "b2_high"})
    bars = b1.join(b2, how="outer").reset_index()
    ud = pan[["symbol", "date"]].drop_duplicates().sort_values(["symbol", "date"])
    ud["next_date"] = ud.groupby("symbol")["date"].shift(-1)
    bars_next = bars.merge(ud, on=["symbol", "date"], how="left").dropna(subset=["next_date"])
    bars_next = bars_next.drop(columns=["date"]).rename(columns={"next_date": "date"})
    sig = sig.merge(bars_next, on=["symbol", "date"], how="left")

    dates = sorted(d for d in daily["date"].astype(str).unique()
                   if d >= str(sig["date"].min()))
    sig["date"] = sig["date"].astype(str)

    E = sig["sig_close"].values
    trig_px = E * (1 + POP_TARGET)
    trig = sig["b1_high"].values >= trig_px
    n_sig, n_trig = len(sig), int(np.nansum(trig))
    print(f"[signal] {n_sig} 条；D+1 10:00 冲高触发 {n_trig} 条 "
          f"({n_trig / n_sig * 100:.1f}%)", flush=True)

    report = {"target": POP_TARGET, "top_n": TOP_N, "signal_n": n_sig,
              "trigger_n": n_trig, "trigger_rate": n_trig / n_sig,
              "universes": {}, "approximations": [
                  "30min粒度: 限价成交窗口≈10:00~10:30(生产10:08撤单更窄⇒成交率偏乐观)",
                  "B0成交价=10:00棒close代理市价(exp31已验证该代理偏保守于E0幻想口径)",
                  "宇宙=build_signal全信号未叠板块过滤(两臂同享,不影响对比方向)",
                  "撤单后不再重挂pop(与生产days_held==0口径一致),持满5日收盘",
                  "top4生产口径本面板114天零激活⇒该口径两臂恒等,闸门判据以all全信号口径为准"]}

    # ── 触发腿明细（滑点/成功率统计母体）──
    s_trig = sig[trig].copy()
    tE = s_trig["sig_close"].values
    t_px = tE * (1 + POP_TARGET)
    report["trigger_detail"] = {
        "n": int(len(s_trig)),
        "B0_avg_slippage_vs_trigger": float(np.nanmean(
            s_trig["b1_close"].values / t_px - 1)),
        "B0_exit_stats": summarize(pd.Series(
            s_trig["b1_close"].values / tE - 1)),
    }

    # ── 双宇宙口径（2026-10-19 实测发现必须分口径）──
    # top4 = 生产口径（gap top-4）：⚠️ 本面板 114 天里 top-4 腿**零激活**冲高卖
    #   （高 gap 票次日早盘均值 -8.8%，冲高回落轮不到 +3% 止盈）⇒ 两臂恒等，闸门不可判；
    # all  = 全信号等权：1663 条触发腿在此，唯一能**分辨**两臂的组合口径。
    for uni_name, uni_n in (("top4_生产口径", TOP_N), ("all_全信号", "all")):
        uni = {}
        b0_exit = np.where(trig, sig["b1_close"].values / E - 1, np.nan)
        s_b0 = portfolio_with_pop(sig, dates, b0_exit, top_n=uni_n)
        uni["baseline_B0"] = s_b0
        _sel = np.ones(len(sig), bool) if uni_n == "all" else \
            (sig.assign(r=sig.groupby("date")["gap_open"]
                        .rank(method="first", ascending=False))["r"] <= uni_n).values
        uni["trigger_n_selected"] = int(np.nansum(trig & _sel))
        print(f"[{uni_name}] B0 ann={s_b0['ann'] * 100:+.1f}%  sharpe={s_b0['sharpe']:.2f}  "
              f"mdd={s_b0['mdd'] * 100:.1f}%  trades={s_b0['n_trades']}  "
              f"触发腿={uni['trigger_n_selected']}", flush=True)
        for ratio in RATIOS:
            lp = trig_px * ratio
            filled = trig & (sig["b2_high"].values >= lp)
            l_exit = np.where(filled, lp / E - 1, np.nan)   # 未成交→NaN→5日路径
            s_l = portfolio_with_pop(sig, dates, l_exit, top_n=uni_n)
            g = gate(s_b0, s_l)
            fr = float(np.nansum(filled) / n_trig) if n_trig else float("nan")
            uni[f"L_ratio_{ratio}"] = {
                "ratio": ratio, "portfolio": s_l, "gate": g,
                "limit_fill_rate_of_triggered": fr,
                "filled_avg_slippage_vs_trigger": ratio - 1.0,
                "triggered_exit_stats": summarize(pd.Series(
                    np.where(filled[trig], lp[trig] / tE - 1,
                             s_trig["close5"].values / tE - 1)))}
            print(f"  [L ratio={ratio}] ann={s_l['ann'] * 100:+.1f}%  "
                  f"sharpe={s_l['sharpe']:.2f}  成交率={fr * 100:.0f}%  "
                  f"Δsharpe_rel={g['delta_sharpe_rel'] * 100:+.1f}%  "
                  f"Δann={g['delta_ann'] * 100:+.2f}pp  "
                  f"闸={'过' if g['pass'] else '破'}", flush=True)
        report["universes"][uni_name] = uni

    fp = OUT_DIR / "exp32_osc_pop_limit_gate.json"
    fp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                  encoding="utf-8")
    print(f"\n[done] {fp}  ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    main()
