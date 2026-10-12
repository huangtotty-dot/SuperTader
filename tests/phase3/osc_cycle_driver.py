# -*- coding: utf-8 -*-
"""超跌跳空确认 · **两日全周期纸面演练**（离线段驱动，由 test_osc_full_cycle 以子进程调用）。

与 `gm_main_hook_driver.py` 的区别：那个只验"钩子闸与定序"（钩子被换成记录器）；
**本驱动把钩子真跑一遍**——预筛→买→成交回报→入账本→次日早盘卖→14:50 止损，
用可编程假 GM（取数/账户/下单全假）在**沙箱目录**走完两天。不触生产状态、不下真单。

假件三件：
  · `fake provider` + `fake sym2board_map`（注入 prescreen）→ 确定性造「热门板块 + 超跌票」
  · `gm.api.history` → 按 (frequency, fields) 造 10:00 棒 / 早盘最高 / 14:50 现价
  · `gm.api.order_volume` + `FakeAccount` → 记录委托；驱动把成交回报喂回 `GM.on_order_status`，
    让**真实**的成交入账与账本登记路径跑起来。

输出一行 `RESULT <json>` 供测试断言。
"""
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta

ROOT = os.environ.get("SMOKE_ROOT") or r"E:\superTrader"
TMP = tempfile.mkdtemp(prefix="osc_cycle_")
BOOK = os.path.join(TMP, "book")
os.makedirs(BOOK, exist_ok=True)
os.environ["GM_BRIDGE_DIR"] = os.path.join(TMP, "bridge")
os.environ["SUPERTRADER_ROOT"] = ROOT

import pandas as pd                                                    # noqa: E402

BASE = 10.0        # 合成票的基准价
NDAYS = 29         # 日线根数（>20 供 MA20）

# 每票剧本：slope=日线斜率(负=超跌) / gap=10:00 相对昨收的涨幅 / lu=是否昨日涨停(提供热门板块票数)
#          pop=次日早盘最高相对入场价倍数 / eod=14:50 收盘相对入场价倍数
SPEC = {
    # 三只超跌票（候选；slope 负 ⇒ 昨收 < MA20）
    "600001": {"slope": -0.004, "gap": 0.05, "lu": False, "pop": 1.05, "eod": 0.99},
    # 深超跌（dev20 < −7%，供 sl5_deep）× 14:50 跌 8%
    "600002": {"slope": -0.012, "gap": 0.08, "lu": False, "pop": 1.02, "eod": 0.92},
    "600003": {"slope": -0.004, "gap": 0.03, "lu": False, "pop": 1.00, "eod": 1.00},
    # 第 2 天加入：账户持有但无人认领（归属硬锁必须拦住它的买单）
    "600099": {"slope": -0.004, "gap": 0.06, "lu": False, "pop": 1.00, "eod": 1.00},
    # 三只昨日大涨票（只贡献"板块涨停家数"⇒ 半导体成为热门板块；自己不是超跌票）
    "600004": {"slope": 0.006, "gap": 0.00, "lu": True, "pop": 1.00, "eod": 1.00},
    "600005": {"slope": 0.006, "gap": 0.00, "lu": True, "pop": 1.00, "eod": 1.00},
    "600006": {"slope": 0.006, "gap": 0.00, "lu": True, "pop": 1.00, "eod": 1.00},
}
SECTOR_CODES = list(SPEC)
ACCOUNT_CASH = 300000.0


def prev_close(code):
    """与 daily_many 生成的末根收盘一致（昨收）。"""
    return BASE * (1.0 + SPEC[code]["slope"] * (NDAYS - 1))


def entry_price(code):
    """10:00 收盘 = 昨收 ×(1+gap) —— 入场价口径。"""
    return prev_close(code) * (1.0 + SPEC[code]["gap"])


