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
import time as _time  # noqa: E402

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


class TestDeltaAndProjection(unittest.TestCase):
    """2026-10-23 owner：「要显示增量多少资金」「预测今天的成交额有多少」。

    * `delta` = 今日 − 昨日（与 pct **同分母**：盘中同期、收盘全日）。
    * `projected` = 盘中按「近 N 日**同一时刻**累计占全日比例」外推的全日额。
      A 股日内成交是 U 型，**不能**用线性时间占比（上午会系统性低估）。
    """

    # U 型：开盘半小时 400 亿，上午余下 100，午后 100，尾盘 400
    _U = [("10:00", 400e8), ("11:30", 100e8), ("14:00", 100e8), ("15:00", 400e8)]

    def _legs(self, days, bars=_BARS, scale=1.0):
        return {"sh000001": pd.concat([_leg(d, bars, scale) for d in days]).reset_index(drop=True)}

    def test_增量是绝对值且与pct同分母(self):
        legs = {"sh000001": pd.concat([_leg("2026-09-30", scale=1.5),
                                       _leg("2026-09-29")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 10, 35))
        # 今日 150 亿 vs 昨日 100 亿 ⇒ 增量 50 亿、pct +50%
        self.assertAlmostEqual(r["delta"] / 1e8, 50.0, places=1)
        self.assertAlmostEqual(r["pct"], 50.0, places=4)

    def test_收盘后也给增量(self):
        legs = {"sh000001": pd.concat([_leg("2026-09-30", scale=2.0),
                                       _leg("2026-09-29")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 9, 30, 16, 0))
        self.assertEqual(r["basis"], "全日")
        self.assertAlmostEqual(r["delta"] / 1e8, 400.0, places=1, msg="今日 800 − 昨日 400")
        self.assertNotIn("projected", r, "收盘后 amount 就是全日，不应再加'预测'")

    def test_U型日内按历史同时刻占比外推(self):
        """10:35 时已走 40%（U 型），应外推到 1000 亿 —— 线性时间会给出 3200 亿。"""
        days = ["2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-29"]
        legs = self._legs(days, self._U)
        today = self._legs(["2026-09-30"], self._U)
        for k in legs:
            legs[k] = pd.concat([legs[k], today[k]]).reset_index(drop=True)
        r = CALC(legs, datetime(2026, 9, 30, 10, 35))
        self.assertEqual(r["basis"], "同期")
        self.assertAlmostEqual(r["proj_frac"], 0.4, places=3)
        self.assertAlmostEqual(r["projected"] / 1e8, 1000.0, delta=1.0)
        self.assertLess(r["projected"] / 1e8, 1500.0, "线性外推会给出 3200 亿 —— 明显高估")
        self.assertEqual(r["proj_method"], "near_days_same_time")
        self.assertEqual(r["proj_days"], 6)

    def test_无同日历史时不给数(self):
        """昨日数据从 14:00 才开始 ⇒ 10:35 拿不到"同一时刻"占比 ⇒ 给 None（不线性硬猜）。"""
        r = t_gui.Api._project_full_day(
            {"sh000001": _leg("2026-09-29", bars=[("14:00", 100e8), ("15:00", 100e8)])},
            ["2026-09-29"], "10:35", 200e8)
        self.assertIsNone(r["projected"])
        self.assertEqual(r["proj_method"], "no_history")
        self.assertEqual(r["proj_days"], 0)

    def test_开盘瞬间不外推(self):
        """09:35 时只走了全日的 ~1%，外推乘 100 倍 ⇒ 噪声压过信号，宁可给 None。"""
        bars = [("09:35", 10e8), ("10:00", 400e8), ("11:30", 100e8),
                ("14:00", 100e8), ("15:00", 400e8)]
        legs = self._legs(["2026-09-29", "2026-09-30"], bars)
        r = CALC(legs, datetime(2026, 9, 30, 9, 35))
        self.assertIsNone(r.get("projected"), "占比 <5% 时外推不稳，不该给数")
        self.assertLess(r.get("proj_frac") or 1, 0.05)


class TestPrevTradingDaySameHm(unittest.TestCase):
    """`amount_prev_same_hm` —— 浮窗「实时累计」的分母（2026-10-28 owner）。

    原话：「两市成交额比的时**昨天的此时**不是全天」。
    截图实况 09:33:05：`1,301亿 / −91.92% / vs昨日全日 · 增量 −14,787亿 / 基准 15:00`。

    根因：bar 按**结束时刻**打戳（09:30-09:35 那根叫 09:35），开盘头几分钟当日一根都没有
    ⇒ `cur_day` 停在昨天、`intraday` 判假 ⇒ `amount_prev` 变成"**前日**全日"
    （10-26 的 16,088亿），浮窗拿它配今天开盘 3 分钟的实时累计。

    修法：另给一份**只按时钟 hm 截断、且取上一交易日**的累计。缺就缺（给 None），
    绝不用全日量冒充同期 —— 那正是"看着像真的"的假数字。
    """

    def _d(self, cur, prev):
        return {"sh000001": pd.concat([_leg(cur), _leg(prev)]).reset_index(drop=True)}

    def test_开盘首5分钟昨日还没有bar就不给同期基准(self):
        """09:33 时昨天也只有 10:00 及以后的 bar ⇒ 无同期可比 ⇒ None（浮窗只报总额）。"""
        r = CALC(self._d("2026-10-27", "2026-10-26"), datetime(2026, 10, 28, 9, 33))
        self.assertTrue(r["available"])
        self.assertEqual(r["basis"], "全日", "当日无 bar 时卡片仍按全日比（保持原口径）")
        self.assertEqual(r["prev_trading_day"], "2026-10-27", "「昨日」必须是上一交易日")
        self.assertIsNone(r["amount_prev_same_hm"],
                          "昨日当时也没有成交额 ⇒ 不给数，绝不用全日量顶")
        self.assertEqual(r["hm"], "09:33")

    def test_开盘后按昨日同一时刻截断(self):
        """10:35 ⇒ 分母是昨日**只累计到 10:00 那根**的 100 亿，不是全日 400 亿。"""
        r = CALC(self._d("2026-10-28", "2026-10-27"), datetime(2026, 10, 28, 10, 35))
        self.assertEqual(r["basis"], "同期")
        self.assertEqual(r["prev_trading_day"], "2026-10-27")
        self.assertAlmostEqual(r["amount_prev_same_hm"] / 1e8, 100.0, places=1,
                               msg="全日是 400 亿 —— 拿它做分母会显示 −75%")
        self.assertAlmostEqual(r["amount_prev_same_hm"], r["amount_prev"],
                               msg="盘中同期口径下两者必须一致（不能各算一套）")

    def test_当日无分钟线时也用昨日截断(self):
        """开盘几分钟的真实形态：分钟数据最新只到昨天，但分母仍按"昨日+当前时刻"。"""
        bars = [("09:35", 100e8), ("10:00", 100e8), ("15:00", 100e8)]
        legs = {"sh000001": pd.concat([_leg("2026-10-27", bars),
                                       _leg("2026-10-26", bars)]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 10, 28, 9, 36))
        self.assertEqual(r["day"], "2026-10-27", "当日没有 bar，cur_day 停在昨天")
        self.assertEqual(r["basis"], "全日", "卡片口径不变")
        self.assertEqual(r["prev_trading_day"], "2026-10-27",
                         "⚠️ 回归闸：「昨日」是 10-27，不是 10-26")
        self.assertAlmostEqual(r["amount_prev_same_hm"] / 1e8, 100.0, places=1,
                               msg="只该算昨日 09:35 那根；若得 300 亿说明用了全日")

    def test_收盘后同期等于全日(self):
        r = CALC(self._d("2026-10-28", "2026-10-27"), datetime(2026, 10, 28, 16, 0))
        self.assertEqual(r["basis"], "全日")
        self.assertAlmostEqual(r["amount_prev_same_hm"] / 1e8, 400.0, places=1)

    def test_周六的昨日是周五(self):
        legs = {"sh000001": pd.concat([_leg("2026-10-30"), _leg("2026-10-29")]).reset_index(drop=True)}
        r = CALC(legs, datetime(2026, 10, 31, 10, 35))
        self.assertEqual(r["prev_trading_day"], "2026-10-30",
                         "非交易日：上一交易日仍是周五，不能退到周四")

    def test_降级路径也带同期字段(self):
        """日线兜底只在 ≥15:00 走，此刻同期==全日 ⇒ 也要给浮窗同一套字段。"""
        import inspect
        src = inspect.getsource(t_gui.Api._turnover_daily_fallback)
        for k in ('"amount_prev_same_hm"', '"prev_trading_day"', '"hm"'):
            self.assertIn(k, src, f"兜底载荷缺 {k} ⇒ 浮窗在降级时又回到'全日配实时'")


class TestWidgetBasePick(unittest.TestCase):
    """浮窗挑分母（`scripts/turnover_widget.py::choose_base`，纯函数，离线）。"""

    @classmethod
    def setUpClass(cls):
        _scripts = os.path.join(_ROOT, "scripts")
        if _scripts not in sys.path:
            sys.path.insert(0, _scripts)
        import turnover_widget as tw
        cls.tw = tw

    def test_只认同期基准(self):
        p = {"available": True, "basis": "全日", "amount_prev": 16088e8,
             "amount_prev_same_hm": 500e8, "prev_trading_day": "2026-10-27", "hm": "10:35"}
        pick = self.tw.choose_base(p, datetime(2026, 10, 28, 10, 35).timetuple())
        self.assertAlmostEqual(pick["base"], 500e8, msg="必须用同期，不能用 amount_prev")
        self.assertEqual(pick["day"], "2026-10-27")
        self.assertEqual(pick["label"], "vs昨日同期")
        self.assertTrue(pick["in_session"])

    def test_无同期基准就不给对比(self):
        p = {"available": True, "basis": "全日", "amount_prev": 16088e8,
             "amount_prev_same_hm": None, "hm": "09:33"}
        pick = self.tw.choose_base(p, datetime(2026, 10, 28, 9, 33).timetuple())
        self.assertIsNone(pick["base"], "没有同期基准时不许退化成 amount_prev")
        self.assertEqual(pick["hm"], "09:33")

    def test_收盘后标签转全日(self):
        p = {"amount_prev_same_hm": 1.6e12, "prev_trading_day": "2026-10-27", "hm": "15:00"}
        pick = self.tw.choose_base(p, datetime(2026, 10, 28, 16, 0).timetuple())
        self.assertEqual(pick["label"], "vs昨日全日")
        self.assertFalse(pick["in_session"])

    def test_无载荷不抛(self):
        for p in (None, {}, {"amount_prev_same_hm": "x"}, {"amount_prev_same_hm": 0}):
            self.assertIsNone(self.tw.choose_base(p, datetime(2026, 10, 28, 10, 0).timetuple())["base"])


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

    # ── 2026-10-16：图从"近 60 日"扩到"近 1 年"，北交所腿**够不到一年**（源硬上限）──
    # 新浪 bj899050 的 30 分钟线 `datalen` 实测上限 1500 根 = **188 个交易日**，
    # 再往上（2500/3000/5000）直接返反爬桩。缺的那段 amount 只含沪深两腿 ⇒
    # 少算约 120 亿/日（~0.6%），图上只有 ~1 像素 —— **必须能看出来，不许静默**。

    def test_北交所部分覆盖时逐日标注(self):
        hs = {f"2026-07-0{d}": 100e8 for d in range(1, 6)}
        bse = {"2026-07-04": 12e8, "2026-07-05": 12e8}          # 只覆盖最后两天
        r = self.asm(hs, bse, 60, None, None, datetime(2026, 7, 6, 16, 0))
        d = r["days"]
        self.assertEqual([x["bse_missing"] for x in d], [True, True, True, False, False])
        self.assertEqual(r["bse_days"], 2)
        self.assertEqual(r["bse_from"], "2026-07-04")
        self.assertAlmostEqual(d[0]["amount"], 100e8, msg="缺腿那几天就只有沪深两腿")
        self.assertAlmostEqual(d[3]["amount"], 112e8)

    def test_北交所整腿不可得时不逐日报缺(self):
        """整腿都取不到时由 `bse_included=False` 承担说明，不必逐日刷同一句。"""
        r = self.asm({f"2026-07-0{d}": 100e8 for d in range(1, 4)}, {}, 60, None, None,
                     datetime(2026, 7, 4, 16, 0))
        self.assertFalse(r["bse_included"])
        self.assertFalse(any(x["bse_missing"] for x in r["days"]))
        self.assertEqual(r["bse_days"], 0)
        self.assertIsNone(r["bse_from"])

    def test_一年窗口能取满250根(self):
        """「近 1 年」= 250 个交易日：源给足时应取满、升序、且末根最新。"""
        hs = {f"D{i:04d}": 1e12 for i in range(310)}
        r = self.asm(hs, {}, 250, None, None, datetime(2026, 10, 16, 16, 0))
        ds = [x["date"] for x in r["days"]]
        self.assertEqual(len(ds), 250)
        self.assertEqual(ds, sorted(ds))
        self.assertEqual(ds[-1], "D0309")

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


class TestTurnoverHistoryCacheKey(unittest.TestCase):
    """⚠️ 缓存必须**按天数分键**（2026-10-16 修的**实盘可见**缺陷）。

    症状（owner 截图报回来）：图从"近60日"改成"近1年"后，**标题变了但柱子还是 ~60 根**。
    根因：`_turnover_hist_cache` 是**单槽** `["payload"]`、磁盘快照是**单名**
    `turnover_history.json`，`days` 完全不参与；而 `prewarm_overview` 启动时**裸调**
    `self.load_turnover_history`（当时默认 60）把缓存填满 ⇒ 前端问 250 拿到 60。

    本组用桩替换真实现（全离线），钉住：① 60 天的旧缓存**不能**满足 250 的请求；
    ② 反过来也不能；③ 默认值就是图窗口（裸调预热必须与前端同宽）。
    """

    def setUp(self):
        self._cache = t_gui.Api._turnover_hist_cache
        self._snap = t_gui._snapshot_read
        self._fn = t_gui.Api._turnover_history_cached
        self.calls = []
        t_gui.Api._turnover_hist_cache = {}

        def _stub(days=None):
            d = int(t_gui.Api._TURNOVER_HIST_DAYS if days is None else days)
            self.calls.append(d)
            out = {"available": True, "days": [{"date": "D%04d" % i} for i in range(d)]}
            t_gui.Api._turnover_hist_cache[d] = (_time.time(), out)   # 与真实现同：写自己的槽
            return out

        t_gui.Api._turnover_history_cached = _stub
        t_gui._snapshot_read = lambda k: None

    def tearDown(self):
        t_gui.Api._turnover_hist_cache = self._cache
        t_gui._snapshot_read = self._snap
        t_gui.Api._turnover_history_cached = self._fn

    def test_stale_60d_cache_does_not_satisfy_250d(self):
        t_gui.Api._turnover_hist_cache[60] = (_time.time(),
                                             {"available": True, "days": [{"date": "x"}] * 60})
        got = t_gui.Api.load_turnover_history(t_gui.Api, 250)
        self.assertEqual(len(got["days"]), 250,
                         "250 的请求被 60 天的旧缓存顶掉了 —— 就是 owner 截图那个症状")

    def test_250d_cache_does_not_satisfy_60d(self):
        got = t_gui.Api.load_turnover_history(t_gui.Api, 250)     # 先填 250
        self.assertEqual(len(got["days"]), 250)
        got2 = t_gui.Api.load_turnover_history(t_gui.Api, 60)
        self.assertEqual(len(got2["days"]), 60, "反向也不能串槽")

    def test_second_call_hits_the_same_slot(self):
        t_gui.Api.load_turnover_history(t_gui.Api, 250)
        n = len(self.calls)
        t_gui.Api.load_turnover_history(t_gui.Api, 250)
        self.assertEqual(len(self.calls), n, "同宽度的第二次调用应命中缓存、不再算")

    def test_default_is_the_chart_window(self):
        """裸调（`prewarm_overview` 就这么调）必须与图窗口同宽，否则预热是白热。"""
        self.assertEqual(t_gui.Api._TURNOVER_HIST_DAYS, 250)
        got = t_gui.Api.load_turnover_history(t_gui.Api)
        self.assertEqual(len(got["days"]), 250)


class TestHsLegValidation(unittest.TestCase):
    """⚠️ 沪深两腿**必须都是"真成交额"**才可信（2026-10-16 实盘缺陷）。

    症状（owner 截图）：图上一半高、tooltip 环比 **+99.14%**、5 日均量 9108 亿
    （真值 1.65 万亿）—— 而「两市成交额」卡片是**对的** 1.57 万亿。
    根因：`facade.index_daily` 回退腾讯时，腾讯 ifzq 的指数日线**只有 6 列没有成交额**，
    `tencent_provider` 用 `volume` 顶替 `amount`（`:445` / `_idx_ensure_cols`，
    那条为 ratio 口径刻意保留）⇒ 该腿 "成交额" 只有 ~1e8（成交量级），
    与另一腿真值 ~1e11 **相加 = 当天腰斩**，且完全静默。
    """

    @staticmethod
    def frames(sh_amount, sz_amount, day="2026-10-15"):
        def one(a):
            return pd.DataFrame([{"date": day, "open": 1.0, "high": 1.0, "low": 1.0,
                                  "close": 1.0, "volume": 1.0, "amount": a}])
        return [one(sh_amount), one(sz_amount)]

    def test_both_legs_good_sums(self):
        good, bad = t_gui.Api._hs_daily_from_frames(self.frames(7.744e11, 8.586e11))
        self.assertAlmostEqual(good["2026-10-15"], 1.633e12, delta=1e9)
        self.assertEqual(bad, [])

    def test_degraded_leg_rejects_the_whole_day(self):
        """一腿是成交量级（腾讯回退）⇒ **整条拒收**，绝不吐一个腰斩的值。"""
        good, bad = t_gui.Api._hs_daily_from_frames(self.frames(7.744e11, 5.10e8))
        self.assertEqual(good, {}, "半截值必须被拒 —— 它就是 owner 截图里那个 +99% 的来源")
        self.assertEqual(bad, ["2026-10-15"])

    def test_single_leg_missing_date_rejected(self):
        """某天只有一条腿有数据（另一腿未覆盖）⇒ 同样拒收，不拿单腿充总数。"""
        sh = pd.DataFrame([{"date": "2026-10-15", "amount": 7.744e11}])
        sz = pd.DataFrame([{"date": "2026-10-16", "amount": 8.229e11}])
        good, bad = t_gui.Api._hs_daily_from_frames([sh, sz])
        self.assertEqual(good, {})
        self.assertEqual(bad, ["2026-10-15", "2026-10-16"])

    def test_threshold_is_about_magnitude_not_source_label(self):
        """判据是**量级**（真成交额 ≥1e10 元），不是 source 标签 —— 标签分不出"缓存里的旧值"。"""
        good, _ = t_gui.Api._hs_daily_from_frames(self.frames(1.0e10, 1.0e10))
        self.assertIn("2026-10-15", good, "正好卡在阈值上的应算可信")
        good2, bad2 = t_gui.Api._hs_daily_from_frames(self.frames(9.9e9, 9.9e9))
        self.assertEqual(good2, {})
        self.assertEqual(bad2, ["2026-10-15"])


class TestHsFreezeCache(unittest.TestCase):
    """历史成交额是**事实** ⇒ 算对一次就冻结；降级的计算**不许覆盖**。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._fp = t_gui._HS_DAILY_CACHE_FP
        t_gui._HS_DAILY_CACHE_FP = Path(tempfile.mkdtemp()) / "hs.json"

    def tearDown(self):
        t_gui._HS_DAILY_CACHE_FP = self._fp

    def test_round_trip(self):
        t_gui._save_hs_daily_cache({"2026-10-15": 1.6461e12, "2026-10-16": 1.5692e12})
        got = t_gui._load_hs_daily_cache()
        self.assertAlmostEqual(got["2026-10-15"], 1.6461e12, delta=1e8)
        self.assertEqual(len(got), 2)

    def test_missing_file_is_empty_not_raise(self):
        self.assertEqual(t_gui._load_hs_daily_cache(), {})

    def test_corrupt_file_is_empty_not_raise(self):
        t_gui._HS_DAILY_CACHE_FP.write_text("{ not json", encoding="utf-8")
        self.assertEqual(t_gui._load_hs_daily_cache(), {})

    def test_capped_at_400_days(self):
        t_gui._save_hs_daily_cache({f"D{i:04d}": 1e12 for i in range(500)})
        self.assertEqual(len(t_gui._load_hs_daily_cache()), 400, "别无限长，够 1 年窗口 + 裕度")


if __name__ == "__main__":
    unittest.main(verbosity=2)
