# -*- coding: utf-8 -*-
"""
QMT 自动交易模块 (qmt_trader.py)  v0.1.0
================================================================
【重要】本模块默认 dry-run（模拟单）模式：
  - 本机未安装 xtquant / miniQMT 时，所有下单自动走"模拟单"路径，
    订单仅落盘 t_io/qmt_orders.jsonl，不会触达任何真实券商通道。
  - 接入实盘需同时满足：
      1) 安装 miniQMT 客户端并登录；
      2) pip install xtquant（QMT 官方 Python SDK）；
      3) config.json 增加配置节：
         "qmt": {
             "enabled": true,
             "dry_run": false,
             "account_id": "你的资金账号",
             "mini_qmt_path": "D:\\国金QMT交易端\\userdata_mini",
             "slippage": 0.002,
             "max_order_value": 50000,
             "confirm_token_ttl_sec": 300,
             "auto_liquidate_on_systemic_risk": false
         }
  - enabled=false 或 dry_run=true 或 xtquant 导入失败 → 一律 dry-run。

【与主程序集成】
  main.py 以 exec 方式把本模块加载进 shared 命名空间，可直接复用：
    send_feishu_payload / _append_jsonl / _feishu_card_header / _feishu_md_div / log
  独立运行（python qmt_trader.py ...）时上述函数优雅降级：
    飞书推送 → 控制台打印（绝不触达飞书），jsonl 落盘 → 本地实现。

【铁律】任何 xtquant 调用异常 → 自动降级 dry-run + 告警，绝不搞崩主程序。
================================================================
"""
import argparse
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

# ==================== shared 命名空间兼容层 ====================
# 被 main.py exec 加载时，下列名称已存在于 shared；独立运行时 NameError → 定义降级版本。

try:
    BASE_DIR  # type: ignore[name-defined]  # noqa: F821
except NameError:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

T_IO_DIR = os.path.join(BASE_DIR, "t_io")
HOLDINGS_FILE = os.path.join(BASE_DIR, "holdings.json")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
QMT_ORDERS_FILE = os.path.join(T_IO_DIR, "qmt_orders.jsonl")
QMT_PENDING_FILE = os.path.join(T_IO_DIR, "qmt_pending_liquidate.json")

try:
    log  # type: ignore[name-defined]  # noqa: F821
except NameError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("qmt_trader")

try:
    _append_jsonl  # type: ignore[name-defined]  # noqa: F821
except NameError:
    def _append_jsonl(path: str, record: dict) -> None:
        """独立运行降级：本地 jsonl 追加实现（与 utils.py:223 行为一致）。"""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass

try:
    _feishu_card_header  # type: ignore[name-defined]  # noqa: F821
except NameError:
    def _feishu_card_header(title: str, template: str) -> dict:
        return {"template": template, "title": {"tag": "plain_text", "content": title}}

try:
    _feishu_md_div  # type: ignore[name-defined]  # noqa: F821
except NameError:
    def _feishu_md_div(content: str) -> dict:
        return {"tag": "div", "text": {"content": content, "tag": "lark_md"}}

try:
    send_feishu_payload  # type: ignore[name-defined]  # noqa: F821
except NameError:
    def send_feishu_payload(payload: dict, success_log: str, error_prefix: str,
                            trigger_urgent_alarm_after_success: bool = False) -> bool:
        """独立运行降级：打印代替推送，绝不触达飞书。"""
        try:
            header = (payload or {}).get("card", {}).get("header", {})
            title = header.get("title", {}).get("content", "")
        except Exception:
            title = ""
        print(f"[飞书降级·仅打印不推送] {title or error_prefix}")
        for el in ((payload or {}).get("card", {}).get("elements", []) or [])[:30]:
            try:
                txt = el.get("text", {}).get("content", "")
                if txt:
                    print(f"    {txt}")
            except Exception:
                pass
        log.info(success_log)
        return False

# ==================== 常量 ====================
QMT_TRADER_VERSION = "v0.1.0"
MAX_DAILY_LIQUIDATE = 3          # 每日一键清仓执行次数上限（护栏）
CONFIRM_MASTER_TOKEN = "CONFIRM-LIQUIDATE"  # 人工确认主口令


# ==================== 配置 ====================
@dataclass
class QmtConfig:
    enabled: bool = False                  # QMT 模块总开关
    dry_run: bool = True                   # 模拟单开关（默认 True，实盘须显式关闭）
    account_id: str = ""                   # 资金账号
    mini_qmt_path: str = ""                # miniQMT userdata_mini 目录
    slippage: float = 0.002                # 市价单滑点估计（用于金额护栏估算）
    max_order_value: float = 50000         # 单笔订单金额上限（元）
    confirm_token_ttl_sec: int = 300       # 清仓确认 token 有效期（秒）
    auto_liquidate_on_systemic_risk: bool = False  # 系统性风险是否自动清仓（白名单，默认人工 confirm）
    auto_liquidate_scope: str = "t_qty"    # 自动清仓范围：t_qty=仅活动仓 / all=连底仓


