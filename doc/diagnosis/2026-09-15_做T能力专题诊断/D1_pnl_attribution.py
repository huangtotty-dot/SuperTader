# -*- coding: utf-8 -*-
"""D1 绩效总账诊断 v2：做T的钱去哪了。
只读 t_io/，不修改任何生产文件。分析截至 2026-09-14（含），09-15 数据不纳入。

关键口径（v2 修正）：
- manual 通道（t_trader，实盘）做T台账 VIRTUAL_TRADES 已于 2026-09-14 删除，
  daily_pnl.t0_realized 仅剩 4 天非零（合计 -3.34 元）→ 实盘做T账面≈0。
- auto 通道（掘金）按 main.py 注释为【模拟盘】，其 fill 不落实盘台账。
- 回补链按 (code, sell_price) + 时间序匹配（同票同价可有多条链）。
- 链终态：闭环 / 过期 / 系统仍跟踪 / 当日新卖 / 状态丢失（无终态事件且系统不再跟踪）。
- 悬空机会成本只对「系统自认仍挂起 + 已过期」的链计，状态丢失链单列（存在净额重叠，直接相加会高估）。
输出：控制台中文表格 + D1_results.json
"""
import json, sys, glob, os, collections

sys.stdout.reconfigure(encoding='utf-8')

BASE = r"E:/superTrader"
OUT = os.path.join(BASE, "doc", "diagnosis", "2026-09-15_做T能力专题诊断")
LAST_DAY = "2026-09-14"

FEE_SELL = 0.00121   # 卖出：佣金+印花税
FEE_BUY = 0.00015    # 买入：佣金
AUDIT_FEE = 0.00025  # closure_audit compute_t0_pnl 双腿统一费率

dirty = collections.Counter()

def load_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            dirty[os.path.basename(path)] += 1
    return rows

# ---------- 1. daily_pnl ----------
dpnl = {}
for r in load_jsonl(os.path.join(BASE, "t_io", "logs", "daily_pnl.jsonl")):
    d = r.get("date", "")
    if not d or d > LAST_DAY:
        continue
    if d not in dpnl or r.get("pushed_at", "") > dpnl[d].get("pushed_at", ""):
        dpnl[d] = r
days = sorted(dpnl)
print(f"[daily_pnl] 交易日 {len(days)} 天: {days[0]} ~ {days[-1]}（09-15 已剔除）")

px, pct = {}, {}
for d in days:
    for s in dpnl[d].get("stocks", []):
        c = s["code"].replace("_B", "")
        px[(d, c)] = s.get("price", 0)
        pct[(d, c)] = s.get("day_pct", 0)

t0_nonzero = [(d, dpnl[d]["t0_realized"]) for d in days if abs(dpnl[d].get("t0_realized", 0)) > 1e-9]
print(f"[manual 实盘口径] t0_realized 非零 {len(t0_nonzero)}/{len(days)} 天，合计 {sum(v for _, v in t0_nonzero):+.2f} 元")
for d, v in t0_nonzero:
    print(f"    {d}: {v:+.2f}")

# ---------- 2. closure_audit ----------
ca = collections.defaultdict(list)
for r in load_jsonl(os.path.join(BASE, "t_io", "logs", "closure_audit.jsonl")):
    d = r.get("date", "")
    if d and d <= LAST_DAY:
        ca[d].append(r)
audit = {}
for d, recs in ca.items():
    best = {}
    for r in recs:
        for x in r.get("details") or []:
            c = x.get("code", "")
            k = (x.get("sold", 0) or 0) + (x.get("bought", 0) or 0)
            if c not in best or k > (best[c].get("sold", 0) or 0) + (best[c].get("bought", 0) or 0):
                best[c] = x
    for c, x in best.items():
        audit[(d, c)] = x
print(f"[closure_audit] {len(ca)} 天")

# ---------- 3. bridge events ----------
events = []
for f in sorted(glob.glob(os.path.join(BASE, "t_io", "bridge", "events_2026*.jsonl"))):
    for r in load_jsonl(f):
        t = r.get("time", "")[:10]
        if t and t <= LAST_DAY:
            events.append(r)
