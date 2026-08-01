# E:\06_T — 做T实盘系统 V3.0（5分钟三层信号架构）

本目录承载做T实盘信号系统。2026-08-01 完成 V3.0 重构，核心变化：**从 1 分钟指标为主 → 5 分钟 MACD/BOLL/RSI 三层信号架构**。

---

## 一、V3.0 核心架构：三层信号体系

```
第一层：趋势层（5分钟线 MACD+BOLL）→ 定方向
  输出: trend_state ∈ {STRONG_BULL, BULL, NEUTRAL, BEAR, STRONG_BEAR}

第二层：择时层（5分钟 RSI）→ 在趋势方向上找买卖点
  输出: rsi_buy_trigger / rsi_sell_trigger

第三层：执行层（1分钟 VWAP/ATR/形态/风控）→ 保留原有体系
  接收趋势层门控（逆势信号抑制）
```

### 关键模块

| 文件 | 职责 | V3.0 变化 |
|------|------|-----------|
| `trend_regime.py` | **新** 5分钟趋势状态机 + RSI 择时触发器 | 全新 |
| `indicators.py` | **新** 统一指标计算（1/5/15分钟） | 从 data_fetcher 拆分 + 新增标准 5分钟指标 |
| `signal_engine.py` | 评分引擎（FeatureExtractor + ScoringEngine + RiskManager） | 集成趋势层评分+方向门控+T_MODE |
| `config.py` | 全局参数 + STOCK_PARAMS + 飞书推送 | 删除 SHORT_MODE_PARAMS |
| `data_fetcher.py` | 数据获取（腾讯 ifzq + 缓存） | 指标函数由 indicators.py 在 exec 链中覆盖 |
| `main.py` | 主循环 + scan_once 编排 | 新增 indicators 到 module_order |

### 5分钟指标（indicators.py）

| 指标 | 参数 | 列名 | 用途 |
|------|------|------|------|
| MACD | (12, 26, 9) | `dif_5m`, `dea_5m`, `macd_hist_5m` | 趋势层：零轴位置判定多空 |
| BOLL | (20, 2.0) | `bb_mid_5m`, `bb_width_5m`, `bb_pct_5m` | 趋势层：中轨斜率+带宽确认 |
| RSI | (14) | `rsi_5m` | 择时层：超买超卖点捕捉 |

### 评分权重（FACTOR_WEIGHTS V3.0）

| 因子 | 权重 | 变化 |
|------|------|------|
| 5m_trend | 0.15 | **新增** |
| 5m_rsi | 0.10 | **新增** |
| vwap | 0.15 | 曾 0.20 |
| rsi(1m) | 0.04 | 曾 0.12 |
| macd | 0.08 | 不变 |
| volume | 0.08 | 不变 |
| position | 0.08 | 不变 |
| ema | 0.04 | 不变 |
| pattern | 0.13 | 曾 0.20 |
| index_regime | 0.15 | 曾 0.20 |

### 方向门控

- **STRONG_BEAR**：买入分数 × 0.3 + 门槛 +12（大幅抑制逆势买入）
- **BEAR**：买入分数 × 0.6 + 门槛 +6
- **NEUTRAL**：双向放行
- **BULL**：卖出分数 × 0.6 + 门槛 +6
- **STRONG_BULL**：卖出分数 × 0.3 + 门槛 +12（大幅抑制逆势卖出）

---

## 二、运行方式

```bash
cd E:\06_T
python main.py          # 实盘盯盘
python replay_day.py    # 单日回放（07-24）
```

## 三、配置文件

| 文件 | 用途 |
|------|------|
| `holdings.json` | 持仓（cost/qty/base/t_qty/pre_close），**每日收盘后更新 pre_close** |
| `t_mode.json` | 正T（long）/ 反T（short）逐股配置 |
| `config.json` | 飞书 webhook、报警类型、扫描开关 |

## 四、数据目录 t_io/

| 子目录 | 内容 | 保留策略 |
|--------|------|----------|
| `traces/` | decision_trace / shadow_signals | 近 5 个交易日 |
| `minute_snapshots/` | 盘中分钟快照（回测唯一数据源） | 全量保留 |
| `cache/` | 当日分钟线缓存 | 系统自管 |
| `logs/` | 运行日志 | 近两周 |

## 五、每日维护

1. 收盘后更新 `holdings.json` 各标的 `pre_close`
2. 如需切换正T/反T，改 `t_mode.json`
3. traces 目录每 1-2 周手工清理

## 六、版本历史

| 版本 | 日期 | 关键变化 |
|------|------|----------|
| V3.0 | 2026-08-01 | 三层信号架构：5分钟 MACD/BOLL/RSI 趋势层 + 方向门控 + T_MODE 集成 |
| V2.0 | 2026-07-24 | V2 重写：状态机删除、阈值静态化、FACTOR_WEIGHTS |
| V1.26 | 2026-07-14 | higher-low + STOCK_PARAMS 个股参数 |
