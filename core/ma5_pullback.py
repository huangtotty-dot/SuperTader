# -*- coding: utf-8 -*-
"""「回踩 5 日线不破」· 每 30 分钟轮询 → 飞书（2026-10-20 owner）。

## 扫描范围（owner 2026-10-20 拍板）
三个来源的**并集**，命中后按来源分组展示：
  · `ob`     **30 分钟风险体检**里那批票 = `holdings_manual.json` 中 qty>0 的持仓
             （与 `t_gui._load_ob_analysis_impl` 同一个口径：跳过 `_` 开头的键与已清仓）
  · `pb`     **建仓信号扫描**那批票 = `watchlist_buy.json` 的 stocks
  · `pool`   **异常监控池**（GUI 手动维护，`t_io/state/abnormal_pool.json`）
             —— 2026-10-22 owner：「取消选股猎手的推送飞书，异常监控池需要实时播报」，
             原来的 `hunter`（猎手前 10 热门板块）源已换成它。

## 判定口径（owner 2026-10-20 拍板：触线 0.5% + 现价不超过 MA5+2%）
  · 今 MA5 = (前 4 个**完成日**收盘 + 现价) / 5 —— 与 `core.ma_reclaim.ma5_state` 同口径
  ① 昨收 > 昨 MA5            —— 昨天就站在线上，今天才谈得上「回踩」
     （去掉这条，实测命中数从 149 涨到 543，等于把「本来就在线下」的也算进来）
  ② 今日最低 ≤ MA5 × 1.005    —— 回踩到线附近（0.5% 容差，否则「刚好碰到」几乎不可能发生）
  ②' 今日最低 ≥ MA5 × 0.98    —— 破太深不算回踩（见 TOUCH_MIN 注释；实测不加这条 68 只里 13 只是深 V）
  ③ MA5 ≤ 现价 ≤ MA5 × 1.02   —— 没破，且**没飞走**（>+2% 已经是反弹走了，不是回踩）
     实测反例 000530：最低碰到 MA5，但收盘已高于 MA5 11.1% —— 不该叫「回踩不破」

## 为什么这么取数（成本实测，2026-10-20 本机）
  · 完成日线：本地缓存 `chart_cache.load_daily_display`，**零网络**（1669 只 ≈ 12s）
  · 当日 价/最低：腾讯 `snapshot_auction_many` **一次分块批量**（1669 只 ≈ 1.9s）
  · 绝不走 `facade.daily()`（GM-first，单 worker，慢源时卡 60s）——见 `hunter_ma5_alert` 的教训

## 推送纪律
  · **每只票每天只推一次**（`ma5_pullback_pushed.json` 按日期去重）；推送失败**不写去重**，下轮重试。
  · 没命中就**不发卡**（不刷屏）。
  · 推送走 `config.send_feishu_payload`，**必传三参并检查返回值**（本仓有「只传 card ⇒ TypeError
    被裸 except 吞掉 ⇒ 告警自上线起从未发出过」的血案，见 hunter_ma5_alert 头注释）。

⚠️ 纯通知，**不触发任何交易**。
"""
import json
import logging
import os
import time as _time
from datetime import datetime

log = logging.getLogger("ma5_pullback")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_DIR = os.path.join(_BASE, "t_io", "state")
_HOLDINGS_FP = os.path.join(_STATE_DIR, "holdings_manual.json")
_WATCH_FP = os.path.join(_STATE_DIR, "watchlist_buy.json")
_DEDUP_FP = os.path.join(_STATE_DIR, "ma5_pullback_pushed.json")

TOUCH_TOL = 0.005        # ② 回踩容差：今日最低 ≤ MA5 × (1+0.5%)
TOUCH_MIN = -0.02        # ②' 破位下限：今日最低 ≥ MA5 × (1−2%)
#   为什么要有下限：没有它，一根跌到 MA5 下方 8% 又被拉回的票也会被叫「回踩不破」——
#   那是「破了又收回」，不是回踩。实测 2026-10-20：不加下限，68 只里有 13 只跌破 >2%
#   （最深 −8.44%）。下限 −2% 留足日内毛刺，又不把深 V 混进来。
ABOVE_MAX = 0.02         # ③ 反弹上限：现价 ≤ MA5 × (1+2%)
HUNTER_TOP_N = 10        # 猎手取前 N 热门板块
POLL_SEC = 1800          # 30 分钟一轮
CARD_MAX_ROWS = 20       # 每张卡明细行上限（其余折叠成代码串）
DEDUP_KEEP = 10          # 去重文件保留最近 N 天

