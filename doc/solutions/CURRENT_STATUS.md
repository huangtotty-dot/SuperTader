# 当前系统实施状态 - 快速查阅

**生成时间**: 2026-08-25  
**系统版本**: v2 swing2pt + W33专项  
**数据源**: 实盘运行日志 + 每日复盘

---

## 🟢 核心系统状态

### 交易引擎
| 组件 | 版本 | 状态 | 备注 |
|-----|------|------|------|
| **日内信号** | v2 swing2pt | ✅ 稳定 | 2026-08-13上线，纯两点规则 |
| **建仓策略** | W33双通道 | ⏳ 参考级 | 已上线但未通过W33 A4离线闸门 |
| **加仓策略** | W33单档 | ✅ 稳定 | 2026-08-13上线，固定比例 |
| **风险门控** | 多层 | ✅ 完成 | 日内门 + 时序门 + 涨停防御 |

### 数据和日志
| 组件 | 大小 | 更新频率 | 说明 |
|-----|------|---------|------|
| **日志traces** | 67MB | 实时 | decision_trace / shadow_signals / sizing_advice |
| **快照数据** | 120MB | 盘中 | 分钟线snapshots，回测数据源 |
| **复盘数据** | 自动 | 每日3-4分钟 | sizing_advice 日压缩 + 前瞻回填 |
| **持仓快照** | 343KB | 每日 | holdings_daily_{date}.json |

---

## 📊 关键参数一览

### 日内信号参数（signal_engine.py）
```yaml
engine: "v2_swing2pt"
signal_triggers:
  sell_high:
    condition: "5分收盘 触及上轨 AND RSI(6) > 75"
    params:
      swing_bb_upper: 1.0
      swing_sell_rsi: 75
  buy_low:
    condition: "5分收盘 触及下轨 AND RSI(6) < 35"
    params:
      swing_bb_lower: 0.0
      swing_buy_rsi: 35

rsi_config:
  period: 6
  param_name: "rsi_period_5m_swing"
  note: "独立列 rsi_5m_p6，不动 rsi_5m(14)"

warmup:
  min_bars: 13  # 约10:35后可出信号
  param_name: "swing_min_5m_bars"

push_policy:
  mode: "constant"
  qty_0_notation: "🔒满仓参考"  # 仓控0股时标记
  dedup: "same (code, action, 5min_bucket) per day"
```

### 建仓参数（position_builder.py - 参考级）
```yaml
channels:
  - name: "ice_point_reversal"
    description: "冰点反转（左侧）"
    score: 80
    conditions:
      - trend_confirmation: "近5日 MACD金叉 OR 站上MA5"
      - boll_freeze: "BOLL冰点(20)"
      - volume: "缩量(20)"
      - rsi: "RSI(<35) 仅展示"
  
  - name: "breakout_follow"
    description: "突破跟随（右侧）"
    score: 70
    conditions:
      - box_breakout: "突破箱体(40)"
      - volume_surge: "放量>1.5(30)"
      - macd: "DIF>DEA(30)"

note: "⚠️ W33 A4 离线闸门未过，信号仅供研究"
```

### 加仓参数（position_sizer.py）
```yaml
strategy: "single_tier"
ratios:
  rebalance:
    individual: 0.60  # 接回×0.60
    etf: 0.25
  initial_buy:
    individual: 0.20  # 首加×0.20
    etf: 0.25
```

---

## 🔄 日复盘体系（V2.1）

### 产出物
| 文件 | 位置 | 生成方式 | 用途 |
|-----|------|---------|------|
| sizing_advice_{date}.jsonl | t_io/traces/ | 实时推送 | 加仓/建仓 逐笔记录 |
| daily_review_{date}.md | t_io/validation/daily_review/ | 脚本自动生成 | 当日完整复盘 |
| forward_tracker_{date}.json | t_io/validation/daily_review/ | 幂等回填 | 前瞻收益对标 |

### 复盘模块
```
daily_review.py
├─ G1: 加仓观察
├─ G2: 建仓扫描
├─ G3: 阶段看板
├─ G4: 交易追踪
└─ K2/K3: 持仓准确性对照
```

### 周复盘流程
- **唯一改参时点**: 每周一周复盘后
- **数据来源**: 日复盘 G1~G4 自动汇总
- **决策文件**: `doc/复盘/周复盘/` 最新一周

