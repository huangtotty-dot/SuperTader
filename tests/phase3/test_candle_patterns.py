# -*- coding: utf-8 -*-
"""K线形态自动标注 `detectCandlePatterns`（2026-11-14，调研 TradingView「自动形态识别」后落地）。

纯函数：输入 ECharts candlestick 序列 `[open, close, low, high]`，输出 `[{index,name,dir,side}]`。
用 node 直接跑 app.js 里的真函数（不是复刻逻辑），与 test_chart_gap_window 同套路。

口径（钉住，勿改判定阈值除非同步改 app.js）：
  · 十字星   实体/振幅 < 0.1 且上下影都 > 实体              ⇒ neutral / top
  · 锤子     下影 ≥ 2×实体 且 上影 ≤ 0.5×实体               ⇒ bull / bottom（潜在见底）
  · 射击之星 上影 ≥ 2×实体 且 下影 ≤ 0.5×实体               ⇒ bear / top（潜在见顶）
  · 阳吞没   前阴后阳、当前实体完全包住前一根                ⇒ bull / bottom
  · 阴吞没   前阳后阴、当前实体完全包住前一根                ⇒ bear / top
  · 孕线     前一根实体 ≥ 2×当前、当前实体完全在前一根之内    ⇒ 方向取前一根的反向
  · 只标注不给买卖信号（dir 是方向语义，不是交易动作）
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")

NODE = shutil.which("node")


def _extract():
    i = _JS.index("function detectCandlePatterns")
    j = _JS.index("function buildVolumeProfile", i)
    return _JS[i:j]


def _run(ohlc):
    code = _extract() + f"\nconsole.log(JSON.stringify(detectCandlePatterns({json.dumps(ohlc)})));"
    with tempfile.TemporaryDirectory() as td:
        fp = Path(td) / "t.js"
        fp.write_text(code, encoding="utf-8")
        r = subprocess.run([NODE, str(fp)], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode == 0, r.stderr[:400]
    return json.loads(r.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "没有 node，跳过行为测试")
class TestPatternBehavior(unittest.TestCase):
    def test_十字星(self):
        out = _run([[10, 10.2, 9.9, 10.3], [10, 10, 9, 11]])
        self.assertEqual(out, [{"index": 1, "name": "十字星", "dir": "neutral", "side": "top"}])

    def test_锤子(self):
        out = _run([[10, 10.2, 9.9, 10.3], [10, 9.9, 9.7, 10.05]])
        self.assertIn({"index": 1, "name": "锤子", "dir": "bull", "side": "bottom"}, out)

    def test_射击之星(self):
        out = _run([[10, 10.2, 9.9, 10.3], [10, 10.1, 9.95, 10.3]])
        self.assertIn({"index": 1, "name": "射击之星", "dir": "bear", "side": "top"}, out)

    def test_阳吞没(self):
        out = _run([[10.5, 10, 9.9, 10.6], [9.8, 10.7, 9.7, 10.8]])
        self.assertIn({"index": 1, "name": "阳吞没", "dir": "bull", "side": "bottom"}, out)

    def test_阴吞没(self):
        out = _run([[10, 10.5, 9.9, 10.6], [10.7, 9.7, 9.6, 10.8]])
        self.assertIn({"index": 1, "name": "阴吞没", "dir": "bear", "side": "top"}, out)

    def test_孕线(self):
        out = _run([[10, 11, 9.9, 11.1], [10.4, 10.6, 10.3, 10.7]])
        self.assertIn({"index": 1, "name": "孕线", "dir": "bear", "side": "top"}, out)

    def test_空与单根不炸(self):
        self.assertEqual(_run([]), [])
        self.assertEqual(_run([[10, 11, 9, 12]]), [], "单根序列循环从 index 1 起，不标首根")

    def test_首根不被标(self):
        # 首根就算本身是锤子/十字星也不标（循环从 index 1 起，吞没/孕线需要前一根）
        out = _run([[10, 9.9, 9.7, 10.05], [10, 10.2, 9.9, 10.3]])
        self.assertFalse(any(p["index"] == 0 for p in out), "首根不得标形态")


class TestPatternWiring(unittest.TestCase):
    def test_开关存在且接_chartToggleChanged(self):
        found = False
        for ln in _HTML.splitlines():
            if 'id="tgPattern"' in ln:
                found = True
                self.assertIn("chartToggleChanged()", ln, "形态开关要接 chartToggleChanged（主图+多窗口一起刷新）")
        self.assertTrue(found, "index.html 缺 id=tgPattern 开关")

    def test_调用点存在(self):
        self.assertIn("detectCandlePatterns(period.ohlc)", _JS, "主图 K线 series 必须调用形态检测")

    def test_空标记给空数组以清旧标记(self):
        # 普通 merge 下 markPoint: undefined 清不掉旧标记 ⇒ 必须给 {data:[]}
        self.assertIn("if (!pats.length) return { data: [] };", _JS)

    def test_版本串一致(self):
        m = re.search(r'app\.js\?v=(\d{8}[a-z])', _HTML)
        self.assertTrue(m, "index.html 缺 app.js?v= 版本串")
        self.assertIn(f"app_boot_{m.group(1)}", _JS, "app.js 的 app_boot_ 版本必须与 index.html 对齐")


if __name__ == "__main__":
    unittest.main(verbosity=2)
