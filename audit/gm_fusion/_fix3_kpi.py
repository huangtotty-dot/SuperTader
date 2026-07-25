# -*- coding: utf-8 -*-
"""fix3 回测 KPI 审计分析：费用口径/成交行为/7月PANIC触发/拒单"""
import glob
import pandas as pd

base = r"E:\06_T\audit\gm_fusion\v1.0.1_fix3\report\data_zip"

for f in sorted(glob.glob(base + r"\*.csv")):
    df = pd.read_csv(f)
    print("===", f.split("\\")[-1], df.shape)
    print(list(df.columns))

tr = pd.read_csv(glob.glob(base + r"\*交易*")[0])
print("\n--- 成交明细 ---")
print(tr.to_string(max_rows=60))
if "fee" in tr.columns:
    print("\nfee 合计:", tr["fee"].sum(), " fee>0 笔数:", int((tr["fee"] > 0).sum()), "/", len(tr))

nav = pd.read_csv(glob.glob(base + r"\*净值*")[0])
ncol = [c for c in nav.columns if "nav" in c.lower() or "净值" in c]
print("\n--- 净值 ---")
print(nav.head(2).to_string())
print(nav.tail(2).to_string())
if ncol:
    s = nav[ncol[0]].astype(float)
    print("期末净值:", s.iloc[-1], " 收益: %.2f%%" % ((s.iloc[-1]/s.iloc[0]-1)*100))
    print("最大回撤: %.2f%%" % ((s/s.cummax()-1).min()*100))

# 7 月成交
tcol = [c for c in tr.columns if "time" in c.lower() or "date" in c.lower() or "时间" in c]
if tcol:
    tr["_t"] = pd.to_datetime(tr[tcol[0]], errors="coerce")
    jul = tr[tr["_t"] >= "2026-07-01"]
    print("\n7月成交笔数:", len(jul))
    if len(jul):
        print(jul.to_string(max_rows=30))
    print("\n交易天数:", tr["_t"].dt.date.nunique())
