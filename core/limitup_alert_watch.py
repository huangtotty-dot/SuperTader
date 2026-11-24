# -*- coding: utf-8 -*-
"""全市场涨停 · 飞书实时播报（2026-11-24 owner：「有涨停的个股第一时间推给我」）。

口径（owner 2026-11-24 拍板）：
  · 范围 = **全市场**（不限持仓/股池）；
  · 同一只票**回封也推**（开板后再封 = 新事件）；一直封着的不重复；
  · 节拍 10s：同花顺涨停聚焦 dataapi（`limit_up_pool`，翻页拉全，~2 页/次）；
  · 每拍至多一张卡片（同一拍里封的多只合并），发送失败**不推进状态** ⇒ 下一拍自动重试。

状态文件 `t_io/state/limitup_alert_state.json` = {"date": "YYYYMMDD", "codes": [...]}，
记录**上一次轮询**的池子快照（不是"已推送"集合）：
  · 新封 = 本次池子 − 上次快照；
  · 开板后再封 ⇒ 该票先从上一次快照消失、再出现 ⇒ 自然算"新封"；
  · 同日 09:31 之后进程重启：首拍只**静默接管**快照，不把当时的整池再推一遍。

与盯盘主循环零引擎耦合：只读同花顺公开接口 + 复用 `config.send_feishu_payload` /
`core.feishu_cards` / `core.concept_tags.label_of`；任何异常都吞掉，绝不影响交易主循环。
"""
from __future__ import annotations

import json
import os
import random
import threading as _threading
import time as _time
from datetime import datetime

from config import FEISHU_KEYWORD, log, send_feishu_payload
from core.feishu_cards import feishu_card_header as _feishu_card_header
from core.feishu_cards import feishu_md_div as _feishu_md_div

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_FP = os.path.join(_ROOT, "t_io", "state", "limitup_alert_state.json")

_POLL_SEC = 10
_URL = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
]
_MAX_LINES = 12          # 单卡最多列出的只数（超出的收起为"另 N 只"）
_SILENT_TAKEOVER_HM = "09:31"   # 同日此时间之后重启 ⇒ 首拍静默接管，不重推整池

_thread = None


def _in_window(now: datetime) -> bool:
    """只在工作日 09:20~15:05 轮询（集合竞价起；收盘后池子不再变，推了也没意义）。"""
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return "09:20" <= hm <= "15:05"


def _load_state() -> dict:
    try:
        with open(_STATE_FP, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(date_yyyymmdd: str, codes) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_FP), exist_ok=True)
        tmp = _STATE_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": date_yyyymmdd, "codes": sorted(codes)}, f,
                      ensure_ascii=False)
        os.replace(tmp, _STATE_FP)
    except Exception:
        pass


def _poll_pool(date_yyyymmdd: str):
    """同花顺涨停聚焦 → 当日全市场涨停清单；**网络失败返回 None**（≠ 空池）。

    返回 [{code, name, tag, again, rate}]；tag ∈ FIRST_LIMIT/LIMIT_BACK，
    again=is_again_limit（1=回封），rate=涨跌幅%（用来标 20cm/30cm）。
    """
    try:
        import requests
    except Exception:
        return None
    out, page, guard = [], 1, 0
    while page <= 5 and guard < 10:
        guard += 1
        params = {"page": page, "limit": 200, "field": "199112", "filter": "HS,GEM2STAR",
                  "order_field": "330329", "order_type": 0, "date": date_yyyymmdd,
                  "_": int(_time.time() * 1000)}
        headers = {"User-Agent": random.choice(_UAS), "Referer": "http://data.10jqka.com.cn/",
                   "cookie": "v=AaBb1234567890abcdefghijklmnopqrstuv"}
        got = False
        for attempt in range(2):
            try:
                r = requests.get(_URL, params=params, headers=headers, timeout=8.0)
                j = r.json() if r.status_code == 200 and r.content else {}
                if j.get("status_code") != 0:
                    _time.sleep(0.4 + attempt)
                    continue
                info = (j.get("data") or {}).get("info") or []
                for x in info:
                    code = str(x.get("code") or "").zfill(6)
                    if len(code) != 6 or not code.isdigit():
                        continue
                    try:
                        rate = float(x.get("change_rate") or 0.0)
                    except Exception:
                        rate = 0.0
                    out.append({"code": code, "name": str(x.get("name") or ""),
                                "tag": str(x.get("change_tag") or ""),
                                "again": bool(x.get("is_again_limit")),
                                "rate": rate})
                got = True
                page = 99 if len(info) < 200 else page + 1
                break
            except Exception:
                _time.sleep(0.4 + attempt)
        if not got:
            return None
    return out


