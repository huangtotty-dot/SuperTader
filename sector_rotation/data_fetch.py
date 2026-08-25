# -*- coding: utf-8 -*-
"""板块轮动数据层。

维护一份全市场日线缓存（t_io/rotation/daily_cache/，每交易日一个 CSV），
供韭研概念轮动（Part 1）与全市场行业轮动（Part 2）共用。

行情源：腾讯（qt.gtimg.cn 批量快照 + ifzq.gtimg.cn 历史日线 qfq）。
东财 akshare 接口在本机被 SSL 拦截，故此处直接用腾讯通道（与 stock_hunter/market_data.py 同源）。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

# 去掉系统代理（与 superTrader config/main 一致，避免东财类 SSL 拦截）
for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
_urllib_request = urllib.request
_urllib_request.install_opener(_urllib_request.build_opener(_urllib_request.ProxyHandler({})))

BASE = Path(__file__).resolve().parents[1]
ROT_DIR = BASE / "t_io" / "rotation"
DAILY_CACHE = ROT_DIR / "daily_cache"
INDUSTRY_MAP = ROT_DIR / "industry_map.csv"
JIUYAN_WATCHLIST = BASE / "watchlist_jiuyan.json"
LEGACY_DB = Path(r"E:\sector-rotation-v2\sample_data.db")

BOOTSTRAP_CALENDAR_DAYS = 75      # 拉取历史日历天数（约 50 个交易日，足够 20 日 RS + 10 日成交额基准）
MIN_TRADING_DAYS = 25             # 轮动引擎最低可用交易日数
SNAPSHOT_BATCH = 200
WORKERS = 20

ROTATION_PROGRESS: dict[str, Any] = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}


# ---------- 腾讯基础工具 ----------

def _qt_symbol(code: str) -> str:
    code = str(code).strip()
    market = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{market}{code}"


def _is_a_share_code(code: str) -> bool:
    code = str(code).strip()
    return len(code) == 6 and code.isdigit() and code.startswith(
        ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688", "689")
    )


def _http_get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Referer": "https://finance.qq.com/"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


def _snapshot_map_to_rows(snapshot_map: dict[str, dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for code, data in snapshot_map.items():
        rows.append(
            {
                "代码": code,
                "名称": data.get("名称", ""),
                "收盘": data.get("现价", 0.0),
                "涨跌幅": data.get("涨跌幅", 0.0),
                "成交额": data.get("成交额", 0.0),
            }
        )
    return pd.DataFrame(rows)


def fetch_snapshot(codes: list[str]) -> pd.DataFrame:
    """腾讯实时快照（qt.gtimg.cn），批量 200/请求，返回 代码/名称/收盘/涨跌幅/成交额。"""
    symbols = [_qt_symbol(c) for c in codes if _is_a_share_code(c)]
    snapshot_map: dict[str, dict[str, Any]] = {}
    for i in range(0, len(symbols), SNAPSHOT_BATCH):
        chunk = symbols[i : i + SNAPSHOT_BATCH]
        url = f"https://qt.gtimg.cn/q={','.join(chunk)}"
        try:
            text = _http_get(url, timeout=20)
        except Exception:
            continue
        for line in text.splitlines():
            parsed = _parse_qt_line(line)
            if parsed:
                code, data = parsed
                if data.get("成交额", 0) <= 0 and data.get("现价", 0) <= 0:
                    continue
                snapshot_map[code] = data
    return _snapshot_map_to_rows(snapshot_map)


def _parse_qt_line(line: str):
    line = str(line or "").strip()
    if not line or '="' not in line:
        return None
    payload = line.split("=", 1)[1].strip().strip(";").strip('"')
    fields = payload.split("~")
    if len(fields) < 40:
        return None
    code = str(fields[2]).strip()
    if not code or not code.isdigit():
        return None
    try:
        price = float(fields[3] or 0)
    except Exception:
        price = 0.0
    amount = 0.0
    for field in fields:
        parts = str(field).strip().split("/")
        if len(parts) == 3:
            try:
                amount = float(parts[2] or 0)
                break
            except Exception:
                continue
    if amount <= 0:
        try:
            amount = float(fields[7] or 0)
        except Exception:
            amount = 0.0
    # 腾讯快照字段索引：31=涨跌额，32=涨跌幅（stock_hunter 旧代码误用 31）
    change_pct = 0.0
    try:
        change_pct = float(fields[32] or 0)
    except Exception:
        change_pct = 0.0
    if change_pct == 0.0 and price > 0:
        try:
            prev_close = float(fields[4] or 0)
            if prev_close > 0:
                change_pct = round((price - prev_close) / prev_close * 100, 4)
        except Exception:
            pass
    return code, {"名称": str(fields[1]).strip(), "现价": price, "涨跌幅": change_pct, "成交额": amount}


def fetch_stock_history(code: str, days: int = BOOTSTRAP_CALENDAR_DAYS) -> pd.DataFrame:
    """腾讯历史日线（ifzq.gtimg.cn fqkline qfq），返回 日期/收盘/涨跌幅/成交额（按日期升序）。"""
    symbol = _qt_symbol(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
    try:
        data = json.loads(_http_get(url))
    except Exception:
        return pd.DataFrame()
    if data.get("code") != 0:
        return pd.DataFrame()
    stock_data = (data.get("data") or {}).get(symbol) or {}
    kline = stock_data.get("day") or stock_data.get("qfqday")
    if not kline:
        return pd.DataFrame()

    records = []
    is_kcb = code.startswith("688")
    volume_unit = 1.0 if is_kcb else 100.0
    prev_close = None
    for item in kline:
        try:
            if not isinstance(item, list) or len(item) < 6:
                continue
            date = str(item[0])
            close = float(item[2])
            high = float(item[3])
            low = float(item[4])
            volume = float(item[5])
            if len(item) >= 7:
                amount = float(item[6] or 0)
            else:
                amount = ((high + low + close) / 3.0) * volume * volume_unit
            if amount <= 0:
                amount = close * volume * volume_unit
            chg = 0.0
            if prev_close and prev_close > 0:
                chg = round((close - prev_close) / prev_close * 100, 4)
            prev_close = close
            records.append({"日期": date, "收盘": close, "涨跌幅": chg, "成交额": amount, "代码": code})
        except (ValueError, IndexError, TypeError):
            continue
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records).drop_duplicates("日期").sort_values("日期").reset_index(drop=True)
    return df


# ---------- 全市场代码池 ----------

def load_industry_map() -> pd.DataFrame:
    """行业映射 代码/名称/行业名称。优先读本地 industry_map.csv，缺失则从 sample_data.db 导出。"""
    if INDUSTRY_MAP.exists():
        df = pd.read_csv(INDUSTRY_MAP, dtype={"代码": str}, encoding="utf-8-sig")
        df["代码"] = df["代码"].str.zfill(6)
        return df
    if LEGACY_DB.exists():
        import sqlite3

        with sqlite3.connect(str(LEGACY_DB)) as conn:
            df = pd.read_sql("SELECT * FROM stock_industry", conn)
        if "名称" not in df.columns:
            df["名称"] = df.get("股票名称", df["代码"])
        df["代码"] = df["代码"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        ROT_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(INDUSTRY_MAP, index=False, encoding="utf-8-sig")
        return df
    raise FileNotFoundError(f"行业映射缺失：{INDUSTRY_MAP} 与 {LEGACY_DB} 均不存在")


def load_jiuyan_map() -> pd.DataFrame:
    """韭研映射 代码/名称/行业名称=韭研分类（Part 1）。每只股票归属其第一个非空韭研分类。"""
    if not JIUYAN_WATCHLIST.exists():
        raise FileNotFoundError(f"韭研 watchlist 缺失：{JIUYAN_WATCHLIST}")
    with open(JIUYAN_WATCHLIST, encoding="utf-8") as f:
        raw = json.load(f)
    records = []
    for code, info in raw.items():
        if not isinstance(info, dict):
            continue
        category = ""
        for i in range(1, 10):
            cat = info.get(f"jiuyan_category{i}")
            if cat and str(cat).strip():
                category = str(cat).split("|")[0].strip()
                break
        if not category:
            cat = info.get("jiuyan_category")
            if cat and str(cat).strip():
                category = str(cat).split("|")[0].strip()
        if not category:
            continue
        records.append({"代码": str(code).zfill(6), "名称": str(info.get("name", "")).strip(), "行业名称": category})
    df = pd.DataFrame(records).drop_duplicates("代码")
    return df


def full_market_codes() -> list[str]:
    codes: set[str] = set()
    try:
        codes |= set(load_industry_map()["代码"])
    except Exception:
        pass
    try:
        codes |= set(load_jiuyan_map()["代码"])
    except Exception:
        pass
    return sorted(c for c in codes if _is_a_share_code(c))


# ---------- 日线缓存 ----------

def _cache_date_key(date_str: str) -> str:
    return date_str.replace("-", "")


def write_daily_date(date_str: str, df: pd.DataFrame) -> None:
    if df is None or df.empty:
        return
    DAILY_CACHE.mkdir(parents=True, exist_ok=True)
    keep = df[["代码", "收盘", "涨跌幅", "成交额"]].copy()
    keep["日期"] = date_str
    path = DAILY_CACHE / f"{_cache_date_key(date_str)}.csv"
    keep.to_csv(path, index=False, encoding="utf-8-sig")


def cached_date_files() -> list[Path]:
    if not DAILY_CACHE.exists():
        return []
    return sorted(DAILY_CACHE.glob("*.csv"))


def available_dates() -> list[str]:
    return [f.stem[:4] + "-" + f.stem[4:6] + "-" + f.stem[6:8] for f in cached_date_files()]


def is_trading_day(date_str: str, cached: list[str] | None = None) -> bool:
    if cached is None:
        cached = available_dates()
    return date_str in cached


def load_stock_daily() -> pd.DataFrame:
    files = cached_date_files()
    if not files:
        return pd.DataFrame()
    frames = []
    for path in files:
        df = pd.read_csv(path, dtype={"代码": str}, encoding="utf-8-sig")
        df["代码"] = df["代码"].str.zfill(6)
        frames.append(df)
    daily = pd.concat(frames, ignore_index=True)
    for col in ["收盘", "涨跌幅", "成交额"]:
        daily[col] = pd.to_numeric(daily[col], errors="coerce")
    return daily.dropna(subset=["日期"])


def cache_readiness() -> tuple[bool, str]:
    dates = available_dates()
    if not dates:
        return False, "日线缓存为空，需要首次全量构建（约几分钟）。"
    if len(dates) < MIN_TRADING_DAYS:
        return False, f"日线缓存仅有 {len(dates)} 个交易日（需 ≥{MIN_TRADING_DAYS}）。"
    return True, ""


def bootstrap_daily_cache(codes: list[str] | None = None, max_days: int = BOOTSTRAP_CALENDAR_DAYS) -> None:
    """全量构建日线缓存：逐股拉腾讯历史日线，按日期落盘。前台执行（调用方负责后台线程）。"""
    codes = codes or full_market_codes()
    DAILY_CACHE.mkdir(parents=True, exist_ok=True)
    ROTATION_PROGRESS.update({"running": True, "phase": "拉取历史日线", "done": 0, "total": len(codes), "msg": ""})
    per_date: dict[str, list[dict[str, Any]]] = {}
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(fetch_stock_history, code, max_days): code for code in codes}
            done = 0
            for future in as_completed(futures):
                code = futures[future]
                try:
                    df = future.result()
                except Exception:
                    df = pd.DataFrame()
                if not df.empty:
                    for _, row in df.iterrows():
                        per_date.setdefault(str(row["日期"]), []).append(
                            {"代码": code, "收盘": row["收盘"], "涨跌幅": row["涨跌幅"], "成交额": row["成交额"]}
                        )
                done += 1
                ROTATION_PROGRESS.update({"done": done})
    finally:
        ROTATION_PROGRESS.update({"running": False, "phase": "落盘", "done": 0, "total": 0, "msg": ""})

    # 个别股票无视 count 参数返回超长历史 → 只保留最近 max_days 个自然日的日期
    if per_date:
        date_list = sorted(per_date.keys())
        cutoff = pd.Timestamp(date_list[-1]) - pd.Timedelta(days=max_days)
        per_date = {d: rows for d, rows in per_date.items() if pd.Timestamp(d) >= cutoff}

    for date_str, rows in sorted(per_date.items()):
        write_daily_date(date_str, pd.DataFrame(rows))
    ROTATION_PROGRESS.update({"running": False, "phase": "", "done": 0, "total": 0, "msg": ""})


def update_today_if_needed() -> str:
    """用腾讯快照补最新交易日（工作日收盘后且尚未缓存时）。返回补录日期或空串。"""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5:              # 周末不补
        return ""
    if now.hour * 100 + now.minute < 1500:   # 盘中不补（避免半日数据当收盘）
        return ""
    if is_trading_day(today):
        return ""
    codes = full_market_codes()
    ROTATION_PROGRESS.update({"running": True, "phase": "拉取当日快照", "done": 0, "total": len(codes), "msg": ""})
    try:
        snapshot = fetch_snapshot(codes)
    finally:
        ROTATION_PROGRESS.update({"running": False, "phase": "", "done": 0, "total": 0, "msg": ""})
    if snapshot.empty:
        return ""
    write_daily_date(today, snapshot)
    return today


def ensure_cache_ready(background: bool = True) -> dict[str, Any]:
    """确保日线缓存就绪。返回 {'ok': bool, 'message': str, 'progress': {...}}。"""
    ready, msg = cache_readiness()
    if ready:
        update_today_if_needed()
        return {"ok": True, "message": msg, "progress": dict(ROTATION_PROGRESS)}
    return {"ok": False, "message": msg, "progress": dict(ROTATION_PROGRESS)}


# ---------- 对外主接口 ----------

def load_rotation_inputs(view: str, as_of: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """按视角取 (stock_daily, stock_industry, available_dates)。"""
    daily = load_stock_daily()
    if view == "jiuyan":
        industry = load_jiuyan_map()
    else:
        industry = load_industry_map()
    if as_of:
        dates = available_dates()
        if as_of not in dates:
            as_of = None
    dates = available_dates()
    return daily, industry, dates
