# coding=utf-8
"""
gm_main.py — 掘金量化策略入口（P4-1 迁入自 goldminer main.py，v1.1.0 WIP）
execution/auto/gm_main.py 是唯一 import gm.api 的文件；卖出通道/状态在 sell_channels.py/sell_state.py。
"""

from __future__ import print_function, absolute_import, division
from gm.api import *
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, time as dtime
import os
import sys
import json
import copy

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# P4-1: 支撑模块整体副本在 _gm/（goldminer 内部 import 相对路径不变，仅指向 _gm）；
# 本目录（sell_state/sell_channels）同样入 path，保证 gm SDK 脚本模式与包导入两种方式都可 import。
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
_GM_DIR = os.path.join(PROJECT_DIR, "_gm")
if _GM_DIR not in sys.path:
    sys.path.insert(0, _GM_DIR)

from config.params import PARAMS, STOCK_PARAMS
from data.indicators import add_indicators, clean_code
from t_engine_auto import SignalEngine
from signals.position_sizer import PositionSizer
from utils.helpers import SIM_NOW, _now, get_today_str, _default_daily_context
from gm_bridge.writer import (
    write_signal, write_order, write_fill, write_reject, write_risk,
    write_heartbeat, check_kill_switch, write_snapshot, write_buyback,
)
from gm_bridge import ops_guard

# ── 标的池（P3-2 池分管：auto 侧候选池单一真源 = superTrader config/auto_pool.py）──
# 原 hardcode 17 票迁出；消费方式与 utils/gm_token.py 读取 superTrader 配置同源（SUPERTRADER_ROOT）。
# 用绝对路径 importlib 加载：goldminer 自身也有 config 包，`from config.auto_pool` 会命中本仓 config。
def _load_auto_pool():
    import importlib.util as _ilu
    root = os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader")
    path = os.path.join(root, "config", "auto_pool.py")
    if not os.path.exists(path):
        raise RuntimeError(f"auto 池配置缺失（P3-2 池分管依赖）: {path}")
    _spec = _ilu.spec_from_file_location("auto_pool", path)
    _m = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    return _m


_auto_pool = _load_auto_pool()
STOCKS = {code: v["gm_symbol"] for code, v in _auto_pool.AUTO_POOL.items()}
STOCK_NAMES = {code: v["name"] for code, v in _auto_pool.AUTO_POOL.items()}
REVERSE_MAP = {v: k for k, v in STOCKS.items()}

# ── 镜像持仓（与实盘账户一致） ──
# 模拟盘建仓时按此表中的股数/成本下单
MIRROR_HOLDINGS = {
    # 2026-07-28 owner决策(N2): 事故超配减仓后新基线
    # 2026-07-29 F7返工: MIRROR语义=目标底仓。000988维持500, 缺口200由_base_topup_qty择时回补
    # 2026-08-07 owner决策(WP-E3槽位制配套): MIRROR缩编至4支优先票（华工/巨石/五洲/双良）。
    # 其余12票保留在STOCKS候选池，槽位空出时凭信号竞争建仓，不再预挂目标底仓（消除slot_full排队噪音）。
    "000988": {"qty": 500,  "cost": 0},
    "600481": {"qty": 1400, "cost": 0},
    "600176": {"qty": 500,  "cost": 0},
    "603667": {"qty": 800,  "cost": 0},
    # WP-E4(2026-08-24 owner决策): 红利ETF 防守仓纳入做T体系观察做T效率。
    # 境内股票型ETF，T+1、最小单位100股，与股票机制一致（无588170的T+0兼容问题）。
    "515180": {"qty": 50000, "cost": 1.451},
    # 588170 ETF 已移除：T+0机制/最小单位与策略不兼容，首日仅观察
}

COMMISSION = PARAMS["commission_rate"]
MIN_BARS = 25
T1_AUTO_UNLOCK_HOUR = 9
T1_AUTO_UNLOCK_MINUTE = 31
# 镜像持仓总市值约 123,000（500×100.6 + 1400×3.9 + 500×37.6 + 800×50.9，按2026-07-28收盘）
# 按市值×1.5 配置模拟盘资金（留足做T现金水位）
INITIAL_CASH = 150000
MAX_BASE_RETRY = 3


# ═══════════════════════════════════════════
# T4 卖出通道仲裁器
# ═══════════════════════════════════════════
# 优先级: P1 PANIC > P2 TRAIL > P3 TREND_EXIT > P4 TARGET > P5 SELL_HIGH > P6 TAIL
# 当前: P1/P2/P5/P6 四通道，P3/P4 为 Phase B 预留
#   - P1/P2 豁免 sizer 分批与日卖出计数（止血/保护不受节流）
#   - P5/P6 占用计数
def _raw_code(symbol: str) -> str:
    return symbol.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", "")


def _apply_buyback_downgrade(context, code, sig, qty):
    """WP-B07: 高接降档 — 回补价落入 (前卖价×(1+downgrade_pct), 前卖价×(1+delay_pct)]
    区间时，sizer 结果减半后向下取整到 min_unit 的整数倍。

    返回 (qty, dg_info, min_unit)；dg_info 非 None 表示命中降档。
    qty < min_unit 时调用方应延迟（等同不产生买入）。"""
    dg = None
    for d in (getattr(sig, "details", None) or []):
        if isinstance(d, dict) and d.get("buyback_downgrade"):
            dg = d
            break
    try:
        min_unit = int(context.sizer._effective_params(code).get("stock_min_trade_unit", 100))
    except Exception:
        min_unit = 100
    if dg is None:
        return int(qty), None, min_unit
    qty2 = (int(qty) // 2 // min_unit) * min_unit
    return qty2, dg, min_unit


def _total_equity(context, available_cash: float) -> float:
    """WP-E2: 总权益 = 可用现金 + Σ(全部持仓市值)。

    持仓复用 manual_position（_get_holding 的第一优先数据源，含即时缓存与对账结果），
    定价取 bar_cache 最新收盘价；某票 qty>0 但无 bar 价格数据时退化为成本价估值
    （mark-to-cost——开盘价前/数据缺失场景不用 0 低估、也不 fail-closed 误杀全天）。"""
    total = float(available_cash or 0)
    for sym, mp in (getattr(context, "manual_position", None) or {}).items():
        try:
            q = int(mp.get("qty", 0) or 0)
        except Exception:
            continue
        if q <= 0:
            continue
        px = 0.0
        rows = (getattr(context, "bar_cache", None) or {}).get(sym)
        if rows:
            try:
                px = float(rows[-1].get("close", 0) or 0)
            except Exception:
                px = 0.0
        if px <= 0:
            px = float(mp.get("cost", 0) or 0)  # 退化：成本价估值
        total += q * px
    return total


def _stock_budget_cap(context, code, cp: float, total_eq: float):
    """WP-E2/E3: 个股预算与最大仓位。

    stock_budget = total_equity × (1 − cash_reserve_pct) / max_concurrent_positions
    （WP-E3 槽位制：同时持仓不超 4 支，预算按 4 槽分解；TODO(PhaseD) 趋势加权语义保留）
    max_pos_shares = floor(stock_budget / cp / 100) × 100
    返回 (stock_budget, max_pos_shares)。"""
    reserve = float(PARAMS.get("cash_reserve_pct", 0.20))
    n = max(int(PARAMS.get("max_concurrent_positions", 4)), 1)
    budget = float(total_eq or 0) * (1 - reserve) / n
    mps = int(budget / cp / 100) * 100 if cp > 0 else 0
    return budget, mps


def _held_codes(context):
    """WP-E3: 当前持仓(qty>0)代码列表——槽位占用。
    数据源与 _total_equity 一致（context.manual_position）。"""
    codes = []
    for sym, mp in (getattr(context, "manual_position", None) or {}).items():
        try:
            if int(mp.get("qty", 0) or 0) > 0:
                codes.append(_raw_code(sym))
        except Exception:
            continue
    return codes


def _held_position_count(context) -> int:
    """WP-E3: 当前占用槽位数（qty>0 的票数）。"""
    return len(_held_codes(context))


def _slot_full(context) -> bool:
    """WP-E3: 槽位是否已满（≥ max_concurrent_positions）。"""
    return _held_position_count(context) >= int(PARAMS.get("max_concurrent_positions", 4))


def _emit_slot_full(context, code, now, where: str) -> bool:
    """WP-E3: slot_full 事件（每票每日去重，O-03 风格）。True=首次已写事件。

    where="buy"  → risk kind=slot_full（on_bar 全新建仓信号被挡）；
    where="base" → risk kind=base_deferred、detail 含 reason=slot_full
                   （底仓建仓块复用既有延迟机制，下一根 bar 自然重试）。
    两处统一写 audit event=slot_full（where 字段区分）。"""
    _key = f'_slot_full_{where}_{code}'
    _today = now.strftime("%Y-%m-%d")
    if getattr(context, _key, '') == _today:
        return False
    setattr(context, _key, _today)
    held = _held_codes(context)
    mx = int(PARAMS.get("max_concurrent_positions", 4))
    _base = f"held_count={len(held)}/{mx} held_codes={','.join(held)} candidate={code}"
    try:
        if where == "base":
            write_risk(str(now), "base_deferred", f"reason=slot_full {_base}", code=code)
        else:
            write_risk(str(now), "slot_full", _base, code=code)
    except Exception:
        pass
    _audit_write({"event": "slot_full", "code": code, "where": where,
                  "held_count": len(held), "max_slots": mx,
                  "held_codes": held, "time": str(now)})
    print(f"[{now:%H:%M:%S}] {where.upper()} {code} 槽位满({len(held)}/{mx})→等待 held={held}")
    return True


def _clear_signal_mute_keys(context, code: str):
    """WP-B15: 持仓变化（成交/对账/拒单回滚）→ 解除信号 mute 与地板去重键。
    键含日期串，置空即可——同状态重新被拦会再次置位，信息不丢。"""
    try:
        _keys = [k for k in context.__dict__
                 if k.startswith(f'_sig_muted_{code}_') or k.startswith(f'_floor_logged_{code}_')]
        for _k in _keys:
            context.__dict__[_k] = ''
    except Exception:
        pass


def _buyback_mutex_block(context, code, daily_ctx, open_price, cp, now, ab):
    """WP-B18 3.2: 回补触发互斥矩阵 M1-M4。
    返回 (action, rule)：action ∈ pass / block(永久作废记忆) / delay(保留记忆但本 bar 不触发)。
    M2 不拦截——TRAIL ARMED 为不同仓位腿（不互斥）；TRAIL_SELL 已触发(COOLED)由新卖价 arm 覆盖。"""
    rule = ""
    # M1: 日线 TREND_DOWN + 卖出通道 TREND_EXIT → 趋势破坏不机械接回（0818 600481 案例）
    trend = (daily_ctx or {}).get("_stock_trend_state", "TREND_RANGE")
    if trend == "TREND_DOWN" and ab.get("sell_action") == "TREND_EXIT":
        return "block", "M1"
    # M3: 开盘跳空 >2% → 09:35 开盘缓冲后评估（复用 B-13 闸语义）
    _target = float(ab.get("target_price", 0) or 0)
    if _target > 0 and open_price > 0 and now.hour == 9 and now.minute <= 35:
        if abs(open_price - _target) / _target > 0.02:
            return "delay", "M3"
    # M4: 深亏背景(< -8% PANIC 域)不回补（回补语义是"接回高抛"，不是"摊平亏损"）
    try:
        _cost = float(((getattr(context, "manual_position", None) or {})
                       .get(STOCKS.get(code, ""), {}) or {}).get("cost", 0) or 0)
    except Exception:
        _cost = 0.0
    if _cost > 0 and cp > 0 and (cp - _cost) / _cost < -0.08:
        return "block", "M4"
    return "pass", ""


def _check_max_pos_cap(context, code, now, pos_qty: int, base_ref: int,
                       max_pos_shares: int, budget: float, total_eq: float,
                       force: bool = False, action: str = "", t_headroom: int = 0) -> bool:
    """WP-E2: 个股最大仓位闸。True=拦截（调用方 continue）。

    触发条件：pos_qty>0 且 pos_qty >= max(max_pos_shares, base_ref + t_headroom)
    （预算帽与底仓取高——永不在底仓下方收口，不逼卖出，与 target_t 语义一致）。
    t_headroom（2026-08-31）：做T买入(BUY_LOW/ADD_POS)专用的一档T余量，
    否则持仓=底仓即恒到顶、做T加仓永远被拦（run8 实证）。
    force=True 用于 sizer 返回 0 的确认分支（reason=sizer_zero_at_cap）。
    拦截时写 risk 事件 max_pos_cap + audit，每票每日去重（O-03 风格）。"""
    ceiling = max(int(max_pos_shares or 0), int(base_ref or 0) + int(t_headroom or 0))
    if pos_qty <= 0 or (not force and pos_qty < ceiling):
        return False
    # WP-B15: 到顶拦截 → 每次拦截都置位同源信号 mute（事件层去重；决策/执行不受影响）。
    # 置于去重块之外——成交清键后再到顶时，去重块不执行但 mute 必须重新生效（防复发刷屏）
    _today = now.strftime("%Y-%m-%d")
    if action:
        setattr(context, f'_sig_muted_{code}_{action}', _today)
    _key = f'_max_pos_cap_{code}'
    if getattr(context, _key, '') != _today:
        setattr(context, _key, _today)
        _weight = (budget / total_eq) if total_eq > 0 else 0
        _reason = "sizer_zero_at_cap" if force else "pos_at_cap"
        _detail = (f"budget={budget:.0f} equity={total_eq:.0f} weight={_weight:.1%} "
                   f"max_pos_shares={max_pos_shares} base_ref={base_ref} pos_qty={pos_qty} "
                   f"reason={_reason}")
        try:
            write_risk(str(now), "max_pos_cap", _detail, code=code)
        except Exception:
            pass
        _audit_write({"event": "max_pos_cap", "code": code, "budget": round(budget, 2),
                      "equity": round(total_eq, 2), "weight": round(_weight, 4),
                      "max_pos_shares": max_pos_shares, "base_ref": base_ref,
                      "pos_qty": pos_qty, "reason": _reason, "time": str(now)})
        print(f"[{now:%H:%M:%S}] BUY {code} 个股仓位到顶拦截: {_detail}")
    return True


def _dedup_bar(context, gm_sym: str, eob: str) -> bool:
    """F9: 同 eob 重复 bar 判定（True=重复应跳过）。

    2026-07-31 模拟盘同秒 4 次重复投递 000988 bar，导致 PANIC 连发 4 单；
    同时防止 bar_cache 重复累积。"""
    _eob_map = getattr(context, "_last_bar_eob", None)
    if _eob_map is None:
        _eob_map = {}
        context._last_bar_eob = _eob_map
    if _eob_map.get(gm_sym) == eob:
        return True
    _eob_map[gm_sym] = eob
    return False


def _maybe_clear_audit_log(context):
    """D8/F10: 仅回测模式清空审计文件；模拟盘(MODE_LIVE)保留追加。

    2026-07-31 上午段 backtrace 被 13:09 重启清空——回测设计误伤模拟盘审计。"""
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    if _is_live:
        return False
    try:
        _audit_close()  # 先释放句柄，否则 Windows 下 remove 失败
        if os.path.exists(_AUDIT_LOG_PATH):
            os.remove(_AUDIT_LOG_PATH)
            return True
    except Exception:
        pass
    return False


def _build_bar_df(context, code: str, gm_symbol: str) -> pd.DataFrame:
    rows = context.bar_cache.get(gm_symbol, [])
    if len(rows) < MIN_BARS:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "amount"])
    df = df.sort_values("time").reset_index(drop=True)
    df = add_indicators(df)
    return df



