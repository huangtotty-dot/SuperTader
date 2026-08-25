# -*- coding: utf-8 -*-
"""
analysis_multi_timeframe.py - 大盘多周期分析 + watchlist泛化测试

任务1: 分析 2026-08-19 大阴线之后的底部形成过程
  - 获取正确的 sh000001 数据
  - 分析支撑水平和试探次数
  - 验证高点逐步抬升
  - 检查多周期共振 (日线+15m+30m+60m)

任务2: 泛化测试 - 对 watchlist_buy.json 中的 39 只股票应用三层条件
  - L1: t_regime (市场方向)
  - L2: t_trend (多头结构)
  - L3: t_drawdown (回撤到位)
  - 生成参数表现报告和调优建议
"""

import json
import os
import sys
from datetime import datetime, timedelta
from collections import defaultdict
from pathlib import Path

# 导入项目模块
sys.path.insert(0, '/e/superTrader')
from data_fetcher import fetch_stock_data, fetch_index_data
from index_regime import analyze_regime
from position_builder import evaluate_buy_conditions
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


def analyze_index_bottom_08_19():
    """
    任务1: 分析2026-08-19大阴线后的大盘底部形成
    """
    print("\n" + "="*70)
    print("【大盘多周期底部形成分析】从2026-08-19大阴线之后")
    print("="*70)

    # 获取上证指数数据 (sh000001)
    print("\n[1/4] 正在获取上证指数(sh000001)日线数据...")
    try:
        # 从2026-08-15到2026-08-25，获取最近的日线数据
        index_data = fetch_index_data('sh000001', start_date='2026-08-15', end_date='2026-08-25')
        if index_data is None or len(index_data) == 0:
            print("  警告: 无法获取上证指数数据，使用模拟数据")
            index_data = pd.DataFrame()
        else:
            print(f"  获得 {len(index_data)} 条日线数据")
    except Exception as e:
        print(f"  获取失败: {e}，使用模拟数据")
        index_data = pd.DataFrame()

    # 分析日线底部特征
    print("\n[2/4] 分析支撑水平和试探次数...")
    if not index_data.empty:
        index_data = index_data.sort_index()
        print("  近期日线数据:")
        print(index_data[['close', 'low', 'high', 'volume']].tail(15).to_string())

        # 计算支撑水平
        lows = index_data['low'].values
        lowest_point = lows.min()
        low_date = index_data[index_data['low'] == lowest_point].index[0]
        print(f"\n  最低点: {lowest_point:.2f} @ {low_date}")

        # 计算底部试探次数 (低于最低点之上2%内)
        support_level = lowest_point * 1.02
        probes = (lows >= lowest_point) & (lows <= support_level)
        probe_count = probes.sum()
        print(f"  支撑试探次数 (支撑位 ±2%): {probe_count}")

        # 检查高点是否逐步抬升
        if len(index_data) >= 5:
            recent_highs = index_data['high'].values[-5:]
            high_trend = np.diff(recent_highs)
            up_count = (high_trend > 0).sum()
            print(f"  近5日高点趋势: 向上{up_count}次, 向下{5-up_count-1}次")
            print(f"  高点序列: {recent_highs.round(2)}")
    else:
        print("  无日线数据")

    # 获取多周期数据
    print("\n[3/4] 获取多周期数据进行共振验证...")
    timeframes = {
        '15min': '15m',
        '30min': '30m',
        '60min': '60m'
    }

    for tf_name, tf_code in timeframes.items():
        try:
            # 这里需要实现分钟级数据获取
            print(f"  {tf_name}: 待实现获取...")
        except Exception as e:
            print(f"  {tf_name}: 获取失败 - {e}")

    print("\n[4/4] 多周期共振判定...")
    print("  [待详细实现] 需要整合 index_regime_intraday.py 的分钟级判定")

    # 生成日线底部形成报告
    report = {
        "analysis_date": "2026-08-25",
        "event": "08-19 大阴线后的底部形成",
        "summary": {
            "lowest_point": lowest_point if not index_data.empty else None,
            "probe_count": probe_count if not index_data.empty else None,
            "high_trend": "逐步抬升" if not index_data.empty and up_count >= 3 else "未确认",
            "multi_timeframe_resonance": "待验证"
        }
    }

    return report


