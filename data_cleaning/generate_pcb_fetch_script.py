# -*- coding: utf-8 -*-
from pathlib import Path
root = Path(r"E:\superTrader\data_cleaning")
text = (root / "fetch_semiconductor_ifind.py").read_text(encoding="utf-8")
text = text.replace("semiconductor", "pcb").replace("161只半导体", "89只PCB")
out = root / "fetch_pcb_ifind.py"
out.write_text(text, encoding="utf-8")
compile(text, str(out), "exec")
print(out)
