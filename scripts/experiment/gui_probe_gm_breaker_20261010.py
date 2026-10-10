"""验证 GM 半开单探针闸：16 路并发下只允许 1 个真打 GM，其余快速走腾讯。

做法：把 GM SDK 的取数函数替换成「计数 + 挂住」，然后 16 线程并发调 prov.index_daily。
断言：真达 GM 的次数 == 1，其余 15 个在远小于 4s 的时间内返回（走腾讯兜底）。
用法：python tmp/probe_gm_breaker.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, BASE)

from core.market_data.facade import get_provider  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
prov = get_provider()
print("gm ready:", prov._gm_ready(), " _gm:", type(prov._gm).__name__)

if not prov._gm_ready():
    print("本机 gm 不可用 → 无法验证半开闸（探针闸只在 gm ready 时生效）。改用直接调 _gm_ok 验证。")

hits = {"n": 0}
lock = threading.Lock()


def _hanging_index_daily(*a, **kw):
    with lock:
        hits["n"] += 1
        n = hits["n"]
    if n == 1:
        time.sleep(30)          # 首个探针挂死，模拟 GM 无响应
    return None


# 直接替换 GM provider 的取数入口，保证「真打 GM」一定经过计数点
if prov._gm is not None:
    for _name in ("index_daily", "history_n", "current"):
        if hasattr(prov._gm, _name):
            setattr(prov._gm, _name, _hanging_index_daily)

# 复位熔断，确保从干净状态起跑
prov._gm_down_until = None
prov._gm_probe_until = None

res = {}


def worker(i):
    t0 = time.perf_counter()
    try:
        df = prov.index_daily("sh000001", days=2, end_date=TODAY)
        src = df.attrs.get("source") if df is not None else None
        rows = 0 if df is None else len(df)
    except Exception as e:
        src, rows = f"ERR {type(e).__name__}", 0
    res[i] = (time.perf_counter() - t0, src, rows)


t0 = time.perf_counter()
ths = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(16)]
for t in ths:
    t.start()
for t in ths:
    t.join(timeout=12)
total = time.perf_counter() - t0

print(f"\n=== 16 路并发 index_daily 总耗时 {total:.2f}s ===")
print(f"真达 GM 次数 = {hits['n']}  (期望 1)")
print(f"活线程数 = {threading.active_count()}")
# 判定口径：GM 只被打 1 次；其余 15 个必须**没有被串行排在 GM 单 worker 后面**
# ——即它们的耗时远小于 4s 的 GM 超时（实测 ~1.3-1.9s 是腾讯自身取数）。
NOT_SERIALIZED = 2.5
fast = [i for i, (d, s, r) in res.items() if d < NOT_SERIALIZED]
slow = [(i, round(d, 2), s) for i, (d, s, r) in res.items() if d >= NOT_SERIALIZED]
print(f"<{NOT_SERIALIZED}s 返回(未排在 GM 后): {len(fast)}/16")
print(f">={NOT_SERIALIZED}s 返回: {slow}")
print("\n判定:", "PASS" if hits["n"] == 1 and len(fast) >= 15 else "FAIL")
