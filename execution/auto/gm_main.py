# coding=utf-8
"""
gm_main.py — 掘金量化策略入口（P4-1 迁入自 goldminer main.py，v1.1.0 WIP）

⚠️ 2026-10-12 owner 决策「超跌跳空确认接管 / 旧引擎删干净」后的**唯一策略链**：
  超跌跳空确认（超跌跳空确认）10:00 买侧 / 10:00 早盘冲高卖 / 14:50 止损与到期。
  旧镜像做T引擎（BASE 建仓/信号/卖出门链 + sell_channels/sell_state/t_engine_auto）、
  开盘强制对齐（OPEN_ALIGN）、开盘低开反转（OGR）**均已整链删除**，勿照旧文档复原。
"""

from __future__ import print_function, absolute_import, division
from gm.api import *
import pandas as pd
from datetime import datetime, time as dtime
import os
import sys
import json

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# P4-1: 支撑模块整体副本在 _gm/（goldminer 内部 import 相对路径不变，仅指向 _gm）。
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)
_GM_DIR = os.path.join(PROJECT_DIR, "_gm")
if _GM_DIR not in sys.path:
    sys.path.insert(0, _GM_DIR)

# 仓根：让 `core.*`（cost_model / market_data.facade …）可导入。
# ⚠️ 必须 **append 而不是 insert**：insert 会把仓根顶到 `_gm/` 之前，于是
#    `import analysis.index_regime` 解析到仓根副本 ⇒ init 的 fail-loud 守卫直接 raise
#    （2026-10-12 实测就是这么把策略搞起不来的）。append 则 `_gm/` 优先级不变。
# ⚠️ PROJECT_DIR = execution/auto ⇒ 仓根要再上溯一层（dirname 两次）
_ROOT_DIR = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(os.path.dirname(PROJECT_DIR))
if _ROOT_DIR not in sys.path:
    sys.path.append(_ROOT_DIR)

from config.params import PARAMS
from utils.helpers import _now
from gm_bridge.writer import (
    write_order, write_fill, write_reject, write_risk,
    write_heartbeat, check_kill_switch,
)
from gm_bridge import ops_guard

# 策略框架（2026-10-12）：多策略共存接缝层。framework/ 内**不得** import gm.api（仓规）⇒
# 掘金能力经 GmServices 注入；归属硬锁（一票一策略）在 framework/ownership.py。
from framework.ownership import Ownership, default_path as _ownership_path_fn
from framework.services import GmServices

# 唯一策略：超跌跳空确认（实现全在同名包内；本文件只做宿主与注入）
import oversold_gap_confirm as osc

# 策略账本目录（目录名由**策略包**声明：`osc.BOOK_DIRNAME` 刻意沿用历史名 weak_strong_book，
# 改名会让在途持仓与卖侧失联）。宿主用它做归属判定与 Runtime 注入。
_OSC_BOOK_DIR = os.path.join(
    os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader"),
    "t_io", "state", osc.BOOK_DIRNAME)

# F1(2026-09-09) 退出留痕：gm SDK 把所有退出接到 os._exit（无 traceback、不跑 atexit），
# 死亡只能二分定位——注册自己的 atexit（LIFO 先于 gm 逻辑执行）写 risk:strategy_exit + 横幅。
# 有 banner/事件 = 优雅退出路径；硬 kill(taskkill/终止) 无 banner → 由 watcher 心跳兜底告警。
import atexit as _atexit


def _gm_atexit_banner():
    try:
        _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        write_risk(_ts, "strategy_exit", "策略进程退出(atexit 优雅路径)")
        print(f"\n===== strategy exit atexit {_ts} (pid={os.getpid()}) =====", flush=True)
    except Exception:
        pass


_atexit.register(_gm_atexit_banner)


# ── P0-2 Fix B/C(2026-09-11 Q-20260911-2): fill 记账与下单解耦（轮询兜底版，低风险替代抽取） ──
def _mark_pending_recon(context, code, sym, side, qty, px, orders):
    """下单成功登记待对账项（Fix B 步1）。orders=order_volume 返回（List[Dict] 或单 dict）。"""
    try:
        if getattr(context, "mode", None) != MODE_LIVE:
            return   # 仅实盘需要对账；回测不登记（省内存/无轮询）
        _ids = []
        for _o in (orders if isinstance(orders, list) else [orders]):
            if isinstance(_o, dict):
                _i = _o.get("id") or _o.get("order_id")
                if _i:
                    _ids.append(_i)
        _rec = getattr(context, "_pending_recon", None)
        if _rec is None:
            context._pending_recon = {}
            _rec = context._pending_recon
        _rec[sym] = {"code": code, "side": side, "qty": int(qty or 0), "px": float(px or 0),
                     "ts_dt": datetime.now(), "order_ids": _ids, "closed": False}
    except Exception:
        pass


def _pending_recon_close(context, sym):
    try:
        _rec = getattr(context, "_pending_recon", None)
        if _rec and sym in _rec:
            _rec[sym]["closed"] = True
    except Exception:
        pass


def _poll_pending_recon(context, now):
    """Fix C: 回调失效轮询兜底。扫 _pending_recon，age≥90s 用 get_orders 查当日委托，
    status==3 → 合成 order 喂 on_order_status 补记（回调/轮询经 _fills_done 防重）。整段 fail-open。"""
    try:
        if context.mode != MODE_LIVE:
            return   # 回测无回调失效问题；轮询仅实盘需要，避免每 bar 多余 get_orders 拖慢/污染回放
        _prec = getattr(context, "_pending_recon", None)
        if not _prec:
            return
        for _sym, _rec in list(_prec.items()):
            try:
                if _rec.get("closed"):
                    _prec.pop(_sym, None)
                    continue
                _ts = _rec.get("ts_dt")
                if _ts and (now - _ts).total_seconds() < 90:
                    continue
                try:
                    _orders = _sdk_call("get_orders_poll", _partial(get_orders, symbol=_sym))
                except TypeError:
                    _orders = _sdk_call("get_orders_poll_all", get_orders)
                _hit = None
                for _o in (_orders or []):
                    try:
                        if int(_o.get("status") or 0) == 3 and int(_o.get("volume") or 0) > 0:
                            _hit = _o
                            break
                    except Exception:
                        continue
                if not _hit:
                    continue
                on_order_status(context, _hit)
                _rec["closed"] = True
                try:
                    write_risk(str(now), "fill_recovered_by_poll",
                               f"{_sym} 回调失效,轮询补记 fill qty={_hit.get('volume')}",
                               code=_rec.get("code", ""))
                except Exception:
                    pass
            except Exception:
                continue
    except Exception:
        pass