# 来源展示顺序与标题（命中多源时按第一个作为主分组，其余标在括号里）
_SRC_ORDER = ("ob", "pb", "pool")
_SRC_TITLE = {"ob": "🏥 体检持仓", "pb": "🏗️ 建仓池", "pool": "🚨 异常监控池"}
_SRC_SHORT = {"ob": "体检", "pb": "建仓", "pool": "监控池"}


# ---------------------------------------------------------------- 三源名单
def _load_json(fp, default):
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _norm_code(code):
    c = str(code).split("_")[0].strip()
    return c if len(c) == 6 and c.isdigit() else None


def holdings_codes() -> dict:
    """30 分钟风险体检的同一批票：holdings_manual 中 qty>0。→ {code: name}"""
    out = {}
    for code, info in (_load_json(_HOLDINGS_FP, {}) or {}).items():
        if not isinstance(info, dict) or str(code).startswith("_"):
            continue
        if not (info.get("qty") or 0):
            continue                      # 已清仓不进体检
        c = _norm_code(code)
        if c:
            out[c] = str(info.get("name") or c)
    return out


def pb_codes() -> dict:
    """建仓信号扫描的同一批票：watchlist_buy.json 的 stocks。→ {code: name}"""
    out = {}
    wl = _load_json(_WATCH_FP, {}) or {}
    for code, info in ((wl.get("stocks") or {}) if isinstance(wl, dict) else {}).items():
        c = _norm_code(code)
        if not c:
            continue
        out[c] = (str((info or {}).get("name") or c) if isinstance(info, dict) else c)
    return out


def pool_codes(top_n: int = None) -> dict:
    """**异常监控池** → {code: name}（2026-10-22 起取代原来的「猎手前 10 板块」源）。

    池空 ⇒ {}，这一源就不参与并集（猎手那套已经不发飞书了）。
    """
    try:
        from core.hunter_ma5_alert import pool_stocks
        return {str(c): str(n) for c, n in (pool_stocks() or {}).items()}
    except Exception as e:
        log.warning(f"⚠️ [回踩MA5] 异常监控池取数失败（本轮跳过该源）: {type(e).__name__}: {str(e)[:120]}")
        return {}


def universe(top_n: int = None) -> dict:
    """三源并集 → {code: {"name": 名称, "sources": ["ob", ...]}}。"""
    out = {}
    for key, mp in (("ob", holdings_codes()), ("pb", pb_codes()),
                    ("pool", pool_codes(top_n))):
        for c, n in (mp or {}).items():
            e = out.setdefault(c, {"name": n or c, "sources": []})
            if key not in e["sources"]:
                e["sources"].append(key)
            if (not e["name"] or e["name"] == c) and n:
                e["name"] = n
    return out


# ---------------------------------------------------------------- 判定
def build_context(codes, today: str = None) -> dict:
    """{code: {prev_close, prev_ma5, last4_sum}} —— 全部来自**本地日线缓存**（零网络）。

    今日 MA5 随现价变，故只预存「前 4 个完成日之和」，判定时再 (sum+price)/5。
    """
    from core import chart_cache as _cc
    # 复用 hunter_ma5_alert 的「完成日收盘尾部」实现：同一套停牌/陈旧闸，别各写一份
    from core.hunter_ma5_alert import _completed_history
    today = str(today or datetime.now().strftime("%Y-%m-%d"))
    hist = {}
    for c in codes:
        try:
            fr = _cc.load_daily_display(c)
        except Exception:
            continue
        closes = _completed_history(fr, today)
        if not closes:
            continue
        hist[c] = {"prev_close": float(closes[-1]),
                   "prev_ma5": float(sum(closes[-5:]) / 5.0),
                   "last4_sum": float(sum(closes[-4:]))}
    return hist


