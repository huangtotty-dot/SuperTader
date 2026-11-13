# -*- coding: utf-8 -*-
"""「联动股」口径研究（2026-11-13）。

owner：「搜一只个股 → 找到相关的联动股票」。用什么定义"联动"？
候选：① 过去 N 日收益相关性 ② 同概念板块 ③ 同 legacy sector ④ 概念内再按相关
验证：用**过去**窗口选 10 只，看它们与目标在**未来** 20 日的收益相关性 ——
即"过去的联动"能不能预测"未来的联动"。

结论（见 doc/research/2026-11-13_个股联动口径.md）：
  过去相关 top10 → 未来 20 日相关 0.505（N=60）/ **0.533（N=120）**；随机 0.26；
  同概念随机 10 只 0.46；同行业 0.40；概念内再按相关挑 ≈ 相关 top10（无增益）。
  ⇒ 生产用相关性排序（N=120），概念只做解释。

用法：python scripts/experiment/linkage_study_20261113.py [FORMATION_DAYS]
"""
import json
import pathlib
import sys
from collections import defaultdict

import numpy as np

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "t_io").is_dir())
DAILY = BASE / "t_io" / "cache" / "daily_kline"
WL = BASE / "stock_hunter" / "watchlist_concept.json"

FORM, FUT, STEP = 60, 20, 20          # 形成窗 / 验证窗 / 每步天数
if len(sys.argv) > 1:
    FORM = int(sys.argv[1])
N_TARGET = 120                        # 每个时点抽多少个目标
TOPK = 10


def is_stock(c):
    c = str(c)
    if len(c) != 6 or not c.isdigit() or c[0] in "15":
        return False
    if c[:2] in ("51", "56", "58", "50", "52", "11", "12", "13"):
        return False
    return c[:2] in ("60", "00", "30", "68") or c[:3] == "920"


def load_prices(keep=300):
    out = {}
    for p in DAILY.glob("*.json"):
        if not is_stock(p.stem):
            continue
        try:
            rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
        except Exception:
            continue
        rows = rows[-keep:]
        out[p.stem] = {str(r.get("date"))[:10]: float(r.get("close") or 0) for r in rows}
    return out


def main():
    px = load_prices()
    all_dates = sorted({d for m in px.values() for d in m})
    dates = all_dates[-300:]
    di = {d: i for i, d in enumerate(dates)}
    codes = [c for c in px if len(px[c]) >= int(len(dates) * 0.9)]
    print(f"股票 {len(codes)} / 交易日 {len(dates)}  {dates[0]} → {dates[-1]}")

    # 价格矩阵（缺失 = NaN）→ 收益矩阵
    M = np.full((len(dates), len(codes)), np.nan, dtype=np.float32)
    for j, c in enumerate(codes):
        for d, v in px[c].items():
            i = di.get(d)
            if i is not None:
                M[i, j] = v
    M[M <= 0] = np.nan
    R = np.full_like(M, np.nan)
    R[1:] = M[1:] / M[:-1] - 1.0
    R[np.abs(R) > 0.5] = np.nan                 # 复权/新股伪影
    print(f"矩阵 {M.shape}  非空 {np.isfinite(R).sum()/R.size:.1%}")

    # 概念/行业标签
    wl = json.loads(WL.read_text(encoding="utf-8"))
    concept = {}
    sector1 = {}
    for c in codes:
        v = wl.get(c) or {}
        tags = []
        for k in ("concept1", "concept2"):
            t = v.get(k)
            if isinstance(t, str) and t.strip():
                tags.append(t.strip())
        concept[c] = set(tags)
        s = str(v.get("sector") or "")
        sector1[c] = s.split("/")[0].strip() if s else ""
    n_con = sum(1 for c in codes if concept[c])
    print(f"有概念标签 {n_con}/{len(codes)}；有 sector {sum(1 for c in codes if sector1[c])}")

    rng = np.random.default_rng(7)
    rows = []
    for t in range(FORM, len(dates) - FUT, STEP):
        F = R[t - FORM:t]                       # 形成窗
        Fu = R[t:t + FUT]                       # 验证窗
        ok_f = np.isfinite(F).all(axis=0)
        ok_u = np.isfinite(Fu).all(axis=0)
        ok = ok_f & ok_u
        idx = np.where(ok)[0]
        if len(idx) < 500:
            continue
        # 标准化后的形成窗（算相关用）
        Fs = F[:, idx]
        Fz = (Fs - Fs.mean(0)) / (Fs.std(0) + 1e-12)
        Uz = (Fu[:, idx] - Fu[:, idx].mean(0)) / (Fu[:, idx].std(0) + 1e-12)
        n = Fz.shape[1]
        tgt_pos = rng.choice(n, size=min(N_TARGET, n), replace=False)
        for ti in tgt_pos:
            c_t = Fz[:, ti]
            C = (Fz.T @ c_t) / FORM                          # 过去相关
            Cu = (Uz.T @ Uz[:, ti]) / FUT                    # 未来相关
            # ① 过去相关 topK
            order = np.argsort(-C)
            top = [k for k in order if k != ti][:TOPK]
            # ② 随机
            rnd = [k for k in rng.choice(n, size=TOPK * 2, replace=False) if k != ti][:TOPK]
            base = Cu[rnd].mean()
            # ③ 同概念
            cc = codes[idx[ti]]
            same = [k for k in range(n) if k != ti and concept.get(codes[idx[k]]) & concept[cc]]
            con_f = Cu[same].mean() if len(same) >= 5 else np.nan
            con_all = float(np.mean([Cu[k] for k in same])) if same else np.nan
            # ④ 同 sector
            sec = [k for k in range(n) if k != ti and sector1.get(codes[idx[k]])
                   and sector1.get(codes[idx[k]]) == sector1[cc]]
            sec_f = Cu[sec].mean() if len(sec) >= 5 else np.nan
            # ⑤ 同概念里按过去相关 topK
            combo = np.nan
            if len(same) >= TOPK:
                order2 = [k for k in np.argsort(-C) if k in same][:TOPK]
                combo = Cu[order2].mean()
            rows.append({
                "top": float(Cu[top].mean()), "base": float(base),
                "concept": float(con_f), "concept_all": float(con_all),
                "sector": float(sec_f), "combo": float(combo),
                "n_same": len(same), "has_concept": bool(concept.get(cc)),
            })
    import statistics as st
    print(f"\n样本 {len(rows)} 个 (目标×时点)")
    keys = ["top", "combo", "concept", "sector", "concept_all", "base"]
    print(f"{'口径':<12}{'未来20日相关(均值)':>18}{'中位':>9}{'n':>7}")
    for k in keys:
        xs = [r[k] for r in rows if r[k] == r[k]]
        if not xs:
            continue
        print(f"{k:<12}{np.mean(xs):>18.3f}{st.median(xs):>9.3f}{len(xs):>7}")
    # 只看有概念的目标
    sub = [r for r in rows if r["has_concept"]]
    print(f"\n只看有概念标签的目标（n={len(sub)}）：")
    for k in ("top", "combo", "concept", "sector", "base"):
        xs = [r[k] for r in sub if r[k] == r[k]]
        if xs:
            print(f"  {k:<12}{np.mean(xs):>8.3f}  n={len(xs)}")


if __name__ == "__main__":
    sys.exit(main())
