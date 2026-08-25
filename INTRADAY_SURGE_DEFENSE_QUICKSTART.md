# 日内冲高防御系统 - 快速开始

## 刚完成的工作 (2026-08-25)

为防止"摩恩电气式冲高回落被套"，完整建立了日内防御系统：

### 核心模块
1. **intraday_surge_defense.py** (264行)
   - `classify_daily_limit()` - 区分涨停类型
   - `detect_pullback_from_high()` - 实时回落检测
   - `check_intraday_buypoint_quality()` - 买点质量评估
   - `intraday_surge_defense()` - 综合防御入口

2. **intraday_surge_monitor.py** (123行)
   - `monitor_surge_risks()` - 扫描所有持仓风险
   - 支持 holdings + watchlist 双重监控

3. **demo_surge_defense.py** (194行)
   - 三个场景演示：集合竞价涨停、健康日内涨停、持续下跌
   - 已验证可正常运行

4. **INTRADAY_SURGE_DEFENSE_GUIDE.md** (完整工作流文档)

## 快速使用

### 方式1：独立检测单只股票

```python
from intraday_surge_defense import intraday_surge_defense
from position_builder import load_snapshot_df

# 加载日内分钟线
df_1min, _, _ = load_snapshot_df("002451", "2026-08-25")

# 运行防御检测
result = intraday_surge_defense("002451", "摩恩电气", df_1min)

# 查看结果
print(f"行动: {result.action}")  # SAFE | WARNING | AVOID | EXIT
print(f"原因: {result.reason}")
```

### 方式2：批量监控所有持仓

```python
from intraday_surge_monitor import monitor_surge_risks, format_surge_alert

# 扫描 holdings + watchlist
result = monitor_surge_risks()

# 显示告警
print(format_surge_alert(result))

# 获取需要立即处理的
for alert in result["critical_alerts"]:
    print(f"[{alert['action']}] {alert['code']} - {alert['reason']}")
```

### 方式3：集成到日内监控循环

```python
import time
from intraday_surge_monitor import monitor_surge_risks

# 盘中每10分钟扫描一次
while True:
    result = monitor_surge_risks()
    
    if result["critical_alerts"]:
        print(f"[告警] 发现 {len(result['critical_alerts'])} 只高风险持仓")
        for alert in result["critical_alerts"]:
            # 可以集成飞书推送、语音告警等
            pass
    
    time.sleep(600)  # 10分钟
```

## 关键决策路由

| 系统输出 | 含义 | 建议操作 |
|---------|------|---------|
| **SAFE** | 无明显风险 | 继续持仓，正常监控 |
| **WARNING** | 有回落迹象 | 暂停加仓，观察支撑 |
| **AVOID** | 明显回落信号 | 不宜追高，考虑减仓 |
| **EXIT** | 严重回落(>10%) | 立即止损 |

## 参数说明

### 涨停分类
- **auction_limit** (集合竞价涨停): 09:20-09:31 期间首次触及涨停
  - 风险等级: 🔴 极高
  - 原因: 代表"全市场情绪发泄"，非个股强度
  - 建议: 绝对不追高

- **intraday_limit** (日内涨停): 09:31-14:50 期间首次触及涨停
  - 风险等级: 🟡 中等
  - 原因: 有明确日内上升趋势支撑
  - 建议: 相对安全，可观察

- **close_limit** (尾盘涨停): 14:50+ 涨停
  - 风险等级: 🟢 低
  - 原因: 流动性差，但风险相对可控
  - 建议: 关注次日开盘

### 回落警告阈值
```
回落 < 2%    → 无异常
回落 2-5%    → 轻微提醒
回落 5-10%   → 明显警告
回落 > 10%   → 严重警告(EXIT)
```

## 系统特性

✅ **已实现**
- 涨停类型自动分类
- 实时高点与当前价的回落计算
- 买点质量多指标综合判断
- 支持批量监控
- 详细的诊断信息输出

⚠️ **已知限制**
- 依赖分钟线数据质量（滞后时无法及时预警）
- 集合竞价定义基于 09:31，可微调
- 流动性极差的小盘股预测可能偏差
- 需要与 position_builder 集成才能获得前日收盘数据

## 集成建议

### 1. position_builder 中增强信号评分
```python
def score_with_surge_defense(code, df_1min, base_score):
    """在原有打分上增加冲高风险penalty"""
    from intraday_surge_defense import classify_daily_limit, detect_pullback_from_high
    
    is_limit, limit_type, _ = classify_daily_limit(df_1min)
    
    # 集合竞价涨停 → 评分减半
    if is_limit and limit_type == "auction_limit":
        return base_score * 0.5
    
    # 严重回落 → 评分折3折
    pullback = detect_pullback_from_high(df_1min)
    if pullback["alert_level"] == "critical":
        return base_score * 0.3
    
    return base_score
```

### 2. GUI 中显示冲高防御卡片
```
[摩恩电气 002451]
基础信号分: 70分
├─ 涨停分类: auction_limit ⚠️
├─ 回落: 11% [critical] 🔴
└─ 最终建议: AVOID
```

### 3. 飞书推送告警
```
盘中监控发现高风险持仓：
🔴 摩恩电气(002451) - EXIT
   严重回落11%，建议止损
```

## 文件列表

```
superTrader/
├── intraday_surge_defense.py        # 核心防御模块 ⭐
├── intraday_surge_monitor.py        # 实时监控模块
├── demo_surge_defense.py            # 演示脚本
├── INTRADAY_SURGE_DEFENSE_GUIDE.md  # 完整文档
└── t_io/traces/
    └── intraday_surge_monitor.jsonl # 监控日志(自动生成)
```

## 测试方法

```bash
# 1. 运行演示（验证逻辑）
python demo_surge_defense.py

# 2. 单只监控
python -c "
from intraday_surge_defense import intraday_surge_defense
from position_builder import load_snapshot_df
df_1min, _, _ = load_snapshot_df('002451', '2026-08-25')
result = intraday_surge_defense('002451', '摩恩电气', df_1min)
print(f'{result.action}: {result.reason}')
"

# 3. 全量监控
python -c "
from intraday_surge_monitor import monitor_surge_risks
r = monitor_surge_risks()
print(f'critical alerts: {len(r[\"critical_alerts\"])}')
"
```

## 下一步

1. **集成到实时监控** - 融入 position_builder 或独立监控进程
2. **参数优化** - 根据实际交易反馈调整回落阈值
3. **历史回溯** - 用过去3个月数据验证准确率
4. **飞书推送** - 配置告警卡片模板

## 联系反馈

如遇到问题或有改进建议，请检查：
- 分钟线数据是否完整（需要 amount/volume 字段）
- 前日收盘数据是否正确（影响涨停判定）
- 系统时间是否准确（09:20-09:31 的识别依赖时间戳）
