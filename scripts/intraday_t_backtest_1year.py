#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
论文因子日内做T回测 - 1年对比（修正 FINAL_CONCLUSION 偏航：RSI日线 → 日内做T因子）

背景: FINAL_CONCLUSION.txt 用日线RSI得出"日线最优,不做日内"的结论, 但
  1. RSI 不在论文精选推荐因子中(记忆: 未在论文中, 非重点)
  2. 用户目标是改进日内做T, 而非放弃做T转日线
  3. 论文因子(时段分割/Renko+MACD/微观结构)从未在1年数据验证

本脚本: 用 5支×1年×1min 数据, 在"每天一买一卖做T"规则下对比:
  baseline       当前生产做T(5分布林触轨+15分MACD确认)
  segment        时段分割(论文1 Xu&Zhu 2022)
  renko_macd     Renko+MACD(论文4 Asrani 2025)
  microstructure 微观结构(论文6 Phan 2016)
  combo          segment + microstructure 叠加
"""
import sys
import os
from pathlib import Path
from datetime import datetime

if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import pandas as pd
import numpy as np

from analysis.indicators import (
    resample_to_5min, add_5min_indicators,
    resample_to_15min, add_15min_indicators,
)
from analysis.renko_builder import RenkoBuilder

# ============================================================
# 参数
# ============================================================
COST = 0.0005          # 双边手续费 5bps
FORCE_EXIT_MIN = 14 * 60 + 55   # 尾盘 14:55 强制平仓(做T当日闭环)
DATA_DIR = BASE / "t_io" / "backtest_1year_data"
OUT_FILE = BASE / "t_io" / "backtest_1year" / "intraday_t_factor_comparison.txt"

STOCKS = [
    "000988.SZ", "002176.SZ", "002202.SZ", "002261.SZ", "002451.SZ",
]

SCHEMES = ["baseline", "segment", "renko_macd", "microstructure", "combo"]


# ============================================================
# 指标预处理（每支股票一次）
# ============================================================
class StockData:
    def __init__(self, df1):
        self.df1 = df1
        self.df5 = add_5min_indicators(resample_to_5min(df1))
        self.df15 = add_15min_indicators(resample_to_15min(df1))
        self._attach_15m_macd()
        self._micro_feats()

    def _attach_15m_macd(self):
        """把 15min MACD 柱 映射到 5min bar（按 floor15，无前瞻：5min bar 收盘时其15min bar 已收盘）"""
        m15 = self.df15[["time", "macd_hist_15m"]].rename(columns={"time": "t15"})
        df5 = self.df5.copy()
        df5["t15"] = df5["time"].dt.floor("15min")
        self.df5 = df5.merge(m15, on="t15", how="left")
        self.df5["macd_hist_15m"] = self.df5["macd_hist_15m"].fillna(0.0)
        self.df5["bb_pct_5m"] = self.df5["bb_pct_5m"].clip(0, 1)
        # 5min bar 收盘时刻 t_val(HHMM) 供时段判定
        self.df5["t_val"] = self.df5["time"].dt.hour * 100 + self.df5["time"].dt.minute

    def _micro_feats(self):
        """微观结构特征：当日累计 VWAP / 价差代理 / 量比 / 触底 / 反弹"""
        df5 = self.df5
        day = df5["time"].dt.date
        df5["cum_amount"] = df5.groupby(day)["amount"].cumsum()
        df5["cum_vol"] = df5.groupby(day)["volume"].cumsum()
        df5["vwap_cum"] = df5["cum_amount"] / df5["cum_vol"].replace(0, np.nan)
        df5["spread"] = (df5["close"] - df5["vwap_cum"]).abs() / df5["vwap_cum"]
        df5["spread_ma5"] = df5["spread"].rolling(5, min_periods=1).mean()
        df5["spread_widen"] = df5["spread"] > df5["spread_ma5"] * 1.3
        df5["vol_ratio_5m"] = df5["vol_ratio_5m"].fillna(1.0)
        df5["vol_spike"] = df5["vol_ratio_5m"] >= 1.5
        df5["vol_shrink"] = df5["vol_ratio_5m"] < 0.6
        low_ma5 = df5.groupby(day)["low"].rolling(5, min_periods=1).min().reset_index(level=0, drop=True)
        df5["at_support"] = df5["close"] <= low_ma5 * 1.002
        df5["reversing"] = df5["close"] >= df5["open"]


# ============================================================
# 信号生成 → (buy_times:set[Timestamp], sell_times:set[Timestamp])
# 触发时刻 = 5min bar 收盘时刻（无前瞻）
# ============================================================
def _segment_of(t_val):
    if 930 <= t_val < 1130:
        return "morning"
    if 1300 <= t_val < 1400:
        return "noon"
    if 1400 <= t_val < 1500:
        return "afternoon"
    return "other"


def sig_baseline(sd: StockData):
    df5 = sd.df5
    buy = (df5["bb_pct_5m"] <= 0.0) & (df5["macd_hist_15m"] > 0)
    sell = (df5["bb_pct_5m"] >= 1.0) & (df5["macd_hist_15m"] < 0)
    return set(df5.loc[buy, "time"]), set(df5.loc[sell, "time"])


def sig_segment(sd: StockData):
    df5 = sd.df5
    seg = df5["t_val"].map(_segment_of)
    bb = df5["bb_pct_5m"]
    macd = df5["macd_hist_15m"]
    # 论文时段阈值: 早盘动量(宽松追势) / 午盘反转(宽松低吸) / 午后常规 / 尾盘禁新开仓
    buy_lo = np.where(seg == "morning", 0.25, np.where(seg == "noon", 0.30, 0.15))
    sell_hi = np.where(seg == "morning", 0.75, np.where(seg == "noon", 0.70, 0.85))
    buy = (bb <= buy_lo) & (macd > 0) & (seg != "other") & (df5["t_val"] < 1430)
    sell = (bb >= sell_hi) & (macd < 0) & (seg != "other")
    return set(df5.loc[buy, "time"]), set(df5.loc[sell, "time"])


def sig_renko(sd: StockData):
    """Renko砖(0.3%)方向 + 15min MACD 确认；触发时刻=新砖形成分钟"""
    df1 = sd.df1
    # 每个1min行映射所属15min bar 的 macd
    m15 = sd.df15[["time", "macd_hist_15m"]].rename(columns={"time": "t15"})
    df = df1.copy()
    df["t15"] = df["time"].dt.floor("15min")
    df = df.merge(m15, on="t15", how="left")
    df["macd_hist_15m"] = df["macd_hist_15m"].fillna(0.0)

    builder = RenkoBuilder(brick_size_pct=0.003)
    buy_times, sell_times = set(), set()
    for row in df.itertuples():
        created = builder.update(row.time, row.close, row.high, row.low, row.volume)
        if created:
            if builder.brick_direction == "down" and row.macd_hist_15m > 0:
                buy_times.add(row.time)
            elif builder.brick_direction == "up" and row.macd_hist_15m < 0:
                sell_times.add(row.time)
    return buy_times, sell_times


def sig_microstructure(sd: StockData):
    df5 = sd.df5
    macd = df5["macd_hist_15m"]
    bb = df5["bb_pct_5m"]
    # 低吸: BOLL低 + 缩量 + 触底反弹 + 15分MACD金叉
    buy = (bb <= 0.15) & df5["vol_shrink"] & df5["at_support"] & df5["reversing"] & (macd > 0)
    # 高抛: BOLL高 + (价差扩大|放量) + 15分MACD死叉
    sell = (bb >= 0.85) & (df5["spread_widen"] | df5["vol_spike"]) & (macd < 0)
    return set(df5.loc[buy, "time"]), set(df5.loc[sell, "time"])


def sig_combo(sd: StockData):
    b_t, s_t = sig_baseline(sd)
    bm_t, sm_t = sig_microstructure(sd)
    df5 = sd.df5
    seg = df5["t_val"].map(_segment_of)
    # 时段允许新开仓(早盘/午盘) 且 14:30前
    allowed = (seg != "other") & (df5["t_val"] < 1430)
    b_allowed = set(df5.loc[allowed, "time"])
    buy = b_t & bm_t & b_allowed
    sell = s_t & sm_t
    return buy, sell


SIG_FUNCS = {
    "baseline": sig_baseline,
    "segment": sig_segment,
    "renko_macd": sig_renko,
    "microstructure": sig_microstructure,
    "combo": sig_combo,
}


# ============================================================
# 每天一买一卖做T回测
# ============================================================
def t_backtest(df1, buy_times, sell_times, max_hold_min=None):
    """按日扫描1min：每日最多一轮 低吸买入→高抛卖出，当日闭环。

    max_hold_min=None → 尾盘14:55强平(持有一天, 含贝塔收益)
    max_hold_min=60    → 买入后60分钟强平(短线做T, 剥离整天贝塔)
    返回 (trades, n_normal_sell, n_force_exit)
    """
    normal_pnls = []
    force_pnls = []
    for day, day_df in df1.groupby(df1["time"].dt.date):
        in_pos = False
        entry_px = 0.0
        entry_t = None
        round_done = False
        for row in day_df.itertuples():
            t = row.time
            px = row.close
            t_min = t.hour * 60 + t.minute
            if not in_pos and not round_done:
                if t in buy_times:
                    entry_px = px * (1 + COST)
                    entry_t = t
                    in_pos = True
            elif in_pos:
                if t in sell_times:
                    normal_pnls.append((row.close * (1 - COST) - entry_px) / entry_px * 100)
                    in_pos = False
                    round_done = True
                elif max_hold_min is not None and entry_t is not None \
                        and (t - entry_t).total_seconds() / 60 >= max_hold_min:
                    force_pnls.append((row.close * (1 - COST) - entry_px) / entry_px * 100)
                    in_pos = False
                    round_done = True
                elif max_hold_min is None and t_min >= FORCE_EXIT_MIN:
                    force_pnls.append((row.close * (1 - COST) - entry_px) / entry_px * 100)
                    in_pos = False
                    round_done = True
    trades = normal_pnls + force_pnls
    return trades, normal_pnls, force_pnls


def summarize(trades):
    if not trades:
        return {"trades": 0, "win_rate": 0.0, "avg_profit": 0.0, "total_profit": 0.0, "daily_avg": 0.0}
    arr = np.array(trades)
    return {
        "trades": len(arr),
        "win_rate": float((arr > 0).mean()),
        "avg_profit": float(arr.mean()),
        "total_profit": float(arr.sum()),
        "daily_avg": len(arr) / 244,
    }


# ============================================================
# 买入信号择时质量（不依赖卖出信号，衡量"买入后价格是否上涨"）
# ============================================================
def buy_quality_report(stock_datas):
    """对每支股票每个方案，统计每日首个买入信号后 +15/30/60min 收益。
    剔除跨日（收盘前不足 horizon 分钟则跳过）。"""
    lines = []
    def log(msg=""):
        print(msg)
        lines.append(msg)
    log("\n" + "=" * 96)
    log("📈 买入信号择时质量 (不含手续费, 每日首个买入信号, 衡量买入择时 alpha)")
    log("   —— 这是最干净地判断'低吸买点是否有效'的指标")
    log("=" * 96)
    log(f"{'方案':<16}{'样本':>6}{'胜率':>8}{'平均':>9}{'样本':>6}{'胜率':>8}{'平均':>9}{'样本':>6}{'胜率':>8}{'平均':>9}")
    log(f"{'':<16}{'+15min':>6}{'':>8}{'':>9}{'+30min':>6}{'':>8}{'':>9}{'+60min':>6}{'':>8}{'':>9}")
    log("-" * 96)
    for scheme in SCHEMES:
        row = f"{scheme:<16}"
        for horizon in (15, 30, 60):
            pnls = []
            for code, sd in stock_datas.items():
                df1 = sd.df1
                buy_times = SIG_FUNCS[scheme](sd)[0]
                px_map = dict(zip(df1["time"], df1["close"]))
                for day, d in df1.groupby(df1["time"].dt.date):
                    buys = sorted([t for t in d["time"] if t in buy_times])
                    if not buys:
                        continue
                    t0 = buys[0]
                    t1 = t0 + pd.Timedelta(minutes=horizon)
                    if t1 in px_map and px_map.get(t0, 0) > 0:
                        pnls.append((px_map[t1] - px_map[t0]) / px_map[t0] * 100)
            arr = np.array(pnls)
            if len(arr):
                row += f"{len(arr):>6d}{((arr > 0).mean() * 100):>7.1f}%{arr.mean():>+8.3f}%"
            else:
                row += f"{0:>6d}{'--':>7}{'':>8}"
        log(row)
    log("\n>50%胜率 = 买入信号有正向择时能力(低吸后倾向反弹)")
    return lines


# ============================================================
# 主流程
# ============================================================
def run_one_mode(max_hold_min, mode_label):
    lines = []
    def log(msg=""):
        print(msg)
        lines.append(msg)

    log("=" * 96)
    log(f"论文因子 日内做T回测对比 | {mode_label}")
    log("规则: 每天一买一卖做T / 手续费双边5bps"
        + (f" / 买入后{max_hold_min}分钟强平(短线做T, 剥离贝塔)" if max_hold_min else " / 尾盘14:55强平(含贝塔)"))
    log("数据: 5支 × 1年 × 1min  |  时间: 2025-08-26 ~ 2026-08-26")
    log("=" * 96)

    results = {}   # scheme -> {code: metrics}
    stock_datas = {}
    for code in STOCKS:
        csv_path = DATA_DIR / f"{code}_1year_1min.csv"
        df1 = pd.read_csv(csv_path, parse_dates=["time"])
        df1 = df1.sort_values("time").reset_index(drop=True)
        df1 = df1.dropna(subset=["close"])
        sd = StockData(df1)
        stock_datas[code] = sd

        for scheme in SCHEMES:
            try:
                buy_times, sell_times = SIG_FUNCS[scheme](sd)
                trades, normal_pnls, force_pnls = t_backtest(df1, buy_times, sell_times, max_hold_min=max_hold_min)
                m = summarize(trades)
                m["n_normal"] = len(normal_pnls)
                m["n_force"] = len(force_pnls)
                m["normal_pnls"] = normal_pnls
                m["force_pnls"] = force_pnls
                results.setdefault(scheme, {})[code] = m
                log(f"  {code} [{scheme:14s}] 交易数={m['trades']:3d}  "
                    f"胜率={m['win_rate']*100:5.1f}%  平均收益={m['avg_profit']:+.3f}%  "
                    f"(正常高抛{len(normal_pnls)}/强平{len(force_pnls)})")
            except Exception as e:
                log(f"  {code} [{scheme:14s}] ERROR: {str(e)[:80]}")

    # ---- 汇总表 ----
    log("\n" + "=" * 96)
    log("🏆 方案对比汇总 (5股平均)")
    log("=" * 96)
    log(f"{'方案':<16}{'总交易':>7}{'胜率':>8}{'平均收益':>10}{'总收益':>11}{'日均':>6}{'正常高抛':>8}{'个股>55%':>8}")
    log("-" * 96)

    summary = {}
    for scheme in SCHEMES:
        per_code = results.get(scheme, {})
        if not per_code:
            continue
        wrs = [m["win_rate"] for m in per_code.values() if m["trades"] > 0]
        avg_profits = [m["avg_profit"] for m in per_code.values() if m["trades"] > 0]
        total_trades = sum(m["trades"] for m in per_code.values())
        total_profit = sum(m["total_profit"] for m in per_code.values())
        n_normal = sum(m.get("n_normal", 0) for m in per_code.values())
        if not wrs:
            continue
        beat55 = sum(1 for w in wrs if w > 0.55)
        summary[scheme] = {
            "trades": total_trades,
            "win_rate": float(np.mean(wrs)),
            "avg_profit": float(np.mean(avg_profits)),
            "total_profit": total_profit,
            "n_normal": n_normal,
            "beat55": beat55,
        }
        log(f"{scheme:<16}{total_trades:>7}{np.mean(wrs)*100:>7.1f}%{np.mean(avg_profits):>+9.3f}%"
            f"{total_profit:>+10.2f}%{total_trades/244:>6.1f}{n_normal:>8d}{beat55:>6d}/5")

    # ---- 正常高抛(信号闭环)质量 ----
    log("\n📊 正常高抛(卖出信号触发闭环) vs 强平 —— 真实做T择时能力")
    log(f"{'方案':<16}{'正常笔数':>8}{'正常胜率':>9}{'正常均收益':>11}{'强平笔数':>8}{'强平胜率':>9}")
    log("-" * 96)
    for scheme in SCHEMES:
        per_code = results.get(scheme, {})
        all_norm, all_force = [], []
        for code, m in per_code.items():
            all_norm += m.get("normal_pnls", [])
            all_force += m.get("force_pnls", []) if "force_pnls" in m else []
        n_n, n_f = len(all_norm), len(all_force)
        wr_n = (np.array(all_norm) > 0).mean() * 100 if n_n else 0.0
        wr_f = (np.array(all_force) > 0).mean() * 100 if n_f else 0.0
        avg_n = np.mean(all_norm) if n_n else 0.0
        log(f"{scheme:<16}{n_n:>8d}{wr_n:>8.1f}%{avg_n:>+10.3f}%{n_f:>8d}{wr_f:>8.1f}%")

    # ---- 相对基线的结论 ----
    log("\n" + "=" * 96)
    log("🎯 相对 baseline 的增量 (胜率pp / 平均收益pp)")
    log("=" * 96)
    base = summary.get("baseline", {})
    if base:
        for scheme in ["segment", "renko_macd", "microstructure", "combo"]:
            s = summary.get(scheme)
            if not s:
                continue
            d_wr = (s["win_rate"] - base["win_rate"]) * 100
            d_ap = s["avg_profit"] - base["avg_profit"]
            log(f"{scheme:<16} 胜率{d_wr:+5.1f}pp   平均收益{d_ap:+.3f}pp    "
                f"交易数 {s['trades']} (基线{base['trades']})")
    log("\n⚠️ 样本量警示: 5股×1年, 交易数<30笔的方案统计结论需谨慎")

    log("\n" + "=" * 96)
    return lines, summary, stock_datas


def main():
    # 模式1: 短线做T (60分钟强平, 剥离整天贝塔) —— 主结论
    lines1, summary1, stock_datas = run_one_mode(60, "短线做T (60分钟强平, 剥离贝塔)")
    print()
    # 买入信号择时质量（独立于卖出侧, 主口径佐证）
    lines_bq = buy_quality_report(stock_datas)
    print()
    # 模式2: 尾盘强平 (持有一天, 含贝塔) —— 对照
    lines2, summary2, _ = run_one_mode(None, "尾盘14:55强平 (持有一天, 含贝塔)")

    all_lines = lines1 + [""] + lines_bq + [""] + lines2
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(all_lines))
    print(f"\n✅ 已保存: {OUT_FILE}")


if __name__ == "__main__":
    main()
