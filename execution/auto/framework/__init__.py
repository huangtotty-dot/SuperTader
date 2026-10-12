# -*- coding: utf-8 -*-
"""策略框架（2026-10-12 起）——**多策略共存的接缝层**。

## 为什么有这一层

owner 2026-10-12：「后面有可能会不断拓展策略 ⇒ 策略需要封装增强模块化设计」。
此前 gm_main.py 把「掘金宿主 + 通用下单通道 + 单条策略逻辑」揉在一个文件里（4146 行，
删旧引擎后 1473 行）：加第 2 条策略必须改 gm_main 本体，且会立刻踩到四个共享账户问题
（归属串台 / 持仓归属 / 资金仲裁 / 每 bar 数据扇出）。

## 边界（硬约束）

- **本包一律不得 `import gm.api`** —— 仓规：`execution/auto/gm_main.py` 是唯一 import gm.api 的文件。
  所有掘金能力经 `services.GmServices` **注入**（`gm_main` 建一次，传给策略/网关）。
  好处：本包可离线单测、可干跑、可换纸面网关。
- **本包不做策略决策** —— 决策在各策略包里（如 `oversold_gap_confirm/decision.py` 纯函数核）。

## 已落地（P0+P1）

| 模块 | 作用 |
|---|---|
| `services.py` | `GmServices`：策略/网关能看到的全部掘金能力（唯一注入点） |
| `ownership.py` | `Ownership`：**一票一策略归属表 + 硬锁**（owner 2026-10-12 拍板） |
| `gateway.py` | `GmGateway`：通用下单通道（原 `_OscGmGateway` 平移 + 归属锁 + 策略 id 标签） |

## 待落地（owner 已拍板的口径，尚未实现）

- **资金仲裁 = 各自声明额度 + 先到先用**：每个策略声明 `daily/single` 上限，host 在下单时按
  调用顺序扣减共享剩余（额度是上限不是预留）。现状由各策略自己对着实时现金做，尚未抽成共享件。
- **注册表 + 每日一次状态对象**（现为 `_OSC_*_DONE_DATE` 模块级全局）。
- **每 bar 共享快照**（真瓶颈是逐股出网等待，不是 CPU）。

见 `doc/solutions/2026-10-11_系统效率诊断与冗余清单.md` 与 memory `legacy-engine-deleted-and-renamed-osc`。
"""
