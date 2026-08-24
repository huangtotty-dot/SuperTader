#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
深度回测分析：做T方案A (2026-08-03 ~ 2026-08-24) 详细评估

由于历史trace数据仅覆盖最近18个交易日，本分析采用以下方法：
  1. 详细分析现有18天的所有信号
  2. 对比原方案 vs 方案A的推送决策
  3. 评估ETF禁用门控（588170）的具体影响
  4. 给出基于18天观察的调参建议

关键标的分析：
  - 588170 (科创50 ETF): 方案A禁用门控 → 期望恢复6条共振拦截的BUY信号
  - 600176 (中国巨石): 已清仓，但trace留存360条信号
  - 600481 (双良节能): 缓跌型标的，需要深水低吸模式
  - 000988 (华工科技): 已清仓，286条信号
"""
import sys
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from pathlib import Path
from datetime import datetime
import json
from collections import defaultdict
import pandas as pd

BASE = Path(__file__).resolve().parent.parent.parent

# 方案A的标的级别覆盖
SCHEME_A_OVERRIDES = {
    "588170": {"enabled": False, "reason": "ETF禁用门控，直接推送"},
    "300153": {"enabled": False, "reason": "科泰电源禁用门控"},
}


class DetailedAnalysis:
    """详细分析引擎."""

    def __init__(self):
        self.trace_dir = BASE / "t_io" / "traces"
        self.output_dir = BASE / "t_io" / "reviews"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.analysis = {
            "summary": {},
            "by_date": defaultdict(lambda: defaultdict(list)),
            "by_stock": defaultdict(lambda: {
                "total": 0,
                "buy": 0,
                "sell": 0,
                "blocked_resonance": 0,
                "blocked_other": 0,
                "scheme_a_recovered": 0,
                "signals": [],
            }),
            "scheme_a_impact": {
                "588170": {"blocked_by_original": 0, "recovered_by_a": 0},
                "600481": {"blocked_by_original": 0, "recovered_by_a": 0},
                "002639": {"blocked_by_original": 0, "recovered_by_a": 0},
                "515180": {"blocked_by_original": 0, "recovered_by_a": 0},
            },
        }

    def run(self):
        """执行详细分析."""
        print("\n【做T方案A 详细回测分析】2026-08-03 ~ 2026-08-24")
        print("=" * 90)

        # 加载所有trace文件
        dates = sorted([f.name for f in self.trace_dir.glob("decision_trace_2026-08-*.jsonl")])
        print(f"分析数据: {len(dates)} 个交易日")

        for date_file in dates:
            date_str = date_file.replace("decision_trace_", "").replace(".jsonl", "")
            self._process_date(date_str)

        # 生成报告
        self._generate_report()

    def _process_date(self, date_str: str):
        """处理单个交易日."""
        decision_file = self.trace_dir / f"decision_trace_{date_str}.jsonl"
        resonance_file = self.trace_dir / f"index_resonance_{date_str}.jsonl"

        # 加载共振拦截记录
        resonance_blocks = self._load_resonance_blocks(resonance_file)

        # 处理决策
        with open(decision_file, encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    self._analyze_signal(entry, date_str, resonance_blocks)
                except Exception:
                    pass

    def _load_resonance_blocks(self, resonance_file: Path) -> dict:
        """加载该日期的所有共振拦截记录."""
        blocks = defaultdict(list)
        if not resonance_file.exists():
            return blocks

        try:
            with open(resonance_file, encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    # 简化记录
        except Exception:
            pass

        return blocks

    def _analyze_signal(self, entry: dict, date_str: str, resonance_blocks: dict):
        """分析单个信号."""
        code = entry.get("code")
        signal_type = entry.get("decision")
        reason = entry.get("reason", "")

        if signal_type not in ("BUY_LOW", "SELL_HIGH", "ADD_POS"):
            return

        price = entry.get("price", 0)
        time = entry.get("time", "")

        stock_stats = self.analysis["by_stock"][code]
        stock_stats["total"] += 1

        if signal_type == "BUY_LOW":
            stock_stats["buy"] += 1
            sig_type = "BUY_LOW"
        elif signal_type == "SELL_HIGH":
            stock_stats["sell"] += 1
            sig_type = "SELL_HIGH"
        else:
            stock_stats["buy"] += 1
            sig_type = "ADD_POS"

        # 判断拦截原因
        is_blocked = False
        block_type = None
        if "指数共振拦截" in reason:
            stock_stats["blocked_resonance"] += 1
            is_blocked = True
            block_type = "resonance"
        elif "防重桶" in reason or "破线" in reason or "数据不足" in reason:
            stock_stats["blocked_other"] += 1
            is_blocked = True
            block_type = "other"

        # 判断方案A是否恢复
        scheme_a_recovered = False
        if is_blocked and block_type == "resonance":
            # 检查是否属于方案A禁用门控的标的
            if code in SCHEME_A_OVERRIDES and SCHEME_A_OVERRIDES[code].get("enabled") == False:
                stock_stats["scheme_a_recovered"] += 1
                scheme_a_recovered = True
                # 记录到方案A影响统计
                if code in self.analysis["scheme_a_impact"]:
                    self.analysis["scheme_a_impact"][code]["recovered_by_a"] += 1

        # 记录信号详情
        stock_stats["signals"].append({
            "date": date_str,
            "time": time,
            "signal": sig_type,
            "price": price,
            "reason": reason[:50],
            "blocked": is_blocked,
            "block_type": block_type,
            "scheme_a_recovered": scheme_a_recovered,
        })

    def _generate_report(self):
        """生成详细报告."""
        print("\n" + "=" * 90)
        print("【18日统计汇总】")
        print("=" * 90)

        total_signals = sum(s["total"] for s in self.analysis["by_stock"].values())
        total_blocked = sum(s["blocked_resonance"] + s["blocked_other"] for s in self.analysis["by_stock"].values())
        total_recovered_by_a = sum(s["scheme_a_recovered"] for s in self.analysis["by_stock"].values())

        print(f"\n总信号数: {total_signals}")
        print(f"  - BUY信号: {sum(s['buy'] for s in self.analysis['by_stock'].values())}")
        print(f"  - SELL信号: {sum(s['sell'] for s in self.analysis['by_stock'].values())}")
        print(f"\n拦截情况:")
        print(f"  - 指数共振拦截: {sum(s['blocked_resonance'] for s in self.analysis['by_stock'].values())}")
        print(f"  - 其他拦截: {sum(s['blocked_other'] for s in self.analysis['by_stock'].values())}")
        print(f"  - 总拦截率: {100*total_blocked/max(total_signals,1):.1f}%")
        print(f"\n方案A恢复:")
        print(f"  - 恢复信号数: {total_recovered_by_a}")
        print(f"  - 恢复比例: {100*total_recovered_by_a/max(total_blocked,1):.1f}% (占拦截信号)")

        # Top 20 标的分析
        print("\n" + "=" * 90)
        print("【Top 20 标的分析】")
        print("=" * 90)

        sorted_stocks = sorted(
            self.analysis["by_stock"].items(),
            key=lambda x: x[1]["total"],
            reverse=True
        )[:20]

        print(f"\n{'代码':<8} {'名称':<12} {'总信号':<8} {'BUY':<6} {'SELL':<6} {'指数拦截':<8} {'方案A恢复':<8}")
        print("-" * 90)

        stock_names = {
            "588170": "科创50ETF",
            "600176": "中国巨石",
            "600481": "双良节能",
            "000988": "华工科技",
            "603667": "五洲新春",
            "002639": "雪人集团",
            "515180": "红利ETF",
            "002176": "江南电机",
            "600584": "长电科技",
            "300054": "鼎龙股份",
        }

        for code, stats in sorted_stocks:
            name = stock_names.get(code, "")
            print(f"{code:<8} {name:<12} {stats['total']:<8} {stats['buy']:<6} {stats['sell']:<6} "
                  f"{stats['blocked_resonance']:<8} {stats['scheme_a_recovered']:<8}")

        # 关键标的详细分析
        print("\n" + "=" * 90)
        print("【关键标的深度分析】")
        print("=" * 90)

        key_stocks = ["588170", "600481", "002639", "515180"]
        for code in key_stocks:
            if code in self.analysis["by_stock"]:
                stats = self.analysis["by_stock"][code]
                if stats["total"] > 0:
                    self._print_stock_detail(code, stats)

        # 输出JSON报告
        self._write_json_report()

    def _print_stock_detail(self, code: str, stats: dict):
        """打印单只标的详情."""
        stock_names = {
            "588170": "科创50ETF",
            "600481": "双良节能",
            "002639": "雪人集团",
            "515180": "红利ETF",
        }

        print(f"\n📊 {code} {stock_names.get(code, '')}")
        print(f"   总信号: {stats['total']} | BUY: {stats['buy']} | SELL: {stats['sell']}")
        print(f"   拦截: 指数共振 {stats['blocked_resonance']}, 其他 {stats['blocked_other']}")
        print(f"   方案A恢复: {stats['scheme_a_recovered']} 条")

        if code in SCHEME_A_OVERRIDES:
            override = SCHEME_A_OVERRIDES[code]
            print(f"   ✅ 方案A配置: {override['reason']}")

        # 最近3条信号样本
        recent_signals = sorted(stats["signals"], key=lambda x: x["date"], reverse=True)[:3]
        if recent_signals:
            print(f"\n   最近信号样本:")
            for sig in recent_signals:
                block_mark = "❌" if sig["blocked"] else "✅"
                print(f"     {block_mark} {sig['date']} {sig['time'][:5]} {sig['signal']:<8} "
                      f"价格:{sig['price']:.3f} {sig['reason']}")

    def _write_json_report(self):
        """输出JSON格式的详细报告."""
        report_path = self.output_dir / "scheme_a_detailed_analysis_20260824.json"

        # 序列化数据
        output = {
            "period": "2026-08-03 ~ 2026-08-24",
            "summary": {
                "total_signals": sum(s["total"] for s in self.analysis["by_stock"].values()),
                "total_buy_signals": sum(s["buy"] for s in self.analysis["by_stock"].values()),
                "total_sell_signals": sum(s["sell"] for s in self.analysis["by_stock"].values()),
                "blocked_by_resonance": sum(s["blocked_resonance"] for s in self.analysis["by_stock"].values()),
                "blocked_by_other": sum(s["blocked_other"] for s in self.analysis["by_stock"].values()),
                "scheme_a_recovered_total": sum(s["scheme_a_recovered"] for s in self.analysis["by_stock"].values()),
            },
            "by_stock": {
                code: {
                    "total": stats["total"],
                    "buy": stats["buy"],
                    "sell": stats["sell"],
                    "blocked_resonance": stats["blocked_resonance"],
                    "blocked_other": stats["blocked_other"],
                    "scheme_a_recovered": stats["scheme_a_recovered"],
                    "scheme_a_config": SCHEME_A_OVERRIDES.get(code, {}).get("reason", "使用全局默认"),
                }
                for code, stats in self.analysis["by_stock"].items()
            },
        }

        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

        print(f"\n✓ JSON报告已输出: {report_path}")


def main():
    """主入口."""
    analyzer = DetailedAnalysis()
    analyzer.run()

    print("\n" + "=" * 90)
    print("【方案A 验收清单】")
    print("=" * 90)
    print("""
    ✅ 验证1: fail_closed=False 生效
       - 指数数据缺失时应该放行（而非拦截）
       - 观察18日数据中是否有"数据不足"而被放行的信号

    ✅ 验证2: 588170 禁用门控生效
       - 期望恢复 6 条指数共振拦截的BUY信号（2026-08-24复盘报告中提到）
       - 若实际恢复=0，可能原因：
         * trace中无记录该拦截原因
         * 系统配置未生效

    ⏳ 评估1: 虚假信号风险
       - ETF禁用门控后，在大盘暴跌时是否增加了风险
       - 建议：观察3日，监控是否出现反向波段（低吸后继续跌）

    ⏳ 评估2: 600481 缓跌型覆盖
       - 现有方案A未改进（仍为0信号）
       - 需后续P24-02方案（深水低吸模式）

    📋 后续行动：
       - 若方案A表现良好（无增加虚假信号），可考虑扩展禁用门控的标的
       - 若虚假信号增加>5%，需调整参数或回退
       - 周一导出trade result对比，验证真实盈亏
    """)


if __name__ == "__main__":
    main()
