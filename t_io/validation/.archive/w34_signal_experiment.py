# -*- coding: utf-8 -*-
"""
w34_signal_experiment.py — 做T信号+指数过滤 网格实验（2026-08-14 新增）

目标：实验"个股用什么指标 / 是否15分钟指标搭大盘指标"能大幅提升做T成功率。
方法：同一份数据（tushare 1 分钟，35 候选 × 1 年）上，对每种【个股信号 × 指数过滤】组合
生成信号并按统一结算（+0.5%/-0.4%，30/60tick）算命中率，多组回测对比取最优。

个股信号变体（STOCK_SIGNALS，均复刻 signal_engine 阈值）：
  bb5_rsi6    5分钟 bb触轨 + rsi6 极值（当前实盘口径，基线）
  bb15_rsi6   15分钟 bb触轨 + rsi15 极值
  bb5_conf15  5分钟极值 + 15分钟 RSI 确认（多周期共振）
  macd15_bb5  5分钟 bb 极值 + 15分钟 MACD 方向（15分趋势方向内做5分反转）

指数过滤变体（INDEX_FILTERS）：
  none           不过滤（基线）
  index_ma5_dir  买需指数站上其5分钟MA5 / 卖需指数跌破（前一轮实验最优）
  contrarian     拦截"指数与个股同处极值"

用法：
    python t_io/validation/w34_signal_experiment.py
    python t_io/validation/w34_signal_experiment.py --start 2025-08-14 --end 2026-08-14 --codes 000988
输出：t_io/replay/signal_experiment_{start}_{end}/results.json + 控制台对比表
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis.indicators import resample_to_5min, add_5min_indicators, resample_to_15min, add_15min_indicators  # noqa: E402
from analysis.index_regime_intraday import _iri_tushare_pro  # noqa: E402
from index_resonance import resolve_index  # noqa: E402
from w34_resonance_backtest_year import (  # noqa: E402
    _day_df, _stock_code_to_ts, _trading_days, WATCHLIST_FILE, HOLDINGS_FILE,
)

SETTLE_TICKS_30 = 30
SETTLE_TICKS_60 = 60
WIN_TARGET = 0.005
STOP = 0.004
MACD15_BB_EDGE = 1.0   # macd15_bb5 的 5 分钟 bb 触发阈值（--macd15-bb-edge 覆盖）


def _add_bb15(df15):
    c = df15["close"]
    ma = c.rolling(20, min_periods=1).mean()
    sd = c.rolling(20, min_periods=1).std()
    up, dn = ma + 2 * sd, ma - 2 * sd
    df15["bb_pct_15m"] = (c - dn) / (up - dn).replace(0, np.nan)
    return df15


def _outcome(df1, entry_idx, action, price, ticks):
    tgt = 1 + WIN_TARGET
    stp = 1 - STOP
    for i in range(entry_idx + 1, min(entry_idx + ticks + 1, len(df1))):
        p = float(df1.iloc[i]["close"])
        if action in ("BUY_LOW", "ADD_POS"):
            if p <= price * stp:
                return "FAIL"
            if p >= price * tgt:
                return "WIN"
        else:
            if p >= price * (1 + STOP):
                return "FAIL"
            if p <= price * (1 - WIN_TARGET):
                return "WIN"
    return "VOID"


def _entry_idx(df1, entry_ts):
    times = df1["time"].values
    return int(np.searchsorted(times, np.datetime64(entry_ts), side="right")) - 1


def _asof_15m(df1, entry_ts):
    """entry_ts 时刻的 15 分钟指标（严格无未来，且与实盘 evaluate 口径一致）。

    fix 2026-08-15: 原实现用全天 df15 的 label<=entry_ts 根 → 取到未收盘根的
    未来收盘价（lookahead，高估信号）。改为在 entry_ts 时刻重采样 1 分钟线取最新根，
    与实盘 evaluate 的 resample_to_15min(df 截至当前) 完全一致。返回 (rsi15, dif15, dea15) 或 None。
    """
    if df1 is None or df1.empty:
        return None
    sub = df1[df1["time"] <= pd.Timestamp(entry_ts)]
    if len(sub) < 3:
        return None
    df15 = add_15min_indicators(resample_to_15min(sub))
    if df15 is None or df15.empty:
        return None
    last = df15.iloc[-1]
    rsi = float(last.get("rsi_15m")) if pd.notna(last.get("rsi_15m")) else None
    dif = float(last.get("macd_15m")) if pd.notna(last.get("macd_15m")) else None
    dea = float(last.get("macd_signal_15m")) if pd.notna(last.get("macd_signal_15m")) else None
    if rsi is None or dif is None or dea is None:
        return None
    return rsi, dif, dea


def _gen_signals(code, name, date, df1, df5, df15, idx_5min_day, ic):
    """按全部个股信号变体生成该 (stock, day) 的信号（含指数特征与结算）。"""
    out = {v: [] for v in STOCK_SIGNALS}
    if df5 is None or df5.empty or len(df5) < 13:
        return out
    idx_close = idx_5min_day.get(ic) if idx_5min_day else None
    idx_times = idx_close["time"].values if (idx_close is not None and not idx_close.empty) else np.array([])

    def _idx_feats(entry_ts):
        if idx_close is None or len(idx_times) == 0:
            return {"missing": True}
        # fix 2026-08-15: side="left" → 取 label<entry_ts 的已收盘根（排除未收盘的 forming 根，无 lookahead）
        j = int(np.searchsorted(idx_times, np.datetime64(entry_ts), side="left")) - 1
        if j < 0:
            return {"missing": True}
        last = idx_close.iloc[j]
        bb = float(last.get("bb_pct_5m")) if pd.notna(last.get("bb_pct_5m")) else None
        rsi = float(last.get("rsi_5m_p6")) if pd.notna(last.get("rsi_5m_p6")) else None
        cl = float(last.get("close")) if pd.notna(last.get("close")) else None
        ma5 = float(last.get("idx_ma5")) if pd.notna(last.get("idx_ma5")) else None
        if bb is None or rsi is None or cl is None or ma5 is None:
            return {"missing": True}
        return {"missing": False, "bb": bb, "rsi": rsi, "close": cl, "ma5": ma5}

    def _record(variant, entry_ts, action):
        eidx = _entry_idx(df1, entry_ts)
        if eidx < 0 or eidx >= len(df1):
            return
        price = float(df1.iloc[eidx]["close"])
        if price <= 0:
            return
        fx = _idx_feats(entry_ts)
        out[variant].append({
            "date": date, "code": code, "name": name, "action": action,
            "price": round(price, 3),
            "o30": _outcome(df1, eidx, action, price, SETTLE_TICKS_30),
            "o60": _outcome(df1, eidx, action, price, SETTLE_TICKS_60),
            "idx_missing": fx.get("missing", True),
            "idx_bb": fx.get("bb"), "idx_rsi": fx.get("rsi"),
            "idx_close": fx.get("close"), "idx_ma5": fx.get("ma5"),
        })

    # 5 分钟根信号变体
    for b in range(12, len(df5)):
        row = df5.iloc[b]
        bb = row.get("bb_pct_5m"); rsi = row.get("rsi_5m_p6")
        if bb is None or rsi is None or pd.isna(bb) or pd.isna(rsi):
            continue
        bb, rsi = float(bb), float(rsi)
        entry_ts = row["time"] + pd.Timedelta(minutes=5)
        # bb5_rsi6
        if bb >= 1.0 and rsi > 75:
            _record("bb5_rsi6", entry_ts, "SELL_HIGH")
        elif bb <= 0.0 and rsi < 35:
            _record("bb5_rsi6", entry_ts, "BUY_LOW")
        # 懒计算 15 分钟确认（仅当 bb 触及触发带，避免逐bar重采样拖慢）
        if bb >= 0.85 or bb <= 0.15:
            c15 = _asof_15m(df1, entry_ts)
            if c15 is not None:
                r15, dif, dea = c15
                # bb5_conf15
                if bb >= 1.0 and rsi > 75 and r15 > 55:
                    _record("bb5_conf15", entry_ts, "SELL_HIGH")
                elif bb <= 0.0 and rsi < 35 and r15 < 45:
                    _record("bb5_conf15", entry_ts, "BUY_LOW")
                # macd15_bb5（sell 需 bb≥edge 触上轨区；buy 对称 bb≤1-edge 触下轨区）
                if bb >= MACD15_BB_EDGE and dif < dea:
                    _record("macd15_bb5", entry_ts, "SELL_HIGH")
                elif bb <= 1.0 - MACD15_BB_EDGE and dif > dea:
                    _record("macd15_bb5", entry_ts, "BUY_LOW")

    # 15 分钟根信号变体
    if df15 is not None and len(df15) >= 7:
        for b in range(6, len(df15)):
            row = df15.iloc[b]
            bb = row.get("bb_pct_15m"); rsi = row.get("rsi_15m")
            if bb is None or rsi is None or pd.isna(bb) or pd.isna(rsi):
                continue
            bb, rsi = float(bb), float(rsi)
            entry_ts = row["time"] + pd.Timedelta(minutes=15)
            if bb >= 1.0 and rsi > 75:
                _record("bb15_rsi6", entry_ts, "SELL_HIGH")
            elif bb <= 0.0 and rsi < 35:
                _record("bb15_rsi6", entry_ts, "BUY_LOW")
    return out


def _apply_filter(sig, filt):
    if sig.get("idx_missing"):
        return "data_missing"
    buy = sig["action"] in ("BUY_LOW", "ADD_POS")
    if filt == "index_ma5_dir":
        return "pass" if ((sig["idx_close"] >= sig["idx_ma5"]) if buy else (sig["idx_close"] <= sig["idx_ma5"])) else "block"
    if filt == "contrarian":
        same = (sig["idx_bb"] <= 0.25 and sig["idx_rsi"] <= 40) if buy else (sig["idx_bb"] >= 0.75 and sig["idx_rsi"] >= 60)
        return "block" if same else "pass"
    return "pass"


def _stats(signals, filt, tick):
    g = {}
    for grp in ("pass", "block", "data_missing"):
        sub = [s for s in signals if _apply_filter(s, filt) == grp]
        w = sum(1 for s in sub if s[f"o{tick}"] == "WIN")
        f = sum(1 for s in sub if s[f"o{tick}"] == "FAIL")
        g[grp] = {"n": len(sub), "wins": w, "fails": f,
                  "void": sum(1 for s in sub if s[f"o{tick}"] == "VOID"),
                  "hr": round(w / (w + f), 4) if (w + f) else None}
    return g


def main():
    ap = argparse.ArgumentParser(description="做T信号+指数过滤网格实验")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--codes", nargs="*", default=None)
    ap.add_argument("--macd15-bb-edge", type=float, default=1.0,
                    help="macd15_bb5 的 5 分钟 bb 触发阈值（放宽以增信号量）")
    args = ap.parse_args()
    global MACD15_BB_EDGE
    MACD15_BB_EDGE = args.macd15_bb_edge

    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    if args.codes:
        codes = list(args.codes)
    else:
        wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")) if WATCHLIST_FILE.exists() else {}
        codes = [c for c, v in (wl.get("stocks", {}) or {}).items()
                 if isinstance(v, dict) and not c.startswith("_example") and v.get("status") in ("monitoring", "signal")]
    names = {}
    if WATCHLIST_FILE.exists():
        wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        names = {c: v.get("name", c) for c, v in (wl.get("stocks", {}) or {}).items() if isinstance(v, dict)}
    codes = sorted(set(codes))
    index_codes = {resolve_index(c)[0] for c in codes}
    days = _trading_days(start, end)
    print(f"[universe] {len(codes)} 候选 ｜ {start}~{end}（{len(days)} 工作日）")

    pro = _iri_tushare_pro()
    by_variant = {v: [] for v in STOCK_SIGNALS}
    day_idx = 0
    for day in days:
        day_idx += 1
        idx_5min_day = {}
        for ic in index_codes:
            # ic 形如 sh000001 → ts_code 000001.SH
            ic_ts = (ic[2:] + ".SH") if ic.startswith("sh") else ((ic[2:] + ".SZ") if ic.startswith("sz") else ic)
            df1 = _day_df(ic_ts, day, pro)
            if df1.empty:
                continue
            df5 = resample_to_5min(df1)
            if df5 is not None and not df5.empty:
                df5 = add_5min_indicators(df5)
                df5["idx_ma5"] = df5["close"].rolling(5).mean()
                idx_5min_day[ic] = df5
        for code in codes:
            df1 = _day_df(_stock_code_to_ts(code), day, pro)
            if df1.empty or len(df1) < 60:
                continue
            df5 = resample_to_5min(df1)
            if df5 is None or df5.empty:
                continue
            df5 = add_5min_indicators(df5)
            df15 = _add_bb15(add_15min_indicators(resample_to_15min(df1)))
            ic = resolve_index(code)[0]
            got = _gen_signals(code, names.get(code, code), day, df1, df5, df15, idx_5min_day, ic)
            for v in STOCK_SIGNALS:
                by_variant[v].extend(got[v])
        if day_idx % 30 == 0 or day_idx == len(days):
            print(f"  [{day_idx}/{len(days)}] {day}")

    # ── 汇总对比表 ──
    results = {}
    print(f"\n{'个股信号':12s} {'指数过滤':14s} {'n信号':>6} {'放行n':>6} {'放行hr30':>8} {'拦截hr30':>8} {'差30':>7} {'放行hr60':>8} {'拦截hr60':>8} {'差60':>7}")
    print("-" * 100)
    for v in STOCK_SIGNALS:
        sigs = by_variant[v]
        for f in INDEX_FILTERS:
            s30 = _stats(sigs, f, 30)
            s60 = _stats(sigs, f, 60)
            p30, b30 = s30["pass"]["hr"], s30["block"]["hr"]
            p60, b60 = s60["pass"]["hr"], s60["block"]["hr"]
            gap30 = (p30 - b30) if (p30 is not None and b30 is not None) else None
            gap60 = (p60 - b60) if (p60 is not None and b60 is not None) else None
            results[f"{v}|{f}"] = {"n": len(sigs), "pass_n": s30["pass"]["n"], "hr30": p30, "hr60": p60,
                                   "gap30": gap30, "gap60": gap60}
            def _fmt(x):
                return f"{x:7.1%}" if x is not None else "     —"
            g30 = f"{gap30:+.1%}" if gap30 is not None else "—"
            g60 = f"{gap60:+.1%}" if gap60 is not None else "—"
            print(f"{v:12s} {f:14s} {len(sigs):6d} {s30['pass']['n']:6d} "
                  f"{_fmt(p30)} {_fmt(b30)} {g30:>7} {_fmt(p60)} {_fmt(b60)} {g60:>7}")
    _b = []
    for v in STOCK_SIGNALS:
        hr = _stats(by_variant[v], "none", 30)["pass"]["hr"]
        _b.append(f"{v}={hr:.1%}" if hr is not None else f"{v}=—")
    print("\n基线(不过滤): " + " ｜ ".join(_b))

    out_dir = BASE / "t_io" / "replay" / f"signal_experiment_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    for v in STOCK_SIGNALS:
        (out_dir / f"events_{v}.jsonl").write_text(
            "\n".join(json.dumps(s, ensure_ascii=False) for s in by_variant[v]) + "\n", encoding="utf-8")
    print(f"[OK] 输出 → {out_dir}")


STOCK_SIGNALS = ["bb5_rsi6", "bb15_rsi6", "bb5_conf15", "macd15_bb5"]
INDEX_FILTERS = ["none", "index_ma5_dir", "contrarian"]

if __name__ == "__main__":
    main()
