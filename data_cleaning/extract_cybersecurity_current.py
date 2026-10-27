# -*- coding: utf-8 -*-
"""提取网络安全现有标签及全部产业链上下文。"""
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"E:\superTrader")
SRC = ROOT / "stock_hunter" / "watchlist_jiuyan.json"
OUT = ROOT / "data_cleaning" / "cybersecurity_current_20261027.json"
data = json.loads(SRC.read_text(encoding="utf-8"))


def toks(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").split("|") if x.strip()]


rows = []
for code, rec in data.items():
    pairs = []
    matched = False
    for i in list(range(1, 10)) + [0]:
        suffix = "" if i == 0 else str(i)
        ck, nk = "jiuyan_category" + suffix, "jiuyan_concept" + suffix
        for cat in toks(rec.get(ck)):
            for concept in toks(rec.get(nk)):
                pairs.append({"category": cat, "concept": concept, "slot": i})
                matched = matched or cat == "网络安全"
    if not matched:
        continue
    reasons = rec.get("jiuyan_concept_reason") if isinstance(rec.get("jiuyan_concept_reason"), dict) else {}
    rows.append({
        "code": code,
        "name": rec.get("name"),
        "pairs": pairs,
        "verdict": rec.get("jiuyan_tag_verdict", ""),
        "reasons": reasons,
        "business_summary": rec.get("business_summary", ""),
        "sector": rec.get("sector", ""),
    })

OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"股票池：{len(data)}只；网络安全：{len(rows)}只；输出：{OUT}")
print("旧概念：", dict(Counter(p["concept"] for x in rows for p in x["pairs"] if p["category"] == "网络安全")))
print("跨分类：", dict(Counter(p["category"] for x in rows for p in x["pairs"] if p["category"] != "网络安全")))
print("重复槽位：", [(x["code"], x["name"]) for x in rows if sum(p["category"] == "网络安全" for p in x["pairs"]) > 1])
