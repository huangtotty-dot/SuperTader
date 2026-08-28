# -*- coding: utf-8 -*-
"""
market_review.py — 每日大盘复盘（LLM 调用，2026-08-23 新增）

按《大盘指数多周期复盘方法论》：6 指数横评 + 触发项/双锚多周期深拆 + 标准化输出模板。
数据：腾讯 fqkline（日/周/月线）+ tushare stk_mins（指数分钟线）。
模型：OpenAI 兼容 API（base_url + model + api_key，GUI 配置，明文存 t_io/state/llm_config.json）。

用法：
    from market_review import run_market_review, load_llm_config
    text = run_market_review("2026-08-21", load_llm_config())
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[1]  # 项目根（本模块位于 core/ 下）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

INDEX_POOL = [
    ("sh000001", "000001.SH", "上证指数"),
    ("sh000300", "000300.SH", "沪深300"),
    ("sh000510", "000510.SH", "中证A500"),
    ("sh000905", "000905.SH", "中证500"),
    ("sz399006", "399006.SZ", "创业板指"),
    ("sh000688", "000688.SH", "科创50"),
]
LLM_CONFIG = BASE / "t_io" / "state" / "llm_config.json"
METHODOLOGY = BASE / "doc" / "大盘指数多周期复盘方法论.md"
OUT_DIR = BASE / "t_io" / "validation" / "daily_review"
MINUTE_FREQS = ("60min", "30min", "15min", "5min")
MAX_DEEP = 3   # 深拆指数上限（双锚 + 触发项按强度取前 N），控 token
LOG_FILE = BASE / "t_io" / "logs" / "market_review.log"


def _log(msg: str) -> None:
    """复盘过程日志（2026-08-23 排查用）：t_io/logs/market_review.log 追加。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 大盘分时缓存（监控落盘，2026-08-23）
MINUTE_CACHE_DIR = BASE / "t_io" / "index_regime"
INDEX_MINUTE_CACHE_KEY = "minute_cache"          # minute_cache_{date}.json


def _minute_cache_fp(date: str) -> Path:
    return MINUTE_CACHE_DIR / f"{INDEX_MINUTE_CACHE_KEY}_{date}.json"


def _resample_minutes(df, freq: str) -> list:
    """1min DataFrame → freq 聚合 K 线（dict 列表，time 转字符串便于 json 缓存）。"""
    try:
        df = df.copy()
        df["time"] = pd.to_datetime(df["time"])
        df["_t"] = df["time"].dt.floor(freq)
        g = df.groupby("_t").agg(open=("open", "first"), high=("high", "max"),
                                 low=("low", "min"), close=("close", "last"),
                                 volume=("volume", "sum"))
        g = g.reset_index().rename(columns={"_t": "time"})
        recs = g.to_dict(orient="records")
        for r in recs:
            r["time"] = str(r["time"])
        return recs
    except Exception:
        return []


def save_daily_index_minutes(date: str | None = None) -> None:
    """大盘分时落盘（2026-08-23）：并发拉 6 指数当日 1min → 聚合 5/15/30/60 → 缓存，
    供当日复盘直接使用（tushare 分钟只到 T-1，当日拉不到）。每指数 20s 超时跳过。"""
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTO
    date = date or datetime.now().strftime("%Y-%m-%d")

    def _one(item):
        symbol, ts_code, _name = item
        try:
            from analysis.index_regime_intraday import fetch_index_minutes_live
            df = fetch_index_minutes_live(symbol)
            if df is None or df.empty:
                return ts_code, {}
            ind = {}
            for freq in MINUTE_FREQS:
                r = _resample_minutes(df, freq)
                if r:
                    ind[freq] = r
            return ts_code, ind
        except Exception:
            return ts_code, {}

    cache = {"date": date, "updated_at": datetime.now().strftime("%H:%M:%S"), "indices": {}}
    _ex = ThreadPoolExecutor(max_workers=6)
    try:
        futures = [_ex.submit(_one, item) for item in INDEX_POOL]
        for fut in futures:
            try:
                ts_code, ind = fut.result(timeout=20)
                if ind:
                    cache["indices"][ts_code] = ind
            except Exception:
                continue
    except Exception:
        pass
    finally:
        _ex.shutdown(wait=False)  # 不等待未完成线程（腾讯慢时 fetch 线程挂起，with 会卡死）
    try:
        MINUTE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _minute_cache_fp(date).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    _log(f"大盘分时落盘 {date}: {len(cache['indices'])} 指数")


