# -*- coding: utf-8 -*-
"""30min 趋势判定 — 适配层：取数 → 指标 → 状态机 → 最新快照。

数据获取**不复用** `analysis/divergence.py::fetch_freq_kline`——后者按「当日」缓存，
盘中首拉一次后整天不再刷新（背离列可容忍陈旧，趋势判定不行）。本层改为**原生 30min**
（tushare stk_mins freq="30min"，轻量）+ **按 30 分钟时段缓存**：同一时段内秒回、跨时段
自动重取，状态随每根 30min bar 收盘推进。tushare 访问器 `_iri_tushare_pro` 与 ts_code
映射 `_ts_code` 仍复用。

并发：模块级 Semaphore 限流 + 失败重试退避，避免批量标签刷新时打爆 tushare 配额。
失败/数据不足时返回 `source ∈ {error, insufficient}`，由调用方回退日线口径。
"""
import os
import sys
import threading
import time
from datetime import datetime

import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from analysis.trend30.indicators import (  # noqa: E402
    drop_forming_bar, collapse_stubs, mark_bar_meta, add_30min_indicators, linreg_quality,
)
from analysis.trend30.state_machine import Trend30Config, Trend30StateMachine  # noqa: E402

_CACHE = {}
_LOCK = threading.Lock()
_SEM = threading.Semaphore(8)
_RETRY = 2

_STATE2TREND = {"BULL": "up", "BEAR": "down", "RANGE": "flat"}


def state_to_trend(state) -> str:
    """BULL→up / BEAR→down / RANGE→flat（未知/None → flat）。"""
    return _STATE2TREND.get(str(state or "").upper(), "flat")


def _slot(dt=None) -> str:
    """当前 30 分钟时段键（如 '2026-10-04 10:30'）。"""
    dt = dt or datetime.now()
    return dt.strftime("%Y-%m-%d %H:") + ("00" if dt.minute < 30 else "30")


