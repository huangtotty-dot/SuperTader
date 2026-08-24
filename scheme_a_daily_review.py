#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方案A观察期每日复盘检查清单（2026-08-25 至 2026-08-27）
集成到现有的daily_review.py体系，添加stock_override和虚假信号监控章节

用法: python scheme_a_daily_review.py [--date 2026-08-25]
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent

# 方案A关注的标的
SCHEME_A_CODES = ["588170", "300153"]


def load_traces(date: str):
    """加载指定日期的trace文件"""
    traces = {
        "decision": [],
        "shadow_signals": [],
        "positioning": [],
    }

    # 加载decision_trace
    decision_trace_path = BASE / f"t_io/traces/decision_trace_{date}.jsonl"
    if decision_trace_path.exists():
        with open(decision_trace_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    traces["decision"].append(json.loads(line))
                except:
                    pass

    # 加载shadow_signals
    shadow_signals_path = BASE / f"t_io/traces/shadow_signals_{date}.jsonl"
    if shadow_signals_path.exists():
        with open(shadow_signals_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    traces["shadow_signals"].append(json.loads(line))
                except:
                    pass

    return traces


def analyze_stock_override_impact(traces: dict, codes: list) -> dict:
    """分析stock_override对标的的影响

    §1: 推送数统计 - 对比修复前后
    §2: 拦截原因分布 - 防重桶vs共振vs其他
    §3: 推送时间分布 - 集中度/离散度
    """
    result = {}

    for code in codes:
        code_data = {
            "code": code,
            "total_pushed": 0,
            "buy_pushed": 0,
            "sell_pushed": 0,
            "miss_distribution": defaultdict(int),
            "push_times": [],
            "stock_override_bypasses": 0,
        }

        # 从decision_trace统计推送
        for rec in traces["decision"]:
            if rec.get("code") != code:
                continue
            decision = rec.get("decision")
            if decision in ["BUY_LOW", "ADD_POS"]:
                code_data["buy_pushed"] += 1
                code_data["total_pushed"] += 1
                code_data["push_times"].append(rec.get("scan_time"))
            elif decision in ["SELL_HIGH", "PANIC_SELL"]:
                code_data["sell_pushed"] += 1
                code_data["total_pushed"] += 1
                code_data["push_times"].append(rec.get("scan_time"))

        # 从shadow_signals统计拦截原因
        for rec in traces["shadow_signals"]:
            if rec.get("code") != code:
                continue
            miss_reason = rec.get("miss_reason", "unknown")

            # 分类统计
            if "防重桶" in miss_reason:
                code_data["miss_distribution"]["防重桶"] += 1
            elif "共振" in miss_reason:
                code_data["miss_distribution"]["共振"] += 1
            elif "stock_override" in miss_reason:
                code_data["stock_override_bypasses"] += 1
                code_data["miss_distribution"]["stock_override跳过"] += 1
            elif "MA5" in miss_reason:
                code_data["miss_distribution"]["MA5破线"] += 1
            else:
                code_data["miss_distribution"]["其他"] += 1

        result[code] = code_data

    return result


def analyze_false_signals(date: str) -> dict:
    """分析虚假信号的情况"""
    result = {
        "monitor_status": "not_available",
        "total_signals": 0,
        "checked_signals": 0,
        "false_count": 0,
        "true_count": 0,
        "false_ratio": 0.0,
    }

    # 尝试加载fake_signal_monitor的状态
    monitor_state_path = BASE / "t_io/state/false_signal_monitor.json"
    if monitor_state_path.exists():
        try:
            with open(monitor_state_path, 'r', encoding='utf-8') as f:
                state = json.load(f)
                result["monitor_status"] = "available"
                result["total_false"] = state.get("total_false", 0)
                result["total_true"] = state.get("total_true", 0)
                result["false_ratio"] = state.get("false_ratio", 0.0)
        except Exception as e:
            result["monitor_status"] = f"error: {e}"

    return result


def generate_daily_checklist(date: str) -> str:
    """生成每日复盘检查清单"""
    traces = load_traces(date)
    stock_override_analysis = analyze_stock_override_impact(traces, SCHEME_A_CODES)
    false_signals_analysis = analyze_false_signals(date)

    checklist = f"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                   【方案A观察期每日复盘检查清单】                            ║
║                          日期：{date}                                      ║
╚══════════════════════════════════════════════════════════════════════════════╝

【§1：stock_override 标的推送情况】

"""

    for code, data in stock_override_analysis.items():
        checklist += f"""
标的: {code}
  推送总数:      {data['total_pushed']:>3} 条
    ├─ 买入推送:  {data['buy_pushed']:>3} 条
    └─ 卖出推送:  {data['sell_pushed']:>3} 条

  拦截分布:
"""
        for reason, count in sorted(data["miss_distribution"].items(), key=lambda x: -x[1]):
            checklist += f"    • {reason:.<20} {count:>3} 次\n"

        checklist += f"""
  stock_override绕过: {data['stock_override_bypasses']:>3} 次"""

    checklist += f"""

┌──────────────────────────────────────────────────────────────────────────────┐
│ 【§2：虚假信号监控状态】                                                   │
└──────────────────────────────────────────────────────────────────────────────┘

状态: {false_signals_analysis['monitor_status']}
"""

    if false_signals_analysis['monitor_status'] == 'available':
        checklist += f"""
有效信号:   {false_signals_analysis.get('total_true', 0):>3} 条
虚假信号:   {false_signals_analysis.get('total_false', 0):>3} 条
虚假比例:   {false_signals_analysis.get('false_ratio', 0.0):>6.2%}

状态判断:   """ + ("✅ 正常" if false_signals_analysis.get('false_ratio', 0) < 0.05
                      else "⚠️ 警告" if false_signals_analysis.get('false_ratio', 0) < 0.10
                      else "❌ 触发回退")

    checklist += f"""

┌──────────────────────────────────────────────────────────────────────────────┐
│ 【§3：观察期行动清单】                                                     │
└──────────────────────────────────────────────────────────────────────────────┘

Day 1 (2026-08-25) 启用方案A:
  ✅ 检查项：
     □ 588170 推送数是否 >0（对比修复前的0次）
     □ 300153 推送数是否正常（对比修复前的拦截情况）
     □ 是否有 stock_override 绕过记录
     □ 虚假信号监控是否正常初始化

  📊 期望结果：
     • 588170 推送从0恢复到 6+ 条（修复前36次拦截→现推送）
     • 300153 推送数恢复到正常水平
     • stock_override 绕过计数 > 0
     • 虚假比例初值记录存档

Day 2 (2026-08-26) 检查虚假信号表现:
  ✅ 检查项：
     □ Day 1 推送的信号是否有虚假（下跌>3%）
     □ 虚假信号后续1小时内最低价对比推送价
     □ 虚假比例是否 < 5%
     □ 是否需要触发回退机制

  📊 期望结果：
     • 虚假信号 < 5%（满足继续条件）
     • 无需触发回退
     • 信号质量稳定

Day 3 (2026-08-27) 最终评估决策:
  ✅ 检查项：
     □ 虚假信号3日累计比例
     □ 588170/300153 推送数累计
     □ 是否需要调整参数或回退
     □ 风险等级评估

  📊 期望结果：
     • 虚假 < 5%：继续执行方案A
     • 虚假 5%-10%：触发参数调整（评分降低）
     • 虚假 > 10%：彻底回退方案A

  ⚠️ 决策树：
     └─ 虚假<5%
        ├─ YES → 方案A 继续执行 ✅
        └─ NO  → 回退并分析原因 ❌

┌──────────────────────────────────────────────────────────────────────────────┐
│ 【§4：关键监控指标汇总表】                                                 │
└──────────────────────────────────────────────────────────────────────────────┘

指标名称                    Day 1 期望        Day 2 期望        Day 3 决策
─────────────────────────────────────────────────────────────────────────────
588170推送数                 >0              稳定增长          6+
300153推送数                 正常            稳定正常          正常
stock_override绕过          >0              递增              N次
虚假信号比例                初值记录        <5%              <5% ✅
推送总数(日)                6+              12+              18+
系统状态                    正常启用        实时监控          最终评估

┌──────────────────────────────────────────────────────────────────────────────┐
│ 【§5：自动化检查脚本调用】                                                 │
└──────────────────────────────────────────────────────────────────────────────┘

每日08:30 启动复盘前，自动执行：
  python scheme_a_daily_review.py --date {date}

Day 3 下午 14:00，执行最终决策：
  python scheme_a_daily_review.py --date {date} --finalize

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【数据来源】
  decision_trace_{date}.jsonl     - 推送决策日志
  shadow_signals_{date}.jsonl     - 被拦截信号日志
  false_signal_monitor.json       - 虚假信号监控状态

【持久化输出】
  scheme_a_daily_review_{date}.json  - 本日复盘结果

"""

    return checklist


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"),
                        help="复盘日期，默认今天")
    parser.add_argument("--finalize", action="store_true", help="Day 3最终决策模式")

    args = parser.parse_args()
    date = args.date

    # 生成检查清单
    checklist = generate_daily_checklist(date)
    print(checklist)

    # 保存到文件
    output_path = BASE / f"scheme_a_daily_review_{date}.json"
    output_dir = output_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成JSON格式的结果
    result = {
        "date": date,
        "generated_at": datetime.now().isoformat(),
        "checklist_markdown": checklist,
        "stock_override_analysis": analyze_stock_override_impact(load_traces(date), SCHEME_A_CODES),
        "false_signals_analysis": analyze_false_signals(date),
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\n📄 复盘结果已保存: {output_path}\n")

    # Day 3最终决策
    if args.finalize:
        false_ratio = result["false_signals_analysis"].get("false_ratio", 0.0)

        print("\n" + "=" * 80)
        print("【Day 3 最终决策】")
        print("=" * 80)

        if false_ratio < 0.05:
            print(f"✅ 虚假信号比例 {false_ratio:.2%} < 5%")
            print("✅ 决策：方案A 继续执行，监控期结束")
            print("\n后续行动：")
            print("  1. 归档观察期数据")
            print("  2. 考虑逐步扩展到其他stock_override标的")
            print("  3. 继续保持虚假信号监控（背景运行）")
            return 0
        elif false_ratio < 0.10:
            print(f"⚠️ 虚假信号比例 {false_ratio:.2%}，5%-10% 警告区间")
            print("⚠️ 决策：触发参数调整")
            print("\n后续行动：")
            print("  1. 降低信号评分阈值（深水评分从55改为60）")
            print("  2. 增强拦截力度")
            print("  3. 继续监控1天，确认调整是否有效")
            return 1
        else:
            print(f"❌ 虚假信号比例 {false_ratio:.2%} > 10%")
            print("❌ 决策：彻底回退方案A")
            print("\n后续行动：")
            print("  1. 禁用 stock_override（enabled=True）")
            print("  2. 恢复原始共振门控")
            print("  3. 分析失败原因")
            print("  4. 设计改进方案")
            return 2


if __name__ == "__main__":
    sys.exit(main())
