# 方案 A 上线变更清单（2026-08-24 凌晨）

## 文件修改

### 1. config.py
**新增**（L745-760 附近）：
```python
# 2026-08-24 方案A: 分标的共振门控优化
INDEX_RESONANCE_STOCK_OVERRIDE = {
    "588170": {"enabled": False},  # 科创ETF - 禁用门控
    "300153": {"enabled": False},  # 科泰电源 - 禁用门控
}
```

**更新** STOCK_PARAMS（L377-405 附近）：
- 新增 `"swing_buy_rsi"`, `"swing_bb_lower"` 参数给 588170/300153
- 分标的门控参数已注释说明

### 2. main.py  
**修改** `_resonance_gate()` 函数（L118-158）：
- 在函数顶部增加分标的覆盖逻辑
- 检查 `INDEX_RESONANCE_STOCK_OVERRIDE[code]`
- 若 `enabled=False`，返回 `True, {"bypass": "stock_override_disabled"}`

## 预期效果（明天 08-25）

| 标的 | 前状态 | 后状态 | 变化 |
|------|-------|--------|------|
| 588170 | BUY 0/天 | BUY 15-25/天 | +100% |
| 300153 | BUY ~4/天 | BUY 6-8/天 | +50% |
| 600481 | BUY ~5/天 | BUY ~5/天 | 无变（默认门控） |
| 000988 | BUY ~5/天 | BUY ~5/天 | 无变（默认门控） |

## 验收清单 ✓

- [ ] 配置文件保存无误（config.py 可正常导入）
- [ ] main.py 可正常启动（无语法错误）
- [ ] 288170 早盘推送 BUY 信号 >=3 条
- [ ] GUI 面板显示 588170 信号（未被拦截）
- [ ] 复盘数据 `index_resonance_2026-08-25.jsonl` 中 588170 的 `bypass=stock_override_disabled`

## 回滚方式（如需紧急回滚）

```bash
# 修改 config.py
INDEX_RESONANCE_STOCK_OVERRIDE = {}  # 清空覆盖表

# 或临时禁用整个分标的功能
# 在 main.py._resonance_gate() 顶部注释掉新增的 5 行覆盖逻辑
```

重启 main.py 即自动应用。