events.sort(key=lambda r: r.get("_ts", 0))

fills, seen_fill = [], set()
for r in events:
    if r.get("event") != "fill":
        continue
    key = (r.get("time", "")[:19], r.get("code"), r.get("side"), r.get("qty"), round(r.get("price", 0), 4))
    if key in seen_fill:
        dirty["fill_dup"] += 1
        continue
    seen_fill.add(key)
    fills.append(r)
print(f"[bridge] 事件 {len(events)} 条, 有效 fill {len(fills)} 条")

# 每日 fill vs 审计对照
fill_day = collections.defaultdict(lambda: collections.defaultdict(lambda: {"SELL": 0, "BUY": 0}))
for r in fills:
    fill_day[r["time"][:10]][r["code"]][r["side"]] += r.get("qty", 0)
print("\n===== 数据质量：bridge fills vs closure_audit 股数对照（仅列不一致）=====")
mism = []
for d in sorted(set(list(fill_day.keys()) + [dd for dd, _ in audit])):
    codes = set(fill_day.get(d, {}).keys()) | {c for (dd, c) in audit if dd == d}
    for c in codes:
        fs, fb = fill_day.get(d, {}).get(c, {}).get("SELL", 0), fill_day.get(d, {}).get(c, {}).get("BUY", 0)
        x = audit.get((d, c))
        asold = (x.get("sold", 0) if x else 0) or 0
        abought = (x.get("bought", 0) if x else 0) or 0
        if fs != asold or fb != abought:
            mism.append((d, c, fs, asold, fb, abought))
            print(f"  {d} {c}: events 卖{fs}/买{fb}  vs  审计 卖{asold}/买{abought}")

# ---------- 4. 回补链（时间序匹配）----------
chains = []          # 每条链一个 dict
open_by_key = collections.defaultdict(list)   # (code, price4) -> 未闭链 list（按时间）

def find_chain(code, sp, ts):
    k = (code, round(float(sp), 4))
    cands = open_by_key.get(k, [])
    live = [c for c in cands if c["q_open"] > 0]
    if live:
        return live[0]
    # 没有活链：归因到同 key 最近一条（记异常）
    if cands:
        dirty["late_event_on_closed_chain"] += 1
        return cands[-1]
    dirty["event_without_chain"] += 1
    return None

for r in events:
    e = r.get("event")
    code = r.get("code")
    sp = r.get("sell_price")
    if sp is None:
        continue
    ts = r.get("_ts", 0)
    if e == "buyback_armed":
        ch = {
            "code": code, "sell_price": float(sp), "qty": int(r.get("qty") or 0),
            "action": r.get("sell_action") or "?", "sell_time": r.get("time", "")[:19],
            "sell_date": r.get("time", "")[:10], "closed": [], "expired": None,
            "blocked": 0, "delayed": [], "restored": 0, "q_open": int(r.get("qty") or 0),
        }
        chains.append(ch)
        open_by_key[(code, round(float(sp), 4))].append(ch)
    else:
        ch = find_chain(code, sp, ts)
        if ch is None:
            continue
        if e == "buyback_filled":
            matched = int(r.get("qty") or 0)
            fill_q = int(r.get("fill_qty") or 0)
            q_used = min(matched, fill_q) if fill_q > 0 else matched
            q_used = min(q_used, ch["q_open"])  # 不超过链剩余
            ch["closed"].append({"date": r.get("time", "")[:10], "time": r.get("time", "")[:19],
                                 "buy_price": float(r.get("price") or 0), "qty": q_used,
                                 "matched_raw": matched, "fill_qty": fill_q})
            ch["q_open"] -= q_used
        elif e == "buyback_expired":
            ch["expired"] = r.get("time", "")[:19]
        elif e == "buyback_blocked":
            ch["blocked"] += 1
        elif e == "buyback_delayed":
            ch["delayed"].append((r.get("time", "")[:10], r.get("premium")))
        elif e == "buyback_restored":
            ch["restored"] += 1
            ch["restore_qty"] = int(r.get("qty") or 0)

