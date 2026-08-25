# Phase 4 实现指南

## 📋 目录
1. [快速开始](#快速开始)
2. [项目结构详解](#项目结构详解)
3. [配置系统使用](#配置系统使用)
4. [测试和质量检查](#测试和质量检查)
5. [CI/CD 工作流](#cicd-工作流)
6. [代码迁移路线图](#代码迁移路线图)
7. [常见问题](#常见问题)

---

## 快速开始

### 1. 环境设置

```bash
# 进入项目目录
cd /e/superTrader

# 创建虚拟环境 (可选但推荐)
python -m venv venv
source venv/Scripts/activate  # Windows: venv\Scripts\activate

# 安装项目及开发依赖
pip install -e .[dev]

# 验证安装
python -c "import src; print('✓ superTrader 已安装')"
```

### 2. 首次测试运行

```bash
# 运行所有测试
pytest src/tests/ -v

# 运行特定测试
pytest src/tests/unit/test_config.py -v

# 带覆盖率报告
pytest src/tests/ --cov=src --cov-report=html
```

### 3. 代码检查

```bash
# 格式检查
black src/ --check

# 代码规范检查
pylint src/

# 类型检查
mypy src/

# 一键检查所有
bash -c "black src/ && pylint src/ && pytest src/tests/"
```

---

## 项目结构详解

### 核心架构图

```
superTrader
│
├── src/                        # 新的源代码目录
│   ├── core/                   # ⭐ 核心引擎层
│   │   ├── data/              # 数据获取和转换
│   │   │   ├── fetcher.py     # wrapper -> root data_fetcher.py
│   │   │   └── __init__.py
│   │   ├── analysis/          # 技术分析
│   │   │   ├── indicators.py  # 指标计算
│   │   │   ├── regimes.py     # 市场状态分析
│   │   │   ├── resonance.py   # 共鸣指标
│   │   │   └── __init__.py
│   │   └── state/             # 状态管理
│   │       └── __init__.py
│   │
│   ├── strategies/             # ⭐ 策略层
│   │   ├── signals.py         # 信号生成
│   │   ├── entry.py           # 入场策略
│   │   ├── timing.py          # 时间优化
│   │   └── __init__.py
│   │
│   ├── execution/              # ⭐ 执行层
│   │   ├── builder.py         # 头寸构建
│   │   ├── sizer.py           # 头寸大小
│   │   ├── gates.py           # 风险门槛
│   │   └── __init__.py
│   │
│   ├── ui/                     # ⭐ 用户界面层
│   │   ├── gui.py             # GUI 主程序
│   │   ├── reports.py         # 报告生成
│   │   └── __init__.py
│   │
│   ├── utils/                  # ⭐ 工具层
│   │   ├── constants.py       # 全局常量
│   │   ├── helpers.py         # 辅助函数
│   │   └── __init__.py
│   │
│   ├── config/                 # ⭐ 配置层
│   │   ├── schema.py          # Pydantic 模型
│   │   └── __init__.py
│   │
│   ├── tests/                  # 测试套件
│   │   ├── unit/              # 单元测试
│   │   ├── integration/       # 集成测试
│   │   └── __init__.py
│   │
│   └── main.py                 # 入口程序
│
├── src/ (root level)           # 保留旧文件以保证向后兼容
│   ├── *.py (150+ 文件)
│   └── ...
│
├── .github/
│   └── workflows/
│       └── test.yml            # CI/CD 工作流
│
├── setup.py                    # 包配置
├── pyproject.toml              # 工具配置
├── pytest.ini                  # 测试配置
├── requirements.txt            # 依赖清单
└── README.md
```

### 分层设计原理

| 层级 | 职责 | 关键模块 | 输入/输出 |
|-----|------|--------|---------|
| **Data** | 数据获取 | fetcher.py | API → DataFrame |
| **Analysis** | 技术分析 | indicators.py | OHLC → 指标值 |
| **Strategies** | 信号生成 | signals.py | 指标 → 买卖信号 |
| **Execution** | 头寸管理 | builder.py | 信号 → 实际持仓 |
| **UI** | 用户交互 | gui.py | 数据 → 可视化 |

---

## 配置系统使用

### 配置模型概览

Pydantic 模型位于 `src/config/schema.py`，提供类型安全的配置：

#### 基础使用

```python
from src.config.schema import SystemConfig, RiskConfig

# 创建配置
config = SystemConfig(
    version="1.0.0",
    environment="development",
    debug=False,
    risk=RiskConfig(
        max_position_size=10.0,
        stop_loss_percent=2.0,
        take_profit_percent=5.0
    )
)

# 导出为 JSON
config.to_json("config.json")

# 从 JSON 加载
config = SystemConfig.from_json("config.json")

# 转换为字典
config_dict = config.to_dict()
```

### 配置模型详解

#### 1. DataSourceConfig (数据源)

```python
from src.config.schema import DataSourceConfig

data = DataSourceConfig(
    provider="akshare",           # 数据提供商
    cache_enabled=True,           # 启用缓存
    cache_ttl_hours=24,          # 缓存 TTL
    retry_attempts=3              # 重试次数
)
```

**参数限制:**
- `cache_ttl_hours`: 1-240 小时
- `retry_attempts`: 1-10 次

#### 2. IndicatorConfig (指标配置)

```python
from src.config.schema import IndicatorConfig

indicators = IndicatorConfig(
    ma_periods=[5, 10, 20, 60],   # 移动平均周期
    rsi_period=14,                # RSI 周期
    macd_fast=12,                 # MACD 快线
    macd_slow=26,                 # MACD 慢线
    macd_signal=9,                # MACD 信号线
    bb_period=20,                 # 布林带周期
    bb_std_dev=2.0                # 布林带标准差
)
```

#### 3. RiskConfig (风险管理)

```python
from src.config.schema import RiskConfig, RiskProfileEnum

risk = RiskConfig(
    max_position_size=10.0,          # 最大头寸 %
    stop_loss_percent=2.0,           # 止损 %
    take_profit_percent=5.0,         # 止盈 %
    max_daily_loss=5.0,              # 最大日损 %
    profile=RiskProfileEnum.MODERATE # 风险等级
)
```

**风险等级:**
- `CONSERVATIVE`: 保守 (1-5%)
- `MODERATE`: 温和 (5-10%)
- `AGGRESSIVE`: 激进 (10-15%)

#### 4. SignalConfig (信号配置)

```python
from src.config.schema import SignalConfig

signals = SignalConfig(
    enabled=True,                      # 启用信号
    min_strength=0.5,                  # 最小强度 (0-1)
    confirmation_periods=2,            # 确认周期
    filters=["divergence", "momentum"] # 应用过滤器
)
```

#### 5. BacktestConfig (回测配置)

```python
from src.config.schema import BacktestConfig

backtest = BacktestConfig(
    enabled=True,
    start_date="2024-01-01",           # 开始日期
    end_date="2024-12-31",             # 结束日期
    initial_capital=100000.0,          # 初始资金
    commission_percent=0.001,          # 佣金率
    slippage_percent=0.002             # 滑点率
)
```

#### 6. SystemConfig (主配置)

```python
from src.config.schema import SystemConfig, MarketRegimeEnum

config = SystemConfig(
    version="1.0.0",
    environment="production",          # dev/prod/backtest
    debug=False,
    log_level="INFO",                  # DEBUG/INFO/WARNING/ERROR
    
    # 子配置
    data=data_config,
    indicators=indicator_config,
    signals=signal_config,
    risk=risk_config,
    backtest=backtest_config,
    
    # 市场状态
    market_regime=MarketRegimeEnum.BULL,
    
    # 自定义参数
    custom_parameters={
        "debug_symbol": "000858",
        "max_retries": 5
    }
)
```

### 配置验证

```python
from src.config.schema import validate_config
import json

# 从字典验证
config_dict = {
    "version": "1.0.0",
    "environment": "production",
    "risk": {
        "max_position_size": 15.0,    # 自动验证范围
        "profile": "moderate"
    }
}

try:
    config = validate_config(config_dict)
    print("✓ 配置有效")
except ValueError as e:
    print(f"✗ 配置错误: {e}")
```

---

## 测试和质量检查

### 测试框架

项目使用 **pytest** 作为主测试框架，支持：
- ✅ 单元测试 (`src/tests/unit/`)
- ✅ 集成测试 (`src/tests/integration/`)
- ✅ 代码覆盖率报告
- ✅ 并行执行

### 运行测试

```bash
# 运行所有测试
pytest src/tests/ -v

# 运行特定类别
pytest src/tests/unit/ -v
pytest src/tests/integration/ -v

# 运行特定测试
pytest src/tests/unit/test_config.py::TestSystemConfig -v

# 带覆盖率
pytest src/tests/ --cov=src --cov-report=html --cov-report=term-missing

# 并行执行 (更快)
pytest src/tests/ -n auto
```

### 标记测试

```python
import pytest

# 标记为单元测试
@pytest.mark.unit
def test_config_validation():
    pass

# 标记为集成测试
@pytest.mark.integration
def test_data_fetcher_integration():
    pass

# 标记为慢速测试
@pytest.mark.slow
def test_long_running_backtest():
    pass

# 标记为市场数据测试
@pytest.mark.market
def test_live_data_fetch():
    pass
```

### 质量检查工具

#### 1. Black (代码格式化)

```bash
# 检查格式
black src/ --check

# 自动格式化
black src/

# 指定行长
black src/ --line-length=100
```

#### 2. Pylint (代码规范)

```bash
# 检查代码规范
pylint src/

# 输出评分
pylint src/ --exit-zero

# 指定配置
pylint src/ --rcfile=.pylintrc
```

#### 3. MyPy (类型检查)

```bash
# 类型检查
mypy src/

# 忽略缺失导入
mypy src/ --ignore-missing-imports

# 详细输出
mypy src/ --show-error-codes --pretty
```

#### 4. 一键检查

```bash
# 创建 pre-commit hook
cat > .git/hooks/pre-commit << 'EOF'
#!/bin/bash
set -e

echo "🔍 Running code checks..."

echo "  - Formatting with black..."
black src/

echo "  - Checking with pylint..."
pylint src/ --exit-zero

echo "  - Type checking with mypy..."
mypy src/ --ignore-missing-imports

echo "  - Running tests..."
pytest src/tests/ --tb=short -q

echo "✓ All checks passed!"
EOF

chmod +x .git/hooks/pre-commit
```

---

## CI/CD 工作流

### GitHub Actions 配置

工作流文件: `.github/workflows/test.yml`

**自动触发条件:**
- 🔵 推送到 `main`, `develop`, `refactor/*`
- 🟣 Pull Request 到 `main` 或 `develop`
- 🟡 每日定时 (UTC 2 AM)

### 工作流作业

#### 1. Test Job (测试)

**运行环境:** Ubuntu + Python 3.9/3.10/3.11

**步骤:**
1. 检出代码
2. 安装 Python 和依赖
3. **代码检查** (Black, Pylint, MyPy)
4. **运行测试** (pytest + 覆盖率)
5. **上传到 Codecov**

#### 2. Security Job (安全)

**步骤:**
1. Bandit - 安全漏洞扫描
2. Safety - 依赖漏洞检查

#### 3. Build Docs Job (文档)

**步骤:**
1. Sphinx 文档构建
2. 生成 HTML 文档

### 本地模拟 CI/CD

```bash
# 模拟完整 CI 流程
bash -c "
  echo '📦 Installing dependencies...'
  pip install -e .[dev]
  
  echo '🔍 Code checks...'
  black src/ --check
  pylint src/ --exit-zero
  mypy src/ --ignore-missing-imports
  
  echo '🧪 Running tests...'
  pytest src/tests/ -v --cov=src --cov-report=term-missing
  
  echo '✓ CI simulation complete!'
"
```

---

## 代码迁移路线图

### Phase 4.1: 基础设置 (✅ 已完成)

- ✅ 创建标准目录结构
- ✅ 设置 Pydantic 配置系统
- ✅ 建立 CI/CD 工作流
- ✅ 添加样本测试

### Phase 4.2: 核心迁移 (2-3 周)

**目标:** 迁移核心数据和分析模块

```python
# 旧方式
from data_fetcher import fetch_data
from indicators import calculate_ma

# 新方式
from src.core.data.fetcher import fetch_data
from src.core.analysis.indicators import calculate_ma
```

**迁移步骤:**
1. 将 `data_fetcher.py` 复制到 `src/core/data/`
2. 更新 wrapper 模块导入
3. 添加类型注解
4. 编写单元测试
5. 更新主程序导入

### Phase 4.3: 策略迁移 (2-3 周)

**目标:** 迁移策略和信号生成模块

```python
# 新方式
from src.strategies.signals import generate_signals
from src.strategies.entry import PreciseEntryFramework
```

**迁移步骤:**
1. 将策略模块移到 `src/strategies/`
2. 重构为类基设计
3. 添加配置参数
4. 编写集成测试

### Phase 4.4: 执行迁移 (2-3 周)

**目标:** 迁移执行层 (头寸管理、风险控制)

```python
# 新方式
from src.execution.builder import PositionBuilder
from src.execution.gates import IntraDayRiskGate
```

### Phase 4.5: UI 和主程序 (1-2 周)

**目标:** 更新 UI 和主入口

```python
# src/main.py - 新的入口点
from src.config.schema import SystemConfig
from src.core.data.fetcher import DataFetcher
from src.strategies.signals import SignalEngine
from src.execution.builder import PositionBuilder
```

### Phase 4.6: 清理和优化 (1 周)

- 移除根目录旧文件 (保留备份)
- 完整的测试覆盖
- 性能优化
- 最终文档

---

## 常见问题

### Q1: 旧代码还能用吗？

**A:** 是的！所有根目录的 Python 文件继续工作，新的 `src/` 模块是 wrapper，自动转发导入。

```python
# 这两种方式都有效
from data_fetcher import fetch_data           # 旧方式
from src.core.data.fetcher import fetch_data  # 新方式
```

### Q2: 如何导入新模块？

**A:** 使用相对于 `src/` 的路径：

```python
# 推荐
from src.core.data import fetcher
from src.strategies.signals import generate_signals

# 或具体导入
from src.config.schema import SystemConfig
```

### Q3: 如何运行本地测试？

**A:** 使用 pytest：

```bash
# 运行所有测试
pytest src/tests/ -v

# 运行特定测试文件
pytest src/tests/unit/test_config.py -v

# 带覆盖率
pytest src/tests/ --cov=src
```

### Q4: 配置错误怎么调试？

**A:** 使用 Pydantic 的详细错误信息：

```python
from src.config.schema import SystemConfig

try:
    config = SystemConfig(
        version="1.0.0",
        risk={"max_position_size": 100}  # 超出范围 (max 50)
    )
except ValueError as e:
    print(f"配置错误: {e}")
    # 输出会显示确切的约束条件
```

### Q5: 如何添加新的配置参数？

**A:** 编辑 `src/config/schema.py`：

```python
class SystemConfig(BaseModel):
    # ... 现有字段 ...
    
    # 添加新字段
    new_parameter: str = Field(
        default="default_value",
        description="新参数的描述"
    )
    
    # 添加验证
    @validator('new_parameter')
    def validate_new_parameter(cls, v):
        if len(v) < 3:
            raise ValueError('必须至少 3 个字符')
        return v
```

### Q6: CI/CD 工作流失败怎么办？

**A:** 检查这些常见问题：

1. **测试失败**: 运行 `pytest src/tests/ -v` 查看错误
2. **代码格式**: 运行 `black src/` 自动修复
3. **类型错误**: 运行 `mypy src/` 查看错误位置
4. **安全问题**: 运行 `bandit -r src/` 检查漏洞

### Q7: 如何贡献新功能？

**A:** 遵循这个工作流：

```bash
# 1. 创建功能分支
git checkout -b feature/new-feature

# 2. 在 src/ 中编写代码
# 3. 添加测试 (src/tests/unit/)
# 4. 本地验证
pytest src/tests/ -v
black src/ && pylint src/

# 5. 提交和推送
git add .
git commit -m "feat: 添加新功能"
git push origin feature/new-feature

# 6. 创建 Pull Request
# CI/CD 会自动运行检查
```

---

## 快速命令参考

```bash
# 🚀 启动
pip install -e .[dev]                    # 安装项目

# 🧪 测试
pytest src/tests/ -v                      # 运行测试
pytest src/tests/ --cov=src               # 带覆盖率

# 🔍 检查
black src/                                # 格式化代码
pylint src/                               # 代码规范检查
mypy src/                                 # 类型检查

# 📊 覆盖率
pytest src/tests/ --cov=src --cov-report=html  # 生成 HTML 报告

# 📦 打包
python setup.py sdist bdist_wheel         # 构建分发包

# 🐛 调试
pytest src/tests/unit/test_config.py -v -s  # 显示输出

# 📝 文档
sphinx-build -b html doc/ doc/_build/     # 构建文档
```

---

## 下一步

1. ✅ **立即执行:**
   ```bash
   pip install -e .[dev]
   pytest src/tests/ -v
   ```

2. 📌 **本周内:**
   - 运行完整的 CI/CD 检查
   - 添加更多单元测试
   - 文档化核心 API

3. 📅 **本月内:**
   - 开始逐步迁移根目录文件
   - 完成配置系统集成
   - 达到 80%+ 测试覆盖

---

**Last Updated:** 2026-08-25  
**Status:** Ready for Phase 4 Implementation ✨
