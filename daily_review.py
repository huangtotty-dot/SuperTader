# -*- coding: utf-8 -*-
"""
做T每日复盘分析脚本 (v1.11)
功能：
1. 分析当日信号触发质量（延迟/提前/错过）
2. 统计早盘冲高机会和实际触发情况
3. 生成复盘报告（Markdown + 控制台输出）
4. 输出参数调优建议

使用方法：
    python daily_review.py --date 2026-06-23
    python daily_review.py  # 默认今天
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

# 确保能找到日志增强模块
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

try:
    import log_enhancer as le
except ImportError:
    print("⚠️  log_enhancer.py 未找到，请确保在同一目录")
    sys.exit(1)


def parse_time(t_str: str) -> datetime:
    """解析时间字符串"""
    return datetime.strptime(t_str, "%Y-%m-%d %H:%M:%S")


def time_diff_seconds(t1_str: str, t2_str: str) -> int:
    """计算两个时间字符串的差值（秒）"""
    try:
        t1 = parse_time(t1_str)
        t2 = parse_time(t2_str)
        return int((t2 - t1).total_seconds())
    except Exception:
        return 0


def load_decision_trace(day: str) -> List[dict]:
    """加载决策trace"""
    return le.read_jsonl(os.path.join(le.TRACE_DIR, f"decision_trace_{day}.jsonl"))


def load_shadow_signals(day: str) -> List[dict]:
    """加载shadow signals"""
    return le.read_jsonl(os.path.join(le.TRACE_DIR, f"shadow_signals_{day}.jsonl"))


def load_morning_surge(day: str) -> List[dict]:
    """加载早盘冲高事件"""
    return le.read_jsonl(os.path.join(le.TRACE_DIR, f"morning_surge_events_{day}.jsonl"))


def load_missed_signals(day: str) -> List[dict]:
    """加载错过信号"""
    return le.read_jsonl(os.path.join(le.TRACE_DIR, f"missed_signals_{day}.jsonl"))


def load_t_advice(day: str) -> List[dict]:
    """加载做T建议"""
    return le.read_jsonl(os.path.join(le.TRACE_DIR, f"t_advice_{day}.jsonl"))


def load_signal_outcome(day: str) -> List[dict]:
    """加载信号结果"""
    return le.read_jsonl(os.path.join(le.TRACE_DIR, f"signal_outcome_{day}.jsonl"))


def extract_per_code_records(records: List[dict]) -> Dict[str, List[dict]]:
    """按股票分组记录"""
    grouped = defaultdict(list)
    for r in records:
        code = r.get("code", "")
        if code:
            grouped[code].append(r)
    return dict(grouped)


def find_optimal_morning_sell(records: List[dict]) -> Optional[dict]:
    """从决策trace中找到早盘最优卖出点（09:30-10:00，最高range_pos+price）"""
    best = None
    best_score = 0.0
    for r in records:
        ts = r.get("scan_time", "")
        if not ("09:30" <= ts[11:16] <= "10:00"):
            continue
        range_pos = r.get("range_pos", 0)
        price = r.get("price", 0)
        vwap = r.get("vwap", price)
        sell_score = r.get("sell_score", 0)
        # 综合评分：range_pos高 + 高于VWAP + sell_score有潜力
        if range_pos > 0.5 and price > vwap:
            score = range_pos * 100 + sell_score
            if score > best_score:
                best_score = score
                best = r
    return best


def find_optimal_morning_buy(records: List[dict]) -> Optional[dict]:
    """从决策trace中找到早盘最优买入点（09:30-10:00，最低price+rsi超卖）"""
    best = None
    best_score = 999.0
    for r in records:
        ts = r.get("scan_time", "")
        if not ("09:30" <= ts[11:16] <= "10:00"):
            continue
        price = r.get("price", 0)
        rsi = r.get("rsi", 50)
        buy_score = r.get("buy_score", 0)
        if buy_score > 0:
            score = price / max(buy_score, 1)  # 价格低且buy_score高的
            if score < best_score:
                best_score = score
                best = r
    return best


def analyze_shadow_signals(day: str) -> str:
    """分析shadow_signals：统计哪些信号"差一点"触发"""
    records = load_shadow_signals(day)
    if not records:
        return "⚠️ 无 shadow_signals 数据"

    per_code = extract_per_code_records(records)

    lines = ["## 一、Shadow Signals 分析（接近触发但未触发的信号）\n"]
    lines.append(f"总计记录数：{len(records)}\n")
    lines.append(f"涉及股票数：{len(per_code)}\n")
    lines.append("")

    for code, code_records in sorted(per_code.items()):
        name = code_records[0].get("name", code)
        buy_missed = [r for r in code_records if r.get("best_signal_type") == "buy"]
        sell_missed = [r for r in code_records if r.get("best_signal_type") == "sell"]

        if buy_missed:
            closest = min(buy_missed, key=lambda r: r.get("distance_to_buy_threshold", 999))
            lines.append(f"**{name}({code})** BUY_LOW 最接近：")
            lines.append(f"  - 时间：{closest.get('scan_time', 'N/A')}")
            lines.append(f"  - buy_score：{closest.get('buy_score', 0)} / threshold：{closest.get('buy_threshold', 0)}")
            lines.append(f"  - 距离触发：{closest.get('distance_to_buy_threshold', 0)} 分")
            lines.append(f"  - 原因：{closest.get('miss_reason', 'N/A')}")
            lines.append("")

        if sell_missed:
            closest = min(sell_missed, key=lambda r: r.get("distance_to_sell_threshold", 999))
            lines.append(f"**{name}({code})** SELL_HIGH 最接近：")
            lines.append(f"  - 时间：{closest.get('scan_time', 'N/A')}")
            lines.append(f"  - sell_score：{closest.get('sell_score', 0)} / threshold：{closest.get('sell_threshold', 0)}")
            lines.append(f"  - 距离触发：{closest.get('distance_to_sell_threshold', 0)} 分")
            lines.append(f"  - 原因：{closest.get('miss_reason', 'N/A')}")
            lines.append("")

    return "\n".join(lines)


def analyze_morning_surge(day: str) -> str:
    """分析早盘冲高事件"""
    records = load_morning_surge(day)
    if not records:
        return "## 二、早盘冲高分析\n\n⚠️ 无 morning_surge_events 数据（需升级到v1.11后才能记录）\n"

    per_code = extract_per_code_records(records)

    lines = ["## 二、早盘冲高分析\n"]
    lines.append(f"总计早盘事件：{len(records)}\n")
    lines.append("")

    for code, code_records in sorted(per_code.items()):
        name = code_records[0].get("name", code)
        triggered = [r for r in code_records if r.get("stage") == "triggered"]
        detected = [r for r in code_records if r.get("stage") == "detected"]
        missed = [r for r in code_records if r.get("stage") == "missed"]

        lines.append(f"**{name}({code})**：")
        if triggered:
            t = triggered[-1]
            lines.append(f"  ✅ 触发信号：{t.get('ts', 'N/A')} | 价格：{t.get('price', 0):.2f} | 涨：{t.get('today_ret', 0)*100:.2f}%")
        if detected:
            d = detected[0]
            lines.append(f"  ⚠️ 检测到但未触发：{d.get('ts', 'N/A')} | 价格：{d.get('price', 0):.2f} | 距离阈值：{d.get('distance_to_threshold', 0)} 分")
        if missed:
            lines.append(f"  ❌ 完全错过（未检测）：{len(missed)} 次")
        lines.append("")

    return "\n".join(lines)


def analyze_signal_latency(day: str) -> str:
    """分析信号延迟情况"""
    records = le.read_jsonl(os.path.join(le.TRACE_DIR, f"signal_latency_{day}.jsonl"))
    if not records:
        return "## 三、信号延迟分析\n\n⚠️ 无 signal_latency 数据（需升级到v1.11后才能记录）\n"

    lines = ["## 三、信号延迟分析\n"]
    lines.append(f"总计延迟记录：{len(records)}\n")
    lines.append("")

    for r in records:
        lines.append(f"**{r.get('name', '')}({r.get('code', '')})** {r.get('action', '')}：")
        lines.append(f"  最优时间：{r.get('optimal_time', 'N/A')} 价格：{r.get('optimal_price', 0):.2f}")
        lines.append(f"  实际时间：{r.get('actual_time', 'N/A')} 价格：{r.get('actual_price', 0):.2f}")
        lines.append(f"  延迟：{r.get('latency_seconds', 0)} 秒 | 滑点：{r.get('price_slippage_pct', 0)*100:.2f}%")
        lines.append(f"  原因：{r.get('reason', 'N/A')}")
        lines.append("")

    return "\n".join(lines)


def generate_optimal_t_plan(day: str) -> str:
    """基于决策trace生成最优做T方案（理论最优）"""
    records = load_decision_trace(day)
    if not records:
        return "## 四、理论最优做T方案\n\n⚠️ 无决策trace数据\n"

    per_code = extract_per_code_records(records)

    lines = ["## 四、理论最优做T方案（基于日内高低点）\n"]
    lines.append("")

    for code, code_records in sorted(per_code.items()):
        name = code_records[0].get("name", code)
        prices = [r.get("price", 0) for r in code_records if r.get("price", 0) > 0]
        if not prices:
            continue

        high_price = max(prices)
        low_price = min(prices)
        high_time = next((r.get("scan_time", "") for r in code_records if r.get("price", 0) == high_price), "")
        low_time = next((r.get("scan_time", "") for r in code_records if r.get("price", 0) == low_price), "")
        vwap = code_records[0].get("vwap", 0)

        # 理论T空间
        t_space = (high_price - low_price) / low_price * 100 if low_price > 0 else 0

        lines.append(f"**{name}({code})**：")
        lines.append(f"  高点：{high_price:.2f} @ {high_time[11:16] if len(high_time) > 16 else high_time}")
        lines.append(f"  低点：{low_price:.2f} @ {low_time[11:16] if len(low_time) > 16 else low_time}")
        lines.append(f"  VWAP：{vwap:.2f}")
        lines.append(f"  理论T空间：{t_space:.2f}%")

        # 找最优卖出点（早盘冲高+高于VWAP）
        optimal_sell = find_optimal_morning_sell(code_records)
        if optimal_sell:
            lines.append(f"  🎯 最优卖出点：{optimal_sell.get('price', 0):.2f} @ {optimal_sell.get('scan_time', '')[11:16]}")
            lines.append(f"     range_pos：{optimal_sell.get('range_pos', 0):.2f} | sell_score：{optimal_sell.get('sell_score', 0)}")

        lines.append("")

    return "\n".join(lines)


def generate_param_suggestions(day: str) -> str:
    """基于分析结果生成参数调优建议"""
    records = load_shadow_signals(day)
    if not records:
        return "## 五、参数调优建议\n\n⚠️ 数据不足，无法生成建议\n"

    # 统计各种miss_reason
    miss_reasons = defaultdict(int)
    for r in records:
        miss_reasons[r.get("miss_reason", "unknown")] += 1

    lines = ["## 五、参数调优建议\n"]

    # 1. 分析最常见的错过原因
    top_reasons = sorted(miss_reasons.items(), key=lambda x: x[1], reverse=True)[:5]
    lines.append("### 5.1 最常见的信号错过原因\n")
    for reason, count in top_reasons:
        lines.append(f"- {reason}：{count} 次")
    lines.append("")

    # 2. 基于今天的数据给建议
    lines.append("### 5.2 针对性建议\n")

    # 检查是否有大量"接近阈值但未触发"的情况
    near_threshold = miss_reasons.get("接近阈值但未触发", 0)
    if near_threshold > len(records) * 0.3:
        lines.append("⚠️ **超过30%的信号接近阈值但未触发**，建议：")
        lines.append("  - 降低 buy_threshold / sell_threshold 门槛（-2~3分）")
        lines.append("  - 或降低 buy_confirm_min_score（-3~5分）")
        lines.append("")

    # 检查time_score相关
    time_blocked = sum(1 for r in records if r.get("scan_time", "")[11:16] < "09:46")
    if time_blocked > 5:
        lines.append("⚠️ **早盘有多个信号被时间窗口抑制**，建议：")
        lines.append("  - 确认time_score设置是否合理（当前09:30-09:35为+8，09:36-09:45为0）")
        lines.append("")

    lines.append("### 5.3 长期调优建议\n")
    lines.append('- 持续观察"早盘冲高"因子的触发效果，如果误报多则提高today_ret阈值（>0.008）')
    lines.append('- 如果下午回落接回信号不足，可降低"下午回落"因子的RSI要求（从25到30）')
    lines.append("")

    return "\n".join(lines)


def generate_daily_report(day: str) -> str:
    """生成完整每日复盘报告"""
    lines = [
        f"# 做T每日复盘报告 ({day})",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "---",
        "",
    ]

    lines.append(analyze_shadow_signals(day))
    lines.append("")
    lines.append(analyze_morning_surge(day))
    lines.append("")
    lines.append(analyze_signal_latency(day))
    lines.append("")
    lines.append(generate_optimal_t_plan(day))
    lines.append("")
    lines.append(generate_param_suggestions(day))
    lines.append("")
    lines.append("---")
    lines.append("*报告由 daily_review.py v1.11 自动生成*")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="做T每日复盘分析")
    parser.add_argument("--date", type=str, help="分析日期 (YYYY-MM-DD)，默认今天")
    parser.add_argument("--output", type=str, help="输出报告路径，默认 t_io/reviews/")
    args = parser.parse_args()

    day = args.date or datetime.now().strftime("%Y-%m-%d")

    print(f"[DailyReview] 正在生成 {day} 的复盘报告...")

    report = generate_daily_report(day)

    # 保存报告
    output_dir = args.output or le.REVIEW_DIR
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"daily_review_{day}.md")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"[DailyReview] 报告已保存：{output_path}")
    # 避免Windows控制台编码问题，不直接print中文emoji
    try:
        print(report)
    except UnicodeEncodeError:
        print("[DailyReview] 报告内容已保存到文件，控制台编码不支持直接显示")
        pass


if __name__ == "__main__":
    main()
