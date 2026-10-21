# -*- coding: utf-8 -*-
"""GM 单 worker 池的并发竞态（2026-10-21）。

现场（开 K 线时的真实日志）：
    gm 连接异常(snapshot 002176: cannot schedule new futures after shutdown) → 冷却 60s
它把「本进程内自己关了自己的池」记成了「GM 挂了」，随后 **全部取数降级腾讯**（慢几倍），
和"开图 6~14s""20s 超时"是同一时间线上的事。三个缺陷各自可验证：

  ① `_gm_call` 超时后 `shutdown(cancel_futures=True)` 与别的线程 `submit` 赛跑
     ⇒ 后到的 submit 打到已关闭的池上 ⇒ RuntimeError 被当成 GM 故障；
  ② 排队中被取消的任务（CancelledError）根本没打到 GM，也被熔断；
  ③ `get_provider()` 是裸 check-then-set ⇒ 并发首次各自构造 Facade（实测 6 线程 6 份，
     每份 ~1.5s，大头是 psutil 扫进程取 token），后建的把先建的变成孤儿池。
"""
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.market_data import facade as F   # noqa: E402


def _bare():
    """不跑 __init__（会构造真实 GM/腾讯 provider）：只装配被测状态。"""
    f = object.__new__(F.MarketDataFacade)
    f._gm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-call")
    f._gm_pool_lock = threading.Lock()
    f._GM_CALL_TIMEOUT = 0.2
    return f


class TestPoolRevive:
    def test_submit_to_dead_pool_is_revived(self):
        f = _bare()
        f._gm_pool.shutdown(wait=False, cancel_futures=True)   # 池被"别人"关掉
        assert f._gm_call("t", lambda: 42) == 42, "死池上应换新池重试，而不是抛 GM 故障"

    def test_timeout_then_concurrent_submits(self):
        """一个线程超时熔断的同时，其它线程 submit —— 不得冒 RuntimeError。"""
        f = _bare()
        errs, oks = [], []
        stop = threading.Event()

        def slow():
            time.sleep(0.6)
            return "slow"

        def fast():
            time.sleep(0.001)
            return "fast"

        def racer():
            while not stop.is_set():
                try:
                    oks.append(f._gm_call("t", fast))
                except Exception as e:            # noqa: BLE001
                    errs.append(repr(e))

        ths = [threading.Thread(target=racer) for _ in range(4)]
        for t in ths:
            t.start()
        try:
            with pytest.raises(TimeoutError):
                f._gm_call("slow", slow, timeout=0.05)     # 触发熔断换池
            time.sleep(0.2)
        finally:
            stop.set()
            for t in ths:
                t.join(5)
        assert not errs, f"并发 submit 撞上换池必须自愈，实际报错: {errs[:3]}"
        assert oks, "竞态期间至少应有一次成功"

    def test_cancelled_while_queued_is_not_gm_down(self):
        """排队被 cancel_futures 取消 ⇒ 重排一次（没打到 GM，不算故障）。"""
        f = _bare()
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        real_submit = f._gm_submit
        first = {"done": False}

        def flaky_submit(fn_, args, kwargs):
            if not first["done"]:
                first["done"] = True
                # 第一次不真的提交：模拟"排队时被另一个线程的 cancel_futures 取消"
                import concurrent.futures as cf
                c = cf.Future()
                c.cancel()
                return c
            return real_submit(fn_, args, kwargs)

        f._gm_submit = flaky_submit
        assert f._gm_call("t", fn) == "ok"
        assert calls["n"] == 1, "被取消的那次根本没执行，重排只应真正执行一次"


class TestSingleton:
    def test_get_provider_builds_once_under_concurrency(self, monkeypatch):
        built = []
        real = F.MarketDataFacade

        class Counting(real):
            def __init__(self):
                built.append(1)

        monkeypatch.setattr(F, "MarketDataFacade", Counting)
        monkeypatch.setattr(F, "_facade_singleton", None)
        barrier = threading.Barrier(6)

        def w():
            barrier.wait()
            F.get_provider()

        ths = [threading.Thread(target=w) for _ in range(6)]
        for t in ths:
            t.start()
        for t in ths:
            t.join(10)
        monkeypatch.setattr(F, "_facade_singleton", None)   # 别污染后续测试
        assert len(built) == 1, f"并发首次只该构造 1 份 Facade，实际 {len(built)}"
