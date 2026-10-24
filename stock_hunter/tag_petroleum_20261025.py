# -*- coding: utf-8 -*-
"""新增「石油」一级分类（owner 2026-10-25，按韭研《石油》产业链图）。

图里的 10 个细分 → 落到 8 个概念：
    原油开采 / 成品油销售 / 勘探设备 / 钻采设备 / 油气设备 / 钻井服务 / 油田工程 / 技术服务
（图中「开采」「技术服务」「北交所」并入对应概念；ST 两只不在韭研池内，跳过）

只增不改：已有标签保留，新对追加到下一个空槽；同时写判词（核心/相关）与理由。

用法：``python stock_hunter/tag_petroleum_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_petro"
MARK = "2026-10-25 19:00:00"

# code -> (概念, 判词, 理由)
PETRO = {
    # 原油开采 / 炼化
    "601857": ("原油开采", "核心", "中国石油：油气勘探开发+炼化销售全链条"),
    "600938": ("原油开采", "核心", "中国海油：海上油气勘探开发龙头"),
    "603619": ("原油开采", "核心", "中曼石油：钻井工程+自有油气区块"),
    "600777": ("原油开采", "核心", "新潮能源：美国二叠纪油田资产"),
    "600028": ("成品油销售", "核心", "中国石化：炼化一体化+加油站零售网络"),
    # 成品油销售
    "603353": ("成品油销售", "核心", "和顺石油：加油站零售"),
    "000554": ("成品油销售", "核心", "泰山石油：加油站零售"),
    "000096": ("成品油销售", "相关", "广聚能源：成品油+燃气综合能源"),
    "000159": ("成品油销售", "相关", "国际实业：油品及化工品批发贸易"),
    # 勘探设备
    "300157": ("勘探设备", "相关", "新锦动力：油气勘探装备与技术服务"),
    # 钻采设备
    "000852": ("钻采设备", "核心", "石化机械：钻采装备与油气钢管"),
    "002490": ("钻采设备", "核心", "山东墨龙：油井管与专用设备"),
    "002278": ("钻采设备", "核心", "神开股份：井口装置/录井仪"),
    "301158": ("钻采设备", "核心", "德石股份：钻具与井下工具"),
    "603036": ("钻采设备", "核心", "如通股份：钻采井口工具"),
    # 油气设备
    "002353": ("油气设备", "核心", "杰瑞股份：油服装备龙头（压裂/钻完井）"),
    "002478": ("油气设备", "相关", "常宝股份：油井管为一块业务"),
    "688377": ("油气设备", "核心", "迪威尔：油气专用件（井口/采油树）"),
    "301063": ("油气设备", "相关", "海锅股份：锻件，油气为一块"),
    "300084": ("油气设备", "核心", "海默科技：多相流量计与油田服务"),
    "300370": ("油气设备", "相关", "安控科技：油气自动化控制"),
    "601798": ("油气设备", "相关", "蓝科高新：油气水分离器"),
    "603325": ("油气设备", "相关", "博隆技术：气力输送，石化为主应用"),
    # 钻井服务
    "600871": ("钻井服务", "核心", "石化油服：钻井/物探工程服务"),
    "601808": ("钻井服务", "核心", "中海油服：海上钻井与油田技术服务龙头"),
    "300164": ("钻井服务", "核心", "通源石油：射孔与完井服务"),
    "002828": ("钻井服务", "核心", "贝肯能源：钻井工程服务"),
    # 勘探（技术服务）
    "300191": ("勘探设备", "相关", "潜能恒信：地震数据处理解释+区块权益"),
    # 油田工程
    "600339": ("油田工程", "核心", "中油工程：油气田地面工程总包"),
    "600583": ("油田工程", "核心", "海油工程：海洋油气工程总包"),
    "603727": ("油田工程", "核心", "博迈科：海洋油气模块建造"),
    "002554": ("油田工程", "核心", "惠博普：油气工程与装备"),
    "605167": ("油田工程", "相关", "利柏特：石化大型模块制造"),
    # 技术服务 / 北交所
    "600968": ("技术服务", "核心", "海油发展：能源技术服务（运维/增产）"),
    "002629": ("技术服务", "相关", "仁智股份：油田技术服务，规模较小"),
    "920088": ("技术服务", "核心", "科力股份：油田生产技术服务（北交所）"),
    "920943": ("钻采设备", "核心", "优机股份：油气流体控制设备（北交所）"),
}

DESC = {
    "石油": "石油产业链：上游勘探开采 → 油服工程 → 装备制造 → 炼化销售，赚钱看油价与资本开支",
    "原油开采": "拥有油气储量与产量，赚油价×产量（中国石油/中国海油/中曼/新潮）",
    "成品油销售": "加油站零售与油品批发，赚批零价差",
    "勘探设备": "地震勘探、测井与解释软件，油气发现的上游工具",
    "钻采设备": "钻机、钻具、井口装置、油井管等钻井采油硬件",
    "油气设备": "压裂、采油树、多相计量、分离器等油气专用设备",
    "钻井服务": "钻井、射孔、完井等工程服务，按进尺/项目收费",
    "油田工程": "油气田地面/海洋工程总包与模块建造",
    "技术服务": "油田运维、增产、检测等技术服务",
}


def pairs(rec):
    out = []
    for i in range(10):
        k = "jiuyan_category" if i == 0 else f"jiuyan_category{i}"
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        cat = rec.get(k)
        if isinstance(cat, str) and cat.strip():
            out.append((cat.strip(), str(rec.get(ck) or "").strip()))
    return out


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    log, miss = [], []
    for i, (code, (con, verdict, reason)) in enumerate(PETRO.items(), 1):
        rec = data.get(code)
        if not isinstance(rec, dict):
            miss.append(code); continue
        cur = pairs(rec)
        if any(c == "石油" for c, _ in cur):
            continue
        # 追加到下一个空槽（1..9），老字段存在时先镜像下沉
        used = set()
        for k in list(rec):
            if k.startswith("jiuyan_category") and k[len("jiuyan_category"):].isdigit():
                if str(rec[k] or "").strip():
                    used.add(int(k[len("jiuyan_category"):]))
        legacy = str(rec.get("jiuyan_category") or "").strip()
        if legacy and not used:
            rec["jiuyan_category1"] = legacy
            rec["jiuyan_concept1"] = rec.get("jiuyan_concept") or ""
            used.add(1)
            rec.pop("jiuyan_category", None)
            rec.pop("jiuyan_concept", None)
        slot = next((s for s in range(1, 10) if s not in used), None)
        if slot is None:
            miss.append((code, "槽位已满")); continue
        rec[f"jiuyan_category{slot}"] = "石油"
        rec[f"jiuyan_concept{slot}"] = con
        # 判词：取最高档（原有核心不被降级）
        RANK = {"核心": 3, "相关": 2, "边缘": 1, "错标": 0, "已摘牌": 1}
        old = str(rec.get("jiuyan_tag_verdict") or "").strip()
        if not old or RANK.get(verdict, 0) >= RANK.get(old, 0):
            rec["jiuyan_tag_verdict"] = verdict
        d = rec.get("jiuyan_concept_reason")
        if not isinstance(d, dict):
            d = {}
        d.setdefault(con, reason)
        rec["jiuyan_concept_reason"] = d
        # 概念解释（写第一只成员即可，全表合并）
        if con not in DESC or con == "石油":
            pass
        desc = rec.get("jiuyan_concept_desc")
        if not isinstance(desc, dict):
            desc = {}
        for k in ("石油", con):
            if k in DESC and not str(desc.get(k) or "").strip():
                desc[k] = DESC[k]
        rec["jiuyan_concept_desc"] = desc
        rec["updated_at"] = MARK
        log.append((code, rec.get("name"), con, verdict))

    print(f"新增 石油 标签 {len(log)} 只（未处理 {miss}）")
    print("按概念：", dict(Counter(c for _, _, c, _ in log)))
    print("按判词：", dict(Counter(v for _, _, _, v in log)))
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
