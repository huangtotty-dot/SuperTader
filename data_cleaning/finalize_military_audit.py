# -*- coding: utf-8 -*-
"""合并四组初审，并纳入独立边界复审的三项修正。"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
rows = sum([json.loads((ROOT / f"military_audit_group{i}_20261027.json").read_text(encoding="utf-8")) for i in range(1, 5)], [])
by_code = {x["code"]: x for x in rows}
review = {x["code"]: x for x in json.loads((ROOT / "military_borderline_review_20261027.json").read_text(encoding="utf-8"))}
assert len(rows) == len(by_code) == 97

new_reasons = {
    "002111": "公司军工相关性来自真实历史军用空港装备、靶机和军品特种装备订单，而不是把民航地勤设备直接军工化。2024年官方投资者关系记录仍将军队、武警列为客户，2019年军方靶机及军品特种装备中标也证明已有军工交付；但2025年披露重点已转向民航空港、消防应急和民用无人机，空港地面设备收入21.0201亿元、消防设备10.7719亿元，军品收入未独立拆分，当前民用业务明显为主，故由初审摘除修正为相关而非核心。旧航空地面装备方向可保留，并补充军用无人装备与机器人，同时剔除全部空港收入皆属军工的错误理解。",
    "300589": "公司普通公务执法船、旅游船不能全部归为海军装备，但也不能忽视特殊批量公务船的真实军工属性。2023年报披露15.05亿元合同且合同内容涉及国家秘密，并具备武器装备质量管理体系认证；2025年报仍披露特殊批量公务船项目审价、延期违约及订单减少对经营的影响，说明该军工项目仍在履约链条中。2025年船艇销售收入6.6187亿元，其中公务执法3.8337亿元、旅游休闲1.8685亿元、特种作业0.9165亿元，军船收入未独立拆分且普通公务与民船较多，故由摘除修正为相关，概念统一为船舶总装与海洋防务，不再简单使用海军核心标签。",
    "688070": "公司虽以测绘、巡检、应急等工业无人机为主，2025年报却明确披露防务等重点行业中标多个重点项目、某军贸存量项目顺利交付，第一大客户中航技进出口有限责任公司销售额5756.27万元、占9.27%，研发项目亦明确军事情报侦察用途，因此存在真实军工交付而非单纯无人机军用联想。2025年工业无人机行业收入6.2092亿元，其中无人机系统4.3288亿元、服务0.6399亿元、配件0.6250亿元，民品和低空业务仍为主且军贸未完整独立拆分，故由摘除修正为相关，概念由工业无人机规范为军用无人装备与机器人，但不判核心。",
}
for code, reason in new_reasons.items():
    row = by_code[code]
    evidence = review[code]
    row.update({
        "action": "纠错",
        "pairs": [{"category": "军工", "concepts": evidence["suggested_concepts"]}],
        "verdict": evidence["suggested_verdict"],
        "reason": reason,
        "evidence_title": evidence["evidence_title"],
        "evidence_date": evidence["evidence_date"],
        "evidence_url": evidence["evidence_url"],
        "borderline_review": "2026-10-27二次复核修正",
    })

assert all(len(x["reason"]) >= 150 and x["evidence_url"] for x in rows)
assert all((x["verdict"] == "摘除") == (not x["pairs"]) for x in rows)
out = ROOT / "military_audit_20261027.json"
out.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print("总数", len(rows), "判词", dict(Counter(x["verdict"] for x in rows)))
print("摘除", [(x["code"], x["name"]) for x in rows if x["verdict"] == "摘除"])
print("二级概念", len({c for x in rows for p in x["pairs"] for c in p["concepts"]}))
print("最短理由", min(len(x["reason"]) for x in rows))
print(out)
