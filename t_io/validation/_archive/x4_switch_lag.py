# -*- coding: utf-8 -*-
"""
x4_switch_lag.py — X4: 趋势切换滞后自动测量 + 反转日切换正确性(统一口径样本)

拐点标注规则(文档化):
  在 5 分钟收盘价序列上运行 zigzag, 阈值 REV=0.8%:
  - 上行段中, 收盘价自运行最高点回落 >=0.8% -> 在最高点所在 bar 标注"顶"(期望转向 BEAR)
  - 下行段中, 收盘价自运行最低点反弹 >=0.8% -> 在最低点所在 bar 标注"底"(期望转向 BULL)
  初始方向由首个满足 0.8% 幅度的位移确定; 每日独立标注(日内指标, 不跨日)。

滞后定义:
  lag = trend_timeline 中"拐点确认时刻之后首个与期望方向一致的状态条目的时刻" - 拐点时刻, 以 5 分钟根数计。
  确认时刻 = zigzag 确认反转的 bar 时刻(即回落/反弹达 0.8% 的那根)。
  收盘前未切换记 missed。

反转日定义: 当日存在 >=1 个已确认拐点, 且拐点前期望方向与拐点前活跃 trend_state 相反(即真正构成"反转")。
切换正确性: lag <= 3 根记 correct, >3 记 late, 未切换记 missed。

输入: v108_unified/v102/trend_timeline_v102.jsonl + t_io/minute_snapshots_ts 分钟数据
输出: v108_unified/x4_switch_lag.json + 控制台汇总
"""
import json, sys
from pathlib import Path
from statistics import mean
import pandas as pd

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/v108_unified"
TS_DIR = BASE / "t_io/minute_snapshots_ts"
REV = 0.008
CODES = ["000988", "588170", "600176", "600481", "603667"]


def load_bars(code, d):
    fp = TS_DIR / d[:4] / d[5:7] / f"{code}_{d}.json"
    if not fp.exists():
        return None
    bars = json.load(open(fp, encoding="utf-8"))["bars"]
    df = pd.DataFrame(bars)
    df["time"] = pd.to_datetime(df["time"])
    return df


def closes_5min(df):
    df = df.set_index("time")
    c = df["close"].resample("5min").last().dropna()
    return c  # index=bar 收盘时刻(近似), values=close


def zigzag(c):
    """返回 [(extreme_time, confirm_time, 'top'|'bottom')]"""
    if len(c) < 3:
        return []
    times = list(c.index)
    vals = list(c.values)
    pivots = []
    # 初始: 找首个 >=REV 的位移定方向
    hi_i = lo_i = 0
    direction = None  # 'up' 追踪顶, 'down' 追踪底
    for i in range(1, len(vals)):
        if direction is None:
            if vals[i] >= vals[lo_i] * (1 + REV):
                direction = "up"; hi_i = i
            elif vals[i] <= vals[hi_i] * (1 - REV):
                direction = "down"; lo_i = i
            else:
                if vals[i] > vals[hi_i]:
                    hi_i = i
                if vals[i] < vals[lo_i]:
                    lo_i = i
        elif direction == "up":
            if vals[i] > vals[hi_i]:
                hi_i = i
            elif vals[i] <= vals[hi_i] * (1 - REV):
                pivots.append((times[hi_i], times[i], "top"))
                direction = "down"; lo_i = i
        else:
            if vals[i] < vals[lo_i]:
                lo_i = i
            elif vals[i] >= vals[lo_i] * (1 + REV):
                pivots.append((times[lo_i], times[i], "bottom"))
                direction = "up"; hi_i = i
    return pivots


def state_at(timeline, hhmm):
    """timeline=[[HH:MM,state,conf],...] 时刻 hhmm 的活跃状态"""
    cur = None
    for t, s, _ in timeline:
        if t <= hhmm:
            cur = s
        else:
            break
    return cur


def first_switch(timeline, after_hhmm, expect):
    """确认时刻之后首个状态==expect 的条目时刻"""
    for t, s, _ in timeline:
        if t > after_hhmm and s == expect:
            return t
    return None


def to_min(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def main():
    timelines = {}
    for l in open(ROOT / "v102/trend_timeline_v102.jsonl", encoding="utf-8"):
        r = json.loads(l)
        d, code = r["key"].split(":")
        timelines[(d, code)] = r["timeline"]

    per_pivot = []
    day_meta = {}  # (d,code) -> {"reversal": bool, "results": [...]}
    for (d, code), tl in sorted(timelines.items()):
        df = load_bars(code, d)
        if df is None:
            continue
        pivots = zigzag(closes_5min(df))
        results = []
        for ext_t, conf_t, kind in pivots:
            expect = "BEAR" if kind == "top" else "BULL"
            ext_hhmm = ext_t.strftime("%H:%M")
            conf_hhmm = conf_t.strftime("%H:%M")
            prev_state = state_at(tl, ext_hhmm)
            is_reversal = prev_state is not None and prev_state in ("BULL", "BEAR") and prev_state != expect
            sw = first_switch(tl, conf_hhmm, expect)
            lag = None if sw is None else (to_min(sw) - to_min(ext_hhmm)) / 5.0
            verdict = "missed" if sw is None else ("correct" if lag <= 3 else "late")
            results.append({"extreme": ext_hhmm, "confirm": conf_hhmm, "kind": kind,
                            "expect": expect, "prev_state": prev_state, "is_reversal": is_reversal,
                            "switch": sw, "lag_bars": lag, "verdict": verdict})
            per_pivot.append({"date": d, "code": code, **results[-1]})
        day_meta[(d, code)] = {"n_pivots": len(pivots),
                               "reversal": any(r["is_reversal"] for r in results),
                               "results": results}

    switched = [p["lag_bars"] for p in per_pivot if p["lag_bars"] is not None]
    rev_days = {k: v for k, v in day_meta.items() if v["reversal"]}
    rev_pivots = [p for p in per_pivot if p["is_reversal"]]
    summary = {
        "rule": f"5min收盘 zigzag 阈值 {REV:.1%}; lag=(首个同向trend切换时刻-拐点时刻)/5min; <=3根correct",
        "total_pivots": len(per_pivot),
        "switched": len(switched),
        "missed": len(per_pivot) - len(switched),
        "lag_mean_bars": round(mean(switched), 2) if switched else None,
        "lag_p90_bars": round(sorted(switched)[int(len(switched) * 0.9) - 1], 1) if switched else None,
        "lag_max_bars": max(switched) if switched else None,
        "reversal_days": len(rev_days),
        "reversal_pivots": len(rev_pivots),
        "reversal_correct": sum(1 for p in rev_pivots if p["verdict"] == "correct"),
        "reversal_late": sum(1 for p in rev_pivots if p["verdict"] == "late"),
        "reversal_missed": sum(1 for p in rev_pivots if p["verdict"] == "missed"),
    }
    out = {"summary": summary, "per_pivot": per_pivot,
           "reversal_day_keys": sorted([f"{d}:{c}" for (d, c) in rev_days])}
    json.dump(out, open(ROOT / "x4_switch_lag.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
