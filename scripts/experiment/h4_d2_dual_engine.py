# -*- coding: utf-8 -*-
"""H4 假说离线回测 · D2：双侧仿真引擎（H4 波段链 vs OSC 现行规则，同一候选清单对打）。

排期：`doc/solutions/2026-11-25_H4离线回测施工排期.md` D2。
本工序目标 = **先把引擎管线跑通**（默认只冒烟最近 3 个 sent_ma 窗口），全窗口留 D3。

## 口径（离线近似，诚实披露——日线数据无法 1:1 复刻盘中规则）

**共用入场**：信号日次日开盘买。一字涨停（开=高=低 且涨幅达板限）按不可成交剔除。
  板限：60/00 开头 9.5%，300/688 19.5%（宇宙已剔北交所）。

**H4 侧**（复盘清单 H4 登记条）：
  · 每日收盘判 MA5；盘中判压力位 = MA21/34/55 中**高于昨收的最近一根**，
    high 触及 ⇒ 次日开盘抛（一字跌停顺延到下一日开盘）
  · 抛出当日收盘 ≥ MA5 ⇒ 次日开盘买回（回接链不限次数；上限变体留 D4）
  · 收盘 < MA5 ⇒ 次日开盘卖、波段终结（不再回接）
  · 数据末尾仍持仓 ⇒ 按末日收盘标记 `open_end` 结算（冒烟期大多数如此，正常）

**OSC 侧**（现行规则的日线近似，偏差逐项披露）：
  · 入场过滤：dev20_prev<0（超跌，昨收<MA20）∧ 跳空守住（open>昨收 且 low≥昨收）
    ∧ 站上昨收（close>昨收）；入场价≈**开盘价**（生产为 10:00 棒收盘，无法复刻，偏乐观/悲观不定）
  · 缺口/VWAP 第三条件（close>10:00 VWAP）日线不可复刻 ⇒ **省略**（披露）
  · pop：入场次日若 high≥入场×1.03 ⇒ 按 max(1.03×入场, 当日open) 卖
    （生产 E1 口径为 10:00 棒 close；用触发价近似，**略偏乐观**，披露）
  · SL5 只深：dev20_prev<−7% 且 收盘<入场×0.95 ⇒ 当日收盘卖（14:50≈收盘近似）
  · 到期：入场后满 5 个交易日 ⇒ 第 5 日收盘卖
  · 板块 top-5 过滤与指数 regime 闸门：**本版不接**（披露，D3/D4 评估是否补）

**费用**：`core/cost_model.py` 股票口径（买 0.0000954 / 卖 0.0005954）。

产物：`t_io/validation/h4_backtest/trades_h4.csv` / `trades_osc.csv` + 控制台对照摘要。
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

from core.cost_model import FEE_BUY_STOCK, FEE_SELL_STOCK  # noqa: E402

OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h4_backtest")
CACHE_DIR = os.path.join(_ROOT, "t_io", "cache", "daily_kline")

POP_PCT = 0.03          # osc_pop_target_pct
STOP_PCT = -0.05        # osc_stop_pct
DEEP_DEV20 = -0.07      # osc_stop_deep_dev20
HOLD_DAYS = 5           # osc_hold_days
MA_PRESSURE = (21, 34, 55)


class Bars:
    """单票日线容器（懒加载）。"""

    def __init__(self, code: str):
        fp = os.path.join(CACHE_DIR, f"{code}.json")
        with open(fp, encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("rows") or []
        rows = [r for r in rows if r.get("date")]
        rows.sort(key=lambda r: r["date"])
        self.dates = [r["date"] for r in rows]
        self.o = [float(r["open"]) for r in rows]
        self.h = [float(r["high"]) for r in rows]
        self.l = [float(r["low"]) for r in rows]
        self.c = [float(r["close"]) for r in rows]

    def idx(self, date: str):
        i = bisect.bisect_left(self.dates, date)
        return i if i < len(self.dates) and self.dates[i] == date else None

    def ma(self, n: int, i: int) -> float | None:
        """索引 i 日（含）的 MA n；历史不足返回 None。"""
        if i + 1 < n:
            return None
        return sum(self.c[i - n + 1:i + 1]) / n


def _limit_of(code: str) -> float:
    return 0.195 if code[:3] in ("300", "688") else 0.095


def _one_way(b: Bars, i: int, direction: str) -> bool:
    """一字板（开=高=低 且达板限）；direction: 'up'/'down'。"""
    if i < 1 or i >= len(b.dates):
        return False
    if not (b.o[i] == b.h[i] == b.l[i]):
        return False
    chg = b.c[i] / b.c[i - 1] - 1.0
    lim = _limit_of(getattr(b, "code", ""))
    return chg >= lim if direction == "up" else chg <= -lim


def _net_ret(buy_px: float, sell_px: float) -> float:
    return (sell_px * (1 - FEE_SELL_STOCK) - buy_px * (1 + FEE_BUY_STOCK)) / buy_px


def simulate_h4(b: Bars, entry_i: int):
    """H4 波段链。返回 legs 列表 + 汇总。legs: dict(buy_date,buy_px,sell_date,sell_px,exit,ret)。"""
    legs = []
    i = entry_i
    n = len(b.dates)
    while True:
        # —— 持有期逐日 ——
        sell_i, why = None, None
        j = i
        while j < n:
            prev_close = b.c[j - 1] if j >= 1 else None
            ma5 = b.ma(5, j)
            # 压力位：MA21/34/55 中高于昨收的最近一根
            pressure = None
            if prev_close:
                above = [m for m in (b.ma(k, j) for k in MA_PRESSURE)
                         if m is not None and m > prev_close]
                pressure = min(above) if above else None
            if pressure is not None and b.h[j] >= pressure:
                why = "pressure_touch"
            elif ma5 is not None and b.c[j] < ma5:
                why = "ma5_break"
            if why:
                # 次日开盘卖；一字跌停顺延
                k = j + 1
                while k < n and _one_way(b, k, "down"):
                    k += 1
                sell_i = k if k < n else None
                break
            j += 1
        if why is None:                       # 数据末尾仍持仓
            legs.append({"buy_i": i, "sell_i": n - 1, "exit": "open_end"})
            return legs, "open_end"
        if sell_i is None:                    # 卖出日一字跌停顺延出界
            legs.append({"buy_i": i, "sell_i": n - 1, "exit": "open_end_limitdown"})
            return legs, "open_end"
        legs.append({"buy_i": i, "sell_i": sell_i, "exit": why})
        if why == "ma5_break":                # 波段终结
            return legs, "ma5_break"
        # pressure_touch 抛出：卖日收盘 ≥MA5 ⇒ 次日开盘买回
        if ma5_s_ge(b, sell_i, b.ma(5, sell_i)):
            k = sell_i + 1
            if k >= n:
                return legs, "open_end"
            while k < n and _one_way(b, k, "up"):   # 一字涨停买不进，顺延
                k += 1
            if k >= n:
                return legs, "open_end"
            i = k
            continue
        return legs, "no_buyback"


def ma5_s_ge(b, i, ma5):
    return ma5 is not None and b.c[i] >= ma5


def simulate_osc(b: Bars, entry_i: int, dev20_prev: float,
                 entry_filter: bool = True):
    """OSC 现行规则（日线近似）。返回单腿 dict 或 None（入场日条件不满足）。

    entry_filter=False ⇒ **C 臂：纯出场规则对照**（同一 H4 入场集，不做超跌/跳空过滤），
    用来隔离「出场/持股规则」本身的效应（entry 选择效应归 A/B 臂差异）。
    """
    prev_close = b.c[entry_i - 1] if entry_i >= 1 else None
    if prev_close is None:
        return None
    if entry_filter:
        # 跳空守住 + 站上昨收（VWAP 条件日线不可复刻，省略——披露）
        if not (b.o[entry_i] > prev_close and b.l[entry_i] >= prev_close
                and b.c[entry_i] > prev_close):
            return None
    entry_px = b.o[entry_i]
    n = len(b.dates)
    deep = dev20_prev is not None and dev20_prev < DEEP_DEV20
    for hold in range(1, HOLD_DAYS + 1):
        j = entry_i + hold
        if j >= n:
            return {"buy_i": entry_i, "sell_i": n - 1, "exit": "open_end",
                    "entry_px": entry_px}
        # 到期（14:50）与 SL5（14:50）同日判断；pop 仅次日（hold==1，10:00 先于 14:50）
        if hold == 1 and b.h[j] >= entry_px * (1 + POP_PCT):
            sell_px = max(entry_px * (1 + POP_PCT), b.o[j])
            return {"buy_i": entry_i, "sell_i": j, "exit": "pop_sell",
                    "entry_px": entry_px, "sell_px": sell_px}
        if deep and b.c[j] < entry_px * (1 + STOP_PCT):
            return {"buy_i": entry_i, "sell_i": j, "exit": "sl5_deep",
                    "entry_px": entry_px, "sell_px": b.c[j]}
        if hold == HOLD_DAYS:
            return {"buy_i": entry_i, "sell_i": j, "exit": "horizon_expired",
                    "entry_px": entry_px, "sell_px": b.c[j]}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="", help="逗号分隔 window_id；缺省=最近3个 sent_ma 窗口")
    ap.add_argument("--all", action="store_true", help="全部窗口（D3 用）")
    args = ap.parse_args()

    t0 = time.time()
    cands = list(csv.DictReader(open(os.path.join(OUT_DIR, "candidates.csv"),
                                     encoding="utf-8-sig")))
    wins = json.load(open(os.path.join(OUT_DIR, "windows.json"), encoding="utf-8"))

    if args.all:
        sel = {w["window_id"] for w in wins}
    elif args.windows:
        sel = set(args.windows.split(","))
    else:
        sent_wins = [w for w in wins if w["series"] == "sent_ma"]
        sel = {w["window_id"] for w in sent_wins[-3:]}
    cands = [r for r in cands if r["window_id"] in sel]
    print(f"窗口 {sorted(sel)}")
    print(f"候选 {len(cands)} 行")

    bars_cache: dict[str, Bars] = {}

    def bars(code):
        if code not in bars_cache:
            bars_cache[code] = Bars(code)
        return bars_cache[code]

    trades_h4, trades_osc, trades_armc = [], [], []
    skip = {"no_entry_bar": 0, "one_way_up": 0, "short_history": 0,
            "osc_filter_out": 0, "err": 0}
    for r in cands:
        code, edate = r["code"], r["entry_date"]
        if not edate:
            skip["no_entry_bar"] += 1
            continue
        try:
            b = bars(code)
        except Exception:
            skip["err"] += 1
            continue
        ei = b.idx(edate)
        if ei is None:
            skip["no_entry_bar"] += 1
            continue
        if ei < 55:
            skip["short_history"] += 1
            continue
        if _one_way(b, ei, "up"):
            skip["one_way_up"] += 1
            continue
        base = {"window_id": r["window_id"], "code": code, "name": r["name"],
                "board": r["board"], "signal_date": r["signal_date"],
                "dev20_prev": r["dev20_prev"]}

        # ---- H4 侧 ----
        legs, end = simulate_h4(b, ei)
        for ln, leg in enumerate(legs, 1):
            bi, si = leg["buy_i"], leg["sell_i"]
            rec = dict(base)
            rec.update({"leg": ln,
                        "buy_date": b.dates[bi], "buy_px": b.o[bi],
                        "sell_date": b.dates[si],
                        "sell_px": (b.o[si] if leg["exit"] not in ("open_end", "open_end_limitdown")
                                    else b.c[si]),
                        "exit": leg["exit"]})
            rec["ret_net"] = round(_net_ret(rec["buy_px"], rec["sell_px"]) * 100, 4)
            trades_h4.append(rec)

        # ---- OSC 侧 ----
        dev20 = float(r["dev20_prev"]) if r["dev20_prev"] not in ("", None) else None
        # C 臂：纯出场规则对照（同一入场，不做 OSC 入场过滤）——所有候选都跑
        t3 = simulate_osc(b, ei, dev20 if dev20 is not None else 0.0,
                          entry_filter=False)
        if t3 is not None:
            rec3 = dict(base)
            sp3 = t3.get("sell_px") or b.c[t3["sell_i"]]
            rec3.update({"buy_date": b.dates[t3["buy_i"]], "buy_px": t3["entry_px"],
                         "sell_date": b.dates[t3["sell_i"]], "sell_px": sp3,
                         "exit": t3["exit"]})
            rec3["ret_net"] = round(_net_ret(rec3["buy_px"], rec3["sell_px"]) * 100, 4)
            trades_armc.append(rec3)
        # B 臂：OSC 完整规则（含入场过滤）
        if dev20 is None or dev20 >= 0:
            skip["osc_filter_out"] += 1
            continue
        t = simulate_osc(b, ei, dev20)
        if t is None:
            skip["osc_filter_out"] += 1
            continue
        rec = dict(base)
        sell_px = t.get("sell_px")
        if sell_px is None:
            sell_px = b.c[t["sell_i"]]
        rec.update({"buy_date": b.dates[t["buy_i"]], "buy_px": t["entry_px"],
                    "sell_date": b.dates[t["sell_i"]], "sell_px": sell_px,
                    "exit": t["exit"]})
        rec["ret_net"] = round(_net_ret(rec["buy_px"], rec["sell_px"]) * 100, 4)
        trades_osc.append(rec)

    def dump(fp, rows):
        if not rows:
            return
        with open(fp, "w", encoding="utf-8-sig", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            wr.writeheader()
            wr.writerows(rows)

    dump(os.path.join(OUT_DIR, "trades_h4.csv"), trades_h4)
    dump(os.path.join(OUT_DIR, "trades_osc.csv"), trades_osc)
    dump(os.path.join(OUT_DIR, "trades_oscexit.csv"), trades_armc)

    def brief(name, rows):
        if not rows:
            print(f"{name}: 0 笔")
            return
        settled = [r for r in rows if not r["exit"].startswith("open_end")]
        ss = sorted([float(r["ret_net"]) for r in settled])
        print(f"{name}: {len(rows)} 笔（已结算 {len(settled)} / 在飞 {len(rows)-len(settled)}）")
        if ss:
            print(f"   已结算：均值 {sum(ss)/len(ss):+.3f}%  中位 {ss[len(ss)//2]:+.3f}%  "
                  f"胜率 {sum(1 for x in ss if x > 0)/len(ss)*100:.1f}%  "
                  f"最差 {ss[0]:+.2f}%  最好 {ss[-1]:+.2f}%")
            from collections import Counter
            print(f"   出场分布: {dict(Counter(r['exit'] for r in settled))}")

    print(f"\n== D2 完成（{time.time()-t0:.0f}s） ==  跳过统计: {skip}")
    brief("H4(波段链腿)          ", trades_h4)
    brief("OSC(完整规则·B臂)     ", trades_osc)
    brief("OSC出场·同入场集(C臂) ", trades_armc)

    # —— 逐候选配对对照（核心：同一入场，H4 复合 vs C 臂单腿） ——
    from collections import defaultdict
    h4_by_key = defaultdict(list)
    for r in trades_h4:
        h4_by_key[(r["window_id"], r["code"])].append(float(r["ret_net"]))
    c_by_key = {(r["window_id"], r["code"]): float(r["ret_net"]) for r in trades_armc}
    pairs = []
    for k, legs_ret in h4_by_key.items():
        if k not in c_by_key:
            continue
        comp = 1.0
        for x in legs_ret:
            comp *= 1 + x / 100.0
        pairs.append(((comp - 1) * 100, c_by_key[k]))
    if pairs:
        diffs = sorted(h - c for h, c in pairs)
        n = len(diffs)
        print(f"\n配对对照（H4复合 − C臂，n={n}）：均值 {sum(diffs)/n:+.3f}pp  "
              f"中位 {diffs[n//2]:+.3f}pp  H4胜出率 {sum(1 for d in diffs if d>0)/n*100:.1f}%")
    print(f"产物: {OUT_DIR}/trades_h4.csv / trades_osc.csv / trades_oscexit.csv")


if __name__ == "__main__":
    main()
