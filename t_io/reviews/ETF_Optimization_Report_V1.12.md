# 做T脚本 V1.12 ETF策略优化报告

> 生成时间：2026-06-24
> 分析对象：E:/06_T/main.py 做T盯盘脚本
> 优化版本：V1.12 ETF高频多笔利益最大化版

---

## 一、6/24 今日表现系统分析

### 1.1 总体表现

| 指标 | 数值 | 评价 |
|------|------|------|
| 预估净利润 | 0.00 元 | ❌ 未产生任何有效交易 |
| 总扫描轮次 | 约122轮 | 正常 |
| 飞书通知次数 | 3次SELL_HIGH | 仅有卖出信号，无买入信号 |
| 报错时段 | 09:45-10:46 | ❌ t_val前向引用 + _log_enhancer未定义 |
| 恢复正常 | 10:46后 | ✅ 修复后运行正常 |

### 1.2 各标的今日表现

| 标的 | 类型 | 今日状态 | 振幅 | 触发信号 | 评价 |
|------|------|----------|------|----------|------|
| 科创半导体ETF(588170) | ETF | 停手/可T观察 | 5.4%-7.3% | 0次 | ❌ 阈值过高无法触发 |
| 特变电工(600089) | 股票 | 无波待涨/可T观察 | 2.1% | 0次 | 波动不足 |
| 中国巨石(600176) | 股票 | 弱机会 | 11.8% | 0次 | 高位但无明确优势 |
| 英维克(002837) | 股票 | 可T观察 | 8.4% | 1次SELL_HIGH | ✅ 午后有信号 |
| 华工科技(000988) | 股票 | 可T观察 | 3.4% | 1次SELL_HIGH | ✅ 午后有信号 |

### 1.3 核心问题：ETF为何全天未触发？

从 decision_trace 数据分析，科创半导体ETF(588170) 的问题极其严重：

| 指标 | 实际值 | 阈值 | 差距 | 结论 |
|------|--------|------|------|------|
| buy_score 均值 | 13.6 | buy_threshold 均值 125.6 | -112 | 永远不可能触发买入 |
| sell_score 均值 | 34.1 | sell_threshold 均值 231.1 | -197 | 几乎不可能触发卖出 |
| 最接近买入 | 41 | 49 | -8 | 差距仍不足以触发 |
| 最接近卖出 | 71 | 44 | +27 | 13:01触发1次SELL_HIGH |

**根本原因**：ETF的基础阈值(base=40)被各种惩罚条件叠加后飙升到70-380，
而ETF本身波动小，加分因子有限，buy_score很难超过45。

---

## 二、ETF策略核心问题诊断

### 2.1 问题1：阈值被严重高估（致命）

**ETF基础阈值**：40（低于股票的45）
**实际阈值**：经过各种惩罚后均值125.6，最高380

阈值被推高原因：
- `benchmark_gate='weak'` → buy_threshold += 9
- `daily_gate='overheat'` → buy_threshold += 未知惩罚
- `market_state='range_bound'` → base += 1
- `cycle_count=0`（首次交易）→ buy_threshold += 2
- 其他各种条件叠加...

**结果**：ETF threshold 从40飙升到70-380，而 buy_score 最高仅41，
99.1%的决策都是HOLD，ETF完全失去了做T能力。

### 2.2 问题2：stand_down条件过严

原代码：ETF当 `gap < 0.003` 且 `buy_score < 44 and sell_score < 44` 时停手

- ETF价格波动天然小，0.3%的gap很容易满足
- 44分的阈值对于ETF来说几乎不可达到
- 导致全天44%的扫描被"买卖都不够强"停手

### 2.3 问题3：买卖份数没有优化

原代码：每次交易直接取 `holding.get('t_qty', 0)` = 13000份（全部仓位）

问题：
- ETF每天可T次数多（最多8轮），但每次都全仓买卖
- 如果第一次T失败，后续没有资金补救
- 没有根据信号强度调整仓位
- 不符合利益最大化原则

