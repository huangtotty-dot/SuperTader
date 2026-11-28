# -*- coding: utf-8 -*-
"""H1~H5 假说每日对照采集（复盘专用，不写库、不触网）。

用法：
    python scripts/experiment/h_hypothesis_daily.py --date 2026-11-27
    python scripts/experiment/h_hypothesis_daily.py  # 默认最近一个有情绪数据的交易日

口径：
  * 所有情绪、K 线、收益和均线都硬截断到 as_of，禁止当前快照结算历史；
  * H1 使用手工登记的 11-23 入场、11-24 结算样本，从对应日缓存 OHLC 结算；
  * H3/H5 使用目标日 close / 前一交易日 close，指数基准同样从缓存计算；
  * H1/H4 的股票与入场价仍是手工样本配置，不代表自动信号识别结果。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics
import sys

BASE = Path(__file__).resolve().parents[2]

# 手工样本配置：复盘 owner 更新；代码不会自动推断入场价。
H1_ENTRY_DATE = "2026-11-23"
H1_SETTLE_DATE = "2026-11-24"
H1_SAMPLE = {"300972": "万辰集团", "600800": "渤海化学",
             "300903": "科翔股份", "603217": "元利科技"}
H2_CODES = ("600629", "688708", "300534")
H2_ENTRY_DATES = ("2026-11-11", "2026-11-12", "2026-11-13")
H4_ENTRY = {"300508": 43.7824, "300354": 28.4233}


def J(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def cache_path(code, base=None):
    root = Path(base or BASE) / "t_io" / "cache" / "daily_kline"
    c = str(code).split("_")[0]
    if c[:2].lower() in ("sh", "sz", "bj") and c[2:].isdigit():
        return root / f"index_{c.lower()}.json"
    return root / f"{c}.json"


def cache_bars(code, as_of=None, base=None):
    """缓存日线按日期排序、排除 forming 与 as_of 之后的数据；失败返回空列表。"""
    fp = cache_path(code, base)
    if not fp.exists():
        return []
    try:
        payload = J(fp)
        forming = {str(x)[:10] for x in (payload.get("forming") or [])}
        rows = []
        for row in payload.get("rows") or []:
            day = str((row or {}).get("date") or "")[:10]
            if len(day) != 10 or day in forming or (as_of and day > str(as_of)[:10]):
                continue
            close = float(row.get("close") or 0)
            if close > 0:
                rows.append(dict(row, date=day, close=close))
        return sorted(rows, key=lambda r: r["date"])
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return []


def cache_bar(code, date, base=None):
    day = str(date)[:10]
    return next((r for r in cache_bars(code, day, base) if r["date"] == day), None)


def sent_series(as_of, base=None):
    """60 日滚动分位→5 日均→加权；先截断历史，再计算，避免未来数据渗入。"""
    root = Path(base or BASE)
    try:
        e5 = J(root / "t_io" / "index_regime" / "e5_ths_cache.json")
    except (OSError, json.JSONDecodeError):
        e5 = {}
    try:
        loc = J(root / "t_io" / "cache" / "sentiment_local.json").get("series") or {}
    except (OSError, json.JSONDecodeError):
        loc = {}
    merged = {}
    for d, v in loc.items():
        if int(v.get("n") or 0) < 2000:
            continue
        d = str(d)
        d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d[:10]
        if d <= as_of:
            merged[d] = {"date": d, "zt": int(v.get("zt") or 0),
                         "dt": int(v.get("dt") or 0), "touch": v.get("touch"),
                         "zb_rate": v.get("zb_rate")}
    for k, v in e5.items():
        if v.get("zt_count") is None or v.get("dt_count") is None:
            continue
        k = str(k)
        d = f"{k[:4]}-{k[4:6]}-{k[6:]}" if len(k) == 8 else k[:10]
        if d <= as_of:
            merged[d] = {"date": d, "zt": v["zt_count"], "dt": v["dt_count"],
                         "touch": v.get("zt_touched"), "zb_rate": v.get("zb_rate")}
    rows = [merged[d] for d in sorted(merged)]

    def roll_pct(vals, w=60, min_n=20):
        out = []
        for j, v in enumerate(vals):
            seg = [x for x in vals[max(0, j - w + 1):j + 1] if x is not None]
            out.append(round(sum(1 for x in seg if x <= v) / len(seg), 4)
                       if v is not None and len(seg) >= min_n else None)
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
        r["sent"] = (round(100 * (0.5 * pt[j] + 0.3 * (1 - pz[j]) + 0.2 * (1 - pdt[j])), 1)
                     if None not in (pt[j], pz[j], pdt[j]) else None)
        r["sent_ma"] = (round(100 * (0.5 * ptm[j] + 0.3 * (1 - pzm[j]) + 0.2 * (1 - pdm[j])), 1)
                        if None not in (ptm[j], pzm[j], pdm[j]) else None)
    sv = [r["sent_ma"] for r in rows]
    for j, r in enumerate(rows):
        r["slope5"] = (round((sv[j] - sv[j - 5]) / 5, 2)
                       if j >= 5 and sv[j] is not None and sv[j - 5] is not None else None)
        v = r["sent_ma"]
        r["zone"] = None if v is None else ("弱势" if v < 40 else ("平衡" if v < 60 else "强势"))
    return rows


def _segments(vals, cond):
    segs, start = [], None
    for j, value in enumerate(vals):
        if value is not None and cond(value):
            if start is None:
                start = j
        elif start is not None:
            segs.append((start, j - 1))
            start = None
    if start is not None:
        segs.append((start, len(vals) - 1))
    return segs


def scan_triggers(rows):
    out = {k: [] for k in ("H1-a", "H1-b", "H2-a", "H2-b", "H1-s", "H2-s60", "H2-s70")}
    sv = [r["sent_ma"] for r in rows]
    for a, b in _segments(sv, lambda v: v <= 25):
        for j in range(a + 1, b + 2):
            if (j < len(sv) and sv[j] is not None and sv[j - 1] is not None
                    and sv[j] > sv[j - 1]):
                out["H1-b"].append(rows[j]["date"])
                break
    for a, b in _segments(sv, lambda v: v >= 70):
        peak = max(range(a, b + 1), key=lambda j: sv[j])
        if peak < b:
            out["H2-b"].append(rows[peak + 1]["date"])
        elif b + 1 < len(sv) and sv[b + 1] is not None and sv[b + 1] < sv[b]:
            out["H2-b"].append(rows[b + 1]["date"])
    for j in range(1, len(rows)):
        a, b = rows[j - 1], rows[j]
        if a["sent_ma"] is not None and b["sent_ma"] is not None:
            if (a["sent_ma"] <= 20 and
                    (b["sent_ma"] - a["sent_ma"] >= 5 or
                     ((b.get("slope5") or 0) > 0 and (a.get("slope5") or 0) > 0))):
                out["H1-a"].append(b["date"])
            if a["sent_ma"] >= 70 and (b.get("slope5") or 0) < 0 and (a.get("slope5") or 0) >= 0:
                out["H2-a"].append(b["date"])
        if a.get("slope5") is None or b.get("slope5") is None:
            continue
        recent = [v for v in (r.get("sent_ma") for r in rows[max(0, j - 5):j + 1]) if v is not None]
        if not recent:
            continue
        if a["slope5"] < 0 < b["slope5"] and min(recent) <= 25:
            out["H1-s"].append(b["date"])
        if a["slope5"] > 0 > b["slope5"]:
            if max(recent) >= 60:
                out["H2-s60"].append(b["date"])
            if max(recent) >= 70:
                out["H2-s70"].append(b["date"])
    return out


def session_return(code, date, base=None):
    """目标日 close/前一缓存交易日 close - 1；缺目标日或昨收返回 None。"""
    rows = cache_bars(code, date, base)
    for j, row in enumerate(rows):
        if row["date"] == date:
            if j == 0 or rows[j - 1]["close"] <= 0:
                return None
            return row["close"] / rows[j - 1]["close"] - 1
    return None


def perf(codes, label, as_of, base=None):
    pairs = [(c, session_return(c, as_of, base)) for c in codes]
    pcts = [r * 100 for _, r in pairs if r is not None]
    missing = [c for c, r in pairs if r is None]
    if not pcts:
        print(f"  {label}: 无 {as_of} 完整缓存数据（缺 {len(missing)} 只）")
        return None
    up = sum(1 for p in pcts if p > 0)
    result = {"n": len(pcts), "missing": len(missing),
              "mean": statistics.mean(pcts), "median": statistics.median(pcts),
              "up": up / len(pcts)}
    print(f"  {label}: n={result['n']} 缺={result['missing']} | 均 {result['mean']:+.2f}% | "
          f"中位 {result['median']:+.2f}% | 上涨占比 {result['up'] * 100:.0f}%")
    return result


def h1_settlement(as_of, base=None):
    print(f"\n=== 1. H1 手工样本（{H1_ENTRY_DATE} 开盘假想买入 → {H1_SETTLE_DATE} 缓存结算）===")
    if as_of < H1_SETTLE_DATE:
        print(f"  as_of={as_of} 尚未到结算日，保持 pending，不读未来行情")
        return []
    results = []
    for code, name in H1_SAMPLE.items():
        entry = cache_bar(code, H1_ENTRY_DATE, base)
        settle = cache_bar(code, H1_SETTLE_DATE, base)
        try:
            buy = float(entry.get("open"))
            high = float(settle.get("high"))
            close = float(settle.get("close"))
            if min(buy, high, close) <= 0:
                raise ValueError("non-positive OHLC")
        except (AttributeError, TypeError, ValueError):
            print(f"  {code} {name}: 缺目标日完整 OHLC（entry={bool(entry)}, settle={bool(settle)}）")
            continue
        gmax, gclose = high / buy - 1, close / buy - 1
        pop = high >= buy * 1.03
        results.append({"code": code, "max_gain": gmax, "close_gain": gclose, "pop": pop})
        print(f"  {code} {name}: 买{buy:.2f} → 高{high:.2f} 收{close:.2f} | "
              f"最大 {gmax * 100:+.1f}% | 收盘 {gclose * 100:+.1f}% | pop {'✅' if pop else '❌'}")
    if results:
        bench = session_return("sh000300", H1_SETTLE_DATE, base)
        bench_s = f"{bench * 100:+.2f}%" if bench is not None else "无缓存"
        print(f"  小结: pop {sum(r['pop'] for r in results)}/{len(results)} | "
              f"平均最大 {statistics.mean(r['max_gain'] for r in results) * 100:+.1f}% | "
              f"沪深300 {bench_s}")
    return results


def h2_samples(as_of, base=None):
    root = Path(base or BASE)
    print("\n=== 2. H2 已结算样本（极热降温段入场 → as-of 截断）===")
    entries = {}
    for day in H2_ENTRY_DATES:
        if day > as_of:
            continue
        fp = root / "t_io" / "state" / "weak_strong_book" / f"fills_{day}.json"
        if not fp.exists():
            continue
        try:
            payload = J(fp)
        except json.JSONDecodeError:
            continue
        items = payload if isinstance(payload, list) else (payload.get("fills") or payload.get("records") or [])
        for event in items:
            code = str(event.get("code") or "")
            price = event.get("price") or event.get("fill_vwap") or event.get("vwap")
            if code in H2_CODES and str(event.get("side") or "BUY").upper() == "BUY" and price:
                entries[code] = {"date": day, "price": float(price)}
    print("  入场记录:", json.dumps(entries, ensure_ascii=False))
    results = []
    for code in H2_CODES:
        entry = entries.get(code)
        if not entry:
            print(f"  {code}: 缺入场记录")
            continue
        bars = [r for r in cache_bars(code, as_of, base) if r["date"] >= entry["date"]][:7]
        try:
            mfe = max(float(r["high"]) for r in bars) / entry["price"] - 1
            mae = min(float(r["low"]) for r in bars) / entry["price"] - 1
            last = bars[-1]
        except (IndexError, TypeError, ValueError, KeyError):
            print(f"  {code}: 缺 as-of 前完整 K 线")
            continue
        ret = last["close"] / entry["price"] - 1
        results.append({"code": code, "mfe": mfe, "mae": mae, "ret": ret, "last": last["date"]})
        print(f"  {code}: 入场 {entry['date']}@{entry['price']:.2f} → {len(bars)} 根K线 | "
              f"MFE {mfe * 100:+.1f}% | MAE {mae * 100:+.1f}% | "
              f"末日({last['date']}) {ret * 100:+.1f}% | 曾触+3% {'是' if mfe >= 0.03 else '否'}")
    return results


def mas(code, as_of, base=None):
    rows = cache_bars(code, as_of, base)
    if not rows:
        return {}, None, None
    closes = [r["close"] for r in rows]
    return ({n: round(sum(closes[-n:]) / n, 2) for n in (5, 21, 34, 55)
             if len(closes) >= n}, closes[-1], rows[-1]["date"])


def run(as_of, base=None):
    as_of = str(as_of)[:10]
    root = Path(base or BASE)
    print(f"数据时点 as_of={as_of}；全部结果仅使用该日及此前缓存，不访问当前快照")
    rows = sent_series(as_of, root)
    print("=== 0. 情绪温度序列（近 15 个有效交易日）===")
    print(f"{'日期':<12}{'sent_ma':>8}{'slope5':>8}{'zone':>6}{'zt':>5}{'dt':>4}{'touch':>6}{'zb%':>7}")
    for r in rows[-15:]:
        zb = f"{r['zb_rate'] * 100:.1f}" if r.get("zb_rate") is not None else "—"
        print(f"{r['date']:<12}{r['sent_ma']!s:>8}{r['slope5']!s:>8}{r['zone']!s:>6}"
              f"{r['zt']:>5}{r['dt']:>4}{r['touch']!s:>6}{zb:>7}")
    if not rows or rows[-1]["date"] != as_of:
        print(f"  ⚠️ 情绪序列末端={rows[-1]['date'] if rows else '无'}，目标日={as_of}；不伪造当日温度")
    triggers = scan_triggers(rows)
    for name, days in triggers.items():
        print(f"{name}: {len(days)} 天，近6次 {days[-6:]}")

    h1_settlement(as_of, root)
    h2_samples(as_of, root)

    print("\n=== 3. H3 上轮主线板块目标日表现 ===")
    try:
        watchlist = J(root / "stock_hunter" / "watchlist_concept.json")
    except (OSError, json.JSONDecodeError):
        watchlist = {}

    def bucket(pred):
        return {k: v.get("name") for k, v in watchlist.items()
                if isinstance(v, dict) and pred(v.get("category") or "", v.get("concept") or "")}

    b_robot = bucket(lambda c, s: "机器人" in c)
    b_phys = bucket(lambda c, s: "物理AI" in c or "物理AI" in s)
    b_film = bucket(lambda c, s: "AI影视" in c or "AI影视" in s)
    tech_names = ("半导体", "算力", "AI应用", "机器人", "光通信", "液冷", "PCB", "超节点",
                  "物理AI", "消费电子", "商业航天", "低空经济", "光刻")
    def_names = ("电力", "煤炭")
    b_tech = bucket(lambda c, s: any(t in c for t in tech_names))
    b_def = bucket(lambda c, s: any(t in c for t in def_names)
                   or ("金融" in c and "银行" in s)
                   or ("大消费" in c and ("家电" in s or "白酒" in s))
                   or "石油" in c or "高速" in s)
    perf(b_robot, f"机器人({len(b_robot)}只)", as_of, root)
    perf(b_phys, f"物理AI({len(b_phys)}只)", as_of, root)
    perf(b_film, f"AI影视({len(b_film)}只)", as_of, root)
    print("\n=== 4. H5 风格篮子目标日表现 ===")
    tech = perf(b_tech, f"科技进攻篮子({len(b_tech)}只)", as_of, root)
    defensive = perf(b_def, f"红利防守篮子({len(b_def)}只)", as_of, root)
    if tech and defensive:
        print(f"  科技-防守均值差 {tech['mean'] - defensive['mean']:+.2f}pp")
    benchmarks = []
    for code, name in (("sh000300", "沪深300"), ("sz399006", "创业板"), ("sh000688", "科创50")):
        value = session_return(code, as_of, root)
        benchmarks.append(f"{name} {value * 100:+.2f}%" if value is not None else f"{name} 无缓存")
    print("  指数对照（缓存计算）:", " / ".join(benchmarks))

    print("\n=== 5. H4 手工活体样本压力位距离 ===")
    for code, entry_price in H4_ENTRY.items():
        ma, close, day = mas(code, as_of, root)
        if close is None:
            print(f"  {code}: 无 {as_of} 前有效缓存")
            continue
        above = {n: v for n, v in ma.items() if v > close}
        nearest = min(above.items(), key=lambda kv: kv[1]) if above else None
        pressure = (f"MA{nearest[0]}={nearest[1]}(距离{(nearest[1] / close - 1) * 100:+.1f}%)"
                    if nearest else "上方无均线压力")
        atk5 = "✅站上" if close > ma.get(5, 1e18) else "❌未站上"
        pop_line = entry_price * 1.03
        relation = "H4更高" if nearest and nearest[1] > pop_line else "pop先到"
        print(f"  {code}: 收{close}({day}) | MA5={ma.get(5)}({atk5}MA5) "
              f"MA21={ma.get(21)} MA34={ma.get(34)} MA55={ma.get(55)}")
        print(f"       最近压力: {pressure} | OSC pop={pop_line:.2f} | {relation}")
    print("\n完成。")


def main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="历史时点 YYYY-MM-DD；缺省用最近有情绪数据的交易日")
    args = parser.parse_args(argv)
    if args.date:
        as_of = pd_date(args.date)
    else:
        today = datetime.now().strftime("%Y-%m-%d")
        rows = sent_series(today)
        as_of = rows[-1]["date"] if rows else today
    run(as_of)


def pd_date(value):
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit(f"--date 必须为 YYYY-MM-DD：{value}") from exc


if __name__ == "__main__":
    main()
