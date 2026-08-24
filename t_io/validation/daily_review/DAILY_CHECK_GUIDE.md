# 每日做T优化效果检查 - 自动化设置指南

> 方案A（共振门控优化）上线后，需要每日验收实际效果。本指南说明如何自动化这个流程。

---

## 快速开始

### 方式1：手工执行（最简单，用于测试）

每天盘后（15:00 复盘后），手动运行一次：

```bash
cd E:\superTrader
python t_io/validation/daily_review/check_optimization_effect.py --date 2026-08-25
```

输出会显示当日的做T优化效果对比，可以复制到 Review.md 中。

### 方式2：自动追加到复盘文件（推荐）

```bash
# 生成报告
python t_io/validation/daily_review/check_optimization_effect.py --date 2026-08-25 > report.txt

# 追加到当日复盘文件
type report.txt >> doc/每日复盘/2026-08-25_复盘.md
```

### 方式3：Windows 任务计划（完全自动，推荐用于长期）

#### Step 1: 创建任务计划

1. 打开 **Windows 任务计划程序**
   - Win + R → `taskschd.msc` → 回车

2. 右侧点击"创建基本任务"

3. 填写信息：
   - **名称**：每日做T优化检查
   - **描述**：方案A效果验收，自动追加到复盘文件

4. 触发器设置：
   - **开始日期**：2026-08-25
   - **每天**
   - **时间**：15:10（复盘通常完成的时间）
   - 重复间隔：每 1 天

5. 操作设置：
   - **程序或脚本**：`cmd`
   - **添加参数**：`/c E:\superTrader\t_io\validation\daily_review\check_daily_optimization.bat`
   - **起始于**：`E:\superTrader`

6. 完成创建

#### Step 2: 验证任务

```
任务计划程序 → 任务计划库 → 查找"每日做T优化检查"
右键 → 属性 → 检查设置是否正确
```

#### Step 3: 测试运行

```
右键 → 运行
检查是否成功执行（约 3-5 秒）
检查 doc/每日复盘/YYYY-MM-DD_复盘.md 是否有新增报告
```

---

## 报告内容说明

自动生成的检查报告包括：

### 表格 1：推送统计 vs 前一天

```
| 标的 | 类型 | 当日BUY信号 | 当日推送通过 | 昨日BUY信号 | 推送增长 |
|------|------|-----------|-----------|-----------|--------|
| 588170 | 目标ETF | 36 | 36 | 0 | 📈 +36 |
| 300153 | 目标ETF | 8 | 8 | 2 | 📈 +6 |
| 600481 | 对照个股 | 5 | 5 | 5 | ➡️ 0 |
```

**解读**：
- `当日BUY信号`：decision_trace 中检测到的 BUY_LOW 信号数
- `当日推送通过`：index_resonance 中实际通过门控的推送数
- `推送增长`：与前一天的对比（📈 增加 / 📉 减少 / ➡️ 持平）

### 表格 2：方案A状态确认

```
- [x] 588170: 检测到 `stock_override_disabled` bypass (36 次)
- [x] 300153: 检测到 `stock_override_disabled` bypass (8 次)

✅ **方案A 运行中**
```

**解读**：
- 如果看到 `stock_override_disabled`，说明方案A 的配置已生效
- 如果没有看到，需要检查 config.py 的 `INDEX_RESONANCE_STOCK_OVERRIDE` 是否正确

### 表格 3：推送质量对照

```
| 标的 | 总信号 | 推送通过 | 推送率 | 主要拦截原因 |
|------|--------|---------|--------|--------------|
| 588170 | 36 | 36 | 100% | stock_override_disabled |
| 300153 | 8 | 8 | 100% | stock_override_disabled |
| 600481 | 5 | 5 | 100% | index_ma5_dir |
```

**解读**：
- `总信号`：该标的全天生成的 BUY_LOW 信号数
- `推送通过`：其中实际通过门控的数量
- `推送率`：推送通过 / 总信号
- `主要拦截原因`：被拦截的信号的主要原因（仅当推送率 <100% 时显示）

