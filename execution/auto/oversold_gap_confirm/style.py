# -*- coding: utf-8 -*-
"""控制台着色（**纯展示**，可整体关闭）——包内模块 `style`（2026-10-15 施工）。

**为什么需要**：10:00 的判定过程（漏斗 / 首个未过关 / 最接近明细）一次要打 8~10 行，
无色时"哪一行是要紧的"要靠读，很慢；上色后一眼能扫到瓶颈和选中项。

**为什么放在这个包而不是 `_gm/gm_bridge`**：本包铁律是「不 import gm.api、依赖一律注入」
（见 `__init__.py`）；反过来让 `gm_bridge` import 本包又会造成分层倒挂。
故本模块**自成一体**：只产生 ANSI 色码，不认识控制台。

**日志不被污染的保证（两侧各管一段）**：
- 本模块：只负责**产生**色码
- `gm_bridge.ops_guard._TeeStream`：写**文件**时剥掉色码，写控制台时原样透传
⇒ 两侧都不需要知道对方的实现。

**默认关闭**。宿主 `gm_main.init()` 探测到控制台支持 ANSI 后才调 `enable(True)`。
未开启时 `c()` 原样返回字符串 ⇒ 任何环境（重定向、管道、不支持的终端）都不会出现裸转义序列。
"""
from __future__ import annotations

import re

_ENABLED = False

# ── 色码（SGR）──
RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
GREY = "\x1b[90m"

# ANSI 转义序列。`gm_bridge.ops_guard` 为保持分层（bridge 不该认识具体策略包）**自带了一份
# 等价正则**；本模块这份留给自测与将来可能的复用，两处口径必须一致（测试里钉了）。
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def enable(flag: bool = True) -> None:
    """开关着色。由宿主在探测到控制台支持 ANSI 后调用。"""
    global _ENABLED
    _ENABLED = bool(flag)


def enabled() -> bool:
    return _ENABLED


def c(text, *codes) -> str:
    """给 `text` 上色。未启用 / 没给码 ⇒ 原样返回（保证任何环境都安全）。"""
    if not _ENABLED or not codes:
        return str(text)
    return "".join(codes) + str(text) + RESET


def strip(text) -> str:
    """剥掉 ANSI 色码（写日志文件前用）。"""
    return ANSI_RE.sub("", str(text))
