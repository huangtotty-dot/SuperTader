# -*- coding: utf-8 -*-
"""旧镜像引擎「总闸」+ 2026-10-12 owner 决策 C 的配置自洽性。

## 背景：为什么需要一个新开关

owner 2026-10-12 决策「**启弱转强、关 S1**」，并明确要求**旧引擎也要停**。
但原实现里"关 S1"**必然**复活旧引擎 —— 两个闸的语义是：

```python
_s1_open_align_allowed() = (not _s1_mode_on()) or s1_open_align_enabled
_s1_legacy_active()      = (not _s1_mode_on()) or s1_legacy_engine_enabled
```

`s1_mode_enabled=False` ⇒ 两项都 True ⇒ **镜像对齐 + TAIL + 旧镜像做T引擎全部复活**，
而旧引擎的行为是**镜像手动 A/B 实盘账户下单**（正是 S1 接管要停掉的东西）。
且那两个开关是"S1 **on** 时强制恢复（调试用）"，**没有**"off 时强制挂起"的入口。

⇒ 新增总闸 `legacy_engine_enabled`（`gm_main._legacy_engine_allowed`）：
False = **无论 S1 开关如何**，旧引擎一律挂起；缺键默认 True（保持历史语义）。

运行：python -m unittest tests.phase3.test_legacy_engine_master_switch -v
"""
import ast
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
_GMM = _ROOT / "execution" / "auto" / "gm_main.py"
_PARAMS = _ROOT / "execution" / "auto" / "_gm" / "config" / "params.py"


def _load_gates():
    """从 gm_main 源码取出四个**纯闸函数**（gm_main 顶部 `from gm.api import *`，
    无 SDK 环境不能直接 import ⇒ 与仓里 TestT6GmMainStatics 同做法）。"""
    tree = ast.parse(_GMM.read_text(encoding="utf-8"))
    keep = {"_legacy_engine_allowed", "_s1_mode_on", "_s1_open_align_allowed", "_s1_legacy_active"}
    mod = ast.Module(body=[n for n in tree.body
                           if isinstance(n, ast.FunctionDef) and n.name in keep],
                     type_ignores=[])
    ns = {"PARAMS": {}}
    exec(compile(mod, str(_GMM), "exec"), ns)      # noqa: S102 测试内自取源码
    return ns


class TestLegacyEngineMasterSwitch(unittest.TestCase):
    def setUp(self):
        self.ns = _load_gates()
        self.g = self.ns
        self.assertIn("_legacy_engine_allowed", self.ns, "总闸函数应存在")

    def _set(self, **kw):
        self.ns["PARAMS"] = kw

    def test_s1_off_no_longer_revives_legacy(self):
        """**本开关存在的全部理由**：关 S1 而旧引擎保持挂起。"""
        self._set(s1_mode_enabled=False, legacy_engine_enabled=False)
        self.assertFalse(self.g["_s1_open_align_allowed"](), "关 S1 后镜像对齐必须仍挂起")
        self.assertFalse(self.g["_s1_legacy_active"](), "关 S1 后旧做T引擎必须仍挂起")

    def test_missing_key_keeps_historic_semantics(self):
        """缺键 ⇒ 默认 True ⇒ "关 S1 = 回退旧引擎"的历史语义不变（不影响既有配置/测试）。"""
        self._set(s1_mode_enabled=False)
        self.assertTrue(self.g["_legacy_engine_allowed"]())
        self.assertTrue(self.g["_s1_open_align_allowed"]())
        self.assertTrue(self.g["_s1_legacy_active"]())

    def test_s1_on_still_suspends(self):
        """S1 on 时旧引擎照旧挂起（总闸默认 True 不改变这一点）。"""
        self._set(s1_mode_enabled=True, legacy_engine_enabled=True)
        self.assertFalse(self.g["_s1_open_align_allowed"]())
        self.assertFalse(self.g["_s1_legacy_active"]())

    def test_master_switch_beats_debug_flags(self):
        """总闸优先：即便 S1 on 且调试开关要求恢复，总闸 False 也压得住。"""
        self._set(s1_mode_enabled=True, s1_open_align_enabled=True,
                  s1_legacy_engine_enabled=True, legacy_engine_enabled=False)
        self.assertFalse(self.g["_s1_open_align_allowed"]())
        self.assertFalse(self.g["_s1_legacy_active"]())


class TestShippedConfigCoherent(unittest.TestCase):
    """生产配置自洽性：**别把旧引擎留成"上膛"状态**。

    owner 的决策是「启弱转强 + 关 S1 + 停旧引擎」三者一体。
    若 S1 关着而总闸又是 True，旧引擎就会按**手动 A/B 实盘账户**下单 ——
    这正是 owner 明确说"要停"的东西。此断言逼一次有意识的决定。
    """

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

    def test_legacy_engine_not_armed_while_s1_off(self):
        s1 = self.p.get("s1_mode_enabled")
        legacy = self.p.get("legacy_engine_enabled", True)
        if s1 is False:
            self.assertFalse(
                legacy,
                "⚠️ S1 已关而 legacy_engine_enabled 为 True ⇒ 旧镜像做T引擎会复活并按"
                "**手动 A/B 实盘账户**下单（owner 2026-10-12 明确要求停掉）。"
                "要么把总闸置 False，要么明确改这条测试并说明为什么旧引擎该回来。")

    def test_weak_strong_on_and_sell_side_on(self):
        """弱转强翻启时，卖侧总闸应同时为 True（否则只买不卖，持仓无法闭环）。"""
        if self.p.get("weak_strong_paper_enabled"):
            self.assertTrue(self.p.get("weak_strong_sell_enabled"),
                            "买侧开了卖侧必须开 —— 否则弱转强的持仓没有任何卖出路径")
