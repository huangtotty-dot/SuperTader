# -*- coding: utf-8 -*-
"""「箱体突破」第二轮（2026-10-23 续）：**追突破 vs 回踩不破 vs 站稳再买**，带同日全市场基线。

## 为什么重跑（第一轮的口径缺陷）

第一轮判了「负 alpha」，但它落盘的 `t_io/cache/box_breakout_study.parquet` 只有
**833 只 × 20 个交易日**（2026-09-09~10-14），不是文档写的「1000 只 × 250 交易日」。
根因在生产代码里：`t_gui.Api._detect_boxes` 有一道 `days_since_end > 45: continue`
（只保留「结束距今 ≤45 天」的箱体），用的是 **`datetime.now()` 而不是 as-of 日期** ⇒
回测任何历史日时，只有「距今天 45 天内结束」的箱体可见 —— 长窗口回测被它截成最后约 20 个交易日。
本轮用 **as-of 版判箱**（其余逐字符沿用生产源码）重跑长窗口。

同时测**两套箱体口径**（产品里是两套，别混）：

* `rich`  = `t_gui.Api._detect_boxes`（选股猎手「🚀 突破箱体」卡片、K 线图箱体、`_breakout_probe_one`）
* `simple`= `core.position_builder._detect_boxes_simple`（**建仓信号扫描 c2_box_breakout**，即"第一优先级"那条）

## 一次扫描、离线多规则

扫描阶段把**突破事件之后 6 天的路径**（低点/收盘相对上沿）和**每个候选进场日的前向收益**全部落盘，
于是「回踩容忍度 / 深破上限 / 站稳几天 / 持有期」这些参数都在**分析阶段**扫，不用重扫数据。

* 突破日 t：`昨收 ≤ 上沿(as-of t-1) < 收(t)`；幅度带**不在扫描阶段过滤**，留给分析。
* 候选进场日：t+d+1 的**开盘**（d=0 即追突破；回踩在 t+d 收盘后确认，次日开盘进场）。
* 收益：`close(entry+h) / open(entry) - 1`，h ∈ {1,3,5,10}。
* 超额：减**同一天全市场（本样本这些股票）同口径均值**；t 值按**进场日聚类**。
* 同票同口径 10 个交易日内只取第一个事件（去自相关）。

## 用法

    python scripts/experiment/box_breakout_round2_20261023.py scan --stocks 2000 --days 700 --procs 3
    python scripts/experiment/box_breakout_round2_20261023.py analyze [--detector rich|simple]

    → 数据表写 `doc/research/2026-10-23_箱体突破_第二轮_数据表.md`；判词见同目录
      `2026-10-23_箱体突破_第二轮_回踩与站稳.md`（人写的结论，本脚本不覆盖）。
"""
import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE))
DAILY = BASE / "t_io" / "cache" / "daily_kline"
EV_PARQUET = BASE / "t_io" / "cache" / "box_v2_events.parquet"
BASE_PARQUET = BASE / "t_io" / "cache" / "box_v2_baseline.parquet"
OUT_MD = BASE / "doc" / "research" / "2026-10-23_箱体突破_第二轮_数据表.md"

HOLDS = (1, 3, 5, 10)
K_PATH = 6          # 突破后观察 t+1..t+6
K_DEDUPE = 10       # 同票同口径 10 日内只留第一个事件
RICH_MAX_PCT, RICH_MIN_PCT = 8.0, 0.3
SIMPLE_MAX_PCT, SIMPLE_MIN_PCT, SIMPLE_RETEST_MAX = 8.0, 0.3, 2.0

_DET_NS = None


