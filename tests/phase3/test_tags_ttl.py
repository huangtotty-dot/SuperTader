# -*- coding: utf-8 -*-
"""技术标签缓存 TTL —— 收盘后不再每 2 分钟全量重算（2026-10-21）。

实证：15:28 打开 K 线报「后端 load_stock_chart 超时 20000ms」，`t_io/logs/gui_freeze.log`
的现场栈里 6 个线程全在 `_stock_tags_one → get_trend30 → _ir_adx/_ir_atr_wilder`（pandas
ewm）与 `detect_daily_divergence`（numpy median）——盘后每 120s 一次的全量标签重算把
GIL 吃满，图表请求排队到超时。盘后日线已定稿，标签不会再变 ⇒ TTL 放到 6 小时。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import t_gui   # noqa: E402

_JS = (Path(__file__).resolve().parents[2] / "web" / "app.js").read_text(encoding="utf-8")


class _DT:
    def __init__(self, fake):
        self._f = fake

    def now(self, tz=None):
        return self._f


class TestTagsTTL:
    def _ttl_at(self, monkeypatch, fake):
        monkeypatch.setattr(t_gui, "datetime", _DT(fake))
        return t_gui._tags_ttl_now()

    def test_intraday_is_short(self, monkeypatch):
        # 2026-10-30：120s → **300s**。实测盘中每 30 分钟边界都会起 6~9 个池线程、
        # 单核占用冲到 55~62%（thread_cpu.jsonl），其中一大块是 120s 到期后的整批重算。
        # 标签的"历史部分"已由**日线缓存指纹**兜底（缓存一变立刻重算）⇒ 只放宽"含当日
        # forming bar 的那几项"的刷新节拍，最坏晚 5 分钟。
        assert self._ttl_at(monkeypatch, datetime(2026, 10, 21, 10, 30)) == pytest.approx(300.0)

    def test_after_close_is_long(self, monkeypatch):
        assert self._ttl_at(monkeypatch, datetime(2026, 10, 21, 15, 30)) >= 6 * 3600 - 1

    def test_weekend_is_long(self, monkeypatch):
        assert self._ttl_at(monkeypatch, datetime(2026, 10, 24, 10, 0)) >= 6 * 3600 - 1

    def test_preopen_is_long(self, monkeypatch):
        assert self._ttl_at(monkeypatch, datetime(2026, 10, 21, 8, 0)) >= 6 * 3600 - 1

    def test_both_ttl_sites_use_it(self):
        import inspect
        src = inspect.getsource(t_gui.Api.load_stock_tags_batch)
        assert "_tags_ttl_now()" in src, "批量缓存 TTL 必须走动态值"
        src2 = inspect.getsource(t_gui.Api._stock_tags_one)
        assert "_tags_ttl_now()" in src2, "单只缓存 TTL 也要走动态值"

    def test_worker_pool_is_capped(self):
        import inspect
        src = inspect.getsource(t_gui.Api.load_stock_tags_batch)
        assert "max_workers=3" in src, "标签批算是纯 GIL 活，别开大池和 UI 抢"

    def test_chart_call_timeout_is_relaxed(self):
        assert "load_stock_chart: 45000" in _JS
