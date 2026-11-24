# -*- coding: utf-8 -*-
"""h_hypothesis_daily.py — 假说 H1~H5 每日细致对照采集（复盘专用，不写库）。

用法：python scripts/experiment/h_hypothesis_daily.py
每日复盘 A-7「假说对照」详版的数据源。覆盖：
  §0 情绪温度序列（复刻 gui.py 口径：60日分位→5日均→加权）+ H1/H2 触发日全历史扫描
  §1 H1 样本结算（触发日假想买入 → 次日 OHLC，样本清单按日复盘更新 H1 dict）
  §2 H2 已结算样本台账（fills/sells_close + K线 MFE/MAE）
  §3 H3 主线板块表现（机器人/物理AI/AI影视，watchlist 标签篮子 + qt 快照）
  §4 H5 风格篮子（科技进攻 vs 红利防守）
  §5 H4 活体样本 MA 距离（样本代码/入场价按实际持仓更新 ENTRY dict）
⚠️ H1/H4 样本清单是写死的当日值，次日使用前需按复盘更新；其余全自动。
"""
import json, os, sys, statistics
sys.path.insert(0, r"E:\superTrader")
os.environ.pop("HTTP_PROXY", None); os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None); os.environ.pop("https_proxy", None)
import urllib.request

BASE = r"E:\superTrader"
TODAY = "2026-11-24"

def J(fp):
    with open(fp, encoding="utf-8") as f: return json.load(f)

# ── 0. 情绪序列（复刻 gui.py 口径：60日滚动分位→5日均→加权）────────────
def sent_series():
    e5 = J(BASE + r"\t_io\index_regime\e5_ths_cache.json")
    loc = (J(BASE + r"\t_io\cache\sentiment_local.json").get("series") or {})
    merged = {}
    for d, v in loc.items():
        if int(v.get("n") or 0) < 2000: continue
        d = str(d); d = f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d[:10]
        merged[d] = {"date": d, "zt": int(v.get("zt") or 0), "dt": int(v.get("dt") or 0),
                     "touch": v.get("touch"), "zb_rate": v.get("zb_rate")}
    for k, v in e5.items():
        if v.get("zt_count") is None or v.get("dt_count") is None: continue
        k = str(k); d = f"{k[:4]}-{k[4:6]}-{k[6:]}"
        merged[d] = {"date": d, "zt": v["zt_count"], "dt": v["dt_count"],
                     "touch": v.get("zt_touched"), "zb_rate": v.get("zb_rate")}
    rows = [merged[d] for d in sorted(merged)]
    def roll_pct(vals, w=60, min_n=20):
        out = []
        for j, v in enumerate(vals):
            seg = [x for x in vals[max(0, j-w+1):j+1] if x is not None]
            out.append(round(sum(1 for x in seg if x <= v)/len(seg), 4)
                       if (v is not None and len(seg) >= min_n) else None)
        return out
    def ma5(vals):
        out = []
        for j in range(len(vals)):
            seg = [v for v in vals[max(0, j-4):j+1] if v is not None]
            out.append(round(sum(seg)/len(seg), 4) if len(seg) >= 3 else None)
        return out
    pt = roll_pct([r["touch"] for r in rows]); pz = roll_pct([r["zb_rate"] for r in rows]); pdt = roll_pct([r["dt"] for r in rows])
    ptm, pzm, pdm = ma5(pt), ma5(pz), ma5(pdt)
    for j, r in enumerate(rows):
        r["sent"] = round(100*(0.5*pt[j]+0.3*(1-pz[j])+0.2*(1-pdt[j])), 1) if None not in (pt[j], pz[j], pdt[j]) else None
        r["sent_ma"] = round(100*(0.5*ptm[j]+0.3*(1-pzm[j])+0.2*(1-pdm[j])), 1) if None not in (ptm[j], pzm[j], pdm[j]) else None
    sv = [r["sent_ma"] for r in rows]
    for j, r in enumerate(rows):
        r["slope5"] = round((sv[j]-sv[j-5])/5, 2) if j >= 5 and sv[j] is not None and sv[j-5] is not None else None
        v = r["sent_ma"]
        r["zone"] = None if v is None else ("弱势" if v < 40 else ("平衡" if v < 60 else "强势"))
    return rows

