# -*- coding: utf-8 -*-
"""只读诊断 2026-11-28 S1 基准段对拍差异，不写生产产物。"""
from pathlib import Path
import json
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
XSEC = ROOT / "t_io" / "validation" / "xsection"
FM = ROOT / "t_io" / "validation" / "factor_mining"
RESULTS = FM / "s0_account" / "results"
rep = json.loads((XSEC / "panel" / "_incr" / "update_report.json").read_text(encoding="utf-8"))
cutoff = pd.Timestamp(rep["verify"]["cutoff"])
drift = {str(s).split(".")[-1] for s in rep["fetch"]["overlap_check"]["drift_symbols"]}
old = pd.read_parquet(RESULTS / f"baseline_{cutoff.date()}" / "scores.parquet")
new = pd.read_parquet(RESULTS / "scores.parquet")
for d in (old, new):
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
old = old[old["date"] <= cutoff]
new = new[new["date"] <= cutoff]
m = old.merge(new, on=["symbol", "date"], how="outer", suffixes=("_old", "_new"), indicator=True)
print("ROWS", {"old": len(old), "new": len(new), "both": int((m["_merge"] == "both").sum()),
               "old_only": int((m["_merge"] == "left_only").sum()),
               "new_only": int((m["_merge"] == "right_only").sum())})
for side in ("left_only", "right_only"):
    u = m[m["_merge"] == side]
    by_sym = u.groupby("symbol").size().sort_values(ascending=False)
    print("UNMATCHED", side, "drift_rows", int(u["symbol"].isin(drift).sum()),
          "dates", {str(k.date()): int(v) for k, v in u.groupby("date").size().sort_values(ascending=False).head(8).items()})
    print("UNMATCHED_SYMBOLS", side, [{"symbol": s, "rows": int(n), "drift": s in drift}
                                      for s, n in by_sym.head(20).items()])
both = m[m["_merge"] == "both"].copy()
for c in ("score_eq", "score_icir", "rev10_z"):
    both[c + "_absdiff"] = (both[c + "_new"] - both[c + "_old"]).abs()
    a = both[c + "_absdiff"]
    print("DIFF", c, {"median": float(a.median()), "p95": float(a.quantile(.95)),
                       "p99": float(a.quantile(.99)), "max": float(a.max())})
    for label, sub in (("drift", both[both["symbol"].isin(drift)]),
                       ("non_drift", both[~both["symbol"].isin(drift)])):
        x = sub[c + "_absdiff"]
        print("DIFF_GROUP", c, label, {"rows": len(sub), "median": float(x.median()),
                                        "p99": float(x.quantile(.99)), "max": float(x.max())})
    top = both.groupby("symbol")[c + "_absdiff"].agg(["max", "mean", "count"]).sort_values("max", ascending=False).head(15)
    print("TOP_SYMBOLS", c, [{"symbol": s, "drift": s in drift, "max": float(r["max"]),
                               "mean": float(r["mean"]), "count": int(r["count"])}
                              for s, r in top.iterrows()])
by_date = both.groupby("date")["score_eq_absdiff"].agg(["max", "mean"]).sort_values("max", ascending=False)
print("TOP_DATES", [{"date": str(d)[:10], "max": float(r["max"]), "mean": float(r["mean"])}
                     for d, r in by_date.head(15).iterrows()])
nav_name = "nav_s1_eq_N4_M8_H1_A.csv"
on = pd.read_csv(RESULTS / f"baseline_{cutoff.date()}" / nav_name, index_col=0, parse_dates=True).iloc[:, 0]
nn = pd.read_csv(RESULTS / "s1_nav" / nav_name, index_col=0, parse_dates=True).iloc[:, 0]
on = on[on.index <= cutoff]
nn = nn[nn.index <= cutoff]
common = on.index.intersection(nn.index)
rel = (nn[common] - on[common]).abs() / on[common].abs()
print("NAV_TOP", [{"date": str(d)[:10], "rel": float(rel[d]), "old": float(on[d]), "new": float(nn[d])}
                   for d in rel.sort_values(ascending=False).head(12).index])
print("NAV_FIRST", [{"date": str(d)[:10], "rel": float(rel[d]), "old": float(on[d]), "new": float(nn[d])}
                     for d in common[:12]])
uni_old = pd.read_parquet(XSEC / "panel_aux" / f"universe_backup_{cutoff.date()}.parquet")
uni_new = pd.read_parquet(XSEC / "panel" / "universe.parquet")
print("UNIVERSE_COLUMNS", list(uni_new.columns))
print("UNIVERSE_COUNTS", {"old": len(uni_old), "new": len(uni_new)})
