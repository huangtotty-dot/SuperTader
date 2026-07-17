# -*- coding: utf-8 -*-
"""
股票池管理模块
"""
import os
import json
from typing import Dict, Tuple, List

import pandas as pd

import config
from config import (
    log, BASE_DIR, LEGACY_WATCHLIST_FILE, MIN_MARKET_CAP, EXCLUDED_SECTORS,
    CONCEPT_CACHE, UNIVERSE_CACHE_FILE, A_SHARE_CODE_PREFIXES, CACHE_DIR
)

def normalize_code(code) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return digits.zfill(6)


def load_legacy_concept_map() -> Dict[str, str]:
    if config.LEGACY_CONCEPT_MAP:
        return config.LEGACY_CONCEPT_MAP
    try:
        if os.path.exists(LEGACY_WATCHLIST_FILE):
            with open(LEGACY_WATCHLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            config.LEGACY_CONCEPT_MAP = {
                str(code): _sanitize_sector(str(info.get("sector", "")))
                for code, info in data.items()
                if isinstance(info, dict) and _sanitize_sector(str(info.get("sector", "")))
            }
            log.debug(f"✓ 旧概念映射加载成功: {len(config.LEGACY_CONCEPT_MAP)} 条")
    except Exception as e:
        log.debug(f"⚠️  旧概念映射加载失败: {str(e)[:60]}")
    return config.LEGACY_CONCEPT_MAP


def load_ths_concept_cache() -> Dict[str, dict]:
    return {}


def _sanitize_sector(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "-":
        return ""
    if value in {"未知板块", "全部市场", "全市场", "A股全市场", "A股全市场"}:
        return ""
    return value


def _normalize_sector_text(value: str) -> str:
    return _sanitize_sector(value).replace(" ", "")


def resolve_stock_concept(code: str, fallback_sector: str = "未知板块") -> str:
    if code in CONCEPT_CACHE:
        return CONCEPT_CACHE[code]

    concept = _sanitize_sector(fallback_sector)
    if not concept:
        legacy_map = load_legacy_concept_map()
        if code in legacy_map:
            concept = _sanitize_sector(legacy_map.get(code, ""))
    if not concept:
        concept = "未知板块"

    CONCEPT_CACHE[code] = concept
    return CONCEPT_CACHE[code]


def resolve_business_summary(info: Dict[str, str]) -> str:
    if not isinstance(info, dict):
        return ""
    for key in ("business_summary", "主营业务", "公司简介", "简介", "业务简介"):
        text = str(info.get(key, "")).strip()
        if text:
            return truncate_reason(text, limit=180)
    return ""


def format_business_brief(text: str, limit: int = 18) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = text.replace("：", "").replace(":", "")
    return truncate_reason(text, limit=limit)


def wrap_business_text(text: str, width: int = 16, max_lines: int = 3) -> str:
    text = str(text or "").strip()
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ")
    chunks = [text[i:i + width] for i in range(0, len(text), width)]
    if len(chunks) > max_lines:
        chunks = chunks[:max_lines]
        if chunks[-1] and not chunks[-1].endswith("…"):
            chunks[-1] = chunks[-1].rstrip() + "…"
    return "\n".join(chunks)


def is_st_stock(code: str, name: str = "") -> bool:
    text = f"{code}{name}".upper()
    return "ST" in text or "*ST" in text


def is_bse_stock(code: str) -> bool:
    return str(code).startswith(("4", "8", "9"))


def is_a_share_code(code: str) -> bool:
    code = str(code).strip()
    return len(code) == 6 and code.isdigit() and code.startswith(A_SHARE_CODE_PREFIXES)


def normalize_pool_row(code: str, name: str, sector: str = "未知板块", business_summary: str = "") -> Dict[str, str]:
    info = {"name": name or code, "sector": sector or "未知板块"}
    if business_summary:
        info["business_summary"] = business_summary
    return info


def _to_float(value) -> float:
    try:
        if value in (None, "", "-"):
            return 0.0
        return float(str(value).replace(",", "").strip())
    except Exception:
        return 0.0


def load_spot_market_cap_map() -> Dict[str, float]:
    if isinstance(config.SPOT_MARKET_CAP_MAP, dict):
        return config.SPOT_MARKET_CAP_MAP
    mapping: Dict[str, float] = {}
    try:
        import akshare as ak
        spot = ak.stock_zh_a_spot_em()
        if spot is None or spot.empty:
            config.SPOT_MARKET_CAP_MAP = mapping
            return mapping
        code_col = None
        cap_col = None
        for col in spot.columns:
            text = str(col)
            lower = text.lower()
            if code_col is None and any(k in lower for k in ("代码", "code", "证券代码")):
                code_col = col
            if cap_col is None and any(k in text for k in ("总市值", "总市值(元)", "总市值（元）", "总市值(亿)", "总市值（亿）")):
                cap_col = col
        if code_col is None or cap_col is None:
            config.SPOT_MARKET_CAP_MAP = mapping
            return mapping
        for _, row in spot[[code_col, cap_col]].iterrows():
            code = normalize_code(row.iloc[0])
            cap = _to_float(row.iloc[1])
            if not code or cap <= 0:
                continue
            mapping[code] = cap
    except Exception:
        mapping = {}
    config.SPOT_MARKET_CAP_MAP = mapping
    return mapping


def extract_market_cap(info: Dict[str, str], code: str = "") -> float:
    if not isinstance(info, dict):
        return 0.0
    for key in ("market_cap", "market_capitalization", "总市值", "总市值(元)", "总市值（元）", "总市值_元", "总市值(亿)", "总市值（亿）"):
        value = info.get(key)
        cap = _to_float(value)
        if cap > 0:
            if any(unit in key for unit in ("(亿)", "（亿）")):
                return cap * 100000000
            return cap

    code = str(code).strip()
    if not code:
        return 0.0

    try:
        spot_map = load_spot_market_cap_map()
        if code in spot_map:
            return spot_map[code]
    except Exception:
        return 0.0
    return 0.0


def passes_universe_filters(code: str, name: str, info: Dict[str, str] = None) -> bool:
    if not is_a_share_code(code):
        return False
    if is_st_stock(code, name):
        return False
    if isinstance(info, dict):
        market_cap = extract_market_cap(info, code)
        if market_cap and market_cap < MIN_MARKET_CAP:
            return False
        sector_candidates = [
            info.get("sector", ""),
            info.get("industry", ""),
            info.get("所属行业", ""),
            info.get("行业", ""),
            info.get("concept", ""),
        ]
        for raw_sector in sector_candidates:
            sector = _normalize_sector_text(raw_sector)
            if sector and any(sector == _normalize_sector_text(item) or sector in _normalize_sector_text(item) or _normalize_sector_text(item) in sector for item in EXCLUDED_SECTORS):
                return False
    return True


def load_pool_from_df(df: pd.DataFrame, loader_name: str, code_idx: int = 0, name_idx: int = 1) -> Dict[str, Dict[str, str]]:
    pool = {}
    if df is None or df.empty:
        return pool

    cols = [str(c) for c in df.columns]
    sector_idx = None
    for i, col in enumerate(cols):
        lower = col.lower()
        if any(k in lower for k in ["所属行业", "行业", "板块", "concept", "概念"]):
            sector_idx = i
            break

    for _, row in df.iterrows():
        try:
            if len(row) <= max(code_idx, name_idx):
                continue
            code = str(row.iloc[code_idx]).strip()
            name = str(row.iloc[name_idx]).strip()
            sector = str(row.iloc[sector_idx]).strip() if sector_idx is not None and len(row) > sector_idx else "未知板块"
            info = normalize_pool_row(code, name, sector)
            if not passes_universe_filters(code, name, info):
                continue
            pool[code] = info
        except Exception:
            continue

    if pool:
        log.debug(f"✓ 全A股清单加载成功({loader_name}): {len(pool)} 只")
    return pool


def load_a_share_pool() -> Dict[str, Dict[str, str]]:
    log.debug("🔄 正在加载全A股股票清单...")
    log.debug(f"📌 股票池过滤：ST 已忽略，市值低于 {MIN_MARKET_CAP / 100000000:.0f} 亿已忽略")

    pool = {}
    if os.path.exists(LEGACY_WATCHLIST_FILE):
        try:
            with open(LEGACY_WATCHLIST_FILE, "r", encoding="utf-8") as f:
                legacy_pool = json.load(f)
            if isinstance(legacy_pool, dict) and legacy_pool:
                raw_total = 0
                raw_missing_name = 0
                raw_missing_sector = 0
                valid_count = 0
                for code, info in legacy_pool.items():
                    raw_total += 1
                    if not isinstance(info, dict):
                        continue
                    code = str(code).strip()
                    name = str(info.get("name", code)).strip() or code
                    sector = str(info.get("sector", "")).strip()
                    if not name:
                        raw_missing_name += 1
                    if not sector:
                        raw_missing_sector += 1
                    if not passes_universe_filters(code, name, info):
                        continue
                    business_summary = str(info.get("business_summary", "")).strip()
                    pool[code] = normalize_pool_row(code, name, sector or "未知板块", business_summary)
                    valid_count += 1
                if raw_total < 4500 or raw_missing_name > 0 or raw_missing_sector > 0:
                    log.debug(
                        f"⚠️  watchlist.json 原始数据完整性不足: total={raw_total} missing_name={raw_missing_name} missing_sector={raw_missing_sector}"
                    )
                    return {}
                if pool:
                    log.debug(f"✓ 从 watchlist.json 直接加载股票池成功: {len(pool)} 只")
        except Exception as e:
            log.debug(f"⚠️  读取 watchlist.json 失败: {str(e)[:80]}")

    if pool:
        log.debug(f"✓ 全A股清单最终加载完成: {len(pool)} 只")
        return pool

    log.debug("⚠️  watchlist.json 未能加载任何股票")
    return {}


def clear_cache():
    """清空缓存目录（保留股票池缓存）"""
    try:
        if os.path.exists(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                file_path = os.path.join(CACHE_DIR, f)
                if os.path.isfile(file_path) and os.path.abspath(file_path) != os.path.abspath(UNIVERSE_CACHE_FILE):
                    os.remove(file_path)
        log.debug("缓存已清空（保留股票池缓存）")
        return True
    except Exception as e:
        log.debug(f"缓存清空失败: {str(e)}")
        return False


