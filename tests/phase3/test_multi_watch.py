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

import gui   # noqa: E402

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
        "macd": {"dif": [float(i) for i in range(n)],
                 "dea": [float(i) / 2 for i in range(n)],
                 "hist": [float(i) / 3 for i in range(n)]},
        "quote": {"price": 10.0, "change_pct": 1.25},
    }


class TestLoadMultiChart:
    def test_trims_every_series_to_tail(self, monkeypatch):
        api = gui.Api()
        monkeypatch.setattr(api, "_mw_frame", lambda c, period="min5": (_frame(120), "fresh"))
        r = api.load_multi_chart(["600000"], "min5", 90)
        it = r["items"][0]
        assert it["ok"] and it["code"] == "600000"
        assert len(it["series"]["dates"]) == 90
        assert it["series"]["dates"][-1] == "d119"
        assert len(it["series"]["ma5"]) == 90, "MA5 也要跟着裁"
        assert len(it["series"]["macd"]["dif"]) == 90, "MACD 三条线也要跟着裁"
        assert it["quote"]["price"] == 10.0

    def test_warming_item_is_reported_not_raised(self, monkeypatch):
        api = gui.Api()
        monkeypatch.setattr(api, "_mw_frame", lambda c, period="min5": (None, "warming"))
        r = api.load_multi_chart(["600000"], "min5", 90)
        it = r["items"][0]
        assert it["ok"] is False and it["warming"] is True

    def test_capped_at_12_and_strips_underscore_suffix(self, monkeypatch):
        api = gui.Api()
        monkeypatch.setattr(api, "_mw_frame", lambda c, period="min5": (_frame(60), "fresh"))
        codes = [f"{600000 + i}" for i in range(20)]
        r = api.load_multi_chart(codes, "min5", 40)
        assert len(r["items"]) == 12                     # 防误传整池
        # 与单图 `load_chart_realtime` 同口径：只剥 `_` 之后的标记位，**不剥市场前缀**
        # （指数要用 sh000001 这种带前缀的码，剥了反而查不到）
        r2 = api.load_multi_chart(["600000_1"], "min5", 40)
        assert r2["items"][0]["code"] == "600000"

    def test_never_raises_on_bad_frame(self, monkeypatch):
        api = gui.Api()

        def _boom(c, period="min5"):
            raise RuntimeError("x")
        monkeypatch.setattr(api, "_mw_frame", _boom)
        r = api.load_multi_chart(["600000"], "min5", 90)
        assert r["items"][0]["ok"] is False and "error" in r["items"][0]


class TestSlimUnchanged:
    """2026-11-05：后端帧没变（sig 相同）⇒ 只回价格/均线，**不回序列**。

    背景：pywebview 的 `evaluate_js` 是串行的，6 格 × ~30KB 每 4s 反复过桥会把返回通道堵死。
    """

    def test_known_sig_returns_slim_frame(self, monkeypatch):
        api = gui.Api()
        fr = _frame(120)
        fr["sig"] = "d119|2.000|120"
        monkeypatch.setattr(api, "_mw_frame", lambda c, period="min5": (fr, "fresh"))
        r = api.load_multi_chart(["600000"], "min5", 90, {"600000": "d119|2.000|120"})
        it = r["items"][0]
        assert it.get("unchanged") is True
        assert "series" not in it, "未变就不回序列（省 ~30KB/格）"
        assert it["quote"]["price"] == 10.0, "实时价仍要回（价格标签照常刷新）"
        assert it["dma"] is not None or "dma" in it, "均线/支撑压力仍回（前端 tooltip 要用）"

    def test_帧自带sig(self):
        """`_mw_compute` 必须给帧打 sig（末根时间|收|根数，与单图同一约定）。"""
        py = (_ROOT / "gui.py").read_text(encoding="utf-8")
        assert '"sig": _sig,' in py
        assert '_sig = f"{_dates_out[-1] if _dates_out else \'\'}|{_close_last:.3f}|{len(_dates_out)}"' in py

    def test_different_sig_sends_full_series(self, monkeypatch):
        api = gui.Api()
        fr = _frame(120)
        fr["sig"] = "d119|2.000|120"
        monkeypatch.setattr(api, "_mw_frame", lambda c, period="min5": (fr, "fresh"))
        r = api.load_multi_chart(["600000"], "min5", 90, {"600000": "stale-sig"})
        it = r["items"][0]
        assert "unchanged" not in it and it["series"]["dates"]


