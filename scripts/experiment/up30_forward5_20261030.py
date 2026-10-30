# -*- coding: utf-8 -*-
"""「近30日上涨次数占比」升/降 × 未来 5 个交易日收益（owner 2026-10-30，v2 稳健口径）。

问题（owner）：「这个指标为多少、并且处于上升状态时，买入后未来 5 个交易日会有收益？」

⚠️ v1 的坑（已修）：
  · 面板里 2023-08 之前的日期每天只有 1~40 只（缓存只对少数老票有长历史）
    ⇒ 那些日期的「全市场截面均值」毫无意义，会把超额和 t 值整体带偏；
  · 前复权序列里有 −91% / +221% 的极端 5 日收益（退市/复牌、北交所 30% 板连板）
    ⇒ 均值会被单点支配。
  所以 v2 只保留**当天截面 ≥300 只**的日期（2023-08-29 起），并在**每个截面内**把
  未来 5 日收益按 1%/99% 缩尾后再算均值/超额。

口径：
  · up30      = 最近 30 个日收益里 >0 的天数 / 30 × 100（与生产 `_stock_tags_from_df` 同源）；
  · up30_prev = 同口径、窗口整体前移 **5 根**（前端箭头就是这个）；
  · 「升」= up30 > up30_prev（相等单列「平」）；
  · 未来 5 日 = **次日开盘买、第 5 根收盘卖**（信号收盘后才知道，这才可执行）；
    另给「当日收盘买」对照；
  · 超额 = 当日截面去均值（缩尾后的收益）；
  · t 值 = 日聚类 + Newey-West(lag=5)（5 日窗口互相重叠，普通 t 会被放大）。

运行：`python scripts/experiment/up30_forward5_20261030.py`
（首次会从 `tmp/bias5_panel.parquet` 算并缓存 `tmp/up30_fwd5_panel.parquet`）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

SRC = Path("tmp/bias5_panel.parquet")
CACHE = Path("tmp/up30_fwd5_panel.parquet")
NW_LAG = 5
MIN_XS = 300          # 当日截面至少这么多只才算"全市场"
WINS = (0.01, 0.99)   # 每个截面缩尾
LEVELS = [(30, 40), (40, 45), (45, 50), (50, 55), (55, 60), (60, 70), (70, 100.1)]
DELTAS = [(-100, -10), (-10, -3.34), (-3.34, 0), (0, 0.001), (0.001, 3.34),
          (3.34, 10), (10, 100)]


def build() -> pd.DataFrame:
    if CACHE.exists():
        return pd.read_parquet(CACHE)
    df = pd.read_parquet(SRC).sort_values(["code", "date"]).reset_index(drop=True)
    g = df.groupby("code", sort=False)
    up = (g["close"].diff() > 0).astype(float)
    df["up30"] = up.groupby(df["code"]).transform(
        lambda s: s.rolling(30, min_periods=30).mean() * 100.0)
    df["up30_prev"] = g["up30"].transform(lambda s: s.shift(5))
    df["open1"] = g["open"].transform(lambda s: s.shift(-1))
    df["close5"] = g["close"].transform(lambda s: s.shift(-5))
    df["fwd5"] = (df["close5"] / df["open1"] - 1.0) * 100.0
    df["fwd5c"] = (df["close5"] / df["close"] - 1.0) * 100.0
    df = df.dropna(subset=["up30", "up30_prev", "fwd5", "fwd5c"]).copy()
    df["delta"] = df["up30"] - df["up30_prev"]
    out = df[["code", "date", "up30", "up30_prev", "delta", "fwd5", "fwd5c"]].copy()
    # 只留截面够大的日期
    cnt = out.groupby("date").size()
    out = out[out["date"].isin(cnt[cnt >= MIN_XS].index)].copy()
    out.to_parquet(CACHE, index=False)
    return out


def robust(df: pd.DataFrame, col: str) -> pd.DataFrame:
    """每个截面内缩尾，并给出当日全市场均值/中位数与超额。"""
    df = df.copy()
    ql, qh = df.groupby("date")[col].transform(lambda s: s.quantile(WINS[0])), \
        df.groupby("date")[col].transform(lambda s: s.quantile(WINS[1]))
    df[col + "_w"] = df[col].clip(ql, qh)
    base = df.groupby("date")[col + "_w"].transform("mean")
    df[col + "_ex"] = df[col + "_w"] - base
    return df


def nw_t(s: pd.Series, lag: int = NW_LAG) -> float:
    x = s.dropna().to_numpy()
    T = len(x)
    if T < 30:
        return float("nan")
    xc = x - x.mean()
    var = float(xc @ xc) / T
    for l in range(1, lag + 1):
        var += 2.0 * (1.0 - l / (lag + 1.0)) * float(xc[l:] @ xc[:-l]) / T
    se = np.sqrt(max(var, 1e-18) / T)
    return float(x.mean() / se)


def line(tag: str, x: pd.DataFrame, col: str = "fwd5") -> dict:
    """收益/中位/胜率按行（每只股票各算一次）；**超额与 t 按日等权**
    （先算每天截面均值再平均）—— 早期日子只有千余只、晚期五千余只，
    按行平均等于给晚期加了三倍权重；按日等权才是"随便挑一天"的口径。"""
    ex = col + "_ex"
    daily = x.groupby("date")[ex].mean()
    return {"组": tag, "样本": len(x), "股票": x["code"].nunique(), "天数": len(daily),
            "5日收益%": x[col + "_w"].mean(), "中位%": x[col].median(),
            "胜率%": 100 * (x[col] > 0).mean(),
            "超额%(日权)": daily.mean(), "t(NW)": nw_t(daily)}


def show(rows: list, title: str):
    out = pd.DataFrame(rows)
    print(f"\n===== {title} =====")
    with pd.option_context("display.width", 220, "display.max_columns", 30,
                           "display.float_format", lambda v: f"{v:8.2f}"):
        print(out.to_string(index=False))
    return out


def ic(x: pd.DataFrame) -> tuple[float, float]:
    """每日截面 Spearman(up30, fwd5) 的均值与 NW t。"""
    s = x.groupby("date").apply(
        lambda d: d["up30"].corr(d["fwd5"], method="spearman") if len(d) >= 50 else np.nan,
        include_groups=False)
    s = s.dropna()
    return float(s.mean()), nw_t(s)


def main():
    raw = build()
    df = robust(raw, "fwd5")
    df = robust(df, "fwd5c")
    df["rising"] = np.where(df["up30"] > df["up30_prev"], "升",
                            np.where(df["up30"] < df["up30_prev"], "降", "平"))
    print(f"样本 {len(df):,} 行 / {df['code'].nunique():,} 只 / {df['date'].nunique()} 个交易日 / "
          f"{df['date'].min()}~{df['date'].max()}")
    print(f"截面中位数 {df.groupby('date').size().median():.0f} 只/日")

    m, t = ic(df.dropna(subset=["fwd5"]))
    print(f"\n截面 Spearman IC（up30 vs 未来5日）：均值 {m:+.4f}  t(NW) {t:+.2f}")

    rows = [line("【基线】", df)]
    for lo, hi in LEVELS:
        sub = df[(df["up30"] > lo) & (df["up30"] <= hi)]
        for d in ("升", "平", "降"):
            x = sub[sub["rising"] == d]
            if len(x) >= 300:
                rows.append(line(f"({lo:g},{hi:g}]·{d}", x))
    show(rows, "A. 档位 × 方向（全样本）")

    rows = [line("【基线】", df)]
    for lo, hi in DELTAS:
        x = df[(df["delta"] > lo) & (df["delta"] <= hi)]
        if len(x) >= 300:
            rows.append(line(f"Δ({lo:g},{hi:g}]", x))
    show(rows, "B. 按 Δ（up30 − 5日前 up30）分档")

    rows = []
    for thr in (40, 45, 50, 55, 60, 65):
        x = df[(df["up30"] >= thr) & (df["rising"] == "升")]
        if len(x) >= 300:
            rows.append(line(f"上升 且 up30≥{thr}", x))
    for lo, hi in ((45, 55), (45, 60), (40, 55)):
        x = df[(df["up30"] >= lo) & (df["up30"] < hi) & (df["rising"] == "升")]
        rows.append(line(f"上升 且 {lo}≤up30<{hi}", x))
    show(rows, "C. 阈值 / 区间扫描")

    # D. 口径对照
    rows = []
    for tag, msk in {
        "上升 且 up30≥55": (df["delta"] > 0) & (df["up30"] >= 55),
        "上升 且 45≤up30<55": (df["delta"] > 0) & (df["up30"] >= 45) & (df["up30"] < 55),
        "上升 且 up30<45": (df["delta"] > 0) & (df["up30"] < 45),
        "下降 且 up30≥55": (df["delta"] < 0) & (df["up30"] >= 55),
    }.items():
        x = df[msk]
        r1 = line(tag, x, "fwd5")
        r2 = line(tag, x, "fwd5c")
        rows.append({"状态": tag, "口径": "次日开盘买", **{k: v for k, v in r1.items() if k != "组"}})
        rows.append({"状态": tag, "口径": "当日收盘买", **{k: v for k, v in r2.items() if k != "组"}})
    show(rows, "D. 口径对照（缩尾后）")

    # E. 稳定性（半年）
    df["半年"] = df["date"].str[:4] + "H" + ((df["date"].str[5:7].astype(int) > 6).astype(int) + 1).astype(str)
    for tag, msk in {"上升且45≤up30<55": (df["delta"] > 0) & (df["up30"] >= 45) & (df["up30"] < 55),
                     "上升且up30≥55": (df["delta"] > 0) & (df["up30"] >= 55)}.items():
        rows = []
        for h, s in df[msk].groupby("半年"):
            b = df[df["半年"] == h]
            if len(s) < 300:
                continue
            rows.append({"半年": h, "样本": len(s), "收益%": s["fwd5_w"].mean(),
                         "超额%": s.groupby("date")["fwd5_ex"].mean().mean(),
                         "基线超额%": b.groupby("date")["fwd5_ex"].mean().mean(),
                         "胜率%": 100 * (s["fwd5"] > 0).mean(),
                         "t(NW)": nw_t(s.groupby("date")["fwd5_ex"].mean())})
        show(rows, f"E. 稳定性：{tag}（按半年）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
