# -*- coding: utf-8 -*-
"""
飞书通知模块
"""
import json
import time
import urllib.request
from typing import Dict, List

from config import (
    log, FEISHU_ENABLED, FEISHU_WEBHOOK, FEISHU_AT_ALL, FEISHU_STRONG_NOTIFY,
    MAX_SIGNALS_PER_SECTOR_FEISHU, FEISHU_MAX_CARD_BYTES, FEISHU_ENABLE_SECTOR_DETAIL,
    PRIORITY_ORDER, SIGNAL_EMOJI
)

from utils import (
    group_signals_by_sector, current_amount_range_text, format_sector_chain,
    truncate_reason
)
from weekly_strategies import sector_top3_brief
from stock_pool import wrap_business_text

def post_feishu_payload(payload: Dict) -> bool:
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return False

    req = urllib.request.Request(
        FEISHU_WEBHOOK,
        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'}
    )
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                if result.get('code') == 0:
                    return True
                msg = str(result.get('msg', ''))
                log.debug(f"⚠️  飞书推送返回: {msg}")
                if "30KB" in msg or "size limit" in msg.lower():
                    return False
                return True
        except Exception as e:
            if attempt < max_retries:
                wait_time = 2 ** attempt
                log.debug(f"⚠️  飞书推送第{attempt}次失败，{wait_time}秒后重试: {str(e)[:60]}")
                time.sleep(wait_time)
            else:
                log.debug(f"⚠️  飞书推送异常（已重试{max_retries}次）: {str(e)[:60]}")
    return False


def payload_size(payload: Dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))


def send_to_feishu(signals: List[Dict], scan_date: str, total: int, success_count: int, scan_mode: str = "daily", status_note: str = ""):
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return

    def post_payload(payload: Dict) -> bool:
        return post_feishu_payload(payload)

    def build_compact_overview(mode_tag: str, grouped: List[tuple], sorted_signals: List[Dict]) -> str:
        """构建紧凑的总览消息，避免重复"""
        top3 = sector_top3_brief(grouped)
        amount_range_text = current_amount_range_text("weekly" if mode_tag.startswith("周线") else "daily")

        # 构建信号摘要（只显示关键信息，避免重复）
        signal_summary = []
        for idx, s in enumerate(sorted_signals[:8], 1):
            emoji = SIGNAL_EMOJI.get(s['type'], '')
            name_code = f"{s['name']}({s['code']})"
            price = f"{s['price']:.2f}"

            # 添加涨幅信息
            daily_pct = float(s.get("daily_pct_change", 0) or 0)
            pct_emoji = "📈" if daily_pct >= 0 else "📉"
            pct_text = f"{pct_emoji} {daily_pct:+.2f}%"

            week_total_amount = float(s.get("week_total_amount", 0) or 0)
            week_avg_amount = float(s.get("week_avg_amount", 0) or 0)
            week_total_source = str(s.get("week_total_source", "unknown") or "unknown")
            amount_text = f"{week_total_amount / 100000000:.2f}亿" if week_total_amount > 0 else f"{float(s.get('amount', 0) or 0) / 100000000:.2f}亿"

            sector_full = format_sector_chain(s.get('sector', ''))

            business_summary = s.get("business_summary", "")
            business_display = f"\n业：{wrap_business_text(business_summary, width=18, max_lines=4)}" if business_summary else ""
            week_display = f"\n周总额：{amount_text}" if mode_tag.startswith("周线") else ""
            if mode_tag.startswith("周线") and week_avg_amount > 0:
                week_display += f" | 周均：{week_avg_amount / 100000000:.2f}亿"
            if mode_tag.startswith("周线"):
                week_display += f" | 来源：{week_total_source}"

            concept_display = f"📊 行业：{sector_full}"

            signal_summary.append(
                f"{emoji} {name_code} {price} {pct_text}{week_display}\n{concept_display}{business_display}\n{s['type']}"
            )

        overview = (
            f"📅 {scan_date} | {mode_tag} | 成交额区间：{amount_range_text}\n"
            f"🎯 {len(sorted_signals)}个信号 | ✅{success_count}/{total}\n"
            f"🏆 {top3}\n"
            f"\n{' | '.join(signal_summary)}"
        )
        if len(sorted_signals) > 8:
            overview += f"\n... 还有 {len(sorted_signals) - 8} 个信号"

        return overview

    def build_sector_card_compact(scan_date: str, sector: str, sector_signals: List[Dict]) -> Dict:
        """构建紧凑的板块卡片，减少冗余信息"""
        sector_signals = sorted(sector_signals, key=lambda s: (PRIORITY_ORDER.get(s['type'], 99), s.get('code', '')))
        card_elements = []
        amount_range_text = current_amount_range_text("weekly" if scan_mode.startswith("weekly") else "daily")

        # 日期和板块标题（突出显示）
        card_elements.append({
            "tag": "div",
            "text": {
                "content": f"📅 {scan_date} | 🔥 {sector} | 成交额区间：{amount_range_text}",
                "tag": "lark_md"
            }
        })
        card_elements.append({"tag": "hr"})

        # 信号列表（紧凑格式）
        for idx, s in enumerate(sector_signals, 1):
            emoji = SIGNAL_EMOJI.get(s['type'], "")
            week_total_amount = float(s.get("week_total_amount", 0) or 0)
            week_avg_amount = float(s.get("week_avg_amount", 0) or 0)
            amount_value = week_total_amount if week_total_amount > 0 else float(s.get('amount', 0) or 0)
            amount_text = f" {amount_value / 100000000:.2f}亿" if amount_value > 0 else ""

            # 添加涨幅信息
            daily_pct = float(s.get("daily_pct_change", 0) or 0)
            pct_emoji = "📈" if daily_pct >= 0 else "📉"
            pct_display = f" {pct_emoji} {daily_pct:+.2f}%"

            sector_full = format_sector_chain(s.get('sector', ''))

            business_summary = s.get("business_summary", "")
            business_line = wrap_business_text(business_summary, width=20, max_lines=4) if business_summary else "未知"

            signal_line = f"{emoji} {s['name']}({s['code']}) {s['price']:.2f}{pct_display}{amount_text}"
            if scan_mode.startswith("weekly") and week_avg_amount > 0:
                signal_line += f" | 周均 {week_avg_amount / 100000000:.2f}亿"
            concept_line = f"📊 行业：{sector_full}"
            strategy_line = f"🧭 {s['type']}"
            reason_line = truncate_reason(s['reason'], limit=40)
            week_total_source = str(s.get("week_total_source", "unknown") or "unknown")
            week_total_line = f"📈 周总额：{amount_text.strip()} | 来源：{week_total_source}" if scan_mode.startswith("weekly") and amount_text.strip() else ""
            body_lines = [signal_line]
            if week_total_line:
                body_lines.append(week_total_line)
            body_lines.extend([concept_line, f"📝 业：{business_line}", strategy_line, reason_line])

            card_elements.append({
                "tag": "div",
                "text": {
                    "content": "\n".join(body_lines),
                    "tag": "lark_md"
                }
            })

        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "elements": card_elements
            },
            "notify_type": 1
        }

    def split_chunks(items: List[Dict], chunk_size: int) -> List[List[Dict]]:
        if chunk_size <= 0:
            return [items]
        return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]

    try:
        sorted_signals = sorted(signals, key=lambda s: (PRIORITY_ORDER.get(s['type'], 99), s.get('sector', ''), s.get('code', '')))
        grouped = group_signals_by_sector(sorted_signals)
        mode_tag = "周线轻量" if scan_mode == "weekly_light" else ("周线" if scan_mode.startswith("weekly") else "日线")

        if not sorted_signals:
            msg_text = f"📅 {scan_date} | {mode_tag}\n✅ {success_count}/{total}\n\n暂无信号"
            payload = {"msg_type": "text", "content": {"text": msg_text}}
            if post_payload(payload):
                log.debug("✓ 飞书推送成功 (0 个信号)")
            return

        # 发送紧凑的总览消息
        overview_payload = {
            "msg_type": "text",
            "content": {"text": build_compact_overview(mode_tag, grouped, sorted_signals)}
        }
        if not post_payload(overview_payload):
            log.debug("⚠️  飞书总览消息发送失败")

        # 发送板块详情（只发送有信号的板块，不重复发送日期信息）
        if FEISHU_ENABLE_SECTOR_DETAIL and sorted_signals:
            for sector, sector_signals in grouped:
                chunks = split_chunks(sector_signals, max(1, MAX_SIGNALS_PER_SECTOR_FEISHU))

                for chunk in chunks:
                    payload = build_sector_card_compact(scan_date, sector, chunk)
                    size = payload_size(payload)

                    if size > FEISHU_MAX_CARD_BYTES and len(chunk) > 1:
                        log.debug(f"⚠️  板块卡片过大，继续拆分: {sector}, {size} bytes")
                        for item in chunk:
                            single_payload = build_sector_card_compact(scan_date, sector, [item])
                            if not post_payload(single_payload):
                                log.debug(f"⚠️  飞书板块单条发送失败: {sector} {item.get('code', '')}")
                        continue

                    if not post_payload(payload):
                        log.debug(f"⚠️  飞书板块消息发送失败: {sector}")

        log.debug(f"✓ 飞书推送成功 ({len(signals)} 个信号)")

    except Exception as e:
        log.debug(f"⚠️  飞书推送异常: {str(e)[:60]}")

