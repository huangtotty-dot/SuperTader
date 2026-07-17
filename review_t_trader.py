# -*- coding: utf-8 -*-
"""
深度复盘 t_trader 日志：统计买入/卖出信号及T-cycle完整性
"""
import os, re, glob, json
from collections import defaultdict, Counter

LOG_DIR = r"E:\05_量化\logs"

# 收集所有 t_trader 日志
log_files = sorted(glob.glob(os.path.join(LOG_DIR, "t_trader_sys_*.log")))
print(f"找到 {len(log_files)} 个 t_trader 日志文件")

# 统计结构
stats = {
    "days": 0,
    "total_buy_signals": 0,
    "total_sell_signals": 0,
    "total_panic_sell": 0,
    "by_day": defaultdict(lambda: {"buy":0, "sell":0, "panic":0, "codes":set()}),
    "by_code": defaultdict(lambda: {"buy":0, "sell":0, "panic":0, "buy_scores":[], "sell_scores":[] }),
    "buy_scores": [],
    "sell_scores": [],
    "t_cycles": defaultdict(list),  # code -> [(buy_ts, buy_score, sell_ts, sell_score, same_cycle)]
    "buy_without_sell": defaultdict(list),  # code -> [buy_ts]
    "sell_without_buy": defaultdict(list),  # code -> [sell_ts]
    "stand_down_reasons": Counter(),
    "block_reasons": {"buy": Counter(), "sell": Counter()},
}

def parse_time(line):
    m = re.match(r'(\d{2}:\d{2}:\d{2})', line)
    return m.group(1) if m else ""

for log_path in log_files:
    day = os.path.basename(log_path).replace("t_trader_sys_", "").replace(".log", "")
    stats["days"] += 1
    
    # 每只股票当天的信号序列
    day_signals = defaultdict(list)  # code -> [(ts, action, score, line)]
    
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            
            # 信号触发
            if "【触发】" in line:
                ts = parse_time(line)
                # 买入信号
                m = re.search(r'低吸信号.*\((\d{6})\).*得:(\d+)分', line)
                if m:
                    code, score = m.group(1), int(m.group(2))
                    stats["total_buy_signals"] += 1
                    stats["by_day"][day]["buy"] += 1
                    stats["by_day"][day]["codes"].add(code)
                    stats["by_code"][code]["buy"] += 1
                    stats["by_code"][code]["buy_scores"].append(score)
                    stats["buy_scores"].append(score)
                    day_signals[code].append((ts, "BUY", score, line))
                    continue
                
                # 卖出信号
                m = re.search(r'高抛信号.*\((\d{6})\).*得:(\d+)分', line)
                if m:
                    code, score = m.group(1), int(m.group(2))
                    stats["total_sell_signals"] += 1
                    stats["by_day"][day]["sell"] += 1
                    stats["by_day"][day]["codes"].add(code)
                    stats["by_code"][code]["sell"] += 1
                    stats["by_code"][code]["sell_scores"].append(score)
                    stats["sell_scores"].append(score)
                    day_signals[code].append((ts, "SELL", score, line))
                    continue
                
                # 恐慌卖出
                m = re.search(r'恐慌卖出.*\((\d{6})\).*得:(\d+)分', line)
                if m:
                    code, score = m.group(1), int(m.group(2))
                    stats["total_panic_sell"] += 1
                    stats["by_day"][day]["panic"] += 1
                    stats["by_day"][day]["codes"].add(code)
                    stats["by_code"][code]["panic"] += 1
                    stats["by_code"][code]["sell_scores"].append(score)
                    day_signals[code].append((ts, "PANIC", score, line))
                    continue
            
            # 停手原因
            m = re.search(r'停手[:：]([^\s|]+)', line)
            if m:
                reason = m.group(1).strip()
                stats["stand_down_reasons"][reason] += 1
            
            # 阻塞原因 (从 diagnostics 或日志中提取)
            m = re.search(r'阻塞原因[:：](\w+)', line)
            if m:
                reason = m.group(1).strip()
                # 判断是买还是卖阻塞
                if "buy" in line.lower() or "买入" in line or "多头" in line:
                    stats["block_reasons"]["buy"][reason] += 1
                else:
                    stats["block_reasons"]["sell"][reason] += 1
    
    # 分析当天的 T-cycle
    for code, sigs in day_signals.items():
        # 按时间排序
        sigs.sort(key=lambda x: x[0])
        # 匹配 buy->sell 对
        last_buy = None
        for ts, action, score, line in sigs:
            if action == "BUY":
                last_buy = (ts, score)
            elif action in ("SELL", "PANIC") and last_buy:
                # 完成一个 T-cycle
                stats["t_cycles"][code].append({
                    "day": day,
                    "buy_ts": last_buy[0],
                    "buy_score": last_buy[1],
                    "sell_ts": ts,
                    "sell_score": score,
                    "sell_type": action,
                    "completed": True,
                })
                last_buy = None
            elif action in ("SELL", "PANIC") and not last_buy:
                # 先卖后买（逆T）或不匹配
                stats["sell_without_buy"][code].append({"day": day, "ts": ts, "score": score})
        
        # 未卖出的买入
        if last_buy:
            stats["buy_without_sell"][code].append({"day": day, "ts": last_buy[0], "score": last_buy[1]})

