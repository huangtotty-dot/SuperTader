# 2026-11-27 W48 执行与数据链修复施工计划

## 任务边界

- 已获 owner 授权：修复候选㊺执行恢复链、B40 配置覆盖、G8 留痕及指数/breadth/IC 数据链。
- pack_v2 不重置观察期；S0 继续影子，不改其运行模式。
- 只修改 OSC 仿真生产链及离线验证代码；不连接柜台、不发真实/仿真委托、不 commit/push。
- 不改变 OSC 信号、gap=8%、Tcombo、止损等策略口径；B40 只是把已批准的 40 万配置真正接通。
- 技能：已加载 `a-share-t-trading-system`；其中旧 E:/06_T 做T路径不适用于当前 OSC 单策略仓，按当前代码与 AGENTS.md 执行。

## Stage 1 — 故障定位与审计（主代理 + 数据链审计员_W48）

- [x] 根因：`backfill.run_backfill_exec()` 撤单后只更新内存 `budget_view`，`order_intents` 到 hooks 收尾才同步；其间 `_execute_buy_plan_with_budget()` 先跑 `recover_order_intents()`，读到旧的活动无 ID 记录，触发 `recovery_order_id_missing`。
- [x] B40：`execution/auto/_gm/config/params.py` 显式 200000 覆盖 hooks 的 pack_v2 默认 400000。
- [x] G8：决策核已计算 `open_gap/rejected_gap/gap_capped`，`executor._write_decision()` 序列化遗漏。
- [x] 数据链审计员_W48：已完成 forming/指数/breadth/IC/daily_review 硬编码审计；形成最小修复与外部验收边界。

## Stage 2 — P0 执行与配置施工（主代理）

1. 撤单/竞态成交确认后，在任何恢复检查前，按 `intent_id + order_id` 原子同步既有意图账；写回失败必须闭锁，不得假装释放。
2. 真正“SDK 结果未知且无 order_id”的记录仍保持 fail-closed；不得把本修复扩大成无 ID 也放行。
3. `backfill_rounds` 增加 `budget_bound_breaches` 明细，避免只有笼统 note。
4. `PARAMS['osc_daily_budget']` 改为 400000，并用测试钉住生产显式配置，而非只测 DEFAULTS。
5. 回归：11-12/11-24/11-27 同型场景、撤单终态、部分成交、竞态成交、写回失败、重复恢复。

## Stage 3 — G8 与数据链施工

1. `decision_{date}.json`：picks/spares/stocks 带 `open_gap`；顶层带 `rejected_gap`、`gap_capped` 明细；保持纯展示写失败不阻断交易。
2. 按只读审计结果做最小数据链修复：缓存状态显式化、历史日不得使用当前快照、breadth/IC 缺失不得伪装完整。
3. 外部接口不可用时明确标暂读/陈旧；测试只证明代码行为，不声称外部数据已经恢复。

## Stage 4 — 验证与交付

- 先跑新增用例，再跑 OSC 相关模块，最后 `python scripts/run_regression.py --fast`。
- 视改动范围运行相关数据链/复盘测试；全量测试若未跑不得宣称全绿。
- 检查 `git diff`，确保无生产账本改写、无日志大批变更、无越权提交。
- 输出施工结果与下一交易日盘前验收点；文件生成在当前工作区。

## Stage Gate

- 执行链：已确认撤单终态必须同步到磁盘后，恢复检查才允许继续；任何同步失败都不得发候补单。
- G8：正常入选、候补和超帽剔除都可从 JSON 独立审计，且 NaN 不落入文件。
- 数据链：离线行为测试通过；外部数据可用性另标“待实盘数据源验收”，不与代码通过混为一谈。

## 施工结果（2026-11-28）

- 候选㊺：撤单终态/竞态成交在恢复检查前同步意图账；同步失败继续禁止候补 BUY。旧 fills-only 回放不再误报，唯一 order_id 可找回既有 intent_id；真实无 ID、结果未知仍 fail-closed。
- B40：GM 显式 `osc_daily_budget=400000`，与 pack_v2 默认一致；观察期不重置。
- G8：入选、候补、超帽剔除均落 `open_gap/rejected_gap/gap_capped`；11-03 历史回放测试固定使用当时的 `gap_cap=None`，不回溯套用新规则。
- 数据链：指数目标日缺失/占位行不再生成伪 α/β；今日行修复三态化并先于复盘/预下载；历史复盘不再借用当前持仓；breadth 支持 as-of/EOD 硬边界；IC 陈旧面板禁止生产调权。
- 实测：OSC 六模块 `Ran 109 tests, OK`；数据链五模块 `Ran 46 tests, OK`；`python scripts/run_regression.py --fast` 83 项、0 失败。
- 施工全程未连接柜台、未发真实或仿真委托、未 commit/push；施工前已有的两个运行态文件改动未处理。

