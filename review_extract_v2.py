import json
from collections import defaultdict

date = "2026-08-24"
holdings = ["515180", "002639", "588170", "600481"]

print("=" * 70)
print("📊 各持仓标的 今日决策与价格汇总")
print("=" * 70)

for code in holdings:
    traces = []
    with open(f't_io/traces/decision_trace_{date}.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get("code") == code:
                traces.append(d)

    if not traces:
        print(f"\n【{code}】无记录")
        continue

    # 兼容两种字段名: price / current_price
    prices = []
    for t in traces:
        p = t.get("price") or t.get("current_price") or 0
        if p:
            prices.append(p)

    buy_scores = [t.get("buy_score", 0) for t in traces]
    sell_scores = [t.get("sell_score", 0) for t in traces]

    max_buy = max(buy_scores) if buy_scores else 0
    max_sell = max(sell_scores) if sell_scores else 0
    max_price = max(prices) if prices else 0
    min_price = min(prices) if prices else 0
    amplitude = ((max_price - min_price) / min_price * 100) if min_price > 0 else 0

    # 找最大评分时间点
    max_buy_records = [t for t in traces if t.get("buy_score") == max_buy and max_buy > 0]
    max_sell_records = [t for t in traces if t.get("sell_score") == max_sell and max_sell > 0]

    # 推送记录
    pushes = [t for t in traces if t.get("push_sent") or t.get("action") in ["BUY", "SELL"]]

    last = traces[-1]
    name = last.get("name", "")

    # 统计决策类型
    decisions = defaultdict(int)
    for t in traces:
        decisions[t.get("decision", "UNKNOWN")] += 1

    print(f"\n【{code} {name}】记录数: {len(traces)}")
    print(f"  价格区间: {min_price:.3f} ~ {max_price:.3f} (振幅 {amplitude:.2f}%)")
    print(f"  最高 buy_score:  {max_buy:.1f}")
    if max_buy_records:
        r = max_buy_records[0]
        p = r.get("price") or r.get("current_price") or 0
        print(f"    @ {r.get('scan_time', '')} price={p:.3f} decision={r.get('decision', '')} reason={r.get('decision_reason', '')}")
    print(f"  最高 sell_score: {max_sell:.1f}")
    if max_sell_records:
        r = max_sell_records[0]
        p = r.get("price") or r.get("current_price") or 0
        print(f"    @ {r.get('scan_time', '')} price={p:.3f} decision={r.get('decision', '')} reason={r.get('decision_reason', '')}")

    print(f"  决策分布: {dict(decisions)}")

    if pushes:
        print(f"  推送/交易记录 ({len(pushes)}条):")
        for p in pushes[:5]:
            px = p.get("price") or p.get("current_price") or 0
            print(f"    {p.get('scan_time', '')} {p.get('action', '')} price={px:.3f} reason={p.get('decision_reason', '')}")

# Shadow Signals
print("\n" + "=" * 70)
print("📊 Shadow Signals (接近阈值但被拦截)")
print("=" * 70)

for code in holdings:
    shadows = []
    with open(f't_io/traces/shadow_signals_{date}.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get("code") == code:
                shadows.append(d)

    if not shadows:
        print(f"\n【{code}】无 shadow signals")
        continue

    name = shadows[0].get("name", "")
    print(f"\n【{code} {name}】共 {len(shadows)} 条")

    # 按拦截原因分组
    reasons = defaultdict(int)
    for s in shadows:
        reasons[s.get("miss_reason", "未知")] += 1

    for reason, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {reason}: {cnt}次")

    # 详细记录（前3条）
    for s in shadows[:3]:
        score = s.get("best_signal_score", 0)
        direction = s.get("best_signal_type", "")
        price = s.get("current_price", 0)
        print(f"    {s.get('scan_time', '')} {direction} score={score:.1f} price={price:.3f}")

# 建仓扫描
print("\n" + "=" * 70)
print("📊 建仓扫描结果 (position_builder)")
print("=" * 70)

signals = []
approaching = []
with open(f't_io/traces/position_builder_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        rec = d.get("verdict", "")
        # verdict 可能是 weak/approaching/signal 等
        if rec == "signal":
            signals.append(d)
        elif rec == "approaching" or d.get("channel"):
            # 检查 approach_status
            if d.get("approach_status"):
                approaching.append(d)

# 重新读取，用更精确的逻辑
signals = []
approaching = []
weak = []
with open(f't_io/traces/position_builder_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        v = d.get("verdict", "")
        if v == "signal":
            signals.append(d)
        elif v == "approaching":
            approaching.append(d)
        else:
            weak.append(d)

print(f"\n今日 verdict=signal 的标的: {len(signals)} 只")
for s in signals[:5]:
    code = s.get("code", "")
    name = s.get("name", "")
    score = s.get("composite_score", 0)
    ch = s.get("channel", "")
    print(f"  {code} {name}: 综合得分={score:.1f} 通道={ch}")

print(f"\n今日 verdict=approaching 的标的: {len(approaching)} 只")
for a in approaching[:5]:
    code = a.get("code", "")
    name = a.get("name", "")
    score = a.get("composite_score", 0)
    status = a.get("approach_status", "")
    print(f"  {code} {name}: 综合得分={score:.1f} status={status}")

# 加仓建议
print("\n" + "=" * 70)
print("📊 加仓建议 (sizing_advice)")
print("=" * 70)

advices = []
with open(f't_io/traces/sizing_advice_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("action") in ["add", "first_add", "cover", "reduce"]:
            advices.append(d)

if advices:
    print(f"\n今日 sizing 动作: {len(advices)} 条")
    for a in advices[:8]:
        code = a.get("code", "")
        action = a.get("action", "")
        qty = a.get("suggested_qty", 0)
        price = a.get("suggested_price", 0)
        reason = a.get("reason", "")
        print(f"  {code} {action}: 建议 {qty} 股 @ {price:.3f} ({reason})")
else:
    print("\n今日无 sizing advice")

# Index resonance
print("\n" + "=" * 70)
print("📊 大盘/指数共振状态 (index_resonance)")
print("=" * 70)

with open(f't_io/traces/index_resonance_{date}.jsonl', 'r', encoding='utf-8') as f:
    lines = f.readlines()
    if lines:
        # 取最后一条
        d = json.loads(lines[-1])
        print(f"\n最新记录 @ {d.get('scan_time', '')}")
        print(f"  大盘状态: {d.get('market_state', {})}")
        print(f"  板块状态: {d.get('sector_state', {})}")
        print(f"  共振结论: {d.get('resonance', {})}")
    else:
        print("\n无记录")
