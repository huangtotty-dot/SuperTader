# coding=utf-8
"""
gm_bridge/writer.py — 事件桥写入（仅写文件，不碰网络/飞书）

事件 schema 见 docs/模拟盘实施方案_20260727.md 附录 A：
  signal: 信号生成
  order:  委托发出
  fill:   全部成交
  reject: 拒单/撤单
  risk:   风控事件 (仓位/地板/PANIC/熔断/急停)
  heartbeat: 每分钟心跳（仓位+现金）
"""

import json
import os
import time
from datetime import datetime
from typing import Dict, Any, Optional

# ── 桥目录配置 ──
# P4-2 迁移：事件桥从 goldminer runtime/bridge → superTrader t_io/bridge（schema 不变）。
# GM_BRIDGE_DIR 环境变量仍可覆盖（回放/校验场景隔离用）。
# 旧冻结历史（2026-07-27 ~ 08-06）留存在 superTrader t_io/gm_bridge，不动。
_ST_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))

def _bridge_dir() -> str:
    env = (os.environ.get("GM_BRIDGE_DIR") or "").strip()
    d = env if env else os.path.join(_ST_ROOT, "t_io", "bridge")
    os.makedirs(d, exist_ok=True)
    return d

BRIDGE_DIR = _bridge_dir()


def _events_path(date_str: str = None) -> str:
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(BRIDGE_DIR, f"events_{date_str}.jsonl")


def _heartbeat_path() -> str:
    return os.path.join(BRIDGE_DIR, "heartbeat.json")


def _kill_switch_path() -> str:
    return os.path.join(BRIDGE_DIR, "KILL_SWITCH")


def _snapshot_path(date_str: str = None) -> str:
    if date_str is None:
        date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(BRIDGE_DIR, f"signals_{date_str}.jsonl")


def _buy_pending_path() -> str:
    """待人工确认买入请求（引擎单写者 / GUI 只读）。整文件原子覆写。"""
    return os.path.join(BRIDGE_DIR, "BUY_PENDING.json")


def _buy_decision_path() -> str:
    """用户确认/拒绝回复（GUI 单写者 / 引擎只读）。整文件原子覆写。"""
    return os.path.join(BRIDGE_DIR, "BUY_DECISION.json")


def _auto_build_path() -> str:
    """人工建仓/加仓武装标记（GUI 写 / 引擎读+消费，一次性 one-shot）。整文件原子覆写。"""
    return os.path.join(BRIDGE_DIR, "AUTO_BUILD.json")


# ── 写入工具 ──

def _append_jsonl(path: str, rec: dict):
    """安全追加一行 JSON"""
    try:
        rec["_ts"] = time.time()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _write_json(path: str, data):
    """整文件覆写（heartbeat 用）"""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _write_json_atomic(path: str, data):
    """整文件原子覆写（tmp+replace），GUI 读侧看不到半写状态。"""
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception:
        pass


# ── 公开 API：事件写入 ──

def write_signal(time_str: str, code: str, action: str, score: float,
                 reasons: list = None, pos_qty: int = 0):
    """信号生成事件"""
    _append_jsonl(_events_path(), {
        "event": "signal",
        "time": time_str,
        "code": code,
        "action": action,
        "score": score,
        "reasons": reasons or [],
        "pos_qty": pos_qty,
    })


def write_order(time_str: str, code: str, side: str, qty: int, price: float,
                order_id: str = "", order_type: str = "MKT"):
    """委托发出事件"""
    _append_jsonl(_events_path(), {
        "event": "order",
        "time": time_str,
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "order_id": str(order_id),
        "order_type": order_type,
    })


def write_fill(time_str: str, code: str, side: str, qty: int, price: float,
               order_id: str = "", pos_after: int = 0, fee: float = 0.0, fee_source: str = "estimated",
               fee_rate: Optional[float] = None, order_price: Optional[float] = None,
               fill_vwap: Optional[float] = None, slippage: Optional[float] = None):
    """全部成交事件。F-9/Q-20260911: 落 fee + fee_source（gm=实收 filled_commission / estimated=费率估算），
    全周费用可机读核对（N11 防线费率=0.00015）。

    2026-09-15 阶段0-5（诊断D3）：成本归因实验铺路，新增 4 个可空字段（新增不删旧，向后兼容）：
      fee_rate    费用率 = fee / (price*qty)（仅当 fee>0 且成交额>0 时由真实值推导，否则 null）
      order_price 委托价（gm SDK Order.price / ExecRpt.price；市价单为涨跌停保护价，注意口径）
      fill_vwap   成交均价（gm SDK Order.filled_vwap，调用侧现有兜底链 filled_vwap→vwap→price）
      slippage    滑点 = fill_vwap - order_price（仅当两者均传入时推导；BUY 正=买贵不利，
                  SELL 负=卖贱不利；符号为原始价差，方向解释归下游）
    当前唯一调用侧 gm_main.py:2580 未传新参数 → 实盘暂落 null（禁止编造；
    接线 gm_main 为建议项，见阶段0-5施工报告）。数据源备查：order.get("filled_vwap") /
    order.get("price") / order.get("filled_commission")（gm_main.py:2530/2574）。"""
    # 仅由传入的真实值推导，缺失保持 None（不落估算假值）
    _amount = float(price or 0) * int(qty or 0)
    if fee_rate is None and fee and _amount > 0:
        fee_rate = round(float(fee) / _amount, 6)
    if slippage is None and order_price is not None and fill_vwap is not None:
        slippage = round(float(fill_vwap) - float(order_price), 4)
    _append_jsonl(_events_path(), {
        "event": "fill",
        "time": time_str,
        "code": code,
        "side": side,
        "qty": qty,
        "price": price,
        "order_id": str(order_id),
        "pos_after": pos_after,
        "fee": fee,
        "fee_source": fee_source,
        "fee_rate": fee_rate,
        "order_price": order_price,
        "fill_vwap": fill_vwap,
        "slippage": slippage,
    })


