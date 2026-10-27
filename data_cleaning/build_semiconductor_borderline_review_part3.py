# -*- coding: utf-8 -*-
"""生成半导体第5、6组独立二次复审；不修改初审、研究输入或watchlist。"""
import json
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
INPUT = ROOT / "semiconductor_borderline_review_part3_input_20261027.json"
NORMALIZED = ROOT / "ifind_part3" / "ifind_part3_normalized.json"
OUTPUT = ROOT / "semiconductor_borderline_review_part3_20261027.json"
RESEARCH_FILES = [
    ROOT / "semiconductor_research_group5_20261027.json",
    ROOT / "semiconductor_research_group6_20261027.json",
]

initial = json.loads(INPUT.read_text(encoding="utf-8"))
normalized_rows = json.loads(NORMALIZED.read_text(encoding="utf-8"))
normalized = {x["code"]: x for x in normalized_rows}
research = []
for path in RESEARCH_FILES:
    research.extend(json.loads(path.read_text(encoding="utf-8")))
research_codes = [x["code"] for x in research]
initial_by_code = {x["code"]: x for x in initial}
assert len(initial) == len(normalized_rows) == len(research) == 53
assert len(set(research_codes)) == 53
assert [x["code"] for x in initial] == research_codes
assert set(normalized) == set(research_codes)

D = lambda value: Decimal(str(value))
def pct(value, total):
    if value is None or not total:
        return None
    return float((D(value) / D(total) * D(100)).quantize(D("0.01"), rounding=ROUND_HALF_UP))
def rnd(value, places="0.000001"):
    return float(D(value).quantize(D(places), rounding=ROUND_HALF_UP))

# 仅对七家公司修正初审理由中的四舍五入数字；其余文字完整保留。
replacements = {
    "688126": [("约占65.63%", "约占65.62%")],
    "688268": [("约占总收入21.99%", "约占总收入22.00%")],
    "688300": [("约占58.44%", "约占58.41%"), ("约占23.41%", "约占23.40%"), ("约占18.08%", "约占18.07%")],
    "688368": [("约占49.55%", "约占49.54%"), ("约占18.84%", "约占18.83%")],
    "688383": [("约占73.54%", "约占73.57%")],
    "688385": [("约占21.46%", "约占21.47%")],
    "688545": [("约14.7476亿元的84.60%", "约14.7498亿元的84.58%")],
}
updated_reasons = {}
for code, reps in replacements.items():
    text = initial_by_code[code]["reason"]
    for old, new in reps:
        assert old in text, (code, old)
        text = text.replace(old, new)
    updated_reasons[code] = text

updated_reasons["688187"] = (
    "公司主体仍是轨道交通装备，功率半导体属于新兴装备板块中的规模化业务，真实产品包括IGBT、SiC器件及功率模块，"
    "覆盖轨交、新能源汽车和电网等场景，并非仅有技术储备。2025年营业收入287.027001亿元，其中轨道交通装备158.055202亿元、"
    "约占55.07%，新兴装备127.805376亿元、约占44.53%；年报披露半导体板块收入53.60亿元、约占总收入18.67%，规模较大但仍非公司主营多数，"
    "故维持“相关”而非因金额大就升级核心。年报同时明确公司是同时掌握大功率晶闸管、IGBT、IGCT、SiC器件及其组件技术的IDM模式企业代表，"
    "拥有“芯片—模块—装置—系统”完整产业链，并建有6英寸双极器件、8英寸IGBT和8英寸SiC产业化基地，因此在保留“芯片设计-功率半导体”的同时补充“晶圆制造与IDM”。"
    "该标签只对应功率半导体产业链，不把新兴装备或轨交系统收入全部半导体化。"
)