# P0-2 Fix A(2026-09-11 Q-20260911-2): gm SDK 同步直调（history_n/current/positions/account/order_volume）
# 无超时——挂死不返回也不抛，连坐 SDK 事件派发线程（09-11 14:50 on_bar 阻塞→fill 永久排队）。
# 统一套单 worker 线程池 + 15s 硬超时（略宽于数据侧 facade 12s），超时弃池重建抛 TimeoutError，
# 由各调用点既有 except 接住走拒单/告警/fail-open 路径。
import concurrent.futures as _cfmod
from functools import partial as _partial

_SDK_CALL_TIMEOUT = 15.0
_SDK_POOL = _cfmod.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-sdk")


def _sdk_call(desc, fn, *a, **k):
    """gm SDK 调用硬超时包装。注意：超时≠未成（单可能已报柜台）→ 须配合 Fix B 对账。"""
    global _SDK_POOL
    _fut = _SDK_POOL.submit(fn, *a, **k)
    try:
        return _fut.result(timeout=_SDK_CALL_TIMEOUT)
    except _cfmod.TimeoutError:
        try:
            _SDK_POOL.shutdown(wait=False)
        except Exception:
            pass
        _SDK_POOL = _cfmod.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-sdk")
        raise TimeoutError(f"gm SDK {desc} 超时>{_SDK_CALL_TIMEOUT}s（挂死，弃池）")

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


# ── 持仓真源回写（2026-09-14 并表）──────────────────────────────
def _load_holdings_repo():
    """经 SUPERTRADER_ROOT 加载 src/holdings_repo.py（该模块自述设计为 goldminer 可跨仓 import）。"""
    import importlib.util as _ilu
    root = os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader")
    path = os.path.join(root, "src", "holdings_repo.py")
    if not os.path.exists(path):
        return None
    _spec = _ilu.spec_from_file_location("holdings_repo", path)
    _m = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_m)
    return _m


_WB_DONE_DATE = None


def _writeback_holdings(context) -> int:
    """收盘后把**账户实际 qty/cost** 写回 superTrader 的 holdings.json（唯一持仓真源）。

    2026-09-14 并表：holdings.json 现承载 身份+实际持仓+目标底仓；本函数只同步"实际持仓"。
    **磁盘为基 + 只补丁 qty/cost**（沿用 Q-20260914-1 补丁语义）——绝不整写、绝不碰
    base（目标底仓）与 pre_close。只回写引擎**实际跟踪**的标的，未跟踪的跳过（不臆造）。
    返回回写票数。
    """
    repo = _load_holdings_repo()
    if repo is None:
        return 0
    disk = repo.load_auto()
    rev = {v: k for k, v in STOCKS.items()}          # gm_symbol → 6 位 code
    patch = {}
    for gm_sym, mp in (getattr(context, "manual_position", {}) or {}).items():
        code = rev.get(gm_sym)
        if not code or code not in disk:
            continue
        try:
            h = _get_holding(context, code, gm_sym)
        except Exception:
            continue
        e = dict(disk[code])                          # 磁盘新值全保留
        e["qty"] = int(h.get("qty", 0) or 0)          # 唯一允许写的字段
        e["cost"] = round(float(h.get("cost", 0) or 0), 4)
        patch[code] = e
    if not patch:
        return 0
    repo.save_auto(patch, actor="auto_eod", reason="eod_writeback_qty_cost")
    print(f"[WRITEBACK] 持仓真源已回写 {len(patch)} 票（qty/cost；base/pre_close 未动）")
    return len(patch)


# ── 唯一策略：超跌跳空确认 ────────────────────────────────────────────
# 策略实现（决策核/预筛/执行核/四时点钩子/下单通道）整体在
# `execution/auto/oversold_gap_confirm/` 包里；本文件只剩宿主职责：
#   gm 回调 · 共享服务（服务容器/归属锁/审计/心跳/回写/指数态势）· 经 Runtime 注入。
# 策略细节见该包 __init__ 与 doc/solutions/2026-10-12_超跌跳空确认_策略详解.md。


# 历史常量：模拟盘初始资金（仅作「账户 cash 读不到时」的兜底估值；不再参与任何预算计算——
# 旧引擎的预算表/个股预算已随其删除）。
INITIAL_CASH = 150000


