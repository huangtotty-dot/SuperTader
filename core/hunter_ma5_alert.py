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
import time as _time
from datetime import datetime, time as dtime

log = logging.getLogger("hunter_ma5_alert")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HUNTER_DIR = os.path.join(_BASE, "stock_hunter")
_SUMMARY_FP = os.path.join(_HUNTER_DIR, "history", "daily_summary.json")
# 2026-10-22 owner：「取消选股猎手的推送飞书，异常监控池需要实时播报」——
# 实时 watcher 的观察名单从「猎手热门板块」改成**异常监控池**（GUI 手动维护）。
_POOL_FP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "t_io", "state", "abnormal_pool.json")


def pool_stocks() -> dict:
    """异常监控池 → {code: name}。池空 ⇒ {}（那就不推送，猎手那套已经不发了）。"""
    try:
        d = json.load(open(_POOL_FP, encoding="utf-8")) if os.path.exists(_POOL_FP) else {}
        st = (d or {}).get("stocks") or {}
        return {str(c).split("_")[0]: str((v or {}).get("name") or c)
                for c, v in st.items() if str(c).strip()}
    except Exception as e:
        log.warning(f"⚠️ [MA5实时] 读异常监控池失败（本轮按空池处理）: {type(e).__name__}: {str(e)[:100]}")
        return {}
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


# ══════════════════════════════════════════════════════════════════════
# 实时 watcher（2026-10-20 owner：飞书提醒必须及时，「彻底优化」）
#
# 旧路径：main.py 的 15 分钟主循环里每 5 分钟调一次 `run_hunter_ma5_alert`
#   ⇒ 实际触发被主循环拖到 **15 分钟级**；且每轮都对 170+ 只跑 `daily_many`，
#     数据源慢时「扫描完才发卡」会再叠一层延迟。
# 新路径：独立 daemon 线程，每 20s 只做两件轻活：
#   ① 完成日线 MA5 上下文（每天/每 30 分钟重建一次，`daily_many` 只跑一次）；
#   ② 腾讯**分块批量**快照（`snapshot_auction_many`，172 只 ≈ 1 个请求）。
#   命中即推，不再等主循环、不再等全量扫描。
# ══════════════════════════════════════════════════════════════════════
_LIVE_CTX = {"date": None, "ts": 0.0, "sig": None, "ctx": None}
_LIVE_FAIL = {"n": 0, "last_alert": 0.0}
_LIVE_CTX_TTL = 1800          # 热门板块/完成日线上下文 30 分钟重建
_LIVE_POLL_SEC = 20           # 实时轮询间隔（秒）
_LIVE_FAIL_ALERT_SEC = 600    # 取数失败告警节流


def _completed_history(fr, today):
    """日线帧 → **今日之前**的收盘价尾部（最多 10 根）；不足 5 根/陈旧返回 None。"""
    if fr is None or getattr(fr, "empty", True):
        return None
    try:
        dts = [str(x)[:10] for x in fr["date"].astype(str).values]
        closes = [float(x) for x in fr["close"].astype(float).values]
    except Exception:
        return None
    rows = [(d, c) for d, c in zip(dts, closes) if d < str(today)[:10] and c == c]
    if len(rows) < 5:
        return None
    rows = rows[-10:]
    try:
        _last = datetime.strptime(rows[-1][0], "%Y-%m-%d").date()
        _today_d = datetime.strptime(str(today)[:10], "%Y-%m-%d").date()
        if (_today_d - _last).days > 15:      # 停牌/长期无数据：不做实时判定
            return None
    except Exception:
        pass
    return [c for _, c in rows]


