# -*- coding: utf-8 -*-
"""`stock_hunter/sync_watchlist.py` 纯函数单测（2026-09-29）。

只测**离线纯函数**（口径判定与合并），不打网络、不读写 `watchlist_jiuyan.json`——
该文件是运行期数据（且被 gitignore），测试依赖它就会随数据漂移。

守三件事：
  1. 北交所**纳入**（owner 2026-09-29 追加需求）
  2. ST/*ST/退 **排除**（涨跌幅 5%，与突破判定 0.3~8% 口径不可比）
  3. `merge_new` **幂等且不覆盖**——重跑脚本不能把已有条目的既有字段抹掉

运行：python tests/phase3/test_sync_watchlist.py
"""
import importlib.util
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_PATH = os.path.join(_ROOT, "stock_hunter", "sync_watchlist.py")


def _load():
    spec = importlib.util.spec_from_file_location("sync_watchlist_test", _PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestClassify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_北交所判定(self):
        for c in ("430047", "873169", "920000", "830799", "400001"):
            self.assertTrue(self.m.is_bj(c), f"{c} 应判北交所")
        for c in ("600519", "000001", "300750", "688981", "900901"):
            self.assertFalse(self.m.is_bj(c), f"{c} 不是北交所（900901 是沪B）")

    def test_ST与退市被排除(self):
        for n in ("ST海航", "*ST基础", "退市海润", "st康美", "*st 新亿"):
            self.assertTrue(self.m.is_excluded_name(n), f"{n} 应被排除")

    def test_正常名不排除(self):
        for n in ("贵州茅台", "平安银行", "N鸿富诚", "C中塑股份", "宁德时代"):
            self.assertFalse(self.m.is_excluded_name(n), f"{n} 不应被排除")


class TestMissing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_缺失为差集(self):
        uni = {"600000": "浦发银行", "000001": "平安银行", "300750": "宁德时代"}
        self.assertEqual(self.m.missing_codes(uni, {"600000": {}}), ["000001", "300750"])

    def test_北交所纳入(self):
        uni = {"920000": "安徽凤凰", "430047": "诺思兰德"}
        self.assertEqual(self.m.missing_codes(uni, {}), ["430047", "920000"])

    def test_ST排除(self):
        uni = {"600001": "ST某股", "600002": "正常股"}
        self.assertEqual(self.m.missing_codes(uni, {}), ["600002"])

    def test_北交所ST不被排除(self):
        """北交所优先级高于 ST 排除：ST 规则是为 5% 涨跌幅设的，不适用于北交所板块判定。

        注：本断言锁的是**代码里的判定顺序**（先 is_bj 再 ST）。若将来口径改为「北交所 ST
        也排除」，需显式改这里，而不是让它静默变化。
        """
        uni = {"920003": "ST某北交所股"}
        self.assertEqual(self.m.missing_codes(uni, {}), ["920003"])

    def test_已有条目不再出现(self):
        uni = {"600000": "浦发银行"}
        self.assertEqual(self.m.missing_codes(uni, {"600000": {"name": "浦发银行"}}), [])

    def test_代码补零到6位(self):
        uni = {"1": "某股"}
        self.assertEqual(self.m.missing_codes(uni, {}), ["000001"])


class TestMerge(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = _load()

    def test_新增条数正确且字段齐备(self):
        store, uni, sec = {}, {"920000": "安徽凤凰"}, {"920000": "汽车/安徽板块"}
        n = self.m.merge_new(store, uni, ["920000"], sec, "2026-09-29 10:00:00")
        self.assertEqual(n, 1)
        e = store["920000"]
        self.assertEqual(e["name"], "安徽凤凰")
        self.assertEqual(e["sector"], "汽车/安徽板块")
        self.assertEqual(e["primary_source"], self.m.SOURCE_TAG)
        for k in ("business_summary", "concept_boards", "industry_boards",
                  "jiuyan_category", "jiuyan_concept", "sector_type", "updated_at"):
            self.assertIn(k, e, f"新条目缺字段 {k}")

    def test_不覆盖已有条目(self):
        """幂等的核心：重跑脚本不能抹掉已有条目的既有字段。"""
        store = {"920000": {"name": "旧名", "business_summary": "旧简介", "sector": "旧板块"}}
        self.m.merge_new(store, {"920000": "新名"}, ["920000"], {"920000": "新板块"},
                         "2026-09-29 10:00:00")
        self.assertEqual(store["920000"]["name"], "旧名")
        self.assertEqual(store["920000"]["business_summary"], "旧简介")
        self.assertEqual(store["920000"]["sector"], "旧板块")

    def test_重复调用幂等(self):
        store, uni = {}, {"920000": "安徽凤凰", "920002": "万达轴承"}
        first = self.m.merge_new(store, uni, ["920000", "920002"], {}, "T")
        second = self.m.merge_new(store, uni, ["920000", "920002"], {}, "T")
        self.assertEqual((first, second), (2, 0), "第二次调用不应再新增")

    def test_缺板块时留空不报错(self):
        store = {}
        self.m.merge_new(store, {"920000": "安徽凤凰"}, ["920000"], {}, "T")
        self.assertEqual(store["920000"]["sector"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
