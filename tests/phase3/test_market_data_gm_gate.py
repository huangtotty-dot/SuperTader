# -*- coding: utf-8 -*-
"""数据源门面 · GM 探针闸与腾讯兜底的单测（2026-10-12）。**全离线，不发网络请求**。

背景（owner：「gm 总是不可达这个一定要查清楚」）：查清后 GM 其实一直是好的，
真因有三条，本文件把**已修的三条**钉住：

  T1 `daily_many` 在探针窗被占时**不再静默返空** —— 已复现过的缺陷：
     窗口空闲 3/3，窗口被另一路占住（GM 健康）⇒ 0/3 空且 down_until=None，
     而同一时刻逐只 daily() 照样有数据 ⇒ 现象就是"daily 行、daily_many 不行"。
  T2 `_maybe_append_forming` 里的**腾讯 HTTP 失败不得被记成 gm 挂了**。
     它整段挂在 daily() 的 `except → _note_gm_down` 里 ⇒ 腾讯抖动会误置 60s GM 冷却窗
     （把健康的 GM 熔断）。对齐 `_maybe_append_index_forming`（它一直有兜底）。
  T3 腾讯单只快照 socket 超时 8s→2s：实测补 forming 的 max 8.25s→2.23s、
     40 只合计 34.0s→10.4s（占 daily() 耗时 93%）。**失败语义不变**（打满=本就在挂）。
  T4 `_note_gm_down` 措辞分「超时」与「异常」，不再一律说"服务不可达"
     （那条消息曾误导成"GM 常挂"）。
  T6 qt.gtimg.cn **复用 keep-alive 连接**（2026-10-16）：原来每个请求都裸
     `urlopen` ⇒ 每次都新建 DNS+TCP+TLS，实测**建连本身 ≈290ms**（同一 URL 连打 12 次：
     裸 urlopen 中位 350.9ms → 复用连接中位 61.4ms，**5.7×**；走真实函数
     `_qt_snapshot_raw` 顺序 20 次 350.9→73.3ms，**4.8×**）。`snapshot()` 是**逐只**调的
     ⇒ 一屏 50 只 ≈14.5s 握手；10-16 卡死栈里实测 5 个线程同时卡在 `socket.create_connection`。

⚠️ **刻意不测**（已实测否证，写在这里防日后有人"顺手改回去"）：
   把 `daily()` 也改成 `_gm_ok_wait()` ⇒ 6 线程 150 只 GM 使用率 17%→73%，
   但墙钟 **10.6s→43.8s（+313%）**（GM 池单 worker ⇒ 逐只 293ms）。
   批量路径的正解是**绕开 daily() 读本地缓存**，不是排队等 GM。

运行：python -m unittest tests.phase3.test_market_data_gm_gate -v
"""
import logging
import sys
import time as _time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd  # noqa: E402

from core.market_data import facade as F  # noqa: E402
from core.market_data import tencent_provider as TP  # noqa: E402


class _FakeGm:
    """最小 GM provider 桩：`_ready=True` + 可控的 daily/daily_batch。"""

    _ready = True

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def daily(self, code, days=800):
        self.calls.append(("daily", code))
        if self.fail:
            raise RuntimeError("boom")
        return pd.DataFrame([{"date": "2026-10-10", "open": 1, "high": 2,
                              "low": 0.5, "close": 1.5, "volume": 100}])

    def daily_batch(self, codes, days=250):
        self.calls.append(("batch", tuple(codes)))
        return {str(c): self.daily(c, days) for c in codes}


class _StubProvider:
    """替掉真 `GmProvider()`：它的 `load_token()` 要做终端会话发现（每建一个 facade 数秒），
    单测完全不需要 —— 会把 15 个用例拖成 30s+。"""
    _ready = True

    def __init__(self, *a, **k):
        pass


def _facade(gm=None):
    _orig = F.GmProvider
    F.GmProvider = _StubProvider
    try:
        f = F.MarketDataFacade()
    finally:
        F.GmProvider = _orig
    f._gm = gm if gm is not None else _FakeGm()
    f._gm_down_until = None
    f._gm_probe_until = None
    return f