def _detect_boxes_asof(daily, asof):
    """**as-of 版** `t_gui.Api._detect_boxes`。

    ⚠️ 生产版里的 `if b["days_since_end"] > 45: continue` 用的是 `datetime.now()` —— 回测历史日时
    所有 45 天前结束的箱体都被丢掉。这里把 `now()` 换成 as-of 日期，其余逻辑**逐字符沿用生产源码**
    （`inspect.getsource` 抽取 + 只替换那一行），生产改了这里会自动跟上。
    """
    global _DET_NS
    if _DET_NS is None:
        import inspect
        import textwrap

        import pandas as pd
        import t_gui
        src = textwrap.dedent(inspect.getsource(t_gui.Api._detect_boxes))
        if 'now_dt = _dt.datetime.strptime(str(last_date)[:10], "%Y-%m-%d")' in src:
            # 生产已改为 as-of（2026-10-23 修复）→ 原函数即正确，无需补丁
            _DET_NS = {"fn": t_gui.Api._detect_boxes, "patched": False}
        else:
            key = "now_dt = _dt.datetime.now()"
            assert key in src, "生产 _detect_boxes 已变，as-of 补丁需同步"
            src = src.replace(key, "now_dt = _dt.datetime.fromisoformat(_ASOF)")
            ns = {"pd": pd, "_ASOF": str(asof)}
            exec(src, ns)                               # noqa: S102 — 同仓源码，非外部输入
            ns["patched"] = True
            _DET_NS = ns
    if not _DET_NS["patched"]:
        return _DET_NS["fn"](None, daily)
    _DET_NS["_ASOF"] = str(asof)
    return _DET_NS["_detect_boxes"](None, daily)


def _simple_boxes(df):
    """`core.position_builder._detect_boxes_simple` 的**向量化等价实现**（同一判定，快 ~10×）。

    生产版逐窗 `np.polyfit`（41 窗）是 14ms/次，全池回测跑不动；线性拟合斜率 =
    `Σ(x-x̄)(y-ȳ) / Σ(x-x̄)²`，x 已中心化 ⇒ 与 `np.polyfit(...,1)[0]` 数学等价。
    等价性由 `_verify_simple()` 抽样比对生产实现。
    """
    import pandas as pd  # noqa: F401  (调用方传入 DataFrame)
    from numpy.lib.stride_tricks import sliding_window_view
    if df is None or df.empty or len(df) < 30:
        return []
    recent = df.tail(150).reset_index(drop=True)
    closes = recent["close"].to_numpy(float)
    highs = recent["high"].to_numpy(float)
    lows = recent["low"].to_numpy(float)
    n = len(recent)
    last_close = float(closes[-1])
    WIN = 30
    wh = sliding_window_view(highs, WIN)
    wl = sliding_window_view(lows, WIN)
    wc = sliding_window_view(closes, WIN)
    xc = np.arange(WIN) - (WIN - 1) / 2.0
    den = float(np.sum(xc * xc))
    rel_slope = np.abs((wc @ xc) / den) / np.where(wc.mean(axis=1) == 0, 1e-9, wc.mean(axis=1))
    ups = np.percentile(wh, 88, axis=1)
    dns = np.percentile(wl, 12, axis=1)
    up_touch = np.sum(wh >= (ups * 0.992)[:, None], axis=1)
    dn_touch = np.sum(wl <= (dns * 1.008)[:, None], axis=1)
    w = (ups - dns) / np.where(wc.mean(axis=1) == 0, 1e-9, wc.mean(axis=1)) * 100
    ok = (rel_slope < 0.005) & (w >= 3.0) & (w <= 22.0) & (up_touch >= 2) & (dn_touch >= 2)

    box_flags = np.zeros(n, dtype=bool)
    for s in range(0, n - WIN + 1, 3):
        if ok[s]:
            box_flags[s:s + WIN] = True

    boxes = {}
    i = 0
    while i < n:
        if not box_flags[i]:
            i += 1
            continue
        j = i
        while j < n and box_flags[j]:
            j += 1
        if j - i >= 20:
            up = float(np.percentile(highs[i:j], 88))
            dn = float(np.percentile(lows[i:j], 12))
            up_t = int(np.sum(highs[i:j] >= up * 0.992))
            dn_t = int(np.sum(lows[i:j] <= dn * 1.008))
            ww = (up - dn) / (closes[i:j].mean() or 1e-9) * 100
            if 3.0 <= ww <= 22.0 and up_t >= 2 and dn_t >= 2:
                boxes[(round(up, 3), round(dn, 3))] = {"low": round(dn, 3), "high": round(up, 3)}
        i = j

    merged = []
    for b in boxes.values():
        hit = next((m for m in merged if
                    min(b["high"], m["high"]) - max(b["low"], m["low"])
                    > min(b["high"] - b["low"], m["high"] - m["low"]) * 0.5), None)
        if hit:
            hit["low"] = min(hit["low"], b["low"])
            hit["high"] = max(hit["high"], b["high"])
        else:
            merged.append(dict(b))

    for b in merged:
        if b["low"] <= last_close <= b["high"]:
            b["rel"] = 0
        elif last_close > b["high"]:
            b["rel"] = -1
        else:
            b["rel"] = -2
    merged.sort(key=lambda b: (0 if b["rel"] == 0 else 1, -b["high"]))
    return merged[:3]