def send_monthly_gain_to_feishu(month_std: str, month_start: str, month_end: str, total_scanned: int, success_count: int, failed_count: int, top_results: List[Dict], top_n: int) -> None:
    if not FEISHU_ENABLED or not FEISHU_WEBHOOK:
        return

    header_text = (
        f"📅 月度涨幅统计 {month_std}\n"
        f"区间：{month_start} ~ {month_end}\n"
        f"总扫描：{total_scanned} | 成功：{success_count} | 失败：{failed_count}\n"
        f"TOP{min(top_n, len(top_results))}"
    )
    post_feishu_payload({"msg_type": "text", "content": {"text": header_text}})

    if not top_results:
        post_feishu_payload({"msg_type": "text", "content": {"text": "暂无结果"}})
        return

    chunk_size = 5
    total_chunks = (len(top_results) + chunk_size - 1) // chunk_size
    for chunk_idx in range(total_chunks):
        start = chunk_idx * chunk_size
        chunk = top_results[start:start + chunk_size]
        lines = []
        for idx, item in enumerate(chunk, start + 1):
            business = str(item.get("business_summary", "")).replace("\n", " ").strip()
            sector_text = str(item.get("sector", "")).replace("\n", " ").strip()
            lines.append(
                f"{idx}. {item['name']}({item['code']}) {item['gain_pct']:.2f}%\n"
                f"   起点 {item['base_price']:.2f} → 终点 {item['end_price']:.2f}\n"
                f"   板块：{sector_text}\n"
                f"   业务：{business if business else '未知'}"
            )
        payload = {
            "msg_type": "text",
            "content": {"text": f"TOP明细（{chunk_idx + 1}/{total_chunks}）\n" + "\n".join(lines)},
        }
        post_feishu_payload(payload)
