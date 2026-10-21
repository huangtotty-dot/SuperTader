# -*- coding: utf-8 -*-
"""非分钟视图不得触发「四档分钟全抓」（2026-10-21）。

现场（`t_io/logs/chart_zoom.log` 15:28:22，owner 截图那次）：
    15:28:22.479 load_req  {code:000988, period:"monthly", wantMin:true}
    15:28:42      chart_error（前端 20s 投递上限丢弃）
后端 `load_stock_chart` 只在 `period in _MIN_PERIODS` 时才知道要哪一档；月线 Tab 传
want_minutes=True 却拿不到档位 ⇒ `_build_chart_from_df` 的 `_jobs` 退化成**四档全抓**
（min5 3200 根 + min10 1600 + min30/60 各 320），实测 12.5s（同一进程 15:28:42 那次
`load_req → load_resp` 的间隔），并发时越过 20s。

两条闸都要在：
  ①  后端 `load_stock_chart`：非分钟 period ⇒ want_minutes 强制 False（旧前端/旧缓存也安全）；
  ②  前端 `_wantMinutes()`：只看当前 Tab，不再"上一份 payload 带过 min30 就一直要"。
"""
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


class _Rec:
    """替身：记录 `_serve_payload_cache` 收到的 want_minutes。"""

    def __init__(self, api):
        self.api = api
        self.seen = []

    def __call__(self, code, version=None, want_minutes=False, allow_network=False,
                 period=None):
        self.seen.append(bool(want_minutes))
        return {"code": code, "available": True, "period_data": {}, "version": "v"}


@pytest.fixture()
def api(monkeypatch):
    a = t_gui.Api()
    a._stock_chart_cache = {}
    # 日线缓存给足行长，避免走网络/重建分支（本测试只关心 want_minutes 的收口）
    monkeypatch.setattr(a, "_serve_payload_cache", _Rec(a))
    return a


class TestBackendCoercion:
    @pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
    def test_non_minute_period_drops_want_minutes(self, api, period):
        api.load_stock_chart("600176", None, True, period)
        assert api._serve_payload_cache.seen == [False], (
            f"{period} 视图不该要分钟线（会触发四档全抓）")

    @pytest.mark.parametrize("period", list(t_gui._MIN_PERIODS))
    def test_minute_period_keeps_want_minutes(self, api, period):
        api.load_stock_chart("600176", None, True, period)
        assert api._serve_payload_cache.seen == [True], "分钟 Tab 必须按档抓"

    def test_period_none_unchanged(self, api):
        """period=None（预下载/其它调用方）行为不变。"""
        api.load_stock_chart("600176", None, True, None)
        assert api._serve_payload_cache.seen == [True]


class TestFrontendWantsMinutes:
    def _fn_body(self):
        m = re.search(r"function _wantMinutes\(\) \{(.*?)\n\}", _JS, re.S)
        assert m, "找不到 _wantMinutes()"
        return m.group(1)

    def test_only_current_period(self):
        body = self._fn_body()
        assert "MIN_PERIODS.includes(stockChartPeriod)" in body
        assert "pd.min30" not in body and "stockChartData" not in body, (
            "不再看上一份 payload —— 那会让日/周/月 Tab 也带上 wantMin")
