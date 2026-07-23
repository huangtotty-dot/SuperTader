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
DEFAULT_CODE = "000988"
INITIAL_CAPITAL = 200000.0
FIXED_QTY = 200         # 每笔固定股数
MAX_BUYS = 3
MAX_SELLS = 3
COMMISSION = 0.00015
STAMP_TAX = 0.0005
SLIPPAGE = 0.01

PARAM_DEFAULTS = {
    "vwap_buy_deviation": -0.020,
    "take_profit_pct": 0.010,
    "buy_confirm_min_score": 18,
}

PARAM_SPACE = {
    "vwap_buy_deviation": {"low": -0.035, "high": -0.015, "default": -0.020, "label": "VWAP偏离买入阈值"},
    "take_profit_pct": {"low": 0.005, "high": 0.025, "default": 0.010, "label": "止盈比例"},
    "buy_confirm_min_score": {"low": 15, "high": 30, "default": 18, "label": "买入确认最低分"},
}

TRAIN_DATES = ("2025-06-01", "2026-03-31")
TEST_DATES = ("2026-04-01", "2026-07-20")
TUSHARE_TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"

CSV_FIELDS = [
    "trial_no", "vwap_buy_deviation", "take_profit_pct", "buy_confirm_min_score",
    "train_win_rate", "train_total_pnl", "train_n_trades",
    "train_max_drawdown", "train_annualized_return", "train_composite_score",
    "test_win_rate", "test_total_pnl", "test_n_trades",
    "test_max_drawdown", "test_annualized_return", "test_composite_score",
    "elapsed_sec", "status",
]

_results_csv = OUT_DIR / "optimizer_results.csv"
_best_json = OUT_DIR / "best_params.json"


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


def _ensure_trading_dates(start: str, end: str) -> List[str]:
    """获取交易日列表并预热缓存。"""
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]
    print(f"  [数据] {start}~{end}: {len(dates)} 个交易日，检查缓存...")
    cached = 0
    for d in dates:
        p = CACHE_DIR / DEFAULT_CODE / f"{d}.csv"
        if p.exists():
            df = pd.read_csv(p)
            if not df.empty and "vwap" in df.columns:
                cached += 1
    print(f"  [数据] 已缓存 {cached}/{len(dates)} 天，缺失将在首次 trial 下载")
    return dates


def _build_daily_context_from_minute(minute_df: pd.DataFrame) -> Dict[str, Any]:
    """从分钟数据构建简化日线上下文（不含日线MA，但让 daily_buy_t_ok=True）。"""
    if minute_df.empty:
        return {}
    last = minute_df.iloc[-1]
    return {
        "daily_status": "ok",
        "daily_gate": "normal",
        "daily_buy_t_ok": True,
        "daily_trend_bg": "bull",
        "daily_ma5_state": "above_ma5_trend",
        "daily_above_ma5": True,
        "daily_ma5": float(last.get("close", 0)),
        "daily_ma10": float(last.get("close", 0)),
        "daily_ma20": float(last.get("close", 0)),
        "daily_breakdown_risk": False,
        "daily_overheated": False,
        "daily_pullback_support": False,
        "index_regime": "range",
        "index_regime_status": "normal",
        "index_circuit_state": "normal",
        "index_gate_advice": "normal_t",
        "index_temp_bucket": "neutral",
        "intraday_alerts": [],
        "benchmark_gate": "neutral",
    }


