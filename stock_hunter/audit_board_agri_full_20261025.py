# -*- coding: utf-8 -*-
"""大农业板块 **全量** 逐股判词（127 只）。

规则：多概念取最高档；「供销社/农机/农业机器人/棕榈油/煤化工」默认相关，其余默认核心，
逐只例外见 OVERRIDE（每条都写了理由）。

用法：``python stock_hunter/audit_board_agri_full_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_agri"
MARK = "2026-10-25 04:00:00"

CORE = {
    "AI农业", "农药", "北交所", "尿素", "林业", "果蔬", "棉花", "渔业", "牛", "猪",
    "种业-水稻", "种业-玉米", "粮食", "糖", "菌类", "鸡鸭",
}
RELATED = {"供销社", "农机", "农业机器人", "棕榈油", "煤化工", "多元金融", "正极材料"}
RANK = {"核心": 3, "相关": 2, "边缘": 1}

OVERRIDE = {
    # 供销社体系里真正做农资的
    "603970": ("核心", "中农立华：农药流通与植保服务（供销社体系）"),
    "003042": ("核心", "中农联合：农药原药/制剂（供销社体系）"),
    "002999": ("核心", "天禾股份：农资销售与农业社会化服务（供销社体系）"),
    "002758": ("核心", "浙农股份：现代农业综合服务商（供销社体系）"),
    "002556": ("核心", "辉隆股份：农资流通（供销社体系）"),
    "000564": ("相关", "供销大集：城乡商品流通，与农业生产端关联弱"),
    "600217": ("相关", "中再资环：废电回收（供销社体系），非农业"),
    "603029": ("相关", "天鹅股份：棉花加工机械，供销社体系"),
    "600318": ("边缘", "新力金融：融资租赁，与农业无关"),
    # 农机
    "000157": ("相关", "中联重科：工程机械为主，农机为一块"),
    "300387": ("相关", "富邦科技：农化助剂与农业解决方案"),
    "603767": ("边缘", "中马传动：汽车变速器，与农机无关"),
    "300022": ("相关", "吉峰科技：农机流通（另有低空业务）"),
    "001266": ("相关", "宏英智能：智能控制/农机电子"),
    "000816": ("核心", "智慧农业：农机与动力终端"),
    # 林业
    "600189": ("边缘", "泉阳泉：矿泉水主业，林业是历史业务"),
    "603359": ("相关", "东珠生态：生态修复，涉林业生态"),
    "000592": ("相关", "平潭发展：造林营林+贸易"),
    # 渔业
    "600257": ("边缘", "大湖股份：健康医疗/健康产品为主"),
    # 菌类
    "300972": ("边缘", "万辰集团：现主业量贩零食，食用菌是早期业务"),
    # 棕榈油
    "000626": ("边缘", "远大控股：大宗贸易为主，棕榈油只是一条线"),
    "300175": ("相关", "朗源股份：农副产品加工"),
    # 煤化工/尿素（化肥为一块）
    "601898": ("边缘", "中煤能源：煤炭为主，化肥占比小"),
    "600123": ("相关", "兰花科创：煤炭+化肥"),
    "002274": ("相关", "华昌化工：煤化工/化肥"),
    "600426": ("相关", "华鲁恒升：煤化工，化肥为一块"),
    "000912": ("核心", "泸天化：尿素/化肥主业"),
    "000422": ("相关", "湖北宜化：化肥+化工+煤炭"),
    "600691": ("边缘", "潞化科技：化工装备为主"),
    "603395": ("核心", "红四方：复合肥/氮肥"),
    "600096": ("核心", "云天化：磷肥/化肥龙头"),
    # 牛
    "300313": ("相关", "天山生物：牛冻精与育种"),
    "300485": ("边缘", "赛升药业：注射针剂，与牛养殖无关"),
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
            if "大农业" not in str(rec.get(k) or ""):
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
