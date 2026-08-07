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
# 数据源可用环境变量 T_SNAPSHOT_DIR 切换（默认原腾讯快照目录；统一口径复测指向 minute_snapshots_ts）
SNAPSHOT_DIR = Path(os.environ.get("T_SNAPSHOT_DIR", str(BASE_DIR / "t_io" / "minute_snapshots")))
# v1.1.1: E2 日上下文注入 — 预热期分钟库(MA60 预热)目录，默认指向 e2_daily_gate/minute_snapshots_pre
PRE_SNAPSHOT_DIR = Path(os.environ.get("T_PRE_SNAPSHOT_DIR", str(BASE_DIR / "t_io" / "validation" / "e2_daily_gate" / "minute_snapshots_pre")))
# v1.1.1: 默认开启真实日上下文(键修复后世界); T_DAILY_CTX=0 回退旧硬编码(死门控, 用于门控自身 A/B)
DAILY_CTX_ENABLED = os.environ.get("T_DAILY_CTX", "1") != "0"

_LEGACY_DAILY_CTX = {"daily_status": "ok", "daily_buy_t_ok": True,
                     "index_regime": "range", "intraday_alerts": []}

_daily_rows_cache: dict = {}

def _daily_rows(code: str) -> dict:
    """聚合分钟快照为日线 {date: row}(预热期+样本期)。缓存每股一次。"""
    if code not in _daily_rows_cache:
        rows = {}
        for root in (PRE_SNAPSHOT_DIR, SNAPSHOT_DIR):
            for fp in root.glob(f"*/*/{code}_*.json"):
                try:
                    d = json.load(open(fp, encoding="utf-8"))
                except Exception:
                    continue
                bars = d.get("bars") or []
                if not bars:
                    continue
                rows[d["date"]] = {"date": d["date"], "open": bars[0]["open"],
                                   "close": bars[-1]["close"],
                                   "high": max(b["high"] for b in bars),
                                   "low": min(b["low"] for b in bars),
                                   "volume": sum(b.get("volume", 0) for b in bars),
                                   "amount": sum(b.get("amount", 0) for b in bars)}
        _daily_rows_cache[code] = rows
    return _daily_rows_cache[code]

def build_daily_ctx(shared: dict, code: str, date_str: str, price: float) -> dict:
    """v1.1.1: 生产同源日上下文(无前视: 仅用 date_str 之前日线; ref_price=当前tick价)。
    离线无基准指数分钟线, index_regime 固定 'range'(与原 harness 口径一致)。"""
    if not DAILY_CTX_ENABLED:
        return dict(_LEGACY_DAILY_CTX)
    rows = _daily_rows(code)
    prior = [rows[d] for d in sorted(rows) if d < date_str]
    if len(prior) < 10:
        return dict(_LEGACY_DAILY_CTX)
    try:
        ctx = shared["_build_daily_context_from_df"](code, pd.DataFrame(prior), current_price=price)
    except Exception:
        return dict(_LEGACY_DAILY_CTX)
    ctx["index_regime"] = "range"
    ctx.setdefault("intraday_alerts", [])
    return ctx


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


