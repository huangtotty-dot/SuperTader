# -*- coding: utf-8 -*-
"""个股 K 线（含 **5 分钟**周期）上的券商成交买卖点标记（2026-10-21 owner）。

owner：「K线图中的买卖点需要显示在 5 分钟线上」。
排查发现：功能本来就有（`data.trades` → 主图 ▲/▼ scatter），但**归桶要求成交带时间**：

    if (ts.length < 19) continue;      # 分钟图跳过

而台账实测 74 只有成交的票里**只有 7 只**带成交时间（其余 67 只是日期级老行）
⇒ 打开这些票的 5 分钟线，一条买卖点都没有；日线却照常显示 ⇒ 看起来像"5 分钟没做"。

现在：日期级成交落到**当日首根**占位，并标 `approx`（半透明 + tooltip 注明时间未知），
既不丢信息，也不假装精确。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_PY = (_ROOT / "t_gui.py").read_text(encoding="utf-8")


class TestTradeMarksOnMinute:
    def test_min5_bucket_rule(self):
        # K 线时间是右端，成交落进 (bar_t - 5min, bar_t]
        assert "const _pMin = ({ min5: 5, min10: 10, min30: 30, min60: 60 })" in _JS
        assert "_barEnd[bi] - _pMin >= tMin" in _JS

    def test_date_only_trades_are_placed_not_dropped(self):
        assert "if (ts.length < 19) {" in _JS, "日期级成交不该再被 continue 丢掉"
        assert "a0.approx = true;" in _JS
        assert "时间未知（按当日首根占位）" in _JS

    def test_approx_mark_is_visually_subdued(self):
        assert "opacity: a.approx ? 0.55 : 1" in _JS

    def test_daily_path_unchanged(self):
        # 日线按日期精确落，不该带 approx
        assert "if (_barDate[bi] !== tDate) continue;     // 日线按日期精确落" in _JS

    def test_trades_reach_the_chart_payload(self):
        # 两条路径都要挂 trades，否则任何一个入口都会丢标记
        assert _PY.count('out["trades"] = _chart_trades(code)') >= 1
        assert 'payload["trades"] = _chart_trades(code)' in _PY