boundary_notes = {
    "688138": "掩膜版下游同时包括显示、半导体、触控和电路板；年报单列显示9.78亿元、半导体2.04亿元，维持相关，不能把掩膜版行业收入全部归入半导体。",
    "688187": "半导体53.60亿元为年报补充口径；轨交仍为主业，维持相关并补充IDM，新兴装备收入不得全部归入半导体。",
    "688268": "光刻及其他混合气体是产品口径，特种气体还服务多元下游，不能把特种气体全部视作半导体收入。",
    "688300": "球形、角形粉体同时用于封装与PCB等场景，年报未单列半导体收入，维持相关。",
    "688383": "固晶机混合半导体、LED及新型显示下游，73.57%是固晶机占总收入比例，不是半导体设备占比。",
    "688392": "半导体超声波设备0.963799亿元、占12.45%；动力电池、线束设备及配件不得并入半导体。",
    "688401": "年报未单列半导体掩膜版收入，显示仍为主导，维持相关；掩膜版行业收入不能全部归入半导体。",
    "688503": "正面银浆144.221903亿元、占总收入约98.83%；电子专用材料为含光伏的宽口径，空白掩膜版尚未形成可识别规模收入，维持摘除。",
    "688516": "半导体设备1.459990亿元、占2.28%，光伏设备44.435720亿元，维持相关。",
    "688521": "一站式芯片定制及IP授权服务，归类EDA/IP与设计服务，不把量产业务写成自有端侧AI或GPU芯片销售。",
    "688525": "存储模组与系统为核心并保留先进封测；模组收入不等同自主DRAM晶圆设计制造。",
    "688610": "半导体检测应用真实，但年报未单列半导体收入，电子制造、显示、新能源等收入不得全部半导体化。",
    "688630": "泛半导体系列2.334194亿元、占16.58%，PCB系列10.799278亿元、占76.69%；不得写成晶圆前道光刻机。",
    "688665": "半导体设备配套传感器已批量供货，但气体传感器和分析仪器下游混合且未单列半导体收入，维持相关。",
    "688691": "一站式芯片定制服务，归类EDA/IP与设计服务，不是自有芯片产品销售。",
    "688795": "GPU设计核心；云端板卡、智算一体机及集群等系统收入不得全部表述为GPU裸片收入，不追加晶圆制造。",
    "688802": "GPU设计核心；GPU产品及配件口径含配件和系统，不全部写成GPU裸片，不追加晶圆制造。",
    "688825": "自主DRAM设计制造IDM；无行业分部时以产品分部合计为总收入，不能与存储模组公司混淆。",
    "688396": "芯片设计、晶圆制造、封装测试一体化IDM，维持核心。",
    "600745": "半导体与产品集成业务并存，按真实IDM业务保留核心，不把智能终端收入并入半导体。",
    "688469": "真实业务为晶圆代工及模组封测；“晶圆制造与IDM”为白名单合并概念，维持核心。",
    "688147": "半导体设备8.808926亿元、占33.45%，光伏设备15.912888亿元、占60.43%，维持相关。",
}
change_notes = {
    "688126": "300mm半导体硅片占总收入复算为65.623381%，两位小数应为65.62%，仅修正初审尾数。",
    "688268": "光刻及其他混合气体占总收入复算为21.996392%，两位小数应为22.00%，仅修正初审尾数。",
    "688300": "按总收入11.155035亿元复算，球形、角形和其他产品分别应为58.41%、23.40%、18.07%。",
    "688368": "按总收入15.698114亿元复算，LED驱动和AC/DC分别应为49.54%、18.83%。",
    "688383": "固晶机5.347210亿元除以总收入7.268498亿元为73.57%，仅修正占比，混合下游边界不变。",
    "688385": "安全与识别芯片8.548617亿元除以总收入39.822611亿元为21.47%，仅修正占比。",
    "688545": "行业分部合计14.749814亿元，集成电路12.475817亿元占84.58%，修正总收入及占比。",
    "688187": "年报明确IDM模式、完整产业链及三类产业化基地，补充IDM标签；半导体占18.67%，轨交仍为主业，定级不变。",
}

company_review = []
for row in initial:
    code = row["code"]
    verdict = row["verdict"]
    if code == "688187":
        decision = "维持相关并补充IDM标签"
    elif code in replacements:
        decision = "维持定级并修正数字"
    else:
        decision = f"维持{verdict}"
    company_review.append({
        "code": code,
        "name": row["name"],
        "initial_verdict": verdict,
        "recommended_verdict": verdict,
        "decision": decision,
        "rationale": updated_reasons.get(code, row["reason"]),
    })