def _verify_simple(n_pairs=15, seed=0):
    """抽样比对 _simple_boxes 与生产 _detect_boxes_simple（只在 --verify-simple 时跑）。"""
    import pandas as pd
    from core.position_builder import _detect_boxes_simple
    codes = sorted(p.stem for p in DAILY.glob("*.json") if p.stem.isdigit())[:400]
    rng = np.random.default_rng(seed)
    bad = 0
    for code in rng.choice(codes, n_pairs, replace=False):
        blob = json.loads((DAILY / f"{code}.json").read_text(encoding="utf-8"))
        df = pd.DataFrame(blob.get("rows") or [])
        if df.empty or len(df) < 80:
            continue
        df["date"] = df["date"].astype(str).str[:10]
        for t in rng.integers(60, len(df) - 20, 3):
            a = _simple_boxes(df.iloc[:t])
            b = _detect_boxes_simple(df.iloc[:t])
            ka = [(x["rel"], x["low"], x["high"]) for x in a]
            kb = [(x["rel"], x["low"], x["high"]) for x in b]
            if ka != kb:
                bad += 1
                print(f"  DIFF {code}@{t}: fast={ka} prod={kb}")
    print(f"等价性抽样：{n_pairs * 3} 组，不一致 {bad}")
    return bad == 0


