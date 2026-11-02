# -*- coding: utf-8 -*-
"""2026-11-02 owner：「光伏板块合并到太空光伏中，同时太空光伏改名光伏」。

净效果 = **一个一级分类「光伏」**：原先 `jiuyan_category = 太空光伏` 的 64 只全部改归
`光伏`，与今天复盘新增的「光伏」段（亚玛顿/清源股份/闽发铝业）同一板块。

实现上**只改分类字段本身**（`jiuyan_category`、`jiuyan_category1..9`，含字符串与 list 两种
形态），不重排槽位、不重建旧版概念串 —— 上一版用 `put_pairs` 顺带把 29 只的旧串补全、
还覆盖了 2 只的旧概念值（云南锗业/罗博特科），属于不该有的副作用。

⚠️ 这是 owner 点名的**改派**（同 2026-10-19「移入某概念」的例外），不受「只增不改」约束；
   细分概念（`太空光伏设备/辅材/组件`、`太阳翼结构`、`砷化镓电池`、`锗电池`）原样保留。

配套：`stock_hunter/modules/sector_phase.py` 的 `CATEGORY_SECTIONS["光伏"]` 同时收
`["光伏", "太空光伏"]`，历史/未来复盘若出现「太空光伏」分段也归到同一板块。

写前备份到 `backup/watchlist_jiuyan_before_pv_merge_20261102.json`；脚本幂等（再跑命中 0 只）。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"

OLD, NEW = "太空光伏", "光伏"


def _swap(v):
    """把分类字段里的 OLD 整段换成 NEW（字符串 `A|B` 与 list 两种形态都认）。

    返回 (新值, 是否改动)。只做**整段精确匹配**：`太空光伏设备` 这种概念值不会被误伤。
    """
    if isinstance(v, list):
        out = [NEW if str(x).strip() == OLD else x for x in v]
        return out, out != v
    if isinstance(v, str) and v:
        parts = v.split("|")
        if any(p.strip() == OLD for p in parts):
            return "|".join(NEW if p.strip() == OLD else p for p in parts), True
    return v, False


def main() -> int:
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = []

    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        hit = False
        for k in range(1, 10):
            f = f"jiuyan_category{k}"
            nv, ch = _swap(e.get(f))
            if ch:
                e[f] = nv
                hit = True
        nv, ch = _swap(e.get("jiuyan_category"))
        if ch:
            e["jiuyan_category"] = nv
            hit = True
        if hit:
            e["updated_at"] = now
            changed.append((code, e.get("name")))

    if changed:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / "watchlist_jiuyan_before_pv_merge_20261102.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"改派 {len(changed)} 只：{OLD} → {NEW}")
    for code, name in changed[:8]:
        print(f"  * {code} {name}")
    if len(changed) > 8:
        print(f"  … 其余 {len(changed) - 8} 只同样处理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
