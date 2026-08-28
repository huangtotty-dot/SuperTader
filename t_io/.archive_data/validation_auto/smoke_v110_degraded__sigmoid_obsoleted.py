# -*- coding: utf-8 -*-
"""
smoke_v110_degraded.py — v1.1.0 降级专项断言
1) 同一输入切 trend_state(BULL/BEAR/NEUTRAL/STRONG_BULL/STRONG_BEAR), calc_buy/sell_score 完全不变
2) FACTOR_WEIGHTS: 5m_trend=0 且总和=1.0, 5m_rsi=0.10 保留
3) signal_engine 源码不再消费门控方法(gate_multiplier/threshold_penalty/apply_t_mode 无调用点)
4) feats 信息层字段(trend_state 等)写入逻辑仍存在(signal_engine 中 feats[k]=v 趋势特征块)
"""
import re, sys
sys.path.insert(0, r"E:\06_T")
from signal_engine import ScoringEngine, FACTOR_WEIGHTS

base_feats = {
    "price": 100.0, "vwap": 99.0, "rsi": 35.0, "dif": -0.01, "dea": -0.02,
    "volume": 1000, "vol_ma5": 1200, "lower_shadow": 0.5, "upper_shadow": 0.2,
    "ema5": 99.5, "ema10": 99.0, "rsi_5m": 33.0, "dif_5m": 0.001, "dea_5m": 0.0005,
    "rsi5_buy_trigger": True, "rsi5_sell_trigger": False,
    "index_regime": "range", "trend_confidence": 0.55,
}

fails = []
# 1) trend_state 不变性
results = {}
for st in ("BULL", "BEAR", "NEUTRAL", "STRONG_BULL", "STRONG_BEAR"):
    f = dict(base_feats); f["trend_state"] = st
    b, _ = ScoringEngine.calc_buy_score(f)
    s, _ = ScoringEngine.calc_sell_score(f)
    results[st] = (b, s)
if len(set(results.values())) != 1:
    fails.append(f"T1 分数随 trend_state 变化: {results}")
else:
    print(f"[PASS] T1 五种 trend_state 下 buy/sell_score 完全一致 {list(results.values())[0]}")

# 2) 权重配置
w = FACTOR_WEIGHTS
total = sum(v for k, v in w.items() if k.startswith("factor_weight_"))
if w["factor_weight_5m_trend"] != 0.0:
    fails.append("T2 5m_trend 未置零")
if abs(total - 1.0) > 1e-9:
    fails.append(f"T2 权重总和={total} != 1.0")
if w["factor_weight_5m_rsi"] != 0.10:
    fails.append("T2 5m_rsi 未保留 0.10")
if not fails:
    print(f"[PASS] T2 5m_trend=0 / 5m_rsi=0.10 / 权重总和={total:.2f}")

# 3) 门控调用点清除
src = open(r"E:\06_T\signal_engine.py", encoding="utf-8").read()
hits = [m for m in ("gate_multiplier(", "threshold_penalty(", "apply_t_mode(") if m in src]
if hits:
    fails.append(f"T3 门控调用残留: {hits}")
else:
    print("[PASS] T3 signal_engine 无 gate_multiplier/threshold_penalty/apply_t_mode 调用点")

# 4) 信息层 feats 写入保留
if 'feats[k] = v' in src and '"trend_state"' in src:
    print("[PASS] T4 trend_state/confidence/rsi5 feats 写入逻辑保留(信息层)")
else:
    fails.append("T4 信息层 feats 写入丢失")

print("TOTAL", 4, "FAIL", len(fails))
for f in fails:
    print(" ", f)
sys.exit(1 if fails else 0)