def classify_day_type(df: pd.DataFrame) -> str:
    """R5修正: 日型分类 — 用pre_close做日涨幅，反转日优先判定"""
    if df.empty or len(df) < 30:
        return "unknown"
    open_p = float(df.iloc[0]["open"])
    close_p = float(df.iloc[-1]["close"])
    day_ret = (close_p - open_p) / open_p if open_p > 0 else 0
    # 均价线上方时间占比（用 (H+L)/2 近似）
    mid_prices = [(float(r["high"]) + float(r["low"])) / 2 for _, r in df.iterrows()]
    avg_price = sum(mid_prices) / len(mid_prices) if mid_prices else open_p
    above_ratio = sum(1 for mp in mid_prices if mp > avg_price) / len(mid_prices)
    # 开盘1小时方向 vs 收盘方向（反转日优先判定）
    end_1h = df.iloc[0]["time"] + pd.Timedelta(hours=1)
    first_hour = df[df["time"] <= end_1h]
    fh_open = float(first_hour.iloc[0]["open"]) if not first_hour.empty else open_p
    fh_close = float(first_hour.iloc[-1]["close"]) if not first_hour.empty else open_p
    fh_ret = (fh_close - fh_open) / fh_open if fh_open > 0 else 0
    reversed_dir = (fh_ret > 0.003 and day_ret < -0.005) or (fh_ret < -0.003 and day_ret > 0.005)
    # R5: 反转日优先判定（在单边之前）
    if reversed_dir and abs(day_ret) >= 0.008:
        return "reversal_day"
    elif day_ret >= 0.01 and above_ratio >= 0.55:
        return "bull_day"
    elif day_ret <= -0.01 and above_ratio <= 0.45:
        return "bear_day"
    else:
        return "chop_day"


