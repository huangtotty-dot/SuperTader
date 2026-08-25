# 系统级优化工程计划 - 2026-08-25

## 📌 工程目标

1. **清理冗余**：消除重复的文档、代码、配置；整理过期信息
2. **架构升级**：从实验性堆叠向清晰分层架构转变
3. **可持续性**：建立持续优化的框架、工具和流程

**预计工期**：3-4周（并行推进）  
**优先级**：高（影响后续所有开发）

---

## 第一阶段：文档清理与组织（1周）

### 现状分析

```
根目录：35份Markdown文档 (~250KB)
├── 方案报告 (25份) → 过时、方案演进残留
│   ├── COMPREHENSIVE_IMPLEMENTATION_PLAN.md (19KB) - 参数优化方案
│   ├── SCHEME_A_RISK_FIXES.md (14KB) - 旧方案
│   ├── P24-02_DEEP_WATER_IMPLEMENTATION.md (11KB) - 过期
│   ├── 其他临时报告 (6-10KB×多) ...
│
├── 快速参考 (3份) → 重复、更新滞后
│   ├── QUICK_REFERENCE_GUIDE.md (6KB)
│   ├── QUICK_REFERENCE.md (2.9KB) - 更旧的版本
│   └── ANSWER_QUICK.md (1.6KB) - 更旧的版本
│
├── 索引和清单 (4份) → 维护负担
│   ├── DOCUMENT_INDEX.md (9.3KB) - 元文档
│   ├── NEXT_STEPS_CHECKLIST.md (5.8KB)
│   ├── DECISION_CHECKLIST.md (3.3KB)
│   └── plan.md (1.6KB)
│
├── 特定功能指南 (3份) → 分散到doc目录更好
│   ├── INTRADAY_SURGE_DEFENSE_GUIDE.md (7.8KB)
│   ├── INTRADAY_SURGE_DEFENSE_QUICKSTART.md (6KB)
│   ├── K_LINE_BOX_DISPLAY_GUIDE.md (6.2KB)
│
└── 其他报告、总结、实施指南 (约20份) ...

doc目录：18份文档（周复盘+日复盘）
├── 每周复盘/ (7份) - 保留，需整理
├── 每日复盘/ (8份) - 保留（最近7日），历史归档
└── 其他方案文档 (3份) - 需评估
```

### 任务分解

#### 1.1 根目录文档分类与评审（1-2天）

**分类标准**：

| 分类 | 处理方式 | 文件数 | 大小 |
|------|---------|-------|------|
| **核心保留** | 保持在根目录 | 3-5 | |
| **功能指南** | 迁移到 `doc/guides/` | 5-6 | ~30KB |
| **方案报告** | 迁移到 `doc/archive/proposals/` | 15-20 | ~100KB |
| **临时清单** | 归档到 `doc/archive/checklists/` | 4-5 | ~15KB |
| **过时文档** | 删除 | 3-5 | ~20KB |

**保留在根目录的文档**：
- ✅ `README.md` - 主入口，定期更新
- ✅ `CLAUDE.md` - 工程管理和指导（如存在）
- ✅ `CURRENT_IMPLEMENTATION.md` (新建) - 当前实施状态快速参考
- ❓ `SYSTEM_OPTIMIZATION_PLAN.md` - 本计划文档（完成后可归档）

**删除的文档**（>30天未更新且为过时方案）：
- `PLAN_A_*.md`, `SCHEME_B_*.md` 等历史方案
- 重复的快速参考版本（只保留最新的一份）
- 无实施价值的分析报告

#### 1.2 建立doc目录新结构（1天）

```
doc/
├── 📖 README.md                          # doc入口导航
├── 🚀 guides/                            # 实施指南
│   ├── 日内交易信号.md
│   ├── 建仓策略.md
│   ├── GUI操作手册.md
│   └── ...
├── 📊 复盘/
│   ├── 周复盘/
│   │   ├── 2026-W34.md
│   │   └── ...
│   ├── 日复盘/
│   │   ├── 2026-08-25.md
│   │   └── ...
│   └── 历史归档/
│       ├── 2026-W30-W33/
│       └── 2026-08-01-08-14/
├── 🏗️ architecture/                       # 架构设计（新增）
│   ├── 系统架构设计.md
│   ├── 模块依赖图.md
│   └── ...
├── 🔧 solutions/                         # 解决方案（新增）
│   ├── 参数优化方案/
│   │   ├── L2_L3参数方案.md
│   │   └── 效果对比.md
│   ├── GUI改进方案/
│   │   └── ...
│   └── ...
└── 📦 archive/
    ├── proposals/                        # 历史方案（分类）
    │   ├── 2026-08-参数优化/
    │   ├── 2026-07-架构探索/
    │   └── ...
    ├── checklists/                       # 历史清单
    ├── experiments/                      # 历史实验
    └── 归档说明.md
```