def _scan_one(args):
    code, days, cooldown = args
    import pandas as pd

    try:
        blob = json.loads((DAILY / f"{code}.json").read_text(encoding="utf-8"))
    except Exception:
        return [], {}, 1
    rows = blob.get("rows") or []
    forming = set(str(x)[:10] for x in (blob.get("forming") or []))
    if forming:                     # 盘中快照根绝不能当收盘用（2026-10-21 daily_partial_bar_poison）
        rows = [r for r in rows if str(r["date"])[:10] not in forming]
    if len(rows) < 80:
        return [], {}, 0

    df = pd.DataFrame(rows)
    df["date"] = df["date"].astype(str).str[:10]
    n = len(df)
    dates = df["date"].tolist()
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    ma20 = pd.Series(c).rolling(20).mean().to_numpy()
    ma60 = pd.Series(c).rolling(60).mean().to_numpy()
    vol20 = pd.Series(v).rolling(20).mean().to_numpy()

    # ---- 基线：全市场、每个进场日的前向收益均值 ----
    base = {}
    for i in range(max(20, n - int(days) - 17), n - 11):
        e = o[i + 1]
        if not (e > 0) or not np.isfinite(c[i + 1 + max(HOLDS)]):
            continue
        key = dates[i + 1]
        acc = base.get(key)
        if acc is None:
            acc = base[key] = [0.0] * len(HOLDS) + [0]
        for j, hh in enumerate(HOLDS):
            acc[j] += c[i + 1 + hh] / e - 1.0
        acc[len(HOLDS)] += 1

    last_t = n - 17                     # 需要 t+K_PATH+1+max(HOLDS) < n
    first_t = max(35, n - int(days) - 17)
    evs = []
    last_r = last_s = -10 ** 9
    for t in range(first_t, last_t):
        want_r = (t - last_r) >= cooldown
        want_s = (t - last_s) >= cooldown
        if not (want_r or want_s):
            continue
        prev_close, cur = c[t - 1], c[t]

        rec = None
        if want_r:
            try:
                boxes = _detect_boxes_asof(df.iloc[:t], dates[t])
            except Exception:
                boxes = []
            box = next((b for b in boxes if b.get("rel") == 0 and b.get("high")), None)
            if box:
                top = float(box["high"])
                if top > 0 and prev_close <= top < cur:
                    last_r = t
                    bot = float(box.get("low") or 0.0)
                    rec = rec or {"code": code, "date": dates[t], "chg": None}
                    rec.update({"sig_r": 1, "top_r": round(top, 3),
                                "pct_r": round((cur - top) / top * 100.0, 3),
                                "width_r": round((top - bot) / bot * 100.0, 2) if bot > 0 else None,
                                "conf_r": box.get("conf"), "top_r0": round(top, 3)})
                    for d in range(1, K_PATH + 1):
                        rec[f"low{d}_r"] = round((l[t + d] / top - 1.0) * 100.0, 3)
                        rec[f"cl{d}_r"] = round((c[t + d] / top - 1.0) * 100.0, 3)

        if want_s:
            sb = _simple_boxes(df.iloc[:t])
            b0 = next((b for b in sb if b.get("rel") == 0), None)
            top_s = None
            if b0 and cur > b0["high"]:
                pct = (cur - b0["high"]) / b0["high"] * 100.0
                if SIMPLE_MIN_PCT <= pct <= SIMPLE_MAX_PCT:
                    top_s, kind_s = float(b0["high"]), "rel0"
            if top_s is None:
                b1 = [b for b in sb if b.get("rel") == -1]
                if b1:
                    tb = max(b1, key=lambda b: b["high"])
                    pct = (cur - tb["high"]) / tb["high"] * 100.0
                    if SIMPLE_MIN_PCT <= pct <= SIMPLE_RETEST_MAX:
                        top_s, kind_s = float(tb["high"]), "rel1"
            if top_s is not None:
                last_s = t
                rec = rec or {"code": code, "date": dates[t], "chg": None}
                rec.update({"sig_s": 1, "top_s": round(top_s, 3), "kind_s": kind_s,
                            "pct_s": round((cur - top_s) / top_s * 100.0, 3)})
                for d in range(1, K_PATH + 1):
                    rec[f"low{d}_s"] = round((l[t + d] / top_s - 1.0) * 100.0, 3)
                    rec[f"cl{d}_s"] = round((c[t + d] / top_s - 1.0) * 100.0, 3)

        if rec is None:
            continue
        rec["chg"] = round((cur / prev_close - 1.0) * 100.0, 3) if prev_close else None
        rec["vr"] = round(float(v[t] / vol20[t]), 3) if (vol20[t] and not np.isnan(vol20[t])) else None
        rec["ushadow"] = round((h[t] - max(o[t], cur)) / max(cur, 1e-9) * 100.0, 3)
        rec["above_ma20"] = bool(ma20[t] == ma20[t] and cur > ma20[t])
        rec["above_ma60"] = bool(ma60[t] == ma60[t] and cur > ma60[t])
        for d in range(0, K_PATH + 1):
            e = t + d + 1
            if e >= n or not (o[e] > 0):
                rec[f"ed{d}"] = None
                for hh in HOLDS:
                    rec[f"r{d}_{hh}"] = None
                continue
            rec[f"ed{d}"] = dates[e]
            for hh in HOLDS:
                rec[f"r{d}_{hh}"] = (c[e + hh] / o[e] - 1.0) if e + hh < n else None
        evs.append(rec)
    return evs, base, 0


def _worker(args):
    code, days, cooldown = args
    try:
        return _scan_one((code, days, cooldown))
    except Exception as e:
        print(f"[WARN] {code}: {type(e).__name__}: {str(e)[:90]}", flush=True)
        return [], {}, 1


