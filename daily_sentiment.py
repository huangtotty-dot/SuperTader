# -*- coding: utf-8 -*-
"""
daily_sentiment.py — V3.0 大盘热度 × 韭研TOP3 每日热度推送与做T策略决策模块
================================================================================

职责：
  1. 每日 14:30（盘中 mode="tail"，含 forming bar，estimate）合成：
       大盘态势（index_regime：regime / 综合分 S / K-day / E5 跌停 / 持续天数）
       + 韭研TOP3 板块热度（复用 E:\\04_实战资料\\report_gen 的 ConceptScorer +
         MarketDataFetcher + watchlist_jiuyan.json，板块平均分取 TOP3 均值）
       + z 归一化（z_S / z_top3，60 日滚动，窗口不足回退 108 日常量）
       + 决策矩阵 t_decision（正T/反T/不做T + 仓位系数 + 理由）
       + 系统性风险预警（z_S≤-1.5 且 [E5 跌停潮 或 指数跌≥2%]）
  2. 飞书合成卡片推送 push_daily_sentiment（后台线程，避免阻塞主扫描循环）。
  3. 落盘 <log_dir>/sentiment_daily.jsonl（逐日 append）+ sentiment_daily.csv（同日覆盖）。
  4. 可独立运行：
       python daily_sentiment.py [--date YYYY-MM-DD] [--mode tail|eod|morning] [--no-push]

双模运行：
  - 宿主模式：main.py 以 exec 方式载入（module_order 末尾），直接使用共享命名空间中的
    detect_index_regime / send_feishu_payload / _feishu_md_div / _feishu_card_header /
    _feishu_hr / _append_jsonl / log / _now / HOLDINGS / FEISHU_KEYWORD / SENTIMENT_PARAMS /
    BASE_DIR / index_regime_name。
  - 独立模式：自动加载 E:\\06_T\\index_regime.py（importlib 按文件路径），自建 log/_now/
    feishu 辅助函数；--no-push 时不读取 webhook、不发送任何网络推送。

打分口径（与 workspace\\jiuyan_backtest\\v2\\run_backtest_v2.py 完全一致）：
  池 = watchlist_jiuyan.json 中 韭研概念 非空的行（多分类股拆多行）；
  行情 = MarketDataFetcher.fetch_for_date(codes, date)（腾讯 fqkline 150 日历史 +
         当日 forming bar，含 近5日涨幅/近5,10,20,150日最高/首板涨停/连板天数/
         一字板涨停/近10日涨停/成交额 等派生字段）；
  打分 = ConceptScorer（config.json scoring.dimensions 全 9 维）；
  板块平均分 = 该 韭研分类 下全部行 总得分 的简单均值（注意：非成交额加权，
         历史 108 日回测口径即简单均值，归一化常量均基于此口径校准）；
  top3_avg = 板块平均分降序前 3 名的均值；top3_names = 前 3 名板块名。

归一化（108 日窗常量，生产 60 日滚动、窗口不足回退常量）：
  z_S    = (S - (-4.41)) / 31.82
  z_top3 = (top3_avg - 5.05) / 2.34
"""

import argparse
import csv
import importlib.util
import json
import os
import statistics
import sys
import threading
import time as _time_mod
import urllib.request
from datetime import datetime, timedelta, time as dtime
from typing import Any, Dict, List, Optional, Tuple

# ============================================================================
# 宿主 / 独立 双模兼容层
# ============================================================================

_IN_HOST = callable(globals().get("detect_index_regime"))