def load_index_minutes(date: str, ts_code: str, freq: str) -> list | None:
    """读大盘分时缓存（监控落盘）；无则返回 None。"""
    try:
        fp = _minute_cache_fp(date)
        if fp.exists():
            c = json.loads(fp.read_text(encoding="utf-8"))
            return (c.get("indices") or {}).get(ts_code, {}).get(freq) or None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------- 数据获取
def _http_json(url: str, timeout: int = 12):
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]:
        os.environ.pop(_k, None)
    os.environ["NO_PROXY"] = "*"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore"))


def _tx_kline(symbol: str, period: str, count: int, end: str) -> list:
    """日/周/月线（P1-2 #7 收敛：走 market_data provider；指数走 index_daily，个股走 daily）。
    period 的 week/month 由 provider 日线重采样（OHLC 聚合）。返回 [{date,open,close,high,low,volume}] 升序。"""
    from core.market_data import get_provider
    from core.market_data.facade import _resample_period
    try:
        p = get_provider()
        # 按周期换算拉取量（审核 P1 阻断2）：周/月线用 count 根日线重采样会坍缩。
        # 交易日口径：周≈5天、月≈22天，日线重采样后条数≈count。
        mult = 5 if period == "week" else (22 if period == "month" else 1)
        fetch_days = max(count * mult + 5, count)
        if str(symbol).startswith(("sh", "sz")):
            df = p.index_daily(symbol, fetch_days, end)
        else:
            df = p.daily(symbol, fetch_days)
        if df is None or df.empty:
            return []
        # 审核 #9: 按 end 截止（个股日线含未来数据 → 缓存污染按 end 键）
        df = df[df["date"] <= end]
        df = _resample_period(df, period)
        return [{"date": r.date, "open": r.open, "close": r.close,
                 "high": r.high, "low": r.low, "volume": r.volume}
                for r in df.itertuples()]
    except Exception:
        return []


INDEX_DAILY_CACHE = BASE / "t_io" / "cache" / "market_review_idx"


def fetch_index_daily(symbol: str, count: int = 60, end: str | None = None) -> list:
    """指数日线（按 symbol+end 磁盘缓存，当日秒回，避免每次复盘网络拉）。"""
    return _cached_kline(symbol, "day", count, end or datetime.now().strftime("%Y-%m-%d"))


def _cached_kline(symbol: str, period: str, count: int, end: str) -> list:
    fp = INDEX_DAILY_CACHE / f"{symbol}_{period}_{end}.json"
    try:
        if fp.exists():
            return json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        pass
    rows = _tx_kline(symbol, period, count, end)
    try:
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return rows


def fetch_index_weekly(symbol: str, count: int = 52, end: str | None = None) -> list:
    return _cached_kline(symbol, "week", count, end or datetime.now().strftime("%Y-%m-%d"))


def fetch_index_monthly(symbol: str, count: int = 36, end: str | None = None) -> list:
    return _cached_kline(symbol, "month", count, end or datetime.now().strftime("%Y-%m-%d"))


def fetch_index_minutes(ts_code: str, freq: str, date: str) -> list:
    """指数分钟线：优先读大盘分时缓存（监控落盘，当日可用）；无则 tushare（历史日/T-1）。
    失败/超时返回 []。单次 tushare 限时 20s。"""
    # 1) 本地大盘分时缓存（当日监控落盘，绕过 tushare T-1 限制）
    cached = load_index_minutes(date, ts_code, freq)
    if cached:
        return cached
    # 2) tushare 历史分钟（T-1 及更早）
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTO

    def _fetch():
        from analysis.index_regime_intraday import _iri_fetch_stk_mins_one_day
        return _iri_fetch_stk_mins_one_day(ts_code, date, freq)

    _ex = ThreadPoolExecutor(max_workers=1)
    try:
        df = _ex.submit(_fetch).result(timeout=20)
        if df is None or df.empty:
            return []
        out = []
        for _, r in df.iterrows():
            out.append({
                "time": str(r["time"]), "open": float(r["open"]), "high": float(r["high"]),
                "low": float(r["low"]), "close": float(r["close"]), "volume": float(r.get("volume", 0)),
            })
        return out
    except Exception:
        return []
    finally:
        _ex.shutdown(wait=False)  # 不等待未完成线程（tushare 慢时 with 会卡死）


