# -*- coding: utf-8 -*-
"""旧引擎 / OPEN_ALIGN / OGR **整链已删** —— 守卫它保持删除（2026-10-12）。

## 经过（删错一次就会掐断别的东西，故记下来）

owner 2026-10-12：贴来 S1 首发日复盘 → 拍板「**直接启动超跌跳空确认，关掉 S1**」→ 随后
「**S1 和所谓的旧引擎删干净**」。分两段做：第一段删 S1 执行集成（自包含）；
第二段（本文件守的这一段）删「旧镜像做T引擎 + 开盘强制对齐 + 开盘低开反转」。

第二段为什么不能一把梭（第一段施工时测绘出的三处硬纠缠，逐条怎么解的）：
  1. `t_engine_auto` **不是纯旧引擎件**：`on_order_status` 对任何成交都调
     `record_trade_action` ⇒ 直接删会让成交流水与日切报错。**解法**：删引擎的同时
     删掉那三处调用（`context.engine` 一并消失），成交流水改由 `executed_orders` 独立承载。
  2. `sell_state` 被共享回调写 ⇒ 删 `sell_channels` 会留孤儿写手。**解法**：连
     `on_order_status` 里的 `_sell_state_persist` / `_pending_sell_action` 一起删。
  3. 超跌跳空确认此前**没有专属单测** ⇒ 在共享回调上动刀没有判据。**解法**：先补
     `test_osc_regression.py`（28 项）再动——本文件只守"删干净"，不重复守它。

另：`_maybe_unwind_probe_leftover` 不属旧引擎（被 equity_daily 消费）——**勿**因本文件误删。

## 本文件守什么

守"退场"本身 —— 防止日后有人照旧文档把旧引擎/OGR 接线接回来而无人察觉。
运行：python -m unittest tests.phase3.test_legacy_chain_retired -v
"""
import ast
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_AUTO = _ROOT / "execution" / "auto"
_GMM = _AUTO / "gm_main.py"
_PARAMS = _AUTO / "_gm" / "config" / "params.py"

# 随旧引擎 / OPEN_ALIGN / OGR 一起删除的文件（存在即说明有人把它们接回来了）
_DELETED_FILES = [
    "sell_channels.py", "sell_state.py", "t_engine_auto.py",
    "backtest_holdings.py", "replay_verify.py", "ogr_shadow_glue.py",
    "oversold_gap_executor.py",       # 2026-10-12 搬进 oversold_gap_confirm/ 包
]

# 不应再出现在 gm_main 里的符号（任何一条命中都说明旧链没删干净）
_DEAD_IN_GMM = [
    "_s1_mode_on", "_legacy_engine_allowed", "_s1_open_align_allowed", "_s1_legacy_active",
    "_force_open_align", "OPEN_ALIGN_BUY_ORDER", "MIRROR_HOLDINGS", "_load_mirror_holdings",
    "_total_equity", "_stock_budget_cap", "_check_max_pos_cap", "max_concurrent_positions",
    "_base_ordered", "_base_settled", "_base_ref_", "_base_topup_qty",
    "_buy_confirm_gate", "_buy_confirm_pending", "_scan_pending_confirm",
    "_scan_pending_unanswered", "write_buy_pending", "read_buy_pending",
    "read_buy_decision", "write_confirm", "read_auto_build", "consume_auto_build",
    "SignalEngine", "PositionSizer", "sell_channels", "sell_state", "bar_cache",
    "_pop_buy_snapshot", "_pending_buy_snapshot", "daily_buy_count", "daily_sell_count",
    "MARKET_PROXY", "_ogr_", "ogr_shadow_glue", "_dedup_bar", "_clear_signal_mute_keys",
    "_inflight_sell", "_protect_sell_reject_until", "MAX_BASE_RETRY",
]

# params 里随代码一起删掉的开关（留着 = 又能"开关制复活"一条死链）
_DEAD_PARAMS = [
    "s1_mode_enabled", "s1_open_align_enabled", "s1_tail_enabled",
    "s1_legacy_engine_enabled", "legacy_engine_enabled",
    "open_gap_reversal_shadow_enabled", "open_gap_reversal_live_enabled",
    "human_confirm_buy_enabled", "human_confirm_base_topup",
]


