# -*- coding: utf-8 -*-
"""自动盘执行层（P4-1 迁入，goldminer 执行层新规范副本）。

gm_main.py            唯一 import gm.api 的入口（gm 回调 / 超跌跳空确认三时点钩子 / 下单 / 台账）
framework/            策略框架（2026-10-12 起）：GmServices 注入 / 通用下单网关 / **一票一策略归属硬锁**
oversold_gap_confirm/  **唯一策略「超跌跳空确认」的实现包**（决策核/预筛/执行核/四时点钩子/网关）
                       入口 `osc.on_bar(OSC_RT, …)`；口径详解见 doc/solutions/2026-10-12_超跌跳空确认_策略详解.md
build_decision_auto.py   GUI 侧决策适配（core/build_decision 的取数适配）
_gm/                   goldminer 支撑模块整体副本（config/data/signals/utils/gm_bridge/analysis）

⚠️ 2026-10-12 owner 决策「超跌跳空确认接管 / 旧引擎删干净」后**已删除**：
   sell_channels.py（P0-P6 卖出通道）/ sell_state.py（跨日卖出状态）/ t_engine_auto.py（做T信号引擎）
   / backtest_holdings.py / replay_verify.py（两者的回测·回放驱动）/ ogr_shadow_glue.py（OGR 胶水）。
   它们服务的旧镜像做T引擎、OPEN_ALIGN、OGR 均已一并删除，勿照旧文档复原。
"""