print(f"[chains] 回补链 {len(chains)} 条")

# 系统仍跟踪的挂起链（09-14 早晨 restore 的）+ 09-14 当日新卖
pending_keys = set()
for r in events:
    if r.get("event") == "buyback_restored" and r.get("time", "")[:10] >= "2026-09-11":
        pending_keys.add((r.get("code"), round(float(r.get("sell_price")), 4)))

def classify(ch):
    if ch["q_open"] <= 0:
        return "闭环"
    if ch["expired"]:
        return "过期未回补"
    if ch["sell_date"] == LAST_DAY:
        return "当日新卖待回补"
    if (ch["code"], round(ch["sell_price"], 4)) in pending_keys and ch["restored"] > 0:
        return "系统仍挂起"
    return "状态丢失"

def env_of(date, code):
    p = pct.get((date, code))
    if p is None:
        return "未知"
    return "收涨" if p > 0.3 else ("收跌" if p < -0.3 else "震荡")

rows = []
for ch in chains:
    code, sp = ch["code"], ch["sell_price"]
    q_closed = sum(c["qty"] for c in ch["closed"])
    gross = sum((sp - c["buy_price"]) * c["qty"] for c in ch["closed"])
    fees = sum(c["qty"] * (sp * FEE_SELL + c["buy_price"] * FEE_BUY) for c in ch["closed"])
    status = classify(ch)
    # 悬空机会成本：仅对 过期/系统仍挂起/当日新卖 三类计（状态丢失链单列，存在净额重叠）
    open_cost, ref_info = 0.0, ""
    if ch["q_open"] > 0 and status != "状态丢失":
        if ch["expired"]:
            ref_d = max([d for d in days if d <= ch["expired"][:10] and (d, code) in px], default=None)
        else:
            ref_d = max([d for d in days if (d, code) in px], default=None)
        if ref_d:
            open_cost = (px[(ref_d, code)] - sp) * ch["q_open"]
            ref_info = f"{ref_d}收{px[(ref_d, code)]}"
    rows.append({
        "code": code, "action": ch["action"], "sell_date": ch["sell_date"], "sell_time": ch["sell_time"],
        "sell_price": sp, "qty": ch["qty"], "q_closed": q_closed, "q_open": ch["q_open"],
        "cross_day": bool(ch["closed"] and ch["closed"][0]["date"] > ch["sell_date"]),
        "gross": gross, "fees": fees, "net": gross - fees,
        "open_cost": open_cost, "ref_info": ref_info, "status": status,
        "blocked": ch["blocked"], "delayed": len(ch["delayed"]), "restored": ch["restored"],
        "env": env_of(ch["sell_date"], code),
        "overfill": sum(1 for c in ch["closed"] if c["matched_raw"] > c["fill_qty"] > 0),
    })

print("\n===== 逐链明细 =====")
print(f"{'卖出时间':<17}{'票':<7}{'动作':<11}{'卖价':>8}{'量':>6}{'闭环':>6}{'费前':>8}{'费用':>6}{'费后':>8}{'悬空':>5}{'悬空代价':>9} 状态")
for r in sorted(rows, key=lambda x: x["sell_time"]):
    print(f"{r['sell_time']:<17}{r['code']:<7}{r['action']:<11}{r['sell_price']:>8.3f}{r['qty']:>6}{r['q_closed']:>6}"
          f"{r['gross']:>8.2f}{r['fees']:>6.2f}{r['net']:>8.2f}{r['q_open']:>5}{r['open_cost']:>9.2f} {r['status']}{('|'+r['ref_info']) if r['ref_info'] else ''}")

