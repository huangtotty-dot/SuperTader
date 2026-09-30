# -*- coding: utf-8 -*-
"""两市成交额卡片（指数板第 8 张）离线单测（2026-09-30）。

`Api._turnover_from_minutes` 是**纯计算**（无 IO），故可全离线测。守四条：

  T1 **盘中比昨日「同期」**——本模块最要紧的口径。今日是累计值，10:00 可能才走 20%，
     直接比昨日全日会显示 −80%。实测冻结 10:35 应给 +4.96%（两边都只算到 10:35），
     而非把今日部分量与昨日全量比。
  T2 **收盘后（≥15:00）比「全日」**——owner 要求：已收盘按全天成交量显示。
  T3 **今天不是交易日**（周末/节假日）也按全日比——交易日以**分钟数据里最新一天**为准，
     不用日历日期，否则周六会拿周五的部分量当"今日"。
  T4 **降级**：缺上一交易日 / 成交额为 0 → available=False，不抛。

铁律：全离线，全部合成帧，不打网络、不读 t_io。

运行：python tests/phase3/test_market_turnover.py
"""
import os
import sys
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import t_gui  # noqa: E402

CALC = t_gui.Api._turnover_from_minutes

# 一天 4 根（简化）：每根 100 亿
_BARS = [("10:00", 100e8), ("11:30", 100e8), ("14:00", 100e8), ("15:00", 100e8)]


def _leg(day, bars=_BARS, scale=1.0):
    return pd.DataFrame([{"_day": day, "_hm": hm, "amount": a * scale} for hm, a in bars])


def _two(day_cur="2026-09-30", day_prev="2026-09-29", scale_cur=1.0, scale_prev=1.0):
    return {"sh000001": _leg(day_cur, scale=scale_cur),
            "sz399106": _leg(day_prev, scale=scale_prev)}


