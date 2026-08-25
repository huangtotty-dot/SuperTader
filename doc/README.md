# 📚 superTrader 文档导航

**最后更新**: 2026-08-25  
**维护者**: 系统优化工程

---

## 🚀 快速开始

### 新手必读（5-10分钟）
1. [当前实施状态](#当前系统状态) - 了解系统的当前运行配置
2. [核心概念](#核心架构) - 理解系统的四层设计
3. [快速命令](#常用命令) - 快速启动和调试

### 想要的帮助
- **"我想了解系统是怎么工作的"** → 查看 [系统架构](#核心架构)
- **"我要修改参数"** → 查看 [参数管理](#参数和配置)
- **"我要运行日复盘"** → 查看 [复盘和数据](#复盘和数据)
- **"我要查找历史方案"** → 查看 [历史资料](#历史资料)

---

## 📖 文档分类导航

### 核心架构
- [系统架构设计](architecture/) - 四层架构、模块划分、数据流
- [核心概念](architecture/CONCEPTS.md) - *(新增)* 关键概念解释

### 📋 实施指南
包含系统各模块的使用指南：

| 指南 | 内容 | 适合人群 |
|-----|------|---------|
| [日内交易信号](guides/INTRADAY_SURGE_DEFENSE_GUIDE.md) | 日内涨停防御系统 | 交易员 |
| [日内信号快速版](guides/INTRADAY_SURGE_DEFENSE_QUICKSTART.md) | 5分钟快速了解 | 时间紧张的人 |
| [精确入场框架](guides/PRECISE_ENTRY_GUIDE.md) | 建仓策略详解 | 系统研究员 |
| [Scheme A复盘](guides/SCHEME_A_DAILY_REVIEW_GUIDE.md) | 日评体系说明 | 复盘分析师 |
| [K线盒子显示](guides/K_LINE_BOX_DISPLAY_GUIDE.md) | GUI展示配置 | GUI开发者 |
| [GUI界面](guides/UI_INTEGRATION_GUIDE.md) | 前端集成指南 | 前端工程师 |
| [stock_hunter集成](guides/HUNTER_INTEGRATION_GUIDE_P2.md) | 猎人系统文档 | 后端工程师 |

### 📊 复盘和数据

#### 最近的复盘
- [最近周复盘](复盘/周复盘/) - 最新的一周回顾
- [最近日复盘](复盘/日复盘/) - 最近7日的日度回顾

#### 历史数据
- [历史复盘归档](复盘/历史归档/) - 按周/月组织的历史数据
- 使用场景：追溯历史决策、分析长期趋势

### 📦 历史资料

所有已完成的方案、实验、决策都在 [archive/](archive/) 中有序保存：

| 内容 | 位置 | 说明 |
|-----|------|------|
| **参数优化方案** | `archive/proposals/param_optimization_20260825/` | 2026-08-25的参数优化完整方案 |
| **Scheme A修复** | `archive/proposals/scheme_a_fixes_20260821/` | Scheme A的问题修复记录 |
| **GUI改进方案** | `archive/proposals/gui_improvement_20260825/` | GUI界面改进设计 |
| **其他方案** | `archive/proposals/` | 其他历史方案 |
| **完成清单** | `archive/checklists/` | 历史任务、决策清单 |
| **实验记录** | `archive/experiments/` | *(预留)* 历史实验数据 |

### 🔧 系统运维

- [系统优化工程计划](../SYSTEM_OPTIMIZATION_PLAN.md) - 长期优化路线图
- [优化快速启动指南](../OPTIMIZATION_QUICK_START.md) - Week-by-Week执行计划
- [优化决策详表](../PHASE1_DETAILED_DECISIONS.md) - 文档、代码清理的具体决策

---

## 💡 按用户角色查找

### 👨‍💼 产品/交易决策者
**目标**: 快速了解系统状态和最新决策

推荐阅读顺序:
1. [当前实施状态](#当前系统状态) (2min)
2. [最近的周复盘](复盘/周复盘/) (10min)
3. [最近的日复盘](复盘/日复盘/) (5min)

### 👨‍💻 工程师/系统开发
**目标**: 深入理解系统架构和代码组织

推荐阅读顺序:
1. [系统架构设计](architecture/) (20min)
2. 对应的实施指南 (15min)
3. 对应的源代码 (30min)

### 📊 数据分析师
**目标**: 查找验证结果和性能数据

推荐阅读顺序:
1. [最近复盘](复盘/日复盘/) (10min)
2. 对应的实验结果 (20min)
3. [历史资料](archive/) - 对比不同版本 (30min)

### 🔍 新成员/熟悉系统
**目标**: 从零开始了解整个系统

推荐阅读顺序:
1. [当前实施状态](#当前系统状态) (5min)
2. [核心架构](#核心架构) (15min)
3. 选择一个 [实施指南](guides/) (15-30min)
4. 查看对应源代码 (1小时)

---

## 🔍 按关键词快速查找

使用Ctrl+F查找关键词：

- **参数**: `param_optimization_20260825/`
- **日内防御**: `INTRADAY_SURGE_DEFENSE_GUIDE.md`
- **日内信号**: `guides/INTRADAY_SURGE_DEFENSE_QUICKSTART.md`
- **建仓**: `PRECISE_ENTRY_GUIDE.md` 或 `SCHEME_A_DAILY_REVIEW_GUIDE.md`
- **GUI**: `UI_INTEGRATION_GUIDE.md` 或 `K_LINE_BOX_DISPLAY_GUIDE.md`
- **复盘**: `复盘/` 目录
- **历史**: `archive/` 目录

---

## 📝 当前系统状态

**系统版本**: v2 swing2pt (2026-08-13)  
**最后更新**: 2026-08-25

### 核心模块
| 模块 | 状态 | 说明 |
|-----|------|------|
| 日内信号 | ✅ 稳定 | 纯两点规则 + RSI(6) |
| 建仓策略 | ⏳ 验收中 | 双通道（参考级） |
| 加仓策略 | ✅ 稳定 | 单档固定比例 |
| 日志数据层 | ✅ 完成 | 日复盘自动化 |

### 关键参数
```yaml
signal:
  swing_bb_upper: 1.0
  swing_bb_lower: 0.0
  swing_sell_rsi: 75
  swing_buy_rsi: 35
  rsi_period_5m_swing: 6
```

更详细的参数见: `archive/proposals/param_optimization_20260825/`

---

## 🎯 常用命令

```bash
# 实盘运行
python main.py

# 打开复盘看板
python t_gui.py

# 单日回放
python replay_day.py

# 建仓扫描
python position_builder.py --no-feishu

# 日复盘生成
python t_io/validation/daily_review/daily_review.py --date 2026-08-25

# 历史回测
python harness_backtest.py --codes 000988 --start 2026-07-24 --end 2026-07-24
```

更多命令见: [README.md](../README.md)

---

## ✅ 文档完整性

- [x] 快速开始指南
- [x] 实施指南（7份）
- [x] 复盘和数据
- [x] 历史资料
- [x] 系统优化计划
- [ ] *(即将推出)* 常见问题FAQ
- [ ] *(即将推出)* 故障排除指南

---

## 📞 获取帮助

- **找不到文档?** 用Ctrl+F搜索关键词，或查看 [按用户角色查找](#按用户角色查找)
- **文档过期了?** 请提出反馈，我们会及时更新
- **有新方案要添加?** 将文档放入 `archive/proposals/YYYY-MM-名称/` 并更新本导航

---

**导航更新日期**: 2026-08-25  
**下次计划更新**: 每周一更新最新复盘数据
