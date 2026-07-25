# -*- coding: utf-8 -*-
"""fix4 深层根因分析：逐票趋势画像 + 亏损卖出占比 + 开盘即卖模式 + 补仓归因"""
import pandas as pd

dz = r"E:\06_T\audit\gm_fusion\v1.0.1_fix4\report\data_zip"
pos = pd.read_csv(dz + r"\持仓数据_20260424_20260725.csv")
tr = pd.read_csv(dz + r"\交易数据_20260424_20260725.csv")

# ── 1. 逐票日线画像（持仓CSV的日收盘价） ──
pos["d"] = pd.to_datetime(pos["date"]).dt.date
px = pos.pivot_table(index="d", columns="symbol", values="price", aggfunc="last")
NAMES = {"SZSE.000988": "000988华工", "SHSE.600481": "600481双良", "SHSE.600176": "600176巨石", "SHSE.603667": "603667五洲"}
print("=== 逐票趋势画像（04-27 ~ 07-24）===")
for c in px.columns:
    s = px[c].dropna()
    ret = s.pct_change().dropna()
    print(f"{NAMES.get(c,c)}: 首={s.iloc[0]:.2f} 末={s.iloc[-1]:.2f} 窗口收益={(s.iloc[-1]/s.iloc[0]-1)*100:+.1f}%  "
          f"最大回撤={(s/s.cummax()-1).min()*100:.1f}%  日收益σ={ret.std()*100:.2f}%  下跌日占比={(ret<0).mean()*100:.0f}%  数据天数={len(s)}")

# ── 2. 亏损卖出占比（卖出价 vs 镜像/混合成本） ──
fills = tr[tr["btype"].astype(str).str.contains("买|卖", na=False)].copy()
fills["_t"] = pd.to_datetime(fills["trade_time"])
MIRROR_COST = {"SZSE.000988": 117.32, "SHSE.600481": 6.32, "SHSE.600176": 34.35, "SHSE.603667": 65.88}
print("\n=== 卖出价 vs 镜像成本 ===")
for sym, cost in MIRROR_COST.items():
    sells = fills[(fills["symbol"] == sym) & (fills["volume"] < 0)]
    below = sells[sells["vwap"] < cost * 0.999]
    print(f"{NAMES.get(sym,sym)}: 卖出{len(sells)}笔, 低于镜像成本{len(below)}笔 ({len(below)/max(len(sells),1)*100:.0f}%)  成本≈{cost}")

# ── 3. 开盘即卖（09:31-09:33 的卖出） ──
sells_all = fills[fills["volume"] < 0].copy()
sells_all["_hm"] = sells_all["_t"].dt.strftime("%H:%M")
open_dumps = sells_all[sells_all["_hm"] <= "09:33"]
print(f"\n=== 开盘3分钟内卖出: {len(open_dumps)}/{len(sells_all)} 笔 ===")
print(open_dumps[["trade_time", "symbol", "vwap", "volume"]].to_string(index=False))

# ── 4. 603667 两笔补仓买入的结局 ──
print("\n=== 603667 买入明细 ===")
buys = fills[(fills["symbol"] == "SHSE.603667") & (fills["volume"] > 0)]
print(buys[["trade_time", "vwap", "volume"]].to_string(index=False))
print("（对照：06-29 三笔卖出 @60.11/60.11/59.56，07 月最低约 59 附近）")

# ── 5. 底仓资金分配 vs 账户 ──
print("\n=== 镜像底仓资金分配（占15万账户）===")
ALLOC = {"SZSE.000988": (300, 117.32), "SHSE.600481": (1400, 6.32), "SHSE.600176": (300, 34.35), "SHSE.603667": (500, 65.88)}
for sym, (q, p) in ALLOC.items():
    print(f"{NAMES.get(sym,sym)}: {q}股×{p} = {q*p:,.0f}元 = {q*p/150000*100:.1f}%")

# ── 6. 000988 卖出结构：止损卖 vs 盈利卖 ──
print("\n=== 000988 卖出价位分布 ===")
s988 = fills[(fills["symbol"] == "SZSE.000988") & (fills["volume"] < 0)]
jul = s988[s988["_t"] >= "2026-07-01"]
print(f"5-6月卖出 {len(s988)-len(jul)} 笔, 均价 {s988[s988['_t']<'2026-07-01']['vwap'].mean():.1f}（成本区上方, 卖强）")
print(f"7月卖出 {len(jul)} 笔, 均价 {jul['vwap'].mean():.1f}（PANIC 止损）")
print(f"000988 费用合计 {fills[fills['symbol']=='SZSE.000988']['fee'].sum():.1f} 元（占其亏损 {fills[fills['symbol']=='SZSE.000988']['fee'].sum()/2027*100:.0f}%）")
