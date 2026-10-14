# -*- coding: utf-8 -*-
"""超跌跳空确认 · 时点钩子（包内模块 `hooks`）—— 从 gm_main 搬入（2026-10-12）。

## 四个时点（都是**每日一次**，仅 MODE_LIVE）

    09:31  prepare           盘前预筛：前一日涨停家数 top-N 板块 ∩ 昨收<MA20 → 候选
    10:00  try_morning_sell  D+1 持仓早盘冲高 ≥ 入场价×(1+target) → 卖（**先于买侧**）
    10:00  try_buy           10:00 棒 → 决策核 → gap top-N → 下单
    14:50  try_sell          SL5只深 close 止损 + 满 N 日到期平仓

由 `on_bar(rt, context, now, today, t)` 统一分发：内部做**时间窗 + 每日一次 + 模式**闸，
每个钩子各自 try/except（一条钩子炸不影响别的，也不阻断主循环）。

## 依赖注入

本模块**不 import gm.api**（仓规：只有 gm_main 能）。全部掘金能力与宿主服务经 `Runtime`（`rt`）
传入：`rt.gm.*`（原语）/ `rt.sdk_call` / `rt.gateway(context)` / `rt.param` / `rt.audit` /
`rt.risk` / `rt.log` / `rt.book_dir` / `rt.kill_switch` / `rt.is_live`。
执行核与预筛是同包模块（`executor` / `prescreen`），决策核是同包 `decision`。
"""
from __future__ import annotations

import json
import time as _time
from collections import Counter
from datetime import time as dtime
from functools import partial

import pandas as pd

from . import executor, prescreen

# 每日一次状态（原先散在 gm_main 的模块级全局 `_OSC_*_DONE_DATE` + `context._osc_candidates`）
_STATE = {"date": None, "prepare": False, "buy": False, "sell": False,
          "morning": False, "candidates": {}}


def reset_day(today) -> None:
    """按日重置（宿主在日期切换时调用；同时清空预筛候选）。"""
    _STATE.update(date=today, prepare=False, buy=False, sell=False,
                  morning=False, candidates={})


def enabled(rt) -> bool:
    return bool(rt.param("osc_enabled", False))


def sell_enabled(rt) -> bool:
    return bool(rt.param("osc_sell_enabled", True))


def prepare(rt, context, now, today):
    """盘前盘前预筛（每日一次，09:31 触发；取数须在 10:00 之前完成）。"""
    _top = int(rt.param("osc_board_top_n", 5))
    _t0 = _time.time()
    rt.log(f"[OSC] ── 盘前预筛开始 ──（前{_top}热门板块 ∩ 全市场 ∩ 昨收<MA20）")
    try:
        _cand = prescreen.premarket_candidates(_top, str(today))
    except Exception as _e:
        rt.log(f"[OSC] 盘前预筛失败（fail-closed，当日不下单）: {_e}")
        rt.audit({"event": "osc_prepare_fail", "date": str(today),
                      "error": str(_e)[:200]})
        _cand = {}
    _STATE["candidates"] = _cand
    _el = _time.time() - _t0
    rt.log(f"[OSC] 盘前超跌候选 {len(_cand)} 只（前{_top}板块内 且 昨收<MA20）· 耗时 {_el:.1f}s")
    if _cand:
        # 板块分布 + 最超跌样例：让人一眼看出"池子长什么样"，而不是只有一个数字
        _bc = Counter(v.get("board", "") for v in _cand.values())
        rt.log("[OSC]   板块分布: " + " / ".join(f"{b} {n}只" for b, n in _bc.most_common(8)))
        _deep = sorted(_cand.items(),
                       key=lambda kv: (kv[1].get("prev_close", 0) / (kv[1].get("ma20") or 1))
                       if kv[1].get("ma20") else 0)[:5]
        rt.log("[OSC]   最超跌 5 只(昨收/MA20-1): " + " / ".join(
            f"{c} {v['prev_close'] / v['ma20'] - 1:+.1%}" for c, v in _deep if v.get("ma20")))
    else:
        rt.log("[OSC]   [!] 0 只候选 -> 今日买侧不下单（fail-closed）")
    rt.audit({"event": "osc_prepare", "date": str(today), "candidates": len(_cand)})