tot_gross = sum(r["gross"] for r in rows)
tot_fees = sum(r["fees"] for r in rows)
tot_net = sum(r["net"] for r in rows)
tot_open = sum(r["open_cost"] for r in rows)
lost = [r for r in rows if r["status"] == "状态丢失"]
print(f"\n[合计] 闭环费前差价 {tot_gross:+.2f} | 费用 {tot_fees:.2f} | 闭环费后 {tot_net:+.2f}")
print(f"[悬空] 过期+挂起+当日新卖的机会成本合计 {tot_open:+.2f}（正=卖飞损失）")
print(f"[状态丢失] {len(lost)} 条链 {sum(r['q_open'] for r in lost)} 股无终态事件（经济效果被后续买入净额覆盖，不可直接相加）")
for r in lost:
    print(f"    {r['sell_date']} {r['code']} {r['action']} {r['q_open']}股@{r['sell_price']:.3f}")

# 口径B：逐日逐票净额（min(卖,买)，量加权均价，真实费率）——与 closure_audit 同构但费率真实
print("\n===== 口径B：日净额配对（真实费率 0.136% 双边）=====")
dayB = collections.defaultdict(lambda: {"gross": 0, "fees": 0, "net": 0, "matched": 0})
fills_by_dc = collections.defaultdict(lambda: {"SELL": [], "BUY": []})
for r in fills:
    fills_by_dc[(r["time"][:10], r["code"])][r["side"]].append((r.get("qty", 0), r.get("price", 0)))
for (d, c), v in sorted(fills_by_dc.items()):
    sq = sum(q for q, _ in v["SELL"]); bq = sum(q for q, _ in v["BUY"])
    m = min(sq, bq)
    if m <= 0:
        continue
    as_ = sum(q * p for q, p in v["SELL"]) / sq if sq else 0
    ab = sum(q * p for q, p in v["BUY"]) / bq if bq else 0
    gross = (as_ - ab) * m
    fee = m * (as_ * FEE_SELL + ab * FEE_BUY)
    dayB[d]["gross"] += gross; dayB[d]["fees"] += fee; dayB[d]["net"] += gross - fee; dayB[d]["matched"] += m
totB = {"gross": 0, "fees": 0, "net": 0, "matched": 0}
for d in sorted(dayB):
    a = dayB[d]
    print(f"  {d}: matched {a['matched']:>6} 股  费前 {a['gross']:>+8.2f}  费用 {a['fees']:>6.2f}  费后 {a['net']:>+8.2f}")
    for k in totB: totB[k] += a[k]
print(f"  合计: matched {totB['matched']} 股 费前 {totB['gross']:+.2f} 费用 {totB['fees']:.2f} 费后 {totB['net']:+.2f}")

# closure_audit 对照
ca_days = collections.defaultdict(float)
for (d, c), x in sorted(audit.items()):
    v = x.get("est_pnl")
    if v is None or (x.get("sold", 0) or 0) + (x.get("bought", 0) or 0) <= 0:
        continue
    ca_days[d] += v
ca_sum = sum(ca_days.values())
ca_corrupt = ca_days.get("2026-07-24", 0)
print(f"\n[closure_audit est_pnl] 合计 {ca_sum:+.2f}；剔除 07-24 损坏值 {ca_corrupt:+.2f} 后 {ca_sum - ca_corrupt:+.2f}（费率 0.00025×双腿口径）")

# ---------- 5. 逐日归因（链口径，归卖出日）----------
day_agg = collections.defaultdict(lambda: {"chains": 0, "qty": 0, "gross": 0, "fees": 0, "net": 0, "open_cost": 0})
for r in rows:
    a = day_agg[r["sell_date"]]
    a["chains"] += 1; a["qty"] += r["qty"]; a["gross"] += r["gross"]; a["fees"] += r["fees"]
    a["net"] += r["net"]; a["open_cost"] += r["open_cost"]
