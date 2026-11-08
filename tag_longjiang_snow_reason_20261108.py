# -*- coding: utf-8 -*-
"""2026-11-08 owner：在**公司资料**里补充「龙江交通 601188 为什么与冰雪经济相关」。

依据（2026-11-08 核实，来源见交付说明）：
  · 主业 = 哈大高速（哈尔滨—大庆，132.8 公里）收费运营 + 子公司龙运现代出租车；
  · 公司在互动易明确：**哈尔滨旅游热对哈大高速车流量、龙运现代出租车平均运单额
    有正向拉动作用**；冰雪季公司承担哈大高速保通保畅与出租车运力保障；
  · 公司同时澄清：**与冰雪大世界没有直接合作业务** ⇒ 属「客流受益」而非直接标的。

写入位置：`watchlist_jiuyan.json` 的 `jiuyan_concept_reason`（概念 chip 悬停 + 公司资料
「题材关联」一栏都用它）。只改这一只这一条理由，其它字段不动；写前备份；幂等。
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

CODE = "601188"
CONCEPT_KEY = "旅游消费"          # 该股的细分概念（冰雪经济板块下）
REASON = (
    "冰雪经济：主业哈大高速（哈尔滨—大庆）是冰雪旅游季主要客流通道，"
    "公司披露哈尔滨旅游热对哈大高速车流量、子公司龙运现代出租车平均运单额有正向拉动，"
    "冬季承担保通保畅与运力保障；但公司明确与冰雪大世界无直接合作业务 ⇒ 属客流受益，非直接标的。"
)


def main() -> int:
    wl = json.loads(WL.read_text(encoding="utf-8"))
    e = wl.get(CODE)
    if not isinstance(e, dict):
        print(f"{CODE} 不在 watchlist")
        return 1
    d = e.get("jiuyan_concept_reason")
    if not isinstance(d, dict):
        d = {}
    old = str(d.get(CONCEPT_KEY) or "")
    if old == REASON:
        print(f"{CODE} 理由已是最新，无需改动")
        return 0
    d[CONCEPT_KEY] = REASON
    e["jiuyan_concept_reason"] = d
    e["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_longjiang_reason_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"{CODE} {e.get('name')} 更新「{CONCEPT_KEY}」理由：")
    print(f"  旧: {old}")
    print(f"  新: {REASON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
