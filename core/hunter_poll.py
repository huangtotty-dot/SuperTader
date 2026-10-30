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
import re
import sys
import threading
import traceback
from contextlib import redirect_stdout
from datetime import datetime

log = logging.getLogger("hunter_poll")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HUNTER_DIR = os.path.join(_BASE, "stock_hunter")
_DEDUP_FP = os.path.join(_BASE, "t_io", "state", "hunter_poll_alerts.json")
# 行情覆盖率下限。**2026-10-12 由 0.5 收紧到 0.95**：0.5 是"整个行情源死掉"才够得着的阈值，
# 实测 32 个已存档交易日 `_coverage` **恒 = 1.0000**（min=max=中位=1.0，见 §7），
# 生产里见过的最低是 0.996 ⇒ 阈值 0.5 与实测差 50 个百分点，是**结构性死判据**（永不触发）。
# 0.95 留 5pp 余量：只有"池内 >5% 拿不到行情"才报，正常波动够不着。
_MIN_COVERAGE = 0.95
_DEDUP_KEEP = 10             # 去重文件保留最近 N 天

# 管线 stdout 里**已知常驻**的噪声：北交所日线四源全不通（见 memory/文档），每轮必然出现
# 若干条 "920xxx 获取失败"，报警它 = 每轮永久噪声。证据：2026-10-12 三次观测中出现的
# WARN 全是这一类（7 只 920xxx）。
_BSE_CODE_RE = re.compile(r"^(?:920\d{3}|43\d{4}|8[3-9]\d{4})$")

# 飞书短标签（消息要短；长标签只在日志/文档里用）
_KIND_SHORT = {
    "run_exception": "跑挂了",
    "empty_watchlist": "池未加载",
    "empty_pool": "打分结果空",
    "empty_summary": "板块排名空",
    "low_coverage": "行情覆盖低",
    "stale_date": "数据非当日",
    "data_warn": "管线告警",
}
_MAX_PER_KIND_PER_DAY = 5      # 抖动上限：同类一天最多报几次

