# E:\06_T — 做T实盘系统 V2 swing2pt（纯两点日内 + 双通道建仓）

本目录承载做T实盘信号系统。2026-08-13 完成 **V2 swing2pt** 改造：日内信号引擎从加权评分/风控闸门/闭环追踪简化为**纯两点规则**；同日落地 W33 建仓加仓专项（双通道建仓 + 单档加仓）与日志数据层（G1~G4，支撑每日/每周 Review V2.1）。

---

## 一、核心架构

```
日内信号（signal_engine）    建仓（position_builder）      加仓（position_sizer）
┌──────────────────┐      ┌─────────────────────┐      ┌──────────────────┐
│ 纯两点规则        │      │ 双通道（参考级·未验收）│      │ 单档固定比例      │
│ 5分收盘触轨+RSI(6)│      │ 冰点反转 / 突破跟随  │      │ 接回×0.60        │
│ 恒推送·最小防重    │      │ 5分冰点=日内择时层   │      │ 首加×0.20        │
└──────────────────┘      └─────────────────────┘      └──────────────────┘
```

- **日内信号**：只参考两点（用户 2026-08-13 拍板），不再打分/门控/闭环/冷却。
- **建仓/加仓**：独立于日内信号的质量跟踪主轴（Review V2.1），走验证管线。
- **日志数据层**：sizing 逐笔落盘 → 每日 Review 全自动产出，喂周复盘持续优化。

## 二、日内信号：纯两点规则（signal_engine.py）

| 信号 | 触发条件 | 阈值（PARAMS） |
|------|----------|----------------|
| **SELL_HIGH 高抛** | 5分收盘触及上轨 **且** 5分RSI(6)>75 | `swing_bb_upper=1.0` / `swing_sell_rsi=75` |
| **BUY_LOW 低吸** | 5分收盘触及下轨 **且** 5分RSI(6)<35 | `swing_bb_lower=0.0` / `swing_buy_rsi=35` |

- 专用列 `rsi_5m_p6`（周期 6，`rsi_period_5m_swing`；不动 `rsi_5m`(14) 信息层）。
- 预热：至少 13 根 5 分钟 K 线（`swing_min_5m_bars`，约 10:35 后可出信号）。
- **恒推送**：两点满足即推，`qty=0` 也推（仓控0股标注"🔒满仓参考"，仅供参考不记账）。
- **最小防重**：同 (code, action, 5分钟桶) 每日只推一次。
- 已删除：ScoringEngine/FACTOR_WEIGHTS 评分链、RiskManager 风控闸门、闭环追踪、30min 冷却、轮次/日限。

### 关键模块

| 文件 | 职责 | 备注 |
|------|------|------|
| `main.py` | 主循环 + scan_once 编排 | exec 共享命名空间加载 14 模块 |
| `signal_engine.py` | 纯两点日内信号 + 决策 trace | `evaluate()` 只判两点，`engine="v2_swing2pt"` |
| `indicators.py` | 统一指标（1/5/15分钟） | 含 `rsi_5m_p6` 专用列 |
| `position_sizer.py` | 买卖数量计算（单档） | `calc_buy_qty` 接回×0.60/首加×0.20，ETF×0.25 |
| `position_builder.py` | 建仓扫描（双通道） | 参考级·未过 W33 A4 离线闸门 |
| `trend_regime.py` | 5分钟趋势状态机 | 已降级为信息层（仅 state/confidence 展示） |
| `index_regime.py` | 大盘日线态势 | 信息层 |
| `config.py` | PARAMS + STOCK_PARAMS + 飞书 | 纯两点/单档参数 |
| `data_fetcher.py` | 腾讯 ifzq + akshare + 快照 | 缓存 |

## 三、建仓：双通道（position_builder.py，参考级）

> ⚠️ **验收状态（2026-08-14）**：W33 A4 离线闸门未过（8 组参数变体全 FAIL，假阳性闸门 <40% 为硬约束）。双通道为**参考级·未验收**，信号仅供研究，不作跟单依据。

