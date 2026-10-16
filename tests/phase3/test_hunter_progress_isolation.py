# -*- coding: utf-8 -*-
"""猎手进度条**不被别的任务抢**的回归（2026-10-16）。**全离线**。

## 缺陷（owner 当场截图报回）

选股猎手跑的时候，进度条写「**50/264 只**」「150/**257** 只」，owner 问「为什么只有 200 多支？」
—— 而**池子其实是 1575 只**（实测 `[NET] 获取历史行情 …，1575 只` + `已缓存 market_20261016.csv (1575 只)`）。

根因**不在池子，在进度**：`MARKET_PROGRESS` 是**进程内全局**，而 `_hunter_build_conformance`
有**三个**调用方，各自带**自己的**码表：

| 调用方 | 码表 | 会不会写进度 |
|---|---|---|
| `_load_hunter_impl`（猎手主流程） | 池子 **1575** | ✅ 该写 |
| `_beacon_enrich`（风向标：各板块龙头+中军） | **~264** | ❌ 不该写 |
| `_go_hist_compute`（GO 历史回算） | 另一套 | ❌ 不该写 |

⇒ 风向标一跑，就把猎手进度条的 `total` 顶成 **264**（换个时刻是 257 —— 那是**它的码表在变**，
所以每次看数字都不一样，池子却纹丝不动）。截图里 `phase` 写着"构建板块/个股明细"（猎手设的）、
`msg` 却是"计算建仓符合度 50/264"（风向标设的）—— **两个来源叠在同一条进度上**，这就是指纹。

修法：加 `report_progress` 开关，**只有猎手主流程传 True**。

运行：python -m unittest tests.phase3.test_hunter_progress_isolation -v
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import t_gui  # noqa: E402
from modules.market_data import MARKET_PROGRESS as MP  # noqa: E402

_SRC = (_ROOT / "t_gui.py").read_text(encoding="utf-8")
# ⚠️ 判据看**代码**不看注释：本文件 docstring 里就写着这些调用点。
_SRC_CODE = re.sub(r'"""[\s\S]*?"""', " ", _SRC)
_SRC_CODE = re.sub(r"(?m)#.*$", " ", _SRC_CODE)

_SENTINEL = {"running": False, "phase": "哨兵", "done": 7, "total": 999, "msg": "不许被覆盖"}
_CODES = ["600519", "000001", "601318"]      # 有日线缓存，跑得动也够快


class TestHunterProgressIsolation(unittest.TestCase):
    def setUp(self):
        self._snap = dict(MP)

    def tearDown(self):
        MP.update(self._snap)

    def _sentinel(self):
        MP.update(dict(_SENTINEL))

    def test_foreign_call_does_not_touch_progress(self):
        """风向标/GO 回算那种调用（默认 report_progress=False）必须**一个字都不改**进度。"""
        self._sentinel()
        t_gui.Api()._hunter_build_conformance(_CODES, "2026-10-16")
        self.assertEqual(dict(MP), _SENTINEL,
                         "非猎手调用把猎手进度顶掉了 —— 面板就会显示成别人的码表规模（就是那个 264）")

    def test_hunter_call_still_reports(self):
        """猎手主流程传 True ⇒ 照旧写进度（别把功能一起修没了）。"""
        self._sentinel()
        t_gui.Api()._hunter_build_conformance(_CODES, "2026-10-16", report_progress=True)
        self.assertEqual(MP.get("total"), len(_CODES))
        self.assertIn("计算建仓符合度", MP.get("msg") or "")

    def test_only_the_hunter_path_passes_true(self):
        """钉住"只有猎手传 True" —— 否则等于没修。"""
        hit = re.findall(r"_hunter_build_conformance\([^)]*report_progress=True\)", _SRC_CODE)
        self.assertEqual(len(hit), 1, f"应恰好一处传 True，实际 {len(hit)}: {hit}")

    def test_known_foreign_callers_do_not_pass_true(self):
        for fn in ("_beacon_enrich", "_go_hist_compute"):
            a = _SRC_CODE.index("def %s(" % fn)
            b = _SRC_CODE.index("\n    def ", a + 1)
            body = _SRC_CODE[a:b]
            self.assertIn("_hunter_build_conformance(", body, f"{fn} 里没有该调用？")
            self.assertNotIn("report_progress=True", body,
                             f"{fn} 不该写猎手进度（它带的是自己的码表）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
