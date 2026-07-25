# -*- coding: utf-8 -*-
"""fix4 回测 KPI 审计分析：费用闭环/多标的/窗口"""
import glob, zipfile, os
import pandas as pd

base = r"E:\06_T\audit\gm_fusion\v1.0.1_fix4\report"
z = [f for f in os.listdir(base) if f.endswith(".zip")][0]
zf = zipfile.ZipFile(os.path.join(base, z))
print(zf.namelist())
zf.extractall(os.path.join(base, "data_zip"))

dz = os.path.join(base, "data_zip")
for f in sorted(glob.glob(dz + r"\*.csv")):
    df = pd.read_csv(f)
    print("===", os.path.basename(f), df.shape)

tr = pd.read_csv(glob.glob(dz + r"\*交易*")[0])
nav = pd.read_csv(glob.glob(dz + r"\*净值*")[0])

tr["_t"] = pd.to_datetime(tr["trade_time"], errors="coerce")
fills = tr[tr["btype"].astype(str).str.contains("买|卖", na=False)]
print("\n窗口:", tr["_t"].min(), "~", tr["_t"].max())
print("成交笔数:", len(fills), " 交易天数:", fills["_t"].dt.date.nunique())
print("标的分布:", fills["symbol"].value_counts().to_dict())
print("fee 合计: %.2f  fee>0 笔数: %d/%d" % (fills["fee"].sum(), (fills["fee"]>0).sum(), len(fills)))
# 估算佣金率
amt = fills["amount"].abs()
if (fills["fee"]>0).any():
    ratio = (fills["fee"]/amt).replace([float("inf")], pd.NA).dropna()
    print("佣金率估算: min=%.5f median=%.5f max=%.5f" % (ratio.min(), ratio.median(), ratio.max()))

s = nav["nav"].astype(float)
print("\n期初/期末净值: %.5f → %.5f  收益: %.2f%%" % (s.iloc[0], s.iloc[-1], (s.iloc[-1]/s.iloc[0]-1)*100))
print("最大回撤: %.2f%%" % ((s/s.cummax()-1).min()*100))
print("\n--- 全部成交 ---")
print(fills[["trade_time","btype","symbol","order_price","volume","vwap","amount","fee"]].to_string(max_rows=80))
