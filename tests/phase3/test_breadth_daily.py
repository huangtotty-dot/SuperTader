# -*- coding: utf-8 -*-
"""每日涨跌家数（core.breadth_daily，全离线 unittest）。

默认排除 as_of 的 forming bar；显式 EOD 可包含 as_of 并截断未来。
缓存新鲜度同时检查自然日、目标交易日、序列末端和样本覆盖。
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.breadth_daily as BD  # noqa: E402


def _rows(*pairs):
    return [{"date": d, "close": c} for d, c in pairs]


class _OfflineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._patchers = []

    def tearDown(self):
        for p in reversed(self._patchers):
            p.stop()
        self._tmp.cleanup()

    def patch(self, obj, name, value):
        p = mock.patch.object(obj, name, value)
        self._patchers.append(p)
        return p.start()


class TestCountRows(_OfflineCase):
    def test_涨跌平与首日(self):
        rows = _rows(("2026-11-20", 10), ("2026-11-21", 11), ("2026-11-24", 9),
                     ("2026-11-25", 9))
        got = BD.count_rows(rows, skip_date="2026-11-25")
        self.assertEqual(got, {"2026-11-21": (1, 0, 0), "2026-11-24": (0, 1, 0)})
        self.assertNotIn("2026-11-25", got)

    def test_平盘与坏行(self):
        rows = [{"date": "2026-11-20", "close": 10},
                {"date": "2026-11-21", "close": 10},
                {"date": "2026-11-24", "close": None},
                {"date": "2026-11-25", "close": 12}]
        got = BD.count_rows(rows, skip_date=None)
        self.assertEqual(got.get("2026-11-21"), (0, 0, 1))
        self.assertNotIn("2026-11-25", got)


class TestBuild(_OfflineCase):
    def test_聚合样本闸与显式EOD(self):
        self.patch(BD, "_MIN_COVER", 2)
        for code, rows in {
            "600000": _rows(("2026-11-20", 10), ("2026-11-21", 11), ("2026-11-24", 12)),
            "000001": _rows(("2026-11-20", 10), ("2026-11-21", 9), ("2026-11-24", 9)),
            "300750": _rows(("2026-11-20", 10), ("2026-11-21", 12), ("2026-11-24", 12)),
        }.items():
            (self.tmp_path / f"{code}.json").write_text(
                json.dumps({"rows": rows}), encoding="utf-8")
        (self.tmp_path / "510300.json").write_text(json.dumps({"rows": []}), encoding="utf-8")

        got = BD.build(str(self.tmp_path), workers=2, as_of="2026-11-24")
        self.assertEqual(got["2026-11-21"], [2, 1, 0, 3])
        self.assertNotIn("2026-11-24", got)

        eod = BD.build(str(self.tmp_path), workers=2,
                       as_of="2026-11-24", include_as_of=True)
        self.assertEqual(eod["2026-11-24"], [1, 0, 2, 3])
        self.assertNotIn("2026-11-20", eod)

    def test_历史EOD硬截断未来(self):
        self.patch(BD, "_MIN_COVER", 1)
        rows = _rows(("2026-11-20", 10), ("2026-11-21", 11),
                     ("2026-11-24", 12), ("2026-11-25", 20))
        (self.tmp_path / "600000.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
        got = BD.build(str(self.tmp_path), workers=1,
                       as_of="2026-11-21", include_as_of=True)
        self.assertEqual(list(got), ["2026-11-21"])


class TestCache(_OfflineCase):
    def test_当日EOD只能由受控钩子放行(self):
        kicked = []
        self.patch(BD, "_state", {"data": None, "ts": 0.0, "running": False})
        self.patch(BD, "_kick_build", lambda *a, **kw: kicked.append((a, kw)))
        today = BD.datetime.now().strftime("%Y-%m-%d")
        self.assertTrue(BD.refresh_eod(today))
        self.assertEqual(kicked, [((today,), {"include_as_of": True, "eod_verified": True})])

    def test_普通重建不能把时钟当当日EOD证明(self):
        captured = {}
        self.patch(BD, "_threading", mock.Mock())
        self.patch(BD, "build", lambda **kw: captured.update(kw) or {})
        BD._kick_build(BD.datetime.now().strftime("%Y-%m-%d"),
                       include_as_of=True, eod_verified=False)
        BD._threading.Thread.call_args.kwargs["target"]()
        self.assertFalse(captured["include_as_of"])

    def _cache(self, payload, in_session=False):
        fp = self.tmp_path / "breadth_daily.json"
        if payload is not None:
            fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        self.patch(BD, "_CACHE_FP", str(fp))
        self.patch(BD, "_state", {"data": None, "ts": 0.0, "running": False})
        self.patch(BD, "_in_session", lambda: in_session)
        return fp

    def test_无缓存_pending并重建(self):
        self._cache(None)
        kicked = []
        self.patch(BD, "_kick_build", lambda: kicked.append(1))
        r = BD.get_cached(86400)
        self.assertTrue(r["pending"])
        self.assertTrue(r["stale"])
        self.assertEqual(r["data_status"], "insufficient_data")
        self.assertEqual(kicked, [1])

    def test_当日缓存且序列覆盖_立返不重建(self):
        self._cache({"schema": BD._SCHEMA, "date": BD._today(), "built_at": "15:10:00",
                     "series": {"2026-11-24": [1000, 2000, 50, 3000]}})
        kicked = []
        self.patch(BD, "_kick_build", lambda *a, **kw: kicked.append(1))
        r = BD.get_cached(86400, expected_date="2026-11-24")
        self.assertFalse(r["pending"])
        self.assertFalse(r["stale"])
        self.assertEqual(r["n_days"], 1)
        self.assertEqual(r["data_status"], "ready")
        self.assertEqual(r["series_end"], "2026-11-24")
        self.assertEqual(kicked, [])

    def test_同自然日但缺最新交易日_stale并重建(self):
        self._cache({"schema": BD._SCHEMA, "date": BD._today(), "built_at": "15:10:00",
                     "series": {"2026-11-24": [1000, 2000, 50, 3000]}})
        kicked = []
        self.patch(BD, "_kick_build", lambda *a, **kw: kicked.append((a, kw)))
        r = BD.get_cached(86400, expected_date="2026-11-25")
        self.assertTrue(r["stale"])
        self.assertEqual(r["data_status"], "stale")
        self.assertEqual(kicked, [(('2026-11-25',), {'include_as_of': True})])

    def test_目标日覆盖不足_stale(self):
        self._cache({"schema": BD._SCHEMA, "date": BD._today(), "built_at": "15:10:00",
                     "series": {"2026-11-25": [10, 20, 5, 35]}})
        kicked = []
        self.patch(BD, "_kick_build", lambda *a, **kw: kicked.append(1))
        r = BD.get_cached(86400, expected_date="2026-11-25")
        self.assertTrue(r["stale"])
        self.assertEqual(kicked, [1])

    def test_盘中不重建_盘后重建(self):
        payload = {"schema": BD._SCHEMA, "date": "19990101", "built_at": "10:00:00",
                   "series": {"2026-11-24": [1000, 2000, 50, 3000]}}
        kicked = []
        self.patch(BD, "_kick_build", lambda: kicked.append(1))
        self._cache(payload, in_session=True)
        r = BD.get_cached(86400)
        self.assertFalse(r["pending"])
        self.assertTrue(r["stale"])
        self.assertEqual(kicked, [])
        self.patch(BD, "_in_session", lambda: False)
        BD.get_cached(86400)
        self.assertEqual(kicked, [1])


class TestMerge(_OfflineCase):
    def test_合并进情绪行并传完成日锚点(self):
        seen = {}

        def fake_get(ttl=86400, expected_date=None):
            seen["expected_date"] = expected_date
            return {"series": {"2026-11-24": [1000, 2000, 50, 3050]},
                    "built_at": "x", "date": BD._today(), "pending": False,
                    "stale": False, "n_days": 1}

        self.patch(BD, "get_cached", fake_get)
        rows = [{"date": "2026-11-24"}, {"date": "2026-11-25"}]
        meta = BD.merge_into(rows)
        self.assertEqual((rows[0]["up"], rows[0]["dn"], rows[0]["flat"], rows[0]["n"]),
                         (1000, 2000, 50, 3050))
        self.assertIsNone(rows[1]["up"])
        self.assertIsNone(rows[1]["n"])
        self.assertEqual(meta["n_days"], 1)
        self.assertEqual(seen["expected_date"], "2026-11-25")


if __name__ == "__main__":
    unittest.main()
