# -*- coding: utf-8 -*-
"""自动盘执行层（P4-1 迁入，goldminer 执行层新规范副本）。

gm_main.py      唯一 import gm.api 的入口（gm 回调/闸链/下单/台账）
sell_channels.py P0-P6 卖出通道 + _sell_arbiter（★独立边界，逻辑与迁移前一致）
sell_state.py    sell_state.json 跨日状态（pos_key 指纹）
_gm/             goldminer 支撑模块整体副本（config/data/signals/utils/gm_bridge/analysis）
"""
