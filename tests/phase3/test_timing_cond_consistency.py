# -*- coding: utf-8 -*-
"""「时机条件」列跨表一致性守卫（2026-10-16）。**全离线**。

owner 2026-10-16：「选股猎手的时机条件列与建仓信号扫描的该列保持一致」。

## 缺陷是什么（本文件要钉住的）

真源 `t_gui.COND_LABELS` 在 2026-10-15 从 4 条扩到 **6 条**（新增 `t_ma20`/`t_ma5`
两条**硬约束** —— 正是"解释 NO-GO 为什么 NO-GO"的那两条）。但：

  · **后端**：`_hunter_build_conformance` 的 `conds` **没有**这两个键 ⇒ 猎手/风向标的
    圆点数据本身就缺；
  · **前端**：猎手行/风向标行的条件键是**硬编码 `["t_regime","t_trend","t_drawdown"]`**，
    与真源脱钩，且 tooltip 拿 `build_reason` 当 title（与建仓表的逐条「通过/未过」不同源）。

⇒ 同一只票在猎手表里 **4 个圆点**、在建仓表里 **7 个**（6 条件 + 1 否决）。

## 修法

1. 后端 `_hunter_build_conformance.conds` 补齐两键（与 `core/position_builder.py` 同口径 `is True`）；
2. 四张表（建仓 / 自动盘 / 猎手 / 风向标）的圆点与 tooltip 收进**前端一个** `condCellHtml`；
3. 标签一律用 **payload 里的 `cond_labels`**（后端真源），前端**不再维护任何副本**
   —— 副本正是本次漂移的来源（`AUTO_COND_LABELS`、猎手硬编码键表都已删）。

运行：python -m unittest tests.phase3.test_timing_cond_consistency -v
"""
import ast
import re
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_TGUI_SRC = (_ROOT / "t_gui.py").read_text(encoding="utf-8")
_APPJS_SRC = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """剥掉 JS 的 `//` 行注释与 `/* */` 块注释。

    ⚠️ 必要性：本文件好几个判据是"某标识符/字面量**不得再出现**"，而**注释里提它是正常的**
    （比如"已删掉 AUTO_COND_LABELS 副本"这种说明）。首版没剥注释 ⇒ 被自己的注释判失败。
    **判据必须看代码，不看说明文字。**
    """
    src = re.sub(r"/\*[\s\S]*?\*/", " ", src)
    src = re.sub(r"(?m)^\s*//.*$", " ", src)
    src = re.sub(r"\s//\s.*$", " ", src, flags=re.M)
    return src


_APPJS_CODE = _strip_js_comments(_APPJS_SRC)

#: 喂给「时机条件」列的四张表（前端各自的行构造）——都必须在 payload 里拿到标签真源
_TABLES = {
    "建仓信号扫描": "cond_labels",       # 既有（`_agg_position_builder` 一直带）
    "自动盘建仓扫描": "cond_labels",
    "选股猎手": "cond_labels",
    "市场风向标": "cond_labels",
}


def _cond_labels_keys() -> set:
    """从 t_gui.py 源码里取出真源 `COND_LABELS` 的键（不 import t_gui：导入重且有副作用）。"""
    tree = ast.parse(_TGUI_SRC)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "COND_LABELS":
                    return {k.value for k in node.value.keys}
    raise AssertionError("t_gui.py 里找不到 COND_LABELS —— 真源被改名/删除了？")


def _hunter_conds_keys() -> set:
    """从 `_hunter_build_conformance` 里取出 `conds = {...}` 字面量的键。"""
    tree = ast.parse(_TGUI_SRC)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_hunter_build_conformance":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    tgt = sub.targets[0]
                    if isinstance(tgt, ast.Name) and tgt.id == "conds" \
                            and isinstance(sub.value, ast.Dict):
                        return {k.value for k in sub.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("_hunter_build_conformance 里找不到 conds 字面量")


class TestHunterCondsKeysMatchSource(unittest.TestCase):
    """**本次缺陷的正题**：猎手后端喂的键集必须 == 真源键集（+ t_veto）。"""

    def test_keys_equal_cond_labels_plus_veto(self):
        want = _cond_labels_keys() | {"t_veto"}
        got = _hunter_conds_keys()
        self.assertEqual(
            got, want,
            "猎手 conds 键集与真源 COND_LABELS 不一致。\n"
            "  真源+veto = %s\n  猎手实际  = %s\n"
            "  缺: %s ／ 多: %s\n"
            "（真源一改就必须同步这里，否则同一只票在不同表里圆点数不同）"
            % (sorted(want), sorted(got), sorted(want - got), sorted(got - want)))

    def test_hunter_renderer_uses_payload_labels_not_hardcoded_keys(self):
        """前端不许再出现硬编码的条件键表（`.map(` 后面直接跟 3 键数组那种）。"""
        bad = re.findall(r'\["t_regime",\s*"t_trend",\s*"t_drawdown"\]\.map\(', _APPJS_SRC)
        self.assertEqual(bad, [], "app.js 里又有硬编码条件键表了 —— 请改走 payload 的 cond_labels")


class TestSharedRendererUsedByAllTables(unittest.TestCase):
    """各表的圆点/tooltip 必须走同一个渲染函数。

    2026-10-17：自动盘「建仓信号扫描」整块已删 ⇒ 用它的表从 4 张减到 3 张
    （建仓 / 猎手 / 风向标）。新增表必须继续走这个渲染器，别各写一份。
    """

    def test_condCellHtml_defined_once(self):
        self.assertEqual(_APPJS_SRC.count("function condCellHtml("), 1,
                         "condCellHtml 应只有一份定义")

    def test_every_table_uses_it(self):
        # 建仓表 / 猎手行 / 风向标行 各一处调用（+1 处定义本身）
        self.assertGreaterEqual(_APPJS_SRC.count("condCellHtml("), 4,
                                "应有 3 张表调用 + 1 处定义")

    def test_legacy_inline_dots_removed(self):
        """旧的 `condStr`/`condTitle`/`AUTO_COND_LABELS` 内联实现必须清干净。

        ⚠️ 只按**变量名赋值**判，不按注释里的字样 —— 注释里说明"已删掉副本"不算命中。
        """
        for legacy in ("const condStr =", "const condTitle =", "const AUTO_COND_LABELS =",
                       "const vetoDot = isNoData"):
            self.assertNotIn(legacy, _APPJS_SRC, f"{legacy} 仍在 —— 内联实现没清干净")


class TestLabelSourceDelivered(unittest.TestCase):
    """四张表的 payload 都必须真的带 `cond_labels`，否则前端拿不到真源。"""

    def test_payloads_carry_cond_labels(self):
        n = len(re.findall(r'"cond_labels":\s*COND_LABELS', _TGUI_SRC))
        self.assertGreaterEqual(
            n, 4,
            "应有 ≥4 处下发 cond_labels（建仓表聚合 / 自动盘 / 猎手 live / 猎手历史 / 风向标）——"
            "少一处，那张表的圆点就没有标签真源（历史视图会退化成空白）")

    def test_frontend_never_ships_its_own_label_map(self):
        """前端**不许**自带标签表 —— 副本是漂移源（本次就是这么坏的）。

        只看**代码**（注释已剥）：注释里说明"删掉了某副本"是应该允许的。
        """
        self.assertNotIn("AUTO_COND_LABELS", _APPJS_CODE)
        # 也不许在 JS 里写条件标签字面量（真源只在后端 COND_LABELS）
        self.assertIsNone(re.search(r'"站上MA20\(硬约束\)"', _APPJS_CODE),
                          "app.js 里出现了条件标签字面量 ⇒ 又建副本了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
