# -*- coding: utf-8 -*-
"""两市成交额：GM 不可用时用**新浪**兜底（2026-10-21）。

owner：「盘中两市的成交额经常报读不到数据」。根因是两条腿（上证指数 + 深证综指）**只走 GM**
（`facade.index_minute`，GM 不可用直接回空表、注释就写着"由调用方决定"），而 GM 会因终端重启 /
令牌轮换 / 60s 冷却窗而不可用 ⇒ 卡片只剩一行「分钟线不可得(sh000001)」。

新浪 `CN_MarketDataService.getKLineData` 给 sh000001 / sz399106 的 5 分钟线**与 GM 逐日完全一致**
（2026-10-19/20/21：沪 6870.1/6999.7/6427.5 亿、深 7505.0/7561.4/7865.9 亿，浮点都不差）
⇒ 可直接顶替。另加两道闸：
  · **冷却窗内不再去敲 GM**（展示卡片不值得付 12s 硬超时）；
  · **新鲜度**：盘中拿到的最新一天不是今天 ⇒ 换源；两源都不新鲜（假期/盘前）时仍回退给
    已有的那份（显示上一交易日**全日**额是对的，不能每逢假期就空白）。
"""
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

import t_gui   # noqa: E402
from core.market_data import facade as F   # noqa: E402
from core.market_data import sina_index   # noqa: E402

_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


class _DT:
    def __init__(self, fake):
        self._f = fake

    def now(self, tz=None):
        return self._f


def _frame(day: str, amount_dev: float, n: int = 4):
    """造一段 5 分钟线：时间落在那一天的 09:35 起，amount 是**元**。"""
    times = pd.to_datetime([f"{day} 09:{35 + 5 * i:02d}:00" for i in range(n)])
    return pd.DataFrame({"time": times, "open": 1.0, "high": 1.0, "low": 1.0,
                         "close": 1.0, "volume": 100.0, "amount": [amount_dev] * n})


def _mk(*pairs):
    """[(day, amount), …] → 一条腿的分钟线。

    ⚠️ 必须**至少两天**：`_turnover_from_minutes` 要拿"上一交易日"做同期/全日对比，
    只有一天会直接判 `分钟线里没有上一交易日`。
    """
    return pd.concat([_frame(d, a) for d, a in pairs], ignore_index=True)


_TODAY = "2026-10-21"
_PREV = "2026-10-20"
_PREV2 = "2026-10-19"


class _Prov:
    """假 provider：按 sym 返回帧，或抛异常/记调用。"""

    def __init__(self, frames=None, boom=False):
        self.frames = frames or {}
        self.boom = boom
        self._gm_down_until = None
        self.calls = []

    def index_minute(self, sym, count_bars=800, freq="300s"):
        self.calls.append(sym)
        if self.boom:
            raise RuntimeError("gm down")
        return self.frames.get(sym, pd.DataFrame())


@pytest.fixture()
def patch_env(monkeypatch):
    def _apply(prov, sina_frames):
        monkeypatch.setattr(F, "get_provider", lambda: prov)
        monkeypatch.setattr(sina_index, "fetch_index_minutes",
                            lambda sym, scale=5, datalen=400:
                            sina_frames.get(sym, pd.DataFrame()))
        monkeypatch.setattr(t_gui, "datetime", _DT(datetime(2026, 10, 21, 10, 0)))
    return _apply


_GM = {"sh000001": _mk((_PREV, 1e10), (_TODAY, 1e10)),
       "sz399106": _mk((_PREV, 2e10), (_TODAY, 2e10))}
_SINA = {"sh000001": _mk((_PREV, 1e10), (_TODAY, 1e10)),
         "sz399106": _mk((_PREV, 2e10), (_TODAY, 2e10)),
         "bj899050": _mk((_PREV, 1e9), (_TODAY, 1e9))}


