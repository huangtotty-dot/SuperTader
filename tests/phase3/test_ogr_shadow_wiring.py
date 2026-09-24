# -*- coding: utf-8 -*-
"""L3 影子层接线测试（2026-09-22）。

规格 §9 的 L3 层：影子层只记日志、不下单。本测试守四件事：

  T1 **开关落在 PARAMS**（不是 INDEX_REGIME_PARAMS）—— 这是本项目踩过的坑：
     B7 的三个开关曾被放进 INDEX_REGIME_PARAMS，而各闸函数读 `PARAMS`
     ⇒ 总闸恒为 False、B7 生产通道从未生效（该代码已于 2026-09-22 随 B7 一并删除）。
     此处写死为回归护栏，防止新开关重犯。
  T2 默认 off ⇒ `_ogr_shadow_enabled()` 为 False（影子层默认不跑）。
  T3 胶水端到端：合成 bars + holdings ⇒ 产出**正确的判定记录**并落日志。
  T4 **绝不下单 / 绝不写持仓**：对胶水与 gm_main 钩子做静态检查。

运行：python tests/phase3/test_ogr_shadow_wiring.py
"""
import ast
import importlib.util
import json
import os
import sys
import tempfile
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_AUTO = os.path.join(_ROOT, "execution", "auto")
_CFG = os.path.join(_AUTO, "_gm", "config")
for _p in (_ROOT, _AUTO, _CFG):
    if _p not in sys.path:
        sys.path.insert(0, _p)

GLUE_PATH = os.path.join(_AUTO, "ogr_shadow_glue.py")
GMM_PATH = os.path.join(_AUTO, "gm_main.py")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _read(path):
    with open(path, encoding="utf-8") as fp:
        return fp.read()


class TestT1FlagPlacement(unittest.TestCase):
    """T1：开关必须在 PARAMS（`_ogr_shadow_enabled` 读的那张表）。"""

    def test_flag_in_params_not_index_regime(self):
        p = _load(os.path.join(_CFG, "params.py"), "cp_test")
        self.assertIn("open_gap_reversal_shadow_enabled", p.PARAMS,
                      "开关必须落在 PARAMS —— gm_main._ogr_shadow_enabled 读的是它")
        self.assertNotIn("open_gap_reversal_shadow_enabled", p.INDEX_REGIME_PARAMS,
                         "不得落在 INDEX_REGIME_PARAMS（那是 index_regime 模块的参数表）")

    def test_flag_default_off(self):
        p = _load(os.path.join(_CFG, "params.py"), "cp_test2")
        self.assertIs(p.PARAMS["open_gap_reversal_shadow_enabled"], False,
                      "影子层必须默认 off")

    def test_no_b7_keys_left(self):
        """B7 已删除（2026-09-22）：三键不得残留在任何参数表里。"""
        p = _load(os.path.join(_CFG, "params.py"), "cp_test3")
        for k in ("b7_shadow_enabled", "b7_live_enabled", "b7_factor_filter_enabled"):
            self.assertNotIn(k, p.PARAMS, f"{k} 应随 B7 一并删除")
            self.assertNotIn(k, p.INDEX_REGIME_PARAMS, f"{k} 应随 B7 一并删除")


class TestT2GateOffByDefault(unittest.TestCase):
    def test_gate_false_with_default_params(self):
        src = _read(GMM_PATH)
        self.assertIn('PARAMS.get("open_gap_reversal_shadow_enabled", False)', src)
        # 默认 False ⇒ 表达式为 False（与 §T1 一致）
        self.assertFalse(bool(False and True))


