# -*- coding: utf-8 -*-
"""中特估100 行业类别标签（2026-11-17，owner：「GUI显示的时候按照类别进行显示」）。

口径：
- 只给 category 含「同花顺中特估100」的 100 只标的写 `industry_group` 字段。
- 分组口径 = 2026-11-17 与 owner 对齐的 20 组行业主线梳理（基于 watchlist 的
  sector 字段重新归并；sector 原字段粒度不一，不直接采用）。
- 核心资产标注**不落盘**：后端按 category/sector 含「同花顺漂亮100」实时计算下发。
- 改动前自动备份 watchlist_concept.json → .bak_20261117_industry_group。
"""
import json, sys, shutil

sys.stdout.reconfigure(encoding="utf-8")
PATH = "stock_hunter/watchlist_concept.json"
BACKUP = "stock_hunter/watchlist_concept.json.bak_20261117_industry_group"

# 代码 → 行业类别（100 只，20 组）
GROUPS = {
    "银行": ["600036", "601288", "601328", "601398", "601939", "601988", "601998"],
    "证券": ["600030", "601066", "601211", "601881", "601995"],
    "保险": ["601318", "601319", "601601", "601628"],
    "金控": ["000617", "002423"],
    "建筑基建": ["000065", "000498", "000928", "002051", "002541", "600039",
                 "600502", "600720", "600970", "601117", "601186", "601390",
                 "601611", "601618", "601668", "601669", "601800", "601868"],
    "建材": ["002271", "002302", "600176", "600326", "600425", "600585"],
    "港口航运": ["001872", "600018", "600026", "601018", "601298", "601598",
                 "601866", "601919"],
    "航空机场": ["600004", "600009", "600115", "601111"],
    "铁路公路": ["001965", "601006", "601816"],
    "油气石化": ["002493", "600028", "600339", "600938", "601808", "601857"],
    "煤炭": ["601088", "601898"],
    "电力": ["003816", "600886", "600900", "600905", "601985"],
    "通信": ["600050", "600941", "601698", "601728"],
    "军工装备": ["000519", "600072", "600150", "600685", "600760"],
    "装备制造": ["601106", "601608", "601766"],
    "钢铁有色": ["000708", "000831", "000923", "000932", "600019", "601061",
                 "601600", "601899"],
    "电网设备": ["601179"],
    "科技算力": ["603019", "688041"],
    "传媒出版": ["600977", "601858", "601949"],
    "商贸消费": ["600415", "600916", "601888"],
    "房地产": ["600007"],
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

    zgt_codes = {k for k, v in data.items()
                 if isinstance(v, dict) and "中特估100" in str(v.get("category", ""))}
    missing = sorted(zgt_codes - set(code2group))
    extra = sorted(set(code2group) - zgt_codes)
    assert not missing, f"板块内漏配: {missing}"
    if extra:
        print(f"[WARN] 映射里但不在中特估100（跳过）: {extra}")

    n = 0
    for code, g in code2group.items():
        if code in zgt_codes:
            data[code]["industry_group"] = g
            n += 1

    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"[完成] 写入 industry_group {n} 只 / 板块共 {len(zgt_codes)} 只")

    # 校验：分组统计
    from collections import Counter
    cnt = Counter(code2group[c] for c in zgt_codes)
    for g, k in sorted(cnt.items(), key=lambda x: -x[1]):
        print(f"  {g}: {k}")


if __name__ == "__main__":
    main()
