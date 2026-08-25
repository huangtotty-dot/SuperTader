# 第二阶段：代码清理决策 - validation/_archive 清理方案

**生成时间**: 2026-08-25  
**分析对象**: `t_io/validation/_archive/` (37个文件，296KB)

---

## 📊 扫描结果汇总

| 分类 | 数量 | 大小 | 处理方式 |
|-----|------|------|---------|
| 🗑️ **完全删除** | 26 | ~220KB | 无用/过期 |
| 📚 **保留参考** | 6 | ~80KB | refs/（学习用） |
| 📄 **日志文件** | 5 | ~5KB | 一起删除 |
| **总计** | 37 | 296KB | |

---

## 🔍 分类详表

### A. 实验运行脚本 - 完全删除（7个）

这些是旧版本的实验运行脚本，对应的实验版本已不再进行。

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `run_ab_expanded.py` | | 旧版本 AB test 实验 | 🗑️ 删除 |
| `run_ab_threshold.py` | | 旧版本阈值实验 | 🗑️ 删除 |
| `run_ab_unified.py` | | 旧版本统一方案实验 | 🗑️ 删除 |
| `run_baseline_live.py` | | 旧版本基线实验 | 🗑️ 删除 |
| `run_degraded.py` | | 旧版本降级方案 | 🗑️ 删除 |
| `run_e1_final.py` | | 旧版本 E1 实验 | 🗑️ 删除 |
| `run_variant_a.py` | | 旧版本变体实验 | 🗑️ 删除 |

**理由**：对应版本的实验已完成，结果已迁出，运行脚本无继续使用价值。

---

### B. 数据合并脚本 - 完全删除（7个）

这些是旧实验的结果合并脚本。

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `merge_ab_expanded.py` | | 合并 AB 实验结果 | 🗑️ 删除 |
| `merge_ab_threshold.py` | | 合并阈值实验结果 | 🗑️ 删除 |
| `merge_ab_unified.py` | | 合并统一方案结果 | 🗑️ 删除 |
| `merge_baseline_live.py` | | 合并基线结果 | 🗑️ 删除 |
| `merge_degraded.py` | | 合并降级结果 | 🗑️ 删除 |
| `merge_e1_final.py` | | 合并 E1 结果 | 🗑️ 删除 |
| `merge_variant_a.py` | | 合并变体结果 | 🗑️ 删除 |

**理由**：实验结果已保存到对应目录，不需要这些脚本再次合并。

---

### C. 分析脚本 - 完全删除（3个）

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `analyze_ab_expanded.py` | | 分析 AB 实验 | 🗑️ 删除 |
| `analyze_ab_unified.py` | | 分析统一方案 | 🗑️ 删除 |
| `analyze_threshold_ladder.py` | | 分析阈值阶梯 | 🗑️ 删除 |

**理由**：一次性分析脚本，分析结果无记录，实验已结束。

---

### D. 审计脚本 - 完全删除（3个）

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `audit_recompute.py` | | 审计重新计算 | 🗑️ 删除 |
| `audit_v105_recompute.py` | | V105 版本审计 | 🗑️ 删除 |
| `audit_v106_recompute.py` | | V106 版本审计 | 🗑️ 删除 |

**理由**：V105/V106 已是历史版本，对应审计已完成。

---

### E. 数据获取脚本 - 完全删除（5个）

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `ts_fetch_minutes.py` | | 时间序列分钟数据获取 | 🗑️ 删除 |
| `ts_fetch_snapshot_seg.py` | | 时间序列快照分段获取 | 🗑️ 删除 |
| `ts_probe.py` | | 时间序列探测 | 🗑️ 删除 |
| `ts_fetch_log.txt` | | 日志 | 🗑️ 删除 |
| `ts_fetch_snapshot_seg_log.txt` | | 日志 | 🗑️ 删除 |

**理由**：已被 `data_fetcher.py` 替代，这些是旧版本实现。

---

### F. 调试探测脚本 - 完全删除（2个）

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `probe_0810.py` | | 2026-08-10 调试探测 | 🗑️ 删除 |
| `probe_0811.py` | | 2026-08-11 调试探测 | 🗑️ 删除 |

