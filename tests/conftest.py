# -*- coding: utf-8 -*-
"""测试全局夹具。

`gui._pool_rows` 的**冷算**（整表无旧值）2026-11-24 起默认挪后台 —— 生产上 646 只冷算
实测 43s，同步跑会把 pywebview 串行 js_api 队列整段堵死（owner 报 UI 卡顿的元凶之一）。
离线测试需要确定性（调用后立刻拿到 rows），这里统一把同步开关打开；
个别测试若想验证 warming 分支，可在用例内自行 monkeypatch 回 False。
"""
import pytest


@pytest.fixture(autouse=True)
def _sync_pool_rows_cold():
    try:
        import gui
    except Exception:      # 纯静态测试（只读源码）不需要
        yield
        return
    old = getattr(gui, "_POOL_ROWS_SYNC_COLD", False)
    gui._POOL_ROWS_SYNC_COLD = True
    try:
        yield
    finally:
        gui._POOL_ROWS_SYNC_COLD = old
