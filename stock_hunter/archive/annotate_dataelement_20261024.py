# -*- coding: utf-8 -*-
"""给「数据要素」各二级概念写一句解释（owner 2026-10-24：猎手分组头红框处要显示板块解释）。

沿用 2026-10-19 医药板块的机制：解释写在 watchlist 的 `jiuyan_concept_desc`（{概念: 解释}），
t_gui `_jiuyan_all_concept_descs()` 合并成全表字典 → 前端 `gkDesc()` 直接渲染在分组头右侧
（过长省略、悬停看全文）。**只增不覆盖**：已有该概念的注释一律保留。

用法：``python stock_hunter/annotate_dataelement_20261024.py [--apply]``（默认 dry-run）
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261024_dedesc"

DESC = {
    "数据要素_数据存储":
        "把数据存下来、调得动的那一层：分布式数据库、存储阵列、数据湖。一句话：卖数据库/存储的，数据先要有地方放；收入看政企信息化预算。",
    "数据要素_交易所":
        "参股或运营地方/行业数据交易所（上海、深圳、贵阳等），是数据挂牌、撮合、结算的通道方。一句话：靠「数据买卖抽佣」的故事，弹性看交易额是否真起来。",
    "数据要素_数据确权":
        "数据产权登记、确权与合规流通——版权登记、数字水印、人民数据体系。一句话：给数据「发证」的，确权是交易的前置条件。",
    "数据要素_数据清洗":
        "数据加工、清洗、标准化，把原始数据整理成可交易、可训练的成品。一句话：数据流水线上的加工厂，按项目/按量收费。",
    "数据要素_数据分析":
        "BI 与行业数据分析平台，把数据变成决策与风控产品。一句话：卖分析工具和报告的，客户越依赖数据越稳。",
    "数据要素_数据标注":
        "数据标注与训练数据加工，为 AI 模型提供高质量样本。一句话：AI 的上游「人工流水线」，弹性看大模型训练订单。",
    "数据要素_数据安全":
        "数据安全治理、加密脱敏与隐私计算，是数据流通的前置合规环节。一句话：数据要能交易先得合规，卖的是「敢不敢放开」的保险。",
    "数据要素_内容数据":
        "手握正版内容库（小说、图书、教材、影视）的出版/阅读公司，内容可作大模型训练语料对外授权。一句话：有版权库的出版公司。",
    "数据要素_交通数据":
        "高速、城市交通与车路协同的数据运营，典型是交通信息化国企。一句话：管着路和车流数据的，政府项目制。",
    "数据要素_医疗数据":
        "医保、医院与公共健康数据的采集与运营，医疗信息化落地。一句话：管医保和医院数据的公司。",
    "数据要素_汽车数据":
        "车联网与智能驾驶数据，车企/供应链的数据资产运营。一句话：车跑出来的数据归谁、怎么变现。",
    "数据要素_司法数据":
        "法院与司法信息化数据服务，政法条线的数据平台。一句话：政法系统的信息化承包商。",
    "数据要素_营销数据":
        "营销与广告投放数据，用户画像、投放归因类服务。一句话：靠数据帮人投广告的。",
    "数据要素_学术数据":
        "期刊、论文与学术数据库，科研数据资产。一句话：卖论文库的，AIGC 时代成了语料供给方。",
    "数据要素_图片数据":
        "图片版权与视觉内容数据，AIGC 训练语料的一类。一句话：图库版权方，卖图也卖训练授权。",
    "数据要素_酒店数据":
        "酒店与商旅数据，酒旅行业的运营数据平台。一句话：酒旅行业的数据中台。",
    "数据要素_航运数据":
        "航运、港口与物流数据，大宗运输的数据运营。一句话：港口/航运信息化，数据跟着货走。",
    "数据要素_大宗数据":
        "大宗商品价格与交易数据，商品资讯/交易平台。一句话：卖行情和撮合的，数据即定价权。",
    "数据要素_网络可视化监管":
        "网络流量可视化与监管采集，网络安全与数据要素的交叉分支。一句话：给运营商/监管做流量分析的。",
    "数据要素_其他行业数据":
        "未单列行业的垂直数据运营（政务、能源、制造等）——兜底分支，个股差异大，按个股逻辑看。",
    "数据要素_北交所":
        "北交所上市的数据要素标的（仅作板块标记，不构成题材细分）。",
    "物理AI_数据采集":
        "真实世界数据采集（传感器、激光雷达、动作捕捉），为具身智能与物理仿真提供训练数据。一句话：给机器人「喂」现实数据的。",
}


def _cat_slots(rec):
    for i in range(0, 10):
        k = "jiuyan_category" if i == 0 else f"jiuyan_category{i}"
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        cat = rec.get(k)
        if isinstance(cat, str) and "数据要素" in cat:
            yield ck, cat, rec.get(ck)


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    touched, skipped = [], 0
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        need = {}
        for ck, cat, con in _cat_slots(rec):
            toks = con if isinstance(con, list) else str(con or "").split("|")
            for t in toks:
                t = str(t).strip()
                if t in DESC:
                    need[t] = DESC[t]
        if not need:
            continue
        d = rec.get("jiuyan_concept_desc")
        if not isinstance(d, dict):
            d = {}
        add = {k: v for k, v in need.items() if not str(d.get(k) or "").strip()}
        if not add:
            skipped += 1
            continue
        touched.append((code, rec.get("name"), list(add)))
        if apply:
            d.update(add)
            rec["jiuyan_concept_desc"] = d

    cover = {}
    for code, rec in data.items():
        if isinstance(rec, dict) and isinstance(rec.get("jiuyan_concept_desc"), dict):
            for k in rec["jiuyan_concept_desc"]:
                if k in DESC:
                    cover[k] = rec.get("name")
    print(f"待写 {len(touched)} 只（已有注释跳过 {skipped} 只）")
    for code, name, add in touched[:12]:
        print(f"  {code} {name}: +{','.join(add)}")
    if len(touched) > 12:
        print(f"  …… 其余 {len(touched) - 12} 只同理")
    print("\n概念覆盖：")
    for k in DESC:
        print(f"  {'✓' if k in cover else '×'} {k}" + (f"  （写作：{cover[k]}）" if k in cover else ""))

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
