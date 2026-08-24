# 实盘验证与参数优化指南（P3）

## 概述

修复完成后需要1-2周的实盘验证，收集数据反馈，持续优化参数。

---

## 第1周：关键指标监测

### 1.1 加仓条件触发频率对标

**监测内容**：

| 指标 | 旧版预期 | 新版预期 | 监测周期 |
|------|---------|---------|---------|
| 日均加仓触发次数 | ~5-8次 | ~2-3次 | 每日 |
| 右侧突破触发率 | 100% | ~30-50% | 每日 |
| signal级突破 | 计入 | 排除 | 实时 |
| reliable级突破 | 计入 | 计入 | 实时 |
| strong级突破 | 计入 | 计入 | 实时 |

**监测脚本**（Python）：

```python
# t_io/validation/weekly_metrics.py
import json
from pathlib import Path
from datetime import datetime, timedelta

class MetricsCollector:
    def __init__(self):
        self.metrics_file = Path("t_io/validation/metrics_weekly.jsonl")
    
    def record_add_watch(self, date, stocks):
        """记录每日加仓观察统计"""
        record = {
            "date": date,
            "timestamp": datetime.now().isoformat(),
            "total_add_watch": len(stocks),
            "breakout_levels": {
                "signal": sum(1 for s in stocks.values() if s.get("breakout_level") == "signal"),
                "reliable": sum(1 for s in stocks.values() if s.get("breakout_level") == "reliable"),
                "strong": sum(1 for s in stocks.values() if s.get("breakout_level") == "strong"),
            },
            "quality_scores": [s.get("box_quality_score", 0) for s in stocks.values() if s.get("box_quality_score")],
        }
        
        with open(self.metrics_file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    
    def weekly_summary(self):
        """生成周汇总"""
        records = []
        with open(self.metrics_file) as f:
            for line in f:
                records.append(json.loads(line))
        
        # 按日期分组统计
        daily_stats = {}
        for r in records:
            date = r["date"]
            if date not in daily_stats:
                daily_stats[date] = {
                    "total": 0,
                    "signal": 0,
                    "reliable": 0,
                    "strong": 0,
                }
            daily_stats[date]["total"] = r["total_add_watch"]
            daily_stats[date]["signal"] = r["breakout_levels"]["signal"]
            daily_stats[date]["reliable"] = r["breakout_levels"]["reliable"]
            daily_stats[date]["strong"] = r["breakout_levels"]["strong"]
        
        return daily_stats

collector = MetricsCollector()
```

### 1.2 虚假突破率

**定义**：加仓后3-5日内未能继续上涨或回踩破位的比例

**监测**：

```python
def track_breakout_quality(code, entry_price, entry_date):
    """跟踪单次加仓的突破质量"""
    # 3日后检查
    result_3d = check_performance(code, entry_date, days=3)
    gain_3d = (result_3d["close"] - entry_price) / entry_price * 100
    
    # 5日后检查
    result_5d = check_performance(code, entry_date, days=5)
    gain_5d = (result_5d["close"] - entry_price) / entry_price * 100
    min_5d = result_5d["low"]
    
    # 判定为真突破还是虚假
    is_true_breakout = gain_3d >= 3 or (gain_5d >= 5 and min_5d > entry_price)
    
    return {
        "date": entry_date,
        "code": code,
        "entry_price": entry_price,
        "gain_3d": gain_3d,
        "gain_5d": gain_5d,
        "is_true": is_true_breakout,
    }
```

### 1.3 加仓成功率

**定义**：加仓后达成目标收益（+5%）的概率

**目标**：新版 > 60%（旧版基线 ~40-50%）

---

## 第2周：参数微调

基于第1周的数据反馈，对以下参数进行微调：

### 2.1 突破阈值调整

| 参数 | 旧值 | 可调范围 | 调整依据 |
|------|------|---------|---------|
| signal_max | 1.0% | 0.5-1.5% | 信号级误报率 |
| reliable_min | 1.0% | 0.8-1.2% | 可靠级触发率 |
| reliable_max | 3.0% | 2.5-3.5% | 可靠级胜率 |
| strong_min | 3.0% | 2.5-3.5% | 强势级胜率 |

**调整公式**：

