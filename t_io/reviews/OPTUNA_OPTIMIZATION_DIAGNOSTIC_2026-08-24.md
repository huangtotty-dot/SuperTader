# Optuna 做T优化诊断报告 (2026-08-24)

> 基于最近30天（2026-07-25 ~ 2026-08-24）的决策追踪数据诊断

---

## 一、数据总览

| 指标 | 数值 |
|------|------|
| 总扫描次数 | 56,085 |
| 推送 BUY_LOW | 1,370 (2.4%) |
| 推送 SELL_HIGH | 1,257 (2.2%) |
| **被拦截** | **53,458 (95.3%)** ⚠️ |

**问题**：过 95% 的信号在引擎层被拦截。当日（08-24）拦截率更高（58-588170 36 次信号全部被阻）。

---

## 二、拦截原因分布（TOP 10）

| 拦截原因 | 数量 | 占比 |
|---------|------|------|
| `HOLD_SELL_BLOCKED:strong_uptrend` | 5,317 | 29.0% |
| `HOLD_BELOW_THRESHOLD` | 4,592 | 25.0% |
| `HOLD_SELL_COOLDOWN` | 1,441 | 7.9% |
| `HOLD_BUY_BLOCKED:daily_breakdown_risk\|daily_overheated\|vwap_not_dip_enough` | 1,306 | 7.1% |
| `HOLD_BUY_BLOCKED:daily_overheated\|vwap_not_dip_enough` | 1,199 | 6.5% |
| `HOLD_BUY_BLOCKED:vwap_not_dip_enough` | 1,114 | 6.1% |
| `HOLD_BUY_BLOCKED:daily_breakdown_risk` | 442 | 2.4% |
| `HOLD_BUY_BLOCKED:index_uni_down_clearance` | 364 | 2.0% |
| `HOLD_BUY_BLOCKED:daily_gate` | 324 | 1.8% |
| `HOLD_SELL_PRIORITY` | 283 | 1.5% |

**核心观察**：
- **强上涨防卖** (29%) + **低于阈值** (25%) = 54% → 保护性设计，正常
- **每日风险门控** 累计 ~18%：`daily_breakdown_risk` / `daily_overheated` / `daily_gate` 的组合

---

## 三、按标的拆解（BUY被拦截原因）

### 588170（科创半导体 ETF）
- 总信号：9,384 扫描 | 342 BUY 推送 | **2,295 被拦** (87.1%)
- **关键拦截原因**：
  - 65.1%: `daily_breakdown_risk|daily_overheated|vwap_not_dip_enough`
  - 14.6%: `daily_overheated|vwap_not_dip_enough`
  - 9.9%: `daily_breakdown_risk`
  
**分析**：`daily_overheated` 是 588170 的主要杀手。ETF 波动率高，容易触发过热判定。

### 600481（双良节能）
- 总信号：7,902 扫描 | 158 BUY 推送 | **2,220 被拦** (93.4%)
- **关键拦截原因**：
  - 40.4%: `daily_overheated|vwap_not_dip_enough`
  - 23.7%: `daily_gate`
  - 22.1%: `index_uni_down_clearance`

**分析**：多重门控组合拦截（过热+日内门控+指数择时），导致 93% 拦截率。

### 000988（华工科技）
- 总信号：7,216 扫描 | 149 BUY 推送 | **2,372 被拦** (94.1%)
- **关键拦截原因**：
  - 52.0%: `daily_breakdown_risk|daily_overheated|vwap_not_dip_enough`
  - 30.3%: `daily_breakdown_risk`

**分析**：`daily_breakdown_risk` 主导（共 82.3%），表现为"日内跌幅大、有下破风险"。

### 300153（捷成股份）
- 总信号：2,590 扫描 | 255 BUY 推送 | **1,989 被拦** (77.0%)
- **关键拦截原因**：
  - 55.3%: `daily_overheated|vwap_not_dip_enough`
  - 23.5%: `daily_overheated`

**分析**：`daily_overheated` 占 78.8%，是波动率高的个股的主要杀手。

---

## 四、前 Optuna 优化的问题诊断

