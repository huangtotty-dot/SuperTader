# -*- coding: utf-8 -*-
from pathlib import Path

ROOT = Path(r"E:\superTrader")
source = (ROOT / "stock_hunter" / "audit_optical_comm_deep_20261027.py").read_text(encoding="utf-8")
start = source.index("DESC = {")
end = source.index("\n}\n\n\ndef toks", start) + 2
desc = 'DESC = json.loads((ROOT / "data_cleaning" / "semiconductor_taxonomy_20261027.json").read_text(encoding="utf-8"))'
text = source[:start] + desc + source[end:]
text = text.replace("optical_comm", "semiconductor").replace("光通信", "半导体")
text = text.replace("len(audit) == 45", "len(audit) == 161")
text = text.replace('MARK = "2026-10-27 13:13:00"', 'MARK = "2026-10-27 13:46:00"')
out = ROOT / "stock_hunter" / "audit_semiconductor_deep_20261027.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
