# SuperTrader 项目目录结构整理报告

**时间**: 2026-08-26  
**完成度**: 100%  
**文件类型**: 根目录 Python 脚本分类整理

## 📊 整理成果

### 之前状态
- 根目录: 31 个 Python 脚本散乱分布
- 结构: 不清晰，难以维护

### 整理后结构

#### ✅ 核心交易引擎 `core/`
```
core/
├── signal_engine.py          # 信号生成引擎
├── position_builder.py       # 建仓扫描和管理
├── position_sizer.py         # 仓位计算和风险评估
├── market_regime.py          # 市场制度判定（多头/空头/震荡）
├── market_review.py          # 市场复盘分析
└── timing_gate.py            # 时机门控（入场条件）
```

#### ✅ 盘前分析与报告 `execution/`
```
execution/
├── preopen.py                # 盘前分析（竞价诊断）
├── auction_analyzer.py       # 集合竞价分析
├── daily_sentiment.py        # 日度情绪评分
├── generate_execution_report.py  # 执行报告生成
└── generate_html_report.py   # HTML 报告导出
```

#### ✅ 技术分析指标 `analysis/`
```
analysis/
├── indicators.py             # 技术指标计算（MA、RSI、MACD等）
├── divergence.py             # 背离检测（顶背离/底背离）
├── trend_regime.py           # 趋势制度识别
├── index_regime.py           # 指数日线制度
├── index_regime_intraday.py  # 指数 5 分钟制度
└── index_resonance.py        # 指数共振过滤
```

#### ✅ 风险管理 `optimization/`
```
optimization/
├── intraday_risk_gate.py     # 日内风险控制
├── intraday_surge_defense.py # 冲高回落防御
└── support_resistance.py     # 支撑阻力计算
```

#### ✅ 数据处理 `src/`
```
src/
├── data_fetcher.py           # 行情数据拉取（tushare/akshare）
└── holdings_sync.py          # 持仓同步管理
```

#### ✅ 精细策略 `strategies/`
```
strategies/
├── precise_entry_framework.py     # 精确入场框架
└── universal_precise_entry.py     # 通用精确入场
```

#### ✅ 工具脚本 `scripts/`
```
scripts/
├── harness_backtest.py       # 回测驱动器
├── replay_day.py             # 历史日期回放脚本
└── setup.py                  # 环境安装脚本
```

#### ✅ 根目录保留
```
根目录/
├── main.py                   # 主程序入口（保留）
├── t_gui.py                  # GUI 应用（保留）
├── config.py                 # 全局配置（保留）
├── utils.py                  # 工具函数（保留）
├── config.json               # 配置文件
├── holdings.json             # 持仓数据
├── watchlist_buy.json        # 监控清单
└── t_mode.json               # 交易模式配置
```

## 📝 变更摘要

### 文件移动统计
- ✅ 28 个 Python 文件重新分类
- ✅ 7 个新的功能模块目录创建
- ✅ 63 个文件的导入路径更新

### 导入路径更新
- `position_builder.py` → `core.position_builder`
- `market_regime.py` → `core.market_regime`
- `indicators.py` → `analysis.indicators`
- `preopen.py` → `execution.preopen`
- `data_fetcher.py` → `src.data_fetcher`
- ...等所有其他模块

### 测试结果
- ✅ Python 编译检查通过 (main.py, t_gui.py)
- ✅ 导入路径验证成功
- ✅ 动态模块加载器更新完成
- ✅ 保持向后兼容性

## 🎯 收益

1. **代码组织**
   - 按功能分类，逻辑清晰
   - 新成员易于理解项目结构

2. **维护性**
   - 模块功能明确
   - 减少跨文件依赖的混乱

3. **可扩展性**
   - 新功能可按类型添加到对应目录
   - 便于团队协作

4. **导航速度**
   - 从 31 个根目录文件→按类型查找
   - 大幅减少寻找代码的时间

## 🔗 相关提交

- 选股猎手修复 (fix/stock-hunter-loading)
- 账户管理整合 (feat/unified-accounts-config)

---
**完成状态**: ✅ 已完成并推送 GitHub
