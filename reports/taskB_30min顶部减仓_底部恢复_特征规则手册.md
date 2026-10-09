# 《30min 顶部减仓 + 底部恢复》特征规则手册

> 任务B 产出物 · superTrader 量化交易系统
> 总纲（owner 止损哲学）：「真正的止损应该是参考 30 分钟线找到顶部特征逐步减仓，在底部区域寻找支撑进行恢复仓位」。
> 本手册把"止损"从一次性全仓清仓闸，重构为 **30min 顶部特征 → 分档减仓；30min 底部支撑确认 → 分档恢复仓位** 的仓位调节状态机。所有规则均给出伪代码级判定条件（输入序列、阈值、确认规则、失效条件），可直接交给工程师实现。
> 数据口径约定：30min K线序列 `bars`（字段 `open/high/low/close/volume/ts`），A股日内 8 根 30min K线（10:00 / 10:30 / 11:00 / 11:30 / 13:30 / 14:00 / 14:30 / 15:00 收盘）。MACD 参数默认 (12, 26, 9)，DIF = EMA12 − EMA26（owner 指定以 DIF 快线为准，不以 MACD 柱为准）。

---

## 0. 总体状态机（仓位调节引擎）

```
状态: FULL(满仓=目标仓位100%) → REDUCING(减仓中) → DEFENSIVE(防守仓位=底仓下限) 
      → RESTORING(恢复中) → FULL

FULL      --顶部特征T1触发--> 减第1档 (100%→70%)
REDUCING  --顶部特征T2确认--> 减第2档 (70%→40%)
REDUCING  --顶部特征T3确认--> 减第3档 (40%→20%, 即防守底仓)
DEFENSIVE --底部信号B1触发--> 恢复第1档 (20%→40%)
RESTORING --底部信号B2确认--> 恢复第2档 (40%→70%)
RESTORING --底部信号B3确认--> 恢复第3档 (70%→100%)

任意状态  --价格有效跌破"最后防线"(见 §4.4)--> HARD_EXIT(清仓，唯一保留的硬止损)
任意状态  --顶部信号失效条件触发--> 回到上一级仓位档（防止误减仓踏空）
```