def build_live_context(top_n: int = None, date: str = None, universe: str = "pool") -> dict:
    """热门板块股票 + 完成日线 MA5 预计算（**每天/每 30 分钟只跑一次**）。

    返回 {date, stocks, hist, codes}；hist[code] = {prev_close, prev_ma5, last4_sum}。
    今日 MA5 = (近 4 个完成日收盘 + 实时价) / 5 —— 盘中不必再拉日线。
    """
    today = str(date or _today())
    # 观察名单：默认 **异常监控池**（owner 2026-10-22）；`universe="hunter"` 保留旧口径给测试/CLI。
    stocks = pool_stocks() if universe == "pool" else hot_board_stocks(top_n)
    if not stocks:
        return {"date": today, "stocks": {}, "hist": {}, "codes": []}
    # 优先读**本地日线缓存**（172 只实测 ~1.8s、零网络）。`facade.daily_many` 是 GM-first，
    # 收盘后/数据源异常时会卡 60s 再回退腾讯——绝不能放在 20s 轮询的第一跳上。
    # 缓存缺失的少量票再用 daily_many 兜底（>20 只就跳过，避免整轮被慢源拖死）。
    from core import chart_cache as _cc
    frames, missing = {}, []
    for c in stocks:
        try:
            fr = _cc.load_daily_display(c)
        except Exception:
            fr = None
        if fr is None:
            missing.append(c)
        else:
            frames[c] = fr
    if missing and len(missing) <= 20:
        try:
            from core.market_data.facade import get_provider
            prov = get_provider()
            for i in range(0, len(missing), _BATCH):
                try:
                    frames.update(prov.daily_many(missing[i:i + _BATCH], days=10) or {})
                except Exception:
                    pass
        except Exception:
            pass
    elif missing:
        log.warning("[MA5实时] 本地日线缓存缺 %d/%d 只（>20）⇒ 本轮跳过这些票，不做慢源兜底"
                    % (len(missing), len(stocks)))
    hist = {}
    for c in stocks:
        closes = _completed_history(frames.get(c), today) if c in frames else None
        if not closes:
            continue
        prev5 = closes[-5:]
        hist[c] = {
            "prev_close": float(closes[-1]),
            "prev_ma5": float(sum(prev5) / 5.0),
            "last4_sum": float(sum(closes[-4:])),
        }
    return {"date": today, "stocks": stocks, "hist": hist, "codes": list(hist)}


def _live_ctx(top_n: int = None, date: str = None, force: bool = False,
              universe: str = "pool") -> dict:
    today = str(date or _today())
    now = _time.time()
    sig = tuple(hot_boards(top_n)) if universe != "pool" else tuple(sorted(pool_stocks()))
    c = _LIVE_CTX
    stale = (force or c["date"] != today or c["sig"] != sig
             or c["ctx"] is None or (now - float(c["ts"] or 0)) > _LIVE_CTX_TTL)
    # 上下文为空（首建失败/数据源没就绪）→ 最多每 5 分钟重试一次
    if not stale and not (c["ctx"] or {}).get("codes") and (now - float(c["ts"] or 0)) > 300:
        stale = True
    if stale:
        ctx = build_live_context(top_n, today, universe=universe)
        _LIVE_CTX.update({"date": today, "ts": now, "sig": sig, "ctx": ctx})
        log.info("[MA5实时] 上下文重建: %d/%d 只有完成日线（%s）"
                 % (len(ctx["codes"]), len(ctx["stocks"]),
                    "异常监控池" if universe == "pool" else f"前 {top_n or _TOPN_DEFAULT} 板块"))
    return _LIVE_CTX["ctx"]


def scan_live_once(ctx: dict, below_today: set, provider=None, today: str = None):
    """一次实时快照扫描 → (hits, ok)。命中未去重；`below_today` 原地记录今日到过 MA5 下方。

    · 隔夜回站：昨收 < 昨MA5 且 现价 > 今MA5；
    · 盘内 V 反转：今日**.任意一次轮询**到过 MA5 下方，之后回到上方。
    `ok=False` 表示快照整体没拿到（用于失败告警），不是"没有命中"。
    """
    codes = (ctx or {}).get("codes") or []
    if not codes:
        return [], False
    snap = {}
    try:
        if provider is None:
            # P2-13(2026-10-23)：默认快照走进程级 **QuoteBus**（共享档+跨进程锁+stale 兜底，
            # 新鲜 <10s 零网络）——与 main.py/t_gui 共用同一份腾讯快照，不再每 20s
            # 独自打 qt.gtimg.cn；也顺带绕开了 facade.snapshot 的 GM-first 60s 卡顿。
            from core.market_data.quote_bus import get_quotes as _bus_get_quotes
            snap = _bus_get_quotes(codes, max_age_sec=10.0) or {}
        else:
            # **必须绕开 facade.snapshot 的 GM-first**（GM 不可用时实测卡 60s 再回退腾讯）。
            # 优先用 facade 内部的腾讯实例 `_tx` 的分块批量接口；测试桩可直接实现该方法。
            fn = getattr(provider, "snapshot_auction_many", None)
            if fn is None:
                _tx = getattr(provider, "_tx", None)
                fn = getattr(_tx, "snapshot_auction_many", None)
            snap = (fn(codes) if fn is not None else provider.snapshot(codes)) or {}
    except Exception as e:
        log.warning("[MA5实时] 快照失败: %s: %s" % (type(e).__name__, str(e)[:160]))
        return [], False
    today = str(today or (ctx or {}).get("date") or _today())
    hits = []
    for c in codes:
        s = snap.get(c) or {}
        try:
            px = float(s.get("price") or 0)
        except Exception:
            px = 0.0
        if px <= 0:
            continue
        tsd = s.get("ts_date")
        if tsd and str(tsd)[:10] != today:
            continue                              # 快照不是今天 ⇒ 休市/停牌，跳过
        h = (ctx.get("hist") or {}).get(c) or {}
        if not h:
            continue
        ma5 = (float(h["last4_sum"]) + px) / 5.0
        if ma5 <= 0:
            continue
        above = px > ma5
        if not above:
            below_today.add(c)
            continue
        overnight = (float(h["prev_close"]) < float(h["prev_ma5"])) and above
        vrev = (c in below_today) and above
        if overnight or vrev:
            hits.append({
                "code": c, "name": ((ctx.get("stocks") or {}).get(c) or c),
                "kind": "overnight" if overnight else "vrev",
                "price": round(px, 3), "ma5": round(ma5, 3),
                "dev5_pct": round((px / ma5 - 1.0) * 100.0, 2),
                "above": True, "below": False,
                "overnight_reclaim": bool(overnight),
            })
    hits.sort(key=lambda x: -(x.get("dev5_pct") or 0))
    return hits, bool(snap)