def cmd_scan(a):
    import pandas as pd
    from concurrent.futures import ProcessPoolExecutor

    codes = sorted(p.stem for p in DAILY.glob("*.json")
                   if p.stem.isdigit() and len(p.stem) == 6)
    if a.stocks and len(codes) > a.stocks:
        step = len(codes) / a.stocks
        codes = [codes[int(i * step)] for i in range(a.stocks)]
    print(f"扫描 {len(codes)} 只 × 最近 {a.days} 交易日 · procs={a.procs} · 去重窗 {K_DEDUPE}", flush=True)

    evs, base_acc, n_err = [], {}, 0
    jobs = [(c, a.days, K_DEDUPE) for c in codes]
    with ProcessPoolExecutor(max_workers=a.procs) as ex:
        for i, (part, b, err) in enumerate(ex.map(_worker, jobs, chunksize=4)):
            evs.extend(part)
            n_err += err
            for k, v in b.items():
                acc = base_acc.get(k)
                if acc is None:
                    base_acc[k] = list(v)
                else:
                    for j in range(len(v)):
                        acc[j] += v[j]
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(codes)} 只 · 事件 {len(evs)}", flush=True)

    edf = pd.DataFrame(evs)
    edf.to_parquet(EV_PARQUET, index=False)
    bdf = pd.DataFrame(
        [{"date": k, **{f"m{hh}": v[j] / v[-1] for j, hh in enumerate(HOLDS)}, "n": v[-1]}
         for k, v in base_acc.items() if v[-1] > 0])
    bdf.to_parquet(BASE_PARQUET, index=False)
    print(f"\n事件 {len(edf)} · 基线条 {len(bdf)} · 失败股 {n_err}")
    print(f"写入 {EV_PARQUET.name} / {BASE_PARQUET.name}")


# ============================ 分析 ============================

