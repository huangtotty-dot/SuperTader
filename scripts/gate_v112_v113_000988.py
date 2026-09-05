# -*- coding: utf-8 -*-
"""gate_v112_v113_000988.py — 华工科技(000988) 做T低吸 v1.1.2 vs v1.1.3 闸门放行对比

背景: v1.1.2(tag=ab9a9407) 做T低吸 BUY_LOW 仍被两道「顺趋势建仓闸」拦截:
        C-2 个股日线破 MA5(收盘<5日均线)只卖不买
        C-1 指数5min共振(深证成指价<指数5min MA5) 拦截 BUY 侧
      v1.1.3(tag=419e707a, 当前生产) 经 c564aa25 对 swing_renko 做T低吸跳过这两闸。

口径: 回放 core/t_decision.TDecisionEngine(与生产 SignalEngine 同决策核),
      逐笔 BUY_LOW→SELL_HIGH 闭环。双臂:
        V112  apply_gate=True   旧行为: BUY 若 C-2 或 C-1 拦截则不开仓
        V113  apply_gate=False  新行为: BUY 全放行
指标(等权每股闭环): 闭环数/胜率/合计收益(%)/平均每笔/被拦截 BUY 数及去向。

产出: t_io/gate_v112_v113_000988.json
用法: python scripts/gate_v112_v113_000988.py [--start 2026-06-01 --end 2026-09-04]
"""
import argparse, json, os, sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

CODE = "000988"
INDEX_TS = "399001.SZ"          # 深证成指（000 开头 → sz399001）
SNAP = Path(os.environ.get("T_SNAPSHOT_DIR", str(BASE / "t_io" / "minute_snapshots")))


def load_snapshots(code, date_str):
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    ym = SNAP / str(dt.year) / f"{dt.month:02d}"
    for p in (ym / f"{code}_{date_str}.json", ym / f"{code}_A_{date_str}.json",
              ym / f"{code}_B_{date_str}.json", SNAP / f"{code}_{date_str}.json"):
        if p.exists():
            break
    else:
        return pd.DataFrame()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return pd.DataFrame()
    snaps = data.get("bars") or data.get("snapshots") or (data if isinstance(data, list) else [])
    rows = []
    for s in snaps:
        t = s.get("time", "")
        if len(str(t)) <= 5:
            t = f"{date_str} {t}"
        rows.append({"time": t, "open": float(s.get("open", 0) or 0),
                     "high": float(s.get("high", 0) or 0), "low": float(s.get("low", 0) or 0),
                     "close": float(s.get("close", 0) or 0),
                     "volume": float(s.get("volume", 0) or 0),
                     "amount": float(s.get("amount", 0) or 0)})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    return df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def load_daily_rows(code):
    p = BASE / "t_io" / "cache" / "daily_kline" / f"{code}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    rows = data.get("rows", [])
    df = pd.DataFrame(rows)
    if df.empty or "date" not in df.columns or "close" not in df.columns:
        return df
    return df.sort_values("date").reset_index(drop=True)


def below_ma5_on(daily_df, date_str):
    """C-2: date_str 盘中，个股最新可得收盘(前一日) < 5日均线 → 破线。数据不足返回 None。"""
    if daily_df.empty:
        return None
    d = daily_df[daily_df["date"] < date_str]
    if len(d) < 6:
        return None
    c = d["close"].astype(float)
    return bool(c.iloc[-1] < c.rolling(5).mean().iloc[-1])


_idx_cache = {}


def index_bar_at(date_str, ts):
    """深证成指当日 1min→5min，返回 ts 前最近一根 5min bar 的 (close, ma5)；不可得返回 None。"""
    key = date_str
    if key not in _idx_cache:
        try:
            from analysis.index_regime_intraday import fetch_index_minutes_backtest as _bt
            df1 = _bt(ts_code=INDEX_TS, date=date_str, freq="1min")
            if df1 is None or df1.empty:
                _idx_cache[key] = None
            else:
                df1 = df1.copy()
                df1["time"] = pd.to_datetime(df1["time"])
                df1 = df1.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
                df1["t5"] = df1["time"].dt.floor("5min")
                df5 = df1.groupby("t5").agg(
                    open=("open", "first"), high=("high", "max"), low=("low", "min"),
                    close=("close", "last"), volume=("volume", "sum")).reset_index()
                df5["ma5"] = df5["close"].rolling(5).mean()
                _idx_cache[key] = df5
        except Exception:
            _idx_cache[key] = None
    df5 = _idx_cache[key]
    if df5 is None or df5.empty:
        return None
    sub = df5[df5["t5"] <= pd.Timestamp(ts)]
    if sub.empty:
        return None
    bar = sub.iloc[-1]
    ma5 = bar["ma5"]
    if pd.isna(ma5):
        return None
    return float(bar["close"]), float(ma5)


def index_below_ma5_at(date_str, ts):
    """C-1: BUY 时刻指数5min价 < 指数5min MA5 → 指数短趋势向下，拦截 BUY。数据缺失返回 None。"""
    r = index_bar_at(date_str, ts)
    if r is None:
        return None
    close, ma5 = r
    return bool(close < ma5)


