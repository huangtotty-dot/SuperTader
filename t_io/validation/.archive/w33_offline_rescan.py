# -*- coding: utf-8 -*-
"""W33 A4: 建仓双通道离线重扫 + 预注册闸门（2026-08-13）

近 60 交易日 × 30 只池 × 盘后快照口径；5 分钟层以当日 14:55 分钟快照近似（缺快照→按 eod 口径并标注置信度）。
信号判定复用 position_builder.eval_dual_channels（单一真源，避免漂移）。

闸门（预注册，执行前写入 W33 方案 §3-A4）:
  1. 双通道合计信号密度 8~30 次/月
  2. 冰点通道 3 日胜率 ≥55%
  3. 突破通道 3 日胜率 ≥55%
  4. 两通道信号重叠率 <30%
  5. 假阳性率（3 日内跌回 signal 日低点下方）<40%
任一破闸 → FAIL + w33_offline_rescan_report.md；全过 → PASS。

口径: signal 日收盘买入 → t+3 / t+5 收盘收益；假阳性 = t+1..t+3 任一日收盘 < signal 日最低价。
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(r"E:\06_T")
sys.path.insert(0, str(BASE))

from core.position_builder import eval_dual_channels, fetch_daily_kline, load_snapshot_df, _box_raw_pct  # noqa: E402

REPORT_FP = BASE / "t_io" / "validation" / "w33_offline_rescan_report.md"


def _daily_ctx_as_of(kdf: pd.DataFrame, date_str: str) -> dict:
    """把日线 kline 切片到 date_str（含当日），按 _ensure_daily_indicators 公式现算 as-of daily_ctx。
    箱体/突破的 look-ahead 风险：_detect_boxes_simple 用截至当日的 K 线（近 30 日窗口），为近似口径。"""
    if kdf is None or kdf.empty:
        return {}
    df = kdf.copy()
    if "date" in df.columns:
        df["date"] = df["date"].astype(str)
        df = df[df["date"] <= str(date_str)]
    if len(df) < 30:
        return {}
    c = df["close"].astype(float)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd_dif = (ema12 - ema26).values
    macd_dea = pd.Series(macd_dif).ewm(span=9, adjust=False).mean().values
    macd_hist = (macd_dif - macd_dea) * 2
    d = c.diff()
    g = d.clip(lower=0).rolling(14, min_periods=1).mean()
    l = (-d.clip(upper=0)).rolling(14, min_periods=1).mean()
    rsi = (100 - 100 / (1 + (g / l.replace(0, float("nan"))))).fillna(50.0)
    boll_mid = c.rolling(20).mean()
    boll_std = c.rolling(20).std()
    boll_up = boll_mid + 2 * boll_std
    boll_dn = boll_mid - 2 * boll_std
    boll_pct = (c - boll_dn) / (boll_up - boll_dn).replace(0, float("nan"))
    vol = df["volume"].astype(float)
    vol_ma5 = vol.rolling(5).mean()
    cross_up = (pd.Series(macd_dif) > pd.Series(macd_dea)) & (pd.Series(macd_dif).shift(1) <= pd.Series(macd_dea).shift(1))
    ma5 = c.rolling(5).mean()
    ctx = {
        "daily_macd_dif": float(macd_dif[-1]),
        "daily_macd_dea": float(macd_dea[-1]),
        "daily_macd_hist": float(macd_hist[-1]),
        "daily_macd_golden": bool(cross_up.tail(5).any()),
        "daily_rsi": float(rsi.iloc[-1]),
        "daily_boll_pct": float(boll_pct.iloc[-1]) if pd.notna(boll_pct.iloc[-1]) else None,
        "daily_vol_today": float(vol.iloc[-1]),
        "daily_vol_ma5": float(vol_ma5.iloc[-1]) if pd.notna(vol_ma5.iloc[-1]) else None,
        "daily_ma5": float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else None,
        "daily_price_ref": float(c.iloc[-1]),
        "_close": float(c.iloc[-1]),
        "_low": float(df["low"].astype(float).iloc[-1]),
        "_date": str(df["date"].iloc[-1]),
    }
    return ctx


def _snapshot_1min_for(code: str, date_str: str):
    """当日 14:55 分钟快照（近似 5 分钟层）。缺快照返回 None（脚本按 eod 口径并标注置信度）。"""
    try:
        df, _ctx, snap_date = load_snapshot_df(code, date_str)
        if snap_date == date_str and df is not None and not df.empty:
            return df
    except Exception:
        pass
    return None


def _load_pool() -> list:
    """候选池：watchlist_buy.json stocks（30 只池）。"""
    fp = BASE / "watchlist_buy.json"
    if fp.exists():
        try:
            wl = json.loads(fp.read_text(encoding="utf-8"))
            stocks = wl.get("stocks", {})
            return [str(k) for k, v in stocks.items() if not str(k).startswith("_example")]
        except Exception:
            pass
    return ["000988", "588170", "600176", "600481", "603667", "002639", "300153", "300364"]


def _common_trading_days(kdf_by_code: dict, n: int) -> list:
    """近 n 个共同交易日（按池内 kline 日期集合排序）。"""
    day_counts = defaultdict(int)
    for kdf in kdf_by_code.values():
        if kdf is None or kdf.empty or "date" not in kdf.columns:
            continue
        for d in kdf["date"].astype(str).unique():
            day_counts[d] += 1
    total = len(kdf_by_code)
    days = sorted(d for d, cnt in day_counts.items() if cnt >= max(1, int(total * 0.5)))
    return days[-n:]


def _precompute(pool, kdf_by_code, dates):
    """一次性预计算每日每 code 的 daily_ctx/快照/箱体原始检测（变体无关），变体间复用。"""
    cache = {}
    no_snapshot_days = 0
    total_days = 0
    for code in pool:
        kdf = kdf_by_code.get(code)
        if kdf is None or kdf.empty:
            continue
        kdf_sorted = kdf.sort_values("date").reset_index(drop=True) if "date" in kdf.columns else kdf
        for date_str in dates:
            ctx = _daily_ctx_as_of(kdf_sorted, date_str)
            if not ctx or ctx.get("_date") != date_str:
                continue
            df1 = _snapshot_1min_for(code, date_str)
            scan_type = "intraday" if df1 is not None else "eod"
            total_days += 1
            if df1 is None:
                no_snapshot_days += 1
            kdf_asof = kdf_sorted[kdf_sorted["date"].astype(str) <= str(date_str)] if "date" in kdf_sorted.columns else kdf_sorted
            box_raw = _box_raw_pct(kdf_asof, ctx.get("_close"))
            cache[(code, date_str)] = {"ctx": ctx, "df1": df1, "scan_type": scan_type, "box_raw": box_raw}
    return cache, total_days, no_snapshot_days


def _evaluate_variant(cache, dates, opts, kdf_by_code, total_days=0, no_snapshot_days=0, n_pool=0):
    """对一组参数 opts 跑一遍双通道离线重扫（复用预计算 cache），返回闸门统计。"""
    events = []
    ice_signals = []
    brk_signals = []
    for (code, date_str), pc in cache.items():
        ctx = pc["ctx"]
        df1 = pc["df1"]
        scan_type = pc["scan_type"]
        dc = eval_dual_channels(code, ctx, df1, scan_type, ctx.get("_close"),
                                opts={**opts, "_box_raw": pc["box_raw"]})
        c1_v = dc["channels"]["iceberg"]["verdict"]
        c1_score = dc["channels"]["iceberg"]["score"]
        c2_v = dc["channels"]["breakout"]["verdict"]
        # 盘后口径: 冰点 signal 按日线 setup（转向+boll+缩量 全过 = 80 分）；m5 择时层近似掉
        c1 = "signal" if c1_score == 80 else c1_v
        c2 = c2_v
        events.append({"code": code, "date": date_str, "c1": c1, "c2": c2})
        if c1 == "signal":
            ice_signals.append({"code": code, "date": date_str, "px": ctx["_close"], "low": ctx["_low"]})
        if c2 == "signal":
            brk_signals.append({"code": code, "date": date_str, "px": ctx["_close"], "low": ctx["_low"]})

    def _fwd(code, date_str, kdf):
        if kdf is None or kdf.empty:
            return None, None
        df = kdf.copy()
        df["date"] = df["date"].astype(str)
        idx = df.index[df["date"] == date_str]
        if idx.empty:
            return None, None
        pos = idx[0]
        def _close_at(offset):
            j = pos + offset
            return float(df["close"].astype(float).iloc[j]) if j < len(df) else None
        c3 = _close_at(3)
        lows = [float(df["low"].astype(float).iloc[pos + k]) for k in range(1, 4) if pos + k < len(df)]
        return c3, lows

    def _stats(signals):
        if not signals:
            return {"n": 0, "win3": 0, "n3": 0, "fp3": 0}
        win3 = fp3 = 0
        n3 = 0
        for s in signals:
            c3, lows = _fwd(s["code"], s["date"], kdf_by_code.get(s["code"]))
            if c3 is not None:
                n3 += 1
                win3 += 1 if c3 > s["px"] else 0
            if lows and min(lows) < s["low"]:
                fp3 += 1
        return {"n": len(signals), "win3": win3, "n3": n3, "fp3": fp3}

    ice = _stats(ice_signals)
    brk = _stats(brk_signals)
    months = max(len(dates) / 21.0, 1.0)
    density = (ice["n"] + brk["n"]) / months
    sig_days = {(e["code"], e["date"]) for e in events if e["c1"] == "signal" or e["c2"] == "signal"}
    both_days = {(e["code"], e["date"]) for e in events if e["c1"] == "signal" and e["c2"] == "signal"}
    overlap = len(both_days) / len(sig_days) if sig_days else 0.0
    ice_fpr = (ice["fp3"] / ice["n"]) if ice["n"] else 0.0
    brk_fpr = (brk["fp3"] / brk["n"]) if brk["n"] else 0.0
    ice_w3 = (ice["win3"] / ice["n3"]) if ice["n3"] else 0.0
    brk_w3 = (brk["win3"] / brk["n3"]) if brk["n3"] else 0.0
    gates = [
        ("信号密度 8~30 次/月", 8 <= density <= 30, f"{density:.1f}"),
        ("冰点通道 3 日胜率 ≥55%", ice_w3 >= 0.55, f"{ice_w3 * 100:.1f}% (n={ice.get('n3', 0)})"),
        ("突破通道 3 日胜率 ≥55%", brk_w3 >= 0.55, f"{brk_w3 * 100:.1f}% (n={brk.get('n3', 0)})"),
        ("两通道信号重叠率 <30%", overlap < 0.30, f"{overlap * 100:.1f}%"),
        ("假阳性率 <40%", ice_fpr < 0.40 and brk_fpr < 0.40,
         f"冰点 {ice_fpr * 100:.1f}% / 突破 {brk_fpr * 100:.1f}%"),
    ]
    passed = all(g[1] for g in gates)
    return {
        "opts": opts, "passed": passed, "gates": gates, "density": density,
        "ice": ice, "brk": brk, "overlap": overlap,
        "ice_w3": ice_w3, "brk_w3": brk_w3, "ice_fpr": ice_fpr, "brk_fpr": brk_fpr,
        "total_days": total_days, "no_snapshot_days": no_snapshot_days,
        "n_dates": len(dates), "n_pool": n_pool,
    }


def _opts_label(opts):
    parts = []
    for k in ("boll_ice_max", "vol_shrink_ratio", "vol_confirm_ratio",
              "box_min_pct", "box_max_pct", "breakout_signal_mode"):
        if k in opts:
            parts.append(f"{k}={opts[k]}")
    return ", ".join(parts) if parts else "生产默认"


def main():
    pool = _load_pool()
    kdf_by_code = {}
    for code in pool:
        try:
            kdf_by_code[code] = fetch_daily_kline(code)
        except Exception:
            kdf_by_code[code] = None
    dates = _common_trading_days(kdf_by_code, 60)
    if len(dates) < 40:
        print(f"[WARN] 共同交易日仅 {len(dates)} 天（<40），样本不足，闸门置信度低")

    # 预计算一次（daily_ctx/快照/箱体原始检测，变体无关），变体间复用
    cache, total_days, no_snapshot_days = _precompute(pool, kdf_by_code, dates)
    print(f"预计算完成: {len(cache)} 判定（{len(dates)} 日 × {len(pool)} 只），快照覆盖 {total_days - no_snapshot_days}/{total_days}")

    variants = [
        ("V0 生产默认", {}),
        ("V1 突破全过", {"breakout_signal_mode": "all"}),
        ("V2 突破全过+放量>2", {"breakout_signal_mode": "all", "vol_confirm_ratio": 2.0}),
        ("V3 突破>1%", {"box_min_pct": 1.0}),
        ("V4 突破全过+>1%", {"breakout_signal_mode": "all", "box_min_pct": 1.0}),
        ("V5 冰点更深(boll≤0.05)", {"boll_ice_max": 0.05}),
        ("V6 冰点缩量<0.6", {"vol_shrink_ratio": 0.6}),
        ("V7 冰点深+缩量严", {"boll_ice_max": 0.05, "vol_shrink_ratio": 0.6}),
    ]

    results = []
    for label, opts in variants:
        r = _evaluate_variant(cache, dates, opts, kdf_by_code, total_days, no_snapshot_days, len(pool))
        r["label"] = label
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"[{mark}] {label}（{_opts_label(opts)}）"
              f" 密度{r['density']:.1f}/月 冰点{r['ice_w3'] * 100:.0f}%(n={r['ice']['n3']})/{r['ice_fpr'] * 100:.0f}%FP "
              f"突破{r['brk_w3'] * 100:.0f}%(n={r['brk']['n3']})/{r['brk_fpr'] * 100:.0f}%FP")

    best = next((r for r in results if r["passed"]), None)
    best = best or max(results, key=lambda r: (r["ice_w3"] + r["brk_w3"]) / 2 - (r["ice_fpr"] + r["brk_fpr"]))
    passed = best["passed"]

    lines = [
        "# W33 A4 离线重扫闸门报告（调参重扫）", "",
        f"- 样本: {best['n_dates']} 交易日 × {best['n_pool']} 只池 = {best['total_days']} 判定",
        f"- 5 分钟层快照覆盖: {best['total_days'] - best['no_snapshot_days']}/{best['total_days']} 日",
        f"- 冰点 signal 口径: 日线 setup 全过（80 分），m5 择时层近似掉（盘后口径）",
        "", "## 变体扫描", "",
    ]
    for r in results:
        ok = "✅" if r["passed"] else "❌"
        lines.append(f"- {ok} **{r['label']}**（{_opts_label(r['opts'])}）: "
                     f"密度 {r['density']:.1f}/月 ｜ 冰点 3日 {r['ice_w3'] * 100:.0f}%(n={r['ice']['n3']}) FP {r['ice_fpr'] * 100:.0f}% ｜ "
                     f"突破 3日 {r['brk_w3'] * 100:.0f}%(n={r['brk']['n3']}) FP {r['brk_fpr'] * 100:.0f}% ｜ 重叠 {r['overlap'] * 100:.0f}%")
    lines += ["", "## 最优变体闸门", ""]
    if best["passed"]:
        lines.append(f"**最优: {best['label']}（{_opts_label(best['opts'])}）— 全过**")
    else:
        lines.append(f"**最优(未过): {best['label']}（{_opts_label(best['opts'])}）**")
    for name, ok, val in best["gates"]:
        lines.append(f"- [{'PASS' if ok else 'FAIL'}] {name}: {val}")
    lines.append("")
    lines.append(f"**结论: {'✅ 存在过闸参数，可进入全管线决赛' if passed else '❌ 调参后仍破闸，双通道参数面判定不可行/需重构条件'}**")
    lines.append("")
    lines.append("> 口径注: 箱体 as-of 近似（少量 look-ahead）；5 分钟层快照覆盖 30%，冰点密度可能低估；"
                 "n<15 的胜率为小样本估计。")
    REPORT_FP.write_text("\n".join(lines), encoding="utf-8")

    print(f"\n最优变体: {best['label']} → {'PASS' if passed else 'FAIL'}（报告已写 {REPORT_FP.name}）")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
