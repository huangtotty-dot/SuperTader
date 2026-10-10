"""验证：卡死的非守护 ThreadPoolExecutor worker 会不会把解释器退阻住。

模拟 facade._gm_pool 的处境：GM SDK 挂死在不可取消的调用里，池被 shutdown(wait=False) 弃掉。
python 的 atexit `_python_exit` 会 join 所有 worker 线程 ⇒ 主流程跑完也退不出去。
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import concurrent.futures as cf
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "stuck"

ex = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gm-call")
ex.submit(lambda: time.sleep(600))      # 挂死的 GM 调用：600s 不返回、不可取消
time.sleep(0.5)
ex.shutdown(wait=False)                 # 复刻 facade 超时后的弃池

print(f"mode={mode} 主流程已跑完，现在尝试退出…", flush=True)
if mode == "stuck":
    pass                                # 什么都不做 → 看 atexit join 会不会卡住
else:
    import os
    os._exit(0)                         # 强制退出 → 立刻死