class TestQuietTtl:
    """2026-11-05：非交易时段分钟帧 TTL 放宽到 60s（盘前/盘后数据不变，4.6s 一次纯白烧）。"""

    def test_TTL放宽后旧帧仍判fresh(self, monkeypatch):
        import time as _t
        api = gui.Api.__new__(gui.Api)
        key = "600000|min5"
        gui._MW_CACHE.clear()
        gui._MW_CACHE[key] = (_t.time() - 10.0, _frame(30))   # 10s 前：交易时段已过期
        monkeypatch.setattr(api, "_mw_worker", lambda *a, **k: None)
        monkeypatch.setattr(gui, "_mw_ttl_now", lambda: 60.0)   # 盘前/盘后
        _fr, st = api._mw_frame("600000", "min5")
        assert st == "fresh", "盘前 10s 的帧应判新鲜（quiet TTL 60s）"
        monkeypatch.setattr(gui, "_mw_ttl_now", lambda: 4.6)    # 交易时段
        _fr, st = api._mw_frame("600000", "min5")
        assert st == "stale", "交易时段 10s 已超 4.6s ⇒ stale（后台刷新）"
        gui._MW_CACHE.clear()
        gui._MW_RUN.clear()

    def test_竞价窗口用10s(self, monkeypatch):
        """09:15-09:30：价格在动、K 线不动 ⇒ 既不是 4.6s 也不是 60s。"""
        import core.hunter_poll as hp
        monkeypatch.setattr(hp, "in_trading_hours", lambda now: False)
        class _Now:
            @staticmethod
            def strftime(fmt):
                return "09:20"
        monkeypatch.setattr(gui, "datetime", type("D", (), {
            "now": staticmethod(lambda: _Now)}))
        assert gui._mw_ttl_now() == gui._MW_TTL_AUCTION


class TestWebViewNoThrottle:
    def test_关闭遮挡_后台节流(self):
        py = (_ROOT / "gui.py").read_text(encoding="utf-8")
        assert "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS" in py
        for flag in ("--disable-background-timer-throttling",
                     "--disable-renderer-backgrounding",
                     "--disable-backgrounding-occluded-windows"):
            assert flag in py, flag


class TestDma30_60:
    """2026-11-05 owner：「多窗口里如果上方有 30 日线/60 日线也要显示，前提是离得很近，
    不要影响现在的显示比例」。"""

    def test_后端均线包包含30和60(self, monkeypatch):
        import pandas as pd
        import core.chart_cache as cc
        api = gui.Api.__new__(gui.Api)
        api._dma_memo = {}
        dates = pd.date_range("2026-01-01", periods=100).strftime("%Y-%m-%d").tolist()
        df = pd.DataFrame({"date": dates, "close": [10.0 + i * 0.01 for i in range(100)]})
        monkeypatch.setattr(cc, "load_daily_display", lambda c: df)
        d = api._daily_ma_bundle("600000", 11.0)
        for k in ("ma5", "ma10", "ma20", "ma30", "ma60"):
            assert k in d, k
        # 口径 = (前 n-1 个完成日收盘 + 现价)/n
        basis = [10.0 + i * 0.01 for i in range(100)]
        assert d["ma30"] == pytest.approx(round((sum(basis[-29:]) + 11.0) / 30, 3))

    def test_前端只在价格区间内画(self):
        assert '"ma30"' in _JS and '"ma60"' in _JS
        assert "dma[d.key]" in _JS, "五条日线按 key 统一取"
        assert "_near(d.y)" in _JS, "MA30/60 只在可见价格区间内才画"
        assert "y >= _lo * 0.995 && y <= _hi * 1.005" in _JS
        # 右列标签（与左侧 MA5/10/20 分开，贴近时不叠）
        assert 'pos: "insideEndTop"' in _JS and 'pos: "insideEndBottom"' in _JS


