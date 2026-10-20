# -*- coding: utf-8 -*-
"""K 线「量能」窗口改画**成交额**（2026-10-20 owner：「显示成交额，我不要成交量」）。

背景：owner 报「澜起科技的量能显然不对」—— 我们的窗口画的是**成交量(手)**（24.6 万），
东财那个窗口画的是**成交额(亿)**（41.32 亿），量级差着数量级，看着当然"不对"。

腾讯 K 线只有 volume(手)、没有成交额字段 ⇒ 后端用 VWAP 近似补 `amount`：
    amount = (high + low + close) / 3 × volume × 100
实测 688008 当日 41.36 亿 vs 东财 41.32 亿（差 0.1%）。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _df():
    return pd.DataFrame([
        {"date": "2026-10-19", "open": 169.01, "high": 170.49, "low": 164.60,
         "close": 165.25, "volume": 241677.87},
        {"date": "2026-10-20", "open": 165.26, "high": 171.11, "low": 164.02,
         "close": 169.27, "volume": 245973.13},
    ])


class TestEnsureAmount:
    def test_derives_with_vwap(self):
        out = t_gui._ensure_amount(_df())
        assert "amount" in out.columns
        vwap = (171.11 + 164.02 + 169.27) / 3.0
        assert float(out["amount"].iloc[-1]) == pytest.approx(vwap * 245973.13 * 100, rel=1e-9)
        # 与东财当日 41.32 亿对得上（差 < 1%）
        assert float(out["amount"].iloc[-1]) / 1e8 == pytest.approx(41.32, rel=0.01)

    def test_keeps_existing_amount(self):
        d = _df()
        d["amount"] = [1.0, 2.0]
        out = t_gui._ensure_amount(d)
        assert out["amount"].tolist() == [1.0, 2.0]

    def test_empty_and_none_are_safe(self):
        assert t_gui._ensure_amount(None) is None
        assert t_gui._ensure_amount(pd.DataFrame()).empty


class TestSeriesCarriesAmount:
    def _ready(self, with_amount):
        from t_gui import _calc_ma_and_indicators
        d = _df()
        d["date"] = pd.to_datetime(d["date"])      # `_to_series` 用 strftime，需要 datetime
        if with_amount:
            d = t_gui._ensure_amount(d)
        return _calc_ma_and_indicators(d)

    def test_to_series_includes_amount(self):
        from t_gui import _to_series
        s = _to_series(self._ready(True))
        assert "amount" in s and len(s["amount"]) == len(s["dates"])

    def test_to_series_omits_amount_when_absent(self):
        from t_gui import _to_series
        s = _to_series(self._ready(False))          # 没有 amount 列
        assert "amount" not in s


class TestFrontendRendersAmount:
    def test_series_uses_amount_with_volume_fallback(self):
        assert 'name: _hasAmt ? "成交额(亿)" : "成交量(手)"' in _JS
        assert "period.amount.map(" in _JS

    def test_axis_is_labeled_in_yi(self):
        assert 'v.toFixed(0) : v.toFixed(1)) + "亿"' in _JS or '+ "亿"' in _JS

    def test_tooltip_prefers_amount(self):
        assert '`<div>成交额 <b>${amtTxt}</b></div>`' in _JS
        # 没有成交额时仍准确标"手"，不能把成交量当成交额显示
        assert '`<div>成交 <b>${volTxt}</b> 手</div>`' in _JS
