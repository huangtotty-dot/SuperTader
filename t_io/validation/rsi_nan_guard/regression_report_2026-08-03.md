# V1.1.2 RSI NaN 兜底（C 语义）回归报告 — 2026-08-03 实盘数据

## 修复对象与确诊路径

- 除零点（生产生效 3 处）：`indicators.py:36`（1分RSI）、`indicators.py:195`（5分RSI(14)）、`indicators.py:245`（15分RSI(6)）
- 同模式 dead code 2 处（共享命名空间被 indicators.py 遮蔽，同步修复防未来加载顺序变化踩雷）：`data_fetcher.py:921`、`data_fetcher.py:1034`
- 产生路径：`rs = gain / loss.replace(0, np.nan)` → 0/0 钉平窗（gain==0 & loss==0）→ rs=NaN → rsi=NaN
  → `signal_engine.py:690` `feats["rsi"] = float(last.get("rsi", 50) or 50)`（NaN 为 truthy，`or 50` 兜不住）
  → RSI超卖/RSI超买因子 NaN → buy/sell_score 双 NaN → 决策失明（HOLD_BELOW_THRESHOLD）
- 实证：2026-08-03 decision_trace 14:13:15 起 600176 nan fields=['rsi','buy_score','sell_score']；
  600176 尾盘 14:13~15:00 价格钉平（113 tick 中 107 个相邻不变）

## C 语义（父代理 2026-08-04 裁决）

- 0/0 钉平窗 → RSI 填 **50 中性**
- 纯上涨窗（loss==0 & gain>0）→ **保持 NaN**（与现网一致盲；若填数学真值 100 会激活满强度 RSI超买 卖压因子，属策略行为变更，登记为候选变更另行管线验证）
- 预热 leading NaN → 不变
- ffill 方案否决理由：携带值路径依赖——600176 尾盘钉平段前值为 0.0（钉平前全跌窗），ffill 会使 RSI超卖 买入因子满强度常开整段尾盘（假信号源）

## 回归 a/b/c（指标层，脚本 t_io/validation/rsi_nan_guard/regress_rsi_nan.py）

| 项 | 结果 | 数字 |
|---|---|---|
| a) 健康行为不变 | PASS | 5股×3周期（1m/5m/15m）旧有效值差异 **0** 处；旧有效→新NaN 回退 **0** 处 |
| b) 尾盘原 NaN 窗出分 | PASS | 尾盘（≥14:13）填充 **27** 条（600176×26 + 600481×1，14:15），全部 ==50.0 |
| c) 无副作用 | PASS | 填充共 **35** 条全部 ==50.0 且全部落在 0/0 钉平窗；纯上涨窗 **69** 条全部保持 NaN；违规 0 条 |

- 14:13 前填充 8 条：600481 10:50~10:56 + 11:14（盘中 0/0 钉平窗，同一 bug 修复范围，填中性 50）

## 回归 c（决策层，harness 双世界 diff，生产同源 SignalEngine.evaluate）

方法：`git worktree` 双世界——baseline=HEAD 未修复代码，C=修复代码；
同输入（t_io/minute_snapshots 当日快照 + HEAD 版 holdings.json=开盘持仓 4000 股 + `--ab v102`），
harness_backtest 逐分钟回放 2026-08-03，diff decision_trace（各 1185 行）。

- 信号流水：**完全一致**（baseline 6 条 = C 6 条：588170 SELL 10:00×1300 / 603667 SELL 10:23×100 /
  588170 SELL 10:30×700 / 000988 SELL 11:04×0 / 603667 SELL 11:27×100 / 000988 SELL 13:40×0）——**零新增非 HOLD 决策**
- 轨迹 diff：**仅 34 行**（全部为原失明窗口），34 行决策全部为 HOLD；
  34 行之外（含全部健康时段、全部纯上涨窗）**零差异**

### 原失明窗口逐条枚举（34 条，全部 HOLD，逐条 sanity check）