class TestProbeWindowWait(unittest.TestCase):
    def test_waits_then_succeeds_when_window_frees(self):
        """窗被占时**等一下**，窗一空就真打 GM（而不是立刻降级腾讯）。"""
        import threading
        f = _facade()
        self.assertTrue(f._gm_ok(), "先占住探针窗")
        t = threading.Timer(0.15, f._gm_ok_reset)      # 模拟健康调用很快清窗
        t.start()
        try:
            t0 = _time.perf_counter()
            self.assertTrue(f._gm_ok_wait(2.0), "窗腾出来后应拿到 GM")
            self.assertGreater(_time.perf_counter() - t0, 0.05, "确实等过")
        finally:
            t.cancel()

    def test_gives_up_at_deadline(self):
        f = _facade()
        f._gm_probe_until = datetime.now() + timedelta(seconds=30)   # 窗长期被占
        t0 = _time.perf_counter()
        self.assertFalse(f._gm_ok_wait(0.2))
        dt = _time.perf_counter() - t0
        self.assertGreaterEqual(dt, 0.2)
        self.assertLess(dt, 2.0, "必须有上界，不能无限等")

    def test_cooldown_returns_immediately(self):
        """冷却窗内**不等** —— 那才是"真的不用 GM"，等也等不到。"""
        f = _facade()
        f._gm_down_until = datetime.now() + timedelta(seconds=60)
        t0 = _time.perf_counter()
        self.assertFalse(f._gm_ok_wait(5.0))
        self.assertLess(_time.perf_counter() - t0, 0.05, "冷却窗不得引入等待")

    def test_not_ready_returns_false(self):
        f = _facade()
        f._gm = None
        self.assertFalse(f._gm_ok_wait(0.5))


class TestDailyManyNoSilentEmpty(unittest.TestCase):
    """T1：窗口被占不再是「整批返空」。"""

    def test_daily_many_survives_occupied_window(self):
        f = _facade()
        self.assertTrue(f._gm_ok(), "另一路拿走探针窗（GM 健康）")
        res = f.daily_many(["000001", "600000", "300750"], 20)
        self.assertEqual(len(res), 3, f"窗被占也应收齐（等待+重试）: {sorted(res)}")

    def test_daily_many_still_returns_empty_on_real_cooldown(self):
        """真降级（冷却窗）时仍返回空 —— 但必须**明确**，且不冒充"GM 不可用"以外的东西。"""
        f = _facade()
        f._gm_down_until = datetime.now() + timedelta(seconds=60)
        with self.assertLogs("market_data.facade", level="WARNING") as cm:
            res = f.daily_many(["000001", "600000"], 20)
        self.assertEqual(res, {})
        self.assertTrue(any("冷却窗" in m for m in cm.output),
                        f"日志要说清是冷却窗，不能只写 GM 不可用: {cm.output}")

    def test_batch_wait_is_longer_than_single(self):
        """批量是**一次**调用取多只，没有并发羊群问题 ⇒ 值得等更久。"""
        f = _facade()
        self.assertGreater(f._GM_PROBE_WAIT_BATCH_SEC, f._GM_PROBE_WAIT_SEC)


