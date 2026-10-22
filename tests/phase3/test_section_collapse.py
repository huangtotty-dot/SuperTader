# -*- coding: utf-8 -*-
"""区块折叠（2026-10-22 owner：建仓信号扫描 / 异常监控池 要能折叠隐藏）。

约束：
  · 折叠只隐藏，**不销毁**（内容仍在 DOM，展开即见，不丢已加载的数据）；
  · 状态存 localStorage，刷新/重启后保持；
  · 标题行里的输入框/按钮照常可用（折叠按钮是独立元素，不抢它们的点击）。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_CSS = (_ROOT / "web" / "style.css").read_text(encoding="utf-8")


class TestCollapseMarkup:
    def test_both_sections_have_toggle(self):
        for sec in ("position-builder", "abnormal-pool"):
            assert f"toggleSection('{sec}')" in _HTML, f"{sec} 缺少折叠按钮"
            assert f'data-sec="{sec}"' in _HTML

    def test_toggle_is_inside_h2(self):
        """按钮必须在 <h2> 里 —— 折叠时 <h2> 是唯一保留的行，按钮不能跟着被藏掉。"""
        for sec in ("position-builder", "abnormal-pool"):
            i = _HTML.index(f'data-sec="{sec}"')
            h2 = _HTML.rfind("<h2", 0, i)
            assert h2 > 0 and _HTML.find("</h2>", i) > i


class TestCollapseBehaviour:
    def test_css_hides_body_keeps_h2(self):
        assert ".section.collapsed > *:not(h2)" in _CSS
        assert "display: none !important" in _CSS.split(".section.collapsed")[1][:200]

    def test_state_persisted(self):
        assert 'const SEC_LS_KEY = "secCollapsed"' in _JS
        i = _JS.index("function toggleSection")
        seg = _JS[i:i + 900]
        assert "localStorage.setItem" in seg, "折叠状态要落 localStorage"
        assert "classList" in seg and "collapsed" in seg

    def test_restored_on_load(self):
        assert "function _secRestore" in _JS
        assert "_secRestore();" in _JS, "加载时要恢复上次状态"

    def test_no_dom_destroy(self):
        """只切 class，不许把内容 innerHTML 清空（否则展开要重算/丢滚动位置）。"""
        i = _JS.index("function _secApply")
        seg = _JS[i:i + 500]
        assert "innerHTML" not in seg
        assert "classList.toggle" in seg
