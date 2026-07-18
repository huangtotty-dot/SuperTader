# -*- coding: utf-8 -*-
"""
index_regime_intraday.py — 大盘分时辅助模块（盘中预警 + 回测分时双通道）

所属体系: E:\06_T\ 做T量化系统 · 大盘态势判定体系（分时/分钟线部分）
配合日线核心模块 index_regime.py 使用；本模块独立实现、独立运行，不 import 宿主文件。

============================ 数据源实测结论 ============================
实测时间: 2026-07-17 收盘后 @ Python 3.11.9 / pandas 3.0.3 / akshare 1.18.60 / tushare 1.4.29

【live 实盘通道】（盘中实时分钟线，主用 a，失败回落 b）
  a) 任务指定接口 ak.stock_zh_index_hist_min_em —— akshare 1.18.60 中【不存在】
     (AttributeError: module 'akshare' has no attribute 'stock_zh_index_hist_min_em')。
     东财系候选 index_zh_a_hist_min_em / stock_zh_index_spot_em —— 本机 HTTPS 连接
     *.push2.eastmoney.com 全部 SSLError，不可用。
     实测可用的 akshare 替代接口: ak.stock_zh_a_minute(symbol="sh000001", period="1")
       · 新浪源 1 分钟线，列: day/open/high/low/close/volume/amount，时间升序
       · 返回最近约 8 个交易日（1970 行级），live 模式筛出最后交易日当日段
       · volume 单位=股，amount 单位=元
       · 注意: 个别分钟偶发缺失（实测 2026-07-17 缺 14:59），检测一律按时间窗过滤
     —— 本模块 live【主通道】。
  b) 腾讯当日分时 https://ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001
       · 实测可用。结构: data[code]["data"]["data"] = ["0930 3865.32 3969215 8068133963.70", ...]
         即 "HHMM 分时价 累计量(手) 累计额(元)"，累计值需【差分】还原每分钟量；
         data[code]["data"]["date"] = "20260717" 为当日日期
       · 无 OHLC（分时价），open=high=low=close=分时价；volume 手→股 需 *100
     —— 本模块 live【回落通道】。

【backtest 回测通道】（tushare 历史分钟线）
  tushare pro.stk_mins(ts_code="000001.SH", freq="5min",
                       start_date="2026-07-17 09:00:00", end_date="2026-07-17 19:00:00")
    · 已验证可用；列: ts_code/trade_time/close/open/high/low/vol/amount
    · 返回【倒序】（最新在前），需反转升序；vol 单位=股，amount 单位=元
    · bar 时间戳为该 bar 结束时间（首根 09:30 含集合竞价，末根 15:00）
    · 单日行数: 5min=49 根 / 1min=241 根，远低于单次 8000 行上限；多日按日循环
    · 指数代码: 上证指数 000001.SH / 深证成指 399001.SZ（均已实测）

【工程约定】
  · 风格B: 顶部集中 import，可独立运行 / 可标准 import；
    顶层不定义 PARAMS / log / _now / fetch_minute_bar；顶层名字用 iri_/IRI_ 前缀或收进函数
  · 时间源: _iri_now = globals().get("_now") or datetime.now（宿主注入 _now 时沿用）
  · 全部可调参数集中在 IRI_DEFAULT_PARAMS
  · 两个通道输出统一列结构: time,open,high,low,close,volume,amount（time 为
    "YYYY-MM-DD HH:MM:SS" 字符串，全表按 time 升序），供同一个 detect_intraday_alert 消费
  · vol_ratio_vs_5d（前 5 日同时段均量比）只有 backtest 通道算得出：
    fetch_index_minutes_backtest 将其写入返回 DataFrame 的
    attrs["prev5d_avg_vol_same_period"]，detect 读取；live 通道没有则置 None 并记入 degraded
=======================================================================
"""
import argparse
import json
import os
import time as _iri_time_mod
import urllib.request
from datetime import datetime, timedelta

