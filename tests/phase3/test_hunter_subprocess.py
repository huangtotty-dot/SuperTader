# -*- coding: utf-8 -*-
"""猎手管线子进程化（2026-11-22 P2-4）的离线单元验证。

不联网、不起真子进程：mock subprocess.Popen 验证
  ① run_hunter 拉起子进程的参数/日志重定向/状态机；
  ② 防重入（同日期早退、异日期拒跑、残留进程收尸）；
  ③ monitor 线程的结果回收（正常/缺文件/过期文件）与建仓推送触发；
  ④ hunter_progress 读状态文件（进程内 MARKET_PROGRESS 兜底）；
  ⑤ _terminate_hunter_proc 退出清理；
  ⑥ worker 侧原子写 / 父进程看门狗 / 进度落盘。
"""
import json
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
from core import hunter_worker  # noqa: E402


class _FakeProc:
    """可控假子进程：wait() 阻塞在 event 上，可脚本化退出码与"完工写结果"动作。"""

    def __init__(self, rc=0, on_wait=None, block=True):
        self.pid = 43210
        self._rc = rc
        self._on_wait = on_wait
        self._done = threading.Event()
        self._block = block
        self.terminated = False
        self.killed = False
        if not block:
            self._done.set()

    def wait(self, timeout=None):
        if self._on_wait:
            self._on_wait()          # 模拟 worker 完工：写结果档后再退出
            self._on_wait = None
            self._done.set()
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
            "state": dict(gui.HUNTER_RUN_STATE),
            "status_fp": gui._HUNTER_STATUS_FP,
            "result_fp": gui._HUNTER_RESULT_FP,
            "logs": gui.LOGS_DIR,
            "proc": dict(gui._HUNTER_PROC),
        }
        gui.HUNTER_RUN_STATE.update({"date": None, "running": False, "result": None})
        gui._HUNTER_STATUS_FP = t / "hunter_run_status.json"
        gui._HUNTER_RESULT_FP = t / "hunter_run_result.json"
        gui.LOGS_DIR = t / "logs"
        with gui._HUNTER_PROC_LOCK:
            gui._HUNTER_PROC["proc"] = None
            gui._HUNTER_PROC["log_fp"] = None
        self.api = gui.Api()
        self.api._push_hunter_build_candidates = mock.Mock()   # 默认不碰真实飞书推送
        self.procs = []
        self._pop = mock.patch.object(gui.subprocess, "Popen", side_effect=self._mk)
        self._pop.start()

    def _mk(self, cmd, **kw):
        """Popen 工厂：记录 argv/kw，返回可脚本化的 _FakeProc。"""
        self.last_cmd = cmd
        self.last_kw = kw
        p = self.proc_factory() if hasattr(self, "proc_factory") else _FakeProc()
        self.procs.append(p)
        return p

    def tearDown(self):
        self._pop.stop()
        for p in self.procs:
            try:
                p.terminate()        # 释放 monitor 线程的 proc.wait()
            except Exception:
                pass
        self._wait_done(3.0)         # 等 monitor 收尾（关日志句柄），防 WinError 32
        # 僵尸回收用例的残余竞态（2026-11-23 实锤）：`_wait_done` 只看 running 标志，
        # 但 test_zombie 主体自己把 running 复位成 False——被 terminate 的 monitor
        # 线程被唤醒→关日志句柄虽只需毫秒，临时目录清理却可能抢在它前面 ⇒ WinError 32
        # 连红。先主动关残留句柄，再短重试清理，不掩盖真问题（最终仍抛）。
        try:
            with gui._HUNTER_PROC_LOCK:
                _fp = gui._HUNTER_PROC.get("log_fp")
                gui._HUNTER_PROC["log_fp"] = None
            if _fp:
                _fp.close()
        except Exception:
            pass
        gui.HUNTER_RUN_STATE.update(self._saved["state"])
        gui._HUNTER_STATUS_FP = self._saved["status_fp"]
        gui._HUNTER_RESULT_FP = self._saved["result_fp"]
        gui.LOGS_DIR = self._saved["logs"]
        with gui._HUNTER_PROC_LOCK:
            gui._HUNTER_PROC.update(self._saved["proc"])
        for _attempt in range(10):
            try:
                self.td.cleanup()
                break
            except PermissionError:
                time.sleep(0.2)      # 等 monitor 线程把日志句柄放掉
        else:
            self.td.cleanup()

    def _wait_done(self, sec=5.0):
        t0 = time.time()
        while gui.HUNTER_RUN_STATE.get("running") and time.time() - t0 < sec:
            time.sleep(0.02)
        return not gui.HUNTER_RUN_STATE.get("running")


