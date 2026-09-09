# -*- coding: utf-8 -*-
"""tools/preopen_check.py — 盘前 09:20 自检（F4, 2026-09-09，schtasks 每日调度）

外部冗余（gm_main 内 F3 已做，本脚本兜底）：auto 进程在否 / 心跳新鲜度 / 隔夜陈旧 pending 清理。
注意写权限纪律：BUY_PENDING.json/BUY_DECISION.json 引擎单写者——**仅在 auto 心跳过期(进程离线)时**才代清，
避免与运行中引擎并发写文件。

调度（管理员）：
    schtasks /create /tn "superTrader_preopen_check" /tr "python E:\\superTrader\\tools\\preopen_check.py" /sc daily /st 09:20
退出码：0=正常；2=发现异常/已清理陈旧 pending（供计划任务日志告警）；1=自身错误。
"""
import json
import os
import sys
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tools/ → 仓库根
BRIDGE = os.path.join(_ROOT, "t_io", "bridge")
HEARTBEAT = os.path.join(BRIDGE, "heartbeat.json")
PENDING = os.path.join(BRIDGE, "BUY_PENDING.json")
DECISION = os.path.join(BRIDGE, "BUY_DECISION.json")
_STALE_SECONDS = 150


def _log(msg: str) -> None:
    print("[preopen_check %s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def _hb_age() -> float:
    try:
        return datetime.now().timestamp() - os.path.getmtime(HEARTBEAT)
    except Exception:
        return float("inf")


def _clear_stale_files(today: str, reason: str) -> None:
    # 引擎单写者纪律：先原子清 BUY_PENDING.json（保 date=今日），再清 BUY_DECISION.json
    for path, payload in ((PENDING, {"date": today, "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                     "rejected_today": [], "pending": {}}),
                          (DECISION, {})):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            _log(f"清理失败 {os.path.basename(path)}: {e}")


def main() -> int:
    os.environ["NO_PROXY"] = "*"
    today = datetime.now().strftime("%Y-%m-%d")
    rc = 0
    age = _hb_age()
    online = age <= _STALE_SECONDS
    _log("auto 进程: %s（heartbeat 距今 %.0fs%s）" % ("在线" if online else "离线",
                                                    age, "" if os.path.exists(HEARTBEAT) else " 且无 heartbeat 文件"))
    if not online:
        rc = 2
        _log("⚠️ auto 心跳过期/缺失——盘中若在跑请人工确认；当前按离线处理")
    # 隔夜陈旧 pending 清理（离线才代清）
    try:
        if os.path.exists(PENDING):
            pend = json.load(open(PENDING, encoding="utf-8")) or {}
            stale = pend.get("date") != today and bool(pend.get("pending") or {})
            if stale:
                _log("发现隔夜陈旧 pending date=%s 共 %d 条 → 作废清空" %
                     (pend.get("date"), len(pend.get("pending") or {})))
                if not online:
                    _clear_stale_files(today, "offline_stale_pending")
                    rc = 2
                else:
                    _log("auto 在线——跳过代清（引擎 init F3 会自行作废）")
            else:
                _log("BUY_PENDING 无隔夜陈旧（date=%s）" % pend.get("date"))
    except Exception as e:
        _log("读 BUY_PENDING 失败: %s" % str(e)[:120])
    _log("完成 rc=%d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
