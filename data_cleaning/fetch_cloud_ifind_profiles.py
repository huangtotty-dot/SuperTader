# -*- coding: utf-8 -*-
"""批量获取云计算候选股的iFinD主营资料。"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
TOOL = Path(r"C:\Users\Lenovo\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\kimi-code\home\plugins\managed\ifind\scripts\ifind_tool.py")
OUT = ROOT / "cloud_ifind_profiles"
OUT.mkdir(parents=True, exist_ok=True)
rows = json.loads((ROOT / "cloud_current_20261027.json").read_text(encoding="utf-8"))


def ticker(code):
    if code.startswith(("4", "8", "92")):
        return code + ".BJ"
    if code.startswith("6"):
        return code + ".SH"
    return code + ".SZ"


batches = [rows[i:i + 3] for i in range(0, len(rows), 3)]


def fetch(item):
    idx, batch = item
    path = OUT / f"profiles_{idx:02d}.csv"
    tickers = ",".join(ticker(x["code"]) for x in batch)
    params = {"ticker": tickers, "request_time": "2026-10-27", "file_path": str(path), "format": "json"}
    cmd = [sys.executable, str(TOOL), "call", "--api-name", "ifind_get_stock_info", "--params-json", json.dumps(params, ensure_ascii=False)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    log = (proc.stdout or "") + (proc.stderr or "")
    return {"batch": idx, "tickers": tickers, "ok": proc.returncode == 0 and path.exists(), "file": str(path), "log": log[-3000:]}


results = []
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(fetch, x) for x in enumerate(batches, 1)]
    for fut in as_completed(futures):
        try:
            result = fut.result()
        except Exception as exc:
            result = {"ok": False, "log": repr(exc)}
        results.append(result)
        print(("OK" if result.get("ok") else "FAIL"), result.get("batch"), result.get("tickers", ""), flush=True)
results.sort(key=lambda x: x.get("batch", 999))
(ROOT / "cloud_ifind_fetch_log.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"完成 {sum(bool(x.get('ok')) for x in results)}/{len(results)} 批")