numeric_findings = []
for row in initial:
    code = row["code"]
    data = normalized[code]
    industry_raw = data.get("industry") or []
    product_raw = data.get("product") or []
    industry_valid = [x for x in industry_raw if x.get("revenue_yi") is not None]
    product_valid = [x for x in product_raw if x.get("revenue_yi") is not None]
    industry_total = sum((D(x["revenue_yi"]) for x in industry_valid), D(0))
    product_total = sum((D(x["revenue_yi"]) for x in product_valid), D(0))
    basis = "行业分部合计" if industry_valid else "产品分部合计（无行业分部）"
    total = industry_total if industry_valid else product_total
    assert total > 0, code
    industry_segments = [
        {"name": x["name"], "revenue_yi": x.get("revenue_yi"), "share_of_total_pct": pct(x.get("revenue_yi"), total)}
        for x in industry_raw
    ]
    product_segments = [
        {"name": x["name"], "revenue_yi": x.get("revenue_yi"), "share_of_total_pct": pct(x.get("revenue_yi"), total)}
        for x in product_raw
    ]
    item = {
        "code": code,
        "name": row["name"],
        "financial_period": "2025年度",
        "main_business": data.get("main_business", ""),
        "products": data.get("products", ""),
        "reported_composition": data.get("composition", ""),
        "revenue_basis": basis,
        "2025_total_revenue_yi": rnd(total),
        "industry_segments": industry_segments,
        "industry_total_yi": rnd(industry_total),
        "industry_reconciliation_difference_yi": rnd(industry_total - total),
        "product_segments": product_segments,
        "product_total_yi": rnd(product_total),
        "product_reconciliation_difference_yi": rnd(product_total - total),
        "boundary_note": boundary_notes.get(code, change_notes.get(code, "按初审公告证据及iFinD主营、行业和产品分部复核；仅按可识别业务口径归属，维持初审定级。")),
        "source": "[Source: iFinD — 股票主营构成（行业/产品）, as of 2025-12-31；本地拉取于2026-10-27]",
    }
    if code == "688138":
        item["annual_report_supplemental_segments"] = [
            {"name": "平板显示掩膜版", "revenue_yi": 9.78, "share_of_total_pct": pct(9.78, total)},
            {"name": "半导体芯片掩膜版", "revenue_yi": 2.04, "share_of_total_pct": pct(2.04, total)},
        ]
        item["supplemental_source"] = {
            "evidence_title": row["evidence_title"], "evidence_date": row["evidence_date"], "evidence_url": row["evidence_url"]
        }
    if code == "688187":
        item["annual_report_supplemental_segments"] = [
            {"name": "半导体板块", "revenue_yi": 53.60, "share_of_total_pct": pct(53.60, total)}
        ]
        item["supplemental_source"] = {
            "evidence_title": row["evidence_title"], "evidence_date": row["evidence_date"], "evidence_url": row["evidence_url"]
        }
    numeric_findings.append(item)

# 对摘要中已确定的七项修正及时代电气IDM结论作独立算术断言。
num = {x["code"]: x for x in numeric_findings}
def product_share(code, name):
    return next(x["share_of_total_pct"] for x in num[code]["product_segments"] if x["name"] == name)
assert num["688126"]["2025_total_revenue_yi"] == 37.160301 and product_share("688126", "300mm半导体硅片") == 65.62
assert product_share("688268", "光刻及其他混合气体") == 22.00
assert product_share("688300", "球形无机粉体") == 58.41 and product_share("688300", "角形无机粉体") == 23.40 and product_share("688300", "其他产品") == 18.07
assert product_share("688368", "LED照明驱动芯片") == 49.54 and product_share("688368", "AC/DC电源芯片") == 18.83
assert product_share("688383", "固晶机") == 73.57
assert product_share("688385", "安全与识别芯片") == 21.47
assert num["688545"]["2025_total_revenue_yi"] == 14.749814
assert next(x["share_of_total_pct"] for x in num["688545"]["industry_segments"] if x["name"] == "集成电路") == 84.58
assert num["688187"]["annual_report_supplemental_segments"][0]["share_of_total_pct"] == 18.67