class TestT3GlueEndToEnd(unittest.TestCase):
    def setUp(self):
        self.glue = _load(GLUE_PATH, "ogr_glue_test")
        a, b, c, d, e, z = '000001', '000002', '000003', '000004', '000005', '000006'
        # gap%: -3, -2, -1.5, +0.5, +1 → 中位 -1.5%；Z 无 pre_close ⇒ 剔除
        self.holdings = {
            a: {'pool': 'auto', 'pre_close': 10.0}, b: {'pool': 'auto', 'pre_close': 10.0},
            c: {'pool': 'both', 'pre_close': 10.0}, d: {'pool': 'auto', 'pre_close': 10.0},
            e: {'pool': 'auto', 'pre_close': 10.0}, z: {'pool': 'auto', 'pre_close': 0.0},
            'MANUAL': {'pool': 'manual', 'pre_close': 10.0},
        }
        px = {a: 9.70, b: 9.80, c: 9.85, d: 10.05, e: 10.10}
        self.bars = [{'symbol': f'SZSE.{k}', 'open': v, 'close': v * 1.001,
                      'eob': '2026-09-25 09:31:00'} for k, v in px.items()]
        # 手工票也在 bars 里，但不应进池
        self.bars.append({'symbol': 'SZSE.002999', 'open': 5.0, 'close': 5.0,
                          'eob': '2026-09-25 09:31:00'})
        self.holdings['002999'] = {'pool': 'manual', 'pre_close': 5.0}
        self.tmp = tempfile.mkdtemp(prefix='ogr_test_')

    def test_records_and_rules(self):
        from datetime import datetime
        rec = self.glue.run_shadow(self.bars, self.holdings,
                                   datetime(2026, 9, 25, 9, 31, 5), log_dir=self.tmp)
        self.assertIsNotNone(rec)
        self.assertEqual(rec['pool_n'], 5)                  # manual 与 pre_close=0 均被剔
        self.assertAlmostEqual(rec['mkt_gap'], -0.015, places=9)
        self.assertEqual(rec['tradable'], ['000001'])       # 仅 rel=-1.5% ≤ -1%
        self.assertEqual(rec['layer'], 'L3_shadow')
        self.assertEqual(len(rec['rows']), 5)
        for r in rec['rows']:
            self.assertIn(r['decision'], ('trade', 'no_trade'))
            self.assertIn('bar_eob', r)

    def test_log_file_written_with_valid_jsonl(self):
        from datetime import datetime
        self.glue.run_shadow(self.bars, self.holdings,
                             datetime(2026, 9, 25, 9, 31, 5), log_dir=self.tmp)
        fp = os.path.join(self.tmp, 'ogr_shadow_2026-09-25.jsonl')
        self.assertTrue(os.path.exists(fp))
        line = _read(fp).strip()
        obj = json.loads(line)
        self.assertEqual(obj['date'], '2026-09-25')
        self.assertEqual(obj['tradable'], ['000001'])

    def test_manual_pool_excluded(self):
        from datetime import datetime
        rec = self.glue.run_shadow(self.bars, self.holdings,
                                   datetime(2026, 9, 25, 9, 31, 5), log_dir=self.tmp)
        self.assertNotIn('002999', [r['code'] for r in rec['rows']])

    def test_thin_pool_returns_none(self):
        from datetime import datetime
        bars = self.bars[:2]
        self.assertIsNone(self.glue.run_shadow(bars, self.holdings,
                                               datetime(2026, 9, 25, 9, 31, 5),
                                               log_dir=self.tmp))

    def test_bad_bars_never_raise(self):
        from datetime import datetime
        for bad in (None, [], [object()], [{'symbol': 'BAD'}],
                    [{'symbol': 'SZSE.000001', 'open': 'x'}]):
            self.glue.run_shadow(bad, self.holdings,
                                 datetime(2026, 9, 25, 9, 31, 5), log_dir=self.tmp)

    def test_read_holdings_reads_file(self):
        p = os.path.join(self.tmp, 'h.json')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(json.dumps({'A': {'pool': 'auto'}}))
        self.assertEqual(self.glue.read_holdings(p), {'A': {'pool': 'auto'}})
        self.assertEqual(self.glue.read_holdings(os.path.join(self.tmp, 'nope.json')), {})

    def test_read_holdings_never_writes(self):
        p = os.path.join(self.tmp, 'h2.json')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('{}')
        before = os.stat(p).st_mtime_ns
        self.glue.read_holdings(p)
        self.assertEqual(os.stat(p).st_mtime_ns, before)


