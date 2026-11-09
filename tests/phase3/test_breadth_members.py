# -*- coding: utf-8 -*-
"""主线广度表「点数字看涨停个股」（owner 2026-11-09）。

需求：「增加一个功能，点击数量后可以显示具体哪几只股票涨停」。

实现：
  · `modules.sector_phase.members_of(kind, label, date)` —— kind=theme/branch/section，
    按主题/AI链分支/原始分段取当天 members，同一只票多分段时按代码去重、board 取最大；
  · `Api.get_theme_members()` 透传；
  · 前端 `renderBreadthTable` 的格子加 onclick → `showBreadthMembers()` 弹出浮层
    （复用星轨图的浮层节点，关闭走 hideStarContribPop）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "stock_hunter"))

import t_gui  # noqa: E402
from modules.sector_phase import members_of  # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def test_主题成员_商业航天():
    r = members_of("theme", "商业航天", "2026-11-09")
    assert r["available"] and r["count"] == 11
    names = {m["name"] for m in r["members"]}
    assert {"顺灏股份", "润贝航科"} <= names
    top = r["members"][0]
    assert top["board"] >= 2, "按连板数降序，顺灏股份 4 板应在前面"


def test_分支成员_机器人():
    r = members_of("branch", "机器人", "2026-11-09")
    assert r["available"] and r["count"] == 11


def test_原始分段成员_寒潮():
    r = members_of("section", "寒潮", "2026-11-09")
    assert r["available"] and r["count"] == 6
    assert any(m["name"] == "日出东方" for m in r["members"])


def test_主题成员_去重且取最大板数():
    r = members_of("theme", "AI链", "2026-11-09")
    codes = [m["code"] for m in r["members"]]
    assert len(codes) == len(set(codes)), "同一只票不应重复出现"
    assert r["count"] == 13


def test_缺失日期返回不可用():
    r = members_of("theme", "商业航天", "2020-01-01")
    assert r["available"] is False and "没有复盘数据" in r["error"]


def test_接口透传():
    api = t_gui.Api.__new__(t_gui.Api)
    r = api.get_theme_members("theme", "冰雪经济", "2026-11-09")
    assert r["available"] and r["count"] == 5


def test_前端点击接线():
    assert "function showBreadthMembers(kind, label, date, ev)" in _JS
    assert "onclick=\"showBreadthMembers('${_kind}','${esc(r.name)}','${esc(d)}',event)\"" in _JS
    assert 'renderBreadthTable(themes, dates, { name: "主题", kind: "theme" })' in _JS
    assert 'renderBreadthTable(ai, dates, { name: "分支", kind: "branch" })' in _JS
