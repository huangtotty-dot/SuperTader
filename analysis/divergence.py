# -*- coding: utf-8 -*-
"""
divergence.py — 30/60 分钟线顶背离/底背离检测（2026-08-19 新增）

需求：建仓扫描待选股增加背离列，30/60 分钟线出现顶/底背离时显示并飞书提醒。

数据：tushare stk_mins 近 30 日 30/60 分钟线（含当日 forming 根），当日缓存（盘中首拉一次，后续秒级）。
背离判定（与 t_gui 日线背离同逻辑，MACD dif）：
  - 顶背离：价格创新高，但 MACD dif 未创新高（看跌）
  - 底背离：价格创新低，但 MACD dif 未创新低（看涨）
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]  # 项目根（本模块位于 analysis/ 下）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

CACHE_DIR = BASE / "t_io" / "cache" / "tushare_mins"
FETCH_DAYS = 35

# 新鲜度口径（2026-09-19）：背离事件超过 N 根 bar 未更新即视为过期，不再展示/推送。
# 修正前 detect_minute_divergence_detail 取 events[-1] 且不检查年龄，
# 实测把 24 天前的 30min 底背离当成当前信号（300153，189 根前）。
#
# 阈值标定（39 只样本实测 bars_ago 分布，见 plan 验证节）：
#   30min ≤32 根 → 49% 的票有显示；60min ≤20 根 → 37%。既滤陈旧又不会空列。
#   注意地板：_local_extrema 需 3 根 bar 确认峰谷 ⇒ 最新可能的事件也已 3 根前，
#   故「约 1 个交易日」级别的阈值（8/6 根）会让该列长期为空。
MAX_AGE_BARS = {"30min": 32, "60min": 20}

# "创新高/新低"的幅度门槛（2026-09-20）：没有它时，下跌途中两个相邻微反弹峰
# 只要高出 0.1% 就被判顶背离（实测 002202：18.55 vs 18.52 仅高 0.16%，真实顶在 19.58）。
# 0.3% 与 t0_schemes/run_experiment_v2.py 的 BUF 同源，保持项目内口径一致。
PRICE_EXCESS = 0.003

# 背离必须发生在对应价区（对齐 同花顺/通达信 口径）：顶背离要求 DIF>0、底背离要求 DIF<0。
# 关闭时，下跌趋势中 DIF 深负区的小反弹会被误标为"顶背离"（owner 2026-09-20 报的 002202）。
REQUIRE_DIF_ZONE = True

# "两个极值必须被一次真实摆动分开"（2026-09-20 owner 抽查 300475 后加）。
#
# 问题：_local_extrema(n_bars=3) 会找出**大量噪声级小极值**，而背离判定只取相邻两个，
# 于是同一段筑底/做顶过程里的两个小极值会被当成"两个低点/高点"去比对。
# 实测 300475（30min）：谷 160.85(09-15 13:00) 与 谷 159.20(09-16 09:30) **仅隔 5 根、
# 中间只反弹 2.26%** ⇒ 被判底背离；而行情软件把 09-14~09-16 视为**同一个底**（158.34），
# 根本不存在两个低点 ⇒ owner 看到的图"没有背离"。
#
# 判据：两点之间的**逆向幅度**必须 ≥ SWING_MULT × 该票自身的中位 bar 振幅。
# **必须用相对量** —— 固定 3% 会把低波票彻底静音（实测 515180/600900/601318/
# 601628/600089/515120 六只信号归零）。
# 取 2.5 是实测的最小可行值（1.5/2.0 挡不住 300475 那对，3.0 会多砍 3 个显示信号）。
SWING_MULT = 2.5

# 顶背离用**更严**的摆动门槛（2026-09-20 扩池 974 只 × 540 天，57,045 事件扫出来的）。
#
# 阈值扫描（训练/测试分开，`w35_divergence/sweep_divergence_thresholds.py`）显示：
#   顶背离 —— 提升在**整条阈值曲线**上都显著（P30→P90），可放心落地：
#       30min 测试 +5.4~+8.7pp(z 3.8~5.4)；60min 测试 +4.7~+10.5pp(z 3.7~5.4)
#       且 swing_depth 在**低分位最好** ⇒ 抬到 4x 即够，再往上样本流失快、收益递减
#   底背离 —— **不加**：60min 底背离上训练 +9.8~+12.6pp 但**测试 ≈0 甚至为负**
#       （教科书级 train/test 落差），30min 底也只在高分位才勉强显著。
#   ⇒ 故本参数**只作用于顶背离**；底背离仍用 SWING_MULT。
SWING_MULT_TOP = 4.0

# 日线口径与分钟**不同**：日线背离更稀疏（39 只实测 min=3 / P50=20 根，
# ≤5 交易日仅 13% 的票有事件），"N 天内有没有事件"这种状态式判据对提醒毫无用处
# —— 提醒要的是「**新形成**」而非「最近有过」。
#   ⇒ 日线走**事件式**：只在事件刚被确认的头几天报（bars_ago<=4），
#      再由调用方按事件时间戳去重，保证每个事件只提醒一次。
DAILY_ALERT_MAX_AGE_BARS = 4    # 事件确认后 4 个交易日内视为"新"，用于飞书提醒
DAILY_DISPLAY_MAX_AGE_BARS = 20 # 若将来要在 GUI 展示日线背离，用这个更宽的口径


def _ts_code(code: str) -> str:
    base = str(code).split("_")[0]
    return (base + ".SH") if base[0] in "56" else (base + ".SZ")


def fetch_freq_kline(code: str, freq: str = "60min", days: int = FETCH_DAYS) -> pd.DataFrame:
    """tushare 拉近 days 日 30/60 分钟线，当日缓存。返回 {time, open, high, low, close, volume}。
    生产默认 days=35：30min 走 1min 聚合保持口径；长历史(days>35)30min 用原生数据，
    因为 1min 单次拉取有 8000 条上限(90 天会被截断)。缓存文件名按 days 区分。"""
    ts_code = _ts_code(code)
    cache_key = f"{ts_code}_{freq}" if days == FETCH_DAYS else f"{ts_code}_{freq}_d{days}"
    fp = CACHE_DIR / f"{cache_key}.json"
    today = datetime.now().strftime("%Y-%m-%d")
    if fp.exists():
        try:
            cached = json.loads(fp.read_text(encoding="utf-8"))
            if cached.get("date") == today and cached.get("rows"):
                df = pd.DataFrame(cached["rows"])
                df["time"] = pd.to_datetime(df["time"])
                return df
        except Exception:
            pass
    try:
        from analysis.index_regime_intraday import _iri_tushare_pro
        pro = _iri_tushare_pro()
        start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        ts_freq = "1min" if (freq == "30min" and days == FETCH_DAYS) else freq
        df = pro.stk_mins(ts_code=ts_code, freq=ts_freq,
                          start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume"})
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)
    keep = [c for c in ("time", "open", "close", "high", "low", "volume", "amount") if c in df.columns]
    df = df[keep]
    # 聚合到目标分辨率（30min 生产默认用 1min 聚合；其余用原生+幂等重采样）
    if freq == "30min" and ts_freq == "1min":
        df = _resample_minutes(df, "30min")
    else:
        df = _resample_minutes(df, freq)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _rows = df.copy()
        _rows["time"] = _rows["time"].astype(str)  # json 不能序列化 Timestamp
        fp.write_text(json.dumps({"date": today, "rows": _rows.to_dict(orient="records")},
                                 ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return df


def _resample_minutes(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["_tb"] = df["time"].dt.floor(freq)
    agg = df.groupby("_tb").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "volume": "sum", "amount": "sum",
    }).reset_index()
    return agg.rename(columns={"_tb": "time"})


def _macd_dif(closes) -> np.ndarray:
    c = pd.Series(closes, dtype=float)
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    return (e12 - e26).values


def _local_extrema(highs, lows, n_bars=3):
    peaks, troughs = [], []
    for i in range(n_bars, len(highs) - n_bars):
        if all(highs[i] >= highs[i - j] for j in range(1, n_bars + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, n_bars + 1)):
            peaks.append(i)
        if all(lows[i] <= lows[i - j] for j in range(1, n_bars + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, n_bars + 1)):
            troughs.append(i)

    def _merge(idxs, vals, keep_max):
        """合并相邻极值（间距≤4 视为同一极值，保留更极端者），避免相邻等高峰误判。"""
        if not idxs:
            return []
        out = [idxs[0]]
        for i in idxs[1:]:
            if i - out[-1] <= 4:
                if (keep_max and vals[i] > vals[out[-1]]) or (not keep_max and vals[i] < vals[out[-1]]):
                    out[-1] = i
            else:
                out.append(i)
        return out

    return _merge(peaks, highs, True), _merge(troughs, lows, False)


def detect_divergence_events(df: pd.DataFrame, price_excess: float = None,
                             require_dif_zone: bool = None,
                             swing_mult: float = None,
                             swing_mult_top: float = None) -> list:
    """检测单分辨率 K 线全部顶/底背离事件。
    返回 [{index, time, type('顶'/'底'), price, dif, consec, bars_ago}]；事件记在最新峰/谷上。
    consec=True 表示该峰/谷与前一个峰/谷形成连续同向背离（验证显示 60min 连续底背离有区分度）。

    两道门槛（2026-09-20 加，对齐 同花顺/通达信 口径）：

    1. `price_excess`（默认 0.3%）——**"创新高/新低"的幅度门槛**，与 t0_schemes/
       run_experiment_v2 的 BUF 同源。没有它时，下跌途中两个相邻微反弹峰只要高出 0.1%
       就被判顶背离（实测 002202：18.55 vs 18.52 仅高 0.16%，真实顶在 19.58）。
    2. `require_dif_zone`（默认 True）——**背离必须发生在对应价区**：
       顶背离要求后一峰 DIF > 0（在零轴上方才叫"顶"），底背离要求 DIF < 0。
       没有它时，**下跌趋势中 DIF 深负区的小反弹也会被标成"顶背离"**——这正是
       owner 报的 002202 情形（两峰 DIF 均为负：-0.0668 / -0.2107）。

    ⚠️ `require_dif_zone=True` 会**显著减少事件数**（实测 9 只 30min：603667 3→0、
    002451 5→2、002261 5→3）。这会影响"60min 连续底背离"这一**唯一已验证信号**的
    样本口径，若要引用其 +12.5pp 结论需按新口径重跑验证（validate_divergence.py）。"""
    if df is None or df.empty or len(df) < 40:
        return []
    # ⚠️ 用 None 哨兵而非直接把模块常量写成默认值：默认值在 **import 时求值一次**，
    # 写成 `price_excess=PRICE_EXCESS` 会让常量**运行时改不动** ——
    # 2026-09-20 就因此静默算错了一次 A/B（两次运行结果完全相同才发现）。
    if price_excess is None:
        price_excess = PRICE_EXCESS
    if require_dif_zone is None:
        require_dif_zone = REQUIRE_DIF_ZONE
    if swing_mult is None:
        swing_mult = SWING_MULT
    if swing_mult_top is None:
        swing_mult_top = SWING_MULT_TOP
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    times = df["time"].values
    dif = _macd_dif(closes)
    peaks, troughs = _local_extrema(highs, lows)
    # 摆动门槛（相对该票自身波动）：两个极值之间的逆向幅度不足 `swing_min` 时，
    # 它们属于同一段走势，不构成"两个高点/低点"⇒ 不作背离比对。
    # ⚠️ 顶/底用**不同**门槛：顶更严（见 SWING_MULT_TOP 的标定说明），底维持宽松。
    _med_bar = float(np.median((highs - lows) / closes))
    swing_min_top = swing_mult_top * _med_bar if swing_mult_top > 0 else 0.0
    swing_min = swing_mult * _med_bar if swing_mult > 0 else 0.0
    events = []
    peak_events, trough_events = {}, {}
    for i in range(1, len(peaks)):
        p2, p1 = peaks[i - 1], peaks[i]
        if (highs[p1] > highs[p2] * (1 + price_excess) and dif[p1] < dif[p2]
                and (not require_dif_zone or dif[p1] > 0)
                and (swing_min_top <= 0 or lows[p2:p1 + 1].min() <= highs[p1] * (1 - swing_min_top))):
            e = {"index": int(p1), "time": str(times[p1]),
                 "type": "顶", "price": float(highs[p1]), "dif": float(dif[p1]), "consec": False}
            events.append(e)
            peak_events[p1] = e
    for i in range(1, len(troughs)):
        t2, t1 = troughs[i - 1], troughs[i]
        if (lows[t1] < lows[t2] * (1 - price_excess) and dif[t1] > dif[t2]
                and (not require_dif_zone or dif[t1] < 0)
                and (swing_min <= 0 or highs[t2:t1 + 1].max() >= lows[t1] * (1 + swing_min))):
            e = {"index": int(t1), "time": str(times[t1]),
                 "type": "底", "price": float(lows[t1]), "dif": float(dif[t1]), "consec": False}
            events.append(e)
            trough_events[t1] = e
    events.sort(key=lambda e: e["index"])
    peak_pos = {p: i for i, p in enumerate(peaks)}
    trough_pos = {t: i for i, t in enumerate(troughs)}
    for e in events:
        if e["type"] == "顶":
            pos = peak_pos.get(e["index"])
            e["consec"] = bool(pos is not None and pos >= 1 and peaks[pos - 1] in peak_events)
        else:
            pos = trough_pos.get(e["index"])
            e["consec"] = bool(pos is not None and pos >= 1 and troughs[pos - 1] in trough_events)
        # 距今 bar 数：事件在窗口里有多旧（0=最后一根）。供新鲜度过滤与前端展示。
        e["bars_ago"] = int(len(closes) - 1 - e["index"])
    return events


def _latest_fresh(events: list, max_age_bars: int):
    """窗口内最新的、且未过期（bars_ago <= max_age_bars）的事件；无则 None。
    修正前一律取 events[-1]，会把数周前的背离当成当前信号。"""
    fresh = [e for e in events if e.get("bars_ago", 0) <= max_age_bars]
    return fresh[-1] if fresh else None


def detect_minute_divergence_detail(code: str, max_age_bars: dict = None) -> dict:
    """检测个股 30/60 分钟线背离详情（含连续标记与新鲜度）。
    返回 {m30: {type, consec, bars_ago, time, price, dif}, m60: {...}}；过期项不返回。

    ⚠️ 2026-09-19 修正：此前取 events[-1] 不看年龄，会把数周前的背离当当前信号
    （实测 300153 的 30min 底背离距今 24 天仍被展示）→ 现按 MAX_AGE_BARS 过滤。

    验证结论（2026-08-19，180天）：单次背离命中率≈随机基线；60min 连续底背离是唯一可信正向信号。"""
    age_map = max_age_bars or MAX_AGE_BARS
    out = {}
    for freq, key in (("30min", "m30"), ("60min", "m60")):
        try:
            df = fetch_freq_kline(code, freq)
            if df is None or df.empty or len(df) < 40:
                continue
            events = detect_divergence_events(df)
            _max_age = age_map.get(freq)
            if _max_age is None:                       # 调用方传了不完整的 map
                _max_age = MAX_AGE_BARS.get(freq, 20)
            last = _latest_fresh(events, _max_age)
            if last is None:
                continue
            out[key] = {"type": "顶背离" if last["type"] == "顶" else "底背离",
                        "consec": bool(last.get("consec", False)),
                        "bars_ago": int(last.get("bars_ago", 0)),
                        "time": str(last.get("time", "")),
                        "price": last.get("price"),
                        "dif": last.get("dif")}
        except Exception:
            continue
    return out


def detect_daily_divergence(df_daily: pd.DataFrame,
                            max_age_bars: int = DAILY_ALERT_MAX_AGE_BARS) -> dict:
    """日线 MACD 背离（复用 _macd_dif / _local_extrema，无周期假设）。
    df_daily 需含 time|date / high / low / close 列（core.market_data provider 口径）。
    返回 {type, consec, bars_ago, time, price, dif}；无新鲜事件返回 {}。

    ⚠️ 验证状态：本项目只证过 ①30/60min 单次背离≈随机基线 ②日线**复合**顶背离无效
    ③60min 连续底背离仅在"事件后止跌"口径有效。**日线单次 MACD 背离本身未验证** →
    下游文案必须写"参考/未验证"，不得表述为买卖信号。"""
    if df_daily is None or df_daily.empty or len(df_daily) < 40:
        return {}
    df = df_daily.copy()
    if "time" not in df.columns:
        for cand in ("date", "trade_date", "datetime"):
            if cand in df.columns:
                df = df.rename(columns={cand: "time"})
                break
    if "time" not in df.columns or not {"high", "low", "close"}.issubset(df.columns):
        return {}
    events = detect_divergence_events(df)
    last = _latest_fresh(events, max_age_bars)
    if last is None:
        return {}
    return {"type": "顶背离" if last["type"] == "顶" else "底背离",
            "consec": bool(last.get("consec", False)),
            "bars_ago": int(last.get("bars_ago", 0)),
            "time": str(last.get("time", "")),
            "price": last.get("price"),
            "dif": last.get("dif")}


def detect_divergence_df(df: pd.DataFrame) -> str:
    """对单个分辨率 K 线判定背离。返回 '顶背离' / '底背离' / None。"""
    if df is None or df.empty or len(df) < 40:  # MACD(26) 预热 + 峰谷
        return None
    closes = df["close"].astype(float).values
    highs = df["high"].astype(float).values
    lows = df["low"].astype(float).values
    dif = _macd_dif(closes)
    peaks, troughs = _local_extrema(highs, lows)
    if len(peaks) >= 2:
        p2, p1 = peaks[-2], peaks[-1]
        if highs[p1] > highs[p2] and dif[p1] < dif[p2]:
            return "顶背离"
    if len(troughs) >= 2:
        t2, t1 = troughs[-2], troughs[-1]
        if lows[t1] < lows[t2] and dif[t1] > dif[t2]:
            return "底背离"
    return None


def detect_minute_divergence(code: str) -> dict:
    """检测个股 30/60 分钟线顶/底背离。返回 {m30: '顶背离'/'底背离'/None, m60: ...}。"""
    out = {}
    for freq, key in (("30min", "m30"), ("60min", "m60")):
        try:
            df = fetch_freq_kline(code, freq)
            d = detect_divergence_df(df)
            if d:
                out[key] = d
        except Exception:
            continue
    return out


def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="30/60分钟线背离检测")
    ap.add_argument("--code", required=True)
    args = ap.parse_args()
    r = detect_minute_divergence(args.code)
    print(f"{args.code}: 30分={r.get('m30', '无')} 60分={r.get('m60', '无')}")


if __name__ == "__main__":
    _cli()
