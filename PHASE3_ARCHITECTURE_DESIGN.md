# 第三阶段：架构优化工程 - 详细设计

**制定时间**: 2026-08-26 00:45  
**基于**: 第一、二阶段的完整清理成果

---

## 🎯 第三阶段目标

从现状的**功能驱动、代码散落**升级到**架构驱动、清晰分层**的系统。

**核心目标**:
1. 建立稳定的四层架构
2. 实现集中式配置管理
3. 自动化的参数优化管线

---

## 📐 四层架构设计

### 架构示意

```
┌─────────────────────────────────────────────┐
│          GUI / API / 人工决策                │  第4层：接口层
├─────────────────────────────────────────────┤
│    EXECUTION (订单、撤销、风控)              │  第3层：执行层
├─────────────────────────────────────────────┤
│  SIGNAL + POSITION (决策逻辑)                │  第2层：决策层
│  ├─ SignalEngine (日内两点)
│  ├─ PositionBuilder (双通道建仓)
│  └─ PositionSizer (仓位计算)
├─────────────────────────────────────────────┤
│  ANALYSIS (市场分析)                         │  第1a层：分析层
│  ├─ Indicators (技术指标)
│  ├─ Regime (市场制度)
│  └─ Correlation (关联性)
├─────────────────────────────────────────────┤
│  DATA (行情 + 状态)                          │  第1层：数据层
│  ├─ Fetcher (实时行情)
│  ├─ Cache (缓存)
│  ├─ Snapshots (快照)
│  └─ State (系统状态)
└─────────────────────────────────────────────┘
```

### 各层职责

#### 第1层：数据层 (Data)
**职责**: 采集、缓存、管理所有数据

```python
# data/fetcher.py
class DataFetcher:
    def fetch_realtime(self, code) -> MarketData:
        """获取实时行情"""
    
    def fetch_historical(self, code, start, end) -> List[OHLCV]:
        """获取历史数据"""

# data/cache.py  
class DataCache:
    def get(self, key) -> Any:
        """缓存读取"""
    
    def set(self, key, value, ttl=None):
        """缓存写入"""

# data/state.py
class SystemState:
    def load(self) -> Dict:
        """加载系统状态"""
    
    def save(self, state: Dict):
        """保存系统状态"""
```

#### 第1a层：分析层 (Analysis)
**职责**: 市场分析、指标计算、制度判断

```python
# analysis/indicators.py
class IndicatorEngine:
    def calculate_rsi(self, data, period=14) -> pd.Series:
        """计算RSI"""
    
    def calculate_ma(self, data, periods=[5, 10, 20]) -> Dict:
        """计算移动平均"""

# analysis/regime.py
class RegimeAnalyzer:
    def detect_index_regime(self, data) -> RegimeState:
        """指数日线制度"""
    
    def detect_trend(self, data) -> TrendState:
        """趋势判断"""

# analysis/correlation.py
class CorrelationAnalyzer:
    def check_resonance(self, index, stock) -> float:
        """检查共振"""
```

#### 第2层：决策层 (Decision)
**职责**: 生成交易信号和建仓建议

```python
# strategies/signal_engine.py
class SignalEngine:
    def evaluate(self, market_data) -> Signal:
        """生成日内信号（纯两点）"""

# strategies/position_builder.py
class PositionBuilder:
    def scan(self, market_data) -> BuildSignal:
        """扫描建仓机会（双通道）"""

# strategies/position_sizer.py
class PositionSizer:
    def calculate(self, signal, position) -> SizingAdvice:
        """计算仓位（单档）"""
```

#### 第3层：执行层 (Execution)
**职责**: 订单管理、风控、实际执行

```python
# execution/executor.py
class OrderExecutor:
    def submit_order(self, order: Order) -> OrderResult:
        """提交订单"""
    
    def cancel_order(self, order_id: str):
        """撤销订单"""

# execution/risk_manager.py
class RiskManager:
    def check_risk(self, signal, position) -> RiskCheck:
        """风险检查"""
    
    def apply_limits(self, order) -> Order:
        """应用风控限制"""
```

