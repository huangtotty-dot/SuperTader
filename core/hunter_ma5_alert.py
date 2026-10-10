# -*- coding: utf-8 -*-
"""选股猎手「热门板块内个股 · 刚站上5日线」飞书告警（2026-10-09）。

范围（owner 2026-10-09）：仅**猎手热门板块**内个股（默认前 5 个板块的成分股并集）。
  · 热门板块 = stock_hunter/history/daily_summary.json 最新日排名（按 平均分）前 N；
  · 成分股   = watchlist_jiuyan.json 按 sector/韭研概念 匹配（与 t_gui.load_hunter_history 同款）。
判定：core/ma_reclaim.reclaim5（昨收<昨MA5 且 今价>今MA5）；取数走 daily_many 批量帧。
去重：每只每日一次（t_io/state/hunter_ma5_pushed.json）。纯通知，不触发交易。
"""
import json
import logging
import os
from datetime import datetime

log = logging.getLogger("hunter_ma5_alert")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HUNTER_DIR = os.path.join(_BASE, "stock_hunter")
_SUMMARY_FP = os.path.join(_HUNTER_DIR, "history", "daily_summary.json")
_WL_FP = os.path.join(_HUNTER_DIR, "watchlist_jiuyan.json")
_DEDUP_FP = os.path.join(_BASE, "t_io", "state", "hunter_ma5_pushed.json")
_BELOW_FP = os.path.join(_BASE, "t_io", "state", "hunter_ma5_below.json")
_TOPN_DEFAULT = 5
_CARD_MAX_ROWS = 20          # 卡片明细最多列这么多行，其余折叠成代码清单（owner 2026-10-10）
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


def _today() -> str:
    """今天的日期串。单独抽出来做**测试缝**（单测可替换，从而离线验证 V 反转）。"""
    return datetime.now().strftime("%Y-%m-%d")


def _load_below() -> dict:
    """{日期: [今日曾在 MA5 下方的码]} —— 盘中「V 反转」判据的必需记忆。

    隔夜回站只看昨收/今价，答不了「早盘破线、午后拉回」——owner 2026-10-08 就实报过
    江西铜业 600362 这种 case。要判 V 反转，必须记住今天它曾经在 MA5 下方过。
    """
    try:
        with open(_BELOW_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_below(d: dict) -> None:
    try:
        for k in sorted(k for k in d if k)[:-_DEDUP_KEEP]:
            d.pop(k, None)
        os.makedirs(os.path.dirname(_BELOW_FP), exist_ok=True)
        tmp = _BELOW_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _BELOW_FP)
    except Exception:
        pass


def scan_hunter_ma5(date: str = None, top_n: int = None, record_below: bool = True) -> list:
    """对热门板块内个股算「刚站上5日线」，返回命中事件（未去重、未推送）。

    两种口径都算命中（owner 2026-10-10 确认要补第二种）：
      · **隔夜回站**（kind=`overnight`）：昨收 < 昨MA5 且 现价 > 今MA5。
      · **盘内 V 反转**（kind=`vrev`）：今日**曾在 MA5 下方**（本函数每轮记录）且现价回到 MA5 上方。
        这条只有**当天实时**跑才有意义——历史回放没有盘中状态，不参与。
    """
    stocks = hot_board_stocks(top_n, date)
    if not stocks:
        return []
    target = str(date) if date else _today()
    live = (target == _today())
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
    from core.ma_reclaim import ma5_state, _round_state
    below_store = _load_below() if live else {}
    below_prev = set(below_store.get(target) or [])
    below_now = set(below_prev)
    hits = []
    for c in codes:
        fr = frames.get(c)
        if fr is None or getattr(fr, "empty", True):
            continue
        # 交易日闸：末根必须是目标日（历史扫描防前视；盘中由 daily_many 补当日 forming bar）
        if str(fr["date"].astype(str).iloc[-1]) != target:
            continue
        s = ma5_state(fr[fr["date"].astype(str) <= target]["close"].astype(float).values)
        if s is None:
            continue
        if s["below"]:
            below_now.add(c)                      # 记下「今天它到过 MA5 下方」
        kind = None
        if s["overnight_reclaim"]:
            kind = "overnight"
        elif c in below_prev and s["above"]:
            kind = "vrev"
        if kind:
            hits.append({"code": c, "name": stocks[c], "kind": kind,
                         **_round_state(s)})
    if live and record_below:
        below_store[target] = sorted(below_now)
        _save_below(below_store)
    hits.sort(key=lambda x: -(x.get("dev5_pct") if x.get("dev5_pct") is not None else -1e9))
    return hits


def build_card(events: list, top_n: int = None, date: str = None) -> dict:
    if not events:
        return None
    tn = int(top_n or _TOPN_DEFAULT)
    n_over = sum(1 for e in events if e.get("kind") == "overnight")
    n_vrev = len(events) - n_over
    head = f"📅 {date or _today()} · 共 {len(events)} 只"
    if n_vrev:
        head += f"（隔夜回站 {n_over} · 盘内V反转 {n_vrev}）"
    lines = [f"**选股猎手·热门板块内「刚站上5日线」**（前 {tn} 板块）", head, ""]
    shown = events[:_CARD_MAX_ROWS]
    for e in shown:
        tag = " ｜盘内V反转" if e.get("kind") == "vrev" else ""
        lines.append(f"⬆️ **{e['code']}** {e['name']}  "
                     f"现价 {e['price']} ｜ MA5 {e['ma5']}（+{e['dev5_pct']}%）{tag}")
    rest = events[_CARD_MAX_ROWS:]
    if rest:
        # 折叠而不是丢弃：只给代码，省版面又不丢信息（按偏离幅度已在上面排过序）
        lines.append("")
        lines.append(f"**另有 {len(rest)} 只**（偏离幅度较小）："
                     + " ".join(e["code"] for e in rest))
    lines += ["",
              "📌 口径：**隔夜回站**=昨收在 MA5 下、今日站上；**盘内V反转**=今日曾跌破 MA5 又拉回站上。",
              f"🔎 明细仅列前 {_CARD_MAX_ROWS} 只（按偏离幅度降序）。",
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
    # ⚠️ send_feishu_payload 的签名是 (payload, success_log, error_prefix, ...)——
    # 2026-10-10 前这里只传了 card，每次都抛 TypeError，又被裸 except 吞掉 ⇒
    # 本告警**自 2026-10-09 上线起从未推送成功过一次**，且不留任何日志/去重痕迹。
    # 现在：参数补齐 + 检查返回值 + 失败时把原因打出来（别再用静默 except 把这类问题藏住）。
    try:
        from config import send_feishu_payload
        ok = send_feishu_payload(
            card, success_log=f"猎手热门板块站上5日线飞书推送: {len(fresh)} 只",
            error_prefix="猎手热门板块站上5日线推送")
    except Exception as e:
        log.warning(f"⚠️ 猎手站上5日线推送异常（不写去重，下轮重试）: {type(e).__name__}: {str(e)[:180]}")
        return []
    if not ok:
        log.warning("⚠️ 猎手站上5日线推送返回失败（不写去重，下轮重试）")
        return []
    seen.update(h["code"] for h in fresh)
    dedup[target] = sorted(seen)
    _save_dedup(dedup)
    return fresh
