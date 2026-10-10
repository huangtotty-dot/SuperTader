
# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys, time
T0 = time.perf_counter()
def mark(msg): print(f"[{time.perf_counter()-T0:6.2f}s] {msg}", flush=True)
sys.path.insert(0, BASE)
import t_gui
mark("import t_gui 完成")
t_gui._start_ctrlc_abort_watchdog(grace_s=1.0, poll_s=0.2)
mark("看门狗已启动")
from webview.platforms import winforms as _wfw
mark("import winforms 完成")
_wfw._sigint_received = True
mark("已置 SIGINT 标志 ← 从这一刻起到退出应只花 ~1.2s")
time.sleep(6)
mark("❌ 仍存活，看门狗未触发")
sys.exit(3)