def _fetch_30min(code: str, days: int = 35) -> pd.DataFrame:
    """原生 30min（tushare stk_mins freq='30min'）。返回 {time,open,high,low,close,volume(,amount)}。"""
    from analysis.divergence import _ts_code
    from analysis.index_regime_intraday import _iri_tushare_pro
    pro = _iri_tushare_pro()
    start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    df = pro.stk_mins(ts_code=_ts_code(code), freq="30min",
                      start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume"})
    df["time"] = pd.to_datetime(df["time"])
    keep = [c for c in ("time", "open", "close", "high", "low", "volume", "amount") if c in df.columns]
    return df[keep].sort_values("time").reset_index(drop=True)


def get_trend30(code: str, days: int = 35, cfg: Trend30Config = None, use_cache: bool = True) -> dict:
    """一站式：取数 → 指标 → 状态机 → 最新快照。
    返回 {state, trend('up'/'down'/'flat'), confidence, adx, bar_time, n_bars, gate_ok, source}；
    失败返回 {source:'error'|'insufficient', trend:None, state:None}。"""
    code = str(code).split("_")[0]
    cfg = cfg or Trend30Config()
    slot = _slot()
    if use_cache:
        with _LOCK:
            c = _CACHE.get(code)
            if c and c.get("slot") == slot:
                return c["value"]

    df = None
    with _SEM:
        for att in range(_RETRY + 1):
            try:
                df = _fetch_30min(code, days)
                if df is not None and not df.empty:
                    break
            except Exception:
                df = None
            if att < _RETRY:
                time.sleep(0.4 * (att + 1))

    if df is None or df.empty:
        return {"source": "error", "trend": None, "state": None, "code": code}

    try:
        d = drop_forming_bar(df)
        d = collapse_stubs(d)
        d = mark_bar_meta(d)
        d = add_30min_indicators(d, st_n=cfg.st_n, st_mult=cfg.st_mult)
        d = d.join(linreg_quality(d["close"], n=cfg.reg_n))
    except Exception:
        return {"source": "error", "trend": None, "state": None, "code": code}

    if len(d) < cfg.min_bars:
        return {"source": "insufficient", "trend": None, "state": None,
                "n_bars": len(d), "code": code}

    try:
        sm = Trend30StateMachine(cfg)
        sm.run(d)
        cur = sm.current()
    except Exception:
        return {"source": "error", "trend": None, "state": None, "code": code}

    _last = d.iloc[-1]
    _r2 = _last.get("r2", np.nan)
    _er = _last.get("er10", np.nan)
    res = {"source": "30min", "code": code, "trend": state_to_trend(cur["state"]),
           "state": cur["state"], "confidence": cur["confidence"], "adx": cur["adx"],
           "bar_time": cur["bar_time"], "n_bars": cur["n_bars"], "gate_ok": cur["gate_ok"],
           "r2": (None if pd.isna(_r2) else round(float(_r2), 3)),
           "er10": (None if pd.isna(_er) else round(float(_er), 3))}
    with _LOCK:
        _CACHE[code] = {"slot": slot, "value": res}
    return res


# ── §5 做T许可映射表（30min 状态 × 日线三档）────────────────────────────
#   日线档：多={bull,uptrend} / 中={base,neutral} / 空={downtrend,weak_breakdown}
_BUCKET = {"bull": "多", "uptrend": "多", "base": "中", "neutral": "中",
           "downtrend": "空", "weak_breakdown": "空"}
# (allow_zheng_t, allow_fan_t, max_position_ratio, reason)
_PERM = {
    ("BULL", "多"): (True, False, 1.00, "趋势多×日线多：正T，禁反T防卖飞"),
    ("BULL", "中"): (True, True, 0.70, "趋势多×日线中：正T，反T仅冲高急拉减半"),
    ("BULL", "空"): (True, False, 0.30, "趋势多×日线空：原则上停手；R²>0.6 且 ER>0.35 才小仓正T"),
    ("RANGE", "多"): (True, False, 0.50, "震荡×日线多：轻仓正T，只做回踩"),
    ("RANGE", "中"): (False, False, 0.00, "震荡×日线中：停手为主"),
    ("RANGE", "空"): (False, False, 0.00, "震荡×日线空：停手"),
    ("BEAR", "多"): (False, True, 0.00, "空头×日线多：禁正T，允许反T，底仓不动"),
    ("BEAR", "中"): (False, True, 0.30, "空头×日线中：禁正T，反T活动仓≤30%"),
    ("BEAR", "空"): (False, False, 0.00, "空头×日线空：全面停手，只观察"),
}


def get_trade_permission(code: str, daily_trend_bg: str = None) -> dict:
    """§5 做T许可映射。输入 30min 状态（自动取）+ 日线 trend_bg 七态（调用方给）。
    返回 {state_30min, trend_bg_daily, bucket, allow_zheng_t, allow_fan_t,
          max_position_ratio, reason, confidence, r2, er10, source, daily_fallback}。
    30min 数据不可用时各许可为 None（调用方自行决定放行/拦截）。"""
    r = get_trend30(code)
    state = r.get("state")
    bg = str(daily_trend_bg or "").lower()
    bucket = _BUCKET.get(bg)
    daily_fallback = bucket is None
    if daily_fallback:
        bucket = "中"   # 日线态未知 → 取中性档
    base = {"state_30min": state, "trend": r.get("trend"), "trend_bg_daily": daily_trend_bg,
            "bucket": bucket, "confidence": r.get("confidence"), "r2": r.get("r2"),
            "er10": r.get("er10"), "source": r.get("source"), "daily_fallback": daily_fallback}
    if r.get("source") != "30min" or not state:
        base.update({"allow_zheng_t": None, "allow_fan_t": None,
                     "max_position_ratio": None, "reason": "30min 数据不可用"})
        return base
    az, af, ratio, reason = _PERM[(state, bucket)]
    # §5 附注：BULL×日线空 的小仓正T 需 R²>0.6 且 ER>0.35，否则停手
    if state == "BULL" and bucket == "空":
        r2, er = r.get("r2"), r.get("er10")
        ok = (r2 is not None and r2 >= 0.6) and (er is not None and er >= 0.35)
        if not ok:
            az, ratio = False, 0.00
            reason = "趋势多×日线空：R²/ER 未达标 → 停手"
    base.update({"allow_zheng_t": az, "allow_fan_t": af,
                 "max_position_ratio": ratio, "reason": reason})
    return base
