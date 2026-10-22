# -*- coding: utf-8 -*-
"""异常监控池（2026-10-22 owner：「建仓信号扫描下面加一个异常监控池，样式一样，可以搜索加载」）。

口径（owner 已确认）：**手动加入**，不做任何"异常"自动判定。
  · 存 `t_io/state/abnormal_pool.json`；
  · 行数据由 `_hunter_build_conformance` 现算（→ `core.build_decision`），与建仓表/猎手**同一真源**；
  · 前端列与样式复用建仓表的共享单元格渲染，不另写一套。
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(t_gui.Api, "_ABN_FP", tmp_path / "abnormal_pool.json")
    return t_gui.Api()


class TestAddRemove:
    def test_add_by_code(self, api):
        r = api.abnormal_pool_add("600519")
        assert r["ok"] and r["code"] == "600519"
        assert r["code"] in api._abn_read()

    def test_add_by_exact_name(self, api):
        r = api.abnormal_pool_add("贵州茅台")
        assert r["ok"] and r["code"] == "600519"

    def test_add_by_partial_name(self, api):
        r = api.abnormal_pool_add("贵州茅")
        assert r["ok"] and r["code"] == "600519"

    def test_ambiguous_name_is_rejected(self, api):
        """「润建」同时命中 002062(宏润建设)/002929(润建股份) ⇒ 要求输完整代码，别猜。"""
        r = api.abnormal_pool_add("润建")
        assert r["ok"] is False and "多只" in r["error"]

    def test_duplicate_is_idempotent(self, api):
        api.abnormal_pool_add("600519")
        r = api.abnormal_pool_add("600519")
        assert r["ok"] and r.get("already") is True
        assert len(api._abn_read()) == 1

    def test_unknown_name_errors(self, api):
        r = api.abnormal_pool_add("不存在的股票名XYZ")
        assert r["ok"] is False and "没找到" in r["error"]

    def test_remove(self, api):
        api.abnormal_pool_add("600519")
        api.abnormal_pool_remove("600519")
        assert api._abn_read() == {}


class TestLoad:
    def test_empty_pool(self, api):
        d = api.load_abnormal_pool("2026-10-22")
        assert d["has_data"] is True and d["rows"] == []

    def test_rows_carry_pb_fields(self, api):
        api.abnormal_pool_add("002929")
        d = api.load_abnormal_pool("2026-10-22")
        rows = d["rows"]
        assert len(rows) == 1
        r = rows[0]
        for k in ("code", "name", "verdict", "composite_score", "conditions",
                  "timing_features", "build_stage", "build_met", "build_reason"):
            assert k in r, f"缺字段 {k}（要与建仓表同列）"
        assert r["_has_conf"] is True
        # 与建仓表同一个判定来源：verdict 必属这套枚举
        assert r["verdict"] in ("signal", "approaching", "weak", "watch_signal",
                                "insufficient_data", "pending")

    def test_sorted_by_score(self, api):
        for c in ("002929", "600519"):
            api.abnormal_pool_add(c)
        rows = api.load_abnormal_pool("2026-10-22")["rows"]
        assert [r["composite_score"] for r in rows] == sorted(
            [r["composite_score"] for r in rows], reverse=True)


class TestFrontendWiring:
    def test_card_below_pb(self):
        i_pb = _HTML.index('id="pbBody"')
        i_abn = _HTML.index('id="abnormalPoolBody"')
        assert i_abn > i_pb, "异常监控池要排在「建仓信号扫描」下面"
        assert 'id="abnSearch"' in _HTML and "abnormalAdd()" in _HTML

    def test_reuses_shared_cells(self):
        i = _JS.index("function renderAbnormalPool")
        seg = _JS[i:i + 3000]
        for fn in ("verdictBadge", "condCellHtml", "ma20FlagCellHtml",
                   "hunterPriceHtml", "hunterMktcapHtml"):
            assert fn in seg, f"单元格必须复用 {fn}（与建仓表同一套样式）"
