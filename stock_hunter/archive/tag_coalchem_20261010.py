# -*- coding: utf-8 -*-
"""煤化工概念标签追加（2026-10-10，同花顺煤化工概念板块截图 6 张）。

口径：
- 扁平板块无二级细分 → jiuyan_category = "煤化工"，jiuyan_concept = "煤化工"
- ST/*ST 剔除（owner 2026-09-01 拍板）：ST百利、*ST柳化、*ST瑞茂、ST海钦 不录
- 按代码匹配为主、名称校验为辅；已有标签按 "|" 追加去重
- ⚠️ 截图缺第 81~94 行（16 只），本次仅覆盖 94 只
"""
import json, sys, shutil, datetime

sys.stdout.reconfigure(encoding="utf-8")
PATH = "stock_hunter/watchlist_jiuyan.json"

# (代码, 图中名称)
STOCKS = [
    ("600408","安泰集团"),("600256","广汇能源"),("600746","江苏索普"),("603395","红四方"),
    ("600722","金牛化工"),("000409","云鼎科技"),("000761","本钢板材"),("000723","美锦能源"),
    ("600188","兖矿能源"),("000552","甘肃能化"),("601011","宝泰隆"),("002556","辉隆股份"),
    ("600160","巨化股份"),("000912","泸天化"),("600470","六国化工"),("300080","易成新能"),
    ("601898","中煤能源"),("002092","中泰化学"),("000937","冀中能源"),("600227","赤天化"),
    ("601088","中国神华"),("920832","齐鲁华信"),("300384","三联虹普"),("000983","山西焦煤"),
    ("920407","驰诚股份"),("603113","金能科技"),("601216","君正集团"),("601699","潞安环能"),
    ("600821","金开新能"),("600426","华鲁恒升"),("600096","云天化"),("600075","新疆天业"),
    ("002597","金禾实业"),("600691","潞化科技"),("601117","中国化学"),("002573","清新环境"),
    ("601666","平煤股份"),("300055","万邦达"),("600844","金煤科技"),
    ("600123","兰花科创"),("300470","中密控股"),("600984","建设机械"),("000565","渝三峡A"),
    ("600740","山西焦化"),("000830","鲁西化工"),("000990","诚志股份"),("002783","凯龙股份"),
    ("920126","永大股份"),("002430","杭氧股份"),("600997","开滦股份"),
    ("002395","双象股份"),("000777","中核科技"),("600248","陕建股份"),("600157","永泰能源"),
    ("002274","华昌化工"),("603169","兰石重装"),("002469","三维化学"),("600985","淮北矿业"),
    ("600309","万华化学"),("601568","北元化工"),("000898","鞍钢股份"),("601918","新集能源"),
    ("000707","双环科技"),("600579","中化装备"),("600008","首创环保"),("000422","湖北宜化"),
    ("600623","华谊集团"),("600346","恒力石化"),("601015","陕西黑猫"),("002140","东华科技"),
    ("002442","龙星科技"),("001217","华尔泰"),("601101","昊华能源"),("000703","恒逸石化"),
    ("600803","新奥股份"),("600725","云维股份"),("600282","南钢股份"),
    ("300786","国林科技"),("605090","九丰能源"),("002911","佛燃能源"),("600378","昊华科技"),
    ("002564","天沃科技"),("300950","德固特"),("600028","中国石化"),
    ("000301","东方盛虹"),("002202","金风科技"),("002493","荣盛石化"),("002584","西陇科学"),
    ("603698","航天工程"),("600792","云煤能源"),("600343","航天动力"),("601091","沈鼓集团"),
]
EXCLUDED_ST = [("603959","ST百利"),("600423","*ST柳化"),("600180","*ST瑞茂"),("600753","ST海钦")]


def norm(s: str) -> str:
    return (s or "").replace("Ａ", "A").replace("Ｂ", "B").replace("　", "").strip()


def main():
    wl = json.load(open(PATH, encoding="utf-8"))
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched, unmatched, name_mismatch, appended, newly = [], [], [], [], []
    for code, name in STOCKS:
        rec = wl.get(code)
        if rec is None:
            unmatched.append((code, name))
            continue
        if norm(rec.get("name", "")) != norm(name):
            name_mismatch.append((code, name, rec.get("name")))
        had = bool(rec.get("jiuyan_category"))
        cats = [c for c in str(rec.get("jiuyan_category", "")).split("|") if c]
        if "煤化工" not in cats:
            cats.append("煤化工")
        rec["jiuyan_category"] = "|".join(cats)
        cur = [c for c in str(rec.get("jiuyan_concept", "")).split("|") if c]
        if "煤化工" not in cur:
            cur.append("煤化工")
        rec["jiuyan_concept"] = "|".join(cur)
        rec["updated_at"] = ts
        matched.append((code, rec["name"]))
        (appended if had else newly).append(rec["name"])

    shutil.copy(PATH, PATH + ".bak_20261010_coalchem")
    json.dump(wl, open(PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    print(f"截图覆盖 96 行（110 缺第81~94行），ST剔除 {len(EXCLUDED_ST)} 只 → 应打标 {len(STOCKS)} 只")
    print(f"匹配 {len(matched)}，未匹配 {len(unmatched)}，名称不一致 {len(name_mismatch)}")
    print(f"其中新打标签 {len(newly)}，已有标签追加 {len(appended)}")
    if unmatched:
        print("\n== 未匹配（不在 watchlist）==")
        for c, n in unmatched:
            print(f"  {c} {n}")
    if name_mismatch:
        print("\n== 名称不一致（按代码已打标，请核对）==")
        for c, img_n, wl_n in name_mismatch:
            print(f"  {c} 图中={img_n} watchlist={wl_n}")
    if appended:
        print("\n== 跨产业链追加（原标签保留）==")
        print(" ", "、".join(appended))


if __name__ == "__main__":
    main()
