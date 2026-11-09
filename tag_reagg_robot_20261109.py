# -*- coding: utf-8 -*-
"""2026-11-09 owner：「机器人概念也存在分类太碎的问题，请重新整合」。

## 现状（核实）
机器人 149 条成员散在 **42 个细分**里，其中 **23 个细分只有 1 只票**
（加工设备/专用设备/机器视觉/微型链/灵巧手/滚动体/弹性元件/丝杠零部件/减速器零部件/
传感器/电子皮肤/PCB/线缆/物流系统/伺服系统…）。

## 重新整合（42 → 5，按产业链环节）
  ① **本体与整机**：机器人本体/整机、总成、人形机器人、场景与护理应用
  ② **核心零部件-传动**：减速器、丝杠、轴承、滚动体、关节模组、执行器、腱绳材料、灵巧手
  ③ **电机电控与感知**：电机、微特减速电机、伺服、智能控制、工业自动化、传感器、芯片、
     机器视觉、电子皮肤、线束/线缆/PCB
  ④ **结构件与材料**：结构件、材料、弹性元件、精密铁芯
  ⑤ **设备与集成**：自动化集成、丝杠设备/加工设备/专用设备、加工刀具、物流系统、
     工程机械及液压件、重型装备

写前备份；幂等；每只票打 `jiuyan_robot_reagg_20261109` 审计标记。
运行：`python tag_reagg_robot_20261109.py [--apply]`
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
NOTE_KEY = "jiuyan_robot_reagg_20261109"
CAT = "机器人"

G_BODY = "本体与整机"
G_DRIVE = "核心零部件-传动"
G_ELEC = "电机电控与感知"
G_STRUCT = "结构件与材料"
G_EQUIP = "设备与集成"

MAP = {
    "本体及应用": G_BODY, "总成": G_BODY, "人形机器人": G_BODY,
    "场景应用": G_BODY, "护理机器人": G_BODY,
    "减速器": G_DRIVE, "减速器零部件": G_DRIVE, "丝杠": G_DRIVE, "丝杠零部件": G_DRIVE,
    "轴承": G_DRIVE, "滚动体": G_DRIVE, "关节模组": G_DRIVE, "执行器": G_DRIVE,
    "腱绳材料": G_DRIVE, "灵巧手": G_DRIVE, "微型链/灵巧手": G_DRIVE,
    "电机": G_ELEC, "微特减速电机": G_ELEC, "伺服系统": G_ELEC, "智能控制": G_ELEC,
    "工业自动化": G_ELEC, "传感器": G_ELEC, "芯片": G_ELEC, "机器视觉": G_ELEC,
    "电子皮肤": G_ELEC, "线束": G_ELEC, "线缆": G_ELEC, "PCB": G_ELEC,
    "结构件": G_STRUCT, "材料": G_STRUCT, "弹性元件": G_STRUCT, "精密铁芯": G_STRUCT,
    "自动化集成": G_EQUIP, "丝杠设备": G_EQUIP, "加工设备": G_EQUIP, "专用设备": G_EQUIP,
    "加工刀具": G_EQUIP, "物流系统": G_EQUIP, "工程机械": G_EQUIP,
    "工程机械液压件": G_EQUIP, "重型装备": G_EQUIP, "自动化设备": G_EQUIP,
}
# 多细分票的取舍顺序（本体 > 传动 > 电机 > 结构 > 设备）
PRIORITY = [G_BODY, G_DRIVE, G_ELEC, G_STRUCT, G_EQUIP]


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
        rb = [(c, cn) for c, cn in pairs if c == CAT]
        if not rb:
            continue
        toks = []
        for _, cn in rb:
            toks.extend(_toks(cn))
        mapped = {MAP.get(t) for t in toks if MAP.get(t)}
        if not mapped:
            unknown.append((code, e.get("name"), toks))
            grp = G_EQUIP
        else:
            grp = next(g for g in PRIORITY if g in mapped)
        plan.append((code, e.get("name"), grp, toks, pairs))
    dist = Counter(p[2] for p in plan)
    print(f"机器人 {len(plan)} 只 → 新分组：")
    for k, v in dist.most_common():
        print(f"  {k}: {v}")
    if unknown:
        print("未匹配旧名：", unknown[:5])
    multi = [p for p in plan if len({MAP.get(t) for t in p[3] if MAP.get(t)}) > 1]
    print(f"多细分票 {len(multi)} 只，按 本体>传动>电机>结构>设备 取一个：")
    for p in multi[:8]:
        print(f"  {p[0]} {p[1]}: {' / '.join(p[3])} → {p[2]}")
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
        e[NOTE_KEY] = f"{now} 机器人聚合 → {grp}"
        changed += 1
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_robot_reagg_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_robot_reagg_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