def bars_from_10am_df(df, codes, now):
    """GM history(df=True) → {code: 10:00棒的 low/close/amount/volume}。防御式。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    try:
        df = df.copy()
        df["_code"] = df["symbol"].astype(str).str.split(".").str[-1]
        df["_hm"] = pd.to_datetime(df["eob"]).dt.strftime("%H:%M:%S")
        df = df[df["_hm"] == "10:00:00"]
        for c in codes:
            _row = df[df["_code"] == c]
            if _row.empty:
                continue
            _r = _row.iloc[0]
            try:
                low = float(_r.get("low") or 0)
                close = float(_r.get("close") or 0)
                amount = float(_r.get("amount") or 0)
                volume = float(_r.get("volume") or 0)
            except Exception:
                continue
            if low > 0 and close > 0:
                out[c] = {"low": low, "close": close, "amount": amount, "volume": volume}
    except Exception:
        pass
    return out



def try_buy(rt, context, now) -> int:
    """10:00 超跌跳空确认买入（每日一次）。返回已下单腿数。fail-closed：候选/棒缺失当日不下单。"""
    _exec = executor
    # KILL_SWITCH（人工红按钮，存在即禁新开仓）：2026-10-12 之前只有已删除的旧引擎读它，
    # 删后由本条（**本策略唯一的开仓路径**）承接，避免整个系统失去这道人工闸。
    try:
        if rt.kill_switch():
            rt.log(f"[OSC] [X] KILL_SWITCH 存在 -> 今日不开新仓")
            rt.audit({"event": "osc_skip", "reason": "kill_switch", "time": str(now)})
            return 0
    except Exception:
        pass
    _cand = dict(_STATE["candidates"])
    if not _cand:
        rt.audit({"event": "osc_skip", "reason": "no_candidates", "time": str(now)})
        return 0
    _pc, _ma = prescreen.split_into_prev_close_ma20(_cand)
    _board = {c: v.get("board", "") for c, v in _cand.items()}
    _codes = sorted(_cand)
    _syms = [rt.code_to_gm(c) for c in _codes]
    _start = now.strftime("%Y-%m-%d 09:25:00")
    _end = now.strftime("%Y-%m-%d 10:01:00")
    try:
        _df = rt.sdk_call("osc_10am_bars", partial(
            rt.gm.history, symbol=_syms, frequency="1800s", start_time=_start, end_time=_end,
            fields="symbol,eob,open,high,low,close,volume,amount",
            adjust=rt.gm.ADJUST_PREV, df=True))
    except Exception as _e:
        rt.log(f"[OSC] 10:00 棒取数失败（fail-closed，当日不下单）: {_e}")
        rt.audit({"event": "osc_fetch_fail", "error": str(_e)[:200], "time": str(now)})
        return 0
    _bars = bars_from_10am_df(_df, _codes, now)
    rt.log(f"[OSC] 10:00 取数: 拿到 {len(_bars)}/{len(_codes)} 只的 10:00 棒"
           + (f"（缺 {len(_codes) - len(_bars)} 只，fail-closed 跳过）" if len(_bars) < len(_codes) else ""))
    if not _bars:
        rt.audit({"event": "osc_skip", "reason": "no_bars", "time": str(now)})
        return 0
    _book = rt.book_dir
    _params = {
        "top_n": int(rt.param("osc_top_n", 4)),
        "single_budget": float(rt.param("osc_single_budget", 100000.0)),
        "daily_budget": float(rt.param("osc_daily_budget", 400000.0)),
        "cash_headroom": float(rt.param("osc_cash_headroom", 0.95)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = executor.run_buy_exec(rt.gateway(context), str(now.date()),
                                 prev_close=_pc, ma20=_ma, bars=_bars, board=_board,
                                 allowed_boards=None, book_dir=_book, params=_params,
                                 dry_run=False, log=print)
        _sm = (_res or {}).get("summary") or {}
        rt.audit({"event": "osc_buy_exec", "date": str(now.date()),
                      "candidates": _sm.get("candidates"), "buys": _sm.get("buys_placed"),
                      "rejected": _sm.get("rejected_count"),
                      "picks": [p["code"] for p in (_res or {}).get("picks") or []]})
        _pk = (_res or {}).get("picks") or []
        if _pk:
            rt.log("[OSC] 10:00 选中(gap 降序): " + " / ".join(
                f"{p['code']} gap{p['gap']:+.1%}" for p in _pk))
        else:
            rt.log("[OSC] 10:00 无信号触发（缺口未守住/未站上昨收或 VWAP）-> 今日不买")
        rt.log(f"[OSC] 10:00 买侧执行: {json.dumps(_sm, ensure_ascii=False)}")
        return int(_sm.get("buys_placed") or 0)
    except Exception as _e:
        rt.log(f"[OSC] 买侧执行失败（不阻断主循环）: {_e}")
        try:
            rt.risk(str(now), "osc_exec_error", f"超跌跳空确认执行异常: {str(_e)[:200]}", code="")
        except Exception:
            pass
        return 0


def sell_codes(rt) -> list:
    """当前超跌跳空确认账本在持仓的 6 位码列表（空=无需卖侧检查）。"""
    try:
        _exec = executor
        _book = rt.book_dir
        return sorted((executor.load_book(_book).get("open") or {}).keys())
    except Exception:
        return []


def latest_close_from_df(df, codes, now):
    """GM history(df=True) → {code: 当日最新一根 bar 的 close}。防御式。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    try:
        df = df.copy()
        df["_code"] = df["symbol"].astype(str).str.split(".").str[-1]
        df = df.sort_values("eob")
        for c in codes:
            _row = df[df["_code"] == c]
            if _row.empty:
                continue
            _r = _row.iloc[-1]
            try:
                close = float(_r.get("close") or 0)
            except Exception:
                continue
            if close > 0:
                out[c] = close
    except Exception:
        pass
    return out



