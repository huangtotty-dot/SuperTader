# -*- coding: utf-8 -*-
"""共享 keep-alive 池 `core/market_data/http_pool.py` 的离线单测 + 5 处裸 urlopen 迁移钉死。

2026-10-20：把 `tencent_provider` 的 keep-alive 池抽成全仓库共享的 `http_pool`，并把
东财/新浪/猎手/指数背离/指数日内 5 处裸 `urlopen` 统一接到它上（此前每个请求都新建
DNS+TCP+TLS，本机实测**建连本身 ≈290ms**）。

本文件**全离线**（把 `http.client.HTTPSConnection` 换成桩，不发任何网络请求），两件事：
  1. 钉 `pooled_get` 的复用 / 解码 / 按主机分桶行为（镜像 test_market_data_gm_gate 的 T6）。
  2. 钉 5 处调用点**不许再回裸 urlopen**（每次新建 TLS）。

运行：python -m unittest tests.phase3.test_http_pool_shared -v
"""
import inspect
import sys
import unittest
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.market_data import http_pool as HP  # noqa: E402


class _FakeResp:
    def __init__(self, body, will_close=False):
        self._body = body
        self.will_close = will_close

    def read(self):
        return self._body


class _FakeConn:
    """假 `http.client.HTTPSConnection`：记下「一共新建了几条连接」+ 可控返回体。"""

    made = []
    resp_body = b""          # 本轮所有实例统一返回体（测试里设）

    def __init__(self, host=None, timeout=None, broken=False):
        self.host = host
        self.timeout = timeout
        self.sock = None
        self.closed = False
        self.n_req = 0
        self.broken = broken
        _FakeConn.made.append(self)

    def request(self, method, path, headers=None):
        self.n_req += 1
        if self.broken:
            raise OSError("模拟 keep-alive 连接被对端静默关闭")

    def getresponse(self):
        return _FakeResp(_FakeConn.resp_body)

    def close(self):
        self.closed = True


def _reset_shared_pools():
    """**就地清空**每个主机桶的空闲连接，而不是把 `_POOLS` 的键删掉。

    ⚠️ 2026-10-20 踩到的坑：`tencent_provider` 在 import 时就绑定了
    `_QT_POOL = host_pool(_QT_HOST)["pool"]`（一个 list 的别名）。若这里 `pop` 掉整个桶，
    别名就指向一个**孤儿列表** ⇒ 下一家测试 `TP._QT_POOL.clear()` 清的是孤儿，
    真正的池里还留着本文件造的假连接 ⇒ `test_market_data_gm_gate` 的连接复用断言
    “3 次请求建了 0 条连接”而红（单跑却全绿，典型的跨文件污染）。
    只要桶还在、只清空里面的连接，别名就始终有效。
    """
    for _h, rec in list(HP._POOLS.items()):
        for c in list(rec.get("pool") or []):
            try:
                c.close()
            except Exception:
                pass
        (rec.get("pool") or []).clear()


class TestPooledGet(unittest.TestCase):
    def setUp(self):
        import http.client
        _FakeConn.made = []
        _FakeConn.resp_body = b""
        _reset_shared_pools()
        self._orig = http.client.HTTPSConnection
        http.client.HTTPSConnection = _FakeConn

    def tearDown(self):
        import http.client
        http.client.HTTPSConnection = self._orig
        _reset_shared_pools()
        _FakeConn.made = []
        _FakeConn.resp_body = b""

    def test_reuses_same_connection_same_host(self):
        for _ in range(3):
            HP.pooled_get("h.example", "/x", 2, {}, encoding="utf-8")
        self.assertEqual(len(_FakeConn.made), 1, "同主机 3 次请求只许建 1 条连接")
        self.assertEqual(_FakeConn.made[0].n_req, 3)

    def test_buckets_by_host(self):
        """不同主机不得共用一条 keep-alive 连接（绑死建连 host）。"""
        HP.pooled_get("a.example", "/x", 2, {}, encoding="utf-8")
        HP.pooled_get("b.example", "/x", 2, {}, encoding="utf-8")
        self.assertEqual(len(_FakeConn.made), 2, "两个主机必须各自建连")

    def test_utf8_decode(self):
        _FakeConn.resp_body = "涨跌".encode("utf-8")
        out = HP.pooled_get("h.example", "/x", 2, {}, encoding="utf-8")
        self.assertEqual(out, "涨跌")

    def test_gbk_decode(self):
        _FakeConn.resp_body = "涨跌".encode("gbk")
        out = HP.pooled_get("h.example", "/x", 2, {}, encoding="gbk")
        self.assertEqual(out, "涨跌")

    def test_stale_connection_recovers(self):
        """池里连接被对端关掉后，下一次调用必须仍成功（换新连接重试一次）。"""
        self.assertEqual(HP.pooled_get("h.example", "/x", 2, {}, encoding="utf-8"), "")
        _FakeConn.made[0].broken = True
        HP.pooled_get("h.example", "/x", 2, {}, encoding="utf-8")   # 不许抛
        self.assertEqual(len(_FakeConn.made), 2, "脏连接应被弃用、另建一条")
        self.assertTrue(_FakeConn.made[0].closed)

    def test_failure_raises_after_both_attempts(self):
        import http.client

        class _AlwaysBroken(_FakeConn):
            def request(self, method, path, headers=None):
                raise OSError("boom")
        http.client.HTTPSConnection = _AlwaysBroken
        with self.assertRaises(OSError):
            HP.pooled_get("h.example", "/x", 2, {}, encoding="utf-8")


class TestNoBareUrlopen(unittest.TestCase):
    """5 处迁移点**不许再回裸 urlopen**（每次新建 TLS）。"""

    def _assert_pooled(self, fn):
        src = inspect.getsource(fn)
        self.assertNotIn("urlopen", src, f"{fn.__module__}.{fn.__qualname__} 仍在裸 urlopen")
        self.assertIn("pooled_get", src, f"{fn.__module__}.{fn.__qualname__} 应走 pooled_get 复用连接")

    def test_bj_daily(self):
        from core.market_data import bj_daily
        self._assert_pooled(bj_daily.fetch_bj_daily)

    def test_sina_index(self):
        from core.market_data import sina_index
        self._assert_pooled(sina_index.fetch_index_minutes)

    def test_index_divergence_em_kline(self):
        from analysis import index_divergence
        self._assert_pooled(index_divergence._em_kline)

    def test_index_regime_intraday_http_get_json(self):
        from analysis import index_regime_intraday
        self._assert_pooled(index_regime_intraday._iri_http_get_json)

    def test_hunter_fetch_kline_multi(self):
        from stock_hunter.modules import market_data
        self._assert_pooled(market_data.MarketDataFetcher._fetch_kline_multi)

    # ── P0-5(2026-07-29)：tencent_provider 最后 4 处裸直连收编进连接池 ──
    def test_tencent_daily(self):
        from core.market_data.tencent_provider import TencentProvider
        self._assert_pooled(TencentProvider.daily)

    def test_tencent_index_daily(self):
        from core.market_data.tencent_provider import TencentProvider
        self._assert_pooled(TencentProvider.index_daily)

    def test_tencent_minute(self):
        from core.market_data.tencent_provider import TencentProvider
        self._assert_pooled(TencentProvider.minute)

    def test_tencent_index_minute(self):
        from core.market_data.tencent_provider import TencentProvider
        self._assert_pooled(TencentProvider.index_minute)


if __name__ == "__main__":
    unittest.main(verbosity=2)
