# -*- coding: utf-8 -*-
"""src/data_fetcher.py C-1 分板 helper 单测（2026-09-07）。

覆盖纯函数：_ir_resolve_board_index（个股→板指数码，与 core/board_index 单一源一致）、
_ir_board_circuit（与 _attach 市场路径 401-424 同公式的板 circuit）、
_ir_board_mode_on（预注册闸门默认 False → 零行为变化）。全离线无网络。
pytest / unittest 均可运行：python t_io/validation/data_fetcher/test_board_override_circuit.py
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# src/ 非包（main.py 走 exec 加载）→ 用绝对路径 importlib 载入 data_fetcher 模块
import importlib.util  # noqa: E402

_SPEC = importlib.util.spec_from_file_location("_df_board_test", os.path.join(_ROOT, "src", "data_fetcher.py"))
_df_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_df_mod)
df = _df_mod


class TestBoardModeGate(unittest.TestCase):
    def test_gate_default_off(self):
        # 预注册闸门默认关：_board_index_override 不生效 → 市场级零变化
        self.assertFalse(df._ir_board_mode_on())


class TestResolveBoardIndex(unittest.TestCase):
    def test_mapping(self):
        cases = {"600519": "sh000001", "688981": "sh000688", "588170": "sh000688",
                 "300750": "sz399006", "002451": "sz399001", "000988": "sz399001"}
        for code, board in cases.items():
            self.assertEqual(df._ir_resolve_board_index(code), board, code)


class TestBoardCircuit(unittest.TestCase):
    def test_normal_by_default(self):
        self.assertEqual(df._ir_board_circuit(0.0, [], 0, "normal_t", "range"), "normal")

    def test_clear_bucket_deterioration(self):
        # bucket=clear(score≤-40) 且 delta≤-10 → clear
        self.assertEqual(df._ir_board_circuit(-45.0, [5.0, -10.0], 0, "normal_t", "range"), "clear")

    def test_freeze_bucket_reduce(self):
        # bucket=freeze(-25≤s≤-15?) + delta 恶化 → reduce（与市场路径同口径）
        self.assertEqual(df._ir_board_circuit(-30.0, [0.0, -12.0], 0, "normal_t", "range"), "reduce")

    def test_uni_down_days_escalate_defensive_to_reduce(self):
        # uni_down 且持续≥2日 & delta≤0：defensive → reduce
        self.assertEqual(
            df._ir_board_circuit(-20.0, [0.0, -1.0], 3, "defensive_t", "uni_down"), "reduce")

    def test_hot_bucket_neutral_bucket_stay_normal(self):
        # 高位/中性分不触发电路（与市场路径同口径）
        self.assertEqual(df._ir_board_circuit(30.0, [], 0, "normal_t", "range"), "normal")
        self.assertEqual(df._ir_board_circuit(0.0, [], 0, "normal_t", "uni_up"), "normal")


if __name__ == "__main__":
    unittest.main(verbosity=2)
