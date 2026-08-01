# -*- coding: utf-8 -*-
"""
replay_day.py — 2026-07-24 单日实盘回放器（V1.30 复盘对照专用）

用法：
    python replay_day.py --engine baseline --regime trace  --tag baseline_trace
    python replay_day.py --engine fixed    --regime trace  --tag fixed_trace
    python replay_day.py --engine fixed    --regime locked --tag fixed_locked

在 /e/06_T（修复版）或 /e/06_T_baseline（基线）目录下运行；
--engine 只决定"信号处理编排逻辑"（对应各版本 main.py 的信号块），
引擎/仓控/配置代码由运行目录决定。

数据输入（全部为 07-24 实盘落盘的本地缓存，无网络）：
  t_io/cache/minute_{code}_2026-07-24.csv     当日 1 分钟线（腾讯，242 根）
  t_io/cache/tushare_mins/{code}/*.csv        历史 5 分钟线（聚合日线上下文，截至 07-23）

输出（不污染实盘 traces）：
  t_io/replay/<tag>/events.jsonl   每次评估/推送/阻断事件
  t_io/replay/<tag>/summary.json   汇总（推送明细、闭环盈亏、审计口径对比）
"""
import argparse, json, os, sys
from pathlib import Path
from datetime import datetime, timedelta, time as dtime

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
REPLAY_DATE = "2026-07-24"
CODES = ["588170", "600176", "600481", "603667", "000988"]

# 07-24 开盘持仓（实盘早盘真实状态；pre_close 为实盘当时的陈旧值，两个版本保持一致输入）
HOLDINGS0 = {
    "588170": {"name": "科创半导体ETF华夏", "cost": 0.782, "qty": 4000, "base": 4000, "t_qty": 4000, "type": "etf", "account": "账户A", "pre_close": 1.07},
    "600176": {"name": "中国巨石", "cost": 68.128, "qty": 300, "base": 300, "t_qty": 300, "type": "stock", "account": "账户A", "pre_close": 43.0},
    "600481": {"name": "双良节能", "cost": 6.028, "qty": 1400, "base": 1400, "t_qty": 1400, "type": "stock", "account": "账户A", "pre_close": 3.77},
    "603667": {"name": "五洲新春", "cost": 60.867, "qty": 400, "base": 400, "t_qty": 400, "type": "stock", "account": "账户A", "pre_close": 55.36},
    "000988": {"name": "华工科技", "cost": 313.335, "qty": 300, "base": 300, "t_qty": 300, "type": "stock", "account": "账户A", "pre_close": 113.15},
}

# 模式A：从实盘 decision_trace 的"大盘态势(uni_down)"因子出现/消失重建的抖动时间线
#   09:34 range → 09:42 uni_down → 10:13 range → 10:46 uni_down → 收盘
# 模式B（修复后 index_regime_intraday_lock）：全天锁定 07-23 EOD 判定 = range
def regime_at(mode: str, hhmm: int) -> str:
    if mode == "locked":
        return "range"
    if 942 <= hhmm < 1013:
        return "uni_down"
    if hhmm >= 1046:
        return "uni_down"
    return "range"