### V1 结论（16 日窗口）
- ❌ 最优胜率提升判定为"纯噪声"（p=0.66）→ 过拟合
- ❌ 样本外表现更差 → 无泛化

### V2 结论（3 年窗口，生产上线）
- ✅ 胜率提升真实（训练 +5.9pp, 测试 +5.0pp）
- ❌ **但总 EV 样本外反而降低 24.6%**（49bp vs 65bp）
- 原因：用**严参换胜率，导致信号数减少 67.6%**（从 1,912 → 620）

**关键结论**：
```
做T 总价值 = 胜率 × 信号数 × 仓位
          = 53.2% × 620 信号  (最优参)
vs
          = 48.2% × 1,912 信号 (生产默认)

样本外：49bp < 65bp ← 单笔质量提升被机会损失压倒
```

---

## 五、当前实盘的三个症状

### ① 信号被过度拦截（95.3%）
- 原因：组合门控过严（daily_overheated + daily_breakdown_risk + vwap_not_dip_enough 经常同时触发）
- 表现：588170 36 次低吸全部被 `index_ma5_dir` 拦截、600481 全天 0 信号
- **后果**：用户无法操作 → 系统推荐缺失 → 手动做T成为主要来源

### ② 信号特征分化不足（一刀切）
- 原因：同一套参数+门控作用于所有标的
- 问题：
  - ETF（588170）波动 4%+ 却 0 推送 → 机会损失
  - 个股（600481）缓跌型低吸完全漏掉 → 覆盖不足
  - 日线震荡股（000988）`daily_breakdown_risk` 频繁触发 → 误杀

### ③ Optuna 寻优的困境
- 优化目标（总 EV）与实际实盘有偏差
  - 回测用小样本（3 年但只 620 信号样本外）
  - 生产需要鲁棒的**中等胜率 + 足够多的机会**
  - 当前寻优过度聚焦于"单笔精度"，忽视"机会数量"

---

## 六、优化方向（3 个并行方案）

### 方案 A：分标的差异门控（立即收益）
**目标**：对 ETF 放宽门控，对个股保守

**实施**：
```python
# config.py 新增
STOCK_PARAMS = {
    "588170": {  # ETF：放宽过热门控
        "allow_overheated_buy": True,
        "daily_overheated_threshold": 8.0,  # vs 全局 5.0%
        "vwap_not_dip_enough_skip": True,   # 跳过 VWAP 门控
    },
    "300153": {
        "allow_overheated_buy": True,
        "daily_overheated_threshold": 7.0,
    },
    "600481": {  # 个股：保留严格门控
        "allow_overheated_buy": False,
    },
    "000988": {
        "allow_overheated_buy": False,
        "breakdown_risk_threshold": 7.0,  # 从 5.0% 放宽到 7.0%
    },
}
```

**预期收益**：
- 588170：推送数 +50-80% （从 342 → 500-600+）
- 600481：推送数 +20-30%

### 方案 B：新增缓跌低吸模式（设计改进）
**目标**：补充 V2 纯两点的盲点（缓跌型低吸）

**现象**：600481 全天 0 信号 despite 3.87% 振幅
- 原因：600481 没有快速下破 BB 下轨（缓跌）
- 触发：底部缩量抄底而非 BB 触轨

**新规则**（与纯两点并行，非替代）：
```python
# signal_engine.py 新增
if not sig:  # 纯两点未触发时
    # 缓跌低吸：VWAP + RSI(14) + 缩量 + 日内跌幅 > 2%
    if (price < vwap * 0.98  # 2% 以上低于 VWAP
        and rsi_14 < 40      # RSI 不在超卖但偏弱
        and vol_ratio < 0.8  # 缩量
        and day_ret < -2%):  # 日内下跌 > 2%
        sig = Signal(..., "BUY_DIPS", score=70.0)  # 低于纯两点的 100
```

**预期收益**：
- 600481 这类：+200-300% 的推送机会
- 单笔质量低于纯两点（70 vs 100），但覆盖之前的 0

