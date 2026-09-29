# -*- coding: utf-8 -*-
"""猎手「盘中实时」+ 突破箱体「按行业分组」离线单测（2026-09-29）。

覆盖四块：

  T1 **行业粗分类**（`t_gui._stock_industry`）：核心回归闸是**概念段不得劫持行业匹配**——
     实测踩过：`汽车零部件/参股保险/…` 被 '保险' 抢成非银金融、`软件开发/互联网保险` 同理。
     另守顺序陷阱：`非金属矿物制品业`/`黑色金属冶炼` 都含 '金属'、`水上运输业` 含 '水'、
     `酒店` 含 '酒' —— 特定规则必须先命中。
  T2 **盘中实时接线**：`_hunter_build_conformance` 不再盘中跳过；盘中手动跑不推飞书
     （定时自动档仍推）。两者都用**静态断言**守，避免依赖运行时刻。
  T3 **`merge_daily_cache`**：回写当日实时日线时**不得截短既有长历史**——这正是
     `t_io/cache/daily_kline/index_*.json` 踩过的静默截短坑（见 memory）。
  T4 **北交所进猎手拉取**：`_is_a_share_code` 收 4/8/920；符号规则走 codec（bj 前缀）。

铁律：**全离线**。T3 把缓存目录指到临时目录，绝不碰真实 `t_io/`。

运行：python tests/phase3/test_hunter_intraday_industry.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestIndustryClassifier(unittest.TestCase):
    """T1：行业粗分类。"""

    @classmethod
    def setUpClass(cls):
        import t_gui
        # 注意：不要把模块级函数直接赋成类属性——那会让它变成绑定方法，调用时多出一个 self。
        cls.tg = t_gui
        cls.UNK = t_gui._INDUSTRY_UNKNOWN

    def g(self, info):
        return self.tg._stock_industry(info)

    def test_常见行业名映射(self):
        cases = {
            "化学原料和化学制品制造业": "基础化工", "医药制造业": "医药生物",
            "专用设备制造业": "机械设备", "软件和信息技术服务业": "计算机",
            "电气机械和器材制造业": "电力设备", "半导体": "电子",
            "汽车制造业": "汽车", "房地产业": "房地产", "货币金融服务": "银行",
            "资本市场服务": "非银金融", "酒、饮料和精制茶制造业": "食品饮料",
            "煤炭开采和洗选业": "煤炭", "土木工程建筑业": "建筑装饰",
        }
        for sec, want in cases.items():
            self.assertEqual(self.g({"sector": sec}), want, f"{sec} 映射错误")

    def test_概念段不得劫持行业匹配(self):
        """核心回归闸：sector 后续段是概念，曾把行业抢走。

        `汽车零部件/参股保险/飞行汽车` 的 '保险' 曾命中非银金融；
        `软件开发/ChatGPT概念/互联网保险` 同理。故实现**只看首段**。
        """
        self.assertEqual(self.g({"sector": "汽车零部件/参股保险/飞行汽车(eVTOL)/国企改革"}),
                         "汽车", "后续段的 '保险' 劫持了行业")
        self.assertEqual(self.g({"sector": "软件开发/ChatGPT概念/华为昇腾/互联网保险"}),
                         "计算机", "后续段的 '保险' 劫持了行业")
        self.assertEqual(self.g({"sector": "自动化设备/共封装光学(CPO)/F5G概念"}),
                         "机械设备", "后续段的 '光学' 劫持了行业")
        self.assertEqual(self.g({"sector": "通用设备/核电/风电/海工装备"}),
                         "机械设备", "后续段的 '风电' 劫持了行业")

    def test_顺序陷阱_特定先于宽泛(self):
        """含同一关键词但归类的行业不同，特定规则必须先命中。"""
        self.assertEqual(self.g({"sector": "非金属矿物制品业"}), "建筑材料", "被 '金属' 抢走")
        self.assertEqual(self.g({"sector": "黑色金属冶炼和压延加工业"}), "钢铁", "被 '金属' 抢走")
        self.assertEqual(self.g({"sector": "有色金属冶炼和压延加工业"}), "有色金属")
        self.assertEqual(self.g({"sector": "水上运输业"}), "交通运输", "被 '水' 抢走")
        self.assertEqual(self.g({"sector": "水的生产和供应业"}), "公用事业")
        self.assertEqual(self.g({"sector": "酒店"}), "社会服务", "被 '酒' 抢走")
        self.assertEqual(self.g({"sector": "电力设备"}), "电力设备", "被 '电力' 抢走")

    def test_东财三级名与长尾(self):
        self.assertEqual(self.g({"sector": "家用电器"}), "家用电器")
        self.assertEqual(self.g({"sector": "乘用车"}), "汽车")
        self.assertEqual(self.g({"sector": "底盘与发动机系统/汽车/汽车零部件"}),
                         "汽车", "东财三级名应为汽车")

    def test_无行业与字面其他区分(self):
        """首段是概念（我们不知道行业）与源数据字面的「其他」不是一回事。"""
        for bad in ({}, None, {"sector": ""}, {"sector": "/"}, "notadict"):
            self.assertEqual(self.g(bad), self.UNK, f"{bad!r} 应判未分类")
        self.assertEqual(self.g({"sector": "其他"}), "其他")
        # 归不到任何大类的概念首段（如专精特新/IP经济）→ 未分类，如实说不知道
        self.assertEqual(self.g({"sector": "专精特新/融资融券"}), self.UNK)
        self.assertEqual(self.g({"sector": "IP经济/文化传媒概念"}), self.UNK)

    def test_概念首段仍可关键词归类(self):
        """概念首段若命中行业词，归入该行业（比一律未分类更有用）。

        这是**有意**行为：`钠离子电池` 命 '电池' ⇒ 电力设备，对电池厂是合理的；
        只有归不进去的才落到未分类。
        """
        self.assertEqual(self.g({"sector": "钠离子电池/动力电池回收"}), "电力设备")
        self.assertEqual(self.g({"sector": "芯片概念/国产替代"}), "电子")

    def test_全池未分类率不失控(self):
        """规则表退化（如被误改顺序）会让大量票掉进未分类——设上限兜住。"""
        wl_path = os.path.join(_ROOT, "stock_hunter", "watchlist_jiuyan.json")
        if not os.path.exists(wl_path):
            self.skipTest("无 watchlist（运行期数据，可能被 gitignore）")
        with open(wl_path, encoding="utf-8") as fh:
            wl = json.load(fh)
        unk = sum(1 for v in wl.values() if self.g(v) == self.UNK)
        rate = unk / max(1, len(wl))
        self.assertLess(rate, 0.10, f"未分类率 {rate:.1%} 过高（{unk}/{len(wl)}），规则表可能被改坏")


class TestHunterIntraday(unittest.TestCase):
    """T2：盘中实时接线（静态断言，不依赖运行时刻）。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(_ROOT, "t_gui.py"), encoding="utf-8") as fh:
            cls.src = fh.read()
        import t_gui
        cls.tg = t_gui

    @staticmethod
    def _body(src, name):
        import ast
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                body = node.body
                if (body and isinstance(body[0], ast.Expr)
                        and isinstance(body[0].value, ast.Constant)
                        and isinstance(body[0].value.value, str)):
                    body = body[1:]
                return "\n".join(ast.unparse(n) for n in body)
        return ""

    def test_符合度不再盘中跳过(self):
        body = self._body(self.src, "_hunter_build_conformance")
        self.assertTrue(body, "_hunter_build_conformance 缺失")
        self.assertNotIn("915", body, "仍存在盘中(9:15)跳过逻辑 ⇒ 盘中 GO 列仍会为空")
        self.assertIn("result", body)

    def test_盘中手动只算不推(self):
        body = self._body(self.src, "run_hunter")
        self.assertIn("_hunter_is_intraday", body, "缺少盘中判定")
        self.assertIn("if auto or not _hunter_is_intraday(date)", body,
                      "盘中手动运行未被排除在推送之外（会刷屏飞书群）")

    def test_定时自动档仍推(self):
        """2026-09-21 拍板放开的正是定时自动档——不得一并关掉。"""
        body = self._body(self.src, "run_hunter")
        i = body.find("if auto or not _hunter_is_intraday(date)")
        self.assertGreaterEqual(i, 0)
        self.assertIn("_push_hunter_build_candidates", body[i:i + 200],
                      "自动档的建仓推送被误关")

    def test_盘中判定函数(self):
        f = self.tg._hunter_is_intraday
        self.assertFalse(f("1999-01-01"), "非今日必须为 False")
        self.assertFalse(f("2999-01-01"), "非今日必须为 False")
        self.assertIsInstance(f(datetime.now().strftime("%Y-%m-%d")), bool)

    def test_拉取当日时回写缓存(self):
        body = self._body(open(os.path.join(_ROOT, "stock_hunter", "modules", "market_data.py"),
                               encoding="utf-8").read(), "_fetch_historical_tencent")
        self.assertIn("merge_daily_cache", body, "未回写共享日线缓存 ⇒ GO 列仍基于陈旧收盘")
        self.assertIn("target_date_str", body, "回写未按目标日期设闸（历史日会挤掉当天数据）")


