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
  holdings.json / t_io/state/holdings_{date}.json         持仓（当前 + 日快照）
  t_mode.json                                             T模式（正/反T）
"""
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Windows 终端 UTF-8 修复（避免 GBK 乱码）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(r"E:\06_T")
OUT = BASE / "t_io" / "validation" / "daily_review"
TRACES = BASE / "t_io" / "traces"
STATE_DIR = BASE / "t_io" / "state"
REPORT_DIR = BASE / "doc" / "每日复盘"
HOLDINGS = BASE / "holdings.json"
T_MODE = BASE / "t_mode.json"
IDX_REGIME = BASE / "t_io" / "index_regime"
LOGS_DIR = BASE / "t_io" / "logs"
INTRADAY_STATE = BASE / "t_io" / "intraday_state.json"

# 内置名称映射（数据缺失 code 时兜底；可由 holdings/add_watch/trace 补充）
NAMES = {
    "000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
    "600481": "双良节能", "603667": "五洲新春", "002639": "雪人集团",
    "300153": "科泰电源", "300364": "中文在线",
}

COND_LABELS = {
    "macd_golden": "MACD多头",
    "boll_mid_support": "BOLL中轨",
    "rsi_healthy": "RSI健康",
    "volume_shrink": "缩量",
    "support_retest": "回踩支撑",
}


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


class Api:
    """暴露给前端的 js_api 方法（pywebview 序列化返回值）。"""

    def __init__(self):
        self._dates_cache = None
        # 增量信号轮询的内存态（webview.start() 期间存活）
        self._dt = {"date": None, "offset": 0, "seen": set()}

    # ---------- 日期发现 ----------
    def available_dates(self):
        dates = set()
        for p in OUT.glob("daily_review_*.json"):
            stem = p.stem
            if stem.startswith("daily_review_") and len(stem) == len("daily_review_") + 10:
                dates.add(stem[len("daily_review_"):])
        dates.add(datetime.now().strftime("%Y-%m-%d"))  # 今天盘中可能尚无 daily_review，仍可选（实时模式）
        return sorted(dates, reverse=True)

    # ---------- 单日完整载荷 ----------
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
            # 今天盘中可能尚无 daily_review，返回部分载荷供实时模式用
            if date == today:
                out.update({
                    "sig_stat": {}, "shadow": {"total": None, "near": {}},
                    "qty_freeze": {}, "closed_loop": {}, "audit_problems": None,
                    "settle": {}, "add_watch": {}, "watch": {}, "kpi": {},
                    "positions": self._load_positions(date, {}),
                    "position_builder": self._agg_position_builder(date),
                    "stage_board": self._load_stage_board(),
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
        out["add_watch"] = dr.get("add_watch", {})
        out["watch"] = dr.get("watch", {})

        # KPI：优先独立文件，缺失时回退到日复盘内嵌 kpi
        kpi_fp = OUT / f"kpi_{date}.json"
        if kpi_fp.exists():
            out["kpi"] = _load_json(kpi_fp, {})
        else:
            out["kpi"] = dr.get("kpi", {})

        out["positions"] = self._load_positions(date, out.get("kpi", {}))
        out["position_builder"] = self._agg_position_builder(date)
        out["stage_board"] = self._load_stage_board()

        md_fp = REPORT_DIR / f"{date}_复盘.md"
        out["report_md"] = ""
        if md_fp.exists():
            try:
                out["report_md"] = open(md_fp, encoding="utf-8").read()
            except Exception:
                pass

        out["name_map"] = self._build_name_map(
            out["sig_stat"], out["add_watch"], out["position_builder"], out["positions"]["current"]
        )
        return _clean(out)

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
    def load_market_score(self, date=None):
        """跨日 S 打分曲线 + 当日盘中曲线。"""
        out = {"history": [], "intraday": []}
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

        # 当日盘中曲线（jsonl 逐行容错）
        if date:
            fp = IDX_REGIME / "traces" / f"index_regime_{date}.jsonl"
            if fp.exists():
                for line in open(fp, encoding="utf-8"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    out["intraday"].append({
                        "ts": r.get("ts"), "time": (r.get("ts") or "")[-8:],
                        "score": r.get("score"), "regime": r.get("regime"),
                        "regime_name": r.get("regime_name"),
                    })
        return _clean(out)

    # ---------- 持仓成本历史 ----------
    def load_cost_history(self):
        """读全部 holdings 快照，按股按日聚合 cost。"""
        dates, stocks = [], {}
        for fp in sorted(STATE_DIR.glob("holdings_*.json")):
            d = fp.stem.replace("holdings_", "")
            try:
                snap = json.loads(open(fp, encoding="utf-8").read())
            except Exception:
                continue
            if not snap:
                continue
            dates.append(d)
            for code, info in snap.items():
                if not isinstance(info, dict):
                    continue
                cost = info.get("cost")
                if cost is None:
                    continue
                st = stocks.setdefault(code, {"name": info.get("name", code), "points": []})
                st["points"].append({"date": d, "cost": float(cost)})
        return _clean({"dates": dates, "stocks": stocks})

    # ---------- 实时 console ----------
    KEY_LINE_WORDS = ["推送", "信号", "拦截", "阻断", "熔断", "告警", "建仓",
                      "策略卡", "竞价", "接回", "WARNING", "ERROR", "异常",
                      "熔断/仓控", "急跌", "追涨"]
    NOISE_LINE_WORDS = ["扫描心跳", "缓存", "数据更新完成", "poll", "网络重试"]

    def load_console(self, date, since=0):
        """增量读日志。since=字节偏移，返回新增行+新偏移。"""
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

    @staticmethod
    def _parse_log_line(line):
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

    @staticmethod
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
        out = {"signals": [], "intraday_state": {}, "market_intraday": []}

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
            # 取尾部最多 20 条非 HOLD + 补足 HOLD 到 20
            non_hold = [r for r in rows if r.get("decision") not in ("HOLD", None)]
            tail = (non_hold + [r for r in rows if r.get("decision") in ("HOLD", None)])[-20:]
            out["signals"] = [{
                "scan_time": r.get("scan_time"), "code": r.get("code"),
                "name": r.get("name"), "price": r.get("price"),
                "buy_score": r.get("buy_score"), "sell_score": r.get("sell_score"),
                "decision": r.get("decision"), "reason": r.get("decision_reason"),
            } for r in tail]

        out["intraday_state"] = _load_json(INTRADAY_STATE, {})
        out["market_intraday"] = self.load_market_score(date).get("intraday", [])
        return _clean(out)

    # ---------- 增量信号轮询（报警用） ----------
    SIGNAL_TYPES = ("BUY_LOW", "SELL_HIGH", "ADD_POS", "PANIC_SELL")

    def poll_new_signals(self, date):
        """增量读 decision_trace，返回自上次调用以来的新信号（非 HOLD）。
        首次调用建立基线不报警；文件轮转自动重置基线。"""
        out = {"signals": [], "baseline": True}
        fp = TRACES / f"decision_trace_{date}.jsonl"
        if not fp.exists():
            self._dt["date"] = None
            self._dt["offset"] = 0
            self._dt["seen"] = set()
            return out
        try:
            size = fp.stat().st_size
        except Exception:
            return out

        st = self._dt
        if st["date"] != date or size < st["offset"]:
            # 首次/切日期/文件轮转：建立基线（offset=当前末尾，旧行不重复处理）
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
            if r.get("decision") not in self.SIGNAL_TYPES:
                continue
            key = (r.get("scan_time"), r.get("code"), r.get("decision"))
            if key in st["seen"]:
                continue
            st["seen"].add(key)
            score = (r.get("buy_score") if r.get("decision") in ("BUY_LOW", "ADD_POS")
                     else r.get("sell_score"))
            out["signals"].append({
                "scan_time": r.get("scan_time"),
                "code": r.get("code"), "name": r.get("name"),
                "price": r.get("price"), "decision": r.get("decision"),
                "score": score, "reason": r.get("decision_reason"),
            })
        return _clean(out)

    # ---------- 内部聚合 ----------
    def _load_stage_board(self):
        sb = _load_json(OUT / "stage_board.json", {})
        return sb.get("stages", [])

    def _agg_position_builder(self, date):
        fp = TRACES / f"position_builder_{date}.jsonl"
        empty = {"has_data": False, "counts": {}, "by_code": {}, "rows": [], "cond_labels": COND_LABELS}
        if not fp.exists():
            return empty

        verdicts = Counter()
        by_code = {}
        try:
            lines = open(fp, encoding="utf-8").read().splitlines()
        except Exception:
            return empty

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            code = r.get("code")
            if not code:
                continue
            verdicts[r.get("verdict", "")] += 1
            st = r.get("scan_type", "manual")
            bucket = by_code.setdefault(code, {}).setdefault(
                st, {"latest": None, "best": None, "scans": 0}
            )
            bucket["scans"] += 1
            score = r.get("composite_score") or 0
            if bucket["latest"] is None or (r.get("scan_time") or "") > (
                bucket["latest"].get("scan_time") or ""
            ):
                bucket["latest"] = r
            if bucket["best"] is None or score > (bucket["best"].get("composite_score") or 0):
                bucket["best"] = r

        # 每 code 一行：eod best 优先，其次 intraday best
        rows = []
        for code, rec in by_code.items():
            eod = (rec.get("eod") or {}).get("best")
            intraday = (rec.get("intraday") or {}).get("best")
            row = dict(eod or intraday or {})
            row["_eod_best_score"] = (eod or {}).get("composite_score")
            row["_intraday_best_score"] = (intraday or {}).get("composite_score")
            row["_scans"] = sum(v.get("scans", 0) for v in rec.values())
            rows.append(row)
        rows.sort(key=lambda x: -(x.get("composite_score") or 0))

        return {
            "has_data": True,
            "counts": dict(verdicts),
            "by_code": by_code,
            "rows": rows,
            "cond_labels": COND_LABELS,
        }

    def _load_positions(self, date, kpi):
        current = _load_json(HOLDINGS, {})
        snap_today = {}
        snap_prev = {}
        prev_date = None

        fps = sorted(STATE_DIR.glob("holdings_*.json"))
        for fp in fps:
            d = fp.stem.replace("holdings_", "")
            if d == date:
                snap_today = _load_json(fp, {})
            if d < date and (prev_date is None or d > prev_date):
                prev_date = d
        if prev_date:
            snap_prev = _load_json(STATE_DIR / f"holdings_{prev_date}.json", {})

        t_mode_raw = _load_json(T_MODE, {})
        t_mode = {k: v for k, v in t_mode_raw.items() if not k.startswith("_")}
        auto = t_mode_raw.get("_auto_decision") or {}

        return {
            "current": current,
            "snapshot_today": snap_today,
            "snapshot_prev": snap_prev,
            "prev_date": prev_date,
            "t_mode": t_mode,
            "auto_decision": auto,
            "k2": (kpi or {}).get("K2_cost_change", {}),
            "k3": (kpi or {}).get("K3_base_drift", {}),
        }

    def _build_name_map(self, sig_stat, add_watch, pb, current):
        names = dict(NAMES)
        for src in (sig_stat, add_watch, current):
            for code, info in (src or {}).items():
                nm = info.get("name") if isinstance(info, dict) else None
                if nm:
                    names[code] = nm
        for code, rec in (pb.get("by_code") or {}).items():
            for bucket in rec.values():
                row = bucket.get("best") or bucket.get("latest")
                if row and row.get("name"):
                    names[code] = row["name"]
        return names


if __name__ == "__main__":
    import webview

    api = Api()
    here = Path(__file__).parent
    entry = here / "web" / "index.html"

    # 前端开发调试: 设置 WEBVIEW_DEBUG=1 打开 devtools
    debug = sys.argv[1] == "--debug" if len(sys.argv) > 1 else False

    window = webview.create_window(
        "做T复盘决策看板",
        str(entry),
        js_api=api,
        width=1440,
        height=920,
        min_size=(1100, 700),
    )
    webview.start(gui="edgechromium", debug=debug)