class TestFrontendRenderSkip:
    """2026-11-05：数据/缩放/画框都没变就不 setOption（否则 6 格 × 4s 全量重建）。"""

    def test_渲染签名与瘦帧(self):
        assert "let mwSig = [];" in _JS
        assert "function _mwSigOf(it, i)" in _JS
        assert "if (_sig === mwSig[i]) return;" in _JS
        assert "if (it.unchanged) return;" in _JS, "后端瘦帧（未变）直接跳过重画"
        assert "let mwFrameSig = {};" in _JS
        assert "_known[c] = mwFrameSig[c]" in _JS


class TestFrontend:
    def test_button_on_ob_panel(self):
        # 2026-10-26：按钮改成优先开**独立原生窗口**，不可用才退回浮层（见 openMultiWatchWindow）
        assert "openMultiWatchWindow()" in _HTML
        assert "多窗口看盘" in _HTML

    def test_window_is_3x3_float(self):
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
        assert "grid-template-rows: repeat(3" in _CSS
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
        # 码归一：`sh000001` 原样保留（指数），裸 6 位当个股
        assert "function _mwNormCode(raw)" in _JS
        assert r"/^(sh|sz|bj)(\d{6})$/" in _JS

    def test_supports_major_indices(self):
        """owner 2026-10-21：多窗口要能选各大指数。

        ⚠️ 指数必须带市场前缀：裸 `000001` 是**平安银行**，不是上证指数
        ⇒ `mwPick` 的老解析（取第一个 6 位数）会把 `sh000001` 剥成平安银行。
        """
        assert "const MW_INDEXES = [" in _JS
        for c in ("sh000001", "sz399001", "sz399006", "sh000300", "sh000905",
                  "sh000016", "sh000688", "sh000852", "bj899050"):
            assert f'code: "{c}"' in _JS, f"缺指数 {c}"
        assert '<optgroup label="📈 指数">' in _JS
        assert '<optgroup label="体检持仓">' in _JS

    def test_cells_show_name_not_code(self):
        """owner：窗口里不要显示代码，直接显示名字。下拉项文案 = 名称（取不到才回退代码）。"""
        assert "${esc(h.name || h.code)}</option>" in _JS

    def test_gap_and_daily_ma_drawn(self):
        """owner：① 缺口没显示 ② 5/10/20 日均线没显示。"""
        # 缺口与单图共用同一个函数，口径不会漂
        assert "function buildGapAreas(ohlc, dates, lookbackDays, minPct, maxN)" in _JS
        # 2026-10-26 单图窗口从 15 天放宽到 250 天（默认视窗是近 6 月）
        assert "buildGapAreas(period.ohlc, period.dates, 250)" in _JS
        assert "buildGapAreas(s.ohlc, s.dates, 15, 0.003, 8)" in _JS   # 九宫格
        # 2026-10-28 起九宫格的 markArea 还要装「画框提醒」
        # （2026-11-02：密集成交区改成右侧直方图 custom series，不再进 markArea）
        assert "markArea: (gaps.length || _mwBoxes2.length)" in _JS
        # 日线均线三条（颜色与单图的「日线MA5/10/20」一致）
        assert '"日MA5"' in _JS and '"日MA10"' in _JS and '"日MA20"' in _JS
        assert "markLine: (_lines.length || _vpLine || _ma5Line)" in _JS
        assert '"#58a6ff"' in _JS and '"#bc8cff"' in _JS and '"#2dd4bf"' in _JS

    def test_realtime_polling(self):
        assert "MW_PERIOD_MS = 4000" in _JS
        assert "mwTimer = setInterval(mwTick, MW_PERIOD_MS)" in _JS
        # 2026-10-21 起每格画 96 根（两个交易日）；更早是 48 根
        assert "MW_TAIL = 96" in _JS
        assert '"load_multi_chart", codes, mwPeriod, MW_TAIL' in _JS
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
        src = inspect.getsource(gui.Api._mw_compute)
        assert "_fetch_min_df" in src
        assert "tail(120)" in src
        assert "_calc_ma_and_indicators" not in src, "轻量帧不该跑整套指标"
        assert "_calc_fibonacci" not in src, "轻量帧不该算黄金分割"

    def test_multi_does_not_use_single_chart_cache(self):
        import inspect
        src = inspect.getsource(gui.Api.load_multi_chart)
        assert "_mw_frame" in src
        assert "_chart_live_frame" not in src, "别再复用单图的深档帧"


