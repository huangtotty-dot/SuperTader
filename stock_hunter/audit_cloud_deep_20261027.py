# -*- coding: utf-8 -*-
"""云计算概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "cloud_audit_20261027.json"
CURRENT = ROOT / "data_cleaning" / "cloud_current_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_cloud_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_cloud_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 10:49:00"

DESC = {
    "IDC与云基础设施": "以自营或运营数据中心提供机柜、托管、带宽和运维，是云计算的物理基础设施；不自动等同云主机等IaaS。",
    "IaaS计算与存储": "通过网络按需交付云主机、裸金属、虚拟化、存储和算力资源，并按配置、时长或用量收费。",
    "PaaS-企业应用开发": "为企业财务、人力、供应链等应用提供云技术、开发、数据和连接集成平台。",
    "PaaS-容器与AI开发平台": "以容器、MaaS、AI Infra和工作流提供模型及应用开发运行环境。",
    "PaaS-工业低代码与数智底座": "面向工业软件的低代码、业务中台、数据中台和应用构建运行平台。",
    "PaaS-应用开发与运维": "提供低代码建模、服务编排、应用构建、发布和运维全生命周期的平台。",
    "PaaS-应用集成": "以iPaaS提供服务、消息、文件和数据的云端或私有化集成能力。",
    "PaaS-数据开发与分析": "以容器、多租户和弹性扩展提供数据库、大数据开发及智能分析运行环境。",
    "SaaS-互联网医疗与医院云服务": "以互联网医院、云医等在线平台提供医疗和医院云服务，须有实际运营或收入。",
    "SaaS-人力资源": "以在线订阅方式提供招聘、人事、薪酬等人力资源软件服务。",
    "SaaS-企业管理": "以在线订阅提供财务、人力、供应链、制造等企业管理软件。",
    "SaaS-办公协同": "以云文档、在线表单和协作平台提供组织办公及协同服务。",
    "SaaS-商户经营与餐饮门店": "面向商户提供在线会员、门店、营销、报表和餐饮经营工具。",
    "SaaS-地图选址与物流调度": "通过在线地图平台提供选址、分单、路线规划和人员轨迹管理。",
    "SaaS-外贸营销与获客": "面向外贸企业提供在线内容生成、营销和获客工具；普通B2B会员平台不全部计入。",
    "SaaS-工业企业管理": "以云上ERP、MES、PLM和设备云等支持工业企业经营管理。",
    "SaaS-建筑工程造价与成本管理": "以在线订阅提供算量、计价、清标和建筑成本管理服务。",
    "SaaS-数字创意与在线协作": "以订阅和云端工具提供视频、绘图、文档及在线协作服务。",
    "SaaS-智慧校园与政企服务": "以校园及政企云平台提供教学、餐饮、农水等在线应用和持续服务。",
    "SaaS-酒店与餐饮管理": "以云架构和订阅方式交付酒店PMS、餐饮POS及门店管理系统。",
    "云平台与多云管理": "提供私有云、混合云、多云纳管、云迁移、资源编排和持续运维，不等同普通系统集成。",
    "云通信与消息服务": "以CPaaS平台按消息和通信能力提供短信、RCS及国际消息触达服务。",
    "边缘计算与CDN": "依托边缘节点提供内容分发及边缘云主机、容器、存储；单纯CDN不自动等于IaaS。",
    "云安全与SaaS安全服务": "以云平台或订阅方式交付安全防护、检测和运营，归属网络安全而非通用云计算。",
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


def remove_cloud(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "云计算" not in cats:
            continue
        old.extend(("云计算", con) for con in cons)
        keep = [cat for cat in cats if cat != "云计算"]
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
    assert used_concepts == set(DESC), (used_concepts - set(DESC), set(DESC) - used_concepts)
    counts = Counter()

    for row in audit:
        rec = data[row["code"]]
        assert rec["name"] == row["name"]
        before_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "云计算"]
        old_pairs = remove_cloud(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        after_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "云计算" and (cat, con) not in new_pairs]
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

        cloud_pairs = [(cat, con) for cat, con in new_pairs if cat == "云计算"]
        if cloud_pairs:
            rec["jiuyan_cloud_verdicts"] = {"云计算": row["verdict"]}
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        else:
            rec.pop("jiuyan_cloud_verdicts", None)
            if not active_pairs(rec):
                rec.pop("jiuyan_tag_verdict", None)
                rec.pop("jiuyan_tag_evidence", None)
        rec["jiuyan_cloud_audited_at"] = DATE
        rec["jiuyan_cloud_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info", "profile_asof": DATE,
        }
        rec["jiuyan_cloud_audit_20261027"] = {
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
        got = [(cat, con) for cat, con in pairs if cat == "云计算"]
        expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "云计算" for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        for cat, con in [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]:
            assert (cat, con) in pairs
            assert data[row["code"]]["jiuyan_concept_reason"].get(con)
            assert data[row["code"]]["jiuyan_concept_desc"].get(con)

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    cloud_rows = [x for x in audit if any(p["category"] == "云计算" for p in x["pairs"])]
    print("动作统计：", dict(counts))
    print("云计算判词：", dict(Counter(x["verdict"] for x in cloud_rows)))
    print("云计算股池：", len(cloud_rows))
    print("二级概念：", len({c for x in cloud_rows for p in x["pairs"] if p["category"] == "云计算" for c in p["concepts"]}))
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
