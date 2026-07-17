# SHORT_MODE_PARAMS 参数优化报告

## 一、优化背景

**日期**: 2026-07-13  
**标的**: 华工科技 (000988)  
**当天走势**: 开盘155.01 → 9:46冲高162.08(+4.6%) → 全天阴跌 → 收盘154.24(-2.84%)

### 07-13 正T亏损四大原因
1. **尾盘频繁开仓**: 13:30后价格从156.14继续下跌至152.54，多次触发BUY_LOW，买入后无反弹空间即被套
2. **止盈止损不对称**: 止盈1.5%太紧被洗出，止损3%太松导致深套
3. **持仓时间过长**: 最长持有到收盘，没有强制日内闭环
4. **早盘预警失效**: 未触发Level 2预警，但实际走势是冲高回落+全天阴跌，完美反T场景

### 反T模式核心目标
- ✅ 早盘弱势时**第一时间卖出**（不需要等冲高，弱势即卖）
- ✅ 卖出后**严格等深跌**才接回（不能小跌就接）
- ✅ 接回后**当天必须完成闭环**（不能留隔夜敞口）

---

## 二、当前 SHORT_MODE_PARAMS 逐项分析

| 参数 | 当前值 | 问题诊断 | 风险等级 |
|------|--------|----------|----------|
| `morning_no_sell_until` | 930 | ✅ 正确：9:30即可卖，解除早盘保护 | 🟢 |
| `morning_no_sell_min_ret` | 0.005 | ⚠️ 语义歧义：正T下是"需涨0.5%才卖"，反T下应改为极负值确保弱势可卖 | 🟡 |
| `sell_holding_min_minutes` | 5 | ❌ 严重过短：买入后5分钟即鼓励卖出，反T买入后应持有到闭环 | 🔴 |
| `sell_holding_strict_minutes` | 10 | ❌ 过短：应恢复至35分钟，给足够利润空间 | 🔴 |
| `awaiting_buyback_score_boost` | 20 | ❌ 过高：接回加分20+门槛放松15，组合效应导致小跌就接 | 🔴 |
| `awaiting_buyback_threshold_relax` | 15 | ❌ 过大：大幅降低买入门槛，深跌纪律被破坏 | 🔴 |
| `awaiting_buyback_vwap_gap` | 1.005 | ❌ 严重错误：允许高于VWAP 0.5%接回，与"严格等深跌"矛盾 | 🔴 |
| `buy_confirm_min_score` | 30 | ✅ 方向正确：提高买入门槛 | 🟢 |
| `vwap_buy_deviation` | -0.025 | ✅ 正确：低于VWAP 2.5%才考虑接回 | 🟢 |
| `max_buy_times_per_stock` | 2 | ✅ 正确：减少买入次数 | 🟢 |
| `max_sell_times_per_stock` | 3 | ⚠️ 偏多：反T每笔卖出必须接回，减少至2次避免过度交易 | 🟡 |
| `cooldown_minutes` | 15 | ❌ 过短：频繁买卖，提高摩擦成本 | 🟡 |
| `stand_down_min_amplitude` | 0.008 | ❌ 过小：反T需要更大振幅才有足够价差 | 🟡 |
| `post_sell_rebuild_minutes` | 8 | ❌ 严重过短：卖出后8分钟即可触发买入，小跌就接 | 🔴 |
| `post_sell_rebuild_price_gap` | 0.005 | ❌ 过小：仅要求0.5%跌幅，不足以构成"深跌" | 🔴 |
| `rsi_oversold` | 30 | ✅ 正确：RSI<30才接，深跌才接 | 🟢 |
| `rsi_overbought` | 70 | ✅ 正确：RSI>70即卖，更早卖出 | 🟢 |
| `take_profit_pct` | 0.008 | ⚠️ 未使用：signal_engine.py中无此参数消费逻辑，冗余 | 🟡 |
| `take_profit_time_after` | 930 | ⚠️ 未使用：同上 | 🟡 |

### 关键发现

**`awaiting_buyback_vwap_gap = 1.005` 是最致命的问题**

在 `signal_engine.py:2302` 中的使用逻辑：
```python
price_below_vwap = bool(vwap) and price < vwap * ab_vwap_gap
```

- 默认值 `ab_vwap_gap = 0.998` → 价格需低于 VWAP × 0.998（低于VWAP 0.2%）
- 反T设置 `ab_vwap_gap = 1.005` → 价格只需低于 VWAP × 1.005（允许高于VWAP 0.5%）

这意味着：反T模式下，**价格可以在VWAP上方**就触发"卖后低吸"加分！这与"严格等深跌"完全矛盾。

---

## 三、具体修改建议

### 3.1 数值调整（直接修改）

