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
HUNTER_DIR = BASE / "stock_hunter"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))  # stock_hunter 模块在 stock_hunter/ 下导入
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
        # 顶背离报警去重：{date: set(codes)}
        self._div_alerted = {}

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
        # 加仓观察为空 或 缺 conditions（突破箱体条件）→ 实时计算
        dr_aw = out["add_watch"]
        if not dr_aw or not any(
            isinstance(v, dict) and "conditions" in v for v in dr_aw.values()):
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
        """拉腾讯实时行情（持仓 + watchlist 候选股），失败回退 pre_close。"""
        cur = dict(_load_json(HOLDINGS, {}))
        # 合并 watchlist_buy 候选股（非持仓的也拉，供建仓表实时价）
        wl = _load_json(BASE / "watchlist_buy.json", {})
        for code, info in (wl.get("stocks", {}) or {}).items():
            if code not in cur and isinstance(info, dict):
                cur[code] = {"name": info.get("name", code), "qty": 0, "cost": None,
                             "pre_close": 0, "in_watchlist": True}
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
            # 条件0（第一优先级）: 突破箱体上沿
            bx = self.check_box_breakout(code)
            conditions.append({"name": "突破箱体", "met": bool(bx.get("broken")),
                               "detail": (f"突破箱体上沿 {bx.get('box',{}).get('high')}，超出{bx.get('pct_above')}%"
                                          if bx.get("broken") else "未突破箱体")})
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
            # 突破箱体 = 强加分项，但需至少2个其它条件满足才满（防单条件误报）
            if conditions[0]["met"] and met_count >= 3:
                met_count = len(conditions)
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

    # ---------- 突破箱体判定（建仓+加仓共用） ----------
    def check_box_breakout(self, code):
        """判定是否突破当前箱体上沿（只认 rel=0 当前箱体，历史箱体不算）。
        返回 {broken, box, price, pct_above}。"""
        h = self.load_stock_chart(code)
        if not h.get("available"):
            return {"broken": False, "error": h.get("error", "")}
        cur = h.get("current_price")
        boxes = h.get("boxes", [])
        # 只认当前箱体（rel=0，现价在箱体内或刚出界）
        cur_boxes = [b for b in boxes if b.get("rel") == 0]
        # 现价 > 当前箱体上沿 → 突破
        for box in cur_boxes:
            if cur > box["high"]:
                pct_above = (cur - box["high"]) / box["high"] * 100 if box["high"] else 0
                if 0.3 <= pct_above <= 8:
                    return {"broken": True, "box": {"low": box["low"], "high": box["high"]},
                            "price": cur, "pct_above": round(pct_above, 2)}
                return {"broken": False, "price": cur,
                        "near_box": {"low": box["low"], "high": box["high"]},
                        "pct_above": round(pct_above, 2),
                        "reason": "已远离当前箱体" if pct_above > 8 else "未达突破阈值"}
        # 无当前箱体或现价在箱体内 → 未突破
        return {"broken": False, "price": cur}

    # ---------- 持仓日线超买/顶背离体检 ----------
    def load_ob_analysis(self, date=None):
        """每只持仓：日线超买指标(RSI/KDJ-J/CCI/BOLL) + 顶背离(MACD/RSI/KDJ/量价) + 建仓建议。"""
        import numpy as np
        import pandas as pd
        cur = _load_json(HOLDINGS, {})
        out = {"stocks": []}

        for code, info in cur.items():
            if not isinstance(info, dict) or code.startswith("_"):
                continue
            base_code = code.split("_")[0]  # 000988_B → 000988
            h = self.load_stock_chart(base_code)
            if not h.get("available"):
                out["stocks"].append({"code": code, "name": info.get("name", code),
                                      "error": h.get("error", "无数据")})
                continue
            d = h["period_data"]["daily"]
            closes = [x[1] for x in d["ohlc"]]
            highs = [x[3] for x in d["ohlc"]]
            lows = [x[2] for x in d["ohlc"]]
            volumes = d["volume"]
            rsi = d["rsi"]
            dif = d["macd"]["dif"]
            boll_up = d["boll"]["up"]
            n = len(closes)
            if n < 30:
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
            cur_close = closes[-1]
            cur_boll = boll_up[-1] if boll_up and boll_up[-1] is not None else 0
            ob = {
                "rsi": bool(cur_rsi > 70),
                "kdj": bool(cur_j > 100),
                "cci": bool(cur_cci > 100),
                "boll": bool(cur_boll and cur_close > cur_boll),
            }
            ob["count"] = sum(1 for v in ob.values() if v)

            # 顶背离检测（近60日）
            div = {"macd": False, "rsi": False, "kdj": False, "vol": False}
            win = range(max(2, n - 60), n)
            # 找近60日两个局部价格高点
            highs_list = list(win)
            price_peaks = []
            for i in range(2, len(win) - 2):
                idx = list(win)[i]
                if highs[idx] >= highs[idx - 1] and highs[idx] >= highs[idx - 2] and \
                   highs[idx] >= highs[idx + 1] and highs[idx] >= highs[idx + 2]:
                    price_peaks.append(idx)
            if len(price_peaks) >= 2:
                p2, p1 = price_peaks[-2], price_peaks[-1]
                # MACD 顶背离: 价创新高 但 DIF 未创新高
                if highs[p1] > highs[p2] and dif[p1] is not None and dif[p2] is not None and dif[p1] < dif[p2]:
                    div["macd"] = True
                # RSI 顶背离
                if highs[p1] > highs[p2] and rsi[p1] is not None and rsi[p2] is not None and rsi[p1] < rsi[p2]:
                    div["rsi"] = True
                # KDJ 顶背离
                if highs[p1] > highs[p2] and j_arr[p1] < j_arr[p2]:
                    div["kdj"] = True
                # 量价背离: 价新高 量萎缩
                if highs[p1] > highs[p2] and volumes[p1] < volumes[p2] * 0.9:
                    div["vol"] = True
            div["count"] = sum(1 for v in div.values() if v)

            # 趋势方向（通道下行=风险因子）
            ch = (h.get("channel") or {})
            trend_down = ch.get("direction") == "down"
            trend_up = ch.get("direction") == "up"

            # 风险提醒建议（超买/顶背离/趋势下行 → 提示减仓回避，非建仓建议）
            if div["count"] >= 1:
                risk = "高"
                advice = "🚨 顶背离风险：警惕见顶回落，建议减仓/回避"
            elif ob["count"] >= 2:
                risk = "高"
                advice = "⚠ 严重超买：短期高位风险，不建议追高，注意回落"
            elif ob["count"] == 1 and trend_down:
                risk = "中"
                advice = "⚠ 超买+趋势下行：偏空，反弹减仓"
            elif ob["count"] == 1:
                risk = "中"
                advice = "⚠ 轻微超买：注意短线回调"
            elif trend_down:
                risk = "中"
                advice = "⚠ 趋势下行：不追高，反弹减仓"
            else:
                risk = "低"
                advice = "✓ 无超买无下行：风险较低，持有/关注"

            out["stocks"].append({
                "code": code, "name": info.get("name", code),
                "price": cur_close,
                "trend": ch.get("direction", "flat"),
                "risk": risk,
                "overbought": {"rsi": round(cur_rsi, 1), "kdj": round(cur_j, 1),
                               "cci": round(cur_cci, 1), "boll": bool(ob["boll"]), "count": ob["count"]},
                "divergence": div,
                "advice": advice,
            })
        return _clean(out)

    # ---------- 严重顶背离报警 ----------
    def alert_severe_divergence(self, date=None):
        """检测严重顶背离（≥2指标背离），每日每股只报一次，推送飞书。
        返回 {alerts: [{code, name, price, div_types, message}]}。"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        today = date
        ob = self.load_ob_analysis()
        alerts = []
        day_set = self._div_alerted.setdefault(today, set())

        for s in ob.get("stocks", []):
            if "error" in s:
                continue
            dv = s.get("divergence", {})
            if dv.get("count", 0) < 2:
                continue  # 非严重（<2 指标背离）
            if s["code"] in day_set:
                continue  # 当日已报过
            day_set.add(s["code"])

            div_types = []
            if dv.get("macd"): div_types.append("MACD")
            if dv.get("rsi"): div_types.append("RSI")
            if dv.get("kdj"): div_types.append("KDJ")
            if dv.get("vol"): div_types.append("量价")
            message = (f"{s['name']} 严重顶背离! 现价{s['price']} "
                       f"{'+'.join(div_types)}双背离, 价格创新高但指标未创新高, 注意回调风险")
            alerts.append({
                "code": s["code"], "name": s["name"], "price": s["price"],
                "div_types": div_types, "message": message,
            })

            # 飞书推送
            try:
                from config import send_feishu_payload
                div_txt = " + ".join(div_types)
                card = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {"template": "red",
                                   "title": {"tag": "plain_text", "content": "🚨 严重顶背离警报"}},
                        "elements": [{"tag": "markdown", "content": (
                            f"**{s['name']}（{s['code']}）** 现价 {s['price']}\n\n"
                            f"**{div_txt} 双背离**\n\n"
                            f"→ 价格创新高但指标未创新高，注意回调风险\n\n"
                            f"⚠ 建议：不追高，警惕反转")}],
                    },
                }
                send_feishu_payload(card,
                                   success_log=f"严重顶背离飞书推送: {s['code']}",
                                   error_prefix=f"顶背离推送({s['code']})")
            except Exception:
                pass

        return _clean({"alerts": alerts})

    # ---------- 个股技术分析弹窗 ----------
    def load_stock_chart(self, code):
        """拉 400 根日线 → 7 条 MA + MACD/RSI/BOLL → resample 周/月 → 支撑压力。"""
        out = {"code": code, "name": code, "available": False, "error": ""}
        import os as _os, urllib.request as _ur
        for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"]:
            _os.environ.pop(_k, None)
        _os.environ["NO_PROXY"] = "*"

        symbol = ("sh" + code if code[0] in "56" else "sz" + code)
        try:
            url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,400,qfq"
            req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                            "Referer": "https://finance.qq.com/"})
            raw = _ur.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
            data = json.loads(raw)
        except Exception as e:
            out["error"] = f"拉取日线失败: {e}"
            return out

        try:
            stock_data = data.get("data", {}).get(symbol)
            kline = stock_data.get("day") or stock_data.get("qfqday") or []
            rows = []
            for item in kline:
                if isinstance(item, list) and len(item) >= 6:
                    rows.append({
                        "date": item[0], "open": float(item[1]), "close": float(item[2]),
                        "high": float(item[3]), "low": float(item[4]), "volume": float(item[5]),
                    })
            if not rows:
                out["error"] = "无日线数据"
                return out
            out["name"] = stock_data.get("qt", {}).get(symbol, [None, code])[1] or code
        except Exception as e:
            out["error"] = f"解析日线失败: {e}"
            return out

        try:
            import pandas as pd
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            def calc_ma_and_indicators(d):
                d = d.copy()
                for n in (5, 10, 20, 30, 60, 180, 365):
                    d[f"ma{n}"] = d["close"].rolling(n).mean()
                # MACD
                ema12 = d["close"].ewm(span=12, adjust=False).mean()
                ema26 = d["close"].ewm(span=26, adjust=False).mean()
                d["dif"] = ema12 - ema26
                d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
                d["macd_hist"] = (d["dif"] - d["dea"]) * 2
                # RSI(14)
                delta = d["close"].diff()
                gain = delta.clip(lower=0).rolling(14).mean()
                loss = (-delta.clip(upper=0)).rolling(14).mean()
                rs = gain / loss.replace(0, float("nan"))
                d["rsi"] = 100 - 100 / (1 + rs)
                # BOLL(20,2)
                d["boll_mid"] = d["close"].rolling(20).mean()
                d["boll_std"] = d["close"].rolling(20).std()
                d["boll_up"] = d["boll_mid"] + 2 * d["boll_std"]
                d["boll_dn"] = d["boll_mid"] - 2 * d["boll_std"]
                return d

            daily = calc_ma_and_indicators(df)
            weekly = calc_ma_and_indicators(df.resample("W-FRI", on="date").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index())
            monthly = calc_ma_and_indicators(df.resample("M", on="date").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index())

            def to_series(d):
                return {
                    "dates": [x.strftime("%Y-%m-%d") for x in d["date"]],
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

            out["period_data"] = {
                "daily": to_series(daily),
                "weekly": to_series(weekly),
                "monthly": to_series(monthly),
            }

                # 支撑/压力位（基于日线）
            levels = self._calc_support_resistance(daily)
            out["levels"] = levels
            # 箱体检测（基于日线）
            out["boxes"] = self._detect_boxes(daily)
            # 通道检测（基于日线）
            out["channel"] = self._detect_channel(daily)
            out["current_price"] = round(float(daily["close"].iloc[-1]), 3)
            out["available"] = True
        except Exception as e:
            out["error"] = f"计算失败: {e}"
            return out

        return _clean(out)

    def _detect_boxes(self, daily):
        """检测箱体：全历史滑窗 → 置信分排序，现价箱体/刚突破优先。
        分位数(88/12)定义边界，多次触及验证，置信分=触及+横盘+适中宽度。"""
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
        # 滑窗收集候选箱体（用区间位置唯一标识，避免重复）
        boxes = {}
        for start in range(0, n - WIN + 1, 3):
            seg = closes[start:start + WIN]
            slope = np.polyfit(np.arange(WIN), seg, 1)[0]
            rel_slope = abs(slope) / (seg.mean() or 1e-9)
            up = float(np.percentile(highs[start:start + WIN], 88))
            dn = float(np.percentile(lows[start:start + WIN], 12))
            up_touch = int(np.sum(highs[start:start + WIN] >= up * 0.992))
            dn_touch = int(np.sum(lows[start:start + WIN] <= dn * 1.008))
            width_pct = (up - dn) / (seg.mean() or 1e-9) * 100
            # 候选：横盘(斜率<0.5%/天) + 宽度3-22% + 双边触及≥2
            if rel_slope < 0.005 and 3.0 <= width_pct <= 22.0 and up_touch >= 2 and dn_touch >= 2:
                key = (round(up, 3), round(dn, 3))
                # 置信分：触及次数 + 横盘度 + 宽度适中
                conf = (up_touch + dn_touch) * 1.5 + max(0, 1 - rel_slope / 0.005) * 3 + (1 if 5 <= width_pct <= 15 else 0)
                if key not in boxes or conf > boxes[key]["conf"]:
                    s = dates[start].strftime("%Y-%m-%d") if hasattr(dates[start], "strftime") else str(dates[start])[:10]
                    e = dates[start + WIN - 1].strftime("%Y-%m-%d") if hasattr(dates[start+WIN-1], "strftime") else str(dates[start+WIN-1])[:10]
                    boxes[key] = {"start": s, "end": e, "low": round(dn, 3), "high": round(up, 3),
                                  "touches": (up_touch, dn_touch), "width": round(width_pct, 1),
                                  "conf": round(conf, 1), "rel": 0}

        # 关联现价关系 + 刚突破判定（箱体 end 距今 ≤20 天 且现价在上方 <15% 算"刚突破"）
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
            if b["low"] <= last_close <= b["high"]:
                b["rel"] = 0  # 现价在箱体内
            elif last_close > b["high"] and days_since <= 20 and (last_close - b["high"]) / b["high"] < 0.15:
                b["rel"] = 1  # 刚突破上方
            elif last_close > b["high"]:
                b["rel"] = -1  # 上方历史箱体
            else:
                b["rel"] = -2  # 下方历史箱体
            # 距现价距离（用于排序）
            b["dist"] = abs(b["center"] if "center" in b else (b["high"] + b["low"]) / 2 - last_close)
            result.append(b)

        # 合并重叠箱体（价格区间重叠>50% + 时间重叠 → 同一箱体；日期用字典序比较）
        def overlap(a, b):
            price_overlap = min(a["high"], b["high"]) - max(a["low"], b["low"])
            price_span = min(a["high"] - a["low"], b["high"] - b["low"])
            t_overlap = a["end"] > b["start"] and b["end"] > a["start"]
            return price_overlap > price_span * 0.5 and t_overlap

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

        # 重算 rel + center
        for b in merged:
            b["center"] = round((b["high"] + b["low"]) / 2, 3)
            if b["low"] <= last_close <= b["high"]:
                b["rel"] = 0
            elif last_close > b["high"]:
                b["rel"] = -1
            else:
                b["rel"] = -2

        # 排序：现价箱体(rel=0) > 刚突破 > 上方历史 > 下方历史；再按置信分
        merged.sort(key=lambda b: (
            0 if b["rel"] == 0 else 1 if b["rel"] == -1 else 2,
            -b["conf"]))
        return merged[:3]

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

    # ---------- 选股猎手（概念评分，与 Excel 报告一致） ----------
    def load_hunter(self, date=None):
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
                out["error"] = "watchlist 加载为空"
                return out

            df_pool = watchlist[watchlist["韭研概念"].str.strip().ne("")].copy()
            codes = list(dict.fromkeys(df_pool["代码"].astype(str).tolist()))

            st_codes = set()
            if "名称" in watchlist.columns:
                st_mask = watchlist["名称"].str.startswith(("*ST", "ST", "SST", "S*ST")).fillna(False)
                st_codes = set(watchlist.loc[st_mask, "代码"].astype(str).tolist())
            fetcher = MarketDataFetcher(data_dir=str(HUNTER_DIR / "data"), st_codes=st_codes)
            market_df = fetcher.fetch_for_date(codes, date)
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

            score_map = {str(s.get("代码", "")): s for s in scored}
            for col in ["总得分", "涨停", "D1强势形态且新高", "D2强势形态",
                        "D4首板资金池", "D5潜在突破10日", "D6潜在突破5日",
                        "D7持续性", "D8情绪分数", "D9活跃程度", "大成交额额外加分"]:
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
            for _, row in df_pool.iterrows():
                pass  # TOP5 用 ranker.select
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
                        stocks.append({
                            "name": d.get("名称", ""), "code": code,
                            "score": total, "d5": d5, "d6": d6, "d9": d9,
                            "change_pct": round(float(d.get("涨跌幅", 0) or 0), 2) if str(d.get("涨跌幅")) not in ("nan", "None", "") else 0.0,
                            "limit_up": int(float(d.get("涨停", 0) or 0)) if str(d.get("涨停")) not in ("nan", "None", "") else 0,
                        })
                    stocks.sort(key=lambda x: -x["score"])
                    sector_stocks[category] = stocks
            except Exception:
                pass  # 个股明细非致命

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

            out["available"] = True
            out["pool_size"] = len(codes)
            out["summary_cols"] = sum_cols
            out["summary_rows"] = sum_rows
            out["top5"] = top5_list
            out["heat_trends"] = heat_trends
            out["sector_stocks"] = sector_stocks
            out["concept_trends"] = concept_trends
            out["refreshed_at"] = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            out["error"] = f"排名生成失败: {e}"
            return out

        return _clean(out)

    # ---------- 批量通道标注（板块成分股） ----------
    def load_channel_batch(self, codes):
        """批量拉日线算通道方向（分批并发，支持全部成分股）。返回 {code: trend}。"""
        import threading
        from position_builder import fetch_daily_kline
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
    def _stock_tags_one(self, code):
        """单只股票技术标签。返回 {trend, box_pos, tags:[{label,color}]}。"""
        import numpy as np
        import pandas as pd
        from position_builder import fetch_daily_kline
        from position_builder import _detect_boxes_simple
        df = fetch_daily_kline(code)
        if df.empty or len(df) < 30:
            return {"trend": "flat", "tags": []}
        closes = df["close"].values
        highs = df["high"].values
        lows = df["low"].values
        volumes = df["volume"].values
        cur = float(closes[-1])
        n = len(closes)

        # 通道方向
        recent = df.tail(40)
        rc = recent["close"].values
        slope = np.polyfit(np.arange(len(rc)), rc, 1)[0]
        norm = slope / (rc.mean() or 1e-9)
        trend = "up" if norm > 0.0015 else ("down" if norm < -0.0015 else "flat")

        # 简化箱体
        boxes = _detect_boxes_simple(df)
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
                elif cur < lo:
                    tags.append({"label": "跌破下沿", "color": "down"})
        elif near_box and cur > near_box["high"] and (cur - near_box["high"]) / near_box["high"] <= 0.08:
            tags.append({"label": "向上突破", "color": "up"})

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

        # 背离（近60日局部高低点）
        win_idx = range(max(2, n - 60), n)
        idxs = list(win_idx)
        price_peaks = []
        price_troughs = []
        for i in range(2, len(idxs) - 2):
            idx = idxs[i]
            if highs[idx] >= highs[idx - 1] and highs[idx] >= highs[idx - 2] and \
               highs[idx] >= highs[idx + 1] and highs[idx] >= highs[idx + 2]:
                price_peaks.append(idx)
            if lows[idx] <= lows[idx - 1] and lows[idx] <= lows[idx - 2] and \
               lows[idx] <= lows[idx + 1] and lows[idx] <= lows[idx + 2]:
                price_troughs.append(idx)
        # MACD
        ema12 = pd.Series(closes).ewm(span=12, adjust=False).mean()
        ema26 = pd.Series(closes).ewm(span=26, adjust=False).mean()
        dif = (ema12 - ema26).values
        # RSI
        delta = pd.Series(closes).diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = (100 - 100 / (1 + rs)).values
        # 顶背离
        if len(price_peaks) >= 2:
            p2, p1 = price_peaks[-2], price_peaks[-1]
            if highs[p1] > highs[p2] and dif[p1] < dif[p2]:
                tags.append({"label": "顶背离", "color": "warn"})
        # 底背离
        if len(price_troughs) >= 2:
            t2, t1 = price_troughs[-2], price_troughs[-1]
            if lows[t1] < lows[t2] and dif[t1] > dif[t2]:
                tags.append({"label": "底背离", "color": "neutral"})

        # 超买/超卖
        cur_rsi = rsi[-1] if not np.isnan(rsi[-1]) else 50
        if cur_rsi > 70:
            tags.append({"label": "超买", "color": "warn"})
        elif cur_rsi < 30:
            tags.append({"label": "超卖", "color": "neutral"})

        # 通道标签（放最前）
        trend_label = {"up": {"label": "上行", "color": "up"},
                       "down": {"label": "下行", "color": "down"},
                       "flat": {"label": "震荡", "color": "neutral"}}[trend]
        tags.insert(0, trend_label)

        box_pos = None
        if cur_box and cur_box["high"] > cur_box["low"]:
            box_pos = round((cur - cur_box["low"]) / (cur_box["high"] - cur_box["low"]), 2)
        return {"trend": trend, "box_pos": box_pos, "tags": tags[:6]}

    def load_stock_tags_batch(self, codes):
        """批量拉技术标签（分批并发）。返回 {code: {trend, box_pos, tags}}。"""
        import threading
        codes = [str(c) for c in (codes or []) if c]
        result = {}
        lock = threading.Lock()

        def work(code):
            try:
                r = self._stock_tags_one(code)
                with lock:
                    result[code] = r
            except Exception:
                with lock:
                    result[code] = {"trend": "flat", "tags": []}

        for i in range(0, len(codes), 30):
            batch = codes[i:i + 30]
            threads = [threading.Thread(target=work, args=(c,), daemon=True) for c in batch]
            for t in threads: t.start()
            for t in threads: t.join(timeout=25)
        return _clean({"tags": result})

    # ---------- 选股猎手历史 ----------
    def available_hunter_dates(self):
        """返回 stock_hunter history 里所有日期（降序）。"""
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            dates = sorted(hist.keys(), reverse=True)
            return [{"date": d[:4] + "-" + d[4:6] + "-" + d[6:8]} for d in dates]
        except Exception:
            return []

    def load_hunter_history(self, date):
        """秒级读 history 快照（不重新拉行情）。date=YYYY-MM-DD。"""
        out = {"date": date, "available": False, "error": ""}
        try:
            from modules.heat_tracker import load_history as load_heat_history
            hist = load_heat_history()
            key = date.replace("-", "")
            items = hist.get(key, [])
            if not items:
                out["error"] = "该日期无历史数据"
                return out
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
                jy = _load_json(BASE / "watchlist_jiuyan.json", {})
                for code, info in jy.items():
                    if not isinstance(info, dict):
                        continue
                    # sector 字段 + jiuyan_concept 字段双源匹配（覆盖更全）
                    concepts = str(info.get("jiuyan_concept", "") or info.get("概念", ""))
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
                            sector_stocks.setdefault(sector, []).append(
                                {"code": code, "name": nm, "score": 0, "d5": 0, "d6": 0,
                                 "d9": 0, "change_pct": 0, "limit_up": 0})
                            break
            except Exception:
                pass

            out["available"] = True
            out["summary_cols"] = ["排名", "板块", "股票数量", "平均分", "涨停数", "热度", "趋势", "前三强"]
            out["summary_rows"] = rows
            out["sector_stocks"] = sector_stocks
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
        # 1. 精确代码: 腾讯行情直接查
        if query.isdigit() and len(query) == 6:
            symbol = "sh" + query if query[0] in "56" else "sz" + query
            try:
                url = f"https://qt.gtimg.cn/q={symbol}"
                req = _ur.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                "Referer": "https://gu.qq.com/"})
                raw = _ur.urlopen(req, timeout=5).read().decode("gbk", errors="replace")
                if "~" in raw:
                    f = raw.split('"')[1].split("~")
                    if len(f) > 3:
                        results.append({"code": query, "name": f[1]})
            except Exception:
                pass
        # 2. 名称模糊: 从 watchlist_jiuyan.json 匹配（{code: {name,...}} 或 list）
        if not results:
            try:
                jy = _load_json(BASE / "watchlist_jiuyan.json", {})
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

    def add_to_watchlist(self, code, name):
        """将股票加入 watchlist_buy.json（status=monitoring）。"""
        fp = BASE / "watchlist_buy.json"
        wl = _load_json(fp, {"stocks": {}, "total_capital": 300000, "max_per_stock_pct": 0.2})
        stocks = wl.setdefault("stocks", {})
        if code in stocks:
            stocks[code]["status"] = "monitoring"
        else:
            stocks[code] = {"name": name, "status": "monitoring", "composite_score": 0,
                            "criteria_met": {}, "suggested_qty": 0, "in_holdings": False}
        try:
            tmp = fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(wl, f, ensure_ascii=False, indent=2)
            tmp.replace(fp)
            return {"ok": True, "code": code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def remove_from_watchlist(self, code):
        """从 watchlist_buy.json 删除股票。"""
        fp = BASE / "watchlist_buy.json"
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
        wl = _load_json(BASE / "watchlist_buy.json", {})
        wl_stocks = wl.get("stocks", {})
        holdings = _load_json(HOLDINGS, {})
        empty = {"has_data": True, "counts": {}, "by_code": {}, "rows": [],
                 "cond_labels": COND_LABELS, "note": "", "progress": {}}

        verdicts = Counter()
        by_code = {}
        scanned_codes = set()
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
                verdicts[r.get("verdict", "")] += 1
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

        # 扫描过的：正常聚合
        rows = []
        for code, rec in by_code.items():
            eod = (rec.get("eod") or {}).get("best")
            intraday = (rec.get("intraday") or {}).get("best")
            row = dict(eod or intraday or {})
            row["_eod_best_score"] = (eod or {}).get("composite_score")
            row["_intraday_best_score"] = (intraday or {}).get("composite_score")
            row["_scans"] = sum(v.get("scans", 0) for v in rec.values())
            rows.append(row)

        # 未扫描的 watchlist 股票：作为"等待扫描"添加
        pending = 0
        for code, info in wl_stocks.items():
            if not isinstance(info, dict): continue
            if code in scanned_codes: continue
            if info.get("status") not in ("monitoring", "signal", None): continue
            in_hold = code in holdings and isinstance(holdings.get(code), dict)
            rows.append({
                "code": code, "name": info.get("name", code),
                "verdict": "pending", "composite_score": 0,
                "conditions": {},
                "suggested_qty": 0, "suggested_price": 0, "capital_required": 0,
                "in_holdings": in_hold, "scan_type": "等待扫描",
                "_scans": 0,
            })
            pending += 1

        rows.sort(key=lambda x: -(x.get("composite_score") or 0))
        verdicts["pending"] = pending
        no_data_count = verdicts.get("insufficient_data", 0)
        note_parts = []
        if no_data_count: note_parts.append(f"{no_data_count}只无快照")
        if pending: note_parts.append(f"{pending}只等待首次扫描")
        note = " · ".join(note_parts) if note_parts else ""

        total = len(wl_stocks)
        progress = {
            "total_candidates": total,
            "scanned": len(scanned_codes),
            "online_fetched": sum(1 for k in by_code if by_code[k].get("eod") or by_code[k].get("intraday")),
            "no_data": no_data_count,
            "pending": pending,
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
