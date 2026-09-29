# -*- coding: utf-8 -*-
"""代码映射（codec）——三套代码格式的唯一转换点（合并实施方案 §0.1）。
禁止在业务代码里再写 startswith(("5","6")) 判断。

格式：
  内部纯 6 位        "600481"
  内部多账户后缀     "600481_A" / "600481_B"（superTrader 特有，goldminer 无此概念）
  GM 格式            "SHSE.600481" / "SZSE.002639" / "BJSE.430047"
"""
import logging

log = logging.getLogger("market_data.codec")

_GM_PREFIX = {"SH": "SHSE.", "SZ": "SZSE.", "BJ": "BJSE."}
_TX_PREFIX = {"SH": "sh", "SZ": "sz", "BJ": "bj"}


def strip_account(code: str) -> str:
    """"600481_A"→"600481"；无后缀原样返回。"""
    return str(code).split("_")[0]


def market_of(code: str) -> str:
    """"600481"→"SH"、"002639"→"SZ"、"430047"→"BJ"。

    规则（按序）：
      920xxx / 4xxxxx / 8xxxxx → BJ（北交所）
      5/6/9 开头               → SH（沪市，含 900xxx 沪B）
      其余                     → SZ
    """
    c = strip_account(str(code))
    if not c:
        return "SZ"
    if c.startswith("920") or c[0] in "48":
        return "BJ"
    return "SH" if c[0] in "569" else "SZ"


def to_gm(code: str) -> str:
    """"600481"→"SHSE.600481"；"600481_A"→"SHSE.600481"（后缀剥离，记 warning）。

    ⚠️ BJSE. 前缀仅保证**格式正确**：实测 2026-09-29 GM（history_n/history，BJSE./BSE./BJ.
    三种前缀）对北交所**一律返回 0 行**。北交所日线另走 `bj_daily`（见该模块），
    本映射是为将来 GM 支持时无需再改与格式一致性。
    """
    c = strip_account(str(code))
    if "_" in str(code):
        log.warning("codec.to_gm 剥离多账户后缀: %s → %s", code, c)
    return _GM_PREFIX.get(market_of(c), "SZSE.") + c


def to_tx(code: str) -> str:
    """"600481"→"sh600481"、"430047"→"bj430047"（腾讯/东财通用的小写前缀符号）。

    本仓此前在 tencent_provider、stock_hunter/modules/market_data 里各写了一份
    `"sh" if code[0] in "56" else "sz"` —— 正是本模块 docstring 明令禁止的重复。
    北交所（4/8/920）必须走 `bj`：实测 `bj430047` 报价可用，而 `sz430047` 不被识别。
    """
    c = strip_account(str(code))
    return _TX_PREFIX.get(market_of(c), "sz") + c


def to_internal(gm_code: str) -> str:
    """"SHSE.600481"→"600481"（兼容小写 sh/sz/bj 前缀）。"""
    g = str(gm_code).strip()
    for p in ("SHSE.", "SZSE.", "BJSE.", "sh", "sz", "bj"):
        if g.upper().startswith(p.upper()):
            return g[len(p):]
    return g
