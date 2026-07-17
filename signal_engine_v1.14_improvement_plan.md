# signal_engine V1.14 改进方案文档

## 1. 改进总览

本次改进围绕 **"多维度支撑位识别 + 开盘急跌旁路买入 + 决策透明化"** 三大需求，对 `signal_engine.py`、`main.py`、`data_fetcher.py`、`utils.py` 四文件进行了协同修改。

---

## 2. 具体改进点

### 2.1 多维度支撑位识别（signal_engine.py）

**问题**：原系统仅依赖 VWAP 和单一支撑判断，无法识别昨日低点、MA20、MA30 等多维度支撑。

**改进**：
- 新增 4 个支撑位识别维度：
  | 维度 | 数据来源 | 有效范围 |
  |------|----------|----------|
  | 昨日低点 | `daily_context["daily_prev_low"]` | 偏离 < 1% |
  | MA20 | `daily_context["daily_ma20"]` | 偏离 < 1% |
  | MA30 | `daily_context["daily_ma30"]` | 偏离 < 1% |
  | 15分低点 | `df_15m` 当日最低 | 偏离 < 1% |

- 在 `indicators` 中存储 `nearest_support` 字典（含 name/level/gap_pct），供后续 notify() 使用。

### 2.2 支撑位加分机制（signal_engine.py）

**问题**：触及强支撑时，系统仍按常规阈值（70分）要求买入，容易错失支撑位反弹机会。

**改进**：
- 当股价在任一支撑 1% 范围内时，按偏离程度加分：
  | 偏离程度 | 加分 | 买入阈值调整 |
  |----------|------|--------------|
  | < 0.3% | +18 | 65 → 47 |
  | < 0.5% | +12 | 65 → 53 |
  | < 1.0% | +6  | 65 → 59 |

- 低吸阈值从 `_buy_threshold = 70` 下调至 `65`（已全局修改）。

### 2.3 开盘急跌旁路买入（signal_engine.py）

**问题**：如特变电工 0703 案例，开盘 5 分钟内急跌 > 2% 且触及昨日低点，系统未给出任何买入信号，用户手动补仓后才在高位卖出。

**改进**：
- 新增 **"开盘急跌旁路"** 买入条件：
  - 时间窗口：09:30-09:35（开盘后 5 分钟）
  - 跌幅条件：`today_ret < -0.02`（已跌超 2%）
  - 支撑条件：触及任一支撑（gap < 1%）
  - 固定评分：60 分（不受常规阈值限制）
  - 买卖状态：优先接回（有未接回仓位时），否则首次建仓/加仓
  - 信号详情：`entry_kind = "open_dip_support"`，记录 `open_dip_reason` 和 `nearest_support`

**示例飞书通知**：
```
⚡ 旁路买入：开盘后急跌-2.5%，触及昨日低点(21.71)
📍 最近支撑：昨日低点 21.71（偏离0.15%）
```

### 2.4 决策透明化：飞书通知增强（main.py）

**问题**：用户反馈 "做决策时应该把过程透明化告诉我"。

**改进**：`notify()` 函数新增以下信息展示：
- **旁路买入标识**：若 `entry_kind == "open_dip_support"`，显示 `⚡ **旁路买入**：{原因}`
- **最近支撑信息**：显示 `📍 **最近支撑**：{名称} {价位}（偏离{百分比}）`
- **市场状态**：保持原有 `regime_info`（重压/出货等）
- **组合拳交易摘要**：保持原有已卖/已接回/未接回统计
- **风险提醒**：保持原有重压/出货状态下的接回限制

### 2.5 日线数据增强（data_fetcher.py + utils.py）

**问题**：`daily_context` 返回字典中缺少 `daily_prev_low`，导致 `signal_engine` 中的昨日低点支撑始终为 0。

**改进**：
- `data_fetcher.py` `_build_daily_context_from_df`：新增 `"daily_prev_low": float(prev["low"] or 0.0)`
- `utils.py` `_default_daily_context`：新增 `"daily_prev_low": 0.0`（默认值，数据不可用时安全回退）

### 2.6 VWAP 下方盘中禁买过滤器（signal_engine.py + main.py）

**问题**：华工科技 0703 案例，价格全天在均价（VWAP）下方运行（173.50 vs 178.90），日内卖方主导。如果在盘中（14:30 前）任何时间买入，都是逆势接飞刀，买入后会继续被套。

**改进**：
- **新增过滤器**：当 `price < vwap` 且 `today_ret < 0`（当前价格低于均价且当日下跌）时：
  - 若 `t_val < 1430`（14:30 之前）→ `buy_price_ok` 强制为 False，阻断所有常规买入
  - 若 `t_val >= 1430`（14:30 之后）→ 解禁，允许低吸（尾盘可能止跌反弹）
  - 例外：**强势回调**（`is_strong_pullback`）和 **趋势上涨**（`market_state == "trend_up"`）不受影响
  - 例外：**开盘急跌旁路**（`is_open_dip_support`）不受影响（09:30-09:35 急跌+支撑是特殊场景）

- **状态展示**：`scan_once` 状态面板新增 `"停手:日内弱势：价格低于均价且下跌，14:30前禁止买入"`，用户可直观看到系统为何暂停买入建议

