# -*- coding: utf-8 -*-
"""线程级 CPU 采样：`proc` 字段与按进程汇总（2026-10-30）。

背景：此前只有 GUI 挂了 `core.thread_watch`；owner 批准后 main 也挂上，两个进程
**追加同一份** `thread_cpu.jsonl` ⇒ 记录必须带 `proc`，报表必须按 `proc/线程` 归并，
否则 main 的 314% 与 GUI 的线程名混在一起，等于没归因。

运行：python -m pytest -q tests/phase3/test_thread_watch_proc.py
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import thread_watch as tw  # noqa: E402
from scripts.experiment import cpu_sampler as cs  # noqa: E402


class TestProcField(unittest.TestCase):
    def test_make_record带proc(self):
        r = tw.make_record([{"name": "pb-struct_0", "pct": 12.3, "threads": 1}],
                           60.0, label="main", ts="2026-10-30 14:00:00")
        self.assertEqual(r["proc"], "main")
        self.assertEqual(r["ts"], "2026-10-30 14:00:00")
        self.assertEqual(r["total_pct"], 12.3)
        self.assertEqual(r["window_s"], 60.0)

    def test_无label时proc为空串(self):
        self.assertEqual(tw.make_record([], 1.0)["proc"], "")

    def test_报表按proc归并(self):
        recs = [
            {"ts": "2026-10-30 14:00:00", "proc": "gui",
             "top": [{"name": "ThreadPoolExecutor", "pct": 40.0, "threads": 4}]},
            {"ts": "2026-10-30 14:00:00", "proc": "main",
             "top": [{"name": "ThreadPoolExecutor", "pct": 55.0, "threads": 8}]},
            {"ts": "2026-10-30 14:01:00", "proc": "",          # 旧记录（无 proc）
             "top": [{"name": "tags_0", "pct": 5.0, "threads": 1}]},
        ]
        with tempfile.TemporaryDirectory() as td:
            fp = os.path.join(td, "t.jsonl")
            with open(fp, "w", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                cs.cmd_report_threads(fp)
        out = buf.getvalue()
        self.assertIn("gui/ThreadPoolExecutor", out)
        self.assertIn("main/ThreadPoolExecutor", out)
        self.assertIn("tags_0", out, "旧记录（无 proc）按原样显示")


if __name__ == "__main__":
    unittest.main(verbosity=2)
