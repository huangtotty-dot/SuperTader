# -*- coding: utf-8 -*-
"""gm_main `on_bar` 的四个时点钩子 —— **行为级**回归（2026-10-12，旧引擎整链删除后补）。

## 为什么需要它

2026-10-12 旧引擎/OPEN_ALIGN/OGR 整链删除后，`gm_main.on_bar` 只剩**一条**策略链：
    09:31 盘前超跌候选预筛 → 10:00 早盘冲高卖 → 10:00 买侧 → 14:50 止损/到期
另有与其并列的 `_writeback_holdings`（14:57 收盘回写）。

删那一千多行时唯一的护栏是**结构断言**（`test_legacy_chain_retired` 读源码判符号），
而结构断言证明不了"跑起来还认时间窗/还每天只放行一次/回测模式不放行"——
本文件补这一层：真 import gm_main（桩掉 gm.api）→ 真跑 `init` + `on_bar`。

## 口径（断言）

  T1 每日一次：同一交易日 09:31/10:00 各打两次，钩子只被放行一次
  T2 定序：10:00 的**早盘冲高卖先于买侧**（先回笼现金再买，codex/WS5 的刻意排序）
  T3 时间窗：15:05（>15:00 提前 return）与 12:00（午休）不放行任何钩子
  T4 跨日重置：次日（10-13）四个闸重新放行
  T5 模式闸：MODE_BACKTEST 下不放行（钩子全是 MODE_LIVE 专属）
  T6 无 SDK 副作用：桩掉的 `order_volume` 一旦被调用即断言失败 ⇒ 本测试同时证明
     「钩子被调用≠会下单」的那部分由钩子自身负责（本驱动里钩子被换成记录器）

运行：python -m unittest tests.phase3.test_gm_main_osc_hooks -v
（内部以**子进程**跑 `gm_main_hook_driver.py`：gm_main 顶部 `from gm.api import *` 必须桩掉，
  桩不进同进程，避免污染其它测试。）
"""
import json
import os
import subprocess
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DRIVER = os.path.join(_ROOT, "tests", "phase3", "gm_main_hook_driver.py")
# 2026-10-12 策略归拢后：四个钩子在策略包 `oversold_gap_confirm.hooks` 内（宿主只调
# `osc.on_bar`）；`_writeback_holdings` 仍是宿主件。
_PREPARE, _BUY, _SELL, _MORNING, _WB = (
    "prepare", "try_buy", "try_sell", "try_morning_sell", "_writeback_holdings")


_CACHE = {}


def _run_driver() -> dict:
    """跑一次驱动（子进程隔离），结果进程内缓存 —— 两个 TestCase 共用，别跑两遍。"""
    if "r" in _CACHE:
        return _CACHE["r"]
    env = dict(os.environ)
    env["SMOKE_ROOT"] = _ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    # ⚠️ 刻意**不设 PYTHONPATH**：设了会让「`sys.path.insert(0, ROOT)` 型导入副作用」变成
    #    无操作，从而掩盖 `_gm/` 副本被顶掉的故障（2026-10-12 真踩过）。裸环境才是严苛用例。
    env.pop("PYTHONPATH", None)
    proc = subprocess.run([sys.executable, _DRIVER], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env=env, timeout=180)
    line = next((ln for ln in (proc.stdout or "").splitlines()
                 if ln.startswith("RESULT ")), None)
    if line is None:
        raise AssertionError(
            "驱动未产出 RESULT 行（import/init 就炸了？）\n"
            f"rc={proc.returncode}\nSTDOUT:\n{(proc.stdout or '')[-3000:]}\n"
            f"STDERR:\n{(proc.stderr or '')[-3000:]}")
    _CACHE["r"] = json.loads(line[len("RESULT "):])
    return _CACHE["r"]


class TestGmMainOscHooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = _run_driver()

    def _c(self, key):
        return self.r["cases"][key]

    def test_driver_saw_the_pool(self):
        self.assertGreater(self.r["stock_n"], 0, "STOCKS 池应可加载（config/auto_pool.py）")

    def test_t1_once_per_day(self):
        self.assertEqual(self._c("0931_first"), [_PREPARE])
        self.assertEqual(self._c("0931_second"), [], "同一交易日 09:31 只能放行一次")
        self.assertEqual(self._c("0932"), [])
        self.assertEqual(self._c("1000_first"), [_MORNING, _BUY])
        self.assertEqual(self._c("1000_second"), [], "同一交易日 10:00 只能放行一次")
        self.assertEqual(self._c("1450"), [_SELL])
        self.assertEqual(self._c("1457"), [_WB])

    def test_t2_morning_sell_precedes_buy(self):
        c = self._c("1000_first")
        self.assertEqual(c, [_MORNING, _BUY],
                         "10:00 早盘冲高卖必须先于买侧（先回笼现金再买）")

    def test_first_bar_at_1000_refreshes_regime_before_osc_hooks(self):
        c = self._c("late_1000")
        self.assertEqual(self.r.get("late_regime"), "uni_down")
        self.assertIn("regime_refresh", c)
        self.assertLess(c.index("regime_refresh"), c.index(_MORNING),
                        "首根 10:00 也必须先刷新 regime，再进卖→买")
        self.assertLess(c.index(_MORNING), c.index(_BUY))

    def test_uni_down_does_not_block_sell_hooks(self):
        self.assertIn(_MORNING, self._c("late_1000"))
        self.assertEqual(self._c("late_1450"), [_SELL],
                         "uni_down 只熔断买侧，14:50 风控卖侧必须照常执行")

    def test_t3_out_of_window(self):
        self.assertEqual(self._c("1505_oow"), [], ">15:00 应提前 return")
        self.assertEqual(self._c("1200_lunch"), [], "午休窗口不应放行")

    def test_t4_next_day_rearms(self):
        self.assertEqual(self._c("d2_0931"), [_PREPARE], "跨日必须重新放行")
        self.assertEqual(self._c("d2_1000"), [_MORNING, _BUY])

    def test_t5_backtest_mode_blocked(self):
        self.assertEqual(self._c("bt_1000"), [],
                         "四个钩子都是 MODE_LIVE 专属（回测不得下模拟盘单）")

    def test_t7_data_source_reachable(self):
        """盘前预筛的数据源必须可用：宿主把仓根 append 进 sys.path（**不是 insert**）。

        2026-10-12 实测的真缺陷：GM 终端里 sys.path 只有 `execution/auto` 与 `_gm/` ⇒
        `import core` 失败 ⇒ 预筛 fail-closed ⇒ **当日永不下单**（且只在日志里躺一行）。
        本条同时守 `_gm/` 优先级（见 T6）：append 而不是 insert 是这两条能同时成立的唯一写法。
        """
        self.assertTrue(self.r.get("root_on_path"),
                        "gm_main 必须把仓根加进 sys.path（否则 core.* 不可导入）")
        self.assertIs(self.r.get("core_facade_importable"), True,
                      f"core.market_data.facade 不可导入 ⇒ 预筛必失败: "
                      f"{self.r.get('core_facade_importable')}")

    def test_t6_index_regime_still_resolves_to_gm_copy(self):
        """`analysis.index_regime` 必须仍是 `_gm/` 副本（gm_main 自带 fail-loud 守卫同款口径）。

        2026-10-12 真踩：任何在 init 之前 import、且会 `sys.path.insert(0, ROOT)` 的模块
        （旧 `oversold_gap_executor` 就是；归拢进策略包时该副作用已消除）
        """
        self.assertTrue(self.r.get("index_regime_from_gm"),
                        "analysis.index_regime 被顶成了仓根副本 ⇒ 生产会 fail-loud 起不来")


class TestGatewayChainEndToEnd(unittest.TestCase):
    """网关整链（P0/P1，2026-10-12）：真 SERVICES 注入 + 一票一策略硬锁，端到端。"""

    @classmethod
    def setUpClass(cls):
        cls.gw = _run_driver()["gw"]

    def test_book_owned_code_is_buyable(self):
        self.assertEqual(self.gw["own_book_code"], "submitted",
                         "自己账本持有的票必须买得进（锁只拦别人的）")

    def test_unclaimed_account_position_is_locked(self):
        r = self.gw["unclaimed_held"]
        self.assertEqual(r["status"], "rejected")
        self.assertIn("ownership_lock", r["message"])
        self.assertIn("unclaimed_account_position", r["message"])

    def test_locked_order_never_reaches_broker_nor_bridge(self):
        self.assertEqual(self.gw["broker_calls"], ["SHSE.600123", "SHSE.600345", "SHSE.600123"],
                         "被锁的 600234 一次都不能送到柜台")
        codes = [c for c, _t in self.gw["order_events"]]
        self.assertNotIn("600234", codes,
                         "被锁的单绝不能落 order 事件（否则孤儿闸误以为本策略下过单）")

    def test_order_events_tagged_with_strategy_id(self):
        self.assertTrue(self.gw["order_events"], "应有 order 事件")
        for _c, t in self.gw["order_events"]:
            self.assertEqual(t, "osc", "order_type 必须是策略 id（多策略归属靠它）")

    def test_sell_is_not_locked(self):
        self.assertEqual(self.gw["sell_owned"], "submitted",
                         "卖侧不设锁（各策略账本本就是卖侧真源）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
