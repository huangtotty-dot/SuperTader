# -*- coding: utf-8 -*-
"""网络安全概念深度清洗（2026-10-27）。默认dry-run，--apply才备份并写入。"""
import copy
import json
import shutil
import sys
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
AUDIT = ROOT / "data_cleaning" / "cybersecurity_audit_20261027.json"
CURRENT = ROOT / "data_cleaning" / "cybersecurity_current_20261027.json"
BAK = ROOT / "stock_hunter" / "watchlist_jiuyan.json.bak_20261027_cybersecurity_deep"
PREVIEW = ROOT / "data_cleaning" / "watchlist_jiuyan_cybersecurity_cleaned_20261027.json"
DATE = "2026-10-27"
MARK = "2026-10-27 10:19:00"

DESC = {
    "AI安全": "面向大模型、智能体及AI应用的安全评估、护栏、威胁检测和防护产品；须有正式产品或客户交付，普通AI应用不等同AI安全。",
    "DPI与流量智能": "以深度包检测、全流量采集和协议解析开展流量管理、异常识别、内容审计及安全防护。",
    "Web应用安全": "以WAF、API防护、漏洞检测等保护Web站点、接口和应用服务，须有正式产品或项目。",
    "专用安全终端与测控安全": "面向航空航天及专用场景的芯片、板卡、整机、检测台和测控链路安全防护。",
    "云安全": "保护云主机、容器、微服务及云边界的访问控制、微隔离、工作负载和云管理平台。",
    "云安全与CDN": "依托CDN和边缘节点提供DDoS、WAAP、Web及API安全；普通加速服务本身不等同安全业务。",
    "云安全与SASE": "将SD-WAN、零信任、云原生防火墙和安全访问整合为SASE服务，须有真实客户或订阅交付。",
    "保密安全": "面向党政、央国企及特殊行业的终端保密检查、隔离交换、敏感信息监测和电磁空间检测。",
    "内容安全与AI鉴伪": "利用AI识别深度合成、伪造音视频及违规内容，并提供标识、溯源或治理产品。",
    "内容安全与审核服务": "以人工与智能系统向外部平台提供文本、图片、音频、视频和直播内容审核服务。",
    "内容安全与智能审核": "以智能审核模型和播控治理系统服务文化执法、传媒或内容平台，须有实际项目或合同。",
    "内容安全与模型安全": "覆盖内容合规审核、生成式AI内容治理和大模型安全评估的综合平台。",
    "内容安全与深度合成鉴伪": "面向深度合成、AI反诈和伪造内容的识别、审计、溯源及防护产品。",
    "内容安全与舆情治理": "向政府、媒体和平台提供内容风控、全模态治理、舆情监测和合规服务。",
    "商用密码": "密码芯片、模块、整机、系统及密钥管理等商用密码基础设施和解决方案。",
    "商用密码与安全传输": "以密码、身份鉴别、防篡改和加扰技术保障专用网络及数据链路传输。",
    "安全托管与MDR": "以订阅或远程运营方式提供持续监测、研判、溯源和处置的托管检测响应服务。",
    "安全策略可视化": "跨厂商采集并解析安全策略，提供策略清理、合规审计、路径和攻击面可视化。",
    "安全运营与应急响应": "提供安全托管、驻场、应急响应和安全集成服务；普通IT运维不直接纳入。",
    "安全运营与托管服务": "通过安全运营中心、MSS或托管服务持续提供监测、研判、处置、咨询和运营。",
    "安全通信与移动办公": "面向党政军及政企的高安全即时通信、加密移动办公和终端协同产品。",
    "安全隔离与数据交换": "以网闸、单向光闸和边界监管实现跨域安全隔离及数据、视频交换。",
    "密码与身份认证": "基于PKI、数字证书、签名和认证网关提供身份信任、密码服务及电子认证。",
    "抗量子密码": "抗量子算法、密钥和PKI迁移产品及试点项目；应与现有商用密码主业区分。",
    "操作系统与云计算安全": "以高安全操作系统、存取控制、加密存储和安全云桌面构成的系统与云计算安全产品。",
    "数字水印与内容溯源": "将数字水印用于视音频版权保护、内容认证、泄密追踪和可信标识。",
    "数字版权保护与数字水印": "以DRM、条件接收、数字水印、版权监测和电子证据保护数字内容版权。",
    "数据安全": "覆盖数据分类分级、脱敏、防泄漏、审计、合规、隐私计算和跨域流动的安全产品。",
    "数据安全与云安全": "覆盖数据全生命周期防护及云网边、云工作负载和云数据安全的综合产品。",
    "漏洞与攻击面管理": "提供漏洞扫描、网络资产测绘、暴露面监测和攻击面持续管理。",
    "电子数据取证": "面向司法执法的手机、计算机和云端电子数据提取、固定、分析及取证平台。",
    "移动应用安全": "提供APP漏洞与隐私合规检测、安全加固、渠道监测和移动威胁感知。",
    "终端安全与威胁检测": "以终端管理、EDR、威胁检测、零信任和响应平台保护政企端点。",
    "网络内容与行为审计": "对网络内容、用户行为和合规状态进行审计、监管、反诈及威胁分析。",
    "网络内容安全": "面向政府、运营商等客户的网络内容监测、审计、识别和安全治理产品。",
    "网络可视化": "通过宽带网、移动网流量提取和解析构建网络可视化基础架构。",
    "网络安全综合平台": "覆盖边界、终端、检测、响应和运营的完整网络安全产品体系，而非单一产品或概念延伸。",
    "网络安全运营": "以安全运营中心及持续服务开展监测、预警、研判和处置。",
    "网络安全集成与运维": "面向涉密、信创等项目提供网络安全咨询、实施、信息运维及专业软件开发。",
    "网络空间安全": "面向执法和政企客户的网络空间探测、监测、防护及安全解决方案。",
    "网络设备与安全": "以防火墙、入侵防御、抗DDoS、WAF及应用交付设备构成网络与安全基础设施。",
    "网络靶场与安全测试": "以网络靶场、平行仿真和数字风洞开展攻防演练、测试评估及安全验证。",
    "身份与终端安全": "整合数字身份、认证准入、终端防护、EDR、DLP和零信任。",
    "身份认证与电子签名": "提供数字证书、可靠电子签名、电子合同、证据保全及身份认证服务。",
    "边界安全与防火墙": "以下一代防火墙、IDPS和边界访问控制保护网络边界。",
    "防火墙与零信任": "以防火墙、VPN、WAF和零信任访问构建边界与身份驱动的防护体系。",
    "零信任与SASE": "以零信任访问、SASE和SD-WAN融合提供身份驱动的安全连接。",
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


def remove_cybersecurity(rec):
    old = []
    for _, ck, nk in list(slots(rec)):
        cats, cons = toks(rec.get(ck)), toks(rec.get(nk))
        if "网络安全" not in cats:
            continue
        old.extend(("网络安全", con) for con in cons)
        keep = [cat for cat in cats if cat != "网络安全"]
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
        before_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "网络安全"]
        old_pairs = remove_cybersecurity(rec)
        for pair in row["pairs"]:
            add_or_merge(rec, pair["category"], pair["concepts"])
        new_pairs = [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]
        after_other = [(cat, con) for cat, con in active_pairs(rec) if cat != "网络安全" and (cat, con) not in new_pairs]
        assert before_other == after_other, (row["code"], before_other, after_other)
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

        if any(cat == "网络安全" for cat, _ in new_pairs):
            rec["jiuyan_cybersecurity_verdicts"] = {"网络安全": row["verdict"]}
            rec["jiuyan_tag_verdict"] = row["verdict"]
            rec["jiuyan_tag_evidence"] = "text"
        else:
            rec.pop("jiuyan_cybersecurity_verdicts", None)
            if not active_pairs(rec):
                rec.pop("jiuyan_tag_verdict", None)
                rec.pop("jiuyan_tag_evidence", None)
        rec["jiuyan_cybersecurity_audited_at"] = DATE
        rec["jiuyan_cybersecurity_evidence"] = {
            "title": row["evidence_title"], "date": row["evidence_date"], "url": row["evidence_url"],
            "profile_source": "iFinD ifind_get_stock_info", "profile_asof": DATE,
        }
        rec["jiuyan_cybersecurity_audit_20261027"] = {
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
        got = [(cat, con) for cat, con in pairs if cat == "网络安全"]
        expected = [(p["category"], c) for p in row["pairs"] if p["category"] == "网络安全" for c in p["concepts"]]
        assert got == expected, (row["code"], got, expected)
        for cat, con in [(p["category"], c) for p in row["pairs"] for c in p["concepts"]]:
            assert (cat, con) in pairs
            assert data[row["code"]]["jiuyan_concept_reason"].get(con)
            assert data[row["code"]]["jiuyan_concept_desc"].get(con)

    PREVIEW.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print("动作统计：", dict(counts))
    print("判词统计：", dict(Counter(x["verdict"] for x in audit)))
    print("网络安全股池：", sum(x["verdict"] != "摘除" for x in audit))
    print("二级概念：", len(used_concepts))
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
