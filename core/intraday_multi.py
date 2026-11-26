# -*- coding: utf-8 -*-
"""多日分时（上证/科创50/创业板指）+ 量能「和前一天同一分钟」对比（2026-11-25 owner）。

需求（owner）：
  · 独立窗口显示 上证指数 / 科创50 / 创业板指 分时；若干天连排，**最多 100 天**；
  · 量能对比前一天：增量红、缩量绿；天与天用竖线分隔；盘中强实时。

数据分层（按 date 合并，后者覆盖前者）
  1) 本地沉淀库 `t_io/cache/index_1min/<code>/<date>.json` —— 每次刷新把今日分时落盘，
     日积月累自然长成 20 天（新浪/腾讯都只给最近 3~5 天，这是"20 天"的唯一长期解）；
  2) GM `index_minute(freq="60s")` —— 可一次取很多天（GM 在线时优先）；
  3) 新浪 `scale=1, datalen=1000` —— 上限 ~5 天（兜底）；
  4) 腾讯 `mkline m1` 640 根 ≈2.6 天，但**没有 amount** ⇒ 不进正式口径（宁缺不混）。
  今日 tail 单独走腾讯分时（累计量差分，3s 缓存）⇒ 盘中强实时。

返回 items[code] = {
  "name", "hist_sig",
  "hist": [{date, pts}], … | None,        # 不含今天；指纹一致 ⇒ None（只发今日 tail）
  "today_date", "prev_date", "today", "as_of",
  "avail_days", "src_days",               # 可用历史天数 / 各来源命中（前端提示用）
}
pts = [hhm, px×1000, amt_万元] 整数（过 pywebview 串行桥）。
"""
from __future__ import annotations

import json
import os
import re
import threading as _threading
import time as _time
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STORE_DIR = os.path.join(_ROOT, "t_io", "cache", "index_1min")

_INDICES = (
    ("sh000001", "上证指数"),
    ("sh000688", "科创50"),
    ("sz399006", "创业板指"),
)

_HIST_TTL = 600.0
_TODAY_TTL = 3.0
_MAX_DAYS = 100
_SAVE_MIN_GAP = 30.0          # 今日分时落盘节流（每 code）

_lock = _threading.Lock()
_hist = {}                    # code -> {"ts","days":{date:pts},"sig","src"}
_today = {}                   # code -> {"ts","date","pts"}
_hist_run = set()
_today_run = set()
_save_ts = {}                 # code -> 上次落盘时间


def _norm_pts(rows):
    acc = {}
    for hm, px, amt in rows:
        try:
            hhmm = int(str(hm).replace(":", "")[:4])
            p = float(px or 0)
            a = float(amt or 0)
        except (TypeError, ValueError):
            continue
        if p <= 0:
            continue
        # 只保留连续竞价时段（个股腾讯分时还带 09:15~09:25 竞价与 15:0x 之后的行，
        # 混进来会让"日高/日低"和量能对比失真）
        if not ((930 <= hhmm <= 1130) or (1300 <= hhmm <= 1500)):
            continue
        acc[hhmm] = [hhmm, int(round(p * 1000)), int(round(a / 1e4))]
    return [acc[k] for k in sorted(acc)]


def _parse_hist_df(df):
    out = {}
    try:
        _d = df.copy()
        _t = _d["time"].astype(str)
        _d["_day"] = _t.str[:10]
        _d["_hhm"] = _t.str[11:16].str.replace(":", "", regex=False)
        for day, sub in _d.groupby("_day"):
            pts = _norm_pts(zip(sub["_hhm"].tolist(), sub["close"].tolist(),
                                sub["amount"].tolist()))
            if pts:
                out[str(day)] = pts
    except Exception:
        return {}
    return out


# ---------------- 本地沉淀库 ----------------
def _store_save(code, date, pts):
    """今日分时落盘（30s 节流；原子替换）。"""
    if not code or not date or not pts or len(pts) < 30:
        return
    now = _time.time()
    if now - float(_save_ts.get(code) or 0.0) < _SAVE_MIN_GAP:
        return
    _save_ts[code] = now
    try:
        d = os.path.join(_STORE_DIR, code)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, f"{date}.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date, "pts": pts}, f)
        os.replace(tmp, os.path.join(d, f"{date}.json"))
    except Exception:
        pass


def _store_load(code, max_days=_MAX_DAYS + 2):
    out = {}
    d = os.path.join(_STORE_DIR, code)
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))[-max_days:]
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(d, n), "r", encoding="utf-8") as f:
                row = json.load(f)
            day = str(row.get("date") or n[:-5])
            pts = row.get("pts") or []
            if day and isinstance(pts, list) and len(pts) >= 30:
                out[day] = pts
        except Exception:
            continue
    return out


