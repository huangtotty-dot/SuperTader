# E:\06_T — 做T实盘系统 + 三度猎手选股

本目录承载两套每日运行的系统：**做T实盘信号系统（主）** 与 **三度猎手选股系统**。
2026-07-17 完成目录清理，移除了失效模块、测试脚本与过期数据，以下为当前真实结构。

---

## 一、做T实盘系统（t_trader，当前 V1.26）

### 运行方式

```bash
cd E:\06_T
python main.py
```

### 模块加载链（main.py 以共享命名空间 exec 顺序加载）

```
main.py
 ├─ import log_enhancer.py          # 日志增强：事件流水、eod复盘、missed_signals
 ├─ exec 链（顺序固定，后者可引用前者）
 │    config.py                     # 全部参数、STOCK_PARAMS 个股定制、飞书发送 send_feishu_payload
 │    utils.py                      # 工具函数、快照/preopen 落盘
 │    data_fetcher.py               # 取数：Tushare stk_mins 1分钟线、分钟缓存、持仓读取
 │    multi_timeframe_fetcher.py    # 多周期：由1分钟线构造5/15min、日/周/月线MA
 │    signal_engine.py              # 评分引擎：买卖评分、场景因子、状态机
 │    preopen.py                    # 盘前上下文、replay学习、buy_starvation 状态
 │    market_regime.py              # 大盘状态判断（读取当日 preopen_trace）
 │    position_sizer.py             # 仓位计算
 └─ config.py 动态挂载 system_alert_v17_3.py   # 声音报警
```

注意：`feishu.py` 已删除——飞书推送由 `config.py` 内置 `send_feishu_payload` 经 Webhook 直发。

### 配置文件

| 文件 | 用途 | 维护要求 |
|------|------|----------|
| `holdings.json` | 持仓（9 条：账户A 5 只 + 账户B 3 只 + 华工科技B户）。含 cost/qty/base/t_qty/type/account/**pre_close** | **每日收盘后更新 pre_close**，否则 today_ret/open_gap 计算失真 |
| `t_mode.json` | 正T（long，先买后卖）/ 反T（short，先卖后买）逐股配置 | 启动时未设置会提示选择；signal_engine 第683行读取 |
| `config.json` | 飞书 webhook、报警类型（auto/gentle/normal/urgent/critical）、扫描开关 | 改动即生效 |

### 信号与通知

- 买入通知阈值 **68**；卖出 **75（10:00 前）/ 65（10:00 后）**
- 版本要点：V1.15 场景化因子（GAP_BOUNCE20/SPIKE20/FADE15/LIMIT_UP_BLOCK/DOUBLE_TOP15）→ V1.17 5分钟量能反转 → V1.18 弱势震荡/45度斜率/VWAP穿越 → V1.20 场景因子状态机 → V1.21 高点确认延迟 → V1.22 大阳线反包确认 → V1.23 双顶保护精细化 → **V1.24 STOCK_PARAMS 个股定制参数**（`_get_params()` 优先读个股再回退全局）→ **V1.25 早盘预警**（开盘5分钟跌>0.8% 触发，10:30 前禁买）→ **V1.26 连续低点抬高支撑**（higher-low，买+15/门槛-8）

### 数据环境

- 分钟数据：Tushare `stk_mins`（仅约近 6 个交易日），时间格式 `YYYY-MM-DD HH:MM:SS`
- `akshare` 不可用，通过 MockAkshare 注入绕过
- 飞书 Webhook 配置于 `config.json`（勿外泄）

---

## 二、三度猎手选股系统（V17.10）

```bash
run_with_config.bat        # 或直接 python selection_v17.10.py
```

| 文件 | 用途 |
|------|------|
| `selection_v17.10.py` | **自包含单文件**扫描器（全A扫描、概念/市值/ST过滤、飞书推送） |
| `strategy_menu.py` | 策略菜单（selection 惰性引用） |
| `watchlist.json` | 股票池（约1.7MB，含名称/sector/概念标签） |
| `strategy_config.json` | 策略配置 |
| `cache\`、`logs\`、`results\` | 运行时工作目录（自动生成内容） |

> 2026-07-17 说明：6 月曾拆出的 11 个模块（scan_engine/stock_pool 等）因与做T版 config.py 不兼容（import 即崩）已删除，git 历史可查。选股固定走单文件入口。

---

## 三、数据目录 t_io\（做T系统读写，勿删）

| 子目录 | 内容 | 保留策略 |
|--------|------|----------|
| `traces\` | decision_trace / shadow_signals / preopen_trace 等决策轨迹（按日 .jsonl） | **仅保留近 5 个交易日**（2026-07-17 清理后约 34 个文件）；代码无自动清理，需定期手工瘦身 |
| `minute_snapshots\` | 盘中分钟快照 `{code}_{date}.json`（2026/05、06、07） | **全量保留**——回测唯一历史数据源 |
| `preopen\` | 盘前数据（按日 .json） | 近两周 |
| `logs\` | t_trader_sys 运行日志 + ai_review 日志 | 近两周 |
| `cache\` | 当日分钟线缓存 | 系统自管，自动过期 |

---

## 四、运营工具

- `daily_review.py` — 每日收盘复盘：读取 t_io\traces 的 decision_trace/shadow_signals 生成复盘（配合收盘反馈调参流程）
- `backtest.py` — 通用回测：仅处理当前持仓，产出 backtest_report.json

## 五、archive\ 与 git

- `archive\`：历史回测报告与调参文档（v1.17/v1.18 报告、V1.26 回测文档等），仅存档不参与运行
- git 基线：`5356770`（2026-07-17 清理后）。被删文件均可从 git 历史找回：
  `git checkout <commit> -- <文件名>`

## 六、每日维护清单

1. 收盘后更新 `holdings.json` 各标的 `pre_close`
2. 如需切换正T/反T，改 `t_mode.json` 或启动时选择
3. traces 目录每 1-2 周手工清理一次（保留近 5 个交易日即可）
