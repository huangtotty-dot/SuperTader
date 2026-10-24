# -*- coding: utf-8 -*-
"""金融板块 **全量** 逐股判词（100 只）。

规则：券商/保险/期货/多元金融/金融IT/银行默认核心，例外逐只列在 OVERRIDE。

用法：``python stock_hunter/audit_board_fin_full_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_fin"
MARK = "2026-10-25 06:00:00"

CORE = {"保险", "券商", "多元金融", "期货", "金融IT", "金融央企", "创投"}
RELATED = {"资本运营", "国家队", "网安细分-CA/密码"}
RANK = {"核心": 3, "相关": 2, "边缘": 1}

OVERRIDE = {
    "600864": ("相关", "哈投股份：热电为主，江海证券为参股"),
    "000712": ("核心", "锦龙股份：控股中山证券，证券为主业"),
    "300688": ("相关", "创业黑马：企业加速服务，创投为一块"),
    "000958": ("边缘", "电投产融：核能业务为主，金融为副业"),
    "300399": ("边缘", "天利科技：融合通信业务为主"),
    "002657": ("相关", "中科金财：AI 综合服务，金融客户为主"),
    "300542": ("相关", "新晨科技：软件开发，金融客户为主"),
    "301236": ("相关", "软通动力：通用 IT 服务，金融为行业之一"),
    "603171": ("相关", "税友股份：财税软件为主"),
    "300579": ("相关", "数字认证：CA/密码，金融为客群"),
    "600318": ("相关", "新力金融：融资租赁（供销社体系）"),
    "002423": ("核心", "中粮资本：信托/期货/保险等金融牌照"),
    "600061": ("核心", "国投资本：证券+信托+基金"),
}


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    log = []
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        cons = []
        for i in list(range(1, 10)) + [0]:
            k = "jiuyan_category" if i == 0 else f"jiuyan_category{i}"
            ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
            if "金融" not in str(rec.get(k) or ""):
                continue
            v = rec.get(ck)
            cons += [str(t).strip() for t in (v if isinstance(v, list) else str(v or "").split("|")) if str(t).strip()]
        if not cons:
            continue
        cons = list(dict.fromkeys(cons))
        if code in OVERRIDE:
            verdict, reason = OVERRIDE[code]
        else:
            v = "边缘"
            for c in cons:
                if c in CORE:
                    v = "核心"; break
                if c in RELATED and RANK["相关"] > RANK[v]:
                    v = "相关"
            verdict, reason = v, ""
        rec["jiuyan_tag_verdict"] = verdict
        if reason:
            d = rec.get("jiuyan_concept_reason")
            if not isinstance(d, dict):
                d = {}
            for c in cons:
                if not str(d.get(c) or "").strip() or str(d.get(c) or "").startswith("主营"):
                    d[c] = reason
            rec["jiuyan_concept_reason"] = d
        rec["updated_at"] = MARK
        log.append((code, rec.get("name"), verdict))

    print(f"写入 {len(log)} 只；分布 {dict(Counter(v for _, _, v in log))}")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
