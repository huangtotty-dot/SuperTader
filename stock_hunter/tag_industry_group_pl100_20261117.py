# -*- coding: utf-8 -*-
"""同花顺漂亮100 行业类别标签（2026-11-17，owner：「GUI中的同花顺漂亮100请做分类」）。

口径：
- 只给 category 含「同花顺漂亮100」的 100 只标的写 `industry_group` 字段。
- 分组口径沿用 2026-11-17 中特估100 的命名习惯（银行/证券/保险/油气石化/军工装备/
  港口航运/航空机场/钢铁有色 等同名同义），与中特估100 重叠的 13 只标的
  （600036/600030/601318/601669/600585/002271/600019/601899/601088/600900/600905/
  601888/002493/601919/600009/600760）分组名与中特估保持一致，避免同一字段两种写法。
- 若标的已有 industry_group 且与新分组不一致，则中止并打印冲突清单，不覆盖。
- 改动前自动备份 watchlist_concept.json → .bak_20261117_industry_group_pl100。
"""
import json, sys, shutil

sys.stdout.reconfigure(encoding="utf-8")
PATH = "stock_hunter/watchlist_concept.json"
BACKUP = "stock_hunter/watchlist_concept.json.bak_20261117_industry_group_pl100"

# 行业类别 → 代码（100 只，33 组）
GROUPS = {
    "银行": ["600036"],
    "证券": ["600030", "000776", "300059"],
    "保险": ["601318"],
    "房地产": ["000002", "600048"],
    "建筑基建": ["601669"],
    "建材": ["600585", "002271"],
    "白酒": ["600519", "000858", "000568", "600809"],
    "食品饮料": ["600887", "600600", "603288", "300999"],
    "家电家居": ["000333", "000651", "600690", "603195", "603833"],
    "商贸消费": ["601888"],
    "农业": ["002311", "002714"],
    "医药医疗": ["600276", "603259", "300122", "603392", "300760",
                 "300015", "300896"],
    "中药": ["000538", "600436"],
    "汽车整车": ["002594", "600104", "601238", "601633"],
    "汽车零部件": ["000338", "600660"],
    "锂电池": ["300750", "300014", "002709", "002812", "300450", "603799"],
    "锂矿资源": ["000792", "002460", "002466"],
    "光伏": ["601012", "600438", "002129", "300274", "688223", "688599"],
    "电力": ["600900", "600025", "600905", "001289"],
    "电网设备": ["600406"],
    "煤炭": ["601088", "601225", "600188"],
    "油气石化": ["002493", "600346", "000301", "600309"],
    "钢铁有色": ["600019", "601899", "603993", "600111"],
    "半导体": ["688981", "002371", "002049", "300782", "603501",
               "600703", "600745"],
    "消费电子": ["002475", "002241", "300433", "601138", "688036",
                 "000725"],
    "AI与软件": ["002230", "600588", "688111", "601360", "002415"],
    "通信": ["000063"],
    "传媒": ["002027"],
    "军工装备": ["600760", "600893"],
    "机械装备": ["600031", "601100", "300124"],
    "交通运输": ["002352"],
    "港口航运": ["601919"],
    "航空机场": ["600009"],
}


def main():
    code2group = {}
    for g, codes in GROUPS.items():
        for c in codes:
            assert c not in code2group, f"代码重复: {c}"
            code2group[c] = g
    assert len(code2group) == 100, f"映射应覆盖 100 只，实际 {len(code2group)}"

    shutil.copy2(PATH, BACKUP)
    print(f"[备份] {BACKUP}")

    with open(PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    pl_codes = set()
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        blob = "|".join(str(v.get(f, "")) for f in
                        ("category", "category1", "category2"))
        if "漂亮100" in blob:
            pl_codes.add(k)

    missing = sorted(pl_codes - set(code2group))
    extra = sorted(set(code2group) - pl_codes)
    assert not missing, f"板块内漏配: {missing}"
    assert not extra, f"映射里但不在漂亮100: {extra}"

    # 冲突守卫：与中特估已写入的分组不一致则中止，不覆盖
    conflicts = [(c, data[c].get("industry_group"), g)
                 for c, g in code2group.items()
                 if data.get(c, {}).get("industry_group")
                 and data[c]["industry_group"] != g]
    assert not conflicts, f"与既有 industry_group 冲突: {conflicts}"

    n = 0
    for code, g in code2group.items():
        if code in pl_codes:
            data[code]["industry_group"] = g
            n += 1

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"[完成] 写入 industry_group {n} 只 / 板块共 {len(pl_codes)} 只")

    # 校验：分组统计
    from collections import Counter
    cnt = Counter(code2group[c] for c in pl_codes)
    for g, k in sorted(cnt.items(), key=lambda x: -x[1]):
        print(f"  {g}: {k}")


if __name__ == "__main__":
    main()
