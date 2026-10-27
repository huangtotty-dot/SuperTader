import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
OUT = ROOT / "ifind_part3"
TOOL = Path(r"C:\Users\Lenovo\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\kimi-code\home\plugins\managed\ifind\scripts\ifind_tool.py")
research = json.loads((ROOT / "semiconductor_research_group5_20261027.json").read_text(encoding="utf-8"))
research += json.loads((ROOT / "semiconductor_research_group6_20261027.json").read_text(encoding="utf-8"))
codes = [x["code"] for x in research]
tickers = [f"{c}.{'SH' if c.startswith('6') else 'SZ'}" for c in codes]
results = []
for kind, api in [("profiles", "ifind_get_stock_info"), ("segments", "ifind_get_stock_business_segmentation")]:
    for i in range(0, len(tickers), 3):
        batch = tickers[i:i+3]
        tag = f"{i//3+1:02d}"
        params = {
            "ticker": ",".join(batch),
            "file_path": str(OUT / f"{kind}_{tag}.csv"),
            "format": "json",
        }
        if kind == "segments":
            params["financial_parameter"] = "20251231"
        else:
            params["request_time"] = "2026-10-27"
        pf = OUT / f"{kind}_{tag}_params.json"
        pf.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")
        cp = subprocess.run([sys.executable, str(TOOL), "call", "--api-name", api, "--params-file", str(pf)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        rec = {"kind": kind, "batch": tag, "tickers": batch, "returncode": cp.returncode, "stdout": cp.stdout, "stderr": cp.stderr}
        results.append(rec)
        (OUT / f"{kind}_{tag}_result.txt").write_text(cp.stdout + ("\nSTDERR:\n" + cp.stderr if cp.stderr else ""), encoding="utf-8")
        print(kind, tag, cp.returncode, len(cp.stdout), cp.stderr[:160], flush=True)
(OUT / "fetch_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("calls", len(results), "failed", sum(x["returncode"] != 0 for x in results))