rows = sent_series()
print("=== 0. 情绪温度序列（近 15 个交易日）===")
print(f"{'日期':<12}{'sent_ma':>8}{'slope5':>8}{'zone':>6}{'zt':>5}{'dt':>4}{'touch':>6}{'zb%':>7}")
for r in rows[-15:]:
    zb = f"{r['zb_rate']*100:.1f}" if r.get("zb_rate") is not None else "—"
    print(f"{r['date']:<12}{r['sent_ma']!s:>8}{r['slope5']!s:>8}{r['zone']!s:>6}{r['zt']:>5}{r['dt']:>4}{r['touch']!s:>6}{zb:>7}")

# H1/H2 触发日全历史扫描（原口径 + 修正候选口径双轨，2026-11-24 核实修正）
h1_days, h2_days, h1b_days, h2b_days = [], [], [], []
# H1-b: ≤25 段内首个上升日；H2-b: ≥70 段峰后首个下跌日
def _segments(vals, cond):
    segs, s = [], None
    for j, v in enumerate(vals):
        if v is not None and cond(v):
            if s is None: s = j
        else:
            if s is not None: segs.append((s, j-1)); s = None
    if s is not None: segs.append((s, len(vals)-1))
    return segs
sv = [r["sent_ma"] for r in rows]
for a, b in _segments(sv, lambda v: v <= 25):
    for j in range(a+1, b+2):
        if j < len(sv) and sv[j] is not None and sv[j-1] is not None and sv[j] > sv[j-1]:
            h1b_days.append(rows[j]["date"]); break
for a, b in _segments(sv, lambda v: v >= 70):
    pk = max(range(a, b+1), key=lambda j: sv[j])
    if pk < b: h2b_days.append(rows[pk+1]["date"])
    elif b+1 < len(sv) and sv[b+1] is not None and sv[b+1] < sv[b]: h2b_days.append(rows[b+1]["date"])
for j in range(1, len(rows)):
    a, b = rows[j-1], rows[j]
    if a["sent_ma"] is None or b["sent_ma"] is None: continue
    # H1-a 原口径: 昨日 ≤20 且（单日 +5 以上 或 slope5 连续 2 日为正）
    if a["sent_ma"] <= 20 and (b["sent_ma"] - a["sent_ma"] >= 5 or
       (b.get("slope5") or 0) > 0 and (a.get("slope5") or 0) > 0):
        h1_days.append(b["date"])
    # H2-a 原口径: 昨日 ≥70 且 slope5 转负
    if a["sent_ma"] >= 70 and (b.get("slope5") or 0) < 0 and (a.get("slope5") or 0) >= 0:
        h2_days.append(b["date"])
print(f"\nH1-a 原口径触发: {len(h1_days)} 天 {h1_days[-6:]}")
print(f"H1-b 修正候选(≤25段内拐点日): {len(h1b_days)} 天 ≈{len(h1b_days)/3.33:.1f}/年 {h1b_days[-6:]}")
print(f"H2-a 原口径触发: {len(h2_days)} 天 {h2_days[-6:]}")
print(f"H2-b 修正候选(≥70段峰后首日跌): {len(h2b_days)} 天 ≈{len(h2b_days)/3.33:.1f}/年 {h2b_days[-6:]}")

# ── qt.gtimg.cn 批量快照（f[3]=现价 f[4]=昨收 f[5]=开 f[33]=高 f[34]=低）──
def qt_batch(codes):
    out = {}
    syms = []
    for c in codes:
        c = str(c).split("_")[0].split(".")[-1]
        syms.append(("sh" if c[0] in "56" else "sz") + c)
    for i in range(0, len(syms), 60):
        chunk = syms[i:i+60]
        url = "http://qt.gtimg.cn/q=" + ",".join(chunk)
        try:
            raw = urllib.request.urlopen(url, timeout=6).read().decode("gbk", "ignore")
        except Exception as e:
            print("  [qt batch fail]", e); continue
        for seg in raw.split(";"):
            if "=" not in seg or '"' not in seg: continue
            sym = seg.split("=")[0].strip().lstrip("v_")
            try:
                f = seg.split('"')[1].split("~")
                if len(f) < 35 or not f[3]: continue
                code = sym[2:]
                out[code] = {"price": float(f[3]), "prev": float(f[4] or 0),
                             "open": float(f[5] or 0), "high": float(f[33] or 0),
                             "low": float(f[34] or 0),
                             "pct": (float(f[3])/float(f[4])-1) if float(f[4] or 0) > 0 else None}
            except Exception:
                continue
    return out

