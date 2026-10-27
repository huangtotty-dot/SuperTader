# -*- coding: utf-8 -*-
"""把iFinD主营和2025分部按股票整理成四组商业航天审计输入。"""
import csv
import json
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
rows = json.loads((ROOT / "commercial_space_current_20261027.json").read_text(encoding="utf-8"))
by_code = {r["code"]: r for r in rows}

def num(value):
    try:
        return round(float(value) / 100000000, 4)
    except (TypeError, ValueError):
        return None

for batch in range(1, 31):
    with (ROOT / "commercial_space_ifind_profiles" / f"profiles_{batch:02d}.csv").open(encoding="utf-8-sig", newline="") as f:
        for p in csv.DictReader(f):
            code = p["thscode"][:6]
            by_code[code]["ifind_profile"] = {
                "main_products": p.get("ths_mo_product_name_stock", ""),
                "main_business": p.get("ths_main_businuess_stock", ""),
                "controller": p.get("ths_actual_controller_stock", ""),
                "website": p.get("ths_corp_website_stock", ""),
            }
    with (ROOT / "commercial_space_ifind_segments" / f"segments_{batch:02d}.csv").open(encoding="utf-8-sig", newline="") as f:
        for s in csv.DictReader(f):
            code = s["thscode"][:6]
            rec = by_code[code]
            rec.setdefault("ifind_2025_segments", [])
            if not any(s.get(k) for k in ("ths_moc_project_name_industry_stock", "ths_moc_project_name_product_stock")):
                continue
            rec["ifind_2025_segments"].append({
                "industry_composition": s.get("ths_main_business_composition_stock", ""),
                "industry": s.get("ths_moc_project_name_industry_stock", ""),
                "industry_revenue_yi": num(s.get("ths_moc_project_income_industry_stock")),
                "industry_gross_margin_pct": s.get("ths_moc_project_gross_industry_stock", ""),
                "product": s.get("ths_moc_project_name_product_stock", ""),
                "product_revenue_yi": num(s.get("ths_moc_project_income_product_stock")),
                "product_gross_margin_pct": s.get("ths_moc_project_gross_rate_product_stock", ""),
            })

ordered = list(by_code.values())
sizes = [23, 23, 22, 22]
start = 0
for group, size in enumerate(sizes, 1):
    subset = ordered[start:start + size]
    start += size
    path = ROOT / f"commercial_space_research_group{group}_20261027.json"
    path.write_text(json.dumps(subset, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path, len(subset), subset[0]["code"], subset[-1]["code"])
assert start == 90 and all("ifind_profile" in r and "ifind_2025_segments" in r for r in ordered)
