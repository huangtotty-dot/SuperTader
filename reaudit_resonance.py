import json

print("=" * 70)
print("🔍 重新审视 index_resonance trace 数据")
print("=" * 70)

# 读取所有 trace 记录
records = []
with open('t_io/traces/index_resonance_2026-08-24.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        records.append(json.loads(line))

print(f"\n总记录数: {len(records)}")

# 统计 missing 和 gate_pass
missing_count = sum(1 for r in records if r.get("missing"))
pass_count = sum(1 for r in records if r.get("gate_pass"))
fail_count = sum(1 for r in records if not r.get("gate_pass") and not r.get("missing"))

print(f"\nmissing=True: {missing_count}")
print(f"gate_pass=True: {pass_count}")
print(f"gate_pass=False (非missing): {fail_count}")

# 查看 missing=True 的记录
if missing_count > 0:
    print("\n【missing=True 的记录】")
    for r in records:
        if r.get("missing"):
            print(f"  {r.get('scan_time')} {r.get('code')} reason={r.get('reason')}")
else:
    print("\n✅ 没有 missing=True 的记录！数据采集是正常的！")

# 查看 gate_pass=True 的记录
if pass_count > 0:
    print(f"\n【gate_pass=True 的记录 ({pass_count}条)】")
    for r in records:
        if r.get("gate_pass"):
            print(f"  {r.get('scan_time')} {r.get('code')} price={r.get('price')} "
                  f"index={r.get('index_close')} vs MA5={r.get('index_ma5_5m')}")

# 查看 gate_pass=False 的记录（按代码分组）
print(f"\n【gate_pass=False 的详情】")
for code in ['588170', '002639', '515180']:
    code_records = [r for r in records if r.get("code") == code and not r.get("gate_pass") and not r.get("missing")]
    if code_records:
        print(f"\n  {code}: 共 {len(code_records)} 条被拦截")
        # 显示原因分布
        reasons = {}
        for r in code_records:
            reason = r.get("gate_reason", "")
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, cnt in list(reasons.items())[:2]:
            print(f"    {reason[:80]}... ({cnt}次)")

# 关键问题：11:15:17 gate_pass=True 后为什么没有推送？
print("\n" + "=" * 70)
print("🔍 深挖：11:15:17 gate_pass=True 后发生了什么？")
print("=" * 70)

# 读取 shadow_signals
shadows = []
with open('t_io/traces/shadow_signals_2026-08-24.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        shadows.append(json.loads(line))

# 找 11:15 附近的 shadow
near_1115 = [s for s in shadows if "11:15" <= s.get("scan_time", "") < "11:20"]
print(f"\n11:15-11:19 shadow_signals: {len(near_1115)} 条")
for s in near_1115:
    print(f"  {s.get('scan_time')} {s.get('code')} miss={s.get('miss_reason')}")

# 读取 decision_trace 11:15 附近
dt_1115 = []
with open('t_io/traces/decision_trace_2026-08-24.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("code") == "588170" and "11:15" <= d.get("scan_time", "") < "11:20":
            dt_1115.append(d)

print(f"\n11:15-11:19 decision_trace (588170): {len(dt_1115)} 条")
if dt_1115:
    for d in dt_1115[:5]:
        print(f"  {d.get('scan_time')} action={d.get('action')} "
              f"decision={d.get('decision')} reason={d.get('decision_reason')} "
              f"push={d.get('push_sent')}")

# 600481 的决策
print("\n" + "=" * 70)
print("🔍 600481 全天决策详情")
print("=" * 70)

dt_600481 = []
with open('t_io/traces/decision_trace_2026-08-24.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        d = json.loads(line)
        if d.get("code") == "600481":
            dt_600481.append(d)

# 找有非0评分的
non_zero = [d for d in dt_600481 if d.get("buy_score", 0) > 0 or d.get("sell_score", 0) > 0]
print(f"\n总记录: {len(dt_600481)}, 非0评分: {len(non_zero)}")

# 看前3条详情
for d in dt_600481[:3]:
    print(f"\n  {d.get('scan_time')}")
    print(f"    price={d.get('price')} vwap={d.get('vwap')} rsi={d.get('rsi')}")
    print(f"    buy_score={d.get('buy_score')} sell_score={d.get('sell_score')}")
    print(f"    decision={d.get('decision')} reason={d.get('decision_reason')}")
    print(f"    buy_factors={d.get('buy_factors')}")
    print(f"    sell_factors={d.get('sell_factors')}")

# 检查是否有index_resonance记录（600481映射到哪个指数？）
ir_600481 = [r for r in records if r.get("code") == "600481"]
print(f"\n600481 的 index_resonance 记录: {len(ir_600481)} 条")

print("\n" + "=" * 70)
print("排查完成")
print("=" * 70)