#### 1.3 迁移和更新（1-2天）

**步骤**：
1. 创建新目录结构
2. 按分类移动文档到对应目录
3. 更新内部链接（Markdown引用）
4. 创建统一的导航入口
5. 删除冗余文件，git commit

**新建的核心文档**：

- `doc/README.md` - 文档体系总览
- `doc/CURRENT_STATUS.md` - 系统当前状态（自动更新）
- `CURRENT_IMPLEMENTATION.md` (根目录) - 当前实施快速查阅

---

## 第二阶段：代码清理与结构化（2周）

### 现状分析

```
Python文件结构（~176个.py文件）：

project_root/
├── 核心业务代码
│   ├── main.py ✅
│   ├── signal_engine.py ✅
│   ├── position_builder.py ✅
│   ├── position_sizer.py ✅
│   ├── indicators.py ✅
│   ├── config.py ✅
│   ├── t_gui.py ✅
│   └── 其他核心模块 (5-8个)
│
├── 工具和utility
│   ├── data_fetcher.py ✅
│   ├── support_resistance.py
│   ├── trend_regime.py
│   ├── market_regime.py
│   ├── index_regime_*.py (多个变体)
│   ├── replay_day.py
│   ├── harness_backtest.py
│   └── 其他 ...
│
├── stock_hunter/ 子系统
│   ├── main.py
│   ├── modules/
│   │   ├── data_loader.py
│   │   ├── heat_tracker.py
│   │   ├── ranker.py
│   │   ├── reporter.py
│   │   ├── styler.py
│   │   ├── validator.py
│   │   ├── push_feishu.py
│   │   └── __init__.py
│   └── ...
│
└── t_io/validation/ 测试和验证
    ├── _archive/ (296KB - 归档验证脚本)
    │   ├── analyze_*.py (多个)
    │   ├── run_*.py (多个)
    │   ├── merge_*.py (多个)
    │   ├── smoke_*.py (多个)
    │   └── ...
    │
    ├── daily_review/
    │   ├── daily_review.py ✅ (核心验证脚本)
    │   ├── forward_tracker.py ✅
    │   ├── fetch_*.py (3-4个)
    │   └── ...
    │
    ├── e1_threshold/ (离线扫描)
    ├── e2_daily_gate/ (日线验证)
    ├── w33_*/ (W33专项验证)
    ├── w34_*/ (W34专项验证)
    ├── rsi_nan_guard/ (RSI修复)
    ├── attr_*/  (特定股票分析)
    └── ...
```

### 任务分解

#### 2.1 审视验证代码（3-4天）

**目标**：确定`_archive`中哪些脚本可以安全删除

**分类方式**：

| 分类 | 处理 | 说明 |
|-----|------|------|
| **Dead Code** | 删除 | 已过期的实验，无替代者依赖 |
| **Reference Only** | 保留到refs/ | 可能有学习或历史价值 |
| **Active** | 保留 | 仍在使用或需要 |
| **Duplicated** | 合并或删除 | 多个相似脚本合并为一个 |

**具体审视清单**：
- [ ] `analyze_ab_*.py` - 多个A/B测试分析脚本 → 是否都需要？
- [ ] `run_*.py` / `merge_*.py` - 多个变体运行脚本 → 是否有相同逻辑？
- [ ] `smoke_*.py` - 多个版本 → 保留最新一个？
- [ ] `probe_*.py` 系列 - 多个调试脚本 → 清理或整合？
- [ ] `det_*.py` 系列 - 确定性测试 → 是否都有效？

**输出物**：
- 决策文档：`doc/archive/validation_cleanup_decision.md`
- 要删除的文件列表
- 要保留/引用的文件分类

#### 2.2 识别重复和冗余代码（3-4天）

**扫描重复的指标/工具**：
- `index_regime*.py` 的多个变体
- 多个 signal_engine 版本
- 多个数据获取实现

**使用工具**：
```bash
# 伪代码，实际需要编写脚本
find . -name "*.py" -type f | xargs wc -l | sort -rn  # 查看大文件
# 使用统计工具找重复代码
```

**处理方式**：
1. 确认是否需要多个版本（例如不同的实验变体）
2. 如果是，建立清晰的命名规范和归档策略
3. 如果不是，统一成一份核心实现，旧版本移到`_deprecated/`

#### 2.3 建立清晰的模块划分（2-3天）

**目标**：将散落的代码按功能重新组织

**建议的新结构**：