# ── 简版回测 ────────────────────────────────────────────────
def _run_single_backtest(params: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
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
    code = DEFAULT_CODE
    _se.PARAMS.update(STOCK_PARAMS.get(code, {}))
    _se.PARAMS.update(params)
    _se.MINUTE_FETCH_STATUS[code] = "ok"

    # OPTIMIZE: SignalEngine 支持自定义 factor_weights 参数
    engine = _se.SignalEngine()

    trading_dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, end)]
    all_trades: List[Dict] = []  # 完成的T0闭环
    nav_records: List[Dict] = []

    cash = INITIAL_CAPITAL
    holdings = 1000  # 固定底仓
    base_holdings = 1000

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

        day_buys: List[float] = []  # 当日买入价格
        day_sells: List[float] = []  # 当日卖出价格

        daily_ctx = _build_daily_context_from_minute(minute_df)
        pre_close = float(minute_df.iloc[0].get("prev_close", minute_df.iloc[0]["close"]))
        holding = {
            "name": "华工科技", "code": code,
            "cost": float(minute_df.iloc[0]["close"]),
            "qty": holdings, "t_qty": holdings,
            "type": "stock", "pre_close": pre_close,
        }

        bc, sc = 0, 0  # 当日买/卖计数
        for i in range(25, len(minute_df)):
            sub_df = minute_df.iloc[:i + 1].copy()
            try:
                buy_score, sell_score, sig = engine.evaluate(code, "华工科技", sub_df, holding, daily_ctx=daily_ctx)
            except Exception:
                continue
            if sig is None:
                continue

            cp = float(minute_df.iloc[i]["close"])
            ct = str(minute_df.iloc[i]["time"])

            if sig.action in ("BUY_LOW", "ADD_POS") and bc < MAX_BUYS:
                buy_px = cp + SLIPPAGE
                cost = buy_px * FIXED_QTY * (1 + COMMISSION)
                if cash >= cost:
                    cash -= cost
                    day_buys.append(buy_px)
                    bc += 1
                    engine.record_trade_action(code, "BUY_LOW", 0)
            elif sig.action == "SELL_HIGH" and sc < MAX_SELLS and holdings >= FIXED_QTY:
                sell_px = cp - SLIPPAGE
                proceeds = sell_px * FIXED_QTY * (1 - COMMISSION - STAMP_TAX)
                cash += proceeds
                holdings -= FIXED_QTY
                day_sells.append(sell_px)
                sc += 1
                engine.record_trade_action(code, "SELL_HIGH", 0)

        # T0 闭环配对：依次配对当日买入和卖出的前 min(len(buys), len(sells)) 对
        n_cycles = min(len(day_buys), len(day_sells))
        for j in range(n_cycles):
            bp = day_buys[j]
            sp = day_sells[j]
            net = (sp - bp) * FIXED_QTY - bp * FIXED_QTY * COMMISSION - sp * FIXED_QTY * (COMMISSION + STAMP_TAX)
            all_trades.append({
                "date": ds,
                "buy_price": round(bp, 2),
                "sell_price": round(sp, 2),
                "net_pnl": round(net, 2),
                "qty": FIXED_QTY,
            })
            # 恢复底仓：T0 卖出后补回底仓
            holdings += FIXED_QTY

        # 日末恢复底仓
        end_holdings = holdings
        end_cash = cash

        close_px = float(minute_df.iloc[-1]["close"])
        nav = end_cash + end_holdings * close_px
        nav_records.append({"date": ds, "nav": round(nav, 2)})

    if not all_trades:
        return {"win_rate": 0, "total_pnl": 0, "n_trades": 0,
                "annualized_return": 0, "max_drawdown": 0, "composite_score": -9999.0}

    # 计算指标
    n_trades = len(all_trades)
    winning = [t for t in all_trades if t["net_pnl"] > 0]
    win_rate = len(winning) / n_trades

    # 从 nav_records 计算
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

    total_pnl = sum(t["net_pnl"] for t in all_trades)
    mdd_abs = abs(max_dd) if max_dd < 0 else 0.01
    composite = (annualized_ret / mdd_abs) * (win_rate * 100)

    if n_trades < 3:
        composite = -1000.0

    return {
        "win_rate": round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "n_trades": n_trades,
        "annualized_return": round(annualized_ret, 4),
        "max_drawdown": round(max_dd, 4),
        "composite_score": round(composite, 4),
    }


# ── 单次 Trial ──────────────────────────────────────────────
def _run_trial(trial_params: Dict[str, Any], start: str, end: str,
               trial_no: int = 0) -> Tuple[Dict[str, Any], float]:
    """运行一次 trial，返回 (metrics, composite_score)。"""
    # 备份原始 PARAMS
    import signal_engine as _se
    saved = {k: _se.PARAMS.get(k) for k in trial_params}

    try:
        metrics = _run_single_backtest(trial_params, start, end)
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
def run_optuna(n_trials: int, start: str, end: str) -> Tuple[Dict[str, Any], List[Dict]]:
    import optuna
    results: List[Dict] = []

    def objective(trial):
        params = {
            "vwap_buy_deviation": trial.suggest_float("vwap_buy_deviation", -0.035, -0.015),
            "take_profit_pct": trial.suggest_float("take_profit_pct", 0.005, 0.025),
            "buy_confirm_min_score": trial.suggest_int("buy_confirm_min_score", 15, 30),
        }
        t0 = time.time()
        metrics, composite = _run_trial(params, start, end, trial.number)
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
    print(f"\n[Optuna] 最佳 composite_score = {study.best_value:.4f}, params = {study.best_params}")
    return study.best_params, results


# ── 快速随机搜索 ────────────────────────────────────────────
def run_quick(n_trials: int, start: str, end: str) -> Tuple[Dict, List]:
    import random
    results = []
    best_params = {}
    best_score = -9999.0
    print(f"\n[快速搜索] {n_trials} 次随机采样...")
    for i in range(n_trials):
        params = {"vwap_buy_deviation": round(random.uniform(-0.035, -0.015), 3),
                   "take_profit_pct": round(random.uniform(0.005, 0.025), 3),
                   "buy_confirm_min_score": random.randint(15, 30)}
        t0 = time.time()
        metrics, composite = _run_trial(params, start, end, i)
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
    print(f"\n[快速搜索] 完成. best={best_score:.4f}, params={best_params}")
    return best_params, results