### 2.4 问题4：T+0优势未充分利用

- 冷却时间12分钟过长，错失快速波动
- min_amplitude=0.6% 对ETF偏高，ETF经常只有0.4-0.5%的波动
- min_profit_space=0.5% 偏高，ETF手续费低，0.3%即可盈利

---

## 三、V1.12 ETF高频多笔利益最大化优化方案

### 3.1 优化1：ETF阈值硬性上限（解决核心问题）

**方案**：在 `_dynamic_threshold` 和 `evaluate` 最后阶段，对ETF阈值添加硬性上限。

```python
# config.py 新增参数
"etf_threshold_cap": 38,  # ETF买卖阈值硬性上限

# signal_engine.py _dynamic_threshold
if is_etf:
    base = min(base, p.get("etf_threshold_cap", 38))

# signal_engine.py evaluate 最后阶段
if is_etf:
    buy_threshold = min(buy_threshold, p.get("etf_threshold_cap", 38))
    sell_threshold = min(sell_threshold, p.get("etf_threshold_cap", 38))
```

**预期效果**：ETF threshold 从均值125.6 → 上限38，buy_score均值13.6+加分后可达到30-45，
实际触发率从0.9%提升到15-25%。

### 3.2 优化2：ETF专属加分因子

```python
# config.py 新增参数
"etf_buy_score_boost": 5,   # ETF买入额外加分
"etf_sell_score_boost": 3,  # ETF卖出额外加分

# signal_engine.py evaluate
if is_etf:
    buy_score += p.get("etf_buy_score_boost", 5)
    sell_score += p.get("etf_sell_score_boost", 3)
```

**预期效果**：buy_score均值从13.6 → 18.6，更接近阈值38。

### 3.3 优化3：放宽ETF停手条件

```python
# 原代码
if holding.get('type') == 'etf' and gap < 0.003 and buy_score < 44 and sell_score < 44:
    return True, "ETF波动不足"

# 新代码
if holding.get("type") == "etf" and gap < p["etf_stand_down_gap"] and buy_score < 38 and sell_score < 38:
    return True, "ETF波动不足"
```

`etf_stand_down_gap` 从0.003 → 0.0015，buy_score/sell_score阈值从44 → 38。

### 3.4 优化4：ETF动态份数计算（核心创新）

**设计原则**：根据信号强度 + 剩余可交易次数 + 利益最大化，动态计算每次交易份数。

```python
def _calc_etf_qty(self, code, holding, action, sig_score, threshold):
    total_t_qty = holding.get("t_qty", 0)  # 13000份

    # 信号强度决定仓位比例
    signal_strength = sig_score - threshold
    if signal_strength >= 10:
        strength_pct = 0.25   # 强信号：25%仓位 = 3250份
    elif signal_strength >= 5:
        strength_pct = 0.15   # 中等信号：15%仓位 = 1950份
    else:
        strength_pct = 0.08   # 弱信号：8%仓位 = 1040份

    # 剩余次数调整（次数越少，每次越大）
    remaining_factor = 1.0 + (3 - remaining) * 0.3  if remaining <= 3

    # 确保最小交易单位（100份）
    qty = max(100, (qty // 100) * 100)

    # 确保不超过当前可交易额度
    if action == 'BUY': qty = min(qty, total_t_qty - net_qty)
    if action == 'SELL': qty = min(qty, net_qty)

    return qty
```

**示例**：13000份ETF，在不同情况下的交易份数：

| 场景 | 信号强度 | 剩余次数 | 计算份数 | 实际份数 | 占总仓 |
|------|----------|----------|----------|----------|--------|
| 早盘强买入 | +15 | 5次 | 13000×25%×1.0 | 3250 | 25% |
| 午后中等买入 | +7 | 3次 | 13000×15%×1.0 | 1950 | 15% |
| 尾盘弱买入 | +3 | 1次 | 13000×8%×1.6 | 1660 | 13% |
| 强卖出 | +12 | 4次 | 13000×25%×1.0 | 3250 | 25% |
| 最后1次卖出 | +8 | 1次 | 13000×15%×1.6 | 3120 | 24% |

