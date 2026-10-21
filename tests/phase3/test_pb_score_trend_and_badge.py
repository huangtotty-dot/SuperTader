# -*- coding: utf-8 -*-
"""建仓信号扫描：去掉「空头·NO-GO」徽章 + 得分旁加 ↑/↓（2026-10-21，owner 两条要求）。

① 「空头 NO-GO 不需要了，相关代码删除」——该徽章与右侧「时机条件」列（●/○ + 悬停说明）
   重复，而且动荡/空头市**一屏全是同一个徽章**，只是噪音。判定/条件仍由「判定」「时机条件」表达。
② 得分旁加 ↑/↓：相比**上一个有扫描记录的交易日**在升还是在降。
   `score_prev` 由 `Api._prev_day_score_map` 提供，口径 = 该日 `scan_time` **最新**那条的
   `composite_score`（与表格「得分」同源；不是当日的最高分快照）。
   ⚠️ 上一日**没扫过**这只票 ⇒ None ⇒ 前端不画箭头（画"—"会被读成"持平"）。
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _rec(code, score, scan_time, **kw):
    r = {"code": code, "composite_score": score, "scan_time": scan_time,
         "verdict": kw.pop("verdict", "approaching"), "conditions": {},
         "scan_type": kw.pop("scan_type", "intraday")}
    r.update(kw)
    return r


def _write_trace(dirpath: Path, date: str, recs):
    fp = dirpath / f"position_builder_{date}.jsonl"
    fp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                  encoding="utf-8")
    return fp


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """把 TRACES / STATE_DIR 指到临时目录，避免依赖真实日期。"""
    monkeypatch.setattr(t_gui, "TRACES", tmp_path)
    monkeypatch.setattr(t_gui, "STATE_DIR", tmp_path)
    (tmp_path / "watchlist_buy.json").write_text(json.dumps(
        {"stocks": {"600000": {"name": "浦发银行", "pool": "manual",
                               "status": "monitoring"}}}), encoding="utf-8")
    t_gui._PREV_SCORE_CACHE.clear()
    yield tmp_path
    t_gui._PREV_SCORE_CACHE.clear()


class TestPrevDayScoreMap:
    def test_latest_record_wins(self, env):
        _write_trace(env, "2026-10-20", [
            _rec("600000", 30, "2026-10-20 09:40:00"),
            _rec("600000", 50, "2026-10-20 14:30:00"),   # 更晚 ⇒ 以它为准
            _rec("600001", 10, "2026-10-20 10:00:00"),
        ])
        m = t_gui.Api()._prev_day_score_map("2026-10-21")
        assert m == {"600000": 50, "600001": 10}

    def test_skips_weekend_gap(self, env):
        """周一（10-19）要比**上一个有 trace 的日子**（10-17 周五），不是"日期减一"。"""
        _write_trace(env, "2026-10-17", [_rec("600000", 70, "2026-10-17 15:00:00")])
        _write_trace(env, "2026-10-19", [_rec("600000", 20, "2026-10-19 15:00:00")])
        m = t_gui.Api()._prev_day_score_map("2026-10-20")
        assert m == {"600000": 20}

    def test_no_earlier_trace_is_empty(self, env):
        _write_trace(env, "2026-10-21", [_rec("600000", 40, "2026-10-21 10:00:00")])
        assert t_gui.Api()._prev_day_score_map("2026-10-21") == {}

    def test_cached_by_mtime(self, env, monkeypatch):
        fp = _write_trace(env, "2026-10-20", [_rec("600000", 50, "2026-10-20 15:00:00")])
        api = t_gui.Api()
        assert api._prev_day_score_map("2026-10-21") == {"600000": 50}
        # 把文件内容换掉但 mtime 不变 ⇒ 应命中缓存（refresh_pb 每 10s 调一次，不能重解析）
        import os
        st = fp.stat()                                        # 先记旧 mtime
        fp.write_text(json.dumps(_rec("600000", 99, "2026-10-20 15:00:00")) + "\n",
                      encoding="utf-8")
        os.utime(fp, ns=(st.st_atime_ns, st.st_mtime_ns))     # 再精确还原（ns 精度）
        assert api._prev_day_score_map("2026-10-21") == {"600000": 50}


class TestRowCarriesScorePrev:
    def test_rows_have_score_prev(self, env):
        _write_trace(env, "2026-10-20", [_rec("600000", 50, "2026-10-20 15:00:00")])
        _write_trace(env, "2026-10-21", [_rec("600000", 40, "2026-10-21 10:00:00")])
        rows = t_gui.Api()._agg_position_builder("2026-10-21").get("rows") or []
        row = next(r for r in rows if r["code"] == "600000")
        assert row["composite_score"] == 40
        assert row["score_prev"] == 50

    def test_unscanned_yesterday_is_none(self, env):
        _write_trace(env, "2026-10-21", [_rec("600000", 40, "2026-10-21 10:00:00")])
        rows = t_gui.Api()._agg_position_builder("2026-10-21").get("rows") or []
        row = next(r for r in rows if r["code"] == "600000")
        assert row["score_prev"] is None, "昨天没扫过 ⇒ None，前端不能画成持平"


class TestFrontend:
    def test_no_go_badge_removed(self):
        assert "_goTxt" not in _JS and "_goCls" not in _JS, "「空头·NO-GO」徽章代码应已删除"
        assert "tmBadge" not in _JS.split("// 建仓/加仓时机门控徽章", 1)[1][:2000], \
            "建仓表行渲染里不该再插 tmBadge"

    def test_arrow_uses_score_prev(self):
        i = _JS.index("const _sp = (r.score_prev")
        seg = _JS[i:i + 1200]
        assert "r.score_prev" in seg
        assert "▲" in seg and "▼" in seg
        assert 'class="up"' in seg and 'class="down"' in seg, "A股口径：升=红(.up)、降=绿(.down)"