## Stage 4 遗留项处置（Stage 5 已续作）

1. 下一交易日仍须核验 OSC 实际补单留痕、B40 40 万生效及 G8 JSON；真实委托链不能靠离线测试或 watcher 心跳代替验收。
2. breadth 显式当日 EOD 已接入今日行修复成功钩子；失败单独留痕，不回滚修复成功状态。
3. IC 默认日期已改由本地已完成指数日线解析，法定节假日可自然回退；日历不可证明时禁止生产调权。生产 `monitor_status.json` 本轮未重生成。
4. S1 完成口径与 IC 依赖、假说 `--date` 历史 as-of、chart cache 当日 EOD 证明均已完成代码施工及离线测试。
5. `equity_daily.py` 已加固个股 bar 完整性；缺票或只有残缺缓存值时，正式净值/收益/α 置空并标 `partial_position_valuation`，合法空仓仍按现金账户计算。
6. `.gitignore` 已只放行 `t_io/validation/daily_review/daily_review.py`；运行状态、账本及其他产物继续忽略。

## Stage 5 — W48 遗留项续作（2026-11-28）

1. [x] **breadth 成功钩子**：今日行修复成功后，后台单飞构建目标日 EOD breadth；只允许该受控入口包含当日行。
2. [x] **IC 日历**：显式 `--date` 用于历史回放；默认日期从本地已完成指数日线推导，来源无法证明时标记未核验并禁止生产调权。
3. [x] **S1 完成口径**：失败记录不进入完成集合；无新增与未证实空返回分开留痕；未完成/失败阻断后续阶段，verify 成功后才刷新 IC。
4. [x] **假说 as-of**：H1~H5 按目标日缓存结算，禁止用当前快照结算历史事件或使用写死基准涨幅。
5. [x] **chart cache 与净值**：当日 EOD 必须有受控证明；缺合法 EOD 价时正式净值口径 fail-closed。
6. [x] **Git 忽略**：复盘源码可被普通提交包含；只验证规则，未执行 add/commit/push。
7. [x] **在线无损验收**：完成指数、东财备用源、日历和本地 GM 状态核查；未主动发单。

### 最终验收结果

- OSC 原六模块：`Ran 109 tests, OK`。
- 数据链、breadth、IC、S1、假说、chart/equity 及既有图表防线：`Ran 118 tests, OK`。
- `python scripts/run_regression.py --fast`：83 项，0 失败、0 基线外新增。
- `git diff --check`：通过；仅有 Git 的 LF→CRLF 提示，无空白错误。
- 腾讯只读门面：`sh000300/sh000001/sh000688` 均取得末行 2026-11-27 的有效日线；东财备用源直调三只指数也均取得同一末行。此结果不代表 GM 已恢复。
- 本地日历解析：`as_of=2026-11-27`、`calendar_verified=true`、`calendar_source=local_index_daily:sh000300`。
- 最终 Git 状态未出现新的生产净值或 IC/S1 状态产物；`trade_history_ledger.json` 与 `weak_strong_book/positions.json` 仍是施工前已有差异，保持不动。

### 仍待下一交易日或独立验收窗口

1. 在线走一遍「今日行修复成功 → breadth EOD → 复盘/预下载 → 净值落盘」，检查实际产物中的证明、缺票和失败状态。
2. 核验 OSC 补单恢复、40 万预算和 G8 留痕的真实运行结果；不得为验收主动制造委托。
3. 全量 S1 本轮未运行，生产 IC/panel 未重生成；需在独立窗口执行并验收新完成口径与 IC 刷新链。
4. 受管 Python 仍无 `gm` SDK；watcher 有心跳且当天只有 `strategy_exit`，没有委托验收证据。GM panel、连接状态和真实委托回报链仍待下一交易日只读核查。
5. 受管环境无 `pytest`，本轮使用项目 `unittest` 与回归脚本完成验证，未安装依赖。

全程未连接柜台发单、未运行全量 S1、未 add/commit/push。

## Stage 6 — 全量 S1 与 IC 在线续作（2026-11-28）