class TestTencentFormingGuard(unittest.TestCase):
    """T2：腾讯那一步失败，不许影响 GM 的判定。"""

    @staticmethod
    def _old_df(today="2026-10-10"):
        return pd.DataFrame([{"date": today, "open": 1.0, "high": 2.0,
                              "low": 0.5, "close": 1.5, "volume": 100}])

    def test_tencent_exception_keeps_df_and_does_not_mark_gm_down(self):
        f = _facade()
        marked = []
        f._note_gm_down = lambda *a, **k: marked.append(a)

        class _Boom:
            def snapshot(self, codes):
                raise RuntimeError("腾讯快照挂了")
        f._tx = _Boom()
        df = self._old_df()
        out = f._maybe_append_forming(df, "000001", now=datetime(2026, 10, 12, 11, 0))
        self.assertIs(out, df, "补 bar 失败要保持原 df（降级用昨日），不得抛")
        self.assertEqual(marked, [], "⚠️ 腾讯的异常绝不能记成 gm 不可达（会误熔断健康的 GM）")

    def test_tencent_timeout_also_guarded(self):
        f = _facade()

        class _Slow:
            def snapshot(self, codes):
                raise TimeoutError("read timed out")
        f._tx = _Slow()
        df = self._old_df()
        self.assertIs(f._maybe_append_forming(df, "000001",
                                              now=datetime(2026, 10, 12, 11, 0)), df)

    def test_window_inactive_skips_network_entirely(self):
        """周末/盘前不补 —— 且**根本不碰**腾讯（省一次 HTTP）。"""
        f = _facade()
        called = []

        class _Rec:
            def snapshot(self, codes):
                called.append(codes)
                return {}
        f._tx = _Rec()
        df = self._old_df()
        # 2026-10-10 是周六
        self.assertIs(f._maybe_append_forming(df, "000001",
                                              now=datetime(2026, 10, 10, 11, 0)), df)
        self.assertEqual(called, [], "非窗口内不得出网")

    def test_success_appends_bar(self):
        f = _facade()

        class _Snap:
            def snapshot(self, codes):
                return {str(codes[0]): {"ts_date": "2026-10-12", "price": 9.9,
                                        "open": 9.8, "high": 10.0, "low": 9.7, "volume": 5}}
        f._tx = _Snap()
        out = f._maybe_append_forming(self._old_df(), "000001",
                                      now=datetime(2026, 10, 12, 11, 0))
        self.assertEqual(len(out), 2, "应补上当日 bar")
        self.assertEqual(str(out["date"].iloc[-1]), "2026-10-12")


class TestTencentSnapshotTimeout(unittest.TestCase):
    """T3：8s 太宽 —— 单只快照实测中位 0.232s，打满 8s 的请求本就在挂。"""

    def test_timeout_tightened(self):
        self.assertLessEqual(TP._QT_SNAPSHOT_TIMEOUT, 3,
                             "单只快照超时不得再回到 8s（40 只里 93% 耗时被它拖住）")
        self.assertGreaterEqual(TP._QT_SNAPSHOT_TIMEOUT, 1, "太紧会误杀正常快照")


class TestTencentAuctionTimeout(unittest.TestCase):
    """T5：批量实时快照（qt.gtimg.cn）的 8s 超时要收紧。

    2026-10-12：**批量**请求同样会偶发挂满 ~8.2s（实测 n=200 那次 8.33s，数据其实回来了；
    n=50 × 30 次里 1 次 >3s）。8s 不是"等合法慢响应"，而是把"偶发挂死"放大成"阻塞 8 秒" ——
    前端在飞上限只有 6，一次 8.5s 就把整块界面拖慢（gui_slow_calls.log: load_quotes_for 8639ms）。
    """

    def test_auction_timeout_tightened(self):
        self.assertLessEqual(TP._QT_AUCTION_TIMEOUT, 4,
                             "成功路径仅 ~0.26s（n=50 中位），超时不得再回到 8s")
        self.assertGreaterEqual(TP._QT_AUCTION_TIMEOUT, 1, "太紧会误杀正常批量快照")

    def test_both_batch_snapshots_use_the_constant(self):
        """两处 gbk 批量快照都必须走常量 —— 别只改一处、另一处留硬编码 8s。"""
        import inspect
        for fn in (TP.TencentProvider.snapshot_auction, TP.TencentProvider.index_auction):
            src = inspect.getsource(fn)
            self.assertIn("_QT_AUCTION_TIMEOUT", src, f"{fn.__name__} 应使用 _QT_AUCTION_TIMEOUT")

    def test_daily_path_not_touched(self):
        """日线（utf-8 大 payload）**刻意不动** —— 它的慢可能是合法的，与本条不是同一风险。"""
        import inspect
        self.assertNotIn("_QT_AUCTION_TIMEOUT", inspect.getsource(TP.TencentProvider.daily))


