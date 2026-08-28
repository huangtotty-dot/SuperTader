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

_TERM_PROC = "gmterm-serv.exe"
_TOKEN_RE = re.compile(r"--token=([0-9a-fA-F]{32,64})")

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_BASE, "t_io", "state", "gm_config.json")


def _parse_token(text: str):
    m = _TOKEN_RE.search(text or "")
    return m.group(1) if m else None


def _terminal_token():
    try:
        import psutil
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                if p.info["name"] and p.info["name"].lower() == _TERM_PROC and p.info["cmdline"]:
                    return _parse_token(" ".join(p.info["cmdline"]))
            except Exception:
                continue
    except ImportError:
        pass
    try:
        out = subprocess.run(
            ["wmic", "process", "where", f"name='{_TERM_PROC}'", "get", "commandline", "/format:list"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            t = _parse_token(out.stdout)
            if t:
                return t
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Get-CimInstance Win32_Process -Filter \"name='{_TERM_PROC}'\" | Select-Object -ExpandProperty CommandLine"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            t = _parse_token(out.stdout)
            if t:
                return t
    except Exception:
        pass
    return None


def load_token():
    t = os.environ.get("GM_TOKEN")
    if t:
        return t
    t = _terminal_token()
    if t:
        return t
    try:
        return json.load(open(_CONFIG, encoding="utf-8")).get("token")
    except Exception:
        return None
