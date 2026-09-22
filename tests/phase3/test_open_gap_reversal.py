# -*- coding: utf-8 -*-
"""`core/open_gap_reversal.py` 离线单测（2026-09-22，规格 §9 的 L1 层）。

覆盖：池内中位口径 / rel 边界（含「恰好 = −1.0% 出手」）/ mkt_gap 符号边界
      （含「恰好 = 0 不出手」）/ 池太薄 fail-closed / 无效价与新股首日的剔除 /
      codes 白名单 / 单腿容量公式 / **纯函数性（无 IO、不写文件）**。

铁律：本模块按规格 §5.2 **不做任何 IO**，故本测试不需要重定向任何目录；
      测试自身也不碰 t_io。

运行：python tests/phase3/test_open_gap_reversal.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import open_gap_reversal as ogr  # noqa: E402


def pool(**gaps_pct):
    """按「目标 gap%」构造 (prev_close, open)：prev 固定 10.0，open = 10*(1+gap)。"""
    pc = {c: 10.0 for c in gaps_pct}
    op = {c: 10.0 * (1 + g / 100.0) for c, g in gaps_pct.items()}
    return pc, op


class TestComputeGaps(unittest.TestCase):
    def test_basic_gap(self):
        pc, op = {'A': 10.0, 'B': 20.0}, {'A': 10.5, 'B': 19.0}
        g = ogr.compute_gaps(pc, op)
        self.assertAlmostEqual(g['A'], 0.05)
        self.assertAlmostEqual(g['B'], -0.05)

    def test_invalid_prices_excluded(self):
        pc = {'A': 10.0, 'B': 10.0, 'C': 10.0, 'D': 0.0, 'E': 10.0}
        op = {'A': 10.0, 'B': float('nan'), 'C': 10.0, 'D': 10.0, 'E': -1.0}
        g = ogr.compute_gaps(pc, op)
        # B: open=NaN；D: prev_close=0；E: open<0 ⇒ 三者剔除；A/C 保留
        self.assertEqual(set(g), {'A', 'C'})

    def test_missing_prev_close_new_listing(self):
        """新股首日 / 停牌复牌无前收 ⇒ 不进结果、也不进中位数。"""
        g = ogr.compute_gaps({'A': 10.0}, {'A': 10.0, 'NEW': 30.0})
        self.assertEqual(set(g), {'A'})

    def test_zero_prev_close_excluded(self):
        g = ogr.compute_gaps({'A': 0.0}, {'A': 10.0})
        self.assertEqual(g, {})


class TestMarketGap(unittest.TestCase):
    def test_median_odd(self):
        self.assertAlmostEqual(ogr.market_gap({'a': -0.02, 'b': -0.01, 'c': 0.0,
                                               'd': 0.01, 'e': 0.02}), 0.0)

    def test_median_even_is_mean_of_middles(self):
        # 6 只（≥MIN_POOL）偶数样本 ⇒ 两个中间值的平均
        self.assertAlmostEqual(ogr.market_gap({'a': -0.03, 'b': -0.02, 'c': -0.01,
                                               'd': 0.01, 'e': 0.02, 'f': 0.03}), 0.0)

    def test_too_thin_returns_none(self):
        """MIN_POOL=5，4 只 ⇒ None（fail-closed）。"""
        self.assertIsNone(ogr.market_gap({str(i): 0.01 for i in range(ogr.MIN_POOL - 1)}))
        self.assertIsNotNone(ogr.market_gap({str(i): 0.01 for i in range(ogr.MIN_POOL)}))


class TestDecideOne(unittest.TestCase):
    def test_trade_when_gap_down_more_than_market(self):
        ok, why = ogr.decide_one(-0.03, -0.01)
        self.assertTrue(ok, why)
        self.assertIn('rel=-2.0000%', why)

    def test_rel_boundary_inclusive(self):
        """rel 恰好 = −1.0% ⇒ **出手**（规则写的是 ≤）。"""
        ok, _ = ogr.decide_one(-0.02, -0.01)
        self.assertTrue(ok)

    def test_rel_just_above_threshold_no_trade(self):
        ok, why = ogr.decide_one(-0.0199, -0.01)
        self.assertFalse(ok, why)

    def test_market_gap_exactly_zero_no_trade(self):
        """mkt_gap 必须 **严格 < 0**；恰好 0 ⇒ 不出手。"""
        ok, why = ogr.decide_one(-0.05, 0.0)
        self.assertFalse(ok)
        self.assertIn('mkt_gap>=0', why)

    def test_market_gap_positive_no_trade(self):
        ok, why = ogr.decide_one(-0.05, 0.01)
        self.assertFalse(ok)

    def test_none_market_gap_no_trade(self):
        ok, why = ogr.decide_one(-0.05, None)
        self.assertFalse(ok)

    def test_nan_gap_no_trade(self):
        ok, why = ogr.decide_one(float('nan'), -0.01)
        self.assertFalse(ok)


class TestEvaluate(unittest.TestCase):
    def test_end_to_end_trade_set(self):
        pc, op = pool(A=-3.0, B=-2.0, C=-1.5, D=0.5, E=1.0)
        # gaps: -3,-2,-1.5,+0.5,+1 → 中位 = -1.5% ; rel: -1.5,-0.5,0,+2,+2.5 (%)
        r = ogr.evaluate(pc, op)
        self.assertAlmostEqual(r['mkt_gap'], -0.015, places=9)
        self.assertEqual(r['pool_n'], 5)
        # rel ≤ -1% 仅 A（-1.5%）；B 的 rel = -0.5% 不够
        self.assertEqual(r['tradable'], ['A'])

    def test_market_up_all_no_trade(self):
        pc, op = pool(A=1.0, B=2.0, C=3.0, D=4.0, E=5.0)
        r = ogr.evaluate(pc, op)
        self.assertGreater(r['mkt_gap'], 0)
        self.assertEqual(r['tradable'], [])
        self.assertTrue(all(d['decision'] == ogr.NO_TRADE for d in r['decisions']))

    def test_thin_pool_fail_closed(self):
        pc, op = pool(A=-5.0, B=-4.0)
        r = ogr.evaluate(pc, op)
        self.assertIsNone(r['mkt_gap'])
        self.assertEqual(r['tradable'], [])
        self.assertTrue(all(d['reason'] == 'pool_too_thin_or_no_median'
                            for d in r['decisions']))

    def test_codes_whitelist_restricts_output(self):
        pc, op = pool(A=-3.0, B=-2.0, C=-1.5, D=0.5, E=1.0)
        r = ogr.evaluate(pc, op, codes=['D', 'E'])
        self.assertEqual([d['code'] for d in r['decisions']], ['D', 'E'])
        self.assertEqual(r['tradable'], [])       # 名单里没有合格的
        self.assertAlmostEqual(r['mkt_gap'], -0.015, places=9)   # 中位仍用全池

    def test_invalid_names_do_not_dilute_median(self):
        """无效价不进中位数（这是规格与离线检验一致性的关键）。"""
        pc = {'A': 10.0, 'B': 10.0, 'C': 10.0, 'D': 10.0, 'E': 10.0, 'Z': 0.0}
        op = {'A': 9.7, 'B': 9.8, 'C': 9.85, 'D': 10.05, 'E': 10.1, 'Z': 5.0}
        r = ogr.evaluate(pc, op)
        self.assertEqual(r['pool_n'], 5)          # Z 被剔，不参与中位
        self.assertAlmostEqual(r['mkt_gap'], -0.015, places=9)

    def test_decisions_cover_full_watch_list(self):
        pc, op = pool(A=-3.0, B=-2.0, C=-1.5, D=0.5, E=1.0)
        r = ogr.evaluate(pc, op)
        self.assertEqual(len(r['decisions']), 5)
        for d in r['decisions']:
            self.assertIn('code', d)
            self.assertIn(d['decision'], (ogr.TRADE, ogr.NO_TRADE))
            self.assertTrue(d['reason'])


class TestSizeCap(unittest.TestCase):
    def test_participation_math(self):
        self.assertAlmostEqual(ogr.size_cap_by_auction(10_000_000, 0.10), 1_000_000)

    def test_invalid_amount_zero(self):
        self.assertEqual(ogr.size_cap_by_auction(0), 0.0)
        self.assertEqual(ogr.size_cap_by_auction(float('nan')), 0.0)


class TestPurity(unittest.TestCase):
    """规格 §5.2：本模块不得有任何 IO —— 静态检查 import 与危险调用。"""

    def test_no_io_imports(self):
        import ast
        src = open(os.path.join(_ROOT, 'core', 'open_gap_reversal.py'),
                   encoding='utf-8').read()
        tree = ast.parse(src)
        banned = {'os', 'io', 'pathlib', 'json', 'shutil', 'subprocess', 'requests'}
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split('.')[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split('.')[0])
        self.assertEqual(mods & banned, set(), f'禁止 import: {mods & banned}')

    def test_no_open_or_write_calls(self):
        import ast
        src = open(os.path.join(_ROOT, 'core', 'open_gap_reversal.py'),
                   encoding='utf-8').read()
        names = {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
        for bad in ('open', 'write', 'dump', 'save', 'set_token', 'order_volume'):
            self.assertNotIn(bad, names, f'禁止调用 {bad}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