def load_shared() -> dict:
    """仿 backtest.py 的共享命名空间加载（模块间通过 globals 互通）。"""
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

    shared = {'akshare': ak_mock, 'ak': ak_mock}
    shared.update({
        '__name__': '__main__', '__file__': str(BASE_DIR / 'replay_day.py'),
        'os': _os, 'sys': _sys, 'json': _json, 'time': _time, 'logging': _logging, 'traceback': _traceback,
        'dataclass': dataclass, 'field': field,
        'datetime': _dt, 'timedelta': _td, 'dtime': _dtime,
        'Dict': Dict, 'List': List, 'Optional': Optional, 'Any': Any,
        'np': np, 'pd': pd, 'requests': requests, 'urllib': urllib,
        'urllib.request': urllib.request, 'urllib.error': urllib.error,
    })
    for mod_name in ['config', 'utils', 'data_fetcher', 'indicators', 'multi_timeframe_fetcher', 'signal_engine', 'position_sizer']:
        mod_path = BASE_DIR / f"{mod_name}.py"
        with open(mod_path, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(compile(code, str(mod_path), 'exec'), shared)
    return shared


def build_daily_df(code: str) -> pd.DataFrame:
    """tushare_mins 5分钟线聚合日线（截至 2026-07-23，无未来函数）。"""
    folder = BASE_DIR / "t_io" / "cache" / "tushare_mins" / code
    rows = []
    for p in sorted(folder.glob("*.csv")):
        if p.stem >= REPLAY_DATE:
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        if df.empty:
            continue
        rows.append({
            "date": p.stem,
            "open": float(df.iloc[0]["open"]),
            "close": float(df.iloc[-1]["close"]),
            "high": float(df["high"].max()),
            "low": float(df["low"].min()),
            "volume": float(df["volume"].sum()),
            "amount": float(df["amount"].sum()) if "amount" in df.columns else 0.0,
        })
    return pd.DataFrame(rows)


def load_day_minutes(code: str) -> pd.DataFrame:
    p = BASE_DIR / "t_io" / "cache" / f"minute_{code}_{REPLAY_DATE}.csv"
    df = pd.read_csv(p)
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["baseline", "fixed"], required=True)
    ap.add_argument("--regime", choices=["trace", "locked"], required=True)
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()

    out_dir = BASE_DIR / "t_io" / "replay" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()

    shared = load_shared()

    # ── 环境隔离：traces/虚拟账/盘中状态全部指向回放目录，不碰实盘文件 ──
    shared['TRACE_DIR'] = str(out_dir)
    shared['VIRTUAL_TRADES_FILE'] = str(out_dir / "virtual_trades.json")
    if 'PERSIST_INTRADAY_STATE' in shared:
        shared['PERSIST_INTRADAY_STATE'] = False
    shared['HOLDINGS'] = {c: dict(h) for c, h in HOLDINGS0.items()}

    PARAMS = shared['PARAMS']
    STOCK_PARAMS = shared['STOCK_PARAMS']
    _se = sys.modules.get('signal_engine')  # 不存在（共享命名空间），用 shared 取
    VIRTUAL_TRADES = shared['VIRTUAL_TRADES']
    VIRTUAL_TRADES.clear()
    MINUTE_FETCH_STATUS = shared.get('MINUTE_FETCH_STATUS')
    if MINUTE_FETCH_STATUS is not None:
        for c in CODES:
            MINUTE_FETCH_STATUS[c] = "ok"

    engine = shared['SignalEngine']()
    add_indicators = shared['add_indicators']
    build_ctx = shared['_build_daily_context_from_df']
    calc_sell_qty = shared['calc_sell_qty']
    calc_buy_qty = shared['calc_buy_qty']
    write_shadow = shared.get('write_shadow_signal')  # 仅修复版有

    # ── 数据准备 ──
    day_bars = {c: load_day_minutes(c) for c in CODES}
    daily_dfs = {c: build_daily_df(c) for c in CODES}
    n_daily = {c: len(d) for c, d in daily_dfs.items()}

    pushes = []          # 推送事件
    blocked = []         # 阻断事件
    n_evals = n_signals = n_silent = 0
    decision_reasons = {}

    # 通知阈值（与两版本 main.py 一致的双层逻辑）
    def notify_threshold(code, action, t_hhmm, daily_ctx):
        _sp = STOCK_PARAMS.get(code, {})
        if action in ("BUY_LOW", "ADD_POS"):
            return _sp.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
        if t_hhmm >= 1000:
            return _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
        today_ret_snap = daily_ctx.get("daily_day_ret", 0.0)
        if today_ret_snap < -0.04 and action in ("PANIC_SELL", "SELL_HIGH"):
            return PARAMS.get("notify_sell_panic_threshold", 60)
        return _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_early_threshold", 75)

    def record_action(code, action, qty, price):
        """版本自适应记账：修复版带 price，基线不带。"""
        try:
            engine.record_trade_action(code, action, qty, price=price)
        except TypeError:
            engine.record_trade_action(code, action, qty)

    # ── 逐分钟回放（所有代码同一时间轴，仿实盘 scan_once 轮询） ──
    bar_times = sorted(set().union(*[set(day_bars[c]["time"]) for c in CODES]))
    for bt in bar_times:
        shared['SIM_NOW'] = pd.Timestamp(bt).to_pydatetime()
        hhmm = bt.hour * 100 + bt.minute
        regime = regime_at(args.regime, hhmm)
        for code in CODES:
            dfc = day_bars[code]
            sub = dfc[dfc["time"] <= bt]
            if len(sub) < 5:
                continue
            df_ind = add_indicators(sub.copy())
            price = float(df_ind.iloc[-1]["close"])
            holding = dict(HOLDINGS0[code])
            daily_ctx = build_ctx(code, daily_dfs[code], current_price=price)
            daily_ctx["index_regime"] = regime
            daily_ctx["index_regime_score"] = -41.0 if regime == "uni_down" else -35.86
            daily_ctx["intraday_alerts"] = []
            n_evals += 1
            try:
                buy_score, sell_score, sig = engine.evaluate(
                    code, holding["name"], df_ind, holding, daily_ctx=daily_ctx)
            except Exception as e:
                blocked.append({"ts": str(bt), "code": code, "kind": "exception",
                                "msg": f"{type(e).__name__}: {str(e)[:100]}"})
                continue
            # 决策原因码（仅修复版有）
            ld = getattr(engine, "last_decision", {})
            if isinstance(ld, dict) and code in ld:
                decision_reasons[f"{code}@{bt.strftime('%H:%M')}"] = ld[code].get("reason", "")
            if sig is None or sig.action not in ("BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"):
                continue
            n_signals += 1

            # ── 动态份数（两版本 main.py 一致；regime 对象传 None，仓控内部按 normal 处理） ──
            threshold = float(sig.factors.get("threshold", 35))
            cur_price = float(sig.price or price)
            merged_params = {**PARAMS, **STOCK_PARAMS.get(code, {})}
            if sig.action in ("SELL_HIGH", "PANIC_SELL"):
                dynamic_qty = calc_sell_qty(
                    code, holding, None, float(sig.score), threshold,
                    used_sells=engine.sell_count_per_stock.get(code, 0),
                    params=merged_params, virtual_trades=VIRTUAL_TRADES,
                    index_ctx=daily_ctx, current_price=cur_price)
            else:
                dynamic_qty = calc_buy_qty(
                    code, holding, None, float(sig.score), threshold,
                    params=merged_params, virtual_trades=VIRTUAL_TRADES,
                    index_ctx=daily_ctx, current_price=cur_price)
            sig.hold_qty = int(dynamic_qty or 0)

            nth = notify_threshold(code, sig.action, hhmm, daily_ctx)
            ev_base = {"ts": bt.strftime("%H:%M"), "code": code, "action": sig.action,
                       "score": round(float(sig.score), 1), "price": cur_price,
                       "qty": sig.hold_qty, "nth": nth, "regime": regime}

            if args.engine == "baseline":
                # ── 基线 main.py 信号块：推送与记账分离，静默+有量也记账（幽灵交易来源） ──
                if sig.score >= nth:
                    pushes.append({**ev_base, "kind": "push"})
                    if sig.action in ("SELL_HIGH", "PANIC_SELL"):
                        engine.cycle_count[code] = engine.cycle_count.get(code, 0) + 1
                else:
                    n_silent += 1
                if sig.hold_qty > 0:
                    engine.record_signal(code, sig.action, sig.price, sig.score)
                    record_action(code, sig.action, sig.hold_qty, cur_price)
                    blocked.append({**ev_base, "kind": "record",
                                    "pushed": bool(sig.score >= nth)})
                else:
                    blocked.append({**ev_base, "kind": "qty0_no_record"})
            else:
                # ── 修复版 main.py 信号块（V1.30） ──
                pushed = sig.score >= nth
                if pushed and sig.action in ("SELL_HIGH", "PANIC_SELL"):
                    if engine.cycle_count.get(code, 0) >= PARAMS["max_t_cycles_per_stock"]:
                        pushed = False
                        blocked.append({**ev_base, "kind": "cycle_cap_block"})
                if pushed and sig.hold_qty > 0:
                    pushes.append({**ev_base, "kind": "push"})
                    if sig.action in ("SELL_HIGH", "PANIC_SELL"):
                        if hasattr(engine, "incr_cycle"):
                            engine.incr_cycle(code)
                        else:
                            engine.cycle_count[code] = engine.cycle_count.get(code, 0) + 1
                    engine.record_signal(code, sig.action, sig.price, sig.score)
                    record_action(code, sig.action, sig.hold_qty, cur_price)
                elif pushed:
                    blocked.append({**ev_base, "kind": "qty0_cooldown_only"})
                    engine.record_signal(code, sig.action, sig.price, sig.score)
                else:
                    n_silent += 1
                    if write_shadow is not None:
                        try:
                            _sp2 = STOCK_PARAMS.get(code, {})
                            _nb = _sp2.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
                            _ns = _sp2.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
                            write_shadow(
                                code, holding["name"], cur_price,
                                float(sig.indicators.get("vwap", cur_price) or cur_price),
                                buy_score, sell_score, _nb, _ns,
                                "低于推送阈值静默",
                                extra={"action": sig.action,
                                       "decision_reason": getattr(engine, "last_decision", {}).get(code, {}).get("reason", "")})
                        except Exception:
                            pass

    # ── 事件落盘 ──
    with open(events_path, "w", encoding="utf-8") as f:
        for e in pushes:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
        for e in blocked:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    # ── 汇总：虚拟账 + 闭环盈亏（FIFO 配对） + 审计双口径 ──
    commission = PARAMS.get("commission_rate", 0.0015)
    stamp = PARAMS.get("stamp_tax_rate", 0.0005)
    book = {}
    total_matched_net = 0.0
    total_matched_gross = 0.0
    n_closed = 0
    naive_est = 0.0   # 基线审计口径：全部记录参与（price=0 也照算 → 伪影来源）
    valid_est = 0.0   # 修复版审计口径：仅 price>0 有效记录
    for code in CODES:
        vt = VIRTUAL_TRADES.get(code, {})
        sells = list(vt.get("SELL_HIGH", [])) + list(vt.get("PANIC_SELL", []))
        buys = list(vt.get("BUY_LOW", [])) + list(vt.get("ADD_POS", []))
        cost = HOLDINGS0[code]["cost"]
        for t in sells:
            p = float(t.get("price", 0) or 0)
            naive_est += (p - cost) * t.get("qty", 0)
            if p > 0:
                valid_est += (p - cost) * t.get("qty", 0)
        for t in buys:
            p = float(t.get("price", 0) or 0)
            naive_est -= (p - cost) * t.get("qty", 0)
            if p > 0:
                valid_est -= (p - cost) * t.get("qty", 0)
        # FIFO 闭环
        bq = [(float(t.get("price", 0) or 0), int(t.get("qty", 0))) for t in buys]
        sq = [(float(t.get("price", 0) or 0), int(t.get("qty", 0))) for t in sells]
        bi = si = 0
        code_net = code_gross = 0.0
        code_closed = 0
        while bi < len(bq) and si < len(sq):
            bp, bqty = bq[bi]
            sp, sqty = sq[si]
            m = min(bqty, sqty)
            if bp > 0 and sp > 0 and m > 0:
                gross = (sp - bp) * m
                net = gross - bp * m * commission - sp * m * (commission + stamp)
                code_gross += gross
                code_net += net
                code_closed += 1
            bqty -= m
            sqty -= m
            if bqty <= 0:
                bi += 1
            if sqty <= 0:
                si += 1
            if bi < len(bq):
                bq[bi] = (bq[bi][0], bqty)
            if si < len(sq):
                sq[si] = (sq[si][0], sqty)
        book[code] = {
            "sells": [{"ts": str(t.get("ts", ""))[11:16], "qty": t.get("qty"), "price": round(float(t.get("price", 0) or 0), 3)} for t in sells],
            "buys": [{"ts": str(t.get("ts", ""))[11:16], "qty": t.get("qty"), "price": round(float(t.get("price", 0) or 0), 3)} for t in buys],
            "closed_cycles": code_closed,
            "matched_gross": round(code_gross, 2),
            "matched_net": round(code_net, 2),
            "cycle_count": engine.cycle_count.get(code, 0),
            "sell_count": engine.sell_count_per_stock.get(code, 0),
            "buy_count": engine.buy_count_per_stock.get(code, 0),
        }
        total_matched_net += code_net
        total_matched_gross += code_gross
        n_closed += code_closed

    shadow_file = out_dir / f"shadow_signals_{REPLAY_DATE}.jsonl"
    n_shadow = 0
    if shadow_file.exists():
        n_shadow = sum(1 for _ in open(shadow_file, encoding="utf-8"))

    summary = {
        "tag": args.tag, "engine": args.engine, "regime_mode": args.regime,
        "date": REPLAY_DATE,
        "daily_bars_available": n_daily,
        "n_evals": n_evals, "n_signals": n_signals,
        "n_pushed": len(pushes), "n_silent": n_silent,
        "n_blocked": len(blocked),
        "pushes": pushes,
        "blocked": blocked,
        "book": book,
        "closed_cycles": n_closed,
        "matched_gross": round(total_matched_gross, 2),
        "matched_net": round(total_matched_net, 2),
        "naive_est_pnl": round(naive_est, 2),
        "valid_est_pnl": round(valid_est, 2),
        "shadow_signals_written": n_shadow,
        "decision_reasons_sample": dict(list(decision_reasons.items())[:40]),
    }
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)

    print(f"[{args.tag}] evals={n_evals} signals={n_signals} pushed={len(pushes)} "
          f"silent={n_silent} blocked={len(blocked)} closed={n_closed} "
          f"matched_net={total_matched_net:.2f} naive_est={naive_est:.2f} valid_est={valid_est:.2f} "
          f"shadow={n_shadow}")
    for p in pushes:
        print(f"  PUSH {p['ts']} {p['code']} {p['action']} score={p['score']} qty={p['qty']} price={p['price']}")


if __name__ == "__main__":
    main()
