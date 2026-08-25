# -*- coding: utf-8 -*-
"""
analysis_comprehensive.py - 大盘多周期分析 + watchlist泛化测试

直接使用现有的日志和缓存数据进行分析
"""

import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path
import pandas as pd
import numpy as np

def load_json_safe(path, encoding='utf-8'):
    """安全读取JSON文件"""
    try:
        with open(path, encoding=encoding) as f:
            return json.load(f)
    except Exception as e:
        print(f"Error reading {path}: {e}")
        return None


def analyze_index_traces():
    """
    从 traces 日志分析大盘多周期行为 (2026-08-19 之后)
    """
    print("\n" + "="*70)
    print("【大盘多周期底部形成分析】从2026-08-19大阴线之后")
    print("="*70)

    traces_dir = Path('./t_io/index_regime/traces')
    breadth_dir = Path('./t_io/index_regime')

    # 收集08-19之后的所有traces
    trace_files = sorted([f for f in traces_dir.glob('*.jsonl') if '2026-08' in f.name])
    print(f"\n  找到 {len(trace_files)} 个交易日的trace数据")

    analysis = {
        'event_date': '2026-08-19',
        'analysis_period': '2026-08-19 至 2026-08-25',
        'daily_analysis': []
    }

    # 逐日分析
    for trace_file in sorted(trace_files)[-7:]:  # 最近7日
        try:
            with open(trace_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            day_data = {
                'date': trace_file.stem.split('_')[-1],
                'regime_state': None,
                'score': None,
                'fired_rules': [],
                'support_levels': [],
                'price_structure': {}
            }

            # 解析trace
            for line in lines:
                try:
                    entry = json.loads(line)
                    if 'regime' in entry:
                        day_data['regime_state'] = entry.get('regime')
                    if 'score' in entry:
                        day_data['score'] = entry.get('score')
                    if 'fired_rules' in entry:
                        day_data['fired_rules'] = entry.get('fired_rules', [])
                except:
                    pass

            analysis['daily_analysis'].append(day_data)
            print(f"  {day_data['date']}: regime={day_data['regime_state']}, score={day_data['score']}, rules={len(day_data['fired_rules'])}")

        except Exception as e:
            print(f"  {trace_file.name}: 读取失败 - {e}")

    return analysis


def analyze_watchlist_conditions():
    """
    对watchlist中的39只股票进行L1/L2/L3条件分析
    使用watchlist_buy.json中已有的信号数据
    """
    print("\n" + "="*70)
    print("【Watchlist 泛化性能测试】39只股票 × L1/L2/L3三层条件")
    print("="*70)

    # 加载watchlist
    watchlist_path = os.path.join(os.getcwd(), 'watchlist_buy.json')
    watchlist = load_json_safe(watchlist_path)

    if not watchlist or 'stocks' not in watchlist:
        print("  错误: 无法加载watchlist")
        return None

    stocks = watchlist['stocks']
    print(f"\n  共 {len(stocks)} 只股票")

    results = {
        'timestamp': datetime.now().isoformat(),
        'total_stocks': len(stocks),
        'analysis_date': '2026-08-25',
        'stocks': {}
    }

    # 汇总统计
    stats = {
        'total': len(stocks),
        't_regime_pass': 0,
        't_trend_pass': 0,
        't_drawdown_pass': 0,
        't_golden_pass': 0,
        'all_pass': 0,
        'by_verdict': defaultdict(int)
    }

    # 逐只股票分析
    for idx, (code, stock_info) in enumerate(stocks.items(), 1):
        name = stock_info.get('name', '??')
        in_holdings = stock_info.get('in_holdings', False)
        status = stock_info.get('status', 'unknown')
        composite_score = stock_info.get('composite_score', 0)
        criteria_met = stock_info.get('criteria_met', {})

        # 从criteria_met提取条件
        t_regime = criteria_met.get('t_regime', False)
        t_trend = criteria_met.get('t_trend', False)
        t_drawdown = criteria_met.get('t_drawdown', False)
        t_golden = criteria_met.get('t_golden', False)

        # 统计
        stats['t_regime_pass'] += t_regime
        stats['t_trend_pass'] += t_trend
        stats['t_drawdown_pass'] += t_drawdown
        stats['t_golden_pass'] += t_golden

        if t_regime and t_trend and t_drawdown:
            stats['all_pass'] += 1

        # 获取最新signal_history
        signal_history = stock_info.get('signal_history', [])
        latest_signal = signal_history[-1] if signal_history else {}
        latest_verdict = latest_signal.get('verdict', 'unknown')
        latest_score = latest_signal.get('score', 0)
        latest_price = latest_signal.get('price', 0)

        stats['by_verdict'][latest_verdict] += 1

        # 记录结果
        stock_result = {
            'name': name,
            'code': code,
            'composite_score': composite_score,
            'latest_price': latest_price,
            'latest_verdict': latest_verdict,
            'latest_signal_score': latest_score,
            'in_holdings': in_holdings,
            'status': status,
            'conditions': {
                't_regime': t_regime,
                't_trend': t_trend,
                't_drawdown': t_drawdown,
                't_golden': t_golden
            },
            'conditions_pass_count': sum([t_regime, t_trend, t_drawdown])
        }

        results['stocks'][code] = stock_result

        # 输出日志
        l_str = f"L:{int(t_regime)} T:{int(t_trend)} D:{int(t_drawdown)} G:{int(t_golden)}"
        verdict_icon = {'watch_signal': '[!]', 'weak': '[.]', 'approach': '[>]'}.get(latest_verdict, '[?]')
        print(f"  [{idx:2d}] {code} {name:12s} {l_str} score:{composite_score:3d} {verdict_icon} {latest_verdict}")

    results['summary'] = {
        'total': stats['total'],
        'l1_pass': stats['t_regime_pass'],
        'l1_rate': f"{stats['t_regime_pass']*100/stats['total']:.1f}%",
        'l2_pass': stats['t_trend_pass'],
        'l2_rate': f"{stats['t_trend_pass']*100/stats['total']:.1f}%",
        'l3_pass': stats['t_drawdown_pass'],
        'l3_rate': f"{stats['t_drawdown_pass']*100/stats['total']:.1f}%",
        'bonus_pass': stats['t_golden_pass'],
        'bonus_rate': f"{stats['t_golden_pass']*100/stats['total']:.1f}%",
        'all_pass': stats['all_pass'],
        'all_pass_rate': f"{stats['all_pass']*100/stats['total']:.1f}%",
        'verdict_distribution': dict(stats['by_verdict'])
    }

    return results


def generate_comprehensive_report():
    """
    生成完整的分析报告并保存
    """
    print("\n" + "="*70)
    print("【综合报告汇总】")
    print("="*70)

    # 分析1: 大盘多周期
    index_analysis = analyze_index_traces()

    # 分析2: watchlist泛化测试
    watchlist_analysis = analyze_watchlist_conditions()

    # 生成汇总
    print("\n" + "="*70)
    print("【统计汇总】")
    print("="*70)

    if watchlist_analysis and 'summary' in watchlist_analysis:
        summary = watchlist_analysis['summary']
        print(f"\n  总股票数: {summary['total']}")
        print(f"\n  分层条件通过情况:")
        print(f"    L1 (市场方向):  {summary['l1_pass']:2d}/{summary['total']} = {summary['l1_rate']}")
        print(f"    L2 (多头结构):  {summary['l2_pass']:2d}/{summary['total']} = {summary['l2_rate']}")
        print(f"    L3 (回撤到位):  {summary['l3_pass']:2d}/{summary['total']} = {summary['l3_rate']}")
        print(f"    加分(MACD金叉): {summary['bonus_pass']:2d}/{summary['total']} = {summary['bonus_rate']}")
        print(f"\n  综合通过 (L1+L2+L3): {summary['all_pass']:2d}/{summary['total']} = {summary['all_pass_rate']}")
        print(f"\n  信号分布: {summary['verdict_distribution']}")

    # 生成参数调优建议
    print("\n" + "="*70)
    print("【参数调优建议】")
    print("="*70)

    if watchlist_analysis and 'summary' in watchlist_analysis:
        summary = watchlist_analysis['summary']

        # 解析百分比
        def parse_pct(s):
            return float(s.rstrip('%')) / 100

        l1_rate = parse_pct(summary['l1_rate'])
        l2_rate = parse_pct(summary['l2_rate'])
        l3_rate = parse_pct(summary['l3_rate'])
        all_rate = parse_pct(summary['all_pass_rate'])

        print("\n  [条件通过率分析]")

        if l1_rate < 0.2:
            print(f"    [!] L1通过率仅 {summary['l1_rate']}，市场缺乏方向性")
            print(f"      建议: 等待市场确认，或放宽L1条件门槛")
        elif l1_rate > 0.7:
            print(f"    [OK] L1通过率 {summary['l1_rate']}，市场方向良好")

        if l2_rate < 0.3:
            print(f"    [!] L2通过率仅 {summary['l2_rate']}，多头结构不足")
            print(f"      建议: 检查 MA20/MA60 参数，或等待更多结构确认")

        if l3_rate < 0.2:
            print(f"    [!] L3通过率仅 {summary['l3_rate']}，回撤幅度不足")
            print(f"      建议: 放宽回撤门槛 (当前-3% → -5%)")
        elif l3_rate > 0.6:
            print(f"    [OK] L3通过率 {summary['l3_rate']}，回撤机会充分")

        print(f"\n  [综合环境判定]")
        if all_rate < 0.05:
            print(f"    环境恶劣 ({all_rate*100:.1f}%): 市场处于磨底阶段，极少数股票同时满足三层条件")
            print(f"    → 建议: 观望为主，严格要求，重点关注 watch_signal 信号")
        elif all_rate < 0.15:
            print(f"    环境一般 ({all_rate*100:.1f}%): 市场存在机会但不明朗")
            print(f"    → 建议: 保持警觉，每日更新，关注确认信号")
        elif all_rate < 0.3:
            print(f"    环境良好 ({all_rate*100:.1f}%): 市场存在明确的买点机会")
            print(f"    → 建议: 可以适度积极，优先选择 watch_signal 的标的")
        else:
            print(f"    环境优秀 ({all_rate*100:.1f}%): 市场机会充足")
            print(f"    → 建议: 积极介入，关注位置的安全性")

    # 保存报告
    print("\n" + "="*70)
    print("【保存分析报告】")
    print("="*70)

    report_date = datetime.now().strftime("%Y-%m-%d")
    output_dir = Path('./t_io/validation')
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成汇总报告
    comprehensive_report = {
        'analysis_date': report_date,
        'timestamp': datetime.now().isoformat(),
        'index_analysis': index_analysis,
        'watchlist_analysis': watchlist_analysis
    }

    report_file = output_dir / f'comprehensive_analysis_{report_date}.json'
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump(comprehensive_report, f, indent=2, ensure_ascii=False)
    print(f"\n  已保存完整报告: {report_file}")

    # 保存watchlist分析为CSV供查阅
    if watchlist_analysis and 'stocks' in watchlist_analysis:
        csv_file = output_dir / f'watchlist_conditions_{report_date}.csv'
        rows = []
        for code, stock_data in watchlist_analysis['stocks'].items():
            cond = stock_data.get('conditions', {})
            rows.append({
                '代码': code,
                '名称': stock_data.get('name'),
                '综合分': stock_data.get('composite_score'),
                '最新价': stock_data.get('latest_price'),
                '信号': stock_data.get('latest_verdict'),
                 'L1-方向': cond.get('t_regime'),
                'L2-结构': cond.get('t_trend'),
                'L3-回撤': cond.get('t_drawdown'),
                '加分-金叉': cond.get('t_golden'),
                '条件通过数': stock_data.get('conditions_pass_count'),
                '持仓': stock_data.get('in_holdings'),
                '状态': stock_data.get('status')
            })

        df = pd.DataFrame(rows)
        df = df.sort_values('综合分', ascending=False)
        df.to_csv(csv_file, index=False, encoding='utf-8-sig')
        print(f"  已保存CSV: {csv_file}")

    print("\n" + "#"*70)
    print("# 分析完成")
    print("#"*70)

    return comprehensive_report


if __name__ == '__main__':
    print("\n" + "#"*70)
    print("# 大盘多周期分析 + Watchlist 泛化性能测试")
    print("# 执行时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("#"*70)

    report = generate_comprehensive_report()
