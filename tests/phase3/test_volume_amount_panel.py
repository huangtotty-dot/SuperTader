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


class TestPanelStyling:
    """2026-10-20 owner：成交额/MACD 两个副图「更美化、醒目」。

    配色不是拍脑袋选的 —— 都过了 dataviz 校验器（dark 面，CVD ΔE / 常视觉 ΔE / 亮度带 / 对比度）：
      · 成交额MA5 `#c98500` ↔ MA10 `#9085e9`：CVD 27.3 / 常视觉 27.5
      · MACD DIF `#3987e5` ↔ DEA `#d55181`：CVD 15.9 / 常视觉 26.5
    原来 MACD 两条线是 `#ff8c5a` / `#f85149` —— 两个几乎一样的暖红，深底细线上分不出谁是谁。
    """

    def test_amount_ma_overlay_present(self):
        assert '"成交额MA5"' in _JS and '"成交额MA10"' in _JS
        assert '"#c98500"' in _JS and '"#9085e9"' in _JS

    def test_macd_lines_were_separated(self):
        # 只看 MACD 那一段（`#ff8c5a` 在别的图里还有用）
        _macd = _JS.split("// MACD 窗口")[1].split('{ name: "RSI"')[0]
        assert '"#ff8c5a"' not in _macd, "MACD-DIF 又回到与 DEA 几乎同色的旧配色"
        assert '"#3987e5"' in _macd and '"#d55181"' in _macd

    def test_macd_has_zero_line(self):
        assert "yAxis: 0, lineStyle" in _JS

    def test_subplot_heights_give_macd_room(self):
        # 主图 50→48%、MACD 10→12%：柱 + 两条线 + 零轴塞进 10% 太挤
        assert 'top: 50, height: "48%"' in _JS
        assert 'top: "69%", height: "12%"' in _JS


class TestKcbVolumeUnit:
    """科创板(688)成交量单位归一 —— 2026-10-20 owner 截图：10-20 那根成交额画到 2000 亿。

    腾讯按**板块**给不同单位：688 是**股**、其余是**手**（差 100 倍）。
    日线缓存/GM 都是手，所以任何"从腾讯进来"的路径都必须 ÷100，否则整根柱子大 100 倍。
    """

    def test_divisor(self):
        from core.market_data.tencent_provider import _vol_div
        assert _vol_div("sh688008") == 100.0
        assert _vol_div("688008") == 100.0
        assert _vol_div("sh600000") == 1.0
        assert _vol_div("sz000001") == 1.0
        assert _vol_div("sh000001") == 1.0        # 指数
        assert _vol_div("bj920008") == 1.0

    def test_daily_kline_raw_normalizes_688(self, monkeypatch):
        """fqkline 真返回时（WAF 间歇放行）也不能把「股」直接写进按「手」的序列。"""
        import json
        from core.market_data import tencent_provider as TP

        payload = {"data": {"sh688008": {"qfqday": [
            ["2026-10-20", "165.26", "169.27", "171.11", "164.02", "24597313.0"]]}}}
        monkeypatch.setattr(TP, "_pooled_get", lambda *a, **k: json.dumps(payload), raising=False)
        df = TP.daily_kline_raw("688008", days=3)
        assert len(df) == 1
        assert float(df["volume"].iloc[0]) == pytest.approx(245973.13, rel=1e-6)

    def test_daily_kline_raw_leaves_mainboard_alone(self, monkeypatch):
        import json
        from core.market_data import tencent_provider as TP

        payload = {"data": {"sh600000": {"qfqday": [
            ["2026-10-20", "9.79", "9.76", "9.86", "9.68", "605133.0"]]}}}
        monkeypatch.setattr(TP, "_pooled_get", lambda *a, **k: json.dumps(payload), raising=False)
        df = TP.daily_kline_raw("600000", days=3)
        assert float(df["volume"].iloc[0]) == pytest.approx(605133.0, rel=1e-9)


class TestPayloadSchemaGate:
    def test_gate_applies_to_every_view(self):
        """schema 低于当前版本（现为 4：min5/min10 加深近3月）的旧档不许回放 ——
        原来这道闸只写在 `want_minutes` 里面，日 K 视图照样回放旧档，
        于是 688 的成交额还是 2000 亿。"""
        import inspect
        src = inspect.getsource(t_gui.Api._serve_payload_cache)
        # 闸必须在 `if want_minutes:` 之前
        assert src.index('int(hit.get("schema") or 0) < 4') < src.index("if want_minutes:")

    def test_forming_bar_from_batch_snapshot_normalizes_688(self):
        """`_today_forming_bar` 走**批量快照**那条路时同样要 ÷100。

        这是 owner 截图里「10-20 那根成交额画到 2000 亿」的直接来源：该票在持仓/watchlist
        里 ⇒ 命中 `_quotes_snaps` 批量缓存 ⇒ vol_hand 是**股**（24,597,313），
        直接写进按「手」的日线序列 ⇒ 那一根大 100 倍。
        """
        import time as _t
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        api = t_gui.Api()
        api._quotes_snaps = (_t.time(), {"688008": {
            "price": 169.27, "open": 165.26, "high": 171.11, "low": 164.02,
            "vol_hand": 24597313.0, "ts_date": today}})
        bar = api._today_forming_bar("688008", allow_network=False)
        assert bar and abs(float(bar["volume"]) - 245973.13) < 1

    def test_forming_bar_leaves_mainboard_alone(self):
        import time as _t
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        api = t_gui.Api()
        api._quotes_snaps = (_t.time(), {"600000": {
            "price": 9.76, "open": 9.79, "high": 9.86, "low": 9.68,
            "vol_hand": 605133.0, "ts_date": today}})
        bar = api._today_forming_bar("600000", allow_network=False)
        assert bar and abs(float(bar["volume"]) - 605133.0) < 1