def _get_holding(context, code: str, gm_symbol: str) -> dict:
    """多源持仓读取，按优先级：

    1. context.manual_position（即时缓存）
    2. context.account().positions()（优先，每30分钟对账）
    3. context.executed_orders（回退）
    """
    default = {"name": STOCK_NAMES.get(code, code), "qty": 0,
               "available": 0, "t_qty": 0, "cost": 0,
               "type": "stock", "pre_close": 0}
    now = _now()

    # 1. 每30分钟跟 gm.api positions 对账一次
    reconcile_interval = 1800
    last_rec = getattr(context, "_last_position_reconcile", None)
    # F2: 模拟盘模式启用对账，回测模式跳过
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    _skip_reconcile = not _is_live
    if not _skip_reconcile and (last_rec is None or (now - last_rec).total_seconds() > reconcile_interval):
        context._last_position_reconcile = now
        try:
            pos = context.account().positions(symbol=gm_symbol, side=PositionSide_Long)
            if pos and len(pos) > 0:
                p = pos[0]
                gm_pos = {
                    "name": STOCK_NAMES.get(code, code),
                    "qty": int(p.volume),
                    "available": int(p.available),
                    "t_qty": int(p.volume),
                    "cost": float(p.vwap or 0),
                    "type": "stock",
                    "pre_close": float(p.vwap or context.latest_pre_close.get(code, 0)),
                }
                mp = context.manual_position.get(gm_symbol)
                if mp and abs(int(mp.get("qty", 0)) - int(p.volume)) > 0:
                    _my_cost = mp.get("cost", gm_pos["cost"])
                    context.manual_position[gm_symbol] = gm_pos
                    context.manual_position[gm_symbol]["cost"] = _my_cost
                    # WP-B15: 对账持仓变化 → 解除信号 mute / 地板去重键
                    _clear_signal_mute_keys(context, code)
                    _audit_write({"event": "reconcile_fix", "code": code, "time": str(now),
                                  "old_qty": mp.get("qty"), "new_qty": gm_pos["qty"]})
                # ①-1: manual_position cost=0时用gm vwap修正(市价单price=0兜底)
                if mp and float(mp.get("cost", 0) or 0) <= 0 and float(gm_pos.get("cost", 0) or 0) > 0:
                    mp["cost"] = gm_pos["cost"]
                    _audit_write({"event": "cost_fix", "code": code, "time": str(now),
                                  "cost": gm_pos["cost"]})
                # qty 一致时返回 manual_position（我们跟踪的成本），不返回 gm_pos
                # gm_pos 的 vwap 可能含前复权调整，与真实买入成本不一致
                if mp and int(mp.get("qty", 0) or 0) > 0:
                    return mp
            else:
                # F11: 终端已无持仓而台账仍有余量 → 向下对账归零
                # (2026-07-31 PANIC清仓000988后 心跳仍报300股：空仓查询返回空列表
                # 走不到向上对账分支，台账残影永远不自愈)
                mp = context.manual_position.get(gm_symbol)
                if mp and int(mp.get("qty", 0) or 0) > 0:
                    _old_q = int(mp.get("qty", 0))
                    mp["qty"] = 0
                    mp["available"] = 0
                    mp["t_qty"] = 0
                    # WP-B15: 对账持仓归零 → 解除信号 mute / 地板去重键
                    _clear_signal_mute_keys(context, code)
                    _audit_write({"event": "reconcile_fix", "code": code, "time": str(now),
                                  "old_qty": _old_q, "new_qty": 0})
        except Exception:
            pass

    # 2. manual_position
    mp = context.manual_position.get(gm_symbol)
    if mp and int(mp.get("qty", 0) or 0) > 0:
        return mp

    # 3. executed_orders
    if gm_symbol in context.executed_orders:
        return context.executed_orders[gm_symbol]
    return default


# ── 交易日线上下文构建 ──

