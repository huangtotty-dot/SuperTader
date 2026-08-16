# -*- coding: utf-8 -*-
"""
w34_resonance_backtest_year.py — 指数共振 1 年长周期回测（2026-08-14 新增）

将回测范围扩大到全部 GUI 建仓候选股池（watchlist_buy.json status=monitoring），
时间跨 1 年（默认最近 ~250 个交易日），评估指数5分钟共振门控对做T成功率的影响。

信号生成：忠实复刻 signal_engine 纯两点规则（bb_pct_5m 触轨 + rsi_5m_p6 极值，
swing_min_5m_bars=13 预热），在预计算的 5 分钟 K 线上逐根判定（秒级，可全年迭代）。
成功率口径：与 daily_review settle 一致（+0.5%/-0.4%/30tick）。

门控口径（--gate，均可切）：
  - same_direction 同向极值：指数与个股同处极值才放行（实证 2026-08-14：有害）
  - contrarian      反向：指数与个股同处极值时拦截（实证：放行组命中率显著更高）
  - non_contrary    不逆势：指数未深破下轨才放行（阈值过松，实证拦截率极低）

主事件（events.jsonl）含每条信号的指数 bb/rsi 原始值；--reuse 复用主事件、
秒级切换门控口径做迭代，无需重算信号。

数据：tushare stk_mins 1 分钟线，按 (ts_code, 月) 拉取缓存（<8000 上限），幂等。

用法：
    python t_io/validation/w34_resonance_backtest_year.py --gate contrarian
    python t_io/validation/w34_resonance_backtest_year.py --gate same_direction --reuse
输出：t_io/replay/resonance_year_{start}_{end}_{gate}/  events.jsonl + metrics.json
"""
import argparse
import calendar
import json
import sys
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]  # 仓库根
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import index_resonance as ir
from indicators import resample_to_5min, add_5min_indicators
from index_regime_intraday import _iri_tushare_pro

HOLDINGS_FILE = BASE / "holdings.json"
WATCHLIST_FILE = BASE / "watchlist_buy.json"
CACHE_DIR = BASE / "t_io" / "cache" / "tushare_mins"
FETCH_DELAY = 0.4

SETTLE_TICKS = 30
WIN_TARGET = {"BUY_LOW": 0.005, "SELL_HIGH": 0.005}
STOP = {"BUY_LOW": 0.004, "SELL_HIGH": 0.004}

# (ts_code, YYYY-MM) -> {date_str: 当日1分钟 DataFrame}（按日切片缓存，避免逐日过滤）
_month_days = {}


def _stock_code_to_ts(code: str) -> str:
    base = str(code).split("_")[0]
    return (base + ".SH") if base[0] in "56" else (base + ".SZ")


