# -*- coding: utf-8 -*-
"""每日大盘复盘子进程化（2026-11-23）的离线单元验证。

不联网、不起真子进程：mock subprocess.Popen 与 core.market_review 验证
  ① run_daily_review 拉起子进程的参数/日志重定向/api_key 不上命令行；
  ② 防重入（运行中早退）、残留进程收尸、启动失败、配置不完整拒跑；
  ③ monitor 线程的结果回收（正常/worker 报错透传/缺文件/过期文件/超时强杀）；
  ④ daily_review_progress 读状态档（运行中流式文本、无档回退、异日期档忽略、
     空闲直返且恰好三字段——前端契约）；
  ⑤ worker 端：状态档 running→定格、结果档落盘、退出码、看门狗接线、运行中中间档；
  ⑥ subprocess_job 公共件：原子写 / pid_alive / parent_watch / wait_with_timeout。

模式照抄 tests/phase3/test_hunter_subprocess.py（P2-4 基线，必须保持全绿）。
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "stock_hunter"))

import gui  # noqa: E402
from core import review_worker, subprocess_job  # noqa: E402

_CFG_OK = {"ok": True, "saved": True, "base_url": "http://x", "model": "m",
           "api_key": "k", "reasoning_effort": ""}


class _FakeProc:
    """可控假子进程：wait() 阻塞在 event 上，可脚本化退出码/完工写结果/挂死。"""

    def __init__(self, rc=0, on_wait=None, block=True, hang=False):
        self.pid = 43210
        self._rc = rc
        self._on_wait = on_wait
        self._done = threading.Event()
        self._hang = hang          # True ⇒ wait(timeout) 到点抛 TimeoutExpired（模拟 LLM 挂死）
        self.terminated = False
        self.killed = False
        if not block:
            self._done.set()

    def wait(self, timeout=None):
        if self._on_wait:
            self._on_wait()          # 模拟 worker 完工：写结果档后再退出
            self._on_wait = None
            self._done.set()
        if self._hang and not self._done.is_set():
            if timeout is None:
                self._done.wait(10)
            elif not self._done.wait(timeout):
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
            return self._rc
        self._done.wait(timeout if timeout is not None else 10)
        return self._rc

    def poll(self):
        return self._rc if self._done.is_set() else None

    def terminate(self):
        self.terminated = True
        self._done.set()

    def kill(self):
        self.killed = True
        self._done.set()


class _CaseBase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        t = Path(self.td.name)
        self._saved = {
            "state": dict(gui._REVIEW_RUN_STATE),
            "status_fp": gui._REVIEW_STATUS_FP,
            "result_fp": gui._REVIEW_RESULT_FP,
            "logs": gui.LOGS_DIR,
            "proc": dict(gui._REVIEW_PROC),
            "timeout": gui._REVIEW_TIMEOUT_SEC,
        }
        gui._REVIEW_RUN_STATE.clear()
        gui._REVIEW_RUN_STATE.update({"running": False, "error": None, "output": "", "date": None})
        gui._REVIEW_STATUS_FP = t / "review_run_status.json"
        gui._REVIEW_RESULT_FP = t / "review_run_result.json"
        gui.LOGS_DIR = t / "logs"
        with gui._REVIEW_PROC_LOCK:
            gui._REVIEW_PROC["proc"] = None
            gui._REVIEW_PROC["log_fp"] = None
        self.api = gui.Api()
        self.procs = []
        self._pop = mock.patch.object(gui.subprocess, "Popen", side_effect=self._mk)
        self._pop.start()
        # save_llm_config 会写真配置 t_io/state/llm_config.json ⇒ mock 掉
        # （离线 + 不碰生产数据；run_daily_review 惰性 import，吃模块属性补丁）
        self._save_mock = mock.Mock(return_value=dict(_CFG_OK))
        self._cfg = mock.patch("core.market_review.save_llm_config", self._save_mock)
        self._cfg.start()

    def _mk(self, cmd, **kw):
        """Popen 工厂：记录 argv/kw，返回可脚本化的 _FakeProc。"""
        self.last_cmd = cmd
        self.last_kw = kw
        p = self.proc_factory() if hasattr(self, "proc_factory") else _FakeProc()
        self.procs.append(p)
        return p

    def tearDown(self):
        self._cfg.stop()
        self._pop.stop()
        for p in self.procs:
            try:
                p.terminate()        # 释放 monitor 线程的 proc.wait()
            except Exception:
                pass
        self._wait_done(3.0)         # 等 monitor 收尾（关日志句柄），防 WinError 32
        gui._REVIEW_RUN_STATE.clear()
        gui._REVIEW_RUN_STATE.update(self._saved["state"])
        gui._REVIEW_STATUS_FP = self._saved["status_fp"]
        gui._REVIEW_RESULT_FP = self._saved["result_fp"]
        gui.LOGS_DIR = self._saved["logs"]
        gui._REVIEW_TIMEOUT_SEC = self._saved["timeout"]
        with gui._REVIEW_PROC_LOCK:
            gui._REVIEW_PROC.update(self._saved["proc"])
        self.td.cleanup()

    def _wait_done(self, sec=5.0):
        t0 = time.time()
        while gui._REVIEW_RUN_STATE.get("running") and time.time() - t0 < sec:
            time.sleep(0.02)
        return not gui._REVIEW_RUN_STATE.get("running")

    def _run_review(self, date="2026-11-20"):
        return self.api.run_daily_review(date, "http://x", "m", "sk-SECRET-XYZ")


class TestLaunch(_CaseBase):
    def test_run_daily_review_launches_subprocess(self):
        r = self._run_review()
        self.assertEqual(r, {"status": "running"})               # 契约：恰好这一个键
        cmd = self.last_cmd
        self.assertEqual(cmd[0], sys.executable)                  # 用同一解释器
        self.assertTrue(cmd[1].replace("/", "\\").endswith("core\\review_worker.py"))
        self.assertIn("--date", cmd) and self.assertIn("2026-11-20", cmd)
        self.assertIn("--parent-pid", cmd)                        # 父进程看门狗
        self.assertEqual(str(gui._REVIEW_STATUS_FP), cmd[cmd.index("--status-file") + 1])
        self.assertEqual(str(gui._REVIEW_RESULT_FP), cmd[cmd.index("--result-file") + 1])
        # api_key 不上命令行（进程列表对同机任何用户可见）
        self.assertFalse(any("sk-SECRET-XYZ" in str(a) for a in cmd))
        self.assertEqual(self.last_kw.get("cwd"), str(gui.BASE))
        self.assertIs(self.last_kw.get("stderr"), gui.subprocess.STDOUT)
        fp = self.last_kw.get("stdout")                           # 输出落日志文件
        self.assertTrue(getattr(fp, "name", "").startswith(str(gui.LOGS_DIR)))
        self.assertIn("review_run_", fp.name)
        st = json.loads(gui._REVIEW_STATUS_FP.read_text(encoding="utf-8"))
        self.assertTrue(st["running"] and st["date"] == "2026-11-20")
        self.assertIn("启动复盘子进程", st["output"])              # 首拍父进程先落
        self.assertTrue(gui._REVIEW_RUN_STATE["running"])
        self.assertIsNone(gui._REVIEW_RUN_STATE["error"])

    def test_reentry_while_running_returns_early(self):
        self._run_review("2026-11-20")
        r = self._run_review("2026-11-21")   # 单状态档 ⇒ 异日期也早退（同旧线程版语义）
        self.assertEqual(r, {"status": "running", "message": "已有复盘进行中"})
        self.assertEqual(len(self.procs), 1, "运行中重入不得再起子进程")

    def test_config_incomplete_returns_error(self):
        self._save_mock.return_value = {"ok": True, "saved": False}
        r = self._run_review()
        self.assertEqual(r["status"], "error")
        self.assertIn("模型配置不完整", r["message"])
        self.assertEqual(len(self.procs), 0, "配置不完整不得起子进程")
        self.assertFalse(gui._REVIEW_RUN_STATE["running"])

    def test_zombie_proc_reaped_before_relaunch(self):
        self._run_review()
        old = self.procs[0]
        gui._REVIEW_RUN_STATE["running"] = False   # 模拟 monitor 漏收尾：状态复位但进程还活着
        r = self._run_review()
        self.assertEqual(r["status"], "running")
        self.assertTrue(old.terminated, "残留活进程必须先 terminate 再起跑")
        self.assertEqual(len(self.procs), 2)

    def test_launch_failure(self):
        self._pop.stop()
        with mock.patch.object(gui.subprocess, "Popen", side_effect=OSError("boom")):
            r = self._run_review()
        self.assertEqual(r["status"], "error")
        self.assertIn("boom", r["message"])
        self.assertFalse(gui._REVIEW_RUN_STATE["running"])
        self.assertIn("复盘子进程启动失败", gui._REVIEW_RUN_STATE["error"])
        self._pop.start()


class TestMonitor(_CaseBase):
    def test_result_collected(self):
        def _finish():
            gui._write_json_atomic(gui._REVIEW_RESULT_FP,
                                   {"date": "2026-11-20", "ok": True,
                                    "result": {"output": "① 收集…\n复盘全文", "error": None}})

        self.proc_factory = lambda: _FakeProc(rc=0, on_wait=_finish)
        self._run_review()
        self.assertTrue(self._wait_done(), "monitor 未在限时内收尾")
        self.assertIsNone(gui._REVIEW_RUN_STATE["error"])
        self.assertEqual(gui._REVIEW_RUN_STATE["output"], "① 收集…\n复盘全文")

    def test_worker_error_propagates(self):
        def _finish():
            gui._write_json_atomic(gui._REVIEW_RESULT_FP,
                                   {"date": "2026-11-20", "ok": False,
                                    "result": {"output": "① 收集到一半",
                                               "error": "复盘子进程未捕获异常: RuntimeError: LLM 挂"}})

        self.proc_factory = lambda: _FakeProc(rc=1, on_wait=_finish)
        self._run_review()
        self.assertTrue(self._wait_done())
        self.assertIn("LLM 挂", gui._REVIEW_RUN_STATE["error"])
        self.assertEqual(gui._REVIEW_RUN_STATE["output"], "① 收集到一半")   # 部分文本不丢

    def test_missing_result_file_reports_error(self):
        self.proc_factory = lambda: _FakeProc(rc=9, block=False)
        self._run_review()
        self.assertTrue(self._wait_done())
        self.assertIn("子进程异常退出", gui._REVIEW_RUN_STATE["error"])
        # 状态档首拍文本被保留为排障上下文
        self.assertIn("启动复盘子进程", gui._REVIEW_RUN_STATE["output"])

    def test_stale_result_file_rejected(self):
        # 上一轮残留的结果档（日期不符）⇒ 不得当成本轮结果
        gui._write_json_atomic(gui._REVIEW_RESULT_FP,
                               {"date": "2026-11-01", "ok": True,
                                "result": {"output": "旧复盘", "error": None}})
        self.proc_factory = lambda: _FakeProc(rc=0, block=False)
        self._run_review()
        self.assertTrue(self._wait_done())
        self.assertIn("子进程异常退出", gui._REVIEW_RUN_STATE["error"])

    def test_timeout_terminates_proc(self):
        gui._REVIEW_TIMEOUT_SEC = 0.2          # 硬超时（父侧 monitor 权威）
        self.proc_factory = lambda: _FakeProc(rc=-9, hang=True)   # 模拟 LLM 挂死
        self._run_review()
        self.assertTrue(self._wait_done(5.0))
        self.assertTrue(self.procs[0].terminated, "超时必须先 terminate")
        self.assertIn("超时", gui._REVIEW_RUN_STATE["error"])
        self.assertIn("启动复盘子进程", gui._REVIEW_RUN_STATE["output"])   # 死前状态档留作上下文


class TestProgress(_CaseBase):
    def test_progress_reads_status_file_while_running(self):
        gui._REVIEW_RUN_STATE.update({"running": True, "date": "2026-11-20"})
        gui._write_json_atomic(gui._REVIEW_STATUS_FP,
                               {"running": True, "date": "2026-11-20", "pid": 1,
                                "output": "④ 正在调用模型…", "ts": time.time()})
        p = self.api.daily_review_progress()
        self.assertEqual(p, {"running": True, "error": None, "output": "④ 正在调用模型…"})

    def test_progress_falls_back_when_file_missing(self):
        gui._REVIEW_RUN_STATE.update({"running": True, "date": "2026-11-20", "output": "进程内"})
        p = self.api.daily_review_progress()
        self.assertEqual(p["output"], "进程内")       # 无状态档 ⇒ 进程内兜底
        self.assertTrue(p["running"] and p["error"] is None)

    def test_progress_stale_date_file_ignored(self):
        gui._REVIEW_RUN_STATE.update({"running": True, "date": "2026-11-20", "output": "进程内"})
        gui._write_json_atomic(gui._REVIEW_STATUS_FP,
                               {"running": True, "date": "2026-11-19", "pid": 1,
                                "output": "旧日期档", "ts": time.time()})
        p = self.api.daily_review_progress()
        self.assertEqual(p["output"], "进程内")       # 异日期档 ⇒ 忽略，回退进程内

    def test_progress_idle_returns_state_exact_keys(self):
        gui._REVIEW_RUN_STATE.update({"running": False, "error": "x", "output": "y",
                                      "date": "2026-11-20"})
        p = self.api.daily_review_progress()
        # 前端契约（app.js pollReviewProgress）：恰好 {running, error, output} 三字段
        self.assertEqual(set(p.keys()), {"running", "error", "output"})
        self.assertEqual((p["running"], p["error"], p["output"]), (False, "x", "y"))


class TestWorkerMain(unittest.TestCase):
    """worker 端到端（sys.modules 注入假 core.market_review）：

    状态档 running→定格、结果档落盘、退出码、看门狗接线、运行中中间档可读。
    """

    def _run(self, stream_impl, td, extra_argv=None):
        import types
        sf = str(Path(td) / "status.json")
        rf = str(Path(td) / "result.json")
        fake_mr = types.ModuleType("core.market_review")
        fake_mr.load_llm_config = lambda: {"base_url": "http://x", "model": "m", "api_key": "k"}
        fake_mr.run_market_review_stream = stream_impl
        argv = ["review_worker.py", "--date", "2026-11-20",
                "--status-file", sf, "--result-file", rf] + list(extra_argv or [])
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(sys.modules, {"core.market_review": fake_mr}):
            rc = review_worker.main()
        return rc, sf, rf

    def test_success_path(self):
        def _impl(date, cfg, on_text):
            self.assertEqual(cfg["model"], "m")           # 配置来自 load_llm_config（非命令行）
            on_text("① 正在收集…\n")
            time.sleep(0.6)                                # 让 status-writer 至少落一拍中间进度
            on_text("④ 模型输出…复盘全文")
            return "复盘全文"

        with tempfile.TemporaryDirectory() as td:
            rc, sf, rf = self._run(_impl, td)
            self.assertEqual(rc, 0)
            r = json.loads(Path(rf).read_text(encoding="utf-8"))
            self.assertTrue(r["ok"] and r["date"] == "2026-11-20")
            self.assertEqual(r["result"]["output"], "① 正在收集…\n④ 模型输出…复盘全文")
            self.assertIsNone(r["result"]["error"])
            s = json.loads(Path(sf).read_text(encoding="utf-8"))
            self.assertFalse(s["running"])                 # 状态档最后定格
            self.assertEqual(s["output"], r["result"]["output"])
            self.assertIsNone(s["error"])

    def test_stream_exception_writes_error_result(self):
        def _impl(date, cfg, on_text):
            on_text("① 正在收集…\n")
            raise RuntimeError("LLM 挂死")

        with tempfile.TemporaryDirectory() as td:
            rc, sf, rf = self._run(_impl, td)
            self.assertEqual(rc, 1)
            r = json.loads(Path(rf).read_text(encoding="utf-8"))
            self.assertFalse(r["ok"])
            self.assertIn("LLM 挂死", r["result"]["error"])
            self.assertIn("① 正在收集", r["result"]["output"])   # 部分文本不丢
            s = json.loads(Path(sf).read_text(encoding="utf-8"))
            self.assertFalse(s["running"])
            self.assertIn("LLM 挂死", s["error"])

    def test_watchdog_wired_with_parent_pid(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(review_worker._sj, "start_parent_watchdog") as mw:
                rc, _, _ = self._run(lambda d, c, o: "x", td,
                                     extra_argv=["--parent-pid", "12345"])
            self.assertEqual(rc, 0)
            mw.assert_called_once()
            self.assertEqual(mw.call_args[0][0], 12345)

    def test_no_watchdog_without_parent_pid(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(review_worker._sj, "start_parent_watchdog") as mw:
                rc, _, _ = self._run(lambda d, c, o: "x", td)
            self.assertEqual(rc, 0)
            mw.assert_not_called()

    def test_intermediate_status_readable_during_run(self):
        # 运行中读状态档：running=True + 已流出的部分文本（前端 800ms 轮询的数据源）
        with tempfile.TemporaryDirectory() as td:
            sf = str(Path(td) / "status.json")             # 与 _run 内部同式
            seen = {}

            def _impl(date, cfg, on_text):
                on_text("① 正在收集…\n")
                deadline = time.time() + 3
                while time.time() < deadline:
                    if Path(sf).exists():
                        d = json.loads(Path(sf).read_text(encoding="utf-8"))
                        if d.get("output"):
                            # 等到流出文本落盘的那一拍（writer 首拍可能是空串——
                            # 它在线程启动即跑第一轮，可能早于 on_text）
                            seen.update(d)
                            break
                    time.sleep(0.05)
                return "x"

            rc, _, _ = self._run(_impl, td)
            self.assertEqual(rc, 0)
            self.assertTrue(seen.get("running"))
            self.assertEqual(seen.get("output"), "① 正在收集…\n")
            self.assertEqual(seen.get("date"), "2026-11-20")
            self.assertTrue(seen.get("pid"))


class TestSubprocessJobUnits(unittest.TestCase):
    """core/subprocess_job 公共件单元（hunter_worker 私有副本的同源抽出版）。"""

    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "sub" / "s.json"
            subprocess_job.atomic_write_json(fp, {"a": 1, "中文": "✓"})
            self.assertEqual(json.loads(fp.read_text(encoding="utf-8")), {"a": 1, "中文": "✓"})
            self.assertFalse(Path(str(fp) + ".tmp").exists())

    def test_pid_alive_self_and_dead(self):
        import os
        self.assertTrue(subprocess_job.pid_alive(os.getpid()))
        self.assertFalse(subprocess_job.pid_alive(2 ** 30 - 7))   # 远超 pid_max
        self.assertTrue(subprocess_job.pid_alive(0))              # 未传父 PID ⇒ 不看门

    def test_utf8_console_fix_no_crash(self):
        subprocess_job.utf8_console_fix()

    def test_parent_watch_exits_when_parent_dead(self):
        # os._exit 打桩成 SystemExit：桩退出后线程即死，不会回头调真 _exit 杀掉测试进程
        with mock.patch.object(subprocess_job.os, "_exit", side_effect=SystemExit(3)) as mx:
            th = threading.Thread(target=subprocess_job.parent_watch,
                                  args=(2 ** 30 - 7, 0.02, "t"), daemon=True)
            th.start()
            th.join(3)
        mx.assert_called_once_with(3)

    def test_status_writer_skips_unchanged(self):
        # 文本没变不落盘（LLM 思考停顿期省小文件写放大）：mtime 应保持不变
        with tempfile.TemporaryDirectory() as td:
            fp = str(Path(td) / "s.json")
            with review_worker._STREAM_LOCK:
                review_worker._STREAM["output"] = "A"
            stop = threading.Event()
            th = threading.Thread(target=review_worker._status_writer,
                                  args=(stop, "2026-11-20", fp, 0.05), daemon=True)
            th.start()
            deadline = time.time() + 2
            while not Path(fp).exists() and time.time() < deadline:
                time.sleep(0.03)
            self.assertTrue(Path(fp).exists(), "status-writer 首拍未落盘")
            m1 = Path(fp).stat().st_mtime_ns
            time.sleep(0.3)          # 多个 tick 文本未变 ⇒ 不应再写
            m2 = Path(fp).stat().st_mtime_ns
            stop.set()
            th.join(2)
            self.assertEqual(m1, m2, "文本未变不应重复写盘")
            d = json.loads(Path(fp).read_text(encoding="utf-8"))
            self.assertTrue(d["running"] and d["date"] == "2026-11-20" and d["pid"])
            self.assertEqual(d["output"], "A")
        with review_worker._STREAM_LOCK:     # 复位模块级缓冲，防污染后续用例
            review_worker._STREAM["output"] = ""

    def test_wait_with_timeout_fast_return(self):
        class P:
            def wait(self, timeout=None):
                return 0
        rc, timed_out = subprocess_job.wait_with_timeout(P(), 5)
        self.assertEqual((rc, timed_out), (0, False))

    def test_wait_with_timeout_disabled(self):
        class P:
            def wait(self, timeout=None):
                self.seen = timeout
                return 3
        p = P()
        rc, timed_out = subprocess_job.wait_with_timeout(p, 0)
        self.assertEqual((rc, timed_out), (3, False))
        self.assertIsNone(p.seen)            # 不限时 ⇒ 纯 wait（不传 timeout）

    def test_wait_with_timeout_terminate_suffices(self):
        class P:
            def __init__(self):
                self.n = 0
                self.terminated = False

            def wait(self, timeout=None):
                self.n += 1
                if self.n == 1:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return 0                     # terminate 后第二次 wait 正常退出

            def terminate(self):
                self.terminated = True

            def kill(self):
                raise AssertionError("terminate 已够用，不该走到 kill")
        p = P()
        rc, timed_out = subprocess_job.wait_with_timeout(p, 0.1, kill_after=0.05)
        self.assertEqual((rc, timed_out), (0, True))
        self.assertTrue(p.terminated)

    def test_wait_with_timeout_escalates_to_kill(self):
        class P:
            def __init__(self):
                self.terminated = False
                self.killed = False

            def wait(self, timeout=None):
                if self.killed:
                    return -9
                raise subprocess.TimeoutExpired("fake", timeout)

            def terminate(self):
                self.terminated = True       # terminate 无效（仍挂）

            def kill(self):
                self.killed = True
        p = P()
        rc, timed_out = subprocess_job.wait_with_timeout(p, 0.1, kill_after=0.05)
        self.assertEqual((rc, timed_out), (-9, True))
        self.assertTrue(p.terminated and p.killed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