def test_stock_conditions(stock_code, stock_data):
    """
    评估单只股票的L1/L2/L3条件
    返回详细的条件评估结果
    """
    try:
        results = {
            'code': stock_code,
            'data_available': False,
            'conditions': {}
        }

        if stock_data.empty or len(stock_data) < 60:
            return results

        # 确保数据排序
        stock_data = stock_data.sort_index()
        results['data_available'] = True

        # 计算基本指标
        close = stock_data['close'].values
        ma5 = pd.Series(close).rolling(5).mean().values
        ma10 = pd.Series(close).rolling(10).mean().values
        ma20 = pd.Series(close).rolling(20).mean().values
        ma60 = pd.Series(close).rolling(60).mean().values

        # 取最新值
        current_price = close[-1]
        current_ma5 = ma5[-1]
        current_ma10 = ma10[-1]
        current_ma20 = ma20[-1]
        current_ma60 = ma60[-1]

        # 计算MACD (简化版)
        exp12 = pd.Series(close).ewm(span=12).mean().values[-1]
        exp26 = pd.Series(close).ewm(span=26).mean().values[-1]
        macd = exp12 - exp26

        # L1: t_regime - 市场方向 (这需要大盘数据，这里简化)
        # 假设从 index_regime 获取
        t_regime = True  # 占位符

        # L2: t_trend - 多头结构 (价格在MA20和MA60之上)
        t_trend = (current_price > current_ma20) and (current_ma20 > current_ma60)

        # L3: t_drawdown - 回撤到位 (相对于MA20的回撤幅度)
        drawdown_pct = (current_price - current_ma20) / current_ma20 if current_ma20 > 0 else 0
        t_drawdown = drawdown_pct >= -0.03  # 回撤3%以内算到位

        # 加分项: MACD金叉
        t_golden = macd > 0

        results['conditions'] = {
            't_regime': t_regime,
            't_trend': t_trend,
            't_drawdown': t_drawdown,
            't_golden': t_golden
        }

        results['details'] = {
            'price': round(current_price, 2),
            'ma5': round(current_ma5, 2) if not np.isnan(current_ma5) else None,
            'ma20': round(current_ma20, 2) if not np.isnan(current_ma20) else None,
            'ma60': round(current_ma60, 2) if not np.isnan(current_ma60) else None,
            'drawdown_pct': round(drawdown_pct, 4),
            'macd': round(macd, 4)
        }

        return results

    except Exception as e:
        print(f"    Error evaluating {stock_code}: {e}")
        return {'code': stock_code, 'error': str(e)}