设计要点：
1. **替代 −8% HARD_STOP 的是"分档减仓"，不是"不止损"**。−8% 一次性清仓降级为 §4.4 的"最后防线"（仅结构性破位才触发，阈值建议放宽至 −12% 或跌破日线关键支撑）。
2. 减仓/恢复均为**三档**，档位比例 30%/30%/20%（见 §4），与社区主流"分档止损（10%/30%/60%）"与"金字塔减仓"实践一致（[OE分档止损](https://health.yxlady.com/214162.html)、[叩富网金字塔减仓](https://licai.cofool.com/ask/vipqa_7404801_42738427.html)）。
3. 所有信号以 **30min K线收盘确认** 为准（盘中不触发，避免毛刺）；T+1 约束见 §4.2。

---

## 1. 顶部特征（减仓触发器）

### T1 · MACD 顶背离（DIF 快线口径，owner 指定，权重最高）

**定义**：第二个 30min 价格高点的 DIF 值低于第一个高点的 DIF 值 → 顶部背离确认。价格创新高 + 动能不创新高 = 上涨衰竭。

**伪代码**：
```
输入: bars[30min], dif[], dea[]
参数: swing_lr = 3          # 摆动高点左右各3根确认（社区主流口径）
      max_gap  = 48         # 两个高点最多间隔48根30minK线（≈6个交易日）
      min_gap  = 4          # 至少间隔4根，避免同一根K线簇内重复计数
      dif_drop_min = 0      # DIF2 < DIF1 即可；严格版要求 (DIF1-DIF2)/|DIF1| ≥ 10%

1. 在 close[] 上找摆动高点 P1, P2: high[P] 为左右 swing_lr 根内最高
2. 条件: min_gap ≤ (P2 - P1) ≤ max_gap
        AND close[P2] > close[P1] * 1.001   # 价格创新高（0.1% 容差）
        AND dif[P2] < dif[P1]               # DIF 不创新高 ← owner 指定口径
        AND dif[P2] < dif[P2-1]             # DIF 已拐头向下（"结构"确认）
3. 确认规则: 满足以上 → T1 触发（预警）；
   加强确认(任一): ① DIF 死叉 DEA (dif 下穿 dea)；
                  ② P2 之后首根收盘 < P2 对应K线低点。
   预警即减第1档；加强确认叠加时直接视为 T1+T2 共振（减第2档）。
4. 失效条件: close 再创新高 AND dif 同步创新高（dif > dif[P1]）→ 背离被化解，
   撤销未执行的减仓；若已减第1档，不追回，等待底部信号恢复。
```

依据：[知乎·12万次MACD顶底背离量化（K线新高 + DIFF低于前高）](https://zhuanlan.zhihu.com/p/415823554)、[CSDN·双峰背离量化识别标准](https://blog.csdn.net/weixin_32487557/article/details/162589922)、[腾讯云·Python检测MACD背离（左右3根摆动高点）](https://cloud.tencent.com/developer/article/2755444?policyId=1003)、[天风·背离结构判定（DIF拐头=结构确认，复合背离）](https://kknews.cc/zh-sg/finance/j855q66.html)、[龙哥量化·MACD背离量化细节（背离化解条件）](https://www.cnblogs.com/long136/p/18321103)。

### T2 · 量价背离（价创新高量缩）

**伪代码**：
```
输入: bars[30min]
参数: vol_ma_n = 8          # 8根30min ≈ 1日均量
      shrink_ratio = 0.7    # 缩量阈值：量能 < 前高点量能的70%

1. 复用 T1 找到的相邻摆动高点 P1, P2（无背离高点时，用日内新高 vs 前一日高点）
2. 条件: close[P2] > close[P1]
        AND volume[P2] < volume[P1] * shrink_ratio
        AND volume[P2] < MA(volume, vol_ma_n)[P2]      # 且低于均量
3. 确认规则: 单发即可触发（减档）；若与 T1 同日共振，视为强顶部信号。
4. 失效条件: 后续K线放量（volume > volume[P1]）且收盘创新高 → 量价背离化解。
```

依据：[金荣圈·顶背离确认"第二次探顶成交量萎缩30%以上"](https://share.wolfinance.com/index/article/MjI0MzQyMg==)（经验阈值 30%，此处取 0.7）、[淘股吧·量价实操体系](https://m.tgb.cn/a/2odUqlYrU9Z)、[人人文库·量价背离定义](https://www.renrendoc.com/paper/454421424.html)。

### T3 · 顶分型 / 长上影（形态确认器，通常作为 T1/T2 的"扳机"）

**伪代码**：
```
输入: bars[30min]（先做包含关系处理，缠论标准）
1. 顶分型: 处理后连续3根K线 K1,K2,K3，
   high[K2] > high[K1] AND high[K2] > high[K3]
   AND low[K2] > low[K1] AND low[K2] > low[K3]
2. 强度分级(用于决定减档力度):
   强: K2 为长上影(上影线 ≥ 2×实体 且 上影线 ≥ 该K线振幅的50%)
       且 K3 收盘 < (K1.low + K1.high)/2   # 第3根跌破第1根中枢 → 最弱顶分型
   弱: 其余
3. 长上影独立信号: 单根30min K线 上影线 ≥ 2×|实体| AND 上影线 ≥ 振幅×50%
   AND 该K线出现在近 16 根K线高位区(close ≥ 近16根最高收盘的97%)
4. 确认规则: 强顶分型单独可触发减档；弱顶分型仅在 T1 或 T2 已预警时作为确认扳机。
5. 失效条件: 收盘向上突破 K2.high → 分型失效。
```

依据：[博客园·缠论分型定义（三根K线中间高点最高）](https://www.cnblogs.com/long136/p/17991062)、[知乎·顶分型力度判断（第2根长上影/十字星力度强）](https://zhuanlan.zhihu.com/p/24750987545)、[CSDN·最弱顶分型（第3根跌破第1根一半）](https://blog.csdn.net/weixin_39736547/article/details/111161896)、开源实现可借鉴 [hugo_chan（缠论Python框架，含分型/笔/区间套）](https://github.com/hugo2046/hugo_chan)。

### T4 · 均线压制（趋势过滤器）

**伪代码**：
```
输入: bars[30min]; 均线组 MA20, MA60（30min级别）
1. 空头压制: close < MA20 AND MA20 < MA60 AND MA20 向下斜率(dMA20/dt < 0)
   → 反弹至 MA20 ± 0.3% 区域 且 出现长上影/缩量 → 触发减档（反弹即是减仓点）
2. 死叉确认: MA20 下穿 MA60 且 close 连续2根收在 MA20 下方 → 强减仓信号
3. 失效: close 放量(>均量1.5倍)收复 MA20 → 压制解除
```

依据：[凤凰网·两条均线+30min顶背离高抛低吸实战](https://ishare.ifeng.com/c/s/7nT4JcrlLG9)、[BigQuant·天风量化择时"跌破20日线减仓"框架](https://bigquant.com/square/paper/de08f962-d9f7-4afb-aa88-aa2029a03af8)。（注：原文为日线口径，移植到 30min 级别为本项目适配，属经验规则。）

---

## 2. 底部恢复信号（仓位恢复触发器）

### B1 · MACD 底背离（T1 的镜像）

**伪代码**：
```
1. 找摆动低点 V1, V2（左右各3根内最低），min_gap ≤ V2-V1 ≤ max_gap
2. 条件: close[V2] < close[V1] * 0.999    # 价格创新低
        AND dif[V2] > dif[V1]              # DIF 不创新低 ← owner 口径镜像
        AND dif[V2] > dif[V2-1]            # DIF 拐头向上
3. 确认: DIF 金叉 DEA → 强确认
4. 失效: close 创新低 AND dif 同步创新低 → 背离化解，停止恢复
```

依据：同 T1 来源，底背离为顶背离镜像（[CSDN双峰背离表](https://blog.csdn.net/weixin_32487557/article/details/162589922)：第二低点更低 + DIF 第二低点更高）。

### B2 · 缩量止跌 + 放量反攻（量价双段确认）

**伪代码**：
```
阶段一(止跌): 连续 ≥2 根30min K线 volume < MA(volume,8) * 0.6   # 缩至均量60%
             AND 最低价不再创新低 (low[K] ≥ low[K-1] - 0.2%)
             → "止跌候选"
阶段二(反攻): 出现单根K线 close > open (阳线)
             AND volume > MA(volume,8) * 1.5              # 放量≥1.5倍均量
             AND close > MA(close,20) 或 close > 前一根K线.high
             → B2 触发
失效: 反攻K线后 4 根内收盘跌回反攻K线.low 下方 → 假反攻，撤销恢复
```

依据：[淘股吧·量能企稳信号"连续3日量<5日均量 → 放量阳线(量>10日均量, 涨幅≥3%)"](https://m.tgb.cn/a/2odUqlYrU9Z)（日线口径，比例移植到30min，0.6/1.5 为经验换算）、[约投顾·回调缩量至均量60-70%、反弹放量回均量以上为理想企稳](https://ag.yueniuzq.com/qa/gu-jia-suo-liang-hui-diao-dao-20-ri-jun-x-s2-70935/)、[360doc·缩量止跌于重要均线形态](http://www.360doc.com/content/24/0921/15/11685912_1134647057.shtml)。

### B3 · 关键支撑位企稳（前低 / 均线）

**伪代码**：
```
支撑位集合 S = { 前一交易日30min最低点, 日线MA10/MA20, 本轮下跌起点平台低点 }
企稳判定: |low[K] - s| / s ≤ 0.5% (盘中触及或略破支撑 s∈S)
         AND close[K] > s                          # 收盘收回支撑上方
         AND 其后 2 根K线收盘均 ≥ s
         AND (出现底分型 OR 缩量条件满足)
→ B3 触发
失效: 收盘跌破 s 超过 1% 且 2 根内未收回 → 支撑失效，禁止在该支撑恢复，
      且触发"支撑下移"（找下一个支撑位）
```

依据：[雪球·分时回踩均线不破即买点（上攻放量下跌缩量）](https://xueqiu.com/3915806985/171203167)、[约投顾·缩量回调至20日均线企稳](https://ag.yueniuzq.com/qa/gu-jia-suo-liang-hui-diao-dao-20-ri-jun-x-s2-70935/)、[CSDN文库·"盘中破线+收盘收回"细节](https://wenku.csdn.net/answer/49sqouvygofj)。

---

## 3. 信号评分与共振规则（决定减/增哪一档）

| 信号 | 单独触发力度 | 备注 |
|---|---|---|
| T1 MACD顶背离 | 减1档 | owner 指定主信号；DIF死叉DEA加强为减2档 |
| T2 量价背离 | 减1档 | 与T1同日共振 → 直接减2档 |
| T3 强顶分型/长上影 | 减1档 | 弱分型只做确认扳机 |
| T4 均线压制/死叉 | 减1档 | MA20下穿MA60 强信号 |
| B1 MACD底背离 | 恢复1档 | DIF金叉加强为恢复2档 |
| B2 缩量止跌+放量反攻 | 恢复1档 | 与B1共振 → 恢复2档 |
| B3 支撑位企稳 | 恢复1档 | 只做恢复，不做初始买入 |

**共振矩阵**：同日（或相邻2根K线内）出现 ≥2 个顶部信号 → 减仓力度 +1 档；顶部信号与"价格从日内高点回撤 ≥3%"叠加 → 再 +1 档（最多一次减至防守底仓）。恢复侧对称。此为经验规则（多信号确认降误判，参考 [头条·OBV+MACD必共振出货信号](https://www.toutiao.com/article/7603266735436874292/)）。

---

## 4. T+1 约束下的分档仓位管理模式

### 4.1 档位设计

```
减仓三档:  100% → 70% → 40% → 20%（防守底仓，保留）
恢复三档:  20% → 40% → 70% → 100%
```

- **保留 20% 防守底仓**：10-08/10-09 的教训是"清仓后深V踏空"。底仓不清 → 反弹时仍有持仓参与，且保留 T+1 可用的"可卖额度"。
- 每档间隔必须由**新的信号**触发（同一信号不得连减两档），且相邻两档间隔 ≥ 1 根 30min K线收盘，防止一根K线内连环减仓。

### 4.2 T+1 执行约束（关键工程规则）

```
可卖数量 = 持仓总量 − 当日买入量          # 只有昨日及以前买入的仓位可卖
可买恢复 = 无限制（资金足够即可），但当日买入部分当日不可再卖
```

操作含义（经验规则，A股制度约束）：
1. **减仓不受限**（只要卖的是老仓），因此顶部信号当日即可执行。
2. **恢复仓位当日形成"锁仓"**：下午 14:00 后恢复的仓位当日无法再减，因此 14:30 之后的恢复信号降权（只恢复半档，剩余半档次日早盘确认后补）。
3. **"减仓→等待→恢复"闭环理想节奏**：上午出顶部信号减仓 → 午后观察底部信号 → 尾盘或次日早盘支撑位恢复。这正是 owner 哲学在 T+1 下的自然形态。
4. 单日减仓总档数建议 ≤ 3（即最多减至底仓），对应社区"单日做T ≤3 次、单次 ≤ 底仓30%"的风控红线（[CSDN·分时做T风控清单](https://blog.csdn.net/weixin_42573113/article/details/165999998)）。

### 4.3 社区/开源实现借鉴情况

| 项目/社区 | 可借鉴内容 | 适配评估 |
|---|---|---|
| [vnpy (VeighNa)](https://www.vnpy.com/portal/) | `ArrayManager` 多周期K线序列+指标计算框架，`on_15min_bar` 式按周期驱动策略主干 | 本项目 GM SDK 已有自己的K线缓存（t_io/cache/daily_kline/），**借鉴其"K线序列→指标→按持仓状态分支"的策略结构**即可，无需引入框架 |
| [hugo_chan](https://github.com/hugo2046/hugo_chan) | 缠论分型/笔的完整 Python 实现（含包含处理、strict/loss/half 分型强度分级） | **T3 顶分型判定可直接移植其分型模块**，省去自写包含处理 |
| qlib | 通用 ML 量化平台，无现成"30min背离减仓"策略 | 参考价值低 |
| 掘金社区/聚宽 | [MACD顶背离风控策略示例](https://guorn.com/forum/post/p.2894710.334754772990154)（用顶背离做卖出风控） | 思路一致，口径简单（单背离即卖），本项目需升级为分档 |
| 网格/分档止损实践 | [OE分档止损 10%/30%/60%](https://health.yxlady.com/214162.html)、[雪球三三制（3批建仓+3批卖出，跌破强支撑3%才清仓）](https://xueqiu.com/6186913084/372328957) | **档位比例与"仅收盘破位才清仓"的纪律可直接借用** |
| BigQuant·天风择时 | [趋势+估值九宫格仓位梯度](https://bigquant.com/square/paper/de08f962-d9f7-4afb-aa88-aa2029a03af8) | 仓位梯度思想可借鉴，本项目简化为三档 |

结论：**没有完全现成的"30min顶部背离分档减仓+底部支撑分档恢复"开源实现可直接接入**；但 ①分型判定（hugo_chan）、②背离判定（知乎/腾讯云通用算法）、③分档纪律（三三制/分档止损）三块积木均成熟，工程上是组合+适配工作，而非从零研发。

### 4.4 最后防线（HARD_EXIT，替代现行 HARD_STOP）

现行"浮亏−8%全仓市价卖出"降级为唯一保留的硬止损，且条件收紧：
```
触发(需同时满足):
  ① 浮亏 ≤ −12%（放宽阈值，经验规则：给分档减仓留出工作空间）
  OR  ② 收盘有效跌破 日线MA60 / 本轮大级别平台低点（结构性破位，与浮亏无关）
执行: 次日开盘市价清仓（不在盘中低点市价砸出，避免 10-08/10-09 重演）
豁免: 若清仓信号出现时 B1/B2 已触发，则只减至防守底仓 20%，不清仓
```
依据：雪球三三制"跌破强支撑3%（收盘价）立即止损，不扛单"的纪律（[来源](https://xueqiu.com/6186913084/372328957)）+ 对 10-08/10-09 事件的直接修正（经验规则）。

---

## 5. 工程师实现清单（对接 superTrader 现有架构）

1. **数据源**：30min K线由 `t_io/cache/daily_kline/` 日线缓存日内合成，或 GM SDK `history_n` 直接取 30min bar；MACD/MA 在 `core/` 新增 `m30_features.py` 统一计算。
2. **信号层**：T1–T4、B1–B3 各为一个纯函数 `(bars) -> SignalResult{triggered, strength, invalidation_price}`，输出写入 `t_io/bridge/events_YYYYMMDD.jsonl` 的 `signal` 事件（复用现有事件桥）。
3. **仓位状态机**：新增 `execution/auto/position_ladder.py`，维护每股当前档位（100/70/40/20），与 `t_io/state/holdings_auto.json` 台账联动；减/增指令走 `sell_channels.py` / 买入通道，T+1 可卖数量校验必须在下单前完成。
4. **HARD_STOP 改造**：`sell_channels.py` 中现行 −8% 触发器改为 §4.4 条件，原逻辑保留为编译开关 `LEGACY_HARD_STOP` 以便 A/B 回测对比。
5. **回测验证**：用 2026-10-08/10-09 两日逐笔回放做第一优先级测试集——验证新机制在那两天"少卖在低点、反弹有仓位"。

---

## 6. 来源汇总

- MACD背离量化：[知乎·12万次背离回测](https://zhuanlan.zhihu.com/p/415823554) · [CSDN·双峰背离标准](https://blog.csdn.net/weixin_32487557/article/details/162589922) · [腾讯云·Python检测背离](https://cloud.tencent.com/developer/article/2755444?policyId=1003) · [天风·背离结构判定](https://kknews.cc/zh-sg/finance/j855q66.html) · [龙哥量化·背离细节](https://www.cnblogs.com/long136/p/18321103)
- 缠论分型：[博客园·分型定义](https://www.cnblogs.com/long136/p/17991062) · [知乎·分型力度](https://zhuanlan.zhihu.com/p/24750987545) · [hugo_chan 开源实现](https://github.com/hugo2046/hugo_chan)
- 量价关系：[淘股吧·量能实操](https://m.tgb.cn/a/2odUqlYrU9Z) · [约投顾·缩量企稳](https://ag.yueniuzq.com/qa/gu-jia-suo-liang-hui-diao-dao-20-ri-jun-x-s2-70935/) · [雪球·回踩均线](https://xueqiu.com/3915806985/171203167) · [金荣圈·缩量30%确认](https://share.wolfinance.com/index/article/MjI0MzQyMg==)
- 仓位管理：[雪球·三三制](https://xueqiu.com/6186913084/372328957) · [OE·分档止损](https://health.yxlady.com/214162.html) · [叩富·金字塔减仓](https://licai.cofool.com/ask/vipqa_7404801_42738427.html) · [CSDN·做T风控红线](https://blog.csdn.net/weixin_42573113/article/details/165999998) · [BigQuant·天风择时](https://bigquant.com/square/paper/de08f962-d9f7-4afb-aa88-aa2029a03af8)
- 框架参考：[vnpy/VeighNa](https://www.vnpy.com/portal/) · [聚宽·MACD顶背离风控策略](https://guorn.com/forum/post/p.2894710.334754772990154)
- 标注"经验规则"的条目（缩量0.6/放量1.5、档位30/30/20、−12%阈值、14:30后恢复降权等）为社区实践惯例 + 本项目适配推断，需回测校准。
