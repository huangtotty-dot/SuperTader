# -*- coding: utf-8 -*-
"""QuoteBus（core/market_data/quote_bus.py）离线单测（P2-13, 2026-10-23）。

monkeypatch 假 `TencentProvider.snapshot_auction_many` 计数，覆盖契约五条：
  ① 新鲜命中零网络；② 过期触发一次补齐（且补齐后写回共享档）；
  ③ 双"进程"（重置进程内 memo 模拟）第二个命中、零追加网络；
  ④ 取数失败 stale 兜底不抛；⑤ 共享档损坏 ⇒ 丢弃重建。

运行：python -m unittest tests.phase3.test_quote_bus -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import core.market_data.quote_bus as qb  # noqa: E402


def _q(px=11.0, d="2026-10-23"):
    return {"name": "测试股", "price": px, "pre_close": 10.0, "ts_date": d}


class _CountingFetcher:
    """替换 TencentProvider.snapshot_auction_many：记调用次数/码数，返回给定 payload。"""

    def __init__(self, payload=None, raises=False):
        self.payload = payload if payload is not None else {}
        self.raises = raises
        self.calls = 0
        self.codes_seen = []

    def __call__(self, codes, chunk_size=None):
        # 注意：本实例被直接赋为 TencentProvider 的**类属性**（非描述符），
        # 调用形态是 fk(codes) ⇒ 这里第一个参数就是 codes，不要再留 _self。
        self.calls += 1
        self.codes_seen.append(list(codes or []))
        if self.raises:
            raise RuntimeError("boom")
        if callable(self.payload):
            return self.payload(codes)
        return dict(self.payload)


class _BusCase(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self._o = (qb._BUS_FP, qb._LOCK_FP, qb._STATE_DIR)
        qb._STATE_DIR = self._td.name
        qb._BUS_FP = os.path.join(self._td.name, "quote_bus.json")
        qb._LOCK_FP = os.path.join(self._td.name, "quote_bus.lock")
        self._o_stats = dict(qb._STATS)
        self._o_memo = dict(qb._MEMO)
        qb._STATS.update({"hit": 0, "miss": 0, "fetch": 0})
        self._reset_memo()
        from core.market_data import tencent_provider as TP
        self._TP = TP
        self._o_fetch = TP.TencentProvider.snapshot_auction_many

    def tearDown(self):
        qb._BUS_FP, qb._LOCK_FP, qb._STATE_DIR = self._o
        qb._STATS.update(self._o_stats)
        qb._MEMO.update(self._o_memo)
        self._TP.TencentProvider.snapshot_auction_many = self._o_fetch
        self._td.cleanup()

    def _reset_memo(self):
        """模拟"另一个进程"：进程内 1s memo 是进程私有的，跨进程共享只走文件层。"""
        qb._MEMO.update({"ts": 0.0, "store": None})

    def _write_store(self, store):
        with open(qb._BUS_FP, "w", encoding="utf-8") as f:
            json.dump(store, f)

    def _read_store(self):
        with open(qb._BUS_FP, encoding="utf-8") as f:
            return json.load(f)


class TestFreshHitNoNetwork(_BusCase):
    def test_1_fresh_hit_zero_network(self):
        self._write_store({"600001": {"ts": time.time(), "q": _q(11.0)}})
        fk = _CountingFetcher({"600001": _q(99.0)})
        self._TP.TencentProvider.snapshot_auction_many = fk
        got = qb.get_quotes(["600001"], max_age_sec=8.0)
        self.assertEqual(fk.calls, 0, "新鲜命中必须零网络")
        self.assertEqual(got["600001"]["price"], 11.0, "命中必须返回共享档里的那份")
        self.assertEqual(qb.bus_stats()["hit"], 1)
        self.assertEqual(qb.bus_stats()["miss"], 0)

    def test_code_normalization(self):
        """sh600001 / 600001_SH 输入与共享档的裸码 key 对齐。"""
        self._write_store({"600001": {"ts": time.time(), "q": _q()}})
        self._TP.TencentProvider.snapshot_auction_many = _CountingFetcher()
        got = qb.get_quotes(["sh600001", "600001_SH"], max_age_sec=8.0)
        self.assertIn("600001", got)
        self.assertEqual(len(got), 1, "同一码不同写法去重后只占一个 key")


class TestStaleTriggersOneFetch(_BusCase):
    def test_2_stale_fetch_once_and_writeback(self):
        self._write_store({"600001": {"ts": time.time() - 100, "q": _q(9.9)}})
        fk = _CountingFetcher({"600001": _q(11.5)})
        self._TP.TencentProvider.snapshot_auction_many = fk
        got = qb.get_quotes(["600001"], max_age_sec=8.0)
        self.assertEqual(fk.calls, 1, "过期必须恰好触发一次补齐")
        self.assertEqual(got["600001"]["price"], 11.5, "补齐后必须返回新价")
        ent = self._read_store()["600001"]
        self.assertEqual(ent["q"]["price"], 11.5, "补齐结果必须 merge 写回共享档")
        self.assertGreater(time.time() - float(ent["ts"]), -1, "写回必须带新 ts")
        # 紧接着再要一次：共享档已新鲜 ⇒ 不再取网
        got2 = qb.get_quotes(["600001"], max_age_sec=8.0)
        self.assertEqual(fk.calls, 1)
        self.assertEqual(got2["600001"]["price"], 11.5)

    def test_partial_miss_fetches_only_missing(self):
        """一只新鲜、一只缺失 ⇒ 只对缺失的那只取网。"""
        self._write_store({"600001": {"ts": time.time(), "q": _q(1.0)}})
        fk = _CountingFetcher(lambda codes: {c: _q(2.0) for c in codes})
        self._TP.TencentProvider.snapshot_auction_many = fk
        got = qb.get_quotes(["600001", "600002"], max_age_sec=8.0)
        self.assertEqual(fk.calls, 1)
        self.assertEqual(fk.codes_seen[0], ["600002"])
        self.assertEqual(got["600001"]["price"], 1.0)
        self.assertEqual(got["600002"]["price"], 2.0)


class TestCrossProcessHit(_BusCase):
    def test_3_second_process_hits_without_fetch(self):
        # "进程 A"：空档 ⇒ 取网补齐
        fk = _CountingFetcher({"600001": _q(12.0)})
        self._TP.TencentProvider.snapshot_auction_many = fk
        got_a = qb.get_quotes(["600001"], max_age_sec=8.0)
        self.assertEqual(fk.calls, 1)
        self.assertEqual(got_a["600001"]["price"], 12.0)
        # "进程 B"：memo 是进程私有的 ⇒ 重置后模拟全新进程，只读共享档
        self._reset_memo()
        got_b = qb.get_quotes(["600001"], max_age_sec=8.0)
        self.assertEqual(fk.calls, 1, "第二个'进程'必须命中共享档、零追加网络")
        self.assertEqual(got_b["600001"]["price"], 12.0)
        self.assertEqual(qb.bus_stats()["hit"], 1)
        self.assertEqual(qb.bus_stats()["fetch"], 1)


class TestStaleFallback(_BusCase):
    def test_4_fetch_failure_returns_stale_without_raising(self):
        self._write_store({"600001": {"ts": time.time() - 100, "q": _q(9.9)}})
        fk = _CountingFetcher(raises=True)
        self._TP.TencentProvider.snapshot_auction_many = fk
        got = qb.get_quotes(["600001"], max_age_sec=8.0)   # 不许抛
        self.assertEqual(fk.calls, 1)
        self.assertEqual(got["600001"]["price"], 9.9, "取数失败 ⇒ 过期数据照样返回")

    def test_4b_fetch_failure_and_nothing_cached_returns_missing(self):
        fk = _CountingFetcher(raises=True)
        self._TP.TencentProvider.snapshot_auction_many = fk
        got = qb.get_quotes(["600099"], max_age_sec=8.0)   # 不许抛
        self.assertEqual(got, {}, "实在没有就缺 code，不是抛异常")

    def test_empty_and_bad_input(self):
        self.assertEqual(qb.get_quotes([], max_age_sec=8.0), {})
        self.assertEqual(qb.get_quotes(None, max_age_sec=8.0), {})
        self.assertEqual(qb.get_quotes(["", None]), {})


class TestCorruptRebuild(_BusCase):
    def test_5_corrupt_file_discarded_and_rebuilt(self):
        with open(qb._BUS_FP, "wb") as f:
            f.write(b"\x00\x01 not json {{{")
        fk = _CountingFetcher({"600001": _q(13.0)})
        self._TP.TencentProvider.snapshot_auction_many = fk
        got = qb.get_quotes(["600001"], max_age_sec=8.0)   # 不许抛
        self.assertEqual(fk.calls, 1, "损坏档按空档处理 ⇒ 触发补齐")
        self.assertEqual(got["600001"]["price"], 13.0)
        store = self._read_store()                          # 写回后必须是合法 JSON
        self.assertEqual(store["600001"]["q"]["price"], 13.0)


class TestCapacityEviction(_BusCase):
    def test_oldest_ts_evicted_over_capacity(self):
        now = time.time()
        store = {f"{i:06d}": {"ts": now - (3000 - i), "q": _q(float(i))}
                 for i in range(qb._MAX_ENTRIES)}           # 正好满容，000000 最旧
        self._write_store(store)
        fk = _CountingFetcher({"700001": _q(7.0)})
        self._TP.TencentProvider.snapshot_auction_many = fk
        qb.get_quotes(["700001"], max_age_sec=8.0)
        store2 = self._read_store()
        self.assertLessEqual(len(store2), qb._MAX_ENTRIES)
        self.assertIn("700001", store2)
        self.assertNotIn("000000", store2, "超容必须按 ts 最旧淘汰")


class TestStatsShape(_BusCase):
    def test_bus_stats_keys_and_copy(self):
        s = qb.bus_stats()
        self.assertEqual(set(s), {"hit", "miss", "fetch"})
        s["hit"] = 999
        self.assertNotEqual(qb.bus_stats()["hit"], 999, "必须返回拷贝，不给外部改内部态")


if __name__ == "__main__":
    unittest.main(verbosity=2)
