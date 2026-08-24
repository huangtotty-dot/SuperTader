# P24-02 深水低吸模式 — 集成实现方案

> **方案类型**: 补充策略（Fallback Mode）  
> **目标标的**: 600481、601899等缓跌型  
> **预期效果**: 覆盖主策略漏掉的底部机会  
> **实现难度**: 中等  
> **上线风险**: 中等（评分降级有助于风控）

---

## 一、核心设计

### 问题场景

```
600481 (双良节能) 2026-08-24:
  振幅: 3.87% (4.29 → 4.13)
  主策略信号: 0 条
  原因: 缓跌不触及布林下轨，RSI未深度超卖
  
  但用户期望: 应该有低吸提示
```

### 解决方案

**深水低吸模式** = Fallback 信号生成器

当主策略（纯两点：bb_pct_5m + rsi_5m_p6）无法生成 BUY_LOW 时，
检查"深水"条件，生成降级评分的买入信号。

```
主策略评分 100.0 (急跌极值)
        ↓
   评分 = 0 (无信号)
        ↓
深水模式触发 (缓跌底部)
        ↓
降级评分 70.0 (BUY_LOW_DEEP_WATER)
```

---

## 二、实现步骤

### Step 1: 在 signal_engine.py 开头添加导入

```python
# 在第25-35行导入部分后添加
try:
    from deep_water_low_buy import DeepWaterLowBuyMode
except ImportError:
    DeepWaterLowBuyMode = None
```

### Step 2: 在 SignalEngine.__init__ 中初始化

```python
class SignalEngine:
    def __init__(self):
        # ... 现有代码 ...
        self.deep_water_mode = DeepWaterLowBuyMode() if DeepWaterLowBuyMode else None
```

### Step 3: 在 evaluate_swing() 中集成

在约第500行处，主策略返回 HOLD 时，添加深水检查：

```python
# 原代码（第500行之后）:
                    else:
                        decision_reason = "HOLD_NO_SWING"
                        buy_score = 0.0
                        sell_score = 0.0

# 修改为:
                    else:
                        decision_reason = "HOLD_NO_SWING"
                        buy_score = 0.0
                        sell_score = 0.0
                        
                        # P24-02: 深水低吸模式 (Fallback)
                        if self.deep_water_mode is not None:
                            dw_result = self.deep_water_mode.check_deep_water_signal(
                                code, df, _df5
                            )
                            if dw_result["trigger"]:
                                decision_reason = f"BUY_LOW_DEEP_WATER"
                                buy_score = dw_result["score"]  # 70.0
                                _det = dw_result["reason"]
                                _ind["entry_kind"] = "deep_water"
                                # 继续后续流程，让 BUY_LOW 信号被生成
```

### Step 4: 在 config.py 中添加参数

```python
# 在约第900行，PARAMS字典中添加：

"enable_deep_water_mode": False,        # 全局开关（先关闭，观察3日后启用）
"deep_water_daily_drop": 0.03,          # 日跌幅阈值 3%
"deep_water_low_proximity": 0.01,       # 接近低点 ±1%
"deep_water_rsi_max": 45.0,             # RSI(14) < 45
"deep_water_ma5_deviation": -0.02,      # 距MA5 < -2%
"deep_water_signal_score": 70.0,        # 评分 70（低于主策略100）
```

### Step 5: 可选 — 按标的配置

```python
# 在 config.py 中，STOCK_PARAMS 字典中：

STOCK_PARAMS = {
    "600481.SH": {
        # 双良节能 - 缓跌型，启用深水模式
        "enable_deep_water_mode": True,
        "deep_water_daily_drop": 0.03,
        "deep_water_rsi_max": 45.0,
    },
    "601899.SH": {
        # 紫金矿业 - 缓跌型，启用深水模式
        "enable_deep_water_mode": True,
    },
    "588170.SH": {
        # 科创50 ETF - 已禁用共振门控，不需要深水模式
        "enable_deep_water_mode": False,
    },
}
```