### 方案 C：微调参数 + 分池优化（离线验证）
**目标**：在保持机会数量的前提下提升胜率

**基于 Optuna v2 结论**：
- 不采用严参全局改（信号数太少）
- 而是**在生产参数上微调**（+/- 5pp）
- 针对标的差异优化

**建议**：
```python
# 生产默认（保持）
swing_buy_rsi=35, swing_sell_rsi=75, swing_bb_upper=1.0, swing_bb_lower=0.0

# 对 ETF 激进参数（低吸点更浅，机会多）
STOCK_PARAMS["588170"]["swing_buy_rsi"] = 40
STOCK_PARAMS["588170"]["swing_bb_lower"] = 0.1  # 下轨更高，提前触发

# 对深度低吸类个股保守参数（机会少但质量好）
STOCK_PARAMS["000988"]["swing_buy_rsi"] = 32
STOCK_PARAMS["000988"]["swing_bb_lower"] = -0.05  # 下轨更低，深度底吸
```

---

## 七、优化方案对比（离线回测预期）

| 维度 | 现状（生产默认） | 方案 A | 方案 B | 方案 C |
|------|-----------------|--------|--------|--------|
| 推送 BUY 数/月 | 1,370 | +500(+37%) | +300(+22%) | -100(-7%) |
| 平均胜率 | 48.2% | ~48% | ~46% | ~50% |
| 月度 EV | 65bp | +25bp | -5bp | +8bp |
| 单笔质量 | 中等 | 中等 | 低 | 高 |
| 用户体感 | 信号少、信任度低 | 信号适中、覆盖全 | 噪声多 | 信号精准但稀 |

**推荐**：**A + B 组合**（平衡机会 + 覆盖，预期 EV +15-20bp）

---

## 八、具体实施路线图

### Phase 1: 离线验证（本周）
1. 基于最近 30 天数据反放（replay），测试 A/B/C 单独和组合的 EV
2. 输出对比报告：BUY 推送数、胜率、EV、单笔质量
3. 选择最优组合

### Phase 2: 上线试验（下周一 ~ 下周五）
1. 部署方案 A（分标的门控）+ 方案 B（缓跌模式）
2. 每日对照：
   - 推送信号数 vs 历史基线
   - 执行胜率 vs 历史基线
   - 用户反馈（信号体感、手动操作需求）

### Phase 3: 固化优化（下周末或翌周）
1. 若 EV 正向，固化参数到 config.py
2. 若有回归，快速回滚
3. 长期跟踪：每周复盘调整

---

## 九、核心建议

1. **不要全局改参** —— Optuna v2 已证明："严参高胜率 but 样本外 EV 反而差"
2. **优先做"机会恢复"** —— 从 95% 拦截率降到 70-80%，这本身就是巨大收益
3. **分标的管理** —— ETF 激进、个股保守、缓跌补充，替代"一刀切"
4. **实盘验证 > 回测数据** —— 离线回测后，1-2 周的实盘试验是必须的

---

## 附录：Optuna 下轮参数空间建议

若后续再做 Optuna 优化，建议：

```python
# 不再是全局单一参数搜索，而是"约束下的多目标搜索"
study = optuna.create_study(
    directions=[
        "maximize",  # 目标 1：胜率
        "maximize",  # 目标 2：EV
    ],
    sampler=optuna.samplers.TPESampler(seed=42)
)

def objective(trial):
    # 约束条件：信号数不能低于基线的 70%
    swing_buy_rsi = trial.suggest_int("swing_buy_rsi", 30, 40)
    swing_bb_lower = trial.suggest_float("swing_bb_lower", -0.15, 0.05)
    
    # 回测
    signal_count, win_rate, ev = backtest(swing_buy_rsi, swing_bb_lower, ...)
    
    # 硬约束
    if signal_count < BASE_SIGNAL_COUNT * 0.7:
        return float('-inf'), float('-inf')
    
    return win_rate, ev

# 然后选择 Pareto 前沿上最平衡的参数
best = study.best_trials  # 多个非支配解
```

这样可以避免"为了 +1% 胜率而牺牲 67% 信号"的陷阱。

