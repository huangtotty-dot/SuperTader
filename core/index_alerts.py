# -*- coding: utf-8 -*-
"""大盘态势判定钩子（2026-11-13 从 main.py 抽出）：尾盘评分落库 / 盘中分时预警 / 指数背离提醒。

三者共用 `_index_regime_feishu_enabled`（feishu.enabled 闸）。⚠️ 共享状态注意：
`_INDEX_INTRADAY_ACTIVE_ALERTS` / `_INDEX_INTRADAY_ACTIVE_ALERTS_BY_BOARD` 会被本模块**重绑定**
（`= 新对象`），main.py 的 scan_once 仍要读它们 ⇒ main.py 必须以**模块属性**方式读
（`_ia._INDEX_INTRADAY_ACTIVE_ALERTS_BY_BOARD`），不能用 `from ... import`（那是快照）。
`_code_intraday_board` 是稳定函数引用，scan_once 直接 `from ... import` 即可。
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime

from config import HOLDINGS, INDEX_REGIME_CONTEXT, load_runtime_config, log, send_feishu_payload
from analysis.index_regime import detect_index_regime, push_index_regime_context
from analysis.index_regime_intraday import (
    IRI_DEFAULT_PARAMS, detect_intraday_alert, fetch_index_minutes_live,
)
from execution.daily_sentiment import push_daily_sentiment
from core.feishu_cards import (
    feishu_card_header as _feishu_card_header,
    feishu_hr as _feishu_hr,
    feishu_md_div as _feishu_md_div,
)
from core.paths import INDEX_DIVERGENCE_SEEN_FILE as _DIV_SEEN_FP

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_INDEX_INTRADAY_LAST_FETCH_TS = 0.0   # 分时数据拉取节流（至多 300 秒一次）
# V1.30: 盘中预警活动状态（注入引擎 feats["intraday_alerts"] 供展示/回溯）
_INDEX_INTRADAY_ACTIVE_ALERTS: list = []
_INDEX_INTRADAY_ACTIVE_TS: float = 0.0
# B-3(2026-09-07): 分板盘中预警活动状态 {板块指数码: [alerts]}——注入改按个股所属板取
_INDEX_INTRADAY_ACTIVE_ALERTS_BY_BOARD: dict = {}
_BOARD_INDEX_NAME = {"sh000001": "上证指数", "sz399001": "深证成指",
                     "sz399006": "创业板指", "sh000688": "科创50"}
# 2026-10-26（D 轮回归）：去重标记只在函数里 `global` 声明后**先读后写**，模块级从来没初始化过
# ⇒ 当日第一次调用必然 NameError，被 except 吞掉 ⇒ 推送一次都不会发。这里补上模块级初值。
_index_regime_eod_pushed_date = ""


def _index_regime_feishu_enabled() -> bool:
    """读取 config.json 的 feishu.enabled 开关。"""
    try:
        runtime_config = load_runtime_config()
        feishu_cfg = runtime_config.get("feishu", {}) if isinstance(runtime_config, dict) else {}
        return bool(feishu_cfg.get("enabled", True))
    except Exception:
        return True


def _maybe_record_index_regime_eod(now: datetime) -> None:
    """14:30-14:55 尾盘大盘评分**落库**（每日一次，mode="tail" 含当日 forming bar）。

    **2026-10-29 owner「这类消息删掉」后本函数不再推送任何飞书卡片**，只剩三件数据活：
      ① `detect_index_regime(mode="tail")` 出盘中预判；
      ② `push_index_regime_context` 更新 INDEX_REGIME_CONTEXT / SESSION_CONTEXT（timing_gate 要读）；
      ③ `push_daily_sentiment` 落 sentiment_daily.jsonl（每日复盘的情绪/板块/持仓段靠它）。
    删推送**不能**顺手删这三件 —— 那会让复盘缺段、门控拿不到上下文。"""
    global _index_regime_eod_pushed_date
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(14, 30) <= t <= dtime(14, 55)):
            return
        today = now.strftime("%Y-%m-%d")
        if _index_regime_eod_pushed_date == today:
            return
        _index_regime_eod_pushed_date = today              # 先占位防重复触发（无论成败）
        # mode="tail"：14:30 后当日态势已基本定型，腾讯日线含 forming bar；
        # 模块标注 estimate=true 且不写 state.json/trace（保持 EOD 状态机纯净，收盘落库为准）
        regime, score, ctx = detect_index_regime(mode="tail")
        try:
            push_index_regime_context(ctx)                 # 更新 INDEX_REGIME_CONTEXT / SESSION_CONTEXT
        except Exception as e:
            log.warning(f"⚠️ push_index_regime_context 异常: {str(e)[:80]}")
        # V3.0: 大盘热度×概念TOP3 合成卡片（后台线程计算+落盘，异常自吞不阻塞主循环）
        # 与飞书开关解耦：关推送仍须落盘，否则复盘的情绪/板块/持仓段整段缺失
        try:
            if 'push_daily_sentiment' in globals():
                push_daily_sentiment(now=now)
        except Exception as e:
            log.warning(f"⚠️ push_daily_sentiment 钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")
        # 2026-10-29 owner：「这类消息删掉」——同族的**尾盘大盘评分预判卡片也停发**。
        # ⚠️ 只删**推送**：上面 `push_index_regime_context`（INDEX_REGIME_CONTEXT / SESSION_CONTEXT）
        #    与 `push_daily_sentiment`（sentiment_daily.jsonl 落盘）照旧执行 —— 复盘要用的
        #    情绪/板块/持仓段落全靠它，删了会让每日复盘缺段。
        state_detail = (ctx.get("detail") or {}).get("state") or {}
        prev_regime = state_detail.get("prev_regime")
        switched = bool(prev_regime) and str(prev_regime) != str(ctx.get("regime"))
        log.info(f"尾盘大盘评分（不推送）: {ctx.get('regime_name')} S={ctx.get('score')} "
                 f"切换={switched} (mode=tail)")
    except Exception as e:
        log.warning(f"⚠️ 尾盘大盘评分预判钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


def _code_intraday_board(code: str) -> str:
    """个股 → 板块指数码（B-3 分板注入归因）。失败回落上证。"""
    try:
        from core.board_index import resolve_index as _ri
        return _ri(str(code))[0]
    except Exception:
        return "sh000001"


def _intraday_board_codes() -> list:
    """当前持仓覆盖的板块指数码集合（B-3：逐板盘中预警）；无持仓/读取失败回落 [sh000001]。"""
    try:
        from core.board_index import resolve_index as _ri
    except Exception:
        return ["sh000001"]
    codes = []
    _h = globals().get("HOLDINGS") or {}
    if isinstance(_h, dict):
        codes = [str(_c) for _c in _h if not str(_c).startswith("_")]
    if not codes:
        try:
            import json as _json
            fp = os.path.join(_ROOT, "t_io", "state", "holdings_manual.json")
            if os.path.exists(fp):
                _data = _json.load(open(fp, encoding="utf-8"))
                codes = [str(_c) for _c, _v in (_data.items() if isinstance(_data, dict) else [])
                         if isinstance(_v, dict) and int(_v.get("qty") or 0) > 0
                         and not str(_c).startswith("_")]
        except Exception:
            codes = []
    boards = set()
    for _c in codes:
        try:
            boards.add(_ri(_c)[0])
        except Exception:
            pass
    return sorted(boards) or ["sh000001"]


def _maybe_check_index_intraday_alert(now: datetime) -> None:
    """盘中 09:35-14:55 大盘分时预警**状态计算**（每轮检查；拉数据 300s 节流）。

    2026-10-27 owner：**取消飞书推送**（「大盘分时预警」卡不再发），只保留两个非通知副作用：
      · `save_daily_index_minutes()` —— 当日指数分钟落盘（复盘直接用，绕过 tushare T-1）
      · `_INDEX_INTRADAY_ACTIVE_ALERTS[_BY_BOARD]` —— 活动预警状态，按个股所属板块注入 daily_ctx
    B-3(2026-09-07)：按当前持仓所属板块逐指数检测（上证/深成/创业板/科创50 覆盖的板块）。
    成本：300s 节流内每板块拉一次分钟线（×N，N≤4），owner 已拍板接受。
    """
    global _INDEX_INTRADAY_LAST_FETCH_TS
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(9, 35) <= t <= dtime(14, 55)):
            return
        if dtime(11, 30) < t < dtime(13, 0):
            return
        now_ts = time.time()
        if now_ts - _INDEX_INTRADAY_LAST_FETCH_TS < 300:   # 拉取节流：至多 300 秒一次
            return
        _INDEX_INTRADAY_LAST_FETCH_TS = now_ts
        if not _index_regime_feishu_enabled():
            return
        # 宿主调参合并：index_regime_intraday 无 globals 合并机制，调用前把
        # config.py 的 INDEX_INTRADAY_PARAMS 合入模块内 IRI_DEFAULT_PARAMS。
        # 2026-10-26：config.py:694 已把 `INDEX_INTRADAY_PARAMS` **删除**（9 个键与模块默认值
        # 全相同，merge 本就是空操作），这里保留"有则合、没有就跳过"的写法：
        # 以前裸名靠 exec 命名空间带进来，改成正常 import 后是 NameError（并被 except 吞掉），
        # 现在显式从 config 取，没定义就为空 dict。
        try:
            _iip = getattr(sys.modules.get("config"), "INDEX_INTRADAY_PARAMS", None)
            if isinstance(_iip, dict):
                IRI_DEFAULT_PARAMS.update(_iip)
        except Exception:
            pass
        # B-3(2026-09-07): 按持仓所属板块逐指数盘中预警（不再只盯上证）
        _boards = _intraday_board_codes()
        _daily_regime = str(INDEX_REGIME_CONTEXT.get("regime", "range"))
        _daily_score = float(INDEX_REGIME_CONTEXT.get("score") or 0.0)
        # 2026-08-23: 大盘分时落盘缓存（供当日复盘直接用，绕过 tushare T-1 当日拿不到分钟）
        try:
            from core.market_review import save_daily_index_minutes as _save_idx_min
            _save_idx_min()
        except Exception:
            pass
        global _INDEX_INTRADAY_ACTIVE_ALERTS, _INDEX_INTRADAY_ACTIVE_TS, \
            _INDEX_INTRADAY_ACTIVE_ALERTS_BY_BOARD
        _active_by_board: dict = {}
        _all_alerts: list = []   # 活动预警行（含板块前缀；注入 daily_ctx）
        for _bc in _boards:
            try:
                _mb = fetch_index_minutes_live(_bc)
            except Exception:
                continue          # 单板分钟不可用 → 跳过该板，其余板块照常
            try:
                _res = detect_intraday_alert(
                    _mb,
                    daily_regime=_daily_regime,
                    daily_score=_daily_score,
                )
            except Exception:
                continue
            _al = _res.get("alerts") or []
            _active_by_board[_bc] = list(_al)
            for _a in _al:
                _ta = dict(_a)
                _ta["msg"] = f"【{_BOARD_INDEX_NAME.get(_bc, _bc)}】{_a.get('msg')}"
                _all_alerts.append(_ta)
        # V1.30: 活动预警状态（分板注入 + 兼容旧全局列表），45 分钟未刷新自动过期
        _INDEX_INTRADAY_ACTIVE_ALERTS_BY_BOARD = _active_by_board
        if _all_alerts:
            _INDEX_INTRADAY_ACTIVE_ALERTS = _all_alerts
            _INDEX_INTRADAY_ACTIVE_TS = now_ts
        elif _INDEX_INTRADAY_ACTIVE_ALERTS and now_ts - _INDEX_INTRADAY_ACTIVE_TS > 2700:
            _INDEX_INTRADAY_ACTIVE_ALERTS = []
    except Exception as e:
        log.warning(f"⚠️ 大盘分时预警钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")


_DIV_LAST_FETCH_TS = 0.0
_DIV_SEEN_TTL_DAYS = 10


def _div_seen_load() -> dict:
    """已推送背离事件表 {key: 首次推送日}。"""
    try:
        if os.path.exists(_DIV_SEEN_FP):
            d = json.load(open(_DIV_SEEN_FP, encoding="utf-8"))
            if isinstance(d, dict):
                return {str(k): str(v) for k, v in d.items()}
    except Exception:
        pass
    return {}


def _div_seen_save(seen: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_DIV_SEEN_FP), exist_ok=True)
        cutoff = (datetime.now() - timedelta(days=_DIV_SEEN_TTL_DAYS)).strftime("%Y-%m-%d")
        pruned = {k: v for k, v in seen.items() if str(v)[:10] >= cutoff}
        with open(_DIV_SEEN_FP, "w", encoding="utf-8") as f:
            json.dump(pruned, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _maybe_check_index_divergence(now: datetime) -> None:
    """指数板各指数 30min/60min/日线的顶/底背离 → 飞书提醒（**同一事件只推一次**）。

    ⚠️ 定位：**观察提示，不是交易信号**。仓内自研研究（t_io/validation/board_div/）结论：
    30min 底 +5.8pp 显著；30min 顶 −1.4pp 无边际；60min 顶/底不显著；日线未验证；
    **指数顶背离减仓规则至今未接线**。故卡片固定带证据分级 + 免责说明，且本函数**不触发任何交易动作**。
    ⚠️ 滞后：背离需 3 根 bar 确认（30min 线即峰值后 1.5 小时），存在固有延迟。
    """
    global _DIV_LAST_FETCH_TS
    try:
        t = now.time()
        if now.weekday() >= 5 or not (dtime(9, 35) <= t <= dtime(14, 55)):
            return
        if dtime(11, 30) < t < dtime(13, 0):
            return
        now_ts = time.time()
        if now_ts - _DIV_LAST_FETCH_TS < 300:      # 与分时预警各自独立节流
            return
        _DIV_LAST_FETCH_TS = now_ts
        if not _index_regime_feishu_enabled():
            return
        from analysis.index_divergence import detect_index_divergence
        alerts = (detect_index_divergence() or {}).get("alerts") or []
        if not alerts:
            return
        seen = _div_seen_load()
        today = now.strftime("%Y-%m-%d")
        fresh = [a for a in alerts if a.get("key") and a["key"] not in seen]
        if not fresh:
            return
        for a in fresh:
            seen[a["key"]] = today
        _div_seen_save(seen)

        lines = []
        for a in fresh:
            lines.append(f"**{a['index']}｜{a['freq']}｜{a['type']}背离**　"
                         f"证据 {a['tier']} {a['evidence']}")
            lines.append(f"> 事件时点 {a['time']}｜价 {a.get('price')}｜"
                         f"距今 {a.get('bars_ago')} 根 bar" + ("｜连续背离" if a.get("consec") else ""))
        lines.append(_feishu_hr())
        lines.append(f"⚠️ {fresh[0].get('disclaimer', '')}；{fresh[0].get('lag_note', '')}")
        types = {a.get("type") for a in fresh}
        template = "red" if types == {"顶"} else ("blue" if types == {"底"} else "orange")
        has_sig = any(a.get("significant") for a in fresh)
        title = ("📉 指数背离提醒（含 1 条有边际信号）" if has_sig else "📉 指数背离提醒（观察项）")
        card = {"config": {"wide_screen_mode": True},
                "header": _feishu_card_header(title, template),
                "elements": [_feishu_md_div(x) for x in lines]}
        send_feishu_payload(
            payload={"msg_type": "interactive", "card": card, "notify_type": 1},
            success_log=f"✅ 指数背离提醒已推送: {[a['key'] for a in fresh]}",
            error_prefix="指数背离提醒推送",
        )
    except Exception as e:
        log.warning(f"⚠️ 指数背离提醒钩子异常（已吞掉，不影响主循环）: {str(e)[:120]}")
