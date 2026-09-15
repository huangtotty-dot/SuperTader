# coding=utf-8
"""
scripts/auto_process_guardian.py — 阶段0-3(2026-09-15 诊断D3-A) 策略进程守护

背景（D3 诊断）：策略进程静默死亡/冻结 3 次 / 11 个交易日（27%），均无 traceback：
  09-09 09:42 断（心跳缺口 777s）；09-09 14:45 发出买入确认后死亡；09-11 14:50 TAIL 下单后冻结。
既有 watcher（gm_bridge/watcher.py）只告警不拉起；本守护补"自动拉起"这一半。

行为：
  每 CHECK_INTERVAL_SEC(30s) 检查 t_io/bridge/heartbeat.json 刷新时间：
  - 心跳停顿 > HEARTBEAT_STALE_SEC(180s)（且处于交易时段）→ 先尝试重启策略进程
    （若策略进程仍存活=冻结态，先 taskkill 再拉起，防双实例重复下单）；
  - 重启后 RESTART_GRACE_SEC(240s) 内心跳未恢复记 1 次失败；连续 MAX_RESTART_FAIL(2)
    次失败 → 进入"只告警不拉起"态（防拉起风暴）；
  - t_io/bridge/KILL_SWITCH 存在 → 只发飞书告警，绝不拉起（人工急停语义优先）。
  守护自身日志写 t_io/logs/guardian_YYYYMMDD.log（时间戳行文本，同 daily_review 等现有日志结构）。

使用：
  python scripts/auto_process_guardian.py          # 前台运行（建议开机/盘前启动）
环境变量（测试/隔离用，生产不设）：
  GUARDIAN_BRIDGE_DIR / GUARDIAN_LOG_DIR  覆盖桥/日志目录
  GUARDIAN_DRY_RUN=1                      只巡检留痕，不 taskkill、不拉起、不发飞书
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, time as dtime

# ═══════════════ 可配置区（脚本头部，按需调整） ═══════════════
ST_ROOT = os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader")
BRIDGE_DIR = os.environ.get("GUARDIAN_BRIDGE_DIR") or os.path.join(ST_ROOT, "t_io", "bridge")
LOG_DIR = os.environ.get("GUARDIAN_LOG_DIR") or os.path.join(ST_ROOT, "t_io", "logs")
HEARTBEAT_PATH = os.path.join(BRIDGE_DIR, "heartbeat.json")
KILL_SWITCH_PATH = os.path.join(BRIDGE_DIR, "KILL_SWITCH")

CHECK_INTERVAL_SEC = 30        # 巡检间隔（任务书：每 30s）
HEARTBEAT_STALE_SEC = 180      # 心跳停顿 >3 分钟判死亡/冻结
RESTART_GRACE_SEC = 240        # 重启后等待心跳恢复的宽限（策略启动+盘前预取耗时，宽于停顿阈值）
MAX_RESTART_FAIL = 2           # 连续重启失败上限，达到后只告警不再拉起
ALERT_THROTTLE_SEC = 600       # 同类飞书告警节流（防告警轰炸）

# 策略进程启动命令——从现有启动方式推断：gm_main.py 的 __main__ 直接调 gm.api.run()
# （execution/auto/gm_main.py:2817-2823），生产由掘金终端内嵌 Python 或用户 Python 执行，
# 拉起模式仿 ops_guard.ensure_watcher（DETACHED_PROCESS + stdout 重定向到 logs/）。
# 若生产用专用 Python 解释器，把 "python" 改成绝对路径（如 r"C:\Users\Lenovo\...\python.exe"）。
STRATEGY_WORKDIR = os.path.join(ST_ROOT, "execution", "auto")
STRATEGY_CMD = ["python", "gm_main.py"]
KILL_FROZEN_BEFORE_RESTART = True   # 进程存活但心跳停（冻结）→ 先 taskkill 再拉起，防双实例

# 拉起时段闸：心跳只在实盘运行期刷新，非交易时段停顿属正常，绝不拉起
ONLY_TRADING_HOURS = True
TRADING_WINDOW = (dtime(9, 25), dtime(15, 10))   # 覆盖盘前预取(9:25 前 init)~收盘后末根 bar
WEEKDAYS_ONLY = True

_DRY_RUN = os.environ.get("GUARDIAN_DRY_RUN", "") == "1"

# ═══════════════ 飞书告警（复用 gm_bridge.feishu，与 watcher 同卡样式） ═══════════════
_GM_BRIDGE_DIR = os.path.join(ST_ROOT, "execution", "auto", "_gm", "gm_bridge")
if _GM_BRIDGE_DIR not in sys.path:
    sys.path.insert(0, _GM_BRIDGE_DIR)
_send_feishu = None
try:
    from feishu import send_feishu_payload as _send_feishu  # noqa: E501
    _FEISHU_OK = True
except Exception as _e:
    _FEISHU_OK = False
    _FEISHU_ERR = str(_e)

_state = {
    "consec_fail": 0,          # 连续重启失败计数
    "alert_only": False,       # True=只告警不拉起
    "pending_restart_ts": 0.0, # 最近一次拉起时刻（>0 表示等待心跳恢复确认）
    "spawned_proc": None,      # 本守护拉起的进程句柄（存活探查用）
    "last_alert": {},          # 告警节流 {key: ts}
}


# ── 基础工具 ──

def _now_dt():
    """当前时间（单独成函数便于离线测试注入假时钟）。"""
    return datetime.now()


def _log(msg):
    """守护自身日志：t_io/logs/guardian_YYYYMMDD.log（时间戳行文本，结构同现有日志）。"""
    line = "[%s] %s" % (_now_dt().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, "guardian_%s.log" % _now_dt().strftime("%Y%m%d"))
        with open(path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _push(title, content, level="info"):
    """飞书告警（watcher 同卡样式；DRY_RUN/飞书不可用 → 仅日志）。返回是否真正送达。"""
    key = "%s|%s" % (title, level)
    now_ts = time.time()
    if now_ts - _state["last_alert"].get(key, 0) < ALERT_THROTTLE_SEC:
        _log("[alert_throttled] %s" % title)
        return False
    _state["last_alert"][key] = now_ts
    _log("[ALERT:%s] %s — %s" % (level, title, content[:200]))
    if _DRY_RUN or not _FEISHU_OK:
        return False
    try:
        template = {"green": "green", "orange": "orange", "red": "red"}.get(level, "blue")
        card = {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True},
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": template},
                "elements": [{"tag": "markdown", "content": content}],
            },
        }
        return bool(_send_feishu(payload=card, success_log="", error_prefix="guardian",
                                 trigger_urgent_alarm_after_success=(level == "red")))
    except Exception as e:
        _log("[alert_failed] %s" % e)
        return False


def _heartbeat_age_sec():
    """心跳文件距上次刷新的秒数；文件不存在 → None。"""
    try:
        return time.time() - os.path.getmtime(HEARTBEAT_PATH)
    except Exception:
        return None


def _kill_switch_on():
    try:
        return os.path.exists(KILL_SWITCH_PATH)
    except Exception:
        return False


def _in_trading_window():
    if not ONLY_TRADING_HOURS:
        return True
    n = _now_dt()
    if WEEKDAYS_ONLY and n.weekday() >= 5:
        return False
    return TRADING_WINDOW[0] <= n.time() <= TRADING_WINDOW[1]


def _find_strategy_pids():
    """找命令行含 gm_main.py 的 python 进程（冻结探查用；排除本守护自身）。
    wmic 优先，失败回退 PowerShell CIM；都不可用 → []（当作无存活进程，不影响拉起链路）。"""
    pids = []
    self_pid = os.getpid()
    try:
        out = subprocess.check_output(
            ["wmic", "process", "where", "name='python.exe'", "get", "processid,commandline"],
            stderr=subprocess.DEVNULL, timeout=15)
        text = out.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if "gm_main.py" in line:
                parts = line.strip().split()
                if parts:
                    try:
                        pid = int(parts[-1])
                        if pid != self_pid:
                            pids.append(pid)
                    except ValueError:
                        pass
        return pids
    except Exception:
        pass
    try:
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")
        out = subprocess.check_output(["powershell", "-NoProfile", "-Command", ps],
                                      stderr=subprocess.DEVNULL, timeout=20)
        data = json.loads(out.decode("utf-8", errors="replace") or "[]")
        if isinstance(data, dict):
            data = [data]
        for p in data:
            cmd = str(p.get("CommandLine") or "")
            if "gm_main.py" in cmd:
                pid = int(p.get("ProcessId") or 0)
                if pid and pid != self_pid:
                    pids.append(pid)
    except Exception:
        pass
    return pids


def _kill_pids(pids):
    """taskkill 冻结的策略进程（重启前置，防双实例）。DRY_RUN 只留痕。"""
    for pid in pids:
        if _DRY_RUN:
            _log("[DRY_RUN] 将 taskkill 冻结策略进程 pid=%d" % pid)
            continue
        try:
            subprocess.check_call(["taskkill", "/PID", str(pid), "/F"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            _log("已 taskkill 冻结策略进程 pid=%d" % pid)
        except Exception as e:
            _log("taskkill pid=%d 失败: %s" % (pid, e))


def _spawn_strategy():
    """拉起策略进程（detached，stdout→logs/guardian_strategy_stdout.log）。返回 Popen 或 None。"""
    if _DRY_RUN:
        _log("[DRY_RUN] 将拉起策略: %s (cwd=%s)" % (" ".join(STRATEGY_CMD), STRATEGY_WORKDIR))
        return "DRY"
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        stdout_path = os.path.join(LOG_DIR, "guardian_strategy_stdout.log")
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with open(stdout_path, "ab") as out:
            proc = subprocess.Popen(STRATEGY_CMD, cwd=STRATEGY_WORKDIR,
                                    stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=flags)
        _log("策略进程已拉起 pid=%s cmd=%s stdout→%s" % (proc.pid, STRATEGY_CMD, stdout_path))
        return proc
    except FileNotFoundError:
        # PATH 无 python → 退回当前解释器（与 ops_guard.ensure_watcher 同回退）
        try:
            cmd = [sys.executable] + STRATEGY_CMD[1:]
            stdout_path = os.path.join(LOG_DIR, "guardian_strategy_stdout.log")
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            with open(stdout_path, "ab") as out:
                proc = subprocess.Popen(cmd, cwd=STRATEGY_WORKDIR,
                                        stdout=out, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, creationflags=flags)
            _log("策略进程已拉起(回退解释器) pid=%s cmd=%s" % (proc.pid, cmd))
            return proc
        except Exception as e2:
            _log("策略拉起失败(回退解释器亦失败): %s" % e2)
            return None
    except Exception as e:
        _log("策略拉起失败: %s" % e)
        return None


# ── 主巡检 ──

def _try_restart(reason):
    """执行一次重启：冻结进程先 kill 再拉起；置 pending_restart_ts 等待心跳恢复确认。"""
    pids = _find_strategy_pids()
    if pids:
        _log("心跳停顿但策略进程仍存活 pids=%s（判定=冻结）" % pids)
        if KILL_FROZEN_BEFORE_RESTART:
            _kill_pids(pids)
    else:
        _log("心跳停顿且未发现存活策略进程（判定=静默死亡）")
    proc = _spawn_strategy()
    _state["spawned_proc"] = proc if proc not in (None, "DRY") else _state["spawned_proc"]
    _state["pending_restart_ts"] = time.time()
    _push("做T策略进程守护：已尝试重启",
          "原因: %s\n冻结进程: %s\n拉起: %s" % (reason, pids or "无",
                                              "成功" if proc else "失败"),
          level="orange")


def tick():
    """单轮巡检（主循环每 30s 调用；离线测试直接驱动本函数）。"""
    # ① KILL_SWITCH 优先：人工急停语义——只告警，绝不拉起
    if _kill_switch_on():
        _push("做T策略进程守护：KILL_SWITCH 存在",
              "检测到急停文件 %s，守护不拉起策略进程。如需恢复请先移除 KILL_SWITCH。" % KILL_SWITCH_PATH,
              level="red")
        return

    age = _heartbeat_age_sec()
    # ② 心跳新鲜 → 一切正常；有待确认的重启/失败计数/只告警态一律销账复位
    if age is not None and age <= HEARTBEAT_STALE_SEC:
        if _state["pending_restart_ts"]:
            _state["pending_restart_ts"] = 0.0
            _state["consec_fail"] = 0
            _state["alert_only"] = False
            _log("心跳已恢复（重启确认成功），连续失败计数清零")
            _push("做T策略进程守护：心跳已恢复", "策略进程重启后心跳恢复，守护回到正常巡检。", level="green")
        elif _state["alert_only"] or _state["consec_fail"]:
            # 人工介入恢复（如手动重启策略）后，守护自动解除只告警态回到正常巡检
            _log("心跳正常，解除只告警态/清零失败计数（人工恢复确认）")
            _state["consec_fail"] = 0
            _state["alert_only"] = False
        return

    # ③ 非交易时段：心跳停顿属正常，不动
    if not _in_trading_window():
        return

    # ④ 有待确认的重启且宽限已过、心跳仍未恢复 → 记一次失败
    if (_state["pending_restart_ts"]
            and time.time() - _state["pending_restart_ts"] > RESTART_GRACE_SEC):
        _state["consec_fail"] += 1
        _state["pending_restart_ts"] = 0.0
        _log("重启后 %ds 内心跳未恢复，记连续失败 #%d" % (RESTART_GRACE_SEC, _state["consec_fail"]))
        if _state["consec_fail"] >= MAX_RESTART_FAIL:
            _state["alert_only"] = True
            _push("做T策略进程守护：连续 %d 次重启失败，转入只告警" % _state["consec_fail"],
                  "策略进程连续 %d 次拉起后心跳仍未恢复，守护已停止自动拉起，请人工介入。" % _state["consec_fail"],
                  level="red")
            return

    # ⑤ 心跳停顿超时
    reason = "心跳停顿 %s（阈值 %ds）" % ("文件不存在" if age is None else "%.0fs" % age,
                                          HEARTBEAT_STALE_SEC)
    if _state["alert_only"]:
        _push("做T策略进程守护：策略心跳停顿（只告警态）", reason + "\n守护处于只告警态，请人工介入。", level="red")
        return
    if _state["pending_restart_ts"]:
        return  # 宽限期内，等心跳恢复
    _log(reason + "，尝试重启策略进程")
    _try_restart(reason)


def main():
    _log("guardian 启动 pid=%d bridge=%s dry_run=%s feishu=%s" % (
        os.getpid(), BRIDGE_DIR, _DRY_RUN,
        _FEISHU_OK if not _FEISHU_OK else "ok"))
    if not _FEISHU_OK:
        _log("飞书不可用（%s），告警降级为日志" % globals().get("_FEISHU_ERR", "?"))
    while True:
        try:
            tick()
        except Exception as e:
            _log("[ERROR] 巡检异常（不退出）: %s" % e)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    main()
