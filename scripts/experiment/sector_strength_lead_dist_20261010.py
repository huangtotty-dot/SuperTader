"""量「龙头集中度」lead = Top3成员收益均值 − 其余成员收益均值 的分布，用于给 heat_score 定标。"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json, sys
from pathlib import Path
import numpy as np
BASE = Path(BASE); DAILY = BASE / "t_io" / "cache" / "daily_kline"
wl = json.loads((BASE/"stock_hunter"/"watchlist_jiuyan.json").read_text(encoding="utf-8"))
cat = {}
for c, v in wl.items():
    if isinstance(v, dict) and (v.get("jiuyan_category") or "").strip() and "|" not in v["jiuyan_category"]:
        cat.setdefault(v["jiuyan_category"].strip(), []).append(c)
def _f(x):
    try: return float(x)
    except Exception: return np.nan
leads = []
for k, codes in cat.items():
    P = []
    for c in codes:
        fp = DAILY / f"{c}.json"
        if fp.exists():
            rows = json.loads(fp.read_text(encoding="utf-8")).get("rows") or []
            P.append(np.array([_f(r.get("close")) for r in rows]))
    if len(P) < 5: continue
    m = min(len(x) for x in P)
    M = np.vstack([x[-m:] for x in P])
    with np.errstate(invalid="ignore", divide="ignore"):
        R = M[:, 1:] / M[:, :-1] - 1.0
    for i in range(R.shape[1]):
        r = R[:, i]; r = r[np.isfinite(r)]
        if len(r) < 5: continue
        s = np.sort(r)[::-1]
        leads.append(float(s[:3].mean() - s[3:].mean()) if len(s) > 4 else 0.0)
a = np.array(leads)
print(f"样本 {len(a):,}")
for q in (1, 10, 25, 50, 75, 90, 95, 99):
    print(f"  P{q:<3}= {np.percentile(a,q)*100:+6.3f}%")
print(f"  max = {a.max()*100:+.2f}%")
pos = a[a > 0]
print(f"  >0 占比 {len(pos)/len(a)*100:.1f}%")
print(f"\n若映射 score = clamp(lead*K, 0, 10)：")
for K in (40, 50, 60, 80):
    sc = np.clip(a*K, 0, 10)
    print(f"  K={K:<3} 均值={sc.mean():5.2f}  满分占比={np.mean(sc>=10)*100:4.1f}%  零分占比={np.mean(sc<=0)*100:4.1f}%")
