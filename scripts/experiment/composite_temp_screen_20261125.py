# -*- coding: utf-8 -*-
"""综合情绪温度 · H1/H2 口径回放（2026-11-25 owner 立项）

背景：owner 指定 H1/H2 假说从「单看涨停/跌停/炸板」升级为「综合情绪温度」——
    综合温度 = 0.5 × ①涨停/跌停温度(sent_ma) + 0.5 × ②涨跌家数温度(上涨占比5日均)
（口径与 GUI「综合情绪温度」面板 1:1，web/app.js `_renderSentComposite`）。

本脚本：
  1) 重建 sent_ma 序列（复刻 gui 口径，与 h_hypothesis_daily.sent_series 同式）；
  2) 从 t_io/cache/breadth_daily.json 取涨跌家数 → 上涨占比 → MA5（窗口≥3 出数）；
  3) 合成综合温度，落盘 t_io/index_regime/composite_temp.json（每日复盘可增量更新）；
  4) 在综合温度上回放 H1/H2 全部六个口径（a/b/s × 综合版，记 H1-cX/H2-cX），
     并与原温度口径触发日对照；对每个触发日算沪深300 +5/+10 日前瞻收益作证据。
"""
import json
import os
import statistics

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def J(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


# ── 1) sent_ma（与 h_hypothesis_daily.py 同式，勿改口径）─────────────────────
def sent_series():
    e5 = J(BASE + r"\t_io\index_regime\e5_ths_cache.json")
    loc = (J(BASE + r"\t_io\cache\sentiment_local.json").get("series") or {})
    merged = {}
    for d, v in loc.items():
        if int(v.get("n") or 0) < 2000:
            continue
        d = str(d)
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d[:10]
        merged[d] = {"date": d, "zt": int(v.get("zt") or 0), "dt": int(v.get("dt") or 0),
                     "touch": v.get("touch"), "zb_rate": v.get("zb_rate")}
    for k, v in e5.items():
        if v.get("zt_count") is None or v.get("dt_count") is None:
            continue
        k = str(k)
        d = f"{k[:4]}-{k[4:6]}-{k[6:]}"
        merged[d] = {"date": d, "zt": v["zt_count"], "dt": v["dt_count"],
                     "touch": v.get("zt_touched"), "zb_rate": v.get("zb_rate")}
    rows = [merged[d] for d in sorted(merged)]

    def roll_pct(vals, w=60, min_n=20):
        out = []
        for j, v in enumerate(vals):
            seg = [x for x in vals[max(0, j - w + 1):j + 1] if x is not None]
            out.append(round(sum(1 for x in seg if x <= v) / len(seg), 4)
                       if (v is not None and len(seg) >= min_n) else None)
        return out

    def ma5(vals):
        out = []
        for j in range(len(vals)):
            seg = [v for v in vals[max(0, j - 4):j + 1] if v is not None]
            out.append(round(sum(seg) / len(seg), 4) if len(seg) >= 3 else None)
        return out

    pt = roll_pct([r["touch"] for r in rows])
    pz = roll_pct([r["zb_rate"] for r in rows])
    pdt = roll_pct([r["dt"] for r in rows])
    ptm, pzm, pdm = ma5(pt), ma5(pz), ma5(pdt)
    for j, r in enumerate(rows):
        r["sent_ma"] = (round(100 * (0.5 * ptm[j] + 0.3 * (1 - pzm[j]) + 0.2 * (1 - pdm[j])), 1)
                        if None not in (ptm[j], pzm[j], pdm[j]) else None)
    return rows


def ma5_of(vals, min_n=3):
    out = []
    for j in range(len(vals)):
        seg = [v for v in vals[max(0, j - 4):j + 1] if v is not None]
        out.append(round(sum(seg) / len(seg), 1) if len(seg) >= min_n else None)
    return out


# ── 2) 涨跌家数温度 ─────────────────────────────────────────────────────────
def breadth_ratio_ma5(dates):
    bd = (J(BASE + r"\t_io\cache\breadth_daily.json").get("series") or {})
    ratio = {}
    for d, arr in bd.items():
        try:
            up, dn = float(arr[0]), float(arr[1])
        except Exception:
            continue
        if up + dn > 0:
            ratio[str(d)[:10]] = up / (up + dn) * 100.0
    raw = [ratio.get(d) for d in dates]
    return ma5_of(raw), raw


# ── 3) 触发扫描（与 h_hypothesis_daily.py 同规则，输入换成综合温度）──────────
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


def scan(rows, key):
    """在 rows[key] 上扫 H1-a/H1-b/H1-s/H2-a/H2-b/H2-s（规则同原脚本）。"""
    sv = [r.get(key) for r in rows]
    sl = [None] * len(rows)
    for j in range(5, len(rows)):
        if sv[j] is not None and sv[j - 5] is not None:
            sl[j] = round((sv[j] - sv[j - 5]) / 5, 2)
    h1a, h1b, h1s, h2a, h2b, h2s60, h2s70 = [], [], [], [], [], [], []
    for a, b in _segments(sv, lambda v: v <= 25):
        for j in range(a + 1, b + 2):
            if j < len(sv) and sv[j] is not None and sv[j - 1] is not None and sv[j] > sv[j - 1]:
                h1b.append(rows[j]["date"])
                break
    for a, b in _segments(sv, lambda v: v >= 70):
        pk = max(range(a, b + 1), key=lambda j: sv[j])
        if pk < b:
            h2b.append(rows[pk + 1]["date"])
        elif b + 1 < len(sv) and sv[b + 1] is not None and sv[b + 1] < sv[b]:
            h2b.append(rows[b + 1]["date"])
    for j in range(1, len(rows)):
        va, vb = sv[j - 1], sv[j]
        if va is None or vb is None or sl[j] is None or sl[j - 1] is None:
            continue
        if va <= 20 and (vb - va >= 5 or (sl[j] > 0 and sl[j - 1] > 0)):
            h1a.append(rows[j]["date"])
        if va >= 70 and sl[j] < 0 <= sl[j - 1]:
            h2a.append(rows[j]["date"])
        recent = [v for v in sv[max(0, j - 5):j + 1] if v is not None]
        if not recent:
            continue
        if sl[j - 1] < 0 < sl[j] and min(recent) <= 25:
            h1s.append(rows[j]["date"])
        if sl[j - 1] > 0 > sl[j]:
            if max(recent) >= 60:
                h2s60.append(rows[j]["date"])
            if max(recent) >= 70:
                h2s70.append(rows[j]["date"])
    return {"H1-a": h1a, "H1-b": h1b, "H1-s": h1s,
            "H2-a": h2a, "H2-b": h2b, "H2-s60": h2s60, "H2-s70": h2s70}


# ── 4) 前瞻收益证据（沪深300）──────────────────────────────────────────────
def index_fwd():
    d = J(BASE + r"\t_io\cache\daily_kline\index_sh000300.json")
    rows = d.get("rows") or []
    out = {}
    dates = [str(r.get("date"))[:10] for r in rows]
    closes = [float(r.get("close") or 0) for r in rows]
    pos = {d: i for i, d in enumerate(dates)}
    for d, i in pos.items():
        f = {}
        for h in (5, 10):
            if i + h < len(closes) and closes[i] > 0:
                f[f"r{h}"] = round((closes[i + h] / closes[i] - 1) * 100, 2)
        out[d] = f
    return out


def report(name, days, fwd):
    if not days:
        print(f"  {name:<10}: 0 天")
        return
    r5 = [fwd[d]["r5"] for d in days if d in fwd and "r5" in fwd[d]]
    r10 = [fwd[d]["r10"] for d in days if d in fwd and "r10" in fwd[d]]
    w5 = sum(1 for x in r5 if x > 0) / len(r5) * 100 if r5 else 0
    w10 = sum(1 for x in r10 if x > 0) / len(r10) * 100 if r10 else 0
    m5 = statistics.mean(r5) if r5 else 0
    print(f"  {name:<10}: {len(days):>2} 天 | +5日胜率 {w5:.0f}% 均 {m5:+.2f}% | "
          f"+10日胜率 {w10:.0f}% | 近6次 {days[-6:]}")


def main():
    rows = sent_series()
    dates = [r["date"] for r in rows]
    t2, ratio_raw = breadth_ratio_ma5(dates)
    for j, r in enumerate(rows):
        r["t2_ma"] = t2[j]
        r["up_ratio"] = round(ratio_raw[j], 1) if ratio_raw[j] is not None else None
        r["comp"] = (round((r["sent_ma"] + t2[j]) / 2, 1)
                     if r["sent_ma"] is not None and t2[j] is not None else None)

    # 落盘综合温度序列（每日复盘可复用）
    out_fp = BASE + r"\t_io\index_regime\composite_temp.json"
    json.dump({r["date"]: {"sent_ma": r["sent_ma"], "t2_ma": r["t2_ma"],
                           "up_ratio": r["up_ratio"], "comp": r["comp"]}
               for r in rows if r.get("comp") is not None},
              open(out_fp, "w", encoding="utf-8"), ensure_ascii=False, indent=0)

    print("=== 近 10 个交易日：原温度 vs 综合温度 ===")
    print(f"{'日期':<12}{'sent_ma':>8}{'t2_ma':>7}{'comp':>7}{'up%':>7}")
    for r in rows[-10:]:
        print(f"{r['date']:<12}{r['sent_ma']!s:>8}{r['t2_ma']!s:>7}"
              f"{r['comp']!s:>7}{r['up_ratio']!s:>7}")

    fwd = index_fwd()
    print("\n=== 原温度口径（对照组）===")
    for k, v in scan(rows, "sent_ma").items():
        report(k, v, fwd)
    print("\n=== 综合温度口径（实验组 H1-c/H2-c）===")
    for k, v in scan(rows, "comp").items():
        report(k, v, fwd)

    print(f"\n[composite_temp] 已落盘 {out_fp}（{sum(1 for r in rows if r.get('comp') is not None)} 天）")


if __name__ == "__main__":
    main()