# ══════════════ 假 gm.api ══════════════
gm_api = types.ModuleType("gm.api")
for k, v in {"MODE_LIVE": 1, "MODE_BACKTEST": 2, "ADJUST_PREV": 1, "ADJUST_NONE": 0,
             "OrderSide_Buy": 1, "OrderSide_Sell": 2, "OrderType_Market": 1,
             "OrderType_Limit": 2, "PositionEffect_Open": 1, "PositionEffect_Close": 2,
             "PositionSide_Long": 1}.items():
    setattr(gm_api, k, v)
gm_api.subscribe = lambda **k: None
gm_api.history_n = lambda **k: []
gm_api.current = lambda *a, **k: []


def _history(symbol=None, frequency=None, fields=None, start_time=None, end_time=None,
             df=False, **kw):
    """按 (frequency, fields) 分辨调用方，返回合成的 df。"""
    syms = symbol if isinstance(symbol, list) else [symbol]
    day = str(start_time)[:10]
    rows = []
    for s in syms:
        c = str(s).split(".")[-1]
        sp = SPEC.get(c)
        if not sp or c not in ACTIVE:                     # ACTIVE：当天可交易的合成票
            continue
        pc = prev_close(c)
        if df and frequency == "1800s" and "low" in (fields or ""):        # 10:00 买侧棒
            rows.append({"symbol": s, "eob": "%s 10:00:00" % day, "open": pc,
                         "high": pc * (1 + sp["gap"]), "low": pc,          # low == 昨收 ⇒ 缺口未回补
                         "close": pc * (1 + sp["gap"]),
                         "volume": 1000.0, "amount": pc * 0.99 * 1000.0})  # VWAP < close
        elif df and frequency == "1800s" and "high" in (fields or ""):     # 次日早盘最高
            rows.append({"symbol": s, "eob": "%s 10:00:00" % day,
                         "high": entry_price(c) * sp["pop"]})
        elif df and frequency == "60s":                                    # 14:50 现价
            rows.append({"symbol": s, "eob": "%s 14:50:00" % day,
                         "close": entry_price(c) * sp["eod"]})
    return pd.DataFrame(rows)


gm_api.history = _history
sys.modules["gm.api"] = gm_api


class FakeProvider(object):
    """`daily_many(codes, days)` → {code: DataFrame(date, close)}。"""

    def daily_many(self, codes, days=40):
        dates = (["2026-09-%02d" % (d + 1) for d in range(20)]
                 + ["2026-10-%02d" % (d + 1) for d in range(1, 10)])[:NDAYS]
        out = {}
        for c in codes:
            sp = SPEC.get(c)
            if not sp:
                continue
            closes = [BASE * (1.0 + sp["slope"] * i) for i in range(NDAYS)]
            if sp["lu"]:
                closes[-1] = closes[-2] * 1.10                 # 昨日 +10% ⇒ 计入涨停家数
            out[c] = pd.DataFrame({"date": dates, "close": closes})
        return out


class FakeAccount(object):
    def __init__(self):
        self.cash = ACCOUNT_CASH
        self.pos = {}

    def cash(self):
        return {"available": self.cash, "total": self.cash}

    def positions(self, symbol=None, side=None):
        out = []
        for s, v in self.pos.items():
            if symbol and s != symbol:
                continue
            out.append(types.SimpleNamespace(symbol=s, volume=v["qty"],
                                             available=v["available"], vwap=v["vwap"]))
        return out


ACCT = FakeAccount()
ORDERS = []
ACTIVE = set(SPEC) - {"600099"}   # 当天"可交易"的合成票（600099 第 2 天才进池）


def _order_volume(**kw):
    ORDERS.append(kw)
    return [{"id": "O%d" % len(ORDERS), "status": 0}]     # 与 _get_orders 的编号一致


gm_api.order_volume = _order_volume


