# -*- coding: utf-8 -*-
"""军工板块 **全量** 逐股判词（100 只）。

规则：军工材料/电子/主机厂/导弹/雷达/海军/陆军/空军默认核心，例外逐只列在 OVERRIDE。

用法：``python stock_hunter/audit_board_mil_full_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_mil"
MARK = "2026-10-25 08:00:00"

CORE = {
    "军工材料", "军工材料-石英材料", "军工材料-碳纤维", "军工材料-金属3D打印",
    "军工材料-钛材", "军工材料-隐身材料", "军工电子", "军工电子-军工芯片", "军工电子-FPGA",
    "军工电子-军用通信", "军工电子-水声电子", "军工电子-海军电子", "军工电子-激光晶体",
    "军工电子-特种集成电路", "军工电子-电子对抗", "军工电子-军用MLCC", "军工电子-军用连接器",
    "军工电子-钽电容", "军工电子-T/R组件", "军工配套", "军用机器人", "空军", "海军", "陆军",
    "导弹", "导弹-弹药", "导弹-精确制导", "导弹-制导控制", "导弹-动力", "导弹-配套",
    "导弹-光学制导", "雷达", "无人机", "军贸", "航空装备-战斗机主机厂", "航空装备-直升机主机厂",
    "航空装备-机载系统", "航空装备-航空锻件", "航空装备-环形锻件", "航空装备-航发控制系统",
    "航空发动机", "航天-航天发动机", "航天-航天配套", "航天-航天科工装备", "航天-特种装备",
    "红外探测", "核化生防御", "卫星", "卫星-有效载荷", "卫星-北斗导航", "卫星-卫星应用",
    "卫星-卫星导航", "惯性导航", "军工AI", "陆军-装备配套", "海军-舰船电力推进", "海军-电磁发射",
    "航空地面装备",
}
RELATED = {"工业无人机", "工业机器人", "低空经济"}
RANK = {"核心": 3, "相关": 2, "边缘": 1}

OVERRIDE = {
    "002342": ("边缘", "巨力索具：索具为主，军工材料只是客户场景"),
    "600992": ("相关", "贵绳股份：钢丝绳，可作军工配套但非军工专供"),
    "002383": ("相关", "合众思壮：导航定位，军民两用"),
    "603488": ("错标", "展鹏科技：电梯门系统，与军工电子无关"),
    "600353": ("相关", "旭光电子：电真空器件，军工为一块"),
    "000576": ("相关", "甘化科工：电源/合金材料+军工光电"),
    "002190": ("相关", "成飞集成：工装模具/汽车零部件，航空配套为延伸"),
    "002535": ("边缘", "林州重机：煤矿机械为主"),
    "002724": ("相关", "海洋王：专业照明，军工为应用场景"),
    "600698": ("相关", "湖南天雁：兵装集团涡轮增压器，军品配套"),
    "002747": ("相关", "埃斯顿：工业机器人本体，军工为应用场景"),
    "300024": ("相关", "机器人（新松）：工业机器人，军用为一块"),
    "688070": ("相关", "纵横股份：工业无人机，非军用型号"),
    "002111": ("相关", "威海广泰：空港装备+无人机"),
    "600118": ("核心", "中国卫星：卫星总体制造"),
    "002151": ("相关", "北斗星通：北斗芯片与数据服务"),
    "300627": ("相关", "华测导航：高精度导航（民用为主）"),
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
            if "军工" not in str(rec.get(k) or ""):
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
