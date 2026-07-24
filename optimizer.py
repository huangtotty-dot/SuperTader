# -*- coding: utf-8 -*-
"""华工科技 000988 参数寻优系统

使用 Optuna 贝叶斯优化（或网格搜索）来自动搜索 VWAP 深V低吸策略的最佳参数。
支持 In-Sample 训练 + Out-of-Sample 盲测验证。

数据来源：优先使用 t_io/cache/tushare_mins/000988/ 下的缓存分钟CSV，
          缺失日期自动通过 backtest_v127_000988 的下载函数补全。
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import os
import sys
import time
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
CACHE_DIR = BASE_DIR / "t_io" / "cache" / "tushare_mins"
OUT_DIR = BASE_DIR / "t_io" / "optimizer"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ── 默认参数 ────────────────────────────────────────────────
INITIAL_CAPITAL = 200000.0
FIXED_QTY = 200         # 每笔固定股数
MAX_BUYS = 3
MAX_SELLS = 3
STEP = 3                # 分钟评估步长（3=每3分钟评估一次，≈3倍提速）
COMMISSION = 0.00015
STAMP_TAX = 0.0005
SLIPPAGE = 0.01

PARAM_DEFAULTS = {
    "vwap_buy_deviation": -0.020,
    "take_profit_pct": 0.010,
    "buy_confirm_min_score": 18,
    "notify_sell_threshold": 65,
    "notify_buy_threshold": 68,
    "stock_qty_strong_pct": 0.40,
    "stock_qty_base_pct": 0.30,
    "stock_rebuild_strong_pct": 0.80,
    "stock_first_add_pct": 0.20,
}

PARAM_SPACE = {
    "vwap_buy_deviation": {"low": -0.035, "high": -0.015, "default": -0.020, "label": "VWAP偏离买入阈值"},
    "take_profit_pct": {"low": 0.005, "high": 0.025, "default": 0.010, "label": "止盈比例"},
    "buy_confirm_min_score": {"low": 15, "high": 30, "default": 18, "label": "买入确认最低分"},
    "notify_sell_threshold": {"low": 40, "high": 70, "default": 65, "label": "卖出推送阈值"},
    "notify_buy_threshold": {"low": 40, "high": 70, "default": 68, "label": "买入推送阈值"},
    "stock_qty_strong_pct": {"low": 0.25, "high": 0.60, "default": 0.40, "label": "强信号卖出比例"},
    "stock_qty_base_pct": {"low": 0.15, "high": 0.40, "default": 0.30, "label": "中等信号卖出比例"},
    "stock_rebuild_strong_pct": {"low": 0.50, "high": 1.00, "default": 0.80, "label": "强信号接回比例"},
    "stock_first_add_pct": {"low": 0.10, "high": 0.40, "default": 0.20, "label": "首次加仓比例"},
}

TRAIN_DATES = ("2025-06-01", "2026-03-31")
TEST_DATES = ("2026-04-01", "2026-07-20")
TUSHARE_TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"

CSV_FIELDS = [
    "trial_no", "vwap_buy_deviation", "take_profit_pct", "buy_confirm_min_score",
    "notify_sell_threshold", "notify_buy_threshold",
    "stock_qty_strong_pct", "stock_qty_base_pct",
    "stock_rebuild_strong_pct", "stock_first_add_pct",
    "train_win_rate", "train_total_pnl", "train_n_trades",
    "train_max_drawdown", "train_annualized_return", "train_composite_score",
    "test_win_rate", "test_total_pnl", "test_n_trades",
    "test_max_drawdown", "test_annualized_return", "test_composite_score",
    "elapsed_sec", "status",
]

_results_csv = OUT_DIR / "optimizer_results.csv"
_best_json = OUT_DIR / "best_params.json"

def _set_per_stock_paths(code: str):
    """每个股票独立的输出文件，避免并行训练时互相覆盖。"""
    global _results_csv, _best_json
    _results_csv = OUT_DIR / f"optimizer_results_{code}.csv"
    _best_json = OUT_DIR / f"best_params_{code}.json"


# ── 数据加载 ────────────────────────────────────────────────
def _download_and_cache(code: str, date: str) -> pd.DataFrame:
    """下载单日分钟数据并缓存到 CSV，返回带 vwap 等基础指标的 DataFrame。"""
    cache_path = CACHE_DIR / code / f"{date}.csv"
    if cache_path.exists():
        df = pd.read_csv(cache_path)
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)
        if not df.empty and "vwap" in df.columns:
            return df
    # 下载
    from backtest_v127_000988 import _tsmin, _addi, _dm
    end_dt = datetime.strptime(date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=3)
    mdf = _tsmin(code, start_dt.strftime("%Y-%m-%d"), date, "")
    sm = _dm(mdf, date)
    if sm.empty or len(sm) < 25:
        return pd.DataFrame()
    sm = _addi(sm)
    # 缓存到 CSV（保留 vwap 等指标）
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    sm.to_csv(cache_path, index=False, encoding="utf-8-sig")
    return sm


def _load_cached(code: str, date: str) -> pd.DataFrame:
    """从缓存加载含指标的分钟数据，缺失时下载。"""
    cache_path = CACHE_DIR / code / f"{date}.csv"
    if cache_path.exists():
        df = pd.read_csv(cache_path)
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)
        if not df.empty and "vwap" in df.columns and len(df) >= 25:
            return df
    return _download_and_cache(code, date)


def _ensure_trading_dates(code: str, start: str, end: str) -> List[str]:
    """获取交易日列表并预热缓存。"""
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]
    print(f"  [数据] {code} {start}~{end}: {len(dates)} 个交易日，检查缓存...")
    cached = 0
    for d in dates:
        p = CACHE_DIR / code / f"{d}.csv"
        if p.exists():
            df = pd.read_csv(p)
            if not df.empty and "vwap" in df.columns:
                cached += 1
    print(f"  [数据] 已缓存 {cached}/{len(dates)} 天，缺失将在首次 trial 下载")
    return dates


# ── 日线上下文（无未来函数版） ──────────────────────────────
# 预加载所有可用日期的收盘价，避免逐个读文件的性能问题
_PREV_CLOSE_INDEX: Dict[str, float] = {}

def _build_prev_close_index(code: str) -> None:
    """扫描缓存目录，建立 {date: close} 索引。"""
    if _PREV_CLOSE_INDEX:
        return
    cache_dir = CACHE_DIR / code
    if not cache_dir.exists():
        return
    for f in sorted(cache_dir.glob("*.csv")):
        date = f.stem
        try:
            df = pd.read_csv(f)
            if not df.empty and "close" in df.columns:
                _PREV_CLOSE_INDEX[date] = float(df.iloc[-1]["close"])
        except Exception:
            continue
    print(f"  [数据] 预处理 {len(_PREV_CLOSE_INDEX)} 天收盘价索引")


def _load_prev_day_close(date_str: str) -> Optional[float]:
    """取前一交易日收盘价（从内存索引查找，不读文件）。"""
    all_dates = sorted(d for d in _PREV_CLOSE_INDEX.keys() if d < date_str)
    if not all_dates:
        return None
    # 取最近的前一日（最后一个小于 date_str 的日期）
    return _PREV_CLOSE_INDEX[all_dates[-1]]


def _build_daily_context(date_str: str) -> Dict[str, Any]:
    """用前一交易日收盘价构建日线上下文。

    绝不使用当日任何分钟数据，杜绝未来函数泄露。
    若无可用的前一日数据，返回宽松默认值。
    """
    prev_close = _load_prev_day_close(date_str)
    return {
        "daily_status": "ok", "daily_gate": "normal",
        "daily_buy_t_ok": True, "daily_trend_bg": "bull",
        "daily_ma5_state": "above_ma5_trend", "daily_above_ma5": True,
        "daily_ma5": prev_close or 0,
        "daily_ma10": prev_close or 0,
        "daily_ma20": prev_close or 0,
        "daily_breakdown_risk": False, "daily_overheated": False,
        "daily_pullback_support": False,
        "index_regime": "range", "index_regime_status": "normal",
        "index_circuit_state": "normal", "index_gate_advice": "normal_t",
        "index_temp_bucket": "neutral",
        "intraday_alerts": [], "benchmark_gate": "neutral",
    }


# ── 简版回测 ────────────────────────────────────────────────
def _run_single_backtest(code: str, params: Dict[str, Any], start: str, end: str, step: int = 3) -> Dict[str, Any]:
    """用自定义参数跑一次回测，返回指标字典。

    1. 按交易日遍历
    2. 每日加载含指标的分钟数据
    3. 逐分钟调用 SignalEngine.evaluate()
    4. 记录 T0 闭环盈亏
    5. 计算净值曲线
    """
    # 加载 signal_engine 并注入参数 + STOCK_PARAMS
    import signal_engine as _se
    from config import STOCK_PARAMS
    _se.PARAMS.update(STOCK_PARAMS.get(code, {}))
    _se.PARAMS.update(params)
    _se.MINUTE_FETCH_STATUS[code] = "ok"

    # ===== 修复：清空全局状态，杜绝跨 Trial 污染 =====
    _se.VIRTUAL_TRADES.clear()
    _se.HOLDINGS.clear()

    engine = _se.SignalEngine()

    # V1.29: 集成 PositionSizer 动态仓位计算
    from position_sizer import PositionSizer
    sizer = PositionSizer(params=params, virtual_trades=_se.VIRTUAL_TRADES)

    trading_dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]
    all_trades: List[Dict] = []
    nav_records: List[Dict] = []

    cash = INITIAL_CAPITAL
    base_holdings = 1000
    intraday_buy_qty = 0

    # 逐日回放
    for ds in trading_dates:
        minute_df = _load_cached(code, ds)
        if minute_df.empty or len(minute_df) < 30:
            continue

        # 每日重置 engine 状态
        engine.state_reset_date = ds
        engine.buy_count_per_stock[code] = 0
        engine.sell_count_per_stock[code] = 0
        engine.post_sell_block_until[code] = None

        day_buys: List[tuple] = []  # (price, qty)
        day_sells: List[tuple] = []  # (price, qty)

        # ===== 修复：日线上下文基于前一日数据，杜绝未来函数 =====
        daily_ctx = _build_daily_context(ds)

        pre_close = float(minute_df.iloc[0].get("prev_close", minute_df.iloc[0]["close"]))
        hold_qty = base_holdings + intraday_buy_qty  # 实际可卖数量
        holding = {
            "name": "华工科技", "code": code,
            "cost": float(minute_df.iloc[0]["close"]),
            "qty": hold_qty, "t_qty": hold_qty,
            "type": "stock", "pre_close": pre_close,
        }

        bc = 0  # 当日买入计数
        for i in range(25, len(minute_df), step):
            sub_df = minute_df.iloc[:i + 1].copy()
            try:
                buy_score, sell_score, sig = engine.evaluate(code, "华工科技", sub_df, holding, daily_ctx=daily_ctx)
            except Exception:
                continue
            if sig is None:
                continue

            # ===== 推送阈值过滤（模拟 main.py 实盘逻辑） =====
            _t_dt = pd.Timestamp(minute_df.iloc[i]["time"])
            t_val = _t_dt.hour * 100 + _t_dt.minute
            if sig.action in ("BUY_LOW", "ADD_POS"):
                _nth = params.get("notify_buy_threshold", 68)
            elif t_val >= 1000:
                _nth = params.get("notify_sell_threshold", 65)
            else:
                _nth = params.get("notify_sell_early_threshold", 75)
            if sig.score < _nth:
                continue  # 低于推送阈值，模拟实盘中静默处理

            cp = float(minute_df.iloc[i]["close"])

            # ===== V1.29: PositionSizer 动态计算交易股数 =====
            # V1.29: 动态仓位 = max(base_sizer_qty, FIXED_QTY)
            actual_qty = base_holdings + intraday_buy_qty
            _h = holding.copy()
            _h["t_qty"] = actual_qty
            _h["qty"] = actual_qty
            if sig.action in ("BUY_LOW", "ADD_POS") and bc < MAX_BUYS:
                buy_qty = sizer.calc_buy_qty(code, _h, None, sig.score, 42.0)
                if buy_qty <= 0:
                    buy_qty = 200  # fallback
                buy_qty = max(100, (buy_qty // 100) * 100)
                buy_px = cp + SLIPPAGE
                cost = buy_px * buy_qty * (1 + COMMISSION)
                if cash >= cost:
                    cash -= cost
                    day_buys.append((buy_px, buy_qty))
                    intraday_buy_qty += buy_qty
                    bc += 1
                    engine.record_signal(code, sig.action, cp, sig.score)
                    engine.record_trade_action(code, "BUY_LOW", buy_qty)
            elif sig.action == "SELL_HIGH":
                sellable = base_holdings + intraday_buy_qty
                sell_qty = sizer.calc_sell_qty(code, _h, None, sig.score, 42.0, bc)
                if sell_qty <= 0:
                    sell_qty = min(200, sellable)  # fallback: max 200
                sell_qty = max(100, (sell_qty // 100) * 100)
                sell_qty = min(sell_qty, sellable)
                if sell_qty >= 100:
                    sell_px = cp - SLIPPAGE
                    proceeds = sell_px * sell_qty * (1 - COMMISSION - STAMP_TAX)
                    cash += proceeds
                    day_sells.append((sell_px, sell_qty))
                    if intraday_buy_qty >= sell_qty:
                        intraday_buy_qty -= sell_qty
                    else:
                        remaining = sell_qty - intraday_buy_qty
                        intraday_buy_qty = 0
                        base_holdings -= remaining
                    engine.record_signal(code, sig.action, cp, sig.score)
                    engine.record_trade_action(code, "SELL_HIGH", sell_qty)

        # T0 闭环配对：当日买入与卖出配对（使用实际交易量）
        n_cycles = min(len(day_buys), len(day_sells))
        for j in range(n_cycles):
            bp, bq = day_buys[j]
            sp, sq = day_sells[j]
            match_qty = min(bq, sq)
            net = (sp - bp) * match_qty - bp * match_qty * COMMISSION - sp * match_qty * (COMMISSION + STAMP_TAX)
            all_trades.append({
                "date": ds,
                "buy_price": round(bp, 2),
                "sell_price": round(sp, 2),
                "net_pnl": round(net, 2),
                "qty": match_qty,
            })
            # 配对后当日T0买入对应的持仓清空，base_holdings不变
            # intraday_buy_qty 已经在卖出时扣减过了

        # ===== 修复：日末不再强复位底仓，让真实持仓带入次日 =====
        close_px = float(minute_df.iloc[-1]["close"])
        total_holdings = base_holdings + intraday_buy_qty
        nav = cash + total_holdings * close_px
        nav_records.append({"date": ds, "nav": round(nav, 2)})

    if not all_trades:
        return {"win_rate": 0, "total_pnl": 0, "n_trades": 0,
                "annualized_return": 0, "max_drawdown": 0, "composite_score": -9999.0}

    # 计算指标
    n_trades = len(all_trades)
    winning = [t for t in all_trades if t["net_pnl"] > 0]
    win_rate = len(winning) / n_trades

    if len(nav_records) >= 3:
        df = pd.DataFrame(nav_records)
        first_nav = float(df.iloc[0]["nav"])
        last_nav = float(df.iloc[-1]["nav"])
        total_return = (last_nav / first_nav - 1) if first_nav > 0 else 0
        n_days = max(len(nav_records) - 1, 1)
        annualized_ret = ((last_nav / first_nav) ** (252.0 / n_days) - 1) if first_nav > 0 else 0

        peak = df["nav"].cummax()
        dd_series = df["nav"] / peak - 1.0
        max_dd = float(dd_series.min())
    else:
        total_return = 0
        annualized_ret = 0
        max_dd = 0

    mdd_abs = abs(max_dd) if max_dd < 0 else 0.01

    # ===== 修复：对数交易频次惩罚（期望年化 ≥30 笔） =====
    import math
    n_days_actual = max(len(nav_records), 1)
    annualized_trades = n_trades * (252.0 / n_days_actual)
    # 软惩罚：低于30笔/年时压降得分；高于30笔时趋于1
    trade_penalty = math.log(min(annualized_trades, 60) + 1) / math.log(31)
    composite = (annualized_ret / mdd_abs) * (win_rate * 100) * max(trade_penalty, 0.05)

    return {
        "win_rate": round(win_rate, 4),
        "total_pnl": round(sum(t["net_pnl"] for t in all_trades), 2),
        "n_trades": n_trades,
        "annualized_return": round(annualized_ret, 4),
        "max_drawdown": round(max_dd, 4),
        "composite_score": round(composite, 4),
    }


# ── 单次 Trial ──────────────────────────────────────────────
def _run_trial(code: str, trial_params: Dict[str, Any], start: str, end: str,
               trial_no: int = 0, step: int = 3) -> Tuple[Dict[str, Any], float]:
    """运行一次 trial，返回 (metrics, composite_score)。"""
    # 备份原始 PARAMS
    import signal_engine as _se
    saved = {k: _se.PARAMS.get(k) for k in trial_params}

    try:
        metrics = _run_single_backtest(code, trial_params, start, end, step=step)
        composite = metrics["composite_score"]
    except Exception as e:
        print(f"  [trial {trial_no}] FAILED: {type(e).__name__}: {e}")
        metrics = {"win_rate": 0, "total_pnl": 0, "n_trades": 0,
                    "max_drawdown": 0, "annualized_return": 0, "composite_score": -9999.0}
        composite = -9999.0
    finally:
        for k, v in saved.items():
            if v is None:
                _se.PARAMS.pop(k, None)
            else:
                _se.PARAMS[k] = v

    return metrics, composite


# ── Optuna ──────────────────────────────────────────────────
def run_optuna(code: str, n_trials: int, start: str, end: str, step: int = 3) -> Tuple[Dict[str, Any], List[Dict]]:
    import optuna
    results: List[Dict] = []

    def objective(trial):
        params = {
            "vwap_buy_deviation": trial.suggest_float("vwap_buy_deviation", -0.035, -0.015),
            "take_profit_pct": trial.suggest_float("take_profit_pct", 0.005, 0.025),
            "buy_confirm_min_score": trial.suggest_int("buy_confirm_min_score", 15, 30),
            "notify_sell_threshold": trial.suggest_int("notify_sell_threshold", 40, 70),
            "notify_buy_threshold": trial.suggest_int("notify_buy_threshold", 40, 70),
            "stock_qty_strong_pct": trial.suggest_float("stock_qty_strong_pct", 0.25, 0.60),
            "stock_qty_base_pct": trial.suggest_float("stock_qty_base_pct", 0.15, 0.40),
            "stock_rebuild_strong_pct": trial.suggest_float("stock_rebuild_strong_pct", 0.50, 1.00),
            "stock_first_add_pct": trial.suggest_float("stock_first_add_pct", 0.10, 0.40),
        }
        t0 = time.time()
        metrics, composite = _run_trial(code, params, start, end, trial.number, step=step)
        elapsed = round(time.time() - t0, 1)

        row = {"trial_no": trial.number, **params,
               "train_win_rate": metrics["win_rate"],
               "train_total_pnl": metrics["total_pnl"],
               "train_n_trades": metrics["n_trades"],
               "train_max_drawdown": metrics["max_drawdown"],
               "train_annualized_return": metrics["annualized_return"],
               "train_composite_score": composite,
               "test_win_rate": 0, "test_total_pnl": 0, "test_n_trades": 0,
               "test_max_drawdown": 0, "test_annualized_return": 0, "test_composite_score": 0,
               "elapsed_sec": elapsed, "status": "ok"}
        results.append(row)
        _append_csv_row(row)
        return composite

    study = optuna.create_study(direction="maximize",
                                sampler=optuna.samplers.TPESampler(seed=42, n_startup_trials=10))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=True)
    bp = study.best_params if study.best_params else PARAM_DEFAULTS.copy()
    print(f"\n[Optuna] 最佳 composite_score = {study.best_value:.4f}, params = {bp}")
    return bp, results


# ── 快速随机搜索 ────────────────────────────────────────────
def run_quick(code: str, n_trials: int, start: str, end: str, step: int = 3) -> Tuple[Dict, List]:
    import random
    results = []
    best_params = {}
    best_score = -9999.0
    print(f"\n[快速搜索] {code} {n_trials} 次随机采样...")
    for i in range(n_trials):
        params = {"vwap_buy_deviation": round(random.uniform(-0.035, -0.015), 3),
                   "take_profit_pct": round(random.uniform(0.005, 0.025), 3),
                   "buy_confirm_min_score": random.randint(15, 30),
                   "notify_sell_threshold": random.randint(40, 70),
                   "notify_buy_threshold": random.randint(40, 70),
                   "stock_qty_strong_pct": round(random.uniform(0.25, 0.60), 2),
                   "stock_qty_base_pct": round(random.uniform(0.15, 0.40), 2),
                   "stock_rebuild_strong_pct": round(random.uniform(0.50, 1.00), 2),
                   "stock_first_add_pct": round(random.uniform(0.10, 0.40), 2)}
        t0 = time.time()
        metrics, composite = _run_trial(code, params, start, end, i, step=step)
        elapsed = round(time.time() - t0, 1)
        print(f"  [{i}] vwap={params['vwap_buy_deviation']:.3f} tp={params['take_profit_pct']:.3f} "
              f"score={params['buy_confirm_min_score']} → composite={composite:.2f} "
              f"wr={metrics['win_rate']:.2%} trades={metrics['n_trades']} dd={metrics['max_drawdown']:.2%}")
        row = {"trial_no": i, **params,
               "train_win_rate": metrics["win_rate"],
               "train_total_pnl": metrics["total_pnl"],
               "train_n_trades": metrics["n_trades"],
               "train_max_drawdown": metrics["max_drawdown"],
               "train_annualized_return": metrics["annualized_return"],
               "train_composite_score": composite,
               "test_win_rate": 0, "test_total_pnl": 0, "test_n_trades": 0,
               "test_max_drawdown": 0, "test_annualized_return": 0, "test_composite_score": 0,
               "elapsed_sec": elapsed, "status": "ok"}
        results.append(row)
        _append_csv_row(row)
        if composite > best_score:
            best_score, best_params = composite, params.copy()
    if not best_params:
        best_params = PARAM_DEFAULTS.copy()
    print(f"\n[快速搜索] 完成. best={best_score:.4f}, params={best_params}")
    return best_params, results


# ── 网格搜索 ────────────────────────────────────────────────
def run_grid(code: str, start: str, end: str, step: int = 3) -> Tuple[Dict, List]:
    vwap_vals = [round(x, 3) for x in np.arange(-0.035, -0.010, 0.005)]
    tp_vals = [round(x, 3) for x in np.arange(0.005, 0.030, 0.005)]
    score_vals = list(range(15, 33, 3))
    total = len(vwap_vals) * len(tp_vals) * len(score_vals)
    print(f"\n[网格搜索] {code} {total} 种组合 ({len(vwap_vals)}×{len(tp_vals)}×{len(score_vals)})")

    results = []
    best_params, best_score = {}, -9999.0
    idx = 0
    for v in vwap_vals:
        for tp in tp_vals:
            for sc in score_vals:
                params = {"vwap_buy_deviation": v, "take_profit_pct": tp, "buy_confirm_min_score": sc,
                           "notify_sell_threshold": 65, "notify_buy_threshold": 68,
                           "stock_qty_strong_pct": 0.40, "stock_qty_base_pct": 0.30,
                           "stock_rebuild_strong_pct": 0.80, "stock_first_add_pct": 0.20}
                t0 = time.time()
                metrics, composite = _run_trial(code, params, start, end, idx, step=step)
                elapsed = round(time.time() - t0, 1)
                if idx % 15 == 0:
                    print(f"  [{idx}/{total}] vwap={v:.3f} tp={tp:.3f} sc={sc} → composite={composite:.2f} "
                          f"wr={metrics['win_rate']:.2%} trades={metrics['n_trades']} dd={metrics['max_drawdown']:.2%}")
                row = {"trial_no": idx, **params,
                       "train_win_rate": metrics["win_rate"],
                       "train_total_pnl": metrics["total_pnl"],
                       "train_n_trades": metrics["n_trades"],
                       "train_max_drawdown": metrics["max_drawdown"],
                       "train_annualized_return": metrics["annualized_return"],
                       "train_composite_score": composite,
                       "test_win_rate": 0, "test_total_pnl": 0, "test_n_trades": 0,
                       "test_max_drawdown": 0, "test_annualized_return": 0, "test_composite_score": 0,
                       "elapsed_sec": elapsed, "status": "ok"}
                results.append(row)
                _append_csv_row(row)
                if composite > best_score:
                    best_score, best_params = composite, params.copy()
                idx += 1
    if not best_params:
        best_params = PARAM_DEFAULTS.copy()
    print(f"\n[网格搜索] 完成. best={best_score:.4f}, params={best_params}")
    return best_params, results


# ── 验证报告 ────────────────────────────────────────────────
def run_neighborhood_test(code: str, best_params: Dict[str, Any],
                          test_start: str, test_end: str, step: int = 3) -> Dict[str, Any]:
    """邻域稳定性测试：对最优参数 ±5% 浮动，在测试集上验证。

    返回：
      - "stable": True/False — 邻域平均得分不低于最优得分的 70%
      - "best_composite": 最优参数得分
      - "neighbors": 各邻居的得分列表
      - "avg_neighbor": 邻域平均得分
    """
    import copy
    neighbor_scores = []
    neighbor_details = []

    for key, delta in [("vwap_buy_deviation", 0.05), ("take_profit_pct", 0.05), ("buy_confirm_min_score", 1)]:
        for direction in [-1, 1]:
            neighbor = copy.deepcopy(best_params)
            if isinstance(best_params.get(key), int):
                val = best_params[key] + direction * delta
                neighbor[key] = int(round(val))
            else:
                val = best_params[key] * (1 + direction * delta)
                neighbor[key] = round(val, 4)

            # 边界约束
            space = PARAM_SPACE.get(key)
            if space:
                neighbor[key] = max(space["low"], min(space["high"], neighbor[key]))

            if neighbor[key] == best_params[key]:
                continue

            metrics, _ = _run_trial(code, neighbor, test_start, test_end, -10, step=step)
            cs = metrics.get("composite_score", -9999)
            neighbor_scores.append(cs)
            neighbor_details.append({"param": key, "value": neighbor[key], "composite": cs})

    best_cs = 0
    # 重算最优参数在测试集的得分
    best_metrics, _ = _run_trial(code, best_params, test_start, test_end, -11, step=step)
    best_cs = best_metrics.get("composite_score", 0)

    avg_neighbor = np.mean(neighbor_scores) if neighbor_scores else 0
    stable = best_cs > 0 and avg_neighbor > best_cs * 0.7

    return {
        "stable": stable,
        "best_composite": best_cs,
        "neighbors": neighbor_details,
        "avg_neighbor": round(float(avg_neighbor), 4),
    }

def _init_csv():
    if not _results_csv.exists():
        with open(_results_csv, "w", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writeheader()


def _append_csv_row(row):
    _init_csv()
    try:
        with open(_results_csv, "a", newline="", encoding="utf-8-sig") as f:
            csv.DictWriter(f, fieldnames=CSV_FIELDS).writerow({k: row.get(k, "") for k in CSV_FIELDS})
    except Exception as e:
        print(f"[警告] CSV 写入失败: {e}")


def _save_best(train_params, train_metrics, test_metrics):
    _best_json.write_text(json.dumps({
        "best_params": train_params,
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[结果] -> {_best_json}")


def generate_report(code, best_params, train_metrics, test_metrics, method, neighborhood=None):
    report_path = OUT_DIR / "optimizer_report.md"
    lines = [
        "# 参数寻优报告", "",
        f"- 标的: {code}",
        f"- 方法: {method}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "",
        "## 1. 参数搜索空间", "",
        "| 参数 | 默认值 | 搜索范围 |",
        "|------|--------|----------|",
    ]
    for k, v in PARAM_SPACE.items():
        lines.append(f"| {v['label']} (`{k}`) | {v['default']} | [{v['low']}, {v['high']}] |")
    lines += ["", "## 2. 最佳参数", "", "| 参数 | 值 |", "|------|-----|"]
    for k, v in best_params.items():
        label = PARAM_SPACE.get(k, {}).get("label", k)
        lines.append(f"| {label} (`{k}`) | {v} |")
    lines += ["", "## 3. 训练集 vs 测试集", "",
               f"- 训练集: {TRAIN_DATES[0]} ~ {TRAIN_DATES[1]}",
               f"- 测试集: {TEST_DATES[0]} ~ {TEST_DATES[1]}", "",
               "| 指标 | 训练集 | 测试集 |", "|------|--------|--------|"]
    for key, label, fmt in [("win_rate", "胜率", "{:.2%}"), ("total_pnl", "总盈亏", "{:+.0f}元"),
                             ("n_trades", "成交笔数", "{:.0f}"),
                             ("annualized_return", "年化收益率", "{:.2%}"),
                             ("max_drawdown", "最大回撤", "{:.2%}"),
                             ("composite_score", "综合得分", "{:.2f}")]:
        lines.append(f"| {label} | {fmt.format(train_metrics.get(key, 0))} | {fmt.format(test_metrics.get(key, 0))} |")

    train_cs, test_cs = train_metrics.get("composite_score", 0), test_metrics.get("composite_score", 0)
    if train_cs > 0 and test_cs > 0:
        ratio = test_cs / train_cs
        lines += ["", "### 过拟合评估", "",
                   f"{'✅ 通过' if ratio >= 0.6 else '⚠️ 警告'}: "
                   f"测试/训练得分比 {ratio:.2%} ({'≥60% ✅' if ratio >= 0.6 else '<60% ❌'})"]

    # 邻域稳定性测试
    nh = neighborhood
    if nh and nh.get("neighbors"):
        lines += ["", "## 4. 邻域稳定性测试", "",
                   "| 参数 | 方向 | 值 | 综合得分 |",
                   "|------|------|-----|----------|"]
        for n in nh["neighbors"]:
            direction_label = "+" if n["value"] > best_params.get(n["param"], 0) else "-"
            lines.append(f"| {n['param']} | {direction_label}5% | {n['value']} | {n['composite']:.2f} |")
        lines += ["", f"**最优参数测试集得分**: {nh['best_composite']:.2f}",
                   f"**邻域平均得分**: {nh['avg_neighbor']:.2f}",
                   f"**判定**: {'✅ 稳定 — 邻域平均≥最优的70%' if nh.get('stable') else '❌ 不稳定 — 参数孤岛风险，实盘需谨慎'}",
                   ""]

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[报告] {report_path}")


def main():
    ap = argparse.ArgumentParser(description="华工科技 000988 参数寻优系统")
    ap.add_argument("--code", default="000988", help="股票代码，默认 000988")
    ap.add_argument("--method", default="optuna", choices=["optuna", "grid", "quick"])
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--start-train", default=TRAIN_DATES[0])
    ap.add_argument("--end-train", default=TRAIN_DATES[1])
    ap.add_argument("--start-test", default=TEST_DATES[0])
    ap.add_argument("--end-test", default=TEST_DATES[1])
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--step", type=int, default=3, help="分钟评估步长（默认3，每3分钟评估一次）")
    args = ap.parse_args()

    print("=" * 55, "\n  参数寻优系统", "=" * 55, sep="\n")
    print(f"  标的: {args.code}  方法: {args.method}  训练: {args.start_train}~{args.end_train}")

    _set_per_stock_paths(args.code)
    _build_prev_close_index(args.code)
    _ensure_trading_dates(args.code, args.start_train, args.end_train)
    if not args.no_validate:
        _ensure_trading_dates(args.code, args.start_test, args.end_test)

    print("\n[基线] 默认参数...")
    base_metrics, _ = _run_trial(args.code, PARAM_DEFAULTS, args.start_train, args.end_train, -2, step=args.step)
    print(f"  wr={base_metrics['win_rate']:.2%} trades={base_metrics['n_trades']} "
          f"dd={base_metrics['max_drawdown']:.2%} composite={base_metrics['composite_score']:.2f}")

    # 寻优
    if args.method == "optuna":
        best_params, _ = run_optuna(args.code, args.trials, args.start_train, args.end_train, step=args.step)
    elif args.method == "grid":
        best_params, _ = run_grid(args.code, args.start_train, args.end_train, step=args.step)
    else:
        best_params, _ = run_quick(args.code, args.trials, args.start_train, args.end_train, step=args.step)

    # 最佳参数训练集重算
    print("\n[最优参数] 训练集...")
    train_metrics, _ = _run_trial(args.code, best_params, args.start_train, args.end_train, -3, step=args.step)

    # 样本外验证
    test_metrics = {}
    neighborhood_result = None
    if not args.no_validate:
        test_metrics, _ = _run_trial(args.code, best_params, args.start_test, args.end_test, -5, step=args.step)
        print("\n[基准对比] 默认参数在测试集...")
        def_test, _ = _run_trial(args.code, PARAM_DEFAULTS, args.start_test, args.end_test, -4, step=args.step)
        print(f"  默认: wr={def_test['win_rate']:.2%} pnl={def_test['total_pnl']:+.0f} "
              f"composite={def_test['composite_score']:.2f}")

        tc, tsc = train_metrics.get("composite_score", 0), test_metrics.get("composite_score", 0)
        if tc > 0 and tsc > 0:
            print(f"\n[过拟合] 训练={tc:.2f} 测试={tsc:.2f} 比={tsc/tc:.2%} "
                  f"{'[通过]' if tsc/tc>=0.6 else '[有风险]'}")

        # 邻域稳定性测试
        print("\n[邻域稳定性测试] 最优参数 ±5% 摇动...")
        neighborhood_result = run_neighborhood_test(args.code, best_params, args.start_test, args.end_test, step=args.step)
        if neighborhood_result["neighbors"]:
            print(f"  最优: {neighborhood_result['best_composite']:.2f}")
            print(f"  邻域平均: {neighborhood_result['avg_neighbor']:.2f}")
            for n in neighborhood_result["neighbors"]:
                print(f"    {n['param']}={n['value']} → composite={n['composite']:.2f}")
            print(f"  判定: {'[稳定]' if neighborhood_result['stable'] else '[不稳定 孤岛参数风险]'}")

    _save_best(best_params, train_metrics, test_metrics)
    generate_report(args.code, best_params, train_metrics, test_metrics, args.method, neighborhood=neighborhood_result)

    print("\n" + "=" * 55, "  寻优完成", "=" * 55, sep="\n")
    for k, v in best_params.items():
        lbl = PARAM_SPACE.get(k, {}).get("label", k)
        print(f"  {lbl}: {v}")
    print(f"  训练集综合得分: {train_metrics.get('composite_score', 0):.2f}")
    print(f"  输出: {OUT_DIR}/")


if __name__ == "__main__":
    main()
