# -*- coding: utf-8 -*-
"""`core/style_rotation.py` 离线单测（2026-09-28）。

覆盖：p75 分位 / 板块排名确定性（并列按名字） / 退化日识别（全等值·fetch_ok=0·样本过少） /
      相对强度 RS 计算 / **切换状态机的两个字段与迟滞维持带** / 纯函数性（不写盘）。

铁律：**全离线**——全部用合成序列，不读 `t_io/`、不打网络。
      （`build_style_rotation` 需要真实指数日线，故不在本文件；口径断言见 tmp/verify_review_revamp.py。）

背景：本模块此前把「风格切没切」与「现在是什么风格」塞进同一个标签，产出过
      "RS5 +7.27 却显示未切换"这类反直觉结果；现拆成 `当前风格` / `状态` 两列。
      另加 0.6θ 迟滞维持带，避免陈旧事件永远挂着"已切换"。

运行：python tests/phase3/test_style_rotation.py
"""
import os
import sys
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import style_rotation as sr  # noqa: E402


def _days(n, start_day=1):
    """生成 n 个连续日期的字符串（用日期序号当"交易日"，只管排序与间隔）。"""
    return [f"2026-{1 + (start_day + i) // 28:02d}-{1 + (start_day + i) % 28:02d}"
            for i in range(n)]


class TestP75(unittest.TestCase):
    def test_基本分位(self):
        # statistics.quantiles 默认 exclusive 法：n=4 → [1.5, 3.0, 4.5]
        self.assertAlmostEqual(sr._p75([1, 2, 3, 4, 5]), 4.5, places=6)

    def test_样本不足返回None(self):
        self.assertIsNone(sr._p75([1, 2, 3]), "少于 4 个样本无法取四分位")

    def test_忽略None(self):
        # 过滤 None 后为 [1,2,3,4]，exclusive p75 = 3.75
        self.assertAlmostEqual(sr._p75([1, 2, 3, 4, None]), 3.75, places=6)


class TestRankMap(unittest.TestCase):
    def test_按均分降序(self):
        self.assertEqual(sr._rank_map({"a": 3.0, "b": 1.0, "c": 2.0}),
                         {"a": 1, "c": 2, "b": 3})

    def test_并列按名字保证确定性(self):
        """并列必须有确定的排序，否则同输入两次调用可能给出不同排名。"""
        first = sr._rank_map({"b": 1.0, "a": 1.0, "c": 1.0})
        second = sr._rank_map({"c": 1.0, "a": 1.0, "b": 1.0})
        self.assertEqual(first, second, "同输入不同构造顺序必须给出同一排名")
        self.assertEqual(first, {"a": 1, "b": 2, "c": 3})


class TestDegenerate(unittest.TestCase):
    """退化日若不当成退化，排名会变成纯属偶然的"轮动"结论（09-21 实测教训）。"""

    def test_全量拉取失败(self):
        self.assertIsNotNone(sr._degenerate({"avgs": {"a": 1.0, "b": 2.0}, "fetch_ok": 0}))

    def test_板块均分全等(self):
        self.assertIsNotNone(sr._degenerate({"avgs": {k: 0.0 for k in "abcdef"}, "fetch_ok": 100}))

    def test_样本过少(self):
        self.assertIsNotNone(sr._degenerate({"avgs": {"a": 1.0, "b": 2.0}, "fetch_ok": 100}))

    def test_无数据(self):
        self.assertIsNotNone(sr._degenerate(None))

    def test_正常数据不判退化(self):
        self.assertIsNone(sr._degenerate({"avgs": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
                                          "fetch_ok": 100}))


class TestRSSeries(unittest.TestCase):
    def test_同涨同跌时RS为零(self):
        c = {f"d{i}": 100.0 * (1.01 ** i) for i in range(10)}
        rs = sr._rs_series(c, c, 5)
        for v in rs.values():
            self.assertAlmostEqual(v, 0.0, places=9)

    def test_相对更强时RS为正(self):
        base = {f"d{i}": 100.0 for i in range(10)}
        strong = {f"d{i}": 100.0 * (1.01 ** i) for i in range(10)}
        rs = sr._rs_series(strong, base, 5)
        self.assertGreater(rs["d9"], 0)

    def test_样本不足返回空(self):
        c = {f"d{i}": 100.0 for i in range(3)}
        self.assertEqual(sr._rs_series(c, c, 5), {})


class TestSwitchState(unittest.TestCase):
    """状态机：两个字段各答一问 + 迟滞维持带。"""

    def _rs(self, tail_value):
        """构造 200 日 RS5：前 150 日 −3（弱），第 150 日翻 +5（构成一次有效事件），
        此后线性衰减到 tail_value（保持同号）。

        ⚠️ 翻转必须落在第 120 日之后：`_theta_at` 需要 ≥120 个历史样本才算得出 θ，
        否则翻转当日 θ 为 None、事件根本不会登记（本测试第一版就栽在这里）。
        """
        ds = _days(200)
        rs = {}
        for i, d in enumerate(ds):
            if i < 150:
                rs[d] = -3.0
            elif i == 150:
                rs[d] = 5.0
            else:
                rs[d] = max(0.1, 5.0 - (i - 150) * (5.0 - tail_value) / (199 - 150))
        return rs, ds[-1]

    def test_两字段同时存在(self):
        rs, last = self._rs(4.0)
        out = sr._switch_state("测试", rs, {}, last)
        self.assertIn("当前风格", out, "必须回答'现在是什么风格'")
        self.assertIn("状态", out, "必须回答'有没有发生过切换'")
        self.assertIn("RS5(pp)", out)

    def test_方向未弱化时维持已切换(self):
        rs, last = self._rs(4.0)
        out = sr._switch_state("测试", rs, {}, last)
        self.assertEqual(out["状态"], "已切换", f"事件方向仍在维持带之上：{out}")
        self.assertIn("偏强", out["当前风格"])

    def test_方向弱化后迟滞解除(self):
        """陈旧事件不得永远挂着'已切换'——当前 |RS5| 跌破 0.6θ 即解除。"""
        rs, last = self._rs(0.1)
        out = sr._switch_state("测试", rs, {}, last)
        self.assertEqual(out["状态"], "未切换", f"|RS5| 已远低于维持带：{out}")
        self.assertIn("维持带", out.get("说明", ""), "解除原因应点明维持带")

    def test_样本不足判insufficient(self):
        ds = _days(50)
        rs = {d: 1.0 for d in ds}
        out = sr._switch_state("测试", rs, {}, ds[-1])
        self.assertEqual(out["状态"], "insufficient", f"{out}")
        self.assertIsNone(out["阈值θ(pp)"])

    def test_当日无数据判insufficient(self):
        out = sr._switch_state("测试", {"2026-01-01": 1.0}, {}, "2026-06-01")
        self.assertEqual(out["状态"], "insufficient")


class TestConstants(unittest.TestCase):
    def test_迟滞比在开区间(self):
        self.assertGreater(sr._SWITCH_HYSTERESIS, 0.0)
        self.assertLess(sr._SWITCH_HYSTERESIS, 1.0)

    def test_θ门槛最小样本不高于滚动窗口(self):
        self.assertLessEqual(sr._THETA_MIN_OBS, sr._THETA_LOOKBACK,
                             "最小样本不得大于回看窗口，否则永远算不出 θ")

    def test_空板块数据不崩(self):
        out = sr.build_sector_rotation("2026-09-24")
        self.assertIn("状态", out)
        self.assertIsInstance(out["状态"], str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