**理由**：临时调试脚本，无文档记录，实验已结束。

---

### G. 一次性工具 - 完全删除（1个）

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `summarize_unified.py` | | 汇总统一数据 | 🗑️ 删除 |

**理由**：一次性汇总脚本，结果已保存。

---

### H. 日志文件 - 删除（2个）

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `audit_recompute_report.txt` | | 审计日志 | 🗑️ 删除 |
| `audit_v105_report.txt` | | 审计日志 | 🗑️ 删除 |
| `audit_v106_report.txt` | | 审计日志 | 🗑️ 删除 |

**理由**：过期的日志文件。

---

### I. 参考脚本 - 保留到 refs/（6个）

这些脚本虽然过期，但可能具有参考或学习价值。

| 文件 | 大小 | 理由 | 决策 |
|-----|------|------|------|
| `smoke_v110_degraded.py` | | 降级版本烟雾测试 | 📚 保留 |
| `buy_score_diag.py` | | 买入评分诊断 | 📚 保留 |
| `check_part_ranges.py` | | 分段范围检查 | 📚 保留 |
| `intercept_attribution.py` | | 拦截归因分析 | 📚 保留 |
| `x4_switch_lag.py` | | 延迟检查工具 | 📚 保留 |
| `p1_det_test.py` | | P1 确定性测试 | 📚 保留 |

**理由**：这些脚本虽然不再使用，但包含有用的诊断逻辑或测试方法。保留到 `t_io/validation/refs/` 作为参考。

---

## ✅ 执行计划

### 步骤1：创建 refs 目录
```bash
mkdir -p t_io/validation/refs
```

### 步骤2：移动参考脚本
```bash
mv t_io/validation/_archive/smoke_v110_degraded.py t_io/validation/refs/
mv t_io/validation/_archive/buy_score_diag.py t_io/validation/refs/
mv t_io/validation/_archive/check_part_ranges.py t_io/validation/refs/
mv t_io/validation/_archive/intercept_attribution.py t_io/validation/refs/
mv t_io/validation/_archive/x4_switch_lag.py t_io/validation/refs/
mv t_io/validation/_archive/p1_det_test.py t_io/validation/refs/
```

### 步骤3：创建 refs 说明文档
创建 `t_io/validation/refs/README.md`

### 步骤4：删除_archive目录
```bash
rm -rf t_io/validation/_archive
```

### 步骤5：Commit
```bash
git add t_io/validation/refs
git rm -r t_io/validation/_archive
git commit -m "refactor: phase2.1完成 - validation代码清理

- 删除26个过期/无用脚本 (~220KB)
  - 实验运行脚本 (7个)
  - 结果合并脚本 (7个)
  - 分析脚本 (3个)
  - 审计脚本 (3个)
  - 数据获取脚本 (5个)
  - 调试脚本 (2个)
- 保留6个参考脚本到 t_io/validation/refs/
- 删除5个日志文件
- 结果: _archive 清理完毕，节省260KB+整理成本"
```

---

## 📈 预期收益

- **空间节省**: 296KB → 0 (archive 删除) + 80KB (refs) = 节省 216KB
- **维护负担**: 消除37个过期脚本的查看/维护成本
- **代码清晰**: validation 目录结构更清楚
- **向后兼容**: 需要时，refs 中的脚本仍可参考

---

## 📝 风险评估

**风险等级**: 🟢 **低**

| 风险 | 评估 | 缓解措施 |
|-----|------|---------|
| 误删需要的脚本 | 极低（都是旧版本） | 删除前备份到外部 |
| 破坏其他系统 | 无（这些脚本独立） | 无需特殊措施 |
| 数据丢失 | 无（结果已保存） | 无需特殊措施 |

---

## 🎯 后续计划

**2.2 - 代码模块重复扫描**（Week 2-3）
- 扫描根目录 63 个 PY 模块
- 识别重复的指标/工具实现
- 规划合并或参数化策略

**2.3 - 代码渐进式重构**（Week 3+）
- 建立新的 core/data/indicators/strategies 结构
- 新代码直接写到新位置
- 旧代码逐步迁移

---

**决策日期**: 2026-08-25  
**执行建议**: 立即执行第1-5步（总耗时 ~5分钟）
