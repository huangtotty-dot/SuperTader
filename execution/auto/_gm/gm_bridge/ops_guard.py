# coding=utf-8
"""
gm_bridge/ops_guard.py — 运维自举与双向看门狗（2026-08-06 红日整改）

背景：0806 策略 09:42 静默僵死 5.3 小时，watcher 告警从未发出（进程未启动或同样僵死），
且控制台日志未落盘导致根因无法追查。本模块解决三件事：

1. 控制台日志落盘：init 时把 stdout/stderr tee 到 logs/strategy_YYYYMMDD.log，
   崩溃 traceback / 挂起前最后的 print 全部留痕（L1/L2 缺口的策略侧修复）。
2. watcher 自动拉起：点掘金终端"运行"按钮即由策略进程自动拉起 watcher，
   无需单独双击 start_monitor.bat。
3. 双向看门狗：watcher 每 2s 写 watcher_heartbeat.json；策略每根 bar 检查，
   心跳缺失/过期 >90s 自动重生 watcher（冷却 300s 防抖动）。
   反向（watcher 盯策略心跳）由 watcher._check_heartbeat 既有逻辑承担。

设计约束：
- watcher 僵死/未启动不影响交易链路（拉起失败仅 print，不抛异常）；
- 所有动作落 print → 进 strategy 日志，供复盘归因。
"""

import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import datetime

_WATCHER_HB_NAME = "watcher_heartbeat.json"
_STALE_SECONDS = 90          # watcher 心跳超过 90s 未刷新视为死亡
_RESPAWN_COOLDOWN = 300      # 重生冷却，防反复拉起
_state = {"last_respawn": 0.0, "bootstrapped": False, "ansi": False}

# ANSI 转义序列（写日志文件前剥离）。与 `oversold_gap_confirm.style.ANSI_RE` 口径一致，
# 但**刻意不复用**：gm_bridge 是最底层桥，不该 import 任何具体策略包。
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _enable_vt_console():
    """尽力开启控制台的 ANSI(VT) 处理。**开了才允许策略侧发色码**——否则控制台会把
    转义序列当普通字符打出来，比不上色还糟。返回 True 表示至少一个句柄设置成功。"""
    if os.name != "nt":
        return False
    ok = False
    try:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetStdHandle.restype = wintypes.HANDLE
        k.GetStdHandle.argtypes = [wintypes.DWORD]
        k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        _ENABLE_VT = 0x0004
        STD_OUTPUT_HANDLE, STD_ERROR_HANDLE = -11, -12
        for _std in (STD_OUTPUT_HANDLE, STD_ERROR_HANDLE):
            h = k.GetStdHandle(_std & 0xFFFFFFFF)
            mode = wintypes.DWORD()
            if not k.GetConsoleMode(h, ctypes.byref(mode)):
                continue                           # 没有控制台（重定向/管道）
            _set = bool(k.SetConsoleMode(h, mode.value | _ENABLE_VT))
            # ⚠️ 只有 **stdout** 成功才算"可以上色"：策略的 print 与 tee 都走 stdout。
            #    若 stdout 被重定向而 stderr 是控制台，这里必须回 False——否则色码会被
            #    写进那个重定向的文件里。
            if _std == STD_OUTPUT_HANDLE:
                ok = _set
    except Exception:
        ok = False
    return ok


def ansi_enabled():
    """控制台是否已确认支持 ANSI 着色（供宿主决定要不要给策略侧开色）。"""
    return bool(_state.get("ansi"))

# 桥目录统一由 writer 决定（0806 迁移：项目内 runtime/bridge）
try:
    from gm_bridge.writer import BRIDGE_DIR as _BRIDGE_DIR
except ImportError:
    from writer import BRIDGE_DIR as _BRIDGE_DIR


