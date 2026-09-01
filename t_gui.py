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
  t_mode.json                                             T模式（正/反T）
"""
import json
import math
import sys
import threading
import time as _time_mod
from collections import Counter
from datetime import datetime
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
HOLDINGS = STATE_DIR / "holdings.json"
T_MODE = STATE_DIR / "t_mode.json"
IDX_REGIME = BASE / "t_io" / "index_regime"
LOGS_DIR = BASE / "t_io" / "logs"
INTRADAY_STATE = BASE / "t_io" / "intraday_state.json"
PORTFOLIO = STATE_DIR / "accounts_config.json"  # 2026-08-30 合并：账户配置唯一源头（原 portfolio_config.json 已并入）
PORTFOLIO_LEGACY = STATE_DIR / "portfolio_config.json"  # 旧部署回退（.gszq 等）
BRIDGE_DIR = BASE / "t_io" / "bridge"  # P4-2/3: 自动盘事件总线（heartbeat.json + events_*.jsonl + KILL_SWITCH）

# 内置名称映射（数据缺失 code 时兜底；可由 holdings/add_watch/trace 补充）
NAMES = {
    "000988": "华工科技", "588170": "科创半导体ETF华夏", "600176": "中国巨石",
    "600481": "双良节能", "603667": "五洲新春", "002639": "雪人集团",
    "300153": "科泰电源", "300364": "中文在线",
}

# W33 A1: 双通道 8 键标签（与 position_builder.CHANNEL_COND_KEYS 同序）
# 方案A (2026-08-15): 建仓条件=时机门控，与 position_builder.COND_LABELS 一致
COND_LABELS = {
    "t_regime": "市场有方向",
    "t_trend": "多头结构",
    "t_drawdown": "回撤到位",
    "t_golden": "MACD金叉(加分)",
}


PREOPEN_DIR = BASE / "t_io" / "preopen"
HUNTER_DIR = BASE / "stock_hunter"
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))  # stock_hunter 模块在 stock_hunter/ 下导入
if str(HUNTER_DIR) not in sys.path:
    sys.path.insert(0, str(HUNTER_DIR))

# 2026-08-15: 选股猎手后台运行状态（进度条轮询用）。进度细节来自 market_data.MARKET_PROGRESS。
import threading as _th
HUNTER_RUN_STATE = {"date": None, "running": False, "result": None}
ROTATION_RUN_STATE = {"running": False, "error": None}
# 2026-08-22: 板块轮动结果缓存（内存+磁盘）——build_rotation_model 约 15s，重复点击/切 view 秒回
_ROTATION_CACHE_DIR = BASE / "t_io" / "cache" / "sector_rotation"
_ROTATION_CACHE_MEM = {}
# 2026-08-23: 每日大盘复盘（LLM）后台线程状态
_REVIEW_RUN_STATE = {"running": False, "error": None}


def _jiuyan_concepts(info):
    """合并股票记录的所有韭研概念（编号字段 jiuyan_concept1..9 + 旧普通字段）。
    返回用 | 连接的去重字符串；无概念返回空串。"""
    if not isinstance(info, dict):
        return ""
    parts = []
    for i in range(1, 10):
        v = info.get(f"jiuyan_concept{i}")
        if v and str(v).strip():
            parts.append(str(v).strip())
    if not parts:
        v = info.get("jiuyan_concept")
        if v and str(v).strip():
            parts.append(str(v).strip())
    seen = set()
    out = []
    for p in parts:
        for c in p.split("|"):
            c = c.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return "|".join(out)


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


# 技术标签 TTL 缓存：GUI 每 10s 轮询 refresh_pb → load_stock_tags_batch（单次约 7-12s，
# 期间大量 pandas + 网络在 pywebview 主线程执行会冻结界面）。改为 TTL 缓存 + 后台异步重算，
# 轮询永远读缓存即时返回，界面不卡。TTL 取 120s：标签变化慢，过长 TTL 减少后台重算的 CPU 尖峰。
_TAGS_TTL = 120.0
_TAGS_CACHE: dict = {}
_TAGS_LOCK = threading.Lock()
_TAGS_RUNNING = False


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
        today = datetime.now().strftime("%Y-%m-%d")
        dates.add(today)  # 今天始终在首位（默认选中今天进入LIVE，即使盘前尚无数据）
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

    def _build_name_map(self, sig_stat, add_watch, pb, current):
        """汇总各数据源构建 {code: name} 映射，供信号条/持仓/结算显示股票名。
        （828fcea6 误删本方法只留调用，load_day 完整路径会 AttributeError；此处恢复原实现）"""
        names = dict(NAMES)
        for src in (sig_stat, add_watch, current):
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

    # ---------- 主要指数概览 ----------
    def load_indices(self):
        """拉主要指数实时行情 + 大盘 regime 状态，返回列表（点击可看K线）。
        腾讯指数(sh/sz) + 东财特殊指数(em，如 A股平均股价 em47.800005)。"""
        out = {"ts": None, "indices": [], "regime": None, "days_in_regime": None}
        try:
            import os as _os
            import urllib.request as _ur
            for _k in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                       "ALL_PROXY", "all_proxy"]:
                _os.environ.pop(_k, None)
            _os.environ["NO_PROXY"] = "*"
            tx_indices = [
                {"symbol": "sh000001", "name": "上证指数"},
                {"symbol": "sz399001", "name": "深证成指"},
                {"symbol": "sz399006", "name": "创业板指"},
                {"symbol": "sh000300", "name": "沪深300"},
                {"symbol": "sh000905", "name": "中证500"},
                {"symbol": "sh000688", "name": "科创50"},
                {"symbol": "sh000680", "name": "科创综指"},
            ]
            # 指数实时行情（P1-2 收敛：tencent_provider.index_auction，竞价/快照专用保留腾讯）
            from core.market_data.tencent_provider import TencentProvider
            idx_snaps = TencentProvider().index_auction([i["symbol"] for i in tx_indices])
            px = {}
            for code, d in idx_snaps.items():
                price = d.get("auction_price") or 0
                pre_close = d.get("pre_close") or 0
                px[code[2:]] = {
                    "price": price, "pre_close": pre_close,
                    "change": round(price - pre_close, 3) if price and pre_close else 0.0,
                    "change_pct": d.get("gap_pct") or 0.0,
                }
            for i in tx_indices:
                base = i["symbol"][2:]
                p = px.get(base)
                if p:
                    out["indices"].append({
                        "symbol": i["symbol"], "name": i["name"],
                        "price": p["price"], "change": p["change"],
                        "change_pct": p["change_pct"],
                    })
            # 东财特殊指数（A股平均股价等腾讯无代码的；用 push2delay 延迟行情，较稳定）
            em_list = [
                {"secid": "47.800005", "name": "A股平均股价"},
            ]
            for em in em_list:
                try:
                    url_em = (f"https://push2delay.eastmoney.com/api/qt/stock/get?"
                              f"secid={em['secid']}&fields=f43,f44,f45,f57,f58")
                    req_em = _ur.Request(url_em, headers={"User-Agent": "Mozilla/5.0",
                                                          "Referer": "https://quote.eastmoney.com/"})
                    data_em = _ur.urlopen(req_em, timeout=5).read().decode("utf-8", errors="ignore")
                    ed = (json.loads(data_em).get("data") or {})
                    price = (ed.get("f43") or 0) / 100.0
                    pre_close = (ed.get("f44") or 0) / 100.0
                    if price and pre_close:
                        chg = (price - pre_close) / pre_close * 100.0
                        out["indices"].append({
                            "symbol": "em" + em["secid"], "name": em["name"],
                            "price": price, "change": price - pre_close,
                            "change_pct": chg,
                        })
                except Exception:
                    continue
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
        wl = _load_json(STATE_DIR / "watchlist_buy.json", {})
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
        # P1-2 收敛：tencent_provider.snapshot_auction（快照/竞价专用保留腾讯）
        from core.market_data.tencent_provider import TencentProvider
        snaps = TencentProvider().snapshot_auction(list(symbols.keys()))

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
        for code, info in cur.items():
            if not isinstance(info, dict):
                continue
            if not (info.get("qty") or 0):
                continue  # fix 2026-08-20: 已清仓(qty=0)不进加仓观察
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
            # 补算日线指标（旧快照缺 daily_macd_golden/trend 等字段时，用日线现算）
            if daily_ctx.get("daily_macd_golden") is None or not daily_ctx.get("daily_trend_bg"):
                self._ensure_daily_ctx_indicators(code, daily_ctx)

            # fix P0-3: 日低改用 bars 的真实 low 最小值（分钟收盘价会丢下影线）；
            # 15:00 前"收盘"实为盘中现价，打 is_intraday 标志，文案统一"现价(盘中暂定)"
            closes = [float(b.get("close", 0)) for b in bars if b.get("close")]
            lows = [float(b.get("low", 0)) for b in bars if b.get("low")]
            if not closes:
                continue
            day_low = min(lows) if lows else min(closes)
            day_close = closes[-1]
            is_intraday = (date == datetime.now().strftime("%Y-%m-%d")
                           and datetime.now().strftime("%H:%M") < "15:00")
            px_word = "现" if is_intraday else "收"
            sfx = "(盘中暂定)" if is_intraday else ""
            # fix P2-16: ETF 标识，缺 MA/VWAP 时写"不适用(ETF)"
            is_etf = str(cur[code].get("type", "")).lower() == "etf"
            # fix P0-6/P1-8: VWAP 只读快照根级 last_vwap，缺失不回退 MA 冒充
            vwap_raw = snap.get("last_vwap") if isinstance(snap, dict) else None
            vwap_val = (float(vwap_raw) if vwap_raw
                        and not (isinstance(vwap_raw, float) and math.isnan(vwap_raw)) else None)

            # 支撑位
            raw_supports = {}
            for key, label in [("daily_ma5", "MA5"), ("daily_ma10", "MA10"),
                               ("daily_ma20", "MA20"), ("daily_ma60", "MA60")]:
                val = daily_ctx.get(key)
                if val and not (isinstance(val, float) and math.isnan(val)):
                    raw_supports[label] = float(val)
            # 近20日低点（fix P1-8: daily_20d_low 无生产端，不再回退 daily_support_level 冒充）
            low20 = daily_ctx.get("daily_20d_low")
            if low20 and not (isinstance(low20, float) and math.isnan(low20)):
                raw_supports["近20日低点"] = float(low20)
            if vwap_val:
                raw_supports["日内VWAP"] = float(vwap_val)
            # fix P1-8: 同一价位(差<0.1%)多标签去重合并为一个标签，如 "MA20/日内VWAP"
            supports = {}
            for label, val in raw_supports.items():
                hit = None
                for k, v in supports.items():
                    if v and abs(val - v) / v < 0.001:
                        hit = k
                        break
                if hit is not None:
                    if label not in hit.split("/"):
                        supports[hit + "/" + label] = supports.pop(hit)
                else:
                    supports[label] = val

            # 回踩事件：fix P1-6 触及窗放宽到 ±1.0%，基于修复后的真实日低(bars low)判定
            events, near = [], []
            for label, level in supports.items():
                dist = (day_low - level) / level * 100 if level else 0
                abs_dist = abs(dist)
                if abs_dist <= 1.0:
                    status = "守住" if day_close >= level else "破位"
                    events.append({"level": label, "support": round(level, 3),
                                   "dist%": round(dist, 2), "status": status})
                elif abs_dist <= 3:
                    stype = "刺穿收回" if (day_low < level and day_close >= level) else \
                            ("刺穿破位" if day_low < level else "临近未触")
                    near.append({"level": label, "support": round(level, 3),
                                 "dist%": round(dist, 2), "type": stype})

            # ===== 加仓两组判定：左侧(冰点) + 右侧(突破箱体) =====
            not_applicable = []
            # ---- 右侧加仓：突破箱体 ----
            bx = self.check_box_breakout(code)
            # P0修复：改为分级突破判定，仅"可靠级"及以上作为加仓条件
            right_breakout = bx.get("level") in ("reliable", "strong")  # 排除signal级的误报
            right_detail = (f"突破箱体上沿{bx.get('box',{}).get('high')}，"
                           f"超出{bx.get('pct_above')}%，等级:{bx.get('level')}"
                           if bx.get("broken") else "未突破箱体")

            # ---- 左侧加仓：情绪冰点（W33 A1 判据 + 5分钟确认）----
            # 日线冰点: 转向确认(金叉或站上MA5) AND BOLL冰点 AND 缩量；RSI 降展示层
            _mc_golden = bool(daily_ctx.get("daily_macd_golden"))
            _mc_rsi = daily_ctx.get("daily_rsi")
            _mc_boll = daily_ctx.get("daily_boll_pct")
            _mc_vol = daily_ctx.get("daily_vol_today")
            _mc_volma = daily_ctx.get("daily_vol_ma5")
            _mc_ma5 = daily_ctx.get("daily_ma5")
            _mc_price = daily_ctx.get("daily_price_ref")
            d_turn = _mc_golden or (_mc_ma5 is not None and _mc_price is not None
                                    and float(_mc_ma5) > 0 and float(_mc_price) > float(_mc_ma5))
            d_rsi = (_mc_rsi is not None and not (isinstance(_mc_rsi, float) and math.isnan(_mc_rsi)) and float(_mc_rsi) < 35)
            d_boll = (_mc_boll is not None and not (isinstance(_mc_boll, float) and math.isnan(_mc_boll)) and float(_mc_boll) <= 0.15)
            d_shrink = (_mc_vol is not None and _mc_volma is not None and _mc_volma > 0 and float(_mc_vol) / float(_mc_volma) < 0.8)
            daily_iceberg = d_turn and d_boll and d_shrink   # 转向 + 冰点2项全过（W33 冰点通道 signal 判据）

            # 5分钟冰点确认（盘中快照有分钟数据时）
            m5_iceberg = True
            m5_note = "盘后(无分钟数据)"
            if bars and len(bars) >= 30:
                try:
                    import pandas as _pd
                    from core.position_builder import resample_to_5min, add_5min_indicators
                    _df = _pd.DataFrame(bars)
                    if "time" in _df.columns:
                        _df["time"] = _pd.to_datetime(_df["time"], errors="coerce")
                    _df5 = add_5min_indicators(resample_to_5min(_df))
                    m5_hits = 0
                    if "dif_5m" in _df5.columns and "dea_5m" in _df5.columns:
                        _up = (_df5["dif_5m"] > _df5["dea_5m"]) & (_df5["dif_5m"].shift(1) <= _df5["dea_5m"].shift(1))
                        if bool(_up.tail(5).any()): m5_hits += 1
                    if "rsi_5m" in _df5.columns and not _pd.isna(_df5["rsi_5m"].iloc[-1]) and float(_df5["rsi_5m"].iloc[-1]) < 30:
                        m5_hits += 1
                    if "bb_pct_5m" in _df5.columns and float(_df5["bb_pct_5m"].iloc[-1]) <= 0.15:
                        m5_hits += 1
                    if "volume" in _df5.columns and len(_df5) >= 25:
                        _recent = _df5["volume"].tail(5).mean()
                        _prior = _df5["volume"].iloc[-25:-5].mean()
                        if _prior > 0 and _recent / _prior < 0.8:
                            m5_hits += 1
                    m5_iceberg = m5_hits >= 3
                    m5_note = f"5分钟冰点 {m5_hits}/4"
                except Exception:
                    m5_iceberg = True
                    m5_note = "5分钟计算失败"

            left_iceberg = daily_iceberg and m5_iceberg   # 左侧加仓 = 日线冰点 + 5分钟冰点

            left_conditions = [
                {"name": "转向确认", "met": d_turn,
                 "detail": "金叉或站上MA5=通过" if d_turn else "金叉/站上MA5=未过"},
                {"name": "RSI超卖(展示)", "met": d_rsi,
                 "detail": f"日线RSI={float(_mc_rsi):.1f}" if d_rsi or (_mc_rsi is not None and not math.isnan(_mc_rsi)) else "日线RSI=无"},
                {"name": "BOLL冰点", "met": d_boll,
                 "detail": f"日线bb_pct={float(_mc_boll):.3f}" if d_boll or (_mc_boll is not None and not math.isnan(_mc_boll)) else "日线BOLL=无"},
                {"name": "缩量止跌", "met": d_shrink,
                 "detail": "日线量<5日均量×0.8" if d_shrink else "日线量未缩"},
                {"name": "5分钟确认", "met": bool(m5_iceberg), "detail": m5_note},
            ]

            conditions = left_conditions + [{"name": "右侧突破箱体", "met": right_breakout, "detail": right_detail}]
            met_count = sum(1 for c in conditions if c["met"])
            # 加仓时机判定（timing_gate: 多头追强/空头抄底/震荡降频）——仅供 GUI 展示加仓是否被时机门控
            _tm = {}
            try:
                from core.timing_gate import timing_verdict as _timing_verdict
                _g = _timing_verdict(str(code).split("_")[0], datetime.now().strftime("%Y-%m-%d"))
                _tm = {"regime": _g["regime"], "go": _g["go"], "reason": _g["reason"]}
            except Exception:
                _tm = {}
            out[code] = {
                "name": cur[code].get("name", code),
                "day_low": round(day_low, 3), "close": round(day_close, 3),
                "is_intraday": bool(is_intraday),  # fix P0-3: True 时 close 实为现价
                "box_boost": right_breakout,  # 右侧突破箱体
                "left_iceberg": bool(left_iceberg),  # 左侧冰点(日线+5分钟)
                "daily_iceberg": bool(daily_iceberg),
                "right_breakout": right_breakout,
                "not_applicable": not_applicable,
                "vwap": round(float(vwap_val), 3) if vwap_val else None,
                "supports": {k: round(v, 3) for k, v in supports.items()},
                "events": events, "near": near,
                "conditions": conditions, "met_count": met_count,
                "timing": _tm,
            }

        total = len([c for c in cur if isinstance(cur.get(c), dict) and (cur.get(c, {}).get("qty") or 0) > 0])
        ok = len(out)
        # fix P1-10: _progress 为内部统计键，以 _ 前缀标识，前端按 _ 前缀过滤，不计入股票数
        out["_progress"] = {"total_holdings": total, "snapshots_ok": ok, "snapshots_miss": total - ok}
        return _clean(out)

    def _ensure_daily_ctx_indicators(self, code, daily_ctx):
        """补算 daily_ctx 的日线 MACD/趋势字段（旧快照缺失时）。就地更新 daily_ctx。"""
        try:
            import pandas as pd
            from core.position_builder import fetch_daily_kline
            df = fetch_daily_kline(str(code).split("_")[0])
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
        h = self.load_stock_chart(code)
        if not h.get("available"):
            return {"broken": False, "level": None, "error": h.get("error", "")}
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
        cur = qc[1].get(code) or qc[1].get(code.split("_")[0]) or h.get("current_price")
        if not cur:
            return {"broken": False, "level": None, "error": "无可用现价"}
        boxes = h.get("boxes", [])
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
    def load_ob_analysis(self, date=None):
        """每只持仓：日线超买指标(RSI/KDJ-J/CCI/BOLL) + 顶背离(MACD/RSI/KDJ/量价) + 建仓建议。"""
        import numpy as np
        import pandas as pd
        cur = _load_json(HOLDINGS, {})
        out = {"stocks": []}

        for code, info in cur.items():
            if not isinstance(info, dict) or code.startswith("_"):
                continue
            if not (info.get("qty") or 0):
                continue  # fix 2026-08-20: 已清仓(qty=0)不进入持仓体检
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

    # ---------- 日内冲高防御系统 ----------
    def load_intraday_surge_defense(self, date=None):
        """实时监控holdings+watchlist的冲高风险。

        返回:
        {
            "timestamp": "2026-08-25 14:30:00",
            "holdings_alerts": [...],        # 持仓风险告警
            "watchlist_alerts": [...],       # 监控风险告警
            "critical_alerts": [...],        # 需立即处理的
            "summary": {
                "safe_count": int,
                "warning_count": int,
                "avoid_count": int,
                "exit_count": int
            }
        }
        """
        out = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "holdings_alerts": [],
            "watchlist_alerts": [],
            "critical_alerts": [],
            "summary": {"safe_count": 0, "warning_count": 0, "avoid_count": 0, "exit_count": 0},
            "available": False,
            "error": ""
        }

        try:
            from intraday_surge_monitor import monitor_surge_risks
            result = monitor_surge_risks()

            # 转换为前端需要的格式
            out.update({
                "timestamp": result.get("timestamp", out["timestamp"]),
                "holdings_alerts": result.get("holdings_alerts", []),
                "watchlist_alerts": result.get("watchlist_alerts", []),
                "critical_alerts": result.get("critical_alerts", []),
                "available": True
            })

            # 统计摘要
            for alert in out["holdings_alerts"] + out["watchlist_alerts"]:
                action = alert.get("action", "")
                if action == "SAFE":
                    out["summary"]["safe_count"] += 1
                elif action == "WARNING":
                    out["summary"]["warning_count"] += 1
                elif action == "AVOID":
                    out["summary"]["avoid_count"] += 1
                elif action == "EXIT":
                    out["summary"]["exit_count"] += 1

        except ImportError:
            out["error"] = "冲高防御模块未安装"
        except Exception as e:
            out["error"] = f"加载失败: {str(e)[:100]}"

        return _clean(out)

    def get_surge_defense_alert_level(self):
        """返回当前最严重的告警等级 (normal|warning|critical)。用于UI顶部状态栏。"""
        try:
            result = self.load_intraday_surge_defense()

            if not result.get("available"):
                return "normal"

            if result.get("critical_alerts"):
                for alert in result["critical_alerts"]:
                    if alert.get("action") == "EXIT":
                        return "critical"
                return "warning"

            if result["summary"].get("exit_count", 0) > 0:
                return "critical"
            if result["summary"].get("avoid_count", 0) > 0:
                return "warning"
            if result["summary"].get("warning_count", 0) > 0:
                return "warning"

            return "normal"
        except Exception:
            return "normal"

    # ---------- 个股技术分析弹窗 ----------
    def load_stock_chart(self, code):
        """日线(本地缓存秒回/网络兜底) → 7 条 MA + MACD/RSI/BOLL → resample 周/月 → 支撑压力。
        支持带前缀指数代码(sh000001/sz399001 等)。内存缓存：同一标的当日结果复用。"""
        out = {"code": code, "name": code, "available": False, "error": ""}
        if not hasattr(self, "_stock_chart_cache"):
            self._stock_chart_cache = {}
        cache_key = f"{datetime.now().strftime('%Y-%m-%d')}_{code}"
        if cache_key in self._stock_chart_cache:
            _ts, _res = self._stock_chart_cache[cache_key]
            # fix P0-15: 盘前缓存的图缺今日K线(最后日期<今天)，或盘中超15分钟 → 重算，避免图停在昨日
            _dates = _res.get("period_data", {}).get("daily", {}).get("dates") or []
            _last = str(_dates[-1]) if _dates else ""
            _now = datetime.now()
            _today = _now.strftime("%Y-%m-%d")
            _stale = (_last < _today) or (_last == _today and (_now - _ts).total_seconds() > 15 * 60)
            if not _stale:
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

        # 股票优先本地日线缓存（当日秒回）；指数/东财无个股缓存，直接网络
        rows = []
        if not is_index and not is_em:
            try:
                from core.position_builder import fetch_daily_kline
                _df = fetch_daily_kline(code_str)
                if not _df.empty:
                    for _r in _df.itertuples(index=False):
                        rows.append({"date": str(_r.date), "open": float(_r.open),
                                     "close": float(_r.close), "high": float(_r.high),
                                     "low": float(_r.low), "volume": float(_r.volume)})
            except Exception:
                rows = []

        # 本地缓存不可用 → 走网络拉 400 根
        if not rows:
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
                # P1-2 收敛：market_data provider（gm 主源/腾讯兜底）
                from core.market_data import get_provider
                try:
                    df = get_provider().daily(code, 400)
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
            out = self._build_chart_from_df(df, out, code)
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
        self._stock_chart_cache[cache_key] = (datetime.now(), result)
        return result

    def _build_chart_from_df(self, df, out, code):
        """由日线 DataFrame 构建 K 线弹窗数据（MA/MACD/RSI/BOLL + 周/月 + 支撑箱体通道）。"""
        import pandas as pd

        def calc_ma_and_indicators(d):
            d = d.copy()
            for n in (5, 10, 20, 30, 60, 180, 365):
                d[f"ma{n}"] = d["close"].rolling(n).mean()
            ema12 = d["close"].ewm(span=12, adjust=False).mean()
            ema26 = d["close"].ewm(span=26, adjust=False).mean()
            d["dif"] = ema12 - ema26
            d["dea"] = d["dif"].ewm(span=9, adjust=False).mean()
            d["macd_hist"] = (d["dif"] - d["dea"]) * 2
            delta = d["close"].diff()
            gain = delta.clip(lower=0).rolling(14).mean()
            loss = (-delta.clip(upper=0)).rolling(14).mean()
            rs = gain / loss.replace(0, float("nan"))
            d["rsi"] = 100 - 100 / (1 + rs)
            d["boll_mid"] = d["close"].rolling(20).mean()
            d["boll_std"] = d["close"].rolling(20).std()
            d["boll_up"] = d["boll_mid"] + 2 * d["boll_std"]
            d["boll_dn"] = d["boll_mid"] - 2 * d["boll_std"]
            return d

        daily = calc_ma_and_indicators(df)
        weekly = calc_ma_and_indicators(df.resample("W-FRI", on="date").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna().reset_index())
        # 月线频率兼容：pandas≥2.2 用 "ME"，旧版用 "M"
        # 2026-08-24 fix: pandas 3.x 次版本为 0，原 split[1]>="2" 误判为旧 "M"(已移除)；用版本元组比较
        _maj, _min = (int(x) for x in pd.__version__.split(".")[:2])
        _month_freq = "ME" if (_maj, _min) >= (2, 2) else "M"
        monthly = calc_ma_and_indicators(df.resample(_month_freq, on="date").agg(
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
        out["levels"] = self._calc_support_resistance(daily)
        out["boxes"] = self._detect_boxes(daily)
        out["channel"] = self._detect_channel(daily)
        out["current_price"] = round(float(daily["close"].iloc[-1]), 3)
        out["available"] = True
        return out

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

    # ---------- 选股猎手（概念评分，与 Excel 报告一致） ----------
    def run_hunter(self, date=None):
        """后台运行选股猎手（拉取+评分），立即返回；前端轮询 hunter_progress 看进度。"""
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
                HUNTER_RUN_STATE["result"] = self._load_hunter_impl(date)
            except Exception as e:
                HUNTER_RUN_STATE["result"] = {"available": False, "error": f"选股猎手运行失败: {e}"}
            finally:
                HUNTER_RUN_STATE["running"] = False

        _th.Thread(target=_work, daemon=True).start()
        return {"started": True, "running": True, "date": date}

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

    def load_hunter(self, date=None):
        """选股猎手数据。若有进行中的后台运行→返回 running；有该日期结果→返回；否则同步跑（兼容）。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        if HUNTER_RUN_STATE.get("running") and HUNTER_RUN_STATE.get("date") == date:
            return {"available": False, "running": True}
        if HUNTER_RUN_STATE.get("date") == date and HUNTER_RUN_STATE.get("result"):
            return HUNTER_RUN_STATE["result"]
        # 无后台运行（如历史视图直接调用）→ 同步执行
        return self._load_hunter_impl(date)

    def _hunter_build_conformance(self, codes, date):
        """盘后计算各股建仓信号符合度（时机门控 GO：市场有方向/多头结构/回撤到位/金叉加分）。

        实盘盘中（当日 9:15-15:00）跳过以省资源；盘后/周末/历史日点击运行时计算。
        直接读 t_io/cache/daily_kline 日线缓存算特征（零网络，秒级；未缓存跳过显示"—"）。
        返回 {code: {go, regime, met, conds:{t_*:bool}, reason}}。"""
        today = datetime.now().strftime("%Y-%m-%d")
        now = datetime.now()
        _now_int = now.hour * 100 + now.minute
        if date == today and 915 <= _now_int <= 1500 and now.weekday() < 5:
            return {}  # 实盘盘中：省资源不计算
        result = {}
        try:
            import pandas as _pd
            from core.position_builder import _DAILY_CACHE_DIR
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
                _fp = _DAILY_CACHE_DIR / f"{str(code).split('_')[0]}.json"
                if not _fp.exists():
                    continue
                try:
                    rows = json.loads(_fp.read_text(encoding="utf-8")).get("rows", [])
                    if len(rows) < 61:
                        continue
                    _sub = [r for r in rows if str(r.get("date", "")) <= str(date)]
                    if len(_sub) < 61:
                        continue
                    c = _pd.Series([float(r["close"]) for r in _sub])
                    h = _pd.Series([float(r["high"]) for r in _sub])
                    price = float(c.iloc[-1])
                    ma20 = float(c.rolling(20).mean().iloc[-1])
                    ma60 = float(c.rolling(60).mean().iloc[-1])
                    rec_high = float(h.tail(20).max())
                    e12 = c.ewm(span=12, adjust=False).mean()
                    e26 = c.ewm(span=26, adjust=False).mean()
                    dif = e12 - e26
                    dea = dif.ewm(span=9, adjust=False).mean()
                    golden = bool(((dif > dea) & (dif.shift(1) <= dea.shift(1))).tail(5).any())
                    trend = bool(price > ma20 and price > ma60)
                    dd = price / rec_high - 1 if rec_high > 0 else 0.0
                    # RSI(14)（空头抄底超卖极值，与 timing_gate 一致）
                    _dlt = c.diff()
                    _gn = _dlt.clip(lower=0).rolling(14).mean()
                    _ls = (-_dlt.clip(upper=0)).rolling(14).mean()
                    _rsi = float((100 - 100 / (1 + _gn / _ls.replace(0, float("nan")))).iloc[-1]) if _ls.iloc[-1] and _ls.iloc[-1] > 0 else 50.0
                    if _regime == "trend_up":
                        dd_ok = dd >= -0.03
                        _dir_ok = True
                        _rsi_ok = True
                    elif _regime == "trend_dn":
                        dd_ok = dd < -0.10
                        _dir_ok = True
                        _rsi_ok = _rsi < float(_ETP.get("trend_dn_rsi_max", 20))
                    else:
                        dd_ok = False
                        _dir_ok = False
                        _rsi_ok = False
                    conds = {
                        "t_regime": _dir_ok,
                        "t_trend": trend,
                        "t_drawdown": dd_ok,
                        "t_golden": golden,
                    }
                    go = (_dir_ok and trend and dd_ok) if _regime == "trend_up" else (_dir_ok and dd_ok and _rsi_ok)
                    result[str(code)] = {
                        "go": bool(go),
                        "regime": _regime,
                        "met": sum(1 for v in conds.values() if v),
                        "conds": conds,
                        "reason": f"{_regime}: GO" if go else f"{_regime}: 降频",
                    }
                except Exception:
                    continue
        except Exception:
            pass
        return result

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
                watchlist_path = HUNTER_DIR / "watchlist_jiuyan.json"
                log.warning(f"[HUNTER] watchlist 加载失败")
                log.warning(f"  - watchlist_path: {watchlist_path}")
                log.warning(f"  - exists: {watchlist_path.exists()}")
                if watchlist_path.exists():
                    try:
                        test_data = json.loads(watchlist_path.read_text(encoding='utf-8'))
                        log.warning(f"  - file size: {watchlist_path.stat().st_size} bytes")
                        log.warning(f"  - records: {len(test_data)}")
                    except Exception as e:
                        log.warning(f"  - read error: {e}")
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
                            # 细分（韭研概念，| 分隔多分类）——供前端展开板块后按下一级分类分组
                            "concepts": [c.strip() for c in str(d.get("韭研概念", "") or "").split("|") if c.strip()],
                            "score": total, "d5": d5, "d6": d6, "d9": d9,
                            "change_pct": round(float(d.get("涨跌幅", 0) or 0), 2) if str(d.get("涨跌幅")) not in ("nan", "None", "") else 0.0,
                            "limit_up": int(float(d.get("涨停", 0) or 0)) if str(d.get("涨停")) not in ("nan", "None", "") else 0,
                        })
                    stocks.sort(key=lambda x: -x["score"])
                    sector_stocks[category] = stocks
            except Exception:
                pass  # 个股明细非致命

            # 盘后建仓信号符合度注入（实盘盘中跳过省资源；盘后点击"今日数据"时显示）
            try:
                from modules.market_data import MARKET_PROGRESS as _MP
                _MP.update({"phase": "计算建仓符合度", "done": 0, "total": len(codes), "msg": ""})
            except Exception:
                pass
            try:
                build_conf = self._hunter_build_conformance(codes, date)
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
            except Exception:
                pass
            # 建仓符合股靠前显示：GO(时机放行)优先 → 符合条件数 → 得分
            for cat, stocks in sector_stocks.items():
                stocks.sort(key=lambda s: (
                    -(1 if s.get("build_go") else 0),
                    -(s.get("build_met") or 0),
                    -(s.get("score") or 0),
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
            try:
                from modules.heat_tracker import save_daily_summary as _save_hs
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
            out["concept_trends"] = concept_trends
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

    def _stock_tags_one(self, code):
        """单只股票技术标签。返回 {trend, box_pos, tags:[{label,color}]}。"""
        import numpy as np
        import pandas as pd
        from core.position_builder import fetch_daily_kline
        df = fetch_daily_kline(code)
        if df.empty or len(df) < 30:
            return {"trend": "flat", "tags": []}
        # P0: ifzq K线主机被 WAF 501 拦截时 fetch_daily_kline 静默回退旧缓存（缺当日 forming bar），
        # 破5/10日线 等标签会用昨日收盘误判（现价已站上均线仍显示破线）。补当日实时 forming bar。
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

        # 破5日线/破10日线（现价处于均线下方）
        _ma5_s = pd.Series(closes).rolling(5).mean()
        _ma10_s = pd.Series(closes).rolling(10).mean()
        if not np.isnan(_ma5_s.iloc[-1]) and cur < _ma5_s.iloc[-1]:
            tags.append({"label": "破5日线", "color": "down"})
        if not np.isnan(_ma10_s.iloc[-1]) and cur < _ma10_s.iloc[-1]:
            tags.append({"label": "破10日线", "color": "down"})

        # 通道标签（放最前）
        trend_label = {"up": {"label": "上行", "color": "up"},
                       "down": {"label": "下行", "color": "down"},
                       "flat": {"label": "震荡", "color": "neutral"}}[trend]
        tags.insert(0, trend_label)

        box_pos = None
        if cur_box and cur_box["high"] > cur_box["low"]:
            box_pos = round((cur - cur_box["low"]) / (cur_box["high"] - cur_box["low"]), 2)
        return {"trend": trend, "box_pos": box_pos, "price": round(cur, 3), "tags": tags[:6]}

    def load_stock_tags_batch(self, codes):
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
        _cache_key = f"{today}:{_codes_fp}"

        def _compute():
            result = {}
            with ThreadPoolExecutor(max_workers=40) as ex:
                futures = {ex.submit(self._stock_tags_one, c): c for c in codes}
                for fut in as_completed(futures, timeout=90):
                    code = futures[fut]
                    try:
                        result[code] = fut.result()
                    except Exception:
                        result[code] = {"trend": "flat", "tags": []}
            return result

        def _cache_store(tags):
            with _TAGS_LOCK:
                if len(_TAGS_CACHE) > 60:  # 防指纹 key 无限增长：超阈值清空（重算成本可接受）
                    _TAGS_CACHE.clear()
                _TAGS_CACHE[_cache_key] = {"ts": _time_mod.time(), "tags": tags}

        with _TAGS_LOCK:
            cached = _TAGS_CACHE.get(_cache_key)
            if cached and (now - cached["ts"]) < _TAGS_TTL:
                return _clean({"tags": cached["tags"]})
            if cached:
                # 缓存过期 → 后台重算，先返回旧值，界面不阻塞
                if not _TAGS_RUNNING:
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
                return _clean({"tags": cached["tags"]})
        # 冷启动（无缓存）：同步算一次，仅首次加载会稍等，之后轮询不再阻塞
        tags = _compute()
        _cache_store(tags)
        return _clean({"tags": tags})

    # ---------- 突破箱体股票聚合 ----------
    def _breakout_pool_codes(self):
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        return [c for c, i in jy.items()
                if isinstance(i, dict) and c.isdigit()
                and _jiuyan_concepts(i).strip()]

    def _breakout_disk_path(self, today):
        return BASE / "t_io" / "cache" / f"breakout_{today}.json"

    def _scan_breakout(self, codes, state):
        """分批算技术标签筛"向上突破"。state 非空时更新进度（done/total/found/stocks）。"""
        jy = _load_json(HUNTER_DIR / "watchlist_jiuyan.json", {})
        breakouts = []
        _seen = set()  # 防同一股票重复（缓存污染遗留防御）
        for i in range(0, len(codes), 80):
            batch = codes[i:i + 80]
            r = self.load_stock_tags_batch(batch)
            for code, info in (r.get("tags", {}) or {}).items():
                if not info or code in _seen:
                    continue
                tags = info.get("tags", []) or []
                if any(t.get("label") == "向上突破" for t in tags):
                    _seen.add(code)
                    nm = jy.get(code, {}).get("name", code) if isinstance(jy.get(code), dict) else code
                    breakouts.append({
                        "code": code, "name": nm,
                        "price": info.get("price"),
                        "trend": info.get("trend"),
                        "tags": tags,
                    })
            if state is not None:
                state["done"] = min(i + 80, len(codes))
                state["found"] = len(breakouts)
                state["stocks"] = list(breakouts)
        breakouts.sort(key=lambda x: 0 if x["trend"] == "up" else 1)
        return breakouts

    def load_breakout_stocks(self):
        """同步全量扫描突破箱体（前端走后端后台线程时用 start_breakout_scan）。
        结果缓存到内存+磁盘（当日），避免重复扫描。"""
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        if not hasattr(self, "_breakout_cache"):
            self._breakout_cache = {}
        if cache_key in self._breakout_cache:
            return self._breakout_cache[cache_key]
        disk_fp = self._breakout_disk_path(today)
        if disk_fp.exists():
            disk = _load_json(disk_fp, None)
            if disk and isinstance(disk, dict) and "stocks" in disk:
                self._breakout_cache[cache_key] = disk
                return disk

        codes = self._breakout_pool_codes()
        if not codes:
            return {"stocks": [], "count": 0}
        breakouts = self._scan_breakout(codes, None)
        result = _clean({"stocks": breakouts, "count": len(breakouts)})
        self._breakout_cache[cache_key] = result
        try:
            disk_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        return result

    def start_breakout_scan(self, force=False):
        """启动后台突破扫描（幂等：内存/磁盘缓存命中→立即 done；扫描中→返回当前进度）。
        force=True 绕开当日缓存强制重扫（前端「🔄 重新扫描」，修复 2026-09-01 缓存永不更新的 bug）。
        返回 {status: idle|running|done|error, total, done, found, stocks, error?}。"""
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        if not hasattr(self, "_breakout_cache"):
            self._breakout_cache = {}
        if not force and cache_key in self._breakout_cache:
            r = self._breakout_cache[cache_key]
            return {"status": "done", "total": 0, "done": 0,
                    "found": r.get("count", 0), "stocks": r.get("stocks", [])}
        disk_fp = self._breakout_disk_path(today)
        if not force and disk_fp.exists():
            disk = _load_json(disk_fp, None)
            if disk and isinstance(disk, dict) and "stocks" in disk:
                self._breakout_cache[cache_key] = disk
                return {"status": "done", "total": 0, "done": 0,
                        "found": disk.get("count", 0), "stocks": disk.get("stocks", [])}

        cur = getattr(self, "_breakout_scan", None)
        if cur and cur.get("status") == "running":
            return cur

        codes = self._breakout_pool_codes()
        if not codes:
            return {"status": "done", "total": 0, "done": 0, "found": 0, "stocks": []}
        import threading
        state = {"status": "running", "total": len(codes), "done": 0, "found": 0, "stocks": []}
        self._breakout_scan = state

        def run():
            try:
                breakouts = self._scan_breakout(codes, state)
                result = _clean({"stocks": breakouts, "count": len(breakouts)})
                self._breakout_cache[cache_key] = result
                try:
                    disk_fp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                except Exception:
                    pass
                state.update({"status": "done", "done": len(codes), "found": len(breakouts), "stocks": breakouts})
            except Exception as e:
                state.update({"status": "error", "error": str(e)})

        threading.Thread(target=run, daemon=True).start()
        return state

    def get_breakout_scan(self):
        """轮询后台突破扫描进度。done 后返回完整结果（含磁盘/内存缓存命中）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = "breakout_" + today
        if hasattr(self, "_breakout_cache") and cache_key in self._breakout_cache:
            r = self._breakout_cache[cache_key]
            return {"status": "done", "total": 0, "done": 0,
                    "found": r.get("count", 0), "stocks": r.get("stocks", [])}
        cur = getattr(self, "_breakout_scan", None)
        if cur:
            return cur
        return {"status": "idle", "total": 0, "done": 0, "found": 0, "stocks": []}

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
                            sector_stocks.setdefault(sector, []).append(
                                {"code": code, "name": nm,
                                 "concepts": [c.strip() for c in str(concepts).split("|") if c.strip()],
                                 "score": 0, "d5": 0, "d6": 0,
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

    # ---------- 板块轮动（移植自 sector-rotation-v2） ----------

    @staticmethod
    def _rotation_df_to_rows(df):
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

    def get_auction_diagnosis(self, date=None):
        """加载竞价诊断报告（auction_diagnosis_{date}.json）"""
        if not date:
            date = datetime.now().strftime("%Y-%m-%d")
        fp = PREOPEN_DIR / f"auction_diagnosis_{date}.json"
        if not fp.exists():
            return None
        try:
            return json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            return None

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

    def load_auto_status(self):
        """P4-3 自动盘页：读 t_io/bridge（heartbeat.json + 当日 events + KILL_SWITCH）。
        返回 heartbeat{positions/cash/index_regime/index_score} + order/fill/reject/risk 计数
        + 最新 10 条事件 + kill_switch 状态。GM 格式持仓 key 经 codec 转内部码。"""
        hb = _load_json(BRIDGE_DIR / "heartbeat.json", {})
        out = {"heartbeat": None, "events": {"order": 0, "fill": 0, "reject": 0, "risk": 0},
               "latest": [], "kill_switch": (BRIDGE_DIR / "KILL_SWITCH").exists(),
               "bridge_dir": str(BRIDGE_DIR)}
        if hb:
            try:
                from core.market_data.codec import to_internal
            except Exception:
                to_internal = lambda g: str(g).split(".")[-1]
            positions = {}
            for gk, p in (hb.get("positions", {}) or {}).items():
                try:
                    positions[to_internal(str(gk))] = p
                except Exception:
                    positions[str(gk)] = p
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

    # ---------- 自动盘：建仓扫描 / 添加标的 / 手动建仓做T衔接（2026-08-30） ----------

    def _auto_pool_module(self):
        """加载 config/auto_pool.py（config 无 __init__.py，需把 config 目录入 path）。"""
        if str(BASE / "config") not in sys.path:
            sys.path.insert(0, str(BASE / "config"))
        try:
            import auto_pool
            return auto_pool
        except Exception:
            return None

    def _auto_pool_codes(self):
        """当前 auto 池 6 位码（基于 holdings.json 实时派生，不依赖 auto_pool 模块缓存——新增标的立即可见）。"""
        try:
            from src.holdings_repo import load_full
            full = load_full()
        except Exception:
            return []
        return [c for c, h in full.items()
                if isinstance(h, dict) and str(h.get("pool")) in ("auto", "both")]

    def load_auto_scan(self, date=None):
        """自动盘建仓扫描结果：读 TRACES/auto_scan_{date}.jsonl 聚合 + 合并 auto 池全量
        （未扫描/新增标的 → verdict=pending 待扫描行，保证添加后立即显示）。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        fp = TRACES / f"auto_scan_{date}.jsonl"
        latest = {}
        if fp.exists():
            for line in open(fp, encoding="utf-8").read().splitlines():
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
                if code not in latest or (r.get("scan_time") or "") > (latest[code].get("scan_time") or ""):
                    latest[code] = r
        # 合并 auto 池全量：不在 trace 的（新增/未扫）→ pending 待扫描行
        try:
            from src.holdings_repo import load_full
            full = load_full()
        except Exception:
            full = {}
        for code in self._auto_pool_codes():
            if code in latest:
                continue
            h = full.get(code) or {}
            latest[code] = {
                "code": code, "scan_time": "", "date": date,
                "name": h.get("name", code),
                "mirror_qty": int(h.get("mirror_qty") or 0),
                "held": bool(int(h.get("qty") or 0)),
                "verdict": "pending", "score": 0,
                "regime": "", "go": False, "reasons": [], "veto": [],
            }
        rows = sorted(latest.values(), key=lambda r: -(r.get("score") or 0))
        counts = Counter(r.get("verdict", "weak") for r in rows)
        return _clean({"has_data": True, "date": date, "rows": rows, "counts": dict(counts)})

    def run_auto_scan(self, date=None):
        """对 auto 池全量跑建仓判定（EOD 口径，df_1min=None 跳过 W35 日内确认），
        逐行追加 TRACES/auto_scan_{date}.jsonl，返回聚合结果。咨询性扫描，以引擎闸链为准。"""
        date = date or datetime.now().strftime("%Y-%m-%d")
        try:
            from src.holdings_repo import load_full
            from execution.auto.build_decision_auto import decide
            from core.market_data import get_provider
            from config import ENTRY_TIMING_PARAMS
        except Exception as e:
            return {"has_data": False, "error": f"依赖导入失败: {e}"}
        if self._auto_pool_module() is None:
            return {"has_data": False, "error": "auto_pool 不可用"}
        hold = load_full()
        try:
            prov = get_provider()
            idx = prov.index_daily("sh000001", 400)
        except Exception as e:
            return {"has_data": False, "error": f"指数数据获取失败: {e}"}
        fp = TRACES / f"auto_scan_{date}.jsonl"
        fp.parent.mkdir(parents=True, exist_ok=True)
        _scan_time = datetime.now().strftime("%H:%M:%S")
        for code in self._auto_pool_codes():  # 基于 holdings 实时派生（新增标的也扫），非 auto_pool 模块缓存
            row = {"code": code, "scan_time": _scan_time, "date": date,
                   "name": (hold.get(code) or {}).get("name", code),
                   "mirror_qty": int((hold.get(code) or {}).get("mirror_qty") or 0),
                   "held": bool(int((hold.get(code) or {}).get("qty") or 0))}
            try:
                df = prov.daily(code, 400)
                dec = decide(df, idx, date, params=ENTRY_TIMING_PARAMS, df_1min=None)
                row.update({"verdict": dec.get("verdict", "weak"),
                            "go": bool(dec.get("go")), "score": dec.get("score", 0),
                            "regime": dec.get("regime"), "reasons": dec.get("reasons", []),
                            "veto": dec.get("veto", []),
                            "data_insufficient": bool(dec.get("data_insufficient"))})
                _f = dec.get("features") or {}
                if _f.get("price"):
                    row["price"] = _f["price"]
                # 2026-08-31: 构造与手动盘建仓表一致的条件圆点（t_regime/t_trend/t_drawdown/t_golden + t_veto）
                _cond = {}
                _regime = dec.get("regime")
                _cond["t_regime"] = _regime in ("trend_up", "trend_dn")
                _cond["t_trend"] = bool(_f.get("trend_multihead"))
                _dd = _f.get("drawdown")
                if _dd is not None:
                    _cond["t_drawdown"] = (float(_dd) >= -0.03 if _regime != "trend_dn" else float(_dd) < -0.10)
                else:
                    _cond["t_drawdown"] = False
                _cond["t_golden"] = bool(_f.get("macd_golden_5d"))
                if dec.get("veto"):
                    _cond["t_veto"] = False
                row["conditions"] = _cond
            except Exception as e:
                row.update({"verdict": "scan_error", "error": str(e)[:80]})
            try:
                with open(fp, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:
                pass
        return self.load_auto_scan(date)

    def add_auto_stock(self, code, name, mirror_qty, type=None):
        """添加新股票到 auto 池（pool=auto + mirror_qty 目标底仓）→ 原子写 holdings.json；
        若 code 在 watchlist 且 pool=manual → 改 auto（防引擎 validate_pool_split 拒绝启动）。
        引擎需重启才含该标的。"""
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            return {"ok": False, "error": "代码须为 6 位数字"}
        try:
            mirror_qty = int(mirror_qty)
        except (TypeError, ValueError):
            return {"ok": False, "error": "目标底仓须为整数"}
        if mirror_qty < 100 or mirror_qty % 100 != 0:
            return {"ok": False, "error": "目标底仓须 ≥100 且为 100 的整数倍"}
        _ap = self._auto_pool_module()
        if _ap is not None and not _ap.is_manual(code):
            return {"ok": False, "error": f"{code} 已在 auto 池"}
        try:
            from src.holdings_repo import upsert_auto_entry, load_full
            from core.market_data.codec import to_gm
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # 已在 auto/both 池 → 拒绝（基于当前 holdings 而非 auto_pool 模块缓存，防重复添加）
        _cur = load_full().get(code) or {}
        if str(_cur.get("pool") or "") in ("auto", "both"):
            return {"ok": False, "error": f"{code} 已在 auto 池"}
        try:
            gm_symbol = to_gm(code)
        except Exception:
            gm_symbol = ("SHSE." if code.startswith(("6", "5")) else "SZSE.") + code
        if type is None:
            type = "etf" if code.startswith("5") else "stock"
        try:
            upsert_auto_entry(code, name=name or code, gm_symbol=gm_symbol,
                              type=type, mirror_qty=mirror_qty,
                              actor="gui", reason="添加自动盘标的")
        except Exception as e:
            return {"ok": False, "error": f"写 holdings 失败: {e}"}
        try:
            wl_fp = STATE_DIR / "watchlist_buy.json"
            wl = _load_json(wl_fp, {})
            stocks = wl.get("stocks", {})
            if isinstance(stocks, dict) and isinstance(stocks.get(code), dict):
                if str(stocks[code].get("pool") or "manual") == "manual":
                    stocks[code]["pool"] = "auto"
                    tmp = wl_fp.with_suffix(".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(wl, f, ensure_ascii=False, indent=2)
                    tmp.replace(wl_fp)
        except Exception:
            pass
        return {"ok": True, "code": code, "gm_symbol": gm_symbol, "type": type,
                "mirror_qty": mirror_qty, "restart_required": True,
                "msg": f"已加入 auto 池（目标底仓 {mirror_qty}），重启掘金策略后生效"}

    def manual_auto_build(self, code, qty, action="build"):
        """自动盘手动建仓/加仓入口（仅限 auto 池内）：写 holdings.json（mirror_qty 设/加）+
        写 AUTO_BUILD.json 武装标记（引擎重启后 BASE 建仓跳过确认闸直接做T）。
        同时清除该 code 既有 BUY_PENDING 请求（防双通道）。"""
        code = str(code or "").strip()
        if action not in ("build", "add"):
            return {"ok": False, "error": "action 必须为 build/add"}
        if not (code.isdigit() and len(code) == 6):
            return {"ok": False, "error": "代码须为 6 位数字"}
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            return {"ok": False, "error": "数量须为整数"}
        if qty < 100 or qty % 100 != 0:
            return {"ok": False, "error": "数量须 ≥100 且为 100 的整数倍"}
        _ap = self._auto_pool_module()
        if _ap is not None and _ap.is_manual(code):
            return {"ok": False, "error": f"{code} 不在 auto 池（仅限 auto 池内已有股票）"}
        try:
            from src.holdings_repo import load_full, save_held_merged
        except Exception as e:
            return {"ok": False, "error": str(e)}
        full = load_full()
        entry = dict(full.get(code) or {})
        if not entry:
            return {"ok": False, "error": f"{code} 不在持仓真源"}
        old_mirror = int(entry.get("mirror_qty") or 0)
        new_mirror = qty if action == "build" else old_mirror + qty
        entry["mirror_qty"] = new_mirror
        if str(entry.get("pool") or "") == "manual":
            entry["pool"] = "auto"
        try:
            save_held_merged({code: entry}, actor="gui", reason="手动建仓/加仓武装")
        except Exception as e:
            return {"ok": False, "error": f"写 holdings 失败: {e}"}
        # 写 AUTO_BUILD.json 武装标记（GUI 直读直写 bridge，与 respond_buy_confirm 同款原子写）
        try:
            ab_fp = BRIDGE_DIR / "AUTO_BUILD.json"
            ab = _load_json(ab_fp, {}) or {}
            ab.setdefault("requests", {})
            ab["requests"][code] = {"action": action, "qty": new_mirror,
                                    "ts": datetime.now().timestamp()}
            ab["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tmp = ab_fp.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(ab, f, ensure_ascii=False, indent=2)
            tmp.replace(ab_fp)
        except Exception as e:
            return {"ok": False, "error": f"写武装标记失败: {e}"}
        # 清除该 code 的既有 BUY_PENDING 请求（防"武装"与"旧请求"双通道）
        try:
            bp_fp = BRIDGE_DIR / "BUY_PENDING.json"
            bp = _load_json(bp_fp, {}) or {}
            if code in (bp.get("pending") or {}):
                bp["pending"].pop(code, None)
                tmp = bp_fp.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(bp, f, ensure_ascii=False, indent=2)
                tmp.replace(bp_fp)
        except Exception:
            pass
        return {"ok": True, "code": code, "action": action, "mirror_qty": new_mirror,
                "restart_required": True,
                "msg": f"已武装 {'建仓' if action == 'build' else '加仓'} {new_mirror} 股，"
                       "重启掘金策略后引擎将自动建仓并开始做T"}

    def clear_auto_build(self, code):
        """撤销自动盘手动建仓/加仓武装标记（从 AUTO_BUILD.json 删除该 code）。"""
        try:
            ab_fp = BRIDGE_DIR / "AUTO_BUILD.json"
            ab = _load_json(ab_fp, {}) or {}
            if code in (ab.get("requests") or {}):
                ab["requests"].pop(code, None)
                ab["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                tmp = ab_fp.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(ab, f, ensure_ascii=False, indent=2)
                tmp.replace(ab_fp)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def load_auto_build_armed(self):
        """读 AUTO_BUILD.json 武装标记（手动建仓/加仓，引擎待消费，一次性）。"""
        ab = _load_json(BRIDGE_DIR / "AUTO_BUILD.json", {}) or {}
        return _clean({"requests": ab.get("requests") or {}})

    def remove_auto_stock(self, code):
        """从 auto 池删除标的（仅 qty/base 均为 0 的候选；有持仓拒绝）。同步 watchlist pool 改回 manual。
        引擎需重启后不再含该标的。"""
        code = str(code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            return {"ok": False, "error": "代码须为 6 位数字"}
        try:
            from src.holdings_repo import load_full, delete_entry
        except Exception as e:
            return {"ok": False, "error": str(e)}
        full = load_full()
        entry = full.get(code)
        if not entry:
            return {"ok": False, "error": f"{code} 不在持仓真源"}
        if int(entry.get("qty") or 0) > 0 or int(entry.get("base") or 0) > 0:
            return {"ok": False, "error": f"{code} 有持仓（qty>0），不能从 auto 池删除"}
        try:
            delete_entry(code, actor="gui", reason="删除auto标的")
        except Exception as e:
            return {"ok": False, "error": f"写 holdings 失败: {e}"}
        # watchlist 该 code pool 若为 auto → 改回 manual（防悬空 auto 标记）
        try:
            wl_fp = STATE_DIR / "watchlist_buy.json"
            wl = _load_json(wl_fp, {})
            stocks = wl.get("stocks", {})
            if isinstance(stocks, dict) and isinstance(stocks.get(code), dict):
                if str(stocks[code].get("pool") or "") == "auto":
                    stocks[code]["pool"] = "manual"
                    tmp = wl_fp.with_suffix(".tmp")
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(wl, f, ensure_ascii=False, indent=2)
                    tmp.replace(wl_fp)
        except Exception:
            pass
        return {"ok": True, "code": code,
                "msg": f"已从 auto 池删除 {code}（重启掘金策略后生效）"}

    def load_position_manager(self):
        """仓位管理器：每只持仓的目标/当前市值、资金占比、超配欠配。
        目标比例 = STOCK_PARAMS 个股 stock_qty_base_pct 或 全局默认。"""
        try:
            import config
        except Exception:
            config = None
        pcfg = _load_json(PORTFOLIO, {})
        accounts = pcfg.get("accounts", {})
        total_capital = sum(float(a.get("total_capital") or 0) for a in accounts.values())
        cur = _load_json(HOLDINGS, {})
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
            raw.append({"base": base, "name": info["name"], "raw_pct": raw_pct,
                        "mkt_val": mkt_val, "total_qty": total_qty,
                        "cost": (cost_total / total_qty) if total_qty else 0})
        # W33 A3: 归一化/欠配缺口/分批 抽到 config.build_position_gap 共享（避免 GUI/扫描器两处漂移）
        _cost_map = {r["base"]: r["cost"] for r in raw}
        gap_ctx = config.build_position_gap(total_capital, raw, default_pct) if config else None
        rows = []
        for r in (gap_ctx["rows"] if gap_ctx else []):
            row = dict(r)
            row["cost"] = round(_cost_map.get(r["code"], 0), 3) if r.get("total_qty") else 0
            rows.append(row)
        rows.sort(key=lambda x: -x["pct"])
        return _clean({
            "total_capital": round(total_capital, 0),
            "rows": rows,
            "sum_mkt": round(sum(r["mkt_val"] for r in rows), 0),
            "sum_pct": round(sum(r["pct"] for r in rows), 1),
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

    def add_to_watchlist(self, code, name):
        """将股票加入 watchlist_buy.json（status=monitoring）。"""
        fp = STATE_DIR / "watchlist_buy.json"
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
        idx_info = (record.get("timing") or {}).get("index") or {}
        close, up_line, dn_line, ma60 = (idx_info.get("close"), idx_info.get("up_line"),
                                         idx_info.get("dn_line"), idx_info.get("ma60"))
        if not (close and up_line and dn_line):
            try:
                import json as _json
                _cache = TRACES.parent / "cache" / "daily_kline" / "index_sh000001.json"
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
                tag_res = self.load_stock_tags_batch(codes)
                tags_map = tag_res.get("tags", {})
                for r in result.get("rows", []):
                    code = r.get("code")
                    info = tags_map.get(code, {})
                    if info:
                        r["tags"] = info.get("tags", [])
                        r["trend"] = info.get("trend")
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
        except Exception:
            pass
        return result

    def recompute_pb(self, date):
        """盘后重跑建仓扫描 + 重算加仓观察。返回 {position_builder, add_watch, error?}。
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
        # 2) 聚合新 trace（含技术标签）+ 重算加仓
        pb = self.refresh_pb(date)
        aw = self.compute_add_watch(date)
        out = {"position_builder": pb, "add_watch": aw}
        if err:
            out["error"] = err
        return _clean(out)

    # ---------- 内部聚合 ----------
    def _load_stage_board(self):
        sb = _load_json(OUT / "stage_board.json", {})
        return sb.get("stages", [])

    def _agg_position_builder(self, date, filter_high_confidence=False):
        fp = TRACES / f"position_builder_{date}.jsonl"
        wl = _load_json(STATE_DIR / "watchlist_buy.json", {})
        wl_stocks = wl.get("stocks", {})
        holdings = _load_json(HOLDINGS, {})
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
        # 手动盘建仓表可见性（2026-08-30）：manual 候选 + 实际持仓股；auto 池且未持有 → 隐藏（属自动盘 tab）。
        # 判定用 holdings 派生的 auto_pool.is_manual（权威源），不用 watchlist 的 pool 字段（可能过时，
        # 如 600481/002451 在 watchlist 标 auto 但 holdings 是 both 且 qty>0 → _is_holding 放行）。
        if str(BASE / "config") not in sys.path:
            sys.path.insert(0, str(BASE / "config"))
        try:
            import auto_pool as _auto_pool_mod
        except Exception:
            _auto_pool_mod = None

        def _visible(code: str) -> bool:
            if _is_holding(code):
                return True                       # 持仓股始终显示
            if _auto_pool_mod is not None and not _auto_pool_mod.is_manual(code):
                return False                      # auto 池且未持有 → 隐藏
            return code in wl_stocks              # manual 候选

        for code, r in latest_by_code.items():
            if not _visible(code):
                continue
            verdicts[r.get("verdict", "")] += 1

        # 扫描过的：正常聚合
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

        return {
            "has_data": True,
            "counts": dict(verdicts),
            "by_code": by_code,
            "rows": self._filter_high_confidence_signals(rows) if filter_high_confidence else rows,
            "cond_labels": COND_LABELS,
            "note": note,
            "progress": progress,
        }

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

    @staticmethod
    def _format_divergence_summary(div_detail: dict) -> str:
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
        current = _load_json(HOLDINGS, {})
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


if __name__ == "__main__":
    import webview

    api = Api()
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
