# -*- coding: utf-8 -*-
"""背离过滤因子 —— **扩池提功效**复核（2026-09-20）。

## 为什么要扩池

43 只 × 540 天的样本外检验集只有 n=27~350，CI 宽达 ±5~10pp ⇒
**+5pp 量级的效应在这个数据上确认不了**。上一轮的结论是
「瓶颈是统计功效，不是缺因子」；本脚本把股票池从 43 扩到 ~974（韭研池），
把 CI 压下来。

## 🔒 预注册（在跑之前写死，防事后改口径）

上一轮在 43 只池上，15 个因子里**只有 `price_excess`（新高/新低幅度）**
在四个组合的样本外都为正、且汇总后勉强显著（+5.1pp, CI[36.9, 47.2], n=350）。

**本轮只验这一个假设**：

  H1：`price_excess ≥ P60`（本组内分位）把背离命中率提升 ≥ +3pp
  H1 证伪条件：扩池后 pooled Δ < +2pp，或 95%CI 包含 0

**同时预注册两个"不要再走一遍"的对照**（上一轮已证其过拟合，此处只做确认）：
  - `swing_depth ≥ P60` 单独
  - `两者都 ≥ P60`（组合）—— 预期**弱于**单用 price_excess

口径与上一轮完全一致：标签 = 驻顶/驻底（K=3 交易日、3% 反向）；
分位在本组内取；时间 2/3 训练 / 1/3 检验**沿用**（本轮主口径是**全样本 + 时间切分双报**）。

⚠️ 诚实声明：扩池是**横截面**扩容、时间区间不变 ⇒ 它解决的是**功效**，
不解决"特定时段过拟合"（后者靠时间切分）。两个都要报。

用法：python t_io/validation/w35_divergence/expand_universe_mine.py [--limit N]
"""
import argparse
import datetime as _dt
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent.parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis import divergence  # noqa: E402
from t_io.validation.w35_divergence.validate_divergence import WARMUP  # noqa: E402
from t_io.validation.w35_divergence.mine_divergence_filters import (  # noqa: E402
    build_events, _wilson)
from t_io.validation.w35_divergence.combine_divergence_filters import _apply  # noqa: E402

OUT_DIR = BASE / "t_io" / "validation" / "w35_divergence"
EVENTS_FP = OUT_DIR / "events_expanded.json"


def _universe(limit=None):
    fp = BASE / "stock_hunter" / "watchlist_jiuyan.json"
    d = json.loads(fp.read_text(encoding="utf-8"))
    codes = [k for k, v in d.items()
             if isinstance(v, dict) and k.isdigit() and len(k) == 6
             and (v.get("jiuyan_concept") or "").strip()]
    return codes[:limit] if limit else codes


def build(days=540, limit=None, force=False):
    if EVENTS_FP.exists() and not force:
        ev = json.loads(EVENTS_FP.read_text(encoding="utf-8"))
        print(f"[cache] 事件表已存在：{len(ev)} 条 → {EVENTS_FP.name}")
        return ev
    codes = _universe(limit)
    print(f"扩池：{len(codes)} 只 × 2 周期 × {days} 天（预计 ~{len(codes)*2*0.45/60:.0f} 分钟）")
    ev, ok, bad = [], 0, 0
    t0 = time.time()
    for n, code in enumerate(codes, 1):
        for freq in ("30min", "60min"):
            try:
                df = divergence.fetch_freq_kline(code, freq, days=days)
            except Exception:
                bad += 1
                continue
            if df is None or df.empty or len(df) < WARMUP + 40:
                bad += 1
                continue
            ev.extend(build_events(df, freq, code))
            ok += 1
        if n % 50 == 0:
            el = time.time() - t0
            print(f"  {n}/{len(codes)}  事件 {len(ev)}  "
                  f"用时 {el/60:.1f}min  预计剩余 {el/n*(len(codes)-n)/60:.1f}min", flush=True)
    print(f"\n完成：有效(票,周期) {ok}  跳过 {bad}  事件 {len(ev)}  "
          f"用时 {(time.time()-t0)/60:.1f}min")
    EVENTS_FP.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    print(f"→ {EVENTS_FP}")
    return ev


