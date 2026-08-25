import json

print("=" * 70)
print("🔬 检查 decision_trace 中是否包含 bb_pct / rsi_5m 指标字段")
print("=" * 70)

for date in ['2026-08-24', '2026-08-25']:
    print(f"\n【{date} 588170 decision_trace 字段检查】")
    traces = []
    with open(f't_io/traces/decision_trace_{date}.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get("code") == "588170":
                traces.append(d)
    
    if not traces:
        print("  无记录")
        continue
    
    # 检查第一条记录的所有字段
    first = traces[0]
    print(f"  总记录: {len(traces)}")
    print(f"  字段列表: {sorted(first.keys())}")
    
    # 查找指标相关字段
    indicator_fields = [k for k in first.keys() if any(x in k.lower() for x in ['bb', 'boll', 'rsi', 'macd', 'vwap', 'ind'])]
    print(f"  指标相关字段: {indicator_fields}")
    
    # 如果有指标字段，打印值
    for field in indicator_fields:
        print(f"    {field}: {first.get(field)}")
    
    # 打印第一条记录的完整内容（精简）
    print(f"\n  第一条记录:")
    print(f"    scan_time: {first.get('scan_time')}")
    print(f"    price: {first.get('price')}")
    print(f"    vwap: {first.get('vwap')}")
    print(f"    rsi: {first.get('rsi')}")
    print(f"    buy_score: {first.get('buy_score')}")
    print(f"    sell_score: {first.get('sell_score')}")
    print(f"    decision: {first.get('decision')}")
    print(f"    decision_reason: {first.get('decision_reason')}")
    
    # 检查是否有 _ind 或 _fac 等字段
    for k in sorted(first.keys()):
        if k not in ['scan_time', 'code', 'name', 'price', 'vwap', 'rsi', 'buy_score', 'sell_score', 'buy_threshold', 'sell_threshold', 'decision', 'decision_reason', 'buy_block', 'sell_block', 'buy_factors', 'sell_factors', 'engine']:
            v = first.get(k)
            if v is not None and v != {} and v != []:
                print(f"    {k}: {v}")

print("\n" + "=" * 70)
print("检查完成")
print("=" * 70)
