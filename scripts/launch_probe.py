# -*- coding: utf-8 -*-
"""滑点探针启动器（一次性 Automation 承载，2026-09-29 盘前）。

流程：前置检查（掘金终端进程 / 探针未重复启动）→ 后台拉起 probe_slippage.py →
90 秒存活验证 → 结果落 t_io/validation/slippage_probe/launch_report.json。
probe 进程脱离本启动器常驻（设计如此：常驻跨日、state.json 续跑、跑满 10 做T日自退）。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

ROOT = r"E:\superTrader"
PROBE = os.path.join(ROOT, "execution", "auto", "probe_slippage.py")
OUT_DIR = os.path.join(ROOT, "t_io", "validation", "slippage_probe")
LOG = os.path.join(OUT_DIR, "probe_stdout.log")
REPORT = os.path.join(OUT_DIR, "launch_report.json")

report = {"ts": datetime.now().isoformat(timespec="seconds"), "steps": []}


def step(name, ok, detail):
    report["steps"].append({"step": name, "ok": ok, "detail": detail})
    print(f"[{'OK' if ok else 'FAIL'}] {name}: {detail}")


# 1) 掘金终端进程检查
r = subprocess.run(
    ["C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command",
     "Get-Process gsgm3,gmterm-serv -ErrorAction SilentlyContinue | Select-Object Name,Id,StartTime | ConvertTo-Json"],
    capture_output=True, text=True, timeout=30)
procs = r.stdout.strip()
step("掘金终端在线", bool(procs and procs != ""), procs[:300] or "未发现 gsgm3/gmterm-serv 进程")

# 2) 探针未重复启动
r2 = subprocess.run(
    ["C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe", "-NoProfile", "-Command",
     "Get-CimInstance Win32_Process | Where-Object {$_.CommandLine -like '*probe_slippage*'} | Select-Object ProcessId | ConvertTo-Json"],
    capture_output=True, text=True, timeout=30)
already = "ProcessId" in (r2.stdout or "")
step("探针未在跑", not already, r2.stdout.strip()[:200] if already else "无 probe_slippage 进程")

if report["steps"][0]["ok"] and report["steps"][1]["ok"]:
    os.makedirs(OUT_DIR, exist_ok=True)
    logf = open(LOG, "a", encoding="utf-8")
    # 不降优先级：探针的 09:31 委托时效就是测量对象本身
    p = subprocess.Popen([sys.executable, PROBE], stdout=logf, stderr=subprocess.STDOUT,
                         cwd=ROOT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    report["pid"] = p.pid
    step("探针拉起", True, f"pid={p.pid} log={LOG}")
    time.sleep(90)
    alive = p.poll() is None
    step("90秒存活验证", alive, "进程存活" if alive else f"进程已退出 code={p.returncode}，看 {LOG}")
    state_fp = os.path.join(OUT_DIR, "state.json")
    step("state.json 落盘", os.path.exists(state_fp), state_fp if os.path.exists(state_fp) else "尚未生成（init 阶段可能延迟）")
else:
    step("探针拉起", False, "前置检查未过，未启动")

report["conclusion"] = "探针已启动" if report.get("pid") and report["steps"][-2].get("ok") else "未启动，需人工排查"
with open(REPORT, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(json.dumps({"artifact": {"report": REPORT, "conclusion": report["conclusion"],
                               "pid": report.get("pid"),
                               "steps": [f"{'OK' if s['ok'] else 'FAIL'} {s['step']}" for s in report["steps"]]}},
                 ensure_ascii=False))
