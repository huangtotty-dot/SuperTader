# -*- coding: utf-8 -*-
"""2026-10-31 owner：按韭研「商业航天」板块截图逐只核对，补齐遗漏。

做法与 `jiuyan_tagging_convention` 一致：**只增不改**（目标大类目已存在就跳过；
没有概念就写第一对；已有其它对就追加，不动原有序），写前备份。

⚠️ **刻意不加回的一只**：002985 北摩高科 —— 2026-10-27 的商业航天证据审计已明确
「摘除」（真实产品是飞机刹车/起落架，没有商业卫星/火箭客户或订单，旧"千帆星座"标签
无供货证据）。这次不擅自推翻该结论，留给 owner 决定。

⚠️ 688270（ST臻镭）原本不在 watchlist（不在猎手池）——按截图新建一条最小记录
（名称 + 商业航天/星载射频芯片），其余字段留空走默认。
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

# 20 只"缺商业航天"里，除 002985 外全部补齐（概念取公司主业口径）
ADD = {
    "300034": [("商业航天", "高温合金与钛合金材料")],     # 钢研高纳
    "002151": [("商业航天", "北斗导航芯片与终端")],       # 北斗星通
    "301232": [("商业航天", "航天紧固件")],               # 飞沃科技
    "002297": [("商业航天", "航天复合材料与刹车材料")],   # 博云新材
    "920576": [("商业航天", "航天金属复合材料")],         # 天力复合
    "002171": [("商业航天", "航天铜基材料")],             # 楚江新材
    "300699": [("商业航天", "航天碳纤维与复材")],         # 光威复材
    "002651": [("商业航天", "航空零部件与锻件")],         # 利君股份
    "688239": [("商业航天", "火箭环形锻件")],             # 航宇科技
    "688818": [("商业航天", "空间电源")],                 # 电科蓝天
    "688651": [("商业航天", "卫星网络安全")],             # 盛邦安全
    "301117": [("商业航天", "测控与信息安全")],           # 佳缘科技
    "300964": [("商业航天", "航天PCB")],                  # 本川智能
    "688668": [("商业航天", "航天连接器")],               # 鼎通科技
    "300747": [("商业航天", "航天激光加工")],             # 锐科激光
    "300489": [("商业航天", "红外光学材料")],             # 光智科技
    "600877": [("商业航天", "星载芯片")],                 # 电科芯片
    "603698": [("商业航天", "航天科技集团（煤气化）")],   # 航天工程
    "920564": [("商业航天", "遥感测绘与地理信息")],       # 天润科技
}

# 不在 watchlist 的新增（截图里有、池子里没有）
NEW = {
    "688270": {"name": "ST臻镭", "pairs": [("商业航天", "星载射频芯片")]},
}


def norm(x):
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
    lc = norm(e.get("jiuyan_category"))
    lp = norm(e.get("jiuyan_concept"))
    return [(t.strip(), lp) for t in lc.split("|") if t.strip()] if lc else []


def put_pairs(e: dict, pairs: list, now: str):
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
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed, skipped = [], []
    for code, adds in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "不在 watchlist", []))
            continue
        pairs = get_pairs(e)
        cats = [c for c, _ in pairs]
        added = [(c, p) for c, p in adds if c not in cats]
        if not added:
            skipped.append((code, e.get("name"), cats))
            continue
        put_pairs(e, pairs + added, now)
        changed.append((code, e.get("name"), added, cats))

    created = []
    for code, spec in NEW.items():
        if code in wl:
            continue
        e = {"name": spec["name"], "business_summary": "", "concept_boards": [],
             "industry_boards": [], "primary_source": "manual",
             "sector": "商业航天", "sector_type": "manual",
             "jiuyan_category": "", "jiuyan_concept": ""}
        put_pairs(e, spec["pairs"], now)
        wl[code] = e
        created.append((code, spec["name"], spec["pairs"]))

    if changed or created:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / "watchlist_jiuyan_before_cs_20261031.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"补齐 {len(changed)} 只；跳过 {len(skipped)} 只；新建 {len(created)} 只")
    for code, name, added, cats in changed:
        print(f"  + {code} {name}: " + ", ".join(f"{c}/{p}" for c, p in added)
              + f"   （原: {'|'.join(cats) or '—'}）")
    for code, name, pairs in created:
        print(f"  ★ {code} {name}: 新建 " + ", ".join(f"{c}/{p}" for c, p in pairs))
    for code, name, cats in skipped:
        print(f"  = {code} {name}: {cats if isinstance(cats, str) else '|'.join(cats)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