def _refresh_daily_ctx(context, code: str, gm_symbol: str, now: datetime) -> dict:
    """用 history_n 拉标的日线，计算 daily_ctx 供信号引擎用。

    每日 09:31 触发一次（或首次调用时），结果缓存到 context.daily_ctx_cache。
    P3-1(B): 废 T-1 冻结——实时拉取（end_time=now）。量能口径：gm 盘中不返回当日
    daily bar → 序列末根=上一根「已完成」bar（当前 bar 未完成时用上一根已完成 bar）；
    收盘结算后若含当日 bar 则为当日完整量。若 P4 迁公共 provider（含 forming bar），
    需在此补「剔除未完成末根」守卫。
    """
    today_str = now.strftime("%Y-%m-%d")
    _cache_key = f"{today_str}|{code}"
    if not hasattr(context, "_daily_ctx_cache_map"):
        context._daily_ctx_cache_map = {}
    if _cache_key in context._daily_ctx_cache_map:
        return context._daily_ctx_cache_map[_cache_key]

    # 取 200 个交易日日线（P3-1(A): ≥150 供箱体 _daily_ohlc tail(150)）
    _exc_info = None
    try:
        daily = history_n(symbol=gm_symbol, frequency="1d", count=200,
                          fields="eob,open,high,low,close,volume",
                          fill_missing="Previous", adjust=ADJUST_PREV,
                          end_time=now.strftime("%Y-%m-%d %H:%M:%S"))
    except Exception as _e:
        daily = None
        # O-01(2026-08-07 W32表决): 异常不再裸吞——留痕在下方失败分支统一打印，
        # 每票每日一条（O-04 修正：原双分支各打一次，同一失败出两行且"无异常"字样误导）
        _exc_info = f"{type(_e).__name__}: {str(_e)[:200]}"

    ctx = dict(_default_daily_context(code))

    if daily is not None and len(daily) >= 10:
        df = pd.DataFrame(daily)
        # P4-6: 供 core/build_decision 决策核消费的日线 DataFrame（date/open/high/low/close/volume）
        try:
            _ddf = df.copy()
            if "eob" in _ddf.columns:
                _ddf["date"] = pd.to_datetime(_ddf["eob"]).dt.strftime("%Y-%m-%d")
            _ddf["open"] = pd.to_numeric(_ddf.get("open"), errors="coerce")
            _ddf["high"] = pd.to_numeric(_ddf.get("high"), errors="coerce")
            _ddf["low"] = pd.to_numeric(_ddf.get("low"), errors="coerce")
            _ddf["close"] = pd.to_numeric(_ddf.get("close"), errors="coerce")
            _ddf["volume"] = pd.to_numeric(_ddf.get("volume"), errors="coerce")
            ctx["_daily_df"] = _ddf[["date", "open", "high", "low", "close", "volume"]].dropna(subset=["date"])
        except Exception:
            ctx["_daily_df"] = pd.DataFrame()
        c = df["close"].astype(float)
        h = df["high"].astype(float)
        l = df["low"].astype(float)
        ctx["daily_ma5"] = float(c.rolling(5).mean().iloc[-1]) if len(c) >= 5 else 0
        ctx["daily_ma10"] = float(c.rolling(10).mean().iloc[-1]) if len(c) >= 10 else 0
        ctx["daily_ma20"] = float(c.rolling(20).mean().iloc[-1]) if len(c) >= 20 else 0
        # N4: 日线 ATR（用于 PANIC_SELL）
        if len(c) >= 14:
            tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
            ctx["daily_atr"] = float(tr.rolling(14).mean().iloc[-1] / c.iloc[-1]) if c.iloc[-1] > 0 else 0.02
        else:
            ctx["daily_atr"] = 0.02
        # M2: 做T门槛指标（供标的池准入检查）
        if len(c) >= 20 and "volume" in df.columns:
            v = df["volume"].astype(float)
            ctx["_m2_amp20"] = float((tr.rolling(20).mean().iloc[-1] / c.iloc[-1])) if c.iloc[-1] > 0 else 0
            ctx["_m2_amount20"] = float((v * c).rolling(20).mean().iloc[-1]) if len(v) >= 20 else 0
            ctx["_m2_lot_value"] = float(c.iloc[-1] * 100)
            # 门槛判定（AMP/AMT/单手价值低于阈值 → 标记仅观察）
            # WP-E4(2026-08-24 owner决策): 阈值支持 STOCK_PARAMS 个股覆盖
            # （515180红利ETF 低波动定制：amp20≈0.8%、单手≈145元，硬编码门槛会永久拦截），
            # 未配置个股参数时保持原硬编码缺省值（0.03 / 2亿 / 2000）。
            _m2_sp = STOCK_PARAMS.get(code, {})
            _m2_amp_min = float(_m2_sp.get("m2_amp20_min", 0.03))
            _m2_amt_min = float(_m2_sp.get("m2_amount20_min", 200000000))
            _m2_lot_min = float(_m2_sp.get("m2_lot_value_min", 2000))
            _pass = (ctx["_m2_amp20"] >= _m2_amp_min and ctx["_m2_amount20"] >= _m2_amt_min
                     and ctx["_m2_lot_value"] >= _m2_lot_min)  # TODO(PhaseD): 寻优定值
            ctx["_m2_pool_pass"] = _pass
            if not _pass:
                ctx["daily_status"] = "pool_gate_fail"
        # G4: 支撑建仓闸指标（2026-08-05 owner决策：RSI/MACD/BOLL/缩量/MA60）
        if len(c) >= 26:
            _d = c.diff()
            _up = _d.clip(lower=0).rolling(14).mean()
            _dn = (-_d.clip(upper=0)).rolling(14).mean()
            _rs = _up / _dn.replace(0, 1e-10)
            ctx["daily_rsi14"] = float((100 - 100 / (1 + _rs)).iloc[-1])
            _ema12 = c.ewm(span=12, adjust=False).mean()
            _ema26 = c.ewm(span=26, adjust=False).mean()
            _dif = _ema12 - _ema26
            _dea = _dif.ewm(span=9, adjust=False).mean()
            ctx["daily_macd_dif"] = float(_dif.iloc[-1])
            ctx["daily_macd_dea"] = float(_dea.iloc[-1])
            ctx["daily_macd_bull"] = bool(_dif.iloc[-1] >= _dea.iloc[-1])
            # WP-B20: 双通道建仓闸字段——近5日 MACD 金叉（冰点通道·转向确认用）
            _difs = pd.Series(_dif)
            _deas = pd.Series(_dea)
            _cross_up = (_difs > _deas) & (_difs.shift(1) <= _deas.shift(1))
            ctx["daily_macd_golden"] = bool(_cross_up.tail(5).any())
            _mid = c.rolling(20).mean()
            _std = c.rolling(20).std()
            ctx["daily_boll_mid"] = float(_mid.iloc[-1])
            ctx["daily_boll_upper"] = float((_mid + 2 * _std).iloc[-1])
            ctx["daily_boll_lower"] = float((_mid - 2 * _std).iloc[-1])
            # WP-B20: BOLL 百分比位置（冰点通道·bb_pct≤0.15 用）
            _bup = (_mid + 2 * _std).iloc[-1]
            _bdn = (_mid - 2 * _std).iloc[-1]
            ctx["daily_boll_pct"] = float((c.iloc[-1] - _bdn) / (_bup - _bdn)) if (_bup - _bdn) > 0 else None
        if len(c) >= 60:
            ctx["daily_ma60"] = float(c.rolling(60).mean().iloc[-1])
        if len(c) >= 20 and "volume" in df.columns:
            _v = df["volume"].astype(float)
            ctx["_vol3"] = float(_v.iloc[-3:].mean())
            ctx["_vol20"] = float(_v.iloc[-20:].mean())
            # WP-B20: 双通道建仓闸字段——当日量/5日均量（冰点缩量 & 突破放量用）
            ctx["daily_vol_today"] = float(_v.iloc[-1])
            ctx["daily_vol_ma5"] = float(_v.iloc[-5:].mean())
        prev_close = float(c.iloc[-1]) if len(c) > 0 else 0
        ctx["daily_prev_close"] = prev_close
        # WP-B20: 日线收盘参考价（冰点·转向确认站上MA5 用；实时拉取 → 上一已完成 bar 收盘）
        ctx["daily_price_ref"] = prev_close
        # WP-B20: 近150日 OHLC 序列（P3-1(A): 双通道·箱体突破检测用，与 superTrader 150日/30窗同参）
        ctx["_daily_ohlc"] = {
            "high": [float(x) for x in h.tail(150).tolist()],
            "low": [float(x) for x in l.tail(150).tolist()],
            "close": [float(x) for x in c.tail(150).tolist()],
        }
        # F7-2(2026-08-10 复盘①)：setdefault 对默认 "unavailable" 无效——
        # _default_daily_context 自带 daily_status="unavailable"，setdefault 永不覆盖，
        # 致 G4 对所有 M2 通过票恒报"日线数据不足→保守拦截"（五要素上线 3 日零运行的
        # 真根因；0806/0807 归因"取数失败"系误判），且引擎 daily_buy_t_ok 恒 False。
        # 仅 pool_gate_fail 需保留可观测，其余成功路径必须置 ok（F7 验收：正常票仍为 ok）。
        if ctx.get("daily_status") == "unavailable":
            ctx["daily_status"] = "ok"  # F7: 不覆盖 pool_gate_fail
        ctx["daily_buy_t_ok"] = True

        # 破位/过热简化判断 + R1 个股趋势状态
        if len(c) >= 20:
            ma20 = c.rolling(20).mean().iloc[-1]
            if prev_close < ma20 * 0.985:
                ctx["daily_breakdown_risk"] = True
            if prev_close > ma20 * 1.08:
                ctx["daily_overheated"] = True
            ctx["daily_ma5_state"] = "above_ma5_trend" if prev_close > ctx["daily_ma5"] else "near_ma5_chop"
            # R1: 个股趋势状态（供趋势熔断 G3 消费）
            _ma5_val = ctx["daily_ma5"]
            _ma5_slope = float(c.rolling(5).mean().diff().iloc[-1]) if len(c) >= 6 else 0
            if ctx.get("daily_breakdown_risk"):
                ctx["_stock_trend_state"] = "TREND_BREAKDOWN"
            elif prev_close < _ma5_val and _ma5_slope < 0:
                ctx["_stock_trend_state"] = "TREND_DOWN"
            elif prev_close > _ma5_val and _ma5_slope > 0:
                ctx["_stock_trend_state"] = "TREND_UP"
            else:
                ctx["_stock_trend_state"] = "TREND_RANGE"

        # 将 latest_pre_close 暴露给 _get_holding
        context.latest_pre_close[code] = prev_close
        # O-01: 取数成功则清零连续失败计数
        if getattr(context, "_daily_fail_cnt", None):
            context._daily_fail_cnt[code] = 0
    else:
        context.latest_pre_close[code] = 0
        ctx["daily_status"] = "unavailable"
        # G4-FIX(2026-08-06, 复盘0806-①): 取数失败不写日缓存、次 bar 重试——
        # 瞬时失败不再锁死全天（0806 实战:7 只新票 09:31 取数抽风被整日关在 G4 门外）。
        # 口径不变：成功后的数据仍冻结于昨收，仅失败分支允许重试。
        # O-01(2026-08-07 W32表决): 无异常但数据为空/不足也要留痕；连续失败升级 risk 事件告警
        _fc = getattr(context, "_daily_fail_cnt", None) or {}
        _fc[code] = _fc.get(code, 0) + 1
        context._daily_fail_cnt = _fc
        _dn_key = f'_daily_fetch_none_{code}'
        if getattr(context, _dn_key, '') != today_str:
            setattr(context, _dn_key, today_str)
            if _exc_info:
                print(f"[daily] {code} 日线取数异常: {_exc_info}")
            else:
                print(f"[daily] {code} 日线数据不足(无异常): daily={'None' if daily is None else len(daily)}")
        if _fc[code] == 10:
            try: write_risk(str(now), "data_fetch_fail",
                            f"{code} 日线连续10次取数失败, G4/趋势闸失效中", code=code)
            except Exception: pass
        return ctx

    context._daily_ctx_cache_map[_cache_key] = ctx
    return ctx


def _base_entry_gate(cp: float, dc: dict):
    """G4 支撑建仓闸（2026-08-05 owner决策）：多票池不可能同时买入——
    仅"回踩重要支撑不破 + RSI/MACD/BOLL 日线联动 + 缩量"同时成立才放行建仓/回补。
    返回 (是否放行, 判定明细)。数值均为 TODO(PhaseD) 临时值，日常复盘只记录不调整。
    日线指标冻结于昨收（_refresh_daily_ctx 口径），盘中变量仅为现价 cp。"""
    if dc.get("daily_status") == "unavailable":
        return False, "G4: 日线数据不足→保守拦截"
    sup_gap = float(PARAMS.get("daily_ma_support_gap", 0.025))
    brk_gap = float(PARAMS.get("daily_ma_breakdown_gap", 0.015))
    # ① 回踩重要支撑不破：现价落在任一支撑位 [lv*(1-brk), lv*(1+sup)] 带内
    supports = {k: dc.get(k, 0) for k in ("daily_ma10", "daily_ma20", "daily_ma60", "daily_boll_lower")}
    supports = {k: v for k, v in supports.items() if v and v > 0}
    near = [k for k, lv in supports.items() if lv * (1 - brk_gap) <= cp <= lv * (1 + sup_gap)]
    if not near:
        return False, (f"G4: 未回踩支撑带 cp={cp:.2f} "
                       + " ".join(f"{k}={v:.2f}" for k, v in supports.items()))
    # ② RSI 企稳区间（回踩未超买、未崩盘）
    rsi = float(dc.get("daily_rsi14", 0) or 0)
    rsi_lo, rsi_hi = PARAMS.get("entry_rsi_low", 30), PARAMS.get("entry_rsi_high", 55)
    if not (rsi_lo <= rsi <= rsi_hi):
        return False, f"G4: RSI={rsi:.1f} 不在[{rsi_lo},{rsi_hi}] 支撑={near}"
    # ③ MACD 多头未破坏（DIF ≥ DEA）
    if not dc.get("daily_macd_bull", False):
        return False, (f"G4: MACD非多头 DIF={dc.get('daily_macd_dif',0):.3f}"
                       f"<DEA={dc.get('daily_macd_dea',0):.3f} 支撑={near} RSI={rsi:.1f}")
    # ④ BOLL 联动：不深破下轨
    bl = float(dc.get("daily_boll_lower", 0) or 0)
    if bl > 0 and cp < bl * (1 - brk_gap):
        return False, f"G4: 深破BOLL下轨 cp={cp:.2f} lower={bl:.2f} 支撑={near}"
    # ⑤ 缩量回调：近3日均量 < 20日均量 × 系数
    v3, v20 = float(dc.get("_vol3", 0) or 0), float(dc.get("_vol20", 0) or 0)
    shrink = PARAMS.get("entry_vol_shrink", 0.95)
    if v20 > 0 and v3 >= v20 * shrink:
        return False, f"G4: 未缩量 v3/v20={v3 / v20:.2f}≥{shrink} 支撑={near} RSI={rsi:.1f}"
    return True, (f"G4放行: 支撑={near} RSI={rsi:.1f} MACD多头 "
                  f"v3/v20={(v3 / v20 if v20 else 0):.2f}")


# ── 审计 JSONL ──

_AUDIT_LOG_PATH = os.path.join(PROJECT_DIR, "gmcache", "backtrace.jsonl")
_audit_file = None
_AUDIT_RUN_ID = ""

def _audit_write(rec: dict):
    rec['_run_id'] = _AUDIT_RUN_ID
    """追加一条决策审计记录"""
    global _audit_file
    try:
        if _audit_file is None:
            os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
            _audit_file = open(_AUDIT_LOG_PATH, "a", encoding="utf-8")
        _audit_file.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _audit_file.flush()
    except Exception:
        pass

def _audit_close():
    global _audit_file
    if _audit_file:
        _audit_file.close()
        _audit_file = None


import sell_state
from sell_state import (
    SELL_STATE_PATH, _sell_state_load, _sell_state_save,
    _pos_key, _split_pos_key, _sell_state_persist, _sell_state_restore,
)
# sell_state/sell_channels 经 GM 命名空间取 gm_main 的 MODE_LIVE/STOCKS/_audit_write 等
# （保持「唯一 import gm.api 在 gm_main」，且规避双模块名导入分裂）
sell_state.GM = sys.modules[__name__]
import sell_channels
sell_channels._bind_gm(sys.modules[__name__])
# 重导出（原 main 模块名兼容；测试沿用 main._sell_arbiter 等引用）
from sell_channels import _sell_arbiter, _sell_channel_gate  # noqa: E402


def _reconcile_positions_at_init(context):
    """F1: 启动全量持仓对账（2026-07-28 日复盘 P0）

    重启后 _base_ordered/_base_settled 为纯内存空集，若不从账户拉取真实持仓，
    已持有标的会被重发底仓单（2026-07-28 600481 三轮重复建仓至 5600 股事故）。
    本函数在 init 末尾执行：
      1. 逐票查询 account().positions()，有持仓则灌入 executed_orders/manual_position
      2. 已持仓标的直接入 _base_settled（跳过重发底仓单）并设 _base_ref_
      3. 每票写 reconcile_init 审计事件
    仅 MODE_LIVE 执行；回测模式跳过。
    """
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    if not _is_live:
        return
    for code, sym in STOCKS.items():
        try:
            pos = context.account().positions(symbol=sym, side=PositionSide_Long)
            if not pos or len(pos) == 0:
                continue
            p = pos[0]
            vol = int(p.volume)
            if vol <= 0:
                continue
            _cost = float(p.vwap or 0)
            context.executed_orders[sym] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": vol,
                "available": int(p.available),
                "t_qty": vol,
                "cost": _cost,
                "type": "stock",
                "pre_close": _cost,
            }
            context.manual_position[sym] = dict(context.executed_orders[sym])
            context._base_settled.add(code)
            # F7: _base_ref_ 语义=目标底仓(镜像表)，非实际持仓——缺口由 _base_topup_qty 择时回补
            setattr(context, f'_base_ref_{code}',
                    int(MIRROR_HOLDINGS.get(code, {}).get("qty", 0) or vol))
            _audit_write({"event": "reconcile_init", "code": code, "qty": vol,
                          "available": int(p.available), "cost": _cost,
                          "time": str(datetime.now())})
            print(f"[INIT] {code} {STOCK_NAMES.get(code, code)} 持仓对账: "
                  f"{vol}股 可用{int(p.available)} 成本{_cost:.2f}")
        except Exception as e:
            print(f"[INIT] {code} 持仓对账失败: {e}")


