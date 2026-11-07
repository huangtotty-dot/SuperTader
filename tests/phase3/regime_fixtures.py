# -*- coding: utf-8 -*-
"""分板买闸测试夹具：构造字段完整、可校验的同轮 regime 上下文。"""
import datetime as dt
import hashlib


def previous_weekday(day):
    d = dt.date.fromisoformat(str(day)[:10]) - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d.isoformat()


def valid_regime_ctx(requested_date, index_symbol, regime, mode="morning",
                     effective_date=None, input_last_date=None, score=0.0):
    requested = str(requested_date)[:10]
    effective = effective_date or (
        previous_weekday(requested) if mode in ("morning", "live") else requested)
    input_last = input_last_date or (requested if mode in ("morning", "live") else effective)
    payload = "%s|%s|%s|%s" % (requested, index_symbol, input_last, regime)
    return {
        "requested_date": requested,
        "input_last_date": input_last,
        "effective_date": effective,
        "expected_effective_date": effective,
        "forming_bar_removed": bool(mode in ("morning", "live") and input_last == requested),
        "input_version": "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "input_rows": 120,
        "effective_rows": 119 if mode in ("morning", "live") and input_last == requested else 120,
        "data_source": "gm_api",
        "mode": mode,
        "index_symbol": index_symbol,
        "date": effective,
        "regime": regime,
        "score": score,
        "degraded": [],
        "gate_advice": "normal_t",
    }


def valid_board_regime_ctx(requested_date, board_regime, mode="morning"):
    return {
        symbol: valid_regime_ctx(requested_date, symbol, regime, mode=mode)
        for symbol, regime in (board_regime or {}).items()
        if regime in ("range", "uni_up", "uni_down")
    }