class TestWindowMaxMin:
    """owner 2026-10-21：多窗口看盘要支持最大化与最小化。"""

    def test_buttons_present(self):
        assert 'id="mwMaxBtn"' in _HTML
        assert 'id="mwMinBtn"' in _HTML
        assert "mwToggleMax()" in _HTML and "mwToggleMin()" in _HTML

    def test_max_saves_and_restores_geometry(self):
        """最大化要**记住原来的位置尺寸**，再点还原回去（不能回到默认位置）。"""
        assert "let mwGeomSaved = null;" in _JS
        assert "mwGeomSaved = { left: r.left + \"px\"" in _JS
        assert "win.classList.add(\"mw-max\")" in _JS
        assert "win.classList.remove(\"mw-max\")" in _JS
        # 最大化时禁用拖拽（否则会把全屏窗拖出屏幕）
        assert 'if (win.classList.contains("mw-max")) return;' in _JS
        # 最大化时禁用缩放手柄
        assert ".mw-win.mw-max { resize: none; }" in (_ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_minimize_keeps_titlebar(self):
        assert "grid.style.display = \"none\"" in _JS
        assert "mwMinH = win.style.height" in _JS
        # 还原时把高度放回去
        assert 'win.style.height = mwMinH || "88vh";' in _JS

    def test_resize_guard_when_hidden(self):
        """最小化时格子 display:none ⇒ 尺寸为 0，不能拿 0 去 resize（会画成空画布）。"""
        assert "if (el && el.clientWidth && el.clientHeight) i.resize();" in _JS

    def test_browser_resize_refills_when_maxed(self):
        assert 'win.classList.contains("mw-max")' in _JS
        assert "window.innerWidth - 16" in _JS


class TestMacdPanel:
    """owner 2026-10-21：多窗口显示要有 MACD。"""

    def test_light_frame_has_macd(self):
        """后端轻量帧必须带 MACD，且**在完整 340 根上算完再截尾** ——
        EMA12/26 的头几十根严重受种子影响，只喂 120 根算出来与单图对不上。"""
        import inspect
        src = inspect.getsource(gui.Api._mw_compute)
        assert "_ema_macd" in src
        assert "df.tail(120)" in src
        # 先算（用 _closes_all）再截尾
        assert "_closes_all" in src and "_cut" in src

    def test_ema_macd_matches_main_chart(self):
        """与单图 `_calc_ma_and_indicators` 的 MACD 同口径（EMA12/26/9，hist=(dif-dea)×2）。"""
        import pandas as pd
        closes = [10.0, 10.2, 10.1, 10.4, 10.3, 10.6, 10.5, 10.8, 10.7, 11.0] * 6
        dif, dea, hist = gui._ema_macd(closes)
        assert len(dif) == len(dea) == len(hist) == len(closes)
        for d, e, h in zip(dif, dea, hist):
            assert h == pytest.approx((d - e) * 2.0, rel=1e-9)

    def test_frontend_draws_macd_panel(self):
        assert 's.macd || {}' in _JS
        assert 'name: "MACD柱"' in _JS
        assert 'name: "DIF"' in _JS and 'name: "DEA"' in _JS
        # 第三个网格（主图/成交额/MACD）
        assert 'gridIndex: 2' in _JS
        # 配色与单图一致
        assert '"#3987e5"' in _JS and '"#d55181"' in _JS


class TestNineCells:
    """owner 2026-10-21：多窗口增加到 9 宫格。"""

    def test_nine_cells(self):
        assert "const MW_N = 9;" in _JS
        assert "grid-template-rows: repeat(3" in (_ROOT / "web" / "style.css").read_text(encoding="utf-8")

    def test_legacy_six_codes_are_kept_not_dropped(self):
        """老版本 localStorage 存的是 6 格 ⇒ 升级到 9 格时**不能把用户选的全丢掉**：
        截到 9 后空位用体检持仓里还没选过的票补齐。"""
        assert "saved = saved.slice(0, MW_N)" in _JS
        assert "空位依次用体检持仓里还没选过的票补齐" in _JS


class TestCrosshair:
    """owner 2026-10-21：多窗口看盘要十字光标。"""

    def test_crosshair_and_linked_grids(self):
        assert 'axisPointer: { type: "cross"' in _JS
        # 同一格的三窗共享竖线（K线/成交额/MACD 对齐）；
        # 2026-11-02：显式列 0/1/2 —— 隐藏轴 3 是密集区直方图专用，不参与联动
        assert "axisPointer: { link: [{ xAxisIndex: [0, 1, 2] }]" in _JS

    def test_tooltip_shows_bar_details(self):
        assert 'trigger: "axis"' in _JS
        assert "formatter: (ps) => {" in _JS
        assert "成交额 <b>${amt}</b>" in _JS
        assert "DIF ${dif}" in _JS and "DEA ${dea}" in _JS
        # 小格子里弹窗要夹在图内，别飘到隔壁格
        assert "confine: true" in _JS


class TestSupportResistance:
    """owner 2026-10-21：多窗口要把支撑位/压力位画出来。"""

    def _fake_daily(self, n=260):
        import pandas as pd
        base = [10.0 + (i % 20) * 0.05 for i in range(n)]
        return pd.DataFrame({
            "date": pd.date_range("2025-01-01", periods=n, freq="D"),
            "open": base, "high": [x * 1.01 for x in base],
            "low": [x * 0.99 for x in base], "close": base,
            "volume": [1e5] * n,
        })

    def test_levels_are_computed_offline(self, monkeypatch):
        import core.chart_cache as cc
        api = gui.Api()
        monkeypatch.setattr(cc, "load_daily_display", lambda code: self._fake_daily())
        monkeypatch.setattr(api, "_lvl_memo", {}, raising=False)
        lv = api._daily_levels("600000")
        assert lv is not None and isinstance(lv.get("supports"), list)
        assert len(lv["supports"]) <= 2 and len(lv["resistances"]) <= 2
        for v in lv["supports"] + lv["resistances"]:
            assert isinstance(v, float) and v == v      # 不能是 NaN

    def test_short_history_returns_none(self, monkeypatch):
        """历史不足 30 根 ⇒ 直接判无，别拿 NaN 均线去算。"""
        import core.chart_cache as cc
        api = gui.Api()
        monkeypatch.setattr(cc, "load_daily_display", lambda code: self._fake_daily(10))
        monkeypatch.setattr(api, "_lvl_memo", {}, raising=False)
        assert api._daily_levels("600000") is None

    def test_frontend_draws_levels(self):
        # 2026-10-28 起多窗口也要画「画框提醒」⇒ 入参多了 code
        assert "function _mwOption(s, dma, levels, trades, zoom, code)" in _JS
        assert '`${up ? "压" : "支"} ${Number(y).toFixed(2)}`' in _JS
        # 支撑绿、压力红（与单图一致）；标签在右侧（日线MA 在左，两拨不打架）
        assert '"#f85149", type: "dashed"' in _JS and '"#3fb950", type: "dashed"' in _JS
        assert 'position: up ? "insideEndTop" : "insideEndBottom"' in _JS
        assert "it.dma, it.levels, it.trades" in _JS


class TestKlineSearchFuzzy:
    """owner 2026-10-21：K线速查搜「领益制造」0 条 —— 实为「领益智造」，打错一个字。

    精确 / 前缀 / 包含 三种匹配 + 腾讯 smartbox **全是字面匹配** ⇒ 错一个字就全空。
    加一层「共同字」兜底（仅在前面全空时启用），并且**模糊结果不自动开图**。
    """

    def _api(self, monkeypatch):
        import gui as G
        api = G.Api()
        monkeypatch.setattr(api, "_instrument_names",
                            lambda: {"002600": "领益智造", "600519": "贵州茅台",
                                     "000589": "贵州轮胎", "900901": "云赛B股"})
        # 在线搜索返回空（真实环境里 smartbox 对错字同样 0 条）
        import core.market_data.tencent_provider as TP
        monkeypatch.setattr(TP, "search_instruments", lambda q, limit=12: [])
        G._SEARCH_CACHE.clear()
        return api

    def test_typo_finds_closest_name(self, monkeypatch):
        api = self._api(monkeypatch)
        r = api.search_instrument("领益制造")
        assert r["results"], "错一个字也应该能兜到候选"
        assert r["results"][0]["code"] == "002600"
        assert r["results"][0]["note"] == "模糊匹配"
        assert "原词没搜到" in r["note"]

    def test_exact_name_still_exact(self, monkeypatch):
        api = self._api(monkeypatch)
        r = api.search_instrument("领益智造")
        assert r["results"][0]["code"] == "002600"
        assert r["results"][0]["note"] == ""      # 精确命中不带模糊标记

    def test_fuzzy_does_not_auto_open(self):
        """模糊结果必须让用户点一下确认 —— 自动开图等于把猜的结果当用户意图。"""
        assert "const _isFuzzy = x =>" in _JS
        assert "if (rows.length === 1 && !_isFuzzy(rows[0]))" in _JS


class TestTradeMarkersInCells:
    """owner 2026-10-21：K 线的买卖点要显示在 5 分钟线上（九宫格每格也要）。"""

    def test_backend_slices_trades_to_window(self):
        import inspect
        src = inspect.getsource(gui.Api._mw_trades)
        assert "_chart_trades" in src
        assert "dates[0]" in src          # 只取窗口内的
        assert "_out[-200:]" in src       # 上限，九宫格 9 只也不至于把包撑大
        src2 = inspect.getsource(gui.Api.load_multi_chart)
        assert '"trades"' in src2

    def test_frontend_buckets_same_as_main_chart(self):
        # 与单图同口径：成交落进 (bar_t - 5min, bar_t]
        assert "function _mwOption(s, dma, levels, trades, zoom, code)" in _JS
        assert "_be[bi] - _pMin >= tm" in _JS
        # 买=红▲托在 low 下、卖=绿▼压在 high 上（与单图同色同形）
        assert 'color: "#f85149", borderColor: "#fff"' in _JS
        assert 'color: "#3fb950", borderColor: "#fff"' in _JS
        assert "symbolRotate: 180" in _JS
        assert "it.dma, it.levels, it.trades" in _JS


class TestVolumePaneLegibility:
    """2026-11-02 owner：「多窗口显示中，成交量的显示太小很难看清楚」。

    原来三窗 52% / **10%** / 18%，九宫格里成交额窗只有 ~25px（3×3）/~39px（3×2），
    柱子几乎糊成一条线。守四条：
      ① 成交额窗高度 ≥16%（约 45~70px），主图让出空间但不低于 40%；
      ② 成交额 y 轴不再整条空白：留 1 个刻度并显示**最大刻度值**（带亿/万单位）；
      ③ 柱子更亮更宽 + 从 0 起算（`scale:false`），量能大小不被"缩放"骗；
      ④ 左上角有「最新一根」的读数（不悬停也能看到当下量能）。
    """

    def _grid(self):
        i = _JS.index("grid: [{ left: 40, right: _mwVpRight, top: 3, height:")
        return _JS[i:i + 260]

    def test_成交额窗变高主图仍占大头(self):
        g = self._grid()
        assert 'height: "44%"' in g
        assert 'top: "49%", height: "20%"' in g
        assert 'top: "71%", height: "18%"' in g

    def test_y轴显示最大刻度且按单位格式化(self):
        i = _JS.index("scale: false, gridIndex: 1, splitNumber: 1")
        seg = _JS[i:i + 400]
        assert "axisLabel: { show: true" in seg
        assert "formatter: _volFmt" in seg
        # 亿 / 手 两种单位都要认
        assert '"亿"' in _JS and '"万"' in _JS

    def test_柱子更亮更宽且从0起算(self):
        assert "rgba(248,81,73,.92)" in _JS and "rgba(63,185,80,.92)" in _JS
        i = _JS.index('{ name: "量能", type: "bar"')
        assert "barMaxWidth: 12" in _JS[i:i + 200]

    def test_最新一根读数角标(self):
        assert 'text: `${amtYi ? "额" : "量"}' in _JS
        i = _JS.index("graphic: (() => {")
        assert 'top: "49.4%"' in _JS[i:i + 700]


class TestLayout2x2:
    """2026-11-03 owner：多窗口要支持 2×2（2 列 × 2 行，4 格）。"""

    def test_前端支持2x2(self):
        assert 'mwLayout === "2x2" ? 4 : 9' in _JS
        assert "lay === \"2x2\"" in _JS
        assert 'v === "3x2" || v === "3x3" || v === "2x2"' in _JS
        assert 'grid.classList.toggle("mw-c2", mwLayout === "2x2")' in _JS

    def test_按钮与样式(self):
        assert 'id="mwLay2x2"' in _HTML
        css = (_ROOT / "web" / "style.css").read_text(encoding="utf-8")
        assert ".mw-grid.mw-c2" in css


class TestMainChartMacdFlip:
    """2026-11-03 owner：「K线图也要支持 MACD 第一根红标记」——与多窗口同一口径。"""

    def test_按可视窗口找翻红首根(self):
        assert "const _macdHist = ((period.macd || {}).hist) || [];" in _JS
        assert "Math.floor(zStart / 100 * (_mN - 1))" in _JS
        assert "_a <= 0 && _b > 0" in _JS

    def test_首根亮红加白边与三角(self):
        assert "data: _macdHist.map((v, i) => ({" in _JS
        assert 'color: "#ff3b30", borderColor: "#ffffff"' in _JS
        assert "symbol: \"triangle\", symbolSize: 10" in _JS
        assert "xAxis: _mFlipLast" in _JS
        assert 'formatter: "翻红"' in _JS


class TestPeriodSwitch:
    """2026-11-04 owner：窗口看盘可选 5 / 30 / 60 分钟。"""

    def test_前端周期按钮与切换(self):
        for p in ("min5", "min10", "min30", "min60"):
            assert f'id="mwPer{p}"' in _HTML, p
        assert "function mwSetPeriod(p)" in _JS
        assert 'apiCall("load_multi_chart", codes, mwPeriod, MW_TAIL, _known)' in _JS
        assert "mwZoom = [];" in _JS.split("function mwSetPeriod")[1][:600]
        assert 'id="mwSubTitle"' in _HTML

    def test_周期写进后端记忆(self):
        assert 'apiCall("save_multi_watch", null, null, p)' in _JS
        assert "MW_LS_PERIOD" in _JS
        assert "_mwRemotePeriod" in _JS


class TestPeriodBackend:
    def test_load_multi_chart_透传周期(self):
        import gui
        api = gui.Api()
        seen = {}

        def _fake_frame(code, period="min5"):
            seen[code] = period
            return {"dates": [], "ohlc": [], "volume": [], "amount": None,
                    "ma5": [], "macd": {"dif": [], "dea": [], "hist": []}, "quote": {}}, "fresh"
        api._mw_frame = _fake_frame
        r = api.load_multi_chart(["600000"], "min30", 96)
        assert seen["600000"] == "min30"
        assert r["period"] == "min30"
        r2 = api.load_multi_chart(["600000"], "bogus", 96)
        assert r2["period"] == "min5", "非法周期回落 5 分钟"

    def test_周期记忆往返(self, tmp_path):
        import gui
        api = gui.Api()
        api._MW_STATE_FP = tmp_path / "mw.json"
        api.save_multi_watch(["600000"], "3x2", "min60")
        d = api.load_multi_watch()
        assert (d["codes"], d["layout"], d["period"]) == (["600000"], "3x2", "min60")
        api.save_multi_watch(None, None, "bogus")
        assert api.load_multi_watch()["period"] == "min60", "非法周期不能覆盖旧值"


class TestDailyMaFallback:
    """2026-11-06 owner：多窗口跑一段时间后很多票没有日均线。

    根因：盘中日线缓存写入**当天半成品根** ⇒ `load_daily_display` 的
    `is_unconfirmed_today_bar` 闸返回 None ⇒ `_daily_ma_bundle/_daily_levels` 变 None。
    这些计算只用已完成日 ⇒ 必须退回原始行，不能被那道闸卡死。
    """

    def test_盘中半成品根不再让日均线消失(self, monkeypatch):
        import datetime as _dt
        import core.chart_cache as cc
        api = gui.Api.__new__(gui.Api)
        api._dma_memo = {}
        rows = []
        d0 = _dt.date(2026, 6, 1)
        for i in range(120):
            rows.append({"date": str(d0 + _dt.timedelta(days=i)), "open": 10.0 + i * 0.01,
                         "high": 10.2 + i * 0.01, "low": 9.8 + i * 0.01,
                         "close": 10.0 + i * 0.01, "volume": 1e6})
        monkeypatch.setattr(cc, "load_daily_display", lambda c: None)   # 模拟 display 闸返回 None
        monkeypatch.setattr(gui, "_daily_rows_before_today", lambda c: rows)
        d = api._daily_ma_bundle("600584")
        assert d and d["ma5"] > 0 and d["ma60"] > 0
