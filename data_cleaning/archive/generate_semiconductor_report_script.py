# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
source = (ROOT / "build_optical_comm_report.py").read_text(encoding="utf-8")
text = source.replace("optical_comm", "semiconductor").replace("光通信", "半导体")
text = text.replace("45只", "161只").replace("42只", "153只").replace("3只", "8只")
text = text.replace("核心30只、相关12只", "核心111只、相关42只").replace("19个", "43个")
start = text.index("notes = [")
end = text.index("\n]\nfor i, text in enumerate(notes, 23):", start) + 2
notes = '''notes = [
    "核心：半导体是明确主营、重要收入来源，或已有规模化核心产品；相关：有真实半导体产品、订单、客户验证或交付，但早期、低占比、未拆分或其他行业为主。",
    "摘除：只有通用产品潜在用途、下游模组外购芯片、锂电或光伏设备联想、未落地投资；模组不写成芯片设计，分销不写成芯片，参股不等于自身经营。",
    "二次复审：奥普光电按真实参股恢复相关，致尚科技按半导体抛光设备恢复相关，合盛硅业按量产碳化硅衬底恢复相关；同步修正多家公司概念与数字口径。",
    "主营与2025分部：iFinD ifind_get_stock_info、ifind_get_stock_business_segmentation（2026-10-27）；逐股公告与证据见Source列。",
]'''
text = text[:start] + notes + text[end:]
out = ROOT / "build_semiconductor_report.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