print("\n===== 逐日（链口径，卖出日）=====")
print(f"{'日期':<11}{'链':>3}{'股数':>7}{'闭环费后':>9}{'悬空代价':>9}{'净经济':>9}{'口径B费后':>9}  当日组合浮动")
for d in sorted(set(list(day_agg.keys()) + list(dayB.keys()))):
    a = day_agg.get(d, {"chains": 0, "qty": 0, "net": 0, "open_cost": 0})
    b = dayB.get(d, {"net": 0})
    fl = dpnl.get(d, {}).get("day_pnl_float", 0)
    print(f"{d:<11}{a['chains']:>3}{a['qty']:>7}{a['net']:>9.2f}{a['open_cost']:>9.2f}{a['net']-a['open_cost']:>9.2f}{b['net']:>9.2f}  {fl:>+10.1f}")

# ---------- 6. 按票 ----------
stk = collections.defaultdict(lambda: {"chains": 0, "qty": 0, "gross": 0, "fees": 0, "net": 0, "open_cost": 0, "win": 0, "loss": 0, "flat": 0})
for r in rows:
    s = stk[r["code"]]
    s["chains"] += 1; s["qty"] += r["qty"]; s["gross"] += r["gross"]; s["fees"] += r["fees"]
    s["net"] += r["net"]; s["open_cost"] += r["open_cost"]
    if r["status"] == "闭环":
        if r["net"] > 0: s["win"] += 1
        elif r["net"] < 0: s["loss"] += 1
        else: s["flat"] += 1
print("\n===== 按票归因（链口径）=====")
print(f"{'票':<8}{'链':>3}{'股数':>7}{'费前':>9}{'费用':>7}{'闭环费后':>9}{'悬空代价':>9}{'净经济':>9}  闭环胜/负/平")
for c, s in sorted(stk.items(), key=lambda kv: kv[1]["net"] - kv[1]["open_cost"]):
    print(f"{c:<8}{s['chains']:>3}{s['qty']:>7}{s['gross']:>9.2f}{s['fees']:>7.2f}{s['net']:>9.2f}{s['open_cost']:>9.2f}{s['net']-s['open_cost']:>9.2f}  {s['win']}/{s['loss']}/{s['flat']}")

# ---------- 7. 环境 / 动作分层 ----------
for name, keyf in (("卖出日个股环境", lambda r: r["env"]), ("卖出动作", lambda r: r["action"])):
    agg = collections.defaultdict(lambda: {"chains": 0, "qty": 0, "gross": 0, "net": 0, "open_cost": 0, "unclosed": 0})
    for r in rows:
        a = agg[keyf(r)]
        a["chains"] += 1; a["qty"] += r["qty"]; a["gross"] += r["gross"]; a["net"] += r["net"]; a["open_cost"] += r["open_cost"]
        if r["status"] != "闭环": a["unclosed"] += 1
    print(f"\n===== {name}分层 =====")
    for e, a in agg.items():
        print(f"  {e}: 链 {a['chains']} 股 {a['qty']} 费前 {a['gross']:+.2f} 闭环费后 {a['net']:+.2f} 悬空 {a['open_cost']:+.2f} 净经济 {a['net']-a['open_cost']:+.2f} 未闭环 {a['unclosed']}")

# ---------- 8. 回补价差分布 ----------
spreads = []
for ch in chains:
    for c in ch["closed"]:
        spreads.append({"code": ch["code"], "sell": ch["sell_price"], "buy": c["buy_price"], "qty": c["qty"],
                        "spread_pct": (ch["sell_price"] - c["buy_price"]) / ch["sell_price"] * 100,
                        "cross": c["date"] > ch["sell_date"]})
