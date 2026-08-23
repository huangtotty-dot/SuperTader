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

BASE = Path(__file__).resolve().parent
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


# ---------------------------------------------------------------- 数据获取
def _http_json(url: str, timeout: int = 12):
    for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"]:
        os.environ.pop(_k, None)
    os.environ["NO_PROXY"] = "*"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.qq.com/"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore"))


def _tx_kline(symbol: str, period: str, count: int, end: str) -> list:
    """腾讯 fqkline（day/week/month）→ [{date,open,close,high,low,volume}] 升序。仿 index_regime._ir_fetch_index_daily_tx。"""
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    # 按周期算 start 提前天数：day 按交易日、week/month 按自然日（原 count*1.6 只对 day 对，导致周/月线拉不满）
    if period == "week":
        start_days = int(count * 7 * 1.3) + 40
    elif period == "month":
        start_days = int(count * 30 * 1.3) + 40
    else:
        start_days = int(count * 1.6) + 40
    start = (end_dt - timedelta(days=start_days)).strftime("%Y-%m-%d")
    url = (f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={symbol},{period},{start},{end},{count},qfq")
    js = _http_json(url)
    try:
        node = js["data"][symbol]
        rows = node.get(f"qfq{period}") or node.get(period) or []
    except Exception:
        return []
    out = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            out.append({
                "date": str(row[0])[:10],
                "open": float(row[1]), "close": float(row[2]),
                "high": float(row[3]), "low": float(row[4]),
                "volume": float(row[5]),
            })
        except (ValueError, TypeError):
            continue
    return out


def fetch_index_daily(symbol: str, count: int = 60, end: str | None = None) -> list:
    return _tx_kline(symbol, "day", count, end or datetime.now().strftime("%Y-%m-%d"))


def fetch_index_weekly(symbol: str, count: int = 52, end: str | None = None) -> list:
    return _tx_kline(symbol, "week", count, end or datetime.now().strftime("%Y-%m-%d"))


def fetch_index_monthly(symbol: str, count: int = 36, end: str | None = None) -> list:
    return _tx_kline(symbol, "month", count, end or datetime.now().strftime("%Y-%m-%d"))


def fetch_index_minutes(ts_code: str, freq: str, date: str) -> list:
    """tushare stk_mins 指数分钟线（当日）。失败返回 []。"""
    try:
        from index_regime_intraday import _iri_fetch_stk_mins_one_day
        df = _iri_fetch_stk_mins_one_day(ts_code, date, freq)
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
def build_deep_dive(date: str, cross: dict) -> dict:
    """触发项 + 双锚（上证+最弱）的多周期 K 线数据。
    控 token：双锚固定 + 触发项按强度取前 N，深拆总数 ≤ MAX_DEEP（默认3）。"""
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
        monthly = fetch_index_monthly(symbol, count=36, end=date)
        weekly = fetch_index_weekly(symbol, count=52, end=date)
        daily = [d for d in fetch_index_daily(symbol, count=60, end=date) if d["date"] <= date]
        minutes = {}
        for freq in MINUTE_FREQS:
            m = fetch_index_minutes(ts_code, freq, date)
            if m:
                minutes[freq] = m
        out[name] = {
            "月线": monthly, "周线": weekly, "日线": daily, "分钟线": minutes,
        }
    return out


# ---------------------------------------------------------------- LLM
def load_llm_config() -> dict:
    try:
        if LLM_CONFIG.exists():
            return json.loads(LLM_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"base_url": "", "model": "", "api_key": ""}


def save_llm_config(base_url: str, model: str, api_key: str) -> dict:
    cfg = {"base_url": (base_url or "").strip(), "model": (model or "").strip(), "api_key": (api_key or "").strip()}
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
        "temperature": 0.4,
        "max_tokens": 4000,
        "stream": False,
    }
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


def build_prompt(date: str, cross: dict, deep: dict) -> str:
    """组装：方法论全文 + 数据 JSON + 使用说明（方法论 §五）。"""
    method = METHODOLOGY.read_text(encoding="utf-8") if METHODOLOGY.exists() else ""
    data = {"日期": date, "指数横评": cross.get("rows", []), "深拆数据": deep}
    usage = (
        "你是一名A股复盘分析师。请严格按照附件《大盘指数多周期复盘方法论》执行：\n"
        "1. 用我提供的行情数据（指数横评 + 触发项指数的多周期K线数据）进行复盘；\n"
        "2. 遵守'从大往小看、横向优先、触发式深拆'三原则；\n"
        "3. 每个判断必须引用具体数据（价格、量能、均线值），不允许出现没有数据支撑的结论；\n"
        "4. 严格按'标准化输出模板'的六个部分输出（一句话结论/指数横评/深拆/共振结论/关键位表/次日推演/操作含义）；\n"
        "5. 数据缺失的周期直接说明'数据缺失'，禁止编造；\n"
        "6. 复盘结论只做概率描述，不做确定性预测；推演剧本必须同时给出乐观与谨慎两套。"
    )
    return f"{method}\n\n===== 本轮行情数据 =====\n{json.dumps(data, ensure_ascii=False, default=str)}\n\n===== 复盘要求 =====\n{usage}"


def run_market_review(date: str, cfg: dict) -> str:
    """大盘复盘主入口：收集数据 → 组装提示词 → 调模型 → 落盘并返回 markdown。"""
    cross = build_cross_section(date)
    deep = build_deep_dive(date, cross)
    prompt = build_prompt(date, cross, deep)
    text = call_llm(prompt, cfg)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"market_review_{date}.md").write_text(text, encoding="utf-8")
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
