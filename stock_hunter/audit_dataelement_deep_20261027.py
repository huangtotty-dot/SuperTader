# -*- coding: utf-8 -*-
"""数据要素概念深度清洗（2026-10-27）。

输入：data_cleaning/dataelement_audit_20261027.json（98 只逐股复核结论）。
动作：重写详细理由、纠正二级概念、摘除边缘/错标，并把物理 AI 标的迁出数据要素。
默认 dry-run；加 --apply 才写 watchlist，写入前自动备份。
"""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "dataelement_audit_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_dataelement_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_cleaned_20261027.json"
MARK = "2026-10-27 08:11:00"
DATE = "2026-10-27"

DESC = {
    "数据要素_数据运营": "对获授权的公共数据或行业数据开展治理、融合、产品孵化、上架和持续运营；交付物是可使用、可流通的数据产品，而非单纯云资源或信息化项目。",
    "数据要素_数据治理": "数据资源普查、目录管理、质量控制、标准化和资产管理，把分散数据整理成可管理、可使用的数据资产；通常以软件许可、实施和运维交付。",
    "数据要素_数据库": "关系型、分布式、多模态等数据库软件，承担数据组织、查询、事务处理和管理；通过软件许可、一体机、迁移及运维服务交付，与IDC和通用存储设备区分。",
    "数据要素_教育数据": "围绕教学、考务、课堂质量和产业需求形成教育数据库、诊断及分析服务；须有明确教育数据产品，不把一般教育信息化或系统安全组件混入。",
    "数据要素_水务数据": "汇聚水厂、泵站、管网和供水调度数据，用于控漏、调度与资产管理；以水务软件、SaaS、智能终端及运维项目交付。",
    "物理AI_智能终端": "服务物理AI交互与运行的智能终端设备，包括XR、AIoT及其他端侧硬件；属于终端产业链，不等同于数据要素或数据采集服务。",
    "物理AI_传感器": "机器人触觉、压力、剪切力等感知传感器及方案，属于物理AI上游感知硬件，不等同于数据产品或数据运营。",
    "物理AI_机器视觉": "工业相机、光源、镜头和视觉算法构成的机器视觉感知链，服务检测识别；属于物理AI上游设备，不直接视为数据要素业务。",
}
# 旧定义需要随本次清洗同步收紧；其他概念沿用已有解释。
DESC.update({
    "数据要素_数据存储": "IDC、云存储、存储阵列、容灾和备份等数据承载基础设施，按机柜、云资源、软硬件或项目收费；数据库软件单列为“数据库”，数据运营单列为“数据运营”。",
    "数据要素_交易所": "实际控股、运营数据交易机构或依托交易机构提供上架、流通服务；少量参股、一般技术建设或第三方数据服务商不直接归为交易所运营标的。",
    "数据要素_内容数据": "数字阅读、可授权版权语料或已形成销售的数字内容库；必须有数字内容交付、订阅或授权证据，不能仅凭纸质出版版权就归为内容数据。",
    "数据要素_交通数据": "汇聚车辆、路况、客流、停车或港航数据并提供平台、分析、运营服务；纯ETC、雷达、路侧设备等上游感知硬件不按交通数据产品处理。",
    "数据要素_汽车数据": "车辆测试、物流、电池或车联网数据已形成登记、上架、平台或运营服务；仅销售场地、设备或在内部产生数据，不作为汽车数据产品。",
})


