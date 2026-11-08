# -*- coding: utf-8 -*-
"""板块星轨图 · 二级细分贡献度（owner 2026-11-08）。

需求：「大板块（如电力）排名上升时，要能显示各个二级板块的贡献度」。

实现分三层：
  1. `stock_hunter/modules/data_loader.py::load_concept_summary` 把每个板块的**二级细分**
     拆解（股票数 / 得分和 / 涨停数 / 平均分）挂在 `DataFrame.attrs["sub_details"]`；
  2. 猎手两条落盘路径（GUI `_load_hunter_impl`、CLI `_save_sector_history`）把拆解写进
     `daily_summary.json` 每行；历史 10 日由 `scripts/backfill_sector_subdetail.py` 回填；
  3. 后端 `Api.load_sector_contrib(board, days, end)` + 前端点板块 → 面板列出
     「细分 / 股票数 / 得分和 / 涨停 / Δ得分（vs 前一日）」。
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "stock_hunter")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

import t_gui  # noqa: E402
from modules.data_loader import DataLoader  # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _loader() -> DataLoader:
    l = DataLoader(config={})
    df = pd.DataFrame([
        {"代码": "600001", "名称": "甲", "韭研分类": "电力", "韭研概念": "水电|绿电",
         "总得分": 10, "涨停": 1, "成交额": 1e8},
        {"代码": "600002", "名称": "乙", "韭研分类": "电力", "韭研概念": "水电",
         "总得分": 6, "涨停": 0, "成交额": 1e8},
        {"代码": "600003", "名称": "丙", "韭研分类": "电力", "韭研概念": "火电",
         "总得分": 4, "涨停": 0, "成交额": 1e8},
    ])
    l.set_watchlist(df)
    return l


def test_细分明细_按细分聚合() -> None:
    s = _loader().load_concept_summary()
    det = s.attrs.get("sub_details") or {}
    subs = {x["细分"]: x for x in det.get("电力", [])}
    assert subs["水电"]["股票数"] == 2
    assert subs["水电"]["得分和"] == 16.0      # 10 + 6
    assert subs["水电"]["涨停数"] == 1
    assert subs["绿电"]["股票数"] == 1 and subs["绿电"]["得分和"] == 10.0
    assert subs["火电"]["得分和"] == 4.0
    # 一只票同时计入多个细分 ⇒ 各细分股票数之和 ≥ 板块股票数
    assert sum(x["股票数"] for x in det["电力"]) == 4 > 3


def _write_fake_summary(base: Path) -> None:
    day1 = [{"排名": 12, "板块": "电力", "平均分": 4.0, "涨停数": 1, "股票数": 250,
             "细分明细": [{"细分": "水电", "股票数": 10, "得分和": 30.0, "涨停数": 1, "平均分": 3.0},
                          {"细分": "火电", "股票数": 8, "得分和": 20.0, "涨停数": 0, "平均分": 2.5}]}]
    day2 = [{"排名": 9, "板块": "电力", "平均分": 4.7, "涨停数": 2, "股票数": 258,
             "细分明细": [{"细分": "水电", "股票数": 12, "得分和": 50.0, "涨停数": 2, "平均分": 4.17},
                          {"细分": "火电", "股票数": 8, "得分和": 20.0, "涨停数": 0, "平均分": 2.5}]}]
    (base / "stock_hunter" / "history").mkdir(parents=True, exist_ok=True)
    (base / "stock_hunter" / "history" / "daily_summary.json").write_text(
        json.dumps({"20261105": day1, "20261106": day2}, ensure_ascii=False), encoding="utf-8")
    (base / "t_io" / "cache" / "daily_kline").mkdir(parents=True, exist_ok=True)
    (base / "t_io" / "cache" / "daily_kline" / "index_sh000001.json").write_text(
        json.dumps({"rows": [{"date": "2026-11-05"}, {"date": "2026-11-06"}]}), encoding="utf-8")


def test_contrib接口_按板块返回每日细分(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(t_gui, "BASE", tmp_path)
    _write_fake_summary(tmp_path)
    api = t_gui.Api.__new__(t_gui.Api)
    r = api.load_sector_contrib("电力", 10)
    assert r["available"] and r["has_detail"]
    assert [d["ymd"] for d in r["days"]] == ["20261105", "20261106"]
    last, prev = r["days"][-1], r["days"][-2]
    assert last["rank"] == 9 and prev["rank"] == 12
    subs = {x["细分"]: x for x in last["subs"]}
    assert subs["水电"]["得分和"] == 50.0 and subs["水电"]["涨停数"] == 2


def test_contrib接口_未知板块(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(t_gui, "BASE", tmp_path)
    _write_fake_summary(tmp_path)
    api = t_gui.Api.__new__(t_gui.Api)
    r = api.load_sector_contrib("不存在的板块", 10)
    assert r["available"] is False and "没有排名记录" in r["error"]


def test_前端_点板块出贡献面板() -> None:
    assert 'id="sectorContrib"' in _HTML
    assert "function selectStarBoard(name)" in _JS
    assert 'apiCall("load_sector_contrib", name, 10, _panelDate())' in _JS
    assert "function renderSectorContrib(d)" in _JS
    assert 'Δ对比' in _JS, "面板要写明是与哪一天对比"
    assert '各细分之和 ≥ 板块股票数' in _JS, "口径提示必须在面板里"


def test_右侧标签显示贡献度() -> None:
    """owner 2026-11-08：「贡献度的显示要在图中的板块右侧显示」。

    选中板块 ⇒ endLabel（末端直标）展开成多行：板块名 + 名次变化 + Top3 细分 Δ得分；
    未选中仍是单行板块名；右侧 grid 余量随选中状态变宽。
    """
    assert "const _contribLabel = (name) => {" in _JS
    assert "_contribLabel(sec.name)" in _JS
    assert 'formatter: _txt || sec.name' in _JS
    assert 'grid: { left: 56, right: _starSelBoard ? 260 : 150, top: 34, bottom: 30 }' in _JS
    assert "right: _starSelBoard ? 260 : 150" in _JS
    assert "renderSectorStars(_starLast));   // 右侧标签展开" in _JS


def test_点击板块弹出浮层() -> None:
    """owner 2026-11-08：「点击具体板块时，贡献度显示在弹出来的框中」。"""
    assert "function _ensureStarPop()" in _JS
    assert "function showStarContribPop(name, x, y)" in _JS
    assert "function _fillStarPop(name, d)" in _JS
    assert 'showStarContribPop(nm, _x, _y);' in _JS
    assert 'id="starContribPop"' in _JS or 'el.id = "starContribPop"' in _JS
    assert "_fillStarPop(name, d);  // 弹出框填充" in _JS
    assert 'onclick="hideStarContribPop()"' in _JS, "浮层要有关闭按钮"


def test_下拉选择也能看贡献度() -> None:
    """点图命中不稳时的兜底：标题栏下拉选板块 → 同样弹浮层。"""
    assert 'id="starBoardSel"' in _HTML
    assert 'onchange="if(this.value) selectStarBoardFromSel(this.value)"' in _HTML
    assert "function selectStarBoardFromSel(name)" in _JS
    assert "showStarContribPop(name, r.left, r.bottom + 6)" in _JS
