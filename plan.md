# 2026-10-27 日复盘执行计划

> 唯一清单：`doc/review/复盘清单.md` V3.1。`dailyReview/每日复盘清单.md` 已作废，仅作历史存档。
> 已加载 `a-share-t-trading-system`；其中 E:\06_T 旧环境说明不适用于当前重构后的 E:\superTrader，按现行清单与代码证据执行。

## 阶段 1：证据采集（只读）
- 主线程：OSC 四时点产物、成交/拒单、账本与账户、净值与风控。
- 子任务「日志与通道审计员」：进程、心跳、backtrace、飞书推送、异常与重启。
- 子任务「manual 质量审计员」：manual 建仓/加仓、timing_gate、前瞻表与样本进度。
- 输出：可复核的文件路径、字段、数值，不做参数修改。

## 阶段 2：盘后补跑与结算
- 按清单运行 equity_daily、ic_monitor、daily_review、指数缓存刷新及必要的前瞻结算。
- 若失败，保留错误证据并区分数据缺口、执行故障和既有问题。

## 阶段 3：复核与报告
- 对照红线、额度、SL5、到期、账本一致性、S0 探针和 E1 口径逐项验收。
- 汇总 owner 已批准的候选⑩排期及昨日到期票执行情况，延续未决议题。
- 输出 `doc/review/dailyReview/20261027/2026-10-27_复盘.md` 与 evidence 证据目录。

## 阶段 4：质量门禁
- 核对报告数值与原始 JSON/日志；事实、判断、动作分开。
- 每日只记录不改参；不改其他会话正在编辑的 GUI/web 文件，不提交运行时 positions.json。

---

# 2026-10-27 P0 成交回填修复计划（owner 15:32 授权）

## 阶段 1：最小代码修复
- 子任务「回填修复工程师」处理 `execution/auto/oversold_gap_confirm/fillbackfill.py`：去除回填读 JSON 对 `core.utils -> config.log` 的宿主路径耦合，不改交易规则。
- 新增隔离回归测试，模拟 auto 的 `config` 模块阴影，覆盖 BUY/SELL fill 与 BUY reject；测试只能使用临时目录，严禁接触生产台账。

## 阶段 2：测试与副本重放
- 运行定向 pytest；检查现有 fillbackfill 测试并防回归。
- 备份今日账本相关文件到复盘 evidence；在临时副本按时间顺序重放 `events_20261027.jsonl`。
- 副本验收：open=账户5只、已卖5只进入 closed、买入成本=真实成交价、拒单与成交摘要=终态。

## 阶段 3：正式对账与归档
- 仅在副本全过后对生产 `weak_strong_book` 重放同一事件；不重启策略、不下单。
- 复核 positions/fills/sells/day_state，账户逐票一致后更新复盘报告和修复证据。
- 代码修复独立 commit，只提交回填模块与测试；运行台账按仓库纪律不提交，GUI/web 等其他会话改动一律不碰。
