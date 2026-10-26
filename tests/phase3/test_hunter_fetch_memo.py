# -*- coding: utf-8 -*-
"""P2-14 猎手逐只加工「历史段记忆化」的对照冒烟（2026-07-29）。**全离线，零网络**。

钉死三件事：
  ① **逐字节等价**：同一份输入（同一本地缓存态 + 同一快照），
     「二轮热跑（记忆化）」与「全量重算（旁路记忆化 = 原始全帧加工）」的产出 DataFrame
     必须 check_exact 相等 —— 打分输入契约不变是硬约束；
  ② **调用数显著下降**：二轮热跑时历史段重加工（`_process_bulk_frame` 帧长>1 的调用）
     从 N 只降到 0，`_FETCH_MEMO_STATS` 全部 hit；
  ③ **行情变化仍传播**：快照价格变了的票，二轮结果必须跟着变（记忆化不得冻住行情）。

另钉一条保守回退：历史段混入 ≥目标日 的病理行 ⇒ 回退全量加工，结果仍与参照相等。

运行：python -m unittest tests.phase3.test_hunter_fetch_memo -v
"""
import copy
import io
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "stock_hunter") not in sys.path:
    sys.path.insert(0, str(_ROOT / "stock_hunter"))

import pandas as pd  # noqa: E402

import core.chart_cache as _cc  # noqa: E402
import core.market_data.tencent_provider as _tp  # noqa: E402
from modules import market_data as md  # noqa: E402

_TODAY = datetime.now().strftime("%Y-%m-%d")
_COLS = ["date", "open", "high", "low", "close", "volume"]


def _mk_rows(code: str, n: int = 80, end_offset_days: int = 1):
    """生成 n 根合成日线，末根 = 今天 - end_offset_days（确定性伪随机，按 code 播种）。"""
    seed = sum(ord(c) for c in code)
    end = datetime.now().date() - timedelta(days=end_offset_days)
    rows = []
    for k in range(n):
        d = (end - timedelta(days=n - 1 - k)).strftime("%Y-%m-%d")
        base = 10.0 + (seed % 50) + k * 0.11
        rows.append({
            "date": d,
            "open": round(base + 0.01 * ((seed + k) % 3), 2),
            "high": round(base + 0.50, 2),
            "low": round(base - 0.40, 2),
            "close": round(base + 0.05 * ((seed + k) % 5), 2),
            "volume": float(1000 + (seed * 7 + k * 13) % 9000),
        })
    return rows


