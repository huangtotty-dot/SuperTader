# -*- coding: utf-8 -*-
"""飞书卡片构建器（2026-11-13 收敛 main.py 与 execution/daily_sentiment.py 的两份重复副本）。

之前 `main.py` 有 `_feishu_md_div`/`_feishu_hr`/`_feishu_card_header`，
`execution/daily_sentiment.py` 有逐字相同的 `_feishu_*_fn` 三兄弟 —— 纯冗余。
此处为单一真源，两个调用方各自 `import ... as 旧名` 保持调用点不变。
"""
from __future__ import annotations


def feishu_md_div(content: str) -> dict:
    """飞书卡片 markdown 元素。"""
    return {"tag": "div", "text": {"content": content, "tag": "lark_md"}}


def feishu_hr() -> dict:
    """飞书卡片分割线元素。"""
    return {"tag": "hr"}


def feishu_card_header(title: str, template: str) -> dict:
    """飞书卡片头部。"""
    return {"template": template, "title": {"tag": "plain_text", "content": title}}