---

## ✅ 当前在线的特性

### ✅ 已验收并稳定
- 日内纯两点信号 (swing2pt)
- 单档加仓策略
- 多层风险门控
- 日志数据层 (G1~G4)
- 日复盘自动化
- GUI复盘看板

### ⏳ 验收中 / 参考级
- 双通道建仓 (W33 A4离线闸门未过)
- 日内冲高防御系统
- 参数优化实验

### 🔧 即将上线
- 架构优化 (第二、三阶段)
- 配置中心升级
- 优化管线自动化

---

## 📈 性能指标目标

### 收益预期（基于回测）
- **前瞻5日收益**: +10-11% (覆盖率55-60%)
- **风险保护**: 92%（最大回撤控制）
- **夏普比**: >1.0

### 交易质量指标
- **信号假阳性率**: <40% (W33 A4 阈值)
- **胜率**: >55% (建仓信号)
- **赔率**: >1.5 (收益/风险比)

### 系统指标
- **信号推送延迟**: <100ms
- **日复盘生成时间**: <4分钟
- **持仓更新频率**: 每分钟 / 每日闭收

---

## 🚀 运行方式

### 实盘运行（带GUI）
```bash
cd e:\superTrader
python main.py
```
- 自动启动盯盘主循环 + GUI看板（子进程）
- 每5分钟扫描一次信号
- 实时推送到飞书

### 纯盯盘（无GUI）
```bash
python main.py --no-gui
```

### 仅打开复盘看板
```bash
python t_gui.py
```

### 单日回放（调试）
```bash
python replay_day.py
```

### 建仓扫描（离线）
```bash
python position_builder.py --no-feishu
# 或单个股票
python position_builder.py --code 000988 --no-feishu
```

### 日复盘生成
```bash
python t_io/validation/daily_review/daily_review.py --date 2026-08-25
```

---

## 📁 关键文件位置

| 文件 | 位置 | 说明 |
|-----|------|------|
| 主程序 | main.py | 实盘主循环 |
| 信号引擎 | signal_engine.py | 日内两点逻辑 |
| 建仓逻辑 | position_builder.py | 双通道建仓 |
| 配置 | config.py | PARAMS + STOCK_PARAMS |
| GUI | t_gui.py | 复盘看板 |
| 参数优化 | optuna_parameter_optimization.py | 参数搜索 |
| 日复盘 | t_io/validation/daily_review/daily_review.py | 自动复盘 |
| 前瞻跟踪 | t_io/validation/daily_review/forward_tracker.py | 收益回填 |

---

## 🔗 相关文档

### 快速参考
- [日内防御系统快速版](doc/guides/INTRADAY_SURGE_DEFENSE_QUICKSTART.md) - 5分钟了解
- [日内防御系统完整版](doc/guides/INTRADAY_SURGE_DEFENSE_GUIDE.md) - 完整设计

### 深度理解
- [精确入场框架](doc/guides/PRECISE_ENTRY_GUIDE.md)
- [Scheme A日评](doc/guides/SCHEME_A_DAILY_REVIEW_GUIDE.md)

### 参数详情
- [参数优化方案](doc/archive/proposals/param_optimization_20260825/)
- [Scheme A修复方案](doc/archive/proposals/scheme_a_fixes_20260821/)

### 系统优化
- [系统优化工程计划](SYSTEM_OPTIMIZATION_PLAN.md)
- [优化快速启动](OPTIMIZATION_QUICK_START.md)
- [优化决策详表](PHASE1_DETAILED_DECISIONS.md)

---

## 💾 数据保留策略

| 数据 | 保留期 | 说明 |
|-----|--------|------|
| decision_trace | 近1月 | 日内决策追踪 |
| sizing_advice | 全量 | 加仓建仓逐笔 |
| 分钟快照 | 全量 | 回测数据源 |
| 持仓快照 | 全量 | K2/K3对照 |
| 日志 | 近2周 | 系统运行日志 |
| 验证产物 | 全量 | 回测和验证结果 |

---

**上次更新**: 2026-08-25  
**下次更新**: 2026-08-26 (日常实盘后)

💡 如需查看更详细的内容，请参考 [doc/README.md](doc/README.md) 的完整导航。