class _Harness:
    """离线猎手取数环境：本地缓存 / 快照 / 回写全部打桩到内存 STORE。"""

    def __init__(self, test: unittest.TestCase, codes, st_codes=frozenset()):
        self.t = test
        self.codes = list(codes)
        self.st_codes = set(st_codes)
        self.store = {c: _mk_rows(c) for c in self.codes}
        self.snap_price = {}   # c6 -> 本轮快照价（可变）
        self.merge_calls = []
        for c in self.codes:
            last = self.store[c][-1]
            self.snap_price[c] = round(last["close"] * 1.01 + 0.03, 2)
        # 打桩
        self._orig_load = _cc.load_daily_display
        self._orig_snap = _tp.TencentProvider.snapshot_auction_many
        self._orig_merge = _tp.merge_daily_cache
        self._orig_proc = md._process_bulk_frame
        self.proc_calls = []   # (len(frame),) —— 历史段重加工计数
        _cc.load_daily_display = self._fake_load
        _tp.TencentProvider.snapshot_auction_many = self._fake_snap
        _tp.merge_daily_cache = self._fake_merge
        md._process_bulk_frame = self._spy_proc

    def restore(self):
        _cc.load_daily_display = self._orig_load
        _tp.TencentProvider.snapshot_auction_many = self._orig_snap
        _tp.merge_daily_cache = self._orig_merge
        md._process_bulk_frame = self._orig_proc

    # ── 桩实现 ──────────────────────────────────────────────
    def _fake_load(self, code, now=None):
        rows = self.store.get(str(code)[-6:])
        if not rows:
            return None
        return pd.DataFrame(copy.deepcopy(rows))[_COLS]

    def _fake_snap(self, codes, chunk_size=None):
        self.last_chunk_failures = 0
        out = {}
        for c in codes:
            c6 = str(c)[-6:]
            px = self.snap_price[c6]
            out[c6] = {"ts_date": _TODAY, "price": px, "high": round(px + 0.20, 2),
                       "low": round(px - 0.15, 2), "open": round(px - 0.05, 2),
                       "vol_hand": 12345.0}
        return out

    def _fake_merge(self, code, df, cap=800, forming_date=None):
        c6 = str(code)[-6:]
        by = {r["date"]: r for r in self.store[c6]}
        for r in df.itertuples():
            d = str(r.date)[:10]
            by[d] = {"date": d, "open": float(r.open), "close": float(r.close),
                     "high": float(r.high), "low": float(r.low), "volume": float(r.volume)}
        self.store[c6] = [by[k] for k in sorted(by)]
        self.merge_calls.append(c6)

    def _spy_proc(self, raw, code):
        self.proc_calls.append(len(raw))
        return self._orig_proc(raw, code)

    # ── 跑一轮 ──────────────────────────────────────────────
    def run_round(self, bypass_memo=False):
        """bypass_memo=True ⇒ 旁路记忆化、走**原始全帧加工**（等价性参照）。"""
        orig_helper = md._processed_bulk_df
        if bypass_memo:
            md._processed_bulk_df = lambda raw, code, tgt: md._process_bulk_frame(raw, code)
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                fetcher = md.MarketDataFetcher(st_codes=self.st_codes)
                df, failed = fetcher._fetch_historical_tencent(self.codes, _TODAY)
        finally:
            if bypass_memo:
                md._processed_bulk_df = orig_helper
        return df, failed, buf.getvalue()

    @staticmethod
    def sorted(df):
        return df.sort_values("代码").reset_index(drop=True)


