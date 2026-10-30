# -*- coding: utf-8 -*-
"""gm_main.on_bar 时点钩子的**离线段驱动**（由 test_gm_main_osc_hooks 以子进程方式调用）。

为什么是独立进程 + 独立文件：
  · gm_main.py `from gm.api import *`，仓里没有 gm SDK ⇒ 必须桩掉 `gm.api` 才能 import；
    桩模块塞进 sys.modules 会污染同进程的其它测试 ⇒ **子进程隔离**。
  · 不叫 test_*.py ⇒ unittest discover 不会直接收它。

它做什么：桩掉 gm.api（订阅/取数返空、**下单函数一旦被调用即断言失败**）→ 真 import gm_main
→ `init(fake_ctx)` → 在若干时刻打 `on_bar`，把四个时点钩子与 `_writeback_holdings` 换成记录器，
最后打印一行 `RESULT <json>` 供测试断言。**不下单、不取数、不写生产目录**
（事件桥/审计全部重定向到临时目录）。
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime

ROOT = os.environ.get("SMOKE_ROOT") or r"E:\superTrader"
TMP = tempfile.mkdtemp(prefix="gm_hook_smoke_")
os.environ["GM_BRIDGE_DIR"] = TMP
os.environ["SUPERTRADER_ROOT"] = ROOT

# ── 桩 gm.api ──
gm_api = types.ModuleType("gm.api")
for k, v in {
    "MODE_LIVE": 1, "MODE_BACKTEST": 2,
    "ADJUST_PREV": 1, "ADJUST_NONE": 0,
    "OrderSide_Buy": 1, "OrderSide_Sell": 2,
    "OrderType_Market": 1, "OrderType_Limit": 2,
    "PositionEffect_Open": 1, "PositionEffect_Close": 2,
    "PositionSide_Long": 1,
}.items():
    setattr(gm_api, k, v)


def _unimpl(*a, **k):
    raise AssertionError("冒烟期不应触到下单/撮合类 SDK 调用: %r %r" % (a, k))


for _fn in ("order_volume", "get_instruments"):
    setattr(gm_api, _fn, _unimpl)
# init 需要的空数据源（否则 init 走不完）
gm_api.subscribe = lambda **k: None
gm_api.history_n = lambda **k: []
gm_api.history = lambda **k: []
gm_api.current = lambda *a, **k: []
gm_api.get_orders = lambda *a, **k: []
gm_api.run = _unimpl
sys.modules["gm.api"] = gm_api

sys.path.insert(0, os.path.join(ROOT, "execution", "auto"))
import importlib.util                                                  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "gm_main", os.path.join(ROOT, "execution", "auto", "gm_main.py"))
GM = importlib.util.module_from_spec(spec)
sys.modules["gm_main"] = GM
spec.loader.exec_module(GM)

# ops_guard 桩掉（否则 init 会往 execution/auto/logs 写运维日志）
GM.ops_guard = type("_OpsGuard", (), {"bootstrap_logging": lambda *a, **k: None,
                                      "ensure_watcher": lambda *a, **k: None})()
# 审计双写重定向到临时目录
GM._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace.jsonl")
GM._AUDIT_MIRROR_PATH = os.path.join(TMP, "auto_backtrace.jsonl")
# 别让进程退出时写 risk:strategy_exit
import atexit                                                          # noqa: E402
try:
    atexit.unregister(GM._gm_atexit_banner)
except Exception:
    pass

# ── 钩子换成记录器（只验"闸 + 定序"，不验钩子内部）──
# 2026-10-12 策略归拢后：四个钩子在策略包的 hooks 模块里；`hooks.on_bar` 按模块全局查表
# ⇒ 替换 `OSC.hooks.<name>` 即可被记录器接住。写入侧仍在宿主 gm_main。
OSC = sys.modules["oversold_gap_confirm"]
calls = []
for _n in ("prepare", "try_buy", "try_sell", "try_morning_sell"):
    setattr(OSC.hooks, _n, (lambda n: (lambda *a, **k: calls.append(n)))(_n))
setattr(GM, "_writeback_holdings", lambda *a, **k: calls.append("_writeback_holdings"))
# P0-3（2026-10-26）：init 阶段的盘前预筛主路径置 no-op——本驱动只验「闸 + 定序」，
# 不能让它在 init 里真跑全市场批量取数；prescreen_init 自身行为由 test_osc_prescreen_init 覆盖。
OSC.prescreen_init = lambda *a, **k: False


class _Pos(object):
    """假持仓对象（网关走 getattr 分支）。"""
    def __init__(self, sym, vol, avail=0, vwap=0.0):
        self.symbol, self.volume, self.available, self.vwap = sym, vol, avail, vwap


class FakeAccount(object):
    def __init__(self, positions=None, cash=1_000_000.0):
        self._pos = positions or {}
        self._cash = cash

    def cash(self):
        return {"available": self._cash}

    def positions(self, symbol=None, side=None):
        if symbol:
            v = self._pos.get(symbol)
            return [_Pos(symbol, v[0], v[1], v[2])] if v else []
        return [_Pos(s, v[0], v[1], v[2]) for s, v in self._pos.items()]


class FakeCtx(object):
    def __init__(self, mode=1, positions=None, cash=1_000_000.0):
        self.mode = mode
        self.now = None
        self.account = (lambda: FakeAccount(positions, cash))


def run_at(hh, mm, ctx, day=12):
    ctx.now = datetime(2026, 10, day, hh, mm)
    n0 = len(calls)
    GM.on_bar(ctx, [])
    return calls[n0:]


out = {"stock_n": len(GM.STOCKS), "cases": {}}
live = FakeCtx(GM.MODE_LIVE)
GM.init(live)
for label, hh, mm, day in [
    ("0931_first", 9, 31, 12), ("0931_second", 9, 31, 12), ("0932", 9, 32, 12),
    ("1000_first", 10, 0, 12), ("1000_second", 10, 0, 12),
    ("1450", 14, 50, 12), ("1457", 14, 57, 12),
    ("1505_oow", 15, 5, 12), ("1200_lunch", 12, 0, 12),
    ("d2_0931", 9, 31, 13), ("d2_1000", 10, 0, 13),
]:
    out["cases"][label] = run_at(hh, mm, live, day)

bt = FakeCtx(GM.MODE_BACKTEST)
GM.init(bt)
out["cases"]["bt_1000"] = run_at(10, 0, bt, 12)

# 迟到启动：首根 bar 直接 10:00，D4 必须先刷新 uni_down，再进入 OSC 卖→买钩子。
late = FakeCtx(GM.MODE_LIVE)
GM.init(late)
import analysis.index_regime as _ir                                # noqa: E402
_ir.GM_DATA_READY = True
GM._index_daily_df = lambda *a, **k: None


def _fake_detect_index_regime(**kwargs):
    calls.append("regime_refresh")
    return type("_Regime", (), {"value": "uni_down"})(), 0.0, {"degraded": []}


_ir.detect_index_regime = _fake_detect_index_regime
out["cases"]["late_1000"] = run_at(10, 0, late, 14)
out["late_regime"] = late.last_index_regime
out["cases"]["late_1450"] = run_at(14, 50, late, 14)

# ── D 段：网关整链（SERVICES 注入是否正确 + 归属硬锁端到端）─────────────
# 用真 getattr 的假 order_volume 替换「一旦调用即失败」的桩，并给账户塞一只持仓。
placed = []


def _rec_order_volume(**kw):
    placed.append(kw)
    return [{"id": "T1", "status": 0}]


gm_api.order_volume = _rec_order_volume
# `_GmPrimitives` 在类定义时就把 gm.api 的 order_volume 绑成 staticmethod ⇒ 必须改类属性
# （改 GM.order_volume 或 gm_api 都不生效——这正是"注入点在类定义时固定"的表现）。
GM._GmPrimitives.order_volume = staticmethod(_rec_order_volume)

# 让超跌跳空确认"账本里持有 600123"（归属证据来自账本，不是状态文件）
osc_book = os.path.join(TMP, "osc_book")
os.makedirs(osc_book, exist_ok=True)
with open(os.path.join(osc_book, "positions.json"), "w", encoding="utf-8") as f:
    json.dump({"updated_at": "", "open": {"600123": {"qty": 500, "entry_price": 9.0}}}, f)
GM._OSC_BOOK_DIR = osc_book
GM.OWNERSHIP.reload_overrides()

gw_ctx = FakeCtx(GM.MODE_LIVE, positions={"SHSE.600234": (300, 300, 9.5)})   # 账户持有、无人认领
gw = OSC.gateway.OscGateway(gw_ctx, GM.SERVICES, log=lambda *a: None)
out["gw"] = {
    "own_book_code": gw.place_order("600123", "BUY", 100, 10.0)["status"],
    "unclaimed_held": gw.place_order("600234", "BUY", 100, 10.0),
    "free": gw.place_order("600345", "BUY", 100, 10.0)["status"],
    "sell_owned": gw.place_order("600123", "SELL", 500, 10.0)["status"],
    "broker_calls": [k["symbol"] for k in placed],
}
# 事件桥：被锁的 600234 绝不能留下 order 事件（否则孤儿闸误判）
ev = os.path.join(TMP, "events_%s.jsonl" % datetime.now().strftime("%Y%m%d"))
codes = []
if os.path.exists(ev):
    for ln in open(ev, encoding="utf-8"):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("event") == "order":
            codes.append((e.get("code"), e.get("order_type")))
out["gw"]["order_events"] = codes

# 不变量①：宿主必须把**仓根 append 进 sys.path**（append 而非 insert）——
#   否则 `core.*` 不可导入 ⇒ 盘前预筛天天 fail-closed（2026-10-12 实测的真缺陷）。
out["root_on_path"] = os.path.abspath(ROOT) in [os.path.abspath(p) for p in sys.path]
# 不变量②：`core.market_data.facade`（盘前预筛的数据源）必须真的可导入
try:
    import core.market_data.facade as _fac            # noqa: F401
    out["core_facade_importable"] = True
except Exception as _e:
    out["core_facade_importable"] = "%s: %s" % (type(_e).__name__, _e)

# 不变量：`analysis.index_regime` 必须仍是 `_gm/` 副本（gm_main 的 fail-loud 守卫同款口径）。
# 2026-10-12 踩过：任何在 init 之前 import、且会 `sys.path.insert(0, ROOT)` 的模块都会把它顶掉。
_mod = sys.modules.get("analysis.index_regime") or sys.modules.get("analysis")
out["index_regime_from_gm"] = bool(_mod and "_gm" in (getattr(_mod, "__file__", "") or ""))

print("RESULT " + json.dumps(out, ensure_ascii=False))

# 收尾：关掉审计句柄再删临时目录（Windows 下句柄未放会删不掉；失败无所谓）
try:
    GM._audit_close()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass
