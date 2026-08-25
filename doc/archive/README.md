# 📦 历史资料和存档指南

**最后更新**: 2026-08-25

这个目录包含所有已完成的方案、实验、决策和清单。资料按日期和主题分类，便于查阅和参考。

---

## 📂 目录结构

### proposals/ - 历史方案和设计
所有完成或存档的系统方案都保存在这里。按主题按日期分类。

#### 参数优化系列 (2026-08-25)
位置: `proposals/param_optimization_20260825/`

文档:
- `COMPREHENSIVE_IMPLEMENTATION_PLAN.md` - 参数优化的完整实施方案
- `PARAMETER_IMPLEMENTATION_REPORT.md` - 参数变更的详细报告
- `OPTUNA_OPTIMIZATION_PLAN.md` - Optuna参数搜索计划
- `OPTUNA_RESULT_REPORT.md` - 参数优化的结果

用途：参考参数优化的方法论和结果

#### Scheme A修复方案 (2026-08-21)
位置: `proposals/scheme_a_fixes_20260821/`

文档:
- `SCHEME_A_RISK_FIXES.md` - 风险修复详情
- `SCHEME_A_FIXES_IMPLEMENTATION.md` - 实施细节
- `BOX_BREAKOUT_FIX_REPORT.md` - 盒子突破修复
- `BOX_BREAKOUT_QUALITY_REVIEW.md` - 质量评审

用途：了解Scheme A的问题修复过程

#### GUI改进方案 (2026-08-25)
位置: `proposals/gui_improvement_20260825/`

文档:
- `GUI_IMPROVEMENT_REPORT.md` - 改进设计
- `GUI_LOADING_FIX.md` - 加载修复

用途：参考GUI界面改进的思路

#### 其他历史方案
- `DIRECTION_B_PLAN.md` - B方向探索
- `P24-02_DEEP_WATER_IMPLEMENTATION.md` - 深水策略
- `UNIVERSAL_FRAMEWORK_REPORT.md` - 通用框架
- `VALIDATION_AND_OPTIMIZATION_P3.md` - 验证和优化
- `MOEN_L2_L3_DETAILED.md` - 摩恩L2/L3分析

### checklists/ - 完成清单和决策记录
所有历史的任务清单、决策清单和完成总结。

文档:
- `DOCUMENT_INDEX.md` - 文档导航（已被 doc/README.md 替代）
- `NEXT_STEPS_CHECKLIST.md` - 历史的下一步清单
- `DECISION_CHECKLIST.md` - 决策记录
- `TASK_COMPLETION_SUMMARY.md` - 任务完成总结
- `EXECUTION_SUMMARY.md` - 执行摘要
- `plan.md` - 历史计划

用途：查看历史任务和决策过程

### experiments/ - 实验数据（预留）
*(当前为空，计划用于存储过期实验的数据和记录)*

---

## 🔍 如何查找内容

### 按主题查找
| 主题 | 位置 | 文档 |
|-----|------|------|
| 参数如何优化? | `param_optimization_20260825/` | COMPREHENSIVE_IMPLEMENTATION_PLAN.md |
| 什么是Scheme A修复? | `scheme_a_fixes_20260821/` | SCHEME_A_RISK_FIXES.md |
| GUI怎么改进的? | `gui_improvement_20260825/` | GUI_IMPROVEMENT_REPORT.md |
| 过去的决策? | `checklists/` | DECISION_CHECKLIST.md |
| 完成了什么工作? | `checklists/` | TASK_COMPLETION_SUMMARY.md |

### 按时间查找
| 时间 | 主要方案 |
|-----|---------|
| 2026-08-25 | 参数优化、GUI改进 |
| 2026-08-21 | Scheme A修复 |
| 2026-08 | 各种实验和探索 |

---

## 📚 阅读建议

### 想了解历史决策
1. 打开 `checklists/DECISION_CHECKLIST.md`
2. 查看对应日期的方案

### 想参考参数优化方法
1. 查看 `param_optimization_20260825/COMPREHENSIVE_IMPLEMENTATION_PLAN.md`
2. 参考 `OPTUNA_OPTIMIZATION_PLAN.md` 了解执行过程

### 想了解问题修复
1. 查看 `scheme_a_fixes_20260821/` 下的文档
2. 理解问题和解决方案

---

## 🗂️ 添加新方案的步骤

如果有新的方案要存档：

1. **创建新目录**: `proposals/YYYY-MM-主题名/`
2. **将文档放入**: 一个方案的所有文档放在同一目录
3. **编写README**: 在目录下创建 README.md 简介
4. **更新本文档**: 在上面的表格中添加链接

---

## 清理策略

这个目录按以下规则维护：

- **保留期**: 所有方案和清单长期保留
- **分类**: 按日期和主题分组
- **更新**: 新方案定期添加，过期方案标记为deprecated
- **冗余**: 相同主题的新旧版本，保留最新一份，旧版移到 `_deprecated/`（如需要）

---

## 从这里导航

- **回到文档主导航**: [doc/README.md](../README.md)
- **查看当前系统状态**: [doc/CURRENT_STATUS.md](../CURRENT_STATUS.md)
- **系统优化计划**: [../../SYSTEM_OPTIMIZATION_PLAN.md](../../SYSTEM_OPTIMIZATION_PLAN.md)

---

**上次更新**: 2026-08-25  
**维护者**: 系统优化工程