def compute_p1_metrics(trend_timelines: dict, day_bars: dict) -> dict:
    """R3修正: P1 趋势方向指标 — NEUTRAL按时长、STRONG准确率、切换方向一致率"""
    per_stock = {}
    for tkey, timeline in trend_timelines.items():
        date_str, code = tkey.split(":", 1)
        if not timeline:
            continue
        if code not in per_stock:
            per_stock[code] = {"bull_match": 0, "bear_match": 0, "bull_total": 0, "bear_total": 0,
                               "neutral_segments": 0, "total_segments": 0,
                               "neutral_minutes_est": 0, "total_minutes_est": 0,
                               "strong_bull_correct": 0, "strong_bull_total": 0,
                               "strong_bear_correct": 0, "strong_bear_total": 0,
                               "reversal_detected": 0, "reversal_total": 0}

        # 日型判定
        df = day_bars.get(date_str, {}).get(code)
        dtype = classify_day_type(df) if (df is not None and not df.empty) else "unknown"

        ps = per_stock[code]

        # W3: NEUTRAL真实时长（相邻转移时间差，最后一个状态延续到15:00收盘）
        for i, (t_str, s, _) in enumerate(timeline):
            try:
                t_mins = int(t_str[:2]) * 60 + int(t_str[3:5])
            except Exception:
                t_mins = 0
            if i + 1 < len(timeline):
                try:
                    next_t = int(timeline[i+1][0][:2]) * 60 + int(timeline[i+1][0][3:5])
                except Exception:
                    next_t = t_mins + 5
            else:
                next_t = 15 * 60  # 收盘15:00
            duration = max(next_t - t_mins, 1)
            ps["total_segments"] += 1
            if s == "NEUTRAL":
                ps["neutral_segments"] += 1
                ps["neutral_minutes_est"] += duration
            ps["total_minutes_est"] += duration

        # W3: 午盘前众数判定趋势（去前视）；若午盘全NEUTRAL回退全天
        am_cutoff = 11 * 60 + 30
        am_states = []
        for t_str, s, _ in timeline:
            try:
                tm = int(t_str[:2]) * 60 + int(t_str[3:5])
            except Exception:
                tm = 0
            if tm <= am_cutoff and s != "NEUTRAL":
                am_states.append(s)
        states = am_states if am_states else [s for _, s, _ in timeline if s != "NEUTRAL"]
        # X1: 确定化众数 — 按出现顺序计数，平局保留最先出现的状态
        # （原 max(set(...), key=count) 依赖 str hash 随机化，平局日结果跨进程不稳定）
        if states:
            _cnt = {}
            for _s in states:
                _cnt[_s] = _cnt.get(_s, 0) + 1
            _best = max(_cnt.values())
            dominant = next(_s for _s in states if _cnt[_s] == _best)
        else:
            dominant = "NEUTRAL"

        if dtype == "bull_day" and dominant in ("BULL", "STRONG_BULL"):
            ps["bull_match"] += 1
        if dtype == "bull_day":
            ps["bull_total"] += 1
        if dtype == "bear_day" and dominant in ("BEAR", "STRONG_BEAR"):
            ps["bear_match"] += 1
        if dtype == "bear_day":
            ps["bear_total"] += 1
        if dtype == "reversal_day":
            ps["reversal_total"] += 1

        # STRONG 档准确率：带30分钟后验证（用5分钟粒度估算）
        for i, (t_str, s, _) in enumerate(timeline):
            if s == "STRONG_BULL":
                ps["strong_bull_total"] += 1
                if i + 6 < len(timeline):  # 30分钟后=6根5分K
                    later_state = timeline[i + 6][1]
                    if later_state in ("BULL", "STRONG_BULL"):
                        ps["strong_bull_correct"] += 1
            elif s == "STRONG_BEAR":
                ps["strong_bear_total"] += 1
                if i + 6 < len(timeline):
                    later_state = timeline[i + 6][1]
                    if later_state in ("BEAR", "STRONG_BEAR"):
                        ps["strong_bear_correct"] += 1

    # 汇总
    total_bull = sum(ps["bull_total"] for ps in per_stock.values())
    total_bull_match = sum(ps["bull_match"] for ps in per_stock.values())
    total_bear = sum(ps["bear_total"] for ps in per_stock.values())
    total_bear_match = sum(ps["bear_match"] for ps in per_stock.values())
    total_neutral_min = sum(ps["neutral_minutes_est"] for ps in per_stock.values())
    total_all_min = sum(ps["total_minutes_est"] for ps in per_stock.values())
    total_strong_bull = sum(ps["strong_bull_total"] for ps in per_stock.values())
    total_strong_bull_ok = sum(ps["strong_bull_correct"] for ps in per_stock.values())
    total_strong_bear = sum(ps["strong_bear_total"] for ps in per_stock.values())
    total_strong_bear_ok = sum(ps["strong_bear_correct"] for ps in per_stock.values())

    return {
        "per_stock": per_stock,
        "bull_consistency": round(total_bull_match / total_bull, 3) if total_bull > 0 else None,
        "bear_consistency": round(total_bear_match / total_bear, 3) if total_bear > 0 else None,
        "overall_consistency": round((total_bull_match + total_bear_match) / (total_bull + total_bear), 3) if (total_bull + total_bear) > 0 else None,
        "neutral_ratio": round(total_neutral_min / total_all_min, 3) if total_all_min > 0 else None,
        "bias_ratio": round(total_bull / total_bear, 3) if total_bear > 0 else None,
        "sample_days_bull": total_bull,
        "sample_days_bear": total_bear,
        "strong_bull_accuracy": round(total_strong_bull_ok / total_strong_bull, 3) if total_strong_bull > 0 else None,
        "strong_bear_accuracy": round(total_strong_bear_ok / total_strong_bear, 3) if total_strong_bear > 0 else None,
    }


