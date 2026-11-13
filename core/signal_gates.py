# -*- coding: utf-8 -*-
"""信号门控集群（2026-11-13 从 main.py 抽出）：指数5分钟共振 / 个股MA5闸 / 拦截可见性 / 信号白名单。

scan_once 只**调用**这些函数、不读它们的模块全局 ⇒ main.py 直接 `from ... import` 即可，
无重绑定共享状态。`_RESONANCE_MODULE_OK`/`_compute_resonance`/`_write_resonance_trace` 是
`_resonance_gate` 的专属全局，一并搬走。
"""
from __future__ import annotations

import sys

from config import (
    FEISHU_KEYWORD, FEISHU_WEBHOOK, INDEX_RESONANCE_PARAMS, log, send_feishu_payload,
)
from core.paths import INTERCEPT_NOTICE_PUSHED_FILE as _INTERCEPT_STATE_FILE


# ── 指数5分钟共振过滤（做T信号，2026-08-14 新增）──
# 回测（--tushare-replay）不调 _resonance_gate，跳过导入避免其依赖链在本机间歇性挂起
_RESONANCE_MODULE_OK = True
if "--tushare-replay" in sys.argv:
    _RESONANCE_MODULE_OK = False
    _compute_resonance = None
    _write_resonance_trace = None
else:
    try:
        from analysis.index_resonance import compute_resonance as _compute_resonance
        from analysis.index_resonance import write_resonance_trace as _write_resonance_trace
    except Exception as _e:
        _RESONANCE_MODULE_OK = False
        _compute_resonance = None
        _write_resonance_trace = None
        print(f"[WARN] index_resonance 加载失败（共振过滤不可用，信号按不过滤放行）: {_e}")


def _t_gate_bypass(key: str) -> bool:
    """读 T_INTRADAY_GATE_BYPASS 开关（config），供做T低吸意图分流用。"""
    try:
        from config import T_INTRADAY_GATE_BYPASS as _tgb
        return bool((_tgb or {}).get(key, False))
    except Exception:
        return False


def _resonance_gate(code, sig, now):
    """指数5分钟共振门控。返回 (gate_pass, resonance_info)。

    共振模块不可用/计算异常 → 降级放行（代码故障不阻断交易）；
    指数数据缺失/不足 → 按 INDEX_RESONANCE_PARAMS.fail_closed 决定（默认拦截）。
    每次计算都落盘 index_resonance trace（含被拦截的准信号），供复盘优化。
    C-1(2026-08-21): 做T/接回意图分流——SELL_HIGH(日内了结)跳过共振门控直接放行，
    卖侧不受指数 MA5 尺约束（08-19 破线日 0 卖出信号教训）。
    2026-08-24 方案A: 按标的启用/禁用共振门控
    2026-09-05 意图分流(owner 拍板): swing_renko 做T日内低吸(BUY, 14:55 强平了结)同样跳过共振——
    近一周活跃 T 仓低吸几乎为零的主因之一（002451 多日被 index_ma5_dir 拦）。开关见 T_INTRADAY_GATE_BYPASS。
    """
    # 2026-08-24 方案A: 检查该标的是否有分标的覆盖
    try:
        from config import INDEX_RESONANCE_STOCK_OVERRIDE as _irso
        if isinstance(_irso, dict) and code in _irso:
            _override = _irso[code]
            if _override.get("enabled") is False:
                # 该标的禁用共振门控，直接放行
                return True, {"bypass": "stock_override_disabled"}
    except Exception:
        pass

    try:
        from config import RESONANCE_GATE as _rg
    except Exception:
        _rg = {"enabled": True, "bypass_sell_high": True}
    if (_rg.get("enabled", True) and _rg.get("bypass_sell_high", True)
            and str(sig.action) == "SELL_HIGH"):
        return True, {"bypass": "sell_high"}
    # 2026-09-05 意图分流: 做T日内低吸(swing_renko BUY, 当日14:55强平了结)跳过指数共振 BUY 侧
    if (_rg.get("enabled", True) and _t_gate_bypass("resonance_for_swing_buy")
            and str(sig.action) == "BUY_LOW"
            and str((sig.factors or {}).get("entry_kind", "")) == "swing_renko"):
        return True, {"bypass": "t_swing_buy"}
    if not _RESONANCE_MODULE_OK or _compute_resonance is None:
        return True, None
    try:
        _rp = INDEX_RESONANCE_PARAMS if isinstance(globals().get("INDEX_RESONANCE_PARAMS"), dict) else {}
    except Exception:
        _rp = {}
    if not _rp.get("enabled", True):
        return True, None
    try:
        r = _compute_resonance(code, sig.action, float(sig.price or 0), boundary_ts=now)
    except Exception as e:
        log.warning(f"⚠️ 共振计算异常（降级放行）: {code} {str(e)[:80]}")
        return True, None
    try:
        _write_resonance_trace({
            "scan_time": now.strftime("%Y-%m-%d %H:%M:%S"),
            "code": code, "name": sig.name, "action": sig.action,
            "price": float(sig.price or 0), "score": float(sig.score or 0),
            **r,
        })
    except Exception:
        pass
    if r.get("missing") and not _rp.get("fail_closed", True):
        return True, r  # fail-open：数据缺失放行
    return bool(r.get("gate_pass")), r