def load_qmt_config() -> QmtConfig:
    """读取 E:\\06_T\\config.json 的 "qmt" 节；缺失或异常 → 全默认（dry-run）。"""
    cfg = QmtConfig()
    try:
        raw: Dict[str, Any] = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
        q = raw.get("qmt", {}) if isinstance(raw, dict) else {}
        if not isinstance(q, dict):
            q = {}
        cfg.enabled = bool(q.get("enabled", cfg.enabled))
        cfg.dry_run = bool(q.get("dry_run", cfg.dry_run))
        cfg.account_id = str(q.get("account_id", cfg.account_id) or "")
        cfg.mini_qmt_path = str(q.get("mini_qmt_path", cfg.mini_qmt_path) or "")
        cfg.slippage = float(q.get("slippage", cfg.slippage) or cfg.slippage)
        cfg.max_order_value = float(q.get("max_order_value", cfg.max_order_value) or cfg.max_order_value)
        cfg.confirm_token_ttl_sec = int(q.get("confirm_token_ttl_sec", cfg.confirm_token_ttl_sec) or cfg.confirm_token_ttl_sec)
        cfg.auto_liquidate_on_systemic_risk = bool(q.get("auto_liquidate_on_systemic_risk", False))
        scope = str(q.get("auto_liquidate_scope", cfg.auto_liquidate_scope) or "t_qty")
        cfg.auto_liquidate_scope = scope if scope in ("t_qty", "all") else "t_qty"
        if not q:
            log.info("ℹ️  config.json 无 \"qmt\" 配置节 → QMT 全默认 dry-run 模拟单模式")
    except Exception as e:
        log.warning(f"⚠️  qmt 配置读取失败（{str(e)[:80]}）→ 回退全默认 dry-run")
        return QmtConfig()
    return cfg


# ==================== 工具函数 ====================
def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _pure_code(code: str) -> str:
    """'600176.SH' / '600176_B' → '600176'。"""
    c = str(code or "").strip().upper()
    for sep in (".", "_"):
        if sep in c:
            c = c.split(sep)[0]
    return c


def _xt_code(code: str) -> str:
    """6 位代码 → xtquant 格式 '600176.SH' / '000988.SZ' / '430047.BJ'。"""
    c = _pure_code(code)
    if c.startswith(("60", "68", "5", "9", "11", "13")):
        return f"{c}.SH"
    if c.startswith(("4", "8")):
        return f"{c}.BJ"
    return f"{c}.SZ"


def _load_holdings() -> Dict[str, dict]:
    try:
        if os.path.exists(HOLDINGS_FILE):
            with open(HOLDINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"⚠️  holdings.json 读取失败: {str(e)[:80]}")
    return {}


