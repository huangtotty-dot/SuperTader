# -*- coding: utf-8 -*-
"""
飞书推送模块（Webhook + 开放平台应用API）
支持：文字摘要、Excel文件上传、板块5日变化追踪
"""
import json
import requests
import os
import time
import pandas as pd
from datetime import datetime, timedelta


# ==================== 历史数据管理 ====================

def _history_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "history", "daily_summary.json")


def _load_history() -> dict:
    path = _history_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_history_entry(date_str: str, sectors: list) -> None:
    path = _history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    history = _load_history()
    history[date_str] = sectors
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _find_5_days_ago(history: dict, today: str) -> str:
    sorted_dates = sorted(history.keys())
    if today in sorted_dates:
        idx = sorted_dates.index(today)
    else:
        idx = len(sorted_dates)
    target_idx = max(0, idx - 5)
    return sorted_dates[target_idx] if sorted_dates else ""


def _delta_arrow(current, previous, reverse=False):
    if previous is None or current is None:
        return "", ""
    try:
        c = float(current)
        p = float(previous)
    except (TypeError, ValueError):
        return "", ""
    diff = c - p
    if reverse:
        diff = p - c
    if abs(diff) < 0.01:
        return "→", "0"
    arrow = "↑" if diff > 0 else "↓"
    return arrow, f"{diff:+.1f}" if abs(diff) >= 1 else f"{diff:+.2f}"


# ==================== 飞书开放平台应用API ====================

