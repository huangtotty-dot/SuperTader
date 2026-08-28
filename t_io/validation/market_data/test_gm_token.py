# -*- coding: utf-8 -*-
"""gm_token 发现链单测（P4-4 新终端适配）。pytest / unittest 均可运行。
覆盖：token 正则解析、单进程候选发现（psutil mock）、多候选、GM_TOKEN env 覆盖、缺进程回退。"""
import json
import os
import sys
import unittest
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, _ROOT)

from core.market_data import gm_token  # noqa: E402

TOK = "9977f6f956ea0db5f9c4ca417f366cd52dcebb78"
GMTERM_CMD = [r"C:\...\gmterm-serv.exe", "serve", f"--token={TOK}",
              "--config=C:\\Users\\Lenovo\\.gszq\\.gmserv.json", "--orgCode=gszq"]
GSGM_CMD = [r"C:\...\gsgm3.exe", "--type=renderer", "--service-pipe-token=CB33E61F7E89AC64288403ACEF8A545A"]


def _fake_psutil(procs):
    """构造 fake psutil.process_iter 返回（name/cmdline）。"""
    class _P:
        def __init__(self, name, cmdline):
            self.info = {"name": name, "cmdline": cmdline}
    return [_P(n, c) for n, c in procs]


class TestGmToken(unittest.TestCase):
    def setUp(self):
        # 生产环境常缺 psutil（gm_token 回退 wmic）；注入假 psutil 使 psutil 分支可测
        if "psutil" not in sys.modules:
            sys.modules["psutil"] = mock.MagicMock()
            self._fake_psutil_injected = True

    def test_parse_token(self):
        self.assertEqual(gm_token._parse_token(" ".join(GMTERM_CMD)), TOK)
        self.assertIsNone(gm_token._parse_token("no token here"))
        self.assertIsNone(gm_token._parse_token(""))

    def test_terminal_token_via_psutil(self):
        with mock.patch("psutil.process_iter", return_value=_fake_psutil([("gmterm-serv.exe", GMTERM_CMD)])):
            self.assertEqual(gm_token._terminal_token(), TOK)

    def test_terminal_token_new_terminal_process(self):
        # 新终端壳 gsgm3.exe 若带 --token 也能命中（P4-4 加固）
        gsgm = [r"C:\...\gsgm3.exe", "serve", f"--token={TOK}"]
        with mock.patch("psutil.process_iter", return_value=_fake_psutil([("gsgm3.exe", gsgm)])):
            self.assertEqual(gm_token._terminal_token(), TOK)

    def test_terminal_token_old_only_still_hits(self):
        with mock.patch("psutil.process_iter", return_value=_fake_psutil([("gmterm-serv.exe", GMTERM_CMD)])):
            self.assertEqual(gm_token._terminal_token(), TOK)

    def test_terminal_token_none_when_no_match(self):
        # 无 token 进程时返回 None；同时 mock 掉 wmic/powershell 回退，避免命中真实运行终端
        with mock.patch("psutil.process_iter", return_value=_fake_psutil([("gsgm3.exe", GSGM_CMD)])):
            with mock.patch("subprocess.run",
                            return_value=mock.Mock(returncode=1, stdout="")) as _mr:
                self.assertIsNone(gm_token._terminal_token())

    def test_load_token_env_override(self):
        with mock.patch.dict(os.environ, {"GM_TOKEN": "envtoken12345678901234567890123456789012"}):
            self.assertEqual(gm_token.load_token(), "envtoken12345678901234567890123456789012")

    def test_load_token_fallback_config(self):
        import tempfile
        os.environ.pop("GM_TOKEN", None)
        cfg = os.path.join(tempfile.mkdtemp(), "gm_config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"token": "cfgtoken12345678901234567890123456789012"}, f)
        with mock.patch.object(gm_token, "_terminal_token", return_value=None):
            with mock.patch.object(gm_token, "_CONFIG", cfg):
                self.assertEqual(gm_token.load_token(), "cfgtoken12345678901234567890123456789012")


if __name__ == "__main__":
    unittest.main(verbosity=2)