- **通道一 冰点反转（左侧）**：转向确认(40 必要：近5日MACD金叉 或 站上MA5) + BOLL冰点(20) + 缩量(20)；RSI(<35) 仅展示。signal=80 / approaching=60。
- **通道二 突破跟随（右侧）**：突破箱体(40) + 放量>1.5(30) + DIF>DEA(30)。signal≥70。
- **A2 5分钟层**：盘中为择时加分项（即时可建/待日内确认）；盘后冰点恒 approaching（待次日盘中确认）。
- **A3 股数对齐**：`suggested_qty = min(欠配缺口, ⅓目标批)`，复用 `config.build_position_gap`（与 GUI 仓位管理器同源）。

## 四、加仓：单档固定比例（position_sizer.py）

V2 纯两点后 strength 恒≥10（score=100/阈值36），三档失效 → W33 B2 简化为单档：

| 场景 | 个股 | ETF |
|------|------|-----|
| 接回（未接回量×比例） | ×0.60 | ×0.25 |
| 首加（t_qty×比例） | ×0.20 | ×0.25 |

## 五、日志数据层（支撑 Review V2.1）

| 产物 | 位置 | 用途 |
|------|------|------|
| `sizing_advice_{date}.jsonl` | `t_io/traces/` | 每次 sizing 调用逐笔落盘（买卖双侧） |
| `forward_tracker.py` | `t_io/validation/daily_review/` | 前瞻收益回填（加仓/建仓两张表，幂等） |
| `confirm_position` API | `t_gui.py` + web | 人工确认建仓 → 回写 signal_history |
| `daily_review.py` | `t_io/validation/daily_review/` | 日复盘自动产出（加仓观察/建仓扫描/阶段看板/sizing汇总/持仓准确性 K2K3） |

Review 体系（V2.1）：`doc/每日复盘/每日Review.md`（加仓+建仓双主轴）、`doc/每周复盘/每周Review.md`（唯一允许改参/上线的时点）、`doc/每周复盘/W33_*.md`（专项方案）。

## 六、运行方式

```bash
cd E:\06_T
python main.py                          # 实盘盯盘（自动拉起桌面 GUI 看板；--no-gui 禁用）
python t_gui.py                         # 仅打开盘后复盘决策看板（不盯盘）
python replay_day.py                    # 单日回放
python position_builder.py --no-feishu  # 建仓扫描（单股 --code）
python harness_backtest.py --codes 000988 --start 2026-07-24 --end 2026-07-24  # 回测
python t_io/validation/daily_review/daily_review.py --date 2026-08-13           # 日复盘数据
python t_io/validation/daily_review/forward_tracker.py --date 2026-08-13        # 前瞻回填
python t_io/validation/w33_offline_rescan.py                                    # 双通道离线闸门
```

> **GUI 看板**：`main.py` 默认以子进程启动 `t_gui.py`（pywebview 桌面壳，只读盘上 JSON/JSONL 数据，失败不影响盯盘）。建仓区块含「✓确认建仓」按钮（回写 signal_history）。

## 七、数据目录 t_io/

| 子目录 | 内容 | 保留策略 |
|--------|------|----------|
| `traces/` | decision_trace / shadow_signals / sizing_advice / position_builder | 日复盘按日期读取；历史定期清理（decision_trace 近 1 月） |
| `minute_snapshots/` | 盘中分钟快照（回测唯一数据源） | 全量保留 |
| `cache/` | 当日分钟线缓存 | 系统自管 |
| `logs/` | t_trader_sys_{date}.log + closure_audit 等 | 近两周，推送通道健康可查 |
| `state/` | holdings_{date}.json 归档快照（K2/K3 对照） | 全量 |
| `validation/` | daily_review / forward_tracker / w33 验证产物 | 全量 |

## 八、版本历史

| 版本 | 日期 | 关键变化 |
|------|------|----------|
| W33 专项 | 2026-08-13 | 建仓双通道 + 加仓单档 + J6 满仓推送拍板 + 日志数据层 G1~G4 |
| V2 swing2pt | 2026-08-13 | 日内信号改纯两点规则；删除评分链/风控/闭环/冷却/轮次日限 |
| V1.2.x | 2026-08-08 | C1' 买信号日限 + 满仓开关 |
| V3.0 | 2026-08-01 | 5分钟三层信号架构（趋势层/择时层/执行层，后随纯两点改造移除） |
| V2.0 | 2026-07-24 | V2 重写：状态机删除、阈值静态化 |