**核心逻辑**：
```python
# buy_price_ok 修改：低吸条件（price <= vwap）仅在 14:30 后或非弱势时才有效
buy_price_ok = (price <= vwap and mom5 <= 0 
                and not (price < vwap and today_ret < 0 and t_val < 1430)) \
               or is_strong_pullback or market_state == "trend_up"

# buy_limit_reason 记录
if price < vwap and today_ret < 0 and t_val < 1430:
    buy_limit_reason = "日内弱势：价格低于均价且下跌，14:30前禁止买入"
```

## 3. 修改文件清单

| 文件 | 修改内容 | 行数变化 |
|------|----------|----------|
| `signal_engine.py` | 多维度支撑识别、加分逻辑、阈值下调、旁路买入、VWAP下方盘中禁买 | ~+55 行 |
| `main.py` | `notify()` 增强（支撑/旁路/决策透明化）、状态面板展示 buy_limit_reason | ~+20 行 |
| `data_fetcher.py` | `_build_daily_context_from_df` 新增 `daily_prev_low` | +1 行 |
| `utils.py` | `_default_daily_context` 新增 `daily_prev_low` | +1 行 |

---

## 4. 关键代码逻辑验证

### 4.1 支撑位加分与阈值调整
```python
threshold_adj = 0
if is_near_any_support:
    if nearest_support[2] < 0.003:
        score += 18; threshold_adj += 18
    elif nearest_support[2] < 0.005:
        score += 12; threshold_adj += 12
    elif nearest_support[2] < 0.01:
        score += 6; threshold_adj += 6

# 阈值降低：70 → 65，支撑加分后进一步降低
effective_threshold = _buy_threshold - threshold_adj
# 如 +18 分后，65-18=47，即触及强支撑时 47 分即可触发买入
```

### 4.2 开盘急跌旁路判断
```python
is_open_dip_support = False
open_dip_reason = ""
if 930 <= t_val <= 935 and today_ret < -0.02 and is_near_any_support:
    is_open_dip_support = True
    open_dip_reason = f"开盘后急跌{today_ret*100:.1f}%，触及{nearest_support[0]}({nearest_support[1]:.2f})"
```

### 4.3 旁路买入信号生成
```python
elif is_open_dip_support and can_buy_more and not self._in_cooldown(code, "BUY_LOW"):
    reasons = ["开盘急跌旁路"]
    if nearest_support:
        reasons.append(f"触及{nearest_support[0]}({nearest_support[1]:.2f})")
    bypass_score = 60
    sig = Signal(..., extra={
        "entry_kind": "open_dip_support",
        "open_dip_support": True,
        "nearest_support": indicators.get("nearest_support_name", ""),
        "support_level": indicators.get("nearest_support_level", 0),
    }, ...)
```

---

## 5. 待后续验证项

1. **语法验证**：当前环境无 Python 运行环境，建议用户在本地执行 `python -m py_compile signal_engine.py main.py data_fetcher.py utils.py` 验证语法。
2. **数据管道验证**：确认 `daily_context` 中 `daily_prev_low` 在运行时非零（可通过 `decision_trace` 日志检查）。
3. **特变电工回测**：用 0703 的本地 trace 数据验证 09:30-09:35 是否触发 `BUY_LOW` 旁路信号。
4. **华工科技回测**：用 0703 的本地 trace 数据验证 10:30-14:30 是否被正确阻断买入（状态应显示"停手:日内弱势..."），14:30 后是否解禁。
5. **market_regime 数据传入**：当前 `detect_regime` 仅接收 `preopen_data`，缺少 `daily_bars` 和 `minute_bars`，导致 `DISTRIBUTION`（连续2日长上影）和 `MORNING_SURGE`（早盘急拉后跳水）无法识别。建议后续修改 `scan_once` 传入完整数据。

---

## 6. 飞书通知示例（改进后）

### 示例1：开盘急跌旁路买入（特变电工）
```
🟢 【触发】低吸信号(BUY_LOW) 特变电工(600089) 得:60分

⚡ 旁路买入：开盘后急跌-2.5%，触及昨日低点(21.71)
📍 最近支撑：昨日低点 21.71（偏离0.15%）
股票：特变电工 (600089)
动作：低吸
现价：21.71
VWAP：22.15
评分：60
市场状态：ok
总T仓：3000 股/份

**触发原因**：
• 开盘急跌旁路
• 触及昨日低点(21.71)

**操作建议**：
建议买入 3000 股/份（首次加仓/建仓）
💡 参考卖出价位：22.33（VWAP上方0.8%）
```

### 示例2：日内弱势盘中阻断（华工科技 10:30）
```
⚠️ 【做T猎手状态】华工科技(000988)

股票：华工科技 (000988)
现价：180.50
VWAP：184.61
评分：38/52（多/空）
状态：停手:日内弱势：价格低于均价且下跌，14:30前禁止买入

**说明**：
当前价格 180.50 低于均价 184.61，且当日已下跌 -2.5%。
系统在 14:30 前暂停所有买入建议，避免逆势接飞刀。
14:30 后将重新评估是否出现止跌信号。
```

---

*文档生成时间：2026-07-03*
*版本：signal_engine V1.14 + VWAP盘中禁买*
