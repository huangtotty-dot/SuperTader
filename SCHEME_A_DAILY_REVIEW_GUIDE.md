# 方案A实盘观察期 - 每日复盘集成指南

## 📋 概述

方案A观察期（2026-08-25 至 2026-08-27）需要每日复盘验证三个关键指标：
1. **stock_override标的的推送恢复情况**
2. **虚假信号的实时监控**
3. **是否需要触发自动回退机制**

本指南描述如何将方案A的检查点集成到现有的 `daily_review.py` 体系中。

---

## 🔄 复盘流程集成

### 现有复盘体系
```
daily_review.py (现有)
├─ §1: decision_trace 分析（信号/极值/振幅）
├─ §2: shadow_signals 分析（拦截分布）
├─ §3: position 分析（持仓/收益）
├─ §4: 日报表生成
└─ §5: 观察项汇总
```

### 集成后的复盘体系
```
daily_review.py (现有)
├─ §1-5: 原有分析
└─ 【新增】方案A观察期检查

scheme_a_daily_review.py (新建)
├─ §1: stock_override 标的推送统计
├─ §2: 虚假信号监控状态
├─ §3: 观察期行动清单（Day 1/2/3）
├─ §4: 关键监控指标汇总表
├─ §5: 自动化检查脚本调用
└─ 输出: scheme_a_daily_review_DATE.json
```

---

## 📅 每日执行步骤

### Day 1 (2026-08-25) - 启用方案A

**时间点：08:30 启动前、12:00 中盘、16:00 收盘**

```bash
# 08:30 启动前检查（确认方案A已启用）
python scheme_a_daily_review.py --date 2026-08-25

# 检查清单：
#  □ 588170 推送数 > 0（对比修复前的0）
#  □ 300153 推送数正常
#  □ stock_override 绕过计数 > 0
#  □ 虚假信号监控已初始化
```

**预期输出：**
```
【§1：stock_override 标的推送情况】
标的: 588170
  推送总数:        6+ 条
  stock_override绕过:   >0 次

标的: 300153
  推送总数:       正常 条
```

**验证要点：**
- ✅ 588170 从修复前的 **36次拦截→0条推送** 恢复到 **6+条推送**
- ✅ 300153 推送数恢复到正常水平
- ✅ stock_override 绕过计数记录存档

### Day 2 (2026-08-26) - 检查虚假信号表现

**时间点：09:30（检查Day1后续表现）、16:00 收盘**

```bash
# 09:30 检查Day1推送的虚假信号后续表现
python scheme_a_daily_review.py --date 2026-08-26

# 检查清单：
#  □ Day 1推送信号是否有虚假（下跌>3%）
#  □ 虚假信号后续1小时内最低价
#  □ 虚假比例是否 < 5%
#  □ 是否需要触发回退
```

**预期输出：**
```
【§2：虚假信号监控状态】
有效信号:    15 条
虚假信号:     0 条
虚假比例:  0.00%

状态判断:   ✅ 正常
```

**验证要点：**
- ✅ 虚假信号比例 < 5%（满足继续条件）
- ✅ 无需触发回退机制
- ✅ 信号质量稳定

### Day 3 (2026-08-27) - 最终评估决策

**时间点：15:00 下午14点做最终评估**

```bash
# 15:00 最终决策（Day 3观察期的最后一步）
python scheme_a_daily_review.py --date 2026-08-27 --finalize

# 这会自动输出决策树：
#  虚假 < 5%  → 方案A 继续执行 ✅
#  5% ≤ 虚假 < 10% → 参数调整
#  虚假 ≥ 10% → 彻底回退 ❌
```

**预期输出：**
```
【Day 3 最终决策】
✅ 虚假信号比例 3.33% < 5%
✅ 决策：方案A 继续执行，监控期结束

后续行动：
  1. 归档观察期数据
  2. 考虑逐步扩展到其他stock_override标的
  3. 继续保持虚假信号监控（背景运行）
```

---

## 📊 关键监控指标

| 指标 | Day 1 期望 | Day 2 期望 | Day 3 决策 | 责任方 |
|------|----------|----------|---------|-------|
| 588170推送数 | >0 | 稳定增长 | 6+ | 自动检查 |
| 300153推送数 | 正常 | 稳定正常 | 正常 | 自动检查 |
| stock_override绕过 | >0 | 递增 | N次 | 自动统计 |
| 虚假信号比例 | 初值记录 | <5% | <5% ✅ | 虚假监控 |
| 推送总数(日) | 6+ | 12+ | 18+ | 自动统计 |
| 系统状态 | 正常启用 | 实时监控 | 最终评估 | 自动判断 |

---

## 🔍 数据来源与验证

### 输入数据
```
e:/superTrader/
├─ t_io/traces/
│  ├─ decision_trace_2026-08-2X.jsonl    # 推送决策日志
│  └─ shadow_signals_2026-08-2X.jsonl    # 被拦截信号日志
├─ t_io/state/
│  └─ false_signal_monitor.json          # 虚假信号监控状态
└─ config.py                              # stock_override配置
```

### 输出产物
```
e:/superTrader/
└─ scheme_a_daily_review_2026-08-2X.json  # 每日复盘结果
```

