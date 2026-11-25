# -*- coding: utf-8 -*-
"""每日涨跌家数（core.breadth_daily，2026-11-25，全离线）。

口径：
  · `close > prev_close` 上涨 / `<` 下跌 / `=` 平盘；首日无前收不计；
  · **永远排除今天**（盘中 forming bar 不是收盘口径）；
  · 覆盖家数 <2000 的日期丢弃；缓存按自然日，盘后后台重建；
  · `merge_into` 按 date 把 up/dn/flat/n 补进情绪面板的 rows。

运行：python -m pytest tests/phase3/test_breadth_daily.py -q
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.breadth_daily as BD  # noqa: E402


def _rows(*pairs):
    return [{"date": d, "close": c} for d, c in pairs]


class TestCountRows:
    def test_涨跌平与首日(self):
        rows = _rows(("2026-11-20", 10), ("2026-11-21", 11), ("2026-11-24", 9),
                     ("2026-11-25", 9))
        got = BD.count_rows(rows, skip_date="2026-11-25")
        assert got == {"2026-11-21": (1, 0, 0), "2026-11-24": (0, 1, 0)}, got
        assert "2026-11-25" not in got, "今天必须排除（forming bar）"

    def test_平盘与坏行(self):
        rows = [{"date": "2026-11-20", "close": 10},
                {"date": "2026-11-21", "close": 10},
                {"date": "2026-11-24", "close": None},
                {"date": "2026-11-25", "close": 12}]
        got = BD.count_rows(rows, skip_date=None)
        assert got.get("2026-11-21") == (0, 0, 1)
        assert "2026-11-25" not in got, "前一根坏行 ⇒ 无法比较，不计"


class TestBuild:
    def test_聚合与样本闸(self, monkeypatch, tmp_path):
        monkeypatch.setattr(BD, "_MIN_COVER", 2)
        for code, rows in {
            "600000": _rows(("2026-11-20", 10), ("2026-11-21", 11), ("2026-11-24", 12)),
            "000001": _rows(("2026-11-20", 10), ("2026-11-21", 9), ("2026-11-24", 9)),
            "300750": _rows(("2026-11-20", 10), ("2026-11-21", 12), ("2026-11-24", 12)),
        }.items():
            (tmp_path / f"{code}.json").write_text(
                json.dumps({"rows": rows}), encoding="utf-8")
        # 非股票文件不参与
        (tmp_path / "510300.json").write_text(json.dumps({"rows": []}), encoding="utf-8")
        got = BD.build(str(tmp_path), workers=2)
        assert got["2026-11-21"] == [2, 1, 0, 3], got.get("2026-11-21")
        assert got["2026-11-24"] == [1, 0, 2, 3], got.get("2026-11-24")  # 涨1 平2
        assert "2026-11-20" not in got, "首日无前收"


class TestCache:
    def _patch(self, monkeypatch, tmp_path, payload, in_session=False):
        fp = tmp_path / "breadth_daily.json"
        if payload is not None:
            fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setattr(BD, "_CACHE_FP", str(fp))
        monkeypatch.setattr(BD, "_state", {"data": None, "ts": 0.0, "running": False})
        monkeypatch.setattr(BD, "_in_session", lambda: in_session)
        return fp

    def test_无缓存_pending并重建(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path, None)
        kicked = []
        monkeypatch.setattr(BD, "_kick_build", lambda: kicked.append(1))
        r = BD.get_cached(86400)
        assert r["pending"] is True and r["stale"] is True
        assert kicked == [1]

    def test_当日缓存_立返不重建(self, monkeypatch, tmp_path):
        self._patch(monkeypatch, tmp_path,
                    {"schema": BD._SCHEMA, "date": BD._today(), "built_at": "15:10:00",
                     "series": {"2026-11-24": [1000, 2000, 50, 3000]}})
        kicked = []
        monkeypatch.setattr(BD, "_kick_build", lambda: kicked.append(1))
        r = BD.get_cached(86400)
        assert r["pending"] is False and r["stale"] is False and r["n_days"] == 1
        assert kicked == []

    def test_盘中不重建_盘后重建(self, monkeypatch, tmp_path):
        """跨日旧档：盘中照常下发（stale=True）但**不扫盘**；盘后才踢重建。"""
        payload = {"schema": BD._SCHEMA, "date": "19990101", "built_at": "10:00:00",
                   "series": {"2026-11-24": [1000, 2000, 50, 3000]}}
        kicked = []
        monkeypatch.setattr(BD, "_kick_build", lambda: kicked.append(1))
        self._patch(monkeypatch, tmp_path, payload, in_session=True)
        r = BD.get_cached(86400)
        assert r["pending"] is False and r["stale"] is True
        assert kicked == [], "盘中不扫盘（25s CPU 会和 UI 抢 GIL）"
        # 盘后同一份档 ⇒ 允许重建
        monkeypatch.setattr(BD, "_in_session", lambda: False)
        BD.get_cached(86400)
        assert kicked == [1]


class TestMerge:
    def test_合并进情绪行(self, monkeypatch):
        monkeypatch.setattr(BD, "get_cached", lambda ttl=86400: {
            "series": {"2026-11-24": [1000, 2000, 50, 3050]},
            "built_at": "x", "date": BD._today(), "pending": False, "stale": False, "n_days": 1})
        rows = [{"date": "2026-11-24"}, {"date": "2026-11-25"}]
        meta = BD.merge_into(rows)
        assert (rows[0]["up"], rows[0]["dn"], rows[0]["flat"], rows[0]["n"]) == (1000, 2000, 50, 3050)
        assert rows[1]["up"] is None and rows[1]["n"] is None
        assert meta["n_days"] == 1