class TestLaunch(_CaseBase):
    def test_run_hunter_launches_subprocess(self):
        r = self.api.run_hunter("2026-11-20")
        self.assertTrue(r["started"] and r["running"] and r["date"] == "2026-11-20")
        cmd = self.last_cmd
        self.assertEqual(cmd[0], sys.executable)                     # 用同一解释器
        self.assertTrue(cmd[1].replace("/", "\\").endswith("core\\hunter_worker.py"))
        self.assertIn("--date", cmd) and self.assertIn("2026-11-20", cmd)
        self.assertIn("--parent-pid", cmd)                           # 父进程看门狗
        self.assertEqual(str(gui._HUNTER_STATUS_FP), cmd[cmd.index("--status-file") + 1])
        self.assertEqual(str(gui._HUNTER_RESULT_FP), cmd[cmd.index("--result-file") + 1])
        self.assertEqual(self.last_kw.get("cwd"), str(gui.BASE))
        self.assertIs(self.last_kw.get("stderr"), gui.subprocess.STDOUT)
        fp = self.last_kw.get("stdout")                              # 输出落日志文件
        self.assertTrue(getattr(fp, "name", "").startswith(str(gui.LOGS_DIR)))
        self.assertIn("hunter_run_", fp.name)
        st = json.loads(gui._HUNTER_STATUS_FP.read_text(encoding="utf-8"))
        self.assertEqual(st["phase"], "启动子进程")                   # 首拍进度父进程先落
        self.assertTrue(st["running"] and st["date"] == "2026-11-20")
        self.assertTrue(gui.HUNTER_RUN_STATE["running"])
        self.assertIsNone(gui.HUNTER_RUN_STATE["result"])

    def test_reentry_same_date_returns_early(self):
        self.api.run_hunter("2026-11-20")
        r = self.api.run_hunter("2026-11-20")
        self.assertEqual(r, {"started": True, "running": True, "date": "2026-11-20"})
        self.assertEqual(len(self.procs), 1, "同日期重入不得再起子进程")

    def test_reentry_other_date_refused(self):
        self.api.run_hunter("2026-11-20")
        r = self.api.run_hunter("2026-11-19")
        self.assertFalse(r["started"]) and self.assertTrue(r["running"])
        self.assertEqual(r["date"], "2026-11-20")
        self.assertEqual(len(self.procs), 1, "异日期并发会互相覆盖状态档，必须拒跑")

    def test_zombie_proc_reaped_before_relaunch(self):
        self.api.run_hunter("2026-11-20")
        old = self.procs[0]
        # 模拟 monitor 漏收尾：状态复位但进程还活着
        gui.HUNTER_RUN_STATE.update({"running": False, "result": None})
        r = self.api.run_hunter("2026-11-20")
        self.assertTrue(r["started"])
        self.assertTrue(old.terminated, "残留活进程必须先 terminate 再起跑")
        self.assertEqual(len(self.procs), 2)

    def test_launch_failure(self):
        self._pop.stop()
        with mock.patch.object(gui.subprocess, "Popen", side_effect=OSError("boom")):
            r = self.api.run_hunter("2026-11-20")
        self.assertFalse(r["started"]) and self.assertFalse(r["running"])
        self.assertIn("boom", gui.HUNTER_RUN_STATE["result"]["error"])
        self.assertFalse(gui.HUNTER_RUN_STATE["result"]["available"])
        self._pop.start()


