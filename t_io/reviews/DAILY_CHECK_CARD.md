# 每日做T优化检查 · 快速参考卡

> 复盘时快速查看的检查清单（打印版）

---

## 📋 每日 15:10 后的检查项

### ✅ 快速检查（2 分钟）

```
☐ 588170 当日 BUY 推送 > 0 个?      预期: 10-30 个
   Y → 继续   N → ⚠️ 检查方案A配置
   
☐ 300153 当日 BUY 推送 > 0 个?      预期: 5-10 个  
   Y → 继续   N → ⚠️ 检查方案A配置
   
☐ 看到 `stock_override_disabled` 标记?
   Y → ✅ 方案A 正常   N → ⚠️ 配置未生效
   
☐ 对照个股(600481/000988)推送率 ~100%?
   Y → ✅ 无异常   N → ⚠️ 可能有新拦截规则
```

---

## 📊 数据来源位置

| 检查项 | 数据位置 | 命令 |
|--------|---------|------|
| 当日BUY推送数 | `t_io/traces/decision_trace_2026-MM-DD.jsonl` | `grep '"decision": "BUY_LOW"' ` |
| 推送通过情况 | `t_io/traces/index_resonance_2026-MM-DD.jsonl` | 查找 `bypass: stock_override_disabled` |
| 完整报告 | 自动生成: `doc/每日复盘/2026-MM-DD_复盘.md` 的 §4.5 | 在 Review.md 中查看 |

---

## 🎯 预期效果对比

| 日期 | 588170 BUY | 300153 BUY | 状态 | 备注 |
|------|-----------|-----------|------|------|
| 08-24 (优化前) | 0 | 2-3 | 基准 | 方案A 上线前 |
| 08-25 (优化后) | 15+ | 5+ | 验证中 | 方案A 首日 |
| 08-26+ | 20+ | 7+ | 稳定 | 持续监控 |

---

## ⚠️ 故障排查速查表

| 现象 | 原因 | 解决 |
|------|------|------|
| 588170 推送仍为 0 | 方案A 配置未生效 | ① 检查 config.py 的 `INDEX_RESONANCE_STOCK_OVERRIDE` ② 检查 main.py._resonance_gate() ③ 重启 main.py |
| `bypass_disabled` 标记 | 方案A 不存在 | 检查 config.py + main.py 中是否手工添加了新代码 |
| 推送数小幅波动 | 正常（市场波动） | 无需处理，继续监控趋势 |
| 推送数突增后急跌 | 可能系统故障或配置被重置 | 查看 main.py 日志，检查 config 是否被覆盖 |

---

## 📱 通知设置（可选）

### 如果检查失败，自动通知你

可在 `check_optimization_effect.py` 中添加：

```python
# 如果方案A 未生效，发送飞书通知
if not schema_a_working:
    send_feishu_alert(
        "⚠️ 做T优化方案A 未生效",
        f"检测日期: {date_str}\n"
        f"目标 ETF 推送数为 0\n"
        f"请检查 config.py + main.py 配置"
    )
```

---

## 📞 联系信息

- **代码维护**：见 `2026-08-24_OPTIMIZATION_IMPLEMENTATION_PLAN.md`
- **配置问题**：见 `DAILY_CHECK_GUIDE.md` → 故障排查
- **自动化问题**：Windows 任务计划 → 查看历史记录

---

**打印此卡，贴在显示器旁** 👉

