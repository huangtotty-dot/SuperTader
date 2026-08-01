# -*- coding: utf-8 -*-
"""
check_part_ranges.py — 校验 v107_ab_expanded/parts 每个分块信号日期是否落在其周段内
发现"大肚子块"(月段/半月段残留写入周段同名目录) -> 打印需删除重跑的目录清单
"""
import json, re, sys
from pathlib import Path

BASE = Path(r"E:\06_T")
PARTS = BASE / "t_io/validation/v107_ab_expanded/parts"

sys.path.insert(0, str(BASE / "t_io/validation"))
from run_ab_expanded import SEGS  # noqa: E402

SEG_MAP = {s.replace("-", ""): (s, e) for s, e in SEGS}

bad = []
total = 0
for d in sorted(PARTS.iterdir()):
    if not d.is_dir():
        continue
    m = re.match(r"(baseline|v102)_(\d+)_(20\d{6})$", d.name)
    if not m:
        continue
    mode, code, start_key = m.groups()
    sig = d / f"signals_{mode}.jsonl"
    if not sig.exists():
        continue
    total += 1
    dates = set()
    for line in open(sig, encoding="utf-8"):
        line = line.strip()
        if line:
            dates.add(json.loads(line)["ts"][:10])
    if start_key not in SEG_MAP:
        bad.append((d.name, "UNKNOWN_SEG", sorted(dates)))
        continue
    s, e = SEG_MAP[start_key]
    out = sorted(x for x in dates if not (s <= x <= e))
    if out:
        bad.append((d.name, f"{s}~{e}", f"{min(dates)}~{max(dates)} 越界{len(out)}天: {out}"))

print(f"共扫描 {total} 个含信号分块")
if not bad:
    print("OK: 所有分块日期均落在对应周段内")
else:
    print(f"发现 {len(bad)} 个越界分块:")
    for name, seg, info in bad:
        print(f"  {name}  段={seg}  实际={info}")
