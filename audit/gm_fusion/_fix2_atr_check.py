# -*- coding: utf-8 -*-
"""fix2 PANIC 未触发根因验证：
日线 = archive 日线CSV(至07-06) + 分钟快照重建(07-07起)，复算 TR14/close 与 -5*ATR 触发线，
对照期末持仓成本 162.14 的 profit_pct 逐日判定是否应触发 PANIC。"""
import glob, json
import pandas as pd

# 1) 日线历史
h = pd.read_csv("E:/06_T/archive/t_io_reports/000988_history.csv")
h["d"] = pd.to_datetime(h["time"], format="%Y%m%d").dt.date
daily = h[["d", "open", "high", "low", "close"]].copy()

# 2) 分钟快照重建 07-07 之后日线
rows = []
for f in sorted(glob.glob("E:/06_T/t_io/minute_snapshots/2026/07/000988_2026-*.json")):
    j = json.load(open(f, encoding="utf-8"))
    bars = j.get("bars") or []
    if not bars:
        continue
    b = pd.DataFrame(bars)
    rows.append({"d": pd.to_datetime(j["date"]).date(),
                 "open": b["open"].iloc[0], "high": b["high"].max(),
                 "low": b["low"].min(), "close": b["close"].iloc[-1]})
snap = pd.DataFrame(rows)
daily = pd.concat([daily, snap], ignore_index=True).drop_duplicates("d", keep="last")
daily = daily.sort_values("d").reset_index(drop=True)

# 3) TR14 与触发线
daily["prev_close"] = daily["close"].shift(1)
tr = pd.concat([(daily["high"] - daily["low"]),
                (daily["high"] - daily["prev_close"]).abs(),
                (daily["low"] - daily["prev_close"]).abs()], axis=1).max(axis=1)
daily["TR14"] = tr.rolling(14).mean()
daily["atr_pct"] = daily["TR14"] / daily["close"]
daily["trigger_pct"] = -5 * daily["atr_pct"] * 100          # fix2 PANIC 触发线(%)
COST = 162.14                                                # 期末持仓成本（成交记录）
daily["profit_close"] = (daily["close"] / COST - 1) * 100
daily["profit_low"] = (daily["low"] / COST - 1) * 100        # 盘中最深浮亏
daily["hit_close"] = daily["profit_close"] <= daily["trigger_pct"]
daily["hit_low"] = daily["profit_low"] <= daily["trigger_pct"]

jul = daily[daily["d"] >= pd.Timestamp("2026-06-24").date()]
cols = ["d", "close", "low", "atr_pct", "trigger_pct", "profit_close", "profit_low", "hit_close", "hit_low"]
print(jul[cols].round(4).to_string(index=False))

jun = daily[(daily["d"] >= pd.Timestamp("2026-06-01").date())]
print("\n6月以来 ATR%% 范围: %.3f ~ %.3f" % (jun["atr_pct"].min(), jun["atr_pct"].max()))
mj = daily[(daily["d"] >= pd.Timestamp("2026-05-01").date()) & (daily["d"] <= pd.Timestamp("2026-06-24").date())]
print("5-6月卖强期 ATR%% 范围: %.3f ~ %.3f" % (mj["atr_pct"].min(), mj["atr_pct"].max()))
print("7月 hit_close 天数:", int(jul["hit_close"].sum()), " hit_low 天数:", int(jul["hit_low"].sum()), " 共", len(jul), "天")
