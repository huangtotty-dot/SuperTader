# -*- coding: utf-8 -*-
"""商业航天概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "commercial_space_audit_20261027.json"
CURRENT = ROOT / "data_cleaning" / "commercial_space_current_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_commercial_space_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_commercial_space_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 12:23:00"

DESC = {
    "卫星总体与制造": "商业卫星整星总体设计、总装集成、测试与批量制造，不含仅提供一般零部件的企业。",
    "卫星载荷-通信与转发": "卫星通信转发器、通信载荷及直接任务设备，按实际产品层级标注。",
    "卫星载荷-遥感与探测": "商业遥感、气象、探测等卫星任务载荷及在轨智能处理设备。",
    "卫星载荷-导航增强": "卫星导航增强、星基导航任务载荷与相关专用设备。",
    "卫星平台与核心分系统": "卫星平台控制、推进、热控、供配电等核心分系统及单机。",
    "卫星结构与机构": "星体结构、太阳翼基板、展开机构及卫星专用结构机构产品。",
    "星载电子与核心元器件": "已实际用于商业卫星的星载计算、控制、存储、连接器及高可靠电子元器件。",
    "星载射频与微波芯片": "星载相控阵T/R芯片、射频微波芯片及直接组成通信载荷的射频组件。",
    "卫星电源与太阳翼": "空间电源、太阳电池阵、太阳翼及卫星专用电源控制产品。",
    "火箭总体与动力": "商业火箭总体、发动机及其关键动力零部件，按实际产品层级标注，不把配套企业写成总体厂商。",
    "火箭结构与紧固件": "箭体结构、钣金、连接件、锻件及已形成真实航天供货的专用紧固件。",
    "火箭推进与流体控制": "火箭发动机推进、特种阀门、管路、流体输送与控制产品。",
    "航天材料与增材制造": "面向商业火箭和卫星的专用材料、锻件及金属增材制造产品，须有真实客户或交付证据。",
    "航天电子与测控组件": "运载火箭和航天器用测控、数据链、惯性导航、机电组件及地面测控电子。",
    "地面站与测运控": "商业卫星地面站、测控天线、测运控系统、在轨管理与太空态势感知服务。",
    "卫星通信终端与天线": "卫星互联网用户终端、便携站、车载船载机载终端及专用天线。",
    "卫星互联网网络与系统": "卫星互联网网络系统、星地组网、核心网与系统级解决方案。",
    "卫星运营与通信服务": "通信广播卫星空间段运营、卫星通信带宽和传输服务。",
    "卫星数据与遥感应用": "商业遥感星座运营、卫星数据生产、处理、分发和行业应用服务。",
    "导航定位与授时服务": "基于北斗等卫星系统的商业导航定位、芯片模块、终端和授时服务。",
    "发射场与地面保障": "商业发射场、发射支持、燃料制备储运和地面保障系统。",
    "环境试验与检测服务": "面向商业航天器、火箭及元器件的环境可靠性、雷电静电和入网检测服务。",
    "商业航天股权与产业投资": "已实际持有商业航天企业股权或基金份额，须有可核验权益，不采用诉讼未决或潜在投资。",
    "商业航天配套-通用零部件": "已有真实商业航天订单或交付的通用零部件和加工配套，低占比业务原则上列相关。",
}


def toks(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def slot_names(i):
    return ("jiuyan_category", "jiuyan_concept") if i == 0 else (f"jiuyan_category{i}", f"jiuyan_concept{i}")


def slots(rec):
    for i in list(range(1, 10)) + [0]:
        ck, nk = slot_names(i)
        if str(rec.get(ck) or "").strip():
            yield i, ck, nk


def active_pairs(rec):
    out = []
    for _, ck, nk in slots(rec):
        for cat in toks(rec.get(ck)):
            for con in toks(rec.get(nk)):
                if (cat, con) not in out:
                    out.append((cat, con))
    return out


def remove_commercial_space(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "商业航天" not in cats:
            continue
        old.extend(("商业航天", con) for con in cons)
        keep = [cat for cat in cats if cat != "商业航天"]
        if keep:
            rec[ck] = keep[0] if len(keep) == 1 else keep
        else:
            rec.pop(ck, None)
            rec.pop(nk, None)
    return list(dict.fromkeys(old))


def add_or_merge(rec, category, concepts):
    for _, ck, nk in slots(rec):
        if toks(rec.get(ck)) == [category]:
            merged = list(dict.fromkeys(toks(rec.get(nk)) + concepts))
            rec[nk] = merged[0] if len(merged) == 1 else merged
            return
    value = concepts[0] if len(concepts) == 1 else concepts
    for i in list(range(1, 10)) + [0]:
        ck, nk = slot_names(i)
        if not str(rec.get(ck) or "").strip():
            rec[ck], rec[nk] = category, value
            return
    raise RuntimeError(f"标签槽位不足：{rec.get('name')} {category}/{concepts}")


def main():
    apply = "--apply" in sys.argv
    original = json.loads(SRC.read_text(encoding="utf-8"))
    data = copy.deepcopy(original)
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    current = json.loads(CURRENT.read_text(encoding="utf-8"))
    target_codes = {x["code"] for x in current}
    assert len(original) == 5400 and len(audit) == 90
    assert {x["code"] for x in audit} == target_codes
    used_concepts = {c for x in audit for p in x["pairs"] for c in p["concepts"]}
    assert used_concepts <= set(DESC), used_concepts - set(DESC)
    counts = Counter()

    for row in audit:
        rec = data[row["code"]]
        assert rec["name"] == row["name"]
        before_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "商业航天"]
        old_pairs = remove_commercial_space(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        after_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "商业航天" and (cat, con) not in new_pairs]
        assert before_other == after_other, (row["code"], before_other, after_other)
        current_concepts = {con for _, con in active_pairs(rec)}
        old_concepts = {con for _, con in old_pairs}

        reasons = rec.get("jiuyan_concept_reason") if isinstance(rec.get("jiuyan_concept_reason"), dict) else {}
        for key in list(reasons):
            if key in old_concepts and key not in current_concepts:
                reasons.pop(key, None)
        for pair in row["pairs"]:
            for concept in pair["concepts"]:
                reasons[concept] = row["reason"]
        if reasons:
            rec["jiuyan_concept_reason"] = reasons
        else:
            rec.pop("jiuyan_concept_reason", None)

        descs = rec.get("jiuyan_concept_desc") if isinstance(rec.get("jiuyan_concept_desc"), dict) else {}
        for key in list(descs):
            if key in old_concepts and key not in current_concepts:
                descs.pop(key, None)
        for _, concept in new_pairs:
            descs[concept] = DESC[concept]
        if descs:
            rec["jiuyan_concept_desc"] = descs
        else:
            rec.pop("jiuyan_concept_desc", None)

        commercial_pairs = [(cat, con) for cat, con in new_pairs if cat == "商业航天"]
        if commercial_pairs:
            rec["jiuyan_commercial_space_verdicts"] = {"商业航天": row["verdict"]}
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        else:
            rec.pop("jiuyan_commercial_space_verdicts", None)
            if not active_pairs(rec):
                rec.pop("jiuyan_tag_verdict", None)
                rec.pop("jiuyan_tag_evidence", None)
        rec["jiuyan_commercial_space_audited_at"] = DATE
        rec["jiuyan_commercial_space_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info + ifind_get_stock_business_segmentation", "profile_asof": DATE,
        }
        rec["jiuyan_commercial_space_audit_20261027"] = {
            "action": row["action"], "old_pairs": old_pairs, "new_pairs": new_pairs,
            "verdict": row["verdict"], "reason": row["reason"],
        }
        if row.get("borderline_review"):
            rec["jiuyan_commercial_space_audit_20261027"]["borderline_review"] = row["borderline_review"]
        rec["updated_at"] = MARK
        counts[row["action"]] += 1

    for code in original:
        if code not in target_codes:
            assert data[code] == original[code], f"非目标股票被修改：{code}"
    for row in audit:
        pairs = active_pairs(data[row["code"]])
        assert len(pairs) == len(set(pairs))
        got = [(cat, con) for cat, con in pairs if cat == "商业航天"]
        expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "商业航天" for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        for cat, con in expected:
            assert data[row["code"]]["jiuyan_concept_reason"].get(con) == row["reason"]
            assert data[row["code"]]["jiuyan_concept_desc"].get(con) == DESC[con]

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    commercial_rows = [x for x in audit if any(p["category"] == "商业航天" for p in x["pairs"])]
    print("动作统计：", dict(counts))
    print("商业航天判词：", dict(Counter(x["verdict"] for x in commercial_rows)))
    print("商业航天股池：", len(commercial_rows))
    print("二级概念：", len({c for x in commercial_rows for p in x["pairs"] if p["category"] == "商业航天" for c in p["concepts"]}))
    print("预览：", PREVIEW)
    if not apply:
        print("dry-run：未修改正式watchlist；加--apply执行。")
        return
    if not BAK.exists():
        shutil.copy2(SRC, BAK)
    shutil.copy2(PREVIEW, SRC)
    assert json.loads(SRC.read_text(encoding="utf-8")) == json.loads(PREVIEW.read_text(encoding="utf-8"))
    assert len(json.loads(BAK.read_text(encoding="utf-8"))) == 5400
    print("已写入：", SRC)
    print("备份：", BAK)


if __name__ == "__main__":
    main()
