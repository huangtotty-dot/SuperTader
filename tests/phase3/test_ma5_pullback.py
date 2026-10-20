# -*- coding: utf-8 -*-
"""「回踩5日线不破」轮询（core/ma5_pullback.py）的判定与推送纪律。

**全离线**：uni / ctx / snapshot_fn / push_fn 全部注入，不碰日线缓存与网络。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import core.ma5_pullback as mp   # noqa: E402

TODAY = "2026-10-20"


def _ctx(prev_close, prev_ma5, last4_sum=40.0):
    return {"prev_close": prev_close, "prev_ma5": prev_ma5, "last4_sum": last4_sum}


def _snap(price, low, ts=TODAY, pct=0.0):
    return {"price": price, "low": low, "ts_date": ts, "pct": pct}


class TestJudge:
    """四条判据逐条卡边界：① 昨收>昨MA5 ② 最低≤MA5*1.005 ②' 最低≥MA5*0.98 ③ MA5≤现价≤MA5*1.02"""

    def _run(self, ctx, snap):
        return mp.judge({"000001": ctx}, {"000001": snap}, TODAY)

    def test_normal_hit(self):
        # last4_sum=40, price=10.2 → ma5=(40+10.2)/5=10.04；low=10.0（-0.4%）在带内
        hits = self._run(_ctx(10.5, 10.3), _snap(10.2, 10.0))
        assert len(hits) == 1
        assert hits[0]["ma5"] == pytest.approx(10.04, abs=1e-6)
        assert hits[0]["dev5_pct"] == pytest.approx(1.59, abs=0.01)

    def test_reject_when_yesterday_below_ma5(self):
        """昨天就站在线下的票没有「回踩」可言（实测：去掉这条命中数 149→543）。"""
        assert self._run(_ctx(10.0, 10.3), _snap(10.2, 10.0)) == []

    def test_reject_when_low_not_touching(self):
        # ma5=10.04，最低 10.1 离线上方 0.6% > 0.5% ⇒ 没回踩到
        assert self._run(_ctx(10.5, 10.3), _snap(10.2, 10.1)) == []

    def test_reject_when_broken_too_deep(self):
        """跌破 2% 以上又拉回 = 破位后反抽，不是「回踩不破」。"""
        assert self._run(_ctx(10.5, 10.3), _snap(10.2, 9.8)) == []

    def test_reject_when_price_flew_away(self):
        # 现价 10.3 比 ma5(10.06) 高 2.4% > 2% ⇒ 已经反弹走，不是回踩
        assert self._run(_ctx(10.5, 10.3), _snap(10.3, 10.0)) == []

    def test_reject_when_price_below_ma5(self):
        """现价掉到 MA5 下方 = 破了，不算「不破」。"""
        assert self._run(_ctx(10.5, 10.3), _snap(9.98, 9.9)) == []

    def test_zero_low_is_dropped(self):
        """`low=0` 是坏快照 —— 只判 `is None` 会算成「跌了 100%」（实测踩到过 −100.0%）。"""
        assert self._run(_ctx(10.5, 10.3), _snap(10.2, 0)) == []

    def test_stale_snapshot_is_dropped(self):
        """停牌/陈旧快照的 ts_date 不是今天 ⇒ 那个「最低」不是今天的。"""
        assert self._run(_ctx(10.5, 10.3), _snap(10.2, 10.0, ts="2026-10-17")) == []

    def test_missing_quote_is_dropped(self):
        assert self._run(_ctx(10.5, 10.3), {}) == []

    def test_sorted_by_distance_to_ma5(self):
        ctx = {"a": _ctx(10.5, 10.3), "b": _ctx(10.5, 10.3)}
        snap = {"a": _snap(10.15, 10.0), "b": _snap(10.05, 10.0)}
        hits = mp.judge(ctx, snap, TODAY)
        assert [h["code"] for h in hits] == ["b", "a"]     # 越贴近线越靠前


class TestUniverse:
    def test_merge_and_source_tags(self, monkeypatch):
        monkeypatch.setattr(mp, "holdings_codes", lambda: {"600000": "浦发银行"})
        monkeypatch.setattr(mp, "pb_codes", lambda: {"600000": "浦发银行", "000001": "平安银行"})
        monkeypatch.setattr(mp, "hunter_codes", lambda top_n=None: {"000001": "平安银行"})
        u = mp.universe(10)
        assert set(u) == {"600000", "000001"}
        assert u["600000"]["sources"] == ["ob", "pb"]
        assert u["000001"]["sources"] == ["pb", "hunter"]

    def test_holdings_skip_zero_qty(self, tmp_path, monkeypatch):
        fp = tmp_path / "holdings.json"
        fp.write_text(json.dumps({
            "600000": {"qty": 100, "name": "浦发银行"},
            "600001": {"qty": 0, "name": "已清仓"},
            "_meta": {"x": 1},
        }), encoding="utf-8")
        monkeypatch.setattr(mp, "_HOLDINGS_FP", str(fp))
        assert mp.holdings_codes() == {"600000": "浦发银行"}


