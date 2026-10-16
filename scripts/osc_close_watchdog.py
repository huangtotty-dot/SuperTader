# -*- coding: utf-8 -*-
r"""osc_close_watchdog.py — 14:50 卖侧硬性时间窗补跑看门狗（2026-10-15 施工）。

## 为什么要有它

2026-10-15 13:00 策略进程静默挂死（stdout 管道写满阻塞，py-spy 实锤），
**14:50 卖侧三件套（SL5只深止损 + 满5日到期平仓 + days_held 推进）整日缺席**。
ops_guard 的防堵整改（e7ebf2c）防"再挂"，本脚本防"万一还挂了，卖侧不能缺席"：
策略进程之外的一条**独立补跑通道**，检测条件 = 当日 14:52 后
`t_io/state/weak_strong_book/sells_close_{date}.json` 不存在 ⇒ 按账本 positions.json
+ 实时行情重放 14:50 卖侧判定并下单（复用 executor.run_sell_exec，判定逻辑与策略
**同一份代码**，不是另写一套）。

## 拉起方式（保守，不动 watcher.py）

研究结论：**不挂进 gm_bridge/watcher.py**。watcher 是纯事件通知进程（不持 GM 上下文、
无下单通道、2s 轮询循环里塞下单逻辑会引入拒单/重入风险），接入成本与风险都高。
建议两选一（owner 定）：
  ① **Windows 计划任务**：每个交易日 14:52 启动一次本脚本（掘金终端在线即可），
     策略正常 ⇒ 文件已存在 ⇒ 秒退零副作用；策略死了 ⇒ 自动补跑。
     命令示例（任务计划程序，周一至周五 14:52）：
       "C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe" ^
         E:\superTrader\scripts\osc_close_watchdog.py
  ② 手动：发现策略挂了，14:52~14:58 之间双击/命令行跑一次。

## 安全闸（全部 fail-closed）

  · **幂等**：sells_close_{date}.json 存在 ⇒ 空操作退出（--force 也不覆盖此闸，
    重复执行会重复下单 + days_held 语义错乱，绝不允许）；
  · **时间窗**：只在 [14:52, 14:58) 内允许真实下单（补的是"当日 14:50 那一轮"，
    收盘后/次日跑出来的单没有意义还可能制造意外持仓）；窗口外默认拒绝，
    --force 仅用于演练，且仍受幂等闸约束；
  · **取数失败**：history 拿不到任何一只持仓现价 ⇒ 当日不卖（与策略 try_sell 同口径）；
  · **dry-run**：--dry-run 全程离线（不连 GM），在临时账本副本上重放判定，供复盘验证。

## 用法

  # 生产补跑（掘金终端在线，14:52~14:58）：
  python scripts/osc_close_watchdog.py
  # 离线干跑验证（不连 GM、不动真账本）：
  python scripts/osc_close_watchdog.py --dry-run \
      --prices 605198=62.0,688003=65.6,301373=108.0 --date 2026-10-15
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, time as dtime

# 2026-10-16 修：计划任务（schtasks）控制台是 GBK，print "⇒" 直接抛
# UnicodeEncodeError 致看门狗 14:52 崩溃缺席 ⇒ 入口强制 stdout/stderr 转 UTF-8
# （老 Python 无 reconfigure 也不崩，errors=replace 兜底）。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, "execution", "auto"), os.path.join(ROOT, "execution", "auto", "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("SUPERTRADER_ROOT", ROOT)

BOOK_DIR = os.path.join(ROOT, "t_io", "state", "weak_strong_book")
WINDOW_START = (14, 52)      # 策略 14:50 触发 + 2 分钟容差
WINDOW_END = (14, 58)        # 收盘前必须收工
STRATEGY_GM_ID = "e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7"   # 与 gm_main.py 同一策略（同账户）


# ════════════════════ 纯判定（可单测，不依赖 gm）════════════════════

def sells_close_path(date: str, book_dir: str = BOOK_DIR) -> str:
    return os.path.join(book_dir, "sells_close_%s.json" % date)


def already_done(date: str, book_dir: str = BOOK_DIR) -> bool:
    """策略当日 14:50 卖侧是否已正常跑过（产物文件存在即为跑过）。"""
    return os.path.exists(sells_close_path(date, book_dir))


def in_window(now: datetime) -> bool:
    t = now.time()
    return dtime(*WINDOW_START) <= t < dtime(*WINDOW_END)


def decide(now: datetime, date: str, book_dir: str = BOOK_DIR, force: bool = False) -> tuple:
    """返回 (action, reason)。action ∈ {noop_done, wait, refuse_outside_window, run}。

    幂等闸绝对优先：文件在 ⇒ 永不再跑（force 只放行了时间窗，不放行重复执行）。
    """
    if already_done(date, book_dir):
        return "noop_done", "sells_close_%s.json 已存在 ⇒ 策略已跑过，空操作" % date
    if in_window(now):
        return "run", "卖侧产物缺失且在补跑窗 [%02d:%02d,%02d:%02d) 内" % (
            WINDOW_START + WINDOW_END)
    if force:
        return "run", "卖侧产物缺失，--force 放行时间窗（演练用）"
    t = now.time()
    if t < dtime(*WINDOW_START):
        return "wait", "未到补跑窗 %02d:%02d（当前 %s）" % (
            WINDOW_START[0], WINDOW_START[1], now.strftime("%H:%M:%S"))
    return ("refuse_outside_window",
            "已过补跑窗 %02d:%02d（当前 %s）⇒ 拒绝下单（--force 仅演练可放行）" % (
                WINDOW_END[0], WINDOW_END[1], now.strftime("%H:%M:%S")))


def latest_prices_from_history(bars, codes) -> dict:
    """gm.history(df=False) 返回的 [{symbol,eob,close,...}] → {code: 最新 close}。

    与 hooks.latest_close_from_df 同口径（每只取时间序最后一根），但走 df=False
    ⇒ 不依赖 pandas（掘金内嵌 Python 与单测环境都更稳）。"""
    out = {}
    for c in codes:
        rows = [b for b in (bars or [])
                if str(b.get("symbol") or "").split(".")[-1] == c]
        if not rows:
            continue
        try:
            px = float(rows[-1].get("close") or 0)
        except Exception:
            continue
        if px > 0:
            out[c] = px
    return out


def replay_sell_side(gateway, date: str, prices: dict, book_dir: str = BOOK_DIR,
                     dry_run: bool = False, log=print) -> dict:
    """重放 14:50 卖侧：与策略 try_sell 完全同一条 executor.run_sell_exec 路径。

    ⚠️ days_held 推进也在其中（executor._advance_days_held，按 last_check_date 幂等）。
    """
    from execution.auto.oversold_gap_confirm import executor
    return executor.run_sell_exec(gateway, str(date), prices,
                                  book_dir=book_dir, dry_run=dry_run, log=log)


# ════════════════════ 在线通道（仅生产补跑路径用）════════════════════

class _WatchdogGateway:
    """最小鸭子 Gateway：get_positions/place_order/poll_order（卖侧够用，买侧不实现）。

    下单前先写事件桥 write_order（order_type="osc"）——孤儿闸靠它认归属，
    与 framework.GmGateway 同口径；市价单风格与策略一致。
    """

    def __init__(self, context, gm, log=print):
        self.context = context
        self.gm = gm
        self.log = log

    def get_cash(self):
        return 0.0            # 卖侧不用现金；返回 0 让任何意外买路径自然截断

    def get_positions(self):
        out = {}
        try:
            for p in (self.context.account().positions() or []):
                sym = (p.get("symbol") if isinstance(p, dict)
                       else getattr(p, "symbol", None))
                vol = int((p.get("volume") if isinstance(p, dict)
                           else getattr(p, "volume", 0)) or 0)
                if not sym or vol <= 0:
                    continue
                av = int((p.get("available") if isinstance(p, dict)
                          else getattr(p, "available", 0)) or 0)
                cost = float((p.get("vwap") if isinstance(p, dict)
                              else getattr(p, "vwap", 0)) or 0)
                code = str(sym).split(".")[-1]
                out[code] = {"qty": vol, "available": av, "cost": cost}
        except Exception as e:
            self.log("[watchdog] ⚠️ get_positions 失败（按空）: %s" % e)
        return out

    def place_order(self, code, side, qty, price):
        from gm_bridge.writer import write_order
        now = self.context.now if hasattr(self.context, "now") else datetime.now()
        write_order(str(now), code, side, int(qty), float(price or 0), order_type="osc")
        gm = self.gm
        sym = ("SHSE." if str(code)[:1] in "569" else "SZSE.") + str(code)
        o = gm.order_volume(symbol=sym, volume=int(qty),
                            side=gm.OrderSide_Buy if side == "BUY" else gm.OrderSide_Sell,
                            order_type=gm.OrderType_Market,
                            position_effect=(gm.PositionEffect_Open if side == "BUY"
                                             else gm.PositionEffect_Close))
        oid, rej = None, None
        for od in (o if isinstance(o, list) else [o]):
            if isinstance(od, dict):
                oid = oid or (od.get("id") or od.get("order_id"))
                if rej is None and int(od.get("status") or 0) in (4, 5, 8, 12):
                    rej = "status=%s" % od.get("status")
        if rej:
            return {"order_id": oid, "status": "rejected", "filled_qty": 0,
                    "filled_price": None, "message": "同步回报拒单 %s" % rej}
        return {"order_id": oid, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": ""}

    def poll_order(self, order_id, code):
        gm = self.gm
        sym = ("SHSE." if str(code)[:1] in "569" else "SZSE.") + str(code)
        try:
            orders = gm.get_orders(symbol=sym)
        except TypeError:
            orders = gm.get_orders()
        for od in (orders or []):
            try:
                if str(od.get("id") or od.get("order_id") or "") != str(order_id):
                    continue
                st = int(od.get("status") or 0)
                fq = int(od.get("filled_volume") or od.get("volume") or 0)
                fp = float(od.get("filled_vwap") or od.get("vwap")
                           or od.get("price") or 0) or None
                if st == 3:
                    return {"order_id": order_id, "status": "filled",
                            "filled_qty": fq, "filled_price": fp, "message": ""}
                if st in (4, 5, 8, 12):
                    return {"order_id": order_id, "status": "rejected", "filled_qty": 0,
                            "filled_price": None, "message": "status=%s" % st}
                if fq > 0:
                    return {"order_id": order_id, "status": "partial",
                            "filled_qty": fq, "filled_price": fp, "message": "status=%s" % st}
                return {"order_id": order_id, "status": "submitted", "filled_qty": 0,
                        "filled_price": None, "message": "status=%s" % st}
            except Exception:
                continue
        return {"order_id": order_id, "status": "submitted", "filled_qty": 0,
                "filled_price": None, "message": "order_not_found"}


# ════════════════════ GM 入口（on_bar 一次性触发）════════════════════

def init(context):
    context._wd_done = False


def on_bar(context, bars):
    if context._wd_done:
        return
    now = context.now
    today = now.strftime("%Y-%m-%d")
    action, reason = decide(now, today)
    if action == "wait":
        return                                   # 14:52 前的 bar 继续等
    context._wd_done = True
    print("[watchdog] %s ⇒ %s" % (action, reason))
    if action != "run":
        _safe_exit(0 if action == "noop_done" else 2)

    from gm_bridge.writer import write_risk
    write_risk(str(now), "osc_close_watchdog",
               "14:50 卖侧产物缺失，看门狗补跑启动（%s）" % reason)

    import gm.api as gm
    from execution.auto.oversold_gap_confirm import executor
    book = executor.load_book(BOOK_DIR)
    codes = sorted((book.get("open") or {}).keys())
    if not codes:
        print("[watchdog] 账本无持仓，无需补跑（仍落 sells_close 闭环当日）")
        gw = _WatchdogGateway(context, gm)
        res = replay_sell_side(gw, today, {}, book_dir=BOOK_DIR, dry_run=False)
        print("[watchdog] summary: %s" % json.dumps(res.get("summary"), ensure_ascii=False))
        _safe_exit(0)

    syms = [("SHSE." if c[:1] in "569" else "SZSE.") + c for c in codes]
    try:
        bars_df = gm.history(symbol=syms, frequency="60s",
                             start_time=now.strftime("%Y-%m-%d 09:25:00"),
                             end_time=now.strftime("%Y-%m-%d %H:%M:%S"),
                             fields="symbol,eob,close", adjust=gm.ADJUST_PREV, df=False)
    except Exception as e:
        print("[watchdog] 现价取数失败（fail-closed，不卖）: %s" % e)
        write_risk(str(now), "osc_close_watchdog", "补跑取数失败，放弃: %s" % str(e)[:150])
        _safe_exit(3)
    prices = latest_prices_from_history(bars_df, codes)
    if not prices:
        print("[watchdog] 一只现价都拿不到（fail-closed，不卖）")
        write_risk(str(now), "osc_close_watchdog", "补跑无现价，放弃")
        _safe_exit(3)

    gw = _WatchdogGateway(context, gm)
    res = replay_sell_side(gw, today, prices, book_dir=BOOK_DIR, dry_run=False)
    sm = res.get("summary") or {}
    print("[watchdog] 补跑完成: %s" % json.dumps(sm, ensure_ascii=False))
    write_risk(str(now), "osc_close_watchdog",
               "补跑完成 sells_placed=%s stops=%s expired=%s open_after=%s"
               % (sm.get("sells_placed"), sm.get("stop_count"),
                  sm.get("expired_count"), sm.get("open_after")))
    _safe_exit(0)


def _safe_exit(code):
    import time as _t
    _t.sleep(3)               # 给事件桥/日志落盘留时间
    os._exit(code)


# ════════════════════ CLI ════════════════════

class _DryGateway:
    """离线干跑通道：只打印，不下单；持仓照账本原样报（full available）。"""

    def __init__(self, book_open):
        self._open = book_open or {}

    def get_cash(self):
        return 0.0

    def get_positions(self):
        return {c: {"qty": int(r.get("qty") or 0), "available": int(r.get("qty") or 0),
                    "cost": float(r.get("entry_price") or 0)}
                for c, r in self._open.items()}

    def place_order(self, code, side, qty, price):
        print("  [DRY-ORDER] %s %s %d股 @%.3f" % (side, code, qty, price))
        return {"order_id": "dry", "status": "filled", "filled_qty": qty,
                "filled_price": price, "message": "dry"}

    def poll_order(self, order_id, code):
        return {"order_id": order_id, "status": "filled", "filled_qty": 0,
                "filled_price": None, "message": "dry"}


def _parse_prices(s: str) -> dict:
    out = {}
    for kv in (s or "").split(","):
        kv = kv.strip()
        if not kv:
            continue
        c, _, v = kv.partition("=")
        out[c.strip()] = float(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="OSC 14:50 卖侧补跑看门狗")
    ap.add_argument("--dry-run", action="store_true", help="离线重放（不连 GM、不动真账本）")
    ap.add_argument("--prices", default="", help="干跑注入现价：code=px,code=px")
    ap.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--force", action="store_true", help="放行时间窗（仅演练；幂等闸不可越过）")
    ap.add_argument("--now", default="", help="演练用虚拟当前时间 'YYYY-MM-DD HH:MM:SS'")
    args = ap.parse_args(argv)

    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M:%S") if args.now else datetime.now()
    action, reason = decide(now, args.date, force=args.force)
    print("[watchdog] decide ⇒ %s (%s)" % (action, reason))

    if args.dry_run:
        if action != "run":
            print("[watchdog] 干跑结束（无动作）")
            return 0 if action == "noop_done" else 2
        from execution.auto.oversold_gap_confirm import executor
        book = executor.load_book(BOOK_DIR)
        tmp = tempfile.mkdtemp(prefix="osc_watchdog_dry_")
        try:
            if os.path.exists(os.path.join(BOOK_DIR, "positions.json")):
                shutil.copy(os.path.join(BOOK_DIR, "positions.json"),
                            os.path.join(tmp, "positions.json"))
            prices = _parse_prices(args.prices)
            print("[watchdog] 干跑重放 %s 卖侧（账本副本 %s，prices=%s）" % (args.date, tmp, prices))
            res = replay_sell_side(_DryGateway(book.get("open")), args.date, prices,
                                   book_dir=tmp, dry_run=True)
            print("[watchdog] summary: %s" % json.dumps(res.get("summary"), ensure_ascii=False))
            for s in res.get("sells") or []:
                print("  %s days_held=%s → %s (%s)" % (s["code"], s.get("days_held"),
                                                       s["reason"], s.get("message")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0

    if action not in ("run", "wait"):
        return 0 if action == "noop_done" else 2
    # 生产补跑：交给 GM 终端（on_bar 里真正执行；wait ⇒ 挂到 14:52 窗口自动放行。
    # 此处 run 前 decide 已过一次闸，on_bar 内还会再 decide 一次——双闸）
    from gm.api import run, MODE_LIVE
    from utils.gm_token import load_token
    print("[watchdog] 进入 GM 补跑模式（策略 id 同 gm_main）")
    run(strategy_id=STRATEGY_GM_ID, filename=os.path.basename(__file__),
        mode=MODE_LIVE, token=load_token())
    return 0


if __name__ == "__main__":
    sys.exit(main())
