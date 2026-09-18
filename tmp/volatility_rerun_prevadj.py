# -*- coding: utf-8 -*-
"""临时复跑入口：波动选股因子包在前复权面板上的全历史体检（MC n=200）。

由临时 manual Automation 调用；跑完即删。结果写入
t_io/validation/factor_mining/results/volatility_screen_2026-09-18_prevadj/

注意：Automation 会把本脚本复制到其 assets 目录执行，__file__ 不一定在
E:\\superTrader 下，因此用标记文件向上查找工作区根，兜底绝对路径。
"""
import json
import sys
import time
from pathlib import Path


def find_root() -> Path:
    marker = Path("t_io") / "validation" / "factor_mining" / "volatility_screen.py"
    here = Path(__file__).resolve()
    for p in [here, *here.parents]:
        if (p / marker).exists():
            return p
    cwd = Path.cwd()
    for p in [cwd, *cwd.parents]:
        if (p / marker).exists():
            return p
    return Path(r"E:\superTrader")


ROOT = find_root()
sys.path.insert(0, str(ROOT / "t_io" / "validation" / "factor_mining"))
sys.stdout.reconfigure(encoding="utf-8")
print(f"[root] {ROOT}", flush=True)

import volatility_screen

t0 = time.time()
volatility_screen.main(["--tag=prevadj"])
elapsed = time.time() - t0

out_dir = ROOT / "t_io" / "validation" / "factor_mining" / "results" / "volatility_screen_2026-09-18_prevadj"
checks = sorted(p.name for p in out_dir.glob("check_*.json"))
pool = out_dir / "top100_pool.csv"

# AutomationOutput 契约：{"artifact": {...}}
print(json.dumps({
    "artifact": {
        "elapsed_sec": round(elapsed, 1),
        "out_dir": str(out_dir),
        "check_files": checks,
        "top100_exists": pool.exists(),
        "ok": len(checks) == 7 and pool.exists(),
    }
}, ensure_ascii=False))
