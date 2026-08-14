# -*- coding: utf-8 -*-
"""W33 回测方案: 建仓/加仓当前水平评估（V1.0, 2026-08-14）

按 doc/每周复盘/W33_回测方案.md §3 指标字典 + §4 方法学(时间切分/基线对照/成本模型/样本诚实)。

建仓（日线层, 近 120 交易日）:
  双通道(冰点/突破) 现行条件 → 密度 / 3日胜率 / 5日胜率 / 期望收益(净) / 盈亏比 / 假阳性 / 最大亏损 / 重叠率
  时间切分: 训练段(前80) 为主评估窗 + 验证段(后40) 只测一次
  基线对照: 买入持有(池内等权) / 随机基线(同密度)

加仓（日内层, decision_trace 08-03~08-14 实盘 BUY_LOW 事件）:
  VWAP优势率 / 次日浮盈胜率 / 3日浮盈均值 / 破位率(MA10/20 近者)

成本模型: 双向 0.2%（期望收益扣减）。T+1（次日才可卖，用 D+3/D+5 收盘）。
样本诚实: 30 只池×120 日 signal 预计 30~100 笔，统计功效有限 → 结论分级。

用法: python t_io/validation/w33_backtest.py
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(r"E:\06_T")
sys.path.insert(0, str(BASE))

from position_builder import eval_dual_channels, fetch_daily_kline, _box_raw_pct  # noqa: E402 单一真源

COST_RT = 0.002  # 双向成本（建仓+卖出）
TRAIN_N, VALID_N = 80, 40

# ---------------- 数据辅助 ----------------

def _daily_ctx_as_of(kdf, date_str):
    """kline 切片至 date_str，按生产 _ensure_daily_indicators 公式现算 as-of daily_ctx。"""
    if kdf is None or kdf.empty:
        return {}
    df = kdf.copy()
    df["date"] = df["date"].astype(str)
    df = df[df["date"] <= str(date_str)]
    if len(df) < 30:
        return {}
    c = df["close"].astype(float)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd_dif = (ema12 - ema26).values
    macd_dea = pd.Series(macd_dif).ewm(span=9, adjust=False).mean().values
    macd_hist = (macd_dif - macd_dea) * 2
    d = c.diff()
    g = d.clip(lower=0).rolling(14, min_periods=1).mean()
    l = (-d.clip(upper=0)).rolling(14, min_periods=1).mean()
    rsi = (100 - 100 / (1 + (g / l.replace(0, float("nan"))))).fillna(50.0)
    boll_mid = c.rolling(20).mean()
    boll_std = c.rolling(20).std()
    boll_pct = (c - (boll_mid - 2 * boll_std)) / ((boll_mid + 2 * boll_std) - (boll_mid - 2 * boll_std)).replace(0, float("nan"))
    vol = df["volume"].astype(float)
    vol_ma5 = vol.rolling(5).mean()
    cross_up = (pd.Series(macd_dif) > pd.Series(macd_dea)) & (pd.Series(macd_dif).shift(1) <= pd.Series(macd_dea).shift(1))
    ma5 = c.rolling(5).mean()
    return {
        "daily_macd_dif": float(macd_dif[-1]), "daily_macd_dea": float(macd_dea[-1]),
        "daily_macd_golden": bool(cross_up.tail(5).any()),
        "daily_rsi": float(rsi.iloc[-1]),
        "daily_boll_pct": float(boll_pct.iloc[-1]) if pd.notna(boll_pct.iloc[-1]) else None,
        "daily_vol_today": float(vol.iloc[-1]),
        "daily_vol_ma5": float(vol_ma5.iloc[-1]) if pd.notna(vol_ma5.iloc[-1]) else None,
        "daily_ma5": float(ma5.iloc[-1]) if pd.notna(ma5.iloc[-1]) else None,
        "daily_price_ref": float(c.iloc[-1]),
        "_close": float(c.iloc[-1]), "_low": float(df["low"].astype(float).iloc[-1]),
        "_date": str(df["date"].iloc[-1]),
    }


def _load_pool():
    fp = BASE / "watchlist_buy.json"
    if fp.exists():
        try:
            wl = json.loads(fp.read_text(encoding="utf-8"))
            return [str(k) for k, v in wl.get("stocks", {}).items() if not str(k).startswith("_example")]
        except Exception:
            pass
    return ["000988", "588170", "600176", "600481", "603667", "002639", "300153", "300364"]


# ---------------- 建仓评估 ----------------

def _fwd(df, date_str):
    """signal 日后的 t+3/t+5 收盘、t+1..t+3 低点。返回 dict。"""
    df = df.copy()
    df["date"] = df["date"].astype(str)
    idx = df.index[df["date"] == date_str]
    if idx.empty:
        return None
    pos = idx[0]
    n = len(df)
    def _close(o):
        return float(df["close"].iloc[pos + o]) if pos + o < n else None
    def _low(o):
        return float(df["low"].iloc[pos + o]) if pos + o < n else None
    return {"c3": _close(3), "c5": _close(5),
            "lows": [_low(k) for k in (1, 2, 3) if pos + k < n]}


def _eval_build_channel(signals, kdf_by_code):
    """对一组 signal 事件计算建仓指标字典。signals=[{code,date,px,low}]。"""
    if not signals:
        return {"n": 0, "n3": 0, "n5": 0, "win3": None, "win5": None,
                "exp_ret3": None, "exp_ret3_gross": None, "profit_ratio": None,
                "fp_rate": None, "max_loss": None}
    ret3s, ret5s, fp = [], [], 0
    for s in signals:
        fwd = _fwd(kdf_by_code.get(s["code"]), s["date"])
        if not fwd or fwd["c3"] is None:
            continue
        r3 = (fwd["c3"] - s["px"]) / s["px"]
        ret3s.append(r3)
        if fwd["c5"] is not None:
            ret5s.append((fwd["c5"] - s["px"]) / s["px"])
        if fwd["lows"] and min(fwd["lows"]) < s["low"]:
            fp += 1
    n3 = len(ret3s)
    if n3 == 0:
        return {"n": len(signals), "n3": 0}
    wins = [r for r in ret3s if r > 0]
    losses = [r for r in ret3s if r <= 0]
    avg_w = np.mean(wins) if wins else 0
    avg_l = np.mean(losses) if losses else 0
    return {
        "n": len(signals), "n3": n3, "n5": len(ret5s),
        "win3": sum(1 for r in ret3s if r > 0) / n3,
        "win5": sum(1 for r in ret5s if r > 0) / len(ret5s) if ret5s else None,
        "exp_ret3": float(np.mean(ret3s) - COST_RT),          # 净(扣双向成本)
        "exp_ret3_gross": float(np.mean(ret3s)),
        "profit_ratio": (avg_w / abs(avg_l)) if abs(avg_l) > 1e-9 else None,
        "fp_rate": fp / len(signals),
        "max_loss": float(min(ret3s)),
    }


def build_assess(dates, pool, kdf_by_code):
    """评估双通道在建仓评估窗(默认全量日期)的指标 + 基线。返回报告 dict。"""
    ice, brk, both_days = [], [], []
    events = []
    for code in pool:
        kdf = kdf_by_code.get(code)
        if kdf is None or kdf.empty:
            continue
        kdf_s = kdf.sort_values("date").reset_index(drop=True)
        for date_str in dates:
            ctx = _daily_ctx_as_of(kdf_s, date_str)
            if not ctx or ctx.get("_date") != date_str:
                continue
            kdf_asof = kdf_s[kdf_s["date"].astype(str) <= str(date_str)]
            dc = eval_dual_channels(code, ctx, None, "eod", ctx.get("_close"),
                                    box_df=kdf_asof, opts={"_box_raw": _box_raw_pct(kdf_asof, ctx.get("_close"))})
            c1 = dc["channels"]["iceberg"]
            c2 = dc["channels"]["breakout"]
            c1_sig = c1["score"] == 80          # 冰点日线 setup 全过
            c2_sig = c2["verdict"] == "signal"
            ev = {"code": code, "date": date_str, "c1": c1_sig, "c2": c2_sig,
                  "px": ctx["_close"], "low": ctx["_low"]}
            events.append(ev)
            if c1_sig:
                ice.append(ev)
            if c2_sig:
                brk.append(ev)
            if c1_sig and c2_sig:
                both_days.append(date_str)
    months = max(len(dates) / 21.0, 1.0)
    sig_days = {(e["code"], e["date"]) for e in events if e["c1"] or e["c2"]}
    overlap = len({(e["code"], e["date"]) for e in events if e["c1"] and e["c2"]}) / len(sig_days) if sig_days else 0.0

    ice_m = _eval_build_channel(ice, kdf_by_code)
    brk_m = _eval_build_channel(brk, kdf_by_code)
    return {
        "ice": ice_m, "brk": brk_m,
        "density": (ice_m["n"] + brk_m["n"]) / months,
        "ice_density": ice_m["n"] / months, "brk_density": brk_m["n"] / months,
        "overlap": overlap, "n_dates": len(dates), "n_pool": len(pool),
    }


def buyhold_baseline(dates, pool, kdf_by_code):
    """买入持有基线: 池内等权 3 日收益均值。"""
    rets = []
    for code in pool:
        kdf = kdf_by_code.get(code)
        if kdf is None or kdf.empty:
            continue
        kdf_s = kdf.sort_values("date").reset_index(drop=True)
        for date_str in dates:
            fwd = _fwd(kdf_s, date_str)
            if fwd and fwd["c3"] is not None:
                px = float(kdf_s[kdf_s["date"].astype(str) == date_str]["close"].iloc[0])
                rets.append((fwd["c3"] - px) / px)
    return float(np.mean(rets)) if rets else None


# ---------------- 加仓评估（日内实盘事件） ----------------

def _dedup_buys():
    """从近期 decision_trace 提取每日每股首个 BUY_LOW 事件（建议价=信号价, vwap=当日VWAP）。"""
    events = {}
    for fp in sorted((BASE / "t_io/traces").glob("decision_trace_2026-08-*.jsonl")):
        d = fp.stem.replace("decision_trace_", "")
        for line in open(fp, encoding="utf-8"):
            r = json.loads(line)
            if r.get("decision") != "BUY_LOW" or not r.get("price"):
                continue
            code = r["code"]
            key = (d, code)
            if key not in events:  # 取当日首个
                events[key] = {"date": d, "code": code, "px": float(r["price"]),
                               "vwap": float(r.get("vwap") or 0) or float(r["price"])}
    return list(events.values())


def add_assess(kdf_by_code):
    evs = _dedup_buys()
    n = len(evs)
    vwap_adv = sum(1 for e in evs if e["px"] < e["vwap"])
    c1_w, c3_rets, break_ma = 0, [], 0
    n_c1, n_c3, n_brk = 0, 0, 0
    for e in evs:
        kdf = kdf_by_code.get(e["code"])
        if kdf is None or kdf.empty:
            continue
        df = kdf.sort_values("date").reset_index(drop=True)
        df["date"] = df["date"].astype(str)
        idx = df.index[df["date"] == e["date"]]
        if idx.empty:
            continue
        pos = idx[0]; L = len(df)
        c1 = float(df["close"].iloc[pos + 1]) if pos + 1 < L else None
        c3 = float(df["close"].iloc[pos + 3]) if pos + 3 < L else None
        if c1 is not None:
            n_c1 += 1
            c1_w += 1 if c1 > e["px"] else 0
        if c3 is not None:
            n_c3 += 1
            c3_rets.append((c3 - e["px"]) / e["px"])
        # 破位: 加仓后 3 日内收盘跌破最近支撑(MA10/20 近者)
        ma10 = float(df["close"].iloc[max(0, pos - 9): pos + 1].mean())
        ma20 = float(df["close"].iloc[max(0, pos - 19): pos + 1].mean())
        sup = ma20 if abs(e["px"] - ma20) <= abs(e["px"] - ma10) else ma10
        if c1 is not None and c1 < sup:
            n_brk += 1
            break_ma += 1
    return {
        "n": n, "vwap_adv": vwap_adv / n if n else None,
        "c1_win": c1_w / n_c1 if n_c1 else None, "n_c1": n_c1,
        "c3_mean": float(np.mean(c3_rets)) if c3_rets else None, "n_c3": n_c3,
        "break_rate": break_ma / n_brk if n_brk else None, "n_brk": n_brk,
    }


# ---------------- 参数网格寻优（W33 §4.2 多重比较控制: 每通道 ≤16 组） ----------------

def _precompute_build(dates, pool, kdf_by_code):
    """预计算每日每 code 的 daily_ctx + box_raw（变体无关），网格变体间复用。"""
    cache = {}
    for code in pool:
        kdf = kdf_by_code.get(code)
        if kdf is None or kdf.empty:
            continue
        kdf_s = kdf.sort_values("date").reset_index(drop=True)
        for date_str in dates:
            ctx = _daily_ctx_as_of(kdf_s, date_str)
            if not ctx or ctx.get("_date") != date_str:
                continue
            kdf_asof = kdf_s[kdf_s["date"].astype(str) <= str(date_str)]
            box_raw = _box_raw_pct(kdf_asof, ctx.get("_close"))
            cache[(code, date_str)] = (ctx, box_raw)
    return cache


def _eval_build_variant(cache, kdf_by_code, opts):
    """对一组 opts 判定双通道 signal 事件（复用预计算）。"""
    ice, brk = [], []
    for (code, date), (ctx, box_raw) in cache.items():
        dc = eval_dual_channels(code, ctx, None, "eod", ctx.get("_close"),
                                opts={**opts, "_box_raw": box_raw})
        c1, c2 = dc["channels"]["iceberg"], dc["channels"]["breakout"]
        if c1["score"] == 80:
            ice.append({"code": code, "date": date, "px": ctx["_close"], "low": ctx["_low"]})
        if c2["verdict"] == "signal":
            brk.append({"code": code, "date": date, "px": ctx["_close"], "low": ctx["_low"]})
    return ice, brk


def _grid_metrics(events, kdf_by_code, months):
    m = _eval_build_channel(events, kdf_by_code)
    return {"n": m["n"], "win3": m["win3"], "exp": m["exp_ret3"],
            "fp": m["fp_rate"], "density": m["n"] / months if months else 0}


def grid_search_ice(cache, kdf_by_code, months):
    """冰点网格: BOLL 阈(4) × 缩量阈(4) = 16 组。返回 [(opts, metrics)]。"""
    res = []
    for boll in (0.15, 0.25, 0.35, 0.50):
        for shrink in (0.8, 0.9, 1.0, 1.2):
            ice, _ = _eval_build_variant(cache, kdf_by_code, {"boll_ice_max": boll, "vol_shrink_ratio": shrink})
            res.append(({"boll_ice_max": boll, "vol_shrink_ratio": shrink},
                        _grid_metrics(ice, kdf_by_code, months)))
    return res


def grid_search_brk(cache, kdf_by_code, months):
    """突破网格: 放量倍(4) × 突破幅度下限(4) = 16 组。返回 [(opts, metrics)]。"""
    res = []
    for volc in (1.5, 2.0, 2.5, 3.0):
        for box_min in (0.3, 0.5, 1.0, 2.0):
            _, brk = _eval_build_variant(cache, kdf_by_code, {"vol_confirm_ratio": volc, "box_min_pct": box_min})
            res.append(({"vol_confirm_ratio": volc, "box_min_pct": box_min},
                        _grid_metrics(brk, kdf_by_code, months)))
    return res


def _pick_candidates(res):
    """参数高原选择: 期望收益≥0 且 密度≥1 的组按 (期望收益, 3日胜率) 排序; 取前 3 + 高原候选。
    高原候选 = 期望收益 ≥ 峰值×90% 且 n≥5 的组（邻域平坦性由热力图人工确认）。"""
    viable = [(o, m) for o, m in res if (m["exp"] is not None and m["exp"] > 0 and m["density"] >= 1)]
    viable.sort(key=lambda x: -x[1]["exp"])
    top = viable[:3]
    if viable:
        peak = viable[0][1]["exp"]
        plateau = [(o, m) for o, m in viable if m["exp"] >= peak * 0.9 and m["n"] >= 5]
        plateau.sort(key=lambda x: -x[1]["exp"])
        return top, plateau
    return [], []


# ---------------- 报告 ----------------

def _fmt(v, pct=True, digits=1):
    if v is None:
        return "N/A"
    return f"{v * 100:.{digits}f}%" if pct else f"{v:.{digits}f}"


def main():
    pool = _load_pool()
    kdf_by_code = {}
    for code in pool:
        try:
            kdf_by_code[code] = fetch_daily_kline(code)
        except Exception:
            kdf_by_code[code] = None

    # 共同交易日（取近 TRAIN_N+VALID_N=120 日）
    day_cnt = defaultdict(int)
    for kdf in kdf_by_code.values():
        if kdf is None or kdf.empty:
            continue
        for d in kdf["date"].astype(str).unique():
            day_cnt[d] += 1
    days = sorted(d for d, c in day_cnt.items() if c >= max(1, int(len(pool) * 0.5)))
    dates_all = days[-120:]
    train = dates_all[:TRAIN_N]
    valid = dates_all[TRAIN_N:]
    print(f"建仓评估窗: {len(dates_all)} 交易日 ({dates_all[0]}~{dates_all[-1]}) 训练段 {len(train)} / 验证段 {len(valid)}")

    print("\n" + "=" * 76)
    print("  [1] 建仓双通道 · 训练段（当前条件）")
    print("=" * 76)
    r = build_assess(train, pool, kdf_by_code)
    ice, brk = r["ice"], r["brk"]
    for name, m, d_health in (("冰点反转", ice, r["ice_density"]), ("突破跟随", brk, r["brk_density"])):
        print(f"\n  ■ {name}  (signal {m['n']} 次 / 密度 {d_health:.1f} 次每月)")
        print(f"    3日胜率    {_fmt(m['win3'])}   (健康线≥55%)")
        print(f"    5日胜率    {_fmt(m['win5'])}   (≥52%)")
        print(f"    期望收益   {_fmt(m['exp_ret3'], pct=True)} / 毛 {_fmt(m['exp_ret3_gross'], pct=True)}   (>+0.8%净)")
        print(f"    盈亏比     {_fmt(m['profit_ratio'], pct=False, digits=2)}   (≥1.3)")
        print(f"    假阳性率   {_fmt(m['fp_rate'])}   (<40%)")
        print(f"    最大单笔   {_fmt(m['max_loss'])}   (>-6%)")
        print(f"    样本 n3    {m['n3']}")
    print(f"\n  合计密度 {r['density']:.1f}/月 ｜ 通道重叠率 {_fmt(r['overlap'])}  (<30%)")

    bh = buyhold_baseline(train, pool, kdf_by_code)
    print(f"\n  基线对照(训练段): 买入持有池内等权 3日收益 = {_fmt(bh, pct=True)}")

    print("\n  [2] 建仓双通道 · 验证段（单次测试，只测当前条件）")
    rv = build_assess(valid, pool, kdf_by_code)
    iv, bv = rv["ice"], rv["brk"]
    print(f"  冰点: n={iv['n']} 3日胜率 {_fmt(iv['win3'])} 期望 {_fmt(iv['exp_ret3'])} 假阳性 {_fmt(iv['fp_rate'])}")
    print(f"  突破: n={bv['n']} 3日胜率 {_fmt(bv['win3'])} 期望 {_fmt(bv['exp_ret3'])} 假阳性 {_fmt(bv['fp_rate'])}")
    bhv = buyhold_baseline(valid, pool, kdf_by_code)
    print(f"  买入持有基线: {_fmt(bhv, pct=True)}")

    print("\n" + "=" * 76)
    print("  [3] 加仓 · 日内实盘 BUY_LOW 事件（decision_trace 08-03~08-14）")
    print("=" * 76)
    a = add_assess(kdf_by_code)
    print(f"  事件数: {a['n']}（按 code×date 去重, 实盘仅 10 天样本）")
    print(f"  VWAP优势率  {_fmt(a['vwap_adv'])}   (≥60%: 建议价低于当日VWAP)")
    print(f"  次日浮盈胜率 {_fmt(a['c1_win'])} (n={a['n_c1']})   (≥55%)")
    print(f"  3日浮盈均值 {_fmt(a['c3_mean'])} (n={a['n_c3']})   (>0)")
    print(f"  破位率     {_fmt(a['break_rate'])} (n={a['n_brk']})   (<35%: 3日内收盘破MA10/20近者)")

    print("\n" + "=" * 76)
    print("  [4] 参数网格寻优（训练段 16 组/通道 + 验证段单次验证）")
    print("=" * 76)

    cache = _precompute_build(train, pool, kdf_by_code)
    months = len(train) / 21.0

    def _print_heatmap(title, rows, val_key, dim1_key, dim2_key, pct=True):
        """rows=[(opts, metrics)]; dim1=行, dim2=列。显示 val_key 热力图。"""
        print(f"\n  ■ {title}")
        dim1s = sorted({o[dim1_key] for o, _ in rows})
        dim2s = sorted({o[dim2_key] for o, _ in rows})
        print("      " + "".join(f"{str(d):>9}" for d in dim2s))
        for d1 in dim1s:
            line = f"  {str(d1):>4} "
            for d2 in dim2s:
                m = next((mm for o, mm in rows if o[dim1_key] == d1 and o[dim2_key] == d2), None)
                line += f"{_fmt(m[val_key] if m else None, pct=pct):>9}"
            print(line)

    # 冰点网格
    ice_grid = grid_search_ice(cache, kdf_by_code, months)
    print("\n  ◆ 冰点通道网格（行=BOLL阈, 列=缩量阈）")
    _print_heatmap("冰点 · 期望收益(净)", ice_grid, "exp", "boll_ice_max", "vol_shrink_ratio")
    _print_heatmap("冰点 · 3日胜率", ice_grid, "win3", "boll_ice_max", "vol_shrink_ratio")
    _print_heatmap("冰点 · 假阳性率", ice_grid, "fp", "boll_ice_max", "vol_shrink_ratio")
    _print_heatmap("冰点 · 密度(次/月)", ice_grid, "density", "boll_ice_max", "vol_shrink_ratio", pct=False)
    ice_top, ice_plateau = _pick_candidates(ice_grid)
    print("\n  冰点候选(期望>0 且 密度≥1):")
    for o, m in ice_top:
        print(f"    BOLL≤{o['boll_ice_max']} 缩量<{o['vol_shrink_ratio']}  →  n={m['n']} 期望 {_fmt(m['exp'])} 3日 {_fmt(m['win3'])} 假阳性 {_fmt(m['fp'])} 密度 {m['density']:.1f}")
    if not ice_top:
        print("    （无期望>0 的候选——冰点通道当前参数域无正期望组合）")

    # 突破网格
    brk_grid = grid_search_brk(cache, kdf_by_code, months)
    print("\n  ◆ 突破通道网格（行=放量倍, 列=突破幅度下限%）")
    _print_heatmap("突破 · 期望收益(净)", brk_grid, "exp", "vol_confirm_ratio", "box_min_pct")
    _print_heatmap("突破 · 3日胜率", brk_grid, "win3", "vol_confirm_ratio", "box_min_pct")
    _print_heatmap("突破 · 假阳性率", brk_grid, "fp", "vol_confirm_ratio", "box_min_pct")
    _print_heatmap("突破 · 密度(次/月)", brk_grid, "density", "vol_confirm_ratio", "box_min_pct", pct=False)
    brk_top, brk_plateau = _pick_candidates(brk_grid)
    print("\n  突破候选(期望>0 且 密度≥1):")
    for o, m in brk_top:
        print(f"    放量>{o['vol_confirm_ratio']} 幅>={o['box_min_pct']}%  →  n={m['n']} 期望 {_fmt(m['exp'])} 3日 {_fmt(m['win3'])} 假阳性 {_fmt(m['fp'])} 密度 {m['density']:.1f}")
    if not brk_top:
        print("    （无期望>0 的候选——突破通道当前参数域无正期望组合）")

    # 验证段单次验证（各通道前 3 候选，W33 §5: 粗筛产前3 → 验证段单次测试）
    print("\n  ◆ 验证段单次验证（各通道前 3 候选，只测一次）")
    cache_v = _precompute_build(valid, pool, kdf_by_code)
    months_v = len(valid) / 21.0
    for ch_name, grid, ev_key in (("冰点", ice_grid, "ice"), ("突破", brk_grid, "brk")):
        top, _ = _pick_candidates(grid)
        if not top:
            print(f"  {ch_name}: 训练段无正期望候选，跳过验证")
            continue
        for o, _m in top[:3]:
            ice, brk = _eval_build_variant(cache_v, kdf_by_code, o)
            evs = ice if ev_key == "ice" else brk
            m = _grid_metrics(evs, kdf_by_code, months_v)
            print(f"  {ch_name} {o}  →  验证段 n={m['n']} 期望 {_fmt(m['exp'])} 3日 {_fmt(m['win3'])} 假阳性 {_fmt(m['fp'])}")

    print("\n  ◆ 参数高原（最优候选 ±1 邻域衰减）")
    for ch_name, top, plateau in (("冰点", ice_top, ice_plateau), ("突破", brk_top, brk_plateau)):
        if not plateau:
            continue
        peak = plateau[0][1]["exp"]
        others = plateau[1:]
        if others:
            decay = max(abs(peak - o[1]["exp"]) / peak for o in others) if peak else None
            print(f"  {ch_name}: 峰值 {_fmt(peak)} ｜ 邻域 {len(others)} 组 ｜ 最大衰减 {_fmt(decay, pct=True) if decay else 'N/A'}（<30% 视为平坦高原）")
        else:
            print(f"  {ch_name}: 仅 1 个候选，高原不成立（孤点，需谨慎）")

    print("\n" + "=" * 76)
    print("  结论分级（W33 §4.5）: 样本 n<30 → 方向性证据/直觉候选，非强证据")
    print("=" * 76)


if __name__ == "__main__":
    main()
