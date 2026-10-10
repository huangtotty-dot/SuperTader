"""验证：标签批算的并发度改动后，峰值线程数是否大幅下降。

背景：一次卡死现场 6 份线程栈里 25~38 线程常驻 pandas，160/162 来自 futures._worker，
业务帧集中在 _stock_tags_one/_stock_tags_from_df/get_trend30。该池原为 max_workers=40。

用法：PYTHONIOENCODING=utf-8 python tmp/probe_tags_threads.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, BASE)

import t_gui  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")

# 取一批真实标的（用持仓+watchlist 规模的量级）
api = t_gui.Api()
codes = []
try:
    cur = t_gui._load_json(t_gui.HOLDINGS_MANUAL, {}) or {}
    codes += [c for c in cur if isinstance(cur[c], dict)]
except Exception:
    pass
codes = [c for c in codes if c and not c.startswith("_")]
if len(codes) < 20:
    codes = [f"{600000 + i}" for i in range(40)]
codes = codes[:60]
print(f"标的数 {len(codes)}")

peak = {"n": threading.active_count()}
stop = False


def watch():
    while not stop:
        peak["n"] = max(peak["n"], threading.active_count())
        time.sleep(0.05)


threading.Thread(target=watch, daemon=True).start()
base = threading.active_count()

t_gui._TAGS_CACHE.clear()
t_gui._TAGS_RUNNING = False
t0 = time.perf_counter()
api.load_stock_tags_batch(codes)          # 冷缓存 → 后台起批算
kick = (time.perf_counter() - t0) * 1000
print(f"load_stock_tags_batch 返回耗时 {kick:.0f}ms（应很快=非阻塞）")

time.sleep(25)                            # 让后台池跑起来
stop = True
time.sleep(0.2)

print(f"\n起始线程={base}  峰值线程={peak['n']}  增量={peak['n'] - base}")
import collections  # noqa: E402
c = collections.Counter()
for th in threading.enumerate():
    nm = th.name
    if nm.startswith("ThreadPoolExecutor"):
        nm = "ThreadPoolExecutor-*"
    c[nm] += 1
print("当前线程构成:", dict(c.most_common(10)))
print("\n判定:", "PASS（增量应 <15，原 40 并发时约 +40）" if peak["n"] - base < 15 else "FAIL")