```
superTrader/
├── core/                          # 核心交易引擎
│   ├── signal_engine.py           # 信号生成
│   ├── position_builder.py        # 建仓逻辑
│   ├── position_sizer.py          # 仓位计算
│   └── execution.py               # 执行层（新）
│
├── data/                          # 数据处理层
│   ├── fetcher.py                 # 行情获取
│   ├── cache.py                   # 缓存管理
│   ├── snapshots.py               # 快照存储
│   └── __init__.py
│
├── indicators/                    # 技术指标（新）
│   ├── base.py                    # 基础指标
│   ├── momentum.py                # 动量类
│   ├── trend.py                   # 趋势类
│   └── __init__.py
│
├── analysis/                      # 市场分析（新）
│   ├── regime.py                  # 市场态势
│   ├── correlation.py             # 关联性分析
│   └── __init__.py
│
├── strategies/                    # 策略（新）
│   ├── intraday/                  # 日内策略
│   │   ├── swing2pt.py            # 两点规则
│   │   └── __init__.py
│   ├── position/                  # 建仓策略
│   │   ├── dual_channel.py        # 双通道
│   │   └── __init__.py
│   └── __init__.py
│
├── gui/                           # GUI相关
│   ├── app.py                     # 主应用
│   ├── renderer.py                # 渲染
│   └── __init__.py
│
├── utils/                         # 通用工具
│   ├── config.py                  # 配置管理
│   ├── logger.py                  # 日志
│   ├── feishu.py                  # 飞书集成
│   └── __init__.py
│
├── hunter/                        # stock_hunter子系统
│   ├── main.py
│   ├── modules/
│   └── ...
│
├── validation/                    # 验证框架（新）
│   ├── daily_review.py            # 日复盘逻辑
│   ├── forward_tracker.py         # 前瞻跟踪
│   ├── backtester.py              # 回测框架
│   └── __init__.py
│
├── tests/                         # 测试（新）
│   ├── test_signal_engine.py
│   ├── test_position_builder.py
│   └── ...
│
├── main.py                        # 实盘主入口
├── replay_day.py                  # 单日回放入口
├── config.py                      # 全局配置
└── __init__.py
```

**关键重构**：
1. `indicators.py` → `indicators/`（拆成多个文件）
2. 根目录散落的tools → `utils/`
3. `stock_hunter/` 保持独立子系统
4. 验证脚本 → `validation/`（核心脚本+脚本库）
5. 清理`t_io/validation/`，保留only核心验证脚本

---

## 第三阶段：架构优化与可持续框架（2-3周）

### 3.1 核心架构设计

**四层架构**：

```
┌──────────────────────────────────────┐
│  GUI / API / 人工决策                 │  (执行层接口)
├──────────────────────────────────────┤
│  EXECUTION (订单/风控/撤销)           │  执行层
├──────────────────────────────────────┤
│  SIGNAL + POSITION (决策逻辑)         │  决策层
│  ├─ SignalEngine (日内两点)
│  ├─ PositionBuilder (双通道建仓)
│  └─ PositionSizer (仓位计算)
├──────────────────────────────────────┤
│  ANALYSIS (市场分析)                  │ 分析层
│  ├─ Indicators (技术指标)
│  ├─ Regime (市场态势)
│  └─ Correlation (关联性)
├──────────────────────────────────────┤
│  DATA (行情+状态)                     │ 数据层
│  ├─ Fetcher (实时行情)
│  ├─ Cache (行情缓存)
│  ├─ Snapshots (历史快照)
│  └─ State (系统状态)
└──────────────────────────────────────┘
```

**关键设计原则**：
- 层间通信通过定义明确的接口/DTO
- 每层可独立测试、更新、优化
- 信号决策与执行解耦（便于纸面交易或延迟执行）
- 配置集中管理（Config中心）

### 3.2 配置管理体系（新建）

**目标**：统一参数管理，便于A/B测试和版本管理

**结构**：

```
config/
├── defaults.py                    # 全局默认参数
├── strategies/
│   ├── swing2pt.yaml              # 日内两点参数
│   ├── dual_channel.yaml          # 双通道建仓参数
│   └── single_tier.yaml           # 单档加仓参数
├── market/
│   ├── index_levels.yaml          # 大盘阈值
│   └── individual_stock.yaml      # 个股参数
├── system/
│   ├── data_fetch.yaml            # 数据获取配置
│   ├── execution.yaml             # 执行配置
│   └── logging.yaml               # 日志配置
└── versions/                      # 版本控制
    ├── v2_swing2pt.yaml           # 2026-08-13版本
    ├── v1.2.yaml                  # 历史版本
    └── ...
```

**API**：

