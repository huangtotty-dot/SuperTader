# -*- coding: utf-8 -*-
"""30min 趋势判定 — 适配层：取数 → 指标 → 状态机 → 最新快照。

数据获取**不复用** `analysis/divergence.py::fetch_freq_kline`——后者按「当日」缓存，
盘中首拉一次后整天不再刷新（背离列可容忍陈旧，趋势判定不行）。本层改为**原生 30min**
（tushare stk_mins freq="30min"，轻量）+ **按 30 分钟时段缓存**：同一时段内秒回、跨时段
自动重取，状态随每根 30min bar 收盘推进。tushare 访问器 `_iri_tushare_pro` 与 ts_code
映射 `_ts_code` 仍复用。

并发：模块级 Semaphore 限流 + 失败重试退避，避免批量标签刷新时打爆 tushare 配额。
失败/数据不足时返回 `source ∈ {error, insufficient}`，由调用方回退日线口径。
"""
import os
import sys
import threading
import time
from datetime import datetime

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

from analysis.trend30.indicators import (  # noqa: E402
    drop_forming_bar, collapse_stubs, mark_bar_meta, add_30min_indicators, linreg_quality,
)
from analysis.trend30.state_machine import Trend30Config, Trend30StateMachine  # noqa: E402

_CACHE = {}
_LOCK = threading.Lock()
_SEM = threading.Semaphore(8)
_RETRY = 2
# 原始 30min 帧的**预取缓存**（2026-10-23，owner 报「猎手技术标签太慢」）：
#   批量标签原先逐只「取数(190ms 纯网络) → 状态机(77ms)」串在 2 个 pandas 名额里
#   ⇒ 取数那段白占名额，50 只要 30~40 秒。`prefetch_30min` 把取数拆出来 8 路并发，
#   `get_trend30` 命中这里的原始帧就不再出网，名额只用来跑状态机。
_RAW = {}                 # code -> {"slot": 30min 时段, "df": DataFrame}
_RAW_MAX = 400            # ≈ 400×640 行 ≈ 12MB，够一屏板块翻页用

_STATE2TREND = {"BULL": "up", "BEAR": "down", "RANGE": "flat"}

# ── 判定快照的**磁盘缓存**（2026-10-28，owner：各表"计算中"太久）────────────
# 为什么：`_CACHE` 只在内存里，而 30min 趋势的输入（**已完成** bar 的序列）在一个 slot 内
# 完全不变 —— 重启 GUI / 重开板块 / 另开进程都不该重跑那条 130~370ms 的 pandas 管线。
# 实测 50 只的板块首次要 20~30s 的"⏳计算中"，其中一多半是重复计算。
# 键 = (code, slot)；一个 slot 落一个 JSON，切 slot 时旧文件自动淘汰（保留最近 2 个）。
_SNAP_DIR = os.path.join(_BASE, "t_io", "cache", "trend30_slot")
_SNAP_PENDING = {}          # slot -> {code: value}
_SNAP_LOADED = None         # 已灌进内存的 slot
_SNAP_AT = 0.0


def _snap_path(slot: str) -> str:
    return os.path.join(_SNAP_DIR, slot.replace(" ", "_").replace(":", "") + ".json")


def _jsonable(v):
    """numpy 标量 → 原生（json.dump 不认识 np.float64）。"""
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    return v


def _snap_load(slot: str) -> None:
    """把该 slot 的磁盘快照灌进 `_CACHE`（每个 slot 只读一次盘）。"""
    global _SNAP_LOADED
    if _SNAP_LOADED == slot:
        return
    _SNAP_LOADED = slot
    try:
        import json as _json
        with open(_snap_path(slot), encoding="utf-8") as f:
            d = _json.load(f)
        if isinstance(d, dict):
            with _LOCK:
                for c, v in d.items():
                    if isinstance(v, dict):
                        _CACHE.setdefault(c, {"slot": slot, "value": v})
            if d:
                print(f"[trend30] 复用磁盘快照 {len(d)} 只（slot={slot}）", flush=True)
    except Exception:
        pass


def _snap_add(slot: str, code: str, value: dict) -> None:
    _SNAP_PENDING.setdefault(slot, {})[code] = _jsonable(value)
    _snap_flush()


