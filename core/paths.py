# -*- coding: utf-8 -*-
"""仓库路径注册表（零依赖、零副作用）。

路径推导以 config.py 原有逻辑为正本；本模块只提供字符串常量，不创建目录、
不读写文件，也不导入 config，供轻量模块安全复用。
"""
import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T_IO_DIR = os.path.join(BASE_DIR, "t_io")
STATE_DIR = os.path.join(T_IO_DIR, "state")

# 跨进程共享快照。
QUOTE_BUS_FILE = os.path.join(STATE_DIR, "quote_bus.json")
QUOTE_BUS_LOCK_FILE = os.path.join(STATE_DIR, "quote_bus.lock")

# 已推送/已见事件去重状态。
HUNTER_BUILD_PUSHED_FILE = os.path.join(STATE_DIR, "hunter_build_pushed.json")
HUNTER_MA5_PUSHED_FILE = os.path.join(STATE_DIR, "hunter_ma5_pushed.json")
POSITION_SIGNAL_PUSHED_FILE = os.path.join(STATE_DIR, "position_signal_pushed.json")
MA_BREAK_PUSHED_FILE = os.path.join(STATE_DIR, "ma_break_pushed.json")
MA_RECLAIM_PUSHED_FILE = os.path.join(STATE_DIR, "ma_reclaim_pushed.json")
DIVERGENCE_PUSHED_FILE = os.path.join(STATE_DIR, "divergence_pushed.json")
DAILY_DIVERGENCE_PUSHED_FILE = os.path.join(STATE_DIR, "daily_divergence_pushed.json")
MA5_PULLBACK_PUSHED_FILE = os.path.join(STATE_DIR, "ma5_pullback_pushed.json")
INTERCEPT_NOTICE_PUSHED_FILE = os.path.join(STATE_DIR, "intercept_notice_pushed.json")
INDEX_DIVERGENCE_SEEN_FILE = os.path.join(STATE_DIR, "index_divergence_seen.json")
