# -*- coding: utf-8 -*-
"""2026-11-08 owner：按核实结果调整电力板块里 8 只错位的票。

依据（逐只核对公司简介）：
  603105 芯能科技   自持运营分布式光伏电站、卖电收入     智能电网 → **电力运营**
  003035 南网能源   综合能源服务/节能服务                智能电网 → **电力运营**
  301386 未来电器   低压断路器附件（硬件）              智能电网 → **开关与成套设备**
  605066 天正电气   低压电器元件及成套                  智能电网 → **开关与成套设备**
  002169 智光电气   储能产品 + 独立储能电站投资运营      智能电网 → **储能**
  688248 南网科技   大型储能电站整套解决方案            智能电网 → **储能**
  600770 综艺股份   芯片设计/智能卡/手游/功率半导体      电力/电力运营 → **半导体/芯片设计-功率半导体**（移出电力）
  300317 珈伟新能   太阳能草坪灯/LED 照明（北美）        电力/智能电网 → **消费电子/LED**（移出电力）

另：**000533 顺钠股份** = 顺特电气「干式变压器」→ 现归 `电力/变压器/SST` **正确**，不动。

写前备份；幂等；每只票打 `jiuyan_power_fix_20261108` 审计标记。
运行：`python tag_fix_power_misfits_20261108.py [--apply]`
"""
from __future__ import annotations

import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"
NOTE_KEY = "jiuyan_power_fix_20261108"

# code -> (旧大类, 旧概念, 新大类, 新概念)
FIX = {
    "603105": ("电力", "智能电网", "电力", "电力运营"),
    "003035": ("电力", "智能电网", "电力", "电力运营"),
    "301386": ("电力", "智能电网", "电力", "开关与成套设备"),
    "605066": ("电力", "智能电网", "电力", "开关与成套设备"),
    "002169": ("电力", "智能电网", "电力", "储能"),
    "688248": ("电力", "智能电网", "电力", "储能"),
    "600770": ("电力", "电力运营", "半导体", "芯片设计-功率半导体"),
    "300317": ("电力", "智能电网", "消费电子", "LED"),
}


def norm(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return "|".join(str(v).strip() for v in x if str(v).strip())
    return str(x).strip()


def get_pairs(e: dict) -> list:
    out = []
    for k in range(1, 10):
        c = norm(e.get(f"jiuyan_category{k}"))
        if c:
            out.append((c, norm(e.get(f"jiuyan_concept{k}"))))
    if out:
        return out
    cats = [c.strip() for c in norm(e.get("jiuyan_category")).split("|") if c.strip()]
    cons = [c.strip() for c in norm(e.get("jiuyan_concept")).split("|") if c.strip()]
    return [(c, cons[i] if i < len(cons) else "") for i, c in enumerate(cats)]


def put_pairs(e: dict, pairs: list, now: str) -> None:
    for k in range(1, 10):
        e.pop(f"jiuyan_category{k}", None)
        e.pop(f"jiuyan_concept{k}", None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f"jiuyan_category{k}"] = c
        if p:
            e[f"jiuyan_concept{k}"] = p
    e["jiuyan_category"] = "|".join(c for c, _ in pairs)
    e["jiuyan_concept"] = "|".join(p for _, p in pairs if p)
    e["updated_at"] = now


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    plan, skip = [], []
    for code, (fc, fcon, tc, tcon) in FIX.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skip.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if (fc, fcon) not in pairs:
            skip.append((code, e.get("name"), f"没有 {fc}/{fcon}（现 {'|'.join(f'{c}/{p}' for c, p in pairs) or '空'}）"))
            continue
        plan.append((code, e.get("name"), fc, fcon, tc, tcon, pairs))
    print(f"待调整 {len(plan)} 只；跳过 {len(skip)} 只")
    for p in plan:
        print(f"  {p[0]} {p[1]}: {p[2]}/{p[3]} → {p[4]}/{p[5]}")
    for s in skip:
        print(f"  = {s[0]} {s[1]}: {s[2]}")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = []
    for code, name, fc, fcon, tc, tcon, pairs in plan:
        e = wl[code]
        out = []
        for c, p in pairs:
            if c == fc and p == fcon:
                out.append((tc, tcon))
            else:
                out.append((c, p))
        put_pairs(e, out, now)
        e[NOTE_KEY] = f"{now} 电力错位纠正：{fc}/{fcon} → {tc}/{tcon}（按公司简介核实）"
        changed.append(code)
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_power_fix_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"\n已改 {len(changed)} 只；备份 backup/watchlist_jiuyan_before_power_fix_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