# 输出统计报告
print("\n" + "="*70)
print("【做T信号复盘报告】")
print("="*70)
print(f"\n统计天数: {stats['days']}")
print(f"总买入信号: {stats['total_buy_signals']}")
print(f"总卖出信号: {stats['total_sell_signals']}")
print(f"总恐慌卖出: {stats['total_panic_sell']}")
print(f"卖出/买入比: {stats['total_sell_signals']/max(stats['total_buy_signals'],1):.2f}")

if stats['buy_scores']:
    print(f"\n买入信号分数分布: 最低={min(stats['buy_scores'])}, 最高={max(stats['buy_scores'])}, 平均={sum(stats['buy_scores'])/len(stats['buy_scores']):.1f}")
if stats['sell_scores']:
    print(f"卖出信号分数分布: 最低={min(stats['sell_scores'])}, 最高={max(stats['sell_scores'])}, 平均={sum(stats['sell_scores'])/len(stats['sell_scores']):.1f}")

# T-cycle 分析
total_cycles = sum(len(v) for v in stats["t_cycles"].values())
total_buy_only = sum(len(v) for v in stats["buy_without_sell"].values())
total_sell_only = sum(len(v) for v in stats["sell_without_buy"].values())
print(f"\n【T-cycle 完整性】")
print(f"  完整 T-cycle (买后卖出): {total_cycles}")
print(f"  只买未卖: {total_buy_only}")
print(f"  只卖未买: {total_sell_only}")
print(f"  T-cycle 完成率: {total_cycles / max(total_cycles + total_buy_only, 1) * 100:.1f}%")

# 按日统计
print(f"\n【按日统计】")
for day in sorted(stats["by_day"].keys())[-7:]:
    d = stats["by_day"][day]
    print(f"  {day}: 买入={d['buy']}, 卖出={d['sell']}, 恐慌={d['panic']}, 涉及股票={len(d['codes'])}")

# 按股票统计
print(f"\n【按股票统计】")
for code in sorted(stats["by_code"].keys(), key=lambda c: stats["by_code"][c]["buy"] + stats["by_code"][c]["sell"], reverse=True)[:15]:
    c = stats["by_code"][code]
    buy_avg = sum(c["buy_scores"])/len(c["buy_scores"]) if c["buy_scores"] else 0
    sell_avg = sum(c["sell_scores"])/len(c["sell_scores"]) if c["sell_scores"] else 0
    print(f"  {code}: 买入={c['buy']}, 卖出={c['sell']}, 恐慌={c['panic']}, 买均分={buy_avg:.1f}, 卖均分={sell_avg:.1f}")

# 只买未卖的股票
print(f"\n【只买未卖的股票】")
for code, buys in sorted(stats["buy_without_sell"].items(), key=lambda x: len(x[1]), reverse=True)[:10]:
    print(f"  {code}: {len(buys)} 次买入未卖出")
    for b in buys[-3:]:
        print(f"    -> {b['day']} {b['ts']} 分数{b['score']}")

# 停手原因
print(f"\n【停手原因统计】")
for reason, count in stats["stand_down_reasons"].most_common(10):
    print(f"  {reason}: {count}")

# 阻塞原因
print(f"\n【买入阻塞原因】")
for reason, count in stats["block_reasons"]["buy"].most_common(10):
    print(f"  {reason}: {count}")

print(f"\n【卖出阻塞原因】")
for reason, count in stats["block_reasons"]["sell"].most_common(10):
    print(f"  {reason}: {count}")

# 保存详细报告
report_path = r"E:\06_T\t_trader_review.json"
with open(report_path, "w", encoding="utf-8") as f:
    # 转换 set 为 list 以便 JSON 序列化
    report = {
        "days": stats["days"],
        "total_buy_signals": stats["total_buy_signals"],
        "total_sell_signals": stats["total_sell_signals"],
        "total_panic_sell": stats["total_panic_sell"],
        "buy_sell_ratio": stats["total_sell_signals"] / max(stats["total_buy_signals"], 1),
        "t_cycles": {k: v for k, v in stats["t_cycles"].items()},
        "buy_without_sell": {k: v for k, v in stats["buy_without_sell"].items()},
        "sell_without_buy": {k: v for k, v in stats["sell_without_buy"].items()},
        "stand_down_reasons": dict(stats["stand_down_reasons"]),
        "block_reasons": {k: dict(v) for k, v in stats["block_reasons"].items()},
    }
    json.dump(report, f, ensure_ascii=False, indent=2)

print(f"\n详细报告已保存: {report_path}")