# ---------------------------------------------------------------- 横评表
def build_cross_section(date: str) -> dict:
    """6 指数横评：收盘/当日%/近5日%/量比/MA20/MA60/20日位置/距20日高 + 触发判断。"""
    rows = []
    for symbol, ts_code, name in INDEX_POOL:
        daily = fetch_index_daily(symbol, count=60, end=date)
        daily = [d for d in daily if d["date"] <= date]
        if len(daily) < 25:
            rows.append({"指数": name, "error": f"日线不足({len(daily)})"})
            continue
        closes = [d["close"] for d in daily]
        vols = [d["volume"] for d in daily]
        close = closes[-1]
        prev = closes[-2] if len(closes) >= 2 else close
        chg = (close / prev - 1) * 100 if prev else 0.0
        chg5 = (close / closes[-6] - 1) * 100 if len(closes) >= 6 else None
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        win20 = closes[-20:]
        lo20, hi20 = min(win20), max(win20)
        pos20 = (close - lo20) / (hi20 - lo20) * 100 if hi20 > lo20 else 50.0
        dist_hi20 = (close / hi20 - 1) * 100 if hi20 else 0.0
        vol_ratio = vols[-1] / (sum(vols[-6:-1]) / 5) if len(vols) >= 6 else None
        # 触发条件（方法论 §三）
        triggers = []
        if abs(chg) >= 1.0:
            triggers.append(f"当日{chg:+.2f}%")
        if vol_ratio is not None and (vol_ratio > 1.3 or vol_ratio < 0.7):
            triggers.append(f"量比{vol_ratio:.2f}")
        if close < ma20:
            triggers.append("破MA20")
        if close <= lo20:
            triggers.append("破20日低点")
        if close >= hi20 and (vol_ratio or 0) > 1.3:
            triggers.append("放量破20日高点")
        rows.append({
            "指数": name, "收盘": round(close, 2), "当日%": round(chg, 2),
            "近5日%": round(chg5, 2) if chg5 is not None else None,
            "量比": round(vol_ratio, 2) if vol_ratio is not None else None,
            "MA20": round(ma20, 2), "MA60": round(ma60, 2),
            "20日位置%": round(pos20, 1), "距20日高%": round(dist_hi20, 2),
            "触发": triggers,
        })
    # 最弱指数（近5日跌幅最大，双锚之一）
    weakest = None
    valid = [r for r in rows if "error" not in r and r.get("近5日%") is not None]
    if valid:
        weakest = min(valid, key=lambda r: r["近5日%"])["指数"]
    return {"date": date, "rows": rows, "weakest": weakest}


# ---------------------------------------------------------------- 深拆数据
def build_deep_dive(date: str, cross: dict, on_progress=None) -> dict:
    """触发项 + 双锚（上证+最弱）的多周期 K 线数据。
    控 token：双锚固定 + 触发项按强度取前 N，深拆总数 ≤ MAX_DEEP（默认3）。
    on_progress: 逐指数/频率进度回调（2026-08-23 新增，供 GUI 显示卡在哪一步）。"""
    pool = {name: (symbol, ts_code) for symbol, ts_code, name in INDEX_POOL}
    triggered = [r for r in cross.get("rows", []) if "error" not in r and r.get("触发")]

    def _strength(r):
        return (abs(r.get("当日%") or 0)
                + abs((r.get("量比") or 1) - 1) * 100
                + (10 if "破MA20" in r["触发"] else 0)
                + (10 if "破20日低点" in r["触发"] else 0))

    triggered.sort(key=_strength, reverse=True)
    deep_names = ["上证指数"]
    if cross.get("weakest") and cross["weakest"] != "上证指数":
        deep_names.append(cross["weakest"])
    for r in triggered:
        if r["指数"] in deep_names:
            continue
        if len(deep_names) >= MAX_DEEP:
            break
        deep_names.append(r["指数"])
    out = {}
    for name in deep_names:
        if name not in pool:
            continue
        symbol, ts_code = pool[name]
        if on_progress:
            on_progress(f"   {name} 月/周/日线…\n")
        monthly = fetch_index_monthly(symbol, count=36, end=date)
        weekly = fetch_index_weekly(symbol, count=52, end=date)
        daily = [d for d in fetch_index_daily(symbol, count=60, end=date) if d["date"] <= date]
        minutes = {}
        for freq in MINUTE_FREQS:
            if on_progress:
                on_progress(f"   {name} {freq}…\n")
            m = fetch_index_minutes(ts_code, freq, date)
            if m:
                minutes[freq] = m
        out[name] = {
            "月线": monthly, "周线": weekly, "日线": daily, "分钟线": minutes,
        }
    return out


