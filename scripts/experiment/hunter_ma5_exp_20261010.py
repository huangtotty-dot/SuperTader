"""实验：选股猎手「站上5日线」三种口径的前向收益对照（2026-10-10）。

对照三种口径（都在信号日收盘建仓，持有到 t+5 收盘）：
  A 布尔「站上」  close > MA5
  B 连续「乖离」  dev5_pct 分档
  C 状态跃迁「刚站上」overnight_reclaim（昨收<昨MA5 且今收>今MA5）

两层 universe：
  · 全池   = 所有有日线缓存的个股（回答「这个量到底有没有前向信息」）
  · 候选层 = 猎手打分器的触发条件（今日最高 > 近20日最高，回答「接进打分器会怎样」）

指标：n / 均值 r5 / 中位 / 胜率 / P5(左尾) / t（另给非重叠子样本 t 做稳健校验）
分层：h1/h2 按日期中位切；另出「排除涨停日」稳健版。

⚠️ 数据说明：t_io/cache/daily_kline 每只 800 根，有**陈旧尾巴**（不同票停在不同日期）
   ⇒ 逐票按自身序列算前向收益，不做跨票对齐；并剔除末根过旧的票。
⚠️ 重叠自相关：5 日前向收益逐日重叠 ⇒ 普通 t 偏高，故另出非重叠子样本 t。
用法：PYTHONIOENCODING=utf-8 python tmp/exp_hunter_ma5.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

BASE = Path(BASE)
DAILY = BASE / "t_io" / "cache" / "daily_kline"

FWD = 5                 # 前向持有天数
STALE_DAYS = 45         # 末根早于全池最新日 N 天 ⇒ 视为陈旧，剔除
MIN_BARS = 300
N150 = 150


def _f(x):
    try:
        v = float(x)
        return v
    except Exception:
        return np.nan


def roll_mean(x, w):
    """向量化滚动均值；窗口内必须全部有效，否则 NaN。"""
    valid = ~np.isnan(x)
    xs = np.where(valid, x, 0.0)
    cs = np.concatenate(([0.0], np.cumsum(xs)))
    cc = np.concatenate(([0], np.cumsum(valid)))
    s = cs[w:] - cs[:-w]
    k = cc[w:] - cc[:-w]
    out = np.full(len(x), np.nan)
    out[w - 1:] = np.where(k == w, s / np.where(k == 0, 1, k), np.nan)
    return out


def roll_max_prev(x, w):
    """t 时刻 = x[t-w:t] 的最大值（**不含今日**）；窗口内有 NaN 则 NaN。"""
    n = len(x)
    out = np.full(n, np.nan)
    if n <= w:
        return out
    try:
        sw = np.lib.stride_tricks.sliding_window_view(x, w)   # 形状 (n-w+1, w)，sw[i]=x[i:i+w]
    except Exception:
        return out
    finite = np.isfinite(sw).all(axis=1)
    mx = np.where(finite, np.nanmax(np.where(np.isfinite(sw), sw, -np.inf), axis=1), np.nan)
    # sw[i] 对应 x[i:i+w]；我们要 t 用 x[t-w:t] ⇒ i = t-w，即 out[t] = mx[t-w]
    out[w:] = mx[:n - w]
    return out


# ---------- 载入面板 ----------
files = [p for p in sorted(DAILY.glob("*.json")) if not p.name.startswith("index_")]
recs = []
for fp in files:
    try:
        rows = (json.loads(fp.read_text(encoding="utf-8")).get("rows") or [])
    except Exception:
        continue
    if len(rows) < MIN_BARS:
        continue
    d = np.array([str(r.get("date"))[:10] for r in rows])
    c = np.array([_f(r.get("close")) for r in rows], dtype=np.float64)
    h = np.array([_f(r.get("high")) for r in rows], dtype=np.float64)
    ok = np.isfinite(c) & np.isfinite(h) & (c > 0) & (h > 0)
    if ok.sum() < MIN_BARS:
        continue
    o = np.argsort(d)
    recs.append((fp.stem, d[o], c[o], h[o], ok[o]))

print(f"载入 {len(recs)} 只个股（每只 ≥{MIN_BARS} 根）")
max_date = max(r[1][-1] for r in recs)
print(f"全池最新日期: {max_date}")
cut = (datetime.strptime(max_date, "%Y-%m-%d") - timedelta(days=STALE_DAYS)).strftime("%Y-%m-%d")
kept = [r for r in recs if r[1][-1] >= cut]
print(f"剔除陈旧尾巴（末根 < {cut}）后剩 {len(kept)} 只\n")

# ---------- 逐票算特征 ----------
acc = {k: [] for k in ("date", "above", "dev5", "reclaim", "cand20", "fwd5", "limitup")}
for code, d, c, h, ok in kept:
    n = len(c)
    if n < N150 + FWD + 2:
        continue
    cs = np.where(ok, c, np.nan)
    hs = np.where(ok, h, np.nan)

    ma5 = roll_mean(cs, 5)
    ma5_prev = np.concatenate(([np.nan], ma5[:-1]))
    c_prev = np.concatenate(([np.nan], cs[:-1]))

    above = cs > ma5
    with np.errstate(invalid="ignore", divide="ignore"):
        dev5 = (cs - ma5) / ma5 * 100.0
        chg = cs / c_prev - 1.0
    reclaim = (c_prev < ma5_prev) & (cs > ma5)
    cand20 = hs > roll_max_prev(hs, 20)
    limitup = chg > 0.095

    fwd = np.full(n, np.nan)
    fwd[:n - FWD] = cs[FWD:] / cs[:n - FWD] - 1.0

    lo, hi = N150, n - FWD
    sel = np.arange(lo, hi)
    sel = sel[ok[sel] & np.isfinite(fwd[sel]) & np.isfinite(dev5[sel])]
    if len(sel) == 0:
        continue
    acc["date"].append(d[sel])
    acc["above"].append(above[sel])
    acc["dev5"].append(dev5[sel])
    acc["reclaim"].append(reclaim[sel])
    acc["cand20"].append(cand20[sel])
    acc["fwd5"].append(fwd[sel])
    acc["limitup"].append(limitup[sel])

DATE = np.concatenate(acc["date"])
ABOVE = np.concatenate(acc["above"])
DEV5 = np.concatenate(acc["dev5"])
RECLAIM = np.concatenate(acc["reclaim"])
CAND = np.concatenate(acc["cand20"])
FWD5 = np.concatenate(acc["fwd5"])
LIMIT = np.concatenate(acc["limitup"])

print(f"总观测(stock-day): {len(FWD5):,}   日期 {min(DATE)} → {max(DATE)}")
print(f"猎手候选层(>20日新高): {CAND.sum():,} ({CAND.mean()*100:.1f}%)")
print(f"其中在 MA5 上方: {(CAND & ABOVE).sum():,} ({(CAND & ABOVE).sum()/max(CAND.sum(),1)*100:.1f}%)")
print(f"其中在 MA5 下方: {(CAND & ~ABOVE).sum():,} ({(CAND & ~ABOVE).sum()/max(CAND.sum(),1)*100:.1f}%)\n")


def stat(mask, label):
    x = FWD5[mask]
    n = len(x)
    if n < 30:
        return f"  {label:<24} n={n:<8} （样本太少，不下结论）"
    m, sd = float(np.mean(x)), float(np.std(x, ddof=1))
    t = m / (sd / np.sqrt(n)) if sd > 0 else 0.0
    return (f"  {label:<24} n={n:<8} 均值={m*100:+6.2f}%  中位={float(np.median(x))*100:+6.2f}%  "
            f"胜率={float(np.mean(x>0))*100:5.1f}%  P5={float(np.percentile(x,5))*100:+7.2f}%  t={t:6.2f}")


def section(title, base):
    nb = int(base.sum())
    print(f"=== {title} ===")
    if nb < 100:
        print(f"  基准样本仅 {nb}，跳过\n")
        return
    print(f"  基准样本 {nb:,}")
    print("  [A 布尔「站上5日线」]")
    print(stat(base & ABOVE, "站上 MA5"))
    print(stat(base & ~ABOVE, "未站上 MA5"))
    print("  [C 状态跃迁「刚站上」overnight_reclaim]")
    print(stat(base & RECLAIM, "刚站上"))
    print(stat(base & ~RECLAIM, "非刚站上"))
    print("  [B dev5_pct 分档]")
    for lo, hi, lab in ((-999, -5, "-inf~-5"), (-5, -2, "-5~-2"), (-2, 0, "-2~0"),
                        (0, 2, "0~+2"), (2, 5, "+2~+5"), (5, 999, "+5~+inf")):
        print(stat(base & (DEV5 >= lo) & (DEV5 < hi), f"dev5 {lab}"))
    print()


section("全池（所有个股 stock-day）", np.ones(len(FWD5), dtype=bool))
section("猎手候选层（今日最高 > 近20日最高）", CAND)
section("猎手候选层 · 排除涨停日（涨停买不到）", CAND & ~LIMIT)

d_u = np.unique(DATE)
mid = d_u[len(d_u) // 2]
print(f"=== 候选层 h1/h2 稳定性（切分 {mid}）===")
for tag, hm in (("h1", DATE <= mid), ("h2", DATE > mid)):
    bm = CAND & hm
    print(f"  --- {tag}（基准 {int(bm.sum()):,}）---")
    print(stat(bm & ABOVE, "站上 MA5"))
    print(stat(bm & ~ABOVE, "未站上 MA5"))
    print(stat(bm & RECLAIM, "刚站上"))

print("\n=== 候选层 · 非重叠子样本（每 5 个交易日取 1 日）===")
ux = np.unique(DATE)
keepd = np.isin(DATE, ux[::5])
print(stat(CAND & keepd & ABOVE, "站上 MA5"))
print(stat(CAND & keepd & ~ABOVE, "未站上 MA5"))
print(stat(CAND & keepd & RECLAIM, "刚站上"))


def diff_t(m1, m2, label):
    """两组均值差 + 双样本 t（Welch）。"""
    a, b = FWD5[m1], FWD5[m2]
    if len(a) < 30 or len(b) < 30:
        print(f"  {label:<34} n={len(a)}/{len(b)} （样本太少）")
        return
    va, vb = np.var(a, ddof=1) / len(a), np.var(b, ddof=1) / len(b)
    se = np.sqrt(va + vb)
    d = float(np.mean(a) - np.mean(b))
    t = d / se if se > 0 else 0.0
    print(f"  {label:<34} Δ={d*100:+6.2f}pp  t={t:6.2f}   (n={len(a):,} / {len(b):,})")


print("\n=== 候选层 · 组间差值显著性（Welch t）===")
diff_t(CAND & ~ABOVE, CAND & ABOVE, "未站上 − 站上")
diff_t(CAND & RECLAIM, CAND & ~RECLAIM, "刚站上 − 非刚站上")
diff_t(CAND & (DEV5 > 5), CAND & (DEV5 <= 5), "dev5>+5% − 其余")
diff_t(CAND & (DEV5 >= -5) & (DEV5 < -2), CAND & (DEV5 >= 0), "dev5[-5,-2) − dev5>=0")
diff_t(CAND & (DEV5 > 5) & ~LIMIT, CAND & (DEV5 <= 5) & ~LIMIT, "dev5>+5% − 其余 (剔涨停)")

print("\n=== 候选层 · dev5 分档 × h1/h2（验稳定性）===")
for lo, hi, lab in ((-5, -2, "-5~-2"), (-2, 0, "-2~0"), (0, 2, "0~+2"),
                    (2, 5, "+2~+5"), (5, 999, "+5~+inf")):
    bm = CAND & (DEV5 >= lo) & (DEV5 < hi)
    print(f"  --- dev5 {lab} ---")
    print(stat(bm & (DATE <= mid), "  h1"))
    print(stat(bm & (DATE > mid), "  h2"))
