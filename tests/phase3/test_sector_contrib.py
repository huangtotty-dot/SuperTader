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
    # 2026-11-08：每个细分下带「贡献最大的个股」（按得分降序）
    top1 = subs["水电"]["个股"]
    assert [x["名称"] for x in top1] == ["甲", "乙"]
    assert top1[0]["得分"] == 10.0 and top1[0]["涨停"] == 1
    assert subs["火电"]["个股"][0]["名称"] == "丙"


def _write_fake_summary(base: Path) -> None:
    day1 = [{"排名": 12, "板块": "电力", "平均分": 4.0, "涨停数": 1, "股票数": 250,
             "细分明细": [{"细分": "水电", "股票数": 10, "得分和": 30.0, "涨停数": 1, "平均分": 3.0},
                          {"细分": "火电", "股票数": 8, "得分和": 20.0, "涨停数": 0, "平均分": 2.5}]}]
    day2 = [{"排名": 9, "板块": "电力", "平均分": 4.7, "涨停数": 2, "股票数": 258,
             "细分明细": [{"细分": "水电", "股票数": 12, "得分和": 50.0, "涨停数": 2, "平均分": 4.17,
                           "个股": [{"代码": "600001", "名称": "甲", "得分": 10.0, "涨停": 1}]},
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
    assert subs["水电"]["个股"][0]["名称"] == "甲", "个股明细要透传给前端"


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
    assert 'formatter: _txt || _plain' in _JS
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


def test_关闭后不留残影() -> None:
    """owner 2026-11-08：「取消贡献度显示后仍然发现有窗口残影」。

    根因：原来只把浮层 display:none —— WebView2 对被 fixed 浮层盖住的画布区域
    不一定重绘，屏幕上残留旧画面。修法：关闭时**移除 DOM 节点**（强制重绘）
    + 顺手收起图表右侧展开的多行标签。
    """
    assert "try { _starPopEl.remove(); } catch (e)" in _JS
    assert "_starPopEl = null;" in _JS
    assert 'if (_starSelBoard) {\n    _starSelBoard = "";' in _JS
    assert "_starLast) echGuard(\"板块星轨图\", () => renderSectorStars(_starLast));" in _JS
    assert "transform:translateZ(0);" in _JS, "浮层自身也开合成层，减少残影"


def test_上升速度用多色并标注() -> None:
    """owner 2026-11-08：「上升速度排名靠前的板块不要只用红色，也要用其它颜色，并且标注出来」。

    三档：↑1~2 红 / ↑3~5 橙 / ↑≥6 品红；末端直标带 `↑名次`，说明区有颜色图例。
    """
    assert 'const SECTOR_STAR_RISE_MID = "#f0883e";' in _JS
    assert 'const SECTOR_STAR_RISE_FAST = "#bf3989";' in _JS
    assert "function starRiseColor(up)" in _JS
    assert "mk(s, isRose ? starRiseColor(roseUp.get(s.name) || 0) : SECTOR_STAR_MUTED, isRose)" in _JS
    assert "const _plain = isHl && _up ? `${sec.name} ↑${_up}` : sec.name;" in _JS
    assert "■ 橙 ↑${STAR_RISE_MID_MIN}~${STAR_RISE_FAST_MIN - 1}" in _JS
    assert "红/橙/品红线" in _HTML or "品红" in _HTML


def test_每个细分最多十只个股() -> None:
    """owner 2026-11-08：「二级板块下再显示贡献度大的个股，为了控制数量，各细分不要超过 10 个」。"""
    import pandas as pd
    l = DataLoader(config={})
    rows = [{"代码": f"6000{i:02d}", "名称": f"股{i}", "韭研分类": "电力", "韭研概念": "水电",
             "总得分": 20 - i, "涨停": 0, "成交额": 1e8} for i in range(15)]
    l.set_watchlist(pd.DataFrame(rows))
    det = l.load_concept_summary().attrs["sub_details"]["电力"][0]
    assert len(det["个股"]) == 10, "每细分最多 10 只"
    assert det["个股"][0]["名称"] == "股0", "按得分降序"
    assert det["个股"][-1]["名称"] == "股9"


def test_前端细分行可展开个股() -> None:
    assert "function toggleSubStocks(tr)" in _JS
    assert "onclick=\"toggleSubStocks(this)\"" in _JS
    assert "_st.slice(0, 10)" in _JS, "前端也截断到 10"
    assert "sub-stock-row" in _JS