def all_days(start, end):
    d, e = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    out = []
    while d <= e:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def replay_code_day(engine, code, date_str, df, daily_df, apply_gate, stats):
    """回放单日。apply_gate=True 时模拟 v1.1.2（C-2/C-1 拦截则不开仓）。"""
    engine.reset_day(date_str)
    cycles, open_entry = [], {"px": None}

    def trace(ev, ts):
        ev = dict(ev)
        if ev.get("action") == "BUY_LOW":
            stats["n_buy_total"] += 1
            if apply_gate:
                c2 = below_ma5_on(daily_df, date_str)
                c1 = index_below_ma5_at(date_str, ts)
                blocked = []
                if c2:
                    blocked.append("C2")
                if c1:
                    blocked.append("C1")
                if blocked:
                    stats["n_buy_blocked"] += 1
                    stats["blocked_by"]["/".join(blocked)] += 1
                    stats["blocked_ts"].append(str(ts))
                    return  # 不开仓
            open_entry["px"] = float(ev.get("price", 0))
            open_entry["ts"] = ts
        elif ev.get("action") == "SELL_HIGH" and open_entry["px"] is not None:
            px = float(ev.get("price", 0))
            ep = float(ev.get("entry_price") or 0)
            if abs(ep - open_entry["px"]) < 1e-9:
                cycles.append({"date": date_str, "entry_ts": str(open_entry["ts"]),
                               "entry_px": round(open_entry["px"], 4), "exit_ts": str(ts),
                               "exit_px": round(px, 4), "reason": str(ev.get("exit_reason", "")),
                               "ret_pct": round((px / ep - 1) * 100, 4) if ep > 0 else None})
                open_entry["px"] = None

    times = df["time"].tolist()
    for i, t in enumerate(times):
        hhmm = t.hour * 100 + t.minute
        if hhmm < 930 or (1130 < hhmm < 1300) or hhmm > 1500:
            continue
        if i + 1 < 5:
            continue
        sub = df.iloc[:i + 1]
        price = float(df["close"].iloc[i])
        try:
            engine.evaluate(code, code, sub, price=price, t_val=hhmm, vwap=price,
                            today_ret=0.0, daily_status="ok", today_str=date_str,
                            params=None, trace=lambda ev: trace(ev, t))
        except Exception:
            continue
    return cycles


def summarize(cycles):
    closed = [c for c in cycles if c.get("ret_pct") is not None]
    n = len(closed)
    if n == 0:
        return {"n": 0, "win_rate": None, "sum_pct": 0.0, "avg_pct": None}
    wins = [c for c in closed if c["ret_pct"] > 0]
    return {"n": n, "win_rate": round(len(wins) / n, 4),
            "sum_pct": round(sum(c["ret_pct"] for c in closed), 3),
            "avg_pct": round(sum(c["ret_pct"] for c in closed) / n, 4)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-09-04")
    args = ap.parse_args()

    daily_df = load_daily_rows(CODE)
    print(f"[i] 日线缓存 {CODE}: {len(daily_df)} 行 "
          f"({daily_df['date'].min()}..{daily_df['date'].max()})" if not daily_df.empty else "[!] 日线缓存为空")

    from core.t_decision import TDecisionEngine
    engine = TDecisionEngine()
    days = all_days(args.start, args.end)
    out = {"code": CODE, "window": f"{args.start}..{args.end}", "arms": {}}

    for arm, apply_gate in (("V112", True), ("V113", False)):
        stats = {"n_buy_total": 0, "n_buy_blocked": 0,
                 "blocked_by": {"C2": 0, "C1": 0, "C2/C1": 0}, "blocked_ts": []}
        cycles = []
        for d in days:
            df = load_snapshots(CODE, d)
            if df.empty or len(df) < 60:
                continue
            cycles += replay_code_day(engine, CODE, d, df, daily_df, apply_gate, stats)
        s = summarize(cycles)
        out["arms"][arm] = {"summary": s, "gate": stats}
        w = s["win_rate"]
        print(f"\n===== {arm} 做T闭环 {s['n']} 笔 =====")
        print(f"  胜率 {f'{w:.1%}' if w is not None else '—'} | 合计 {s['sum_pct']:+.2f}% | "
              f"平均 {s['avg_pct']:+.3f}%")
        print(f"  总BUY {stats['n_buy_total']} | 被拦 {stats['n_buy_blocked']} "
              f"(C2:{stats['blocked_by']['C2']} C1:{stats['blocked_by']['C1']} "
              f"C2&C1:{stats['blocked_by']['C2/C1']})")

    # 被放行信号明细（V112 拦截、V113 放行的那批 BUY 的闭环表现）
    v112 = out["arms"]["V112"]
    v113 = out["arms"]["V113"]
    # 被放行 = V113 有而 V112 无的闭环数（数量差）+ 那些闭环的收益（= V113 收益 - V112 收益，因 V113⊇V112）
    n_released = v113["summary"]["n"] - v112["summary"]["n"]
    sum_released = round(v113["summary"]["sum_pct"] - v112["summary"]["sum_pct"], 3)
    avg_released = round(sum_released / n_released, 4) if n_released else None
    out["released_delta"] = {"n": n_released, "sum_pct": sum_released, "avg_pct": avg_released}
    print(f"\n===== 放闸增量（v1.1.3 比 v1.1.2 多放行的做T低吸）=====")
    print(f"  多放行闭环 {n_released} 笔 | 贡献合计 {sum_released:+.2f}% | "
          f"平均 {f'{avg_released:+.3f}%' if avg_released is not None else '—'}")

    json.dump(out, open(BASE / "t_io" / "gate_v112_v113_000988.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    print("\nwritten t_io/gate_v112_v113_000988.json")


if __name__ == "__main__":
    main()
