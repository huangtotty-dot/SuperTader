# -*- coding: utf-8 -*-
"""机器人与物理AI概念深度清洗（2026-10-27）。

仅清理“机器人/物理AI”槽位，保留其他产业链标签；支持一股多分类。
默认 dry-run，--apply 才备份并写正式 watchlist。
"""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "robot_physicalai_audit_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_robot_physicalai_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_robot_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 09:30:00"
TARGETS = {"机器人", "物理AI"}

DESC = {
    "本体及应用": "工业机器人、特种机器人本体及其行业应用，须有整机、系统或实际项目交付；自动化产线集成与具身智能本体另列。",
    "自动化集成": "以机器人、控制系统和物流/制造装备完成产线或系统方案设计、集成、调试和验收，不等同机器人本体制造。",
    "物流系统": "智能分拣、输送、仓储及WMS/WCS等软硬件一体化物流系统，按项目交付，不单列为通用机器人本体。",
    "工业自动化": "PLC、伺服、控制与工业机器人等自动化产品和方案；普通工控产品只有在存在直接机器人产品或应用时纳入。",
    "场景应用": "在文旅、演艺、巡检等具体场景运营或交付机器人应用项目，不据此推断具备本体制造能力。",
    "减速器": "谐波、RV、行星等机器人关节精密减速器，直接承担降速增扭和精密传动。",
    "减速器零部件": "摆线轮、针齿壳、行星架、刚轮等减速器内部工作部件，不等同完整减速器。",
    "丝杠": "滚珠或行星滚柱丝杠，把旋转运动转为精密直线运动，用于机器人线性关节和执行器。",
    "丝杠零部件": "螺母、滚柱、主丝杠等丝杠内部零件，不等同完整丝杠或执行器。",
    "丝杠设备": "用于丝杠及螺母螺纹、滚道精密加工的车磨专用装备，属于上游制造设备。",
    "加工刀具": "用于丝杠、减速器等机器人零件加工的专用刀具和量检具，属于制造耗材而非机器人零件。",
    "轴承": "交叉滚子、柔性、薄壁、自润滑等机器人关节或减速器轴承。",
    "滚动体": "钢球、滚子、陶瓷球等轴承或丝杠内部滚动元件，不等同轴承总成。",
    "电机": "空心杯、无框力矩、伺服、步进等机器人动力电机及直接配套驱动产品。",
    "微特减速电机": "微型电机与减速机构集成的动力部件，须有明确机器人产品或供货应用。",
    "精密铁芯": "用于空心杯、无框力矩等机器人电机的精密铁芯部件，属于执行器上游零件。",
    "伺服系统": "伺服驱动器、伺服电机及随动控制总成，承担机器人位置、速度和力矩控制。",
    "关节模组": "集成减速器、电机、编码器、驱动或传感器的机器人关节单元。",
    "执行器": "将电机、丝杠、减速及控制部件集成为旋转或直线运动输出的执行装置。",
    "总成": "机器人旋转/线性关节、灵巧手或机电执行系统的总成级产品。",
    "灵巧手": "多自由度机器人灵巧手及直接配套驱动模组。",
    "微型链/灵巧手": "以微型链传动实现灵巧手运动的部件和整手产品，区别于腱绳传动路线。",
    "腱绳材料": "用于灵巧手腱绳传动的高强纤维、钢丝绳及复合材料。",
    "弹性元件": "用于机器人关节、灵巧手复位、连接和传动的弹簧、卡簧等弹性元件。",
    "结构件": "机器人躯干、头部、执行器壳体等承载、防护或轻量化结构件。",
    "线缆": "用于机器人关节和本体运动的高耐弯折线缆及组件。",
    "物理AI_具身本体": "具备环境感知、任务决策和物理动作执行能力的机器人或无人系统本体；传统工业机器人收入不直接视为具身收入。",
    "物理AI_传感器": "力觉、触觉、惯性、压力等物理状态感知器件及模组，服务机器人或无人系统的反馈与控制。",
    "物理AI_机器视觉": "工业相机、3D视觉、光源镜头和视觉算法，承担物理环境识别、定位、测量和引导。",
    "物理AI_智能控制": "运动控制、驱控一体或机器人大小脑控制器，把感知和任务指令转为物理设备动作。",
    "物理AI_仿真训练": "物理模拟、批量仿真、模型训练、测试验证及训练场工具链，服务具身模型开发和迭代。",
    "物理AI_数据采集": "通过真机、多摄像设备或仿真环境采集、生成和治理机器人训练所需多模态数据。",
    "物理AI_动作捕捉": "光学、惯性或无标记动作捕捉系统及动作模型，为机器人提供动作数据采集、映射和训练。",
    "物理AI_通信模组": "蜂窝、短距、GNSS、卫星及边缘计算模组，为物理AI设备提供连接、定位和端侧算力。",
    "物理AI_智能终端": "具备感知、交互和端侧智能能力的XR、AIoT、陪伴及其他智能终端硬件。",
}


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


