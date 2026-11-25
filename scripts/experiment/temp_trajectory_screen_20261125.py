# -*- coding: utf-8 -*-
"""H6 情绪温度轨迹形态分类 · 全历史回放（2026-11-25 owner 立项）

owner 观察：温度从极冰点出发的路径不止一种——
  ① 极冰点 → 平稳区 → 极热点（V 型直达）
  ② 极冰点 → 平稳区/部分升温 → 二次探底（W 型）
  热点侧镜像：倒 V 直达降温 vs M 型二次冲高 vs 高位钝化。
远景：大样本确认各路径的分布与收益差后，做「情绪温度判定器」，
选股买卖按路径形态自适应调整（现阶段只记录，不碰任何生产规则）。

分类口径（sent_ma 或综合温度 comp，双温度计各跑一遍）：
  冰点段（≤25）段后首个上升日 T（与 H1-b 同规则）起，向前看 20 个交易日：
    A  直达平稳：未再 ≤25，且 20 日内触及 ≥40（其中 ≥60 记 A+ 直达强势）
    B  二次探底：20 日内再次 ≤25，其后 20 日内出现新拐点（再上升日）
    C  冰点延续：20 日内再次 ≤25 且之后无新拐点（假拐点/磨底）
  热点段（≥70）峰后首个下跌日 T（与 H2-b 同规则）起，向前看 20 个交易日：
    D  直达弱势：未再 ≥70，且 20 日内触及 ≤40
    E  二次冲高：20 日内再次 ≥70
    F  高位钝化：20 日内始终 40~70 之间
  窗口不足 20 日 → 「在飞」。
证据：各路径触发日 T 的沪深300 +5/+10/+20 日前瞻收益。
"""
import json
import os
import statistics

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FWD_WIN = 20


