# -*- coding: utf-8 -*-
"""指数层面 MACD 背离检测（2026-09-28）。

**复用** `analysis.divergence` 的检测核（`_macd_dif` / `_local_extrema` /
`detect_divergence_events` / `_latest_fresh`），**不重写算法** —— 保证与仓内已发表研究
（`t_io/validation/board_div/`）口径一致。

数据来源
--------
- 指数分钟线：GM 5min（`get_provider().index_minute(..., freq="300s")`）→ 聚合 30/60min。
  GM 不可用时 `facade.index_minute` **返回空表（无腾讯兜底）** → 降级 tushare 逐日拼接
  （30/60min 原生，`index_regime_intraday._iri_fetch_stk_mins_one_day`）。
- 指数日线：`get_provider().index_daily`（GM → 腾讯，有兜底）。
- 平均股价（非交易所指数，GM 无此标的）：东财 secid，且带风控重试。

⚠️ 定位：**观察提示，不是交易信号**
-----------------------------------
仓内自研研究（2026-09-25，`t_io/validation/board_div/`）对指数背离的结论：

    30min 底  +5.8pp   显著
    60min 顶  +2.2pp   不显著
    30min 顶  −1.4pp   无边际（CI 重叠）
    60min 底  −3.4pp   不显著
    日线      未验证

⇒ **指数顶背离减仓规则至今未接线**。故每条提醒都带 `EVIDENCE` 分级与 `DISCLAIMER`，
且**绝不接进任何下单/减仓链路**。

⚠️ 确认天然滞后
---------------
`_local_extrema` 用 n_bars=3 确认峰谷 ⇒ 事件最早也在**峰值后 3 根 bar** 才可知。
30min 线上 = **1.5 小时**。文案必须体现，避免被读成"实时"。

⚠️ 窗口敏感性（实测，2026-09-28）
----------------------------------
`divergence.SWING_MULT` 门槛是**相对量**：`倍数 × 该窗口自身的中位 bar 振幅`。
⇒ **喂多长的窗口，就得什么样的门槛，事件集合随之改变**（不是"越长越准"）。

科创50 30min 实测（`t_io/validation/board_div/cache/000688_SH_30min.csv`）：

| 窗口(根) | 中位振幅 | 顶门槛(4×) | 09-22 10:30 靶子命中 |
|---|---|---|---|
| 133（生产口径） | 0.74% | 2.96% | ✅（price 1707.67 逐位一致） |
| 400 | 1.04% | 4.18% | ❌ 被门槛滤掉 |
| 11568（研究全历史） | 0.58% | 2.34% | ✅ |

生产用 GM 原生 800×5min ≈ **133 根 30min**，其门槛（2.96%）与研究口径（2.34%）同量级，
且能复现已知靶子 ⇒ 采用。但**证据分级（EVIDENCE）是在研究全历史窗口上标定的**，
与本模块的短窗口并非同一口径 —— 故分级只能当**量级参考**，不可当作精确边际。
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

from analysis import divergence as dv

# 参与检测的三个分辨率（"日线" 非 bar 周期名，仅作标签）
FREQS = ("30min", "60min", "日线")

# 证据分级：取自 t_io/validation/board_div/ 的实测边际，写死随提醒输出
EVIDENCE = {
    ("30min", "顶"): {"tier": "☆", "label": "无边际 (−1.4pp)", "significant": False},
    ("30min", "底"): {"tier": "★★", "label": "有边际 (+5.8pp 显著)", "significant": True},
    ("60min", "顶"): {"tier": "☆", "label": "不显著 (+2.2pp)", "significant": False},
    ("60min", "底"): {"tier": "☆", "label": "不显著 (−3.4pp)", "significant": False},
    ("日线", "顶"): {"tier": "☆", "label": "未验证", "significant": False},
    ("日线", "底"): {"tier": "☆", "label": "未验证", "significant": False},
}

DISCLAIMER = "仅供观察，不构成交易信号"
LAG_NOTE = "背离需 3 根 bar 确认（30min 线即峰值后 1.5 小时）"

# 提醒窗口（事件式）—— 2026-09-28 修正，**与展示窗口不是一回事**
# ---------------------------------------------------------------------------
# `divergence.MAX_AGE_BARS`（30min=32 / 60min=20）是**展示**口径：目的是让个股背离列
# "不空"（其注释自陈 "30min ≤32 根 → 49% 的票有显示"）。拿它当提醒阈值 ⇒ 会把 22 根前
# 的旧事件当成当前信号推出去。
#
# owner 2026-09-28 实测踩中：当天大盘一路下跌、同花顺未报任何顶背离，系统却弹出
# 09-22 10:30 的科创50 顶背离（bars_ago=22，约 3 个交易日前）。
#
# 提醒只认「刚被确认」：`_local_extrema` 需 3 根确认峰谷 ⇒ bars_ago 最小即 3；
# 留 1~2 根余量给 300s 轮询节奏，故 30min 取 5、60min 与日线取 4
# （日线与 `divergence.DAILY_ALERT_MAX_AGE_BARS` 同值，口径一致）。
ALERT_MAX_AGE_BARS = {"30min": 5, "60min": 4, "日线": 4}

# GM 5min 一次取多少根：800 × 5min ≈ 16.7 个交易日 → 聚合后 30min ≈133 根 / 60min ≈66 根
GM_COUNT_BARS = 800
# tushare 降级时回溯的自然日数（覆盖 ~11 个交易日）
TS_LOOKBACK_DAYS = 20
# 日线回溯根数（MACD 预热 + 峰谷）
DAILY_COUNT = 200

# 统一分析窗口（2026-09-28）：**必须对所有指数取同一长度**。
# SWING_MULT 门槛 = 倍数 × 该窗口自身的中位 bar 振幅 ⇒ 窗口不同则门槛不同、事件集合不同。
# 各源原生长度并不一致（GM 30min≈134 / 东财 30min≈250；东财日线可达 6000+），
# 若各用各的，等于**同一个"证据分级"套在不同门槛上**。故统一截断到下列长度。
WINDOW_BARS = {"30min": 120, "60min": 60, "日线": 200}

_UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

# 单次检测内复用原始 5min（{symbol: df}），由 detect_index_divergence 开头清空
_RAW5_CACHE = {}


# ---------------------------------------------------------------- bar 取数
def _agg_minutes(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """5min → 30/60min 聚合，一天恰好 8 根 30min / 4 根 60min（对齐 board_div 研究口径）。

    **不能按时钟重采样**：A股 60min 的桶不是整点对齐的 —— 上午是 09:30–10:30 / 10:30–11:30，
    下午是 13:00–14:00 / 14:00–15:00；用 `resample("60min")` 会按整点切出 5 个桶，
    其中 12:00 那个是午休空档的幻影桶（实测：60min 得 5 根而非 4 根）。
    30min 恰好因 09:30 是半点边界而"看起来对"，属侥幸，同样不能依赖。

    ⇒ 改为**按段内 bar 序号切块**：上午段 / 下午段各自每 6 根（30min）或 12 根（60min）成一块。
    块时间取段内最后一根 bar 的时刻（= bar 结束时刻，与同花顺/通达信一致）。
    """
    if df is None or df.empty:
        return pd.DataFrame()
    per_bucket = 6 if rule == "30min" else 12
    d = df.copy()
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values("time").reset_index(drop=True)
    d["_day"] = d["time"].dt.strftime("%Y-%m-%d")
    d["_am"] = d["time"].dt.strftime("%H:%M") <= "11:30"   # 上午段 / 下午段
    d["_grp"] = d.groupby(["_day", "_am"]).cumcount() // per_bucket
    out = d.groupby(["_day", "_am", "_grp"], sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), time=("time", "last"),
    ).reset_index(drop=True)
    # 丢竞价根（tushare 原生 30/60min 会带一根 09:30；GM 5min 路径本就没有）
    out = out[out["time"].dt.strftime("%H:%M") != "09:30"]
    return out.sort_values("time").reset_index(drop=True)


def _gm_minute_bars(symbol: str, freq: str) -> pd.DataFrame:
    """GM 5min → 目标分辨率。GM 不可用时 facade 返回空表。

    单次检测内按 symbol 复用原始 5min（`_RAW5_CACHE`）：30min 与 60min 同源，
    否则每个指数要打两次 `index_minute`（单次调用在 GM 变慢时可吃满 12s 超时）。
    """
    hit = _RAW5_CACHE.get(symbol)
    if hit is None:
        try:
            from core.market_data import get_provider
            hit = get_provider().index_minute(symbol, count_bars=GM_COUNT_BARS, freq="300s")
        except Exception:
            hit = pd.DataFrame()
        if hit is None:
            hit = pd.DataFrame()
        _RAW5_CACHE[symbol] = hit
    if hit.empty:
        return pd.DataFrame()
    return _agg_minutes(hit, "30min" if freq == "30min" else "60min")


def _ts_minute_bars(ts_code: str, freq: str) -> pd.DataFrame:
    """tushare 逐日拼接（GM 不可用时的降级路径）。"""
    if not ts_code:
        return pd.DataFrame()
    try:
        from analysis.index_regime_intraday import _iri_fetch_stk_mins_one_day
    except Exception:
        return pd.DataFrame()
    frames = []
    today = datetime.now()
    for back in range(TS_LOOKBACK_DAYS):
        d = (today - timedelta(days=back)).strftime("%Y-%m-%d")
        if today.weekday() >= 5 and back == 0:
            continue
        try:
            one = _iri_fetch_stk_mins_one_day(ts_code, d, freq)
        except Exception:
            continue
        if one is None or len(one) == 0:
            continue
        frames.append(one)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    if "time" not in out.columns:
        return pd.DataFrame()
    out["time"] = pd.to_datetime(out["time"])
    out = out.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)
    return out[out["time"].dt.strftime("%H:%M") != "09:30"].reset_index(drop=True)


def _em_kline(secid: str, klt: int, lmt: int = 400, attempts: int = 8) -> list:
    """东财 K 线（平均股价等腾讯/GM 都无代码的标的）。

    push2his **有风控**（间歇断连），仓库既有做法是重试 8 次（多数 3-6 次内成功，
    见 t_gui.load_stock_chart 的 em 分支）。返回原始 klines 字符串列表，失败返回 []。
    """
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "*"
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
           f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
           f"&klt={klt}&fqt=1&beg=0&end=20500101&lmt={lmt}")
    for _ in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, headers=_UA)
            raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
            kl = (data.get("data") or {}).get("klines") or []
            if kl:
                return kl
        except Exception:
            pass
    return []


def _em_bars(symbol: str, freq: str) -> pd.DataFrame:
    """平均股价 bars。symbol 形如 "em47.800005"。分钟级受风控，失败返回空表由调用方降级。"""
    secid = str(symbol)[2:] if str(symbol).startswith("em") else str(symbol)
    klt = {"30min": 30, "60min": 60, "日线": 101}[freq]
    kl = _em_kline(secid, klt, lmt=(DAILY_COUNT if freq == "日线" else 400))
    rows = []
    for item in kl:
        p = str(item).split(",")
        if len(p) < 6:
            continue
        try:
            rows.append({"time": p[0], "open": float(p[1]), "close": float(p[2]),
                         "high": float(p[3]), "low": float(p[4]), "volume": float(p[5])})
        except (TypeError, ValueError):
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    return df.sort_values("time").reset_index(drop=True)


def _trim(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """截断到统一窗口 WINDOW_BARS[freq]（尾部）。见该常量注释：窗口长度会改变 SWING 门槛。"""
    n = WINDOW_BARS.get(freq)
    if df is None or df.empty or not n or len(df) <= n:
        return df
    return df.tail(n).reset_index(drop=True)


def fetch_index_bars(entry: dict, freq: str):
    """按注册表条目取某一分辨率的指数 bars（已截断到统一窗口）。返回 (df, source)。

    source ∈ {gm, tushare, em, none}
    """
    symbol, ts_code, src = entry.get("symbol"), entry.get("ts_code"), entry.get("source")
    if freq == "日线":
        if src == "em":
            df = _em_bars(symbol, freq)
            return _trim(df, freq), ("em" if not df.empty else "none")
        try:
            from core.market_data import get_provider
            df = get_provider().index_daily(symbol, days=DAILY_COUNT)
            if df is not None and not df.empty:
                out = df.rename(columns={"date": "time"}).copy()
                out["time"] = pd.to_datetime(out["time"])
                out = out.sort_values("time").reset_index(drop=True)
                return _trim(out, freq), "gm"
        except Exception:
            pass
        return pd.DataFrame(), "none"
    # 分钟级
    if src == "em":
        df = _em_bars(symbol, freq)
        return _trim(df, freq), ("em" if not df.empty else "none")
    try:
        df = _gm_minute_bars(symbol, freq)
        if not df.empty:
            return _trim(df, freq), "gm"
    except Exception:
        pass
    df = _ts_minute_bars(ts_code, freq)
    return _trim(df, freq), ("tushare" if not df.empty else "none")


# ---------------------------------------------------------------- 检测
def _fmt_event_time(t, freq: str) -> str:
    """统一事件时点格式：分钟级 "YYYY-MM-DD HH:MM:SS"，日线 "YYYY-MM-DD"。

    原始值是 pandas Timestamp，str() 出来是 "2026-09-22T10:30:00.000000"——
    既难看又会污染去重键（键必须跨进程稳定可比）。
    """
    try:
        ts = pd.to_datetime(t)
        if pd.isna(ts):
            return str(t)
        return ts.strftime("%Y-%m-%d" if freq == "日线" else "%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(t)


def _alert_key(entry: dict, freq: str, typ: str, time_str: str) -> str:
    """事件级去重键：同一指数+周期+方向+事件时点 → 同一个 key。"""
    return f"{entry.get('symbol')}|{freq}|{typ}|{time_str}"


def detect_index_divergence(board: list | None = None, freqs: tuple = FREQS) -> dict:
    """检测指数板各指数在 30min/60min/日线的顶/底背离。

    返回 {"alerts": [...], "watching": [...], "health": {...},
          "alert_window": {...}, "display_window": {...}, "generated_at": str}

    - `alerts`  ：**提醒集**——仅「刚被确认」的事件（`ALERT_MAX_AGE_BARS`，紧）。
                  驱动飞书推送 / GUI 闪烁 / 指数卡角标。这是"新事件"的唯一判据。
    - `watching`：**展示集**——宽窗口（`divergence.MAX_AGE_BARS`）内但未进提醒集的旧事件，
                  供 GUI 静默展示（不闪、不推），避免"背离在生效中"这一上下文丢失。
    每条 = {index, symbol, freq, type, time, price, dif, bars_ago, consec,
            tier, evidence, significant, disclaimer, lag_note, source, key}
    """
    if board is None:
        from core.board_index import gui_board
        board = gui_board()

    alerts, watching, health = [], [], {}
    _RAW5_CACHE.clear()      # 单次检测内复用 5min，避免 30/60min 重复取数
    for entry in board:
        name, symbol = entry.get("name"), entry.get("symbol")
        for freq in freqs:
            try:
                df, source = fetch_index_bars(entry, freq)
            except Exception as e:
                health[f"{name}|{freq}"] = {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:80]}"}
                continue
            if df is None or df.empty:
                health[f"{name}|{freq}"] = {"ok": False, "source": source,
                                            "reason": "bars 不可得（GM 不可用且降级失败）"}
                continue
            health[f"{name}|{freq}"] = {"ok": True, "source": source, "bars": int(len(df))}
            if len(df) < 40:   # detect_divergence_events 的下限（MACD 预热 + 峰谷）
                health[f"{name}|{freq}"]["ok"] = False
                health[f"{name}|{freq}"]["reason"] = f"bars 不足({len(df)}<40)"
                continue
            try:
                events = dv.detect_divergence_events(df)
                # 提醒窗口（事件式，紧）与展示窗口（宽）分别取最新未过期事件
                last = dv._latest_fresh(events, ALERT_MAX_AGE_BARS.get(freq, 4))
                watch = dv._latest_fresh(
                    events, (dv.DAILY_ALERT_MAX_AGE_BARS if freq == "日线"
                             else dv.MAX_AGE_BARS.get(freq, 20)))
            except Exception as e:
                health[f"{name}|{freq}"]["ok"] = False
                health[f"{name}|{freq}"]["reason"] = f"检测异常 {type(e).__name__}"
                continue

            def _mk(e):
                typ = e.get("type")
                time_str = _fmt_event_time(e.get("time"), freq)
                ev = EVIDENCE.get((freq, typ),
                                  {"tier": "?", "label": "未知", "significant": False})
                return {
                    "index": name, "symbol": symbol, "freq": freq, "type": typ,
                    "time": time_str, "price": e.get("price"), "dif": e.get("dif"),
                    "bars_ago": int(e.get("bars_ago", 0)), "consec": bool(e.get("consec", False)),
                    "tier": ev["tier"], "evidence": ev["label"], "significant": ev["significant"],
                    "disclaimer": DISCLAIMER, "lag_note": LAG_NOTE, "source": source,
                    "key": _alert_key(entry, freq, typ, time_str),
                }

            if last is not None:
                alerts.append(_mk(last))
            # 展示集：宽窗口内、但**未**进入提醒集的（已在提醒集的不重复列），供 GUI 静默展示
            if watch is not None:
                w = _mk(watch)
                if not last or w["key"] != alerts[-1]["key"]:
                    watching.append(w)
    return {"alerts": alerts, "watching": watching, "health": health,
            "alert_window": dict(ALERT_MAX_AGE_BARS), "display_window": dict(dv.MAX_AGE_BARS),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


def format_alert_line(a: dict) -> str:
    """一行文本摘要（飞书/GUI 共用，保持口径一致）。"""
    return (f"{a['index']} {a['freq']} {a['type']}背离 | {a['time']} "
            f"| 证据 {a['tier']} {a['evidence']} | {a['disclaimer']}")
