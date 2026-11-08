# -*- coding: utf-8 -*-
"""2026-11-08 owner：「电力设备这个概念有点大，可以切分几个细的板块？」

## 为什么「变压器/SST」和「电力设备」分开是对的
· 变压器/SST = **一条产品线**：电力变压器（特变/中国西电/金盘/保变…）+ 固态变压器 SST
  （科华/特锐德/易事特/云路…）+ 变压器配套（华明装备分接开关、可立克/京泉华/伊戈尔磁元件）；
· 电力设备 = **其余一次设备的集合**，本身不是一条产品线 —— 线缆、输电铁塔、绝缘子、
  开关柜、继电器、电容、电源全塞在一起 ⇒ 这就是"大"的原因。

## 切分方案（电力设备 31 只 → 5 个产品线 + 6 只归位）
| 新细分 | 只数 | 成员 |
|---|---|---|
| 电缆 | 8 | 华菱线缆、太阳电缆、远程股份、长缆科技、杭电股份、中利集团、福华尚纬、新能泰山 |
| 输电铁塔 | 2 | 东方铁塔、风范股份 |
| 绝缘子 | 2 | 大连电瓷、神马电力 |
| 开关与成套设备 | 8 | 平高电气、新宏泰、明阳电气、长城电工、中电鑫龙、昇辉科技、正泰电器、九洲集团 |
| 电力电子与配套 | 5 | 宏发股份(继电器)、江海股份(电容)、盛弘股份(电源)、中国电研(检测)、冰轮环境(温控) |

归位（按公司简介核实）：
  301012 扬电科技（节能电力变压器）→ 变压器/SST
  001239 永达股份（风电金属结构件/齿轮锻件）→ 风电设备
  001388 信通电子（电力 AIoT/智能巡检）→ 智能电网
  688616 西力科技（智能电表/用电信息）→ 智能电网
  600202 哈空调（电站空冷器）→ 发电设备
  002245 蔚蓝锂芯（圆柱锂电池，本就不是电力设备）→ **电池/锂电池**（跨大类修正）

写前备份；幂等；每只票打 `jiuyan_power_split_20261108` 审计标记。
运行：`python tag_split_power_equipment_20261108.py [--apply]`
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
CAT = "电力"
NOTE_KEY = "jiuyan_power_split_20261108"

SPLIT = {  # 电力设备 → 新细分（仍属电力）
    "001208": "电缆", "002300": "电缆", "002692": "电缆", "002879": "电缆",
    "603618": "电缆", "002309": "电缆", "603333": "电缆", "000720": "电缆",
    "002545": "输电铁塔", "601700": "输电铁塔",
    "002606": "绝缘子", "603530": "绝缘子",
    "600312": "开关与成套设备", "603016": "开关与成套设备", "301291": "开关与成套设备",
    "600192": "开关与成套设备", "002298": "开关与成套设备", "300423": "开关与成套设备",
    "601877": "开关与成套设备", "300040": "开关与成套设备",
    "600885": "电力电子与配套", "002484": "电力电子与配套", "300693": "电力电子与配套",
    "688128": "电力电子与配套", "000811": "电力电子与配套",
}
MOVE = {  # 归位到已有细分（new_cat=None 表示大类不变，仍 电力）
    "301012": ("电力", "变压器/SST"),
    "001239": ("电力", "风电设备"),
    "001388": ("电力", "智能电网"),
    "688616": ("电力", "智能电网"),
    "600202": ("电力", "发电设备"),
    "002245": ("电池", "锂电池"),
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


def plan_for(code, e):
    pairs = get_pairs(e)
    pcon = [con for c, con in pairs if c == CAT]
    if not pcon:
        return None
    if code in SPLIT and "电力设备" in pcon:
        return ("split", SPLIT[code])
    if code in MOVE and any(con in pcon for con in ("电力设备",)):
        return ("move", MOVE[code])
    return None


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    plan = []
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        p = plan_for(code, e)
        if p:
            plan.append((code, e.get("name"), p))
    print(f"待处理 {len(plan)} 只：拆分 {sum(1 for _,_,p in plan if p[0]=='split')} / 归位 {sum(1 for _,_,p in plan if p[0]=='move')}")
    for code, name, (kind, val) in plan:
        print(f"  {code} {name}: {kind} → {val}")
    # 校验：拆分+归位后，电力设备应清零
    rest = []
    for code, e in wl.items():
        if isinstance(e, dict) and any(c == CAT and con == "电力设备" for c, con in get_pairs(e)) and code not in SPLIT and code not in MOVE:
            rest.append((code, e.get("name")))
    print("拆分后仍留在『电力设备』的:", rest or "无")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = []
    for code, name, (kind, val) in plan:
        e = wl[code]
        pairs = get_pairs(e)
        out, done = [], False
        if kind == "split":
            for c, con in pairs:
                if c == CAT and con == "电力设备":
                    out.append((CAT, val))
                else:
                    out.append((c, con))
        else:  # move：(new_cat, new_con) 替换该票在电力下的那一对
            new_cat, new_con = val
            for c, con in pairs:
                if c == CAT and con == "电力设备":
                    out.append((new_cat, new_con))
                else:
                    out.append((c, con))
        put_pairs(e, out, now)
        e[NOTE_KEY] = f"{now} 电力设备拆分/归位 → {val if isinstance(val,str) else '/'.join(val)}"
        changed.append((code, name, val))
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_power_split_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"\n已改 {len(changed)} 只；备份 backup/watchlist_jiuyan_before_power_split_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