class TestSnapshotAuctionChunking(unittest.TestCase):
    """T6：批量快照必须**分块** —— URL 一长就被腾讯静默拒绝，而调用方看不出来（2026-10-12）。

    实测悬崖：n=800（URL≈12000 字符）正常返 800 条；**n=1000（≈15000 字符）返回 0 条**，0.2s。
    `snapshot_auction` 里 `except: pass` + 空 body 都表现为 `{}` ⇒ **静默**。
    受害的是猎手盘中 live patch（传整池 1200~1500 只）⇒ 常年 patch 0 只，扫描退回上一交易日
    （实测 300821 显示 10-09 的 +20.01%，当天其实只有 +4.6%）。
    """

    def test_chunked_covers_whole_pool(self):
        """每次子调用都必须在悬崖之下；整池要能全覆盖。"""
        tp = TP.TencentProvider.__new__(TP.TencentProvider)
        seen = []

        def _fake(chunk):
            seen.append(len(chunk))
            assert len(chunk) <= 800, f"单块 {len(chunk)} 只已越悬崖（实测 1000 只返空）"
            return {str(c): {"price": 1.0, "ts_date": "2026-10-12"} for c in chunk}

        tp.snapshot_auction = _fake
        codes = [f"{i:06d}" for i in range(1400)]
        got = tp.snapshot_auction_many(codes)
        self.assertEqual(len(got), 1400, "整池必须全覆盖（分块后合并）")
        self.assertGreater(len(seen), 1, "1400 只绝不能只发一次请求")
        self.assertEqual(tp.last_chunk_failures, 0)

    def test_empty_chunk_surfaces_as_failure(self):
        """空块必须**可见**（记进 last_chunk_failures），不许静默 —— 这正是原缺陷的形态。"""
        tp = TP.TencentProvider.__new__(TP.TencentProvider)
        tp.snapshot_auction = lambda chunk: {}          # 模拟被腾讯静默拒绝
        got = tp.snapshot_auction_many([f"{i:06d}" for i in range(500)])
        self.assertEqual(got, {})
        self.assertEqual(tp.last_chunk_failures, 3, "500/200 ⇒ 3 块全空，应如实计数")

    def test_partial_failure_not_counted_as_failed(self):
        tp = TP.TencentProvider.__new__(TP.TencentProvider)
        calls = {"n": 0}

        def _flaky(chunk):
            calls["n"] += 1
            return {} if calls["n"] == 1 else {str(c): {"price": 2.0} for c in chunk}
        tp.snapshot_auction = _flaky
        got = tp.snapshot_auction_many([f"{i:06d}" for i in range(400)])
        self.assertEqual(tp.last_chunk_failures, 1)
        self.assertEqual(len(got), 200, "第 2 块的 200 只仍应拿到")

    def test_empty_input_safe(self):
        tp = TP.TencentProvider.__new__(TP.TencentProvider)
        self.assertEqual(tp.snapshot_auction_many([]), {})
        self.assertEqual(tp.snapshot_auction_many(None), {})
        self.assertEqual(tp.last_chunk_failures, 0)

    def test_chunk_size_under_cliff(self):
        self.assertLessEqual(TP._SNAPSHOT_CHUNK, 800,
                             "分块大小必须在静默拒绝的悬崖之下（实测 800 过 / 1000 挂）")