class FeishuAppClient:
    """
    飞书开放平台应用API客户端
    支持：获取tenant_access_token、上传文件、发送文件消息
    """

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.base_url = "https://open.feishu.cn/open-apis"
        self._token = None
        self._token_expire = 0

    def get_tenant_access_token(self) -> str:
        if self._token and time.time() < self._token_expire:
            return self._token
        url = f"{self.base_url}/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={"app_id": self.app_id, "app_secret": self.app_secret}, timeout=15)
        data = resp.json()
        if data.get("code") == 0:
            self._token = data["tenant_access_token"]
            self._token_expire = time.time() + data.get("expire", 7200) - 300
            print(f"[OK] 获取 tenant_access_token 成功，有效期约 {data.get('expire', 7200)}s")
            return self._token
        raise Exception(f"获取 tenant_access_token 失败: {data}")

    def upload_file(self, file_path: str, file_type: str = "stream") -> str:
        """上传文件到飞书，返回 file_key"""
        token = self.get_tenant_access_token()
        url = f"{self.base_url}/im/v1/files"
        headers = {"Authorization": f"Bearer {token}"}
        file_name = os.path.basename(file_path)
        with open(file_path, "rb") as f:
            files = {"file": (file_name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
            data = {"file_type": file_type, "file_name": file_name}
            resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
        result = resp.json()
        if result.get("code") == 0:
            file_key = result["data"]["file_key"]
            print(f"[OK] 文件上传成功: {file_name} -> file_key={file_key}")
            return file_key
        raise Exception(f"上传文件失败: {result}")

    def send_file_message(self, receive_id: str, file_key: str, receive_id_type: str = "chat_id") -> dict:
        """发送文件消息到指定接收方"""
        token = self.get_tenant_access_token()
        url = f"{self.base_url}/im/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        params = {"receive_id_type": receive_id_type}
        body = {
            "receive_id": receive_id,
            "msg_type": "file",
            "content": json.dumps({"file_key": file_key})
        }
        resp = requests.post(url, headers=headers, params=params, json=body, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[OK] 文件消息发送成功 -> receive_id={receive_id}")
            return {"ok": True, "data": result.get("data", {})}
        else:
            print(f"[ERR] 文件消息发送失败: {result}")
            return {"ok": False, "error": result}

    def send_post_message(self, receive_id: str, title: str, content_lines: list, receive_id_type: str = "chat_id") -> dict:
        """通过应用API发送富文本消息（替代webhook，使用同一chat_id）"""
        token = self.get_tenant_access_token()
        url = f"{self.base_url}/im/v1/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        params = {"receive_id_type": receive_id_type}

        content_parts = []
        for line in content_lines:
            if not line.strip():
                content_parts.append([{"tag": "text", "text": "\n"}])
            else:
                content_parts.append([{"tag": "text", "text": line}])

        body = {
            "receive_id": receive_id,
            "msg_type": "post",
            "content": json.dumps({
                "zh_cn": {
                    "title": title,
                    "content": content_parts
                }
            })
        }
        resp = requests.post(url, headers=headers, params=params, json=body, timeout=15)
        result = resp.json()
        if result.get("code") == 0:
            print(f"[OK] 富文本消息发送成功 -> receive_id={receive_id}")
            return {"ok": True}
        else:
            print(f"[ERR] 富文本消息发送失败: {result}")
            return {"ok": False, "error": result}

    def upload_and_send_excel(self, file_path: str, chat_id: str) -> dict:
        """一键上传Excel并发送到群聊"""
        try:
            file_key = self.upload_file(file_path)
            return self.send_file_message(chat_id, file_key)
        except Exception as e:
            print(f"[ERR] 上传并发送Excel失败: {e}")
            return {"ok": False, "error": str(e)}


# ==================== Webhook 文字推送（保留兼容） ====================

def send_text(webhook_url: str, text: str) -> dict:
    payload = {"msg_type": "text", "content": {"text": text}}
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        result = resp.json()
        if result.get("code", 0) != 0:
            return {"ok": False, "error": result}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_post(webhook_url: str, title: str, content_lines: list) -> dict:
    content_parts = []
    for line in content_lines:
        if not line.strip():
            content_parts.append([{"tag": "text", "text": "\n"}])
        else:
            content_parts.append([{"tag": "text", "text": line}])
    payload = {
        "msg_type": "post",
        "content": {"post": {"zh_cn": {"title": title, "content": content_parts}}}
    }
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        result = resp.json()
        if result.get("code", 0) != 0:
            return {"ok": False, "error": result}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ==================== 报告摘要推送（整合版） ====================

def send_report_summary(config: dict, data: dict, output_path: str, date_str: str) -> dict:
    """
    发送报告摘要到飞书
    - 文字摘要：通过 Webhook 或 应用API
    - Excel文件：通过 应用API 上传（需要 app_id/app_secret/chat_id）
    """
    stats = data.get("stats", {})
    top5_list = data.get("top5_list", [])
    summary = data.get("summary", None)

    feishu_cfg = config.get("feishu", {})
    webhook_url = feishu_cfg.get("webhook_url", "")
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    chat_id = feishu_cfg.get("chat_id", "")

    # 1. 读取历史摘要（由 heat_tracker 模块无条件保存）
    history = _load_history()
    past_date = _find_5_days_ago(history, date_str)
    past_sectors = {s["板块"]: s for s in history.get(past_date, [])} if past_date else {}

    # 2. 构建文字摘要
    lines = []
    lines.append(f"📊 韭研概念打分报告 | {date_str}")
    lines.append(f"打分池: {stats.get('pool_count', 0)} 只 | 行情成功: {stats.get('market_success', 0)} 只 | 失败: {stats.get('market_failed', 0)} 只")
    lines.append(f"板块数: {stats.get('sector_count', 0)} | 得分范围: {stats.get('score_range', 'N/A')} | 平均: {stats.get('score_avg', 0)}")
    heat_top = stats.get('heat_top_sector', '')
    if heat_top:
        lines.append(f"🔥热度最高: {heat_top}")
    lines.append("")

    if summary is not None and not summary.empty and '板块' in summary.columns:
        has_heat = "热度分" in summary.columns
        lines.append(f"📈 板块热度追踪 (对比: {past_date or '无历史'})")
        lines.append("")
        for _, row in summary.iterrows():
            sector = str(row.get('板块', '-'))
            rank = int(row.get('排名', 0)) if pd.notna(row.get('排名')) else 0
            zt = int(row.get('涨停数', 0)) if pd.notna(row.get('涨停数')) else 0

            if has_heat:
                heat = row.get('热度分', 0)
                trend = row.get('趋势', '')
                past = past_sectors.get(sector)
                if past:
                    zt_arrow, _ = _delta_arrow(zt, past.get('涨停数'))
                    heat_arrow, _ = _delta_arrow(heat, past.get('热度分'))
                    zt_str = f" 涨停{zt}{zt_arrow}"
                    heat_str = f" 热度{heat}{heat_arrow}"
                else:
                    zt_str = f" 涨停{zt}"
                    heat_str = f" 热度{heat}"
                lines.append(f"{trend} {sector}  #{rank}{heat_str}{zt_str}")
            else:
                avg = float(row.get('平均分', 0)) if pd.notna(row.get('平均分')) else 0.0
                past = past_sectors.get(sector)
                if past:
                    rank_arrow, _ = _delta_arrow(rank, past.get('排名', rank), reverse=True)
                    zt_arrow, _ = _delta_arrow(zt, past.get('涨停数', zt))
                    rank_str = f"排名{rank}{rank_arrow}"
                    zt_str = f"涨停{zt}{zt_arrow}"
                else:
                    rank_str = f"排名{rank}新"
                    zt_str = f"涨停{zt}"
                lines.append(f"{sector} {rank_str} 均分{avg:.1f} {zt_str}")
        lines.append("")

    if top5_list:
        lines.append("🏆 TOP5 精选:")
        for item in top5_list:
            rank = item.get('排名', '-')
            code = item.get('代码', '-')
            name = item.get('名称', '-')
            score = item.get('总得分', 0)
            sector = item.get('所属板块', item.get('板块', '未知'))
            lines.append(f"  #{rank} {code} {name} | 得分: {score} | 板块: {sector}")
        lines.append("")

    lines.append(f"📁 详细报告 Excel 已生成: {output_path}")
    title = f"【韭研概念打分报告】{date_str}"

    # 3. 推送文字摘要
    text_result = None
    if app_id and app_secret and chat_id:
        # 优先使用应用API发送文字（确保和文件发送到同一群）
        try:
            client = FeishuAppClient(app_id, app_secret)
            text_result = client.send_post_message(chat_id, title, lines)
            print(f"[OK] 文字摘要通过应用API推送成功")
        except Exception as e:
            print(f"[WARN] 应用API文字推送失败，fallback到Webhook: {e}")
            text_result = None

    if (text_result is None or not text_result.get("ok")) and webhook_url:
        text_result = send_post(webhook_url, title, lines)

    # 4. 上传 Excel 文件（应用API）
    file_result = None
    if app_id and app_secret and chat_id and os.path.exists(output_path):
        try:
            print(f"\n[上传] 正在通过飞书应用API上传 Excel...")
            client = FeishuAppClient(app_id, app_secret)
            file_result = client.upload_and_send_excel(output_path, chat_id)
        except Exception as e:
            print(f"[ERR] Excel上传失败: {e}")
            file_result = {"ok": False, "error": str(e)}
    elif not (app_id and app_secret and chat_id):
        print(f"[INFO] 未配置飞书应用API（app_id/app_secret/chat_id），跳过Excel上传")
    elif not os.path.exists(output_path):
        print(f"[WARN] Excel文件不存在: {output_path}")

    return {
        "ok": text_result.get("ok", False) if text_result else False,
        "text_ok": text_result.get("ok", False) if text_result else False,
        "file_ok": file_result.get("ok", False) if file_result else False,
        "file_error": file_result.get("error") if file_result else None
    }


def send_error_alert(config: dict, error_msg: str, date_str: str) -> dict:
    feishu_cfg = config.get("feishu", {})
    webhook_url = feishu_cfg.get("webhook_url", "")
    app_id = feishu_cfg.get("app_id", "")
    app_secret = feishu_cfg.get("app_secret", "")
    chat_id = feishu_cfg.get("chat_id", "")

    text = f"⚠️ 【韭研报告告警】{date_str}\n报告生成失败或异常:\n{error_msg}\n请检查脚本日志。"

    # 优先应用API
    if app_id and app_secret and chat_id:
        try:
            client = FeishuAppClient(app_id, app_secret)
            return client.send_post_message(chat_id, "【韭研报告告警】", [text])
        except Exception:
            pass

    if webhook_url:
        return send_text(webhook_url, text)
    return {"ok": False}