class TestMergeDailyCache(unittest.TestCase):
    """T3：回写不得截短既有长历史（离线，缓存目录指到临时目录）。"""

    def setUp(self):
        from core.market_data import tencent_provider as tp
        self.tp = tp
        self._orig = tp._DAILY_CACHE_DIR
        self.tmp = tempfile.mkdtemp(prefix="dailycache_")
        tp._DAILY_CACHE_DIR = self.tmp

    def tearDown(self):
        self.tp._DAILY_CACHE_DIR = self._orig

    def _write(self, code, dates):
        import pandas as pd
        fp = os.path.join(self.tmp, f"{code}.json")
        rows = [{"date": d, "open": 1.0, "close": 2.0, "high": 2.1, "low": 0.9, "volume": 10.0}
                for d in dates]
        with open(fp, "w", encoding="utf-8") as fh:
            json.dump({"date": "2026-01-01", "saved_at": "2026-01-01 00:00:00", "rows": rows}, fh)

    def _read(self, code):
        with open(os.path.join(self.tmp, f"{code}.json"), encoding="utf-8") as fh:
            return json.load(fh)["rows"]

    @staticmethod
    def _df(dates):
        import pandas as pd
        return pd.DataFrame([{"date": d, "open": 1.0, "close": 2.0, "high": 2.1,
                              "low": 0.9, "volume": 10.0} for d in dates])

    def test_合并新增日期且不丢历史(self):
        self._write("600519", ["2026-01-01", "2026-01-02"])
        self.tp.merge_daily_cache("600519", self._df(["2026-01-02", "2026-01-03"]))
        got = [r["date"] for r in self._read("600519")]
        self.assertEqual(got, ["2026-01-01", "2026-01-02", "2026-01-03"])

    def test_短帧不得截短长历史(self):
        """核心回归闸：150 根的帧并入 300 根的缓存后，缓存仍必须是 300 根。"""
        old = [f"2025-{m:02d}-{d:02d}" for m in range(1, 7) for d in range(1, 11)]
        self._write("600519", old)
        new = old[-5:] + ["2026-07-01"]
        self.tp.merge_daily_cache("600519", self._df(new))
        got = self._read("600519")
        self.assertGreaterEqual(len(got), len(old), "长历史被截短了（save_daily_cache 的整体覆盖坑）")
        self.assertIn("2026-07-01", [r["date"] for r in got])

    def test_同日期以新值覆盖(self):
        self._write("000001", ["2026-01-01"])
        import pandas as pd
        df = pd.DataFrame([{"date": "2026-01-01", "open": 9.0, "close": 9.9,
                            "high": 10.0, "low": 8.8, "volume": 99.0}])
        self.tp.merge_daily_cache("000001", df)
        rows = self._read("000001")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["close"], 9.9, "同日应当以新值覆盖（当日实时价）")

    def test_空输入不崩不写(self):
        for bad in (None, self._df([])):
            self.tp.merge_daily_cache("300750", bad)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "300750.json")))

    def test_上限生效(self):
        self._write("600000", [f"2026-01-{d:02d}" for d in range(1, 21)])
        self.tp.merge_daily_cache("600000", self._df(["2026-02-01"]), cap=5)
        self.assertEqual(len(self._read("600000")), 5)