# ── 1. H1 样本 #1 结算明细 ────────────────────────────────
print("\n=== 1. H1 样本 #1 结算（11-23 触发 → 11-23 开盘假想买入 → 11-24 结算）===")
H1 = {"300972": "万辰集团", "600800": "渤海化学", "300903": "科翔股份", "603217": "元利科技"}
qt = qt_batch(list(H1))
# 11-23 开盘价：读日线缓存
def cache_bar(code, date):
    fp = BASE + rf"\t_io\cache\daily_kline\{code}.json"
    if not os.path.exists(fp): return None
    for r in J(fp).get("rows") or []:
        if str(r.get("date")) == date: return r
    return None
pop_n, gains = 0, []
for code, name in H1.items():
    b23 = cache_bar(code, "2026-11-23")
    q = qt.get(code) or {}
    o23 = (b23 or {}).get("open")
    buy = o23
    hi, cl = q.get("high"), q.get("price")
    if buy and hi:
        gmax = hi/buy - 1; gclose = (cl/buy - 1) if cl else None
        pop = hi >= buy*1.03
        pop_n += pop; gains.append(gmax)
        print(f"  {code} {name}: 买(11-23开){buy:.2f} → 11-24 高{hi:.2f} 收{cl:.2f} | 最大涨幅 {gmax*100:+.1f}% | 收涨跌 {gclose*100:+.1f}% | pop+3% {'✅' if pop else '❌'}")
    else:
        print(f"  {code} {name}: 数据缺（11-23缓存={bool(b23)}, quote={bool(q)}）")
if gains:
    print(f"  小结: pop 达成 {pop_n}/4 | 平均最大涨幅 {statistics.mean(gains)*100:+.1f}% | 对照沪深300今日 +0.14%")

# ── 2. H2 已结算样本台账 ──────────────────────────────────
print("\n=== 2. H2 已结算样本（极热降温段入场 → 到期亏损）===")
# 入场 fill：fills_2026-11-12/13.json；出场：sells_close 11-19/20
def load_fills(d):
    fp = BASE + rf"\t_io\state\weak_strong_book\fills_{d}.json"
    return J(fp) if os.path.exists(fp) else None
H2C = ["600629", "688708", "300534"]
entries = {}
for d in ("2026-11-11", "2026-11-12", "2026-11-13"):
    f = load_fills(d)
    if not f: continue
    items = f if isinstance(f, list) else (f.get("fills") or f.get("records") or [])
    for e in items:
        c = str(e.get("code") or "")
        if c in H2C and str(e.get("side") or "BUY").upper() == "BUY":
            px = e.get("price") or e.get("fill_vwap") or e.get("vwap")
            if c not in H2C and px: pass
            if px: entries[c] = {"date": d, "price": float(px)}
print("  入场记录:", json.dumps(entries, ensure_ascii=False))
for c in H2C:
    fp = BASE + rf"\t_io\cache\daily_kline\{c}.json"
    en = entries.get(c)
    if not (os.path.exists(fp) and en): 
        print(f"  {c}: 缺缓存或入场记录"); continue
    bars = [r for r in J(fp).get("rows") or [] if str(r.get("date")) >= en["date"]]
    bars = bars[:7]
    if not bars: continue
    mfe = max(float(r["high"]) for r in bars)/en["price"] - 1
    mae = min(float(r["low"]) for r in bars)/en["price"] - 1
    last = bars[-1]
    ret = float(last["close"])/en["price"] - 1
    print(f"  {c}: 入场 {en['date']}@{en['price']:.2f} → 持有 {len(bars)} 根K线 | MFE {mfe*100:+.1f}% | MAE {mae*100:+.1f}% | 末日({last['date']})收盘损益 {ret*100:+.1f}% | 期内曾触+3%: {'是' if mfe>=0.03 else '否'}")

