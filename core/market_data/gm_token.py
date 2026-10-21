# -*- coding: utf-8 -*-
"""掘金 token 加载（superTrader 侧，合并实施方案 P0-2 返工）。
国盛定制版 gm SDK 数据接口认证掘金终端会话 token（gmterm-serv.exe --token=，随终端重启轮换），
静态配置文件实测无效（审核 commit 5f905f5c）。

读取顺序：GM_TOKEN 环境变量 → 终端进程动态发现 → t_io/state/gm_config.json 兜底 → None。
"""
import json
import os
import re
import subprocess
import threading

# P4-4: 新终端（国盛掘金3 专业版）数据服务仍是 gmterm-serv.exe（从新安装目录启动）；
# gsgm3.exe 为新终端 Electron 壳（防御未来版本改名/只挂壳场景）。任一进程命中即提取 --token。
_TERM_PROCS = ("gmterm-serv.exe", "gsgm3.exe")
_TOKEN_RE = re.compile(r"--token=([0-9a-fA-F]{32,64})")

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_BASE, "t_io", "state", "gm_config.json")

_TOKEN_CACHED = None      # `load_token` 记忆化（见其 docstring）
_TOKEN_LOCK = threading.Lock()


def _parse_token(text: str):
    m = _TOKEN_RE.search(text or "")
    return m.group(1) if m else None


def _run(cmd):
    """跑 wmic / powershell 并**按 Windows OEM 代码页解码**。

    不能用 `text=True`：它按 locale 解码，而 `python -X utf8`（或 PYTHONUTF8=1）会把
    locale 变成 utf-8，此时中文 Windows 上 wmic/powershell 吐的 GBK 字节解不开 ⇒
    subprocess 读取线程成批抛 UnicodeDecodeError（实测启动一次 60 条），token 也随之丢失。
    显式指定 'oem' 与解释器是否开 UTF-8 模式无关，两种启动方式结果一致。
    """
    r = subprocess.run(cmd, capture_output=True, timeout=10)
    try:
        txt = r.stdout.decode("oem", "replace")
    except LookupError:                          # 非 Windows / 无 oem 别名
        txt = r.stdout.decode("utf-8", "replace")
    return r.returncode, txt


def _terminal_token():
    for proc in _TERM_PROCS:
        t = _token_from_proc(proc)
        if t:
            return t
    return None


def _token_from_proc(proc: str):
    """对单个候选进程名做 psutil→wmic→powershell 回退链提取 token；命中即返回。"""
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if p.info["name"] and p.info["name"].lower() == proc.lower() and p.info["cmdline"]:
                    t = _parse_token(" ".join(p.info["cmdline"]))
                    if t:
                        return t
            except Exception:
                continue
    except ImportError:
        pass
    try:
        rc, txt = _run(["wmic", "process", "where", f"name='{proc}'",
                        "get", "commandline", "/format:list"])
        if rc == 0:
            t = _parse_token(txt)
            if t:
                return t
    except Exception:
        pass
    try:
        rc, txt = _run(["powershell", "-NoProfile", "-Command",
                        f"Get-CimInstance Win32_Process -Filter \"name='{proc}'\" "
                        f"| Select-Object -ExpandProperty CommandLine"])
        if rc == 0:
            t = _parse_token(txt)
            if t:
                return t
    except Exception:
        pass
    return None


def load_token():
    """返回掘金 token（**进程内记忆化**，2026-10-21）。

    为什么必须记忆化：`_token_from_proc` 要 `psutil.process_iter()` 扫全部进程
    （本机实测 ~1.2s，未命中还会退到 wmic/powershell 子进程），而它是 `GmProvider()`
    构造的一部分。GUI 首帧扇出时若同时构造多个 provider，每个都要白扫一遍。
    记忆化后同一进程只扫一次（并发的首个调用也只会有一个线程真去扫）。

    注意：终端重启会轮换 token ⇒ 只在**解析成功**时记忆；失败不缓存（$GM_TOKEN / 配置文件
    随时可能补上）。调用方拿到的仍是字符串，无状态可写，语义不变。
    """
    global _TOKEN_CACHED
    if _TOKEN_CACHED:
        return _TOKEN_CACHED
    with _TOKEN_LOCK:                 # 并发首个调用只让一个线程扫，别让 N 个线程各扫一遍
        if _TOKEN_CACHED:
            return _TOKEN_CACHED
        t = os.environ.get("GM_TOKEN")
        if not t:
            t = _terminal_token()
        if not t:
            try:
                t = json.load(open(_CONFIG, encoding="utf-8")).get("token")
            except Exception:
                t = None
        if t:
            _TOKEN_CACHED = t
        return t