def _get_orders(symbol=None, **kw):
    """轮询终态：把已记录的委托当作"全部成交"（真实 GM 也会这么回）。"""
    out = []
    for i, od in enumerate(ORDERS):
        if symbol and od["symbol"] != symbol:
            continue
        px = float(od.get("price") or 0)
        out.append({"id": "O%d" % (i + 1), "status": 3, "volume": int(od["volume"]),
                    "filled_volume": int(od["volume"]), "filled_vwap": px, "price": px})
    return out


gm_api.get_orders = _get_orders

# ══════════════ 导入真 gm_main ══════════════
sys.path.insert(0, os.path.join(ROOT, "execution", "auto"))
import importlib.util                                                  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "gm_main", os.path.join(ROOT, "execution", "auto", "gm_main.py"))
GM = importlib.util.module_from_spec(spec)
sys.modules["gm_main"] = GM
spec.loader.exec_module(GM)
OSC = sys.modules["oversold_gap_confirm"]

GM._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace.jsonl")
GM._AUDIT_MIRROR_PATH = os.path.join(TMP, "auto_backtrace.jsonl")
GM.ops_guard = type("_OG", (), {"bootstrap_logging": lambda *a, **k: None,
                                "ensure_watcher": lambda *a, **k: None})()
import atexit                                                          # noqa: E402
try:
    atexit.unregister(GM._gm_atexit_banner)
except Exception:
    pass

GM._OSC_BOOK_DIR = BOOK
GM.OSC_RT.book_dir = BOOK
OSC.prescreen.default_provider = lambda: FakeProvider()
OSC.prescreen.sym2board_map = lambda: {c: "半导体" for c in SECTOR_CODES}


class Ctx(object):
    def __init__(self, mode=1):
        self.mode = mode
        self.now = None
        self.account = (lambda: ACCT)


ctx = Ctx()
GM.init(ctx)
out = {"stock_n": len(GM.STOCKS), "days": {}}
FED = set()


def feed_fills():
    """把尚未回报的委托当作"全部成交"喂回 on_order_status（走真实入账/账本路径）。"""
    for i, od in enumerate(ORDERS):
        if i in FED:
            continue
        FED.add(i)
        sym = od["symbol"]
        qty = int(od["volume"])
        side = int(od["side"])
        px = float(od.get("price") or 0) or prev_close(str(sym).split(".")[-1])
        GM.on_order_status(ctx, {"symbol": sym, "status": 3, "volume": qty, "side": side,
                                 "filled_vwap": px, "price": px,
                                 "id": "F%d" % i})
        c = f"SHSE.{sym.split('.')[-1]}" if str(sym).split(".")[-1][:1] in "569" \
            else f"SZSE.{sym.split('.')[-1]}"
        prev = ACCT.pos.get(c) or {"qty": 0, "available": 0, "vwap": px}
        if side == 1:                       # 买：当日 available=0（T+1）
            ACCT.cash -= qty * px
            ACCT.pos[c] = {"qty": prev["qty"] + qty, "available": prev["available"],
                           "vwap": px}
        else:                               # 卖
            ACCT.cash += qty * px
            nq = max(0, prev["qty"] - qty)
            if nq == 0:
                ACCT.pos.pop(c, None)
            else:
                ACCT.pos[c] = {"qty": nq, "available": max(0, prev["available"] - qty),
                               "vwap": prev["vwap"]}


def unlock_all():
    """跨日：T+1 解锁（隔夜后 available = qty）。"""
    for s, v in list(ACCT.pos.items()):
        ACCT.pos[s] = {"qty": v["qty"], "available": v["qty"], "vwap": v["vwap"]}


def drive(day_idx, plan):
    """按时点依次打 on_bar；**每个时点后立刻喂成交回报**（真实 fill 回调在委托后 1s 内到）。"""
    day = (datetime.now() + timedelta(days=day_idx)).date()
    for hh, mm in plan:
        ctx.now = datetime(day.year, day.month, day.day, hh, mm)
        GM.on_bar(ctx, [])
        feed_fills()
    return str(day)