# ---------------------------------------------------------------- 情绪/板块/个股附加数据
def load_sector_brief_ths(date: str, limit: int | None = None) -> dict:
    """同花顺概念板块强弱（2026-08-23，用户要求板块用同花顺概念）。
    并发拉同花顺概念板块(默认全部)的当日涨跌幅，排序取强势/弱势。失败返回 {}。
    名称去"概念"等后缀以对齐同花顺 App 显示。按 date 缓存（当日只拉一次，避免每次复盘 1-2 分钟）。"""
    import time as _t
    cache_fp = BASE / "t_io" / "cache" / f"ths_concept_{date}.json"
    try:  # 读缓存
        if cache_fp.exists():
            r = json.loads(cache_fp.read_text(encoding="utf-8"))
            if r.get("强势TOP5") and r.get("弱势BOTTOM5"):
                return r
    except Exception:
        pass
    try:
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor
        names = ak.stock_board_concept_name_ths()
        pool = names["name"].astype(str).tolist()
        if limit:
            pool = pool[:limit]

        def _clean(n):
            for suf in ("概念板块", "概念", "板块"):
                if n.endswith(suf) and len(n) > len(suf):
                    return n[: -len(suf)]
            return n

        end = date.replace("-", "")
        start_dt = (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=12)).strftime("%Y%m%d")
    except Exception as e:
        _log(f"概念板块列表失败: {str(e)[:80]}")
        return {}

    def _fetch(raw):
        try:
            df = ak.stock_board_concept_index_ths(symbol=raw, start_date=start_dt, end_date=end)
            if df is None or len(df) < 2 or "收盘价" not in df.columns:
                return None
            closes = df["收盘价"].astype(float).tolist()
            return (_clean(raw), round((closes[-1] / closes[-2] - 1) * 100, 2))
        except Exception:
            return None

    items = []
    try:
        with ThreadPoolExecutor(max_workers=15) as ex:
            for r in ex.map(_fetch, pool):
                if r:
                    items.append(r)
    except Exception:
        pass
    _log(f"概念板块拉取完成: 成功 {len(items)}/{len(pool)}")
    if len(items) < 3:
        _log(f"概念板块涨跌幅不足({len(items)})")
        return {}
    items.sort(key=lambda x: -x[1])
    result = {"强势TOP5": items[:5], "弱势BOTTOM5": items[-5:] if len(items) >= 5 else items}
    try:  # 写缓存（当日只拉一次）
        cache_fp.parent.mkdir(parents=True, exist_ok=True)
        cache_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    _log(f"同花顺概念板块: 强{items[:3]} 弱{items[-3:]}")
    return result