import pandas as pd

# 时间源：宿主项目若注入全局 _now 则沿用，否则用系统 datetime.now
_iri_now = globals().get("_now") or datetime.now

# ---------------------------------------------------------------------------
# 集中参数（全部可调）
# ---------------------------------------------------------------------------
IRI_DEFAULT_PARAMS = {
    # ---- 预警规则阈值 ----
    "window_minutes": 30,          # I1/I2 回看窗口（分钟）
    "i1_drop_pct": -0.8,           # I1 急跌阈值（%），近30分钟跌幅 <= 此值 → warn
    "i2_rise_pct": 0.8,            # I2 急涨阈值（%），近30分钟涨幅 >= 此值 → info
    "daily_low_lookback": 20,      # I3 日线最低价回看天数
    "i4_conflict_pct": 1.5,        # I4 状态冲突盘中涨/跌幅阈值（%，相对当日开盘）
    "i5_tail_time": "14:30",       # I5 尾盘起始时刻（HH:MM，含）
    "i5_tail_move_pct": 0.5,       # I5 尾盘单向波动阈值（%）
    # ---- live 通道 ----
    "live_primary": "akshare_sina",        # 主通道: ak.stock_zh_a_minute（新浪源）
    "live_fallback": "tencent_minute",     # 回落通道: 腾讯当日分时
    "http_timeout": 10,                    # HTTP 超时（秒）
    "http_retry": 1,                       # 每通道失败额外重试次数
    "http_retry_sleep": 0.6,               # 重试间隔（秒）
    # ---- backtest 通道（tushare）----
    "tushare_token_env": "TUSHARE_TOKEN",  # token 环境变量名
    # 环境变量取不到时的回落 token（任务书提供，已实测可用）
    "tushare_token_fallback": "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def",
    "prev_trade_days": 5,                  # vol_ratio 前 N 个交易日
    "prev_calendar_days": 14,              # 向前扫描的自然日上限（覆盖周末/小长假）
    "tushare_sleep": 0.25,                 # tushare 连续调用间隔（秒），防限流
    # ---- CLI 演示用日线（I3）----
    "daily_bars_calendar_days": 60,        # CLI 拉日线时向前推的自然日数
}

