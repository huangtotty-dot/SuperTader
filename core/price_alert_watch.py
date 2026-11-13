# -*- coding: utf-8 -*-
"""价格突破提醒 watcher（2026-11-13 从 main.py 抽出）。

K 线图上画价位线 → **每次**穿越都推飞书。为什么盯在独立模块而不是 GUI：飞书是
"人不在屏幕前"的兜底，GUI 关掉也得能报。响声由 GUI 轮询 `t_io/state/price_alerts.json`
的 events 后播放；两边共用 `core.price_alerts`。

与盯盘热路径零引擎状态耦合：只依赖 config（log/load_runtime_config/send_feishu_payload/
FEISHU_KEYWORD）+ core.feishu_cards + 懒加载的 core.price_alerts/stage_gate/concept_tags/
market_data.quote_bus。故整体搬到本模块，main.py 只留 `_start_price_alert_watch()` 调用点。
"""
from __future__ import annotations

import json
import os
import threading as _threading
import time as _time
from datetime import datetime, time as dtime

from config import FEISHU_KEYWORD, load_runtime_config, log, send_feishu_payload
from core.feishu_cards import feishu_card_header as _feishu_card_header
from core.feishu_cards import feishu_md_div as _feishu_md_div

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_price_alert_thread = None


def _start_price_alert_watch() -> None:
    global _price_alert_thread
    try:
        if _price_alert_thread is not None and _price_alert_thread.is_alive():
            return
        _price_alert_thread = _threading.Thread(target=_price_alert_loop, daemon=True,
                                                name="price-alert-watch")
        _price_alert_thread.start()
        log.info("🔔 价格突破提醒: 已启动（10s 一拍，读 t_io/state/price_alerts.json）")
    except Exception as e:
        log.warning(f"⚠️ 价格突破提醒启动失败（不阻断主循环）: {str(e)[:160]}")


def _price_alert_loop() -> None:
    while True:
        try:
            _price_alert_tick()
        except Exception as e:
            log.warning(f"⚠️ 价格突破提醒异常（已吞掉）: {type(e).__name__}: {str(e)[:160]}")
        _time.sleep(10)


def _price_alert_tick() -> None:
    """一拍：读全部框 → 拉快照 → 判穿越 → 推飞书。

    **每次穿越都报**（owner 2026-10-28：「价格有可能全天波动 一会在价格上面一会在价格下面
    需要连续报 不要只报一次」）。同侧要价格先退回框内才重新可报，避免贴边抖动刷屏 ——
    判定与去抖都在 `price_alerts.evaluate()` 里，这里只负责取价和推送。
    """
    now = datetime.now()
    t = now.time()
    if now.weekday() >= 5 or not (dtime(9, 15) <= t <= dtime(15, 5)):
        return          # 非交易时段不判：收盘价会把"线本来就在下方"的提醒瞬间全部触发
    from core import price_alerts as pa
    todo = pa.armed()
    if not todo:
        return
    codes = sorted({x["code"] for x in todo})
    try:
        from core.market_data.quote_bus import get_quotes
        snap = get_quotes(codes, max_age_sec=8.0) or {}
    except Exception as e:
        log.debug(f"价格突破提醒快照失败: {type(e).__name__}: {str(e)[:80]}")
        return
    hits = []
    for x in todo:
        s = snap.get(x["code"]) or {}
        try:
            px = float(s.get("price") or 0)
        except Exception:
            continue
        if px <= 0:
            continue
        item = x["item"]
        # 记价 + 判穿越 + 去抖 + 落事件，全在 evaluate 里一次做完（原子，不跨两拍）
        hits.extend(pa.evaluate(x["code"], item.get("id"), px, name=s.get("name") or ""))
    if hits:
        _push_price_alert(hits)


def _price_alert_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled + notify_on_price_alert（2026-11-04 新增）。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        if not bool(feishu_cfg.get("enabled", True)):
            return False
        return bool(feishu_cfg.get("notify_on_price_alert", True))
    except Exception:
        return True


# 2026-11-04 owner：「飞书弹出的消息太多」——突破预警一天 66 条（同票来回穿框反复推）。
# 节流三条：同票同向 10 分钟冷却；全局两条至少隔 60s、每小时 ≤30 条；
# 被压住的先攒着，下一次可发时**合并成一条**（不丢事件）。状态落盘，重启沿用。
_PRICE_ALERT_COOLDOWN_SEC = 600
_PRICE_ALERT_MIN_GAP_SEC = 120
_PRICE_ALERT_HOURLY_MAX = 30
_PRICE_ALERT_PENDING_MAX = 12
_PUSH_RATE_FP = os.path.join(_ROOT, "t_io", "state", "push_rate.json")