class TestLoadQuotesForContract(unittest.TestCase):
    """T7：`load_quotes_for` 的**返回契约**必须与 `load_quotes` 同款（包在 `quotes` 键里）。

    2026-10-12 踩过：后端返回**裸字典** `{code: {...}}`，而前端照 `load_quotes` 的写法读
    `r.quotes` ⇒ 永远读到 `{}` ⇒ 重试耗尽 ⇒ 猎手"实时涨幅"整列**永远 ⏳**。
    最坑的是**两侧各自的单测都发现不了**（后端返回的是它自己承诺的形状、前端读的是它以为的形状）
    —— 缺的是**跨边界的契约测试**。诊断日志一次定死：
      `hunter_hq req=47 got=47`  ←→  `hunter_hq_js req=47 got=0 patched=2438`
    """

    def _call(self, payload):
        import t_gui
        from core.market_data import tencent_provider as TP
        orig = TP.TencentProvider.snapshot_auction_many
        TP.TencentProvider.snapshot_auction_many = lambda self, codes, chunk_size=None: payload
        try:
            api = t_gui.Api.__new__(t_gui.Api)          # 不跑 __init__（会建一堆东西）
            api.report_client_diag = lambda *a, **k: None
            return api.load_quotes_for(["300821", "300019"])
        finally:
            TP.TencentProvider.snapshot_auction_many = orig

    def test_returns_quotes_wrapper(self):
        r = self._call({"300821": {"price": 18.0, "pct": 5.5, "ts_date": "2026-10-12"}})
        self.assertIsInstance(r, dict)
        self.assertIn("quotes", r, "前端按 `r.quotes` 读 ⇒ 后端必须包这一层")
        # 桩给的是 provider 口径的 `pct`，接口负责映射成对外的 `change_pct`
        self.assertEqual(r["quotes"]["300821"]["change_pct"], 5.5)
        self.assertEqual(r["count"], 1, "count 反映真正拿到几只")

    def test_empty_still_wrapped(self):
        """取不到也必须回 `{"quotes": {}}` —— 回裸 `{}` 会让前端把"没数据"当成"契约错"混在一起。"""
        r = self._call({})
        self.assertEqual(r.get("quotes"), {})
        self.assertEqual(r.get("count"), 0)

    def test_zero_price_filtered_out(self):
        """没有有效价的（停牌/无行情）不给值 —— 别拿 0 冒充"平盘"。"""
        r = self._call({"300821": {"price": 0, "pct": 0.0, "ts_date": "2026-10-12"},
                        "300019": {"price": 12.3, "pct": -1.1, "ts_date": "2026-10-12"}})
        self.assertNotIn("300821", r["quotes"])
        self.assertIn("300019", r["quotes"])
        self.assertEqual(r["count"], 1)


class TestGmDownMessage(unittest.TestCase):
    """T4：措辞要区分「超时」与「异常」——原来说法一律"服务不可达"，误导了一轮排查。"""

    def test_timeout_message_says_timeout(self):
        f = _facade()
        with self.assertLogs("market_data.facade", level="WARNING") as cm:
            f._note_gm_down("daily", "000001", TimeoutError("gm daily 调用超时>4.0s"))
        msg = "\n".join(cm.output)
        self.assertIn("超时", msg)
        self.assertNotIn("服务不可达", msg, "不得再断言'服务不可达'（GM 可能只是慢）")

    def test_generic_exception_message(self):
        f = _facade()
        with self.assertLogs("market_data.facade", level="WARNING") as cm:
            f._note_gm_down("daily", "000001", RuntimeError("some error"))
        msg = "\n".join(cm.output)
        self.assertIn("异常", msg)
        self.assertNotIn("服务不可达", msg)

    def test_sets_cooldown(self):
        """行为不变：仍然置 60s 冷却窗（措辞改了口径没改）。"""
        f = _facade()
        with self.assertLogs("market_data.facade", level="WARNING"):
            f._note_gm_down("daily", "000001", TimeoutError("x"))
        self.assertIsNotNone(f._gm_down_until)
        self.assertFalse(f._gm_ok(), "冷却窗内不再尝试 GM")


class _FakeQtResp:
    def __init__(self, body, will_close=False):
        self._body = body
        self.will_close = will_close

    def read(self):
        return self._body


class _FakeQtConn:
    """假 `http.client.HTTPSConnection`：**记下"一共新建了几条连接"** + 可控地让某条连接失效。

    这是本组测试的核心观测点 —— "复用"这件事只能靠"建了几条"来证，不能靠"没报错"。
    """

    made = []

    def __init__(self, host=None, timeout=None):
        self.host = host
        self.timeout = timeout
        self.sock = None            # `_qt_conn_take` 命中池时据此决定要不要重设超时
        self.closed = False
        self.n_req = 0
        self.broken = False         # True ⇒ 模拟"被对端静默关闭"，请求时抛
        _FakeQtConn.made.append(self)

    def request(self, method, path, headers=None):
        self.n_req += 1
        if self.broken:
            raise OSError("模拟 keep-alive 连接被对端静默关闭")

    def getresponse(self):
        return _FakeQtResp(b'v_sh600000="1~x~9.60~";\n')

    def close(self):
        self.closed = True


