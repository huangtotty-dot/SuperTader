# -*- coding: utf-8 -*-
"""当日「均线回站（5/10/20/30/60 日线）」+ **突破时刻** 的离线单测（2026-10-23 owner）。

owner：「建仓信号扫描 / 30 分钟风险体检 / 异常监控池 里，某只票**当日**刚刚突破 5 日线
10 日线或者 20/30/60 日线，请标注出突破的时间」。

口径（与 `core/ma_reclaim.ma5_state` 逐字同源，只是周期可变）：
  · 昨 MA(p) = mean(截至昨日的 p 根收盘)（含昨日）
  · 今 MA(p) = (前 p-1 根收盘 + 今日现价) / p
  · 回站 = 昨收 < 昨MA(p) 且 现价 > 今MA(p)
  · 突破时刻 = 今日 5min bar 里**第一次收盘价** > 该时刻 MA(p) 的那根（"HH:MM"）
    ⚠️ 化简后阈值恒等于 `mean(前 p-1 根收盘)`（今日价同时在两边），日内不漂移 —— 本文件钉住这点。

铁律：全离线，全部合成数据。
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

from core.ma_reclaim import (  # noqa: E402
    RECLAIM_PERIODS, reclaim_first_time, reclaim_state, reclaim_threshold, reclaim_today,
)


def _hist(n=60, last=9.0, base=10.0):
    """前 n-1 天 base、昨天 last。默认 10.0…10.0, 9.0。

    ⚠️ 阈值 = mean(前 p-1 根) —— **包含昨天**，所以 p=5 的阈值是 9.75（不是 10.0）。
    """
    return [base] * (n - 1) + [last]


def _bars(points, day="2026-10-23"):
    return pd.DataFrame([{"time": f"{day} {hm}:00", "close": c} for hm, c in points])


class TestReclaimState(unittest.TestCase):
    def test_回站判定与均线口径(self):
        closes = [10, 10, 10, 10, 9, 10.5]
        s = reclaim_state(closes, 5)
        self.assertAlmostEqual(s["ma_prev"], 9.8)          # mean(10,10,10,10,9)
        self.assertAlmostEqual(s["ma"], 9.9)               # (10+10+10+9+10.5)/5
        self.assertTrue(s["reclaim"])
        self.assertAlmostEqual(s["dev_pct"], (10.5 - 9.9) / 9.9 * 100)

    def test_昨收不在均线下方就不算回站(self):
        # 昨天 10.2 在 MA5(=10.04) 上方 ⇒ 今天涨到 10.5 不算"回站"
        s = reclaim_state([10, 10, 10, 10, 10.2, 10.5], 5)
        self.assertFalse(s["reclaim"])

    def test_现价没站上也不算(self):
        s = reclaim_state([10, 10, 10, 10, 9, 9.7], 5)     # 阈值 9.75
        self.assertFalse(s["reclaim"])
        self.assertFalse(s["above"])

    def test_数据不足返回None(self):
        self.assertIsNone(reclaim_state([1, 2, 3], 5))
        self.assertIsNone(reclaim_state(None, 5))


class TestReclaimThreshold(unittest.TestCase):
    def test_阈值就是前p减1根收盘均值(self):
        h = list(range(1, 21))                              # 1..20
        self.assertAlmostEqual(reclaim_threshold(h, 5), sum(range(17, 21)) / 4)
        self.assertAlmostEqual(reclaim_threshold(h, 20), sum(range(2, 21)) / 19)

    def test_数据不足(self):
        self.assertIsNone(reclaim_threshold([1, 2], 5))


class TestReclaimFirstTime(unittest.TestCase):
    def test_首根站上的时刻(self):
        h = _hist()                                          # p=5 阈值 = mean(10,10,10,9) = 9.75
        self.assertAlmostEqual(reclaim_threshold(h, 5), 9.75)
        bars = _bars([("09:35", 9.50), ("09:40", 9.70), ("09:45", 9.80), ("09:50", 10.20)])
        self.assertEqual(reclaim_first_time(h, 5, bars["time"], bars["close"]), "09:45")

    def test_开盘就在线上(self):
        h = _hist()
        bars = _bars([("09:35", 9.90), ("09:40", 9.60)])
        self.assertEqual(reclaim_first_time(h, 5, bars["time"], bars["close"]), "09:35")

    def test_全天没站上(self):
        h = _hist()
        bars = _bars([("09:35", 9.5), ("09:40", 9.7)])
        self.assertIsNone(reclaim_first_time(h, 5, bars["time"], bars["close"]))

    def test_用收盘价而不是最高价(self):
        """盘中冲高回落不算站上（口径与日线标签一致）。"""
        h = _hist()                                          # 阈值 9.75
        bars = pd.DataFrame([{"time": "2026-10-23 09:35:00", "open": 10.5, "high": 10.6,
                              "low": 9.4, "close": 9.60, "volume": 1.0}])
        self.assertIsNone(reclaim_first_time(h, 5, bars["time"], bars["close"]))


class TestReclaimToday(unittest.TestCase):
    def test_多周期阈值各不相同(self):
        """同一根 bar 对不同周期可能「已站上」或「还没」——阈值 = mean(前 p-1 根)。

        last=9.0 时：p5=9.75 / p10=9.888… / p20=9.947… / p30=9.966… / p60=9.983…
        """
        h = _hist()
        bars = _bars([("09:35", 9.70), ("09:45", 9.90), ("10:15", 10.00)])
        out = reclaim_today(h, bars)
        self.assertEqual(out[5]["time"], "09:45")
        self.assertEqual(out[10]["time"], "09:45")
        self.assertEqual(out[20]["time"], "10:15")
        self.assertEqual(out[60]["time"], "10:15")
        self.assertAlmostEqual(out[5]["ma"], (10 + 10 + 10 + 9 + 10.0) / 5)

    def test_只算指定周期(self):
        h = _hist()
        bars = _bars([("09:35", 9.70), ("09:45", 10.00)])   # 10.00 > 两条阈值(9.75/9.947)
        self.assertEqual(sorted(reclaim_today(h, bars, periods=(5, 20))), [5, 20])

    def test_昨日在均线上方的周期不出现(self):
        """近期高的那段只抬得动短均线：MA5 昨值 10.4（昨收 10.0 在其下方 ⇒ 可回站），
        MA10/20 昨值 9.7/9.35（昨收在**上方** ⇒ 不可能回站）。"""
        h = [9.0] * 55 + [10.5] * 4 + [10.0]
        bars = _bars([("09:35", 10.30), ("10:00", 10.60)])
        out = reclaim_today(h, bars, periods=(5, 10, 20))
        self.assertIn(5, out)
        self.assertEqual(out[5]["time"], "10:00")            # 阈值 = mean(10.5,10.5,10.5,10.0) = 10.25
        self.assertNotIn(10, out)
        self.assertNotIn(20, out)

    def test_冲高后又跌回则不算(self):
        h = _hist()                                          # 阈值 9.75
        bars = _bars([("09:35", 10.2), ("14:55", 9.70)])     # 收盘价回到线下
        self.assertEqual(reclaim_today(h, bars), {})

    def test_无分钟数据(self):
        self.assertEqual(reclaim_today(_hist(), pd.DataFrame()), {})
        self.assertEqual(reclaim_today(_hist(), None), {})

    def test_周期常量(self):
        self.assertEqual(RECLAIM_PERIODS, (5, 10, 20, 30, 60))


class TestBackendCompute(unittest.TestCase):
    """`t_gui.Api._ma_reclaim_compute`：本地日线（**不含当日**）+ 今日 5min → 三张表共用的载荷。"""

    def _daily_file(self, tmpdir):
        import json
        from datetime import date, timedelta
        d0 = date(2026, 10, 22)
        days = []
        d = d0
        while len(days) < 60:                     # 往前取 60 个工作日
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        days.sort()
        rows = [{"date": x.strftime("%Y-%m-%d"),
                 "open": 10.0, "high": 10.1, "low": 9.9,
                 "close": (9.0 if i == len(days) - 1 else 10.0), "volume": 1.0}
                for i, x in enumerate(days)]
        (tmpdir / "600000.json").write_text(
            json.dumps({"rows": rows, "date": "2026-10-22", "forming": [], "saved_at": "x"}),
            encoding="utf-8")

    def test_日线基准加今日分钟(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        import t_gui

        tmpdir = Path(tempfile.mkdtemp())
        self._daily_file(tmpdir)
        bars = _bars([("09:35", 9.50), ("09:45", 9.90), ("15:00", 10.00)])
        with mock.patch.object(t_gui, "_DAILY_KLINE_DIR", tmpdir), \
                mock.patch.object(t_gui, "_fetch_min_bars_tencent",
                                  lambda code, freq: bars):
            out = t_gui.Api()._ma_reclaim_compute("600000", "2026-10-23")
        self.assertEqual(out["basis"], "2026-10-22", "均线基准日要如实带出（缓存陈旧时前端 tooltip 会标）")
        ma = out["ma"]
        self.assertEqual(sorted(ma), [5, 10, 20, 30, 60])
        self.assertEqual(ma[5]["time"], "09:45")
        self.assertEqual(ma[60]["time"], "15:00")
        self.assertAlmostEqual(ma[5]["ma"], (10 + 10 + 10 + 9 + 10.0) / 5, places=3)

    def test_日线缓存停在几天前时用分钟补齐(self):
        """2026-10-23 owner 实报（深圳华强 002056）：本地日线缓存停在 10-20，
        昨天(10-22)其实是**跌破** 5 日线的、今天才回站 ⇒ 不补基准就永远判不出来。

        合成：缓存 60 天（10-20 收 10.5）→ 若只用缓存，昨收 10.5 在 MA5(10.1) 上方 ⇒ 判不出回站；
        分钟里 10-21/10-22 各收 9.6 ⇒ 补齐后昨收 9.6 < MA5(9.94) ⇒ 今天 10:00 站上时应命中 5 日线。
        """
        import json, tempfile
        from pathlib import Path
        from datetime import date, timedelta
        from unittest import mock
        import t_gui

        tmpdir = Path(tempfile.mkdtemp())
        days, d = [], date(2026, 10, 20)
        while len(days) < 60:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        days.sort()
        rows = [{"date": x.strftime("%Y-%m-%d"), "close": 10.0, "open": 10.0,
                 "high": 10.1, "low": 9.9, "volume": 1.0} for x in days]
        rows[-1]["close"] = 10.5                       # 10-20
        (tmpdir / "600000.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
        bars = pd.DataFrame([
            {"time": "2026-10-21 15:00:00", "close": 9.6},
            {"time": "2026-10-22 15:00:00", "close": 9.6},
            {"time": "2026-10-23 09:35:00", "close": 9.90},
            {"time": "2026-10-23 10:00:00", "close": 10.20},
        ])
        with mock.patch.object(t_gui, "_DAILY_KLINE_DIR", tmpdir), \
                mock.patch.object(t_gui, "_fetch_min_bars_tencent", lambda code, freq: bars):
            out = t_gui.Api()._ma_reclaim_compute("600000", "2026-10-23")
        self.assertEqual(out["basis"], "2026-10-22", "基准日必须用分钟补齐到昨天")
        self.assertIn(5, out["ma"], "补齐后应判出 5 日线回站")
        self.assertEqual(out["ma"][5]["time"], "10:00")
        self.assertIs(out["above5"], True)

    def test_昨收在均线上方时不出回站(self):
        """昨收在所有均线上方 ⇒ 不可能回站 ⇒ `ma` 为空（但仍会算出 above5 给行底色用）。"""
        import json, tempfile
        from pathlib import Path
        from datetime import date, timedelta
        from unittest import mock
        import t_gui

        tmpdir = Path(tempfile.mkdtemp())
        d0, days = date(2026, 10, 22), []
        d = d0
        while len(days) < 60:
            if d.weekday() < 5:
                days.append(d)
            d -= timedelta(days=1)
        days.sort()
        rows = [{"date": x.strftime("%Y-%m-%d"), "close": 10.0, "open": 10.0,
                 "high": 10.1, "low": 9.9, "volume": 1.0} for x in days]   # 昨天也在均线上方
        (tmpdir / "600000.json").write_text(json.dumps({"rows": rows}), encoding="utf-8")
        called = []
        bars = _bars([("09:35", 10.10), ("15:00", 10.10)])
        with mock.patch.object(t_gui, "_DAILY_KLINE_DIR", tmpdir), \
                mock.patch.object(t_gui, "_fetch_min_bars_tencent",
                                  lambda code, freq: called.append(code) or bars):
            out = t_gui.Api()._ma_reclaim_compute("600000", "2026-10-23")
        self.assertTrue(called, "分钟取数现在总会发（要先补齐日线缓存缺的天）")
        self.assertEqual(out["ma"], {}, "昨收在所有均线上方 ⇒ 不该有回站")
        self.assertIs(out["above5"], True, "above5 仍要算出来（行底色用它）")


class TestKickDedupe(unittest.TestCase):
    """2026-10-23 实盘缺陷：预热队列只按「在不在队列里」去重，不看缓存是否新鲜，
    而建仓表每 10s 踢一次 48 只 ⇒ worker 永不停歇重算同一批，新票（持仓）排在后面等一分钟
    ⇒ owner 看到的就是「均线突破时刻更新太慢」。"""

    def setUp(self):
        import t_gui
        self.t_gui = t_gui
        self._cache = dict(t_gui._MA_RC_CACHE)
        t_gui._MA_RC_CACHE.clear()

    def tearDown(self):
        self.t_gui._MA_RC_CACHE.clear()
        self.t_gui._MA_RC_CACHE.update(self._cache)

    def _drain(self, timeout=5.0):
        import time
        t0 = time.time()
        while time.time() - t0 < timeout:
            if not self.t_gui._MA_RC_WARM["running"]:
                return
            time.sleep(0.02)

    def test_缓存新鲜时不重复排队重算(self):
        import time
        from unittest import mock
        calls = []

        def _fake(self, code, date):
            calls.append(code)
            return {"basis": "2026-10-22", "ma": {}}

        api = self.t_gui.Api()
        with mock.patch.object(self.t_gui.Api, "_ma_reclaim_compute", _fake):
            api._kick_ma_reclaim_warm(["600000", "600001"], "2026-10-23")
            self._drain()
            n1 = len(calls)
            self.assertEqual(n1, 2, "首次应算 2 只")
            for _ in range(3):                    # 模拟建仓表每 10s 踢一次
                api._kick_ma_reclaim_warm(["600000", "600001"], "2026-10-23")
                time.sleep(0.05)
        self.assertEqual(len(calls), n1, "缓存新鲜仍被重新排队重算 —— worker 会永不停歇")

    def test_失败条目用短负缓存(self):
        import time
        from core.ma_reclaim import RECLAIM_PERIODS  # noqa: F401
        rec = {"date": "2026-10-23", "ts": time.time(), "err": True, "ma": {}}
        self.assertTrue(self.t_gui._ma_rc_fresh(rec, "2026-10-23", time.time(), 6 * 3600))
        old = {"date": "2026-10-23", "ts": time.time() - 700, "err": True, "ma": {}}
        self.assertFalse(self.t_gui._ma_rc_fresh(old, "2026-10-23", time.time(), 6 * 3600),
                         "失败条目不能用 6 小时 TTL 把一列永久钉成 —")


class TestObOverlayWiring(unittest.TestCase):
    """持仓体检的覆盖层（`_ob_overlay_live`）必须真能读到缓存。

    2026-10-23 实盘缺陷：`_ma_reclaim_get(code, date, kick=True)` 的 `date` **没有默认值**，
    而覆盖层写成 `self._ma_reclaim_get(_base, kick=False)` ⇒ 每次 TypeError → 被 except 吞掉
    → 整列永远是 ⏳（前端还一直按 3s 重拉）。这类"静默降级"必须在测试里钉住。
    """

    def setUp(self):
        import t_gui
        self.t_gui = t_gui
        self._cache = dict(t_gui._MA_RC_CACHE)
        t_gui._MA_RC_CACHE.clear()

    def tearDown(self):
        self.t_gui._MA_RC_CACHE.clear()
        self.t_gui._MA_RC_CACHE.update(self._cache)

    def test_get_可省略date(self):
        """覆盖层不传 date —— 缺默认值会 TypeError，被 except 吞成 pending。"""
        self.t_gui.Api()._ma_reclaim_get("600000", kick=False)

    def test_覆盖层从缓存读到值并清掉pending标记(self):
        import time
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        self.t_gui._MA_RC_CACHE["600000"] = {
            "date": today, "ts": time.time(), "basis": "2026-10-22",
            "ma": {5: {"time": "10:35", "ma": 10.0, "dev_pct": 1.0}}}
        out = {"stocks": [{"code": "600000"}]}
        self.t_gui.Api()._ob_overlay_live(out)
        mr = out["stocks"][0]["ma_reclaim"]
        self.assertNotIn("pending", mr, "缓存里有值却还报 pending —— 覆盖层没读到")
        self.assertEqual(mr["ma"][5]["time"], "10:35")
        self.assertNotIn("ma_reclaim_pending", out, "算完了却把 pending 标记留在 payload 里")

    def test_pending标记不会粘住(self):
        """上一轮挂上的 True 必须在本轮被清掉（否则前端永远 3s 轮询）。"""
        out = {"stocks": [], "ma_reclaim_pending": True}
        self.t_gui.Api()._ob_overlay_live(out)
        self.assertNotIn("ma_reclaim_pending", out)


class TestHunterEndpoint(unittest.TestCase):
    """选股猎手：池子 2000+ 只，不能全量算 ⇒ 前端只把**展开板块当前页**的码传上来。"""

    def setUp(self):
        import t_gui
        self.t_gui = t_gui
        self._cache = dict(t_gui._MA_RC_CACHE)
        t_gui._MA_RC_CACHE.clear()

    def tearDown(self):
        self.t_gui._MA_RC_CACHE.clear()
        self.t_gui._MA_RC_CACHE.update(self._cache)

    def test_只算传入的码_去重_封顶(self):
        from unittest import mock
        kicked = []
        api = self.t_gui.Api()
        with mock.patch.object(self.t_gui.Api, "_kick_ma_reclaim_warm",
                               lambda self_, codes, date=None: kicked.extend(codes)):
            r = api.load_ma_reclaim(["600000", "600000_1", "000001"])
            self.assertEqual(sorted(r["items"]), ["000001", "600000"], "重复码要去重")
            self.assertEqual(kicked, ["600000", "000001"])
            self.assertTrue(all(v.get("pending") for v in r["items"].values()))
            r2 = api.load_ma_reclaim([f"{i:06d}" for i in range(300)])
            self.assertEqual(len(r2["items"]), 200, "单次上限 200（防前端误传全池打爆预热队列）")

    def test_空输入不炸(self):
        self.assertEqual(self.t_gui.Api().load_ma_reclaim([]), {"items": {}})


class TestFrontendWiring(unittest.TestCase):
    """三张表必须共用同一个渲染器（别再各写一份）。"""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.js = (Path(_ROOT) / "web" / "app.js").read_text(encoding="utf-8")

    def test_三张表都用同一渲染器(self):
        for fn in ("function renderPB", "function renderAbnormalPool", "function renderOB"):
            i = self.js.index(fn)
            nxt = self.js.index("\nfunction ", i + 10)
            seg = self.js[i:nxt]
            self.assertIn("maReclaimCellHtml(", seg, f"{fn} 没接入「均线突破时刻」列")

    def test_渲染器本身(self):
        i = self.js.index("function maReclaimInnerHtml")
        seg = self.js[i:i + 1200]
        for k in ("pending", "na", "badge t-long"):
            self.assertIn(k, seg)

    def test_选股猎手也接了这一列(self):
        """2026-10-23 owner：「选股猎手增加均线突破时间这一列」。
        猎手池 2000+ 只 ⇒ 只对**展开板块当前页**的码现算（`load_ma_reclaim` 端点）。"""
        i = self.js.index("function renderHunter(h)")
        nxt = self.js.index("\nfunction ", i + 10)
        seg = self.js[i:nxt]
        self.assertIn("h-mr", seg, "猎手个股行要有均线突破单元格")
        self.assertIn("maReclaimInnerHtml", seg)
        self.assertIn("function loadHunterMaReclaim", self.js)
        self.assertIn('apiCall("load_ma_reclaim"', self.js)
        # 展开板块 / 翻页重渲染后都要补一次
        self.assertGreaterEqual(self.js.count("loadHunterMaReclaim("), 3)

    def test_趋势线默认不显示(self):
        """owner 2026-10-23：K 线图「趋势线」默认关。"""
        from pathlib import Path
        html = (Path(_ROOT) / "web" / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="tgTrend" checked', html, "趋势线勾选框不能默认勾上")


if __name__ == "__main__":
    unittest.main(verbosity=2)