class TestBjInclusion(unittest.TestCase):
    """T4：北交所进猎手拉取 + 符号规则走 codec。"""

    @classmethod
    def setUpClass(cls):
        cls.md = _load(os.path.join(_ROOT, "stock_hunter", "modules", "market_data.py"),
                       "hunter_market_data_test")
        from core.market_data import codec
        cls.codec = codec

    def test_北交所纳入(self):
        for c in ("430047", "830799", "873169", "920000", "920819"):
            self.assertTrue(self.md.MarketDataFetcher._is_a_share_code(c), f"{c} 应纳入")

    def test_既有票仍纳入(self):
        for c in ("600519", "000001", "300750", "688981"):
            self.assertTrue(self.md.MarketDataFetcher._is_a_share_code(c))

    def test_非A股不纳入(self):
        for c in ("399001", "sh000001", "12345", "0000011", "abcdef"):
            self.assertFalse(self.md.MarketDataFetcher._is_a_share_code(c), f"{c} 不应纳入")

    def test_符号规则走codec(self):
        for c, want in (("600519", "sh600519"), ("000001", "sz000001"),
                        ("300750", "sz300750"), ("430047", "bj430047"), ("920000", "bj920000")):
            self.assertEqual(self.md.MarketDataFetcher._normalize_qt_symbol(c), want)
            self.assertEqual(self.codec.to_tx(c), want)

    def test_沪B仍归沪(self):
        self.assertEqual(self.codec.to_tx("900901"), "sh900901", "900xxx 是沪B，不是北交所")


if __name__ == "__main__":
    unittest.main(verbosity=2)
