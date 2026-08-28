# -*- coding: utf-8 -*-
"""TencentProvider — 腾讯数据源（合并实施方案 P1-1，降级兜底）。
从现有内联逻辑原样搬迁（fetch_daily_kline / _fetch_index_daily / fetch_minute_bar /
_live_quote_forming），不改变行为，只加契约断言与 attrs["source"] 标记。
缓存格式不变式：t_io/cache/daily_kline/*.json 保持 {date, saved_at, rows:[...]}。
"""
import json
import os
import urllib.request
from datetime import datetime

import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DAILY_CACHE_DIR = os.path.join(_BASE, "t_io", "cache", "daily_kline")
_MINUTE_CACHE_DIR = os.path.join(_BASE, "t_io", "cache")

_DAILY_COLS = ["date", "open", "high", "low", "close", "volume"]


def _read_json(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


_IDX_COLS_ = ["date", "open", "high", "low", "close", "volume", "amount"]


def _idx_ensure_cols(df, source):
    """确保指数日线含全部列（旧缓存缺 amount 时退化为 volume，口径自洽）。"""
    for c in _IDX_COLS_:
        if c not in df.columns:
            df[c] = df["volume"] if c == "amount" else 0.0
    df = df[_IDX_COLS_]
    df.attrs["source"] = source
    return df


def save_daily_cache(code: str, df, days: int = 800):
    """写个股日线缓存（不变式 {date, saved_at, rows:[{date,open,close,high,low,volume}]}）。
    gm 结果也写缓存（审核 P1 阻断6：gm 路径不能每次直拉）。"""
    if df is None or df.empty:
        return
    code = str(code).split("_")[0]
    recs = []
    for r in df.itertuples():
        recs.append({"date": r.date, "open": float(r.open), "close": float(r.close),
                     "high": float(r.high), "low": float(r.low), "volume": float(r.volume)})
    try:
        os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_DAILY_CACHE_DIR, f"{code}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"date": datetime.now().strftime("%Y-%m-%d"),
                                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "rows": recs}, ensure_ascii=False))
    except Exception:
        pass


def save_index_daily_cache(index: str, df):
    """写指数日线缓存（含 amount 列）。"""
    if df is None or df.empty:
        return
    idx = str(index).lower()
    recs = [{"date": r.date, "open": float(r.open), "close": float(r.close),
             "high": float(r.high), "low": float(r.low), "volume": float(r.volume),
             "amount": float(r.amount)} for r in df.itertuples()]
    try:
        os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        with open(os.path.join(_DAILY_CACHE_DIR, f"index_{idx}.json"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"rows": recs}, ensure_ascii=False))
    except Exception:
        pass


def _clear_proxy():
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]:
        os.environ.pop(_k, None)
    os.environ["NO_PROXY"] = "*"


