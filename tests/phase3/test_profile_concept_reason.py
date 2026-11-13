# -*- coding: utf-8 -*-
"""公司资料 · 「题材关联」一栏（owner 2026-11-08）。

需求：「龙江交通在公司资料中补充为什么它跟冰雪经济相关」。

实现：
  · 数据源 = `watchlist_jiuyan.json` 的 `jiuyan_concept_reason`（逐概念理由，
    2026-10-24 起写入），由 `tag_longjiang_snow_reason_20261108.py` 更新；
  · 后端 `load_stock_profile` 已下发 `concept_reasons`（原先只给 chip 悬停用）；
  · 前端 `renderStockProfile` 增加「题材关联」一栏，把理由**直接铺开**，不再只藏在 tooltip。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import gui  # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_龙江交通的冰雪经济理由已写入资料() -> None:
    api = gui.Api.__new__(gui.Api)
    d = api.load_stock_profile("601188", live=False)
    assert d["ok"] and d["name"] == "龙江交通"
    reasons = d.get("concept_reasons") or {}
    txt = reasons.get("旅游消费") or ""
    assert "冰雪经济" in txt
    assert "哈大高速" in txt, "要写清机制：哈大高速客流"
    assert "冰雪大世界" in txt, "要如实写明与冰雪大世界无直接业务"


def test_公司资料面板渲染题材关联一栏() -> None:
    assert "<b>题材关联</b>" in _JS
    assert "d.concept_reasons" in _JS or "(d && d.concept_reasons)" in _JS
    assert "_rsKeys.map(k =>" in _JS
