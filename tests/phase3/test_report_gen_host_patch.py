# -*- coding: utf-8 -*-
"""外部 report_gen 模块「加载时改写腾讯主机名」（2026-11-16，全离线）。

背景：`E:\\04_实战资料\\report_gen\\modules\\market_data.py` 里写死
`https://ifzq.gtimg.cn/...fqkline`，该主机现在对 fqkline 直接回 **HTTP 501**
（我们自己的 tencent_provider 早已改走 `web.ifzq.gtimg.cn`）⇒ 1227 只全部取数失败
⇒ 概念 TOP3 恒 0。修法是在 `_load_report_gen_modules` 里**加载时替换主机名**，
外部仓库文件不动。这里用一个假目录钉住这个行为。

运行：python -m unittest tests.phase3.test_report_gen_host_patch -v
"""
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import execution.daily_sentiment as ds  # noqa: E402

_MARKET_SRC = '''# fake module
HOST = "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=x"
UNTOUCHED = "https://ifzq.gtimg.cn/other/endpoint"
'''


class TestReportGenHostPatch(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        moddir = os.path.join(self.td, "modules")
        os.makedirs(moddir, exist_ok=True)
        with open(os.path.join(moddir, "market_data.py"), "w", encoding="utf-8") as f:
            f.write(_MARKET_SRC)
        for name in ("data_loader.py", "scorer.py"):
            with open(os.path.join(moddir, name), "w", encoding="utf-8") as f:
                f.write("# fake\n")
        self._old_cache = ds._RG_MOD_CACHE
        ds._RG_MOD_CACHE = {}

    def tearDown(self):
        ds._RG_MOD_CACHE = self._old_cache

    def test_fqkline_host_rewritten(self):
        """fqkline 地址换成 web.ifzq；其它地址（非 fqkline 前缀替换规则）一并按前缀换。"""
        mods = ds._load_report_gen_modules(self.td)
        self.assertTrue(mods["rg_market_data"].HOST.startswith("https://web.ifzq.gtimg.cn/"))

    def test_empty_module_still_loads(self):
        """没有该字面量的模块照常 exec（走原 loader 路径）。"""
        mods = ds._load_report_gen_modules(self.td)
        self.assertIn("rg_data_loader", mods)
        self.assertIn("rg_scorer", mods)


if __name__ == "__main__":
    unittest.main(verbosity=2)
