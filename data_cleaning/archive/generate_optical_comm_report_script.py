# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
source = (ROOT / "build_commercial_space_report.py").read_text(encoding="utf-8")
text = source.replace("commercial_space", "optical_comm").replace("商业航天", "光通信")
text = text.replace("90只", "45只").replace("74只", "42只").replace("16只", "3只")
text = text.replace("核心14只、相关60只", "核心30只、相关12只").replace("23个", "19个")
start = text.index("notes = [")
end = text.index("\n]\nfor i, text in enumerate(notes, 23):", start) + 2
notes = '''notes = [
    "核心：光通信是明确主营、重要收入来源，或已有规模化核心产品；相关：有真实光通信产品、订单、客户验证或交付，但早期、低占比、未拆分或其他行业为主。",
    "摘除：只有通用晶振、材料、零部件或设备的潜在用途，以及算力租赁、SOFC、MLCC、千帆星座、SpaceX等跨概念联想；参股不等于自身经营。",
    "二次复审：30只核心、12只相关和3只摘除均维持；修正亨通光电总收入数量级、长飞光纤光互联组件收入、太辰光总收入及占比三处数字。",
    "主营与2025分部：iFinD ifind_get_stock_info、ifind_get_stock_business_segmentation（2026-10-27）；逐股公告与证据见Source列。",
]'''
text = text[:start] + notes + text[end:]
out = ROOT / "build_optical_comm_report.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