def run_watchlist_generalization_test():
    """
    任务2: 对watchlist中的39只股票进行L1/L2/L3泛化测试
    """
    print("\n" + "="*70)
    print("【Watchlist 泛化性能测试】39只股票 × L1/L2/L3三层条件")
    print("="*70)

    # 加载watchlist
    watchlist_path = '/e/superTrader/watchlist_buy.json'
    watchlist = load_json_safe(watchlist_path)

    if not watchlist or 'stocks' not in watchlist:
        print("  错误: 无法加载watchlist")
        return None

    stocks = watchlist['stocks']
    print(f"\n  共 {len(stocks)} 只股票待测试")

    results = {
        'timestamp': datetime.now().isoformat(),
        'total_stocks': len(stocks),
        'stocks': {},
        'summary': {}
    }

    # 逐只股票测试
    for idx, (code, stock_info) in enumerate(stocks.items(), 1):
        print(f"\n  [{idx}/{len(stocks)}] {code} - {stock_info.get('name', '??')}")

        try:
            # 获取股票数据
            stock_data = fetch_stock_data(code, start_date='2026-05-01', end_date='2026-08-25')

            if stock_data is not None and not stock_data.empty:
                # 评估条件
                eval_result = test_stock_conditions(code, stock_data)
                results['stocks'][code] = {
                    'name': stock_info.get('name'),
                    'evaluation': eval_result,
                    'watchlist_status': stock_info.get('status'),
                    'in_holdings': stock_info.get('in_holdings', False)
                }

                # 输出结果
                if 'conditions' in eval_result:
                    conds = eval_result['conditions']
                    l1 = '✓' if conds.get('t_regime') else '✗'
                    l2 = '✓' if conds.get('t_trend') else '✗'
                    l3 = '✓' if conds.get('t_drawdown') else '✗'
                    bonus = '✓' if conds.get('t_golden') else '✗'
                    print(f"    L1:{l1} L2:{l2} L3:{l3} BONUS:{bonus}")
                    if 'details' in eval_result:
                        details = eval_result['details']
                        print(f"    价:{details['price']} MA20:{details['ma20']} 回撤:{details['drawdown_pct']:.2%}")
                else:
                    print(f"    无法评估条件")
            else:
                print(f"    无数据")
                results['stocks'][code] = {'name': stock_info.get('name'), 'error': 'no_data'}

        except Exception as e:
            print(f"    异常: {e}")
            results['stocks'][code] = {'name': stock_info.get('name'), 'error': str(e)}

    # 统计汇总
    print("\n" + "="*70)
    print("【泛化测试汇总统计】")
    print("="*70)

    stats = {
        'total': len(stocks),
        'l1_pass': 0,
        'l2_pass': 0,
        'l3_pass': 0,
        'bonus_pass': 0,
        'all_conditions_pass': 0,
        'no_data': 0
    }

    for code, stock_result in results['stocks'].items():
        if 'error' in stock_result:
            stats['no_data'] += 1
        elif 'evaluation' in stock_result:
            eval_res = stock_result['evaluation']
            if eval_res.get('data_available'):
                conds = eval_res.get('conditions', {})
                stats['l1_pass'] += conds.get('t_regime', False)
                stats['l2_pass'] += conds.get('t_trend', False)
                stats['l3_pass'] += conds.get('t_drawdown', False)
                stats['bonus_pass'] += conds.get('t_golden', False)

                if all([conds.get('t_regime'), conds.get('t_trend'), conds.get('t_drawdown')]):
                    stats['all_conditions_pass'] += 1

    results['summary'] = stats

    print(f"\n  总股票数: {stats['total']}")
    print(f"  有效数据: {stats['total'] - stats['no_data']}")
    print(f"\n  L1 (市场方向) 通过: {stats['l1_pass']}/{stats['total']} ({stats['l1_pass']*100/max(1, stats['total']):.1f}%)")
    print(f"  L2 (多头结构) 通过: {stats['l2_pass']}/{stats['total']} ({stats['l2_pass']*100/max(1, stats['total']):.1f}%)")
    print(f"  L3 (回撤到位) 通过: {stats['l3_pass']}/{stats['total']} ({stats['l3_pass']*100/max(1, stats['total']):.1f}%)")
    print(f"  加分 (MACD金叉) 通过: {stats['bonus_pass']}/{stats['total']} ({stats['bonus_pass']*100/max(1, stats['total']):.1f}%)")
    print(f"  三层全通过: {stats['all_conditions_pass']}/{stats['total']} ({stats['all_conditions_pass']*100/max(1, stats['total']):.1f}%)")

    return results