def push_events(events: list, date: str = None, top_n: int = None,
                dry_run: bool = False, universe: str = "pool") -> list:
    """去重 → 发飞书；返回**本次新推**的事件。推送成功才写去重（失败下轮重试）。"""
    target = str(date or _today())
    if not events:
        return []
    dedup = _load_dedup()
    seen = set(dedup.get(target) or [])
    fresh = [e for e in events if e.get("code") and e["code"] not in seen]
    if not fresh:
        return []
    card = build_card(fresh, top_n=top_n, date=target, universe=universe)
    if dry_run:
        return fresh
    try:
        from config import send_feishu_payload
        ok = send_feishu_payload(
            card, success_log=f"猎手热门板块站上5日线飞书推送: {len(fresh)} 只",
            error_prefix="猎手热门板块站上5日线推送")
    except Exception as e:
        log.warning(f"⚠️ {_uni_cn(universe)}站上5日线推送异常（不写去重，下轮重试）: {type(e).__name__}: {str(e)[:180]}")
        return []
    if not ok:
        log.warning(f"⚠️ {_uni_cn(universe)}站上5日线推送返回失败（不写去重，下轮重试）")
        return []
    seen.update(e["code"] for e in fresh)
    dedup[target] = sorted(seen)
    _save_dedup(dedup)
    return fresh


def _in_live_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(14, 55))


def _alert_live_failure(now: datetime) -> None:
    """连续取数失败 → 飞书告警（owner：异常一定要发）。10 分钟节流。"""
    if now.timestamp() - float(_LIVE_FAIL.get("last_alert") or 0) < _LIVE_FAIL_ALERT_SEC:
        return
    _LIVE_FAIL["last_alert"] = now.timestamp()
    try:
        from config import send_feishu_payload
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"template": "red",
                           "title": {"tag": "plain_text", "content": "⚠️ 猎手站上5日线监控·取数失败"}},
                "elements": [{"tag": "markdown", "content":
                              "**实时快照连续 3 次未拿到**（腾讯批量）。\n"
                              "监控仍在重试；若持续失败请检查网络/数据源。\n"
                              f"时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"}],
            },
        }
        send_feishu_payload(card, success_log="猎手站上5日线监控·取数失败告警已推送",
                            error_prefix="猎手站上5日线监控取数失败告警")
    except Exception as e:
        log.warning(f"⚠️ 猎手站上5日线监控失败告警推送异常: {str(e)[:120]}")


