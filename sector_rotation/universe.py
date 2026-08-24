from __future__ import annotations

from typing import Any

import pandas as pd


MAIN_BOARD_PREFIXES = ("000", "001", "002", "003", "600", "601", "603", "605")
CHINEXT_PREFIXES = ("300", "301")
STAR_PREFIXES = ("688", "689")
BSE_PREFIXES = ("4", "8", "9")


def clean_code(value: Any) -> str:
    return str(value or "").replace(".0", "").strip().zfill(6)


def is_st_name(value: Any) -> bool:
    name = str(value or "").upper().replace("＊", "*").strip()
    return "ST" in name or "退" in name


def board_segment(code: Any) -> str:
    cleaned = clean_code(code)
    if cleaned.startswith(CHINEXT_PREFIXES):
        return "创业板"
    if cleaned.startswith(STAR_PREFIXES):
        return "科创板"
    if cleaned.startswith(MAIN_BOARD_PREFIXES):
        return "沪深主板"
    if cleaned.startswith(BSE_PREFIXES):
        return "北交所"
    return "其他"


def is_main_board_tradable(code: Any, name: Any = "") -> bool:
    return board_segment(code) == "沪深主板" and not is_st_name(name)


def is_leader_recommendable(code: Any, name: Any = "") -> bool:
    return not is_st_name(name)


def append_market_universe_columns(frame: pd.DataFrame) -> pd.DataFrame:
    # 2026-08-22 性能: 32万行 map/listcomp 逐行调用极慢(占 build ~6s)；改为 pandas 向量化
    enriched = frame.copy()
    codes = (enriched["代码"].astype(str)
             .str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6))
    enriched["代码"] = codes
    seg = pd.Series("其他", index=codes.index)
    for label, prefixes in (("创业板", CHINEXT_PREFIXES), ("科创板", STAR_PREFIXES),
                            ("沪深主板", MAIN_BOARD_PREFIXES), ("北交所", BSE_PREFIXES)):
        seg[codes.str.startswith(prefixes)] = label
    enriched["证券板块"] = seg
    if "名称" in enriched.columns:
        names = (enriched["名称"].astype(str)
                 .str.upper().str.replace("＊", "*", regex=False).str.strip())
        is_st = names.str.contains("ST|退", regex=True, na=False)
        enriched["是否ST"] = is_st
        enriched["可推荐龙头"] = ~is_st
        enriched["主板可推荐"] = (seg == "沪深主板") & ~is_st
    else:
        enriched["是否ST"] = False
        enriched["可推荐龙头"] = True
        enriched["主板可推荐"] = seg == "沪深主板"
    return enriched