def _snap_flush() -> None:
    """节流落盘（≤1 次 / 1.5s）+ 淘汰旧 slot 文件。失败静默（缓存不是正确性依赖）。"""
    global _SNAP_AT
    if not _SNAP_PENDING:
        return
    now = time.time()
    if now - _SNAP_AT < 1.5:
        return
    _SNAP_AT = now
    with _LOCK:
        pend = dict(_SNAP_PENDING)
        _SNAP_PENDING.clear()
    try:
        import json as _json
        os.makedirs(_SNAP_DIR, exist_ok=True)
        for slot, items in pend.items():
            path = _snap_path(slot)
            old = {}
            try:
                with open(path, encoding="utf-8") as f:
                    old = _json.load(f) or {}
            except Exception:
                old = {}
            old.update(items)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(old, f)
            os.replace(tmp, path)
        # 只留最近 2 个 slot 文件（一个 slot = 30 分钟，留 2 个够跨日边界）
        try:
            fs = [(os.path.getmtime(os.path.join(_SNAP_DIR, x)), x)
                  for x in os.listdir(_SNAP_DIR) if x.endswith(".json")]
            for _, x in sorted(fs)[:-2]:
                os.remove(os.path.join(_SNAP_DIR, x))
        except Exception:
            pass
    except Exception:
        pass


def state_to_trend(state) -> str:
    """BULL→up / BEAR→down / RANGE→flat（未知/None → flat）。"""
    return _STATE2TREND.get(str(state or "").upper(), "flat")


def _slot(dt=None) -> str:
    """当前 30 分钟时段键（如 '2026-10-04 10:30'）。

    **收盘后 / 非交易日冻结到 15:30**（2026-10-28）：一日之内的判定只随「已收盘 30min bar」
    变化，收盘后序列不可能再变。原来收盘后键还每 30 分钟往前滚 ⇒ 判定缓存（含磁盘快照）
    每半小时被整份作废、全部重算 —— 盘后打开板块依旧"⏳计算中"正是这么来的。
    ⚠️ 冻结点是 **15:30 而不是 15:00**：末根 15:00 的 bar 要到 15:30 才算收盘
    （`drop_forming_bar` 按 `bar_time+30min` 判），15:00~15:29 算出来的结果**不含末根**，
    冻结它就等于整个晚上用错一版。
    """
    dt = dt or datetime.now()
    if dt.weekday() >= 5 or (dt.hour * 100 + dt.minute) >= 1530:
        return dt.strftime("%Y-%m-%d") + " 15:30"
    return dt.strftime("%Y-%m-%d %H:") + ("00" if dt.minute < 30 else "30")


