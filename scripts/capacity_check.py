# -*- coding: utf-8 -*-
"""capacity_check.py — OGR 策略容量/排队核算（阻塞三件之二，owner 2026-09-29 排期）。

问题：池内每只股票，09:31 按市价买 10 万元（单腿名义 ≤10 万、参与率 ≤ 竞价成交额 10%），
在真实集合竞价/开盘排队下能不能成交、多久成交、冲击多大。

口径与假设（如实声明）：
  A. 真实竞价额：t_io/preopen/auction_YYYY-MM-DD.json 的 09:25 快照 auction_amount_approx
     （09:31 后 backfill 回填，单位：元）。覆盖不全（仅 14/20 票、1~15 天）。
  B. GM 首分钟 bar 代理：掘金 60s 历史，每日 eob=09:31 的首根 bar 成交额
     = 集合竞价撮合额 + 09:30~09:31 连续竞价额，是竞价额的【上限】（偏乐观）。
     用口径 A/B 同票同日的中位比值 r 作 haircut：proxy_auction = first_bar_amount × r。
  C. 首 5 分钟成交额：GM 60s 09:31~09:35 五根 bar 的 amount 求和。
  D. 预计成交耗时 t_fill ≈ 100000 / (首5分钟额中位数 / 300s)（秒，线性假设，上限参考）。
判定三档（按竞价额口径，10 万单腿）：
  ✅ 宽裕：占比 < 5%；⚠️ 临界：5%~10%；❌ 不可得：>10% 或竞价额中位数 < 100 万。
统计窗口：近 20 个交易日（截至数据最新交易日，默认 2026-09-29）。

用法：
  python scripts/capacity_check.py            # 自动补拉 GM 数据（缺缓存时）+ 核算 + 打印表格
  python scripts/capacity_check.py --gm-fetch # 仅 GM 拉数（需 gm 环境，主流程会自动调度 Py311）

幂等：GM 分钟数据落缓存 t_io/cache/capacity_check/gm60s_{code}.csv，命中即不重拉。
只读现有数据，不改任何策略代码/参数。
"""
import csv
import glob
import json
import os
import statistics
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREOPEN_DIR = os.path.join(BASE, "t_io", "preopen")
CACHE_DIR = os.path.join(BASE, "t_io", "cache", "capacity_check")
RESULT_JSON = os.path.join(CACHE_DIR, "result_latest.json")
PY311 = r"C:\Users\Lenovo\AppData\Local\Programs\Python\Python311\python.exe"

LEG_NOTIONAL = 100_000          # 单腿名义 10 万
PARTICIPATION_CAP = 0.10        # 参与率上限 10%
AUCTION_MIN_YUAN = 1_000_000    # 竞价额 <100 万判不可得
WINDOW_DAYS = 20

# 近 20 个交易日窗口（截至 2026-09-29，来自 t_io/cache/daily_kline 交易日序列）
WINDOW_START, WINDOW_END = "2026-09-01", "2026-09-29"


def load_pool():
    """holdings_auto.json + watchlist_buy.json 中 pool ∈ {auto, both} 的票。"""
    pool = {}
    for fp, key in ((os.path.join(BASE, "t_io", "state", "holdings_auto.json"), None),
                    (os.path.join(BASE, "t_io", "state", "watchlist_buy.json"), "stocks")):
        with open(fp, encoding="utf-8") as f:
            d = json.load(f)
        stocks = d.get(key, {}) if key else d
        for code, v in stocks.items():
            if not str(code).isdigit() or len(str(code)) != 6:
                continue
            if v.get("pool") in ("auto", "both"):
                rec = pool.setdefault(str(code), {"name": v.get("name", ""), "pool": v.get("pool")})
                if v.get("pool") == "both":
                    rec["pool"] = "both"
    for code, rec in pool.items():
        rec["exchange"] = "SHSE" if code[0] in "56" else "SZSE"
        rec["gm_symbol"] = f"{rec['exchange']}.{code}"
    return dict(sorted(pool.items()))


# ---------------- GM 拉数（需要 gm SDK，主流程自动调度 Py311） ----------------

