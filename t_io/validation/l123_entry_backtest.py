# -*- coding: utf-8 -*-
"""
l123_entry_backtest.py — L1/L2/L3三层建仓逻辑回放验证

背景：
  universal_precise_entry.py 实现了新的三层建仓判定逻辑：
    L1: 追高风险评分（放量涨停第一天为高风险）
    L2: 缩量支撑（冲高回踩支撑不破+显著缩量）
    L3: 日内共振（15分钟 EMA8+放量+VWAP确认）

  但该逻辑从未经过真实历史数据的回放验证，只有纸面设计和Optuna评分（后者已被证实为空壳）。

本实验目的（唯一目标）：
  在现行 timing_gate.GO 的日子里，对比：
    A. baseline: GO日收盘价买入（现行signal隐含成交）
    B. l123_pass: L1/L2/L3全过的GO日，用确认价买入
    C. l12_only: L1/L2过但L3未过的GO日（是否有价值提前买）
    D. l1_only:  仅L1过的GO日（评估L1的信噪比）
    E. range_l12: 震荡市中L1/L2过的个股（range_release的质量评估）

  前向收益：T+1/T+3/T+5相对成交价。无未来函数。

数据源（同w35）：
  - 日线: position_builder.fetch_daily_kline
  - 日内1分钟: w34_resonance_backtest_year._day_df（月缓存复用）
  - regime/GO: timing_gate.timing_verdict
  - L1/L2/L3: universal_precise_entry.UniversalPreciseEntry

用法:
    python t_io/validation/l123_entry_backtest.py
    python t_io/validation/l123_entry_backtest.py --start 2025-08-15 --end 2026-08-15
    python t_io/validation/l123_entry_backtest.py --codes 000988 300054
输出: t_io/replay/l123_entry_{start}_{end}/
"""
import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from position_builder import fetch_daily_kline  # noqa: E402
from timing_gate import timing_verdict  # noqa: E402
from universal_precise_entry import UniversalPreciseEntry  # noqa: E402
from indicators import resample_to_15min, add_15min_indicators  # noqa: E402
from t_io.validation.w34_resonance_backtest_year import (  # noqa: E402
    _day_df, _stock_code_to_ts, _trading_days, WATCHLIST_FILE,
)
from index_regime_intraday import _iri_tushare_pro  # noqa: E402

OUT_ROOT = BASE / "t_io" / "replay"


def _fwd_from_daily(daily: pd.DataFrame, t_date: str, entry_px: float) -> dict:
    """从日线T日往后取T+1/T+3/T+5收盘，相对entry_px的收益。无未来。"""
    d = daily.sort_values("date").reset_index(drop=True)
    idx = d.index[d["date"].astype(str) == str(t_date)]
    if len(idx) == 0 or entry_px <= 0:
        return {}
    i = int(idx[0])
    out = {}
    for label, k in (("fwd1", 1), ("fwd3", 3), ("fwd5", 5)):
        j = i + k
        out[label] = round(float(d["close"].iloc[j]) / entry_px - 1, 5) if j < len(d) else None
    lows = [float(d["close"].iloc[i + k]) for k in range(1, 6) if i + k < len(d)]
    out["maxdd5"] = round(min(lows) / entry_px - 1, 5) if lows else None
    return out


def _load_candidates(explicit):
    if explicit:
        return sorted(set(explicit))
    wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")) if WATCHLIST_FILE.exists() else {}
    return sorted({c for c, v in (wl.get("stocks", {}) or {}).items()
                   if isinstance(v, dict) and not c.startswith("_example")
                   and v.get("status") in ("monitoring", "signal")})


