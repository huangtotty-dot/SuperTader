# -*- coding: utf-8 -*-
"""GP 正式搜索 · Automation 一次性运行器（2026-09-19）

以子进程方式调用 gp_miner.py CLI 跑一轮正式 GP 因子搜索。
由临时 manual Automation 承载（Bash 300s 上限装不下 2~6h 的任务）。
runInput: {"seed": 0}；跑完即删 Automation，不占任务额度。
产物：t_io/validation/factor_mining/results/gp_mine/ 台账（gp_miner 自写）。
"""
import json
import os
import subprocess
import sys
import time

# 注意：Automation code 运行时会把本文件复制到自动化自己的 workspace 执行，
# 因此 ROOT 不能按文件位置推导，硬编码到仓库根。
ROOT = r"E:\superTrader"


def run(ctx=None):
    sys.stdout.reconfigure(encoding="utf-8")
    seed = 0
    if isinstance(ctx, dict):
        seed = int(ctx.get("seed", 0))
    cmd = [sys.executable,
           os.path.join(ROOT, "t_io", "validation", "factor_mining", "gp_miner.py"),
           "--pop", "1000", "--gen", "40", "--seed", str(seed)]
    t0 = time.time()
    print(f"[gp_formal] seed={seed} 启动: {' '.join(cmd)}", flush=True)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=169200, cwd=ROOT, encoding="utf-8", errors="replace",
                              creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        rc, out, err = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        rc, out, err = -9, "", "subprocess timeout (169200s)"
    except Exception as e:  # noqa
        rc, out, err = -1, "", f"launcher error: {e}"
    el = time.time() - t0
    print(f"[gp_formal] seed={seed} 结束 rc={rc} 用时={el / 3600:.2f}h", flush=True)
    return {"artifact": {
        "seed": seed, "returncode": rc, "hours": round(el / 3600, 2),
        "stdout_tail": out[-3000:], "stderr_tail": err[-1500:],
        "ledger_dir": "t_io/validation/factor_mining/results/gp_mine/",
    }}


if __name__ == "__main__":
    # 本地直跑：python scripts/gp_formal_run.py [seed]
    _seed = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print(json.dumps(run({"seed": _seed}), ensure_ascii=False, indent=2)[:2000])