def load_market_extra(date: str) -> dict:
    """情绪指标 + 板块强弱(同花顺概念) + 持仓个股（2026-08-23 补充）。
    数据源：sentiment_daily.jsonl（情绪分/题材/涨停跌停/系统性风险/持仓做T决策）+ breadth json（炸板）+ 同花顺概念。"""
    out = {"情绪": {}, "板块": {}, "持仓个股": {}}
    sfp = BASE / "t_io" / "logs" / "sentiment_daily.jsonl"
    if sfp.exists():
        try:
            for line in reversed(sfp.read_text(encoding="utf-8").splitlines()):
                r = json.loads(line)
                if r.get("date") != date:
                    continue
                out["情绪"] = {
                    "情绪分S": r.get("score_S"), "z_S": r.get("z_S"),
                    "大盘regime": r.get("regime_name"), "题材TOP3": r.get("top3_names"),
                    "题材均分": r.get("top3_avg"), "涨停数": r.get("zt_count"),
                    "跌停数": r.get("dt_count"), "系统性风险": r.get("systemic_risk"),
                    "过热连续天数": r.get("overheat_streak"), "决策摘要": r.get("decision_summary"),
                }
                sa = r.get("sector_avgs") or {}
                items = [(str(k), float(v["avg"])) for k, v in sa.items()
                         if isinstance(v, dict) and v.get("avg") is not None]
                items.sort(key=lambda x: -x[1])
                # 板块：优先同花顺概念（用户要求），失败降级 sentiment 题材均分
                ths = load_sector_brief_ths(date)
                if ths:
                    out["板块"] = ths
                else:
                    out["板块"] = {"强势TOP5": items[:5],
                                  "弱势BOTTOM5": items[-5:] if len(items) >= 5 else items}
                ps = r.get("per_stock") or {}
                out["持仓个股"] = {
                    str(k): {"做T模式": v.get("mode_cn"), "仓位因子": v.get("pos_factor"),
                             "交易门": v.get("trade_gate"), "理由": str(v.get("reason"))[:120]}
                    for k, v in ps.items() if isinstance(v, dict)}
                break
        except Exception:
            pass
    bfp = BASE / "t_io" / "index_regime" / f"breadth_{date}.json"
    if bfp.exists():
        try:
            b = json.loads(bfp.read_text(encoding="utf-8"))
            out["情绪"]["炸板数"] = b.get("zb_count")
            out["情绪"]["炸板率"] = b.get("zb_rate")
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- LLM
def load_llm_config() -> dict:
    try:
        if LLM_CONFIG.exists():
            cfg = json.loads(LLM_CONFIG.read_text(encoding="utf-8"))
            cfg.setdefault("reasoning_effort", "")
            return cfg
    except Exception:
        pass
    return {"base_url": "", "model": "", "api_key": "", "reasoning_effort": ""}


def save_llm_config(base_url: str, model: str, api_key: str, reasoning_effort: str = "") -> dict:
    cfg = {"base_url": (base_url or "").strip(), "model": (model or "").strip(),
           "api_key": (api_key or "").strip(), "reasoning_effort": (reasoning_effort or "").strip()}
    LLM_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    LLM_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    # 返回完整配置（含 base_url/model/api_key），供 run_daily_review 后台线程直接使用
    return {"ok": True, "saved": bool(cfg.get("base_url") and cfg.get("model") and cfg.get("api_key")), **cfg}


def call_llm(prompt: str, cfg: dict) -> str:
    import requests
    base = (cfg.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}", "Content-Type": "application/json"}
    payload = {
        "model": cfg.get("model", ""),
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    # 2026-08-23: 按 Kimi 手册——K3 用顶层 reasoning_effort(low/high/max)，不传 temperature/max_tokens（各模型参数差异大）
    _effort = (cfg.get("reasoning_effort") or "").strip()
    if _effort:
        payload["reasoning_effort"] = _effort
    last_err = None
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=180)
            if r.status_code != 200:
                detail = (r.text or "").strip().replace("\n", " ")[:120]
                raise RuntimeError(f"HTTP {r.status_code} @ {url} (model={cfg.get('model')}): {detail}")
            data = r.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            last_err = e
            if attempt < 2:
                import time
                time.sleep(2)
    raise RuntimeError(f"模型调用失败(3次重试): {last_err}")