# ==================== C-2/C-3: 个股MA5闸 + 拦截可见性（2026-08-21） ====================
def _below_ma5(code):
    """个股日线最新收盘 < MA5（盘中用最新可得收盘，通常为昨日；docstring 注明边界）。
    C-2(2026-08-21): 用户规则"破五日线只卖不买"系统化。数据缺失返回 None（不拦截）。"""
    try:
        from core.position_builder import fetch_daily_kline
        df = fetch_daily_kline(code)
        if df is None or df.empty or "close" not in df.columns or len(df) < 6:
            return None
        df = df.sort_values("date").reset_index(drop=True)
        c = df["close"].astype(float)
        return bool(float(c.iloc[-1]) < float(c.rolling(5).mean().iloc[-1]))
    except Exception:
        return None


def _push_intercept_notice(code, sig, now, reason):
    """C-3(2026-08-21)+F-11(2026-09-04): 拦截可见性——被 C-1/C-2 拦截且原 score>=推送阈值，推低优飞书（非加急）。
    去重由"每股每向每日 1 条"布尔改为 20min 滚动冷却窗（core/intercept_notice.py）：同码同日复拦仍发
    （09-04 10:57 002451 漏发案，Q-20260904-5）；send 失败不写时间戳（B3 语义，重启允许补报）。"""
    try:
        if not FEISHU_WEBHOOK:
            return
        from core import intercept_notice as _intn
        _today = now.strftime("%Y-%m-%d")
        _key = f"{code}:{sig.action}"
        _st = _intn.read_state(_INTERCEPT_STATE_FILE)
        if not _intn.send_allowed(_st, _today, _key, now):
            return
        _acn = {"BUY_LOW": "买入", "SELL_HIGH": "卖出", "ADD_POS": "加仓",
                "PANIC_SELL": "恐慌卖"}.get(sig.action, sig.action)
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": "blue",
                           "title": {"tag": "plain_text", "content": f"🔕 信号已拦截 - {FEISHU_KEYWORD}"}},
                "elements": [{"tag": "markdown", "content":
                    f"🔕 **{sig.name}（{code}）** {_acn} score={sig.score:.0f} 已拦截（{reason}）"}],
            },
        }
        if send_feishu_payload(card, success_log=f"拦截可见性飞书: {code} {sig.action}",
                               error_prefix="拦截可见性飞书"):
            _intn.mark_sent(_st, _today, _key, now)
            _intn.write_state(_INTERCEPT_STATE_FILE, _st)
    except Exception:
        pass


def _signal_whitelist(code, holding):
    """C-5(2026-08-21): 信号评估白名单（C15+C25+C27 合并）。
      qty==0 已清仓/零持仓 → 跳过全部评估与推送（C27：603667 清仓仍吃加急卖信号教训）；
      t_qty==0 且 qty>0 纯底仓股 → 保留评估（日报展示）但跳过买/卖推送（C25：002639 一天 6 条买信号噪音）；
      返回 {eval_buy, eval_sell, push}，集中一处便于以后调。"""
    qty = int(holding.get("qty") or 0)
    t_qty = int(holding.get("t_qty") or 0)
    if qty <= 0:
        return {"eval_buy": False, "eval_sell": False, "push": False}
    if t_qty <= 0:
        return {"eval_buy": True, "eval_sell": True, "push": False}
    return {"eval_buy": True, "eval_sell": True, "push": True}