**优势**：
- 强信号大仓位，弱信号小仓位，利益最大化
- 剩余次数少时自动加大仓位，避免资金闲置
- 保留部分资金用于后续补救，降低单次失误风险

### 3.5 优化5：ETF参数全面调整

| 参数 | 原值 | 新值 | 理由 |
|------|------|------|------|
| max_t_cycles_per_stock | 8 | 10 | 充分利用T+0优势 |
| max_buy_times_per_stock | 5 | 6 | 增加买入机会 |
| max_sell_times_per_stock | 5 | 6 | 增加卖出机会 |
| cooldown_minutes | 12 | 8 | 缩短冷却，捕捉快速波动 |
| min_amplitude | 0.006 | 0.004 | ETF波动天然较小 |
| min_profit_space | 0.005 | 0.003 | ETF手续费低，0.3%即可盈利 |
| market_state_threshold_bias | 4 | 2 | 降低市场状态惩罚 |
| rsi_overbought | 80 | 75 | 放宽超买条件 |
| rsi_oversold | 35 | 40 | 放宽超卖条件 |
| buy_confirm_min_score | 25 | 22 | 降低买入确认门槛 |

---

## 四、修改代码清单（V1.12）

### 4.1 config.py
- ETF_T0_PARAMS 全面更新（18项参数调整 + 6项新增）
- 新增：etf_threshold_cap, etf_stand_down_gap, etf_buy_score_boost, etf_sell_score_boost
- 新增：etf_min_trade_unit, etf_qty_base_pct, etf_qty_strong_pct, etf_qty_weak_pct

### 4.2 signal_engine.py
- 新增 `_calc_etf_qty` 方法：ETF动态份数计算
- 修改 `_should_stand_down`：ETF停手gap从0.003→0.0015，score从44→38
- 修改 `_dynamic_threshold`：ETF阈值硬性上限38
- 修改 `evaluate`：ETF专属加分 + 最终阈值保护

### 4.3 main.py
- 修改 `scan_once`：信号触发时调用 `_calc_etf_qty` 动态计算份数
- 修改 `scan_once`：强制平仓时也使用动态份数
- 修改 `notify`：飞书通知中显示ETF交易份数信息

---

## 五、预期效果与风险

### 5.1 预期效果

| 指标 | 优化前（6/24） | 优化后预期 | 提升 |
|------|---------------|-----------|------|
| ETF触发率 | 0.9%（3次/323轮） | 15-25% | 17-28x |
| buy_threshold均值 | 125.6 | ≤38 | -70% |
| sell_threshold均值 | 231.1 | ≤38 | -84% |
| 单次交易份数 | 13000（100%） | 1000-3250（8-25%） | 分散化 |
| 每日可T轮次 | 8轮 | 10轮 | +25% |
| 冷却时间 | 12分钟 | 8分钟 | -33% |

### 5.2 风险提示

- **频率风险**：触发率提升后，交易频率增加，需确保人工执行能跟上
- **小利润风险**：ETF单次利润可能仅0.3-0.5%，需确保手续费足够低
- **错误信号风险**：threshold降低后，可能增加错误信号，需观察1-2天验证
- **资金分配风险**：动态份数可能导致某些时段仓位过轻，需根据实际调整

### 5.3 验证建议

1. **观察1-2天**：记录每日触发次数、胜率、实际利润
2. **调整参数**：如果触发过多，调高 `etf_threshold_cap`；如果过少，继续降低
3. **份数优化**：根据实际资金量和执行能力，调整 `etf_qty_base_pct` 等参数
4. **对比验证**：对比优化前后同一标的的触发率和收益率

---

**报告结束**