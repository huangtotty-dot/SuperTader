"""找「pandas 线程」都在跑哪条业务路径（决定该收哪个池）。

用法：PYTHONIOENCODING=utf-8 python tmp/find_pandas_paths.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import collections
import re
from pathlib import Path

txt = (Path(BASE) / "t_io" / "logs" / "gui_thread_dumps.log").read_text(encoding="utf-8", errors="ignore")
blocks = [b for b in txt.split("===== 按需线程栈") if "Thread 0x" in b]

FRAME = re.compile(r'File "([^"]+)", line (\d+) in (\w+)')
biz = collections.Counter()
pool = collections.Counter()
n_pandas = 0
for b in blocks:
    for t in re.split(r"\nThread 0x|\nCurrent thread 0x", b)[1:]:
        if "pandas" not in t and "numpy" not in t:
            continue
        n_pandas += 1
        for m in FRAME.finditer(t):
            f = m.group(1)
            if "superTrader" in f:
                biz[f"{Path(f).name}:{m.group(3)}"] += 1
            if "concurrent\\futures" in f:
                pool[f"futures:{m.group(3)}"] += 1
            if "ThreadPoolExecutor" in f or "_worker" in m.group(3):
                pool[f"{Path(f).name}:{m.group(3)}"] += 1

print(f"六个 dump 里共 {n_pandas} 个「pandas/numpy」线程")
print("\n=== 它们栈里的**业务帧** top 20 ===")
for k, v in biz.most_common(20):
    print(f"  {v:4}  {k}")
print("\n=== 它们栈里与线程池相关的帧 ===")
for k, v in pool.most_common(10):
    print(f"  {v:4}  {k}")

# 直接看一个完整样本
print("\n=== 样本：一个 pandas 线程的完整栈 ===")
for b in blocks:
    for t in re.split(r"\nThread 0x|\nCurrent thread 0x", b)[1:]:
        if "pandas" in t:
            print(t.strip()[:900])
            break
    else:
        continue
    break
