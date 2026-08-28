# -*- coding: utf-8 -*-
"""build_decision_auto.py — auto 侧建仓判定适配器（P4-6）

把 gm_main BASE 建仓块的数据（个股日线 + 指数日线 + bar_cache 1m）适配进
core/build_decision.py 决策核（P3 双侧单一真源）。纯函数、无 gm.api，供：
  · execution/auto/gm_main.py BASE 放行判据（WP-B20 降为参考留痕）
  · scripts/build_verdict_parity.py 的 auto_chain（真实 auto 代码路径）

数据不足 fail-closed（features<61 行 → go=False verdict=weak data_insufficient=True）。
盘中（df_1min 提供）：GO 后跑 W35 日内确认，未过 → verdict=approaching(待日内确认)不建仓；
EOD 口径（df_1min=None）：跳过 W35（与 manual 侧 EOD 同）。
"""
import os
import sys

_ST = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ST, os.path.dirname(os.path.abspath(__file__)),
           os.path.join(os.path.dirname(os.path.abspath(__file__)), "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import build_decision as bd  # noqa: E402


def decide(stock_daily_df, index_daily_df, date_str, params=None, df_1min=None) -> dict:
    """auto 侧建仓判定。返回 {go, veto, verdict, regime, score, reasons, data_insufficient, intraday_confirm}。

    params 用 superTrader config.ENTRY_TIMING_PARAMS（决策核内嵌 DEFAULT_TIMING_PARAMS 兜底）。
    df_1min 为当日 1 分钟线 DataFrame（time/open/high/low/close/volume/amount）；盘中提供，
    EOD/回测不提供（跳过 W35 日内确认）。
    """
    p = params or bd.DEFAULT_TIMING_PARAMS
    r = bd.regime_from_index_daily(index_daily_df, date_str, p)
    regime = r.get("regime", "unknown")
    f = bd.features_from_daily(stock_daily_df, date_str)
    if not f:
        return {"go": False, "veto": [], "verdict": "weak", "regime": regime,
                "score": 0, "reasons": ["数据不足(日线<61行) fail-closed"],
                "data_insufficient": True, "intraday_confirm": None}
    dec = bd.timing_decision(f, regime, p)
    verdict, score = bd.verdict_from_timing(dec["go"], regime, f, False)
    ic = None
    if dec["go"] and df_1min is not None:
        vol_min = float(p.get("intraday_confirm_vol_min", 1.2))
        try:
            _pass, _detail, _insuff = bd.intraday_confirm(df_1min, vol_min=vol_min)
            ic = {"passed": _pass, "detail": _detail, "insufficient": _insuff}
            if _insuff:
                # W35 数据不足：不惩罚，维持 signal（与 manual scan_stock 1109-1111 同口径）
                pass
            elif not _pass:
                # GO 但日内确认未过 → 降级 approaching（待日内确认），不建仓
                verdict = "approaching"
        except Exception as _e:
            ic = {"passed": False, "detail": f"日内确认异常: {_e}", "insufficient": True}
            # 异常视为数据不足，不惩罚（维持 signal）
            pass
    return {"go": dec["go"], "veto": dec["veto"], "verdict": verdict, "regime": regime,
            "score": score, "features": f, "reasons": dec["reasons"],
            "data_insufficient": False, "intraday_confirm": ic}
