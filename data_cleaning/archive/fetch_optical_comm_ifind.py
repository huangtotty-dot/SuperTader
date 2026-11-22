# -*- coding: utf-8 -*-
"""批量获取45只光通信候选股的iFinD主营资料和2025年业务分部。"""
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(r"E:\superTrader\data_cleaning")
TOOL = Path(r"C:\Users\Lenovo\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\kimi-code\home\plugins\managed\ifind\scripts\ifind_tool.py")
PROFILE_DIR = ROOT / "optical_comm_ifind_profiles"
SEGMENT_DIR = ROOT / "optical_comm_ifind_segments"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
rows = json.loads((ROOT / "optical_comm_current_20261027.json").read_text(encoding="utf-8"))


def ticker(code):
    if code.startswith(("4", "8", "92")):
        return code + ".BJ"
    if code.startswith("6"):
        return code + ".SH"
    return code + ".SZ"


batches = [rows[i:i + 3] for i in range(0, len(rows), 3)]


def fetch(item):
    idx, batch = item
    tickers = ",".join(ticker(x["code"]) for x in batch)
    result = {"batch": idx, "tickers": tickers}
    jobs = [
        ("profile", "ifind_get_stock_info", PROFILE_DIR / f"profiles_{idx:02d}.csv", {"request_time": "2026-10-27"}),
        ("segment", "ifind_get_stock_business_segmentation", SEGMENT_DIR / f"segments_{idx:02d}.csv", {"financial_parameter": "20251231"}),
    ]
    for kind, api, path, extra in jobs:
        params = {"ticker": tickers, "file_path": str(path), "format": "json", **extra}
        cmd = [sys.executable, str(TOOL), "call", "--api-name", api, "--params-json", json.dumps(params, ensure_ascii=False)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=150)
            log = (proc.stdout or "") + (proc.stderr or "")
            result[kind] = {"ok": proc.returncode == 0 and path.exists(), "file": str(path), "log": log[-4000:]}
        except Exception as exc:
            result[kind] = {"ok": False, "file": str(path), "log": repr(exc)}
    return result


results = []
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(fetch, x) for x in enumerate(batches, 1)]
    for fut in as_completed(futures):
        result = fut.result()
        results.append(result)
        print(result["batch"], "profile", result["profile"]["ok"], "segment", result["segment"]["ok"], result["tickers"], flush=True)
results.sort(key=lambda x: x["batch"])
(ROOT / "optical_comm_ifind_fetch_log.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print("profiles", sum(x["profile"]["ok"] for x in results), "/", len(results))
print("segments", sum(x["segment"]["ok"] for x in results), "/", len(results))