class TestMonitor(_CaseBase):
    def test_result_collected_and_push_fired(self):
        payload = {"date": "2026-11-20", "available": True, "sector_stocks": {"半导体": []}}

        def _finish():
            gui._write_json_atomic(gui._HUNTER_RESULT_FP,
                                   {"date": "2026-11-20", "ok": True, "result": payload})

        self.proc_factory = lambda: _FakeProc(rc=0, on_wait=_finish)
        self.api._push_hunter_build_candidates = mock.Mock()
        with mock.patch.object(gui, "_hunter_is_intraday", return_value=False):
            self.api.run_hunter("2026-11-20")
            self.assertTrue(self._wait_done(), "monitor 未在限时内收尾")
        self.assertEqual(gui.HUNTER_RUN_STATE["result"], payload)
        self.api._push_hunter_build_candidates.assert_called_once_with(
            payload, "2026-11-20", dedup=False)

    def test_intraday_manual_run_skips_push(self):
        self.proc_factory = lambda: _FakeProc(rc=0, on_wait=lambda: gui._write_json_atomic(
            gui._HUNTER_RESULT_FP,
            {"date": "2026-11-20", "ok": True, "result": {"available": True}}))
        self.api._push_hunter_build_candidates = mock.Mock()
        with mock.patch.object(gui, "_hunter_is_intraday", return_value=True):
            self.api.run_hunter("2026-11-20")   # auto=False 且盘中 ⇒ 只算不推
            self.assertTrue(self._wait_done())
        self.api._push_hunter_build_candidates.assert_not_called()

    def test_missing_result_file_reports_error(self):
        self.proc_factory = lambda: _FakeProc(rc=9, block=False)
        self.api._push_hunter_build_candidates = mock.Mock()
        with mock.patch.object(gui, "_hunter_is_intraday", return_value=True):
            self.api.run_hunter("2026-11-20")
            self.assertTrue(self._wait_done())
        res = gui.HUNTER_RUN_STATE["result"]
        self.assertFalse(res["available"])
        self.assertIn("子进程异常退出", res["error"])

    def test_stale_result_file_rejected(self):
        # 上一轮残留的结果档（日期不符）⇒ 不得当成本轮结果
        gui._write_json_atomic(gui._HUNTER_RESULT_FP,
                               {"date": "2026-11-01", "ok": True, "result": {"available": True}})
        self.proc_factory = lambda: _FakeProc(rc=0, block=False)
        with mock.patch.object(gui, "_hunter_is_intraday", return_value=True):
            self.api.run_hunter("2026-11-20")
            self.assertTrue(self._wait_done())
        res = gui.HUNTER_RUN_STATE["result"]
        self.assertFalse(res.get("available", False))
        self.assertIn("子进程异常退出", res["error"])


class TestProgress(_CaseBase):
    def test_progress_reads_status_file(self):
        gui.HUNTER_RUN_STATE.update({"date": "2026-11-20", "running": True})
        gui._write_json_atomic(gui._HUNTER_STATUS_FP,
                               {"running": True, "date": "2026-11-20", "pid": 1,
                                "phase": "拉取日线行情", "done": 120, "total": 300,
                                "msg": "已拉取 120/300 只", "ts": time.time()})
        p = self.api.hunter_progress()
        self.assertEqual((p["running"], p["ready"], p["date"]), (True, False, "2026-11-20"))
        self.assertEqual((p["phase"], p["done"], p["total"]), ("拉取日线行情", 120, 300))
        self.assertEqual(p["msg"], "已拉取 120/300 只")

    def test_progress_falls_back_to_market_progress(self):
        gui.HUNTER_RUN_STATE.update({"date": "2026-11-20", "running": False, "result": {"x": 1}})
        from modules.market_data import MARKET_PROGRESS as _MP
        _MP.update({"phase": "概念打分", "done": 0, "total": 0, "msg": "兜底"})
        p = self.api.hunter_progress()
        self.assertEqual(p["phase"], "概念打分")      # 无状态档 ⇒ 进程内 MARKET_PROGRESS
        self.assertTrue(p["ready"])

    def test_load_hunter_contract_unchanged(self):
        gui.HUNTER_RUN_STATE.update({"date": "2026-11-20", "running": True})
        r = self.api.load_hunter("2026-11-20")
        self.assertEqual(r, {"available": False, "running": True, "date": "2026-11-20"})
        gui.HUNTER_RUN_STATE.update({"running": False, "result": {"available": True, "date": "2026-11-20"}})
        r = self.api.load_hunter("2026-11-20")
        self.assertTrue(r["available"])


