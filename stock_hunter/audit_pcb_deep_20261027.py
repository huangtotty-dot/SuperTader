# -*- coding: utf-8 -*-
"""PCB概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "pcb_audit_20261027.json"
CURRENT = ROOT / "data_cleaning" / "pcb_current_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_pcb_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_pcb_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 14:39:00"

DESC = json.loads((ROOT / "data_cleaning" / "pcb_taxonomy_20261027.json").read_text(encoding="utf-8"))


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


def remove_pcb(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "PCB" not in cats:
            continue
        old.extend(("PCB", con) for con in cons)
        keep = [cat for cat in cats if cat != "PCB"]
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
    assert len(original) == 5400 and len(audit) == 89
    assert {x["code"] for x in audit} == target_codes
    used_concepts = {c for x in audit for p in x["pairs"] for c in p["concepts"]}
    assert used_concepts <= set(DESC), used_concepts - set(DESC)
    counts = Counter()

    for row in audit:
        rec = data[row["code"]]
        assert rec["name"] == row["name"]
        before_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "PCB"]
        old_pairs = remove_pcb(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        after_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "PCB" and (cat, con) not in new_pairs]
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

        commercial_pairs = [(cat, con) for cat, con in new_pairs if cat == "PCB"]
        if commercial_pairs:
            rec["jiuyan_pcb_verdicts"] = {"PCB": row["verdict"]}
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        else:
            rec.pop("jiuyan_pcb_verdicts", None)
            if not active_pairs(rec):
                rec.pop("jiuyan_tag_verdict", None)
                rec.pop("jiuyan_tag_evidence", None)
        rec["jiuyan_pcb_audited_at"] = DATE
        rec["jiuyan_pcb_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info + ifind_get_stock_business_segmentation", "profile_asof": DATE,
        }
        rec["jiuyan_pcb_audit_20261027"] = {
            "action": row["action"], "old_pairs": old_pairs, "new_pairs": new_pairs,
            "verdict": row["verdict"], "reason": row["reason"],
        }
        if row.get("borderline_review"):
            rec["jiuyan_pcb_audit_20261027"]["borderline_review"] = row["borderline_review"]
        rec["updated_at"] = MARK
        counts[row["action"]] += 1

    for code in original:
        if code not in target_codes:
            assert data[code] == original[code], f"非目标股票被修改：{code}"
    for row in audit:
        pairs = active_pairs(data[row["code"]])
        assert len(pairs) == len(set(pairs))
        got = [(cat, con) for cat, con in pairs if cat == "PCB"]
        expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "PCB" for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        for cat, con in expected:
            assert data[row["code"]]["jiuyan_concept_reason"].get(con) == row["reason"]
            assert data[row["code"]]["jiuyan_concept_desc"].get(con) == DESC[con]

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    commercial_rows = [x for x in audit if any(p["category"] == "PCB" for p in x["pairs"])]
    print("动作统计：", dict(counts))
    print("PCB判词：", dict(Counter(x["verdict"] for x in commercial_rows)))
    print("PCB股池：", len(commercial_rows))
    print("二级概念：", len({c for x in commercial_rows for p in x["pairs"] if p["category"] == "PCB" for c in p["concepts"]}))
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