def _raw_code(symbol: str) -> str:
    return symbol.replace("SHSE.", "").replace("SZSE.", "").replace("BJ.", "")


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
    # 2026-09-30 复盘实证：对账节流必须**按 symbol** 记账——原全局单时间戳使每个 30min
    # 窗口只有 STOCKS 序首那一票真正对账（09-29/09-30 的 reconcile_fix 全落在 588170），
    # 其余票的台账残影永远轮不到自愈（002451/603667 残留全天的直接原因之二）。
    _recs = getattr(context, "_last_position_reconcile", None)
    if not isinstance(_recs, dict):          # 兼容旧的全局 datetime 形态
        _recs = {}
        context._last_position_reconcile = _recs
    last_rec = _recs.get(gm_symbol)
    # F2: 模拟盘模式启用对账，回测模式跳过
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    _skip_reconcile = not _is_live
    if not _skip_reconcile and (last_rec is None or (now - last_rec).total_seconds() > reconcile_interval):
        _recs[gm_symbol] = now
        try:
            pos = _sdk_call("positions_reconcile",
                            lambda: context.account().positions(symbol=gm_symbol, side=PositionSide_Long))
            if pos and len(pos) > 0:
                p = pos[0]
                gm_pos = {
                    "name": STOCK_NAMES.get(code, code),
                    "qty": int(p.volume),
                    "available": int(p.available),
                    "t_qty": int(p.volume),
                    "cost": float(p.vwap or 0),
                    "type": "stock",
                    "pre_close": float(p.vwap or 0),   # 原「回退到昨收」的源随旧引擎删除
                }
                mp = context.manual_position.get(gm_symbol)
                if mp and abs(int(mp.get("qty", 0)) - int(p.volume)) > 0:
                    _my_cost = mp.get("cost", gm_pos["cost"])
                    context.manual_position[gm_symbol] = gm_pos
                    context.manual_position[gm_symbol]["cost"] = _my_cost
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


def _account_positions_all(context) -> dict:
    """全账户持仓快照 {gm_symbol: {qty, cost}}（**不按 STOCKS 池过滤**）。

    2026-10-13 修复：心跳与 [OWN] 归属诊断原先只逐票轮询 STOCKS 池内标的，
    池外真实持仓（旧引擎 10-12 留下的 605198×1800 / 688003×1700）被读成
    「账户空仓」（positions={} / 账户持有 0 只），而 cash 是全账户口径所以一直准。
    本函数与现金同一口径（`account().positions()` 无 symbol 过滤）。
    仅用于监控/诊断展示（心跳、[OWN] 启动行），不参与任何交易决策；
    失败静默返回 {}（调用方按原池内口径继续）。
    """
    out = {}
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    if not _is_live:
        return out
    try:
        poss = _sdk_call("positions_all", lambda: context.account().positions())
        for p in (poss or []):
            try:
                vol = int(getattr(p, "volume", 0) or 0)
                sym = str(getattr(p, "symbol", "") or "")
                if vol <= 0 or not sym:
                    continue
                out[sym] = {"qty": vol, "cost": float(getattr(p, "vwap", 0) or 0)}
            except Exception:
                continue
    except Exception as _e:
        print(f"[hb] 全账户持仓查询失败（按池内口径继续）: {_e}")
    return out


# ── 审计 JSONL ──
# P0-2(2026-08-31): backtrace 实际落盘 gmcache/backtrace.jsonl；复盘清单期待 t_io/logs/auto_backtrace.jsonl。
# 双写镜像到清单路径（口径修复，历史不回改；当日已 281 条在 gmcache，明日 B-2 起清单路径可查）。

_AUDIT_LOG_PATH = os.path.join(PROJECT_DIR, "gmcache", "backtrace.jsonl")
# 2026-09-01: 修镜像路径——PROJECT_DIR=execution/auto，dirname×2 才是项目根；
# 此前 dirname×1 解析到 execution/ → 镜像误写到 execution/t_io/logs/（已清）。
_AUDIT_MIRROR_PATH = os.path.join(
    os.environ.get("SUPERTRADER_ROOT", os.path.dirname(os.path.dirname(PROJECT_DIR))),
    "t_io", "logs", "auto_backtrace.jsonl")
_audit_file = None
_audit_mirror_file = None
_AUDIT_RUN_ID = ""

def _audit_write(rec: dict):
    rec['_run_id'] = _AUDIT_RUN_ID
    """追加一条决策审计记录"""
    global _audit_file, _audit_mirror_file
    try:
        if _audit_file is None:
            os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
            _audit_file = open(_AUDIT_LOG_PATH, "a", encoding="utf-8")
        _audit_file.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _audit_file.flush()
    except Exception:
        pass
    # P0-2 镜像写：复盘清单路径（失败静默，不阻断主链）
    try:
        if _audit_mirror_file is None:
            os.makedirs(os.path.dirname(_AUDIT_MIRROR_PATH), exist_ok=True)
            _audit_mirror_file = open(_AUDIT_MIRROR_PATH, "a", encoding="utf-8")
        _audit_mirror_file.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        _audit_mirror_file.flush()
    except Exception:
        pass

def _audit_close():
    global _audit_file, _audit_mirror_file
    if _audit_file:
        _audit_file.close()
        _audit_file = None
    if _audit_mirror_file:
        _audit_mirror_file.close()
        _audit_mirror_file = None


def _reconcile_positions_at_init(context):
    """F1: 启动全量持仓对账（2026-07-28 日复盘 P0）

    历史事故：重启后内存台账为空，已持仓标的被重发底仓单（2026-07-28 600481 三轮重复建仓
    至 5600 股）。旧引擎已删（不再有底仓单），但**台账仍需启动灌满**——心跳
    （`_get_holding` → `manual_position`）与收盘回写（`_writeback_holdings`）都读它。
    本函数在 init 末尾执行：逐票查询 account().positions()，有持仓则灌入
    executed_orders/manual_position，每票写 reconcile_init 审计事件。仅 MODE_LIVE 执行。
    """
    try:
        _is_live = context.mode == MODE_LIVE
    except Exception:
        _is_live = False
    if not _is_live:
        return
    for code, sym in STOCKS.items():
        try:
            pos = _sdk_call("positions_recover", lambda: context.account().positions(symbol=sym, side=PositionSide_Long))
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
            _audit_write({"event": "reconcile_init", "code": code, "qty": vol,
                          "available": int(p.available), "cost": _cost,
                          "time": str(datetime.now())})
            print(f"[INIT] {code} {STOCK_NAMES.get(code, code)} 持仓对账: "
                  f"{vol}股 可用{int(p.available)} 成本{_cost:.2f}")
        except Exception as e:
            print(f"[INIT] {code} 持仓对账失败: {e}")


