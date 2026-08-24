import json

date = "2026-08-24"

# ========== 600481 深度诊断 ==========
print("=" * 70)
print("🔍 600481 双良节能 — 全天0评分诊断")
print("=" * 70)

traces_600481 = []
with open(f't_io/traces/decision_trace_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("code") == "600481":
            traces_600481.append(d)

if traces_600481:
    # 取前5条看结构
    print(f"\n总记录数: {len(traces_600481)}")
    print("\n前3条记录详情:")
    for t in traces_600481[:3]:
        print(f"  time={t.get('scan_time')} price={t.get('price')} vwap={t.get('vwap', 0):.3f} rsi={t.get('rsi', 0):.1f}")
        print(f"    buy_score={t.get('buy_score')} sell_score={t.get('sell_score')}")
        print(f"    buy_threshold={t.get('buy_threshold')} sell_threshold={t.get('sell_threshold')}")
        print(f"    decision={t.get('decision')} reason={t.get('decision_reason')}")
        print(f"    buy_factors={t.get('buy_factors', {})}")
        print(f"    sell_factors={t.get('sell_factors', {})}")
        print(f"    buy_block={t.get('buy_block', [])}")
        print(f"    sell_block={t.get('sell_block', [])}")
        print(f"    engine={t.get('engine', '')}")

    # 检查是否有非0评分
    non_zero = [t for t in traces_600481 if t.get("buy_score", 0) > 0 or t.get("sell_score", 0) > 0]
    print(f"\n非0评分记录数: {len(non_zero)}")

    # 统计decision_reason分布
    reasons = {}
    for t in traces_600481:
        r = t.get("decision_reason", "")
        reasons[r] = reasons.get(r, 0) + 1
    print(f"\ndecision_reason 分布: {reasons}")

    # 价格走势
    prices = [t.get("price", 0) for t in traces_600481 if t.get("price")]
    if prices:
        print(f"\n价格统计: min={min(prices):.3f} max={max(prices):.3f} avg={sum(prices)/len(prices):.3f}")

# ========== 588170 0.996 时刻深度诊断 ==========
print("\n" + "=" * 70)
print("🔍 588170 科创半导体ETF — 0.996 拦截诊断")
print("=" * 70)

traces_588170 = []
with open(f't_io/traces/decision_trace_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("code") == "588170":
            traces_588170.append(d)

# 找 0.996 附近的记录
near_0996 = [t for t in traces_588170 if abs((t.get("price") or 0) - 0.996) < 0.005]
print(f"\nprice≈0.996 的记录数: {len(near_0996)}")

# 找第一条 buy_score=100 的记录
first_buy100 = None
for t in traces_588170:
    if t.get("buy_score") == 100.0:
        first_buy100 = t
        break

if first_buy100:
    print(f"\n第一条 buy_score=100 记录:")
    print(f"  time={first_buy100.get('scan_time')}")
    print(f"  price={first_buy100.get('price')} vwap={first_buy100.get('vwap', 0):.3f}")
    print(f"  rsi={first_buy100.get('rsi', 0):.1f}")
    print(f"  buy_score={first_buy100.get('buy_score')} sell_score={first_buy100.get('sell_score')}")
    print(f"  buy_threshold={first_buy100.get('buy_threshold')} sell_threshold={first_buy100.get('sell_threshold')}")
    print(f"  decision={first_buy100.get('decision')} reason={first_buy100.get('decision_reason')}")
    print(f"  buy_factors={first_buy100.get('buy_factors', {})}")
    print(f"  buy_block={first_buy100.get('buy_block', [])}")

# shadow_signals 中 588170 的详细记录
print(f"\nshadow_signals 中 588170 的拦截详情:")
with open(f't_io/traces/shadow_signals_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("code") == "588170":
            print(f"  {d.get('scan_time')} {d.get('best_signal_type')} score={d.get('best_signal_score')}")
            print(f"    price={d.get('current_price')} miss_reason={d.get('miss_reason')}")
            print(f"    distance_to_buy={d.get('distance_to_buy_threshold')}")
            break  # 只看第一条

# ========== 检查 index_resonance 11:02 附近状态 ==========
print("\n" + "=" * 70)
print("🔍 Index Resonance — 11:02 附近大盘状态")
print("=" * 70)

resonance_records = []
with open(f't_io/traces/index_resonance_{date}.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        resonance_records.append(d)

# 找 11:00-11:05 的记录
near_1102 = [r for r in resonance_records if "11:0" in r.get("scan_time", "")]
print(f"\n11:00-11:09 的 index_resonance 记录数: {len(near_1102)}")
for r in near_1102[:3]:
    print(f"  {r.get('scan_time')}: market_state={r.get('market_state')} sector_state={r.get('sector_state')}")

# 取第一条和最后一条看是否有数据
if resonance_records:
    first = resonance_records[0]
    last = resonance_records[-1]
    print(f"\n第一条: {first.get('scan_time')} market={first.get('market_state')}")
    print(f"最后一条: {last.get('scan_time')} market={last.get('market_state')}")

# ========== 对比 002639 和 515180 的拦截模式 ==========
print("\n" + "=" * 70)
print("🔍 002639 / 515180 拦截模式对比")
print("=" * 70)

for code in ["002639", "515180"]:
    shadows = []
    with open(f't_io/traces/shadow_signals_{date}.jsonl', 'r', encoding='utf-8') as f:
        for line in f:
            d = json.loads(line)
            if d.get("code") == code:
                shadows.append(d)
    if shadows:
        print(f"\n【{code}】第一条 shadow:")
        s = shadows[0]
        print(f"  {s.get('scan_time')} {s.get('best_signal_type')} score={s.get('best_signal_score')}")
        print(f"    price={s.get('current_price')} miss_reason={s.get('miss_reason')}")

# ========== 600481 分钟快照分析 ==========
print("\n" + "=" * 70)
print("🔍 600481 分钟级数据快照")
print("=" * 70)

snap_file = f't_io/minute_snapshots/2026/08/600481_2026-08-24.json'
try:
    with open(snap_file, 'r', encoding='utf-8') as f:
        snap = json.load(f)
    print(f"\n快照keys: {list(snap.keys())[:10]}")
    if 'data' in snap:
        data = snap['data']
        print(f"数据点数: {len(data)}")
        if data:
            first = data[0]
            last = data[-1]
            print(f"第一条: {first}")
            print(f"最后一条: {last}")
            # 找最低价
            lows = [d.get('low', 0) for d in data if d.get('low')]
            if lows:
                print(f"最低: {min(lows):.3f} 最高: {max([d.get('high', 0) for d in data if d.get('high')]):.3f}")
except FileNotFoundError:
    print(f"\n{snap_file} 不存在")
except Exception as e:
    print(f"\n读取失败: {e}")

# ========== 588170 分钟快照分析 ==========
print("\n" + "=" * 70)
print("🔍 588170 分钟级数据快照")
print("=" * 70)

snap_file = f't_io/minute_snapshots/2026/08/588170_2026-08-24.json'
try:
    with open(snap_file, 'r', encoding='utf-8') as f:
        snap = json.load(f)
    if 'data' in snap:
        data = snap['data']
        print(f"数据点数: {len(data)}")
        if data:
            # 找 11:02 附近
            near_1102 = [d for d in data if "11:02" in d.get('time', '')]
            print(f"11:02 附近数据点数: {len(near_1102)}")
            for d in near_1102[:3]:
                print(f"  {d.get('time')}: open={d.get('open')} high={d.get('high')} low={d.get('low')} close={d.get('close')} vol={d.get('volume')}")
except FileNotFoundError:
    print(f"\n{snap_file} 不存在")
except Exception as e:
    print(f"\n读取失败: {e}")
