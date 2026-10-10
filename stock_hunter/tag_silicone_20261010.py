# -*- coding: utf-8 -*-
"""有机硅概念标签追加（2026-10-10，韭研公社 有机硅(261009) 截图）。

口径：
- 一级分类 jiuyan_category = "有机硅"
- 二级 jiuyan_concept = 有机硅单体 / 硅橡胶 / 功能性硅烷 / 其他，多细分用 "|" 连接（沿用文件惯例）
- ST 股剔除（owner 2026-09-01 拍板：不需要任何 ST）→ ST宏达 不录
- 已有标签的股票：category/concept 按 "|" 追加去重，不覆盖
"""
import json, sys, shutil, datetime

sys.stdout.reconfigure(encoding="utf-8")
PATH = "stock_hunter/watchlist_jiuyan.json"

CONCEPT_MAP = {
    # 有机硅单体
    "合盛硅业": ["有机硅单体", "硅橡胶"],
    "东岳硅材": ["有机硅单体", "硅橡胶"],
    "兴发集团": ["有机硅单体"],
    "新安股份": ["有机硅单体", "硅橡胶"],
    "三友化工": ["有机硅单体"],
    "鲁西化工": ["有机硅单体"],
    "恒星科技": ["有机硅单体"],
    # 硅橡胶（单体已含的不再重复列）
    "硅宝科技": ["硅橡胶"],
    "回天新材": ["硅橡胶"],
    # ST宏达 —— 剔除（ST）
    # 功能性硅烷
    "江瀚新材": ["功能性硅烷"],
    "晨光新材": ["功能性硅烷"],
    "新亚强":   ["功能性硅烷"],
    "宏柏新材": ["功能性硅烷"],
    # 其他
    "润禾材料": ["其他"],
    "远翔新材": ["其他"],
}
ALIASES = {  # 图中名 → watchlist 名（按需补充）
}


def norm(s: str) -> str:
    return (s or "").replace("Ａ", "A").replace("Ｂ", "B").replace("　", "").strip()


def main():
    wl = json.load(open(PATH, encoding="utf-8"))
    name2code = {}
    for code, v in wl.items():
        name2code.setdefault(norm(v.get("name", "")), code)

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched, unmatched, appended, newly = [], [], [], []
    for name, concepts in CONCEPT_MAP.items():
        key = norm(ALIASES.get(name, name))
        code = name2code.get(key)
        if not code:
            unmatched.append(name)
            continue
        rec = wl[code]
        had = bool(rec.get("jiuyan_category"))
        # category
        cats = [c for c in str(rec.get("jiuyan_category", "")).split("|") if c]
        if "有机硅" not in cats:
            cats.append("有机硅")
        rec["jiuyan_category"] = "|".join(cats)
        # concept
        cur = [c for c in str(rec.get("jiuyan_concept", "")).split("|") if c]
        for c in concepts:
            if c not in cur:
                cur.append(c)
        rec["jiuyan_concept"] = "|".join(cur)
        rec["updated_at"] = ts
        matched.append((code, rec["name"], rec["jiuyan_concept"]))
        (appended if had else newly).append(rec["name"])

    shutil.copy(PATH, PATH + ".bak_20261010_silicone")
    json.dump(wl, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"总股票数: {len(wl)}")
    print(f"图中公司(去ST): {len(CONCEPT_MAP)}，匹配 {len(matched)}，未匹配 {len(unmatched)}")
    print(f"其中新打标签 {len(newly)}，已有标签追加 {len(appended)}")
    print("\n== 匹配明细 ==")
    for code, name, concept in matched:
        print(f"  {code} {name} -> 有机硅 | {concept}")
    if unmatched:
        print("\n== 未匹配 ==")
        for n in unmatched:
            print(" ", n)
    if appended:
        print("\n== 跨产业链追加（原有标签保留）==")
        for n in appended:
            print(" ", n)


if __name__ == "__main__":
    main()
