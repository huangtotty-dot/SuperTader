# -*- coding: utf-8 -*-
"""轻量交易规则常量与判定（仅标准库，不导入行情/配置依赖）。"""


MAIN_BOARD_LIMIT_PCT = 9.5
CHINEXT_STAR_LIMIT_PCT = 19.5
BSE_LIMIT_PCT = 29.5
ST_LIMIT_PCT = 4.5


def limit_pct(code, is_st: bool = False) -> float:
    """返回猎手涨停旗标使用的近似阈值（%），保留既有四档口径。

    ST 优先；30/68 开头为创业板/科创板，8/9 开头沿用猎手既有北交所口径，
    其余按沪深主板。这里判断的是展示旗标，不替代交易所精确涨停价规则。
    """
    if is_st:
        return ST_LIMIT_PCT
    c = str(code or "").split("_")[0].strip()
    if c.startswith(("30", "68")):
        return CHINEXT_STAR_LIMIT_PCT
    if c.startswith(("8", "9")):
        return BSE_LIMIT_PCT
    return MAIN_BOARD_LIMIT_PCT