class TestFetchMemoEquivalence(unittest.TestCase):
    """首轮冷跑 / 二轮热跑 / 全量参照 三方对照。"""

    def setUp(self):
        md._FETCH_MEMO["date"] = None
        md._FETCH_MEMO["map"] = {}
        self.codes = (["600000", "600001", "600519", "601318", "000001", "000681",
                       "300750", "300059", "301236", "002594", "002929", "003816",
                       "688003", "688543", "688981", "689009",
                       "920008", "920111"] + [f"60{i:04d}" for i in range(2100, 2106)])
        self.h = _Harness(self, self.codes, st_codes={"600001"})

    def tearDown(self):
        self.h.restore()
        md._FETCH_MEMO["date"] = None
        md._FETCH_MEMO["map"] = {}

    def test_cold_hot_reference_equivalence(self):
        h = self.h
        n = len(self.codes)

        # ── 首轮冷跑：全部 miss，历史段各重加工一次（raw 无当日根 ⇒ 每只恰好 1 次）
        df1, failed1, out1 = h.run_round()
        self.assertEqual(failed1, [], f"首轮不应有失败: {failed1}")
        self.assertEqual(len(df1), n)
        heavy1 = [L for L in h.proc_calls if L > 1]
        self.assertEqual(len(heavy1), n, "首轮冷跑应逐只重加工历史段")
        self.assertEqual(md._FETCH_MEMO_STATS["miss"], n)
        self.assertEqual(md._FETCH_MEMO_STATS["hit"], 0)
        self.assertEqual(len(md._FETCH_MEMO["map"]), n, "首轮后每只都应有记忆化条目")
        self.assertEqual(len(h.merge_calls), n, "首轮应逐只回写当日 forming bar")
        self.assertIn("[MEMO]", out1)

        # ── 二轮热跑：快照改 6 只的价；本地缓存已是「首轮回写后」状态（末根=当日 forming）
        state_after_r1 = copy.deepcopy(h.store)
        changed = self.codes[:6]
        for c in changed:
            h.snap_price[c] = round(h.snap_price[c] + 0.17, 2)
        h.proc_calls.clear()
        df2, failed2, out2 = h.run_round()
        self.assertEqual(failed2, [])
        self.assertEqual(len(df2), n)
        heavy2 = [L for L in h.proc_calls if L > 1]
        self.assertEqual(heavy2, [], f"二轮热跑不得再重加工历史段，实际 {heavy2}")
        self.assertEqual(md._FETCH_MEMO_STATS["hit"], n, "二轮应全部命中历史段复用")
        self.assertEqual(md._FETCH_MEMO_STATS["miss"], 0)
        self.assertIn(f"复用 {n} 只 / 重算 0 只", out2, f"[MEMO] 日志行缺失: {out2[-300:]}")

        # 行情变化仍传播：改价的 6 只现价必须变，其余不变
        p1 = dict(zip(df1["代码"], df1["现价"]))
        p2 = dict(zip(df2["代码"], df2["现价"]))
        for c in self.codes:
            if c in changed:
                self.assertNotEqual(p1[c], p2[c], f"{c} 快照改价后结果不得被冻结")
            else:
                self.assertEqual(p1[c], p2[c])

        # ── 全量参照：同一输入态 + 同一快照，旁路记忆化（= 原始全帧加工）
        h.store = copy.deepcopy(state_after_r1)
        h.proc_calls.clear()
        df2_ref, failed_ref, _ = h.run_round(bypass_memo=True)
        self.assertEqual(failed_ref, [])
        heavy_ref = [L for L in h.proc_calls if L > 1]
        self.assertEqual(len(heavy_ref), n, "参照组应逐只全帧重加工（证明热跑真的省了）")

        # 硬约束：逐字节等价
        pd.testing.assert_frame_equal(h.sorted(df2), h.sorted(df2_ref), check_exact=True)

    def test_pathological_future_row_falls_back_safely(self):
        """历史段混入 >目标日 的病理行 ⇒ 回退全量加工，结果仍与参照逐字节相等。"""
        h = self.h
        bad = "600000"
        future = (datetime.now().date() + timedelta(days=3)).strftime("%Y-%m-%d")
        # 在**中间**插一根未来日期的行，末根仍是昨日 ⇒ 首轮就触发切分 + 病理闸
        h.store[bad] = h.store[bad][:40] + [dict(h.store[bad][40], date=future)] + h.store[bad][40:]
        # 首轮冷跑（含病理票）
        h.run_round()
        state = copy.deepcopy(h.store)
        # 二轮：记忆化（先抓本轮统计，参照轮会清零）
        df2, _, _ = h.run_round()
        hit2, miss2 = md._FETCH_MEMO_STATS["hit"], md._FETCH_MEMO_STATS["miss"]
        h.store = copy.deepcopy(state)
        df2_ref, _, _ = h.run_round(bypass_memo=True)
        pd.testing.assert_frame_equal(h.sorted(df2), h.sorted(df2_ref), check_exact=True)
        # 病理票每轮都按 miss 重算（保守侧），其余票正常复用
        self.assertGreaterEqual(hit2, len(self.codes) - 1)
        self.assertGreaterEqual(miss2, 1)

    def test_cross_day_invalidation(self):
        """跨交易日记忆化自动失效（key 带目标日）。"""
        h = self.h
        h.run_round()
        self.assertEqual(md._FETCH_MEMO["date"], _TODAY)
        yesterday = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
        # 用一份**未被首轮回写污染**的干净帧（末根=昨日）触达不同目标日 ⇒ 整张表应被清掉
        md._processed_bulk_df(pd.DataFrame(_mk_rows("600002"))[_COLS], "600002", yesterday)
        self.assertEqual(md._FETCH_MEMO["date"], yesterday)
        self.assertEqual(len(md._FETCH_MEMO["map"]), 1, "换目标日应先清空旧条目")


if __name__ == "__main__":
    unittest.main(verbosity=2)
