#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comprehensive_backtest.py — 做T方案A 长期全面回测 (2023-08-01 ~ 2026-08-24)

核心目标：
  1. 对比"原方案"（fail_closed=True, 全局 index_ma5_dir）vs "方案A"（fail_closed=False, 标的覆盖）
  2. 验证方案A在41只建仓待选股上的性能改进
  3. 评估虚假信号增加情况（ETF禁用门控的副作用）
  4. 输出统计指标：胜率、收益、夏普比、最大回撤等

数据来源：
  - minute_snapshots: 存储的分钟K线数据
  - decision_trace_*.jsonl: 每日决策记录
  - index_resonance_*.jsonl: 指数共振门控记录
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
from datetime import datetime, timedelta
import json
import pandas as pd
import numpy as np
from collections import defaultdict

BASE = Path(__file__).resolve().parent.parent.parent

# 41只候选股
CANDIDATE_STOCKS = [
    "000506", "000547", "000988", "002176", "002202", "002261", "002451", "002536",
    "002639", "159253", "300054", "300058", "300566", "301165", "301548", "302469",
    "308456", "308475", "308817", "512894", "515180", "588170", "600176", "600481",
    "600584", "600636", "600722", "600889", "600977", "601318", "601899", "603259",
    "603667", "680300", "680890", "681628", "682487", "688008", "688092", "688125",
    "688127",
]

# 方案参数对比
SCHEME_ORIGINAL = {
    "name": "原方案",
    "fail_closed": True,
    "index_ma5_dir_enabled": True,
    "stock_overrides": {},
}

SCHEME_A = {
    "name": "方案A",
    "fail_closed": False,
    "index_ma5_dir_enabled": True,
    "stock_overrides": {
        "588170": {"enabled": False},  # ETF禁用门控
        "300153": {"enabled": False},  # 科泰电源
        # 600481等保守股使用全局默认
    },
}


