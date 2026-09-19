# -*- coding: utf-8 -*-
"""背离过滤阈值扫描（2026-09-20）—— 给 owner 挑工作点用。

## 背景

扩池到 974 只后已证两个因子有效（见 memory `project-divergence-validation`）：
  - `price_excess`（新高/新低幅度）—— 顶背离 +6~8pp，60min 底背离无效
  - `swing_depth`（摆动深度）—— 四组合都 +3~7pp

但"用 P60"是我在**全样本**上挑的。本脚本把**整条曲线**打出来，并**训练/测试分开报**：
若最优点在两侧位置差很多 ⇒ 曲线形状是噪声，别照抄那个点。

## 口径

- 事件表读缓存 `events_expanded.json`（57,045 条，974 只 × 540 天）。
- 互斥两组：入选(≥阈值) vs 落选(<阈值)，z 检验。
- 阈值按**分位**在该组内取（自校准），同时报它换算成的**固定值**（可直接落生产参数）。
- 训练 = 前 2/3，测试 = 后 1/3（按日期切）。

用法：python t_io/validation/w35_divergence/sweep_divergence_thresholds.py
"""
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

EVENTS_FP = BASE / "t_io" / "validation" / "w35_divergence" / "events_expanded.json"
PCTS = [0, 30, 40, 50, 60, 70, 80, 90]
FEATS = ["price_excess", "swing_depth"]


def _split(grp):
    ds = sorted(e["date"] for e in grp)
    cut = ds[int(len(ds) * 2 / 3)]
    return [e for e in grp if e["date"] < cut], [e for e in grp if e["date"] >= cut]


def _ztest(sel, exc):
    if len(sel) < 15 or len(exc) < 15:
        return None
    h1, h2 = sum(1 for e in sel if e["hit"]), sum(1 for e in exc if e["hit"])
    p1, p2 = h1 / len(sel), h2 / len(exc)
    pp = (h1 + h2) / (len(sel) + len(exc))
    se = (pp * (1 - pp) * (1 / len(sel) + 1 / len(exc))) ** 0.5
    return {"p1": p1, "n1": len(sel), "p2": p2, "n2": len(exc),
            "d": p1 - p2, "z": (p1 - p2) / se if se > 0 else 0.0}


def _row(grp, feat, pct):
    vals = [e[feat] for e in grp if e.get(feat) is not None]
    if len(vals) < 60:
        return None
    thr = float(np.percentile(vals, pct)) if pct else float("-inf")
    sel = [e for e in grp if e.get(feat) is not None and e[feat] >= thr]
    exc = [e for e in grp if e.get(feat) is not None and e[feat] < thr]
    r = _ztest(sel, exc)
    if r is None:
        return None
    r["thr"] = thr
    # 换算成可落地口径：price_excess 直接是百分比；swing_depth 用 / 该组中位 bar 振幅
    if feat == "price_excess":
        r["fixed"] = f"{thr*100:.2f}%"
    else:
        at = float(np.median([e["atr_ratio"] for e in grp if e.get("atr_ratio")]))
        r["fixed"] = f"{thr/at if at else float('nan'):.1f}x振幅"
    return r


def main():
    if not EVENTS_FP.exists():
        print(f"缺事件表 {EVENTS_FP}（先跑 expand_universe_mine.py）")
        return
    ev = json.loads(EVENTS_FP.read_text(encoding="utf-8"))
    print(f"事件 {len(ev)}（读缓存）")
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            grp = [e for e in ev if e["freq"] == freq and e["kind"] == kind
                   and e.get("price_excess") is not None]
            if len(grp) < 400:
                continue
            tr, te = _split(grp)
            print(f"\n{'='*112}\n{freq} {kind}背离   全样本 n={len(grp)}"
                  f"   基线 {sum(1 for e in grp if e['hit'])/len(grp)*100:.1f}%"
                  f"   训练 n={len(tr)}  测试 n={len(te)}")
            for feat in FEATS:
                print(f"  ── {feat}")
                print(f"  {'分位':>6}{'阈值(可落地)':>16}{'全样本Δ':>10}{'z':>7}"
                      f"{'  ┃ 训练Δ':>11}{'z':>7}{'  ┃ 测试Δ':>11}{'z':>7}{'剩余n':>8}")
                for pct in PCTS:
                    a = _row(grp, feat, pct)
                    b = _row(tr, feat, pct)
                    c = _row(te, feat, pct)
                    if not a:
                        continue
                    lab = "无门槛" if pct == 0 else f"P{pct}"
                    def cell(r):
                        return (f"{r['d']*100:>+9.1f}pp{r['z']:>7.2f}" if r else
                                f"{'—':>9}{'—':>7}")
                    print(f"  {lab:>6}{a['fixed']:>16}{a['d']*100:>+9.1f}pp{a['z']:>7.2f}"
                          f"{'  ┃':>3}{cell(b)}{'  ┃':>3}{cell(c)}{c['n1'] if c else 0:>8}")


if __name__ == "__main__":
    main()
