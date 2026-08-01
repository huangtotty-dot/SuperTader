# -*- coding: utf-8 -*-
"""
harness_backtest.py — V3.0 最小回测器（P0-C 规格，P1-P3 基础工具）

用法:
    python harness_backtest.py --codes 000988,588170 --start 2026-07-01 --end 2026-07-31
    python harness_backtest.py --codes 000988 --start 2026-07-01 --end 2026-07-31 --ab baseline
    python harness_backtest.py --codes 000988 --start 2026-07-01 --end 2026-07-31 --ab v102

输入: 股票列表 + 日期区间 + 模式开关
数据: t_io/minute_snapshots/ 本地缓存
引擎: 与实盘同源的 SignalEngine.evaluate 调用链
步进: 每分钟 1 tick (09:30-15:00)
输出:
  1) 信号流水 JSONL (含 settle 回填)
  2) 汇总报告 JSON
  3) 文本摘要 TXT

硬性要求: 确定性（同输入两跑一致）、A/B 开关、动态份数、信号结算
"""
import argparse, json, os, sys
from pathlib import Path
from datetime import datetime, timedelta, time as dtime
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_DIR = BASE_DIR / "t_io" / "minute_snapshots"


def load_shared() -> dict:
    """加载 exec 共享命名空间（与 replay_day.py 同源）。"""
    import os as _os, sys as _sys, json as _json, time as _time, logging as _logging, traceback as _traceback
    from dataclasses import dataclass, field
    from datetime import datetime as _dt, timedelta as _td, time as _dtime
    from typing import Dict, List, Optional, Any
    import requests, urllib.request, urllib.error

    for _k in ['http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'all_proxy']:
        _os.environ[_k] = ''

    class MockAkshare:
        def __getattr__(self, name):
            return lambda *args, **kwargs: pd.DataFrame()
    ak_mock = MockAkshare()
    sys.modules['akshare'] = ak_mock
    sys.modules['ak'] = ak_mock

    shared = {'akshare': ak_mock, 'ak': ak_mock}
    shared.update({
        '__name__': '__main__', '__file__': str(BASE_DIR / 'harness_backtest.py'),
        'os': _os, 'sys': _sys, 'json': _json, 'time': _time, 'logging': _logging, 'traceback': _traceback,
        'dataclass': dataclass, 'field': field,
        'datetime': _dt, 'timedelta': _td, 'dtime': _dtime,
        'Dict': Dict, 'List': List, 'Optional': Optional, 'Any': Any,
        'np': np, 'pd': pd, 'requests': requests, 'urllib': urllib,
        'urllib.request': urllib.request, 'urllib.error': urllib.error,
    })
    for mod_name in ['config', 'utils', 'data_fetcher', 'indicators', 'multi_timeframe_fetcher',
                      'signal_engine', 'position_sizer']:
        mod_path = BASE_DIR / f"{mod_name}.py"
        with open(mod_path, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(compile(code, str(mod_path), 'exec'), shared)
    return shared


def load_snapshots(code: str, date_str: str) -> pd.DataFrame:
    """加载某日分钟快照。
    路径: t_io/minute_snapshots/{year}/{month}/{code}_{date}.json
    或: t_io/minute_snapshots/{code}_{date}.json（兼容旧格式）
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    ym_dir = SNAPSHOT_DIR / str(dt.year) / f"{dt.month:02d}"
    # 尝试不带/带账户后缀的路径
    candidates = [
        ym_dir / f"{code}_{date_str}.json",
        ym_dir / f"{code}_A_{date_str}.json",
        ym_dir / f"{code}_B_{date_str}.json",
        SNAPSHOT_DIR / f"{code}_{date_str}.json",
    ]
    for p in candidates:
        if p.exists():
            break
    else:
        return pd.DataFrame()
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 快照格式: {"bars": [...], "daily_context": {...}, "signal": {...}, ...} 或直接列表
        rows = []
        if isinstance(data, list):
            snaps = data
        elif isinstance(data, dict):
            snaps = data.get("bars") or data.get("snapshots") or []
        else:
            snaps = []
        for s in snaps:
            t = s.get("time", "")
            # 如果时间只有 HH:MM，补齐日期
            if len(str(t)) <= 5:
                t = f"{date_str} {t}"
            rows.append({
                "time": t,
                "open": float(s.get("open", 0) or 0),
                "high": float(s.get("high", 0) or 0),
                "low": float(s.get("low", 0) or 0),
                "close": float(s.get("close", 0) or 0),
                "volume": float(s.get("volume", 0) or 0),
                "amount": float(s.get("amount", 0) or 0),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            df["time"] = pd.to_datetime(df["time"])
            df = df.sort_values("time").reset_index(drop=True)
        return df
    except Exception as e:
        return pd.DataFrame()


def settle_signal(sig_action: str, sig_price: float, future_bars: pd.DataFrame,
                  win_pct: float = 0.005) -> tuple:
    """§1.1 信号结算算法（回测器与实盘日志共用同一实现）。

    future_bars: 信号后最多 30 根 1 分钟 K 线
    返回: ("WIN"|"FAIL"|"VOID", settle_time or None)
    """
    fail_pct = 0.004
    for _, bar in future_bars.iterrows():
        if sig_action in ("BUY_LOW", "BUY", "ADD_POS"):
            if bar["low"] <= sig_price * (1 - fail_pct):
                return ("FAIL", str(bar.get("time", "")))
            if bar["high"] >= sig_price * (1 + win_pct):
                return ("WIN", str(bar.get("time", "")))
        else:  # SELL 类
            if bar["high"] >= sig_price * (1 + fail_pct):
                return ("FAIL", str(bar.get("time", "")))
            if bar["low"] <= sig_price * (1 - win_pct):
                return ("WIN", str(bar.get("time", "")))
    return ("VOID", None)


def run_backtest(codes: list, date_range: list, holdings_map: dict,
                 ab_mode: str = "v102", out_dir: Path = None,
                 override_params: dict = None) -> dict:
    """核心回测循环。

    Returns: dict with pushes, summary stats, per-code signals
    """
    shared = load_shared()

    # 环境隔离
    out_dir = out_dir or (BASE_DIR / "t_io" / "validation" / "p0_test")
    out_dir.mkdir(parents=True, exist_ok=True)
    shared['TRACE_DIR'] = str(out_dir)
    shared['VIRTUAL_TRADES_FILE'] = str(out_dir / "virtual_trades.json")
    if 'PERSIST_INTRADAY_STATE' in shared:
        shared['PERSIST_INTRADAY_STATE'] = False

    PARAMS = shared['PARAMS']
    STOCK_PARAMS = shared['STOCK_PARAMS']
    VIRTUAL_TRADES = shared['VIRTUAL_TRADES']
    VIRTUAL_TRADES.clear()

    # 所有回测标的的分钟状态设为 ok（跳过实盘 fetch_minute_bar 检查）
    MINUTE_FETCH_STATUS = shared.get('MINUTE_FETCH_STATUS', {})
    for c in codes:
        MINUTE_FETCH_STATUS[c] = "ok"
    shared['HOLDINGS'] = {c: dict(h) for c, h in holdings_map.items()}
    shared['VIRTUAL_TRADES'] = VIRTUAL_TRADES

    # A/B 模式下的参数覆盖
    if ab_mode == "baseline":
        PARAMS["factor_weight_5m_trend"] = 0.0
        PARAMS["factor_weight_5m_rsi"] = 0.0
        # 门控全 1.0（在 TrendRegime 层面通过覆盖趋势状态实现）
        # 简化：直接修改 FACTOR_WEIGHTS
        if 'FACTOR_WEIGHTS' in shared:
            shared['FACTOR_WEIGHTS']["factor_weight_5m_trend"] = 0.0
            shared['FACTOR_WEIGHTS']["factor_weight_5m_rsi"] = 0.0

    if override_params:
        if "PARAMS" in override_params:
            PARAMS.update(override_params["PARAMS"])
        if "STOCK_PARAMS" in override_params:
            for code, sp in override_params["STOCK_PARAMS"].items():
                STOCK_PARAMS.setdefault(code, {}).update(sp)

    engine = shared['SignalEngine']()
    add_indicators = shared['add_indicators']

    # 基线模式：完全关闭趋势层（TrendRegime=None 跳过整个趋势块）
    if ab_mode == "baseline":
        shared['TrendRegime'] = None  # 让 evaluate() 的 if TrendRegime is not None 失败
        engine.factor_weights = dict(shared.get('FACTOR_WEIGHTS', {}))
        engine.factor_weights["factor_weight_5m_trend"] = 0.0
        engine.factor_weights["factor_weight_5m_rsi"] = 0.0

    all_signals = []
    daily_stats = {}

    for date_str in date_range:
        day_signals = []
        day_bars = {}
        for code in codes:
            df = load_snapshots(code, date_str)
            if not df.empty:
                day_bars[code] = df

        if not day_bars:
            continue

        # 合并所有股票的分钟时间点
        all_times = set()
        for df in day_bars.values():
            all_times.update(df["time"].tolist())
        bar_times = sorted(all_times)

        for bt in bar_times:
            shared['SIM_NOW'] = bt.to_pydatetime()
            hhmm = bt.hour * 100 + bt.minute
            # 只看交易时段
            if hhmm < 930 or (hhmm > 1130 and hhmm < 1300) or hhmm > 1500:
                continue

            for code in codes:
                if code not in day_bars:
                    continue
                dfc = day_bars[code]
                sub = dfc[dfc["time"] <= bt].copy()
                if len(sub) < 5:
                    continue
                df_ind = add_indicators(sub)
                price = float(df_ind.iloc[-1]["close"])
                holding = holdings_map.get(code, {"name": code, "cost": price,
                            "qty": 0, "base": 0, "t_qty": 0, "type": "stock"})
                daily_ctx = {"daily_status": "ok", "daily_buy_t_ok": True,
                             "index_regime": "range", "intraday_alerts": []}

                try:
                    buy_score, sell_score, sig = engine.evaluate(
                        code, holding.get("name", code), df_ind,
                        holding, daily_ctx=daily_ctx)
                except Exception as e:
                    continue

                if sig is None or sig.action not in ("BUY_LOW", "ADD_POS", "SELL_HIGH"):
                    continue

                signal_rec = {
                    "ts": bt.strftime("%Y-%m-%d %H:%M:%S"),
                    "code": code, "name": holding.get("name", code),
                    "action": sig.action, "price": round(price, 2),
                    "buy_score": round(float(buy_score), 1),
                    "sell_score": round(float(sell_score), 1),
                    "threshold": 42.0,
                    "trend_state": "NEUTRAL", "trend_confidence": 0.0,
                    "rsi_5m": 50.0, "dif_5m": 0.0, "dea_5m": 0.0,
                    "rsi5_buy_trigger": False, "rsi5_sell_trigger": False,
                    "t_mode": "long",
                    "settle_result": None, "settle_time": None,
                }

                # V3.0 trend info if active
                if code in engine.trend_regimes:
                    tr = engine.trend_regimes[code]
                    signal_rec["trend_state"] = tr.state.value
                    signal_rec["trend_confidence"] = round(tr.confidence, 3)
                    signal_rec["rsi_5m"] = round(tr._last_rsi, 1)
                    signal_rec["dif_5m"] = round(tr._last_dif, 4)
                    signal_rec["dea_5m"] = round(tr._last_dea, 4)
                    signal_rec["rsi5_buy_trigger"] = tr.rsi_buy_trigger
                    signal_rec["rsi5_sell_trigger"] = tr.rsi_sell_trigger

                day_signals.append(signal_rec)

        # 盘后结算：用当日K线回填 settle_result
        for sig_rec in day_signals:
            code = sig_rec["code"]
            sig_ts = pd.Timestamp(sig_rec["ts"])
            if code in day_bars:
                future = day_bars[code][day_bars[code]["time"] > sig_ts].head(30)
                if not future.empty:
                    is_etf = holdings_map.get(code, {}).get("type") == "etf"
                    wp = 0.003 if is_etf else 0.005
                    result, settle_ts = settle_signal(
                        sig_rec["action"], sig_rec["price"], future, wp)
                    sig_rec["settle_result"] = result
                    sig_rec["settle_time"] = settle_ts

        all_signals.extend(day_signals)
        daily_stats[date_str] = {"signals": len(day_signals),
                                 "wins": sum(1 for s in day_signals if s["settle_result"] == "WIN"),
                                 "fails": sum(1 for s in day_signals if s["settle_result"] == "FAIL"),
                                 "voids": sum(1 for s in day_signals if s["settle_result"] == "VOID")}

    # 汇总
    wins = sum(1 for s in all_signals if s["settle_result"] == "WIN")
    fails = sum(1 for s in all_signals if s["settle_result"] == "FAIL")
    voids = sum(1 for s in all_signals if s["settle_result"] == "VOID")
    total_decided = wins + fails
    win_rate = wins / total_decided if total_decided > 0 else 0

    return {
        "signals": all_signals,
        "summary": {
            "total": len(all_signals), "wins": wins, "fails": fails, "voids": voids,
            "win_rate": round(win_rate, 4),
            "void_rate": round(voids / len(all_signals), 4) if all_signals else 0,
            "daily_stats": daily_stats,
            "ab_mode": ab_mode,
        }
    }


def main():
    ap = argparse.ArgumentParser(description="V3.0 最小回测器")
    ap.add_argument("--codes", default="000988,588170,600176,600481,603667",
                    help="逗号分隔股票代码")
    ap.add_argument("--start", default="2026-07-24", help="起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default="2026-07-24", help="结束日期 YYYY-MM-DD")
    ap.add_argument("--ab", choices=["baseline", "v102"], default="v102",
                    help="A/B 模式: baseline(趋势层全关) | v102(全开)")
    ap.add_argument("--out", default=None, help="输出目录")
    args = ap.parse_args()

    codes = [c.strip() for c in args.codes.split(",")]
    start_dt = datetime.strptime(args.start, "%Y-%m-%d")
    end_dt = datetime.strptime(args.end, "%Y-%m-%d")
    dates = []
    dt = start_dt
    while dt <= end_dt:
        dates.append(dt.strftime("%Y-%m-%d"))
        dt += timedelta(days=1)

    # 默认持仓映射（从 holdings.json 读取）
    holdings_file = BASE_DIR / "holdings.json"
    holdings_map = {}
    if holdings_file.exists():
        with open(holdings_file, 'r', encoding='utf-8') as f:
            raw = json.load(f)
            # holdings.json 格式: {code: {name, cost, qty, base, t_qty, type, account, pre_close}}
            for code, h in raw.items():
                clean = code.split("_")[0] if "_" in code else code
                holdings_map[clean] = h
    # 兜底：简单持仓
    for c in codes:
        if c not in holdings_map:
            holdings_map[c] = {"name": c, "cost": 50.0, "qty": 500,
                               "base": 500, "t_qty": 500, "type": "stock"}

    out_dir = Path(args.out) if args.out else (BASE_DIR / "t_io" / "validation" / "p0_test")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Harness backtest: {len(codes)} stocks × {len(dates)} days, mode={args.ab}")
    result = run_backtest(codes, dates, holdings_map, ab_mode=args.ab,
                          out_dir=out_dir)

    s = result["summary"]
    print(f"\n=== SUMMARY ({args.ab}) ===")
    print(f"Signals: {s['total']}  WIN={s['wins']} FAIL={s['fails']} VOID={s['voids']}")
    print(f"Win rate: {s['win_rate']:.1%} (decided: {s['wins']+s['fails']})")
    if s['total'] > 0:
        print(f"Void rate: {s['void_rate']:.1%}")

    # 写入输出
    signals_path = out_dir / f"signals_{args.ab}.jsonl"
    with open(signals_path, 'w', encoding='utf-8') as f:
        for sig in result["signals"]:
            f.write(json.dumps(sig, ensure_ascii=False, default=str) + "\n")

    summary_path = out_dir / f"summary_{args.ab}.json"
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(s, f, ensure_ascii=False, indent=2, default=str)

    txt_path = out_dir / f"report_{args.ab}.txt"
    with open(txt_path, 'w', encoding='ascii', errors='replace') as f:
        f.write(f"=== BACKTEST REPORT ({args.ab}) ===\n")
        f.write(f"Codes: {','.join(codes)}\n")
        f.write(f"Period: {args.start} ~ {args.end}\n")
        f.write(f"Signals: {s['total']}  WIN={s['wins']} FAIL={s['fails']} VOID={s['voids']}\n")
        f.write(f"Win rate: {s['win_rate']:.1%}\n")

    print(f"\nOutput: {out_dir}")
    print(f"  {signals_path}")
    print(f"  {summary_path}")
    print(f"  {txt_path}")


if __name__ == "__main__":
    main()
