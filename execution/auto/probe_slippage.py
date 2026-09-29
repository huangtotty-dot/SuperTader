# -*- coding: utf-8 -*-
"""真实买侧滑点探针（预注册：doc/experiment/2026-09-24_真实买侧滑点测量_预注册.md）。

**目的**：量 `买侧滑点 = 成交价 / 09:31 首根 60s bar 的 open − 1`（用**委托回报**，不用 K 线）。
这是 OGR 规则唯一没被测量、却能单独定生死的量（优势 +0.34pp/腿，滑点 >0.35pp 即不成立）。

## 与生产的隔离（**硬要求，勿改**）
- **独立 `strategy_id`** ⇒ 独立纸面账户（生产 = e8bb1f4d-…，本探针 = PROBE_STRATEGY_ID）。
- **不 import gm_main**、不写 `holdings.json`、不写事件桥、不推飞书、不碰 GUI 确认闸。
- 只下探针单；任何异常 ⇒ 当日跳过（fail-closed，绝不猜）。

## T+1 的处理（**必须**）
A 股当日买入不可当日卖出 ⇒ 若 09:31 买、10:00 卖同一批，卖单必被拒。
故：**第 0 个交易日只建底仓**（每票 10 万），**从第 1 个交易日起**才做 T（09:31 买补、10:00 卖底仓）。
底仓那天的腿**不计入滑点样本**。

## 规则条件（与生产一致）
`mkt_gap(代理池中位) < 0` 且 `个股 gap = 09:31 open / 前收 − 1 ≤ −1%`。
代理池 = Stage18 冻死的 L20（`MARKET_PROXY`）——这也顺带验证了「live 注入 MARKET_PROXY」这一待办。

## 落盘
`t_io/validation/slippage_probe/{state.json, probe_YYYY-MM-DD.jsonl}`（UTF-8 JSONL，每笔一行）。

## 启动（**开盘前**，且须确认掘金终端在跑）
    python execution/auto/probe_slippage.py
    结束条件：跑满 --days 个交易日（默认 10）后自动 exit；或 Ctrl-C。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, time as dtime

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "execution", "auto"),
           os.path.join(_ROOT, "execution", "auto", "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 预注册冻结的标的与规模（改这里 = 改预注册）──────────────────────────────
PROBE = {                      # 篮子内成交额前三（已排除数据坏的 301396）
    "300166": "SZSE.300166",
    "603629": "SHSE.603629",
    "002733": "SZSE.002733",
}
MARKET_PROXY: dict = {}        # 下面从**规则单一源**填实（见文件尾部的 _build_proxy()）
LEG_NOTIONAL = 100_000.0       # 与生产 _OGR_LEG_NOTIONAL 一致
MAX_DAYS = 10                  # 跑满 10 个交易日（做 T 的天数，不含建底仓那天）
MAX_NOTIONAL_RATIO = 1.10      # 兜底：单腿名义额不得超目标 10%
BUY_AT, SELL_AT = dtime(9, 31), dtime(10, 0)

def _build_proxy() -> dict:
    """市场代理池 —— 从**规则单一源**取（`core/open_gap_reversal.MARKET_PROXY_CODES`，经胶水暴露）。

    与 live/回测同源；名单若各引一份，「大盘」就不是同一条规则（见该常量的注释）。
    """
    try:
        import ogr_shadow_glue as _g
        return {c: ("SHSE." if c[:1] in "569" else "SZSE.") + c
                for c in _g.proxy_codes() if c not in PROBE}
    except Exception as e:
        print(f"[probe] ⚠️ 代理池构建失败（mkt_gap 会退化）: {e}")
        return {}


MARKET_PROXY = _build_proxy()
PROBE_STRATEGY_ID = "6786d88d-bbac-11f1-88f2-98fa9b8df5e7"   # 独立账户，勿与生产混用
# （2026-09-29 国盛定制版实证：strategy_id==account_id 必须终端注册；
#   原拍脑袋 UUID 4f2a9d10-… 被柜台 1020 拒单，本 id 为 owner 终端新建仿真策略）
_DRY = os.environ.get("PROBE_DRY") == "1"    # 只记录不下单（先验证逻辑用）
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "slippage_probe")

from utils.gm_token import load_token          # noqa: E402
from gm.api import (                           # noqa: E402
    run, subscribe, order_volume, history_n, MODE_LIVE,
    OrderSide_Buy, OrderSide_Sell, OrderType_Market,
    PositionEffect_Open, PositionEffect_Close, ADJUST_PREV,
)


# ── 落盘（只写本探针自己的目录）─────────────────────────────────────────────
def _w(rec: dict) -> None:
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        rec.setdefault("ts", datetime.now().isoformat())
        day = rec.get("date") or datetime.now().strftime("%Y-%m-%d")
        with open(os.path.join(OUT_DIR, f"probe_{day}.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("[probe]", rec)
    except Exception as e:                      # 落盘失败不阻断
        print(f"[probe] 落盘失败: {e}")


def _save_state(st: dict) -> None:
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "state.json"), "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def _load_state() -> dict:
    try:
        with open(os.path.join(OUT_DIR, "state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"base": {}, "days_done": 0, "legs": []}


def _order(sym, qty, side, effect, log: dict):
    """下单（`PROBE_DRY=1` 时只记录、不真下 —— 用于先验证判定逻辑）。"""
    if _DRY:
        _w({**log, "DRY_RUN": True})
        return None
    try:
        r = order_volume(symbol=sym, volume=qty, side=side,
                         order_type=OrderType_Market, position_effect=effect)
        _w({**log, "order": r if isinstance(r, dict) else str(r)})
        return r
    except Exception as e:
        _w({**log, "event": str(log.get("event", "")) + "_FAIL", "err": str(e)})
        return None


def _positions(context) -> list:
    """账户全量持仓（list[dict]）。2026-09-29 实证：国盛定制版 `account().positions`
    是**方法**（须调用），旧代码把方法对象当列表迭代 ⇒ 永远空/报错，
    T_SELL 会永远看到 avail=0。此处统一调用口径。"""
    try:
        acct = context.account() if callable(getattr(context, "account", None)) else context.account
        attr = getattr(acct, "positions", None)
        if callable(attr):
            try:
                pos = attr()
            except TypeError:
                pos = attr(symbol="", side=0)
        elif isinstance(acct, dict):
            pos = acct.get("positions")
        else:
            pos = attr
        return [p for p in (pos or []) if isinstance(p, dict)]
    except Exception:
        return []


def _avail(context, sym: str) -> int:
    """该 symbol 的**可卖**数量。"""
    for p in _positions(context):
        if p.get("symbol") == sym:
            return int(p.get("available", 0) or 0)
    return 0


def _reconcile_base(context, today: str) -> None:
    """底仓对账：柜台**异步**拒单（资金不足）会让"同步被接受"的底仓实际不存在
    （2026-09-29 002733 实证）。每日一次把 state.base 与账户实际持仓对齐：
    实际=0 → 除名（下个窗口自动重试）；0<实际<记录 → 降为实际（部分成交）。"""
    st = getattr(context, "_st", None)
    if not isinstance(st, dict):
        return
    if st.get("_base_recon_date") == today:
        return
    base = st.get("base") or {}
    if not base:
        return
    pos = _positions(context)
    if not pos and st.get("base"):
        return          # 查询失败/空回报不轻举妄动（fail-closed：宁可不对账也不误删）
    held = {p.get("symbol"): int(p.get("volume", 0) or 0) for p in pos}
    changed = False
    for c in list(base):
        hv = held.get(PROBE[c], 0)
        if hv <= 0:
            del base[c]; changed = True
            _w({"event": "BASE_LOST", "date": today, "code": c, "held": hv})
        elif hv < base[c]:
            base[c] = hv; changed = True
            _w({"event": "BASE_PARTIAL", "date": today, "code": c, "held": hv})
    st["_base_recon_date"] = today
    _save_state(st)
    if changed:
        print(f"[probe] 底仓对账修正: {base}")


def _snapshot_positions(context, today: str) -> None:
    """持仓快照（按日一次，防缺仓日每根 bar 刷屏）+ 触发底仓对账（自带按日闸）。"""
    st = getattr(context, "_st", None)
    if isinstance(st, dict) and st.get("_snap_date") != today:
        try:
            rows = [{k: p.get(k) for k in
                     ("symbol", "volume", "available", "vwap", "amount") if k in p}
                    for p in _positions(context)]
            _w({"event": "POSITIONS", "date": today, "n": len(rows), "rows": rows})
        except Exception as e:
            _w({"event": "POSITIONS_FAIL", "date": today, "err": str(e)})
        st["_snap_date"] = today
        _save_state(st)
    _reconcile_base(context, today)


def _px_prev_close(sym: str):
    """上一已完成交易日的收盘（live 下 history_n(1d) 不含当日 forming bar）。"""
    try:
        his = history_n(symbol=sym, frequency="1d", count=3,
                        fields="eob,close", adjust=ADJUST_PREV, fill_missing="Previous")
        if his:
            return float(his[-1]["close"])
    except Exception:
        pass
    return 0.0


def init(context):
    st = _load_state()
    context._st = st
    context._opens, context._pc = {}, {}
    context._bought_today, context._sold_today = set(), set()
    context._day = None
    for c, s in {**PROBE, **MARKET_PROXY}.items():
        try:
            subscribe(symbols=s, frequency="60s", count=240,
                      fields="symbol,eob,open,high,low,close,volume,amount")
            pc = _px_prev_close(s)
            if pc > 0:
                context._pc[c] = pc
        except Exception as e:
            print(f"[probe] 订阅/取前收失败 {s}: {e}")
    print(f"[probe] strategy_id={PROBE_STRATEGY_ID}（独立纸面账户）"
          f"  标的={list(PROBE)}  代理={len(MARKET_PROXY)} 只"
          f"  前收={len(context._pc)} 条  底仓={st.get('base', {})}"
          f"  已做T天数={st.get('days_done')}")
    _w({"event": "init", "pc": context._pc, "state": st})


def _mk_qty(px: float) -> int:
    return int(LEG_NOTIONAL / px / 100) * 100 if px > 0 else 0


def on_bar(context, bars):
    now = context.now
    t, today = now.time(), now.strftime("%Y-%m-%d")
    if context._day != today:                    # 日界清零
        context._day = today
        context._opens, context._bought_today, context._sold_today = {}, set(), set()

    # ① 累积 09:31 首根 bar 的 open（逐票回调 ⇒ 必须跨回调累积）
    for b in (bars or []):
        try:
            s = b["symbol"] if isinstance(b, dict) else getattr(b, "symbol", None)
            op = float((b["open"] if isinstance(b, dict) else getattr(b, "open", 0)) or 0)
            cd = next((c for c, ss in {**PROBE, **MARKET_PROXY}.items() if ss == s), None)
            if cd and op > 0 and t >= BUY_AT:
                context._opens.setdefault(cd, op)
        except Exception:
            continue

    st = context._st
    # ② 建底仓（**按票记账**；这批腿不进滑点样本）。建底仓当天该票不做 T。
    # 2026-09-29 v2（实证：柜台**异步**拒单"资金不足"，同步接受≠成交）：
    # 弃用全局 base_done，改为 st["base"]={code: qty}；缺仓票每日一次重试，
    # 终端补足资金后自愈。
    base = st.setdefault("base", {})
    missing = [c for c in PROBE if not base.get(c)]
    if (missing and st.get("_base_attempt_date") != today and t >= BUY_AT
            and all(c in context._opens for c in missing)):
        st["_base_attempt_date"] = today
        for c in missing:
            q = _mk_qty(context._opens[c])
            if q <= 0:
                continue
            r = _order(PROBE[c], q, OrderSide_Buy, PositionEffect_Open,
                       {"event": "BASE_BUY", "date": today, "code": c, "qty": q,
                        "ref_open": context._opens[c]})
            if r:
                base[c] = q
        still = [c for c in PROBE if not base.get(c)]
        if still:
            _w({"event": "BASE_INCOMPLETE", "date": today,
                "have": sorted(base), "missing": still})
        _save_state(st)
    if missing and t >= SELL_AT:
        _snapshot_positions(context, today)          # 缺仓日核实 positions 字段名

    # ③ 规则条件（mkt_gap 只取代理池）
    need = [c for c in PROBE if c in context._opens]
    pg = [c for c in MARKET_PROXY if c in context._opens and c in context._pc]
    if len(pg) < 5 or not need:
        return
    mg = sorted((context._opens[c] / context._pc[c] - 1) for c in pg)
    mkt_gap = mg[len(mg) // 2] if len(mg) % 2 else (mg[len(mg) // 2 - 1] + mg[len(mg) // 2]) / 2

    # ④ 09:31 买入（做 T 的买腿 —— **这是滑点样本**）
    if t >= BUY_AT and t < SELL_AT and mkt_gap < 0 and st.get("days_done", 0) < MAX_DAYS:
        for c in need:
            if c in context._bought_today or c not in context._pc:
                continue
            if not base.get(c):
                continue        # 无底仓的票不做 T（防现金只进不出，2026-09-29 v2）
            gap = context._opens[c] / context._pc[c] - 1
            if gap > -0.010:
                continue
            q = _mk_qty(context._opens[c])
            if q <= 0 or q * context._opens[c] > LEG_NOTIONAL * MAX_NOTIONAL_RATIO:
                _w({"event": "SKIP_SIZING", "date": today, "code": c, "qty": q})
                continue
            _order(PROBE[c], q, OrderSide_Buy, PositionEffect_Open,
                   {"event": "T_BUY", "date": today, "code": c, "qty": q,
                    "ref_open": context._opens[c], "prev_close": context._pc[c],
                    "gap": round(gap, 6), "mkt_gap": round(mkt_gap, 6)})
            context._bought_today.add(c)
        _w({"event": "DECISION", "date": today, "mkt_gap": round(mkt_gap, 6),
            "proxy_n": len(pg), "probe_opens": {c: context._opens[c] for c in need}})

    # ⑤ 10:00 卖出（卖的是**底仓**，T+1 下合法）
    if t >= SELL_AT and st.get("days_done", 0) < MAX_DAYS:
        for c in sorted(context._bought_today):
            if c in context._sold_today:
                continue
            try:
                avail = _avail(context, PROBE[c])
                q = min(avail, _mk_qty(context._opens.get(c, 0)))
                if q < 100:
                    _w({"event": "T_SELL_SKIP", "date": today, "code": c, "avail": avail})
                    continue
                _order(PROBE[c], q, OrderSide_Sell, PositionEffect_Close,
                       {"event": "T_SELL", "date": today, "code": c, "qty": q,
                        "ref_px_1000": context._opens.get(c)})
                context._sold_today.add(c)
            except Exception as e:
                _w({"event": "T_SELL_FAIL", "date": today, "code": c, "err": str(e)})
        if context._sold_today and not _DRY and st.get("_counted_today") != today:
            st["days_done"] = st.get("days_done", 0) + 1
            st["_counted_today"] = today
            _save_state(st)
            _snapshot_positions(context, today)
            if st["days_done"] >= MAX_DAYS:
                _w({"event": "DONE", "days_done": st["days_done"]})
                print(f"[probe] 已跑满 {MAX_DAYS} 个交易日，退出。")
                os._exit(0)          # GM 的回调线程会吞掉 SystemExit ⇒ 用硬退出


def on_order_status(context, order):
    """把**委托回报**落盘（滑点的唯一合法来源）。"""
    try:
        sym = order["symbol"]
        side = "BUY" if order.get("side") == 1 else "SELL"
        status = order.get("status")
        px = order.get("filled_vwap") or order.get("vwap") or order.get("price") or 0
        cd = next((c for c, s in {**PROBE, **MARKET_PROXY}.items() if s == sym), None)
        _w({"event": "ORDER_STATUS", "date": context.now.strftime("%Y-%m-%d"),
            "code": cd, "sym": sym, "side": side, "status": status,
            "qty": order.get("volume"), "fill_px": float(px or 0),
            "order_id": str(order.get("id") or ""),
            "rej": order.get("ord_rej_reason_detail") or ""})
    except Exception as e:
        print(f"[probe] on_order_status 落盘失败: {e}")


if __name__ == "__main__":
    print(f"[probe] 启动（独立 strategy_id={PROBE_STRATEGY_ID}，纸面账户；"
          f"预注册见 doc/experiment/2026-09-24_真实买侧滑点测量_预注册.md）")
    _w({"event": "PROCESS_START", "strategy_id": PROBE_STRATEGY_ID,
        "uuid_sanity": str(uuid.UUID(PROBE_STRATEGY_ID))})
    run(strategy_id=PROBE_STRATEGY_ID, filename=os.path.basename(__file__),
        mode=MODE_LIVE, token=load_token())
