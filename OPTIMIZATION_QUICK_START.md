# 系统优化执行指南 - 快速启动

## 🎯 三个阶段概览

```
Week 1 (文档清理)
  ├─ 评审35份根目录文档，分类处理
  ├─ 建立 doc/ 新结构
  └─ 迁移和删除过期文档

Week 2-3 (代码清理)
  ├─ 审视 validation/_archive （296KB）
  ├─ 识别重复代码模块
  └─ 重构为清晰的模块结构

Week 4+ (架构升级)
  ├─ 建立四层架构
  ├─ 实现配置管理中心
  └─ 建立参数优化管线
```

## 📋 第一阶段详细任务清单

### 1.1 文档分类评审 (目标：2天)

**打开文件**：分析35份根目录MD文档

| 文档 | 大小 | 年份 | 是否需要 | 处理方式 |
|-----|------|------|---------|---------|
| README.md | 7.4K | 持续 | ✅ 保留 | 更新维护 |
| COMPREHENSIVE_IMPLEMENTATION_PLAN.md | 19K | 8.25 | 需评估 | → guides/ |
| SCHEME_A_RISK_FIXES.md | 14K | 历史 | ❌ 过期 | → archive/ |
| ... | | | | |

**执行**：
```bash
# 1. 按修改时间排序，看哪些是陈旧的
ls -lt *.md | tail -20

# 2. 检查引用（看是否有文档互相链接）
grep -l "LINK\|href\|README" *.md

# 3. 扫描内容，确定是历史还是活跃
head -20 SCHEME_*.md  # 看文件内容和日期
```

**分类结果示例**：
```
保留(根目录): README.md, CURRENT_IMPLEMENTATION.md
迁移到guides/: INTRADAY_SURGE_DEFENSE_*.md, K_LINE_BOX_DISPLAY_GUIDE.md
迁移到archive/proposals/: COMPREHENSIVE_IMPLEMENTATION_PLAN.md, 各种SCHEME_/PLAN_/*.md
删除: 重复的QUICK_REFERENCE版本, 无对应代码的分析文档
```

### 1.2 新建目录结构 (目标：1天)

```bash
# 创建新目录
mkdir -p doc/{guides,复盘/{周复盘,日复盘,历史归档},architecture,solutions,archive/{proposals,checklists,experiments}}

# 创建导航文件
touch doc/README.md doc/CURRENT_STATUS.md doc/archive/README.md
```

**doc/README.md 内容框架**：
```markdown
# 文档导航

## 🚀 快速开始
- [当前实施状态](CURRENT_STATUS.md)
- [系统架构](architecture/)
- [实施指南](guides/)

## 📊 复盘和回顾
- [最近周复盘](复盘/周复盘/)
- [最近日复盘](复盘/日复盘/)
- [历史数据](复盘/历史归档/)

## 🏗️ 系统和架构
- [系统设计文档](architecture/)
- [优化方案](solutions/)

## 📦 历史资料
- [存档的方案](archive/proposals/)
- [实验记录](archive/experiments/)
```

### 1.3 文档迁移 (目标：2天)

```bash
# 步骤1：备份
git status  # 确认当前状态

# 步骤2：移动文档
mv INTRADAY_SURGE_DEFENSE_*.md doc/guides/
mv SCHEME_*.md doc/archive/proposals/
mv PLAN_*.md doc/archive/proposals/
# ... etc

# 步骤3：更新内部链接
# 在MD文件中搜索和替换相对路径

# 步骤4：删除重复/过期文件
rm QUICK_REFERENCE.md ANSWER_QUICK.md  # 保留最新版本

# 步骤5：commit
git add . && git commit -m "refactor: 文档结构整理和清理 (第一阶段)"
```

---

## 📋 第二阶段详细任务清单

### 2.1 验证代码审视 (目标：3-4天)

```bash
# 扫描 _archive 目录结构
find t_io/validation/_archive -type f -name "*.py" | sort
wc -l t_io/validation/_archive/*.py  # 文件数量

# 按类别统计
ls -la t_io/validation/_archive/ | grep "^-" | wc -l
```

**关键决策**：

对于每一类脚本，判断：
- 是否有对应的"当前版本"在使用？
- 是否有文档记录其用途？
- 是否有其他脚本依赖它？

```python
# 示例决策矩阵
ANALYZE_AB_*.py:
  ✅ analyze_ab_expanded.py - 旧版本，有替代者 → 删除
  ✅ run_ab_threshold.py - 旧版本 → 删除
  → 结论：全部删除，核心逻辑已在其他脚本中

SMOKE_*.py:
  ✅ smoke_v110.py - 旧版本 → 删除
  ✅ smoke_v120_production.py - 当前使用 → 保留
  → 结论：只保留最新的smoke测试

PROBE_*.py:
  ✅ probe_0810.py - 调试脚本，无文档 → 删除
  → 结论：清理调试遗留
```