def run_live_watch(poll_sec: float = None, top_n: int = None, stop_event=None,
                   provider=None, sleep_fn=_time.sleep, now_fn=datetime.now,
                   push_fn=None, universe: str = "pool") -> None:
    """独立实时 watcher：交易时段每 `poll_sec` 秒扫一次，命中即推。

    由 main.py 在启动时拉起的 daemon 线程调用；**不依赖 15 分钟主循环**。
    `provider` / `push_fn` / `sleep_fn` / `now_fn` 均为测试缝。
    """
    poll = float(poll_sec or _LIVE_POLL_SEC)
    day, below = None, set()
    saved = None
    log.info(f"[MA5实时] watcher 启动：每 {poll:.0f}s 一轮（交易时段）·"
             f"观察名单={('异常监控池' if universe == 'pool' else '猎手热门板块')}")
    while not (stop_event is not None and stop_event.is_set()):
        now = now_fn()
        if not _in_live_session(now):
            if sleep_fn:
                sleep_fn(30)
            continue
        today = now.strftime("%Y-%m-%d")
        if today != day:
            day = today
            below = set(_load_below().get(today) or [])
            saved = None
        ctx = _live_ctx(top_n, today, universe=universe)
        if not ctx or not ctx.get("codes"):
            if sleep_fn:
                sleep_fn(poll)
            continue
        try:
            hits, ok = scan_live_once(ctx, below, provider=provider, today=today)
        except Exception as e:
            hits, ok = [], False
            log.warning(f"[MA5实时] 扫描异常（继续下一轮）: {type(e).__name__}: {str(e)[:160]}")
        if below and below != saved:
            try:
                store = _load_below()
                store[today] = sorted(below)
                _save_below(store)
                saved = set(below)
            except Exception:
                pass
        if ok:
            _LIVE_FAIL["n"] = 0
        else:
            _LIVE_FAIL["n"] = int(_LIVE_FAIL.get("n") or 0) + 1
            if _LIVE_FAIL["n"] >= 3:
                _alert_live_failure(now)
        if hits:
            try:
                fresh = (push_fn or push_events)(hits, date=today, top_n=top_n,
                                                 universe=universe)
            except Exception as e:
                fresh = []
                log.warning(f"[MA5实时] 推送异常（下轮重试）: {str(e)[:160]}")
            for e in fresh or []:
                log.info(f"⬆️ 异常监控池·站上5日线: {e['code']} {e['name']} "
                         f"现价{e['price']} MA5={e['ma5']}(+{e['dev5_pct']}%) [{e['kind']}]")
        if sleep_fn:
            sleep_fn(poll)


def _uni_cn(universe: str) -> str:
    return "异常监控池" if universe != "hunter" else "猎手热门板块"


def build_card(events: list, top_n: int = None, date: str = None,
               universe: str = "pool") -> dict:
    if not events:
        return None
    tn = int(top_n or _TOPN_DEFAULT)
    n_over = sum(1 for e in events if e.get("kind") == "overnight")
    n_vrev = len(events) - n_over
    head = f"📅 {date or _today()} · 共 {len(events)} 只"
    if n_vrev:
        head += f"（隔夜回站 {n_over} · 盘内V反转 {n_vrev}）"
    lines = ([f"**异常监控池·「刚站上5日线」**（本次命中 {len(events)} 只）",
              head, ""] if universe != "hunter"
             else [f"**选股猎手·热门板块内「刚站上5日线」**（前 {tn} 板块）", head, ""])
    # 2026-10-16 owner：「飞书消息要有代码 + 中文名称 + 所属行业」——
    # 原来这里只写 `**{code}** {name}`（无行业）。真源 `core.stock_meta`（industry_map.csv）。
    from core.stock_meta import stock_label as _stock_label
    shown = events[:_CARD_MAX_ROWS]
    for e in shown:
        tag = " ｜盘内V反转" if e.get("kind") == "vrev" else ""
        lines.append(f"⬆️ **{_stock_label(e['code'], e['name'])}**  "
                     f"现价 {e['price']} ｜ MA5 {e['ma5']}（+{e['dev5_pct']}%）{tag}")
    rest = events[_CARD_MAX_ROWS:]
    if rest:
        # 折叠而不是丢弃：**同样给全 名称/代码/行业**（owner 要求的是"飞书消息要有"，
        # 不留例外）。这一段动辄几十只、行会很长，但它本来就是"扫一眼还有谁"的折叠区。
        lines.append("")
        lines.append(f"**另有 {len(rest)} 只**（偏离幅度较小）："
                     + " ".join(_stock_label(e["code"], e["name"]) for e in rest))
    lines += ["",
              "📌 口径：**隔夜回站**=昨收在 MA5 下、今日站上；**盘内V反转**=今日曾跌破 MA5 又拉回站上。",
              f"🔎 明细仅列前 {_CARD_MAX_ROWS} 只（按偏离幅度降序）。",
              "⚠️ 纯技术通知，不构成交易指令；请人工确认。"]
    return {
        "msg_type": "interactive",
        "card": {
            "header": {"template": "green",
                       "title": {"tag": "plain_text",
                                  "content": ("📈 异常监控池·刚站上5日线" if universe != "hunter"
                                              else "📈 猎手热门板块·刚站上5日线")}},
            "elements": [{"tag": "markdown", "content": "\n".join(lines)}],
        },
    }


def run_hunter_ma5_alert(top_n: int = None, date: str = None, dry_run: bool = False) -> list:
    """检测→去重→推飞书（历史/离线口径）。返回本次**新推**的事件。

    ⚠️ 盘中及时性由 `run_live_watch` 负责（独立线程、20s 一轮）；本函数保留给
    历史扫描/手工 CLI 与回归测试，推送逻辑已抽到 `push_events` 共用。
    """
    target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    hits = scan_hunter_ma5(date=date, top_n=top_n)
    return push_events(hits, date=target, top_n=top_n, dry_run=dry_run)
