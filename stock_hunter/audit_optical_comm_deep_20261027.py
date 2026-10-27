# -*- coding: utf-8 -*-
"""光通信概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "optical_comm_audit_20261027.json"
CURRENT = ROOT / "data_cleaning" / "optical_comm_current_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_optical_comm_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_optical_comm_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 13:13:00"

DESC = {
    "光模块-数通与AI算力": "用于数据中心、AI集群和云计算网络的数通光模块及有源光缆，按真实速率与交付能力标注。",
    "光模块-电信与传输": "用于电信承载、传输、接入和无线网络的光模块，不与数通产品混同。",
    "光器件-无源器件": "光分路器、波分复用器、隔离器、耦合器、光纤连接与无源光器件。",
    "光器件-有源器件": "光收发组件、光放大器、光引擎及其他需要有源光电转换的光器件。",
    "光芯片-激光器与探测器": "光通信用激光器、探测器及其外延和芯片，必须有实际光通信产品或供货证据。",
    "光芯片-PLC与AWG": "PLC、AWG等无源光芯片、晶圆和集成光路产品。",
    "CPO与硅光集成": "CPO、LPO、硅光芯片、硅光引擎和光电共封装产品；研发建设期不等于形成收入。",
    "光纤预制棒与光纤": "通信光纤预制棒和通信光纤，不将塑料光纤之外的能力或潜在产能虚构为石英光纤。",
    "光缆与线缆": "通信光缆、光电复合缆和实际用于光网络的线缆，电力电缆不自动计入。",
    "特种光纤与光纤环": "保偏光纤、掺镱光纤、光纤环等特种光纤产品，通信与传感用途须分开说明。",
    "MPO/MTP与高密度连接": "MPO/MTP、MT插芯、高密度预端接跳线及数据中心高密度光连接产品。",
    "光连接器与陶瓷插芯": "光连接器、陶瓷插芯、光跳线及光纤活动连接产品，普通电连接器不自动计入。",
    "光通信材料-光学与晶体": "已用于光通信调制器、探测器、激光器或集成光路的光学晶体与功能材料。",
    "光通信材料-封装与胶材": "光纤光缆涂覆、光器件封装、光学胶黏等已有光通信应用的材料。",
    "光通信设备-测试与生产": "光模块、光器件、MPO等产品的生产、耦合、测试和自动化设备，须有客户验证或交付。",
    "光传输与接入设备": "OTN、PTN、PON、光传输、光接入和光网络系统设备。",
    "光网络系统与工程服务": "通信光网络建设、系统集成、工程与运维服务，算力租赁不自动计入。",
    "光通信配套-精密结构件": "已有光通信客户或供货的精密结构件、注塑件和封装配套，低占比业务原则上列相关。",
    "时钟晶振与频率器件": "已明确用于光模块或光通信设备的晶振和频率器件，不能以通用晶振用途替代应用证据。",
    "光通信股权与产业投资": "已实际持有光通信企业股权或基金份额，须与自身经营业务区分。",
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


def remove_optical_comm(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "光通信" not in cats:
            continue
        old.extend(("光通信", con) for con in cons)
        keep = [cat for cat in cats if cat != "光通信"]
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
    assert len(original) == 5400 and len(audit) == 45
    assert {x["code"] for x in audit} == target_codes
    used_concepts = {c for x in audit for p in x["pairs"] for c in p["concepts"]}
    assert used_concepts <= set(DESC), used_concepts - set(DESC)
    counts = Counter()

    for row in audit:
        rec = data[row["code"]]
        assert rec["name"] == row["name"]
        before_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "光通信"]
        old_pairs = remove_optical_comm(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        after_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "光通信" and (cat, con) not in new_pairs]
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

        commercial_pairs = [(cat, con) for cat, con in new_pairs if cat == "光通信"]
        if commercial_pairs:
            rec["jiuyan_optical_comm_verdicts"] = {"光通信": row["verdict"]}
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        else:
            rec.pop("jiuyan_optical_comm_verdicts", None)
            if not active_pairs(rec):
                rec.pop("jiuyan_tag_verdict", None)
                rec.pop("jiuyan_tag_evidence", None)
        rec["jiuyan_optical_comm_audited_at"] = DATE
        rec["jiuyan_optical_comm_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info + ifind_get_stock_business_segmentation", "profile_asof": DATE,
        }
        rec["jiuyan_optical_comm_audit_20261027"] = {
            "action": row["action"], "old_pairs": old_pairs, "new_pairs": new_pairs,
            "verdict": row["verdict"], "reason": row["reason"],
        }
        if row.get("borderline_review"):
            rec["jiuyan_optical_comm_audit_20261027"]["borderline_review"] = row["borderline_review"]
        rec["updated_at"] = MARK
        counts[row["action"]] += 1

    for code in original:
        if code not in target_codes:
            assert data[code] == original[code], f"非目标股票被修改：{code}"
    for row in audit:
        pairs = active_pairs(data[row["code"]])
        assert len(pairs) == len(set(pairs))
        got = [(cat, con) for cat, con in pairs if cat == "光通信"]
        expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "光通信" for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        for cat, con in expected:
            assert data[row["code"]]["jiuyan_concept_reason"].get(con) == row["reason"]
            assert data[row["code"]]["jiuyan_concept_desc"].get(con) == DESC[con]

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    commercial_rows = [x for x in audit if any(p["category"] == "光通信" for p in x["pairs"])]
    print("动作统计：", dict(counts))
    print("光通信判词：", dict(Counter(x["verdict"] for x in commercial_rows)))
    print("光通信股池：", len(commercial_rows))
    print("二级概念：", len({c for x in commercial_rows for p in x["pairs"] if p["category"] == "光通信" for c in p["concepts"]}))
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
