# -*- coding: utf-8 -*-
"""日线背离飞书文案单测 —— stock_hunter/modules/daily_divergence.py（2026-11-13 补）。

只测 `format_alert_lines`（纯函数，无网络）。它把命中结果格式化成飞书 `send_post` 的
content_lines。三件要钉死的事：
① 空命中 → 空列表（不产生任何输出，更不会挂上「仅供观察」声明）；
② 命中行含代码+名称+日期+距今根数，`consec` 存在时追加「连续」；
③ 命中非空时**必须**在末尾附「尚未经本项目验证」的免责声明（owner 强调：单次日线
   MACD 背离本身尚未验证，不得当作买卖信号）。

运行：python tests/phase3/test_daily_divergence.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from stock_hunter.modules.daily_divergence import format_alert_lines  # noqa: E402


class TestFormatAlertLines(unittest.TestCase):
    def test_empty_hits(self):
        self.assertEqual(format_alert_lines([]), [])

    def test_hit_with_name_and_consec(self):
        hits = [{"code": "600000", "time": "2026-09-19 10:00:00",
                 "bars_ago": 3, "consec": 2}]
        lines = format_alert_lines(hits, name_of=lambda c: "贵州茅台")
        self.assertEqual(
            lines[0],
            "🟡 贵州茅台（600000）日线底背离（2026-09-19，距今3根，连续）",
        )

    def test_hit_without_name_falls_back_code(self):
        hits = [{"code": "600000", "time": "2026-09-19", "bars_ago": 1}]
        lines = format_alert_lines(hits)
        self.assertTrue(lines[0].startswith("🟡 600000（600000）"))

    def test_no_consec_omits_连续(self):
        hits = [{"code": "600000", "time": "2026-09-19", "bars_ago": 1}]
        lines = format_alert_lines(hits, name_of=lambda c: "X")
        self.assertNotIn("连续", lines[0])
        self.assertIn("距今1根", lines[0])

    def test_disclaimer_appended_when_nonempty(self):
        hits = [{"code": "600000", "time": "2026-09-19", "bars_ago": 1}]
        lines = format_alert_lines(hits, name_of=lambda c: "X")
        self.assertIn("", lines)  # 空行分隔
        self.assertTrue(lines[-1].startswith("📌"))
        self.assertIn("非买卖建议", lines[-1])

    def test_disclaimer_absent_when_empty(self):
        self.assertEqual(format_alert_lines([]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
