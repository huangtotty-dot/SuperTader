# coding=utf-8
"""
gm_bridge/feishu.py — 项目内飞书推送基建（0817 内移，去除 E:\\06_T 依赖）

背景：watcher 原依赖 E:\\06_T\\config.py 的 send_feishu_payload；0817 该文件随
E:\\06_T 暂停被清理删除，导致全天推送 sent=false（复盘 2026-08-17 异常 #1）。

webhook 读取顺序（不入库，runtime/ 已 gitignore）：
  1. 环境变量 GM_FEISHU_WEBHOOK
  2. 项目 runtime/feishu_webhook.txt（纯 URL 一行）

安全说明：自定义机器人用关键词校验（默认"掘金模拟盘"），卡片文案需含关键词。

2026-11-23 限频治理（历史事故：多路推送共用 webhook 触发频率限制时，推送静默丢失，
仅 print 留痕）：
  - 限频识别：返回体 code==11232，或 msg 含 频率/rate/limit/frequency/too frequent/
    too many（英文词边界，防 "operate" 含 "rate" 误命中），或 HTTP 429；
  - 限频走独立长退避 RATE_LIMIT_BACKOFF = (5s, 15s, 30s)，最多重试 3 次；
  - 重试耗尽 → 死信落盘 t_io/logs/feishu_deadletter.jsonl（append 一行 JSON），
    并 print 明确告警；resend_deadletters() 供手动/调度重投（成功移除、失败保留并计数）；
  - 关键词/签名等永久业务错误维持原判：不重试、不落死信（向后兼容）；
  - 网络/解析瞬时异常维持 10-14 语义：max_attempts 次，退避 2s/4s（向后兼容）。
"""

import json
import os
import re
import time
import urllib.request
try:
    from core.diag_metrics import swallow as _swallow
except ImportError:  # 路径未就绪的极端环境：计数降级为空操作，绝不崩主链（P1-6）
    def _swallow(*_a, **_k):
        return None

FEISHU_KEYWORD = "掘金模拟盘"

_WEBHOOK_CACHE = None

# ── 限频治理常量（2026-11-23） ──
RATE_LIMIT_BACKOFF = (5, 15, 30)      # 限频长退避秒表：第 1/2/3 次重试前的等待
_RATE_LIMIT_CODES = {11232}           # 飞书自定义机器人「频率限制」错误码
# 限频关键词判据：英文用词边界（"operate" 含 "rate" 子串但非限频）；中文直接匹配
_RATE_LIMIT_RE = re.compile(
    r"频率|\brate\b|\blimit\b|\bfrequency\b|too\s+frequent|too\s+many", re.I)


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


# ── 死信与限频工具（2026-11-23） ──