def _fetch_30min(code: str, days: int = 35) -> pd.DataFrame:
    """30min bars → {time,open,high,low,close,volume(,amount)}。

    2026-10-18：**腾讯 mkline 优先**。原先每只票每个 30min 时段都打 tushare —
    批量标签（猎手/风向标 300~400 只）会瞬间打出几百个 tushare 请求，叠加默认 30s 超时
    与两次重试、且 DNS 不受 requests timeout 约束 ⇒ 卡死栈里常见 `adapter._fetch_30min`
    卡在 `getaddrinfo`。腾讯走 keep-alive（≈60ms）、640 根 ≈ 80 日、**含未收盘根**
    （`drop_forming_bar` 会丢掉）。
    后备顺序：**腾讯 → 本地磁盘（零网络）→ tushare（兜底，BJ/腾讯失败时）**。"""
    # ① 腾讯（实时）
    try:
        from core.market_data.tencent_provider import minute_kline
        d = minute_kline(code, "m30", 640)
        if d is not None and not d.empty:
            d = d[[c for c in ("time", "open", "high", "low", "close", "volume")
                   if c in d.columns]].copy()
            d["time"] = pd.to_datetime(d["time"])
            return d.sort_values("time").reset_index(drop=True)
    except Exception:
        pass
    # ② 本地磁盘（零网络；可能陈旧，但比出网卡死强）
    try:
        from core import chart_cache as _cc
        from analysis.divergence import _ts_code
        d = _cc.load_minute_history(_ts_code(code), "30min")
        if d is not None and not d.empty:
            d = d[[c for c in ("time", "open", "high", "low", "close", "volume")
                   if c in d.columns]].copy()
            d["time"] = pd.to_datetime(d["time"])
            return d.sort_values("time").reset_index(drop=True)
    except Exception:
        pass
    # ③ tushare 兜底（原实现）
    from analysis.divergence import _ts_code
    from analysis.index_regime_intraday import _iri_tushare_pro
    pro = _iri_tushare_pro()
    start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    df = pro.stk_mins(ts_code=_ts_code(code), freq="30min",
                      start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume"})
    df["time"] = pd.to_datetime(df["time"])
    keep = [c for c in ("time", "open", "close", "high", "low", "volume", "amount") if c in df.columns]
    return df[keep].sort_values("time").reset_index(drop=True)


def evaluate_bars(df, cfg: Trend30Config = None) -> dict:
    """纯计算：**已取好的** 30min bars（{time,open,high,low,close,volume} 升序）→ 最新趋势快照，
    与 `get_trend30` 同一管线（drop_forming_bar→collapse_stubs→mark_bar_meta→指标→状态机），
    但**不出网**。供 GUI 热路径（持仓体检）直接喂磁盘缓存的 30min bars，零网络阻塞主线程。

    成功 → {trend, state, confidence, adx, bar_time, n_bars, gate_ok, r2, er10}（不含 source/code）。
    数据不足 → {trend:None, state:None, n_bars:..., insufficient:True}。
    异常     → {trend:None, state:None, error:...}。"""
    cfg = cfg or Trend30Config()
    if df is None or getattr(df, "empty", True):
        return {"trend": None, "state": None, "n_bars": 0, "insufficient": True}
    try:
        d = drop_forming_bar(df)
        d = collapse_stubs(d)
        d = mark_bar_meta(d)
        d = add_30min_indicators(d, st_n=cfg.st_n, st_mult=cfg.st_mult)
        d = d.join(linreg_quality(d["close"], n=cfg.reg_n))
    except Exception:
        return {"trend": None, "state": None, "error": "indicators"}
    if len(d) < cfg.min_bars:
        return {"trend": None, "state": None, "n_bars": len(d), "insufficient": True}
    try:
        sm = Trend30StateMachine(cfg)
        sm.run(d)
        cur = sm.current()
    except Exception:
        return {"trend": None, "state": None, "error": "state_machine"}
    _last = d.iloc[-1]
    _r2 = _last.get("r2", np.nan)
    _er = _last.get("er10", np.nan)
    return {"trend": state_to_trend(cur["state"]), "state": cur["state"],
            "confidence": cur["confidence"], "adx": cur["adx"],
            "bar_time": cur["bar_time"], "n_bars": cur["n_bars"], "gate_ok": cur["gate_ok"],
            "r2": (None if pd.isna(_r2) else round(float(_r2), 3)),
            "er10": (None if pd.isna(_er) else round(float(_er), 3))}


def prefetch_30min(codes, workers: int = 8) -> int:
    """**并发预取** 30min bars（只取数、不跑状态机），供 `get_trend30` 直接命中。

    为什么要有：批量标签（猎手展开板块 ≤50 只）原先逐只「取数 → 状态机」串在
    `_BG_PANDAS_SEM` 的 2 个名额里，而取数 190ms/只**纯网络**、白占 pandas 名额
    ⇒ 一个 50 只的板块要 30~40 秒才出标签。拆出来 8 路并发后，每只只剩 ~80ms 的计算。
    返回实际发起取数的只数（同 30min 时段内已有的跳过）。
    """
    import threading as _th

    slot = _slot()
    _snap_load(slot)          # 先把本 slot 的磁盘快照灌进 _CACHE（每个 slot 只读一次盘）
    todo = []
    with _LOCK:
        for c in codes or []:
            c = str(c).split("_")[0]
            if not c:
                continue
            r = _RAW.get(c)
            if r and r.get("slot") == slot and r.get("df") is not None:
                continue
            # **已有本 slot 的判定快照 ⇒ 根本不需要再取 bars**（2026-10-28）。磁盘缓存上线后，
            # 第二次打开同一个板块/重启后同一时段，取数原本还是照做（纯浪费：50 只 ≈ 10s 网络）。
            _sn = _CACHE.get(c)
            if _sn and _sn.get("slot") == slot:
                continue
            if c not in todo:
                todo.append(c)
    if not todo:
        return 0

    def _one(c):
        try:
            with _SEM:
                df = _fetch_30min(c)
        except Exception:
            return
        if df is not None and not df.empty:
            with _LOCK:
                if len(_RAW) > _RAW_MAX:
                    _RAW.clear()
                _RAW[c] = {"slot": slot, "df": df}

    from concurrent.futures import ThreadPoolExecutor
    # 2026-10-30：命名（线程级 CPU 归因；此前所有未命名池在 thread_cpu.jsonl 里混成一组）
    with ThreadPoolExecutor(max_workers=max(1, int(workers)),
                            thread_name_prefix="prefetch30") as ex:
        list(ex.map(_one, todo))
    return len(todo)


def get_trend30(code: str, days: int = 35, cfg: Trend30Config = None, use_cache: bool = True) -> dict:
    """一站式：取数 → 指标 → 状态机 → 最新快照。
    返回 {state, trend('up'/'down'/'flat'), confidence, adx, bar_time, n_bars, gate_ok, source}；
    失败返回 {source:'error'|'insufficient', trend:None, state:None}。"""
    code = str(code).split("_")[0]
    cfg = cfg or Trend30Config()
    slot = _slot()
    if use_cache:
        with _LOCK:
            c = _CACHE.get(code)
            if c and c.get("slot") == slot:
                return c["value"]
        _snap_load(slot)                 # 内存没有 → 看磁盘快照（重启后同 slot 免算）
        with _LOCK:
            c = _CACHE.get(code)
            if c and c.get("slot") == slot:
                return c["value"]

    df = None
    with _LOCK:
        _r = _RAW.get(code)
        if _r and _r.get("slot") == slot:
            df = _r["df"]                    # 预取命中（同 30min 时段）⇒ 零网络
    if df is None:
        with _SEM:
            for att in range(_RETRY + 1):
                try:
                    df = _fetch_30min(code, days)
                    if df is not None and not df.empty:
                        break
                except Exception:
                    df = None
                if att < _RETRY:
                    time.sleep(0.4 * (att + 1))

    if df is None or df.empty:
        return {"source": "error", "trend": None, "state": None, "code": code}

    res = evaluate_bars(df, cfg)
    if res.get("state") is None:
        out = {"source": ("insufficient" if res.get("insufficient") else "error"),
               "trend": None, "state": None, "code": code}
        if "n_bars" in res:
            out["n_bars"] = res["n_bars"]
        return out

    res = dict(res)
    res["source"] = "30min"
    res["code"] = code
    with _LOCK:
        _CACHE[code] = {"slot": slot, "value": res}
    _snap_add(slot, code, res)           # 落磁盘：同 slot 内重启/重开不再重算
    return res


# ── §5 做T许可映射表（30min 状态 × 日线三档）────────────────────────────
#   日线档：多={bull,uptrend} / 中={base,neutral} / 空={downtrend,weak_breakdown}
_BUCKET = {"bull": "多", "uptrend": "多", "base": "中", "neutral": "中",
           "downtrend": "空", "weak_breakdown": "空"}
# (allow_zheng_t, allow_fan_t, max_position_ratio, reason)
_PERM = {
    ("BULL", "多"): (True, False, 1.00, "趋势多×日线多：正T，禁反T防卖飞"),
    ("BULL", "中"): (True, True, 0.70, "趋势多×日线中：正T，反T仅冲高急拉减半"),
    ("BULL", "空"): (True, False, 0.30, "趋势多×日线空：原则上停手；R²>0.6 且 ER>0.35 才小仓正T"),
    ("RANGE", "多"): (True, False, 0.50, "震荡×日线多：轻仓正T，只做回踩"),
    ("RANGE", "中"): (False, False, 0.00, "震荡×日线中：停手为主"),
    ("RANGE", "空"): (False, False, 0.00, "震荡×日线空：停手"),
    ("BEAR", "多"): (False, True, 0.00, "空头×日线多：禁正T，允许反T，底仓不动"),
    ("BEAR", "中"): (False, True, 0.30, "空头×日线中：禁正T，反T活动仓≤30%"),
    ("BEAR", "空"): (False, False, 0.00, "空头×日线空：全面停手，只观察"),
}


def get_trade_permission(code: str, daily_trend_bg: str = None) -> dict:
    """§5 做T许可映射。输入 30min 状态（自动取）+ 日线 trend_bg 七态（调用方给）。
    返回 {state_30min, trend_bg_daily, bucket, allow_zheng_t, allow_fan_t,
          max_position_ratio, reason, confidence, r2, er10, source, daily_fallback}。
    30min 数据不可用时各许可为 None（调用方自行决定放行/拦截）。"""
    r = get_trend30(code)
    state = r.get("state")
    bg = str(daily_trend_bg or "").lower()
    bucket = _BUCKET.get(bg)
    daily_fallback = bucket is None
    if daily_fallback:
        bucket = "中"   # 日线态未知 → 取中性档
    base = {"state_30min": state, "trend": r.get("trend"), "trend_bg_daily": daily_trend_bg,
            "bucket": bucket, "confidence": r.get("confidence"), "r2": r.get("r2"),
            "er10": r.get("er10"), "source": r.get("source"), "daily_fallback": daily_fallback}
    if r.get("source") != "30min" or not state:
        base.update({"allow_zheng_t": None, "allow_fan_t": None,
                     "max_position_ratio": None, "reason": "30min 数据不可用"})
        return base
    az, af, ratio, reason = _PERM[(state, bucket)]
    # §5 附注：BULL×日线空 的小仓正T 需 R²>0.6 且 ER>0.35，否则停手
    if state == "BULL" and bucket == "空":
        r2, er = r.get("r2"), r.get("er10")
        ok = (r2 is not None and r2 >= 0.6) and (er is not None and er >= 0.35)
        if not ok:
            az, ratio = False, 0.00
            reason = "趋势多×日线空：R²/ER 未达标 → 停手"
    base.update({"allow_zheng_t": az, "allow_fan_t": af,
                 "max_position_ratio": ratio, "reason": reason})
    return base
