# -*- coding: utf-8 -*-
"""掘金 token 加载（合并方案 P0-2 返工，2026-08-28 审核打回后修正）。

审核结论（commit 5f905f5c）：国盛定制版 gm SDK 的数据接口认证的是**掘金终端会话 token**
（gmterm-serv.exe 启动参数 `--token=`，随终端重启轮换），静态配置文件 token 实测无效。

读取顺序：
  1. 环境变量 GM_TOKEN（最高覆盖）；
  2. 动态发现：查 gmterm-serv.exe 进程命令行解析 `--token`（主机制，终端重启后自动恢复）；
  3. 静态配置 t_io/state/gm_config.json 兜底/覆盖；
  4. 都没有 → 返回 None（调用方须降级腾讯并 log 告警）。
"""
import json
import os
import re
import subprocess

_TERM_PROC = "gmterm-serv.exe"
_TOKEN_RE = re.compile(r"--token=([0-9a-fA-F]{32,64})")


def _parse_token(text: str):
    m = _TOKEN_RE.search(text or "")
    return m.group(1) if m else None


def _terminal_token():
    """动态发现终端会话 token。优先 psutil，缺则 wmic，再 PowerShell。"""
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
    # wmic（Windows 内置，可能已弃用但通用）
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
    # PowerShell 兜底
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


def _config_token():
    try:
        cfg = os.environ.get("GM_CONFIG") or os.path.join(
            os.environ.get("SUPERTRADER_ROOT", r"E:\superTrader"),
            "t_io", "state", "gm_config.json")
        return json.load(open(cfg, encoding="utf-8")).get("token")
    except Exception:
        return None


def load_token():
    t = os.environ.get("GM_TOKEN")
    if t:
        return t
    t = _terminal_token()
    if t:
        return t
    return _config_token()