_KIND_LABEL = {
    "run_exception": "轮询执行异常",
    "empty_watchlist": "watchlist 未加载",
    "empty_pool": "打分池为空",
    "empty_summary": "板块排名为空",
    "low_coverage": "行情覆盖率过低",
    "stale_date": "数据日期不是今天",
    "data_warn": "管线内部告警",
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


class _ThreadWarnCapture(logging.Handler):
    """把**本线程**发出的 WARNING+ 日志收进列表（供 `_pipeline_warnings` 用）。

    为什么必须收（2026-10-12 排查）：管线里有一类失败走 `log.warning` 而不是 `print`
    （实测 `core/market_data/bj_daily.py:100`「北交所日线不可得…」、
    `core/market_data/facade.py:256`「daily_many: 北交所首次取数失败…」），
    而 `redirect_stdout` **只换 `sys.stdout`** ⇒ logging 输出对轮询完全不可见
    （记录下来的 `pipeline_tail` 里确实没有这两条）。只调阈值治不了这个。

    ⚠️ 只收**本线程**的：轮询跑在后台线程，主线程/其它任务的 market_data 日志
    不该被算作"本轮轮询的退化"（`record.thread` 是产生该记录的线程 ident）。
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.lines = []
        self._tid = threading.get_ident()

    def emit(self, record):
        if record.thread != self._tid:
            return
        try:
            self.lines.append(str(record.getMessage()))
        except Exception:
            pass


# 已知「**已经处置过**」的噪声（2026-10-30 owner 截图报误报）：
#     `[WARN] tencent.daily 兜底丢弃未确认的当日快照根(<code>)`
#   —— 这是 2026-10-26（603200）事故后加的**安全闸在正常工作**：宁可今天缺一根，
#   也不把「收盘前的盘中快照」当收盘价交给下游。它每轮会对几十只票各打一行
#   （10-30 10:01 实测 37 条 / 打分 2623 只），按"管线告警"报警 ⇒ **每个交易日
#   上午必响一次假警报**。
#   处置：**不进异常列表**，但把条数记进 stats 并留一行日志 —— 数量异常放大时
#   （说明缓存被批量写入未确认的当日根）仍看得见，只是不再打扰。
_HANDLED_NOISE_RE = re.compile(r"tencent\.daily 兜底丢弃未确认的当日快照根")


def _handled_noise_count(lines) -> int:
    return sum(1 for ln in lines or [] if _HANDLED_NOISE_RE.search(str(ln)))


def _is_known_dead_line(s: str) -> bool:
    """该行是否**只**涉及已知常驻不可得的标的（北交所：日线四源全不通）。

    ⚠️ 按**标的**豁免而不是按整行豁免：`"3 只获取失败: 920371, 000001"` 这种混着真失败的
    行不能一起被吞掉 ⇒ 只有当行内**所有** 6 位代码都是北交所、或行内有"北交所"字样时才算。
    不含 6 位代码的通用告警（如"拉取超时…返回部分结果"）**一律保留**。
    """
    if "北交所" in s:
        return True
    codes = re.findall(r"\b\d{6}\b", s)
    return bool(codes) and all(_BSE_CODE_RE.match(c) for c in codes)


def _pipeline_warnings(lines) -> list:
    """从**管线 stdout** 里挑出非已知常驻的 `[WARN]`/`[ERR]`/`[ERROR]` 行。

    为什么需要（2026-10-12 排查）：猎手管线遇到取数失败**不抛异常**，只 print 一行 WARN 然后
    "已跳过"继续 ⇒ 原有判据里没有一个看得见它。实测同日：北交所整批取数失败、S1 两只买单被拒，
    而三轮轮询全报「正常」。这里把**已经算出来的**退化管理信号接上，零额外 IO。
    """
    out = []
    for ln in lines or []:
        s = str(ln).strip()
        if not re.search(r"\[(?:WARN|ERR|ERROR)\]", s):
            continue
        if _is_known_dead_line(s) or _HANDLED_NOISE_RE.search(s):
            continue
        out.append(s)
    return out


def _log_warnings(lines) -> list:
    """从 **logging 捕获**里挑出非已知常驻的行。

    ⚠️ **不能复用 `_pipeline_warnings`**（本函数正是为这个坑单独存在）：logging 的
    `record.getMessage()` **不带 `[WARN]` 前缀**（前缀是 formatter 加的），
    所以"必须含 `[WARN]`"那条判据会把捕获到的日志行**全部滤掉**（实测踩到：`log_warns`
    有值、`pipeline_warns` 却是空的）。级别过滤在挂 handler 时就做完（`level=WARNING`），
    这里只需再做已知常驻豁免。
    """
    return [str(s).strip() for s in (lines or [])
            if not _is_known_dead_line(str(s)) and not _HANDLED_NOISE_RE.search(str(s))]


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
        # 同时收 **logging**（管线有一类失败只走 log.warning，redirect_stdout 看不见）
        _cap = _ThreadWarnCapture()
        _root = logging.getLogger()
        _root_old_level = _root.level
        _root.addHandler(_cap)
        try:
            # ⚠️ 必须把 root 级别放到 WARNING 以下，否则 `log.warning` 在 logger 级就被丢掉、
            #    handler 根本收不到（今日主日志里那两类北交所失败 0 条，正是这个后果）。
            if _root.level > logging.WARNING:
                _root.setLevel(logging.WARNING)
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
        finally:
            _root.removeHandler(_cap)
            _root.setLevel(_root_old_level)
        _all = buf.getvalue().strip().splitlines()
        tail = _all[-8:]
        res["stats"]["pipeline_tail"] = tail
        # ⚠️ `pipeline_tail` 只有最后 8 行，**早期 WARN 会被截掉** ⇒ 另外全量扫一遍告警行；
        #    再把 logging 那一路并进来（两条通道的判据不同，见 `_log_warnings`）。
        res["stats"]["pipeline_warns"] = (_pipeline_warnings(_all)
                                          + _log_warnings(_cap.lines))
        res["stats"]["log_warns"] = list(_cap.lines)
        res["stats"]["scored"] = len(scored or [])
        # 已处置噪声单列计数（不进异常列表）：安全闸丢掉几只未确认的当日快照根。
        # 留一行日志，数量异常放大时能从这里看出来。
        _und = _handled_noise_count(_all)
        res["stats"]["unconfirmed_dropped"] = _und
        if _und:
            log.info(f"猎手轮询：本轮丢弃 {_und} 只「未确认的当日快照根」"
                     f"（安全闸正常工作，不计入异常）")
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
    # 摘要素材（owner 2026-10-12：每轮要能收到一条精简猎手信息，不只是异常）
    try:
        import pandas as _pd
        st = res["stats"]
        st["sectors"] = int(len(summary))
        _lu = _pd.to_numeric(wl.get("涨停"), errors="coerce") if "涨停" in wl.columns else None
        st["limit_up"] = int(_lu.fillna(0).sum()) if _lu is not None else None
        # 排序用**涨停数**：本仓实测「涨停率」是唯一在两种成分口径、两个持有期都稳的板块强弱度量
        #（见 doc/research/2026-10-10_板块强弱度_成分股数量差异实验.md），比"平均分"更能代表板块强弱。
        _cols = [c for c in ("涨停数", "平均分") if c in summary.columns]
        _sm = summary.copy()
        for _c in _cols:
            _sm[_c] = _pd.to_numeric(_sm[_c], errors="coerce").fillna(0)
        _sm = _sm.sort_values(_cols, ascending=False) if _cols else _sm
        st["top3"] = [(str(r.get("板块")), int(r.get("涨停数") or 0), round(float(r.get("平均分") or 0), 1))
                      for _, r in _sm.head(3).iterrows()]
        _t3 = str(_sm.iloc[0].get("前三强") or "") if len(_sm) else ""
        st["top1_sector"] = st["top3"][0][0] if st["top3"] else None
        st["top1_names"] = re.findall(r"\d+\.([^(（]+)", _t3)[:3]
    except Exception as e:
        log.warning(f"猎手轮询：摘要素材提取失败（不影响异常判定）: {type(e).__name__}: {e}")
    if cov < _MIN_COVERAGE:
        _bad("low_coverage", f"行情覆盖率仅 {cov*100:.0f}%（阈值 {_MIN_COVERAGE*100:.0f}%）"
                             f"⇒ 行情源可能不可用")
    # 管线内部告警（取数部分失败/超时截断等）：管线**不抛异常**只 print，故必须从 stdout 捞。
    # 已知常驻噪声（北交所）已在 `_pipeline_warnings` 里剔除。
    _warns = res["stats"].get("pipeline_warns") or []
    if _warns:
        _bad("data_warn", f"管线内部告警 {len(_warns)} 条: " + " / ".join(_warns[:2]))
    # 盘中轮询时，数据日期必须是今天（历史日轮询不适用该判据）
    if date_str == datetime.now().strftime("%Y-%m-%d"):
        _ads = data.get("actual_data_dates") or set()
        if _ads and date_str not in {str(x)[:10] for x in _ads}:
            _bad("stale_date", f"实际数据日期 {sorted(map(str, _ads))[:3]} 不含今天 {date_str}")
    # 箱体突破提示（2026-10-29 owner）：「刚刚突破」的票跟着本轮卡片一起发（不额外发第二条，
    # 保持 owner 2026-10-12 定的「每轮恰好一条消息」）。探测失败只记日志，绝不影响异常判定。
    try:
        res["breakouts"] = find_box_breakouts(wl, date_str)
        if res["breakouts"]:
            log.info(f"猎手突破提示：本轮 {len(res['breakouts'])} 只 "
                     f"{[b['code'] for b in res['breakouts']]}")
    except Exception as e:
        res["breakouts"] = []
        log.warning(f"猎手轮询：箱体突破探测失败（不影响异常判定）: {type(e).__name__}: {e}")
    return res


# ============================================================================
# 箱体突破提示（2026-10-29 owner：「对于刚刚突破箱体的股票发送飞书进行提示」）
# ============================================================================
# 口径**完全对齐** GUI「突破箱体」那张卡（`t_gui._breakout_probe_one` 的 2026-10-25 收紧版）：
#   · 上沿取自 **as-of 上一交易日** 的箱体（`core.scanners._box_top_for`），
#     命中 `昨收 ≤ 上沿 < 现价`，且突破幅度 ∈ [0.3%, 3%]（3~5% 档 T+5 超额 −0.65%，
#     0.3~1% 档只有 −0.15% ⇒ 上限从 8% 收到 3%）；
#   · 当日涨幅 > 7% 直接剔除（那一档是最差一组，−0.58%）；
#   · **不做"放量确认"**（实测放量是反指，缩量档唯一不亏）⇒ 量比只展示。
# ⚠️ 这四个常量必须与 `t_gui.Api` 的 `_BK_*` 一致；`test_hunter_breakout.py` 会钉住。
BK_MIN_PCT = 0.3
BK_MAX_PCT = 3.0
BK_MAX_CHG = 0.07
BK_MIN_BARS = 30
_BK_MAX_PROBE = 1200          # 单轮探测上限（防御性：预筛异常时不至于把轮询拖到几分钟）
_BK_DEDUP_FP = os.path.join(_BASE, "t_io", "state", "hunter_breakout_alerts.json")


def _bk_dedup_load(date_str: str) -> set:
    try:
        with open(_BK_DEDUP_FP, encoding="utf-8") as f:
            d = json.load(f) or {}
        return set((d.get(date_str) or {}).get("codes") or [])
    except Exception:
        return set()


def _bk_dedup_save(date_str: str, codes) -> None:
    """每只票**一天只提示一次**（"刚刚突破"只在发生那一轮有意义）。"""
    try:
        d = {}
        try:
            with open(_BK_DEDUP_FP, encoding="utf-8") as f:
                d = json.load(f) or {}
        except Exception:
            d = {}
        d[date_str] = {"codes": sorted(set(list(_bk_dedup_load(date_str)) + list(codes))),
                       "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        for k in sorted(d)[:-_DEDUP_KEEP]:
            d.pop(k, None)
        os.makedirs(os.path.dirname(_BK_DEDUP_FP), exist_ok=True)
        tmp = _BK_DEDUP_FP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _BK_DEDUP_FP)
    except Exception as e:
        log.warning(f"猎手突破提示：去重文件写失败（不影响本轮推送）: {e}")


def find_box_breakouts(wl, date_str: str) -> list:
    """从猎手 watchlist（**含实时行情**）里找出"刚刚突破箱体上沿"的票 → [{code,name,...}]。

    **零网络**：历史只用本地日线缓存（`core.chart_cache.load_daily_display`），
    当日价用 watchlist 里的实时快照。全离线、失败静默（提示不该拖累轮询本体）。

    预筛：突破幅度 ≤ 当日涨幅（因为 `昨收 ≤ 上沿`）⇒ 当日涨幅 < 0.3% 或 > 7% 的票
    根本不可能命中，先剔掉能砍掉九成探测。
    """
    if wl is None or getattr(wl, "empty", True):
        return []
    try:
        import pandas as _pd
        from core.scanners import _box_top_for
        import core.chart_cache as _cc
    except Exception:
        return []
    today = str(date_str)[:10]
    out, probed, truncated = [], 0, False
    prev_hit = _bk_dedup_load(today)
    try:
        chg = _pd.to_numeric(wl.get("涨跌幅"), errors="coerce")
        px = _pd.to_numeric(wl.get("现价"), errors="coerce")
        pc = _pd.to_numeric(wl.get("昨收"), errors="coerce")
        m = (chg >= BK_MIN_PCT) & (chg <= BK_MAX_CHG * 100) & (px > 0) & (pc > 0)
        cand = wl[m]
    except Exception:
        return []
    for _, r in cand.iterrows():
        code = str(r.get("代码") or "").split("_")[0]
        if not code or code in prev_hit:
            continue
        if probed >= _BK_MAX_PROBE:
            truncated = True
            break
        probed += 1
        try:
            df = _cc.load_daily_display(code)
            if df is None or getattr(df, "empty", True):
                continue
            hist = df[df["date"].astype(str) < today]        # 只用**昨天及以前**的 bar 定箱体
            if len(hist) < BK_MIN_BARS:
                continue
            box_top = _box_top_for(hist, min_bars=BK_MIN_BARS)
            if not box_top:
                continue
            price = float(px.loc[r.name])
            prev_close = float(pc.loc[r.name])
            if not (prev_close <= box_top < price):
                continue
            pct = (price - box_top) / box_top * 100.0
            if not (BK_MIN_PCT <= pct <= BK_MAX_PCT):
                continue
            _chg = (price / prev_close - 1.0) * 100.0
            if _chg > BK_MAX_CHG * 100:
                continue
            vr = r.get("量比")
            out.append({"code": code, "name": str(r.get("名称") or code),
                        "price": round(price, 3), "prev_close": round(prev_close, 3),
                        "box_top": round(float(box_top), 3),
                        "pct": round(pct, 2), "chg": round(_chg, 2),
                        "vol_ratio": (round(float(vr), 2) if _pd.notna(vr) else None)})
        except Exception:
            continue
    # 漏斗打点（owner 2026-10-29 问「数量不全」）：预筛多少 / 实际探测多少 / 命中多少。
    # 下一轮觉得少，看这行就知道卡在哪一步：预筛小 = 当日涨幅不在 0.3~7%；
    # 探测小 = 候选都被去重跳过；命中 0 = 没有票满足 `昨收 ≤ 上沿 < 现价`。
    try:
        log.info(f"猎手突破提示：预筛 {len(cand)} 只 → 探测 {probed} 只 → 命中 {len(out)} 只"
                 f"（已去重跳过 {len(prev_hit)} 只）")
    except Exception:
        pass
    if truncated:
        log.warning(f"猎手突破提示：候选超过 {_BK_MAX_PROBE} 只，本轮只探测了前 {_BK_MAX_PROBE} 只")
    if out:
        out.sort(key=lambda x: -x["pct"])
        try:
            _bk_dedup_save(today, [x["code"] for x in out])
        except Exception:
            pass
    return out


def _breakout_lines(res: dict, limit: int = 0) -> list:
    """两类卡片共用的「刚突破箱体」清单（没有就是空列表 —— 不占位、不刷屏）。

    2026-10-29 owner：「突破箱体通知的数量不全 且显示的风格太抽」——
      · **默认全列**（`limit=0` 不截断）；只有异常卡这种"顺带提一句"的场景才传 limit。
      · 每行改成**分字段**、带序号：`现价 / 箱体上沿（突破幅度）/ 今日涨幅 / 量比`，
        不再把四个数字挤成一句「昨收6.57→6.9（上沿6.704 突破+2.9% 涨+5.0%）」。
      · 末尾那行原来写「…等 N 只」（看着像还有没列出来的，其实总数就 N），改成
        「…另有 K 只未列出」，只有真截断时才出现。
    """
    bk = res.get("breakouts") or []
    if not bk:
        return []
    lines = [f"📦 刚突破箱体 {len(bk)} 只（今日首次，按突破幅度排序）"]
    show = bk if not limit else bk[:limit]
    for i, b in enumerate(show, 1):
        bits = [f"现价 {b['price']}", f"箱体上沿 {b['box_top']}（突破 +{b['pct']:.1f}%）",
                f"今日 {b['chg']:+.1f}%"]
        if b.get("vol_ratio"):
            bits.append(f"量比 {b['vol_ratio']}")
        lines.append(f"{i}. {b['code']} {b['name']}　" + "　".join(bits))
    if limit and len(bk) > limit:
        lines.append(f"…另有 {len(bk) - limit} 只未列出")
    return lines


def _stats_line(st: dict) -> str:
    """两类卡片共用的那一行关键数字。"""
    bits = []
    if st.get("sectors"):
        bits.append(f"板块{st['sectors']}")
    if st.get("limit_up") is not None:
        bits.append(f"涨停{st['limit_up']}家")
    if st.get("scored") is not None:
        bits.append(f"打分{st['scored']}")
    if st.get("coverage") is not None:
        bits.append(f"覆盖{float(st['coverage']) * 100:.0f}%")
    return "· " + " ".join(bits) if bits else ""


def format_summary_card(res: dict, date_str: str, now: datetime = None) -> dict:
    """**每轮心跳**（owner 2026-10-12：除了异常，也要能持续收到猎手信息）。

    3~4 行、不发 Excel、不重算：板块数/涨停家数/覆盖率 + 均分前三 + 榜首三强。
    若本轮**仍有未恢复的异常**，标题换成 ⚠️ 并把异常一行置顶（心跳不会掩盖故障）。
    """
    now = now or datetime.now()
    anoms = res.get("anomalies") or []
    head = ("⚠️ " if anoms else "📊 ") + f"猎手轮询 · {now.strftime('%m-%d %H:%M')}"
    lines = [head]
    if anoms:
        a = anoms[0]
        msg = " ".join(str(a.get("msg") or "").split()).split("Traceback")[0].strip()[:60]
        more = f"（等 {len(anoms)} 项）" if len(anoms) > 1 else ""
        lines.append(f"{_KIND_SHORT.get(a['kind'], a['kind'])}：{msg}{more}")
    st = res.get("stats") or {}
    if st.get("top3"):
        lines.append("涨停前三：" + " / ".join(f"{n} {lu}" for n, lu, _ in st["top3"]))
    if st.get("top1_names"):
        lines.append(f"榜首三强（{st.get('top1_sector') or ''}）：{' '.join(st['top1_names'])}")
    lines.extend(_breakout_lines(res))
    sl = _stats_line(st)
    if sl:
        lines.append(sl)
    return {"msg_type": "text", "content": {"text": "\n".join(lines)}}


def push_summary(res: dict, date_str: str = None, now: datetime = None) -> bool:
    """发每轮心跳。**不去重**（就是要每轮都看到）；推送契约与异常卡相同。"""
    date_str = date_str or (res.get("stats") or {}).get("date") \
        or datetime.now().strftime("%Y-%m-%d")
    card = format_summary_card(res, date_str, now)
    try:
        from config import send_feishu_payload
        ok = send_feishu_payload(
            card, success_log=f"猎手轮询心跳: {date_str}",
            error_prefix="猎手轮询心跳推送")
    except Exception as e:
        log.warning(f"⚠️ 猎手轮询心跳推送抛异常: {type(e).__name__}: {str(e)[:180]}")
        return False
    if not ok:
        log.warning("⚠️ 猎手轮询心跳推送返回失败")
    return bool(ok)


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
    sl = _stats_line(st)
    if sl:
        lines.append(sl)
    lines.extend(_breakout_lines(res, limit=3))     # 异常卡也带一行"刚突破"，仍是**一条**消息
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
    """轮询 + 推送一步到位（供 main.py 调度线程调用）。**不抛**。

    **每轮恰好一条消息**（owner 2026-10-12 定）：
      · 本轮出现**新异常** ⇒ 发异常卡（精简、可执行）；
      · 其余（一切正常 / 异常持续中）⇒ 发**心跳摘要卡**（持续异常会在心跳里标 ⚠️ 置顶）。
    """
    res = poll_once(date_str)
    sent = False
    if not res["ok"]:
        log.warning(f"⚠️ 选股猎手轮询发现 {len(res['anomalies'])} 项异常: "
                    f"{[a['kind'] for a in res['anomalies']]}")
        # 飞书卡片只发精简版 ⇒ **完整细节必须落日志**（含 traceback），别让信息凭空消失
        for a in res["anomalies"]:
            log.warning(f"   · [{a['kind']}] {a['msg']}")
        try:
            sent = push_anomalies(res, date_str)      # 新异常才发（去重语义不变）
        except Exception as e:
            log.error(f"❌ 猎手轮询告警推送链路异常: {type(e).__name__}: {e}")
    if not sent:
        try:
            sent = push_summary(res, date_str)        # 心跳：每轮都发
        except Exception as e:
            log.error(f"❌ 猎手轮询心跳推送链路异常: {type(e).__name__}: {e}")
    res["pushed"] = bool(sent)
    return res
