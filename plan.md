# 2026-11-23 进一步架构优化可能性核查

## 背景
- 10-26 已完成一轮实时性/负载优化（P0/P1 修复，未复发）
- 11-22 已完成 UI 实时性深度诊断 + P0/P1/P2 施工 + 铁律工程化（f4c1727 已推送）
- 本任务：核查**更宽维度**的架构优化空间（存储/模块边界/进程/配置/运维）
- ⚠️ 今日周一开盘中：施工只写磁盘不热重载，重启才生效

## 已知摸底数据（主代理已查）
- gui.py 17,359 行；web/app.js 15,431 行；core/ 59 个模块
- t_io 总量 16GB / 36,489 个 JSON：validation 9.4GB（GP因子挖掘产物）+ cache 5.2GB = 91%
- t_io/logs 432 个文件 166MB；traces 181MB；minute_snapshots 135MB
- 昨日挂账：6 处 TencentProvider 未入守卫、retention_patrol 未接调度、P0-1 部分成交终态未覆盖等

## Stage 1 — 三路并行只读审计（explore × 3）✅ 完成
- A 存储架构师 / B 后端架构师 / C 前端与基建架构师

## Stage 2 — 汇总评估 ✅ 完成
- 产出 doc/solutions/2026-11-23_架构优化机会评估.md
- 核心结论：债在"双份"和"没人丢"，不在"大"

## Stage 3 — P0 施工（4 项）✅ 全部完成（当日，用户「直接开始」）
- P0-1 双脑对拍闸门 test_regime_parity.py 28 项 + 两默认值对齐
- P0-2 飞书限频退避+死信重投（gm_bridge/feishu.py 重写 + config.py 同款）22 项
- P0-3 retention 防事故修正 + 清理 3.4GB（_bak 594MB + tushare_mins 2.8GB）
- P0-4 beacon 组包节流 10s（根因=预热期指纹逐拍变）守卫 25+11 绿
- 合并验证 105 项 + beacon 11 项全绿

## Stage 4 — P1 施工（7 项）（当日，用户「请继续修改/继续」）
- P1-1 ✅ retention_patrol 接调度（周六 15:50 dry-run 推飞书，永不自动 --apply）27 项绿
- P1-2 ✅ 子进程模板化（E1：subprocess_job.py + review_worker.py + run_daily_review 子进程化）55 项绿；遗留：真实 LLM 路径待 GUI 重启后 e2e
- P1-3 ✅ 测试入口固化（E2：run_regression.py 三口径——--fast 83条2.1s exit 0 / 全量 2141 条 247s 基线 39 id 全基线内 exit 0 / --update-baseline；39 条基线逐条定因；AGENTS.md ⑤ 更新）
- P1-4 ✅ 守卫扩范围（E3：analysis/stock_hunter/sector_rotation/scripts，6 豁免 8 实例钉死+反向断言）
- P1-5 ✅ 成交终态测试钉 5 用例 + **买侧账本 bug 修复**（终态写回 filled_qty/vwap/fee，预算结算前）OSC 189 项绿；追加 config 撞名隔离（合并验证根因修复）
- P1-6 ✅ 首片 diag_metrics + chart_cache 5 吞点计数化 + metrics.jsonl 每日落盘 42 项绿；**次批 ✅ gui.py 207 处全计数化**（swallow_codemod.py 建成+备份；唯一豁免=模块级 stdout 点先于 import；300s 落盘 reporter 挂 __main__ tag="gui"）
- P1-7 ✅ validation 化石 7.3GB 归档仓外（t_io 16G→5G）
- **P1 合并验证 9 模块 139 项全绿（16.7s）**

## Stage 5 — 收尾 ✅ 完成
- [x] agent-388 验收（--fast 独立复跑 exit 0；全量 2141 条 247s 基线外 0）
- [x] 评估文档 + plan.md 进展已同步
- [ ] 全部改动未提交 git，等用户「上传」
- [ ] 生效时机提醒：GUI 重启 → beacon 节流 + 复盘子进程化；自动盘重启 → 飞书限频 + 买侧账本修复
- [x] E/F 族 13 条修复转绿（E=config 路径序污染→preload 钉包；hunter=tearDown 句柄竞态）+ `--update-baseline` 基线 39→26（移除 13/新增 0，--fast 复核 exit 0）
- [x] gui.py 207 处吞点计数化（P1-6 次批，见上）
- [x] execution/ 196 处静默吞点计数化（P1-6 三批：swallow_codemod_execution.py，AST 定位，pass 替换/continue/return/break 前插，auto 系 ImportError 兜底注入；40 文件编译过+导入冒烟过）
- [x] P2 批次（用户「请继续」）：P2-3 慢日志 JSONL 化（前后端 tab+rid 串链）✅；P2-4 依赖卫生（删 8 死依赖+requirements 三分组）✅；P2-5 echarts 空闲预热 ✅；P2-6 隐藏期降频 ×3（pricealert 保频+回可见补拍+flying 闸）✅；P2-1 锁定不动（长假条件）；P2-2 评估后并入 P2-1 同窗口（验证手段=重启 GUI 点验，实盘日做不了）
- [ ] 后续：无（P1/P2 全部清零，剩 P2-1+P2-2 待长假窗口）

## P2（2026-11-23 批次后状态）
- ✅ 已完成：慢日志 JSONL 化（P2-3）、依赖卫生（P2-4）、echarts 预热（P2-5）、隐藏期降频（P2-6）
- 🔒 待长假窗口：gui.py 四刀拆分（P2-1，需守卫改注册表）、app.js 分文件（P2-2，验证需重启 GUI 全页面点验）
