# -*- coding: utf-8 -*-
"""Offline generator for watchlist.json."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Tuple

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ[key] = ""

import akshare as ak
import pandas as pd

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.json")
CACHE_DIR = os.path.join(BASE_DIR, "cache")
BOARD_CACHE_FILE = os.path.join(CACHE_DIR, "watchlist_boards.json")
THS_CONCEPTS_FILE = os.path.join(CACHE_DIR, "ths_concepts.json")
UNIVERSE_CACHE_FILE = os.path.join(CACHE_DIR, "a_share_pool.json")
A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
WATCHLIST_MIN_EXPECTED = int(os.getenv("WATCHLIST_MIN_EXPECTED", "4500"))


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path: str, data) -> None:
    ensure_dir(os.path.dirname(path))
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def normalize_code(code) -> str:
    text = str(code or "").strip()
    if not text:
        return ""
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 6:
        return digits[-6:]
    return digits.zfill(6)


def is_a_share_code(code: str) -> bool:
    code = normalize_code(code)
    return len(code) == 6 and code.isdigit() and code.startswith(A_SHARE_PREFIXES)


def is_st_stock(code: str, name: str = "") -> bool:
    text = f"{code}{name}".upper()
    return "ST" in text or "*ST" in text


def find_column(df: pd.DataFrame, candidates: Iterable[str]) -> str:
    if df is None or df.empty:
        return ""
    cols = [str(c) for c in df.columns]
    for cand in candidates:
        for col in df.columns:
            text = str(col)
            low = text.lower()
            if cand in text or cand in low:
                return col
    return ""


def extract_code_name_df(df: pd.DataFrame) -> Dict[str, str]:
    if df is None or df.empty:
        return {}
    code_col = ""
    name_col = ""
    for col in df.columns:
        text = str(col)
        low = text.lower()
        if not code_col and any(k in low for k in ("code", "代码", "证券代码", "a股代码")):
            code_col = col
        if not name_col and any(k in low for k in ("name", "名称", "简称", "证券简称", "a股简称")):
            name_col = col
    if not code_col and len(df.columns) >= 1:
        code_col = df.columns[0]
    if not name_col and len(df.columns) >= 2:
        name_col = df.columns[1]
    if not code_col:
        return {}

    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        code = normalize_code(row.get(code_col, ""))
        if not is_a_share_code(code):
            continue
        name = str(row.get(name_col, code)).strip() if name_col else code
        if not name:
            name = code
        if is_st_stock(code, name):
            continue
        out[code] = name
    return out


def extract_board_name_df(df: pd.DataFrame) -> List[str]:
    if df is None or df.empty:
        return []
    name_col = ""
    for col in df.columns:
        text = str(col)
        low = text.lower()
        if any(k in low for k in ("板块名称", "名称", "概念名称", "行业名称", "板块", "概念")):
            name_col = col
            break
    if not name_col and len(df.columns) >= 1:
        name_col = df.columns[0]
    if not name_col:
        return []
    names = []
    for item in df[name_col].astype(str).tolist():
        text = item.strip()
        if text and text != "-" and text not in names:
            names.append(text)
    return names


def extract_member_codes(df: pd.DataFrame) -> Dict[str, str]:
    if df is None or df.empty:
        return {}
    code_col = ""
    name_col = ""
    for col in df.columns:
        text = str(col)
        low = text.lower()
        if not code_col and any(k in low for k in ("代码", "code", "证券代码")):
            code_col = col
        if not name_col and any(k in low for k in ("名称", "name", "简称", "证券简称")):
            name_col = col
    if not code_col:
        if len(df.columns) >= 1:
            code_col = df.columns[0]
        else:
            return {}
    if not name_col and len(df.columns) >= 2:
        name_col = df.columns[1]

    out: Dict[str, str] = {}
    for _, row in df.iterrows():
        code = normalize_code(row.get(code_col, ""))
        if not is_a_share_code(code):
            continue
        name = str(row.get(name_col, code)).strip() if name_col else code
        if not name:
            name = code
        out[code] = name
    return out


def normalize_concept_name(value: str) -> str:
    text = str(value or "").strip()
    if not text or text == "-":
        return ""
    return text.replace("　", " ")


def extract_ths_concepts() -> Dict[str, dict]:
    concepts: Dict[str, dict] = {}
    try:
        name_df = ak.stock_board_concept_name_ths()
    except Exception:
        return concepts
    if name_df is None or name_df.empty:
        return concepts

    name_col = ""
    code_col = ""
    for col in name_df.columns:
        text = str(col)
        low = text.lower()
        if not name_col and any(k in low for k in ("name", "名称", "概念", "板块")):
            name_col = col
        if not code_col and any(k in low for k in ("code", "代码")):
            code_col = col
    if not name_col and len(name_df.columns) >= 1:
        name_col = name_df.columns[0]
    if not code_col and len(name_df.columns) >= 2:
        code_col = name_df.columns[1]
    if not name_col or not code_col:
        return concepts

    for _, row in name_df.iterrows():
        name = normalize_concept_name(row.get(name_col, ""))
        code = str(row.get(code_col, "")).strip()
        if not name:
            continue
        entry = {
            "name": name,
            "code": code,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            info_df = ak.stock_board_concept_info_ths(symbol=name)
            if info_df is not None and not info_df.empty and set(info_df.columns) >= {"项目", "值"}:
                info_map = {}
                for k, v in zip(info_df["项目"].astype(str), info_df["值"].astype(str)):
                    info_map[k.strip()] = v.strip()
                entry["info"] = info_map
                entry["summary"] = info_map.get("概念简介", "") or info_map.get("简介", "") or info_map.get("概念说明", "")
        except Exception:
            pass
        concepts[code or name] = entry
    return concepts


def load_existing_watchlist() -> Dict[str, dict]:
    data = read_json(WATCHLIST_FILE, {})
    if not isinstance(data, dict):
        return {}
    out: Dict[str, dict] = {}
    for code, info in data.items():
        code = normalize_code(code)
        if not is_a_share_code(code) or not isinstance(info, dict):
            continue
        out[code] = copy.deepcopy(info)
        out[code]["name"] = str(out[code].get("name", code)).strip() or code
        sector = str(out[code].get("sector", "")).strip()
        if sector:
            out[code]["sector"] = sector
    return out


def check_watchlist_completeness(data: Dict[str, dict], min_expected: int = WATCHLIST_MIN_EXPECTED) -> Tuple[bool, dict]:
    stats = {
        "total": 0,
        "valid": 0,
        "invalid_code": 0,
        "missing_name": 0,
        "missing_sector": 0,
        "missing_both": 0,
        "min_expected": int(min_expected),
    }
    if not isinstance(data, dict):
        return False, stats

    for raw_code, info in data.items():
        stats["total"] += 1
        code = normalize_code(raw_code)
        if not is_a_share_code(code) or not isinstance(info, dict):
            stats["invalid_code"] += 1
            continue
        name = str(info.get("name", "")).strip()
        sector = str(info.get("sector", "")).strip()
        if not name and not sector:
            stats["missing_both"] += 1
        if not name:
            stats["missing_name"] += 1
        if not sector:
            stats["missing_sector"] += 1
        stats["valid"] += 1

    complete = stats["valid"] >= stats["min_expected"] and stats["invalid_code"] == 0 and stats["missing_both"] == 0
    return complete, stats


def ensure_watchlist_complete(refresh: bool = True) -> Tuple[Dict[str, dict], dict]:
    existing = load_existing_watchlist()
    complete, stats = check_watchlist_completeness(existing)
    if complete:
        return existing, stats

    rebuilt = generate(refresh=refresh, cache_only=False, preview=False)
    complete, rebuilt_stats = check_watchlist_completeness(rebuilt)
    stats.update({"rebuilt_complete": complete, "rebuilt_valid": rebuilt_stats["valid"], "rebuilt_total": rebuilt_stats["total"]})
    if complete:
        return rebuilt, stats

    try:
        rebuilt = generate(refresh=False, cache_only=True, preview=False)
    except Exception:
        rebuilt = {}
    complete, cache_stats = check_watchlist_completeness(rebuilt)
    stats.update({"cache_complete": complete, "cache_valid": cache_stats["valid"], "cache_total": cache_stats["total"]})
    return rebuilt, stats


def fetch_universe() -> Dict[str, str]:
    loaders = [
        lambda: ak.stock_zh_a_spot_em(),
        lambda: ak.stock_info_a_code_name(),
        lambda: ak.stock_info_sh_name_code(),
        lambda: ak.stock_info_sz_name_code(),
    ]
    for loader in loaders:
        try:
            df = loader()
            universe = extract_code_name_df(df)
            if universe:
                return universe
        except Exception:
            continue
    return {}


def fetch_board_membership(loader_name: str, board_symbol: str) -> Dict[str, str]:
    try:
        if loader_name == "industry":
            df = ak.stock_board_industry_cons_em(symbol=board_symbol)
        else:
            df = ak.stock_board_concept_cons_em(symbol=board_symbol)
    except Exception:
        return {}
    return extract_member_codes(df)


def fetch_board_names(loader_name: str) -> List[Tuple[str, str]]:
    loaders = []
    if loader_name == "industry":
        loaders = [ak.stock_board_industry_name_ths, ak.stock_board_industry_name_em]
    else:
        loaders = [ak.stock_board_concept_name_ths, ak.stock_board_concept_name_em]
    for loader in loaders:
        try:
            df = loader()
        except Exception:
            continue
        if df is None or df.empty:
            continue
        name_col = ""
        code_col = ""
        for col in df.columns:
            text = str(col)
            low = text.lower()
            if not name_col and any(k in low for k in ("name", "名称", "板块名称", "概念名称", "行业名称", "板块", "概念")):
                name_col = col
            if not code_col and any(k in low for k in ("code", "代码", "板块代码")):
                code_col = col
        if not name_col and len(df.columns) >= 2:
            name_col = df.columns[0]
        if not code_col and len(df.columns) >= 2:
            code_col = df.columns[1]
        if not name_col or not code_col:
            continue
        out = []
        for _, row in df.iterrows():
            symbol = str(row.get(code_col, "")).strip()
            name = str(row.get(name_col, "")).strip()
            if symbol and name:
                out.append((symbol, name))
        if out:
            return out
    return []


def fetch_stock_detail(code: str) -> dict:
    info: Dict[str, str] = {}
    try:
        profile = ak.stock_profile_cninfo(symbol=code)
        if profile is not None and not profile.empty:
            row = profile.iloc[0]
            for key in ("A股简称", "所属行业", "A股代码"):
                if key in profile.columns:
                    info[key] = str(row.get(key, "")).strip()
            if "主营业务" in profile.columns:
                info["主营业务"] = str(row.get("主营业务", "")).strip()
            if "公司简介" in profile.columns:
                info["公司简介"] = str(row.get("公司简介", "")).strip()
    except Exception:
        pass

    try:
        df = ak.stock_individual_info_em(symbol=code)
    except Exception:
        return info
    if df is None or df.empty:
        return info
    if {"item", "value"}.issubset(df.columns):
        for key, value in zip(df["item"].astype(str), df["value"].astype(str)):
            info[key.strip()] = value.strip()
    elif len(df.columns) >= 2:
        for key, value in zip(df.iloc[:, 0].astype(str), df.iloc[:, 1].astype(str)):
            info[key.strip()] = value.strip()
    return info


def sanitize_sector(value: str) -> str:
    value = str(value or "").strip()
    if not value or value == "-":
        return ""
    if value in {"未知板块", "全市场", "全部市场", "A股全市场"}:
        return ""
    return value


def pick_primary_sector(existing: dict, industry_boards: List[str], concept_boards: List[str], detail: dict) -> Tuple[str, str]:
    for key in ("所属概念板块", "概念板块", "概念", "板块"):
        candidate = sanitize_sector(detail.get(key, ""))
        if candidate:
            return candidate, "detail"

    if concept_boards:
        return concept_boards[0], "concept"

    old_sector = sanitize_sector(existing.get("sector", "")) if existing else ""
    if old_sector and not concept_boards:
        return old_sector, "legacy"

    industry = sanitize_sector(detail.get("所属行业", ""))
    if industry:
        return industry, "profile"

    return "其他", "fallback"


def build_board_cache(universe: Dict[str, str]) -> dict:
    industry_boards = fetch_board_names("industry")
    concept_boards = fetch_board_names("concept")

    industry_members: Dict[str, List[str]] = defaultdict(list)
    concept_members: Dict[str, List[str]] = defaultdict(list)

    for board_symbol, board_name in industry_boards:
        members = fetch_board_membership("industry", board_symbol)
        for code, name in members.items():
            if code in universe:
                industry_members[code].append(board_name)

    for board_symbol, board_name in concept_boards:
        members = fetch_board_membership("concept", board_symbol)
        for code, name in members.items():
            if code in universe:
                concept_members[code].append(board_name)

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_version": "akshare-eastmoney",
        "universe_count": len(universe),
        "boards": {
            "industry": industry_boards,
            "concept": concept_boards,
        },
        "members": {
            "industry": dict(sorted((code, sorted(set(boards))) for code, boards in industry_members.items())),
            "concept": dict(sorted((code, sorted(set(boards))) for code, boards in concept_members.items())),
        },
    }


def build_watchlist(refresh: bool = True, cache_only: bool = False) -> Tuple[Dict[str, dict], dict]:
    existing = load_existing_watchlist()

    if cache_only:
        cache = read_json(BOARD_CACHE_FILE, {})
        if not cache:
            raise RuntimeError(f"缓存不存在或为空: {BOARD_CACHE_FILE}")
        rebuilt = rebuild_from_cache(existing, cache)
        return rebuilt, cache

    universe = fetch_universe()
    if not universe:
        if os.path.exists(BOARD_CACHE_FILE):
            cache = read_json(BOARD_CACHE_FILE, {})
            if cache:
                return rebuild_from_cache(existing, cache), cache
        raise RuntimeError("无法获取 A 股基础股票池")

    cache = build_board_cache(universe)
    cache["universe_names"] = universe
    write_json_atomic(BOARD_CACHE_FILE, cache)
    rebuilt = rebuild_from_cache(existing, cache)
    write_json_atomic(UNIVERSE_CACHE_FILE, rebuilt)
    return rebuilt, cache


def build_legacy_cache(existing: Dict[str, dict]) -> dict:
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_version": "legacy-existing",
        "universe_count": len(existing),
        "boards": {"industry": [], "concept": []},
        "members": {"industry": {}, "concept": {}},
        "universe_names": {code: info.get("name", code) for code, info in existing.items()},
    }


def build_ths_concept_cache() -> dict:
    concepts = extract_ths_concepts()
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_version": "ths-concept-name-info",
        "concept_count": len(concepts),
        "concepts": dict(sorted(concepts.items(), key=lambda kv: kv[1].get("name", kv[0]))),
    }


def build_business_summary(sector: str, industry_boards: List[str], concept_boards: List[str]) -> str:
    sector = str(sector or "").strip()
    industry = industry_boards[0] if industry_boards else ""
    concept = concept_boards[0] if concept_boards else ""
    parts = []
    if sector:
        parts.append(sector)
    if industry and industry not in parts:
        parts.append(industry)
    if concept and concept not in parts:
        parts.append(concept)
    if not parts:
        return ""
    return "主营" + "，".join(parts[:3])


def rebuild_from_cache(existing: Dict[str, dict], cache: dict, fetch_details: bool = False) -> Dict[str, dict]:
    universe_names = {}
    if isinstance(cache, dict):
        for code, name in cache.get("universe_names", {}).items():
            universe_names[normalize_code(code)] = str(name).strip()

    industry_members = cache.get("members", {}).get("industry", {}) if isinstance(cache, dict) else {}
    concept_members = cache.get("members", {}).get("concept", {}) if isinstance(cache, dict) else {}

    all_codes = set(existing.keys()) | set(universe_names.keys()) | set(industry_members.keys()) | set(concept_members.keys())
    if not all_codes:
        all_codes = set(existing.keys())

    rebuilt: Dict[str, dict] = {}
    for code in sorted(all_codes):
        base = copy.deepcopy(existing.get(code, {})) if code in existing else {}
        name = str(base.get("name", "")).strip() or universe_names.get(code, "") or code

        industry_boards = list(industry_members.get(code, []) or [])
        concept_boards = list(concept_members.get(code, []) or [])
        sector, source = pick_primary_sector(base, industry_boards, concept_boards, {})

        business_summary = build_business_summary(sector, industry_boards, concept_boards)

        rebuilt[code] = {
            "name": name,
            "sector": sector,
            "sector_type": source,
            "primary_source": source,
            "industry_boards": industry_boards,
            "concept_boards": concept_boards,
            "business_summary": business_summary,
            "updated_at": cache.get("generated_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        }

        for key in ("cost", "qty", "base", "t_qty", "type"):
            if key in base:
                rebuilt[code][key] = base[key]

        for key, value in base.items():
            if key not in rebuilt[code]:
                rebuilt[code][key] = value

    return rebuilt


def build_universe_names_from_cache(cache: dict, existing: Dict[str, dict]) -> dict:
    names = {}
    if isinstance(cache, dict):
        for code, info in existing.items():
            names[code] = info.get("name", code)
    return names


def generate(refresh: bool = True, cache_only: bool = False, preview: bool = False) -> Dict[str, dict]:
    existing = load_existing_watchlist()
    legacy_cache = build_legacy_cache(existing) if existing else {}

    if cache_only:
        cache = read_json(BOARD_CACHE_FILE, {})
        if not cache:
            if legacy_cache:
                cache = legacy_cache
            else:
                raise RuntimeError(f"缓存不存在或为空: {BOARD_CACHE_FILE}")
        rebuilt = rebuild_from_cache(existing, cache, fetch_details=False)
    else:
        universe = fetch_universe() if refresh else {}
        if not universe:
            if existing:
                cache = legacy_cache or read_json(BOARD_CACHE_FILE, {})
                if not cache:
                    raise RuntimeError("无法获取 A 股基础股票池，也没有可回退的缓存")
                if legacy_cache and not os.path.exists(BOARD_CACHE_FILE):
                    write_json_atomic(BOARD_CACHE_FILE, cache)
                rebuilt = rebuild_from_cache(existing, cache, fetch_details=False)
            else:
                cache = read_json(BOARD_CACHE_FILE, {})
                if not cache:
                    raise RuntimeError("无法获取 A 股基础股票池，也没有可回退的缓存")
                rebuilt = rebuild_from_cache(existing, cache, fetch_details=False)
        else:
            cache = build_board_cache(universe)
            cache["universe_names"] = universe
            write_json_atomic(BOARD_CACHE_FILE, cache)
            rebuilt = rebuild_from_cache(existing, cache)
            for code, name in universe.items():
                if code in rebuilt and not rebuilt[code].get("name"):
                    rebuilt[code]["name"] = name

    if preview:
        print(f"预览：将生成 {len(rebuilt)} 只股票")
        sample = list(sorted(rebuilt.items()))[:10]
        for code, info in sample:
            print(f"  {code} {info.get('name')} | {info.get('sector')} | {info.get('primary_source')}")
        return rebuilt

    write_json_atomic(WATCHLIST_FILE, rebuilt)
    return rebuilt


def main() -> None:
    parser = argparse.ArgumentParser(description="Build offline watchlist.json from A-share concept/industry boards")
    parser.add_argument("--cache-only", action="store_true", help="rebuild watchlist.json from local cache only")
    parser.add_argument("--preview", action="store_true", help="print summary without writing files")
    parser.add_argument("--no-refresh", action="store_true", help="skip online universe refresh")
    parser.add_argument("--ensure-complete", action="store_true", help="check completeness and auto-repair if needed")
    args = parser.parse_args()

    try:
        if args.ensure_complete:
            print("正在执行 watchlist 完整性校验...")
            data, stats = ensure_watchlist_complete(refresh=not args.no_refresh)
            if args.preview:
                print(f"预览：watchlist 完整性校验 {stats}")
                return
            print(f"✓ watchlist.json 已校验/补全: {len(data)} 只")
            print(f"✓ 完整性统计: {stats}")
        else:
            data = generate(refresh=not args.no_refresh, cache_only=args.cache_only, preview=args.preview)
            if not args.preview:
                print(f"✓ watchlist.json 已生成: {len(data)} 只")

        if not args.ensure_complete:
            try:
                concept_cache = build_ths_concept_cache()
                write_json_atomic(THS_CONCEPTS_FILE, concept_cache)
                if not args.preview:
                    print(f"✓ 同花顺概念缓存已写入: {THS_CONCEPTS_FILE}")
            except Exception as e:
                print(f"⚠️  同花顺概念缓存更新失败: {e}")
        elif not args.preview:
            print("✓ 已跳过同花顺概念缓存更新，避免阻塞启动")

        if not args.preview:
            print(f"✓ 缓存已写入: {BOARD_CACHE_FILE}")
    except Exception as e:
        print(f"✗ 生成失败: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