# ---------------- 各历史源 ----------------
def _from_gm(code, days):
    try:
        from core.market_data.facade import get_provider
        prov = get_provider()
        _down = getattr(prov, "_gm_down_until", None)
        if _down is not None and datetime.now() < _down:
            return {}
        df = prov.index_minute(code, count_bars=min((days + 2) * 240 + 60, 20000), freq="60s")
        return _parse_hist_df(df) if df is not None and len(df) else {}
    except Exception:
        return {}


def _from_sina(code):
    try:
        from core.market_data import sina_index
        df = sina_index.fetch_index_minutes(code, scale=1, datalen=1000)
        return _parse_hist_df(df) if df is not None and len(df) else {}
    except Exception:
        return {}


_ts = {"pro": None, "dead": False, "ts": 0.0}


def _ts_pro():
    """tushare pro_api 单例（失败缓存 10 分钟，别每拍重试）。"""
    if _ts["dead"] and _time.time() - float(_ts["ts"] or 0) < 600:
        return None
    if _ts["pro"] is None:
        try:
            import tushare as ts
            _ts["pro"] = ts.pro_api()
        except Exception:
            _ts["dead"] = True
            _ts["ts"] = _time.time()
            return None
    return _ts["pro"]


def _from_tushare(code, days):
    """tushare `stk_mins`（指数也支持，实测 000001.SH 1min 有 close+amount）→ {date: pts}。

    owner 2026-11-25：「20 天可以从 tushare 下载」。单次上限 8000 行，20 天≈4800 行够用；
    与腾讯/新浪成交额口径一致（实测同一分钟 94.58 亿 对 94.58 亿）。**盘中可能停在前一交易日**
    ⇒ 今天一律以腾讯分时为准，这里只补历史。
    """
    pro = _ts_pro()
    if pro is None:
        return {}
    try:
        from datetime import timedelta
        ts_code = code[2:] + (".SH" if str(code).startswith("sh") else ".SZ")
        # 2026-11-26 owner：「多日分时拓展最多支持 100 天」。
        # tushare 单次上限 8000 行（≈34 个交易日）⇒ 按窗口分段拉取，逐段往前推。
        out = {}
        end = datetime.now()
        want_days = max(3, int(days) + 2)
        for _chunk in range(6):
            start = end - timedelta(days=48)          # 48 自然日 ≈ 33 个交易日，接近 8000 行上限
            df = pro.stk_mins(ts_code=ts_code, freq="1min",
                              start_date=start.strftime("%Y-%m-%d 09:00:00"),
                              end_date=end.strftime("%Y-%m-%d 15:30:00"))
            if df is None or not len(df):
                break
            _t = df["trade_time"].astype(str)
            _day = _t.str[:10]
            _hm = _t.str[11:16]
            for d, sub in df.groupby(_day):
                pts = _norm_pts(zip(_hm.loc[sub.index].tolist(), sub["close"].tolist(),
                                    sub["amount"].tolist()))
                if pts and str(d) not in out:
                    out[str(d)] = pts
            if len(out) >= want_days:
                break
            try:
                _earliest = min(out) if out else str(start)[:10]
            except Exception:
                break
            _dt_earliest = datetime.strptime(_earliest, "%Y-%m-%d")
            if _dt_earliest <= (end - timedelta(days=160)):   # 防死循环：最多回溯 ~5 个月
                break
            end = _dt_earliest - timedelta(days=1)
        return out
    except Exception:
        _ts["ts"] = _time.time()
        return {}


_IDX_RE = re.compile(r"^(sh000|sh880|sh950|sz399|bj899)\d{3}$", re.I)


def _is_index(code: str) -> bool:
    return bool(_IDX_RE.match(str(code or "").split("_")[0].strip()))


def _fetch_hist(code, days):
    """合并：沉淀库 + GM + 新浪 → ({date: pts}, src_days)（不含今天）。"""
    merged = {}
    src_days = {}
    store = _store_load(code)
    if store:
        merged.update(store)
        src_days["store"] = len(store)
    gm = _from_gm(code, days) if _is_index(code) else {}
    if gm:
        src_days["gm"] = len(gm)
        merged.update(gm)           # GM 权威，覆盖沉淀库
    tsd = _from_tushare(code, days)
    if tsd:
        src_days["tushare"] = len(tsd)
        merged.update(tsd)          # tushare：最深历史（单次 8000 行≈34 天，长区间分段拉取）
    sina = _from_sina(code)
    if sina:
        src_days["sina"] = len(sina)
        merged.update(sina)         # 新浪覆盖（同为分钟成交额口径）
    return merged, src_days