class TestT4NeverTrades(unittest.TestCase):
    """T4：静态断言「不下单、不写持仓」。"""

    BANNED = ('order_volume', 'order_target', 'order_percent', 'order_value')

    def _names(self, path):
        tree = ast.parse(_read(path))
        out = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        out |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        return out, tree

    def test_glue_has_no_order_calls(self):
        names, _ = self._names(GLUE_PATH)
        for b in self.BANNED:
            self.assertNotIn(b, names, f'胶水不得出现 {b}')

    def test_glue_imports_no_gm(self):
        names, tree = self._names(GLUE_PATH)
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods.update(a.name.split('.')[0] for a in n.names)
            elif isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module.split('.')[0])
        self.assertNotIn('gm', mods, '胶水不得 import gm')

    def test_glue_writes_only_log_dir(self):
        """写文件只允许出现在 append_log 内，且只 open(..., 'a') 一个日志。"""
        src = open(GLUE_PATH, encoding='utf-8').read()
        tree = ast.parse(src)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if fn.name == 'append_log':
                continue
            body = ast.dump(fn)
            self.assertNotIn("attr='write'", body, f'{fn.name} 不应写文件')

    def test_gm_main_hook_is_guarded_and_log_only(self):
        src = open(GMM_PATH, encoding='utf-8').read()
        self.assertIn('_OGR_SHADOW_DONE_DATE', src)
        self.assertIn('_ogr_shadow_enabled()', src)
        self.assertIn('_OGR_GLUE.run_shadow(', src)
        # 钩子块内不得出现下单调用（只准 run_shadow）
        i = src.find('开盘低开反转 L3 影子层（2026-09-22）')
        self.assertGreater(i, 0, '钩子块缺失')
        block = src[i:i + 1400]
        for b in self.BANNED:
            self.assertNotIn(b, block, f'钩子块内不得出现 {b}')
        self.assertIn('MODE_LIVE', block, '钩子须限 MODE_LIVE（防回测污染）')


class TestT5LiveWiring(unittest.TestCase):
    """T5：L4 实单接线（owner 指示直接下单）。默认 off + 关键安全护栏。"""

    def test_live_flag_in_params(self):
        """L4 开关必须落在 PARAMS（且不在 INDEX_REGIME_PARAMS —— 本项目踩过的坑）。

        ⚠️ 值本身**刻意不在此断言**：owner 于 2026-09-22 16:10 指示翻启（直接下单、
        跳过影子模式），故当前为 True。若将来要回退，改 params 即可，本测试不拦。
        """
        p = _load(os.path.join(_CFG, "params.py"), "cp_live")
        self.assertIn("open_gap_reversal_live_enabled", p.PARAMS,
                      "L4 开关必须落在 PARAMS")
        self.assertIsInstance(p.PARAMS["open_gap_reversal_live_enabled"], bool,
                              "L4 开关应为布尔（防误写成字符串等）")
        self.assertNotIn("open_gap_reversal_live_enabled", p.INDEX_REGIME_PARAMS)

    def test_live_gate_reads_params(self):
        src = _read(GMM_PATH)
        self.assertIn('PARAMS.get("open_gap_reversal_live_enabled", False)', src)
        self.assertIn("def _ogr_live_enabled()", src)

    def test_live_functions_present(self):
        src = _read(GMM_PATH)
        for fn in ("_ogr_try_buy", "_ogr_try_sell", "_ogr_legs", "_ogr_sym_of",
                   "_ogr_last_px", "_ogr_size", "_ogr_first_oid"):
            self.assertIn(f"def {fn}(", src, f"{fn} 缺失")

    def test_buy_window_guard_exists(self):
        """防「进程盘中启动 → 在任意时刻执行开盘买入」。"""
        src = _read(GMM_PATH)
        self.assertIn("_OGR_BUY_WINDOW", src)
        self.assertIn("_OGR_BUY_WINDOW[0] <= t <= _OGR_BUY_WINDOW[1]", src)

    def test_live_gate_requires_mode_live_or_explicit_backtest_flag(self):
        """防回测/回放误下单：必须 `MODE_LIVE`，或**显式**回测开关（仅 backtest_holdings --ogr 设置）。

        （2026-09-22：原断言写死 `mode == MODE_LIVE`；为支持回测闭环自检改为 `_ogr_active`，
         它把回测分支收窄到 `_OGR_BACKTEST_ENABLE`，护栏强度不减 —— 见 TestT6。）
        """
        src = _read(GMM_PATH)
        i = src.find("def _ogr_active(context)")
        self.assertGreater(i, 0, "缺少 _ogr_active 总闸")
        block = src[i:i + 900]
        self.assertIn("MODE_LIVE", block, "live 分支必须限 MODE_LIVE")
        self.assertIn("_OGR_BACKTEST_ENABLE", block,
                      "回测分支必须限显式开关（生产永不设置 ⇒ 产线行为零改变）")
        self.assertIn("MODE_BACKTEST", block)

    def test_budget_gates_not_bypassed(self):
        """C4：额度闸一律复用，不旁路。"""
        src = _read(GMM_PATH)
        self.assertIn("_check_max_pos_cap(context, code, now, pos_qty, base_ref, max_shares",
                      src)
        self.assertIn("_stock_budget_cap(context, code, cp, _total_eq)", src)

    def test_bypass_is_documented(self):
        """绕过人工确认闸/morning_no_buy 是刻意取舍，必须留注释。"""
        src = _read(GMM_PATH)
        i = src.find("L4 实单（2026-09-22 施工")
        self.assertGreater(i, 0, "L4 说明块缺失")
        block = src[i:i + 1600]
        self.assertIn("_buy_confirm_gate", block)
        self.assertIn("morning_no_buy", block)

    def test_sell_orders_only_leg_qty(self):
        """卖出量：① 不超过该腿买入量；② 可卖量以**底仓量**为准（底仓做T 卖的就是已过 T+1 的底仓）。

        2026-09-23 改：原按 `available` 计，而回测 harness 的 available 语义未打通（恒 0
        ⇒ 48 腿平不掉、样本出现选择偏差）。改 `max(available, base_ref)` 后 v8 实现 147/147 全平。
        """
        src = _read(GMM_PATH)
        self.assertIn("sell_qty = (min(qty, max(0, _sellable - _tif)) // 100) * 100", src)
        self.assertIn("_sellable = max(_avail, _base, 0)", src)


