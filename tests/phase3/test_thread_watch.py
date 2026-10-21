# -*- coding: utf-8 -*-
"""进程内**线程级** CPU 归因（core/thread_watch.py，2026-10-21）。

为什么需要外部进程做不到：Windows 上 `GetThreadDescription` 对本机 Python 线程
**全返回 None**（Python 3.11 不把 `threading.Thread(name=…)` 写进 OS 线程描述，实测），
所以只能在**进程内部**用 `threading.enumerate()` 的 ident 去对 psutil 的 thread id。

用途：2026-10-21 的进程级采样显示 GUI「43% 的采样点 >90% 单核 + 30 分钟周期」，
但看不出是 `chart-live-*`（K线实时尾）、`mw-*`（多窗口）还是别的线程 —— 这组测试
保证"线程名 → CPU"这条链可靠。
"""
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.thread_watch as tw   # noqa: E402


class TestGroupOf:
    def test_named_groups(self):
        assert tw.group_of("mw-600000") == tw.group_of("mw-300503")   # 同组
        assert "多窗口" in tw.group_of("mw-600000")
        assert "K线实时尾" in tw.group_of("chart-live-600000-min5")
        assert tw.group_of("MainThread") == "MainThread"

    def test_unknown_passthrough(self):
        assert tw.group_of("some-other-thread") == "some-other-thread"


class TestSnapshot:
    def test_attributes_cpu_to_the_right_thread(self):
        """一个忙线程的 CPU 必须落到**它自己的名字/分组**上（不是全算进 MainThread）。"""
        stop = threading.Event()

        def busy():
            x = 0
            while not stop.is_set():
                x += 1
        t = threading.Thread(target=busy, name="mw-999999", daemon=True)
        t.start()
        try:
            prev = {}
            tw.snapshot(prev, 0.5)          # 建基线（首次没有增量）
            time.sleep(0.5)
            snap = tw.snapshot(prev, 0.5)
        finally:
            stop.set()
            t.join(timeout=2)
        groups = {r["name"]: r["pct"] for r in snap.get("by_group", [])}
        assert groups, f"没采到任何线程: {snap}"
        mw = groups.get(tw.group_of("mw-999999"), 0)
        assert mw > 20, f"忙线程的 CPU 没归到 mw-* 组: {groups}"

    def test_no_delta_first_window(self):
        """首窗口没有上一帧 ⇒ 不能凭空报 CPU（否则每天第一条都是假峰值）。"""
        snap = tw.snapshot({}, 1.0)
        assert snap.get("total_pct") == 0


class TestStart:
    def test_start_is_idempotent(self):
        a = tw.start(interval=30, flush_sec=600)     # 拉长间隔，别在测试里真写盘
        b = tw.start(interval=30, flush_sec=600)
        assert a is True and b is False
