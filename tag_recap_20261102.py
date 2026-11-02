# -*- coding: utf-8 -*-
"""2026-11-02 owner：按「韭研公社午盘涨停复盘简图（11.02）」补韭研概念遗漏。

来源：`tmp/recap_20261102.json`（89 只涨停，12 个复盘板块）+ `scripts/daily_recap_ingest.py`
的对账报告。对账 44 条候选，去掉「板块缺失（已有标签、只是归类名不同）」后：

  · 真·漏票（无任何韭研标签）34 只 → 本脚本补 32 只；
  · **跳过 2 只**（现有分类表装不下，硬塞会错标）：
      603863 松炀资源（造纸/彩票）、601339 百隆东方（色纺纱/纺织）。
    要补需先加一级分类，等 owner 定。

口径（守 `jiuyan_tagging_convention`）：
  · 只增不改：已有一级分类就**追加**一对，保持原有 `jiuyan_category1` 主板块不动；
  · 大类目沿用现有分类表；细分概念尽量复用现有词表（如 AI影视短剧/AI视觉/白酒/券商），
    没有对应词时才新起一个（如「光伏支架」「MLCC与陶瓷基板」）；
  · 「公告/其他」两个分段**不作为分类写入**，但其中题材明确的个股按复盘关键词归到真实分类
    （与 10-30「公告按题材归位」的口径一致）。

写前备份到 `backup/watchlist_jiuyan_before_recap_20261102.json`。
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
    # ── AI影视 *13 ──
    "605577": ("AI应用", "AI影视短剧"),
    "601599": ("AI应用", "AI影视短剧"),
    "605287": ("AI应用", "AI影视短剧"),
    "300556": ("AI应用", "AI视觉"),
    "301218": ("AI应用", "AI影视短剧"),
    # ── 大消费 *11 ──
    "600199": ("大消费", "白酒"),
    # ── AI应用 *11 ──
    "600229": ("AI应用", "AI出版"),
    "002362": ("AI应用", "AI视觉"),
    "002949": ("AI应用", "AI设计"),
    "002774": ("AI应用", "AI制造"),
    "603466": ("AI应用", "AI视觉"),
    # ── 机器人 *9 ──
    "001230": ("机器人", "本体及应用"),
    "600960": ("机器人", "结构件"),
    "605189": ("机器人", "材料"),
    "003019": ("机器人", "本体及应用"),
    # ── 光伏 *3 ──
    "603628": ("光伏", "光伏支架"),
    # ── 商业航天 *2 ──
    "688175": ("商业航天", "卫星通信终端与天线"),
    # ── 半导体 *2 ──
    "605303": ("半导体", "芯片设计-存储芯片"),
    "002811": ("半导体", "洁净室与厂务工程"),
    # ── AI硬件 *2 ──
    "001216": ("超节点", "MLCC与陶瓷基板"),
    # ── 公告 *22（按关键词归到真实分类）──
    "603989": ("消费电子", "被动元件"),
    "600109": ("金融", "券商"),
    "600299": ("化工", "基础化工"),
    "600801": ("地产", "地产基建"),
    "600892": ("AI应用", "AI影视短剧"),
    "601019": ("AI应用", "AI出版"),
    "603887": ("算力租赁", "算力租赁"),
    # ── 其他 *6 ──
    "000819": ("石油", "炼油化工"),
    "600293": ("光伏", "光伏玻璃"),
    "000599": ("汽车", "轮胎"),
    "002983": ("消费电子", "光学显示"),
    "603016": ("电力", "电力设备"),
}

SKIP = {
    "603863": "松炀资源：造纸/彩票，现有分类表无对应一级分类",
    "601339": "百隆东方：色纺纱/纺织，现有分类表无对应一级分类",
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
    lc = norm(e.get("jiuyan_category"))
    lp = norm(e.get("jiuyan_concept"))
    return [(t.strip(), lp) for t in lc.split("|") if t.strip()] if lc else []


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
    changed, skipped = [], []

    for code, (cat, con) in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        cats = [c for c, _ in pairs]
        if cat in cats:
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, cats))
    for code, why in SKIP.items():
        skipped.append((code, (wl.get(code) or {}).get("name", ""), why))

    if changed:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / "watchlist_jiuyan_before_recap_20261102.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"补齐 {len(changed)} 只；跳过 {len(skipped)} 只")
    for code, name, cat, con, cats in changed:
        print(f"  + {code} {name}: {cat}/{con}   （原: {'|'.join(cats) or '—'}）")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