sp = sorted(s["spread_pct"] for s in spreads)
print("\n===== 回补价差分布（闭合段）=====")
if sp:
    n = len(sp)
    med = sp[n // 2] if n % 2 else (sp[n // 2 - 1] + sp[n // 2]) / 2
    wavg = sum(s["spread_pct"] * s["qty"] for s in spreads) / sum(s["qty"] for s in spreads)
    BE = (FEE_SELL + FEE_BUY) * 100
    print(f"n={n} 均值 {sum(sp)/n:+.3f}% 量加权 {wavg:+.3f}% 中位 {med:+.3f}% 区间 [{sp[0]:+.3f}%, {sp[-1]:+.3f}%]")
    print(f"价差>0 占 {sum(1 for x in sp if x > 0)}/{n}；> 盈亏平衡 {BE:.3f}% 占 {sum(1 for x in sp if x > BE)}/{n}")
    cross = [s for s in spreads if s["cross"]]
    print(f"跨日回补段 {len(cross)} 个，量加权价差 {sum(s['spread_pct']*s['qty'] for s in cross)/max(1,sum(s['qty'] for s in cross)):+.3f}%")

# ---------- 9. 闭环率 ----------
n_closed = sum(1 for r in rows if r["status"] == "闭环")
q_total = sum(r["qty"] for r in rows)
q_closed = sum(r["q_closed"] for r in rows)
print(f"\n[闭环率] 链级 {n_closed}/{len(rows)} = {n_closed/len(rows)*100:.0f}%；股数级 {q_closed}/{q_total} = {q_closed/q_total*100:.1f}%")
same_day = sum(1 for r in rows if r["status"] == "闭环" and not r["cross_day"])
print(f"[当日闭环] {same_day}/{n_closed} 条闭环链为当日闭环；未回补过夜（跨日/丢失/挂起/过期）{len(rows)-same_day} 条")

# ---------- 10. 汇总指标 ----------
active_days = sorted(dayB.keys())
netB = [dayB[d]["net"] for d in active_days]
econ_days = sorted(day_agg.keys())
econ = [day_agg[d]["net"] - day_agg[d]["open_cost"] for d in econ_days]
streak = mx = 0
for v in econ:
    streak = streak + 1 if v < 0 else 0
    mx = max(mx, streak)
print(f"\n[汇总] 做T活跃日 {len(active_days)} 天（auto 模拟盘 2026-08-31~09-14）")
print(f"  口径A 链闭环费后合计 {tot_net:+.2f} → 期望 {tot_net/len(active_days):+.2f}/活跃日")
print(f"  口径B 日净额费后合计 {totB['net']:+.2f} → 期望 {totB['net']/len(active_days):+.2f}/活跃日, 中位 {sorted(netB)[len(netB)//2]:+.2f}")
print(f"  含悬空机会成本净经济合计 {tot_net - tot_open:+.2f} → {((tot_net-tot_open)/len(econ_days)):+.2f}/活跃日")
print(f"  日胜率(口径B, net>0): {sum(1 for v in netB if v>0)}/{len(netB)}")
print(f"  最大连续亏损日(净经济口径): {mx} 天")
closed_chains = [r for r in rows if r["status"] == "闭环"]
if closed_chains:
    per = tot_net / len(closed_chains)
    print(f"  闭环链 {len(closed_chains)} 条, 单笔均费后 {per:+.2f}, 胜 {sum(1 for r in closed_chains if r['net']>0)} 负 {sum(1 for r in closed_chains if r['net']<0)} 平 {sum(1 for r in closed_chains if r['net']==0)}")

# ---------- 落盘 ----------
res = {
    "chains": rows, "totals": {"gross": tot_gross, "fees": tot_fees, "net_closed": tot_net,
                               "open_cost_pending": tot_open, "econ": tot_net - tot_open},
    "dayB": {d: dict(a) for d, a in dayB.items()}, "totB": totB,
    "day_agg": {d: dict(a) for d, a in day_agg.items()},
    "stock_agg": {c: dict(s) for c, s in stk.items()},
    "spreads": spreads,
    "ca_days": dict(ca_days), "ca_sum": ca_sum, "ca_corrupt_0724": ca_corrupt,
    "t0_realized_nonzero": t0_nonzero,
    "lost_chains": lost, "mismatch_fill_vs_audit": mism,
    "dirty": dict(dirty), "n_days": len(days), "fills_n": len(fills),
}
with open(os.path.join(OUT, "D1_results.json"), "w", encoding="utf-8") as f:
    json.dump(res, f, ensure_ascii=False, indent=2, default=str)
print(f"\n[saved] {os.path.join(OUT, 'D1_results.json')}")
