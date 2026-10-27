import csv, json
from pathlib import Path
ROOT=Path(r"E:\superTrader\data_cleaning")
OUT=ROOT/"ifind_part3"
research=json.loads((ROOT/"semiconductor_research_group5_20261027.json").read_text(encoding="utf-8"))+json.loads((ROOT/"semiconductor_research_group6_20261027.json").read_text(encoding="utf-8"))
profiles={}
segments={}
for p in sorted(OUT.glob("profiles_[0-9][0-9].csv")):
    with p.open(encoding="utf-8-sig",newline="") as f:
        for r in csv.DictReader(f):
            if r.get("thscode"): profiles[r["thscode"]]=r
for p in sorted(OUT.glob("segments_[0-9][0-9].csv")):
    if p.name=="segments_test.csv": continue
    with p.open(encoding="utf-8-sig",newline="") as f:
        for r in csv.DictReader(f):
            c=r.get("thscode")
            if c: segments.setdefault(c,[]).append(r)
def num(v):
    try:return float(v)
    except:return None
def yi(v):
    n=num(v)
    return None if n is None else round(n/1e8,6)
all_data=[]
lines=[]
for x in research:
    t=x["code"]+(".SH" if x["code"].startswith("6") else ".SZ")
    pr=profiles.get(t,{})
    rs=segments.get(t,[])
    comp=next((r.get("ths_main_business_composition_stock") for r in rs if r.get("ths_main_business_composition_stock")),"")
    ind=[]; prod=[]
    for r in rs:
        if r.get("ths_moc_project_name_industry_stock"):
            ind.append({"name":r["ths_moc_project_name_industry_stock"],"revenue_yi":yi(r.get("ths_moc_project_income_industry_stock"))})
        if r.get("ths_moc_project_name_product_stock"):
            prod.append({"name":r["ths_moc_project_name_product_stock"],"revenue_yi":yi(r.get("ths_moc_project_income_product_stock"))})
    rec={"code":x["code"],"name":x["name"],"ticker":t,"main_business":pr.get("ths_main_businuess_stock",""),"products":pr.get("ths_mo_product_name_stock",""),"composition":comp,"industry":ind,"product":prod}
    all_data.append(rec)
    lines.append(f"## {x['code']} {x['name']}\n主营：{rec['main_business']}\n产品：{rec['products']}\n构成：{comp}\n行业："+json.dumps(ind,ensure_ascii=False)+"\n产品分部："+json.dumps(prod,ensure_ascii=False)+"\n")
(OUT/"ifind_part3_normalized.json").write_text(json.dumps(all_data,ensure_ascii=False,indent=2),encoding="utf-8")
(OUT/"ifind_part3_summary.md").write_text("\n".join(lines),encoding="utf-8")
print(len(all_data),len(profiles),len(segments))
