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
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from core.paths import STATE_DIR as _STATE_DIR_STR

_DAILY_CACHE_DIR = _BASE / "t_io" / "cache" / "daily_kline"
_PAYLOAD_DIR = _BASE / "t_io" / "cache" / "chart_payload"
_MINUTE_CACHE_DIR = _BASE / "t_io" / "cache" / "tushare_mins"
_STATE_DIR = Path(_STATE_DIR_STR)
_HUNTER_DIR = _BASE / "stock_hunter"

_STATE_FP = _STATE_DIR / "chart_prefetch_state.json"
_LOCK_FP = _STATE_DIR / "chart_prefetch.lock"

_DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]

# 展示可用上限：覆盖周末(2) + 最长法定长假(≤9) + 缓冲。超出即视为过期，让调用方走网络。
MAX_DISPLAY_GAP_DAYS = 12
# 预下载幂等的「已够新」阈值：≥ 一个周末的最长间隔(Fri→Mon=3) + 缓冲。
PREFETCH_CURRENT_GAP_DAYS = 5


def _read_json(fp, default=None):
    from core.utils import read_json
    return read_json(fp, default)


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
    # now 允许传字符串（老调用方/测试），统一转 datetime
    _n = _as_dt(now) if not isinstance(now, datetime) else now
    today = (_n or datetime.now()).date()
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
    # 2026-10-21：档里有**不可能存在的隔夜跳空** ⇒ 含盘中半成品根（002929：10-20 显示"跌 0.65%"，
    # 实际涨停 51.04，因为那根是 10-20 上午的快照）。判 None 让调用方重新取数，
    # 取回的真数据会把坏根合并覆盖掉（`merge_daily_cache` 按日期以新为准）。
    try:
        from core.market_data.tencent_provider import has_impossible_gap
        if has_impossible_gap(rows, code):
            return None
    except Exception:
        pass
    # 2026-10-26（603200 事故）：**当日那根如果是收盘前写入、又没标 forming** ⇒ 它是盘中快照，
    # 不是收盘。上面的隔夜跳空闸抓不到它（当日根后面还没有下一根可比），旧的新鲜度闸只看
    # 日期差（≤12 自然日）也放行 ⇒ 半成品一路当收盘用。判 None 逼调用方重取。
    # 判据与 `tencent.daily` 兜底共用同一个函数，避免两处漂移。
    try:
        from core.market_data.tencent_provider import is_unconfirmed_today_bar
        _n = _as_dt(now) if not isinstance(now, datetime) else now
        if is_unconfirmed_today_bar(rows, cached, now=_n):
            return None
    except Exception:
        pass
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
    # 2026-10-26（owner 报「上海洗霸已站上 5/10 日线却显示破位、整行是黄的」）：
    # 这里原来**不带 forming_date** 就把 df 并进缓存，而 `daily_many` 在盘中会把
    # **当日快照根**一起返回 ⇒ merge 既把快照值写成收盘、又顺手把 forming 标记清空
    # （`forming = {forming_date} if forming_date else set()`）⇒ 那根半成品被当成真收盘永久留档。
    # 实测（2026-10-26 09:3x 写入）：603200 存成 close=46.44（当天实为涨停），
    # MA5=47.57 ⇒ 技术标签报「破5日线」、猎手整行按未站上染黄。全池同批 **61 只**中招。
    # 判据：**末行就是今天** ⇒ 它只能来自快照（历史日不会变），必须标 forming；
    # 后续真数据确认时会覆盖并清标记（merge 的 `old_forming ∩ incoming` 分支）。
    _forming = None
    try:
        _last = str(df["date"].iloc[-1])[:10]
        if _last == datetime.now().strftime("%Y-%m-%d"):
            _forming = _last
    except Exception:
        _forming = None
    try:
        from core.market_data.tencent_provider import merge_daily_cache
        merge_daily_cache(str(code).split("_")[0], df, forming_date=_forming)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 图表 payload 缓存（小池「瞬开」）