def sell_time(rt):
    """卖侧检查时点（dtime）。"""
    _hhmm = int(rt.param("osc_sell_time_hhmm", 1450))
    return dtime(_hhmm // 100, _hhmm % 100)


def try_sell(rt, context, now, today) -> int:
    """14:50 超跌跳空确认卖侧（每日一次）：SL5只深 close 止损 + 到期平仓。返回已下单腿数。"""
    _exec = executor
    _reconcile_book(rt, context)                 # ⚠️ 必须先对账（同上）
    _codes = sell_codes(rt)
    if not _codes:
        rt.log("[OSC] 14:50 卖侧: 账本无持仓，跳过")
        return 0
    rt.log(f"[OSC] 14:50 卖侧: 持仓 {len(_codes)} 只 → {_codes}")
    _syms = [rt.code_to_gm(c) for c in _codes]
    _start = now.strftime("%Y-%m-%d 09:25:00")
    _end = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _df = rt.sdk_call("osc_sell_bars", partial(
            rt.gm.history, symbol=_syms, frequency="60s", start_time=_start, end_time=_end,
            fields="symbol,eob,close", adjust=rt.gm.ADJUST_PREV, df=True))
    except Exception as _e:
        rt.log(f"[OSC] 卖侧现价取数失败（fail-closed，当日不卖）: {_e}")
        rt.audit({"event": "osc_sell_fetch_fail", "error": str(_e)[:200], "time": str(now)})
        return 0
    _prices = latest_close_from_df(_df, _codes, now)
    if not _prices:
        rt.audit({"event": "osc_sell_skip", "reason": "no_prices", "time": str(now)})
        return 0
    _book = rt.book_dir
    _params = {
        "hold_days": int(rt.param("osc_hold_days", 5)),
        "stop_pct": float(rt.param("osc_stop_pct", 0.05)),
        "stop_deep_dev20": float(rt.param("osc_stop_deep_dev20", -0.07)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = executor.run_sell_exec(rt.gateway(context), str(today), _prices,
                                  book_dir=_book, params=_params, dry_run=False, log=print)
        _sm = (_res or {}).get("summary") or {}
        rt.audit({"event": "osc_sell_exec", "date": str(today),
                      "sells": _sm.get("sells_placed"), "stops": _sm.get("stop_count"),
                      "expired": _sm.get("expired_count"),
                      "open_after": _sm.get("open_after"),
                      "rejected": _sm.get("rejected_count")})
        rt.log(f"[OSC] 卖侧执行: {json.dumps(_sm, ensure_ascii=False)}")
        return int(_sm.get("sells_placed") or 0)
    except Exception as _e:
        rt.log(f"[OSC] 卖侧执行失败（不阻断主循环）: {_e}")
        try:
            rt.risk(str(now), "osc_sell_error", f"超跌跳空确认卖侧异常: {str(_e)[:200]}", code="")
        except Exception:
            pass
        return 0


def morning_high_from_df(df, codes, now):
    """GM history(df=True) → {code: 早盘(09:30~当前)最高价}。防御式，取 high 列最大值。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    try:
        df = df.copy()
        df["_code"] = df["symbol"].astype(str).str.split(".").str[-1]
        for c in codes:
            _row = df[df["_code"] == c]
            if _row.empty:
                continue
            try:
                hi = float(_row["high"].max())
            except Exception:
                continue
            if hi > 0:
                out[c] = hi
    except Exception:
        pass
    return out


def morning_sell_time(rt):
    """早盘冲高卖检查时点（dtime）。"""
    _hhmm = int(rt.param("osc_morning_sell_time_hhmm", 1000))
    return dtime(_hhmm // 100, _hhmm % 100)


def _reconcile_book(rt, context) -> None:
    """卖前先把账本按**账户实际持仓**夹紧（挂单未成交/部分成交都会让账本虚高）。

    见 `executor.reconcile_book` 的说明 —— 上线首日实测出现过「账本 5 只 / 账户 3 只」。
    """
    try:
        _gw = rt.gateway(context)
        _acct = {str(c).split(".")[-1]: int((v or {}).get("qty") or 0)
                 for c, v in (_gw.get_positions() or {}).items()}
        _n = executor.reconcile_book(rt.book_dir, _acct)
        if _n:
            rt.log(f"[OSC] 账本对账: 按账户夹紧/剔除 {_n} 笔（挂单未成交 or 部分成交）")
    except Exception as _e:
        rt.log(f"[OSC] 账本对账失败（不阻断本次卖出）: {_e}")


def try_morning_sell(rt, context, now, today) -> int:
    """10:00 超跌跳空确认早盘冲高卖（每日一次，先于买侧）：D+1 持仓早盘冲 ≥ entry×(1+target) 就卖。

    只卖 days_held==0（次日）持仓；更老持仓不动，交 14:50 止损/到期。与买侧同刻触发，
    但钩子排在买侧之前，先卖回笼现金再买。
    """
    _exec = executor
    _reconcile_book(rt, context)                 # ⚠️ 必须先对账：挂单未成交会让账本虚高
    _codes = sell_codes(rt)
    if not _codes:
        rt.log("[OSC] 10:00 早盘冲高卖: 账本无持仓，跳过")
        return 0
    rt.log(f"[OSC] 10:00 早盘冲高卖: 持仓 {len(_codes)} 只 → {_codes}")
    _syms = [rt.code_to_gm(c) for c in _codes]
    _start = now.strftime("%Y-%m-%d 09:25:00")
    _end = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _df = rt.sdk_call("osc_morning_high_bars", partial(
            rt.gm.history, symbol=_syms, frequency="1800s", start_time=_start, end_time=_end,
            fields="symbol,eob,high", adjust=rt.gm.ADJUST_PREV, df=True))
    except Exception as _e:
        rt.log(f"[OSC] 早盘最高价取数失败（fail-closed，当日不卖）: {_e}")
        rt.audit({"event": "osc_morning_sell_fetch_fail", "error": str(_e)[:200], "time": str(now)})
        return 0
    _highs = morning_high_from_df(_df, _codes, now)
    if not _highs:
        rt.audit({"event": "osc_morning_sell_skip", "reason": "no_highs", "time": str(now)})
        return 0
    _book = rt.book_dir
    _params = {
        "pop_target_pct": float(rt.param("osc_pop_target_pct", 0.03)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = executor.run_morning_sell_exec(rt.gateway(context), str(today), _highs,
                                          book_dir=_book, params=_params, dry_run=False, log=print)
        _sm = (_res or {}).get("summary") or {}
        rt.audit({"event": "osc_morning_sell_exec", "date": str(today),
                      "sells": _sm.get("sells_placed"), "pops": _sm.get("pop_count"),
                      "open_after": _sm.get("open_after"),
                      "rejected": _sm.get("rejected_count")})
        rt.log(f"[OSC] 早盘冲高卖执行: {json.dumps(_sm, ensure_ascii=False)}")
        return int(_sm.get("sells_placed") or 0)
    except Exception as _e:
        rt.log(f"[OSC] 早盘冲高卖执行失败（不阻断主循环）: {_e}")
        try:
            rt.risk(str(now), "osc_morning_sell_error", f"超跌跳空确认早盘卖侧异常: {str(_e)[:200]}", code="")
        except Exception:
            pass

# ════════════════════════ 分发（宿主唯一入口）════════════════════════

def on_bar(rt, context, now, today, t) -> None:
    """四个钩子的时间窗 + 每日一次 + 模式闸。异常一律吞掉（不阻断主循环）。"""
    if not enabled(rt):
        return
    live = rt.is_live(context)
    # 09:31 盘前预筛（候选要先于 10:00 备好）
    if live and t >= dtime(9, 31) and not _STATE["prepare"]:
        _STATE["prepare"] = True
        try:
            prepare(rt, context, now, today)
        except Exception as _e:
            rt.log(f"[OSC] 盘前预筛失败（不阻断主循环）: {_e}")
    # 10:00 早盘冲高卖（**先于买侧**：先回笼现金再买）
    if (live and sell_enabled(rt) and t >= morning_sell_time(rt)
            and not _STATE["morning"]):
        _STATE["morning"] = True
        try:
            try_morning_sell(rt, context, now, today)
        except Exception as _e:
            rt.log(f"[OSC] 10:00 早盘冲高卖失败（不阻断主循环）: {_e}")
    # 10:00 买侧
    if live and t >= dtime(10, 0) and not _STATE["buy"]:
        _STATE["buy"] = True
        try:
            try_buy(rt, context, now)
        except Exception as _e:
            rt.log(f"[OSC] 10:00 买侧失败（不阻断主循环）: {_e}")
    # 14:50 止损/到期
    if (live and sell_enabled(rt) and t >= sell_time(rt) and not _STATE["sell"]):
        _STATE["sell"] = True
        try:
            try_sell(rt, context, now, today)
        except Exception as _e:
            rt.log(f"[OSC] 14:50 卖侧失败（不阻断主循环）: {_e}")

    # ── 周期心跳（每 30 分钟一格）────────────────────────────────────────────
    # 为什么要有：四个钩子都是"每日一次"，盘前 09:31 到 10:00、10:00 到 14:50 之间**一行日志都没有**
    # ⇒ owner 2026-10-14 反馈「log 更新太少，不知道它具体在干什么」。这里每半小时报一行"我还活着 +
    # 手里有什么 + 今天做完哪些动作"，把信息真空填掉，且**不碰任何判定**。
    # 60s 的 bar 循环里靠**槽位去重**（不然每个 bar 打一行、240 行/天）。
    if live and t >= dtime(9, 31):
        _slot = "%02d:%s" % (now.hour, "00" if now.minute < 30 else "30")
        if _STATE.get("hb_slot") != _slot:
            _STATE["hb_slot"] = _slot
            try:
                _done = [k for k in ("prepare", "morning", "buy", "sell") if _STATE.get(k)]
                _t = {"prepare": "预筛", "morning": "早盘卖", "buy": "买侧", "sell": "收盘卖"}
                rt.log(f"[OSC] {_slot} 心跳 · 持仓 {len(sell_codes(rt))} 只 · "
                       f"今日已完成 {'/'.join(_t[k] for k in _done) if _done else '无'}")
            except Exception:
                pass
