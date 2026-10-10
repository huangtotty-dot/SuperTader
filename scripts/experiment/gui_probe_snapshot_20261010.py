"""验证磁盘快照层：秒显 + stale 标注 + 跨日拒绝 + 占位符绝不落盘。

用法：python tmp/probe_snapshot.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, BASE)

import t_gui  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
SNAP = t_gui._OVERVIEW_SNAPSHOT_DIR
ok = True


def check(cond, msg):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    if not cond:
        ok = False


print("=== 0) 清干净：删快照 + 清内存缓存 ===")
if SNAP.exists():
    shutil.rmtree(SNAP, ignore_errors=True)
t_gui.Api._turnover_cache.clear()
t_gui.Api._turnover_hist_cache.clear()
t_gui.Api._indices_cache.clear()
t_gui.Api._em_cache.clear()
print("  快照目录存在:", SNAP.exists())

api = t_gui.Api()

print("\n=== 1) 首次冷调（无快照）：三个端点都打一遍，触发各自单飞刷新 ===")
for _nm, _call in (("load_indices", api.load_indices),
                   ("load_market_turnover", api.load_market_turnover),
                   ("load_turnover_history", api.load_turnover_history)):
    t0 = time.perf_counter()
    _o = _call()
    _d = (time.perf_counter() - t0) * 1000
    print(f"  {_nm:<22}{_d:7.0f}ms  keys={sorted(_o.keys())}")
    check(not _o.get("stale"), f"{_nm} 首次调用不应标 stale（内存里本来就没有）")

print("\n=== 2) 等后台单飞刷新落盘 ===")
for i in range(40):
    time.sleep(0.5)
    if (SNAP / "indices.json").exists() and (SNAP / "market_turnover.json").exists():
        print(f"  快照在 {(i + 1) * 0.5:.1f}s 后落盘")
        break
has_idx = (SNAP / "indices.json").exists()
has_tov = (SNAP / "market_turnover.json").exists()
print(f"  indices.json={has_idx}  market_turnover.json={has_tov}")
print(f"  目录内容: {sorted(p.name for p in SNAP.glob('*.json')) if SNAP.exists() else []}")

if has_idx:
    rec = json.loads((SNAP / "indices.json").read_text(encoding="utf-8"))
    pl = rec["payload"]
    print(f"  快照 payload keys={sorted(pl.keys())}")
    check(bool(pl.get("indices")), "落盘的 indices 非空（占位符不许落盘）")
    check("stale" not in pl, "落盘的 payload 自身不带 stale（stale 是读时包装的）")

print("\n=== 3) 清内存缓存后冷调：应秒返且标 stale ===")
t_gui.Api._indices_cache.clear()
t_gui.Api._turnover_cache.clear()
t_gui.Api._turnover_hist_cache.clear()
if has_idx:
    t0 = time.perf_counter()
    ind2 = api.load_indices()
    d2 = (time.perf_counter() - t0) * 1000
    print(f"  load_indices {d2:.0f}ms  stale={ind2.get('stale')} "
          f"snapshot_day={ind2.get('snapshot_day')} snapshot_ts={ind2.get('snapshot_ts')} "
          f"age={ind2.get('stale_age_sec')}")
    check(d2 < 150, f"冷调应 <150ms（实测 {d2:.0f}ms）")
    check(ind2.get("stale") is True, "必须标 stale=True（安全契约第 1 条）")
    check(ind2.get("snapshot_day") == TODAY, "snapshot_day 应为今天")
    check(bool(ind2.get("snapshot_ts")), "必须带 snapshot_ts 供前端显示")
    check(bool([i for i in (ind2.get("indices") or []) if i.get("price")]),
          "秒显的快照带真实数字（不是空白卡片）")

print("\n=== 4) 把 indices 快照改成昨日：实时价类必须拒绝（跨日） ===")
if has_idx:
    fp = SNAP / "indices.json"
    rec = json.loads(fp.read_text(encoding="utf-8"))
    rec["snapshot_day"] = "2020-01-01"
    fp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    t_gui.Api._indices_cache.clear()
    t0 = time.perf_counter()
    ind3 = api.load_indices()
    d3 = (time.perf_counter() - t0) * 1000
    print(f"  load_indices {d3:.0f}ms  stale={ind3.get('stale')} error={ind3.get('error')}")
    check(not ind3.get("stale"), "跨日快照必须被拒（不得当实时价显示）")

print("\n=== 5) turnover_history 允许跨日：应返回 stale ===")
fp = SNAP / "turnover_history.json"
if fp.exists():
    rec = json.loads(fp.read_text(encoding="utf-8"))
    rec["snapshot_day"] = "2020-01-01"
    fp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    t_gui.Api._turnover_hist_cache.clear()
    h = api.load_turnover_history()
    print(f"  stale={h.get('stale')} days={len(h.get('days') or [])} "
          f"in_progress={h.get('in_progress')}")
    check(h.get("stale") is True, "60 日柱状图允许跨日，但必须标 stale")
    check(h.get("in_progress") in (True, False), "in_progress 原样重放（未按当前时钟重算）")
else:
    print("  turnover_history.json 未落盘，跳过（可能本次北交所/日线取数失败）")

print("\n=== 6) 占位符绝不落盘 ===")
for name in ("indices", "market_turnover", "turnover_history"):
    fp = SNAP / f"{name}.json"
    if not fp.exists():
        print(f"  {name}.json 不存在")
        continue
    pl = json.loads(fp.read_text(encoding="utf-8"))["payload"]
    if name == "indices":
        check(bool(pl.get("indices")), f"{name} 落盘内容非空")
    else:
        check(pl.get("available") is True, f"{name} 落盘 available=True（非占位）")

print("\n判定:", "PASS" if ok else "FAIL")
