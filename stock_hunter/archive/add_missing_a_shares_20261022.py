# -*- coding: utf-8 -*-
"""把「市场有、韭研表没有」的非 ST A 股补进 watchlist（2026-10-22）。

owner：「全市场只有 5382 只股票，是否有很多缺失」。

实测（新浪 `Market_Center.getHQNodeData?node=hs_a` 全量拉取，2026-10-22）：

    市场 5576（沪 2317 / 深 2907 / 北 352）  vs  我们 5382  ⇒ **缺 198 只（3.5%）**

拆开看：
  · **186 只是 ST / *ST** —— 韭研表不收，本站的打标脚本也一律跳过 ST（打标惯例）⇒ **刻意不补**；
  · 12 只是非 ST、且今天都在正常交易 ⇒ **本脚本补进表里**（只写 name，不带概念 ⇒ 不进猎手池，
    但会进「突破箱体扫描」的宇宙，那才是 owner 关心的覆盖面）；
  · 另 1 只 `301139 元道退` 处于**退市整理期**（0.77 元）⇒ 不补。

反向：我们有、市场列表没有的 4 只（000004 国华网安 / 601059 信达证券 / 601198 东兴证券 /
689009 九号公司-WD）—— 属新浪 `hs_a` 节点口径差异（退市/P转板/CDR），**不动**。

用法：``python stock_hunter/add_missing_a_shares_20261022.py [--apply]``
"""
import json
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261022_addmissing"

# 非 ST、今日正常交易、但表里没有的（名称取自新浪快照）
ADD = {
    "001246": "力勤资源",
    "300527": "中船应急",
    "301569": "联亚药业",
    "301660": "粤芯半导",
    "301718": "N通则",
    "603517": "绝味食品",
    "603838": "四通股份",
    "920157": "宇特光电",
    "920162": "莫森泰克",
    "920196": "南方乳业",
    "920296": "文峰光电",
}


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    todo = [(c, n) for c, n in ADD.items() if c not in data]
    skipped = [(c, n) for c, n in ADD.items() if c in data]
    print(f"待补 {len(todo)} 只：")
    for c, n in todo:
        print(f"  {c} {n}")
    if skipped:
        print(f"已存在，跳过：{skipped}")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    shutil.copy2(SRC, BAK)
    for c, n in todo:
        data[c] = {
            "name": n,
            "jiuyan_category": "",
            "jiuyan_concept": "",
            "concept_boards": [],
            "industry_boards": [],
            "business_summary": "",
            "sector": "",
            "sector_type": "manual_add",
            "primary_source": "manual_add",
            "updated_at": "2026-10-22 00:00:00",
        }
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入 {len(todo)} 只，总条目 {len(data)}。备份: {BAK}")


if __name__ == "__main__":
    main()