# ---------------- 今日（腾讯分时，强实时） ----------------
_dma_cache = {}          # code -> {"ts", "mas": {date: [ma5, ma13, ma21]}}


def _daily_rows(code):
    """日线历史（本地缓存优先，零网络）：指数读 index_*.json，个股读 <code>.json。"""
    import os as _os
    name = f"index_{code}.json" if _is_index(code) else f"{str(code)[-6:]}.json"
    fp = _os.path.join(_ROOT, "t_io", "cache", "daily_kline", name)
    try:
        with open(fp, "r", encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("rows") or []
    except Exception:
        return []
    return rows


def _daily_mas(code, days=_MAX_DAYS + 5):
    """日线 MA5/13/21/34（2026-11-25 owner：多日分时里画日均线，不同颜色；
    2026-11-26 追加 34 日线）。

    用本地日线缓存的收盘价算；结果按 (code) 缓存 10 分钟。
    返回 {date: [ma5, ma13, ma21, ma34]}（只回最近 days+2 个交易日）。
    """
    now = _time.time()
    hit = _dma_cache.get(code)
    if hit and (now - float(hit.get("ts") or 0.0)) < 600.0:
        return hit["mas"]
    rows = _daily_rows(code)
    closes, dates = [], []
    for r in rows:
        try:
            c = float(r.get("close") or 0)
            d = str(r.get("date") or "")[:10]
        except Exception:
            continue
        if c > 0 and len(d) == 10:
            closes.append(c)
            dates.append(d)
    out = {}
    n = len(closes)
    for i in range(n):
        v = []
        for w in (5, 13, 21, 34):
            if i + 1 >= w:
                seg = closes[i + 1 - w: i + 1]
                v.append(round(sum(seg) / w, 3))
            else:
                v.append(None)
        out[dates[i]] = v
    if len(out) > days:
        keep = sorted(out)[-days:]
        out = {d: out[d] for d in keep}
    _dma_cache[code] = {"ts": now, "mas": out}
    return out


_ma5flag_cache = {}      # code -> (ts, flag)


def _ma5_flag_calc(code, price=None):
    """日线 MA5「走平/走平上翘」判定（与 gui._ma5_flag **同口径**，供多日分时上色用）。

    走平 = 最近 4 个完成日 MA5 振幅 ≤2.0%；上翘 = 今日斜率 ≥+0.15%/日 且此前 3 日净变化 ≤2.0%。
    读本地日线缓存，60s 记忆。
    """
    now = _time.time()
    hit = _ma5flag_cache.get(code)
    if hit and (now - float(hit[0] or 0.0)) < 60.0:
        return hit[1]
    try:
        rows = _daily_rows(code)
        dates = [str(r.get("date"))[:10] for r in rows if r.get("date")]
        cl = [float(r.get("close") or 0) for r in rows]
        today = _time.strftime("%Y-%m-%d")
        if dates and dates[-1] == today:
            dates, cl = dates[:-1], cl[:-1]
        if len(cl) < 10:
            return {}
        ma5c = [sum(cl[i - 4:i + 1]) / 5.0 for i in range(4, len(cl))]
        if len(ma5c) < 4:
            return {}
        px = float(price) if price else cl[-1]
        today_ma5 = (sum(cl[-4:]) + px) / 5.0
        prev4 = ma5c[-4:]
        avg = sum(prev4) / len(prev4)
        flat_range = (max(prev4) - min(prev4)) / avg * 100 if avg > 0 else 999.0
        slope1 = ((today_ma5 - ma5c[-1]) / ma5c[-1] * 100) if ma5c[-1] else 0.0
        slope3 = ((ma5c[-1] - ma5c[-4]) / ma5c[-4] * 100) if ma5c[-4] else 0.0
        if flat_range <= 2.0 and slope1 >= 0.15 and slope3 <= 2.0:
            state = "flat_turn_up"
        elif flat_range <= 2.0 and abs(slope1) < 0.15:
            state = "flat"
        elif slope1 >= 0.15:
            state = "up"
        elif slope1 <= -0.15:
            state = "down"
        else:
            state = ""
        out = {"state": state, "ma5": round(today_ma5, 3),
               "slope1": round(slope1, 3), "flat_range": round(flat_range, 3)}
    except Exception:
        out = {}
    _ma5flag_cache[code] = (now, out)
    return out


def _fetch_today(code):
    try:
        from core.market_data.tencent_provider import TencentProvider
        df = TencentProvider().index_minute(code)
        if df is None or not len(df):
            return None, []
        _t = df["time"].astype(str)
        day = str(_t.iloc[-1])[:10] if len(_t) else ""
        rows = list(zip(_t.str[11:16].tolist(), df["close"].tolist(), df["amount"].tolist()))
        return day, _norm_pts(rows)
    except Exception:
        return None, []


# ---------------- 后台刷新 ----------------
def _kick_hist(code, days):
    if code in _hist_run:
        return
    _hist_run.add(code)

    def _bg():
        try:
            got, srcs = _fetch_hist(code, days)
            if got:
                today_s = _time.strftime("%Y-%m-%d")
                got = {d: p for d, p in got.items() if d < today_s and len(p) >= 100}
                if got:
                    sig = f"{max(got)}|{len(got)}|{days}"
                    with _lock:
                        _hist[code] = {"ts": _time.time(), "days": got, "sig": sig,
                                       "src": srcs}
        except Exception:
            pass
        finally:
            _hist_run.discard(code)
    _threading.Thread(target=_bg, daemon=True, name=f"im-hist-{code}").start()


def _kick_today(code):
    if code in _today_run:
        return
    _today_run.add(code)

    def _bg():
        try:
            day, pts = _fetch_today(code)
            if pts:
                _store_save(code, day, pts)     # 沉淀库：日积月累支持 20 天
                with _lock:
                    _today[code] = {"ts": _time.time(), "date": day, "pts": pts}
        except Exception:
            pass
        finally:
            _today_run.discard(code)
    _threading.Thread(target=_bg, daemon=True, name=f"im-today-{code}").start()


def get_cached(days=5, hist_sig=None, codes=None, labels=None):
    """js_api 用：只读内存 + 后台刷新（零网络）。

    codes 省略 ⇒ 三大指数；个股/ETF 传 ["sh600584"] 这类带市场前缀的码。
    labels = {code: 名称}（前端把 K 线窗里的名称带过来；缺省回落内置名/代码）。
    """
    try:
        days = max(1, min(int(days or 5), _MAX_DAYS))
    except Exception:
        days = 5
    per_sig = hist_sig if isinstance(hist_sig, dict) else {}
    labels = labels if isinstance(labels, dict) else {}
    if codes:
        _want = []
        for c in codes:
            c = str(c or "").strip().split("_")[0]
            if len(c) >= 8 and c[:2].lower() in ("sh", "sz", "bj") and c[2:].isdigit():
                _want.append(c)
        _pairs = [(c, labels.get(c) or labels.get(c[2:]) or c) for c in _want]
    else:
        _pairs = list(_INDICES)
    now = _time.time()
    items, warming = {}, False
    for code, name in _pairs:
        with _lock:
            h = _hist.get(code)
            t = _today.get(code)
        h_ok = bool(h) and (now - float(h.get("ts") or 0.0)) < _HIST_TTL
        if not h_ok:
            warming = True
            _kick_hist(code, days + 1)
        t_ok = bool(t) and (now - float(t.get("ts") or 0.0)) < _TODAY_TTL
        if not t_ok:
            _kick_today(code)
        hist_days = (h or {}).get("days") or {}
        dates = sorted(hist_days)[-days:]
        sig = (h or {}).get("sig") or ""
        send_hist = bool(dates) and str(per_sig.get(code) or "") != sig
        t_pts = (t or {}).get("pts") or []
        today_date = (t or {}).get("date") or ""
        prev_date = dates[-1] if dates else ""
        if today_date and prev_date and today_date <= prev_date:
            t_pts, today_date = [], ""
        as_of = ""
        if t_pts:
            _h0 = t_pts[-1][0]
            as_of = f"{_h0 // 100:02d}:{_h0 % 100:02d}"
        # 日线 MA5/13/21（多日分时里按天画横线；不同颜色）
        _dm = _daily_mas(code)
        _show_dates = list(dates) + ([today_date] if t_pts else [])
        dmas = {d: _dm[d] for d in _show_dates if d in _dm}
        _last_px = None
        if t_pts:
            _last_px = t_pts[-1][1] / 1000.0
        elif hist_days and dates:
            _last_px = hist_days[dates[-1]][-1][1] / 1000.0
        items[code] = {
            "name": name,
            "ma5flag": _ma5_flag_calc(code, _last_px),
            "hist_sig": sig,
            "hist": ([{"date": d, "pts": hist_days[d]} for d in dates]
                     if send_hist else None),
            "prev_date": prev_date,
            "today_date": today_date,
            "today": t_pts,
            "as_of": as_of,
            "dmas": dmas,
            "avail_days": len(dates) + (1 if t_pts else 0),
            "src_days": (h or {}).get("src") or {},
        }
    return {"items": items, "days": days, "max_days": _MAX_DAYS, "warming": warming}