def write_reject(time_str: str, code: str, side: str, qty: int,
                 reason: str = "", raw: dict = None):
    """拒单/撤单事件"""
    _append_jsonl(_events_path(), {
        "event": "reject",
        "time": time_str,
        "code": code,
        "side": side,
        "qty": qty,
        "reason": reason,
        "raw": str(raw) if raw else "",
    })


def write_risk(time_str: str, kind: str, detail: str = "", code: str = ""):
    """风控事件（仓位拦截/地板保护/PANIC/熔断/急停）"""
    _append_jsonl(_events_path(), {
        "event": "risk",
        "time": time_str,
        "code": code,
        "kind": kind,
        "detail": detail,
    })


def write_buyback(time_str: str, code: str, kind: str, detail: str = "", **kw):
    """WP-B07 回补价格记忆事件：
    kind ∈ armed(记忆建立) / delayed(高接延迟) / downgrade(降档成交) / filled(回补完成清除)
    事件名为 buyback_<kind>（snake_case），与 write_risk/write_signal 同文件同风格。"""
    rec = {
        "event": f"buyback_{kind}",
        "time": time_str,
        "code": code,
        "detail": detail,
    }
    rec.update(kw)
    _append_jsonl(_events_path(), rec)


def write_heartbeat(time_str: str, bar: str, positions: Dict[str, Any],
                    cash: float = 0.0, index_regime: str = "", index_score: float = 0.0):
    """每分钟心跳（同时写实时覆盖文件 + 追加历史jsonl）"""
    rec = {
        "event": "heartbeat",
        "time": time_str,
        "bar": bar,
        "positions": positions,
        "cash": cash,
        "index_regime": index_regime,
        "index_score": index_score,
    }
    _write_json(_heartbeat_path(), rec)
    # L3: 追加历史时序快照(不覆盖)
    _append_jsonl(os.path.join(BRIDGE_DIR, f"heartbeat_{time_str[:10]}.jsonl"), rec)


def write_snapshot(time_str: str, code: str, price: float, bar: str = "",
                   buy_score=None, sell_score=None, gate: str = "",
                   gate_detail: str = "", action: str = "", pos_qty: int = 0):
    """全票每 bar 决策快照（0806 红日整改）：
    16 票 × 每 bar 一条 → signals_YYYYMMDD.jsonl。
    回放"策略活着会不会有卖点 / 改阈值会怎样"类问题的数据底座。
    纯监控产物，不参与决策；仅 MODE_LIVE 调用（回测省 I/O）。"""
    rec = {
        "event": "snapshot",
        "time": time_str,
        "bar": bar,
        "code": code,
        "price": price,
        "pos_qty": pos_qty,
        "gate": gate,
    }
    if buy_score is not None:
        rec["buy_score"] = round(float(buy_score), 1)
    if sell_score is not None:
        rec["sell_score"] = round(float(sell_score), 1)
    if gate_detail:
        rec["gate_detail"] = gate_detail[:120]
    if action:
        rec["action"] = action
    _append_jsonl(_snapshot_path(), rec)


# ── 公开 API：风控文件 ──

def check_kill_switch() -> bool:
    """检查 KILL_SWITCH 文件是否存在。存在 → 返回 True（禁止新开仓）"""
    try:
        return os.path.exists(_kill_switch_path())
    except Exception:
        return False


# ── 公开 API：人工确认闸（2026-08-30 建仓/加仓人工把关） ──

def write_buy_pending(rec: dict):
    """写待人工确认买入请求（BUY_PENDING.json，引擎单写者）。整文件原子覆写。
    rec 结构 {date, updated_at, rejected_today[], pending{code: request}}。"""
    _write_json_atomic(_buy_pending_path(), rec)