def compute_closed_loop(signals: list, holdings_map: dict) -> dict:
    """W2修正: T闭环 — 当日FIFO配对 + 印花税记卖出腿 + qty入信号"""
    from collections import deque
    commission = 0.00015
    stamp_tax = 0.0005  # 卖出侧印花税（仅卖出腿）
    per_stock = {}
    for sig in signals:
        code = sig["code"]
        if code not in per_stock:
            per_stock[code] = {"long_positions": deque(), "short_positions": deque(),
                               "closed_pairs": [], "net_pnl": 0.0, "open_long": 0, "open_short": 0}

    # 按日期分组，当日FIFO配对
    by_date = {}
    for sig in signals:
        d = sig["ts"][:10]
        by_date.setdefault(d, []).append(sig)

    for date_str, day_sigs in sorted(by_date.items()):
        day_positions = {}  # per-code daily positions, reset each day
        for sig in sorted(day_sigs, key=lambda s: s["ts"]):
            code = sig["code"]
            if code not in day_positions:
                day_positions[code] = {"long": deque(), "short": deque()}
            dp = day_positions[code]
            ps = per_stock[code]
            action = sig["action"]
            price = sig["price"]
            qty = sig.get("qty", 100)

            if action in ("BUY_LOW", "ADD_POS"):
                if dp["short"]:
                    sell_entry = dp["short"].popleft()
                    pnl = (sell_entry["price"] - price) * qty
                    fee = sell_entry["price"] * qty * (commission + stamp_tax) + price * qty * commission
                    ps["net_pnl"] += pnl - fee
                    ps["closed_pairs"].append({"type": "short_close", "date": date_str,
                        "sell_ts": sell_entry["ts"], "buy_ts": sig["ts"],
                        "sell_price": sell_entry["price"], "buy_price": price,
                        "qty": qty, "pnl": round(pnl - fee, 2)})
                else:
                    dp["long"].append({"ts": sig["ts"], "price": price, "qty": qty})
            elif action == "SELL_HIGH":
                if dp["long"]:
                    buy_entry = dp["long"].popleft()
                    pnl = (price - buy_entry["price"]) * qty
                    fee = buy_entry["price"] * qty * commission + price * qty * (commission + stamp_tax)
                    ps["net_pnl"] += pnl - fee
                    ps["closed_pairs"].append({"type": "long_close", "date": date_str,
                        "buy_ts": buy_entry["ts"], "sell_ts": sig["ts"],
                        "buy_price": buy_entry["price"], "sell_price": price,
                        "qty": qty, "pnl": round(pnl - fee, 2)})
                else:
                    dp["short"].append({"ts": sig["ts"], "price": price, "qty": qty})

        # 当日未配对计入 open
        for code, dp in day_positions.items():
            ps = per_stock[code]
            for e in dp["long"]:
                ps["open_long"] += e["qty"]
            for e in dp["short"]:
                ps["open_short"] += e["qty"]

    total_pnl = sum(ps["net_pnl"] for ps in per_stock.values())
    total_closed = sum(len(ps["closed_pairs"]) for ps in per_stock.values())
    return {"per_stock": {c: {"net_pnl": round(p["net_pnl"], 2),
                               "closed": len(p["closed_pairs"]),
                               "open_long": p["open_long"],
                               "open_short": p["open_short"]}
                          for c, p in per_stock.items()},
            "total_net_pnl": round(total_pnl, 2),
            "total_closed_pairs": total_closed}


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

    # v1.1.1: 变体A实验开关(默认关) — T_GATE_VARIANT_A=1 时 below_ma5_weak且slope>=0 放行
    if os.environ.get("T_GATE_VARIANT_A") == "1":
        PARAMS["daily_gate_allow_below_ma5_rebound"] = True

    # E1: 引擎买阈基线注入(验证用途, 默认42不变) — 引擎软消费 PARAMS["engine_buy_threshold_base"](signal_engine.py:524)
    if os.environ.get("T_BUY_BONUS_MIN_SCORE"):
        PARAMS["engine_buy_threshold_base"] = float(os.environ["T_BUY_BONUS_MIN_SCORE"])
    # W32 C1-final: 接回解耦注入(验证用途, 默认关=生产行为不变) — 引擎软消费 PARAMS["buyback_bypass_gates"]
    if os.environ.get("T_BUYBACK_BYPASS_GATES") == "1":
        PARAMS["buyback_bypass_gates"] = True

    if override_params:
        if "PARAMS" in override_params:
            PARAMS.update(override_params["PARAMS"])
        if "STOCK_PARAMS" in override_params:
            for code, sp in override_params["STOCK_PARAMS"].items():
                STOCK_PARAMS.setdefault(code, {}).update(sp)

    # 基线模式：完全关闭趋势层
    if ab_mode == "baseline":
        shared['TrendRegime'] = None  # 必须 BEFORE SignalEngine() 创建
        shared['FACTOR_WEIGHTS']["factor_weight_5m_trend"] = 0.0
        shared['FACTOR_WEIGHTS']["factor_weight_5m_rsi"] = 0.0

    engine = shared['SignalEngine']()
    add_indicators = shared['add_indicators']

    if ab_mode == "baseline":
        engine.factor_weights = dict(shared.get('FACTOR_WEIGHTS', {}))

    all_signals = []
    daily_stats = {}
    # P1: per-day trend timelines
    trend_timelines = {}  # {f"{date_str}:{code}": [(time, state, conf), ...]}
    day_bars_cache = {}   # {date_str: {code: df}}

    for date_str in date_range:
        day_signals = []
        day_bars = {}
        for code in codes:
            df = load_snapshots(code, date_str)
            if not df.empty:
                day_bars[code] = df

        if not day_bars:
            continue

        day_bars_cache[date_str] = day_bars

        # 合并所有股票的分钟时间点
        all_times = set()
        for df in day_bars.values():
            all_times.update(df["time"].tolist())
        bar_times = sorted(all_times)

        # 预计算每只股票全天指标（避免每分钟重复计算 — 10x 提速）
        day_indicators = {}
        for code in codes:
            if code in day_bars:
                day_indicators[code] = add_indicators(day_bars[code].copy())

        for bt in bar_times:
            shared['SIM_NOW'] = bt.to_pydatetime()
            hhmm = bt.hour * 100 + bt.minute
            if hhmm < 930 or (hhmm > 1130 and hhmm < 1300) or hhmm > 1500:
                continue

            for code in codes:
                if code not in day_indicators:
                    continue
                df_full = day_indicators[code]
                sub = df_full[df_full["time"] <= bt]
                if len(sub) < 5:
                    continue
                price = float(sub.iloc[-1]["close"])
                holding = holdings_map.get(code, {"name": code, "cost": price,
                            "qty": 0, "base": 0, "t_qty": 0, "type": "stock"})
                daily_ctx = build_daily_ctx(shared, code, date_str, price)

                # P1: 记录趋势状态（每分钟最后已知状态）
                _tkey = f"{date_str}:{code}"
                if _tkey not in trend_timelines:
                    trend_timelines[_tkey] = []
                try:
                    buy_score, sell_score, sig = engine.evaluate(
                        code, holding.get("name", code), sub,
                        holding, daily_ctx=daily_ctx)
                except Exception as e:
                    continue

                # P1: 记录每5分钟边界的趋势状态（去重：同状态不重复记）
                if code in engine.trend_regimes:
                    tr = engine.trend_regimes[code]
                    _ts = tr.state.value
                    _tl = trend_timelines[_tkey]
                    if not _tl or _tl[-1][1] != _ts:
                        _tl.append((bt.strftime("%H:%M"), _ts, round(tr.confidence, 3)))

                if sig is None or sig.action not in ("BUY_LOW", "ADD_POS", "SELL_HIGH"):
                    continue

                # R1: 信号事件化 — 应用通知阈值 + 同方向段去重
                _sp = STOCK_PARAMS.get(code, {})
                _nth_buy = _sp.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
                _nth_sell_early = PARAMS.get("notify_sell_early_threshold", 75)
                _nth_sell = _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
                # X9: 记录阈值阶梯实验环境变量覆盖（仅验证用途，不改默认值）
                # E1: 买侧通知阈覆盖（默认个股40/43逻辑不变，对齐引擎降阈档）
                if os.environ.get("T_NOTIFY_BUY"):
                    _nth_buy = float(os.environ["T_NOTIFY_BUY"])
                if os.environ.get("T_NOTIFY_SELL"):
                    _nth_sell = float(os.environ["T_NOTIFY_SELL"])
                if os.environ.get("T_NOTIFY_SELL_EARLY"):
                    _nth_sell_early = float(os.environ["T_NOTIFY_SELL_EARLY"])
                if sig.action in ("BUY_LOW", "ADD_POS"):
                    _nth = _nth_buy
                elif hhmm < 1000:
                    _nth = _nth_sell_early
                else:
                    _nth = _nth_sell

                if float(sig.score) < _nth:
                    continue  # 低于通知阈值，不记录

                # 同方向信号段去重：只记录段首（方向变化或间隔>5分钟）
                _seg_key = f"{code}:{sig.action}"
                _last_seg = getattr(engine, '_last_signal_seg', {})
                if _seg_key in _last_seg:
                    _last_ts = _last_seg[_seg_key]
                    _gap = (bt - _last_ts).total_seconds() / 60
                    if _gap < 5:  # 5分钟内同方向视为同一段
                        continue
                if not hasattr(engine, '_last_signal_seg'):
                    engine._last_signal_seg = {}
                engine._last_signal_seg[_seg_key] = bt

                # R2: 回测簿记对齐 — record_signal冷却 + record_trade_action闭环
                try:
                    engine.record_signal(code, sig.action, price, float(sig.score))
                except Exception:
                    pass
                # 动态份数 + 记录交易动作（触发 pending_sells/awaiting_buyback）
                _merged_params = {**PARAMS, **STOCK_PARAMS.get(code, {})}
                _calc_qty = shared.get('calc_buy_qty') if sig.action in ("BUY_LOW", "ADD_POS") else shared.get('calc_sell_qty')
                _qty = 0
                if _calc_qty:
                    try:
                        _qty = _calc_qty(code, holding, None, float(sig.score), 42.0,
                                         params=_merged_params, virtual_trades=VIRTUAL_TRADES,
                                         index_ctx=daily_ctx, current_price=price)
                        _qty = int(_qty or 0)
                    except Exception:
                        _qty = 0
                if _qty > 0:
                    try:
                        engine.record_trade_action(code, sig.action, _qty, price=price)
                    except Exception:
                        pass

                signal_rec = {
                    "ts": bt.strftime("%Y-%m-%d %H:%M:%S"),
                    "code": code, "name": holding.get("name", code),
                    "action": sig.action, "price": price, "qty": _qty,
                    "buy_score": round(float(buy_score), 1),
                    "sell_score": round(float(sell_score), 1),
                    "threshold": _nth,
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

    # 汇总（R5: settle=None单独统计，不混入VOID）
    wins = sum(1 for s in all_signals if s["settle_result"] == "WIN")
    fails = sum(1 for s in all_signals if s["settle_result"] == "FAIL")
    voids = sum(1 for s in all_signals if s["settle_result"] == "VOID")
    unsettled = sum(1 for s in all_signals if s["settle_result"] is None)
    total_decided = wins + fails
    win_rate = wins / total_decided if total_decided > 0 else 0

    # P1: 日型分类 + 趋势方向一致率
    p1_metrics = compute_p1_metrics(trend_timelines, day_bars_cache)
    # R4: T闭环配对
    closed_loop = compute_closed_loop(all_signals, holdings_map)

    return {
        "signals": all_signals,
        "trend_timelines": trend_timelines,
        "p1_metrics": p1_metrics,
        "closed_loop": closed_loop,
        "summary": {
            "total": len(all_signals), "wins": wins, "fails": fails, "voids": voids,
            "unsettled": unsettled,
            "win_rate": round(win_rate, 4),
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

    # 默认持仓映射（从 holdings.json 读取；W32: T_HOLDINGS_FILE 可注入历史快照，验证用途默认不变）
    holdings_file = Path(os.environ.get("T_HOLDINGS_FILE", str(BASE_DIR / "holdings.json")))
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
    print(f"\n=== SIGNALS ({args.ab}) ===")
    print(f"Signals: {s['total']}  WIN={s['wins']} FAIL={s['fails']} VOID={s['voids']} UNSETTLED={s.get('unsettled', 0)}")
    print(f"Win rate: {s['win_rate']:.1%} (decided: {s['wins']+s['fails']})")

    # P1 metrics
    p1 = result.get("p1_metrics", {})
    if p1:
        print(f"\n=== P1 TREND DIRECTION ({args.ab}) ===")
        oc = p1.get("overall_consistency")
        print(f"Direction consistency: {oc:.1%}" if oc is not None else "Direction consistency: N/A")
        bc = p1.get("bull_consistency")
        print(f"  Bull day match: {bc:.1%}" if bc is not None else "  Bull day match: N/A")
        bec = p1.get("bear_consistency")
        print(f"  Bear day match: {bec:.1%}" if bec is not None else "  Bear day match: N/A")
        nr = p1.get('neutral_ratio')
        print(f"NEUTRAL ratio (time): {nr:.1%}" if nr is not None else "NEUTRAL ratio: N/A")
        bias = p1.get("bias_ratio")
        print(f"Bias (BULL/BEAR days): {bias:.2f}" if bias else "Bias: N/A")
        sba = p1.get("strong_bull_accuracy")
        print(f"STRONG_BULL accuracy: {sba:.1%}" if sba is not None else "STRONG_BULL acc: N/A")
        sbea = p1.get("strong_bear_accuracy")
        print(f"STRONG_BEAR accuracy: {sbea:.1%}" if sbea is not None else "STRONG_BEAR acc: N/A")
        print(f"Sample: {p1.get('sample_days_bull', 0)} bull + {p1.get('sample_days_bear', 0)} bear")

    # R4: T闭环
    cl = result.get("closed_loop", {})
    if cl:
        print(f"\n=== T CLOSED LOOP ({args.ab}) ===")
        print(f"Total net PnL: {cl.get('total_net_pnl', 0):.2f}")
        print(f"Closed pairs: {cl.get('total_closed_pairs', 0)}")
        for c, p in sorted((cl.get("per_stock") or {}).items()):
            print(f"  {c}: PnL={p['net_pnl']:.2f} closed={p['closed']} open_long={p['open_long']} open_short={p['open_short']}")

    # 写入信号
    signals_path = out_dir / f"signals_{args.ab}.jsonl"
    with open(signals_path, 'w', encoding='utf-8') as f:
        for sig in result["signals"]:
            f.write(json.dumps(sig, ensure_ascii=False, default=str) + "\n")

    # 写入趋势时间线
    tl_path = out_dir / f"trend_timeline_{args.ab}.jsonl"
    with open(tl_path, 'w', encoding='utf-8') as f:
        for tkey, tl in result.get("trend_timelines", {}).items():
            f.write(json.dumps({"key": tkey, "timeline": tl}, ensure_ascii=False) + "\n")

    summary_path = out_dir / f"summary_{args.ab}.json"
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump({**s, "p1_metrics": p1}, f, ensure_ascii=False, indent=2, default=str)

    txt_path = out_dir / f"report_{args.ab}.txt"
    with open(txt_path, 'w', encoding='ascii', errors='replace') as f:
        f.write(f"=== BACKTEST REPORT ({args.ab}) ===\n")
        f.write(f"Codes: {','.join(codes)}\n")
        f.write(f"Period: {args.start} ~ {args.end}\n")
        f.write(f"Signals: {s['total']}  WIN={s['wins']} FAIL={s['fails']} VOID={s['voids']}\n")
        f.write(f"Win rate: {s['win_rate']:.1%}\n")
        if oc is not None:
            f.write(f"\nP1 Trend Direction:\n")
            f.write(f"  Consistency: {oc:.1%}\n")
            f.write(f"  NEUTRAL ratio: {p1.get('neutral_ratio', 0):.1%}\n")

    print(f"\nOutput: {out_dir}")


if __name__ == "__main__":
    main()
