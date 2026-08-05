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

    # ---------- 日期发现 ----------
    def available_dates(self):
        dates = set()
        for p in OUT.glob("daily_review_*.json"):
            stem = p.stem
            if stem.startswith("daily_review_") and len(stem) == len("daily_review_") + 10:
                dates.add(stem[len("daily_review_"):])
        return sorted(dates, reverse=True)

    # ---------- 单日完整载荷 ----------
    def load_day(self, date=None):
        if not date:
            dates = self.available_dates()
            if not dates:
                return {"date": None, "error": "未找到 daily_review_*.json，请先运行 daily_review.py"}
            date = dates[0]

        out = {"date": date}
        dr_path = OUT / f"daily_review_{date}.json"
        if not dr_path.exists():
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
