"""实验：板块强弱度算法 × 成分股数量差异（2026-10-10）。

问题（owner）：有些板块票多、有些票少（实测 150x：半导体 150 vs 有机硅 12），
如何让算法**真正**体现板块强弱，而不是被尺寸带偏？

现有实现的两套口径（`stock_hunter/modules/heat_tracker.py` + `config.ranking`）：
  · heat_score = 比率口径，但涨停密度 `min(k/n*150, 30)` **20% 密度即封顶**
    ⇒ 5 票板块 1 只涨停就满分，200 票板块要 40 只才等价；
  · `ranking.sector_sort_keys` 第一键 = **原始涨停家数** ⇒ 大板块天然占优。

本实验对同一批板块、同一批日期，比较 8 种打分法对**次日前向收益**的预测力。
板块→成分股：`watchlist_jiuyan.json` 的 `jiuyan_category`（单名类目 22 个，尺寸 12~150）。

⚠️ 必读局限：成分股用**当前** watchlist 回溯历史 ⇒ 有前视/幸存偏差。它对所有打分法
   **同等作用**，故"哪种口径更准"的比较有效；"绝对收益水平"不可外推。
⚠️ 涨停阈值统一取 9.5%（未区分 20%/30% 板块）⇒ 对创业板/科创板成分为主的板块会低估 k。
   但同样对所有方法同等作用。
用法：PYTHONIOENCODING=utf-8 python tmp/exp_sector_strength.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
from pathlib import Path

import numpy as np

BASE = Path(BASE)
DAILY = BASE / "t_io" / "cache" / "daily_kline"
CAL_DAYS = 500
MIN_MEMBERS = 5
LIMIT_TH = 0.095
FWD = int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 1


def _f(x):
    try:
        return float(x)
    except Exception:
        return np.nan


def spearman(a, b):
    """Spearman = 秩的 Pearson（不依赖 scipy）。"""
    if len(a) < 4:
        return np.nan
    ra = np.argsort(np.argsort(a)).astype(float)
    rb = np.argsort(np.argsort(b)).astype(float)
    ra -= ra.mean(); rb -= rb.mean()
    den = np.sqrt((ra ** 2).sum() * (rb ** 2).sum())
    return float((ra * rb).sum() / den) if den > 0 else np.nan


# ---------- 1) 板块 → 成分股 ----------
wl = json.loads((BASE / "stock_hunter" / "watchlist_jiuyan.json").read_text(encoding="utf-8"))
cat = {}
for c, v in wl.items():
    if not isinstance(v, dict):
        continue
    k = (v.get("jiuyan_category") or "").strip()
    if not k or "|" in k:
        continue
    cat.setdefault(k, []).append(c)
sizes = sorted((len(v) for v in cat.values()))
print(f"单名类目 {len(cat)} 个，尺寸 {sizes[0]}~{sizes[-1]}（中位 {sizes[len(sizes)//2]}）")

# ---------- 2) 全局日历 + 逐股对齐 ----------
codes = sorted({c for v in cat.values() for c in v})
raw = {}
dcnt = {}
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
    ok = np.isfinite(cl) & (cl > 0)
    if ok.sum() < 60:
        continue
    o = np.argsort(d)
    raw[c] = (d[o], cl[o], ok[o])
    for x in set(d.tolist()):
        dcnt[x] = dcnt.get(x, 0) + 1

CAL = sorted([d for d, n in dcnt.items() if n >= max(len(raw) * 0.6, 1)])[-CAL_DAYS:]
T = len(CAL)
CALARR = np.array(CAL)
print(f"交易日历 {len(CAL)} 天：{CAL[0]} → {CAL[-1]}   个股 {len(raw)}")

px = {}
for c, (d, cl, ok) in raw.items():
    pos = np.searchsorted(CALARR, d)
    a = np.full(T, np.nan)
    sel = (pos < T) & ok
    a[pos[sel]] = cl[sel]
    px[c] = a

# ---------- 3) 逐 (板块, 日) ----------
cats = [k for k, v in cat.items() if sum(1 for c in v if c in px) >= MIN_MEMBERS]
print(f"参与实验的类目 {len(cats)} 个\n")

# ret[t] = CAL[t] 的日收益（用 t-1→t）
obs = {}          # (t, cat) -> dict
for k in cats:
    mem = [c for c in cat[k] if c in px]
    P = np.vstack([px[c] for c in mem])
    with np.errstate(invalid="ignore", divide="ignore"):
        R = P[:, 1:] / P[:, :-1] - 1.0            # R[:,i] 对应 CAL[i+1]
    for i in range(R.shape[1]):
        r = R[:, i]
        v = np.isfinite(r)
        n = int(v.sum())
        if n < MIN_MEMBERS:
            continue
        rr = r[v]
        obs[(i + 1, k)] = {"n": n, "k": int((rr > LIMIT_TH).sum()),
                           "mean": float(rr.mean()), "med": float(np.median(rr)),
                           "var": float(rr.var(ddof=1)) if n > 1 else 0.0}
print(f"(板块,日) 观测 {len(obs):,}")

# 前向 FWD 日累计板块收益
FWD_RET = {}
for (t, k), o in obs.items():
    cur = 1.0
    ok = True
    for j in range(1, FWD + 1):
        nx = obs.get((t + j, k))
        if nx is None:
            ok = False
            break
        cur *= (1.0 + nx["mean"])
    if ok:
        FWD_RET[(t, k)] = cur - 1.0

by_day = {}
for (t, k), o in obs.items():
    if (t, k) in FWD_RET:
        by_day.setdefault(t, []).append((k, o))

# ---------- 4) 打分法 ----------
METHODS = ["1_原始涨停家数", "2_涨停率k/n", "3_收缩涨停率", "4_显著z", "5_等权平均涨",
           "6_中位涨幅", "7_精度加权收缩涨", "8_涨停率×上涨广度"]
ics = {m: [] for m in METHODS}
tp5 = {m: [] for m in METHODS}
sz5 = {m: [] for m in METHODS}
picks = {m: [] for m in METHODS}
BUCKET = {}
BUCKET_EXTRA = {}

# ---- 先**池化**估计 Beta-Binomial 先验（不能逐日估完再平均 α/β：τ² 偶被夹到 1e-9 的
#      退化日会让该日 α 变成天文数字，平均值被它主导 ⇒ 先验压倒一切、所有板块压成同一个数）----
_days = [l for l in by_day.values() if len(l) >= 8]
_mu_pool = sum(o["k"] for l in _days for _, o in l) / sum(o["n"] for l in _days for _, o in l)
_tau_days = []
for l in _days:
    K_ = np.array([o["k"] for _, o in l], float)
    N_ = np.array([o["n"] for _, o in l], float)
    _tau_days.append(float(np.mean((K_ / N_ - _mu_pool) ** 2 - _mu_pool * (1 - _mu_pool) / N_)))
TAU2 = max(float(np.mean(_tau_days)), 1e-9)
_kappa = max(_mu_pool * (1 - _mu_pool) / TAU2 - 1.0, 1e-6)
ALPHA, BETA = _mu_pool * _kappa, (1 - _mu_pool) * _kappa
MU0 = _mu_pool
print(f"[池化先验] mu={MU0:.4f}  tau2(日均)={TAU2:.6f}  kappa={_kappa:.1f}"
      f"  alpha={ALPHA:.2f} beta={BETA:.2f}")

for t, lst in sorted(by_day.items()):
    if len(lst) < 8:
        continue
    K = np.array([o["k"] for _, o in lst], float)
    N = np.array([o["n"] for _, o in lst], float)
    M = np.array([o["mean"] for _, o in lst], float)
    MD = np.array([o["med"] for _, o in lst], float)
    V = np.array([o["var"] for _, o in lst], float)
    mu = MU0
    al, be = ALPHA, BETA
    # 收益侧收缩：σ²_w 组内方差、τ²_b 组间真实方差
    sw = float(np.mean(V))
    mbar = float(M.mean())
    tb = max(float(M.var(ddof=1)) - sw / float(np.mean(N)), 1e-12)
    rate = K / N
    se = np.sqrt(np.maximum(mu * (1 - mu) / N, 1e-12))
    sc = {
        "1_原始涨停家数": K,
        "2_涨停率k/n": rate,
        "3_收缩涨停率": (K + al) / (N + al + be),
        "4_显著z": (rate - mu) / se,
        "5_等权平均涨": M,
        "6_中位涨幅": MD,
        "7_精度加权收缩涨": mbar + (M - mbar) * (N / (N + sw / tb)),
        "8_涨停率×上涨广度": rate * (M > 0).astype(float),
    }
    # ⚠️ 排序能力要用**超额**（减当日板块横截面均值）来评：绝对收益被大盘涨跌主导，
    # 而大盘是任何横截面打分法都预测不了的，会把所有方法的差异冲平。
    y_abs = np.array([FWD_RET[(t, k)] for k, _ in lst], float)
    y = y_abs - y_abs.mean()
    names = [k for k, _ in lst]
    # 逐尺寸桶的 IC（验「小板块是不是更吵」）
    for lab, mm in (("small", N < 30), ("large", N >= 70)):
        if mm.sum() >= 5 and np.std(rate[mm]) > 1e-12:
            ic_b = spearman(rate[mm], y[mm])
            if not np.isnan(ic_b):
                BUCKET.setdefault(lab, []).append(ic_b)
                BUCKET_EXTRA.setdefault(lab, []).append(
                    (int(mm.sum()), float(np.std(rate[mm])), float(np.ptp(rate[mm])),
                     int(mm.sum() and np.median(N[mm]))))
    for m in METHODS:
        v = sc[m]
        if np.std(v) < 1e-12:
            continue
        ic = spearman(v, y)
        if not np.isnan(ic):
            ics[m].append(ic)
        order = np.argsort(-v)[:5]
        tp5[m].append(float(y[order].mean()))
        sz5[m].append(float(N[order].mean()))
        picks[m].append(set(np.array(names)[order]))

print(f"=== 各打分法：对**未来 {FWD} 日板块超额收益**（减当日横截面均值）的预测力（每日横截面算 IC，再跨日平均）===")
print(f"{'方法':<20}{'IC均值':>9}{'IC_t':>7}{'IC>0':>7}{'Top5收益':>11}{'Top5均成分数':>13}")
print("-" * 70)
rowsout = []
for m in METHODS:
    a = np.array(ics[m])
    if len(a) < 10:
        continue
    t_ic = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else 0
    rowsout.append((m, a.mean(), t_ic, float((a > 0).mean()),
                    float(np.mean(tp5[m])), float(np.mean(sz5[m]))))
for m, ic, t_ic, pos, tp, sz in sorted(rowsout, key=lambda r: -r[1]):
    print(f"{m:<20}{ic:9.4f}{t_ic:7.2f}{pos*100:6.0f}%{tp*100:+10.3f}%{sz:13.1f}")

print("\n=== 收缩参数的直观含义（池化估计，非逐日平均）===")
print(f"  n=5   板块 · 1 只涨停 → 原始 20.0%  →  收缩后 {(1+ALPHA)/(5+ALPHA+BETA)*100:.1f}%")
print(f"  n=150 板块 · 30 只涨停 → 原始 20.0%  →  收缩后 {(30+ALPHA)/(150+ALPHA+BETA)*100:.1f}%")
print(f"  ⇒ 先验等效样本量 kappa={_kappa:.0f}：小板块被拉向先验、大板块基本保留自身率")

print("\n=== Top5 名单重合度：尺寸归一后换了多少人 ===")
for m in METHODS[1:]:
    if not picks[m]:
        continue
    same = np.mean([len(a & b) / 5 for a, b in zip(picks["1_原始涨停家数"], picks[m])])
    print(f"  「1_原始涨停家数」 vs 「{m}」: 重合 {same*100:3.0f}%")

print("\n=== 尺寸偏置的直接证据：Top5 的平均成分股数 ===")
for m in METHODS:
    if sz5[m]:
        print(f"  {m:<20} {np.mean(sz5[m]):6.1f} 只")

print("\n=== 尺寸分桶 IC：涨停率对超额收益的预测力（验「小板块更吵」）===")
for lab, name in (("small", "小板块 n<30"), ("large", "大板块 n>=70")):
    a = np.array(BUCKET.get(lab, []))
    if len(a) < 10:
        print(f"  {name:<14} 样本不足"); continue
    t_ic = a.mean() / (a.std(ddof=1) / np.sqrt(len(a))) if a.std(ddof=1) > 0 else 0
    print(f"  {name:<14} IC均值={a.mean():+.4f}  t={t_ic:5.2f}  日数={len(a)}")

print("\n=== 分桶离散度（排除「桶内没差可排 ⇒ 假零」的解释）===")
for lab, name in (("small", "小板块 n<30"), ("large", "大板块 n>=70")):
    e = np.array(BUCKET_EXTRA.get(lab, []), float)
    if len(e) < 10:
        print(f"  {name:<14} 样本不足"); continue
    print(f"  {name:<14} 日均板块数={e[:,0].mean():.1f}  涨停率std均值={e[:,1].mean():.4f}"
          f"  极差均值={e[:,2].mean():.4f}  成分数中位={e[:,3].mean():.0f}")