def _analyze_detector(det, ev, hmap, a):
    """det ∈ {rich, simple}。返回 (rows1, rows2, rows3, rows4) 与打印。"""
    import pandas as pd

    sfx = "r" if det == "rich" else "s"
    col = f"sig_{sfx}"
    lo_cols = [f"low{d}_{sfx}" for d in range(1, K_PATH + 1)]
    cl_cols = [f"cl{d}_{sfx}" for d in range(1, K_PATH + 1)]
    ev = ev[ev.get(col, 0) == 1].copy()
    if ev.empty:
        print(f"\n### {det}: 无事件")
        return [], [], [], [], ev
    lo = ev[lo_cols].to_numpy(float)
    cl = ev[cl_cols].to_numpy(float)

    def path_first(rule, tol, deep, hold):
        out = np.full(len(ev), -1)
        for k in range(K_PATH):
            und = out < 0
            if not und.any():
                break
            if rule == "pullback":
                ok = (lo[:, k] <= tol) & (lo[:, k] >= -deep) & (cl[:, k] >= -hold)
            elif rule == "stay":
                ok = cl[:, k] >= hold
            else:
                ok = np.ones(len(ev), dtype=bool)      # none
            out[und & np.isfinite(lo[:, k]) & ok] = k + 1
        return out

    def returns_for(dsel, hh):
        ret = np.full(len(ev), np.nan)
        ex = np.full(len(ev), np.nan)
        edn = np.array([None] * len(ev), dtype=object)
        for d in range(0, K_PATH + 1):
            idx = np.flatnonzero(dsel == d)
            if not len(idx):
                continue
            rr = ev[f"r{d}_{hh}"].to_numpy(float)[idx]
            dd = ev[f"ed{d}"].to_numpy(object)[idx]
            ret[idx] = rr
            edn[idx] = dd
            ex[idx] = np.array([(x - hmap[hh].get(dt, np.nan)) if (x == x and dt) else np.nan
                                for x, dt in zip(rr, dd)], dtype=float)
        return ret, ex, edn

    def stat(name, ret, ex, edn):
        m = np.isfinite(ex)
        if m.sum() < 5:
            return {"规则": name, "n": int(m.sum()), "覆盖": 0.0}
        r, e = pd.Series(ret[m]), pd.Series(ex[m])
        g = pd.Series(e.to_numpy()).groupby(np.array(edn, dtype=object)[m]).mean().dropna()
        sd = g.std(ddof=1)
        t_cl = g.mean() / (sd / np.sqrt(len(g))) if len(g) > 4 and sd else np.nan
        return {"规则": name, "n": int(m.sum()), "覆盖": m.sum() / len(ev) * 100,
                "绝对%": r.mean() * 100, "超额%": e.mean() * 100,
                "胜率%": (r > 0).mean() * 100, "t": t_cl}

    def fmt_t(s):
        t = s.get("t")
        return "—" if t is None or t != t else f"{t:+.2f}"

    pb = path_first("pullback", a.tol * 100, a.deep * 100, a.hold * 100)
    st = path_first("stay", 0, 0, a.hold * 100)

    print(f"\n### {det}：事件 {len(ev)} · {ev['date'].min()} ~ {ev['date'].max()} · {ev['code'].nunique()} 只")
    rows1 = []
    print("| 规则 | 持有 | n | 覆盖 | 绝对 | 超额 | 胜率 | t(日聚类) |")
    print("|---|---|---|---|---|---|---|---|")
    for hh in HOLDS:
        for rule, dsel in (("追突破", np.zeros(len(ev), dtype=int)),
                           ("回踩不破", pb), ("站稳", st)):
            s = stat(rule, *returns_for(dsel, hh))
            if not s.get("n", 0):
                continue
            rows1.append({"规则": rule, "持有": f"T+{hh}", **s})
            print(f"| {rule} | T+{hh} | {s['n']} | {s['覆盖']:.0f}% | {s['绝对%']:+.2f}% | "
                  f"{s['超额%']:+.2f}% | {s['胜率%']:.0f}% | {fmt_t(s)} |")

    rows2 = []
    print("\n| 配对（同股同事件） | 持有 | n | 追突破超额 | 回踩超额 | 差 | 配对t |")
    print("|---|---|---|---|---|---|---|")
    for hh in HOLDS:
        _, e1, _ = returns_for(np.zeros(len(ev), dtype=int), hh)
        _, e2, _ = returns_for(pb, hh)
        m = np.isfinite(e1) & np.isfinite(e2)
        if m.sum() < 5:
            continue
        d = pd.Series(e2[m] - e1[m])
        t = d.mean() / (d.std(ddof=1) / np.sqrt(len(d))) if len(d) > 2 and d.std(ddof=1) else np.nan
        rows2.append({"持有": f"T+{hh}", "n": int(m.sum()), "追突破超额": e1[m].mean() * 100,
                      "回踩超额": e2[m].mean() * 100, "差pp": d.mean() * 100, "配对t": t})
        print(f"| {det} | T+{hh} | {m.sum()} | {e1[m].mean()*100:+.2f}% | {e2[m].mean()*100:+.2f}% | "
              f"{d.mean()*100:+.2f}pp | {t:+.2f} |")

    rows3 = []
    print("\n| tol | deep | hold | n | 覆盖 | 超额% | t |")
    print("|---|---|---|---|---|---|---|")
    for tol, deep, hold in itertools.product((0.0, 0.5, 1.0, 2.0), (1.0, 2.0, 3.0, 5.0), (0.5, 1.0, 2.0)):
        s = stat("grid", *returns_for(path_first("pullback", tol, deep, hold), 5))
        if s.get("n", 0) < 20:
            continue
        rows3.append({"tol": f"{tol:g}%", "deep": f"{deep:g}%", "hold": f"{hold:g}%", **s})
        print(f"| {tol:g}% | {deep:g}% | {hold:g}% | {s['n']} | {s['覆盖']:.0f}% | "
              f"{s['超额%']:+.2f}% | {fmt_t(s)} |")

    rows4 = []
    print("\n| 年 | 追n | 追超额 | 回踩n | 回踩超额 |")
    print("|---|---|---|---|---|")
    yr = pd.to_datetime(ev["date"]).dt.year.to_numpy()
    _, e1, _ = returns_for(np.zeros(len(ev), dtype=int), 5)
    _, e2, _ = returns_for(pb, 5)
    for y in sorted(set(yr)):
        m = yr == y
        m1, m2 = m & np.isfinite(e1), m & np.isfinite(e2)
        if m1.sum() < 5 or m2.sum() < 5:
            continue
        rows4.append({"年": int(y), "追n": int(m1.sum()), "追超额": e1[m1].mean() * 100,
                      "回踩n": int(m2.sum()), "回踩超额": e2[m2].mean() * 100})
        print(f"| {y} | {m1.sum()} | {e1[m1].mean()*100:+.2f}% | {m2.sum()} | {e2[m2].mean()*100:+.2f}% |")
    return rows1, rows2, rows3, rows4, ev


