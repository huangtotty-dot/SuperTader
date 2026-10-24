# -*- coding: utf-8 -*-
"""大消费板块 **全量** 逐股判词（143 只）。

规则：多概念取最高档（核心 > 相关 > 边缘）；「免税概念」默认相关（机场/商贸的免税是租金或牌照），
中国中免单独判核心；北交所是板块标记，按各自主业判；其余逐条例外见 OVERRIDE。

用法：``python stock_hunter/audit_board_cons_full_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_cons"
MARK = "2026-10-25 02:00:00"

CORE = {
    "大消费_休闲食品", "大消费_卤制品", "大消费_奶酪", "大消费_乳酸菌", "大消费_常温奶",
    "大消费_鲜奶", "大消费_功能饮料", "大消费_果汁", "大消费_植物蛋白", "大消费_炼乳",
    "大消费_烘焙食品", "大消费_调味品", "大消费_预制菜", "大消费_餐饮老字号", "大消费_酒店",
    "大消费_酒类", "大消费_服装", "大消费_日化", "大消费_文旅旅游", "大消费_百货零售",
    "大消费_小家电", "大消费_冰雪经济",
}
RELATED = {"大消费_免税概念"}
RANK = {"核心": 3, "相关": 2, "边缘": 1}

OVERRIDE = {
    "601888": ("核心", "中国中免：免税零售龙头"),
    "600009": ("相关", "上海机场：机场免税靠租金与分成，主业是航空服务"),
    "600004": ("相关", "白云机场：免税是航站楼租金收益"),
    "600515": ("相关", "海南机场：机场管理，免税是口岸流量变现"),
    "002707": ("相关", "众信旅游：旅游服务为主，免税为牌照申请"),
    "600778": ("相关", "友好集团：百货为主，免税为申请中的牌照"),
    "002320": ("边缘", "海峡股份：船舶运输，免税只是客流关联"),
    "603069": ("边缘", "海汽集团：汽车客运，免税依据弱"),
    "002163": ("边缘", "海南发展：幕墙与内装工程，与免税无关"),
    "000886": ("边缘", "海南高速：交通建材，海南免税只是区域概念"),
    "600881": ("边缘", "亚泰集团：建材为主，冰雪/文旅为区域资产"),
    "000530": ("相关", "冰山冷热：工业制冷，冰雪场馆为下游场景"),
    "301010": ("相关", "晶雪节能：冷库保温材料，冰雪场馆配套"),
    "920699": ("边缘", "海达尔：精密滑轨，非消费品牌"),
    "920765": ("边缘", "美之高：置物架收纳（「北交所」只是板块标记）"),
    "920262": ("核心", "太湖雪：丝绸家纺消费"),
    "920826": ("核心", "盖世食品：标准化凉菜（预制菜）"),
    "920926": ("核心", "鸿智科技：厨房小家电"),
    "002264": ("边缘", "新华都：互联网营销为主，百货已成历史"),
    "600774": ("相关", "汉商集团：医药业务为主，商业为辅"),
    "600250": ("相关", "南京商旅：旅游+商贸"),
    "000419": ("相关", "通程控股：零售+金融+酒店"),
    "600865": ("相关", "百大集团：零售+物业租赁"),
    "001338": ("相关", "永顺泰：麦芽（啤酒上游），非成品酒"),
    "300175": ("相关", "朗源股份：农副产品加工"),
    "600693": ("核心", "东百集团：商业零售"),
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
            if "大消费" not in str(rec.get(k) or ""):
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
