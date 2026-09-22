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
from data.indicators import add_indicators
from t_engine_auto import SignalEngine
from signals.position_sizer import PositionSizer
from utils.helpers import _now, _default_daily_context
from gm_bridge.writer import (
    write_signal, write_order, write_fill, write_reject, write_risk,
    write_heartbeat, check_kill_switch, write_snapshot,
    write_buy_pending, read_buy_pending, read_buy_decision, write_confirm,
    read_auto_build, consume_auto_build,
)
from gm_bridge import ops_guard

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

# ── 目标底仓（2026-09-14 持仓并表后） ──
# MIRROR = 各票**目标底仓**，直读 holdings.json 的 `base`（身份/实际持仓/目标同源）。
# 旧的两级回退（AUTO_MIRROR_OVERRIDE / AUTO_POOL[code].mirror_qty）已随并表删除。
def _load_mirror_holdings():
    import json as _json
    root = os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader")
    path = os.path.join(root, "t_io", "state", "holdings.json")
    if not os.path.exists(path):
        raise RuntimeError(f"持仓真源缺失（镜像持仓依赖）: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = _json.load(f)
    try:
        _pool = (_auto_pool.AUTO_POOL if hasattr(_auto_pool, "AUTO_POOL")
                 else getattr(_auto_pool, "AUTO_POOL", {})) or {}
    except Exception:
        _pool = {}
    out = {}
    for code, h in (data.items() if isinstance(data, dict) else []):
        if not isinstance(h, dict) or str(code).startswith("_"):
            continue
        # 仅 auto 池成员纳入 MIRROR（防纯手动票被镜像进来）；池读取失败时不裁剪（保持可用）
        if _pool and code not in _pool:
            continue
        # 2026-09-14 持仓并表：目标底仓 = holdings.json 的 `base`（旧 OVERRIDE / mirror_qty
        # 两级回退已随并表删除——身份+持仓+目标现为同一份真源）。
        tgt = int(h.get("base") or 0)
        if tgt <= 0:
            continue
        out[code] = {"qty": tgt, "cost": float(h.get("cost") or 0)}
    return out


MIRROR_HOLDINGS = _load_mirror_holdings()


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
    disk = repo.load_full()
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
    repo.save_held_merged(patch, actor="auto_eod", reason="eod_writeback_qty_cost")
    print(f"[WRITEBACK] 持仓真源已回写 {len(patch)} 票（qty/cost；base/pre_close 未动）")
    return len(patch)


# ── 开盘强制对齐（2026-09-14 owner 裁决）────────────────────────────
_OPEN_ALIGN_DONE_DATE = None
# 买入优先级：缺额从小到大（资金效率优先，能多对齐几只）。owner 可自行调整顺序，
# 未列出的 code 排在最后。
OPEN_ALIGN_BUY_ORDER = ["588170", "002639", "300153", "002451", "000988", "300054", "600176"]


# ── 2026-09-15 阶段0-6（诊断D3旁注）：沪市市价单(=保护限价)涨跌停贴板钳制 ──
# 背景：09-15 盘中新增 7 笔 GMBROKER 拒单——沪市市价单的保护限价与涨/跌停价冲突
# （600176×5、588170×1）。贴涨停的买单/贴跌停的卖单是**确定性拒单**（涨停价买单
# 排不到队、跌停价卖单同理），下单前预判跳过并留痕，不再把废单送到柜台。
# 约束（施工单）：只加钳制与留痕——不改 OrderType_Market、不改任何量价逻辑。
# 贴板判定带宽默认 0.2%（距涨/跌停不足 0.2% 即跳过，预留撮合余量）；
# 可在 config/params.py 的 PARAMS 里加 "limit_clamp_margin": 0.002 覆盖。
LIMIT_CLAMP_MARGIN = float(PARAMS.get("limit_clamp_margin", 0.002) or 0.002)


def _board_limit_pct(code, name=""):
    """按板块规则返回涨跌停幅度（2026-09 现行规则口径）：
    - 主板（60xxxx/000xxx/001xxx/002xxx/003xxx）10%
    - 主板风险警示 ST/*ST 5%
    - 创业板（300/301/302）20%（2020-08-24 注册制起，含其风险警示股）
    - 科创板（688/689）20%；科创类 ETF（588xxx）同板 20%
    - 北交所（4xxxxx/8xxxxx/920xxx）30%（本池暂无，防御性列出）
    - 其余场内基金（51xxxx/15xxxx/56xxxx 等）随主板 10%
    返回小数（0.10/0.20/0.30/0.05）。ST 判定用名称含 "ST"。"""
    c = str(code or "")
    nm = str(name or STOCK_NAMES.get(c, "") or "").upper()
    if c.startswith(("300", "301", "302")):          # 创业板 20%
        return 0.20
    if c.startswith(("688", "689", "588")):          # 科创板 / 科创ETF 20%
        return 0.20
    if c.startswith(("4", "8", "920")):              # 北交所 30%（池内暂无）
        return 0.30
    if "ST" in nm:                                    # 主板风险警示 5%
        return 0.05
    return 0.10                                       # 主板（含主板ETF）10%


def _limit_prices(context, code, sym, pre_close):
    """返回 (limit_up, limit_down, src)。优先 gm 真实涨跌停价（get_instruments 的
    upper_limit/lower_limit 字段——真实值含 ST/次新股等全部特殊规则，优于推算）；
    拿不到按板块规则 × pre_close 推算。结果按 (code, 交易日) 缓存（涨跌停价日内不变）。
    全程 fail-open：任何异常回退规则推算；仍不可得 → (None, None, "none") 表示无法预判。"""
    _cache = getattr(context, "_limit_px_cache", None)
    if _cache is None:
        _cache = {}
        context._limit_px_cache = _cache
    # 缓存键日期用策略时钟（context.now，回测=仿真日），无则系统时钟——防回测跨日缓存不刷新
    _ndt = getattr(context, "now", None) or datetime.now()
    _today = _ndt.strftime("%Y-%m-%d")
    _hit = _cache.get(code)
    if _hit and _hit.get("date") == _today:
        return _hit.get("up"), _hit.get("down"), _hit.get("src")
    _up = _down = None
    _src = "none"
    # ① gm 真实值（实盘；回测/异常静默跳过）
    try:
        if getattr(context, "mode", None) == MODE_LIVE and "get_instruments" in globals():
            _df = _sdk_call("get_instruments_limit", _partial(
                get_instruments, symbols=sym, df=True))
            if _df is not None and len(_df) > 0:
                _row = _df.iloc[0]
                _u = float(_row.get("upper_limit") or 0)
                _d = float(_row.get("lower_limit") or 0)
                if _u > 0 and _d > 0:
                    _up, _down, _src = _u, _d, "gm"
    except Exception:
        pass
    # ② 板块规则 × pre_close 推算
    if _up is None:
        try:
            _pc = float(pre_close or 0)
        except Exception:
            _pc = 0.0
        if _pc > 0:
            _pct = _board_limit_pct(code)
            # 最小变动价位 0.01 四舍五入（A 股涨跌停价按分舍入，容差由 0.2% 带宽覆盖）
            _up = round(_pc * (1 + _pct), 2)
            _down = round(_pc * (1 - _pct), 2)
            _src = "rule"
    _cache[code] = {"date": _today, "up": _up, "down": _down, "src": _src}
    return _up, _down, _src


def _limit_clamp_should_skip(context, code, sym, side, qty, price, now, where):
    """下单前贴板预判：BUY 距涨停 / SELL 距跌停不足 LIMIT_CLAMP_MARGIN（默认 0.2%）
    → 确定性拒单，跳过并留痕（print + risk:limit_clamp_skip + audit），返回 True。
    涨跌停价不可得 / 钳制器自身异常 → False（不拦，保持原行为，fail-open）。"""
    try:
        _px = float(price or 0)
        if _px <= 0:
            return False
        try:
            _pc = float(context.latest_pre_close.get(code, 0) or 0)
        except Exception:
            _pc = 0.0
        _up, _down, _src = _limit_prices(context, code, sym, _pc)
        # 数据一致性护栏（2026-09-15）：涨跌停价由 latest_pre_close 推导，若它与 bar 价**不同尺度**，
        # 钳制会误拦该标的的全部买入。实测 588170：pre_close≈0.64 → up=0.70，而 bar 价 1.66-1.74，
        # 导致 11,049 次 BUY 被"贴板跳过"（其余 8 只全为 0），该票日内只能卖不能买。
        # 真实价格不可能高于涨停价 / 低于跌停价 ⇒ 出现该情形即判定涨跌停数据不可信，
        # fail-open 不拦（只按 code 告警一次），把问题交回数据源而不是让订单被静默吞掉。
        if (_up and _px > _up * 1.05) or (_down and _px < _down * 0.95):
            _bad = getattr(context, "_limit_data_bad", None)
            if _bad is None:
                _bad = set(); context._limit_data_bad = _bad
            if code not in _bad:
                _bad.add(code)
                print(f"[{now:%H:%M:%S}] LIMIT_DATA_BAD {code} 价格{_px:.3f} 越出涨跌停带 "
                      f"(up={_up:.3f} down={_down:.3f} pre_close={_pc:.3f} src={_src}) "
                      f"→ 涨跌停数据不可信，本标的不再贴板钳制（fail-open）")
                try:
                    write_risk(str(now), "limit_data_inconsistent",
                               f"price={_px:.3f} up={_up:.3f} down={_down:.3f} pre_close={_pc:.3f}",
                               code=code)
                except Exception:
                    pass
            return False
        _kind, _lim = None, None
        if side == "BUY" and _up and _px >= _up * (1 - LIMIT_CLAMP_MARGIN):
            _kind, _lim = "near_limit_up", _up
        elif side == "SELL" and _down and _px <= _down * (1 + LIMIT_CLAMP_MARGIN):
            _kind, _lim = "near_limit_down", _down
        if not _kind:
            return False
        _msg = (f"{side} {int(qty or 0)}@{_px:.3f} 贴板跳过: {_kind} limit={_lim:.3f} "
                f"带宽={LIMIT_CLAMP_MARGIN:.1%} src={_src} where={where} "
                f"—市价保护限价与涨跌停冲突属确定性拒单(09-15盘中×7)")
        print(f"[{now:%H:%M:%S}] LIMIT_CLAMP {code} {_msg}")
        try:
            write_risk(str(now), "limit_clamp_skip", _msg, code=code)
        except Exception:
            pass
        try:
            _audit_write({"event": "limit_clamp_skip", "code": code, "side": side,
                          "qty": int(qty or 0), "price": _px, "limit": _lim,
                          "kind": _kind, "src": _src, "where": where, "time": str(now)})
        except Exception:
            pass
        return True
    except Exception:
        return False


def _force_open_align(context) -> int:
    """开盘一次性把实际持仓对齐到目标底仓（base）：超额卖出、缺口买入。

    owner 2026-09-14 裁决："明日开盘一次性强制对齐；资金不足时按优先级逐个买满"。
    这是**账本校正**不是做T，故不走信号/闸门链；但有两条硬约束：
      ① **必须先 write_order 落 order 事件** —— 否则 on_order_status 的孤儿闸会把成交
         判成"非本策略"直接丢弃（6a96829c 实证：5/6 底仓单因此不入台账）；
      ② 买入受**可用现金**封顶，按 OPEN_ALIGN_BUY_ORDER 逐个买满，买不起就停。
    返回本轮下单单数。
    """
    avail = 0.0
    try:
        _acct = _sdk_call("account", context.account)
        _c = getattr(_acct, "cash", None)
        if _c is not None:
            avail = float(getattr(_c, "available", 0) or 0)
    except Exception:
        avail = 0.0

    def _px_of(code, sym):
        try:
            p = float(context.latest_pre_close.get(code, 0) or 0)
        except Exception:
            p = 0.0
        if p <= 0:
            try:
                dec = (getattr(context, "daily_decision_stats", None) or {}).get(code) or {}
                p = float(dec.get("last_price") or 0)
            except Exception:
                p = 0.0
        return p

    sells, buys = [], []
    for code, sym in STOCKS.items():
        target = int(getattr(context, f"_base_ref_{code}", 0) or 0)
        if target <= 0:
            continue
        try:
            h = _get_holding(context, code, sym)
        except Exception:
            continue
        actual = int(h.get("qty", 0) or 0)
        diff = actual - target
        if diff >= 100:                                   # 超额 → 卖
            _av_raw = h.get("available")
            _av = actual if _av_raw is None else int(_av_raw)
            q = (min(diff, _av) // 100) * 100
            if q >= 100:
                sells.append((code, sym, q))
        elif -diff >= 100:                                # 缺口 → 买
            buys.append((code, sym, (-diff // 100) * 100))

    now = context.now if hasattr(context, "now") else datetime.now()
    n = 0
    # ① 超额一律卖出（不占资金）
    for code, sym, q in sells:
        px = _px_of(code, sym)
        if px <= 0:
            continue
        # 2026-09-15 阶段0-6（诊断D3旁注）：贴跌停卖单=确定性拒单，跳过并留痕
        if _limit_clamp_should_skip(context, code, sym, "SELL", q, px, now, "open_align"):
            continue
        try:
            write_order(str(now), code, "SELL", q, px, order_type="ALIGN")
            _o = _sdk_call("order_volume_align_sell", _partial(
                order_volume, symbol=sym, volume=q, side=OrderSide_Sell,
                order_type=OrderType_Market, position_effect=PositionEffect_Close))
            _mark_pending_recon(context, code, sym, "SELL", q, px, _o)
            n += 1
            print(f"[OPEN_ALIGN] SELL {code} {q}股@{px:.3f}（超额归位到目标 {getattr(context, f'_base_ref_{code}', 0)}）")
            _audit_write({"event": "open_align", "code": code, "side": "SELL", "qty": q,
                          "price": round(px, 4), "time": str(now), "reason": "excess_over_base"})
        except Exception as e:
            print(f"[OPEN_ALIGN] SELL {code} 失败: {e}")
    # ② 缺口按优先级买满，现金不够就停
    _pri = {c: i for i, c in enumerate(OPEN_ALIGN_BUY_ORDER)}
    buys.sort(key=lambda x: _pri.get(x[0], 999))
    skipped = []
    for code, sym, q in buys:
        px = _px_of(code, sym)
        if px <= 0:
            skipped.append((code, q, "无价"))
            continue
        # 2026-09-15 阶段0-6（诊断D3旁注）：贴涨停买单=确定性拒单，跳过并留痕
        if _limit_clamp_should_skip(context, code, sym, "BUY", q, px, now, "open_align"):
            skipped.append((code, q, "贴板(近涨停)"))
            continue
        afford = int(avail / px) // 100 * 100
        q = min(q, afford)
        if q < 100:
            skipped.append((code, q, f"现金不足(可用{avail:.0f})"))
            continue
        try:
            write_order(str(now), code, "BUY", q, px, order_type="ALIGN")
            _o = _sdk_call("order_volume_align_buy", _partial(
                order_volume, symbol=sym, volume=q, side=OrderSide_Buy,
                order_type=OrderType_Market, position_effect=PositionEffect_Open))
            _mark_pending_recon(context, code, sym, "BUY", q, px, _o)
            avail -= q * px
            n += 1
            print(f"[OPEN_ALIGN] BUY {code} {q}股@{px:.3f}（补缺口到目标 {getattr(context, f'_base_ref_{code}', 0)}，余现金 {avail:.0f}）")
            _audit_write({"event": "open_align", "code": code, "side": "BUY", "qty": q,
                          "price": round(px, 4), "time": str(now), "reason": "shortfall_vs_base"})
        except Exception as e:
            print(f"[OPEN_ALIGN] BUY {code} 失败: {e}")
    if skipped:
        print(f"[OPEN_ALIGN] 未补满: {skipped}")
    print(f"[OPEN_ALIGN] 完成：下单 {n} 笔（卖 {len(sells)} / 买 {len(buys) - len(skipped)}）")
    return n

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


def _project_buy_qty(context, code, holding, sig, threshold, pos_qty):
    """预估确认买入量（仅请求文件展示用；下单量以下单块 sizer 重算为准）。
    pos_qty>0 → sizer.calc_buy_qty(带 target_t=底仓+T余量)，异常/0 回退 300。
    注：调用点（BUY 块）pos_qty 恒 >0（on_bar 1498 已守卫），无持仓分支不可达。"""
    try:
        _base_ref = getattr(context, f'_base_ref_{code}', 0) or pos_qty
        _t_pct = float(context.engine._get_params(code).get("stock_qty_base_pct", 0.3) or 0.3)
        _t_head = max(100, int(_base_ref * _t_pct / 100) * 100)
        _target_t = max(pos_qty, _base_ref + _t_head)
        _q = context.sizer.calc_buy_qty(code, dict(holding, target_t=_target_t),
                                        getattr(sig, "score", 0) or 0, threshold)
        return int(_q) if _q and int(_q) > 0 else 300
    except Exception:
        return 300


def _buy_confirm_gate(context, code, now, *, action, price, qty_proj, pos_qty,
                      reasons, needs_confirm, kind=None):
    """人工确认闸（2026-08-30 建仓/加仓/底仓回补人工把关）。on_bar 内绝不阻塞。
    返回 "allow" | "pending" | "rejected_today"。

    - 主开关关闭或非 MODE_LIVE（回测）→ "allow"（零文件 I/O，防回测卡死）
    - 当日已拒绝 → "rejected_today"（不再弹、不再写请求）
    - 无 pending：needs_confirm=False（做T回补路径）→ "allow"；否则发请求 → "pending"
    - 有 pending：action 不匹配（同标的有另一块请求未决）→ "pending" 等待不消费；
      action 匹配 → 读 BUY_DECISION.json：
        confirm → 清请求 → "allow"；reject → 记当日拒绝 → "rejected_today"；
        无回复 → "pending"（一直挂起直到用户响应）。"""
    if not PARAMS.get("human_confirm_buy_enabled", True) or context.mode != MODE_LIVE:
        return "allow"
    if code in context._buy_confirm_rejected:
        return "rejected_today"

    def _persist():
        try:
            write_buy_pending({"date": f"{now:%Y-%m-%d}",
                               "updated_at": f"{now:%Y-%m-%d %H:%M:%S}",
                               "rejected_today": sorted(context._buy_confirm_rejected),
                               "pending": context._buy_confirm_pending})
        except Exception:
            pass

    if code not in context._buy_confirm_pending:
        if not needs_confirm:
            return "allow"
        _req = {
            "request_id": f"{code}_{action}_{now:%Y%m%d%H%M%S}",
            "code": code,
            "name": STOCK_NAMES.get(code, code),
            "action": action,
            "kind": kind or ("build" if pos_qty <= 0 else "add"),
            "side": "BUY",
            "qty": int(qty_proj or 0),
            "price": round(float(price), 3) if price else None,
            "pos_qty": int(pos_qty or 0),
            "score": 0,
            "reasons": [str(r) for r in (reasons or [])],
            "request_ts": now.timestamp(),
        }
        context._buy_confirm_pending[code] = _req
        _persist()
        try:
            write_confirm(str(now), code, "request",
                          detail=(f"{_req['action']} {_req['qty']}@{_req['price']} "
                                  f"pos={_req['pos_qty']} kind={_req['kind']}"),
                          request_id=_req["request_id"], action=_req["action"],
                          qty=_req["qty"], kind=_req["kind"])
        except Exception:
            pass
        print(f"[{now:%H:%M:%S}] BUY {code} 待人工确认: {_req['action']} "
              f"{_req['qty']}@{_req['price']} pos_qty={pos_qty} kind={_req['kind']}")
        return "pending"

    _pend = context._buy_confirm_pending[code]
    if _pend.get("action") != action:
        return "pending"
    try:
        _d = (read_buy_decision().get("decisions") or {}).get(code)
    except Exception:
        _d = None
    if _d and _d.get("request_id") == _pend.get("request_id"):
        if _d.get("decision") == "confirm":
            context._buy_confirm_pending.pop(code, None)
            _persist()
            try:
                write_confirm(str(now), code, "approved",
                              detail=f"{action} qty={_pend.get('qty')}@{_pend.get('price')}",
                              request_id=_pend.get("request_id"))
            except Exception:
                pass
            print(f"[{now:%H:%M:%S}] BUY {code} 用户确认放行: {action}")
            return "allow"
        if _d.get("decision") == "reject":
            context._buy_confirm_pending.pop(code, None)
            context._buy_confirm_rejected.add(code)
            _persist()
            try:
                write_confirm(str(now), code, "rejected",
                              detail=f"{action} 用户拒绝，当日不再提示",
                              request_id=_pend.get("request_id"))
            except Exception:
                pass
            print(f"[{now:%H:%M:%S}] BUY {code} 用户拒绝，当日不再提示: {action}")
            return "rejected_today"
    return "pending"


# P0-4(2026-09-01): confirm 后未执行超时阈值——具体数值留周六(09-05)拍板，先用占位 300s
BUY_CONFIRM_EXEC_TIMEOUT_SEC = 300


def _scan_pending_confirm(context, now):
    """P0-4 日内巡检：BUY_DECISION 已 confirm 但 pending 未被消费（信号未再触发 → gate 未调用）
    → 超时后作废并留痕 approved_not_executed + 告警，杜绝永久悬空。on_bar 每 bar 调用。"""
    _pend_map = getattr(context, "_buy_confirm_pending", None)
    if not _pend_map:
        return
    try:
        _decs = (read_buy_decision().get("decisions") or {})
    except Exception:
        return
    for code, pend in list(_pend_map.items()):
        _d = _decs.get(code)
        if not (_d and _d.get("request_id") == pend.get("request_id")
                and _d.get("decision") == "confirm"):
            continue
        try:
            _elapsed = now.timestamp() - float(pend.get("request_ts") or 0)
        except Exception:
            _elapsed = 0
        if _elapsed < BUY_CONFIRM_EXEC_TIMEOUT_SEC:
            continue
        _pend_map.pop(code, None)
        try:
            write_confirm(str(now), code, "approved_not_executed",
                          detail=(f"confirm 到达但超时({BUY_CONFIRM_EXEC_TIMEOUT_SEC}s)未执行，"
                                  f"信号未再触发 request_ts={pend.get('request_ts')}"),
                          request_id=pend.get("request_id"), action=pend.get("action"))
        except Exception:
            pass
        try:
            write_buy_pending({"date": f"{now:%Y-%m-%d}",
                               "updated_at": f"{now:%Y-%m-%d %H:%M:%S}",
                               "rejected_today": sorted(getattr(context, "_buy_confirm_rejected", set())),
                               "pending": _pend_map})
        except Exception:
            pass
        print(f"[{now:%H:%M:%S}] BUY {code} 确认超时未执行（信号未再触发），已作废 approved_not_executed")


# 2026-09-15 阶段0-3（诊断D3）：BUY_PENDING 盘中超时看门狗默认 20 分钟。
# 可在 config/params.py 的 PARAMS 里加 "buy_pending_timeout_min": <分钟> 覆盖；
# 跨日作废已有（init F3 块）、confirm 到达未执行已有（上方 P0-4），本项补"无人确认挂起"第三种失控。
BUY_PENDING_TIMEOUT_MIN = float(PARAMS.get("buy_pending_timeout_min", 20) or 20)


def _scan_pending_unanswered(context, now):
    """阶段0-3(2026-09-15 诊断D3-C)：人工确认请求（BUY_PENDING/BUY_DECISION）挂起超过
    BUY_PENDING_TIMEOUT_MIN 分钟无人确认 → 自动作废并留痕（expired_timeout + risk 告警）。
    防 09-09「confirm 无人执行、挂起至今」重演（D3：9 条请求 6 条非正常闭环，挂起未决 1 条）。
    与 P0-4 互补：P0-4 处理"confirm 已到但信号未再触发"，本函数处理"用户始终未响应"。
    作废后下一根 bar 若信号仍触发会重新发请求（request_id 更新），不堵交易链路。
    on_bar 每 bar 调用；仅 MODE_LIVE 有意义（回测 gate 恒 allow，pending 恒空）。"""
    if getattr(context, "mode", None) != MODE_LIVE:
        return
    _pend_map = getattr(context, "_buy_confirm_pending", None)
    if not _pend_map:
        return
    _timeout_sec = BUY_PENDING_TIMEOUT_MIN * 60.0
    try:
        _decs = (read_buy_decision().get("decisions") or {})
    except Exception:
        _decs = {}
    _expired = []
    for code, pend in list(_pend_map.items()):
        # 已有匹配决策（confirm/reject）→ 交给 gate / P0-4 消费，本看门狗不管
        _d = _decs.get(code)
        if _d and _d.get("request_id") == pend.get("request_id"):
            continue
        try:
            _elapsed = now.timestamp() - float(pend.get("request_ts") or 0)
        except Exception:
            _elapsed = 0
        if _elapsed < _timeout_sec:
            continue
        _pend_map.pop(code, None)
        _expired.append((code, pend, _elapsed))
    if not _expired:
        return
    for code, pend, _elapsed in _expired:
        try:
            write_confirm(str(now), code, "expired",
                          detail=(f"盘中超时无人确认自动作废: {pend.get('action')} "
                                  f"qty={pend.get('qty')}@{pend.get('price')} "
                                  f"挂起{_elapsed/60:.1f}min>阈值{BUY_PENDING_TIMEOUT_MIN:.0f}min"),
                          request_id=pend.get("request_id"), action=pend.get("action"))
        except Exception:
            pass
        try:
            write_risk(str(now), "buy_pending_timeout",
                       f"{code} {pend.get('action')} 挂起{_elapsed/60:.1f}分钟无人确认已自动作废", code=code)
        except Exception:
            pass
        print(f"[{now:%H:%M:%S}] BUY {code} 挂起 {_elapsed/60:.1f} 分钟无人确认，已自动作废（防挂起失控）")
    try:
        write_buy_pending({"date": f"{now:%Y-%m-%d}",
                           "updated_at": f"{now:%Y-%m-%d %H:%M:%S}",
                           "rejected_today": sorted(getattr(context, "_buy_confirm_rejected", set())),
                           "pending": _pend_map})
    except Exception:
        pass


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


def _build_bar_df(context, code: str, gm_symbol: str, now=None) -> pd.DataFrame:
    rows = context.bar_cache.get(gm_symbol, [])
    if len(rows) < MIN_BARS:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "amount"])
    df = df.sort_values("time").reset_index(drop=True)
    # 2026-09-14: 截断到 <= 当前时刻。回测模式下 init 的 history_n(60s×240) 预取会返回
    # **到回测窗口末尾**的 bar（GM 的 count=N 取的是窗口最后 N 根，非"截至当前"），
    # 于是每根 bar 内核都拿着"当日收盘价结尾的全天数据"评估：Renko 砖方向恒 up、m15 恒负
    # → 买入条件 `last_down and m15>0` 永不成立 → 全程 no_signal、0 成交（实测 2867 次评估
    # close 唯一值=[45.29]、m15 恒 -0.037）。实盘时该过滤是无操作（不会有未来 bar）。
    if now is not None:
        try:
            df = df[df["time"].astype(str) <= str(now)].reset_index(drop=True)
        except Exception:
            pass
    if df.empty:
        return df
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
        daily = _sdk_call("history_n_daily200", _partial(
            history_n, symbol=gm_symbol, frequency="1d", count=200,
            fields="eob,open,high,low,close,volume",
            fill_missing="Previous", adjust=ADJUST_PREV,
            end_time=now.strftime("%Y-%m-%d %H:%M:%S")))
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
            # Wilder 平滑（2026-09-21 统一口径）；原 rolling(14).mean() 简单均值
            _up = _d.clip(lower=0).ewm(alpha=1.0 / 14, adjust=False).mean()
            _dn = (-_d.clip(upper=0)).ewm(alpha=1.0 / 14, adjust=False).mean()
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


import sell_state
from sell_state import _sell_state_persist, _sell_state_restore
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
    例外: 若已人工武装，表示用户已确认，跳过上述反绞肉门控。
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
    _armed = bool((getattr(context, "_auto_build_armed", {}) or {}).get(code))
    if not _armed:
        if context.daily_sell_count.get(code, 0):
            return 0
        if getattr(context, "last_index_regime", "range") == "uni_down":
            return 0
    return _short


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
    # 2026-08-31 版本探针：用于确认掘金策略是否加载了含武装/拒单修复的最新 gm_main.py
    print(f"[INIT] gm_main.py 2026-08-31 armed-v2 loaded")
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
    context.board_regime = {}      # A-7: {GM指数sym: regime} 分板 regime（上证=市场级恒有，键 GM 全称）
    context._board_gm_symbols = _needed_index_gm_symbols()  # A-7: 本池所需板块指数（预取/每日刷新共用）
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

    # 人工确认闸状态（2026-08-30 建仓/加仓人工把关）：
    # _buy_confirm_pending = {code: request}；_buy_confirm_rejected = set(code)。
    # MODE_LIVE 下断点续传（date==今日才恢复，跨日/回测不恢复）——引擎重启后
    # 仍兑现「挂起直到确认」与「拒绝后当天不再弹」。
    context._buy_confirm_pending = {}
    context._buy_confirm_rejected = set()
    context._pending_recon = {}    # Fix B: 下单成功待对账 {sym: {...}}
    context._fills_done = set()    # Fix B: fill 防重 key 集合（回调/轮询共用）
    if context.mode == MODE_LIVE:
        try:
            _bkp = read_buy_pending()
            if _bkp and _bkp.get("date") == datetime.now().strftime("%Y-%m-%d"):
                context._buy_confirm_pending = dict(_bkp.get("pending") or {})
                context._buy_confirm_rejected = set(_bkp.get("rejected_today") or [])
            elif _bkp and (_bkp.get("pending") or {}):
                # F3(2026-09-09): 跨日陈旧 pending → 作废清空（自动化应急处置#1）——
                # 防 GUI 幽灵待确认与"引擎重启后 date 不匹配不恢复"的悬空文件
                _stale = list((_bkp.get("pending") or {}).items())
                _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for _c, _req in _stale:
                    try:
                        write_confirm(_ts, _c, "expired",
                                      f"跨日作废 {_req.get('action','')} qty={_req.get('qty')}@"
                                      f"{_req.get('price')}",
                                      request_id=_req.get("request_id"))
                    except Exception:
                        pass
                write_buy_pending({"date": datetime.now().strftime("%Y-%m-%d"),
                                   "updated_at": _ts, "rejected_today": [],
                                   "pending": {}})
                try:
                    from gm_bridge.writer import write_buy_decision as _wbd
                    _wbd({})
                except Exception:
                    pass
                try:
                    write_risk(_ts, "buy_pending_expired",
                               f"跨日作废 {len(_stale)} 条陈旧 pending 已清空(BUY_PENDING/BUY_DECISION)", code="")
                except Exception:
                    pass
                print(f"[init] F3 跨日陈旧 pending 作废: {len(_stale)} 条已清空")
        except Exception:
            pass

    # P3-1(C) 冰点预热：盘前 gm history_n(60s×240) 预取进 bar_cache，消灭开盘 5 分钟指标空窗
    # （对齐方案「盘前用 gm history_n(60s×240) 预取」统一口径；预取覆盖上一交易日 session，
    #  开盘后 subscribe 追加当日 bar，>480 根自动裁剪）。
    for code, sym in STOCKS.items():
        try:
            his = _sdk_call("history_n_60s240", _partial(
                history_n, symbol=sym, frequency="60s", count=240,
                fields="symbol,eob,open,high,low,close,volume,amount",
                fill_missing="Previous", adjust=ADJUST_PREV))
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


# ══════════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════════
# 开盘低开反转 · L3 影子层接线（2026-09-22 施工，owner 审批：先影子层）
# **只记日志、绝不下单、绝不写持仓。** 规则与判据已事前冻结并通过样本外 + 独立池检验：
#   样本外 2019-01~2025-03 +0.5245%/腿（t=8.07）；独立 400 只池 +0.5275%（比 1.01）；
#   退市股子集 +0.7447%（更强）。规格：doc/solutions/2026-09-22_开盘低开反转日内T_设计规格.md
# 决策逻辑全在 core/open_gap_reversal.py（纯函数；L1 24 项测试 + L2 逐腿 diff=0 已过）；
# 本侧只做「取 bar → 组池快照 → 落日志」。加载失败 fail-open = 影子层关闭，绝不影响主循环。
# ══════════════════════════════════════════════════════════════════════
_OGR_GLUE = None
try:
    import ogr_shadow_glue as _OGR_GLUE          # noqa: E402  （execution/auto/ 同目录）
except Exception as _e:
    print(f"[OGR] 影子层胶水加载失败 → 影子层关闭: {_e}")
    _OGR_GLUE = None

_OGR_SHADOW_DONE_DATE = None                     # 每日一次标记（与 _OPEN_ALIGN_DONE_DATE 同款）


def _ogr_shadow_enabled() -> bool:
    """影子层总闸：PARAMS 翻启 + 胶水与决策核均加载成功。**翻启也不下单。**"""
    return (bool(PARAMS.get("open_gap_reversal_shadow_enabled", False))
            and _OGR_GLUE is not None and _OGR_GLUE.available())


# ══════════════════════════════════════════════════════════════════════
# 开盘低开反转 · L4 实单（2026-09-22 施工，owner 指示：**直接下单、跳过影子模式**）
#
# 规则：大盘低开(mkt_gap<0) 且 个股相对低开 ≤ −1% ⇒ 09:31 买 / 10:00 卖（30 分钟持仓）。
# 证据：样本外 2019-01~2025-03 +0.5245%/腿（t=8.07）；独立 400 只池 +0.5275%（比 1.01）；
#       扣执行折价（拿不到竞价价、按 09:31 入场）后 +0.3385%/腿。
#
# ⚠️ 三处刻意的设计取舍（务必知悉）：
#   ① **绕过 `_buy_confirm_gate`（人工确认闸）** —— 规则必须在 09:31 无人值守时下单；
#      与已删除的 B7 接回单同款先例（"直下市价买单 Open——显式绕过确认闸与 morning_no_buy"）。
#   ② **不受 `morning_no_buy`（09:30-09:35 禁买）约束** —— 本路径不经 sig 门链、直下市价单。
#   ③ **额度闸一律复用**（`_stock_budget_cap` / `_check_max_pos_cap`），**不旁路**。
#
# C4 定序：调用点位于 `_force_open_align` 之后 ⇒ **先对齐底仓、后本策略买入**。
# 安全网：若 10:00 卖出失败，腿仍留在 `_ogr_legs`，14:50 的 TAIL 尾盘归位会把它作为
#         「超出底仓的部分」卖掉 —— 不会留过夜（符合「底仓不变」）。
# ══════════════════════════════════════════════════════════════════════
_OGR_LIVE_BUY_DONE_DATE = None
_OGR_BUY_WINDOW = (dtime(9, 31), dtime(9, 35))   # 买入时间窗（防进程盘中启动时误在任意时刻买）
_OGR_SELL_FROM = dtime(10, 0)                    # 10:00 起卖出
_OGR_MAX_LEGS = 10                               # 单日最多腿数（仓位/风险上限）
_OGR_PARTICIPATION = 0.10                        # 单腿不超过该票集合竞价量的比例


def _ogr_live_enabled() -> bool:
    """L4 实单总闸：PARAMS 翻启 + 胶水与决策核加载成功。**默认 off。**"""
    return (bool(PARAMS.get("open_gap_reversal_live_enabled", False))
            and _OGR_GLUE is not None and _OGR_GLUE.available())


# 2026-09-22：回测启用开关（仅 `backtest_holdings.py --ogr` 设置）。
# 生产路径**永不设置** ⇒ 本段在产线是死代码，实盘行为零改变。
_OGR_BACKTEST_ENABLE = os.environ.get("SUPERTRADER_OGR_BACKTEST") == "1"
_OGR_LOG_DIR = None          # None=胶水默认(t_io/logs)；回测由 backtest_holdings 重定向


def _ogr_active(context) -> bool:
    """L4 是否生效：**live**（PARAMS 翻启）**或 回测开关**。

    ⚠️ 回测下 `context.mode == MODE_BACKTEST`，故不能只认 MODE_LIVE —— 否则回测里
    本策略一次都不会触发（这正是回测闭环自检的前提）。
    """
    if not (bool(PARAMS.get("open_gap_reversal_live_enabled", False))
            and _OGR_GLUE is not None and _OGR_GLUE.available()):
        return False
    _mode = getattr(context, "mode", None)
    return (_mode == MODE_LIVE) or (_OGR_BACKTEST_ENABLE and _mode == MODE_BACKTEST)


def _ogr_legs(context) -> dict:
    """当日 T 腿台账 {code: {gm_symbol, qty, buy_px, buy_time, date}}。"""
    d = getattr(context, "_ogr_legs", None)
    if not isinstance(d, dict):
        d = {}
        context._ogr_legs = d
    return d


def _ogr_sym_of(context, code: str):
    """6 位码 → gm symbol（沿用既有 STOCKS 映射）。"""
    try:
        return STOCKS.get(code)
    except Exception:
        return None


def _ogr_last_px(context, sym: str) -> float:
    """该 symbol 的最新参考价：优先账户/信号价，回退 bar_cache 末根收盘。"""
    try:
        mp = (getattr(context, "manual_position", {}) or {}).get(sym) or {}
        p = float(mp.get("price") or 0)
        if p > 0:
            return p
    except Exception:
        pass
    try:
        bars = (getattr(context, "bar_cache", {}) or {}).get(sym) or []
        if bars:
            return float(bars[-1].get("close") or 0)
    except Exception:
        pass
    return 0.0


_OGR_LEG_NOTIONAL = 100000.0        # 单腿目标金额（owner 单笔 ≥10 万；容量表 10 万可覆盖 78~95%）


def _ogr_prev_close_map(context, codes) -> dict:
    """{code: 前一交易日收盘} —— 取自 `bar_cache` 末根。

    ⚠️ **不可用 `holdings.json::pre_close`**：那是"当前"值（superTrader 14:59 写入），
    回测里拿它算历史某日的 gap 会得到完全错误的信号；live 下也依赖 superTrader 当日已写。
    `bar_cache` 在**盘前预热**（`:1620` history_n 60s×240）时就已填到前一交易日的最后一根，
    且本触发点位于逐票循环**之前**（循环尚未 append 今日首根）⇒ 末根 = 前一交易日收盘 ✓。
    取不到则跳过该票（fail-closed，不猜）。
    """
    out = {}
    bc = getattr(context, "bar_cache", None) or {}
    for code in codes:
        try:
            sym = _ogr_sym_of(context, code)
            rows = bc.get(sym) or []
            if rows:
                c = float(rows[-1].get("close") or 0)
                if c > 0:
                    out[code] = c
        except Exception:
            continue
    return out


def _ogr_size(context, code: str, cp: float, avail_cash: float, sym: str) -> int:
    """单腿规模：目标 10 万，封顶到剩余现金与既定仓位口径；取整到 100 股。

    刻意**不**复用 `context.sizer`（那是做T的按比例口径，需要 sig），本规则是固定金额口径。
    """
    if cp <= 0:
        return 0
    _n = min(_OGR_LEG_NOTIONAL, max(0.0, avail_cash * 0.95))
    return int(_n / cp / 100) * 100


def _ogr_first_oid(orders):
    """从 order_volume 返回（List[Dict]）取首个 id。"""
    try:
        for _o in (orders if isinstance(orders, list) else [orders]):
            if isinstance(_o, dict):
                _i = _o.get("id") or _o.get("order_id") or _o.get("cl_ord_id")
                if _i:
                    return _i
    except Exception:
        pass
    return None


def _ogr_try_buy(context, bars, now) -> int:
    """09:31 开盘低开反转买入。返回已下单腿数。**下单前逐项过既有额度闸。**"""
    if _OGR_GLUE is None:
        return 0
    hmap = _OGR_GLUE.read_holdings()
    _codes = [c for c in hmap
              if isinstance(hmap.get(c), dict)
              and str(hmap[c].get("pool", "")) in ("auto", "both")]
    rec = _OGR_GLUE.decide(bars, hmap, now,
                           prev_close_map=_ogr_prev_close_map(context, _codes))
    if rec is None:
        return 0
    _OGR_GLUE.append_log({**rec, "layer": "L4_live", "phase": "buy_decision"},
                         _OGR_LOG_DIR)
    tradable = list(rec.get("tradable") or [])[:_OGR_MAX_LEGS]
    if not tradable:
        return 0

    _acct = None
    try:
        _acct = _sdk_call("account", context.account)
    except Exception:
        _acct = None
    _avail = 0.0
    try:
        _c = getattr(_acct, "cash", None)
        _c = _c() if callable(_c) else _c
        _avail = float(getattr(_acct, "available", None) or _c or 0.0)
    except Exception:
        _avail = 0.0
    if _avail <= 0:
        _audit_write({"event": "ogr_buy_skip", "reason": "no_cash", "time": str(now)})
        return 0

    _total_eq = _total_equity(context, _avail)
    n = 0
    for code in tradable:
        try:
            sym = _ogr_sym_of(context, code)
            if not sym:
                continue
            h = _get_holding(context, code, sym)
            pos_qty = int(h.get("qty", 0) or 0)
            cp = _ogr_last_px(context, sym)
            if not (cp > 0):
                continue
            base_ref = int(getattr(context, f"_base_ref_{code}", 0) or 0)
            budget, max_shares = _stock_budget_cap(context, code, cp, _total_eq)
            if _check_max_pos_cap(context, code, now, pos_qty, base_ref, max_shares,
                                  budget, _total_eq, action="OGR_BUY", t_headroom=0):
                continue                                   # 额度闸拦截（已留痕）
            qty = _ogr_size(context, code, cp, _avail, sym)
            if qty < 100:
                _audit_write({"event": "ogr_buy_skip", "code": code, "reason": "size_lt_100",
                              "time": str(now)})
                continue
            if _limit_clamp_should_skip(context, code, sym, OrderSide_Buy, qty, cp, now,
                                        "ogr_buy"):
                continue
            _o = _sdk_call("order_volume", _partial(
                order_volume, symbol=sym, volume=qty, side=OrderSide_Buy,
                order_type=OrderType_Market, position_effect=PositionEffect_Open))
            _oid = _ogr_first_oid(_o)
            _audit_write({"event": "ogr_buy", "code": code, "gm_symbol": sym, "qty": qty,
                          "px_bar_open": rec["rows"][0].get("bar_open") if rec.get("rows") else None,
                          "ref_px": cp, "order_id": _oid, "time": str(now)})
            _ogr_legs(context)[code] = {"gm_symbol": sym, "qty": qty, "buy_px": cp,
                                        "buy_time": str(now), "date": rec["date"]}
            n += 1
        except Exception as _e:
            print(f"[OGR] 买入异常 {code}: {_e}")

    if n:
        print(f"[{now:%H:%M:%S}] [OGR] 开盘低开反转买入 {n} 腿 "
              f"(mkt_gap={rec.get('mkt_gap')})")
    return n


def _ogr_try_sell(context, now) -> int:
    """10:00 起平掉当日 T 腿（按买入原量卖回）。返回已下单腿数。"""
    legs = _ogr_legs(context)
    if not legs:
        return 0
    n = 0
    for code, leg in list(legs.items()):
        try:
            sym = leg.get("gm_symbol")
            qty = int(leg.get("qty", 0) or 0)
            if not sym or qty < 100:
                legs.pop(code, None)
                continue
            h = _get_holding(context, code, sym)
            avail = h.get("available")
            avail = int(h.get("qty", 0)) if avail is None else int(avail)
            _tif = int(getattr(context, "_inflight_sell", {}).get(sym, 0) or 0)
            sell_qty = (min(qty, max(0, avail - _tif)) // 100) * 100
            if sell_qty < 100:
                _audit_write({"event": "ogr_sell_skip", "code": code,
                              "reason": "avail_lt_100", "avail": avail, "time": str(now)})
                continue
            cp = _ogr_last_px(context, sym)
            if _limit_clamp_should_skip(context, code, sym, OrderSide_Sell, sell_qty, cp, now,
                                        "ogr_sell"):
                continue
            _o = _sdk_call("order_volume", _partial(
                order_volume, symbol=sym, volume=sell_qty, side=OrderSide_Sell,
                order_type=OrderType_Market, position_effect=PositionEffect_Close))
            _audit_write({"event": "ogr_sell", "code": code, "gm_symbol": sym,
                          "qty": sell_qty, "leg_qty": qty, "buy_px": leg.get("buy_px"),
                          "ref_px": cp, "order_id": _ogr_first_oid(_o), "time": str(now)})
            legs.pop(code, None)
            n += 1
        except Exception as _e:
            print(f"[OGR] 卖出异常 {code}: {_e}")
    if n:
        print(f"[{now:%H:%M:%S}] [OGR] 开盘低开反转卖出 {n} 腿")
    return n


def on_bar(context, bars):
    # 模块级"每日一次"标记（Python 要求 global 声明位于函数内任何使用之前）
    global _OGR_LIVE_BUY_DONE_DATE, _OGR_SHADOW_DONE_DATE
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
        # 2026-09-22 开盘低开反转（L4）：按日清空 T 腿台账 + 买入一次标记
        _OGR_LIVE_BUY_DONE_DATE = None
        context._ogr_legs = {}
        _audit_write({"event": "date_reset", "date": str(today)})
        # 人工确认闸按日重置：作废旧 pending（留痕 expired）+ 清当日拒绝 + 重写空请求文件
        _old_pending = dict(getattr(context, "_buy_confirm_pending", {}) or {})
        if _old_pending or getattr(context, "_buy_confirm_rejected", set()):
            try:
                for _c, _req in _old_pending.items():
                    write_confirm(str(now), _c, "expired",
                                  detail=(f"跨日作废 {_req.get('action', '')} "
                                          f"qty={_req.get('qty')}@{_req.get('price')}"),
                                  request_id=_req.get("request_id"))
            except Exception:
                pass
        context._buy_confirm_rejected = set()
        context._buy_confirm_pending = {}
        try:
            write_buy_pending({"date": str(today),
                               "updated_at": f"{now:%Y-%m-%d %H:%M:%S}",
                               "rejected_today": [], "pending": {}})
        except Exception:
            pass

    # ── KILL_SWITCH 检查 ──
    _killed = check_kill_switch()

    # 人工建仓武装标记（2026-08-30 手动建仓→做T衔接）：GUI 手动确认建仓/加仓后写 AUTO_BUILD.json，
    # 本 bar 起对武装标的跳过 BASE 确认闸（不再重复弹窗），直接走完整闸链建仓→成交→做T。
    # 每根 bar 读一次存 context（避免 17 票每票读文件）；下单成功后消费（一次性）。
    try:
        context._auto_build_armed = read_auto_build().get("requests") or {}
    except Exception:
        context._auto_build_armed = {}

    # P0-4(2026-09-01): 日内巡检——confirm 已到但信号未再触发导致 pending 悬空 → 超时作废留痕
    try:
        _scan_pending_confirm(context, now)
    except Exception:
        pass

    # 2026-09-15 阶段0-3（诊断D3）：BUY_PENDING 盘中超时看门狗——挂起无人确认超时自动作废留痕
    try:
        _scan_pending_unanswered(context, now)
    except Exception:
        pass

    # 双向看门狗：watcher 心跳缺失/过期自动重生（0806 红日整改）
    ops_guard.ensure_watcher(PROJECT_DIR)

    if t < dtime(9, 30) or (dtime(11, 30) < t < dtime(13, 0)) or t > dtime(15, 0):
        return

    # ── 持仓真源回写（2026-09-14 并表）：收盘后一次，把账户实际 qty/cost 写回 holdings.json ──
    # 取 14:57 每日一次（on_bar 在 15:00 后 return，没有更晚的钩子）。与 superTrader 14:59 的
    # pre_close 写并发也安全：本侧是"磁盘为基 + 只补丁 qty/cost"，且对方读后再写，两个方向都不丢。
    # ── 开盘强制对齐（2026-09-14 owner 裁决）：每个交易日一次，把实际持仓拉到目标底仓 ──
    global _OPEN_ALIGN_DONE_DATE
    if (t >= dtime(9, 31) and _OPEN_ALIGN_DONE_DATE != today
            and getattr(context, "mode", None) == MODE_LIVE):
        _OPEN_ALIGN_DONE_DATE = today
        try:
            _force_open_align(context)
        except Exception as _oae:
            print(f"[OPEN_ALIGN] 失败（不阻断主循环）: {_oae}")

    # ── 开盘低开反转 L3 影子层（2026-09-22）：每个交易日一次，只记日志、不下单 ──
    # 时点同 _OPEN_ALIGN：t >= 09:31 时本轮 on_bar 的 bars 即当日**第一根 60s bar**，
    # 其 open 应等于集合竞价价（这正是 L3 要在真实 bar 流上验证的第一件事）。
    # 位置在逐票循环**之前**，故不依赖 context.latest_pre_close 是否已填。
    if (_ogr_shadow_enabled() and _OGR_BUY_WINDOW[0] <= t <= _OGR_BUY_WINDOW[1]
            and _OGR_SHADOW_DONE_DATE != today
            and getattr(context, "mode", None) == MODE_LIVE):
        _OGR_SHADOW_DONE_DATE = today
        try:
            _ogr_rec = _OGR_GLUE.run_shadow(bars, _OGR_GLUE.read_holdings(), now)
            if _ogr_rec:
                print(f"[OGR] 影子 {today} mkt_gap={_ogr_rec.get('mkt_gap')} "
                      f"池={_ogr_rec.get('pool_n')} 触发={_ogr_rec.get('n_tradable')} "
                      f"{_ogr_rec.get('tradable')}")
        except Exception as _oe:
            print(f"[OGR] 影子层失败（不阻断主循环）: {_oe}")

    # ── 开盘低开反转 L4 实单（2026-09-22）：09:31 买（每日一次）／10:00 起卖 ──
    # C4 定序：本块位于 `_force_open_align` **之后** ⇒ 先对齐底仓、后本策略买入。
    # 时点同 L3：t >= 09:31 时本轮 bars 即当日第一根 60s bar（其 open 应≈集合竞价价，
    # 该假设由 L4 日志里的 px_bar_open / 实际成交回报对照验证）。
    if _ogr_active(context):
        if (_OGR_BUY_WINDOW[0] <= t <= _OGR_BUY_WINDOW[1]
                and _OGR_LIVE_BUY_DONE_DATE != today):
            _OGR_LIVE_BUY_DONE_DATE = today
            try:
                _ogr_try_buy(context, bars, now)
            except Exception as _oe:
                print(f"[OGR] 实单买入失败（不阻断主循环）: {_oe}")
        elif t >= _OGR_SELL_FROM:
            try:
                _ogr_try_sell(context, now)
            except Exception as _oe:
                print(f"[OGR] 实单卖出失败（不阻断主循环）: {_oe}")

    global _WB_DONE_DATE
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

        df = _build_bar_df(context, code, gm_sym, now=now)
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
            # 2026-08-31 修复：GUI 手动武装建仓/加仓，表示用户已人工确认，应跳过自动闸直接执行。
            # 此标记在 build_decision 等闸之前读取，确保「武装」真正生效而不只是跳过确认弹窗。
            _armed = bool((getattr(context, "_auto_build_armed", {}) or {}).get(code))
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
                if _armed:
                    print(f"[{now:%H:%M:%S}] BASE {code} 已人工武装→跳过硬止损日禁")
                else:
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
                if _armed:
                    print(f"[{now:%H:%M:%S}] BASE {code} 已人工武装→跳过 TREND_BREAKDOWN 延迟")
                else:
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
                if _armed:
                    print(f"[{now:%H:%M:%S}] BASE {code} 已人工武装→跳过槽满限制")
                else:
                    _emit_slot_full(context, code, now, "base")
                    if not _is_topup:
                        return
                    _topup_blocked = True
            # 默认 False: 数据不足时保守不放行（F5: 恢复M2门槛）
            if not _topup_blocked and not _dc.get("_m2_pool_pass", False):
                if _armed:
                    print(f"[{now:%H:%M:%S}] BASE {code} 已人工武装→跳过 M2 门槛未过")
                else:
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
                    # A-7: 建仓时机闸按个股所属板取对应指数（60→上证/688·588→科创50/300→创业板/00x→深成），
                    # 不再恒传上证。板指数缺失 fail-closed（数据不足不建仓），不回退上证冒充。
                    import analysis.index_regime as _ir
                    _ic, _ig = _code_board_index(code)
                    _idx_df = _ir.GM_INDEX_CACHE.get(_ig)
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
                    if _armed:
                        print(f"[{now:%H:%M:%S}] BASE {code} 已人工武装→跳过 build_decision({_bd_verdict}) 直接建仓")
                        _topup_blocked = False
                    else:
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
                # 2026-08-30 手动建仓/加仓衔接：GUI 已人工确认（武装标记）→ 跳过确认闸，直接走完整闸链建仓→做T
                # _armed 已在上方 BASE 块开头读取
                _needs_confirm = (not _is_topup) or PARAMS.get("human_confirm_base_topup", True)
                if _armed:
                    _needs_confirm = False
                _cg = _buy_confirm_gate(
                    context, code, now, action="BASE", price=cp, qty_proj=base_qty,
                    pos_qty=(0 if not _is_topup else int(mirror.get("qty", 0) or 0)),
                    reasons=["初始建仓" if not _is_topup else "镜像底仓 F7回补"],
                    needs_confirm=_needs_confirm,
                    kind=("build" if not _is_topup else "topup"))
                if _cg in ("pending", "rejected_today"):
                    # 待人工确认/当日已拒绝：不下单。topup 沿用 F8 语义继续信号评估（可卖）；
                    # 初始建仓 pos_qty<=0 走下方 return 自然退出本标的本 bar（非阻塞挂起等待）。
                    _topup_blocked = True
                else:
                    # 2026-09-15 阶段0-6（诊断D3旁注）：贴涨停买单=确定性拒单，跳过并留痕。
                    # 不消费 armed 标记、不进 _base_ordered——下一根 bar 价格离开贴板带可重试（同拒单语义）。
                    if _limit_clamp_should_skip(context, code, gm_sym, "BUY", base_qty, cp, now, "base"):
                        return
                    try:
                        try:
                            write_order(str(now), code, "BUY", base_qty, cp, order_id="base")
                        except Exception:
                            pass
                        _base_orders = _sdk_call("order_volume_base", _partial(
                            order_volume, symbol=gm_sym, volume=base_qty,
                            side=OrderSide_Buy,
                            order_type=OrderType_Market,
                            position_effect=PositionEffect_Open))
                        # 掘金 SDK order_volume 同步返回 List[Dict]；status=8 等表示拒单，
                        # 不能当成已下单，否则武装标记会被误消费且 N5 重试也会丢标记。
                        _base_first = _base_orders[0] if isinstance(_base_orders, list) and _base_orders else {}
                        _base_status = _base_first.get("status") if isinstance(_base_first, dict) else None
                        if _base_status in (4, 5, 6, 8, 12):
                            _rej_detail = _base_first.get("ord_rej_reason_detail", "") or ""
                            print(f"[{now:%H:%M:%S}] BASE {code} 下单被拒 status={_base_status} {_rej_detail}")
                            try:
                                write_risk(str(now), "order_rejected",
                                           f"BASE BUY {base_qty}@{cp:.2f} status={_base_status} {_rej_detail}", code=code)
                            except Exception:
                                pass
                            # 不加入 _base_ordered，不消费武装标记，下一根 bar  armed 仍在，继续尝试
                        else:
                            context._base_ordered.add(code)
                            _mark_pending_recon(context, code, gm_sym, "BUY", base_qty, cp, _base_orders)
                            if _armed:
                                # 人工建仓武装标记：下单成功即消费（一次性），防止损离场后残留标记自动无确认重入
                                try:
                                    consume_auto_build(code)
                                except Exception:
                                    pass
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
        # 注入指数态势（market 级字段保持现状；C-2/C-3 数据就位：按股补所属板块 regime 归因，决策开关留周六对照）
        daily_ctx["index_circuit_state"] = "clear" if context.last_index_regime == "uni_down" else "normal"
        daily_ctx["index_gate_advice"] = "defensive_t" if context.last_index_regime == "uni_down" else "normal_t"
        try:
            _bic, _big = _code_board_index(code)
            _brv = (getattr(context, "board_regime", {}) or {}).get(_big) or context.last_index_regime
            daily_ctx["index_board_code"] = _bic
            daily_ctx["index_board_regime"] = _brv      # 该股所属板块 regime（上证=市场级恒有）
            daily_ctx["index_regime"] = daily_ctx.get("index_regime") or context.last_index_regime
        except Exception:
            pass

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

        # 2026-09-22：原「尾盘强制回补（数量不变硬约束）」已随做T引擎删除——
        # 该约束是反T回补义务（awaiting_buyback）的兜底，义务机制已整删，故无回补可做。
        # 尾盘「归位卖出」（TAIL，把持仓还原到目标底仓）仍在下方门链内，未受影响。

        # ── P0-P6 卖出通道门链（P4-1 迁至 sell_channels._sell_channel_gate，行为逐字一致）──
        feats_cache = getattr(context.engine, "_last_feats", {}).get(code, {})
        sig, tail_done = sell_channels._sell_channel_gate(
            context, code, gm_sym, cp, now, sig, pos_qty, holding, daily_ctx,
            feats_cache, is_tail, morning_no_buy)
        if tail_done:
            continue

        if sig is None:
            # P0-4(2026-09-01): 信号褪化留痕——confirm 已到达但信号消失/score 掉阈，
            # pending 不再有消费机会 → 作废并留痕 signal_faded（防悬空）
            if code in getattr(context, "_buy_confirm_pending", {}):
                try:
                    _d_fade = (read_buy_decision().get("decisions") or {}).get(code)
                    _pend_fade = context._buy_confirm_pending.get(code)
                    if _d_fade and _pend_fade and _d_fade.get("request_id") == _pend_fade.get("request_id") \
                            and _d_fade.get("decision") == "confirm":
                        context._buy_confirm_pending.pop(code, None)
                        write_confirm(str(now), code, "signal_faded",
                                      detail="confirm 已到但信号褪化，pending 作废",
                                      request_id=_pend_fade.get("request_id"),
                                      action=_pend_fade.get("action"))
                        write_buy_pending({"date": f"{now:%Y-%m-%d}",
                                           "updated_at": f"{now:%Y-%m-%d %H:%M:%S}",
                                           "rejected_today": sorted(context._buy_confirm_rejected),
                                           "pending": context._buy_confirm_pending})
                except Exception:
                    pass
            _last_dec = context.engine.last_decision.get(code, {})
            # 2026-09-22：原「高接延迟事件（WP-B07）」已随做T引擎删除
            # （buyback_above_sell_delayed 不再产生）。
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
            # 人工确认闸（2026-08-30 建仓/加仓人工把关）：加仓/信号建仓 → 弹窗确认后才下单。
            # pending/当日已拒绝 → 本 bar 跳过（非阻塞挂起）。位于信号级闸之后（无假弹窗）、
            # 容量闸（slot/cash/sizer/pos_limit）之前（确认放行时全量重查）。
            # 2026-09-22：原「做T回补记忆态豁免弹窗 / 回补路径绕过确认闸」两分支已随做T引擎删除
            # （awaiting_buyback 恒空 ⇒ 回补豁免永不适用，一律走确认闸）。
            _cg = _buy_confirm_gate(
                context, code, now, action=sig.action, price=cp,
                qty_proj=_project_buy_qty(context, code, holding, sig, threshold, pos_qty),
                pos_qty=pos_qty, reasons=list(getattr(sig, "reasons", []) or []),
                needs_confirm=True)
            if _cg in ("pending", "rejected_today"):
                continue

            # N3: 现金预检（移到 sizer 之前，供 target_t 计算）
            available_cash = INITIAL_CASH
            _cash_ok = False
            try:
                _acct = _sdk_call("account", context.account)
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
            # P0-1(2026-09-10 修): 原来把个股预算上限 max_pos_shares(≈20400) 当 sizer 目标仓位传入
            # → 目标虚高、按 30% 出量失控（600481 BUY 6100）。目标应为「底仓+一档T」，上限防护由
            # 下方 _check_max_pos_cap 承担。
            target_t = max(_base_ref + _t_head, pos_qty)
            holding_with_target = dict(holding, target_t=target_t)

            # WP-E2: 个股最大仓位闸——到顶直接拦截（堵 sizer 内部 1.5× 兜底洞）
            if _check_max_pos_cap(context, code, now, pos_qty, _base_ref,
                                  max_pos_shares, _stock_budget, _total_eq,
                                  action=sig.action, t_headroom=_t_head):
                continue

            qty = context.sizer.calc_buy_qty(code, holding_with_target, sig.score, threshold)
            if qty <= 0:
                # WP-E2: 已有持仓 sizer 返回 0 = 已到个股上限 → max_pos_cap（pos_qty 恒>0，无 300 兜底分支）
                _check_max_pos_cap(context, code, now, pos_qty, _base_ref,
                                   max_pos_shares, _stock_budget, _total_eq,
                                   force=True, action=sig.action, t_headroom=_t_head)
                continue

            # 2026-09-22：回补量硬帽（_buyback_cap_qty）与「高接降档」（_apply_buyback_downgrade）
            # 已随做T引擎删除——两者都只作用于反T回补（awaiting_buyback 记忆态），该记忆态恒空。

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
            # 2026-09-15 阶段0-6（诊断D3旁注）：贴涨停买单=确定性拒单，跳过并留痕（不下单、不计数）
            if _limit_clamp_should_skip(context, code, gm_sym, "BUY", qty, cp, now, "buy"):
                continue
            try:
                write_order(str(now), code, "BUY", qty, cp)
            except Exception:
                pass
            try:
                _oid = _sdk_call("order_volume_buy", _partial(
                    order_volume, symbol=gm_sym, volume=qty,
                    side=OrderSide_Buy,
                    order_type=OrderType_Market,
                    position_effect=PositionEffect_Open))
                _mark_pending_recon(context, code, gm_sym, "BUY", qty, cp, _oid)
                # 2026-09-14: 把本笔 T 腿**实际下单量**记回 entry——平腿按原量卖，保证数量不变。
                # ⚠️ t_entry_price 在**决策核**上（context.engine 是适配器 SignalEngine，
                #    内核是它的 ._core）；早先误写成 context.engine.t_entry_price → getattr
                #    恒返回 {} → qty 从未记录 → 下游 t_lot_qty 恒 0、平腿按原量卖与成本锚豁免双双空转。
                if sig.action in ("BUY_LOW", "ADD_POS"):
                    _core = getattr(context.engine, "_core", None)
                    _ent = (getattr(_core, "t_entry_price", {}) or {}).get(code) if _core else None
                    if isinstance(_ent, dict):
                        _ent["qty"] = int(qty)
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
                # 2026-09-22：原「降档成交事件（WP-B07）」已随做T引擎删除。
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
    if price <= 0:
        price = context.latest_pre_close.get(code, 0)
    side = order["side"]

    # F9: 在途卖单释放（成交/拒单/撤单/过期均归还额度）
    if side == 2 and status in (3, 4, 5, 6, 8, 12):
        _ifl = getattr(context, "_inflight_sell", None)
        if _ifl and symbol in _ifl:
            _ifl[symbol] = max(0, int(_ifl[symbol]) - int(volume))

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
        # WP-B15: 持仓变化（成交）→ 解除信号 mute / 地板去重键（单点清理，防解封后忘清键）
        _clear_signal_mute_keys(context, code)
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
            # （非期望价，见 :2728 既有注释），slippage=fill_vwap-order_price 的方向解读归下游；
            # 无真实值的字段传 None（writer 落 null，禁止编造）；0 值经 or None 归一防假滑点。
            write_fill(str(datetime.now()), _raw_code(symbol), _side, volume, price,
                       order_id=str(order.get("id") or ""), pos_after=_pos_after,
                       fee=_fee, fee_source=_fsrc,
                       order_price=(order.get("price") or None),
                       fill_vwap=(order.get("filled_vwap") or order.get("vwap") or None))
        except Exception:
            pass
        _pending_recon_close(context, symbol)   # Fix B: 该 symbol 已完成对账，轮询不再兜底
        # P0-2: 成交回调接线冷却（只有真的成交了才计冷却，避免下单即计）
        # WP-B07: 捕获返回值——卖成交建回补记忆(armed) / 买成交清记忆(buyback_filled)
        _rta = None
        if code in STOCKS:
            _action = 'BUY_LOW' if side == 1 else 'SELL_HIGH'
            _rta = context.engine.record_trade_action(code, _action, volume, price)
            # 2026-09-22：原「HARD_STOP_EXIT / T_LEG_CLOSE 不生成回补记忆」两个清除分支
            # 已随做T引擎删除（回补记忆机制整体不存在了）。
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
            # 2026-09-22：原「买入成交 → 回补闭环完成写 filled 事件」已随做T引擎删除。
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
            # 2026-09-22：原「卖出成交 → 建立回补价格记忆并写 armed 事件」已随做T引擎删除。
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
    # F1(2026-09-09): run() 正常返回分支横幅（区分优雅结束 vs 硬 kill）
    print("===== strategy run 返回（正常结束）=====", flush=True)
    try:
        write_risk(datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "strategy_exit", "策略 run() 返回（优雅结束）")
    except Exception:
        pass