def judge(ctx: dict, snap: dict, today: str = None) -> list:
    """完成日上下文 + 实时快照 → 命中列表（未去重、未排序以外的加工）。"""
    today = str(today or datetime.now().strftime("%Y-%m-%d"))
    hits = []
    for c, h in (ctx or {}).items():
        q = (snap or {}).get(c) or {}
        px, low = q.get("price"), q.get("low")
        # ⚠️ `low` 可能是 0（坏快照/停牌占位）—— 只判 `is None` 会让它算成「跌了 100%」，
        #    实测踩到过一次（touch_pct = −100.0）。价格同理。
        if not px or low is None:
            continue
        try:
            px, low = float(px), float(low)
        except (TypeError, ValueError):
            continue
        if px <= 0 or low <= 0:
            continue
        # 停牌/陈旧：快照日期不是今天 ⇒ 那个「最低/现价」不是今天的，别当回踩
        if str(q.get("ts_date") or "")[:10] != today:
            continue
        ma5 = (h["last4_sum"] + px) / 5.0
        if ma5 <= 0:
            continue
        if h["prev_close"] <= h["prev_ma5"]:
            continue                                  # ① 昨天没站在线上，谈不上回踩
        if low > ma5 * (1 + TOUCH_TOL):
            continue                                  # ② 今天没回踩到线附近
        if low < ma5 * (1 + TOUCH_MIN):
            continue                                  # ②' 破太深（＞2%）= 破位后又拉回，不是回踩
        if not (ma5 <= px <= ma5 * (1 + ABOVE_MAX)):
            continue                                  # ③ 破了 或 已经飞走
        hits.append({"code": c, "price": round(px, 3), "ma5": round(ma5, 3),
                     "low": round(low, 3),
                     "dev5_pct": round((px - ma5) / ma5 * 100, 2),
                     "touch_pct": round((low - ma5) / ma5 * 100, 2),
                     "pct": q.get("pct")})
    hits.sort(key=lambda x: x["dev5_pct"])            # 越贴近线越靠前
    return hits


# ---------------------------------------------------------------- 去重
def _load_dedup() -> dict:
    d = _load_json(_DEDUP_FP, {})
    return d if isinstance(d, dict) else {}


def _save_dedup(d: dict) -> None:
    try:
        for k in sorted(k for k in d if k)[:-DEDUP_KEEP]:
            d.pop(k, None)
        os.makedirs(os.path.dirname(_DEDUP_FP), exist_ok=True)
        tmp = _DEDUP_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _DEDUP_FP)
    except Exception:
        pass


# ---------------------------------------------------------------- 卡片
def _row(h: dict, uni: dict) -> str:
    name = (uni.get(h["code"]) or {}).get("name") or h["code"]
    _pct = h.get("pct")
    _pct_txt = ("　今日 " + ("+" if _pct >= 0 else "") + f"{_pct:.2f}%") if isinstance(_pct, (int, float)) else ""
    return (f"· `{h['code']}` **{name}**　现价 {h['price']}　MA5 {h['ma5']}"
            f"（{'+' if h['dev5_pct'] >= 0 else ''}{h['dev5_pct']}%）"
            f"　最低 {h['low']}（{'+' if h['touch_pct'] >= 0 else ''}{h['touch_pct']}%）{_pct_txt}")


def build_card(hits: list, now: datetime = None, uni: dict = None) -> dict:
    """命中列表 → 飞书交互卡（按来源分组；多源命中标在括号里）。"""
    now = now or datetime.now()
    uni = uni or {}
    date_str = now.strftime("%Y-%m-%d")
    by_src = {k: [] for k in _SRC_ORDER}
    for h in hits:
        srcs = (uni.get(h["code"]) or {}).get("sources") or ["pool"]
        by_src[srcs[0]].append((h, srcs))

    rows, shown = [], 0
    for key in _SRC_ORDER:
        items = by_src.get(key) or []
        if not items:
            continue
        rows.append("")
        rows.append(f"**{_SRC_TITLE[key]}（{len(items)}）**")
        for h, srcs in items:
            if shown >= CARD_MAX_ROWS:
                break
            extra = ("（同时属：" + "、".join(_SRC_SHORT[s] for s in srcs[1:]) + "）") if len(srcs) > 1 else ""
            rows.append(_row(h, uni) + extra)
            shown += 1
        rest = len(items) - shown if len(items) > shown else 0
        if rest > 0:
            rows.append(f"　…另有 {rest} 只（见 GUI）")

    head = (f"**口径**：昨收 > 昨 MA5 · 今日最低在 MA5-{abs(TOUCH_MIN) * 100:.0f}% ~ "
            f"MA5+{TOUCH_TOL * 100:.1f}% 之间 · 现价在 MA5 ~ MA5+{ABOVE_MAX * 100:.0f}% 之间\n"
            f"共 **{len(hits)}** 只 · 每只每日只报一次 · 纯通知，不构成交易指令")
    markdown = head + "\n" + "\n".join(rows)
    return {"msg_type": "interactive",
            "card": {"header": {"template": "orange",
                                "title": {"tag": "plain_text",
                                          "content": f"📉 回踩5日线不破 - {date_str} {now.strftime('%H:%M')}"}},
                     "elements": [{"tag": "markdown", "content": markdown}]}}