### 验证方法
```bash
# 查看trace文件内容
cat t_io/traces/decision_trace_2026-08-25.jsonl | grep 588170 | head -5

# 查看拦截分布
grep -c "防重桶" t_io/traces/shadow_signals_2026-08-25.jsonl
grep -c "共振" t_io/traces/shadow_signals_2026-08-25.jsonl

# 查看虚假信号监控状态
cat t_io/state/false_signal_monitor.json | python -m json.tool
```

---

## ⚠️ 决策规则

### 虚假信号比例判断

```
虚假比例 = 虚假信号数 / (虚假信号数 + 有效信号数)

【第1档】虚假 < 5%        → ✅ 正常，继续执行
【第2档】5% ≤ 虚假 < 10%  → ⚠️ 警告，触发参数调整
【第3档】虚假 ≥ 10%       → ❌ 危险，彻底回退
```

### 自动回退触发条件

| 条件 | 行动 | 执行时间 |
|------|------|--------|
| 虚假 > 5% | 降低信号评分(55→60) | 即时 |
| 虚假 > 8% | 恢复单股共振门控 | 即时 |
| 虚假 > 10% | 禁用方案A，恢复原方案 | 即时 |

---

## 🛠️ 使用示例

### 基础用法
```bash
# 生成指定日期的复盘清单
python scheme_a_daily_review.py --date 2026-08-25

# 生成今天的复盘清单
python scheme_a_daily_review.py
```

### 最终决策模式
```bash
# Day 3下午运行，自动决策和输出后续行动
python scheme_a_daily_review.py --date 2026-08-27 --finalize
```

### 集成到定时任务
```bash
# 每天08:30执行（Linux/Mac cron 或 Windows Task Scheduler）
0 8 25-27 8 * python /path/to/scheme_a_daily_review.py

# Day 3下午14:00执行最终决策
0 14 27 8 * python /path/to/scheme_a_daily_review.py --finalize
```

---

## 📝 输出示例

### 输出日志文件
```json
{
  "date": "2026-08-25",
  "generated_at": "2026-08-25T08:35:42.123456",
  "stock_override_analysis": {
    "588170": {
      "code": "588170",
      "total_pushed": 6,
      "buy_pushed": 6,
      "miss_distribution": {
        "防重桶": 0,
        "共振": 0,
        "stock_override跳过": 12
      }
    }
  },
  "false_signals_analysis": {
    "monitor_status": "available",
    "total_false": 0,
    "total_true": 6,
    "false_ratio": 0.0
  }
}
```

---

## 📋 观察期检查清单模板

### 每日关键检查点

- [ ] **morning (08:30)** 启动复盘前检查
  - [ ] 推送恢复情况：588170 > 0?
  - [ ] stock_override绕过计数 > 0?
  - [ ] 虚假信号监控状态正常?

- [ ] **midday (12:00)** 中盘状态检查
  - [ ] 推送数是否如预期增长?
  - [ ] 是否有异常拦截?

- [ ] **eod (16:00)** 收盘复盘
  - [ ] 每日推送总数统计
  - [ ] 虚假信号初步统计
  - [ ] 生成每日复盘JSON

### 每日决策点（Day 2/3）

- [ ] **Day 2 09:30** 检查Day1虚假信号
  - [ ] 虚假信号后续表现 (下跌>3%?)
  - [ ] 虚假比例 < 5%?
  - [ ] 继续还是调整?

- [ ] **Day 3 15:00** 最终决策
  - [ ] 3日累计虚假比例
  - [ ] 执行自动化决策 (--finalize)
  - [ ] 归档观察期数据

---

## 🚨 常见问题

### Q: 如果Day 1虚假信号比例就 > 5%?
A: 立即触发回退机制：
1. 降低信号评分阈值
2. 增强拦截力度
3. 继续监控1天确认效果
4. 若仍 > 5%，彻底回退

### Q: stock_override绕过计数为0怎么办?
A: 说明修复2未生效，检查：
1. main.py 是否正确部署
2. config.py 的INDEX_RESONANCE_STOCK_OVERRIDE配置是否正确
3. 手动检查推送日志是否有stock_override相关记录

### Q: 虚假信号监控状态显示"not_available"?
A: 说明虚假信号监控未初始化，检查：
1. fake_signal_monitor.py 是否部署
2. main.py 推送时是否调用了 m.record_signal()
3. t_io/state/ 目录权限是否正确

---

## ✅ 完整观察期清单

- [x] 修复代码实现完成
- [x] 虚假信号监控系统就位
- [x] 每日复盘脚本开发完成
- [ ] Day 1 (2026-08-25) 启用 & 初值记录
- [ ] Day 2 (2026-08-26) 检查虚假表现
- [ ] Day 3 (2026-08-27) 最终决策
- [ ] 决策执行 & 文档归档

---

## 📞 相关文件

| 文件 | 说明 |
|------|------|
| fake_signal_monitor.py | 虚假信号监控系统 |
| scheme_a_daily_review.py | 每日复盘检查清单 ← **本文** |
| SCHEME_A_EXECUTION_REPORT.txt | 执行报告 |
| SCHEME_A_FIXES_IMPLEMENTATION.md | 实现文档 |
| main.py | 已集成虚假信号监控 |
| config.py | stock_override配置 |

