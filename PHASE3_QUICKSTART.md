# Phase 3 系统快速入门指南

## 快速开始 (5分钟)

### 1. 系统初始化

```python
from core.trading_system_coordinator import TradingSystemCoordinator

# 创建系统协调器
coordinator = TradingSystemCoordinator(config_dir="config")

# 初始化系统（自动进行三阶段验证）
coordinator.initialize_system(config_version="v2.0_current_20260825")

# 启动交易系统
coordinator.start_trading()
```

### 2. 配置管理

```python
from core.config_manager import ConfigManager

cfg = ConfigManager(config_dir="config")

# 加载配置版本
cfg.load_version("v2.0_current_20260825")

# 获取参数（支持点号路径）
bb_upper = cfg.get("signal.swing_bb_upper")  # 返回 1.0

# 修改参数
cfg.set("signal.swing_bb_upper", 1.2)

# 验证配置
is_valid, errors = cfg.validate()

# 保存快照
cfg.save_snapshot("my_config", "My custom configuration")

# 对比版本
diff = cfg.diff_versions("v2.0_current_20260825", "v2.0_current_20260825")
```

### 3. 信号生成与订单执行

```python
from core.dto import Signal, Order

# 生成交易信号
signal = coordinator.generate_signal(
    code="600000",
    signal_type="BUY_LOW",
    price=10.50,
    strength=85.0,
    reason="Ice point reversal detected"
)

# 创建订单
order = Order(
    code="600000",
    direction="BUY",
    quantity=100,
    price=10.50,
    timestamp=datetime.now()
)

# 执行订单
coordinator.execute_order(order)
```

### 4. 系统监控

```python
# 获取系统状态
status = coordinator.get_system_status()

# 获取诊断信息
diagnostics = coordinator.get_diagnostics()

# 更新持仓
holdings = {"600000": 100, "600001": 50}
coordinator.update_holdings(holdings)

# 生成报告
daily_report = coordinator.generate_daily_report()
system_state = coordinator.save_system_state()
```

### 5. 停止系统

```python
# 停止交易（自动保存报告）
coordinator.stop_trading()
```

---

## 配置文件说明

### 策略参数 (`config/strategies/`)

**swing2pt.yaml** - 日内两点交易配置
```yaml
swing_bb_upper: 1.0       # 上轨标准差倍数
swing_bb_lower: -0.8      # 下轨标准差倍数
time_window_start: "10:00" # 交易开始时间
time_window_end: "15:00"   # 交易结束时间
```

**dual_channel.yaml** - 双通道建仓配置
```yaml
dual_channel:
  channel1:
    name: "ice_point_reversal"
    weight: 0.6            # 权重
  channel2:
    name: "breakout_follow"
    weight: 0.4
```

**single_tier.yaml** - 单档加仓配置
```yaml
add_position:
  max_add_times: 3         # 最多加仓次数
stop_loss:
  max_drawdown: 3.0        # 最大回撤%
```

### 市场参数 (`config/market/`)

**index_levels.yaml** - 大盘阈值
```yaml
index_levels:
  sh:
    very_strong: 3500
    strong: 3300
    neutral: 3100
```

### 系统参数 (`config/system/`)

**data_fetch.yaml** - 数据获取配置
```yaml
data_fetch:
  interval: 1              # 获取间隔（秒）
  retries: 3               # 重试次数
cache:
  enabled: true
  ttl: 300                 # 缓存TTL（秒）
```

**execution.yaml** - 执行配置
```yaml
execution:
  order_submit:
    timeout: 5             # 提交超时（秒）
risk_control:
  single_order:
    max_loss_per_order: 1000  # 单笔最大亏损
```

**logging.yaml** - 日志配置
```yaml
logging:
  level: "INFO"            # 日志级别
  outputs:
    file:
      enabled: true
      path: "logs"
```

---

## 参数优化流程

### 完整流程