def _qt_snapshot_raw(symbol: str):
    """腾讯 qt.gtimg.cn 实时快照 → fields 列表；失败返回 None。"""
    _clear_proxy()
    req = urllib.request.Request(f"https://qt.gtimg.cn/q={symbol}",
                                 headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
    raw = urllib.request.urlopen(req, timeout=8).read().decode("gbk", errors="replace")
    if "~" not in raw:
        return None
    f = raw.split('"')[1].split("~")
    return f


class TencentProvider:
    source = "tencent"

    # ---------- 日线 ----------
    def daily_cache(self, code: str):
        """读个股日线缓存（每日 + 盘中 15 分钟新鲜度）。命中返回 df(source=cache)，否则 None。
        供 facade gm 路径 cache-first 快读（审核残留建议，削减 10× 负载）。"""
        code = str(code).split("_")[0]
        cache_fp = os.path.join(_DAILY_CACHE_DIR, f"{code}.json")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        try:
            os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
        except Exception:
            return None
        if not os.path.exists(cache_fp):
            return None
        try:
            cached = _read_json(cache_fp)
            if not (cached.get("date") == _today and cached.get("rows")):
                return None
            rows = cached["rows"]
            _last_date = str(rows[-1].get("date", "")) if rows else ""
            _saved_at = cached.get("saved_at")
            try:
                _ts = datetime.strptime(_saved_at, "%Y-%m-%d %H:%M:%S") if _saved_at \
                    else datetime.fromtimestamp(os.path.getmtime(cache_fp))
            except Exception:
                _ts = datetime.fromtimestamp(os.path.getmtime(cache_fp))
            _stale = ((_last_date < _today and _now.strftime("%H:%M") >= "09:15")
                      or (_last_date == _today and (_now - _ts).total_seconds() > 15 * 60))
            if _stale:
                return None
            df = pd.DataFrame(rows)[_DAILY_COLS]
            df.attrs["source"] = "cache"
            return df
        except Exception:
            return None

    def daily(self, code: str, days: int = 800) -> pd.DataFrame:
        """腾讯日线（前复权 qfq），带本地缓存（t_io/cache/daily_kline/{code}.json）。"""
        cached = self.daily_cache(code)
        if cached is not None:
            return cached
        code = str(code).split("_")[0]
        symbol = ("sh" + code if code[0] in "56" else "sz" + code)
        _clear_proxy()  # 审核 #7: daily 路径代理清除恢复
        for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
            try:
                url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           "Referer": "https://finance.qq.com/"})
                raw = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", errors="ignore")
                data = json.loads(raw)
                kline = data.get("data", {}).get(symbol, {}).get("day") or \
                        data.get("data", {}).get(symbol, {}).get("qfqday") or []
                rows = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                         "high": float(i[3]), "low": float(i[4]), "volume": float(i[5])}
                        for i in kline if len(i) >= 6]
                if not rows:
                    continue
                if cache_fp:
                    try:
                        with open(cache_fp, "w", encoding="utf-8") as f:
                            f.write(json.dumps(
                                {"date": _today, "saved_at": _now.strftime("%Y-%m-%d %H:%M:%S"),
                                 "rows": rows}, ensure_ascii=False))
                    except Exception:
                        pass
                df = pd.DataFrame(rows)[_DAILY_COLS]
                df.attrs["source"] = "tencent"
                return df
            except Exception:
                continue

        # 全部主机失败 → 回退旧缓存 + 补当日 forming bar（防破均线误判，08-28 教训）
        if cache_fp and os.path.exists(cache_fp):
            try:
                cached = _read_json(cache_fp)
                if cached.get("rows"):
                    rows = cached["rows"]
                    live = self.snapshot([code]).get(code)
                    if live and str(live.get("ts_date")) == _today and live.get("price"):
                        rows = rows + [{"date": _today, "open": live["open"], "close": live["price"],
                                        "high": live["high"], "low": live["low"],
                                        "volume": live["volume"]}]
                    df = pd.DataFrame(rows)[_DAILY_COLS]
                    df.attrs["source"] = "cache"
                    return df
            except Exception:
                pass
        return pd.DataFrame(columns=_DAILY_COLS)

    # ---------- 指数日线 ----------
    _IDX_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]

    def index_daily(self, index: str = "sh000001", days: int = 800, end_date: str = None) -> pd.DataFrame:
        """指数日线（腾讯 qfq，OHLCV），缓存 t_io/cache/daily_kline/index_{index}.json。
        end_date 给定时按该日截止（回测/历史 regime 用）。"""
        idx = str(index).lower()
        cache_fp = os.path.join(_DAILY_CACHE_DIR, f"index_{idx}.json")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        _end = end_date or _today
        cached_rows = None
        if end_date is None and os.path.exists(cache_fp):
            try:
                d = _read_json(cache_fp)
                if d.get("rows"):
                    cached_rows = d["rows"]
                    cache_date = str(cached_rows[-1].get("date", ""))
                    need_refresh = (_now.weekday() < 5 and _now.strftime("%H:%M") >= "09:15"
                                    and cache_date < _today)
                    if cached_rows and not need_refresh:
                        return _idx_ensure_cols(pd.DataFrame(cached_rows), "cache")
            except Exception:
                pass
        symbol = idx.replace("sh", "sh").replace("sz", "sz")
        _clear_proxy()  # 审核 #7: index_daily 路径代理清除恢复
        for host in ("ifzq.gtimg.cn", "web.ifzq.gtimg.cn"):
            try:
                url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                           "Referer": "https://finance.qq.com/"})
                raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
                data = json.loads(raw)
                kline = data.get("data", {}).get(symbol, {}).get("day") or \
                        data.get("data", {}).get(symbol, {}).get("qfqday") or []
                rows = [{"date": i[0], "open": float(i[1]), "close": float(i[2]),
                         "high": float(i[3]), "low": float(i[4]), "volume": float(i[5]),
                         "amount": float(i[6]) if len(i) >= 7 and str(i[6]).replace(".", "").isdigit() else float(i[5])}
                        for i in kline if len(i) >= 6]
                if end_date:  # 审核 #8: 历史查询按 end_date 截止（此前静默退化）
                    rows = [r for r in rows if r["date"] <= end_date]
                if not rows:
                    continue
                if end_date is None:
                    os.makedirs(_DAILY_CACHE_DIR, exist_ok=True)
                    with open(cache_fp, "w", encoding="utf-8") as f:
                        f.write(json.dumps({"rows": rows}, ensure_ascii=False))
                return _idx_ensure_cols(pd.DataFrame(rows), "tencent")
            except Exception:
                continue
        if cached_rows:
            return _idx_ensure_cols(pd.DataFrame(cached_rows), "cache")
        return pd.DataFrame(columns=self._IDX_COLS)

    # ---------- 分钟线 ----------
    def minute_cache(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        """只读分钟 CSV 缓存。命中返回 df(source=cache)；超龄或缺失返回空 df。"""
        code = str(code).split("_")[0]
        cache_fp = os.path.join(_MINUTE_CACHE_DIR, f"minute_{code}_{date}.csv")
        if not os.path.exists(cache_fp):
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
        if ttl_seconds is not None:
            try:
                if (datetime.now().timestamp() - os.path.getmtime(cache_fp)) > ttl_seconds:
                    return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
            except Exception:
                pass
        try:
            df = pd.read_csv(cache_fp)
            if df.empty or "time" not in df.columns:
                return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])
            t = df["time"].astype(str).str.strip()
            mask = ~t.str.match(r"^\d{4}-\d{2}-\d{2}")
            if mask.any():
                t = t.mask(mask, date + " " + t)
            df["time"] = pd.to_datetime(t, errors="coerce")  # datetime64（与 gm/腾讯输出一致）
            _cols = ["time", "open", "high", "low", "close", "volume", "amount"]
            df = df.reindex(columns=[c for c in _cols if c in df.columns])
            df.attrs["source"] = "cache"
            return df
        except Exception:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume", "amount"])

    def save_minute_cache(self, code: str, date: str, df: pd.DataFrame):
        try:
            code = str(code).split("_")[0]
            os.makedirs(_MINUTE_CACHE_DIR, exist_ok=True)
            df.to_csv(os.path.join(_MINUTE_CACHE_DIR, f"minute_{code}_{date}.csv"), index=False, encoding="utf-8")
        except Exception:
            pass

    def minute(self, code: str, date: str, ttl_seconds: int = None) -> pd.DataFrame:
        """当日 1 分钟线（腾讯 minute 接口），CSV 缓存（t_io/cache/minute_{code}_{date}.csv）。
        ttl_seconds 给定时缓存超龄视为 miss（盘中避免返回陈旧分钟数据）。"""
        code = str(code).split("_")[0]
        # 缓存命中（TTL 超龄视为 miss）→ 直接返回
        _cached = self.minute_cache(code, date, ttl_seconds)
        if not _cached.empty:
            return _cached
        market = "sh" if code[0] in ("5", "6", "9") else "sz"
        symbol = f"{market}{code}"
        last_error = None
        _clear_proxy()  # 审核 #7: minute 路径代理清除恢复
        for _ in range(3):
            try:
                url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://finance.qq.com/"})
                content = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
                if not content.strip() or "<html" in content.lower():
                    last_error = "empty_or_html"
                    continue
                data = json.loads(content)
                if data.get("code") != 0 or not data.get("data"):
                    last_error = "api_empty"
                    continue
                minute_data = data.get("data", {}).get(symbol)
                if not minute_data:
                    last_error = "symbol_missing"
                    continue
                # P1 审核阻断3：校验响应实际日期，周末/节假日会把上一交易日伪造成当日 → 不符即空
                _pack = minute_data.get("data")
                resp_date = (str(_pack.get("date") or "") if isinstance(_pack, dict)
                             else str(minute_data.get("date") or ""))
                if resp_date and len(resp_date) >= 8:
                    rd = f"{resp_date[:4]}-{resp_date[4:6]}-{resp_date[6:8]}"
                    if rd != str(date):
                        last_error = f"resp_date_mismatch({rd} != {date})"
                        continue
                rows = minute_data.get("data") or minute_data.get("day") or []
                if isinstance(rows, dict):
                    rows = rows.get("data") or []   # 实际列表在 data.data[symbol].data.data 下
                bars = []
                for row in rows:
                    parts = row.split() if isinstance(row, str) else \
                        ([str(x) for x in row] if isinstance(row, list) else None)
                    if not parts:
                        continue
                    if len(parts) >= 6:
                        tm, o, c, h, l, v = (parts[0], float(parts[1]), float(parts[2]),
                                             float(parts[3]), float(parts[4]), float(parts[5]))
                        amt = float(parts[6]) if len(parts) > 6 else 0.0
                    elif len(parts) >= 4:
                        # 腾讯基础分钟接口仅 time close vol amount → OHLC 同价派生（与原实现一致）
                        tm, c, v, amt = (parts[0], float(parts[1]), float(parts[2]), float(parts[3]))
                        o = h = l = c
                    else:
                        continue
                    tm = str(tm).strip()
                    if tm.isdigit() and len(tm) in (3, 4):
                        tm = tm.zfill(4)
                        # time 用 datetime64：signal_engine 按时间切片依赖 datetime 比较与算术
                        bars.append({"time": pd.to_datetime(f"{date} {tm[:2]}:{tm[2:]}:00"),
                                     "open": o, "high": h, "low": l, "close": c,
                                     "volume": v, "amount": amt})
                if bars:
                    df = pd.DataFrame(bars)
                    self.save_minute_cache(code, date, df)
                    df.attrs["source"] = "tencent"
                    return df
                last_error = "no_bars"
            except Exception as e:
                last_error = str(e)[:60]
        _cols = ["time", "open", "high", "low", "close", "volume", "amount"]
        return pd.DataFrame(columns=_cols)

    # ---------- 指数分时（累计量差分还原） ----------
    def index_minute(self, index: str = "sh000001") -> pd.DataFrame:
        """指数当日分时（腾讯 minute/query，rows=[HHMM price cum_vol(手) cum_amount(元)]）。
        累计量差分还原为每分钟量；gm 无此数据形态，指数分钟保留腾讯（P1-2 #6 记录在案）。
        返回 {time(datetime64), open, high, low, close, volume(手), amount(元)}。"""
        code = str(index).strip()
        _cols = ["time", "open", "high", "low", "close", "volume", "amount"]
        try:
            _clear_proxy()
            url = f"https://ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"})
            content = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
            data = json.loads(content)
            node = data.get("data", {}).get(code) or {}
            pack = node.get("data") or {}
            if isinstance(pack, list):
                rows, day_str = pack, ""
            else:
                rows = pack.get("data") or []
                day_str = str(pack.get("date") or "")
            if not rows:
                return pd.DataFrame(columns=_cols)
            if not day_str:
                day_str = datetime.now().strftime("%Y%m%d")
            day_fmt = f"{day_str[:4]}-{day_str[4:6]}-{day_str[6:8]}"
            parsed = []
            prev_v, prev_a = 0.0, 0.0
            for row in rows:
                parts = row.split() if isinstance(row, str) else [str(x) for x in row]
                if len(parts) < 4:
                    continue
                hm = str(parts[0]).strip().zfill(4)
                price = float(parts[1])
                cum_v, cum_a = float(parts[2]), float(parts[3])
                # 审核 #6: 差分还原 → 股（×100 手→股），与 akshare 主通道 vwap 口径一致（Σamount/Σvolume=元/股）
                v = max(cum_v - prev_v, 0.0) * 100.0
                a = max(cum_a - prev_a, 0.0)
                prev_v, prev_a = cum_v, cum_a
                parsed.append({"time": pd.to_datetime(f"{day_fmt} {hm[:2]}:{hm[2:]}:00"),
                               "open": price, "high": price, "low": price, "close": price,
                               "volume": v, "amount": a})
            if not parsed:
                return pd.DataFrame(columns=_cols)
            df = pd.DataFrame(parsed)
            df.attrs["source"] = "tencent"
            return df.sort_values("time").reset_index(drop=True)
        except Exception:
            return pd.DataFrame(columns=_cols)

    # ---------- 实时快照 ----------
    def snapshot(self, codes: list) -> dict:
        """腾讯实时快照 → {code: {price, open, high, low, volume(手), ts_date}}。"""
        out = {}
        if not codes:
            return out
        for code in codes:
            base = str(code).split("_")[0]
            symbol = ("sh" + base if base[0] in "56" else "sz" + base)
            try:
                f = _qt_snapshot_raw(symbol)
                if not f or len(f) < 35:
                    continue
                price = float(f[3])
                if price <= 0:
                    continue
                ts = f[30] if len(f) > 30 else ""
                _tsd = ts[:8] if len(ts) >= 8 and ts[:8].isdigit() else None
                ts_date = (f"{_tsd[:4]}-{_tsd[4:6]}-{_tsd[6:8]}" if _tsd else None)
                def _f(i, d=0.0):
                    try:
                        return float(f[i]) if f[i] else d
                    except (ValueError, IndexError):
                        return d
                out[base] = {"price": price, "open": _f(5), "high": _f(33), "low": _f(34),
                             "volume": _f(6), "ts_date": ts_date}
            except Exception:
                continue
        return out

    # ---------- 竞价快照（唯一例外：gm 无虚拟匹配价，保留腾讯） ----------
    def snapshot_auction(self, codes: list) -> dict:
        """竞价/实时快照（qt.gtimg.cn 批量，字段[3]=虚拟匹配价/现价）。
        gm 无虚拟匹配价概念，竞价专用保留腾讯（合并实施方案 P1-2 #9）。
        返回 {code: {name, price, pre_close, open, high, low, vol_hand, amount_wan, ts_raw, ts_date}}。"""
        out = {}
        if not codes:
            return out
        syms = []
        for c in codes:
            b = str(c).split("_")[0]
            syms.append(("sh" if b[0] in "56" else "sz") + b)
        try:
            _clear_proxy()
            q = ",".join(syms)
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={q}",
                                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            txt = urllib.request.urlopen(req, timeout=8).read().decode("gbk", errors="replace")
            for part in txt.strip().split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                key, _, payload = part.partition("=")
                code = key.strip().lstrip("v_").upper()[2:]
                f = payload.strip().strip('"').split("~")
                if len(f) < 38:
                    continue
                def _f(i, d=0.0):
                    try:
                        return float(f[i]) if f[i] else d
                    except (ValueError, IndexError):
                        return d
                ts = f[30] if len(f) > 30 else ""
                _tsd = ts[:8] if len(ts) >= 8 and ts[:8].isdigit() else None
                ts_date = (f"{_tsd[:4]}-{_tsd[4:6]}-{_tsd[6:8]}" if _tsd else None)
                out[code] = {"name": f[1] if len(f) > 1 else code,
                             "price": _f(3), "pre_close": _f(4), "open": _f(5) or None,
                             "high": _f(33), "low": _f(34), "vol_hand": _f(6),
                             "amount_wan": _f(37), "pct": _f(32),
                             "turnover": _f(38), "amplitude": _f(43),
                             "limit_up": _f(47), "limit_down": _f(48), "vol_ratio": _f(49),
                             "ts_raw": ts, "ts_date": ts_date}
        except Exception:
            pass
        return out

    def index_auction(self, codes: list) -> dict:
        """指数竞价快照（qt.gtimg.cn 批量，字段[3]=虚拟匹配价，[4]=昨收）。
        返回 {code: {name, auction_price, pre_close, gap_pct}}。"""
        out = {}
        if not codes:
            return out
        try:
            _clear_proxy()
            q = ",".join(codes)
            req = urllib.request.Request(f"https://qt.gtimg.cn/q={q}",
                                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"})
            txt = urllib.request.urlopen(req, timeout=8).read().decode("gbk", errors="replace")
            for part in txt.strip().split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                key, _, payload = part.partition("=")
                code = key.strip().lstrip("v_").lower()
                f = payload.strip().strip('"').split("~")
                if len(f) < 5:
                    continue
                def _f(i):
                    try:
                        return float(f[i])
                    except Exception:
                        return None
                price, pc = _f(3), _f(4)
                out[code] = {"name": f[1] if len(f) > 1 and f[1] else code,
                             "auction_price": price, "pre_close": pc,
                             "gap_pct": round((price - pc) / pc * 100, 2) if price and pc else None}
        except Exception:
            pass
        return out
