import json
from collections import defaultdict
import statistics

date = "2026-08-24"
holdings = ["515180", "002639", "588170", "600481"]

# 读取 decision_trace
print("=" * 60)
print("📊 各持仓标的 今日决策汇总")
print("=" * 60)

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

    # 提取关键指标
    buy_scores = [t.get("buy_score", 0) for t in traces if t.get("buy_score")]
    sell_scores = [t.get("sell_score", 0) for t in traces if t.get("sell_score")]
    prices = [t.get("current_price", 0) for t in traces if t.get("current_price")]
    highs = [t.get("high", 0) for t in traces if t.get("high")]
    lows = [t.get("low", 0) for t in traces if t.get("low")]
    actions = [t.get("action", "") for t in traces]
    decisions = [t.get("decision", "") for t in traces]

    max_buy = max(buy_scores) if buy_scores else 0
    max_sell = max(sell_scores) if sell_scores else 0
    max_price = max(prices) if prices else 0
    min_price = min(prices) if prices else 0
    amplitude = ((max_price - min_price) / min_price * 100) if min_price > 0 else 0

    buy_count = sum(1 for a in actions if a == "BUY")
    sell_count = sum(1 for a in actions if a == "SELL")
    hold_count = sum(1 for a in actions if a == "HOLD")

    # 最后一条记录
    last = traces[-1]
    name = last.get("name", "")

    # 找最大sell_score的时间点
    max_sell_idx = sell_scores.index(max_sell) if sell_scores else -1
    max_sell_time = traces[max_sell_idx].get("time", "") if max_sell_idx >= 0 else ""

    # 找最大buy_score的时间点
    max_buy_idx = buy_scores.index(max_buy) if buy_scores else -1
    max_buy_time = traces[max_buy_idx].get("time", "") if max_buy_idx >= 0 else ""

    print(f"\n【{code} {name}】")
    print(f"  最高 sell_score: {max_sell:.1f} @ {max_sell_time}")
    print(f"  最高 buy_score:  {max_buy:.1f} @ {max_buy_time}")
    print(f"  价格区间: {min_price:.3f} ~ {max_price:.3f} (振幅 {amplitude:.2f}%)")
    print(f"  收盘: {last.get('current_price', 0):.3f}")
    print(f"  动作统计: BUY={buy_count} SELL={sell_count} HOLD={hold_count}")
    print(f"  最后状态: action={last.get('action', '')} decision={last.get('decision', '')}")

    # 是否有 PUSH 动作
    pushes = [t for t in traces if t.get("push_sent")]
    if pushes:
        print(f"  推送记录 ({len(pushes)}条):")
        for p in pushes[:3]:
            print(f"    {p.get('time', '')} {p.get('action', '')} {p.get('decision', '')} price={p.get('current_price', 0):.3f}")

# 读取 shadow_signals
print("\n" + "=" * 60)
print("📊 各持仓标的 Shadow Signals (接近阈值但未触发)")
print("=" * 60)

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
    for s in shadows[:5]:
        reason = s.get("stand_down_reason", "")
        score = s.get("sell_score", s.get("buy_score", 0))
        direction = s.get("direction", "")
        print(f"  {s.get('time', '')} {direction} score={score:.1f} 拦截原因: {reason}")

# 读取 position_builder (建仓扫描)
print("\n" + "=" * 60)
print("📊 建仓扫描结果 (position_builder)")
print("=" * 60)

signals = []
approaching = []
with open(f't_io/traces/position_builder_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        rec = d.get("recommendation", "")
        if rec == "signal":
            signals.append(d)
        elif rec == "approaching":
            approaching.append(d)

print(f"\n今日 signal 股: {len(signals)} 只")
for s in signals[:5]:
    code = s.get("code", "")
    name = s.get("name", "")
    score = s.get("composite_score", 0)
    gap = s.get("gap_to_trigger", "")
    print(f"  {code} {name}: 综合得分={score:.1f} 距触发={gap}")

print(f"\n今日 approaching 股: {len(approaching)} 只")
for a in approaching[:5]:
    code = a.get("code", "")
    name = a.get("name", "")
    gap = a.get("gap_to_trigger", "")
    print(f"  {code} {name}: 距触发={gap}")

# 读取 sizing_advice
print("\n" + "=" * 60)
print("📊 加仓建议 (sizing_advice)")
print("=" * 60)

advices = []
with open(f't_io/traces/sizing_advice_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("action") in ["add", "first_add", "cover"]:
            advices.append(d)

if advices:
    print(f"\n今日加仓建议: {len(advices)} 条")
    for a in advices[:5]:
        code = a.get("code", "")
        action = a.get("action", "")
        qty = a.get("suggested_qty", 0)
        price = a.get("suggested_price", 0)
        reason = a.get("reason", "")
        print(f"  {code} {action}: 建议 {qty} 股 @ {price:.3f} ({reason})")
else:
    print("\n今日无加仓建议")