def active_pairs(rec):
    out = []
    for _, cat_key, con_key in slots(rec):
        cats, cons = toks(rec.get(cat_key)), toks(rec.get(con_key))
        for cat in cats:
            for con in cons:
                if (cat, con) not in out:
                    out.append((cat, con))
    return out


def active_concepts(rec):
    return list(dict.fromkeys(con for _, con in active_pairs(rec)))


def remove_target_slots(rec):
    old_pairs = []
    for _, cat_key, con_key in list(slots(rec)):
        cats, cons = toks(rec.get(cat_key)), toks(rec.get(con_key))
        if not any(cat in TARGETS for cat in cats):
            continue
        for cat in cats:
            if cat in TARGETS:
                old_pairs.extend((cat, con) for con in cons)
        # 目标分类在现有数据中独占槽位；如出现混合槽位则只移除目标category并保留其他concept。
        keep_cats = [cat for cat in cats if cat not in TARGETS]
        if keep_cats and len(keep_cats) == len(cats):
            continue
        if keep_cats:
            rec[cat_key] = keep_cats[0] if len(keep_cats) == 1 else keep_cats
        else:
            rec.pop(cat_key, None)
            rec.pop(con_key, None)
    return list(dict.fromkeys(old_pairs))


def add_slot(rec, category, concepts):
    value = concepts[0] if len(concepts) == 1 else concepts
    for i in list(range(1, 10)) + [0]:
        cat_key, con_key = slot_names(i)
        if not str(rec.get(cat_key) or "").strip():
            rec[cat_key] = category
            rec[con_key] = value
            return
    raise RuntimeError(f"标签槽位不足：{rec.get('name')} {category}/{concepts}")


def pair_verdict(row, category):
    if len(row["pairs"]) == 1:
        return row["verdict"]
    # 混合主营：传统业务与具身业务分开定级，不用传统业务收入抬升具身判词。
    if row["code"] in {"300607", "688306"}:
        return "核心" if category == "机器人" else "相关"
    return row["verdict"]


def validate_audit(audit, current):
    assert len(audit) == 137 and len({x["code"] for x in audit}) == 137
    assert {x["code"] for x in audit} == {x["code"] for x in current}
    for row in audit:
        assert row["verdict"] in {"核心", "相关", "摘除"}
        assert (not row["pairs"]) == (row["action"] == "摘除")
        assert len(row["reason"]) >= 60, (row["code"], "理由过短")
        seen = set()
        for pair in row["pairs"]:
            cat = pair["category"]
            assert cat in TARGETS and pair["concepts"]
            for con in pair["concepts"]:
                assert (cat, con) not in seen
                seen.add((cat, con))
                assert con in DESC, (row["code"], con)
                assert con.startswith("物理AI_") if cat == "物理AI" else not con.startswith("物理AI_")