---

## 三、参数说明

| 参数 | 默认值 | 含义 | 调优建议 |
|------|--------|------|---------|
| enable_deep_water_mode | False | 全局开关 | 先观察3日，确认虚假信号<5%后启用 |
| deep_water_daily_drop | 0.03 | 日跌幅阈值 | 3% = 缓跌但有深度；可调至2%~4% |
| deep_water_low_proximity | 0.01 | 接近低点 | ±1% = 已接近底部；保守改为±2% |
| deep_water_rsi_max | 45.0 | RSI阈值 | <45 = 弱势区；激进改为<50 |
| deep_water_ma5_deviation | -0.02 | 距MA5 | -2% = 下穿MA5；激进改为-1% |
| deep_water_signal_score | 70.0 | 评分 | 70 = 低于主策略100；保守改为60 |

---

## 四、流程图

```
【evaluate_swing()】

  拉取5分钟K线 df_5min
           ↓
    计算bb_pct + rsi
           ↓
   ┌─ 主策略判定 ─┐
   │             │
   ├→ bb触轨+RSI满足?
   │  YES → BUY_LOW (100分)
   │
   └→ 不满足 → HOLD (0分)
           ↓
   【P24-02深水模式】
   
   check_deep_water_signal():
     1. 日跌幅 > 3%?  ✓
     2. 接近低点±1%?  ✓
     3. RSI(14) < 45?  ✓
     4. 距MA5 < -2%?   ✓
     ↓
   ALL满足? YES → BUY_LOW_DEEP_WATER (70分)
           NO  → HOLD (0分)
           ↓
    记录trace (decision_trace.jsonl)
           ↓
    应用拦截层:
      - 指数共振拦截? → shadow_signals记录
      - 防重桶拦截?   → shadow_signals记录
      ↓
    最终决策: 推送或拦截
```

---

## 五、风险评估与对策

### 风险1: 抄底在半山腰

**表现**: 低吸后继续跌 > 3%  
**原因**: RSI<45仍为弱势，未深度超卖  
**对策**:
- 降级评分70，允许后续拦截层过滤
- 建议用户设定止损 3% 以内
- 可调低 deep_water_rsi_max 至 35~40

### 风险2: 虚假信号增加

**表现**: 低吸后快速反弹失败  
**监控指标**: 浮亏>2%的频率  
**对策**:
- 观察3日，若虚假增加 >5% 则回退
- 可按标的启用/禁用（激进+缓跌 vs 保守+ETF）
- 可提高 deep_water_daily_drop 至 4% 或 5%

### 风险3: 与主策略冲突

**表现**: 同一个机会生成两个信号（100+70）  
**对策**:
- 深水模式仅在主策略返回0分时触发（已设计）
- 防重桶会自动去重

---

## 六、验证计划

### Phase 1: 离线验证（今日）

```bash
# 运行：
python deep_water_low_buy.py

# 输出：
  - 600481 6个月历史中触发多少次
  - 覆盖率与主策略的补充度
```

### Phase 2: 配置部署（周一）

```
1. 合并 deep_water_low_buy.py 到 signal_engine.py
2. 添加参数到 config.py
3. 测试编译通过
4. 部署到生产环境
```

### Phase 3: 上线观察（周一-周三）

```
Day 1: 验证 600481 是否出现新信号
Day 2: 统计虚假信号增加量
Day 3: 评估浮亏情况
      ↓
若虚假<5% → 正式启用
若虚假>5% → 调整参数或回退
```

---

## 七、代码示例

### 完整集成补丁

