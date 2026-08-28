# -*- coding: utf-8 -*-
"""代码映射（codec）——三套代码格式的唯一转换点（合并实施方案 §0.1）。
禁止在业务代码里再写 startswith(("5","6")) 判断。

格式：
  内部纯 6 位        "600481"
  内部多账户后缀     "600481_A" / "600481_B"（superTrader 特有，goldminer 无此概念）
  GM 格式            "SHSE.600481" / "SZSE.002639"
"""
import logging

log = logging.getLogger("market_data.codec")


def strip_account(code: str) -> str:
    """"600481_A"→"600481"；无后缀原样返回。"""
    return str(code).split("_")[0]


def market_of(code: str) -> str:
    """"600481"→"SH"；规则：5/6/9 开头=SH，其余=SZ。"""
    c = strip_account(str(code))
    return "SH" if c[0] in "569" else "SZ"


def to_gm(code: str) -> str:
    """"600481"→"SHSE.600481"；"600481_A"→"SHSE.600481"（后缀剥离，记 warning）。"""
    c = strip_account(str(code))
    if "_" in str(code):
        log.warning("codec.to_gm 剥离多账户后缀: %s → %s", code, c)
    return ("SHSE." if market_of(c) == "SH" else "SZSE.") + c


def to_internal(gm_code: str) -> str:
    """"SHSE.600481"→"600481"（兼容小写 sh/sz 前缀）。"""
    g = str(gm_code).strip()
    for p in ("SHSE.", "SZSE.", "sh", "sz"):
        if g.upper().startswith(p.upper()):
            return g[len(p):]
    return g
