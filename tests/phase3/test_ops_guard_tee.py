# -*- coding: utf-8 -*-
"""ops_guard._TeeStream 防管道堵塞整改（2026-10-15）—— 行为级钉测。

## 事故口径（本测试要钉死的行为）

2026-10-15 13:00 策略进程静默挂死：py-spy 栈显示主线程阻塞在 `_TeeStream.write`
→ `self._stream.write(data)`（旧实现控制台先行）。策略 stdout 是通向掘金终端的管道，
**终端进程午后死亡**，Windows 管道写满 64KB 后 write 永久阻塞 ⇒ 14:50 卖侧缺席、
days_held 断档、心跳停摆。

整改后必须满足：
  T1 控制台 write 永久阻塞 ⇒ 主线程 write 仍在 1 秒内返回（文件先行 + 有界队列异步）
  T2 同上场景下**文件内容完整**（落盘是第一优先级，一条都不许丢）
  T3 队列打满后丢弃控制台输出并计数（降级告警落文件，不抛不堵）
  T4 控制台管道**抛异常**（BrokenPipeError）⇒ write 不抛、标记停用、文件不受影响
  T5 ANSI 剥色码逻辑保留（文件纯文本，控制台那份不受影响）
  T6 跨日滚动保留（且不经过会阻塞的 sys.__stdout__）
  T7 flush() 绝不碰控制台流（flush 在满管道上同样会永久阻塞）
  T8 多线程并发 write（GM 回调线程模型）⇒ 文件收齐全部行、不串行半截

运行：python -m unittest discover -s tests/phase3 -p "test_ops_guard_tee.py" -v
"""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GM = os.path.join(_ROOT, "execution", "auto", "_gm")
if _GM not in sys.path:
    sys.path.insert(0, _GM)

from gm_bridge import ops_guard          # noqa: E402
from gm_bridge.ops_guard import _TeeStream, _state   # noqa: E402


class BlockingStream:
    """write/flush 永不返回的控制台桩（模拟终端死亡后写满的 Windows 管道）。"""

    def __init__(self):
        self.gate = threading.Event()      # 永远不设 ⇒ 永久阻塞
        self.write_calls = 0

    def write(self, data):
        self.write_calls += 1
        self.gate.wait()                   # ← 永久阻塞点
        return len(data)

    def flush(self):
        self.gate.wait()


class RaisingStream:
    """write 立即抛 BrokenPipeError 的控制台桩（管道已断的另一形态）。"""

    def write(self, data):
        raise BrokenPipeError("模拟终端死亡: 管道另一端已关闭")

    def flush(self):
        raise BrokenPipeError("同上")