def gm_fetch(pool, start=WINDOW_START, end=WINDOW_END):
    """逐票拉 60s 历史落缓存 CSV（date,eob,amount）。供 --gm-fetch 子命令或 gm 环境内调用。"""
    sys.path.insert(0, BASE)
    from core.market_data.gm_token import load_token  # 动态发现掘金终端 token
    from gm.api import history, set_token, ADJUST_NONE
    tok = load_token()
    if not tok:
        raise RuntimeError("GM token 未发现（掘金终端未在跑？）")
    set_token(tok)
    os.makedirs(CACHE_DIR, exist_ok=True)
    for code, rec in pool.items():
        fp = os.path.join(CACHE_DIR, f"gm60s_{code}.csv")
        df = history(symbol=rec["gm_symbol"], frequency="60s",
                     start_time=f"{start} 09:00:00", end_time=f"{end} 15:30:00",
                     fields="symbol,eob,amount", adjust=ADJUST_NONE, df=True)
        if df is None or len(df) == 0:
            print(f"[gm-fetch] {code} EMPTY", flush=True)
            continue
        with open(fp, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "eob", "amount"])
            for r in df.itertuples():
                ts = str(r.eob)[:19]
                w.writerow([ts[:10], ts[11:19], float(r.amount)])
        print(f"[gm-fetch] {code} rows={len(df)}", flush=True)


