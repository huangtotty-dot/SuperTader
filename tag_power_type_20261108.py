# -*- coding: utf-8 -*-
"""2026-11-08 owner：「电力板块中的电力运营，请给每支股一个标签：火电？核电？水电？储能相关？」

按公司简介/主营逐只判定**发电方式**，写入两个字段：
  · `jiuyan_power_type` = 水电 / 火电 / 核电 / 风光 / 光伏 / 热电联产 / 供热 / 电网供电 / 综合能源 / 待定
  · `jiuyan_concept_reason['电力运营']` 里带上该类型：`名称：主营（发电运营·水电）`

判定口径：
  · 水电/核电：以该电源为主的发电商（长江电力、中国核电…）
  · 火电：燃煤/燃气为主（华能国际、国电电力、大唐发电…）
  · 风光：风电+光伏为主的新能源运营商（三峡能源、绿发电力…）
  · 光伏：分布式光伏电站投资运营（芯能科技）
  · 热电联产：给园区供汽+发电（富春环保、新中港…）
  · 供热：纯居民/园区供热（金房能源、京能热力…）
  · 电网供电：地方供电公司（涪陵电力、郴电国际…）
  · 综合能源：多电源+综合服务、无单一主导（国投电力水电为主→仍记水电；电投绿能记综合）
  · 待定：新筑股份（轨交为主，已在上一轮标注）

写前备份；幂等。
运行：`python tag_power_type_20261108.py [--apply]`
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
CONCEPT = "电力运营"
FIELD = "jiuyan_power_type"
NOTE_KEY = "jiuyan_power_type_20261108"

TYPE = {
    # 水电
    "000601": "水电", "000722": "水电", "000993": "水电", "002039": "水电",
    "600025": "水电", "600236": "水电", "600886": "水电", "600900": "水电",
    "600505": "水电", "600644": "水电", "600101": "水电",
    # 火电
    "000037": "火电", "000539": "火电", "000543": "火电", "000600": "火电",
    "000767": "火电", "000899": "火电", "001896": "火电", "600011": "火电",
    "600027": "火电", "600396": "火电", "600578": "火电", "600744": "火电",
    "600795": "火电", "600863": "火电", "601991": "火电",
    # 核电
    "003816": "核电", "601985": "核电",
    # 风光
    "000155": "风光", "000537": "风光", "000595": "风光", "000862": "风光",
    "001258": "风光", "002310": "风光", "600032": "风光", "600163": "风光",
    "600905": "风光", "603693": "风光",
    # 光伏
    "603105": "光伏",
    # 热电联产
    "001376": "热电联产", "002479": "热电联产", "002616": "热电联产",
    "600719": "热电联产", "600982": "热电联产", "605011": "热电联产",
    "605028": "热电联产", "605162": "热电联产", "605580": "热电联产",
    # 供热
    "001210": "供热", "002893": "供热", "600149": "供热",
    # 电网供电
    "600452": "电网供电", "600969": "电网供电",
    # 综合能源
    "000791": "综合能源", "000875": "综合能源", "002015": "综合能源",
    "003035": "综合能源", "600021": "综合能源", "600310": "综合能源",
    "600483": "综合能源", "600979": "综合能源",
    # 待定
    "002480": "待定",
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


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    members = [c for c, e in wl.items() if isinstance(e, dict)
               and any(cc == "电力" and cn == CONCEPT for cc, cn in get_pairs(e))]
    missing = [c for c in members if c not in TYPE]
    print(f"电力运营 {len(members)} 只；已分类 {len(members)-len(missing)}；未分类 {missing}")
    dist = Counter(TYPE.get(c, "未分类") for c in members)
    print("分布：", dict(dist.most_common()))

    changed = []
    for c in members:
        t = TYPE.get(c)
        if not t:
            continue
        e = wl[c]
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        old = str(rs.get(CONCEPT) or "")
        base = old.split("（")[0].strip() if old else f"{e.get('name') or c}："
        # 统一成「名称：主营（发电运营·类型）」
        new = f"{base}（发电运营·{t}）"
        if old == new and e.get(FIELD) == t:
            continue
        changed.append((c, e.get("name"), t, new, old))
        if apply:
            rs[CONCEPT] = new
            e["jiuyan_concept_reason"] = rs
            e[FIELD] = t
            e[NOTE_KEY] = now

    print(f"待更新 {len(changed)} 只")
    for x in changed[:8]:
        print(f"  {x[0]} {x[1]} [{x[2]}]: {x[3]}" + (f"   （原：{x[4]}）" if x[4] else ""))
    if len(changed) > 8:
        print(f"  … 其余 {len(changed)-8} 只")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_power_type_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print("已落盘；备份 backup/watchlist_jiuyan_before_power_type_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
