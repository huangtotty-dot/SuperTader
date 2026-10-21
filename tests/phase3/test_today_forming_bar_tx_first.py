# -*- coding: utf-8 -*-
"""当日 forming bar 的单只快照：腾讯优先（2026-10-21）。

为什么改：`get_provider().snapshot()` 是 GM 优先，而 GM 调用池只有 **1 个 worker**
（`facade._gm_call` 为硬超时故意如此）。开 K 线时若后台预热/标签/扫描正在排队，
这一只快照就要排在它们后面——`bench_boot5` 分阶段计时里它 **8.5s**（单独跑 0.3s），
是 `load_stock_chart` 越过前端 20s 投递上限的主项。
K 线弹窗是展示用途，不需要"与自动盘同源"的 GM 保证（同 2026-10-18 日线取数改腾讯优先）。

⚠️ 换源就要换单位：腾讯 `f[6]` 成交量**主板/创业板/ETF 是「手」、科创板 688 是「股」**
（差 100 倍）。forming bar 写进按「手」的日线序列，688 必须 ÷100；GM 那份本来就是手。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402
from core.market_data import tencent_provider as TP   # noqa: E402

_TODAY = datetime.now().strftime("%Y-%m-%d")


class _FakeTx:
    def __init__(self, snap=None, boom=False):
        self.snap = snap
        self.boom = boom
        self.calls = 0

    def snapshot(self, codes):
        self.calls += 1
        if self.boom:
            raise RuntimeError("tencent down")
        return {str(c).split("_")[0]: self.snap for c in codes} if self.snap else {}


def _api():
    a = t_gui.Api()
    a._quotes_snaps = None          # 强制走"单只快照"回退分支
    return a


@pytest.fixture()
def patch_tx(monkeypatch):
    def _apply(fake):
        monkeypatch.setattr(TP, "TencentProvider", lambda *a, **k: fake)
    return _apply


def test_uses_tencent_and_not_gm(patch_tx, monkeypatch):
    fake = _FakeTx({"price": 10.0, "open": 9.0, "high": 11.0, "low": 8.0,
                    "volume": 5000.0, "ts_date": _TODAY})
    patch_tx(fake)
    called = {"gm": 0}

    import core.market_data as _md
    monkeypatch.setattr(_md, "get_provider",
                        lambda *a, **k: called.__setitem__("gm", called["gm"] + 1) or None)
    bar = _api()._today_forming_bar("600176", allow_network=True)
    assert fake.calls == 1
    assert bar and bar["close"] == 10.0
    assert bar["volume"] == 5000.0, "主板成交量就是手，不要换算"
    assert called["gm"] == 0, "腾讯成功时不该再排 GM 队列"


def test_688_volume_normalized(patch_tx):
    fake = _FakeTx({"price": 10.0, "open": 9.0, "high": 11.0, "low": 8.0,
                    "volume": 200000.0, "ts_date": _TODAY})
    patch_tx(fake)
    bar = _api()._today_forming_bar("688008", allow_network=True)
    assert bar and bar["volume"] == 2000.0, "科创板腾讯给的是股 ⇒ 手要 ÷100"


def test_gm_fallback_when_tencent_fails(patch_tx, monkeypatch):
    patch_tx(_FakeTx(boom=True))
    seen = {}

    class _FakeFacade:
        def snapshot(self, codes):
            seen["codes"] = list(codes)
            return {"600176": {"price": 20.0, "open": 19.0, "high": 21.0, "low": 18.0,
                               "volume": 123.0, "ts_date": _TODAY}}

    import core.market_data as _md
    monkeypatch.setattr(_md, "get_provider", lambda *a, **k: _FakeFacade())
    bar = _api()._today_forming_bar("600176", allow_network=True)
    assert seen["codes"] == ["600176"]
    assert bar and bar["close"] == 20.0
    assert bar["volume"] == 123.0, "GM 那份已是手，绝不能再除 100"


def test_non_today_snapshot_not_used(patch_tx):
    patch_tx(_FakeTx({"price": 10.0, "open": 9.0, "high": 11.0, "low": 8.0,
                      "volume": 100.0, "ts_date": "2026-01-01"}))
    assert _api()._today_forming_bar("600176", allow_network=True) is None
