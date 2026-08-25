# 日内冲高防御系统 - 实施指南

## 问题背景

**摩恩电气事件分析**：
- 08-25 日内从 7.39 涨至 8.13（涨幅 10%，接近涨停）
- 9:30 开盘系统给出 `watch_signal`（非 GO），理由是市场处于震荡，timing_gate 不满足
- 但后期冲高至 8.13，随后回落至 7.24（回落 11%）
- **根本问题**：虽然系统正确地没有给GO信号，但仍然存在两个缺陷：
  1. 如果交易员看到"放量涨停 + watch_signal"自主追高，会被冲高回落套住
  2. 系统缺少**实时冲高防御机制**，无法在冲高时预警"小心回落"

## 解决方案架构

### 第一层：涨停分类（防止被"集合竞价涨停"迷惑）

```python
from intraday_surge_defense import classify_daily_limit

# 三种涨停类型，风险完全不同
is_limit, limit_type, reason = classify_daily_limit(df_1min)

# limit_type:
#   "auction_limit"   → 集合竞价涨停 (09:30前)，极高风险，不应追高
#   "intraday_limit"  → 日内涨停 (09:30-14:50)，相对安全
#   "close_limit"     → 尾盘涨停 (14:50后)，较安全但流动性差
```

**应用**：如果检测到 `auction_limit`，系统应该在 L1 层面给 AVOID 信号。

### 第二层：冲高回落实时监控（防止被"冲高后回落"套住）

```python
from intraday_surge_defense import detect_pullback_from_high

pullback = detect_pullback_from_high(df_1min)

# 输出：
# {
#   "high_price": 8.13,
#   "high_time": "10:30",
#   "current_price": 7.24,
#   "pullback_ratio": 0.1092,  # 11% 回落
#   "alert_level": "critical"
# }
```

**应用**：
- `pullback_ratio < 2%` → 正常
- `2% < pullback_ratio < 5%` → 轻微警告
- `5% < pullback_ratio < 10%` → 明显警告
- `pullback_ratio > 10%` → 严重警告，建议止损

### 第三层：日内买点质量评估（防止"虚假突破后回落"）

```python
from intraday_surge_defense import check_intraday_buypoint_quality

quality = check_intraday_buypoint_quality(df_1min)

# 检查当前价是否处于"真实支撑"而不是"技术反弹"
# 综合判据：5m MA5、5m 放量、15m EMA8、距低点距离
```

**应用**：即使冲高有回落，如果当前价满足买点质量条件，仍可考虑底部建仓。

### 综合防御函数

```python
from intraday_surge_defense import intraday_surge_defense

result = intraday_surge_defense(
    code="002451",
    name="摩恩电气",
    df_1min=df_1min
)

# result.action:
#   "SAFE"    → 当前无冲高风险
#   "WARNING" → 有回落迹象，谨慎追高
#   "AVOID"   → 明显回落，应回避
#   "EXIT"    → 严重回落，建议止损
```

## 集成点

### 1. 持仓监控（每 5-10 分钟一次）

```python
from intraday_surge_monitor import monitor_surge_risks

result = monitor_surge_risks()
# 扫描所有 holdings 和 watchlist 中的冲高风险
# 输出 critical_alerts 供交易员参考
```

**使用场景**：
- 盘中定期监控，如发现 `EXIT` 信号可立即止损
- 发现 `AVOID` 信号时暂停追高

### 2. 信号评分时增强追高风险评估

在 position_builder 的信号评分中加入 L1 追高风险门控：

```python
def score_with_surge_defense(code, df_1min, daily_df):
    """在原有打分逻辑基础上，增加冲高风险评估"""
    
    # 原逻辑
    base_score = calculate_base_score(...)
    
    # 新增：冲高防御
    is_limit, limit_type, _ = classify_daily_limit(df_1min)
    if is_limit and limit_type == "auction_limit":
        return base_score * 0.5  # 集合竞价涨停降级50%
    
    pullback = detect_pullback_from_high(df_1min)
    if pullback["alert_level"] == "critical":
        return base_score * 0.3  # 严重回落降级70%
    elif pullback["alert_level"] == "warning":
        return base_score * 0.7  # 有回落降级30%
    
    return base_score
```

### 3. 交易员决策支持

在 GUI 或日志中显示冲高防御结果：

