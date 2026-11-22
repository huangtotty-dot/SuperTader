# -*- coding: utf-8 -*-
"""P0-1(2026-11-22)：成交到达 ⇒ gm_main 心跳持仓/现金快照缓存立即失效。

背景：`_HB_SNAPSHOT_INTERVAL_SEC=300` 把心跳 SDK 降频后，fill 没有反向打通缓存失效
⇒ GUI 持仓链最坏 ~6 分钟陈旧（"看到成交、持仓不变"）。
修复 = `on_order_status` status==3 分支置 `_HB_CACHE["ts"]=None`，下一根 bar 强制重取。

子进程驱动（gm_main_hb_cache_driver.py）真 import gm_main 回放回调，避免 gm.api 桩
污染其它测试。运行：python -m unittest tests.phase3.test_hb_cache_fill_invalidation -v
"""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TestHbCacheFillInvalidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = dict(os.environ)
        env["SMOKE_ROOT"] = str(ROOT)
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop("PYTHONPATH", None)
        driver = ROOT / "tests" / "phase3" / "gm_main_hb_cache_driver.py"
        proc = subprocess.run([sys.executable, str(driver)], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=180)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if line is None:
            raise AssertionError("gm_main 心跳缓存驱动失败\nSTDOUT:\n%s\nSTDERR:\n%s" %
                                 (proc.stdout[-4000:], proc.stderr[-4000:]))
        cls.r = json.loads(line[7:])

    def test_fill_invalidates_hb_cache(self):
        self.assertTrue(self.r["fill_invalidated"],
                        "fill 到达后 _HB_CACHE['ts'] 必须置 None（下一根 bar 强制重取）")

    def test_duplicate_fill_keeps_invalidation(self):
        self.assertTrue(self.r["dup_invalidated"],
                        "重复成交回调（防重闸早退）不得让失效状态回弹")

    def test_pending_cancel_does_not_invalidate(self):
        self.assertTrue(self.r["pending_kept"],
                        "status=6（待撤，非成交）不得失效心跳缓存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
