# -*- coding: utf-8 -*-
"""选股猎手一级板块指南的数据契约、前端位置与转义测试（全离线）。"""
import ast
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY_SOURCE = (ROOT / "t_gui.py").read_text(encoding="utf-8")
JS_SOURCE = (ROOT / "web" / "app.js").read_text(encoding="utf-8")


def _literal_assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"缺少模块常量 {name}")


def _api_method(tree, name):
    api = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Api")
    return next(node for node in api.body if isinstance(node, ast.FunctionDef) and node.name == name)


class TestCategoryGuidePayload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse(PY_SOURCE)
        cls.guides = _literal_assignment(cls.tree, "CATEGORY_GUIDES")

    def test_cloud_computing_and_network_security_are_configured(self):
        self.assertEqual(set(self.guides), {"云计算", "网络安全"})
        guide = self.guides["云计算"]
        self.assertEqual(guide["title"], "什么是云计算？")
        self.assertEqual(len(guide["sections"]), 4)
        security_guide = self.guides["网络安全"]
        self.assertEqual(security_guide["title"], "什么是网络安全？")
        self.assertEqual(len(security_guide["sections"]), 4)

    def test_guide_covers_definition_services_boundaries_and_relevance(self):
        text = json.dumps(self.guides["云计算"], ensure_ascii=False)
        required = (
            "远端数据中心", "按需使用", "按用量或订阅付费", "不自己买发电机，而是接电厂的电",
            "IaaS", "租计算、存储和网络资源", "PaaS", "开发、部署和运行应用的平台",
            "SaaS", "直接使用线上软件", "IDC 机柜不自动等于云", "CDN 不自动等于云",
            "云安全、SASE 属于安全服务", "普通硬件销售、系统集成、传统软件授权",
            "DeepSeek 合作，都不等于云计算", "明确主营业务、重要收入来源，或已有规模化产品",
            "真实产品或项目，但仍属新业务，或收入占比较低", "只有规划、参股或内部上云",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_network_security_covers_definition_services_boundaries_and_relevance(self):
        text = json.dumps(self.guides["网络安全"], ensure_ascii=False)
        required = (
            "保护网络、系统、应用、数据及云环境", "阻止攻击、泄露和中断",
            "边界与终端安全", "身份与数据安全", "安全运营与 SOC", "云安全与 SaaS 安全", "SASE",
            "保护云环境不等于提供云计算", "普通 IT 集成、硬件销售、等保咨询单次项目",
            "不自动等于核心网络安全", "使用安全产品或进行内部安全建设，不等于对外提供安全业务",
            "明确主营业务、重要收入来源，或已有规模化安全产品",
            "有真实安全产品或项目，但仍属新业务，或收入占比较低",
            "只有规划、合作、参股或内部使用，且无对外商业化证据",
        )
        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_today_and_history_share_the_same_mapping(self):
        for method_name in ("_load_hunter_impl", "load_hunter_history"):
            method = _api_method(self.tree, method_name)
            assignments = [
                node for node in ast.walk(method)
                if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name) and target.value.id == "out"
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "category_guides" for target in node.targets)
            ]
            self.assertEqual(len(assignments), 1, f"{method_name} 应下发一次 category_guides")
            self.assertIsInstance(assignments[0].value, ast.Name)
            self.assertEqual(assignments[0].value.id, "CATEGORY_GUIDES")


class TestCategoryGuideFrontend(unittest.TestCase):
    def test_guide_is_before_legend_and_stock_table(self):
        start = JS_SOURCE.index("function renderHunter(h)")
        segment = JS_SOURCE[start:JS_SOURCE.index("\nfunction ", start + 10)]
        guide = segment.index("hunterCategoryGuideHtml((h.category_guides || {})[category])")
        legend = segment.index('<div class="h-legend"')
        table = segment.index("<table><thead>", legend)
        self.assertLess(guide, legend)
        self.assertLess(legend, table)

    @unittest.skipUnless(shutil.which("node"), "需要 Node 验证实际 HTML 转义")
    def test_renderer_escapes_all_backend_copy(self):
        esc_start = JS_SOURCE.index("function esc(s)")
        esc_end = JS_SOURCE.index("\nfunction fmt", esc_start)
        guide_start = JS_SOURCE.index("function hunterCategoryGuideHtml(guide)")
        guide_end = JS_SOURCE.index("\nfunction renderHunter", guide_start)
        hostile = {
            "title": "<script>alert(1)</script>",
            "sections": [{"heading": "A&B", "text": '"<b>x</b>', "items": ["'<img>"]}],
        }
        script = "\n".join((
            JS_SOURCE[esc_start:esc_end],
            JS_SOURCE[guide_start:guide_end],
            f"const hostile = {json.dumps(hostile, ensure_ascii=False)};",
            "process.stdout.write(JSON.stringify({hostile: hunterCategoryGuideHtml(hostile), empty: hunterCategoryGuideHtml(null)}));",
        ))
        result = subprocess.run(
            ["node", "-e", script], check=True, capture_output=True, text=True, encoding="utf-8")
        rendered = json.loads(result.stdout)
        self.assertEqual(rendered["empty"], "")
        self.assertNotIn("<script>", rendered["hostile"])
        self.assertNotIn("<b>x</b>", rendered["hostile"])
        self.assertNotIn("<img>", rendered["hostile"])
        self.assertIn("&lt;script&gt;", rendered["hostile"])
        self.assertIn("A&amp;B", rendered["hostile"])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", rendered["hostile"])
        self.assertIn("&lt;img&gt;", rendered["hostile"])

    def test_dark_theme_styles_are_present(self):
        css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".h-category-guide-grid", css)
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        self.assertIn(".h-category-guide-title", css)


if __name__ == "__main__":
    unittest.main(verbosity=2)
