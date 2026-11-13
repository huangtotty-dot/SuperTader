# -*- coding: utf-8 -*-
"""飞书卡片构建器单测 —— core/feishu_cards.py（2026-11-13 补）。

2026-11-13 把 main.py 与 execution/daily_sentiment.py 里两份逐字相同的
`_feishu_md_div`/`_feishu_hr`/`_feishu_card_header` 收敛成单一真源，两个调用方
`import ... as 旧名` 保持调用点不变。本测试钉住三个元素的结构契约（飞书卡片 JSON
schema），防止有人改坏结构导致卡片渲染失败而不自知。

运行：python tests/phase3/test_feishu_cards.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.feishu_cards import feishu_card_header, feishu_hr, feishu_md_div  # noqa: E402


class TestFeishuCards(unittest.TestCase):
    def test_md_div(self):
        self.assertEqual(
            feishu_md_div("**加粗**"),
            {"tag": "div", "text": {"content": "**加粗**", "tag": "lark_md"}},
        )

    def test_hr(self):
        self.assertEqual(feishu_hr(), {"tag": "hr"})

    def test_card_header(self):
        self.assertEqual(
            feishu_card_header("标题", "blue"),
            {"template": "blue", "title": {"tag": "plain_text", "content": "标题"}},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