def _plan_hash(plan: List[dict], scope: str) -> str:
    payload = json.dumps({"scope": scope, "plan": plan}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()


# ==================== 核心类 ====================
class QmtTrader:
    """QMT 交易接口封装：dry-run / 实盘双模，异常自动降级。"""

    def __init__(self, config: Optional[QmtConfig] = None):
        self.config = config or QmtConfig()
        self.live = False
        self.xt_trader = None
        self.account = None
        self._XtQuantTrader = None
        self._StockAccount = None
        self._xtconstant = None
        self._order_seq = 0
        self._degrade_alerted = False
        self._try_import_xtquant()

    # ---------- 初始化与连接 ----------
    def _try_import_xtquant(self) -> None:
        if not self.config.enabled:
            log.info("ℹ️  QMT 模块未启用（config.json: qmt.enabled 缺省/false）→ 全部订单走 dry-run 模拟单")
            return
        if self.config.dry_run:
            log.info("ℹ️  qmt.dry_run=true → 模拟单模式（结构对齐实盘，装好 xtquant 后开箱即用）")
            return
        try:
            from xtquant.xttrader import XtQuantTrader  # type: ignore
            from xtquant.xttype import StockAccount  # type: ignore
            from xtquant import xtconstant  # type: ignore
            self._XtQuantTrader = XtQuantTrader
            self._StockAccount = StockAccount
            self._xtconstant = xtconstant
            self.live = True
            log.info("✅ xtquant 导入成功，QMT 进入实盘待命（connect() 建立连接）")
        except Exception as e:
            self.live = False
            log.warning(f"⚠️  xtquant 导入失败（{str(e)[:80]}）→ 自动降级 dry-run；"
                        f"接入实盘请安装 miniQMT 并 pip install xtquant")

    def connect(self) -> bool:
        """建立交易连接。dry-run 直接返回 True；实盘失败自动降级 dry-run。"""
        if not self.live:
            log.info("🔌 [dry-run] 无需真实券商连接，模块就绪")
            return True
        try:
            userdata = self.config.mini_qmt_path or "userdata_mini"
            session_id = int(time.time() * 1000) % 2147483647
            self.xt_trader = self._XtQuantTrader(userdata, session_id)
            self.xt_trader.start()
            rc = self.xt_trader.connect()
            if rc != 0:
                raise RuntimeError(f"XtQuantTrader.connect 返回 {rc}（miniQMT 未启动/未登录？）")
            self.account = self._StockAccount(self.config.account_id)
            sub_rc = self.xt_trader.subscribe(self.account)
            log.info(f"✅ QMT 实盘连接成功 account={self.config.account_id} subscribe_rc={sub_rc}")
            return True
        except Exception as e:
            self._degrade(f"QMT 实盘连接失败，自动降级 dry-run: {str(e)[:120]}")
            return True  # 降级后模块仍可用

    def _degrade(self, reason: str) -> None:
        """任何实盘异常 → 降级 dry-run + 日志/飞书告警（一次运行只告警一次）。"""
        self.live = False
        log.warning(f"⚠️  {reason}")
        if self._degrade_alerted:
            return
        self._degrade_alerted = True
        try:
            payload = self._build_card(
                "⚠️ QMT 交易模块降级告警", "orange",
                [f"**原因**：{reason}",
                 "**处置**：已自动切换 dry-run 模拟单，主程序继续运行不受影响",
                 f"**时间**：{_now_str()}"])
            send_feishu_payload(payload, "✅ QMT 降级告警已推送", "QMT 降级告警推送")
        except Exception:
            pass

    # ---------- 持仓查询 ----------
    def query_positions(self) -> List[Dict[str, Any]]:
        """返回 list[dict{code,name,qty,available,cost,base,t_qty,account,pre_close}]。

        live：xt_trader.query_stock_positions（数量为准，base/t_qty 从 holdings.json 合并）；
        dry-run：直接读 holdings.json（含 000988 A/B 双账户两条）。
        """
        try:
            if self.live and self.xt_trader and self.account:
                return self._query_positions_live()
            return self._query_positions_dry()
        except Exception as e:
            log.warning(f"⚠️  持仓查询异常（{str(e)[:80]}）→ 回退 holdings.json")
            return self._query_positions_dry()

    def _query_positions_dry(self) -> List[Dict[str, Any]]:
        positions: List[Dict[str, Any]] = []
        for key, h in _load_holdings().items():
            try:
                qty = int(h.get("qty", 0) or 0)
                positions.append({
                    "code": _pure_code(key),
                    "holding_key": key,
                    "name": h.get("name", key),
                    "qty": qty,
                    "available": qty,  # dry-run 近似：昨持仓全部可用（不含当日买入）
                    "cost": float(h.get("cost", 0) or 0),
                    "base": int(h.get("base", 0) or 0),
                    "t_qty": int(h.get("t_qty", 0) or 0),
                    "account": h.get("account", ""),
                    "pre_close": float(h.get("pre_close", 0) or 0),
                })
            except Exception:
                continue
        return positions

    def _query_positions_live(self) -> List[Dict[str, Any]]:
        xt_positions = self.xt_trader.query_stock_positions(self.account)
        meta = _load_holdings()
        positions: List[Dict[str, Any]] = []
        for p in (xt_positions or []):
            try:
                code = _pure_code(getattr(p, "stock_code", ""))
                if not code:
                    continue
                # base/t_qty 以 holdings.json 为准（按 code+account 匹配，兼容 000988_B）
                m = {}
                for key, h in meta.items():
                    if _pure_code(key) == code:
                        m = h
                        break
                positions.append({
                    "code": code,
                    "holding_key": code,
                    "name": m.get("name", code),
                    "qty": int(getattr(p, "volume", 0) or 0),
                    "available": int(getattr(p, "can_use_volume", 0) or 0),
                    "cost": float(getattr(p, "avg_price", 0) or getattr(p, "open_price", 0) or 0),
                    "base": int(m.get("base", 0) or 0),
                    "t_qty": int(m.get("t_qty", 0) or 0),
                    "account": m.get("account", ""),
                    "pre_close": float(m.get("pre_close", 0) or 0),
                })
            except Exception:
                continue
        return positions

    # ---------- 下单 ----------
    def place_order(self, code: str, side: str, qty: int, price: Optional[float] = None,
                    order_type: str = "market", reason: str = "",
                    _force_dry: bool = False) -> Dict[str, Any]:
        """下单。返回 dict{order_id,status,dry_run,code,side,qty,price,reason,ts}。

        校验：qty>0；买入须 100 股整数倍（卖出允许零股）；单笔金额 ≤ max_order_value。
        任何实盘下单异常 → 自动降级 dry-run 补记模拟单并告警，绝不抛崩主程序。
        """
        try:
            code = _pure_code(code)
            side = str(side or "").lower().strip()
            qty = int(qty or 0)
            order_type = str(order_type or "market").lower().strip()

            if side not in ("buy", "sell"):
                return self._reject(code, side, qty, price, reason, f"非法方向: {side}")
            if qty <= 0:
                return self._reject(code, side, qty, price, reason, "qty 必须 > 0")
            if side == "buy" and qty % 100 != 0:
                return self._reject(code, side, qty, price, reason, f"买入须为 100 股整数倍: {qty}")
            if order_type == "limit" and (price is None or float(price) <= 0):
                return self._reject(code, side, qty, price, reason, "限价单必须给出正价格")

            est_price = self._estimate_price(code, side, price, order_type)
            est_value = round(qty * est_price, 2) if est_price else 0.0
            if est_price and est_value > float(self.config.max_order_value):
                return self._reject(code, side, qty, price, reason,
                                    f"单笔金额 {est_value:.0f} 元超过上限 {self.config.max_order_value:.0f} 元")

            if self.live and not _force_dry and self.xt_trader and self.account:
                try:
                    return self._place_order_live(code, side, qty, price, order_type, reason, est_price, est_value)
                except Exception as e:
                    self._degrade(f"实盘下单异常（{str(e)[:100]}）→ 本笔降级为 dry-run 模拟单")
                    # 继续走下方 dry-run 路径补记

            return self._place_order_dry(code, side, qty, price, order_type, reason, est_price, est_value)
        except Exception as e:
            log.error(f"❌ place_order 未捕获异常（已兜底）: {str(e)[:120]}")
            return {"order_id": "", "status": "error", "dry_run": True, "code": code,
                    "side": side, "qty": qty, "price": price, "reason": reason,
                    "error": str(e)[:120], "ts": _now_str()}

    def _estimate_price(self, code: str, side: str, price: Optional[float], order_type: str) -> float:
        """金额护栏估价：限价用委托价；市价用 (昨收/成本) × (1±slippage)。"""
        if price and float(price) > 0:
            base = float(price)
        else:
            base = 0.0
            for key, h in _load_holdings().items():
                if _pure_code(key) == code:
                    base = float(h.get("pre_close", 0) or h.get("cost", 0) or 0)
                    break
        if base <= 0:
            return 0.0
        if order_type == "market":
            slip = float(self.config.slippage or 0)
            base = base * (1 + slip) if side == "buy" else base * (1 - slip)
        return base

    def _reject(self, code: str, side: str, qty: int, price: Optional[float],
                reason: str, why: str) -> Dict[str, Any]:
        log.warning(f"⛔ 订单被拒绝: {side} {code} x{qty} | {why}")
        rec = {"kind": "order", "ts": _now_str(), "order_id": "", "status": "rejected",
               "dry_run": not self.live, "code": code, "side": side, "qty": qty,
               "price": price, "reason": reason, "reject_reason": why}
        _append_jsonl(QMT_ORDERS_FILE, rec)
        return {"order_id": "", "status": "rejected", "dry_run": not self.live,
                "code": code, "side": side, "qty": qty, "price": price,
                "reason": reason, "reject_reason": why, "ts": _now_str()}

    def _place_order_dry(self, code: str, side: str, qty: int, price: Optional[float],
                         order_type: str, reason: str, est_price: float, est_value: float) -> Dict[str, Any]:
        self._order_seq += 1
        order_id = f"DRY-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{self._order_seq:03d}"
        rec = {"kind": "order", "ts": _now_str(), "order_id": order_id, "status": "simulated",
               "dry_run": True, "code": code, "side": side, "qty": qty, "price": price,
               "est_price": round(est_price, 3) if est_price else None,
               "est_value": est_value, "order_type": order_type, "reason": reason}
        _append_jsonl(QMT_ORDERS_FILE, rec)
        odd_note = "（零股卖出）" if (side == "sell" and qty % 100 != 0) else ""
        log.info(f"🧪 [dry-run] 模拟单 {order_id}: {side} {code} x{qty}{odd_note} "
                 f"@ {price if price else '市价'} 估额 {est_value:.0f} 元 | {reason}")
        return {"order_id": order_id, "status": "simulated", "dry_run": True, "code": code,
                "side": side, "qty": qty, "price": price, "est_value": est_value,
                "order_type": order_type, "reason": reason, "ts": _now_str()}

    def _place_order_live(self, code: str, side: str, qty: int, price: Optional[float],
                          order_type: str, reason: str, est_price: float, est_value: float) -> Dict[str, Any]:
        xt_side = self._xtconstant.STOCK_BUY if side == "buy" else self._xtconstant.STOCK_SELL
        if order_type == "limit":
            price_type = self._xtconstant.FIX_PRICE
            px = float(price)
        else:
            price_type = self._xtconstant.LATEST_PRICE
            px = -1  # 市价：以最新价撮合
        async_seq = self.xt_trader.order_stock_async(
            self.account, _xt_code(code), xt_side, int(qty), price_type, px,
            "t_trader_qmt", (reason or "")[:100])
        rec = {"kind": "order", "ts": _now_str(), "order_id": str(async_seq),
               "status": "submitted", "dry_run": False, "code": code, "side": side,
               "qty": qty, "price": price, "est_price": round(est_price, 3) if est_price else None,
               "est_value": est_value, "order_type": order_type, "reason": reason}
        _append_jsonl(QMT_ORDERS_FILE, rec)
        log.info(f"📡 [实盘] 已报单 seq={async_seq}: {side} {code} x{qty} @ {px if px > 0 else '市价'} | {reason}")
        return {"order_id": str(async_seq), "status": "submitted", "dry_run": False,
                "code": code, "side": side, "qty": qty, "price": price,
                "est_value": est_value, "order_type": order_type, "reason": reason,
                "ts": _now_str()}

    # ---------- 一键清仓 ----------
    def liquidate_all(self, confirm: Optional[str] = None, scope: str = "t_qty",
                      dry_run: Optional[bool] = None, reason: str = "") -> Dict[str, Any]:
        """一键清仓（双重确认防误触）。

        scope="t_qty" 只清活动 T 仓（保留底仓）；scope="all" 连底仓全清。
        执行条件（二选一）：
          1) confirm == 本次清仓计划 plan_hash（16 位，TTL confirm_token_ttl_sec 内有效）；
          2) confirm == "CONFIRM-LIQUIDATE"（人工主口令）。
        无 confirm → 生成计划 + 飞书红卡预警 + 返回 need_confirm（含 plan_hash），不执行。
        """
        try:
            if scope not in ("t_qty", "all"):
                return {"status": "error", "error": f"非法 scope: {scope}（仅支持 t_qty / all）"}
            eff_dry = bool(self.config.dry_run) if dry_run is None else bool(dry_run)

            # 护栏：每日清仓执行次数上限
            if self._daily_liquidate_count() >= MAX_DAILY_LIQUIDATE:
                msg = f"今日清仓执行已达 {MAX_DAILY_LIQUIDATE} 次上限，拒绝再次执行"
                log.warning(f"⛔ {msg}")
                return {"status": "rejected", "reject_reason": msg}

            plan = self._build_liquidation_plan(scope)
            if not plan:
                log.info(f"ℹ️  清仓计划为空（scope={scope}）：无可卖持仓")
                return {"status": "empty", "scope": scope, "plan": []}

            ph = _plan_hash(plan, scope)
            total = round(sum(l["est_value"] for l in plan), 2)
            _append_jsonl(QMT_ORDERS_FILE, {
                "kind": "liquidate_plan", "ts": _now_str(), "scope": scope,
                "plan_hash": ph, "legs": plan, "est_total": total,
                "dry_run": eff_dry, "reason": reason})

            # 确认门
            confirmed, token_state = self._check_confirm(confirm, ph)
            if not confirmed:
                if token_state == "expired":
                    log.warning(f"⛔ 清仓确认 token 已过期（plan_hash={ph}），请重新生成计划")
                    return {"status": "token_expired", "plan_hash": ph,
                            "hint": "请重新调用 liquidate_all(confirm=None) 生成新 token"}
                self._save_pending(ph, scope)
                self._push_liquidate_warning(plan, ph, total, scope, eff_dry, reason)
                log.warning(f"🚨 清仓计划已生成待确认 plan_hash={ph}（TTL {self.config.confirm_token_ttl_sec}s）"
                            f" scope={scope} 预估 {total:.0f} 元")
                return {"status": "need_confirm", "plan_hash": ph,
                        "ttl_sec": int(self.config.confirm_token_ttl_sec),
                        "scope": scope, "dry_run": eff_dry, "plan": plan, "est_total": total,
                        "confirm_hint": f"liquidate_all(confirm='{ph}') 或 confirm='{CONFIRM_MASTER_TOKEN}'"}

            # 执行：逐腿卖出
            log.warning(f"🚨 清仓确认通过（{token_state}），开始执行 scope={scope} 共 {len(plan)} 腿 "
                        f"{'[dry-run 演练]' if eff_dry else '[实盘]'}")
            results: List[Dict[str, Any]] = []
            for leg in plan:
                r = self.place_order(leg["code"], "sell", leg["sell_qty"], price=None,
                                     order_type="market",
                                     reason=f"一键清仓[{scope}] {reason}".strip(),
                                     _force_dry=eff_dry)
                r["holding_key"] = leg.get("holding_key", leg["code"])
                r["name"] = leg.get("name", leg["code"])
                results.append(r)

            ok = sum(1 for r in results if r.get("status") in ("simulated", "submitted"))
            summary = {"kind": "liquidate_execute", "ts": _now_str(), "scope": scope,
                       "plan_hash": ph, "dry_run": eff_dry, "reason": reason,
                       "total_legs": len(results), "ok_legs": ok,
                       "est_total": total,
                       "legs": [{"code": r.get("code"), "qty": r.get("qty"),
                                 "status": r.get("status"), "order_id": r.get("order_id")}
                                for r in results]}
            _append_jsonl(QMT_ORDERS_FILE, summary)
            self._push_liquidate_result(results, scope, eff_dry, reason, ok, total)
            log.warning(f"🏁 清仓执行完毕: {ok}/{len(results)} 腿成功 "
                        f"{'(dry-run 模拟)' if eff_dry else '(实盘已报单)'}")
            return {"status": "executed", "dry_run": eff_dry, "scope": scope,
                    "plan_hash": ph, "ok_legs": ok, "total_legs": len(results),
                    "est_total": total, "results": results}
        except Exception as e:
            log.error(f"❌ liquidate_all 未捕获异常（已兜底，主程序不受影响）: {str(e)[:120]}")
            return {"status": "error", "error": str(e)[:120]}

    def _build_liquidation_plan(self, scope: str) -> List[Dict[str, Any]]:
        plan: List[Dict[str, Any]] = []
        for p in self.query_positions():
            try:
                qty = int(p.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                if scope == "t_qty":
                    sell_qty = int(p.get("t_qty", 0) or 0)
                else:
                    sell_qty = qty
                sell_qty = max(0, min(sell_qty, int(p.get("available", qty) or qty)))
                if sell_qty <= 0:
                    continue
                est_price = float(p.get("pre_close", 0) or p.get("cost", 0) or 0)
                plan.append({
                    "code": p["code"], "holding_key": p.get("holding_key", p["code"]),
                    "name": p.get("name", p["code"]), "account": p.get("account", ""),
                    "hold_qty": qty, "sell_qty": sell_qty,
                    "est_price": round(est_price, 3),
                    "est_value": round(sell_qty * est_price, 2) if est_price else 0.0,
                })
            except Exception:
                continue
        return plan

    def _check_confirm(self, confirm: Optional[str], plan_hash: str) -> tuple:
        """返回 (是否放行, 状态: master/hash/expired/none)。"""
        if not confirm:
            return False, "none"
        if str(confirm).strip() == CONFIRM_MASTER_TOKEN:
            return True, "master"
        if str(confirm).strip().upper() == plan_hash:
            pend = self._load_pending()
            if pend and pend.get("hash") == plan_hash and time.time() <= float(pend.get("expire_at", 0)):
                return True, "hash"
            return False, "expired"
        return False, "none"

    def _save_pending(self, plan_hash: str, scope: str) -> None:
        try:
            rec = {"hash": plan_hash, "scope": scope,
                   "expire_at": time.time() + float(self.config.confirm_token_ttl_sec),
                   "created_at": _now_str()}
            with open(QMT_PENDING_FILE, "w", encoding="utf-8") as f:
                json.dump(rec, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_pending(self) -> Optional[dict]:
        try:
            if os.path.exists(QMT_PENDING_FILE):
                with open(QMT_PENDING_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    def _daily_liquidate_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        n = 0
        try:
            if os.path.exists(QMT_ORDERS_FILE):
                with open(QMT_ORDERS_FILE, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("kind") == "liquidate_execute" and str(rec.get("ts", "")).startswith(today):
                            n += 1
        except Exception:
            pass
        return n

    # ---------- 系统性风险对接 ----------
    def handle_systemic_risk(self, meta: Optional[dict] = None) -> Dict[str, Any]:
        """系统性风险入口：上游 daily_sentiment 产出 systemic_risk=True 时由主程序调用。

        默认：生成清仓计划 + 飞书红卡预警，等人工 confirm（hash 或 CONFIRM-LIQUIDATE）。
        config.json 白名单 qmt.auto_liquidate_on_systemic_risk=true 时：
        按 qmt.auto_liquidate_scope 自动以主口令执行（仍受日清仓次数护栏约束）。
        """
        meta = meta or {}
        reason = f"系统性风险触发 {meta.get('source', 'daily_sentiment')} {meta.get('note', '')}".strip()
        if self.config.auto_liquidate_on_systemic_risk:
            log.warning(f"🚨 系统性风险白名单自动清仓已启用 scope={self.config.auto_liquidate_scope} | {reason}")
            return self.liquidate_all(confirm=CONFIRM_MASTER_TOKEN,
                                      scope=self.config.auto_liquidate_scope, reason=reason)
        log.warning(f"🚨 系统性风险预警（人工确认模式）| {reason}")
        return self.liquidate_all(confirm=None, scope=self.config.auto_liquidate_scope, reason=reason)

    # ---------- 闭环核对 ----------
    @staticmethod
    def close_loop_check(holdings: Dict[str, dict], virtual_trades: List[dict]) -> Dict[str, Any]:
        """买卖闭环核对（供主程序尾盘审计调用）。

        逐股（聚合 A/B 账户到纯代码）计算：
          unrebuilt = ΣSELL_HIGH.qty - ΣBUY_LOW.qty
          unrebuilt > 0 → 反T已卖未接回，需 BUY_BACK 接回（EOD 前必须闭合）
          unrebuilt < 0 → 正T已买未卖出，需 SELL_OUT 卖出
          unrebuilt = 0 → 闭环 OK
        铁律：EOD qty == base，底仓不受做T影响。
        """
        name_map: Dict[str, str] = {}
        for key, h in (holdings or {}).items():
            c = _pure_code(key)
            if c not in name_map:
                name_map[c] = h.get("name", c)

        agg: Dict[str, Dict[str, int]] = {}
        for t in (virtual_trades or []):
            try:
                c = _pure_code(t.get("code", ""))
                if not c:
                    continue
                action = str(t.get("action", "") or "").upper()
                qty = int(t.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                bucket = agg.setdefault(c, {"sold": 0, "bought": 0})
                if action.startswith("SELL"):
                    bucket["sold"] += qty
                elif action.startswith("BUY"):
                    bucket["bought"] += qty
            except Exception:
                continue

        items: List[Dict[str, Any]] = []
        need_rebuild: List[Dict[str, Any]] = []
        for c in sorted(agg.keys()):
            sold = agg[c]["sold"]
            bought = agg[c]["bought"]
            unrebuilt = sold - bought
            if unrebuilt > 0:
                action_needed, qty_to_trade = "BUY_BACK", unrebuilt
            elif unrebuilt < 0:
                action_needed, qty_to_trade = "SELL_OUT", -unrebuilt
            else:
                action_needed, qty_to_trade = "OK", 0
            item = {"code": c, "name": name_map.get(c, c), "sold": sold, "bought": bought,
                    "unrebuilt": unrebuilt, "action_needed": action_needed,
                    "qty_to_trade": qty_to_trade}
            items.append(item)
            if action_needed != "OK":
                need_rebuild.append(item)

        return {"date": datetime.now().strftime("%Y-%m-%d"),
                "all_closed": len(need_rebuild) == 0,
                "items": items, "need_rebuild": need_rebuild}

    # ---------- 飞书卡片 ----------
    @staticmethod
    def _build_card(title: str, template: str, lines: List[str]) -> dict:
        elements = [_feishu_md_div(l) for l in lines]
        card = {"config": {"wide_screen_mode": True},
                "header": _feishu_card_header(title, template),
                "elements": elements}
        return {"msg_type": "interactive", "card": card, "notify_type": 1}

    def _push_liquidate_warning(self, plan: List[dict], plan_hash: str, total: float,
                                scope: str, dry_run: bool, reason: str) -> None:
        try:
            scope_cn = "仅活动T仓（保留底仓）" if scope == "t_qty" else "全部持仓（含底仓！）"
            lines = [
                f"**模式**：{'🧪 dry-run 演练（不会真实下单）' if dry_run else '⚠️ 实盘'}",
                f"**清仓范围**：{scope_cn}",
                f"**触发原因**：{reason or '手动触发'}",
                "**持仓清单**：",
            ]
            for l in plan:
                lines.append(f"  · {l['code']} {l['name']}（{l.get('account') or '-'}）"
                             f" 卖 {l['sell_qty']} 股 / 持 {l['hold_qty']} 股，约 {l['est_value']:.0f} 元")
            lines += [
                f"**预估总金额**：约 {total:.0f} 元",
                f"**确认方式**（{int(self.config.confirm_token_ttl_sec)} 秒内有效）：",
                f"  · 回复计划哈希：`{plan_hash}`",
                f"  · 或人工主口令：`{CONFIRM_MASTER_TOKEN}`",
                f"**时间**：{_now_str()}",
            ]
            payload = self._build_card("🚨 一键清仓预警（待确认）", "red", lines)
            send_feishu_payload(payload, "✅ 清仓预警红卡已推送", "清仓预警推送",
                                trigger_urgent_alarm_after_success=True)
        except Exception as e:
            log.warning(f"⚠️  清仓预警推送异常（不影响流程）: {str(e)[:80]}")

    def _push_liquidate_result(self, results: List[dict], scope: str, dry_run: bool,
                               reason: str, ok: int, total: float) -> None:
        try:
            lines = [
                f"**模式**：{'🧪 dry-run 演练' if dry_run else '⚠️ 实盘'}",
                f"**范围**：{'仅活动T仓' if scope == 't_qty' else '全部持仓（含底仓）'}",
                f"**原因**：{reason or '手动触发'}",
                f"**结果**：{ok}/{len(results)} 腿成功，预估总额约 {total:.0f} 元",
                "**逐腿明细**：",
            ]
            for r in results:
                mark = "✅" if r.get("status") in ("simulated", "submitted") else "❌"
                lines.append(f"  {mark} {r.get('code')} {r.get('name', '')} 卖 {r.get('qty')} 股 "
                             f"[{r.get('status')}] {r.get('order_id', '')}")
            lines.append(f"**时间**：{_now_str()}")
            template = "green" if ok == len(results) else "orange"
            payload = self._build_card("🏁 一键清仓执行结果", template, lines)
            send_feishu_payload(payload, "✅ 清仓结果汇总卡已推送", "清仓结果推送")
        except Exception as e:
            log.warning(f"⚠️  清仓结果推送异常（不影响流程）: {str(e)[:80]}")


# ==================== CLI ====================
def _safe_reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def _print_positions(positions: List[dict]) -> None:
    print("\n📊 当前持仓清单")
    print("-" * 78)
    print(f"{'代码':<8}{'名称':<12}{'账户':<8}{'总持仓':>8}{'底仓':>8}{'活动T仓':>8}{'成本':>10}{'昨收':>10}")
    print("-" * 78)
    for p in positions:
        print(f"{p['code']:<8}{str(p['name']):<12}{str(p.get('account') or '-'):<8}"
              f"{p['qty']:>8}{p.get('base', 0):>8}{p.get('t_qty', 0):>8}"
              f"{p.get('cost', 0):>10.3f}{p.get('pre_close', 0):>10.3f}")
    print("-" * 78)
    print(f"共 {len(positions)} 条持仓记录（000988 含 A/B 双账户两条）")


def _close_loop_demo(trader: QmtTrader) -> None:
    holdings = _load_holdings()
    demo_trades = [
        {"code": "300153", "action": "SELL_HIGH", "qty": 200, "ts": "10:12:05", "price": 24.80},
        {"code": "300153", "action": "BUY_LOW", "qty": 100, "ts": "13:40:11", "price": 24.10},
        {"code": "600176", "action": "SELL_HIGH", "qty": 600, "ts": "09:58:30", "price": 52.90},
        {"code": "600176", "action": "BUY_LOW", "qty": 600, "ts": "14:05:47", "price": 51.60},
        {"code": "000988", "action": "SELL_HIGH", "qty": 200, "ts": "10:30:00", "price": 155.00},
        {"code": "000988_B", "action": "SELL_HIGH", "qty": 100, "ts": "10:31:00", "price": 155.20},
    ]
    print("\n🧪 闭环核对演示（虚拟成交：300153 卖200买100 / 600176 卖600买600 / 000988 A卖200+B卖100）")
    result = QmtTrader.close_loop_check(holdings, demo_trades)
    print("-" * 78)
    print(f"{'代码':<8}{'名称':<12}{'已卖':>8}{'已买':>8}{'未接回':>8}{'处置':<10}{'数量':>8}")
    print("-" * 78)
    for it in result["items"]:
        print(f"{it['code']:<8}{str(it['name']):<12}{it['sold']:>8}{it['bought']:>8}"
              f"{it['unrebuilt']:>8}{it['action_needed']:<10}{it['qty_to_trade']:>8}")
    print("-" * 78)
    if result["all_closed"]:
        print("✅ 全部闭环：EOD qty==base，底仓不受做T影响")
    else:
        print(f"⚠️  {len(result['need_rebuild'])} 只未闭环，尾盘须处理（BUY_BACK=反T接回 / SELL_OUT=正T卖出）：")
        for it in result["need_rebuild"]:
            print(f"    → {it['code']} {it['name']} {it['action_needed']} {it['qty_to_trade']} 股")


def _cli() -> None:
    _safe_reconfigure_stdout()
    parser = argparse.ArgumentParser(description="QMT 自动交易模块 CLI（默认 dry-run，绝不触达实盘）")
    parser.add_argument("--positions", action="store_true", help="查询持仓清单")
    parser.add_argument("--liquidate", action="store_true", help="一键清仓（默认只生成计划，需 --confirm 才执行）")
    parser.add_argument("--scope", choices=["t_qty", "all"], default="t_qty",
                        help="清仓范围：t_qty=仅活动T仓（默认） / all=含底仓")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true",
                        help="强制 dry-run 演练（即使 config 切了实盘也不真实下单）")
    parser.add_argument("--confirm", default=None,
                        help=f"清仓确认：计划哈希 或 人工主口令 {CONFIRM_MASTER_TOKEN}")
    parser.add_argument("--reason", default="CLI 手动触发", help="清仓原因（写入卡片与日志）")
    parser.add_argument("--close-loop-demo", action="store_true", help="买卖闭环核对演示")
    args = parser.parse_args()

    cfg = load_qmt_config()
    trader = QmtTrader(cfg)
    trader.connect()

    mode = "⚠️ 实盘" if trader.live else "🧪 DRY-RUN 模拟单（无 xtquant / 未启用，不会真实下单）"
    print(f"\n[QMT {QMT_TRADER_VERSION}] 当前模式：{mode}")
    print(f"[QMT] 订单落盘：{QMT_ORDERS_FILE}")

    ran = False
    if args.positions:
        ran = True
        _print_positions(trader.query_positions())

    if args.liquidate:
        ran = True
        dry = True if args.dry_run else None
        result = trader.liquidate_all(confirm=args.confirm, scope=args.scope,
                                      dry_run=dry, reason=args.reason)
        print("\n📋 清仓结果")
        print("-" * 78)
        if result.get("status") == "need_confirm":
            scope_cn = "仅活动T仓（保留底仓）" if result["scope"] == "t_qty" else "全部持仓（含底仓！）"
            print(f"状态: 待确认（need_confirm）  范围: {scope_cn}  模式: {'dry-run 演练' if result['dry_run'] else '实盘'}")
            print(f"{'代码':<8}{'名称':<12}{'账户':<8}{'持仓':>8}{'拟卖':>8}{'估价':>10}{'估额(元)':>12}")
            print("-" * 78)
            for l in result["plan"]:
                print(f"{l['code']:<8}{str(l['name']):<12}{str(l.get('account') or '-'):<8}"
                      f"{l['hold_qty']:>8}{l['sell_qty']:>8}{l['est_price']:>10.3f}{l['est_value']:>12.0f}")
            print("-" * 78)
            print(f"预估总金额: {result['est_total']:.0f} 元")
            print(f"计划哈希 plan_hash: {result['plan_hash']}（TTL {result['ttl_sec']} 秒）")
            print(f"执行方式: --confirm {result['plan_hash']}  或  --confirm {CONFIRM_MASTER_TOKEN}")
            print("（本次未执行任何下单，仅生成计划与预警）")
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    if args.close_loop_demo:
        ran = True
        _close_loop_demo(trader)

    if not ran:
        parser.print_help()


if __name__ == "__main__":
    _cli()