class _TeeStream:
    """同时写原控制台与日志文件的流。F2(2026-09-09): 每次写前做跨日滚动——旧句柄定死首日日期，
    进程跨日运行时日志仍全写首日文件，新增按日滚动（仿 watcher._roll_events_path）。

    2026-10-15 管道堵塞事故整改（终端22516死亡 → stdout 管道写满 64KB → write 永久阻塞 →
    策略主线程静默挂死 2 小时，14:50 卖侧缺席）：
      ① **文件日志先行**：先 `_fh.write + flush`（落盘可靠性最高优先级），再碰控制台；
      ② 控制台写入改走**后台 daemon 线程 + 有界队列**：队列满 → 丢弃控制台输出并计数
         （降级告警落文件），**绝不阻塞主线程**；消费者线程阻塞也只堵它自己；
      ③ 全路径线程安全（GM 回调线程模型：on_bar/print 可能来自 SDK 线程）。
    """

    _QUEUE_MAX = 4096        # 控制台输出队列上限（条）。约覆盖数小时密集日志
    _FH_LOCK = threading.RLock()   # stdout/stderr 两个 Tee 共享同一文件句柄 ⇒ 类级互斥

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh
        self._q = queue.Queue(maxsize=self._QUEUE_MAX)
        self._dropped = 0            # 因队列满被丢弃的控制台输出条数
        self._console_dead = False   # 消费者确认管道已断（异常）⇒ 之后不再入队
        self._worker = threading.Thread(target=self._console_consumer,
                                        name="tee-console-%d" % id(self),
                                        daemon=True)
        self._worker.start()

    # ── 控制台：后台消费者（唯一会碰 self._stream 的地方）────────────
    def _console_consumer(self):
        """从队列取数据写控制台。write/flush 永久阻塞（管道写满）也只堵本 daemon 线程。"""
        while True:
            try:
                data = self._q.get()
            except Exception:
                return
            try:
                if data is not None and not self._console_dead:
                    self._stream.write(data)
                    try:
                        self._stream.flush()
                    except Exception:
                        pass
            except Exception as e:
                # 管道已断（BrokenPipeError 等）：标记死亡，write 侧不再入队；
                # 降级告警落文件（此时控制台已不可达）。
                self._console_dead = True
                self._file_notice("⚠️ 控制台写入异常（%s: %s），后续控制台输出已停用；"
                                  "文件日志不受影响" % (type(e).__name__, str(e)[:80]))
            finally:
                try:
                    self._q.task_done()
                except Exception:
                    pass

    def _enqueue_console(self, data):
        """非阻塞入队；满 ⇒ 丢弃 + 计数（周期性落文件告警）。任何路径都不抛。"""
        if self._console_dead:
            return
        try:
            self._q.put_nowait(data)
            return
        except queue.Full:
            pass
        except Exception:
            return
        with self._FH_LOCK:
            self._dropped += 1
            n = self._dropped
        if n == 1 or n % 1000 == 0:
            self._file_notice("⚠️ 控制台输出队列已满(>%d条)，已丢弃 %d 条 "
                              "（终端失联/管道堵塞？交易链路不受影响）" % (self._QUEUE_MAX, n))

    # ── 文件：主线程直写（先行），全在类级锁内 ─────────────────────
    def _file_notice(self, msg):
        """降级/异常告警落文件（自带时间戳）。best-effort，不抛。"""
        try:
            with self._FH_LOCK:
                self._fh.write("\n[ops] %s %s\n"
                               % (datetime.now().strftime("%H:%M:%S"), msg))
                self._fh.flush()
        except Exception:
            pass

    def _roll_if_needed(self):
        today = datetime.now().strftime("%Y%m%d")
        if today == _state.get("_log_date"):
            return
        log_dir = _state.get("_log_dir") or ""
        if not log_dir:
            return
        try:
            log_path = os.path.join(log_dir, "strategy_%s.log" % today)
            nf = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
            nf.write("\n===== strategy run rollover %s pid=%d (跨日新日志) =====\n"
                     % (datetime.now().isoformat(timespec="seconds"), os.getpid()))
            nf.flush()
            old = self._fh
            self._fh = nf
            _state["_log_fh"] = nf
            _state["_log_date"] = today
            try:
                old.close()
            except Exception:
                pass
            # ⚠️ 2026-10-15：不再 `sys.__stdout__.write` —— 那正是会永久阻塞的坏管道。
            #    滚动通告走「文件 + 控制台队列」两条安全通道。
            self._enqueue_console("  [ops] 日志已跨日滚动: %s\n" % log_path)
        except Exception:
            pass

    def write(self, data):
        # ① 文件先行：落盘可靠是第一优先级（控制台堵死也不许拖累文件）。
        try:
            with self._FH_LOCK:
                if getattr(self._fh, "closed", False):
                    # stdout/stderr 两个 Tee 共享句柄，一方跨日滚动关了旧句柄 ⇒ 另一方收养新句柄
                    self._fh = _state.get("_log_fh") or self._fh
                self._roll_if_needed()
                # 2026-10-15：日志文件剥掉 ANSI 色码（控制台原样透传）。
                # 色码由策略侧（oversold_gap_confirm.style）产生，写盘这份必须是纯文本，
                # 否则 strategy_*.log 会被塞满转义序列、grep/复盘全废。
                # ⚠️ 分层：本模块**不** import 策略包，自带等价正则（口径见 style.ANSI_RE）。
                self._fh.write(_ANSI_RE.sub("", data) if "\x1b" in data else data)
                self._fh.flush()
        except Exception:
            pass
        # ② 控制台：异步队列，满即丢，绝不阻塞主线程
        self._enqueue_console(data)
        return len(data)

    def flush(self):
        # ⚠️ 刻意**不**碰 self._stream：管道写满时 flush 同样永久阻塞。
        #    控制台的 flush 由消费者线程在每次 write 后负责（堵也只堵线程）。
        try:
            with self._FH_LOCK:
                self._fh.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        # 透传 encoding/isatty/writelines 等属性到原流（print/file= 之外的鸭子接口）。
        # 注意：只补属性访问；write/flush 已在上面对防阻塞做过专门处理。
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._stream, name)