def _append_index_forming(idx_df, gm_symbol="SHSE.000001"):
    """指数日线补当日 forming bar（2026-08-31，对齐手动链 facade 的 _maybe_append_index_forming）：
    gm history_n 盘中不含当日指数 bar → build_decision/regime 用昨日收盘误判市场方向。
    用 gm.current(gm_symbol) 实时行情补当日 OHLC；失败/盘前(未开盘)/已含当日/周末不补。
    仅 MODE_LIVE 调用（回测不得用实时数据污染历史）。A-7: gm_symbol 参数化（分板 4 指数各自补）。"""
    import datetime as _dt
    _now = _dt.datetime.now()
    today = _now.strftime("%Y-%m-%d")
    if _now.weekday() >= 5 or not ("09:15" <= _now.strftime("%H:%M") <= "16:00"):
        return idx_df
    if idx_df is None or idx_df.empty or str(idx_df["date"].iloc[-1]) >= today:
        return idx_df
    try:
        rows = _sdk_call("current", current, gm_symbol)
    except Exception:
        return idx_df
    if not rows:
        return idx_df
    r = rows[0] if isinstance(rows, list) else rows
    px = float(r.get("price") or 0)
    if px <= 0:
        return idx_df
    fb = pd.DataFrame([{
        "date": today,
        "open": float(r.get("open") or px),
        "high": float(r.get("high") or px),
        "low": float(r.get("low") or px),
        "close": px,
        "volume": float(r.get("cum_volume") or 0) / 100.0,  # 股 → 手
    }])
    return pd.concat([idx_df, fb], ignore_index=True)


# ═══════════════════════════════════════════
# A-7 分板指数（2026-09-07）：个股按所属板（沪主板/深主板/创业板/科创）参考对应指数，
# 不再恒用上证。resolve_index 规则单一来源 = superTrader core/board_index（A-1 公共函数，
# 与 manual 侧 timing_gate/index_resonance 同源，禁止在本仓复制第二份前缀规则）。
# ═══════════════════════════════════════════
_BOARD_INDEX_MOD = None


def _board_index_module():
    """加载 superTrader core/board_index（与 build_decision_auto._load_build_decision 同款：
    常规 import 优先，.gszq 部署无 superTrader 根时回退 SUPERTRADER_ROOT importlib）。"""
    global _BOARD_INDEX_MOD
    if _BOARD_INDEX_MOD is not None:
        return _BOARD_INDEX_MOD
    import importlib.util as _ilu
    try:
        from core import board_index as _m
    except ImportError:
        _root = os.environ.get("SUPERTRADER_ROOT") or os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        _path = os.path.join(_root, "core", "board_index.py")
        if not os.path.exists(_path):
            raise RuntimeError(f"分板规则缺失（A-7 依赖）: {_path}")
        _spec = _ilu.spec_from_file_location("core.board_index", _path)
        _m = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_m)
    _BOARD_INDEX_MOD = _m
    return _m


def _code_board_index(code):
    """个股代码 → (index_code, index_gm)。失败回落市场级 (sh000001, SHSE.000001)。"""
    try:
        m = _board_index_module()
        ic, _name = m.resolve_index(code)
        return ic, m.index_gm_symbol(ic)
    except Exception:
        return "sh000001", "SHSE.000001"


def _index_daily_df(gm_symbol):
    """拉 gm 指数日线 900 根 → df(date,open,high,low,close,volume)；失败/不足返回 None。"""
    idx_data = _sdk_call("history_n_index900", _partial(
        history_n, symbol=gm_symbol, frequency="1d", count=900,
        fields="eob,open,high,low,close,volume", fill_missing="Previous"))
    if idx_data is None or len(idx_data) <= 10:
        return None
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
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _needed_index_gm_symbols():
    """本池所需板块指数 GM 符号集合（distinct resolve(STOCKS) ∪ 上证市场级恒有）。"""
    out = {"SHSE.000001"}  # 市场级上证恒拉（大盘 regime/兼容字段依赖）
    for _c in STOCKS:
        try:
            _ic, _gm = _code_board_index(_c)
            out.add(_gm)
        except Exception:
            pass
    return sorted(out)


