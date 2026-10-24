# -*- coding: utf-8 -*-
"""分类归位（owner 2026-10-25 的 13 条意见里的结构性部分）。

问题根因：有些槽位把多个类目/概念塞进同一个字段（`煤化工|电力` / `煤化工|风电设备`），
下游按「类目=板块」展示时就会串板（电力的板块里出现煤化工、太空光伏里出现风电设备…）。

本脚本：
  1. 把每个槽位按 `|` **按位置拆成 (类目, 概念) 对**；
  2. 按 `CONCEPT_HOME` 把「概念其实属于别的板块」的对**归位**（数据要素→数据要素、正极/电解液→电池、
     火箭/spacex→商业航天、风电设备→电力、煤化工→煤化工、供销社→大农业、太空光伏组件→太空光伏、
     算力租赁→算力租赁、伺服系统→机器人）；
  3. 类目改名：黄金→有色金属、HVDC→电力；
  4. 个别手工处理：超捷股份只留「商业航天/结构件」（删 火箭、spacex-潜在供应商 与重复的机器人/结构件）；
  5. 重写槽位（1..9 号；不再写老字段）。

用法：``python stock_hunter/normalize_categories_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_norm"
MARK = "2026-10-25 18:00:00"

CATEGORY_MERGE = {"黄金": "有色金属", "HVDC": "电力"}

CONCEPT_HOME = {
    # 数据要素（含物理AI 数据采集）
    "数据要素_数据存储": "数据要素", "数据要素_交易所": "数据要素", "数据要素_数据确权": "数据要素",
    "数据要素_数据清洗": "数据要素", "数据要素_数据分析": "数据要素", "数据要素_数据标注": "数据要素",
    "数据要素_数据安全": "数据要素", "数据要素_内容数据": "数据要素", "数据要素_交通数据": "数据要素",
    "数据要素_医疗数据": "数据要素", "数据要素_汽车数据": "数据要素", "数据要素_司法数据": "数据要素",
    "数据要素_营销数据": "数据要素", "数据要素_学术数据": "数据要素", "数据要素_图片数据": "数据要素",
    "数据要素_酒店数据": "数据要素", "数据要素_航运数据": "数据要素", "数据要素_大宗数据": "数据要素",
    "数据要素_网络可视化监管": "数据要素", "数据要素_其他行业数据": "数据要素",
    "数据要素_北交所": "数据要素", "物理AI_数据采集": "数据要素",
    # 电池
    "正极材料": "电池", "电解液": "电池", "负极及辅材": "电池",
    # 商业航天
    "火箭": "商业航天", "卫星": "商业航天", "卫星-有效载荷": "商业航天",
    "卫星-地面设备": "商业航天", "spacex": "商业航天", "spacex-供应商": "商业航天",
    "spacex-参股": "商业航天", "spacex-潜在供应商": "商业航天", "千帆星座": "商业航天",
    "太阳翼结构": "太空光伏",
    # 电力 / 煤化工 / 大农业 / 太空光伏 / 算力租赁 / 机器人
    "风电设备": "电力",
    "煤化工": "煤化工",
    "供销社": "大农业",
    "太空光伏组件": "太空光伏", "太空光伏设备": "太空光伏", "太空光伏辅材": "太空光伏",
    "算力租赁": "算力租赁",
    "伺服系统": "机器人",
    # AI 内容类概念统一挂 AI应用（AI监管-* 除外，那属于网络安全）
    "短剧": "AI应用", "AI影视短剧": "AI应用", "AI营销": "AI应用", "AI办公": "AI应用",
    "AI游戏": "AI应用", "AI玩具": "AI应用", "AI终端": "AI应用", "AI教育": "AI应用",
}

SPECIAL = {
    # 超捷股份：只留 商业航天/结构件（火箭、spacex 删除；机器人/结构件 去重）
    "301005": [("商业航天", "结构件")],
    # 星辰科技：伺服系统是它的主业，归机器人
    "920885": [("机器人", "伺服系统")],
}

CAT_KEYS = ["jiuyan_category"] + [f"jiuyan_category{i}" for i in range(1, 10)]
CON_KEYS = ["jiuyan_concept"] + [f"jiuyan_concept{i}" for i in range(1, 10)]


def toks(v):
    """拆 token：list 里也可能再带 `|`（如 ['IaaS基础设施服务|国资云']），要一并拆开。"""
    raw = v if isinstance(v, list) else [v]
    out = []
    for x in raw:
        out += [t.strip() for t in str(x or "").split("|") if t.strip()]
    return out


def pairs_raw(rec):
    """原始槽位（**不拆** `|`），用于判断是否需要重写。"""
    out = []
    for i in range(10):
        cat = rec.get(CAT_KEYS[i])
        if isinstance(cat, str) and cat.strip():
            con = rec.get(CON_KEYS[i])
            con_s = "|".join(con) if isinstance(con, list) else str(con or "")
            out.append((cat.strip(), con_s.strip()))
    return out


def pairs_of(rec):
    out = []
    for i in range(10):
        k, ck = CAT_KEYS[i], CON_KEYS[i]
        cats = toks(rec.get(k))
        if not cats:
            continue
        cons = toks(rec.get(ck))
        for idx, c in enumerate(cats):
            con = cons[idx] if idx < len(cons) else (cons[0] if len(cons) == 1 else "")
            out.append((CATEGORY_MERGE.get(c, c), con))
    return out


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    moved, merged, changed = Counter(), Counter(), 0
    samples = []
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        raw = pairs_of(rec)
        if not raw:
            continue
        raw_slots = pairs_raw(rec)
        out = []
        for cat, con in raw:
            if cat in CATEGORY_MERGE.values() and cat not in ("有色金属", "电力"):
                pass
            home = CONCEPT_HOME.get(con)
            new_cat = CATEGORY_MERGE.get(cat, cat)
            if home and home != new_cat:
                moved[f"{new_cat}→{home}"] += 1
                new_cat = home
            if CATEGORY_MERGE.get(cat):
                merged[f"{cat}→{CATEGORY_MERGE[cat]}"] += 1
            out.append((new_cat, con))
        # 去重（保持顺序）
        seen = set()
        out = [p for p in out if not (p in seen or seen.add(p))]
        if code in SPECIAL:
            out = list(SPECIAL[code])
        needs = (out != raw
                 or any("|" in c or "|" in n for c, n in raw_slots)
                 or (str(rec.get("jiuyan_category") or "").strip()
                     and any(str(rec.get(CAT_KEYS[i]) or "").strip() for i in range(1, 10))))
        if not needs:
            continue
        changed += 1
        if len(samples) < 8:
            samples.append((code, rec.get("name"), raw, out))
        # 重写槽位：1..9 号；清掉老字段
        for i in range(1, 10):
            rec.pop(f"jiuyan_category{i}", None)
            rec.pop(f"jiuyan_concept{i}", None)
        for i, (c, n) in enumerate(out[:9], start=1):
            rec[f"jiuyan_category{i}"] = c
            rec[f"jiuyan_concept{i}"] = n
        rec.pop("jiuyan_category", None)
        rec.pop("jiuyan_concept", None)
        rec["updated_at"] = MARK

    print(f"重写 {changed} 只")
    print("概念归位：", dict(moved.most_common(20)))
    print("类目合并：", dict(merged))
    for code, name, old, new in samples:
        print(f"  {code} {name}: {old} → {new}")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
