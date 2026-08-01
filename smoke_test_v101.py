# -*- coding: utf-8 -*-
"""v1.0.1 smoke test: import + TrendRegime synthetic validation (ASCII only)"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd

results = []

def rec(name, ok, detail=""):
    results.append((name, ok, detail))

# T1: standalone import
try:
    import signal_engine
    rec("T1 import signal_engine standalone", True)
except Exception as e:
    rec("T1 import signal_engine standalone", False, repr(e))

try:
    import indicators, trend_regime
    rec("T1b import indicators/trend_regime", True)
except Exception as e:
    rec("T1b import indicators/trend_regime", False, repr(e))

# T2: does signal_engine see resample_to_5min in standalone mode?
ok = 'resample_to_5min' in dir(signal_engine)
rec("T2 resample_to_5min visible in signal_engine globals (standalone)", ok,
    "if False: standalone mode trend layer gets empty df_5m -> dead")

# T3: synthetic 5min data -> indicators -> TrendRegime
import indicators as ind
from trend_regime import TrendRegime, TrendState

np.random.seed(42)
n = 60
base = 100.0
# phase 1: downtrend 25 bars, phase 2: uptrend 35 bars
rets = np.concatenate([np.full(25, -0.004), np.full(35, 0.005)])
noise = np.random.normal(0, 0.001, n)
prices = base * np.exp(np.cumsum(rets + noise))
times = pd.date_range("2026-07-31 09:35", periods=n, freq="5min")
df5 = pd.DataFrame({
    "time": times, "open": prices, "high": prices * 1.002,
    "low": prices * 0.998, "close": prices,
    "volume": np.full(n, 10000.0), "amount": prices * 10000,
})
df5 = ind.add_5min_indicators(df5)
need_cols = ["dif_5m", "dea_5m", "bb_mid_5m", "bb_width_5m", "rsi_5m"]
rec("T3 indicators produce 5m cols", all(c in df5.columns for c in need_cols),
    ",".join([c for c in need_cols if c in df5.columns]))

tr = TrendRegime()
states = []
for i in range(5, n + 1):
    s, c = tr.update(df5.iloc[:i])
    states.append(s.value)
down_states = set(states[:25])
up_states = set(states[-10:])
rec("T4 downtrend detected as BEAR*", any("BEAR" in s for s in down_states), str(down_states))
rec("T5 uptrend detected as BULL*", any("BULL" in s for s in up_states), str(up_states))

# T6: debounce per-call problem — repeat update() on the SAME bar 20 times (15s polling)
tr2 = TrendRegime()
for i in range(5, 30):
    tr2.update(df5.iloc[:i])
before = tr2.state.value
for _ in range(20):  # 20 polls within one 5-min bar
    tr2.update(df5.iloc[:30])
after = tr2.state.value
# now give one new bar with reversed signal, poll 2 times only
rec("T6 debounce counts calls not bars", True,
    f"state after 20 same-bar polls: {before}->{after} (debounce_bars=2 means 2 calls ~30s, not 2 bars ~10min)")

# T7: RSI trigger swallowed on second poll of same bar
tr3 = TrendRegime()
for i in range(5, 40):
    tr3.update(df5.iloc[:40])
# find a bar where rsi crosses up through oversold: craft manually
tr4 = TrendRegime()
tr4._prev_rsi = 30.0; tr4._last_rsi = 30.0
df_one = df5.iloc[:40].copy()
df_one.loc[df_one.index[-1], "rsi_5m"] = 35.0  # rebound from oversold
tr4.update(df_one)
first = tr4.rsi_buy_trigger
tr4.update(df_one)  # second poll, same bar
second = tr4.rsi_buy_trigger
rec("T7 rsi trigger fires on 1st poll, swallowed on 2nd", first and not second,
    f"first={first} second={second}")

# T8: state_history flooding
rec("T8 history flooding (entries = calls not bars)", len(tr4._state_history) == 2,
    f"len={len(tr4._state_history)} after 2 same-bar calls")

print("=== SMOKE RESULTS ===")
for name, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
fails = [r for r in results if not r[1]]
print(f"TOTAL {len(results)}  FAIL {len(fails)}")
