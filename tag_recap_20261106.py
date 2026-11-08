# -*- coding: utf-8 -*-
"""2026-11-06 韭研全天涨停复盘简图 → 补齐遗漏/缺失的韭研概念（只增不改）。

来源：owner 2026-11-08 传的「韭研公社全天涨停复盘简图（11.06）」，
      32 只涨停 / 9 个分段；对账由 `scripts/daily_recap_ingest.py` 产出（16 个候选）。

处理原则：
  · 已有票 **只增不改**：只在缺少该大类时追加一对 (cat, con)；已有同大类不重复加；
  · 不在池里的票按截图关键词 + 主营归类（公告/其他段也按真实题材写，玄学词如「东方系/阳字辈」不写）；
  · 没有合适分类的**不硬塞**（东方嘉盛/东方创业 的「供应链物流」在现有词表里没有对应大类，跳过并报告）；
  · 写前备份；脚本幂等。

口径见 memory `jiuyan_tagging_convention` / `jiuyan-打标签惯例`。
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

# 追加（已有票，只在缺该大类时加）
ADD = {
    "002578": ("光伏", "太空光伏辅材"),        # 闽发铝业：光伏边框/铝型材（现 有色金属/铝）
    "301697": ("光伏", "太空光伏辅材"),        # N贝特利：光伏银粉（现 半导体/封装材料）
    "601188": ("大消费", "大消费_冰雪经济"),   # 龙江交通：冰雪旅游客流（现 冰雪经济/旅游消费）
    "600327": ("大消费", "大消费_百货零售"),   # 大东方：百货零售（现 医药/医疗服务）
    "000525": ("大农业", "农药"),              # 红太阳：农药（现 化工/农药）
}
# 新建（不在池里/无韭研标签）
NEW = {
    "600207": {"name": "安彩高科", "pair": ("光伏", "光伏玻璃")},          # 光伏玻璃+玻璃基板+石英砂
    "603366": {"name": "日出东方", "pair": ("光伏", "光储充")},            # 光伏+热泵+储能+液冷超充
    "002973": {"name": "侨银股份", "pair": ("算力租赁", "算力租赁")},      # 算力服务+环卫
    "600156": {"name": "华升股份", "pair": ("算力租赁", "算力租赁")},      # 算力租赁+液冷+纺织
    "000595": {"name": "新能股份", "pair": ("电力", "电力运营")},          # 绿色电力+储能电站
    "001308": {"name": "康冠科技", "pair": ("消费电子", "光学显示")},      # 智能显示 ODM（端侧AI硬件）
    "001316": {"name": "润贝航科", "pair": ("低空经济", "制造链")},        # 航空材料/航材分销
    "002712": {"name": "思美传媒", "pair": ("AI应用", "AI影视短剧")},      # AI短剧+AI营销
    "601595": {"name": "上海电影", "pair": ("AI应用", "AI影视短剧")},      # AI影视+IP
    "002388": {"name": "新亚制程", "pair": ("电池", "电解液")},            # 固态电池+电解液+AI服务器电源
    "603657": {"name": "春光科技", "pair": ("机器人", "本体及应用")},      # 清洁电器 ODM / 具身机器人合作
    "300237": {"name": "美晨科技", "pair": ("汽车", "零部件")},            # 摘帽+汽车零部件+燃料电池
}
# 明确跳过（无合适分类，别硬塞）
SKIP = {
    "002889": "东方嘉盛：供应链物流/光刻机寄售维修 —— 现有词表无对应大类",
    "600278": "东方创业：外贸/物流 —— 现有词表无对应大类",
    "300686": "智动力：端侧AI 无对应细分概念；已有 消费电子/消费电子，保持",
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
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if any(c == cat for c, _ in pairs):
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, pairs))
    for code, spec in NEW.items():
        e = wl.get(code)
        if isinstance(e, dict):
            pairs = get_pairs(e)
            cat, con = spec["pair"]
            if any(c == cat for c, _ in pairs):
                skipped.append((code, e.get("name"), f"已含 {cat}"))
                continue
            put_pairs(e, pairs + [(cat, con)], now)
            changed.append((code, spec["name"], cat, con, pairs))
            continue
        e = {"name": spec["name"], "business_summary": "", "concept_boards": [],
             "industry_boards": [], "primary_source": "manual",
             "sector": spec["pair"][1], "sector_type": "manual",
             "jiuyan_category": "", "jiuyan_concept": ""}
        put_pairs(e, [spec["pair"]], now)
        wl[code] = e
        created.append((code, spec["name"], spec["pair"]))
    for code, why in SKIP.items():
        skipped.append((code, (wl.get(code) or {}).get("name", ""), why))
    if changed or created:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_recap_20261106_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"补齐 {len(changed)} 只；新建 {len(created)} 只；跳过 {len(skipped)} 只")
    for code, name, cat, con, old in changed:
        print(f"  + {code} {name}: {cat}/{con}（原 {'|'.join(c for c,_ in old) or '空'}）")
    for code, name, pair in created:
        print(f"  ★ {code} {name}: 新建 {pair[0]}/{pair[1]}")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
