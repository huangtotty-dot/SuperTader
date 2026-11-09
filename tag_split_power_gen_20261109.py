# -*- coding: utf-8 -*-
"""2026-11-09 owner：「更改」（发电设备按主营再拆 + 两只归位）。

## 拆分（发电设备 22 只）
`发电设备` 里混了「整机/辅机」和「燃机/发电用材料零部件」两拨：
  · **发电设备（整机与辅机）** 6 只：东方电气、哈空调（电站空冷）、龙源技术（火电改造）、
    华电科工、青达环保（炉渣处理）、潍柴动力（发电机组/SOFC）
  · **燃机与发电材料零部件** 14 只：万泽/应流/隆达（高温合金）、航宇/航亚（叶片/锻件）、
    三角防务（航空锻件）、飞沃（紧固件）、常宝/盛德鑫泰（锅炉管/电站无缝管）、
    联德/福鞍（精密件/铸件）、豪迈科技（大型机加工）、精工科技（碳纤维装备）、
    杰瑞股份（油气/燃机机组）

## 归位（主营不是发电设备）
  · 603067 振华股份（铬盐化工，SOFC 连接体涂层只是材料配套）→ **化工/基础化工**
  · 002733 雄韬股份（铅酸/锂电/氢燃料电池）→ **电池/锂电池**

写前备份；幂等；每只票打 `jiuyan_power_gen_split_20261109` 审计标记。
运行：`python tag_split_power_gen_20261109.py [--apply]`
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
NOTE_KEY = "jiuyan_power_gen_split_20261109"
OLD_CON = "发电设备"

GEN_EQUIP = "发电设备（整机与辅机）"
GEN_PARTS = "燃机与发电材料零部件"

SPLIT = {
    # 整机/辅机
    "600875": GEN_EQUIP,   # 东方电气：能源装备制造
    "600202": GEN_EQUIP,   # 哈空调：电站空冷器
    "300105": GEN_EQUIP,   # 龙源技术：火电灵活性改造
    "601226": GEN_EQUIP,   # 华电科工：物料输送/火电改造
    "688501": GEN_EQUIP,   # 青达环保：炉渣处理
    "000338": GEN_EQUIP,   # 潍柴动力：发电机组/燃料电池
    # 燃机/发电材料零部件
    "000534": GEN_PARTS,   # 万泽股份：高温合金/单晶叶片
    "603308": GEN_PARTS,   # 应流股份：高温合金/核电铸件
    "688231": GEN_PARTS,   # 隆达股份：高温合金
    "688239": GEN_PARTS,   # 航宇科技：锻件
    "688510": GEN_PARTS,   # 航亚科技：航发/燃机叶片
    "300775": GEN_PARTS,   # 三角防务：航空锻件
    "301232": GEN_PARTS,   # 飞沃科技：紧固件
    "002478": GEN_PARTS,   # 常宝股份：锅炉管
    "300881": GEN_PARTS,   # 盛德鑫泰：电站无缝钢管
    "605060": GEN_PARTS,   # 联德股份：精密零部件
    "603315": GEN_PARTS,   # 福鞍股份：铸件
    "002595": GEN_PARTS,   # 豪迈科技：大型机加工
    "002006": GEN_PARTS,   # 精工科技：碳纤维装备
    "002353": GEN_PARTS,   # 杰瑞股份：油气/燃机机组
}
MOVE = {
    "603067": ("化工", "基础化工"),   # 振华股份：铬盐
    "002733": ("电池", "锂电池"),     # 雄韬股份：铅酸/锂电/氢燃料
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
    plan, skipped = [], []
    for code, new in SPLIT.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if ("电力", OLD_CON) not in pairs:
            skipped.append((code, e.get("name"), f"没有 电力/{OLD_CON}（现 {'|'.join(f'{c}/{p}' for c, p in pairs)}）"))
            continue
        plan.append((code, e.get("name"), "split", new, pairs))
    for code, target in MOVE.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if ("电力", OLD_CON) not in pairs:
            skipped.append((code, e.get("name"), f"没有 电力/{OLD_CON}"))
            continue
        plan.append((code, e.get("name"), "move", target, pairs))
    print(f"待处理 {len(plan)} 只（拆 {sum(1 for p in plan if p[2]=='split')} / 归位 {sum(1 for p in plan if p[2]=='move')}）；跳过 {len(skipped)}")
    for p in plan:
        print(f"  {p[0]} {p[1]}: 电力/{OLD_CON} → {p[3] if isinstance(p[3], str) else '/'.join(p[3])}")
    for s in skipped:
        print(f"  = {s[0]} {s[1]}: {s[2]}")
    # 校验：拆分后还有谁在 发电设备
    rest = [c for c, e in wl.items() if isinstance(e, dict)
            and any(cc == "电力" and cn == OLD_CON for cc, cn in get_pairs(e))
            and c not in SPLIT and c not in MOVE]
    print("拆分后仍在『发电设备』:", rest or "无")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = 0
    for code, name, kind, val, pairs in plan:
        e = wl[code]
        out = []
        for c, p in pairs:
            if (c, p) == ("电力", OLD_CON):
                out.append(("电力", val) if kind == "split" else val)
            else:
                out.append((c, p))
        put_pairs(e, out, now)
        rs = e.get("jiuyan_concept_reason")
        if isinstance(rs, dict) and OLD_CON in rs:
            rs[val if kind == "split" else val[1]] = rs.pop(OLD_CON)
            e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 发电设备拆分/归位 → {val if isinstance(val, str) else '/'.join(val)}"
        changed += 1
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_power_gen_split_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"已改 {changed} 只；备份 backup/watchlist_jiuyan_before_power_gen_split_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
