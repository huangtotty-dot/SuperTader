#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
做T效果每日检查脚本（方案A验证）
集成到 doc/每日复盘/每日Review.md 的§4.5

用法：
    python t_io/validation/daily_review/check_optimization_effect.py --date 2026-08-25

输出：自动生成的每日检查报告，内嵌到 Review.md 的"做T优化效果"段落
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

# ==================== 路径配置 ====================
BASE_DIR = Path(__file__).resolve().parent.parent.parent.parent
T_IO_DIR = BASE_DIR / "t_io"
TRACES_DIR = T_IO_DIR / "traces"
REVIEWS_DIR = BASE_DIR / "doc" / "每日复盘"

# ==================== 全局参数 ====================
# 方案A监控的目标标的
TARGET_ETF_CODES = {"588170", "300153"}  # 应该推送增加的
BASELINE_ETF_CODES = {"600481", "000988"}  # 作为对照的个股


def get_today_str(date_str=None):
    """解析日期字符串，返回 YYYY-MM-DD 格式"""
    if date_str:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def load_decision_trace(date_str):
    """加载当日 decision_trace，返回按标的分组的信号"""
    trace_file = TRACES_DIR / f"decision_trace_{date_str}.jsonl"
    if not trace_file.exists():
        return {}

    signals = defaultdict(lambda: {"BUY": 0, "SELL": 0, "HOLD": 0})

    try:
        with open(trace_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    code = record.get("code", "")
                    decision = record.get("decision", "HOLD")

                    if decision in ["BUY_LOW", "ADD_POS"]:
                        signals[code]["BUY"] += 1
                    elif decision in ["SELL_HIGH", "PANIC_SELL"]:
                        signals[code]["SELL"] += 1
                    else:
                        signals[code]["HOLD"] += 1
                except:
                    pass
    except Exception as e:
        print(f"⚠️ 加载 decision_trace 失败: {e}", file=sys.stderr)

    return dict(signals)


def load_index_resonance_trace(date_str):
    """加载当日 index_resonance_trace，分析推送通过情况"""
    trace_file = TRACES_DIR / f"index_resonance_{date_str}.jsonl"
    if not trace_file.exists():
        return {}

    resonance = defaultdict(lambda: {
        "total": 0,
        "passed": 0,
        "blocked": 0,
        "bypass_reasons": defaultdict(int),
    })

    try:
        with open(trace_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    code = record.get("code", "")
                    action = record.get("action", "")

                    # 只关心 BUY_LOW
                    if action != "BUY_LOW":
                        continue

                    resonance[code]["total"] += 1

                    # 检查是否通过门控
                    gate_pass = record.get("index_ma5_dir", {}).get("pass", False)
                    bypass = record.get("bypass", "")

                    if gate_pass or bypass:
                        resonance[code]["passed"] += 1
                        if bypass:
                            resonance[code]["bypass_reasons"][bypass] += 1
                    else:
                        resonance[code]["blocked"] += 1
                except:
                    pass
    except Exception as e:
        print(f"⚠️ 加载 index_resonance_trace 失败: {e}", file=sys.stderr)

    return dict(resonance)


def compare_with_yesterday(today_str, signals_today, resonance_today):
    """对比昨天的数据，计算推送增长"""
    yesterday = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")

    signals_yesterday = load_decision_trace(yesterday)
    resonance_yesterday = load_index_resonance_trace(yesterday)

    comparison = {}
    for code in TARGET_ETF_CODES | BASELINE_ETF_CODES:
        today_buy = signals_today.get(code, {}).get("BUY", 0)
        yesterday_buy = signals_yesterday.get(code, {}).get("BUY", 0)

        today_passed = resonance_today.get(code, {}).get("passed", 0)
        yesterday_passed = resonance_yesterday.get(code, {}).get("passed", 0)

        comparison[code] = {
            "buy_signals_today": today_buy,
            "buy_signals_yesterday": yesterday_buy,
            "buy_change": today_buy - yesterday_buy,
            "passed_today": today_passed,
            "passed_yesterday": yesterday_passed,
            "passed_change": today_passed - yesterday_passed,
        }

    return comparison


def generate_optimization_effect_report(date_str):
    """生成方案A的效果检查报告（Markdown 格式）"""

    signals = load_decision_trace(date_str)
    resonance = load_index_resonance_trace(date_str)
    comparison = compare_with_yesterday(date_str, signals, resonance)

    lines = []
    lines.append("## 做T优化效果（方案A验证·2026-08-24 起）\n")
    lines.append("> 监控指标：目标 ETF（588170/300153）的推送数变化；对照组（600481/000988）作为基准\n")

    # 1. 推送统计表
    lines.append("\n### 推送统计 vs 前一天\n")
    lines.append("| 标的 | 类型 | 当日BUY信号 | 当日推送通过 | 昨日BUY信号 | 推送增长 | 备注 |")
    lines.append("|------|------|-----------|-----------|-----------|--------|------|")

    for code in sorted(TARGET_ETF_CODES | BASELINE_ETF_CODES):
        code_type = "目标ETF" if code in TARGET_ETF_CODES else "对照个股"
        cmp = comparison.get(code, {})

        buy_today = cmp.get("buy_signals_today", 0)
        buy_yesterday = cmp.get("buy_signals_yesterday", 0)
        passed_today = cmp.get("passed_today", 0)
        change = cmp.get("buy_change", 0)

        change_emoji = "📈" if change > 0 else ("📉" if change < 0 else "➡️")

        lines.append(f"| {code} | {code_type} | {buy_today} | {passed_today} | {buy_yesterday} | {change_emoji} {change:+d} | - |")

    # 2. 方案A状态确认
    lines.append("\n### 方案A状态确认\n")

    schema_a_working = False
    for code in TARGET_ETF_CODES:
        passed = resonance.get(code, {}).get("passed", 0)
        bypass = resonance.get(code, {}).get("bypass_reasons", {}).get("stock_override_disabled", 0)
        if bypass > 0:
            schema_a_working = True
            lines.append(f"- [x] {code}: 检测到 `stock_override_disabled` bypass ({bypass} 次)")
        else:
            lines.append(f"- [ ] {code}: 未检测到 bypass 标记（方案A 可能未生效）")

    if schema_a_working:
        lines.append("\n✅ **方案A 运行中**")
    else:
        lines.append("\n❌ **警告**：方案A 的 `INDEX_RESONANCE_STOCK_OVERRIDE` 未生效")

    # 3. 推送质量对照
    lines.append("\n### 推送质量对照\n")
    lines.append("| 标的 | 总信号 | 推送通过 | 推送率 | 主要拦截原因 |")
    lines.append("|------|--------|---------|--------|--------------|")

    for code in sorted(TARGET_ETF_CODES | BASELINE_ETF_CODES):
        res = resonance.get(code, {})
        total = res.get("total", 0)
        passed = res.get("passed", 0)
        blocked = res.get("blocked", 0)

        if total > 0:
            pass_rate = 100 * passed / total
        else:
            pass_rate = 0

        # 统计拦截原因
        bypass_reasons = res.get("bypass_reasons", {})
        if bypass_reasons:
            top_reason = max(bypass_reasons.items(), key=lambda x: x[1])[0]
        else:
            top_reason = "index_ma5_dir" if blocked > 0 else "N/A"

        lines.append(f"| {code} | {total} | {passed} | {pass_rate:.0f}% | {top_reason} |")

    # 4. 关键结论
    lines.append("\n### 关键结论\n")

    target_total_signals = sum(resonance.get(code, {}).get("total", 0) for code in TARGET_ETF_CODES)
    target_passed_signals = sum(resonance.get(code, {}).get("passed", 0) for code in TARGET_ETF_CODES)
    baseline_total_signals = sum(resonance.get(code, {}).get("total", 0) for code in BASELINE_ETF_CODES)
    baseline_passed_signals = sum(resonance.get(code, {}).get("passed", 0) for code in BASELINE_ETF_CODES)

    lines.append(f"- 目标 ETF 推送总数：{target_passed_signals}/{target_total_signals}（推送率 {100*target_passed_signals/max(1,target_total_signals):.0f}%）")
    lines.append(f"- 对照个股推送总数：{baseline_passed_signals}/{baseline_total_signals}（推送率 {100*baseline_passed_signals/max(1,baseline_total_signals):.0f}%）")

    if target_passed_signals > baseline_passed_signals * 0.8:
        lines.append(f"- ✅ 目标 ETF 推送充足，方案A 有效")
    else:
        lines.append(f"- ⚠️ 目标 ETF 推送较少，需排查配置")

    lines.append("")

    return "\n".join(lines)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="做T效果每日检查")
    parser.add_argument("--date", type=str, help="检查日期 (YYYY-MM-DD)，默认今日")
    args = parser.parse_args()

    date_str = get_today_str(args.date)

    print(f"🔍 生成 {date_str} 的做T优化效果报告...")
    report = generate_optimization_effect_report(date_str)
    print(report)

    # 也可选地保存到单独文件供复盘脚本读取
    report_file = T_IO_DIR / "traces" / f"optimization_effect_{date_str}.txt"
    try:
        with open(report_file, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n✅ 报告已保存：{report_file}")
    except Exception as e:
        print(f"⚠️ 保存报告失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