if _IN_HOST:
    _BASE_DIR = globals().get("BASE_DIR") or os.path.dirname(os.path.abspath(__file__))
    _log = globals().get("log")
    _now_fn = globals().get("_now") or datetime.now
    _detect_index_regime = globals().get("detect_index_regime")
    _index_regime_name = globals().get("index_regime_name")
    _send_feishu_payload = globals().get("send_feishu_payload")
    _feishu_md_div_fn = globals().get("_feishu_md_div")
    _feishu_card_header_fn = globals().get("_feishu_card_header")
    _feishu_hr_fn = globals().get("_feishu_hr")
    _append_jsonl_fn = globals().get("_append_jsonl")
    _FEISHU_KEYWORD = globals().get("FEISHU_KEYWORD") or "做T猎手预警"
    _FEISHU_WEBHOOK = globals().get("FEISHU_WEBHOOK") or ""
    _pd = globals().get("pd")
    if _pd is None:
        import pandas as _pd  # noqa
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    import logging as _logging
    import pandas as _pd  # noqa

    _log = _logging.getLogger("daily_sentiment")
    if not _log.handlers:
        _h = _logging.StreamHandler()
        _h.setFormatter(_logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
        _log.addHandler(_h)
        _log.setLevel(_logging.INFO)

    _now_fn = datetime.now
    _FEISHU_KEYWORD = "做T猎手预警"
    _FEISHU_WEBHOOK = ""

    # —— 独立模式：按文件路径加载 index_regime.py（只读，不改动）——
    def _load_index_regime_module():
        candidates = [
            os.path.join(_BASE_DIR, "index_regime.py"),
            r"E:\06_T\index_regime.py",
        ]
        for path in candidates:
            if os.path.exists(path):
                spec = importlib.util.spec_from_file_location("index_regime_standalone", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                return mod
        raise FileNotFoundError(f"index_regime.py 未找到（尝试: {candidates}）")

    _ir_mod = _load_index_regime_module()
    _detect_index_regime = _ir_mod.detect_index_regime
    _index_regime_name = _ir_mod.index_regime_name

    def _feishu_md_div_fn(content: str) -> dict:
        return {"tag": "div", "text": {"content": content, "tag": "lark_md"}}

    def _feishu_card_header_fn(title: str, template: str) -> dict:
        return {"template": template, "title": {"tag": "plain_text", "content": title}}

    def _feishu_hr_fn() -> dict:
        return {"tag": "hr"}

    def _append_jsonl_fn(path: str, record: dict) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

    def _load_standalone_feishu() -> Tuple[str, str]:
        """独立模式推送时才读取 E:\\06_T\\config.json 的 webhook/keyword（--no-push 不调用）"""
        webhook, keyword = "", "做T猎手预警"
        for cfg_path in (os.path.join(_BASE_DIR, "config.json"), r"E:\06_T\config.json"):
            try:
                if os.path.exists(cfg_path):
                    with open(cfg_path, "r", encoding="utf-8") as f:
                        cfg = json.load(f)
                    webhook = (cfg.get("feishu", {}).get("webhook_url", "") or "").strip()
                    keyword = (cfg.get("feishu", {}).get("keyword", "") or keyword).strip() or keyword
                    if webhook:
                        break
            except Exception:
                continue
        return webhook, keyword

    def _send_feishu_payload(payload: dict, success_log: str, error_prefix: str,
                             trigger_urgent_alarm_after_success: bool = False) -> bool:
        webhook, _ = _load_standalone_feishu()
        if not webhook:
            _log.warning(f"⚠️  {error_prefix}：飞书 Webhook 未配置")
            return False
        try:
            import requests as _requests
            resp = _requests.post(webhook, json=payload, timeout=8)
            resp.raise_for_status()
            result = resp.json()
            if isinstance(result, dict) and result.get("code", 0) != 0:
                _log.warning(f"⚠️  {error_prefix}失败: {result}")
                return False
            _log.info(success_log)
            return True
        except Exception as e:
            _log.error(f"❌ {error_prefix}发送异常: {str(e)[:120]}")
            return False


def _holdings() -> Dict[str, dict]:
    """持仓 dict：宿主模式取 HOLDINGS 全局；独立模式读 BASE_DIR/holdings.json"""
    h = globals().get("HOLDINGS")
    if isinstance(h, dict) and h:
        return h
    path = os.path.join(_BASE_DIR, "holdings.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


# ============================================================================
# 参数（宿主 SENTIMENT_PARAMS 合并覆盖默认值）
# ============================================================================

DEFAULT_SENTIMENT_PARAMS: Dict[str, Any] = {
    # —— z 归一化常量（108 日窗：2026-02 ~ 2026-07 校准）——
    "z_S_mean": -4.41, "z_S_std": 31.82,
    "z_top3_mean": 5.05, "z_top3_std": 2.34,
    "rolling_window": 60,          # 生产滚动归一化窗口（交易日）
    "rolling_min_samples": 20,     # 历史样本不足则回退常量
    # —— 热度分档（z_top3）——
    "overheat_z": 1.5,             # >= +1.5 过热
    "ice_z": -1.0,                 # <= -1.0 冰点
    "overheat_streak_days": 2,     # uni_up 连续过热 N 日 → 反T止盈
    "uni_down_ban_long_days": 3,   # uni_down 连续 >=N 日 → 禁止正T
    # —— 系统性风险 ——
    "sysrisk_z_S": -1.5,           # z_S 阈值
    "sysrisk_index_drop_pct": -2.0,  # 指数当日跌幅阈值 %（清仓流程升级确认条件之一）
    "sysrisk_e5_dt": 30,           # E5 跌停潮阈值（家，对齐 e5_dt_count；清仓流程升级确认条件之一）
    "sysrisk_intraday_enforce": True,  # V2.1: 14:30 tail z_S≤阈值 → 当日全标的 hold + systemic_risk（盘中生效）
    # —— V2/V2.1 个股级覆盖规则（优先级数字越小越高，见 per_stock_decisions）——
    "stock_diverge_drop_5d": -8.0,     # P2 个股前5日累计跌幅% ≤ 此值 → 背离否决禁 long
    "stock_diverge_below_ma5_days": 3,  # P2 收盘连续 N 日 <MA5 → 背离否决禁 long
    "enable_yesterday_crash_veto": True,  # P3 昨日大跌否决开关
    "yesterday_crash_pct": -4.0,       # P3 昨日跌幅% ≤ 此值 → 次日禁 long 降 hold
    "yesterday_limit_pct": -9.8,       # P4 昨日跌幅% ≤ 此值（近似跌停/一字板）→ 次日 hold
    "loss_streak_days": 2,             # P6 同一标的连续 N 日做T亏损 → 次日 hold
    "gap_up_no_chase_pct": 1.0,        # P7 正T日竞价高开 >此值% → 标注等回踩VWAP确认才买
    "gap_vwap_retrace_pct": 0.3,       # P7 标注文案中的 VWAP 回踩幅度%
    "closure_audit_file": None,        # P6 数据源；None → <BASE_DIR>/t_io/logs/closure_audit.jsonl
    # —— 执行层参数（供 signal_engine/下游读取，本模块只落盘展示）——
    "stop_loss_pct": 0.008,            # 正T买后浮亏-0.8%立即止损 / 反T卖后反向+0.8%接回止损
    "profit_target_pct": 0.008,        # 做T单笔止盈目标 0.8%
    "force_flat_time": "14:50",        # 尾盘强制平仓/接回时点
    # —— 决策矩阵（plan.md V3.0 原表，可配置；键=regime|heat，值=[mode, pos_factor, 理由]）——
    "t_matrix": {
        "uni_up|overheat": ["long", 0.5, "单边上涨×过热→正T半仓，禁追买"],
        "uni_up|hot": ["long", 1.0, "单边上涨×偏热→正T标准仓"],
        "uni_up|cold": ["long", 1.0, "单边上涨×偏冷→正T标准仓(B2区低吸)"],
        "uni_up|ice": ["long", 1.2, "单边上涨×冰点→正T加仓"],
        "range|overheat": ["short", 1.0, "震荡×过热→反T标准仓(S4禁追)"],
        "range|hot": ["long", 1.0, "震荡×偏热→正T标准仓"],
        "range|cold": ["long", 1.0, "震荡×偏冷→正T标准仓"],
        "range|ice": ["long", 1.2, "震荡×冰点→正T加仓(B1区低吸)"],
        "uni_down|overheat": ["short", 0.5, "单边下行×过热→反T轻仓"],
        "uni_down|hot": ["short", 1.0, "单边下行×偏热→反T标准仓"],
        "uni_down|cold": ["short", 0.5, "单边下行×偏冷→反T轻仓"],
        "uni_down|ice": ["long", 0.3, "单边下行×冰点→小仓正T，严禁追买"],
    },
    # —— 数据源 ——
    "report_gen_dir": r"E:\04_实战资料\report_gen",
    "log_dir": None,               # None → env SENTIMENT_LOG_DIR > BASE_DIR/logs
    "push_enabled": True,
}


def sentiment_params() -> Dict[str, Any]:
    p = dict(DEFAULT_SENTIMENT_PARAMS)
    host_p = globals().get("SENTIMENT_PARAMS")
    if isinstance(host_p, dict):
        p.update(host_p)
    return p


def sentiment_log_dir() -> str:
    env = os.environ.get("SENTIMENT_LOG_DIR")
    if env:
        return env
    p = sentiment_params()
    if p.get("log_dir"):
        return str(p["log_dir"])
    return os.path.join(_BASE_DIR, "logs")


def sentiment_jsonl_path() -> str:
    return os.path.join(sentiment_log_dir(), "sentiment_daily.jsonl")


def sentiment_csv_path() -> str:
    return os.path.join(sentiment_log_dir(), "sentiment_daily.csv")


# ============================================================================
# 决策矩阵（plan.md V3.0 定稿；矩阵本体在 SENTIMENT_PARAMS["t_matrix"] 可配置）
# ============================================================================

# heat 分档：overheat z>=+1.5 / hot 0<=z<+1.5 / cold -1<z<0 / ice z<=-1
_MODE_CN = {"long": "正T", "short": "反T"}
_HEAT_CN = {"overheat": "过热", "hot": "偏热", "cold": "偏冷", "ice": "冰点"}


def heat_bucket(z_top3: float, params: Optional[Dict[str, Any]] = None) -> str:
    p = params or sentiment_params()
    z = float(z_top3 or 0.0)
    if z >= float(p["overheat_z"]):
        return "overheat"
    if z >= 0.0:
        return "hot"
    if z > float(p["ice_z"]):
        return "cold"
    return "ice"


def t_decision(regime: str, z_S: Optional[float] = None, z_top3: float = 0.0,
               overheat_streak: int = 0, k_day_type: Optional[str] = None,
               prev_k_down: bool = False, uni_down_days: int = 0,
               params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """市场级决策矩阵：输入大盘态/热度/K-day/连续天数，输出 {mode, pos_factor, reason, k_override}。

    覆盖优先级（本函数内，后应用者优先级高；个股级 P1~P7 见 per_stock_decisions）：
      1) uni_up 且连续过热 >=N 日 → 反T止盈
      2) K-up 当日 → 强制正T（仓位系数抬至 >=1.0）
      3) uni_down 连续 >=3 日 → 禁止正T（改由 trade_gate/pos_factor 表达，而不是 hold）
      4) K-down 当日或次日 → 强制反T 且 pos_factor×0.5（k_override=True，优先级 P5）
    """
    p = params or sentiment_params()
    regime = str(regime or "range")
    heat = heat_bucket(z_top3, p)
    matrix = p.get("t_matrix") if isinstance(p.get("t_matrix"), dict) else {}
    cell = matrix.get(f"{regime}|{heat}") or ["long", 1.0, "默认正T标准仓"]
    mode, factor, base_reason = str(cell[0]), float(cell[1]), str(cell[2])
    reasons = [base_reason]
    k_override = False

    if regime == "uni_up" and heat == "overheat" \
            and int(overheat_streak) >= int(p["overheat_streak_days"]):
        mode, factor = "short", 1.0
        reasons.append(f"连续{int(overheat_streak)}日过热(z_top3≥{p['overheat_z']})→反T止盈")

    if k_day_type == "k_up":
        if mode != "long" or factor < 1.0:
            reasons.append("K-up当日→强制正T")
        mode = "long"
        factor = max(float(factor), 1.0)
        k_override = True

    if regime == "uni_down" and int(uni_down_days) >= int(p["uni_down_ban_long_days"]) \
            and mode == "long":
        mode = "short"
        factor = min(float(factor), 0.5)
        reasons.append(f"uni_down连续{int(uni_down_days)}日≥{p['uni_down_ban_long_days']}日→禁止正T，切换反T")

    if k_day_type == "k_down" or prev_k_down:
        base = float(factor)
        mode = "short"
        factor = round(max(0.0, base * 0.5), 2)
        tag = "K-down当日" if k_day_type == "k_down" else "K-down次日"
        reasons.append(f"{tag}→强制反T，仓位系数×0.5")
        k_override = True

    return {"mode": mode, "mode_cn": _MODE_CN.get(mode, mode),
            "pos_factor": factor, "heat": heat, "heat_cn": _HEAT_CN.get(heat, heat),
            "k_override": k_override,
            "reason": "；".join(reasons),
            "trade_gate": "normal",
            "t_enabled": True}


# ============================================================================
# V2 个股级特征（轻量计算：腾讯 fqkline 日线，与 data_fetcher 日线同源口径；
#    get_daily_context 未含 前5日累计跌幅/连续N日<MA5/当日竞价高开 字段，故新增）
# ============================================================================

def stock_daily_features(code: str, date_str: Optional[str] = None) -> Dict[str, Any]:
    """个股日线特征：前5日累计涨幅%、连续收盘<MA5天数、当日竞价高开%、最新收盘。

    数据源：腾讯 fqkline 前复权日线（含当日 forming bar），单股单请求，仅对持仓股调用。
    """
    out = {"ok": False, "code": code, "date": None, "close": None, "open": None,
           "prev_close": None, "gap_pct": None, "pct_5d": None, "below_ma5_days": 0,
           "prev_day_pct": None}
    try:
        # 持仓键可能带账户后缀（如 000988_B），取数字部分构造行情 symbol
        digits = "".join(ch for ch in str(code) if ch.isdigit())[:6]
        if len(digits) != 6:
            return out
        symbol = ("sh" if digits.startswith(("5", "6", "9")) else "sz") + digits
        end = date_str or _now_fn().strftime("%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        start = (end_dt - timedelta(days=60)).strftime("%Y-%m-%d")
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,{start},{end},40,qfq")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.qq.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            js = json.loads(resp.read().decode("utf-8", errors="ignore"))
        node = (js.get("data") or {}).get(symbol) or {}
        if not isinstance(node, dict):
            return out
        rows = node.get("qfqday") or node.get("day") or []
        bars = []
        for r in rows:
            if isinstance(r, (list, tuple)) and len(r) >= 3:
                try:
                    bars.append({"date": str(r[0])[:10], "open": float(r[1]), "close": float(r[2])})
                except Exception:
                    continue
        if len(bars) < 7:
            return out
        idx = len(bars) - 1
        if date_str:
            for i, b in enumerate(bars):
                if b["date"] == date_str:
                    idx = i
                    break
        cur = bars[idx]
        prev = bars[idx - 1] if idx >= 1 else None
        out["date"] = cur["date"]
        out["close"] = cur["close"]
        out["open"] = cur["open"]
        if prev and prev["close"] > 0:
            out["prev_close"] = prev["close"]
            out["gap_pct"] = round((cur["open"] / prev["close"] - 1.0) * 100.0, 2)
        # 昨日涨跌幅%（P3/P4 否决数据源）：前一交易日 bar 相对其前收
        if idx >= 2 and prev and bars[idx - 2]["close"] > 0:
            out["prev_day_pct"] = round((prev["close"] / bars[idx - 2]["close"] - 1.0) * 100.0, 2)
        # 前5日累计涨幅（当日收盘 vs 5日前收盘）
        if idx >= 5 and bars[idx - 5]["close"] > 0:
            out["pct_5d"] = round((cur["close"] / bars[idx - 5]["close"] - 1.0) * 100.0, 2)
        # 连续收盘 <MA5 天数（含当日，MA5=当日及前4日收盘均值）
        streak = 0
        for i in range(idx, 3, -1):
            ma5 = sum(bars[j]["close"] for j in range(i - 4, i + 1)) / 5.0
            if bars[i]["close"] < ma5:
                streak += 1
            else:
                break
        out["below_ma5_days"] = streak
        out["ok"] = True
    except Exception as e:
        _log.warning(f"⚠️  个股日线特征获取失败 {code}: {str(e)[:80]}")
    return out


def _closure_audit_path(params: Optional[Dict[str, Any]] = None) -> str:
    p = params or sentiment_params()
    if p.get("closure_audit_file"):
        return str(p["closure_audit_file"])
    env = os.environ.get("CLOSURE_AUDIT_FILE")
    if env:
        return env
    return os.path.join(_BASE_DIR, "t_io", "logs", "closure_audit.jsonl")


def load_loss_streaks(params: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """V2c 连亏熔断数据源：读 closure_audit.jsonl，按标的统计最近连续
    做T亏损天数（est_pnl<0 且当日有成交；无成交日跳过不计、不打断连续性）。"""
    p = params or sentiment_params()
    path = _closure_audit_path(p)
    series: Dict[str, List[Tuple[str, float]]] = {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    date = str(rec.get("date") or "")
                    for d in (rec.get("details") or []):
                        code = str(d.get("code") or "")
                        if not code:
                            continue
                        if (float(d.get("sold", 0) or 0) + float(d.get("bought", 0) or 0)) <= 0:
                            continue  # 当日无做T成交
                        pnl = d.get("est_pnl")
                        if pnl is None:
                            continue
                        series.setdefault(code, []).append((date, float(pnl)))
    except Exception:
        return {}
    streaks: Dict[str, int] = {}
    for code, items in series.items():
        items.sort(key=lambda x: x[0])
        streak = 0
        for _, pnl in reversed(items):
            if pnl < 0:
                streak += 1
            else:
                break
        if streak:
            streaks[code] = streak
    return streaks


def per_stock_decisions(regime: str, z_S: Optional[float], z_top3: float,
                        overheat_streak: int = 0, k_day_type: Optional[str] = None,
                        prev_k_down: bool = False, uni_down_days: int = 0,
                        systemic_risk: bool = False,
                        holdings: Optional[Dict[str, dict]] = None,
                        date_str: Optional[str] = None,
                        params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """市场级决策 + V2.1 个股级覆盖，优先级（数字越小越高，高优先级决定后低优先级不再改 mode）：

      P1 清仓覆盖：sysrisk_intraday_enforce 且 z_S≤sysrisk_z_S → 全标的 hold（盘中当日生效）
      P2 个股背离否决：前5日累计跌幅≤-8% 或 连续3日收<MA5 → 禁long，降 short（regime≠uni_down）/ hold
      P3 昨日大跌否决（enable_yesterday_crash_veto）：昨日跌幅≤-4% → 禁long 降 hold
      P4 昨日跌停/一字板（昨日跌幅≤-9.8% 近似）→ hold
      P5 K-down 当日及次日→强制 short×0.5；K-up 当日→强制 long（t_decision 内完成，k_override）
      P6 连亏熔断：同一标的连续2日做T亏损 → hold（closure_audit.jsonl est_pnl）
      P7 高开不追：正T日竞价高开>1% → 仍 long，reason 标注"等回踩VWAP-0.3%确认才买"

    返回 {"market": 市场级决策, "per_stock": {code: {mode, pos_factor, reason, notes}}}"""
    p = params or sentiment_params()
    base = t_decision(regime=regime, z_S=z_S, z_top3=z_top3,
                      overheat_streak=overheat_streak, k_day_type=k_day_type,
                      prev_k_down=prev_k_down, uni_down_days=uni_down_days, params=p)
    base_rank = 5 if base.get("k_override") else 7   # K-day 覆盖=P5；基础矩阵=P7 最低

    # P1 清仓覆盖（市场级，z_S≤阈值 当日生效）
    sysrisk_hit = False
    if bool(p.get("sysrisk_intraday_enforce", True)) and z_S is not None:
        try:
            sysrisk_hit = float(z_S) <= float(p["sysrisk_z_S"])
        except Exception:
            sysrisk_hit = False
    if systemic_risk:
        sysrisk_hit = True
    if sysrisk_hit:
        base["mode"] = "short"
        base["pos_factor"] = 0.0
        base["trade_gate"] = "clear"
        base["t_enabled"] = False
        base["reason"] += (f"；🚨清仓覆盖(z_S={float(z_S):+.2f}≤{p['sysrisk_z_S']}，"
                           f"14:30当日判定当日生效)→全标的清仓门控")
        base_rank = 1
    base["mode_cn"] = _MODE_CN.get(base["mode"], base["mode"])
    base.setdefault("trade_gate", "normal")
    base.setdefault("t_enabled", True)

    holdings = holdings if holdings is not None else _holdings()
    loss_streaks = load_loss_streaks(p)
    per_stock: Dict[str, Dict[str, Any]] = {}
    for code, holding in holdings.items():
        name = str(holding.get("name", code))
        mode, factor, rank = base["mode"], float(base["pos_factor"]), base_rank
        notes: List[str] = []
        feat = stock_daily_features(code, date_str)
        feat_ok = bool(feat.get("ok"))

        # P2 个股背离否决：禁 long，降级 short（regime≠uni_down）或 hold（uni_down）
        if rank > 2 and mode == "long" and feat_ok:
            diverge_drop = (feat.get("pct_5d") is not None
                            and float(feat["pct_5d"]) <= float(p["stock_diverge_drop_5d"]))
            diverge_ma5 = int(feat.get("below_ma5_days") or 0) >= int(p["stock_diverge_below_ma5_days"])
            if diverge_drop or diverge_ma5:
                why = (f"前5日{float(feat['pct_5d']):+.1f}%≤{p['stock_diverge_drop_5d']}%" if diverge_drop
                       else f"连续{int(feat['below_ma5_days'])}日收<MA5≥{p['stock_diverge_below_ma5_days']}日")
                mode, factor = "short", min(factor, 1.0)
                rank = 2
                notes.append(f"个股背离否决({why})→禁正T")

        # P3 昨日大跌否决：昨日跌幅≤阈值 → 禁 long
        if rank > 3 and mode == "long" and feat_ok \
                and bool(p.get("enable_yesterday_crash_veto", True)) \
                and feat.get("prev_day_pct") is not None \
                and float(feat["prev_day_pct"]) <= float(p["yesterday_crash_pct"]):
            mode, factor, rank = "short", min(factor, 0.5), 3
            notes.append(f"昨日大跌否决(昨日{float(feat['prev_day_pct']):+.2f}%≤{p['yesterday_crash_pct']}%)→切换反T")

        # P4 昨日跌停/一字板（近似：昨日跌幅≤-9.8%）→ 清仓门控
        if rank > 4 and mode in ("long", "short") and feat_ok \
                and feat.get("prev_day_pct") is not None \
                and float(feat["prev_day_pct"]) <= float(p["yesterday_limit_pct"]):
            factor, rank = 0.0, 4
            notes.append(f"昨日跌停/一字板(昨日{float(feat['prev_day_pct']):+.2f}%)→清仓门控")

        # P6 连亏熔断：同一标的连续 N 日做T亏损 → 清仓门控
        ls = int(loss_streaks.get(code, 0) or 0)
        if rank > 6 and ls >= int(p["loss_streak_days"]):
            factor, rank = 0.0, 6
            notes.append(f"连亏熔断(连续{ls}日做T亏损)→今日清仓门控")

        # P7 高开不追（仅标注，不改 mode）
        d = {"mode": mode, "mode_cn": _MODE_CN.get(mode, mode), "pos_factor": factor, "trade_gate": "normal", "t_enabled": True}
        if mode == "long" and feat_ok and feat.get("gap_pct") is not None \
                and float(feat["gap_pct"]) > float(p["gap_up_no_chase_pct"]):
            d["gap_wait_vwap"] = True
            notes.append(f"竞价高开{float(feat['gap_pct']):+.2f}%>{p['gap_up_no_chase_pct']}%"
                         f"→等回踩VWAP-{p['gap_vwap_retrace_pct']}%确认才买")

        d["reason"] = base["reason"] + ("；" + "；".join(notes) if notes else "")
        d["notes"] = notes
        d["name"] = name
        if feat_ok:
            d["stock_feat"] = {"gap_pct": feat.get("gap_pct"), "pct_5d": feat.get("pct_5d"),
                               "below_ma5_days": feat.get("below_ma5_days"),
                               "prev_day_pct": feat.get("prev_day_pct")}
        if factor <= 0.0:
            d["trade_gate"] = "clear"
            d["t_enabled"] = False
        if d["mode"] == "short" and int(holding.get("base", 0) or 0) <= 0:
            d["reason"] += "；⚠️无底仓，反T不可执行"
        per_stock[code] = d
    return {"market": base, "per_stock": per_stock, "sysrisk_hit": sysrisk_hit}


# ============================================================================
# 历史记录与滚动归一化
# ============================================================================

def load_sentiment_history() -> List[Dict[str, Any]]:
    """读取 sentiment_daily.jsonl，按 date 去重（保留最后一条），按日期升序返回"""
    path = sentiment_jsonl_path()
    recs: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        d = str(rec.get("date") or "")
                        if d:
                            recs[d] = rec
                    except Exception:
                        continue
        except Exception:
            pass
    return [recs[d] for d in sorted(recs.keys())]


def _rolling_norm(values: List[Optional[float]], const_mean: float, const_std: float,
                  window: int, min_samples: int) -> Tuple[float, float, str]:
    """60 日滚动均值/标准差；样本不足回退 108 日常量。返回 (mean, std, source)"""
    vals = [float(v) for v in values if v is not None][-int(window):]
    if len(vals) >= int(min_samples):
        mu = statistics.fmean(vals)
        sd = statistics.pstdev(vals)
        if sd > 1e-9:
            return mu, sd, f"rolling{len(vals)}"
    return float(const_mean), float(const_std), "const108"


def _overheat_streak(history: List[Dict[str, Any]], today_z: float, exclude_date: str,
                     params: Dict[str, Any]) -> int:
    """连续过热天数（含当日）：从最近历史向回数 z_top3>=阈值 的连续天数"""
    streak = 1 if float(today_z) >= float(params["overheat_z"]) else 0
    if streak == 0:
        return 0
    for rec in reversed(history):
        if str(rec.get("date")) == exclude_date:
            continue
        try:
            if float(rec.get("z_top3")) >= float(params["overheat_z"]):
                streak += 1
            else:
                break
        except Exception:
            break
    return streak


# ============================================================================
# 韭研 TOP3 板块热度（复用 report_gen 打分链，口径对齐 run_backtest_v2.py）
# ============================================================================

_RG_MOD_CACHE: Dict[str, Any] = {}


def _load_report_gen_modules(report_gen_dir: str) -> Dict[str, Any]:
    """importlib 按文件路径局部加载 report_gen 的 data_loader/market_data/scorer。

    这三个模块仅依赖 pandas/urllib/json 等轻量库，不触发 report_gen 主流程
    （不 import 其 main.py / push_feishu.py，避免重链与飞书副作用）。
    """
    global _RG_MOD_CACHE
    if _RG_MOD_CACHE.get("dir") == report_gen_dir and _RG_MOD_CACHE.get("mods"):
        return _RG_MOD_CACHE["mods"]
    mods = {}
    for alias, rel in (("rg_data_loader", os.path.join("modules", "data_loader.py")),
                       ("rg_market_data", os.path.join("modules", "market_data.py")),
                       ("rg_scorer", os.path.join("modules", "scorer.py"))):
        path = os.path.join(report_gen_dir, rel)
        if not os.path.exists(path):
            raise FileNotFoundError(f"report_gen 模块缺失: {path}")
        spec = importlib.util.spec_from_file_location(alias, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[alias] = mod
    _RG_MOD_CACHE = {"dir": report_gen_dir, "mods": mods}
    return mods


def compute_jiuyan_top3(date_str: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """计算指定日期的韭研板块平均分 / TOP3 均值（口径与 108 日回测完全一致）"""
    p = params or sentiment_params()
    rg_dir = str(p["report_gen_dir"])
    mods = _load_report_gen_modules(rg_dir)
    DataLoader = mods["rg_data_loader"].DataLoader
    MarketDataFetcher = mods["rg_market_data"].MarketDataFetcher
    ConceptScorer = mods["rg_scorer"].ConceptScorer

    # 打分维度（report_gen config.json scoring.dimensions；缺省=全 9 维）
    dimensions: List[str] = []
    cfg_path = os.path.join(rg_dir, "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            rg_cfg = json.load(f)
        dimensions = list(rg_cfg.get("scoring", {}).get("dimensions", []) or [])
    except Exception as e:
        _log.warning(f"⚠️  report_gen config.json 读取失败（用全维度）: {str(e)[:80]}")

    loader = DataLoader(config={})
    watchlist_df = loader.load_watchlist()
    if watchlist_df is None or watchlist_df.empty:
        raise RuntimeError("watchlist_jiuyan.json 加载失败或为空")
    df_pool = watchlist_df[watchlist_df["韭研概念"].str.strip().ne("")].copy()
    codes = list(dict.fromkeys(df_pool["代码"].astype(str).tolist()))
    _log.info(f"📊 韭研打分池: {len(df_pool)} 行 / {len(codes)} 只标的，获取 {date_str} 行情...")

    fetcher = MarketDataFetcher()
    market_df = fetcher.fetch_for_date(codes, date_str)
    failed = list(getattr(fetcher, "last_failed", []) or [])

    if market_df is None or market_df.empty:
        merged_pool = df_pool.copy()
        _log.warning("⚠️  行情获取为空，全部标的按 0 分处理（热度失真，仅兜底）")
    else:
        if "名称" in market_df.columns:
            market_df = market_df.drop(columns=["名称"])
        merged_pool = df_pool.merge(market_df, on="代码", how="left")
        merged_pool = merged_pool[merged_pool["韭研概念"].str.strip().ne("")].copy()

    # —— score_stocks 口径（setdefault 不覆盖已有键，与生产 main.py / run_backtest_v2 一致）——
    scorer = ConceptScorer(dimensions=dimensions if dimensions else None)
    stock_list = []
    for _, row in merged_pool.iterrows():
        stock = row.to_dict()
        stock.setdefault("涨停", int(row.get("涨停", 0)) if _pd.notna(row.get("涨停")) else 0)
        stock.setdefault("连板天数", 0)
        stock.setdefault("领涨天数", 0)
        stock.setdefault("突破", 1 if abs(row.get("涨跌幅", 0) or 0) > 5 else 0)
        stock.setdefault("封单质量", 0)
        stock.setdefault("暗线概念数", len(str(stock.get("韭研概念", "")).split("_")))
        stock.setdefault("量比", float(row.get("量比", 1.0)) if _pd.notna(row.get("量比")) else 1.0)
        stock.setdefault("板块涨停家数", 0)
        stock.setdefault("近5日振幅", float(row.get("振幅", 5.0)) if _pd.notna(row.get("振幅")) else 5.0)
        stock.setdefault("近5日换手率", float(row.get("换手率", 3.0)) if _pd.notna(row.get("换手率")) else 3.0)
        stock_list.append(stock)
    scored_list = scorer.compute_batch(stock_list)
    scored_df = _pd.DataFrame(scored_list)

    # —— 板块聚合（load_concept_summary 口径：简单均值）——
    sector_avgs: Dict[str, Dict[str, Any]] = {}
    for category in sorted(scored_df["韭研分类"].unique()):
        if not category:
            continue
        cat_df = scored_df[scored_df["韭研分类"] == category]
        sector_avgs[str(category)] = {
            "avg": round(float(cat_df["总得分"].fillna(0).mean()), 2),
            "n": int(len(cat_df)),
        }
    ranked = sorted(sector_avgs.items(), key=lambda kv: kv[1]["avg"], reverse=True)
    top3 = ranked[:3]
    top3_avg = round(sum(v["avg"] for _, v in top3) / len(top3), 2) if top3 else 0.0
    top3_names = [str(k) for k, _ in top3]

    return {
        "date": date_str,
        "sector_avgs": sector_avgs,
        "top3_avg": top3_avg,
        "top3_names": top3_names,
        "pool_rows": int(len(df_pool)),
        "pool_codes": int(len(codes)),
        "fetch_ok": int(0 if market_df is None or market_df.empty else len(market_df)),
        "fetch_failed": failed,
    }


# ============================================================================
# 指数当日涨跌幅（腾讯 fqkline，含 forming bar；独立轻量请求）
# ============================================================================

def fetch_index_pct_change(date_str: Optional[str] = None, symbol: str = "sh000001") -> Optional[float]:
    """上证指数当日涨跌幅 %。date_str 为空取最后一根 bar；历史日期匹配对应 bar。"""
    try:
        end = date_str or _now_fn().strftime("%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        start = (end_dt - timedelta(days=20)).strftime("%Y-%m-%d")
        url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
               f"param={symbol},day,{start},{end},10,qfq")
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://finance.qq.com/",
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            js = json.loads(resp.read().decode("utf-8", errors="ignore"))
        node = js["data"][symbol]
        rows = node.get("qfqday") or node.get("day") or []
        bars = [(str(r[0])[:10], float(r[2])) for r in rows
                if isinstance(r, (list, tuple)) and len(r) >= 3]
        if len(bars) < 2:
            return None
        idx = len(bars) - 1
        if date_str:
            for i, (d, _) in enumerate(bars):
                if d == date_str:
                    idx = i
                    break
        if idx <= 0:
            return None
        close, prev_close = bars[idx][1], bars[idx - 1][1]
        if prev_close <= 0:
            return None
        return round((close / prev_close - 1.0) * 100.0, 2)
    except Exception as e:
        _log.warning(f"⚠️  指数涨跌幅获取失败: {str(e)[:80]}")
        return None


# ============================================================================
# 核心：compute_daily_sentiment
# ============================================================================

def compute_daily_sentiment(mode: str = "tail", as_of: Optional[str] = None) -> Dict[str, Any]:
    """合成大盘热度 × 韭研TOP3 × 决策矩阵，返回完整结果 dict。"""
    p = sentiment_params()
    t0 = _time_mod.time()

    # 1) 大盘态势（index_regime；tail=盘中 forming bar estimate / eod / morning）
    regime_obj, score, ctx = _detect_index_regime(as_of=as_of, mode=mode)
    date_str = str(ctx.get("date") or _now_fn().strftime("%Y-%m-%d"))
    regime = str(ctx.get("regime", "range"))
    score_S = float(ctx.get("score") or 0.0)
    days_in_regime = int(ctx.get("days_in_regime") or 0)
    detail = ctx.get("detail") or {}
    key_day = detail.get("key_day") or {}
    k_day_type = key_day.get("type")  # "k_up" / "k_down" / None
    limit_pool = detail.get("limit_pool") or {}
    dt_count = int(limit_pool.get("dt_count") or 0)
    zt_count = int(limit_pool.get("zt_count") or 0)
    uni_down_days = days_in_regime if regime == "uni_down" else 0
    try:
        regime_name = str(ctx.get("regime_name") or _index_regime_name(regime))
    except Exception:
        regime_name = regime

    # 2) 韭研 TOP3 板块热度
    jiuyan = compute_jiuyan_top3(date_str, params=p)
    top3_avg = float(jiuyan["top3_avg"])
    top3_names = list(jiuyan["top3_names"])

    # 3) 历史 + 滚动归一化（60 日滚动，不足回退 108 日常量）
    history = load_sentiment_history()
    hist_excl_today = [r for r in history if str(r.get("date")) != date_str]
    mu_S, sd_S, src_S = _rolling_norm([r.get("score_S") for r in hist_excl_today],
                                      p["z_S_mean"], p["z_S_std"],
                                      p["rolling_window"], p["rolling_min_samples"])
    mu_t, sd_t, src_t = _rolling_norm([r.get("top3_avg") for r in hist_excl_today],
                                      p["z_top3_mean"], p["z_top3_std"],
                                      p["rolling_window"], p["rolling_min_samples"])
    z_S = round((score_S - mu_S) / sd_S, 3)
    z_top3 = round((top3_avg - mu_t) / sd_t, 3)

    # 4) 连续状态 / K-day 次日
    overheat_streak = _overheat_streak(hist_excl_today, z_top3, date_str, p)
    prev_k_down = False
    if hist_excl_today:
        prev_k_down = str(hist_excl_today[-1].get("k_day_type") or "") == "k_down"

    # 5) 系统性风险（V2.1 终稿：z_S≤阈值 → systemic_risk 当日生效全标的 hold；
    #    E5跌停潮/指数跌幅≥阈值 为清仓流程升级确认条件 systemic_confirmed）
    index_pct = fetch_index_pct_change(date_str)
    e5_surge = dt_count >= int(p["sysrisk_e5_dt"])
    idx_crash = (index_pct is not None and float(index_pct) <= float(p["sysrisk_index_drop_pct"]))
    sysreasons: List[str] = []
    if e5_surge:
        sysreasons.append(f"E5跌停潮(跌停{dt_count}家≥{p['sysrisk_e5_dt']})")
    if idx_crash:
        sysreasons.append(f"指数{float(index_pct):+.2f}%≤{p['sysrisk_index_drop_pct']}%")
    systemic_confirmed = bool(sysreasons)

    # 6) 决策矩阵 + V2.1 个股级覆盖（P1清仓/P2背离/P3昨日大跌/P4昨日跌停/P5 K-day/P6连亏/P7高开标注）
    ds = per_stock_decisions(regime=regime, z_S=z_S, z_top3=z_top3,
                             overheat_streak=overheat_streak, k_day_type=k_day_type,
                             prev_k_down=prev_k_down, uni_down_days=uni_down_days,
                             systemic_risk=False, holdings=_holdings(),
                             date_str=date_str, params=p)
    decision = ds["market"]
    per_stock = ds["per_stock"]
    systemic_risk = bool(ds.get("sysrisk_hit"))           # z_S≤阈值 当日生效
    if systemic_risk and systemic_confirmed:
        sysreasons.append("满足清仓流程升级确认条件→建议启动清仓流程")

    # 7) 汇总理由
    reasons = [
        f"大盘{regime_name}(持续{days_in_regime}日) S={score_S:.2f} z_S={z_S:+.2f}({src_S})",
        f"韭研TOP3: {'/'.join(top3_names)} 均值={top3_avg:.2f} z_top3={z_top3:+.2f}({src_t}) {decision['heat_cn']}",
        f"决策: {decision['mode_cn']} ×{decision['pos_factor']} — {decision['reason']}",
    ]
    if k_day_type:
        reasons.append(f"K-day: {k_day_type}")
    if prev_k_down:
        reasons.append("昨日K-down→今日按K-down次日处理")
    if systemic_risk:
        reasons.append(f"🚨 系统性风险: z_S={z_S:+.2f}≤{p['sysrisk_z_S']}（14:30当日判定当日生效，全标的hold）"
                       + (f"；{' + '.join(sysreasons)}" if sysreasons else ""))

    decision_summary = decision["mode"]
    if per_stock:
        from collections import Counter
        cnt = Counter(v["mode"] for v in per_stock.values())
        decision_summary = "/".join(f"{m}×{n}" for m, n in cnt.items())

    return {
        "date": date_str,
        "mode": mode,
        "estimate": bool(ctx.get("estimate", mode == "tail")),
        "regime": regime,
        "regime_name": regime_name,
        "days_in_regime": days_in_regime,
        "score_S": round(score_S, 2),
        "z_S": z_S,
        "z_S_src": src_S,
        "top3_avg": top3_avg,
        "z_top3": z_top3,
        "z_top3_src": src_t,
        "top3_names": top3_names,
        "sector_avgs": jiuyan["sector_avgs"],
        "pool_rows": jiuyan["pool_rows"],
        "pool_codes": jiuyan["pool_codes"],
        "fetch_ok": jiuyan["fetch_ok"],
        "fetch_failed_n": len(jiuyan["fetch_failed"]),
        "k_day_type": k_day_type,
        "prev_k_down": prev_k_down,
        "overheat_streak": overheat_streak,
        "uni_down_days": uni_down_days,
        "index_pct": index_pct,
        "zt_count": zt_count,
        "dt_count": dt_count,
        "systemic_risk": systemic_risk,
        "systemic_confirmed": systemic_confirmed,
        "systemic_reasons": sysreasons,
        "t_decision": decision,
        "per_stock": per_stock,
        "decision_summary": decision_summary,
        "reasons": reasons,
        "elapsed_sec": round(_time_mod.time() - t0, 1),
        "generated_at": _now_fn().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ============================================================================
# 落盘：jsonl 逐日 append + csv 同日覆盖
# ============================================================================

_CSV_FIELDS = ["date", "regime", "regime_name", "score_S", "z_S", "top3_avg", "z_top3",
               "top3_names", "k_day_type", "index_pct", "dt_count", "systemic_risk",
               "decision_summary"]


def save_sentiment_record(result: Dict[str, Any]) -> None:
    log_dir = sentiment_log_dir()
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    # jsonl 逐日一条（append；读取侧按 date 去重取最后）
    _append_jsonl_fn(sentiment_jsonl_path(), result)
    # csv 同日覆盖更新
    csv_path = sentiment_csv_path()
    row = {
        "date": result.get("date"),
        "regime": result.get("regime"),
        "regime_name": result.get("regime_name"),
        "score_S": result.get("score_S"),
        "z_S": result.get("z_S"),
        "top3_avg": result.get("top3_avg"),
        "z_top3": result.get("z_top3"),
        "top3_names": "|".join(result.get("top3_names") or []),
        "k_day_type": result.get("k_day_type") or "",
        "index_pct": result.get("index_pct"),
        "dt_count": result.get("dt_count"),
        "systemic_risk": int(bool(result.get("systemic_risk"))),
        "decision_summary": result.get("decision_summary"),
    }
    try:
        rows: List[Dict[str, Any]] = []
        if os.path.exists(csv_path):
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    if str(r.get("date")) != str(row["date"]):
                        rows.append(r)
        rows.append(row)
        rows.sort(key=lambda r: str(r.get("date") or ""))
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in _CSV_FIELDS})
        _log.info(f"💾 热度落盘: {sentiment_jsonl_path()} + {csv_path}")
    except Exception as e:
        _log.warning(f"⚠️  sentiment csv 落盘失败: {str(e)[:100]}")


# ============================================================================
# 飞书合成卡片
# ============================================================================

def build_sentiment_card(result: Dict[str, Any]) -> dict:
    decision = result.get("t_decision") or {}
    mode = decision.get("mode", "long")
    template = {"long": "green", "short": "red"}.get(mode, "blue")
    if result.get("systemic_risk"):
        template = "red"

    date_s = str(result.get("date", ""))
    title_tag = "🔥"
    if result.get("systemic_risk"):
        title_tag = "🚨"
    est_tag = "（盘中预判 estimate）" if result.get("estimate") else ""
    title = f"{title_tag} 大盘热度×韭研TOP3 {date_s}{est_tag} - {_FEISHU_KEYWORD}"

    lines: List[str] = []
    lines.append(
        f"**大盘态势**：{result.get('regime_name')}（持续{result.get('days_in_regime')}日）"
        f"｜S={result.get('score_S')}｜z_S={float(result.get('z_S') or 0):+.2f}（{result.get('z_S_src')}）")
    idx_pct = result.get("index_pct")
    idx_txt = f"{float(idx_pct):+.2f}%" if idx_pct is not None else "N/A"
    k_txt = result.get("k_day_type") or "无"
    lines.append(
        f"**指数**：{idx_txt}｜涨停 {result.get('zt_count')} / 跌停 {result.get('dt_count')} 家｜K-day：{k_txt}")

    sector_avgs = result.get("sector_avgs") or {}
    top3_bits = []
    for name in (result.get("top3_names") or []):
        avg = (sector_avgs.get(name) or {}).get("avg", "?")
        top3_bits.append(f"{name}({avg})")
    lines.append(
        f"**韭研TOP3**：{' / '.join(top3_bits)} → 均值 {result.get('top3_avg')}"
        f"｜z_top3={float(result.get('z_top3') or 0):+.2f}（{decision.get('heat_cn', '')}，{result.get('z_top3_src')}）")

    lines.append(
        f"**T策略矩阵**：{decision.get('mode_cn')} ×{decision.get('pos_factor')}"
        f" — {decision.get('reason')}")

    per_stock = result.get("per_stock") or {}
    if per_stock:
        lines.append("**逐股策略**：")
        for code, d in per_stock.items():
            line = f"• {d.get('name')}({code})：{d.get('mode_cn')} ×{d.get('pos_factor')}"
            notes = d.get("notes") or []
            if notes:
                line += f"｜{'；'.join(notes)}"
            lines.append(line)

    if result.get("systemic_risk"):
        sr_line = (f"🚨 **系统性风险预警**：z_S={float(result.get('z_S') or 0):+.2f}≤阈值"
                   f" → **14:30 当日判定当日生效，全标的清仓门控**")
        if result.get("systemic_confirmed"):
            sr_line += (f"；{' + '.join(result.get('systemic_reasons') or [])}"
                        f" → 建议尾盘启动清仓流程（qmt_trader.liquidate_all dry_run 清单，人工确认后执行）")
        lines.append(sr_line)
    if result.get("uni_down_days", 0) >= 3:
        lines.append(f"⚠️ uni_down 已连续 {result.get('uni_down_days')} 日：禁止正T，反T/观望为主")

    card_elements = []
    for i, line in enumerate(lines):
        if line.startswith("**逐股策略**"):
            card_elements.append(_feishu_hr_fn())
        card_elements.append(_feishu_md_div_fn(line))
    card_elements.append(_feishu_hr_fn())
    card_elements.append(_feishu_md_div_fn(
        f"数据：池{result.get('pool_rows')}行/成功{result.get('fetch_ok')}只"
        f"｜耗时{result.get('elapsed_sec')}s｜落盘 logs/sentiment_daily.jsonl"))
    card = {"config": {"wide_screen_mode": True},
            "header": _feishu_card_header_fn(title, template),
            "elements": card_elements}
    return {"msg_type": "interactive", "card": card, "notify_type": 1}


# ============================================================================
# 推送入口（后台线程，避免阻塞主扫描循环 1~2 分钟）
# ============================================================================

_push_thread_running = False
_push_lock = threading.Lock()


def push_daily_sentiment(now: Optional[datetime] = None) -> bool:
    """14:30 热度推送（由 main.py _maybe_push_index_regime_eod 钩子调用）。

    计算 + 落盘 + 推送全部在后台线程执行；调用方已做窗口/每日一次占位。
    返回 True 表示后台任务已启动。
    """
    global _push_thread_running
    with _push_lock:
        if _push_thread_running:
            return False
        _push_thread_running = True

    def _worker():
        global _push_thread_running
        try:
            result = compute_daily_sentiment(mode="tail")
            save_sentiment_record(result)
            p = sentiment_params()
            if p.get("push_enabled", True):
                payload = build_sentiment_card(result)
                _send_feishu_payload(
                    payload=payload,
                    success_log=(f"✅ 大盘热度×韭研TOP3已推送: {result.get('regime_name')} "
                                 f"S={result.get('score_S')} z_S={result.get('z_S')} "
                                 f"top3={result.get('top3_avg')} z_top3={result.get('z_top3')} "
                                 f"决策={result.get('decision_summary')}"),
                    error_prefix="大盘热度×韭研TOP3推送",
                    trigger_urgent_alarm_after_success=bool(result.get("systemic_risk")),
                )
        except Exception as e:
            try:
                _log.warning(f"⚠️ push_daily_sentiment 后台异常（已吞掉）: {str(e)[:150]}")
            except Exception:
                pass
        finally:
            with _push_lock:
                _push_thread_running = False

    th = threading.Thread(target=_worker, name="daily_sentiment_push", daemon=True)
    th.start()
    return True


# ============================================================================
# CLI
# ============================================================================

def _cli() -> None:
    ap = argparse.ArgumentParser(
        description="V3.0 大盘热度×韭研TOP3 每日热度推送与做T策略决策（daily_sentiment）")
    ap.add_argument("--date", default=None, help="评估日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--mode", default="tail", choices=["eod", "morning", "tail"],
                    help="大盘态势评估时点，默认 tail（盘中含 forming bar）")
    ap.add_argument("--no-push", action="store_true", help="只计算+落盘，不推飞书")
    ap.add_argument("--no-save", action="store_true", help="只计算打印，不落盘不推送")
    args = ap.parse_args()

    # report_gen 模块用 print 输出进度：计算期统一引到 stderr，保证 stdout 是纯净 JSON
    import contextlib
    with contextlib.redirect_stdout(sys.stderr):
        result = compute_daily_sentiment(mode=args.mode, as_of=args.date)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if not args.no_save:
        save_sentiment_record(result)
    if not args.no_push and not args.no_save:
        payload = build_sentiment_card(result)
        _send_feishu_payload(
            payload=payload,
            success_log=f"✅ 大盘热度×韭研TOP3已推送: {result.get('date')}",
            error_prefix="大盘热度×韭研TOP3推送",
            trigger_urgent_alarm_after_success=bool(result.get("systemic_risk")),
        )


if __name__ == "__main__":
    _cli()