```
参数修改
    ↓
【离线验证】(Offline Validation)
├─ 检查参数完整性
├─ 历史回测 (过去3个月)
├─ 收益预期评估 (目标: >=5%)
├─ 风险指标检查 (最大回撤: <=15%)
└─ 假阳性率评估 (目标: <50%)
    ↓ [PASSED]
【纸面交易】(Paper Trading)
├─ 胜率检查 (目标: >=60%)
├─ 利润因子验证 (目标: >=1.2)
├─ 执行延迟检查 (目标: <200ms)
└─ 系统稳定性验证
    ↓ [PASSED]
【灰度部署】(Canary Deployment)
├─ 实盘表现对标
├─ 最大日亏限制检查
├─ 对标baseline性能 (偏差: <2%)
└─ 系统可靠性检查
    ↓ [PASSED]
【生产部署】(Production)
```

### 编程示例

```python
from core.optimization_pipeline import OptimizationPipeline

# 创建优化管线
pipeline = OptimizationPipeline()

# 运行完整管线
final_report = pipeline.run_full_pipeline(config)

# 查看结果
if final_report.status == "PASSED":
    print("Ready for production!")
else:
    print(f"Issues: {final_report.checks_failed}")

# 保存报告
pipeline.save_report(final_report)
```

---

## 指标跟踪

### 系统指标

```python
from core.metrics_tracker import MetricsTracker

tracker = MetricsTracker()

# 记录信号
tracker.record_signal("600000", "BUY_LOW", 10.50, 85.0)

# 记录订单
tracker.record_order("600000", "BUY", 100, 10.50, status="FILLED")

# 更新盈亏
tracker.update_pnl(daily_pnl=250, total_pnl=1500)

# 获取摘要
summary = tracker.get_summary()

# 保存报告
tracker.save_daily_report()
```

---

## 故障排查

### 配置验证失败

```python
from core.config_validator import ConfigValidator

validator = ConfigValidator()
is_valid, errors = validator.validate(config)

if not is_valid:
    for error in errors:
        print(f"{error.path}: {error.message}")
```

### 系统诊断

```python
# 获取完整诊断
diagnostics = coordinator.get_diagnostics()

print(f"Status: {diagnostics['system_status']}")
print(f"Queue sizes: {diagnostics['market_data_queue_size']}")
print(f"Errors: {diagnostics['metrics_summary']['system']['errors']}")
```

### 版本回滚

```python
# 快速回滚到上一个版本
cfg = ConfigManager()
cfg.rollback("v2.0_current_20260825")
```

---

## 常见任务

### 任务1: 测试新的参数组合

```python
cfg = ConfigManager()
cfg.load_version("v2.0_current_20260825")

# 修改参数
cfg.set("signal.swing_bb_upper", 1.3)
cfg.set("signal.swing_bb_lower", -0.9)

# 保存快照
cfg.save_snapshot("experiment_v1", "Testing new bands")

# 验证
is_valid, errors = cfg.validate()

# 运行优化管线
if is_valid:
    pipeline = OptimizationPipeline()
    pipeline.run_full_pipeline(cfg.current_config)
```

### 任务2: 比较两个版本

```python
cfg = ConfigManager()
diff = cfg.diff_versions(
    "v2.0_current_20260825",
    "experiment_v1_20260825_123456"
)

print("Changes:")
for key, change in diff['changed'].items():
    print(f"  {key}: {change['before']} -> {change['after']}")
```

### 任务3: 生成日报告

```python
coordinator = TradingSystemCoordinator()
coordinator.initialize_system()
coordinator.start_trading()

# ... 交易过程 ...

coordinator.stop_trading()

# 自动生成日报告
daily_report_file = coordinator.generate_daily_report()
```

---

## 相关文件

- 主配置: `config/versions/v2.0_current_20260825.yaml`
- API文档: `PHASE3_COMPLETION_SUMMARY.md`
- 测试用例: `test_*.py`

---

**Last Updated**: 2026-08-25  
**Phase**: 3 (Architecture Optimization)
