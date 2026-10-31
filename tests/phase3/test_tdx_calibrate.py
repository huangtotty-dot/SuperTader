# -*- coding: utf-8 -*-
"""通达信日线校准的单测（2026-10-30）。

守四条：
  ① **锚点之前只读**：锚点（最后一天"量对得上"的重叠日）及更早的行一字不改；
  ② **尾部重写**：锚点之后按通达信 × r 重写（既补缺失，也修被**盘中快照**污染的根
     —— 实测 000006 10-29 缓存 vol=0.0 而真实 180127 手）；
  ③ **复权安全门**：锚点比率 |r−1| > 2% ⇒ 不写（ratio_mismatch）；
  ④ 量单位：通达信 `.day` 是股、缓存是手 ⇒ `/100`（读取侧已转）。

运行：python -m pytest -q tests/phase3/test_tdx_calibrate.py
"""
import importlib.util
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location("tdx_cal", os.path.join(_ROOT, "core", "tdx_calibrate.py"))
tdx_cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tdx_cal)


def _day_row(date, o, h, l, c, vol_shares) -> bytes:
    d = int(date.replace("-", ""))
    return struct.pack("<IIIIIfii", d, int(round(o * 100)), int(round(h * 100)),
                       int(round(l * 100)), int(round(c * 100)),
                       float(vol_shares * c), int(vol_shares), 0)


class TestCalibrate(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        (self.home / "vipdoc" / "sz" / "lday").mkdir(parents=True)
        self._orig_dir = tdx_cal.CACHE_DIR
        self.cache_dir = self.home / "cache"
        self.cache_dir.mkdir()
        tdx_cal.CACHE_DIR = self.cache_dir

    def tearDown(self):
        tdx_cal.CACHE_DIR = self._orig_dir
        self.td.cleanup()

    def _write_tdx(self, code, rows):
        mk = tdx_cal.tdx_market(code)
        fp = self.home / "vipdoc" / mk / "lday" / f"{mk}{code}.day"
        fp.write_bytes(b"".join(rows))

    def _write_cache(self, code, rows, date):
        (self.cache_dir / f"{code}.json").write_text(json.dumps(
            {"date": date, "forming": [], "rows": rows}, ensure_ascii=False), encoding="utf-8")

    def test_补缺失尾部_且锚点以早的行不动(self):
        code = "000001"
        self._write_tdx(code, [
            _day_row("2026-10-26", 10.0, 10.4, 9.9, 10.0, 1_000_000),
            _day_row("2026-10-27", 10.0, 10.5, 9.9, 10.2, 1_000_000),
            _day_row("2026-10-28", 10.2, 10.6, 10.1, 10.4, 900_000),
        ])
        self._write_cache(code, [
            {"date": "2026-10-26", "open": 10.0, "close": 10.0, "high": 10.4, "low": 9.9, "volume": 10000.0},
        ], "2026-10-26")
        r = tdx_cal.calibrate_one(code, self.home, fix=True, bak_dir=self.home / "bak")
        self.assertEqual(r["status"], "fixed")
        self.assertEqual(r["anchor"], "2026-10-26")
        out = json.loads((self.cache_dir / f"{code}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(out["rows"]), 3)
        self.assertEqual(out["rows"][0]["close"], 10.0, "锚点行不得被改")
        self.assertAlmostEqual(out["rows"][1]["volume"], 10000.0, places=2)  # 1e6 股 /100
        self.assertEqual(out["date"], "2026-10-28")
        self.assertEqual(out["forming"], [])
        self.assertTrue((self.home / "bak" / f"{code}.json").exists())

    def test_重写被盘中快照污染的尾部(self):
        """量只有真实 10% 的那根是快照根 ⇒ 必须按通达信改回真实收盘/成交量。"""
        code = "000006"
        self._write_tdx(code, [
            _day_row("2026-10-26", 6.5, 6.6, 6.4, 6.48, 1_000_000),
            _day_row("2026-10-27", 6.4, 6.5, 6.2, 6.28, 2_000_000),
            _day_row("2026-10-28", 6.3, 6.5, 6.2, 6.44, 1_500_000),
        ])
        self._write_cache(code, [
            {"date": "2026-10-26", "open": 6.5, "close": 6.48, "high": 6.6, "low": 6.4, "volume": 10000.0},
            {"date": "2026-10-27", "open": 6.4, "close": 6.68, "high": 6.7, "low": 6.3, "volume": 2321.0},
        ], "2026-10-27")
        r = tdx_cal.calibrate_one(code, self.home, fix=True, bak_dir=None)
        self.assertEqual(r["status"], "fixed")
        self.assertEqual(r["anchor"], "2026-10-26")
        self.assertEqual(r["polluted"], 2)
        out = json.loads((self.cache_dir / f"{code}.json").read_text(encoding="utf-8"))
        self.assertEqual(out["rows"][0]["close"], 6.48, "锚点行不动")
        self.assertEqual(out["rows"][1]["close"], 6.28, "快照根应被真实收盘覆盖")
        self.assertAlmostEqual(out["rows"][1]["volume"], 20000.0, places=2)
        self.assertEqual(out["rows"][2]["close"], 6.44)

    def test_复权比率不符不写(self):
        code = "000002"
        self._write_tdx(code, [
            _day_row("2026-10-26", 10, 10.5, 9.9, 10.0, 1_000_000),
            _day_row("2026-10-27", 10, 10.5, 9.9, 10.0, 1_000_000),
        ])
        self._write_cache(code, [
            {"date": "2026-10-26", "open": 8, "close": 8.0, "high": 8.4, "low": 7.9, "volume": 10000.0},
        ], "2026-10-26")
        r = tdx_cal.calibrate_one(code, self.home, fix=True, bak_dir=None)
        self.assertEqual(r["status"], "ratio_mismatch")
        out = json.loads((self.cache_dir / f"{code}.json").read_text(encoding="utf-8"))
        self.assertEqual(len(out["rows"]), 1)

    def test_无通达信文件(self):
        r = tdx_cal.calibrate_one("999999", self.home, fix=False, bak_dir=None)
        self.assertEqual(r["status"], "no_tdx")


if __name__ == "__main__":
    unittest.main(verbosity=2)