def call_llm_stream(prompt: str, cfg: dict, on_token=None, on_reasoning=None) -> str:
    """OpenAI 兼容流式调用（2026-08-23）：content 逐 token 回调 on_token；思考过程(reasoning_content)
    首次触发时回调 on_reasoning（K3 等推理模型先思考很久再输出 content，若不处理会看似无响应）。"""
    import time as _t
    import requests
    base = (cfg.get("base_url") or "").rstrip("/")
    url = base + "/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}", "Content-Type": "application/json",
               "Accept": "text/event-stream"}
    payload = {
        "model": cfg.get("model", ""),
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    # 2026-08-23: 按 Kimi 手册——K3 用顶层 reasoning_effort(low/high/max)，不传 temperature/max_tokens
    _effort = (cfg.get("reasoning_effort") or "").strip()
    if _effort:
        payload["reasoning_effort"] = _effort
    _log(f"call_llm_stream url={url} model={cfg.get('model')} effort={_effort or '(默认)'}")
    last_err = None
    for attempt in range(3):
        try:
            with requests.post(url, json=payload, headers=headers, timeout=300, stream=True) as r:
                _log(f"  HTTP {r.status_code}")
                if r.status_code != 200:
                    detail = (r.text or "").strip().replace("\n", " ")[:200]
                    _log(f"  错误响应: {detail}")
                    raise RuntimeError(f"HTTP {r.status_code} @ {url} (model={cfg.get('model')}): {detail}")
                parts = []
                reasoning_started = False
                lines_seen = 0
                content_chars = 0
                first_data = ""
                # 2026-08-23: 用 bytes 模式 iter_lines() + 手动按行 decode("utf-8")——decode_unicode=True
                # 会在 UTF-8 多字节字符跨 chunk 时切坏字节，导致 content 成 Latin-1 乱码（落盘双重编码 mojibake）
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        line = line.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        lines_seen += 1
                        continue
                    if not line.startswith("data:"):
                        continue
                    lines_seen += 1
                    if lines_seen <= 3:
                        first_data = (first_data + " " + line[:120]).strip()
                    data = line[5:].strip()
                    if data == "[DONE]":
                        _log(f"  收到 [DONE]，lines={lines_seen} reasoning={reasoning_started} content_chars={content_chars}")
                        break
                    try:
                        chunk = json.loads(data)
                        delta = (chunk.get("choices") or [{}])[0].get("delta", {}) or {}
                        reasoning = delta.get("reasoning_content")
                        if reasoning and not reasoning_started:
                            reasoning_started = True
                            _log(f"  首次 reasoning_content @ line {lines_seen}")
                            if on_reasoning:
                                on_reasoning()
                        content = delta.get("content")
                        if content:
                            content_chars += len(content)
                            parts.append(content)
                            if on_token:
                                on_token(content)
                    except Exception as _je:
                        _log(f"  解析失败 line={lines_seen}: {line[:120]}")
                if not parts:
                    _log(f"  流式结束但无 content：lines={lines_seen} reasoning={reasoning_started} 首行={first_data[:200]}")
                return "".join(parts)
        except Exception as e:
            last_err = e
            _log(f"  attempt {attempt+1} 异常: {type(e).__name__}: {str(e)[:200]}")
            if attempt < 2:
                # 2026-08-23: 429(engine overloaded)用递增长退避，普通错误短退避
                _backoff = 8 if str(e).startswith("HTTP 429") else 2
                _t.sleep(_backoff * (attempt + 1))
    raise RuntimeError(f"模型调用失败(3次重试): {last_err}")


def build_prompt(date: str, cross: dict, deep: dict, extra: dict | None = None) -> str:
    """组装：方法论全文 + 数据 JSON + 使用说明（方法论 §五）。
    extra：情绪指标/板块强弱/持仓个股（2026-08-23 补充，模型需分析这三块）。"""
    method = METHODOLOGY.read_text(encoding="utf-8") if METHODOLOGY.exists() else ""
    data = {"日期": date, "指数横评": cross.get("rows", []), "深拆数据": deep}
    if extra:
        data["市场情绪指标"] = extra.get("情绪") or {}
        data["板块强弱"] = extra.get("板块") or {}
        data["持仓个股"] = extra.get("持仓个股") or {}
    usage = (
        "你是一名A股复盘分析师。请严格按照附件《大盘指数多周期复盘方法论》执行：\n"
        "1. 用我提供的行情数据（指数横评 + 触发项指数的多周期K线数据）进行复盘；\n"
        "2. 遵守'从大往小看、横向优先、触发式深拆'三原则；\n"
        "3. 每个判断必须引用具体数据（价格、量能、均线值），不允许出现没有数据支撑的结论；\n"
        "4. 严格按'标准化输出模板'的六个部分输出（一句话结论/指数横评/深拆/共振结论/关键位表/次日推演/操作含义）；\n"
        "5. 数据缺失的周期直接说明'数据缺失'，禁止编造；\n"
        "6. 复盘结论只做概率描述，不做确定性预测；推演剧本必须同时给出乐观与谨慎两套。\n"
        "7. 额外三块分析（基于'市场情绪指标/板块强弱/持仓个股'数据）：\n"
        "   (a) 市场情绪温度：涨停/跌停/炸板数、题材TOP3热度、情绪分S与z值、是否有系统性风险；\n"
        "   (b) 板块轮动方向：强势/弱势行业TOP5，判断主线与补涨/退潮方向；\n"
        "   (c) 持仓个股点评：各持仓做T模式与理由，结合指数/板块结论给出次日操作提示。\n"
        "   上述三块可并入对应章节或在'操作含义'前单列'情绪·板块·个股速览'小节。"
    )
    return f"{method}\n\n===== 本轮行情数据 =====\n{json.dumps(data, ensure_ascii=False, default=str)}\n\n===== 复盘要求 =====\n{usage}"


def run_market_review(date: str, cfg: dict) -> str:
    """大盘复盘主入口：收集数据 → 组装提示词 → 调模型 → 落盘并返回 markdown。"""
    cross = build_cross_section(date)
    deep = build_deep_dive(date, cross)
    extra = load_market_extra(date)
    prompt = build_prompt(date, cross, deep, extra)
    text = call_llm(prompt, cfg)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"market_review_{date}.md").write_text(text, encoding="utf-8")
    return text