def main():
    apply = "--apply" in sys.argv
    data = json.loads(SRC.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    current = json.loads((ROOT / "data_cleaning" / "robot_current_20261027.json").read_text(encoding="utf-8"))
    validate_audit(audit, current)
    counts = Counter()
    for row in audit:
        code = row["code"]
        rec = data.get(code)
        if not isinstance(rec, dict):
            raise KeyError(f"watchlist 缺少 {code} {row['name']}")
        assert rec.get("name") == row["name"], (code, rec.get("name"), row["name"])
        old_pairs = remove_target_slots(rec)
        for pair in row["pairs"]:
            add_slot(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        current_concepts = set(active_concepts(rec))
        old_concepts = {con for _, con in old_pairs}

        reasons = rec.get("jiuyan_concept_reason")
        if not isinstance(reasons, dict):
            reasons = {}
        for key in list(reasons):
            if key in old_concepts and key not in current_concepts:
                reasons.pop(key, None)
        for pair in row["pairs"]:
            for concept in pair["concepts"]:
                category_reason = row["reason"]
                if len(row["pairs"]) > 1:
                    category_reason += f" 本分类（{pair['category']}）判词为{pair_verdict(row, pair['category'])}。"
                reasons[concept] = category_reason
        if reasons:
            rec["jiuyan_concept_reason"] = reasons
        else:
            rec.pop("jiuyan_concept_reason", None)

        descs = rec.get("jiuyan_concept_desc")
        if not isinstance(descs, dict):
            descs = {}
        for key in list(descs):
            if key in old_concepts and key not in current_concepts:
                descs.pop(key, None)
        for _, concept in new_pairs:
            descs[concept] = DESC[concept]
        if descs:
            rec["jiuyan_concept_desc"] = descs
        else:
            rec.pop("jiuyan_concept_desc", None)

        verdicts = {p["category"]: pair_verdict(row, p["category"]) for p in row["pairs"]}
        rec["jiuyan_robot_physicalai_verdicts"] = verdicts
        rec["jiuyan_robot_physicalai_audited_at"] = DATE
        rec["jiuyan_robot_physicalai_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info", "profile_asof": DATE,
        }
        rec["jiuyan_robot_physicalai_audit_20261027"] = {
            "action": row["action"], "old_pairs": old_pairs, "new_pairs": new_pairs,
            "verdict": row["verdict"], "category_verdicts": verdicts, "reason": row["reason"],
        }
        if new_pairs:
            rec["jiuyan_tag_verdict"] = "核心" if "核心" in verdicts.values() else "相关"
            rec["jiuyan_tag_evidence"] = "text"
        elif active_concepts(rec):
            rec["jiuyan_tag_verdict"] = "已摘牌"
        else:
            rec.pop("jiuyan_tag_verdict", None)
            rec.pop("jiuyan_tag_evidence", None)
        rec["updated_at"] = MARK
        counts[row["action"]] += 1

    # 全文件验证：137只分类与审计一致，其他股票不被修改范围触碰。
    for row in audit:
        rec = data[row["code"]]
        got = [(cat, con) for cat, con in active_pairs(rec) if cat in TARGETS]
        expected = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        assert len(got) == len(set(got))
        for _, con in got:
            assert rec["jiuyan_concept_reason"].get(con)
            assert rec["jiuyan_concept_desc"].get(con)

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("动作统计：", dict(counts))
    print("判词统计：", dict(Counter(x["verdict"] for x in audit)))
    print("分类槽位：", dict(Counter(p["category"] for x in audit for p in x["pairs"])))
    print(f"清洗后完整预览：{PREVIEW}")
    if not apply:
        print("dry-run：未修改正式 watchlist；加 --apply 执行。")
        return
    if not BAK.exists():
        shutil.copy2(SRC, BAK)
    shutil.copy2(PREVIEW, SRC)
    assert json.loads(SRC.read_text(encoding="utf-8")) == json.loads(PREVIEW.read_text(encoding="utf-8"))
    print(f"已写入：{SRC}\n备份：{BAK}")


if __name__ == "__main__":
    main()
