# -*- coding: utf-8 -*-
"""fix5 回测审计分析：窗口/费用/通道行为/盈亏归因"""
import glob, zipfile, os
import pandas as pd

base = r"E:\06_T\audit\gm_fusion\v1.0.1_fix5\report"
z = [f for f in os.listdir(base) if f.endswith(".zip")][0]
zf = zipfile.ZipFile(os.path.join(base, z))
zf.extractall(os.path.join(base, "data_zip"))
print("zip内容:", zf.namelist())

dz = os.path.join(base, "data_zip")
tr = pd.read_csv(glob.glob(dz + r"\*交易*")[0])
nav = pd.read_csv(glob.glob(dz + r"\*净值*")[0])

tr["_t"] = pd.to_datetime(tr["trade_time"], errors="coerce")
fills = tr[tr["btype"].astype(str).str.contains("买|卖", na=False)].copy()
nav["_d"] = pd.to_datetime(nav["date"], errors="coerce")

print("\n=== 窗口 ===")
print("净值日期:", nav["_d"].min(), "~", nav["_d"].max(), " 共", len(nav), "天")
print("首笔/末笔成交:", fills["_t"].min(), "~", fills["_t"].max())

print("\n=== 费用 ===")
print("fee>0 笔数: %d/%d  合计 %.2f" % ((fills["fee"]>0).sum(), len(fills), fills["fee"].sum()))

print("\n=== 成交概览 ===")
buys = fills[fills["volume"] > 0]
sells = fills[fills["volume"] < 0]
print("买入 %d 笔 / 卖出 %d 笔  交易天数 %d" % (len(buys), len(sells), fills["_t"].dt.date.nunique()))
print("标的分布:", fills["symbol"].value_counts().to_dict())

s = nav["nav"].astype(float)
print("\n=== 净值 ===")
print("期初/期末: %.5f → %.5f  收益 %.2f%%  MDD %.2f%%" % (s.iloc[0], s.iloc[-1], (s.iloc[-1]/s.iloc[0]-1)*100, (s/s.cummax()-1).min()*100))

# 分标的归因
g = fills.groupby("symbol").agg(amount=("amount","sum"), fee=("fee","sum"))
pos = pd.read_csv(glob.glob(dz + r"\*持仓*")[0])
last = pos[pos["date"] == pos["date"].max()]
print("\n=== 期末持仓 ===")
print(last.to_string(index=False))
print("\n=== 分标的归因（现金损益+期末市值）===")
mv = dict(zip(last["symbol"], last["market_value"]))
for sym in g.index:
    pnl = g.loc[sym, "amount"] - g.loc[sym, "fee"] + mv.get(sym, 0)
    print("  %s: %+.0f" % (sym, pnl))

# 关键日期行为：07-07 之后有无成交（主跌段是否在窗口内）
print("\n=== 7月成交 ===")
jul = fills[fills["_t"] >= "2026-07-01"]
print(jul[["trade_time","btype","symbol","vwap","volume"]].to_string(index=False))
