# -*- coding: utf-8 -*-
"""Q-20260914-3'/4' 回归：收盘复盘子进程退出码守护 + forward_tracker 报告路径（2026-09-14）。

3' 原实现 fire-and-forget，子进程 rc 不查 → 中途崩当天静默断档（次日复盘才发现）。
4' forward_tracker 缺省路径 doc/每日复盘/ 整目录不存在 → 每日空转，前瞻表回填从未执行。

本测试从 main.py 源码抽取**真实的 `_py` 模板字符串**（防测试与生产漂移），用假子进程替换
daily_review.py / forward_tracker.py，断言：
  ① 全 0 → 外层 rc=0；② 任一非 0 → 外层 rc=1（守护线程据此撤占位重试）；
  ③ forward_tracker 收到的 --report = doc/review/dailyReview/{date}_复盘.md。

运行：python t_io/validation/daily_review/test_review_child_guard.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
MAIN_PY = os.path.join(_ROOT, "main.py")
DATE = "2026-09-14"

_FAKE = '''# -*- coding: utf-8 -*-
import json, os, sys
rec = os.environ["FAKE_REC"]
with open(rec, "a", encoding="utf-8") as f:
    f.write(json.dumps({"script": os.path.basename(__file__), "argv": sys.argv[1:]}) + "\\n")
fail_marker = os.environ.get("FAKE_FAIL", "")
if fail_marker and fail_marker in __file__:
    sys.exit(3)
sys.exit(0)
'''


def _extract_py_template():
    """从 main.py 抽出 `_py = (...)` 的字符串拼接块并求值（需要 dr/base/log 三个名字）。"""
    with open(MAIN_PY, encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"\n(\s+)_py = \(\n(.*?)\n\s+\)\n", src, re.S)
    if not m:
        raise AssertionError("未能从 main.py 抽取 _py 模板（结构已变，请同步本测试）")
    # 保留外层括号：块内是隐式字符串拼接（相邻字面量），须在同一表达式内求值
    return "(\n" + m.group(2) + "\n)"


class TestReviewChildGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="review_guard_")
        self._base = os.path.join(self._tmp, "base")
        self._dr = os.path.join(self._base, "t_io", "validation", "daily_review")
        os.makedirs(self._dr, exist_ok=True)
        for name in ("daily_review.py", "forward_tracker.py"):
            with open(os.path.join(self._dr, name), "w", encoding="utf-8") as f:
                f.write(_FAKE)
        self._log = os.path.join(self._tmp, "review.log")
        self._rec = os.path.join(self._tmp, "rec.jsonl")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, fail_marker=""):
        env = dict(os.environ, FAKE_REC=self._rec, FAKE_FAIL=fail_marker)
        body = _extract_py_template()
        py = eval(body, {"_dr_dir": self._dr, "BASE_DIR": self._base,  # noqa: S307
                         "log_fp": self._log})
        proc = subprocess.run([sys.executable, "-c", py, DATE], env=env,
                              capture_output=True, text=True, timeout=120)
        recs = []
        if os.path.exists(self._rec):
            with open(self._rec, encoding="utf-8") as f:
                recs = [json.loads(ln) for ln in f if ln.strip()]
        return proc, recs

    def test_all_zero_exit_code(self):
        proc, recs = self._run()
        self.assertEqual(proc.returncode, 0, f"全 0 应 rc=0，实得 {proc.returncode}\n{proc.stdout}")
        self.assertEqual([r["script"] for r in recs], ["daily_review.py", "forward_tracker.py"])

    def test_child_failure_propagates(self):
        proc, _ = self._run(fail_marker="forward_tracker")
        self.assertEqual(proc.returncode, 1,
                         f"子进程 rc=3 应传导为外层 rc=1（守护线程据此撤占位重试），实得 {proc.returncode}")
        self.assertIn("FAILED", proc.stdout)

    def test_forward_tracker_gets_real_report_path(self):
        _, recs = self._run()
        fwd = [r for r in recs if r["script"] == "forward_tracker.py"][0]
        argv = fwd["argv"]
        self.assertIn("--report", argv, "forward_tracker 未收到 --report（路径空转复发）")
        report = argv[argv.index("--report") + 1]
        expected = os.path.join(self._base, "doc", "review", "复盘清单.md")
        self.assertEqual(os.path.normpath(report), os.path.normpath(expected))
        self.assertNotIn("每日复盘", report, "仍指向已不存在的旧目录")

    def test_daily_review_gets_no_report_flag(self):
        _, recs = self._run()
        dr = [r for r in recs if r["script"] == "daily_review.py"][0]
        self.assertNotIn("--report", dr["argv"], "daily_review.py 不吃 --report，传了会启动即失败")


if __name__ == "__main__":
    unittest.main(verbosity=2)