def init(context):
    global _AUDIT_RUN_ID
    _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 版本探针：用于确认掘金策略加载的是哪一版 gm_main.py（2026-10-12 = 超跌跳空确认单链版）
    print(f"[INIT] gm_main.py 2026-10-12 ws-only loaded")
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
    # 数据源自检（2026-10-12）：盘前预筛靠 `core.market_data.facade`，路径不对会**静默**
    # fail-closed（当日永不下单）⇒ 启动时就探一次并显式告警（08:55 看见，而不是 09:31 才猜）。
    try:
        from core.market_data import facade as _facade_probe      # noqa: F401
        print(f"[init] 数据源就绪: {_facade_probe.__file__}")
    except Exception as _fe:
        print(f"⚠️⚠️ [init] core.market_data.facade 不可导入（盘前预筛将 fail-closed、当日不下单）: "
              f"{type(_fe).__name__}: {_fe}｜sys.path={sys.path[:3]}")
    context.executed_orders = {}
    context.last_index_regime = "range"
    context.last_index_score = 0.0
    context.board_regime = {}      # A-7: {GM指数sym: regime} 分板 regime（上证=市场级恒有，键 GM 全称）
    context._board_gm_symbols = _needed_index_gm_symbols()  # A-7: 本池所需板块指数（预取/每日刷新共用）
    context.manual_position = {}
    context.cur_date = None
    context.rejected_order_count = 0

    # F1: 启动全量持仓对账（账户 → executed_orders/manual_position；心跳与收盘回写的数据源）
    _reconcile_positions_at_init(context)

    # 归属锁（一票一策略）：override 允许人工豁免 ⇒ 每次启动重读（改文件+重启即生效）；
    # 并把当前归属打印出来，让"谁持有哪只票"在启动日志里可见（多策略下这行会变长）。
    try:
        OWNERSHIP.reload_overrides()
        if OWNERSHIP.load_error:
            print(f"[OWN] ⚠️ override 读取失败: {OWNERSHIP.load_error}")
        _snap = OWNERSHIP.snapshot()
        _held = {}
        for _c, _s in STOCKS.items():
            try:
                _h = _get_holding(context, _c, _s)
                if int(_h.get("qty", 0) or 0) > 0:
                    _held[_c] = int(_h.get("qty", 0))
            except Exception:
                continue
        # 2026-10-13 修复：补池外全账户真实持仓——否则旧引擎遗留单被报成
        # 「账户持有 0 只」，unclaimed（LEGACY 锁定）告警也不会触发。
        for _as, _ap in _account_positions_all(context).items():
            _ac = _raw_code(_as)
            if _ac and _ac not in _held and int(_ap.get("qty", 0) or 0) > 0:
                _held[_ac] = int(_ap["qty"])
        print(f"[OWN] 归属表(账本认领)={_snap or '{}'}｜账户持有 {len(_held)} 只")
        _unclaimed = sorted(set(_held) - set(_snap))
        if _unclaimed:
            print(f"[OWN] ⚠️ 账户持有但无策略认领（对全部策略锁定）: "
                  f"{[(c, _held[c]) for c in _unclaimed]}")
    except Exception as _e:
        print(f"[OWN] ⚠️ 归属初始化失败（锁仍生效，按无 override 运行）: {_e}")

    # S-1(2026-08-31): 启动自检 GM 交易会话登录态 + cash>0——未通过即写风险事件（watcher 推飞书加急）。
    # 对应今日账号未登录 2.5h、6 笔拒单事故；cash 读取失败/为 0 视为会话失效。
    if context.mode == MODE_LIVE:
        _cashv = 0.0
        try:
            _acct = _sdk_call("account", context.account)
            _c = getattr(_acct, "cash", None)
            if _c is not None:
                _c = _c() if callable(_c) else _c
                if isinstance(_c, dict):
                    _cashv = float(_c.get("available") or _c.get("available_cash")
                                   or _c.get("cash") or _c.get("total") or 0)
                else:
                    _cashv = float(_c or 0)
        except Exception:
            _cashv = 0.0
        if _cashv <= 0:
            print(f"⚠️⚠️ [S-1] GM 交易会话异常: cash={_cashv}（未登录或会话失效——曾 2.5h 拒单事故）")
            try:
                write_risk(str(datetime.now()), "session_down",
                           f"启动自检: cash={_cashv}，交易会话可能未登录", code="")
            except Exception:
                pass

    context._pending_recon = {}    # Fix B: 下单成功待对账 {sym: {...}}
    context._fills_done = set()    # Fix B: fill 防重 key 集合（回调/轮询共用）

    # 预取板块指数日线（A-7 分板：本池标的所属板指数逐指数预取 + 上证市场级恒有）。
    # 2026-08-28 复审修复（隐性遮蔽显式化）：本模块必须解析到 _gm/analysis 副本
    # （其 GM_INDEX_CACHE/GM_DATA_READY 是本策略的指数数据契约；superTrader 侧同名模块
    # 是另一套带 IO 的实现）。_GM_DIR 在 sys.path 最前，正常即命中 _gm 副本；
    # 若未来 sys.path 被外部改动（如 .gszq 壳误注入仓库根）而遮蔽到 superTrader 侧，立即 fail-loud。
    import analysis.index_regime as ir
    if "_gm" not in ir.__file__:
        raise RuntimeError(f"analysis.index_regime 解析错误（应命中 _gm 副本）: {ir.__file__}")
    for _gm_idx in context._board_gm_symbols:
        try:
            df_idx = _index_daily_df(_gm_idx)
            if df_idx is not None:
                # 2026-08-31: 实盘补当日 forming bar（gm history_n 盘中不含当日指数 → 否则 regime 用昨日收盘）
                if context.mode == MODE_LIVE:
                    df_idx = _append_index_forming(df_idx, _gm_idx)
                ir.GM_INDEX_CACHE[_gm_idx] = df_idx
                print(f"[init] 指数日线已缓存 {_gm_idx}: {len(df_idx)} 行, "
                      f"{df_idx['date'].iloc[0]} ~ {df_idx['date'].iloc[-1]}")
            else:
                print(f"[init] 警告: history_n 未返回指数日线 {_gm_idx}")
        except Exception as e:
            print(f"[init] 指数日线预取失败 {_gm_idx}: {e}")
    # GM_DATA_READY = 上证市场级就绪（大盘 regime 判定依赖；板指数缺失时 detect 单板降级 range）
    ir.GM_DATA_READY = bool(ir.GM_INDEX_CACHE.get("SHSE.000001") is not None
                            and not ir.GM_INDEX_CACHE["SHSE.000001"].empty)

    # 订阅 60s bar：on_bar 的节拍来源（时间窗钩子/心跳/指数态势全走它）。
    # 2026-10-12：原「再额外 subscribe 一遍市场代理池」随 OGR 删除一并移除（代理池只为 mkt_gap 存在）。
    # subscribe 的 count=240 历史上供「盘前 60s 预取进 bar_cache」——该预取与 bar_cache 一并删除
    # 后，count 仅剩「首帧回补 240 根」语义（无消费方），保留数值不动以免改变首帧到达形态。
    symbols = list(STOCKS.values())
    subscribe(symbols=symbols, frequency="60s", count=240,
              fields="symbol,eob,open,high,low,close,volume,amount")
    # 确保事件桥目录存在
    from gm_bridge.writer import BRIDGE_DIR
    os.makedirs(BRIDGE_DIR, exist_ok=True)
    print(f"[init] 事件桥: {BRIDGE_DIR}")
    print(f"[init] 策略初始化完成: {len(symbols)} 只标的")