---

## 每日检查清单

### 每天 15:10 自动执行后，检查以下几点：

- [ ] **方案A 状态**：是否看到 `stock_override_disabled` 标记
  - 如果 **有**：✅ 方案A 正常运行
  - 如果 **没有**：⚠️ 检查 config.py 配置

- [ ] **目标 ETF 推送数**：588170/300153 的当日 BUY 推送是否 >0
  - 预期：588170 每天 10-30 个推送（vs 之前 0）
  - 预期：300153 每天 5-10 个推送（vs 之前 2-3 个）
  - 如果 **低于预期**：可能配置未生效，需排查

- [ ] **对照个股推送率**：600481/000988 的推送率是否 ~100%
  - 预期：>90%（这些个股不受方案A 影响）
  - 如果 **下降**：可能有新的拦截规则被触发，需排查

- [ ] **推送增长趋势**：与前一天对比是否稳定
  - 预期：目标ETF 的推送数 持平或略涨（小幅波动正常）
  - 如果 **急剧下降**：可能系统出故障或配置被重置

---

## 故障排查

### 问题1：脚本执行失败，报错"NameError: name 'INDEX_RESONANCE_STOCK_OVERRIDE' is not defined"

**原因**：config.py 中没有正确定义 `INDEX_RESONANCE_STOCK_OVERRIDE`

**解决**：
1. 检查 config.py 是否有以下代码（L745 左右）
   ```python
   INDEX_RESONANCE_STOCK_OVERRIDE = {
       "588170": {"enabled": False},
       "300153": {"enabled": False},
   }
   ```
2. 如果没有，手工添加
3. 重启 main.py

### 问题2：方案A 状态显示"未检测到 bypass 标记"

**原因**：main.py._resonance_gate() 中的分标的覆盖逻辑未生效

**解决**：
1. 检查 main.py 的 _resonance_gate 函数（L118）是否有以下代码
   ```python
   # 2026-08-24 方案A: 检查该标的是否有分标的覆盖
   try:
       from config import INDEX_RESONANCE_STOCK_OVERRIDE as _irso
       if isinstance(_irso, dict) and code in _irso:
           _override = _irso[code]
           if _override.get("enabled") is False:
               return True, {"bypass": "stock_override_disabled"}
   except Exception:
       pass
   ```
2. 如果没有，手工添加（须在 SELL_HIGH 分流之前）
3. 重启 main.py

### 问题3：推送数没有增加（588170 仍然 0）

**原因**：可能是指数数据采集问题，或 index_resonance 模块异常

**解决**：
1. 检查当日的 index_resonance trace 文件是否存在
   ```bash
   ls -l t_io/traces/index_resonance_2026-08-25.jsonl
   ```
2. 如果文件很小或为空，说明数据采集有问题
3. 查看 main.py 日志中是否有 "⚠️ 共振计算异常" 的警告
4. 如果有，可能是 index_resonance 模块加载失败，检查依赖

---

## 长期监控指标

### 周报表（每周五生成）

```
周 | 目标ETF推送/天 | 对照个股推送/天 | 方案A状态 | 问题反馈
----|--------------|----------------|---------|--------
W34 | 16/36 (44%) | 5/5 (100%) | ✅ | 无
W35 | 22/30 (73%) | 5/5 (100%) | ✅ | 无
```

**预期趋势**：
- 目标ETF推送：第一周 ~40-50%，第二周 ~70-80%，第三周 ~90%+
- 对照个股：始终 ~100%
- 方案A状态：始终 ✅（除非主动关闭）

---

## 配置变更日志

| 日期 | 变更 | 原因 |
|------|------|------|
| 2026-08-24 | 方案A 上线 | 588170 做T信号全被拦截 |
| 2026-08-25 | 自动检查脚本部署 | 便于每日验收 |
| YYYY-MM-DD | — | — |

---

**版本**：v1.0  
**最后更新**：2026-08-24  
**维护者**：用户