def cmd_analyze(a):
    import pandas as pd

    if not EV_PARQUET.exists():
        print("还没有扫描结果，先跑 scan")
        return
    ev_all = pd.read_parquet(EV_PARQUET)
    bs = pd.read_parquet(BASE_PARQUET)
    # 只用横截面够厚的日子做基线/样本（老缓存里有 1999-2022 的残段，当天只有几只票）
    thick = set(bs.loc[bs["n"] >= 200, "date"])
    n0 = len(ev_all)
    ev_all = ev_all[ev_all["date"].isin(thick)].copy()
    bs = bs[bs["date"].isin(thick)]
    print(f"剔除横截面<200只的日期：事件 {n0} → {len(ev_all)}")
    hmap = {hh: dict(zip(bs["date"], bs[f"m{hh}"])) for hh in HOLDS}

    dets = ["rich", "simple"] if a.detector == "both" else [a.detector]
    out = {}
    for det in dets:
        if f"sig_{'r' if det == 'rich' else 's'}" not in ev_all:
            continue
        ev = ev_all.copy()
        # 幅度带过滤按各自口径（rich 用 pct_r，simple 用 pct_s；都已是生产口径）
        out[det] = _analyze_detector(det, ev, hmap, a)

    def md_table(rows, cols, skip_zero_t=True):
        o = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        for r in rows:
            cells = []
            for c in cols:
                v = r.get(c, "")
                if isinstance(v, float):
                    if c == "覆盖":
                        v = f"{v:.0f}%"
                    elif c in ("胜率%",):
                        v = f"{v:.0f}%"
                    elif c == "n":
                        v = f"{v:.0f}"
                    elif c in ("差pp", "配对t", "追超额", "回踩超额", "追突破超额", "回踩超额%"):
                        v = "—" if v != v else f"{v:+.2f}"
                    else:
                        v = "—" if (v != v and skip_zero_t) else f"{v:+.2f}"
                cells.append(str(v))
            o.append("| " + " | ".join(cells) + " |")
        return o

    lines = ["# 箱体突破 第二轮：回踩 / 站稳 vs 追突破（2026-10-23 续）", "",
             f"* 回踩口径：tol={a.tol:.1%} / deep={a.deep:.1%} / hold={a.hold:.1%} · 观察 K={K_PATH} 天",
             "* 超额 = 减**同日全市场同口径均值**；t 按**进场日聚类**；进场价 = 次日/回踩次日**开盘**",
             "* `rich` = 猎手「🚀 突破箱体」卡片（t_gui._detect_boxes，含 as-of 修复）",
             "* `simple` = 建仓扫描「突破箱体」c2（core.position_builder._detect_boxes_simple）", ""]
    sec = {"rich": "A. rich（猎手卡片）", "simple": "B. simple（建仓扫描 c2）"}
    for det, (r1, r2, r3, r4, ev) in out.items():
        lines += [f"## {sec.get(det, det)}", "",
                  f"事件 {len(ev)} · {ev['date'].min()} ~ {ev['date'].max()} · {ev['code'].nunique()} 只", "",
                  "### 表一 规则 × 持有期", ""]
        lines += md_table(r1, ["规则", "持有", "n", "覆盖", "绝对%", "超额%", "胜率%", "t"])
        lines += ["", "### 表二 同一事件配对：追突破 vs 回踩", ""]
        lines += md_table(r2, ["持有", "n", "追突破超额", "回踩超额", "差pp", "配对t"])
        lines += ["", "### 表三 回踩参数扫描（T+5 超额）", ""]
        lines += md_table(r3, ["tol", "deep", "hold", "n", "覆盖", "超额%", "t"])
        lines += ["", "### 表四 按年份（T+5 超额）", ""]
        lines += md_table(r4, ["年", "追n", "追超额", "回踩n", "回踩超额"])
        lines.append("")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n写入 {OUT_MD}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--stocks", type=int, default=2000)
    s.add_argument("--days", type=int, default=700)
    s.add_argument("--procs", type=int, default=3)
    s.set_defaults(fn=cmd_scan)
    an = sub.add_parser("analyze")
    an.add_argument("--detector", choices=["rich", "simple", "both"], default="both")
    an.add_argument("--tol", type=float, default=0.005)
    an.add_argument("--deep", type=float, default=0.02)
    an.add_argument("--hold", type=float, default=0.005)
    an.set_defaults(fn=cmd_analyze)
    v = sub.add_parser("verify-simple")
    v.set_defaults(fn=lambda _a: _verify_simple())
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
