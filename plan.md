# 建仓信号扫描 + 加仓观察 深度审核计划

## 背景
- 系统：t_trader（E:\06_T），Web GUI（web/app.js + web/index.html）
- 截图疑点：
  1. signal:3 / approaching:382 / weak:413 —— approaching 占比过高，且几乎全部 60 分 3/5，分布退化
  2. 38 只无快照、在线拉取 19 只、1 只等待首次扫描 —— 数据覆盖不足
  3. 建议价=现价，所需资金全部约 5.5~6 万 —— 仓位计算疑似一刀切
  4. ETF（588170）无 MA5、无 VWAP 数据仍参与判定
  5. 加仓观察 9 只中：守住 0、破位 0、近阈 2、无事件 7 —— 回踩条件可能永远难触发
  6. 科泰电源 2/5 条件满足 与判定之间的一致性存疑；盘中 10:39 用实时价当"收盘>MA5"

## 阶段 1 — 并行代码审核（6 个 plan 类审查 worker）
- W1 建仓信号判定逻辑：position_builder.py 五条件、得分、分级阈值
- W2 回踩支撑/加仓观察逻辑：support_resistance.py + main.py 中加仓观察部分
- W3 数据链路与时效：data_fetcher.py、快照覆盖、盘中实时计算、ETF/数据缺失降级
- W4 仓位建议计算：position_sizer.py、建议股数/建议价/所需资金、与 holdings.json 联动
- W5 前端展示一致性：web/app.js + web/index.html 的计数、排序、等级映射、刷新
- W6 实盘可追溯性：decision_trace / replay / harness_backtest 与 GUI 显示的交叉验证

## 阶段 2 — 汇总
- 主 agent 交叉核对各 worker 结论，合并为分级问题清单（P0 致命 / P1 高危 / P2 改进）