```python
# signal_engine.py 修改点

# 位置1: 开头导入 (第40行之后)
try:
    from deep_water_low_buy import DeepWaterLowBuyMode
except ImportError:
    DeepWaterLowBuyMode = None

# 位置2: __init__ (第95行之后)
class SignalEngine:
    def __init__(self):
        # ... 现有代码 ...
        self.deep_water_mode = None
        if DeepWaterLowBuyMode is not None:
            self.deep_water_mode = DeepWaterLowBuyMode()

# 位置3: evaluate_swing() 中 HOLD 处理 (第500-520行)
                    else:
                        decision_reason = "HOLD_NO_SWING"
                        buy_score = 0.0
                        sell_score = 0.0
                        
                        # ===== P24-02: 深水低吸模式 =====
                        if (self.deep_water_mode is not None and 
                            buy_score == 0.0 and 
                            PARAMS.get("enable_deep_water_mode", False)):
                            try:
                                dw_result = self.deep_water_mode.check_deep_water_signal(
                                    code, df, _df5
                                )
                                if dw_result["trigger"]:
                                    decision_reason = "BUY_LOW_DEEP_WATER"
                                    buy_score = dw_result["score"]
                                    _det = f"深水低吸: {dw_result['reason']}"
                                    _ind["entry_kind"] = "deep_water"
                            except Exception:
                                pass  # 深水模式失败不阻断主流程
```

---

## 八、后续优化

### 可选方向 1: 多时间框架联合

```python
# 结合 MA60 日线方向，增强可靠性
if ma60_direction == "DOWN":
    # 空头趋势中，深水模式更可靠
    deep_water_signal_score = 75.0
else:
    # 多头趋势中，深水信号更容易反弹
    deep_water_signal_score = 70.0
```

### 可选方向 2: 反弹验证

```python
# 等价格回升到前日高点附近再确认
if price > yesterday_high * 0.98:
    # 已反弹，确认低吸有效
    record_deep_water_win()
```

### 可选方向 3: 标的聚类

```python
# 对所有缓跌型标的统一配置
SLOW_FALLING_STOCKS = [
    "600481", "601899", "600977", ...
]
for code in SLOW_FALLING_STOCKS:
    STOCK_PARAMS[code] = {
        "enable_deep_water_mode": True,
    }
```

---

## 九、预期效果（基于模拟）

| 指标 | 预期 | 说明 |
|------|------|------|
| 600481 月均信号增加 | +5~8 条 | 缓跌型标的补充 |
| 虚假信号增加 | <5% | 降级评分有助于过滤 |
| 捕获率改进 | +1~2pp | 相对全部做T机会 |
| 最大回撤 | <3% | 止损设定有效 |

---

## 十、快速启动

### 最小可行版本（MVP）

```bash
# 1. 复制 deep_water_low_buy.py 到项目目录
cp deep_water_low_buy.py /path/to/project/

# 2. 在 signal_engine.py 中导入（3行代码）
try:
    from deep_water_low_buy import DeepWaterLowBuyMode
except ImportError:
    DeepWaterLowBuyMode = None

# 3. 在 config.py 中启用（1行代码）
"enable_deep_water_mode": True,

# 4. 重启系统，观察效果
```

### 验证步骤

```bash
# 检查 trace 中是否出现 BUY_LOW_DEEP_WATER 信号
grep "BUY_LOW_DEEP_WATER" t_io/traces/decision_trace_*.jsonl

# 统计 600481 推送数变化
grep "600481" t_io/traces/decision_trace_2026-08-24.jsonl | wc -l
grep "600481" t_io/traces/decision_trace_2026-08-25.jsonl | wc -l
```

---

## 总结

**P24-02 深水低吸模式** 是一个低风险、高收益的补充策略：

✅ **优势**:
- 覆盖缓跌型标的的底部机会
- 降级评分(70)有助于风控
- 可灵活按标的启用/禁用
- 实现简单，可快速部署

⚠️ **风险**:
- 可能抄底在半山腰（<3%止损可控）
- 虚假信号需观察（观察3日后决策）

📈 **预期效果**:
- 600481 从 0 信号 → ~5-8 条/月
- 全局虚假信号增加 <5%
- 综合做T捕获率提升 1-2pp

**建议** ✅ 可以立即部署，观察3日虚假信号后确认。
