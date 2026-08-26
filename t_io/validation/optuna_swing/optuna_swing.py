# -*- coding: utf-8 -*-
"""
optuna_swing.py — 用 Optuna 寻优做T(高抛低吸纯两点)参数,最大化做T总期望收益 (2026-08-24 v2)

v2 相对 v1 的三处改进:
1. 窗口 16 日 → 3 年 (2023-08-01 ~ 2026-08-24), 由 backfill_minutes.py 回补历史分钟数据
2. 目标函数: 裸胜率 → 总期望收益 (wins×0.5% − fails×0.4%), 避免"牺牲机会换噪声胜率"
3. walk-forward: 2 年训练段寻优 + 1 年测试段验证, 防止样本内过拟合

回测: 复用 harness_backtest.load_snapshots (逐分钟回放 minute_snapshots 快照)
结算: 信号后 30 根 1min 内, 高抛先触 +0.5% 记 WIN / 先触 -0.4% 记 FAIL (与生产同口径)

用法: python t_io/validation/optuna_swing/optuna_swing.py [--trials 50] [--seed 42]
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import optuna

BASE = Path(__file__).resolve().parent.parent.parent.parent  # e:/superTrader
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from harness_backtest import load_snapshots, load_shared

OUT_DIR = BASE / "t_io" / "validation" / "optuna_swing" / "out"
HOLDINGS_FILE = BASE / "t_io" / "state" / "holdings.json"
CODES = ["588170", "600176", "600481", "000988", "002639"]   # 5 核心持仓
DATE_START = "2023-08-01"     # 3 年窗口（回补数据起点; 002639 亦从该月起有数据）
DATE_END = "2026-08-24"
TRAIN_END = "2025-07-31"      # walk-forward: 训练段终点, 之后为测试段
MIN_SWING_SAMPLES = 200       # 做T信号样本下限(防小样本噪声)
WIN_PCT, FAIL_PCT = 0.005, 0.004   # 与 settle_signal 同口径
DEFAULT_PARAMS = {"swing_sell_rsi": 75, "swing_buy_rsi": 35, "swing_bb_upper": 1.0,
                  "swing_bb_lower": 0.0, "swing_sell_vol_ratio": 1.5,
                  "rsi_period_5m_swing": 6}

# 一次性加载 exec 共享命名空间（含 PARAMS 全局）
_SHARED = load_shared()
_PARAMS = _SHARED["PARAMS"]

_FIVE_MIN_NS = 5 * 60 * 1000 * 1000 * 1000


def _dates() -> list:
    from datetime import datetime, timedelta
    start = datetime.strptime(DATE_START, "%Y-%m-%d")
    end = datetime.strptime(DATE_END, "%Y-%m-%d")
    out = []
    dt = start
    while dt <= end:
        if dt.weekday() < 5:
            out.append(dt.strftime("%Y-%m-%d"))
        dt += timedelta(days=1)
    return out


def _holdings_map():
    hm = {}
    if HOLDINGS_FILE.exists():
        raw = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        for code, h in raw.items():
            clean = code.split("_")[0] if "_" in code else code
            hm[clean] = h
    for c in CODES:
        if c not in hm:
            hm[c] = {"name": c, "cost": 50.0, "qty": 500, "base": 500, "t_qty": 500, "type": "stock"}
    return hm


_HOLDINGS = _holdings_map()
_DATES = _dates()


def _bb_pct(seq: list, cur: float):
    """布林%位置: (cur − dn)/(up − dn), 20 期 ±2σ(样本std)。seq=已收 5分bar收盘, cur=当前 forming bar。"""
    w = seq[-19:] + [cur]          # 最后 20 个收盘(含当前)
    if len(w) < 2:
        return None
    mid = 0.0
    for x in w:
        mid += x
    mid /= len(w)
    sd = 0.0
    for x in w:
        sd += (x - mid) ** 2
    sd = (sd / (len(w) - 1)) ** 0.5
    up = mid + 2.0 * sd
    dn = mid - 2.0 * sd
    if up == dn:
        return None
    return (cur - dn) / (up - dn)


def _rsi(seq: list, cur: float, period: int):
    """RSI(period) 对 (seq+[cur]) 最后 period 根。g/l 同除 period 抵消, 无需除。"""
    closes = seq[-(period + 1):] + [cur]
    if len(closes) < 2:
        return None
    g = l = 0.0
    for a, b in zip(closes, closes[1:]):
        d = b - a
        if d > 0:
            g += d
        elif d < 0:
            l -= d
    if g == 0 and l == 0:
        return 50.0
    if l == 0:
        return None
    return 100 - 100 / (1 + g / l)


def _swing_counts(override: dict, dates: list | None = None) -> tuple:
    """逐tick做T回放（纯两点: forming 5分bar的 bb_pct/rsi6 随tick价变）。
    返回 (wins, fails)。比 v1 快约 5-10×: 去掉 add_indicators, 时间分桶/settle 向量化。"""
    _PARAMS.update(override)
    sell_rsi = float(override.get("swing_sell_rsi", 75))
    buy_rsi = float(override.get("swing_buy_rsi", 35))
    bb_up = float(override.get("swing_bb_upper", 1.0))
    bb_dn = float(override.get("swing_bb_lower", 0.0))
    vol_ratio = float(override.get("swing_sell_vol_ratio", 0) or 0)
    rsi_period = int(override.get("rsi_period_5m_swing", 6))
    min_bars = int(_PARAMS.get("swing_min_5m_bars", 13))
    dates = dates or _DATES
    wins = fails = 0
    last_sig = set()
    for code in CODES:
        for date in dates:
            df = load_snapshots(code, date)
            if df is None or df.empty:
                continue
            close = df["close"].astype(float).values
            high = df["high"].astype(float).values
            low = df["low"].astype(float).values
            vol = df["volume"].astype(float).values
            bar_b = df["time"].values.astype("datetime64[ns]").astype(np.int64) // _FIVE_MIN_NS
            n = len(df)
            seq_close = []
            seq_vol = []
            cur_vol = 0.0
            prev_b = None
            prev_close = 0.0
            for i in range(n):
                b = bar_b[i]
                if prev_b is not None and b != prev_b:
                    seq_close.append(prev_close)
                    seq_vol.append(cur_vol)
                    cur_vol = 0.0
                prev_b = b
                prev_close = close[i]
                cur_vol += vol[i]
                if len(seq_close) + 1 < min_bars:
                    continue
                bb = _bb_pct(seq_close, close[i])
                rsi = _rsi(seq_close, close[i], rsi_period)
                if bb is None or rsi is None:
                    continue
                sig = None
                if bb >= bb_up and rsi > sell_rsi:
                    if vol_ratio > 0:
                        avg_v = (sum(seq_vol) / len(seq_vol)) if seq_vol else 0.0
                        if avg_v > 0 and cur_vol / avg_v >= vol_ratio:
                            sig = "SELL_HIGH"
                    else:
                        sig = "SELL_HIGH"
                elif bb <= bb_dn and rsi < buy_rsi:
                    sig = "BUY_LOW"
                if sig is None:
                    continue
                dk = (code, date, b, sig)
                if dk in last_sig:
                    continue
                last_sig.add(dk)
                hi = high[i + 1:i + 31]
                if hi.size == 0:
                    continue
                lo = low[i + 1:i + 31]
                p = close[i]
                if sig == "SELL_HIGH":
                    f_hit = hi >= p * (1 + FAIL_PCT)
                    w_hit = lo <= p * (1 - WIN_PCT)
                else:
                    f_hit = lo <= p * (1 - FAIL_PCT)
                    w_hit = hi >= p * (1 + WIN_PCT)
                f_idx = int(np.argmax(f_hit)) if f_hit.any() else 9999
                w_idx = int(np.argmax(w_hit)) if w_hit.any() else 9999
                if f_idx < w_idx:
                    fails += 1
                elif w_idx < f_idx:
                    wins += 1
    return wins, fails


def _metrics(override: dict, dates: list | None = None) -> dict:
    w, f = _swing_counts(override, dates)
    n = w + f
    return {
        "wins": w, "fails": f, "n": n,
        "win_rate": (w / n if n else 0.0),
        "ev_bp": round((w * WIN_PCT - f * FAIL_PCT) * 100, 1),   # 总期望收益(基点, 常数仓位)
    }


def _trial_params(trial) -> dict:
    return {
        "swing_sell_rsi": trial.suggest_int("swing_sell_rsi", 60, 90),
        "swing_buy_rsi": trial.suggest_int("swing_buy_rsi", 10, 45),
        "swing_bb_upper": trial.suggest_float("swing_bb_upper", 0.8, 1.2),
        "swing_bb_lower": trial.suggest_float("swing_bb_lower", -0.3, 0.3),
        "swing_sell_vol_ratio": trial.suggest_float("swing_sell_vol_ratio", 1.0, 2.5),
        "rsi_period_5m_swing": trial.suggest_int("rsi_period_5m_swing", 4, 14),
    }


def _fmt(m: dict) -> str:
    return f"胜率{m['win_rate']:.1%} 样本{m['n']} EV={m['ev_bp']}bp (wins{m['wins']}/fails{m['fails']})"


def main():
    ap = argparse.ArgumentParser(description="Optuna 寻优做T参数(最大化总期望收益)")
    ap.add_argument("--trials", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--speed", action="store_true", help="只测一次回放耗时")
    args = ap.parse_args()

    if args.speed:
        t0 = time.time()
        m = _metrics(DEFAULT_PARAMS)
        print(f"3年全窗口默认参数: {_fmt(m)} 耗时={time.time()-t0:.1f}s")
        return

    train = [d for d in _DATES if d <= TRAIN_END]
    test = [d for d in _DATES if d > TRAIN_END]
    print(f"训练段: {train[0]} ~ {train[-1]} ({len(train)}交易日) | 测试段: {test[0]} ~ {test[-1]} ({len(test)}交易日)")

    t0 = time.time()
    base_train = _metrics(DEFAULT_PARAMS, train)
    base_test = _metrics(DEFAULT_PARAMS, test)
    print(f"生产默认 | 训练: {_fmt(base_train)} | 测试: {_fmt(base_test)} 耗时={time.time()-t0:.1f}s")

    def objective(trial):
        p = _trial_params(trial)
        m = _metrics(p, train)
        trial.set_user_attr("n", m["n"])
        trial.set_user_attr("params", json.dumps(p))
        trial.set_user_attr("win_rate", m["win_rate"])
        if m["n"] < MIN_SWING_SAMPLES:
            return 1e6        # 样本过少 → 极差
        return -m["ev_bp"]    # 最小化 = 最大化总期望收益

    study = optuna.create_study(direction="minimize",
                                sampler=optuna.samplers.TPESampler(seed=args.seed))
    study.optimize(objective, n_trials=args.trials)

    best = study.best_trial
    best_train = _metrics(best.params, train)
    best_test = _metrics(best.params, test)

    print("\n=== 最优参数(训练段寻优) ===")
    print(json.dumps(best.params, ensure_ascii=False, indent=2))
    print(f"训练段: {_fmt(best_train)}")
    print(f"测试段: {_fmt(best_test)}")
    print(f"\n生产默认 训练段: {_fmt(base_train)} | 测试段: {_fmt(base_test)}")
    print(f"测试段 EV 提升: {best_test['ev_bp'] - base_test['ev_bp']:+.0f}bp")

    out = {
        "window": {"start": DATE_START, "end": DATE_END, "train_end": TRAIN_END},
        "best": {"params": best.params,
                 "train": best_train, "test": best_test},
        "baseline": {"params": DEFAULT_PARAMS,
                     "train": base_train, "test": base_test},
        "trials": args.trials, "seed": args.seed, "min_samples": MIN_SWING_SAMPLES,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (OUT_DIR / "optuna_swing_result_v2.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n结果已写:", OUT_DIR / "optuna_swing_result_v2.json")


if __name__ == "__main__":
    main()
