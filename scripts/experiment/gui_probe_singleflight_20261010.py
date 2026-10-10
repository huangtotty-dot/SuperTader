"""验证单飞：16 路并发调三个公共端点，各自的慢路径（_impl）至多跑 1 次。

改前（_bounded 弃工）每个调用者都会起一个自己的池跑一遍完整慢路径。
用法：python tmp/probe_singleflight.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import threading
import time

sys.path.insert(0, BASE)

import t_gui  # noqa: E402

counts = {"turnover": 0, "hist": 0, "indices": 0}
lk = threading.Lock()

_o_tov = t_gui.Api._market_turnover_impl
_o_hist = t_gui.Api._turnover_history_cached
_o_idx = t_gui.Api._load_indices_impl


def wrap(key, orig):
    def _w(*a, **kw):
        with lk:
            counts[key] += 1
        return orig(*a, **kw)
    return _w


t_gui.Api._market_turnover_impl = wrap("turnover", _o_tov)
t_gui.Api._turnover_history_cached = wrap("hist", _o_hist)
t_gui.Api._load_indices_impl = wrap("indices", _o_idx)

# 冷缓存（内存），但保留磁盘快照 → 走「秒返快照 + 踢单飞刷新」分支
t_gui.Api._turnover_cache.clear()
t_gui.Api._turnover_hist_cache.clear()
t_gui.Api._indices_cache.clear()

api = t_gui.Api()


def worker(i):
    for fn in (api.load_indices, api.load_market_turnover, api.load_turnover_history):
        try:
            fn()
        except Exception:
            pass


t0 = time.perf_counter()
ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(16)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=30)
total = time.perf_counter() - t0

print(f"\n=== 16 路并发 × 3 端点，总耗时 {total:.2f}s，活线程 {threading.active_count()} ===")
print(f"慢路径实际执行次数: _market_turnover_impl={counts['turnover']}  "
      f"_turnover_history_cached={counts['hist']}  _load_indices_impl={counts['indices']}")
print("  （期望各 =1：单飞把 16 个并发调用者合并成 1 次刷新）")
# 给在飞刷新一点时间收尾
time.sleep(12)
print(f"收尾后: {counts}")
ok = counts["turnover"] <= 1 and counts["hist"] <= 1 and counts["indices"] <= 1
print("\n判定:", "PASS" if ok else "FAIL")
