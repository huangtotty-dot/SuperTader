# -*- coding: utf-8 -*-
"""获取军工候选股2025年年报公告元数据，作为逐股证据入口。"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
TOOL = Path(r"C:\Users\Lenovo\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\kimi-code\home\plugins\managed\ifind\scripts\ifind_tool.py")
OUT = ROOT / "military_ifind_announcements"
OUT.mkdir(parents=True, exist_ok=True)
rows = json.loads((ROOT / "military_current_20261027.json").read_text(encoding="utf-8"))

def ticker(code):
    return code + (".SH" if code.startswith("6") else ".BJ" if code.startswith(("4", "8", "92")) else ".SZ")

batches = [rows[i:i + 3] for i in range(0, len(rows), 3)]

def fetch(item):
    idx, batch = item
    tickers = ",".join(ticker(x["code"]) for x in batch)
    path = OUT / f"announcements_{idx:02d}.csv"
    params = {"ticker": tickers, "start_date": "2026-01-01", "end_date": "2026-10-27", "file_path": str(path), "format": "json"}
    cmd = [sys.executable, str(TOOL), "call", "--api-name", "ifind_get_stock_announcement", "--params-json", json.dumps(params, ensure_ascii=False)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
        log = (proc.stdout or "") + (proc.stderr or "")
        return {"batch": idx, "tickers": tickers, "ok": proc.returncode == 0 and path.exists(), "file": str(path), "log": log[-4000:]}
    except Exception as exc:
        return {"batch": idx, "tickers": tickers, "ok": False, "file": str(path), "log": repr(exc)}

results = []
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(fetch, x) for x in enumerate(batches, 1)]
    for fut in as_completed(futures):
        result = fut.result()
        results.append(result)
        print("OK" if result["ok"] else "FAIL", result["batch"], result["tickers"], flush=True)
results.sort(key=lambda x: x["batch"])
(ROOT / "military_announcement_fetch_log.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("完成", sum(x["ok"] for x in results), "/", len(results))