def read_buy_pending() -> dict:
    """读待人工确认买入请求。异常/不存在 → {}。"""
    try:
        with open(_buy_pending_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def read_buy_decision() -> dict:
    """读用户确认/拒绝回复（BUY_DECISION.json）。异常/不存在 → {}。"""
    try:
        with open(_buy_decision_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_buy_decision(rec: dict):
    """F3(2026-09-09): 写/清空用户确认回复（BUY_DECISION.json，引擎单写者，整文件原子覆写）。
    跨日陈旧 pending 作废时以 {} 清空，避免 GUI 幽灵待确认。"""
    _write_json_atomic(_buy_decision_path(), rec)


def write_confirm(time_str: str, code: str, state: str, detail: str = "", **kw):
    """人工确认闸事件（追加进既有 events 流，引擎是 events 唯一写者）：
    state ∈ request / approved / rejected / expired / blocked。事件名 buy_confirm_<state>。"""
    rec = {
        "event": f"buy_confirm_{state}",
        "time": time_str,
        "code": code,
        "detail": detail,
    }
    rec.update(kw)
    _append_jsonl(_events_path(), rec)


# ── 公开 API：人工建仓/加仓武装标记（2026-08-30 手动建仓→引擎做T衔接） ──

def read_auto_build() -> dict:
    """读人工建仓武装标记 AUTO_BUILD.json。异常/不存在 → {}。
    结构 {updated_at, requests: {code: {action: build|add, qty, ts}}}。"""
    try:
        with open(_auto_build_path(), encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def write_auto_build(data: dict):
    """整文件原子写 AUTO_BUILD.json（GUI 与引擎共用；单写者纪律=GUI 写、引擎消费时重写）。"""
    _write_json_atomic(_auto_build_path(), data)


def consume_auto_build(code: str):
    """消费（删除）某 code 的武装标记并原子重写。返回删除的请求 dict 或 None。"""
    data = read_auto_build()
    req = (data.get("requests") or {}).pop(code, None)
    if req is not None:
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_auto_build(data)
    return req


# ── 2026-09-15 阶段0-4（诊断D3）：backtrace run_type 分流 ──
# 背景：t_io/logs/auto_backtrace.jsonl 74,887 行中 91% 为回放/回测 run 与实盘混存。
# 写入链事实（施工核查）：backtrace 唯一写入点是 gm_main._audit_write（gm_main.py:1133，
# 主链 gmcache/backtrace.jsonl + 镜像 _AUDIT_MIRROR_PATH=t_io/logs/auto_backtrace.jsonl），
# 不经过本模块；gm_main.py 本阶段禁改，故实盘侧无法在记录上补 run_type=live。
# 约定：审计记录「run_type 字段缺失 ⇒ live」（下游按 run_type.isnull()|=="live" 过滤实盘）；
# 回放/回测入口（replay_verify.py / backtest_holdings.py，非本施工员专属文件）调用
# install_audit_run_type(gm_main, "replay"/"backtest") 即可在 writer 层完成打标，无需改 gm_main。

RUN_TYPE_LIVE = "live"           # 实盘（缺省；实盘记录不落字段，按缺失=live 过滤）
RUN_TYPE_REPLAY = "replay"       # 通用回放（scripts/replay_renko_t.py 等）
RUN_TYPE_REPLAY_DAY = "replay_day"   # 单日回放（scripts/replay_day.py）
RUN_TYPE_BACKTEST = "backtest"   # 回测（execution/auto/backtest_holdings.py 等）


def stamp_run_type(rec: dict, run_type: str = RUN_TYPE_LIVE) -> dict:
    """为一条审计/事件记录补 run_type 字段（已存在则不覆盖）。返回 rec 本身（就地修改）。"""
    try:
        rec.setdefault("run_type", run_type or RUN_TYPE_LIVE)
    except Exception:
        pass
    return rec


def install_audit_run_type(gm_module, run_type: str):
    """包装 gm_main._audit_write：此后该进程全部审计记录自动打 run_type（setdefault，不覆盖显式值）。
    仅供回放/回测入口脚本在 import gm_main 之后、run() 之前调用一次；实盘进程不调用，
    实盘记录保持无 run_type（=live 缺省约定）。重复调用安全（后调用的 run_type 生效于外层）。"""
    try:
        _orig = gm_module._audit_write
        if getattr(_orig, "_run_type_wrapped", False):
            _orig = _orig._run_type_orig  # 重复安装时解到最里层再包
        def _wrapped(rec, _o=_orig, _rt=run_type):
            try:
                rec.setdefault("run_type", _rt)
            except Exception:
                pass
            return _o(rec)
        _wrapped._run_type_wrapped = True
        _wrapped._run_type_orig = _orig
        gm_module._audit_write = _wrapped
        return True
    except Exception:
        return False
