# -*- coding: utf-8 -*-
"""临时只读分析：解析 2026-08-31 t_trader 面板日志，提取每票价格序列/振幅，评估 Renko BUY_LOW +30min 表现。
仅读取日志，不写任何业务文件。"""
import re, json, datetime

LOG = r"t_io/logs/t_trader_sys_2026-08-31.log"
codes = ["588170", "600481", "002451", "300364", "002639"]

# 面板行形如: 中文在线(300364)       24.10     23.59    4.7%     多0.0/空0.0  无信号
row_re = re.compile(r"\((\d{6})\)\s+([\d.]+|-)\s+([\d.]+|-)\s+([\d.]+|-)%")
ts_re = re.compile(r"^(\d{2}:\d{2}:\d{2}) \[INFO\]")
panel_re = re.compile(r"护城河防御面板")

lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()

# 每条面板记录归属时间戳：面板头那行自带时间戳（如 "14:58:44 [INFO] " 后接面板）
records = []  # (ts, code, price, amp)
cur_ts = None
i = 0
while i < len(lines):
    ln = lines[i]
    m = ts_re.match(ln)
    if m:
        cur_ts = m.group(1)
    if panel_re.search(ln):
        # 向下找最多 10 行内的票行
        for j in range(i + 1, min(i + 12, len(lines))):
            rm = row_re.search(lines[j])
            if rm:
                c, p, v, a = rm.groups()
                if c in codes and p != "-":
                    records.append((cur_ts, c, float(p), None if a == "-" else float(a)))
        i += 12
        continue
    i += 1

print(f"面板样本点数: {len(records)}")
series = {c: [] for c in codes}
for ts, c, p, a in records:
    series[c].append((ts, p, a))

def to_min(ts):
    h, m, s = ts.split(":")
    return int(h) * 60 + int(m)

print("\n=== 各票当日价格序列概况（面板采样）===")
for c in codes:
    pts = series[c]
    if not pts:
        print(f"{c}: 无样本"); continue
    ps = [p for _, p, _ in pts]
    hi = max(ps); lo = min(ps)
    hi_ts = [t for t, p, _ in pts if p == hi][0]
    lo_ts = [t for t, p, _ in pts if p == lo][0]
    amps = [a for _, _, a in pts if a is not None]
    print(f"{c}: n={len(pts)} 高={hi}@{hi_ts} 低={lo}@{lo_ts} 面板振幅max={max(amps) if amps else '-'}% 首={pts[0]} 末={pts[-1]}")

# Renko BUY_LOW +30min 验证
print("\n=== Renko BUY_LOW +30min 表现（用面板最近邻价格估算）===")
buys = []
sells = []
for ln in open(r"t_io/traces/renko_t_2026-08-31_2026-08-31.jsonl", encoding="utf-8"):
    d = json.loads(ln)
    if d["action"] == "BUY_LOW":
        buys.append(d)
    else:
        sells.append(d)

def price_at(c, target_ts):
    """取 target_ts 之后最近的面板价；若无则取之前最近的"""
    pts = series.get(c, [])
    if not pts:
        return None, None
    after = [(t, p) for t, p, _ in pts if to_min(t) >= to_min(target_ts)]
    if after:
        return after[0]
    return (pts[-1][0], pts[-1][1])

win30 = 0
false_sig = 0
for b in buys:
    bts = b["ts"].split(" ")[1]
    bmin = to_min(bts)
    t30 = f"{(bmin + 30) // 60:02d}:{(bmin + 30) % 60:02d}:00"
    pts = series.get(b["code"], [])
    # +30min 内的最高价（反弹幅度）与 +30min 时点价
    win = [(t, p) for t, p, _ in pts if bmin <= to_min(t) <= bmin + 30]
    t30_ts, p30 = price_at(b["code"], t30)
    hi30 = max([p for _, p in win], default=None)
    chg = (p30 / b["price"] - 1) * 100 if p30 else None
    hi_chg = (hi30 / b["price"] - 1) * 100 if hi30 else None
    ok = chg is not None and chg > 0
    fs = hi_chg is not None and hi_chg < -1.0  # 30min内跌>1%（用区间最低价更准，下面改）
    lo30 = min([p for _, p in win], default=None)
    lo_chg = (lo30 / b["price"] - 1) * 100 if lo30 else None
    fs = lo_chg is not None and lo_chg < -1.0
    if ok: win30 += 1
    if fs: false_sig += 1
    print(f"{b['ts']} {b['code']} {b['name']} 买@{b['price']} macd15={b['macd15']} | +30min价≈{p30}({t30_ts}) 变动={chg:+.2f}% 区间内高={hi30}({hi_chg:+.2f}%) 低={lo30}({lo_chg:+.2f}%) | {'反弹' if ok else '未反弹'} {'假信号' if fs else ''}")

print(f"\n买入信号数={len(buys)} +30min收涨={win30} 胜率={win30/len(buys)*100:.1f}% 假信号(区间内跌>1%)={false_sig} 占比={false_sig/len(buys)*100:.1f}%")

# 闭环统计（BUY_LOW -> 后续 SELL_HIGH 配对，按 code+entry_price 匹配）
print("\n=== 闭环配对（trace 内 entry_price 匹配）===")
closed = []
open_buys = list(buys)
for s in sells:
    ep = s.get("entry_price")
    match = None
    for b in open_buys:
        if b["code"] == s["code"] and abs(b["price"] - ep) < 0.011:
            match = b; break
    if match:
        open_buys.remove(match)
        ret = (s["price"] / match["price"] - 1) * 100
        closed.append((match, s, ret))
        print(f"{match['code']} 买{match['ts'][11:]}@{match['price']} -> 卖{s['ts'][11:]}@{s['price']} ret={ret:+.3f}% [{s['exit_reason']}]")
    else:
        print(f"!! 未匹配卖出: {s['code']} {s['ts']} @{s['price']} entry={ep}")
print(f"未闭环买入: {[(b['code'], b['ts'], b['price']) for b in open_buys]}")
rets = [r for _, _, r in closed]
wins = [r for r in rets if r > 0]
tp = sum(1 for _, s, _ in closed if "目标止盈" in s["exit_reason"])
tail = sum(1 for _, s, _ in closed if "尾盘强平" in s["exit_reason"])
tstop = sum(1 for _, s, _ in closed if "时间止损" in s["exit_reason"])
print(f"闭环数={len(closed)} 胜率={len(wins)/len(closed)*100:.1f}% 均收益={sum(rets)/len(rets):+.3f}% 止盈={tp} 强平={tail} 时间止损={tstop}")
