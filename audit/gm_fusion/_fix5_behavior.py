# -*- coding: utf-8 -*-
"""fix5 深度行为分析：亏损高抛检测 / 阴跌票买卖 / 通道行为"""
import glob
import pandas as pd

dz = r"E:\06_T\audit\gm_fusion\v1.0.1_fix5\report\data_zip"
tr = pd.read_csv(glob.glob(dz + r"\*交易*")[0])
fills = tr[tr["btype"].astype(str).str.contains("买|卖", na=False)].copy()
fills["_t"] = pd.to_datetime(fills["trade_time"])

MIRROR_COST = {"SZSE.000988": 117.32, "SHSE.600481": 6.32, "SHSE.600176": 34.35, "SHSE.603667": 65.88}
NAMES = {"SZSE.000988": "000988华工", "SHSE.600481": "600481双良", "SHSE.600176": "600176巨石", "SHSE.603667": "603667五洲"}

print("=== 亏损高抛检测（卖出 vwap < 镜像成本，fix4基线: 600481 91% / 603667 86%）===")
for sym, cost in MIRROR_COST.items():
    sells = fills[(fills["symbol"] == sym) & (fills["volume"] < 0)]
    below = sells[sells["vwap"] < cost * 0.999]
    print(f"  {NAMES[sym]}: 卖出{len(sells)}笔, 低于成本{len(below)}笔")
    if len(below):
        print(below[["trade_time", "vwap", "volume"]].to_string(index=False))

print("\n=== 600481 全部成交（fix4: 12笔磨到清仓）===")
print(fills[fills["symbol"] == "SHSE.600481"][["trade_time", "btype", "vwap", "volume"]].to_string(index=False))

print("\n=== 603667 全部成交（fix4: 逆势补仓73.85/65.39）===")
print(fills[fills["symbol"] == "SHSE.603667"][["trade_time", "btype", "vwap", "volume"]].to_string(index=False))

print("\n=== 600176 全部成交（T3验收: 尾仓应在趋势破位落袋）===")
print(fills[fills["symbol"] == "SHSE.600176"][["trade_time", "btype", "vwap", "volume"]].to_string(index=False))

print("\n=== 000988 5-7月卖出价位（TRAIL/PANIC 行为）===")
s988 = fills[(fills["symbol"] == "SZSE.000988") & (fills["volume"] < 0)]
print(s988[["trade_time", "vwap", "volume"]].to_string(index=False))
