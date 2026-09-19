# -*- coding: utf-8 -*-
"""背离过滤因子**组合**验证（2026-09-20）。

单因子筛选（`mine_divergence_filters.py`）发现两个特征在**四个组合里全部三段同号**：

  price_excess —— 新高/新低的**幅度**（越大越好）
  swing_depth  —— 两峰/两谷之间**摆动深度**（越深越好）

这与三轮误报排查的根因完全同构：**幅度勉强、摆动浅的极值 → 假背离**。

本脚本把二者组合，回答「加了这道闸，命中率能提多少、还剩多少样本」。

## 口径

- 基线 = 同周期同方向的**全部背离事件**（条件化问题，不是"背离 vs 非背离"）。
- 分位阈值在**本组内**取（避免跨周期量纲差）。
- 报告：命中率 / n / lift / Wilson 95%CI / **三段符号**。
- 副标签：顶背离后应跌、底背离后应涨（方向正确率）。

用法：python t_io/validation/w35_divergence/combine_divergence_filters.py --days 540
"""
import argparse
import datetime as _dt
import json
import math
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis import divergence  # noqa: E402
from t_io.validation.w35_divergence.validate_divergence import WARMUP, _load_watchlist  # noqa: E402
from t_io.validation.w35_divergence.mine_divergence_filters import (  # noqa: E402
    build_events, _fwd_ok, _wilson)

MIN_CELL = 25


def _collect(days):
    ev = []
    for code in _load_watchlist():
        for freq in ("30min", "60min"):
            try:
                df = divergence.fetch_freq_kline(code, freq, days=days)
            except Exception:
                continue
            if df is None or df.empty or len(df) < WARMUP + 40:
                continue
            ev.extend(build_events(df, freq, code))
    return ev


def _segs(ev, n):
    ds = sorted({e["date"] for e in ev})
    d0, d1 = _dt.date.fromisoformat(ds[0]), _dt.date.fromisoformat(ds[-1])
    span = (d1 - d0).days
    b = [d0 + _dt.timedelta(days=int(span * k / n)) for k in range(n + 1)]
    for e in ev:
        d = _dt.date.fromisoformat(e["date"])
        e["seg"] = next((k for k in range(n) if b[k] <= d < b[k + 1]), n - 1)


def _apply(grp, combos, seg_n, base):
    recs = []
    for label, fn in combos:
        pct = 60 if "P60" in label else (70 if "P70" in label else 0)
        pa = float(np.percentile([e["price_excess"] for e in grp], pct)) if pct else 0
        sa = float(np.percentile([e["swing_depth"] for e in grp], pct)) if pct else 0
        sel = [e for e in grp if fn(e, pa, sa)]
        if len(sel) < MIN_CELL:
            continue
        h = sum(1 for e in sel if e["hit"])
        lo, hi = _wilson(h, len(sel))
        signs = []
        for s in range(seg_n):
            sub = [e for e in sel if e["seg"] == s]
            if len(sub) >= 8:
                signs.append("+" if sum(1 for e in sub if e["hit"]) / len(sub) > base else "-")
        recs.append({"label": label, "rate": h / len(sel), "n": len(sel),
                     "lift": h / len(sel) - base, "ci": [lo, hi],
                     "seg_signs": "".join(signs),
                     "stable": len(signs) == seg_n and len(set(signs)) == 1})
    return recs