class TestRunOnce:
    """推送纪律：每只每日一次；推送失败不写去重（下轮重试）。"""

    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mp, "_DEDUP_FP", str(tmp_path / "pushed.json"))

    def _args(self):
        uni = {"000001": {"name": "平安银行", "sources": ["hunter"]}}
        ctx = {"000001": _ctx(10.5, 10.3)}
        snap = {"000001": _snap(10.2, 10.0)}

        def snapshot_fn(codes):
            return snap
        return uni, ctx, snapshot_fn

    def test_push_once_then_dedup(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        uni, ctx, snap_fn = self._args()
        sent = []
        r1 = mp.run_once(now=datetime(2026, 10, 20, 10, 0), uni=uni, ctx=ctx,
                         snapshot_fn=snap_fn, push_fn=lambda c: sent.append(c) or True)
        assert r1["pushed"] and len(r1["hits"]) == 1
        r2 = mp.run_once(now=datetime(2026, 10, 20, 10, 30), uni=uni, ctx=ctx,
                         snapshot_fn=snap_fn, push_fn=lambda c: sent.append(c) or True)
        assert r2["pushed"] is False and r2["hits"] == []      # 当天已推过
        assert len(sent) == 1

    def test_push_failure_does_not_write_dedup(self, tmp_path, monkeypatch):
        """本仓血案：推送失败却写了去重 ⇒ 该票当天永远不会再报。"""
        self._setup(tmp_path, monkeypatch)
        uni, ctx, snap_fn = self._args()
        r1 = mp.run_once(now=datetime(2026, 10, 20, 10, 0), uni=uni, ctx=ctx,
                         snapshot_fn=snap_fn, push_fn=lambda c: False)
        assert r1["pushed"] is False and len(r1["hits"]) == 1
        assert mp._load_dedup() == {}
        # 下一轮仍会重试
        sent = []
        r2 = mp.run_once(now=datetime(2026, 10, 20, 10, 30), uni=uni, ctx=ctx,
                         snapshot_fn=snap_fn, push_fn=lambda c: sent.append(c) or True)
        assert r2["pushed"] is True and len(sent) == 1

    def test_empty_universe_is_not_ok(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        r = mp.run_once(uni={}, ctx={}, snapshot_fn=lambda c: {}, push_fn=lambda c: True)
        assert r["ok"] is False and "名单" in r["reason"]


class TestCard:
    def _hits(self, n):
        return [{"code": f"{600000 + i}", "price": 10.0 + i / 100, "ma5": 10.0,
                 "low": 9.98, "dev5_pct": round(i / 100, 2), "touch_pct": -0.2,
                 "pct": 0.1, "sources": ["hunter"]} for i in range(n)]

    def test_groups_by_source(self):
        uni = {"600000": {"name": "甲", "sources": ["ob"]},
               "600001": {"name": "乙", "sources": ["hunter"]}}
        hits = [{"code": "600000", "price": 10.0, "ma5": 9.9, "low": 9.85,
                 "dev5_pct": 1.0, "touch_pct": -0.5, "pct": 0.2, "sources": ["ob"]},
                {"code": "600001", "price": 11.0, "ma5": 11.0, "low": 10.95,
                 "dev5_pct": 0.0, "touch_pct": -0.45, "pct": -0.2, "sources": ["hunter"]}]
        md = mp.build_card(hits, datetime(2026, 10, 20, 14, 30), uni)["card"]["elements"][0]["content"]
        assert "体检持仓" in md and "猎手前10板块" in md
        assert md.index("体检持仓") < md.index("猎手前10板块")     # 体检优先展示
        assert "甲" in md and "乙" in md

    def test_row_cap(self, monkeypatch):
        monkeypatch.setattr(mp, "CARD_MAX_ROWS", 3)
        md = mp.build_card(self._hits(5), datetime(2026, 10, 20, 14, 30), {})["card"]["elements"][0]["content"]
        assert md.count("· `") == 3
        assert "另有" in md