```python
# 统一配置访问接口
from config import ConfigManager

cfg = ConfigManager()
cfg.load_version("v2_swing2pt")      # 加载某个版本
cfg.get("signal.swing_bb_upper")      # 获取参数
cfg.set("signal.swing_bb_upper", 1.0) # 设置参数
cfg.validate()                         # 验证参数合法性
cfg.save_snapshot("exp_2026_08_25")   # 保存快照
```

### 3.3 持续优化框架（新建）

**目标**：建立系统化的参数优化、验证、回滚流程

**三层验证管线**：

```
参数变更
  ↓
[离线验证] - 历史数据回测 (1小时)
  ├─ 收益预期 (fwd5 目标)
  ├─ 覆盖率 (最小阈值)
  └─ 风险指标 (最大下行)
  ↓ (✅通过则进入下一层)
[实盘观察] - 观察期 3-5天 (模拟/小仓位)
  ├─ 信号质量 (假阳性率、胜率)
  ├─ 执行效果 (成交价格偏差)
  └─ 系统稳定性 (错误/卡顿)
  ↓ (✅通过则进入下一层)
[灰度上线] - 逐步扩大 (1-2周)
  ├─ 先扩到全部跟踪股票
  ├─ 逐日扩大仓位比例
  └─ 持续监控关键指标
  ↓ (✅稳定则正式发布)
[正式版本] - 标记版本，长期跟踪
  ├─ 记录上线日期和参数
  ├─ 持续对标历史版本
  └── 定期复盘效果
```

**实现工具**：

```python
# 优化框架
class OptimizationPipeline:
    def offline_validate(self, config, historical_data):
        """离线验证"""
        
    def paper_trade(self, config, days=5):
        """纸面交易验证"""
        
    def canary_deploy(self, config, pct=0.1):
        """灰度部署"""
        
    def rollback(self, version):
        """快速回滚"""

# 指标追踪
class MetricsTracker:
    def record_signal(self, code, signal_type, metrics):
        """记录信号质量"""
        
    def report_daily(self, date):
        """日报告"""
        
    def compare_versions(self, v1, v2, period):
        """版本对比"""
```

**对应文档**：
- `doc/architecture/optimization_pipeline.md` - 流程说明
- `doc/solutions/parameter_versioning.md` - 版本管理

---

## 实施时间表

| 周 | 第一阶段（文档） | 第二阶段（代码） | 第三阶段（架构） | 状态 |
|----|---------------|---------------|---------------|------|
| W35 (8.25-8.31) | 1.1 评审文档 | 2.1 审视验证代码 | - | 进行中 |
| W36 (9.01-9.07) | 1.2 目录重组 + 1.3 迁移 | 2.2 识别冗余代码 | 3.1 架构设计 | 计划中 |
| W37 (9.08-9.14) | ✅ 完成 | 2.3 代码重构 | 3.2 配置系统 | 计划中 |
| W38 (9.15-9.21) | - | ✅ 完成 | 3.3 优化框架 | 计划中 |
| W39+ | - | - | ✅ 完成 + 验证 | 计划中 |

---

## 关键决策点

### D1：是否删除验证_archive？

**当前状态**：296KB 旧验证脚本  
**选项**：
- A) 全部删除 (激进，节省空间但可能丢失参考)
- B) 部分保留到 `refs/` 作为参考 (保留学习价值)
- C) 全部保留，只重新组织 (保守，维护负担大)

**建议**：B (选择性保留有文档的重要实验)

### D2：是否立即重构代码？

**当前状态**：核心代码能工作但散落  
**选项**：
- A) 立即大重构 (时间成本高，易出bug)
- B) 渐进式重构 (新代码用新结构，旧代码后迁移)
- C) 维持现状 (完成第一二阶段后再做)

**建议**：B (新增模块直接使用新结构，逐步迁移)

### D3：文档归档多久的算过期？

**建议**：
- 方案文档：>30天无更新 + 无对应代码在使用
- 复盘数据：>2周自动归档到历史
- 清单：完成后立即归档

---

## 预期收益

### 文档层面
✅ 清晰的文档导航，新手10分钟了解系统  
✅ 无过期信息干扰，查找时间 30% → 10%  
✅ 维护负担减少（自动生成清单）  

### 代码层面
✅ 模块依赖清晰，修改影响范围易识别  
✅ 冗余代码消除，同样功能代码量 -20%  
✅ 新功能开发效率 +30% (清晰的位置和接口)  

### 架构层面
✅ 参数变更有标准流程，减少线上事故  
✅ 持续优化自动化，参数调优周期 -50%  
✅ 新策略集成快速，5个工作日从想法到实盘验证  

---

## 执行方式

**逐周迭代**：每周一份commit，记录阶段性进展  
**风险控制**：大重构前必须有充分的自动化测试  
**反馈循环**：每周末评估进度，必要时调整计划  

---