class TestTerminate(_CaseBase):
    def test_terminate_hunter_proc(self):
        self.api.run_hunter("2026-11-20")
        proc = self.procs[0]
        gui._terminate_hunter_proc()
        self.assertTrue(proc.terminated or proc.killed)
        self.assertIsNone(gui._HUNTER_PROC["proc"])
        self.assertIsNone(gui._HUNTER_PROC["log_fp"])

    def test_terminate_idempotent_when_empty(self):
        gui._terminate_hunter_proc()   # 无子进程：静默通过
        self.assertIsNone(gui._HUNTER_PROC["proc"])


class TestWorkerMain(unittest.TestCase):
    """worker 端到端（注入假 gui 模块）：状态档 running→定格、结果档落盘、退出码。"""

    def _run(self, impl, td):
        import types
        sf = str(Path(td) / "status.json")
        rf = str(Path(td) / "result.json")
        fake_gui = types.ModuleType("gui")
        fake_gui.Api = lambda: types.SimpleNamespace(_load_hunter_impl=impl)
        argv = ["hunter_worker.py", "--date", "2026-11-20",
                "--status-file", sf, "--result-file", rf]
        with mock.patch.object(sys, "argv", argv), \
             mock.patch.dict(sys.modules, {"gui": fake_gui}):
            rc = hunter_worker.main()
        return rc, sf, rf

    def test_success_path(self):
        from modules.market_data import MARKET_PROGRESS as _MP

        def _impl(date):
            _MP.update({"phase": "概念打分", "done": 1, "total": 2, "msg": "x"})
            time.sleep(0.25)          # 让 status-writer 至少落一拍中间进度
            return {"date": date, "available": True, "pool_size": 1}

        with tempfile.TemporaryDirectory() as td:
            rc, sf, rf = self._run(_impl, td)
            self.assertEqual(rc, 0)
            r = json.loads(Path(rf).read_text(encoding="utf-8"))
            self.assertTrue(r["ok"] and r["result"]["available"] and r["date"] == "2026-11-20")
            s = json.loads(Path(sf).read_text(encoding="utf-8"))
            self.assertFalse(s["running"])
            self.assertEqual(s["phase"], "完成")

    def test_impl_exception_writes_error_result(self):
        def _impl(date):
            raise RuntimeError("管线炸")

        with tempfile.TemporaryDirectory() as td:
            rc, sf, rf = self._run(_impl, td)
            self.assertEqual(rc, 1)
            r = json.loads(Path(rf).read_text(encoding="utf-8"))
            self.assertFalse(r["ok"])
            self.assertFalse(r["result"]["available"])
            self.assertIn("管线炸", r["result"]["error"])
            s = json.loads(Path(sf).read_text(encoding="utf-8"))
            self.assertEqual(s["phase"], "失败")


class TestWorkerUnits(unittest.TestCase):
    def test_atomic_write_json(self):
        with tempfile.TemporaryDirectory() as td:
            fp = Path(td) / "sub" / "s.json"
            hunter_worker._atomic_write_json(fp, {"a": 1, "中文": "✓"})
            self.assertEqual(json.loads(fp.read_text(encoding="utf-8")), {"a": 1, "中文": "✓"})
            self.assertFalse((Path(str(fp) + ".tmp")).exists())

    def test_pid_alive_self_and_dead(self):
        import os
        self.assertTrue(hunter_worker._pid_alive(os.getpid()))
        self.assertFalse(hunter_worker._pid_alive(2 ** 30 - 7))   # 远超 pid_max
        self.assertTrue(hunter_worker._pid_alive(0))              # 未传父 PID ⇒ 不看门

    def test_status_writer_dumps_market_progress(self):
        with tempfile.TemporaryDirectory() as td:
            fp = str(Path(td) / "status.json")
            from modules.market_data import MARKET_PROGRESS as _MP
            _MP.update({"phase": "拉取日线行情", "done": 3, "total": 9, "msg": "m"})
            stop = threading.Event()
            th = threading.Thread(target=hunter_worker._status_writer,
                                  args=(stop, "2026-11-20", fp, 0.05), daemon=True)
            th.start()
            deadline = time.time() + 3
            while time.time() < deadline:
                if Path(fp).exists():
                    break
                time.sleep(0.05)
            stop.set()
            th.join(2)
            d = json.loads(Path(fp).read_text(encoding="utf-8"))
            self.assertEqual((d["phase"], d["done"], d["total"]), ("拉取日线行情", 3, 9))
            self.assertTrue(d["running"] and d["date"] == "2026-11-20" and d["pid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
