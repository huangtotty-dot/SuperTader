# -*- coding: utf-8 -*-
"""掘金 token 加载单测 —— core/market_data/gm_token.py（2026-11-13 补）。

只测可离线确定的两件事：
① `_parse_token` 从命令行文本里抠 `--token=<32~64 位 hex>`（psutil/wmic/powershell 三路
   回退链都要靠它提取，正则太松/太紧都会丢 token）；② `load_token` 的进程内记忆化与
   GM_TOKEN 环境变量优先（不扫进程、不读配置文件）。
不测 `_token_from_proc`（要真实终端进程 + wmic/powershell，非本机环境）。

运行：python tests/phase3/test_gm_token.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.market_data.gm_token as gm_token  # noqa: E402

_VALID = "a" * 32  # 恰好 32 位 hex，正则下界


class TestParseToken(unittest.TestCase):
    def test_extracts_from_cmdline(self):
        text = 'gmterm-serv.exe --token=abcdef1234567890abcdef1234567890 --log=1'
        self.assertEqual(gm_token._parse_token(text), "abcdef1234567890abcdef1234567890")

    def test_extracts_64_hex_upper(self):
        tok = "F" * 64
        self.assertEqual(gm_token._parse_token(f"--token={tok}"), tok)

    def test_32_hex_lower_boundary(self):
        self.assertEqual(gm_token._parse_token(f"--token={_VALID}"), _VALID)

    def test_too_short_rejected(self):
        self.assertIsNone(gm_token._parse_token("--token=abc123"))

    def test_no_token_returns_none(self):
        self.assertIsNone(gm_token._parse_token("no token here"))

    def test_none_input_safe(self):
        self.assertIsNone(gm_token._parse_token(None))


class TestLoadTokenMemoization(unittest.TestCase):
    def setUp(self):
        self._saved_env = os.environ.get("GM_TOKEN")
        os.environ.pop("GM_TOKEN", None)
        self._saved_cache = gm_token._TOKEN_CACHED
        gm_token._TOKEN_CACHED = None

    def tearDown(self):
        gm_token._TOKEN_CACHED = self._saved_cache
        if self._saved_env is None:
            os.environ.pop("GM_TOKEN", None)
        else:
            os.environ["GM_TOKEN"] = self._saved_env

    def test_memoized_value_short_circuits(self):
        """已有缓存直接返回，不再扫进程（记忆化的核心价值：GUI 首帧扇出只扫一次）。"""
        gm_token._TOKEN_CACHED = "sentinel-token"
        self.assertEqual(gm_token.load_token(), "sentinel-token")

    def test_env_var_priority(self):
        os.environ["GM_TOKEN"] = "env-token-abc"
        self.assertEqual(gm_token.load_token(), "env-token-abc")

    def test_env_var_is_cached(self):
        os.environ["GM_TOKEN"] = "env-token-abc"
        gm_token.load_token()
        os.environ.pop("GM_TOKEN", None)  # 拿走环境变量后仍返回缓存
        self.assertEqual(gm_token.load_token(), "env-token-abc")


if __name__ == "__main__":
    unittest.main(verbosity=2)