#### 第4层：接口层 (Interface)
**职责**: 与外部交互

```python
# main.py - 实盘主循环
class TradingSystem:
    def run(self):
        """主循环：获取数据 → 分析 → 决策 → 执行"""

# t_gui.py - GUI看板
class DashboardApp:
    def render(self):
        """渲染复盘看板"""

# api/ - REST API（可选）
class TradeAPI:
    def get_signals(self) -> List[Signal]:
        """查询当前信号"""
```

---

## ⚙️ 配置管理中心设计

### 配置架构

```
config/
├── defaults.py                        # 全局默认参数
├── validator.py                       # 参数验证
│
├── strategies/
│   ├── swing2pt.yaml                  # 日内两点
│   ├── dual_channel.yaml              # 双通道建仓
│   └── single_tier.yaml               # 单档加仓
│
├── market/
│   ├── index_levels.yaml              # 大盘阈值
│   └── individual_stock.yaml          # 个股参数
│
├── system/
│   ├── data_fetch.yaml                # 数据获取配置
│   ├── execution.yaml                 # 执行配置
│   └── logging.yaml                   # 日志配置
│
└── versions/
    ├── v2_swing2pt_20260825.yaml      # 当前版本
    ├── v2_swing2pt_exp_20260826.yaml  # 实验版本
    └── v1.2_legacy_20260801.yaml      # 历史版本
```

### 配置管理API

```python
# config/manager.py
class ConfigManager:
    def load_version(self, version: str) -> Dict:
        """加载指定版本的参数"""
    
    def get(self, path: str, default=None) -> Any:
        """获取参数值 (例: "signal.swing_bb_upper")"""
    
    def set(self, path: str, value: Any):
        """设置参数值"""
    
    def validate(self) -> ValidationResult:
        """验证所有参数的合法性"""
    
    def save_snapshot(self, name: str):
        """保存当前配置快照"""
    
    def diff_versions(self, v1: str, v2: str) -> Dict:
        """比较两个版本的差异"""
    
    def rollback(self, version: str):
        """快速回滚到某个版本"""

# 使用示例
cfg = ConfigManager()
cfg.load_version("v2_swing2pt_20260825")
bb_upper = cfg.get("signal.swing_bb_upper")  # 返回 1.0
cfg.validate()  # 验证参数
cfg.save_snapshot("exp_20260826")  # 保存快照
```

---

## 🔄 优化管线设计

### 参数优化流程

```
新参数 / 变更
    ↓
【离线验证】(1-2小时)
├─ 历史回测 (过去3个月)
├─ 收益预期 (目标覆盖率、夏普比)
├─ 风险指标 (最大回撤)
└─ 假阳性率 (<40%)
    ↓ (✅通过)
【纸面交易】(3-5天)
├─ 模拟交易 (小仓位)
├─ 信号质量 (胜率、赔率)
├─ 执行效果 (成交价格偏差)
└─ 系统稳定性 (错误率、卡顿)
    ↓ (✅通过)
【灰度部署】(1-2周)
├─ 扩大跟踪股票
├─ 逐日扩大仓位比例
├─ 持续监控关键指标
└─ 与旧版本对标
    ↓ (✅稳定)
【正式发布】(长期)
├─ 标记版本号
├─ 记录上线日期
├─ 定期对标历史
└─ 持续复盘
```

### 优化管线实现

