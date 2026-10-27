# -*- coding: utf-8 -*-
from pathlib import Path
ROOT = Path(r"E:\superTrader")
text = (ROOT / "stock_hunter" / "audit_semiconductor_deep_20261027.py").read_text(encoding="utf-8")
text = text.replace("semiconductor", "pcb").replace("半导体", "PCB")
text = text.replace("len(audit) == 161", "len(audit) == 89")
text = text.replace('MARK = "2026-10-27 13:46:00"', 'MARK = "2026-10-27 14:39:00"')
out = ROOT / "stock_hunter" / "audit_pcb_deep_20261027.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