def _oos(ev, freq, kind, split_frac=2.0 / 3.0):
    """样本外：前 2/3 选规则（含阈值），后 1/3 检验。规则在两侧用同一「公式」重算分位。

    这一步是为了排除"P70 是看着全样本结果挑出来的"这一自我欺骗。"""
    grp = [e for e in ev if e["freq"] == freq and e["kind"] == kind
           and e.get("price_excess") is not None and e.get("swing_depth") is not None]
    if len(grp) < 120:
        return None
    ds = sorted(e["date"] for e in grp)
    cut = ds[int(len(ds) * split_frac)]
    tr = [e for e in grp if e["date"] < cut]
    te = [e for e in grp if e["date"] >= cut]
    if len(tr) < 80 or len(te) < 40:
        return None
    btr = sum(1 for e in tr if e["hit"]) / len(tr)
    bte = sum(1 for e in te if e["hit"]) / len(te)
    cand = [
        ("裸背离", lambda e, pa, sa: True),
        ("price_excess≥P60", lambda e, pa, sa: e["price_excess"] >= pa),
        ("swing_depth≥P60", lambda e, pa, sa: e["swing_depth"] >= sa),
        ("两者都≥P60", lambda e, pa, sa: e["price_excess"] >= pa and e["swing_depth"] >= sa),
        ("两者都≥P70", lambda e, pa, sa: e["price_excess"] >= pa and e["swing_depth"] >= sa),
    ]
    train = _apply(tr, cand, 1, btr)
    test = _apply(te, cand, 1, bte)
    return {"freq": freq, "kind": kind, "cut": cut,
            "train_base": btr, "test_base": bte,
            "train_all": train, "test_all": test}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--seg", type=int, default=3)
    ap.add_argument("--oos", action="store_true", help="额外跑样本外检验")
    args = ap.parse_args()
    ev = _collect(args.days)
    _segs(ev, args.seg)
    print(f"事件 {len(ev)}  分 {args.seg} 段")

    if args.oos:
        print(f"\n{'='*108}\n【样本外】前 2/3 训练 / 后 1/3 检验 —— **全部 5 条预注册规则都测**")
        print("（只报「训练集最优」会挑到过拟合的那条；全测才能看出有没有任何一条站得住）")
        print(f"{'周期':>7}{'方向':>5}{'规则':>18}{'训练lift':>10}{'测试基线':>9}"
              f"{'测试命中':>9}{'n':>5}{'测试lift':>10}{'95%CI':>18}{'成立?':>8}")
        for freq in ("30min", "60min"):
            for kind in ("顶", "底"):
                r = _oos(ev, freq, kind)
                if not r:
                    continue
                for lab in ("裸背离", "price_excess≥P60", "swing_depth≥P60",
                            "两者都≥P60", "两者都≥P70"):
                    tr_r = next((x for x in r["train_all"] if x["label"] == lab), None)
                    te_r = next((x for x in r["test_all"] if x["label"] == lab), None)
                    if tr_r is None or te_r is None:
                        continue
                    lo, hi = te_r["ci"]
                    ok = "是" if lo > r["test_base"] else "否"
                    print(f"{freq:>7}{kind:>5}{lab:>18}{tr_r['lift']*100:>+9.1f}pp"
                          f"{r['test_base']*100:>8.1f}%{te_r['rate']*100:>8.1f}%{te_r['n']:>5}"
                          f"{te_r['lift']*100:>+9.1f}pp   [{lo*100:>5.1f},{hi*100:>5.1f}]{ok:>8}")

    combos = [
        ("裸背离（现状）", lambda e, pa, sa: True),
        (f"price_excess≥P60", lambda e, pa, sa: e["price_excess"] >= pa),
        (f"swing_depth≥P60", lambda e, pa, sa: e["swing_depth"] >= sa),
        (f"两者都≥P60", lambda e, pa, sa: e["price_excess"] >= pa and e["swing_depth"] >= sa),
        (f"两者都≥P70", lambda e, pa, sa: e["price_excess"] >= pa and e["swing_depth"] >= sa),
    ]
    out = {}
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            grp = [e for e in ev if e["freq"] == freq and e["kind"] == kind
                   and e.get("price_excess") is not None and e.get("swing_depth") is not None]
            if len(grp) < 50:
                continue
            base = sum(1 for e in grp if e["hit"]) / len(grp)
            fbase = sum(1 for e in grp if _fwd_ok(e)) / len(grp)
            print(f"\n{'='*104}\n{freq} {kind}背离   基线 {base*100:.1f}% (n={len(grp)})"
                  f"   方向正确率基线 {fbase*100:.1f}%")
            print(f"{'组合':<18}{'命中率':>9}{'n':>6}{'Δ':>9}{'95%CI':>18}"
                  f"{'方向正确':>10}{'三段符号':>12}")
            recs = []
            for label, fn in combos:
                pct = 60 if "P60" in label else (70 if "P70" in label else 0)
                pa = float(np.percentile([e["price_excess"] for e in grp], pct)) if pct else 0
                sa = float(np.percentile([e["swing_depth"] for e in grp], pct)) if pct else 0
                sel = [e for e in grp if fn(e, pa, sa)]
                if len(sel) < MIN_CELL:
                    continue
                h = sum(1 for e in sel if e["hit"])
                r = h / len(sel)
                lo, hi = _wilson(h, len(sel))
                fw = sum(1 for e in sel if _fwd_ok(e)) / len(sel)
                signs = []
                for s in range(args.seg):
                    sub = [e for e in sel if e["seg"] == s]
                    if len(sub) >= 8:
                        signs.append("+" if sum(1 for e in sub if e["hit"]) / len(sub) > base else "-")
                stable = "".join(signs)
                star = " ★" if len(signs) == args.seg and len(set(signs)) == 1 else ""
                print(f"{label:<18}{r*100:>8.1f}%{len(sel):>6}{(r-base)*100:>+8.1f}pp"
                      f"   [{lo*100:>5.1f},{hi*100:>5.1f}]{fw*100:>9.1f}%{stable:>10}{star}")
                recs.append({"label": label, "rate": r, "n": len(sel), "lift": r - base,
                             "ci": [lo, hi], "fwd_ok": fw, "seg_signs": stable,
                             "stable": len(signs) == args.seg and len(set(signs)) == 1})
            out[f"{freq}:{kind}"] = {"base": base, "n": len(grp), "fwd_base": fbase, "combos": recs}

    (BASE / "t_io" / "validation" / "w35_divergence" / "summary_filter_combo.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n→ summary_filter_combo.json")


if __name__ == "__main__":
    main()