def _code_to_gm(code: str) -> str:
    """6 位码 → gm symbol（6/5/9 开头沪市，其余深市）。"""
    return ("SHSE." if code[:1] in "569" else "SZSE.") + code


_REJ_STATUS = (4, 5, 6, 8, 12)   # gm 委托终态：这些 status = 拒单/撤单/过期


# ══════════════════════════════════════════════════════════════════════
# 策略框架接线（2026-10-12）：掘金原语 → GmServices → 网关/策略
#
# 仓规：**只有本文件能 import gm.api**。framework/ 内的网关与归属锁一律经 GmServices 注入，
# 因此它们可离线单测、可换纸面网关干跑。
# ══════════════════════════════════════════════════════════════════════


class _GmPrimitives(object):
    """掘金原语的命名空间（注入给框架用，避免框架直接 import gm.api）。"""

    order_volume = staticmethod(order_volume)
    get_orders = staticmethod(get_orders)
    current = staticmethod(current)
    history = staticmethod(history)          # 列表式批量（1800s/60s 多票一次拉）——钩子取数用
    history_n = staticmethod(history_n)
    OrderSide_Buy = OrderSide_Buy
    OrderSide_Sell = OrderSide_Sell
    OrderType_Market = OrderType_Market
    OrderType_Limit = OrderType_Limit
    PositionEffect_Open = PositionEffect_Open
    PositionEffect_Close = PositionEffect_Close
    ADJUST_PREV = ADJUST_PREV


class _OscBookOwner(object):
    """超跌跳空确认的**归属证据**：它的持仓账本（`positions.json`）就是"谁持有"的真源。

    策略增加时，每个策略挂一个这样的适配器进 `OWNERSHIP.strategies` 即可；
    归属判据本身不需要新状态文件（见 framework/ownership.py docstring）。
    """

    id = "osc"

    def _open(self) -> dict:
        # 归属证据 = 策略账本（`osc.executor.load_book`）。2026-10-12 搬进策略包时，
        # executor 的「导入时 sys.path.insert(0, ROOT)」副作用已消除（改绝对路径加载 cost_model）
        # ⇒ 本适配器对宿主零副作用，不再需要 save/restore sys.path。
        try:
            return dict((osc.executor.load_book(_OSC_BOOK_DIR).get("open") or {}))
        except Exception as _e:
            print(f"[OWN] ⚠️ 读超跌跳空确认账本失败（按无持仓——只影响归属判定，不阻断）: {_e}")
            return {}

    def owns(self, code: str) -> bool:
        return str(code) in self._open()

    def owned_codes(self):
        return sorted(self._open())


