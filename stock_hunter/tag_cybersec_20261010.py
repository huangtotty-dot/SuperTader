# -*- coding: utf-8 -*-
"""网络安全概念标签追加（2026-10-10，韭研公社 网络安全(260828) 截图，分 4 段全分辨率读取）。

口径：
- 一级分类 jiuyan_category = "网络安全"
- 二级 jiuyan_concept：国家队 / 其他 / 抗量子密码 / AI监管-内容审核 / AI监管-内容标识 /
  AI监管-AI鉴伪 / AI监管-数字水印 / AI监管-确权；多细分 "|" 连接
- ST 剔除：ST汇洲（图中已戴帽）不录；watchlist 中名称含 ST 的匹配项跳过并报告
- 按名称匹配（全角归一化 + 别名表）；已有标签按 "|" 追加去重
"""
import json, sys, shutil, datetime

sys.stdout.reconfigure(encoding="utf-8")
PATH = "stock_hunter/watchlist_jiuyan.json"

CONCEPT_MAP = {
    # 国家队
    "奇安信": ["国家队"], "启明星辰": ["国家队"], "电科网安": ["国家队", "AI监管-内容标识"],
    "绿盟科技": ["国家队", "AI监管-AI鉴伪"], "国投智能": ["国家队", "AI监管-内容审核"],
    "数字认证": ["国家队"], "深信服": ["国家队"], "安恒信息": ["国家队"],
    "亚信安全": ["国家队"], "永信至诚": ["国家队"], "盛邦安全": ["国家队"], "北信源": ["国家队"],
    # 其他
    "迪普科技": ["其他"], "三六零": ["其他"], "中孚信息": ["其他"], "国华网安": ["其他"],
    "南凌科技": ["其他"], "山石网科": ["其他"], "佳缘科技": ["其他"], "中安科": ["其他"],
    "麒麟信安": ["其他"], "久其软件": ["其他"], "天融信": ["其他"],
    # 抗量子密码
    "三未信安": ["抗量子密码"], "格尔软件": ["抗量子密码"], "吉大正元": ["抗量子密码"],
    # AI监管-内容审核（ST汇洲剔除）
    "人民网": ["AI监管-内容审核"], "国安股份": ["AI监管-内容审核"],
    "华数传媒": ["AI监管-内容审核"], "新华网": ["AI监管-内容审核"],
    "博汇科技": ["AI监管-内容审核"], "拓尔思": ["AI监管-内容审核"],
    "新媒股份": ["AI监管-内容审核"], "苏州高新": ["AI监管-内容审核"],
    "科大讯飞": ["AI监管-内容审核"],
    # AI监管-内容标识
    "汉王科技": ["AI监管-内容标识"], "佳都科技": ["AI监管-内容标识"],
    "视觉中国": ["AI监管-内容标识"],
    # AI监管-AI鉴伪
    "浩瀚深度": ["AI监管-AI鉴伪"], "数码视讯": ["AI监管-AI鉴伪"], "古鳌科技": ["AI监管-AI鉴伪"],
    # AI监管-数字水印
    "汉邦高科": ["AI监管-数字水印"], "恒信东方": ["AI监管-数字水印"],
    # AI监管-确权
    "安妮股份": ["AI监管-确权"],
}
ALIASES = {  # 图中名 → watchlist 名
    "国安股份": "中信国安",
}
EXCLUDED_ST_IMG = ["ST汇洲"]  # 图中已戴帽，直接不录


def norm(s: str) -> str:
    return (s or "").replace("Ａ", "A").replace("Ｂ", "B").replace("　", "").strip()


def main():
    wl = json.load(open(PATH, encoding="utf-8"))
    name2code = {}
    for code, v in wl.items():
        name2code.setdefault(norm(v.get("name", "")), code)

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched, unmatched, st_skipped, appended, newly = [], [], [], [], []
    for name, concepts in CONCEPT_MAP.items():
        key = norm(name)
        code = name2code.get(key)
        if not code and name in ALIASES:
            code = name2code.get(norm(ALIASES[name]))
        if not code:
            unmatched.append(name)
            continue
        rec = wl[code]
        if "ST" in rec.get("name", "").upper():
            st_skipped.append((code, rec["name"]))
            continue
        had = bool(rec.get("jiuyan_category"))
        cats = [c for c in str(rec.get("jiuyan_category", "")).split("|") if c]
        if "网络安全" not in cats:
            cats.append("网络安全")
        rec["jiuyan_category"] = "|".join(cats)
        cur = [c for c in str(rec.get("jiuyan_concept", "")).split("|") if c]
        for c in concepts:
            if c not in cur:
                cur.append(c)
        rec["jiuyan_concept"] = "|".join(cur)
        rec["updated_at"] = ts
        matched.append((code, rec["name"], "|".join(concepts)))
        (appended if had else newly).append(rec["name"])

    shutil.copy(PATH, PATH + ".bak_20261010_cybersec")
    json.dump(wl, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"图中公司(去重去ST): {len(CONCEPT_MAP)}，匹配打标 {len(matched)}，未匹配 {len(unmatched)}，ST跳过 {len(st_skipped)}")
    print(f"其中新打标签 {len(newly)}，已有标签追加 {len(appended)}")
    print("\n== 匹配明细 ==")
    for code, name, concept in matched:
        print(f"  {code} {name} -> {concept}")
    if unmatched:
        print("\n== 未匹配 ==", "、".join(unmatched))
    if st_skipped:
        print("\n== ST 跳过 ==")
        for c, n in st_skipped:
            print(f"  {c} {n}")
    if appended:
        print("\n== 跨产业链追加（原标签保留）==", "、".join(appended))


if __name__ == "__main__":
    main()