class TestTencentQtConnectionReuse(unittest.TestCase):
    """T6（2026-10-16）：qt.gtimg.cn **必须复用连接**，不许每个请求新建 TLS。

    实测（本机，同一 URL 连打 12 次）：裸 `urlopen` 中位 **350.9ms**、复用连接 **61.4ms**
    ⇒ 建连本身 ≈290ms/次。而 `snapshot()` 是**逐只**调的（`t_gui._live_quote_forming`
    → `snapshot([code])`）⇒ 一屏 50 只票 = 50 次握手 ≈14.5s，全压在后台线程上。
    2026-10-16 的卡死线程栈里**实测 5 个线程同时卡在 `socket.create_connection`**
    （另有 1 个在读 TLS）—— 这就是"打开 GUI 卡顿"的直接来源。

    ⚠️ 本组**全离线**：把 `http.client.HTTPSConnection` 换成桩，不发任何网络请求。
    """

    def setUp(self):
        import http.client
        TP._QT_POOL.clear()
        _FakeQtConn.made = []
        self._orig_conn = http.client.HTTPSConnection
        http.client.HTTPSConnection = _FakeQtConn

    def tearDown(self):
        import http.client
        http.client.HTTPSConnection = self._orig_conn
        TP._QT_POOL.clear()
        _FakeQtConn.made = []

    def test_reuses_the_same_connection(self):
        """连打 3 次只许建 1 条连接（这就是"5.7×"的来源）。"""
        for _ in range(3):
            TP._qt_get("/q=sh600000", 2)
        self.assertEqual(len(_FakeQtConn.made), 1,
                         "3 次请求建了 %d 条连接 —— 连接池没生效" % len(_FakeQtConn.made))
        self.assertEqual(_FakeQtConn.made[0].n_req, 3, "同一条连接上应发了 3 个请求")

    def test_stale_connection_recovers(self):
        """池里的连接被对端关掉后，下一次调用**必须仍然成功**（换新连接重试）。

        这是连接复用唯一的真实风险：keep-alive 的固有代价，不能让它把"本来能拿到的
        数据"变成拿不到。
        """
        self.assertTrue(TP._qt_get("/q=sh600000", 2))
        _FakeQtConn.made[0].broken = True       # 把池里那条弄脏
        out = TP._qt_get("/q=sh600000", 2)      # 不许抛
        self.assertIn("~", out)
        self.assertEqual(len(_FakeQtConn.made), 2, "脏连接应被弃用、另建一条")
        self.assertTrue(_FakeQtConn.made[0].closed, "弃用的连接要关掉（别漏 fd）")

    def test_failure_still_raises_when_both_attempts_fail(self):
        """两次都失败才抛 —— 调用方三个点的语义都是"失败即当作没数据"，与改前一致。"""
        import http.client

        class _AlwaysBroken(_FakeQtConn):
            def request(self, method, path, headers=None):
                raise OSError("boom")

        http.client.HTTPSConnection = _AlwaysBroken
        with self.assertRaises(OSError):
            TP._qt_get("/q=sh600000", 2)

    def test_pool_is_bounded(self):
        """池必须有上限 —— 否则每次并发峰值都留一条 fd，长期跑会把句柄耗光。"""
        conns = [_FakeQtConn() for _ in range(TP._QT_POOL_MAX + 5)]
        for c in conns:
            TP._qt_conn_put(c)
        self.assertLessEqual(len(TP._QT_POOL), TP._QT_POOL_MAX)
        self.assertTrue(all(c.closed for c in conns[TP._QT_POOL_MAX:]),
                        "超出上限的连接应当被关掉")

    def test_all_qt_endpoints_go_through_the_pool(self):
        """三个 qt.gtimg.cn 调用点都不许再裸 `urlopen`（每次新建 TLS）。

        ⚠️ 别只改一处：`_qt_snapshot_raw` 是**逐只**调的那个，漏了它等于没修。
        """
        import inspect
        for fn in (TP._qt_snapshot_raw,
                   TP.TencentProvider.snapshot_auction,
                   TP.TencentProvider.index_auction):
            src = inspect.getsource(fn)
            self.assertNotIn("urlopen", src,
                             f"{fn.__name__} 仍在裸 urlopen（每次新建 TLS 连接）")
            self.assertIn("_qt_get", src, f"{fn.__name__} 应走 _qt_get 复用连接")


if __name__ == "__main__":
    unittest.main(verbosity=2)