| # | 时间 | 代码 | 新rsi | buy_score(阈36) | sell_score(阈42) | 决策理由 | 阻断 |
|---|------|------|-------|----------------|------------------|----------|------|
| 1 | 10:50 | 600481 | 50.0 | 35.1 | 64.0 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 2 | 10:51 | 600481 | 50.0 | 35.1 | 62.6 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 3 | 10:52 | 600481 | 50.0 | 35.1 | 60.2 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 4 | 10:53 | 600481 | 50.0 | 35.1 | 57.9 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 5 | 10:54 | 600481 | 50.0 | 35.1 | 56.0 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 6 | 10:55 | 600481 | 50.0 | 35.1 | 61.0 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 7 | 10:56 | 600481 | 50.0 | 35.1 | 61.0 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 8 | 11:14 | 600481 | 50.0 | 34.9 | 54.1 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 9 | 14:14 | 600176 | 50.0 | 37.7 | 33.0 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 10 | 14:15 | 600176 | 50.0 | 37.7 | 33.0 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 11 | 14:15 | 600481 | 50.0 | 42.5 | 62.7 | HOLD_SELL_BLOCKED:strong_uptrend | buy:daily_breakdown_risk/daily_overheated; sell:strong_uptrend |
| 12 | 14:16 | 600176 | 50.0 | 37.7 | 33.0 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 13 | 14:38 | 600176 | 50.0 | 37.4 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 14 | 14:39 | 600176 | 50.0 | 37.4 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 15 | 14:40 | 600176 | 50.0 | 37.2 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 16 | 14:41 | 600176 | 50.0 | 37.2 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 17 | 14:42 | 600176 | 50.0 | 37.2 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 18 | 14:43 | 600176 | 50.0 | 37.2 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 19 | 14:44 | 600176 | 50.0 | 37.2 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 20 | 14:45 | 600176 | 50.0 | 37.3 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 21 | 14:46 | 600176 | 50.0 | 37.3 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 22 | 14:47 | 600176 | 50.0 | 37.3 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 23 | 14:48 | 600176 | 50.0 | 37.3 | 32.9 | HOLD_BUY_BLOCKED:daily_breakdown_risk | buy:daily_breakdown_risk |
| 24 | 14:49 | 600176 | 50.0 | 37.3 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 25 | 14:50 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 26 | 14:51 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 27 | 14:52 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 28 | 14:53 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 29 | 14:54 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 30 | 14:55 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 31 | 14:56 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 32 | 14:57 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 33 | 14:58 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |
| 34 | 14:59 | 600176 | 50.0 | 37.5 | 40.9 | HOLD_SELL_PRIORITY | buy:daily_breakdown_risk |

### 逐组 sanity check 结论

1. **600481 10:50~10:56 + 11:14（盘中钉平窗，8 条）**：新 sell_score 54.1~64.0 ≥ 卖阈 42，
   但 sell_block=strong_uptrend（强趋势禁卖，日线门控，与 RSI 无关、两世界一致存在）→ HOLD_SELL_BLOCKED；
   buy_score 34.9~35.1 < 买阈 36 且 buy_block=daily_breakdown_risk/daily_overheated → 合理，无交易。
2. **600481 14:15（1 条）**：buy_score 42.5 ≥ 买阈 36，但 buy_block=daily_breakdown_risk + daily_overheated
   （日线破位/过热门控，两世界一致存在）→ HOLD。该阻断在 baseline 世界同样生效（baseline 因分数 NaN 未走到阻断展示层），无交易。
3. **600176 14:14~14:16 + 14:38~14:59（尾盘钉平窗，25 条）**：buy_score 37.2~37.7 ≥ 买阈 36，
   全部 buy_block=daily_breakdown_risk（600176 当日阴跌破位，门控两世界一致存在）→ HOLD_BUY_BLOCKED；
   sell_score 32.9~40.9 < 卖阈 42 → 不触发卖出。RSI=50 中性本身未贡献任何 RSI 因子（RSI超卖/超买 均 0.0），
   分数由 VWAP/形态/5分RSI偏低等其他因子构成，与该股当日 14:13 前健康时段的因子结构连续一致 → 合理，无交易。

**结论：34 条原失明窗口在 C 语义下全部维持 HOLD，零新增交易信号；无"深跌钉平尾盘触发 BUY"类不合理信号。**

## 单测（t_io/validation/rsi_nan_guard/test_rsi_nan_guard.py，6/6 通过）

钉平→50 / 纯上涨→NaN 保持 / 预热 leading NaN 保持 / 正常窗与旧公式逐点一致 / 开盘即钉平自第2根填50 / 5m+15m 同语义。

## 已知边界与登记

- harness 与实盘的口径差异（1 tick/分钟、index_regime 固定 range、无实时买卖盘）：不影响本结论，
  因结论基于**同 harness 双世界差分**，两世界唯一变量即修复本身。
- 候选变更登记（复盘文档同步）：①B 类语义（纯上涨窗 RSI=100 数学真值，需 90 日管线 A/B）；
  ②signal_engine.py:690 `or 50` 兜不住 NaN 层（策略侧，与①合并评估）。
