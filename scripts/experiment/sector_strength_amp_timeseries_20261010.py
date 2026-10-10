"""实验：`量能放大` 有没有**时序**信息（它是否被放错了位置）？（2026-10-10）

背景：`heat_score` 里 `量能放大 = clamp((当日成交额/前5日均 − 1)×10, 0, 20)` 占 20 分。
前几轮横截面 IC 测下来它 ≈ 0（概念 0.0073/0.0075；行业 −0.0018/−0.0035）。
但 heat_score 是**横截面排名**用的，水平项对排名的贡献本就是横截面的
⇒ 对"它在 heat_score 里的角色"而言，横截面 IC 已是正确测量。
待答的是：**它是不是被放错了位置**——若它是好用的**时序**信号（板块自身放量 = 启动），
那它就不该拿 20 分进一个横截面排名，而应另做 regime 标记/告警。

**区分时序 vs 横截面**：用**组内（固定效应）**分解——
  · 横截面成分：同一天，A 板块比 B 板块放量更多 ⇒ 次日 A 跑赢 B？（= 已知的 IC，≈0）
  · 时序成分  ：同一板块，它自己放量的日子 vs 它自己不放量的日子，后续收益差多少？
本脚本测**时序成分**：把 amp 与 fwd 收益**都按板块（再按日）去均值**，再求相关。

另加事件研究：amp 从 ≤T 上穿到 >T 的那一天后，N 日收益 vs 非事件日。

用法：PYTHONIOENCODING=utf-8 python tmp/exp_amp_timeseries.py [FWD]
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
FWD = int(sys.argv[1]) if len(sys.argv) > 1 else 5
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


# ---------- 载入 ----------
wl = json.loads((BASE / "stock_hunter" / "watchlist_jiuyan.json").read_text(encoding="utf-8"))
IND, CAT = {}, {}
for c, v in wl.items():
    if not isinstance(v, dict):
        continue
    seg = (v.get("sector") or "").split("/")
    if seg and seg[0].strip() and seg[0].strip() not in DROP:
        IND.setdefault(seg[0].strip(), []).append(c)
    k = (v.get("jiuyan_category") or "").strip()
    if k and "|" not in k:
        CAT.setdefault(k, []).append(c)

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

# 固定绝对阈值取日历（避免阈值随票数放大导致窗口被压到 201 天）
CAL = sorted([d for d, n in dcnt.items() if n >= 2000])[-600:]
if len(CAL) < 100:
    CAL = sorted([d for d, n in dcnt.items() if n >= max(len(raw) * 0.5, 1)])[-600:]
T = len(CAL); CALARR = np.array(CAL)
print(f"日历 {CAL[0]} → {CAL[-1]}（{T} 天）  个股 {len(raw)}")

PX, AM = {}, {}
for c, (d, cl, am, ok) in raw.items():
    pos = np.searchsorted(CALARR, d)
    a = np.full(T, np.nan); b = np.full(T, np.nan)
    sel = (pos < T) & ok
    a[pos[sel]] = cl[sel]; b[pos[sel]] = am[sel]
    PX[c] = a; AM[c] = b


def build(groups, label):
    cats = [k for k, v in groups.items() if sum(1 for c in v if c in PX) >= MIN_MEMBERS]
    obs = {}
    for k in cats:
        mem = [c for c in groups[k] if c in PX]
        P = np.vstack([PX[c] for c in mem])
        A = np.vstack([AM[c] for c in mem])
        with np.errstate(invalid="ignore", divide="ignore"):
            R = P[:, 1:] / P[:, :-1] - 1.0
        for i in range(R.shape[1]):
            r = R[:, i]; v = np.isfinite(r)
            n = int(v.sum())
            if n < MIN_MEMBERS:
                continue
            rr = r[v]
            amt_today = np.nansum(A[v, i + 1])
            prev = [np.nansum(A[v, j]) for j in range(max(0, i - 5), i)]
            prev = [p for p in prev if np.isfinite(p) and p > 0]
            if not prev or not np.isfinite(amt_today) or amt_today <= 0:
                continue
            obs[(i + 1, k)] = {"n": n, "mean": float(rr.mean()),
                               "amp": float(min(amt_today / np.mean(prev), 5.0)),
                               "rate": float((rr > LIMIT_TH).mean()),
                               # 控制变量：板块当日收益（放量的日子往往也是大涨的日子，
                               # 而 A 股有短期反转 ⇒ 必须把「放量」与「涨多了」分开）
                               "ret1": float(rr.mean())}
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

    keys = [k for k in obs if k in fwd]
    A_ = np.array([obs[k]["amp"] for k in keys])
    Y_ = np.array([fwd[k] for k in keys])
    D_ = np.array([k[0] for k in keys])
    S_ = np.array([k[1] for k in keys])
    print(f"\n{'='*64}\n### {label}：{len(cats)} 组，{len(keys):,} 个 (组,日)\n{'='*64}")

    # --- 1) 横截面（已知 ≈0，作对照）---
    ics = []
    for d in np.unique(D_):
        m = D_ == d
        if m.sum() < 8:
            continue
        ic = spearman(A_[m], Y_[m])
        if not np.isnan(ic):
            ics.append(ic)
    ics = np.array(ics)
    t_cs = ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics))) if ics.std(ddof=1) > 0 else 0
    print(f"  [横截面成分] amp 对次日超额收益的 IC = {ics.mean():+.4f} (t={t_cs:.2f}, 日数={len(ics)})")

    # --- 2) 时序成分：amp 与 fwd 都「先按板块、再按日」去均值（双向固定效应近似）---
    # 先按板块去均值（去掉板块自身的平均水平差异）
    def demean(v, g):
        out = np.zeros_like(v, dtype=float)
        for u in np.unique(g):
            m = g == u
            out[m] = v[m] - v[m].mean()
        return out

    a_s = demean(A_, S_)
    y_s = demean(Y_, S_)
    # 再去日（去掉共同的市场/日期效应）
    a_ss = demean(a_s, D_)
    y_ss = demean(y_s, D_)
    sd = a_ss.std(ddof=1)
    if sd > 0:
        # 时序相关系数（组内）
        r_ts = float(np.corrcoef(a_ss, y_ss)[0, 1])
        n_eff = len(a_ss)
        t_ts = r_ts * np.sqrt(n_eff - 2) / np.sqrt(max(1 - r_ts ** 2, 1e-12))
        print(f"  [时序成分]   组内去均值后 corr(amp, fwd) = {r_ts:+.4f} (t≈{t_ts:.2f}, n={n_eff:,})")
        # 斜率（pp per 1x 放大）
        b = float(np.polyfit(a_ss, y_ss, 1)[0])
        print(f"               斜率 = {b*100:+.3f}pp / 每 1x 放量（时序）")

        # --- 2b) 控制「板块当日涨幅」后的 amp 净效应（多元组内回归）---
        # 放量的日子往往也是大涨的日子，而 A 股短期反转 ⇒ 不加控制就分不清
        # 「放量预测反转」与「涨多了反转」。
        R1 = np.array([obs[k]["ret1"] for k in keys])
        r1_s = demean(demean(R1, S_), D_)
        X = np.column_stack([a_ss, r1_s, np.ones(len(a_ss))])
        try:
            beta, *_ = np.linalg.lstsq(X, y_ss, rcond=None)
            resid = y_ss - X @ beta
            s2 = (resid @ resid) / (len(y_ss) - X.shape[1])
            XtX_inv = np.linalg.inv(X.T @ X)
            se = np.sqrt(np.diag(XtX_inv) * s2)
            print(f"  [控制当日涨幅后] amp 净系数 = {beta[0]*100:+.3f}pp/1x "
                  f"(t={beta[0]/se[0]:+.2f})   当日涨幅系数 = {beta[1]*100:+.3f}pp/1% "
                  f"(t={beta[1]/se[1]:+.2f})")
        except Exception as e:
            print(f"  [控制当日涨幅后] 回归失败: {e}")

    # --- 3) 事件研究：amp 从 <=T 上穿到 >T ---
    print("  [事件研究] amp 上穿阈值后 %d 日收益 vs 非事件日：" % FWD)
    order = np.lexsort((D_, S_))
    prev_amp = {}
    for idx in order:
        k = (S_[idx], D_[idx]); amp = A_[idx]
        prev_amp[k] = amp
    for Tt in (1.5, 2.0, 3.0):
        ev, non = [], []
        for idx in order:
            s, d = S_[idx], D_[idx]
            pa = prev_amp.get((s, d - 1))
            if pa is None:
                continue
            if pa <= Tt < A_[idx]:
                ev.append(Y_[idx])
            elif pa <= Tt:
                non.append(Y_[idx])
        if len(ev) >= 30 and len(non) >= 30:
            ev, non = np.array(ev), np.array(non)
            dm = ev.mean() - non.mean()
            se = np.sqrt(ev.var(ddof=1) / len(ev) + non.var(ddof=1) / len(non))
            print(f"     上穿 {Tt}x: n={len(ev):5,}  事件日 {ev.mean()*100:+.3f}%  "
                  f"非事件 {non.mean()*100:+.3f}%  Δ={dm*100:+.3f}pp  t={dm/se if se>0 else 0:5.2f}")
        else:
            print(f"     上穿 {Tt}x: 样本不足 (n={len(ev)})")


build(IND, "行业口径")
build(CAT, "九眼概念口径")