def toks(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


def slot_names(i):
    return ("jiuyan_category", "jiuyan_concept") if i == 0 else (f"jiuyan_category{i}", f"jiuyan_concept{i}")


def slots(rec):
    for i in list(range(1, 10)) + [0]:
        cat_key, con_key = slot_names(i)
        if str(rec.get(cat_key) or "").strip():
            yield i, cat_key, con_key


def active_concepts(rec):
    out = []
    for _, _, con_key in slots(rec):
        out.extend(toks(rec.get(con_key)))
    return list(dict.fromkeys(out))


def is_dataelement_slot(cat, con):
    cats, cons = toks(cat), toks(con)
    return any(c == "数据要素" for c in cats) or any(x.startswith("数据要素_") or x == "物理AI_数据采集" for x in cons)


def remove_dataelement_slots(rec):
    removed = []
    for _, cat_key, con_key in list(slots(rec)):
        cat, con = rec.get(cat_key), rec.get(con_key)
        if not is_dataelement_slot(cat, con):
            continue
        cats, cons = toks(cat), toks(con)
        # 数据要素槽位可能因历史重复或多标签而存在；本任务只清这一类目，保留其他类目。
        if any(c == "数据要素" for c in cats):
            removed.extend(x for x in cons if x.startswith("数据要素_") or x == "物理AI_数据采集")
            rec.pop(cat_key, None)
            rec.pop(con_key, None)
        else:
            keep = [x for x in cons if not (x.startswith("数据要素_") or x == "物理AI_数据采集")]
            removed.extend(x for x in cons if x not in keep)
            if keep:
                rec[con_key] = keep[0] if len(keep) == 1 else keep
            else:
                rec.pop(cat_key, None)
                rec.pop(con_key, None)
    return list(dict.fromkeys(removed))


def add_slot(rec, category, concepts):
    if not category or not concepts:
        return
    value = concepts[0] if len(concepts) == 1 else concepts
    for i in list(range(1, 10)) + [0]:
        cat_key, con_key = slot_names(i)
        if not str(rec.get(cat_key) or "").strip():
            rec[cat_key] = category
            rec[con_key] = value
            return
    raise RuntimeError(f"标签槽位不足：{rec.get('name')} {category}/{concepts}")


def main():
    apply = "--apply" in sys.argv
    data = json.loads(SRC.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    assert len(audit) == 98 and len({x["code"] for x in audit}) == 98
    counts = Counter()
    for row in audit:
        code = row["code"]
        rec = data.get(code)
        if not isinstance(rec, dict):
            raise KeyError(f"watchlist 缺少 {code} {row['name']}")
        old = remove_dataelement_slots(rec)
        add_slot(rec, row["category"], row["concepts"])
        current = set(active_concepts(rec))

        reasons = rec.get("jiuyan_concept_reason")
        if not isinstance(reasons, dict):
            reasons = {}
        for key in list(reasons):
            if (key.startswith("数据要素_") or key == "物理AI_数据采集") and key not in current:
                reasons.pop(key, None)
        for concept in row["concepts"]:
            reasons[concept] = row["reason"]
        if reasons:
            rec["jiuyan_concept_reason"] = reasons
        else:
            rec.pop("jiuyan_concept_reason", None)

        descs = rec.get("jiuyan_concept_desc")
        if not isinstance(descs, dict):
            descs = {}
        for key in list(descs):
            if (key.startswith("数据要素_") or key == "物理AI_数据采集") and key not in current:
                descs.pop(key, None)
        for concept in row["concepts"]:
            if concept in DESC:
                descs[concept] = DESC[concept]
        if descs:
            rec["jiuyan_concept_desc"] = descs
        else:
            rec.pop("jiuyan_concept_desc", None)

        rec["jiuyan_dataelement_verdict"] = row["verdict"]
        rec["jiuyan_dataelement_audited_at"] = DATE
        rec["jiuyan_dataelement_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info", "profile_asof": DATE,
        }
        if row["action"] in ("摘除", "迁出数据要素"):
            rec["jiuyan_dataelement_removed_20261027"] = {
                "action": row["action"], "old_concepts": old, "reason": row["reason"],
            }
        else:
            rec.pop("jiuyan_dataelement_removed_20261027", None)

        if row["concepts"]:
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        elif active_concepts(rec):
            rec["jiuyan_tag_verdict"] = "已摘牌"
        else:
            rec.pop("jiuyan_tag_verdict", None)
            rec.pop("jiuyan_tag_evidence", None)
        rec["updated_at"] = MARK
        counts[row["action"]] += 1
        print(f"{code} {row['name']:<7s} {row['action']:<8s} {row['verdict']}  {','.join(row['concepts']) or '—'}")

    remaining = []
    for code, rec in data.items():
        if not isinstance(rec, dict):
            continue
        for _, cat_key, con_key in slots(rec):
            if "数据要素" in str(rec.get(cat_key) or ""):
                remaining.append(code)
                break
    print("\n动作统计：", dict(counts))
    print(f"数据要素股池：98 → {len(remaining)}；物理AI迁移 7；完全摘除 14")
    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"清洗后完整预览：{PREVIEW}")
    if not apply:
        print("\ndry-run：未修改正式 watchlist；加 --apply 执行。")
        return
    if not BAK.exists():
        shutil.copy2(SRC, BAK)
    shutil.copy2(PREVIEW, SRC)
    print(f"已写入：{SRC}\n备份：{BAK}")


if __name__ == "__main__":
    main()
