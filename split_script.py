# -*- coding: utf-8 -*-
"""
拆分脚本：将 t_trader_v1.10.py 拆分为独立模块
"""
import os, sys, re

SRC = r"E:\06_T\t_trader_v1.10.py"
OUT_DIR = r"E:\06_T"

with open(SRC, "r", encoding="utf-8") as f:
    lines = f.readlines()
    total = len(lines)

print(f"源文件共 {total} 行")

# 定义拆分边界（基于之前 grep 的结果）
# 每个模块: (文件名, 起始行, 结束行)
# 注意：行号是 1-based，list index 是 0-based
modules = [
    ("config.py", 1, 346),           # 导入、常量、路径、PARAMS、日志、全局状态
    ("utils.py", 346, 712),          # 辅助函数到数据获取之前
    ("data_fetcher.py", 712, 1468), # 数据获取、指标计算
    ("signal_engine.py", 1468, 3174), # SignalEngine + 推送逻辑 + 交易计划
    ("preopen.py", 3174, 4970),     # 盘前分析
    ("learning.py", 4970, 5311),     # 学习、回放、复盘（放到主循环之前）
    ("main.py", 5311, 5363),         # 主循环和入口
]

# 写每个模块
for fname, start, end in modules:
    path = os.path.join(OUT_DIR, fname)
    content = "".join(lines[start-1:end])
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  -> {fname}: {end-start+1} 行")

print("\n拆分完成，接下来需要：")
print("1. 为每个模块添加正确的导入")
print("2. 修复模块间交叉引用")
print("3. 测试运行")