def _tag_txt(it: dict) -> str:
    # THS 只给「首板 / 非首板」下界（连板数恒 null）⇒ 非首板统一标「连板/回封」，不猜具体板数
    txt = ("首板" if it.get("tag") == "FIRST_LIMIT"
           else ("连板/回封" if it.get("again") else "连板"))
    rate = float(it.get("rate") or 0.0)
    if rate >= 29.0:
        txt += " · 30cm"
    elif rate >= 19.0:
        txt += " · 20cm"
    return txt


def _build_card(new_items, now: datetime):
    """一拍的新涨停 → 飞书卡片（同拍多只合并；最多列 _MAX_LINES 只）。"""
    try:
        from core.concept_tags import label_of
    except Exception:
        label_of = lambda code: {}      # noqa: E731
    lines = [f"**全市场涨停 · {len(new_items)} 只新封**", ""]
    for it in new_items[:_MAX_LINES]:
        tag = ""
        try:
            lab = label_of(it["code"]) or {}
            if lab.get("board"):
                tag = f"　🏷 **{lab['board']}**"
                if lab.get("concepts"):
                    tag += f"（{' · '.join(lab['concepts'][:2])}）"
        except Exception:
            tag = ""
        lines.append(f"⚡ **{it.get('name') or it['code']}（{it['code']}）**"
                     f"　{_tag_txt(it)}{tag}　`{now.strftime('%H:%M:%S')}`")
    if len(new_items) > _MAX_LINES:
        lines += ["", f"…另 {len(new_items) - _MAX_LINES} 只（见同花顺涨停池）"]
    lines += ["", f"口径：同花顺涨停聚焦（全市场）· 每 {_POLL_SEC}s 轮询，回封也报；"
                  f"本拍 +{len(new_items)}"]
    return {"msg_type": "interactive", "notify_type": 1, "card": {
        "config": {"wide_screen_mode": True},
        "header": _feishu_card_header(f"⚡ 涨停快报 - {FEISHU_KEYWORD}", "red"),
        "elements": [_feishu_md_div("\n".join(lines))]}}


def _tick(now: datetime | None = None) -> list:
    """一拍：拉池 → 对比上次快照 → 新封推送。返回本次新封清单（便于测试/日志）。"""
    now = now or datetime.now()
    if not _in_window(now):
        return []
    d = now.strftime("%Y%m%d")
    pool = _poll_pool(d)
    if pool is None:
        return []                                  # 网络失败：不动状态，下一拍重试
    codes = {x["code"] for x in pool}
    st = _load_state()
    same_day = str(st.get("date") or "") == d
    prev = set(st.get("codes") or []) if same_day else set()
    if not same_day and now.strftime("%H:%M") > _SILENT_TAKEOVER_HM:
        _save_state(d, codes)                      # 盘中重启：静默接管，不重推整池
        return []
    new_items = [x for x in pool if x["code"] not in prev]
    if not new_items:
        _save_state(d, codes)
        return []
    ok = send_feishu_payload(_build_card(new_items, now),
                             success_log=f"✅ 涨停快报已推送: {[x['code'] for x in new_items]}",
                             error_prefix="涨停快报推送")
    if ok:
        _save_state(d, codes)                      # 只有推成功才推进快照 ⇒ 失败下拍重试
    return new_items


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as e:
            log.warning(f"⚠️ 涨停快报异常（已吞掉）: {type(e).__name__}: {str(e)[:160]}")
        _time.sleep(_POLL_SEC)


def _start_limitup_alert_watch() -> None:
    """main.py 启动时调用：独立 daemon 线程，失败只告警不阻断主循环。"""
    global _thread
    try:
        if _thread is not None and _thread.is_alive():
            return
        _thread = _threading.Thread(target=_loop, daemon=True, name="limitup-alert-watch")
        _thread.start()
        log.info(f"⚡ 涨停快报: 已启动（全市场，{_POLL_SEC}s 一拍，回封也报）")
    except Exception as e:
        log.warning(f"⚠️ 涨停快报启动失败（不阻断主循环）: {str(e)[:160]}")
