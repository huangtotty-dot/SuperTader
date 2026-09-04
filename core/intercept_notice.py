# -*- coding: utf-8 -*-
"""intercept_notice.py — 🔕 拦截可见性去重（F-11, 2026-09-04）

原 main.py 去重为"每股每向每日最多 1 条"布尔标记：同日同码第二次被拦静默
（09-04 10:34 与 10:57 002451 BUY_LOW 两次共振拦截只发 1 条 🔕 → Q-20260904-5
「拦截全量可感知」）。改为滚动冷却窗：同 (code:action) 距上次**成功发送** < COOLDOWN_S
抑制，否则放行再发——同码同日复拦仍可见，同时防 5min 扫描节拍紧邻刷屏。

设计点：
- 旧布尔 True 值视为"很久前已发"（过期）→ 放行，兼容升级当日已写 state 的情况。
- 时间戳由调用方在 send 成功后写回（mark_sent），发送失败不占额（B3 语义：
  sent=false 不写防重，重启后允许补报）。
仅依赖 stdlib，供 main.py 与单测共用。
"""
import json
from datetime import datetime

# 20 分钟：09-04 实测同码两次拦截间隔 23min14s —— 低于它放行同日复拦，同时把最坏刷屏
# 压到 ≤3 次/小时/码向（原日级去重的防噪意图部分保留）。
COOLDOWN_S = 20 * 60


def read_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def write_state(path: str, state: dict) -> None:
    import os
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def send_allowed(state: dict, today: str, key: str, now_dt: datetime,
                 cooldown_s: int = COOLDOWN_S) -> bool:
    """同 (today,key) 距上次成功发送不足 cooldown_s → False（抑制）；否则 True（放行）。"""
    day = state.setdefault(str(today), {})
    prev = day.get(key)
    last = None
    if isinstance(prev, str):
        try:
            last = datetime.strptime(prev, "%Y-%m-%d %H:%M:%S")
        except Exception:
            last = None
    if last is not None and (now_dt - last).total_seconds() < cooldown_s:
        return False
    return True


def mark_sent(state: dict, today: str, key: str, now_dt: datetime) -> None:
    """send 成功后写回时间戳（失败不调用——不占额，B3 语义）。"""
    state.setdefault(str(today), {})[key] = now_dt.strftime("%Y-%m-%d %H:%M:%S")
