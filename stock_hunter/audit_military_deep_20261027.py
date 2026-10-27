# -*- coding: utf-8 -*-
"""军工概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "military_audit_20261027.json"
CURRENT = ROOT / "data_cleaning" / "military_current_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_military_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_military_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 11:26:00"

DESC = {
    "航空整机-固定翼": "固定翼军用飞机整机研发、总装与交付，包括战斗机、运输机、教练机和特种飞机。",
    "航空整机-直升机": "军用直升机整机研发、总装与交付，不含仅提供一般零部件的企业。",
    "航空发动机与燃气轮机": "航空发动机、燃气轮机整机或关键动力分系统及直接配套，按实际产品层级标注。",
    "航空机载系统与航电": "机载航电、飞控、机电、座舱和任务系统等机载设备与分系统。",
    "航空结构件与锻铸件": "飞机和航空发动机用结构件、锻铸件、环形件及增材制造复杂零部件。",
    "航空地面保障装备": "军用机场和航空兵保障用地面电源、加油、牵引、场务及维修保障装备。",
    "航天装备与分系统": "航天器、运载火箭及地面发射装备的整机、分系统和直接配套产品。",
    "导弹与精确制导": "导弹、火箭弹、固体动力、导引头、制导与控制等武器装备及关键分系统。",
    "弹药与特种装备": "弹药、引信、火工品、光电对抗及其他直接军用特种装备。",
    "地面武器与装甲车辆": "坦克、装甲车辆、火炮及其他陆军地面武器平台与直接分系统。",
    "枪械与单兵装备": "枪械、轻武器及直接服务单兵作战的装备。",
    "船舶总装与海洋防务": "军用舰艇、军贸公务防务船艇和海洋防务装备总装，不把普通民船自动计入。",
    "舰船动力与推进": "舰船主动力、电力推进、综合电力系统及直接动力配套。",
    "水声与海洋电子": "声呐、水声探测、水下对抗及舰船专用电子系统。",
    "雷达与预警探测": "防空预警、火控、监视雷达及T/R组件等核心探测分系统。",
    "军用通信与指挥控制": "军用无线通信、数据链、指挥控制和战场信息系统。",
    "电子对抗与蓝军仿真": "电子干扰、光电对抗、蓝军装备和对抗训练仿真系统。",
    "军用导航与惯导": "军用惯性导航、北斗抗干扰终端和高可靠导航定位设备。",
    "军工芯片与特种集成电路": "面向特种装备的FPGA、射频、存储、抗辐照及其他高可靠集成电路。",
    "军工被动元件与连接器": "高可靠电容、电阻、滤波器、微波器件和军用连接器。",
    "军工电源与电机电气": "军用高可靠电源、电机、电驱、真空器件和专用电气设备。",
    "红外光电与激光": "军用红外探测、夜视、光电观测、激光与光学制导部件。",
    "军用无人装备与机器人": "军用无人机、靶机、无人车、排爆侦察机器人及任务系统，不含纯民用产品。",
    "军事仿真与军工软件": "面向武器装备研制、军事训练、态势推演和防务分析的软件与仿真系统。",
    "核化生防御与应急救援": "核化生侦检、防护、洗消及军队专用应急救援装备。",
    "军工材料-钛合金与高温合金": "用于军用航空、航天和装备关键部件的钛合金、高温合金及母合金。",
    "军工材料-碳纤维与复合材料": "用于军用装备承力与轻量化结构的碳纤维、预浸料和复合材料。",
    "军工材料-超材料与隐身材料": "用于武器装备电磁调控、隐身、伪装和特种防护的功能材料与结构。",
    "军工材料-特种玻璃与石英": "用于军用航空、航天和光电装备的石英玻璃、透明件与特种玻璃材料。",
    "军工材料-其他关键材料": "有明确军工应用的其他关键材料，须排除仅凭潜在用途的通用材料。",
    "军工配套-通用零部件": "已形成真实军工订单或收入的通用零部件与加工配套，军品未明确平台时使用。",
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


def remove_military(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "军工" not in cats:
            continue
        old.extend(("军工", con) for con in cons)
        keep = [cat for cat in cats if cat != "军工"]
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
    assert len(original) == 5400 and len(audit) == 97
    assert {x["code"] for x in audit} == target_codes
    used_concepts = {c for x in audit for p in x["pairs"] for c in p["concepts"]}
    assert used_concepts == set(DESC), (used_concepts - set(DESC), set(DESC) - used_concepts)
    counts = Counter()

    for row in audit:
        rec = data[row["code"]]
        assert rec["name"] == row["name"]
        before_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "军工"]
        old_pairs = remove_military(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        after_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "军工" and (cat, con) not in new_pairs]
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

        military_pairs = [(cat, con) for cat, con in new_pairs if cat == "军工"]
        if military_pairs:
            rec["jiuyan_military_verdicts"] = {"军工": row["verdict"]}
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        else:
            rec.pop("jiuyan_military_verdicts", None)
            if not active_pairs(rec):
                rec.pop("jiuyan_tag_verdict", None)
                rec.pop("jiuyan_tag_evidence", None)
        rec["jiuyan_military_audited_at"] = DATE
        rec["jiuyan_military_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info + ifind_get_stock_business_segmentation", "profile_asof": DATE,
        }
        rec["jiuyan_military_audit_20261027"] = {
            "action": row["action"], "old_pairs": old_pairs, "new_pairs": new_pairs,
            "verdict": row["verdict"], "reason": row["reason"],
        }
        rec["updated_at"] = MARK
        counts[row["action"]] += 1

    for code in original:
        if code not in target_codes:
            assert data[code] == original[code], f"非目标股票被修改：{code}"
    for row in audit:
        pairs = active_pairs(data[row["code"]])
        assert len(pairs) == len(set(pairs))
        got = [(cat, con) for cat, con in pairs if cat == "军工"]
        expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "军工" for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        for cat, con in expected:
            assert data[row["code"]]["jiuyan_concept_reason"].get(con) == row["reason"]
            assert data[row["code"]]["jiuyan_concept_desc"].get(con) == DESC[con]

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    military_rows = [x for x in audit if any(p["category"] == "军工" for p in x["pairs"])]
    print("动作统计：", dict(counts))
    print("军工判词：", dict(Counter(x["verdict"] for x in military_rows)))
    print("军工股池：", len(military_rows))
    print("二级概念：", len({c for x in military_rows for p in x["pairs"] if p["category"] == "军工" for c in p["concepts"]}))
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