def _gmm_src() -> str:
    return _GMM.read_text(encoding="utf-8")


def _code_tokens(path: Path) -> set:
    """源码里**代码层面**出现过的标识符/字符串常量（**排除注释与文档字符串**）。

    为什么不用朴素 `assertNotIn` 文本搜索：`gm_main` 的模块 docstring 与注释**故意**
    写明"旧引擎（sell_channels/bar_cache/...）已删除"——文本搜索会把说明当违规。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0]))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            out.add(node.value)
    return out


class TestDeletedFilesStayDeleted(unittest.TestCase):
    def test_engine_files_are_gone(self):
        for name in _DELETED_FILES:
            self.assertFalse((_AUTO / name).exists(),
                             f"{name} 应随旧引擎/OGR 一并删除（它服务的东西已不存在）")


class TestGmMainHasNoLegacyWiring(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _gmm_src()
        cls.tokens = _code_tokens(_GMM)

    def test_no_dead_symbols(self):
        hit = [s for s in _DEAD_IN_GMM if s in self.tokens]
        self.assertFalse(hit, f"gm_main 里仍有旧链符号 {hit} —— 旧引擎/OGR 没删干净")

    def test_one_shot_date_reset_only_for_oversold_gap_confirm(self):
        """按日重置：宿主只负责"发现换日并通知"，四个"每日一次"标记由**策略包**自持。

        2026-10-12 归拢前这四条是 gm_main 的模块级全局；现在归 `hooks._STATE`。
        """
        self.assertIn("osc.reset_day(today)", self.src,
                      "宿主换日时必须调用策略包的 reset_day")
        hk = (_AUTO / "oversold_gap_confirm" / "hooks.py").read_text(encoding="utf-8")
        for _k in ('"prepare": False', '"buy": False', '"sell": False', '"morning": False'):
            self.assertIn(_k, hk, f"策略包每日一次状态应含 {_k}")
        self.assertNotIn("_OSC_", self.src.replace("_OSC_BOOK_DIR", ""),
                         "宿主不该再持有策略的每日一次标记（账本目录常量除外）")
        for _flag in ("_OSC_PREPARE_DONE_DATE", "_OSC_BUY_DONE_DATE",
                      "_OSC_MORNING_SELL_DONE_DATE", "_OSC_SELL_DONE_DATE"):
            self.assertNotIn(_flag, self.src, f"{_flag} 应已归策略包")


class TestSurvivingGuardsStillHold(unittest.TestCase):
    """删旧链**不能顺手删掉**的几条（它们不属于旧引擎）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _gmm_src()

    def test_gateway_writes_order_before_ordering(self):
        """下单前必须 `write_order`（**孤儿闸**登记；6a96829c 前科）。

        2026-10-12 两次搬迁后，实现分三处，逐处守：
          · 通用实现 `framework/gateway.py`：write_order **早于** order_volume，且晚于归属硬锁
          · 策略绑定 `oversold_gap_confirm/gateway.py`：`STRATEGY_ID = "osc"`
        """
        pkg_gw = (_AUTO / "oversold_gap_confirm" / "gateway.py").read_text(encoding="utf-8")
        self.assertIn("class OscGateway(GmGateway):", pkg_gw)
        self.assertIn('STRATEGY_ID = "osc"', pkg_gw)
        gw = (_AUTO / "framework" / "gateway.py").read_text(encoding="utf-8")
        i_lock = gw.find("ownership_lock")
        i_wo = gw.find("self.sv.write_order(str(now), code, side")
        i_ov = gw.find("gm.order_volume, symbol=sym")
        self.assertGreater(i_wo, 0, "通用网关里下单前必须 write_order（孤儿闸登记）")
        self.assertGreater(i_ov, i_wo, "write_order 必须在 order_volume **之前**")
        self.assertGreater(i_wo, i_lock, "归属硬锁必须在落 order 事件**之前**（被锁的单不落事件）")

    def test_strategy_lives_in_its_own_package(self):
        """2026-10-12 归拢：策略实现必须在**策略包**里，宿主 gm_main 只剩宿主职责。

        owner 原话：「为了使得后面的模块化更加清晰，把超跌跳空确认相关的实现放到一个文件夹中」。
        """
        pkg = _AUTO / "oversold_gap_confirm"
        for f in ("__init__.py", "decision.py", "prescreen.py", "executor.py",
                  "hooks.py", "gateway.py", "runtime.py"):
            self.assertTrue((pkg / f).exists(), f"策略包缺文件 {f}")
        # 宿主里不该再出现策略内部符号（否则"归拢"名不副实）
        for dead in ("_osc_prepare", "_osc_try_buy", "_osc_try_sell", "_osc_try_morning_sell",
                     "_OscGmGateway", "_load_osc_poll", "_osc_sell_codes", "_osc_mode_on",
                     "oversold_gap_executor", "core/oversold_gap_confirm"):
            self.assertNotIn(dead, self.src, f"gm_main 仍含策略内部件 `{dead}` ⇒ 未归拢干净")
        # 宿主对策略的唯一入口就是这两个调用
        self.assertIn("osc.on_bar(OSC_RT, context, now, today, t)", self.src)
        self.assertIn("osc.reset_day(today)", self.src)

    def test_orphan_gate_intact(self):
        self.assertIn("_strategy_ordered_today", self.src, "孤儿闸（非本策略成交不入台账）应保留")
        self.assertIn("orphan_fill", self.src)

    def test_heartbeat_and_writeback_and_regime_intact(self):
        """数据链三件（心跳 / 收盘回写 / 指数态势）与旧引擎无关，必须留着。"""
        self.assertIn("write_heartbeat(", self.src)
        self.assertIn("_writeback_holdings(context)", self.src)
        self.assertIn("detect_index_regime(", self.src)

    def test_fill_ledger_and_recon_intact(self):
        """成交入账与"回调失效轮询兜底"是共享件（超跌跳空确认的成交也走 on_order_status）。"""
        self.assertIn("write_fill(", self.src)
        self.assertIn("_poll_pending_recon(context, now)", self.src)
        self.assertIn("_pending_recon_close(context, symbol)", self.src)

    def test_kill_switch_still_guards_the_only_open_path(self):
        """KILL_SWITCH 原先只有旧引擎读；旧引擎删除后由**超跌跳空确认买侧**（唯一开仓路径）承接。

        2026-10-12 归拢后：读它的是策略包 `hooks.try_buy`（经 `rt.kill_switch()`），
        而 `rt.kill_switch` 的实现在宿主注入（`SERVICES.kill_switch=check_kill_switch`）——
        两处都要在，缺一处这道人工闸就断了。
        """
        hk = (_AUTO / "oversold_gap_confirm" / "hooks.py").read_text(encoding="utf-8")
        i = hk.find("def try_buy")
        self.assertGreater(i, 0, "策略包应有买侧钩子")
        seg = hk[i:i + 1200]
        self.assertIn("rt.kill_switch()", seg,
                      "唯一开仓路径必须仍受 KILL_SWITCH 约束，否则系统失去人工红按钮")
        self.assertIn("kill_switch=check_kill_switch", self.src,
                      "宿主必须把真实的 check_kill_switch 注入 SERVICES")


