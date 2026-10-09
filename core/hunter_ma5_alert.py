# -*- coding: utf-8 -*-
"""选股猎手「热门板块内个股 · 刚站上5日线」飞书告警（2026-10-09）。

范围（owner 2026-10-09）：仅**猎手热门板块**内个股（默认前 5 个板块的成分股并集）。
  · 热门板块 = stock_hunter/history/daily_summary.json 最新日排名（按 平均分）前 N；
  · 成分股   = watchlist_jiuyan.json 按 sector/韭研概念 匹配（与 t_gui.load_hunter_history 同款）。
判定：core/ma_reclaim.reclaim5（昨收<昨MA5 且 今价>今MA5）；取数走 daily_many 批量帧。
去重：每只每日一次（t_io/state/hunter_ma5_pushed.json）。纯通知，不触发交易。
"""
import json
import os
from datetime import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HUNTER_DIR = os.path.join(_BASE, "stock_hunter")
_SUMMARY_FP = os.path.join(_HUNTER_DIR, "history", "daily_summary.json")
_WL_FP = os.path.join(_HUNTER_DIR, "watchlist_jiuyan.json")
_DEDUP_FP = os.path.join(_BASE, "t_io", "state", "hunter_ma5_pushed.json")
_TOPN_DEFAULT = 5
_BATCH = 200
_BARS = 30
_DEDUP_KEEP = 10


def _c(code) -> str:
    return str(code).split("_")[0]


def _jiuyan_concepts(info: dict) -> str:
    out = []
    for i in range(1, 10):
        v = info.get(f"jiuyan_concept{i}")
        if v:
            out.append(str(v))
    v = info.get("jiuyan_concept")
    if v:
        out.append(str(v))
    return "|".join(out)


def hot_boards(top_n: int = None, date: str = None) -> list:
    """最新（<=date）交易日的热门板块名（排名前 N）。无数据返回 []。"""
    top_n = int(top_n or _TOPN_DEFAULT)
    try:
        with open(_SUMMARY_FP, "r", encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        return []
    if not isinstance(hist, dict) or not hist:
        return []
    key = (date.replace("-", "") if date else "")
    keys = sorted(k for k in hist if k.isdigit())
    if key and key in hist:
        rows = hist[key]
    else:
        rows = hist[keys[-1]] if keys else []
    out = []
    for r in (rows or [])[:top_n]:
        b = (r or {}).get("板块")
        if b:
            out.append(str(b))
    return out


def hot_board_stocks(top_n: int = None, date: str = None) -> dict:
    """热门板块内个股并集 → {code: name}。板块空/无匹配返回 {}。
    匹配口径与 t_gui.load_hunter_history 一致（sector 字段 + 韭研概念，双向子串）。"""
    tops = hot_boards(top_n, date)
    if not tops:
        return {}
    try:
        with open(_WL_FP, "r", encoding="utf-8") as f:
            jy = json.load(f)
    except Exception:
        return {}
    out = {}
    for code, info in (jy.items() if isinstance(jy, dict) else []):
        if not isinstance(info, dict) or not str(code).isdigit():
            continue
        concepts = _jiuyan_concepts(info) or str(info.get("概念", ""))
        sector_field = str(info.get("sector", ""))
        all_text = (sector_field + "_" + concepts).replace("|", "_").replace("/", "_")
        parts = [x.strip() for x in all_text.split("_") if x.strip() and len(x.strip()) >= 2]
        for s in tops:
            if any(s == p or s in p or p in s for p in parts):
                out[_c(code)] = info.get("name", info.get("名称", code))
                break
    return out


def _load_dedup() -> dict:
    try:
        with open(_DEDUP_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_dedup(d: dict) -> None:
    try:
        keys = sorted(k for k in d if k)
        for k in keys[:-_DEDUP_KEEP]:
            d.pop(k, None)
        os.makedirs(os.path.dirname(_DEDUP_FP), exist_ok=True)
        tmp = _DEDUP_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _DEDUP_FP)
    except Exception:
        pass


def scan_hunter_ma5(date: str = None, top_n: int = None) -> list:
    """对热门板块内个股算 reclaim5，返回命中事件（未去重、未推送）。"""
    stocks = hot_board_stocks(top_n, date)
    if not stocks:
        return []
    target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    codes = list(stocks)
    try:
        from core.market_data.facade import get_provider
        prov = get_provider()
    except Exception:
        return []
    frames = {}
    for i in range(0, len(codes), _BATCH):
        try:
            frames.update(prov.daily_many(codes[i:i + _BATCH], days=_BARS) or {})
        except Exception:
            pass
    from core.ma_reclaim import reclaim5_frame
    hits = []
    for c in codes:
        fr = frames.get(c)
        if fr is None or getattr(fr, "empty", True):
            continue
        r = reclaim5_frame(fr, target)
        if r:
            hits.append({"code": c, "name": stocks[c], **r})
    hits.sort(key=lambda x: -(x.get("dev5_pct") if x.get("dev5_pct") is not None else -1e9))
    return hits


def build_card(events: list, top_n: int = None, date: str = None) -> dict:
    if not events:
        return None
    tn = int(top_n or _TOPN_DEFAULT)
    lines = [f"**选股猎手·热门板块内「刚站上5日线」**（前 {tn} 板块）",
             f"📅 {date or datetime.now().strftime('%Y-%m-%d')} · 共 {len(events)} 只", ""]
    for e in events:
        lines.append(f"⬆️ **{e['code']}** {e['name']}  "
                     f"现价 {e['price']} ｜ MA5 {e['ma5']}（+{e['dev5_pct']}%）")
    lines += ["", "📌 昨收在 MA5 之下、今日站上 MA5（隔夜回站口径）。",
              "⚠️ 纯技术通知，不构成交易指令；请人工确认。"]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"template": "green",
                       "title": {"tag": "plain_text", "content": "📈 猎手热门板块·刚站上5日线"}},
            "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
        },
    }


def run_hunter_ma5_alert(top_n: int = None, date: str = None, dry_run: bool = False) -> list:
    """检测→去重→推飞书。返回本次**新推**的事件。推送成功才写去重。"""
    target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    hits = scan_hunter_ma5(date=date, top_n=top_n)
    if not hits:
        return []
    dedup = _load_dedup()
    seen = set(dedup.get(target) or [])
    fresh = [h for h in hits if h["code"] not in seen]
    if not fresh:
        return []
    card = build_card(fresh, top_n=top_n, date=date)
    if dry_run:
        return fresh
    try:
        from config import send_feishu_payload
        send_feishu_payload(card)
    except Exception:
        return []                      # 推送失败不写去重，下轮重试
    seen.update(h["code"] for h in fresh)
    dedup[target] = sorted(seen)
    _save_dedup(dedup)
    return fresh
