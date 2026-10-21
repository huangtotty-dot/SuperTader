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


def _frame(n=120):
    """多窗口**轻量帧**的形状（不是单图的整帧）：只到 K线/MA5/成交额。"""
    return {
        "dates": [f"d{i}" for i in range(n)],
        "ohlc": [[1.0, 2.0, 0.5, 2.5]] * n,
        "volume": [100.0] * n,
        "amount": [1e8] * n,
        "ma5": [float(i) for i in range(n)],
        "quote": {"price": 10.0, "change_pct": 1.25},
    }


class TestLoadMultiChart:
    def test_trims_every_series_to_tail(self, monkeypatch):
        api = t_gui.Api()
        monkeypatch.setattr(api, "_mw_frame", lambda c: (_frame(120), "fresh"))
        r = api.load_multi_chart(["600000"], "min5", 90)
        it = r["items"][0]
        assert it["ok"] and it["code"] == "600000"
        assert len(it["series"]["dates"]) == 90
        assert it["series"]["dates"][-1] == "d119"
        assert len(it["series"]["ma5"]) == 90, "MA5 也要跟着裁"
        assert it["quote"]["price"] == 10.0

    def test_warming_item_is_reported_not_raised(self, monkeypatch):
        api = t_gui.Api()
        monkeypatch.setattr(api, "_mw_frame", lambda c: (None, "warming"))
        r = api.load_multi_chart(["600000"], "min5", 90)
        it = r["items"][0]
        assert it["ok"] is False and it["warming"] is True

    def test_capped_at_12_and_strips_underscore_suffix(self, monkeypatch):
        api = t_gui.Api()
        monkeypatch.setattr(api, "_mw_frame", lambda c: (_frame(60), "fresh"))
        codes = [f"{600000 + i}" for i in range(20)]
        r = api.load_multi_chart(codes, "min5", 40)
        assert len(r["items"]) == 12                     # 防误传整池
        # 与单图 `load_chart_realtime` 同口径：只剥 `_` 之后的标记位，**不剥市场前缀**
        # （指数要用 sh000001 这种带前缀的码，剥了反而查不到）
        r2 = api.load_multi_chart(["600000_1"], "min5", 40)
        assert r2["items"][0]["code"] == "600000"

    def test_never_raises_on_bad_frame(self, monkeypatch):
        api = t_gui.Api()

        def _boom(c):
            raise RuntimeError("x")
        monkeypatch.setattr(api, "_mw_frame", _boom)
        r = api.load_multi_chart(["600000"], "min5", 90)
        assert r["items"][0]["ok"] is False and "error" in r["items"][0]