# 统一输出列结构（两个通道一致）
_IRI_STD_COLUMNS = ["time", "open", "high", "low", "close", "volume", "amount"]


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _iri_std_df(df: "pd.DataFrame") -> "pd.DataFrame":
    """规整为标准列结构 + 时间升序 + 数值列 float。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=_IRI_STD_COLUMNS)
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = df["time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return df[_IRI_STD_COLUMNS]


def _iri_http_get_json(url: str) -> dict:
    """带浏览器头的 HTTP GET → JSON。异常向上抛，由调用方计入 degraded。"""
    p = IRI_DEFAULT_PARAMS
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.qq.com/",
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=p["http_timeout"]) as resp:
        content = resp.read().decode("utf-8", errors="ignore")
    if not content.strip() or "<html" in content.lower() or "<!doctype html" in content.lower():
        raise ValueError("http response not json")
    return json.loads(content)


# ---------------------------------------------------------------------------
# live 通道 a：akshare 新浪源 1 分钟线（主）
#   实测: ak.stock_zh_a_minute(symbol="sh000001", period="1")
#   返回最近多日升序数据，列 day/open/high/low/close/volume/amount；筛最后交易日当日段
# ---------------------------------------------------------------------------
def _iri_fetch_live_akshare_sina(code: str) -> "pd.DataFrame":
    import akshare as ak  # 延迟 import：仅用 backtest 通道时不强制依赖 akshare

    df = ak.stock_zh_a_minute(symbol=code, period="1")
    if df is None or df.empty:
        raise ValueError("akshare stock_zh_a_minute empty")
    df = df.rename(columns={"day": "time"})
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    # 只保留最后一个交易日（live 语义 = 当日盘中）
    last_day = df["time"].dt.strftime("%Y-%m-%d").max()
    df = df[df["time"].dt.strftime("%Y-%m-%d") == last_day]
    if df.empty:
        raise ValueError("akshare stock_zh_a_minute no today rows")
    return _iri_std_df(df)


# ---------------------------------------------------------------------------
# live 通道 b：腾讯当日分时（回落）
#   实测: https://ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001
#   rows: ["HHMM price cum_vol(手) cum_amount(元)", ...] → 差分还原每分钟量
# ---------------------------------------------------------------------------
def _iri_fetch_live_tencent(code: str) -> "pd.DataFrame":
    url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    data = _iri_http_get_json(url)
    node = (data.get("data", {}) or {}).get(code) or {}
    pack = node.get("data") or {}
    if isinstance(pack, list):  # 兼容个别返回形态
        rows, day_str = pack, ""
    else:
        rows = pack.get("data") or []
        day_str = str(pack.get("date") or "")
    if not rows:
        raise ValueError("tencent minute rows empty")
    if not day_str:
        day_str = _iri_now().strftime("%Y%m%d")
    day_fmt = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:8]}"

    parsed = []
    prev_cum_vol, prev_cum_amt = 0.0, 0.0
    for row in rows:
        parts = row.split() if isinstance(row, str) else [str(x) for x in row]
        if len(parts) < 4:
            continue
        hm = str(parts[0]).strip().zfill(4)          # "0930" → 09:30
        price = float(parts[1])
        cum_vol = float(parts[2])                    # 累计量（手）
        cum_amt = float(parts[3])                    # 累计额（元）
        vol = max(cum_vol - prev_cum_vol, 0.0) * 100  # 差分 + 手→股
        amt = max(cum_amt - prev_cum_amt, 0.0)
        prev_cum_vol, prev_cum_amt = cum_vol, cum_amt
        parsed.append({
            "time": f"{day_fmt} {hm[:2]}:{hm[2:]}:00",
            "open": price, "high": price, "low": price, "close": price,
            "volume": vol, "amount": amt,
        })
    if not parsed:
        raise ValueError("tencent minute parsed empty")
    return _iri_std_df(pd.DataFrame(parsed))


def fetch_index_minutes_live(code: str = "sh000001") -> "pd.DataFrame":
    """盘中实时分钟线（live 通道）。

    主用 akshare 新浪源 1 分钟线，失败回落腾讯当日分时。
    返回标准列结构 DataFrame（time,open,high,low,close,volume,amount，升序）；
    实际使用的数据源与降级信息写入 df.attrs["iri_source"] / df.attrs["iri_degraded"]。
    code 形如 sh000001（上证）/ sz399001（深证成指），两个子通道通用。
    """
    p = IRI_DEFAULT_PARAMS
    degraded = []
    channels = [
        (p["live_primary"], _iri_fetch_live_akshare_sina),
        (p["live_fallback"], _iri_fetch_live_tencent),
    ]
    last_err = ""
    for name, fn in channels:
        for attempt in range(p["http_retry"] + 1):
            try:
                df = fn(code)
                if df is None or df.empty:
                    raise ValueError("empty dataframe")
                df.attrs["iri_source"] = name
                df.attrs["iri_degraded"] = list(degraded)
                return df
            except Exception as e:  # noqa: BLE001 - 降级链需要吞掉单通道异常
                last_err = f"{type(e).__name__}: {str(e)[:120]}"
                if attempt < p["http_retry"]:
                    _iri_time_mod.sleep(p["http_retry_sleep"])
        degraded.append(f"live:{name}_failed({last_err})")
    raise RuntimeError(f"live 通道全部失败: {'; '.join(degraded)}")


# ---------------------------------------------------------------------------
# backtest 通道：tushare 历史分钟线
#   实测: pro.stk_mins(ts_code="000001.SH", freq="5min", start_date=..., end_date=...)
#   倒序返回 → 反转升序；单日一拉（5min=49 行 << 8000 行上限），多日按日循环
# ---------------------------------------------------------------------------
_IRI_TUSHARE_PRO_CACHE = {}


def _iri_tushare_pro():
    """构造（并缓存）tushare pro 句柄；token 读环境变量，取不到回落内置值。"""
    if "pro" not in _IRI_TUSHARE_PRO_CACHE:
        import tushare as ts  # 延迟 import：仅用 live 通道时不强制依赖 tushare

        p = IRI_DEFAULT_PARAMS
        token = os.environ.get(p["tushare_token_env"]) or p["tushare_token_fallback"]
        _IRI_TUSHARE_PRO_CACHE["pro"] = ts.pro_api(token)
    return _IRI_TUSHARE_PRO_CACHE["pro"]


def _iri_fetch_stk_mins_one_day(ts_code: str, date: str, freq: str) -> "pd.DataFrame":
    """拉取单日指数分钟线并标准化（升序）。date 格式 YYYY-MM-DD。

    实测 tushare 经本地代理（127.0.0.1:7890）偶发 ReadTimeout，加轻量重试。
    """
    p = IRI_DEFAULT_PARAMS
    pro = _iri_tushare_pro()
    last_err = None
    for attempt in range(p["http_retry"] + 2):  # 默认共 3 次
        try:
            df = pro.stk_mins(ts_code=ts_code, freq=freq,
                              start_date=f"{date} 09:00:00", end_date=f"{date} 19:00:00")
            break
        except Exception as e:  # noqa: BLE001 - 网络抖动重试
            last_err = e
            if attempt < p["http_retry"] + 1:
                _iri_time_mod.sleep(max(p["http_retry_sleep"], 1.0))
    else:
        raise last_err
    if df is None or df.empty:
        return pd.DataFrame(columns=_IRI_STD_COLUMNS)
    df = df.rename(columns={"trade_time": "time", "vol": "volume"})
    return _iri_std_df(df)


def fetch_index_minutes_backtest(ts_code: str = "000001.SH", date: str = "2026-07-17",
                                 freq: str = "5min") -> "pd.DataFrame":
    """历史分钟线（backtest 通道）。

    返回指定交易日标准列结构 DataFrame（升序）；并向前逐日循环拉取前 N 个交易日
    （默认 5，自然日窗口默认 14 天），计算【同时段累计成交量均值】写入
    df.attrs["prev5d_avg_vol_same_period"]，供 detect_intraday_alert 计算 vol_ratio_vs_5d。
    降级信息写入 df.attrs["iri_degraded"]。
    """
    p = IRI_DEFAULT_PARAMS
    degraded = []

    day_df = _iri_fetch_stk_mins_one_day(ts_code, date, freq)
    if day_df.empty:
        raise RuntimeError(f"backtest 通道无数据: {ts_code} {date} {freq}")

    # ---- 前 N 个交易日同时段均量（供 vol_ratio_vs_5d）----
    cutoff_hm = day_df["time"].iloc[-1][11:16]  # 当日最后一根 bar 的 HH:MM（"同时段"截止）
    prev_sums = []
    base = datetime.strptime(date, "%Y-%m-%d")
    for i in range(1, p["prev_calendar_days"] + 1):
        if len(prev_sums) >= p["prev_trade_days"]:
            break
        d = (base - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            _iri_time_mod.sleep(p["tushare_sleep"])
            pdf = _iri_fetch_stk_mins_one_day(ts_code, d, freq)
        except Exception as e:  # noqa: BLE001
            degraded.append(f"backtest:prev_day {d} failed({type(e).__name__}: {str(e)[:80]})")
            continue
        if pdf.empty:
            continue  # 周末/节假日
        same_period = pdf[pdf["time"].str.slice(11, 16) <= cutoff_hm]
        prev_sums.append(float(same_period["volume"].sum()))
    if len(prev_sums) >= p["prev_trade_days"]:
        day_df.attrs["prev5d_avg_vol_same_period"] = sum(prev_sums) / len(prev_sums)
    else:
        day_df.attrs["prev5d_avg_vol_same_period"] = None
        degraded.append(f"backtest:prev_days_only_{len(prev_sums)}(<{p['prev_trade_days']})")

    day_df.attrs["iri_source"] = "tushare_stk_mins"
    day_df.attrs["iri_degraded"] = degraded
    return day_df


# ---------------------------------------------------------------------------
# 统一判定函数（live / backtest 双通道消费）
# ---------------------------------------------------------------------------
def detect_intraday_alert(minute_bars, daily_regime: str = "range",
                          daily_score: float = 0.0, daily_bars=None) -> dict:
    """盘中分时预警判定。

    参数:
      minute_bars  : 标准列结构分钟线 DataFrame（time,open,high,low,close,volume,amount 升序），
                     live / backtest 通道输出均可；读取其 attrs 中的
                     iri_degraded / prev5d_avg_vol_same_period（可选）。
      daily_regime : 日线态势（uni_up / uni_down / range ...），供 I4 状态冲突判定。
      daily_score  : 日线综合评分（预留，当前规则未直接使用，透传进 snapshot 便于联调）。
      daily_bars   : 日线 DataFrame（需含 low 列；调用方负责裁剪到当日之前，
                     backtest 场景严禁含未来数据），供 I3 盘中破位判定。

    返回: {alerts:[{tag,level,msg}], snapshot:{last,chg_pct,high,low,vwap,
           vol_ratio_vs_5d,time}, degraded:[...]}
    """
    p = IRI_DEFAULT_PARAMS
    alerts, degraded = [], []

    df = minute_bars.copy() if minute_bars is not None else pd.DataFrame()
    degraded.extend(getattr(minute_bars, "attrs", {}).get("iri_degraded", []) or [])
    if df.empty:
        return {"alerts": [], "snapshot": {}, "degraded": degraded + ["empty_minute_bars"]}

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    last_row = df.iloc[-1]
    last_close = float(last_row["close"])
    last_time = last_row["time"]
    day_open = float(df.iloc[0]["open"])
    chg_pct = (last_close / day_open - 1.0) * 100.0 if day_open else 0.0  # 相对当日开盘

    # ---- I1 急跌 / I2 急涨：近 window_minutes 分钟单向波动 ----
    win_start = last_time - timedelta(minutes=p["window_minutes"])
    win = df[df["time"] >= win_start]
    if len(win) >= 2:
        ref_price = float(win.iloc[0]["close"])  # 窗口内最早 bar ≈ 30 分钟前价格
        move_pct = (last_close / ref_price - 1.0) * 100.0 if ref_price else 0.0
        if move_pct <= p["i1_drop_pct"]:
            alerts.append({"tag": "I1", "level": "warn",
                           "msg": f"急跌: 近{p['window_minutes']}分钟 {move_pct:+.2f}% "
                                  f"(阈值 {p['i1_drop_pct']:+.1f}%)"})
        elif move_pct >= p["i2_rise_pct"]:
            alerts.append({"tag": "I2", "level": "info",
                           "msg": f"急涨: 近{p['window_minutes']}分钟 {move_pct:+.2f}% "
                                  f"(阈值 {p['i2_rise_pct']:+.1f}%)"})
    else:
        degraded.append("I1_I2:insufficient_window_bars")

    # ---- I3 盘中破位：现价 < 近 N 日日线最低价（需 daily_bars，且应由调用方裁到当日之前）----
    if daily_bars is not None and len(daily_bars) > 0:
        try:
            low_col = "low" if "low" in daily_bars.columns else daily_bars.columns[
                [str(c).lower() for c in daily_bars.columns].index("low")]
            low_n = float(pd.to_numeric(
                daily_bars[low_col].tail(p["daily_low_lookback"]), errors="coerce").min())
            if last_close < low_n:
                alerts.append({"tag": "I3", "level": "alert",
                               "msg": f"盘中破位: 现价 {last_close:.2f} < "
                                      f"近{p['daily_low_lookback']}日最低 {low_n:.2f}"})
        except Exception as e:  # noqa: BLE001
            degraded.append(f"I3:daily_bars_parse_failed({type(e).__name__})")
    else:
        degraded.append("I3:no_daily_bars")

    # ---- I4 状态冲突：日线趋势 vs 盘中反向剧烈波动（相对当日开盘）----
    th = p["i4_conflict_pct"]
    if daily_regime == "uni_up" and chg_pct <= -th:
        alerts.append({"tag": "I4", "level": "alert",
                       "msg": f"趋势破坏预警: 日线 uni_up 但盘中 {chg_pct:+.2f}% "
                              f"(<= {-th:+.1f}%)"})
    elif daily_regime == "uni_down" and chg_pct >= th:
        alerts.append({"tag": "I4", "level": "info",
                       "msg": f"反弹预警: 日线 uni_down 但盘中 {chg_pct:+.2f}% "
                              f"(>= {th:+.1f}%)"})

    # ---- I5 尾盘异动：最后 bar >= 14:30 且 14:30 后单向波动超阈值 ----
    tail_hm = p["i5_tail_time"]
    if last_time.strftime("%H:%M") >= tail_hm:
        pre_tail = df[df["time"].dt.strftime("%H:%M") < tail_hm]
        tail = df[df["time"].dt.strftime("%H:%M") >= tail_hm]
        if len(pre_tail) >= 1 and len(tail) >= 1:
            base_price = float(pre_tail.iloc[-1]["close"])  # 14:30 前最后一根收盘为基准
            if base_price:
                up_pct = (float(tail["close"].max()) / base_price - 1.0) * 100.0
                dn_pct = (float(tail["close"].min()) / base_price - 1.0) * 100.0
                if up_pct >= p["i5_tail_move_pct"]:
                    alerts.append({"tag": "I5", "level": "info",
                                   "msg": f"尾盘拉升: {tail_hm} 后最大涨幅 {up_pct:+.2f}%"})
                elif dn_pct <= -p["i5_tail_move_pct"]:
                    alerts.append({"tag": "I5", "level": "warn",
                                   "msg": f"尾盘跳水: {tail_hm} 后最大跌幅 {dn_pct:+.2f}%"})
        else:
            degraded.append("I5:no_tail_window_bars")
    else:
        degraded.append("I5:not_tail_time")

    # ---- snapshot ----
    vol_sum = float(df["volume"].sum())
    amt_sum = float(df["amount"].sum())
    vwap = amt_sum / vol_sum if vol_sum > 0 else None  # Σamount/Σvolume（两通道均为 元/股）
    prev5_avg = getattr(minute_bars, "attrs", {}).get("prev5d_avg_vol_same_period")
    if prev5_avg:
        vol_ratio = vol_sum / float(prev5_avg)
    else:
        vol_ratio = None
        degraded.append("vol_ratio_vs_5d:unavailable")
    snapshot = {
        "last": round(last_close, 3),
        "chg_pct": round(chg_pct, 3),          # 相对当日开盘（%）
        "high": round(float(df["high"].max()), 3),
        "low": round(float(df["low"].min()), 3),
        "vwap": round(vwap, 3) if vwap is not None else None,
        "vol_ratio_vs_5d": round(vol_ratio, 3) if vol_ratio is not None else None,
        "time": last_time.strftime("%Y-%m-%d %H:%M:%S"),
        "daily_regime": daily_regime,
        "daily_score": daily_score,
    }
    return {"alerts": alerts, "snapshot": snapshot, "degraded": degraded}


# ---------------------------------------------------------------------------
# CLI 辅助：拉日线供 I3 演示（调用方视角示例；backtest 时裁剪到 date 之前，防未来数据）
# ---------------------------------------------------------------------------
def _iri_fetch_daily_bars_for_cli(ts_code: str, before_date: str):
    """CLI 演示用日线（供 I3）。返回 (DataFrame 升序含 low 列, source 名)。

    优先 tushare pro.index_daily；实测本 token 无 index_daily 权限，
    回落 akshare 新浪日线 ak.stock_zh_index_daily（实测可用）。
    统一裁剪到 before_date 之前（backtest 防未来数据）。
    """
    p = IRI_DEFAULT_PARAMS
    cutoff = (datetime.strptime(before_date, "%Y-%m-%d") - timedelta(days=1))
    # a) tushare index_daily（本 token 实测无权限，留作有权限环境的首选）
    try:
        pro = _iri_tushare_pro()
        start = (cutoff - timedelta(days=p["daily_bars_calendar_days"])).strftime("%Y%m%d")
        df = pro.index_daily(ts_code=ts_code, start_date=start,
                             end_date=cutoff.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            return df.sort_values("trade_date").reset_index(drop=True), "tushare_index_daily"
    except Exception:
        pass  # 无权限/网络异常 → 回落新浪
    # b) akshare 新浪日线
    import akshare as ak
    symbol = ("sh" if ts_code.endswith(".SH") else "sz") + ts_code.split(".")[0]
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or df.empty:
        raise ValueError("sina daily empty")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df[df["date"] <= pd.Timestamp(cutoff.strftime("%Y-%m-%d"))]
    df = df.sort_values("date").reset_index(drop=True)
    if df.empty:
        raise ValueError("sina daily empty after cutoff")
    return df, "akshare_sina_daily"


def _iri_main() -> None:
    parser = argparse.ArgumentParser(description="大盘分时辅助模块（盘中预警 + 回测分时双通道）")
    parser.add_argument("--mode", choices=["live", "backtest"], required=True)
    parser.add_argument("--code", default="sh000001", help="live 通道代码，如 sh000001 / sz399001")
    parser.add_argument("--ts-code", default="000001.SH", help="backtest 通道代码，如 000001.SH / 399001.SZ")
    parser.add_argument("--date", default=_iri_now().strftime("%Y-%m-%d"), help="backtest 日期 YYYY-MM-DD")
    parser.add_argument("--freq", default="5min", help="backtest 频率，如 5min / 1min")
    parser.add_argument("--regime", default="range", help="日线态势（I4 用），如 uni_up / uni_down / range")
    parser.add_argument("--score", type=float, default=0.0, help="日线综合评分（透传）")
    parser.add_argument("--no-daily", action="store_true", help="不拉日线（I3 将降级）")
    args = parser.parse_args()

    out = {"mode": args.mode}
    if args.mode == "live":
        df = fetch_index_minutes_live(args.code)
        ts_code = args.ts_code
    else:
        df = fetch_index_minutes_backtest(args.ts_code, args.date, args.freq)
        ts_code = args.ts_code

    daily_bars = None
    if not args.no_daily:
        try:
            ref_date = args.date if args.mode == "backtest" else _iri_now().strftime("%Y-%m-%d")
            daily_bars, daily_src = _iri_fetch_daily_bars_for_cli(ts_code, ref_date)
            out["daily_bars_rows"] = len(daily_bars)
            out["daily_bars_source"] = daily_src
        except Exception as e:  # noqa: BLE001
            out["daily_bars_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            daily_bars = None

    result = detect_intraday_alert(df, daily_regime=args.regime,
                                   daily_score=args.score, daily_bars=daily_bars)
    out.update({
        "source": df.attrs.get("iri_source"),
        "rows": len(df),
        "first_bar": df["time"].iloc[0] if len(df) else None,
        "last_bar": df["time"].iloc[-1] if len(df) else None,
        "prev5d_avg_vol_same_period": df.attrs.get("prev5d_avg_vol_same_period"),
        "result": result,
    })
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    _iri_main()
