"""逐个端点测线程增量：定位冷启动 43 个 ThreadPoolExecutor 是谁生的。

用法：python tmp/probe_thread_src.py
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

NAMES = ["load_day", "load_position_manager", "load_auto_pnl", "load_auto_status",
         "load_ob_analysis", "load_quotes", "load_market_score", "load_stock_tags_batch",
         "load_index_divergence", "load_live", "load_console", "available_dates"]

print(f"起始线程 {threading.active_count()}\n")
print(f"{'端点':<26}{'耗时ms':>9}{'线程增量':>9}  新线程名")
print("-" * 90)
for nm in NAMES:
    # 每次都用全新 Api（清掉实例级缓存），保留类级缓存以模拟真实冷启动第二波
    api = t_gui.Api()
    for a in ("_indices_cache", "_turnover_cache", "_turnover_hist_cache"):
        if hasattr(t_gui.Api, a):
            getattr(t_gui.Api, a).clear()
    before = {t.ident for t in threading.enumerate()}
    fn = getattr(api, nm, None)
    if fn is None:
        print(f"{nm:<26}{'--':>9}  (不存在)")
        continue
    t0 = time.perf_counter()
    try:
        fn(TODAY)
    except TypeError:
        try:
            fn()
        except Exception as e:
            print(f"{nm:<26}{'ERR':>9}  {type(e).__name__}: {e}")
            continue
    except Exception as e:
        print(f"{nm:<26}{'ERR':>9}  {type(e).__name__}: {e}")
        continue
    ms = (time.perf_counter() - t0) * 1000
    time.sleep(0.4)
    new = [t.name for t in threading.enumerate() if t.ident not in before]
    print(f"{nm:<26}{ms:9.0f}{len(new):9}  {new[:6]}")

print(f"\n结束线程 {threading.active_count()}")
