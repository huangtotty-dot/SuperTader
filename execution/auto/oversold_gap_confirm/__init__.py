# -*- coding: utf-8 -*-
"""**超跌跳空确认** · 策略包（2026-10-12 归拢；原名「弱转强」，前缀 `osc_`）。

一个策略 = 一个文件夹。本包自带决策核、预筛、执行核、时点钩子与下单通道；
宿主 `gm_main.py` 只负责 gm 回调、共享服务与注入（见 §"宿主怎么用"）。

## 文件

| 文件 | 职责 | 是否 import gm.api |
|---|---|---|
| `decision.py` | 信号核（纯函数、零 IO）：`signal_ok` / `evaluate` | ❌ |
| `prescreen.py` | 预筛两层漏斗：韭研板块 → 涨停家数 top-N → 昨收<MA20 | ❌（facade 绝对路径加载/注入） |
| `executor.py` | 买/卖/早盘卖三条腿 + 持仓账本 + 留痕（注入式网关，可干跑） | ❌ |
| `backfill.py` | **买不进替补核**（纯函数 + 编排）：涨停价/封死判据/时效复检/额度顺移/撤单确认 | ❌ |
| `hooks.py` | 六个时点钩子 + 每日一次状态 + `on_bar` 分发 + 当日状态落盘 | ❌ |
| `gateway.py` | `OscGateway(GmGateway)`：策略 id 绑 `osc` 的下单通道 | ❌ |
| `runtime.py` | `Runtime`：宿主注入件（services/账本目录/时钟/日志） | ❌ |

**仓规**：`execution/auto/gm_main.py` 是本仓唯一 import `gm.api` 的文件 ⇒ 本包一律经
`Runtime`/`GmServices` 注入掘金能力。

## 宿主怎么用

```python
import oversold_gap_confirm as osc
OSC_RT = osc.Runtime(services=SERVICES, book_dir=_OSC_BOOK_DIR, is_live=_is_live, log=print)
...
osc.reset_day(today)                       # 按日重置（宿主在日期切换时调用）
osc.prescreen_init(OSC_RT)                 # P0-3：盘前预筛主路径（init 阶段调用，fail-open）
osc.on_bar(OSC_RT, context, now, today, t)  # 四个时点的时间窗/每日一次/模式闸都在包内
osc.executor.load_book(dir)                 # 账本（宿主归属判定也读它）
```

## 口径在哪

信号、参数、卖侧三件套、账本 schema、失败语义、研究证据与局限：
`doc/solutions/2026-10-12_超跌跳空确认_策略详解.md`。
**改动常量/信号口径 = 换策略**，须重做 exp 系列检验（见 `decision.py` docstring）。
"""
from __future__ import annotations

from . import backfill, decision, executor, gateway, hooks, prescreen, style
from .hooks import enabled, on_bar, prescreen_init, reset_day, sell_enabled
from .runtime import Runtime

# 策略元信息（宿主/框架按此登记；目前只有这一条策略）
ID = "osc"
NAME = "超跌跳空确认"
ORDER_TYPE = ID          # 事件桥 order_type 标签 = 策略 id（孤儿闸归属靠它）
PARAM_PREFIX = "osc_"
BOOK_DIRNAME = "weak_strong_book"   # ⚠️ 在线账本目录**刻意沿用历史名**（改名会让在途持仓失联）
HOOKS = (
    ("prepare", 931), ("morning_sell", 1000), ("buy", 1000), ("sell", 1450),
    # 买不进替补（2026-10-14）：总闸 `osc_backfill_enabled` **默认 False**，回测过关才开。
    # 时点由 `osc_backfill_first_hhmm`/`_interval_min`/`_rounds` 派生，默认 10:08 / 10:14。
    ("backfill", 1008),
)

__all__ = ["ID", "NAME", "ORDER_TYPE", "PARAM_PREFIX", "BOOK_DIRNAME", "HOOKS",
           "Runtime", "decision", "executor", "gateway", "hooks", "prescreen",
           "on_bar", "reset_day", "enabled", "sell_enabled", "prescreen_init"]