recommended_changes = []
change_order = [x["code"] for x in initial if x["code"] in updated_reasons]
for code in change_order:
    row = initial_by_code[code]
    evidence = {
        "evidence_title": row["evidence_title"],
        "evidence_date": row["evidence_date"],
        "evidence_url": row["evidence_url"],
    }
    if code == "688187":
        new_pairs = [{
            "category": "半导体",
            "concepts": ["芯片设计-功率半导体", "晶圆制造与IDM"],
        }]
        recommended_changes.append({
            "code": code, "name": row["name"], "field": "pairs",
            "from": row["pairs"], "to": new_pairs,
            "reason": "2025年年报明确公司为IDM模式企业代表，拥有芯片—模块—装置—系统完整产业链及6英寸双极、8英寸IGBT、8英寸SiC产业化基地，需补充晶圆制造与IDM概念。",
            **evidence,
        })
    recommended_changes.append({
        "code": code, "name": row["name"], "field": "reason",
        "from": row["reason"], "to": updated_reasons[code],
        "reason": change_notes[code],
        **evidence,
    })
assert len(recommended_changes) == 9
assert len({x["code"] for x in recommended_changes}) == 8

consistency_findings = [
    {"code": code, "name": initial_by_code[code]["name"], "finding": change_notes[code]}
    for code in change_order
]
for code in ["688503", "688138", "688401", "688147", "688516", "688392", "688630", "688521", "688691", "688665", "688610", "688525", "688825", "688795", "688802", "688396", "600745", "688469"]:
    consistency_findings.append({"code": code, "name": initial_by_code[code]["name"], "finding": boundary_notes[code]})

initial_counts = Counter(x["initial_verdict"] for x in company_review)
recommended_counts = Counter(x["recommended_verdict"] for x in company_review)
assert initial_counts == recommended_counts == Counter({"核心": 38, "相关": 14, "摘除": 1})
assert sum(x["initial_verdict"] != x["recommended_verdict"] for x in company_review) == 0

result = {
    "reviewed_at": "2026-10-27",
    "scope": {
        "groups": ["半导体第5组", "半导体第6组"],
        "company_count": 53,
        "group_counts": [27, 26],
        "review_type": "独立二次复审",
        "inputs_read_only": True,
        "primary_financial_period": "2025年度；iFinD分部数据参数为20251231",
        "method": "按第5、6组顺序逐家复核初审理由、概念和定级；以iFinD行业分部合计作为总收入，无行业分部时采用产品分部合计，复算全部收入占比，并对关键边界核对初审所列公告。",
        "source_files": [str(INPUT), str(NORMALIZED)] + [str(x) for x in RESEARCH_FILES],
    },
    "company_review": company_review,
    "numeric_findings": numeric_findings,
    "consistency_findings": consistency_findings,
    "recommended_changes": recommended_changes,
    "summary": {
        "reviewed_companies": 53,
        "verdict_unchanged_companies": 53,
        "verdict_changed_companies": 0,
        "initial_verdict_counts": dict(initial_counts),
        "recommended_verdict_counts": dict(recommended_counts),
        "concept_changed_companies": 1,
        "reason_corrected_companies": 8,
        "numeric_corrected_companies": 7,
        "companies_with_field_changes": 8,
        "field_change_count": 9,
        "fully_unchanged_companies": 45,
        "changed_company_codes": change_order,
        "key_conclusion": "53家公司定级全部维持：核心38家、相关14家、摘除1家。时代电气补充晶圆制造与IDM概念；7家公司仅修正数字或四舍五入，时代电气同步重写理由，共9条字段建议、涉及8家公司。",
        "data_sources": "初审及研究输入只读；iFinD公司资料与2025年度主营构成分部；初审所列公司年报、投资者关系活动记录等公开公告。",
        "read_only_declaration": "未修改原初审、研究输入或watchlist；recommended_changes仅为后续人工应用建议。",
        "classification_principle": "混合收入不可全部归入半导体；按真实产品、可识别收入规模、主营地位及混合下游边界综合判断，不以单一收入比例机械升降级。",
    },
}

OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({
    "output": str(OUTPUT),
    "company_review": len(company_review),
    "numeric_findings": len(numeric_findings),
    "recommended_changes": len(recommended_changes),
    "changed_companies": len(change_order),
    "verdict_counts": dict(recommended_counts),
}, ensure_ascii=False, indent=2))