def _base_topup_qty(context, code, gm_sym):
    """F7: 底仓择时回补量（2026-07-29 owner定调：基线维持，缺口择时买回）

    语义: MIRROR_HOLDINGS 是目标底仓而非现状快照。已建仓标的实际持仓低于目标
    100 股以上时，返回回补量(向下100取整)，由 on_bar 底仓块走既有 M2/趋势闸
    择时买入；否则返回 0。

    反绞肉门控（本函数内，on_bar 的 M2/趋势闸仍照常叠加）:
      - 当日已有任意卖出成交(如PANIC止损) → 当日不反补，防恐慌-回补来回打脸
      - 指数 uni_down → 不补（防御日不加重敞口）
    """
    if code not in getattr(context, "_base_settled", set()):
        return 0  # 未建仓标的走原建仓路径
    _mirror = int(MIRROR_HOLDINGS.get(code, {}).get("qty", 0) or 0)
    if _mirror <= 0:
        return 0
    _held = int(context.manual_position.get(gm_sym, {}).get("qty", 0) or 0)
    _short = ((_mirror - _held) // 100) * 100
    if _short < 100:
        return 0
    if context.daily_sell_count.get(code, 0):
        return 0
    if getattr(context, "last_index_regime", "range") == "uni_down":
        return 0
    return _short


def init(context):
    global _AUDIT_RUN_ID
    _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 运维自举（0806 红日整改）：控制台日志落盘 + watcher 自动拉起
    ops_guard.bootstrap_logging(PROJECT_DIR)
    ops_guard.ensure_watcher(PROJECT_DIR)
    # P3-2 池分管校验：manual 池与 auto 池交集冲突 → 拒绝启动（fail-closed）
    _wl = os.path.join(os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader"),
                       "t_io", "state", "watchlist_buy.json")
    _pool_conflicts = _auto_pool.validate_pool_split(_wl)
    if _pool_conflicts:
        raise RuntimeError(
            f"P3-2 池分管冲突：{_pool_conflicts} 同属 manual 池与 auto 池，拒绝启动。"
            f"请修正 superTrader watchlist_buy.json 的 pool 字段或 config/auto_pool.py。")
    # D8/F10: 仅回测模式清空审计文件（模拟盘重启不丢当日段）
    _maybe_clear_audit_log(context)
    context.bar_cache = {}
    context.executed_orders = {}
    context.engine = SignalEngine()
    context.daily_buy_count = {}
    context.daily_sell_count = {}
    context.daily_trade_price = {}
    context.last_index_regime = "range"
    context.last_index_score = 0.0
    context.manual_position = {}
    context.latest_pre_close = {}
    context._base_ordered = set()
    context._base_settled = set()
    context._inflight_sell = {}   # F9: 在途卖单台账 {gm_sym: qty}
    context._pending_buy_snapshot = {}  # WP-A1: 做T买入快照 {委托id或symbol: manual_position条目快照}
    context._last_bar_eob = {}    # F9: 重复bar去重 {gm_sym: eob}
    context.cur_date = None
    context._daily_ctx_cache_map = {}
    context.total_trade_cost = 0.0
    context.total_trade_count = 0
    context.rejected_order_count = 0
    context.audit_records = []
    context.sizer = PositionSizer(params=PARAMS)

    # F1: 启动全量持仓对账——已持仓标的入 _base_settled，防止重启重发底仓单
    _reconcile_positions_at_init(context)
    # WP-B14: 卖出体系状态跨日恢复（pos_key 校验；qty<=0/不符 → 作废）
    _sell_state_restore(context)

    # P3-1(C) 冰点预热：盘前 gm history_n(60s×240) 预取进 bar_cache，消灭开盘 5 分钟指标空窗
    # （对齐方案「盘前用 gm history_n(60s×240) 预取」统一口径；预取覆盖上一交易日 session，
    #  开盘后 subscribe 追加当日 bar，>480 根自动裁剪）。
    for code, sym in STOCKS.items():
        try:
            his = history_n(symbol=sym, frequency="60s", count=240,
                           fields="symbol,eob,open,high,low,close,volume,amount",
                           fill_missing="Previous", adjust=ADJUST_PREV)
            if his is not None and len(his) > 0:
                rows = []
                for bar in his:
                    rows.append({
                        "time": str(bar["eob"]),
                        "open": float(bar["open"]),
                        "high": float(bar["high"]),
                        "low": float(bar["low"]),
                        "close": float(bar["close"]),
                        "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
                        "amount": float(bar["amount"]) if bar["amount"] is not None else 0,
                    })
                context.bar_cache[sym] = rows
        except Exception as e:
            print(f"[init] 历史分钟数据预取失败 {sym}: {e}")

    # 预取上证指数日线
    # 2026-08-28 复审修复（隐性遮蔽显式化）：本模块必须解析到 _gm/analysis 副本
    # （其 GM_INDEX_CACHE/GM_DATA_READY 是本策略的指数数据契约；superTrader 侧同名模块
    # 是另一套带 IO 的实现）。_GM_DIR 在 sys.path 最前，正常即命中 _gm 副本；
    # 若未来 sys.path 被外部改动（如 .gszq 壳误注入仓库根）而遮蔽到 superTrader 侧，立即 fail-loud。
    import analysis.index_regime as ir
    if "_gm" not in ir.__file__:
        raise RuntimeError(f"analysis.index_regime 解析错误（应命中 _gm 副本）: {ir.__file__}")
    try:
        idx_data = history_n(symbol="SHSE.000001", frequency="1d", count=900,
                            fields="eob,open,high,low,close,volume",
                            fill_missing="Previous")
        if idx_data is not None and len(idx_data) > 10:
            rows = []
            for bar in idx_data:
                dt = str(bar["eob"])[:10]
                rows.append({
                    "date": dt,
                    "open": float(bar["open"]),
                    "high": float(bar["high"]),
                    "low": float(bar["low"]),
                    "close": float(bar["close"]),
                    "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
                })
            df_idx = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
            ir.GM_INDEX_CACHE["SHSE.000001"] = df_idx
            ir.GM_DATA_READY = True
            print(f"[init] 大盘日线已缓存: {len(df_idx)} 行, {df_idx['date'].iloc[0]} ~ {df_idx['date'].iloc[-1]}")
        else:
            print(f"[init] 警告: history_n 未返回上证指数日线")
    except Exception as e:
        print(f"[init] 大盘日线预取失败: {e}")
        ir.GM_DATA_READY = False

    symbols = list(STOCKS.values())
    # WP-E2/E3: 启动预算表（复盘核对用——equity/现金保留/每股预算(按槽分解)/各票 max_pos_shares）
    try:
        _eq0 = _total_equity(context, INITIAL_CASH)
        _reserve0 = float(PARAMS.get("cash_reserve_pct", 0.20))
        _slots0 = max(int(PARAMS.get("max_concurrent_positions", 4)), 1)
        _bud0 = _eq0 * (1 - _reserve0) / _slots0
        _caps = []
        for _c, _s in STOCKS.items():
            _rows = context.bar_cache.get(_s) or []
            _px = float(_rows[-1].get("close", 0) or 0) if _rows else 0.0
            _mps = int(_bud0 / _px / 100) * 100 if _px > 0 else 0
            _caps.append(f"{_c}:{_mps}")
        print(f"[init] WP-E2/E3 预算表: equity={_eq0:.0f} reserve={_reserve0:.0%} "
              f"每股预算={_bud0:.0f}(按{_slots0}槽分解) max_pos_shares={' '.join(_caps)}")
    except Exception as _e:
        print(f"[init] WP-E2/E3 预算表生成失败: {_e}")
    subscribe(symbols=symbols, frequency="60s", count=240,
              fields="symbol,eob,open,high,low,close,volume,amount")
    # 确保事件桥目录存在
    from gm_bridge.writer import BRIDGE_DIR
    os.makedirs(BRIDGE_DIR, exist_ok=True)
    print(f"[init] 事件桥: {BRIDGE_DIR}")
    print(f"[init] 策略初始化完成: {len(symbols)} 只标的")


def on_bar(context, bars):
    now = context.now if hasattr(context, "now") else datetime.now()
    import utils.helpers as uh
    uh.SIM_NOW = now
    import t_engine_auto as tea
    tea.SIM_NOW = now

    t = now.time()
    today = now.date()

    # ── D1: 按日重置 ──
    if context.cur_date is None or context.cur_date != today:
        context.cur_date = today
        context.daily_buy_count.clear()
        context.daily_sell_count.clear()
        context.daily_trade_price.clear()
        context.engine._check_date_reset()
        _audit_write({"event": "date_reset", "date": str(today)})

    # ── KILL_SWITCH 检查 ──
    _killed = check_kill_switch()

    # 双向看门狗：watcher 心跳缺失/过期自动重生（0806 红日整改）
    ops_guard.ensure_watcher(PROJECT_DIR)

    if t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
        return

    # ── D4: 大盘态势（每交易日一次） ──
    if today != getattr(context, "_last_ir_date", None):
        context._last_ir_date = today
        try:
            import analysis.index_regime as ir
            if ir.GM_DATA_READY:
                # 每个交易日重新拉指数日线（回测时钟下自动对齐）
                try:
                    idx_data = history_n(symbol="SHSE.000001", frequency="1d", count=900,
                                        fields="eob,open,high,low,close,volume",
                                        fill_missing="Previous")
                    if idx_data is not None and len(idx_data) > 10:
                        rows = []
                        for bar in idx_data:
                            rows.append({
                                "date": str(bar["eob"])[:10],
                                "open": float(bar["open"]),
                                "high": float(bar["high"]),
                                "low": float(bar["low"]),
                                "close": float(bar["close"]),
                                "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
                            })
                        df_idx = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
                        ir.GM_INDEX_CACHE["SHSE.000001"] = df_idx
                        ir.GM_DATA_READY = True
                except Exception as e:
                    print(f"[ir] 指数日线刷新失败: {e}")
                    # R-3(2026-08-07 W32表决): regime 数据故障不再静默——fail-open 保留但要告警
                    try: write_risk(str(now), "regime_degraded", f"指数日线刷新失败: {str(e)[:120]}", code="")
                    except Exception: pass

                # R-1(2026-08-07 W32表决): 实盘传 mode="live"，剔除当日未成形K线再判定
                try:
                    _ir_mode = "live" if context.mode == MODE_LIVE else "eod"
                except Exception:
                    _ir_mode = "eod"
                ir_regime, ir_score, ir_ctx = ir.detect_index_regime(
                    as_of=now.strftime("%Y-%m-%d"), force=True, mode=_ir_mode)
                context.last_index_regime = ir_regime.value if hasattr(ir_regime, "value") else str(ir_regime)
                context.last_index_score = float(ir_score)
                degraded = ir_ctx.get("degraded", [])
                if degraded:
                    print(f"[ir] {str(today)} regime={context.last_index_regime} score={context.last_index_score:.1f} degraded={degraded}")
                    # R-3: degraded fail-open 但写 risk 告警
                    try: write_risk(str(now), "regime_degraded", f"degraded={degraded} regime={context.last_index_regime}", code="")
                    except Exception: pass
                else:
                    print(f"[ir] {str(today)} regime={context.last_index_regime} score={context.last_index_score:.1f}")
        except Exception as e:
            print(f"[ir] 大盘态势判定失败: {e}")

    # ── 心跳（每分钟写一次；仅模拟盘/实盘，回测跳过省I/O——纯监控产物不参与决策） ──
    try:
        _hb_live = context.mode == MODE_LIVE
    except Exception:
        _hb_live = False
    if _hb_live:
        # F11: 心跳持仓改走 _get_holding 多源对账（含终端空仓向下同步），
        # 不再裸读 manual_position（0731 心跳报000988=300 实际=0 事故）
        _hb_positions = {}
        for _hc, _hs in STOCKS.items():
            try:
                _h = _get_holding(context, _hc, _hs)
            except Exception:
                continue
            if int(_h.get("qty", 0) or 0) > 0:
                _hb_positions[_hs] = {"qty": int(_h.get("qty", 0)),
                                      "cost": float(_h.get("cost", 0) or 0)}
        # ①-3: 实时读取可用现金
        _hb_cash = INITIAL_CASH
        try:
            _acct = context.account()
            _c = getattr(_acct, 'cash', None)
            if _c is not None:
                _c = _c() if callable(_c) else _c
                if isinstance(_c, dict):
                    _hb_cash = float(_c.get('available', _c.get('total', INITIAL_CASH)))
                else:
                    _hb_cash = float(_c)
        except Exception:
            pass
        write_heartbeat(
            time_str=str(now), bar=f"{now:%H:%M}",
            positions=_hb_positions,
            cash=_hb_cash,
            index_regime=context.last_index_regime,
            index_score=context.last_index_score,
        )

    for bar in bars:
        gm_sym = str(bar["symbol"])
        code = _raw_code(gm_sym)
        if code not in STOCKS:
            continue

        # F9: 同 eob 重复 bar 去重（2026-07-31 模拟盘同秒 4 次重复投递
        # 导致 PANIC 连发 4 单；同时防止 bar_cache 重复累积）
        if _dedup_bar(context, gm_sym, str(bar["eob"])):
            continue

        # 累积 bar
        row = {
            "time": str(bar["eob"]),
            "open": float(bar["open"]),
            "high": float(bar["high"]),
            "low": float(bar["low"]),
            "close": float(bar["close"]),
            "volume": float(bar["volume"]) if bar["volume"] is not None else 0,
            "amount": float(bar["amount"]) if bar["amount"] is not None else 0,
        }
        context.bar_cache.setdefault(gm_sym, []).append(row)
        if len(context.bar_cache[gm_sym]) > 480:
            context.bar_cache[gm_sym] = context.bar_cache[gm_sym][-480:]
        # WP-B18 M3: 缓存当日首根 bar 开盘价（跳空判断用）
        if not hasattr(context, "_day_open"):
            context._day_open = {}
        context._day_open.setdefault(code, row["open"])

        df = _build_bar_df(context, code, gm_sym)
        if df.empty:
            continue

        cp = float(bar["close"])

        # ── D6: VWAP 单位验证（首次 bar 打一行） ──
        if not getattr(context, "_vwap_checked", False) and row["amount"] > 0 and row["volume"] > 0:
            ratio = row["amount"] / row["volume"]
            print(f"[D6] amount/volume={ratio:.2f} close={cp:.2f} → VWAP单位比={ratio/cp:.4f}")
            if ratio / cp < 0.1:
                print(f"[D6] 结论: volume单位为股，需移除×100")
            context._vwap_checked = True

        # ── D2: 底仓（按镜像持仓表逐股建仓 + F7择时回补缺口） ──
        _topup_qty = _base_topup_qty(context, code, gm_sym)
        if code not in context._base_ordered and (code not in context._base_settled or _topup_qty >= 100):
            mirror = MIRROR_HOLDINGS.get(code, {})
            base_qty = mirror.get("qty", 0) if code not in context._base_settled else _topup_qty
            _is_topup = code in context._base_settled  # F8: 已持仓标的走回补路径——闸门拦截不得中断持仓信号评估(0730盲区事故)
            if base_qty < 100:
                print(f"[{now:%H:%M:%S}] BASE {code} 跳过: MIRROR_HOLDINGS 中无此标的或 qty<100")
                context._base_settled.add(code)
                if not _is_topup:
                    return
            # M2: 做T门槛检查（底仓建仓前置）
            _dc = _refresh_daily_ctx(context, code, gm_sym, now)
            # R1/A3: 底仓过趋势闸——TREND_BREAKDOWN 延迟到次日（F5: 回退61a19e6激进模式）
            _trend = _dc.get("_stock_trend_state", "TREND_RANGE")
            _topup_blocked = False
            # WP-B19-rev(2026-08-28): 硬止损触发日禁 BASE 建仓/回补（最先执行，任何门槛前拦截；每票每日去重留痕）
            _hs_today_base = (getattr(context, "_hard_stop_today", {}) or {}).get(code)
            if not _topup_blocked and _hs_today_base == now.strftime("%Y-%m-%d"):
                _bhs_k = f'_hard_stop_block_{code}'
                if getattr(context, _bhs_k, '') != now.strftime("%Y-%m-%d"):
                    setattr(context, _bhs_k, now.strftime("%Y-%m-%d"))
                    try:
                        write_risk(str(now), "hard_stop_block",
                                   f"BASE blocked after HARD_STOP today", code=code)
                    except Exception:
                        pass
                    _audit_write({"event": "buy_blocked", "code": code, "reason": "hard_stop",
                                  "where": "base", "cp": cp,
                                  "time": str(now)})
                    print(f"[{now:%H:%M:%S}] BASE {code} 硬止损触发日→禁建仓 cp={cp:.2f}")
                if not _is_topup:
                    return
                _topup_blocked = True
            if _trend == "TREND_BREAKDOWN":
                _defer_key = f'_base_deferred_{code}'
                if getattr(context, _defer_key, '') != now.strftime("%Y-%m-%d"):
                    setattr(context, _defer_key, now.strftime("%Y-%m-%d"))
                    print(f'[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} TREND_BREAKDOWN→延迟建仓')
                    try: write_risk(str(now), "base_deferred", f"_stock_trend_state={_trend}", code=code)
                    except: pass
                if _hb_live:
                    try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}",
                                        gate="trend_breakdown", gate_detail=_trend)
                    except Exception: pass
                if not _is_topup:
                    return
                _topup_blocked = True  # F8: 回补被趋势闸拦截，但持仓信号评估照常落地
            # WP-E3: 持仓槽位闸（底仓建仓块）——该票当前持仓为 0（建仓=新增持票数）
            # 且槽满 → 以 base_deferred(reason=slot_full) 延迟，下一根 bar 自然重试
            # （复用既有延迟机制，不新建重试）；该票已持仓的 topup 回补不受限。
            _held_now = int(context.manual_position.get(gm_sym, {}).get("qty", 0) or 0)
            if not _topup_blocked and _held_now <= 0 and _slot_full(context):
                _emit_slot_full(context, code, now, "base")
                if not _is_topup:
                    return
                _topup_blocked = True
            # 默认 False: 数据不足时保守不放行（F5: 恢复M2门槛）
            if not _topup_blocked and not _dc.get("_m2_pool_pass", False):
                # O-03(2026-08-07 W32表决): pool_gate 每票每日只报一次（0807 实战:3票×237bar=711条刷屏）
                _pg_key = f'_pool_gate_{code}'
                if getattr(context, _pg_key, '') != now.strftime("%Y-%m-%d"):
                    setattr(context, _pg_key, now.strftime("%Y-%m-%d"))
                    print(f"[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} 门槛未过→仅观察 "
                          f"(amp={_dc.get('_m2_amp20',0):.1%} amt={_dc.get('_m2_amount20',0)/1e8:.1f}亿 "
                          f"lot={_dc.get('_m2_lot_value',0):.0f}元)")
                    try: write_risk(str(now), "pool_gate", f"amp={_dc.get('_m2_amp20',0):.1%} 仅观察", code=code)
                    except: pass
                if _hb_live:
                    try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}", gate="pool_gate",
                                        gate_detail=(f"amp={_dc.get('_m2_amp20',0):.1%} "
                                                     f"amt={_dc.get('_m2_amount20',0)/1e8:.1f}亿 "
                                                     f"lot={_dc.get('_m2_lot_value',0):.0f}元"))
                    except Exception: pass
                if not _is_topup:
                    context._base_settled.add(code)
                    return
                _topup_blocked = True  # F8: 回补被M2闸拦截，信号评估照常
            # P4-6: auto 建仓判定接 core/build_decision（P3 双侧单一真源）。
            # 数据适配：_daily_df（个股日线）+ ir.GM_INDEX_CACHE（指数日线）+ bar_cache 1m → 决策核；
            # 数据不足 fail-closed；WP-B20 双通道降为参考留痕（与 manual 侧 result["channels"] 同定位）。
            # 2026-08-31（owner批复）: 回补(topup)走轻量闸——豁免 build_decision=signal 要求，
            # 个股非 TREND_BREAKDOWN（上方 :1075 趋势闸已拦）即允许补回 MIRROR 目标。
            # 语义：回补是恢复既有持仓配置，不是新建仓决策；震荡市 go 恒 False 曾致止损后
            # 5 个月空仓踏空（run8 实证：588170 硬止损后 +160% 行情全程未回补）。
            # 全新建仓（非 topup）仍走下方全闸不变。
            if not _topup_blocked and not _is_topup:
                from signals import position_builder as _pb
                from build_decision_auto import decide as _bd_decide
                _idx_df = None
                try:
                    import analysis.index_regime as _ir
                    _idx_df = _ir.GM_INDEX_CACHE.get("SHSE.000001")
                except Exception:
                    _idx_df = None
                _daily_df = _dc.get("_daily_df")
                if _daily_df is None or _daily_df.empty or _idx_df is None or _idx_df.empty:
                    _dec = {"go": False, "veto": [], "verdict": "weak", "reasons": ["数据不足(日线/指数缺失) fail-closed"],
                            "data_insufficient": True}
                else:
                    _bars = context.bar_cache.get(gm_sym, []) or []
                    _today_bars = [b for b in _bars if str(b.get("time", "")).startswith(now.strftime("%Y-%m-%d"))]
                    _df1m = None
                    try:
                        _src = _today_bars if _today_bars else _bars
                        if _src:
                            _df1m = pd.DataFrame(_src)
                    except Exception:
                        _df1m = None
                    _dec = _bd_decide(_daily_df, _idx_df, now.strftime("%Y-%m-%d"), None, df_1min=_df1m)
                _bd_verdict = _dec["verdict"]
                # WP-B20 双通道 → 参考留痕（不再驱动放行）
                _pb_res = _pb.eval_dual_channels(
                    _dc, cp, m5_df=_pb.build_m5_df(context.bar_cache.get(gm_sym, [])),
                    scan_type="intraday")
                _pb_verdict = _pb_res["verdict"]
                _pb_channel = _pb_res["channel"]
                _pb_score = _pb_res["composite_score"]
                if _bd_verdict != "signal":
                    _bd_key = f'_bd_last_{code}'
                    _bd_sig = f"{_bd_verdict}|go={_dec.get('go')}|{'、'.join(_dec.get('veto', []))}"
                    if getattr(context, _bd_key, None) != _bd_sig:
                        setattr(context, _bd_key, _bd_sig)
                        print(f"[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} "
                              f"build_decision={_bd_verdict}(go={_dec.get('go')}, "
                              f"veto={'、'.join(_dec.get('veto', [])) or '无'})→仅观察 "
                              f"(双通道参考:{_pb_channel}={_pb_verdict}/{_pb_score})")
                        try:
                            write_risk(str(now), "build_gate",
                                       f"verdict={_bd_verdict} go={_dec.get('go')} "
                                       f"veto={'、'.join(_dec.get('veto', [])) or '无'}", code=code)
                        except Exception:
                            pass
                        _audit_write({"event": "build_gate_block", "code": code,
                                      "verdict": _bd_verdict, "go": _dec.get("go"),
                                      "veto": _dec.get("veto"), "cp": cp, "time": str(now),
                                      "channels": f"{_pb_channel}={_pb_verdict}({_pb_score})"})
                    if _hb_live:
                        try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}",
                                            gate="build_gate",
                                            gate_detail=f"{_bd_verdict}(go={_dec.get('go')})")
                        except Exception: pass
                    if not _is_topup:
                        return
                    _topup_blocked = True  # build_decision 拦截回补，信号评估照常（F8 同模式）
                else:
                    if _hb_live:
                        try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}",
                                            gate="build_gate_pass", gate_detail=f"signal(score={_dec.get('score')})")
                        except Exception: pass
                    if getattr(context, f'_bd_last_{code}', None) is not None:
                        setattr(context, f'_bd_last_{code}', None)
                        print(f"[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} build_decision=signal")
            if not _topup_blocked:
                try:
                    try:
                        write_order(str(now), code, "BUY", base_qty, cp, order_id="base")
                    except Exception:
                        pass
                    order_volume(symbol=gm_sym, volume=base_qty,
                                 side=OrderSide_Buy,
                                 order_type=OrderType_Market,
                                 position_effect=PositionEffect_Open)
                    context._base_ordered.add(code)
                    print(f"[{now:%H:%M:%S}] BASE {code} {STOCK_NAMES.get(code,code)} 下单 {base_qty}股@{cp:.2f}")
                    _audit_write({"event": "base_order", "code": code, "qty": base_qty, "price": cp, "time": str(now)})
                    if _hb_live:
                        try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}",
                                            gate="base_order", action="BUY",
                                            gate_detail=f"qty={base_qty}")
                        except Exception: pass
                except Exception as e:
                    print(f"[{now:%H:%M:%S}] BASE {code} 下单失败: {e}")
                    try:
                        write_risk(str(now), "order_failed", f"BASE BUY {base_qty}@{cp:.2f} err={e}", code=code)
                    except Exception:
                        pass
                return
            # F8: _topup_blocked=True 时不下单，继续走下方信号评估流程

        if code not in context._base_settled and code in context._base_ordered:
            # 已下单未成交，跳过
            return

        # ── 日线上下文刷新（每日首根有效 bar） ──
        daily_ctx = _refresh_daily_ctx(context, code, gm_sym, now)
        # 注入指数态势
        daily_ctx["index_circuit_state"] = "clear" if context.last_index_regime == "uni_down" else "normal"
        daily_ctx["index_gate_advice"] = "defensive_t" if context.last_index_regime == "uni_down" else "normal_t"

        # 持仓读取
        holding = _get_holding(context, code, gm_sym)
        pos_qty = int(holding.get("qty", 0) or 0)
        if pos_qty <= 0:
            return

        # N6: 开盘5分钟买入隔离
        morning_no_buy = now.hour == 9 and now.minute <= 35

        # T+1 结转
        if now.hour == T1_AUTO_UNLOCK_HOUR and now.minute == T1_AUTO_UNLOCK_MINUTE:
            mp = context.manual_position.get(gm_sym)
            if mp:
                mp["available"] = mp.get("qty", 0)
                mp["t_qty"] = mp.get("qty", 0)

        # 补上 today_ret
        prev_close = float(daily_ctx.get("daily_prev_close", 0) or 0)
        if prev_close > 0:
            daily_ctx["daily_day_ret"] = (cp - prev_close) / prev_close

        # ── 信号引擎 ──
        try:
            buy_score, sell_score, sig = context.engine.evaluate(
                code, STOCK_NAMES.get(code, code), df, holding, daily_ctx)
        except Exception as e:
            print(f"[{now:%H:%M:%S}] {code} evaluate err: {e}")
            continue
        # 修正 profit_pct: engine 读 DataFrame 最后一行可能不是当前 bar
        _holding_cost = float(holding.get("cost", 0) or 0)
        _last_f = context.engine._last_feats.get(code, {})
        if _last_f and _holding_cost > 0:
            _last_f["price"] = cp
            _last_f["profit_pct"] = (cp - _holding_cost) / _holding_cost
            _last_f["vwap"] = cp
            _daily_atr = float(daily_ctx.get("daily_atr", 0.02) or 0.02)
            _panic_trigger = max(-5 * _daily_atr, -0.12)
            _last_f["is_deep_loss"] = _holding_cost > 0 and _last_f["profit_pct"] < _panic_trigger
            _last_f["panic_trigger"] = _panic_trigger

        # ── D5/G2: uni_down 熔断 ──
        if context.last_index_regime == "uni_down" and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None

        # ── R1/G3: 个股趋势熔断（一票一闸） ──
        _trend = daily_ctx.get("_stock_trend_state", "TREND_RANGE")
        # 2026-08-31: G3 闸接入个股放行开关（与 RiskManager 同源语义）——此前此处硬拦，
        # 588170 配了 allow_breakdown_buy=True 仍被掐（回测 run8 实证：23 次 BUY_LOW 全灭）
        _g3_allow_bd = bool(STOCK_PARAMS.get(code, {}).get("allow_breakdown_buy"))
        if (_trend == "TREND_BREAKDOWN" and not _g3_allow_bd
                and sig and sig.action in ("BUY_LOW", "ADD_POS")):
            sig = None
            try: write_risk(str(now), "stock_trend_gate", f"{_trend} 禁买", code=code)
            except: pass
        elif _trend == "TREND_DOWN" and sig and sig.action == "ADD_POS":
            sig = None

        # ── D5: 尾盘回转（14:50-15:00，先于 PANIC_SELL 检查） ──
        is_tail = now.hour == 14 and now.minute >= 50
        if is_tail and sig and sig.action in ("BUY_LOW", "ADD_POS"):
            sig = None

        # ── P0-P6 卖出通道门链（P4-1 迁至 sell_channels._sell_channel_gate，行为逐字一致）──
        feats_cache = getattr(context.engine, "_last_feats", {}).get(code, {})
        sig, tail_done = sell_channels._sell_channel_gate(
            context, code, gm_sym, cp, now, sig, pos_qty, holding, daily_ctx,
            feats_cache, is_tail, morning_no_buy)
        if tail_done:
            continue

        if sig is None:
            _last_dec = context.engine.last_decision.get(code, {})
            # WP-B07: 高接延迟事件（每次记忆建立后只报一次，防每 bar 刷屏）
            if _last_dec.get("reason") == "buyback_above_sell_delayed":
                _bb_key = f"{code}|{_last_dec.get('sell_time', '')}"
                _bb_notified = getattr(context, "_buyback_delayed_notified", None)
                if _bb_notified is None:
                    _bb_notified = set()
                    context._buyback_delayed_notified = _bb_notified
                if _bb_key not in _bb_notified:
                    _bb_notified.add(_bb_key)
                    try:
                        write_buyback(str(now), code, "delayed",
                                      detail=(f"sell={_last_dec.get('sell_price')} "
                                              f"cur={_last_dec.get('price')} "
                                              f"premium={float(_last_dec.get('premium', 0) or 0):.2%} "
                                              f"reason=above_sell_delay"),
                                      sell_price=_last_dec.get("sell_price"),
                                      price=_last_dec.get("price"),
                                      premium=_last_dec.get("premium"))
                    except Exception:
                        pass
            _audit_write({
                "event": "no_signal", "code": code, "time": str(now),
                "buy_score": buy_score, "sell_score": sell_score,
                "pos_qty": pos_qty, "price": cp,
                "index_regime": context.last_index_regime,
                "buy_blocks": _last_dec.get("buy_blocks", []),
                "sell_blocks": _last_dec.get("sell_blocks", []),
                "decision_reason": _last_dec.get("reason", ""),
                "profit_pct": feats_cache.get("profit_pct", 0),
                "daily_atr": feats_cache.get("daily_atr", 0),
                "is_deep_loss": feats_cache.get("is_deep_loss", False),
            })
            if _hb_live:
                try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}",
                                    buy_score=buy_score, sell_score=sell_score,
                                    gate="evaluated", pos_qty=pos_qty,
                                    gate_detail=_last_dec.get("reason", ""))
                except Exception: pass
            continue

        # ── 信号事件写入 ──
        if sig is not None:
            # WP-B15: 信号事件去重——被下游拦截点（地板/到顶）mute 的同源信号静默，
            # 首条照写；mute 键含日期，日切自清；持仓变化由成交/对账回调清键。
            # 快照(snapshot)不受 mute 影响（监控底座，非事件流）。
            _mute_key = f'_sig_muted_{code}_{sig.action}'
            if getattr(context, _mute_key, '') != now.strftime("%Y-%m-%d"):
                try:
                    write_signal(str(now), code, sig.action, sig.score,
                                 reasons=sig.reasons, pos_qty=pos_qty)
                except Exception:
                    pass
            if _hb_live:
                try: write_snapshot(str(now), code, cp, bar=f"{now:%H:%M}",
                                    buy_score=buy_score, sell_score=sell_score,
                                    gate="signal", action=sig.action, pos_qty=pos_qty,
                                    gate_detail=";".join(sig.reasons or []))
                except Exception: pass

        # ── 参数准备 ──
        stock_params = STOCK_PARAMS.get(code, {})
        max_buys = stock_params.get("max_buy_times_per_stock", 3)

        # ── D1: 引擎冷却/计数 ──
        threshold = stock_params.get("notify_sell_threshold", 65) if sig.action in ("SELL_HIGH", "PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "TARGET_SELL", "HARD_STOP_EXIT") else \
                    stock_params.get("notify_buy_threshold", 43)

        if sig.score < threshold:
            continue

        # 执行交易
        if sig.action in ("BUY_LOW", "ADD_POS"):
            # WP-B19-rev(2026-08-28): 硬止损触发日禁一切买入（BUY_LOW/buyback/ADD_POS 一视同仁；每票每日去重留痕）
            _hs_today = (getattr(context, "_hard_stop_today", {}) or {}).get(code)
            if _hs_today == now.strftime("%Y-%m-%d"):
                _mbk = f'_hard_stop_block_{code}'
                if getattr(context, _mbk, '') != now.strftime("%Y-%m-%d"):
                    setattr(context, _mbk, now.strftime("%Y-%m-%d"))
                    try:
                        write_risk(str(now), "hard_stop_block",
                                   f"BUY {sig.action} blocked after HARD_STOP today", code=code)
                    except Exception:
                        pass
                    _audit_write({"event": "buy_blocked", "code": code, "reason": "hard_stop",
                                  "action": sig.action, "cp": cp,
                                  "time": str(now)})
                    print(f"[{now:%H:%M:%S}] BUY {code} 硬止损触发日→禁买 {sig.action} cp={cp:.2f}")
                continue
            if _killed:
                try:
                    write_risk(str(now), "kill_switch", f"KILL_SWITCH 阻止 {code} 买入", code=code)
                except Exception:
                    pass
                continue
            bc = context.daily_buy_count.get(code, 0)
            if bc >= max_buys:
                continue
            # WP-B18 3.2: 回补记忆互斥矩阵（M1-M4）——仅该票有回补记忆时检查
            _ab_now = (getattr(context.engine, "awaiting_buyback", {}) or {}).get(code)
            if _ab_now:
                _day_open = float(getattr(context, "_day_open", {}).get(code, row.get("open", 0)) or 0)
                _mx_act, _mx_rule = _buyback_mutex_block(context, code, daily_ctx,
                                                         _day_open, cp, now, _ab_now)
                if _mx_act == "block":
                    context.engine.awaiting_buyback.pop(code, None)
                    try:
                        write_buyback(str(now), code, "blocked",
                                      detail=(f"rule={_mx_rule} "
                                              f"sell={_ab_now.get('sell_price')} "
                                              f"target={_ab_now.get('target_price')} "
                                              f"trend={daily_ctx.get('_stock_trend_state', '')}"),
                                      rule=_mx_rule, sell_price=_ab_now.get("sell_price"),
                                      target_price=_ab_now.get("target_price"))
                    except Exception:
                        pass
                    _audit_write({"event": "buyback_blocked", "code": code, "rule": _mx_rule,
                                  "sell_price": _ab_now.get("sell_price"),
                                  "target_price": _ab_now.get("target_price"),
                                  "time": str(now)})
                    try:
                        _sell_state_persist(context, code, gm_sym)  # 落盘清除镜像
                    except Exception:
                        pass
                    continue
                if _mx_act == "delay":
                    try:
                        write_buyback(str(now), code, "blocked",
                                      detail=(f"rule=M3 开盘跳空延迟到09:35后评估 "
                                              f"sell={_ab_now.get('sell_price')}"),
                                      rule="M3", sell_price=_ab_now.get("sell_price"))
                    except Exception:
                        pass
                    _audit_write({"event": "buyback_blocked", "code": code, "rule": "M3",
                                  "sell_price": _ab_now.get("sell_price"),
                                  "time": str(now)})
                    continue

            # WP-E3: 持仓槽位闸（买入执行块）——仅全新建仓(pos_qty<=0)检查；
            # 已持仓票的做T买入不新增持票数，不受槽位闸限制
            if pos_qty <= 0 and _slot_full(context):
                _emit_slot_full(context, code, now, "buy")
                continue

            # N3: 现金预检（移到 sizer 之前，供 target_t 计算）
            available_cash = INITIAL_CASH
            _cash_ok = False
            try:
                _acct = context.account()
                _c = getattr(_acct, 'cash', None)
                if _c is not None:
                    _c = _c() if callable(_c) else _c
                    if isinstance(_c, dict):
                        # gm3 account().cash 返回 dict，键名可能是 available/available_cash/total
                        _v = _c.get('available') or _c.get('available_cash') or _c.get('cash') or _c.get('total') or 0
                        available_cash = float(_v)
                    else:
                        available_cash = float(_c)
                    _cash_ok = True
            except Exception:
                pass
            if not _cash_ok:
                available_cash = 0  # N15: fail-closed 每 bar 生效
                if not getattr(context, '_cash_warned', False):
                    context._cash_warned = True
                    print(f'[N8] WARN: 无法读取可用现金 → fail-closed: 禁止买入')

            # N10/WP-E2: 算 target_t（总权益预算制下的个股最大仓位，供 sizer 算 max_buyable）
            pos_limit_pct = float(PARAMS.get('max_single_position_pct', 0.80))
            # WP-E2: 总权益 = 现金 + Σ全部持仓市值（旧口径用 available_cash 等权分，无视其他票持仓）
            _total_eq = _total_equity(context, available_cash)
            _stock_budget, max_pos_shares = _stock_budget_cap(context, code, cp, _total_eq)
            _base_ref = getattr(context, f'_base_ref_{code}', 0) or pos_qty
            # 2026-08-31（owner批复）: 做T买入顶帽加一档T余量——做T语义即「底仓之上加一档、
            # 日内了结」，旧口径 ceiling=max(预算帽,底仓) 在持仓=底仓时恒到顶、
            # 做T加仓永远被拦（run8 实证: 002451 底仓1300=顶帽1300，3 次 BUY_LOW 全灭）
            _t_head = 0
            if sig.action in ("BUY_LOW", "ADD_POS") and _base_ref > 0:
                _t_pct = float(context.engine._get_params(code).get("stock_qty_base_pct", 0.3) or 0.3)
                _t_head = max(100, int(_base_ref * _t_pct / 100) * 100)
            target_t = max(max_pos_shares, _base_ref + _t_head, pos_qty)
            holding_with_target = dict(holding, target_t=target_t)

            # WP-E2: 个股最大仓位闸——到顶直接拦截（堵 sizer 内部 1.5× 兜底洞）
            if _check_max_pos_cap(context, code, now, pos_qty, _base_ref,
                                  max_pos_shares, _stock_budget, _total_eq,
                                  action=sig.action, t_headroom=_t_head):
                continue

            qty = context.sizer.calc_buy_qty(code, holding_with_target, sig.score, threshold)
            if qty <= 0:
                # WP-E2: 区分兜底——全新建仓(pos_qty<=0)保留 300 股兜底；
                # 已有持仓 sizer 返回 0 = 已到个股上限 → max_pos_cap（堵 qty=300 强制兜底洞）
                if pos_qty <= 0:
                    qty = 300  # 全新建仓信号的最小交易量兜底
                else:
                    _check_max_pos_cap(context, code, now, pos_qty, _base_ref,
                                       max_pos_shares, _stock_budget, _total_eq,
                                       force=True, action=sig.action, t_headroom=_t_head)
                    continue

            # WP-B07: 高接降档 — 数量减半取整到 min_unit，不足 min_unit 则延迟
            qty, _bb_dg, _bb_min_unit = _apply_buyback_downgrade(context, code, sig, qty)
            if _bb_dg is not None and qty < _bb_min_unit:
                try:
                    write_buyback(str(now), code, "delayed",
                                  detail=(f"downgrade_below_min_unit: sizer_halved<{_bb_min_unit} "
                                          f"sell={_bb_dg.get('sell_price')} cur={_bb_dg.get('price')} "
                                          f"premium={float(_bb_dg.get('premium', 0) or 0):.2%}"),
                                  sell_price=_bb_dg.get("sell_price"),
                                  price=_bb_dg.get("price"),
                                  premium=_bb_dg.get("premium"))
                except Exception:
                    pass
                _audit_write({"event": "buyback_downgrade_defer", "code": code,
                              "sell_price": _bb_dg.get("sell_price"), "price": _bb_dg.get("price"),
                              "premium": _bb_dg.get("premium"), "time": str(now)})
                continue

            # N3: 现金预检
            max_by_cash = int(available_cash * 0.95 / cp / 100) * 100 if cp > 0 else 0
            qty = min(qty, max_by_cash) if max_by_cash > 0 else qty
            if qty < 100:
                if bc == 0:
                    print(f'[{now:%H:%M:%S}] BUY {code} 现金不足跳过: 可用={available_cash:.0f}')
                try:
                    write_risk(str(now), "cash_insufficient",
                               f"available={available_cash:.0f} needed={qty*cp:.0f}", code=code)
                except Exception:
                    pass
                continue

            # N2: 仓位上限检查（WP-E2: 分母修正为总权益——现金+全部持仓市值，
            # 旧口径只算本票市值，"账户总权益"名不副实；max_single_position_pct=0.80 保留为外层安全帽）
            current_pos_value = pos_qty * cp
            new_pos_value = current_pos_value + qty * cp
            total_equity_value = _total_eq if _total_eq > 0 else (available_cash + current_pos_value)
            if total_equity_value > 0 and new_pos_value / total_equity_value > pos_limit_pct:
                print(f'[{now:%H:%M:%S}] BUY {code} 仓位上限拦截: {new_pos_value/total_equity_value:.0%}>{pos_limit_pct:.0%}')
                try:
                    write_risk(str(now), "position_limit",
                               f"{new_pos_value/total_equity_value:.1%}>{pos_limit_pct:.0%} qty={qty}", code=code)
                except Exception:
                    pass
                continue
            try:
                write_order(str(now), code, "BUY", qty, cp)
            except Exception:
                pass
            try:
                _oid = order_volume(symbol=gm_sym, volume=qty,
                                    side=OrderSide_Buy,
                                    order_type=OrderType_Market,
                                    position_effect=PositionEffect_Open)
                # WP-A1: 下单副作用之前留存 manual_position 条目快照（含"无此条目"状态）。
                # 快照法而非逆运算，避免成本加权逆推的浮点漂移；纯日内状态，无需落盘。
                if not hasattr(context, "_pending_buy_snapshot") or context._pending_buy_snapshot is None:
                    context._pending_buy_snapshot = {}
                # 2026-08-30: order_volume 返回 List[Dict]（同步下单回报），非单值——此前
                # 直接把整个 list 当 dict 键 → TypeError: unhashable type: 'list'，
                # 做T买入(BUY_LOW/ADD_POS)下单成功后记账全崩、manual_position 永不更新
                # （run9 实证 90 次，并连锁 16 次 status=8 仓位不足卖单拒单）。取首单
                # cl_ord_id 作快照键，空/异常回退 gm_sym（与 _pop_buy_snapshot 的 symbol 兜底一致）。
                _orders = _oid if isinstance(_oid, list) else []
                _first = _orders[0] if _orders and isinstance(_orders[0], dict) else {}
                _snap_key = (_first.get("cl_ord_id") or _first.get("order_id")) or gm_sym
                _snap = context.manual_position.get(gm_sym)
                context._pending_buy_snapshot[_snap_key] = copy.deepcopy(_snap) if _snap else None
                context.daily_buy_count[code] = bc + 1
                context.daily_trade_price[code] = cp
                context.total_trade_count += 1
                # 手动跟踪买入（T+1: 只加 qty，不加 available）
                old = context.manual_position.get(gm_sym, {"qty": 0, "available": 0, "cost": cp, "t_qty": 0})
                old_q = int(old.get("qty", 0))
                old_c = float(old.get("cost", cp))
                new_q = old_q + qty
                new_c = (old_c * old_q + cp * qty) / new_q if new_q > 0 else cp
                context.manual_position[gm_sym] = dict(old, **{"qty": new_q, "t_qty": new_q, "cost": new_c})
                context.engine.buy_count_per_stock[code] = context.daily_buy_count.get(code, 0)
                print(f"[{now:%H:%M:%S}] BUY {code} {qty}@{cp:.2f} score={sig.score:.0f} regime={context.last_index_regime}")
                _audit_write({"event": "buy", "code": code, "qty": qty, "price": cp, "score": sig.score,
                              "time": str(now), "regime": context.last_index_regime,
                              "pos_after_buy": new_q, "buy_count": bc + 1})
                # WP-B07: 降档成交事件
                if _bb_dg is not None:
                    try:
                        write_buyback(str(now), code, "downgrade",
                                      detail=(f"qty={qty}@{cp:.2f} "
                                              f"sell={_bb_dg.get('sell_price')} "
                                              f"premium={float(_bb_dg.get('premium', 0) or 0):.2%}"),
                                      qty=qty, price=cp,
                                      sell_price=_bb_dg.get("sell_price"),
                                      premium=_bb_dg.get("premium"))
                    except Exception:
                        pass
            except Exception as e:
                print(f"[{now:%H:%M:%S}] BUY {code} 失败: {e}")

        elif sig.action in ("SELL_HIGH", "PANIC_SELL", "TRAIL_SELL", "TREND_EXIT", "TARGET_SELL", "HARD_STOP_EXIT"):
            # T4: 仲裁器统一处理（地板 + 阈值 + sizer + 下单 + 审计）——sell_channels._sell_arbiter
            sell_channels._sell_arbiter(context, code, sig, pos_qty, cp, now, holding,
                                        threshold, stock_params, gm_sym)


# WP-A1: 买向拒单对称回滚哨兵——用于区分"无快照（键缺失）"与"快照为 None（下单前无此条目）"
_MISSING = object()


def _pop_buy_snapshot(context, order, symbol):
    """WP-A1: 按 委托id(cl_ord_id/order_id)→symbol 顺序弹出买向快照。

    下单侧以 order_volume 返回值键控（无返回值时回退 symbol），回报侧可能有
    cl_ord_id/order_id 差异，按序尝试；均未命中返回 _MISSING（无快照）。"""
    pbs = getattr(context, "_pending_buy_snapshot", None) or {}
    for _k in (order.get("cl_ord_id"), order.get("order_id"), symbol):
        if _k and _k in pbs:
            return pbs.pop(_k)
    return _MISSING


def on_order_status(context, order):
    symbol = order["symbol"]
    status = order["status"]
    volume = order["volume"]
    code = _raw_code(symbol)  # 提前到price兜底之前
    # ①-1/F6: 成交价优先 filled_vwap —— 掘金市价单 order["price"] 携带涨跌停保护价
    # (2026-07-29 C1: 600481卖出真实成交4.01被记为跌停价3.52；买入路径会用此价计算cost，错误价会毒化成本)
    price = order.get("filled_vwap") or order.get("vwap") or order.get("price") or 0
    if price <= 0:
        price = context.latest_pre_close.get(code, 0)
    side = order["side"]

    # F9: 在途卖单释放（成交/拒单/撤单/过期均归还额度）
    if side == 2 and status in (3, 4, 5, 6, 8, 12):
        _ifl = getattr(context, "_inflight_sell", None)
        if _ifl and symbol in _ifl:
            _ifl[symbol] = max(0, int(_ifl[symbol]) - int(volume))

    if status == 3:  # 全部成交
        # WP-B15: 持仓变化（成交）→ 解除信号 mute / 地板去重键（单点清理，防解封后忘清键）
        _clear_signal_mute_keys(context, code)
        _side = "BUY" if side == 1 else "SELL"
        # O-06(2026-08-11 复盘①轻)：台账在本回调内尚未更新（更新在下方），
        # SELL 分支直接读台账得到的是成交前持仓（0811 实战：卖 200 后 pos_after 仍报 1400）。
        _pre_qty = int(context.executed_orders.get(symbol, {}).get("qty", 0))
        _pos_after = max(0, _pre_qty - volume) if _side == "SELL" else _pre_qty + volume
        try:
            write_fill(str(datetime.now()), _raw_code(symbol), _side, volume, price,
                       pos_after=_pos_after)
        except Exception:
            pass
        # P0-2: 成交回调接线冷却（只有真的成交了才计冷却，避免下单即计）
        # WP-B07: 捕获返回值——卖成交建回补记忆(armed) / 买成交清记忆(buyback_filled)
        _rta = None
        if code in STOCKS:
            _action = 'BUY_LOW' if side == 1 else 'SELL_HIGH'
            _rta = context.engine.record_trade_action(code, _action, volume, price)
            # WP-B19 f: HARD_STOP_EXIT 硬止损离场不生成回补记忆（破位不回头；
            # record_trade_action 以 SELL_HIGH 记 arm，此处按真实通道清除）
            if side == 2 and getattr(context, "_pending_sell_action", {}).get(symbol, ("", 0))[0] == "HARD_STOP_EXIT":
                context.engine.awaiting_buyback.pop(code, None)
                _rta = None
        _rta = _rta or {}
        if side == 1:  # 买入
            # WP-A1: 成交即真实，快照使命结束（快照仅服务"纯拒单"场景）
            _pop_buy_snapshot(context, order, symbol)
            old = context.executed_orders.get(symbol, {"qty": 0, "available": 0, "cost": price})
            old_qty = int(old.get("qty", 0))
            old_cost = float(old.get("cost", price))
            new_qty = old_qty + volume
            new_cost = (old_cost * old_qty + price * volume) / new_qty if new_qty > 0 else price
            context.executed_orders[symbol] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": new_qty,
                # N25-2: 当日买入不解锁(T+1), available保持旧值
                "available": int(old.get("available", 0)),
                "t_qty": new_qty,
                "cost": new_cost,
                "type": "stock",
                "pre_close": price,
            }
            # WP-B07: 买入成交 → 回补闭环完成，清除记忆并写事件
            _bb_filled = _rta.get("buyback_filled")
            if _bb_filled:
                try:
                    # O-06(2026-08-11 复盘①轻)：qty 记 armed 匹配量（sell_qty），
                    # 而非整笔买入成交量（0811 实战：armed 200，事件误报 qty=2500）。
                    _matched = int(_bb_filled.get("sell_qty") or 0)
                    write_buyback(str(getattr(context, "now", None) or datetime.now()),
                                  code, "filled",
                                  detail=(f"sell={_bb_filled.get('sell_price')} "
                                          f"buyback={price:.2f} matched={_matched} fill={volume}"),
                                  sell_price=_bb_filled.get("sell_price"),
                                  price=price, qty=(_matched or volume), fill_qty=volume,
                                  sell_action=_bb_filled.get("sell_action", ""))
                except Exception:
                    pass
            # 底仓确认（含F7回补单：已settled标的回补成交同样同步台账并释放_base_ordered）
            if code in context._base_ordered:
                context._base_settled.add(code)
                # F12: 同步台账时保留做T状态键——直接整体替换会清空
                # _target_filled_l1/_trail_state/_trail_peak，导致同一持仓期内
                # TARGET 同档重复触发、TRAIL 状态机重置（WP-B 回放包 fix3 实证）
                _keep = {k: v for k, v in context.manual_position.get(symbol, {}).items()
                         if k.startswith("_target_") or k.startswith("_trail_")}
                context.manual_position[symbol] = dict(context.executed_orders[symbol], **_keep)
                # 底仓参考量=镜像目标值（供 sizer/sell_floor/tail 使用）
                setattr(context, f'_base_ref_{code}',
                        int(MIRROR_HOLDINGS.get(code, {}).get("qty", 0) or volume))
                context._base_ordered.discard(code)
                print(f"[BASE] {code} 底仓成交 {volume}股@{price:.2f}")
        elif side == 2:  # 卖出
            old = context.executed_orders.get(symbol, {"qty": 0, "available": 0})
            old_qty = int(old.get("qty", 0))
            old_cost = old.get("cost", price)
            new_qty = max(0, old_qty - volume)
            # cost 保持不变（买入成本），不覆写为卖出价
            context.executed_orders[symbol] = {
                "name": STOCK_NAMES.get(code, code),
                "qty": new_qty,
                "available": new_qty,
                "t_qty": new_qty,
                "cost": old_cost,
                "type": "stock",
                "pre_close": price,
            }
            # N26+N28: 成交时写入审计(含通道信息)
            _act, _sc = getattr(context, "_pending_sell_action", {}).pop(symbol, ("", 0))
            # WP-B回放包: 回测下用仿真时钟(context.now)，否则验收无法对齐窗口日期
            _ts_now = str(getattr(context, "now", None) or datetime.now())
            _audit_write({"event": "sell", "code": code, "qty": volume, "price": price,
                          "time": _ts_now, "pos_after_sell": new_qty,
                          "action": _act, "score": _sc})
            # WP-B14: TARGET 成交 → 置 filled 落盘（真实落袋才封档）
            if _act == "TARGET_SELL" and symbol in context.manual_position:
                context.manual_position[symbol]["_target_l1_state"] = "filled"
                _sell_state_persist(context, _raw_code(symbol), symbol)
            # WP-B07: 卖出成交 → 建立回补价格记忆并写事件（通道名以 _pending_sell_action 为准）
            _bb_armed = _rta.get("armed") or getattr(context.engine, "awaiting_buyback", {}).get(code)
            if _bb_armed:
                _bb_armed["sell_action"] = _act or _bb_armed.get("sell_action", "SELL_HIGH")
                try:
                    write_buyback(_ts_now, code, "armed",
                                  detail=(f"sell={_bb_armed.get('sell_price')} qty={volume} "
                                          f"action={_bb_armed.get('sell_action')} "
                                          f"target={_bb_armed.get('target_price')}"),
                                  sell_price=_bb_armed.get("sell_price"),
                                  qty=volume,
                                  sell_action=_bb_armed.get("sell_action"),
                                  target_price=_bb_armed.get("target_price"))
                except Exception:
                    pass
        # O-10(2026-08-17 复盘①轻)：成交回调同步刷新 sell_state 指纹（pos_key）。
        # 活跃 TRAIL/TARGET 状态期间成交会使 qty/cost 变化，但状态字段不变、
        # 不触发落盘 → 次日 INIT pos_key 校验不符，活跃状态被静默作废
        # （0817 实锤：603667 买 200 后文件指纹仍 400@51.9962，0818 将误作废 ARMED）。
        # 只刷指纹不动状态：persist 镜像的内存状态字段在此刻均未变化。
        if symbol in (getattr(context, "manual_position", None) or {}):
            try:
                _sell_state_persist(context, code, symbol)
            except Exception:
                pass
    elif status == 2 and side == 1:
        # WP-A1: 部分成交亦真实——部分成交量按实计，快照仅服务"纯拒单"场景，此处丢弃；
        # 防止随后剩余量被拒时误按整笔回滚
        _pop_buy_snapshot(context, order, symbol)
    elif status in (4, 5, 6, 8, 12):  # 拒单/撤单/待撤/已拒绝(8)/已过期(12) —— F2修复: 2026-07-28前漏掉8导致所有拒单静默
        _rej_detail = ""
        try:
            _rej_detail = order.get("ord_rej_reason_detail", "") or ""
        except Exception:
            pass
        context.rejected_order_count = getattr(context, 'rejected_order_count', 0) + 1
        # N5: 底仓拒单恢复
        # WP-A1: _is_base_reject 须在 N5 的 discard 之前求值——N5 会把 code 移出
        # _base_ordered 允许重发，若在其后再判 `code not in _base_ordered` 会误把底仓
        # 拒单当做T买入拒单回滚（T-A1 实证：1400 条目被兜底逆减误删）
        _is_base_reject = code in getattr(context, '_base_ordered', set())
        if _is_base_reject:
            if not hasattr(context, '_base_retry_count'):
                context._base_retry_count = {}
            retry = context._base_retry_count.get(code, 0) + 1
            context._base_retry_count[code] = retry
            if retry <= MAX_BASE_RETRY:
                context._base_ordered.discard(code)
                print(f'[ORDER] {code} 底仓拒单 status={status} 重试 #{retry} {_rej_detail}')
            else:
                print(f'[ORDER] {code} 底仓拒单已达上限({MAX_BASE_RETRY})，停止重试')
        print(f"[ORDER] {symbol} 被拒 status={status} {_rej_detail}")
        # 2026-08-31: 卖单拒单退避——拒单回滚后本地状态复原，若不记冷却，保护通道
        # （HARD_STOP 无冷却检查）会下一分钟同单重发形成订单风暴（run6 实证 2524 次
        # status=8 仓位不足）。用仿真时钟(_now)，回测压缩时间下仍按行情时间计 30 分钟。
        if side == 2:
            if not hasattr(context, "_protect_sell_reject_until") or context._protect_sell_reject_until is None:
                context._protect_sell_reject_until = {}
            context._protect_sell_reject_until[code] = _now() + timedelta(minutes=30)
        # N25-2: 卖出拒单回滚manual_position(下单时已虚减)
        if side == 2 and symbol in context.manual_position:
            # WP-B15: 持仓回滚 → 解除信号 mute / 地板去重键
            _clear_signal_mute_keys(context, code)
            mp = context.manual_position[symbol]
            mp["qty"] = mp.get("qty", 0) + volume
            mp["available"] = mp.get("available", 0) + volume
            mp["t_qty"] = mp.get("t_qty", 0) + volume
            _audit_write({"event": "sell_rollback", "code": code, "qty": volume,
                          "time": str(getattr(context, "now", None) or datetime.now())})
            # WP-B14: TARGET 拒单 → 状态清回 None 落盘（拒单不耗档，条件满足后可再触发）
            _pending_act = getattr(context, "_pending_sell_action", {}).get(symbol, ("", 0))[0]
            if _pending_act == "TARGET_SELL":
                mp["_target_l1_state"] = None
                _sell_state_persist(context, _raw_code(symbol), symbol)
            # F14: 拒单不消耗日卖出配额/总成交计数（防止误耗挤占信号通道）
            if hasattr(context, "daily_sell_count") and context.daily_sell_count is not None:
                context.daily_sell_count[code] = max(0, context.daily_sell_count.get(code, 0) - 1)
            if hasattr(context, "total_trade_count"):
                context.total_trade_count = max(0, context.total_trade_count - 1)
            # OBS-1(WP-A1): _pending_sell_action 拒单残留顺手清理——卖成交分支(:2135)才读该键，
            # 残留无下游影响，但避免同票新卖单覆盖语义歧义
            getattr(context, "_pending_sell_action", {}).pop(symbol, None)
        # WP-A1: 做T买入拒单对称回滚（底仓 BASE 走 N5 重试路径，不碰 manual_position，排除）
        elif side == 1 and not _is_base_reject:
            _snap = _pop_buy_snapshot(context, order, symbol)
            _fb = 0
            if _snap is _MISSING:
                # 无快照兜底：按 volume 逆减 qty/t_qty，结果 ≤0 删除条目（进程内遗留/版本热切换防御）
                _fb = 1
                if symbol in context.manual_position:
                    _mp = context.manual_position[symbol]
                    _nq = int(_mp.get("qty", 0)) - int(volume)
                    if _nq <= 0:
                        context.manual_position.pop(symbol, None)
                    else:
                        _mp["qty"] = _nq
                        _mp["t_qty"] = _nq
            elif _snap is None:
                # 下单前无 manual_position 条目 → 整条删除
                context.manual_position.pop(symbol, None)
            else:
                # 有快照：deepcopy 恢复（_pending_buy_snapshot 存的即为 deepcopy 副本）
                context.manual_position[symbol] = copy.deepcopy(_snap)
            # F14 买向对称：拒单不消耗日买入配额/总成交计数（下限 0）
            if hasattr(context, "daily_buy_count") and context.daily_buy_count is not None:
                context.daily_buy_count[code] = max(0, context.daily_buy_count.get(code, 0) - 1)
            if hasattr(context, "total_trade_count"):
                context.total_trade_count = max(0, context.total_trade_count - 1)
            if hasattr(context, "engine") and hasattr(context.engine, "buy_count_per_stock"):
                context.engine.buy_count_per_stock[code] = context.daily_buy_count.get(code, 0)
            _audit_write({"event": "buy_rollback", "code": code, "qty": volume,
                          "time": str(getattr(context, "now", None) or datetime.now()),
                          "fallback": _fb})
        try:
            _r_code = _raw_code(symbol)
            _r_side = "BUY" if side == 1 else "SELL"
            write_reject(str(datetime.now()), _r_code, _r_side, volume,
                         reason=f"status={status} {_rej_detail}",
                         raw={"status": status, "side": side, "volume": volume,
                              "rej_detail": _rej_detail})
            write_risk(str(datetime.now()), "order_rejected",
                         f"{_r_side} {volume}股被拒 status={status} {_rej_detail}", code=_r_code)
        except Exception:
            pass


def on_backtest_finished(context, indicator):
    _audit_close()
    print("*" * 50)
    print("回测已完成")
    if isinstance(indicator, dict):
        for k, v in sorted(indicator.items()):
            try:
                print(f"  {k}: {v}")
            except Exception:
                print(f"  {k}: {v}")
    print(f"  手动统计: 成交笔数={getattr(context, 'total_trade_count', 0)}")
    print(f"  拒单笔数={getattr(context, 'rejected_order_count', 0)}")
    print("*" * 50)


if __name__ == "__main__":
    _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 合并方案 P0-2(2026-08-28): token 不再硬编码入库，统一走 utils/gm_token.py
    from utils.gm_token import load_token
    run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
        filename="gm_main.py", mode=MODE_LIVE,
        token=load_token())
