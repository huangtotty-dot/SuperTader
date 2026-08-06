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
HOLDINGS = BASE / "holdings.json"
T_MODE = BASE / "t_mode.json"
IDX_REGIME = BASE / "t_io" / "index_regime"
LOGS_DIR = BASE / "t_io" / "logs"
INTRADAY_STATE = BASE / "t_io" / "intraday_state.json"
PORTFOLIO = STATE_DIR / "portfolio_config.json"

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


PREOPEN_DIR = BASE / "t_io" / "preopen"
HUNTER_DIR = Path(r"E:\stock_hunter")
if str(HUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(HUNTER_DIR))


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
        # 建仓/加仓信号增量轮询内存态
        self._pos = {"date": None, "offset": 0, "seen": set()}

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
                    "settle": {}, "watch": {}, "kpi": {},
                    "add_watch": self.compute_add_watch(date),
                    "positions": self._load_positions(date, {}),
                    "position_builder": self._agg_position_builder(date),
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
        out["add_watch"] = dr.get("add_watch", {})
        # 加仓观察为空时（如今天盘中无 daily_review）→ 实时计算
        if not out["add_watch"]:
            out["add_watch"] = self.compute_add_watch(date)
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

        out["portfolio_config"] = self.load_portfolio_config()
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
        """读全部 holdings 快照 + 人工校准文件，按股按日聚合 cost。
        校准优先（src=人工校准），否则快照值（src=快照）。"""
        dates, stocks = [], {}
        calib = _load_json(STATE_DIR / "cost_calibration.json", {}).get("calibrations", {})
        for fp in sorted(STATE_DIR.glob("holdings_*.json")):
            if "holdings_daily" in fp.stem:
                continue  # 跳过 holdings_daily_* 文件（结构不同）
            d = fp.stem.replace("holdings_", "")
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
        cur = _load_json(HOLDINGS, {})
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
    def load_quotes(self):
        """拉腾讯实时行情（持仓），失败回退 pre_close。"""
        cur = _load_json(HOLDINGS, {})
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
        try:
            import os as _os
            import urllib.request as _ur
            for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "all_proxy"]:
                _os.environ.pop(_k, None)
            _os.environ["NO_PROXY"] = "*"
            url = "https://qt.gtimg.cn/q=" + ",".join(symbols.values())
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                            "Referer": "https://gu.qq.com/"})
            data = _ur.urlopen(req, timeout=4).read().decode("gbk", errors="replace")
        except Exception:
            data = ""

        ts = datetime.now().strftime("%H:%M:%S")
        out["ts"] = ts
        if not data or "~" not in data:
            # 回退：用 pre_close
            out["source"] = "fallback"
            for code, info in cur.items():
                if not isinstance(info, dict):
                    continue
                pc = info.get("pre_close") or 0
                out["quotes"].append({
                    "code": code, "name": info.get("name", code),
                    "price": pc, "pre_close": pc, "change": 0.0, "change_pct": 0.0,
                    "cost": info.get("cost"), "pnl_pct": None, "offline": True,
                    "qty": info.get("qty", 0), "base": info.get("base", 0),
                    "t_qty": info.get("t_qty", 0),
                })
            self._write_daily_holdings(out)
            return out

        out["source"] = "live"
        # 1) 解析 API → {base_code: {price,pre_close,change,change_pct}}
        px = {}
        for line in data.splitlines():
            line = line.strip()
            if "=" not in line or '"' not in line:
                continue
            body = line[line.index('"') + 1: line.rindex('"')]
            f = body.split("~")
            if len(f) < 40:
                continue
            base = f[2]
            try:
                px[base] = {
                    "price": float(f[3]), "pre_close": float(f[4]),
                    "change": float(f[31]) if f[31] else 0.0,
                    "change_pct": float(f[32]) if f[32] else 0.0,
                }
            except (ValueError, IndexError):
                continue

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
                "cost": cost, "pnl_pct": pnl, "offline": False,
                "qty": info.get("qty", 0), "base": info.get("base", 0),
                "t_qty": info.get("t_qty", 0),
            })
        self._write_daily_holdings(out)
        return _clean(out)

    def save_daily_holdings(self):
        """写今日持仓每日快照（含数量/成本/盈亏）。"""
        return self._write_daily_holdings(self.load_quotes())

    def _write_daily_holdings(self, q):
        """数量/成本读用户每天更新的 holdings.json，盈亏按盘中实时价计算。"""
        today = datetime.now().strftime("%Y-%m-%d")
        fp = STATE_DIR / f"holdings_daily_{today}.json"
        cur = _load_json(HOLDINGS, {})
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
                "account": info.get("account", ""), "type": info.get("type", ""),
                "qty": qty, "base": info.get("base", 0), "t_qty": info.get("t_qty", 0),
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
        data = {
            "date": today,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": q.get("source", "fallback"),
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

    # ---------- 做T闭环盈亏（K1） ----------
    def load_trade_pnl(self, date):
        """读 closure_audit.jsonl 当天行，聚合 est_pnl（系统报警产生的做T闭环盈亏）。"""
        out = {"total_pnl": None, "by_code": {}, "source": "closure_audit", "note": ""}
        fp = LOGS_DIR / "closure_audit.jsonl"
        if not fp.exists():
            out["note"] = "closure_audit.jsonl 不存在"
            return out
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("date") != date:
                continue
            total = 0.0
            for d in r.get("details", []):
                code = d.get("code")
                pnl = d.get("est_pnl", 0) or 0
                sold = d.get("sold", 0) or 0
                bought = d.get("bought", 0) or 0
                out["by_code"][code] = {"pnl": round(pnl, 2), "sold": sold, "bought": bought}
                total += pnl
            out["total_pnl"] = round(total, 2) if total else 0.0
            out["source"] = "closure_audit"
            if not out["by_code"]:
                out["note"] = "当日无成交闭环"
            return _clean(out)
        out["note"] = "当日无 closure_audit 记录"
        return _clean(out)

    # ---------- 加仓观察（实时计算，不依赖 daily_review） ----------
    def compute_add_watch(self, date):
        """从分钟快照+最新价实时计算支撑位距离，返回 add_watch 同结构数据。
        替代 daily_review 收盘后才生成的静态 add_watch。"""
        out = {}
        cur = _load_json(HOLDINGS, {})
        if not cur:
            return out

        # 先尝试加载当前快照获取 daily_context（含 MA 支撑位）
        for code in cur:
            if not isinstance(cur[code], dict):
                continue
            # 找分钟快照
            ym = datetime.strptime(date, "%Y-%m-%d").strftime("%Y/%m") if len(date) == 10 else datetime.now().strftime("%Y/%m")
            snap_dir = SNAPSHOT_DIR = BASE / "t_io" / "minute_snapshots" / ym
            snap_fp = snap_dir / f"{code}_{date}.json"
            if not snap_fp.exists():
                # 尝试带后缀
                candidates = list(snap_dir.glob(f"{code}*{date}.json"))
                snap_fp = candidates[0] if candidates else None
            if not snap_fp or not snap_fp.exists():
                continue

            try:
                snap = json.loads(open(snap_fp, encoding="utf-8").read())
            except Exception:
                continue

            daily_ctx = snap.get("daily_context", {}) if isinstance(snap, dict) else {}
            bars = snap.get("bars", []) if isinstance(snap, dict) else (snap if isinstance(snap, list) else [])

            # 日高低收
            prices = [float(b.get("close", 0)) for b in bars if b.get("close")]
            if not prices:
                continue
            day_low = min(prices)
            day_close = prices[-1]
            vwap_val = daily_ctx.get("last_vwap") or daily_ctx.get("daily_support_level")

            # 支撑位
            supports = {}
            for key, label in [("daily_ma5", "MA5"), ("daily_ma10", "MA10"),
                               ("daily_ma20", "MA20"), ("daily_ma60", "MA60")]:
                val = daily_ctx.get(key)
                if val and not (isinstance(val, float) and math.isnan(val)):
                    supports[label] = float(val)
            # 近20日低点
            low20 = daily_ctx.get("daily_20d_low") or daily_ctx.get("daily_support_level")
            if low20 and not (isinstance(low20, float) and math.isnan(low20)):
                supports["近20日低点"] = float(low20)
            if vwap_val and not (isinstance(vwap_val, float) and math.isnan(vwap_val)):
                supports["日内VWAP"] = float(vwap_val)

            # 回踩事件：日低距支撑 ≤0.5% 记"触及"
            events, near = [], []
            for label, level in supports.items():
                dist = (day_low - level) / level * 100 if level else 0
                abs_dist = abs(dist)
                if abs_dist <= 0.5:
                    status = "守住" if day_close >= level else "破位"
                    events.append({"level": label, "support": round(level, 3),
                                   "dist%": round(dist, 2), "status": status})
                elif abs_dist <= 3:
                    stype = "刺穿收回" if (day_low < level and day_close >= level) else \
                            ("刺穿破位" if day_low < level else "临近未触")
                    near.append({"level": label, "support": round(level, 3),
                                 "dist%": round(dist, 2), "type": stype})

            # 加仓条件判定
            conditions = []
            # 条件1: 回踩支撑（日低距任意支撑 ≤2%）
            min_dist = min(
                (abs((day_low - lv) / lv * 100) for lv in supports.values() if lv > 0),
                default=100)
            nearest_name = min(
                ((abs((day_low - lv) / lv * 100), k) for k, lv in supports.items() if lv > 0),
                default=(100, ""))[1]
            retest_ok = min_dist <= 2.0
            conditions.append({"name": "回踩支撑", "met": retest_ok,
                               "detail": f"日低{day_low:.2f}距{nearest_name} {min_dist:.1f}%" if retest_ok else f"日低距最近支撑{nearest_name} {min_dist:.1f}%（需≤2%）"})
            # 条件2: 支撑守住（有回踩事件且收盘≥支撑）
            hold_ok = any(e["status"] == "守住" for e in events)
            conditions.append({"name": "支撑守住", "met": hold_ok,
                               "detail": "收盘≥支撑位" if hold_ok else ("触及但收盘跌破" if any(e["status"] == "破位" for e in events) else "未触发回踩")})
            # 条件3: 收盘高于MA5（短线不弱）
            ma5 = supports.get("MA5")
            above_ma5 = ma5 and day_close > ma5
            conditions.append({"name": "收盘>MA5", "met": bool(above_ma5),
                               "detail": f"收{day_close:.2f}>MA5 {ma5:.2f}" if above_ma5 else f"收{day_close:.2f}≤MA5 {ma5:.2f}" if ma5 else "无MA5数据"})
            # 条件4: VWAP确认（收盘>VWAP 或 近VWAP）
            vwap_f = float(vwap_val) if vwap_val else 0
            near_vwap = vwap_f and abs((day_close - vwap_f) / vwap_f * 100) <= 1.5
            conditions.append({"name": "VWAP确认", "met": bool(near_vwap),
                               "detail": f"收{day_close:.2f}距VWAP {vwap_f:.2f} {abs((day_close-vwap_f)/vwap_f*100):.1f}%" if vwap_f else "无VWAP"})

            met_count = sum(1 for c in conditions if c["met"])
            out[code] = {
                "name": cur[code].get("name", code),
                "day_low": round(day_low, 3), "close": round(day_close, 3),
                "vwap": round(float(vwap_val), 3) if vwap_val else None,
                "supports": {k: round(v, 3) for k, v in supports.items()},
                "events": events, "near": near,
                "conditions": conditions, "met_count": met_count,
            }

        total = len([c for c in cur if isinstance(cur.get(c), dict)])
        ok = len(out)
        out["_progress"] = {"total_holdings": total, "snapshots_ok": ok, "snapshots_miss": total - ok}
        return _clean(out)

    # ---------- 选股猎手（概念评分） ----------
    def load_hunter(self, date=None):
        """调用 stock_hunter 打分管线，返回概念排名+TOP5+板块明细。"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        out = {"date": date, "available": False, "error": ""}
        try:
            import pandas as pd
            from modules.data_loader import DataLoader as HLoader
            from modules.market_data import MarketDataFetcher
            from modules.scorer import ConceptScorer
            from modules.ranker import Top5Ranker, SectorRanker, DetailRanker
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
            out["error"] = f"加载 stock_hunter 配置失败: {e}"
            return out

        try:
            # 1. 加载 watchlist
            loader = HLoader(config=hunter_cfg)
            watchlist = loader.load_watchlist()
            if watchlist is None or watchlist.empty:
                out["error"] = "watchlist_jiuyan.json 加载为空"
                return out

            # 2. 过滤有概念的股票
            df_pool = watchlist[watchlist["韭研概念"].str.strip().ne("")].copy()
            codes = list(dict.fromkeys(df_pool["代码"].astype(str).tolist()))

            # 3. 获取行情
            st_codes = set()
            if "名称" in watchlist.columns:
                st_mask = watchlist["名称"].str.startswith(("*ST", "ST", "SST", "S*ST")).fillna(False)
                st_codes = set(watchlist.loc[st_mask, "代码"].astype(str).tolist())
            fetcher = MarketDataFetcher(
                data_dir=str(HUNTER_DIR / "data"), st_codes=st_codes)
            market_df = fetcher.fetch_for_date(codes, date)
            if "名称" in (market_df.columns if not market_df.empty else []):
                market_df = market_df.drop(columns=["名称"])
            if not market_df.empty:
                watchlist = watchlist.merge(market_df, on="代码", how="left")
                loader.set_watchlist(watchlist)
        except Exception as e:
            out["error"] = f"数据加载/行情获取失败: {e}"
            return out

        try:
            # 4. 打分
            dims = hunter_cfg.get("scoring", {}).get("dimensions", [])
            scorer = ConceptScorer(dimensions=dims if dims else None)
            stock_list = []
            for _, row in df_pool.iterrows():
                s = row.to_dict()
                s.setdefault("涨停", int(row.get("涨停", 0)) if pd.notna(row.get("涨停")) else 0)
                s.setdefault("连板天数", 0)
                s.setdefault("暗线概念数", len(str(s.get("韭研概念", "")).split("_")))
                stock_list.append(s)
            scored = scorer.compute_batch(stock_list)

            # 5. 排名
            top5_ranker = Top5Ranker()
            sector_ranker = SectorRanker()

            # 按概念聚类
            concept_map = {}
            for s in scored:
                for concept in str(s.get("韭研概念", "")).split("_"):
                    concept = concept.strip()
                    if not concept:
                        continue
                    concept_map.setdefault(concept, []).append(s)

            summary_rows = []
            for concept, stocks in concept_map.items():
                top5 = top5_ranker.select(stocks)
                avg_score = sum(x.get("总得分", 0) for x in stocks) / len(stocks) if stocks else 0
                summary_rows.append({
                    "concept": concept, "stock_count": len(stocks),
                    "top_stock": (top5[0].get("名称", "") if top5 else (stocks[0].get("名称", "") if stocks else "")),
                    "avg_score": round(avg_score, 1),
                    "top5": [{"name": t.get("名称", ""), "code": t.get("代码", ""),
                              "score": round(t.get("总得分", 0), 1),
                              "change_pct": round(t.get("涨跌幅", 0) or 0, 2)}
                             for t in (top5 or [])[:5]],
                })
            summary_rows.sort(key=lambda x: -(x["avg_score"] or 0))

            out["available"] = True
            out["pool_size"] = len(codes)
            out["scored_count"] = len(scored)
            out["concept_count"] = len(summary_rows)
            out["summary"] = summary_rows[:30]  # TOP 30 概念
            out["refreshed_at"] = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            out["error"] = f"打分/排名失败: {e}"
            return out

        return _clean(out)

    # ---------- 集合竞价信息层 ----------
    def load_auction(self, date):
        """读 t_io/preopen/auction_{date}.json，聚合并返回竞价摘要。"""
        fp = PREOPEN_DIR / f"auction_{date}.json"
        if not fp.exists():
            return {"date": date, "available": False}
        try:
            d = json.loads(open(fp, encoding="utf-8").read())
        except Exception:
            return {"date": date, "available": False}

        # 取最接近开盘的快照（优先 09:25，其次 09:28，否则第一个）
        snaps = d.get("snapshots", {})
        slot = snaps.get("09:25") or snaps.get("09:28")
        if not slot and isinstance(snaps, dict):
            slot = list(snaps.values())[0] if snaps else None

        rows = {}
        same_dir = {"up": 0, "down": 0, "flat": 0}
        if slot and isinstance(slot, dict):
            for code, r in (slot.get("rows") or {}).items():
                pct = r.get("pct_vs_preclose", 0) or 0
                price = r.get("auction_price") or r.get("open_approx")
                vol_vs_yday = r.get("auction_vol_vs_yday")
                rows[code] = {
                    "name": r.get("name", code),
                    "price": price,
                    "pre_close": r.get("pre_close"),
                    "pct": round(pct, 2),
                    "vol_hand": r.get("auction_vol_hand") or r.get("auction_vol_hand_approx"),
                    "amount_wan": r.get("amount_wan") or r.get("auction_amount_approx"),
                    "vol_vs_yday": round(vol_vs_yday * 100, 2) if vol_vs_yday is not None else None,
                    "yday_vol": r.get("yday_total_vol_hand"),
                }
                if pct > 0.5: same_dir["up"] += 1
                elif pct < -0.3: same_dir["down"] += 1
                else: same_dir["flat"] += 1

        total = sum(same_dir.values()) or 1
        bias = "偏多" if same_dir["up"] >= total * .6 else ("偏空" if same_dir["down"] >= total * .6 else "中性")
        gaps = d.get("gaps", [])
        return _clean({
            "date": date, "available": True,
            "slot_used": slot.get("ts") if isinstance(slot, dict) else None,
            "rows": rows,
            "same_dir": same_dir, "bias": bias, "total_stocks": total,
            "gaps": gaps, "has_gaps": len(gaps) > 0,
        })

    # ---------- 独立配置（账户总资金+已实现亏损） ----------
    def load_portfolio_config(self):
        """读 t_io/state/portfolio_config.json（独立于 holdings.json，用户更新持仓不会覆盖）。"""
        data = _load_json(PORTFOLIO, {})
        return _clean({
            "accounts": data.get("accounts", {}),
            "realized_loss": data.get("realized_loss", {}),
        })

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
                return {
                    "scan_time": r.get("scan_time"), "code": r.get("code"),
                    "name": r.get("name"), "price": r.get("price"),
                    "buy_score": bs, "sell_score": ss, "decision": r.get("decision"),
                    "reason": r.get("decision_reason"),
                    "buy_threshold": bt, "sell_threshold": st,
                    "near": near,
                }
            out["signals"] = [_sig(r) for r in tail]

        out["intraday_state"] = _load_json(INTRADAY_STATE, {})
        out["market_intraday"] = self.load_market_score(date).get("intraday", [])
        out["add_watch"] = self.compute_add_watch(date)
        return _clean(out)

    # ---------- 增量信号轮询（报警用） ----------
    SIGNAL_TYPES = ("BUY_LOW", "SELL_HIGH", "ADD_POS", "PANIC_SELL")
    # 飞书同款通知阈值（与 config.py PARAMS 对齐：notify_buy=68, sell=55, sell_early=65）
    NOTIFY_BUY = 68
    NOTIFY_SELL = 55
    NOTIFY_SELL_EARLY = 65

    def poll_new_signals(self, date):
        """增量读 decision_trace，仅返回飞书同款通知阈值以上的新信号。
        与 main.py scan_once 推送逻辑对齐：score >= notify_threshold 才报警。"""
        out = {"signals": [], "baseline": True}
        fp = TRACES / f"decision_trace_{date}.jsonl"
        if not fp.exists():
            self._dt["date"] = None; self._dt["offset"] = 0; self._dt["seen"] = set()
            return out
        try: size = fp.stat().st_size
        except Exception: return out

        st = self._dt
        if st["date"] != date or size < st["offset"]:
            st["date"] = date; st["offset"] = size; st["seen"] = set()
            return out
        if size <= st["offset"]:
            return {"signals": [], "baseline": False}

        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                f.seek(st["offset"]); data = f.read()
        except Exception: return out
        st["offset"] = size; out["baseline"] = False

        for line in data.splitlines():
            line = line.strip()
            if not line: continue
            try: r = json.loads(line)
            except Exception: continue
            if r.get("decision") not in self.SIGNAL_TYPES: continue
            key = (r.get("scan_time"), r.get("code"), r.get("decision"))
            if key in st["seen"]: continue
            score = (r.get("buy_score") if r.get("decision") in ("BUY_LOW", "ADD_POS")
                     else r.get("sell_score"))
            if score is None: continue
            dec = r.get("decision", "")
            # 飞书同款通知阈值过滤
            if dec in ("BUY_LOW", "ADD_POS"):
                if score < self.NOTIFY_BUY: continue
            else:
                ts = r.get("scan_time", "") or ""
                hour = 9
                if ts and len(ts) >= 13:
                    try: hour = int(ts[11:13])
                    except Exception: pass
                threshold = self.NOTIFY_SELL_EARLY if hour < 10 else self.NOTIFY_SELL
                if score < threshold: continue

            st["seen"].add(key)
            out["signals"].append({
                "scan_time": ts, "code": r.get("code"), "name": r.get("name"),
                "price": r.get("price"), "decision": dec,
                "score": score, "reason": r.get("decision_reason"),
            })
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

    # ---------- 轻量 PB 刷新（盘中实时） ----------
    def refresh_pb(self, date):
        """仅重读 position_builder jsonl 并返回聚合结果（不触发其他重载）。"""
        result = self._agg_position_builder(date)
        result["refreshed_at"] = datetime.now().strftime("%H:%M:%S")
        return result

    # ---------- 内部聚合 ----------
    def _load_stage_board(self):
        sb = _load_json(OUT / "stage_board.json", {})
        return sb.get("stages", [])

    def _agg_position_builder(self, date):
        fp = TRACES / f"position_builder_{date}.jsonl"
        empty = {"has_data": False, "counts": {}, "by_code": {}, "rows": [],
                 "cond_labels": COND_LABELS, "note": ""}
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
        no_data_count = verdicts.get("insufficient_data", 0)
        note = (f"含 {no_data_count} 只无分钟快照（不在持仓中，待采集器收集数据）"
                if no_data_count else "")

        total_candidates = sum(1 for k in by_code)
        online_ok = sum(1 for k in by_code
                        if by_code[k].get("eod") or by_code[k].get("intraday"))
        progress = {
            "total_candidates": total_candidates,
            "scanned": total_candidates,
            "online_fetched": online_ok,
            "no_data": no_data_count,
        }
        return {
            "has_data": True,
            "counts": dict(verdicts),
            "by_code": by_code,
            "rows": rows,
            "cond_labels": COND_LABELS,
            "note": note,
            "progress": progress,
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

        # 从独立配置文件读（不再依赖 holdings.json）
        pcfg = _load_json(PORTFOLIO, {})
        accounts = pcfg.get("accounts", {})
        return {
            "current": current,
            "accounts": accounts,
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
