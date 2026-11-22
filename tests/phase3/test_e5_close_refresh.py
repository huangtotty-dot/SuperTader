# -*- coding: utf-8 -*-
"""e5 涨跌停缓存：**当天收盘后必须重取**（2026-11-22，全离线）。

背景（owner 报「市场情绪和同花顺对不上」）：`_ir_fetch_limit_counts_ths` 按日期 append-only
缓存，14:30 尾盘快照先把**盘中数**写进去（11-20 实测 涨停26/跌停25），15:30 的 eod 直接命中
缓存 ⇒ 面板一整天显示盘中数；同花顺收盘真值 28/58。

契约：
  A. 当天且已过收盘（≥15:00）⇒ 无视缓存，重取并覆盖；
  B. 当天盘中 / 历史日期 ⇒ 命中缓存直接返回（省请求、防反爬）。

运行：python -m pytest tests/phase3/test_e5_close_refresh.py -q
"""
import json
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

import analysis.index_regime as ir  # noqa: E402

_PAYLOAD = {
    "status_code": 0,
    "data": {
        "limit_up_count": {"today": {"num": 28, "history_num": 35, "open_num": 7, "rate": 0.8}},
        "limit_down_count": {"today": {"num": 58}},
    },
}


class _Resp:
    status_code = 200

    def __init__(self, body):
        self.text = json.dumps(body)


class _Clock:
    """把 ir 模块里的 time 换掉：固定「今天」与时刻。"""

    def __init__(self, ymd, hm):
        self._ymd, self._hm = ymd, hm

    def strftime(self, fmt, t=None):
        if fmt == "%Y%m%d":
            return self._ymd
        if fmt == "%H:%M":
            return self._hm
        raise ValueError(fmt)

    def localtime(self):
        return object()

    def time(self):
        return 1_700_000_000.0

    def sleep(self, *_a):
        return None


def _run(monkeypatch, clock, cache, calls):
    def _get(*a, **k):
        calls.append(1)
        return _Resp(_PAYLOAD)

    monkeypatch.setattr(ir, "time", clock)
    monkeypatch.setattr(requests, "get", _get)
    return ir._ir_fetch_limit_counts_ths("20261120", timeout=1, retries=0, cache_path=cache)


def _cache_file(stale):
    fd, fp = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"20261120": stale}, f)
    return fp


def test_after_close_refetches_and_overwrites(monkeypatch):
    fp = _cache_file({"date": "20261120", "zt_count": 26, "dt_count": 25})
    calls = []
    rec = _run(monkeypatch, _Clock("20261120", "15:31"), fp, calls)
    assert (rec["zt_count"], rec["dt_count"]) == (28, 58)
    assert len(calls) == 1, "收盘后必须真的重取"
    with open(fp, encoding="utf-8") as f:
        assert json.load(f)["20261120"]["dt_count"] == 58, "缓存要被收盘数覆盖"
    os.unlink(fp)


def test_intraday_still_uses_cache(monkeypatch):
    fp = _cache_file({"date": "20261120", "zt_count": 26, "dt_count": 25})
    calls = []
    rec = _run(monkeypatch, _Clock("20261120", "10:30"), fp, calls)
    assert (rec["zt_count"], rec["dt_count"]) == (26, 25)
    assert not calls, "盘中照旧命中缓存（防反爬）"
    os.unlink(fp)


def test_past_date_uses_cache(monkeypatch):
    fp = _cache_file({"date": "20261120", "zt_count": 26, "dt_count": 25})
    calls = []
    rec = _run(monkeypatch, _Clock("20261125", "10:30"), fp, calls)
    assert (rec["zt_count"], rec["dt_count"]) == (26, 25)
    assert not calls, "历史日期不再重刷"
    os.unlink(fp)
