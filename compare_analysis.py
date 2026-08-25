import json

print("=" * 70)
print("🔬 515180 vs 588170 对比分析 — 为什么一个有信号一个无？")
print("=" * 70)

for code in ['515180', '588170']:
    print(f"\n【{code} 2026-08-25 分析】")
    traces = []
    with open(f't_io/traces/decision_trace_2026-08-25.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get("code") == code:
                traces.append(d)
    
    if not traces:
        print("  无记录")
        continue
    
    # 找buy_score>0的记录
    buy_signals = [t for t in traces if t.get("buy_score", 0) > 0]
    
    print(f"  总记录: {len(traces)}")
    print(f"  buy_score>0: {len(buy_signals)}")
    
    if buy_signals:
        print(f"\n  BUY_LOW 信号详情:")
        for t in buy_signals[:5]:
            print(f"    {t.get('scan_time')} price={t.get('price')} vwap={t.get('vwap', 0):.3f} rsi={t.get('rsi', 0):.1f} buy_score={t.get('buy_score')}")
    
    # 价格统计
    prices = [t.get('price', 0) for t in traces if t.get('price')]
    if prices:
        print(f"\n  价格统计: min={min(prices):.3f} max={max(prices):.3f}")
    
    # RSI统计
    rsis = [t.get('rsi', 0) for t in traces if t.get('rsi')]
    if rsis:
        print(f"  RSI统计: min={min(rsis):.1f} max={max(rsis):.1f} avg={sum(rsis)/len(rsis):.1f}")
    
    # VWAP统计
    vwaps = [t.get('vwap', 0) for t in traces if t.get('vwap')]
    if vwaps:
        print(f"  VWAP统计: min={min(vwaps):.3f} max={max(vwaps):.3f} avg={sum(vwaps)/len(vwaps):.3f}")

# 检查08-24的对比
print("\n" + "=" * 70)
print("🔬 08-24 vs 08-25 588170 对比")
print("=" * 70)

for date in ['2026-08-24', '2026-08-25']:
    print(f"\n【{date} 588170】")
    traces = []
    with open(f't_io/traces/decision_trace_{date}.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get("code") == "588170":
                traces.append(d)
    
    buy_signals = [t for t in traces if t.get("buy_score", 0) > 0]
    prices = [t.get('price', 0) for t in traces if t.get('price')]
    rsis = [t.get('rsi', 0) for t in traces if t.get('rsi')]
    
    print(f"  记录数: {len(traces)}")
    print(f"  buy_score>0: {len(buy_signals)}")
    if prices:
        print(f"  价格: {min(prices):.3f} ~ {max(prices):.3f}")
    if rsis:
        print(f"  RSI: {min(rsis):.1f} ~ {max(rsis):.1f}")
    
    # 看第一条和最后一条
    if traces:
        first = traces[0]
        last = traces[-1]
        print(f"  开盘: {first.get('scan_time')} price={first.get('price')} rsi={first.get('rsi', 0):.1f}")
        print(f"  收盘: {last.get('scan_time')} price={last.get('price')} rsi={last.get('rsi', 0):.1f}")

print("\n" + "=" * 70)
print("分析完成")
print("=" * 70)