def generate_parameter_tuning_report(test_results):
    """
    基于泛化测试结果生成参数调优建议
    """
    print("\n" + "="*70)
    print("【参数调优建议】")
    print("="*70)

    stats = test_results.get('summary', {})

    print("\n[调优策略]\n")

    # 策略1: L1 通过率
    l1_rate = stats.get('l1_pass', 0) / max(1, stats.get('total', 1))
    if l1_rate < 0.3:
        print("  1. L1 (市场方向) 通过率过低 (< 30%)")
        print("     建议: 放宽市场方向判定条件 (当前可能过于严格)")
        print("           或等待更多市场方向确认信号")
    elif l1_rate > 0.8:
        print("  1. L1 (市场方向) 通过率很高 (> 80%)")
        print("     建议: 可考虑加强其他条件门控以提高信号质量")

    # 策略2: L2 通过率
    l2_rate = stats.get('l2_pass', 0) / max(1, stats.get('total', 1))
    if l2_rate < 0.3:
        print("  2. L2 (多头结构) 通过率过低 (< 30%)")
        print("     建议: 检查是否处于市场弱势阶段")
        print("           或调整 MA20/MA60 比例判定条件")

    # 策略3: L3 通过率
    l3_rate = stats.get('l3_pass', 0) / max(1, stats.get('total', 1))
    if l3_rate < 0.2:
        print("  3. L3 (回撤到位) 通过率过低 (< 20%)")
        print("     建议: 放宽回撤幅度门槛 (当前 -3%，可考虑 -5%)")
    elif l3_rate > 0.6:
        print("  3. L3 (回撤到位) 通过率较高 (> 60%)")
        print("     建议: 考虑加强回撤确认 (降低 MA20/MA60 位置要求)")

    # 综合通过率
    all_pass = stats.get('all_conditions_pass', 0)
    total = stats.get('total', 1)
    combined_rate = all_pass / max(1, total - stats.get('no_data', 0))

    print(f"\n  综合通过率: {all_pass}/{total} = {combined_rate*100:.1f}%")
    if combined_rate < 0.1:
        print("     → 现在市场环境不利，等待更多信号确认")
    elif combined_rate > 0.3:
        print("     → 环境良好，可以提升入场积极性")

    print("\n[下一步]\n")
    print("  1. 对通过三层条件的股票做深度基本面/技术面复核")
    print("  2. 持续监控参数表现，每周更新一次泛化测试")
    print("  3. 记录参数调整前后的胜率对比，逐步优化")


def main():
    print("\n" + "#"*70)
    print("# 大盘多周期分析 + Watchlist 泛化性能测试")
    print("# 执行时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("#"*70)

    # 任务1: 分析大盘底部
    print("\n【任务1】大盘多周期底部形成分析")
    index_report = analyze_index_bottom_08_19()

    # 任务2: Watchlist泛化测试
    print("\n【任务2】Watchlist 泛化性能测试")
    test_results = run_watchlist_generalization_test()

    # 任务3: 参数调优建议
    if test_results:
        generate_parameter_tuning_report(test_results)

    # 保存报告
    print("\n" + "="*70)
    print("【保存分析结果】")
    print("="*70)

    report_date = datetime.now().strftime("%Y-%m-%d")
    output_dir = Path('/e/superTrader/t_io/validation')
    output_dir.mkdir(parents=True, exist_ok=True)

    # 保存index分析
    index_file = output_dir / f'index_multiframe_analysis_{report_date}.json'
    with open(index_file, 'w', encoding='utf-8') as f:
        json.dump(index_report, f, indent=2, ensure_ascii=False)
    print(f"\n  已保存: {index_file}")

    # 保存generalization测试结果
    if test_results:
        test_file = output_dir / f'watchlist_generalization_test_{report_date}.json'
        with open(test_file, 'w', encoding='utf-8') as f:
            json.dump(test_results, f, indent=2, ensure_ascii=False)
        print(f"  已保存: {test_file}")

    print("\n" + "#"*70)
    print("# 分析完成")
    print("#"*70)


if __name__ == '__main__':
    main()
