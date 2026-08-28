# -*- coding: utf-8 -*-
"""sell_state.py — auto 侧卖出状态跨日持久化（P4-1 迁入，逻辑与 goldminer main.py 一致）。

内存权威源 = manual_position 的 _target_l1_state/_trail_state/_trail_peak，
本文件只做镜像落盘，供跨日 INIT 恢复。仅 live 生效；回测模式跳过。
pos_key 持仓指纹校验逻辑保留（O-12 容差）。
"""
import json
import os
import sys

_GM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_gm")
if _GM_DIR not in sys.path:
    sys.path.insert(0, _GM_DIR)

from gm_bridge.writer import write_buyback  # noqa: E402

# P4 迁移：路径从 goldminer runtime/state/sell_state.json → superTrader t_io/state/auto_sell_state.json
# 注意：本文件位于 execution/auto/，需 3 级 dirname 到 superTrader 根
SELL_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                               "t_io", "state", "auto_sell_state.json")

# GM 命名空间由 gm_main 注入（MODE_LIVE/STOCKS/_audit_write），保持「唯一 import gm.api 在 gm_main」
GM = None


def _sell_state_load():
    """读取卖出状态文件；异常/缺失 fail-open 为空 dict（等同现状不持久化，不致劣化）。"""
    try:
        if not os.path.exists(SELL_STATE_PATH):
            return {}
        with open(SELL_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"[sell_state] 读取失败 fail-open: {e}")
        return {}