class TestFrontend:
    def test_button_on_ob_panel(self):
        assert "openMultiWatch()" in _HTML
        assert "多窗口看盘" in _HTML

    def test_window_is_3x2_float(self):
        """owner 2026-10-21：「点开后直接阻断了我看 GUI，需要独立」。

        原来用的是 `.modal-mask`（`position:fixed; inset:0` 的全屏遮罩，z-index 400）——
        后面的界面全被挡住、点不动。现在改成**独立浮窗**：只占自己那一块，后面照常可点。
        """
        _CSS = (_ROOT / "web" / "style.css").read_text(encoding="utf-8")
        assert 'id="multiWatchWin"' in _HTML
        assert 'id="multiWatchGrid"' in _HTML
        assert 'class="mw-win"' in _HTML
        assert 'class="modal-mask"' not in _HTML.split('id="multiWatchWin"')[1].split("</div>")[0]
        # 浮窗：定位 + 可拖 + 可缩放 + 不被全屏遮罩包住
        assert ".mw-win { position: fixed;" in _CSS
        assert "resize: both;" in _CSS
        assert "grid-template-columns: repeat(3" in _CSS
        assert "grid-template-rows: repeat(2" in _CSS
        # 标题栏拖动 + 尺寸变化后小图重排
        assert "function _mwBindDrag(win)" in _JS
        assert "function _mwBindResize()" in _JS

    def test_cells_have_selector_and_chart(self):
        assert "function _mwBuildCells()" in _JS
        # owner：「不要让我输入代码，直接下拉框选体检里的股票」⇒ 只手输框已移除
        assert 'class="mw-sel"' in _JS
        assert 'class="mw-inp"' not in _JS
        assert "function mwPick(i, raw)" in _JS
        assert 'id="mwChart' in _JS

    def test_selector_works_without_ob_data(self):
        """owner 2026-10-21 报「点了多窗口看盘无法选择股票」。

        根因：候选只取 `_obLast`（体检结果），而体检首帧常常是 pending（stocks 为空）
        ⇒ 下拉里只剩一个占位项，等于没得选。现在候选会退回 `state.quotes`
        （10s 轮询顺手拉的持仓行情，比体检先到）—— 但**下拉仍是唯一入口**，不给手输。
        """
        assert "(state.quotes || []).forEach" in _JS
        # 宽容解析：600000 / sh600000 / "600000 浦发银行" 都要能认
        assert r'String(raw || "").match(/(\d{6})/)' in _JS

    def test_cells_show_name_not_code(self):
        """owner：窗口里不要显示代码，直接显示名字。下拉项文案 = 名称（取不到才回退代码）。"""
        assert "${esc(h.name || h.code)}</option>" in _JS

    def test_gap_and_daily_ma_drawn(self):
        """owner：① 缺口没显示 ② 5/10/20 日均线没显示。"""
        # 缺口与单图共用同一个函数，口径不会漂
        assert "function buildGapAreas(ohlc, lookback, minPct, maxN)" in _JS
        assert "buildGapAreas(period.ohlc)" in _JS          # 单图
        assert "buildGapAreas(s.ohlc, 250, 0.003, 8)" in _JS  # 六宫格
        assert "markArea: gaps.length" in _JS
        # 日线均线三条（颜色与单图的「日线MA5/10/20」一致）
        assert '"日MA5"' in _JS and '"日MA10"' in _JS and '"日MA20"' in _JS
        assert "markLine: _dmaData.length" in _JS
        assert '"#58a6ff"' in _JS and '"#bc8cff"' in _JS and '"#2dd4bf"' in _JS

    def test_realtime_polling(self):
        assert "MW_PERIOD_MS = 3000" in _JS
        assert "mwTimer = setInterval(mwTick, MW_PERIOD_MS)" in _JS
        # 每格只画 48 根（5 分 K 的一个交易日）—— owner：「不要显示太多的 K 线」
        assert "MW_TAIL = 48" in _JS
        assert '"load_multi_chart", codes, "min5", MW_TAIL' in _JS
        # 单飞：上一轮没回来不叠请求（6 格 × 每格 ~1s 计算，叠起来会拖死）
        assert "if (mwBusy) return;" in _JS

    def test_selection_persisted(self):
        assert 'MW_LS_KEY = "multiWatchCodes"' in _JS
        assert "localStorage.setItem(MW_LS_KEY" in _JS

    def test_closes_and_disposes(self):
        assert "function closeMultiWatch()" in _JS
        assert "mwTimer = null" in _JS
        assert "i.dispose()" in _JS


class TestLightweightFrame:
    def test_uses_light_frame_not_deep_base(self):
        """owner 2026-10-21：「多窗口 K 线更新明显不对，很慢，几乎看不到实时变化」。

        根因：六宫格原复用**单图实时尾**，而单图现在建在「3 个月深档」（近 3000 根）上，
        实测 ~1s/只；6 只并发全卡在 GIL 上 ⇒ 一帧几十秒，等于不动。
        现在改走 `_mw_frame`：只取 m5 尾 120 根 + MA5 + 成交额，
        **不碰** MACD/RSI/BOLL/黄金分割（细胞只有 1/6 屏宽，用不着）。
        """
        import inspect
        src = inspect.getsource(t_gui.Api._mw_compute)
        assert "_fetch_min_df" in src
        assert "tail(120)" in src
        assert "_calc_ma_and_indicators" not in src, "轻量帧不该跑整套指标"
        assert "_calc_fibonacci" not in src, "轻量帧不该算黄金分割"

    def test_multi_does_not_use_single_chart_cache(self):
        import inspect
        src = inspect.getsource(t_gui.Api.load_multi_chart)
        assert "_mw_frame" in src
        assert "_chart_live_frame" not in src, "别再复用单图的深档帧"
