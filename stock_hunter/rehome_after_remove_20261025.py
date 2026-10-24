# -*- coding: utf-8 -*-
"""给摘牌后暂时无标签的 4 只补挂正确归属（owner 2026-10-25「补」）。

只增不改：这 4 只当前概念全空，直接写老字段（jiuyan_category / jiuyan_concept）。
判词同步更新（核心/相关），并保留 `jiuyan_tag_removed_20261025` 的审计记录。

用法：``python stock_hunter/rehome_after_remove_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_rehome"
MARK = "2026-10-25 17:00:00"

ADD = {
    "300750": ("电池", "其他关键材料及设备", "核心", "宁德时代：动力/储能电池龙头"),
    "603306": ("汽车", "零部件", "核心", "华懋科技：汽车安全气囊布"),
    "688611": ("电力", "智能电网-输变配电", "核心", "杭州柯林：电网数字化智能感知设备"),
    "002654": ("消费电子", "光学显示", "相关", "万润科技：LED 照明/标识，光学显示为延伸"),
}


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    log = []
    for code, (cat, con, verdict, reason) in ADD.items():
        rec = data.get(code)
        if not isinstance(rec, dict):
            print("不在表", code); continue
        rec["jiuyan_category"] = cat
        rec["jiuyan_concept"] = con
        rec["jiuyan_tag_verdict"] = verdict
        d = rec.get("jiuyan_concept_reason")
        if not isinstance(d, dict):
            d = {}
        d[con] = reason
        rec["jiuyan_concept_reason"] = d
        rec["updated_at"] = MARK
        log.append((code, rec.get("name"), f"{cat}/{con}", verdict))

    print(f"补挂 {len(log)} 只：")
    for code, name, pair, v in log:
        print(f"  {code} {name:<7s} → {pair} [{v}]")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
