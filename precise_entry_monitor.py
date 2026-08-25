# -*- coding: utf-8 -*-
"""
precise_entry_monitor.py — 精准买入监控面板
持续追踪摩恩电气的三层条件达成情况，给出每日的买入就绪度评估

用法:
  python precise_entry_monitor.py --code 002451 --watch         自动每日监控并报告
  python precise_entry_monitor.py --code 002451 --check-all    检查最近30天的每一天
  python precise_entry_monitor.py --code 002451 --date 2026-08-28  检查特定日期
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from precise_entry_framework import PreciseEntryValidator
from position_builder import fetch_daily_kline

MONITOR_FILE = BASE / "t_io" / "state" / "precise_entry_monitor.json"


def _load_monitor_state() -> dict:
    """读取监控状态"""
    try:
        if MONITOR_FILE.exists():
            return json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"last_check_date": None, "history": {}}


def _save_monitor_state(state: dict):
    """保存监控状态"""
    try:
        MONITOR_FILE.parent.mkdir(parents=True, exist_ok=True)
        MONITOR_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def generate_daily_report(code: str, date_str: str) -> dict:
    """生成单日的三层检测报告"""
    validator = PreciseEntryValidator(code)
    result = validator.check_ready_to_buy(date_str)

    # 提取关键信息
    report = {
        "date": date_str,
        "verdict": result["verdict"],
        "summary": result["summary"],
        "ready": result["ready_to_buy"],
        "l1": {
            "level": result["l1"].get("level"),
            "risk_score": result["l1"].get("risk_score"),
            "status": "✅ 安全" if result["l1"].get("level") == "safe" else ("⚠️ 警告" if result["l1"].get("level") == "warning" else "❌ 危险"),
        },
        "l2": {
            "consolidating": result["l2"].get("is_consolidating"),
            "support_level": result["l2"].get("support_level"),
            "volume_ratio": result["l2"].get("volume_shrink_ratio"),
            "status": "✅ 通过" if result["l2"].get("is_consolidating") else "⏳ 等待",
        },
        "l3": {
            "resonance": result["l3"].get("resonance"),
            "status": "✅ 共振" if result["l3"].get("resonance") else ("⏳ 等待" if not result["l3"].get("insufficient") else "❌ 数据不足"),
        },
    }

    return report


def print_daily_report(report: dict):
    """打印单日报告"""
    date = report["date"]
    verdict = report["verdict"]
    summary = report["summary"]

    print()
    print("=" * 100)
    print(f"摩恩电气(002451) 精准买入监控 — {date}")
    print("=" * 100)
    print()
    print(f"【综合判定】{summary}")
    print()

    # L1
    print(f"【L1 - 追高风险】{report['l1']['status']}")
    if report["l1"]["risk_score"] is not None:
        score = report["l1"]["risk_score"]
        bar_len = int(score / 5)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"  风险评分: {score:3d}/100  [{bar}]")
    print()

    # L2
    print(f"【L2 - 缩量支撑】{report['l2']['status']}")
    if report["l2"]["support_level"] is not None:
        print(f"  支撑位: {report['l2']['support_level']:.2f}")
    if report["l2"]["volume_ratio"] is not None:
        vol_str = f"  缩量比: {report['l2']['volume_ratio']:.2f}x " + ("✅" if report["l2"]["volume_ratio"] < 0.8 else "❌")
        print(vol_str)
    print()

    # L3
    print(f"【L3 - 日内共振】{report['l3']['status']}")
    print()

    # 建议
    print("【行动建议】")
    if verdict == "buy_now":
        print("  🟢 立即买入（三层全绿）")
    elif verdict == "wait_resonance":
        print("  🟡 持续监控，等待日内共振信号")
    elif verdict == "wait_consolidation":
        print("  🟡 耐心等待缩量巩固，可能需要数天")
    elif verdict == "avoid_chase":
        print("  🔴 避免追高，继续观察")
    else:
        print("  ⚪ 数据不足，继续收集")

    print()
    print("=" * 100)


def check_all_recent_days(code: str, days: int = 30):
    """检查最近N天的情况"""
    df = fetch_daily_kline(code)
    if df.empty or len(df) < days:
        print(f"日线数据不足 {days} 天")
        return

    recent_dates = df.tail(days)["date"].astype(str).tolist()

    print()
    print("=" * 100)
    print(f"摩恩电气(002451) 最近 {days} 天的买入就绪度追踪")
    print("=" * 100)
    print()

    # 简表
    print(f"{'日期':<12} {'L1状态':<10} {'L1风险':<8} {'L2状态':<10} {'L3状态':<10} {'综合':<15} {'建议':<15}")
    print("-" * 100)

    for date_str in recent_dates:
        try:
            report = generate_daily_report(code, date_str)

            l1_status = report["l1"]["status"]
            l1_risk = f"{report['l1']['risk_score']}" if report["l1"]["risk_score"] is not None else "?"
            l2_status = report["l2"]["status"]
            l3_status = report["l3"]["status"]
            summary = report["summary"][:15]
            verdict = report["verdict"]

            print(f"{date_str}  {l1_status:<10} {l1_risk:<8} {l2_status:<10} {l3_status:<10} {summary:<15} {verdict:<15}")
        except Exception as e:
            print(f"{date_str}  [错误: {str(e)[:30]}]")

    print()
    print("=" * 100)


def check_single_date(code: str, date_str: str):
    """检查单个日期"""
    try:
        report = generate_daily_report(code, date_str)
        print_daily_report(report)

        # 保存到历史记录
        state = _load_monitor_state()
        state["history"][date_str] = report
        state["last_check_date"] = date_str
        _save_monitor_state(state)

    except Exception as e:
        print(f"[ERROR] 检查 {date_str} 时出错: {e}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="精准买入监控")
    parser.add_argument("--code", default="002451", help="股票代码")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--check-all", action="store_true", help="检查最近30天")
    parser.add_argument("--watch", action="store_true", help="自动每日监控")
    args = parser.parse_args()

    if args.check_all:
        check_all_recent_days(args.code, days=30)
    elif args.watch:
        today = datetime.now().strftime("%Y-%m-%d")
        state = _load_monitor_state()
        if state.get("last_check_date") == today:
            print(f"[INFO] 今日已检查过，读取缓存结果")
        check_single_date(args.code, today)
    else:
        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        check_single_date(args.code, date_str)


if __name__ == "__main__":
    main()
