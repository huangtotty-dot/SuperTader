# -*- coding: utf-8 -*-
"""core/market_data — 数据同源抽象层（合并实施方案 P1）
统一四个方法契约（daily/minute/snapshot/index_daily），主源 gm、腾讯兜底降级。
对外入口：get_provider()。
"""
def get_provider(*args, **kwargs):
    """惰性转发到 `facade.get_provider`（2026-10-11）。

    ⚠️ 为什么不能再用 `from .facade import get_provider`：本包**任何子模块**被导入都会
    执行本文件，而原写法会把 `facade`（连带 **pandas**）一起拉进来。实测
    `import core.market_data.codec`（该文件顶层只 import logging）也要 **2176ms**。

    这直接砸在 GUI 冷启动首帧上：`load_auto_status` 只用 `codec.to_internal` 做代码转换、
    `_compute_auto_pnl` 只用 `core.position_builder._DAILY_CACHE_DIR` 取路径常量，
    却各自拖起整条 pandas 导入链，而首帧的十几个 js_api 调用**会全堵在同一个 import 锁**上。

    改成函数内再 import ⇒ 只要不真的调 `get_provider()`，就不会触发 facade/pandas。
    对外接口不变（`from core.market_data import get_provider` 照用，共 31 处）。
    """
    from .facade import get_provider as _gp
    return _gp(*args, **kwargs)


__all__ = ["get_provider"]
