# -*- coding: utf-8 -*-
"""医药板块（含中药/创新药，共 209 只）**全量** 逐股判词。

做法：逐只过一遍后，按「二级概念 → 默认判词」+**例外名单**落地——
医药这块概念名普遍就是业务名（中药品牌/化学制药/CDMO/体外诊断…），
默认核心，凡是「概念只是公司的一部分 / 只是支线 / 名不副实」的逐只列在 OVERRIDE 里。

用法：``python stock_hunter/audit_board_med_full_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_med"
MARK = "2026-10-25 01:00:00"

DEFAULT = {
    "中药": "核心", "中药品牌": "核心", "中药饮片": "核心", "百年老字号": "核心", "民族药": "核心",
    "化学制药": "核心", "创新药": "核心", "医疗器械": "核心", "医疗服务": "核心", "医药商业": "核心",
    "体外诊断": "核心", "基因测序": "核心", "生命科学上游": "核心", "生物医药": "核心",
    "模式动物": "核心", "临床CRO": "核心", "临床前CRO": "核心", "CDMO": "核心",
    "AI4S制药领域": "核心", "药用包材": "核心", "原料/试剂": "核心",
    "ADC": "核心", "多特异性抗体": "核心", "自身免疫": "核心", "mRNA技术平台": "核心",
    "已上市": "核心", "未上市": "核心",          # 概念是状态标签，业务仍是疫苗
    "AI医疗": "相关", "mRNA递送系统": "相关", "肿瘤疫苗": "相关", "种植": "相关",
}

OVERRIDE = {
    # 医疗服务里其实是百货
    "600327": ("边缘", "大东方：百货零售为主，医疗服务只是参股"),
    # 种植：真有中药材基地的算相关，没有的算边缘
    "000919": ("边缘", "金陵药业：药品制造为主，无实质种植业务"),
    "000623": ("相关", "吉林敖东：中药+人参种植"),
    "600129": ("相关", "太极集团：中成药，兼有中药材基地"),
    "600518": ("相关", "康美药业：中药饮片为主，兼有种植"),
    "300158": ("相关", "振东制药：化药/中药，有中药材种植基地"),
    "002864": ("相关", "盘龙药业：中药，有药材种植基地"),
    "600085": ("相关", "同仁堂：中成药为主，药材种植为配套"),
    "600422": ("相关", "昆药集团：中药为主，药材种植为配套"),
    "002644": ("相关", "佛慈制药：中成药，兼有中药材种植"),
    # CRO / CDMO
    "002399": ("相关", "海普瑞：肝素产业链为主，CDMO 为一块业务"),
    "300692": ("边缘", "中赋科技：工程建设/咨询为主，CRO 属性弱"),
    # AI 医疗
    "300253": ("相关", "卫宁健康：医院信息化（AI 医疗载体）"),
    "688393": ("相关", "安必平：病理诊断，AI 辅助为延伸"),
    "300078": ("相关", "思创智联：医疗物联网/商业智能"),
    "300288": ("相关", "朗玛信息：互联网医疗"),
    "603108": ("相关", "润达医疗：IVD 流通与实验室服务，AI 为延伸"),
    # mRNA
    "301207": ("相关", "华兰疫苗：流感疫苗为主，mRNA 为技术延伸"),
    "603707": ("边缘", "健友股份：肝素/注射剂，mRNA 递送缺业务"),
    "603538": ("边缘", "美诺华：原料药/制剂，mRNA 递送缺业务"),
    "688356": ("相关", "键凯科技：PEG 衍生物，mRNA 递送用辅料"),
    "300841": ("相关", "康华生物：疫苗为主，mRNA 为技术延伸"),
    # 化学制药里的非主业
    "002562": ("相关", "兄弟科技：维生素/皮革化工为主，医药为辅"),
    "603880": ("相关", "南卫股份：透皮产品/防护用品"),
    "300111": ("相关", "向日葵：医药+光伏双主业"),
    "600851": ("相关", "海欣股份：医药+物业出租"),
    "002868": ("相关", "绿康生化：动保（兽药）为主"),
    "000931": ("相关", "中关村：生物医药+养老+商砼"),
    # 器械 / 试剂
    "603976": ("相关", "正川股份：药用玻璃包材，医疗器械配套"),
    "300171": ("相关", "东富龙：制药装备，非试剂耗材"),
    # 肿瘤疫苗（多为抗肿瘤药或检测，不是疫苗）
    "603590": ("相关", "康辰药业：创新药（血凝酶等），非疫苗"),
    "688658": ("相关", "悦康药业：仿制药/创新药，非疫苗"),
    "688091": ("相关", "上海谊众：抗肿瘤药物（紫杉醇胶束）"),
    "688276": ("相关", "百克生物：疫苗（带状疱疹/水痘），肿瘤疫苗在研"),
    "300676": ("边缘", "华大基因：基因检测，与肿瘤疫苗无关"),
    "300109": ("边缘", "新开源：精细化工+医疗，肿瘤疫苗为参股"),
    "300006": ("边缘", "莱美药业：化学仿制药，非疫苗"),
}


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    log = []
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        cats, cons = [], []
        for i in list(range(1, 10)) + [0]:
            k = "jiuyan_category" if i == 0 else f"jiuyan_category{i}"
            ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
            c = str(rec.get(k) or "")
            if not any(x in c for x in ("医药", "中药", "创新药")):
                continue
            v = rec.get(ck)
            cons += [str(t).strip() for t in (v if isinstance(v, list) else str(v or "").split("|")) if str(t).strip()]
        if not cons:
            continue
        cons = list(dict.fromkeys(cons))
        if code in OVERRIDE:
            verdict, reason = OVERRIDE[code]
        else:
            verdict = next((DEFAULT[c] for c in cons if c in DEFAULT), "相关")
            reason = ""
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
    print(f"其中例外名单 {len(OVERRIDE)} 只")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
