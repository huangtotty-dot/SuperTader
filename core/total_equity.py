# -*- coding: utf-8 -*-
"""账户总资产计算（2026-11-13 从 main.py 抽出）：P0-9(B1) 单股上限所需真实总资产。

`_TOTAL_EQUITY_CACHE` 是**专属全局**（只被本函数读/写），抽出后 main.py 只需
`from core.total_equity import _compute_total_equity`。`HOLDINGS`（config）与
`DAILY_DECISION_STATS`（core.utils）只读，原地更新语义不变。
"""
from __future__ import annotations

import json
import os
import time

from config import HOLDINGS, log
from core.utils import DAILY_DECISION_STATS

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_TOTAL_EQUITY_CACHE = {"ts": 0.0, "value": 0.0}  # fix P0-9(B1): total_equity 缓存


def _compute_total_equity() -> float:
    """fix P0-9(B1): 从 t_io/state/accounts_config.json 计算账户总资产
    （账户A现金+持仓市值 + 账户B现金+持仓市值）。
    现金 ≈ total_capital - 持仓成本（忽略已实现盈亏/手续费）；持仓市值用快照最新价。
    2026-08-30: 账户配置合并为 accounts_config.json 唯一源头，旧 portfolio_config.json 仅回退。
    """
    now_ts = time.time()
    if _TOTAL_EQUITY_CACHE["value"] > 0 and now_ts - _TOTAL_EQUITY_CACHE["ts"] < 300:
        return _TOTAL_EQUITY_CACHE["value"]
    equity = 0.0
    try:
        fp = os.path.join(_ROOT, "t_io", "state", "accounts_config.json")
        if not os.path.exists(fp):
            fp = os.path.join(_ROOT, "t_io", "state", "portfolio_config.json")  # 旧部署回退
        with open(fp, encoding="utf-8") as f:
            cfg = json.load(f)
        capital_by_acct = {k: float((v or {}).get("total_capital") or 0)
                           for k, v in (cfg.get("accounts") or {}).items()}
        cost_by_acct = {k: 0.0 for k in capital_by_acct}
        mv_by_acct = {k: 0.0 for k in capital_by_acct}
        # 2026-09-14 并表：holdings.json 不再存 `account` 字段 → 归属改由 accounts_config.json
        # 各账户的 holdings 清单派生（该清单本就是账户归属的声明处）。
        _code_acct = {}
        for _a, _v in (cfg.get("accounts") or {}).items():
            for _hc in ((_v or {}).get("holdings") or []):
                _code_acct[str(_hc).split("_")[0]] = _a
        _default_acct = "账户A" if "账户A" in capital_by_acct else next(iter(capital_by_acct), None)
        for _c, _h in (HOLDINGS or {}).items():
            acct = _code_acct.get(str(_c).split("_")[0]) or _default_acct
            if acct is None or acct not in capital_by_acct:
                continue
            _q = int(_h.get("qty") or 0)
            if _q <= 0:
                continue
            _cost = float(_h.get("cost") or 0)
            _dec = DAILY_DECISION_STATS.get(_c) or {}
            _price = float(_dec.get("last_price") or 0) or float(_h.get("pre_close") or 0) or _cost
            cost_by_acct[acct] += _cost * _q
            mv_by_acct[acct] += _price * _q
        for acct, cap in capital_by_acct.items():
            cash = max(0.0, cap - cost_by_acct.get(acct, 0.0))
            equity += cash + mv_by_acct.get(acct, 0.0)
    except Exception as e:
        log.warning(f"⚠️ total_equity 计算失败（本轮单股上限检查跳过）: {str(e)[:120]}")
        return 0.0
    _TOTAL_EQUITY_CACHE["ts"] = now_ts
    _TOTAL_EQUITY_CACHE["value"] = equity
    return equity
