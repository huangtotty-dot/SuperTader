# -*- coding: utf-8 -*-
"""2026-11-09 owner：「光伏概念也有点乱：①不用重点强调太空光伏 ②分类太零散，重新分类」。

## 现状（核实）
光伏 76 只散在 12 个细分里，且名字有一半带「太空光伏」前缀：
  太空光伏设备21 / 太空光伏辅材18 / 太空光伏组件14 / 太阳翼结构8 / 砷化镓电池4 /
  光储充3 / 光伏玻璃2 / 光伏支架2 / 光伏建筑一体化1 / 锗电池1 / 光伏组件1 / 光伏电站1

## 重新分类（12 → 5，去掉「太空」前缀）
  ① **硅料硅片**：多晶硅、硅片（通威/TCL中环/弘元绿能）
  ② **电池与组件**：电池片、组件、砷化镓/锗电池
  ③ **辅材与结构**：玻璃、胶膜、银浆、焊带/金刚线、边框、支架、太阳翼结构
  ④ **设备**：硅片/电池/组件产线设备（含还原炉、切片设备）
  ⑤ **电站与应用**：电站运营、EPC、光储充、BIPV

写前备份；幂等；每只票打 `jiuyan_pv_reagg_20261109` 审计标记。
运行：`python tag_reagg_pv_20261109.py [--apply]`
"""
from __future__ import annotations

import ast
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
NOTE_KEY = "jiuyan_pv_reagg_20261109"
CAT = "光伏"

G_WAFER = "硅料硅片"
G_CELL = "电池与组件"
G_MAT = "辅材与结构"
G_EQUIP = "设备"
G_APP = "电站与应用"

# 旧细分 → 新分组
MAP = {
    "太空光伏设备": G_EQUIP, "光伏设备": G_EQUIP, "光伏设备-其他": G_EQUIP,
    "太空光伏辅材": G_MAT, "光伏辅材": G_MAT,
    "太空光伏组件": G_CELL, "光伏组件": G_CELL,
    "砷化镓电池": G_CELL, "锗电池": G_CELL,
    "太阳翼结构": G_MAT,
    "光伏玻璃": G_MAT, "光伏支架": G_MAT,
    "光储充": G_APP, "光伏电站": G_APP, "光伏建筑一体化": G_APP,
}
# 按主营覆盖（旧标签与主营不符的）
OVERRIDE = {
    "002129": G_WAFER,   # TCL中环：硅片
    "600438": G_WAFER,   # 通威股份：硅料+电池，硅料为主
    "603185": G_WAFER,   # 弘元绿能：硅片
    "600481": G_EQUIP,   # 双良节能：多晶硅还原炉（设备）
    "688556": G_EQUIP,   # 高测股份：切片设备
    "300861": G_MAT,     # 美畅股份：金刚线（耗材）
    "002135": G_APP,     # 东南网架：BIPV
    "603098": G_APP,     # 森特股份：BIPV
}


def _toks(cn: str) -> list:
    s = str(cn or "").strip()
    if s.startswith("["):
        try:
            return [str(x).strip() for x in ast.literal_eval(s) if str(x).strip()]
        except Exception:
            return [s]
    return [x.strip() for x in s.split("|") if x.strip()]


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
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan, unknown = [], []
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        pv = [(c, cn) for c, cn in pairs if c == CAT]
        if not pv:
            continue
        toks = []
        for _, cn in pv:
            toks.extend(_toks(cn))
        grp = OVERRIDE.get(code)
        if not grp:
            mapped = {MAP.get(t) for t in toks if MAP.get(t)}
            if len(mapped) == 1:
                grp = mapped.pop()
            elif len(mapped) > 1:
                # 多映射：按 设备 > 电池 > 辅材 > 硅片 > 应用 取第一个
                for cand in (G_EQUIP, G_CELL, G_MAT, G_WAFER, G_APP):
                    if cand in mapped:
                        grp = cand
                        break
            else:
                unknown.append((code, e.get("name"), toks))
                grp = G_MAT
        plan.append((code, e.get("name"), grp, toks, pairs))
    dist = Counter(p[2] for p in plan)
    print(f"光伏 {len(plan)} 只 → 新分组：")
    for k, v in dist.most_common():
        print(f"  {k}: {v}")
    if unknown:
        print("未匹配旧名（归辅材与结构）：", unknown[:5])
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, grp, toks, pairs in plan:
        e = wl[code]
        out = [(c, cn) for c, cn in pairs if c != CAT] + [(CAT, grp)]
        put_pairs(e, out, now)
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        for t in toks:
            rs.pop(t, None)
        if toks:
            rs[grp] = f"{name}：{' / '.join(dict.fromkeys(toks))[:50]}（{grp}）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 光伏聚合 → {grp}"
        changed += 1
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_pv_reagg_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_pv_reagg_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