```
【建仓建议】摩恩电气 002451 @ 7.30
  基础信号分: 70
  └─ 涨停分类: auction_limit (集合竞价涨停) 
  └─ 冲高回落: 11% (critical)
  └─ 最终建议: AVOID - 极高风险
  
  ⚠️ 原因分析:
    1. 集合竞价即为涨停，属于"全市场情绪发泄"，非个股强度
    2. 当前距高点 8.13 已回落 11%，止跌信号未现
    3. 15m 形态未恢复，不具备"反弹空间"
  
  💡 建议：回避本次机会，等待缩量巩固 2-3 天后再评估
```

## 参数说明

### 追高风险评分（0-100）

| 情景 | 评分 | 措施 |
|-----|------|------|
| 无放量、无涨停 | 0-20 | SAFE |
| 放量但未涨停 | 20-40 | 正常 |
| 集合竞价涨停 | 70-100 | AVOID |
| 日内涨停 + 缩量 | 30-50 | WARNING |
| 冲高回落 > 10% | 80-100 | EXIT |

### 涨停分类阈值

| 类型 | 时间 | 风险 | 说明 |
|-----|------|------|------|
| auction_limit | 09:20-09:31 | 极高 | 集合竞价"团购"涨停 |
| intraday_limit | 09:31-14:50 | 中等 | 日内上升趋势涨停 |
| close_limit | 14:50-15:00 | 低 | 尾盘封板，流动性差 |

### 回落警告阈值

| 回落幅度 | 警告等级 | 建议 |
|---------|--------|------|
| < 2% | normal | 继续持有 |
| 2-5% | warning | 暂停追高 |
| 5-10% | warning | 不宜加仓 |
| > 10% | critical | 考虑止损 |

## 工作流示例

### 场景 1：摩恩电气式冲高回落

```
09:30 扫描：价格 7.17，系统 watch_signal（非GO）
         → 冲高防御: 无涨停，无回落，SAFE
         
10:30 实时监控：价格 8.13（涨停）
         → 冲高防御: 
           - 集合竞价涨停(09:30-09:31)
           - 无回落
           → 动作: WARNING（不宜追高，等待回踩）
         
11:00 实时监控：价格 7.50（回踩）
         → 冲高防御:
           - 集合竞价涨停
           - 回落 7.7%
           → 动作: WARNING（有回踩但未缩量确认）
           
11:30 实时监控：价格 7.24（持续回踩）
         → 冲高防御:
           - 集合竞价涨停
           - 回落 11%（critical）
           - 15m 未恢复
           → 动作: EXIT（建议止损）
```

### 场景 2：健康日内涨停

```
10:00 扫描：价格 5.80，底背离信号，wait_consolidation
         → 冲高防御: 无涨停，SAFE

10:30 上涨：价格 6.10（上升+放量）
         → 冲高防御: 
           - 无涨停
           - 无回落
           → 动作: SAFE
         
11:50 涨停：价格 6.39（日内涨停）
         → 冲高防御:
           - 日内涨停(11:50)
           - 无明显回落(< 1%)
           - 15m 上升趋势
           → 动作: SAFE（相对安全的涨停）
           
14:30 继续涨停：价格 6.39
         → 冲高防御: 继续持有（尾盘封板）
```

## 实现检查清单

- [ ] 导入 `intraday_surge_defense` 模块
- [ ] 在 `position_builder.py` 的信号打分中加入 L1 涨停分类
- [ ] 在 `position_builder.scan_stock()` 后调用 `intraday_surge_monitor()`
- [ ] 在日志/GUI 中显示冲高防御结果
- [ ] 配置告警阈值（根据个人风险偏好调整）
- [ ] 测试：使用历史数据回放验证冲高防御的准确性

## 已知限制

1. **分钟线质量** - 依赖分钟线数据的准确性；如果数据源滞后，防御效果会打折
2. **集合竞价定义** - 当前以 09:31 为界，实际不可撤单时段是 09:20-09:25，可根据需要微调
3. **流动性风险** - 对于流动性极差的小股，回落幅度预测可能偏大
4. **板块联动** - 集合竞价涨停时常带有板块统一抢筹的特征，个股判断可能失效

## 下一步

1. **集成到实时监控** - 融入盘中 GUI/推送机制
2. **历史回溯** - 用历史数据验证冲高防御的准确率和收益
3. **参数优化** - 根据实际交易反馈调整告警阈值
4. **扩展功能** - 增加"冲高后缩量确认"、"多日缩量巩固"等进阶条件
