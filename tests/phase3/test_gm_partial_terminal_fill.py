# -*- coding: utf-8 -*-
"""部分成交终态（status 5/12 且 filled>0）回归钉 —— gm_main.on_order_status 终态分支。

## 背景（P1-5）

`execution/auto/gm_main.py:1434-1512` 已有部分成交处理：

  · `_terminal_filled` 提取（1435）；
  · `_terminal_done` 终态去重（1437-1447，同一柜台终态由回调与轮询重复送达只应用一次）；
  · 卖侧未成交余量回补 `manual_position`（1462-1469，仅 side==2）；
  · reject/terminal 审计落盘（1473-1482：4/8→write_reject，5/12→write_order_terminal）；
  · LIVE 模式 `_osc_fillbackfill.apply_reject` 带 filled_qty/filled_vwap/fee 补账（1494-1504）。

缺口：**买侧部分成交终态路径无回归测试**（既有 tests/phase3/test_osc_reject_backfill.py
只钉 BUY 零成交拒单；gm_main_sell_terminal_rollback_driver.py 只钉卖侧）——本文件补上。

## 依赖隔离

全 mock、零网络、零 gm SDK 依赖：`gm` / `gm.api` 用 sys.modules 桩
（桩面与 tests/phase3/gm_main_order_status_driver.py 同款）；宿主侧
write_reject / write_risk / write_order_terminal / _audit_write 与
`_osc_fillbackfill` 全部替换为 Mock 捕获——用例 1-4 不落任何文件。

⚠️ 用例只断言**当前实现的真实行为**（回归钉，不改行为）。唯一例外是
`TestBuyPartialTerminalBookPin`：钉的是**设计意图**（fillbackfill.apply_reject
docstring「BUY 时把该笔**未成交**虚挂份额从账本剔除」），当前实现与之不符
（疑似买侧 bug）⇒ 以 expectedFailure 登记，待 owner 裁决后修生产代码再摘标。

运行：python -m unittest tests.phase3.test_gm_partial_terminal_fill -v
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO_DIR = _ROOT / "execution" / "auto"

_DATE = "2026-11-23"           # 固定业务日（盘中终态场景）
_NOW = "2026-11-23 14:55:00"   # 固定事件时间（context.now 字符串化后透传）


def _install_gm_stub():
    """与 tests/phase3/gm_main_*_driver.py 同款的无交易假件桩面。"""
    gm_pkg = types.ModuleType("gm")
    gm_api = types.ModuleType("gm.api")
    for key, value in {
        "MODE_LIVE": 1, "MODE_BACKTEST": 2,
        "ADJUST_PREV": 1, "ADJUST_NONE": 0,
        "OrderSide_Buy": 1, "OrderSide_Sell": 2,
        "OrderType_Market": 1, "OrderType_Limit": 2,
        "PositionEffect_Open": 1, "PositionEffect_Close": 2,
        "PositionSide_Long": 1,
    }.items():
        setattr(gm_api, key, value)

    def _no_sdk(*args, **kwargs):
        raise AssertionError("本测试不得触发任何真实 SDK 调用")

    for name in ("order_volume", "get_instruments", "run"):
        setattr(gm_api, name, _no_sdk)
    gm_api.subscribe = lambda **kwargs: None
    gm_api.history_n = lambda **kwargs: []
    gm_api.history = lambda **kwargs: []
    gm_api.current = lambda *args, **kwargs: []
    gm_api.get_orders = lambda *args, **kwargs: []
    gm_pkg.api = gm_api
    return {"gm": gm_pkg, "gm.api": gm_api}


_GM = None                 # 加载后的 gm_main 模块句柄
_SAVED_MODULES = {}        # sys.modules 打桩前的现场（tearDownModule 恢复）
_SAVED_ENV = {}
_TMP_BRIDGE = None

# 撞名顶层模块清单：gm_main 从 `_gm/`（config/utils/gm_bridge/analysis/data/
# runtime/signals）与 `execution/auto/`（framework/oversold_gap_confirm）解析这些名字，
# 而仓根恰好也有 `config.py`（单文件模块）与 `analysis/` 包——**同进程合并验证时**，
# 先跑的测试模块（如 test_eod_review → core.eod_review → import config）会把仓根
# 副本先装进 sys.modules，gm_main exec 时 `from config.params import PARAMS` 就会炸
# "'config' is not a package"（2026-11-23 合并验证 134 项实测 1 error 即此因）。
# setUpModule 先摘除存档、tearDownModule 恢复现场；`core` 不在清单内——
# gm_main 刻意 append 仓根就是要用仓根 core.*。
_COLLIDE_TOPS = ("config", "utils", "gm_bridge", "analysis",
                 "data", "runtime", "signals",
                 "framework", "oversold_gap_confirm")


def setUpModule():
    global _GM, _TMP_BRIDGE
    # 环境钉死：SUPERTRADER_ROOT 指仓根；GM_BRIDGE_DIR 指临时目录（双保险——
    # 宿主写入件全部 Mock，理论上不会有任何文件落到这两处）。
    _TMP_BRIDGE = tempfile.TemporaryDirectory(prefix="gm_partial_terminal_")
    for k, v in (("SUPERTRADER_ROOT", str(_ROOT)),
                 ("GM_BRIDGE_DIR", _TMP_BRIDGE.name)):
        _SAVED_ENV[k] = os.environ.get(k)
        os.environ[k] = v
    for name, mod in _install_gm_stub().items():
        _SAVED_MODULES[name] = sys.modules.get(name)
        sys.modules[name] = mod
    # 撞名隔离：摘除 sys.modules 里已被其它测试模块装入的仓根副本（含子模块），
    # 让 gm_main exec 期间的同名 import 一律从 _gm/ 与 execution/auto/ 重新解析。
    for name in list(sys.modules):
        if name.split(".", 1)[0] in _COLLIDE_TOPS:
            _SAVED_MODULES[name] = sys.modules[name]
            del sys.modules[name]
    # 路径序隔离（2026-11-23 全套件 red 实锤补钉）：purge 只挡得住 sys.modules 污染，
    # 挡不住**路径序**污染——全套件语境下 _GM_DIR 可能已被先跑的驱动塞进 sys.path
    # 但不在首位（gm_main.py:43-45 的 `if _GM_DIR not in sys.path` 就不会再把它顶到
    # 0 号位），而仓根（含 config.py 单文件模块）排在前面 ⇒ `from config.params
    # import PARAMS` 命中仓根 config.py 照炸 "'config' is not a package"。
    # ⇒ 显式按路径预载 _gm/config 包并钉进 sys.modules['config']，exec 时缓存直中，
    # 与 sys.path 顺序彻底解耦（params.py 纯数据零导入，preload 无副作用）。
    _cfg_spec = importlib.util.spec_from_file_location(
        "config", str(_AUTO_DIR / "_gm" / "config" / "__init__.py"),
        submodule_search_locations=[str(_AUTO_DIR / "_gm" / "config")])
    _cfg = importlib.util.module_from_spec(_cfg_spec)
    sys.modules["config"] = _cfg
    _cfg_spec.loader.exec_module(_cfg)
    if str(_AUTO_DIR) not in sys.path:
        sys.path.insert(0, str(_AUTO_DIR))
    if str(_ROOT) not in sys.path:
        sys.path.append(str(_ROOT))
    # 真 import gm_main（独立模块名，避免与仓内其它 "gm_main" 加载互踩）。
    spec = importlib.util.spec_from_file_location(
        "gm_main_partial_terminal_fill", str(_AUTO_DIR / "gm_main.py"))
    _GM = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = _GM
    spec.loader.exec_module(_GM)
    try:
        import atexit
        atexit.unregister(_GM._gm_atexit_banner)
    except Exception:
        pass


def tearDownModule():
    global _GM
    if _GM is not None:
        try:
            _GM._audit_close()
        except Exception:
            pass
    # 先清掉 exec 期间新装入的撞名模块（_gm/ 副本，未入 _SAVED_MODULES 者），
    # 再经下方恢复循环放回仓根原版——顺序不能反：否则 _gm 的 config 包会残留在
    # sys.modules，排在本模块之后跑的 test_eod_review 等 `import config` 会拿错包。
    for name in list(sys.modules):
        if (name.split(".", 1)[0] in _COLLIDE_TOPS
                and name not in _SAVED_MODULES):
            sys.modules.pop(name, None)
    for name, saved in _SAVED_MODULES.items():
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved
    sys.modules.pop("gm_main_partial_terminal_fill", None)
    for k, saved in _SAVED_ENV.items():
        if saved is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = saved
    try:
        sys.path.remove(str(_AUTO_DIR))
    except ValueError:
        pass
    if _TMP_BRIDGE is not None:
        _TMP_BRIDGE.cleanup()
    _GM = None


def _fresh_context(**over):
    """LIVE 模式最小 context（字段对齐 gm_main_*_driver.py 既有惯例）。"""
    ctx = types.SimpleNamespace(
        mode=_GM.MODE_LIVE, now=_NOW,
        executed_orders={}, manual_position={},
        _fills_done=set(), rejected_order_count=0, _pending_recon={},
    )
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


def _buy_order(status=5, oid="OID-B1", volume=1000, filled=600):
    """买侧部分成交终态事件：委托 1000、累计成交 600、随后撤单/过期。"""
    return {"symbol": "SHSE.600000", "status": status, "volume": volume,
            "side": 1, "id": oid, "filled_volume": filled, "filled_vwap": 10.5,
            "filled_commission": 1.6, "price": 10.0, "ord_rej_reason_detail": ""}


class TestPartialTerminalHostBranch(unittest.TestCase):
    """宿主终态分支（gm_main.py:1434-1512）行为钉：审计落盘口径、卖侧余量回补、
    apply_reject 事件透传、_terminal_done 去重。"""

    def setUp(self):
        for name in ("write_reject", "write_risk", "write_order_terminal",
                     "_audit_write"):
            p = mock.patch.object(_GM, name, mock.MagicMock(name=name))
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        # 只换宿主手里的命名空间引用，绝不改真实 fillbackfill 模块本体。
        fb_ns = mock.MagicMock(name="_osc_fillbackfill")
        self.apply_reject = fb_ns.apply_reject
        p = mock.patch.object(_GM, "_osc_fillbackfill", fb_ns)
        p.start()
        self.addCleanup(p.stop)

    # ── 用例 1：买侧部分成交后撤单（status=5, filled>0）──
    def test_buy_partial_cancel_status5(self):
        ctx = _fresh_context()
        _GM.on_order_status(ctx, _buy_order(status=5, oid="OID-B1"))
        # ① 终态审计：cancelled 落 write_order_terminal，绝不计 reject
        self.write_order_terminal.assert_called_once()
        _, kw = self.write_order_terminal.call_args
        self.assertEqual(kw.get("terminal"), "cancelled")
        self.assertEqual(kw.get("raw_status"), 5)
        self.assertEqual(kw.get("order_id"), "OID-B1")
        self.write_reject.assert_not_called()
        self.assertEqual(ctx.rejected_order_count, 0)
        # ② apply_reject 收到带累计成交/VWAP/费用的完整终态事件（补账原料透传）
        self.apply_reject.assert_called_once()
        args, _ = self.apply_reject.call_args
        self.assertEqual(args[0], _GM._OSC_BOOK_DIR)
        ev = args[1]
        self.assertEqual(
            {k: ev[k] for k in ("time", "code", "side", "qty", "order_id",
                                "filled_qty", "filled_vwap", "fee", "status",
                                "rej_detail")},
            {"time": _NOW, "code": "600000", "side": "BUY", "qty": 1000,
             "order_id": "OID-B1", "filled_qty": 600, "filled_vwap": 10.5,
             "fee": 1.6, "status": 5, "rej_detail": ""})
        # ③ 买侧不动 manual_position（买侧虚挂回滚只走 OSC 账本链）
        self.assertEqual(ctx.manual_position, {})
        # ④ 终态指纹（symbol, oid, status, volume, filled）已入去重集
        self.assertIn(("SHSE.600000", "OID-B1", 5, 1000, 600), ctx._terminal_done)

    # ── 用例 2：买侧部分成交终止（status=12 过期, filled>0）──
    def test_buy_partial_expired_status12(self):
        ctx = _fresh_context()
        order = _buy_order(status=12, oid="OID-B2", volume=500, filled=200)
        _GM.on_order_status(ctx, order)
        # ① 终态审计：expired
        _, kw = self.write_order_terminal.call_args
        self.assertEqual(kw.get("terminal"), "expired")
        self.assertEqual(kw.get("raw_status"), 12)
        self.write_reject.assert_not_called()
        self.assertEqual(ctx.rejected_order_count, 0)
        # ② apply_reject 透传 qty=500 / filled_qty=200 / status=12
        self.assertEqual(self.apply_reject.call_count, 1)
        ev = self.apply_reject.call_args[0][1]
        self.assertEqual((ev["side"], ev["qty"], ev["filled_qty"], ev["status"]),
                         ("BUY", 500, 200, 12))
        self.assertEqual(ctx.manual_position, {})
        self.assertIn(("SHSE.600000", "OID-B2", 12, 500, 200), ctx._terminal_done)

    # ── 用例 3：卖侧部分成交终态（既有逻辑防回归）──
    def test_sell_partial_cancel_rolls_back_unfilled(self):
        ctx = _fresh_context()
        ctx.manual_position["SHSE.600001"] = {"qty": 0, "available": 0, "t_qty": 0}
        order = {"symbol": "SHSE.600001", "status": 5, "volume": 1000, "side": 2,
                 "id": "OID-S1", "filled_volume": 400, "filled_vwap": 10.5,
                 "filled_commission": 7.9, "price": 10.0,
                 "ord_rej_reason_detail": ""}
        _GM.on_order_status(ctx, order)
        # ① 未成交余量 600（=volume−filled）回补三字段——只认 side==2
        self.assertEqual(ctx.manual_position["SHSE.600001"],
                         {"qty": 600, "available": 600, "t_qty": 600})
        # ② sell_rollback 审计事件（qty=未成交余量，不是委托量）
        rollbacks = [c.args[0] for c in self._audit_write.call_args_list
                     if c.args and c.args[0].get("event") == "sell_rollback"]
        self.assertEqual(len(rollbacks), 1)
        self.assertEqual((rollbacks[0]["code"], rollbacks[0]["qty"]),
                         ("600001", 600))
        # ③ 终态事件带累计成交进 apply_reject（账本侧只对真实成交补账）
        self.assertEqual(self.apply_reject.call_count, 1)
        ev = self.apply_reject.call_args[0][1]
        self.assertEqual((ev["side"], ev["qty"], ev["filled_qty"],
                          ev["filled_vwap"], ev["fee"]),
                         ("SELL", 1000, 400, 10.5, 7.9))

    # ── 用例 4：重复终态事件去重（_terminal_done）──
    def test_duplicate_terminal_event_deduped(self):
        ctx = _fresh_context()
        order = _buy_order(status=5, oid="OID-DUP")
        _GM.on_order_status(ctx, order)
        _GM.on_order_status(ctx, dict(order))     # 轮询补记同事件重放
        # 审计/回填整条只应用一次
        self.assertEqual(self.apply_reject.call_count, 1)
        self.assertEqual(self.write_order_terminal.call_count, 1)
        self.assertEqual(len(ctx._terminal_done), 1)


def _write_json(fp, payload):
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _read_json(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


class TestBuyPartialTerminalBookPin(unittest.TestCase):
    """买侧部分成交终态的**账本层**行为钉：真实 fillbackfill + 临时账本目录
    （fillbackfill/executor 仓规不 import gm.api，纯文件链，可离线真跑）。"""

    def setUp(self):
        from oversold_gap_confirm import executor, fillbackfill   # noqa: E402
        self.executor = executor
        self.fb = fillbackfill
        self._tmp = tempfile.TemporaryDirectory(prefix="osc_buy_partial_pin_")
        self.dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        # 「挂单即入账」现场：委托 1000@10.0 已全额虚挂进账本；留痕 submitted、
        # filled_qty=0（status=2 部成在途事件宿主不处理 ⇒ 终态前留痕拿不到量）。
        _write_json(os.path.join(self.dir, "positions.json"), {
            "updated_at": "", "closed": {},
            "open": {"600000": {"entry_date": _DATE, "entry_price": 10.0,
                                "qty": 1000, "dev20": None, "gap": 0.05,
                                "board": "半导体", "days_held": 0,
                                "last_check_date": None}}})
        _write_json(os.path.join(self.dir, "fills_%s.json" % _DATE), {
            "date": _DATE, "mode": "live", "picks": [],
            "buys": [{"code": "600000", "symbol": "SHSE.600000", "side": "BUY",
                      "gap": 0.05, "price": 10.0, "qty_target": 1000,
                      "qty_placed": 1000, "truncated": False, "defer_reason": None,
                      "status": "submitted", "filled_qty": 0, "filled_price": None,
                      "fee_est": 5.0, "booked_qty": 1000, "order_id": "OID-B1",
                      "qty_remaining": 1000}],
            "summary": {}})

    def test_buy_partial_terminal_keeps_real_filled_shares(self):
        """设计意图（fillbackfill.apply_reject docstring 原话）：「BUY 时把该笔
        **未成交**虚挂份额从账本剔除」⇒ 委托 1000 / 实成 600 / 随后撤单终态后：

          · 账本应保留真实成交 600 股（只回滚未成交的 400 虚挂）；
          · fills 留痕 filled_qty 应记 600（终态事件携带的累计成交量/VWAP 落痕）。

        2026-11-23 已修复（P1-5）：`_reject_buy_fills_file` 新增终态写回——事件携带的
        累计 filled_qty/filled_vwap/fee 先落留痕（且在 `_settle_buy_budget_record`
        预算结算之前），回滚量=booked−filled=400，账本正确保留真实成交 600 股。
        原 expectedFailure 钉，修复后转正为正式断言。
        """
        res = self.fb.apply_reject(self.dir, {
            "time": _NOW, "code": "600000", "side": "BUY", "qty": 1000,
            "order_id": "OID-B1", "status": 5,
            "filled_qty": 600, "filled_vwap": 10.5, "fee": 1.6},
            log=lambda *_: None)
        self.assertIsNotNone(res)
        book_after = self.executor.load_book(self.dir)
        rec_after = _read_json(os.path.join(self.dir, "fills_%s.json" % _DATE))["buys"][0]
        # 终态语义本身是对的（这部分当前即成立，先于 xfail 断言）
        self.assertEqual(rec_after["status"], "cancelled")
        # 设计意图断言（当前实现不满足 ⇒ expectedFailure）
        self.assertEqual(int(rec_after.get("filled_qty") or 0), 600)
        entry = (book_after.get("open") or {}).get("600000")
        self.assertIsNotNone(entry, "真实成交 600 股不应随虚挂整条剔除")
        self.assertEqual(int(entry["qty"]), 600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