# ---------------------------------------------------------------------------
def _as_dt(x):
    """now 参数可能是 datetime / 字符串 / None（老调用方）——统一成 datetime。"""
    if isinstance(x, datetime):
        return x
    if isinstance(x, str) and x:
        try:
            return datetime.strptime(x[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                return datetime.strptime(x[:10], "%Y-%m-%d")
            except Exception:
                return None
    return None


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
    # 2026-10-25：**盘中半成品根**闸（owner 报「五洲新春 10-23 那根日线不对」）。
    # 现场：payload 于 10-23 11:37 写入，最后一行是当时的快照（收 43.09/高 45.29），
    # 当天真实收盘 48.39（+8.35%）。`payload_display_fresh` 只看日期新不新鲜 ⇒ 周末照旧命中。
    # 判据两条，任一成立即判旧、让它重建：
    #   ① payload 标记为 provisional（写入时当天还没收盘）且现在已经过了收盘；或
    #   ② 最后一行日线与**权威日线缓存**同一天但收盘价差 >0.3%。
    try:
        _rows = hit.get("daily_rows") or []
        _nd = _as_dt(now) or datetime.now()
        if hit.get("provisional"):
            _saved = str(hit.get("saved_at") or "")
            _day = str(hit.get("last_daily_date") or "")[:10]
            if _day and _saved[:10] == _day and _nd.strftime("%Y-%m-%d") >= _day                     and _nd.strftime("%H:%M") >= "15:10":
                return None
        if _rows:
            _last = _rows[-1]
            _d = str(_last.get("date"))[:10]
            _fp = daily_fp(code)
            if _fp.exists():
                _c = _read_json(_fp, None) or {}
                _crows = _c.get("rows") or []
                # ③ 权威缓存已经比 payload 更新（payload 少一天）⇒ 也判旧，别让图停在上一交易日
                if _crows and str(_crows[-1].get("date"))[:10] > _d:
                    return None
                for _r in reversed(_crows):
                    if str(_r.get("date"))[:10] == _d:
                        try:
                            _a = float(_last.get("close")); _b = float(_r.get("close"))
                            if _b > 0 and abs(_a / _b - 1) > 0.003:
                                return None
                        except Exception:
                            pass
                        break
    except Exception:
        pass
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
            # 2026-10-17：payload 结构版本。
            #   1（隐式，无此键）= 10min 合成路径有 bug（带 date 列 ⇒ 永远空）
            #   2 = 含可用的 min10
            # `t_gui._serve_payload_cache` 要求 want_minutes 时 schema≥2，旧档自动重建一次。
            # 3 = 分钟档成交量已按科创板(688)归一（腾讯对 688 返"股"、其余"手"）；
            # schema<3 的旧档里 688 的分钟量能大 100 倍，必须重建一次。
            # 4 = min5/min10 分钟档已加深到近 3 个月（约 3200/1600 根，见 t_gui.MIN_BARS_CHART）；
            # schema<4 的旧档 5/10 分只有 320 根 ⇒ 切到这两个 Tab 时按需重建一次。
            "schema": 4,
            "version": payload.get("version"),
            "last_daily_date": last_row_date(daily_rows),
            # 2026-10-25：当天**未收盘**时落的 payload 记为 provisional，
            # 收盘后再读会被 `load_payload` 判旧并重建（否则盘中快照根会一直顶替真收盘）。
            "provisional": bool(str(last_row_date(daily_rows) or "")[:10]
                                == datetime.now().strftime("%Y-%m-%d")
                                and datetime.now().strftime("%H:%M") < "15:00"),
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


# ── 分时帧解析缓存（2026-10-14）────────────────────────────────────────────
# 为什么必须有：`load_minute_asof` 每次调用都要 **json.load 整个档 + 建 DataFrame +
#   dropna/sort/dedup**。实测（600362.SH，`_d540` 档 2.38MB / 17689 行，热进程取 3 次最小值）：
#     json.load 89.5ms + DataFrame 33.8ms + _norm_minutes 53.6ms ≈ 180ms/次；
#   而 `load_structure(一只票)` 要调它 **4 次**（5min/30min × 取数前、取数后各一次）⇒ ~0.7s/只。
#   再叠加 GUI 侧「仓位结构」预热池（2 线程 × ≤240 只/30min 槽位）⇒ GUI 进程 CPU 长期 ~45%，
#   GIL 争抢拖住 pywebview 的**结果投递**（线程栈实测：3 个线程堵在 `evaluate_js` 的信号量上），
#   用户可见症状就是**界面卡死**（见 doc/research 与本仓 GUI 卡死排查史）。
# 契约：这些文件是**只读缓存**，两次写之间内容恒定 ⇒ 按 (路径, mtime_ns, size) 记忆化；
#   写入侧改文件后键自然失效（`save_minute_history` 另有一条「没变就不写」的闸兜底）。
_MIN_FRAME_CACHE: "OrderedDict" = OrderedDict()
_MIN_FRAME_LOCK = threading.Lock()
_MIN_FRAME_MAX_ROWS = 600_000        # 行数预算（≈24MB）—— 本机仅 4GB 内存，必须封顶
_MIN_FRAME_MAX_ENTRIES = 128
_MIN_FRAME_ROWS = 0


def _norm_frame_cached(fp):
    """读一个分时档 → 规范帧（带记忆化）。

    ⚠️ 返回的是**缓存本体**，调用方**不得就地修改**（`load_minute_asof` 走 concat/sort，
    产出的帧已与缓存脱钩，故那里安全）。异常/空 → 空 DataFrame（与 `_norm_minutes` 同契约）。
    """
    import pandas as pd
    global _MIN_FRAME_ROWS
    try:
        st = os.stat(fp)
        key = (str(fp), st.st_mtime_ns, st.st_size)
    except OSError:
        return pd.DataFrame()
    with _MIN_FRAME_LOCK:
        hit = _MIN_FRAME_CACHE.get(key)
        if hit is not None:
            _MIN_FRAME_CACHE.move_to_end(key)
            return hit
    rows = (_read_json(fp, {}) or {}).get("rows") or []
    if not rows:
        return pd.DataFrame()
    try:
        d = _norm_minutes(pd.DataFrame(rows))
    except Exception:
        return pd.DataFrame()
    if d.empty:
        return pd.DataFrame()
    del rows
    with _MIN_FRAME_LOCK:
        # 同路径的旧版本先逐出（一轮写入只留最新版，别让废帧吃掉行数预算）
        for k in [k for k in _MIN_FRAME_CACHE if k[0] == key[0] and k != key]:
            _MIN_FRAME_ROWS -= int(_MIN_FRAME_CACHE[k].shape[0])
            del _MIN_FRAME_CACHE[k]
        _MIN_FRAME_CACHE[key] = d
        _MIN_FRAME_ROWS += int(d.shape[0])
        while _MIN_FRAME_CACHE and (len(_MIN_FRAME_CACHE) > _MIN_FRAME_MAX_ENTRIES
                                    or _MIN_FRAME_ROWS > _MIN_FRAME_MAX_ROWS):
            _, ev = _MIN_FRAME_CACHE.popitem(last=False)
            _MIN_FRAME_ROWS -= int(ev.shape[0])
    return d


def _invalidate_path(fp):
    """按**路径**逐出该档的解析缓存（写入侧用）。

    ⚠️ 为什么要显式逐出、不能只靠 (mtime,size) 键：Windows 的文件时间戳粒度约 15.6ms，
    同一 tick 内「等长改写两次」会得到**完全相同的键** ⇒ 缓存会回旧数据。`save_minute_history`
    是本目录唯一写入口，落盘后调本函数即可根治。"""
    global _MIN_FRAME_ROWS
    key0 = str(fp)
    with _MIN_FRAME_LOCK:
        for k in [k for k in _MIN_FRAME_CACHE if k[0] == key0]:
            _MIN_FRAME_ROWS -= int(_MIN_FRAME_CACHE[k].shape[0])
            del _MIN_FRAME_CACHE[k]


def invalidate_minute_cache(ts_code=None):
    """丢弃解析缓存：`ts_code=None` 全清；给了就只清该码（`ts_code_freq...`）的档。"""
    global _MIN_FRAME_ROWS
    with _MIN_FRAME_LOCK:
        if ts_code is None:
            _MIN_FRAME_CACHE.clear()
            _MIN_FRAME_ROWS = 0
            return
        pref = f"{ts_code}_"
        for k in [k for k in _MIN_FRAME_CACHE if os.path.basename(k[0]).startswith(pref)]:
            _MIN_FRAME_ROWS -= int(_MIN_FRAME_CACHE[k].shape[0])
            del _MIN_FRAME_CACHE[k]


def load_minute_asof(ts_code, freq, date_str=None, keep=None):
    """**零网络**：合并 plain（近月，新鲜）+ `_d540`（长历史）两档分钟缓存，按 time 去重后
    只留 `<= date_str`（当日 23:59:59）的根。date_str=None → 全量。空/缺 → 空 DataFrame。

    对应 `t_gui._min_bars_asof` 的 core 版：GUI 侧版本供「历史日 30min 判定」列用，
    本函数供结构层/回放复用，避免 core→t_gui 反向依赖。

    ⚠️ 2026-10-14：单档解析走 `_norm_frame_cached` 记忆化（见该函数上方注释——这是 GUI
    卡死的主因之一）。返回帧经 concat/sort 重建，**与缓存脱钩**，调用方可安全就地改。"""
    import pandas as pd
    if not ts_code:
        return pd.DataFrame()
    frames = []
    # 顺序即优先级：**后读的覆盖先读的**（keep="last"）。`_d540` 是长历史但可能陈旧
    # （5min 档实测停在 2026-09-18），plain 是新鲜的近月档 ⇒ plain 放最后，重复时刻以它为准。
    for name in (f"{ts_code}_{freq}_d540.json", f"{ts_code}_{freq}.json"):
        fp = _MINUTE_CACHE_DIR / name
        if not fp.exists():
            continue
        d = _norm_frame_cached(fp)      # 命中缓存时**不再 json.load**（那正是主成本）
        if not d.empty:
            frames.append(d)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
    if date_str:
        _end = pd.Timestamp(date_str) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        df = df[df["time"] <= _end].reset_index(drop=True)
    if keep and len(df) > keep:
        df = df.tail(keep).reset_index(drop=True)
    return df


# 2026-10-30：落盘 cap 按周期分档 —— K 线弹窗的 5/10 分钟档支持**近 3 个月**深度
# （5min：65 交易日 × 48 根 ≈ 3120 ⇒ cap 3600 留余量；10min 由深 5min 合成、磁盘不单独存，
#  这里仍给 1800 档以防将来直存）。其余周期维持 920（30/60min 计算口径不需要更深）。
# 内存评估（本机 4GB，`_MIN_FRAME_MAX_ROWS=600_000` 的解析缓存预算不变）：
#   单条 3600 行规范帧 ≈ 0.2MB（6 列 × 8B/格 + 索引开销）；即便 128 个缓存条目全是
#   深 5min 档 ≈ 22MB，仍远低于 600k 行（≈29MB）的逐出阈值，LRU 逐出兜底。
_MIN_SAVE_CAP = {"5min": 3600, "10min": 1800}


def save_minute_history(ts_code, freq, df, cap=None) -> bool:
    """把分钟 df **并入** plain 档 `{ts_code}_{freq}.json`（按 time 去重、cap 截尾、原子写）。

    cap=None ⇒ 按 `_MIN_SAVE_CAP` 分档（5min 3600 / 10min 1800 / 其它 920）。
    返回 True=落盘、False=**没写成**（入参空 / 合并后内容与盘上一致 ⇒ 刻意跳过，见函数内注释）。"""
    import pandas as pd
    if cap is None:
        cap = _MIN_SAVE_CAP.get(freq, 920)
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
        # 2026-10-14：**内容没变就不写**。原先无条件重写（`saved_at` 每次都是新值）⇒
        #   文件 mtime 每轮都变 ⇒ 上游 `_norm_frame_cached` 的 (mtime,size) 键每轮失效、白解析，
        #   且 240 只/槽位地刷盘。实测盘中 20 分钟内 **245 个 plain 档被改写而内容一根没多**
        #   （数据源盘中只到上一交易日 ⇒ merge 进来全是已有时刻）。返回 False = 未写。
        if rows == old_rows:
            return False
        tmp = _MINUTE_CACHE_DIR / f".{ts_code}_{freq}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "rows": rows}, ensure_ascii=False))
        os.replace(tmp, fp)
        _invalidate_path(fp)          # 显式逐出解析缓存（不赌文件时间戳粒度，见该函数注释）
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
    """逐只拉分钟线写 plain 档（tushare 单只、无法批量）。返回 {requested, got}。不抛。

    `5min`：结构层（10min 由 5min 降采样而来）的唯一磁盘源。tushare 无原生 10min。
    2026-10-30：5min 窗口 30→95 自然日（≈65 交易日 ≈ 3100+ 根），给 K 线弹窗「近3月」档
    铺磁盘底座；单只仍是**一次** `stk_mins` 调用，只是时间窗更宽，不增加请求次数。
    落盘 cap 按 `_MIN_SAVE_CAP` 分档（5min 3600），深度留得住。"""
    from analysis.divergence import _ts_code as _div_ts_code
    days_map = {"30min": 70, "60min": 130, "5min": 95}
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
            out["minute_small"] = prefetch_minute(small_codes, freqs=("30min", "60min", "5min"))
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


# ── 日线缓存「日期+收盘」共享解析层（2026-10-26，P2-15）────────────────────
# 为什么必须有：GUI 进程内「年限宽度（_compute_ma365_breadth，~5500 档）」与
#   「板块站上5/20线（_compute_sector_ma_breadth，~1700 档）」各自整目录 json.loads
#   同一批日线缓存（单线程 ~10s / 6~8s），彼此不共享 ⇒ 同一文件一天被重复解析多次。
#   实测单档解析后驻留 ~270KB（rows 原始 dict 列表），整池裸缓存 ~1.5GB，本机 4GB
#   内存扛不住 ⇒ 本层只缓存**轻量投影**（dates + closes）：
#     · dates 用 sys.intern 跨文件共享同一批日期字符串（≈8B/行引用）；
#     · closes 用 array('d')（8B/行，与 Python float 同精度）。
#   ≈16B/行 + tuple 开销，6000 档 × 800 行 ≈ 77MB 封顶（双闸：条数 + 总行数）。
# 转换语义与调用方原实现逐位一致：date → str(r.get("date"))[:10]；
#   close → float(r.get("close") or 0)；**任一行转换失败 = 整档解析失败**（不缓存、
#   返回 ((), ())），调用方按"样本不足"跳过 —— 与原 try/except → None 同向。
# 契约与 _norm_frame_cached 相同：键 = (路径, mtime_ns, size)，文件改写 ⇒ 键自然失效；
#   同路径旧版本先逐出。⚠️ 返回的 closes 是 array（可变）⇒ 调用方**不得就地修改**。
from array import array as _array

_DAILY_KV_CACHE: "OrderedDict" = OrderedDict()
_DAILY_KV_LOCK = threading.Lock()
_DAILY_KV_MAX_ENTRIES = 6000           # 略大于全池（~5537），保证一轮全量扫描后整池在缓存内
_DAILY_KV_MAX_ROWS = 4_800_000         # 行数预算 ≈ 77MB（6000 档 × 800 行 × ~16B）
_DAILY_KV_ROWS = 0


def daily_close_series(fp):
    """读一个日线缓存档 → (dates tuple, closes array('d'))，全进程共享记忆化。

    解析失败 / 无 rows / 任一行 close 转不动 → ((), ())（调用方按样本不足跳过，永不抛）。
    """
    global _DAILY_KV_ROWS
    try:
        st = os.stat(fp)
        key = (str(fp), st.st_mtime_ns, st.st_size)
    except OSError:
        return (), _array("d")
    with _DAILY_KV_LOCK:
        hit = _DAILY_KV_CACHE.get(key)
        if hit is not None:
            _DAILY_KV_CACHE.move_to_end(key)
            return hit
    try:
        rows = (_read_json(fp, {}) or {}).get("rows") or []
        if not rows:
            return (), _array("d")
        # intern：全池 5500 档的日期字符串其实只有 ~900 个不同值，共享后 dates 只剩指针
        dates = tuple(sys.intern(str(r.get("date"))[:10]) for r in rows)
        closes = _array("d", (float(r.get("close") or 0) for r in rows))
    except Exception:
        return (), _array("d")
    del rows
    n = len(dates)
    with _DAILY_KV_LOCK:
        # 同路径的旧版本先逐出（一轮回写只留最新版，别让废数据吃行数预算）
        for k in [k for k in _DAILY_KV_CACHE if k[0] == key[0] and k != key]:
            _DAILY_KV_ROWS -= len(_DAILY_KV_CACHE[k][0])
            del _DAILY_KV_CACHE[k]
        _DAILY_KV_CACHE[key] = (dates, closes)
        _DAILY_KV_ROWS += n
        while _DAILY_KV_CACHE and (len(_DAILY_KV_CACHE) > _DAILY_KV_MAX_ENTRIES
                                   or _DAILY_KV_ROWS > _DAILY_KV_MAX_ROWS):
            _, ev = _DAILY_KV_CACHE.popitem(last=False)
            _DAILY_KV_ROWS -= len(ev[0])
    return dates, closes


def invalidate_daily_close_cache(fp=None):
    """丢弃本层缓存：`fp=None` 全清；给了路径就只清该档（测试/写入口用）。"""
    global _DAILY_KV_ROWS
    with _DAILY_KV_LOCK:
        if fp is None:
            _DAILY_KV_CACHE.clear()
            _DAILY_KV_ROWS = 0
            return
        key0 = str(fp)
        for k in [k for k in _DAILY_KV_CACHE if k[0] == key0]:
            _DAILY_KV_ROWS -= len(_DAILY_KV_CACHE[k][0])
            del _DAILY_KV_CACHE[k]
