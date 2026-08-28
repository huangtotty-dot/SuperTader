# -*- coding: utf-8 -*-
"""config/auto_pool.py 单元测试（合并实施方案 P3-2 验收：池分管/交集裁决）。pytest / unittest 均可运行。"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _load_auto_pool():
    """按绝对路径加载 config/auto_pool.py（superTrader 的 config 是目录、config.py 是模块，
    常规 `from config.auto_pool` 无法解析，与 goldminer 消费方式保持一致）。"""
    path = os.path.join(_ROOT, "config", "auto_pool.py")
    spec = importlib.util.spec_from_file_location("auto_pool", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


auto_pool = _load_auto_pool()


def _write_watchlist(path, stocks):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"stocks": stocks}, f, ensure_ascii=False)


class TestAutoPool(unittest.TestCase):
    def test_codes_count_and_content(self):
        codes = auto_pool.auto_pool_codes()
        self.assertEqual(len(codes), 17)
        self.assertIn("000988", codes)
        self.assertIn("515180", codes)
        self.assertEqual(auto_pool.POOL, "auto")

    def test_every_code_has_name_and_gm_symbol(self):
        for code, v in auto_pool.AUTO_POOL.items():
            self.assertIn("name", v)
            self.assertIn("gm_symbol", v)
            self.assertTrue(v["gm_symbol"].endswith(code))

    def test_is_manual(self):
        self.assertFalse(auto_pool.is_manual("600481"))     # auto 池
        self.assertFalse(auto_pool.is_manual("000988_A"))   # 后缀剥离后仍在 auto 池
        self.assertTrue(auto_pool.is_manual("300058"))      # manual 池
        self.assertTrue(auto_pool.is_manual("588170"))      # manual 池

    def test_validate_no_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "watchlist_buy.json")
            stocks = {c: {"pool": "auto"} for c in auto_pool.AUTO_POOL}
            stocks["300058"] = {"pool": "manual"}
            stocks["588170"] = {"pool": "manual"}
            _write_watchlist(p, stocks)
            self.assertEqual(auto_pool.validate_pool_split(p), [])

    def test_validate_conflict_detected(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "watchlist_buy.json")
            stocks = {c: {"pool": "manual"} for c in list(auto_pool.AUTO_POOL)[:2]}
            _write_watchlist(p, stocks)
            conflicts = auto_pool.validate_pool_split(p)
            self.assertEqual(conflicts, sorted(list(auto_pool.AUTO_POOL)[:2]))

    def test_validate_missing_file(self):
        self.assertEqual(auto_pool.validate_pool_split(os.path.join(tempfile.gettempdir(), "nope.json")), [])

    def test_validate_default_pool_is_manual(self):
        """pool 字段缺失按 manual 计 → 与 auto 池重叠即冲突。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "watchlist_buy.json")
            _write_watchlist(p, {"600481": {"pool": "manual"}})
            self.assertIn("600481", auto_pool.validate_pool_split(p))
            _write_watchlist(p, {"600481": {"status": "monitoring"}})  # 无 pool 字段 → 默认 manual
            self.assertIn("600481", auto_pool.validate_pool_split(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