| 参数 | 原值 | 新值 | 调整理由 |
|------|------|------|----------|
| `morning_no_sell_min_ret` | 0.005 | -0.999 | 确保弱势时绝对可以卖出，不受涨幅限制 |
| `sell_holding_min_minutes` | 5 | 30 | 反T买入后应持有足够时间，不轻易卖出 |
| `sell_holding_strict_minutes` | 10 | 35 | 恢复与正T一致，确保利润空间 |
| `awaiting_buyback_score_boost` | 20 | 8 | 大幅降低接回加分，抑制过早接回冲动 |
| `awaiting_buyback_threshold_relax` | 15 | 5 | 小幅放松门槛，深跌才触发 |
| `awaiting_buyback_vwap_gap` | 1.005 | **0.985** | 严格低于VWAP 1.5%才接回 |
| `buy_confirm_min_score` | 30 | 35 | 进一步提高买入确认门槛 |
| `cooldown_minutes` | 15 | 30 | 翻倍冷却时间，减少频繁交易 |
| `stand_down_min_amplitude` | 0.008 | 0.015 | 停手振幅提高到1.5%，反T需要更大空间 |
| `post_sell_rebuild_minutes` | 8 | 20 | 卖出后至少等20分钟才考虑买入 |
| `post_sell_rebuild_price_gap` | 0.005 | 0.012 | 要求至少1.2%跌幅才触发重建 |
| `max_sell_times_per_stock` | 3 | 2 | 减少卖出次数，每笔卖出必须严格接回 |

### 3.2 删除冗余参数

删除 `take_profit_pct` 和 `take_profit_time_after`：
- 经代码审查，`signal_engine.py` 中无此参数的消费逻辑
- 反T模式的核心是"当天闭环"，不依赖止盈机制
- 如后续需要止盈，应在 `signal_engine.py` 中统一实现

### 3.3 建议新增参数（需在 signal_engine.py 中实现）

以下参数本次仅在优化说明中建议，**需要在 signal_engine.py 中增加消费逻辑后才能生效**：

| 建议新增参数 | 建议值 | 作用 |
|--------------|--------|------|
| `short_no_afternoon_open_until` | 1330 | 午后13:30前禁止新的反T买入（防止早盘卖出后小跌就接） |
| `short_min_decline_to_buyback` | 0.015 | 卖出后要求最低跌幅1.5%才允许接回信号 |
| `short_force_close_time` | 1430 | 反T买入后最迟14:30必须卖出闭环（防止持仓到收盘） |
| `short_morning_weak_sell_boost` | 15 | 早盘弱势时（开盘价低于前收或开盘30分钟跌>0.5%）额外卖出加分 |

### 3.4 代码层建议修改（signal_engine.py）

1. **增加反T早盘弱势检测**：不依赖早盘预警，当 `open < prev_close` 或 `open_30min_ret < -0.005` 时直接给卖出加分
2. **增加反T午后开仓限制**：`t_val < short_no_afternoon_open_until` 时禁止 BUY_LOW 信号
3. **增加反T强制平仓**：`t_val >= short_force_close_time` 时强制触发卖出信号

---

## 四、07-13 回测推演（基于优化后参数）

### 原始SHORT_MODE_PARAMS下的反T行为推演
- 9:30 开盘即触发卖出保护检查：`t_val=930`，`morning_no_sell_until=930` → `t_val < 930` 为False → 保护不触发 ✅
- 但 `morning_no_sell_min_ret=0.005` 在此场景下无实际影响
- 9:46 冲高162.08：若RSI>70且range_pos高，可能触发SELL_HIGH ✅
- 卖出后 `post_sell_rebuild_minutes=8` → 9:54即可开始考虑买入
- `awaiting_buyback_vwap_gap=1.005` → 价格低于 VWAP×1.005 即可触发接回加分
- VWAP全天约157.15，157.15×1.005≈157.94
- 10:00价格约159.03 > 157.94 → 不触发？但还有其他买入逻辑
- 11:00价格约158.68 > 157.94 → 可能触发
- 实际上价格只要低于157.94就会触发"卖后低吸"加分，而当天大部分时间都低于此值
- **结论：原始参数下反T会过早接回，利润空间被压缩**

### 优化后参数的反T行为推演
- `awaiting_buyback_vwap_gap=0.985` → 价格需低于 157.15×0.985≈154.79
- 当天价格低于154.79的时段：午后14:00后
- `post_sell_rebuild_minutes=20` → 卖出后至少等20分钟
- `post_sell_rebuild_price_gap=0.012` → 价格需低于卖出价×0.988
- 假设9:46在161.08卖出，则需低于161.08×0.988≈159.15
- 结合VWAP条件（<154.79），两个条件同时满足约需等到午后
- **结论：优化后反T会在午后深跌区（14:00后）接回，利润空间更大**

---

## 五、总结

### 修改清单
- ✅ 12项数值调整
- ✅ 2项冗余参数删除
- ✅ 4项新增参数建议（待代码层实现）

### 核心优化方向
1. **收紧接回条件**：`awaiting_buyback_vwap_gap` 从1.005→0.985 是最关键的一刀
2. **延长等待时间**：`post_sell_rebuild_minutes` 从8→20，`cooldown_minutes` 从15→30
3. **提高买入门槛**：`buy_confirm_min_score` 从30→35，`stand_down_min_amplitude` 从0.8%→1.5%
4. **恢复持仓纪律**：`sell_holding_min_minutes` 从5→30，防止买入后急于卖出

### 后续行动
1. 在 `signal_engine.py` 中实现新增参数的消费逻辑
2. 使用 `backtest_short_000988_0713.py` 回测验证优化效果
3. 持续跟踪反T模式实盘表现，迭代优化

---

*报告生成时间: 基于 2026-07-13 华工科技快照数据分析*  
*版本: V1.26 SHORT_MODE_PARAMS 优化*
