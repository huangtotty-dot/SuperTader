# -*- coding: utf-8 -*-
"""CPU 基准：热点函数耗时（只读；每日复跑，抓回归/验优化）。

为什么要有它：优化 CPU 必须先能**重复测同一个数**。此前结论都靠一次性探针，
无法回答"我这次改动到底快了没有"。本脚本对已定位的热点用**真实缓存数据**跑固定
样本，输出 ms/次，并把结果追加到 JSONL 供跨日对比。

已定位热点（2026-10-11 首次测量，4 物理核机器）：
  · trend30.evaluate_bars   ~116 ms/只 ← 最大头
      └ collapse_stubs 中 `iterrows()` 占 37%
      └ linreg_quality 中逐 bar Python 循环调 OLS 占 21%
  · analysis.indicators.add_indicators  ~38 ms/只
  · 全市场 VWAP 构造（`iterrows()`）      ~684 ms/次
  · 分钟缓存 JSON→DataFrame              ~24.6 ms/只

用法：
    python scripts/experiment/cpu_bench_hotpaths.py              # 跑一轮并对比上次
    python scripts/experiment/cpu_bench_hotpaths.py --n 40       # 加大样本
    python scripts/experiment/cpu_bench_hotpaths.py --report     # 只看跨日趋势
    python scripts/experiment/cpu_bench_hotpaths.py --baseline   # 写入基线快照

只读保证：不改任何业务文件；只追加自己的 JSONL（t_io/logs/cpu_bench.jsonl）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")
_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(_BASE)
sys.path.insert(0, _BASE)

DEFAULT_JSONL = os.path.join(_BASE, "t_io", "logs", "cpu_bench.jsonl")
BARS_30M = 280          # 对齐 get_trend30(days=35) ≈ 280 根
BARS_5M = 480           # 一日常规分钟根数


def _load_rows(files, n):
    import pandas as pd
    out = []
    for f in files:
        try:
            rows = json.load(open(f, encoding="utf-8"))["rows"][-n:]
            out.append(pd.DataFrame(rows))
        except Exception:
            continue
    return out


def bench_helpers(n):
    """被基准覆盖的小工具（本文件内，避免污染业务代码）。"""
    import numpy as np
    import pandas as pd

    def json_to_df(files):
        for f in files:
            rows = json.load(open(f, encoding="utf-8"))["rows"]
            df = pd.DataFrame(rows)
            df["time"] = pd.to_datetime(df["time"])
            df.sort_values("time").reset_index(drop=True)

    def vwap_iterrows(spot):
        cache = {}
        for _, row in spot.iterrows():
            cache[str(row["代码"]).strip()] = round(
                float(row["成交额"]) / (float(row["成交量"]) * 100), 2)
        return cache

    def vwap_vectorized(spot):
        v = spot["成交量"].to_numpy(dtype=float)
        a = spot["成交额"].to_numpy(dtype=float)
        return dict(zip(spot["代码"].astype(str).str.strip(),
                        np.round(a / (v * 100), 2)))
    return json_to_df, vwap_iterrows, vwap_vectorized


def run(n: int, quiet=False) -> dict:
    import pandas as pd
    from analysis.indicators import add_indicators
    from analysis.trend30.adapter import evaluate_bars
    from analysis.trend30.indicators import collapse_stubs, linreg_quality

    res: dict[str, dict] = {}

    def timed(name, fn, units):
        t0 = time.perf_counter()
        fn()
        dt = time.perf_counter() - t0
        res[name] = {"total_s": round(dt, 4), "per_op_ms": round(dt / units * 1000, 2),
                     "units": units}
        return dt

    # ── 噪声标尺（canary）───────────────────────────────────────────────
    # 纯 Python 定长工作负载，**结果恒定**，只受机器负载影响。
    # 为什么必须有：2026-10-11 实测，机器上跑了别的进程时，未改动的函数也会
    # 「变慢」+70~140%（json_to_df +140.6%）⇒ 拿绝对毫秒做跨日比较会得出假结论。
    # 判读法：canary 相对其历史中位数偏移多少，其余项就要按同幅度折价看待。
    def _canary():
        s = 0
        for i in range(300000):
            s += (i * i) % 7
        return s

    timed("__canary__(噪声标尺)", _canary, 1)

    f30 = sorted(glob.glob("t_io/cache/tushare_mins/*_30min_*.json"))[:n]
    f5 = sorted(glob.glob("t_io/cache/tushare_mins/*_5min_*.json"))[:n]
    if not f30:
        print("⛔ 找不到 30min 缓存，无法基准")
        return res

    df30 = _load_rows(f30, BARS_30M)
    json_to_df, vwap_iterrows, vwap_vectorized = bench_helpers(n)

    timed("trend30.evaluate_bars", lambda: [evaluate_bars(d) for d in df30], len(df30))
    timed("trend30.collapse_stubs", lambda: [collapse_stubs(d) for d in df30], len(df30))
    timed("trend30.linreg_quality",
          lambda: [linreg_quality(d["close"].astype(float), 20) for d in df30], len(df30))

    if f5:
        df5 = _load_rows(f5, BARS_5M)
        timed("indicators.add_indicators",
              lambda: [add_indicators(d.copy()) for d in df5], len(df5))

    timed("cache.json_to_df(全历史)", lambda: json_to_df(f30), len(f30))

    # 全市场 VWAP 构造（5000 只）——对比 iterrows vs 向量化
    import numpy as np
    spot = pd.DataFrame({"代码": [f"{600000+i:06d}" for i in range(5000)],
                         "成交额": np.random.rand(5000) * 1e9,
                         "成交量": np.random.rand(5000) * 1e6 + 1})
    timed("vwap.iterrows(5000只)", lambda: vwap_iterrows(spot), 1)
    timed("vwap.vectorized(5000只)", lambda: vwap_vectorized(spot), 1)

    # 把 eval/collapse/linreg 的占比算出来，便于看优化是否打对了地方
    ev = res["trend30.evaluate_bars"]["per_op_ms"] or 1
    for k in ("trend30.collapse_stubs", "trend30.linreg_quality"):
        res[k]["share_of_eval_pct"] = round(res[k]["per_op_ms"] / ev * 100, 1)

    if not quiet:
        print(f"\n{'基准项':<30}{'ms/次':>10}{'占 eval':>10}{'样本':>7}")
        print("-" * 60)
        for k, v in res.items():
            share = f"{v.get('share_of_eval_pct', ''):>9}%" if "share_of_eval_pct" in v else ""
            print(f"{k:<30}{v['per_op_ms']:>10.2f}{share:>10}{v['units']:>7}")
    return res


def append_jsonl(path, res, n):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rec = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "n": n, "results": res}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


CANARY = "__canary__(噪声标尺)"


def cmd_report(path):
    if not os.path.exists(path):
        print(f"⛔ 无历史：{path}")
        return
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                recs.append(json.loads(line))
            except Exception:
                pass
    if not recs:
        print("⛔ JSONL 无可用记录")
        return
    keys = list(recs[-1]["results"].keys())

    # 噪声标尺：取历史**最小** canary 作为"机器最空闲"参照（负载只会让它变大）
    canaries = [r["results"].get(CANARY, {}).get("per_op_ms") for r in recs]
    canaries = [c for c in canaries if c]
    base_canary = min(canaries) if canaries else None

    print(f"共 {len(recs)} 轮基准")
    if base_canary:
        print(f"噪声标尺 canary 基线（历史最小）: {base_canary:.1f} ms —— "
              f"越大说明机器越忙，跨日比较必须看「归一化」列")
    print()
    cols = [k for k in keys if k != CANARY]
    print(f"{'时间':<20}{'噪声×':>7}" + "".join(f"{k.split('.')[-1][:10]:>12}" for k in cols))
    for r in recs:
        res = r["results"]
        c = res.get(CANARY, {}).get("per_op_ms")
        noise = (c / base_canary) if (c and base_canary) else 1.0
        line = f"{r['ts']:<20}{noise:>7.2f}"
        for k in cols:
            v = res.get(k, {}).get("per_op_ms")
            line += f"{v:>12.2f}" if v is not None else f"{'-':>12}"
        print(line)
    print("\n=== 归一化（各项 ÷ 噪声×）——跨日比较看这张 ===")
    print(f"{'时间':<20}{'噪声×':>7}" + "".join(f"{k.split('.')[-1][:10]:>12}" for k in cols))
    for r in recs:
        res = r["results"]
        c = res.get(CANARY, {}).get("per_op_ms")
        noise = (c / base_canary) if (c and base_canary) else 1.0
        line = f"{r['ts']:<20}{noise:>7.2f}"
        for k in cols:
            v = res.get(k, {}).get("per_op_ms")
            line += f"{v/noise:>12.2f}" if v is not None else f"{'-':>12}"
        print(line)

    if len(recs) >= 2:
        print("\n=== 首轮 → 末轮（归一化后）===")
        def norm(r):
            res = r["results"]
            c = res.get(CANARY, {}).get("per_op_ms")
            f = (c / base_canary) if (c and base_canary) else 1.0
            return {k: (res.get(k, {}).get("per_op_ms") or 0) / f for k in cols}
        a, b = norm(recs[0]), norm(recs[-1])
        for k in cols:
            x, y = a[k], b[k]
            if x and y:
                flag = "" if abs((y - x) / x) > 0.07 else "  (在噪声带内)"
                print(f"  {k:<32}{x:>9.2f} → {y:>9.2f} ms  ({(y-x)/x*100:+.1f}%){flag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20, help="每项样本只数")
    ap.add_argument("--jsonl", default=DEFAULT_JSONL)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--baseline", action="store_true", help="标记为基线快照")
    args = ap.parse_args()

    if args.report:
        cmd_report(args.jsonl)
        return 0

    t0 = time.perf_counter()
    res = run(args.n)
    if not res:
        return 1
    append_jsonl(args.jsonl, res, args.n)
    tag = "（基线）" if args.baseline else ""
    print(f"\n总墙钟 {time.perf_counter()-t0:.1f}s{tag} → 已追加 {args.jsonl}")
    print("跨日趋势：python scripts/experiment/cpu_bench_hotpaths.py --report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
