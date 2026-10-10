"""实验：大板块（n>=70）该用什么度量？（2026-10-10）

前一轮（tmp/exp_sector_strength.py）结论：涨停率对超额收益的预测力**全在小板块**
（n<30: IC=+0.0371, t=1.66），大板块是精确的零（n>=70: IC=-0.0004, t=-0.02），
且大板块的涨停率测得**更准**（噪声更小）⇒ 那个零是真没信号，不是没得排。

故本轮找「对大板块有效」的度量。候选（按假设）：
  · 相对广度：up_ratio − 当日全市场 up_ratio。假设大板块被宏观 beta 主导，
    绝对广度只是大盘的影子，减掉市场才有信息。
  · 量能放大：当日成交额 / 前5日均额。资金流而非价格。
  · 龙头集中度：Top3 成员相对其余的超额（大板块可能只见龙头）。
  · 细分层：把大类目拆到 jiuyan_concept 子概念（尺寸天然更小），取最强子概念。

目标：未来 FWD 日**超额**收益（减当日横截面板块均值）。
输出：每个度量在「小板块 / 大板块」两桶里的 IC —— 看哪个能救大板块。

用法：PYTHONIOENCODING=utf-8 python tmp/exp_sector_large_measure.py [FWD]
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(BASE)
DAILY = BASE / "t_io" / "cache" / "daily_kline"
FWD = int(sys.argv[1]) if len(sys.argv) > 1 else 1
CAL_DAYS = 500
MIN_MEMBERS = 5
LIMIT_TH = 0.095
SMALL_N, LARGE_N = 30, 70


def _f(x):
    try:
        return float(x)
    except Exception:
        return np.nan


def spearman(a, b):
    if len(a) < 4:
        return np.nan
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else np.nan


# ---------- 载入 ----------
wl = json.loads((BASE / "stock_hunter" / "watchlist_jiuyan.json").read_text(encoding="utf-8"))
cat, sub = {}, {}
for c, v in wl.items():
    if not isinstance(v, dict):
        continue
    k = (v.get("jiuyan_category") or "").strip()
    if not k or "|" in k:
        continue
    cat.setdefault(k, []).append(c)
    sc = (v.get("jiuyan_concept") or "").strip()
    if sc:
        sub[c] = sc

codes = sorted({c for v in cat.values() for c in v})
raw, dcnt = {}, {}
for c in codes:
    fp = DAILY / f"{c}.json"
    if not fp.exists():
        continue
    try:
        rows = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
    except Exception:
        continue
    d = np.array([str(r.get("date"))[:10] for r in rows])
    cl = np.array([_f(r.get("close")) for r in rows])
    am = np.array([_f(r.get("close")) * _f(r.get("volume")) for r in rows])
    ok = np.isfinite(cl) & (cl > 0)
    if ok.sum() < 60:
        continue
    o = np.argsort(d)
    raw[c] = (d[o], cl[o], am[o], ok[o])
    for x in set(d.tolist()):
        dcnt[x] = dcnt.get(x, 0) + 1

CAL = sorted([d for d, n in dcnt.items() if n >= max(len(raw) * 0.6, 1)])[-CAL_DAYS:]
T = len(CAL); CALARR = np.array(CAL)
print(f"日历 {CAL[0]} → {CAL[-1]}（{T} 天）  个股 {len(raw)}")

PX, AM = {}, {}
for c, (d, cl, am, ok) in raw.items():
    pos = np.searchsorted(CALARR, d)
    a = np.full(T, np.nan); b = np.full(T, np.nan)
    sel = (pos < T) & ok
    a[pos[sel]] = cl[sel]; b[pos[sel]] = am[sel]
    PX[c] = a; AM[c] = b

ALL = np.vstack([PX[c] for c in PX])
with np.errstate(invalid="ignore", divide="ignore"):
    ALLR = ALL[:, 1:] / ALL[:, :-1] - 1.0
MKT_UP = np.array([np.nanmean(np.where(np.isfinite(ALLR[:, i]), ALLR[:, i] > 0, np.nan))
                   for i in range(ALLR.shape[1])])
MKT_RET = np.array([np.nanmean(ALLR[:, i]) for i in range(ALLR.shape[1])])

# ---------- 逐 (板块, 日) ----------
cats = [k for k, v in cat.items() if sum(1 for c in v if c in PX) >= MIN_MEMBERS]
obs = {}
for k in cats:
    mem = [c for c in cat[k] if c in PX]
    P = np.vstack([PX[c] for c in mem])
    A = np.vstack([AM[c] for c in mem])
    with np.errstate(invalid="ignore", divide="ignore"):
        R = P[:, 1:] / P[:, :-1] - 1.0
        AMT = A[:, 1:]                      # 成交额对齐到 t
    for i in range(R.shape[1]):
        t = i + 1
        r = R[:, i]; v = np.isfinite(r)
        n = int(v.sum())
        if n < MIN_MEMBERS:
            continue
        rr = r[v]
        amt = AMT[v, i]
        # 量能放大：今日 / 前5日均（该板块成分股的成交额合计）
        prev = []
        for j in range(max(0, i - 5), i):
            x = AMT[v, j]
            x = x[np.isfinite(x)]
            if x.size:
                prev.append(x.sum())
        amp = (np.nansum(amt) / np.mean(prev)) if prev and np.mean(prev) > 0 else 1.0
        # 龙头集中度：Top3 收益均值 − 其余均值
        srt = np.sort(rr)[::-1]
        lead = float(srt[:3].mean()) - float(srt[3:].mean()) if n > 4 else 0.0
        # 细分层：把成分股按 jiuyan_concept 分组
        grp = {}
        sel_codes = [c for c, vv in zip(mem, v) if vv]
        for c, rv in zip(sel_codes, rr):
            sc = sub.get(c)
            if sc:
                grp.setdefault(sc, []).append(rv)
        sub_best, sub_lim = -9.0, 0
        for sc, vals in grp.items():
            if len(vals) < 3:
                continue
            va = np.array(vals)
            sub_best = max(sub_best, float((va > 0).mean()))
            if (va > LIMIT_TH).any():
                sub_lim += 1
        obs[(t, k)] = {
            "n": n, "rate": float((rr > LIMIT_TH).mean()),
            "up": float((rr > 0).mean()), "strong": float((rr > 0.03).mean()),
            "mean": float(rr.mean()), "med": float(np.median(rr)),
            "amp": float(min(amp, 5.0)), "lead": lead,
            "up_rel": float((rr > 0).mean()) - float(MKT_UP[i]) if np.isfinite(MKT_UP[i]) else 0.0,
            "strong_rel": float((rr > 0.03).mean()) - float(np.nanmean(np.where(np.isfinite(ALLR[:, i]), ALLR[:, i] > 0.03, np.nan))),
            "med_rel": float(np.median(rr)) - (float(MKT_RET[i]) if np.isfinite(MKT_RET[i]) else 0.0),
            "sub_best": sub_best, "sub_lim": float(sub_lim),
        }
print(f"(板块,日) 观测 {len(obs):,}\n")

FWD_RET = {}
for (t, k), o in obs.items():
    cur, ok = 1.0, True
    for j in range(1, FWD + 1):
        nx = obs.get((t + j, k))
        if nx is None:
            ok = False; break
        cur *= (1.0 + nx["mean"])
    if ok:
        FWD_RET[(t, k)] = cur - 1.0

MEAS = ["rate", "up", "strong", "mean", "med", "amp", "lead", "up_rel", "strong_rel",
        "med_rel", "sub_best", "sub_lim"]
CN = {"rate": "涨停率", "up": "上涨广度", "strong": "强涨广度(>3%)", "mean": "等权平均涨",
      "med": "中位涨幅", "amp": "量能放大倍数", "lead": "龙头集中度(Top3-其余)",
      "up_rel": "相对上涨广度(减大盘)", "strong_rel": "相对强涨广度(减大盘)",
      "med_rel": "相对中位涨幅(减大盘)", "sub_best": "细分层·最强子概念广度",
      "sub_lim": "细分层·有涨停子概念数"}

by_day = {}
for (t, k), o in obs.items():
    if (t, k) in FWD_RET:
        by_day.setdefault(t, []).append((k, o))

ICS = {(b, m): [] for b in ("small", "large", "all") for m in MEAS}
for t, lst in sorted(by_day.items()):
    if len(lst) < 8:
        continue
    y_abs = np.array([FWD_RET[(t, k)] for k, _ in lst], float)
    y = y_abs - y_abs.mean()
    N = np.array([o["n"] for _, o in lst], float)
    for b, bm in (("small", N < SMALL_N), ("large", N >= LARGE_N),
                  ("all", np.ones(len(lst), bool))):
        if bm.sum() < 5:
            continue
        for m in MEAS:
            v = np.array([o[m] for _, o in lst], float)[bm]
            if np.std(v) < 1e-12:
                continue
            ic = spearman(v, y[bm])
            if not np.isnan(ic):
                ICS[(b, m)].append(ic)


def show(bucket, title):
    print(f"=== {title}（未来 {FWD} 日超额收益）===")
    print(f"{'度量':<26}{'IC':>9}{'t':>7}{'日数':>7}")
    print("-" * 50)
    out = []
    for m in MEAS:
        a = np.array(ICS[(bucket, m)])
        if len(a) < 20:
            continue
        t_ic = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else 0
        out.append((m, a.mean(), t_ic, len(a)))
    for m, ic, t_ic, nd in sorted(out, key=lambda r: -abs(r[1])):
        print(f"{CN[m]:<26}{ic:+9.4f}{t_ic:7.2f}{nd:7d}")
    print()


show("large", f"大板块 n>={LARGE_N}")
show("small", f"小板块 n<{SMALL_N}")
show("all", "全板块")