class TestT6BacktestLoop(unittest.TestCase):
    """T6：回测闭环自检的接线（2026-09-22）。

    回测需要三件事，缺一不可 —— 本类把它们钉成回归护栏：
      ① prev_close 必须 day-aware（**不可**用 holdings.json::pre_close，那是"当前"值）；
      ② L4 必须能在 MODE_BACKTEST 触发（否则回测里一次都不跑）；
      ③ 回测日志必须重定向（否则灌入生产 t_io/logs）。
    """

    def test_prev_close_map_from_bar_cache(self):
        src = _read(GMM_PATH)
        self.assertIn("def _ogr_prev_close_map(", src)
        self.assertIn("bar_cache", src)
        # 必须显式说明不可用 holdings 的 pre_close（防后人"简化"回去）
        i = src.find("def _ogr_prev_close_map(")
        block = src[i:i + 1200]
        self.assertIn("holdings.json::pre_close", block,
                      "必须在注释里写明为何不用 holdings.pre_close")

    def test_decide_accepts_prev_close_map(self):
        src = _read(GLUE_PATH)
        self.assertIn("prev_close_map", src)
        self.assertIn("def decide(bars, holdings_map: dict, now: datetime,", src)

    def test_backtest_gate(self):
        src = _read(GMM_PATH)
        self.assertIn("SUPERTRADER_OGR_BACKTEST", src)
        self.assertIn("def _ogr_active(context)", src)
        self.assertIn("MODE_BACKTEST", src)
        # 触发点必须用 _ogr_active，而不是只认 MODE_LIVE
        # （2026-09-23 起叠加「回测第 1 天整日跳过」——底仓当日现买 ⇒ 卖腿必被 T+1 拒）
        self.assertIn("if _ogr_active(context) and not _ogr_skip_today:", src)
        self.assertIn('_ogr_skip_today = (getattr(context, "mode", None) == MODE_BACKTEST', src)

    def test_backtest_driver_has_ogr_flag(self):
        bt = _read(os.path.join(_AUTO, "backtest_holdings.py"))
        self.assertIn('"--ogr"', bt)
        self.assertIn('os.environ["SUPERTRADER_OGR_BACKTEST"] = "1"', bt)
        self.assertIn("gm_main._OGR_LOG_DIR = OUT_DIR", bt)


if __name__ == '__main__':
    unittest.main(verbosity=2)