def _agg(rows, key):
    """对一组事件按fwd1/3/5/maxdd5求均值、胜率。"""
    def _stat(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        if not vals:
            return None, None
        arr = np.array(vals, dtype=float)
        return round(float(arr.mean()), 4), round(float((arr > 0).mean()), 4)
    m1, w1 = _stat("fwd1")
    m3, w3 = _stat("fwd3")
    m5, w5 = _stat("fwd5")
    dd, _ = _stat("maxdd5")
    return {"n": len(rows), "fwd1": m1, "win1": w1, "fwd3": m3, "win3": w3,
            "fwd5": m5, "win5": w5, "maxdd5": dd}


def main():
    ap = argparse.ArgumentParser(description="L1/L2/L3三层建仓逻辑回放验证")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--codes", nargs="*", default=None)
    args = ap.parse_args()

    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    codes = _load_candidates(args.codes)
    days = _trading_days(start, end)
    print(f"[universe] {len(codes)}只候选 | {start}~{end}({len(days)}工作日)")

    try:
        pro = _iri_tushare_pro()
    except Exception as e:
        print(f"[FATAL] tushare初始化失败: {e}")
        return

    arms = {
        "A_baseline": [],      # GO日收盘价买入（baseline）
        "B_l123_pass": [],     # L1/L2/L3全过
        "C_l12_only": [],      # L1/L2过L3未过
        "D_l1_only": [],       # 仅L1过
        "E_range_l12": [],     # 震荡市L1/L2过
    }
    go_days = 0
    l123_days = defaultdict(int)

    print(f"\n开始逐股逐日扫描...")
    for ci, code in enumerate(codes, 1):
        daily = fetch_daily_kline(code)
        if daily is None or daily.empty or len(daily) < 66:
            continue
        daily = daily.sort_values("date").reset_index(drop=True)
        daily["date"] = daily["date"].astype(str)
        ts_code = _stock_code_to_ts(code)

        for day in days:
            if day not in set(daily["date"]):
                continue
            try:
                tv = timing_verdict(code, day)
                upe = UniversalPreciseEntry(code)
                l123_result = upe.check_ready_to_buy_universal(day)
            except Exception:
                continue

            regime = tv.get("regime")
            t_close_rows = daily[daily["date"] == day]
            if t_close_rows.empty:
                continue
            t_close = float(t_close_rows["close"].iloc[0])

            # ---- A 臂: GO日baseline ----
            if tv.get("go"):
                go_days += 1
                fwd_a = _fwd_from_daily(daily, day, t_close)
                if fwd_a:
                    arms["A_baseline"].append({"code": code, "date": day,
                                              "entry_price": round(t_close, 4), **fwd_a})

                # ---- B/C/D 臂: L1/L2/L3评估（仅在GO日） ----
                l1 = l123_result.get("l1", {})
                l2 = l123_result.get("l2", {})
                l3 = l123_result.get("l3", {})
                l1_pass = l1.get("level") == "safe"
                l2_pass = l2.get("is_consolidating", False)
                l3_pass = l3.get("resonance", False)

                l123_key = f"L1{int(l1_pass)}L2{int(l2_pass)}L3{int(l3_pass)}"
                l123_days[l123_key] += 1

                # 日内确认价（复用w35逻辑）
                df1 = _day_df(ts_code, day, pro)
                if df1 is not None and not df1.empty and len(df1) > 20:
                    d = df1.copy()
                    d["time"] = pd.to_datetime(d["time"], errors="coerce")
                    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
                    if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
                        cum_amt = d["amount"].fillna(0).cumsum()
                    else:
                        cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
                    cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
                    d["vwap_cum"] = cum_amt / cum_vol

                    df15 = add_15min_indicators(resample_to_15min(d))
                    if df15 is not None and not df15.empty:
                        df15 = df15.copy()
                        df15["time"] = pd.to_datetime(df15["time"], errors="coerce")
                        last_min_ts = d["time"].iloc[-1]

                        intraday_px = None
                        for _, bar in df15.iterrows():
                            close_ts = bar["time"] + pd.Timedelta(minutes=15)
                            if close_ts > last_min_ts + pd.Timedelta(minutes=1):
                                continue
                            c = bar.get("close")
                            ema8 = bar.get("ema_fast_15m")
                            volr = bar.get("vol_ratio_15m")
                            if any(pd.isna(x) for x in (c, ema8, volr)):
                                continue
                            ema_ok = float(c) > float(ema8)
                            vol_ok = float(volr) > 1.2
                            vw_rows = d[d["time"] <= close_ts]
                            if vw_rows.empty:
                                continue
                            vwap = float(vw_rows["vwap_cum"].iloc[-1]) if pd.notna(vw_rows["vwap_cum"].iloc[-1]) else None
                            vwap_ok = (vwap is None) or (float(c) >= vwap)
                            if ema_ok and vol_ok and vwap_ok:
                                intraday_px = float(vw_rows["close"].iloc[-1])
                                break

                    # 计算日内让价
                    if intraday_px:
                        day_close_raw = float(d["close"].iloc[-1]) if len(d) > 0 else t_close
                        slip = round(intraday_px / day_close_raw - 1, 5) if day_close_raw else None
                        if slip is not None:
                            entry_qfq = round(t_close * (1 + slip), 4)
                        else:
                            entry_qfq = t_close
                    else:
                        entry_qfq = t_close

                # B臂: L1/L2/L3全过
                if l1_pass and l2_pass and l3_pass:
                    fwd_b = _fwd_from_daily(daily, day, entry_qfq)
                    if fwd_b:
                        arms["B_l123_pass"].append({"code": code, "date": day,
                                                    "entry_price": round(entry_qfq, 4), **fwd_b})

                # C臂: L1/L2过L3未过
                elif l1_pass and l2_pass and not l3_pass:
                    fwd_c = _fwd_from_daily(daily, day, t_close)
                    if fwd_c:
                        arms["C_l12_only"].append({"code": code, "date": day,
                                                   "entry_price": round(t_close, 4), **fwd_c})

                # D臂: 仅L1过
                elif l1_pass and not l2_pass and not l3_pass:
                    fwd_d = _fwd_from_daily(daily, day, t_close)
                    if fwd_d:
                        arms["D_l1_only"].append({"code": code, "date": day,
                                                  "entry_price": round(t_close, 4), **fwd_d})

            # ---- E臂: 震荡市L1/L2过 ----
            if regime == "range":
                l1 = l123_result.get("l1", {})
                l2 = l123_result.get("l2", {})
                l1_pass = l1.get("level") == "safe"
                l2_pass = l2.get("is_consolidating", False)
                if l1_pass and l2_pass:
                    fwd_e = _fwd_from_daily(daily, day, t_close)
                    if fwd_e:
                        arms["E_range_l12"].append({"code": code, "date": day,
                                                    "entry_price": round(t_close, 4), **fwd_e})

        if (ci) % 10 == 0:
            print(f"  进度 {ci}/{len(codes)} | GO日累计{go_days} | B臂{len(arms['B_l123_pass'])} "
                  f"C臂{len(arms['C_l12_only'])} D臂{len(arms['D_l1_only'])}")

    # ---- 输出 ----
    out_dir = OUT_ROOT / f"l123_entry_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "events.jsonl", "w", encoding="utf-8") as fjs:
        for arm, rows in arms.items():
            for r in rows:
                fjs.write(json.dumps({"arm": arm, **r}, ensure_ascii=False) + "\n")

    stats = {arm: _agg(rows, arm) for arm, rows in arms.items()}

    def _fmt(x):
        return f"{x:+.2%}" if isinstance(x, float) else "—"

    lines = [
        f"# L1/L2/L3三层建仓逻辑回放验证 ({start}~{end})",
        "",
        f"候选 {len(codes)}只 | GO日 {go_days} | L1/L2/L3组合分布: {dict(l123_days)}",
        "",
        "| 臂 | 含义 | n | fwd1 | 胜1 | fwd3 | 胜3 | fwd5 | 胜5 | 平均maxdd5 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    meanings = {
        "A_baseline": "GO日收盘价(现行signal)",
        "B_l123_pass": "GO+L1/L2/L3全过",
        "C_l12_only": "GO+L1/L2过L3未过",
        "D_l1_only": "GO+仅L1过",
        "E_range_l12": "震荡市L1/L2过",
    }
    for arm in ("A_baseline", "B_l123_pass", "C_l12_only", "D_l1_only", "E_range_l12"):
        s = stats[arm]
        w = lambda x: f"{x:.0%}" if isinstance(x, float) else "—"  # noqa: E731
        lines.append(f"| {arm} | {meanings[arm]} | {s['n']} | {_fmt(s['fwd1'])} | {w(s['win1'])} | "
                     f"{_fmt(s['fwd3'])} | {w(s['win3'])} | {_fmt(s['fwd5'])} | {w(s['win5'])} | {_fmt(s['maxdd5'])} |")

    lines += [
        "",
        "## 读法",
        "- **B vs A**: L1/L2/L3全过是否有增量价值（相对baseline的fwd/胜率/回撤）",
        "- **C vs A**: L1/L2过但缺L3确认的质量——若C≈A则L3无价值、若C>A则早买有优势、若C<A则L3是重要闸门",
        "- **D vs A**: 仅L1过的表现——L1是否有信号质量",
        "- **E**: 震荡市L1/L2组合的表现——是否支持range市放行观察态",
        "",
        "> 无未来函数; 日线截止T日; 15m指标只用已收盘bar; VWAP用当日累计; B臂成交价为日内确认bar收盘"
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "\n".join(lines))
    print(f"\n[OK] → {out_dir}")


if __name__ == "__main__":
    main()
