# -*- coding: utf-8 -*-
import os, sys

SRC = r"E:\06_T\t_trader_v1.10.py"
OUT_DIR = r"E:\06_T"

with open(SRC, "r", encoding="utf-8") as f:
    lines = f.readlines()
    total = len(lines)

print(f"源文件共 {total} 行")

# 修正后的拆分边界（行号 1-based, end 是 inclusive）
modules = [
    ("config.py", 1, 345),      # 导入、常量、路径、PARAMS、日志配置
    ("utils.py", 346, 711),      # 全局变量、辅助函数到数据获取之前
    ("data_fetcher.py", 712, 1467),  # 数据获取、日线、分钟线、指标（到SignalEngine之前）
    ("signal_engine.py", 1468, 3173), # SignalEngine + 信号推送 + 拍卖 + 交易计划（到PreOpenContext之前）
    ("preopen.py", 3174, 4969),   # 盘前分析（到scan_once之前）
    ("main.py", 4970, 5363),      # 扫描、主循环、入口
]

for fname, start, end in modules:
    path = os.path.join(OUT_DIR, fname)
    content = "".join(lines[start-1:end])
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  -> {fname}: {end-start+1} 行")

print("\n拆分完成")