def _load_push_rate(today: str) -> dict:
    d = {}
    try:
        with open(_PUSH_RATE_FP, encoding="utf-8") as f:
            d = json.load(f) or {}
    except Exception:
        d = {}
    if str(d.get("day") or "") != today:
        d = {"day": today, "per": {}, "times": [], "pending": []}
    d.setdefault("per", {})
    d.setdefault("times", [])
    d.setdefault("pending", [])
    return d


def _save_push_rate(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_PUSH_RATE_FP), exist_ok=True)
        tmp = _PUSH_RATE_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, _PUSH_RATE_FP)
    except Exception:
        pass


def _push_price_alert(hits: list, now_ts: float = None) -> None:
    """画框突破推送（2026-11-04 加节流：同票同向冷却 + 全局限流 + 积压合并）。"""
    if not _price_alert_feishu_enabled():
        return
    # 2026-11-04 owner：「飞书播报的信息目前仅限于多头的股票」——非日线多头不播（判不了 ⇒ 放行）。
    # ⚠️ 即便 hits 全被滤掉也**不能 return**：调用方会用 `hits=[]` 来"冲刷积压"（下面还要合并发）。
    try:
        from core.stage_gate import filter_bull
        hits, _drop = filter_bull(hits)
        if _drop:
            log.info("[突破预警] 过滤非日线多头 %d 只: %s",
                     len(_drop), [h.get("code") for h in _drop])
    except Exception as e:                     # noqa: BLE001  闸门异常不得阻断告警
        log.warning("[突破预警] 多头过滤异常（按原样播报）: %s: %s", type(e).__name__, str(e)[:120])
    try:
        now = datetime.now()
        _ts = float(now_ts if now_ts is not None else now.timestamp())
        _now = datetime.fromtimestamp(_ts) if now_ts is not None else now
        today = _now.strftime("%Y-%m-%d")
        st = _load_push_rate(today)
        per, pending, times = st["per"], st["pending"], st["times"]
        for h in hits:
            key = f"{h.get('code')}|{h.get('side')}"
            if _ts - float(per.get(key) or 0) < _PRICE_ALERT_COOLDOWN_SEC:
                continue                       # 同票同向 10 分钟内不重复
            per[key] = _ts
            pending.append(h)
        if not pending:
            _save_push_rate(st)
            return
        times = [t for t in times if _ts - float(t) < 3600]
        st["times"] = times
        _last = max(times) if times else 0.0
        _end_of_day = _now.strftime("%H:%M") >= "15:00"
        _due = (_end_of_day
                or len(pending) >= _PRICE_ALERT_PENDING_MAX
                or (_ts - _last >= _PRICE_ALERT_MIN_GAP_SEC
                    and len(times) < _PRICE_ALERT_HOURLY_MAX))
        if not _due:
            _save_push_rate(st)                # 攒到下一拍合并
            return
        # 2026-11-02 owner：「飞书推送的这个信息要注明来自概念板块的哪个板块」——
        # 主板块口径与猎手/风向标同源（core.concept_tags，有测试逐码守卫）；读不到就只少这个标签。
        from core.concept_tags import label_of
        lines = ["**画框提醒 · 价格刚穿出你画的框**", ""]
        for h in pending:
            _ico = "🔔 向上突破" if h.get("side") == "up" else "🔻 向下跌破"
            _n = int(h.get("n") or 1)
            _lab = label_of(h.get("code"))
            _tag = ""
            if _lab.get("board"):
                _tag = f"　🏷 **{_lab['board']}**"
                if _lab.get("concepts"):
                    _tag += f"（{' · '.join(_lab['concepts'])}）"
            lines.append(f"{_ico} **{h['name']}（{h['code']}）**　现价 {h['price']}"
                         f"（框沿 {h['level']}）"
                         + (f"　今天第 {_n} 次" if _n > 1 else "")
                         + _tag
                         + f"　`{str(h.get('ts') or '')[11:19]}`")
        if len(pending) > 1:
            lines += ["", f"ℹ️ 同票同向 10 分钟内只报一次，本条合并了 {len(pending)} 次穿越"]
        card = {"msg_type": "interactive", "notify_type": 1, "card": {
            "config": {"wide_screen_mode": True},
            "header": _feishu_card_header(f"🔔 突破预警 - {FEISHU_KEYWORD}", "red"),
            "elements": [_feishu_md_div("\n".join(lines))]}}
        ok = send_feishu_payload(card,
                                 success_log=f"✅ 突破预警已推送: {[h['code'] for h in pending]}",
                                 error_prefix="突破预警推送")
        if ok:
            st["pending"] = []
            st["times"].append(_ts)
        _save_push_rate(st)                    # 失败保留积压，下一拍重试
    except Exception as e:
        log.warning(f"⚠️ 突破预警推送失败: {type(e).__name__}: {str(e)[:120]}")
