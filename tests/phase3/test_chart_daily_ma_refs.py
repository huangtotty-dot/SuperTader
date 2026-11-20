# -*- coding: utf-8 -*-
"""非日线周期上的「日线 MA 水平参考线」（2026-10-22 owner：其它周期也画 30/60 日均线）。

⚠️ 两条 MA 别混淆：
  · `maSeries`（每周期自己的均线）= 该周期 30/60 根 K 线的均线，一直都有；
  · 本文件说的「日线 MAx 参考线」= 取**日线序列**的 MA 末值画一条水平虚线（30/60 个交易日），
    只在 `stockChartPeriod !== "daily"` 时出现 —— 与已有的 日线MA5/10/20 同口径。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _block(name):
    m = re.search(rf"const {name} = .*?\}} : null;", _JS, re.S)
    assert m, f"找不到 {name}"
    return m.group(0)


class TestDailyMaRefs:
    """2026-11-20 owner：日线均线统一改**斐波那契** 5/13/21/34/55（原来是 5/10/20/30/60）。

    实现已从「五条 dailyMa{N}Line 硬编码」重构成 `_DMA_DEFS` 循环生成 `dailyMaLines`；
    契约不变：①只在非日线周期画；②受「日线均线」开关控制；③标签用 distance 错开；
    ④接进 candlestick 的 markLine。默认开关 = **关**（2026-11-17 owner）。"""

    def test_defs_are_fibonacci_and_non_daily(self):
        m = re.search(r"const _DMA_DEFS = \[(.*?)\];", _JS, re.S)
        assert m, "找不到 _DMA_DEFS"
        body = m.group(0)
        for p_ in ("5", "13", "21", "34", "55"):
            assert f"p: {p_}" in body, f"_DMA_DEFS 缺 MA{p_}"
        assert "for (let i = 0; i < 5" not in body
        # 取值来自**日线** ma[i]（索引 0..4 与后端 _CHART_MA_PERIODS 前 5 条一致）
        assert "_DMA_DEFS.map(o => _lastMa(o.i))" in _JS
        assert "stockChartPeriod !== \"daily\"" in _JS

    def test_labels_are_offsets(self):
        """五条水平线共用左右两端标签位 ⇒ 必须用 distance 错开，否则价位接近就叠字。"""
        m = re.search(r"const _DMA_DEFS = \[(.*?)\];", _JS, re.S)
        body = m.group(0)
        assert "dist: 17" in body and "dist: 34" in body
        assert "日线MA${o.p}" in _JS

    def test_toggle_controls_all_lines(self):
        """统一由「日线均线」开关控制；**默认关闭**（2026-11-17 owner），非日线全周期显示。"""
        html = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
        i = html.index('id="tgDailyMA"')
        seg = html[max(0, i - 200):i + 200]
        assert "tgDailyMA" in seg
        assert "checked" not in seg.split(">")[0], "默认应关闭（2026-11-17）"
        m = re.search(r"const dailyMaLines = (.*?): \[\];", _JS, re.S)
        assert m, "找不到 dailyMaLines 生成块"
        body = m.group(1)
        assert "showDailyMA" in body, "必须受「日线均线」开关控制"
        assert 'stockChartPeriod !== "daily"' in body, "只在非日线周期显示"
        assert 'type: "dashed"' in body
        assert "_isIntradayPeriod" not in body, "不再只限分钟周期"

    def test_wired_into_markline(self):
        assert "...dailyMaLines," in _JS

    def test_last_ma_helper_bounds(self):
        """`_lastMa` 对空数组/缺失索引要返回 null（月线只有 40 根 ⇒ MA60 全 null）。"""
        assert "const _lastMa = (i) => {" in _JS
        assert "return (a && a.length) ? a[a.length - 1] : null;" in _JS