# ── 网格搜索 ────────────────────────────────────────────────
def run_grid(start: str, end: str) -> Tuple[Dict, List]:
    vwap_vals = [round(x, 3) for x in np.arange(-0.035, -0.010, 0.005)]
    tp_vals = [round(x, 3) for x in np.arange(0.005, 0.030, 0.005)]
    score_vals = list(range(15, 33, 3))
    total = len(vwap_vals) * len(tp_vals) * len(score_vals)
    print(f"\n[网格搜索] {total} 种组合 ({len(vwap_vals)}×{len(tp_vals)}×{len(score_vals)})")

    results = []
    best_params, best_score = {}, -9999.0
    idx = 0
    for v in vwap_vals:
        for tp in tp_vals:
            for sc in score_vals:
                params = {"vwap_buy_deviation": v, "take_profit_pct": tp, "buy_confirm_min_score": sc}
                t0 = time.time()
                metrics, composite = _run_trial(params, start, end, idx)
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
    print(f"\n[网格搜索] 完成. best={best_score:.4f}, params={best_params}")
    return best_params, results


# ── 验证报告 ────────────────────────────────────────────────
def run_validation(params: Dict, start: str, end: str) -> Dict:
    print(f"\n[样本外验证] {start} ~ {end}")
    metrics, _ = _run_trial(params, start, end, -1)
    print(f"  → wr={metrics['win_rate']:.2%} pnl={metrics['total_pnl']:+.0f} "
          f"trades={metrics['n_trades']} dd={metrics['max_drawdown']:.2%} "
          f"ann_ret={metrics['annualized_return']:.2%} composite={metrics['composite_score']:.2f}")
    return metrics


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


def generate_report(best_params, train_metrics, test_metrics, method):
    report_path = OUT_DIR / "optimizer_report.md"
    lines = [
        "# 参数寻优报告", "",
        f"- 标的: {DEFAULT_CODE}",
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

    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[报告] {report_path}")


def main():
    ap = argparse.ArgumentParser(description="华工科技 000988 参数寻优系统")
    ap.add_argument("--code", default=DEFAULT_CODE)
    ap.add_argument("--method", default="optuna", choices=["optuna", "grid", "quick"])
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--start-train", default=TRAIN_DATES[0])
    ap.add_argument("--end-train", default=TRAIN_DATES[1])
    ap.add_argument("--start-test", default=TEST_DATES[0])
    ap.add_argument("--end-test", default=TEST_DATES[1])
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    print("=" * 55, "\n  参数寻优系统", "=" * 55, sep="\n")
    print(f"  标的: {args.code}  方法: {args.method}  训练: {args.start_train}~{args.end_train}")

    if not args.resume and _results_csv.exists():
        _results_csv.unlink()
        print("[初始化] 已重置 CSV")

    # 预热数据
    _ensure_trading_dates(args.start_train, args.end_train)
    if not args.no_validate:
        _ensure_trading_dates(args.start_test, args.end_test)

    # 基线
    print("\n[基线] 默认参数...")
    base_metrics, _ = _run_trial(PARAM_DEFAULTS, args.start_train, args.end_train, -2)
    print(f"  wr={base_metrics['win_rate']:.2%} trades={base_metrics['n_trades']} "
          f"dd={base_metrics['max_drawdown']:.2%} composite={base_metrics['composite_score']:.2f}")

    # 寻优
    if args.method == "optuna":
        best_params, _ = run_optuna(args.trials, args.start_train, args.end_train)
    elif args.method == "grid":
        best_params, _ = run_grid(args.start_train, args.end_train)
    else:
        best_params, _ = run_quick(args.trials, args.start_train, args.end_train)

    # 最佳参数训练集重算
    print("\n[最优参数] 训练集...")
    train_metrics, _ = _run_trial(best_params, args.start_train, args.end_train, -3)

    # 样本外验证
    test_metrics = {}
    if not args.no_validate:
        test_metrics = run_validation(best_params, args.start_test, args.end_test)
        print("\n[基准对比] 默认参数在测试集...")
        def_test, _ = _run_trial(PARAM_DEFAULTS, args.start_test, args.end_test, -4)
        print(f"  默认: wr={def_test['win_rate']:.2%} pnl={def_test['total_pnl']:+.0f} "
              f"composite={def_test['composite_score']:.2f}")

        tc, tsc = train_metrics.get("composite_score", 0), test_metrics.get("composite_score", 0)
        if tc > 0 and tsc > 0:
            print(f"\n[过拟合] 训练={tc:.2f} 测试={tsc:.2f} 比={tsc/tc:.2%} "
                  f"{'✅通过' if tsc/tc>=0.6 else '⚠️有风险'}")

    _save_best(best_params, train_metrics, test_metrics)
    generate_report(best_params, train_metrics, test_metrics, args.method)

    print("\n" + "=" * 55, "  寻优完成", "=" * 55, sep="\n")
    for k, v in best_params.items():
        lbl = PARAM_SPACE.get(k, {}).get("label", k)
        print(f"  {lbl}: {v}")
    print(f"  训练集综合得分: {train_metrics.get('composite_score', 0):.2f}")
    print(f"  输出: {OUT_DIR}/")


if __name__ == "__main__":
    main()
