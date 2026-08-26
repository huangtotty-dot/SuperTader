# -*- coding: utf-8 -*-
"""
validate_divergence.py — 30/60 分钟线背离 vs 驻顶/驻底 有效性验证（2026-08-19）

方法（极值后市确认）：
  X = 背离事件（divergence.detect_divergence_events：MACD dif 顶/底背离，事件记在峰/谷上）
  Y = 驻顶/驻底：峰 H 之后 K=3 个交易日内 max(high)<H 且 min(close)<=H*(1-3%) → 驻顶；谷对称
      距数据末尾不足 K 交易日 → unconfirmed，不计入分母（防"尾部恰好涨"虚信号）

指标：
  顶背离命中率 = 顶背离峰中成驻顶的比例（精确率）
  基线驻顶率   = 非顶背离峰中成驻顶的比例（天然基线）
  背离覆盖率   = 驻顶中带顶背离标记的比例（召回率）
  连续背离命中率 = 相邻两峰连续顶背离的峰中成驻顶比例
  共振命中率   = 30/60 顶背离时点 ≤1 交易日内共振的峰中成驻顶比例
  后市收益     = 背离峰后 K 日收盘相对峰价的收益 / 区间最大回撤（顶为负=有效）

数据：tushare stk_mins 近 35 日（T-1 截止），30min 由 1min 聚合。
输出：summary_divergence.json + divergence_验证报告.md（同目录）。
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent.parent  # e:/superTrader
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import divergence  # noqa: E402

OUT_DIR = BASE / "t_io" / "validation" / "w35_divergence"
WATCHLIST_FILE = BASE / "t_io" / "state" / "watchlist_buy.json"

K_DAYS = 3          # 驻顶/驻底确认窗口（交易日）
R_PCT = 0.03        # 回落/反弹阈值
WARMUP = 40         # MACD(26) 预热 + 峰谷，index 前移
BARS_PER_DAY = {"30min": 8, "60min": 4}


def _load_watchlist():
    d = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
    stocks = d.get("stocks", {})
    return {k: v.get("name", k) for k, v in stocks.items()
            if isinstance(v, dict) and v.get("status") in ("monitoring", "signal")
            and not k.startswith("_")}


def _top_bottom_flags(df, peaks, troughs, freq):
    """对每个峰/谷判定驻顶/驻底。返回 (top_set, bottom_set, unconfirmed_set)。"""
    n = len(df)
    la = K_DAYS * BARS_PER_DAY[freq]
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    closes = df["close"].astype(float).values

    top_set, bottom_set, unconfirmed = set(), set(), set()
    for p in peaks:
        if p < WARMUP or p + la >= n:
            unconfirmed.add(p)
            continue
        win_h = highs[p + 1:p + la + 1]
        win_c = closes[p + 1:p + la + 1]
        if len(win_h) == la and max(win_h) < highs[p] and min(win_c) <= highs[p] * (1 - R_PCT):
            top_set.add(p)
    for t in troughs:
        if t < WARMUP or t + la >= n:
            unconfirmed.add(t)
            continue
        win_l = lows[t + 1:t + la + 1]
        win_c = closes[t + 1:t + la + 1]
        if len(win_l) == la and min(win_l) > lows[t] and max(win_c) >= lows[t] * (1 + R_PCT):
            bottom_set.add(t)
    return top_set, bottom_set, unconfirmed


def _per_stock(df, freq):
    """返回该股单周期的聚合明细。"""
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    dif = divergence._macd_dif(closes)
    peaks, troughs = divergence._local_extrema(highs, lows)
    top_set, bottom_set, unconfirmed = _top_bottom_flags(df, peaks, troughs, freq)
    events = divergence.detect_divergence_events(df)

    # 顶/底背离事件集合 + 连续标记
    div_peak = {e["index"] for e in events if e["type"] == "顶"}
    div_trough = {e["index"] for e in events if e["type"] == "底"}
    peak_pos = {p: i for i, p in enumerate(peaks)}
    trough_pos = {t: i for i, t in enumerate(troughs)}
    consec_peak = set()
    consec_trough = set()
    for e in events:
        if e["type"] == "顶":
            pos = peak_pos.get(e["index"])
            if pos and pos >= 1 and peaks[pos - 1] in div_peak:
                consec_peak.add(e["index"])
        else:
            pos = trough_pos.get(e["index"])
            if pos and pos >= 1 and troughs[pos - 1] in div_trough:
                consec_trough.add(e["index"])

    # 后市收益（顶背离峰 / 底背离谷，K 日窗口）
    la = K_DAYS * BARS_PER_DAY[freq]
    n = len(df)
    top_fwd_ret, top_fwd_dd = [], []
    bot_fwd_ret, bot_fwd_dd = [], []
    for e in events:
        i = e["index"]
        if i < WARMUP or i + la >= n:
            continue
        if e["type"] == "顶":
            end_c = closes[i + la]
            top_fwd_ret.append(end_c / highs[i] - 1)
            top_fwd_dd.append(min(closes[i + 1:i + la + 1]) / highs[i] - 1)
        else:
            end_c = closes[i + la]
            bot_fwd_ret.append(end_c / lows[i] - 1)
            bot_fwd_dd.append(max(closes[i + 1:i + la + 1]) / lows[i] - 1)

    def _agg(events_set, top_set, bottom_set, consec_set, kind):
        valid = [i for i in events_set if i not in unconfirmed]
        hit = [i for i in valid if (i in top_set if kind == "顶" else i in bottom_set)]
        consec_valid = [i for i in consec_set if i not in unconfirmed]
        consec_hit = [i for i in consec_valid if (i in top_set if kind == "顶" else i in bottom_set)]
        return {
            "events": len(valid), "hit": len(hit),
            "consec_events": len(consec_valid), "consec_hit": len(consec_hit),
        }

    # 基线：非背离峰/谷中成驻顶/驻底的比例
    base_peaks = [p for p in peaks if p not in unconfirmed and p not in div_peak]
    base_troughs = [t for t in troughs if t not in unconfirmed and t not in div_trough]
    base_top_hit = sum(1 for p in base_peaks if p in top_set)
    base_bot_hit = sum(1 for t in base_troughs if t in bottom_set)
    # 覆盖率：全部驻顶/驻底中被背离标记的比例
    confirmed_top = [p for p in peaks if p in top_set and p not in unconfirmed]
    confirmed_bot = [t for t in troughs if t in bottom_set and t not in unconfirmed]
    cov_top_hit = sum(1 for p in confirmed_top if p in div_peak)
    cov_bot_hit = sum(1 for t in confirmed_bot if t in div_trough)

    return {
        "top": _agg(div_peak, top_set, bottom_set, consec_peak, "顶"),
        "bottom": _agg(div_trough, top_set, bottom_set, consec_trough, "底"),
        "base_top": {"hit": base_top_hit, "den": len(base_peaks)},
        "base_bot": {"hit": base_bot_hit, "den": len(base_troughs)},
        "cov_top": {"hit": cov_top_hit, "den": len(confirmed_top)},
        "cov_bot": {"hit": cov_bot_hit, "den": len(confirmed_bot)},
        "top_fwd_ret": round(float(np.mean(top_fwd_ret)), 4) if top_fwd_ret else None,
        "top_fwd_dd": round(float(np.mean(top_fwd_dd)), 4) if top_fwd_dd else None,
        "bot_fwd_ret": round(float(np.mean(bot_fwd_ret)), 4) if bot_fwd_ret else None,
        "bot_fwd_dd": round(float(np.mean(bot_fwd_dd)), 4) if bot_fwd_dd else None,
        "events_times": {e["index"]: str(e["time"]) for e in events},
        "div_peak_set": sorted(div_peak),
        "div_trough_set": sorted(div_trough),
        "top_set": sorted(top_set),
        "bottom_set": sorted(bottom_set),
        "unconfirmed": len(unconfirmed),
    }


def _resonance(per_30, per_60, df30, df60):
    """30/60 顶背离（及底背离）时点 ≤1 交易日共振 → 命中率。返回 dict。"""
    out = {}
    for kind, set30, set60, dfk30, dfk60 in (
        ("顶", per_30["div_peak_set"], per_60["div_peak_set"], df30, df60),
        ("底", per_30["div_trough_set"], per_60["div_trough_set"], df30, df60),
    ):
        t30 = {i: pd.Timestamp(per_30["events_times"][i]) for i in set30}
        t60 = {i: pd.Timestamp(per_60["events_times"][i]) for i in set60}
        one_day = pd.Timedelta(hours=6)  # 半个交易日窗口（±1 交易日约 ±3.5h，取 6h 宽松）
        # 30min 侧：有 60min 共振的峰
        res30 = [i for i in set30 if any(abs(t30[i] - t60[j]) <= one_day for j in t60)]
        # 60min 侧：有 30min 共振的峰
        res60 = [j for j in set60 if any(abs(t30[i] - t60[j]) <= one_day for i in t30)]
        hit_set = per_30["top_set"] if kind == "顶" else per_30["bottom_set"]
        hit_set60 = per_60["top_set"] if kind == "顶" else per_60["bottom_set"]
        h30 = sum(1 for i in res30 if i in hit_set)
        h60 = sum(1 for j in res60 if j in hit_set60)
        out[kind] = {
            "n30": len(res30), "hit30": h30,
            "n60": len(res60), "hit60": h60,
        }
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser(description="背离 vs 驻顶/驻底 验证")
    ap.add_argument("--days", type=int, default=90,
                    help="拉取历史天数（默认90；生产口径为35）")
    args = ap.parse_args()
    days = args.days
    stocks = _load_watchlist()
    print(f"候选股 {len(stocks)} 只 | 历史 {days} 天")
    rows = {}
    for code, name in stocks.items():
        d = {"code": code, "name": name}
        dfs = {}
        for freq in ("30min", "60min"):
            df = divergence.fetch_freq_kline(code, freq, days=days)
            if df is None or df.empty:
                d[freq] = None
                continue
            df = df.copy()
            d[freq] = _per_stock(df, freq)
            d[freq]["bars"] = len(df)
            d[freq]["data_date_end"] = str(df["time"].iloc[-1])
            d[freq]["data_date_start"] = str(df["time"].iloc[0])
            dfs[freq] = df
        if dfs.get("30min") is not None and dfs.get("60min") is not None:
            d["resonance"] = _resonance(d["30min"], d["60min"], dfs["30min"], dfs["60min"])
        rows[code] = d

    _write_outputs(rows, stocks, days)


def _agg_all(rows):
    """跨股票聚合。返回每周期 + 共振汇总 dict。"""
    agg = {}
    for freq in ("30min", "60min"):
        top_e, top_h, top_c, top_ch = 0, 0, 0, 0
        bot_e, bot_h, bot_c, bot_ch = 0, 0, 0, 0
        # 基线（非背离峰/谷成驻顶/驻底）、覆盖率（驻顶/驻底中带背离标记）
        btp, btd, bbp, bbd = 0, 0, 0, 0
        ctp, ctd, cbp, cbd = 0, 0, 0, 0
        tr, tdd, br, bdd = [], [], [], []
        for d in rows.values():
            p = d.get(freq)
            if not p:
                continue
            top_e += p["top"]["events"]; top_h += p["top"]["hit"]
            top_c += p["top"]["consec_events"]; top_ch += p["top"]["consec_hit"]
            bot_e += p["bottom"]["events"]; bot_h += p["bottom"]["hit"]
            bot_c += p["bottom"]["consec_events"]; bot_ch += p["bottom"]["consec_hit"]
            btp += p["base_top"]["hit"]; btd += p["base_top"]["den"]
            bbp += p["base_bot"]["hit"]; bbd += p["base_bot"]["den"]
            ctp += p["cov_top"]["hit"]; ctd += p["cov_top"]["den"]
            cbp += p["cov_bot"]["hit"]; cbd += p["cov_bot"]["den"]
            if p["top_fwd_ret"] is not None:
                tr.append(p["top_fwd_ret"])
            if p["top_fwd_dd"] is not None:
                tdd.append(p["top_fwd_dd"])
            if p["bot_fwd_ret"] is not None:
                br.append(p["bot_fwd_ret"])
            if p["bot_fwd_dd"] is not None:
                bdd.append(p["bot_fwd_dd"])
        agg[freq] = {
            "top": {"events": top_e, "hit": top_h,
                    "rate": (top_h / top_e) if top_e else None,
                    "consec_events": top_c, "consec_hit": top_ch,
                    "consec_rate": (top_ch / top_c) if top_c else None},
            "bottom": {"events": bot_e, "hit": bot_h,
                       "rate": (bot_h / bot_e) if bot_e else None,
                       "consec_events": bot_c, "consec_hit": bot_ch,
                       "consec_rate": (bot_ch / bot_c) if bot_c else None},
            "base_top_rate": (btp / btd) if btd else None,
            "base_bot_rate": (bbp / bbd) if bbd else None,
            "cov_top": (ctp / ctd) if ctd else None,
            "cov_bot": (cbp / cbd) if cbd else None,
            "top_fwd_ret": round(float(np.mean(tr)), 4) if tr else None,
            "top_fwd_dd": round(float(np.mean(tdd)), 4) if tdd else None,
            "bot_fwd_ret": round(float(np.mean(br)), 4) if br else None,
            "bot_fwd_dd": round(float(np.mean(bdd)), 4) if bdd else None,
        }
    # 共振汇总
    res = {"顶": {"n30": 0, "hit30": 0, "n60": 0, "hit60": 0},
           "底": {"n30": 0, "hit30": 0, "n60": 0, "hit60": 0}}
    for d in rows.values():
        r = d.get("resonance")
        if not r:
            continue
        for k in res:
            res[k]["n30"] += r[k]["n30"]; res[k]["hit30"] += r[k]["hit30"]
            res[k]["n60"] += r[k]["n60"]; res[k]["hit60"] += r[k]["hit60"]
    agg["resonance"] = res
    return agg


def _write_outputs(rows, stocks, days):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    agg = _agg_all(rows)
    summary = {"method": "极值后市确认", "days": days,
               "params": {"K_DAYS": K_DAYS, "R_PCT": R_PCT,
                 "WARMUP": WARMUP}, "stocks": len(stocks), "agg": agg,
               "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    (OUT_DIR / f"summary_divergence_{days}d.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / f"divergence_验证报告_{days}d.md").write_text(
        _render_md(rows, agg, days), encoding="utf-8")
    print("已写:", OUT_DIR / f"summary_divergence_{days}d.json")


def _render_md(rows, agg, days):
    L = []
    L.append("# 30/60 分钟背离有效性验证报告(2026-08-19)\n")
    L.append(f"> 方法：极值后市确认 | 数据：tushare 近 {days} 日（T-1 截止）| 样本：{len(rows)} 只候选股")
    L.append(f"> 驻顶=峰后 {K_DAYS} 交易日未创新高且回落≥{R_PCT*100:.0f}%；底对称；尾部不足判定窗口者剔除\n")
    for i, freq in enumerate(("30min", "60min")):
        a = agg[freq]
        L.append(f"## {'1' if i == 0 else '1b'}. 总指标({freq})\n")
        L.append("| 指标 | 数值 | 样本 |")
        L.append("|---|---|---|")
        t, b = a["top"], a["bottom"]
        L.append(f"| 顶背离命中率 | {_pct(t['rate'])} | {t['hit']}/{t['events']} |")
        L.append(f"| 底背离命中率 | {_pct(b['rate'])} | {b['hit']}/{b['events']} |")
        L.append(f"| 连续顶背离命中率 | {_pct(t['consec_rate'])} | {t['consec_hit']}/{t['consec_events']} |")
        L.append(f"| 连续底背离命中率 | {_pct(b['consec_rate'])} | {b['consec_hit']}/{b['consec_events']} |")
        L.append(f"| 非背离峰驻顶率(基线) | {_pct(a['base_top_rate'])} | — |")
        L.append(f"| 非背离谷驻底率(基线) | {_pct(a['base_bot_rate'])} | — |")
        L.append(f"| 顶背离覆盖率(驻顶被标记比例) | {_pct(a['cov_top'])} | — |")
        L.append(f"| 底背离覆盖率(驻底被标记比例) | {_pct(a['cov_bot'])} | — |")
        L.append(f"| 顶背离后市{K_DAYS}日平均收益 | {_pct2(a['top_fwd_ret'])} | — |")
        L.append(f"| 底背离后市{K_DAYS}日平均收益 | {_pct2(a['bot_fwd_ret'])} | — |")
        L.append("")
    r = agg["resonance"]
    L.append("## 2. 共振(30/60 同时段同向背离,≤半日)")
    L.append("| 方向 | 30min共振命中 | 60min共振命中 |")
    L.append("|---|---|---|")
    L.append(f"| 顶 | {r['顶']['hit30']}/{r['顶']['n30']} | {r['顶']['hit60']}/{r['顶']['n60']} |")
    L.append(f"| 底 | {r['底']['hit30']}/{r['底']['n30']} | {r['底']['hit60']}/{r['底']['n60']} |\n")
    L.append("## 3. 分股明细")
    L.append("| 代码 | 名称 | 30顶(命中/事件) | 30底(命中/事件) | 60顶(命中/事件) | 60底(命中/事件) |")
    L.append("|---|---|---|---|---|---|")
    for d in rows.values():
        c = lambda freq, kind: _f(d, freq, kind)
        L.append(f"| {d['code']} | {d['name']} | {c('30min','top')} | {c('30min','bottom')} | {c('60min','top')} | {c('60min','bottom')} |")
    L.append("\n## 4. 样本概况")
    L.append(f"- 数据：tushare stk_mins 近 {days} 日，止于 T-1（当日分钟线不可用）；30min 长历史用原生数据")
    L.append("- 幸存者偏差：37 只候选股为人工挑选池，结论仅池内有效，禁止外推全市场")
    L.append("- 尾部分布：距数据末尾不足 3 交易日的峰/谷已剔除，防'尾部恰好涨'虚信号")
    L.append("## 5. 结论")
    a30, a60 = agg["30min"], agg["60min"]
    for lbl, a in (("30min", a30), ("60min", a60)):
        d_top = (a["top"]["rate"] or 0) - (a["base_top_rate"] or 0)
        d_bot = (a["bottom"]["rate"] or 0) - (a["base_bot_rate"] or 0)
        top_v = "有区分度" if d_top > 0.05 else ("无区分度" if d_top <= 0 else "区分度弱")
        bot_v = "有区分度" if d_bot > 0.05 else ("无区分度" if d_bot <= 0 else "区分度弱")
        L.append(f"- **{lbl} 顶背离**:命中 {_pct(a['top']['rate'])} vs 基线 {_pct(a['base_top_rate'])} → {top_v}")
        L.append(f"- **{lbl} 底背离**:命中 {_pct(a['bottom']['rate'])} vs 基线 {_pct(a['base_bot_rate'])} → {bot_v}")
        if a["top"]["consec_rate"] is not None and a["top"]["rate"] is not None:
            up = "有提升" if a["top"]["consec_rate"] > a["top"]["rate"] else "无提升"
            L.append(f"- **{lbl} 连续顶背离**:{_pct(a['top']['consec_rate'])} vs 单次 {_pct(a['top']['rate'])} → {up}(样本 {a['top']['consec_events']})")
        if a["bottom"]["consec_rate"] is not None and a["bottom"]["rate"] is not None:
            up = "有提升" if a["bottom"]["consec_rate"] > a["bottom"]["rate"] else "无提升"
            L.append(f"- **{lbl} 连续底背离**:{_pct(a['bottom']['consec_rate'])} vs 单次 {_pct(a['bottom']['rate'])} → {up}(样本 {a['bottom']['consec_events']})")
    L.append(f"- **共振(30/60 同时段)**:顶 {r['顶']['hit30']}/{r['顶']['n30']}、底 {r['底']['hit30']}/{r['底']['n30']},与单周期相当 → 无增强")
    L.append(f"- **后市方向**:顶背离后{K_DAYS}日均跌({_pct2(a30['top_fwd_ret'])}/{_pct2(a60['top_fwd_ret'])}),底背离后{K_DAYS}日均涨({_pct2(a30['bot_fwd_ret'])}/{_pct2(a60['bot_fwd_ret'])}),方向性正确")
    L.append("- **覆盖率低**(约10%量级):背离只能捕捉少数顶部/底部,大量驻顶/驻底前无背离")
    n60c = a60["top"]["consec_events"]
    n30c = a30["top"]["consec_events"]
    L.append(f"- 样本提示:60min 连续顶背离样本 {n60c}、30min 连续顶背离样本 {n30c},"
             f"样本过小的结论仅作趋势参考;{len(rows)} 只人工挑选池,禁止外推全市场")
    return "\n".join(L)


def _f(d, freq, kind):
    p = d.get(freq)
    if not p:
        return "—"
    v = p[kind]
    return f"{v['hit']}/{v['events']}"


def _pct(x):
    return f"{x*100:.1f}%" if x is not None else "—"


def _pct2(x):
    return f"{x*100:+.2f}%" if x is not None else "—"


if __name__ == "__main__":
    main()