def watcher_heartbeat_path():
    return os.path.join(_BRIDGE_DIR, _WATCHER_HB_NAME)


def bootstrap_logging(project_dir):
    """stdout/stderr tee 到 logs/strategy_YYYYMMDD.log（幂等）。"""
    if _state["bootstrapped"]:
        return
    _state["bootstrapped"] = True
    # 控制台能力探测（2026-10-15，先于日志落盘）：开 ANSI/VT，开了才让策略侧发色码
    # （`ansi_enabled()` 供 gm_main 转发给 style）。best-effort：失败只影响"好不好看"，
    # 绝不影响下单与日志落盘。
    # ⚠️ 刻意**不改**控制台代码页：CPython 对真控制台走 WriteConsoleW 直写宽字符，代码页
    #    不参与 ⇒ `→ ⇒ ✓` 本来就能打（`UnicodeEncodeError('gbk')` 只在**管道**输出时出现）；
    #    改 CP 收益为零，却可能扰乱 GM SDK（编译包）的裸字节写。
    try:
        _state["ansi"] = _enable_vt_console()
    except Exception:
        _state["ansi"] = False
    try:
        log_dir = os.path.join(project_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "strategy_%s.log" % datetime.now().strftime("%Y%m%d"))
        fh = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)
        _state["_log_dir"] = log_dir            # F2: 供 _TeeStream 跨日滚动开新文件
        _state["_log_date"] = datetime.now().strftime("%Y%m%d")
        _state["_log_fh"] = fh
        fh.write("\n===== strategy run start %s pid=%d =====\n"
                 % (datetime.now().isoformat(timespec="seconds"), os.getpid()))
        sys.stdout = _TeeStream(sys.stdout, fh)
        sys.stderr = _TeeStream(sys.stderr, fh)
        print("[ops] 控制台日志已落盘: %s" % log_path)
    except Exception as e:
        # 落盘失败不阻断策略启动
        try:
            sys.__stdout__.write("[ops] 日志落盘失败: %s\n" % e)
        except Exception:
            pass


def watcher_alive():
    """watcher 心跳文件存在且 90s 内刷新过。"""
    try:
        return (time.time() - os.path.getmtime(watcher_heartbeat_path())) <= _STALE_SECONDS
    except Exception:
        return False


def ensure_watcher(project_dir):
    """watcher 心跳缺失/过期则自动拉起（冷却 300s）。每根 bar 调用，代价 = 一次 stat。"""
    if watcher_alive():
        return
    now = time.time()
    if now - _state["last_respawn"] < _RESPAWN_COOLDOWN:
        return
    _state["last_respawn"] = now
    # P4-2: watcher 随支撑模块在 _gm/gm_bridge/，按本文件位置解析（而非 project_dir）
    watcher_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "gm_bridge", "watcher.py")
    if not os.path.exists(watcher_py):
        print("[ops] watcher 拉起失败: 文件不存在 %s" % watcher_py)
        return
    stdout_path = os.path.join(project_dir, "logs", "watcher_stdout.log")
    try:
        os.makedirs(os.path.dirname(stdout_path), exist_ok=True)
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        with open(stdout_path, "ab") as out:
            try:
                subprocess.Popen(["python", watcher_py], cwd=project_dir,
                                 stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=flags)
            except FileNotFoundError:
                # PATH 无 python 时退回当前解释器（掘金终端内嵌 Python 也可跑 watcher，
                # 其依赖仅 json/os/requests 及 E:\06_T\config）
                subprocess.Popen([sys.executable, watcher_py], cwd=project_dir,
                                 stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, creationflags=flags)
        print("[ops] watcher 心跳缺失/过期(>%ds)，已自动拉起，stdout→%s"
              % (_STALE_SECONDS, stdout_path))
    except Exception as e:
        print("[ops] watcher 自动拉起失败: %s" % e)
