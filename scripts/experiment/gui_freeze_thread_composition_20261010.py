"""统计 gui_thread_dumps.log 里所有线程「在干什么」，定位线程数从哪来。

用法：PYTHONIOENCODING=utf-8 python tmp/parse_thread_composition.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import collections
import re
from pathlib import Path

txt = (Path(BASE) / "t_io" / "logs" / "gui_thread_dumps.log").read_text(encoding="utf-8", errors="ignore")
blocks = [b for b in txt.split("===== 按需线程栈") if "Thread 0x" in b]
print(f"dump 段数: {len(blocks)}")

CATS = [
    ("堵在 pywebview 返回路径(semaphore.acquire)", ["edgechromium.py\", line 160"]),
    ("堵在 pywebview Invoke", ["edgechromium.py\", line 152"]),
    ("在 UI 线程里创建线程(Thread.start)", ["threading.py\", line 969 in start"]),
    ("pandas / numpy 计算", ["pandas\\", "numpy\\"]),
    ("网络 I/O", ["socket.py\", line 8", "http\\client.py", "ssl.py\", line", "urllib3\\", "urllib\\"]),
    ("等 GM 单worker池(_gm_call)", ["facade.py\", line 137"]),
    ("等锁/Event/join", ["threading.py\", line 327", "threading.py\", line 331",
                       "threading.py\", line 479", "threading.py\", line 629"]),
    ("json 解析", ["json\\__init__.py"]),
]

for i, b in enumerate(blocks):
    hdr = re.search(r"按需线程栈[^=]*", b)
    live = re.search(r"活线程=(\d+)", b)
    threads = re.split(r"\nThread 0x|\nCurrent thread 0x", b)[1:]
    cnt = collections.Counter()
    for t in threads:
        hit = None
        for name, pats in CATS:
            if any(p in t for p in pats):
                hit = name
                break
        cnt[hit or "其他"] += 1
    total = sum(cnt.values())
    print(f"\n--- dump#{i+1} 活线程={live.group(1) if live else '?'}（本段解析 {total}）---")
    for k, v in cnt.most_common():
        print(f"   {v:4}  {k}")
