# -*- coding: utf-8 -*-
"""图表数据缓存层（2026-10-04）——「盘后预下载 + 盘中增量读」的单一真源。

背景：K线弹窗打开慢，根因是**取数**——普通个股日线走 `facade.daily()` 的 GM 优先路径
（GM 不可达时每次卡满 12s 硬超时）；磁盘日线缓存又只在腾讯兜底路径才被读，且其新鲜度硬闸
是 `cached["date"] == 今天`（`tencent_provider.daily_cache`）⇒ **盘后落的缓存次日必然不命中**。

本模块提供**交易日感知**的缓存读法与盘后预下载编排，供 `t_gui.load_stock_chart` 与
调度任务共用。**只做加法**：不改 `facade.daily` 的 GM-first 语义、不碰交易扫描链路。

层：
  · 日线历史   t_io/cache/daily_kline/{code}.json         （复用既有格式，merge 写）
  · 图表 payload t_io/cache/chart_payload/{code}.json      （新增，小池「瞬开」）
  · 分钟线     t_io/cache/tushare_mins/{ts}_{freq}.json    （复用既有目录）

新鲜度：无全局交易日历，用「**末行数据日期**距今自然日数」近似（不看写入日，故可跨日/周末/假期命中）。
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

_DAILY_CACHE_DIR = _BASE / "t_io" / "cache" / "daily_kline"
_PAYLOAD_DIR = _BASE / "t_io" / "cache" / "chart_payload"
_MINUTE_CACHE_DIR = _BASE / "t_io" / "cache" / "tushare_mins"
_STATE_DIR = _BASE / "t_io" / "state"
_HUNTER_DIR = _BASE / "stock_hunter"

_STATE_FP = _STATE_DIR / "chart_prefetch_state.json"
_LOCK_FP = _STATE_DIR / "chart_prefetch.lock"

_DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]

# 展示可用上限：覆盖周末(2) + 最长法定长假(≤9) + 缓冲。超出即视为过期，让调用方走网络。
MAX_DISPLAY_GAP_DAYS = 12
# 预下载幂等的「已够新」阈值：≥ 一个周末的最长间隔(Fri→Mon=3) + 缓冲。
PREFETCH_CURRENT_GAP_DAYS = 5


def _read_json(fp, default=None):
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# 新鲜度（交易日近似）
# ---------------------------------------------------------------------------
def last_row_date(rows) -> str:
    """末行数据日期字符串（'YYYY-MM-DD'）；无则 ''。"""
    if not rows:
        return ""
    try:
        return str(rows[-1].get("date") or rows[-1].get("time") or "")[:10]
    except Exception:
        return ""


def natural_gap_days(last_date, now=None) -> int:
    """末行日期距今的**自然日**数；解析失败返回 9999（视为过期）。"""
    try:
        d = datetime.strptime(str(last_date)[:10], "%Y-%m-%d").date()
    except Exception:
        return 9999
    today = (now or datetime.now()).date()
    return (today - d).days


def is_display_fresh(rows, now=None) -> bool:
    """展示可用：末行日期在 MAX_DISPLAY_GAP_DAYS 自然日内。"""
    return natural_gap_days(last_row_date(rows), now) <= MAX_DISPLAY_GAP_DAYS


def is_prefetch_current(rows, now=None) -> bool:
    """预下载「已够新」：末行日期在 PREFETCH_CURRENT_GAP_DAYS 自然日内 ⇒ 可跳过重下。"""
    return natural_gap_days(last_row_date(rows), now) <= PREFETCH_CURRENT_GAP_DAYS


# ---------------------------------------------------------------------------
# 日线历史缓存
# ---------------------------------------------------------------------------
def daily_fp(code):
    """日线缓存文件路径。指数码（sh/sz/bj + 6 位数字）落在 `index_{code}.json`（provider 惯例），
    个股落 `{code}.json`。2026-10-06：此前只认个股名，指数读不到缓存。"""
    c = str(code).split("_")[0]
    if c[:2].lower() in ("sh", "sz", "bj") and c[2:].isdigit():
        return _DAILY_CACHE_DIR / f"index_{c.lower()}.json"
    return _DAILY_CACHE_DIR / f"{c}.json"


def load_daily_display(code, now=None):
    """读日线缓存（指数走 `index_{code}.json`），按**交易日近似**新鲜度；命中返回 DataFrame，否则 None。

    与 `TencentProvider.daily_cache` 的关键差别：只看末行数据日期，**不看写入日 `date`**
    —— 后者要求 `date==今天`，会让盘后预下载的缓存在次日直接失效。
    """
    import pandas as pd
    fp = daily_fp(code)
    if not fp.exists():
        return None
    cached = _read_json(fp, None)
    rows = (cached or {}).get("rows") or []
    if not rows or not is_display_fresh(rows, now):
        return None
    try:
        df = pd.DataFrame(rows)[_DAILY_COLS]
        df.attrs["source"] = "cache"
        return df
    except Exception:
        return None


def write_daily_history(code, df) -> bool:
    """把日线 df **并入**共享缓存（merge 语义，保留长历史、不截短）。

    复用 `tencent_provider.merge_daily_cache`（sanctioned 的合并写端）——**禁止**用
    `save_daily_cache`（整体覆盖会把 800 行历史截短，见 memory「指数日线缓存会被静默截短」）。
    """
    if df is None or getattr(df, "empty", True):
        return False
    try:
        from core.market_data.tencent_provider import merge_daily_cache
        merge_daily_cache(str(code).split("_")[0], df)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 图表 payload 缓存（小池「瞬开」）
# ---------------------------------------------------------------------------
def load_payload(code, now=None):
    """读 `chart_payload/{code}.json`。命中且展示新鲜 → 返回 `{payload, daily_rows, last_daily_date, version}`；
    否则 None。"""
    code = str(code).split("_")[0]
    fp = _PAYLOAD_DIR / f"{code}.json"
    if not fp.exists():
        return None
    hit = _read_json(fp, None)
    if not isinstance(hit, dict) or not hit.get("payload"):
        return None
    if not payload_display_fresh(hit, now):
        return None
    return hit


def payload_display_fresh(hit, now=None) -> bool:
    """payload 是否在展示新鲜窗内（用冗余的 `last_daily_date` 快判）。"""
    if not isinstance(hit, dict):
        return False
    return natural_gap_days(hit.get("last_daily_date"), now) <= MAX_DISPLAY_GAP_DAYS


def save_payload(code, payload, daily_rows) -> bool:
    """原子写 `chart_payload/{code}.json`（temp + os.replace），失败静默。超上限则 LRU 淘汰。"""
    if not isinstance(payload, dict) or not payload.get("available"):
        return False
    code = str(code).split("_")[0]
    try:
        _PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": payload.get("version"),
            "last_daily_date": last_row_date(daily_rows),
            "payload": payload,
            "daily_rows": daily_rows or [],
        }
        tmp = _PAYLOAD_DIR / f".{code}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False))
        os.replace(tmp, _PAYLOAD_DIR / f"{code}.json")
        _PAYLOAD_WRITES["n"] += 1
        if _PAYLOAD_WRITES["n"] % 25 == 0:      # 每 25 次落盘做一次容量检查（避免每次 glob）
            enforce_payload_cap()
        return True
    except Exception:
        return False


_PAYLOAD_MAX = 800          # 容量上限：~800×300KB ≈ 240MB
_PAYLOAD_WRITES = {"n": 0}


def enforce_payload_cap(max_files=_PAYLOAD_MAX) -> int:
    """payload 目录超上限时按 mtime **LRU 淘汰**最旧的。返回删除数。失败静默。"""
    try:
        files = sorted(_PAYLOAD_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
        excess = len(files) - max_files
        removed = 0
        for p in files[:max(0, excess)]:
            try:
                p.unlink()
                removed += 1
            except Exception:
                pass
        return removed
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 分钟线缓存
# ---------------------------------------------------------------------------
_MINUTE_COLS = ["time", "open", "high", "low", "close", "volume"]


def fetch_minute_online(ts_code, freq, days) -> "object":
    """tushare 原生 30/60min（镜像 t_gui._fetch_min_bars_online 的口径，但置于 core 层
    供预下载复用；t_gui 的原函数保持不动）。异常/空 → 空 DataFrame。"""
    import pandas as pd
    try:
        from analysis.index_regime_intraday import _iri_tushare_pro
        pro = _iri_tushare_pro()
        start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        df = pro.stk_mins(ts_code=ts_code, freq=freq,
                          start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")
    except Exception:
        return pd.DataFrame()
    return _norm_minutes(df)


def _norm_minutes(df):
    """统一分时帧列名/类型/排序（与 t_gui._norm_min_bars 同契约）。"""
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume", "date": "time"})
    need = set(_MINUTE_COLS)
    if not need.issubset(df.columns):
        return pd.DataFrame()
    df = df[_MINUTE_COLS].copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").drop_duplicates(subset=["time"])
    return df.reset_index(drop=True)


def save_minute_history(ts_code, freq, df, cap=920) -> bool:
    """把分钟 df **并入** plain 档 `{ts_code}_{freq}.json`（按 time 去重、cap 截尾、原子写）。"""
    import pandas as pd
    if df is None or getattr(df, "empty", True) or not ts_code:
        return False
    try:
        _MINUTE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fp = _MINUTE_CACHE_DIR / f"{ts_code}_{freq}.json"
        old = _read_json(fp, {}) or {}
        old_rows = old.get("rows") or []
        by_time = {}
        for r in list(old_rows) + [
            {"time": str(t), "open": float(o), "high": float(h), "low": float(l),
             "close": float(c), "volume": float(v)}
            for t, o, h, l, c, v in zip(df["time"], df["open"], df["high"],
                                        df["low"], df["close"], df["volume"])
        ]:
            k = str(r.get("time") or "")
            if k:
                by_time[k] = r
        rows = [by_time[k] for k in sorted(by_time)]
        if cap and len(rows) > cap:
            rows = rows[-cap:]
        tmp = _MINUTE_CACHE_DIR / f".{ts_code}_{freq}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "rows": rows}, ensure_ascii=False))
        os.replace(tmp, fp)
        return True
    except Exception:
        return False


def load_minute_history(ts_code, freq, now=None, max_gap_days=MAX_DISPLAY_GAP_DAYS):
    """读 plain 档 `{ts_code}_{freq}.json`，末行时间在 max_gap_days 自然日内才命中。"""
    import pandas as pd
    if not ts_code:
        return None
    fp = _MINUTE_CACHE_DIR / f"{ts_code}_{freq}.json"
    if not fp.exists():
        return None
    cached = _read_json(fp, None)
    rows = (cached or {}).get("rows") or []
    if not rows or not is_display_fresh(rows, now):
        return None
    try:
        return _norm_minutes(pd.DataFrame(rows))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 池子清单（stdlib 直读 JSON，不 import t_gui / src.data_fetcher）
# ---------------------------------------------------------------------------
def _codes_from_holdings(path) -> list:
    d = _read_json(_BASE / path, {}) or {}
    out = []
    for code, info in d.items():
        if not isinstance(info, dict) or str(code).startswith("_"):
            continue
        if not (info.get("qty") or 0):
            continue
        out.append(str(code).split("_")[0])
    return out


def small_pool_codes() -> list:
    """小池 = 手动持仓 ∪ 自动持仓 ∪ 建仓池（剥后缀去重）。"""
    codes = []
    codes += _codes_from_holdings("t_io/state/holdings_manual.json")
    codes += _codes_from_holdings("t_io/state/holdings_auto.json")
    wb = _read_json(_BASE / "t_io" / "state" / "watchlist_buy.json", {}) or {}
    for code in (wb.get("stocks") or {}):
        codes.append(str(code).split("_")[0])
    return _dedup_valid(codes)


def full_pool_codes() -> list:
    """全池 = watchlist_jiuyan 全部 6 位码（对齐 t_gui._breakout_pool_codes）。"""
    jy = _read_json(_HUNTER_DIR / "watchlist_jiuyan.json", {}) or {}
    return _dedup_valid(list(jy.keys()))


def _dedup_valid(codes) -> list:
    seen, out = set(), []
    for c in codes:
        c = str(c).split("_")[0]
        if len(c) == 6 and c.isdigit() and c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# 盘后预下载编排
# ---------------------------------------------------------------------------
def prefetch_daily(codes, days=250, now=None, skip_current=True, batch=200,
                   sleep_s=0.3, retries=2, retry_pause=3.0) -> dict:
    """批量下载日线并 merge 入缓存。返回 {requested, got, skipped, failed}。不抛。

    **自己分批**调用 `daily_many`（而非一次喂全池）——`daily_many` 一遇 GM 异常就 `break`
    丢掉剩余；分批后单批失败不影响其余（实测全池一次调用遇 RemoteDisconnected 只拿到 900/5373）。
    某批失败/缺码的，隔 `retry_pause` 秒**整体重试 `retries` 轮**（GM 断连多为瞬时）。
    """
    from core.market_data import get_provider
    codes = _dedup_valid(codes)
    stat = {"requested": len(codes), "got": 0, "skipped": 0, "failed": 0}
    if not codes:
        return stat

    def _is_current(c):
        if not skip_current:
            return False
        cd = _read_json(_DAILY_CACHE_DIR / f"{c}.json", {}) or {}
        if is_prefetch_current(cd.get("rows") or [], now):
            return True
        # 今日已写过（merge_daily_cache 会把 date 头写成今天）⇒ 视为当期。
        # 假期里「末行=上个交易日、gap>5」会让 5 天窗失效，导致每次跑都重下全池，这条兜住。
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        return str(cd.get("date") or "") == today

    todo = [c for c in codes if not _is_current(c)]
    stat["skipped"] = len(codes) - len(todo)
    if not todo:
        return stat
    prov = get_provider()
    remaining = list(todo)
    for attempt in range(retries + 1):
        if not remaining:
            break
        still = []
        for i in range(0, len(remaining), batch):
            chunk = remaining[i:i + batch]
            try:
                got = prov.daily_many(chunk, days=days) or {}
            except Exception:
                got = {}
            for c, df in got.items():
                if write_daily_history(c, df):
                    stat["got"] += 1
                else:
                    still.append(c)
            still.extend([c for c in chunk if c not in got])
            time.sleep(sleep_s)      # 轻节流，降低 GM 断连概率
        remaining = still
        stat["failed"] = len(remaining)
        if remaining and attempt < retries:
            time.sleep(retry_pause)
    return stat


def prefetch_minute(codes, freqs=("30min", "60min"), sleep_s=0.35) -> dict:
    """逐只拉分钟线写 plain 档（tushare 单只、无法批量）。返回 {requested, got}。不抛。"""
    from analysis.divergence import _ts_code as _div_ts_code
    days_map = {"30min": 70, "60min": 130}
    codes = _dedup_valid(codes)
    stat = {"requested": len(codes) * len(freqs), "got": 0}
    for c in codes:
        ts_code = _div_ts_code(c)
        if not ts_code:
            continue
        for freq in freqs:
            try:
                df = fetch_minute_online(ts_code, freq, days_map.get(freq, 70))
                if save_minute_history(ts_code, freq, df):
                    stat["got"] += 1
            except Exception:
                pass
            time.sleep(sleep_s)   # 防 tushare 限流
    return stat


# ---- 跨进程锁 + 当日幂等 ----
def _acquire_lock(ttl=1800) -> bool:
    """O_CREAT|O_EXCL 原子建锁；已存在且 mtime 超 ttl 视为残留、抢占。失败 fail-open（返回 True）。"""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_LOCK_FP), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(_LOCK_FP) > ttl:
                os.remove(_LOCK_FP)
                return _acquire_lock(ttl)
        except Exception:
            pass
        return False
    except Exception:
        return True   # fail-open


def _release_lock() -> None:
    try:
        os.remove(_LOCK_FP)
    except Exception:
        pass


def was_run_today(kind, now=None) -> bool:
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    st = _read_json(_STATE_FP, {}) or {}
    return bool((st.get(today) or {}).get(kind))


def mark_run_today(kind, now=None) -> None:
    today = (now or datetime.now()).strftime("%Y-%m-%d")
    st = _read_json(_STATE_FP, {}) or {}
    st.setdefault(today, {})[kind] = 1
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_FP.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
        os.replace(tmp, _STATE_FP)
    except Exception:
        pass


def run_prefetch(small=True, full=True, minutes=True, days_full=200,
                 payload_builder=None, force=False, now=None) -> dict:
    """盘后预下载编排：小池日线 → 全池日线 → 小池分钟 → 小池 payload。

    幂等：跨进程锁（防 main.py 子进程与 t_gui 守护线程同时跑）+ 当日状态标记。
    payload_builder: 小池 payload 构建回调（由 t_gui 注入，避免 core→t_gui 反向依赖）。
    全程不抛，返回各项统计。
    """
    now = now or datetime.now()
    if not force and now.weekday() >= 5:
        return {"skipped": "weekend"}
    if not _acquire_lock():
        return {"skipped": "locked"}
    out = {}
    try:
        small_codes = small_pool_codes()
        if small and small_codes and (force or not was_run_today("small")):
            out["daily_small"] = prefetch_daily(small_codes, days=800, now=now)
            mark_run_today("small", now)
        if full and (force or not was_run_today("full")):
            out["daily_full"] = prefetch_daily(full_pool_codes(), days=days_full, now=now)
            mark_run_today("full", now)
        if minutes and small_codes and (force or not was_run_today("minute")):
            out["minute_small"] = prefetch_minute(small_codes)
            mark_run_today("minute", now)
        if payload_builder and small_codes and (force or not was_run_today("payload")):
            try:
                out["payload_small"] = payload_builder(small_codes)
            except Exception as e:
                out["payload_small"] = {"error": str(e)[:120]}
            mark_run_today("payload", now)
        return out
    finally:
        _release_lock()
