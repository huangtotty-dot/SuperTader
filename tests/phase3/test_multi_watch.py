# -*- coding: utf-8 -*-
"""多窗口看盘（2026-10-21 owner）：体检面板上的 3×2 六宫格，每格自选一只票的 5 分 K。

后端 `load_multi_chart` 复用单图的 SWR 取帧，只回**最后 tail 根**（单图是几千根深档，
六宫格每格只有 ~1/6 屏宽）⇒ 一轮 6 格约 60KB 而不是 6×100KB。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _frame(n=300):
    return {
        "series": {
            "dates": [f"d{i}" for i in range(n)],
            "ohlc": [[1.0, 2.0, 0.5, 2.5]] * n,
            "volume": [100.0] * n,
            "amount": [1e8] * n,
            # ma 是「7 条均线的数组的数组」—— 裁尾巴时**每条都要裁**，只裁外层是经典坑
            "ma": [[float(i) for i in range(n)] for _ in range(7)],
        },
        "quote": {"price": 10.0, "change_pct": 1.25},
        "sig": "2026-10-21 09:35:10.0:300",
    }


class TestLoadMultiChart:
    def test_trims_every_series_to_tail(self, monkeypatch):
        api = t_gui.Api()
        monkeypatch.setattr(api, "_chart_live_frame", lambda c, p: (_frame(300), "fresh"))
        r = api.load_multi_chart(["600000"], "min5", 90)
        it = r["items"][0]
        assert it["ok"] and it["code"] == "600000"
        assert len(it["series"]["dates"]) == 90
        assert it["series"]["dates"][-1] == "d299"
        assert all(len(x) == 90 for x in it["series"]["ma"]), "ma 必须逐条裁"
        assert it["quote"]["price"] == 10.0

    def test_warming_item_is_reported_not_raised(self, monkeypatch):
        api = t_gui.Api()
        monkeypatch.setattr(api, "_chart_live_frame", lambda c, p: (None, "warming"))
        r = api.load_multi_chart(["600000"], "min5", 90)
        it = r["items"][0]
        assert it["ok"] is False and it["warming"] is True

    def test_capped_at_12_and_strips_underscore_suffix(self, monkeypatch):
        api = t_gui.Api()
        monkeypatch.setattr(api, "_chart_live_frame", lambda c, p: (_frame(50), "fresh"))
        codes = [f"{600000 + i}" for i in range(20)]
        r = api.load_multi_chart(codes, "min5", 40)
        assert len(r["items"]) == 12                     # 防误传整池
        # 与单图 `load_chart_realtime` 同口径：只剥 `_` 之后的标记位，**不剥市场前缀**
        # （指数要用 sh000001 这种带前缀的码，剥了反而查不到）
        r2 = api.load_multi_chart(["600000_1"], "min5", 40)
        assert r2["items"][0]["code"] == "600000"

    def test_never_raises_on_bad_frame(self, monkeypatch):
        api = t_gui.Api()

        def _boom(c, p):
            raise RuntimeError("x")
        monkeypatch.setattr(api, "_chart_live_frame", _boom)
        r = api.load_multi_chart(["600000"], "min5", 90)
        assert r["items"][0]["ok"] is False and "error" in r["items"][0]


class TestFrontend:
    def test_button_on_ob_panel(self):
        assert "openMultiWatch()" in _HTML
        assert "多窗口看盘" in _HTML

    def test_modal_is_3x2(self):
        assert 'id="multiWatchModal"' in _HTML
        assert 'id="multiWatchGrid"' in _HTML
        assert "grid-template-columns: repeat(3" in (_ROOT / "web" / "style.css").read_text(encoding="utf-8")
        assert "grid-template-rows: repeat(2" in (_ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_cells_have_selector_and_chart(self):
        assert "function _mwBuildCells()" in _JS
        assert 'class="mw-sel"' in _JS
        assert "function mwPick(i, code)" in _JS
        assert 'id="mwChart' in _JS

    def test_realtime_polling(self):
        assert "MW_PERIOD_MS = 4000" in _JS
        assert "mwTimer = setInterval(mwTick, MW_PERIOD_MS)" in _JS
        assert '"load_multi_chart", codes, "min5", 90' in _JS
        # 单飞：上一轮没回来不叠请求（6 格 × 每格 ~1s 计算，叠起来会拖死）
        assert "if (mwBusy) return;" in _JS

    def test_selection_persisted(self):
        assert 'MW_LS_KEY = "multiWatchCodes"' in _JS
        assert "localStorage.setItem(MW_LS_KEY" in _JS

    def test_closes_and_disposes(self):
        assert "function closeMultiWatch()" in _JS
        assert "mwTimer = null" in _JS
        assert "i.dispose()" in _JS
