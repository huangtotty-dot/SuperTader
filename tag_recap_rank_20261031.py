# -*- coding: utf-8 -*-
"""2026-10-31 owner：按「涨幅榜 124 只」截图逐只核对韭研概念，补齐遗漏。

核对结果（代码为主键）：
  · 74 只**已有韭研分类**（不动）；
  · 50 只缺失 —— 本脚本按图里的**行业**给它们补（大类目, 细分=行业口径）；
  · 其中 3 只原本不在 watchlist（600289 ST信通 / 688089 英科再生 / 600187 *ST国中）⇒ 新建最小记录。

⚠️ 新增两个一级分类 **交运 / 环保**：现有分类表没有它们，硬塞进其它类会错标
（例如航运塞进化工）。这两个分类目前没有对应的韭研复盘板块名，所以**不会出现在主线广度**里，
只参与板块分组与个股归类；要进广度需在 `sector_phase.CATEGORY_SECTIONS` 里加映射。

守 `jiuyan_tagging_convention`：只增不改、写前备份。
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

ADD = {
    # ── 医药 ──
    "920201": ("医药", "医疗耗材"), "301015": ("医药", "医药流通"),
    "301363": ("医药", "医疗设备"), "688765": ("医药", "血液制品"),
    "603368": ("医药", "医药流通"), "002390": ("医药", "医药流通"),
    "002550": ("医药", "生物制药"), "300289": ("医药", "诊断试剂"),
    "603939": ("医药", "线下药店"),
    # ── 大消费 ──
    "600809": ("大消费", "白酒"), "603369": ("大消费", "白酒"),
    "000860": ("大消费", "白酒"), "603919": ("大消费", "白酒"),
    "603589": ("大消费", "白酒"), "300740": ("大消费", "化妆品"),
    "300856": ("大消费", "化妆品原料"), "603899": ("大消费", "文化用品"),
    # ── PCB / 消费电子 / 电池 ──
    "300739": ("PCB", "PCB"),
    "301606": ("消费电子", "品牌消费电子"), "002724": ("消费电子", "LED"),
    "605118": ("消费电子", "光学元件"), "920438": ("消费电子", "光学元件"),
    "301210": ("电池", "锂电池"),
    # ── 化工 / 石油 / 汽车 ──
    "300829": ("化工", "食品及饲料添加剂"), "300980": ("化工", "塑料制品"),
    "603330": ("化工", "胶黏剂及胶带"), "301071": ("化工", "超硬材料"),
    "600688": ("石油", "炼油化工"),
    "603278": ("汽车", "轮胎骨架材料"), "920146": ("汽车", "底盘与发动机"),
    "920663": ("汽车", "汽车零部件"), "300695": ("汽车", "轮毂"),
    "603112": ("汽车", "铸造件"), "920058": ("汽车", "摩托车"),
    # ── 金融 ──
    "600928": ("金融", "城商行"), "002958": ("金融", "农商行"),
    "000415": ("金融", "租赁"),
    # ── 机械（沿用「机器人」大类，细分写工程机械/专用设备）──
    "301079": ("机器人", "工程机械液压件"), "920156": ("机器人", "自动化设备"),
    "603338": ("机器人", "工程机械"), "601717": ("机器人", "重型装备"),
    "002073": ("机器人", "专用设备"), "688128": ("电力", "检测设备"),
    # ── 新增一级分类：交运 / 环保 ──
    "601083": ("交运", "航运"), "601919": ("交运", "航运"),
    "600018": ("交运", "港口"), "600035": ("交运", "高速公路"),
    "688089": ("环保", "固废治理"), "600187": ("环保", "水务"),
    # ── 软件 ──
    "600289": ("AI应用", "行业应用软件"),
}

NEW = {
    "600289": {"name": "ST信通", "pair": ("AI应用", "行业应用软件")},
    "688089": {"name": "英科再生", "pair": ("环保", "固废治理")},
    "600187": {"name": "*ST国中", "pair": ("环保", "水务")},
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
    changed, skipped, created = [], [], []

    for code, (cat, con) in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            spec = NEW.get(code)
            if not spec:
                skipped.append((code, "不在 watchlist 且无新建信息", ""))
                continue
            e = {"name": spec["name"], "business_summary": "", "concept_boards": [],
                 "industry_boards": [], "primary_source": "manual",
                 "sector": con, "sector_type": "manual",
                 "jiuyan_category": "", "jiuyan_concept": ""}
            put_pairs(e, [spec["pair"]], now)
            wl[code] = e
            created.append((code, spec["name"], spec["pair"]))
            continue
        pairs = get_pairs(e)
        cats = [c for c, _ in pairs]
        if cat in cats:
            skipped.append((code, e.get("name"), "已含该大类"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), (cat, con), cats))

    if changed or created:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / "watchlist_jiuyan_before_rank_20261031.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"补齐 {len(changed)} 只；新建 {len(created)} 只；跳过 {len(skipped)} 只")
    for code, name, (cat, con), cats in changed:
        print(f"  + {code} {name}: {cat}/{con}   （原: {'|'.join(cats) or '—'}）")
    for code, name, pair in created:
        print(f"  ★ {code} {name}: 新建 {pair[0]}/{pair[1]}")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
