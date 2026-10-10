"""交叉验证：market_data.py 里内联的「刚站上5日线」公式 vs core/ma_reclaim.ma5_state。

market_data 的实现是逐行复刻的（不能 import ma5_state，因为在 fetch 热路径里），
所以必须证明两者在真实数据上**逐位一致**，否则就是又一个「同条款两份实现」的漂移源。
用法：PYTHONIOENCODING=utf-8 python tmp/probe_d11_crosscheck.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json, sys
from pathlib import Path
import numpy as np
sys.path.insert(0, BASE)
from core.ma_reclaim import ma5_state

DAILY = Path(BASE) / "t_io" / "cache" / "daily_kline"

def inline_impl(closes, target_idx):
    """逐行复制 market_data.py 里的那段（改字段名以对齐 ma5_state）。"""
    lo = max(0, target_idx - 5)
    win = [float(x) for x in closes[lo:target_idx + 1]]
    if len(win) < 6:
        return None
    px = win[-1]; basis = win[:-1]
    prev_close = basis[-1]
    prev_ma5 = sum(basis[-5:]) / 5.0
    cur_ma5 = (sum(basis[-4:]) + px) / 5.0
    if cur_ma5 <= 0 or px <= 0:
        return None
    return 1 if (prev_close < prev_ma5 and px > cur_ma5) else 0

files = [p for p in sorted(DAILY.glob("*.json")) if not p.name.startswith("index_")]
n = 0; mism = 0; hits_inline = 0; hits_ref = 0; examples = []
rng = np.random.default_rng(7)
for fp in rng.choice(files, size=min(600, len(files)), replace=False):
    try:
        rows = json.loads(Path(fp).read_text(encoding="utf-8")).get("rows") or []
    except Exception:
        continue
    cl = [r.get("close") for r in rows]
    if len(cl) < 20:
        continue
    for t in range(10, len(cl)):
        # 参考实现：closes[:t+1]（ma5_state 取末根为当日）
        st = ma5_state([float(x) for x in cl[:t + 1] if x is not None])
        a = inline_impl(cl, t)
        if st is None or a is None:
            continue
        b = 1 if st["overnight_reclaim"] else 0
        n += 1
        hits_inline += a; hits_ref += b
        if a != b:
            mism += 1
            if len(examples) < 3:
                examples.append((Path(fp).stem, t, a, b))

print(f"比对 {n:,} 个 stock-day")
print(f"  内联实现命中(刚站上): {hits_inline:,}")
print(f"  ma5_state 命中      : {hits_ref:,}")
print(f"  不一致: {mism}")
for e in examples: print("   例:", e)
print("\n判定:", "PASS 逐位一致" if mism == 0 else f"FAIL 有 {mism} 处不一致")