def _push(card: dict, n: int) -> bool:
    """**必传三参并检查返回值**（本仓血案：只传 card ⇒ TypeError 被吞 ⇒ 从未发出过）。"""
    try:
        from config import send_feishu_payload
        return bool(send_feishu_payload(
            card, success_log=f"回踩5日线不破飞书推送: {n} 只",
            error_prefix="回踩5日线不破推送"))
    except Exception as e:
        log.warning(f"⚠️ [回踩MA5] 推送异常（不写去重，下轮重试）: {type(e).__name__}: {str(e)[:160]}")
        return False


# ---------------------------------------------------------------- 一轮
def run_once(now: datetime = None, dry_run: bool = False, top_n: int = None,
             push_fn=None, snapshot_fn=None, uni=None, ctx=None) -> dict:
    """跑一轮：建名单 → 完成日上下文 → 实时快照 → 判定 → 去重 → 推送。**不抛。**

    返回 {"ok","hits","pushed","reason"}；`push_fn`/`snapshot_fn`/`uni`/`ctx` 是测试缝。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    uni = uni if uni is not None else universe(top_n)
    if not uni:
        return {"ok": False, "hits": [], "pushed": False, "reason": "三源名单为空"}
    if ctx is None:
        ctx = build_context(list(uni), today)
    if not ctx:
        return {"ok": False, "hits": [], "pushed": False, "reason": "完成日线上下文为空（日线缓存没就绪）"}

    if snapshot_fn is None:
        def snapshot_fn(codes):
            from core.market_data.tencent_provider import TencentProvider
            return TencentProvider().snapshot_auction_many(codes) or {}
    try:
        snap = snapshot_fn(list(ctx)) or {}
    except Exception as e:
        log.warning(f"⚠️ [回踩MA5] 快照失败: {type(e).__name__}: {str(e)[:120]}")
        return {"ok": False, "hits": [], "pushed": False, "reason": f"快照失败: {type(e).__name__}"}
    if not snap:
        return {"ok": False, "hits": [], "pushed": False, "reason": "快照为空"}

    hits = judge(ctx, snap, today)
    for h in hits:
        h["sources"] = (uni.get(h["code"]) or {}).get("sources") or []
    dedup = _load_dedup()
    seen = set(dedup.get(today) or [])
    fresh = [h for h in hits if h["code"] not in seen]
    if not fresh:
        return {"ok": True, "hits": [], "pushed": False, "reason": f"命中 {len(hits)} 只但均已推送过"}
    if dry_run:
        return {"ok": True, "hits": fresh, "pushed": False, "reason": "dry_run"}

    card = build_card(fresh, now, uni)
    ok = (push_fn(card) if push_fn else _push(card, len(fresh)))
    if not ok:
        return {"ok": True, "hits": fresh, "pushed": False, "reason": "推送失败（不写去重，下轮重试）"}
    seen.update(h["code"] for h in fresh)
    dedup[today] = sorted(seen)
    _save_dedup(dedup)
    return {"ok": True, "hits": fresh, "pushed": True, "reason": ""}


def run_loop(poll_sec: float = None, stop_event=None, sleep_fn=_time.sleep,
             now_fn=datetime.now, on_run=None) -> None:
    """daemon 线程：交易时段每 `poll_sec` 秒跑一轮（默认 30 分钟）。

    与主循环**解耦**（main.py 一轮可达 15 分钟，挂主循环会把 30 分钟稀释成 30+ 分钟）。
    60s 粒度轮转，避免长 sleep 错过开盘/收盘窗口。
    """
    from core.hunter_poll import in_trading_hours
    poll_sec = float(poll_sec or POLL_SEC)
    last = None
    log.info("📉 回踩5日线不破轮询已启动（每 %.0f 分钟，交易时段）", poll_sec / 60.0)
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            now = now_fn()
            if in_trading_hours(now) and (last is None
                                          or (now - last).total_seconds() >= poll_sec):
                last = now
                res = run_once(now)
                if on_run is not None:
                    try:
                        on_run(res)
                    except Exception:
                        pass
                if res.get("pushed"):
                    log.info("📉 回踩5日线不破：推送 %d 只", len(res.get("hits") or []))
                elif res.get("hits"):
                    log.warning("📉 回踩5日线不破：%s", res.get("reason"))
        except Exception as e:      # 轮询本体不许死
            log.warning("⚠️ [回踩MA5] 本轮异常: %s: %s", type(e).__name__, str(e)[:160])
        sleep_fn(60)