def book():
    fp = os.path.join(BOOK, "positions.json")
    if not os.path.exists(fp):
        return {}
    try:
        return json.load(open(fp, encoding="utf-8")).get("open") or {}
    except Exception:
        return {}


def jload(name):
    fp = os.path.join(BOOK, name)
    return json.load(open(fp, encoding="utf-8")) if os.path.exists(fp) else {}


def events():
    fp = os.path.join(os.environ["GM_BRIDGE_DIR"],
                      "events_%s.jsonl" % datetime.now().strftime("%Y%m%d"))
    rows = []
    if os.path.exists(fp):
        for ln in open(fp, encoding="utf-8"):
            try:
                e = json.loads(ln)
            except Exception:
                continue
            if e.get("event") == "order":
                rows.append((e.get("code"), e.get("order_type"), e.get("side")))
    return rows


# ══════════════ 第 1 天 ══════════════
d1 = drive(0, [(9, 31), (10, 0), (14, 50)])
f1 = jload("fills_%s.json" % d1)
out["days"]["d1"] = {
    "date": d1,
    "candidates": sorted(OSC.hooks._STATE["candidates"]),
    "orders": [(o["symbol"].split(".")[-1], int(o["volume"]), int(o["side"])) for o in ORDERS],
    "buys": [(b["code"], b["qty_placed"], b["status"]) for b in f1.get("buys", [])],
    "summary": f1.get("summary"),
    "book_before_fill": sorted(book()),
    "sell_d1": jload("sells_close_%s.json" % d1).get("summary"),
    "lock_events": 0,
}
out["days"]["d1"]["acct"] = {s: v["qty"] for s, v in ACCT.pos.items()}
out["days"]["d1"]["book_after_fill"] = {c: {k: v[k] for k in ("entry_date", "entry_price", "qty", "dev20")}
                                        for c, v in book().items()}
out["days"]["d1"]["order_event_types"] = sorted({t for _c, t, _s in events()})

# ══════════════ 第 2 天 ══════════════
unlock_all()
ACTIVE.add("600099")                                  # 新票进候选，但账户已持有它（无人认领）
ACCT.pos["SHSE.600099"] = {"qty": 500, "available": 500, "vwap": 9.0}
n_ord_before = len(ORDERS)
d2 = drive(1, [(9, 31), (10, 0), (14, 50)])
s_m = jload("sells_morning_%s.json" % d2)
s_c = jload("sells_close_%s.json" % d2)
out["days"]["d2"] = {
    "date": d2,
    "candidates": sorted(OSC.hooks._STATE["candidates"]),
    "orders": [(o["symbol"].split(".")[-1], int(o["volume"]), int(o["side"]))
               for o in ORDERS[n_ord_before:]],
    "morning_sells": [(x["code"], x["reason"], x["status"]) for x in s_m.get("sells", [])],
    "morning_summary": s_m.get("summary"),
    "close_sells": [(x["code"], x["reason"], x["status"]) for x in s_c.get("sells", [])],
    "close_summary": s_c.get("summary"),
    "book_after": {c: {k: v[k] for k in ("entry_date", "qty", "days_held")}
                   for c, v in book().items()},
    "acct": {s: v["qty"] for s, v in ACCT.pos.items()},
}
# 审计里的归属硬锁事件
conf = []
if os.path.exists(GM._AUDIT_LOG_PATH):
    for ln in open(GM._AUDIT_LOG_PATH, encoding="utf-8"):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("event") == "strategy_lock_conflict":
            conf.append({k: e.get(k) for k in ("code", "reason", "strategy")})
out["lock_conflicts"] = conf
out["book_files"] = sorted(os.listdir(BOOK))

try:
    GM._audit_close()
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
except Exception:
    pass

print("RESULT " + json.dumps(out, ensure_ascii=False, default=str))
