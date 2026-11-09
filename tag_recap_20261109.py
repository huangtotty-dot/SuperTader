# -*- coding: utf-8 -*-
"""2026-11-09 韭研全天涨停复盘简图（54 只/10 段）→ 补齐遗漏概念（只增不改）。

对账（scripts/daily_recap_ingest.py）给出 26 个候选；本脚本按**主营 + 复盘关键词**处理：

新建/补首标签（不在猎手池）：
  603660 苏州科达 → AI应用/AI视觉             （视讯与安防，复盘：AI应用+一体机）
  001379 腾达科技 → 商业航天/火箭结构与紧固件   （不锈钢紧固件）
  301226 祥明智能 → 机器人/电机               （微特电机/风机）
  600776 东方通信 → 商业航天/卫星通信终端与天线 （信息通信/卫星导航）
  301718 N通则   → 商业航天/卫星通信终端与天线 （5G FWA/卫星通信终端）
  001331 胜通能源 → 机器人/本体及应用          （七腾机器人控股）
  002488 金固股份 → 汽车/轮毂                 （钢制车轮）
  000026 飞亚达   → 机器人/减速器             （精密制造/减速器）
  002931 锋龙股份 → 机器人/结构件             （园林机械/汽车零部件）
  603706 东方环宇 → 电力/电力运营             （燃气+供热）
  688585 上纬新材 → 化工/改性材料             （乙烯基酯树脂）
  002638 勤上股份 → 消费电子/LED              （LED 照明）

追加（已有票，缺该大类才加）：
  001316 润贝航科 + 商业航天/航天材料与增材制造
  603683 晶华新材 + 机器人/材料               （机器人电子皮肤/胶粘材料）

跳过并报告（现有词表无合适大类）：
  001390 古麒绒材（羽绒材料）、002889 东方嘉盛（供应链物流）、
  600278 东方创业（外贸物流）、002596 海南瑞泽（混凝土）、
  001226 拓山重工（工程机械件）、603389 亚振家居（家居/培育钻石）

守 `jiuyan_tagging_convention`：只增不改、成对写、写前备份、幂等。
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
    "603660": ("AI应用", "AI视觉"),
    "001379": ("商业航天", "火箭结构与紧固件"),
    "301226": ("机器人", "电机"),
    "600776": ("商业航天", "卫星通信终端与天线"),
    "301718": ("商业航天", "卫星通信终端与天线"),
    "001331": ("机器人", "本体及应用"),
    "002488": ("汽车", "轮毂"),
    "000026": ("机器人", "减速器"),
    "002931": ("机器人", "结构件"),
    "603706": ("电力", "电力运营"),
    "688585": ("化工", "改性材料"),
    "002638": ("消费电子", "LED"),
    "001316": ("商业航天", "航天材料与增材制造"),
    "603683": ("机器人", "材料"),
}
NEW = {
    "603660": "苏州科达", "001379": "腾达科技", "301226": "祥明智能",
    "600776": "东方通信", "301718": "通则康威", "001331": "胜通能源",
    "002488": "金固股份", "000026": "飞亚达", "002931": "锋龙股份",
    "603706": "东方环宇", "688585": "上纬新材", "002638": "勤上股份",
}
SKIP = {
    "001390": "古麒绒材：羽绒材料 —— 无对应大类",
    "002889": "东方嘉盛：供应链物流 —— 无对应大类",
    "600278": "东方创业：外贸物流 —— 无对应大类",
    "002596": "海南瑞泽：混凝土/园林 —— 无对应大类",
    "001226": "拓山重工：工程机械件 —— 无对应大类",
    "603389": "亚振家居：家居/培育钻石 —— 无对应大类",
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
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed, created, skipped = [], [], []
    for code, (cat, con) in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            e = {"name": NEW.get(code, code), "business_summary": "", "concept_boards": [],
                 "industry_boards": [], "primary_source": "manual", "sector": con,
                 "sector_type": "manual", "jiuyan_category": "", "jiuyan_concept": ""}
            put_pairs(e, [(cat, con)], now)
            wl[code] = e
            created.append((code, e["name"], cat, con))
            continue
        pairs = get_pairs(e)
        if any(c == cat for c, _ in pairs):
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, [c for c, _ in pairs]))
    for code, why in SKIP.items():
        skipped.append((code, (wl.get(code) or {}).get("name", ""), why))
    if changed or created:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_recap_20261109_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"补齐 {len(changed)} 只；新建 {len(created)} 只；跳过 {len(skipped)} 只")
    for c, n, cat, con in created:
        print(f"  ★ {c} {n}: 新建 {cat}/{con}")
    for c, n, cat, con, old in changed:
        print(f"  + {c} {n}: {cat}/{con}（原 {'|'.join(old) or '空'}）")
    for c, n, why in skipped:
        print(f"  = {c} {n}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