def ensure_gm_cache(pool):
    """缓存缺失/不完整时自动调度 Py311 补拉（幂等）。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    missing = []
    for code in pool:
        fp = os.path.join(CACHE_DIR, f"gm60s_{code}.csv")
        ok = False
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            dates = {r["date"] for r in rows if WINDOW_START <= r["date"] <= WINDOW_END}
            ok = len(dates) >= 5  # 窗口内至少 5 个交易日视为可用
        if not ok:
            missing.append(code)
    if not missing:
        return
    sub = {c: pool[c] for c in missing}
    try:
        import gm  # noqa: F401 —— 当前解释器自带 gm 则直接拉
        gm_fetch(sub)
        return
    except ImportError:
        pass
    if os.path.exists(PY311):
        print(f"[capacity] 调度 Py311 补拉 GM 分钟数据: {missing}", flush=True)
        subprocess.run([PY311, os.path.abspath(__file__), "--gm-fetch",
                        WINDOW_START, WINDOW_END], check=True, cwd=BASE)
    else:
        print(f"[capacity][WARN] Py311 不存在且本解释器无 gm，{len(missing)} 票分钟数据缺", flush=True)


# ---------------- 数据装载 ----------------

def load_gm_minute_stats(code):
    """读缓存 CSV → {date: {'first_bar': 元, 'first5': 元}}。首 bar = eob 09:31。"""
    fp = os.path.join(CACHE_DIR, f"gm60s_{code}.csv")
    out = {}
    if not os.path.exists(fp):
        return out
    with open(fp, encoding="utf-8") as f:
        by_date = {}
        for r in csv.DictReader(f):
            if not (WINDOW_START <= r["date"] <= WINDOW_END):
                continue
            by_date.setdefault(r["date"], []).append((r["eob"], float(r["amount"])))
    for date, bars in by_date.items():
        bars.sort()
        first5 = [a for t, a in bars if "09:31" <= t <= "09:35"]
        if first5:
            out[date] = {"first_bar": first5[0], "first5": sum(first5)}
    return out


def load_auction_truth(pool):
    """本地竞价采集 09:25 快照 → {code: {date: auction_amount_approx(元)}}。"""
    truth = {c: {} for c in pool}
    for fp in sorted(glob.glob(os.path.join(PREOPEN_DIR, "auction_2026-*.json"))):
        date = os.path.basename(fp)[8:18]
        if not (WINDOW_START <= date <= WINDOW_END):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        snap = (d.get("snapshots") or {}).get("09:25") or {}
        for code, row in (snap.get("rows") or {}).items():
            amt = row.get("auction_amount_approx")
            if code in truth and amt:
                truth[code][date] = float(amt)
    return truth


# ---------------- 核算 ----------------

def verdict(ratio, auction_amt):
    if auction_amt is None:
        return "缺"
    if ratio > PARTICIPATION_CAP or auction_amt < AUCTION_MIN_YUAN:
        return "❌"
    return "⚠️" if ratio >= 0.05 else "✅"


def run():
    pool = load_pool()
    print(f"[capacity] 池内 {len(pool)} 票，窗口 {WINDOW_START}~{WINDOW_END}", flush=True)
    ensure_gm_cache(pool)
    truth = load_auction_truth(pool)

    # A/B 口径中位比值 r（haircut）：同票同日 真实竞价额 / GM首bar额
    ratios = []
    for code, days in truth.items():
        gm_stats = load_gm_minute_stats(code)
        for date, amt in days.items():
            fb = gm_stats.get(date, {}).get("first_bar")
            if fb and amt and fb > 0 and amt <= fb:
                ratios.append(amt / fb)
    haircut = statistics.median(ratios) if len(ratios) >= 10 else None
    print(f"[capacity] A/B haircut r = {haircut:.3f} (n={len(ratios)})" if haircut
          else f"[capacity][WARN] A/B 重叠样本不足(n={len(ratios)})，代理口径不做 haircut", flush=True)

    table = []
    for code, rec in pool.items():
        gm_stats = load_gm_minute_stats(code)
        tr = truth.get(code, {})
        dates = sorted(set(gm_stats) | set(tr))
        # 竞价额：优先真实口径 A；缺日用口径 B（GM首bar × haircut）
        auc_series, n_a, n_b = [], 0, 0
        for date in dates:
            if date in tr:
                auc_series.append(tr[date]); n_a += 1
            elif date in gm_stats:
                est = gm_stats[date]["first_bar"] * (haircut or 1.0)
                auc_series.append(est); n_b += 1
        f5_series = [gm_stats[d]["first5"] for d in dates if d in gm_stats]
        auc_med = statistics.median(auc_series) if auc_series else None
        auc_min = min(auc_series) if auc_series else None
        f5_med = statistics.median(f5_series) if f5_series else None
        ratio_med = LEG_NOTIONAL / auc_med if auc_med else None
        ratio_worst = LEG_NOTIONAL / auc_min if auc_min else None
        ratio_f5 = LEG_NOTIONAL / f5_med if f5_med else None
        t_fill = LEG_NOTIONAL / (f5_med / 300.0) if f5_med else None  # 秒
        table.append({
            "code": code, "name": rec["name"], "exchange": rec["exchange"], "pool": rec["pool"],
            "n_days": len(auc_series), "n_truth": n_a, "n_proxy": n_b,
            "auction_med": auc_med, "auction_min": auc_min,
            "ratio_med": ratio_med, "ratio_worst": ratio_worst,
            "first5_med": f5_med, "ratio_f5": ratio_f5,
            "t_fill_s": t_fill,
            "verdict": verdict(ratio_med, auc_med),
            "verdict_worst": verdict(ratio_worst, auc_min),
        })

    # 输出
    hdr = (f"{'代码':<7}{'名称':<10}{'池':<5}{'天数':>3}({'真':>2}/{'代':>2})"
           f"{'竞价额中位(万)':>12}{'竞价额最低(万)':>12}{'10万占比':>9}{'最差占比':>9}"
           f"{'首5分额(万)':>11}{'占首5分':>8}{'预计成交(s)':>10}  判定/最差")
    print(hdr)
    for r in table:
        def w(x): return "缺" if x is None else f"{x/1e4:,.1f}"
        def p(x): return "缺" if x is None else f"{x*100:.1f}%"
        def t(x): return "缺" if x is None else f"{x:.0f}"
        print(f"{r['code']:<7}{r['name']:<10}{r['pool']:<5}{r['n_days']:>3}"
              f"({r['n_truth']:>2}/{r['n_proxy']:>2}){w(r['auction_med']):>12}{w(r['auction_min']):>12}"
              f"{p(r['ratio_med']):>9}{p(r['ratio_worst']):>9}{w(r['first5_med']):>11}"
              f"{p(r['ratio_f5']):>8}{t(r['t_fill_s']):>10}  {r['verdict']}/{r['verdict_worst']}")
    ok = sum(1 for r in table if r["verdict"] == "✅")
    warn = sum(1 for r in table if r["verdict"] == "⚠️")
    bad = sum(1 for r in table if r["verdict"] == "❌")
    miss = sum(1 for r in table if r["verdict"] == "缺")
    print(f"\n[结论] OGR 单腿 10 万在池内 {ok}/{len(table)} 票宽裕可得"
          f"（⚠️临界 {warn}，❌不可得 {bad}，数据缺 {miss}）")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump({"window": [WINDOW_START, WINDOW_END], "haircut": haircut,
                   "haircut_n": len(ratios), "rows": table}, f, ensure_ascii=False, indent=2)
    print(f"[capacity] 结果落档 {RESULT_JSON}")


if __name__ == "__main__":
    if "--gm-fetch" in sys.argv:
        start = sys.argv[2] if len(sys.argv) > 2 else WINDOW_START
        end = sys.argv[3] if len(sys.argv) > 3 else WINDOW_END
        gm_fetch(load_pool(), start, end)
    else:
        run()
