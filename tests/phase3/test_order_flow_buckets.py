# -*- coding: utf-8 -*-
"""10min 多空分布（core/order_flow.py）的纯计算部分：分桶边界 / 方向归类 / 分档 / 瘦身。

**不出网**：只测 `_bucket_label` / `_parse_tick` / `_add` / `_round_bucket` / `_index_like`。
取数与缓存由 `tmp/` 里的调研脚本对着真实腾讯接口验过（600900：4749 笔 / 68 页 / 5.6s）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.order_flow import (          # noqa: E402
    _bucket_label, _parse_tick, _add, _new_bucket, _round_bucket, _index_like, _tier_of,
)


class TestBucketLabel:
    """桶标签 = 桶**右端**时刻，与 K 线弹窗 10min 周期（m5 两根配一对）同口径。"""

    def test_open_side(self):
        assert _bucket_label("09:25:01") == "09:40"    # 集合竞价 → 第一根
        assert _bucket_label("09:30:00") == "09:40"    # 开盘瞬间
        assert _bucket_label("09:31:00") == "09:40"
        assert _bucket_label("09:40:00") == "09:40"    # 边界点归**左**桶（(09:30,09:40]）
        assert _bucket_label("09:40:01") == "09:50"

    def test_lunch(self):
        assert _bucket_label("11:30:00") == "11:30"
        assert _bucket_label("12:00:00") == "13:10"    # 午休 → 下午第一根
        assert _bucket_label("13:00:00") == "13:10"
        assert _bucket_label("13:05:00") == "13:10"
        assert _bucket_label("13:10:01") == "13:20"

    def test_close(self):
        assert _bucket_label("14:50:00") == "14:50"
        assert _bucket_label("14:50:30") == "15:00"
        assert _bucket_label("15:00:00") == "15:00"
        assert _bucket_label("15:00:30") == "15:00"    # 收盘竞价成交仍归最后一根

    def test_after_hours_excluded(self):
        assert _bucket_label("15:01:00") is None       # 盘后定价不属于任何 K 线
        assert _bucket_label("15:30:00") is None

    def test_bad_input(self):
        assert _bucket_label("") is None
        assert _bucket_label("abc") is None


class TestBucketLabel5min:
    """5 分档（2026-10-20）：桶边界与 K 线弹窗的 5 分周期（腾讯原生 m5）同口径。"""

    def test_open_side(self):
        assert _bucket_label("09:25:01", 5) == "09:35"   # 集合竞价 → 第一根 m5
        assert _bucket_label("09:30:00", 5) == "09:35"
        assert _bucket_label("09:35:00", 5) == "09:35"
        assert _bucket_label("09:35:01", 5) == "09:40"

    def test_lunch(self):
        assert _bucket_label("11:30:00", 5) == "11:30"
        assert _bucket_label("12:00:00", 5) == "13:05"
        assert _bucket_label("13:00:00", 5) == "13:05"
        assert _bucket_label("13:05:00", 5) == "13:05"
        assert _bucket_label("13:05:01", 5) == "13:10"

    def test_close(self):
        assert _bucket_label("14:50:30", 5) == "14:55"
        assert _bucket_label("15:00:30", 5) == "15:00"
        assert _bucket_label("15:01:00", 5) is None

    def test_bad_step_falls_back_to_10(self):
        assert _bucket_label("09:31:00", 7) == "09:40"

    def test_parse_tick_respects_step(self):
        tk = _parse_tick("1/09:31:00/29.54/0.00/100/50000/B", 5)
        assert tk["label"] == "09:35"
        tk10 = _parse_tick("1/09:31:00/29.54/0.00/100/50000/B", 10)
        assert tk10["label"] == "09:40"


class TestParseTick:
    def test_normal_buy(self):
        tk = _parse_tick("1/09:30:02/29.54/0.00/1803/5323919/B")
        assert tk["flag"] == "B" and tk["vol"] == 1803 and tk["amt"] == 5323919
        assert tk["label"] == "09:40"

    def test_auction_forced_neutral(self):
        # 09:25 集合竞价：腾讯标 S，但方向无意义 ⇒ 必须按中性计（否则首根多空比被凭空压低）
        tk = _parse_tick("0/09:25:01/29.54/0.00/3244/9582776/S")
        assert tk["flag"] == "M"

    def test_after_hours_dropped(self):
        assert _parse_tick("9/15:10:00/29.54/0.00/147/434000/B") is None

    def test_garbage(self):
        assert _parse_tick("") is None
        assert _parse_tick("x/y/z") is None
        assert _parse_tick("0/09:30:02/29.54/0.00/100/1000/?") is None


class TestAggregation:
    def test_buy_sell_counts_split_by_tier(self):
        b = _new_bucket("09:40")
        _add(b, {"flag": "B", "vol": 100, "amt": 1_500_000})   # 超大单
        _add(b, {"flag": "B", "vol": 50, "amt": 300_000})      # 大单
        _add(b, {"flag": "S", "vol": 80, "amt": 50_000})       # 中单
        _add(b, {"flag": "M", "vol": 20, "amt": 10_000})       # 中性
        assert b["buy_vol"] == 150 and b["sell_vol"] == 80 and b["neutral_vol"] == 20
        assert b["tiers"]["超大单"]["buy"] == 1_500_000
        assert b["tiers"]["大单"]["buy"] == 300_000
        assert b["tiers"]["中单"]["sell"] == 50_000
        assert b["n"] == 4

    def test_tier_thresholds(self):
        assert _tier_of(1_000_000) == "超大单"
        assert _tier_of(999_999) == "大单"
        assert _tier_of(200_000) == "大单"
        assert _tier_of(199_999) == "中单"
        assert _tier_of(40_000) == "中单"
        assert _tier_of(39_999) == "小单"
        assert _tier_of(0) == "小单"

    def test_round_bucket_contract(self):
        b = _new_bucket("09:40")
        _add(b, {"flag": "B", "vol": 100, "amt": 3_000_000})
        _add(b, {"flag": "S", "vol": 300, "amt": 1_000_000})
        out = _round_bucket(b)
        assert out["buy_ratio"] == 0.25
        assert out["net_amt"] == 2_000_000
        assert set(out) >= {"time", "n", "buy_vol", "sell_vol", "neutral_vol",
                            "buy_amt", "sell_amt", "tiers", "buy_ratio", "net_amt"}

    def test_zero_trade_bucket_no_ratio(self):
        # 只有中性（如一字板/无主动成交）：buy_ratio 必须是 None，不能编个 0 或 1
        b = _new_bucket("09:40")
        _add(b, {"flag": "M", "vol": 10, "amt": 30000})
        assert _round_bucket(b)["buy_ratio"] is None


class TestIncrementalCache:
    """增量缓存的几个反直觉点：`n_ticks` 单日**单调不减**、收盘后不再出网。

    ⚠️ 时间一律**钉死**（monkeypatch `of.datetime`）：本仓已经吃过"测试跟着真实时钟腐烂"的亏
    （`test_chart_realtime_20261017` 就是），新测试不能再埋一个。
    """

    @staticmethod
    def _freeze(of, monkeypatch, hh=9, mm=30):
        class _DT:
            @staticmethod
            def now():
                from datetime import datetime as _d
                return _d(2026, 10, 20, hh, mm, 0)
        monkeypatch.setattr(of, "datetime", _DT, raising=False)

    def _seed(self, of, tmp):
        b = of._new_bucket("09:40")
        of._add(b, {"flag": "B", "vol": 100, "amt": 500_000})
        rec = {"code": "600900", "date": "2026-10-20", "saved_at": "2026-10-20 09:00:00",
               "n_ticks": 70, "pages": 1, "last": "09:40",
               # 尾巴必须是**盘中**的一笔（09:25 竞价会被强制按中性计 ⇒ 重算后买量归 0）
               "tail": ["0/09:31:00/29.54/0.00/100/500000/B"],
               "bars": {"09:40": of._round_bucket(b)}}
        of._write_cache("600900", "2026-10-20", rec)
        return rec

    def test_n_ticks_never_shrinks_on_empty_fetch(self, monkeypatch, tmp_path):
        """本轮一页没抓到（网络抖动）时，n_ticks 不许被尾巴序号缩回去 —— 否则下次增量重翻几十页。"""
        import core.order_flow as of
        monkeypatch.setattr(of, "_CACHE_DIR", tmp_path)
        self._freeze(of, monkeypatch, 9, 30)
        monkeypatch.setattr(of, "_fetch_pages", lambda *a, **k: ([], 0))
        self._seed(of, tmp_path)
        r = of.load("600900")
        assert r["ok"] is True
        assert r["n_ticks"] == 70
        assert r["bars"][0]["buy_vol"] == 100          # 桶数据没被空抓破坏

    def test_unrecognized_code_returns_cache(self, monkeypatch, tmp_path):
        import core.order_flow as of
        monkeypatch.setattr(of, "_CACHE_DIR", tmp_path)
        self._freeze(of, monkeypatch, 9, 30)
        self._seed(of, tmp_path)
        monkeypatch.setattr(of, "_tx_symbol", lambda c: None)
        r = of.load("600900")
        assert r["ok"] is True and len(r["bars"]) == 1

    def test_closed_market_returns_cache_without_network(self, monkeypatch, tmp_path):
        import core.order_flow as of
        monkeypatch.setattr(of, "_CACHE_DIR", tmp_path)
        self._freeze(of, monkeypatch, 16, 0)           # 收盘后
        rec = self._seed(of, tmp_path)
        rec["saved_at"] = "2026-10-20 15:30:00"
        of._write_cache("600900", "2026-10-20", rec)

        def _boom(*a, **k):
            raise AssertionError("收盘后不该再出网")
        monkeypatch.setattr(of, "_fetch_pages", _boom)
        r = of.load("600900")
        assert r["ok"] is True and r["n_ticks"] == 70


class TestIndexLike:
    def test_index_codes(self):
        assert _index_like("sh000001") is True
        assert _index_like("sz399001") is True
        assert _index_like("bj899050") is True

    def test_bare_code_is_stock(self):
        # 裸 000001 是平安银行，不是上证指数 —— 别把个股当指数
        assert _index_like("000001") is False
        assert _index_like("600900") is False
        assert _index_like("sh600900") is False