class TestTeeStreamConsoleBlocked(unittest.TestCase):
    """T1/T2/T3：控制台永久阻塞 ⇒ 主线程不堵、文件完整、丢弃计数。"""

    def test_blocked_console_never_blocks_caller_and_file_complete(self):
        tmp = tempfile.mkdtemp(prefix="tee_blocked_")
        log_path = os.path.join(tmp, "strategy_test.log")
        saved = dict(_state)
        try:
            _state["_log_dir"] = tmp
            _state["_log_date"] = time.strftime("%Y%m%d")
            with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
                _state["_log_fh"] = fh
                tee = _TeeStream(BlockingStream(), fh)
                N = _TeeStream._QUEUE_MAX + 900     # 打满队列还要溢出
                t0 = time.monotonic()
                worst = 0.0
                for i in range(N):
                    _t = time.monotonic()
                    tee.write("flood line %d\n" % i)
                    worst = max(worst, time.monotonic() - _t)
                total = time.monotonic() - t0
                # T1：单次 write 必须 << 1s（owner 验收线，也是本测试的硬闸）
                self.assertLess(worst, 1.0, "单次 write 超过 1 秒 ⇒ 主线程仍会被堵")
                # 总耗时只做防退化参考：套件并发/磁盘繁忙时 file flush 会被拖慢，
                # 不据此判失败（真正的防堵证据是上面的单次 worst）。
                if total > 30.0:
                    self.fail("N 次 write 总耗时 %0.1fs 异常（疑似主线程被拖住）" % total)
                # T3：队列必然溢出过 ⇒ 有丢弃计数
                self.assertGreater(tee._dropped, 0, "队列溢出后应有丢弃计数")
            # T2：文件必须完整——N 行 flood 一行不少
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            for i in (0, 1, N // 2, N - 1):
                self.assertIn("flood line %d\n" % i, content)
            self.assertEqual(content.count("flood line "), N,
                             "文件丢失日志行 ⇒ 落盘可靠性不达标")
            # T3：降级告警落文件
            self.assertIn("控制台输出队列已满", content)
        finally:
            _state.clear()
            _state.update(saved)


class TestTeeStreamBrokenPipe(unittest.TestCase):
    """T4：控制台抛异常 ⇒ write 不抛、停用控制台、文件不受影响。"""

    def test_broken_pipe_marks_dead_and_file_intact(self):
        tmp = tempfile.mkdtemp(prefix="tee_broken_")
        log_path = os.path.join(tmp, "strategy_test.log")
        saved = dict(_state)
        try:
            _state["_log_dir"] = tmp
            _state["_log_date"] = time.strftime("%Y%m%d")
            with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
                _state["_log_fh"] = fh
                tee = _TeeStream(RaisingStream(), fh)
                # 给消费者线程一点时间去拿第一条并抛异常
                for i in range(50):
                    t0 = time.monotonic()
                    tee.write("bp line %d\n" % i)
                    self.assertLess(time.monotonic() - t0, 1.0)
                    if tee._console_dead:
                        break
                    time.sleep(0.02)
                self.assertTrue(tee._console_dead, "管道异常后应标记控制台停用")
                # 停用后再写：不抛、秒回
                t0 = time.monotonic()
                tee.write("after dead\n")
                self.assertLess(time.monotonic() - t0, 1.0)
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("after dead\n", content)
            self.assertIn("控制台写入异常", content)
        finally:
            _state.clear()
            _state.update(saved)


class TestTeeStreamAnsiAndFlush(unittest.TestCase):
    """T5/T7：ANSI 剥码保留；flush() 不碰控制台。"""

    def test_ansi_stripped_in_file(self):
        tmp = tempfile.mkdtemp(prefix="tee_ansi_")
        log_path = os.path.join(tmp, "strategy_test.log")
        saved = dict(_state)
        try:
            _state["_log_dir"] = tmp
            _state["_log_date"] = time.strftime("%Y%m%d")
            with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
                _state["_log_fh"] = fh
                tee = _TeeStream(RaisingStream(), fh)
                tee.write("\x1b[31m红色告警\x1b[0m 正文\n")
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn("\x1b", content, "日志文件不许残留 ANSI 转义")
            self.assertIn("红色告警 正文\n", content)
        finally:
            _state.clear()
            _state.update(saved)

    def test_flush_never_touches_console(self):
        tee = _TeeStream(BlockingStream(), open(os.devnull, "w", encoding="utf-8"))
        t0 = time.monotonic()
        tee.flush()                       # BlockingStream.flush 永久阻塞——若被调到就卡死
        self.assertLess(time.monotonic() - t0, 1.0,
                        "flush() 碰了控制台流 ⇒ 满管道场景会堵死主线程")
        tee._fh.close()


class TestTeeStreamRollover(unittest.TestCase):
    """T6：跨日滚动保留，且滚动通告不经过 sys.__stdout__（坏管道）。"""

    def test_rollover_creates_new_daily_file(self):
        tmp = tempfile.mkdtemp(prefix="tee_roll_")
        saved = dict(_state)
        try:
            old_path = os.path.join(tmp, "strategy_20000101.log")
            _state["_log_dir"] = tmp
            _state["_log_date"] = "20000101"       # 假装句柄停在旧日
            with open(old_path, "a", encoding="utf-8", buffering=1) as fh:
                _state["_log_fh"] = fh
                tee = _TeeStream(RaisingStream(), fh)
                tee.write("rollover trigger\n")
                today = time.strftime("%Y%m%d")
                new_path = os.path.join(tmp, "strategy_%s.log" % today)
                self.assertTrue(os.path.exists(new_path), "跨日应滚出当日新日志文件")
                with open(new_path, encoding="utf-8") as f:
                    content = f.read()
                self.assertIn("rollover", content)          # 滚动横幅
                self.assertIn("rollover trigger\n", content)  # 数据落在**新**文件
        finally:
            _state.clear()
            _state.update(saved)


class TestTeeStreamThreadSafety(unittest.TestCase):
    """T8：GM 回调多线程并发 write ⇒ 文件收齐、不抛。"""

    def test_concurrent_writes_all_landed(self):
        tmp = tempfile.mkdtemp(prefix="tee_mt_")
        log_path = os.path.join(tmp, "strategy_test.log")
        saved = dict(_state)
        try:
            _state["_log_dir"] = tmp
            _state["_log_date"] = time.strftime("%Y%m%d")
            with open(log_path, "a", encoding="utf-8", buffering=1) as fh:
                _state["_log_fh"] = fh
                tee = _TeeStream(RaisingStream(), fh)
                errors = []

                def worker(tid):
                    try:
                        for i in range(50):
                            tee.write("t%d line %d\n" % (tid, i))
                    except Exception as e:                    # noqa: BLE001
                        errors.append(e)

                threads = [threading.Thread(target=worker, args=(t,)) for t in range(6)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
                self.assertFalse(errors, "并发 write 抛异常: %s" % errors)
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
            for tid in range(6):
                self.assertEqual(content.count("t%d line " % tid), 50,
                                 "线程 %d 的日志行丢失" % tid)
        finally:
            _state.clear()
            _state.update(saved)


if __name__ == "__main__":
    unittest.main()