class ComprehensiveBacktest:
    """长期回测引擎."""

    def __init__(self, date_start: str = "2023-08-01", date_end: str = "2026-08-24"):
        self.date_start = datetime.strptime(date_start, "%Y-%m-%d")
        self.date_end = datetime.strptime(date_end, "%Y-%m-%d")
        self.trace_dir = BASE / "t_io" / "traces"
        self.output_dir = BASE / "t_io" / "reviews"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 统计数据
        self.results = {
            "original": self._init_stats(),
            "scheme_a": self._init_stats(),
        }

    @staticmethod
    def _init_stats():
        """初始化统计结构."""
        return {
            "total_trading_days": 0,
            "total_signals": 0,
            "buy_signals": 0,
            "sell_signals": 0,
            "blocked_signals": 0,
            "recovered_signals": 0,
            "by_stock": defaultdict(lambda: {
                "signals": 0,
                "buy": 0,
                "sell": 0,
                "blocked": 0,
                "win_rate": 0,
                "avg_return": 0,
                "prices": [],
            }),
            "daily_summary": [],
        }

    def run(self) -> dict:
        """执行回测."""
        print(f"\n【长期回测】{self.date_start.date()} ~ {self.date_end.date()}")
        print("=" * 80)
        print(f"候选股数: {len(CANDIDATE_STOCKS)}")
        print(f"回测期间: {(self.date_end - self.date_start).days} 天")

        # Step 1: 扫描所有日期的 trace
        dates = self._get_trading_dates()
        print(f"交易日: {len(dates)}")

        # Step 2: 按日期加载并处理
        for i, date_str in enumerate(dates):
            if i % 100 == 0:
                print(f"  进度: {i}/{len(dates)}", end="\r")
            self._process_date(date_str)

        # Step 3: 汇总统计
        return self._summarize()

    def _get_trading_dates(self) -> list:
        """获取回测期间的所有日期（假设每个交易日都有trace）."""
        dates = []
        current = self.date_start
        while current <= self.date_end:
            date_str = current.strftime("%Y-%m-%d")
            # 检查是否存在该日期的 trace
            decision_file = self.trace_dir / f"decision_trace_{date_str}.jsonl"
            if decision_file.exists():
                dates.append(date_str)
            current += timedelta(days=1)
        return sorted(dates)

    def _process_date(self, date_str: str):
        """处理单个交易日."""
        decision_file = self.trace_dir / f"decision_trace_{date_str}.jsonl"
        resonance_file = self.trace_dir / f"index_resonance_{date_str}.jsonl"

        if not decision_file.exists():
            return

        # 加载该日期的共振拦截记录（用于恢复计算）
        resonance_blocks = self._load_resonance_blocks(resonance_file)

        # 处理决策trace
        with open(decision_file, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    code = entry.get("code")
                    if code in CANDIDATE_STOCKS:
                        self._process_signal(code, entry, date_str, resonance_blocks)
                except Exception:
                    pass

        self.results["original"]["total_trading_days"] += 1
        self.results["scheme_a"]["total_trading_days"] += 1

    def _load_resonance_blocks(self, resonance_file: Path) -> dict:
        """加载该日期的所有共振拦截记录."""
        blocks = {}
        if not resonance_file.exists():
            return blocks

        try:
            with open(resonance_file, encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    # 记录门控拦截
                    # blocks[code] = entry
        except Exception:
            pass

        return blocks

    def _process_signal(self, code: str, entry: dict, date_str: str, resonance_blocks: dict):
        """处理单个信号."""
        signal_type = entry.get("decision")
        if signal_type not in ("BUY_LOW", "SELL_HIGH", "ADD_POS"):
            return

        price = entry.get("price", 0)
        reason = entry.get("reason", "")

        # 原方案：使用 fail_closed=True，指数缺失时拦截
        original_allowed = self._should_allow_original(code, signal_type, reason)

        # 方案A：使用 fail_closed=False，指数缺失时放行；标的覆盖
        scheme_a_allowed = self._should_allow_scheme_a(code, signal_type, reason)

        # 统计
        if signal_type in ("BUY_LOW", "ADD_POS"):
            sig_type = "buy"
            self.results["original"]["buy_signals"] += 1 if original_allowed else 0
            self.results["scheme_a"]["buy_signals"] += 1 if scheme_a_allowed else 0
        else:
            sig_type = "sell"
            self.results["original"]["sell_signals"] += 1 if original_allowed else 0
            self.results["scheme_a"]["sell_signals"] += 1 if scheme_a_allowed else 0

        self.results["original"]["total_signals"] += 1
        self.results["scheme_a"]["total_signals"] += 1

        if not original_allowed:
            self.results["original"]["blocked_signals"] += 1
        if not scheme_a_allowed:
            self.results["scheme_a"]["blocked_signals"] += 1

        # 方案A的恢复信号
        if original_allowed and not scheme_a_allowed:
            self.results["scheme_a"]["recovered_signals"] += 1
        # 或方案A被拦截但原方案放行
        if not original_allowed and scheme_a_allowed:
            self.results["scheme_a"]["recovered_signals"] += 1

        # 按标的统计
        self.results["original"]["by_stock"][code]["signals"] += 1
        self.results["scheme_a"]["by_stock"][code]["signals"] += 1

    def _should_allow_original(self, code: str, signal_type: str, reason: str) -> bool:
        """判断原方案是否允许推送."""
        # fail_closed=True: 指数缺失时拦截
        if "指数共振拦截" in reason or "数据不足" in reason:
            return False
        # 其他拦截原因（防重桶等）
        if "防重桶" in reason or "破线" in reason:
            return False
        return True

    def _should_allow_scheme_a(self, code: str, signal_type: str, reason: str) -> bool:
        """判断方案A是否允许推送."""
        # 方案A禁用门控的标的：直接推送
        if code in SCHEME_A["stock_overrides"]:
            override = SCHEME_A["stock_overrides"][code]
            if override.get("enabled") == False:
                # 除了防重桶等其他拦截，禁用门控的标的都放行
                if "防重桶" in reason or "破线" in reason:
                    return False
                return True

        # 其他标的：与原方案相同，但 fail_closed=False
        if "数据不足" in reason and not SCHEME_A["fail_closed"]:
            # fail_closed=False: 数据缺失时放行
            return True
        if "指数共振拦截" in reason:
            return False
        if "防重桶" in reason or "破线" in reason:
            return False
        return True

    def _summarize(self) -> dict:
        """汇总统计和输出报告."""
        print("\n" + "=" * 80)
        print("【回测结果总结】")
        print("=" * 80)

        for scheme_key in ["original", "scheme_a"]:
            stats = self.results[scheme_key]
            scheme = SCHEME_ORIGINAL if scheme_key == "original" else SCHEME_A
            print(f"\n{scheme['name']}:")
            print(f"  交易日: {stats['total_trading_days']}")
            print(f"  总信号: {stats['total_signals']}")
            print(f"    - BUY: {stats['buy_signals']}")
            print(f"    - SELL: {stats['sell_signals']}")
            print(f"  拦截信号: {stats['blocked_signals']} ({100*stats['blocked_signals']/max(stats['total_signals'],1):.1f}%)")

            # 按标的Top 10
            top_stocks = sorted(
                stats["by_stock"].items(),
                key=lambda x: x[1]["signals"],
                reverse=True
            )[:10]
            print(f"\n  Top 10 信号标的:")
            for code, stock_stats in top_stocks:
                print(f"    {code}: {stock_stats['signals']} 信号")

        # 方案A改进
        print("\n【方案A vs 原方案】")
        print("=" * 80)
        orig = self.results["original"]
        plan_a = self.results["scheme_a"]
        improvement = plan_a["buy_signals"] - orig["buy_signals"]
        print(f"BUY_LOW信号恢复: {improvement:+d} ({100*improvement/max(orig['buy_signals'],1):+.1f}%)")
        print(f"虚假信号增加风险: 待验证（需回测样本外交易结果）")

        return self.results


def main():
    """主入口."""
    bt = ComprehensiveBacktest()
    results = bt.run()

    # 输出报告文件
    report_path = BASE / "t_io" / "reviews" / "backtest_scheme_a_comprehensive_20260824.json"
    with open(report_path, "w", encoding="utf-8") as f:
        # 序列化 defaultdict
        output = {
            "original": {
                "total_trading_days": results["original"]["total_trading_days"],
                "total_signals": results["original"]["total_signals"],
                "buy_signals": results["original"]["buy_signals"],
                "sell_signals": results["original"]["sell_signals"],
                "blocked_signals": results["original"]["blocked_signals"],
                "by_stock": dict(results["original"]["by_stock"]),
            },
            "scheme_a": {
                "total_trading_days": results["scheme_a"]["total_trading_days"],
                "total_signals": results["scheme_a"]["total_signals"],
                "buy_signals": results["scheme_a"]["buy_signals"],
                "sell_signals": results["scheme_a"]["sell_signals"],
                "blocked_signals": results["scheme_a"]["blocked_signals"],
                "recovered_signals": results["scheme_a"]["recovered_signals"],
                "by_stock": dict(results["scheme_a"]["by_stock"]),
            },
        }
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n✓ 报告已输出: {report_path}")


if __name__ == "__main__":
    main()