def _st_root() -> str:
    """仓库根目录：feishu.py → gm_bridge → _gm → auto → execution → <root>（与 writer.py 同款 5 级推导）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))))


def _deadletter_path() -> str:
    """死信文件路径（manual 侧 config.py 写同一文件，靠 channel 字段区分来源）。"""
    return os.path.join(_st_root(), "t_io", "logs", "feishu_deadletter.jsonl")


def _is_rate_limit_body(body) -> bool:
    """返回体限频判据：code==11232，或返回体 JSON 文本命中限频关键词。"""
    if not isinstance(body, dict):
        return False
    if body.get("code") in _RATE_LIMIT_CODES or body.get("StatusCode") in _RATE_LIMIT_CODES:
        return True
    try:
        text = json.dumps(body, ensure_ascii=False)
    except Exception:
        text = str(body)
    return bool(_RATE_LIMIT_RE.search(text))


def _is_rate_limit_exc(e: Exception) -> bool:
    """异常限频判据：HTTP 429（urllib 对 4xx 抛 HTTPError，.code==429），
    或错误文本命中限频关键词；HTTPError 可能带 JSON 错误体，尽力再核一次返回体判据。"""
    if getattr(e, "code", None) == 429:
        return True
    if _RATE_LIMIT_RE.search(str(e)):
        return True
    if hasattr(e, "read"):  # HTTPError 带响应体：试解析 code 11232
        try:
            return _is_rate_limit_body(json.loads(e.read().decode("utf-8")))
        except Exception:
            _swallow("exec.gm_bridge.feishu._is_rate_limit_exc")  # 原静默吞点：计数化（P1-6 三批），行为等价
            return False
    return False


def _post_once(url: str, data: bytes) -> dict:
    """单次 POST，返回飞书 JSON 体；异常原样上抛，由调用方分类。"""
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def _payload_summary(payload: dict) -> str:
    """尽力提取推送文案摘要（卡片标题 → 首个文本元素 → content.text），限 120 字。"""
    try:
        title = ((payload.get("card") or {}).get("header") or {}).get("title") or {}
        text = (title.get("content") or "").strip()
        if text:
            return text[:120]
        for el in (payload.get("card") or {}).get("elements") or []:
            content = ((el.get("text") or {}).get("content") or "").strip()
            if content:
                return content[:120]
        return ((payload.get("content") or {}).get("text") or "").strip()[:120]
    except Exception:
        _swallow("exec.gm_bridge.feishu._payload_summary")  # 原静默吞点：计数化（P1-6 三批），行为等价
        return ""


def _write_deadletter(payload: dict, error: str, attempts: int, channel: str) -> None:
    """限频重试耗尽 → 死信落盘 t_io/logs/feishu_deadletter.jsonl（append 一行 JSON）。
    任何异常吞掉——落盘失败绝不能再把推送判负链路炸掉。"""
    try:
        path = _deadletter_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        record = {
            "event": "feishu_deadletter",
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "_ts": time.time(),
            "channel": channel,                 # auto / manual（同一文件区分来源）
            "error": str(error)[:500],
            "attempts": attempts,               # 落死信前的总尝试次数（首试 + 重试）
            "resend_attempts": 0,               # 重投计数，resend_deadletters 失败时累加
            "title": _payload_summary(payload),
            "payload": payload,                 # 原文留存，供重投
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        _swallow("exec.gm_bridge.feishu._write_deadletter")  # 原静默吞点：计数化（P1-6 三批），行为等价


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
    - webhook 缺失、飞书业务码 code!=0（关键词/签名等确定性错误）→ 不重试。

    2026-11-23 限频治理：限频错误（code 11232 / 频率关键词 / HTTP 429）从
    「永久业务错误」中拆出，走独立长退避 5s/15s/30s（最多重试 3 次），耗尽落死信。
    """
    url = get_webhook()
    if not url:
        err = "webhook 未配置（GM_FEISHU_WEBHOOK 或 runtime/feishu_webhook.txt）"
        print(f"[{error_prefix}] {err}")
        return False, err
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_err = ""
    net_retries = 0   # 网络/解析异常已重试次数（2s/4s，上限 max_attempts-1 次）
    rl_retries = 0    # 限频已重试次数（RATE_LIMIT_BACKOFF 长退避，独立预算）
    attempts = 0      # 总尝试次数（含两通道），写入死信记录
    while True:
        attempts += 1
        try:
            body = _post_once(url, data)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if _is_rate_limit_exc(e):
                # 限频异常（HTTP 429 等）：长退避重试；耗尽落死信
                if rl_retries < len(RATE_LIMIT_BACKOFF):
                    wait = RATE_LIMIT_BACKOFF[rl_retries]
                    rl_retries += 1
                    print(f"[{error_prefix}] 触发飞书频率限制，{wait}s 后第{rl_retries}次重投: {last_err[:120]}")
                    time.sleep(wait)
                    continue
                _write_deadletter(payload, last_err, attempts, "auto")
                print(f"[{error_prefix}] ❌ 限频重试{rl_retries}次仍失败，已落死信 "
                      f"{_deadletter_path()}（resend_deadletters 可重投）: {last_err[:200]}")
                return False, last_err
            # 普通瞬时异常：维持 10-14 语义（max_attempts 次尝试，退避 2s/4s）
            if net_retries < max(1, max_attempts) - 1:
                net_retries += 1
                wait = net_retries * 2
                print(f"[{error_prefix}] 第{net_retries}次发送失败，{wait}s 后重试: {last_err[:120]}")
                time.sleep(wait)
                continue
            print(f"[{error_prefix}] {max_attempts}次尝试均失败: {last_err[:200]}")
            return False, last_err
        ok = body.get("code") == 0 or body.get("StatusCode") == 0
        if ok:
            return True, ""
        if _is_rate_limit_body(body):
            # 限频返回体（code 11232 / 频率关键词）：长退避重试；耗尽落死信
            last_err = f"飞书限频: {body}"
            if rl_retries < len(RATE_LIMIT_BACKOFF):
                wait = RATE_LIMIT_BACKOFF[rl_retries]
                rl_retries += 1
                print(f"[{error_prefix}] 触发飞书频率限制，{wait}s 后第{rl_retries}次重投: {str(body)[:120]}")
                time.sleep(wait)
                continue
            _write_deadletter(payload, last_err, attempts, "auto")
            print(f"[{error_prefix}] ❌ 限频重试{rl_retries}次仍失败，已落死信 "
                  f"{_deadletter_path()}（resend_deadletters 可重投）: {last_err[:200]}")
            return False, last_err
        # 永久业务错误（关键词不匹配/签名校验等）：重试无意义，直接判负，不落死信
        err = f"飞书返回异常: {body}"
        print(f"[{error_prefix}] {err}")
        return False, err


