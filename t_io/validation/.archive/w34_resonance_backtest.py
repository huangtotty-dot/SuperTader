# -*- coding: utf-8 -*-
"""
w34_resonance_backtest.py — 指数共振 tushare 回测（2026-08-14 新增）

用 tushare stk_mins 历史 1 分钟数据，对持仓股回放 main.py 的做T逻辑（SignalEngine.evaluate
纯两点信号 + 通知阈值块），并叠加指数5分钟共振门控，量化共振过滤对做T成功率的影响。

做T成功率指标（与 daily_review settle 口径一致）：
  1. 信号命中率（主）：入场后 30 分钟内，目标(+0.5%/-0.5%) 先于 止损(-0.4%/+0.4%) 触及 = WIN，
     否则若触止损 = FAIL，都未触 = VOID。hit_rate = WIN/(WIN+FAIL)
  2. N 根 5 分钟K前瞻收益：信号价 vs +1/+3/+6 根收盘
  3. 共振分组对比：共振通过 / 共振拦截 / 数据缺失 三组命中率；gap = 通过−拦截 决定过滤是否有效
  4. FIFO 闭环盈亏（可选，仅共振 on 时记账）

用法：
    python t_io/validation/w34_resonance_backtest.py --date 2026-07-10 --resonance off
    python t_io/validation/w34_resonance_backtest.py --date 2026-07-10 --resonance on
输出：t_io/replay/resonance_{date}_{resonance}/  events.jsonl + metrics.json
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]  # 仓库根（本文件在 t_io/validation/ 下）
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import index_resonance as ir
from indicators import resample_to_5min, add_5min_indicators
from index_regime_intraday import _iri_tushare_pro

HOLDINGS_FILE = BASE / "holdings.json"
CACHE_DIR = BASE / "t_io" / "cache" / "tushare_mins"

# 做T成功率结算参数（对齐 daily_review.settle：+0.5%/-0.4%/30tick）
WIN_TARGET = {"BUY_LOW": 0.005, "SELL_HIGH": 0.005}   # 目标幅度
STOP = {"BUY_LOW": 0.004, "SELL_HIGH": 0.004}          # 止损幅度
SETTLE_TICKS = 30                                      # 30 根 1 分钟


def _stock_code_to_ts(code: str) -> str:
    base = str(code).split("_")[0]
    return (base + ".SH") if base[0] in "56" else (base + ".SZ")


def _load_tushare_mins(ts_code: str, date_str: str, pro) -> pd.DataFrame:
    """拉 tushare stk_mins 1 分钟线并缓存 CSV（幂等）。ts_code 形如 600176.SH / 000001.SH。"""
    fp = CACHE_DIR / ts_code / f"{date_str}.csv"
    if fp.exists():
        try:
            return pd.read_csv(fp, parse_dates=["time"])
        except Exception:
            pass
    df = pro.stk_mins(ts_code=ts_code, freq="1min",
                      start_date=f"{date_str} 09:00:00", end_date=f"{date_str} 19:00:00")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={"trade_time": "time", "vol": "volume"})
    keep = [c for c in ("time", "open", "close", "high", "low", "volume", "amount") if c in df.columns]
    df = df[keep].copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    fp.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(fp, index=False)
    return df


def _resonance_asof(index_code: str, idx_5min: pd.DataFrame, bt, action: str, price: float) -> dict:
    """按信号时刻 bt 取指数 5 分钟最新根判定共振（5 分钟根滚动指标不含未来，无未来函数）。"""
    empty = {"index_code": index_code, "index_name": idx_5min.attrs.get("name", "") if hasattr(idx_5min, "attrs") else ""}
    if idx_5min is None or idx_5min.empty:
        return ir.verdict_from_indicators(None, action, price, empty, missing=True, reason="无指数5分钟")
    bars = idx_5min[idx_5min["time"] <= pd.Timestamp(bt)]
    min_bars = int(ir._params().get("min_index_5m_bars", 5))
    if len(bars) < min_bars:
        return ir.verdict_from_indicators(None, action, price, empty, missing=True,
                                          reason=f"指数5分钟仅{len(bars)}根(<{min_bars})")
    last = bars.iloc[-1]
    ind = {
        "index_code": index_code,
        "index_name": idx_5min.attrs.get("name", ""),
        "bb_pct_5m": float(last.get("bb_pct_5m")) if pd.notna(last.get("bb_pct_5m")) else None,
        "rsi_5m_p6": float(last.get("rsi_5m_p6")) if pd.notna(last.get("rsi_5m_p6")) else None,
        "dif_5m": float(last.get("dif_5m")) if pd.notna(last.get("dif_5m")) else None,
        "dea_5m": float(last.get("dea_5m")) if pd.notna(last.get("dea_5m")) else None,
    }
    return ir.verdict_from_indicators(None, action, price, ind)


def _signal_outcome(df1: pd.DataFrame, bt, action: str, price: float) -> str:
    """信号结算：30 分钟内目标先触=WIN、止损先触=FAIL、均未触=VOID。"""
    times = df1["time"].values
    start = int(np.searchsorted(times, np.datetime64(bt)))
    tgt = 1 + WIN_TARGET.get(action, 0.005)
    stp = 1 - STOP.get(action, 0.004)
    if action in ("BUY_LOW", "ADD_POS"):
        for i in range(start + 1, min(start + SETTLE_TICKS + 1, len(df1))):
            p = float(df1.iloc[i]["close"])
            if p <= price * stp:
                return "FAIL"
            if p >= price * tgt:
                return "WIN"
    else:
        for i in range(start + 1, min(start + SETTLE_TICKS + 1, len(df1))):
            p = float(df1.iloc[i]["close"])
            if p >= price * (1 + STOP.get(action, 0.004)):
                return "FAIL"
            if p <= price * (1 - WIN_TARGET.get(action, 0.005)):
                return "WIN"
    return "VOID"


def _forward_returns(df1: pd.DataFrame, bt, price: float) -> dict:
    """信号价 → +5/+15/+30 分钟收盘收益。"""
    out = {}
    for label, minutes in (("m5", 5), ("m15", 15), ("m30", 30)):
        target_t = pd.Timestamp(bt) + pd.Timedelta(minutes=minutes)
        sub = df1[df1["time"] <= target_t]
        if sub.empty:
            out[label] = None
        else:
            out[label] = round(float(sub.iloc[-1]["close"]) / price - 1, 5) if price else None
    return out


def _notify_threshold(PARAMS, STOCK_PARAMS, code, action, hhmm, daily_ctx):
    _sp = STOCK_PARAMS.get(code, {})
    if action in ("BUY_LOW", "ADD_POS"):
        return _sp.get("notify_buy_threshold") or PARAMS.get("notify_buy_threshold", 68)
    if hhmm >= 1000:
        return _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_threshold", 65)
    today_ret = daily_ctx.get("daily_day_ret", 0.0)
    if today_ret < -0.04 and action in ("PANIC_SELL", "SELL_HIGH"):
        return PARAMS.get("notify_sell_panic_threshold", 60)
    return _sp.get("notify_sell_threshold") or PARAMS.get("notify_sell_early_threshold", 75)


def _fifo_net(sells, buys, commission, stamp):
    """正T 近似 FIFO：先卖(SELL_HIGH)后买(BUY_LOW)配对，net=(卖价−买价)×股数−费。
    qty 近似按每股 100 份（无当日 T 模式/持仓数据时的简化估计）。"""
    sq = [(float(t[1] or 0), int(t[2] or 0)) for t in sells]
    bq = [(float(t[1] or 0), int(t[2] or 0)) for t in buys]
    si = 0
    total = closed = 0
    for bp, bqty in bq:
        while si < len(sq) and bqty > 0:
            sp, sqty = sq[si]
            m = min(bqty, sqty)
            if bp > 0 and sp > 0 and m > 0:
                gross = (sp - bp) * m
                net = gross - bp * m * commission - sp * m * (commission + stamp)
                total += net
                closed += 1
            sqty -= m
            bqty -= m
            sq[si] = (sp, sqty)
            if sqty <= 0:
                si += 1
    return total, closed


def main():
    ap = argparse.ArgumentParser(description="指数共振 tushare 回测")
    ap.add_argument("--date", default="2026-07-10", help="回测日 YYYY-MM-DD")
    ap.add_argument("--resonance", choices=["on", "off"], default="off",
                    help="on=启用共振门控（仅通过信号计入推送/记账）；off=不过滤基线")
    ap.add_argument("--codes", nargs="*", default=None, help="覆盖持仓代码（默认 holdings.json qty>0）")
    args = ap.parse_args()

    date_str = args.date
    pro = _iri_tushare_pro()

    # ── 持仓池 ──
    if args.codes:
        codes = list(args.codes)
    else:
        holdings = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8")) if HOLDINGS_FILE.exists() else {}
        codes = [c for c, h in holdings.items() if isinstance(h, dict) and int(h.get("qty") or 0) > 0]
        codes = [c.split("_")[0] for c in codes]
    names = {}
    if HOLDINGS_FILE.exists():
        holdings = json.loads(HOLDINGS_FILE.read_text(encoding="utf-8"))
        names = {c.split("_")[0]: h.get("name", c) for c, h in holdings.items() if isinstance(h, dict)}
    codes = sorted(set(codes))

    # ── 拉数据（个股 1 分钟 + 指数 1 分钟→5 分钟指标）──
    print(f"[load] date={date_str} codes={codes}")
    stock_1min = {}
    for c in codes:
        ts = _stock_code_to_ts(c)
        df1 = _load_tushare_mins(ts, date_str, pro)
        stock_1min[c] = df1
        print(f"  {c} {ts}: {len(df1)} 根" if not df1.empty else f"  {c} {ts}: 无数据")

    index_names = {}
    idx_5min = {}
    for c in codes:
        ic, iname = ir.resolve_index(c)
        index_names[ic] = iname
    for ic in index_names:
        ts = ir._index_code_to_ts(ic)
        df1 = _load_tushare_mins(ts, date_str, pro)
        if df1.empty:
            print(f"  [指数] {ic} {ts}: 无数据 → 该股信号将按 fail_closed 拦截")
            continue
        df5 = resample_to_5min(df1)
        if df5 is None or df5.empty:
            continue
        df5 = add_5min_indicators(df5)
        df5.attrs["name"] = index_names[ic]
        idx_5min[ic] = df5
        print(f"  [指数] {ic} {ts}: {len(df5)} 根 5 分钟")

    # ── 共享命名空间（复用 replay_day：exec config/indicators/signal_engine 等）──
    sys.path.insert(0, str(BASE))
    import replay_day as _rd
    shared = _rd.load_shared()
    SignalEngine = shared["SignalEngine"]
    add_indicators = shared["add_indicators"]
    PARAMS = shared["PARAMS"]
    STOCK_PARAMS = shared["STOCK_PARAMS"]
    MINUTE_FETCH_STATUS = shared.get("MINUTE_FETCH_STATUS")
    if MINUTE_FETCH_STATUS is not None:
        for c in codes:
            MINUTE_FETCH_STATUS[c] = "ok"

    out_dir = BASE / "t_io" / "replay" / f"resonance_{date_str}_{args.resonance}"
    out_dir.mkdir(parents=True, exist_ok=True)
    events_path = out_dir / "events.jsonl"
    if events_path.exists():
        events_path.unlink()
    # 环境隔离：evaluate 内部落盘（decision_trace / 虚拟账）全部指向回放目录，不碰实盘 traces
    shared["TRACE_DIR"] = str(out_dir)
    shared["VIRTUAL_TRADES_FILE"] = str(out_dir / "virtual_trades.json")
    if "PERSIST_INTRADAY_STATE" in shared:
        shared["PERSIST_INTRADAY_STATE"] = False

    engines = {c: SignalEngine() for c in codes}
    signals = []
    all_times = sorted(set().union(*[set(stock_1min[c]["time"]) for c in codes if not stock_1min[c].empty]))

    for bt in all_times:
        shared["SIM_NOW"] = bt.to_pydatetime()
        hhmm = bt.hour * 100 + bt.minute
        for c in codes:
            df1 = stock_1min[c]
            if df1.empty:
                continue
            sub = df1[df1["time"] <= bt]
            if len(sub) < 25:
                continue
            df_ind = add_indicators(sub.copy())
            price = float(df_ind.iloc[-1]["close"])
            holding = {"name": names.get(c, c), "t_qty": 0, "qty": 0, "type": "stock", "cost": 0.0}
            daily_ctx = {"daily_status": "ok", "daily_price_ref": price}
            try:
                _, _, sig = engines[c].evaluate(c, holding["name"], df_ind, holding, daily_ctx=daily_ctx)
            except Exception:
                continue
            if sig is None or sig.action not in ("BUY_LOW", "ADD_POS", "SELL_HIGH", "PANIC_SELL"):
                continue
            nth = _notify_threshold(PARAMS, STOCK_PARAMS, c, sig.action, hhmm, daily_ctx)
            if float(sig.score or 0) < nth:
                continue
            res = _resonance_asof(ir.resolve_index(c)[0], idx_5min.get(ir.resolve_index(c)[0]), bt, sig.action, price)
            group = "data_missing" if res.get("missing") else ("pass" if res.get("gate_pass") else "block")
            out = _signal_outcome(df1, bt, sig.action, price)
            fr = _forward_returns(df1, bt, price)
            signals.append({
                "ts": str(bt)[11:16], "code": c, "name": names.get(c, c), "action": sig.action,
                "price": round(price, 3), "score": float(sig.score or 0), "nth": nth,
                "group": group, "gate": res.get("gate", ""), "gate_pass": bool(res.get("gate_pass")),
                "missing": bool(res.get("missing")), "outcome": out,
                "index_code": res.get("index_code", ""),
                "fwd": fr,
            })

    # ── 汇总指标 ──
    def _grp_stats(rows):
        g = {}
        for grp in ("pass", "block", "data_missing"):
            sub = [r for r in rows if r["group"] == grp]
            w = sum(1 for r in sub if r["outcome"] == "WIN")
            f = sum(1 for r in sub if r["outcome"] == "FAIL")
            v = sum(1 for r in sub if r["outcome"] == "VOID")
            f5 = [r["fwd"]["m5"] for r in sub if r["fwd"].get("m5") is not None]
            f15 = [r["fwd"]["m15"] for r in sub if r["fwd"].get("m15") is not None]
            g[grp] = {
                "n": len(sub), "wins": w, "fails": f, "void": v,
                "hit_rate": round(w / (w + f), 4) if (w + f) else None,
                "avg_fwd_5m": round(float(np.mean(f5)), 4) if f5 else None,
                "avg_fwd_15m": round(float(np.mean(f15)), 4) if f15 else None,
            }
        return g

    def _per_code(rows):
        out = {}
        for r in rows:
            out.setdefault(r["code"], {"n": 0, "wins": 0, "fails": 0, "void": 0, "group": {}})
            out[r["code"]]["n"] += 1
            _key = {"WIN": "wins", "FAIL": "fails", "VOID": "void"}.get(r["outcome"], "void")
            out[r["code"]][_key] += 1
            out[r["code"]]["group"].setdefault(r["group"], 0)
            out[r["code"]]["group"][r["group"]] += 1
        for c, v in out.items():
            v["hit_rate"] = round(v["wins"] / (v["wins"] + v["fails"]), 4) if (v["wins"] + v["fails"]) else None
        return out

    emitted = [r for r in signals if args.resonance == "off" or r["gate_pass"]]
    by_group = _grp_stats(signals)
    by_code = _per_code(emitted)

    # FIFO 闭环（仅 emitted 信号记账；多账户同 code 近似合并）
    commission = PARAMS.get("commission_rate", 0.0015)
    stamp = PARAMS.get("stamp_tax_rate", 0.0005)
    book = {}
    total_net = closed = 0
    for c in codes:
        buys = [(r["ts"], r["price"], 100) for r in emitted if r["code"] == c and r["action"] in ("BUY_LOW", "ADD_POS")]
        sells = [(r["ts"], r["price"], 100) for r in emitted if r["code"] == c and r["action"] in ("SELL_HIGH", "PANIC_SELL")]
        net, cc = _fifo_net(sells, buys, commission, stamp)
        book[c] = {"buys": len(buys), "sells": len(sells), "matched_net": round(net, 2), "closed_cycles": cc}
        total_net += net
        closed += cc

    hit_pass = by_group["pass"]["hit_rate"]
    hit_block = by_group["block"]["hit_rate"]
    gap = (hit_pass - hit_block) if (hit_pass is not None and hit_block is not None) else None
    if gap is None:
        verdict = "样本不足"
    elif gap > 0.05:
        verdict = "filter_effective"
    elif gap < -0.05:
        verdict = "filter_harmful"
    else:
        verdict = "filter_neutral"

    metrics = {
        "date": date_str, "resonance_gate": ir._params().get("gate", "same_direction"),
        "resonance_mode": args.resonance,
        "n_emitted": len(emitted), "n_total": len(signals),
        "by_group": by_group, "by_code": by_code,
        "hit_rate_gap": round(gap, 4) if gap is not None else None,
        "verdict": verdict,
        "book": book, "matched_net_total": round(total_net, 2), "closed_cycles_total": closed,
        "fail_closed": bool(ir._params().get("fail_closed", True)),
        "note": "口径: +0.5%/-0.4%/30tick，与 daily_review.settle 一致；n<20 样本结论仅供参考",
    }
    with open(events_path, "w", encoding="utf-8") as f:
        for r in signals:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n== {date_str} resonance={args.resonance} ==")
    print(f"信号 {len(signals)} 条（emitted={len(emitted)}）")
    for grp in ("pass", "block", "data_missing"):
        g = by_group[grp]
        print(f"  {grp:12s} n={g['n']:3d} wins={g['wins']:3d} fails={g['fails']:3d} "
              f"hit_rate={('%.0f%%' % (g['hit_rate'] * 100)) if g['hit_rate'] is not None else '—':>5} "
              f"avg15m={('%.2f%%' % (g['avg_fwd_15m'] * 100)) if g['avg_fwd_15m'] is not None else '—'}")
    if gap is not None:
        print(f"命中率差 通过−拦截 = {gap:+.2%} → {verdict}")
    print(f"FIFO 闭环: net={total_net:.2f} 周期={closed}")
    print(f"[OK] 输出 → {out_dir}")


if __name__ == "__main__":
    main()
