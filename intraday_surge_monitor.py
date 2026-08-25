# -*- coding: utf-8 -*-
"""
intraday_surge_monitor.py - 日内冲高实时监控

集成到 position_builder.scan_stock() 之后，检测所有holdings和watchlist中的冲高风险。
每次扫描时输出防御建议。
"""

import json
import pandas as pd
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from intraday_surge_defense import intraday_surge_defense

# 动态导入以避免循环依赖
def _load_holdings():
    try:
        from position_builder import load_holdings
        return load_holdings()
    except Exception:
        return {}

def _load_watchlist():
    try:
        from position_builder import load_watchlist
        return load_watchlist()
    except Exception:
        return {}

def _load_snapshot_df(code, date_str):
    try:
        from position_builder import load_snapshot_df
        return load_snapshot_df(code, date_str)
    except Exception:
        return None, None, None


def monitor_surge_risks(holdings: Dict[str, dict] = None, watchlist: Dict[str, dict] = None) -> Dict:
    """
    监控holdings和watchlist中所有代码的冲高风险

    Returns:
        {
            "timestamp": str,
            "holdings_alerts": [
                {
                    "code": str,
                    "action": "SAFE" | "WARNING" | "AVOID" | "EXIT",
                    "alert_level": "normal" | "warning" | "critical",
                    "reason": str
                }
            ],
            "watchlist_alerts": [...]
        }
    """

    if holdings is None:
        holdings = _load_holdings()
    if watchlist is None:
        watchlist = _load_watchlist()

    results = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "holdings_alerts": [],
        "watchlist_alerts": [],
        "critical_alerts": []  # 需要立即处理的
    }

    today_str = datetime.now().strftime("%Y-%m-%d")

    # 检查 holdings
    for code, holding in holdings.items():
        try:
            df_1min, _, _ = _load_snapshot_df(code, today_str)
            if df_1min is None or df_1min.empty:
                continue

            defense = intraday_surge_defense(
                code=code,
                name=holding.get("name", code),
                df_1min=df_1min
            )

            alert_item = {
                "code": defense.code,
                "name": defense.name,
                "action": defense.action,
                "alert_level": defense.alert_level,
                "reason": defense.reason,
                "high_reached": defense.high_reached,
                "high_time": defense.high_time,
                "current_price": defense.current_price,
                "pullback_ratio": defense.pullback_ratio,
            }

            results["holdings_alerts"].append(alert_item)

            if defense.action in ("AVOID", "EXIT"):
                results["critical_alerts"].append({
                    "type": "holding",
                    "code": code,
                    "name": holding.get("name", code),
                    "action": defense.action,
                    "reason": defense.reason
                })

        except Exception as e:
            pass  # 单个检查失败不影响其他

    # 检查 watchlist
    for code, watch in watchlist.items():
        try:
            df_1min, _, _ = _load_snapshot_df(code, today_str)
            if df_1min is None or df_1min.empty:
                continue

            defense = intraday_surge_defense(
                code=code,
                name=watch.get("name", code),
                df_1min=df_1min
            )

            alert_item = {
                "code": defense.code,
                "name": defense.name,
                "action": defense.action,
                "alert_level": defense.alert_level,
                "reason": defense.reason,
                "high_reached": defense.high_reached,
                "high_time": defense.high_time,
                "current_price": defense.current_price,
                "pullback_ratio": defense.pullback_ratio,
            }

            results["watchlist_alerts"].append(alert_item)

            if defense.action in ("AVOID", "EXIT"):
                results["critical_alerts"].append({
                    "type": "watchlist",
                    "code": code,
                    "name": watch.get("name", code),
                    "action": defense.action,
                    "reason": defense.reason
                })

        except Exception as e:
            pass

    return results


def format_surge_alert(result: Dict) -> str:
    """格式化为可读的告警字符串"""

    lines = []
    lines.append(f"\n【日内冲高监控】{result['timestamp']}")

    # Holdings 告警
    if result["holdings_alerts"]:
        lines.append("\n🏦 持仓风险:")
        for alert in result["holdings_alerts"]:
            if alert["action"] != "SAFE":
                tag = "🔴" if alert["action"] in ("AVOID", "EXIT") else "⚠️"
                lines.append(
                    f"  {tag} {alert['name']} [{alert['action']}] "
                    f"回落{alert['pullback_ratio']*100:.1f}% - {alert['reason']}"
                )

    # Watchlist 告警
    if result["watchlist_alerts"]:
        lines.append("\n👀 监控风险:")
        warning_count = 0
        for alert in result["watchlist_alerts"]:
            if alert["action"] != "SAFE" and warning_count < 3:  # 最多显示3个
                tag = "🔴" if alert["action"] in ("AVOID", "EXIT") else "⚠️"
                lines.append(
                    f"  {tag} {alert['name']} [{alert['action']}] {alert['reason']}"
                )
                warning_count += 1

    # 紧急告警
    if result["critical_alerts"]:
        lines.append(f"\n🚨 立即行动 ({len(result['critical_alerts'])} 只):")
        for alert in result["critical_alerts"]:
            lines.append(
                f"  【{alert['action']}】{alert['code']} {alert['name']}"
            )
            lines.append(f"         {alert['reason']}")

    return "\n".join(lines)


def save_surge_monitor_trace(result: Dict) -> None:
    """保存监控结果到trace文件"""
    try:
        trace_dir = BASE / "t_io" / "traces"
        trace_dir.mkdir(parents=True, exist_ok=True)
        trace_file = trace_dir / "intraday_surge_monitor.jsonl"

        with open(trace_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    result = monitor_surge_risks()
    print(format_surge_alert(result))
    save_surge_monitor_trace(result)
    print()