class TestBasisIntraday(unittest.TestCase):
    """T1：盘中按昨日同期。"""

    def test_盘中只算到当前时刻(self):
        # 两条腿都是同一交易日；用同一根 day 列更贴近真实（两腿同日）
        legs = {"sh000001": pd.concat([_leg("2026-09-30", scale=1.0),
                                       _leg("2026-09-29", scale=1.0)]).reset_index(drop=True),
                "sz399106": pd.concat([_leg("2026-09-30", scale=1.0),
                                       _leg("2026-09-29", scale=1.0)]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 10, 35))
        self.assertTrue(r["available"])
        self.assertEqual(r["basis"], "同期", "盘中必须是同期口径")
        # 只含 10:00 那根，两腿各 100 亿 ⇒ 200 亿
        self.assertAlmostEqual(r["amount"] / 1e8, 200.0, places=1)
        self.assertAlmostEqual(r["amount_prev"] / 1e8, 200.0, places=1)
        self.assertAlmostEqual(r["pct"], 0.0, places=6)
        self.assertEqual(r["as_of"], "10:00")

    def test_同期口径不得拿部分量比全日(self):
        """核心回归闸：若误用全日做分母，会得到约 −50% 这种误导值。"""
        legs = {"sh000001": pd.concat([_leg("2026-09-30", scale=1.0),
                                       _leg("2026-09-29", scale=1.0)]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 10, 35))
        self.assertAlmostEqual(r["pct"], 0.0, places=6,
                               msg="同期同量应约 0%；若得 −50% 说明误用昨全日做分母")
        self.assertNotAlmostEqual(r["pct"], -50.0, places=1)

    def test_放量缩量符号(self):
        legs = {"sh000001": pd.concat([_leg("2026-09-30", scale=1.5),
                                       _leg("2026-09-29", scale=1.0)]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 10, 35))
        self.assertAlmostEqual(r["pct"], 50.0, places=4, msg="今比昨同期多五成")

    def test_午休边界取最后一根(self):
        legs = {"sh000001": pd.concat([_leg("2026-09-30"), _leg("2026-09-29")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 11, 35))     # 11:35 时上午最后一根是 11:30
        self.assertEqual(r["basis"], "同期")
        self.assertEqual(r["as_of"], "11:30")
        self.assertAlmostEqual(r["amount"] / 1e8, 200.0, places=1)


class TestBasisFullDay(unittest.TestCase):
    """T2：收盘后按全日。"""

    def _legs(self):
        return {"sh000001": pd.concat([_leg("2026-09-30"), _leg("2026-09-29")]).reset_index(drop=True),
                "sz399106": pd.concat([_leg("2026-09-30"), _leg("2026-09-29")]).reset_index(drop=True)}

    def test_15点整算全日(self):
        r = CALC(self._legs(), datetime(2026, 9, 30, 15, 0))
        self.assertEqual(r["basis"], "全日", "15:00 起应为全日")
        self.assertAlmostEqual(r["amount"] / 1e8, 800.0, places=1, msg="两腿各 4 根 ×100亿")
        self.assertEqual(r["as_of"], "15:00")

    def test_盘后算全日(self):
        r = CALC(self._legs(), datetime(2026, 9, 30, 16, 34))
        self.assertEqual(r["basis"], "全日")
        self.assertAlmostEqual(r["amount"] / 1e8, 800.0, places=1)

    def test_14点59仍是同期(self):
        r = CALC(self._legs(), datetime(2026, 9, 30, 14, 59))
        self.assertEqual(r["basis"], "同期", "15:00 之前仍属盘中")


class TestNonTradingDay(unittest.TestCase):
    """T3：今天不是交易日（周末/节假日）⇒ 按全日比，且不误当"今日"。"""

    def test_周六按全日比(self):
        # 分钟数据里最新一天是周五 09-25；当前日历是周六 09-26
        legs = {"sh000001": pd.concat([_leg("2026-09-25"), _leg("2026-09-24")]).reset_index(drop=True),
                "sz399106": pd.concat([_leg("2026-09-25"), _leg("2026-09-24")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 26, 10, 35))
        self.assertEqual(r["basis"], "全日", "非交易日不得判成盘中")
        self.assertEqual(r["day"], "2026-09-25", "交易日应取分钟数据最新一天")
        self.assertEqual(r["prev_day"], "2026-09-24")
        self.assertAlmostEqual(r["amount"] / 1e8, 800.0, places=1)

    def test_盘前按全日比(self):
        # 今天 09-30 但当前 09:00（盘前）：当日还没有 bar ⇒ 最新一天仍是 09-29
        legs = {"sh000001": pd.concat([_leg("2026-09-29"), _leg("2026-09-28")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 9, 0))
        self.assertEqual(r["basis"], "全日")
        self.assertEqual(r["day"], "2026-09-29")


class TestDegrade(unittest.TestCase):
    """T4：降级不抛。"""

    def test_缺上一交易日(self):
        legs = {"sh000001": _leg("2026-09-30")}
        r = CALC(legs, datetime(2026, 9, 30, 16, 0))
        self.assertFalse(r["available"])
        self.assertIn("上一交易日", r["error"])

    def test_成交额为0(self):
        legs = {"sh000001": pd.concat([_leg("2026-09-30", bars=[("10:00", 0.0)]),
                                       _leg("2026-09-29")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 16, 0))
        self.assertFalse(r["available"])

    def test_多腿求和(self):
        legs = {"a": pd.concat([_leg("2026-09-30", scale=1.0), _leg("2026-09-29")]).reset_index(drop=True),
                "b": pd.concat([_leg("2026-09-30", scale=2.0), _leg("2026-09-29")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 16, 0))
        # 今日 = (1+2)*400亿 = 1200亿；昨日 = (1+1)*400亿 = 800亿
        self.assertAlmostEqual(r["amount"] / 1e8, 1200.0, places=1)
        self.assertAlmostEqual(r["amount_prev"] / 1e8, 800.0, places=1)
        self.assertAlmostEqual(r["pct"], 50.0, places=4)


class TestLegConsistency(unittest.TestCase):
    """口径常量断言（含踩坑原因）。"""

    def test_腿为沪综深综北证50(self):
        """三腿 = 上证指数 + 深证综指 + 北证50。

        两个坑：
          · **深证成指不能用**：其实测「分时合计÷日线」仅 0.471（分钟口径只含成分股），
            盘中按分时累计会把深市少算一半。
          · **北交所必须算进来**：同花顺「成交额总计」是沪深北全市场，只算沪深会比它
            恒定低 ~120 亿（2026-09-30 实测：沪深 14380 vs 同花顺 14500）。
        """
        import inspect
        src = inspect.getsource(t_gui.Api._market_turnover)
        self.assertIn('"sh000001"', src)
        self.assertIn('"sz399106"', src, "深市腿必须是深证综指 sz399106")
        self.assertNotIn('"sz399001"', src, "深证成指的分钟口径只有成分股，会少算一半")
        self.assertIn("bj899050", src, "缺北交所腿 ⇒ 会比同花顺恒低 ~120 亿")
        self.assertIn("sina_index", src, "北交所腿只能走新浪（其余源全不可得）")

    def test_北交所不可得时如实标记(self):
        """降级为沪深口径必须显式标记，否则用户会以为数值正常（正是本次对不上的原因）。"""
        import inspect
        src = inspect.getsource(t_gui.Api._market_turnover)
        self.assertIn("bse_included", src)
        self.assertIn("degraded", src)


class TestTurnoverHistory(unittest.TestCase):
    """近 60 日成交额柱状图的数据组装（`_assemble_turnover_history`，纯函数）。"""

    @staticmethod
    def asm(*a):
        # 不要把函数直接赋成类属性（会变成绑定方法、多一个 self）——用包装函数转调
        return t_gui.Api._assemble_turnover_history(*a)

    @staticmethod
    def _hs(n=80, base=15000e8, start=1):
        return {f"2026-07-{d:02d}": base for d in range(start, start + n)}

    def test_截取近60日且升序(self):
        hs = {f"2026-06-{d:02d}": 1e8 for d in range(1, 31)}
        hs.update({f"2026-07-{d:02d}": 2e8 for d in range(1, 31)})
        hs.update({f"2026-08-{d:02d}": 3e8 for d in range(1, 31)})
        r = self.asm(hs, {}, 60, None, None, datetime(2026, 9, 30, 16, 0))
        self.assertEqual(len(r["days"]), 60)
        ds = [x["date"] for x in r["days"]]
        self.assertEqual(ds, sorted(ds), "必须升序")
        self.assertEqual(ds[-1], "2026-08-30")

    def test_沪深加北交所求和(self):
        hs = {"2026-07-01": 100e8, "2026-07-02": 200e8}
        bse = {"2026-07-01": 10e8, "2026-07-02": 20e8}
        r = self.asm(hs, bse, 60, None, None, datetime(2026, 7, 3, 16, 0))
        self.assertAlmostEqual(r["days"][0]["amount"], 110e8)
        self.assertAlmostEqual(r["days"][1]["amount"], 220e8)
        self.assertTrue(r["bse_included"])

    def test_无北交所时如实标记(self):
        r = self.asm({"2026-07-01": 100e8}, {}, 60, None, None, datetime(2026, 7, 2, 16, 0))
        self.assertFalse(r["bse_included"], "北交所缺数据必须显式标记（否则会以为数值正常）")
        self.assertAlmostEqual(r["days"][0]["amount"], 100e8)

    def test_环比前一交易日_首日为None(self):
        hs = {"2026-07-01": 100e8, "2026-07-02": 150e8, "2026-07-03": 120e8}
        r = self.asm(hs, {}, 60, None, None, datetime(2026, 7, 4, 16, 0))
        d = r["days"]
        self.assertIsNone(d[0]["delta_pct"], "首日无前值")
        self.assertAlmostEqual(d[1]["delta_pct"], 50.0, places=4)
        self.assertAlmostEqual(d[2]["delta_pct"], -20.0, places=4)

    def test_盘中补当日一根取实时值(self):
        """盘中 GM 日线不含当日 ⇒ 交易日集合要并入实时交易日，且该根用实时值。"""
        hs = {"2026-07-01": 100e8, "2026-07-02": 100e8}
        r = self.asm(hs, {}, 60, "2026-07-03", 700e8, datetime(2026, 7, 3, 10, 30))
        d = r["days"]
        self.assertEqual(d[-1]["date"], "2026-07-03", "当日应被补进序列")
        self.assertAlmostEqual(d[-1]["amount"], 700e8, msg="当日应用实时值覆盖")
        self.assertTrue(d[-1]["in_progress"])
        self.assertTrue(r["in_progress"])

    def test_收盘后不标进行中(self):
        hs = {"2026-07-01": 100e8, "2026-07-02": 100e8}
        r = self.asm(hs, {}, 60, "2026-07-02", 123e8, datetime(2026, 7, 2, 15, 0))
        self.assertFalse(r["in_progress"], "15:00 起不算进行中")
        self.assertFalse(any(x["in_progress"] for x in r["days"]))
        self.assertAlmostEqual(r["days"][-1]["amount"], 123e8)

    def test_进行中只标最后一根(self):
        hs = {f"2026-07-{d:02d}": 100e8 for d in range(1, 6)}
        r = self.asm(hs, {}, 60, "2026-07-05", 50e8, datetime(2026, 7, 5, 11, 0))
        flags = [x["in_progress"] for x in r["days"]]
        self.assertEqual(sum(flags), 1, "只能有一根进行中")
        self.assertTrue(flags[-1])

    def test_非今日不标进行中(self):
        """历史某日的部分量（回放场景）不得被当成进行中。"""
        hs = {"2026-07-01": 100e8, "2026-07-02": 60e8}
        r = self.asm(hs, {}, 60, "2026-07-02", 60e8, datetime(2026, 7, 9, 11, 0))
        self.assertFalse(r["in_progress"])


class TestSinaParsing(unittest.TestCase):
    """新浪 jsonp 解析（纯函数，离线）。"""

    @classmethod
    def setUpClass(cls):
        from core.market_data import sina_index
        cls.si = sina_index

    def test_解析正常jsonp(self):
        raw = ('var _=([{"day":"2026-09-30 15:00:00","open":"1039.798","high":"1040.531",'
               '"low":"1039.412","close":"1039.611","volume":"24727944",'
               '"amount":"459848704.0000"},{"day":"2026-09-30 14:55:00","open":"1","high":"1",'
               '"low":"1","close":"1","volume":"1","amount":"2"}]);')
        df = self.si.parse_jsonp(raw)
        self.assertEqual(len(df), 2)
        self.assertIn("time", df.columns)
        self.assertIn("amount", df.columns)
        # 升序：14:55 在前
        self.assertTrue(str(df["time"].iloc[0]).startswith("2026-09-30 14:55"))
        self.assertAlmostEqual(float(df["amount"].iloc[-1]), 459848704.0)

    def test_缺amount列补0(self):
        raw = 'var _=([{"day":"2026-09-30 15:00:00","open":"1","high":"1","low":"1","close":"1","volume":"9"}]);'
        df = self.si.parse_jsonp(raw)
        self.assertIn("amount", df.columns)
        self.assertEqual(float(df["amount"].iloc[0]), 0.0)

    def test_非法响应得空帧(self):
        for raw in ("", "not jsonp", "var _=();", "var _=([]);", "var _=([{\"x\":1}]);", "var _=([null]);"):
            self.assertTrue(self.si.parse_jsonp(raw).empty, f"{raw!r} 应为空帧")

    def test_空帧列齐备(self):
        self.assertEqual(list(self.si.parse_jsonp("").columns),
                         ["time", "open", "high", "low", "close", "volume", "amount"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