def resend_deadletters(path: str = "") -> dict:
    """死信重投（手动/后续调度调用，本函数不接调度）。

    逐条重投死信文件中的推送：
    - 成功 → 从死信文件移除；
    - 失败 → 保留并累加 resend_attempts、记录 last_resend_error/time；
    - 原子重写：先写 .tmp 再 os.replace，重投途中崩溃不截断原文件；
    - 每条单次尝试（不在此处睡退避——限频场景的重投节奏由调用方决定）；
    - 坏行原样保留，绝不丢数据；
    - 返回 {"total", "resent", "kept", "path"}。
    """
    path = path or _deadletter_path()
    if not os.path.exists(path):
        return {"total": 0, "resent": 0, "kept": 0, "path": path}
    with open(path, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines() if ln.strip()]
    url = get_webhook()
    kept, resent, total = [], 0, 0
    for ln in lines:
        try:
            entry = json.loads(ln)
        except Exception:
            kept.append(ln)  # 坏行原样保留
            continue
        total += 1
        ok, err = False, ""
        if not url:
            err = "webhook 未配置"
        else:
            try:
                body = _post_once(url, json.dumps(entry.get("payload") or {},
                                                  ensure_ascii=False).encode("utf-8"))
                ok = body.get("code") == 0 or body.get("StatusCode") == 0
                if not ok:
                    err = f"飞书返回异常: {body}"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        if ok:
            resent += 1
            print(f"[feishu] 死信重投成功（{entry.get('time', '?')} {entry.get('title', '')}）")
        else:
            entry["resend_attempts"] = int(entry.get("resend_attempts", 0) or 0) + 1
            entry["last_resend_error"] = str(err)[:500]
            entry["last_resend_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            kept.append(json.dumps(entry, ensure_ascii=False))
            print(f"[feishu] 死信重投失败（第{entry['resend_attempts']}次），保留待下次: {str(err)[:120]}")
    # 原子重写死信文件（全部成功则写为空文件）
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for kept_ln in kept:
            f.write(kept_ln + "\n")
    os.replace(tmp, path)
    return {"total": total, "resent": resent, "kept": len(kept), "path": path}
