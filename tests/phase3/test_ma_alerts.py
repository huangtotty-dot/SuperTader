# -*- coding: utf-8 -*-
"""均线报警飞书开关单测 —— core/ma_alerts.py（2026-11-13 补，只测可离线确定的开关逻辑）。

三路报警（破5/10日线 / 站上5/10日线 / 30min趋势翻转）各有一个 `_*_feishu_enabled` 开关，
逻辑同构：feishu.enabled==False 或 notify_on_* ==False → 关；配置读取抛异常/非 dict →
**fail-open 放行**（宁可多推一条，别因为 config 崩了静默丢报警）。
调度函数 `_maybe_run_*` 会起线程+读行情+真发飞书，不在单测范围（见 [[hunter_no_feishu_trap]]）。

运行：python tests/phase3/test_ma_alerts.py
"""
import os
import sys
import unittest
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.ma_alerts as ma  # noqa: E402

# (函数, notify 键名)
_SWITCHES = [
    (ma._ma_break_feishu_enabled, "notify_on_ma_break"),
    (ma._ma_reclaim_feishu_enabled, "notify_on_ma_reclaim"),
    (ma._trend30_alert_feishu_enabled, "notify_on_trend30"),
]


def _patch_config(cfg):
    return mock.patch.object(ma, "load_runtime_config", return_value=cfg)


class TestFeishuEnabled(unittest.TestCase):
    def test_each_enabled_by_default(self):
        """feishu.enabled 与 notify_on_* 缺省均 True → 放行。"""
        for fn, _ in _SWITCHES:
            with _patch_config({"feishu": {}}):
                self.assertTrue(fn(), fn.__name__)

    def test_each_disabled_when_feishu_disabled(self):
        for fn, _ in _SWITCHES:
            with _patch_config({"feishu": {"enabled": False}}):
                self.assertFalse(fn(), fn.__name__)

    def test_each_disabled_by_own_notify_flag(self):
        for fn, key in _SWITCHES:
            with _patch_config({"feishu": {"enabled": True, key: False}}):
                self.assertFalse(fn(), fn.__name__)

    def test_exception_fails_open(self):
        """load_runtime_config 抛异常 → 放行（fail-open，不静默丢报警）。"""
        for fn, _ in _SWITCHES:
            with mock.patch.object(ma, "load_runtime_config", side_effect=RuntimeError("boom")):
                self.assertTrue(fn(), fn.__name__)

    def test_non_dict_config_fails_open(self):
        """runtime_config 非 dict（旧版/损坏配置）→ 走默认值放行。"""
        for fn, _ in _SWITCHES:
            with _patch_config(None):
                self.assertTrue(fn(), fn.__name__)

    def test_enabled_truthy_string_false_disables(self):
        """enabled: "false"（字符串）按 truthy 本会放行，但这里钉死 bool(...) 语义——
        只要 bool(enabled) 为 False 即关。"""
        for fn, _ in _SWITCHES:
            with _patch_config({"feishu": {"enabled": ""}}):  # bool("") == False
                self.assertFalse(fn(), fn.__name__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