def analyse(ev, seg=3):
    ds = sorted({e["date"] for e in ev})
    d0, d1 = _dt.date.fromisoformat(ds[0]), _dt.date.fromisoformat(ds[-1])
    span = (d1 - d0).days
    b = [d0 + _dt.timedelta(days=int(span * k / seg)) for k in range(seg + 1)]
    for e in ev:
        d = _dt.date.fromisoformat(e["date"])
        e["seg"] = next((k for k in range(seg) if b[k] <= d < b[k + 1]), seg - 1)

    cand = [("裸背离", lambda e, pa, sa: True),
            ("price_excess≥P60", lambda e, pa, sa: e["price_excess"] >= pa),
            ("swing_depth≥P60", lambda e, pa, sa: e["swing_depth"] >= sa),
            ("两者都≥P60", lambda e, pa, sa: e["price_excess"] >= pa and e["swing_depth"] >= sa)]
    print(f"\n事件 {len(ev)}   区间 {ds[0]} ~ {ds[-1]}   分 {seg} 段")

    print(f"\n{'='*104}\n【全样本】扩池后各规则")
    print(f"{'周期':>7}{'方向':>5}{'规则':>18}{'基线':>9}{'命中率':>9}{'n':>7}{'Δ':>9}"
          f"{'95%CI':>18}{'三段符号':>11}")
    allrows = {}
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            grp = [e for e in ev if e["freq"] == freq and e["kind"] == kind
                   and e.get("price_excess") is not None and e.get("swing_depth") is not None]
            if len(grp) < 200:
                continue
            base = sum(1 for e in grp if e["hit"]) / len(grp)
            for r in _apply(grp, cand, seg, base):
                lo, hi = r["ci"]
                print(f"{freq:>7}{kind:>5}{r['label']:>18}{base*100:>8.1f}%{r['rate']*100:>8.1f}%"
                      f"{r['n']:>7}{r['lift']*100:>+8.1f}pp   [{lo*100:>5.1f},{hi*100:>5.1f}]"
                      f"{r['seg_signs']:>9}{' ★' if r['stable'] else ''}")
                allrows[(freq, kind, r["label"])] = r

    # 时间切分 OOS：pooled（把所有 (周期,方向) 的测试集汇总，提功效）
    print(f"\n{'='*104}\n【时间切分 2/3:1/3 后汇总】—— 本轮主口径")
    pooled = defaultdict(lambda: [0, 0])
    pbase = [0, 0]
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            grp = [e for e in ev if e["freq"] == freq and e["kind"] == kind
                   and e.get("price_excess") is not None and e.get("swing_depth") is not None]
            if len(grp) < 200:
                continue
            dss = sorted(e["date"] for e in grp)
            cut = dss[int(len(dss) * 2 / 3)]
            te = [e for e in grp if e["date"] >= cut]
            if len(te) < 60:
                continue
            pbase[0] += sum(1 for e in te if e["hit"])
            pbase[1] += len(te)
            bte = sum(1 for e in te if e["hit"]) / len(te)
            for r in _apply(te, cand, 1, bte):
                pooled[r["label"]][0] += round(r["rate"] * r["n"])
                pooled[r["label"]][1] += r["n"]
    bp = pbase[0] / pbase[1] if pbase[1] else float("nan")
    print(f"{'规则':<20}{'命中率':>9}{'n':>7}{'Δ':>9}{'95%CI':>20}{'H1?':>8}   （基线 {bp*100:.1f}%, n={pbase[1]}）")
    for lab in ("裸背离", "price_excess≥P60", "swing_depth≥P60", "两者都≥P60"):
        h, n = pooled[lab]
        if not n:
            continue
        r = h / n
        lo, hi = _wilson(h, n)
        verdict = ""
        if lab == "price_excess≥P60":
            verdict = "成立" if (lo > bp and (r - bp) * 100 >= 3) else ("弱" if lo > bp else "证伪")
        print(f"{lab:<20}{r*100:>8.1f}%{n:>7}{(r-bp)*100:>+8.1f}pp"
              f"   [{lo*100:>5.1f},{hi*100:>5.1f}]{verdict:>8}")

    # ── 更干净的检验：**入选 vs 落选**（两组互斥）。
    # 与"入选 vs 全体基线"比是稀释的——入选组本身是基线的一部分。
    # 互斥两组的 z 检验才有正确的功效。
    print(f"\n{'='*104}\n【互斥两组】入选(≥P60) vs 落选(<P60) —— 用同一批样本外事件切开，这才是干净的功效")
    print(f"{'规则':<20}{'入选':>9}{'n':>7}{'落选':>9}{'n':>7}{'Δ':>9}{'z':>7}{'p<0.05?':>9}")
    disj = defaultdict(lambda: {"a": [0, 0], "b": [0, 0]})
    for freq in ("30min", "60min"):
        for kind in ("顶", "底"):
            grp = [e for e in ev if e["freq"] == freq and e["kind"] == kind
                   and e.get("price_excess") is not None and e.get("swing_depth") is not None]
            if len(grp) < 200:
                continue
            dss = sorted(e["date"] for e in grp)
            cut = dss[int(len(dss) * 2 / 3)]
            te = [e for e in grp if e["date"] >= cut]
            if len(te) < 60:
                continue
            for lab, fn in cand:
                if lab == "裸背离":
                    continue
                pct = 60
                pa = float(np.percentile([e["price_excess"] for e in te], pct))
                sa = float(np.percentile([e["swing_depth"] for e in te], pct))
                sel = [e for e in te if fn(e, pa, sa)]
                exc = [e for e in te if not fn(e, pa, sa)]
                if len(sel) < 30 or len(exc) < 30:
                    continue
                disj[lab]["a"][0] += sum(1 for e in sel if e["hit"])
                disj[lab]["a"][1] += len(sel)
                disj[lab]["b"][0] += sum(1 for e in exc if e["hit"])
                disj[lab]["b"][1] += len(exc)
    for lab in ("price_excess≥P60", "swing_depth≥P60", "两者都≥P60"):
        a, b_ = disj[lab]["a"], disj[lab]["b"]
        if not a[1] or not b_[1]:
            continue
        p1, p2 = a[0] / a[1], b_[0] / b_[1]
        pp = (a[0] + b_[0]) / (a[1] + b_[1])
        se = (pp * (1 - pp) * (1 / a[1] + 1 / b_[1])) ** 0.5
        z = (p1 - p2) / se if se > 0 else 0.0
        print(f"{lab:<20}{p1*100:>8.1f}%{a[1]:>7}{p2*100:>8.1f}%{b_[1]:>7}"
              f"{(p1-p2)*100:>+8.1f}pp{z:>7.2f}{('是' if abs(z)>1.96 else '否'):>9}")

    # ── 按**股票聚类**的稳健检验。
    # 上面两个检验都把每个"事件"当独立样本，但同一只票的事件高度相关
    # （同一段行情里的多个峰谷）⇒ 会高估显著性。
    # 正确做法：以**股票**为单位，先算每只票的 lift，再对"票间均值是否为0"做 t 检验。
    print(f"\n{'='*104}\n【按股票聚类】以「票」为单位算 lift → 对均值做 t 检验（n=股票数，不是事件数）")
    print(f"{'规则':<20}{'票数':>6}{'均值Δ':>9}{'t':>8}{'p<0.05?':>9}")
    for lab, fn in cand:
        if lab == "裸背离":
            continue
        per = []
        for code in {e["code"] for e in ev}:
            sel_h = sel_n = exc_h = exc_n = 0
            for freq in ("30min", "60min"):
                for kind in ("顶", "底"):
                    grp = [e for e in ev if e["code"] == code and e["freq"] == freq
                           and e["kind"] == kind and e.get("price_excess") is not None
                           and e.get("swing_depth") is not None]
                    # 单元格 = 单票×周期×方向，每格只有 ~15 个事件 ⇒ 门槛必须远低于 40
                    if len(grp) < 12:
                        continue
                    pa = float(np.percentile([e["price_excess"] for e in grp], 60))
                    sa = float(np.percentile([e["swing_depth"] for e in grp], 60))
                    for e in grp:
                        if fn(e, pa, sa):
                            sel_n += 1
                            sel_h += 1 if e["hit"] else 0
                        else:
                            exc_n += 1
                            exc_h += 1 if e["hit"] else 0
            if sel_n >= 4 and exc_n >= 4:
                per.append(sel_h / sel_n - exc_h / exc_n)
        if len(per) >= 10:
            arr = np.array(per)
            se = arr.std(ddof=1) / len(arr) ** 0.5
            t = arr.mean() / se if se > 0 else 0.0
            print(f"{lab:<20}{len(per):>6}{arr.mean()*100:>+8.2f}pp{t:>8.2f}"
                  f"{('是' if abs(t) > 1.96 else '否'):>9}")
    return allrows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="只取前 N 只（调试用）")
    ap.add_argument("--days", type=int, default=540)
    ap.add_argument("--force", action="store_true", help="忽略事件表缓存，重拉")
    args = ap.parse_args()
    ev = build(days=args.days, limit=args.limit, force=args.force)
    if not ev:
        print("无事件")
        return
    analyse(ev)


if __name__ == "__main__":
    main()