def _month_last_day(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{ym}-{calendar.monthrange(y, m)[1]}"


def _get_month_days(ts_code: str, ym: str, pro) -> dict:
    """(ts_code, 月) → {date: df}，缓存。"""
    key = (ts_code, ym)
    if key in _month_days:
        return _month_days[key]
    fp = CACHE_DIR / ts_code / f"{ym}.csv"
    if fp.exists():
        df = pd.read_csv(fp, parse_dates=["time"])
    else:
        df = pro.stk_mins(ts_code=ts_code, freq="1min",
                          start_date=f"{ym}-01 09:00:00", end_date=f"{_month_last_day(ym)} 19:00:00")
        if df is None or df.empty:
            _month_days[key] = {}
            return {}
        df = df.rename(columns={"trade_time": "time", "vol": "volume"})
        keep = [c for c in ("time", "open", "close", "high", "low", "volume", "amount") if c in df.columns]
        df = df[keep].copy()
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
        fp.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(fp, index=False)
        time.sleep(FETCH_DELAY)
    out = {}
    for d, sub in df.groupby(df["time"].dt.date):
        out[d.strftime("%Y-%m-%d")] = sub.reset_index(drop=True)
    _month_days[key] = out
    return out


def _day_df(ts_code: str, date_str: str, pro) -> pd.DataFrame:
    return _get_month_days(ts_code, date_str[:7], pro).get(date_str, pd.DataFrame())


def _index_indicators_asof(index_code: str, idx_5min: pd.DataFrame, entry_ts) -> dict:
    """信号时刻指数最新 5 分钟根指标（无未来函数）。返回 {bb, rsi, missing, reason}。"""
    if idx_5min is None or idx_5min.empty:
        return {"bb": None, "rsi": None, "missing": True, "reason": "无指数5分钟"}
    # fix 2026-08-15: 指数5分钟根 label=T 在 T+5min 才收盘；entry_ts 时刻用 label<entry_ts
    # 的已收盘根，否则取到未收盘根的未来收盘价（lookahead，与实盘 resample 截至当前 对齐）
    bars = idx_5min[idx_5min["time"] < pd.Timestamp(entry_ts)]
    min_bars = int(ir._params().get("min_index_5m_bars", 5))
    if len(bars) < min_bars:
        return {"bb": None, "rsi": None, "missing": True, "reason": f"指数5分钟仅{len(bars)}根(<{min_bars})"}
    last = bars.iloc[-1]
    bb = float(last.get("bb_pct_5m")) if pd.notna(last.get("bb_pct_5m")) else None
    rsi = float(last.get("rsi_5m_p6")) if pd.notna(last.get("rsi_5m_p6")) else None
    if bb is None or rsi is None:
        return {"bb": None, "rsi": None, "missing": True, "reason": "指数指标NaN"}
    return {
        "bb": bb, "rsi": rsi, "missing": False, "reason": None,
        "close": float(last.get("close")) if pd.notna(last.get("close")) else None,
        "dif": float(last.get("dif_5m")) if pd.notna(last.get("dif_5m")) else None,
        "dea": float(last.get("dea_5m")) if pd.notna(last.get("dea_5m")) else None,
        "macd_hist": float(last.get("macd_hist_5m")) if pd.notna(last.get("macd_hist_5m")) else None,
        "ma5": float(last.get("idx_ma5")) if pd.notna(last.get("idx_ma5")) else None,
        "mom3": float(last.get("idx_mom_3bar")) if pd.notna(last.get("idx_mom_3bar")) else None,
        "mom6": float(last.get("idx_mom_6bar")) if pd.notna(last.get("idx_mom_6bar")) else None,
    }


def _signal_outcome(df1: pd.DataFrame, entry_idx: int, action: str, price: float) -> str:
    tgt = 1 + WIN_TARGET.get(action, 0.005)
    stp = 1 - STOP.get(action, 0.004)
    for i in range(entry_idx + 1, min(entry_idx + SETTLE_TICKS + 1, len(df1))):
        p = float(df1.iloc[i]["close"])
        if action in ("BUY_LOW", "ADD_POS"):
            if p <= price * stp:
                return "FAIL"
            if p >= price * tgt:
                return "WIN"
        else:
            if p >= price * (1 + STOP.get(action, 0.004)):
                return "FAIL"
            if p <= price * (1 - WIN_TARGET.get(action, 0.005)):
                return "WIN"
    return "VOID"


def _forward_returns(df1: pd.DataFrame, entry_idx: int, price: float) -> dict:
    out = {}
    for label, minutes in (("m5", 5), ("m15", 15), ("m30", 30)):
        target_t = pd.Timestamp(df1.iloc[entry_idx]["time"]) + pd.Timedelta(minutes=minutes)
        sub = df1[df1["time"] <= target_t]
        out[label] = round(float(sub.iloc[-1]["close"]) / price - 1, 5) if (not sub.empty and price) else None
    return out


def _trading_days(start: str, end: str) -> list:
    days = []
    cur = pd.Timestamp(start).date()
    endd = pd.Timestamp(end).date()
    while cur <= endd:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return days


def _apply_gate(signals: list, gate: str) -> dict:
    """按门控口径把信号分为 pass/block/data_missing，返回分组统计。"""
    p = ir._params()
    is_buy = lambda a: a in ("BUY_LOW", "ADD_POS")  # noqa: E731

    def _verdict(r):
        if r.get("index_missing"):
            return "data_missing"
        bb, rsi = r.get("index_bb_pct_5m"), r.get("index_rsi_6_5m")
        if bb is None or rsi is None:
            return "data_missing"
        buy = is_buy(r["action"])
        if gate == "index_ma5_dir":
            # 指数自身5分钟MA5方向：买需指数站上MA5、卖需指数跌破MA5（全年回测最优）
            cl, ma5 = r.get("index_close"), r.get("index_ma5_5m")
            if cl is None or ma5 is None:
                return "data_missing"
            return "pass" if ((cl >= ma5) if buy else (cl <= ma5)) else "block"
        if gate == "contrarian":
            # 反向：指数与个股同处极值 → 拦截（同向极值时信号更差，实证）
            same = (bb <= float(p.get("buy_bb_max", 0.25)) and rsi <= float(p.get("buy_rsi_max", 40))) if buy \
                else (bb >= float(p.get("sell_bb_min", 0.75)) and rsi >= float(p.get("sell_rsi_min", 60)))
            return "block" if same else "pass"
        if gate == "non_contrary":
            floor = float(p.get("buy_floor", -0.30)) if buy else float(p.get("sell_floor", -0.20))
            return "pass" if bb >= floor else "block"
        # same_direction
        same = (bb <= float(p.get("buy_bb_max", 0.25)) and rsi <= float(p.get("buy_rsi_max", 40))) if buy \
            else (bb >= float(p.get("sell_bb_min", 0.75)) and rsi >= float(p.get("sell_rsi_min", 60)))
        return "pass" if same else "block"

    for r in signals:
        r["group"] = _verdict(r)
    g = {}
    for grp in ("pass", "block", "data_missing"):
        sub = [r for r in signals if r["group"] == grp]
        w = sum(1 for r in sub if r["outcome"] == "WIN")
        f = sum(1 for r in sub if r["outcome"] == "FAIL")
        f15 = [r["fwd"]["m15"] for r in sub if r["fwd"].get("m15") is not None]
        g[grp] = {
            "n": len(sub), "wins": w, "fails": f, "void": sum(1 for r in sub if r["outcome"] == "VOID"),
            "hit_rate": round(w / (w + f), 4) if (w + f) else None,
            "avg_fwd_15m": round(float(np.mean(f15)), 4) if f15 else None,
            "by_action": dict(Counter(r["action"] for r in sub)),
        }
    return g


def _print_metrics(start, end, gate, signals, by_group, n_days_data):
    gap = (by_group["pass"]["hit_rate"] - by_group["block"]["hit_rate"]
           if by_group["pass"]["hit_rate"] is not None and by_group["block"]["hit_rate"] is not None else None)
    if gap is None:
        verdict = "样本不足"
    elif gap > 0.03:
        verdict = "filter_effective"
    elif gap < -0.03:
        verdict = "filter_harmful"
    else:
        verdict = "filter_neutral"
    n_emitted = by_group["pass"]["n"]
    print(f"\n== 区间 {start}~{end} gate={gate} ==")
    print(f"候选 {len(set(r['code'] for r in signals))} 只 ｜ 有数据交易日 {n_days_data} ｜ 信号 {len(signals)} 条（放行 {n_emitted}）")
    for grp in ("pass", "block", "data_missing"):
        g = by_group[grp]
        hr = f"{g['hit_rate'] * 100:.1f}%" if g["hit_rate"] is not None else "—"
        print(f"  {grp:12s} n={g['n']:6d} wins={g['wins']:5d} fails={g['fails']:5d} hit_rate={hr:>7} "
              f"avg15m={('%.2f%%' % (g['avg_fwd_15m'] * 100)) if g['avg_fwd_15m'] is not None else '—':>7} {dict(g['by_action'])}")
    if gap is not None:
        print(f"命中率差 放行−拦截 = {gap:+.2%} → {verdict}")
    return verdict, gap


def main():
    ap = argparse.ArgumentParser(description="指数共振 1 年长周期回测")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--gate", choices=["index_ma5_dir", "same_direction", "contrarian", "non_contrary"],
                    default="index_ma5_dir")
    ap.add_argument("--codes", nargs="*", default=None)
    ap.add_argument("--reuse", action="store_true", help="复用已有 events.jsonl，仅重算门控分组")
    args = ap.parse_args()

    end = args.end or datetime.now().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")

    out_dir = BASE / "t_io" / "replay" / f"resonance_year_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"

    if args.reuse and events_path.exists():
        print(f"[reuse] 加载主事件 {events_path}")
        signals = [json.loads(l) for l in open(events_path, encoding="utf-8")]
    else:
        # ── 候选池 ──
        if args.codes:
            codes = list(args.codes)
        else:
            wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")) if WATCHLIST_FILE.exists() else {}
            codes = [c for c, v in (wl.get("stocks", {}) or {}).items()
                     if isinstance(v, dict) and not c.startswith("_example")
                     and v.get("status") in ("monitoring", "signal")]
        names = {}
        if WATCHLIST_FILE.exists():
            wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
            names = {c: v.get("name", c) for c, v in (wl.get("stocks", {}) or {}).items() if isinstance(v, dict)}
        if HOLDINGS_FILE.exists():
            h = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
            for c, v in h.items():
                if isinstance(v, dict):
                    names.setdefault(c.split("_")[0], v.get("name", c))
        codes = sorted(set(codes))
        index_codes = {ir.resolve_index(c)[0] for c in codes}
        days = _trading_days(start, end)
        print(f"[universe] {len(codes)} 只候选 + {len(index_codes)} 个指数 ｜ {start}~{end}（{len(days)} 工作日）")

        pro = _iri_tushare_pro()
        from config import PARAMS
        swing_min_bars = int(PARAMS.get("swing_min_5m_bars", 13))
        bb_upper = float(PARAMS.get("swing_bb_upper", 1.0))
        bb_lower = float(PARAMS.get("swing_bb_lower", 0.0))
        sell_rsi = float(PARAMS.get("swing_sell_rsi", 75.0))
        buy_rsi = float(PARAMS.get("swing_buy_rsi", 35.0))

        signals, n_days_data = [], 0
        day_idx = 0
        for day in days:
            day_idx += 1
            idx_5min_day = {}
            for ic in index_codes:
                df1 = _day_df(ir._index_code_to_ts(ic), day, pro)
                if df1.empty:
                    continue
                df5 = resample_to_5min(df1)
                if df5 is not None and not df5.empty:
                    df5 = add_5min_indicators(df5)
                    # 附加指数特征列（供"找更好方法"实验）
                    df5["idx_ma5"] = df5["close"].rolling(5).mean()
                    df5["idx_mom_3bar"] = df5["close"].pct_change(3)   # 15 分钟动量
                    df5["idx_mom_6bar"] = df5["close"].pct_change(6)   # 30 分钟动量
                    idx_5min_day[ic] = df5
            for code in codes:
                df1 = _day_df(_stock_code_to_ts(code), day, pro)
                if df1.empty or len(df1) < 60:
                    continue
                df5 = resample_to_5min(df1)
                if df5 is None or df5.empty or len(df5) < swing_min_bars:
                    continue
                df5 = add_5min_indicators(df5)
                n_days_data += 1
                times = df1["time"].values
                for b in range(swing_min_bars - 1, len(df5)):
                    row = df5.iloc[b]
                    bb, rsi = row.get("bb_pct_5m"), row.get("rsi_5m_p6")
                    if bb is None or rsi is None or pd.isna(bb) or pd.isna(rsi):
                        continue
                    bb, rsi = float(bb), float(rsi)
                    if bb >= bb_upper and rsi > sell_rsi:
                        action = "SELL_HIGH"
                    elif bb <= bb_lower and rsi < buy_rsi:
                        action = "BUY_LOW"
                    else:
                        continue
                    entry_ts = row["time"] + pd.Timedelta(minutes=5)
                    idx = int(np.searchsorted(times, np.datetime64(entry_ts), side="right")) - 1
                    if idx < 0 or idx >= len(df1):
                        continue
                    price = float(df1.iloc[idx]["close"])
                    ic = ir.resolve_index(code)[0]
                    ind = _index_indicators_asof(ic, idx_5min_day.get(ic), entry_ts)
                    signals.append({
                        "date": day, "ts": str(entry_ts)[11:16], "code": code, "name": names.get(code, code),
                        "action": action, "price": round(price, 3), "index_code": ic,
                        "index_bb_pct_5m": ind["bb"], "index_rsi_6_5m": ind["rsi"],
                        "index_close": ind.get("close"), "index_dif_5m": ind.get("dif"),
                        "index_dea_5m": ind.get("dea"), "index_macd_hist_5m": ind.get("macd_hist"),
                        "index_ma5_5m": ind.get("ma5"), "index_mom_3bar": ind.get("mom3"),
                        "index_mom_6bar": ind.get("mom6"),
                        "index_missing": ind["missing"],
                        "outcome": _signal_outcome(df1, idx, action, price),
                        "fwd": _forward_returns(df1, idx, price),
                    })
            if day_idx % 30 == 0 or day_idx == len(days):
                print(f"  [{day_idx}/{len(days)}] {day} 累计信号 {len(signals)} 条")
        events_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in signals) + "\n", encoding="utf-8")

    # ── 门控分组 + 输出 ──
    by_group = _apply_gate(signals, args.gate)
    n_days_data = len(set((r["date"], r["code"]) for r in signals))
    verdict, gap = _print_metrics(start, end, args.gate, signals, by_group, n_days_data)

    by_code = {}
    for r in signals:
        if r["group"] != "pass":
            continue
        d = by_code.setdefault(r["code"], {"n": 0, "wins": 0, "fails": 0, "hit_rate": None})
        d["n"] += 1
        k = {"WIN": "wins", "FAIL": "fails"}.get(r["outcome"])
        if k:
            d[k] += 1
    for c, d in by_code.items():
        d["hit_rate"] = round(d["wins"] / (d["wins"] + d["fails"]), 4) if (d["wins"] + d["fails"]) else None

    metrics = {
        "start": start, "end": end, "gate": args.gate,
        "codes": len(set(r["code"] for r in signals)), "days_with_data": n_days_data,
        "n_total": len(signals), "n_emitted": by_group["pass"]["n"],
        "by_group": by_group, "by_code": by_code,
        "hit_rate_gap": round(gap, 4) if gap is not None else None,
        "verdict": verdict,
        "note": "口径: +0.5%/-0.4%/30tick；纯两点规则复刻；n<100 样本结论仅供参考",
    }
    (out_dir / f"metrics_{args.gate}.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 输出 → {out_dir / ('metrics_' + args.gate + '.json')}")


if __name__ == "__main__":
    main()