def _sell_state_save(state):
    try:
        os.makedirs(os.path.dirname(SELL_STATE_PATH), exist_ok=True)
        with open(SELL_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[sell_state] 写入失败: {e}")


def _pos_key(qty, cost):
    """持仓指纹：qty@cost（4 位小数），用于校验状态文件是否仍属于当前持仓期。"""
    return f"{int(qty or 0)}@{float(cost or 0):.4f}"


def _split_pos_key(pk):
    """O-12: 拆解析 pos_key 'qty@cost' → (qty, cost)。异常返回 (None, None)。"""
    try:
        q, c = str(pk).split("@")
        return int(float(q)), float(c)
    except Exception:
        return None, None


def _sell_state_persist(context, code, gm_sym):
    """把该票内存卖出状态镜像落盘。清仓(qty<=0)时作废该票状态段。"""
    from datetime import datetime
    try:
        _is_live = context.mode == GM.MODE_LIVE
    except Exception:
        _is_live = False
    if not _is_live:
        return
    mp = (getattr(context, "manual_position", None) or {}).get(gm_sym) or {}
    qty = int(mp.get("qty", 0) or 0)
    state = _sell_state_load()
    if qty <= 0:
        state.pop(code, None)
    else:
        # WP-B18: 回补记忆跨日镜像（同文件同生命周期；pos_key 复用 state 段指纹）
        _ab = (getattr(context.engine, "awaiting_buyback", {}) or {}).get(code)
        _buyback = None
        if _ab and int(_ab.get("sell_qty", 0) or 0) > 0:
            _buyback = {
                "sell_price": _ab.get("sell_price"),
                "sell_qty": _ab.get("sell_qty"),
                "sell_action": _ab.get("sell_action", ""),
                "target_price": _ab.get("target_price"),
                "sell_time": str(_ab.get("sell_time")),
                "expire_date": _ab.get("expire_date", ""),
            }
        state[code] = {
            "_target_l1_state": mp.get("_target_l1_state"),
            "_trail_state": mp.get("_trail_state", "INACTIVE"),
            "_trail_peak": mp.get("_trail_peak", 0.0),
            "pos_key": _pos_key(qty, float(mp.get("cost", 0) or 0)),
            "updated": str(getattr(context, "now", None) or datetime.now()),
            "_buyback": _buyback,
        }
    _sell_state_save(state)


def _sell_state_restore(context):
    """INIT 对账后执行：券商持仓 qty>0 且 pos_key 一致 → 恢复状态；否则作废。
    保守原则：状态存疑即重置——宁多触发一档，也不错杀新持仓期。
    pending 为进程中断遗留（下单后结果未知）→ 作废（防永久封档）。"""
    from datetime import datetime
    from utils.helpers import _now as _inj_now  # 时间注入（红线：引擎 API 不隐式读系统时钟；SIM_NOW 为空时=datetime.now，生产行为不变）
    try:
        _is_live = context.mode == GM.MODE_LIVE
    except Exception:
        _is_live = False
    if not _is_live:
        return
    state = _sell_state_load()
    if not state:
        return
    for code, st in list(state.items()):
        sym = GM.STOCKS.get(code)
        if not sym:
            continue
        mp = (getattr(context, "manual_position", None) or {}).get(sym) or {}
        qty = int(mp.get("qty", 0) or 0)
        cost = float(mp.get("cost", 0) or 0)
        if qty <= 0:
            state.pop(code, None)
            print(f"[INIT] {code} sell_state 作废: 已清仓")
            continue
        if st.get("pos_key") != _pos_key(qty, cost):
            # O-12: pos_key 容差匹配——数量一致且成本差 ≤ max(0.05元, 0.5%) → 视为匹配并
            # 静默更新指纹（吸收柜台 T+1 清算成本漂移，0818 案例 +0.002 元）；否则作废留痕
            _fq, _fc = _split_pos_key(st.get("pos_key"))
            _tol = 0.0
            if _fc and _fc > 0 and cost > 0 and int(_fq or 0) == int(qty or 0):
                _tol = max(0.05, 0.005 * max(_fc, cost))
            if _tol > 0 and abs(_fc - cost) <= _tol:
                st["pos_key"] = _pos_key(qty, cost)
                GM._audit_write({"event": "pos_key_tolerance", "code": code,
                              "file_cost": _fc, "now_cost": cost,
                              "diff": abs(_fc - cost), "tol": _tol,
                              "time": str(datetime.now())})
            else:
                state.pop(code, None)
                print(f"[INIT] {code} sell_state 作废: pos_key 不符 "
                      f"file={st.get('pos_key')} now={_pos_key(qty, cost)}")
                continue
        if sym not in context.manual_position:
            continue
        _l1 = st.get("_target_l1_state")
        if _l1 == "pending":
            print(f"[INIT] {code} sell_state pending 作废: 进程中断遗留，状态存疑即重置")
            _l1 = None
        context.manual_position[sym]["_target_l1_state"] = _l1
        context.manual_position[sym]["_trail_state"] = st.get("_trail_state", "INACTIVE")
        context.manual_position[sym]["_trail_peak"] = st.get("_trail_peak", 0.0)
        print(f"[INIT] {code} sell_state 恢复: l1={_l1} trail={st.get('_trail_state')} "
              f"peak={st.get('_trail_peak')}")
        # WP-B18: 回补记忆跨日恢复（M0 pos_key 已由上方校验；此处 M0b 有效期校验）
        _bb = st.get("_buyback") or {}
        if _bb and _bb.get("sell_price"):
            _exp = str(_bb.get("expire_date", "") or "")
            _today = _inj_now().strftime("%Y-%m-%d")  # 2026-08-28：系统时钟→注入时钟（b18 跨日用例此前依赖真实日期，时间敏感）
            if _exp and _today > _exp:
                state[code]["_buyback"] = None
                GM._audit_write({"event": "buyback_expired", "code": code,
                              "sell_price": _bb.get("sell_price"),
                              "expire_date": _exp, "time": str(datetime.now())})
                try:
                    write_buyback(str(datetime.now()), code, "expired",
                                  detail=(f"sell={_bb.get('sell_price')} "
                                          f"expire={_exp} 跨日有效期已过"),
                                  sell_price=_bb.get("sell_price"),
                                  expire_date=_exp)
                except Exception:
                    pass
                print(f"[INIT] {code} buyback 作废: 有效期已过(expire={_exp})")
            else:
                # WP-B18 M4: 恢复时深亏背景(< -8% PANIC 域)不回补（接回高抛≠摊平亏损）
                _val_px = 0.0
                _bb_rows = (getattr(context, "bar_cache", None) or {}).get(sym)
                if _bb_rows:
                    try:
                        _val_px = float(_bb_rows[-1].get("close", 0) or 0)
                    except Exception:
                        _val_px = 0.0
                if _val_px <= 0:
                    _val_px = float(mp.get("pre_close", 0) or 0) or cost
                if cost > 0 and _val_px > 0 and (_val_px - cost) / cost < -0.08:
                    state[code]["_buyback"] = None
                    GM._audit_write({"event": "buyback_blocked", "code": code, "rule": "M4",
                                  "sell_price": _bb.get("sell_price"),
                                  "cost": cost, "val_px": _val_px, "time": str(datetime.now())})
                    try:
                        write_buyback(str(datetime.now()), code, "blocked",
                                      detail=(f"rule=M4 恢复时深亏背景不回补 "
                                              f"cost={cost} val={_val_px:.2f} "
                                              f"profit={(_val_px - cost)/cost:.1%}"),
                                      rule="M4", sell_price=_bb.get("sell_price"))
                    except Exception:
                        pass
                    print(f"[INIT] {code} buyback 作废: 深亏背景(PANIC 域)不回补")
                else:
                    _st = str(_bb.get("sell_time", "") or "")
                    try:
                        _st_dt = datetime.fromisoformat(_st) if _st else datetime.now()
                    except Exception:
                        _st_dt = datetime.now()
                    context.engine.awaiting_buyback[code] = {
                        "sell_price": float(_bb.get("sell_price", 0) or 0),
                        "sell_qty": int(_bb.get("sell_qty", 0) or 0),
                        "sell_action": _bb.get("sell_action", "SELL_HIGH"),
                        "target_price": float(_bb.get("target_price", 0) or 0),
                        "sell_time": _st_dt,
                        "expire_date": _exp,
                        "persisted": True,
                    }
                    GM._audit_write({"event": "buyback_restored", "code": code,
                                  "sell_price": _bb.get("sell_price"),
                                  "target_price": _bb.get("target_price"),
                                  "expire_date": _exp, "time": str(datetime.now())})
                    try:
                        write_buyback(str(datetime.now()), code, "restored",
                                      detail=(f"sell={_bb.get('sell_price')} "
                                              f"qty={_bb.get('sell_qty')} "
                                              f"target={_bb.get('target_price')} "
                                              f"expire={_exp}"),
                                      sell_price=_bb.get("sell_price"),
                                      qty=_bb.get("sell_qty"),
                                      target_price=_bb.get("target_price"),
                                      expire_date=_exp)
                    except Exception:
                        pass
                    print(f"[INIT] {code} buyback 恢复: sell={_bb.get('sell_price')} "
                          f"target={_bb.get('target_price')} expire={_exp}")
    _sell_state_save(state)
