# -*- coding: utf-8 -*-
"""大农业概念标签追加（2026-10-10，韭研公社 大农业(260828) 截图）。

口径：
- 一级分类 jiuyan_category = "大农业"
- 二级 jiuyan_concept = 细分名（种业拆 种业-水稻/种业-玉米），多细分 "|" 连接
- ST 剔除：watchlist 中名称含 ST 的匹配项跳过并报告（owner 2026-09-01 拍板）
- 按名称匹配（全角归一化）；已有标签按 "|" 追加去重
"""
import json, sys, shutil, datetime

sys.stdout.reconfigure(encoding="utf-8")
PATH = "stock_hunter/watchlist_jiuyan.json"

CONCEPT_MAP = {
    "隆平高科": ["种业-水稻", "种业-玉米"], "荃银高科": ["种业-水稻", "种业-玉米"],
    "苏垦农发": ["种业-水稻"], "农发种业": ["种业-水稻", "种业-玉米"],
    "神农种业": ["种业-水稻", "种业-玉米"], "登海种业": ["种业-玉米"],
    "万向德农": ["种业-玉米"], "敦煌种业": ["种业-玉米"], "国投丰乐": ["种业-玉米"],
    "金健米业": ["粮食"], "京粮控股": ["粮食"], "深粮控股": ["粮食"],
    "亚盛集团": ["粮食"], "北大荒": ["粮食"],
    "新赛股份": ["棉花"], "冠农股份": ["棉花", "果蔬", "糖"], "新农开发": ["棉花"],
    "宏辉果蔬": ["果蔬"], "朗源股份": ["果蔬"], "中粮糖业": ["果蔬", "糖"],
    "中基健康": ["果蔬"], "中粮科技": ["糖"],
    "赞宇科技": ["棕榈油"], "远大控股": ["棕榈油"],
    "岳阳林纸": ["林业"], "康欣新材": ["林业"], "永安林业": ["林业"],
    "福建金森": ["林业"], "平潭发展": ["林业"], "丰林集团": ["林业"],
    "东珠生态": ["林业"], "海南橡胶": ["林业"], "泉阳泉": ["林业"],
    "中水渔业": ["渔业"], "国联水产": ["渔业"], "佳沃食品": ["渔业"],
    "开创国际": ["渔业"], "百洋股份": ["渔业"], "大湖股份": ["渔业"],
    "东方海洋": ["渔业"], "好当家": ["渔业"], "獐子岛": ["渔业"],
    "牧原股份": ["猪"], "温氏股份": ["猪", "鸡鸭"], "正邦科技": ["猪"],
    "天邦食品": ["猪"], "巨星农牧": ["猪"], "天康生物": ["猪"],
    "神农集团": ["猪"], "京基智农": ["猪"], "立华股份": ["猪", "鸡鸭"],
    "傲农生物": ["猪"], "东瑞股份": ["猪"], "天域生物": ["猪"],
    "正虹科技": ["猪"], "华统股份": ["猪"], "禾丰股份": ["猪", "牛", "鸡鸭"],
    "海大集团": ["猪"], "新希望": ["猪"], "大北农": ["猪"], "唐人神": ["猪"],
    "金新农": ["猪"], "罗牛山": ["猪"], "新五丰": ["猪"],
    "福成股份": ["牛"], "天山生物": ["牛"], "庄园牧场": ["牛"],
    "西部牧业": ["牛"], "赛升药业": ["牛"], "光明肉业": ["牛"], "得利斯": ["牛"],
    "湘佳股份": ["鸡鸭"], "圣农发展": ["鸡鸭"], "仙坛股份": ["鸡鸭"],
    "益生股份": ["鸡鸭"], "民和股份": ["鸡鸭"], "晓鸣股份": ["鸡鸭"],
    "欧福蛋业": ["鸡鸭"], "华英农业": ["鸡鸭"],
    "雪榕生物": ["菌类"], "众兴菌业": ["菌类"], "华绿生物": ["菌类"], "万辰集团": ["菌类"],
    "中农立华": ["供销社"], "中农联合": ["供销社"], "中再资环": ["供销社"],
    "供销大集": ["供销社"], "天鹅股份": ["供销社"], "天禾股份": ["供销社"],
    "浙农股份": ["供销社"], "辉隆股份": ["供销社"], "新力金融": ["供销社"],
    "先达股份": ["农药"], "新农股份": ["农药"], "美邦股份": ["农药"],
    "润丰股份": ["农药"], "农心科技": ["农药"], "海利尔": ["农药"],
    "安道麦A": ["农药"], "诺普信": ["农药"],
    "四川美丰": ["尿素"], "潞化科技": ["尿素"], "湖北宜化": ["尿素"],
    "中煤能源": ["尿素"], "华昌化工": ["尿素"], "华鲁恒升": ["尿素"],
    "兰花科创": ["尿素"], "博源化工": ["尿素"], "泸天化": ["尿素"],
    "云天化": ["尿素"], "红四方": ["尿素"],
    "威马农机": ["农机"], "星光农机": ["农机"], "智慧农业": ["农机", "AI农业"],
    "中马传动": ["农机"], "宏英智能": ["农机"], "吉峰科技": ["农机"], "一拖股份": ["农机"],
    "富邦科技": ["农业机器人"], "宝馨科技": ["农业机器人"],
    "中联重科": ["农业机器人"], "永安行": ["农业机器人"],
    "托普云农": ["AI农业"],
    "秋乐种业": ["北交所"], "康农种业": ["北交所"], "润农节水": ["北交所"],
    "花溪科技": ["北交所"], "绿亨科技": ["北交所"], "田野股份": ["北交所"],
    "骑士乳业": ["北交所"],
}


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
        code = name2code.get(norm(name))
        if not code:
            unmatched.append(name)
            continue
        rec = wl[code]
        if "ST" in rec.get("name", "").upper():
            st_skipped.append((code, rec["name"]))
            continue
        had = bool(rec.get("jiuyan_category"))
        cats = [c for c in str(rec.get("jiuyan_category", "")).split("|") if c]
        if "大农业" not in cats:
            cats.append("大农业")
        rec["jiuyan_category"] = "|".join(cats)
        cur = [c for c in str(rec.get("jiuyan_concept", "")).split("|") if c]
        for c in concepts:
            if c not in cur:
                cur.append(c)
        rec["jiuyan_concept"] = "|".join(cur)
        rec["updated_at"] = ts
        matched.append((code, rec["name"]))
        (appended if had else newly).append(rec["name"])

    shutil.copy(PATH, PATH + ".bak_20261010_agri")
    json.dump(wl, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"图中公司(去重): {len(CONCEPT_MAP)}，匹配打标 {len(matched)}，未匹配 {len(unmatched)}，ST跳过 {len(st_skipped)}")
    print(f"其中新打标签 {len(newly)}，已有标签追加 {len(appended)}")
    if unmatched:
        print("\n== 未匹配（不在 watchlist）==")
        print(" ", "、".join(unmatched))
    if st_skipped:
        print("\n== ST 跳过（当前戴帽）==")
        for c, n in st_skipped:
            print(f"  {c} {n}")
    if appended:
        print("\n== 跨产业链追加（原标签保留）==")
        print(" ", "、".join(appended))


if __name__ == "__main__":
    main()
