#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""快速测试 t_gui.py 是否能正常启动 API"""

import sys
import json
from pathlib import Path
from t_gui import Api

# 测试 API 初始化
try:
    api = Api()
    print("✓ Api 类初始化成功")
except Exception as e:
    print(f"✗ Api 初始化失败: {e}")
    sys.exit(1)

# 测试关键方法
try:
    dates = api.available_dates()
    print(f"✓ available_dates() 成功: 找到 {len(dates)} 个日期")
    if dates:
        print(f"  最近日期: {dates[0]}")
except Exception as e:
    print(f"✗ available_dates() 失败: {e}")
    sys.exit(1)

try:
    day = api.load_day(dates[0] if dates else None)
    if "error" in day:
        print(f"⚠ load_day() 返回错误: {day['error']}")
    else:
        print(f"✓ load_day() 成功: 获取日期 {day.get('date')}")
except Exception as e:
    print(f"✗ load_day() 失败: {e}")
    sys.exit(1)

print("\n✓ 所有测试通过，t_gui.py 应该可以正常启动")