class TestFallback:
    def test_normal_uses_gm(self, patch_env):
        prov = _Prov(_GM)
        patch_env(prov, _SINA)
        out = t_gui.Api()._market_turnover()
        assert out["available"] is True
        assert prov.calls == ["sh000001", "sz399106"]
        assert "index_minute_sina" not in (out.get("degraded") or [])

    def test_gm_empty_falls_back_to_sina(self, patch_env):
        patch_env(_Prov({}), _SINA)          # GM 回空表（不可用时的真实行为）
        out = t_gui.Api()._market_turnover()
        assert out["available"] is True
        # 每腿 4 根 5 分钟线：沪 4×1e10 + 深 4×2e10 + 北 4×1e9
        assert out["amount"] == pytest.approx(4e10 + 8e10 + 4e9), "沪深各 4 根 × 额度 + 北交所"
        assert "index_minute_sina" in out["degraded"]

    def test_gm_exception_falls_back(self, patch_env):
        patch_env(_Prov(boom=True), _SINA)
        out = t_gui.Api()._market_turnover()
        assert out["available"] is True and "index_minute_sina" in out["degraded"]

    def test_cooldown_does_not_call_gm(self, patch_env):
        prov = _Prov(_GM)
        from datetime import timedelta
        prov._gm_down_until = datetime(2026, 10, 21, 10, 0) + timedelta(seconds=60)
        patch_env(prov, _SINA)
        out = t_gui.Api()._market_turnover()
        assert prov.calls == [], "冷却窗内不该再去敲 GM（要付 12s 硬超时）"
        assert out["available"] is True and "index_minute_sina" in out["degraded"]


class TestFreshness:
    def _stale_everywhere(self):
        """两源都停在 10-20（"今天"10-21 没有 bar）。"""
        return ({"sh000001": _mk((_PREV2, 1e10), (_PREV, 1e10)),
                 "sz399106": _mk((_PREV2, 2e10), (_PREV, 2e10))},
                {"sh000001": _mk((_PREV2, 1e10), (_PREV, 1e10)),
                 "sz399106": _mk((_PREV2, 2e10), (_PREV, 2e10)),
                 "bj899050": _mk((_PREV2, 1e9), (_PREV, 1e9))})

    def test_intraday_stale_gm_uses_sina(self, patch_env):
        gm, sina = self._stale_everywhere()
        sina["sh000001"] = _mk((_PREV, 1e10), (_TODAY, 5e10))
        sina["sz399106"] = _mk((_PREV, 2e10), (_TODAY, 6e10))
        sina["bj899050"] = _mk((_PREV, 1e9), (_TODAY, 1e9))
        patch_env(_Prov(gm), sina)
        out = t_gui.Api()._market_turnover()
        assert out["available"] is True
        assert out["basis"] == "同期"
        assert out["day"] == _TODAY, "GM 停在昨天 ⇒ 必须换到新鲜的新浪"
        assert "index_minute_sina" in out["degraded"]

    def test_both_stale_still_returns_prev_day(self, patch_env):
        """假期/盘前：两源都停在上一交易日 ⇒ **照常显示上一交易日全日额**，不能空白。"""
        gm, sina = self._stale_everywhere()
        patch_env(_Prov(gm), sina)
        out = t_gui.Api()._market_turnover()
        assert out["available"] is True, "两个源都不新鲜 ≠ 没数据"
        assert out["basis"] == "全日"
        assert "index_minute_prevday" in out["degraded"]


class TestFastRetry:
    def test_failure_ttl_is_short(self):
        assert t_gui.Api._turnover_ttl_for({"available": False}) <= 10.0
        assert t_gui.Api._turnover_ttl_for({"available": True}) == t_gui.Api._TURNOVER_TTL

    def test_frontend_shows_warming_not_error(self):
        i = _JS.index("function turnoverCard")
        seg = _JS[i:i + 900]
        assert "warming" in seg and "取数中" in seg, "warming 不能被渲染成「不可用」"
