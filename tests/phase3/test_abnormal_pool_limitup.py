# -*- coding: utf-8 -*-
"""异常监控池「按某日累加当日涨停」（2026-11-03 owner）。

owner：「增加时间选项，当运行某日将当天的涨停的股票进行添加。重点：可以需要累加，
同时注明是首版还是二板还是三板」。

守四条：
  ① 数据源 = 猎手当日 `stock_hunter/data/market_YYYYMMDD.csv` 的 `涨停=1` 行，
     `连板天数` 即板数（1=首板、2=二板…）；
  ② **累加**：只增不删；已有票不改名、不动手动填的涨停原因；
  ③ 同一只票多日出现时**以更晚的日期为准**（不回退），同日同板重复点 = 不变；
  ④ 前端有日期选择框 + 「🚀 加入当日涨停」按钮，行内显示 首板/2板/3板 徽章。

运行：python -m unittest tests.phase3.test_abnormal_pool_limitup -v
"""
import csv
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import gui  # noqa: E402

JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _write_csv(fp: Path, rows, data_date=""):
    """rows: [(代码, 名称, 涨停, 连板天数)]；写成带 BOM 的 CSV（与猎手落盘一致）。"""
    with open(fp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "现价", "涨跌幅", "涨停", "连板天数", "首板涨停",
                    "涨停价", "数据日期"])
        for code, name, zt, board in rows:
            w.writerow([code, name, "10.00", "10.00", zt, board,
                        1 if int(board) == 1 else 0, "10.00", data_date])