```python
def adjust_thresholds(metrics):
    """基于实盘指标自动调整阈值"""
    signal_false_rate = metrics["signal_false_rate"]
    reliable_wr = metrics["reliable_win_rate"]
    strong_wr = metrics["strong_win_rate"]
    
    new_params = {}
    
    # 如果signal级误报>70%，降低signal上限
    if signal_false_rate > 0.7:
        new_params["signal_max"] = 0.7  # 从1.0降到0.7
    
    # 如果reliable级胜率<55%，提高reliable下限
    if reliable_wr < 0.55:
        new_params["reliable_min"] = 1.2  # 从1.0提到1.2
    
    # 如果strong级胜率>70%，可以考虑加分
    if strong_wr > 0.70:
        new_params["strong_bonus"] = 1  # D10给2分改3分
    
    return new_params
```

### 2.2 其他可能的调整

| 参数 | 说明 | 调整触发条件 |
|------|------|------------|
| touch_tolerance | 触及容差 | 若虚假突破太多，收紧到0.3% |
| flatness_threshold | 横盘阈值 | 若箱体太宽松，改为0.2%/天 |
| merge_price_overlap | 合并条件 | 若历史箱体过度合并，改为90% |
| quality_score_weight | 质量权重 | 若强势级触发太少，提高权重 |

---

## 第3周：沉淀优化结果

### 3.1 生成优化报告

```python
def generate_optimization_report():
    """生成3周优化总结报告"""
    
    report = {
        "period": "2026-08-24 to 2026-09-14",
        "summary": {
            "total_add_watch": 0,
            "signal_level": 0,
            "reliable_level": 0,
            "strong_level": 0,
            "true_breakout_rate": 0,
            "avg_win_rate_3d": 0,
            "avg_win_rate_5d": 0,
        },
        "parameter_changes": {},
        "recommendations": [],
    }
    
    # ... 数据汇总逻辑 ...
    
    return report
```

### 3.2 优化决策

基于报告，决策是否需要进一步优化：

- **胜率>60%** → 方案可上线生产
- **胜率50-60%** → 继续微调参数
- **胜率<50%** → 需要重新评估设计

---

## 监测清单

### 日常监测（每日）

- [ ] 记录加仓条件触发次数与等级分布
- [ ] 记录加仓后的3日/5日收益
- [ ] 监测虚假突破情况
- [ ] 检查系统运行状态（无异常错误）

### 周期性分析（每周五）

- [ ] 汇总周指标
- [ ] 对标预期
- [ ] 识别异常（某只股票虚假突破率特别高）
- [ ] 记录market regime变化（震荡/上涨/下跌）

### 模型优化（第2-3周）

- [ ] 基于数据调整参数
- [ ] A/B测试新参数组合
- [ ] 评估优化效果
- [ ] 确定最终参数配置

---

## 实盘配置

### 启用监测的配置

```python
# config.py 新增
ENABLE_METRICS_COLLECTION = True
METRICS_OUTPUT_DIR = Path("t_io/validation")

# 每日自动生成周报
AUTO_WEEKLY_REPORT = True
WEEKLY_REPORT_TIME = "17:00"  # 每个交易日下午5点
```

### 告警阈值

```python
ALERT_THRESHOLDS = {
    "breakout_false_rate_high": 0.7,      # 虚假率>70%告警
    "win_rate_low": 0.45,                 # 胜率<45%告警
    "add_watch_anomaly": 20,              # 单日>20次加仓异常
    "system_error_rate": 0.05,            # 错误率>5%告警
}
```

---

## 参考文档

- BOX_BREAKOUT_QUALITY_REVIEW.md — 问题分析
- BOX_BREAKOUT_FIX_REPORT.md — 部署指南
- box_breakout_validation.py — 回测脚本
- SYSTEM_COMPLETE_GUIDE.md — 系统完整说明

---

## 预期时间表

| 时间 | 任务 | 负责人 | 状态 |
|------|------|--------|------|
| 2026-08-24 | 修复完成，代码上线 | Claude | ✅ |
| 2026-08-24 ~ 2026-08-30 | 第1周监测 | 实盘运行 | ⏳ |
| 2026-08-31 ~ 2026-09-06 | 参数调整与微调 | 数据反馈 | 待执行 |
| 2026-09-07 ~ 2026-09-14 | 最终优化与沉淀 | 性能确认 | 待执行 |
| 2026-09-15 | 优化完成，最终上线 | - | - |

---

**监测版本**：P3  
**文档版本**：v1.0  
**创建时间**：2026-08-24  
**状态**：待1周实盘验证

