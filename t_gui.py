# -*- coding: utf-8 -*-
"""
t_gui.py — 做T实盘·盘后复盘决策看板（pywebview 桌面壳）
用法: python t_gui.py

数据只读，不改动任何现有系统行为。前端为 web/ 目录下的纯 HTML/CSS/JS，
通过 pywebview js_api 调用本文件的 Api 方法获取聚合后的当日决策数据。

数据源（全部由现有系统落盘）:
  t_io/validation/daily_review/daily_review_{date}.json   日复盘主聚合
  t_io/validation/daily_review/kpi_{date}.json            K1-K5 KPI 独立文件
  t_io/validation/daily_review/stage_board.json           阶段看板
  t_io/traces/position_builder_{date}.jsonl               建仓扫描逐行日志
  doc/每日复盘/{date}_复盘.md                              复盘报告 markdown
  holdings.json / t_io/state/holdings_daily_{date}.json     持仓（当前 + GUI 日快照；旧 holdings_{date}.json 已于 2026-08-30 清理）
"""
import json
import math
import os
import re
import sys
import threading
import time as _time_mod
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

# Windows 终端 UTF-8 修复（避免 GBK 乱码）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent  # 自解析：生产机 E:\06_T 与本机仓库位置均正确（与 position_builder/config 一致）
OUT = BASE / "t_io" / "validation" / "daily_review"
TRACES = BASE / "t_io" / "traces"
STATE_DIR = BASE / "t_io" / "state"
# 2026-10-04 双文件拆分：手动盘页读 holdings_manual.json；自动盘页经 holdings_repo 读自动侧。
HOLDINGS_MANUAL = STATE_DIR / "holdings_manual.json"
IDX_REGIME = BASE / "t_io" / "index_regime"
LOGS_DIR = BASE / "t_io" / "logs"
INTRADAY_STATE = BASE / "t_io" / "intraday_state.json"
PORTFOLIO = STATE_DIR / "accounts_config.json"  # 2026-08-30 合并：账户配置唯一源头（原 portfolio_config.json 已并入）
PORTFOLIO_LEGACY = STATE_DIR / "portfolio_config.json"  # 旧部署回退（.gszq 等）
BRIDGE_DIR = BASE / "t_io" / "bridge"  # P4-2/3: 自动盘事件总线（heartbeat.json + events_*.jsonl + KILL_SWITCH）
TRADE_HISTORY_LEDGER = STATE_DIR / "trade_history_ledger.json"  # 券商交割单解析后的逐股盈亏台账（2026-10-10 owner 需求3）

# 内置名称映射（数据缺失 code 时兜底；可由 holdings/sig_stat/trace 补充）
NAMES = {
    "000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
    "600481": "双良节能", "603667": "五洲新春", "002639": "雪人集团",
    "300153": "科泰电源", "300364": "中文在线",
}

# 名称**兜底层**（2026-10-13）：韭研 watchlist 覆盖 **5382 个代码**，含持仓/auto 池之外的票。
# 为什么需要：逐笔流水的 name 是"读取端反查补"的（引擎 write_fill 不带股票名），
# 而原来源只有 `NAMES` + `holdings_auto.json` 两处 ⇒ 两处都没有的代码（实测 auto 流水里的
# **605198 安德利 / 688003 天准科技**）只能渲染成"605198 605198"，看起来像坏了。
# ⚠️ 该文件 **2MB**，必须按 mtime 缓存 —— 否则每轮 `_compute_auto_pnl` 都要解析一次。
_JY_NAMES_CACHE = {"mtime": None, "map": {}}


# 不是题材、只作标记的「板块」名（不进星空图/站上均线占比）
NON_BOARD_LABELS = {"北交所", "央企"}


def _jiuyan_board() -> dict:
    """韭研 watchlist → {6位码: 主板块}。**与 `oversold_gap_confirm.prescreen.sym2board_map()`
    同口径**（取第 1 个有值的 `jiuyan_category{1..9}`，再过 MERGES；全空则按 `jiuyan_category`
    的 `|` 拆分取首段）。

    ⚠️ 这是**刻意的第二份实现**，不是疏漏：t_gui 不该 import 策略包（`execution/auto/` 是
    实盘宿主目录、且策略包只在一个部署里存在）。两份口径由
    `tests/phase3/test_market_beacon.py::TestBoardMapSingleSource` 逐码比对守卫 ——
    **改任一侧都要同步另一侧**，否则测试红。这是 2026-10-13 背离那次的教训（副本漂移无人知）。
    """
    fp = BASE / "stock_hunter" / "watchlist_jiuyan.json"
    try:
        if _JY_BOARD_CACHE["mtime"] == fp.stat().st_mtime:
            return _JY_BOARD_CACHE["map"]
    except OSError:
        return _JY_BOARD_CACHE["map"]
    merges = {"创新药": "医药", "中药": "医药"}
    out = {}
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        for code, info in d.items():
            if not isinstance(info, dict) or not str(code).isdigit():
                continue
            cats = []
            for i in range(1, 10):
                c = str(info.get(f"jiuyan_category{i}", "") or "").strip()
                if c:
                    cats.append(merges.get(c, c))
            if not cats:
                c0 = str(info.get("jiuyan_category", "") or "")
                cats = [merges.get(x.strip(), x.strip()) for x in c0.split("|") if x.strip()]
            # 2026-10-25：「北交所 / 央企」是**机构/市场标签**，不是题材板块 ⇒ 不参与板块统计
            cats = [c for c in cats if c not in NON_BOARD_LABELS]
            out[str(code)] = cats[0] if cats else ""
        _JY_BOARD_CACHE.update({"mtime": fp.stat().st_mtime, "map": out})
    except Exception:
        pass
    return _JY_BOARD_CACHE["map"]


_JY_BOARD_CACHE = {"mtime": None, "map": {}}


def _jiuyan_names() -> dict:
    """韭研 watchlist → {6位码: 名称}。首次/文件变更时解析一次，之后走缓存；失败返回上次结果。"""
    fp = BASE / "stock_hunter" / "watchlist_jiuyan.json"
    try:
        _mt = fp.stat().st_mtime
    except OSError:
        return _JY_NAMES_CACHE["map"]
    if _JY_NAMES_CACHE["mtime"] == _mt:
        return _JY_NAMES_CACHE["map"]
    try:
        _d = json.loads(fp.read_text(encoding="utf-8"))
        _m = {str(k).split("_")[0]: v["name"]
              for k, v in _d.items() if isinstance(v, dict) and v.get("name")}
        _JY_NAMES_CACHE.update({"mtime": _mt, "map": _m})
    except Exception:
        pass
    return _JY_NAMES_CACHE["map"]


# ── K线弹窗「买卖点标记」数据源（2026-10-21 owner）────────────────────────────
# 券商成交台账 → 某 code 的成交行 [{ts, side, qty, price, account}]。
# ⚠️ **直接读文件**：`load_trade_history()` 下发前端前会把 `trades` 字段 pop 掉，
#    K线路径要的恰恰是这批原始成交行。文件 <1MB（2MB 内），按 mtime 缓存，
#    与上面 `_JY_NAMES_CACHE` 同款模式，热路径不重复解析。
_TRADE_LEDGER_CACHE = {"mtime": None, "map": {}}


def _chart_trades(code):
    """台账 → 该 code 的成交行（时间升序）：[{ts, side:"B"/"S", qty, price, account}]。

    `ts` 有成交时间时为 "YYYY-MM-DD HH:MM:SS"；老行 time 为空 ⇒ ts 只带日期
    （分钟图无法落桶、前端跳过；日线仍按日期落点）。
    文件不存在/解析失败 ⇒ 返回上次缓存（或空表），绝不抛异常卡住图表链路。
    """
    _c = str(code).split("_")[0]
    try:
        _mt = TRADE_HISTORY_LEDGER.stat().st_mtime
    except OSError:
        return _TRADE_LEDGER_CACHE["map"].get(_c, [])
    if _TRADE_LEDGER_CACHE["mtime"] != _mt:
        _m = {}
        try:
            _d = json.loads(TRADE_HISTORY_LEDGER.read_text(encoding="utf-8"))
            for _t in (_d.get("trades") or []):
                if not isinstance(_t, dict) or _t.get("non_trade"):
                    continue
                _side = {"买入": "B", "卖出": "S"}.get(str(_t.get("op") or ""))
                if not _side:
                    continue
                _code = str(_t.get("code") or "").split("_")[0]
                _date = str(_t.get("date") or "")
                if not _code or not _date:
                    continue
                # `_clean` 同款防护：qty/price 出现 None/NaN/inf 时给 None，不污染 JSON 桥
                _qty = _t.get("qty")
                _px = _t.get("price")
                if isinstance(_qty, float) and (math.isnan(_qty) or math.isinf(_qty)):
                    _qty = None
                if isinstance(_px, float) and (math.isnan(_px) or math.isinf(_px)):
                    _px = None
                _tm = str(_t.get("time") or "").strip()
                _m.setdefault(_code, []).append({
                    "ts": f"{_date} {_tm}" if _tm else _date,
                    "side": _side, "qty": _qty, "price": _px,
                    "account": str(_t.get("account") or "")})
            for _rows in _m.values():
                _rows.sort(key=lambda r: r["ts"])
            _TRADE_LEDGER_CACHE.update({"mtime": _mt, "map": _m})
        except Exception:
            pass
    return _TRADE_LEDGER_CACHE["map"].get(_c, [])


_JY_ROLE_CACHE = {"mtime": None, "map": {}}


def _jiuyan_roles() -> dict:
    """韭研 watchlist → {6位码: {"role": 龙头/中军, "reason": 定性理由}}。

    2026-10-20 机器人/军工板块整治起写入（`stock_hunter/tag_robot_military_fix_20261020.py`）：
    研究核实的**真龙头/中军**静态标签，与 `_tier_sector_stocks` 的当日动态分层**互补**——
    梯队列静态优先（有静态用静态、跳过算法），个股资料面板同步下发。
    与 `_jiuyan_names` 同构：按 mtime 缓存，失败返回上次结果。
    """
    fp = BASE / "stock_hunter" / "watchlist_jiuyan.json"
    try:
        _mt = fp.stat().st_mtime
    except OSError:
        return _JY_ROLE_CACHE["map"]
    if _JY_ROLE_CACHE["mtime"] == _mt:
        return _JY_ROLE_CACHE["map"]
    out = {}
    try:
        _d = json.loads(fp.read_text(encoding="utf-8"))
        for code, info in _d.items():
            if not isinstance(info, dict):
                continue
            r = str(info.get("jiuyan_role") or "").strip()
            if r in ("龙头", "中军"):
                out[str(code).split("_")[0]] = {
                    "role": r,
                    "reason": str(info.get("jiuyan_role_reason") or "").strip(),
                }
        _JY_ROLE_CACHE.update({"mtime": _mt, "map": out})
    except Exception:
        pass
    return _JY_ROLE_CACHE["map"]


# ── 每日选股记录（OSC 超跌跳空确认）读取/还原（2026-10-17）────────────────────
# 数据优先吃新落盘（decision_/prescreen_），历史日从 fills + 策略日志还原。
OSC_BOOK = STATE_DIR / "weak_strong_book"
OSC_LOG_DIR = BASE / "execution" / "auto" / "logs"
_OSC_BT_CACHE = {"key": None, "map": {}}     # auto_backtrace 解析缓存（~28MB，勿每次读）

_OSC_RE_PREP = re.compile(r"^\[OSC\] 盘前超跌候选 (\d+) 只", re.M)
_OSC_RE_PREP_START = re.compile(r"盘前预筛开始 ──（前(\d+)热门板块 ∩ (全市场|猎手池\(已回退\))")
_OSC_RE_BARS = re.compile(r"\[OSC\] 10:00 取数: 拿到 (\d+)/(\d+) 只的 10:00 棒")
_OSC_RE_FUNNEL = re.compile(
    r"\[OSC\] 10:00 判定漏斗: 池 (\d+) \| 超跌\(昨收<MA20\) (\d+) \| "
    r"①全程不破昨收 (\d+) \| ②站上昨收 (\d+) \| ③站上VWAP (\d+) \| 三全过 (\d+)")
_OSC_RE_REASONS = re.compile(r"\[OSC\] 10:00 首个未过关: (.+)")
_OSC_RE_NEAR_HDR = re.compile(r"最接近\(gap 降序 (\d+)/(\d+) 只\)")
_OSC_RE_NEAR_ROW = re.compile(
    r"^\s{4,}(\d{6})\s+(\S+)\s+(\S+)\s+gap\s*([+-][\d.]+)%\s+差:(\S+)", re.M)
_OSC_RE_PICKS = re.compile(r"\[OSC\] 10:00 选中\(gap 降序\): (.+)")
_OSC_RE_PICK_ITEM = re.compile(r"(\d{6}) gap([+-][\d.]+)%")


def _osc_parse_log(date):
    """解析 `execution/auto/logs/strategy_YYYYMMDD.log` 的 [OSC] 段（只读、纯文本）。

    ⚠️ 漏斗/首个未过关/最接近 三段是 **2026-10-15 才上线的日志** ⇒ 更早的日期解析不到，
    调用方须把缺失项如实置 None（**不要填 0**，那会把"没记录"误读成"没有"）。
    ⚠️ 同日可能有多段（进程重启）⇒ 取**最后一段**。
    """
    fp = OSC_LOG_DIR / ("strategy_%s.log" % str(date).replace("-", ""))
    out = {"premarket_n": None, "universe": None, "board_top_n": None,
           "bars_n": None, "bars_want": None, "funnel": None, "reason_counts": None,
           "near": None, "picks": None, "runs": 0}
    try:
        txt = fp.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return out
    m = _OSC_RE_PREP.findall(txt)
    if m:
        out["premarket_n"] = int(m[-1])          # 多段 ⇒ 最后一次
        out["runs"] = len(m)
    ms = _OSC_RE_PREP_START.findall(txt)
    if ms:
        out["board_top_n"] = int(ms[-1][0])
        out["universe"] = "full_market" if ms[-1][1] == "全市场" else "hunter_pool"
    mb = _OSC_RE_BARS.findall(txt)
    if mb:
        out["bars_n"], out["bars_want"] = int(mb[-1][0]), int(mb[-1][1])
    mf = _OSC_RE_FUNNEL.findall(txt)
    if mf:
        p, w, g1, g2, g3, a = [int(x) for x in mf[-1]]
        out["funnel"] = {"pool_n": p, "weak": w, "g1": g1, "g2": g2, "g3": g3, "all": a}
    mr = _OSC_RE_REASONS.findall(txt)
    if mr:
        rc = {}
        for part in mr[-1].split("·"):
            mm = re.match(r"\s*(\S+)\s+(\d+)\s*$", part)
            if mm:
                rc[mm.group(1)] = int(mm.group(2))
        out["reason_counts"] = rc
    if _OSC_RE_NEAR_HDR.search(txt):
        out["near"] = [{"code": c, "name": n, "board": b,
                        "gap": float(g) / 100.0, "reason_cn": why}
                       for c, n, b, g, why in _OSC_RE_NEAR_ROW.findall(txt)]
    mp = _OSC_RE_PICKS.findall(txt)
    if mp:
        out["picks"] = [{"code": c, "gap": float(g) / 100.0}
                        for c, g in _OSC_RE_PICK_ITEM.findall(mp[-1])]
    return out


def _osc_backtrace_candidates(date):
    """**最后手段**：从审计镜像取该日盘前候选数。

    ⚠️ 只读 `t_io/logs/auto_backtrace.jsonl`（镜像）；`execution/auto/gmcache/backtrace.jsonl`
    含**离线测试伪造事件**（600001/600002/600003 那批），绝不能读。
    ⚠️ 该文件 ~28MB / 8 万行 ⇒ 按 (mtime,size) 缓存解析结果，且只在日志解析不到时才走到这里。
    """
    fp = LOGS_DIR / "auto_backtrace.jsonl"
    try:
        st = fp.stat()
    except OSError:
        return None
    key = (st.st_mtime, st.st_size)
    if _OSC_BT_CACHE.get("key") != key:
        idx = {}
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "osc_prepare" not in line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("event") != "osc_prepare":
                        continue
                    d = str(o.get("date") or "")
                    rid = str(o.get("_run_id") or "")
                    # 回放/测试污染：_run_id 的日期前缀与事件日期对不上 ⇒ 丢
                    if rid and rid[:8] != d.replace("-", ""):
                        continue
                    idx[d] = int(o.get("candidates") or 0)
        except Exception:
            idx = {}
        _OSC_BT_CACHE.update({"key": key, "map": idx})
    return _OSC_BT_CACHE.get("map", {}).get(str(date))


# W33 A1: 双通道 8 键标签（与 position_builder.CHANNEL_COND_KEYS 同序）
# 方案A (2026-08-15): 建仓条件=时机门控，与 position_builder.COND_LABELS 一致
# 条件详情卡「按当前规则实时重算」的进程内缓存（2026-10-15）：{(code,date): scan_stock 结果}。
# ⚠️ 进程级 ⇒ **规则改动本来就要求重启 GUI**，重启即自动失效，故不需要再挂版本号。
_COND_FRESH_CACHE = {}

COND_LABELS = {
    "t_regime": "市场有方向",
    "t_trend": "多头结构",
    "t_drawdown": "回撤到位",
    # 2026-10-15：GO 与得分口径一致新增的两条**硬约束**（timing_decision 的
    # go_require_above_ma20 / go_require_above_ma5）——不加进来，面板/条件卡就少显示两条，
    # 用户看到 NO-GO 却找不到原因。与 position_builder 写入侧、condition_detail 渲染侧三处同步。
    "t_ma20": "站上MA20(硬约束)",
    "t_ma5": "站上MA5(硬约束)",
    "t_golden": "MACD金叉(加分)",
}


# 黄金分割（斐波那契回撤/扩展）参数 —— 按周期分档，越长的周期要求越大的摆动幅度
# 回看根数（决定锚点搜索范围）。2026-10-04: daily 250→120，与前端默认视窗根数对齐——
# 原来 250 会让前端为「看得见锚点」把视窗放宽，一屏塞进 250+ 根K线，蜡烛细到看不清。
# 代价：更早的大级别摆动不再被选为锚点（用户 2026-10-04 确认接受）。
_FIB_LOOKBACK = {"daily": 120, "weekly": 120, "monthly": 60,
                 "min5": 160, "min10": 160, "min30": 160, "min60": 160}  # 分钟档与前端默认视窗根数对齐
_FIB_FRACTAL_N = {"daily": 3, "weekly": 2, "monthly": 1,        # 分形确认根数（越长周期 bar 越少）
                  "min5": 2, "min10": 2, "min30": 2, "min60": 2}
_FIB_MIN_AMP = {"daily": 5.0, "weekly": 8.0, "monthly": 12.0,   # 摆动最小幅度 %（低于此视为噪声）
                "min5": 3.0, "min10": 3.0, "min30": 3.0, "min60": 3.0}  # 分钟档为初始值，未标定，待图上肉眼校准
_FIB_RETRACE = [0.236, 0.382, 0.5, 0.618, 0.786]                # 回撤位（0.618 即黄金比例）
_FIB_EXTENSION = [1.272, 1.618]                                 # 扩展位（突破后目标）

HUNTER_DIR = BASE / "stock_hunter"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))  # stock_hunter 模块在 stock_hunter/ 下导入
if str(HUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(HUNTER_DIR))

# 2026-08-15: 选股猎手后台运行状态（进度条轮询用）。进度细节来自 market_data.MARKET_PROGRESS。
import threading as _th
HUNTER_RUN_STATE = {"date": None, "running": False, "result": None}
# 2026-10-10: 原「开盘后每小时自动跑」定时器已删除（owner 需求6，扫描一律按钮触发）。
_CHART_PREFETCH_STATE = {"started": False}  # 盘后 K线预下载调度（2026-10-04）
# 主线程(js_api)心跳：高频轮询方法每次更新；看门狗发现停跳 ⇒ 判界面卡死并落线程栈（2026-10-08）
_GUI_HB = {"ts": 0.0}
# 建仓推送的「当日去重」状态：自动运行只在候选集变化时推，避免一天重复刷屏
_HUNTER_BUILD_PUSHED_FP = STATE_DIR / "hunter_build_pushed.json"
ROTATION_RUN_STATE = {"running": False, "error": None}
# 2026-08-22: 板块轮动结果缓存（内存+磁盘）——build_rotation_model 约 15s，重复点击/切 view 秒回
_ROTATION_CACHE_DIR = BASE / "t_io" / "cache" / "sector_rotation"

# 2026-10-11: 日线缓存目录的**本地常量**。此前若干处为了拿这个路径去
# `from core.position_builder import _DAILY_CACHE_DIR`，而那会连锁导入
# core.market_data → facade → **pandas**（实测 ~2.3s）。它砸在 GUI 冷启动首帧上：
# `_compute_auto_pnl` 只用它拼 `{code}.json` 路径，却把整条 pandas 链拖进首帧的 import 锁。
# 值必须与 core/position_builder.py::_DAILY_CACHE_DIR 一致（同为 BASE/t_io/cache/daily_kline）。
_DAILY_KLINE_DIR = BASE / "t_io" / "cache" / "daily_kline"
# ---- 年限宽度：每日站上 365 日均线的股票占比（2026-10-18）----
# 数据源 = 全部个股日线缓存（t_io/cache/daily_kline/*.json）。全量解析约 5500 个文件 ≈10s（本机
# 单线程实测），故：**后台线程算 + 内存/磁盘 SWR**，前端未就绪时短轮询。磁盘缓存按"数据日"复用，
# 一天只算一次（盘中猎手回写当日 bar 后由 stale 标记触发重算）。
_MA365_CACHE_FP = BASE / "t_io" / "cache" / "ma365_breadth.json"
# 2026-10-18 owner：「给出 2024-09-01 至今的年限宽度」——本地日线缓存只有 800 根（起点≈2023-06），
# 2024-09 的 MA365 回看窗口不够 ⇒ 由 scripts/experiment/ma365_breadth_hist_20240901.py
# 用 GM 批量日线（900 根）一次性补算，落这个文件；面板"自2024-09-01"档直接读它。
_MA365_HIST_FP = BASE / "t_io" / "cache" / "ma365_breadth_20240901.json"
_MA365_STATE: dict = {"running": False, "done": 0, "total": 0, "started": 0.0}
_MA365_LOCK = threading.Lock()
_MA365_MEM: dict = {"ts": 0.0, "val": None}
_MA365_MEM_TTL = 300.0
_MA365_MAX_DAYS = 250          # 一次最多算 250 个交易日（前端按需切片）
# 2026-10-20 owner：「年限宽度下方加上 站上20线公司占比，周期和年限宽度保持一致」。
# MA20 宽度与 MA365 **同一次解析顺手算**（滑动和 O(1)/根，几乎不增加成本），但要覆盖
# 「自2024-09-01」那一档（≈510 个交易日）⇒ 单独用 600 天的窗口。MA20 只要 20 根回看，
# 本地 800 根日线缓存足够，**不需要**像 MA365 那样用 GM 补算历史。
_MA20_MAX_DAYS = 600
_MA365_WORKERS = 1             # JSON 解析持 GIL：实测 **1 线程 9.2s 反而快于 2 线程 14s**
                               #（两线程互抢 GIL + 与 GUI 争），且更不容易造成界面卡顿
# 建仓符合度（GO/时机列）的进程内结果缓存（2026-10-17 提速）。
# 键 = (6位码, 日期, regime, 文件 mtime_ns)：**任何一项变就重算** ⇒ 猎手回写当日 bar 后
# mtime 变 ⇒ 自动失效，不会拿陈旧序列算（那正是「误报破5日线」的老坑）。
# ⚠️ 曾试过把日线截到尾 120 根再算，实测只快 1.17x（真正的成本是整份 json.loads），
#    却会让 Wilder RSI/EMA 的预热种子产生微小漂移 ⇒ **不做截尾**，保持逐位同结果。
_HUNTER_CONF_CACHE = {}
# 「排名持续提升」的判定阈值（load_sector_constellation）：至少过半天数名次变好 + 净提升 ≥5 名。
_SUSTAIN_UPS_MIN = 5
_SUSTAIN_NET_MIN = 5
_ROTATION_CACHE_MEM = {}
# 2026-08-23: 每日大盘复盘（LLM）后台线程状态
_REVIEW_RUN_STATE = {"running": False, "error": None}
# 2026-10-10: 自动盘盈亏账单缓存（load_auto_pnl；纯磁盘读，30s TTL 足够）
_AUTO_PNL_CACHE = {"ts": 0.0, "date": None, "data": None}
# 2026-10-20：自动盘盈亏/成本是**纯磁盘全量重算**（实测 3~5s），30s TTL 会在盘中
# 每半分钟把 UI 线程压住一次（K 线弹窗首帧正好撞上就明显变慢）。fill 是事件驱动的，
# 面板摘要 2 分钟刷新足够 ⇒ TTL 提到 120s，重算频率降 4 倍。
_AUTO_PNL_TTL = 120.0
# 2026-10-10: 自动盘持仓成本派生缓存（load_auto_status 10s 轮询，避免每轮重读 events_*.jsonl）
_AUTO_COST_CACHE = {"ts": 0.0, "data": None}
_AUTO_COST_TTL = 120.0     # 2026-10-20：同 _AUTO_PNL_TTL（全量 fill 重算 3~5s）


def _read_auto_fills():
    """按时间序读出 t_io/bridge/events_*.jsonl 里全部 fill 事件。

    GM 引擎的 write_fill 不带股票名/净发生金额/成交前持仓，但 code/side/qty/price/fee/pos_after
    是齐的；这里只做「跨日排序」这一件事，语义补齐交给各调用方。
    """
    fills = []
    try:
        for fp in sorted(BRIDGE_DIR.glob("events_*.jsonl")):
            try:
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or '"fill"' not in line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        if e.get("event") == "fill":
                            fills.append(e)
            except Exception:
                continue
    except Exception:
        pass
    fills.sort(key=lambda e: (str(e.get("time") or ""), float(e.get("_ts") or 0)))
    return fills


def _daily_last_close(code):
    """日线缓存（t_io/cache/daily_kline）里的最新收盘价；零网络、无缓存 → 0.0。"""
    _DAILY_CACHE_DIR = _DAILY_KLINE_DIR   # 本地常量（2026-10-11）：原 import core.position_builder 会连带 pandas
    try:
        rows = json.loads(
            (_DAILY_CACHE_DIR / f"{code}.json").read_text(encoding="utf-8")).get("rows") or []
        if rows:
            return float(rows[-1].get("close") or 0)
    except Exception:
        pass
    return 0.0


def _auto_fill_costs():
    """从 fill 流复原各票**当前持仓的加权平均成本**（不含手续费，同券商「持仓均价」口径）。

    返回 `{code: {"qty": n, "cost": avg}}`（只含派生持仓 > 0 的票）。

    ⚠️ 为什么不能直接用 heartbeat 的 `cost`：引擎在**加仓时不重新加权**。实测 600584
    2026-10-09 加买 800@61.497 后 heartbeat 仍写 64.35（加仓**前**的均价），而券商终端
    的持仓均价是 (400×64.35 + 800×61.497)/1200 = 62.448 —— 直接采信 heartbeat 会让
    浮盈差 32 元。费用**不计入**成本才能与券商逐只对齐（含费会再偏 0.01~0.03/股）。

    但 heartbeat 的**数量**才是准的（fill 流会漏掉仿真终端手工单/orphan_fill），
    所以数量一律用 heartbeat，只有「派生数量 == heartbeat 数量」时才采信派生成本。
    """
    import time as _t
    c = _AUTO_COST_CACHE
    if c["data"] is not None and (_t.time() - c["ts"] < _AUTO_COST_TTL):
        return c["data"]
    run = {}
    for e in _read_auto_fills():
        code = str(e.get("code") or "").split("_")[0]
        if not code:
            continue
        try:
            qty = int(e.get("qty") or 0)
            price = float(e.get("price") or 0)
        except Exception:
            continue
        st = run.setdefault(code, [0, 0.0])
        if str(e.get("side") or "").upper() == "BUY":
            st[0] += qty
            st[1] += price * qty                      # 成交金额，刻意不含手续费
        else:
            avg = (st[1] / st[0]) if st[0] else 0.0
            matched = min(qty, st[0])
            st[0] = max(0, st[0] - qty)
            st[1] = max(0.0, st[1] - matched * avg)
            if st[0] == 0:
                st[1] = 0.0
    out = {k: {"qty": v[0], "cost": round(v[1] / v[0], 6)}
           for k, v in run.items() if v[0] > 0}
    c.update({"ts": _t.time(), "data": out})
    return out



def _jiuyan_concepts(info):
    """合并股票记录的所有韭研概念（编号字段 jiuyan_concept1..9 + 旧普通字段）。
    返回用 | 连接的去重字符串；无概念返回空串。
    2026-10-19：概念值允许 list/tuple 多标签（医药板块整合起）——逐元素展开，
    否则 str(list) 会把 Python repr（"['临床CRO', …]"）漏进所有下游显示。"""
    if not isinstance(info, dict):
        return ""
    parts = []

    def _collect(v):
        if isinstance(v, (list, tuple)):
            for x in v:
                if x and str(x).strip():
                    parts.append(str(x).strip())
        elif v and str(v).strip():
            parts.append(str(v).strip())

    for i in range(1, 10):
        _collect(info.get(f"jiuyan_concept{i}"))
    if not parts:
        _collect(info.get("jiuyan_concept"))
    seen = set()
    out = []
    for p in parts:
        for c in p.split("|"):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return "|".join(out)


def _jiuyan_concept_descs(info, concepts=None):
    """该股韭研概念的注释映射 {概念: 解释}（watchlist 的 `jiuyan_concept_desc` 字段，
    2026-10-19 医药板块整合起写入）。concepts 给定时只保留其中出现的键；无注释返回 {}。"""
    if not isinstance(info, dict):
        return {}
    d = info.get("jiuyan_concept_desc")
    if not isinstance(d, dict) or not d:
        return {}
    if concepts is None:
        return {str(k): str(v) for k, v in d.items() if k and v}
    keep = set(concepts)
    return {str(k): str(v) for k, v in d.items() if k in keep and v}


def _jiuyan_all_concept_descs():
    """全表合并的韭研概念注释 {概念: 解释}（前端分组头/概念列 tooltip 用）。
    同 key 先见先留；读失败返回 {}（非致命）。"""
    out = {}
    try:
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        for info in (jy.values() if isinstance(jy, dict) else []):
            if not isinstance(info, dict):
                continue
            d = info.get("jiuyan_concept_desc")
            if not isinstance(d, dict):
                continue
            for k, v in d.items():
                if k and v and k not in out:
                    out[str(k)] = str(v)
    except Exception:
        pass
    return out


def _jiuyan_concept_reasons(info, concepts=None):
    """该股「为什么算这个概念」{概念: 理由}（watchlist 的 `jiuyan_concept_reason`，
    2026-10-24 数据要素板块纯正性排查起写入）。concepts 给定时只保留其中出现的键。"""
    if not isinstance(info, dict):
        return {}
    d = info.get("jiuyan_concept_reason")
    if not isinstance(d, dict) or not d:
        return {}
    if concepts is None:
        return {str(k): str(v) for k, v in d.items() if k and v}
    keep = set(concepts)
    return {str(k): str(v) for k, v in d.items() if k in keep and v}


_PROFILE_CACHE = {"mtime": None, "data": {}}


def _company_profiles():
    """`t_io/cache/company_profile.json`（东财 F10 一句话业务）按 mtime 缓存。
    只用于「个股为什么在这个板块」的兜底理由——brief_title 一句话，比 watchlist 里
    「主营XX制造业」有用得多。"""
    fp = BASE / "t_io" / "cache" / "company_profile.json"
    try:
        mt = fp.stat().st_mtime
    except OSError:
        return {}
    if _PROFILE_CACHE["mtime"] == mt and _PROFILE_CACHE["data"]:
        return _PROFILE_CACHE["data"]
    d = _load_json(fp, {})
    if not isinstance(d, dict):
        d = {}
    _PROFILE_CACHE.update({"mtime": mt, "data": d})
    return d


def _jiuyan_tag_verdicts():
    """{代码: [判词, 理由]} —— 逐股「标签与主营的相关度」，猎手个股行名字后面直接显示小标。

    来源：watchlist 的 `jiuyan_tag_verdict`（核心/相关/边缘/错标，2026-10-24 起逐批人工判）
    + `jiuyan_concept_reason` 里该股第一条理由（做 tooltip）。没判过的票不下发。
    """
    out = {}
    try:
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        for code, info in (jy.items() if isinstance(jy, dict) else []):
            if not isinstance(info, dict):
                continue
            v = str(info.get("jiuyan_tag_verdict") or "").strip()
            if not v:
                # 没人工判过的：给「未核」（业务文本能对上该概念，仅是自动比对的参考）
                if str(info.get("jiuyan_tag_evidence") or "") == "text":
                    out[str(code)] = ["未核", "还没逐只核过（业务文本能对上该概念，仅供参考）"]
                continue
            reason = ""
            d = info.get("jiuyan_concept_reason")
            if isinstance(d, dict):
                for k in _jiuyan_concepts(info).split("|"):
                    reason = str(d.get(k) or "").strip()
                    if reason:
                        break
            out[str(code)] = [v, reason]
    except Exception:
        pass
    return out


def _jiuyan_all_concept_reasons():
    """全表合并的 {概念: [[名称, 理由], ...]}（猎手二级分组头 tooltip 用）。
    同概念下按 watchlist 顺序追加；读失败返回 {}（非致命）。"""
    out = {}
    try:
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        for code, info in (jy.items() if isinstance(jy, dict) else []):
            if not isinstance(info, dict):
                continue
            nm = str(info.get("name") or code)
            d = info.get("jiuyan_concept_reason")
            if not isinstance(d, dict):
                d = {}
            # 兜底理由（2026-10-24）：没人工写过的，用「主营」原文——只认**具体**的主营，
            # 「主营XX制造业」这种空话不要（会变成一屏废话）。
            biz = str(info.get("business_summary") or "").strip()
            fallback = ""
            prof = _company_profiles().get(code) or {}
            ptxt = str(prof.get("brief_title") or "").strip()
            if not ptxt:
                ptxt = str(prof.get("brief") or "").strip()[:58]
            if ptxt:
                fallback = "主营：" + ptxt[:58]
            elif biz and not biz.startswith("主营") and len(biz) > 8:
                fallback = "主营：" + biz[:46]
            for k in _jiuyan_concepts(info).split("|"):
                k = k.strip()
                if not k:
                    continue
                v = str(d.get(k) or "").strip() or fallback
                if v:
                    out.setdefault(k, []).append([nm, v, str(info.get("jiuyan_tag_verdict") or "")])
    except Exception:
        pass
    return out


# 行业粗分类规则（2026-09-29，突破箱体按行业分组用）。
#
# 为什么需要它：watchlist 的 `sector` 首段是**国民经济行业分类大类**（约 90 个，且部分条目
# 首段是概念而非行业）⇒ 直接用会碎成「125 只命中 → 55 组、其中 34 组只有 1 只」，反而不直观。
# 这里按关键词收敛到 ~32 个申万一级式大类：实测 125 只命中 → 24 组（单只组仅 5）。
#
# ⚠️ 两条关键约束（都是踩过的）：
#  1. **只匹配首段**，不拼接后续段：后续段是概念，会劫持匹配
#     （`汽车零部件/参股保险/…` 曾被 '保险' 抢成非银金融、`软件开发/互联网保险` 同理）。
#  2. **顺序敏感，特定先于宽泛**：`非金属矿物制品业` 含 '金属'、`黑色金属冶炼` 含 '金属'、
#     `水上运输业` 含 '水'、`酒店` 含 '酒' —— 都必须让更特定的规则先命中。
# 更新规则后请复跑 tests/phase3/test_breakout_daily_breakout.py 里的行业用例。
_INDUSTRY_RULES = (
    ('医药', '医药生物'), ('医疗', '医药生物'), ('中药', '医药生物'), ('生物', '医药生物'),
    ('卫生', '医药生物'), ('化学制药', '医药生物'),
    ('货币金融', '银行'), ('银行', '银行'), ('保险', '非银金融'), ('证券', '非银金融'),
    ('信托', '非银金融'), ('资本市场', '非银金融'), ('期货', '非银金融'), ('金融', '非银金融'),
    ('房地产', '房地产'), ('房屋', '房地产'), ('物业', '房地产'),
    ('半导体', '电子'), ('电子化学品', '电子'), ('消费电子', '电子'), ('军工电子', '电子'),
    ('光学', '电子'), ('元件', '电子'), ('面板', '电子'), ('电子', '电子'),
    ('软件', '计算机'), ('IT服务', '计算机'), ('计算机', '计算机'), ('互联网', '计算机'),
    ('通信', '通信'),
    ('电池', '电力设备'), ('光伏', '电力设备'), ('风电', '电力设备'), ('电网', '电力设备'),
    ('电力设备', '电力设备'), ('电气机械', '电力设备'), ('电机', '电力设备'), ('电源设备', '电力设备'),
    ('汽车', '汽车'),
    ('专用设备', '机械设备'), ('通用设备', '机械设备'), ('工程机械', '机械设备'),
    ('仪器仪表', '机械设备'), ('自动化', '机械设备'), ('机床', '机械设备'), ('轨交', '机械设备'),
    ('运输设备', '机械设备'), ('金属制品', '机械设备'), ('机械', '机械设备'),
    # 建材/钢铁必须排在 metals 之前：'非金属矿物'/'黑色金属' 都含 '金属'
    # ⚠️ 不要加裸的 ('冶','钢铁')：'有色金属冶炼和压延加工业' 含 '冶'，会被它抢成钢铁。
    ('非金属矿物', '建筑材料'), ('建材', '建筑材料'), ('水泥', '建筑材料'), ('玻璃', '建筑材料'),
    ('黑色金属', '钢铁'), ('钢铁', '钢铁'),
    ('建筑', '建筑装饰'), ('装饰', '建筑装饰'), ('土木', '建筑装饰'),
    ('化学', '基础化工'), ('化工', '基础化工'), ('塑料', '基础化工'), ('橡胶', '基础化工'),
    ('石化', '石油石化'), ('石油', '石油石化'), ('油气', '石油石化'),
    ('有色', '有色金属'), ('小金属', '有色金属'), ('工业金属', '有色金属'), ('金属', '有色金属'),
    ('煤炭', '煤炭'), ('采掘', '煤炭'),
    ('酒店', '社会服务'), ('教育', '社会服务'), ('旅游', '社会服务'), ('餐饮', '社会服务'),
    ('服务', '社会服务'),
    ('电力', '公用事业'), ('燃气', '公用事业'), ('水的生产', '公用事业'),
    ('环保', '环保'), ('环境', '环保'),
    ('食品', '食品饮料'), ('饮料', '食品饮料'), ('酒', '食品饮料'), ('精制茶', '食品饮料'),
    ('农林', '农林牧渔'), ('农业', '农林牧渔'), ('农副', '农林牧渔'), ('畜牧', '农林牧渔'),
    ('渔业', '农林牧渔'), ('饲料', '农林牧渔'),
    ('纺织', '纺织服饰'), ('服装', '纺织服饰'), ('服饰', '纺织服饰'),
    ('皮革', '纺织服饰'), ('制鞋', '纺织服饰'),
    ('家电', '家用电器'), ('厨', '家用电器'),
    ('物流', '交通运输'), ('运输', '交通运输'), ('航运', '交通运输'), ('航空', '交通运输'),
    ('港口', '交通运输'), ('铁路', '交通运输'), ('道路', '交通运输'), ('交运', '交通运输'),
    ('批发', '商贸零售'), ('零售', '商贸零售'), ('贸易', '商贸零售'),
    ('商业', '商贸零售'), ('商贸', '商贸零售'),
    ('传媒', '传媒'), ('新闻', '传媒'), ('出版', '传媒'), ('广播', '传媒'),
    ('影视', '传媒'), ('游戏', '传媒'),
    ('造纸', '轻工制造'), ('家具', '轻工制造'), ('包装', '轻工制造'),
    ('轻工', '轻工制造'), ('文教', '轻工制造'), ('玩具', '轻工制造'),
    ('国防', '国防军工'), ('航天', '国防军工'), ('军工', '国防军工'), ('船舶', '国防军工'),
    ('其他', '其他'), ('综合', '综合'),
    # —— 2026-09-29 补充：首段来自**东财行业三级名/长尾大类**，上面的关键词覆盖不到。
    # 按实测未分类 Top30 补齐（加完未分类从 354 只降到 ~150 只）。都是较特定的词，
    # 追加在末尾不会被前面的宽泛规则遮蔽（已复跑审查确认）。
    ('家用电器', '家用电器'), ('照明', '家用电器'),
    ('乘用车', '汽车'), ('商用车', '汽车'), ('底盘', '汽车'), ('车身', '汽车'),
    ('减速器', '机械设备'), ('工控', '机械设备'), ('能源及重型', '机械设备'),
    ('木材', '轻工制造'), ('印刷', '轻工制造'), ('家居', '轻工制造'), ('文娱', '轻工制造'),
    ('仓储', '交通运输'), ('装卸', '交通运输'), ('邮政', '交通运输'),
    ('耐火', '建筑材料'), ('专业工程', '建筑装饰'),
    ('农化', '基础化工'), ('原料药', '医药生物'),
    ('数字媒体', '传媒'), ('住宿', '社会服务'), ('公共设施', '公用事业'),
    ('芯片', '电子'), ('航海', '国防军工'),
)

#: 归不到任何大类时的桶名。与源数据里字面的「其他」区分开——后者是真的"其他"，
#: 前者是**我们不知道**（首段是概念、或 sector 为空），混在一起会误导。
_INDUSTRY_UNKNOWN = "未分类"


def _stock_industry(info) -> str:
    """股票记录 → 粗行业名（申万一级式）。见 `_INDUSTRY_RULES` 的两条约束。"""
    if not isinstance(info, dict):
        return _INDUSTRY_UNKNOWN
    first = str(info.get("sector") or "").split("/")[0].strip()
    if not first:
        return _INDUSTRY_UNKNOWN
    for kw, ind in _INDUSTRY_RULES:
        if kw in first:
            return ind
    return _INDUSTRY_UNKNOWN


def _stock_concepts(info, em_boards=None) -> list:
    """该股的「概念」列表（离线两源合并 + 可选东财第三源，2026-09-30）。

    源：
      1) 韭研概念 `jiuyan_concept1..9` / `jiuyan_concept`（见 `_jiuyan_concepts`）
      2) `sector` **首段以外**的板块段（首段是行业，已由「行业」列表达）
      3) 可选 `em_boards`：东财「所属板块」[{name,kind}]，**仅当 1+2 全空时启用**，
         取 kind=="概念" 的板块名并去掉「概念」后缀（"MicroLED概念"→"MicroLED"）。
         沿用第 2 源的剔除规则（地域/申万层级/与粗行业同名）。

    第 2 源要剔三类噪音，否则会把行业当成概念列出来：
      · **地域**：以「板块」结尾（福建板块）——不是概念
      · **申万层级**：以 Ⅰ/Ⅱ/Ⅲ 结尾（`IT服务Ⅱ`）——是行业层级，不是概念
      · **与粗行业同名**：如 `计算机`（`_stock_industry` 已归入「计算机」）——重复

    ⚠️ 离线覆盖率仅 **46.6%**（两源任一有）。`sector` 是东财「所属板块」混合串，
       而 legacy 条目只有一个行业段 ⇒ **贵州茅台这类完全没有概念**，是数据本身的限制，
       不是 bug。第 3 源由调用方通过 `_em_boards_disk_cached` 按需传入（逐只接口，
       只补命中票；见 `_scan_breakout`）。
    """
    if not isinstance(info, dict):
        return []
    out, seen = [], set()

    def _add(x):
        x = str(x).strip()
        if x and x not in seen:
            seen.add(x)
            out.append(x)

    for x in _jiuyan_concepts(info).split("|"):
        _add(x)
    ind = _stock_industry(info)
    for p in str(info.get("sector") or "").split("/")[1:]:
        p = p.strip()
        if not p or p.endswith("板块") or p in ind or p.endswith(("Ⅰ", "Ⅱ", "Ⅲ")):
            continue
        _add(p)
    # 第 3 源：离线两源全空时，东财「所属板块」里 kind=="概念" 的板块名
    if not out and em_boards:
        for b in em_boards:
            if not isinstance(b, dict) or b.get("kind") != "概念":
                continue
            nm = str(b.get("name") or "").strip()
            if nm.endswith("概念"):
                nm = nm[:-2].strip()
            if not nm or nm.endswith("板块") or nm in ind or nm.endswith(("Ⅰ", "Ⅱ", "Ⅲ")):
                continue
            _add(nm)
    return out


def _clean(obj):
    """递归清洗为 JSON 可序列化类型：nan/inf -> None，numpy 标量 -> 原生。"""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def _load_json(fp, default=None):
    try:
        with open(fp, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _tail_last_json_line(fp, max_bytes=65536):
    """读 jsonl 的**最后一条完整行**并解析（只读尾块，禁整文件读）。

    heartbeat_*.jsonl 单文件 1~4MB 且逐分钟追加，只取当日末条心跳时整文件读会拖慢
    pywebview 串行主线程。此处 seek 到末尾读 max_bytes，丢弃首个不完整行（除非恰好从行首开始），
    返回最后一条能 json.loads 的行；失败返回 None。
    """
    try:
        with open(fp, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            start = max(0, size - int(max_bytes))
            f.seek(start)
            buf = f.read()
    except Exception:
        return None
    text = buf.decode("utf-8", "replace")
    if start > 0:
        nl = text.find("\n")
        if nl < 0:
            return None
        text = text[nl + 1:]
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ---- K线弹窗「30分/60分」分时取数（2026-10-04）----
# 在线走 tushare **原生** freq；不复用 divergence._resample_minutes（它用 dt.floor，
# A股午休会把 60min 桶错位）。无 token / 超频 → 回退本地 tushare_mins 缓存（零网络）。
# 内存按「30 分钟时段」缓存：同一时段内秒回，跨时段自动重取。
_MIN_BARS_DIR = BASE / "t_io" / "cache" / "tushare_mins"
_MIN_BARS_CACHE: dict = {}
_MIN_BARS_LOCK = threading.Lock()
MIN_BARS_KEEP = 320                                    # 计算口径保留根数（OB 体检/历史判定热路径）
_MIN_BARS_DAYS = {"5min": 40, "10min": 45, "30min": 70, "60min": 130}
#                                                          ↑ 自然日：够 320 根（48/日、24/日、8/日、4/日）
# 10min：**tushare 无原生 10min** ⇒ 由 5min 合成（复用仓位结构层同一个
# `position_structure.resample_5min_to_10min`，保证 K 线与结构层 10min 口径一致）。

# ── 图表展示档深度（2026-10-30，仅 K 线弹窗取数链用）──────────────────────────
# owner：5/10 分钟图要能回看**近 3 个月**（≈65 交易日：5min ≈ 48 根/日 × 65 ≈ 3120，
# 10min ≈ 24 根/日 × 65 ≈ 1560）。30/60min 维持 320（与 MIN_BARS_KEEP 相同，行为不变）。
# ⚠️ 为什么单独一张表、不改 MIN_BARS_KEEP：后者被**计算热路径**复用
#    （`_m30_bars_cache_only` / `_min_bars_asof` 等的 tail 截断），那里变深会拖慢体检。
#    而 `_fetch_min_bars` 的 5/10min 调用方**全部是**图表路径（弹窗建图 / 实时尾 /
#    10min 合成兜底），已逐处核实（2026-10-30），故在 `_fetch_min_bars` 尾部按本表加深。
MIN_BARS_CHART = {"5min": 3200, "10min": 1600, "30min": 320, "60min": 320}
# 历史深度来源：磁盘 plain+_d540 合并档；不足时**一次性** tushare 补 95 自然日（≈65 交易日）。
# 只列 5min —— 10min 严禁单独请求 tushare（无此 freq），由深 5min 合成。
_MIN_CHART_DEEP_DAYS = {"5min": 95}

# K 线弹窗的分钟档 **单一真源**（2026-10-20 加 5 分档）。
# ⚠️ 档位清单只在这里写一次：payload 键、按需取数、strip/keep 白名单、版本签名、周期校验
#    全从这三张表取。原来同一个元组 `("min10","min30","min60")` 在 6 处各写一份，
#    加一档就得改 6 遍、漏一处就是"某个 Tab 永远空白"（10min 合成 bug 就是这么卡了两天）。
_MIN_PERIODS = ("min5", "min10", "min30", "min60")
_MIN_PERIOD_FREQ = {"min5": "5min", "min10": "10min", "min30": "30min", "min60": "60min"}
_MIN_PERIOD_MINUTES = {"min5": 5, "min10": 10, "min30": 30, "min60": 60}

# ── tushare 分钟线的熔断（2026-10-18，GUI 卡死专项）──────────────────────────
# 卡死栈实测：`refresh_ob_m30` 的 3 个 worker 同时卡在 tushare 的
# `socket.getaddrinfo`（DNS 解析**不受 requests 的 timeout 约束**），叠加 tushare 默认
# 30s 超时 + 3 次重试 ⇒ 单只可挂 90s+，界面心跳被拖停。处置：①腾讯优先（见
# `_fetch_min_bars`）；②tushare 调用放守护线程里**硬等 8s**；③失败/超时/过慢 ⇒ 冷却 5 分钟。
_TUSHARE_MIN_COOLDOWN_S = 300.0
_TUSHARE_MIN_STATE = {"until": 0.0}
_TUSHARE_MIN_LOCK = threading.Lock()

# ---- 盘中实时 K 线（2026-10-17）----
# 前端在弹窗打开期间每 3s 调 `load_chart_realtime`；整图 10s 轮询也会撞进来。
# 2 秒内同 (code, period) 只打一次网络（腾讯 mkline + 快照 ≈0.1~0.3s）。
# 2026-10-18：改为 **SWR**（`load_chart_realtime` 只读缓存、后台线程算）——
#   真实进程里这个入口原要 1.5~3.0s（`gui_slow_calls.log`），每 3s 投递一次会拖死
#   pywebview 的串行 `evaluate_js`。_CHART_LIVE_STATE 记录每个键是否已有 worker 在跑。
_CHART_LIVE_TTL = 1.2           # 2026-10-19：持仓 K 线 2s 一拉 ⇒ TTL 必须 <2s 才能每拉都刷新
_CHART_LIVE_MIN_KICK = 0.8      # 两次后台刷新之间的最小间隔（防抖）
# 2026-10-20：10 分钟图盘中按 1s 轮询（owner 重点盯的周期）⇒ SWR 也要更短，
# 否则 1s 的请求一直吃同一个缓存帧。10/30/60 分统一用这组更短的阈值。
# 2026-10-20 二轮（owner：5/10 分要「极其实时」）：worker 拆成**并行两跳**后实测
# 从 ~270ms 降到 ~200ms，故 TTL/kick 再收紧到 0.4/0.35，配合前端 0.5s 轮询 ⇒ 约 2 帧/秒。
_CHART_LIVE_TTL_MINUTE = 0.4
_CHART_LIVE_MIN_KICK_MINUTE = 0.35
_CHART_LIVE_CACHE: dict = {}
_CHART_LIVE_STATE: dict = {}
_CHART_LIVE_LOCK = threading.Lock()
_CHART_LIVE_MAX = 200

# ---- 多窗口看盘的**轻量帧**缓存（2026-10-21）----
# 六宫格不复用单图的实时尾：那条现在建立在 3 个月深档（近 3000 根）上，~1s/只，
# 6 只并发全卡 GIL ⇒ 一帧几十秒，界面上就是"K 线几乎不更新"。这里另开一套只算
# K 线/MA5/成交额（120 根），TTL 2s。
_MW_CACHE: dict = {}
_MW_RUN: dict = {}
_MW_LOCK = threading.Lock()
_MW_TTL = 4.0


def _slim_if_same(frame, known_sig):
    """前端已持有同一帧（`known_sig` == 帧的 sig）⇒ **不重复传序列**。

    一帧分钟序列实测 **102 KB**；5/10 分按 0.5s 轮询时，行情没动的每一跳都在把同样的
    100KB 塞过 pywebview 的串行 `evaluate_js`。这里只回 sig/quote（<1KB），前端拿到
    `unchanged=true` 就跳过重画 —— 传输量在静止时降两个数量级。
    没有 `known_sig`（老前端/刚切周期）就照旧回整帧，行为完全兼容。
    """
    if not known_sig or not frame:
        return frame
    if str(known_sig) != str(frame.get("sig") or ""):
        return frame
    o = {k: v for k, v in frame.items() if k != "series"}
    o["series"] = None
    o["unchanged"] = True
    return o

# ---- 手动盘「K线速查」：名称/代码 → 标的（2026-10-18）----
# 本地名称表（韭研 5382 + NAMES + 持仓）600s 缓存；在线搜索（腾讯 smartbox）结果 600s 缓存
# （空结果只缓存 60s，网络抖动后能快速重试）。
_SEARCH_CACHE: dict = {}
_SEARCH_CACHE_TTL = 600.0
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE_MAX = 200
_INSTRUMENT_NAMES = {"ts": 0.0, "map": {}}
_INSTRUMENT_NAMES_TTL = 600.0
_INSTRUMENT_NAMES_LOCK = threading.Lock()

# ---- 持仓体检（OB）30 分钟快照层（2026-10-09）----
# 背景：体检表趋势/背离/风险提醒改用 30min 口径。`load_ob_analysis` 跑在 pywebview 串行主线程，
# 任何网络调用都会冻界面 ⇒ 该层**只读磁盘/内存缓存**（零网络），由后台按 30 分钟时段预热。
_M30_OB_ENABLED = True                                 # 回滚开关：False ⇒ 体检表退回日线口径
_OB_M30_STATE: dict = {}                               # 刷新守护线程幂等标记 {"started": bool}
# 磁盘预热单飞（2026-10-18）：OB 表发现当前 slot 未热时按需补算（零网络）。
# 背景：`start_ob_m30_refresher` 的**在线**刷新已改成只在交易时段跑；周末/盘后 slot 每 30 分钟
# 变一次而没人填充 ⇒ 体检表「30min 判定」永远停在「计算中」（owner 截图 2026-10-18）。
_OB_M30_DISK_WARM: dict = {"running": False, "slot": None}
_OB_M30_DISK_LOCK = threading.Lock()
_M30_SNAP_CACHE: dict = {}                             # {f"{base}_{slot}": snapshot}
_M30_SNAP_LOCK = threading.Lock()
_M30_SNAP_MAX = 2000                                   # 无界增长防护
_M30_MIN_BARS = 60                                     # == Trend30Config.min_bars / m30_features.MIN_BARS
_M30_DISPLAY_MAX_GAP_DAYS = 12                         # 与 chart_cache.MAX_DISPLAY_GAP_DAYS 一致

# ---- 建仓信号扫描「30min 判定」列（2026-10-09）----
# 与体检表**同口径**（`m30_features.verdict_from_features`），但判定只用 feats+div、**不用 trend**
# ⇒ 跳过 trend30 状态机（~300ms/只），只算顶部特征+背离（~17ms/只）。整池 ~53 只 ≈ 0.9s（后台）。
# 独立缓存（键 {code}_{slot}），与体检表的全量快照 `_M30_SNAP_CACHE` 互不覆盖。
_PB_M30_CACHE: dict = {}
_PB_M30_LOCK = threading.Lock()
_PB_M30_MAX = 3000
_PB_M30_WARM: dict = {"slot": None, "queued": set(), "running": False}   # 按 code 排队（原"每时段一轮"闸会被建仓表 10s 轮询抢走 ⇒ 风向标 30min 判定整列卡 ⏳ 30min）
_PB_M30_WARM_LOCK = threading.Lock()
_PB_M30_QUEUE_MAX = 500                              # 单槽位排队上限（风向标 ~323 只 + 建仓池）

# ---- 市场风向标「重列」的 SWR 缓存（2026-10-13）----
# 88 只 × `_hunter_build_conformance` ≈ 4.8s，**绝不能在 pywebview 主线程同步算**（见
# `load_market_beacon` 的注释）。首帧只给轻列、重列后台算完落这里、前端短轮询补齐。
# key = (数据日, 代表股元组)；换板块/换日自然失配 ⇒ 重算。
_BEACON_ENRICH: dict = {"key": None, "ts": 0.0, "rows": None, "running": False}
# 骨架（成分/涨幅/分档）SWR：全部板块要读 ~1300 个日线缓存 ≈6s ⇒ 同样后台算
_BEACON_SKELETON: dict = {"key": None, "ts": 0.0, "val": None, "running": False}
_BEACON_LOCK = threading.Lock()
# 成分股 涨幅/成交额 的 mtime 缓存（见 load_market_beacon 内 _daily_stats 的注释）
_BEACON_STATS: dict = {}
# 历史日「30min 判定」（2026-10-10）：键 {base}_{date}，值 = {level,label,reason,asof} 或 None。
# 由「扫描该日」时从**磁盘 30min 缓存**截到该日预算好（零网络），load_day 浏览历史只回填、不现算。
_PB_M30_HIST: dict = {}
_PB_M30_HIST_LOCK = threading.Lock()
_PB_M30_HIST_MAX = 5000

# ---- 建仓信号扫描「仓位结构」列（2026-10-11）----
# 30min/10min **结构参考**（止损位/阻力/补仓三档/阶段/中期方向），供**仓位管理**用。
# ⚠️ 不是买卖信号：本层模块 `core/position_structure.py` 已实测——机械按"阶段2加仓/阶段3减仓"
#    做仓位择时的纯择时贡献不显著，唯一有证据的是"破位后在**下档支撑**挂限价补仓"。
# 取数与纪律完全沿用「30min 判定」列：**后台预热 + 按 30min 时段缓存，主线程零计算零网络**
# （pywebview 串行主线程，任何网络都会冻界面）。数据源 = tushare 5min(→10min)/30min + 日线。
_PB_STRUCT_CACHE: dict = {}
_PB_STRUCT_LOCK = threading.Lock()

# ---- 后台「纯 pandas」批活的全局限额（2026-10-22）----------------------------------
# 实测（20:37 哨兵栈）：GUI 进程 106% 单核、7 个线程同时在 `_one`（仓位结构预热 4 + 标签批 3），
# 而 pywebview 的 UI 线程**每处理一个 js_api 调用都要 `Thread.start()` 并等新线程被调度**
# —— 这种 GIL 争用下它会长时间拿不到时间片，界面上就是"点什么都没反应"，可 CPU 只有 1 核。
# 本机 4 物理核；把**这两类批活合计**压到最多 2 个并发（原来最高 7），给 UI 线程让出 CPU。
# 代价：批活墙钟≈翻倍（都是懒加载列，晚几秒自填，前端本就轮询补齐）。
_BG_PANDAS_MAX = 2
_BG_PANDAS_SEM = threading.BoundedSemaphore(_BG_PANDAS_MAX)
_PB_STRUCT_MAX = 3000
# 预热队列（2026-10-13 改造）：原来是「全局每 30min 时段只放一轮」的闸 —— 建仓表 10s 轮询
# 总是先抢到名额，OB 表/风向标的预热请求同一时段内被静默丢弃 ⇒ 结构列最长卡 30 分钟。
# 改为按 code 排队：槽位内去重、worker 循环分批排干（每轮 ≤_PB_STRUCT_POOL_MAX、2 线程）。
_PB_STRUCT_WARM: dict = {"slot": None, "queued": set(), "running": False}
_PB_STRUCT_WARM_LOCK = threading.Lock()
_PB_STRUCT_QUEUE_MAX = 240                              # 单槽位排队上限（防误传全池打爆 tushare）
# 历史日「仓位结构」：键 {base}_{date}。由「扫描该日」时**纯磁盘、零网络**截到该日预算好。
_PB_STRUCT_HIST: dict = {}
_PB_STRUCT_HIST_LOCK = threading.Lock()
_PB_STRUCT_HIST_MAX = 5000
_PB_STRUCT_POOL_MAX = 60                                # 单轮预热标的数上限（防误传全池打爆 tushare）

# ---- 当日「均线回站」+ 突破时刻（2026-10-23 owner）----------------------------------
# 「建仓信号扫描 / 持仓 30 分钟风险体检 / 异常监控池」三张表共用这一份缓存：
#   某只票**当日**刚站上 5/10/20/30/60 日线 → 给出**突破的那一分钟**。
# 口径见 `core/ma_reclaim.reclaim_today`（与 `ma5_state` 逐字同源，只是周期可变）；
# 时刻用**今日 5min bar** 逐根回放（腾讯 m5，含未收盘根，约 100~300ms/只）
# ⇒ 必须后台预热，主线程只读缓存（pywebview 主线程任何网络/重活都会冻界面）。
_MA_RC_CACHE: dict = {}          # code -> {"date":..., "ts":..., "ma": {period: {time,ma,dev_pct}}}
_MA_RC_LOCK = threading.Lock()
_MA_RC_WARM: dict = {"queued": set(), "running": False}
_MA_RC_WARM_LOCK = threading.Lock()
_MA_RC_TTL = 120.0               # 盘中 120s；收盘后 6h（分钟已定稿）
_MA_RC_QUEUE_MAX = 300
_MA_RC_ERR_TTL = 600.0           # 取数失败（停牌/北交所无分时/网络）的负缓存：10 分钟就重试，
                                 # 别用 6 小时把一列永久钉成「—」（那是"收盘后 TTL"的副作用）


def _ma_rc_fresh(rec, date, now, ttl):
    """缓存条目是否可用：同一日期 + 未过期；**失败条目**用更短的 `_MA_RC_ERR_TTL`。"""
    if not rec or rec.get("date") != date:
        return False
    cap = _MA_RC_ERR_TTL if rec.get("err") else ttl
    return (now - rec.get("ts", 0)) < cap



def _min_bars_ts_code(code):
    """6 位股票/指数码 → tushare ts_code（sh000001→000001.SH）。
    不复用 divergence._ts_code：它不认 sh/sz 前缀（指数会拼错），且有其它调用方。"""
    c = str(code).split("_")[0]
    pre = c[:2].lower()
    if pre in ("sh", "sz", "bj") and c[2:].isdigit():
        return c[2:] + "." + {"sh": "SH", "sz": "SZ", "bj": "BJ"}[pre]
    if len(c) == 9 and c[3] == "." and c[:3].isdigit():       # 已是 ts_code 形式
        return c.upper()
    if len(c) == 6 and c.isdigit():
        ex = "SH" if c[0] in "56" else ("BJ" if c[0] in "48" else "SZ")
        return f"{c}.{ex}"
    return ""


def _min_bars_slot():
    """当前 30 分钟时段键（跨时段才重取）。"""
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:") + ("00" if now.minute < 30 else "30")


def _in_trading_session(now=None):
    """**粗口径**交易时段（含 09:15 竞价与 15:05 收盘后缓冲）。

    ⚠️ 只用来选**缓存策略/取数源**，不是交易闸 —— 误判的代价只是"多打一次网络"
    或"少打一次网络"，不会影响任何下单语义。周末/节假日一律 False（节假日会被判成
    交易日，代价同样只是一次空请求）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return ("09:15" <= hm <= "11:35") or ("12:55" <= hm <= "15:05")


def _minute_bucket_end(now, minutes):
    """当前**正在形成/刚完成**的分钟 K 线右端时间（A股 09:30-11:30 / 13:00-15:00）。

    2026-10-19：持仓 K 线实时性加固。腾讯的 m30/m60 序列**不一定**在每个 tick 都更新
    未收盘根的 close（观察到的行为是桶边界推进），而持仓用户盯的就是最后一根 —— 用实时
    快照把最后一根补/修成当前价格，见 `_patch_forming_minute_bar`。
    返回 datetime；09:30 前返回 None；午休返回 11:30；收盘后返回 15:00。
    """
    minutes = max(1, int(minutes))
    am0 = now.replace(hour=9, minute=30, second=0, microsecond=0)
    am1 = now.replace(hour=11, minute=30, second=0, microsecond=0)
    pm0 = now.replace(hour=13, minute=0, second=0, microsecond=0)
    pm1 = now.replace(hour=15, minute=0, second=0, microsecond=0)
    if now < am0:
        return None
    if now > pm1:
        return pm1
    if am1 < now < pm0:
        return am1

    def _ceil(base, t):
        elapsed = (t - base).total_seconds() / 60.0
        k = int(math.ceil(elapsed / minutes - 1e-9))
        k = max(1, min(k, 120 // minutes))
        return base + timedelta(minutes=k * minutes)

    return _ceil(am0, now) if now <= am1 else _ceil(pm0, now)


def _patch_forming_minute_bar(df, minutes, quote, now):
    """用实时快照把分钟 K 线的**最后一根**修正/补齐为当前正在形成的 bar。

    · 末根时间 == 当前桶 ⇒ 修正 close/high/low（不动 volume，快照只有当日累计量）；
    · 末根时间 < 当前桶 ⇒ 补一根 O=上一根收盘（当日首根用当日开盘）、C=现价、H/L=max/min(O,C)、量=0。
    只在 `quote.ts_date == 今天` 时动手（休市/节假日不造根）。失败原样返回。
    """
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return df
    if not quote or not quote.get("price"):
        return df
    today = now.strftime("%Y-%m-%d")
    if str(quote.get("ts_date") or "") != today:
        return df
    px = float(quote.get("price") or 0)
    if px <= 0:
        return df
    end = _minute_bucket_end(now, minutes)
    if end is None:
        return df
    _end = pd.Timestamp(end)
    last_t = pd.Timestamp(df["time"].iloc[-1])
    if last_t >= _end:
        if str(last_t)[:10] == today:
            _i = df.index[-1]
            _c = float(df.at[_i, "close"])
            df.at[_i, "close"] = px
            df.at[_i, "high"] = max(float(df.at[_i, "high"]), px, _c)
            df.at[_i, "low"] = min(float(df.at[_i, "low"]), px, _c)
        return df
    if str(last_t)[:10] == today:
        op = float(df["close"].iloc[-1])
    else:
        op = float(quote.get("open") or px)
    row = {"time": _end, "open": op, "high": max(op, px), "low": min(op, px),
           "close": px, "volume": 0.0}
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def _min_bars_slot_live(now=None):
    """**盘中**的分钟线缓存键：10 秒一档（原 30 分钟一档会让分钟 K 线在盘中冻住半小时）。

    只给 `_fetch_min_bars`（K 线弹窗的在线路径）用；体检/热身那条离线路径仍用
    `_min_bars_slot` 的 30 分钟语义（它靠跨时段来触发重算，绝不能被改成 10 秒）。
    """
    now = now or datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:") + f"{now.second // 10}0"


def _norm_min_bars(df):
    """统一分时帧列名/类型/排序；缺列或空 → 空 DataFrame。"""
    import pandas as pd
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume", "date": "time"})
    need = {"time", "open", "high", "low", "close", "volume"}
    if not need.issubset(df.columns):
        return pd.DataFrame()
    df = df[["time", "open", "high", "low", "close", "volume"]].copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").drop_duplicates(subset=["time"])
    return df.reset_index(drop=True)


def _calc_ma_and_indicators(d):
    """MA(7 条)/MACD/RSI(Wilder)/BOLL。2026-10-04 从 _build_chart_from_df 的嵌套函数抽出，
    供 K线 payload 缓存「仅补日线末根」时复用（避免整图重建）。"""
    d = d.copy()
    for n in (5, 10, 20, 30, 60, 180, 365):
        d[f"ma{n}"] = d["close"].rolling(n).mean()
    ema12 = d["close"].ewm(span=12, adjust=False).mean()
    ema26 = d["close"].ewm(span=26, adjust=False).mean()
    d["dif"] = ema12 - ema26
    d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
    d["macd_hist"] = (d["dif"] - d["dea"]) * 2
    from analysis.indicators import wilder_rsi as _wilder_rsi
    d["rsi"] = _wilder_rsi(d["close"], 14)
    d["boll_mid"] = d["close"].rolling(20).mean()
    d["boll_std"] = d["close"].rolling(20).std()
    d["boll_up"] = d["boll_mid"] + 2 * d["boll_std"]
    d["boll_dn"] = d["boll_mid"] - 2 * d["boll_std"]
    return d


_DIV_SCHEMA = 2      # 2 = 2026-10-20 展示口径（周期缩放 + 柱线动量衰减）；改逻辑时 bump


_TREND_SCHEMA = 2    # 趋势线口径版本：2=按周期各算一份（2026-10-22 二轮）。
#                     改摆动定义/容差/结构时 bump —— 它会混进 `_chart_version`，
#                     迫使前端丢掉「unchanged」握手、重新取一次 payload。


def _trendlines_from_series(dates, ohlc):
    """一条序列（任意周期）→ 趋势线两端点。口径见 `_attach_trendlines`。"""
    n = min(len(dates or []), len(ohlc or []))
    if n < 30:
        return {"up": None, "dn": None}
    lows = [float(o[2]) for o in ohlc[:n]]
    highs = [float(o[3]) for o in ohlc[:n]]
    k = 3
    base = max(0, n - 160)          # 只在最近 160 根里找摆动点（更早的与当前趋势无关）

    def _pivots(vals, is_low):
        out = []
        for i in range(base + k, n - k):
            seg = vals[i - k:i + k + 1]
            if is_low:
                if vals[i] <= min(seg):
                    out.append(i)
            elif vals[i] >= max(seg):
                out.append(i)
        return out

    pl = _pivots(lows, True)
    ph = _pivots(highs, False)

    def _pick(points, vals, want_up):
        """从最近的摆动点往回找一对：方向一致（依次抬高/降低）且间隔 ≥5 根。"""
        for a in range(len(points) - 1, 0, -1):
            i1 = points[a]
            for b in range(a - 1, -1, -1):
                i0 = points[b]
                if i1 - i0 < 5:
                    continue
                if want_up and vals[i1] <= vals[i0]:
                    continue
                if (not want_up) and vals[i1] >= vals[i0]:
                    continue
                return i0, i1
        return None

    def _mk(pair, vals, kind):
        i0, i1 = pair
        y0, y1 = vals[i0], vals[i1]
        slope = (y1 - y0) / max(1, (i1 - i0))
        y_now = y1 + slope * (n - 1 - i1)
        src = pl if kind == "up" else ph
        tol = max(1e-9, 0.015 * abs(y0))
        touches = sum(1 for p in src
                      if abs(vals[p] - (y0 + slope * (p - i0))) <= tol)
        return {"i0": i0, "y0": round(y0, 3), "i1": i1, "y1": round(y1, 3),
                "i_now": n - 1, "y_now": round(y_now, 3),
                "slope_pct": round(slope / (abs(y0) or 1.0) * 100.0, 4),
                "touches": int(touches),
                "date0": str(dates[i0])[:10], "date1": str(dates[i1])[:10]}

    up_pair = _pick(pl, lows, True)
    dn_pair = _pick(ph, highs, False)
    return {"up": _mk(up_pair, lows, "up") if up_pair else None,
            "dn": _mk(dn_pair, highs, "dn") if dn_pair else None,
            "n": n}


def _attach_trendlines(payload):
    """给 K 线 payload 增加 `trendlines`：**摆动高低点**连出的趋势线（2026-10-22 owner）。

    两条线各自独立判断，可以只有一条、也可以都没有：
      · **上升趋势线**（支撑）：最近两个**依次抬高**的摆动低点连线；
      · **下降趋势线**（压力）：最近两个**依次降低**的摆动高点连线。
    摆动点 = 局部极值（左右各 `k=3` 根都不越过它）；两点之间至少隔 5 根，避免把一根小
    反弹的两端当成趋势。线**从第一个摆动点画到最新一根**——交易上要的是"这条线现在在哪"。
    每条线带 `touches`：线 ±1.5% 范围内还有多少个同类摆动点。只做**标注**，不参与任何判定。

    结构：**每个周期各算一份** —— `{"schema":1, "daily":{up,dn,n}, "weekly":…, "min5":…}`。
    2026-10-22 二轮（owner：其它周期也要同步显示）：端点索引是**该周期自己的 K 线下标**，
    前端按当前 Tab 取对应那份即可；不同周期混用会整体错位（这也是先前只画日线的原因）。
    """
    try:
        pd_all = payload.get("period_data") or {}
        out = {"schema": _TREND_SCHEMA}
        for key in ("daily", "weekly", "monthly") + tuple(_MIN_PERIODS):
            s = pd_all.get(key) or {}
            dates = s.get("dates") or []
            ohlc = s.get("ohlc") or []
            if not dates or not ohlc:
                continue
            out[key] = _trendlines_from_series(dates, ohlc)
        if len(out) == 1:                      # 一个周期都没有（异常 payload）
            out["daily"] = {"up": None, "dn": None}
        payload["trendlines"] = out
    except Exception:
        payload.setdefault("trendlines", {"schema": _TREND_SCHEMA,
                                          "daily": {"up": None, "dn": None}})
    return payload


def _attach_divergence(payload):
    """给 K 线 payload 增加 `divergence`：各周期顶/底背离事件（2026-10-19 owner 需求）。

    只做**标注**，不参与任何交易判定。口径复用
    `analysis.divergence.detect_divergence_events`（价格创新高/低 + MACD DIF 未创新高/低，
    含 price_excess / DIF 价区 / 摆动门槛三道过滤）。

    两类标注合并：
      1. DIF 两峰背离（`detect_divergence_events`，经典口径）；
      2. **柱线动量衰减**（`detect_hist_fade_events`，价格新高但红柱已缩短；标注「·柱」）。
     科创50 2026-06-30/07-01 属于第 2 类——标准 DIF 两峰没有背离（DIF 反而创新高）。

    ⚠️ **展示口径 ≠ 信号口径**（2026-10-19）：摆动门槛按周期缩放，见函数内 `_PROFILE`。
    信号路径（`detect_minute_divergence_detail` / 建仓表）**保持原参数不动**。

    `period_data[*].ohlc` 的顺序是 **ECharts candlestick 口径 [open, close, low, high]**。
    事件携带 `index`（该周期 dates 的下标）供前端直接画点。
    """
    # 2026-10-20 提速：payload 已是当前口径 ⇒ **先返回，连 import 都不做**。
    # 这是「打开 K 线变慢」的直接修复——之前每次缓存命中都白 import pandas+analysis.divergence
    # （冷 ~1.5s；热也会白算 6 个周期 ~56ms）。
    if payload.get("divergence_schema") == _DIV_SCHEMA and isinstance(payload.get("divergence"), dict):
        return payload
    try:
        import pandas as pd
        from analysis import divergence as _dv
    except Exception:
        payload.setdefault("divergence", {})
        return payload
    # 展示口径 ≠ 信号口径（2026-10-19 owner：「MACD 背离漏掉很多」）：
    #   · 摆动门槛 2.5×/4× 是**用分钟线标定**的；日线中位振幅 2.6~5.4% ⇒ 要求 7~22% 的
    #     逆向摆动才算「两个峰」，绝大多数真实背离被误杀（实测 000988 6→10、300475 3→8、
    #     002202 3→8）。展示时按周期缩放：日/周/月 1.0×/1.5×，分钟 1.5×/2.0×。
    #   · 周/月线根数天然少（800 日≈38 月），最小窗口按周期放宽，否则月线永远无标注。
    #   · price_excess / require_dif_zone 保持与信号口径一致（owner 2026-09-20 报 002202
    #     的「微新高」与「DIF 深负区小反弹」假阳性就是这两道闸挡掉的）。
    _PROFILE = {
        "daily":   dict(swing_mult=1.0, swing_mult_top=1.5, min_bars=40),
        "weekly":  dict(swing_mult=1.0, swing_mult_top=1.5, min_bars=30),
        "monthly": dict(swing_mult=1.0, swing_mult_top=1.5, min_bars=24),
        "min5":    dict(swing_mult=1.5, swing_mult_top=2.0, min_bars=40),
        "min10":   dict(swing_mult=1.5, swing_mult_top=2.0, min_bars=40),
        "min30":   dict(swing_mult=1.5, swing_mult_top=2.0, min_bars=40),
        "min60":   dict(swing_mult=1.5, swing_mult_top=2.0, min_bars=40),
    }
    out = {}
    for name, s in (payload.get("period_data") or {}).items():
        try:
            dates = (s or {}).get("dates") or []
            ohlc = (s or {}).get("ohlc") or []
            n = min(len(dates), len(ohlc))
            _kw = _PROFILE.get(name, dict(swing_mult=1.5, swing_mult_top=2.0, min_bars=40))
            if n < int(_kw["min_bars"]):
                continue
            df = pd.DataFrame({
                "time": dates[:n],
                "open": [r[0] for r in ohlc[:n]],
                "close": [r[1] for r in ohlc[:n]],
                "low": [r[2] for r in ohlc[:n]],
                "high": [r[3] for r in ohlc[:n]],
            })
            ev = _dv.detect_divergence_events(df, **_kw)
            # 第二类：MACD 柱线动量衰减（价格新高但红柱已缩短；行情软件常用口径）。
            # 科创50 2026-06-30/07-01 就是这类——DIF 两峰对比无背离，柱峰却已回落。
            fade = _dv.detect_hist_fade_events(df)
            _by_idx = {}
            for e in ev:
                _by_idx[int(e["index"])] = {
                    "index": int(e["index"]), "time": str(e.get("time")),
                    "type": "顶" if e.get("type") == "顶" else "底",
                    "price": float(e.get("price")),
                    "consec": bool(e.get("consec")),
                    "bars_ago": int(e.get("bars_ago", 0)),
                    "dif": round(float(e.get("dif") or 0), 4),
                    "source": "dif_swing",
                }
            for e in fade:                      # 同一根已有 DIF 事件时以 DIF 为准
                _i = int(e["index"])
                if _i in _by_idx:
                    continue
                _by_idx[_i] = {
                    "index": _i, "time": str(e.get("time")),
                    "type": "顶" if e.get("type") == "顶" else "底",
                    "price": float(e.get("price")),
                    "consec": bool(e.get("consec")),
                    "bars_ago": int(e.get("bars_ago", 0)),
                    "dif": round(float(e.get("dif") or 0), 4),
                    "hist": round(float(e.get("hist") or 0), 4),
                    "source": "hist_fade",
                }
            out[name] = [_by_idx[k] for k in sorted(_by_idx)][-80:]  # 上限防 payload 过大
        except Exception:
            continue
    payload["divergence"] = out
    payload["divergence_schema"] = _DIV_SCHEMA
    return payload


def _index_code(code) -> bool:
    """带市场前缀的**指数**码（sh000001 / sz399001 / sh000300 / bj899050…）。

    裸 6 位 `000001` 是**平安银行**不是上证指数 ⇒ 只在带前缀时才判指数
    （与 `core.order_flow._index_like` 同口径）。
    """
    c = str(code).strip().lower()
    if c[:2] not in ("sh", "sz", "bj") or not c[2:].isdigit():
        return False
    _d = c[2:]
    # ⚠️ 不能只认「0000/3990」这几个前缀：沪深300=sh000300、中证500=sh000905、
    #    科创50=sh000688 都是 `000xxx`，只判 `0000` 会把它们当成个股。
    #    带前缀时：sh 的 `000…` 全是指数（沪市个股 60/68、ETF 5、债 1）；
    #    sz 的 `399…` 是指数（深市个股 00/30）；bj 的 `899…` 是指数。
    return ((c[:2] == "sh" and _d.startswith("000"))
            or (c[:2] == "sz" and _d.startswith("399"))
            or (c[:2] == "bj" and _d.startswith("899")))


def _ensure_amount(df, code=None):
    """给日线/分钟帧补 `amount`（元）—— 2026-10-20 owner：「显示成交额，我不要成交量」。

    腾讯的 K 线只有 `volume`（**手**），没有成交额字段；GM 的日线同样没有。
    用 VWAP 近似：`amount = (high+low+close)/3 × volume × 100`
    （×100 = 每股/手；本仓缓存里 688 的 volume 也已归一到手，见 provider）。
    精度：实测 688008 当日 41.36 亿 vs 东财 41.32 亿（差 0.1%），够看量能。
    已有 `amount` 列的帧原样返回（将来接入真成交额时无需改这里）。

    ⚠️ **指数不算成交额**（2026-10-21）：`× close` 对指数没有意义（点位不是股价）——
    实测上证指数 5 分钟会算出 **2.7 万亿**。指数没有 amount 就原样返回，
    前端自动回落「成交量(手)」并如实标注口径，绝不编一个假成交额。
    """
    try:
        if df is None or getattr(df, "empty", True) or "amount" in df.columns:
            return df
        if code is not None and _index_code(code):
            return df
        d = df.copy()
        _mid = (d["high"].astype(float) + d["low"].astype(float) + d["close"].astype(float)) / 3.0
        d["amount"] = _mid * d["volume"].astype(float) * 100.0
        return d
    except Exception:
        return df


def _ema_macd(closes, fast=12, slow=26, signal=9):
    """收盘价 → (DIF, DEA, HIST)。`HIST = (DIF - DEA) × 2`（国内行情软件口径）。

    与单图 `_calc_ma_and_indicators` 里的 MACD 同参数。⚠️ 多窗口只展示 120 根，
    但**必须先把完整序列喂进来**再截尾：EMA 的头几十根严重受种子影响，
    只喂 120 根算出来的 MACD 与单图对不上。
    """
    dif, dea, hist = [], [], []
    _ef = _es = _ed = None
    _kf, _ks, _kd = 2.0 / (fast + 1), 2.0 / (slow + 1), 2.0 / (signal + 1)
    for c in closes:
        _ef = c if _ef is None else c * _kf + _ef * (1 - _kf)
        _es = c if _es is None else c * _ks + _es * (1 - _ks)
        _d = _ef - _es
        _ed = _d if _ed is None else _d * _kd + _ed * (1 - _kd)
        dif.append(_d)
        dea.append(_ed)
        hist.append((_d - _ed) * 2.0)
    return dif, dea, hist


def _to_series(d, intraday=False):
    """DataFrame → 前端序列 dict（与 _build_chart_from_df 同口径；抽出以复用）。"""
    import pandas as pd
    _fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
    out = {
        "dates": [x.strftime(_fmt) for x in d["date"]],
        "ohlc": [[round(o, 3), round(c, 3), round(l, 3), round(h, 3)]
                 for o, c, l, h in zip(d["open"], d["close"], d["low"], d["high"])],
        "volume": [round(float(v), 0) for v in d["volume"]],
        "ma": [[round(x, 3) if not pd.isna(x) else None for x in d[f"ma{n}"]]
               for n in (5, 10, 20, 30, 60, 180, 365)],
        "macd": {"dif": [round(x, 3) if not pd.isna(x) else None for x in d["dif"]],
                 "dea": [round(x, 3) if not pd.isna(x) else None for x in d["dea"]],
                 "hist": [round(x, 3) if not pd.isna(x) else None for x in d["macd_hist"]]},
        "rsi": [round(x, 1) if not pd.isna(x) else None for x in d["rsi"]],
        "boll": {"mid": [round(x, 3) if not pd.isna(x) else None for x in d["boll_mid"]],
                 "up": [round(x, 3) if not pd.isna(x) else None for x in d["boll_up"]],
                 "dn": [round(x, 3) if not pd.isna(x) else None for x in d["boll_dn"]]},
    }
    # 成交额（2026-10-20）：前端「成交量」窗口改画成交额。没有该列就不下发（前端自动回落成交量）。
    if "amount" in d.columns:
        out["amount"] = [round(float(v), 0) if not pd.isna(v) else None for v in d["amount"]]
    return out


def _run_bounded(fn, timeout):
    """在**守护线程**里跑 fn，最多等 timeout 秒 → (ok, value, exc)。

    ok=False 表示超时（线程留后台自生自灭，不回收）；**调用方永远不会被 DNS/网络无限期挂住**。
    这是 GUI 卡死专项的一部分：requests 的 timeout 管不到 `getaddrinfo`。"""
    box = {}
    def _go():
        try:
            box["v"] = fn()
        except BaseException as e:      # noqa: BLE001 —— 原样回传给调用方
            box["e"] = e
    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None, None
    return True, box.get("v"), box.get("e")


def _tushare_min_cooldown(why):
    with _TUSHARE_MIN_LOCK:
        _TUSHARE_MIN_STATE["until"] = _time_mod.time() + _TUSHARE_MIN_COOLDOWN_S


def _fetch_min_bars_online(ts_code, freq, days):
    """tushare 原生分钟线。异常/超时/冷却中 → 空 DataFrame（由调用方回退）。

    2026-10-18：**硬超时 8s + 失败冷却 5 分钟**。此前它没有上限：tushare 默认 30s×3 次重试，
    而 DNS 解析不受 timeout 约束 —— GUI 卡死栈里 3~4 个线程同时挂在 `getaddrinfo`，
    `refresh_ob_m30` 的 3 个 worker 全被占死。现在最多占用调用方 8 秒。"""
    import pandas as pd
    with _TUSHARE_MIN_LOCK:
        if _time_mod.time() < _TUSHARE_MIN_STATE["until"]:
            return pd.DataFrame()

    def _call():
        from analysis.index_regime_intraday import _iri_tushare_pro
        pro = _iri_tushare_pro()
        start = (datetime.now() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
        return pro.stk_mins(ts_code=ts_code, freq=freq,
                            start_date=f"{start} 09:00:00", end_date=f"{end} 19:00:00")

    _t0 = _time_mod.time()
    ok, df, err = _run_bounded(_call, 8.0)
    if not ok:
        _tushare_min_cooldown("timeout")
        return pd.DataFrame()
    if err is not None:
        _tushare_min_cooldown(type(err).__name__)
        return pd.DataFrame()
    if (_time_mod.time() - _t0) > 6.0:
        _tushare_min_cooldown("slow")      # 拿到了但明显偏慢 ⇒ 下一轮别再等它
    return _norm_min_bars(df)


def _fetch_min_bars_disk(ts_code, freq):
    """本地缓存兜底。2026-10-04：优先读**新鲜**的 plain 档（盘后预下载写这里），
    再回退历史档；多档间按末行时间取**最新者**（原来固定优先陈旧的 `_d540`，
    当 plain 档更新时会被陈旧数据掩盖）。"""
    import pandas as pd
    try:
        from core import chart_cache as _cc
        fresh = _cc.load_minute_history(ts_code, freq)
        if fresh is not None and not fresh.empty:
            return fresh
    except Exception:
        pass
    best, best_last = pd.DataFrame(), ""
    for name in (f"{ts_code}_{freq}_d540.json", f"{ts_code}_{freq}.json"):
        fp = _MIN_BARS_DIR / name
        if not fp.exists():
            continue
        try:
            rows = (json.loads(fp.read_text(encoding="utf-8")) or {}).get("rows") or []
            df = _norm_min_bars(pd.DataFrame(rows)) if rows else pd.DataFrame()
        except Exception:
            continue
        if df.empty:
            continue
        _last = str(df["time"].iloc[-1])
        if _last >= best_last:
            best, best_last = df, _last
    return best


def _fetch_min_bars_tencent(code, freq):
    """腾讯分钟 K 线（**盘中唯一实时源**，含未收盘根）→ 规范帧。

    2026-10-17：tushare `stk_mins` 盘中只回到上一交易日（见 `position_structure_feed`
    的实测注释），分钟 K 线在盘中是**死的**；腾讯 mkline 盘中随行情更新。
    10min 由 m5 两两配对合成（与仓位结构层同一个 `resample_5min_to_10min`）。
    失败/空 → 空 DataFrame（调用方回退 tushare / 磁盘）。

    取多少根（2026-10-20 实时尾提速）：腾讯接口实测 **每次请求 ~95ms 固定开销 + 每根 ~0.12ms**
    （n=640→165ms、n=340→125ms、n=20→96ms）。实时尾只要够 `MIN_BARS_KEEP` 根，
    多取的那几百根纯属白等 ⇒ 非 10min 档只取 `MIN_BARS_KEEP+20`。
    ⚠️ 10min 必须保持 640：它是 m5 两两合成的，取一半只剩 170 根（前端会判"序列变短"跳过替换）。
    """
    import pandas as pd
    _m = {"5min": "m5", "10min": "m5", "30min": "m30", "60min": "m60"}.get(freq)
    if not _m:
        return pd.DataFrame()
    _n = 640 if freq == "10min" else (MIN_BARS_KEEP + 20)
    try:
        from core.market_data.tencent_provider import minute_kline
        df = minute_kline(code, _m, _n)
    except Exception:
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    if freq == "10min":
        try:
            from core.position_structure import resample_5min_to_10min
            _d10 = resample_5min_to_10min(df.rename(columns={"time": "dt"}))
            _d10 = _d10.rename(columns={"dt": "time"})
            # ⚠️ `resample_5min_to_10min` 的输出里还带一列 `date`（分组键）；若原样喂给
            # `_norm_min_bars`，它会把 `date` 也 rename 成 `time` ⇒ **两个 time 列** ⇒
            # `pd.to_datetime` 抛 "cannot assemble with duplicate keys" ⇒ 10min 永远空。
            # （2026-10-17 打开 10 分 Tab 时才实测到；此前该路径从未被 UI 调用过。）
            _d10 = _d10[[c for c in ("time", "open", "high", "low", "close", "volume")
                         if c in _d10.columns]]
            df = _d10
        except Exception:
            return pd.DataFrame()
    return _norm_min_bars(df)


# ── 图表展示档：深底座 + 实时尾合并（2026-10-30）──────────────────────────────
_MIN_DEEP_BASE_CACHE: dict = {}   # (ts_code, freq) -> (YYYY-MM-DD, df)；当日内存缓存（零网络底座）
_MIN_DEEP_TRIED: dict = {}        # (ts_code, freq, YYYY-MM-DD) -> True；tushare 补深当日至多一次
_MIN_DEEP_LOCK = threading.Lock()


def _merge_min_frames(base, tail, keep=0):
    """历史底座 + 实时尾按 time 去重合并（**尾覆盖历史**），keep>0 时截尾。空 → 空 DataFrame。"""
    import pandas as pd
    frames = [d for d in (base, tail) if d is not None and not getattr(d, "empty", True)]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
    if keep and len(df) > keep:
        df = df.tail(keep).reset_index(drop=True)
    return df


def _resample_10min(df5):
    """5min 规范帧 → 10min（与仓位结构层**同一个** `resample_5min_to_10min`）。

    ⚠️ 合成结果带 `date` 分组列，必须丢掉再 rename —— 否则出现两个 `time` 列，
    `pd.to_datetime` 抛 "cannot assemble with duplicate keys"（2026-10-17 实测坑）。"""
    import pandas as pd
    if df5 is None or getattr(df5, "empty", True):
        return pd.DataFrame()
    try:
        from core.position_structure import resample_5min_to_10min
        d10 = resample_5min_to_10min(df5.rename(columns={"time": "dt"}))
        d10 = d10.rename(columns={"dt": "time"})
        d10 = d10[[c for c in ("time", "open", "high", "low", "close", "volume")
                   if c in d10.columns]]
        return _norm_min_bars(d10)
    except Exception:
        return pd.DataFrame()


def _min_bars_deep_base(code, freq="5min", allow_topup=True):
    """图表展示档的**历史底座**（仅 5min；10min 底座由它合成）：磁盘 plain+`_d540` 合并
    （`chart_cache.load_minute_asof`，零网络、单档解析有记忆化），不足目标深度 85% 时
    **一次性** tushare 补 `_MIN_CHART_DEEP_DAYS` 自然日并落盘（之后零网络）。

    「当日已试过」闸：新股/长期停牌票 tushare 也拿不出更多，不能每次开图都白等 8s 硬超时。
    ⚠️ 返回的可能是**缓存本体**，调用方不得就地修改（本模块消费方都经 `_merge_min_frames`
    concat 出新帧，安全）。空/缺 → 空 DataFrame。"""
    import pandas as pd
    import core.chart_cache as _cc
    ts_code = _min_bars_ts_code(code)
    if not ts_code:
        return pd.DataFrame()
    today = datetime.now().strftime("%Y-%m-%d")
    ck = (ts_code, freq)
    with _MIN_DEEP_LOCK:
        hit = _MIN_DEEP_BASE_CACHE.get(ck)
        if hit and hit[0] == today:
            return hit[1]
    try:
        base = _cc.load_minute_asof(ts_code, freq)       # 纯磁盘合并（plain 覆盖 _d540）
    except Exception:
        base = pd.DataFrame()
    target = MIN_BARS_CHART.get(freq, MIN_BARS_KEEP)
    if allow_topup and (base is None or base.empty or len(base) < int(target * 0.85)):
        tk = (ts_code, freq, today)
        with _MIN_DEEP_LOCK:
            tried = tk in _MIN_DEEP_TRIED
            if not tried:
                _MIN_DEEP_TRIED[tk] = True
                if len(_MIN_DEEP_TRIED) > 5000:          # 防无界（全池轮流开图）
                    _MIN_DEEP_TRIED.clear()
        if not tried and freq in _MIN_CHART_DEEP_DAYS:
            # 一次性补深：tushare stk_mins 95 自然日 ≈ 65 交易日（沿用 8s 硬超时 + 熔断）。
            net = _fetch_min_bars_online(ts_code, freq, _MIN_CHART_DEEP_DAYS[freq])
            if net is not None and not net.empty:
                base = _merge_min_frames(base, net)      # 不截尾：完整落盘
                try:
                    # cap 按 chart_cache._MIN_SAVE_CAP 分档（5min 3600），深度留得住
                    _cc.save_minute_history(ts_code, freq, base)
                except Exception:
                    pass
    if base is None:
        base = pd.DataFrame()
    # ⚠️ **单位归一（2026-10-21）**：磁盘/tushare 的分钟成交量是**股**，而腾讯尾与本仓日线
    #    都是**手** —— 差 100 倍（实测 600000 14:55 `base=3,395,200` vs `tail=33,952`；
    #    688008 同样 100 倍）。不归一就直接 merge ⇒ 历史那几百根的量能/成交额大 100 倍，
    #    y 轴被拉爆、当天那几根反而贴地（owner 报「多窗口成交额不对」就是这么来的）。
    #    只在**返回前**复制一份换算：磁盘档（`save_minute_history` 存的是股）与其它消费端
    #    （仓位结构按比值用成交量，量纲无关）一律不动。
    if not base.empty and "volume" in base.columns:
        base = base.copy()
        base["volume"] = base["volume"].astype(float) / 100.0
    # 缓存/返回前截尾到「目标深度 + 400 余量」：`_d540` 长历史档合并后可达 ~1.8 万行
    # （实测 600584 = 18424 行 / 0.84MB），而展示档最多只要 3200 根 —— 余量只为
    # 实时尾合并后仍能截满目标深度。⚠️ 截尾必须放在**落盘之后**（上面存的是完整历史）。
    _base_keep = int(target) + 400
    if len(base) > _base_keep:
        base = base.tail(_base_keep).reset_index(drop=True)
    with _MIN_DEEP_LOCK:
        # 防无界：截尾后单帧 ≤3600 行 ≈ 0.17MB，120 条上限 ≈ 20MB 封顶（4GB 机器须克制）；
        # 实际同时打开的票屈指可数，这里只是兜底。清整表最简单可靠。
        if len(_MIN_DEEP_BASE_CACHE) > 120:
            _MIN_DEEP_BASE_CACHE.clear()
        _MIN_DEEP_BASE_CACHE[ck] = (today, base)
    return base


def _fetch_min_bars(code, freq="30min", days=None):
    """分钟线（5/10/30/60）。30/60min 最近 MIN_BARS_KEEP 根；5/10min 走**图表展示档**
    （`MIN_BARS_CHART`：3200/1600 根 ≈ 近 3 个月，2026-10-30）。

    取数源（2026-10-18 起）：**腾讯 mkline 永远第一**（实时、含未收盘根、keep-alive <100ms、
    640 根原生够用）。tushare 只作兜底，且带**熔断 + 8s 硬超时**；磁盘最后。
    盘中缓存键用 10 秒粒度 —— 原来的 30 分钟时段键会让分钟 K 线在盘中**冻住半小时**。
    任何失败都返回空 DataFrame，调用方据此降级为「该周期无分时数据」。

    深度合并（仅 5/10min）：腾讯实时尾 + `_min_bars_deep_base` 的磁盘历史底座按 time 去重
    （实时尾覆盖历史）。⚠️ 本函数的 5/10min 调用方已逐处核实**全是图表路径**（弹窗建图 /
    实时尾 / 10min 合成兜底）；OB 体检等计算热路径只用 30min 且走 `_m30_bars_cache_only`
    / `_min_bars_asof`（仍 tail(MIN_BARS_KEEP)=320），不受此处加深影响。"""
    import pandas as pd
    ts_code = _min_bars_ts_code(code)
    if not ts_code or freq not in _MIN_BARS_DAYS:
        return pd.DataFrame()
    days = days or _MIN_BARS_DAYS[freq]
    ck = (ts_code, freq, days)
    _live = _in_trading_session()
    slot = _min_bars_slot_live() if _live else _min_bars_slot()
    with _MIN_BARS_LOCK:
        hit = _MIN_BARS_CACHE.get(ck)
        if hit and hit[0] == slot:
            return hit[1].copy()

    if freq == "10min":
        # 由 5min 合成（tushare 无 10min；合成函数与仓位结构层**同一个**）。
        # 优先腾讯 m5：它给 640 根 ⇒ 320 根 10min 实时尾（≈13 日）；
        # 3 个月深度由下方「深底座合并」从磁盘 5min 深档合成补全（不单独请求 tushare 10min）。
        df = _fetch_min_bars_tencent(code, "10min")
        if df.empty:
            b5 = _fetch_min_bars(code, "5min", _MIN_BARS_DAYS["5min"])
            if b5 is None or b5.empty:
                return pd.DataFrame()
            df = _resample_10min(b5)
            if df.empty:
                return pd.DataFrame()
    else:
        # 2026-10-18：**腾讯永远第一**（含盘后）。它走 keep-alive、中位 <100ms，640 根
        # 原生全历史够 `MIN_BARS_KEEP`；tushare 退为兜底（有熔断+硬超时），磁盘最后。
        # 原先是"盘中腾讯、盘后 tushare"——正是盘后这条把 3 个预热 worker 挂在 DNS 上。
        df = _fetch_min_bars_tencent(code, freq)
        if df.empty:
            df = _fetch_min_bars_online(ts_code, freq, days)
        if df.empty:
            df = _fetch_min_bars_disk(ts_code, freq)
    if df.empty:
        return df
    keep = MIN_BARS_CHART.get(freq, MIN_BARS_KEEP)
    if freq == "5min":
        # 展示档深底：磁盘 plain+_d540（近3月级），不足时一次性 tushare 补 95 日并落盘；
        # 腾讯实时尾（含未收盘根）覆盖历史同刻根。
        df = _merge_min_frames(_min_bars_deep_base(code, "5min"), df, keep)
    elif freq == "10min":
        # 10min 深底 = 深 5min 底座两两合成（tushare 无 10min，严禁单独请求）。
        df = _merge_min_frames(_resample_10min(_min_bars_deep_base(code, "5min")), df, keep)
    else:
        df = df.tail(keep).reset_index(drop=True)
    if df.empty:
        return df
    with _MIN_BARS_LOCK:
        if len(_MIN_BARS_CACHE) > 2000:      # 无界增长防护（2026-10-04）：超限整表清空
            _MIN_BARS_CACHE.clear()
        _MIN_BARS_CACHE[ck] = (slot, df)
    return df.copy()


def _m30_bars_cache_only(code, freq="30min"):
    """**零网络** 30min bars：读内存(同 slot) → 磁盘（`_fetch_min_bars_disk`：新鲜 plain → _d540）。
    供持仓体检（OB）热路径用；绝不出网。返回最近 MIN_BARS_KEEP 根的副本（或空 DataFrame）。"""
    import pandas as pd
    ts_code = _min_bars_ts_code(code)
    if not ts_code or freq not in _MIN_BARS_DAYS:
        return pd.DataFrame()
    ck = (ts_code, freq, "offline")
    slot = _min_bars_slot()
    with _MIN_BARS_LOCK:
        hit = _MIN_BARS_CACHE.get(ck)
        if hit and hit[0] == slot:
            return hit[1].copy()
    df = _fetch_min_bars_disk(ts_code, freq)           # 纯磁盘（plain→_d540），无网络
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.tail(MIN_BARS_KEEP).reset_index(drop=True)  # 必须：_d540 有 ~4300 根，须截断
    with _MIN_BARS_LOCK:
        if len(_MIN_BARS_CACHE) > 2000:
            _MIN_BARS_CACHE.clear()
        _MIN_BARS_CACHE[ck] = (slot, df)
    return df.copy()


def _build_m30_snapshot(df):
    """由 30min bars 组装持仓体检快照：{t30, div, feats, bar_time, n_bars}。空/不足 → {}（纯计算）。"""
    if df is None or getattr(df, "empty", True):
        return {}
    try:
        from analysis.trend30.adapter import evaluate_bars
        from analysis.trend30.indicators import drop_forming_bar
        from analysis import m30_features, divergence as _dv
        dbars = drop_forming_bar(df)                    # 丢掉未收盘根（与 trend30 管线一致）
        if len(dbars) < _M30_MIN_BARS:
            return {}
        t30 = evaluate_bars(dbars)
        evs = _dv.detect_divergence_events(dbars)
        last = _dv._latest_fresh(evs, _dv.MAX_AGE_BARS.get("30min", 32))
        return {
            "t30": t30 if t30.get("state") else {},
            "div": (None if last is None else {
                "type": "顶背离" if last.get("type") == "顶" else "底背离",
                "consec": bool(last.get("consec")),
                "bars_ago": int(last.get("bars_ago", 0)),
                "time": str(last.get("time", "")),
                "price": last.get("price")}),
            # dif_zone=True：T1 顶背离须在 DIF>0 区（对齐 同花顺/通达信；滤「下行深负区小反弹」
            # 这一类已知假阳性，见 002202）。精度调优见 t_io/validation/m30_top/调优_m30_precision.md。
            "feats": m30_features.detect_top_features(dbars, dif_zone=True),
            "bar_time": str(dbars["time"].iloc[-1]),
            "n_bars": len(dbars),
        }
    except Exception:
        return {}


def _build_m30_light(df):
    """建仓表「30min 判定」用的**轻量**快照：{feats, div, bar_time}。**跳过 trend30 状态机**
    （`verdict_from_features` 不用 trend；状态机 ~300ms vs 本函数 ~17ms）⇒ 整池预热才 ~0.9s。
    空/不足 → {}。"""
    if df is None or getattr(df, "empty", True):
        return {}
    try:
        from analysis.trend30.indicators import drop_forming_bar
        from analysis import m30_features, divergence as _dv
        dbars = drop_forming_bar(df)
        if len(dbars) < _M30_MIN_BARS:
            return {}
        evs = _dv.detect_divergence_events(dbars)
        last = _dv._latest_fresh(evs, _dv.MAX_AGE_BARS.get("30min", 32))
        return {
            "div": (None if last is None else {
                "type": "顶背离" if last.get("type") == "顶" else "底背离",
                "bars_ago": int(last.get("bars_ago", 0))}),
            # dif_zone=True：T1 顶背离须在 DIF>0 区（对齐 同花顺/通达信；滤「下行深负区小反弹」
            # 这一类已知假阳性，见 002202）。精度调优见 t_io/validation/m30_top/调优_m30_precision.md。
            "feats": m30_features.detect_top_features(dbars, dif_zone=True),
            "bar_time": str(dbars["time"].iloc[-1]),
        }
    except Exception:
        return {}


_MIN_BARS_ASOF_FILES = ("{ts}_{freq}.json", "{ts}_{freq}_d540.json")


def _min_bars_asof(code, date_str, freq="30min"):
    """历史日 30min bars：**纯磁盘（零网络）**，合并各档（plain 覆盖近月、_d540 覆盖长历史）
    按时序去重后只留 `<= date_str` 的根，供「历史日 30min 判定」。

    与 `_fetch_min_bars_disk` 的差别：后者固定取「末行最新」的一档（可能不覆盖历史日），
    本函数按目标日截取、跨档合并，保证任意历史日都能取到当时为止的 bars。空/不足 → 空 DataFrame。"""
    import pandas as pd
    ts_code = _min_bars_ts_code(code)
    if not ts_code or freq not in _MIN_BARS_DAYS:
        return pd.DataFrame()
    frames = []
    for tpl in _MIN_BARS_ASOF_FILES:
        fp = _MIN_BARS_DIR / tpl.format(ts=ts_code, freq=freq)
        if not fp.exists():
            continue
        try:
            rows = (json.loads(fp.read_text(encoding="utf-8")) or {}).get("rows") or []
        except Exception:
            continue
        if rows:
            _d = _norm_min_bars(pd.DataFrame(rows))
            if not _d.empty:
                frames.append(_d)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.sort_values("time").drop_duplicates(subset=["time"], keep="last").reset_index(drop=True)
    _end = pd.Timestamp(date_str) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    df = df[df["time"] <= _end].reset_index(drop=True)
    if df.empty:
        return df
    return df.tail(MIN_BARS_KEEP).reset_index(drop=True)


_ACCT_MAP_CACHE = {"ts": 0.0, "map": {}}


# ---- 总览页冷启动快照 + 单飞刷新（2026-10-10） ----
# 症状：每次打开 GUI，首帧的指数板/成交额/成交额历史三个接口都因**冷缓存 + GM 单 worker 池
# 串行 4s 超时**而返回占位符（实测 load_indices → {"indices":[],"error":"指数板取数超时"} 2571ms、
# load_market_turnover → {"available":false} 2567ms）⇒ 用户看到**空白卡片**。
# 同时 `_bounded` 超时后 shutdown(wait=False) 把 worker 连同已经算出的结果一起丢弃（弃工），
# 16 路并发首帧扇出实测活线程 49-55（本征仅 19ms 的接口也被拖到 2.4-3.5s）。
# 对策：把上次**成功且有效**的结果落盘（跨进程复用，内存 TTL 缓存做不到），冷启动立即返回快照
# 并标 stale，后台**单飞**刷新；刷新到达后前端下一轮 10s 轮询自然替换。
#
# ⚠️ 安全契约（交易台不得把过期快照渲染成实时值）：
#   1) 后端一旦从磁盘出数，**必须**带 stale=True + snapshot_day + snapshot_ts；
#   2) 前端必须在 stale 时渲染可见标记（灰显 + "快照 HH:MM:SS"），不得静默替换；
#   3) **只落盘有效结果**（见 _snapshot_write 的调用点校验），绝不落盘 _bounded 的占位符。
_OVERVIEW_SNAPSHOT_DIR = BASE / "t_io" / "cache" / "overview_snapshots"
_OVERVIEW_SNAPSHOT_MAX_AGE = 26 * 3600.0     # 绝对上限：防周一早上把上周五收盘当实时
_SNAPSHOT_LOCK = threading.Lock()

# 单飞在飞表：key -> threading.Event。**不用** `_bounded` 那个每调用新建的池——它超时时
# `shutdown(wait=False)`，之后再 submit 会 RuntimeError: cannot schedule new futures after shutdown。
_OVERVIEW_INFLIGHT = {}
_OVERVIEW_INFLIGHT_LOCK = threading.Lock()


def _snapshot_read(key):
    """读磁盘快照 → {"written_at","snapshot_day","age_sec","payload"} 或 None。

    fail-open：文件坏/缺/超龄 = 没缓存，绝不抛。有效期为 _OVERVIEW_SNAPSHOT_MAX_AGE
    （调用方各自再做同日/跨日判定——实时价类仅当日可用）。
    """
    fp = _OVERVIEW_SNAPSHOT_DIR / f"{key}.json"
    with _SNAPSHOT_LOCK:
        rec = _load_json(fp, None)
    if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
        return None
    try:
        age = _time_mod.time() - float(rec.get("written_at") or 0)
    except Exception:
        return None
    if age < 0 or age > _OVERVIEW_SNAPSHOT_MAX_AGE:
        return None
    rec["age_sec"] = age
    return rec


def _snapshot_write(key, payload):
    """写磁盘快照（tmp + 原子替换；沿用 `_em_boards_disk_cached` 的写法）。

    fail-open：写失败忽略。**调用方必须保证 payload 有效**（非占位、且过 _clean）。
    """
    if not isinstance(payload, dict):
        return
    fp = _OVERVIEW_SNAPSHOT_DIR / f"{key}.json"
    rec = {"written_at": _time_mod.time(),
           "snapshot_day": datetime.now().strftime("%Y-%m-%d"),
           "payload": payload}
    try:
        blob = json.dumps(rec, ensure_ascii=False, default=str)
    except Exception:
        return
    try:
        with _SNAPSHOT_LOCK:
            fp.parent.mkdir(parents=True, exist_ok=True)
            tmp = fp.with_suffix(".tmp")
            tmp.write_text(blob, encoding="utf-8")
            tmp.replace(fp)
    except Exception:
        pass


# ── 沪深逐日成交额「冻结」缓存（2026-10-16）────────────────────────────────
# **为什么需要**：历史日成交额是**事实**，算对一次就永远对；而算它的两条腿会被
# `facade.index_daily` 的**降级**污染 —— 腾讯 ifzq 的指数日线**只有 6 列、没有成交额**，
# `tencent_provider` 在缺字段时**用 volume 顶替 amount**（`:445` 与 `_idx_ensure_cols`，
# 那条为 ratio 口径刻意保留，不能动）⇒ 一旦某腿回退腾讯，该腿 amount 只剩 ~1e8~1e9，
# 与另一腿（GM 真值 ~1e11）相加 = **当天腰斩**且完全静默（owner 2026-10-16 截图：
# 图上一半高、环比 +99%、5 日均量 9108 亿 —— 而真值 1.65 万亿）。
# ⇒ 只把「两腿都可信」的值写进本缓存，**降级的计算绝不覆盖已冻结的好值**。
_HS_DAILY_CACHE_FP = BASE / "t_io" / "state" / "turnover_hs_daily.json"
_HS_CACHE_LOCK = threading.Lock()


def _load_hs_daily_cache() -> dict:
    """{日期: 沪深合计成交额(元)}。文件坏/缺 ⇒ {}（fail-open，绝不抛）。"""
    try:
        d = json.loads(_HS_DAILY_CACHE_FP.read_text(encoding="utf-8"))
        rows = d.get("days") if isinstance(d, dict) else None
        return {str(k): float(v) for k, v in (rows or {}).items()}
    except Exception:
        return {}


def _save_hs_daily_cache(days: dict) -> None:
    """原子写 + 截到最近 400 日（够 1 年窗口 + 裕度，别无限长）。失败静默。"""
    try:
        _items = sorted(days.items())[-400:]
        rec = {"updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "note": "沪深两市逐日成交额（元）。**只写入两腿都可信的值**，降级计算不覆盖。",
               "days": dict(_items)}
        with _HS_CACHE_LOCK:
            _HS_DAILY_CACHE_FP.parent.mkdir(parents=True, exist_ok=True)
            tmp = _HS_DAILY_CACHE_FP.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_HS_DAILY_CACHE_FP)
    except Exception:
        pass


def _kick_overview_refresh(key, fn):
    """按 key **单飞**起一次后台刷新；返回 (本次是否由我起, Event)。

    锁内**只做 dict 读写**，绝不跨慢活持锁——否则并发调用者会无界阻塞、失去超时语义。
    失败不毒化单飞：finally 里无论成败都清在飞表，下一轮照常重试（异常绝不当作成功缓存）。
    """
    with _OVERVIEW_INFLIGHT_LOCK:
        ev = _OVERVIEW_INFLIGHT.get(key)
        if ev is not None:
            return False, ev
        ev = threading.Event()
        _OVERVIEW_INFLIGHT[key] = ev

    def _bg():
        try:
            fn()
        except Exception:
            pass
        finally:
            with _OVERVIEW_INFLIGHT_LOCK:
                _OVERVIEW_INFLIGHT.pop(key, None)
            ev.set()

    _th.Thread(target=_bg, name=f"ovw-refresh-{key}", daemon=True).start()
    return True, ev


def _stale_wrap(snap):
    """把磁盘快照包装成可下发的 payload（附 stale 元信息，见安全契约第 1 条）。"""
    out = dict(snap["payload"])
    ts = out.get("ts")
    out.update({"stale": True,
                "stale_age_sec": int(snap["age_sec"]),
                "snapshot_day": snap.get("snapshot_day"),
                "snapshot_ts": ts if isinstance(ts, str) else None,
                "stale_reason": "cold_start"})
    return _clean(out)


def _is_today(snap):
    return snap.get("snapshot_day") == datetime.now().strftime("%Y-%m-%d")


# ---- 东财「所属板块」磁盘缓存层（2026-09-30，突破面板概念列补拉专用） ----


# ---- 东财「所属板块」磁盘缓存层（2026-09-30，突破面板概念列补拉专用） ----
#: 结构 {code: {"ts": epoch, "boards": [{name,kind}]}}；概念不常变，正缓存 7 天，
#: 负缓存（拉空=风控或真无板块）1 天，防风控期反复 8 次重试拖慢扫描。
_EM_BOARDS_DISK_FP = BASE / "t_io" / "cache" / "em_boards.json"
_EM_BOARDS_DISK_TTL = 7 * 86400.0
_EM_BOARDS_DISK_NEG_TTL = 1 * 86400.0
_EM_BOARDS_DISK_LOCK = threading.Lock()
_EM_BOARDS_DISK_MEM = {"data": None}     # 整文件内存镜像，避免逐只重复读盘


def _em_boards_disk_cached(api, code) -> list:
    """东财「所属板块」三级取数：Api 内存缓存(600s) → 磁盘(7天/负1天) → 在线。

    读写全部 **fail-open**（文件坏=没缓存；写失败=忽略），绝不抛。
    `Api._profile_cache` 的 600s TTL 面板重开即失效，磁盘层负责跨进程复用。
    """
    c = str(code).split("_")[0]
    now = _time_mod.time()
    hit = Api._profile_cache.get(c)
    if hit and (now - hit[0]) < Api._PROFILE_TTL:
        return hit[1]
    with _EM_BOARDS_DISK_LOCK:
        if _EM_BOARDS_DISK_MEM["data"] is None:
            raw = _load_json(_EM_BOARDS_DISK_FP, None)
            _EM_BOARDS_DISK_MEM["data"] = raw if isinstance(raw, dict) else {}
        disk = _EM_BOARDS_DISK_MEM["data"]
        rec = disk.get(c)
        if isinstance(rec, dict) and isinstance(rec.get("boards"), list):
            boards = rec["boards"]
            ttl = _EM_BOARDS_DISK_TTL if boards else _EM_BOARDS_DISK_NEG_TTL
            try:
                fresh = (now - float(rec.get("ts") or 0)) < ttl
            except Exception:
                fresh = False
            if fresh:
                Api._profile_cache[c] = (now, boards)
                return boards
    boards = api._em_stock_boards(c)       # 在线补拉（自身 8 次重试、永不抛）
    with _EM_BOARDS_DISK_LOCK:
        disk[c] = {"ts": now, "boards": boards}
        try:
            _EM_BOARDS_DISK_FP.parent.mkdir(parents=True, exist_ok=True)
            tmp = _EM_BOARDS_DISK_FP.with_suffix(".tmp")
            tmp.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_EM_BOARDS_DISK_FP)
        except Exception:
            pass
    return boards


def _hunter_is_intraday(date) -> bool:
    """该日期是否为「今日且盘中」（工作日 9:15-15:00）。

    盘中判定集中在此，供「盘中手动跑只算不推飞书」等处复用（2026-09-29）。
    """
    now = datetime.now()
    return (str(date) == now.strftime("%Y-%m-%d")
            and 915 <= now.hour * 100 + now.minute <= 1500
            and now.weekday() < 5)


def _account_of(code) -> str:
    """code → 账户名。**优先**取自「历史成交」台账的 (资金账号, 代码) 持仓，取不到才回退
    accounts_config.json 的 holdings 清单声明（TTL 300s）。

    2026-10-10 owner 定：全站账户信息统一以历史成交为源。历史成交是券商真实成交底账，
    账户名就是券商的资金账号；config 里的 holdings 是人工维护的名单，只在成交流没覆盖到
    （比如另一个账户压根没导）时兜底。
    """
    import time as _t
    base = str(code).split("_")[0]
    if _t.time() - _ACCT_MAP_CACHE["ts"] > 300:
        m = {}
        led = _load_json(TRADE_HISTORY_LEDGER, None)
        if isinstance(led, dict):
            for s in (led.get("stocks") or []):
                acc = s.get("account") or ""
                c = str(s.get("code") or "").split("_")[0]
                if acc and c:
                    m[c] = acc
        cfg = _load_json(STATE_DIR / "accounts_config.json", {}) or {}
        for a, v in (cfg.get("accounts") or {}).items():
            for c in ((v or {}).get("holdings") or []):
                m.setdefault(str(c).split("_")[0], a)   # 历史成交优先，config 只补空缺
        _ACCT_MAP_CACHE.update({"ts": _t.time(), "map": m})
    return _ACCT_MAP_CACHE["map"].get(base, "")


def _trade_history_accounts():
    """历史成交台账里的账户清单（全站账户列表的唯一来源）。未导入 → []。"""
    led = _load_json(TRADE_HISTORY_LEDGER, None)
    if not isinstance(led, dict):
        return []
    return [a for a in (led.get("accounts") or []) if a.get("account")]


def _reconcile_cash():
    """现金口径（净值法 §八）：accounts_config 最新人工 reconcile 的各启用账户
    available（可用资金）之和。取不到返回 (None, "missing")——净值法下 cash 缺失时
    equity/alpha 必须置 null 并标注，禁止硬算。
    2026-09-27：跳过 paper=true 的仿真账户（账户C 国盛掘金仿真），
    防止纸面现金混入真实净值口径（holdings_daily 市值侧也只有实盘持仓）。"""
    fp = PORTFOLIO if PORTFOLIO.exists() else PORTFOLIO_LEGACY
    cfg = _load_json(fp, {}) or {}
    accs = cfg.get("accounts") or {}
    total, found = 0.0, False
    for a in accs.values():
        if not isinstance(a, dict) or not a.get("enabled", True):
            continue
        if a.get("paper"):
            continue
        v = a.get("available")
        if isinstance(v, (int, float)):
            total += float(v)
            found = True
    if not found:
        return None, "missing"
    return round(total, 2), "accounts_config"


# 技术标签 TTL 缓存：GUI 每 10s 轮询 refresh_pb → load_stock_tags_batch（单次约 7-12s，
# 期间大量 pandas + 网络在 pywebview 主线程执行会冻结界面）。改为 TTL 缓存 + 后台异步重算，
# 轮询永远读缓存即时返回，界面不卡。TTL 取 120s：标签变化慢，过长 TTL 减少后台重算的 CPU 尖峰。
_TAGS_TTL = 120.0

# 30 分钟趋势判定（2026-10-04，方案 doc/solutions/2026-10-04_30分钟趋势判定方案.md）：
# 技术标签的「上行/下行/震荡」由日线斜率改按 30min 三层状态机。开关便于回滚；
# 数据不可用（网络/根数不足）时自动回退日线斜率，保持旧行为。
_TREND30_ENABLED = True
_TAGS_CACHE: dict = {}
_TAGS_LOCK = threading.Lock()
_TAGS_RUNNING = False


def _tags_ttl_now() -> float:
    """标签缓存 TTL —— **盘中 120s、收盘后 6 小时**（2026-10-21）。

    实证：15:28 打开 K 线报「load_stock_chart 超时 20000ms」，`gui_freeze.log` 的现场栈里
    6 个线程全在 `_stock_tags_one → get_trend30 → _ir_adx/_ir_atr_wilder`（pandas ewm）
    与 `detect_daily_divergence`（numpy median）—— **盘后每 2 分钟一次的全量标签重算**
    把 GIL 吃满，图表请求排在后面等到超时。

    盘后日线已定稿，标签（30min 状态机 / 背离 / 箱体）**不会再变** ⇒ 没有理由还按 120s 重算。
    """
    try:
        now = datetime.now()
        if now.weekday() >= 5:
            return 6 * 3600.0
        hm = now.strftime("%H:%M")
        if hm >= "15:00" or hm < "09:15":
            return 6 * 3600.0
    except Exception:
        pass
    return _TAGS_TTL
# 单只标签的**跨批次**缓存（2026-10-18）：批量缓存按「代码集合指纹」存，猎手(offline=True)/
# 风向标(288 只代表股)/建仓表 的集合各不相同 ⇒ 同一只票会被重复算（每只 ~300ms 的 pandas：
# 精密箱体 + 30min 状态机）。按 (code, offline) 单独缓存，TTL 与批量一致，三处互相复用。
_TAGS_ONE_CACHE: dict = {}
_TAGS_ONE_TTL = 120.0
_TAGS_ONE_MAX = 6000

# 「得分相比上一交易日 ↑/↓」用的上一日得分表（2026-10-21）。
# key = 日期字符串，value = (trace 文件 mtime, {code: score})。见 `Api._prev_day_score_map`。
_PREV_SCORE_CACHE: dict = {}

# 突破扫描状态/缓存的同步锁（2026-09-29）。此前 `_breakout_scan`/`_breakout_cache` **零同步**：
# 后台 daemon 线程写、pywebview 线程每 800ms 读 ⇒ 可读到 "status=done 但 stocks 还是上一批"
# 的中间态；force 重扫还能与残留线程重叠写同一 state/磁盘。
_BREAKOUT_LOCK = threading.Lock()


def _tier_sector_stocks(stocks, min_members=5):
    """**就地**给一个板块的个股打 龙头/中军/杂毛 —— **按阈值自适应，无固定数量、也不硬凑**。

    设计意图（owner）：**交易替代梯队** —— 龙头涨停买不到时，顺着梯队往下选。
    故三档都用**阈值**判定，数量由板块自身决定：

      · **龙头** = 涨幅 ≥ `max(9.5%, 板块最高涨幅 − LEAD_GAP)`（默认 gap=2pp）＝「最强一档」。
        · 可 **0 个**（板块当日没行情 ⇒ 无龙头，不硬凑）；可 **多个**（并列/接近 ⇒ 多龙头）。
        · 不用"涨停"单一判据：科创板/创业板涨停是 20%，实测半导体当日最强 +14.1%（未涨停）
          ⇒ 按涨停判会得出"最大板块没有龙头"这种废结论。
      · **中军** = 非龙头中 **成交额 ≥ 板块中位数 × MID_MULT**（默认 5 倍）＝资金主战场/核心大票。
        同样可 0 个；数量随板块大小自适应（实测半导体 14 / 锂电池 4 / 电力 5 / AI应用 12）。
      · **杂毛** = 其余。
      · 板块当日成分 < `min_members` 一律不标（1 只的"板块"谈不上梯队）。

    实测输出（2026-10-10 收盘，韭研分类口径）：
      半导体150只 最高+14.1% → 龙头1(和林微纳) 中军14(寒武纪/兆易创新/中微公司…)
      锂电池 88只 最高+15.9% → 龙头1(科恒股份)  中军4(天赐材料/赣锋锂业/龙蟠/恩捷)
      电力   44只 最高+10.0% → **龙头2**(振华股份+雄韬股份，并列涨停) 中军5
      机器人 76只 最高 +1.1% → **龙头0**(不硬凑)  中军10

    ⚠️ **实测无前瞻区分度，别当依据用**（`t_io/validation/sector_tier/exp1|exp2`，
    977 池 × 2009 日，行业口径、同板块同日配对、按日聚类 t、分半窗）：
      四种定义（涨幅头名 / 涨幅前3 / 成交额头名 / **连板龙头**）× 4 个持有期 ≈ 全部无差异：
      涨幅头名 vs 杂毛 H=5 仅 −0.021pp(t=−0.40)；成交额头名反而 **−0.302pp(t=−5.7)**；
      连板龙头限定"板块内有连板"时 H=1 **−0.112pp(t=−1.71)**；**分半窗符号翻转**
      （龙头 h1 +0.282 / h2 −0.217）。效应量 ±0.05~0.13pp 远低于往返成本 0.13~0.35pp。
    ⇒ 它的价值在**能不能买到 / 是不是这个板块的钱**（可执行性），**不在预测收益**。

    2026-10-20 静态优先（机器人/军工板块整治）：watchlist 里带 `jiuyan_role`
    （研究核实的真龙头/中军，见 `_jiuyan_roles`）的票**直接用静态标签、跳过动态算法**，
    也不参与阈值统计；`tier_static=True` + `tier_reason` 供前端区分徽章。
    无静态标签的票照旧走下方动态分层（其它板块的唯一来源）。
    """
    roles = _jiuyan_roles()
    for s in stocks:
        st = roles.get(str(s.get("code", "")))
        if st:
            s["tier"] = st["role"]
            s["tier_static"] = True
            if st.get("reason"):
                s["tier_reason"] = st["reason"]
        else:
            s["tier"] = ""
    # 动态分层只覆盖**无静态标签**的票（静态票不进阈值统计、不被改写）
    ok = [s for s in stocks
          if not s.get("tier_static") and s.get("amount") and s.get("change_pct") is not None]
    if len(stocks) < min_members or len(ok) < min_members:
        return
    chgs = [float(s.get("change_pct") or 0) for s in ok]
    amts = sorted(float(s.get("amount") or 0) for s in ok)
    med = amts[len(amts) // 2]
    lead_thr = max(9.5, max(chgs) - LEAD_GAP)
    leads = [s for s in ok if float(s.get("change_pct") or 0) >= lead_thr]
    for s in leads:
        s["tier"] = "龙头"
    mid_thr = med * MID_MULT
    for s in ok:
        if not s["tier"] and float(s.get("amount") or 0) >= mid_thr:
            s["tier"] = "中军"
    # 整个板块**一只都没分层**（无静态、无动态龙头且无中军）⇒ 全不标。
    # 否则会把这批票一律打成"杂毛"——它们只是"板块成员"，不是杂毛（不硬凑的反面同样适用）。
    if not any(s["tier"] for s in stocks):
        return
    for s in ok:
        if not s["tier"]:
            s["tier"] = "杂毛"



LEAD_GAP = 2.0      # 龙头带宽（pp）：涨幅在"板块最高 − 2pp"以内的都算最强一档
MID_MULT = 5.0      # 中军门槛：成交额 ≥ 板块中位数 × 此倍数（资金主战场）


def _market_latest_date():
    """市场最新交易日：优先读指数日线缓存末行（1 个文件、零网络）。"""
    for name in ("index_sh000001.json", "index_sz399001.json"):
        try:
            rows = (_load_json(_DAILY_KLINE_DIR / name, {}) or {}).get("rows") or []
            if rows:
                return str(rows[-1].get("date"))[:10]
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d")


def _compute_ma365_breadth(days=_MA365_MAX_DAYS):
    """全部个股日线缓存 → 每日「收盘价 > MA365（年限）」的股票占比。

    口径（2026-10-18 owner「每日站上年限的股票占比」）：
      · **样本** = 日线缓存 ≥365 根、且**末行 = 最新交易日**的个股 —— 保证是当前仍有行情的票；
        停牌/退市/陈旧缓存不进分母，避免把"没数据"算成"跌破年限"。
      · 每日分母 = 该日有 bar 的样本股；分子 = 当日收盘 > 365 日均线。
      · 只算最近 `days` 个交易日；MA365 用滚动和（O(n)），不建 DataFrame。
      · 全程后台；**单线程 JSON 解析 ~10s**（5500 个文件），2 线程压一下 I/O。

    返回 {computed_at, latest_date, series:[…365…], series20:[…20…], coverage:{files,cohort}}。

    `series20`（2026-10-20 owner：「站上20线公司占比」）= 同一批样本、同一遍解析里顺手算的
    「收盘价 > MA20」占比，窗口 `_MA20_MAX_DAYS`（600 个交易日，够覆盖「自2024-09-01」档）。
    """
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor
    latest = _market_latest_date()
    files = [p for p in _DAILY_KLINE_DIR.glob("*.json")
             if p.stem.isdigit() and len(p.stem) == 6]
    total = len(files)
    with _MA365_LOCK:
        _MA365_STATE.update({"done": 0, "total": total, "started": _time_mod.time()})
    counts = defaultdict(lambda: [0, 0])       # date -> [above, total]
    counts20 = defaultdict(lambda: [0, 0])     # 同上，但口径是 20 日均线
    cohort = 0

    def _one(p):
        try:
            rows = (_load_json(p, {}) or {}).get("rows") or []
            if len(rows) < 365:
                return None
            if str(rows[-1].get("date"))[:10] != latest:
                return None
            closes = [float(r.get("close") or 0) for r in rows]
            dates = [str(r.get("date"))[:10] for r in rows]
        except Exception:
            return None
        n = len(closes)
        start = max(364, n - days)
        s = sum(closes[start - 364:start + 1])
        out = []
        for i in range(start, n):
            ma = s / 365.0
            if ma > 0 and closes[i] > 0:
                out.append((dates[i], closes[i] > ma))
            if i + 1 < n:
                s += closes[i + 1] - closes[i - 364]
        # 站上 20 日线占比（2026-10-20）：同一次解析顺手算，样本/停牌口径与 MA365 完全一致，
        # 只是回看窗口短（20 根）⇒ 能覆盖到本地缓存最早的那根，足以撑起「自2024-09-01」档。
        start20 = max(19, n - _MA20_MAX_DAYS)
        s20 = sum(closes[start20 - 19:start20 + 1])
        out20 = []
        for i in range(start20, n):
            ma20 = s20 / 20.0
            if ma20 > 0 and closes[i] > 0:
                out20.append((dates[i], closes[i] > ma20))
            if i + 1 < n:
                s20 += closes[i + 1] - closes[i - 19]
        return out, out20

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=_MA365_WORKERS) as ex:
            for res in ex.map(_one, files):
                done += 1
                if res:
                    cohort += 1
                    for d, above in res[0]:
                        c = counts[d]
                        c[1] += 1
                        if above:
                            c[0] += 1
                    for d, above in res[1]:
                        c20 = counts20[d]
                        c20[1] += 1
                        if above:
                            c20[0] += 1
                if done % 100 == 0:
                    with _MA365_LOCK:
                        _MA365_STATE["done"] = done
    except Exception:
        pass

    def _to_series(cnt, keep):
        out = []
        for d in sorted(cnt):
            above, tot = cnt[d]
            if tot:
                out.append({"date": d, "above": above, "total": tot,
                            "pct": round(above / tot * 100, 2)})
        return out[-keep:]

    series = _to_series(counts, days)
    series20 = _to_series(counts20, _MA20_MAX_DAYS)
    rec = {"computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "latest_date": latest, "series": series, "series20": series20,
           "coverage": {"files": total, "cohort": cohort}}
    try:
        _MA365_CACHE_FP.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MA365_CACHE_FP.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_MA365_CACHE_FP)
    except Exception:
        pass
    with _MA365_LOCK:
        _MA365_STATE["done"] = total
    return rec


# ---- 板块「站上5日线 / 20日线」成分股占比（2026-10-20 owner：星空图下方多坐标轴）----
# 数据源 = 韭研主板块映射（`_jiuyan_board()`，与 daily_summary 的板块名同源）+ 个股日线缓存。
# 只扫**有主板块**的代码（实测 1690 只 / 5382），单线程解析约 6~8s ⇒ 与年限宽度同一套
# 后台 + 内存/磁盘 SWR，磁盘缓存按数据日复用。
_SECTOR_MA_CACHE_FP = BASE / "t_io" / "cache" / "sector_ma_breadth.json"
_SECTOR_MA_MEM: dict = {"ts": 0.0, "val": None}
_SECTOR_MA_MEM_TTL = 300.0
_SECTOR_MA_STATE: dict = {"running": False, "done": 0, "total": 0}
_SECTOR_MA_LOCK = threading.Lock()
_SECTOR_MA_MAX_DAYS = 40       # 一次算 40 个交易日（前端按 end_date 收边后取最近 N 天）
# 历史 end 的单独缓存（2026-10-23 owner：「选了 4月8日，面板没跟着变」）。
# 磁盘档只存**最新**那档；历史日按需后台算、只进内存（cap 12 档，LRU 淘汰）。
_SECTOR_MA_BY_END: dict = {}
_SECTOR_MA_END_RUNNING: set = set()
_SECTOR_MA_END_MAX = 12
# 缓存口径版本：**改样本口径/板块来源时 +1**（否则旧磁盘档会被当新鲜直接回放）。
# 2（2026-10-21）：样本容忍 5 个自然日滞后 + 缓存键加板块指纹 board_fp。
_SECTOR_MA_SCHEMA = 4   # v4: 板块来源剔除「北交所/央企」机构标签（2026-10-25）


def _watchlist_fp():
    """韭研 watchlist 的 mtime 指纹（打标/改板块后必变）。读不到返回 ""。"""
    try:
        return str((BASE / "stock_hunter" / "watchlist_jiuyan.json").stat().st_mtime)
    except Exception:
        return ""


def _compute_sector_ma_breadth(days=_SECTOR_MA_MAX_DAYS, end=None):
    """每个板块每日「收盘站上 5 日线 / 20 日线」的成分股占比。

    口径：
      · **板块** = `_jiuyan_board()` 的韭研主板块（每只股票只归一个板块，不重复计数）；
        与 `daily_summary.json`（星空图数据源）的板块名同源。
      · **样本** = 该板块内**日线缓存末行 = 最新交易日**的个股 —— 停牌/陈旧缓存不进分母，
        否则会把"没数据"算成"跌破均线"。
      · **站上** = 当日收盘 > N 日简单均线（含当日），与得分里 −70 悬崖 / 20日线观察列同口径。

    返回 {computed_at, latest_date, sectors:[{name, points:[{date,pct5,pct20,n}]}],
          coverage:{codes, scanned}}。
    """
    from collections import defaultdict
    # `end` 给定时窗口锚在**那一天**（历史日期回看），否则锚在最新交易日。
    latest = str(end).strip()[:10] if end else _market_latest_date()
    board = _jiuyan_board()
    mine = {c: s for c, s in board.items() if s}
    files = [p for p in _DAILY_KLINE_DIR.glob("*.json")
             if p.stem.isdigit() and len(p.stem) == 6 and p.stem in mine]
    total = len(files)
    with _SECTOR_MA_LOCK:
        _SECTOR_MA_STATE.update({"done": 0, "total": total, "started": _time_mod.time()})
    # sec -> date -> [a5, a20, n, aconv, n60]（n60 = 当日**有 60 根以上历史**的只数，粘合的分母）
    counts = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0]))
    scanned = 0

    def _one(p):
        sec = mine.get(p.stem)
        try:
            rows = (_load_json(p, {}) or {}).get("rows") or []
            # 历史 end：只取 ≤ 该日的 bar（否则算的还是最新那段）
            if end:
                rows = [r for r in rows if str(r.get("date"))[:10] <= latest]
            if len(rows) < 20:
                return None
            _last = str(rows[-1].get("date"))[:10]
            # 2026-10-21：**容忍 5 个自然日的滞后**（原来要求"末行=最新交易日"，一刀切）。
            # 起因：owner 新加的大消费/黄金/数据要素 194 只此前不在猎手池 ⇒ 从没被扫过 ⇒
            # 缓存停在 T-1，被这条闸整批剔除 ⇒ 新能源板块样本塌成 n=6 / n=1 / n=0，占比看着像坏了。
            # 停牌/长期不更新的仍排除（超过 5 天不进分母，否则会把"没数据"算成"跌破均线"）。
            if _last != latest:
                try:
                    _lag = (datetime.strptime(latest, "%Y-%m-%d")
                            - datetime.strptime(_last, "%Y-%m-%d")).days
                except Exception:
                    _lag = 99
                if _lag > 5:
                    return None
                _carry = True
            else:
                _carry = False
            closes = [float(r.get("close") or 0) for r in rows]
            dates = [str(r.get("date"))[:10] for r in rows]
        except Exception:
            return None
        n = len(closes)
        start = max(19, n - days)
        s5 = sum(closes[start - 4:start + 1])
        s20 = sum(closes[start - 19:start + 1])
        # 2026-10-23 owner：「这个面板再加一个**均线粘合占比**」。
        # 口径与 `core.ma_reclaim.convergence_state` 完全一致：MA5/10/20/30/60 的相对离散度
        # (max-min)/close ≤ 3%。⚠️ 需要 ≥60 根历史 ⇒ 分母单独算（`n60`），
        # 上市不足 60 天的票不算进粘合的分母（否则会把"没数据"算成"不粘合"）。
        from core.ma_reclaim import (CONV_THR as _CONV_THR,
                                     CONV_THR_SHORT as _CONV_THR_S,
                                     RECLAIM_PERIODS as _MAS)
        rolls = {p: sum(closes[start - p + 1:start + 1]) for p in _MAS if start - p + 1 >= 0}
        _can_conv = len(rolls) == len(_MAS)
        out = []
        for i in range(start, n):
            if closes[i] > 0:
                conv = None
                if _can_conv:
                    _m = [rolls[p] / p for p in _MAS]
                    _sp5 = (max(_m) - min(_m)) / closes[i] * 100.0
                    _ms = _m[:3]
                    _sps = (max(_ms) - min(_ms)) / closes[i] * 100.0
                    # 与「均线粘合」标签同一条规则：五线≤3% **或** 短线(5/10/20)≤2%
                    conv = (_sp5 <= _CONV_THR) or (_sps <= _CONV_THR_S)
                out.append((dates[i], closes[i] > s5 / 5.0, closes[i] > s20 / 20.0, conv))
            if i + 1 < n:
                s5 += closes[i + 1] - closes[i - 4]
                s20 += closes[i + 1] - closes[i - 19]
                for _p in rolls:
                    rolls[_p] += closes[i + 1] - closes[i + 1 - _p]
        # 2026-10-21：缓存停在 T-1 的（停牌 / 还没被本轮扫描刷到）**把最后状态顺延到今天**。
        # 不顺延的话，最新那一天的分子分母只算到"当天有 bar 的股票"——新加的板块 123 只里
        # 只有 6 只有今日 bar ⇒ 图上显示"50%（6只）"，看着像坏了。顺延用的是它自己的收盘与均线
        # （停牌股价格本来就不动），不是把"没数据"当成"跌破均线"。
        if _carry and out:
            out.append((latest, out[-1][1], out[-1][2], out[-1][3]))
        return sec, out

    done = 0
    try:
        for p in files:                     # 单线程（JSON 解析持 GIL，与 ma365 同一结论）
            res = _one(p)
            done += 1
            if res:
                sec, out = res
                scanned += 1
                per = counts[sec]
                for d, a5, a20, conv in out:
                    c = per[d]
                    c[2] += 1
                    if a5:
                        c[0] += 1
                    if a20:
                        c[1] += 1
                    if conv is not None:
                        c[4] += 1                      # n60：有 60 根历史、可判粘合的只数
                        if conv:
                            c[3] += 1
            if done % 200 == 0:
                with _SECTOR_MA_LOCK:
                    _SECTOR_MA_STATE["done"] = done
    except Exception:
        pass

    sectors = []
    for sec, per in counts.items():
        pts = [{"date": d, "pct5": round(a5 / t * 100, 2), "pct20": round(a20 / t * 100, 2), "n": t,
                "pct_conv": (round(ac / n60 * 100, 2) if n60 else None), "n60": n60}
               for d, (a5, a20, t, ac, n60) in sorted(per.items()) if t]
        if pts:
            sectors.append({"name": sec, "points": pts[-days:]})
    rec = {"schema": _SECTOR_MA_SCHEMA,
           "computed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "latest_date": latest, "end_date": latest, "sectors": sectors,
           # 2026-10-21：**把板块映射的指纹一起存**。原来只比 `latest_date` ⇒ owner 新加
           # 「大消费/黄金/数据要素」后，数据日没变 ⇒ 永远判"新鲜" ⇒ 面板一直显示旧板块集，
           # 新板块不出现（"占比没有更新"）。指纹 = watchlist mtime，任何打标都会变。
           "board_fp": _watchlist_fp(),
           "coverage": {"codes": len(mine), "scanned": scanned}}
    if not end:                       # 只有"最新"那档写磁盘；历史档只进内存
        try:
            _SECTOR_MA_CACHE_FP.parent.mkdir(parents=True, exist_ok=True)
            tmp = _SECTOR_MA_CACHE_FP.with_suffix(".tmp")
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            tmp.replace(_SECTOR_MA_CACHE_FP)
        except Exception:
            pass
    with _SECTOR_MA_LOCK:
        _SECTOR_MA_STATE["done"] = total
    return rec




# ---- 首帧信号（2026-10-22）---------------------------------------------------------
# 开机预热（`_boot_warmup`：一只票的完整 load_stock_chart + 三路并行预热）会一口气拉起
# 十条 GIL 重的线程；而前端首帧的 `available_dates` 也在这时到 —— 冷启动的 import 锁 +
# 预热线程一起抢，实测首帧调用可能被拖到 20s 投递上限之外，页面直接报「无法连接后端」白开一次。
# 这里让预热**等首帧的第一次 js_api 调用到达**再开始（最多等 20s，后端卡死时不至于永挂）。
_UI_SEEN = threading.Event()


class Api:
    """暴露给前端的 js_api 方法（pywebview 序列化返回值）。"""

    # 东财标的（平均股价）短缓存 {secid: (ts, price, pre_close, src)}，见 _em_last_price
    _em_cache = {}
    # 东财「所属板块」缓存 {code: (ts, boards)}，见 load_stock_profile（TTL 见 _PROFILE_TTL）
    _profile_cache = {}
    _PROFILE_TTL = 600.0
    # 两市成交额缓存 {"payload": (ts, payload)}，见 load_market_turnover
    _turnover_cache = {}
    # 近 N 日成交额缓存 {天数: (ts, payload)} —— **按天数分槽**（2026-10-16）：
    # 单槽时"60 天先预热、250 天请求"会拿到 60 天的数据。见 load_turnover_history
    _turnover_hist_cache = {}
    # 指数板缓存 {"payload": (ts, payload)}，见 load_indices（2026-10-10：原先**无** TTL，
    # 每 10s 轮询都重打 GM index_snapshot，是冷启动窗口 GM 负载的直接来源）
    _indices_cache = {}
    _INDICES_TTL = 10.0

    def __init__(self):
        self._dates_cache = None
        # 建仓/加仓信号增量轮询内存态
        self._pos = {"date": None, "offset": 0, "seen": set()}

    # ---------- 日期发现 ----------
    def available_dates(self):
        _UI_SEEN.set()          # 首帧已到 ⇒ 允许开机预热开跑（见 `_boot_warmup`）
        dates = set()
        for p in OUT.glob("daily_review_*.json"):
            stem = p.stem
            if stem.startswith("daily_review_") and len(stem) == len("daily_review_") + 10:
                dates.add(stem[len("daily_review_"):])
        # 2026-10-09：支持「对历史任意一天扫描/复盘」——补入有建仓扫描留痕的日期 + 最近 60 个交易日，
        # 使前端日期下拉可选中历史日（选中后点「盘后重跑」即对该日重放扫描）。
        for p in TRACES.glob("position_builder_*.jsonl"):
            stem = p.stem
            if stem.startswith("position_builder_") and len(stem) == len("position_builder_") + 10:
                dates.add(stem[len("position_builder_"):])
        from datetime import timedelta
        _d = datetime.now()
        _added = 0
        while _added < 60:
            _d -= timedelta(days=1)
            if _d.weekday() < 5:                       # 近似交易日（节假日空跑无副作用）
                dates.add(_d.strftime("%Y-%m-%d"))
                _added += 1
        today = datetime.now().strftime("%Y-%m-%d")
        dates.add(today)  # 今天始终在首位（默认选中今天进入LIVE，即使盘前尚无数据）
        return sorted(dates, reverse=True)

    # ---------- 单日完整载荷 ----------
    def _pb_cold_start_ready(self) -> bool:
        """冷启动判定：`load_day` 是否该同步算建仓表聚合。

        背景（2026-10-11 实测）：`load_day` → `_agg_position_builder` → 首次 `import
        pandas / analysis.divergence / analysis.m30_features` 要 ~1.8s，而前端首帧正好
        撞在预热线程还没导完的时刻 ⇒ **全堵在 import 锁**上。实测把这一项打桩后，
        首帧 **2.11s → 0.92s**，`load_day` 本身 **1854ms → 133ms**。

        为什么可以跳过而不错：`refresh_pb`（[`t_gui.py:refresh_pb`]）调的是**同一个**
        `_agg_position_builder`，且前端 `_startStaggered("live", …, offset=0)` 在
        `init()` 之后**几乎立即**触发 `refreshLive` → `refresh_pb`，把结果**覆盖**回去
        （而且它的版本还多带个股技术标签）。即冷启动那次同步计算**算了就扔**。

        ⚠️ 只在**模块尚未导入**时跳过。稳态（已导入）仍同步计算 ⇒ 切日期等既有路径
        行为**完全不变**，不引入"切日期后建仓表空 10s"的回归。
        """
        return "pandas" in sys.modules and "analysis.divergence" in sys.modules

    def load_day(self, date=None):
        if not date:
            dates = self.available_dates()
            if not dates:
                return {"date": None, "error": "未找到 daily_review_*.json，请先运行 daily_review.py"}
            date = dates[0]

        out = {"date": date}
        today = datetime.now().strftime("%Y-%m-%d")
        dr_path = OUT / f"daily_review_{date}.json"
        if not dr_path.exists():
            # 今天盘中可能尚无 daily_review；**历史交易日**也返回部分载荷，供「对历史任意一天
            # 扫描/复盘」——daily_review 缺失不应挡住建仓表渲染（未扫过则该表显示"无扫描结果"，
            # 用户点「盘后重跑」即对该日重放）。
            try:
                _wd = datetime.strptime(date, "%Y-%m-%d").weekday()
            except Exception:
                _wd = 5
            if date == today or (TRACES / f"position_builder_{date}.jsonl").exists() or _wd < 5:
                out.update({
                    "sig_stat": {}, "shadow": {"total": None, "near": {}},
                    "qty_freeze": {}, "closed_loop": {}, "audit_problems": None,
                    "settle": {}, "watch": {}, "kpi": {},
                    "positions": self._load_positions(date, {}),
                    # 冷启动（模块未导入）跳过同步聚合 → 首帧不等那 ~1.8s 首次导入；
                    # 前端 init 后立即调 refresh_pb 补齐（同款见上方 _pb_cold_start_ready）
                    "position_builder": (self._agg_position_builder(date)
                                         if self._pb_cold_start_ready()
                                         else {"rows": [], "counts": {},
                                               "cold_start_pending": True}),
                    "stage_board": self._load_stage_board(),
                    "portfolio_config": self.load_portfolio_config(),
                    "report_md": "", "name_map": {},
                })
                return _clean(out)
            return {"date": date, "error": f"无 {date} 复盘数据"}

        try:
            dr = json.loads(open(dr_path, encoding="utf-8").read())
        except Exception as e:
            return {"date": date, "error": f"读取 {dr_path.name} 失败: {e}"}

        out["sig_stat"] = dr.get("sig_stat", {})
        out["shadow"] = {"total": dr.get("shadow_total"), "near": dr.get("shadow_near_±3", {})}
        out["qty_freeze"] = dr.get("qty_freeze", {})
        out["closed_loop"] = dr.get("closed_loop", {})
        out["audit_problems"] = dr.get("audit_problems")
        out["settle"] = dr.get("settle", {})
        out["watch"] = dr.get("watch", {})

        # KPI：优先独立文件，缺失时回退到日复盘内嵌 kpi
        kpi_fp = OUT / f"kpi_{date}.json"
        if kpi_fp.exists():
            out["kpi"] = _load_json(kpi_fp, {})
        else:
            out["kpi"] = dr.get("kpi", {})

        out["positions"] = self._load_positions(date, out.get("kpi", {}))
        # 同 1050 行处：冷启动跳过同步聚合（见 _pb_cold_start_ready 的说明）
        out["position_builder"] = (self._agg_position_builder(date)
                                   if self._pb_cold_start_ready()
                                   else {"rows": [], "counts": {},
                                         "cold_start_pending": True})
        out["stage_board"] = self._load_stage_board()

        out["portfolio_config"] = self.load_portfolio_config()
        out["name_map"] = self._build_name_map(
            out["sig_stat"], out["position_builder"], out["positions"]["current"]
        )
        return _clean(out)

    def _build_name_map(self, sig_stat, pb, current):
        """汇总各数据源构建 {code: name} 映射，供信号条/持仓/结算显示股票名。
        （828fcea6 误删本方法只留调用，load_day 完整路径会 AttributeError；此处恢复原实现）"""
        names = dict(NAMES)
        for src in (sig_stat, current):
            for code, info in (src or {}).items():
                nm = info.get("name") if isinstance(info, dict) else None
                if nm:
                    names[code] = nm
        for code, rec in ((pb or {}).get("by_code") or {}).items():
            for bucket in rec.values():
                row = bucket.get("best") or bucket.get("latest")
                if row and row.get("name"):
                    names[code] = row["name"]
        return names

    # ---------- K4 跨日胜率 ----------
    def kpi_trend(self, days=10):
        dates = self.available_dates()[: int(days)]
        pts = []
        for d in reversed(dates):
            k4 = None
            kpi_fp = OUT / f"kpi_{d}.json"
            if kpi_fp.exists():
                k4 = _load_json(kpi_fp, {}).get("K4_rolling_wr")
            else:
                dr = _load_json(OUT / f"daily_review_{d}.json", {})
                k4 = dr.get("kpi", {}).get("K4_rolling_wr")
            if not k4:
                continue
            buy = k4.get("buy") or {}
            sell = k4.get("sell") or {}
            pts.append({
                "date": d,
                "buy_wr": buy.get("wr"),
                "buy_n": buy.get("n"),
                "sell_wr": sell.get("wr"),
                "sell_n": sell.get("n"),
            })
        return _clean(pts)

    # ---------- 大盘趋势打分 ----------
    def load_market_score(self):
        """跨日 S 打分曲线（日频）。

        2026-10-10: 原「当日盘中曲线」随「今日盘中 S」图表一同下线——解析
        index_regime_{date}.jsonl（50~220KB）在主线程上没有消费者了。
        """
        out = {"history": []}
        state = _load_json(IDX_REGIME / "state.json", {})
        hist = state.get("history") or state.get("score_history") or []
        if state.get("history"):
            out["history"] = [
                {"date": h.get("date"), "S": h.get("S"), "sadj": h.get("sadj"),
                 "regime": h.get("regime")}
                for h in state["history"][-20:]
            ]
        else:
            out["history"] = [
                {"date": h.get("date"), "S": h.get("S"), "regime": None}
                for h in hist
            ]
        out["last_regime"] = state.get("last_regime")
        out["days_in_regime"] = state.get("days_in_regime")
        return _clean(out)

    # ---------- 两市成交额（2026-09-30） ----------
    # 成交额只看分钟线（见 _market_turnover 的三条口径说明），60s 去重：GUI 10s 轮询不至于每轮打两次 GM。
    _TURNOVER_TTL = 60.0

    @staticmethod
    def _turnover_ttl_for(payload):
        """内存去重窗口：**有效结果 60s，失败结果只压 8s**（2026-10-21）。

        原来失败也按 60s 缓存 ⇒ 一次瞬时 GM 失败就让卡片空白整整一分钟，而下一分钟还要再
        付一次 GM 超时（12s×2 腿）。失败不是有价值的信息，不该长期占坑。
        """
        return Api._TURNOVER_TTL if (payload or {}).get("available") is True else 8.0

    def load_market_turnover(self):
        """两市成交额。三段式（2026-10-10）：内存热 → 磁盘快照(stale) → 踢单飞刷新+短等。

        **不再走 `_bounded`**：那个超时会 shutdown(wait=False) 把 worker 连结果一起丢弃（弃工），
        冷启动每 10s 白跑一次完整慢路径。现在慢路径活在单飞后台线程里、必然跑完并写缓存+落盘。
        策略：冷启动立即返回**当日**磁盘快照并标 stale（首屏不再空白），后台刷新到达后前端
        下一轮 10s 轮询自然替换。跨日快照直接丢弃——指数板/成交额是**实时价**，隔夜值当实时是误读。
        """
        now = _time_mod.time()
        hit = Api._turnover_cache.get("payload")
        if hit and (now - hit[0]) < self._turnover_ttl_for(hit[1]):
            return hit[1]                                      # 1) 内存热
        _, ev = _kick_overview_refresh("market_turnover", self._market_turnover_impl)
        snap = _snapshot_read("market_turnover")
        if snap and _is_today(snap):
            return _stale_wrap(snap)                           # 2) 当日快照 + stale
        if ev.wait(0.8):                                       # 3) 首次运行（无快照）：短等真实值
            hit = Api._turnover_cache.get("payload")
            if hit:
                return hit[1]
        return {"available": False, "warming": True}

    def _market_turnover_impl(self):
        now = _time_mod.time()
        hit = Api._turnover_cache.get("payload")
        if hit and (now - hit[0]) < self._turnover_ttl_for(hit[1]):
            return hit[1]
        try:
            out = self._market_turnover()
        except Exception as e:                      # 兜底：绝不让一张卡片拖垮指数板
            out = {"available": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
        Api._turnover_cache["payload"] = (now, out)
        if out.get("available") is True:            # 只落盘有效结果（安全契约第 3 条）
            _snapshot_write("market_turnover", _clean(out))
        return out

    def _turnover_daily_fallback(self):
        """收盘后的**日线兜底**（owner 2026-10-15：盘内读分钟线、盘后直接读快照）。

        数据源 = **本地日线缓存快照** `t_io/cache/daily_kline/index_*.json`（就是 owner 说的"快照"）。
        为什么不用 `provider.index_daily`：那是 **GM 优先**，而 GM 的日线在盘后初段仍**不含当日**
        （本函数 docstring 第 2 条 + 实测 2026-10-15 16:xx：GM 路径取到的最后一行还是昨天），
        而**本地缓存里已经有当日 bar**（盘中 merge 回写过）⇒ 直接用缓存。
        也正因为读的是本地文件，**GM 挂掉时它依然可用** —— 这才是这次真正需要它救场的场景。

        ⚠️ **两腿在日线口径下要用 sh000001 + sz399001（深证成指），不是 sz399106（深证综指）**：
        见 `_market_turnover` docstring 第 1 条 ——「深证成指的**日线** amount 是深市全市场
        （≈深证综指）」，两者日线数值一致；而**分钟**口径下才必须用深证综指。
        实测佐证：`index_sz399106.json` 本地缓存**停在 2026-09-30**（陈旧），
        `index_sz399001.json` 有当日。验算 7744亿+8586亿=1.633万亿，加北交所 ≈1.645万亿，
        与分钟口径 1.646万亿吻合。

        何时返回 None（⇒ 调用方照旧报"分钟线不可得"）：
          · 缓存里**没有今天**的 bar —— 那是"还没收盘/当日未落库"的直接证据。
            用它当判据、而不是比时钟：数据自证，且天然不会在盘中误降级
            （盘中降级会把昨天的成交额当今天报出去，比报错更糟 —— 它看起来是真的）。
          · 非交易日同理（最后一根是上一交易日）⇒ 也不会回来冒充"今日"。

        ⚠️ 北交所日线**无 amount** ⇒ 走新浪 30 分钟线按日求和（与 `_turnover_history_impl` 同口径）；
        取不到就如实降级标 `bse_turnover`，不静默少算 ~120 亿。
        """
        from core.market_data import sina_index
        # ⚠️ **盘中绝不降级**：缓存里**盘中就有当日的 bar**（猎手本轮 merge 回写过实时 bar），
        #    若只看"今天有 bar"就会在盘中把**半天的额**当"全日"报出去 —— 比报错更糟（像真的）。
        #    故先按会话相位挡一道（owner 的口径：盘内读分钟线、盘后读快照），再看数据自证。
        if datetime.now().strftime("%H:%M") < "15:00":
            return None
        today = datetime.now().strftime("%Y-%m-%d")
        tot = {}
        for sym in ("sh000001", "sz399001"):        # 日线口径用深证成指（见上）
            try:
                rows = _load_json(BASE / "t_io" / "cache" / "daily_kline"
                                  / ("index_%s.json" % sym), {}).get("rows") or []
            except Exception:
                return None
            if not rows:
                return None
            for r in rows[-15:]:
                d = str(r.get("date"))[:10]
                tot[d] = tot.get(d, 0.0) + float(r.get("amount") or 0.0)
        if not tot or today not in tot:
            return None                      # 今天没有日线 bar ⇒ 不冒充"今日"
        ds = sorted(tot)
        amt = tot[today]
        prev_day = ds[ds.index(today) - 1] if ds.index(today) > 0 else None
        prev = tot.get(prev_day) if prev_day else None

        degraded = ["minute_unavailable_daily_fallback"]
        bse_included = False
        try:
            sm = sina_index.fetch_index_minutes("bj899050", scale=30, datalen=1500)
            if sm is not None and not sm.empty:
                sm = sm.copy()
                sm["_d"] = sm["time"].astype(str).str[:10]
                byd = sm.groupby("_d")["amount"].sum().to_dict()
                if float(byd.get(today) or 0) > 0:
                    amt += float(byd[today])
                    if prev is not None:
                        prev += float(byd.get(prev_day) or 0)   # 昨日也要加北交所，否则同比偏大
                    bse_included = True
        except Exception:
            pass
        if not bse_included:
            degraded.append("bse_turnover")

        return {"available": True,
                "amount": amt, "amount_prev": prev,
                "delta": (amt - prev) if prev else None,
                "pct": ((amt / prev - 1.0) * 100.0) if prev else None,
                "basis": "全日", "as_of": "15:00",
                "day": today, "prev_day": prev_day,
                "by_leg": {}, "legs": ["sh000001", "sz399106"],
                "bse_included": bse_included, "degraded": degraded}

    def _market_turnover_live(self):
        """给 `_turnover_history_impl` 用的实时成交额：只读内存/当日快照，**绝不**再踢刷新。

        2026-10-10：原先它调 `self.load_market_turnover()`——那本身又是个 `_bounded` 抛弃池，
        一次冷 `load_turnover_history` 会起 ≥2 个池。改为只读，消掉嵌套池。
        """
        hit = Api._turnover_cache.get("payload")
        if hit:
            return hit[1]
        snap = _snapshot_read("market_turnover")
        if snap and _is_today(snap):
            return snap["payload"]
        return None


    def _market_turnover(self):
        """两市成交额 + 与**昨日同期**的变化（owner 2026-09-30）。

        三条口径（都是实测踩过才知道的）：

        1) **两条腿 = 上证指数 + 深证综指(sz399106)**，不是深证成指(sz399001)。
           实测 2026-09-30 分钟线「分时合计 ÷ 日线 amount」：
             上证指数 1.000、深证综指 1.000、**深证成指 0.471**。
           ⇒ 深证成指的**日线** amount 是深市全市场（≈深证综指），但它的**分钟** amount
             只有成分股。盘中要按分时累计，用深证成指会把深市少算一半。
           注：本口径与 `analysis/index_regime._two_market_amount`（上证+深证成指）**数值一致**
           —— 因为那用的是日线字段，而深证成指日线 == 深证综指日线。

        2) **只读分钟线**（不用日线）：日线在盘中/盘后初段**不含当日 bar**
           （gm history_n 实测 16:00 后仍只返回到昨日），要拿当日得走 forming bar，
           而 forming bar 只在 `end_date is None` 时拼接、会回写共享长历史缓存
           （见 memory「指数日线缓存会被静默截短」）。分钟线一次拿到今昨两天，零缓存风险。

        3) **盘中比昨日同期、收盘比全日**：今日是累计值，10:00 时可能才走 20%，
           直接比昨日全日会显示 −80%，严重误导。故盘中取昨日**同一时刻**的累计值对比
           （owner 选定的口径）；已收盘（或今天不是交易日）则两边都取全日。

        返回 {available, amount, amount_prev, pct, basis: "同期"|"全日", as_of, day, prev_day,
              legs, bse_included}。
        """
        from core.market_data.facade import get_provider
        from core.market_data import sina_index
        per_leg, legs, degraded = {}, [], []

        def _norm(df):
            d = df.copy()
            d["_day"] = d["time"].astype(str).str[:10]
            d["_hm"] = d["time"].astype(str).str[11:16]
            return d

        _today = datetime.now().strftime("%Y-%m-%d")
        _hm_now = datetime.now().strftime("%H:%M")
        _in_session = datetime.now().weekday() < 5 and "09:15" <= _hm_now <= "15:05"

        def _stale_day(df):
            """盘中却停在上一交易日 ⇒ 该源"新鲜度"不合格（不能拿昨天的额当今天报）。"""
            try:
                return _in_session and str(df["time"].astype(str).max())[:10] < _today
            except Exception:
                return False

        def _leg_frame(sym):
            """一条腿的分钟线：**GM 优先 → 新浪兜底**（2026-10-21，owner 报"盘中经常读不到"）。

            原来两腿**只走 GM**，终端重启/令牌轮换/冷却窗一命中，整张卡片就是空白
            （卡片只剩一行「分钟线不可得(sh000001)」）。新浪这条源实测与 GM **逐日完全一致**
            —— 2026-10-19/20/21 三天：沪 6870.1/6999.7/6427.5 亿、深 7505.0/7561.4/7865.9 亿，
            浮点都不差（`tmp/probe_turnover_sources.py`）。同口径 ⇒ 可以直接顶替，无需换算。

            优先级：GM 且新鲜 → 新浪且新鲜 → **两者都不新鲜时仍回退给已有的那一份**
            （节假日/长假里"今天"本来就没有 bar，此时显示上一交易日**全日**额是对的，
             不能因为"不是今天"就把卡片打成空白 —— 那等于每逢假期就坏）。
            """
            _empty = lambda d: d is None or getattr(d, "empty", True)     # noqa: E731
            gm = None
            try:
                _prov = get_provider()
                # 冷却窗内直接走新浪：这张是**展示卡片**，不该为它再付一次 12s 的 GM 硬超时
                # （那正是"等半天还是空白"的体感来源）。
                # ⚠️ 只能读 `_gm_down_until`，**不能**先调 `_gm_ok_wait()` 探一句 ——
                #    那会占掉半开探针窗，`index_minute` 内部的 `_gm_ok()` 随即判"窗被占"⇒
                #    直接返空表，等于把本来健康的 GM 也废掉（实测踩到：正常态被判成 sina 兜底）。
                _down = getattr(_prov, "_gm_down_until", None)
                if _down is None or datetime.now() >= _down:
                    gm = _prov.index_minute(sym, count_bars=800, freq="300s")
            except Exception:
                gm = None
            if not _empty(gm) and not _stale_day(gm):
                return gm
            sdf = None
            try:
                sdf = sina_index.fetch_index_minutes(sym, scale=5, datalen=400)
            except Exception:
                sdf = None
            if not _empty(sdf) and not _stale_day(sdf):
                degraded.append("index_minute_sina")
                return sdf
            if not _empty(gm):
                degraded.append("index_minute_prevday")      # 假期/盘前：用上一交易日整日值
                return gm
            if not _empty(sdf):
                degraded.append("index_minute_prevday")
                return sdf
            return None

        # 沪深两腿：指数分钟线（含 amount）
        _minute_fail = None
        for sym in ("sh000001", "sz399106"):     # 上证指数 + 深证综指
            df = _leg_frame(sym)
            if df is None:
                _minute_fail = sym
                break
            per_leg[sym] = _norm(df)
            legs.append(sym)

        if _minute_fail:
            # owner 2026-10-15：「盘内可以读分钟线，**盘后直接读快照**」。
            # 分钟线不可得（GM 终端挂掉/限流/权限断）时，**盘后**降级走日线快照。
            # ⚠️ 绝不盘中降级：盘中日线不含当日 bar，降级会把"昨天的成交额"当今天报出去 ——
            #    那比报错更糟，因为它看起来是真的。判据见 `_turnover_daily_fallback`。
            _fb = self._turnover_daily_fallback()
            if _fb:
                return _fb
            return {"available": False, "error": f"分钟线不可得({_minute_fail})",
                    "degraded": ["index_minute"]}

        # 北交所腿：只能走新浪（见 sina_index 模块 docstring）。取不到则**如实降级为沪深口径**，
        # 而不是静默少算 ~120 亿 —— 那正是本次与同花顺对不上的原因。
        bse = sina_index.fetch_index_minutes("bj899050", scale=5, datalen=400)
        bse_included = (bse is not None and not bse.empty
                        and float(bse["amount"].sum()) > 0 and not _stale_day(bse))
        if bse_included:
            per_leg["bj899050"] = _norm(bse)
            legs.append("bj899050")
        else:
            degraded.append("bse_turnover")

        out = Api._turnover_from_minutes(per_leg, datetime.now())
        out["legs"] = legs
        out["bse_included"] = bse_included
        if degraded:
            out["degraded"] = list(dict.fromkeys(degraded))   # 去重（两腿各记一次会出现重复）
        return out


    def _turnover_from_minutes(per_leg, now):
        """纯计算（无 IO，便于离线单测）：{腿: 分钟线} + 当前时刻 → 成交额与变化。

        见 `_market_turnover` 的三条口径说明。关键点：
          · 「当前交易日」取分钟数据里的**最新一天**，不是日历日期 ⇒ 周末/节假日自动落到上一交易日
          · 盘中取两边 **≤ 当前时刻** 的累计；收盘后（或今天非交易日）两边都取全日
        """
        cur_day = max(d["_day"].max() for d in per_leg.values())
        days = sorted({x for d in per_leg.values() for x in d["_day"].unique()})
        prior = [x for x in days if x < cur_day]
        if not prior:
            return {"available": False, "error": "分钟线里没有上一交易日", "degraded": ["index_minute"]}
        prev_day = prior[-1]

        hm = now.strftime("%H:%M")
        # 盘中 = 今天就是当前交易日 且 未到收盘；否则（周末/收盘后）两边都按全日
        intraday = (cur_day == now.strftime("%Y-%m-%d") and hm < "15:00")

        def _sum(day):
            per, as_of = {}, None
            for sym, d in per_leg.items():
                sub = d[d["_day"] == day]
                if intraday:
                    sub = sub[sub["_hm"] <= hm]
                if len(sub):
                    per[sym] = float(sub["amount"].sum())
                    last = str(sub["_hm"].iloc[-1])
                    as_of = last if as_of is None else max(as_of, last)
            return per, as_of

        per_cur, as_of = _sum(cur_day)
        per_prev, _ = _sum(prev_day)
        amount, amount_prev = sum(per_cur.values()), sum(per_prev.values())
        if not amount or not amount_prev:
            return {"available": False, "error": "成交额累计为 0", "degraded": ["index_minute"]}
        out = {
            "available": True,
            "amount": amount, "amount_prev": amount_prev,
            # 2026-10-23 owner：「要显示增量多少资金」——绝对值（元），不是只有百分比。
            # 盘中是"比昨日同期"、收盘后是"比昨日全日"，与 pct 同分母。
            "delta": amount - amount_prev,
            "pct": (amount / amount_prev - 1.0) * 100.0,
            "basis": "同期" if intraday else "全日",
            "as_of": as_of, "day": cur_day, "prev_day": prev_day,
            "by_leg": per_cur,          # 分腿拆解（前端 tooltip 用）
        }
        if intraday:
            # 2026-10-23 owner：「预测今天的成交额有多少」。
            out.update(Api._project_full_day(per_leg, prior, hm, amount))
        return out

    # 预测口径参数：拿最近多少天的「同一时刻累计占比」做外推
    _PROJ_DAYS = 10

    @staticmethod
    def _project_full_day(per_leg, prior_days, hm, amount):
        """盘中按「近 N 日**同一时刻**的累计占全日比例」外推今日全日成交额。

        为什么不是简单的 `amount / 已过时间占比`：A 股日内成交是 **U 型**（开盘半小时和
        尾盘各占一大块），线性外推在上午会**系统性低估**、午后**高估**。
        这里改用历史同日时刻的经验占比（多腿合计、取**中位数**抗单日异常），
        精度只取决于"今天的日内节奏和最近十天像不像"，不依赖任何形态假设。

        * 样本不足（分钟线只覆盖 1 天）⇒ 退回**线性时间占比**并如实标 `proj_method="elapsed"`。
        * 返回 projected(元) / proj_frac(已走完的占比) / proj_days(样本天数) / proj_method。
        """
        def _frac_of(day):
            full = cum = 0.0
            seen = False
            for d in per_leg.values():
                sub = d[d["_day"] == day]
                if not len(sub):
                    continue
                seen = True
                full += float(sub["amount"].sum())
                cum += float(sub[sub["_hm"] <= hm]["amount"].sum())
            return (cum / full) if (seen and full > 0 and cum > 0) else None

        fr = [x for x in (_frac_of(d) for d in list(prior_days)[-Api._PROJ_DAYS:]) if x]
        if not fr:
            # 没有"同一时刻"的历史占比就**不给数**：线性时间外推在 U 型日内系统性偏离
            # （上午低估 ~3 倍），给一个看起来像真的假数比不给更糟。
            return {"projected": None, "proj_frac": None, "proj_days": 0,
                    "proj_method": "no_history"}
        fr.sort()
        frac = fr[len(fr) // 2] if len(fr) % 2 else (fr[len(fr) // 2 - 1] + fr[len(fr) // 2]) / 2.0
        # 占比太小时外推是 ×20 以上的放大，噪声压过信号（09:30~09:40）⇒ 不给数，前端就不显示
        if not (0.05 <= frac < 1.0):
            return {"projected": None, "proj_frac": round(frac, 4),
                    "proj_days": len(fr), "proj_method": "near_days_same_time"}
        return {"projected": amount / frac, "proj_frac": round(frac, 4),
                "proj_days": len(fr), "proj_method": "near_days_same_time"}

    # ---------- 近 N 日成交额（「盘中状态」卡柱状图，2026-09-30） ----------
    _TURNOVER_HIST_TTL = 120.0
    # 柱状图窗口（交易日）。2026-10-16 owner：「增加到一年」⇒ 60 → 250（≈1 年）。
    # ⚠️ **它必须是默认值**：`prewarm_overview` 用裸调 `self.load_turnover_history`
    #    预热，若默认仍是 60，预热出来的那份 60 天缓存会顶掉前端要的 250 天
    #    （缓存键当年不含 days，见下）。
    _TURNOVER_HIST_DAYS = 250

    def load_turnover_history(self, days=None):
        """近 N 日全市场成交额。三段式（2026-10-10，同 load_market_turnover，见其 docstring）。

        与前两者**跨日规则不同**：柱状图跨日依然有意义，故快照**允许跨日**显示——
        但存的 `in_progress`/`day`/`as_of`/`basis` 必须**原样重放**（`_stale_wrap` 只加 stale 元信息、
        不重算任何字段），否则昨天的最后一根会被当成今天正在走的实时柱。

        ⚠️ **`days` 必须进缓存键（内存 + 磁盘快照 + 单飞 key 三处）**，2026-10-16 修：
        原实现是**单槽** `_turnover_hist_cache["payload"]` + 单名快照 `turnover_history.json`，
        `days` 完全不参与 ⇒ **谁先填谁说了算，一填管 120 秒**。而 `prewarm_overview` 启动时
        用**裸调**（当时默认 60）预热 ⇒ 前端问 250 却拿到那份 60 天的缓存，
        表现就是**图上只有 ~60 根、标题却写着"近1年"**（owner 当场截图报回来的）。
        """
        days = int(self._TURNOVER_HIST_DAYS if days is None else days)
        _k = "turnover_history_%d" % days
        now = _time_mod.time()
        hit = Api._turnover_hist_cache.get(days)
        if hit and (now - hit[0]) < self._TURNOVER_HIST_TTL:
            return hit[1]                                      # 1) 内存热（按天数分槽）
        _, ev = _kick_overview_refresh(
            _k, lambda: self._turnover_history_cached(days))
        snap = _snapshot_read(_k)
        if snap:
            return _stale_wrap(snap)                           # 2) 快照（可跨日）+ stale
        if ev.wait(0.8):                                       # 3) 首次运行：短等真实值
            hit = Api._turnover_hist_cache.get(days)
            if hit:
                return hit[1]
        return {"available": False, "warming": True, "days": []}

    def _turnover_history_cached(self, days=None):
        """近 N 个交易日的**全市场**成交额（柱状图用）。失败返回 {available: False, error}，永不抛。

        ⚠️ 缓存与快照**都按天数分槽**（2026-10-16，理由见 `load_turnover_history` docstring）：
        单槽时"60 天先预热、250 天请求"会拿到 60 天的数据。
        """
        days = int(self._TURNOVER_HIST_DAYS if days is None else days)
        now = _time_mod.time()
        hit = Api._turnover_hist_cache.get(days)
        if hit and (now - hit[0]) < self._TURNOVER_HIST_TTL:
            return hit[1]
        try:
            out = self._turnover_history_impl(days)
        except Exception as e:
            out = {"available": False, "error": f"{type(e).__name__}: {str(e)[:80]}"}
        Api._turnover_hist_cache[days] = (now, out)
        if out.get("available") is True:            # 只落盘有效结果（安全契约第 3 条）
            _snapshot_write("turnover_history_%d" % days, _clean(out))
        return out

    def _turnover_history_impl(self, days):
        """拼装近 N 日全市场成交额（与 `load_market_turnover` 同口径：沪 + 深 + 北）。

        三源：
          · 沪深：`index_daily(上证指数/深证综指)` 的 amount。**传 end_date** ⇒ provider 不回写
            共享长历史缓存（不传会把 800 行截短，见 memory「指数日线缓存会被静默截短」）。
          · 北交所：新浪 `bj899050` **30 分钟**线按日求和。为什么不用别的精度：
            日线**无 amount**（只有 volume）；5 分钟线 `datalen` 上限只够 ~32 天；
            30 分钟 `datalen=1500` 给 188 天，且按日合计与 5 分钟口径**逐位一致**（实测 125.3/122.4 亿）。
          · 今日那一根：用 `load_market_turnover()` 的实时值**覆盖** ⇒ 柱状图最后一根与
            第 8 张卡片上的数字严格一致，不会出现"图上和卡上不一样"。

        ⚠️ 盘中 GM 日线不含当日 ⇒ 若实时交易日不在日线序列里，**补一根**。
        """
        from core.market_data.facade import get_provider
        from core.market_data import sina_index
        today = datetime.now().strftime("%Y-%m-%d")
        prov = get_provider()

        # ── 沪深两腿：**必须两腿都是"真成交额"才可信**（2026-10-16 修，见 `_HS_DAILY_CACHE_FP` 注释）
        # 判据用**量级**而不是 source 标签：真·指数日成交额必 ≥ 1e10 元；腾讯回退塞进来的
        # volume 只有 ~1e8~1e9。低量级那一腿记为不可信，**当天整条不进 hs**（宁缺勿假）。
        _frames = []
        for sym in ("sh000001", "sz399106"):
            df = prov.index_daily(sym, days=int(days) + 60, end_date=today)
            if df is None or df.empty:
                return {"available": False, "error": f"指数日线不可得({sym})",
                        "degraded": ["index_daily"]}
            _frames.append(df)
        _good, _bad = Api._hs_daily_from_frames(_frames)

        _cache = _load_hs_daily_cache()
        _new = {d: v for d, v in _good.items() if _cache.get(d) != v}
        if _new:
            _cache.update(_new)
            _save_hs_daily_cache(_cache)
        hs = dict(_cache)                            # 用**冻结**值（含本次补上的好值）
        if not hs:
            return {"available": False, "error": "沪深日线为空", "degraded": ["index_daily"]}

        live = self._market_turnover_live() or {}     # 只读，绝不再起嵌套池（2026-10-10）
        live_day = live.get("day") if live.get("available") else None
        live_amount = live.get("amount") if live.get("available") else None

        bse = {}
        sm = sina_index.fetch_index_minutes("bj899050", scale=30, datalen=1500)
        if sm is not None and not sm.empty:
            sm = sm.copy()
            sm["_d"] = sm["time"].astype(str).str[:10]
            bse = sm.groupby("_d")["amount"].sum().to_dict()

        return Api._assemble_turnover_history(hs, bse, days, live_day, live_amount,
                                             datetime.now(), _bad)


    def _hs_daily_from_frames(frames, min_leg_amt=1e10):
        """两条腿（沪/深）的日线帧 → `({date: 沪深合计}, [不可信日期])`。**纯函数，可离线单测**。

        ⚠️ **为什么要有它**（2026-10-16 实盘缺陷）：`facade.index_daily` 会回退到腾讯，而腾讯
        ifzq 的指数日线**只有 6 列、没有成交额** ⇒ `tencent_provider` 用 `volume` 顶替 `amount`
        （那条为 ratio 口径刻意保留）。于是某腿的"成交额"只有 ~1e8~1e9（成交量级），
        与另一腿的真值 ~1e11 **相加 = 当天腰斩**，且完全静默 —— owner 当场截图：图上一半高、
        环比 +99%、5 日均量 9108 亿（真值 1.65 万亿）。

        判据用**量级**而不是 source 标签：真·指数日成交额必 ≥ `min_leg_amt`(1e10 元)。

        **两腿不全可信的日子整条不进结果**（宁缺勿假 —— 宁可少一根柱，不画半截的柱）。
        调用方再用「逐日冻结缓存」把算对过的历史值补回来（历史成交额是事实，算对一次就永远对）。
        """
        legs = {}
        for df in frames:
            try:
                for _, r in df.iterrows():
                    a = float(r.get("amount") or 0.0)
                    legs.setdefault(str(r["date"]), []).append(a if a >= min_leg_amt else None)
            except Exception:
                continue
        good = {d: sum(v) for d, v in legs.items() if len(v) == 2 and all(x is not None for x in v)}
        bad = sorted(d for d in legs if d not in good)
        return good, bad

    def _assemble_turnover_history(hs, bse, days, live_day, live_amount, now, hs_bad=None):
        """纯计算（无 IO，便于离线单测）：沪深/北交所按日成交额 + 实时值 → 柱状图数据。

        · 交易日集合 = 沪深日线日期 ∪ {实时交易日}（盘中 GM 日线不含当日 ⇒ 必须补这一根）
        · 最后一根的 amount 用**实时值覆盖** ⇒ 与「两市成交额」卡片数字严格一致
        · `in_progress` 只标最后一根，且仅在「该日就是今天且未到 15:00」时
        · `delta_pct` 环比**前一交易日**；首日无前值 ⇒ None
        """
        all_dates = sorted(set(hs) | ({live_day} if live_day else set()))
        dates = all_dates[-int(days):]
        rows = [{"date": d, "amount": float(hs.get(d, 0.0)) + float(bse.get(d, 0.0))} for d in dates]
        if live_day and live_amount is not None and rows and rows[-1]["date"] == live_day:
            rows[-1]["amount"] = float(live_amount)
        today = now.strftime("%Y-%m-%d")
        in_progress = bool(rows and rows[-1]["date"] == today and now.strftime("%H:%M") < "15:00")
        for i, r in enumerate(rows):
            prev = rows[i - 1]["amount"] if i > 0 else None
            r["delta_pct"] = ((r["amount"] / prev - 1.0) * 100.0) if prev else None
            r["in_progress"] = bool(in_progress and i == len(rows) - 1)
            r["bse"] = float(bse.get(r["date"], 0.0))
            # 2026-10-16（owner 要「近 1 年」）：北交所腿**够不到一年**，必须逐日如实标注。
            # 源 = 新浪 bj899050 **30 分钟**线，`datalen` 硬上限 1500 根 = **188 个交易日**
            # （实测再往上（2500/3000/5000）直接返反爬桩 "新浪指数K线响应异常"）。
            # 缺的那段 `amount` 只有沪深两腿 ⇒ 比真实值**少约 120 亿/日（~0.6%）**。
            # ⚠️ 0.6% 在图上只有 ~1 像素，**静默**过去就会被当成"那阵子缩量" ⇒ 必须能看出来。
            r["bse_missing"] = bool(bse) and (r["date"] not in bse)
        return {"available": True, "days": rows, "in_progress": in_progress,
                # ⚠️ 2026-10-16：某日沪深两腿不全可信（腾讯回退塞 volume）⇒ 该日**不进 hs**、
                # 自然从序列里消失（宁缺勿假，不画半截的柱）。这里只报个数供前端提示。
                "hs_leg_unreliable_days": len(hs_bad or []),
                "bse_included": bool(bse),
                # 北交所腿的覆盖区间（前端据此说明"哪一段少算了北交所"）
                "bse_days": len(bse), "bse_from": (min(bse) if bse else None),
                "legs": ["sh000001", "sz399106", "bj899050"]}

    # ---------- 主要指数概览（2026-09-28：GUI_INDEX_BOARD 单一真源 + 掘金主源） ----------
    # 2026-10-10: 原 `_bounded(fn, timeout, default)`（每调用新建 max_workers=1 池 + 超时
    # `shutdown(wait=False)`）已删除——它的三个调用方（load_indices / load_market_turnover /
    # load_turnover_history）全部改走「内存热 → 磁盘快照 → 单飞刷新」三段式。
    # 删它的理由：超时后遗弃的 worker 继续跑完整慢路径、结果被丢弃（弃工），且每调用漏一个线程；
    # 单飞刷新让慢路径必然跑完并写缓存+落盘，不需要再"钉时间上界"。

    # ---------- 年限宽度：每日站上 365 日均线的股票占比（2026-10-18） ----------
    def load_ma365_breadth(self, days=120, start=None):
        """**每日站上 365 日均线（年限）的股票占比** → 柱状图数据。SWR：主线程零计算。

        全量解析 5500 个日线缓存 ≈10s ⇒ 后台线程算 + 磁盘缓存（按数据日复用）。
        未就绪回 `warming`（带进度），前端短轮询；就绪后回 `series`（按 `days` 切片）。
        样本/分母口径见 `_compute_ma365_breadth`（停牌/陈旧缓存不进分母，避免误算）。

        `start`（2026-10-18）：如 "2024-09-01" ⇒ 读**预先补算好的长历史档**
        （`_MA365_HIST_FP`，由 scripts/experiment 用 GM 900 根一次性补算）。本地 800 根缓存
        凑不满 2024-09 的 MA365 回看窗口，必须在别处补历史。
        """
        if start:
            try:
                _rec = json.loads(_MA365_HIST_FP.read_text(encoding="utf-8"))
                _series = [x for x in (_rec.get("series") or [])
                           if str(x.get("date")) >= str(start)[:10]]
            except Exception:
                return _clean({"available": False, "historical": True, "error":
                               "历史区间尚未计算：先运行 scripts/experiment/"
                               "ma365_breadth_hist_20240901.py"})
            if not _series:
                return _clean({"available": False, "historical": True,
                               "error": "历史档里没有该区间数据"})
            # MA20 历史档不走 GM 补算：只要 20 根回看，本地 800 根缓存就够 ⇒ 直接取本地这份
            # 计算结果，右端按 365 档的 latest_date 截齐（两张图横轴终点必须一致）。
            _s20 = self._ma20_hist_series(str(start)[:10], _rec.get("latest_date"))
            if not _s20:
                self._kick_ma365_breadth()      # 旧缓存没有 series20 / 尚未算过 → 后台补一次
            return _clean({"available": True, "historical": True,
                           "start": str(start)[:10],
                           "latest_date": _rec.get("latest_date"),
                           "computed_at": _rec.get("computed_at"),
                           "series": _series, "coverage": _rec.get("coverage") or {},
                           "last": _series[-1], "stale": False,
                           "series20": _s20, "last20": _s20[-1] if _s20 else {},
                           "ma20_warming": not _s20})
        try:
            days = max(30, min(int(days or 120), _MA365_MAX_DAYS))
        except Exception:
            days = 120
        now = _time_mod.time()
        with _MA365_LOCK:
            mem = dict(_MA365_MEM)
        # 内存值必须**带 series20** 才算命中（2026-10-20 之前算出来的值没有这一项）——
        # 否则退回磁盘那份，别让用户看到"年限宽度有、下面那张空着"。
        if (mem.get("val") and (mem["val"] or {}).get("series20")
                and (now - mem.get("ts", 0.0)) < _MA365_MEM_TTL):
            return _clean(self._ma365_payload(mem["val"], days, stale=False))

        disk = None
        try:
            if _MA365_CACHE_FP.exists():
                disk = json.loads(_MA365_CACHE_FP.read_text(encoding="utf-8"))
        except Exception:
            disk = None
        latest = _market_latest_date()
        if disk and disk.get("series"):
            # 旧缓存（2026-10-20 之前）没有 `series20` ⇒ 同样判 stale，后台补算一次；
            # 这期间年限宽度照常出图，站上20线那张显示"计算中"。
            stale = (str(disk.get("latest_date") or "") < latest
                     or not (disk.get("series20") or []))
            if stale:
                self._kick_ma365_breadth()
            return _clean(self._ma365_payload(disk, days, stale=stale))

        self._kick_ma365_breadth()
        with _MA365_LOCK:
            st = dict(_MA365_STATE)
        return _clean({"available": False, "warming": True, "days": days,
                       "progress": {"done": int(st.get("done") or 0),
                                    "total": int(st.get("total") or 0)}})

    @staticmethod
    def _ma365_payload(rec, days, stale=False):
        series = (rec.get("series") or [])[-days:]
        series20 = (rec.get("series20") or [])[-days:]
        cov = rec.get("coverage") or {}
        last = series[-1] if series else {}
        return {"available": True, "stale": bool(stale), "days": days,
                "latest_date": rec.get("latest_date") or last.get("date"),
                "computed_at": rec.get("computed_at"), "series": series,
                "coverage": cov, "last": last,
                "series20": series20, "last20": series20[-1] if series20 else {},
                "ma20_warming": not series20}

    @staticmethod
    def _ma20_hist_series(start, end=None):
        """「站上20日线占比」历史档：从本地计算结果按 `[start, end]` 收边（内存优先、磁盘兜底）。

        与 MA365 的历史档不同，这里**不需要** GM 补算 —— MA20 只要 20 根回看，本地 800 根
        日线缓存足以覆盖 2024-09 至今。`end` 传 365 档的 latest_date，让两张图右端对齐。
        """
        rec = None
        with _MA365_LOCK:
            mem = dict(_MA365_MEM)
        if (mem.get("val") or {}).get("series20"):
            rec = mem["val"]
        if not rec:                      # 内存没有/是旧口径 ⇒ 退回磁盘
            try:
                _disk = json.loads(_MA365_CACHE_FP.read_text(encoding="utf-8"))
                rec = (_disk if _disk.get("series20") else None)
            except Exception:
                rec = None
        start = str(start or "")
        end = str(end or "")
        out = []
        for x in ((rec or {}).get("series20") or []):
            d = str(x.get("date") or "")
            if not d or (start and d < start) or (end and d > end):
                continue
            out.append(x)
        return out

    def _kick_ma365_breadth(self):
        """后台算一次年限宽度（单飞）。仅后台线程跑；失败静默。"""
        with _MA365_LOCK:
            if _MA365_STATE.get("running"):
                return
            _MA365_STATE.update({"running": True, "done": 0, "total": 0,
                                 "started": _time_mod.time()})

        def _bg():
            try:
                val = _compute_ma365_breadth(_MA365_MAX_DAYS)
                with _MA365_LOCK:
                    _MA365_MEM.update({"ts": _time_mod.time(), "val": val})
            except Exception:
                pass
            finally:
                with _MA365_LOCK:
                    _MA365_STATE["running"] = False
        _th.Thread(target=_bg, daemon=True, name="ma365-breadth").start()

    def ping(self):
        """**投递层探针**（2026-10-10）：什么都不做，立即返回。

        用途：前端每 2s 调一次并量往返。这个端点本征耗时 ~0，所以量到的任何延迟都是
        **纯投递开销**（pywebview 排队 + UI 线程 Invoke + evaluate_js 的 semaphore + JS 主线程阻塞）。
        背景：`load_console` 进程内本征 4ms，生产日志却记 23202ms ⇒ 慢的绝大部分不在后端计算。
        这个探针把"后端慢"与"投递慢"彻底分开。
        """
        return {"t": _time_mod.time()}

    def report_client_diag(self, kind, value, note=""):
        """前端上报的**客户端侧**诊断（2026-10-10）→ `t_io/logs/gui_client_diag.log`。

        kind: "jsblock"（JS 主线程阻塞 ms）| "ping"（空端点往返 ms）
        value: 毫秒数
        与 `report_slow_call` 分开写，避免污染那份"慢调用"记录。失败静默。
        """
        try:
            log_fp = BASE / "t_io" / "logs" / "gui_client_diag.log"
            log_fp.parent.mkdir(parents=True, exist_ok=True)
            with open(log_fp, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                        f"{str(kind)[:16]:<10} {float(value):9.0f}ms  {str(note)[:80]}\n")
        except Exception:
            pass
        return {"ok": True}

    def dump_threads(self, tag=""):
        """**按需落线程栈**（2026-10-10）：前端测到投递层极慢时自动调它。

        为什么需要：看门狗（`_start_gui_freeze_watchdog`）只在**心跳停 >12s** 时才落栈，而 2s 的
        `load_console` 一直在续心跳 ⇒ 即使每次调用 20s 也触发不了。这里由前端主动触发，
        把"投递层慢时 Python 侧卡在哪一帧"抓下来。失败静默。
        """
        try:
            import faulthandler
            log_fp = BASE / "t_io" / "logs" / "gui_thread_dumps.log"
            log_fp.parent.mkdir(parents=True, exist_ok=True)
            with open(log_fp, "a", encoding="utf-8") as f:
                f.write(f"\n===== 按需线程栈 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        f" tag={str(tag)[:60]} 活线程={_th.active_count()} =====\n")
                faulthandler.dump_traceback(file=f, all_threads=True)
        except Exception:
            pass
        return {"ok": True}

    def report_slow_call(self, name, ms):
        """前端上报的慢 js_api 调用（>=800ms）→ 追加到 `t_io/logs/gui_slow_calls.log`。
        2026-10-07：GUI 无自身日志，卡死只能靠外部探测；这里给"卡在哪"留痕。失败静默。"""
        try:
            log_fp = BASE / "t_io" / "logs" / "gui_slow_calls.log"
            log_fp.parent.mkdir(parents=True, exist_ok=True)
            with open(log_fp, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {str(name)[:40]} {float(ms):.0f}ms\n")
        except Exception:
            pass
        return {"ok": True}

    def report_chart_event(self, event, detail=""):
        """前端 K 线缩放/重绘事件 → `t_io/logs/chart_zoom.log`（每行一条 JSON）。

        2026-10-20：日线缩放"约 1 分钟后失效"反复复现，前端状态是黑盒 ⇒ 加专用事件流，
        用来对齐「render / dataZoom 事件 / 缩放按钮 / 周期切换 / 20s 轮询」的时序。
        纯诊断，失败静默，不参与任何判定。
        """
        try:
            fp = BASE / "t_io" / "logs" / "chart_zoom.log"
            fp.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                   "event": str(event)[:60], "detail": str(detail)[:600]}
            with open(fp, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return {"ok": True}

    def load_indices(self):
        """指数状态板。三段式（2026-10-10）：内存热 → 当日磁盘快照(stale) → 踢单飞刷新+短等。

        原先走 `_bounded(2.5s)`：冷启动必超时返回 {"indices":[],"error":"指数板取数超时"} ⇒
        **首屏空白**，而被丢弃的 worker 还在后台跑完整慢路径。现在慢路径在单飞线程里必然跑完。
        快照**仅当日可用**：指数板本就只在 `isToday` 展示（web/app.js:215），隔夜价当实时是误读。
        """
        now = _time_mod.time()
        hit = Api._indices_cache.get("payload")
        if hit and (now - hit[0]) < self._INDICES_TTL:
            return hit[1]                                      # 1) 内存热
        _, ev = _kick_overview_refresh("indices", self._load_indices_impl)
        snap = _snapshot_read("indices")
        if snap and _is_today(snap):
            return _stale_wrap(snap)                           # 2) 当日快照 + stale
        if ev.wait(0.8):                                       # 3) 首次运行：短等真实值
            hit = Api._indices_cache.get("payload")
            if hit:
                return hit[1]
        return {"ts": None, "indices": [], "regime": None, "error": "指数板取数中"}

    def _load_indices_impl(self):
        """拉指数板实时行情 + 大盘 regime。返回 {ts, indices:[{symbol,name,price,change,change_pct,source}], ...}

        指数列表来自 core.board_index.GUI_INDEX_BOARD（**单一真源**，7 项）。
        实时源：**GM 主源** —— get_provider().index_snapshot()（内部 gm.api.current，指数符号经
        _gm_index_symbol 正确编码）；GM 不可用时 facade 内建降级腾讯 index_auction。
        返回的 source 字段标明本条实际来自 "gm" 还是 "tencent"。
        平均股价（source="em"）非交易所指数、掘金无此标的 → 单列走东财 push2delay。
        """
        out = {"ts": None, "indices": [], "regime": None, "days_in_regime": None}
        try:
            import os as _os
            import urllib.request as _ur
            for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "all_proxy"]:
                _os.environ.pop(_k, None)
            _os.environ["NO_PROXY"] = "*"

            from core.board_index import gui_board
            board = gui_board()

            # 真指数：GM 主源（facade 内建腾讯兜底）
            gm_items = [i for i in board if i.get("source") == "gm"]
            snaps = {}
            if gm_items:
                try:
                    from core.market_data import get_provider
                    snaps = get_provider().index_snapshot([i["symbol"] for i in gm_items]) or {}
                except Exception:
                    snaps = {}

            for i in board:
                sym, name, src = i["symbol"], i["name"], i.get("source")
                if src == "gm":
                    d = snaps.get(sym) or {}
                    price = d.get("price")
                    if not price:
                        # 取不到也占位：卡片置灰显示原因，不静默消失（否则用户不知道为什么少一个）
                        out["indices"].append({"symbol": sym, "name": name,
                                               "error": "掘金与腾讯均取不到", "source": "gm"})
                        continue
                    out["indices"].append({
                        "symbol": sym, "name": name, "price": price,
                        "change": (round(d["change"], 3) if d.get("change") is not None else None),
                        "change_pct": (round(d["change_pct"], 2)
                                       if d.get("change_pct") is not None else None),
                        "source": d.get("source") or "gm",
                    })
                elif src == "em":
                    # 东财特殊条目（A股平均股价等）：非交易所指数，掘金无此标的
                    secid = str(sym)[2:] if str(sym).startswith("em") else str(sym)
                    price, pre_close, esrc = self._em_last_price_bounded(secid)
                    if not price:
                        out["indices"].append({"symbol": sym, "name": name,
                                               "error": "东财风控/不可达", "source": "em"})
                        continue
                    chg = (price - pre_close) if pre_close else None
                    out["indices"].append({
                        "symbol": sym, "name": name, "price": price,
                        "change": round(chg, 3) if chg is not None else None,
                        "change_pct": (round(chg / pre_close * 100.0, 2)
                                       if (chg is not None and pre_close) else None),
                        "source": esrc,
                    })
            out["ts"] = datetime.now().strftime("%H:%M:%S")
        except Exception:
            pass
        # 大盘 regime 状态
        try:
            state = _load_json(IDX_REGIME / "state.json", {})
            out["regime"] = state.get("last_regime")
            out["days_in_regime"] = state.get("days_in_regime")
        except Exception:
            pass
        clean = _clean(out)
        # 写内存 TTL + 落盘快照（2026-10-10）。**只在确实拿到行情时**落盘——空/占位不写，
        # 否则冷启动会把"空指数板"固化成快照，下次开面板秒显一个假的空态（安全契约第 3 条）。
        real = [i for i in (clean.get("indices") or []) if i.get("price")]
        if real:
            Api._indices_cache["payload"] = (_time_mod.time(), clean)
            _snapshot_write("indices", clean)
        return clean

    # ---------- 东财特殊标的报价（平均股价等；2026-09-28） ----------
    def _em_last_price(self, secid: str):
        """东财标的实时价 → (price, pre_close, src)；取不到返回 (None, None, None)。

        push2delay/push2his 均有**间歇风控**（仓库既有注释：重试 8 次多数能成），
        故两条路都重试。全部失败时返回 None，由调用方决定是否跳过该卡片。
        退到 K 线时**只在能确定昨收时才给 pre_close**——宁可不显示涨跌幅，也不显示错的。
        """
        import os as _os
        import time as _t
        import urllib.request as _ur
        # 短缓存：本函数在 10s 轮询里被调，若东财持续风控，
        # 每次都跑「4 次实时重试 + 2×8 次 K 线重试」会把界面拖死。
        # 成功缓存 120s，**失败也缓存 60s**（负缓存）——否则东财一挂，每轮都白等几秒。
        # 代价只是 平均股价 最多滞后 2 分钟（情绪展示指标，不影响交易）。
        _cache = Api._em_cache
        _hit = _cache.get(secid)
        if _hit:
            _ttl = 120 if _hit[1] else 60
            if (_t.time() - _hit[0]) < _ttl:
                return _hit[1], _hit[2], _hit[3]
        for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"]:
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"
        # 1) 实时（带重试）——2026-10-07：重试 4→2、超时 5→3s（东财风控时原设置首拉要 ~14s，冻界面）
        for _ in range(2):
            try:
                url = (f"https://push2delay.eastmoney.com/api/qt/stock/get?"
                       f"secid={secid}&fields=f43,f44,f45,f57,f58")
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://quote.eastmoney.com/"})
                ed = (json.loads(_ur.urlopen(req, timeout=3).read()
                                 .decode("utf-8", errors="ignore")).get("data") or {})
                px, pc = (ed.get("f43") or 0) / 100.0, (ed.get("f44") or 0) / 100.0
                if px and pc:
                    _cache[secid] = (_t.time(), px, pc, "em")
                    return px, pc, "em"
            except Exception:
                _t.sleep(0.4)
        # 2) 退到 K 线（analysis.index_divergence._em_bars 自带 8 次重试）
        try:
            from analysis.index_divergence import _em_bars
            bars = _em_bars("em" + str(secid), "30min")
            if bars is not None and not bars.empty:
                px = float(bars["close"].iloc[-1])
                pc = None
                daily = _em_bars("em" + str(secid), "日线")
                today = datetime.now().strftime("%Y-%m-%d")
                if daily is not None and not daily.empty and len(daily) >= 2:
                    # 仅当日线最新一根是今天时，才能确定昨收 = 倒数第二根
                    # （不引 pandas：t_gui 只在函数内局部 import pd）
                    if str(daily["time"].iloc[-1])[:10] == today:
                        pc = float(daily["close"].iloc[-2])
                if pc:
                    _cache[secid] = (_t.time(), px, pc, "em-kline")
                return px, pc, "em-kline"
        except Exception:
            pass
        _cache[secid] = (_t.time(), None, None, None)      # 负缓存，见函数开头注释
        return None, None, None

    def _em_last_price_bounded(self, secid, timeout=1.0):
        """`_em_last_price` 的**非阻塞**取用（2026-10-10 改造，原为带硬超时的抛弃池）。

        原实现每次调用新建一个 `max_workers=1` 池并 `shutdown(wait=False)`——冷启动时指数板里
        每个东财条目（平均股价等）都起一个抛弃池，池里的 worker 跑完「2×重试 + 8 次 K 线兜底」
        的结果**被丢弃**，只是白烧时间和线程。
        现在：命中 `Api._em_cache`（成功 120s / 失败负缓存 60s）就直接返回；未命中则**单飞**踢一个
        后台线程去补，本次先返回占位——下一轮 10s 轮询即命中缓存。慢路径一个字节都不浪费。
        """
        c = str(secid)
        hit = Api._em_cache.get(c)
        if hit:
            ttl = 120 if hit[1] else 60
            if (_time_mod.time() - hit[0]) < ttl:
                return hit[1], hit[2], hit[3]
        _kick_overview_refresh(f"em_price_{c}", lambda: self._em_last_price(c))
        return None, None, None

    # ---------- 指数背离（2026-09-28） ----------
    _div_cache = {}          # {"t": ts, "v": res}——见 load_index_divergence 的 TTL 说明
    _DIV_TTL = 180           # 秒

    def load_index_divergence(self):
        """指数板各指数 30min/60min/日线的顶/底背离（**观察提示，非交易信号**）。

        检测核复用 analysis.divergence；证据分级与免责说明随每条返回。
        同一事件按 key 去重（前端与飞书共用同一 key）。

        ⚠️ **必须带 TTL 缓存**：一次检测要打 12+ 次 GM 调用（7 指数 × 分时/日线），
        而前端把它挂在 **10s** 的实时轮询上。无缓存时若 GM 变慢，每轮都要等十几秒，
        请求堆积会把 pywebview 拖到窗口释放（2026-09-28 实测：GM 超时期间日志出现
        `index_minute ... 超时>12s` + `[pywebview] Error` + ObjectDisposedException）。
        背离是低频事件，180s 粒度足够（后端 main.py 的推送钩子本身也是 300s 节奏）。
        """
        import time as _t
        now = _t.time()
        hit = Api._div_cache
        if hit.get("v") is not None and (now - hit.get("t", 0)) < Api._DIV_TTL:
            return hit["v"]
        # 2026-10-07 SWR：冷/过期时**后台算 + 立即返回**（旧值或 warming），不再在主线程等 ~6s
        # （一次检测 12+ 次 GM 调用）。前端挂在 60s 的 loadAndRender 上 ⇒ 后台算完下一轮即出。
        if not getattr(self, "_div_running", False):
            self._div_running = True

            def _bg():
                try:
                    from analysis.index_divergence import detect_index_divergence
                    res = _clean(detect_index_divergence())
                except Exception as e:
                    res = _clean({"alerts": [], "watching": [], "health": {},
                                  "error": f"{type(e).__name__}: {str(e)[:160]}"})
                Api._div_cache = {"t": _t.time(), "v": res}
                self._div_running = False
            _th.Thread(target=_bg, daemon=True).start()
        if hit.get("v") is not None:
            return hit["v"]
        return _clean({"alerts": [], "watching": [], "health": {}, "warming": True})

    # ---------- 持仓成本历史 ----------
    def load_cost_history(self):
        """读全部 holdings 快照 + 人工校准文件，按股按日聚合 cost。
        校准优先（src=人工校准），否则快照值（src=快照）。"""
        dates, stocks = [], {}
        calib = _load_json(STATE_DIR / "cost_calibration.json", {}).get("calibrations", {})
        for fp in sorted(STATE_DIR.glob("holdings_*.json")):
            if "holdings_daily" in fp.stem:
                continue  # 跳过 holdings_daily_* 文件（结构不同）
            d = fp.stem.replace("holdings_", "")
            # 2026-10-04 拆分：holdings_manual/auto.json 也匹配 holdings_* 通配，
            # 只接受日期名（YYYY-MM-DD）的快照文件，防新文件名被当成 bogus 日期污染成本图。
            if len(d) != 10 or d[4] != "-" or d[7] != "-":
                continue
            try:
                snap = json.loads(open(fp, encoding="utf-8").read())
            except Exception:
                continue
            if not snap:
                continue
            dates.append(d)
            calib_day = calib.get(d, {})
            for code, info in snap.items():
                if not isinstance(info, dict):
                    continue
                cost = info.get("cost")
                if cost is None:
                    continue
                src = "快照"
                if code in calib_day and calib_day[code] is not None:
                    cost = float(calib_day[code])
                    src = "人工校准"
                st = stocks.setdefault(code, {"name": info.get("name", code), "points": []})
                qty = info.get("qty", 0) or 0
                pc = info.get("pre_close") or 0
                pnl_amt = round((pc - float(cost)) * qty, 2) if (pc and qty) else None
                pnl_pct = round((pc / float(cost) - 1) * 100, 2) if (pc and float(cost)) else None
                st["points"].append({"date": d, "cost": float(cost), "src": src,
                                     "pnl_amt": pnl_amt, "pnl_pct": pnl_pct,
                                     "qty": qty, "pre_close": pc})

        # 今日有效成本（校准优先，否则当前 holdings）供预填
        today = datetime.now().strftime("%Y-%m-%d")
        cur = _load_json(HOLDINGS_MANUAL, {})
        effective = {}
        calib_today = calib.get(today, {})
        for code, info in cur.items():
            if not isinstance(info, dict):
                continue
            if code in calib_today and calib_today[code] is not None:
                effective[code] = {"cost": float(calib_today[code]), "src": "人工校准"}
            elif info.get("cost") is not None:
                effective[code] = {"cost": float(info["cost"]), "src": "快照"}
        return _clean({"dates": dates, "stocks": stocks,
                       "effective_today": effective,
                       "calibrated_dates": sorted(calib.keys())})

    def save_cost_calibration(self, date, costs):
        """保存人工校准成本（date → {code: cost}），原子写。"""
        fp = STATE_DIR / "cost_calibration.json"
        data = _load_json(fp, {})
        if not isinstance(data, dict) or "calibrations" not in data:
            data = {"version": 1, "calibrations": {}}
        calib = data.setdefault("calibrations", {})
        day = calib.setdefault(date, {})
        if isinstance(costs, dict):
            for code, cost in costs.items():
                try:
                    val = float(cost)
                except (TypeError, ValueError):
                    continue
                day[code] = val if val > 0 else None
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "updated_at": data["updated_at"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 实时行情（顶部行情条） ----------
    # 行情刷新：SWR（stale-while-revalidate）。实测 snapshot_auction(49 只) 约 230ms，
    # 前端每 10s 轮询一次，这 230ms 全卡在 pywebview 线程上（K线弹窗卡顿排查 2026-10-04）。
    # 改为：命中新鲜窗口直接返回；略旧则**立即返回旧值 + 后台线程刷新**；过旧才阻塞重拉。
    _QUOTES_SWR_FRESH = 8.0     # 新鲜期：直接返回，连后台刷新都不触发
    _QUOTES_SWR_MAX = 90.0      # 超过此年龄宁可阻塞也拉一次（防后台长期失败把价格冻住）

    def load_quotes(self):
        """腾讯实时行情（SWR 包装；首次调用仍为阻塞拉取）。"""
        c = getattr(self, "_quotes_cache", None)
        if c is not None:
            _ts, _val = c
            _age = _time_mod.time() - _ts
            if _age < self._QUOTES_SWR_FRESH:
                return _val
            if _age < self._QUOTES_SWR_MAX and not getattr(self, "_quotes_refreshing", False):
                self._quotes_refreshing = True

                def _bg():
                    try:
                        self._load_quotes_sync()
                    except Exception:
                        pass
                    finally:
                        self._quotes_refreshing = False

                _th.Thread(target=_bg, name="quotes-refresh", daemon=True).start()
                return _val
        return self._load_quotes_sync()

    def _load_quotes_sync(self):
        """阻塞拉取一次并写入缓存。"""
        _r = self._load_quotes_build()
        self._quotes_cache = (_time_mod.time(), _r)
        return _r

    # 单次批量实时快照的代码上限：只服务"当前渲染的行"（猎手展开页），**绝不整池**。
    # 2026-10-12 由 120 提到 400：前端已改为**把所有展开板块并成一次请求**，
    # 多个板块展开时并集会超 120 ⇒ 原来的截断会让"排在 120 之后"的股票**永远显示 ⏳**（静默）。
    # 400 覆盖 8~10 个板块 × 每板块 20~50 行的常见情形；服务端再分块（`snapshot_auction_many`）。
    _QUOTES_FOR_MAX = 400

    def load_quotes_for(self, codes):
        """一批代码的**实时快照** → {6位码: {price, change_pct, ts_date}}。2026-10-12 新增。

        为什么必须单独有：猎手面板的行是**扫描时**的快照 —— 价与涨跌幅都停在"跑扫描那一刻"
        （实测 300821 显示 10-09 收盘的 +20.01%，而当天实时只有 +5.83%）
        ⇒ 展示层需要一个能按需取**当下**值的入口。只给当前渲染页用（上限 `_QUOTES_FOR_MAX`）、
        **不缓存不落盘**、**永不抛**（失败回空 dict，前端保持"⏳"并重试）。
        """
        out = {}
        _t0 = _time_mod.time()
        try:
            want = []
            for c in (codes or []):
                b = str(c).split("_")[0].strip()
                if b and b not in want:
                    want.append(b)
            want = want[:self._QUOTES_FOR_MAX]
            if not want:
                return out
            from core.market_data.tencent_provider import TencentProvider
            # ⚠️ 必须走**分块**版：`snapshot_auction` 的 URL 一长就被腾讯**静默返空**
            #（实测 800 只过、1000 只返 0）。这里上限 400 本就在悬崖之下，但用分块版
            # 可以把这个上限当成纯业务约束、不必再惦记传输层的长度限制。
            snaps = TencentProvider().snapshot_auction_many(want) or {}
            for base, d in snaps.items():
                px = d.get("price") or 0
                if not px:
                    continue          # 取不到就不给值（别拿 0 冒充"平盘"）
                out[str(base)] = {"price": round(float(px), 3),
                                  "change_pct": round(float(d.get("pct") or 0.0), 2),
                                  # 市值（亿元）：猎手「市值」列用。取不到就是 None（前端显示 —），
                                  # 绝不填 0 冒充"零市值"。
                                  "mktcap": (round(float(d.get("mktcap") or 0.0), 1)
                                             if d.get("mktcap") else None),
                                  "float_mktcap": (round(float(d.get("float_mktcap") or 0.0), 1)
                                                   if d.get("float_mktcap") else None),
                                  "ts_date": d.get("ts_date")}
        except Exception as e:
            # ⚠️ **不许静默**（2026-10-12）：这条链断在哪一层，光看界面分不出来。
            # 写进与 jsblock/ping 同一个诊断通道 —— t_gui 没有日志处理器，`log.info` 落不到文件。
            self.report_client_diag("hunter_hq", (_time_mod.time() - _t0) * 1000,
                                    f"EXC {type(e).__name__}: {str(e)[:50]}")
            return {}
        # 每次都记一条：req=请求只数 / got=拿到几只。断点在后端还是前端，一眼可判。
        self.report_client_diag("hunter_hq", (_time_mod.time() - _t0) * 1000,
                                f"req={len(want)} got={len(out)}")
        # ⚠️ **契约必须与 `load_quotes` 一致**（都包在 `quotes` 键里）。
        # 2026-10-12 踩过：本方法原先返回**裸字典**，而前端照 `load_quotes` 的写法读 `r.quotes`
        # ⇒ 永远读到空 ⇒ 重试耗尽 ⇒ 整列永远 ⏳。诊断日志一次就定死了：
        # `hunter_hq req=47 got=47` 对上 `hunter_hq_js req=47 got=0 patched=2438`。
        return {"quotes": out, "count": len(out)}

    def load_order_flow(self, code, force=False, step=10):
        """5 / 10 分钟 K 线的「多单/空单」分布（2026-10-20 owner）。

        `step`：5 或 10（分钟），与 K 线弹窗当前周期对齐；两档各自缓存。
        **只在当天有分笔时才给得出数**：腾讯逐笔明细只有当天、无历史日期参数
        （`&d=`/`&date=` 实测全被忽略），所以历史桶靠 `core.order_flow` 每天落盘累积。
        慢：首次抓全天约 68 页 ≈ 1~2s（并行 keep-alive），之后走增量/缓存 0.3s 以内。
        **永不抛**（前端按 `ok` 分支显示原因，不把它当致命错误）。
        """
        try:
            from core.order_flow import load as _load_flow
            return _load_flow(code, force=bool(force), step=int(step or 10))
        except Exception as e:
            return {"ok": False, "code": str(code), "bars": [],
                    "note": f"{type(e).__name__}: {str(e)[:100]}"}

    def _load_quotes_build(self):
        """拉腾讯实时行情（持仓 + watchlist 候选股），失败回退 pre_close。"""
        cur = dict(_load_json(HOLDINGS_MANUAL, {}))
        # 合并 watchlist_buy 候选股（非持仓的也拉，供建仓表实时价）
        wl = _load_json(STATE_DIR / "watchlist_buy.json", {})
        for code, info in (wl.get("stocks", {}) or {}).items():
            if code not in cur and isinstance(info, dict):
                cur[code] = {"name": info.get("name", code), "qty": 0, "cost": None,
                             "pre_close": 0, "in_watchlist": True}
        # 2026-10-17：原先这里把**自动盘池**并进行情清单，只为「自动盘建仓信号扫描」表的
        # 「价」列服务；该表已删 ⇒ 一并移除（不再为它多拉一批行情）。
        out = {"source": "fallback", "ts": None, "quotes": []}
        if not cur:
            return out
        def _to_symbol(c):
            """600176→sh600176, 000988→sz000988。后缀 _B 等剥离。"""
            base = c.split("_")[0]
            return ("sh" + base if base[0] in "56" else "sz" + base)
        symbols = {}
        for code in cur:
            sym = _to_symbol(code)
            if sym not in symbols.values():  # 同一只股票（如 000988/000988_B）只请求一次
                symbols[code] = sym
        # P1-2 收敛：tencent_provider.snapshot_auction（快照/竞价专用保留腾讯）
        from core.market_data.tencent_provider import TencentProvider
        snaps = TencentProvider().snapshot_auction(list(symbols.keys()))
        # 2026-10-06: 保留批量快照（含 ts_date/open/high/low/vol_hand）供 K线「补当日 bar」复用，
        # 免去每只票单独打一次快照（假期/首帧实测 1.9s/只）。
        try:
            self._quotes_snaps = (_time_mod.time(), snaps or {})
        except Exception:
            pass

        ts = datetime.now().strftime("%H:%M:%S")
        out["ts"] = ts
        if not snaps:
            # 回退：用 pre_close
            out["source"] = "fallback"
            for code, info in cur.items():
                if not isinstance(info, dict):
                    continue
                pc = info.get("pre_close") or 0
                out["quotes"].append({
                    "code": code, "name": info.get("name", code),
                    "price": pc, "pre_close": pc, "change": 0.0, "change_pct": 0.0,
                    "mktcap": None, "float_mktcap": None,
                    "cost": info.get("cost"), "pnl_pct": None, "offline": True,
                    "qty": info.get("qty", 0), "base": info.get("base", 0),
                    "t_qty": info.get("t_qty", 0),
                })
            self._write_daily_holdings(out)
            return out

        out["source"] = "live"
        # 1) snapshot_auction → {base_code: {price,pre_close,change,change_pct}}
        px = {}
        for base, d in snaps.items():
            price = d.get("price") or 0
            pre_close = d.get("pre_close") or 0
            px[base] = {
                "price": price, "pre_close": pre_close,
                "change": round(price - pre_close, 3) if price and pre_close else 0.0,
                "change_pct": d.get("pct") or 0.0,
                # 2026-10-22：市值（亿元）——建仓表 / 持仓体检 / 风向标三张表的「市值」列都用它，
                # 与 `load_quotes_for` 同源（腾讯快照 f45=总市值、f44=流通市值）。
                "mktcap": (round(float(d.get("mktcap") or 0.0), 1) if d.get("mktcap") else None),
                "float_mktcap": (round(float(d.get("float_mktcap") or 0.0), 1)
                                 if d.get("float_mktcap") else None),
            }

        # 2) 按 holdings 条目驱动 → 每个条目用自己的 code/cost/qty，查同一个 base 的价格
        for code, info in cur.items():
            if not isinstance(info, dict):
                continue
            base = code.split("_")[0]    # 000988_B → 000988
            p = px.get(base)
            if p is None:
                continue
            cost = info.get("cost")
            pnl = (p["price"] / cost - 1) * 100 if cost else None
            out["quotes"].append({
                "code": code, "name": info.get("name", code),
                "price": p["price"], "pre_close": p["pre_close"],
                "change": p["change"], "change_pct": p["change_pct"],
                "mktcap": p.get("mktcap"), "float_mktcap": p.get("float_mktcap"),
                "cost": cost, "pnl_pct": pnl, "offline": False,
                "qty": info.get("qty", 0), "base": info.get("base", 0),
                "t_qty": info.get("t_qty", 0),
            })
        self._write_daily_holdings(out)
        return _clean(out)

    def save_daily_holdings(self):
        """写今日持仓每日快照（含数量/成本/盈亏）。走同步路径取**新鲜**报价，不用 SWR 旧值。"""
        return self._write_daily_holdings(self._load_quotes_sync())

    def _write_daily_holdings(self, q):
        """数量/成本读用户每天更新的 holdings.json，盈亏按盘中实时价计算。
        2026-09-27 净值法改造：summary 增加 cash/available/cash_source（现金口径
        = accounts_config 各启用账户 available 之和，取不到为 null + "missing"）；
        顶层增加 eod 标志（交易日 15:00 后写 = true，复盘只认 eod=true 的行）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        fp = STATE_DIR / f"holdings_daily_{today}.json"
        cur = _load_json(HOLDINGS_MANUAL, {})
        rows = []
        total_value = total_cost = total_pnl = 0.0
        for qq in q.get("quotes", []):
            code = qq.get("code")
            info = cur.get(code, {})
            if not isinstance(info, dict):
                continue
            qty = info.get("qty", 0)
            cost = info.get("cost")
            price = qq.get("price")
            pnl_amt = None
            if cost and price and qty:
                pnl_amt = round((price - cost) * qty, 2)
                total_value += price * qty
                total_cost += cost * qty
                total_pnl += (price - cost) * qty
            rows.append({
                "code": code, "name": qq.get("name", code),
                "account": _account_of(code), "type": info.get("type", ""),
                "qty": qty, "base": info.get("base", 0),
                "cost": cost, "pre_close": info.get("pre_close"),
                "price": price, "change_pct": qq.get("change_pct"),
                "pnl_pct": qq.get("pnl_pct"), "pnl_amt": pnl_amt,
                "offline": bool(qq.get("offline")),
            })
        summary = {
            "total_value": round(total_value, 2) if total_value else None,
            "total_cost": round(total_cost, 2) if total_cost else None,
            "total_pnl": round(total_pnl, 2) if total_pnl else None,
            "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost else None,
        }
        # 现金落盘（净值法 equity = 持仓市值 + 现金）：accounts_config 最新 reconcile
        cash, cash_source = _reconcile_cash()
        summary["cash"] = cash
        summary["available"] = cash
        summary["cash_source"] = cash_source
        # EOD 定值：交易日 15:00 后快照价格即收盘价，标 eod=true；盘中/周末 eod=false。
        # 交易日按 weekday<5 近似（沿用仓库既有口径，法定节假日会误判为 eod=true）。
        now = datetime.now()
        is_eod = now.weekday() < 5 and now.strftime("%H:%M") >= "15:00"
        data = {
            "date": today,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "source": q.get("source", "fallback"),
            "eod": is_eod,
            "holdings": rows,
            "summary": summary,
        }
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(_clean(data), f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "path": str(fp)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 实时 console ----------
    KEY_LINE_WORDS = ["推送", "信号得分", "拦截", "阻断", "熔断", "告警", "建仓",
                      "策略卡", "竞价", "接回", "WARNING", "ERROR", "异常",
                      "仓控", "急跌", "追涨", "成交", "闭环", "已推送",
                      "大盘", "评分=", "进攻", "防守", "触发",
                      "已卖", "已买", "未接回", "启动自检"]
    NOISE_LINE_WORDS = ["扫描心跳", "缓存", "数据更新完成", "poll", "网络重试",
                        "本轮耗时", "等待", "进入下一轮", "非交易时段", "低频保活"]

    def load_console(self, date, since=0):
        """增量读日志。since=字节偏移，返回新增行+新偏移。"""
        _GUI_HB["ts"] = _time_mod.time()   # 前端 2s 轮询打点：看门狗据此判界面是否卡死
        fp = LOGS_DIR / f"t_trader_sys_{date}.log"
        out = {"lines": [], "offset": since, "exists": fp.exists(), "eof": True}
        if not fp.exists():
            return out
        try:
            size = fp.stat().st_size
            if size < since:
                since = 0  # 日志轮转/重建
            with open(fp, encoding="utf-8", errors="replace") as f:
                f.seek(since)
                data = f.read()
            out["offset"] = size
            out["eof"] = size == 0 or data == "" or data.endswith("\n")
            for line in data.splitlines():
                if not line.strip():
                    continue
                out["lines"].append(self._parse_log_line(line))
        except Exception:
            pass
        return out

    def _ensure_daily_ctx_indicators(self, code, daily_ctx):
        """补算 daily_ctx 的日线 MACD/趋势字段（旧快照缺失时）。就地更新 daily_ctx。"""
        try:
            import pandas as pd
            _c6 = str(code).split("_")[0]
            # 2026-10-09: **本地日线缓存优先**——原来逐只 `fetch_daily_kline` 走 GM（实测 ~1.8s/只），
            # 在 `load_day`（主线程）上跑 9 只 ⇒ 17.3s 冻界面（看门狗抓到）。
            df = None
            try:
                import core.chart_cache as _cc
                df = _cc.load_daily_display(_c6)
            except Exception:
                df = None
            if df is None or df.empty:
                from core.position_builder import fetch_daily_kline
                df = fetch_daily_kline(_c6)
            if df.empty or len(df) < 30:
                return
            c = df["close"].astype(float)
            ema12 = c.ewm(span=12, adjust=False).mean()
            ema26 = c.ewm(span=26, adjust=False).mean()
            macd_dif = (ema12 - ema26).values
            macd_dea = pd.Series(macd_dif).ewm(span=9, adjust=False).mean().values
            s_dif, s_dea = pd.Series(macd_dif), pd.Series(macd_dea)
            cross_up = (s_dif > s_dea) & (s_dif.shift(1) <= s_dea.shift(1))
            daily_ctx["daily_macd_dif"] = float(macd_dif[-1])
            daily_ctx["daily_macd_dea"] = float(macd_dea[-1])
            daily_ctx["daily_macd_golden"] = bool(cross_up.tail(5).any())
            # 趋势背景：用 MA 排列粗略推断（上行/下行/震荡）
            ma5 = float(c.rolling(5).mean().iloc[-1])
            ma20 = float(c.rolling(20).mean().iloc[-1])
            ma60 = float(c.rolling(60).mean().iloc[-1]) if len(c) >= 60 else ma20
            cur_px = float(c.iloc[-1])
            if not daily_ctx.get("daily_trend_bg"):
                if cur_px < ma60 and ma20 <= ma60:
                    daily_ctx["daily_trend_bg"] = "downtrend"
                elif ma5 > ma20 > ma60 and cur_px >= ma5:
                    daily_ctx["daily_trend_bg"] = "uptrend"
                else:
                    daily_ctx["daily_trend_bg"] = "range"
        except Exception:
            pass

    # ---------- 突破箱体判定（建仓+加仓共用） ----------
    def check_box_breakout(self, code):
        """判定是否突破当前/刚突破箱体上沿 + 分级突破质量。
        返回 {broken, level, box, price, pct_above, confidence}。

        突破等级（level）：
          - signal: 信号级(0.5-1%)，敏感但低可靠，仅提示
          - reliable: 可靠突破(1-3%)，需辅助确认，可作参考
          - strong: 强势突破(3%+)，高概率后续，适合加仓
          - far_away: 已远离>8%，看不出是否有效
        """
        # 2026-10-09: 本函数只用到 `boxes` 与 `current_price`（**都是日线派生**）——改读**本地日线缓存**
        # + `_detect_boxes`，不再走 `load_stock_chart` 全量建图（含分钟/指标；盘中每 2 分钟重建，
        # 9 只持仓 ⇒ `load_day` 主线程阻塞 17s，看门狗抓到）。缓存 miss 才回退原路径。
        boxes, _h_price = [], None
        try:
            import core.chart_cache as _cc
            _dfd = _cc.load_daily_display(str(code).split("_")[0])
            if _dfd is not None and not _dfd.empty:
                boxes = self._detect_boxes(_dfd)
                _h_price = float(_dfd["close"].iloc[-1])
        except Exception:
            boxes = []
        if not boxes:
            h = self.load_stock_chart(code)
            if not h.get("available"):
                return {"broken": False, "level": None, "error": h.get("error", "")}
            boxes = h.get("boxes", [])
            _h_price = h.get("current_price")
        # fix P0-4: 现价改用 load_quotes 实时报价（30秒缓存避免逐股重复拉网），失败回退日线收盘
        now = datetime.now()
        qc = getattr(self, "_box_quote_cache", None)
        if not qc or (now - qc[0]).total_seconds() > 30:
            px_map = {}
            try:
                for qq in self.load_quotes().get("quotes", []):
                    if qq.get("price") and not qq.get("offline"):
                        px_map[qq.get("code")] = float(qq["price"])
            except Exception:
                px_map = {}
            qc = (now, px_map)
            self._box_quote_cache = qc
        cur = qc[1].get(code) or qc[1].get(code.split("_")[0]) or _h_price
        if not cur:
            return {"broken": False, "level": None, "error": "无可用现价"}
        # fix P0-4: 候选箱体纳入 rel==1（刚突破）；rel 判定基于日线收盘，与实时现价解耦
        cur_boxes = [b for b in boxes if b.get("rel") in (0, 1)]
        # 现价 > 候选箱体上沿 → 判定突破级别
        for box in cur_boxes:
            if cur > box["high"]:
                pct_above = (cur - box["high"]) / box["high"] * 100 if box["high"] else 0
                # 根据突破幅度判定级别（box宽度作为质量权重）
                box_width_pct = (box["high"] - box["low"]) / box["low"] * 100 if box["low"] else 0
                # 宽度作为确信度：宽箱体(>10%)突破容差可更松，窄箱体(<5%)必须严格
                confidence = min(100, max(10, box_width_pct))  # confidence: 10~100

                if pct_above <= 0.5:
                    level = None  # 未达突破阈值
                elif pct_above <= 1:
                    level = "signal"  # 信号级：敏感但易误报
                elif pct_above <= 3:
                    level = "reliable"  # 可靠突破：推荐用于加仓参考
                elif pct_above <= 8:
                    level = "strong"  # 强势突破：已形成上升趋势
                else:
                    level = "far_away"  # 已远离，无法判定是否有效

                if level:
                    return {"broken": True, "level": level, "box": {"low": box["low"], "high": box["high"]},
                            "price": cur, "pct_above": round(pct_above, 2), "confidence": round(confidence, 0)}
                else:
                    return {"broken": False, "level": None, "price": cur,
                            "near_box": {"low": box["low"], "high": box["high"]},
                            "pct_above": round(pct_above, 2),
                            "reason": "未达突破阈值(仅0.5%以下)"}
        # 无候选箱体或现价在箱体内 → 未突破
        return {"broken": False, "level": None, "price": cur}

    # ---------- 持仓日线超买/顶背离体检 ----------
    _OB_TTL = 45.0          # 体检结果缓存新鲜期（秒）
    _BEACON_TTL = 120.0      # 风向标「重列」缓存新鲜期（秒）；未热时前端短轮询（见 load_market_beacon）
    _BEACON_SKELETON_TTL = 90.0   # 骨架缓存新鲜期（秒）；过期后台重算，先给旧值

    def load_ob_analysis(self, date=None):
        """持仓体检（SWR 包装，2026-10-10）：逐只算 KDJ/CCI/标签/30min 判定是**纯 Python 重活**
        （9 只 ~3~11s，叠加预热线程竞争更久），同步跑会冻 pywebview 主线程（看门狗实测 9~11s）。

        改为：命中缓存直返；冷启动/过期起**后台算 + 立即返回**（冷启动返回 `pending`，前端已有
        重试补拉）。判定口径见 `_load_ob_analysis_impl`。

        2026-10-13：返缓存前**就地覆盖易变字段**（`_ob_overlay_live`：structure/price 都是纯内存
        读，µs 级）——否则 45s TTL 会把"后台已预热好"的结构列继续钉成 ⏳、把现价钉旧；
        struct/m30 pending 也改用短 TTL，让 30min 判定及时重算。"""
        now = _time_mod.time()
        _c = getattr(self, "_ob_cache", None)
        if _c is not None:
            self._ob_overlay_live(_c[1])
            # 结果不完整（图表未热 / 结构层未热 / 30min 判定未热）时用短 TTL：前端重试即重算；
            # 完整则 45s 内直返（结构列/现价由上面的覆盖保持新鲜，不受 TTL 钉住）。
            _stale = _c[1].get("pending") or _c[1].get("struct_pending") or _c[1].get("m30_pending")
            _ttl = 10.0 if _stale else self._OB_TTL
            if (now - _c[0]) < _ttl:
                return _c[1]
        if not getattr(self, "_ob_running", False):
            self._ob_running = True

            def _bg():
                try:
                    _r = self._load_ob_analysis_impl(date)
                    self._ob_cache = (_time_mod.time(), _r)
                except Exception:
                    pass
                finally:
                    self._ob_running = False
            _th.Thread(target=_bg, name="ob-analysis", daemon=True).start()
        if _c is not None:
            self._ob_overlay_live(_c[1])
            return _c[1]
        return {"stocks": [], "pending": True}

    def _ob_overlay_live(self, out):
        """返缓存前就地覆盖每行的 structure/price（**全部纯内存读、零网络零重算**）。

        structure：`_pb_struct_get` 只读内存缓存——后台预热一好，下一轮 3s 重试立即显示，
        不等 45s TTL 过期重算；未热则 kick 预热队列。price：`_today_forming_bar` 读
        `load_quotes` 每 10s 的快照缓存，现价随调用即新。"""
        try:
            stocks = (out or {}).get("stocks") or []
        except Exception:
            return
        # 每轮重算「均线突破」的 pending 标记：否则一旦某轮挂上 True 就**永远粘住**，
        # 前端会一直按 3s 重拉（实测：算完之后 OB 表仍在 3s 轮询，白拉一整天）。
        try:
            out.pop("ma_reclaim_pending", None)
        except Exception:
            pass
        if not stocks:
            return
        _struct_pending = False
        for s in stocks:
            if not isinstance(s, dict):
                continue
            _base = str(s.get("code") or "").split("_")[0]
            if not _base:
                continue
            try:
                _st = _clean(self._pb_struct_get(_base))   # 缓存在 _clean 之后注入 ⇒ 这里补清洗
                s["structure"] = _st
                if _st.get("pending"):
                    _struct_pending = True
            except Exception:
                pass
            try:
                _live = self._today_forming_bar(_base, allow_network=False)
                if _live and _live.get("close"):
                    s["price"] = float(_live["close"])
            except Exception:
                pass
            try:                                  # 当日均线回站 + 突破时刻（只读缓存，µs 级）
                s["ma_reclaim"] = self._ma_reclaim_get(_base, kick=False)
            except Exception:
                s["ma_reclaim"] = {"pending": True}
            if isinstance(s.get("ma_reclaim"), dict) and s["ma_reclaim"].get("pending"):
                out["ma_reclaim_pending"] = True
        if _struct_pending:
            out["struct_pending"] = True
            try:
                self._kick_pb_struct_warm([s.get("code") for s in stocks if isinstance(s, dict)])
            except Exception:
                pass
        else:
            out.pop("struct_pending", None)
        if out.get("ma_reclaim_pending"):
            try:
                self._kick_ma_reclaim_warm([s.get("code") for s in stocks if isinstance(s, dict)])
            except Exception:
                pass
        else:
            out.pop("ma_reclaim_pending", None)

    def _kick_cold_chart_warm(self, codes):
        """给**图表不在内存缓存**的持仓补一次后台取数（2026-10-12）。

        为什么必须有：`_load_ob_analysis_impl` 只读 `self._stock_chart_cache`、**从不补取**，
        所以 `prewarm_holdings_charts` 一旦漏掉某只（启动预热失败 / 新增持仓 / 重启后未预热完），
        那只就会**永远不出现**在持仓体检表里 —— 界面上只表现为"少了一行"，
        owner 实测报「少了江西铜业(600362)」，而数据侧（holdings_manual / holdings_daily /
        load_quotes / load_stock_chart）**全都在**，查起来毫无线索。

        ⚠️ **必须节流**：OB 在 `pending>0` 期间 TTL 只有 10s，会反复重算 ⇒ 不节流就会反复起线程池
        （本仓被"线程数爆炸"咬过，见 memory `gui_cold_start_burst`）。这里 30s 内至多一次，
        且只对**缺的那几只**取数（不重复整池预热），并发压到 2。
        """
        now = _time_mod.time()
        if now - getattr(self, "_cold_warm_at", 0.0) < 30:
            return
        self._cold_warm_at = now
        codes = [c for c in dict.fromkeys(codes or []) if c]
        if not codes:
            return

        def _bg():
            try:
                from concurrent.futures import ThreadPoolExecutor

                def _one(c):
                    try:
                        self.load_stock_chart(c)      # 当日内存缓存命中则秒回
                    except Exception:
                        pass
                with ThreadPoolExecutor(max_workers=2) as ex:
                    list(ex.map(_one, codes))
            except Exception:
                pass
        _th.Thread(target=_bg, name="ob-cold-warm", daemon=True).start()

    def _load_ob_analysis_impl(self, date=None):
        """每只持仓：**30min 趋势 / 30min 背离 / 30min 顶部特征(T1–T4)风险提醒** + 日线超买(RSI/KDJ-J/CCI/BOLL)。

        2026-10-09 口径变更（owner 需求）：趋势/判定改用 **30 分钟线**。前端只显示一列
        **「30min 判定」**（`verdict`，由 `m30_features.verdict_from_features` 综合趋势+顶部/底背离特征）。
        关键：只有 **T1顶背离/T2量价背离** 触发「减仓」（981只×540日离线验证 H=4 跌占比 +29/+32pp）；
        T3顶分型/T4均线压制 **无区分度** ⇒ 不单独报警。数据来自 `_m30_top_snapshot`（**只读缓存、零网络**）。

        2026-10-04 卡顿修复：原来逐只 `load_stock_chart`（冷取数）⇒ 冷启动实测 **14.3s** 同步阻塞
        在 pywebview 主线程。改为**只用已预热的图表缓存**：未就绪的持仓本轮跳过、计入 `pending`，
        由前端稍后重拉（启动时 `prewarm_holdings_charts` 已在后台填缓存）。UI 不再被冻住。
        """
        import numpy as np
        import pandas as pd
        cur = _load_json(HOLDINGS_MANUAL, {})
        out = {"stocks": []}
        _today = datetime.now().strftime("%Y-%m-%d")
        _chart_cache = getattr(self, "_stock_chart_cache", {})
        pending = 0
        m30_pending = False          # 有持仓的 30min 判定尚未热（前端稍后重拉，不等 12s）
        _m30_cold = []               # 当前 slot 未热的持仓 ⇒ 本轮结束时补一次**磁盘**预热（零网络）
        _cold = []                   # 图表不在内存缓存 ⇒ 本轮跳过（下面兜底补取，见 _kick_cold_chart_warm）

        for code, info in cur.items():
            if not isinstance(info, dict) or code.startswith("_"):
                continue
            if not (info.get("qty") or 0):
                continue  # fix 2026-08-20: 已清仓(qty=0)不进入持仓体检
            base_code = code.split("_")[0]  # 000988_B → 000988
            _hit = _chart_cache.get(f"{_today}_{base_code}")
            if _hit is None:
                pending += 1          # 图表尚未预热好：跳过，不计入本轮（前端会重拉）
                _cold.append(base_code)   # ⚠️ 记下来：本路径**只读缓存、从不补取**，
                continue                  #    预热漏过一次就永远不出现（2026-10-12 见下）
            h = _hit[1]
            if not h.get("available"):
                out["stocks"].append({"code": code, "name": info.get("name", code),
                                      "error": h.get("error", "无数据")})
                continue
            d = h["period_data"]["daily"]
            # 30min 快照（2026-10-09）：趋势/判定改 30 分钟口径。**主线程只读缓存、绝不算**（重算 ~300ms/只
            # 会冻界面）；未热 → 本行判定显示「计算中」，由后台 `_warm_ob_m30_disk`/`refresh_ob_m30` 填充。
            _m30 = self._m30_top_snapshot(base_code) if _M30_OB_ENABLED else {}
            _m30_warm = _m30 is not None
            if _M30_OB_ENABLED and not _m30_warm:
                _m30_cold.append(base_code)
            _m30 = _m30 or {}
            _t30 = _m30.get("t30") or {}
            _feats = _m30.get("feats") or {}
            # 技术标签（2026-10-07）：与建仓表同口径（复用 `_stock_tags_from_df`）。用已缓存的
            # 日线 payload 重建 df，**零额外网络**；标签口径/顺序与建仓表完全一致。
            _live_close = None
            try:
                import pandas as _pd
                _tdf = _pd.DataFrame({
                    "date": _pd.to_datetime(d["dates"]),
                    "open": [x[0] for x in d["ohlc"]], "close": [x[1] for x in d["ohlc"]],
                    "low": [x[2] for x in d["ohlc"]], "high": [x[3] for x in d["ohlc"]],
                    "volume": [float(v) for v in d["volume"]],
                })
                # 2026-10-09: 图表**内存缓存盘中最多 15 分钟旧** ⇒ 用**实时快照**（load_quotes 每 10s 的
                # 批量快照）校正当日那根，否则标签/现价会停在旧价（实测 600362 价已回 MA5 上方、
                # 标签仍显示 15 分钟前的「破5日线」）。零额外网络；非当日/无快照则原样。
                _live = self._today_forming_bar(base_code, allow_network=False)
                if _live and str(_live["date"]) == str(_tdf["date"].iloc[-1])[:10]:
                    _li = _tdf.index[-1]
                    _tdf.at[_li, "close"] = float(_live["close"])
                    _tdf.at[_li, "high"] = max(float(_tdf.at[_li, "high"]), float(_live["high"]))
                    _tdf.at[_li, "low"] = min(float(_tdf.at[_li, "low"]), float(_live["low"]))
                    _live_close = float(_live["close"])
                _tagres = self._stock_tags_from_df(
                    _tdf, base_code,
                    t30=(_t30 if _t30.get("trend") else False))
                _tags = _tagres.get("tags", [])
                # 20 日线观察（2026-10-14 owner 需求：体检表补「是否站上20日线」列，
                # 与建仓表/猎手表同口径）。口径 = 收盘价 > 20 日简单均线。
                # ⚠️ 必须先剔**尾部假当日根**（盘前快照造的 price=开盘价/vol=0 占位根）再算——
                # 与 `_stock_tags_from_df` 同一处自愈逻辑，否则那根假 bar 会把 MA20 和参照收盘
                # 一起带偏（2026-10-11 实测 600362 就是这么误报「破5日线」的）。
                # 现价优先用实时快照校正后的 `_live_close`（同现价列），未校正则用末根收盘。
                _d20 = _tdf
                while len(_d20) > 0:
                    _l20 = _d20.iloc[-1]
                    if (float(_l20.get("volume") or 0) <= 0
                            and float(_l20.get("high") or 0) == float(_l20.get("low") or 0)):
                        _d20 = _d20.iloc[:-1]
                    else:
                        break
                if len(_d20) >= 20:
                    _c20 = _pd.to_numeric(_d20["close"], errors="coerce").tail(20)
                    _m20raw = float(_c20.mean()) if _c20.notna().all() else float("nan")
                    _r20 = float(_live_close) if _live_close else float(_d20["close"].iloc[-1])
                    # 非有限值一律当「数据不足」→ None（前端显示「—」）。
                    # **绝不**让 NaN 比较落成 False：那会在界面上变成「破位」这个断言。
                    if _m20raw == _m20raw and _r20 == _r20 and _m20raw > 0:   # NaN != NaN
                        _above_ma20 = bool(_r20 > _m20raw)
                        _ma20_d = round(_m20raw, 3)
                    else:
                        _ma20_d, _above_ma20 = None, None
                else:
                    _ma20_d, _above_ma20 = None, None      # 不足 20 根 → 前端显示「—」，不猜
            except Exception:
                _tags = []
                _ma20_d, _above_ma20 = None, None
            closes = [x[1] for x in d["ohlc"]]
            highs = [x[3] for x in d["ohlc"]]
            lows = [x[2] for x in d["ohlc"]]
            rsi = d["rsi"]
            boll_up = d["boll"]["up"]
            n = len(closes)
            if n < 30:
                # ⚠️ 原来这里**直接 continue**：不加分析、也**不留行** ⇒ 界面上"少了一只"，
                # 没有任何线索（owner 2026-10-12 报「持仓体检少了江西铜业」就是这么消失的：
                # 日线缓存被短窗口覆盖成 21 根 → payload 存 21 根 → 这里静默丢）。
                # 改为**留一行并写明原因**（与上面 `available=False` 分支同款，前端可渲染）。
                out["stocks"].append({"code": code, "name": info.get("name", code),
                                      "error": f"日线不足({n}根，需≥30) —— 缓存可能被短窗口写入截短"})
                continue

            # KDJ(9,3,3)
            k_arr, d_arr, j_arr = [50.0], [50.0], [50.0]
            for i in range(1, n):
                hh = max(highs[max(0, i - 8):i + 1])
                ll = min(lows[max(0, i - 8):i + 1])
                rsv = (closes[i] - ll) / (hh - ll) * 100 if hh != ll else 50
                k = 2 / 3 * k_arr[-1] + 1 / 3 * rsv
                dd = 2 / 3 * d_arr[-1] + 1 / 3 * k
                k_arr.append(k); d_arr.append(dd); j_arr.append(3 * k - 2 * dd)

            # CCI(14)
            cci_arr = []
            for i in range(n):
                if i < 13:
                    cci_arr.append(None); continue
                tp = (highs[i] + lows[i] + closes[i]) / 3
                ma_tp = sum((highs[j] + lows[j] + closes[j]) / 3
                            for j in range(i - 13, i + 1)) / 14
                md = sum(abs((highs[j] + lows[j] + closes[j]) / 3 - ma_tp)
                         for j in range(i - 13, i + 1)) / 14
                cci_arr.append((tp - ma_tp) / (0.015 * md) if md else 0)

            # 当前超买状态
            cur_rsi = rsi[-1] if rsi and rsi[-1] is not None else 0
            cur_j = j_arr[-1]
            cur_cci = cci_arr[-1] or 0
            cur_close = _live_close if _live_close else closes[-1]   # 现价优先实时快照（见上方校正）
            cur_boll = boll_up[-1] if boll_up and boll_up[-1] is not None else 0
            ob = {
                "rsi": bool(cur_rsi > 70),
                "kdj": bool(cur_j > 100),
                "cci": bool(cur_cci > 100),
                "boll": bool(cur_boll and cur_close > cur_boll),
            }
            ob["count"] = sum(1 for v in ob.values() if v)

            # 背离（2026-10-09）：改 **30 分钟** MACD 背离（已验证口径 detect_divergence_events
            # + 新鲜度 MAX_AGE_BARS['30min']）；无 30min 数据 → 置空（不冒充日线结论）。
            _dve = (_m30 or {}).get("div") or {}
            div = {"type": _dve.get("type"), "consec": bool(_dve.get("consec")),
                   "bars_ago": _dve.get("bars_ago"), "time": _dve.get("time"),
                   "count": 1 if _dve.get("type") else 0}

            # 趋势方向（2026-10-09）：改 **30 分钟三层状态机趋势**（与行内技术标签同源，
            # 二者从此一致）；无 30min 数据 → 回退日线通道方向（保持旧行为）。
            ch = (h.get("channel") or {})
            if _t30.get("trend"):
                trend, trend_src = _t30["trend"], "30min"
            else:
                trend, trend_src = ch.get("direction", "flat"), "daily"

            # 30min 综合判定（2026-10-09 定稿口径）：**「30min 判定」列的唯一来源**。
            # 依据 t_io/validation/m30_top/报告_m30_top.md：只有 T1顶背离/T2量价背离 前瞻强
            # （z≈97/88），T3顶分型/T4均线压制 无区分度 ⇒ 只有 T1/T2 触发「减仓」，T3/T4 不单独报警。
            from analysis import m30_features as _m30f
            if not _M30_OB_ENABLED:
                _vlevel, _vlabel, _vreason = "na", "—", ""
            elif not _m30_warm:
                _vlevel, _vlabel, _vreason = "pending", "⏳ 计算中", "30min 判定后台计算中…（稍后自动刷新）"
                m30_pending = True
            else:
                _vlevel, _vlabel, _vreason = _m30f.verdict_from_features(
                    _feats, trend, div.get("type"), div.get("bars_ago"))
            # risk/advice 保留（回滚路径 `_M30_OB_ENABLED=False` 仍用旧日线 RSI 口径）。
            if _M30_OB_ENABLED:
                risk, advice = _m30f.risk_from_features(_feats)
            else:                                   # 回滚：日线 RSI 口径（2026-09-11 校准）
                _td = ch.get("direction")
                if cur_rsi > 80:
                    risk, advice = "高", "🚨 RSI极度超买(>80)：实证 T+5 转平、56% 下跌，注意回落/减仓"
                elif cur_rsi > 70 and _td == "down":
                    risk, advice = "高", "🚨 RSI超买+趋势下行：回落风险高，反弹减仓/回避"
                elif cur_rsi > 70:
                    risk, advice = "中", "⚠ RSI超买(>70)：短线偏热，注意回调（实证跌占52%>基线45%）"
                elif _td == "down":
                    risk, advice = "中", "⚠ 趋势下行：不追高，反弹减仓"
                else:
                    risk, advice = "低", "✓ 指标中性（KDJ/CCI/BOLL 偏强属动能）：持有/关注"

            out["stocks"].append({
                "code": code, "name": info.get("name", code),
                "price": cur_close,
                "trend": trend,
                "trend_src": trend_src,
                "verdict": {"level": _vlevel, "label": _vlabel, "reason": _vreason},
                "risk": risk,
                "overbought": {"rsi": round(cur_rsi, 1), "kdj": round(cur_j, 1),
                               "cci": round(cur_cci, 1), "boll": bool(ob["boll"]), "count": ob["count"]},
                "divergence": div,
                "top_features": _feats,
                "bar_time": (_m30 or {}).get("bar_time"),
                "advice": advice,
                "tags": _tags,
                # 偏离 365 日线（2026-10-23 owner：把「依据」列换成它）
                "dev365": _tagres.get("dev365"),
                # 20 日线观察（2026-10-14）：`above_ma20` True/False/None(数据不足)，
                # `ma20_daily` 供 tooltip。**观察列、不驱动任何判定**（前端 ma20FlagCellHtml 同款）。
                "above_ma20": _above_ma20,
                "ma20_daily": _ma20_d,
                # 仓位结构（2026-10-11）：与建仓表**同一真源**（`_pb_struct_get` 只读缓存，
                # 主线程零计算零网络）。是**结构参考、不是买卖信号**（见 core/position_structure.py）。
                "structure": self._pb_struct_get(base_code),
            })
        if pending:
            out["pending"] = pending      # 未就绪的持仓数（>0 ⇒ 前端稍后重拉）
        if _cold:
            # 2026-10-12 owner 报「持仓体检少了江西铜业」：本路径**只读** `_stock_chart_cache`、
            # **从不补取** ⇒ 预热一旦漏掉某只（启动预热失败/新增持仓/重启），那只就**永远不出现**，
            # 且界面上只表现为"少了一行"，没有任何线索。⇒ 兜底：对它**补一次后台取数**
            # （带节流，见 `_kick_cold_chart_warm`），下一轮（≤10s，pending 期间 TTL 就是这么短）即出现。
            # 同时把 code 列出来，让"少了谁"可见。
            out["chart_pending"] = len(_cold)
            out["chart_pending_codes"] = list(_cold)
            try:
                self._kick_cold_chart_warm(_cold)
            except Exception:
                pass
        if m30_pending:
            out["m30_pending"] = True     # 30min 判定未热（>0 ⇒ 前端 3s 快速重拉，不等 12s）
            # 2026-10-18：当前 slot 未热时**按需**补一次磁盘预热（零网络、单飞）。
            # 在线刷新只在交易时段跑 ⇒ 周末/盘后没人翻新 slot，体检表会永远「计算中」。
            if _m30_cold:
                try:
                    self._kick_ob_m30_disk(_m30_cold)
                except Exception:
                    pass
        # 仓位结构未热：后台预热持仓（按 code 排队、槽位内去重），前端按 struct_pending 快速重拉
        _struct_pending = any((s.get("structure") or {}).get("pending") for s in out["stocks"])
        if _struct_pending:
            out["struct_pending"] = True
            try:
                self._kick_pb_struct_warm([s.get("code") for s in out["stocks"] if s.get("code")])
            except Exception:
                pass
        return _clean(out)

    # ---------- 入场三层评判（L1/L2/L3建议） ----------
    def load_entry_verdict(self, date=None):
        """候选股入场评判：L1追高风险 + L2缩量支撑 + L3日内共振 → 综合建议。

        返回格式:
        {
          "stocks": [
            {
              "code": "300058",
              "name": "蓝色光标",
              "market_regime": "range_up",
              "market_score": 60,
              "l1": {"status": "✅", "detail": "安全", "risk_score": 0, "threshold": 35},
              "l2": {"status": "✅", "detail": "缩量0.60x", "is_consolidating": True},
              "l3": {"status": "❌", "detail": "放量不足", "resonance": False},
              "verdict": "wait_resonance",
              "action": "等待日内共振",
              "expected_when": "盘中"
            },
            ...
          ]
        }
        """
        try:
            from strategies.universal_precise_entry import batch_check_all_candidates
            from datetime import datetime as dt

            date_str = date or dt.now().strftime("%Y-%m-%d")
            results = batch_check_all_candidates(date_str)

            # 从候选池加载股票名称
            try:
                candidates = _load_json(BASE / "candidates.json", {})
            except:
                candidates = {}

            stocks = []
            for r in results:
                code = r.get("code", "")
                if not code:
                    continue

                # 构建L1状态
                l1_info = r.get("l1", {})
                l1_detail = l1_info.get("detail", "未知")
                l1_risk = l1_info.get("risk_score", 0)
                l1_threshold = l1_info.get("risk_threshold", 35)
                if l1_risk <= l1_threshold:
                    l1_status = "✅"
                elif l1_risk > l1_threshold * 1.5:
                    l1_status = "❌"
                else:
                    l1_status = "⚠️"

                # 构建L2状态
                l2_info = r.get("l2", {})
                l2_is_ok = l2_info.get("is_consolidating", False)
                l2_detail = l2_info.get("detail", "待评估")
                l2_status = "✅" if l2_is_ok else "❌"

                # 构建L3状态
                l3_info = r.get("l3", {})
                l3_is_ok = l3_info.get("resonance", False)
                l3_detail = l3_info.get("detail", "待评估")
                l3_status = "✅" if l3_is_ok else "❌"

                # 生成行动建议和预期时间
                verdict = r.get("verdict", "unknown")
                if verdict == "ready_to_buy":
                    action = "🟢 可以买入"
                    expected_when = "立即"
                elif verdict == "wait_resonance":
                    action = "⏳ 等待日内共振"
                    expected_when = "盘中"
                elif verdict == "wait_consolidation":
                    action = "⏳ 继续缩量巩固"
                    expected_when = "3-5天"
                elif verdict == "wait_cool_down":
                    action = "⏳ 等待冷却"
                    expected_when = "1-3天"
                elif verdict == "avoid_chase":
                    action = "🔴 避免追高"
                    expected_when = "观察"
                else:
                    action = "❓ 未知"
                    expected_when = "-"

                stocks.append({
                    "code": code,
                    "name": candidates.get(code, {}).get("name", code),
                    "market_regime": r.get("market_regime", "unknown"),
                    "market_score": int(r.get("market_score", 0)),
                    "l1": {
                        "status": l1_status,
                        "detail": l1_detail,
                        "risk_score": int(l1_risk),
                        "threshold": int(l1_threshold)
                    },
                    "l2": {
                        "status": l2_status,
                        "detail": l2_detail,
                        "is_consolidating": bool(l2_is_ok)
                    },
                    "l3": {
                        "status": l3_status,
                        "detail": l3_detail,
                        "resonance": bool(l3_is_ok)
                    },
                    "verdict": verdict,
                    "action": action,
                    "expected_when": expected_when
                })

            return _clean({
                "date": date_str,
                "stocks": stocks,
                "summary": {
                    "total": len(stocks),
                    "ready": len([s for s in stocks if s["verdict"] == "ready_to_buy"]),
                    "wait_resonance": len([s for s in stocks if s["verdict"] == "wait_resonance"]),
                    "waiting": len([s for s in stocks if s["verdict"] in ["wait_consolidation", "wait_cool_down"]]),
                    "avoid": len([s for s in stocks if s["verdict"] == "avoid_chase"])
                }
            })
        except Exception as e:
            return _clean({
                "error": f"入场评判失败: {str(e)}",
                "stocks": []
            })

    # ---------- 严重顶背离报警 ----------
    def alert_severe_divergence(self, date=None):
        """严重顶背离告警——已停用（2026-08-19）。

        极值后市确认验证（37只候选池、约3年、1819事件，见 t_io/validation/w35_divergence/
        divergence_验证报告_daily.md）显示：count≥2 顶背离命中率53.4%反而低于无背离基线
        57.8%，告警无区分度、会大量误报，故不再推送飞书/触发独立警报。
        持仓体检表(load_ob_analysis)仍展示顶背离信息，供与超买/趋势组合参考。"""
        return _clean({"alerts": [], "disabled": True})

    # ---------- 个股技术分析弹窗 ----------
    def load_stock_chart(self, code, version=None, want_minutes=False, period=None):
        """日线(本地缓存秒回/网络兜底) → 7 条 MA + MACD/RSI/BOLL → resample 周/月 + 30分/60分
        → 支撑压力。支持带前缀指数代码(sh000001/sz399001 等)。内存缓存：同一标的当日重复用。

        version：前端持有的上一次版本号。若与缓存一致 ⇒ 只回 `{"unchanged": True}`（几百字节），
        不序列化那 300KB payload —— 前端每 10s 轮询一次，这个握手把桥上的传输整个省掉。

        want_minutes（2026-10-06）：**默认 False ⇒ 不取 30/60分**。分钟线只能逐只打 tushare
        （实测 1.6s/周期、无法批量），是日线视图首次打开的主延迟。前端仅在切到 30分/60分 Tab
        时才传 True，日线/周/月视图因此不再被分钟取数阻塞。

        **2026-10-21：want_minutes 在非分钟视图上一律降级为 False。**
        现场（`chart_zoom.log` 15:28:22）：前端在**月线 Tab**上仍传 `wantMin=true`
        （`_wantMinutes()` 只要上一份 payload 带过 min30 就为真），而后端判"period 不是分钟档
        ⇒ 无从知道要哪一档 ⇒ **四档全抓**。这一次重建实测 **12.5s**（min5 3200 + min10 1600
        + min30/60 各 320 根，逐周期取数 + 指标），并发时直接越过前端 20s ⇒ owner 截图里的
        「K线加载失败 … 投递层积压」。日线/周/月视图根本不渲染分钟序列（`strip_minutes` 还会
        把它剥掉）⇒ 在这里收口，任何旧前端/缓存都不再触发全量分钟重建。
        """
        if period is not None and period not in _MIN_PERIODS:
            want_minutes = False

        out = {"code": code, "name": code, "available": False, "error": ""}
        if not hasattr(self, "_stock_chart_cache"):
            self._stock_chart_cache = {}
        cache_key = f"{datetime.now().strftime('%Y-%m-%d')}_{code}"
        if cache_key in self._stock_chart_cache:
            _ts, _res = self._stock_chart_cache[cache_key]
            # fix P0-15: 盘前缓存的图缺今日K线(最后日期<今天)，或盘中超15分钟 → 重算，避免图停在昨日。
            # 2026-10-04 卡顿修复：原来只看「最后日期<今天」就判 stale ⇒ 周末/假期**每次轮询都全量重建**
            # （实测热路径 2.8s、冷 16.7s，跑在主线程上直接冻界面）。补两道闸：非交易日不可能出新K线；
            # 交易日内也最多 2 分钟重建一次，而不是每 10s 一次。
            _dates = _res.get("period_data", {}).get("daily", {}).get("dates") or []
            _last = str(_dates[-1]) if _dates else ""
            _now = datetime.now()
            _today = _now.strftime("%Y-%m-%d")
            _age = (_now - _ts).total_seconds()
            _new_bar_possible = (_last < _today and _now.weekday() < 5
                                 and _now.strftime("%H:%M") >= "09:30")
            _stale = ((_new_bar_possible and _age > 120)
                      or (_last == _today and _age > 15 * 60))
            # 2026-10-20（owner：日线只显示 40 根）：内存缓存里的日线可能来自**短缓存重建**
            # （40 根）；只要磁盘 payload 有更长的历史就判 stale，强制走 payload/长历史路径。
            if not _stale and len(_dates) < 120:
                try:
                    import core.chart_cache as _cc0
                    _hp = _cc0.load_payload(str(code).split("_")[0]) or {}
                    if len(_hp.get("daily_rows") or []) > len(_dates) * 2:
                        _stale = True
                except Exception:
                    pass
            if not _stale:
                # want_minutes 但缓存里没有分时（首帧为提速未取）⇒ 落到重建（带分时）。
                # 2026-10-17：**三档都要**（10min 之前因合成 bug 常缺，只查 min30 会命中残缺缓存）。
                # 2026-10-20（加 5 分档）：改成**按当前周期判**。`_payload_to_result` 现在按
                # `keep_minute` 只留当前那一档，用"三档齐全"判会让分钟视图每次都判缺失、白重建。
                _pd = _res.get("period_data") or {}
                _min_keys = ((period,) if period in _MIN_PERIODS
                             else ("min10", "min30", "min60"))
                _has_min = all(bool(_pd.get(k)) for k in _min_keys)
                if not (want_minutes and not _has_min):
                    _v = _res.get("version")
                    if version is not None and _v is not None and version == _v:
                        return {"code": code, "available": True, "unchanged": True, "version": _v}
                    return _res

        # 东财标的(em前缀)磁盘缓存：K线静态(每日更新)，当日缓存避免东财接口重试
        if str(code).startswith("em"):
            try:
                _em_cache = BASE / "t_io" / "cache" / f"em_kline_{str(code)[2:]}.json"
                if _em_cache.exists():
                    _c = _load_json(_em_cache, None)
                    if _c and _c.get("date") == datetime.now().strftime("%Y-%m-%d") and _c.get("rows"):
                        _cdf = pd.DataFrame(_c["rows"])
                        _cdf["date"] = pd.to_datetime(_cdf["date"])
                        out["name"] = _c.get("name") or code
                        return self._build_chart_from_df(_cdf, out, code)
            except Exception:
                pass

        code_str = str(code)
        # em前缀 → 东财secid(如 em47.800005=A股平均股价)；sx000000 → 指数；纯6位 → 股票
        is_em = code_str.startswith("em")
        is_index = code_str[:2] in ("sh", "sz", "bj") and code_str[2:].isdigit()
        symbol = code_str if is_index else ("sh" + code_str if code_str[0] in "56" else "sz" + code_str)
        # 2026-08-24: 指数/东财日线磁盘缓存（当日秒回，避免每次网络拉 400 根慢）
        chart_cache_fp = BASE / "t_io" / "cache" / "stock_chart" / f"{code_str}.json"
        if is_index or is_em:
            try:
                if chart_cache_fp.exists():
                    _cc = json.loads(chart_cache_fp.read_text(encoding="utf-8"))
                    if _cc.get("date") == datetime.now().strftime("%Y-%m-%d") and _cc.get("rows"):
                        rows = _cc["rows"]
            except Exception:
                pass

        # 2026-10-04 cache-first：盘后预下载的「图表 payload」命中则**瞬开**，不走 GM 优先路径
        # （GM 不可达时每次卡满 12s 超时）。miss 时行为完全不变。
        # 2026-10-06：**覆盖指数**（此前 `not is_index` 把指数排除 → 指数每次走网络、且用错取数函数）。
        if not is_em:
            _hit = self._serve_payload_cache(code_str, version, want_minutes,
                                             allow_network=True, period=period)
            if _hit is not None:
                return _hit

        # 本地日线缓存（个股 `{code}.json` / 指数 `index_{code}.json`）
        rows = []
        _df = None
        if not is_em:
            rows = self._daily_rows_cache_first(code_str, allow_network=True)
            # 2026-10-20（owner：「日线只能显示一部分」）：扫描类短窗口（如 _RC_BARS=30）
            # 会给**没有旧缓存**的票写入 30~40 根日线 ⇒ K 线图只画得出这一小段。
            # 这里发现缓存过短就补拉一次长历史，并 merge 回共享缓存（下次直接够用）。
            if rows and not is_index and len(rows) < 120:
                try:
                    _long = self._fetch_daily_bounded(code_str, min_rows=250)
                    if _long is not None and not _long.empty and len(_long) > len(rows):
                        try:
                            from core.chart_cache import write_daily_history
                            write_daily_history(code_str, _long)   # merge，不截短
                        except Exception:
                            pass
                        rows = [{"date": str(_r.date), "open": float(_r.open),
                                 "close": float(_r.close), "high": float(_r.high),
                                 "low": float(_r.low), "volume": float(_r.volume)}
                                for _r in _long.itertuples(index=False)]
                except Exception:
                    pass
            if not rows:
                # 北交所：日线四源全不通（memory），`bj_daily.fetch_bj_daily` 逐只重试 8 次 ≈18s
                # ⇒ 卡死主线程。有缓存/payload 的 BJ 码已在上面命中；无缓存则**立即优雅降级**。
                try:
                    from core.market_data.codec import market_of
                    if not is_index and market_of(code_str) == "BJ":
                        out["error"] = "北交所行情源暂不可用（已知受限）"
                        return _clean(out)
                except Exception:
                    pass
                try:
                    # miss → 带 6s 硬超时，防慢源冻主线程（指数走 index_daily）
                    _df = self._fetch_daily_bounded(code_str)
                    if _df is not None and not _df.empty:
                        for _r in _df.itertuples(index=False):
                            rows.append({"date": str(_r.date), "open": float(_r.open),
                                         "close": float(_r.close), "high": float(_r.high),
                                         "low": float(_r.low), "volume": float(_r.volume)})
                except Exception:
                    rows = []

        # 本地缓存不可用 → 走网络拉 400 根
        if not rows:
            # 2026-10-18：腾讯优先的 bounded 取数**明确返空**（代码不存在/已退市）⇒ 直接失败。
            # 不能再落到下面的 provider（GM 优先）再等 4.5s 限流窗 —— 实测手输错码开图 7~11s，
            # owner 反馈「搜寻后加载特别慢」。超时（_df is None）仍保留原兜底路径。
            if not is_em and _df is not None and getattr(_df, "empty", True):
                out["error"] = "无日线数据（代码可能不存在或已退市）"
                return _clean(out)
            import os as _os, urllib.request as _ur
            for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "all_proxy"]:
                _os.environ.pop(_k, None)
            _os.environ["NO_PROXY"] = "*"

            if is_em:
                # 东财 secid (em47.800005 → 47.800005)，平均股价等腾讯无代码的标的
                # push2his 间歇断连(风控)，重试 8 次，多数 3-6 次内成功
                data = {}
                rows = []
                for _att in range(8):
                    try:
                        secid = code_str[2:]
                        url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
                               f"&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
                               f"&klt=101&fqt=1&beg=0&end=20500101&lmt=400")
                        req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                        "Referer": "https://quote.eastmoney.com/"})
                        raw = _ur.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
                        data = json.loads(raw)
                        klines = (data.get("data") or {}).get("klines") or []
                        if not klines:
                            continue
                        rows = []
                        for item in klines:
                            parts = item.split(",")
                            if len(parts) >= 6:
                                rows.append({
                                    "date": parts[0], "open": float(parts[1]), "close": float(parts[2]),
                                    "high": float(parts[3]), "low": float(parts[4]), "volume": float(parts[5]),
                                })
                        if rows:
                            break
                    except Exception:
                        import time as _t
                        _t.sleep(1)
                if rows:
                    out["name"] = (data.get("data") or {}).get("name") or code
                elif not out["error"]:
                    out["error"] = "东财拉取日线失败"
                    return out
            else:
                # P1-2 收敛：market_data provider（gm 主源/腾讯兜底）；指数走 index_daily
                from core.market_data import get_provider
                try:
                    _prov = get_provider()
                    if is_index:
                        # 传 end_date ⇒ 不回写共享长历史缓存（防静默截短 index_*.json）
                        df = _prov.index_daily(code, 400, end_date=datetime.now().strftime("%Y-%m-%d"))
                    else:
                        df = _prov.daily(code, 400)
                    rows = []
                    if df is not None and not df.empty:
                        for r in df.itertuples():
                            rows.append({"date": r.date, "open": r.open, "close": r.close,
                                         "high": r.high, "low": r.low, "volume": r.volume})
                except Exception as e:
                    out["error"] = f"拉取日线失败: {e}"
                    return out

        # 2026-08-24: 网络拉取的指数/东财日线写磁盘缓存（当日，下次秒回）
        if rows and (is_index or is_em):
            try:
                chart_cache_fp.parent.mkdir(parents=True, exist_ok=True)
                chart_cache_fp.write_text(
                    json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "rows": rows},
                               ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        if not rows:
            out["error"] = "无日线数据"
            return out
        if out["name"] == code:
            try:
                jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
                nm = (jy.get(code, {}) or {}).get("name", "") if isinstance(jy.get(code), dict) else ""
                if nm:
                    out["name"] = nm
            except Exception:
                pass

        try:
            import pandas as pd
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            out = self._build_chart_from_df(
                df, out, code,
                min_frames={} if not want_minutes else None,
                want_min_period=period if want_minutes else None)
        except Exception as e:
            out["error"] = f"计算失败: {e}"
            return out

        result = _clean(out)
        # 东财标的写磁盘缓存（当日有效，避免后续东财接口重试）
        if str(code).startswith("em") and result.get("available"):
            try:
                _em_cache = BASE / "t_io" / "cache" / f"em_kline_{str(code)[2:]}.json"
                _em_cache.write_text(json.dumps({
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "name": result.get("name"),
                    "rows": rows,
                }, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        self._cache_chart(cache_key, result)
        # 机会式 payload 落盘（2026-10-06）：任何**真正建完**的图都落一份磁盘 payload，
        # 使非小池码「这次开了、下次（含新会话）瞬开」。后台、失败静默、有 LRU 容量上限。
        if result.get("available") and not is_em:
            try:
                import core.chart_cache as _cc
                # 2026-10-20：**禁止用短图覆盖长 payload**。短日线缓存重建出的 40 根图
                # 一旦落盘，下次打开就永久只剩 40 根（owner「日线只显示一部分」的成因）。
                _old = _cc.load_payload(code_str) or {}
                _old_n = len(_old.get("daily_rows") or [])
                if not (_old_n and len(rows) * 2 < _old_n):
                    _cc.save_payload(code_str, result, rows)
            except Exception:
                pass
        return result

    # ---------- 盘中实时 K 线（2026-10-17） ----------
    def load_chart_realtime(self, code, period="daily", known_sig=None):
        """**盘中实时补丁**：只重算「当前显示周期」，给 K 线弹窗每 3s 一次地喂新尾。

        为什么必须单开一个入口（而不是让 `load_stock_chart` 自己变实时）——三层闸
        各自都把旧数据钉死了：
          ① tushare `stk_mins` 盘中只回到**上一交易日收盘**（实测 2026-10-14 14:13
             仍止于 10-13 15:00）⇒ 分钟图在盘中是死的；
          ② `_fetch_min_bars` 的内存缓存键是**30 分钟时段**⇒ 即便源是活的也冻半小时；
          ③ 前端版本握手只认 (末日期:根数) ⇒ 当日/当根**价格在动但签名不变**，
             回 `unchanged`、不重画。
        本入口绕开这三层：现取腾讯分钟 K 线（盘中的未收盘根就是实时价）+ 实时快照，
        重算该周期的 MA/MACD/RSI/BOLL/黄金分割，回前端**整段**（几十 KB）。
        前端只替换该周期、保留缩放。

        返回：{available, period, session, ts, quote, series, sig, error}
        `sig` = 末根 (时间:收:根数) —— 前端据此跳过没变化的重绘。
        **永不抛**；任何失败回 available=False（前端保留上一帧）。

        ⚠️ 2026-10-18 改成 **SWR（非阻塞）**：调用线程**零网络、零 pandas**，只做字典读写；
        真正的取数/重算丢到守护线程。原因：GUI 卡死专项里实测本入口在真实进程里要
        **1.5~3.0s**（`gui_slow_calls.log`），而它每 3s 就要投递一次结果 —— pywebview 的
        `evaluate_js` 是串行的，慢 js_api 会把界面投递排队拖死。
        现在：有缓存就立刻回（`stale=true`），没有就回 `warming=true`，前端快速重试。

        2026-10-20（owner：5/10 分要「极其实时」）两处提速：
          · worker 拆**并行两跳**（快照 ∥ 分钟取数）+ 非 10min 档只取 340 根
            ⇒ 实测 min5 365ms → **173ms**、min10 333ms → **224ms**；
          · `known_sig`：前端把已持有的 sig 传上来，没变就**不回 102KB 序列**（实测降到 0.36KB）。
        配合前端 0.5s 轮询 + TTL 0.4s ⇒ 每分钟图约 **2 帧/秒**。
        """
        base = str(code).split("_")[0]
        period = str(period or "daily")
        out = {"code": base, "available": False, "period": period, "session": False,
               "ts": datetime.now().strftime("%H:%M:%S"), "quote": {}, "series": None,
               "sig": "", "error": "", "warming": False, "stale": False}
        if period not in ("daily", "weekly", "monthly") + _MIN_PERIODS:
            out["error"] = f"未知周期: {period}"
            return out
        _fr, _state = self._chart_live_frame(base, period)
        if _state == "fresh":
            return _slim_if_same(_fr, known_sig)   # TTL 内直接回（分钟 0.4s / 日线 1.2s）
        if _state == "stale":                      # 有旧快照：立刻回它，后端在刷新
            _o = dict(_fr)
            _o["stale"] = True
            return _slim_if_same(_o, known_sig)
        out["warming"] = True
        out["session"] = _in_trading_session()
        return out

    def _chart_live_frame(self, base, period):
        """SWR 取帧（**零网络、零 pandas**）：返回 `(frame|None, state)`。

        state: `fresh`（TTL 内，可直接用）/ `stale`（旧帧，已顺手 kick 刷新）/ `warming`（还没有帧）。
        抽出来是为了让**多窗口看盘**（`load_multi_chart`，一次 6 只票）复用它 ——
        否则那 6 个窗口要么各写一份 SWR、要么绕开缓存直接算，两条都会把主线程拖住。
        """
        ck = (base, period)
        _t = _time_mod.time()
        _is_min = str(period).startswith("min")
        _ttl = _CHART_LIVE_TTL_MINUTE if _is_min else _CHART_LIVE_TTL
        _kick = _CHART_LIVE_MIN_KICK_MINUTE if _is_min else _CHART_LIVE_MIN_KICK
        with _CHART_LIVE_LOCK:
            hit = _CHART_LIVE_CACHE.get(ck)
            _st = _CHART_LIVE_STATE.get(ck) or {}
            _running = bool(_st.get("running"))
            _last_kick = float(_st.get("ts") or 0.0)
        if hit and (_t - hit[0]) < _ttl:
            return hit[1], "fresh"
        if not _running and (_t - _last_kick) > _kick:
            with _CHART_LIVE_LOCK:
                _CHART_LIVE_STATE[ck] = {"running": True, "ts": _t}
            _th.Thread(target=self._chart_live_worker, args=(base, period),
                       daemon=True, name=f"chart-live-{base}-{period}").start()
        if hit:
            return hit[1], "stale"
        return None, "warming"

    def open_multi_window(self, codes=None):
        """把「多窗口看盘」开成**独立原生窗口**（owner 2026-10-22：要能独立于 GUI 拖动）。

        为什么必须是真窗口：页面内的浮层是 DOM，**拖不出 WebView 的视口**——只能在主窗口
        里面挪（还会被夹在边界内）。要挪到别的显示器、或者让主窗口缩到一角而看盘窗保持大，
        只能开第二个原生窗口。

        实现取巧但零重复：新窗口**加载同一个 index.html**，加载完成后由前端
        `mwEnterWindowMode()` 把界面切进"看盘模式"（隐藏其它 DOM、停掉主界面的轮询、
        让九宫格铺满整个窗口）。图表渲染/选股/刷新全部复用现有代码，不存在第二份实现。

        失败（pywebview 版本不支持运行期建窗等）返回 ok=False，前端自动退回页面内浮层。
        """
        try:
            import webview
        except Exception as e:
            return {"ok": False, "error": f"webview 不可用: {e}"}
        try:
            w = getattr(self, "_mw_window", None)
            if w is not None:
                try:
                    w.show()
                    w.restore()
                    return {"ok": True, "reused": True}
                except Exception:
                    self._mw_window = None          # 旧窗口已销毁 ⇒ 重建
            entry = Path(__file__).parent / "web" / "index.html"
            win = webview.create_window(
                "多窗口看盘 (5分K)",
                str(entry),
                js_api=self,
                width=1180,
                height=820,
                min_size=(560, 380),
            )
            self._mw_window = win

            # 把主窗口当前选的票带过去：独立窗口可能是**另一个 origin**（file:// vs http），
            # localStorage 不通 ⇒ 九格会全是「— 选择标的 —」（2026-10-22 首版实测）。
            _codes = [str(c).split("_")[0] for c in (codes or []) if str(c).strip()][:12]

            def _on_loaded():
                try:
                    import json as _json
                    win.evaluate_js(
                        "mwEnterWindowMode && mwEnterWindowMode(%s)"
                        % _json.dumps(_codes)
                    )
                except Exception:
                    pass

            def _on_closed():
                self._mw_window = None

            try:
                win.events.loaded += _on_loaded
                win.events.closed += _on_closed
            except Exception:
                pass
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    def close_multi_window(self):
        """关掉独立看盘窗（窗口模式里右上角 × 走这条，而不是隐藏浮层）。"""
        w = getattr(self, "_mw_window", None)
        self._mw_window = None
        if w is None:
            return {"ok": True, "already": True}
        try:
            w.destroy()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}

    def load_multi_chart(self, codes, period="min5", tail=90):
        """**多窗口看盘**：一次给多只票的实时尾。

        ⚠️ 2026-10-21 改走 `_mw_frame`（**多窗口专用轻量帧**），不再复用单图的 SWR。
        原因：单图实时尾现在建立在「3 个月深档」（近 3000 根）上，实测约 **1s/只**；
        六宫格 6 只并发全卡在 GIL 上 ⇒ 一帧要好几十秒，owner 看到的就是
        「K 线几乎不更新」。细胞只要 ~48 根，深档对它纯属浪费。

        · 单次最多 12 只（防误传整池）；**永不抛**。
        """
        items = []
        _tail = max(20, int(tail or 90))
        for c in (codes or [])[:12]:
            _c = str(c).split("_")[0].strip()
            if not _c:
                continue
            try:
                fr, st = self._mw_frame(_c)
                if not fr:
                    items.append({"code": _c, "ok": False, "warming": True})
                    continue
                _at = lambda a: (a[-_tail:] if isinstance(a, list) else a)   # noqa: E731
                _q = fr.get("quote") or {}
                items.append({
                    "code": _c, "ok": bool(fr.get("dates")),
                    "stale": st == "stale",
                    "name": self._instrument_name(_c),
                    "quote": _q,
                    # 日线 MA5/10/20：六宫格与单图一样要画「日线均线」参考线
                    "dma": self._daily_ma_bundle(_c, _q.get("price")),
                    # 支撑/压力位（同单图同一个 `_calc_support_resistance`）
                    "levels": self._daily_levels(_c),
                    # 买卖点（券商成交台账）—— 裁到本格窗口内，前端按 5 分钟桶归位
                    "trades": self._mw_trades(_c, fr.get("dates")),
                    "series": {
                        "dates": _at(fr.get("dates")), "ohlc": _at(fr.get("ohlc")),
                        "volume": _at(fr.get("volume")), "amount": _at(fr.get("amount")),
                        "ma5": _at(fr.get("ma5")),
                        # MACD 三条线也要按 tail 截齐（2026-10-21 owner：多窗口要 MACD）
                        "macd": {k: _at(v) for k, v in (fr.get("macd") or {}).items()},
                    },
                })
            except Exception as e:
                items.append({"code": _c, "ok": False,
                              "error": f"{type(e).__name__}: {str(e)[:60]}"})
        return {"items": items, "period": period}

    def _mw_trades(self, code, dates):
        """该 code 的成交行，**裁到九宫格窗口内**（只画最近 48 根 5 分 K）。

        台账本身按 mtime 缓存（见 `_chart_trades`），这里只是过滤 + 截断，零 IO。
        """
        try:
            if not dates:
                return []
            _from = str(dates[0])[:10]
            _t = _chart_trades(code) or []
            _out = [x for x in _t if str((x or {}).get("ts") or "")[:10] >= _from]
            return _out[-200:]
        except Exception:
            return []

    def _mw_frame(self, code):
        """多窗口专用的**轻量帧**（SWR 取）：返回 `(frame|None, state)`。

        只算 K 线 / MA5 / 成交额，**不碰** MACD/RSI/BOLL/黄金分割/深档 ——
        细胞只有 1/6 屏宽，48 根就够，深档的代价（~1s/只 × 6 只并发）直接把它拖成"不动"。
        TTL 2s：一轮 6 只各算一次约 1.2s，够跟上 3 秒轮询。
        """
        _c = str(code).split("_")[0]
        _now = _time_mod.time()
        with _MW_LOCK:
            hit = _MW_CACHE.get(_c)
            _run = bool(_MW_RUN.get(_c))
        if hit is not None and (_now - hit[0]) < _MW_TTL:
            return hit[1], "fresh"
        if not _run:
            with _MW_LOCK:
                _MW_RUN[_c] = True
            _th.Thread(target=self._mw_worker, args=(_c,), daemon=True,
                       name=f"mw-{_c}").start()
        if hit is not None:
            return hit[1], "stale"
        return None, "warming"

    def _mw_worker(self, code):
        try:
            fr = self._mw_compute(code)
            if fr:
                with _MW_LOCK:
                    if len(_MW_CACHE) > 200:
                        _MW_CACHE.clear()
                    _MW_CACHE[code] = (_time_mod.time(), fr)
        except Exception as e:
            print(f"[WARN] [多窗口] {code} 取帧失败: {type(e).__name__}: {str(e)[:120]}")
        finally:
            with _MW_LOCK:
                _MW_RUN[code] = False

    def _mw_compute(self, code):
        """轻量帧本体：腾讯 m5 尾（340 根，含未收盘根=实时）→ 最近 120 根 + MA5 + MACD + 成交额。

        ⚠️ MACD 在**完整 340 根**上算完再截尾，不能只喂 120 根：EMA12/26 的头几十根
        严重受种子影响，只给 120 根会让 MACD 与单图对不上。截尾只切数组、不重算。
        """
        # ⚠️ 直接用**腾讯 m5 尾 340 根**，不走 `_fetch_min_df`：后者会再跟「3 个月深档」
        # （近 3000 行）做一次 concat+去重，而九宫格每格只要 120 根 —— 实测白烧 ~80ms/只/轮。
        # 340 根也够 MACD 收敛（EMA26 的 3 倍预热期 ≈ 78 根），与单图逐位一致（见测试）。
        df = _fetch_min_bars_tencent(code, "5min")
        if df is None or getattr(df, "empty", True):
            df = self._fetch_min_df(code, "min5")     # 兜底：含深底/磁盘
        if df is None or getattr(df, "empty", True):
            return None
        _closes_all = [float(x) for x in df["close"].astype(float).values]
        _dif, _dea, _hist = _ema_macd(_closes_all)
        n = len(df)
        df = df.tail(120).reset_index(drop=True)
        _cut = max(0, n - 120)
        _closes = _closes_all[_cut:]
        _ma5 = []
        for i in range(len(_closes)):
            w = _closes[max(0, i - 4): i + 1]
            _ma5.append(round(sum(w) / len(w), 3) if len(w) == 5 else None)
        # 指数不算成交额（点位 × 量没有意义，实测上证 5 分钟会算出 2.7 万亿）⇒ 交给前端回落成交量
        _amt = None if _index_code(code) else [
            round(((float(h) + float(l) + float(c)) / 3.0) * float(v) * 100.0)
            for h, l, c, v in zip(df["high"], df["low"], df["close"], df["volume"])]
        return {
            "dates": [str(x)[:16] for x in df["time"].astype(str).values],
            "ohlc": [[round(float(o), 3), round(float(c), 3), round(float(lo), 3),
                      round(float(hi), 3)]
                     for o, c, lo, hi in zip(df["open"], df["close"], df["low"], df["high"])],
            "volume": [round(float(v)) for v in df["volume"]],
            "amount": _amt,
            "ma5": _ma5,
            # MACD（2026-10-21 owner：多窗口也要 MACD）—— 与单图同口径 EMA12/26/9，hist 取 (DIF-DEA)×2
            "macd": {"dif": [round(x, 4) for x in _dif[_cut:]],
                     "dea": [round(x, 4) for x in _dea[_cut:]],
                     "hist": [round(x, 4) for x in _hist[_cut:]]},
            "quote": self._fetch_live_quote(code) or {},
        }

    def _instrument_name(self, code):
        """6 位码 → 名称（本地名表，600s 缓存）。取不到回空串，前端就显示代码。"""
        try:
            return str(self._instrument_names().get(str(code).split("_")[0]) or "")
        except Exception:
            return ""

    def _daily_ma_bundle(self, code, price=None):
        """日线 MA5/10/20（**含当日**）：`MA_n = (前 n-1 个完成日收盘 + 现价) / n`。

        与 `core.ma_reclaim.ma5_state` 同口径（日线均线该把当日算进去），数据走本地
        日线缓存 ⇒ **零网络**。60 秒记忆：六宫格每 4 秒轮询，日线基准一天只变一次，
        没必要每跳读 6 个文件。
        """
        try:
            _c = str(code).split("_")[0]
            _now = _time_mod.time()
            _memo = getattr(self, "_dma_memo", None)
            if _memo is None:
                _memo = self._dma_memo = {}
            _hit = _memo.get(_c)
            if _hit is not None and (_now - _hit[0]) < 60:
                return _hit[1]
            import core.chart_cache as _cc
            df = _cc.load_daily_display(_c)
            if df is None or getattr(df, "empty", True):
                return None
            _today = datetime.now().strftime("%Y-%m-%d")
            _dts = [str(x)[:10] for x in df["date"].astype(str).values]
            _cls = [float(x) for x in df["close"].astype(float).values]
            _basis = [c for d, c in zip(_dts, _cls) if d < _today and c == c]
            if len(_basis) < 19:
                return None
            _px = float(price) if price else float(_cls[-1])
            if _px <= 0:
                return None
            out = {f"ma{n}": round((sum(_basis[-(n - 1):]) + _px) / n, 3) for n in (5, 10, 20)}
            _memo[_c] = (_now, out)
            return out
        except Exception:
            return None

    def _daily_levels(self, code):
        """日线口径的**支撑/压力位**（与单图同一个 `_calc_support_resistance`），60 秒记忆。

        六宫格每格也要画支撑压力（2026-10-21 owner）。走本地日线缓存 ⇒ **零网络**；
        该函数需要 ma20/ma60/ma180 列，故先过一次 `_calc_ma_and_indicators`（~20ms/只），
        60 秒内同票复用，3 秒轮询下等于白送。
        每方向只回**最近 2 个**（格子里画 4 条线已经不少了）。
        """
        try:
            _c = str(code).split("_")[0]
            _now = _time_mod.time()
            _memo = getattr(self, "_lvl_memo", None)
            if _memo is None:
                _memo = self._lvl_memo = {}
            _hit = _memo.get(_c)
            if _hit is not None and (_now - _hit[0]) < 60:
                return _hit[1]
            import core.chart_cache as _cc
            df = _cc.load_daily_display(_c)
            if df is None or getattr(df, "empty", True) or len(df) < 30:
                _memo[_c] = (_now, None)
                return None
            # ⚠️ **不要**为了 3 条均线去跑 `_calc_ma_and_indicators`：实测 800 行要 **1.26s/只**
            #    （pandas 3 的 Arrow 字符串 + 一整套指标），9 宫格就是 11 秒。
            #    `_calc_support_resistance` 只用到 close/high/low + ma20/60/180，这里就地算，
            #    ~2ms 拿齐。⚠️ 历史不足 180 根时 ma 是 NaN ⇒ 结果里过滤掉（x==x 是 NaN 判据）。
            _d = df.copy()
            _close_s = _d["close"].astype(float)     # ⚠️ 别叫 _c —— 上面 `_c` 是代码，会被覆盖
            for _n in (20, 60, 180):
                _d[f"ma{_n}"] = _close_s.rolling(_n).mean()
            _lv = self._calc_support_resistance(_d) or {}
            _ok = lambda x: (x.get("price") == x.get("price"))   # noqa: E731  NaN 过滤
            out = {
                "supports": [round(float(x["price"]), 3)
                             for x in (_lv.get("supports") or []) if _ok(x)][:2],
                "resistances": [round(float(x["price"]), 3)
                                for x in (_lv.get("resistances") or []) if _ok(x)][:2],
            }
            _memo[_c] = (_now, out)
            return out
        except Exception:
            return None

    def _chart_live_worker(self, base, period):
        """后台计算一次实时尾并落缓存（`load_chart_realtime` 的 SWR 写端）。失败静默。"""
        ck = (base, period)
        try:
            out = self._compute_chart_live(base, period)
            if out.get("available"):
                with _CHART_LIVE_LOCK:
                    if len(_CHART_LIVE_CACHE) > _CHART_LIVE_MAX:
                        _CHART_LIVE_CACHE.clear()
                    _CHART_LIVE_CACHE[ck] = (_time_mod.time(), out)
        except Exception:
            pass
        finally:
            with _CHART_LIVE_LOCK:
                if len(_CHART_LIVE_STATE) > _CHART_LIVE_MAX * 3:
                    _CHART_LIVE_STATE.clear()
                _st = _CHART_LIVE_STATE.get(ck) or {}
                _st["running"] = False
                _CHART_LIVE_STATE[ck] = _st

    def _fetch_live_quote(self, base):
        """单只实时快照 → quote dict（连接复用后实测 ≈43ms）。失败回 {}。"""
        try:
            from core.market_data.tencent_provider import TencentProvider
            _snaps = TencentProvider().snapshot_auction([base]) or {}
            # `snapshot_auction` 的返回键是**裸 6 位码**（它把 v_sh000001 统一剥了前缀）；
            # 指数的 K 线弹窗传的是 sh000001 ⇒ 两个键都试。
            _q = _snaps.get(base) or _snaps.get(base[2:] if base[:2].lower() in
                                                  ("sh", "sz", "bj") else "") or {}
            if not _q.get("price"):
                return {}
            # 科创板(688)的快照成交量是**股**、其余是**手** ⇒ 这里统一成"手"，
            # 因为下游 `_mk_forming_bar` 会把它写进**手**口径的日线序列（差 100 倍会让
            # 量能窗口整根爆掉）。实测 688008：vol_hand=24,597,313 vs 缓存 245,973 手。
            _vol = _q.get("vol_hand")
            if _vol is not None and str(base).split("_")[0].startswith("688"):
                try:
                    _vol = float(_vol) / 100.0
                except (TypeError, ValueError):
                    _vol = None
            return {"price": round(float(_q["price"]), 3),
                    "change_pct": round(float(_q.get("pct") or 0.0), 2),
                    "pre_close": _q.get("pre_close"),
                    "open": _q.get("open"), "high": _q.get("high"),
                    "low": _q.get("low"), "volume": _vol,
                    "ts_date": _q.get("ts_date")}
        except Exception:
            return {}

    def _compute_chart_live(self, base, period):
        """真正干活的同步实现：快照 + 腾讯分钟K线 → 该周期序列。仅供后台线程调用。

        2026-10-20（owner：5/10 分要「极其实时」）拆成**并行两跳**：快照（43ms）与
        分钟取数（125~165ms）互不依赖，原来串行 ≈210ms，并行后只花长的那个；
        CPU 部分（修末根+指标+黄金分割 ≈55ms）等两边都回来再做，因为修末根要用快照价。
        """
        out = {"code": base, "available": False, "period": period, "session": False,
               "ts": datetime.now().strftime("%H:%M:%S"), "quote": {}, "series": None,
               "sig": "", "error": "", "warming": False, "stale": False}
        _now = datetime.now()
        _session = _in_trading_session(_now)
        out["session"] = _session

        try:
            if period in _MIN_PERIOD_FREQ:
                # ① 快照 ∥ 分钟取数
                import concurrent.futures as _cf
                _df = None
                with _cf.ThreadPoolExecutor(max_workers=2,
                                            thread_name_prefix="chart-live") as _ex:
                    _fq = _ex.submit(self._fetch_live_quote, base)
                    _fd = _ex.submit(self._fetch_min_df, base, period)
                    try:
                        out["quote"] = _fq.result() or {}
                    except Exception:
                        out["quote"] = {}
                    try:
                        _df = _fd.result()
                    except Exception:
                        _df = None
                # ② CPU：修末根（用快照现价）+ 指标 + 黄金分割
                out["series"] = self._build_min_series(base, period, _df,
                                                       out.get("quote") or {})
            else:
                out["quote"] = self._fetch_live_quote(base)
                out["series"] = self._live_bar_series(base, period, _now, _session,
                                                      out.get("quote") or {})
            if out["series"] and (out["series"].get("dates") or []):
                _d = out["series"]["dates"]
                _c = out["series"]["ohlc"][-1][1] if out["series"].get("ohlc") else None
                out["sig"] = f"{_d[-1]}:{_c}:{len(_d)}"
                out["available"] = True
            else:
                out["error"] = "该周期暂无实时数据"
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {str(e)[:80]}"

        return _clean(out)         # numpy 标量/nan → JSON 可序列化（与 load_stock_chart 同纪律）

    def _live_bar_series(self, code, period, now, session, quote=None):
        """日/周/月周期的实时序列：磁盘日线缓存 + **当日 forming bar** → 指标 → 序列。

        当日根优先用腾讯 `fqkline`（前复权、与日线缓存同口径、OHLCV 一次给全）；
        拿不到再退回实时快照造的 forming bar（`_mk_forming_bar`，量的单位按快照口径）。
        """
        import pandas as pd
        import core.chart_cache as _cc
        df = None
        # 2026-10-20（chart_zoom.log：实时尾 n=40 把 801 根整段替换）：
        # `load_daily_display` 可能只有扫描类写入的 40 根；实时尾必须优先用 payload 的
        # **长日线**（801 根），否则盘中每 5s 一次的日线尾会把整图从 801 根缩成 40 根。
        try:
            _hit = _cc.load_payload(str(code).split("_")[0]) or {}
            _prows = _hit.get("daily_rows") or []
            if len(_prows) >= 120:
                df = pd.DataFrame(_prows)
        except Exception:
            df = None
        if df is None or df.empty:
            try:
                df = _cc.load_daily_display(code)
            except Exception:
                df = None
        if df is None or df.empty:
            _rows = self._daily_rows_cache_first(code, allow_network=False)
            if _rows:
                df = pd.DataFrame(_rows)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        _today = now.strftime("%Y-%m-%d")
        _has_today_quote = bool(quote) and str(quote.get("ts_date") or "") == _today
        # 盘中必然要补；**收盘后 15:05~缓存刷新前**也要补（否则图会停在一个"半天的当日根"上）。
        if session or _has_today_quote:
            today_bar = None
            if session or _has_today_quote:
                # 收盘后也取：用**完整**的当日根覆盖盘中的半成品（缓存刷新前图不会停在半天）
                try:
                    from core.market_data.tencent_provider import daily_kline_raw
                    _dk = daily_kline_raw(code, days=3)
                    if _dk is not None and not _dk.empty and str(_dk["date"].iloc[-1])[:10] == _today:
                        _bar = {k: float(_dk.iloc[-1][k]) for k in
                                ("open", "high", "low", "close", "volume")}
                        # 与 `_mk_forming_bar` 同一条盘前防伪闸：集合竞价后开盘前的快照会给出
                        # O=H=L=C、量=0 的假当日根（曾导致「600362 误报破 5 日线」）。
                        if not (_bar["volume"] <= 0 and _bar["high"] == _bar["low"]):
                            today_bar = _bar
                except Exception:
                    today_bar = None
            if today_bar is None:
                _snap = self._today_forming_bar(code, allow_network=False)
                if _snap and str(_snap.get("date")) == _today:
                    today_bar = {k: float(_snap[k]) for k in
                                 ("open", "high", "low", "close", "volume")}
            if today_bar is None and _has_today_quote:
                # 快照兜底（fqkline 万一盘中不带当日根）：与本文件既有的 forming bar 同口径
                # （`_mk_forming_bar`：无成交且无振幅 ⇒ 视为还没开始交易，不造根）
                _b = self._mk_forming_bar(_today, quote.get("price"), quote.get("open"),
                                          quote.get("high"), quote.get("low"),
                                          quote.get("volume"))
                if _b:
                    today_bar = {k: float(_b[k]) for k in
                                 ("open", "high", "low", "close", "volume")}
            if today_bar:
                _last = str(df["date"].iloc[-1])[:10]
                _row = {"date": pd.Timestamp(_today), **today_bar}
                if _last == _today:
                    # 当日根：**以实时为准**（缓存里的当日根可能停在几十分钟前）
                    df = df.iloc[:-1]
                if _last <= _today:
                    df = pd.concat([df, pd.DataFrame([_row])], ignore_index=True)
        # 成交额：补在**补完当日根之后**，当天那根也才有值（2026-10-20 面板改画成交额）
        df = _ensure_amount(df, code)
        _agg_rt = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        if "amount" in df.columns:      # 指数没有 amount ⇒ 别写死（否则 pandas 报 Label 不存在）
            _agg_rt["amount"] = "sum"
        if period == "weekly":
            df = df.resample("W-FRI", on="date").agg(_agg_rt).dropna().reset_index()
        elif period == "monthly":
            _maj, _min = (int(x) for x in pd.__version__.split(".")[:2])
            df = df.resample("ME" if (_maj, _min) >= (2, 2) else "M",
                             on="date").agg(_agg_rt).dropna().reset_index()
        d = _calc_ma_and_indicators(df)
        s = _to_series(d)
        try:
            s["fib"] = self._calc_fibonacci(d, period)
        except Exception:
            pass
        return s

    def _fetch_min_df(self, code, period):
        """分钟档**只取数**（纯网络/回退，无 CPU）—— 供实时尾与快照**并行**跑。

        拆出来的理由（2026-10-20 实测）：一次实时尾 = 快照 43ms + m5 取数 165ms + CPU 55ms，
        原来三者串行 ~270ms；快照与取数互不依赖，并行后只花 max(43,165)+55 ≈ 220ms。

        2026-10-30（展示档深底）：5/10min 的实时尾必须与**历史底座**合并后再下发 ——
        只回 320 根会被前端「明显更短」闸挡掉（live_skip_short），「近3月/全部」档也需要
        完整深度。底座走 `_min_bars_deep_base`（当日内存缓存 + 磁盘深档，**零网络**），
        每跳只现取腾讯尾（5min 340 根 / 10min 640 根 m5 合成），不重复拉 3 个月历史。
        """
        _freq = _MIN_PERIOD_FREQ.get(period)
        if not _freq:
            return None
        # 根数由 `_fetch_min_bars_tencent` 按 freq 决定（10min 需 640 根 m5，其它够 320 即可）
        df = _fetch_min_bars_tencent(code, _freq)
        if df is None or df.empty:
            df = _fetch_min_bars(code, _freq)      # 兜底链内部已含深底合并与截尾
            if df is None or df.empty:
                return None
            return df
        keep = MIN_BARS_CHART.get(_freq, MIN_BARS_KEEP)
        if _freq == "5min":
            df = _merge_min_frames(_min_bars_deep_base(code, "5min"), df, keep)
        elif _freq == "10min":
            df = _merge_min_frames(_resample_10min(_min_bars_deep_base(code, "5min")), df, keep)
        else:
            df = df.tail(MIN_BARS_KEEP).reset_index(drop=True)
        if df is None or df.empty:
            return None
        return df

    def _build_min_series(self, code, period, df, quote=None):
        """已完成帧 + 实时快照 → 该周期序列（修末根 + 指标 + 黄金分割）。纯 CPU ≈ 55ms。

        2026-10-19（owner：持仓 K 线要更实时）：腾讯 m30/m60 的**未收盘根不一定每个 tick
        都更新 close**，而持仓用户盯的就是最后一根 ⇒ 用快照把末根补/修成当前价格
        （`_patch_forming_minute_bar`），并让 MA/BOLL 等指标吃到这个实时收盘。
        """
        if df is None or getattr(df, "empty", True):
            return None
        try:
            df = _patch_forming_minute_bar(
                df, _MIN_PERIOD_MINUTES.get(period, 30),
                quote or {}, datetime.now())
        except Exception:
            pass
        d = _calc_ma_and_indicators(df.rename(columns={"time": "date"}))
        s = _to_series(d, intraday=True)
        try:
            s["fib"] = self._calc_fibonacci(d, period)
        except Exception:
            pass
        return s

    def _live_minute_series(self, code, period, quote=None):
        """分钟周期的实时序列（取数 + 建序列的组合；实时尾走并行版，见 `_compute_chart_live`）。

        10min 由 m5 合成（与仓位结构层同一个函数）。腾讯失败时退回 `_fetch_min_bars`
        （tushare/磁盘，至少能出历史，前端会标明不是实时）。
        """
        if period not in _MIN_PERIOD_FREQ:
            return None
        return self._build_min_series(code, period, self._fetch_min_df(code, period), quote)

    # ---------- 手动盘「K线速查」（2026-10-18） ----------
    def _instrument_names(self):
        """本地 名称→代码 表（韭研 watchlist 5382 + NAMES + 手动/自动持仓），600s 缓存。

        只收裸 6 位码；指数/ETF 不在这里（它们留给在线搜索，腾讯 smartbox 带权威类型）。
        多来源合并顺序：韭研 → NAMES → 持仓（后者覆盖前者，用户自己的命名最权威）。
        """
        now = _time_mod.time()
        with _INSTRUMENT_NAMES_LOCK:
            if _INSTRUMENT_NAMES["map"] and (now - _INSTRUMENT_NAMES["ts"]) < _INSTRUMENT_NAMES_TTL:
                return _INSTRUMENT_NAMES["map"]
        m = {}
        try:
            jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {}) or {}
            for code, info in jy.items():
                c = str(code).split("_")[0]
                nm = str((info or {}).get("name") or "").strip() if isinstance(info, dict) else ""
                if nm and len(c) == 6 and c.isdigit():
                    m[c] = nm
        except Exception:
            pass
        try:
            for c, nm in (NAMES or {}).items():
                c = str(c).split("_")[0]
                if len(c) == 6 and c.isdigit() and nm:
                    m[c] = str(nm)
        except Exception:
            pass
        for fp in (HOLDINGS_MANUAL, STATE_DIR / "holdings_auto.json"):
            try:
                for c, info in (_load_json(fp, {}) or {}).items():
                    c = str(c).split("_")[0]
                    nm = str((info or {}).get("name") or "").strip() if isinstance(info, dict) else ""
                    if nm and len(c) == 6 and c.isdigit():
                        m[c] = nm
            except Exception:
                pass
        with _INSTRUMENT_NAMES_LOCK:
            _INSTRUMENT_NAMES["map"] = m
            _INSTRUMENT_NAMES["ts"] = now
        return m

    @staticmethod
    def _guess_instrument_type(code):
        """裸 6 位码 → 粗类型（本地表没有类型字段；仅用于本地搜索结果的展示）。"""
        c = str(code)
        if c.startswith("bj"):
            return "北交所"
        if c[:2] in ("15", "16", "18") or c[:2] in ("50", "51", "52", "56", "58"):
            return "场内基金"
        return "A股"

    def search_instrument(self, query, limit=12):
        """手动盘「K线速查」：名称/拼音/代码 → 可画 K 线的标的列表。

        口径（为什么这样裁）：
          · **可画**：A股(GP-A，沪深/北交所) / 指数(ZS) / 场内基金(ETF、LOF)；
          · **不可画**：场外基金(jj/KJ，无交易所 K 线)、港美股(hk/us，本仓 K 线链路不支持)
            ⇒ 计入 `excluded` 并在 `note` 里说明，不混进结果让用户点了报错；
          · 本地表**精确名命中**直接回（零网络、秒回）；否则打腾讯 smartbox（带硬超时 4s，
            失败退回本地模糊匹配）。

        返回 {query, results:[{code,name,type,note}], excluded, note, source, error}。**永不抛**。
        `code` 已换成 `openStockChart` 能吃的形式（指数带 sh/sz 前缀，其余裸 6 位）。
        """
        import re as _re
        q = str(query or "").strip()
        out = {"query": q, "results": [], "excluded": 0, "note": "", "source": "", "error": ""}
        if not q:
            out["error"] = "请输入名称或代码"
            return out
        ck = q.lower()
        with _SEARCH_CACHE_LOCK:
            hit = _SEARCH_CACHE.get(ck)
        if hit and (_time_mod.time() - hit[0]) < hit[1]:
            return hit[2]

        names = self._instrument_names()
        by_code = {}
        order = []

        def _add(code, name, typ, note=""):
            code = str(code)
            if code in by_code:
                x = by_code[code]
                if name and (not x["name"] or x["name"] == code):
                    x["name"] = name
                if typ:
                    x["type"] = typ
                return
            by_code[code] = {"code": code, "name": name or code, "type": typ or "标的",
                             "note": note}
            order.append(code)

        mcode = _re.match(r"^(?:(sh|sz|bj))?(\d{6})$", ck)
        _direct = None
        if mcode:
            _pre, _c = mcode.group(1), mcode.group(2)
            if _pre:
                _add(_pre + _c, names.get(_c) or "", "指数")
            else:
                # 裸 6 位码**有歧义**（000001=上证指数/平安银行，000300=沪深300/无此股）
                # ⇒ 先不猜，等在线结果给权威类型；在线失败才退回本地猜测。
                _direct = _c

        # 本地名模糊匹配（精确 > 前缀 > 包含），最多 limit 条
        _q = q.lower()
        _local = []
        for c, nm in names.items():
            _nn = str(nm)
            if _nn == q:
                _local.append((0, c, _nn))
            elif _nn.startswith(q):
                _local.append((1, c, _nn))
            elif q in _nn:
                _local.append((2, c, _nn))
        _local.sort(key=lambda x: (x[0], len(x[2])))
        for _score, c, nm in _local[:limit]:
            _add(c, nm, self._guess_instrument_type(c))

        # 在线搜索：代码输入、或本地没有精确名命中时才打（精确名用户最常用，省一次网络）
        _need_online = bool(mcode) or not any(s == 0 for s, _, _ in _local)
        _online_ok = False          # 在线搜索**成功返回**（哪怕 0 条）：区分"没这只票" vs "网络失败"
        if _need_online:
            try:
                from core.market_data.tencent_provider import search_instruments
                ok, got, _err = _run_bounded(
                    lambda: search_instruments(q, max(int(limit) * 2, 12)), 4.0)
            except Exception:
                ok, got, _err = False, None, True
            _online_ok = bool(ok and not _err)
            if ok and got:
                for it in got:
                    mk = str(it.get("market") or "").lower()
                    cd = str(it.get("code") or "")
                    nm = str(it.get("name") or "")
                    typ = str(it.get("type") or "")
                    if typ.startswith("GP-A") and mk in ("sh", "sz", "bj") and cd.isdigit():
                        _add(cd, nm, "北交所" if mk == "bj" else "A股",
                             "北交所分钟数据源受限" if mk == "bj" else "")
                    elif typ == "ZS" and mk in ("sh", "sz", "bj"):
                        _add(mk + cd, nm, "指数")
                    elif typ in ("ETF", "LOF") and mk in ("sh", "sz"):
                        _add(cd, nm, typ)
                    else:
                        out["excluded"] += 1
                out["source"] = "local+online" if _local else "online"
            else:
                out["source"] = "local"
        else:
            out["source"] = "local"

        if _direct and not order:
            # 裸 6 位码、在线没给结果：不能无脑打开（用户手输错码 688374 会开一张等 6s 的失败图）。
            # ⚠️ 校验放在**在线搜索之后**：000300 这类裸码可能是**指数**（sh000300），
            #    smartbox 能解析；先做股票快照校验会把它误判成"不存在"。
            if _direct in names:
                _add(_direct, names.get(_direct) or "", self._guess_instrument_type(_direct))
            elif _online_ok:
                # 在线成功但 0 条 ⇒ 用一次腾讯快照做存在性校验（有效 ~60ms，无效也快）
                try:
                    from core.market_data.tencent_provider import TencentProvider
                    _okq, _snap, _ = _run_bounded(
                        lambda: TencentProvider().snapshot([_direct]), 2.0)
                except Exception:
                    _okq, _snap = False, None
                if _okq and _snap and _snap.get(_direct):
                    _add(_direct, names.get(_direct) or "", self._guess_instrument_type(_direct))
                else:
                    out["error"] = f"未找到代码 {_direct}（请确认代码是否正确）"
            else:
                # 在线搜索本身失败（网络抖动）⇒ 保留猜测，不因网络问题误拒可能是对的代码
                _add(_direct, names.get(_direct) or "", self._guess_instrument_type(_direct))

        # 2026-10-21 owner：搜「领益制造」0 条（实为「领益智造」，打错一个字）。
        # 精确/前缀/包含 + 腾讯 smartbox **都只做字面匹配** ⇒ 错一个字就全空。
        # 这里再兜一层**共同字**匹配（仅在前面全空时启用，不污染正常搜索）。
        if not order and not mcode and len(q) >= 2:
            _fz = []
            _qs = set(q)
            for c, nm in names.items():
                _nn = str(nm or "")
                if not _nn:
                    continue
                _pre = 0
                for _a, _b in zip(q, _nn):
                    if _a != _b:
                        break
                    _pre += 1
                _shared = sum(1 for ch in _qs if ch in _nn)
                # ≥2 字共同前缀，或「几乎全部字都在名字里」——两个条件都偏严，避免噪声
                if _pre >= 2 or _shared >= max(3, len(q) - 1):
                    _fz.append((_pre, _shared, len(_nn), c, _nn))
            _fz.sort(key=lambda x: (-x[0], -x[1], x[2]))
            for _pre, _sh, _ln, c, nm in _fz[:max(int(limit), 1)]:
                _add(c, nm, self._guess_instrument_type(c), "模糊匹配")
            if order:
                out["source"] = (out.get("source") or "local") + "+fuzzy"
                out["note"] = "原词没搜到，以下是名称最接近的（请确认是不是这只）"

        results = [by_code[c] for c in order][:max(int(limit), 1)]
        out["results"] = results
        if out["excluded"]:
            out["note"] = f"另有 {out['excluded']} 条场外基金/港美股结果（暂无 K 线）"
        elif not results:
            out["note"] = "未找到可画 K 线的标的（支持 A股 / ETF / LOF / 指数）"
        out = _clean(out)
        with _SEARCH_CACHE_LOCK:
            if len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
                _SEARCH_CACHE.clear()
            # 有结果缓存 10 分钟；空结果只缓存 60 秒（网络抖动后能快速重试）
            _SEARCH_CACHE[ck] = (_time_mod.time(),
                                 _SEARCH_CACHE_TTL if results else 60.0, out)
        return out

    # ---------- 公司资料（K 线弹窗「📋 公司资料」） ----------

    def _em_secid(self, code):
        """6 位码 → 东财 secid。沪市=1.，深市/北交所=0.（东财北交所亦用 0.）。"""
        from core.market_data.codec import market_of
        c = str(code).split("_")[0]
        return ("1." if market_of(c) == "SH" else "0.") + c

    def _em_stock_boards(self, code):
        """东财「所属板块」→ [{name, kind}]；取不到返回 []（永不抛）。

        ⚠️ 只走 `push2.eastmoney.com/api/qt/slist/get`：2026-09-29 实测本机
        `push2his`（K线）**整体不可达**（对照组平安银行亦 RemoteDisconnected，已排除代理），
        `clist`/`stock/get` 同样被风控，只有 `slist/get` 稳定可用。故不依赖其它东财主机。

        `kind` 是**板块名启发式**（含"概念"→概念；以"板块"结尾→地域；其余→其他），
        **不是**东财语义 —— 该接口不带板块类型字段（实测 f13/f152 恒定）。界面上如实标注。
        """
        import os as _os
        import time as _t
        import urllib.request as _ur
        c = str(code).split("_")[0]
        _hit = Api._profile_cache.get(c)
        if _hit and (_t.time() - _hit[0]) < Api._PROFILE_TTL:
            return _hit[1]
        for _k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"):
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"
        url = ("https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2"
               f"&fields=f12,f14&secid={self._em_secid(c)}&pn=1&np=1&pz=100")
        for _ in range(8):                       # 东财间歇风控，仓库既有对策=8 次重试
            try:
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://quote.eastmoney.com/"})
                data = (json.loads(_ur.urlopen(req, timeout=6).read()
                                   .decode("utf-8", errors="ignore")).get("data") or {})
                diff = data.get("diff") or []
                boards = []
                for x in diff:
                    nm = str(x.get("f14") or "").strip()
                    if not nm:
                        continue
                    if "概念" in nm:
                        kind = "概念"
                    elif nm.endswith("板块"):
                        kind = "地域"
                    else:
                        kind = "其他"
                    boards.append({"name": nm, "kind": kind})
                if boards:
                    Api._profile_cache[c] = (_t.time(), boards)
                    return boards
            except Exception:
                _t.sleep(0.6)
        Api._profile_cache[c] = (_t.time(), [])   # 负缓存：别让每次点开都等 8 次重试
        return []

    # ---------- 公司简介（2026-10-17）：东财 F10「核心题材」补全 ----------
    # 本地 `business_summary` 只有一句"主营专用设备制造业"（东财旧 F10 被风控 ⇒ 长期没更新），
    # owner 要求"通过查询提前补充"。数据源实测可用：
    #   https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax?code=SH688499
    #   → hxtc[] = [{KEYWORD, MAINPOINT_CONTENT}]，第 1 项是**经营范围**，第 2 项起是
    #     "主营业务/业务描述"（三只样本一致：关键词就是业务名），后面是行业/竞争优势条目。
    # 落盘缓存 `t_io/cache/company_profile.json`；GUI 读缓存，缺的**后台补**（不阻塞主线程）。
    def _fetch_company_profile(self, code):
        """抓一只票的简介。**网络调用** ⇒ 只许在后台线程/预取脚本里调。返回 dict 或 None。"""
        c = str(code).split("_")[0]
        if len(c) != 6 or not c.isdigit():
            return None
        secucode = ("SH" if c[0] in "56" else ("BJ" if c[0] in "48" or c.startswith("920") else "SZ")) + c
        path = "/PC_HSF10/CoreConception/PageAjax?code=" + secucode
        try:
            # ⚠️ 东财**有些响应是 gzip 压缩的**（实测 002639/601318 等 4/52 只返回 `\x1f\x8b`），
            # urllib 不会自动解压 ⇒ 直接 decode 会得到乱码、json 解析失败。
            # 双保险：请求头显式要 identity，再加魔数兜底解压。
            # 2026-10-20：改走共享 keep-alive 连接池 —— 原来裸 urlopen 每只票都要重新
            # DNS+TCP+TLS，实测首只 **969ms**、复用连接后 **140~170ms**（差 6 倍）。
            # "业务简介特别慢"就是这一跳。
            from core.market_data.http_pool import pooled_get_raw
            raw = pooled_get_raw("emweb.securities.eastmoney.com", path, 10, {
                "User-Agent": "Mozilla/5.0", "Referer": "http://quote.eastmoney.com/",
                "Accept-Encoding": "identity"})
            if raw[:2] == b"\x1f\x8b":
                import gzip
                raw = gzip.decompress(raw)
            d = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return None
        items = [{"kw": str(x.get("KEYWORD") or "").strip(),
                  "text": str(x.get("MAINPOINT_CONTENT") or "").strip()}
                 for x in (d.get("hxtc") or []) if isinstance(x, dict)]
        items = [x for x in items if x["kw"] and x["text"]]
        if not items:
            return None
        scope = next((x["text"] for x in items if x["kw"] == "经营范围"), "")
        rest = [x for x in items if x["kw"] != "经营范围"]
        brief = rest[0] if rest else {"kw": "", "text": ""}
        return {"code": c,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "brief": brief["text"], "brief_title": brief["kw"],
                "scope": scope,
                "points": rest[1:9]}          # 行业/竞争优势，最多 8 条

    def _company_profile(self, code, allow_fetch=True):
        """读简介缓存；未命中则（可选）kick 后台抓取，返回 (profile|None, pending)。"""
        c = str(code).split("_")[0]
        # 指数/东财合成标的（sh000001、em47.800005…）没有 F10 简介 ⇒ 直接判无，
        # 别为它起一个必然失败的后台线程、再打一条"抓取为空"的警告。
        if len(c) != 6 or not c.isdigit():
            return None, False
        cache = _load_json(BASE / "t_io" / "cache" / "company_profile.json", {}) or {}
        hit = cache.get(c)
        if isinstance(hit, dict) and hit.get("brief"):
            return hit, False
        if not allow_fetch:
            return None, False
        # 单飞：同一只票不并发重复抓
        inflight = getattr(self, "_prof_inflight", None)
        if inflight is None:
            inflight = self._prof_inflight = set()
        if c in inflight:
            return None, True
        inflight.add(c)

        def _bg():
            # ⚠️ 这里**不许裸 except: pass**（2026-10-20 排查"业务简介一直不补上"时踩到）：
            #    线上真凶是本函数里 `os.replace` 抛 NameError —— **t_gui 当时没有 module 级
            #    `import os`**，而异常被 `except: pass` 吞掉 ⇒ 缓存从 2026-10-17 起**一次都没写进去过**，
            #    界面永远停在「⏳ 正在查询完整简介」。静默失败查了两小时，最后靠"手工复刻这段代码"
            #    才看出差一个 import。教训：后台线程的失败必须留痕。
            try:
                p = self._fetch_company_profile(c)
                if p:
                    _cur = _load_json(BASE / "t_io" / "cache" / "company_profile.json", {}) or {}
                    _cur[c] = p
                    _fp = BASE / "t_io" / "cache" / "company_profile.json"
                    _fp.parent.mkdir(parents=True, exist_ok=True)
                    _tmp = str(_fp) + ".tmp"
                    with open(_tmp, "w", encoding="utf-8") as f:
                        json.dump(_cur, f, ensure_ascii=False)
                    os.replace(_tmp, str(_fp))
                else:
                    print(f"[WARN] [公司简介] {c} 抓取为空（东财风控/网络），本次不缓存")
            except Exception as e:
                print(f"[WARN] [公司简介] {c} 后台抓取/落盘失败: {type(e).__name__}: {str(e)[:160]}")
            finally:
                inflight.discard(c)
        threading.Thread(target=_bg, name="company-profile", daemon=True).start()
        return None, True

    def load_stock_profile(self, code, live=False):
        """公司资料。两层（owner 2026-09-29：本地先显示 + 按钮拉实时）：

          - **本地层**（`live=False`，秒开、零网络）：`watchlist_jiuyan.json` 的
            `business_summary`（主营一句话）、`sector`（`/` 拼接的行业+概念混列）、
            `_jiuyan_concepts`（韭研概念）
          - **实时层**（`live=True`）：东财「所属板块」（见 `_em_stock_boards`）

        三条口径说明（避免过度承诺）：
          - 东财板块列表不带类型字段 ⇒ 行业/概念/地域只能按**板块名启发式**划分，界面标注。
          - 本地 `sector` 是历史遗留混合串，行业与概念混在一起，**原样展示、不擅自归类**。
          - 该文件的 `concept_boards`/`industry_boards` 字段 **0/5041 全空**（从未写入），故不读。

        与 `load_stock_chart` 同契约：**永不抛**，失败置 `error` 且 `ok=False`。
        """
        out = {"code": str(code), "name": None, "ok": False, "error": None,
               "business_summary": "", "sector": "", "sector_list": [],
               "local_concepts": [], "boards": [], "boards_source": None,
               "local_role": None, "local_role_reason": None,
               "profile": None, "profile_pending": False,
               "boards_note": "东财口径；行业/概念/地域按板块名启发式划分"}
        try:
            jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
            info = jy.get(str(code).split("_")[0]) if isinstance(jy, dict) else None
            if isinstance(info, dict):
                out["name"] = info.get("name")
                out["business_summary"] = str(info.get("business_summary") or "").strip()
                sec = str(info.get("sector") or "").strip()
                out["sector"] = sec
                out["sector_list"] = [s.strip() for s in sec.split("/") if s.strip()]
                out["local_concepts"] = [s for s in _jiuyan_concepts(info).split("|") if s]
                # 2026-10-19：概念注释 {概念: 解释} 下发（前端 chip 悬停显示，医药板块起）
                out["concept_descs"] = _jiuyan_concept_descs(info, out["local_concepts"])
                # 2026-10-24：逐股「为什么算这个概念」（数据要素板块起），资料面板 chip 悬停用
                out["concept_reasons"] = _jiuyan_concept_reasons(info, out["local_concepts"])
                # 2026-10-20：研究核实的静态 龙头/中军（机器人/军工板块整治起）——资料面板徽章
                _role = str(info.get("jiuyan_role") or "").strip()
                if _role in ("龙头", "中军"):
                    out["local_role"] = _role
                    out["local_role_reason"] = str(info.get("jiuyan_role_reason") or "").strip()
            out["ok"] = True
        except Exception as e:
            out["error"] = f"读取本地资料失败: {e}"
            return _clean(out)

        # 公司简介（2026-10-17）：本地一句“主营X”太薄 ⇒ 读 F10 缓存；没缓存就后台补
        try:
            _prof, _pend = self._company_profile(str(code))
            out["profile"] = _prof
            out["profile_pending"] = bool(_pend)
        except Exception:
            out["profile"] = None
            out["profile_pending"] = False

        if live:
            try:
                boards = self._em_stock_boards(str(code))
                if boards:
                    out["boards"] = boards
                    out["boards_source"] = "em"
                else:
                    out["error"] = "东财所属板块取不到（风控或网络）"
            except Exception as e:                # 兜底：本地层仍可用
                out["error"] = f"东财取数失败: {e}"
        return _clean(out)

    # ---------- K线 cache-first（2026-10-04） ----------
    def _cache_chart(self, key, res):
        """写内存图表缓存（容量上限 500，防全池无界增长）。"""
        if not hasattr(self, "_stock_chart_cache"):
            self._stock_chart_cache = {}
        if len(self._stock_chart_cache) > 500:
            self._stock_chart_cache.clear()
        self._stock_chart_cache[key] = (datetime.now(), res)


    def warm_stock_profile(self, code):
        """**后台**预取公司简介（K 线弹窗一打开就调）。立即返回，不等网络。

        2026-10-20 owner：「业务简介特别的慢」。慢在**缓存没命中**：`_company_profile`
        发现 miss 才 kick 后台抓取，而用户往往开图后立刻点「📋 公司资料」⇒ 正好撞在
        "还没抓完"那一段上。开图就先预取，把这段等待挪到用户看图的几秒里。
        已在缓存/正在抓 ⇒ 什么都不做（`inflight` 单飞）。
        """
        try:
            self._company_profile(str(code))
        except Exception:
            pass
        return {"ok": True, "code": str(code)}


    def _handshake(self, res, version):
        """版本握手：数据未变只回 {unchanged:true}（省掉 ~300KB 过桥 + 重画）。"""
        _v = res.get("version")
        if version is not None and _v is not None and version == _v:
            return {"code": res.get("code"), "available": True, "unchanged": True, "version": _v}
        return res

    def _serve_payload_cache(self, code, version, want_minutes=False, allow_network=False,
                             period=None):
        """盘后预下载/机会式落盘的图表 payload 命中 → 瞬开结果；未命中返回 None。

        want_minutes 但 payload 内无分时 ⇒ 返回 None（落到重建带分时）。
        `period`（2026-10-20）：只要求**当前切换到的那个分钟档**在 payload 里即可。
        原来要求三档齐全 ⇒ 打开一个分钟 Tab 会串行抓三档（4.8s）；现在按需只抓一档。
        period 为空（预下载/旧调用）仍按三档齐全判。
        allow_network：payload 的日线停在上一交易日时，是否允许拉一次**单只快照**
        补当日 forming bar（K 线弹窗自己那一只：True；批量/热路径：False）。
        """
        try:
            import core.chart_cache as _cc
            hit = _cc.load_payload(code)
            if not hit:
                return None
            # schema<4 的旧档一律不可信 ⇒ 重建一次：
            #   · <2：10min 合成有 bug / 不足 30 根；
            #   · <3：科创板(688)的**成交量单位没归一**（腾讯按股、其余按手，差 100 倍）⇒
            #     那一档算出来的成交额能到 2000 亿（owner 截图）。这条闸原来只判在
            #     `want_minutes` 里面，日 K 视图照样会回放旧档 ⇒ 现在**所有视图**都拦。
            #   · <4（2026-10-30）：min5/min10 只有 320 根；展示档已加深到近 3 个月
            #     （MIN_BARS_CHART 3200/1600），旧档切到 5/10 分 Tab 时按需重建一次。
            if int(hit.get("schema") or 0) < 4:
                return None
            # 2026-10-21：payload 里若含**盘中半成品根**（相邻日跳空超过涨跌停上限）⇒ 不可回放。
            # 现场：002929 的 payload 存着 10-20 收 46.10（真实是涨停 51.04），而日线缓存已修好；
            # 若只比"日期/长度"，payload 看着仍是当日的、会被直接回放 ⇒ 图上永远显示错的那根。
            try:
                from core.market_data.tencent_provider import has_impossible_gap
                if has_impossible_gap(hit.get("daily_rows") or [], code):
                    return None
            except Exception:
                pass
            if want_minutes:
                _pd = (hit.get("payload") or {}).get("period_data") or {}
                _need = period if period in _MIN_PERIODS else None
                if _need:
                    if not _pd.get(_need):
                        return None
                elif not all(_pd.get(k) for k in ("min10", "min30", "min60")):
                    return None
            # 2026-10-08: payload 比**日线缓存**旧 ⇒ 判过期、回落日线缓存路径（零网络、序列完整）。
            # 背景：`MAX_DISPLAY_GAP_DAYS=12` 容忍长假，会把「缺了 1 个交易日」的 payload 也当新鲜
            # 直接回放（实测 600362：payload 停在 09-30、日线缓存已到 10-08 ⇒ MA5 用旧序列 ⇒ 误报破5日线）。
            try:
                _dfc = _cc.load_daily_display(code)
                if _dfc is not None and not _dfc.empty:
                    _prows0 = hit.get("daily_rows") or []
                    # 2026-10-20（owner：「日线只显示一部分」）：日线缓存可能是**短窗口**
                    # （扫描写 30~40 根）却比 payload 新 1 天 ⇒ 旧逻辑直接重建，
                    # 用短缓存建出 40 根图、再覆盖掉 800 根的 payload ⇒ 永久只剩一小段。
                    # 缓存明显比 payload 短 ⇒ 不采信它的"更新"，继续回放长 payload。
                    _cache_short = bool(_prows0) and len(_dfc) * 2 < len(_prows0)
                    if (str(hit.get("last_daily_date") or "") < str(_dfc["date"].iloc[-1])[:10]
                            and not _cache_short):
                        return None
                    # 2026-10-12: 除了"更新"，还要看"**更少**"。日线缓存被短窗口写入截短过之后
                    # （`save_daily_cache` 曾是盲覆盖），payload 会带着**残缺序列**被当成新鲜直接回放
                    # —— 日期闸拦不住（实测 600362：缓存 801 根、payload 只 21 根，
                    # 两者末根都是 10-12 ⇒ 判"不旧" ⇒ 回放 21 根 ⇒ 持仓体检 `n < 30` 静默丢行，
                    # owner 报「少了江西铜业」）。这里顺手判长度 ⇒ 触发重建。
                    # 正常情况两者**同为完整序列**（长度≈相等），只有被截短才会差出量级。
                    _prows = hit.get("daily_rows") or []
                    if _prows and len(_prows) * 2 < len(_dfc):
                        return None
            except Exception:
                pass
            res = self._payload_to_result(
                hit, code, allow_network=allow_network,
                strip_minutes=not want_minutes,
                keep_minute=(period if want_minutes else None))
            if not res or not res.get("available"):
                return None
            self._cache_chart(f"{datetime.now().strftime('%Y-%m-%d')}_{code}", res)
            return self._handshake(res, version)
        except Exception:
            return None

    def _fetch_daily_bounded(self, code, timeout=6.0, min_rows=0):
        """带硬超时的日线取数（2026-10-06）：防止任何慢/挂死的数据源把 pywebview 主线程冻住。
        超时返回 None（取数线程继续在后台跑完，不回收）。指数走 `index_daily`（`daily` 解不了指数码）。

        2026-10-18：**K 线弹窗的单只取数改为腾讯优先**。实测同一台机器 `facade.daily()`（GM 优先）
        对单只在 **0.8~6.6s** 之间波动（GM 单 worker + 4.5s 限流窗）；而腾讯日线走 keep-alive
        + 磁盘缓存，通常 <0.5s。K 线是展示用途，不需要 GM 的"与自动盘同源"保证。
        对**本地名表不认识的码**（用户手输错码，如 688374）腾讯返回空即**快速失败**——
        不再白等 GM 的限流窗（实测从 6.4s 降到 <1s）。"""
        import concurrent.futures as _cf
        _c = str(code).split("_")[0]
        _is_index = _c[:2].lower() in ("sh", "sz", "bj") and _c[2:].isdigit()
        try:
            from core.market_data import get_provider
            from core.position_builder import fetch_daily_kline
        except Exception:
            return None

        def _go():
            if _is_index:
                # 传 end_date ⇒ provider **不回写**共享长历史缓存（否则 400 行会静默截短 index_*.json）
                return get_provider().index_daily(_c, 400,
                                                  end_date=datetime.now().strftime("%Y-%m-%d"))
            try:
                from core.market_data.tencent_provider import TencentProvider
                d = TencentProvider().daily(_c, min_rows=int(min_rows or 0))
                if d is not None and not d.empty:
                    return d
                # 腾讯**明确返空**（源里没有这只票，如手输错码 688374）⇒ 返回**空帧**（不是 None），
                # 让调用方区分"明确没数据"与"超时"，从而快速失败、不再等 GM 的 4.5s 限流窗。
                return d
            except Exception:
                pass
            # 腾讯**异常**（网络/WAF）才走 GM 兜底 —— 此时 GM 有可能救回
            return fetch_daily_kline(_c)
        ex = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            return ex.submit(_go).result(timeout=timeout)
        except Exception:
            return None
        finally:
            ex.shutdown(wait=False)

    def _daily_rows_cache_first(self, code, allow_network=False):
        """盘后预下载的日线历史缓存（+当日 forming bar）→ rows；miss 返回 []。

        补当日 bar **复用 `_today_forming_bar`（走 load_quotes 批量快照）**，不用
        `facade.append_forming_bar` —— 后者会**逐只单打一次网络快照**（实测 ~1.9s/只）。
        allow_network=True 时，不在批量快照池里的票会单独打一次快照（K 线弹窗自己那一只）。
        """
        try:
            import core.chart_cache as _cc
            df = _cc.load_daily_display(code)
            if df is None or df.empty:
                return []
            rows = [{"date": str(_r.date), "open": float(_r.open), "close": float(_r.close),
                     "high": float(_r.high), "low": float(_r.low), "volume": float(_r.volume)}
                    for _r in df.itertuples(index=False)]
            try:
                _now = datetime.now()
                _today = _now.strftime("%Y-%m-%d")
                _last = str(rows[-1]["date"]) if rows else ""
                # 周末/盘前**不补**：那时的快照 `ts_date` 必然是上一交易日，白打一次网络
                # （原来每次开图都要多 ~0.3s，周末尤其明显）。
                _can_probe = (_now.weekday() < 5 and _now.strftime("%H:%M") >= "09:15")
                if _last < _today and _can_probe:
                    bar = self._today_forming_bar(code, allow_network=allow_network)
                    if bar and str(bar["date"]) > _last:
                        rows.append(bar)
            except Exception:
                pass
            return rows
        except Exception:
            return []

    def _payload_to_result(self, hit, code, allow_network=False, strip_minutes=False,
                           keep_minute=None):
        """payload 缓存 → 结果 dict。日内（可能出新 bar）把当日那根补进 daily 序列并重算日线指标；
        非交易时段直接返回收盘态（真·瞬开）。其他周期/箱体/通道沿用收盘态（≤1 根偏差）。

        `strip_minutes`（2026-10-20）：日/周/月视图不把 10/30/60 分序列发给前端
        （payload 里三档 320 根 ≈ 2/3 体积，桥传输+首帧渲染都变慢）；切到分钟 Tab 时
        前端会带 period 再取一次，这样首屏只传当前周期。
        """
        import copy
        payload = copy.deepcopy(hit.get("payload") or {})
        if not payload:
            return None
        payload["code"] = code
        payload.setdefault("name", code)
        # 买卖点标记（2026-10-21 owner）：成交台账按 code 精简行挂顶层 trades 键。
        # 放最前面：后面 `_rebuild_payload_daily` 重算 version 时要把它混进签名。
        payload["trades"] = _chart_trades(code)
        daily_rows = list(hit.get("daily_rows") or [])
        last = str(hit.get("last_daily_date") or "")
        _now = datetime.now()
        _today = _now.strftime("%Y-%m-%d")
        _in_session = (_now.weekday() < 5 and _now.strftime("%H:%M") >= "09:30")
        _new_bar_possible = (last < _today and _in_session)
        # 2026-10-20（688003 天准科技：已站上 10 日线但日线显示在下方）：
        # **payload 里已经带了今天那一根**时，原来这条分支直接跳过 —— 可那根是开盘时
        # 写下的半成品（实测 09:31 的 close=61.08，当日实际 66.42），而 payload 的新鲜度
        # 只看 `last_daily_date == 今天` ⇒ 一整天不再更新，均线全按 9 小时前的价算。
        # 所以 **last == 今天** 时同样要拿实时快照把那一根**替换**掉。
        _today_bar_refresh = (last == _today and _in_session)
        if _new_bar_possible or _today_bar_refresh:
            bar = self._today_forming_bar(code, allow_network=allow_network)
            if bar and str(bar["date"]) == last and daily_rows:
                try:
                    self._rebuild_payload_daily(payload, daily_rows[:-1] + [bar])   # 覆盖半成品
                except Exception:
                    pass
            elif bar and str(bar["date"]) > last:
                try:
                    self._rebuild_payload_daily(payload, daily_rows + [bar])
                except Exception:
                    pass
        _pdta = payload.get("period_data") or {}
        if strip_minutes:
            for _k in _MIN_PERIODS:
                _pdta.pop(_k, None)
        elif keep_minute in _MIN_PERIODS:
            # 分钟视图只带当前 Tab 的那一档（磁盘 payload 仍是各档齐全，切 Tab 再按需取）
            for _k in _MIN_PERIODS:
                if _k != keep_minute:
                    _pdta.pop(_k, None)
        # 老 payload 里没有 amount（成交额是 2026-10-20 新增的）⇒ 就地用 volume+ohlc 补，
        # 不必等重建。口径与 `_ensure_amount` 完全一致：(high+low+close)/3 × volume × 100。
        # `_to_series` 的 ohlc = [open, close, low, high]。
        for _k in ("daily",) + _MIN_PERIODS:
            _s = _pdta.get(_k) or {}
            # 指数不补成交额（点位 × 量没有意义）
            if (_s and not _index_code(code) and "amount" not in _s
                    and _s.get("volume") and _s.get("ohlc")):
                try:
                    _s["amount"] = [round((o[3] + o[2] + o[1]) / 3.0 * float(v) * 100.0)
                                    for v, o in zip(_s["volume"], _s["ohlc"])]
                except Exception:
                    pass
        # 重算 version：磁盘 payload 里的旧版本号不含 trades 段；必须混入成交签名
        # （条数 + 末笔 ts），否则新导入的成交被前端「unchanged 不刷新」握手吞掉。
        payload["version"] = self._chart_version(_pdta, payload.get("trades"))
        return _attach_trendlines(_attach_divergence(payload))


    def _mk_forming_bar(self, today, px, o, h, l, v):
        """造「当日 forming bar」；**无成交且无振幅 ⇒ 返回 None**（视为还没开始交易）。

        ⚠️ 2026-10-11 事故：集合竞价后、开盘前（如 09:28）快照会给出 price=**开盘价**、vol=0，
        照此造 bar 得到 `O=H=L=C`、`volume=0` 的假当日根，被写进 chart_payload。
        实测 600362：假根 close=41.5 ⇒ cur(41.5) < MA5(41.57) ⇒ **误报「破5日线」**，
        而真实 10-09 收盘是 42.51（站上）。旧代码只闸 `_px > 0`，拦不住这种盘前占位。
        判据用「成交量≤0 **且** 高==低」——不依赖 vol_hand 字段存在（缺字段时不会误杀正常盘中根）。
        """
        if px is None or px <= 0:
            return None
        o = o or px
        h = h or px
        l = l or px
        v = v or 0.0
        if float(v) <= 0 and float(h) == float(l):
            return None
        return {"date": today, "open": o, "high": h, "low": l, "close": px, "volume": v}

    def _today_forming_bar(self, code, allow_network=True):
        """腾讯快照 → 当日 forming bar（ts_date 闸，与 facade._maybe_append_forming 同口径）。

        2026-10-06：**优先复用 `load_quotes` 每 10s 批量拉的快照**（含 ts_date/open/high/low/vol_hand）
        —— 小池标的零额外网络；快照非当日（休市/假期）直接判「不补」，不再白打一次 1.9s 的单只快照。

        allow_network=False：不在批量快照内就**不单独打网络**（快路径用，保证打开即出图）。
        """
        try:
            base = str(code).split("_")[0]
            today = datetime.now().strftime("%Y-%m-%d")
            _c = getattr(self, "_quotes_snaps", None)
            if _c and (_time_mod.time() - _c[0]) < 120:
                _s = (_c[1] or {}).get(base)
                if _s:
                    if _s.get("ts_date") != today:
                        return None       # 快照非当日 ⇒ 休市/假期，不补（免无谓网络）
                    _px = _s.get("price") or 0
                    if _px > 0:
                        # ⚠️ 批量快照的 `vol_hand` 对**科创板(688)是「股」**（腾讯按板块给不同单位），
                        #    而 forming bar 要写进按「手」的日线序列 ⇒ 这里必须 ÷100。
                        #    这就是 owner 截图里「10-20 那根成交额画到 2000 亿」的直接来源：
                        #    该票在批量快照池里（持仓/watchlist），拿到的是 24,597,313 股。
                        _v = _s.get("vol_hand")
                        if _v and base.startswith("688"):
                            try:
                                _v = float(_v) / 100.0
                            except (TypeError, ValueError):
                                _v = None
                        return self._mk_forming_bar(today, _px, _s.get("open"), _s.get("high"),
                                                    _s.get("low"), _v)
            if not allow_network:
                return None               # 快路径：不在批量快照内即不补（不打网络）
            # 回退：单只快照（网络）。**2026-10-21：腾讯优先，GM 只做兜底。**
            # 为什么：`get_provider().snapshot()` 是 GM 优先，而 GM 调用池**只有 1 个 worker**
            # （`facade._gm_call`，为硬超时故意如此）。开 K 线时若后台预热/标签/扫描正在排队，
            # 这一只快照就得排在**它们后面**——实测开图时本函数耗时 **8.5s**（单独跑只要 0.3s），
            # 是 `load_stock_chart` 越过前端 20s 投递上限的主项（`bench_boot5` 分阶段计时）。
            # K 线弹窗是展示用途，不需要"与自动盘同源"的 GM 保证 —— 与 2026-10-18 日线取数
            # 改腾讯优先同一取舍；腾讯直连也绕开 `_gm_ok` 的半开探针窗。
            snap, _from_tx = None, False
            try:
                from core.market_data.tencent_provider import TencentProvider
                snap = TencentProvider().snapshot([base]).get(base)
                _from_tx = True
            except Exception:
                snap = None
            if not snap or not snap.get("price"):
                from core.market_data import get_provider
                snap, _from_tx = get_provider().snapshot([base]).get(base), False
            if not snap or not snap.get("price"):
                return None
            if snap.get("ts_date") != today:
                return None
            px = snap["price"]
            _v = snap.get("volume")
            # 腾讯 f[6] 成交量**按板块不同单位**：主板/创业板/ETF 是「手」、科创板 688 是「股」，
            # 差 100 倍（memory: qt_quote_volume_unit_by_board）。forming bar 要写进按「手」的
            # 日线序列 ⇒ 688 必须 ÷100（与上面批量快照分支同口径）。GM 那份本来就是手，别再除。
            if _from_tx and _v and str(base).startswith("688"):
                try:
                    _v = float(_v) / 100.0
                except (TypeError, ValueError):
                    _v = None
            return self._mk_forming_bar(today, px, snap.get("open"), snap.get("high"),
                                        snap.get("low"), _v)
        except Exception:
            return None

    def _rebuild_payload_daily(self, payload, daily_rows):
        """用 raw daily rows 重算日线序列，替换 payload 的 daily + current_price + version。"""
        import pandas as pd
        df = pd.DataFrame(daily_rows)
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        d = _calc_ma_and_indicators(df)
        payload["period_data"]["daily"] = _to_series(d)
        payload["current_price"] = round(float(d["close"].iloc[-1]), 3)
        # trades 段必须与两条建图路径同口径（`_payload_to_result` 已先把 trades 挂进 payload）
        payload["version"] = self._chart_version(payload["period_data"],
                                                 payload.get("trades"))

    def _patch_daily_today(self, df, code):
        """日线序列里「今天那一根」用**实时快照**覆盖掉缓存里的半成品（2026-10-20）。

        为什么必须有（owner 报 688003 天准科技「已经爬上了 10 日均线，日线显示不对」）：
        本地日线缓存/图表 payload 的当日根是**开盘那会儿**写下的快照（实测 688003 的缓存
        09:31:26 落盘，close=61.08 / high=61.99），而 `daily_kline` 的新鲜度闸只看
        `date == 今天` ⇒ 一整天都被当成"新鲜"不再更新。于是 MA5/MA10/MA20、current_price、
        支撑压力/箱体全部按 9 小时前的价格算；真实当日 66.42、high 67.98 ⇒ 明明站上 MA10
        却画在下方。

        边界：只在「末根 == 今天 且 已开盘」时动手；同票 20 秒内复用快照（体检会逐只调用，
        别把批量快照打爆）。**取不到快照 / 日期对不上 ⇒ 原样返回**，绝不猜、不造根
        （沿用 `_mk_forming_bar` 的盘前假根闸）。
        """
        try:
            if df is None or getattr(df, "empty", True) or "date" not in df.columns:
                return df
            _now = datetime.now()
            if _now.weekday() >= 5 or _now.strftime("%H:%M") < "09:30":
                return df
            _today = _now.strftime("%Y-%m-%d")
            if str(df["date"].iloc[-1])[:10] != _today:
                return df          # 末根不是今天 ⇒ 交给"补当日根"那套逻辑，本函数不造根
            _key = str(code).split("_")[0]
            _memo = getattr(self, "_daily_patch_memo", None)
            if _memo is None:
                _memo = self._daily_patch_memo = {}
            _ts = _time_mod.time()
            _hit = _memo.get(_key)
            if _hit is not None and (_ts - _hit[0]) < 20:
                bar = _hit[1]
            else:
                bar = self._today_forming_bar(code, allow_network=True)
                _memo[_key] = (_ts, bar)
            if not bar or str(bar.get("date")) != _today:
                return df
            out = df.copy()
            _i = out.index[-1]
            for _k in ("open", "high", "low", "close", "volume"):
                if _k in out.columns and bar.get(_k) is not None:
                    out.at[_i, _k] = float(bar[_k])
            return out
        except Exception:
            return df

    def _build_chart_from_df(self, df, out, code, min_frames=None, want_min_period=None):
        """由日线 DataFrame 构建 K 线弹窗数据（MA/MACD/RSI/BOLL + 周/月/30分/60分 + 支撑箱体通道）。

        min_frames: {"min30": df, "min60": df} 直接注入（测试用，避免联网）；None → 自行取数
        （仅普通个股；指数/em 标的跳过 ⇒ 这两个 Tab 显示「无分时数据」）。
        注：levels/boxes/channel/current_price **仍只用日线算、全 Tab 共用**（owner 2026-10-04 决定）。
        """
        import pandas as pd

        calc_ma_and_indicators = _calc_ma_and_indicators   # 模块级（2026-10-04 抽出，供 payload 补 bar 复用）

        # 2026-10-20（owner 报 688003 天准科技「已经站上 10 日线，日线却显示不对」）：
        # 日线序列里**今天那一根**常常是缓存里 **09:3x 写下的半成品**（实测 close 61.08，
        # 而当日实际 66.42，高 67.98）——所有均线都建立在它之上 ⇒ 整张图按 9 小时前的价算。
        # 这里在算指标**之前**用实时快照把当日根换掉（取不到就原样，绝不猜）。
        df = self._patch_daily_today(df, code)
        df = _ensure_amount(df, code)    # 成交额（2026-10-20：面板改画成交额）
        daily = calc_ma_and_indicators(df)
        # 指数没有 amount（见 `_ensure_amount`）⇒ 聚合字典要按**实际列**拼，不能写死
        _agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        if "amount" in df.columns:
            _agg["amount"] = "sum"
        weekly = calc_ma_and_indicators(df.resample("W-FRI", on="date").agg(
            _agg).dropna().reset_index())
        # 月线频率兼容：pandas≥2.2 用 "ME"，旧版用 "M"
        # 2026-08-24 fix: pandas 3.x 次版本为 0，原 split[1]>="2" 误判为旧 "M"(已移除)；用版本元组比较
        _maj, _min = (int(x) for x in pd.__version__.split(".")[:2])
        _month_freq = "ME" if (_maj, _min) >= (2, 2) else "M"
        monthly = calc_ma_and_indicators(df.resample(_month_freq, on="date").agg(
            _agg).dropna().reset_index())

        to_series = _to_series   # 模块级（2026-10-04 抽出）

        out["period_data"] = {
            "daily": to_series(daily),
            "weekly": to_series(weekly),
            "monthly": to_series(monthly),
        }

        # 30分/60分（2026-10-04）：原生分钟线，日期带时间。取不到就**不放这两个键**，
        # 前端据此显示「该周期无分时数据」，而不是抛错。
        _raw = min_frames
        if _raw is None:
            _raw = {}
            _c = str(code).split("_")[0]
            if not _c.startswith("em"):      # em 是合成标的(如A股平均股价)，无分时源
                # 2026-10-20 提速：三个分钟档**并行取**。原来串行 3×~1.6s=4.8s，
                # 是 `load_stock_chart 4839ms`（gui_slow_calls.log）的主要来源。
                _jobs = tuple((k, _MIN_PERIOD_FREQ[k]) for k in _MIN_PERIODS)
                if want_min_period in _MIN_PERIODS:
                    # 2026-10-20：按需只取当前 Tab 的分钟档（切到哪个抓哪个），
                    # 不再为了一个 Tab 把各档全抓（首次切分钟从 ~4.8s 降到 ~1s 内）。
                    _jobs = ((want_min_period, _MIN_PERIOD_FREQ[want_min_period]),)
                try:
                    import concurrent.futures as _cf
                    with _cf.ThreadPoolExecutor(max_workers=3,
                                                thread_name_prefix="minfetch") as _ex:
                        _futs = {k: _ex.submit(_fetch_min_bars, _c, f) for k, f in _jobs}
                        for k, _fu in _futs.items():
                            try:
                                _raw[k] = _fu.result()
                            except Exception:
                                _raw[k] = None
                except Exception:
                    for _key, _freq in _jobs:      # 线程池不可用：退回串行（行为不变）
                        try:
                            _raw[_key] = _fetch_min_bars(_c, _freq)
                        except Exception:
                            _raw[_key] = None
        _frames = {"daily": daily, "weekly": weekly, "monthly": monthly}
        for _key in _MIN_PERIODS:
            _mf = (_raw or {}).get(_key)
            # 太短算不出 MA20/BOLL，放出来只会是一片空缺
            if _mf is None or getattr(_mf, "empty", True) or len(_mf) < 30:
                continue
            _mf = _mf.rename(columns={"time": "date"}) if "time" in _mf.columns else _mf
            _mf = _ensure_amount(_mf, code)    # 分钟档同样下发成交额
            _frames[_key] = _mf
            out["period_data"][_key] = to_series(calc_ma_and_indicators(_mf), intraday=True)

        # 黄金分割按周期各算一份（前端切 Tab 即换锚点）
        for _name, _d in _frames.items():
            out["period_data"][_name]["fib"] = self._calc_fibonacci(_d, _name)
        out["levels"] = self._calc_support_resistance(daily)
        out["boxes"] = self._detect_boxes(daily)
        out["channel"] = self._detect_channel(daily)
        out["current_price"] = round(float(daily["close"].iloc[-1]), 3)
        # 买卖点标记（2026-10-21 owner）：券商成交台账 → 顶层 trades 键，前端在 K 线主图
        # 叠 B/S 三角。读文件带 mtime 缓存；台账没导入过 ⇒ 空表，图表照常。
        out["trades"] = _chart_trades(code)
        # 数据身份版本号（不是时间）：前端拿它做「没变就别重传/重画」的握手，见 load_stock_chart。
        out["version"] = self._chart_version(out["period_data"], out["trades"])
        out["available"] = True
        _attach_divergence(out)      # 各周期顶/底背离标注（只读展示）
        _attach_trendlines(out)      # 日线趋势线（摆动高低点连线，只读展示）
        return out


    def _chart_version(self, period_data, trades=None):
        """数据身份版本号：各周期 (末日期:根数) 串联 + 成交标记签名（t<条数>:<末笔ts>）。
        build 与 payload 补 bar 共用，保证握手一致。
        trades 段是 2026-10-21 加的——版本号原来只覆盖 period_data，新导入的成交会被
        前端「unchanged 不刷新」握手吞掉，图上看不到新买卖点。trades=None（老调用）
        保持旧格式，行为不变。"""
        def _sig(k):
            _s = (period_data or {}).get(k)
            _d = (_s or {}).get("dates") or []
            return f"{_d[-1]}:{len(_d)}" if _d else "-:0"
        _v = "|".join(_sig(k) for k in
                      ("daily", "weekly", "monthly") + _MIN_PERIODS)
        if trades is not None:
            _v += f"|t{len(trades)}:{trades[-1]['ts'] if trades else '-'}"
            # 2026-10-22：趋势线口径版本也混进签名 —— 否则改了 trendlines 的形状/口径后，
            # 前端因「unchanged 不刷新」握手继续用旧 payload（新字段永远到不了前端）。
            _v += f"|tr{_TREND_SCHEMA}"
        return _v

    def _detect_boxes(self, daily):
        """检测箱体（P1修复）：严格触及标准 + 优化置信分。
        分位数(88/12)定义初期边界，严格触及验证，置信分=触及质量+时间持久度+宽度合理性。

        P1修复内容：
        1. 触及标准从松散(±0.8-8.8%)改为精确：必须在边界±0.5%内触及
        2. 置信分权重调整：触及质量优先于触及次数
        3. 宽度范围分类：<5%(微箱) 5-12%(正常) 12-22%(宽幅)，权重不同
        4. 合并逻辑改进：避免过度合并历史箱体
        """
        import numpy as np
        d = daily
        if len(d) < 30:
            return []

        recent = d.tail(150).reset_index(drop=True)
        closes = recent["close"].values
        highs = recent["high"].values
        lows = recent["low"].values
        dates = recent["date"].values
        n = len(recent)
        last_close = float(closes[-1])
        last_date = dates[-1]

        WIN = 30
        # 向量化滑窗：分位数/斜率/触及全窗口一次算完（与逐窗循环结果一致，快 ~15x）
        from numpy.lib.stride_tricks import sliding_window_view
        wh = sliding_window_view(highs, WIN)
        wl = sliding_window_view(lows, WIN)
        wc = sliding_window_view(closes, WIN)
        ups = np.percentile(wh, 88, axis=1)
        dns = np.percentile(wl, 12, axis=1)
        _xc = np.arange(WIN) - (WIN - 1) / 2.0
        _denom = float(np.sum(_xc * _xc))
        _slopes = (wc @ _xc) / _denom
        _means = wc.mean(axis=1)
        _rel_slopes = np.abs(_slopes) / np.where(_means == 0, 1e-9, _means)
        # P1修复：触及标准从±0.8-8.8%改为±0.5%(更精确)
        _up_touches = np.sum(wh >= (ups * 0.995)[:, None], axis=1)  # 99.5% 以上算"精确触及"
        _dn_touches = np.sum(wl <= (dns * 1.005)[:, None], axis=1)  # 100.5% 以下算"精确触及"
        _widths = (ups - dns) / np.where(_means == 0, 1e-9, _means) * 100
        # 滑窗收集候选箱体（用区间位置唯一标识，避免重复）
        boxes = {}
        for start in range(0, n - WIN + 1, 3):
            up = float(ups[start])
            dn = float(dns[start])
            rel_slope = float(_rel_slopes[start])
            up_touch = int(_up_touches[start])
            dn_touch = int(_dn_touches[start])
            width_pct = float(_widths[start])
            # P1修复：候选条件更严格 — 横盘<0.3%/天(rather than 0.5%) + 宽度3-22% + 双边精确触及≥2
            if rel_slope < 0.003 and 3.0 <= width_pct <= 22.0 and up_touch >= 2 and dn_touch >= 2:
                key = (round(up, 3), round(dn, 3))
                # P1修复：置信分优化 — 触及质量(precision)优先于触及次数
                # 触及质量分 = (精确触及数 * 2)，其次是横盘度，最后是宽度适中
                touch_quality = (up_touch + dn_touch) * 2.0  # 优先权最高
                flatness = max(0, (0.003 - rel_slope) / 0.003) * 2.0  # 越横盘越好
                # 宽度权重分化：正常箱体(5-15%)得分最高
                if 5 <= width_pct <= 15:
                    width_score = 1.5
                elif 3 <= width_pct < 5 or 15 < width_pct <= 22:
                    width_score = 0.5
                else:
                    width_score = 0
                conf = touch_quality + flatness + width_score
                if key not in boxes or conf > boxes[key]["conf"]:
                    s = dates[start].strftime("%Y-%m-%d") if hasattr(dates[start], "strftime") else str(dates[start])[:10]
                    e = dates[start + WIN - 1].strftime("%Y-%m-%d") if hasattr(dates[start+WIN-1], "strftime") else str(dates[start+WIN-1])[:10]
                    boxes[key] = {"start": s, "end": e, "low": round(dn, 3), "high": round(up, 3),
                                  "touches": (up_touch, dn_touch), "width": round(width_pct, 1),
                                  "conf": round(conf, 1), "rel": 0}

        # 关联现价关系 + 刚突破判定（改进）
        # 刚突破的定义现在基于箱体宽度动态调整，而不是固定的20天+15%
        result = []
        today_days = 999
        try:
            today_days = int(pd.Timestamp(last_date).timestamp() / 86400 - pd.Timestamp(dates[0]).timestamp() / 86400)
        except Exception:
            pass
        for key, b in boxes.items():
            end_date = b["end"]
            try:
                end_dt = pd.Timestamp(end_date)
                days_since = int((pd.Timestamp(last_date) - end_dt).days)
            except Exception:
                days_since = 999

            # P1修复：刚突破判定改为动态，基于箱体宽度
            width = b["width"]
            if width < 5:
                # 微箱体(宽度<5%)：突破后5天内+10%以内算"刚突破"
                recently_broke = days_since <= 5 and (last_close - b["high"]) / b["high"] < 0.10 if b["high"] else False
            elif width < 15:
                # 正常箱体(5-15%)：突破后10天内+12%以内算"刚突破"
                recently_broke = days_since <= 10 and (last_close - b["high"]) / b["high"] < 0.12 if b["high"] else False
            else:
                # 宽幅箱体(>15%)：突破后15天内+15%以内算"刚突破"
                recently_broke = days_since <= 15 and (last_close - b["high"]) / b["high"] < 0.15 if b["high"] else False

            if b["low"] <= last_close <= b["high"]:
                b["rel"] = 0  # 现价在箱体内
            elif last_close > b["high"] and recently_broke:
                b["rel"] = 1  # 刚突破上方
            elif last_close > b["high"]:
                b["rel"] = -1  # 上方历史箱体
            else:
                b["rel"] = -2  # 下方历史箱体
            # 距现价距离（用于排序）
            b["dist"] = abs(b["center"] if "center" in b else (b["high"] + b["low"]) / 2 - last_close)
            result.append(b)

        # 合并重叠箱体（P1改进：更严格的重叠条件，避免过度合并）
        def overlap(a, b):
            price_overlap = min(a["high"], b["high"]) - max(a["low"], b["low"])
            price_span = min(a["high"] - a["low"], b["high"] - b["low"])
            # P1修复：价格重叠从>50%改为>80%（更严格，保留历史分阶段特征）
            price_overlap_pct = price_overlap / max(price_span, 1e-9) if price_span > 0 else 0
            # 时间重叠条件也更严格：不仅要有交集，还要至少重叠5天
            try:
                s1 = pd.Timestamp(a["start"])
                e1 = pd.Timestamp(a["end"])
                s2 = pd.Timestamp(b["start"])
                e2 = pd.Timestamp(b["end"])
                overlap_days = (min(e1, e2) - max(s1, s2)).days
                t_overlap = overlap_days >= 5
            except Exception:
                t_overlap = a["end"] > b["start"] and b["end"] > a["start"]
            return price_overlap_pct > 0.8 and t_overlap

        merged = []
        for b in result:
            hit = None
            for m in merged:
                if overlap(m, b):
                    hit = m
                    break
            if hit:
                hit["low"] = min(hit["low"], b["low"])
                hit["high"] = max(hit["high"], b["high"])
                hit["start"] = min(hit["start"], b["start"])
                hit["end"] = max(hit["end"], b["end"])
                hit["conf"] = round(hit["conf"] + b["conf"], 1)
                hit["touches"] = (max(hit["touches"][0], b["touches"][0]), max(hit["touches"][1], b["touches"][1]))
            else:
                merged.append(dict(b))

        # 重算 rel + center + days + 近期有效性
        import datetime as _dt
        # 2026-10-23：「近期」必须以**这次判定的 as-of 日**（切片末根）为基准，不能用 wall-clock。
        # 原来写 `datetime.now()`，于是对历史切片判箱时，凡是 45 天前结束的箱体全被丢掉 ⇒
        # 猎手「🔄 扫描该日」选 45 天以前的日期**永远扫不出突破**（静默返回空），
        # 回测长窗口也被截成最后约 20 个交易日（第一轮箱体研究就吃了这个）。
        # 实时路径切片末根=今日，与原行为完全一致。
        try:
            now_dt = _dt.datetime.strptime(str(last_date)[:10], "%Y-%m-%d")
        except Exception:
            now_dt = _dt.datetime.now()
        recent_valid = []
        for b in merged:
            b["center"] = round((b["high"] + b["low"]) / 2, 3)
            try:
                s = _dt.datetime.strptime(b["start"][:10], "%Y-%m-%d")
                e = _dt.datetime.strptime(b["end"][:10], "%Y-%m-%d")
                b["days"] = (e - s).days
                b["days_since_end"] = (now_dt - e).days
            except Exception:
                b["days"] = 0
                b["days_since_end"] = 999
            # 只保留近期箱体（结束距今 ≤45 天），远历史箱体无参考意义
            if b["days_since_end"] > 45:
                continue
            if b["low"] <= last_close <= b["high"]:
                b["rel"] = 0
            elif last_close > b["high"]:
                b["rel"] = -1
            else:
                b["rel"] = -2
            recent_valid.append(b)

        # 排序：现价箱体(rel=0) > 上方历史(rel=-1) > 下方历史(rel=-2)；再按置信分
        recent_valid.sort(key=lambda b: (
            0 if b["rel"] == 0 else 1 if b["rel"] == -1 else 2,
            -b["conf"]))

        # P1改进：为K线图添加箱体质量评分信息
        for b in recent_valid:
            # 质量评分维度
            width = b["width"]
            touches = b["touches"]
            days = b["days"]

            # 宽度评级
            if width < 5:
                width_grade = "微"  # 微箱体，敏感但容易假突破
            elif width < 15:
                width_grade = "优"  # 正常箱体，最稳定
            else:
                width_grade = "宽"  # 宽幅箱体，波动大

            # 触及质量评级（基于精确触及次数）
            touch_quality = touches[0] + touches[1]
            if touch_quality >= 6:
                touch_grade = "极强"  # 4+次精确触及，非常稳定
            elif touch_quality >= 4:
                touch_grade = "强"    # 2-3次精确触及，较稳定
            else:
                touch_grade = "弱"    # 1次精确触及，可能噪音

            # 综合质量评分（1-10分）
            quality_score = (touch_quality * 1.5 + max(0, 15 - width) * 0.3 + max(0, 30 - days) * 0.1)
            quality_score = min(10, max(1, round(quality_score, 1)))

            b["width_grade"] = width_grade
            b["touch_grade"] = touch_grade
            b["quality_score"] = quality_score
            # 用于K线图显示的关键字段
            b["display"] = f"{width_grade}箱({width:.1f}%) 触及{touch_grade}({touch_quality}次) 质量{quality_score}/10"

        return recent_valid[:3]

    def _detect_channel(self, daily):
        """检测上行/下行通道：最近 40 日高点/低点线性回归 → 上下轨。
        斜率>0 上行通道，<0 下行通道。返回双轨线端点 + 方向。"""
        import numpy as np
        d = daily
        if len(d) < 25:
            return {"direction": "flat", "slope": 0, "up_line": [], "dn_line": []}
        recent = d.tail(40).reset_index(drop=True)
        n = len(recent)
        x = np.arange(n)
        highs = recent["high"].values
        lows = recent["low"].values

        def regline(vals):
            slope, intercept = np.polyfit(x, vals, 1)
            return slope, intercept, slope * (n - 1) + intercept

        up_slope, up_i, up_end = regline(highs)
        dn_slope, dn_i, dn_end = regline(lows)

        slope = (up_slope + dn_slope) / 2
        # 归一化斜率：每日变化 / 均价，>0.15%/天 = 上行
        avg_price = float(recent["close"].mean()) or 1e-9
        norm_slope = slope / avg_price
        direction = "up" if norm_slope > 0.0015 else ("down" if norm_slope < -0.0015 else "flat")

        # 通道方向反转检测：用更早的 40 日（第 40~80 根往前）回归，与当前方向对比
        reversal = None
        try:
            if len(d) >= 80:
                prev = d.iloc[-80:-40].reset_index(drop=True)
                if len(prev) >= 25:
                    px = np.arange(len(prev))
                    ph = prev["high"].values
                    pl = prev["low"].values
                    p_slope, _ = np.polyfit(px, ph, 1)
                    p_slope2, _ = np.polyfit(px, pl, 1)
                    p_norm = (p_slope + p_slope2) / 2 / (float(prev["close"].mean()) or 1e-9)
                    prev_dir = "up" if p_norm > 0.0015 else ("down" if p_norm < -0.0015 else "flat")
                    if direction in ("up", "down") and prev_dir in ("up", "down") and direction != prev_dir:
                        reversal = "up_to_down" if prev_dir == "up" else "down_to_up"
        except Exception:
            reversal = None

        # 趋势描述补充
        start_price = float(recent["close"].iloc[0])
        end_price = float(recent["close"].iloc[-1])
        ret_40d = (end_price - start_price) / start_price * 100 if start_price else 0
        # 现价在通道中的位置（0=下轨, 100=上轨）：用回归线在当前 x(n-1) 处的值
        up_here, dn_here = up_end, dn_end
        pos_pct = (end_price - dn_here) / (up_here - dn_here) * 100 if up_here != dn_here else 50

        return {
            "direction": direction,
            "slope": round(float(slope), 4),
            "norm_slope_pct": round(norm_slope * 100, 3),
            "up_line": [round(float(up_i), 3), round(float(up_end), 3)],
            "dn_line": [round(float(dn_i), 3), round(float(dn_end), 3)],
            "ret_40d": round(float(ret_40d), 1),
            "pos_pct": round(max(0, min(100, float(pos_pct))), 0),
            "reversal": reversal,   # up_to_down / down_to_up / None
        }

    def _calc_support_resistance(self, daily):
        """按现价强制分类 + 聚类合并 + 强度排序 → 每个方向保留最重要的 3 个。"""
        import pandas as pd
        d = daily
        last_close = float(d["close"].iloc[-1])
        H, L, C = float(d["high"].iloc[-1]), float(d["low"].iloc[-1]), last_close
        PP = (H + L + C) / 3
        candidates = []  # (price, label, strength)

        def add(price, label, strength):
            if not price or price <= 0 or price is None: return
            candidates.append((round(float(price), 2), label, strength))

        # pivot 中枢（注意: R1=2PP-L 才是上方压力, S1=2PP-H 是下方支撑, 但都可能失效）
        add(2 * PP - L, "R1中枢", 2)
        add(2 * PP - H, "S1中枢", 2)
        # 均线（近价均线更有效）
        for n, label, base_strength in ((20, "MA20", 2), (60, "MA60", 2), (180, "MA180", 1)):
            val = d[f"ma{n}"].iloc[-1]
            if not pd.isna(val):
                add(float(val), label, base_strength)
        # 前高/前低（120日局部极值）
        recent = d.tail(120)
        for idx in range(5, len(recent) - 1):
            r = recent.iloc[idx]
            win = recent.iloc[max(0, idx - 3):idx + 4]
            if r["high"] == win["high"].max() and float(r["high"]) > 0:
                add(float(r["high"]), "前高", 2)
            if r["low"] == win["low"].min() and float(r["low"]) > 0:
                add(float(r["low"]), "前低", 2)
        # 量密集区（成交量大的价位，参考性强）
        top_vol = d.nlargest(8, "volume")
        for _, r in top_vol.iterrows():
            add(float(r["close"]), "量密集", 3)

        # 1) 按现价强制分类 + 过滤太近的
        sep = last_close * 0.003  # 0.3% 内忽略
        sup_cand = [(p, l, s) for p, l, s in candidates if p < last_close - sep]
        res_cand = [(p, l, s) for p, l, s in candidates if p > last_close + sep]

        # 2) 聚类合并：价格差 <1.5% 的归一组，取强度最高 + 距现价最近的代表
        def cluster(items):
            items = sorted(items, key=lambda x: x[0])
            groups = []
            for p, l, s in items:
                if groups and abs(p - groups[-1][0]) / groups[-1][0] < 0.015:
                    gp, gl, gs = groups[-1]
                    new_s = max(s, gs)
                    rep_p = p if abs(p - last_close) < abs(gp - last_close) else gp
                    groups[-1] = (rep_p, gl if gs >= s else l, new_s)
                else:
                    groups.append((p, l, s))
            return groups

        sup_cand = cluster(sup_cand)
        res_cand = cluster(res_cand)

        # 3) 强度 = 原始强度 + 距现价近的加成（近的更有参考价值）
        def score(item):
            p, l, s = item
            dist_pct = abs(p - last_close) / last_close * 100
            near_bonus = max(0, 3 - dist_pct * 0.15)  # 距现价越近加分越多
            return s * 0.6 + near_bonus

        sup_cand.sort(key=score, reverse=True)
        res_cand.sort(key=score, reverse=True)

        def fmt(items):
            # 每方向只保留 score 最高的 1 个（最重要）
            if not items:
                return []
            best = max(items, key=lambda it: score(it))
            p, l, s = best
            return [{"price": p, "label": l, "strength": s}]

        return {"supports": fmt(sup_cand), "resistances": fmt(res_cand)}

    def _calc_fibonacci(self, daily, period="daily"):
        """黄金分割：自动锚定该周期**最显著的一段摆动** → 回撤位/扩展位。

        锚点选择：回看 N 根（见 _FIB_LOOKBACK，daily=120 与前端默认视窗对齐）
        → 分形找摆动点（复用 analysis.divergence._local_extrema）
        → 合成"高-低"交替序列 → 滤掉幅度不足的噪声摆动 → 取幅度最大的一段。
        全部不达标时降级为回看区间的最高/最低价（fallback=True）。

        比例位公式（L=摆动低点, H=摆动高点, rng=H-L）：
          上涨（低在前）：回撤 H-rng*ratio，扩展 L+rng*ext
          下跌（高在前）：回撤 L+rng*ratio，扩展 H-rng*ext
        side 按现价分：低于现价=支撑，高于现价=阻力。
        """
        from analysis.divergence import _local_extrema

        n_bars = _FIB_FRACTAL_N.get(period, 3)
        lookback = _FIB_LOOKBACK.get(period, 250)
        min_amp = _FIB_MIN_AMP.get(period, 5.0)
        if daily is None or len(daily) < 2 * n_bars + 5:
            return {"available": False, "reason": "样本不足"}

        d = daily.tail(lookback).reset_index(drop=True)
        n = len(d)
        if n < 2 * n_bars + 5:
            return {"available": False, "reason": "样本不足"}
        # 锚点索引要相对**完整周期序列**（前端 period.dates 是全量），故补齐偏移
        off = len(daily) - n
        highs = d["high"].astype(float).values
        lows = d["low"].astype(float).values
        # 分钟周期的锚点日期要带时间，否则与日线格式混同、图上标不出取自哪根
        _dfmt = "%Y-%m-%d %H:%M" if period in _MIN_PERIODS else "%Y-%m-%d"
        dates = [x.strftime(_dfmt) for x in d["date"]]

        peaks, troughs = _local_extrema(highs, lows, n_bars)

        # 1) 合成交替序列：同类型相邻只保留更极端者（否则两者属同一段走势，不构成摆动）
        piv = sorted([(i, "H") for i in peaks] + [(i, "L") for i in troughs])
        seq = []
        for idx, kind in piv:
            px = float(highs[idx]) if kind == "H" else float(lows[idx])
            if seq and seq[-1][1] == kind:
                if (px > seq[-1][2]) if kind == "H" else (px < seq[-1][2]):
                    seq[-1] = (idx, kind, px)
                continue
            seq.append((idx, kind, px))

        def amp(lo_pt, hi_pt):
            base = lo_pt[2]
            return (hi_pt[2] - lo_pt[2]) / base * 100 if base else 0.0

        # 2) 相邻两点对 → 规范成 (低点, 高点) + 方向；3) 过滤幅度不足的噪声摆动
        cands = []
        for a, b in zip(seq, seq[1:]):
            lo_pt, hi_pt = (a, b) if a[1] == "L" else (b, a)
            if hi_pt[2] <= lo_pt[2]:
                continue
            cands.append((lo_pt, hi_pt, "up" if a[1] == "L" else "down", amp(lo_pt, hi_pt)))

        fallback = False
        ok = [c for c in cands if c[3] >= min_amp]
        if ok:
            # 幅度最大者；幅度接近(差<2%)时取更近的一段
            best = max(c[3] for c in ok)
            lo_pt, hi_pt, direction, amplitude = max(
                [c for c in ok if c[3] >= best * 0.98], key=lambda c: c[1][0])
        elif cands:
            lo_pt, hi_pt, direction, amplitude = max(cands, key=lambda c: c[3])
            fallback = True
        else:
            # 无交替摆动点（单边行情）→ 降级为回看区间最高/最低
            hi_i, lo_i = int(d["high"].idxmax()), int(d["low"].idxmin())
            lo_pt, hi_pt = (lo_i, "L", float(lows[lo_i])), (hi_i, "H", float(highs[hi_i]))
            direction = "up" if lo_i < hi_i else "down"
            amplitude = amp(lo_pt, hi_pt)
            fallback = True

        L, H = lo_pt[2], hi_pt[2]
        rng = H - L
        if rng <= 0:
            return {"available": False, "reason": "摆动幅度为零"}

        cur = float(d["close"].iloc[-1])
        levels = []

        def add(ratio, price, kind):
            price = round(float(price), 3)
            if price <= 0:
                return  # 大幅下跌摆动时扩展位可能算到 0 以下，无意义 → 丢弃
            levels.append({
                "ratio": ratio, "price": price, "kind": kind,
                "label": f"{ratio * 100:.1f}%".replace(".0%", "%"),
                "side": "support" if price < cur else "resistance",
                "golden": abs(ratio - 0.618) < 1e-9,
            })

        up = direction == "up"
        for rt in _FIB_RETRACE:
            add(rt, (H - rng * rt) if up else (L + rng * rt), "retracement")
        for ex in _FIB_EXTENSION:
            add(ex, (L + rng * ex) if up else (H - rng * ex), "extension")

        return {
            "available": True,
            "direction": direction,
            "fallback": fallback,
            "swing": {
                "low": {"price": round(L, 3), "index": int(lo_pt[0]) + off, "date": dates[lo_pt[0]]},
                "high": {"price": round(H, 3), "index": int(hi_pt[0]) + off, "date": dates[hi_pt[0]]},
                "amplitude_pct": round(amplitude, 2),
                "bars": int(abs(hi_pt[0] - lo_pt[0])),
                "lookback": int(n),
            },
            "levels": levels,
        }

    # ---------- 选股猎手（概念评分，与 Excel 报告一致） ----------
    def run_hunter(self, date=None, auto=False):
        """后台运行选股猎手（拉取+评分），立即返回；前端轮询 hunter_progress 看进度。

        auto=True 为保留参数（原定时自动运行已删，见 2026-10-10 owner 需求6）：
        该分支只在候选集变化时才推飞书；手动运行(auto=False)每次都推。
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        if HUNTER_RUN_STATE.get("running") and HUNTER_RUN_STATE.get("date") == date:
            return {"started": True, "running": True, "date": date}
        # 清除旧结果，防读到上一个日期的缓存
        HUNTER_RUN_STATE["date"] = date
        HUNTER_RUN_STATE["running"] = True
        HUNTER_RUN_STATE["result"] = None
        try:
            from modules.market_data import MARKET_PROGRESS as _MP
            _MP.update({"running": True, "phase": "准备", "done": 0, "total": 0, "msg": "启动中"})
        except Exception:
            pass

        def _work():
            try:
                res = self._load_hunter_impl(date)
                HUNTER_RUN_STATE["result"] = res
                # 2026-09-29 owner 拍板：**盘中手动运行只算不推**。
                # 背景：盘中 GO 列现在会算出候选（见 _hunter_build_conformance），而手动运行是
                # dedup=False（每次全推）⇒ 反复点「刷新数据」会把飞书群刷屏。
                # 定时自动档(auto)盘中的建仓推送**保持原样**——2026-09-21 拍板放开的正是它。
                if auto or not _hunter_is_intraday(date):
                    self._push_hunter_build_candidates(res, date, dedup=auto)
            except Exception as e:
                HUNTER_RUN_STATE["result"] = {"available": False, "error": f"选股猎手运行失败: {e}"}
            finally:
                HUNTER_RUN_STATE["running"] = False

        _th.Thread(target=_work, daemon=True).start()
        return {"started": True, "running": True, "date": date}

    def _push_hunter_build_candidates(self, res, date, dedup=False):
        """猎手跑完后，把「符合建仓条件」的股票**按板块逐条**推送到飞书（无候选则不推）。

        口径 = GUI「建仓」列的绿色 x·GO —— 即 _hunter_build_conformance 的时机门控
        （市场有方向/多头结构/回撤到位/金叉加分）。2026-09-21 owner 需求。

        dedup=True（定时自动运行）：**按板块去重** —— 某板块候选集与当日已推的相同则跳过，
        出现新票才推该板块；dedup=False（手动运行）每次全推。
        任何失败只打印，绝不影响猎手结果。可用 stock_hunter/config.json 的
        feishu.push_build_signals=false 关闭。
        """
        try:
            from modules.push_feishu import build_build_candidates, send_build_candidates
            import modules.data_loader as _hdl
            _cfg = _hdl.DataLoader._load_config()
            if not (_cfg.get("feishu", {}) or {}).get("push_build_signals", True):
                print("[建仓推送] 已由配置关闭（feishu.push_build_signals=false），跳过")
                return
            groups = build_build_candidates((res or {}).get("sector_stocks") or {})
            if not groups:
                print("[建仓推送] 无符合建仓条件的股票，跳过")
                return
            _d = str(date).replace("-", "")

            # 当日已推状态（按板块记候选集）；跨日自然清空
            _prev = _load_json(_HUNTER_BUILD_PUSHED_FP, {}) or {}
            _pushed = dict(_prev.get("sectors") or {}) if str(_prev.get("date")) == _d else {}
            if dedup:
                _todo = [g for g in groups
                         if _pushed.get(g["sector"]) != sorted(s["代码"] for s in g["stocks"])]
                if not _todo:
                    print(f"[建仓推送] 各板块候选集均未变化，跳过（{len(groups)} 个板块）")
                    return
                groups = _todo

            total = sum(len(g["stocks"]) for g in groups)
            r = send_build_candidates(_cfg, groups, _d)

            # 记录已推板块的候选集（自动/手动都记，供后续自动运行比对）
            for g in groups:
                _pushed[g["sector"]] = sorted(s["代码"] for s in g["stocks"])
            try:
                _HUNTER_BUILD_PUSHED_FP.write_text(json.dumps(
                    {"date": _d, "sectors": _pushed,
                     "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
                    encoding="utf-8")
            except Exception:
                pass
            print(f"[建仓推送] {total} 只 / {len(groups)} 个板块 → 成功{r.get('sent')} 失败{r.get('failed')}")
        except Exception as e:
            print(f"[建仓推送] 失败（已忽略，不影响猎手）: {str(e)[:150]}")

    def hunter_progress(self):
        """返回选股猎手运行进度（供前端进度条轮询）。"""
        try:
            from modules.market_data import MARKET_PROGRESS as _MP
            mp = dict(_MP)
        except Exception:
            mp = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        return {
            "running": bool(HUNTER_RUN_STATE.get("running")),
            "ready": bool(HUNTER_RUN_STATE.get("result")),
            "date": HUNTER_RUN_STATE.get("date"),
            "phase": mp.get("phase", ""),
            "done": int(mp.get("done") or 0),
            "total": int(mp.get("total") or 0),
            "msg": mp.get("msg", ""),
        }


    def load_ma_reclaim(self, codes):
        """给「选股猎手」**展开板块的可见个股**补「当日均线突破时刻」（2026-10-23 owner）。

        为什么单独开一个端点：猎手池 2000+ 只，全量算要 ~5 分钟；这张表一次只展开一两个板块、
        每页 50 只 ⇒ 由前端把**当前渲染出来的那些码**传上来，只算这些（主线程只读缓存 + 踢预热）。
        """
        code_list = []
        for c in (codes or []):
            b = str(c).split("_")[0]
            if b and b not in code_list:
                code_list.append(b)
        code_list = code_list[:200]
        if not code_list:
            return _clean({"items": {}})
        try:
            self._kick_ma_reclaim_warm(code_list)
        except Exception:
            pass
        items = {}
        for c in code_list:
            try:
                items[c] = self._ma_reclaim_get(c, kick=False)
            except Exception:
                items[c] = {"pending": True}
        return _clean({"items": items})

    def load_hunter(self, date=None):
        """选股猎手数据。运行中→running；有该日结果→返回；都没有→返回空（**不再懒触发扫描**）。

        2026-10-10 owner 需求6：所有扫描一律由按钮（run_hunter）发起，打开页面/轮询不得自动跑。
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        if HUNTER_RUN_STATE.get("running") and HUNTER_RUN_STATE.get("date") == date:
            return {"available": False, "running": True, "date": date}
        if HUNTER_RUN_STATE.get("date") == date and HUNTER_RUN_STATE.get("result"):
            return HUNTER_RUN_STATE["result"]
        return {"available": False, "running": False, "date": date}

    def _hunter_build_conformance(self, codes, date):
        """计算各股建仓信号符合度（时机门控 GO：市场有方向/多头结构/回撤到位/金叉加分）。

        读 t_io/cache/daily_kline 日线缓存算特征（零网络；全池约 20s；未缓存跳过显示"—"）。

        2026-09-29 起**盘中不再跳过**（原为「手动点击时维持原状」以省资源）——owner 要求
        盘中点按钮即按当时实时 K 线算出建仓符合度。之所以敢放开：该缓存已由猎手本轮拉取
        **回写当日实时 bar**（见 market_data 的 merge_daily_cache 调用），零网络也能拿到实时价；
        此前不放开的后半段理由（缓存陈旧、GO 列是昨天的）已随回写一并消除。
        返回 {code: {go, regime, met, conds:{t_*:bool}, reason}}。"""
        result = {}
        try:
            import pandas as _pd
            _DAILY_CACHE_DIR = _DAILY_KLINE_DIR   # 本地常量（2026-10-11），见模块顶部说明
            from config import ENTRY_TIMING_PARAMS as _ETP
            # 指数 regime（读指数日线缓存，零网络）
            regime_by_date = {}
            try:
                _idx = json.loads((BASE / "t_io" / "cache" / "daily_kline" / "index_sh000001.json").read_text(encoding="utf-8")).get("rows", [])
                _idx_df = _pd.DataFrame(_idx)
                _idx_df["close"] = _pd.to_numeric(_idx_df["close"])
                _idx_df["ma60"] = _idx_df["close"].rolling(60).mean()
                _idx_df["date"] = _idx_df["date"].astype(str)
                for _, _r in _idx_df.iterrows():
                    if _r["close"] > _r["ma60"]:
                        regime_by_date[str(_r["date"])] = "trend_up"
                    elif _r["close"] < _r["ma60"] * 0.97:
                        regime_by_date[str(_r["date"])] = "trend_dn"
                    else:
                        regime_by_date[str(_r["date"])] = "range"
            except Exception:
                pass
            _regime = regime_by_date.get(str(date), "range")
            for i, code in enumerate(codes):
                if i % 50 == 0:
                    try:
                        from modules.market_data import MARKET_PROGRESS as _MP
                        _MP.update({"done": i, "total": len(codes), "msg": f"计算建仓符合度 {i}/{len(codes)}"})
                    except Exception:
                        pass
                _c6 = str(code).split("_")[0]
                _fp = _DAILY_CACHE_DIR / f"{_c6}.json"
                try:
                    _mt = _fp.stat().st_mtime_ns
                except OSError:
                    continue
                _ck = (_c6, str(date), _regime, _mt)
                _hit = _HUNTER_CONF_CACHE.get(_ck)
                if _hit is not None:
                    # 同一 (票, 日期, regime, 文件版本) 已算过 ⇒ 直接复用。
                    # 猎手 / 风向标 `_beacon_enrich` / GO 历史回算 三条路径共用这份结果，
                    # 正是它们同时跑同一算法把耗时放大 2~4 倍的解药（2026-10-17）。
                    result[str(code)] = _hit
                    continue
                try:
                    rows = json.loads(_fp.read_text(encoding="utf-8")).get("rows", [])
                    if len(rows) < 61:
                        continue
                    from core import build_decision as _bd
                    _df = _pd.DataFrame(rows)
                    _f = _bd.features_from_daily(_df, str(date))
                    if not _f:
                        continue
                    # 2026-10-08 owner：猎手个股行与**建仓信号扫描同规则同展示** —— 一律走
                    # build_decision 的单一真源（features_from_daily/timing_decision/verdict_from_timing），
                    # 不再自己手算，保证 判定/得分/通过X/3/条件/否决 与建仓表逐列一致。
                    _dec = _bd.timing_decision(_f, _regime, _ETP)
                    _verdict, _score = _bd.verdict_from_timing(bool(_dec["go"]), _regime, _f, False, _ETP)
                    _reachable = _regime in ("trend_up", "trend_dn")
                    _trend = bool(_f.get("trend_multihead"))
                    _dd_ok = _bd.dd_threshold_ok(float(_f["drawdown"]), _regime) if "drawdown" in _f else False
                    _golden = bool(_f.get("macd_golden_5d"))
                    # 2026-10-16 owner：「选股猎手的时机条件列与建仓信号扫描保持一致」。
                    # 缺口在**键集**：本处原先只给 5 键，而真源 COND_LABELS 已是 6 条 ——
                    # 10-15 新增的 t_ma20/t_ma5 两条硬约束正是"解释 NO-GO 为什么 NO-GO"的两条，
                    # 猎手侧没有 ⇒ 同一只票在猎手表里 4 个圆点、在建仓表里 7 个（少 3 个，
                    # 其中 2 个是本次新增），且 tooltip 与建仓表不同源。
                    # ⇒ 与 `core/position_builder.py` 逐字同口径（那里也是 `is True`）补齐。
                    _ma20_ok = _f.get("above_ma20") is True
                    _ma5_ok = _f.get("above_ma5") is True
                    conds = {"t_regime": bool(_reachable), "t_trend": _trend, "t_drawdown": _dd_ok,
                             "t_golden": _golden, "t_veto": not _dec["veto"],
                             "t_ma20": _ma20_ok, "t_ma5": _ma5_ok}
                    result[str(code)] = {
                        "go": bool(_dec["go"]), "regime": _regime,
                        "met": int(_reachable) + int(_trend) + int(_dd_ok),
                        "conds": conds, "reason": "；".join(_dec["reasons"]),
                        # 2026-10-11：与 position_builder 同步——得分已**移出市场项**（横截面 IC 恒 0），
                        # 故恒可满 0~100；旧的"range 市上限 70"不再成立。signal_reachable 仍只由 regime 定。
                        "score": int(_score), "score_ceiling": 100,
                        "signal_reachable": bool(_reachable), "verdict": _verdict,
                        "veto": list(_dec["veto"]), "above_ma5": _f.get("above_ma5"),
                        "above_ma20": _f.get("above_ma20"),
                        "price": _f.get("price"),
                        # 日线四阶段：**本函数已算出**（features_from_daily 的副产品）⇒ 白给，零额外取数。
                        # 猎手池 5000+ 只，30min/10min 那两列要逐只出网（实测 ~1.5s/只、400 只 >5min），
                        # 故猎手个股表**只给这一列**（见 doc/solutions §5.10）。
                        "stage": _f.get("stage_daily_code"),
                    }
                    # 写进进程内缓存（键含 mtime ⇒ 缓存文件一变就重算）。
                    # 只存"算成功"的；条数上限防进程长跑无限增长（每周新日期都会进来）。
                    if len(_HUNTER_CONF_CACHE) > 50000:
                        _HUNTER_CONF_CACHE.clear()
                    _HUNTER_CONF_CACHE[_ck] = result[str(code)]
                except Exception:
                    continue
        except Exception:
            pass
        return result

    def _score_dims(self, sm):
        """韭研 D1~D11 打分 → {短名: 分}（只留**非零**维度 + 大成交额）。

        短名取 `D<数字>` 前缀（"D11刚站上5日线" → "D11"）；"大成交额额外加分" → "大额"。
        全池均值仅 ~0.8，绝大多数票为空 ⇒ 只传非零项，payload 体积可忽略。
        纯函数、不抛：打分字典缺键/值非法都跳过（它只喂展示，不影响任何判定）。
        """
        out = {}
        for k, v in (sm or {}).items():
            ks = str(k)
            try:
                vi = int(float(v or 0))
            except Exception:
                continue
            if not vi:
                continue
            if ks == "大成交额额外加分":
                out["大额"] = vi
                continue
            m = re.match(r"^(D\d+)", ks)
            if m:
                out[m.group(1)] = vi
        return out

    def _load_hunter_impl(self, date=None):
        """运行 stock_hunter 打分管线，返回 DataLoader 原生产出的表格数据。
        与 Excel 报告 Sheet 1/2/3 数据结构对齐。"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        out = {"date": date, "available": False, "error": ""}
        try:
            import pandas as pd
            from modules.data_loader import DataLoader as HLoader
            from modules.market_data import MarketDataFetcher
            from modules.scorer import ConceptScorer
            from modules.ranker import Top5Ranker
        except Exception as e:
            out["error"] = f"stock_hunter 模块加载失败: {e}"
            return out

        try:
            cfg_path = HUNTER_DIR / "config.json"
            if not cfg_path.exists():
                out["error"] = "stock_hunter/config.json 不存在"
                return out
            with open(cfg_path, encoding="utf-8") as f:
                hunter_cfg = json.load(f)
        except Exception as e:
            out["error"] = f"加载配置失败: {e}"
            return out

        try:
            loader = HLoader(config=hunter_cfg)
            watchlist = loader.load_watchlist()
            if watchlist is None or watchlist.empty:
                # 诊断信息：输出更多细节帮助排查问题
                # ⚠️ 2026-10-21：这里原来写的是 `log.warning(...)`，而 t_gui.py **没有 `log`**
                # （没有模块级 logger）⇒ 这条"加载失败"分支自己会抛 NameError，把
                # `out["error"]` 的干净返回变成异常。本文件日志口径是 print(flush=True)。
                watchlist_path = HUNTER_DIR / "watchlist_jiuyan.json"
                print("[HUNTER] watchlist 加载失败", flush=True)
                print(f"  - watchlist_path: {watchlist_path}", flush=True)
                print(f"  - exists: {watchlist_path.exists()}", flush=True)
                if watchlist_path.exists():
                    try:
                        test_data = json.loads(watchlist_path.read_text(encoding='utf-8'))
                        print(f"  - file size: {watchlist_path.stat().st_size} bytes", flush=True)
                        print(f"  - records: {len(test_data)}", flush=True)
                    except Exception as e:
                        print(f"  - read error: {e}", flush=True)
                out["error"] = f"watchlist 加载失败 (path: {watchlist_path}, exists: {watchlist_path.exists()})"
                return out

            df_pool = watchlist[watchlist["韭研概念"].str.strip().ne("")].copy()
            codes = list(dict.fromkeys(df_pool["代码"].astype(str).tolist()))

            st_codes = set()
            if "名称" in watchlist.columns:
                st_mask = watchlist["名称"].str.startswith(("*ST", "ST", "SST", "S*ST")).fillna(False)
                st_codes = set(watchlist.loc[st_mask, "代码"].astype(str).tolist())
            fetcher = MarketDataFetcher(data_dir=str(HUNTER_DIR / "data"), st_codes=st_codes)
            # fix 2026-08-24: 行情拉取近乎全空 → 报错；2026-08-25: 腾讯限流多为瞬态，
            # 不足阈值先整轮重试一次（5s 后），仍不足才报错，避免偶发限流整日断供
            _min_ok = max(1, int(len(codes) * 0.01))
            market_df = pd.DataFrame()
            for _attempt in range(2):
                market_df = fetcher.fetch_for_date(codes, date)
                if len(market_df) >= _min_ok:
                    break
                if _attempt == 0:
                    try:
                        from modules.market_data import MARKET_PROGRESS as _MP
                        _MP.update({"phase": f"行情拉取异常(仅{len(market_df)}只)，5s后整轮重试",
                                    "done": 0, "total": 0, "msg": ""})
                    except Exception:
                        pass
                    import time as _t
                    _t.sleep(5)
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                # done/total 归零 → 前端显示不确定进度条，避免继承拉取期的"100%"误导卡死观感
                _MP.update({"phase": "概念打分", "done": 0, "total": 0, "msg": f"已拉取行情 {len(market_df)} 只，正在打分"})
            except Exception:
                pass
            if len(market_df) < _min_ok:
                out["error"] = (f"行情拉取失败：仅获取 {len(market_df)}/{len(codes)} 只，无法评分。"
                                f"多为腾讯接口限流/网络异常，请稍后重试。"
                                f"若反复失败，可检查 stock_hunter/data/market_{date.replace('-', '')}.csv 缓存是否损坏。")
                return out
            if not market_df.empty:
                if "名称" in market_df.columns:
                    market_df = market_df.drop(columns=["名称"])
                watchlist = watchlist.merge(market_df, on="代码", how="left")
                loader.set_watchlist(watchlist)
                # 用含行情数据的 watchlist 重建 pool
                df_pool = watchlist[watchlist["韭研概念"].str.strip().ne("")].copy()

            # 打分（含行情数据的 pool）
            dims = hunter_cfg.get("scoring", {}).get("dimensions", [])
            scorer = ConceptScorer(dimensions=dims if dims else None)
            stock_list = []
            for _, row in df_pool.iterrows():
                s = row.to_dict()
                s.setdefault("涨停", int(row.get("涨停", 0)) if pd.notna(row.get("涨停")) else 0)
                s.setdefault("连板天数", 0)
                stock_list.append(s)
            scored = scorer.compute_batch(stock_list)
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"phase": "构建板块/个股明细", "done": 0, "total": 0, "msg": f"打分完成 {len(scored)} 只"})
            except Exception:
                pass

            score_map = {str(s.get("代码", "")): s for s in scored}
            for col in ["总得分", "涨停", "D1强势形态且新高", "D2强势形态",
                        "D4首板资金池", "D5潜在突破10日", "D6潜在突破5日",
                        "D7持续性", "D8情绪分数", "D9活跃程度",
                        "D11刚站上5日线", "D12趋势性", "大成交额额外加分"]:
                watchlist[col] = watchlist["代码"].map(
                    lambda x, c=col: score_map.get(str(x), {}).get(c, 0))
            loader.set_watchlist(watchlist)  # ← 关键：评分写入后必须回传
        except Exception as e:
            out["error"] = f"数据/评分失败: {e}"
            return out

        try:
            # 使用 DataLoader 原生方法生成 Excel 同款表格
            import numpy as np
            df_summary = loader.load_concept_summary()
            df_detail = loader.load_detail_ranking()
            top5_list = []  # TOP5 按概念
            top5_ranker = Top5Ranker()
            # 2026-10-11 删除空循环：原为 `for _, row in df_pool.iterrows(): pass`
            # ——遍历整个猎手池、逐行构造 Series，循环体是 `pass`，纯浪费（删后行为零变化）。
            # 按韭研分类聚合并选 TOP5
            for category in sorted(df_pool["韭研分类"].unique()):
                if not category: continue
                cat_df = df_pool[df_pool["韭研分类"] == category]
                stocks = []
                for _, row in cat_df.iterrows():
                    d = row.to_dict()
                    for k, v in (score_map.get(str(d.get("代码", "")), {}) or {}).items():
                        d[k] = v
                    stocks.append(d)
                if stocks:
                    t5 = top5_ranker.select(stocks)
                    top5_list.append({
                        "category": category,
                        "stocks": [{"name": t.get("名称", ""), "code": t.get("代码", ""),
                                    "score": int(float(t.get("总得分", 0) or 0) if str(t.get("总得分")) != "nan" else 0),
                                    "change_pct": round(float(t.get("涨跌幅", 0) or 0), 2) if str(t.get("涨跌幅")) not in ("nan", "None", "") else 0.0}
                                   for t in (t5 or [])[:5]],
                    })

            # DataFrame → 可序列化
            def df_to_rows(df):
                if df is None or df.empty: return [], []
                cols = [str(c) for c in df.columns]
                rows = []
                for _, row in df.iterrows():
                    rows.append({str(c): (None if isinstance(row[c], float) and np.isnan(row[c]) else row[c]) for c in cols})
                return cols, rows


            # 板块热度趋势（对比前一日）
            heat_trends = {}
            try:
                from modules.heat_tracker import load_history as load_heat_history
                heat_hist = load_heat_history()
                today_k = date.replace("-", "")
                dates_sorted = sorted(heat_hist.keys())
                prev_k = dates_sorted[-2] if len(dates_sorted) >= 2 and dates_sorted[-1] == today_k else (
                    dates_sorted[-1] if dates_sorted and dates_sorted[-1] < today_k else None)
                today_heat = {s["板块"]: s for s in heat_hist.get(today_k, [])}
                prev_heat = {s["板块"]: s for s in heat_hist.get(prev_k, [])} if prev_k else {}
                for name, t in today_heat.items():
                    p = prev_heat.get(name, {})
                    heat_trends[name] = {
                        "heat": t.get("热度分"), "trend": t.get("趋势", ""),
                        "prev_heat": p.get("热度分"), "prev_trend": p.get("趋势", ""),
                        "stock_count": t.get("股票数量"), "up_pct": t.get("上涨家数占比%"),
                        "limit_up": t.get("涨停数"), "vol_ratio": t.get("成交额放大倍数"),
                    }
            except Exception:
                pass

            # 板块→个股明细（D5/D6 异常信息）
            sector_stocks = {}
            try:
                for category in sorted(df_pool["韭研分类"].unique()):
                    if not category: continue
                    cat_df = df_pool[df_pool["韭研分类"] == category]
                    stocks = []
                    for _, row in cat_df.iterrows():
                        d = row.to_dict()
                        code = str(d.get("代码", ""))
                        sm = score_map.get(code, {}) or {}
                        d5 = sm.get("D5潜在突破10日", 0) or 0
                        d6 = sm.get("D6潜在突破5日", 0) or 0
                        d9 = sm.get("D9活跃程度", 0) or 0
                        total = sm.get("总得分", 0) or 0
                        try: total = int(float(total))
                        except: total = 0
                        try: d5 = int(float(d5))
                        except: d5 = 0
                        try: d6 = int(float(d6))
                        except: d6 = 0
                        try: d9 = int(float(d9))
                        except: d9 = 0
                        # 2026-10-17：逐只 D1~D11 打分明细（韭研原始评分，与「得分」列的
                        # 建仓综合得分 0~100 是两回事）。此前只带 d5/d6/d9、且只用于板块级
                        # 「D5×N」计数 ⇒ 展开个股后看不到明细，表尾却在解释 D5/D6/D9。
                        stocks.append({
                            "name": d.get("名称", ""), "code": code,
                            "dims": self._score_dims(sm),
                            # 细分（韭研概念，| 分隔多分类）——供前端展开板块后按下一级分类分组
                            "concepts": [c.strip() for c in str(d.get("韭研概念", "") or "").split("|") if c.strip()],
                            "score": total, "d5": d5, "d6": d6, "d9": d9,
                            "change_pct": round(float(d.get("涨跌幅", 0) or 0), 2) if str(d.get("涨跌幅")) not in ("nan", "None", "") else 0.0,
                            "limit_up": int(float(d.get("涨停", 0) or 0)) if str(d.get("涨停")) not in ("nan", "None", "") else 0,
                            # 成交额：**已在 df_pool 里**（零额外取数），仅供板块内分层用
                            "amount": (float(d.get("成交额", 0) or 0)
                                       if str(d.get("成交额")) not in ("nan", "None", "") else 0.0),
                        })
                    stocks.sort(key=lambda x: -x["score"])
                    sector_stocks[category] = stocks
            except Exception:
                pass  # 个股明细非致命

            # 板块内 龙头/中军/杂毛（**描述性标签**，零额外取数：只用已在该表里的 涨跌幅/成交额）。
            # ⚠️ 实测无前瞻区分度，勿当依据 —— 见 `_tier_sector_stocks` docstring。
            for _cat, _ss in sector_stocks.items():
                _tier_sector_stocks(_ss)

            # 盘后建仓信号符合度注入（实盘盘中跳过省资源；盘后点击"今日数据"时显示）
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"phase": "计算建仓符合度", "done": 0, "total": len(codes), "msg": ""})
            except Exception:
                pass
            _build_n = 0
            try:
                build_conf = self._hunter_build_conformance(codes, date)
                _build_n = len(build_conf or {})
                if build_conf:
                    for cat, stocks in sector_stocks.items():
                        for s in stocks:
                            c = build_conf.get(str(s.get("code", "")))
                            if c:
                                s["build_go"] = c["go"]
                                s["build_regime"] = c["regime"]
                                s["build_met"] = c["met"]
                                s["build_conds"] = c["conds"]
                                s["build_reason"] = c["reason"]
                                s["build_score"] = c.get("score")
                                s["build_ceiling"] = c.get("score_ceiling")
                                s["build_reachable"] = c.get("signal_reachable")
                                s["build_verdict"] = c.get("verdict")
                                s["build_veto"] = c.get("veto") or []
                                s["build_above_ma5"] = c.get("above_ma5")
                                s["build_above_ma20"] = c.get("above_ma20")
                                s["build_stage"] = c.get("stage")
                                s["build_price"] = c.get("price")
            except Exception:
                pass
            # 建仓符合股靠前显示：GO(时机放行)优先 → 符合条件数 → 建仓得分（与建仓表同口径）
            for cat, stocks in sector_stocks.items():
                stocks.sort(key=lambda s: (
                    -(1 if s.get("build_go") else 0),
                    -(s.get("build_met") or 0),
                    -(s.get("build_score") or 0),
                ))

            # 3) 生成排名表 + 注入热度/趋势/股票数
            sum_cols, sum_rows = df_to_rows(df_summary)
            for row in sum_rows:
                cat = row.get("板块", "")
                ht = heat_trends.get(cat, {})
                row["股票数"] = len(sector_stocks.get(cat, []))
                row["热度"] = ht.get("heat")
                trend_str = (ht.get("trend") or "")
                if ht.get("heat") is not None and ht.get("prev_heat") is not None:
                    delta = int(ht["heat"] - ht["prev_heat"])
                    trend_str += (" +" if delta >= 0 else " ") + str(delta)
                row["趋势"] = trend_str
            sum_cols = ["排名", "板块", "平均分", "涨停数", "热度", "趋势", "前三强", "股票数"]

            # 4) 概念得分趋势（近14天热度+均分）
            concept_trends = {}
            try:
                from modules.heat_tracker import load_history as load_heat_history
                heat_hist = load_heat_history()
                all_dates = sorted(heat_hist.keys())[-14:]
                for cat in set(r["板块"] for r in sum_rows):
                    pts = []
                    for d in all_dates:
                        items = heat_hist.get(d, [])
                        hit = next((s for s in items if s["板块"] == cat), None)
                        pts.append({
                            "date": d[4:],  # MMDD
                            "heat": hit.get("热度分") if hit else None,
                            "avg": hit.get("平均分") if hit else None,
                        })
                    concept_trends[cat] = pts
            except Exception:
                pass

            # 2026-08-16: 自动保存 heat history 快照（板块汇总），日期列表/历史视图随运行积累
            # 2026-10-13：保存前**合并回 热度分/热度版本/成交额总额** —— 本文件还有一个写入方
            # （CLI 管线 `_save_sector_history`，带热度分字段，供 `_get_past_scores` 趋势回归用）。
            # GUI 这份 sum_rows 没有热度分，直接覆盖会把 CLI 算好的当日热度分**抹掉**
            # （趋势回归对缺字段的条目直接跳过 ⇒ 热度趋势断档）。按板块名合并保住。
            try:
                from modules.heat_tracker import save_daily_summary as _save_hs
                from modules.heat_tracker import load_history as _load_hs
                _exist = {s.get("板块"): s for s in (_load_hs().get(date.replace("-", "")) or [])
                          if isinstance(s, dict) and "热度分" in s}
                if _exist:
                    for _r in sum_rows:
                        _e = _exist.get(_r.get("板块"))
                        if _e:
                            for _k in ("热度分", "热度版本", "成交额总额", "成交额放大倍数",
                                       "上涨家数占比%", "前排率%"):
                                if _k in _e:
                                    _r[_k] = _e[_k]
                _save_hs(date.replace("-", ""), list(sum_rows))
            except Exception:
                pass

            out["available"] = True
            out["pool_size"] = len(codes)
            out["summary_cols"] = sum_cols
            out["summary_rows"] = sum_rows
            out["top5"] = top5_list
            out["heat_trends"] = heat_trends
            out["sector_stocks"] = sector_stocks
            # 2026-10-16：时机条件列的**标签真源**下发给前端（与建仓表 payload 的 `cond_labels` 同一个
            # `COND_LABELS`）。此前猎手前端**自己硬编码只列 3 个条件键**，与真源（6 条）脱钩 ⇒
            # 10-15 新增 t_ma20/t_ma5 时漏改，同一只票在两张表里圆点数不同。下发后端真源即消除重复。
            out["cond_labels"] = COND_LABELS
            # 2026-10-11：把"建仓符合度算了多少"报给前端 —— 表格渲染后这段时间界面**没有反馈**
            # （owner：展开板块后各列空着，不知道是不是在更新）。前端据此显示进度/原因。
            out["build_n"] = _build_n
            out["build_total"] = len(codes)
            out["concept_trends"] = concept_trends
            # 2026-10-24：板块生命周期阶段（首现/发酵/高潮/退潮/扩散）——数据源 = 韭研复盘广度
            # （stock_hunter/data/sector_breadth.json），与「每日复盘 → 主线广度」同一真源。
            # 取不到/没录入当日复盘时返回 {}，前端回退到原来的趋势阶段，不影响渲染。
            try:
                from modules.sector_phase import all_category_phases
                out["sector_phase"] = all_category_phases(date)
            except Exception:
                out["sector_phase"] = {}
            # 2026-10-19：韭研二级概念注释 {概念: 解释} 全局下发——前端细分分组头/概念列
            # 悬停显示「这个标签是什么意思」（医药板块 13 个二级概念起，后续板块沿用同字段）。
            out["concept_descs"] = _jiuyan_all_concept_descs()
            # 2026-10-24：逐股理由 {概念: [[名称, 理由], ...]} ——二级分组头 tooltip 里列本组成员为什么入选
            out["concept_reasons"] = _jiuyan_all_concept_reasons()
            # 2026-10-24：逐股判词 {代码: [核心/相关/边缘/错标, 理由]}——个股行名字后直接显示小标
            out["tag_verdicts"] = _jiuyan_tag_verdicts()
            out["refreshed_at"] = datetime.now().strftime("%H:%M:%S")
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"running": False, "phase": "完成", "done": 1, "total": 1, "msg": "选股猎手运行完成"})
            except Exception:
                pass
        except Exception as e:
            out["error"] = f"排名生成失败: {e}"
            return out

        return _clean(out)

    # ---------- 批量通道标注（板块成分股） ----------
    def load_channel_batch(self, codes):
        """批量拉日线算通道方向（分批并发，支持全部成分股）。返回 {code: trend}。"""
        import threading
        from core.position_builder import fetch_daily_kline
        codes = [str(c) for c in (codes or []) if c]
        result = {}
        lock = threading.Lock()

        def work(code):
            try:
                df = fetch_daily_kline(code)
                if df.empty or len(df) < 25:
                    trend = "flat"
                else:
                    import numpy as np
                    recent = df.tail(40)
                    closes = recent["close"].values
                    slope = np.polyfit(np.arange(len(closes)), closes, 1)[0]
                    norm = slope / (closes.mean() or 1e-9)
                    trend = "up" if norm > 0.0015 else ("down" if norm < -0.0015 else "flat")
                with lock:
                    result[code] = trend
            except Exception:
                with lock:
                    result[code] = "flat"

        # 分批并发（每批 30，避免太多线程）
        for i in range(0, len(codes), 30):
            batch = codes[i:i + 30]
            threads = [threading.Thread(target=work, args=(c,), daemon=True) for c in batch]
            for t in threads: t.start()
            for t in threads: t.join(timeout=20)
        return _clean({"trends": result})

    # ---------- 个股技术标签引擎 ----------
    def _live_quote_forming(self, code):
        """实时快照 → {price, open, high, low, volume, ts_date} 或 None。P1-2 收敛：走 market_data provider。
        K线主机(ifzq)被 WAF 501 拦截时 fetch_daily_kline 会静默回退缺当日K线的旧缓存，
        用实时快照补一条当日 forming bar，避免技术标签按昨日收盘误判。
        ts_date 新鲜度语义保留：开盘前/快照陈旧时返回昨日日期，_stock_tags_one 据此不补 forming bar。"""
        from core.market_data import get_provider
        base = str(code).split("_")[0]
        return get_provider().snapshot([base]).get(base)

    def _m30_top_snapshot(self, code, allow_compute=False):
        """持仓体检 30min 快照（趋势/背离/顶部特征）：**只读内存/磁盘、零网络**、按 30min 时段记忆。

        `allow_compute`（2026-10-09 性能）：`_build_m30_snapshot` 里 `evaluate_bars`（趋势状态机管线）
        单只 ~300ms，7 只就是 ~2s——**绝不能在 pywebview 主线程跑**（实测 load_ob_analysis 冷启动 5.4s）。
        故：缓存 miss 且 `allow_compute=False`（主线程热路径）→ 直接返回 `None`（调用方走"计算中"，
        由后台 `_warm_ob_m30_disk`/`refresh_ob_m30` 填充）。`allow_compute=True` 仅后台线程用。
        返回 {t30,div,feats,bar_time,n_bars} / {}（已算但无数据）/ None（未热，未算）。"""
        base = str(code).split("_")[0]
        key = f"{base}_{_min_bars_slot()}"
        with _M30_SNAP_LOCK:
            if key in _M30_SNAP_CACHE:
                return _M30_SNAP_CACHE[key]
        if not allow_compute:
            return None
        snap = _build_m30_snapshot(_m30_bars_cache_only(base))
        with _M30_SNAP_LOCK:
            if len(_M30_SNAP_CACHE) > _M30_SNAP_MAX:
                _M30_SNAP_CACHE.clear()
            _M30_SNAP_CACHE[key] = snap
        return snap

    def _m30_verdict_pb(self, code):
        """建仓表「30min 判定」：读轻量缓存产出判定 dict（**主线程零计算**）。未热 → pending。
        与体检表同口径（`m30_features.verdict_from_features`）。"""
        from analysis import m30_features as _m30f
        base = str(code).split("_")[0]
        key = f"{base}_{_min_bars_slot()}"
        with _PB_M30_LOCK:
            snap = _PB_M30_CACHE.get(key)
        if snap is None:
            return {"level": "pending", "label": "⏳ 计算中", "reason": "30min 判定后台计算中…（约1s内就绪）"}
        _dv = snap.get("div") or {}
        lv, lb, rs = _m30f.verdict_from_features(
            snap.get("feats") or {}, None, _dv.get("type"), _dv.get("bars_ago"))
        return {"level": lv, "label": lb, "reason": rs}

    def _pb_m30_hist_verdict(self, code, date, compute=False):
        """历史日「30min 判定」：从**磁盘 30min 缓存**取覆盖 date 的 bars（零网络）→ 同口径判定。
        结果按 `{base}_{date}` 缓存；compute=False 时未算过返回 None（load_day 浏览历史不触发计算，
        由「扫描该日」预先算好——见 `recompute_pb`）。与今日列同口径（verdict_from_features）。"""
        from analysis import m30_features as _m30f
        base = str(code).split("_")[0]
        key = f"{base}_{date}"
        with _PB_M30_HIST_LOCK:
            if key in _PB_M30_HIST:
                return _PB_M30_HIST[key]
        if not compute:
            return None
        _v = None
        try:
            df = _min_bars_asof(base, date)
            snap = _build_m30_light(df) if (df is not None and not df.empty) else {}
            if snap:
                _dv = snap.get("div") or {}
                lv, lb, rs = _m30f.verdict_from_features(
                    snap.get("feats") or {}, None, _dv.get("type"), _dv.get("bars_ago"))
                _v = {"level": lv, "label": lb, "reason": rs, "asof": snap.get("bar_time")}
        except Exception:
            _v = None
        with _PB_M30_HIST_LOCK:
            if len(_PB_M30_HIST) > _PB_M30_HIST_MAX:
                _PB_M30_HIST.clear()
            _PB_M30_HIST[key] = _v
        return _v

    def _kick_pb_m30_warm(self, codes):
        """后台预热 30min 轻量快照（守护线程）。`_build_m30_light` ~17ms/只 ⇒ 53 只 ≈ 0.9s CPU。

        2026-10-13 队列化（与 `_kick_pb_struct_warm` 同形）：原来是「全局每 30min 时段只起一轮」
        ——建仓表 10s 轮询总是先抢到名额，风向标的 323 只同一时段内被静默丢弃 ⇒ 「30min 判定」
        整列卡 ⏳ 最长 30 分钟。改为按 code 排队、槽位内去重，worker 循环分批排干（每轮 ≤200 只，
        3 线程；`_fetch_min_bars` 有 per-slot 内存缓存，重复入队天然去重）。"""
        codes = [str(c).split("_")[0] for c in (codes or []) if c]
        if not codes:
            return
        slot = _min_bars_slot()
        with _PB_M30_WARM_LOCK:
            if _PB_M30_WARM.get("slot") != slot:
                _PB_M30_WARM.update({"slot": slot, "queued": set()})
            queued = _PB_M30_WARM["queued"]
            with _PB_M30_LOCK:
                todo = [c for c in dict.fromkeys(codes)
                        if f"{c}_{slot}" not in _PB_M30_CACHE and c not in queued]
            if not todo:
                return
            room = max(0, _PB_M30_QUEUE_MAX - len(queued))
            queued.update(todo[:room])
            if _PB_M30_WARM.get("running"):
                return                                # worker 会排干队列（含新入队的）
            _PB_M30_WARM["running"] = True

        def _run():
            from concurrent.futures import ThreadPoolExecutor
            try:
                while True:
                    with _PB_M30_WARM_LOCK:
                        batch = []
                        for c in list(_PB_M30_WARM["queued"]):
                            if len(batch) >= 200:
                                break
                            _PB_M30_WARM["queued"].discard(c)
                            batch.append(c)
                        if not batch:
                            _PB_M30_WARM["running"] = False
                            return

                    def _one(bc):
                        key = f"{bc}_{slot}"
                        with _PB_M30_LOCK:
                            if key in _PB_M30_CACHE:
                                return
                        try:
                            df = _fetch_min_bars(bc, "30min")       # 在线→磁盘兜底（后台线程）
                        except Exception:
                            df = None
                        snap = _build_m30_light(df)
                        with _PB_M30_LOCK:
                            if len(_PB_M30_CACHE) > _PB_M30_MAX:
                                _PB_M30_CACHE.clear()
                            _PB_M30_CACHE[key] = snap
                    with ThreadPoolExecutor(max_workers=3) as ex:
                        list(ex.map(_one, batch))
            except Exception:
                with _PB_M30_WARM_LOCK:
                    _PB_M30_WARM["running"] = False

        _th.Thread(target=_run, daemon=True, name="pb-m30-warm").start()

    # ---------------- 仓位结构（30min/10min 结构参考）----------------
    def load_market_beacon(self, date=None, top_n=None):
        """**市场风向标**：人气概念板块 × 该板块的代表公司（龙头 + 中军），列沿用建仓信号扫描。

        ⚠️⚠️ **纯描述性看盘，不是信号**。`_tier_sector_stocks` 的实测结论是
        「龙头/中军**无前瞻区分度**」（977 池 × 2009 日、行业口径、同板块同日配对、按日聚类 t、
        分半窗翻号；涨幅头名 vs 杂毛 H=5 仅 −0.021pp t=−0.40，**成交额头名反而 −0.302pp t=−5.7**）。
        ⇒ 本面板只回答「**今天的钱在哪个板块、谁在场**」，**不要当买卖依据**。

        口径
          · 人气板块 = `stock_hunter/history/daily_summary.json` 里 **≤ date 的最近一日**，按
            `平均分` 降序（与「选股猎手」GUI 板块表同口径）；`top_n=None/0` ⇒ **全部板块**
            （owner 2026-10-13：不再只取前 N）。
          · 成分股 = `_jiuyan_board()`（韭研主板块，与 `prescreen.sym2board_map()` 同口径）。
          · 每只的 涨幅/成交额 从**本地日线缓存**算（零网络）——`_tier_sector_stocks` 只用
            `amount` 参与**比值**（板块中位数 × 5），故单位因子整体约掉，不必纠结手/股。
          · 代表公司 = **龙头(全部) + 中军(全部)**（owner 2026-10-13 明确：中军不做截断）。

        **两级 SWR**（2026-10-13）—— 两级都**不在 pywebview 主线程上算**，这是本仓每次 GUI
        冻死的老形状。量级实测：
          · **骨架**（成分 → 涨幅/成交额 → 分档 → 龙头/中军）：全部板块要读 ~1300 个日线缓存 ≈ **6s**
          · **重列**（建仓判定/得分/通过/阶段 + 技术标签）：~330 只 ≈ **18s**
        主线程只做**缓存读**（骨架/重列命中 + 仓位结构 + 30min 判定），稳态实测 **~50ms**。
        前端按"齐没齐"切 TTL：未齐 3s 短轮询、齐了 60s（见 `web/app.js::_beaconReady`）。
        """
        # 键里带 daily_summary.json 的 mtime：猎手跑完重写该文件 ⇒ 骨架缓存立即失效、
        # 下一轮轮询即重算，板块排名与选股猎手对齐（原来要等 90s TTL + 前端 60s 轮询）。
        try:
            _sum_mt = (BASE / "stock_hunter" / "history" / "daily_summary.json").stat().st_mtime
        except OSError:
            _sum_mt = 0.0
        key = (str(date or ""), int(top_n or 0), datetime.now().strftime("%Y-%m-%d"), _sum_mt)
        with _BEACON_LOCK:
            sk = _BEACON_SKELETON
            have = sk.get("val") if sk.get("key") == key else None
            fresh = have is not None and (_time_mod.time() - sk.get("ts", 0.0)) < self._BEACON_SKELETON_TTL
            running = bool(sk.get("running"))
        if not fresh and not running:
            with _BEACON_LOCK:
                _BEACON_SKELETON["running"] = True

            def _bg_sk(_k=key, _d=date, _t=top_n):
                try:
                    v = self._beacon_skeleton(_d, _t)
                    with _BEACON_LOCK:
                        _BEACON_SKELETON.update({"key": _k, "ts": _time_mod.time(), "val": v})
                except Exception:
                    pass
                finally:
                    with _BEACON_LOCK:
                        _BEACON_SKELETON["running"] = False
            threading.Thread(target=_bg_sk, name="beacon-skel", daemon=True).start()
        if have is None:
            # 骨架还没算完：返回 pending（**不是**空错误）—— 前端短轮询即可（实测 ~6s 出）
            return _clean({"available": True, "pending": True, "sectors": [],
                           "date": "", "n_codes": 0, "enrich_pending": True})
        _day, sectors, all_codes = have
        if not all_codes:
            return _clean({"available": True, "date": _day, "trade_date": str(date or ""),
                           "pending": False, "top_n": int(top_n or 0), "sectors": sectors,
                           "n_codes": 0, "enrich_pending": False,
                           "cond_labels": COND_LABELS})
        # ── 重列：同样后台算 ──
        ekey = (_day, tuple(sorted(all_codes)))
        with _BEACON_LOCK:
            en = _BEACON_ENRICH
            rows = en.get("rows") if en.get("key") == ekey else None
            efresh = rows is not None and (_time_mod.time() - en.get("ts", 0.0)) < self._BEACON_TTL
            erunning = bool(en.get("running"))
        if not efresh and not erunning:
            with _BEACON_LOCK:
                _BEACON_ENRICH["running"] = True

            def _bg_en(_codes=list(all_codes), _date=str(date or _day), _k=ekey):
                try:
                    r = self._beacon_enrich(_codes, _date)
                    with _BEACON_LOCK:
                        _BEACON_ENRICH.update({"key": _k, "ts": _time_mod.time(), "rows": r})
                except Exception:
                    pass
                finally:
                    with _BEACON_LOCK:
                        _BEACON_ENRICH["running"] = False
            threading.Thread(target=_bg_en, name="beacon-enrich", daemon=True).start()
        # ── 主线程只做缓存读 ──
        try:
            # 2026-10-18：**offline=True**（批量视图铁律）。288 只代表股若走在线路径，每只都会
            # `_append_today_forming_bar` → 单只快照（2s 超时）⇒ 恢复/盘后高峰把 6 个工作线程
            # 全压在网络上，是卡顿大户；且风向标只做展示，日线缓存 + 批量快照已够。
            _tags_map = (self.load_stock_tags_batch(all_codes, offline=True) or {}).get("tags") or {}
        except Exception:
            _tags_map = {}
        for sec in sectors:
            for m in sec["rows"]:
                m.update((rows or {}).get(m["code"]) or {})
                m["structure"] = self._pb_struct_get(m["code"])
                m["m30_verdict"] = self._m30_verdict_pb(m["code"])
                m["tags"] = (_tags_map.get(m["code"]) or {}).get("tags") or []
        try:
            self._kick_pb_m30_warm(all_codes)      # 每 30min 时段节流一次
            # 仓位结构列（日线阶段/仓位结构/30m MA5 同源）：原来只读不预热 ⇒ 风向标本面板
            # 这三列永远 ⏳。2026-10-13 预热队列化（按 code 去重、60只/轮×2线程渐进排干、
            # 磁盘优先）后补上路，不再抢闸/饿死。
            self._kick_pb_struct_warm(all_codes)
        except Exception:
            pass
        return _clean({"available": True, "date": _day, "trade_date": str(date or ""),
                       "pending": False, "top_n": int(top_n or 0), "sectors": sectors,
                       "n_codes": len(all_codes), "enrich_pending": rows is None,
                       # 2026-10-16：时机条件列的**标签真源**（同猎手表 payload）。本表列沿用
                       # 「建仓信号扫描」那套，该列也必须同源，否则同一只票三张表三种圆点数。
                       "cond_labels": COND_LABELS})

    def _beacon_skeleton(self, date, top_n):
        """（**只许后台线程调**）人气板块 + 成分 + 分档 → `(day, sectors, all_codes)`。

        本面板最贵的一段：全部板块要读 ~1300 个日线缓存（实测 ~6s）。
        """
        from collections import defaultdict
        try:
            _sum = json.loads((BASE / "stock_hunter" / "history" / "daily_summary.json")
                              .read_text(encoding="utf-8"))
        except Exception:
            return "", [], []
        _d0 = str(date or datetime.now().strftime("%Y-%m-%d")).replace("-", "")[:8]
        _days = sorted(k for k in _sum if str(k) <= _d0)
        if not _days:
            return "", [], []
        _day = _days[-1]
        # 排序口径 = 「选股猎手」GUI 板块表：`load_concept_summary` 按 **平均分** 降序
        # （data_loader.py），前端按后端顺序渲染 ⇒ 这里必须同口径按平均分，否则两边排名错位。
        # ⚠️ 不要改成文件顺序或热度分：daily_summary.json 有两个写入方（GUI 保存=平均分序、
        #    CLI 管线=_save_sector_history=热度分序），文件顺序不稳定；只有重排平均分才恒定对齐。
        _rank = sorted((x for x in (_sum.get(_day) or []) if isinstance(x, dict)),
                       key=lambda x: -(x.get("平均分") or 0))
        if top_n:
            _rank = _rank[:max(1, int(top_n))]
        bmap = _jiuyan_board()
        members = defaultdict(list)
        for c, b in bmap.items():
            if b:
                members[b].append(c)
        names = _jiuyan_names()

        def _daily_stats(code):
            """→ (change_pct 百分数, amount)。读不到 → (None, None)（`_tier_sector_stocks` 会跳过）。

            ⚠️ 必须按 **文件 mtime** 缓存：逐只读 JSON ≈ 12ms、全池 ≈6s。mtime 既判新旧
            （盘中日线缓存会被回写），又避免同一轮里重复解析。
            """
            fp = _DAILY_KLINE_DIR / f"{code}.json"
            try:
                mt = fp.stat().st_mtime
            except OSError:
                return None, None
            hit = _BEACON_STATS.get(code)
            if hit and hit[0] == mt:
                return hit[1], hit[2]
            try:
                rows = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
                if len(rows) < 2:
                    return None, None
                cur, prev = rows[-1], rows[-2]
                pc, cc = float(prev.get("close") or 0), float(cur.get("close") or 0)
                if pc <= 0 or cc <= 0:
                    return None, None
                vol = float(cur.get("volume") or 0)
                _BEACON_STATS[code] = (mt, round((cc / pc - 1) * 100, 2), cc * vol)
                if len(_BEACON_STATS) > 8000:      # 无界增长防护（全池也就 5000+）
                    _BEACON_STATS.clear()
                return _BEACON_STATS[code][1], _BEACON_STATS[code][2]
            except Exception:
                return None, None

        sectors, all_codes = [], []
        for x in _rank:
            name = str(x.get("板块") or "")
            if not name:
                continue
            ms = []
            for c in members.get(name, []):
                pct, amt = _daily_stats(c)
                if pct is None:
                    continue
                ms.append({"code": c, "name": names.get(c, c),
                           "change_pct": pct, "amount": amt})
            _tier_sector_stocks(ms)                      # 就地打 tier
            reps = [m for m in ms if m.get("tier") in ("龙头", "中军")]
            if not reps:
                continue                                  # 该板块无分层 ⇒ 如实不显示（不硬凑）
            reps.sort(key=lambda m: (m["tier"] != "龙头", -(m.get("amount") or 0)))
            sectors.append({"name": name, "avg": x.get("平均分"), "limitup": x.get("涨停数"),
                            "members": len(members.get(name, [])), "rows": reps})
            all_codes += [m["code"] for m in reps]
        return _day, sectors, all_codes
    def _beacon_enrich(self, codes, date):
        """后台补「重列」：建仓判定/得分/通过/阶段/时机 + 技术标签。**只许后台线程调。**"""
        out = {}
        try:
            conf = self._hunter_build_conformance(codes, date) or {}
        except Exception:
            conf = {}
        # ⚠️ tags **不在这里取**：`load_stock_tags_batch` 冷启动是"起后台算 + 立即返空"，
        # 在这里取会把**空 tags 钉进 120s 缓存**（实测踩到）。它本身设计成可 10s 轮询的
        # 便宜调用（命中缓存即时返回）⇒ 放主线程，见调用点。
        for c in codes:
            g = conf.get(c) or {}
            out[c] = {
                "build_go": g.get("go"), "build_regime": g.get("regime"),
                "build_met": g.get("met"), "build_conds": g.get("conds"),
                "build_reason": g.get("reason"), "build_score": g.get("score"),
                "build_ceiling": g.get("score_ceiling"),
                "build_reachable": g.get("signal_reachable"),
                "build_verdict": g.get("verdict"), "build_veto": g.get("veto") or [],
                "build_above_ma5": g.get("above_ma5"),
                "build_above_ma20": g.get("above_ma20"), "build_stage": g.get("stage"),
                "build_price": g.get("price"),
            }
        try:
            self._kick_pb_m30_warm(codes)      # 每 30min 时段节流一次
        except Exception:
            pass
        return out

    def _pb_struct_get(self, code):
        """建仓表「仓位结构」：读缓存（**主线程零计算零网络**）。未热 → pending。"""
        base = str(code).split("_")[0]
        key = f"{base}_{_min_bars_slot()}"
        with _PB_STRUCT_LOCK:
            st = _PB_STRUCT_CACHE.get(key)
        if st is None:
            return {"ok": False, "pending": True,
                    "reason": "结构层后台计算中…（整池首轮约 10~30s，之后按 30min 时段刷新）"}
        return st

    # ---------- 当日「均线回站」+ 突破时刻（2026-10-23 owner） ----------
    # 「建仓信号扫描 / 持仓 30 分钟风险体检 / 异常监控池」三张表**同一份缓存、同一个渲染器**。
    # 口径：`core.ma_reclaim.reclaim_today`；时刻 = 今日 5min bar 里**第一次**收盘价站上 MA(p) 的那根。
    # 纪律与 30min 判定/仓位结构一致：**后台预热 + 主线程只读**、未热给 pending、前端轮询自填。
    @staticmethod
    def _ma_reclaim_ttl():
        try:
            now = datetime.now()
            hm = now.strftime("%H:%M")
            if now.weekday() >= 5 or hm >= "15:00" or hm < "09:15":
                return 6 * 3600.0
        except Exception:
            pass
        return _MA_RC_TTL

    def _ma_reclaim_compute(self, code, date):
        """单只：截至昨日的日线收盘 + 今日 5min bar → {"basis": 均线基准日, "ma": {period: {time, ma, dev_pct}}}。

        返回 None = 取数失败（停牌/北交所无分时/网络）；worker 会给它记一条**短负缓存**，
        避免每次轮询都重踢同几个不可能成功的码。
        """
        from core.ma_reclaim import reclaim_today
        c6 = str(code).split("_")[0]
        try:
            blob = json.loads((_DAILY_KLINE_DIR / f"{c6}.json").read_text(encoding="utf-8"))
        except Exception:
            return None
        closes, cdates = [], []
        for r in blob.get("rows") or []:
            d = str(r.get("date"))[:10]
            if d >= date:                      # 基准**不含当日**（MA 用「前 p-1 日 + 今日价」）
                break
            try:
                v = float(r.get("close"))
            except (TypeError, ValueError):
                continue
            if v > 0:
                closes.append(v)
                cdates.append(d)
        if len(closes) < 59:                   # 最长周期 60 天要 59 根做阈值
            return {"basis": (cdates[-1] if cdates else None), "ma": {}}
        # ⚠️ **必须先把分钟数据拿去补日线缓存缺的最近几天**，再判回站（2026-10-23 owner 实报）：
        #    监控池里的票本地日线缓存可能停在几天前（深圳华强 002056 停在 10-20），
        #    于是"昨收"是 10-20 的、昨 MA(p) 也是旧的 ⇒ 明明今天刚站上 5 日线却判成"不算"，
        #    行底色（也用旧收盘算）还会显示成跌破。分钟线一次拿 340 根 ≈ 最近 7 个交易日，
        #    按日取最后一根收盘就能把这几天补齐，零额外网络。
        bars_all = _fetch_min_bars_tencent(c6, "5min")
        if bars_all is None or getattr(bars_all, "empty", True):
            return None
        try:
            _bd = bars_all["time"].astype(str).str[:10]
        except Exception:
            return None
        _last = cdates[-1] if cdates else ""
        for _d, _g in bars_all.assign(_d=_bd).groupby("_d", sort=True):
            if _d <= _last or _d >= str(date):
                continue                       # 缓存已有的、以及今天的，都不并进来
            try:
                _c = float(_g["close"].iloc[-1])
            except Exception:
                continue
            if _c > 0:
                closes.append(_c)
                cdates.append(str(_d))
        _basis = cdates[-1] if cdates else None
        # **必要条件的廉价预筛**：回站要求「昨收 < 昨MA(p)」。用的已是**补齐后**的昨收。
        from core.ma_reclaim import RECLAIM_PERIODS
        prev_close = closes[-1]
        cand = [p for p in RECLAIM_PERIODS
                if len(closes) >= p and prev_close < float(sum(closes[-p:]) / p)]
        # 行底色要用的「现价 vs 5 日线」也用同一份新鲜基准（前端优先读这个，见 abnormal pool）
        bars = bars_all.assign(_d=_bd)
        bars = bars[bars["_d"] == str(date)]
        if bars.empty:
            return None                        # 今天还没分钟数据（盘前/停牌/北交所）
        try:
            _price = float(bars["close"].iloc[-1])
        except Exception:
            return None
        out = {"basis": _basis, "ma": {}, "price": round(_price, 3)}
        for _p, _k in ((5, "above5"), (20, "above20")):
            if len(closes) >= _p:
                _thr = float(sum(closes[-(_p - 1):]) / (_p - 1))
                out[_k] = bool(_price > _thr)
                out[_k + "_ma"] = round((_thr * (_p - 1) + _price) / _p, 3)
        if not cand:
            return out
        try:
            # 只算预筛剩的那几条均线；basis = 均线基准日（缓存陈旧时前端 tooltip 会如实标出）
            out["ma"] = reclaim_today(closes, bars, periods=cand)
            return out
        except Exception:
            return None

    def _ma_reclaim_get(self, code, date=None, kick=True):
        """读缓存；过期/未热则踢预热并（有过期值时）先给旧值（SWR）。

        `kick=False`：调用方已经批量踢过（`_attach_ma_reclaim` / OB 覆盖层），这里只读，
        避免每次轮询对每只票各加一次锁（76 只 × 每 10s 是无谓开销）。
        """
        date = str(date or datetime.now().strftime("%Y-%m-%d"))
        if date != datetime.now().strftime("%Y-%m-%d"):
            return {"na": True}                # 历史视图没有当日分钟，"突破时刻"无从谈起
        key = str(code).split("_")[0]
        with _MA_RC_LOCK:
            rec = _MA_RC_CACHE.get(key)
        now = _time_mod.time()
        fresh = _ma_rc_fresh(rec, date, now, self._ma_reclaim_ttl())
        if not fresh and kick:
            self._kick_ma_reclaim_warm([key], date)
        if rec and rec.get("date") == date:
            return {"date": date, "basis": rec.get("basis"), "ma": rec.get("ma") or {},
                    "above5": rec.get("above5"), "above20": rec.get("above20"),
                    "price": rec.get("price")}
        return {"pending": True}

    def _kick_ma_reclaim_warm(self, codes, date=None):
        """按 code 排队预热（同一 code 在队列里只排一次；单 worker 循环排干）。

        ⚠️ **必须先剔掉缓存仍然新鲜的码**（2026-10-23 实盘修）：原来只按"在不在队列里"去重，
        而建仓表每 10s 就 `_attach_ma_reclaim` 踢一次 48 只 ⇒ 缓存明明没过期也被重新排队重算
        ⇒ worker **永不停歇**（实测常驻 5~12% 单核），新来的票（持仓体检那 10 只）排在后面等一分钟，
        界面上就是「均线突破时刻更新太慢」。
        """
        date = str(date or datetime.now().strftime("%Y-%m-%d"))
        code_list = [str(c).split("_")[0] for c in (codes or []) if c][:_MA_RC_QUEUE_MAX]
        if not code_list:
            return
        _now = _time_mod.time()
        _ttl = self._ma_reclaim_ttl()
        with _MA_RC_LOCK:
            fresh = {c for c in code_list
                     if _ma_rc_fresh(_MA_RC_CACHE.get(c), date, _now, _ttl)}
        todo_all = [c for c in dict.fromkeys(code_list) if c not in fresh]
        if not todo_all:
            return                            # 全都新鲜 ⇒ 一次都不排
        with _MA_RC_WARM_LOCK:
            todo = [c for c in todo_all if c not in _MA_RC_WARM["queued"]]
            room = max(0, _MA_RC_QUEUE_MAX - len(_MA_RC_WARM["queued"]))
            _MA_RC_WARM["queued"].update(todo[:room])
            if _MA_RC_WARM["running"] or not _MA_RC_WARM["queued"]:
                return
            _MA_RC_WARM["running"] = True

        def _run():
            global _MA_RC_CACHE
            try:
                while True:
                    with _MA_RC_WARM_LOCK:
                        batch = list(_MA_RC_WARM["queued"])[:20]
                        for c in batch:
                            _MA_RC_WARM["queued"].discard(c)
                        if not batch:
                            _MA_RC_WARM["running"] = False
                            return
                    for c in batch:
                        try:
                            # ⚠️ **不抢 `_BG_PANDAS_SEM`**：本活 95% 在等网络（腾讯 m5 约 150ms/只），
                            #    pandas 部分只有一次 JSON 解析 + 极小数组。抢那 2 个名额会和标签批
                            #   排在一起 ⇒ 实测「突破时刻」要等一分钟才自填（owner 报"更新太慢"）。
                            #   GIL 用「每只之间睡 5ms」让出（同 `_tags_one_yield` 的做法）。
                            ma = self._ma_reclaim_compute(c, date)
                        except Exception:
                            ma = None
                        finally:
                            _time_mod.sleep(0.005)
                        if ma is None:
                            # 取数失败（停牌/北交所无分时/网络）⇒ 记一个 **短负缓存**，
                            # 否则每次轮询都会重踢、把队列打满（失败的重试没有收益）
                            with _MA_RC_LOCK:
                                _MA_RC_CACHE[c] = {"date": date, "ts": _time_mod.time(),
                                                   "basis": None, "ma": {}, "err": True}
                            continue
                        with _MA_RC_LOCK:
                            if len(_MA_RC_CACHE) > 2000:
                                _MA_RC_CACHE.clear()
                            _MA_RC_CACHE[c] = {"date": date, "ts": _time_mod.time(),
                                               "basis": ma.get("basis"), "ma": ma.get("ma") or {},
                                               "above5": ma.get("above5"), "above20": ma.get("above20"),
                                               "price": ma.get("price")}
            except Exception:
                pass
            finally:
                with _MA_RC_WARM_LOCK:
                    _MA_RC_WARM["running"] = False

        _th.Thread(target=_run, name="ma-reclaim-warm", daemon=True).start()

    def _attach_ma_reclaim(self, rows, date=None):
        """给一批行（dict，含 code）挂上 `ma_reclaim` 字段，并踢预热。"""
        rows = [r for r in (rows or []) if isinstance(r, dict) and r.get("code")]
        if not rows:
            return
        date = str(date or datetime.now().strftime("%Y-%m-%d"))
        self._kick_ma_reclaim_warm([r["code"] for r in rows], date)
        for r in rows:
            try:
                r["ma_reclaim"] = self._ma_reclaim_get(r["code"], date, kick=False)
            except Exception:
                r["ma_reclaim"] = {"pending": True}

    def _pb_struct_hist(self, code, date, compute=False):
        """历史日「仓位结构」：**纯磁盘、零网络**、截到该日（`load_structure(allow_network=False)`）。
        compute=False 时未算过返回 None（浏览历史不触发计算，由「扫描该日」预算好）。"""
        base = str(code).split("_")[0]
        key = f"{base}_{date}"
        with _PB_STRUCT_HIST_LOCK:
            if key in _PB_STRUCT_HIST:
                return _PB_STRUCT_HIST[key]
        if not compute:
            return None
        try:
            from core.position_structure_feed import load_structure
            _st = load_structure(base, date_str=date, allow_network=False)
        except Exception as e:
            _st = {"ok": False, "reason": str(e)[:80]}
        with _PB_STRUCT_HIST_LOCK:
            if len(_PB_STRUCT_HIST) > _PB_STRUCT_HIST_MAX:
                _PB_STRUCT_HIST.clear()
            _PB_STRUCT_HIST[key] = _st
        return _st

    def _kick_pb_struct_warm(self, codes, force=False):
        """后台预热「仓位结构」（守护线程）。磁盘优先（`load_structure` 内部：新鲜 plain 档 →
        陈旧才出网补并落盘）。`max_workers=2`：比 30min 判定列克制——本层每只要多打一次 5min（~0.5s）。

        2026-10-13 队列化：原来是「全局每 30min 时段只放一轮」——建仓表 10s 轮询总是先抢到
        名额，OB 表/风向标的请求同一时段内被静默丢弃 ⇒ 持仓股结构列最长卡 30 分钟全 ⏳。
        改为**按 code 排队**：槽位内去重（已缓存/已排队的跳过），worker 循环分批排干
        （每轮 ≤`_PB_STRUCT_POOL_MAX` 只）。`force` 保留兼容（新设计下 per-code 去重已幂等，
        两种调用等价）。"""
        codes = [str(c).split("_")[0] for c in (codes or []) if c]
        if not codes:
            return
        slot = _min_bars_slot()
        with _PB_STRUCT_WARM_LOCK:
            if _PB_STRUCT_WARM.get("slot") != slot:
                _PB_STRUCT_WARM.update({"slot": slot, "queued": set()})
            queued = _PB_STRUCT_WARM["queued"]
            with _PB_STRUCT_LOCK:
                todo = [c for c in dict.fromkeys(codes)
                        if f"{c}_{slot}" not in _PB_STRUCT_CACHE and c not in queued]
            if not todo:
                return
            room = max(0, _PB_STRUCT_QUEUE_MAX - len(queued))
            queued.update(todo[:room])
            if _PB_STRUCT_WARM.get("running"):
                return                                # worker 会排干队列（含新入队的）
            _PB_STRUCT_WARM["running"] = True

        def _run():
            from concurrent.futures import ThreadPoolExecutor
            from core.position_structure_feed import load_structure
            try:
                while True:
                    with _PB_STRUCT_WARM_LOCK:
                        batch = []
                        for c in list(_PB_STRUCT_WARM["queued"]):
                            if len(batch) >= _PB_STRUCT_POOL_MAX:
                                break
                            _PB_STRUCT_WARM["queued"].discard(c)
                            batch.append(c)
                        if not batch:
                            _PB_STRUCT_WARM["running"] = False
                            return

                    def _one(bc):
                        key = f"{bc}_{slot}"
                        with _PB_STRUCT_LOCK:
                            if key in _PB_STRUCT_CACHE:
                                return
                        try:
                            with _BG_PANDAS_SEM:             # 全局最多 2 个 pandas 批活并发
                                st = load_structure(bc)      # 后台线程，允许出网
                        except Exception as e:
                            st = {"ok": False, "reason": str(e)[:80]}
                        with _PB_STRUCT_LOCK:
                            if len(_PB_STRUCT_CACHE) > _PB_STRUCT_MAX:
                                _PB_STRUCT_CACHE.clear()
                            _PB_STRUCT_CACHE[key] = st
                        # 2026-10-22：**每只之间让一次 GIL**。4 个 pandas 线程连续跑时，pywebview
                        # 的 UI 线程连 `Thread.start()`（每个 js_api 调用都要新起线程、并等它被调度）
                        # 都抢不到切换 ⇒ 界面上表现为"点什么都没反应"，而后端 CPU 才 1 核。
                        # macOS/Win 上 sleep(0) 不保证让出，用 5ms（单只 0.5s 的量级下无感）。
                        _time_mod.sleep(0.005)
                    # 2026-10-17：2 → 4。每只已从 1.8s 降到 ~0.5s（见 position_structure_feed
                    # 的日线缓存 + position_structure 的 strftime 优化），4 线程把 10 只持仓的
                    # 排干时间从 7.3s 压到 ~2s；仍是**固定小池**（不是每请求开线程），
                    # 不会重演「线程爆炸拖死 UI 线程」那类事故。
                    with ThreadPoolExecutor(max_workers=4) as ex:
                        list(ex.map(_one, batch))
            except Exception:
                with _PB_STRUCT_WARM_LOCK:
                    _PB_STRUCT_WARM["running"] = False

        _th.Thread(target=_run, daemon=True, name="pb-struct-warm").start()

    def _kick_pb_struct_hist_warm(self, codes, date):
        """**历史日**「仓位结构」后台预热（守护线程，纯磁盘、零网络）。

        ⚠️ 必须异步：单只中位 0.72s（要解析整档 `_d540` JSON 再截到该日），50 只 ≈55s。
        原先同步跑在 `recompute_pb` 里 ⇒ 前端 20s 投递层上限直接打爆，
        表现为「重跑失败: 后端 recompute_pb 超时 20000ms」，而后端其实还在算。"""
        codes = [str(c).split("_")[0] for c in (codes or []) if c][:_PB_STRUCT_POOL_MAX]
        if not codes:
            return

        def _run():
            try:
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as ex:
                    list(ex.map(lambda c: self._pb_struct_hist(c, date, compute=True), codes))
            except Exception:
                pass

        _th.Thread(target=_run, daemon=True, name="pb-struct-hist-warm").start()

    def load_structure_batch(self, codes):
        """批量取「仓位结构」（**主线程零计算零网络**）：只回**已热**的，未热的进后台预热。

        契约与 `load_stock_tags_batch` 一致（前端**只缓存非空**、空则稍后重试）——但本接口
        天然会回"部分就绪"，前端按 code 增量填充即可。返回 {"structures": {code: st}, "pending": n}。

        ⚠️ 猎手池可达 5000+ 只，**绝不整池预热**：只预热本次传入的代码，且上限
        `_PB_STRUCT_POOL_MAX`（调用方按"展开的板块"传入，通常几十~几百只）。
        """
        codes = [str(c).split("_")[0] for c in (codes or []) if c]
        if not codes:
            return _clean({"structures": {}, "pending": 0})
        out = {}
        for c in codes:
            s = self._pb_struct_get(c)
            if not s.get("pending"):
                out[c] = s
        n_pending = len(codes) - len(out)
        if n_pending:
            # 只对**尚未命中缓存**的排队，且限流（猎手池可达数千只）。限"未热的"而非"入参前 N 个"：
            # 前端会重试，每轮已热的自然出队 ⇒ 逐轮推进剩下的，不会永远卡在同一批。
            _todo = [c for c in codes if c not in out][:_PB_STRUCT_POOL_MAX]
            self._kick_pb_struct_warm(_todo, force=True)
        return _clean({"structures": out, "pending": n_pending})

    def _warm_ob_m30_disk(self, codes):
        """后台**磁盘**预热 OB 30min 快照（零网络，比在线刷新快 ~2s 内填满）。仅后台线程调用。"""
        try:
            from concurrent.futures import ThreadPoolExecutor
            codes = [str(c).split("_")[0] for c in (codes or [])]
            if not codes:
                return
            with ThreadPoolExecutor(max_workers=1) as ex:      # CPU 密集，GIL 串行 ⇒ 1 线程足够、少抢
                list(ex.map(lambda c: self._m30_top_snapshot(c, allow_compute=True), codes))
        except Exception:
            pass

    def _kick_ob_m30_disk(self, codes):
        """**按需**磁盘预热当前 slot 的 OB 30min 快照（零网络、单飞）。

        为什么必须按需：`refresh_ob_m30`（在线）现在只在交易时段跑；周末/盘后每 30 分钟
        `_min_bars_slot()` 就变一次，若没人重算，`_M30_SNAP_CACHE` 里只有旧 slot 的键 ⇒
        体检表「30min 判定」永远「计算中」（2026-10-18 owner 截图）。这里在 OB 表发现
        当前 slot 未热时补一次**磁盘**计算（`load_minute_asof` 零网络），下一轮 3s 轮询即显示。"""
        codes = [str(c).split("_")[0] for c in (codes or []) if c]
        if not codes:
            return
        with _OB_M30_DISK_LOCK:
            if _OB_M30_DISK_WARM.get("running"):
                return
            _OB_M30_DISK_WARM["running"] = True
            _OB_M30_DISK_WARM["slot"] = _min_bars_slot()

        def _bg():
            try:
                self._warm_ob_m30_disk(codes)
            except Exception:
                pass
            finally:
                with _OB_M30_DISK_LOCK:
                    _OB_M30_DISK_WARM["running"] = False
        _th.Thread(target=_bg, daemon=True, name="ob-m30-disk-warm").start()

    def refresh_ob_m30(self, codes=None):
        """后台预热当前 30min 时段内的持仓 OB 快照（走 `_fetch_min_bars` ⇒ 腾讯优先）。
        只在**后台线程**调用（会出网）。codes 缺省 = 持仓（qty>0）∪ 自动盘持仓。失败静默。

        2026-10-18：**仅交易时段出网**。周末/盘后调这条链毫无收益，而卡死栈实测
        3 个 worker 同时卡在 tushare 的 `getaddrinfo`（DNS 不受 timeout 管）⇒ 直接返回。"""
        if not _in_trading_session():
            return
        try:
            from concurrent.futures import ThreadPoolExecutor
            if codes is None:
                codes = []
                for fp in (HOLDINGS_MANUAL,):
                    cur = _load_json(fp, {})
                    for code, info in cur.items():
                        if isinstance(info, dict) and not code.startswith("_") and (info.get("qty") or 0):
                            bc = str(code).split("_")[0]
                            if bc and bc not in codes:
                                codes.append(bc)
            codes = [str(c).split("_")[0] for c in (codes or [])]
            if not codes:
                return
            slot = _min_bars_slot()

            def _one(bc):
                key = f"{bc}_{slot}"
                try:
                    df = _fetch_min_bars(bc, "30min")       # 在线→磁盘兜底（后台线程）
                except Exception:
                    df = None
                snap = _build_m30_snapshot(df)
                if not snap:                                # 失败/空 → 不覆盖已热（可能是磁盘档）
                    return
                with _M30_SNAP_LOCK:
                    if len(_M30_SNAP_CACHE) > _M30_SNAP_MAX:
                        _M30_SNAP_CACHE.clear()
                    _M30_SNAP_CACHE[key] = snap             # 在线档更新鲜 → 覆盖磁盘预热
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(_one, codes))
        except Exception:
            pass

    def _trend30_trend(self, code):
        """30 分钟趋势判定（2026-10-04 方案）。返回 (trend|None, src)。
        不可用（开关关闭/网络/根数不足）→ (None, src)，调用方回退日线斜率。"""
        if not _TREND30_ENABLED:
            return None, "disabled"
        try:
            from analysis.trend30.adapter import get_trend30
            r = get_trend30(code)
        except Exception as e:
            return None, f"error:{type(e).__name__}"
        if r.get("source") != "30min" or not r.get("trend"):
            return None, str(r.get("source") or "error")
        return r["trend"], "30min"

    def _stock_tags_one(self, code, offline=False, date=None):
        """单只标签（**跨批次缓存**）→ `_stock_tags_compute`。

        2026-10-18：批量缓存按代码集合指纹存 ⇒ 猎手/风向标/建仓表的集合不同，同一只票会
        重复算（每只约 300ms 的 pandas）。这里按 `(6位码, offline, 日期)` 再缓存一层，TTL 120s，
        三个调用方互相复用，显著降低后台 CPU 尖峰（卡顿的直接来源之一）。
        `date`（2026-10-23）：猎手看历史日时必须按**那一天**的日线切片算，键里必须带日期，
        否则今天的标签会被当成历史日的。"""
        _c = str(code).split("_")[0]
        _ck = (_c, bool(offline), str(date or "")[:10])
        _now = _time_mod.time()
        with _TAGS_LOCK:
            _hit = _TAGS_ONE_CACHE.get(_ck)
        if _hit and (_now - _hit[0]) < _tags_ttl_now():
            return _hit[1]
        _res = self._stock_tags_compute(code, offline, date)
        with _TAGS_LOCK:
            if len(_TAGS_ONE_CACHE) > _TAGS_ONE_MAX:
                _TAGS_ONE_CACHE.clear()
            _TAGS_ONE_CACHE[_ck] = (_time_mod.time(), _res)
        return _res

    def _stock_tags_compute(self, code, offline=False, date=None):
        """单只股票技术标签 → `_stock_tags_from_df`。`offline=True`：批量视图用，**零网络**。

        **2026-10-11 性能修复：日线改为「本地缓存优先」，缺才回退 provider。**
        原因（实测）：`facade.daily()` 是 **GM 优先**，而 GM 调用池是 `max_workers=1`
        （`facade._gm_call` 为硬超时**故意**如此）⇒ 逐只 GM 调用**全部串行**，
        6 线程并发形同虚设：150 只 **44.0s**（293ms/只）。
        改读本地日线缓存后 6 线程**真并行**：**150 只 2.0s（13ms/只，22×）**，
        且**趋势仍走 30min**（`t30=None` 不传）⇒ **显示口径完全不变**。
        顺带：本仓已有 `_hunter_build_conformance` 走同样的"读日线缓存"零网络路线。
        """
        import pandas as pd
        # ① 本地缓存优先（`_daily_rows_cache_first` 内部还会用**批量快照**补当日 forming bar，零网络）
        df = None
        _today = datetime.now().strftime("%Y-%m-%d")
        _d = str(date or "")[:10]
        _hist = bool(_d and _d < _today)          # 历史日回看（2026-10-23）
        try:
            _rows = self._daily_rows_cache_first(code)
            if _hist and _rows:
                _rows = [r for r in _rows if str(r.get("date"))[:10] <= _d]
            if _rows and len(_rows) >= 30:
                df = pd.DataFrame(_rows)
        except Exception:
            df = None
        if df is not None:
            # `offline=True`（猎手批量）：当日 bar 已由 `_daily_rows_cache_first` 用**批量快照**补过
            #   （零网络）⇒ 到此为止。**不能**再走下面那段补 bar：它调 `_live_quote_forming`
            #   （逐只网络快照，且同为 GM 优先 ⇒ 又串行化，~1.9s/只），会把刚拿到的提速全还回去。
            # `offline=False`（建仓表/持仓体检，小池且喂决策）：缓存只省了"取日线"，
            #   当日 bar 仍按老逻辑用实时快照补，**行为与此前一致**。
            if not offline and not _hist:
                df = self._append_today_forming_bar(df, code)
            # 历史日：30min 趋势拿不到当天的（那接口只有实时）⇒ 显式转日线斜率，别拿今天的冒充
            return self._stock_tags_from_df(df, code, t30=False if _hist else None)
        # ② 本地无缓存（或过旧被新鲜度闸拦掉）→ 回退网络；这类标的数量少，不拖累批量
        from core.position_builder import fetch_daily_kline
        df = fetch_daily_kline(code)
        if df is None or df.empty or len(df) < 30:
            return {"trend": "flat", "tags": []}
        # P0: ifzq K线主机被 WAF 501 拦截时 fetch_daily_kline 静默回退旧缓存（缺当日 forming bar），
        # 破5/10日线 等标签会用昨日收盘误判（现价已站上均线仍显示破线）。补当日实时 forming bar。
        df = self._append_today_forming_bar(df, code)
        return self._stock_tags_from_df(df, code)

    def _append_today_forming_bar(self, df, code):
        """日线末根不是今天时，用**实时快照**补一根当日 forming bar（可能出网，仅小池/回退路径用）。"""
        import pandas as pd
        try:
            # 仅当实时快照时间戳为今日才补 forming bar：开盘前/快照陈旧时腾讯返回昨收，
            # 补进去会把昨收重复计入 MA5 → cur 看似低于虚高的 MA5，误判"破5日线"（08-28 事故）。
            if str(df["date"].iloc[-1]) != datetime.now().strftime("%Y-%m-%d"):
                live = self._live_quote_forming(code)
                if live and live.get("ts_date") == datetime.now().strftime("%Y-%m-%d"):
                    fb = pd.DataFrame([{"date": datetime.now().strftime("%Y-%m-%d"),
                                        "open": live["open"], "close": live["price"],
                                        "high": live["high"], "low": live["low"],
                                        "volume": live["volume"]}])
                    df = pd.concat([df, fb], ignore_index=True)
        except Exception:
            pass
        return df

    def _stock_tags_from_df(self, df, code, t30=None):
        """由**日线 df** 计算技术标签（纯计算，无取数）。批量标签与持仓体检共用同一口径。

        `t30`（可选，2026-10-09）：调用方传入的 30min 趋势 dict，避免主线程再出网——
          · None（默认）→ 走既有 `_trend30_trend`（建仓表/批量路径，可能出网）；
          · dict 且含 trend → 直接用（OB 命中已预热快照）；
          · False → 显式离线（OB 无 30min 数据），直接回退日线斜率。"""
        import numpy as np
        import pandas as pd
        # 2026-10-11：剔除**尾部**「无成交且无振幅」的占位根（盘前快照造的假当日 bar）。
        # 写入侧已加闸（`_mk_forming_bar`），但**磁盘上已存在的坏 payload 也要能自愈**——
        # 实测 600362 的 payload 里那根假 10-09（close=41.5、vol=0）会把 MA5/cur 污染成
        # 「破5日线」，而真实收盘 42.51 是站上。只剔尾部：中间根可能是停牌/无成交的真实历史。
        while len(df) > 0:
            _last = df.iloc[-1]
            if (float(_last.get("volume") or 0) <= 0
                    and float(_last.get("high") or 0) == float(_last.get("low") or 0)):
                df = df.iloc[:-1]
            else:
                break
        if len(df) < 2:
            return {"trend": "flat", "tags": []}
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        volumes = df["volume"].values
        cur = float(closes[-1])
        n = len(closes)

        # 通道方向（2026-10-04：改按 30 分钟趋势判定；不可用时回退日线斜率）
        recent = df.tail(40)
        rc = recent["close"].values
        slope = np.polyfit(np.arange(len(rc)), rc, 1)[0]
        norm = slope / (rc.mean() or 1e-9)
        daily_trend = "up" if norm > 0.0015 else ("down" if norm < -0.0015 else "flat")
        if t30 is False:                       # 显式离线（OB 无 30min 数据）→ 禁止出网
            trend, _trend_src = daily_trend, "daily"
        elif isinstance(t30, dict) and t30.get("trend"):   # OB 已预热快照 → 直接复用（单一口径）
            trend, _trend_src = t30["trend"], "30min"
        else:                                  # 默认路径：建仓表/批量（可能出网）
            trend, _trend_src = self._trend30_trend(code)
            if trend is None:                  # 30min 不可用 → 回退日线（保持旧行为）
                trend, _trend_src = daily_trend, "daily"

        # 精密箱体（365日滑窗+斜率+触及验证+重叠合并）
        boxes = self._detect_boxes(df)
        cur_box = next((b for b in boxes if b.get("rel") == 0), None)
        near_box = boxes[0] if boxes else None

        tags = []
        # 箱体位置 + 突破/跌破
        if cur_box:
            lo, hi = cur_box["low"], cur_box["high"]
            if hi > lo:
                pos = (cur - lo) / (hi - lo)
                if pos > 0.85:
                    tags.append({"label": "箱体上沿", "color": "up"})
                elif pos < 0.15:
                    tags.append({"label": "箱体下沿", "color": "down"})
                else:
                    tags.append({"label": "箱体内部", "color": "neutral"})
                if cur > hi:
                    pct = (cur - hi) / hi * 100
                    if pct <= 8:
                        tags.append({"label": "向上突破", "color": "up"})
                    else:
                        # 高于箱体上沿 >8% → 已完全脱离箱体（区别于刚突破的"向上突破"）
                        tags.append({"label": "完全突破", "color": "up"})
                elif cur < lo:
                    tags.append({"label": "跌破下沿", "color": "down"})
        elif near_box and cur > near_box["high"]:
            pct = (cur - near_box["high"]) / near_box["high"] * 100
            if pct <= 8:
                tags.append({"label": "向上突破", "color": "up"})
            else:
                tags.append({"label": "完全突破", "color": "up"})

        # 筑底/筑顶（近20日横盘）
        win = closes[-20:]
        win_vol = volumes[-20:]
        vol_shrink = win_vol.mean() < (volumes[-60:-20].mean() or 1e9) * 0.8 if len(volumes) >= 60 else False
        price_flat = (max(win) - min(win)) / (win.mean() or 1e-9) < 0.10
        if price_flat and trend == "flat":
            if cur_box and cur < (cur_box["low"] + cur_box["high"]) / 2:
                tags.append({"label": "筑底" if vol_shrink else "筑底中", "color": "neutral"})
            elif cur_box and cur > (cur_box["low"] + cur_box["high"]) / 2:
                tags.append({"label": "筑顶", "color": "warn"})

        # 背离（**日线**口径，2026-10-17 重写）
        #
        # ⚠️ 这里原先是 `analysis/divergence.py` 的**一份手抄副本**（只比"最后两个局部高点"），
        #    漏掉了主模块早已补上的三道闸，实测把 owner 坑了：
        #      ① **没有新鲜度** ⇒ 13 个交易日前的顶背离一直挂在标签上
        #         （002536 飞龙股份：09-21 的高点，10-16 已从 56.8 跌到 40.6，标签还在报顶背离）；
        #      ② **没有摆动门槛** ⇒ 同一段筑顶里的两个小峰被当成"两个顶"
        #         （300475 案例：两谷仅隔 5 根、中间只反弹 2.26%）；
        #      ③ **没有价格超出量** ⇒ 高出 0.1% 就算新高（002202 案例）。
        #    ⇒ 改为**直接调用** `analysis.divergence.detect_daily_divergence`
        #      （内部 = detect_divergence_events：DIF 分区 + PRICE_EXCESS 0.3% +
        #       SWING_MULT_TOP 4.0 摆动深度 + DAILY_ALERT_MAX_AGE_BARS=4 新鲜度）。
        #    模块 docstring 已声明「日线单次 MACD 背离本身未验证」⇒ 标签只是参考，不是信号。
        try:
            from analysis.divergence import detect_daily_divergence as _det_daily
            _dv = _det_daily(df) or {}
        except Exception:
            _dv = {}
        # 标签**必须带周期**（owner 2026-10-17：否则看不出是日线还是 30 分钟，而右边
        # 「30min判定」用的是 30 分钟口径 —— 两列不一致时根本无从对账）。
        if _dv.get("type") == "顶背离":
            tags.append({"label": "日线顶背离", "color": "warn",
                         "title": "日线 MACD 顶背离（近 60 个交易日高低点）· 确认于 %s 根前（%s）"
                                  % (_dv.get("bars_ago"), _dv.get("time", ""))})
        elif _dv.get("type") == "底背离":
            tags.append({"label": "日线底背离", "color": "neutral",
                         "title": "日线 MACD 底背离（近 60 个交易日高低点）· 确认于 %s 根前（%s）"
                                  % (_dv.get("bars_ago"), _dv.get("time", ""))})

        # RSI — Wilder 平滑（2026-09-21 统一口径）
        from analysis.indicators import wilder_rsi as _wilder_rsi2
        rsi = _wilder_rsi2(pd.Series(closes), 14).values

        # 超买/超卖
        cur_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        if cur_rsi > 70:
            tags.append({"label": "超买", "color": "warn"})
        elif cur_rsi < 30:
            tags.append({"label": "超卖", "color": "neutral"})

        # 破5日线/破10日线（现价处于均线下方）
        # 2026-10-13：标签带上**实测数字**（`cur`/`ma`，均为**日线**口径）。
        #   owner 第 3 次拿 30 分钟图的 MA5 来对账（600362：图上 30min MA5=40.94「站上」，
        #   而日线 5 日线=41.712「在下方」——两个数根本不是一个东西）。悬停即见数字，
        #   不必再问；这也是本仓第 3 次因这一列产生"系统报错了"的误判。
        _ma5_s = pd.Series(closes).rolling(5).mean()
        _ma10_s = pd.Series(closes).rolling(10).mean()
        if not np.isnan(_ma5_s.iloc[-1]) and cur < _ma5_s.iloc[-1]:
            tags.append({"label": "破5日线", "color": "down",
                         "cur": round(cur, 3), "ma": round(float(_ma5_s.iloc[-1]), 3)})
        if not np.isnan(_ma10_s.iloc[-1]) and cur < _ma10_s.iloc[-1]:
            tags.append({"label": "破10日线", "color": "down",
                         "cur": round(cur, 3), "ma": round(float(_ma10_s.iloc[-1]), 3)})

        # 通道标签（放最前）
        trend_label = {"up": {"label": "上行", "color": "up"},
                       "down": {"label": "下行", "color": "down"},
                       "flat": {"label": "震荡", "color": "neutral"}}[trend]
        tags.insert(0, trend_label)

        # 均线粘合（2026-10-23 owner）：MA5/10/20/30/60 的相对离散度 ≤3%。
        # ⚠️ **描述性标签，不参与任何打分/排序** —— 先做的实验（666,205 股票·日 / 1932 只 /
        #    700 天）显示它描述的是"低波动蓄势"，没有正向 alpha：
        #      spread≤3% 的 T+5 超额 −0.07%(t=−2.75)，越紧越差（≤1.5% 时 −0.22%, t=−3.62），
        #      未来 5 日振幅 7.1% vs 非粘合 10.9%；分年符号还翻转（2023 +0.12 / 2026 −0.11）。
        #    见 doc/research/2026-10-23_均线粘合_前向收益实验.md。
        try:
            from core.ma_reclaim import convergence_state as _conv_state
            _cv = _conv_state(closes) if n >= 60 else None
            if _cv and _cv.get("conv"):
                # 两种口径：五线(5/10/20/30/60)≤3% 或 短线(5/10/20)≤2%（2026-10-23 补短口径，
                # 股民说的"粘合"多半指短三条；实测两者都无正向 alpha，只是描述）
                tags.insert(1, {"label": "均线粘合", "color": "neutral",
                                "title": "%s粘合：五线(5/10/20/30/60) 离散 %.1f%%（≤3%%）· "
                                         "短线(5/10/20) %.1f%%（≤2%%）· 低波动蓄势；"
                                         "实测无正向 alpha（五线≤3%% 时 T+5 超额 −0.07%%），仅描述"
                                         % (_cv.get("why") or "均线", _cv["spread"],
                                            _cv.get("spread_short") or 0.0)})
        except Exception:
            pass

        box_pos = None
        if cur_box and cur_box["high"] > cur_box["low"]:
            box_pos = round((cur - cur_box["low"]) / (cur_box["high"] - cur_box["low"]), 2)
        # 2026-10-23 owner：四张表把「理由/依据」列换成**偏离 365 日线**
        # =（现价 − MA365）/ MA365 × 100。不足 365 根 → None（前端显示 —，不猜）。
        dev365 = None
        try:
            if n >= 365:
                _m365 = float(np.mean(closes[-365:]))
                if _m365 > 0:
                    dev365 = round((cur - _m365) / _m365 * 100, 2)
        except Exception:
            dev365 = None
        return {"trend": trend, "box_pos": box_pos, "price": round(cur, 3),
                "dev365": dev365, "trend_src": _trend_src, "tags": tags[:7]}

    @staticmethod
    def _tags_partial(codes, offline, date=None):
        """从**逐只**缓存里捞已经算好的标签（`_TAGS_ONE_CACHE`，TTL 同 `_tags_ttl_now`）。

        批量路径的 `_TAGS_CACHE` 是整批算完才落盘的，而 `_stock_tags_one` 每算完一只就写
        逐只缓存 ⇒ 用这个做**增量交付**：前端每 3s 来问一次，能拿几只拿几只。
        """
        now = _time_mod.time()
        try:
            ttl = _tags_ttl_now()
        except Exception:
            ttl = 120.0
        out = {}
        with _TAGS_LOCK:
            for c in codes or []:
                base = str(c).split("_")[0]
                hit = _TAGS_ONE_CACHE.get((base, bool(offline), str(date or "")[:10]))
                if hit and (now - hit[0]) < ttl and isinstance(hit[1], dict):
                    out[c] = hit[1]
        return out

    def load_stock_tags_batch(self, codes, offline=False, date=None):
        """批量拉技术标签（并发，ThreadPoolExecutor），带 TTL 缓存 + 后台异步重算。
        返回 {code: {trend, box_pos, tags}}。GUI 每 10s 轮询 refresh_pb 时走缓存即时返回，
        避免 7-12s 的批量计算（pandas + 网络）阻塞 pywebview 主线程冻结界面。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        global _TAGS_RUNNING
        codes = [str(c) for c in (codes or []) if c]
        if not codes:
            return _clean({"tags": {}})
        today = datetime.now().strftime("%Y-%m-%d")
        now = _time_mod.time()
        # P0 修复(2026-09-01): 缓存 key 加 codes 指纹——此前只按日期键 → 突破扫描 17 批/不同板块
        # 全部命中第 1 批缓存，只扫前 80 只且互相污染标签。改为 (today, codes指纹) 隔离各批次。
        import hashlib
        _codes_fp = hashlib.md5(",".join(sorted(set(codes))).encode()).hexdigest()[:12]
        _dkey = str(date or "")[:10]
        _cache_key = f"{today}:{_dkey}:{_codes_fp}"

        def _compute():
            result = {}
            # ⚠️ 并发度 40 → 6（2026-10-10）。这是 GUI「卡死」的**主因**，实测证据：
            #   一次卡死现场抓 6 份线程栈（t_io/logs/gui_thread_dumps.log），里面
            #   25~38 个线程常驻在 pandas，且 160/162 个来自 `futures._worker`——
            #   业务帧高度集中在本批算上：`_stock_tags_from_df` 121、`_stock_tags_one` 120、
            #   `adapter.get_trend30` 119、`evaluate_bars` 101。
            #   为什么线程数会引发"卡死"而不是"慢"：pywebview 的 `js_bridge_call` 是**在 UI 线程上**
            #   执行 `Thread(target=_call).start()` 的（edgechromium.py:251 → util.py:336），
            #   而 `Thread.start()` 会**阻塞到新 OS 线程真正被调度**（threading.py:969 `_started.wait()`）。
            #   环境里有 100+ 线程时这个等待变长 ⇒ UI 线程跑不了 `ExecuteScriptAsync` 回调 ⇒
            #   那些 `semaphore.acquire()`（无超时，edgechromium.py:160）的线程永不退出 ⇒ 线程更多
            #   ⇒ **自我强化的死亡螺旋**。
            #   为什么降到 6 几乎不损吞吐：这些线程大部分时间在等 facade 那个**单 worker 的 GM 池**
            #   （栈里可见 `facade.py:_gm_call`），40 并发换不到并行度，只换来 40 倍线程压力。
            #   代价：标签批算整体变慢，首次进建仓表/破位表时标签"稍后才自填"（前端本就轮询补齐）。
            def _tags_one_yield(c):
                """单只标签 + **让一次 GIL**（2026-10-22）。

                纯 pandas 活（30min 状态机 ~0.3s/只）；3 个线程连着跑时，pywebview 的 UI 线程
                连 `Thread.start()`（每个 js_api 调用都要新起线程并等它被调度）都可能被饿到
                ⇒ 界面上"点什么都没反应"、后端 CPU 却只有 1 核。每只之间睡 5ms 基本无感
                （单只 0.3s 量级），但能把 GIL 稳定让出去。
                """
                try:
                    with _BG_PANDAS_SEM:                 # 与仓位结构预热**共用**这 2 个名额
                        return self._stock_tags_one(c, offline, date)
                finally:
                    _time_mod.sleep(0.005)

            # ① 先把 30min bars **8 路并发取回来**（纯网络 I/O，不占 pandas 名额）。
            #    不拆的话，每只 190ms 的取数会占满 `_BG_PANDAS_SEM` 的 2 个名额
            #    ⇒ 50 只的板块 30~40s 才出标签（owner 2026-10-23 报「技术标签太慢」）。
            try:
                from analysis.trend30.adapter import prefetch_30min as _pf30
                _pf30(codes, workers=8)
            except Exception:
                pass
            with ThreadPoolExecutor(max_workers=3) as ex:   # 6→3（2026-10-21）：纯 GIL 活，别和 UI 抢
                futures = {ex.submit(_tags_one_yield, c): c for c in codes}
                for fut in as_completed(futures, timeout=90):
                    code = futures[fut]
                    try:
                        result[code] = fut.result()
                    except Exception:
                        result[code] = {"trend": "flat", "tags": []}
            # 标签算完，再排结构层（它要出网，别在标签期间抢那 2 个 pandas 名额）
            try:
                self._kick_pb_struct_warm(codes)
            except Exception:
                pass
            return result

        def _cache_store(tags):
            with _TAGS_LOCK:
                if len(_TAGS_CACHE) > 60:  # 防指纹 key 无限增长：超阈值清空（重算成本可接受）
                    _TAGS_CACHE.clear()
                _TAGS_CACHE[_cache_key] = {"ts": _time_mod.time(), "tags": tags}

        def _spawn_bg():
            """后台重算一次（全局单飞：_TAGS_RUNNING 保证同时只有一批在算）。"""
            global _TAGS_RUNNING
            if _TAGS_RUNNING:
                return
            _TAGS_RUNNING = True

            def _bg():
                global _TAGS_RUNNING
                try:
                    _cache_store(_compute())
                except Exception:
                    pass
                finally:
                    _TAGS_RUNNING = False
            threading.Thread(target=_bg, daemon=True).start()

        # 2026-10-17 owner：「选股猎手加上一列 30min MA5，与 30 分钟风险体验同源」
        # ⇒ 随标签批算一起下发**仓位结构层**（`_pb_struct_get` 读缓存，主线程零计算零网络）。
        #   同源 = 与持仓 30min 体检 / 风向标**同一个函数、同一份缓存**（`position_structure`）。
        #   未热就**排队后台算**（`_kick_pb_struct_warm`），本批先回 pending，前端轮询补齐。
        _struct = {}
        _struct_pending = False
        try:
            for c in codes:
                _struct[str(c)] = self._pb_struct_get(c)
            _struct_pending = any((v or {}).get("pending") for v in _struct.values())
        except Exception:
            _struct = {}

        def _maybe_kick_struct():
            """⚠️ 2026-10-23：标签批在跑时**先不排**结构层预热。两者共用 `_BG_PANDAS_SEM` 的
            2 个名额，而结构层每只都要出网取 5min/30min（且是在**持有名额**的情况下等网络）
            ⇒ 实测把 50 只板块的标签从 7.5s 拖到 19.5s。
            ⚠️ 必须在 `_spawn_bg()` **之后**调用：`_TAGS_RUNNING` 是 `_spawn_bg` 里置的，
            先置后判才有效（先前放在算 `_struct` 那一坨里 ⇒ 首帧永远是 False ⇒ 照排）。
            """
            if _struct_pending and not _TAGS_RUNNING:
                try:
                    self._kick_pb_struct_warm(codes)
                except Exception:
                    pass

        def _with_struct(payload):
            # 结构层走**独立键** `struct`：若塞进 tags 条目里，前端「标签是否取齐」的判据
            # （`tagsMap[code]` 是否存在）会把"只有结构、没有标签"误判成取齐。
            out = dict(payload or {})
            out["struct"] = {str(c): (_struct.get(str(c)) or {"pending": True}) for c in codes}
            return _clean(out)

        with _TAGS_LOCK:
            cached = _TAGS_CACHE.get(_cache_key)
            if cached and (now - cached["ts"]) < _tags_ttl_now():
                _maybe_kick_struct()
                return _with_struct({"tags": cached["tags"]})
            if cached:
                # 缓存过期 → 后台重算，先返回旧值，界面不阻塞
                _spawn_bg()
                _maybe_kick_struct()
                return _with_struct({"tags": cached["tags"]})
        # 冷启动（无缓存）：2026-10-04 改为后台算 + 立即返回空，不再同步阻塞。
        # 原实现首次进建仓表/破位表会同步跑 40 线程 pandas+网络（实测 23~35s），把 pywebview
        # 主线程冻住。代价：标签首次先空着，前端 10s 轮询，约 20s 后自填。调用方均 `.get(code, {})`
        # 守卫，空 map 安全。
        _spawn_bg()
        _maybe_kick_struct()
        # 冷启动时标签还没算出来，但**结构**是独立缓存 —— 能拿到就先给，前端可先显示 MA5。
        # ⚠️ 2026-10-23：批量缓存是「整批算完才落」，而逐只结果早就在 `_TAGS_ONE_CACHE` 里了
        #   ⇒ 这里先回**已算好的那部分**（增量交付）。否则 50 只的板块要等最慢那只（30~40s）
        #   才一次性出现，owner 报「技术标签更新实在太慢」就是它。
        return _with_struct({"tags": self._tags_partial(codes, offline, date)})

    # ---------- 突破箱体股票聚合 ----------
    # 当日有效突破参数（2026-09-29 owner 口径：昨收 ≤ 上沿 < 今价）
    # 2026-10-25 收紧（依据 doc/research/2026-10-23_箱体突破_第二轮_回踩与站稳.md §3）：
    #   · 幅度 3~5% 档 T+5 超额 −0.65%，0.3~1% 档只有 −0.15% ⇒ 上限 8% → 3%
    #   · 当日涨幅 >7% 档 −0.58%（最差）⇒ 显式剔除
    #   · 量比 >2.5 档 −0.47%、缩量档 +0.05%（唯一不亏）⇒ 不打"放量确认"，改成缩量加分
    _BK_MIN_PCT = 0.3
    _BK_MAX_PCT = 3.0
    _BK_MAX_CHG = 0.07
    _BK_MIN_BARS = 30      # _detect_boxes 在 <30 根时静默返回 []，必须显式挡在前面
    _BK_SCAN_BARS = 200    # 取数窗口：_detect_boxes 只用 tail(150)，200 根足够且能开大 batch
    # 2026-10-08：900 → 200。900 只×200 根 ≈ 18 万行，GM SDK 里 pandas 由 list-of-dicts 拼表 +
    # 逐码 groupby 会**长时间持 GIL**，实测把 pywebview 主线程饿死（freeze 看门狗抓到主线程卡在
    # evaluate_js、后台 `_scan_breakout→daily_many`）。降到 200 每次只 ~4 万行、GIL 持有缩 ~4.5×，
    # 批间再显式让出，界面不再冻（代价：GM 调用次数变多、整轮慢一些，但在后台线程）。
    _BK_BATCH = 200
    # 批量取数整批空时的等待（疑 GM 冷却窗/瞬时不可达）；覆盖为 0 可让测试不真等
    _GM_BATCH_RETRY_WAIT = 62

    def _breakout_pool_codes(self):
        """扫描池 = watchlist_jiuyan.json 的**全部** 6 位码。

        2026-09-29 改：去掉 `_jiuyan_concepts(i).strip()` 过滤。原过滤把 5041 只砍到 **1258 只**
        （多数条目没有概念字段），与 owner「扫该文件里出现的所有股票」的要求不符。
        """
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        return [c for c, i in jy.items() if isinstance(i, dict) and c.isdigit()]

    _BK_CACHE_DIR = BASE / "t_io" / "cache" / "daily_kline"

    def _bk_daily_many(self, chunk, budget_s=25):
        """带硬超时的 daily_many（2026-10-25）。

        GM 未连接时 facade 会「报错 → 冷却 60s → 逐只走腾讯兜底」，一批 200 只要好几分钟，
        整轮扫描看着就是卡死。这里给单批一个预算，超时直接放弃 GM（后续批走本地缓存）。
        """
        from core.market_data.facade import get_provider
        box = {}

        def _run():
            try:
                box["v"] = get_provider().daily_many(chunk, days=self._BK_SCAN_BARS)
            except Exception as e:
                box["e"] = e

        th = threading.Thread(target=_run, daemon=True, name="bk-daily-many")
        th.start()
        th.join(budget_s)
        if th.is_alive():
            print(f"[WARN] breakout daily_many 超时({budget_s}s, {len(chunk)}只) ⇒ 放弃 GM，改用本地缓存",
                  flush=True)
            return {}
        if "e" in box:
            print(f"[WARN] breakout daily_many 失败({len(chunk)}只): "
                  f"{type(box['e']).__name__}: {str(box['e'])[:80]}", flush=True)
            return {}
        return box.get("v") or {}

    def _bk_cache_covers(self, codes, target, sample=80):
        """本地日线缓存对目标交易日的覆盖率（抽样 ≤sample 只）。

        2026-10-25：owner 报「突破箱体经常跑不出结果」，实测卡在 `daily_many`（GM 不可达时
        整批阻塞数分钟）。**缓存能覆盖就不该等 GM** —— 猎手每天本来就在更新这份缓存。
        覆盖率 ≥80% 直接走缓存（快、可离线、结果稳定）；低于则仍以 GM 为主、缓存兜底。
        """
        picked = codes[:sample] if len(codes) > sample else codes
        if not picked:
            return 0.0
        hit = 0
        for c in picked:
            p = self._BK_CACHE_DIR / f"{c}.json"
            try:
                rows = (_load_json(p, {}) or {}).get("rows") or []
                if rows and str(rows[-1].get("date"))[:10] >= target:
                    hit += 1
            except Exception:
                pass
        return hit / len(picked)

    def _bk_cache_last(self, code):
        """只读末根日期（不建 DataFrame）—— 全池扫描时绝大多数票在"stale"这一步就被筛掉。"""
        p = self._BK_CACHE_DIR / f"{code}.json"
        try:
            rows = (_load_json(p, {}) or {}).get("rows") or []
        except Exception:
            return None
        return str(rows[-1].get("date"))[:10] if rows else None

    def _bk_cached_df(self, code, days):
        """本地日线缓存兜底（2026-10-25）。

        GM 批量取数不可用时（owner 报「经常跑不出结果」），过去整批记 no_data ⇒ 全池 0 命中。
        猎手本身每天在读这份缓存，所以直接拿它当兜底：字段与 daily_many 同形
        （date/open/high/low/close/volume），`_detect_boxes` 与判定口径不用改。
        """
        p = self._BK_CACHE_DIR / f"{code}.json"
        try:
            rows = (_load_json(p, {}) or {}).get("rows") or []
        except Exception:
            return None
        if len(rows) < self._BK_MIN_BARS + 1:
            return None
        try:
            import pandas as pd
            df = pd.DataFrame(rows[-days:])
            need = {"date", "open", "high", "low", "close", "volume"}
            if not need.issubset(df.columns):
                return None
            df["date"] = df["date"].astype(str)
            return df
        except Exception:
            return None

    def _box_top_for(self, hist):
        """as-of 上一交易日的箱体上沿：取**含昨收**那只箱体（rel==0）的 high。

        `_detect_boxes` 在 `low <= last_close <= high` 时置 rel=0；此处 last_close 即昨收，
        故 rel==0 的箱体正是"昨收还在里面"的那只 —— 它的上沿就是今日要突破的线。
        必须用**排除当日**的切片：带上当日跳空的话上沿会被抬高，今天反而判不出突破。
        """
        if hist is None or len(hist) < self._BK_MIN_BARS:
            return None
        for b in self._detect_boxes(hist):
            if b.get("rel") == 0 and b.get("high"):
                return float(b["high"])
        return None

    def _breakout_probe_one(self, code, df, date=None):
        """单只「当日有效突破」判定，命中返回 dict，否则 None。

        口径（owner 2026-09-29）：上沿取自 as-of 上一交易日的箱体，命中条件
        `昨收 ≤ 上沿 < 今价` 且幅度 ∈ [0.3%, 8%]。

        两道**必须**的闸：
          - **交易日闸**：`df` 末根必须就是今日。`daily_many` 只在交易日 09:15-23:59 且快照
            ts_date=当日 时才补 forming bar，故周末/盘前末根=上一交易日 → 此处返回 None。
            没有这道闸，周六会把**周五**的突破当成"今天"报出去。
          - **长度闸**：切片后需 ≥ `_BK_MIN_BARS` 根（恰好 30 根全量的票切完只剩 29，
            `_detect_boxes` 会静默返回 [] 而无声漏掉）。
        """
        if df is None or df.empty:
            return None
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        # 2026-10-09：支持**任选日期**扫描——先按目标日 as-of 切片（去掉目标日之后的 bar，
        # 否则历史日扫描会用到"今天"的末根 = 前视），再要求切片末根**恰好等于目标日**。
        df = df[df["date"].astype(str) <= today]
        if df.empty or str(df["date"].iloc[-1]) != today:
            return None
        if len(df) < self._BK_MIN_BARS + 1:
            return None
        box_top = self._box_top_for(df.iloc[:-1])
        if not box_top:
            return None
        prev_close = float(df["close"].iloc[-2])
        cur = float(df["close"].iloc[-1])
        # 对 rel==0 的箱体 `low ≤ 昨收 ≤ high` 恒成立 ⇒ `prev_close <= box_top` 恒真，
        # 真正起作用的是"今价站上上沿"。两半都留着以对齐 owner 的表述口径。
        if not (prev_close <= box_top < cur):
            return None
        pct = (cur - box_top) / box_top * 100
        if not (self._BK_MIN_PCT <= pct <= self._BK_MAX_PCT):
            return None
        # 当日涨幅闸（2026-10-25）：>7% 档 T+5 超额 −0.58%，是分档里最差的一组 ⇒ 直接剔除
        chg = (cur / prev_close - 1.0) if prev_close > 0 else 0.0
        if chg > self._BK_MAX_CHG:
            return None
        # 量比（今日量 / 过去 20 日均量）：研究里放量是反指、缩量档唯一不亏 ⇒ 只做展示与排序，不做放量确认
        vol_ratio = None
        try:
            vol = [float(x) for x in df["volume"].tolist()]
            if len(vol) >= 21:
                win = vol[-21:]          # 当日 + 前 20 日
                # 单位一致性闸（2026-10-25）：日线缓存在 10-22 做过一次成交量单位迁移，
                # 前后段可能一手一股混在一起（实测 600887 相邻日差 27 倍）⇒ 这种票的量比不报，
                # 否则会把"换了单位"误判成"缩量"，反而给质量分加分。
                _ok = all(a > 0 and b > 0 and 0.1 <= b / a <= 10.0
                          for a, b in zip(win, win[1:]))
                if _ok:
                    base = sum(win[:-1]) / 20.0
                    if base > 0:
                        vol_ratio = round(win[-1] / base, 2)
        except Exception:
            vol_ratio = None
        q = 0
        if vol_ratio is not None and vol_ratio < 1.0:
            q += 2
        if pct <= 1.5:
            q += 2
        if chg <= 0.03:
            q += 1
        return {"price": round(cur, 3), "prev_close": round(prev_close, 3),
                "box_top": round(box_top, 3), "pct_above": round(pct, 2),
                "day_chg": round(chg * 100, 2), "vol_ratio": vol_ratio, "quality": q}

    def _breakout_disk_path(self, today):
        return BASE / "t_io" / "cache" / f"breakout_{today}.json"

    def _scan_breakout(self, codes, state, date=None):
        """全池扫描「当日有效突破」。state 非空时更新进度（done/total/found/stocks/no_data）。

        2026-09-29 重写要点：
          - 取数改 `facade.daily_many`（GM `history` 列表式，350 只/批 + 批量 forming bar），
            取代逐只 `load_stock_tags_batch` —— 后者走 GM **单线程串行** + 90s 批超时**静默丢票**，
            全池 5000+ 只不可行。
          - 判定改「当日有效突破」，取代旧 `向上突破`（旧标签只看"当前价在箱体上沿之上"，
            没有任何当日成分 ⇒ 三周前突破的票每天照旧被扫出来）。
          - 北交所：`daily_many` 分流到 bj_daily，取不到的码**不出现在返回里** ⇒ 此处计入 no_data
            如实汇报，而不是静默当成"没突破"。
        """
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        from core.market_data.facade import get_provider
        from core.market_data.codec import market_of
        breakouts = []
        total = len(codes)
        dt_target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        rest_total = sum(1 for c in codes if market_of(c) != "BJ")
        # no_data 拆分成因（2026-10-09）：整池取数失败（GM 不可达）≠ 北交所取不到 ≠ 无当日 bar。
        no_data = rest_missing = bj_missing = stale = ok = cached_used = 0
        BATCH = self._BK_BATCH
        _waited = False
        # 缓存能覆盖目标日 ⇒ 直接走缓存，不碰 GM（GM 不可达时会整批阻塞好几分钟）
        _cov = self._bk_cache_covers(codes, dt_target)
        _gm_off = _cov >= 0.8
        if _gm_off:
            print(f"[INFO] breakout: 本地日线缓存覆盖 {_cov:.0%}，本轮只用缓存", flush=True)
        for i in range(0, total, BATCH):
            chunk = codes[i:i + BATCH]
            if _gm_off:
                frames = {}
            else:
                frames = self._bk_daily_many(chunk)
            if not frames and chunk and not _waited:
                # 整批空 ⇒ 疑 GM 冷却窗/瞬时不可达（facade 60s 冷却内后续批全跳过）→ 等窗后重试一次
                # （只等一次，避免 GM 真下线时逐批 62s 打转；仍空则由 fetch_failed 如实汇报）
                _time_mod.sleep(self._GM_BATCH_RETRY_WAIT)
                _waited = True
                try:
                    frames = get_provider().daily_many(chunk, days=self._BK_SCAN_BARS)
                except Exception:
                    frames = {}
            _from_gm = 0
            for code in chunk:
                fr = frames.get(code)
                if fr is not None and not getattr(fr, "empty", True):
                    _from_gm += 1
                if fr is None or getattr(fr, "empty", True):
                    # 2026-10-25：GM 不可用时的兜底——读本地日线缓存（猎手每天本来就在读它）。
                    # 先只读末根日期，末根早于目标日 ⇒ 直接算 stale，不建 DataFrame（全池扫描的主要开销）。
                    _cl = self._bk_cache_last(code)
                    if _cl is not None and _cl == dt_target:
                        fr = self._bk_cached_df(code, self._BK_SCAN_BARS)
                        if fr is not None:
                            cached_used += 1
                    elif _cl is not None and _cl > dt_target:
                        fr = self._bk_cached_df(code, self._BK_SCAN_BARS)
                        if fr is not None:
                            cached_used += 1
                    elif _cl is not None:
                        stale += 1
                        continue
                if fr is None or getattr(fr, "empty", True):
                    no_data += 1
                    if market_of(code) == "BJ":
                        bj_missing += 1
                    else:
                        rest_missing += 1
                    continue
                # 有帧但**无当日 bar** → 未就绪/非交易日/停牌（区别于"取数失败"）
                # 2026-10-25 快路径：末根就是目标日时不用再切片（全池扫时这一步占大头）
                _dates = fr["date"].astype(str)
                _last_d = str(_dates.iloc[-1])
                if _last_d == dt_target:
                    _sl = fr
                elif _last_d < dt_target:
                    stale += 1
                    continue
                else:
                    _sl = fr[_dates <= dt_target]
                    if _sl.empty or str(_sl["date"].iloc[-1]) != dt_target:
                        stale += 1
                        continue
                ok += 1
                hit = self._breakout_probe_one(code, fr, date)
                if not hit:
                    continue
                info = jy.get(code) if isinstance(jy.get(code), dict) else None
                nm = (info or {}).get("name", code)
                concepts = _stock_concepts(info)
                csrc = "offline" if concepts else "none"
                if not concepts:
                    # 离线两源为空：东财磁盘缓存层补拉（仅命中票，~30只/天，fail-open 不阻塞扫描）
                    concepts = _stock_concepts(
                        info, em_boards=_em_boards_disk_cached(self, code))
                    if concepts:
                        csrc = "em"
                breakouts.append({"code": code, "name": nm,
                                  "industry": _stock_industry(info),
                                  "concepts": concepts,
                                  "concepts_source": csrc,
                                  "concept_descs": _jiuyan_concept_descs(info, concepts),
                                  "tags": [{"label": "当日有效突破", "color": "up"}], **hit})
            if state is not None:
                with _BREAKOUT_LOCK:
                    state.update({"done": min(i + BATCH, total), "found": len(breakouts),
                                  "stocks": list(breakouts), "no_data": no_data,
                                  "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                                  "stale": stale, "ok": ok})
            if i == 0 and _from_gm == 0 and cached_used > 0:
                _gm_off = True   # 首批 GM 全空但缓存顶上了 ⇒ 后面直接走缓存，不再逐批等 GM
                print("[INFO] breakout: GM 批量不可用，本轮改用本地日线缓存", flush=True)
            _time_mod.sleep(0.05)   # 2026-10-08：批间显式让出，避免连续持 GIL 把主线程饿死
        # 2026-10-25：按质量分排序（缩量 + 小幅突破 + 当日涨幅温和），
        # **不再按"超出上沿越多越前"**——研究显示幅度 3~5% 档最差，原排序正好把差的排前面。
        breakouts.sort(key=lambda x: (-(x.get("quality") or 0),
                                      (x.get("vol_ratio") if x.get("vol_ratio") is not None else 9.9),
                                      x.get("pct_above") or 0))
        # 整池不可用判据（任一成立即视为"取数失败，结果不可信"）：
        #   ① 无一只带当日 bar（ok==0）—— GM 整池不可达 / 非交易日 / 行情未就绪；
        #   ② 非北交所码大面积无帧（≥50%）—— GM `daily_batch` 不可达（批量路径无腾讯兜底）。
        fetch_failed = total > 0 and (ok == 0
                                      or (rest_total > 0 and rest_missing >= 0.5 * rest_total))
        if state is not None:
            with _BREAKOUT_LOCK:
                state.update({"done": total, "found": len(breakouts),
                              "stocks": list(breakouts), "no_data": no_data,
                              "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                              "stale": stale, "ok": ok, "cached": cached_used,
                              "fetch_failed": bool(fetch_failed)})
        return breakouts

    def _bk_cache_get(self, key):
        with _BREAKOUT_LOCK:
            return (getattr(self, "_breakout_cache", None) or {}).get(key)

    def _bk_cache_put(self, key, val):
        with _BREAKOUT_LOCK:
            if not hasattr(self, "_breakout_cache"):
                self._breakout_cache = {}
            self._breakout_cache[key] = val

    def load_breakout_stocks(self, date=None):
        """同步全量扫描突破箱体（前端走后端后台线程时用 start_breakout_scan）。
        `date` 缺省=今日；可传任意交易日做历史扫描。结果缓存到内存+磁盘（按日），避免重复扫描。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        hit = self._bk_cache_get(cache_key)
        if hit is not None:
            return hit
        disk_fp = self._breakout_disk_path(today)
        if disk_fp.exists():
            disk = _load_json(disk_fp, None)
            if disk and isinstance(disk, dict) and "stocks" in disk:
                self._bk_cache_put(cache_key, disk)
                return disk

        codes = self._breakout_pool_codes()
        if not codes:
            return {"stocks": [], "count": 0}
        st = {}
        breakouts = self._scan_breakout(codes, st, today)
        result = _clean({"date": today, "stocks": breakouts, "count": len(breakouts),
                         "no_data": st.get("no_data", 0),
                         "rest_no_data": st.get("rest_no_data", 0),
                         "bj_no_data": st.get("bj_no_data", 0),
                         "stale": st.get("stale", 0), "ok": st.get("ok", 0),
                         "fetch_failed": bool(st.get("fetch_failed", False))})
        if not result.get("fetch_failed"):
            self._bk_cache_put(cache_key, result)
            try:
                disk_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return result

    def start_breakout_scan(self, force=False, date=None):
        """启动后台突破扫描（幂等：内存/磁盘缓存命中→立即 done；扫描中→返回当前进度）。
        `date` 缺省=今日；可传任意交易日做历史扫描（结果按日缓存到 `breakout_{date}.json`）。
        force=True 绕开该日缓存强制重扫（前端「🔄 重新扫描」）。
        返回 {status: idle|running|done|error, date, total, done, found, stocks, no_data?, error?}。

        2026-09-29：状态与缓存全部改由 `_BREAKOUT_LOCK` 保护；「扫描中」判定与
        `self._breakout_scan` 赋值在同一临界区内完成，防止两个 force 重扫并发跑同一池。
        """
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        if not force:
            hit = self._bk_cache_get(cache_key)
            if hit is not None:
                return {"status": "done", "date": today, "total": 0, "done": 0,
                        "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                        "no_data": hit.get("no_data", 0),
                        "rest_no_data": hit.get("rest_no_data", 0),
                        "bj_no_data": hit.get("bj_no_data", 0),
                        "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                        "fetch_failed": bool(hit.get("fetch_failed", False))}
            disk_fp = self._breakout_disk_path(today)
            if disk_fp.exists():
                disk = _load_json(disk_fp, None)
                if disk and isinstance(disk, dict) and "stocks" in disk:
                    self._bk_cache_put(cache_key, disk)
                    return {"status": "done", "date": today, "total": 0, "done": 0,
                            "found": disk.get("count", 0), "stocks": disk.get("stocks", []),
                            "no_data": disk.get("no_data", 0),
                            "rest_no_data": disk.get("rest_no_data", 0),
                            "bj_no_data": disk.get("bj_no_data", 0),
                            "stale": disk.get("stale", 0), "ok": disk.get("ok", 0),
                            "fetch_failed": bool(disk.get("fetch_failed", False))}

        with _BREAKOUT_LOCK:
            cur = getattr(self, "_breakout_scan", None)
            if cur and cur.get("status") == "running":
                return dict(cur)
            codes = self._breakout_pool_codes()
            if not codes:
                return {"status": "done", "total": 0, "done": 0, "found": 0, "stocks": []}
            state = {"status": "running", "date": today, "total": len(codes), "done": 0, "found": 0,
                     "stocks": [], "no_data": 0}
            self._breakout_scan = state
            if force:
                # 强制重扫必须**先失效**旧缓存：`get_breakout_scan` 优先返回缓存，
                # 否则前端 poll 第一次就拿到上一轮结果、判为 done 并停止轮询 ⇒
                # 用户看到的是旧数据，"重新扫描"形同无效。
                # （状态已在上方置 running，故此刻起 poll 会读到 running 而非 idle。）
                if hasattr(self, "_breakout_cache"):
                    self._breakout_cache.pop(cache_key, None)
                try:
                    self._breakout_disk_path(today).unlink(missing_ok=True)
                except Exception:
                    pass

        def run():
            try:
                breakouts = self._scan_breakout(codes, state, today)
                result = _clean({"date": today, "stocks": breakouts, "count": len(breakouts),
                                 "no_data": state.get("no_data", 0),
                                 "rest_no_data": state.get("rest_no_data", 0),
                                 "bj_no_data": state.get("bj_no_data", 0),
                                 "stale": state.get("stale", 0), "ok": state.get("ok", 0),
                                 "fetch_failed": bool(state.get("fetch_failed", False))})
                # 取数失败（GM 整池不可达 / 非交易日 / 行情未就绪）→ **不落盘**，
                # 避免把"假 0"缓存成当日定论（2026-10-09 事故：整池 no_data 被当成真 0）。
                if not result.get("fetch_failed"):
                    self._bk_cache_put(cache_key, result)
                    try:
                        self._breakout_disk_path(today).write_text(
                            json.dumps(result, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                with _BREAKOUT_LOCK:
                    state.update({"status": "done", "done": len(codes),
                                  "found": len(breakouts), "stocks": breakouts})
            except Exception as e:
                with _BREAKOUT_LOCK:
                    state.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        return dict(state)

    def get_breakout_scan(self, date=None):
        """轮询后台突破扫描进度。done 后返回完整结果（含磁盘/内存缓存命中）。
        `date` 缺省=今日；扫描中且日期匹配才返回 running，否则 idle。

        返回的是**快照副本**（`dict(state)`），避免 800ms 读线程读到
        「status=done 但 stocks 仍是上一批」的中间态。
        """
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        hit = self._bk_cache_get(cache_key)
        if hit is not None:
            return {"status": "done", "date": today, "total": 0, "done": 0,
                    "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                    "no_data": hit.get("no_data", 0),
                    "rest_no_data": hit.get("rest_no_data", 0),
                    "bj_no_data": hit.get("bj_no_data", 0),
                    "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                    "fetch_failed": bool(hit.get("fetch_failed", False))}
        with _BREAKOUT_LOCK:
            cur = getattr(self, "_breakout_scan", None)
            if cur and cur.get("date") == today:
                return dict(cur)
        return {"status": "idle", "date": today, "total": 0, "done": 0, "found": 0, "stocks": []}

    # ---------- 全市场「刚刚站上5日线」扫描（2026-10-09） ----------
    # 口径复用 core/position_builder.check_ma_break 的 reclaim5（只做「隔夜回站」）：
    #   basis=截至昨日的收盘；prev_MA5=mean(basis[-5:])；cur_MA5=(sum(basis[-4:])+price)/5；
    #   reclaim5 = prev_close < prev_MA5 and price > cur_MA5。
    # 取数走 daily_many 批量帧（当日 forming bar 的 close=最新价），零逐只调用；池沿用突破扫描的全市场池。
    _RC_BARS = 30
    _RC_BATCH = 200

    def _reclaim_probe_one(self, code, df, date=None):
        """单只「刚刚站上5日线」判定（口径单一源 core/ma_reclaim.reclaim5_frame），命中返回 dict 否则 None。"""
        from core.ma_reclaim import reclaim5_frame
        target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        return reclaim5_frame(df, target)

    def _reclaim_disk_path(self, today):
        return BASE / "t_io" / "cache" / f"reclaim_{today}.json"

    def _rc_cache_get(self, key):
        with _BREAKOUT_LOCK:
            return (getattr(self, "_reclaim_cache", None) or {}).get(key)

    def _rc_cache_put(self, key, val):
        with _BREAKOUT_LOCK:
            if not hasattr(self, "_reclaim_cache"):
                self._reclaim_cache = {}
            self._reclaim_cache[key] = val

    def _scan_reclaim(self, codes, state, date=None):
        """全市场扫描「刚刚站上5日线」。state 非空时更新进度（分类同突破扫描）。"""
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        from core.market_data.facade import get_provider
        from core.market_data.codec import market_of
        hits = []
        total = len(codes)
        dt_target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        rest_total = sum(1 for c in codes if market_of(c) != "BJ")
        no_data = rest_missing = bj_missing = stale = ok = cached_used = 0
        BATCH = self._RC_BATCH
        _waited = False
        for i in range(0, total, BATCH):
            chunk = codes[i:i + BATCH]
            try:
                frames = get_provider().daily_many(chunk, days=self._RC_BARS)
            except Exception as e:      # 兜底：单批异常绝不终止整轮
                print(f"[WARN] reclaim daily_many 失败({len(chunk)}只): "
                      f"{type(e).__name__}: {str(e)[:80]}", flush=True)
                frames = {}
            if not frames and chunk and not _waited:
                _time_mod.sleep(self._GM_BATCH_RETRY_WAIT)
                _waited = True
                try:
                    frames = get_provider().daily_many(chunk, days=self._RC_BARS)
                except Exception:
                    frames = {}
            for code in chunk:
                fr = frames.get(code)
                if fr is None or getattr(fr, "empty", True):
                    no_data += 1
                    if market_of(code) == "BJ":
                        bj_missing += 1
                    else:
                        rest_missing += 1
                    continue
                _sl = fr[fr["date"].astype(str) <= dt_target]
                if _sl.empty or str(_sl["date"].iloc[-1]) != dt_target:
                    stale += 1
                    continue
                ok += 1
                hit = self._reclaim_probe_one(code, fr, date)
                if not hit:
                    continue
                info = jy.get(code) if isinstance(jy.get(code), dict) else None
                nm = (info or {}).get("name", code)
                concepts = _stock_concepts(info)
                csrc = "offline" if concepts else "none"
                if not concepts:
                    concepts = _stock_concepts(
                        info, em_boards=_em_boards_disk_cached(self, code))
                    if concepts:
                        csrc = "em"
                hits.append({"code": code, "name": nm,
                             "industry": _stock_industry(info),
                             "concepts": concepts, "concepts_source": csrc,
                             "concept_descs": _jiuyan_concept_descs(info, concepts),
                             "tags": [{"label": "站上5日线", "color": "up"}], **hit})
            if state is not None:
                with _BREAKOUT_LOCK:
                    state.update({"done": min(i + BATCH, total), "found": len(hits),
                                  "stocks": list(hits), "no_data": no_data,
                                  "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                                  "stale": stale, "ok": ok})
            _time_mod.sleep(0.05)
        hits.sort(key=lambda x: -(x.get("dev5_pct") if x.get("dev5_pct") is not None else -1e9))
        fetch_failed = total > 0 and (ok == 0
                                      or (rest_total > 0 and rest_missing >= 0.5 * rest_total))
        if state is not None:
            with _BREAKOUT_LOCK:
                state.update({"done": total, "found": len(hits), "stocks": list(hits),
                              "no_data": no_data, "rest_no_data": rest_missing,
                              "bj_no_data": bj_missing, "stale": stale, "ok": ok,
                              "fetch_failed": bool(fetch_failed)})
        return hits

    def load_reclaim_stocks(self, date=None):
        """同步全市场扫描「刚刚站上5日线」。结果缓存内存+磁盘（按日）；取数失败不落盘。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "reclaim_" + today
        hit = self._rc_cache_get(cache_key)
        if hit is not None:
            return hit
        disk_fp = self._reclaim_disk_path(today)
        if disk_fp.exists():
            disk = _load_json(disk_fp, None)
            if disk and isinstance(disk, dict) and "stocks" in disk:
                self._rc_cache_put(cache_key, disk)
                return disk
        codes = self._breakout_pool_codes()
        if not codes:
            return {"stocks": [], "count": 0}
        st = {}
        hits = self._scan_reclaim(codes, st, today)
        result = _clean({"date": today, "stocks": hits, "count": len(hits),
                         "no_data": st.get("no_data", 0),
                         "rest_no_data": st.get("rest_no_data", 0),
                         "bj_no_data": st.get("bj_no_data", 0),
                         "stale": st.get("stale", 0), "ok": st.get("ok", 0),
                         "fetch_failed": bool(st.get("fetch_failed", False))})
        if not result.get("fetch_failed"):
            self._rc_cache_put(cache_key, result)
            try:
                disk_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        return result

    def start_reclaim_scan(self, force=False, date=None):
        """启动后台「刚刚站上5日线」全市场扫描（幂等：缓存命中→done；扫描中→进度）。
        force=True 绕开该日缓存强制重扫（前端「🔄 扫描该日」）。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "reclaim_" + today
        if not force:
            hit = self._rc_cache_get(cache_key)
            if hit is not None:
                return {"status": "done", "date": today, "total": 0, "done": 0,
                        "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                        "no_data": hit.get("no_data", 0),
                        "rest_no_data": hit.get("rest_no_data", 0),
                        "bj_no_data": hit.get("bj_no_data", 0),
                        "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                        "fetch_failed": bool(hit.get("fetch_failed", False))}
            disk_fp = self._reclaim_disk_path(today)
            if disk_fp.exists():
                disk = _load_json(disk_fp, None)
                if disk and isinstance(disk, dict) and "stocks" in disk:
                    self._rc_cache_put(cache_key, disk)
                    return {"status": "done", "date": today, "total": 0, "done": 0,
                            "found": disk.get("count", 0), "stocks": disk.get("stocks", []),
                            "no_data": disk.get("no_data", 0),
                            "rest_no_data": disk.get("rest_no_data", 0),
                            "bj_no_data": disk.get("bj_no_data", 0),
                            "stale": disk.get("stale", 0), "ok": disk.get("ok", 0),
                            "fetch_failed": bool(disk.get("fetch_failed", False))}

        with _BREAKOUT_LOCK:
            cur = getattr(self, "_reclaim_scan", None)
            if cur and cur.get("status") == "running":
                return dict(cur)
            codes = self._breakout_pool_codes()
            if not codes:
                return {"status": "done", "total": 0, "done": 0, "found": 0, "stocks": []}
            state = {"status": "running", "date": today, "total": len(codes), "done": 0, "found": 0,
                     "stocks": [], "no_data": 0}
            self._reclaim_scan = state
            if force:
                if hasattr(self, "_reclaim_cache"):
                    self._reclaim_cache.pop(cache_key, None)
                try:
                    self._reclaim_disk_path(today).unlink(missing_ok=True)
                except Exception:
                    pass

        def run():
            try:
                hits = self._scan_reclaim(codes, state, today)
                result = _clean({"date": today, "stocks": hits, "count": len(hits),
                                 "no_data": state.get("no_data", 0),
                                 "rest_no_data": state.get("rest_no_data", 0),
                                 "bj_no_data": state.get("bj_no_data", 0),
                                 "stale": state.get("stale", 0), "ok": state.get("ok", 0),
                                 "fetch_failed": bool(state.get("fetch_failed", False))})
                if not result.get("fetch_failed"):
                    self._rc_cache_put(cache_key, result)
                    try:
                        self._reclaim_disk_path(today).write_text(
                            json.dumps(result, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
                with _BREAKOUT_LOCK:
                    state.update({"status": "done", "done": len(codes),
                                  "found": len(hits), "stocks": hits})
            except Exception as e:
                with _BREAKOUT_LOCK:
                    state.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        return dict(state)

    def get_reclaim_scan(self, date=None):
        """轮询后台「刚刚站上5日线」扫描进度。done 后返回完整结果（含缓存命中）。"""
        today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
        cache_key = "reclaim_" + today
        hit = self._rc_cache_get(cache_key)
        if hit is not None:
            return {"status": "done", "date": today, "total": 0, "done": 0,
                    "found": hit.get("count", 0), "stocks": hit.get("stocks", []),
                    "no_data": hit.get("no_data", 0),
                    "rest_no_data": hit.get("rest_no_data", 0),
                    "bj_no_data": hit.get("bj_no_data", 0),
                    "stale": hit.get("stale", 0), "ok": hit.get("ok", 0),
                    "fetch_failed": bool(hit.get("fetch_failed", False))}
        with _BREAKOUT_LOCK:
            cur = getattr(self, "_reclaim_scan", None)
            if cur and cur.get("date") == today:
                return dict(cur)
        return {"status": "idle", "date": today, "total": 0, "done": 0, "found": 0, "stocks": []}

    # ---------- 选股猎手历史 ----------
    def available_hunter_dates(self):
        """返回可选日期（降序）：heat history 日期 + 最近 120 个工作日，支持扫描任意日期。"""
        dates = set()
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            dates.update(hist.keys())
        except Exception:
            pass
        from datetime import timedelta
        _today = datetime.now().date()
        for _i in range(120):
            _d = _today - timedelta(days=_i)
            if _d.weekday() < 5:
                dates.add(_d.strftime("%Y%m%d"))
        return [{"date": d[:4] + "-" + d[4:6] + "-" + d[6:8]} for d in sorted(dates, reverse=True)]

    def load_sector_ma_breadth(self, days=10, end_date=None):
        """**每个板块每日「站上5日线 / 20日线」的成分股占比**（2026-10-20 owner：
        星空图下方多坐标轴小图）。SWR：主线程零计算，后台扫一次全池日线缓存（≈6~8s）。

        口径见 `_compute_sector_ma_breadth`。两点与上方星轨图的耦合是刻意的：
          ① `end_date` 同样只取 **≤ 该日** 的数据（跟全局日期选择器走）；
          ② 板块顺序 = `daily_summary.json` 里该日的**排名**顺序 ⇒ 网格顺序与星轨图右端
             直标一致，不用来回找板块。
        """
        try:
            n = max(3, min(int(days or 10), 30))
        except Exception:
            n = 10
        end = str(end_date or "").replace("-", "")[:8]
        latest_now = _market_latest_date()
        _end_d = str(end_date or "").strip()[:10]
        # 2026-10-23 owner：「选了 4月8日，站上5/20日线占比没跟着变」。
        # 磁盘/内存档只覆盖**最新**那 40 天；历史日必须**锚在那一天重算**（只进内存）。
        if _end_d and _end_d < latest_now:
            with _SECTOR_MA_LOCK:
                hit = _SECTOR_MA_BY_END.get(_end_d)
            if (hit and int(hit.get("schema") or 0) == _SECTOR_MA_SCHEMA
                    and str(hit.get("board_fp") or "") == _watchlist_fp()):
                return _clean(self._sector_ma_payload(hit, n, end, stale=False))
            self._kick_sector_ma_breadth(_end_d)
            return _clean({"available": False, "warming": True, "end_date": _end_d})
        now = _time_mod.time()
        with _SECTOR_MA_LOCK:
            mem = dict(_SECTOR_MA_MEM)
        rec = mem.get("val") if (now - mem.get("ts", 0.0)) < _SECTOR_MA_MEM_TTL else None
        latest = _market_latest_date()
        if not rec:
            try:
                if _SECTOR_MA_CACHE_FP.exists():
                    rec = json.loads(_SECTOR_MA_CACHE_FP.read_text(encoding="utf-8"))
            except Exception:
                rec = None
        if rec and rec.get("sectors"):
            # 两个失效条件：数据日过期（明天了）**或板块映射变了**（新加/改板块）。
            # 后者 2026-10-21 补：owner 加了大消费/黄金/数据要素后，数据日没变 ⇒ 面板一直
            # 显示旧板块集 ⇒ "占比没有更新"。
            stale = (str(rec.get("latest_date") or "") < latest
                     or str(rec.get("board_fp") or "") != _watchlist_fp()
                     or int(rec.get("schema") or 0) != _SECTOR_MA_SCHEMA)
            if stale:
                self._kick_sector_ma_breadth()
            return _clean(self._sector_ma_payload(rec, n, end, stale=stale))
        self._kick_sector_ma_breadth()
        with _SECTOR_MA_LOCK:
            st = dict(_SECTOR_MA_STATE)
        return _clean({"available": False, "warming": True,
                       "progress": {"done": int(st.get("done") or 0),
                                    "total": int(st.get("total") or 0)}})

    @staticmethod
    def _sector_ma_payload(rec, days, end="", stale=False):
        # 网格顺序 = 该日板块排名（daily_summary 与星轨图同一份；不重复维护第二套排序）
        rank = {}
        try:
            _d = json.loads((BASE / "stock_hunter" / "history" / "daily_summary.json")
                            .read_text(encoding="utf-8"))
            _ks = sorted(k for k, v in _d.items()
                         if isinstance(v, list) and v and (not end or k <= end))
            if _ks:
                rank = {str(r.get("板块") or "").strip(): int(r.get("排名") or 9999)
                        for r in _d[_ks[-1]]}
        except Exception:
            rank = {}
        secs = []
        for s in (rec.get("sectors") or []):
            pts = [p for p in (s.get("points") or [])
                   if not end or str(p.get("date") or "").replace("-", "") <= end]
            pts = pts[-days:]
            if not pts:
                continue
            secs.append({"name": s.get("name"), "points": pts,
                         "last5": pts[-1].get("pct5"), "last20": pts[-1].get("pct20"),
                         "last_conv": pts[-1].get("pct_conv"),      # 2026-10-23 标题里也显示具体数值
                         "n": pts[-1].get("n")})
        secs.sort(key=lambda x: (rank.get(str(x.get("name") or ""), 9999),
                                 str(x.get("name") or "")))
        return {"available": True, "stale": bool(stale), "days": days,
                "latest_date": rec.get("latest_date"), "computed_at": rec.get("computed_at"),
                "sectors": secs, "coverage": rec.get("coverage") or {}}

    def _kick_sector_ma_breadth(self, end=None):
        """后台算一次板块均线占比（单飞）。仅后台线程跑；失败静默。

        `end` 给定 = **历史档**：窗口锚在那一天，结果只进内存（`_SECTOR_MA_BY_END`）。
        """
        if end:
            with _SECTOR_MA_LOCK:
                if end in _SECTOR_MA_END_RUNNING:
                    return
                _SECTOR_MA_END_RUNNING.add(end)

            def _bg_hist():
                try:
                    val = _compute_sector_ma_breadth(_SECTOR_MA_MAX_DAYS, end=end)
                    with _SECTOR_MA_LOCK:
                        _SECTOR_MA_BY_END[end] = val
                        while len(_SECTOR_MA_BY_END) > _SECTOR_MA_END_MAX:
                            _SECTOR_MA_BY_END.pop(next(iter(_SECTOR_MA_BY_END)))
                except Exception:
                    pass
                finally:
                    with _SECTOR_MA_LOCK:
                        _SECTOR_MA_END_RUNNING.discard(end)
            _th.Thread(target=_bg_hist, daemon=True, name="sector-ma-hist").start()
            return
        with _SECTOR_MA_LOCK:
            if _SECTOR_MA_STATE.get("running"):
                return
            _SECTOR_MA_STATE.update({"running": True, "done": 0, "total": 0,
                                     "started": _time_mod.time()})

        def _bg():
            try:
                val = _compute_sector_ma_breadth(_SECTOR_MA_MAX_DAYS)
                with _SECTOR_MA_LOCK:
                    _SECTOR_MA_MEM.update({"ts": _time_mod.time(), "val": val})
            except Exception:
                pass
            finally:
                with _SECTOR_MA_LOCK:
                    _SECTOR_MA_STATE["running"] = False
        _th.Thread(target=_bg, daemon=True, name="sector-ma-breadth").start()

    def load_sector_constellation(self, days=5, end_date=None):
        """近 N 个交易日的板块「星轨」：位置 = 当日排名，星的大小 = 平均分，轨迹 = 排名变化。

        数据源 `stock_hunter/history/daily_summary.json`（猎手每次运行后按日重写，一日一档）。
        `end_date` 给定时只取 **≤ 该日** 的最近 N 个交易日（跟全局日期选择器走）。

        ⚠️ 两条必须随图一起展示的口径（都是真的会误导人的点）：
          ① **排名只在当日集合内可比** —— 板块数 23(10-15) → 28(10-16)，新题材一进来，
             老板块的排名会被整体下压，这不是"变弱"。
          ② **平均分不能跨"入池日"直接比** —— 低空经济/冰雪经济/大消费等是从加入那天起
             才有分数，且成分股与老板块不是同一批。10-16/10-17 的 10~14 分**不是**因为
             市场变好，而是这几只新板块的样本极小（如大消费 3 只、化工 2 只）。
        """
        try:
            n = max(1, min(int(days or 5), 30))
        except Exception:
            n = 5
        fp = BASE / "stock_hunter" / "history" / "daily_summary.json"
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as e:                                  # noqa: BLE001
            return {"available": False, "error": f"读 daily_summary.json 失败: {e}",
                    "dates": [], "sectors": [], "max_rank": 0, "note": ""}
        end = str(end_date or "").replace("-", "")
        keys_all = sorted(k for k, v in d.items()
                          if isinstance(v, list) and v and (not end or k <= end))
        # 2026-10-17：**只保留真正的交易日**。猎手在周末/法定假日也会被手动跑
        # （10-10 周六、10-04 周日、10-06/10-07 国庆休市…），而这些快照的行情就是
        # **上一交易日的收盘** ⇒ 画进图里等于一段假平线、白占一个横向格子。
        # 日历以指数日线缓存为准（`index_sh000001.json` 的真实 bar 日期），
        # 读不到才退回"工作日"近似（会把假日当交易日，属可接受的降级）。
        _trading = None
        try:
            _idx = json.loads((BASE / "t_io" / "cache" / "daily_kline" / "index_sh000001.json")
                              .read_text(encoding="utf-8")).get("rows", [])
            _trading = {str(r.get("date"))[:10].replace("-", "") for r in _idx if r.get("date")}
        except Exception:
            _trading = None
        if _trading:
            keys = [k for k in keys_all if k in _trading]
        else:
            def _weekday(k):
                try:
                    return datetime.strptime(k, "%Y%m%d").weekday() < 5
                except Exception:
                    return True
            keys = [k for k in keys_all if _weekday(k)]
        keys = keys[-n:]
        if not keys:
            return {"available": False, "error": "该日期之前没有板块排名记录",
                    "dates": [], "sectors": [], "max_rank": 0, "note": ""}
        per = {}
        for k in keys:
            for r in d[k]:
                nm = str(r.get("板块") or "").strip()
                if not nm:
                    continue
                try:
                    per.setdefault(nm, {})[k] = {
                        "rank": int(r.get("排名") or 0),
                        "score": float(r.get("平均分") or 0.0),
                        "lu": int(r.get("涨停数") or 0),
                        "top": str(r.get("最强细分") or ""),
                        "n": int(r.get("股票数") or 0),
                    }
                except Exception:
                    continue
        # 「排名持续提升」口径（2026-10-17 owner 要按钮筛出来）：
        #   ① 窗口内**至少过半的天数**名次变好（`_SUSTAIN_UPS_MIN`）——排除"靠某一天暴涨"；
        #   ② **首尾净提升 ≥ 5 名**（`_SUSTAIN_NET_MIN`）——排除天天小步上挪但最终没上去的。
        # 实测（09-28~10-16，25 个板块）：没有任何板块做到**每天都**变好（最长的连续严格提升只有 3 天），
        # 所以不能按"严格单调"定义，否则永远筛不出东西。
        sectors = []
        for nm, pts in per.items():
            seq = [dict(pts[k], ymd=k) for k in keys if k in pts]
            if not seq:
                continue
            _ups = sum(1 for i in range(1, len(seq)) if seq[i]["rank"] < seq[i - 1]["rank"])
            _net = (seq[0]["rank"] - seq[-1]["rank"]) if len(seq) >= 2 else None
            sectors.append({
                "name": nm,
                "points": seq,
                "last_rank": seq[-1]["rank"],
                "first_rank": seq[0]["rank"],
                # >0 = 排名前进。只上榜 1 天的板块没有"变化"可言 ⇒ None（前端显示「—」），
                # 别把它当 0 混进"变动最大"的排序里。
                "delta": _net,
                "ups": _ups,                       # 窗口内名次变好的天数
                "sustained": bool(_net is not None and _net >= _SUSTAIN_NET_MIN
                                  and _ups >= _SUSTAIN_UPS_MIN),
                "max_score": max(p["score"] for p in seq),
                "days": len(seq),
            })
        # 有最新一日成绩的排前面（最后一天没上榜的排后面），再按最新排名升序
        sectors.sort(key=lambda s: (s["points"][-1]["ymd"] != keys[-1], s["last_rank"], s["name"]))
        return _clean({
            "available": True,
            "dates": [{"ymd": k, "label": f"{k[4:6]}-{k[6:]}"} for k in keys],
            "max_rank": max((p["rank"] for s in sectors for p in s["points"]), default=0),
            "max_score": max((s["max_score"] for s in sectors), default=0.0),
            "sectors": sectors,
            "note": f"{len(keys)} 个交易日（周末/法定休市日的猎手快照不画 —— 那些天的行情"
                    "就是上一交易日收盘，画进去只是一段假平线）。"
                    "全部用当前池子重算（含 D12 趋势性），不是各日当天的池子 ⇒ 分数可跨日比较，"
                    "但与当日实盘的历史截图会有出入；排名只在当日集合内可比。",
        })

    def load_hunter_history(self, date):
        """读指定日期选股猎手数据：heat history 快照（秒级）优先；非快照日期 → 全量运行（支持扫描任意日期）。"""
        out = {"date": date, "available": False, "error": ""}
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            key = date.replace("-", "")
            items = hist.get(key, [])
            if not items:
                # 2026-08-16: 非历史快照日期 → 全量运行（拉取该日行情+计算），支持任意日期扫描
                return self._load_hunter_impl(date)
            rows = []
            for i, s in enumerate(items):
                rows.append({
                    "排名": i + 1,
                    "板块": s.get("板块", ""),
                    "股票数量": s.get("股票数量", 0),
                    "平均分": s.get("平均分", 0),
                    "涨停数": s.get("涨停数", 0),
                    "热度": s.get("热度分"),
                    "趋势": s.get("趋势", ""),
                    "前三强": "",
                })
            # 板块→成分股（从 watchlist_jiuyan.json 静态筛选，非实时价）
            sector_stocks = {}
            try:
                jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
                for code, info in jy.items():
                    if not isinstance(info, dict):
                        continue
                    # sector 字段 + 韭研概念（含编号字段）双源匹配（覆盖更全）
                    concepts = _jiuyan_concepts(info) or str(info.get("概念", ""))
                    sector_field = str(info.get("sector", ""))
                    nm = info.get("name", info.get("名称", code))
                    all_text = (sector_field + "_" + concepts).replace("|", "_").replace("/", "_")
                    parts = [c.strip() for c in all_text.split("_") if c.strip() and len(c.strip()) >= 2]
                    for s in items:
                        sector = s.get("板块", "")
                        if not sector:
                            continue
                        matched = any(sector == p or sector in p or p in sector
                                      for p in parts)
                        if matched:
                            # 2026-10-20：历史视图无实时价、不走 _tier_sector_stocks，
                            # 静态 龙头/中军（jiuyan_role）直接落到 tier 字段，前端徽章照出。
                            _role = str(info.get("jiuyan_role") or "").strip()
                            _stk = {"code": code, "name": nm,
                                    "concepts": [c.strip() for c in str(concepts).split("|") if c.strip()],
                                    "score": 0, "d5": 0, "d6": 0,
                                    "d9": 0, "change_pct": 0, "limit_up": 0}
                            if _role in ("龙头", "中军"):
                                _stk["tier"] = _role
                                _stk["tier_static"] = True
                                _rsn = str(info.get("jiuyan_role_reason") or "").strip()
                                if _rsn:
                                    _stk["tier_reason"] = _rsn
                            sector_stocks.setdefault(sector, []).append(_stk)
                            break
            except Exception:
                pass

            out["available"] = True
            out["summary_cols"] = ["排名", "板块", "股票数量", "平均分", "涨停数", "热度", "趋势", "前三强"]
            out["summary_rows"] = rows
            out["sector_stocks"] = sector_stocks
            # 2026-10-24：历史视图同样下发板块阶段（asof=查看日，口径与今日数据一致）
            try:
                from modules.sector_phase import all_category_phases
                out["sector_phase"] = all_category_phases(date)
            except Exception:
                out["sector_phase"] = {}
            # 2026-10-19：历史视图同样下发概念注释（细分分组头 tooltip 与「今日数据」一致）
            out["concept_descs"] = _jiuyan_all_concept_descs()
            # 2026-10-16：历史视图**没有** build_* 字段（`sector_stocks` 是现搭的），
            # 但"时机条件"列仍要画出一致的圆点槽位 ⇒ 标签真源照样下发，否则该列会退化成空白。
            out["cond_labels"] = COND_LABELS
            out["is_history"] = True
            out["refreshed_at"] = date
            return _clean(out)
        except Exception as e:
            out["error"] = f"读取历史失败: {e}"
            return out

    def sector_history(self, sector):
        """板块历史：近 N 日该板块的热度/均分/涨停/股票数趋势。"""
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            points = []
            for d in sorted(hist.keys()):
                hit = next((s for s in hist.get(d, []) if s.get("板块") == sector), None)
                if hit:
                    points.append({
                        "date": d[4:6] + "-" + d[6:8],
                        "heat": hit.get("热度分"),
                        "avg": hit.get("平均分"),
                        "limit_up": hit.get("涨停数"),
                        "count": hit.get("股票数量"),
                    })
            return _clean({"sector": sector, "points": points[-30:]})
        except Exception as e:
            return {"sector": sector, "error": str(e), "points": []}

    # ---------- 板块轮动（移植自 sector-rotation-v2） ----------


    def _rotation_df_to_rows(self, df):
        """DataFrame → JSON 安全行列表。"""
        if df is None or df.empty:
            return []
        import numpy as np
        import pandas as pd
        rows = []
        for _, row in df.iterrows():
            item = {}
            for c in df.columns:
                v = row[c]
                if isinstance(v, (np.integer,)):
                    v = int(v)
                elif isinstance(v, (np.floating,)):
                    v = float(v)
                elif isinstance(v, np.bool_):
                    v = bool(v)
                elif isinstance(v, (pd.Timestamp, datetime)):
                    v = str(v)
                item[str(c)] = v
            rows.append(item)
        return _clean(rows)

    def sector_rotation_dates(self):
        """板块轮动可用交易日 + 缓存就绪状态。"""
        try:
            from sector_rotation import data_fetch as _df
            dates = _df.available_dates()
            ready, message = _df.cache_readiness()
            return {"ready": ready, "dates": dates, "message": message}
        except Exception as e:
            return {"ready": False, "dates": [], "message": f"板块轮动初始化失败: {e}"}

    def sector_rotation_progress(self):
        """板块轮动日线缓存构建进度（供前端轮询）。"""
        try:
            from sector_rotation import data_fetch as _df
            prog = dict(_df.ROTATION_PROGRESS)
        except Exception:
            prog = {"running": False, "phase": "", "done": 0, "total": 0, "msg": ""}
        return {
            "running": bool(ROTATION_RUN_STATE.get("running")) or bool(prog.get("running")),
            "error": ROTATION_RUN_STATE.get("error"),
            "phase": prog.get("phase", ""),
            "done": int(prog.get("done") or 0),
            "total": int(prog.get("total") or 0),
            "msg": prog.get("msg", ""),
        }

    def _rotation_bootstrap_work(self, codes):
        try:
            from sector_rotation import data_fetch as _df
            _df.bootstrap_daily_cache(codes)
        except Exception as e:
            ROTATION_RUN_STATE["error"] = str(e)
        finally:
            ROTATION_RUN_STATE["running"] = False

    def _build_rotation(self, date, view, tail_days):
        from sector_rotation import data_fetch as _df
        from sector_rotation.engine import build_rotation_model
        daily, industry, dates = _df.load_rotation_inputs(view, date)
        if daily.empty:
            raise ValueError("日线缓存为空，请先构建。")
        if not date or date not in dates:
            date = dates[-1]
        _ckey = (view, str(date), int(tail_days or 18))
        # 命中缓存（内存 → 磁盘），避免 build_rotation_model(~15s) 重复计算
        _cached = _ROTATION_CACHE_MEM.get(_ckey)
        if _cached is None:
            try:
                _fp = _ROTATION_CACHE_DIR / f"{_ckey[0]}_{_ckey[1]}_{_ckey[2]}.json"
                if _fp.exists():
                    _cached = json.loads(_fp.read_text(encoding="utf-8"))
            except Exception:
                _cached = None
        if _cached is not None:
            return _cached
        model = build_rotation_model(
            daily, industry,
            as_of=date, tail_days=int(tail_days or 18),
            include_growth_indices=(view == "industry"),
        )
        _result = {
            "as_of": model.as_of,
            "market_state": model.market_state,
            "summary": _clean(model.summary),
            "sector_frame": self._rotation_df_to_rows(model.sector_frame),
            "trail_frame": self._rotation_df_to_rows(model.trail_frame),
            "family_frame": self._rotation_df_to_rows(model.family_frame),
            "leaders_frame": self._rotation_df_to_rows(model.leaders_frame),
            "dates": dates,
        }
        try:
            _ROTATION_CACHE_MEM[_ckey] = _result
            _ROTATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (_ROTATION_CACHE_DIR / f"{_ckey[0]}_{_ckey[1]}_{_ckey[2]}.json").write_text(
                json.dumps(_result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return _result

    def load_sector_rotation(self, date=None, view="industry", tail_days=18):
        """板块轮动主入口。缓存未就绪 → 后台 bootstrap 并返回进度；就绪 → 返回轮动模型。"""
        try:
            from sector_rotation import data_fetch as _df
            try:
                _df.update_today_if_needed()   # 工作日收盘后自动补当日快照
            except Exception:
                pass
            ready, message = _df.cache_readiness()
            if not ready:
                if not ROTATION_RUN_STATE.get("running") and not _df.ROTATION_PROGRESS.get("running"):
                    ROTATION_RUN_STATE["running"] = True
                    ROTATION_RUN_STATE["error"] = None
                    codes = _df.full_market_codes()
                    _th.Thread(target=self._rotation_bootstrap_work, args=(codes,), daemon=True).start()
                return {"status": "bootstrapping", "message": message, "progress": self.sector_rotation_progress()}
            try:
                result = self._build_rotation(date, view, tail_days)
            except Exception as e:
                return {"status": "error", "message": str(e)}
            return {"status": "ok", **result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    # ---------- 每日大盘复盘（LLM，2026-08-23 新增） ----------
    def _review_bootstrap_work(self, date, cfg):
        try:
            from core.market_review import run_market_review_stream
            _REVIEW_RUN_STATE["output"] = ""
            def _on_text(t):
                _REVIEW_RUN_STATE["output"] = (_REVIEW_RUN_STATE.get("output") or "") + t
            run_market_review_stream(date, cfg, _on_text)
        except Exception as e:
            _REVIEW_RUN_STATE["error"] = str(e)
        finally:
            _REVIEW_RUN_STATE["running"] = False

    def get_llm_config(self):
        from core.market_review import load_llm_config
        return load_llm_config()

    def save_llm_config(self, base_url, model, api_key, reasoning_effort=""):
        from core.market_review import save_llm_config as _s
        return _s(base_url, model, api_key, reasoning_effort)

    def run_daily_review(self, date, base_url, model, api_key, reasoning_effort=""):
        """大盘复盘（后台线程）。模型配置保存后即触发。返回 {status: running/error}。"""
        try:
            from core.market_review import save_llm_config
        except Exception as e:
            return {"status": "error", "message": f"market_review 加载失败: {e}"}
        cfg = save_llm_config(base_url, model, api_key, reasoning_effort)
        if not cfg.get("saved"):
            return {"status": "error", "message": "模型配置不完整（base_url / model / api_key 必填）"}
        if _REVIEW_RUN_STATE.get("running"):
            return {"status": "running", "message": "已有复盘进行中"}
        _REVIEW_RUN_STATE.update({"running": True, "error": None, "output": ""})
        _th.Thread(target=self._review_bootstrap_work, args=(date, cfg), daemon=True).start()
        return {"status": "running"}

    def daily_review_progress(self):
        return {"running": bool(_REVIEW_RUN_STATE.get("running")), "error": _REVIEW_RUN_STATE.get("error"),
                "output": _REVIEW_RUN_STATE.get("output") or ""}

    def get_daily_review(self, date):
        fp = BASE / "t_io" / "validation" / "daily_review" / f"market_review_{date}.md"
        data = {}
        jp = BASE / "t_io" / "validation" / "daily_review" / f"market_review_{date}.json"
        try:
            if jp.exists():
                data = json.loads(jp.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        try:
            if fp.exists():
                return {"text": fp.read_text(encoding="utf-8"), "exists": True, "data": data}
        except Exception:
            pass
        return {"text": "", "exists": False, "data": {}}

    def get_daily_review_list(self):
        """历史复盘日期列表（2026-08-23：存档翻看）。"""
        d = BASE / "t_io" / "validation" / "daily_review"
        dates = []
        try:
            for fp in sorted(d.glob("market_review_*.md")):
                st = fp.stem.replace("market_review_", "")
                if st and len(st) == 10:
                    dates.append(st)
        except Exception:
            pass
        return {"dates": dates}

    def get_sector_breadth(self, days=25, asof=None):
        """韭研复盘广度/阶段（2026-10-24）。

        数据源 `stock_hunter/data/sector_breadth.json`（每日复盘入口：
        `scripts/daily_recap_ingest.py`）。供**每日复盘页的主线广度表**与
        **选股猎手的板块阶段徽章**共用同一套口径（`modules/sector_phase.py`）。
        asof 给定时按该日截断（历史复盘日期可回看当时的阶段）。
        """
        try:
            from modules.sector_phase import snapshot
            return snapshot(max_days=int(days or 25), asof=asof or None)
        except Exception as e:
            return {"error": str(e), "asof": "", "dates": [], "themes": [],
                    "ai_chain": [], "categories": {}}

    def get_margin_balance(self, days=30):
        """近 N 日两融余额（融资融券余额），供每日复盘两融面板（2026-08-29）。"""
        try:
            from core.market_review import fetch_margin_balance
            return fetch_margin_balance(None, days=int(days or 30))
        except Exception as e:
            return {"missing": True, "reason": str(e)[:120], "series": []}

    def get_zt_dt_history(self, days=30):
        """近 N 日涨停/跌停数（读 sentiment_daily.jsonl），供每日复盘涨停跌停面板走势图（2026-08-29）。"""
        fp = BASE / "t_io" / "logs" / "sentiment_daily.jsonl"
        try:
            rows = []
            if fp.exists():
                with open(fp, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if d.get("date"):
                            rows.append({"date": d["date"], "zt": d.get("zt_count"), "dt": d.get("dt_count")})
            rows.sort(key=lambda r: r["date"])
            # 按 date 去重（同一天多次生成 eod/tail 快照，保留最后一条）
            dedup = {}
            for r in rows:
                dedup[r["date"]] = r
            rows = [dedup[k] for k in sorted(dedup)]
            n = int(days) if days else 0
            return {"series": rows[-n:] if n > 0 else rows}
        except Exception:
            return {"series": []}

    # ---------- 独立配置（账户总资金+已实现亏损） ----------
    def load_portfolio_config(self):
        """读 t_io/state/accounts_config.json（独立于 holdings.json，用户更新持仓不会覆盖）。
        2026-08-30 起账户配置唯一源头为 accounts_config.json；旧 portfolio_config.json 仅作回退。"""
        fp = PORTFOLIO if PORTFOLIO.exists() else PORTFOLIO_LEGACY
        data = _load_json(fp, {})
        return _clean({
            "accounts": data.get("accounts", {}),
            "realized_loss": data.get("realized_loss", {}),
        })

    def load_osc_day(self, date=None):
        """每日选股记录（OSC 超跌跳空确认）：盘前候选 → 10:00 三关漏斗 → 选中 + 逐只原因。

        来源优先级：
          ① `decision_{date}.json`（2026-10-17 起由策略落盘）⇒ `source="recorded"`
          ② 否则**读时还原**（`prescreen_`/`fills_` + 策略日志 [+ 审计镜像]）⇒ `source="reconstructed"`

        ⚠️ 拿不到的字段一律返回 `None`（前端显示「未记录」），**绝不填 0** ——
           "没记录"与"确实是 0 只"是两件事。
        ⚠️ 判定漏斗各关是**独立通过数**，不是"上一关的幸存者"（见 decision.evaluate 注释）。
        ⚠️ `fills.summary.candidates` = `len(picks)`，**不是**盘前候选数，别用。
        """
        date = str(date or datetime.now().strftime("%Y-%m-%d"))
        names = _jiuyan_names()
        rec = _load_json(OSC_BOOK / f"decision_{date}.json", None)
        pre = _load_json(OSC_BOOK / f"prescreen_{date}.json", None)
        fills = _load_json(OSC_BOOK / f"fills_{date}.json", None) or {}

        def _nm(code):
            return names.get(str(code), "")

        # 实际下单/成交（10:00 决策 ≠ 成交：可能被拒/资金不足/顺移）
        buys = {}
        try:
            for b in (fills.get("buys") or []):
                c = str(b.get("code"))
                if c:
                    buys[c] = {"status": b.get("status"), "filled_qty": b.get("filled_qty"),
                               "filled_price": b.get("filled_price"),
                               "qty_placed": b.get("qty_placed"),
                               "defer_reason": b.get("defer_reason"), "round": b.get("round")}
        except Exception:
            buys = {}

        if isinstance(rec, dict) and rec.get("stocks") is not None:
            pm = rec.get("premarket") or {}
            pre_d = (pre or {})
            cand = pre_d.get("candidates") or {}
            bd = {}
            for v in cand.values():
                if isinstance(v, dict) and v.get("board"):
                    bd[v["board"]] = bd.get(v["board"], 0) + 1
            return _clean({
                "available": True, "date": date, "source": "recorded", "partial": False,
                "missing": [],
                "premarket": {"candidates_n": pre_d.get("candidates_n"),
                              "universe": pm.get("universe") or pre_d.get("universe"),
                              "board_top_n": pm.get("board_top_n") or pre_d.get("board_top_n"),
                              "board_dist": dict(sorted(bd.items(), key=lambda kv: -kv[1])[:8])},
                "funnel": rec.get("funnel"), "reason_counts": rec.get("reason_counts"),
                "n_failures": (rec.get("funnel") or {}).get("n_failures"),
                "picks": [dict(p, name=p.get("name") or _nm(p.get("code")))
                          for p in (rec.get("picks") or [])],
                "stocks": [dict(s, name=s.get("name") or _nm(s.get("code")))
                           for s in (rec.get("stocks") or [])],
                "buys": buys,
            })

        # ── 还原：历史日（10-13~10-16）只有部分字段 ──
        lg = _osc_parse_log(date)
        n_cand = (pre or {}).get("candidates_n")
        if n_cand is None:
            n_cand = lg.get("premarket_n")
        if n_cand is None:
            n_cand = _osc_backtrace_candidates(date)     # 最后手段（28MB，带缓存）
        universe = (pre or {}).get("universe") or lg.get("universe")
        top_n = (pre or {}).get("board_top_n") or lg.get("board_top_n")

        picks = []
        if fills.get("picks"):
            picks = [{"code": str(p.get("code")), "gap": p.get("gap"),
                      "board": p.get("board", "")} for p in fills["picks"]]
        elif lg.get("picks"):
            picks = lg["picks"]
        for i, p in enumerate(picks):
            p["rank"] = i + 1
            p["status"] = "picked"
            p["reason"] = "ok"
            p["name"] = _nm(p.get("code"))

        rows = []
        for s in (fills.get("spares") or []):
            rows.append({"code": str(s.get("code")), "name": _nm(s.get("code")),
                         "board": s.get("board", ""), "gap": s.get("gap"),
                         "status": "spare", "reason": "ok"})
        for r in (lg.get("near") or []):                 # 只有当日日志记下的若干只
            rows.append({"code": r["code"], "name": r.get("name") or _nm(r["code"]),
                         "board": r.get("board", ""), "gap": r.get("gap"),
                         "status": "failed", "reason": r.get("reason_cn")})
        rows = picks + rows
        rows.sort(key=lambda r: -((r.get("gap") if r.get("gap") is not None else -1e9)))

        missing = []
        if n_cand is None:
            missing.append("盘前候选数")
        if not lg.get("funnel"):
            missing.append("10:00 判定漏斗")
        if not rows:
            missing.append("逐只原因")
        available = bool(n_cand is not None or picks or lg.get("funnel") or fills)
        return _clean({
            "available": available, "date": date,
            "source": "reconstructed" if available else "none",
            "partial": bool(missing), "missing": missing,
            "premarket": {"candidates_n": n_cand, "universe": universe,
                          "board_top_n": top_n, "board_dist": {}},
            "funnel": lg.get("funnel"),
            "reason_counts": lg.get("reason_counts"),
            "n_failures": None,
            "bars": {"got": lg.get("bars_n"), "want": lg.get("bars_want")},
            "picks": picks, "stocks": rows, "buys": buys,
        })

    def load_auto_status(self):
        """P4-3 自动盘页：读 t_io/bridge（heartbeat.json + 当日 events + KILL_SWITCH）。
        返回 heartbeat{positions/cash/index_regime/index_score} + order/fill/reject/risk 计数
        + 最新 10 条事件 + kill_switch 状态。GM 格式持仓 key 经 codec 转内部码。

        2026-10-10: 持仓成本改用 fill 流派生的加权均价（见 `_auto_fill_costs`）——引擎的
        heartbeat cost 在**加仓后不重新加权**，与券商终端对不上。"""
        hb = _load_json(BRIDGE_DIR / "heartbeat.json", {})
        out = {"heartbeat": None, "events": {"order": 0, "fill": 0, "reject": 0, "risk": 0},
               "latest": [], "kill_switch": (BRIDGE_DIR / "KILL_SWITCH").exists(),
               "bridge_dir": str(BRIDGE_DIR)}
        if hb:
            try:
                from core.market_data.codec import to_internal
            except Exception:
                to_internal = lambda g: str(g).split(".")[-1]
            # 名称源（heartbeat positions 只有 qty/cost）：holdings_auto.json → NAMES 兜底
            _names = dict(NAMES)
            try:
                for _c, _h in (_load_json(STATE_DIR / "holdings_auto.json", {}) or {}).items():
                    if isinstance(_h, dict) and _h.get("name"):
                        _names[str(_c).split("_")[0]] = _h["name"]
            except Exception:
                pass
            derived = _auto_fill_costs()
            positions = {}
            for gk, p in (hb.get("positions", {}) or {}).items():
                try:
                    c6 = to_internal(str(gk))
                except Exception:
                    c6 = str(gk)
                pp = dict(p) if isinstance(p, dict) else {"qty": p}
                key = str(c6).split("_")[0]
                if not pp.get("name"):
                    pp["name"] = _names.get(key, "")
                # 派生均价只在「数量对得上」时采信（数量对不上说明有手工单，成本也就无从复原）
                dv = derived.get(key)
                if dv and int(pp.get("qty") or 0) == dv["qty"]:
                    pp["cost"] = dv["cost"]
                    pp["cost_src"] = "fill派生"
                else:
                    pp["cost_src"] = "引擎快照"
                # 市值/浮盈（同券商「持仓市值/浮动盈亏」口径：收盘价 × 数量 − 成本 × 数量）
                try:
                    _q = int(pp.get("qty") or 0)
                    _c = float(pp.get("cost") or 0)
                    _px = _daily_last_close(key) if _q > 0 else 0.0
                    if _px:
                        pp["last_price"] = round(_px, 3)
                        pp["market_value"] = round(_px * _q, 2)
                        pp["unreal_pnl"] = round(_px * _q - _c * _q, 2)
                except Exception:
                    pass
                positions[c6] = pp
            out["heartbeat"] = {
                "time": hb.get("time"), "bar": hb.get("bar"),
                "positions": positions, "cash": hb.get("cash"),
                "index_regime": hb.get("index_regime"), "index_score": hb.get("index_score"),
            }
        date_str = datetime.now().strftime("%Y%m%d")
        ep = BRIDGE_DIR / f"events_{date_str}.jsonl"
        latest = []
        if ep.exists():
            try:
                with open(ep, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except Exception:
                            continue
                        ev = e.get("event")
                        if ev in out["events"]:
                            out["events"][ev] += 1
                            latest.append(e)
            except Exception:
                pass
        out["latest"] = latest[-10:]
        return _clean(out)

    def load_auto_pnl(self):
        """自动盘盈亏账单（2026-10-10 需求5）：按 code 用 fill.pos_after 归零切分
        「建仓→清仓」周期，汇总每周期已实现盈亏；未平仓周期用日线缓存最新收盘算浮盈。
        同一只票清仓后再次建仓 → 新周期（重新计算）。

        纯磁盘（t_io/bridge/events_*.jsonl + t_io/cache/daily_kline），零网络，SWR 缓存 30s。
        名称缺失问题一并修（holdings_auto.json → NAMES 兜底）。
        """
        import time as _t
        today = datetime.now().strftime("%Y-%m-%d")
        c = _AUTO_PNL_CACHE
        if c["data"] is not None and c["date"] == today and (_t.time() - c["ts"] < _AUTO_PNL_TTL):
            return c["data"]
        try:
            data = self._compute_auto_pnl(today)
        except Exception as e:
            return {"error": str(e)[:200], "stocks": [], "summary": {}}
        c.update({"ts": _t.time(), "date": today, "data": data})
        return data

    def _compute_auto_pnl(self, today):
        """账本体（纯函数式磁盘读，无副作用）。"""
        _DAILY_CACHE_DIR = _DAILY_KLINE_DIR   # 本地常量（2026-10-11）：原 import 会连带 pandas 进首帧
        # 名称源三层（后者覆盖前者，最权威的放最后）：
        #   韭研 watchlist（5382 码，最广）→ NAMES 常量 → holdings_auto.json（用户自己的名字）
        names = dict(_jiuyan_names())
        names.update(NAMES)
        try:
            for code, h in (_load_json(STATE_DIR / "holdings_auto.json", {}) or {}).items():
                if isinstance(h, dict) and h.get("name"):
                    names[str(code).split("_")[0]] = h["name"]
        except Exception:
            pass
        # 按时间序读全部 fill（跨日排序）——与 _auto_fill_costs 同源，避免两处解析漂移
        fills = _read_auto_fills()
        cost_map = _auto_fill_costs()

        # ---- 逐笔流水 + 按日累计已实现（移动加权平均成本，随时间顺序全局跑一遍）----
        # 引擎 write_fill 不带股票名/净发生金额/成交前持仓 ⇒ 读取端补齐：name 反查、
        # net 按 BUY 出金/SELL 入金、pos_after 直接用事件自带余额。
        disp_fills = []
        realized_by_date = {}
        _run = {}                                   # code -> [qty, cost_incl_fee]
        for e in fills:
            code = str(e.get("code") or "").split("_")[0]
            if not code:
                continue
            side = str(e.get("side") or "").upper()
            try:
                qty = int(e.get("qty") or 0)
                price = float(e.get("price") or 0)
                fee = float(e.get("fee") or 0)
                pos_after = int(e.get("pos_after") or 0)
            except Exception:
                continue
            amt = price * qty
            tstr = str(e.get("time") or "")
            st = _run.setdefault(code, [0, 0.0])
            if side == "BUY":
                st[0] += qty
                st[1] += amt + fee
                net = -(amt + fee)
            else:
                avg = (st[1] / st[0]) if st[0] else 0.0
                matched = min(qty, st[0])
                # 只对**配对到的那部分**计提价差。若直接写 (amt-fee) - matched*avg，
                # 卖出腿没有对应买入腿时（持仓在事件流起点之前建的）matched=0 ⇒ 整笔卖出款
                # 被当成纯利润，realized_cum 会凭空虚高。
                per = ((amt - fee) / qty) if qty else 0.0
                rl = matched * (per - avg)
                st[0] = max(0, st[0] - qty)
                st[1] = max(0.0, st[1] - matched * avg)
                if st[0] == 0:
                    st[1] = 0.0
                realized_by_date[tstr[:10]] = realized_by_date.get(tstr[:10], 0.0) + rl
                net = amt - fee
            disp_fills.append({
                "date": tstr[:10], "time": tstr[11:19], "code": code,
                "name": names.get(code, code), "side": side,
                "qty": qty, "price": round(price, 3), "amount": round(amt, 2),
                "net": round(net, 2), "fee": round(fee, 2), "pos_after": pos_after,
            })
        disp_fills.sort(key=lambda r: (r["date"], r["time"]), reverse=True)

        # 逐 code 切周期
        by_code = {}
        for e in fills:
            code = str(e.get("code") or "").split("_")[0]
            if not code:
                continue
            by_code.setdefault(code, []).append(e)

        def _last_close(code):
            fp = _DAILY_CACHE_DIR / f"{code}.json"
            try:
                rows = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
                if rows:
                    return float(rows[-1].get("close") or 0)
            except Exception:
                pass
            return 0.0

        stocks = []
        tot_realized = 0.0
        tot_unreal = 0.0
        tot_mv = 0.0
        tot_cost = 0.0
        for code, evs in by_code.items():
            cycles = []
            cur = None
            prev_pos = 0
            for e in evs:
                side = str(e.get("side") or "").upper()
                try:
                    qty = int(e.get("qty") or 0)
                    price = float(e.get("price") or 0)
                    fee = float(e.get("fee") or 0)
                    pos_after = int(e.get("pos_after") or 0)
                except Exception:
                    continue
                if cur is None:
                    # 新周期起点。prev_pos>0 ⇒ 历史被截断（无 0 起点），标注不规整
                    cur = {"start": str(e.get("time") or "")[:10], "end": None,
                           "buy_qty": 0, "buy_amt": 0.0, "buy_fee": 0.0,
                           "sell_qty": 0, "sell_amt": 0.0, "sell_fee": 0.0,
                           "irregular": prev_pos > 0}
                if side == "BUY":
                    cur["buy_qty"] += qty
                    cur["buy_amt"] += price * qty
                    cur["buy_fee"] += fee
                elif side == "SELL":
                    cur["sell_qty"] += qty
                    cur["sell_amt"] += price * qty
                    cur["sell_fee"] += fee
                # 位置连续性护栏：pos_after 与 prev_pos ± qty 不符 ⇒ 标记 irregular
                expect = prev_pos + qty if side == "BUY" else prev_pos - qty
                if pos_after != expect:
                    cur["irregular"] = True
                prev_pos = pos_after
                if pos_after <= 0:
                    cur["end"] = str(e.get("time") or "")[:10]
                    cycles.append(cur)
                    cur = None
                    prev_pos = 0
            if cur is not None:
                cycles.append(cur)  # 未平仓（open）

            # 汇总每个周期
            last_px = _last_close(code) if any(
                c["buy_qty"] > c["sell_qty"] for c in cycles) else None
            out_cycles = []
            stock_realized = 0.0
            stock_unreal = 0.0
            stock_mv = 0.0
            stock_cost = 0.0
            open_qty_total = 0
            # 2026-10-24 owner：「自动盘根据逐步流水模仿手动盘给出每支股票的盈亏状态」
            # ⇒ 补上手动盘那张逐股表有的列：买入额/卖出额/费用/收益率/区间/现价/持仓成本
            stock_buy_amt = 0.0
            stock_sell_amt = 0.0
            stock_fees = 0.0
            _starts, _ends = [], []
            for c in cycles:
                closed = c["end"] is not None
                matched = min(c["buy_qty"], c["sell_qty"])
                unit_cost = ((c["buy_amt"] + c["buy_fee"]) / c["buy_qty"]) if c["buy_qty"] else 0.0
                realized = (c["sell_amt"] - c["sell_fee"]) - matched * unit_cost if matched else 0.0
                residual = c["buy_qty"] - c["sell_qty"]
                unreal = 0.0
                unit_disp = unit_cost
                if not closed and residual > 0:
                    px = float(last_px or 0)
                    # 在场浮盈用**券商口径成本**（fill 派生、不含手续费），与终端「浮动盈亏」
                    # 逐只对齐；派生数量对不上（有手工单）才退回本周期含费均价。
                    dv = cost_map.get(code)
                    unit_disp = dv["cost"] if (dv and dv["qty"] == residual) else unit_cost
                    remain_cost = residual * unit_disp
                    unreal = px * residual - remain_cost if px else 0.0
                    stock_unreal += unreal
                    stock_mv += px * residual
                    stock_cost += remain_cost
                    open_qty_total += residual
                stock_realized += realized
                stock_buy_amt += c["buy_amt"]
                stock_sell_amt += c["sell_amt"]
                stock_fees += c["buy_fee"] + c["sell_fee"]
                if c["start"]:
                    _starts.append(c["start"])
                _ends.append(c["end"] or today)
                cost_basis = matched * unit_cost
                out_cycles.append({
                    "start": c["start"], "end": c["end"],
                    "days": self._pnl_days(c["start"], c["end"] or today),
                    "status": "closed" if closed else "open",
                    "buy_qty": c["buy_qty"], "sell_qty": c["sell_qty"],
                    "avg_buy": round(unit_cost, 3),
                    # 在场周期的券商口径成本价（不含费）；前端「均买」优先显示它
                    "cost_price": round(unit_disp, 3) if not closed else None,
                    "avg_sell": round((c["sell_amt"] / c["sell_qty"]), 3) if c["sell_qty"] else 0,
                    "fees": round(c["buy_fee"] + c["sell_fee"], 2),
                    "realized_pnl": round(realized, 2),
                    "pnl_pct": round(realized / cost_basis * 100, 2) if cost_basis else 0,
                    "open_qty": residual if not closed else 0,
                    "last_price": round(float(last_px or 0), 3) if (not closed and last_px) else None,
                    "unreal_pnl": round(unreal, 2),
                    "irregular": bool(c.get("irregular")),
                })
            out_cycles.sort(key=lambda x: x["start"], reverse=True)
            tot_realized += stock_realized
            tot_unreal += stock_unreal
            tot_mv += stock_mv
            tot_cost += stock_cost
            _tot = stock_realized + stock_unreal
            _dv = cost_map.get(code) or {}
            stocks.append({
                "code": code,
                "name": names.get(code, code),
                "realized_pnl": round(stock_realized, 2),
                "unreal_pnl": round(stock_unreal, 2),
                "total_pnl": round(_tot, 2),
                "pnl_pct": round(_tot / stock_buy_amt * 100, 2) if stock_buy_amt else None,
                "open_qty": open_qty_total,
                "market_value": round(stock_mv, 2),
                "buy_amt": round(stock_buy_amt, 2),
                "sell_amt": round(stock_sell_amt, 2),
                "fees": round(stock_fees, 2),
                "last_price": round(float(last_px or 0), 3) if last_px else None,
                "cost_price": round(float(_dv.get("cost") or 0), 3) if _dv.get("qty") else None,
                "first": min(_starts) if _starts else "",
                "last": max(_ends) if _ends else "",
                "trades_n": len(evs),
                "round_trips": sum(1 for c in cycles if c["end"] is not None),
                "cycles": out_cycles,
            })
        stocks.sort(key=lambda s: -(s["realized_pnl"] + s["unreal_pnl"]))

        # ---- 每日账户盈亏：逐日取 heartbeat_YYYY-MM-DD.jsonl **末条**心跳 ----
        # 引擎只写「当日现金 + 收盘持仓{qty,cost}」，账户权益需配日线缓存收盘价复算。
        # 末条 = 当日收盘快照 ⇒ day_pnl 差分即当日盈亏。只读尾块，禁整文件读（见 _tail_last_json_line）。
        try:
            from core.market_data.codec import to_internal as _to_int
        except Exception:
            _to_int = lambda g: str(g).split(".")[-1]
        _close_memo = {}

        def _close_on(code, ds):
            """该 code 在 ds 当日（含）之前的最后收盘价；日线缓存无此票 → 0。"""
            if code not in _close_memo:
                m = {}
                try:
                    rows = json.loads(
                        (_DAILY_CACHE_DIR / f"{code}.json").read_text(encoding="utf-8")
                    ).get("rows") or []
                    for r in rows:
                        d0 = str(r.get("date") or "")[:10]
                        if d0:
                            m[d0] = float(r.get("close") or 0)
                except Exception:
                    pass
                _close_memo[code] = m
            m = _close_memo[code]
            if not m:
                return 0.0
            if ds in m:
                return m[ds]
            prior = [d0 for d0 in m if d0 <= ds]
            return m[max(prior)] if prior else m[min(m)]

        _real_dates = sorted(realized_by_date)
        daily = []
        prev_equity = None
        cum_realized = 0.0
        for fp in sorted(BRIDGE_DIR.glob("heartbeat_*.jsonl")):
            ds = fp.name[len("heartbeat_"):-len(".jsonl")]
            hb = _tail_last_json_line(fp)
            if not hb:
                continue
            mv = 0.0
            cost_tot = 0.0
            for gk, p in (hb.get("positions") or {}).items():
                if not isinstance(p, dict):
                    continue
                try:
                    q = int(p.get("qty") or 0)
                    cst = float(p.get("cost") or 0)
                except Exception:
                    continue
                if q <= 0:
                    continue
                try:
                    c6 = str(_to_int(str(gk))).split("_")[0]
                except Exception:
                    c6 = str(gk)
                cost_tot += q * cst
                mv += q * _close_on(c6, ds)
            csh = hb.get("cash")
            equity = (float(csh) if isinstance(csh, (int, float)) else 0.0) + mv
            # 累计已实现：把 ds（含）之前所有成交日的已实现全部算入（含无心跳的交易日）
            for d0 in _real_dates:
                if d0 <= ds:
                    cum_realized += realized_by_date[d0]
            _real_dates = [d0 for d0 in _real_dates if d0 > ds]
            day_pnl = round(equity - prev_equity, 2) if prev_equity is not None else None
            daily.append({
                "date": ds,
                "cash": round(float(csh), 2) if isinstance(csh, (int, float)) else None,
                "market_value": round(mv, 2),
                "cost": round(cost_tot, 2),
                "equity": round(equity, 2),
                "unreal_pnl": round(mv - cost_tot, 2),
                "day_pnl": day_pnl,
                "realized_cum": round(cum_realized, 2),
                # 权益日间跳变 >15% ⇒ 该日现金变动与策略成交流水对不上。实测成因是仿真终端
                # 手工单/孤儿成交（只在 risk.orphan_fill 里，不进 fill 流也不进持仓快照）。
                "suspect": bool(day_pnl is not None and prev_equity
                                and abs(day_pnl) > 0.15 * abs(prev_equity)),
            })
            prev_equity = equity
        daily.sort(key=lambda r: r["date"])
        cash = None
        try:
            cash = _load_json(BRIDGE_DIR / "heartbeat.json", {}).get("cash")
        except Exception:
            pass
        _cash = round(float(cash), 2) if isinstance(cash, (int, float)) else None
        return _clean({
            "summary": {
                "realized_pnl": round(tot_realized, 2),
                "unreal_pnl": round(tot_unreal, 2),
                "market_value": round(tot_mv, 2),
                "cost_basis": round(tot_cost, 2),
                "cash": _cash,
                "total_assets": round((_cash or 0.0) + tot_mv, 2),
                "positions_count": len([s for s in stocks if s["open_qty"] > 0]),
            },
            "stocks": stocks,
            "fills": disp_fills,
            "daily": daily,
        })


    def _pnl_days(self, start, end):
        try:
            d0 = datetime.strptime(str(start)[:10], "%Y-%m-%d")
            d1 = datetime.strptime(str(end)[:10], "%Y-%m-%d")
            return max(1, (d1 - d0).days + 1)
        except Exception:
            return 0

    def load_buy_confirm_pending(self):
        """人工确认闸（2026-08-30）：读 BUY_PENDING.json（引擎写）待确认买入请求 + 当日已拒绝清单，
        并从 BUY_DECISION.json 组出已应答集合（防 GUI 重启重弹）。date!=今日 → 忽略。"""
        today = datetime.now().strftime("%Y-%m-%d")
        bp = _load_json(BRIDGE_DIR / "BUY_PENDING.json", {})
        if not isinstance(bp, dict) or bp.get("date") != today:
            return {"date": today, "pending": [], "rejected_today": [], "answered": {}}
        reqs = [r for r in (bp.get("pending") or {}).values()
                if isinstance(r, dict) and r.get("code")]
        reqs.sort(key=lambda r: (r.get("request_ts") or 0))
        dec = _load_json(BRIDGE_DIR / "BUY_DECISION.json", {})
        answered = {}
        if isinstance(dec, dict):
            for code, d in (dec.get("decisions") or {}).items():
                if isinstance(d, dict) and d.get("request_id"):
                    answered[code] = d["request_id"]
        return _clean({"date": today, "pending": reqs,
                       "rejected_today": bp.get("rejected_today") or [],
                       "answered": answered})

    def respond_buy_confirm(self, code, request_id, decision):
        """人工确认闸：写用户确认/拒绝到 BUY_DECISION.json（GUI 单写者，tmp+replace 原子写）。
        引擎只消费 request_id 与内存 pending 匹配的 decision，陈旧/错配一律忽略。"""
        if decision not in ("confirm", "reject"):
            return {"ok": False, "error": "decision 必须为 confirm/reject"}
        if not code or not request_id:
            return {"ok": False, "error": "code/request_id 不能为空"}
        fp = BRIDGE_DIR / "BUY_DECISION.json"
        data = _load_json(fp, {})
        if not isinstance(data, dict) or "decisions" not in data:
            data = {"date": datetime.now().strftime("%Y-%m-%d"), "decisions": {}}
        (data.setdefault("decisions", {}))[code] = {
            "request_id": request_id, "decision": decision,
            "ts": datetime.now().timestamp()}
        data["date"] = datetime.now().strftime("%Y-%m-%d")
        data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "request_id": request_id, "decision": decision}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 历史成交（券商 xls/xlsx）→ 逐股盈亏台账（2026-10-10 需求3） ----------
    # 券商「历史成交」是账户维度的成交底账（各账户唯一的真实成交来源）；解析后落盘缓存，
    # 前端「图表分析」页按每只票的累计已实现盈亏出图，全站账户名也以它为准（见 _account_of）。
    # 输入统一为 xls/xlsx（各券商列序/列名不一 ⇒ 表头驱动），且**可一次选多个**文件。

    def load_accounts(self):
        """全站账户列表 —— 唯一来源是导入的历史成交台账；未导入 → 空列表。"""
        accs = _trade_history_accounts()
        return _clean({"imported": bool(accs), "accounts": accs})

    def load_trade_history(self):
        """读已导入的历史成交台账；未导入过 → {imported: False}。

        缓存里另存了合并后的原始成交行（`trades`，供下次导入增量合并），不下发前端。
        """
        d = _load_json(TRADE_HISTORY_LEDGER, None)
        if not isinstance(d, dict) or not d.get("stocks"):
            return {"imported": False}
        d.pop("trades", None)                  # 原始成交行只留在磁盘里
        try:
            self._ledger_append_today(d)
        except Exception:
            pass
        return _clean(d)

    @staticmethod
    def _ledger_append_today(d, now=None):
        """台账里**没有成交的交易日**也要有一个点（2026-10-23 owner：「图没更新到今天」）。

        为什么会缺：`cum`/`cum_all` 是**按成交日**逐日累计的（`core.trade_history` 里
        `for d in sorted(grp)`），最后一天 = 最后一个成交日。所以"今天没交易"时，
        「累计收益趋势（含浮动）」会停在昨天 —— 可浮动那部分明明每天都变。

        补法（收盘后、且今天还没有成交行时）：
          · 已实现、累计买入成本都不变（今天没成交）；
          · `cum_total = cum_realized + 浮动`，浮动取台账里的 `unrealized`（导入时按最新收盘价算的）；
          · 合计口径同理：`total_realized + total_unrealized`。
        这个点是**估算**（`today_est: true`，前端 tooltip 会标出来）：若今天其实有成交但没导入，
        它不会包含那些成交 —— 那种情况请先导交割单。
        """
        now = now or datetime.now()
        if now.weekday() >= 5 or now.strftime("%H:%M") < "15:00":
            return                             # 盘中/周末不补：台账里的 last_price 不是今天的收盘价
        today = now.strftime("%Y-%m-%d")
        thru = str(d.get("data_through") or "")
        if not thru or thru >= today:
            return
        cum, cum_all = d.get("cum") or [], d.get("cum_all") or []
        if not cum or not cum_all:
            return
        unreal_by_acc = {}
        for s in d.get("stocks") or []:
            a = s.get("account") or ""
            unreal_by_acc[a] = unreal_by_acc.get(a, 0.0) + float(s.get("unrealized") or 0.0)
        last_by_acc = {}
        for e in cum:
            last_by_acc[e.get("account")] = e
        for a, e in last_by_acc.items():
            if str(e.get("date") or "") >= today:
                continue
            total = round(float(e.get("cum_realized") or 0.0) + unreal_by_acc.get(a, 0.0), 2)
            buy = float(e.get("cum_buy") or 0.0)
            cum.append({"account": a, "date": today,
                        "cum_realized": e.get("cum_realized"), "cum_buy": e.get("cum_buy"),
                        "pct": e.get("pct"),
                        "cum_total": total,
                        "pct_total": round(total / buy * 100.0, 3) if buy else None,
                        "today_est": True})
        e = cum_all[-1]
        if str(e.get("date") or "") < today:
            total = round(float(d.get("total_realized") or 0.0)
                          + float(d.get("total_unrealized") or 0.0), 2)
            buy = float(e.get("cum_buy") or 0.0)
            cum_all.append({"date": today,
                            "cum_realized": d.get("total_realized"), "cum_buy": e.get("cum_buy"),
                            "pct": (round(float(d.get("total_realized") or 0.0) / buy * 100.0, 3)
                                    if buy else None),
                            "cum_total": total,
                            "pct_total": round(total / buy * 100.0, 3) if buy else None,
                            "delta_total": round(total - float(e.get("cum_total") or 0.0), 2),
                            "today_est": True})

    def import_trade_history(self, path=None):
        """导入历史成交（xls/xlsx/伪-xls/PDF）：path 为空 → 弹系统文件选择器（**可多选**）。

        `path` 可为单个路径字符串或路径列表（CLI/测试用）。**增量导入**：与上一次已落盘的
        成交行合并去重后再跑台账——券商是「每期一份」，日常只导新那几天，不能把老数据冲掉。
        解析 → 原子写 t_io/state/trade_history_ledger.json → 返回台账。
        注意：跑在 pywebview 串行主线程，多份大文件约 1~3s 会短暂卡界面（用户主动点击触发）。
        """
        src = path
        if not src:
            try:
                import webview
                wins = getattr(webview, "windows", None) or []
                if not wins:
                    return {"imported": False, "error": "窗口未就绪，稍后重试"}
                # pywebview 6.x 无模块级 create_file_dialog，须走 Window 实例
                picked = wins[0].create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=True,
                    file_types=("交割单 (*.xls;*.xlsx;*.pdf)", "Excel (*.xls;*.xlsx)",
                                "PDF (*.pdf)", "所有文件 (*.*)"))
                if not picked:
                    return {"imported": False, "cancelled": True}
                src = picked
            except Exception as e:
                return {"imported": False, "error": f"打开文件选择器失败: {e}"}
        paths = [src] if isinstance(src, (str, Path)) else list(src)
        fps = [Path(str(p)) for p in paths]
        missing = [str(p) for p in fps if not p.exists()]
        if missing:
            return {"imported": False, "error": f"文件不存在: {missing[0]}"}
        # 增量导入：把上一次落盘的成交行一起送进去合并（只导新那几天也不能把历史冲掉）
        prev = _load_json(TRADE_HISTORY_LEDGER, None)
        prev_trades = (prev or {}).get("trades") if isinstance(prev, dict) else None
        prev_through = (prev or {}).get("data_through") if isinstance(prev, dict) else None
        try:
            from core.trade_history import build_from_files
            led = build_from_files(fps, prev_trades=prev_trades)
        except Exception as e:
            return {"imported": False, "error": f"解析失败: {str(e)[:200]}"}
        if not led.get("stocks"):
            detail = "；".join((led.get("parse_warnings") or [])[:3])
            return {"imported": False,
                    "error": "未解析到任何成交记录（确认是券商历史成交，且含「成交日期/证券代码/操作」列）"
                             + (f"：{detail}" if detail else "")}
        # 「记忆已导入到哪一天」：台账本身就是记忆（data_through = 覆盖到的最后交易日），
        # 下次导入应导该日**之后**的成交。重复导入旧文件时 advanced=False，前端据此提示。
        end = (led.get("range") or {}).get("end")
        led["imported"] = True
        led["data_through"] = end
        led["prev_data_through"] = prev_through
        led["advanced"] = bool(end and (not prev_through or end > prev_through))
        srcs = led.get("sources") or [p.name for p in fps]
        led["source"] = " + ".join(srcs) if len(srcs) > 1 else (srcs[0] if srcs else "")
        led["imported_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            TRADE_HISTORY_LEDGER.parent.mkdir(parents=True, exist_ok=True)
            tmp = TRADE_HISTORY_LEDGER.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(led, f, ensure_ascii=False)
            tmp.replace(TRADE_HISTORY_LEDGER)
        except Exception as e:
            return {"imported": False, "error": f"写缓存失败: {str(e)[:200]}"}
        _ACCT_MAP_CACHE.update({"ts": 0.0, "map": {}})   # 账户名以新台账为准，立即失效重算
        led.pop("trades", None)                          # 原始成交行只落盘，不下发前端
        return _clean(led)

    def load_position_manager(self):
        """仓位管理器：每只持仓的目标/当前市值、资金占比、超配欠配。
        目标比例 = STOCK_PARAMS 个股 stock_qty_base_pct 或 全局默认。"""
        try:
            import config
        except Exception:
            config = None
        pcfg = _load_json(PORTFOLIO, {})
        accounts = pcfg.get("accounts", {})
        # 2026-10-10: 手动/自动资金分离——本表行来自 HOLDINGS_MANUAL（手动盘），
        # 分母若含 paper 账户（账户C 国盛掘金仿真=自动盘）会让仓位占比被稀释偏小。
        # 口径同 _reconcile_cash：paper=true 归自动盘，单独汇总为 auto_capital 供展示。
        total_capital = sum(float(a.get("total_capital") or 0)
                            for a in accounts.values() if not a.get("paper"))
        auto_capital = sum(float(a.get("total_capital") or 0)
                           for a in accounts.values() if a.get("paper"))
        cur = _load_json(HOLDINGS_MANUAL, {})
        # 实时价
        px_map = {}
        try:
            for q in self.load_quotes().get("quotes", []):
                if q.get("price"):
                    px_map[q.get("code")] = float(q["price"])
        except Exception:
            pass
        # 按基础代码合并 A/B 双账户
        merged = {}
        for code, h in cur.items():
            if not isinstance(h, dict):
                continue
            base = str(code).split("_")[0]
            merged.setdefault(base, {"name": h.get("name", code), "codes": []})
            merged[base]["codes"].append((code, h))
        default_pct = 0.30
        if config is not None:
            default_pct = config.PARAMS.get("stock_qty_base_pct", 0.30)
        # 第一遍：收集各股原始比例 + 当前市值，归一化使总和=100%（目标市值总和=总资金）
        raw = []
        for base, info in merged.items():
            sp = {}
            if config is not None:
                sp = config.STOCK_PARAMS.get(base, {}) or {}
            raw_pct = sp.get("stock_qty_base_pct", default_pct)
            mkt_val = 0.0
            total_qty = 0
            cost_total = 0.0
            for code, h in info["codes"]:
                px = px_map.get(code) or px_map.get(base) or float(h.get("pre_close") or 0)
                qty = int(h.get("qty") or 0)
                mkt_val += px * qty
                total_qty += qty
                cost_total += float(h.get("cost") or 0) * qty
            if total_qty <= 0:
                continue  # fix 2026-08-20: 已清仓(base 全部 qty=0)不进仓位管理器
            raw.append({"base": base, "code": base, "name": info["name"], "raw_pct": raw_pct,
                        "mkt_val": mkt_val, "total_qty": total_qty,
                        "cost": (cost_total / total_qty) if total_qty else 0})
        # W33 A3: 归一化/欠配缺口/分批 抽到 config.build_position_gap 共享（避免 GUI/扫描器两处漂移）
        _cost_map = {r["base"]: r["cost"] for r in raw}
        gap_ctx = config.build_position_gap(total_capital, raw, default_pct) if config else None
        # 2026-09-14: manual 做T 下线——"可T仓位"列（config.suggest_t_budget → t_suggest/t_ratio/t_amp）删除。
        rows = []
        for r in (gap_ctx["rows"] if gap_ctx else []):
            row = dict(r)
            row["cost"] = round(_cost_map.get(r["code"], 0), 3) if r.get("total_qty") else 0
            rows.append(row)
        rows.sort(key=lambda x: -x["pct"])
        return _clean({
            "total_capital": round(total_capital, 0),
            "auto_capital": round(auto_capital, 0),
            "rows": rows,
            "sum_mkt": round(sum(r["mkt_val"] for r in rows), 0),
            "sum_pct": round(sum(r["pct"] for r in rows), 1),
        })


    def _parse_log_line(self, line):
        """HH:MM:SS [LEVEL] msg  →  {t, level, msg}。"""
        t, level, msg = "", "", line
        try:
            if len(line) >= 8 and line[2] == ":" and line[5] == ":":
                t = line[:8]
                rest = line[8:].strip()
                if rest.startswith("[") and "]" in rest:
                    level, msg = rest[1:rest.index("]")], rest[rest.index("]") + 1:].strip()
                else:
                    msg = rest
        except Exception:
            pass
        return {"t": t, "level": level, "msg": msg,
                "key": Api._is_key_line(line)}


    def _is_key_line(line):
        for w in Api.KEY_LINE_WORDS:
            if w in line:
                return True
        for w in Api.NOISE_LINE_WORDS:
            if w in line:
                return False
        return False

    # ---------- 盘中实时载荷（仅今天） ----------
    def load_live(self, date):
        """decision_trace 尾部 + intraday_state + 大盘盘中尾部。"""
        _GUI_HB["ts"] = _time_mod.time()   # 10s 轮询打点
        out = {"signals": [], "intraday_state": {}}

        fp = TRACES / f"decision_trace_{date}.jsonl"
        if fp.exists():
            rows = []
            for line in open(fp, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
            # 取尾部：非 HOLD 优先 + 近阈 HOLD（差 5 分内）+ 普通 HOLD 补足到 20
            non_hold = [r for r in rows if r.get("decision") not in ("HOLD", None)]
            near_hold = [
                r for r in rows if r.get("decision") == "HOLD"
                and ((r.get("buy_score") or 0) >= (r.get("buy_threshold") or 99) - 5
                     or (r.get("sell_score") or 0) >= (r.get("sell_threshold") or 99) - 5)
            ]
            tail = (non_hold + near_hold + [r for r in rows if r.get("decision") == "HOLD"])[-20:]
            def _sig(r):
                bs = r.get("buy_score") or 0; ss = r.get("sell_score") or 0
                bt = r.get("buy_threshold") or 99; st = r.get("sell_threshold") or 99
                near = r.get("decision") == "HOLD" and (bs >= bt - 5 or ss >= st - 5)
                sw = r.get("swing_meta") or {}
                reason = r.get("decision_reason")
                if r.get("decision") == "HOLD" and sw.get("wait"):
                    reason = sw.get("wait")
                return {
                    "scan_time": r.get("scan_time"), "code": r.get("code"),
                    "name": r.get("name"), "price": r.get("price"),
                    "buy_score": bs, "sell_score": ss, "decision": r.get("decision"),
                    "reason": reason,
                    "swing_meta": sw,
                    "buy_threshold": bt, "sell_threshold": st,
                    "near": near,
                }
            out["signals"] = [_sig(r) for r in tail]

        out["intraday_state"] = _load_json(INTRADAY_STATE, {})
        # 2026-10-10: 「今日盘中 S」图表下线 ⇒ 删掉这里的 market_intraday。它每次 10s 轮询都要
        # 解析整个 index_regime_{date}.jsonl（50~220KB / 约 4400 行）且**跑在主线程**，
        # 是纯浪费。跨日 S 由图表的 load_market_score 单独提供。
        return _clean(out)

    # ---------- 建仓/加仓信号增量轮询 ----------
    def poll_new_position_signals(self, date):
        """增量读 position_builder，返回新增 signal（scan_type=intraday）。
        in_holdings=true → 加仓，false → 建仓。首次/切日期/轮转只建基线。"""
        out = {"signals": [], "baseline": True}
        fp = TRACES / f"position_builder_{date}.jsonl"
        if not fp.exists():
            self._pos["date"] = None
            self._pos["offset"] = 0
            self._pos["seen"] = set()
            return out
        try:
            size = fp.stat().st_size
        except Exception:
            return out

        st = self._pos
        if st["date"] != date or size < st["offset"]:
            st["date"] = date
            st["offset"] = size
            st["seen"] = set()
            return out
        if size <= st["offset"]:
            return {"signals": [], "baseline": False}

        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                f.seek(st["offset"])
                data = f.read()
        except Exception:
            return out
        st["offset"] = size
        out["baseline"] = False

        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("scan_type") != "intraday" or r.get("verdict") != "signal":
                continue
            key = (r.get("scan_time"), r.get("code"))
            if key in st["seen"]:
                continue
            st["seen"].add(key)
            in_hold = bool(r.get("in_holdings"))
            out["signals"].append({
                "scan_time": r.get("scan_time"),
                "code": r.get("code"), "name": r.get("name"),
                "price": r.get("price"),
                "composite_score": r.get("composite_score"),
                "verdict": r.get("verdict"),
                "in_holdings": in_hold,
                "type": "加仓" if in_hold else "建仓",
                "suggested_qty": (r.get("position") or {}).get("suggested_qty")
                    if isinstance(r.get("position"), dict) else r.get("suggested_qty"),
                "suggested_price": (r.get("position") or {}).get("suggested_price")
                    if isinstance(r.get("position"), dict) else r.get("suggested_price"),
            })
        return _clean(out)

    # ---------- 建仓股池增删 ----------
    def search_stock(self, query):
        """按代码或名称模糊搜索股票。返回匹配列表（最多10条）。"""
        import urllib.request as _ur, os as _os
        for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"]:
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"
        query = (query or "").strip()
        if not query:
            return {"results": []}
        results = []
        # 1. 精确代码: provider 快照直查（名称；竞价/快照专用腾讯，gm 无中文名）
        if query.isdigit() and len(query) == 6:
            try:
                from core.market_data.tencent_provider import TencentProvider
                snap = TencentProvider().snapshot_auction([query])
                if query in snap and snap[query].get("name"):
                    results.append({"code": query, "name": snap[query]["name"]})
            except Exception:
                pass
        # 2. 名称模糊: 从 watchlist_jiuyan.json 匹配（{code: {name,...}} 或 list）
        if not results:
            try:
                jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
                if isinstance(jy, dict):
                    for code, info in jy.items():
                        name = str(info.get("名称", "") if isinstance(info, dict) else "")
                        if query in name or query in code:
                            results.append({"code": code, "name": name})
                            if len(results) >= 10:
                                break
                elif isinstance(jy, list):
                    for s in jy[:800]:
                        code = str(s.get("代码", ""))
                        name = str(s.get("名称", ""))
                        if query in name or query in code:
                            results.append({"code": code, "name": name})
                            if len(results) >= 10:
                                break
            except Exception:
                pass
        return _clean({"results": results})

    # ---------- 异常监控池（2026-10-22 owner）----------
    # 「建仓信号扫描」下面的手动自选监控表：搜索代码/名称加入、逐行可删。
    # 数据来源与建仓表/猎手**同一真源**（`_hunter_build_conformance` → `core.build_decision`），
    # 只读本地日线缓存 ⇒ 零网络，单只 ~0.1s（有 (code,date,regime,mtime) 缓存，重复更便宜）。
    _ABN_FP = STATE_DIR / "abnormal_pool.json"

    def _abn_read(self):
        d = _load_json(self._ABN_FP, {})
        st = (d or {}).get("stocks") if isinstance(d, dict) else None
        return st if isinstance(st, dict) else {}

    def _abn_write(self, stocks):
        try:
            self._ABN_FP.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._ABN_FP.with_suffix(".tmp")
            tmp.write_text(json.dumps({"stocks": stocks, "updated_at":
                                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._ABN_FP)
            return True
        except Exception:
            return False

    def abnormal_pool_add(self, text):
        """按 6 位代码或名称加入异常监控池。名称在韭研表中模糊匹配，唯一才加。"""
        t = str(text or "").strip()
        if not t:
            return {"ok": False, "error": "请输入代码或名称"}
        names = _jiuyan_names()
        code = t if (len(t) == 6 and t.isdigit()) else ""
        if not code:
            hits = [c for c, n in names.items() if str(n) == t] or                    [c for c, n in names.items() if t in str(n)]
            if not hits:
                return {"ok": False, "error": f"没找到「{t}」（韭研表里没有）"}
            if len(hits) > 1:
                return {"ok": False, "error": f"「{t}」匹配到多只，请输入完整代码："
                                              + "、".join(hits[:8])}
            code = hits[0]
        stocks = self._abn_read()
        if code in stocks:
            return {"ok": True, "code": code, "already": True}
        stocks[code] = {"name": names.get(code) or code,
                        "added_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        return {"ok": self._abn_write(stocks), "code": code}

    def abnormal_pool_remove(self, code):
        stocks = self._abn_read()
        c = str(code or "").split("_")[0]
        if c in stocks:
            stocks.pop(c, None)
        return {"ok": self._abn_write(stocks), "code": c}

    def abnormal_pool_set_reason(self, code, text):
        """手动写/改**涨停原因**（2026-10-22 owner）。

        只存人写的这一行字，不参与任何判定；行情/判定仍走 build_decision。
        `text` 传空串 = 清空。
        """
        c = str(code or "").split("_")[0]
        stocks = self._abn_read()
        if c not in stocks:
            return {"ok": False, "error": "该票不在异常监控池里"}
        e = stocks[c] if isinstance(stocks.get(c), dict) else {"name": c}
        txt = str(text if text is not None else "").strip()
        if txt:
            e["reason"] = txt[:120]              # 一句话就够，防误贴长文
        else:
            e.pop("reason", None)
        e["reason_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if txt else ""
        stocks[c] = e
        return {"ok": self._abn_write(stocks), "code": c, "reason": txt[:120]}

    def load_abnormal_pool(self, date=None):
        """异常监控池 → 与「建仓信号扫描」同列的数据。

        行字段刻意做成**建仓表的形状**（verdict/composite_score/conditions/…），
        前端直接复用同一套单元格渲染（色阶、圆点、阶段徽章），不另写一份样式。
        """
        date = str(date or datetime.now().strftime("%Y-%m-%d"))
        stocks = self._abn_read()
        codes = list(stocks.keys())
        if not codes:
            return _clean({"has_data": True, "date": date, "rows": [], "counts": {},
                           "cond_labels": COND_LABELS, "note": ""})
        conf = {}
        try:
            conf = self._hunter_build_conformance(codes, date) or {}
        except Exception:
            conf = {}
        # 技术标签（2026-10-23 owner：「异常监控池没有显示技术标签」）—— 与建仓表**同一来源**
        # `load_stock_tags_batch`（命中缓存即时返回；冷启动是"起后台算 + 立即返空"，前端轮询自填）。
        # ⚠️ 不要挪到后台线程里取：那会把**空 tags** 钉进缓存（见 `_beacon_enrich` 的注释）。
        # ⚠️ **offline=True**（批量视图铁律，同风向标 7896 行）：这张是**展示表**，若走在线路径
        #    每只都会 `_trend30_trend` → GM（池子 max_workers=1，串行）⇒ 18 只排满 GM，
        #    把 K 线/指数/成交额等**真正需要实时的**调用全堵在后面（2026-10-23 GUI 卡顿排查）。
        try:
            _tags_map = (self.load_stock_tags_batch(codes, offline=True, date=date) or {}).get("tags") or {}
        except Exception:
            _tags_map = {}
        names = _jiuyan_names()
        rows = []
        for c in codes:
            meta = stocks.get(c) or {}
            k = conf.get(c) or {}
            rows.append({
                "code": c,
                "name": meta.get("name") or names.get(c) or c,
                "added_at": meta.get("added_at") or "",
                # 手动写的涨停原因（owner 2026-10-22）：只展示，不参与判定
                "reason_manual": meta.get("reason") or "",
                "reason_at": meta.get("reason_at") or "",
                # 技术标签（空数组 = 后台还在批算，前端显示 ⏳ 标签 并按 15s 轮询补）
                "tags": ((_tags_map.get(c) or {}).get("tags") or []),
                "dev365": (_tags_map.get(c) or {}).get("dev365"),
                # —— 以下与建仓表同名列同义 ——
                "verdict": k.get("verdict") or ("insufficient_data" if not k else "pending"),
                "composite_score": int(k.get("score") or 0),
                "score_ceiling": int(k.get("score_ceiling") or 100),
                "signal_reachable": bool(k.get("signal_reachable", False)),
                "conditions": k.get("conds") or {},
                "timing": None,
                "timing_features": {"above_ma5": k.get("above_ma5"),
                                    "above_ma20": k.get("above_ma20")},
                "build_stage": k.get("stage"),
                # 与建仓表「通过 X/3」同口径：t_regime + t_trend + t_drawdown 三条
                "build_met": int(k.get("met") or 0) if k else None,
                "build_above_ma20": k.get("above_ma20"),
                "build_above_ma5": k.get("above_ma5"),
                "build_reason": k.get("reason") or "",
                "pool": "manual",
                "in_holdings": False,
                "scan_time": "",
                "_scans": 0,
                "_has_conf": bool(k),
            })
        # 当日「均线回站」+ 突破时刻（2026-10-23 owner，三张表同源）
        try:
            self._attach_ma_reclaim(rows, date)
        except Exception:
            pass
        rows.sort(key=lambda r: -int(r.get("composite_score") or 0))
        return _clean({"has_data": True, "date": date, "rows": rows,
                       "counts": {"total": len(rows)},
                       "cond_labels": COND_LABELS,
                       "note": "判定/得分与建仓信号扫描同源（build_decision）；"
                               "未在本地日线缓存里的票显示 —"})

    def add_to_watchlist(self, code, name):
        """将股票加入 watchlist_buy.json（status=monitoring）。

        人工盘/自动盘**互不阻塞**（2026-09-20 owner 拍板）：auto 池标的也可加入人工盘，
        人工盘建仓表会显示它（带「自动」徽章）。仍照旧写 pool=auto，
        以免触发引擎 validate_pool_split 的启动守卫（pool=manual 且属 AUTO_POOL 才冲突）。
        """
        fp = STATE_DIR / "watchlist_buy.json"
        wl = _load_json(fp, {"stocks": {}, "total_capital": 300000, "max_per_stock_pct": 0.2})
        stocks = wl.setdefault("stocks", {})
        if code in stocks:
            stocks[code]["status"] = "monitoring"
        else:
            # T-4(2026-09-02): 新建条目缺省 manual 会与 auto 池标的冲突（002409 教训）——
            # 若该码属 auto/both 池则照抄持仓池归属。
            # fix 2026-09-21: 原先一律写 "auto" 把 both 压平 → _is_manual_pool 判 False
            # → 该标的永远不进人工盘扫描（表现：建仓表一直"等待扫描"）。照抄原值即可
            # 满足启动守卫（守卫只拒 pool=="manual"）。
            _entry = {"name": name, "status": "monitoring", "composite_score": 0,
                      "criteria_met": {}, "suggested_qty": 0, "in_holdings": False}
            try:
                from src.holdings_repo import get_entry
                _h = get_entry(code)
                if _h and str(_h.get("pool") or "") in ("auto", "both"):
                    _entry["pool"] = str(_h.get("pool"))
            except Exception:
                pass
            stocks[code] = _entry
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(wl, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "code": code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def add_and_scan(self, code, name):
        """加入建仓股池并**立即单股扫描**，返回可展示的成败明细（GUI「+ 添加」用）。

        为什么需要它：扫描表 _agg_position_builder 只从扫描轨迹
        position_builder_{date}.jsonl 构行，而 add_to_watchlist 只写 watchlist_buy.json
        ⇒ 新加的股不扫描就永远不出现在表里，用户会误判"没加成功"。

        返回 {ok, code, name, scan_date, visible_in_manual, scan:{verdict,
        composite_score, block_reason, reason}, scan_error}；失败 {ok:False, error}。
        """
        r = self.add_to_watchlist(code, name)
        if not r.get("ok"):
            return r

        # 扫描只写"今天"的轨迹，不写历史文件（避免篡改历史）；
        # 若用户正在看历史日期，前端据此提示"切回今日可见该行"
        scan_date = datetime.now().strftime("%Y-%m-%d")
        scan = None
        scan_error = None
        try:
            from core.position_builder import run_position_scan
            res = run_position_scan(date_str=scan_date, target_code=code,
                                    scan_type="manual", silent=True, no_feishu=True) or []
            if res:
                _r0 = res[0]
                # reason = 卡点 > 扫描内部错误 > note，兜底给一句人话，
                # 避免 insufficient_data（无分钟数据/未开盘）这类情况前端只能显示英文枚举
                _errs = [str(x) for x in (_r0.get("errors") or []) if str(x).strip()]
                _note = str(_r0.get("note") or "").strip()
                scan = {
                    "verdict": _r0.get("verdict"),
                    "composite_score": int(_r0.get("composite_score") or 0),
                    "block_reason": _r0.get("block_reason"),
                    "reason": _r0.get("block_reason") or (_errs[0] if _errs else (_note or None)),
                }
                # 单股扫描异常不阻断整轮（position_builder 的既有约定），这里如实带回
                if _r0.get("scan_error"):
                    scan_error = str(_r0.get("scan_error"))[:200]
            else:
                scan_error = "扫描未返回结果（该码可能不在股池中）"
        except Exception as e:
            scan_error = str(e)[:200]

        # 走到这里说明该股会被人工盘表显示（auto 池未持有的已在上面被拒；
        # 持仓股即便 pool=auto 也被 _visible() 放行）
        return {"ok": True, "code": code, "name": name, "scan_date": scan_date,
                "visible_in_manual": True, "scan": scan, "scan_error": scan_error}

    def remove_from_watchlist(self, code):
        """从 watchlist_buy.json 删除股票。"""
        fp = STATE_DIR / "watchlist_buy.json"
        wl = _load_json(fp, {})
        stocks = wl.get("stocks", {})
        if code in stocks:
            del stocks[code]
            try:
                tmp = fp.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(wl, f, ensure_ascii=False, indent=2)
                tmp.replace(fp)
                return {"ok": True, "code": code}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"ok": False, "error": "股票不在股池中"}

    # ---------- W33 G3: 人工确认建仓（回写 signal_history，喂 forward_tracker） ----------
    def confirm_position(self, code, price=None, qty=None):
        """人工确认建仓 → signal_history 追加 {confirmed, confirm_price, confirm_time, confirm_qty}，
        状态置 confirmed（不再重复出建仓建议）。forward_tracker 以 confirm_price 为基准算前瞻收益。"""
        fp = STATE_DIR / "watchlist_buy.json"
        wl = _load_json(fp, {})
        stocks = wl.get("stocks", {})
        if code not in stocks:
            return {"ok": False, "error": "股票不在股池中"}
        stock = stocks[code]
        hist = stock.setdefault("signal_history", [])
        confirm_price = float(price) if price else float(stock.get("suggested_price") or 0)
        confirm_qty = int(qty) if qty else int(stock.get("suggested_qty") or 0)
        entry = {
            "date": datetime.now().strftime("%Y-%m-%d"),
            "confirm_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "confirmed": True,
            "confirm_price": confirm_price,
            "confirm_qty": confirm_qty,
            "source": "gui_confirm",
        }
        hist.append(entry)
        stock["status"] = "confirmed"
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(wl, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "code": code, "entry": entry}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_signal_condition_detail(self, code, date=None):
        """获取某支股票的详细条件检查报告（用于 GUI 折叠式面板）。
        返回 {conditions_met, conditions_total, conditions: [{name, status, message, detail}], blockers: [...]}"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        fp = TRACES / f"position_builder_{date}.jsonl"
        if not fp.exists():
            return {"available": False, "error": "扫描数据不可用"}

        # 查找该股票的最新扫描记录
        latest_record = None
        try:
            lines = open(fp, encoding="utf-8").read().splitlines()
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                    if r.get("code") == code:
                        latest_record = r
                        break
                except Exception:
                    continue
        except Exception:
            pass

        if not latest_record:
            return {"available": False, "error": f"未找到 {code} 的扫描记录"}

        # 2026-10-15 owner：「卡片中的信息要和现在的实现保持一致」。
        # 记录是**扫描当时的快照** ⇒ 规则改动后老记录永远显示旧口径（用户点开 09-15 的卡
        # 仍看到旧得分/旧条件）。故打开时**按当前规则实时重算这一天**。
        # fail-safe：重算失败就退回快照，并在卡上标明（不静默把旧值当现值）。
        _live_ok = False
        try:
            from core import position_builder as _pb
            _si = {"代码": str(code), "名称": str(latest_record.get("name") or code)}
            # 进程内缓存：同一 (code,date) 重复点开秒回（实测单只 1~3.5s）。
            # 规则改动必须重启 GUI 才生效 ⇒ 进程重启即自动失效，不必再挂版本号。
            _ck = (str(code), str(date))
            _fresh = _COND_FRESH_CACHE.get(_ck)
            if _fresh is None:
                _fresh = _pb.scan_stock(str(code), _si, str(date))
                if _fresh:
                    if len(_COND_FRESH_CACHE) > 200:
                        _COND_FRESH_CACHE.clear()
                    _COND_FRESH_CACHE[_ck] = _fresh
            if _fresh:
                _stored_scan_time = latest_record.get("scan_time")
                latest_record = {**latest_record, **_fresh}
                latest_record["scan_time"] = _stored_scan_time      # 保留"哪次扫的"
                latest_record["recomputed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                _live_ok = True
        except Exception:
            _live_ok = False

        # 构建条件详情列表：先时机门控条件（现行判定，2026-08-27 对齐），再旧双通道（参考级）
        conditions_detail = []
        channels = latest_record.get("channels", {})
        conditions = latest_record.get("conditions", {})
        verdict = latest_record.get("verdict", "")
        blockers_raw = latest_record.get("blockers", [])
        timing = latest_record.get("timing") or {}

        _bk_by_key = {b.get("key"): b for b in blockers_raw if isinstance(b, dict)}

        # 1) 时机门控条件（trace 中 conditions 为 bool 映射；此前面板只显示旧双通道，
        #    真正的建仓条件不可见——"条件不符合没说清楚"的根源）
        for _k, _label, _required in (
                ("t_regime", "市场有方向", True),
                ("t_trend", "多头结构", True),
                ("t_drawdown", "回撤到位", True),
                # 2026-10-15：GO 与得分口径一致新增的两条硬约束（与 position_builder 写入侧同步）
                ("t_ma20", "站上MA20", True),
                ("t_ma5", "站上MA5", True),
                ("t_golden", "MACD金叉(近5日)", False)):
            if _k not in conditions:
                continue
            _ok = bool(conditions.get(_k))
            _bk = _bk_by_key.get(_k)
            if _ok:
                _msg = "已满足"
            elif _bk:
                _msg = f"未满足：{_bk.get('gap_txt', '')}（需：{_bk.get('need', '')}）"
            else:
                _msg = "未满足"
            conditions_detail.append({
                "category": "timing",
                "name": _label if _required else f"{_label}（加分项，不影响判定）",
                "verdict": "pass" if _ok else "fail",
                "message": _msg,
            })
        # 否决因子（2026-08-27 因子挖掘：爆量/远离MA60，仅触发时存在于 conditions）
        if conditions.get("t_veto") is False:
            _bk = _bk_by_key.get("t_veto") or {}
            conditions_detail.append({
                "category": "timing",
                "name": "否决因子（爆量≥3倍 / 偏离MA60>+20%）",
                "verdict": "fail",
                "message": f"已触发否决：{_bk.get('gap_txt') or timing.get('reason') or ''}",
            })

        # 2) 旧双通道（参考级·未验收 W33 A4，不驱动判定）
        for ch_name in ("iceberg", "breakout"):
            ch = channels.get(ch_name, {})
            if not ch:
                continue
            conditions_detail.append({
                "category": ch_name,
                "name": f"（参考）{ch.get('name', ch_name)}",
                "verdict": ch.get("verdict", ""),
                "score": ch.get("score", 0),
                "message": f"得分 {ch.get('score', 0)}/100，状态: {ch.get('verdict', '未知')}（参考级，不影响判定）"
            })

        # 增强 blockers 信息：计算具体指标值
        blockers_detail = []
        for b in blockers_raw:
            blocker_info = {
                "key": b.get("key", ""),
                "label": b.get("label", ""),
                "current": b.get("cur", ""),
                "required": b.get("need", ""),
                "gap": b.get("gap_txt", ""),
                "message": f"【{b.get('label', '未知')}】{b.get('gap_txt', '条件未满足')}"
            }

            # 增强特定卡点的信息
            if b.get("key") == "t_regime":
                # 计算具体的指数目标值
                blocker_info["detail"] = self._compute_regime_targets(latest_record)
            elif b.get("key") == "t_drawdown":
                # 添加回撤逻辑说明
                blocker_info["detail"] = self._compute_drawdown_detail(latest_record)

            blockers_detail.append(blocker_info)

        # 计算满足的必要条件数（时机门控 3 必要条件；trace 中 conditions 为 bool 映射，
        # 此前按 dict.get('passed') 取值恒为 0——已修复）
        _NECESSARY = ("t_regime", "t_trend", "t_drawdown")
        if any(k in conditions for k in _NECESSARY):
            conditions_met = sum(1 for k in _NECESSARY if conditions.get(k))
            conditions_total = 3
        else:
            conditions_met = 0
            conditions_total = 0

        return {
            "available": True,
            "code": code,
            "name": latest_record.get("name", code),
            "verdict": verdict,
            "composite_score": latest_record.get("composite_score", 0),
            "scan_time": latest_record.get("scan_time", ""),
            "conditions_met": conditions_met,
            "conditions_total": conditions_total,
            "conditions_detail": conditions_detail,
            "blockers": blockers_detail,
            "divergence": latest_record.get("divergence_detail", {}),
            # 2026-10-15：卡片是否按**当前规则**实时重算过（false=重算失败、显示的是扫描快照）
            "live_recomputed": bool(_live_ok),
            "recomputed_at": latest_record.get("recomputed_at") or "",
        }

    def _compute_regime_targets(self, record: dict) -> dict:
        """从 blockers 或特征中计算市场方向条件的具体指数目标值"""
        # 2026-08-27: regime 改读 timing（trace 顶层无 regime 字段，此前恒为空导致详情错乱）
        regime = (record.get("timing") or {}).get("regime") or record.get("regime", "")
        blockers = record.get("blockers", [])

        # 尝试从 blockers 中提取指数信息
        regime_blocker = None
        for b in blockers:
            if b.get("key") == "t_regime":
                regime_blocker = b
                break

        detail = {
            "regime": regime,
            "raw_message": regime_blocker.get("gap_txt") if regime_blocker else "未知",
        }

        # 2026-08-28: 给出具体指数点位（当前值/多头线/空头线/各差多少）。
        # 优先读 trace timing.index（新格式）；旧 trace 无该字段时从指数缓存现算（口径同 timing_gate）。
        # A-4(2026-09-07): 旧 trace 兜底按 code 所属板读对应缓存——原恒读 index_sh000001，
        # 深主板/创业板/科创板个股（30x/00x/68x/588x）会错用上证点位。新 trace 的
        # timing.index.index_code 已带板指数，直接采用；缺时按 code resolve_index 回落。
        idx_info = (record.get("timing") or {}).get("index") or {}
        close, up_line, dn_line, ma60 = (idx_info.get("close"), idx_info.get("up_line"),
                                         idx_info.get("dn_line"), idx_info.get("ma60"))
        if not (close and up_line and dn_line):
            try:
                _idx_code = idx_info.get("index_code")
                if not _idx_code:
                    from core.board_index import resolve_index as _ri
                    _idx_code = _ri(str(record.get("code") or "sh000001"))[0]
                import json as _json
                _cache = TRACES.parent / "cache" / "daily_kline" / f"index_{_idx_code}.json"
                _rows = _json.loads(_cache.read_text(encoding="utf-8"))["rows"]
                import pandas as _pd
                _c = _pd.Series([float(x["close"]) for x in _rows])
                close = float(_c.iloc[-1])
                ma60 = float(_c.rolling(60).mean().iloc[-1])
                up_line = round(ma60 * 1.005, 1)   # 与 timing_gate._regime 同口径
                dn_line = round(ma60 * 0.97, 1)
            except Exception:
                close = up_line = dn_line = ma60 = None

        if close and up_line and dn_line:
            detail["action"] = "需要指数突破 MA60 缓冲带（确立方向）"
            detail["rule"] = (f"多头线：站上 {up_line:.1f}（MA60 {ma60:.1f} × 1.005）｜"
                              f"空头线：跌破 {dn_line:.1f}（MA60 × 0.97）")
            detail["message"] = (f"当前指数 {close:.2f}，位于缓冲带 {dn_line:.1f} ~ {up_line:.1f} 内（无方向）："
                                 f"距多头线 {(up_line / close - 1) * 100:+.2f}%，"
                                 f"距空头线 {(dn_line / close - 1) * 100:+.2f}%")
        elif regime_blocker:
            gap_txt = regime_blocker.get("gap_txt", "")
            detail["message"] = gap_txt
            if "MA60" in gap_txt:
                detail["action"] = "需要指数通过 MA60 缓冲带的检查"
                if regime == "trend_up":
                    detail["rule"] = "指数需站上 MA60×1.005（多头确认）"
                elif regime == "trend_dn":
                    detail["rule"] = "指数需跌破 MA60×0.97（空头确认）"
                else:
                    detail["rule"] = "指数需突破 MA60±缓冲带（确立方向）"

        return detail

    def _compute_drawdown_detail(self, record: dict) -> dict:
        """计算回撤到位条件的详细说明"""
        # 2026-08-27: regime/drawdown 改读 timing/timing_features（trace 顶层无 features 字段，
        # 此前 drawdown 恒 0.0 → 详情永远显示"已满足"，严重误导）
        regime = (record.get("timing") or {}).get("regime") or record.get("regime", "")
        feats = record.get("timing_features") or record.get("features") or {}
        dd = feats.get("drawdown")
        if dd is None:
            # 旧 trace 无 timing_features（2026-08-27 前落盘）：从回撤卡点的 cur 文本回补（如 "-5.0%"）
            for _b in record.get("blockers", []):
                if _b.get("key") == "t_drawdown" and _b.get("cur"):
                    try:
                        dd = float(str(_b["cur"]).replace("%", "")) / 100
                    except (ValueError, TypeError):
                        pass
                    break

        detail = {
            "regime": regime,
            "current_drawdown": None,
        }
        if dd is None:
            detail.update({
                "threshold": None, "type": "回撤", "rule": "个股日线数据不足",
                "explanation": "个股日线拉取失败或不足61根，回撤无法计算。\n请检查数据管道（缓存/网络/腾讯WAF拦截）后重扫。",
                "status": "数据不足",
            })
            return detail
        # 面板按百分数直接显示（toFixed(2)+"%"），这里换算成百分数（如 -5.0 表示 -5.0%）
        detail["current_drawdown"] = round(dd * 100, 2)

        if regime == "trend_up":
            detail["threshold"] = -0.03
            detail["type"] = "浅回撤"
            detail["rule"] = "多头趋势下，需要浅回撤≥-3%"
            detail["explanation"] = (
                "在多头趋势中，股价应该快速回撤到位后继续上升。\n"
                "浅回撤（-3% 以内）表示多头力度足，适合建仓。\n"
                "若回撤超过 -3%，说明多头动能不足，需要等待。"
            )
            detail["status"] = "已满足" if dd >= -0.03 else f"未满足（差 {((-0.03) - dd) * 100:.2f}pp）"
        elif regime == "trend_dn":
            detail["threshold"] = -0.10
            detail["type"] = "深回撤"
            detail["rule"] = "空头趋势下，需要深回撤<-10%"
            detail["explanation"] = (
                "在空头趋势中，股价应该深度回撤后再继续下跌。\n"
                "深回撤（< -10%）表示有充分的获利回吐机会，适合建仓。\n"
                "若回撤不足 -10%，说明跌幅还不够深，继续等待。"
            )
            detail["status"] = "已满足" if dd < -0.10 else f"未满足（差 {(dd - (-0.10)) * 100:.2f}pp）"
        else:  # range
            detail["threshold"] = -0.03
            detail["type"] = "浅回撤"
            detail["rule"] = "震荡市中，按浅回撤≥-3% 判断"
            detail["explanation"] = (
                "在震荡市中，没有明确的主方向，保守按浅回撤标准。\n"
                "等待指数破位后确立方向，然后再调整标准。"
            )
            detail["status"] = "已满足" if dd >= -0.03 else f"未满足（差 {((-0.03) - dd) * 100:.2f}pp）"

        return detail

    def get_high_confidence_signals(self, date):
        """获取高置信度建仓信号（仅 signal/approaching 且有连续背离或无背离数据）。
        筛选规则：排除所有 weak 信号 + 单次无效背离，只显示核心信号。"""
        result = self._agg_position_builder(date, filter_high_confidence=True)
        # 更新刷新时间和技术标签
        try:
            _pb_fp = TRACES / f"position_builder_{date}.jsonl"
            result["refreshed_at"] = datetime.fromtimestamp(_pb_fp.stat().st_mtime).strftime("%H:%M:%S")
        except Exception:
            result["refreshed_at"] = ""
        # 批量个股技术标签
        try:
            codes = [r.get("code") for r in result.get("rows", []) if r.get("code")]
            if codes:
                tag_res = self.load_stock_tags_batch(codes, date=date)
                tags_map = tag_res.get("tags", {})
                for r in result.get("rows", []):
                    code = r.get("code")
                    info = tags_map.get(code, {})
                    if info:
                        r["tags"] = info.get("tags", [])
                        r["trend"] = info.get("trend")
                        r["dev365"] = info.get("dev365")
        except Exception:
            pass
        return result

    # ---------- 轻量 PB 刷新（盘中实时） ----------
    def refresh_pb(self, date):
        """仅重读 position_builder jsonl 并返回聚合结果 + 个股技术标签。"""
        result = self._agg_position_builder(date)
        # fix P0-14: refreshed_at 改为 jsonl 最后写入时间（不再用服务器当前时间冒充"实时"）
        try:
            _pb_fp = TRACES / f"position_builder_{date}.jsonl"
            result["refreshed_at"] = datetime.fromtimestamp(_pb_fp.stat().st_mtime).strftime("%H:%M:%S")
        except Exception:
            result["refreshed_at"] = ""
        # 批量个股技术标签
        try:
            codes = [r.get("code") for r in result.get("rows", []) if r.get("code")]
            if codes:
                tag_res = self.load_stock_tags_batch(codes)
                tags_map = tag_res.get("tags", {})
                for r in result.get("rows", []):
                    code = r.get("code")
                    info = tags_map.get(code, {})
                    if info:
                        r["tags"] = info.get("tags", [])
                        r["trend"] = info.get("trend")
                        r["dev365"] = info.get("dev365")
        except Exception:
            pass
        # 当日「均线回站」+ 突破时刻（2026-10-23 owner，三张表同源）
        try:
            self._attach_ma_reclaim(result.get("rows") or [], date)
        except Exception:
            pass
        return result

    def prewarm_stock_tags(self, date=None):
        """启动预热（2026-10-04）：后台算一次当日建仓池技术标签，避免首次进建仓表标签先空约 20s。
        无返回值；仅触发 load_stock_tags_batch 的后台分支。失败静默（预热不该影响启动）。"""
        try:
            date = date or datetime.now().strftime("%Y-%m-%d")
            rows = self._agg_position_builder(date).get("rows") or []
            codes = [r.get("code") for r in rows if r.get("code")]
            if codes:
                self.load_stock_tags_batch(codes)  # 无缓存 ⇒ 起后台算，立即返回
        except Exception:
            pass

    def prewarm_overview(self):
        """启动预热（2026-10-07）：后台**并发**跑一遍总览页的重端点（指数板/大盘成交额/成交额历史/
        指数背离），把冷启动成本（东财重试、多笔指数日线、12+ GM 调用）挪到后台，避免前端首帧在主
        线程上等十几秒（实测这几个端点冷启动合计 ~15s）。失败静默。"""
        from concurrent.futures import ThreadPoolExecutor

        def _safe(fn):
            try:
                fn()
            except Exception:
                pass
        try:
            with ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(_safe, [self.load_indices, self.load_market_turnover,
                                    self.load_turnover_history, self.load_index_divergence]))
        except Exception:
            pass

    def prewarm_holdings_charts(self):
        """启动预热（2026-10-04）：后台**低并发**预热持仓的个股图表缓存。

        为什么：`load_ob_analysis`（持仓日线体检）逐只调 `load_stock_chart`，冷启动实测 **14.3s**
        阻塞在主线程（热路径只需 60ms）。预热把这段冷取数挪到后台，让前端首次调用即命中热缓存。
        并发压到 3：预热是「顺便」，不该和 UI/其它后台任务（如标签批算）抢 CPU/GIL 把整体拖涩。
        失败静默；已在缓存内的标的直接跳过。"""
        try:
            cur = _load_json(HOLDINGS_MANUAL, {})
            codes = []
            for code, info in cur.items():
                if not isinstance(info, dict) or code.startswith("_"):
                    continue
                if not (info.get("qty") or 0):
                    continue  # 已清仓不体检（与 load_ob_analysis 同口径）
                bc = str(code).split("_")[0]
                if bc and bc not in codes:
                    codes.append(bc)
            if not codes:
                return
            if not hasattr(self, "_stock_chart_cache"):
                self._stock_chart_cache = {}   # 先建，避免并发首次各建一份互相覆盖
            from concurrent.futures import ThreadPoolExecutor
            _today = datetime.now().strftime("%Y-%m-%d")

            def _one(c):
                # 建图（当日内存缓存命中则秒回）
                try:
                    self.load_stock_chart(c)
                except Exception:
                    pass
                # 2026-10-07：顺带预热**技术标签**（持仓体检表要显示标签）。`_stock_tags_from_df`
                # 里最贵的是 30min 趋势 `get_trend30`（~0.8s/只、有 per-code 缓存），预热后
                # `load_ob_analysis` 命中缓存 → 不卡主线程。
                try:
                    import core.chart_cache as _cc2
                    _df = _cc2.load_daily_display(c)
                    if _df is not None and not _df.empty:
                        self._stock_tags_from_df(_df, c)
                except Exception:
                    pass
            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(_one, codes))
            # 2026-10-09：预热**持仓体检 30min 快照**（趋势/背离/顶部特征）→ OB 表零计算读取。
            # 只做**磁盘**预热（快、零网络）；**在线**刷新交给 `start_ob_m30_refresher`（启动即触发一次），
            # 避免此处与它重复各算一遍（`_build_m30_snapshot` ~300ms/只）。
            self._warm_ob_m30_disk(codes)
        except Exception:
            pass

    def _build_and_save_payloads(self, codes):
        """盘后预下载（小池）：逐只（低并发 3）建图并落盘 `chart_payload/`，供次日**瞬开**。
        已有新鲜 payload 则跳过。返回统计。失败静默。"""
        try:
            import pandas as pd
            import core.chart_cache as _cc
            from concurrent.futures import ThreadPoolExecutor
            codes = [str(c).split("_")[0] for c in (codes or [])]
            stat = {"requested": len(codes), "built": 0, "skipped": 0, "miss": 0}

            def _one(c):
                try:
                    if _cc.load_payload(c):
                        return "skip"
                    df = _cc.load_daily_display(c)
                    if df is None or df.empty:
                        from core.position_builder import fetch_daily_kline
                        df = fetch_daily_kline(c)
                    if df is None or df.empty:
                        return "miss"
                    try:
                        from core.market_data import get_provider
                        df = get_provider().append_forming_bar(df, c)
                    except Exception:
                        pass
                    df = df.copy()
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.sort_values("date").reset_index(drop=True)
                    rows = [{"date": str(_r.date), "open": float(_r.open), "close": float(_r.close),
                             "high": float(_r.high), "low": float(_r.low), "volume": float(_r.volume)}
                            for _r in df.itertuples(index=False)]
                    out = {"code": c, "name": c, "available": False, "error": ""}
                    res = self._build_chart_from_df(df, out, c)
                    if res.get("available") and _cc.save_payload(c, res, rows):
                        return "built"
                except Exception:
                    return "err"
                return "miss"

            with ThreadPoolExecutor(max_workers=3) as ex:
                for r in ex.map(_one, codes):
                    if r == "built":
                        stat["built"] += 1
                    elif r == "skip":
                        stat["skipped"] += 1
                    else:
                        stat["miss"] += 1
            return stat
        except Exception as e:
            return {"error": str(e)[:120]}

    def recompute_pb(self, date):
        """盘后重跑建仓扫描。返回 {position_builder, error?}。
        重跑用 eod 档、不推送飞书（避免重复打扰）；run_position_scan 会更新 watchlist_buy。
        fix 2026-08-27: 不再静默吞异常——失败记录并随返回带 error，前端可见。
        fix 2026-08-27(2): 重跑前热重载 config/timing_gate/position_builder——GUI 进程常驻，
        不重启时 import 缓存的是启动时的旧模块，"盘后重跑"会跑旧逻辑。重载顺序保证依赖新鲜：
        config → position_builder → timing_gate（timing_gate 顶部 from position_builder import）。
        重载失败回退普通 import（用当前内存模块），不阻断重跑。"""
        err = None
        # 1) 重跑建仓扫描（eod 档）
        try:
            import importlib
            try:
                import config as _cfg
                import core.position_builder as _pb_mod
                import core.timing_gate as _tg_mod
                importlib.reload(_cfg)
                importlib.reload(_pb_mod)
                importlib.reload(_tg_mod)
            except Exception as _re:
                print(f"⚠️ 模块热重载失败（回退当前内存模块）: {str(_re)[:120]}")
            from core.position_builder import run_position_scan
            run_position_scan(date_str=date, scan_type="eod", silent=True, no_feishu=True)
        except Exception as e:
            err = str(e)[:200]
            print(f"⚠️ 盘后重跑建仓扫描失败: {err}")
        # 2) 历史日：预算「30min 判定」列（读磁盘 30min 缓存截到该日，零网络）写入缓存，
        #    使刷新后的建仓表带出该列；今日走实时轻量缓存路径（_agg_position_builder），不在此算。
        #    同批预算「仓位结构」列（纯磁盘 5min/30min + 日线，零网络）。
        if date != datetime.now().strftime("%Y-%m-%d"):
            try:
                _rows = self._agg_position_builder(date).get("rows") or []
                for _r in _rows:
                    if _r.get("code"):
                        self._pb_m30_hist_verdict(_r["code"], date, compute=True)
                # 仓位结构**必须异步**：纯磁盘也算得慢（单只中位 0.72s、50 只 ≈55s）——
                # 原来同步跑在这里，把前端 20s 投递层上限打爆（2026-10-11 报「重跑失败: 超时」）。
                self._kick_pb_struct_hist_warm(
                    [_r.get("code") for _r in _rows if _r.get("code")], date)
            except Exception:
                pass
        # 3) 聚合新 trace（含技术标签）
        pb = self.refresh_pb(date)
        out = {"position_builder": pb}
        if err:
            out["error"] = err
        return _clean(out)

    # ---------- 内部聚合 ----------
    def _load_stage_board(self):
        sb = _load_json(OUT / "stage_board.json", {})
        return sb.get("stages", [])

    def _prev_day_score_map(self, date):
        """**上一个有 trace 的交易日**里每只票的综合得分 → {code: score}。

        2026-10-21 owner：建仓表「得分」旁要一个 ↑/↓，表示**相比昨天**在升还是在降。
        口径与表格里的「得分」同源：同一只票取该日 `scan_time` **最新**那条记录的
        `composite_score`（不是当日最高分快照 —— 那张是 `_eod_best_score` 的老口径）。

        性能：单日 trace 5~6MB / 2700+ 行，按 `(日期, mtime)` 缓存 ⇒ 上一交易日的文件当天
        不会再变，整个进程生命周期里最多解析一次；`refresh_pb` 每 10s 调一次也不会重读。
        """
        try:
            d0 = str(date)
            files = sorted(TRACES.glob("position_builder_*.jsonl"),
                           key=lambda p: p.stem.rsplit("_", 1)[-1])
            prev = None
            for fp in files:
                d = fp.stem.rsplit("_", 1)[-1]
                if d < d0:
                    prev = (d, fp)          # 取"最近的一个更早日期"（跳过周末/节假日）
            if prev is None:
                return {}
            _d, _fp = prev
            try:
                _mt = _fp.stat().st_mtime
            except Exception:
                return {}
            hit = _PREV_SCORE_CACHE.get(_d)
            if hit and hit[0] == _mt:
                return hit[1]
            out, _last_t = {}, {}
            for line in _fp.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                c = r.get("code")
                if not c:
                    continue
                t = r.get("scan_time") or ""
                if c not in _last_t or t > _last_t[c]:
                    _last_t[c] = t
                    out[c] = r.get("composite_score")
            if len(_PREV_SCORE_CACHE) >= 3:
                _PREV_SCORE_CACHE.clear()          # 只留最近几天，别无限长
            _PREV_SCORE_CACHE[_d] = (_mt, out)
            return out
        except Exception:
            return {}

    # 2026-10-24 owner：「建仓信号扫描 站上5日线该绿、破了该红，现在全是白的」。
    # 根因：整行底色读的是 trace 里的 `timing_features.above_ma5`，而**历史日/旧 trace 根本没有这个字段**
    # （4月8日那天 49 行全是 None）⇒ 不着色。这里按**该日期**的本地日线补齐，带 (码,日期,mtime) 缓存。
    _PB_TF_CACHE: dict = {}

    @classmethod
    def _backfill_timing_features(cls, rows, date):
        """给缺 `above_ma5` 的行补上（用 ≤ 该日的本地日线收盘算 MA5/MA20）。"""
        d = str(date)[:10]
        for row in rows:
            if not isinstance(row, dict):
                continue
            tf = row.get("timing_features")
            if isinstance(tf, dict) and tf.get("above_ma5") is not None:
                continue
            code = str(row.get("code") or "").split("_")[0]
            if not code:
                continue
            try:
                fp = _DAILY_KLINE_DIR / f"{code}.json"
                mt = fp.stat().st_mtime_ns
                ck = (code, d, mt)
                hit = cls._PB_TF_CACHE.get(ck)
                if hit is None:
                    rs = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
                    rs = [r for r in rs if str(r.get("date"))[:10] <= d]
                    if len(rs) < 20:
                        continue
                    cs = [float(r.get("close") or 0) for r in rs]
                    cur = cs[-1]
                    if not (cur > 0):
                        continue
                    hit = (bool(cur > sum(cs[-5:]) / 5.0),
                           bool(cur > sum(cs[-20:]) / 20.0) if len(cs) >= 20 else None)
                    if len(cls._PB_TF_CACHE) > 4000:
                        cls._PB_TF_CACHE.clear()
                    cls._PB_TF_CACHE[ck] = hit
                tf = dict(tf) if isinstance(tf, dict) else {}
                if tf.get("above_ma5") is None:
                    tf["above_ma5"] = hit[0]
                if tf.get("above_ma20") is None:
                    tf["above_ma20"] = hit[1]
                row["timing_features"] = tf
            except Exception:
                continue

    def _agg_position_builder(self, date, filter_high_confidence=False):
        fp = TRACES / f"position_builder_{date}.jsonl"
        wl = _load_json(STATE_DIR / "watchlist_buy.json", {})
        wl_stocks = wl.get("stocks", {})
        holdings = _load_json(HOLDINGS_MANUAL, {})
        empty = {"has_data": True, "counts": {}, "by_code": {}, "rows": [],
                 "cond_labels": COND_LABELS, "note": "", "progress": {}}

        verdicts = Counter()
        by_code = {}
        scanned_codes = set()
        latest_by_code = {}  # fix P0-13/P0-14: 每 code 最新一条扫描记录
        if fp.exists():
            try:
                lines = open(fp, encoding="utf-8").read().splitlines()
            except Exception:
                lines = []
            for line in lines:
                line = line.strip()
                if not line: continue
                try: r = json.loads(line)
                except Exception: continue
                code = r.get("code")
                if not code: continue
                scanned_codes.add(code)
                # fix P0-14: 跟踪每 code 最新记录（按 scan_time）
                _prev = latest_by_code.get(code)
                if _prev is None or (r.get("scan_time") or "") > (_prev.get("scan_time") or ""):
                    latest_by_code[code] = r
                st = r.get("scan_type", "manual")
                bucket = by_code.setdefault(code, {}).setdefault(
                    st, {"latest": None, "best": None, "scans": 0})
                bucket["scans"] += 1
                score = r.get("composite_score") or 0
                if bucket["latest"] is None or (r.get("scan_time") or "") > (
                    bucket["latest"].get("scan_time") or ""):
                    bucket["latest"] = r
                if bucket["best"] is None or score > (bucket["best"].get("composite_score") or 0):
                    bucket["best"] = r

        # fix P0-13: counts 按 code 去重后统计各 code 最新 verdict 的股票数（不再是扫描记录行数）
        # fix 2026-08-25: 已从股池删除的（不在 watchlist 且非持仓）不再展示 → 删除按钮后 refresh_pb 不再"复活"
        def _is_holding(code: str) -> bool:
            return bool((holdings.get(code) or {}).get("qty") or 0) or \
                bool((holdings.get(code.split("_")[0]) or {}).get("qty") or 0)
        # 建仓表可见性（**2026-10-17 owner 再次调整：持仓股一律不出现**）
        #
        # 沿革：2026-08-30 曾用 auto_pool.is_manual 隐藏 auto 标的 → 导致「添加成功却看不到」
        #       （600584）；2026-09-20 改成「持仓股 + 股池里的都显示」。
        # 现在 owner 要求：**已有底仓的票不该出现在"建仓候选"里** —— 它属于「持仓体检」，
        # 列在这里会让人误以为还要再建一次（同一个代码同时出现在两张表里也确实矛盾）。
        # 手动盘 / 自动盘**任一** qty>0 都算持仓（自动盘目前是仿真，但口径一致）。
        _auto_hold = set()
        try:
            from src.holdings_repo import load_auto as _load_auto
            _auto_hold = {str(c) for c, h in (_load_auto() or {}).items()
                          if int((h or {}).get("qty") or 0) > 0}
        except Exception:
            _auto_hold = set()

        def _held(code: str) -> bool:
            c0 = str(code).split("_")[0]
            return _is_holding(code) or c0 in _auto_hold or str(code) in _auto_hold

        def _visible(code: str) -> bool:
            # 必须还在股池里（watchlist_buy），且**不是持仓**
            return (code in wl_stocks) and not _held(code)

        for code, r in latest_by_code.items():
            if not _visible(code):
                continue
            verdicts[r.get("verdict", "")] += 1

        # 扫描过的：正常聚合
        # 2026-10-21：上一交易日的得分（前端在得分旁画 ↑/↓）。取一次即可，函数内部按日缓存。
        _prev_scores = self._prev_day_score_map(date)
        rows = []
        for code, rec in by_code.items():
            if not _visible(code):
                continue
            # fix P0-14: 行选择改为最新一条扫描记录（不再取当日最高分快照）
            eod = (rec.get("eod") or {}).get("latest")
            intraday = (rec.get("intraday") or {}).get("latest")
            row = dict(latest_by_code.get(code) or eod or intraday or {})
            row["_eod_best_score"] = (eod or {}).get("composite_score")
            row["_intraday_best_score"] = (intraday or {}).get("composite_score")
            row["_scans"] = sum(v.get("scans", 0) for v in rec.values())
            row.setdefault("scan_time", "")  # fix P0-14: 每行确保带 scan_time 字段
            # 上一个有 trace 的交易日得分（None = 昨日没扫过这只票 ⇒ 前端不画箭头，别画成"持平"）
            _p = _prev_scores.get(code)
            if isinstance(_p, str):           # 旧 trace 里偶有字符串分数
                try:
                    _p = float(_p)
                except ValueError:
                    _p = None
            row["score_prev"] = _p if isinstance(_p, (int, float)) and not isinstance(_p, bool) else None
            # fix 2026-08-20: in_holdings 实时对齐 holdings.json（qty>0 才算持仓，trace/watchlist 字段可能陈旧）
            row["in_holdings"] = _is_holding(code)
            # P3-2 池分管：pool 标注（优先 holdings 权威源，回退 watchlist，缺省 manual），供 GUI 池筛选
            row["pool"] = (holdings.get(code) or {}).get("pool") or \
                (wl_stocks.get(code) or {}).get("pool") or "manual"
            rows.append(row)

        # 未扫描的 watchlist 股票：monitoring/signal → "等待扫描"；archived → "已停用"（可见但不参与扫描）
        pending = 0
        archived_cnt = 0
        for code, info in wl_stocks.items():
            if not isinstance(info, dict): continue
            if not _visible(code): continue   # fix 2026-08-30: auto 池且未持有 → 隐藏（属自动盘 tab）
            if code in scanned_codes: continue
            status = info.get("status")
            if status not in ("monitoring", "signal", "archived", None): continue
            # fix 2026-08-20: qty>0 才算持仓（已清仓的 holdings 记录不算）
            in_hold = bool((holdings.get(code) or {}).get("qty") or 0) or \
                bool((holdings.get(code.split("_")[0]) or {}).get("qty") or 0)
            is_archived = status == "archived"
            rows.append({
                "code": code, "name": info.get("name", code),
                "verdict": "archived" if is_archived else "pending",
                "composite_score": 0,
                "conditions": {},
                "suggested_qty": 0, "suggested_price": 0, "capital_required": 0,
                "in_holdings": in_hold,
                "pool": (holdings.get(code) or {}).get("pool") or info.get("pool") or "manual",  # P3-2 池分管：GUI 池筛选（优先 holdings）
                "scan_type": "已停用" if is_archived else (
                    "自动盘(不手动扫)" if info.get("pool") == "auto" else "等待扫描"),
                "scan_time": "",  # fix P0-14: 每行确保带 scan_time 字段
                "_scans": 0,
            })
            if is_archived: archived_cnt += 1
            else: pending += 1

        # 2026-10-24 owner：「建仓表站上5日线该绿、破了该红，现在全是白的」。
        # 两个原因都要补：① 历史日/旧 trace 没有 `timing_features`；② 那天**根本没扫过**
        # （4月8日 traces 里没有 position_builder 文件 ⇒ 49 行全是"等待扫描"行，天然没有特征）。
        # 统一用**该日期的本地日线**补 above_ma5/above_ma20（带 mtime 缓存，49 行毫秒级）。
        try:
            self._backfill_timing_features(rows, date)
        except Exception:
            pass

        rows.sort(key=lambda x: -(x.get("composite_score") or 0))
        verdicts["pending"] = pending
        verdicts["archived"] = archived_cnt
        no_data_count = verdicts.get("insufficient_data", 0)
        note_parts = []
        if no_data_count: note_parts.append(f"{no_data_count}只无快照")
        if pending: note_parts.append(f"{pending}只等待首次扫描")
        if archived_cnt: note_parts.append(f"{archived_cnt}只已停用")
        note = " · ".join(note_parts) if note_parts else ""

        total = len([c for c in wl_stocks if _visible(c)])  # fix 2026-08-30: 分母=手动盘可见候选数（auto 池未持有已隐藏）
        progress = {
            "total_candidates": total,
            "scanned": len(scanned_codes),
            # fix P0-13: online_fetched 字段名保留，语义改为"当日已扫描股票数"（前端改标签）
            "online_fetched": len(scanned_codes),
            "no_data": no_data_count,
            "pending": pending,
        }
        # 技术标签：仅当天附加（当天日线缓存新鲜，批量秒回；历史日缓存过期会走网络，跳过避免拖慢）
        if date == datetime.now().strftime("%Y-%m-%d"):
            try:
                codes = [r.get("code") for r in rows if r.get("code")]
                if codes:
                    tag_res = self.load_stock_tags_batch(codes)
                    tags_map = tag_res.get("tags", {})
                    for r in rows:
                        info = tags_map.get(r.get("code"), {})
                        if info:
                            r["tags"] = info.get("tags", [])
                            r["trend"] = info.get("trend")
            except Exception:
                pass

        # 持仓股背离兜底（2026-09-20 owner 要求"持仓股的背离也要显示"）：
        # 自动池由 _gm 侧扫描，那条路径**不产出背离**（_gm/signals/position_builder.py
        # 无背离逻辑，且其 docstring 声明"纯函数，无 IO"，不该塞网络调用）⇒ 自动池持仓
        # 的背离列恒为"—"。这里对**持仓且无背离数据**的行补算一次。
        # 与上面的技术标签同策略：仅当天、逐只 try、失败静默。
        # 成本受控：只补持仓（~17 只）× 每 (票,周期) 当日文件缓存 ⇒ 首次刷新后基本无网络。
        if date == datetime.now().strftime("%Y-%m-%d"):
            _need = [r for r in rows
                     if r.get("code") and r.get("in_holdings") and not r.get("divergence_detail")]
            if _need:
                _memo = getattr(self, "_div_memo", None)
                if _memo is None:
                    _memo = self._div_memo = {}
                try:
                    from analysis.divergence import detect_minute_divergence_detail as _det_div
                    for r in _need:
                        _c = r["code"]
                        if _c not in _memo:
                            try:
                                _memo[_c] = _det_div(_c) or {}
                            except Exception:
                                _memo[_c] = {}
                        _d = _memo[_c]
                        if _d:
                            r["divergence_detail"] = _d
                            r["divergence"] = {k: v["type"] for k, v in _d.items()}
                except Exception:
                    pass

        # 建仓表「30min 判定」列（2026-10-09）：与体检表同口径，主线程**只读轻量缓存、零计算**；
        # 未热 → 该行显示「计算中」，同时后台预热整池（每 30min 时段一次，~0.9s）。
        # 历史日（2026-10-10）：读「扫描该日」时预存的历史判定（`_PB_M30_HIST`）；**不在浏览历史时现算**
        # （load_day 会频繁切日，现算会拖慢；历史日判定由 recompute_pb 预算好）。
        _pb_m30_pending = False
        _is_today = (date == datetime.now().strftime("%Y-%m-%d"))
        if _is_today:
            _codes = [r.get("code") for r in rows if r.get("code")]
            for r in rows:
                if not r.get("code"):
                    continue
                _v = self._m30_verdict_pb(r["code"])
                r["m30_verdict"] = _v
                if _v.get("level") == "pending":
                    _pb_m30_pending = True
            if _pb_m30_pending and _codes:
                self._kick_pb_m30_warm(_codes)
        else:
            for r in rows:
                if not r.get("code"):
                    continue
                _v = self._pb_m30_hist_verdict(r["code"], date, compute=False)
                if _v:
                    r["m30_verdict"] = _v

        # 建仓表「仓位结构」列（2026-10-11）：30min/10min 结构参考（止损位/阻力/补仓档/阶段），
        # 供**仓位管理**用。与 30min 判定列同纪律：主线程只读缓存；未热 → 后台预热整池。
        # ⚠️ 不是买卖信号（见模块纪律）；历史日走纯磁盘 `_pb_struct_hist`。
        _pb_struct_pending = False
        if _is_today:
            _scodes = [r.get("code") for r in rows if r.get("code")]
            for r in rows:
                if not r.get("code"):
                    continue
                _s = self._pb_struct_get(r["code"])
                r["structure"] = _s
                if _s.get("pending"):
                    _pb_struct_pending = True
            if _pb_struct_pending and _scodes:
                self._kick_pb_struct_warm(_scodes)
        else:
            for r in rows:
                if not r.get("code"):
                    continue
                _s = self._pb_struct_hist(r["code"], date, compute=False)
                if _s:
                    r["structure"] = _s

        _out = {
            "has_data": True,
            "counts": dict(verdicts),
            "by_code": by_code,
            "rows": self._filter_high_confidence_signals(rows) if filter_high_confidence else rows,
            "cond_labels": COND_LABELS,
            "note": note,
            "progress": progress,
        }
        if _pb_m30_pending:
            _out["pb_m30_pending"] = True
        return _out

    def _filter_high_confidence_signals(self, rows: list) -> list:
        """过滤仅保留有连续背离的信号。
        规则：必须有任何连续背离（m30 或 m60 的 consec=true），verdict 无限制。"""
        filtered = []
        for row in rows:
            div_detail = row.get("divergence_detail") or {}
            m60 = div_detail.get("m60", {})
            m30 = div_detail.get("m30", {})

            # 唯一条件：必须有连续背离
            has_consecutive_divergence = m60.get("consec") or m30.get("consec")

            if has_consecutive_divergence:
                # 计算优先级：60分钟连续底背离最优
                priority = 0
                if m60.get("type") == "底背离" and m60.get("consec"):
                    priority = 100
                elif m60.get("type") == "顶背离" and m60.get("consec"):
                    priority = 90
                elif m30.get("type") == "底背离" and m30.get("consec"):
                    priority = 80
                elif m30.get("type") == "顶背离" and m30.get("consec"):
                    priority = 70
                else:
                    priority = 50

                row["_priority"] = priority
                row["_divergence_summary"] = self._format_divergence_summary(div_detail)
                filtered.append(row)

        # 按优先级和 score 排序
        filtered.sort(key=lambda x: (
            -(x.get("_priority") or 0),
            -(x.get("composite_score") or 0)
        ))
        return filtered


    def _format_divergence_summary(self, div_detail: dict) -> str:
        """格式化背离简述，供前端显示"""
        if not div_detail:
            return ""
        parts = []
        for key in ("m30", "m60"):
            v = div_detail.get(key)
            if v:
                consec_mark = "✓" if v.get("consec") else ""
                parts.append(f"{key}:{v.get('type', '')}{consec_mark}")
        return " | ".join(parts) if parts else ""

    def _load_positions(self, date, kpi):
        current = _load_json(HOLDINGS_MANUAL, {})
        snap_today = {}
        snap_prev = {}
        prev_date = None

        # 2026-08-30: 旧格式 holdings_{date}.json 快照已清理删除，改读 GUI 每日快照
        # holdings_daily_{date}.json（{"holdings": [...]} 列表）→ 转 code 键 dict（下游口径不变）
        def _daily_to_map(snap):
            rows = snap.get("holdings") if isinstance(snap, dict) else None
            if isinstance(rows, list):
                return {r.get("code"): r for r in rows if isinstance(r, dict) and r.get("code")}
            return snap if isinstance(snap, dict) else {}

        fps = sorted(STATE_DIR.glob("holdings_daily_*.json"))
        for fp in fps:
            d = fp.stem.replace("holdings_daily_", "")
            if d == date:
                snap_today = _daily_to_map(_load_json(fp, {}))
            if d < date and (prev_date is None or d > prev_date):
                prev_date = d
        if prev_date:
            snap_prev = _daily_to_map(_load_json(STATE_DIR / f"holdings_daily_{prev_date}.json", {}))

        # 2026-09-14: manual 做T 下线——t_mode.json（正/反T）已随 manual 做T 删除，不再读入/返回。

        # 从独立配置文件读（不再依赖 holdings.json）
        pcfg = _load_json(PORTFOLIO, {})
        accounts = pcfg.get("accounts", {})
        return {
            "current": current,
            "accounts": accounts,
            "snapshot_today": snap_today,
            "snapshot_prev": snap_prev,
            "prev_date": prev_date,
            "k2": (kpi or {}).get("K2_cost_change", {}),
            "k3": (kpi or {}).get("K3_base_drift", {}),
        }


def start_chart_prefetch_scheduler(api):
    """盘后 K线预下载调度（2026-10-04）：交易日 15:10 后触发一次 `chart_cache.run_prefetch`。

    小池日线+分钟+payload → 全池日线兜底。幂等由 `run_prefetch` 的跨进程锁 + 当日标记保证
    （本循环可反复调用，已跑过即空转）。守护线程，失败静默。仅 __main__ 显式启动。"""
    if _CHART_PREFETCH_STATE.get("started"):
        return
    _CHART_PREFETCH_STATE["started"] = True

    def _loop():
        while True:
            try:
                import core.chart_cache as _cc
                now = datetime.now()
                if now.weekday() < 5 and now.strftime("%H:%M") >= "15:10":
                    _cc.run_prefetch(payload_builder=lambda codes: api._build_and_save_payloads(codes))
            except Exception:
                pass
            _time_mod.sleep(300)

    _th.Thread(target=_loop, daemon=True).start()


def start_ob_m30_refresher(api):
    """持仓体检 30min 快照刷新（2026-10-09）：每 60s 一跳，跨 30 分钟时段即后台重算持仓快照
    （`refresh_ob_m30` 走在线取数，off 主线程），使 OB 表的 30min 趋势/背离/顶部特征保持当根新鲜。
    守护线程，失败静默。仅 __main__ 显式启动。"""
    if _OB_M30_STATE.get("started"):
        return
    _OB_M30_STATE["started"] = True

    def _loop():
        _last_slot = None
        while True:
            try:
                slot = _min_bars_slot()
                if slot != _last_slot:
                    _last_slot = slot
                    # 周末/盘后不出网（见 refresh_ob_m30 内注释）；磁盘预热由面板按需触发
                    if _in_trading_session():
                        api.refresh_ob_m30()
            except Exception:
                pass
            _time_mod.sleep(60)

    _th.Thread(target=_loop, daemon=True, name="ob-m30-refresher").start()


def _start_gui_freeze_watchdog(threshold_s=12.0):
    """界面卡死看门狗（2026-10-08 需求①）：js_api 全在 pywebview 主线程串行执行，任何长阻塞都会冻界面。
    高频轮询（`load_console` 2s / `load_live` 10s）每次给 `_GUI_HB` 打点；本守护线程发现心跳停
    >threshold_s 秒即判「卡死」，把**全部线程栈**追加到 `t_io/logs/gui_freeze.log`
    （含主线程当前卡在哪一帧），供后续持续改进。单次卡死最多 30s 记一次，避免刷屏。

    另（2026-10-14）：**按需落栈** —— 建一个哨兵文件（默认路径见 `_DUMP_REQ`）等 ≤2s 即落一份
    全线程栈并自动删掉哨兵。用途：CPU 高但**没卡死**时也想知道"到底哪条线程在烧"——那种情况下
    上面的心跳判据永远不会触发（心跳一直被续着），此前只能干瞪眼。哨兵路径可用环境变量
    `TRADER_DUMP_REQ` 覆盖。"""
    import faulthandler
    import os as _os
    log_fp = BASE / "t_io" / "logs" / "gui_freeze.log"
    _DUMP_REQ = Path(_os.environ.get("TRADER_DUMP_REQ")
                     or (BASE / "t_io" / "state" / "gui_dump_request"))

    def _dump(head, tail=""):
        """落一份全线程栈。header 格式 = `===== {head} {时间}{tail} 线程栈 =====`。

        ⚠️ **时间必须在 head 之后、尾巴之前** —— 这是 `gui_freeze_regression_20261011.py`
        解析卡死段所依赖的固定形状。2026-10-14 我为加哨兵把 header 重排成"心跳停在前、
        时间在后"，**当天下午起的所有卡死段对看板直接不可见**（看板仍报 39 段、看着正常）
        —— 静默失效，正是本仓最忌讳的那类。此处恢复原形状，解析侧同时放宽成"两种顺序都收"。
        """
        try:
            log_fp.parent.mkdir(parents=True, exist_ok=True)
            with open(log_fp, "a", encoding="utf-8") as f:
                f.write(f"\n===== {head} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        f"{tail} 线程栈 =====\n")
                faulthandler.dump_traceback(file=f, all_threads=True)
        except Exception:
            pass

    def _loop():
        _last = 0.0
        while True:
            try:
                _time_mod.sleep(2.0)
                # 按需落栈（哨兵文件；用完即删，天然幂等）
                try:
                    if _DUMP_REQ.exists():
                        _DUMP_REQ.unlink()
                        _dump("按需线程栈（哨兵触发，未卡死）")
                except Exception:
                    pass
                _ts = _GUI_HB.get("ts") or 0.0
                _now = _time_mod.time()
                if _ts and (_now - _ts) > threshold_s and (_now - _last) > 30:
                    _last = _now
                    _dump("疑似界面卡死", f"（主线程心跳停 {_now - _ts:.0f}s）")
            except Exception:
                pass

    _th.Thread(target=_loop, name="gui-freeze-watchdog", daemon=True).start()


def _start_ctrlc_abort_watchdog(grace_s=2.5, poll_s=0.4):
    """Ctrl+C 必须能**连带关掉 GUI**（owner 2026-10-10）。

    为什么 pywebview 自带的 SIGINT 不足——**两条路都可能断**：
    1) 它的退出路径是「信号处理器置 `_sigint_received` → **GUI 线程的 WinForms Timer 每 500ms
       检查** → `app.Exit()`」。而 `WM_TIMER` 是**最低优先级**的消息，只在消息队列空时才合成；
       界面被大量 js_api 回调/WebView2 消息堵住时（本仓实测过 18+ 并发在飞）它迟迟不触发
       ⇒ Ctrl+C 看起来毫无反应、窗口赖着不走。
    2) 即使优雅路径走通、`app.Run()` 已返回，python 退出时 atexit 会 **join 非守护的
       ThreadPoolExecutor worker**，而 GM 挂死的调用**不可取消**（见 core/market_data/facade.py）
       ⇒ 进程卡在退出上（实测：卡死的非守护 worker 让主流程已跑完的进程 12s 都退不出去，
       见 tmp/probe_exit_hang.py）。

    故这里**不依赖 UI 线程是否响应**：只轮询 pywebview 自己的标志（读一个模块全局，不改它），
    发现收到 Ctrl+C 后走优雅路径；宽限期一过还没死就 `os._exit` 强杀——窗口随进程一起消失，不留孤儿。
    优雅路径能在宽限期内完成时，本守护线程根本没机会动手。
    """
    import os as _os

    def _loop():
        try:
            from webview.platforms import winforms as _wfw
        except Exception:
            return
        seen_at = None
        while True:
            _time_mod.sleep(poll_s)
            try:
                got = bool(getattr(_wfw, "_sigint_received", False))
            except Exception:
                got = False
            if not got:
                continue
            if seen_at is None:
                seen_at = _time_mod.time()
                print("[Ctrl+C] 正在关闭界面…（宽限 %.0fs，超时强制终止）" % grace_s, flush=True)
            elif (_time_mod.time() - seen_at) > grace_s:
                print("[Ctrl+C] 界面未在宽限期内退出 → 强制终止进程", flush=True)
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                _os._exit(0)

    _th.Thread(target=_loop, name="ctrlc-abort", daemon=True).start()


if __name__ == "__main__":
    import webview

    # GIL 切换间隔（2026-10-10）：默认 5ms。启动时 3 个预热线程与 pywebview 主线程抢 GIL，
    # 主线程的 js_api 调用被拖成数秒（实测 kpi_trend 读 10 个小 JSON 从 0.01s → 9.4s）。
    # 调到 1ms ⇒ 主线程更频繁拿到 GIL，实测同序列 39s → 12s。代价是轻微上下文切换开销。
    try:
        sys.setswitchinterval(0.001)
    except Exception:
        pass

    # V8 预热(2026-09-08): akshare 的 py_mini_racer(V8) 首次初始化必须在主线程，见 core/v8guard.py
    try:
        from core.v8guard import prewarm_akshare_v8
        prewarm_akshare_v8()
    except Exception:
        pass

    api = Api()
    # 冷启动 import 预热（2026-10-10）：首个 load_day 会懒加载 config/position_builder/timing_gate/
    # pandas/akshare（合计 ~5s，会冻主线程）。提前在后台线程 import，摊到窗口创建那几秒里。
    def _prewarm_imports():
        for _m in ("config", "core.position_builder", "core.timing_gate", "core.chart_cache",
                   "core.market_data", "analysis.indicators", "analysis.m30_features",
                   "analysis.divergence", "analysis.trend30.adapter"):
            try:
                __import__(_m)
            except Exception:
                pass
    _th.Thread(target=_prewarm_imports, name="import-prewarm", daemon=True).start()

    # GM 健康探测（2026-10-10）：放在窗口创建**之前**，让前端加载页面那 1-2s 与探测重叠。
    # 为什么需要：facade 的 GM 熔断是「失败后才置冷却窗」，而首次失败要付满 4s。若等到前端首帧
    # 扇出时才发现 GM 不可达，几十个 js_api 线程会挤在那个**单 worker** 的 GM 池上排队（见
    # core/market_data/facade.py 的半开探针注释）。这里提前探一次：不可达 ⇒ 首帧扇出时
    # `_gm_down_until` 已置好，整波直接走腾讯。
    def _prewarm_gm_probe():
        try:
            from core.market_data.facade import get_provider
            prov = get_provider()
            if not prov._gm_ready():
                return
            _t = datetime.now().strftime("%Y-%m-%d")
            df = prov.index_daily("sh000001", days=2, end_date=_t)   # 走 _gm_call 的 4s 硬超时
            if df is None or df.empty:
                prov._note_gm_down("startup-probe", "sh000001",
                                   RuntimeError("启动探测：指数日线为空"))
        except Exception:
            pass
    _th.Thread(target=_prewarm_gm_probe, name="gm-health-probe", daemon=True).start()

    # 2026-10-21：进程内**线程级** CPU 归因（外部采样拿不到别的进程的线程名，见 core/thread_watch.py）。
    # 每 5s 读一次、每 60s 落一行到 t_io/logs/thread_cpu.jsonl；只读、纯本地 syscall。
    # ⚠️ 2026-10-21 修：这段原本写的是 `PARAMS.get(...)` 与 `log.info/warning` —— 那两个名字
    # 在 t_gui.py 里**都不存在**（本文件没有模块级 `PARAMS`/`log`，配置在 `config.PARAMS`）。
    # 结果：`try` 里 NameError → 被 `except` 接住 → `log.warning` 再抛 NameError → **GUI 秒退**，
    # 而 main.py 拉起子进程时 stdout/stderr 都是 DEVNULL（见 main._launch_gui）⇒ 窗口不出现、
    # 也没有任何提示。此文件的日志口径是 `print(..., flush=True)`（同 Ctrl+C 看门狗）。
    try:
        import config as _cfg              # 本文件没有模块级 config（各处都是函数内 import）
        from core import thread_watch as _tw
        _tw_sec = float(_cfg.PARAMS.get("thread_watch_sec", 5) or 5)
        if _tw.start(interval=_tw_sec):
            print(f"🧵 线程级 CPU 归因已启动（{_tw_sec:g}s 采样 / 60s 落盘 → "
                  f"t_io/logs/thread_cpu.jsonl）", flush=True)
    except Exception as _e:
        print(f"⚠️ 线程级 CPU 归因启动失败（不阻断）: {str(_e)[:120]}", flush=True)

    # 2026-10-10: 后端「猎手定时自动运行」已停（owner 需求6，扫描一律按钮触发）。
    _start_gui_freeze_watchdog()      # 卡死看门狗：心跳停 >12s → 落线程栈到 t_io/logs/gui_freeze.log
    _start_ctrlc_abort_watchdog()     # Ctrl+C 连带关掉 GUI（不依赖 UI 线程响应，见其 docstring）
    start_chart_prefetch_scheduler(api)   # 盘后 15:10 预下载 K线缓存（小池 payload+分钟、全池日线）
    start_ob_m30_refresher(api)           # 持仓体检 30min 快照：跨时段后台刷新（趋势/背离/顶部特征）
    # 启动预热（均为后台 daemon 线程，不阻塞启动；失败静默）：
    #  · 持仓图表：低并发(3)，消除 load_ob_analysis 的 14s 冷启动阻塞
    #  · 技术标签：走其内置后台分支，消除首次进建仓表/破位表的标签冷算等待
    #
    # **2026-10-21：先「单线程付一次冷启动」，再并行。**
    # 为什么（`tmp/bench_boot*.py` 实测，同一台机器）：
    #   · 三路预热 + 用户开图**同时**起 ⇒ 首次开图 **20.0s**，其中
    #     `_rebuild_payload_daily` 16s、`_today_forming_bar` 8.5s——而单独跑只要 0.07s / 0.3s；
    #   · 先让**一个线程**完整跑一遍 `load_stock_chart`（5.0s，把 import 链 / provider 单例 /
    #     pandas 首次调用全付掉），再起三路并行 ⇒ 同样场景下开图 **1.37s**。
    # 根因不是"后台太多"，而是**冷启动的懒加载在多线程里互相抢 import 锁 + 重复构造 provider**。
    # 所以把并发的起点挪到"冷启动已付完"之后。
    def _boot_warmup():
        # 先等前端首帧打第一个 js_api（`available_dates`）再开跑：宁可预热晚 1~2 秒，
        # 也不要和首帧抢 GIL/import 锁（见 `_UI_SEEN` 注释）。
        _UI_SEEN.wait(20)
        try:
            _cur = _load_json(HOLDINGS_MANUAL, {})
            _first = next((str(_c).split("_")[0] for _c, _i in (_cur or {}).items()
                           if isinstance(_i, dict) and not str(_c).startswith("_")
                           and (_i.get("qty") or 0)), None)
            if _first:
                api.load_stock_chart(_first)      # 冷启动一次性：import + provider + 建图全链
        except Exception:
            pass
        for _fn in (api.prewarm_holdings_charts, api.prewarm_stock_tags, api.prewarm_overview):
            _th.Thread(target=_fn, daemon=True).start()

    _th.Thread(target=_boot_warmup, daemon=True, name="boot-warmup").start()

    def _prewarm_chart_build():
        """建图冷启动预热（2026-10-06）：首次 `_build_chart_from_df` 含懒加载 import
        （analysis.indicators.wilder_rsi / pandas-numba 首次计算）≈1.9s，预热后降到 ~0.5s。
        后台线程，失败静默。"""
        try:
            import pandas as pd
            # 2026-10-20：K 线背离标注的冷导入（pandas + analysis.divergence ≈1.5s）
            # 必须在这里预热掉，否则**第一次打开任意 K 线**都会额外卡 1.5s。
            from analysis import divergence as _dv   # noqa: F401
            _d = pd.DataFrame({
                "date": pd.to_datetime(["2020-01-01"] * 400),
                "open": [1.0] * 400, "high": [1.1] * 400, "low": [0.9] * 400,
                "close": [1.0] * 400, "volume": [1.0] * 400,
            })
            _calc_ma_and_indicators(_d)
        except Exception:
            pass

    _th.Thread(target=_prewarm_chart_build, daemon=True).start()
    here = Path(__file__).parent
    entry = here / "web" / "index.html"

    # 前端开发调试: 设置 WEBVIEW_DEBUG=1 打开 devtools
    debug = sys.argv[1] == "--debug" if len(sys.argv) > 1 else False

    window = webview.create_window(
        "trader pannel",
        str(entry),
        js_api=api,
        width=1440,
        height=920,
        min_size=(1100, 700),
    )
    webview.start(gui="edgechromium", debug=debug)