class TestParseMarketCsv(unittest.TestCase):
    def test_只取涨停并按连板数降序(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "market_20261102.csv"
            _write_csv(fp, [
                ("000001", "平安银行", 0, 0),
                ("600000", "浦发银行", 1, 2),
                ("300001", "", 1, 1),
                ("000002", "万科A", 1, 3),
            ])
            lst = gui._limitup_from_market_csv(fp)
        self.assertEqual([x["code"] for x in lst], ["000002", "600000", "300001"])
        self.assertEqual([x["board"] for x in lst], [3, 2, 1])
        self.assertEqual(lst[2]["name"], "", "CSV 名称列常为空，名称由概念表兜底")
        self.assertEqual(lst[1]["limit_price"], 10.0, "涨停价要一起解析出来（涨停时间要用）")

    def test_文件缺失返回空(self):
        self.assertEqual(gui._limitup_from_market_csv(Path("no_such.csv")), [])

    def test_日期路径(self):
        p = gui._market_csv_fp("2026-11-02")
        self.assertTrue(str(p).replace("\\", "/").endswith("stock_hunter/data/market_20261102.csv"))
        self.assertIsNone(gui._market_csv_fp("11-02"))


class TestAddLimitupAccumulates(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.csv = {}
        self._old_fp = gui._market_csv_fp
        self._old_names = gui._concept_names
        # 与生产一致：合法日期永远给出一条路径（文件不存在由解析函数返回 []）
        gui._market_csv_fp = lambda d: self.tmp / f"market_{str(d)[:10].replace('-', '')}.csv"
        gui._concept_names = lambda: {"600000": "浦发银行", "300001": "特锐德"}
        self.api = gui.Api()
        self.api._ABN_FP = self.tmp / "abnormal_pool.json"
        # 测试禁网：腾讯兜底默认返回空，个别用例再覆盖
        self.api._tencent_limit_time = lambda code, day, lp: ("", None, "")
        # 同花顺涨停池默认空（个别用例覆盖），避免测试真联网
        self._old_ths = gui._limitup_from_ths
        gui._limitup_from_ths = lambda d, timeout=12.0, retries=2: []
        # 复盘兜底默认空（个别用例覆盖），避免读真实 sector_breadth.json
        self._old_recap = gui._limitup_from_recap
        gui._limitup_from_recap = lambda d, fp=None: []
        # 涨停题材索引默认空（个别用例覆盖），避免读真实 sector_breadth.json
        self._old_theme = gui._limitup_theme_index
        gui._limitup_theme_index = lambda fp=None: {}
        # 手工题材分类默认空（个别用例覆盖），避免读真实 config/market/limitup_theme_manual.json
        self._old_manual = gui._limitup_manual_theme_index
        gui._limitup_manual_theme_index = lambda fp=None: {}
        # 东财涨停池默认空（个别用例覆盖），避免测试真联网
        self._old_em = gui._limitup_from_em
        gui._limitup_from_em = lambda d, timeout=12.0, retries=2: {}

    def tearDown(self):
        gui._market_csv_fp = self._old_fp
        gui._concept_names = self._old_names
        gui._limitup_from_ths = self._old_ths
        gui._limitup_from_recap = self._old_recap
        gui._limitup_theme_index = self._old_theme
        gui._limitup_manual_theme_index = self._old_manual
        gui._limitup_from_em = self._old_em
        self._td.cleanup()

    def _mk(self, date, rows, data_date=""):
        fp = self.tmp / f"market_{date.replace('-', '')}.csv"
        _write_csv(fp, rows, data_date=data_date)
        self.csv[date] = fp

    def _pool(self):
        return json.loads(self.api._ABN_FP.read_text(encoding="utf-8"))["stocks"]

    def test_累加且一票多日(self):
        self._mk("2026-11-02", [("600000", "", 1, 2), ("300001", "", 1, 1)])
        r1 = self.api.abnormal_pool_add_limitup("2026-11-02")
        self.assertTrue(r1["ok"])
        self.assertEqual((r1["added"], r1["updated"], r1["pool"]), (2, 0, 2))
        p = self._pool()
        self.assertEqual(p["600000"]["limit_board"], 2)
        self.assertEqual(p["300001"]["limit_board"], 1)
        self.assertEqual(p["600000"]["name"], "浦发银行", "名称用概念表兜底")

        # 手动填的涨停原因不能被累加覆盖
        self.api.abnormal_pool_set_reason("600000", "银行+国资")
        # 11-03：600000 升到 3 板，新加一只
        self._mk("2026-11-03", [("600000", "", 1, 3), ("000002", "", 1, 1)])
        r2 = self.api.abnormal_pool_add_limitup("2026-11-03")
        self.assertEqual((r2["added"], r2["updated"], r2["unchanged"], r2["pool"]), (1, 1, 0, 3))
        p = self._pool()
        # 一票多日：600000 两个涨停日**各记一个事件**（不再被更晚日期吞掉）
        self.assertEqual(sorted(p["600000"]["limit_dates"].keys()), ["2026-11-02", "2026-11-03"])
        self.assertEqual(p["600000"]["limit_dates"]["2026-11-02"]["board"], 2)
        self.assertEqual(p["600000"]["limit_dates"]["2026-11-03"]["board"], 3)
        # 旧单字段仍镜像「最新事件」（供旧读取路径直接读原始池 JSON）
        self.assertEqual(p["600000"]["limit_board"], 3)
        self.assertEqual(p["600000"]["limit_date"], "2026-11-03")
        self.assertEqual(p["600000"]["reason"], "银行+国资", "只增不改：手动原因要保留")

        # 同一天再点一次 = 全部不变
        r3 = self.api.abnormal_pool_add_limitup("2026-11-03")
        self.assertEqual((r3["added"], r3["updated"], r3["unchanged"]), (0, 0, 2))

        # 再点更早的 11-02 = 不新增不覆盖（600000 那两个事件已在，板数各保持）
        r4 = self.api.abnormal_pool_add_limitup("2026-11-02")
        self.assertEqual(r4["added"], 0)
        self.assertEqual(self._pool()["600000"]["limit_board"], 3, "旧日期不能把最新板数改回去")
        self.assertEqual(self._pool()["600000"]["limit_dates"]["2026-11-02"]["board"], 2)

    def test_一票多日每行各列(self):
        """一票多日：同一只票每个涨停日**各出一行**（前端按日期分段靠它）。"""
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api.abnormal_pool_add_limitup("2026-11-02")
        self._mk("2026-11-03", [("600000", "", 1, 3)])
        self.api.abnormal_pool_add_limitup("2026-11-03")
        api = self.api
        api._hunter_build_conformance = lambda codes, date: {}
        api.load_stock_tags_batch = lambda *a, **k: {"tags": {}}
        api._limitup_phase_rows = lambda codes, names, date: {}
        rows = api._pool_rows(api._abn_read(), "2026-11-03", "")["rows"]
        self.assertEqual(len(rows), 2, "600000 两日涨停应出两行")
        self.assertEqual(sorted(r["limit_date"] for r in rows),
                         ["2026-11-02", "2026-11-03"])
        self.assertEqual(sorted(r["limit_board"] for r in rows), [2, 3])

    def test_同花顺全市场优先且板数精修(self):
        """2026-11-15 owner「用同花顺的股票数量」：同花顺全市场名单优先于猎手概念池。
        同花顺只给 首板/非首板 下界，板数用猎手 CSV「连板天数」精修。"""
        gui._limitup_from_ths = lambda d, timeout=12.0, retries=2: [
            {"code": "600000", "name": "", "board": 1, "data_date": d, "tag": "FIRST_LIMIT"},
            {"code": "000002", "name": "", "board": 2, "data_date": d, "tag": "LIMIT_BACK"},
            {"code": "300999", "name": "", "board": 2, "data_date": d, "tag": "LIMIT_BACK"},  # 不在 CSV 里
        ]
        # 猎手 CSV 有 600000 的精确连板天数=3（覆盖同花顺下界 1）
        self._mk("2026-11-02", [("600000", "", 1, 3)])
        r = self.api.abnormal_pool_add_limitup("2026-11-02")
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "ths")
        self.assertEqual(r["total"], 3, "同花顺全市场 3 只，多过猎手 CSV 的 1 只")
        p = self._pool()
        self.assertEqual(p["600000"]["limit_dates"]["2026-11-02"]["board"], 3, "CSV 连板天数覆盖同花顺下界")
        self.assertEqual(p["000002"]["limit_dates"]["2026-11-02"]["board"], 2, "无 CSV 时用同花顺下界")
        self.assertEqual(p["300999"]["name"], "300999", "概念表没有的票名称退回代码")

    def test_东财连板数兜底同花顺下界(self):
        """2026-11-15 owner「板数用东财 lbc」：既不在猎手 CSV 也不在韭研复盘的票，板数只剩
        同花顺首板/非首板下界（1/2）⇒ 用东财真实连板数兜底。猎手 CSV 仍优先于东财。"""
        gui._limitup_from_ths = lambda d, timeout=12.0, retries=2: [
            {"code": "600000", "name": "", "board": 1, "data_date": d, "tag": "FIRST_LIMIT"},
            {"code": "000002", "name": "", "board": 2, "data_date": d, "tag": "LIMIT_BACK"},   # 只东财有
            {"code": "300999", "name": "", "board": 2, "data_date": d, "tag": "LIMIT_BACK"},   # 三源皆无
        ]
        # 猎手 CSV 有 600000 的精确连板天数=3；东财给 600000=4（应被 CSV 盖掉）、000002=5
        self._mk("2026-11-02", [("600000", "", 1, 3)])
        gui._limitup_from_em = lambda d, timeout=12.0, retries=2: {"600000": 4, "000002": 5}
        r = self.api.abnormal_pool_add_limitup("2026-11-02")
        self.assertTrue(r["ok"])
        p = self._pool()
        self.assertEqual(p["600000"]["limit_dates"]["2026-11-02"]["board"], 3,
                         "猎手 CSV 连板天数优先于东财")
        self.assertEqual(p["000002"]["limit_dates"]["2026-11-02"]["board"], 5,
                         "东财真连板数覆盖同花顺下界")
        self.assertEqual(p["300999"]["limit_dates"]["2026-11-02"]["board"], 2,
                         "三源皆无时保留同花顺下界")

    def test_手工题材分类优先于韭研复盘(self):
        """2026-11-15 owner「逐一分类」：手工分类（同花顺涨停原因口径）优先于韭研 section。"""
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api.abnormal_pool_add_limitup("2026-11-02")
        gui._limitup_manual_theme_index = lambda fp=None: {"2026-11-02": {"600000": "AI影视"}}
        gui._limitup_theme_index = lambda fp=None: {"2026-11-02": {"600000": "半导体"}}
        api = self.api
        api._hunter_build_conformance = lambda codes, date: {}
        api.load_stock_tags_batch = lambda *a, **k: {"tags": {}}
        api._limitup_phase_rows = lambda codes, names, date: {}
        rows = api._pool_rows(api._abn_read(), "2026-11-02", "")["rows"]
        self.assertEqual(rows[0]["limit_theme"], "AI影视", "手工分类优先于韭研复盘")

    def test_以CSV的数据日期为准(self):
        """周末补跑的 market_20261031.csv 实际装的是 10-30 行情 —— 不能按文件名计板数。"""
        self._mk("2026-10-31", [("600000", "", 1, 2)], data_date="2026-10-30")
        r = self.api.abnormal_pool_add_limitup("2026-10-31")
        self.assertTrue(r["ok"])
        self.assertEqual(r["date"], "2026-10-30")
        self.assertIn("note", r)
        self.assertEqual(self._pool()["600000"]["limit_date"], "2026-10-30")

    def test_该日无快照给可用日期(self):
        self._mk("2026-11-02", [("600000", "", 1, 1)])
        r = self.api.abnormal_pool_add_limitup("2026-11-05")
        self.assertFalse(r["ok"])
        self.assertIn("没有市场快照", r["error"])
        self.assertIn("20261102", r["error"])

    def test_行里带板数(self):
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api.abnormal_pool_add_limitup("2026-11-02")
        api = self.api
        api._hunter_build_conformance = lambda codes, date: {}
        api.load_stock_tags_batch = lambda *a, **k: {"tags": {}}
        rows = api._pool_rows(api._abn_read(), "2026-11-02", "")["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["limit_board"], 2)
        self.assertEqual(rows[0]["limit_date"], "2026-11-02")

    def test_行里带涨停题材(self):
        """2026-11-15 owner：「每一日按韭研复盘题材分类」——题材来自 sector_breadth 当日 section。"""
        self._mk("2026-11-02", [("600000", "", 1, 2), ("300001", "", 1, 1)])
        self.api.abnormal_pool_add_limitup("2026-11-02")
        # 600000 命中「半导体」，300001 不在复盘里 ⇒ 空（前端归「其他」）
        gui._limitup_theme_index = lambda fp=None: {"2026-11-02": {"600000": "半导体"}}
        api = self.api
        api._hunter_build_conformance = lambda codes, date: {}
        api.load_stock_tags_batch = lambda *a, **k: {"tags": {}}
        api._limitup_phase_rows = lambda codes, names, date: {}
        rows = api._pool_rows(api._abn_read(), "2026-11-02", "")["rows"]
        by_code = {r["code"]: r for r in rows}
        self.assertEqual(by_code["600000"]["limit_theme"], "半导体")
        self.assertEqual(by_code["300001"]["limit_theme"], "")


    def test_加入时补算涨停时间(self):
        """2026-11-03 owner：涨停原因里要带「涨停时间」——通达信 1 分钟线首个触及涨停价的分钟。"""
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api._tdx_first_limit_time = lambda code, day, lp: (
            "09:35", True, "2026-11-02")
        r = self.api.abnormal_pool_add_limitup("2026-11-02")
        self.assertEqual(r["time_filled"], 1)
        p = self._pool()["600000"]
        self.assertEqual(p["limit_time"], "09:35")
        self.assertTrue(p["limit_sealed"])

    def test_补时间只补空的(self):
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api._tdx_first_limit_time = lambda code, day, lp: ("", None, "2026-10-14")
        r = self.api.abnormal_pool_add_limitup("2026-11-02")
        self.assertEqual(r["time_filled"], 0)
        self.assertIn("time_note", r, "通达信没覆盖到该日要明确提示")
        # 更新通达信数据后再补
        self.api._tdx_first_limit_time = lambda code, day, lp: ("09:35", True, "2026-11-02")
        r2 = self.api.abnormal_pool_fill_limit_times("2026-11-02")
        self.assertEqual((r2["filled"], r2["missing"]), (1, 0))
        self.assertEqual(self._pool()["600000"]["limit_time"], "09:35")


    def test_按涨停时间排序(self):
        """owner：「太多了很乱，按时间的顺序显示」。"""
        self._mk("2026-11-02", [("600000", "", 1, 2), ("300001", "", 1, 1),
                                ("000002", "", 1, 1)])
        times = {"600000": "10:00", "300001": "09:31"}   # 000002 算不出时间
        self.api._tdx_first_limit_time = lambda code, day, lp: (
            times.get(code, ""), True, "2026-11-02")
        self.api.abnormal_pool_add_limitup("2026-11-02")
        # 再手动加一只（无涨停日期）——应排在所有涨停票之后
        self.api.abnormal_pool_add("601398")
        api = self.api
        api._hunter_build_conformance = lambda codes, date: {}
        api.load_stock_tags_batch = lambda *a, **k: {"tags": {}}
        api._concept_names = lambda: {"601398": "工商银行"}
        # 本测试只钉「组内按涨停时间排序」；把阶段全打成无数据，避免真实日线缓存里
        # 某只票的历史阶段把顺序搅乱（阶段分组排序由 test_limitup_phase.py 单独钉）。
        api._limitup_phase_rows = lambda codes, names, date: {}
        rows = api._pool_rows(api._abn_read(), "2026-11-02", "")["rows"]
        self.assertEqual([r["code"] for r in rows], ["300001", "600000", "000002", "601398"])


    def test_TDX没覆盖时走腾讯兜底(self):
        """owner 实报「补时间没变化」：本机通达信只到 10-14，11-02 的票要用腾讯分钟线补。"""
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api._tdx_first_limit_time = lambda code, day, lp: ("", None, "2026-10-14")
        self.api.abnormal_pool_add_limitup("2026-11-02")      # 先入池（此时时间补不上）
        self.api._tencent_limit_time = lambda code, day, lp: ("09:31", True, "1m")
        r = self.api.abnormal_pool_fill_limit_times("2026-11-02")
        self.assertEqual((r["filled"], r["missing"], r["from_tx"]), (1, 0, 1))
        p = self._pool()["600000"]
        self.assertEqual(p["limit_time"], "09:31")
        self.assertEqual(p["limit_time_gran"], "1m")
        self.assertTrue(p["limit_sealed"])

    def test_腾讯也没有的日期给提示(self):
        self._mk("2026-09-01", [("600000", "", 1, 2)])
        self.api._tdx_first_limit_time = lambda code, day, lp: ("", None, "")
        self.api.abnormal_pool_add_limitup("2026-09-01")
        self.api._tencent_limit_time = lambda code, day, lp: ("", None, "")
        r = self.api.abnormal_pool_fill_limit_times("2026-09-01")
        self.assertEqual((r["filled"], r["missing"]), (0, 1))
        self.assertIn("腾讯分钟线只覆盖", r["note"])

    def test_市场快照缺失时用复盘名单兜底(self):
        """owner 2026-11-03 实报：11-03 市场快照还没生成 ⇒ 不该直接失败，用已入库的复盘名单。"""
        self.api._limit_price_guess = lambda code, day: 9.99
        seen = {}

        def _fake_tdx(code, day, lp):
            seen[code] = lp
            return "09:31", True, "2026-11-03"

        self.api._tdx_first_limit_time = _fake_tdx
        gui._limitup_from_recap = lambda d, fp=None: [
            {"code": "600000", "name": "", "board": 2, "limit_price": 0.0, "data_date": d},
            {"code": "300001", "name": "", "board": 1, "limit_price": 0.0, "data_date": d},
        ]
        r = self.api.abnormal_pool_add_limitup("2026-11-03")
        self.assertTrue(r["ok"])
        self.assertEqual(r["source"], "recap")
        self.assertEqual((r["added"], r["total"]), (2, 2))
        self.assertIn("复盘名单", r["note"])
        self.assertEqual(seen.get("600000"), 9.99, "复盘来源没有涨停价 ⇒ 用推算值")


    def test_所选日期没有涨停票时自动退回最近日期(self):
        """实报根因：日期框默认是"今天"(11-03)，池里那批是 11-02 的 ⇒ 一只都匹配不上。"""
        self._mk("2026-11-02", [("600000", "", 1, 2)])
        self.api._tdx_first_limit_time = lambda code, day, lp: ("", None, "2026-10-14")
        self.api.abnormal_pool_add_limitup("2026-11-02")
        self.api._tencent_limit_time = lambda code, day, lp: ("09:31", True, "1m")
        r = self.api.abnormal_pool_fill_limit_times("2026-11-03")
        self.assertTrue(r["auto"])
        self.assertEqual(r["date"], "2026-11-02")
        self.assertEqual(r["filled"], 1)
        self.assertIn("没有涨停票", r["note"])
        self.assertEqual(self._pool()["600000"]["limit_time"], "09:31")


class TestTdxLimitTime(unittest.TestCase):
    """直接读通达信 1 分钟定长记录（不经 pandas）：首次触及涨停价 + 是否封板 + 覆盖日期。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.api = gui.Api()
        self.api._TDX_VIPDOC = self.tmp / "vipdoc"
        for sub in ("sz/lday", "sz/minline"):
            (self.api._TDX_VIPDOC / sub).mkdir(parents=True, exist_ok=True)
        (self.api._TDX_VIPDOC / "sz" / "lday" / "sz000001.day").write_bytes(b"\0" * 32)

    def tearDown(self):
        self._td.cleanup()

    def _rec(self, date_str, minute, o, h, l, c):
        y, m, d = (int(x) for x in date_str.split("-"))
        dk = (y - 2004) * 2048 + m * 100 + d
        return struct.pack("<HHfffffII", dk, minute, o, h, l, c, 0.0, 0, 0)

    def _write(self, recs):
        (self.api._TDX_VIPDOC / "sz" / "minline" / "sz000001.lc1").write_bytes(b"".join(recs))

    def test_首次触及与封板(self):
        self._write([
            self._rec("2026-11-02", 571, 10.9, 10.9, 10.8, 10.9),
            self._rec("2026-11-02", 572, 10.9, 10.96, 10.9, 10.95),
            self._rec("2026-11-02", 573, 10.95, 11.0, 10.95, 11.0),
            self._rec("2026-11-02", 574, 11.0, 11.0, 10.98, 10.99),
            self._rec("2026-11-02", 900, 10.99, 11.0, 10.99, 11.0),
        ])
        t, sealed, last = self.api._tdx_first_limit_time("000001", "2026-11-02", 11.0)
        self.assertEqual(t, "09:33")
        self.assertTrue(sealed)
        self.assertEqual(last, "2026-11-02")

    def test_收盘未封板(self):
        self._write([
            self._rec("2026-11-02", 573, 10.95, 11.0, 10.95, 11.0),
            self._rec("2026-11-02", 900, 10.9, 10.95, 10.85, 10.9),
        ])
        t, sealed, _ = self.api._tdx_first_limit_time("000001", "2026-11-02", 11.0)
        self.assertEqual(t, "09:33")
        self.assertFalse(sealed)

    def test_该日不在本地数据里_回传覆盖日期(self):
        self._write([
            self._rec("2026-10-14", 571, 10, 10, 10, 10),
            self._rec("2026-10-14", 900, 10, 10, 10, 10),
        ])
        t, sealed, last = self.api._tdx_first_limit_time("000001", "2026-11-02", 10.0)
        self.assertEqual((t, sealed, last), ("", None, "2026-10-14"))

    def test_没有分钟文件(self):
        t, sealed, last = self.api._tdx_first_limit_time("000001", "2026-11-02", 10.0)
        self.assertEqual((t, sealed, last), ("", None, ""))



class TestLimitTimeFromDf(unittest.TestCase):
    def test_分钟帧取首次触及与封板(self):
        import pandas as pd
        df = pd.DataFrame({
            "time": ["2026-11-02 09:31", "2026-11-02 09:32", "2026-11-02 09:33",
                     "2026-11-02 15:00", "2026-11-01 10:00"],
            "high": [10.9, 10.96, 11.0, 10.99, 11.0],
            "close": [10.9, 10.95, 11.0, 10.98, 11.0],
        })
        t, sealed = gui._limit_time_from_min_df(df, "2026-11-02", 11.0)
        self.assertEqual(t, "09:33")
        self.assertFalse(sealed, "收盘 10.98 < 11.00 ⇒ 未封板")

    def test_该日不在帧里返回空(self):
        import pandas as pd
        df = pd.DataFrame({"time": ["2026-11-01 09:31"], "high": [11.0], "close": [11.0]})
        self.assertEqual(gui._limit_time_from_min_df(df, "2026-11-02", 11.0), ("", None))

class TestFallbacks(unittest.TestCase):
    def test_涨跌停比例(self):
        assert gui._limitup_ratio("600000") == 0.10
        assert gui._limitup_ratio("300001") == 0.20
        assert gui._limitup_ratio("688001") == 0.20
        assert gui._limitup_ratio("830001") == 0.30
        assert gui._limitup_ratio("600000", "*ST某某") == 0.05

    def test_复盘成员解析与去重(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "sector_breadth.json"
            fp.write_text(json.dumps({"days": {"2026-11-03": {"sections": {
                "大消费": {"members": [{"code": "600000", "name": "浦发", "board": 2},
                                     {"code": "300001", "name": "特锐德", "board": 1}]},
                "其他": {"members": [{"code": "600000", "name": "浦发", "board": 3}]},
            }}}}, ensure_ascii=False), encoding="utf-8")
            lst = gui._limitup_from_recap("2026-11-03", fp=fp)
        assert [x["code"] for x in lst] == ["600000", "300001"]
        assert lst[0]["board"] == 3, "同日多段出现取更大板数"
        assert gui._limitup_from_recap("2026-12-01", fp=fp) == []

    def test_旧单字段迁移为一票多日(self):
        """旧池条目（只有 limit_date/limit_board/limit_time/…）就地视为一个事件，写回后多日可并存。"""
        e = {"name": "浦发银行", "limit_date": "2026-11-02", "limit_board": 2,
             "limit_time": "09:35", "limit_sealed": True, "limit_time_gran": "1m"}
        dates = gui._abn_limit_dates(e)
        self.assertEqual(dates, {"2026-11-02": {"board": 2, "time": "09:35",
                                                "sealed": True, "time_gran": "1m"}})
        # 镜像回旧字段（最新事件）：单事件时不变
        gui._abn_sync_limit_legacy(e)
        self.assertEqual(e["limit_board"], 2)
        # 没有 limit_date 的纯手动条目 → 空事件
        self.assertEqual(gui._abn_limit_dates({"name": "X"}), {})
        self.assertEqual(gui._abn_limit_dates(None), {})

    def test_涨停题材索引(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "sector_breadth.json"
            fp.write_text(json.dumps({"days": {"2026-11-03": {"sections": {
                "大消费": {"members": [{"code": "600000", "name": "浦发", "board": 2}]},
                "其他": {"members": [{"code": "300001", "name": "特锐德", "board": 1}]},
            }}}}, ensure_ascii=False), encoding="utf-8")
            idx = gui._limitup_theme_index(fp=fp)
            self.assertEqual(idx, {"2026-11-03": {"600000": "大消费", "300001": "其他"}})
            # 同文件 mtime 未变 ⇒ 命中缓存；文件不存在 ⇒ 空
            self.assertEqual(gui._limitup_theme_index(fp=fp)["2026-11-03"]["600000"], "大消费")
            self.assertEqual(gui._limitup_theme_index(fp=Path(td) / "no.json"), {})

    def test_按昨收推算涨停价(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "600000.json").write_text(json.dumps({"rows": [
                {"date": "2026-10-30", "close": 10.0}, {"date": "2026-11-02", "close": 11.0},
                {"date": "2026-11-03", "close": 12.0}]}), encoding="utf-8")
            old_dir = gui._DAILY_KLINE_DIR
            old_names = gui._concept_names
            gui._DAILY_KLINE_DIR = d
            gui._concept_names = lambda: {"600000": "浦发银行"}
            try:
                api = gui.Api()
                assert api._limit_price_guess("600000", "2026-11-02") == 11.0   # 10.0×1.1
                assert api._limit_price_guess("600000", "2026-11-03") == 12.1   # 11.0×1.1
                assert api._limit_price_guess("600000", "2026-10-30") == 0.0    # 没有昨收
            finally:
                gui._DAILY_KLINE_DIR = old_dir
                gui._concept_names = old_names


class TestFrontendTime(unittest.TestCase):
    def test_涨停时间显示与补算按钮(self):
        self.assertIn('id="abnTimeBtn"', HTML)
        self.assertIn("function limitTimeHtml(r)", JS)
        self.assertIn("limitTimeHtml(r)", JS)
        self.assertIn('apiCall("abnormal_pool_fill_limit_times", d)', JS)
        self.assertIn("🕘", JS)
        self.assertIn('id="abnLimitNotice"', HTML)
        self.assertIn("function _abnNotice(html)", JS)
        self.assertIn("按涨停日期分组（新→旧）", JS)
        self.assertIn("_lds[_lds.length - 1]", JS, "日期框跟随池里最近的涨停日")
        self.assertIn("function limitBoardText(board)", JS)
        self.assertIn('_CN_NUM', JS)
        self.assertIn('（5分）', JS)


class TestFrontend(unittest.TestCase):
    def test_日期框与按钮(self):
        self.assertIn('id="abnLimitDate"', HTML)
        self.assertIn('id="abnLimitBtn"', HTML)
        self.assertIn("abnormalAddLimitup()", HTML)

    def test_前端调用与徽章(self):
        self.assertIn('apiCall("abnormal_pool_add_limitup", d)', JS)
        self.assertIn("function limitBoardBadge(board, date)", JS)
        self.assertIn("limitBoardBadge(r.limit_board, r.limit_date)", JS)
        self.assertIn("首板", JS)
        # 日期框默认跟随全局选中日
        self.assertIn('getElementById("abnLimitDate")', JS)

    def test_题材分组前端(self):
        # 2026-11-15 owner：「每一日按图片里的题材分类」——日期段内再按涨停题材分组
        self.assertIn("abn-theme-head", JS)
        self.assertIn("r.limit_theme", JS)
        self.assertIn("abn-theme-count", JS)
        self.assertIn("其他", JS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
