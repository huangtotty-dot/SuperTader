"""解析 gui_thread_dumps.log：统计最内层帧分布，定位 UI 线程卡在哪一帧。

用法：PYTHONIOENCODING=utf-8 python tmp/parse_thread_dump.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import collections
import re
from pathlib import Path

txt = (Path(BASE) / "t_io" / "logs" / "gui_thread_dumps.log").read_text(encoding="utf-8", errors="ignore")
blocks = txt.split("===== 按需线程栈")
print(f"dump 段数: {len(blocks) - 1}")

PREFIXES = [
    (r"C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\Lib\\site-packages\\", ""),
    (r"C:\\Users\\Lenovo\\AppData\\Local\\Programs\\Python\\Python311\\Lib\\", "stdlib/"),
    (r"E:\\superTrader\\", ""),
]


def shorten(f):
    for p, r in PREFIXES:
        f = f.replace(p, r)
    return f


last = blocks[-1]
threads = re.split(r"\nThread 0x|\nCurrent thread 0x", last)
sigs = collections.Counter()
stacks = {}
for t in threads[1:]:
    lines = [ln.strip() for ln in t.splitlines() if "  File " in ln]
    if not lines:
        continue
    m = re.search(r'File "([^"]+)", line (\d+) in (\S+)', lines[0])
    if not m:
        continue
    key = f"{shorten(m.group(1))}:{m.group(2)} {m.group(3)}"
    sigs[key] += 1
    stacks.setdefault(key, lines[:5])

print(f"\n=== 最内层帧分布（最后一次 dump，共 {sum(sigs.values())} 个线程）===")
for k, v in sigs.most_common(16):
    print(f"  {v:4}  {k}")

print("\n=== 关键帧的完整栈（UI 泵 / 回调 / 锁）===")
KEYS = ("create", "winforms", "_callback", "acquire", "Invoke", "_worker", "join", "wait")
for k in sigs:
    if any(x in k for x in KEYS):
        print(f"\n[{sigs[k]}×] {k}")
        for ln in stacks[k]:
            print("     ", ln)