```python
# optimization/pipeline.py
class OptimizationPipeline:
    def offline_validate(self, config, data_range: DateRange) -> Report:
        """离线验证"""
        backtest = Backtester()
        result = backtest.run(config, data_range)
        
        # 检查收益、覆盖率、风险
        if not self._check_metrics(result):
            return Report(status='FAILED', reason='指标不达标')
        
        return Report(status='PASSED', data=result)
    
    def paper_trade(self, config, days: int = 5) -> Report:
        """纸面交易验证"""
        simulator = PaperTradeSimulator()
        results = []
        
        for _ in range(days):
            daily_result = simulator.run_daily(config)
            results.append(daily_result)
        
        # 分析胜率、赔率等
        metrics = self._analyze_results(results)
        if not self._check_paper_trade_metrics(metrics):
            return Report(status='FAILED', reason='纸面表现不佳')
        
        return Report(status='PASSED', data=metrics)
    
    def canary_deploy(self, config, pct: float = 0.1) -> Report:
        """灰度部署（10%仓位）"""
        # 部署到 10% 仓位
        # 监控关键指标
        # 逐日扩大
        pass
    
    def rollback(self, version: str):
        """快速回滚"""
        cfg = ConfigManager()
        cfg.load_version(version)
        cfg.validate()
        # 重启系统应用新配置

# optimization/metrics.py
class MetricsTracker:
    def record_signal(self, code: str, signal: Signal):
        """记录信号"""
    
    def record_execution(self, code: str, order: Order, result: OrderResult):
        """记录执行"""
    
    def daily_report(self, date: str) -> Report:
        """日报告"""
    
    def compare_versions(self, v1: str, v2: str, period: DateRange) -> Comparison:
        """版本对比"""
```

---

## 📋 实施路线

### Phase 3.1: 架构框架建设 (1周)
- [ ] 建立 core/analysis/strategies/execution 目录结构
- [ ] 创建基础类和接口 (DataSource, Signal, Position 等)
- [ ] 实现层间通信的 DTO 定义
- [ ] 建立依赖注入框架（简化版）

### Phase 3.2: 配置中心实现 (1周)
- [ ] 实现 ConfigManager 类
- [ ] 迁移所有参数到 YAML 配置
- [ ] 建立版本管理机制
- [ ] 实现快照和回滚功能

### Phase 3.3: 优化管线实现 (1.5周)
- [ ] 实现 OptimizationPipeline 类
- [ ] 建立离线验证框架
- [ ] 实现纸面交易模拟器
- [ ] 建立灰度部署机制
- [ ] 实现 MetricsTracker

### Phase 3.4: 系统集成和测试 (1周)
- [ ] 将现有代码集成到新架构
- [ ] 端到端测试
- [ ] 性能优化
- [ ] 文档和教程编写

---

## 🎯 关键指标

| 指标 | 目标 | 预期改进 |
|-----|------|---------|
| 参数调优周期 | 3-5天 | -50% |
| 新功能集成 | 5个工作日 | +100% |
| 配置版本管理 | 标准化 | 显著 |
| 优化管线自动化 | >80% | 显著 |
| 系统可维护性 | 显著提升 | 30-40% |

---

## 📚 交付物

### 代码
- [ ] 完整的四层架构实现
- [ ] 配置管理系统
- [ ] 优化管线框架
- [ ] 单元测试和集成测试

### 文档
- [ ] 架构设计文档
- [ ] API 参考文档
- [ ] 配置指南
- [ ] 优化流程指南

### 工具
- [ ] 配置CLI工具
- [ ] 参数对比工具
- [ ] 版本管理工具

---

## 🚀 立即启动计划

### Day 1 (今天): 架构设计阶段
- [ ] 详细设计四层接口
- [ ] 定义关键 DTO (Signal, Position, Order 等)
- [ ] 规划目录结构

### Day 2-3: 框架实现
- [ ] 创建基础模块结构
- [ ] 实现核心类和接口
- [ ] 建立单元测试框架

### Day 4-5: 集成测试
- [ ] 集成现有代码
- [ ] 端到端测试
- [ ] 性能验证

---

**Next Step**: 确认架构设计，启动实施

**Estimated Duration**: 4-5 weeks
**Team Size**: 1 (可扩展)
**Risk Level**: 低 (基于已清理的代码基础)
