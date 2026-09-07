# -*- coding: utf-8 -*-
"""
core/board_index.py — 个股 → 所属板块指数映射（分板规则单一来源，2026-09-07 D1）

原 resolve_index 位于 analysis/index_resonance.py:58，被买卖多侧消费。A-1 提升为公共函数，
所有消费点 import 本模块同一函数，禁止第二份前缀规则。config.INDEX_RESONANCE_MAP 覆盖表不变
（个股/ETF 显式覆盖在默认板块规则之上）。
"""
_OVERRIDES = None


def _overrides() -> dict:
    """config.INDEX_RESONANCE_MAP 覆盖表（惰性读取 + 单例缓存，防 config 重依赖拖慢热路径）。"""
    global _OVERRIDES
    if _OVERRIDES is None:
        try:
            from config import INDEX_RESONANCE_MAP
            _OVERRIDES = INDEX_RESONANCE_MAP or {}
        except Exception:
            _OVERRIDES = {}
    return _OVERRIDES


def resolve_index(code: str):
    """个股代码 → (index_code, index_name)。剥离 _A/_B 后缀，先查覆盖表，再按板块默认。

    与 2026-09-07 前 index_resonance.resolve_index 行为逐字节一致（A-1 纯搬移）：
      60xxxx    → sh000001 上证指数（沪主板）
      68/588xxx → sh000688 科创50（科创板/科创 ETF）
      30xxxx    → sz399006 创业板指
      00/001/002/003 → sz399001 深证成指（深主板）
    """
    base = str(code).split("_")[0]
    ov = _overrides().get(base)
    if ov:
        return ov[0], ov[1]
    if base.startswith("60"):
        return "sh000001", "上证指数"
    if base.startswith(("68", "588")):
        return "sh000688", "科创50"
    if base.startswith("30"):
        return "sz399006", "创业板指"
    if base.startswith(("00", "001", "002", "003")):
        return "sz399001", "深证成指"
    return "sh000001", "上证指数"


def index_gm_symbol(index_code: str) -> str:
    """指数代码 → GM 全称（供 auto 侧 history_n / GM_INDEX_CACHE 键）：sh000001→SHSE.000001、
    sz399006→SZSE.399006；已是 GM 格式（含 "SHSE."/"SZSE."）原样返回（大小写归一）。"""
    c = str(index_code)
    if "." in c or c.upper().startswith(("SHSE", "SZSE")):
        return c.upper()
    if c.startswith(("sh", "SH")):
        return "SHSE." + c[2:]
    if c.startswith(("sz", "SZ")):
        return "SZSE." + c[2:]
    return c
