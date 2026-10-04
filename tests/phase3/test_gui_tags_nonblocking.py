# -*- coding: utf-8 -*-
"""技术标签批处理的**冷启动不阻塞**防回退单测（2026-10-04）。

背景：`Api.load_stock_tags_batch` 在**无缓存**时原来同步跑 40 线程 pandas+网络
（实测 23~35s），把 pywebview 主线程冻住——就是启动后第一次进建仓表/破位表时卡十几秒。
本次改为：冷启动也走后台线程算，立即返回空 tags；前端 10s 轮询，约 20s 后自填。

本测试把单票计算 `_stock_tags_one` 换成桩（离线、无网络），断言：
  T1 冷启动调用**立即**返回（< 慢桩耗时），且返回空 tags —— 证明未走同步计算。
  T2 冷启动确实**起了后台线程**（_TAGS_RUNNING 置位）。
  T3 后台算完后缓存被填充，标志复位；再次调用命中缓存拿到真值。
  T4 缓存**过期**时先返回旧值、不阻塞。

铁律：全离线。桩掉网络路径，不读 t_io/、不打网络。

运行：python tests/phase3/test_gui_tags_nonblocking.py
"""
import hashlib
import os
import sys
import threading
import time
import unittest
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import t_gui  # noqa: E402

SLOW = 0.6  # 桩的“慢计算”耗时；冷调若走同步路径必 > 此值


class TestTagsColdNonBlocking(unittest.TestCase):
    def setUp(self):
        self.api = t_gui.Api()
        self.codes = ["600000", "600001", "600002"]
        self._orig_one = t_gui.Api._stock_tags_one
        self._saved_cache = dict(t_gui._TAGS_CACHE)
        self._saved_running = t_gui._TAGS_RUNNING
        t_gui._TAGS_CACHE.clear()
        t_gui._TAGS_RUNNING = False

        def _fake_one(_self, code):
            time.sleep(SLOW)  # 模拟慢计算；同步调用会明显拖慢
            return {"trend": "up", "box_pos": 0.5, "tags": [{"label": f"t-{code}", "color": "g"}]}
        t_gui.Api._stock_tags_one = _fake_one

    def tearDown(self):
        t_gui.Api._stock_tags_one = self._orig_one
        # 等后台算完，避免污染后续用例 / 其它测试的全局状态
        t0 = time.time()
        while t_gui._TAGS_RUNNING and time.time() - t0 < 10:
            time.sleep(0.05)
        with t_gui._TAGS_LOCK:
            t_gui._TAGS_CACHE.clear()
            t_gui._TAGS_CACHE.update(self._saved_cache)
        t_gui._TAGS_RUNNING = self._saved_running

    def _key(self):
        today = datetime.now().strftime("%Y-%m-%d")
        fp = hashlib.md5(",".join(sorted(set(self.codes))).encode()).hexdigest()[:12]
        return f"{today}:{fp}"

    def test_01_冷启动立即返回空且不起同步计算(self):
        t = time.perf_counter()
        r = self.api.load_stock_tags_batch(self.codes)
        dt = time.perf_counter() - t
        self.assertLess(dt, SLOW, f"冷启动阻塞了 {dt:.2f}s，疑似走了同步计算路径")
        self.assertEqual(r.get("tags"), {}, "冷启动应先返回空 tags（数据由后台填）")

    def test_02_冷启动起了后台线程(self):
        self.api.load_stock_tags_batch(self.codes)
        self.assertTrue(t_gui._TAGS_RUNNING, "冷启动未起后台线算（_TAGS_RUNNING 未置位）")

    def test_03_后台算完填充缓存并复位(self):
        self.api.load_stock_tags_batch(self.codes)
        t0 = time.time()
        while t_gui._TAGS_RUNNING and time.time() - t0 < 10:
            time.sleep(0.05)
        self.assertFalse(t_gui._TAGS_RUNNING, "后台算完但标志未复位")
        with t_gui._TAGS_LOCK:
            c = t_gui._TAGS_CACHE.get(self._key())
        self.assertIsNotNone(c, "后台算完未写入缓存")
        self.assertEqual(len(c["tags"]), len(self.codes))
        # 再次调用：命中缓存，拿到真值且秒回
        t = time.perf_counter()
        r = self.api.load_stock_tags_batch(self.codes)
        self.assertLess(time.perf_counter() - t, SLOW, "缓存命中却仍在阻塞")
        self.assertEqual(len(r.get("tags", {})), len(self.codes))

    def test_04_过期缓存先返回旧值不阻塞(self):
        # 预置一份“过期”缓存
        with t_gui._TAGS_LOCK:
            t_gui._TAGS_CACHE[self._key()] = {
                "ts": time.time() - (t_gui._TAGS_TTL + 5),
                "tags": {"600000": {"trend": "flat", "tags": []}},
            }
        t = time.perf_counter()
        r = self.api.load_stock_tags_batch(self.codes)
        self.assertLess(time.perf_counter() - t, SLOW, "过期缓存分支阻塞了")
        self.assertIn("600000", r.get("tags", {}), "过期时应先返回旧值")


if __name__ == "__main__":
    unittest.main(verbosity=2)