**输出物**：
创建 `doc/archive/validation_cleanup.md`:
```markdown
# 验证脚本清理决策

## 删除列表 (约80-100个文件, 200KB)
- analyze_ab_*.py (5个) - 已有替代逻辑
- run_v1*.py (10个) - 历史版本
- ... (其他)

## 保留列表 (核心脚本, 保留在 validation/)
- daily_review.py - 日复盘核心
- forward_tracker.py - 前瞻回填
- w33_c1p_*.py - 当前周期验证
- ...

## 参考库 (可选：归档到 refs/)
- 关键实验的smoke脚本 (如果有文档)
- 旧版本的回测脚本 (学习用)
```

### 2.2 重复代码识别 (目标：3-4天)

```bash
# 查找可能重复的文件
find . -type f -name "*.py" | xargs wc -l | sort -rn | head -30

# 查找import相同库的脚本（可能是重复实现）
grep -r "^import\|^from" --include="*.py" | cut -d: -f2 | sort | uniq -c | sort -rn
```

**重点关注的对象**：

```python
# 1. index_regime*.py 系列
index_regime.py              # 主版本
index_regime_intraday.py    # 日内版本
index_regime_v*.py          # 版本迭代？

# 2. signal_engine 系列
signal_engine.py            # 当前版本
signal_engine_v*.py?        # 历史版本？

# 3. 数据获取
data_fetcher.py            # 主版本
ts_fetch_*.py              # 旧版本？（在validation/_archive）

# 4. position builder 系列
position_builder.py         # 主版本
position_builder_v*.py?     # 版本？
```

**处理方案**：
```
如果发现重复：
  选项A: 合并逻辑，参数化区别（推荐）
  选项B: 创建 _deprecated/，标记过期
  选项C: 只保留一个，其他删除
```

### 2.3 代码重构到新结构 (目标：渐进式，2-3周)

**不立即做全局迁移**，而是：

1. **确立新结构** (core/, data/, indicators/, etc.)
2. **创建 __init__.py 统一接口**
3. **新增代码直接写到新位置**
4. **旧代码逐步迁移** (每天迁移1-2个关键模块)

```python
# 示例：逐步迁移 indicators

# 旧：根目录 indicators.py (500+ 行)
# 新：indicators/
#   ├── __init__.py        (导出统一接口)
#   ├── base.py            (基础指标)
#   ├── momentum.py        (RSI, MACD, etc.)
#   └── trend.py           (MA, Bollinger, etc.)

# __init__.py 提供向后兼容
from .base import *
from .momentum import *
from .trend import *

# 使用方无需改动
from indicators import RSI, MA  # 仍然工作
```

---

## 📋 第三阶段关键决策

### 3.1 架构设计方向

**四层架构确认** (Data → Analysis → Decision → Execution)

**关键类设计**：
```python
class SignalEngine:
    def evaluate(self, ohlcv_data) -> Signal
    
class PositionBuilder:
    def scan(self, market_data) -> BuildSignal
    
class PositionSizer:
    def calculate(self, signal, current_position) -> SizingAdvice
    
class Config:
    def load(self, version) -> dict
    def validate(self) -> bool
```

### 3.2 配置管理

**Yaml 还是 Python dict？**
- 推荐：Yaml (易于版本管理和人工编辑)
- 但保持Python接口 (ConfigManager)

**版本控制方案**：
```
config/versions/
├── v2_swing2pt_20260813.yaml      # 当前版本
├── v2_swing2pt_20260821_exp.yaml  # 实验版本
└── v1.2_legacy_20260801.yaml      # 历史版本
```

### 3.3 优化管线

**核心流程** (参数 → 离线验证 → 纸面 → 灰度 → 生产)

**关键工具**：
- `OptimizationPipeline` 类
- `MetricsTracker` 监控
- 自动回测脚本

---

## 🚀 立即开始

### Week 1 (现在)

**Day 1-2：文档评审**
```bash
# 克隆分支
git checkout -b refactor/system-optimization

# 分析文档
ls -lt *.md | head -20
grep -l "2026-08-25" *.md  # 看最近更新的

# 创建分类清单
touch doc/REFACTOR_CHECKLIST.md
```

**Day 3-4：目录创建**
```bash
mkdir -p doc/{guides,复盘,architecture,solutions,archive}
git add doc && git commit -m "chore: 创建新的doc结构"
```

**Day 5：首批迁移**
```bash
# 迁移指南类文档
mv INTRADAY_SURGE_DEFENSE_*.md doc/guides/
mv K_LINE_BOX_DISPLAY_GUIDE.md doc/guides/

# Commit
git commit -m "refactor: 迁移指南类文档到doc/guides/"
```

### Week 2-3 (继续)

**关键路径**：
1. 完成文档第一阶段 (1周)
2. 评审并清理 validation/_archive (1周)
3. 识别重复代码，建立清单 (1周)

---

## 📊 成功指标

- [ ] 根目录 Markdown ≤ 10个 (当前35个)
- [ ] validation/_archive 减少到 ≤50KB (当前296KB)
- [ ] 代码模块结构清晰 (core/, data/, indicators/ 等)
- [ ] 配置有版本控制
- [ ] 文档导航可用且最新

---

**下一步**：确认是否立即开始 Week 1 的工作？还是有其他优先级调整？

