# -*- coding: utf-8 -*-
"""
diag_hang_603667.py — 单块复跑 ctl 603667 2026-05-11~15 + faulthandler 240s 栈转储
若挂起复现：stderr 打出卡死点全线程栈（定位网络/锁/循环）；若正常完成：一次性抖动。
"""
import faulthandler
import os
import runpy
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
OD = BASE / "t_io/validation/w32_c1/diag/ctl_603667_20260511"
OD.mkdir(parents=True, exist_ok=True)

faulthandler.dump_traceback_later(240, exit=True)

os.environ["T_SNAPSHOT_DIR"] = str(BASE / "t_io/minute_snapshots_ts")
os.environ["T_HOLDINGS_FILE"] = str(BASE / "t_io/validation/w32_c1/holdings_snapshot_3d80810.json")
os.environ["T_BUY_BONUS_MIN_SCORE"] = "36"
os.environ["T_NOTIFY_BUY"] = "36"
sys.argv = ["harness_backtest.py", "--codes", "603667",
            "--start", "2026-05-11", "--end", "2026-05-15",
            "--ab", "v102", "--out", str(OD)]
sys.path.insert(0, str(BASE))
os.chdir(str(BASE))
print("DIAG START", flush=True)
runpy.run_path(str(BASE / "harness_backtest.py"), run_name="__main__")
print("DIAG DONE NORMAL", flush=True)
