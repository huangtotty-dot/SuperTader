# -*- coding: utf-8 -*-
"""S2-T1 step3: trend30 门控状态构建（仅对持仓∩缓存的票，~129 只）。

对每只股票：30min 缓存 → collapse_stubs → mark_bar_meta → add_30min_indicators
→ linreg_quality → Trend30StateMachine.run（与 adapter.evaluate_bars 同管线，
但取**全历史逐 bar 状态**而非最新快照）。

门控口径：T 日 09:30 开腿只能使用 T-1 收盘前信息 ⇒
  gate_state[code, T] = state @ (该票 30min 序列中 < T 的最后 bar)（= T-1 末根 bar）。

输出 results/s2_ogr/trend30_gate.parquet: (code, date, state, confidence)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "results" / "s2_ogr"
CACHE = HERE.parents[2] / "cache" / "tushare_mins"
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis.trend30.indicators import (  # noqa: E402
    collapse_stubs, mark_bar_meta, add_30min_indicators, linreg_quality)
from analysis.trend30.state_machine import Trend30Config, Trend30StateMachine  # noqa: E402

SUFFIX = {"6": ".SH", "9": ".SH", "0": ".SZ", "3": ".SZ", "2": ".SZ",
          "4": ".BJ", "8": ".BJ"}


def ts_code(code6: str) -> str:
    return code6 + SUFFIX.get(code6[0], ".SZ")


def gate_for_code(code6: str) -> pd.DataFrame:
    fp = CACHE / f"{ts_code(code6)}_30min_d540.json"
    if not fp.exists():
        return pd.DataFrame()
    rows = json.load(open(fp, encoding="utf-8"))["rows"]
    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"])
    cfg = Trend30Config()
    try:
        d = collapse_stubs(df)
        d = mark_bar_meta(d)
        d = add_30min_indicators(d, st_n=cfg.st_n, st_mult=cfg.st_mult)
        d = d.join(linreg_quality(d["close"], n=cfg.reg_n))
        if len(d) < cfg.min_bars:
            return pd.DataFrame()
        sm = Trend30StateMachine(cfg)
        out = sm.run(d)
    except Exception:
        return pd.DataFrame()
    # 每日末根 bar 的状态
    dd = out.copy()
    dd["d"] = dd["time"].dt.strftime("%Y-%m-%d")
    last = dd.groupby("d").tail(1)[["d", "state", "confidence"]]
    days = sorted(dd["d"].unique())
    pos = {d_: i for i, d_ in enumerate(days)}
    recs = []
    st_of = dict(zip(last["d"], last["state"]))
    cf_of = dict(zip(last["d"], last["confidence"]))
    for d_ in days[1:]:                      # T 日 gate = T-1 末根状态
        prev = days[pos[d_] - 1]
        recs.append((code6, d_, st_of[prev], cf_of[prev]))
    return pd.DataFrame(recs, columns=["code", "date", "state", "confidence"])


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=10**9)
    args = ap.parse_args()
    t0 = time.time()
    codes = json.load(open(OUT / "held_cached_codes.json"))[args.start:args.end]
    frames, fails = [], []
    for i, c in enumerate(codes):
        f = gate_for_code(c)
        if f.empty:
            fails.append(c)
        else:
            frames.append(f)
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(codes)} ({time.time()-t0:.0f}s)", flush=True)
    g = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    tag = f"trend30_gate_{args.start}_{args.start+len(codes)}"
    g.to_parquet(OUT / f"{tag}.parquet", index=False)
    bull = (g["state"] == "BULL").mean() if len(g) else float("nan")
    print(f"[gate:{tag}] {len(frames)} codes {len(g)} rows bull_ratio={bull:.1%} "
          f"fails={len(fails)} ({time.time()-t0:.0f}s)", flush=True)
    if fails:
        (OUT / f"{tag}_fails.json").write_text(json.dumps(fails))


if __name__ == "__main__":
    main()
