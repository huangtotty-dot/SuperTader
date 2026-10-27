# -*- coding: utf-8 -*-
"""AI应用概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "aiapp_audit_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_aiapp_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_aiapp_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 11:20:00"

DESC = {
    "AI营销": "以AIGC、智能体或营销大模型完成客户洞察、内容生成、投放和归因；普通广告代理、媒介采购或仅使用平台算法不直接纳入。",
    "AI影视短剧": "以生成式AI完成影视、短剧、漫剧的剧本、分镜、画面、配音或剪辑，须有真实工具、上线作品或客户项目；普通短剧不等同AI影视。",
    "AI视觉": "面向影像、内容或非物理控制场景的计算机视觉算法和解决方案；车载、安防及工业物理感知另列物理AI机器视觉。",
    "AI教育": "AI课程、教学大模型、智能备课、批改、辅学及校园教育助手，须有实际课程、产品或学校项目。",
    "AI养老": "面向居家或社区养老的AI智能体、健康交互与个性化服务，须有实际产品或社区应用。",
    "AI游戏": "AI原生玩法、智能NPC、游戏内AI伙伴或深度嵌入研运的垂直模型；单纯使用通用美术工具不定核心。",
    "AI运维": "将大模型、知识图谱和智能算法用于告警、根因分析、预测性维护及设施运维。",
    "AI大模型": "自研通用或垂直大模型并通过API、MaaS、订阅、部署或行业项目商业化。",
    "AI办公": "嵌入公文、文档、协同、财务、人力、评审和ERP流程的AI助手、智能体及企业软件。",
    "AI制造": "服务研发设计、生产、工艺、质量或工业管理的AI软件和智能体；直接控制物理设备的业务可另列物理AI。",
    "AI智慧城市": "以人工智能支撑城市安全、能源、低空和公共治理的解决方案及实际城市项目。",
    "AI电视": "在IPTV、互联网电视和视听服务中提供AI搜索、交互、运营和智能内容能力。",
    "AI购物": "AI导购、购物助手、商品问答和消费决策智能体，具有真实用户产品或商业合作。",
    "AI会展": "用于会展搜索、翻译、记录、写作、获客和买家匹配的AI工具及智能体。",
    "AI出版": "将AI用于出版策划、内容生产、排版和IP开发的平台或产品，不等同传统出版数字化。",
    "AI内容审核": "利用多模态模型开展文本、音频、视频识别、内容风控、合规审核和AIGC安全治理。",
    "AI财税": "面向税务、财务和会计专业场景的垂类模型、智能体及数智财税产品。",
    "AI政务": "用于政务服务、办事助手、政务公文和治理场景的AI智能体及大模型应用。",
    "AI数据分析": "以ChatBI、DataAgent等完成经营分析、数据问答、洞察和决策支持的产品。",
    "数据要素_训练数据": "用于模型训练的语音、文本、图片或多模态数据集，经采集、清洗、标注、确权后以授权或定制项目交付。",
    "数据要素_内容数据": "具有合法权属并可授权、订阅或交易的数字内容库；须有实际授权或数据产品交付，不能仅因拥有版权就纳入。",
    "物理AI_智能终端": "具备感知、交互和端侧智能能力的AI眼镜、耳机、玩具、金融终端、教育设备及其他实体终端。",
    "物理AI_通信模组": "蜂窝、短距、GNSS、卫星及边缘计算模组，为物理AI设备提供连接、定位和端侧算力。",
    "物理AI_机器视觉": "摄像机、3D视觉、边缘视觉和算法，用于物理环境识别、定位、检测、告警或设备引导。",
    "物理AI_智能控制": "把空间、设备或环境数据转为物理设备控制指令的AIoT、驱控及工业智能控制平台。",
    "物理AI_仿真训练": "物理模拟、合成数据、批量仿真、模型训练及测试验证工具链。",
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


def remove_aiapp(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "AI应用" not in cats:
            continue
        old.extend(("AI应用", con) for con in cons)
        keep = [cat for cat in cats if cat != "AI应用"]
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
    data = json.loads(SRC.read_text(encoding="utf-8"))
    audit = json.loads(AUDIT.read_text(encoding="utf-8"))
    current = json.loads((ROOT / "data_cleaning" / "aiapp_current_20261027.json").read_text(encoding="utf-8"))
    assert len(audit) == 117 and {x["code"] for x in audit} == {x["code"] for x in current}
    counts = Counter()
    for row in audit:
        rec = data[row["code"]]
        assert rec["name"] == row["name"]
        old_pairs = remove_aiapp(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        current_concepts = {con for _, con in active_pairs(rec)}
        old_concepts = {con for _, con in old_pairs}

        reasons = rec.get("jiuyan_concept_reason")
        if not isinstance(reasons, dict):
            reasons = {}
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

        rec["jiuyan_aiapp_verdicts"] = {p["category"]: row["verdict"] for p in row["pairs"]}
        rec["jiuyan_aiapp_audited_at"] = DATE
        rec["jiuyan_aiapp_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info", "profile_asof": DATE,
        }
        rec["jiuyan_aiapp_audit_20261027"] = {
            "action": row["action"], "old_pairs": old_pairs, "new_pairs": new_pairs,
            "verdict": row["verdict"], "reason": row["reason"],
        }
        if new_pairs:
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        elif active_pairs(rec):
            rec["jiuyan_tag_verdict"] = "已摘牌"
        else:
            rec.pop("jiuyan_tag_verdict", None)
            rec.pop("jiuyan_tag_evidence", None)
        rec["updated_at"] = MARK
        counts[row["action"]] += 1

    for row in audit:
        pairs = active_pairs(data[row["code"]])
        assert len(pairs) == len(set(pairs))
        ai_got = [(cat, con) for cat, con in pairs if cat == "AI应用"]
        ai_expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "AI应用" for c in p["concepts"]]
        assert ai_got == ai_expected, (row["code"], ai_got, ai_expected)
        for cat, con in [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]:
            assert (cat, con) in pairs, (row["code"], cat, con)
            assert data[row["code"]]["jiuyan_concept_reason"].get(con)
            assert data[row["code"]]["jiuyan_concept_desc"].get(con)

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("动作统计：", dict(counts))
    print("判词统计：", dict(Counter(x["verdict"] for x in audit)))
    print("审计分类：", dict(Counter(p["category"] for x in audit for p in x["pairs"])))
    print("预览：", PREVIEW)
    if not apply:
        print("dry-run：未修改正式watchlist；加--apply执行。")
        return
    if not BAK.exists():
        shutil.copy2(SRC, BAK)
    shutil.copy2(PREVIEW, SRC)
    assert json.loads(SRC.read_text(encoding="utf-8")) == json.loads(PREVIEW.read_text(encoding="utf-8"))
    print("已写入：", SRC)
    print("备份：", BAK)


if __name__ == "__main__":
    main()