class TestShippedConfigCoherent(unittest.TestCase):
    """生产配置自洽性（原 test_legacy_engine_master_switch 里**仍然有效**的那条）。"""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(_PARAMS.read_text(encoding="utf-8"))
        cls.p = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "PARAMS":
                        cls.p = ast.literal_eval(node.value)

    def test_params_table_found(self):
        self.assertTrue(self.p, "应能从 params.py 取出 PARAMS 字面量")

    def test_dead_switches_removed(self):
        for k in _DEAD_PARAMS:
            self.assertNotIn(k, self.p, f"{k} 的读取方已删 ⇒ 开关必须一并移除（防'开关制复活'）")

    def test_oversold_gap_confirm_on_and_sell_side_on(self):
        """超跌跳空确认翻启时，卖侧总闸应同时为 True（否则只买不卖，持仓无法闭环）。"""
        if self.p.get("osc_enabled"):
            self.assertTrue(self.p.get("osc_sell_enabled"),
                            "买侧开了卖侧必须开 —— 否则超跌跳空确认的持仓没有任何卖出路径")

    def test_gateway_param_kept(self):
        """`osc_limit_slippage_bp` 必须留着：`_OscGmGateway.place_order`（超跌跳空确认在用）读它。"""
        self.assertIn("osc_limit_slippage_bp", self.p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
