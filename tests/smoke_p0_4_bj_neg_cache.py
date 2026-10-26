# -*- coding: utf-8 -*-
"""P0-4 北交所日内负缓存冒烟（全离线，monkeypatch 计数）。

对一只北交所码连续两次调 `_fetch_kline_multi`：
  第 1 次：四源全失败（桩）→ 返回 None，且网络/兜底被实际调用；
  第 2 次：负缓存命中 → 返回 None，且**不再触碰任何网络/兜底调用**。
另验证跨交易日自动失效（改缓存日期后重新走网络）。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from stock_hunter.modules import market_data as md  # noqa: E402

calls = {"net": 0, "provider": 0}


def _boom(*a, **k):
    calls["net"] += 1
    raise OSError("模拟 ifzq 全挂")


class _FakeProvider:
    def daily(self, code, days):
        calls["provider"] += 1
        import pandas as pd
        return pd.DataFrame()          # 兜底也拿不到（北交所现状）


md.pooled_get = _boom                                   # 断 ifzq 双机
import core.market_data as _cmd                         # noqa: E402
_cmd.get_provider = lambda: _FakeProvider()             # 断 provider 兜底
md._BJ_NEG_CACHE.update({"date": None, "codes": set()})

f = md.MarketDataFetcher()
assert f._fetch_kline_multi("bj430047") is None
n1 = (calls["net"], calls["provider"])
assert calls["net"] == 2 and calls["provider"] == 1, f"第 1 次应走全链路: {calls}"

assert f._fetch_kline_multi("bj430047") is None
assert (calls["net"], calls["provider"]) == n1, f"第 2 次应跳过网络: {calls}"
print(f"PASS 负缓存生效：第 1 次 net={n1[0]} provider={n1[1]}，第 2 次零网络调用")

# 跨交易日失效：把缓存日期改成昨天，下一调用应重新走网络
md._BJ_NEG_CACHE["date"] = "2000-01-01"
assert f._fetch_kline_multi("bj430047") is None
assert calls["provider"] == 2, "跨交易日应自动失效并重试"
print("PASS 跨交易日自动失效")
