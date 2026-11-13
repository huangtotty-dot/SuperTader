# -*- coding: utf-8 -*-
"""选股猎手板块个股排序（2026-11-04 owner：「优先显示多头股票」）。

契约：展开板块的个股按二级分组展示；**每个分组内**把 日线三态=多头（build_stage===1）
排到前面，其余保持后端原序（GO → 通过数 → 建仓得分；Array.sort 稳定）。
只做源码契约（渲染函数依赖 DOM/ECharts，不在单测里跑）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_分组内多头优先():
    assert "for (const _gk of Object.keys(subGroups))" in _JS
    assert "(b.build_stage === 1 ? 1 : 0) - (a.build_stage === 1 ? 1 : 0)" in _JS
    # 必须发生在 subGroups 构造之后、分页/渲染之前（否则改了顺序又被打回）
    i_sort = _JS.index("(b.build_stage === 1 ? 1 : 0)")
    i_groups = _JS.index("const subGroups = {};")
    i_render = _JS.index("const stockRows = gPaged.map")
    assert i_groups < i_sort < i_render


def test_字段由后端注入_前端不重算():
    """build_stage 由后端 `_hunter_build_conformance` 注入（1=多头/2=震荡/3=空头），
    前端只按它排序；不要在前端另算一套 MA60 斜率。"""
    assert 's["build_stage"] = c.get("stage")' in (_ROOT / "gui.py").read_text(encoding="utf-8")
    assert "MA60" not in _JS.split("const subGroups = {};")[1].split("const stockRows")[0], \
        "排序区不应计算均线（口径只认后端 build_stage）"
