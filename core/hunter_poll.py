# -*- coding: utf-8 -*-
"""选股猎手 · 30 分钟轮询 + **异常飞书告警**（2026-10-11）
================================================================================
owner 原话：「在实盘过程中，选股猎手应该每隔 30 分钟做轮询，**重点中的重点：异常信息一定要通过飞书发出来**」。

设计
----
· **由 main.py 主循环驱动**（无头、不依赖 GUI 是否打开）——见 `main._maybe_run_hunter_poll`。
· 每轮**重跑猎手取数 + 打分**（`prepare_data` → `score_stocks`），随后**校验结果**；
  任何异常 / 数据异常 ⇒ 飞书告警。
· **只发异常，不发常规报告**（30 分钟推一次报告会刷屏）；按 (日期, 异常类型) 去重。
· 推送走 **`config.send_feishu_payload`，必传三参并检查返回值** —— 本仓有过血案：
  `core/hunter_ma5_alert.py` 里记着「2026-10-10 前这里只传了 card，每次都抛 TypeError，
  又被裸 except 吞掉 ⇒ 本告警**自上线起从未推送成功过一次**」。
  故本模块：**不写裸 except**；推送失败**不写去重**（下轮重试）；失败原因打日志。

⚠️ 本模块只**读**猎手数据并告警，**不触发任何交易**。
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
import traceback
from contextlib import redirect_stdout
from datetime import datetime

log = logging.getLogger("hunter_poll")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HUNTER_DIR = os.path.join(_BASE, "stock_hunter")
_DEDUP_FP = os.path.join(_BASE, "t_io", "state", "hunter_poll_alerts.json")
_MIN_COVERAGE = 0.5          # 行情覆盖率下限（低于此判"行情拉取异常"）
_DEDUP_KEEP = 10             # 去重文件保留最近 N 天

# 飞书短标签（消息要短；长标签只在日志/文档里用）
_KIND_SHORT = {
    "run_exception": "跑挂了",
    "empty_watchlist": "池未加载",
    "empty_pool": "打分结果空",
    "empty_summary": "板块排名空",
    "low_coverage": "行情覆盖低",
    "stale_date": "数据非当日",
}
_MAX_PER_KIND_PER_DAY = 5      # 抖动上限：同类一天最多报几次

_KIND_LABEL = {
    "run_exception": "轮询执行异常",
    "empty_watchlist": "watchlist 未加载",
    "empty_pool": "打分池为空",
    "empty_summary": "板块排名为空",
    "low_coverage": "行情覆盖率过低",
    "stale_date": "数据日期不是今天",
}


def _ensure_path() -> None:
    """猎手模块在 `stock_hunter/` 下，按需加进 `sys.path`。

    ⚠️ 必须 **append**、不能 `insert(0)`：`stock_hunter/main.py` 与本仓**根目录的 `main.py`
    （调度器）同名**，插到最前面会把 `import main` 劫持成猎手的 CLI（实测踩到：
    `main._maybe_run_hunter_poll` 变成 AttributeError）。根目录没有 `modules/` 包，
    故 append 不会让猎手的 `modules.*` 解析失败。
    """
    if _HUNTER_DIR not in sys.path:
        sys.path.append(_HUNTER_DIR)


def _coverage(watchlist_df) -> float:
    """行情覆盖率 = 打分池里拿到有效成交额/涨跌幅的比例。"""
    try:
        import pandas as pd
        df = watchlist_df.copy()
        if "韭研概念" in df.columns:
            df = df[df["韭研概念"].astype(str).str.strip().ne("")]
        if df.empty:
            return 0.0
        ok = 0
        for col in ("成交额", "涨跌幅"):
            if col in df.columns:
                v = pd.to_numeric(df[col], errors="coerce")
                ok = max(ok, int(v.notna().sum()))
        return ok / len(df)
    except Exception:
        return 0.0


def poll_once(date_str: str = None) -> dict:
    """跑一轮：猎手取数 + 打分 + 校验。**不抛**，异常都收进 `anomalies`。

    返回 {"ok": bool, "anomalies": [{"kind","msg"}], "stats": {...}}
    """
    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    res = {"ok": True, "anomalies": [], "stats": {"date": date_str}}

    def _bad(kind: str, msg: str) -> None:
        res["ok"] = False
        res["anomalies"].append({"kind": kind, "msg": str(msg)[:400]})

    try:
        _ensure_path()
        import stock_hunter.main as H          # noqa: N813  （`if __name__` 有守卫，import 安全）
        cfg = H.load_config(os.path.join(_HUNTER_DIR, "config.json"))
        buf = io.StringIO()                     # 猎手管线 print 很多，别刷主日志
        with redirect_stdout(buf):
            # ⚠️ `prepare_data` 的 docstring 写"返回 dict"，实际正常路径是 **`(data, loader)`** 元组
            #（watchlist 缺失时才早退返回 dict）⇒ 两种形态都要收。
            _out = H.prepare_data(cfg, date_str)
            data, loader = (_out if isinstance(_out, tuple) else (_out, None))
            scored = H.score_stocks(data, cfg)
            # ⚠️ `summary/detail/sectors` **不在 prepare_data 里填**——CLI 是在 main() 里用
            # 第二个返回值 loader 现填的（prepare_data 返回 loader 正是为此）。不填就会
            # 误报"板块排名为空"（本探针实测踩到）。这里照 CLI 的顺序补齐，校验的才是真产物。
            # ⚠️ 刻意**不调** `compute_heat_scores`：它名字像纯计算，却会**无条件落盘生产历史**。
            if loader is not None:
                data["summary"] = loader.load_concept_summary()
                data["detail"] = loader.load_detail_ranking()
                data["sectors"] = loader.load_all_sectors()
        tail = buf.getvalue().strip().splitlines()[-8:]
        res["stats"]["pipeline_tail"] = tail
        res["stats"]["scored"] = len(scored or [])
    except Exception as e:
        _bad("run_exception", f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
        return res

    wl = data.get("watchlist")
    if wl is None or getattr(wl, "empty", True):
        _bad("empty_watchlist", "watchlist_jiuyan.json 未加载或为空")
        return res
    cov = _coverage(wl)
    res["stats"]["coverage"] = round(cov, 3)
    res["stats"]["pool_rows"] = int(len(wl))
    if not scored:
        _bad("empty_pool", "打分结果为空（打分池可能无有效行情）")
    summary = data.get("summary")
    if summary is None or getattr(summary, "empty", True):
        _bad("empty_summary", "板块排名(summary)为空")
    if cov < _MIN_COVERAGE:
        _bad("low_coverage", f"行情覆盖率仅 {cov*100:.0f}%（阈值 {_MIN_COVERAGE*100:.0f}%）"
                             f"⇒ 行情源可能不可用")
    # 盘中轮询时，数据日期必须是今天（历史日轮询不适用该判据）
    if date_str == datetime.now().strftime("%Y-%m-%d"):
        _ads = data.get("actual_data_dates") or set()
        if _ads and date_str not in {str(x)[:10] for x in _ads}:
            _bad("stale_date", f"实际数据日期 {sorted(map(str, _ads))[:3]} 不含今天 {date_str}")
    return res


def format_anomaly_card(res: dict, date_str: str, now: datetime = None) -> dict:
    """异常 → 飞书**精简**文本（owner 2026-10-11：实时性不能动，但消息要短）。

    卡片只留 3 类信息：**什么时候 / 哪里坏了 / 一行关键数字**。
    traceback 与管线输出**不进卡片**（那是手机上一堵墙），完整细节在 `log` 里。
    """
    now = now or datetime.now()
    lines = [f"⚠️ 猎手轮询异常 · {now.strftime('%m-%d %H:%M')}"]
    for k, a in enumerate(res.get("anomalies", [])[:4], 1):
        head = _KIND_SHORT.get(a["kind"], a["kind"])
        # 只取首行、压掉换行与 traceback，单行 ≤70 字
        msg = " ".join(str(a.get("msg") or "").split())
        msg = msg.split("Traceback")[0].strip()
        if len(msg) > 70:
            msg = msg[:70] + "…"
        lines.append(f"{'①②③④'[k-1]} {head}：{msg}")
    st = res.get("stats") or {}
    bits = []
    if st.get("pool_rows"):
        bits.append(f"池{st['pool_rows']}")
    if st.get("scored") is not None:
        bits.append(f"打分{st['scored']}")
    if st.get("coverage") is not None:
        bits.append(f"覆盖{float(st['coverage'])*100:.0f}%")
    if bits:
        lines.append("· " + " ".join(bits))
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def _load_dedup() -> dict:
    try:
        with open(_DEDUP_FP, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_dedup(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_DEDUP_FP), exist_ok=True)
        keep = sorted(d)[-_DEDUP_KEEP:]
        tmp = _DEDUP_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: d[k] for k in keep}, f, ensure_ascii=False)
        os.replace(tmp, _DEDUP_FP)
    except Exception as e:
        log.warning(f"⚠️ 猎手轮询去重写入失败（不影响本轮告警）: {e}")


def push_anomalies(res: dict, date_str: str = None) -> bool:
    """把**新出现**的异常推飞书。返回是否推送成功。

    去重 = **按状态变化**（owner 2026-10-11：实时性很重要，不能"一天只报一次"）：
      · 异常**出现**（上一轮没有）⇒ 报；
      · **持续存在** ⇒ 不重复报（30 分钟一轮不会刷屏）；
      · **恢复后再次出现** ⇒ 再报（这才叫实时）；
      · 同类一天最多 `_MAX_PER_KIND_PER_DAY` 次（防抖动）。
    健康轮询 ⇒ 清掉"上轮出现过"的记录，供下次判定"再次出现"。
    **推送失败不写状态** ⇒ 下一轮重试。
    """
    date_str = date_str or (res.get("stats") or {}).get("date") \
        or datetime.now().strftime("%Y-%m-%d")
    st = _load_dedup()
    day = st.get(date_str) or {}
    if isinstance(day, list):                     # 兼容旧格式 [kinds]
        day = {"last": day, "counts": {}}

    if res.get("ok"):
        # 恢复正常：清 last（保留 counts），这样**再次**坏掉能重新告警
        if day.get("last"):
            day["last"] = []
            st[date_str] = day
            _save_dedup(st)
        return False

    kinds = sorted({a["kind"] for a in res.get("anomalies", [])})
    last = set(day.get("last") or [])
    counts = dict(day.get("counts") or {})
    fresh = [k for k in kinds
             if k not in last and counts.get(k, 0) < _MAX_PER_KIND_PER_DAY]
    if not fresh:
        log.info(f"猎手轮询异常（{','.join(kinds)}）持续中或已达当日上限，跳过")
        # 持续存在也要把 last 记上（否则下轮又判成"新出现"）
        day["last"] = kinds
        st[date_str] = day
        _save_dedup(st)
        return False
    fresh_res = {"ok": False, "stats": res.get("stats"),
                 "anomalies": [a for a in res["anomalies"] if a["kind"] in fresh]}
    card = format_anomaly_card(fresh_res, date_str)
    # ⚠️ 必传 success_log / error_prefix（本仓曾因少传被 TypeError+裸 except 静默吞掉）
    try:
        from config import send_feishu_payload
        ok = send_feishu_payload(
            card, success_log=f"猎手轮询异常飞书推送: {','.join(fresh)}",
            error_prefix="猎手轮询异常推送")
    except Exception as e:
        log.warning(f"⚠️ 猎手轮询异常推送抛异常（不写去重，下轮重试）: "
                    f"{type(e).__name__}: {str(e)[:180]}")
        return False
    if not ok:
        log.warning("⚠️ 猎手轮询异常推送返回失败（不写去重，下轮重试）")
        return False
    for k in fresh:
        counts[k] = counts.get(k, 0) + 1
    day["last"] = kinds                      # 本轮**全部**异常都记为"出现过"
    day["counts"] = counts
    st[date_str] = day
    _save_dedup(st)
    return True


POLL_INTERVAL_SEC = 1800       # 30 分钟一轮（owner 指定）
_MORNING = ((9, 30), (11, 30))
_AFTERNOON = ((13, 0), (15, 0))


def in_trading_hours(now: datetime) -> bool:
    """交易时段闸：工作日 09:30-11:30 / 13:00-15:00。"""
    if now.weekday() >= 5:
        return False
    t = (now.hour, now.minute)
    return (_MORNING[0] <= t <= _MORNING[1]) or (_AFTERNOON[0] <= t <= _AFTERNOON[1])


def should_poll(now: datetime, last: datetime = None,
                interval_sec: int = POLL_INTERVAL_SEC) -> bool:
    """是否该起一轮：①交易时段 ②距上轮 ≥ interval（默认 30 分钟）。

    放在 core 里是为了**可测**：根 `main.py` 导入很重（且 `stock_hunter/main.py` 同名，
    `sys.path` 顺序一变 `import main` 就解析错），调度闸不宜只活在 main.py 里。
    """
    if not in_trading_hours(now):
        return False
    if last is not None and (now - last).total_seconds() < interval_sec:
        return False
    return True


def run_and_alert(date_str: str = None) -> dict:
    """轮询 + 告警一步到位（供 main.py 调度线程调用）。**不抛**。"""
    res = poll_once(date_str)
    if not res["ok"]:
        log.warning(f"⚠️ 选股猎手轮询发现 {len(res['anomalies'])} 项异常: "
                    f"{[a['kind'] for a in res['anomalies']]}")
        # 飞书卡片只发精简版 ⇒ **完整细节必须落日志**（含 traceback），别让信息凭空消失
        for a in res["anomalies"]:
            log.warning(f"   · [{a['kind']}] {a['msg']}")
        try:
            pushed = push_anomalies(res, date_str)
            res["pushed"] = bool(pushed)
        except Exception as e:
            # 告警链路自身出问题也必须可见（但不要再抛）
            log.error(f"❌ 猎手轮询告警推送链路异常: {type(e).__name__}: {e}")
            res["pushed"] = False
    return res
