# -*- coding: utf-8 -*-
"""gateA_1400_final.py — 闸门A「14:00 后禁新开买入」决赛对照（Q-20260903-2）

口径：引擎级每股闭环（与做T信号推送/共振/破线闸/股数正交）。
直接驱动 core/t_decision.TDecisionEngine（当前含 F-4 last_down 修复），在本地分钟库上按日重放，
逐笔 BUY→SELL 记录每股闭环（含止盈/尾盘强平退出原因）。
双臂：
  PROD  现有生产（14:00 后仍可新开）
  CAND  14:00(含)起禁新开买入（当天已开仓仍正常止盈/强平）

指标（等权每股，不复利）：
  闭环数 / 胜率 / 合计收益(%) / 平均每笔 / 上午(≤11:30)胜率 / 尾盘强平占比
产出: t_io/gateA_1400_report.json（打印汇总）

用法: python scripts/gateA_1400_final.py [--codes 000988,002639,588170,600176,600481,603667]
      [--start 2026-06-01 --end 2026-09-04]
"""
import argparse, json, os, sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd

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


def all_days(start, end):
    d, e = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
    out = []
    while d <= e:
        out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def replay_code_day(engine, code, name, date_str, df, no_new_after_1400):
    """按日重放引擎，返回该股当日闭环 [{code,date,entry_ts,entry_px,exit_ts,exit_px,reason,ret_pct}]"""
    engine.reset_day(date_str)
    cycles, open_entry = [], {"px": None}

    def trace(ev, ts):
        ev = dict(ev)
        hhmm = ts.hour * 100 + ts.minute
        if ev.get("action") == "BUY_LOW":
            if no_new_after_1400 and hhmm >= 1400:      # 候选：14:00(含)后禁新开
                return
            open_entry["px"] = float(ev.get("price", 0))
            open_entry["ts"] = ts
        elif ev.get("action") == "SELL_HIGH" and open_entry["px"] is not None:
            px = float(ev.get("price", 0))
            ep = float(ev.get("entry_price") or 0)
            if abs(ep - open_entry["px"]) < 1e-9:        # 与该BUY配对的退出
                cycles.append({"code": code, "date": date_str,
                               "entry_ts": str(open_entry["ts"]), "entry_px": round(open_entry["px"], 4),
                               "exit_ts": str(ts), "exit_px": round(px, 4),
                               "reason": str(ev.get("exit_reason", "")),
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
            engine.evaluate(code, name, sub, price=price, t_val=hhmm, vwap=price,
                            today_ret=0.0, daily_status="ok", today_str=date_str,
                            params=None,
                            trace=lambda ev: trace(ev, t))
        except Exception:
            continue
    # 引擎尾盘强平保证 t>=1455 当日闭环；此处防御性记录残余
    return cycles


def summarize(cycles):
    closed = [c for c in cycles if c.get("ret_pct") is not None]
    n = len(closed)
    if n == 0:
        return {"n_entries": 0, "win_rate": None, "sum_pct": 0.0, "avg_pct": None,
                "am_n": 0, "am_wr": None, "force_ratio": None}
    wins = [c for c in closed if c["ret_pct"] > 0]
    am = [c for c in closed if int(str(c["entry_ts"])[11:13]) * 100 + int(str(c["entry_ts"])[14:16]) < 1130]
    tail = [c for c in closed if int(str(c["entry_ts"])[11:13]) * 100 + int(str(c["entry_ts"])[14:16]) >= 1400]
    force = [c for c in closed if "强平" in c["reason"]]
    def _w(rows):
        return round(len([x for x in rows if x["ret_pct"] > 0]) / len(rows), 4) if rows else None
    return {"n_entries": n, "win_rate": round(len(wins) / n, 4),
            "sum_pct": round(sum(x["ret_pct"] for x in closed), 3),
            "avg_pct": round(sum(x["ret_pct"] for x in closed) / n, 4),
            "am_n": len(am),
            "am_wr": round(len([c for c in am if c["ret_pct"] > 0]) / len(am), 4) if am else None,
            "force_ratio": round(len(force) / n, 4) if n else None,
            "n_after1400": len(tail),
            "tail_wr": _w(tail),
            "tail_sum_pct": round(sum(x["ret_pct"] for x in tail), 3) if tail else 0.0,
            "tail_avg_pct": round(sum(x["ret_pct"] for x in tail) / len(tail), 4) if tail else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default="000988,002639,588170,600176,600481,603667")
    ap.add_argument("--start", default="2026-06-01")
    ap.add_argument("--end", default="2026-09-04")
    args = ap.parse_args()
    codes = [c.strip() for c in args.codes.split(",")]
    from core.t_decision import TDecisionEngine
    engine = TDecisionEngine()
    days = all_days(args.start, args.end)
    out = {"window": f"{args.start}..{args.end}", "codes": codes, "arms": {}}
    for arm, no1400 in (("PROD", False), ("CAND", True)):
        cycles = []
        for code in codes:
            for d in days:
                df = load_snapshots(code, d)
                if df.empty or len(df) < 60:
                    continue
                cycles += replay_code_day(engine, code, code, d, df, no1400)
        per = {c: summarize([x for x in cycles if x["code"] == c]) for c in codes}
        agg_all = summarize(cycles)
        agg_all["n_codes_active"] = sum(1 for c in codes if per[c]["n_entries"] > 0)
        out["arms"][arm] = {"agg": agg_all, "per_code": per}
        def pct(v):
            return f"{v:.1%}" if v is not None else "—"
        print(f"\n===== 双臂汇总: {arm} (no_new_after_1400={no1400}) 闭环 {agg_all['n_entries']} 笔 =====")
        print(f"  胜率 {pct(agg_all['win_rate'])} | 合计 {agg_all['sum_pct']:+.2f}% | 平均 {agg_all['avg_pct']:+.3f}% | "
              f"上午 n={agg_all['am_n']} 胜率 {pct(agg_all['am_wr'])} | 强平占比 {pct(agg_all['force_ratio'])} | "
              f"14:00后新开 {agg_all['n_after1400']} 笔(胜率 {pct(agg_all['tail_wr'])} 合计 {agg_all['tail_sum_pct']:+.2f}%)")
    json.dump(out, open(BASE / "t_io" / "gateA_1400_report.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    print("\nwritten t_io/gateA_1400_report.json")


if __name__ == "__main__":
    main()
