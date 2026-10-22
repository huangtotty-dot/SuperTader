# -*- coding: utf-8 -*-
"""飞书推送换岗：**停掉选股猎手那两路，改由异常监控池实时播报**（owner 2026-10-22）。

owner 原话：「是否站上5日线或者5日线不破推送飞书 取消选股猎手的推送飞书，但是异常监控池
需要实时播报」。

所以：
  · `hunter_ma5_alert.run_live_watch`（站上5日线，20s）观察名单 = **异常监控池**；
  · `ma5_pullback`（回踩5日线不破，30min）的三个来源 = 体检持仓 + 建仓池 + **异常监控池**
    （原来的「猎手前 10 热门板块」被换掉）；
  · 池空 ⇒ 这两路都不推送（猎手那套不再默认启用，`universe="hunter"` 仅留作旧口径）。
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

from core import hunter_ma5_alert as H   # noqa: E402
from core import ma5_pullback as P       # noqa: E402

_MAIN = (_ROOT / "main.py").read_text(encoding="utf-8")


@pytest.fixture()
def pool(tmp_path, monkeypatch):
    fp = tmp_path / "abnormal_pool.json"
    fp.write_text(json.dumps({"stocks": {"002929": {"name": "润建股份"},
                                        "600519": {"name": "贵州茅台"}}}), encoding="utf-8")
    monkeypatch.setattr(H, "_POOL_FP", str(fp))
    return fp


class TestPoolStocks:
    def test_reads_pool(self, pool):
        assert H.pool_stocks() == {"002929": "润建股份", "600519": "贵州茅台"}

    def test_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(H, "_POOL_FP", str(tmp_path / "nope.json"))
        assert H.pool_stocks() == {}


class TestLiveContextUniverse:
    def test_pool_is_default(self, pool):
        ctx = H.build_live_context(universe="pool")
        assert set(ctx["codes"]) <= {"002929", "600519"}
        assert ctx["stocks"], "池里的票要进观察名单"

    def test_hunter_universe_still_available(self, pool, monkeypatch):
        """旧口径保留（测试/CLI 用），但不再是默认。"""
        monkeypatch.setattr(H, "hot_board_stocks", lambda top_n=None: {"600000": "浦发银行"})
        ctx = H.build_live_context(universe="hunter")
        assert list(ctx["stocks"]) == ["600000"]


class TestCardWording:
    def _ev(self):
        return [{"code": "002929", "name": "润建股份", "price": 57.0,
                 "ma5": 55.5, "dev5_pct": 2.7, "kind": "overnight"}]

    def test_title_says_pool(self):
        card = H.build_card(self._ev(), universe="pool")
        assert "异常监控池" in card["card"]["header"]["title"]["content"]
        body = card["card"]["elements"][0]["content"]
        assert "异常监控池" in body
        assert "猎手" not in card["card"]["header"]["title"]["content"]

    def test_hunter_title_preserved(self):
        card = H.build_card(self._ev(), universe="hunter")
        assert "猎手" in card["card"]["header"]["title"]["content"]


class TestWiring:
    def test_main_starts_pool_universe(self):
        i = _MAIN.index("def _start_hunter_ma5_watch")
        seg = _MAIN[i:i + 2000]
        assert '"universe": "pool"' in seg, "watcher 必须显式用异常监控池名单"

    def test_pullback_uses_pool_source(self):
        assert '("pool", pool_codes(top_n))' in (_ROOT / "core/ma5_pullback.py").read_text(encoding="utf-8")
        assert "def pool_codes" in (_ROOT / "core/ma5_pullback.py").read_text(encoding="utf-8")

    def test_pullback_labels(self):
        assert P._SRC_TITLE["pool"].endswith("异常监控池")
        assert "hunter" not in P._SRC_ORDER