OWNERSHIP = Ownership(
    strategies=[_OscBookOwner()],
    override_path=_ownership_path_fn(os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader")),
)

SERVICES = GmServices(
    gm=_GmPrimitives(),
    sdk_call=_sdk_call,
    write_order=write_order,
    mark_pending_recon=_mark_pending_recon,
    code_to_gm=_code_to_gm,
    raw_code=_raw_code,
    param=lambda k, d=None: PARAMS.get(k, d),
    clock=lambda ctx: (ctx.now if hasattr(ctx, "now") else datetime.now()),
    reject_statuses=_REJ_STATUS,
    ownership=OWNERSHIP,
    risk=write_risk,
    audit=_audit_write,
    kill_switch=check_kill_switch,
)

# 策略运行时句柄（账本目录 + 时钟 + 日志 + 网关工厂）—— 策略包的唯一入口
OSC_RT = osc.Runtime(
    services=SERVICES,
    book_dir=_OSC_BOOK_DIR,
    is_live=lambda ctx: getattr(ctx, "mode", None) == MODE_LIVE,
    log=print,
)


def on_bar(context, bars):
    now = context.now if hasattr(context, "now") else datetime.now()
    import utils.helpers as uh
    uh.SIM_NOW = now

    t = now.time()
    today = now.date()

    # ── D1: 按日重置 ──
    if context.cur_date is None or context.cur_date != today:
        context.cur_date = today
        # 策略侧按日重置（预筛候选 + 四个"每日一次"标记）——状态在策略包内
        osc.reset_day(today)
        _audit_write({"event": "date_reset", "date": str(today)})

    # 双向看门狗：watcher 心跳缺失/过期自动重生（0806 红日整改）
    ops_guard.ensure_watcher(PROJECT_DIR)

    if t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
        return

    # ── 唯一策略：超跌跳空确认的四时点钩子（09:31 预筛 / 10:00 早盘卖 → 10:00 买 / 14:50 卖）──
    # 时间窗 · 每日一次 · MODE_LIVE 闸 · 逐钩子异常隔离**都在策略包内**（hooks.on_bar），
    # 宿主不再持有任何策略状态。详见 execution/auto/oversold_gap_confirm/。
    osc.on_bar(OSC_RT, context, now, today, t)

    global _WB_DONE_DATE
    # ── 持仓真源回写（2026-09-14 并表）：收盘后一次，把账户实际 qty/cost 写回 holdings.json ──
    # 取 14:57 每日一次（on_bar 在 15:00 后 return，没有更晚的钩子）。与 superTrader 14:59 的
    # pre_close 写并发也安全：本侧是"磁盘为基 + 只补丁 qty/cost"，且对方读后再写，两个方向都不丢。
    # ⚠️ 必须 MODE_LIVE 才回写：回测/回放里的持仓是模拟的，写回会污染生产 holdings.json
    # （2026-09-14 实证：跑回测把 588170 cost 从 0.914 改成回测播种价 0.8951）。
    if (t >= dtime(14, 57) and _WB_DONE_DATE != today
            and getattr(context, "mode", None) == MODE_LIVE):
        _WB_DONE_DATE = today
        try:
            _writeback_holdings(context)
        except Exception as _wbe:
            print(f"[WRITEBACK] 失败（不阻断主循环）: {_wbe}")

    # ── D4: 大盘态势 + 分板态势（每交易日一次） ──
    if today != getattr(context, "_last_ir_date", None):
        context._last_ir_date = today
        try:
            import analysis.index_regime as ir
            if ir.GM_DATA_READY:
                # 每个交易日重新拉指数日线（回测时钟下自动对齐；A-7: 分板指数逐指数刷新）
                for _gm_idx in getattr(context, "_board_gm_symbols", []) or []:
                    try:
                        df_idx = _index_daily_df(_gm_idx)
                        if df_idx is not None:
                            # 2026-08-31: 实盘补当日 forming bar（gm history_n 盘中不含当日指数）
                            if context.mode == MODE_LIVE:
                                df_idx = _append_index_forming(df_idx, _gm_idx)
                            ir.GM_INDEX_CACHE[_gm_idx] = df_idx
                    except Exception as _e2:
                        print(f"[ir] 指数日线刷新失败 {_gm_idx}: {_e2}")
                        # R-3(2026-08-07 W32表决): regime 数据故障不再静默——fail-open 保留但要告警
                        try: write_risk(str(now), "regime_degraded",
                                        f"指数日线刷新失败 {_gm_idx}: {str(_e2)[:120]}", code="")
                        except Exception: pass

                # R-1(2026-08-07 W32表决): 实盘传 mode="live"，剔除当日未成形K线再判定
                try:
                    _ir_mode = "live" if context.mode == MODE_LIVE else "eod"
                except Exception:
                    _ir_mode = "eod"
                # 市场级（上证腿）——context.last_index_regime 兼容字段恒 = 上证（D2 双轨：市场级保留，不替换）
                ir_regime, ir_score, ir_ctx = ir.detect_index_regime(
                    as_of=now.strftime("%Y-%m-%d"), force=True, mode=_ir_mode)
                context.last_index_regime = ir_regime.value if hasattr(ir_regime, "value") else str(ir_regime)
                context.last_index_score = float(ir_score)
                # A-7: 分板级 regime（board_regime 键 = GM 指数全称；上证复用市场级结果）
                _br = {"SHSE.000001": context.last_index_regime}
                for _gm_idx in getattr(context, "_board_gm_symbols", []) or []:
                    if _gm_idx == "SHSE.000001":
                        continue
                    try:
                        _r, _s, _c = ir.detect_index_regime(
                            as_of=now.strftime("%Y-%m-%d"), force=True, mode=_ir_mode,
                            index_symbol=_gm_idx)
                        _br[_gm_idx] = _r.value if hasattr(_r, "value") else str(_r)
                    except Exception as _e3:
                        print(f"[ir] {_gm_idx} 分板态势判定失败: {_e3}")
                        _br[_gm_idx] = "range"
                context.board_regime = _br
                degraded = ir_ctx.get("degraded", [])
                if degraded:
                    print(f"[ir] {str(today)} regime={context.last_index_regime} score={context.last_index_score:.1f} degraded={degraded}")
                    # R-3: degraded fail-open 但写 risk 告警
                    try: write_risk(str(now), "regime_degraded", f"degraded={degraded} regime={context.last_index_regime}", code="")
                    except Exception: pass
                else:
                    print(f"[ir] {str(today)} regime={context.last_index_regime} score={context.last_index_score:.1f}")
                print(f"[ir] {str(today)} board_regime={context.board_regime}")
        except Exception as e:
            print(f"[ir] 大盘态势判定失败: {e}")

    # Fix C(2026-09-11): 回调失效轮询兜底（挂心跳前，60s bar 自然节拍；fail-open 不阻塞）
    try:
        _poll_pending_recon(context, now)
    except Exception:
        pass

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
        # 2026-10-13 修复：合并**池外**全账户真实持仓（旧引擎遗留单不过滤），
        # 池内口径不变（仍走 _get_holding 多源对账，含我们跟踪的成本）。
        for _as, _ap in _account_positions_all(context).items():
            if _as not in _hb_positions and int(_ap.get("qty", 0) or 0) > 0:
                _hb_positions[_as] = {"qty": int(_ap["qty"]),
                                      "cost": float(_ap.get("cost", 0) or 0)}
        # ①-3: 实时读取可用现金
        _hb_cash = INITIAL_CASH
        try:
            _acct = _sdk_call("account", context.account)
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

    # ⚠️ 2026-10-12：逐票循环（bar 累积 / `_ogr_pc_cache` / 旧引擎决策链）已随旧引擎删除。
    #    超跌跳空确认链路不读 `bar_cache`（自取 1800s/60s 棒），故此处不再累积任何 per-bar 状态。


def _strategy_ordered_today(code: str) -> bool:
    """P2-3A(2026-09-10): 当日事件桥是否存在本策略对该 code 的 order 事件。
    账户级 on_order_status 会对非本策略成交（掘金仿真终端手工单）也回调——据此识别孤儿单。
    fail-open：读失败/文件缺失 → True（按非孤儿，不误拦本策略单）。"""
    try:
        from gm_bridge.writer import BRIDGE_DIR
        fp = os.path.join(BRIDGE_DIR, f"events_{datetime.now().strftime('%Y%m%d')}.jsonl")
        if not os.path.exists(fp):
            return True
        for _line in open(fp, encoding="utf-8", errors="replace"):
            _line = _line.strip()
            if not _line:
                continue
            try:
                _e = json.loads(_line)
            except Exception:
                continue
            if _e.get("event") == "order" and str(_e.get("code")) == str(code):
                return True
        return False
    except Exception:
        return True


def on_order_status(context, order):
    symbol = order["symbol"]
    status = order["status"]
    volume = order["volume"]
    code = _raw_code(symbol)  # 提前到price兜底之前
    # ①-1/F6: 成交价优先 filled_vwap —— 掘金市价单 order["price"] 携带涨跌停保护价
    # (2026-07-29 C1: 600481卖出真实成交4.01被记为跌停价3.52；买入路径会用此价计算cost，错误价会毒化成本)
    price = order.get("filled_vwap") or order.get("vwap") or order.get("price") or 0
    # 注：原「price<=0 回退到 context.latest_pre_close（昨收）」的取数源随旧引擎（_refresh_daily_ctx）
    # 删除 ⇒ 保留 0 并交由下游（_get_holding 的 cost=0 → gm vwap 修正）兜底。
    side = order["side"]

    if status == 3:  # 全部成交
        # P2-3A(2026-09-10): 孤儿闸——账户级回调对非本策略成交（掘金仿真终端手工单）也触发，
        # 当日无本策略 order 记录 → 只留痕 risk:orphan_fill，不写 fill/不进台账（09-10 300054 串 800 股根因）。
        _side = "BUY" if side == 1 else "SELL"
        if not _strategy_ordered_today(code):
            try:
                write_risk(str(datetime.now()), "orphan_fill",
                           f"非本策略成交(疑似仿真终端手工单) {code} {_side} {volume}@{price} → 不入台账",
                           code=code)
                print(f"[{datetime.now():%H:%M:%S}] ORPHAN_FILL {code} {_side} {volume}@{price} 不入台账")
            except Exception:
                pass
            return
        # Fix B(2026-09-11): fill 防重闸——回调迟到与轮询补记共用 key，防二次入账
        _oid = order.get("id") or order.get("order_id") or ""
        _fk = (symbol, _oid, int(volume or 0)) if _oid else \
            (symbol, _side, int(volume or 0), str(datetime.now())[:16])
        _done = getattr(context, "_fills_done", None)
        if _done is None:
            context._fills_done = set()
            _done = context._fills_done
        if _fk in _done:
            print(f"[fill] 重复成交已跳过 {code} {_side} {volume}@{price}")
            return
        _done.add(_fk)
        # O-06(2026-08-11 复盘①轻)：台账在本回调内尚未更新（更新在下方），
        # SELL 分支直接读台账得到的是成交前持仓（0811 实战：卖 200 后 pos_after 仍报 1400）。
        _pre_qty = int(context.executed_orders.get(symbol, {}).get("qty", 0))
        _pos_after = max(0, _pre_qty - volume) if _side == "SELL" else _pre_qty + volume
        try:
            # F-9/Q-20260911: 优先 gm 实收 filled_commission，缺失回退费率估算并标 fee_source
            _gm_fee = order.get("filled_commission")
            if _gm_fee is not None:
                _fee, _fsrc = float(_gm_fee), "gm"
            else:
                _fee_rate = float(PARAMS.get("commission_ratio", 0.00015) or 0.00015)
                _fee, _fsrc = round(float(price) * int(volume) * _fee_rate, 2), "estimated"
            # 2026-09-15 阶段0-5 接线（诊断D3，W4 已在 writer.write_fill 加可空字段）：
            # 补真实委托价/成交均价。口径注意——市价单 order["price"] 是**涨跌停保护价**
            # （非期望价），slippage=fill_vwap-order_price 的方向解读归下游；
            # 无真实值的字段传 None（writer 落 null，禁止编造）；0 值经 or None 归一防假滑点。
            write_fill(str(datetime.now()), _raw_code(symbol), _side, volume, price,
                       order_id=str(order.get("id") or ""), pos_after=_pos_after,
                       fee=_fee, fee_source=_fsrc,
                       order_price=(order.get("price") or None),
                       fill_vwap=(order.get("filled_vwap") or order.get("vwap") or None))
        except Exception:
            pass
        _pending_recon_close(context, symbol)   # Fix B: 该 symbol 已完成对账，轮询不再兜底
        if side == 1:  # 买入
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
            # WP-B回放包: 回测下用仿真时钟(context.now)，否则验收无法对齐窗口日期
            _ts_now = str(getattr(context, "now", None) or datetime.now())
            _audit_write({"event": "sell", "code": code, "qty": volume, "price": price,
                          "time": _ts_now, "pos_after_sell": new_qty})
    elif status in (4, 5, 6, 8, 12):  # 拒单/撤单/待撤/已拒绝(8)/已过期(12) —— F2修复: 2026-07-28前漏掉8导致所有拒单静默
        _rej_detail = ""
        try:
            _rej_detail = order.get("ord_rej_reason_detail", "") or ""
        except Exception:
            pass
        context.rejected_order_count = getattr(context, 'rejected_order_count', 0) + 1
        print(f"[ORDER] {symbol} 被拒 status={status} {_rej_detail}")
        # N25-2: 卖出拒单回滚 manual_position（下单方在 on_order_status 之外只更 executed_orders，
        # 但 OPEN_ALIGN 时代的虚减已随其删除；超跌跳空确认的腿账本自成一套，不走 manual_position）
        if side == 2 and symbol in context.manual_position:
            mp = context.manual_position[symbol]
            mp["qty"] = mp.get("qty", 0) + volume
            mp["available"] = mp.get("available", 0) + volume
            mp["t_qty"] = mp.get("t_qty", 0) + volume
            _audit_write({"event": "sell_rollback", "code": code, "qty": volume,
                          "time": str(getattr(context, "now", None) or datetime.now())})
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
    print(f"  拒单笔数={getattr(context, 'rejected_order_count', 0)}")
    print("*" * 50)


if __name__ == "__main__":
    _AUDIT_RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 合并方案 P0-2(2026-08-28): token 不再硬编码入库，统一走 utils/gm_token.py
    from utils.gm_token import load_token
    run(strategy_id="e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7",
        filename="gm_main.py", mode=MODE_LIVE,
        token=load_token())
    # F1(2026-09-09): run() 正常返回分支横幅（区分优雅结束 vs 硬 kill）
    print("===== strategy run 返回（正常结束）=====", flush=True)
    try:
        write_risk(datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "strategy_exit", "策略 run() 返回（优雅结束）")
    except Exception:
        pass
