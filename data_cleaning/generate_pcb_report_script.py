# -*- coding: utf-8 -*-
from pathlib import Path
ROOT = Path(r"E:\superTrader\data_cleaning")
text = (ROOT / "build_semiconductor_report.py").read_text(encoding="utf-8")
text = text.replace("semiconductor", "pcb").replace("半导体", "PCB")
text = text.replace("161只", "89只").replace("153只", "76只").replace("8只", "13只")
text = text.replace("核心111只、相关42只", "核心52只、相关24只").replace("43个", "23个")
start = text.index("notes = [")
end = text.index("\n]\nfor i, text in enumerate(notes, 23):", start) + 2
notes = '''notes = [
    "核心：PCB产业链是明确主营、重要收入来源，或已有规模化核心产品；相关：有真实PCB产品、订单、客户验证或交付，但早期、低占比、未拆分或其他行业为主。",
    "摘除：MLCC、SOFC、MPO本身不属于PCB；只有潜在用途的粉体、树脂、设备，以及没有独立PCB业务的被动元件公司均摘除。AI服务器相关按真实板型重标。",
    "二次复审：13只摘除结论全部维持；德福科技由相关上调核心，另补充12处真实产品标签并修正少量数字与理由。动作含纠错70只、保留6只、摘除13只。",
    "主营与2025分部：iFinD ifind_get_stock_info、ifind_get_stock_business_segmentation（2026-10-27）；逐股公告与证据见Source列。",
]'''
text = text[:start] + notes + text[end:]
out = ROOT / "build_pcb_report.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
