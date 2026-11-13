# -*- coding: utf-8 -*-
"""板块轮动数据层纯函数单测 —— sector_rotation/data_fetch.py（2026-11-13 补）。

只测无网络的腾讯工具函数：① `_qt_symbol` 代码→市场前缀映射（5/6/9→sh，余→sz）；
② `_is_a_share_code` 全市场代码白名单（6 位纯数字 + 前缀白名单，北交所 9/8 排除）；
③ `_parse_qt_line` 腾讯 `v_shXXXX="..."` 快照行解析（~分隔 40 字段、成交额 /分割回退、
   涨跌幅字段 32 回退到 (现价−昨收)/昨收）；④ `_snapshot_map_to_rows` dict→DataFrame；
⑤ `_cache_date_key` 日期去横杠。

运行：python tests/phase3/test_data_fetch.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from sector_rotation.data_fetch import (  # noqa: E402
    _cache_date_key, _is_a_share_code, _parse_qt_line, _qt_symbol,
    _snapshot_map_to_rows, is_trading_day,
)


class TestQtSymbol(unittest.TestCase):
    def test_shanghai_prefixes(self):
        self.assertEqual(_qt_symbol("600000"), "sh600000")
        self.assertEqual(_qt_symbol("500001"), "sh500001")  # 基金 5 开头
        self.assertEqual(_qt_symbol("900001"), "sh900001")  # B 股 9 开头

    def test_shenzhen_prefixes(self):
        self.assertEqual(_qt_symbol("000001"), "sz000001")
        self.assertEqual(_qt_symbol("300750"), "sz300750")

    def test_star_market_is_shanghai(self):
        """688/689 科创板在上海，前缀须为 sh（不是按 00/30 归 sz）。"""
        self.assertEqual(_qt_symbol("688981"), "sh688981")

    def test_strip_whitespace(self):
        self.assertEqual(_qt_symbol(" 600000 "), "sh600000")


class TestIsAShareCode(unittest.TestCase):
    def test_valid_prefixes(self):
        for c in ("000001", "001979", "002451", "003816", "300750", "301999",
                  "600000", "601318", "603288", "605399", "688981", "689009"):
            self.assertTrue(_is_a_share_code(c), c)

    def test_bse_and_other_prefixes_excluded(self):
        """北交所 8/9 开头不在白名单（与 [[breakout_scan_and_bj_data]] 北交所排除一致）。"""
        for c in ("830799", "920000", "430047"):
            self.assertFalse(_is_a_share_code(c), c)

    def test_malformed(self):
        for c in ("60000", "600000_A", "abcdef", "", "123456"):
            self.assertFalse(_is_a_share_code(c), c)


def _line(overrides=None):
    """构造 40 字段的腾讯快照行 v_sh600000="..."。overrides: {字段索引: 值}。"""
    overrides = overrides or {}
    f = [""] * 40
    f[0] = "1"
    f[1] = "贵州茅台"
    f[2] = "600000"
    f[3] = "1700.00"      # 现价
    f[4] = "1690.00"      # 昨收
    for i, v in overrides.items():
        f[i] = v
    return 'v_sh600000="' + "~".join(f) + '";'


class TestParseQtLine(unittest.TestCase):
    def test_full_parse_with_slash_amount(self):
        code, d = _parse_qt_line(_line({6: "100/200/5000000", 32: "1.50"}))
        self.assertEqual(code, "600000")
        self.assertEqual(d["名称"], "贵州茅台")
        self.assertEqual(d["现价"], 1700.0)
        self.assertEqual(d["涨跌幅"], 1.5)
        self.assertEqual(d["成交额"], 5000000.0)

    def test_change_pct_fallback_from_prev_close(self):
        """字段 32 为 0 时，用 (现价−昨收)/昨收 反推涨跌幅。"""
        code, d = _parse_qt_line(_line({6: "100/200/5000000", 32: "0"}))
        self.assertAlmostEqual(d["涨跌幅"], round((1700 - 1690) / 1690 * 100, 4))

    def test_amount_fallback_to_field7(self):
        """无 / 三段的字段时，成交额回退到字段 7。"""
        code, d = _parse_qt_line(_line({7: "8888.88", 32: "1.50"}))
        self.assertEqual(d["成交额"], 8888.88)

    def test_malformed_lines_return_none(self):
        self.assertIsNone(_parse_qt_line(""))
        self.assertIsNone(_parse_qt_line("no equals quote"))
        self.assertIsNone(_parse_qt_line('v_sh600000="1~a~b~c";'))  # 字段不足 40

    def test_non_digit_code_returns_none(self):
        f = [""] * 40
        f[2] = "abc"  # 非数字代码
        self.assertIsNone(_parse_qt_line('v_sh600000="' + "~".join(f) + '";'))


class TestSnapshotMapToRows(unittest.TestCase):
    def test_dict_to_dataframe(self):
        df = _snapshot_map_to_rows({
            "600000": {"名称": "贵州茅台", "现价": 1700.0, "涨跌幅": 1.5, "成交额": 1.0e8},
            "000001": {"名称": "平安银行", "现价": 10.0, "涨跌幅": -0.5, "成交额": 5.0e7},
        })
        self.assertEqual(list(df.columns), ["代码", "名称", "收盘", "涨跌幅", "成交额"])
        self.assertEqual(len(df), 2)
        self.assertEqual(df.iloc[0]["代码"], "600000")
        self.assertEqual(df.iloc[0]["收盘"], 1700.0)

    def test_empty_map_returns_empty_df(self):
        df = _snapshot_map_to_rows({})
        self.assertEqual(len(df), 0)


class TestCacheDateKey(unittest.TestCase):
    def test_dash_removed(self):
        self.assertEqual(_cache_date_key("2026-11-13"), "20261113")


class TestIsTradingDay(unittest.TestCase):
    def test_membership_in_cached(self):
        cached = ["2026-11-11", "2026-11-12", "2026-11-13"]
        self.assertTrue(is_trading_day("2026-11-13", cached))
        self.assertFalse(is_trading_day("2026-11-14", cached))


if __name__ == "__main__":
    unittest.main(verbosity=2)