- [x] 核查运行入口：`scripts/s1_data_update.py all` 的 probe/universe/fetch 必须使用 GM `history_n`，脚本明确指定用户 Python 3.11；受管 Python 没有 `gm.api`。
- [x] 核查项目环境：`pyproject.toml` 只有开发工具配置，没有项目虚拟环境或替代执行器；研究依赖清单也不提供 GM SDK。
- [x] owner 已明确授权使用用户 Python 3.11 运行全量 S1。
- [x] 用户解释器实测为 Python 3.11.9，`gm.api` 可正常导入，终端 token 获取链路未报缺失。
- [x] 初始 GM 只读探针两次失败：`status=1010, 无法获取掘金服务器地址列表`；owner 恢复终端后未沿用旧失败结论。
- [x] 11:08:27 探针成功：GM 终端在线，600519 最新日线 `eob=2026-11-27`。
- [x] 进程核查：`gmterm-serv.exe` 于 10:56:30 启动，四个 `gsgm3.exe` 进程存在。
- [x] 后台任务 `bash-obn3o4gl` 已结束（约 709 秒），但**不是全链路成功**：probe 成功；universe 为 5,682 只，新增/移除/新退市均 0、更名 11 只；fetch 完成 5,682/5,682，失败 0、未证实空返回 0，本轮归档漂移股 78 只；面板达到 9,920,357 行、末端 2026-11-27。
- [x] 首次 derived 失败原因是缺少 `results/daily_selection_screen_2026-10-09/summary.csv`。已从 Git 历史 `1f1e2b7` 恢复原始 F3 summary；恢复后的 ICIR 权重为 REV10=0.2564、GAP=0.1283、AMOUNT_CHG=0.3370、PRICE_POS60=0.2783，随后 derived 成功。
- [x] 查明默认验收基线错位：`baseline_2026-11-16/scores.parquet` 实际仅到 2026-10-09（2,486,601 行，mtime 2026-10-10 02:39），而 backup 按面板截止日命名为 11-16。错误对拍产生 950 行 old-only、108,275 行 new-only，nav 最大偏差 8.78%，不能作为有效策略差异结论。
- [x] 为断档恢复给 `factors/verify` 增加共用 `--baseline-date`，显式锁定同一原始 ST 分界日；最终复跑状态机测试 6 项通过，`python scripts/run_regression.py --fast` 83 项、0 失败、0 基线外新增。
- [x] 使用原始基线 2026-09-17 重跑 factors、derived、verify：factors 4,082,660 行；scores 2,626,355 行、664 日、末端 2026-11-27；基准段 2,446,289/2,446,289 行全覆盖，无 old-only/new-only。
- [x] **硬验收仍失败**：`score_eq/score_icir/rev10_z` 最大绝对差分别为 `6.57e-05/3.50e-05/2.73e-05`；nav 619/619 日齐全，但最大相对偏差 `1.932757e-03`，高于既定 practical 闸 `1e-3`，故 `exact_pass=false`、`practical_pass=false`。最大偏差日为 2025-11-10（旧 2,716,825.37，新 2,722,076.34），偏差中位数为 `7.846327e-04`。
- [x] 只读诊断补齐归因边界：78 只漂移股差异中位数更高，但最大单项差异也出现在非漂移股；截面 z-score 会联动其他股票，因此现有证据支持“前复权数据代差及截面联动”，不支持“全部差异只来自漂移股”。诊断脚本为 `scripts/experiment/diagnose_s1_baseline_20261128.py`，不写生产产物。
- [x] owner 于 11:52 明确批准本次 `0.1933%` 数据代差一次性例外；报告保留 `exact_pass=false`、`practical_pass=false`，新增 `accepted_with_exception=true` 及完整授权证据，全局 `1e-3` 闸未修改。
- [x] IC 已于 11:56:39 刷新并复跑确认：`data_status=ready`，`panel_end=expected_panel_end=2026-11-27`，`ic_end=2026-11-19`（h5 自然滞后）；独立日历核验为 `local_index_daily:sh000300`、`verified=true`。`production_weight_change_allowed=true`，但未实际修改任何生产权重；GAP 连续 14 日预警、无告警，维持观察。
- [x] 首次 IC 刷新出现 Windows GBK 子进程输出解码警告，状态本身已成功；已为 IC 子进程显式指定 UTF-8/`errors=replace`，第二次刷新无警告。状态机 6 项与 fast 83 项再次全绿。
- [ ] 后续加固：`cmd_backup` 应记录并校验 scores/nav 的真实最大日期；与面板 cutoff 不一致时提前 fail-fast，避免完成 5,682 只抓取后才暴露“假 11-16 基线”。
- 边界：不换腾讯/东财替代 GM 研究面板，不安装依赖，不触碰委托链，不 add/commit/push，两份既有账本继续不动。