def run_market_review_stream(date: str, cfg: dict, on_text=None) -> str:
    """流式复盘（2026-08-23）：数据收集阶段与模型输出通过 on_text 逐步回调，供 GUI 实时显示。"""
    import time as _t
    def _emit(t):
        if on_text:
            on_text(t)

    _log(f"=== run_market_review_stream 开始 date={date} ===")
    _t0 = _t.time()
    _emit("① 正在收集 6 指数日/周/月线…\n")
    cross = build_cross_section(date)
    _log(f"横评完成 耗时{_t.time()-_t0:.1f}s rows={len(cross.get('rows', []))}")
    _emit(f"② 6 指数横评完成（{len(cross.get('rows', []))} 只）；正在准备深拆指数分钟线…\n")
    # 当日复盘：大盘分时缓存已在监控时落盘则直接用（跳过刷新，避免腾讯慢卡死）；
    # 缓存缺失（监控未运行）才拉取，且 save 内部总限时
    if date == datetime.now().strftime("%Y-%m-%d") and not _minute_cache_fp(date).exists():
        _emit("  刷新当日大盘分时缓存…\n")
        save_daily_index_minutes(date)
    deep = build_deep_dive(date, cross, on_progress=_emit)
    _log(f"深拆完成 耗时{_t.time()-_t0:.1f}s indices={list(deep.keys())}")
    _emit("③ 正在读取市场情绪/板块/持仓数据…\n")
    extra = load_market_extra(date)
    _log(f"市场附加数据 情绪={bool(extra['情绪'])} 板块={bool(extra['板块'])} 个股={len(extra['持仓个股'])}")
    _emit(f"④ 数据收集完成，正在调用模型（{cfg.get('model')}）…\n\n")
    prompt = build_prompt(date, cross, deep, extra)
    _log(f"prompt 长度 {len(prompt)} 字符")

    def _on_reasoning():
        _emit("\n🧠 模型思考中…（推理模型先思考，请稍候）\n")
    text = call_llm_stream(prompt, cfg, on_token=lambda t: _emit(t), on_reasoning=_on_reasoning)
    _log(f"模型输出完成 len={len(text)} 总耗时{_t.time()-_t0:.1f}s")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"market_review_{date}.md").write_text(text, encoding="utf-8")
    # 结构化数据存档（2026-08-23：前端可视化玻璃卡用——横评/情绪/板块）
    try:
        _meta = {"date": date, "cross": cross, "extra": extra}
        (OUT_DIR / f"market_review_{date}.json").write_text(
            json.dumps(_meta, ensure_ascii=False, default=str), encoding="utf-8")
    except Exception:
        pass
    _emit("\n\n✅ 复盘完成")
    return text


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="每日大盘复盘（LLM）")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--base-url", default=None, help="OpenAI 兼容 base_url")
    ap.add_argument("--model", default=None, help="模型名")
    ap.add_argument("--api-key", default=None, help="API key")
    args = ap.parse_args()
    cfg = load_llm_config()
    if args.base_url:
        cfg = {"base_url": args.base_url, "model": args.model or cfg.get("model", ""),
               "api_key": args.api_key or cfg.get("api_key", "")}
    if not cfg.get("api_key"):
        print("缺少 API key（GUI 配置或 --api-key）")
        sys.exit(1)
    text = run_market_review(args.date, cfg)
    print(f"已生成复盘 {OUT_DIR / f'market_review_{args.date}.md'}")
    print(text[:1200])
