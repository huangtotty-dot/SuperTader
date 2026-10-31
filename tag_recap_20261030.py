# -*- coding: utf-8 -*-
"""2026-10-30 韭研涨停复盘 → 补韭研概念（owner：「图片里的股票有没有漏掉，漏了核对后归入」）。

守 `stock_hunter/watchlist_jiuyan.json` 的**只增不改**惯例（见 memory
`jiuyan_tagging_convention`）：
  · 目标大类目已存在 ⇒ 跳过（不覆盖、不改概念）；
  · 完全没有概念 ⇒ 写第一对（大类目 + 细分）；
  · 只有旧字段（jiuyan_category / jiuyan_concept）⇒ 下沉到 cat1/con1..N，再追加新对；
  · 写前备份到 backup/。

**故意不补**（已在池内且现有分类已覆盖，补了反而重复/误导）：
  002636 金安国纪 / 605058 澳弘电子 —— PCB（覆铜板/服务器板）已覆盖「AI硬件」；
  001287 中电港 —— 半导体（分销）已覆盖；
  002365 永安药业 —— 医药（化学制药）已覆盖（复盘里的「公告」按医药归位）；
  601083 锦江航运 —— 分类表缺「交运/航运」一级分类，待 owner 定，不硬塞。
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

# code -> [(大类目, 细分概念)]（新增对；已有的不重复加）
ADD = {
    # ── 完全不在猎手池（无任何韭研概念）──
    "600113": [("大消费", "农批市场")],        # 浙江东日：零售/农产品批发
    "300030": [("医药", "医疗器械")],          # 阳普医疗
    "600261": [("商业航天", "航天配套")],      # 阳光照明：复盘归入商业航天
    "603273": [("机器人", "人形机器人")],      # 天元智能：众擎机器人+制氢设备
    "000897": [("地产", "天津国资")],          # 津滨发展：房地产开发+天津国资
    "000716": [("大消费", "休闲食品")],        # 黑芝麻
    "605169": [("化工", "天然气")],            # 洪通燃气：天然气/LNG
    "603587": [("大消费", "服装IP")],          # 地素时尚：泡泡玛特联名/谷子经济
    "301187": [("机器人", "护理机器人")],      # 欧圣电气
    "000417": [("大消费", "百货零售")],        # 合百集团：超市百货
    "603198": [("大消费", "白酒")],            # 迎驾贡酒
    "600285": [("医药", "中药")],              # 羚锐制药（MERGES：中药归医药）
    "603233": [("医药", "连锁药房")],          # 大参林
    "000568": [("大消费", "白酒")],            # 泸州老窖
    "002652": [("半导体", "洁净室工程")],      # 扬子新材：半导体洁净室
    "002088": [("化工", "耐火材料")],          # 鲁阳节能（陶瓷纤维）
    "000525": [("化工", "农药")],              # 红太阳（百草枯）
    "600510": [("地产", "新型城镇化")],        # 黑牡丹
    # ── 已在池内，但复盘所属板块确实缺失（补第二对）──
    "002086": [("大消费", "海洋食品")],        # 东方海洋：海洋食品 + 医疗器械
    "600189": [("大消费", "矿泉水")],          # 泉阳泉
    "600721": [("医药", "临床CRO")],           # 百花医药（MERGES：创新药归医药）
    "002275": [("医药", "中药")],              # 桂林三金（MERGES：中药归医药）
    "603703": [("商业航天", "卫星通信")],      # 盛洋科技：卫星通信/Eutelsat
    "688045": [("半导体", "模拟芯片")],        # 必易微：模拟芯片（原标签是机器人/芯片）
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
    changed, skipped, missing = [], [], []
    for code, adds in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            missing.append(code)
            continue
        pairs = get_pairs(e)
        cats = [c for c, _ in pairs]
        added = []
        for cat, con in adds:
            if cat in cats:
                continue
            pairs.append((cat, con))
            cats.append(cat)
            added.append((cat, con))
        if not added:
            skipped.append((code, e.get("name"), cats))
            continue
        put_pairs(e, pairs, now)
        changed.append((code, e.get("name"), added, cats))

    if changed:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_recap_20261030.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"写入 {len(changed)} 只；跳过 {len(skipped)} 只；不在 watchlist {len(missing)} 只")
    for code, name, added, cats in changed:
        print(f"  + {code} {name}: " + ", ".join(f"{c}/{p}" for c, p in added)
              + f"   （现: {'|'.join(cats)}）")
    for code, name, cats in skipped:
        print(f"  = {code} {name} 已含目标类目（现: {'|'.join(cats)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
