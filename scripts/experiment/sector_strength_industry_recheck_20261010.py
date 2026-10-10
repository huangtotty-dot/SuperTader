"""复核：把板块口径从「九眼概念」换成「行业」，看 §4「信号只在小板块」是否还成立。

为什么要复核：`jiuyan_category` 标签的 `updated_at` 从 2026-05-30 铺到 **2026-10-10**，
其中 269 个是 2026-10-10 才加的，而回测窗口从 2024-09 起 ⇒ 头两年大部分标签**根本不存在**。
更糟：**后加的标签恰恰是小类目**（有机硅 12、HVDC 15、超节点 24），而"信号只在小板块"
正是待检验的结论 ⇒ **该偏差有可能凭空造出这个结论**（新标签因某板块最近热才被建）。

行业口径（`sector` 首段）的三点优势：
  1. 归属**本质稳定**（一家化工公司 2024 年也是化工），不像概念标签的存在性绑定近期题材；
  2. 每天横截面 **125 个组**（vs 概念 22 个）⇒ 逐日 IC 噪声小得多；
  3. 尺寸跨度 5~296。

用法：PYTHONIOENCODING=utf-8 python tmp/exp_sector_industry_recheck.py [FWD]
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
DROP = {"其他", ""}


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


# ---------- 载入 + 建两种成分口径 ----------
wl = json.loads((BASE / "stock_hunter" / "watchlist_jiuyan.json").read_text(encoding="utf-8"))
IND, CAT, SUB = {}, {}, {}
for c, v in wl.items():
    if not isinstance(v, dict):
        continue
    seg = (v.get("sector") or "").split("/")
    if seg and seg[0].strip() and seg[0].strip() not in DROP:
        IND.setdefault(seg[0].strip(), []).append(c)
    k = (v.get("jiuyan_category") or "").strip()
    if k and "|" not in k:
        CAT.setdefault(k, []).append(c)
    sc = (v.get("jiuyan_concept") or "").strip()
    if sc:
        SUB[c] = sc

codes = sorted({c for v in list(IND.values()) + list(CAT.values()) for c in v})
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

MEAS = ["rate", "lead", "up", "strong", "mean", "med", "amp", "sub_lim"]
CN = {"rate": "涨停率", "lead": "龙头集中度", "up": "上涨广度", "strong": "强涨广度",
      "mean": "等权平均涨", "med": "中位涨幅", "amp": "量能放大", "sub_lim": "有涨停子概念数"}


def build(groups, label):
    cats = [k for k, v in groups.items() if sum(1 for c in v if c in PX) >= MIN_MEMBERS]
    obs = {}
    for k in cats:
        mem = [c for c in groups[k] if c in PX]
        P = np.vstack([PX[c] for c in mem])
        A = np.vstack([AM[c] for c in mem])
        with np.errstate(invalid="ignore", divide="ignore"):
            R = P[:, 1:] / P[:, :-1] - 1.0
            AMT = A[:, 1:]
        for i in range(R.shape[1]):
            r = R[:, i]; v = np.isfinite(r)
            n = int(v.sum())
            if n < MIN_MEMBERS:
                continue
            rr = r[v]
            srt = np.sort(rr)[::-1]
            lead = float(srt[:3].mean() - srt[3:].mean()) if n > 4 else 0.0
            prev = [AMT[v, j].sum() for j in range(max(0, i - 5), i)
                    if np.isfinite(AMT[v, j]).any()]
            amp = (np.nansum(AMT[v, i]) / np.mean(prev)) if prev and np.mean(prev) > 0 else 1.0
            grp = {}
            for c, rv in zip([c for c, vv in zip(mem, v) if vv], rr):
                sc = SUB.get(c)
                if sc:
                    grp.setdefault(sc, []).append(rv)
            sub_lim = sum(1 for vals in grp.values()
                          if len(vals) >= 3 and (np.array(vals) > LIMIT_TH).any())
            obs[(i + 1, k)] = {"n": n, "rate": float((rr > LIMIT_TH).mean()), "lead": lead,
                               "up": float((rr > 0).mean()), "strong": float((rr > 0.03).mean()),
                               "mean": float(rr.mean()), "med": float(np.median(rr)),
                               "amp": float(min(amp, 5.0)), "sub_lim": float(sub_lim)}
    fwd = {}
    for (t, k), o in obs.items():
        cur, okk = 1.0, True
        for j in range(1, FWD + 1):
            nx = obs.get((t + j, k))
            if nx is None:
                okk = False; break
            cur *= (1.0 + nx["mean"])
        if okk:
            fwd[(t, k)] = cur - 1.0
    by_day = {}
    for (t, k), o in obs.items():
        if (t, k) in fwd:
            by_day.setdefault(t, []).append((k, o, fwd[(t, k)]))
    ics = {(b, m): [] for b in ("small", "large", "all") for m in MEAS}
    sizes = []
    for t, lst in sorted(by_day.items()):
        if len(lst) < 8:
            continue
        y = np.array([x[2] for x in lst], float)
        y = y - y.mean()
        N = np.array([o["n"] for _, o, _ in lst], float)
        sizes.append(len(lst))
        for b, bm in (("small", N < 30), ("large", N >= 70), ("all", np.ones(len(lst), bool))):
            if bm.sum() < 5:
                continue
            for m in MEAS:
                v = np.array([o[m] for _, o, _ in lst], float)[bm]
                if np.std(v) < 1e-12:
                    continue
                ic = spearman(v, y[bm])
                if not np.isnan(ic):
                    ics[(b, m)].append(ic)
    print(f"\n{'='*66}\n### {label}：{len(cats)} 个组，日均横截面 {np.mean(sizes):.1f} 组\n{'='*66}")
    for b, bn in (("all", "全组"), ("small", "小组 n<30"), ("large", "大组 n>=70")):
        print(f"  --- {bn} ---")
        out = []
        for m in MEAS:
            a = np.array(ics[(b, m)])
            if len(a) < 20:
                continue
            t_ic = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else 0
            out.append((m, a.mean(), t_ic, len(a)))
        for m, ic, t_ic, nd in sorted(out, key=lambda r: -abs(r[1])):
            print(f"    {CN[m]:<16}{ic:+8.4f}  t={t_ic:6.2f}  日数={nd}")


build(IND, f"行业口径（独立复核，FWD={FWD}）")
build(CAT, f"九眼概念口径（原口径，FWD={FWD}）")