def J(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def index_fwd():
    d = J(BASE + r"\t_io\cache\daily_kline\index_sh000300.json")
    rows = d.get("rows") or []
    dates = [str(r.get("date"))[:10] for r in rows]
    closes = [float(r.get("close") or 0) for r in rows]
    out = {}
    for i, dt in enumerate(dates):
        f = {}
        for h in (5, 10, 20):
            if i + h < len(closes) and closes[i] > 0:
                f[h] = round((closes[i + h] / closes[i] - 1) * 100, 2)
        out[dt] = f
    return out


def _segments(vals, cond):
    segs, s = [], None
    for j, v in enumerate(vals):
        if v is not None and cond(v):
            if s is None:
                s = j
        else:
            if s is not None:
                segs.append((s, j - 1))
                s = None
    if s is not None:
        segs.append((s, len(vals) - 1))
    return segs


def _first_rise(vals, a, b):
    """段 [a,b] 之后首个上升日下标（H1-b 规则），无则 None。"""
    for j in range(a + 1, b + 2):
        if j < len(vals) and vals[j] is not None and vals[j - 1] is not None and vals[j] > vals[j - 1]:
            return j
    return None


def ice_paths(rows, key):
    """冰点侧轨迹分类 → list of dict(T, date, kind, detail)。"""
    vals = [r.get(key) for r in rows]
    out = []
    for a, b in _segments(vals, lambda v: v <= 25):
        t = _first_rise(vals, a, b)
        if t is None:
            continue
        win = [v for v in vals[t + 1:t + 1 + FWD_WIN] if v is not None]
        if len(win) < FWD_WIN // 2:
            kind, detail = "在飞", f"窗口仅{len(win)}日"
        elif any(v <= 25 for v in win):
            # 二次探底：其后 20 日内有无新拐点
            dip = next(j for j in range(t + 1, min(t + 1 + FWD_WIN, len(vals)))
                       if vals[j] is not None and vals[j] <= 25)
            nxt = _first_rise(vals, dip, min(dip + FWD_WIN, len(vals) - 1))
            if nxt is not None and nxt - dip <= FWD_WIN:
                kind, detail = "B 二次探底", f"T+{dip - t}日回冰点，T+{nxt - t}日再拐"
            else:
                kind, detail = "C 冰点延续", f"T+{dip - t}日回冰点后未见新拐"
        elif max(win) >= 60:
            kind, detail = "A+ 直达强势", f"峰值 {max(win):.1f}"
        elif max(win) >= 40:
            kind, detail = "A 直达平稳", f"峰值 {max(win):.1f}"
        else:
            kind, detail = "C 冰点延续", f"20日峰值仅 {max(win):.1f}（未回冰点也未到平稳）"
        out.append({"T": rows[t]["date"], "kind": kind, "detail": detail})
    return out


def hot_paths(rows, key):
    """热点侧轨迹分类（镜像）。"""
    vals = [r.get(key) for r in rows]
    out = []
    for a, b in _segments(vals, lambda v: v >= 70):
        pk = max(range(a, b + 1), key=lambda j: vals[j])
        if pk < b:
            t = pk + 1
        elif b + 1 < len(vals) and vals[b + 1] is not None and vals[b + 1] < vals[b]:
            t = b + 1
        else:
            continue
        win = [v for v in vals[t + 1:t + 1 + FWD_WIN] if v is not None]
        if len(win) < FWD_WIN // 2:
            kind, detail = "在飞", f"窗口仅{len(win)}日"
        elif any(v >= 70 for v in win):
            re_pk = next(j for j in range(t + 1, min(t + 1 + FWD_WIN, len(vals)))
                         if vals[j] is not None and vals[j] >= 70)
            kind, detail = "E 二次冲高", f"T+{re_pk - t}日再 ≥70"
        elif min(win) <= 40:
            kind, detail = "D 直达弱势", f"谷值 {min(win):.1f}"
        else:
            kind, detail = "F 高位钝化", f"区间 {min(win):.1f}~{max(win):.1f}"
        out.append({"T": rows[t]["date"], "kind": kind, "detail": detail})
    return out


def report(title, paths, fwd, long_side=True):
    print(f"\n=== {title} ===")
    kinds = {}
    for p in paths:
        kinds.setdefault(p["kind"], []).append(p)
    for k in sorted(kinds):
        ps = kinds[k]
        rets = {h: [fwd[p["T"]][h] for p in ps if p["T"] in fwd and h in fwd[p["T"]]]
                for h in (5, 10, 20)}
        cells = []
        for h in (5, 10, 20):
            r = rets[h]
            if r:
                win = sum(1 for x in r if x > 0) / len(r) * 100
                cells.append(f"+{h}日 {win:.0f}%/均{statistics.mean(r):+.2f}%")
            else:
                cells.append(f"+{h}日 —")
        print(f"  {k:<10}: n={len(ps):>2} | {' | '.join(cells)}")
        for p in ps[-4:]:
            print(f"      {p['T']} {p['detail']}")


def live_status(rows, key, label):
    """当前在飞段的轨迹实况。"""
    vals = [r.get(key) for r in rows]
    n = len(vals)
    tail = [(rows[j]["date"], vals[j]) for j in range(max(0, n - 12), n)]
    print(f"\n[{label} 近 12 日] " + " | ".join(f"{d[5:]}={v}" for d, v in tail))


def main():
    comp = J(BASE + r"\t_io\index_regime\composite_temp.json")
    dates = sorted(comp)
    rows = [{"date": d, "sent_ma": comp[d]["sent_ma"], "comp": comp[d]["comp"]}
            for d in dates]
    fwd = index_fwd()

    for key, label in (("sent_ma", "原温度"), ("comp", "综合温度")):
        print(f"\n{'=' * 60}\n温度计：{label}\n{'=' * 60}")
        report("冰点侧轨迹（H1 触发后的路径分布）", ice_paths(rows, key), fwd)
        report("热点侧轨迹（H2 触发后的路径分布）", hot_paths(rows, key), fwd)
        live_status(rows, key, label)


if __name__ == "__main__":
    main()
