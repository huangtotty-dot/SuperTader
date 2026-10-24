# -*- coding: utf-8 -*-
"""「均线粘合」前向收益实验（2026-10-23 owner：「选股猎手/建仓扫描/异常监控/技术标签
都加一个均线是否粘合的概念 —— 先做实验研究再实现」）。

## 粘合怎么量化

对每个交易日 t，用截至当日的收盘算 MA5/10/20/30/60：

    spread = (max(MA) - min(MA)) / close          # 五条均线的相对离散度
    粘合   = spread <= thr                        # thr ∈ {1.5%, 2%, 3%, 4%, 5%}

同时记录：
  · `arr`  排列：多头（MA5>MA10>MA20>MA30>MA60）/ 空头（反序）/ 纠缠
  · `dur`  连续粘合天数（截至 t 已连续多少天 spread<=thr）
  · `brk`  当日收盘**首次**站上五条均线的最高值（粘合后向上发散）
  · `amp5` 未来 5 日振幅（高-低）/今收 —— 粘合如果只放大波动、不分辨方向，就该只当"关注"而非买点

## 口径（沿用本仓研究铁律）

  · 进场 = **次日开盘**（信号收盘后才成立）；出场 = T+h 收盘（h=1/3/5/10）
  · 超额 = 减**同一天全样本**同口径均值（不是减事件样本自身）
  · t 值按**日期聚类**；分年看稳定性
  · 数据源 = 本地日线缓存（`t_io/cache/daily_kline/*.json`，前复权，`forming` 行剔除）

用法：python scripts/experiment/ma_convergence_forward_20261023.py [--stocks 0] [--days 700]
      （--stocks 0 = 全部；默认抽样 2000 只）
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
DAILY = BASE / "t_io" / "cache" / "daily_kline"
OUT_MD = BASE / "doc" / "research" / "2026-10-23_均线粘合_前向收益实验.md"
OUT_PARQUET = BASE / "t_io" / "cache" / "ma_convergence_study.parquet"

HOLDS = (1, 3, 5, 10)
THRS = (1.5, 2.0, 3.0, 4.0, 5.0)
MAS = (5, 10, 20, 30, 60)


def _scan_one(args):
    """单只 → 每交易日的特征 + 前向收益。返回 list[dict]。"""
    code, days = args
    import pandas as pd
    try:
        blob = json.loads((DAILY / f"{code}.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = blob.get("rows") or []
    forming = set(str(x)[:10] for x in (blob.get("forming") or []))
    if forming:
        rows = [r for r in rows if str(r["date"])[:10] not in forming]
    if len(rows) < 120:
        return []
    df = pd.DataFrame(rows)
    df["date"] = df["date"].astype(str).str[:10]
    n = len(df)
    c = df["close"].to_numpy(float)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    dates = df["date"].tolist()

    ma = {p: pd.Series(c).rolling(p).mean().to_numpy() for p in MAS}
    vol20 = pd.Series(v).rolling(20).mean().to_numpy()
    ma_mat = np.vstack([ma[p] for p in MAS])          # (5, n)

    start = max(59, n - int(days) - 11)
    out = []
    for t in range(start, n - 11):
        if not np.isfinite(ma_mat[:, t]).all():
            continue
        vals = ma_mat[:, t]
        close = c[t]
        if not (close > 0):
            continue
        spread = float(vals.max() - vals.min()) / close * 100.0
        # 短口径（MA5/10/20）：股民口中的"均线粘合"多半指这三条 —— 一并统计（2026-10-23 补）
        _sv = vals[:3]
        spread_s = float(_sv.max() - _sv.min()) / close * 100.0
        # 连续粘合天数（thr 用 3%，dur 单独给；分析时其他阈值按 spread 自己判）
        arr = "bull" if (vals[0] > vals[1] > vals[2] > vals[3] > vals[4]) else \
              ("bear" if (vals[0] < vals[1] < vals[2] < vals[3] < vals[4]) else "mix")
        entry = o[t + 1] if t + 1 < n else None
        if not (entry > 0):
            continue
        r = {k: (c[t + k] / entry - 1.0) if t + k < n else None for k in HOLDS}
        # 未来 5 日振幅（相对今收）
        hi = float(np.max(h[t + 1:t + 6])) if t + 5 < n else np.nan
        lo = float(np.min(l[t + 1:t + 6])) if t + 5 < n else np.nan
        amp5 = (hi - lo) / close * 100.0 if np.isfinite(hi) and np.isfinite(lo) else None
        out.append({
            "code": code, "date": dates[t],
            "spread": round(spread, 3), "spread_s": round(spread_s, 3),
            "arr": arr,
            "brk": bool(close > vals.max()),           # 当日收盘站上五线最高
            "chg": round((close / c[t - 1] - 1.0) * 100.0, 3) if c[t - 1] else None,
            "vr": round(float(v[t] / vol20[t]), 3) if (vol20[t] and np.isfinite(vol20[t])) else None,
            "r1": r[1], "r3": r[3], "r5": r[5], "r10": r[10],
            "amp5": amp5,
        })
    return out


def _t_by_date(df, col):
    g = df.groupby("date")[col].mean().dropna()
    if len(g) < 5:
        return float("nan"), len(g)
    sd = g.std(ddof=1)
    return (float(g.mean() / (sd / np.sqrt(len(g)))), len(g)) if sd else (float("nan"), len(g))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stocks", type=int, default=2000, help="0 = 全部")
    ap.add_argument("--days", type=int, default=700)
    ap.add_argument("--procs", type=int, default=3)
    a = ap.parse_args()

    codes = sorted(p.stem for p in DAILY.glob("*.json")
                   if p.stem.isdigit() and len(p.stem) == 6)
    if a.stocks and len(codes) > a.stocks:
        step = len(codes) / a.stocks
        codes = [codes[int(i * step)] for i in range(a.stocks)]
    print(f"样本 {len(codes)} 只 × 最近 {a.days} 交易日 · procs={a.procs}", flush=True)

    from concurrent.futures import ProcessPoolExecutor
    rows = []
    with ProcessPoolExecutor(max_workers=a.procs) as ex:
        for i, part in enumerate(ex.map(_scan_one, [(c, a.days) for c in codes], chunksize=16)):
            rows.extend(part)
            if (i + 1) % 200 == 0:
                print(f"  {i + 1}/{len(codes)} 只 · {len(rows)} 行", flush=True)

    import pandas as pd
    df = pd.DataFrame(rows)
    # 只用横截面够厚的日子（老缓存里有只有几只票的残段）
    cnt = df.groupby("date")["code"].nunique()
    _min_n = max(50, int(0.15 * df["code"].nunique()))   # 横截面门槛（小样本冒烟时自适应）
    thick = set(cnt[cnt >= _min_n].index)
    df = df[df["date"].isin(thick)].copy()
    print(f"\n样本 {len(df)} 股票·日 · {df['date'].nunique()} 天 · "
          f"{df['date'].min()} ~ {df['date'].max()} · {df['code'].nunique()} 只", flush=True)

    # 横截面超额（减同日全样本均值）
    for k in HOLDS:
        df[f"x{k}"] = df[f"r{k}"] - df.groupby("date")[f"r{k}"].transform("mean")

    # 连续粘合天数（3% 口径）
    df["conv3"] = df["spread"] <= 3.0
    df = df.sort_values(["code", "date"])
    df["dur3"] = (df.groupby("code")["conv3"]
                  .apply(lambda s: s.groupby((~s).cumsum()).cumcount() + 1)
                  .reset_index(level=0, drop=True))
    df.loc[~df["conv3"], "dur3"] = 0
    df.to_parquet(OUT_PARQUET, index=False)

    def summ(name, d):
        if len(d) < 20:
            return {"组": name, "n": len(d)}
        o = {"组": name, "n": len(d)}
        for k in HOLDS:
            o[f"x{k}"] = round(d[f"x{k}"].mean() * 100, 3)
        t5, _ = _t_by_date(d, "x5")
        o["t5"] = round(t5, 2) if t5 == t5 else None
        o["胜率5"] = round((d["r5"] > 0).mean() * 100, 1)
        o["amp5"] = round(d["amp5"].mean(), 2) if d["amp5"].notna().any() else None
        return o

    def rowline(o):
        return (f"| {o['组']} | {o.get('n', 0)} | {o.get('x1', '—')}% | {o.get('x3', '—')}% | "
                f"{o.get('x5', '—')}% | {o.get('x10', '—')}% | {o.get('t5', '—')} | "
                f"{o.get('胜率5', '—')}% | {o.get('amp5', '—')}% |")

    L = []
    def W(s=""):
        print(s, flush=True)
        L.append(s)

    hdr = ("| 组 | n | T+1 | T+3 | T+5 | T+10 | t(T+5) | 胜率5 | 未来5日振幅 |\n"
           "|---|---|---|---|---|---|---|---|---|")
    W("# 均线粘合（MA5/10/20/30/60）前向收益实验（2026-10-23）\n")
    W(f"* 样本 **{len(df)}** 股票·日 · {df['date'].nunique()} 天 · {df['code'].nunique()} 只 · "
      f"{df['date'].min()} ~ {df['date'].max()}")
    W("* 进场=次日开盘；超额=减**同日全样本**均值；t 按日期聚类；粘合 = 五条均线相对离散度 ≤ 阈值\n")

    W("## 一、全样本 vs 粘合（3% 口径）\n")
    W(hdr)
    W(rowline(summ("全样本", df)))
    W(rowline(summ("粘合 spread≤3%", df[df["spread"] <= 3.0])))
    W("")

    W("## 二、阈值扫描（T+5 超额）\n")
    W(hdr)
    for thr in THRS:
        W(rowline(summ(f"spread≤{thr}%", df[df["spread"] <= thr])))
    W("")

    W("## 二B、**短口径**（MA5/10/20）阈值扫描（T+5 超额）\n")
    W(hdr)
    for thr in THRS:
        W(rowline(summ(f"短口径 spread≤{thr}%", df[df["spread_s"] <= thr])))
    W("")

    W("## 三、粘合(≤3%) × 均线排列\n")
    W(hdr)
    conv = df[df["spread"] <= 3.0]
    for arr, lab in (("bull", "粘合 + 多头排列"), ("bear", "粘合 + 空头排列"), ("mix", "粘合 + 纠缠")):
        W(rowline(summ(lab, conv[conv["arr"] == arr])))
    W(rowline(summ("不粘合 + 多头排列", df[(df["spread"] > 3.0) & (df["arr"] == "bull")])))
    W("")

    W("## 四、粘合持续性（spread≤3% 已连续 N 天）\n")
    W(hdr)
    for lo, hi in ((1, 2), (3, 5), (6, 10), (11, 999)):
        d = conv[(conv["dur3"] >= lo) & (conv["dur3"] <= hi)]
        W(rowline(summ(f"连续 {lo}~{hi if hi < 999 else '∞'} 天", d)))
    W("")

    W("## 五、粘合 + 当日向上发散（收盘站上五线最高）\n")
    W(hdr)
    W(rowline(summ("粘合 + 收盘站上五线最高", conv[conv["brk"]])))
    W(rowline(summ("粘合 + 未站上", conv[~conv["brk"]])))
    W(rowline(summ("不粘合 + 站上五线最高", df[(df["spread"] > 3.0) & df["brk"]])))
    W("")

    W("## 六、分年（粘合 ≤3% 的 T+5 超额）\n")
    W("| 年 | 全样本 T+5 | 粘合 T+5 | 粘合 n |")
    W("|---|---|---|---|")
    yr = pd.to_datetime(df["date"]).dt.year
    for y in sorted(set(yr)):
        m = yr == y
        a5 = df.loc[m, "x5"].dropna()
        b5 = df.loc[m & (df["spread"] <= 3.0), "x5"].dropna()
        if len(b5) < 20:
            continue
        W(f"| {y} | {a5.mean() * 100:+.2f}% | {b5.mean() * 100:+.2f}% | {len(b5)} |")
    W("")

    W("## 七、粘合后的方向 vs 波动\n")
    W(f"* 粘合(≤3%) 未来 5 日振幅均值 **{conv['amp5'].mean():.2f}%** · "
      f"非粘合 **{df[df['spread'] > 3.0]['amp5'].mean():.2f}%**")
    W(f"* 粘合 T+5 超额均值 {conv['x5'].mean() * 100:+.3f}% · 胜率 {(conv['r5'] > 0).mean() * 100:.1f}%")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"\n写入 {OUT_MD}\n落盘 {OUT_PARQUET.name}")


if __name__ == "__main__":
    main()
