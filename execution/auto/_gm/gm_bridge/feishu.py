# coding=utf-8
"""
gm_bridge/feishu.py — 项目内飞书推送基建（0817 内移，去除 E:\\06_T 依赖）

背景：watcher 原依赖 E:\\06_T\\config.py 的 send_feishu_payload；0817 该文件随
E:\\06_T 暂停被清理删除，导致全天推送 sent=false（复盘 2026-08-17 异常 #1）。

webhook 读取顺序（不入库，runtime/ 已 gitignore）：
  1. 环境变量 GM_FEISHU_WEBHOOK
  2. 项目 runtime/feishu_webhook.txt（纯 URL 一行）

安全说明：自定义机器人用关键词校验（默认"掘金模拟盘"），卡片文案需含关键词。
"""

import json
import os
import time
import urllib.request

FEISHU_KEYWORD = "掘金模拟盘"

_WEBHOOK_CACHE = None


def get_webhook() -> str:
    """读取 webhook 地址；找不到返回空串。"""
    global _WEBHOOK_CACHE
    if _WEBHOOK_CACHE is not None:
        return _WEBHOOK_CACHE
    url = os.environ.get("GM_FEISHU_WEBHOOK", "").strip()
    if not url:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "runtime", "feishu_webhook.txt")
        try:
            with open(path, encoding="utf-8") as f:
                url = f.read().strip()
        except OSError:
            url = ""
    _WEBHOOK_CACHE = url
    return url


def send_feishu_payload(payload: dict, success_log: str = "", error_prefix: str = "feishu",
                        trigger_urgent_alarm_after_success: bool = False) -> bool:
    """发送卡片/文本到自定义机器人。返回 True=成功。

    签名与旧 E:\\06_T config.send_feishu_payload 保持一致（watcher 调用处不动）。
    trigger_urgent_alarm_after_success 参数保留兼容（当前自建机器人无升级告警通道）。
    """
    sent, _err = send_feishu_payload_ex(payload, success_log, error_prefix,
                                        trigger_urgent_alarm_after_success)
    return sent


def send_feishu_payload_ex(payload: dict, success_log: str = "", error_prefix: str = "feishu",
                           trigger_urgent_alarm_after_success: bool = False,
                           max_attempts: int = 3):
    """同 send_feishu_payload，但返回 (sent: bool, error: str) 供调用方落盘留痕。

    2026-10-14 根修（auto 成交推送 sent=false）：
    旧实现单次尝试、无重试，任何瞬时网络异常（如 RemoteDisconnected，本机当日
    策略预取同样撞到过）直接判负；manual 侧 config._send_feishu_payload 有
    3 次尝试 + 递增退避，同日 71/71 全部送达。此处对齐 manual 侧语义：
    - 网络/解析异常 → 最多 max_attempts 次，退避 2s/4s（与 manual 一致）；
    - webhook 缺失、飞书业务码 code!=0（关键词/限流等确定性错误）→ 不重试。
    """
    url = get_webhook()
    if not url:
        err = "webhook 未配置（GM_FEISHU_WEBHOOK 或 runtime/feishu_webhook.txt）"
        print(f"[{error_prefix}] {err}")
        return False, err
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = ""
    for attempt in range(max(1, max_attempts)):
        try:
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                body = json.loads(r.read().decode("utf-8"))
            ok = body.get("code") == 0 or body.get("StatusCode") == 0
            if not ok:
                # 业务错误（关键词不匹配/签名校验/频率限制等）重试无意义，直接判负
                err = f"飞书返回异常: {body}"
                print(f"[{error_prefix}] {err}")
                return False, err
            return True, ""
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < max_attempts - 1:
                wait = (attempt + 1) * 2
                print(f"[{error_prefix}] 第{attempt + 1}次发送失败，{wait}s 后重试: {last_err[:120]}")
                time.sleep(wait)
            else:
                print(f"[{error_prefix}] {max_attempts}次尝试均失败: {last_err[:200]}")
    return False, last_err