# ── 3/4. H3 主线板块 + H5 风格篮子 今日表现 ───────────────
print("\n=== 3. H3 上轮主线板块今日表现（机器人/AI影视/物理AI）===")
wl = J(BASE + r"\stock_hunter\watchlist_concept.json")
def bucket(pred):
    return {k: v.get("name") for k, v in wl.items()
            if isinstance(v, dict) and pred((v.get("category") or ""), (v.get("concept") or ""))}
b_robot = bucket(lambda c, s: "机器人" in c)
b_phys = bucket(lambda c, s: "物理AI" in c or "物理AI" in s)
b_aifilm = bucket(lambda c, s: "AI影视" in s or "AI影视" in c)
TECH = ("半导体", "算力", "AI应用", "机器人", "光通信", "液冷", "PCB", "超节点", "物理AI", "消费电子", "商业航天", "低空经济", "光刻")
DEF = ("电力", "煤炭")
b_tech = bucket(lambda c, s: any(t in c for t in TECH))
b_def = bucket(lambda c, s: any(t in c for t in DEF)
               or ("金融" in c and "银行" in s)
               or ("大消费" in c and ("家电" in s or "白酒" in s))
               or "石油" in c or "高速" in s)
def perf(codes, label):
    q = qt_batch(list(codes))
    pcts = [v["pct"]*100 for v in q.values() if v.get("pct") is not None]
    if not pcts: print(f"  {label}: 无数据"); return None
    up = sum(1 for p in pcts if p > 0)
    print(f"  {label}: n={len(pcts)} | 均 {statistics.mean(pcts):+.2f}% | 中位 {statistics.median(pcts):+.2f}% | 上涨占比 {up/len(pcts)*100:.0f}%")
    return {"n": len(pcts), "mean": statistics.mean(pcts), "median": statistics.median(pcts), "up": up/len(pcts)}
r_robot = perf(b_robot, f"机器人({len(b_robot)}只)")
r_phys = perf(b_phys, f"物理AI({len(b_phys)}只)")
r_film = perf(b_aifilm, f"AI影视({len(b_aifilm)}只)")
print("\n=== 4. H5 风格篮子今日表现 ===")
r_tech = perf(b_tech, f"科技进攻篮子({len(b_tech)}只)")
r_def = perf(b_def, f"红利防守篮子({len(b_def)}只)")
if r_tech and r_def:
    print(f"  对照: 科技-防守 均值差 {r_tech['mean']-r_def['mean']:+.2f}pp | 沪深300 +0.14% / 创业板 +3.45% / 科创50 +1.70%")

# ── 5. H4 活体样本 MA 距离 ────────────────────────────────
print("\n=== 5. H4 活体样本（300508/300354）压力位距离 ===")
def mas(code):
    fp = BASE + rf"\t_io\cache\daily_kline\{code}.json"
    rs = sorted(J(fp).get("rows") or [], key=lambda r: str(r.get("date")))
    closes = [float(r["close"]) for r in rs]
    out = {}
    for n in (5, 21, 34, 55):
        if len(closes) >= n: out[n] = round(sum(closes[-n:])/n, 2)
    return out, closes[-1], rs[-1]["date"]
ENTRY = {"300508": 43.7824, "300354": 28.4233}
for c in ("300508", "300354"):
    m, close, d = mas(c)
    above = {n: v for n, v in m.items() if v > close}
    nearest = min(above.items(), key=lambda kv: kv[1]) if above else None
    press = f"MA{nearest[0]}={nearest[1]}(距离{(nearest[1]/close-1)*100:+.1f}%)" if nearest else "上方无均线压力(全踩在脚下)"
    atk5 = "✅站上" if close > m.get(5, 1e18) else "❌未站上"
    print(f"  {c}: 收{close}({d}) | MA5={m.get(5)}({atk5}MA5) MA21={m.get(21)} MA34={m.get(34)} MA55={m.get(55)}")
    print(f"       最近上方压力: {press} | OSC pop线={ENTRY[c]*1.03:.2f} | H4抛压 vs OSC pop: {'H4更高(利润更厚)' if nearest and nearest[1] > ENTRY[c]*1.03 else 'pop先到'}")
print("\n完成。")
