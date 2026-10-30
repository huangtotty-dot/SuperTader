# -*- coding: utf-8 -*-
"""⚓ 避风港湾（2026-10-29 owner：「新增一个避风港湾的板块，样式和建仓信号扫描一样」）。

守三条：
  ① 行构造与「异常监控池」**共用** `_pool_rows` —— 两个池的列/样式不可能各写一份漂掉；
  ② 增删走自己的 store（`t_gui.shelter_pool_*`），不污染异常监控池；
  ③ 前端 section / 渲染器就位，且复用建仓表那套单元格渲染（bias5/dev365/时机条件）。

铁律：全离线（`_hunter_build_conformance` / 标签批 / 均线回踩 全部打桩）。
运行：python -m pytest -q tests/phase3/test_shelter_pool.py
"""
import inspect
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402


class TestShelterPoolBackend(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self._fp = self.api._SHELTER_FP
        self.api._SHELTER_FP = Path(tempfile.mkdtemp(prefix="shelter_")) / "shelter_pool.json"
        self._orig = (t_gui._jiuyan_names, self.api._hunter_build_conformance,
                      self.api.load_stock_tags_batch, self.api._attach_ma_reclaim)
        t_gui._jiuyan_names = lambda: {"600900": "长江电力", "002176": "江特电机"}
        self.api._hunter_build_conformance = lambda codes, date=None: {}
        self.api.load_stock_tags_batch = lambda codes, offline=False, date=None: {"tags": {}}
        self.api._attach_ma_reclaim = lambda rows, date=None: None

    def tearDown(self):
        self.api._SHELTER_FP = self._fp
        (t_gui._jiuyan_names, self.api._hunter_build_conformance,
         self.api.load_stock_tags_batch, self.api._attach_ma_reclaim) = self._orig

    def test_两个池共用同一个行构造(self):
        self.assertIn("_pool_rows", inspect.getsource(t_gui.Api.load_abnormal_pool),
                      "异常监控池必须走共用行构造，否则两张表迟早各长一套")
        self.assertIn("_pool_rows", inspect.getsource(t_gui.Api.load_shelter_pool))

    def test_增删与行字段形状(self):
        self.assertTrue(self.api.shelter_pool_add("600900")["ok"])
        self.assertTrue(self.api.shelter_pool_add("江特电机")["ok"], "支持按名称加入")
        d = self.api.load_shelter_pool("2026-10-29")
        rows = d["rows"]
        self.assertEqual([r["code"] for r in rows], ["600900", "002176"])
        # 建仓表同款字段（前端直接复用那套单元格渲染）
        for k in ("verdict", "composite_score", "conditions", "build_met", "build_stage",
                  "tags", "dev365", "bias5", "_has_conf", "name"):
            self.assertIn(k, rows[0], f"缺少建仓表同款字段 {k}")
        self.assertIn("cond_labels", d)

    def test_不与异常监控池串味(self):
        self.api.shelter_pool_add("600900")
        self.assertNotIn("600900", self.api._abn_read(),
                         "避风港湾的加入不能写进异常监控池（反之亦然）")
        self.api.shelter_pool_remove("600900")
        self.assertEqual(self.api.load_shelter_pool()["rows"], [])

    def test_重复加入幂等(self):
        self.api.shelter_pool_add("600900")
        r = self.api.shelter_pool_add("600900")
        self.assertTrue(r.get("ok") and r.get("already"))
        self.assertEqual(len(self.api.load_shelter_pool()["rows"]), 1)

    def test_名称歧义与查无此票(self):
        t_gui._jiuyan_names = lambda: {"600001": "张三科技", "600002": "张三控股"}
        r = self.api.shelter_pool_add("张三")
        self.assertFalse(r["ok"])
        self.assertIn("匹配到多只", r.get("error", ""))
        r2 = self.api.shelter_pool_add("不存在的名字")
        self.assertFalse(r2["ok"])


class TestShelterPoolFrontend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")
        cls.html = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")

    def test_区块在手动盘tab里(self):
        self.assertIn('id="shelter-pool"', self.html)
        self.assertIn("避风港湾", self.html)
        # 必须落在 tab-signals 内、abnormal-pool 之后
        self.assertLess(self.html.index('id="tab-signals"'), self.html.index('id="shelter-pool"'))
        self.assertLess(self.html.index('id="shelter-pool"'), self.html.index('id="tab-hunter"'))

    def test_渲染器复用建仓表单元格(self):
        i = self.js.index("function renderShelterPool")
        seg = self.js[i:i + 4000]
        for fn in ("verdictBadge(", "condCellHtml(", "bias5CellHtml(", "dev365CellHtml(",
                   "ma20FlagCellHtml(", "maReclaimCellHtml("):
            self.assertIn(fn, seg, f"避风港湾必须复用 {fn}，否则样式会与建仓表漂掉")

    def test_加载与增删就位(self):
        for fn in ("function loadShelterPool", "function renderShelterPool",
                   "function shelterAdd", "function shelterRemove"):
            self.assertIn(fn, self.js)
        self.assertIn('apiCall("load_shelter_pool"', self.js)
        self.assertIn('apiCall("shelter_pool_add"', self.js)
        self.assertIn('apiCall("shelter_pool_remove"', self.js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
