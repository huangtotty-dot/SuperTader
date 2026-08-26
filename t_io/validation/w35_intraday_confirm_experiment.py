# -*- coding: utf-8 -*-
"""
w35_intraday_confirm_experiment.py — 建仓信号「日内择时确认层」验证实验（2026-08-25 新增）

背景（见 doc/每周复盘/2026-W34_复盘.md + 建仓门控放行重跑统计_20260820.md）：
  现行方案A 建仓信号 = 日线时机门控 GO（timing_gate.timing_verdict）。GO 一旦成立立即报
  signal，按【收盘价】隐含成交，完全没有「现在这个价位能不能买」的日内确认层。
  08-18 若 regime 判对会发 10 个 signal，按收盘价买入次日均值 -5.70%（胜率 2/10）——
  其中相当一部分损失来自「追高买在日内高点」。

本实验的问题（唯一目标，不发散）：
  在系统已经判 GO 的那些日子里，把成交时点从「当日收盘价」换成「日内出现右侧买点确认后的价」，
  能不能把前向收益/回撤压下来？

三条对照臂（同一批 GO 日、同一批个股，无未来函数）：
  A. baseline_close  ——  T 日收盘价买入（复刻现行 signal 的隐含成交）
  B. intraday_confirm —— T 日盘中等右侧确认后买入：
        15分钟 bar 收盘价站上 EMA8（或 15m MACD 金叉），且 15m vol_ratio>vol_min，
        且该时点价 >= 当日 VWAP（不追在均价之下的弱势位）。取【首个】满足的 15m bar 收盘价成交。
        若全天不满足 → 该 GO 日不成交（NO-FILL，单独统计占比）。
  C. range_release   ——  regime=range 但个股 t_trend & 浅回撤成立（即当前会被判 watch_signal、
        结构性拿不到 signal 的那批），同样按 baseline_close 结算，评估「若震荡市放行会怎样」。

前向收益口径：与 w34 entry_timing 一致——T+1/T+3/T+5 收盘相对【成交价】。
  A/C 成交价=T日收盘；B 成交价=确认 bar 收盘（当日内），fwd 仍按日线 T+1/3/5 收盘算。
  额外记录 B 相对 A 的「日内让价」= 确认价/收盘价-1，看确认是买得更贵还是更便宜。

数据（全部无未来函数）：
  - 日线：position_builder.fetch_daily_kline（腾讯 qfq 缓存，截止 T 日）
  - 日内 1 分钟：tushare stk_mins（复用 w34_resonance_backtest_year._day_df 的月缓存）
  - regime / go：timing_gate.timing_verdict（内部 _regime 截止 date_str，as-of 安全）

⚠️ 沙箱无 tushare/网络时跑不动；须在本机（有 TUSHARE_TOKEN 或内置回落 token）执行。

用法：
    python t_io/validation/w35_intraday_confirm_experiment.py
    python t_io/validation/w35_intraday_confirm_experiment.py --start 2025-08-15 --end 2026-08-15
    python t_io/validation/w35_intraday_confirm_experiment.py --codes 000988 300054 --vol-min 1.2
输出：t_io/replay/intraday_confirm_{start}_{end}/  events.jsonl + report.md + 控制台对照表
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

# ── Windows 终端 UTF-8 编码修复 ──
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = Path(__file__).resolve().parents[2]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from core.position_builder import fetch_daily_kline  # noqa: E402
from core.timing_gate import timing_verdict  # noqa: E402
from analysis.indicators import resample_to_15min, add_15min_indicators  # noqa: E402
from w34_resonance_backtest_year import (  # noqa: E402
    _day_df, _stock_code_to_ts, _trading_days, WATCHLIST_FILE,
)
from analysis.index_regime_intraday import _iri_tushare_pro  # noqa: E402

OUT_ROOT = BASE / "t_io" / "replay"

# 确认层默认参数（--vol-min / --require 覆盖）
DEFAULT_VOL_MIN = 1.2       # 15m vol_ratio 放量阈值
DEFAULT_REQUIRE = "ema8"    # 右侧确认口径: ema8 | macd | either


# ============================================================
# 无未来函数的日内确认判定
# ============================================================
def _day_vwap_series(df1: pd.DataFrame) -> pd.Series:
    """当日累计 VWAP 序列（逐 1 分钟累计 Σamount/Σvol），无未来。缺 amount 时用 close*volume 代理。"""
    if "amount" in df1.columns and df1["amount"].fillna(0).sum() > 0:
        cum_amt = df1["amount"].fillna(0).cumsum()
    else:
        cum_amt = (df1["close"] * df1["volume"].fillna(0)).cumsum()
    cum_vol = df1["volume"].fillna(0).cumsum().replace(0, np.nan)
    return cum_amt / cum_vol


def find_confirm_entry(df1: pd.DataFrame, vol_min: float, require: str) -> dict:
    """在当日 1 分钟线上找【首个】右侧确认的 15 分钟 bar，返回成交信息或 None。

    无未来函数：对每个已收盘的 15m bar（floor 到 15min 且该 15min 窗口的分钟已全部到齐），
    用截止该 bar 收盘时刻的数据算 15m 指标；VWAP 用该 bar 收盘时刻的当日累计 VWAP。
    确认条件：
      - ema8:  15m close > ema_fast_15m
      - macd:  15m macd_15m 上穿 macd_signal_15m（金叉）或 macd_15m>signal 且 hist>0
      - either: 上述任一
      并且 15m vol_ratio_15m > vol_min，并且 close >= 当日 VWAP。
    """
    if df1 is None or df1.empty or len(df1) < 20:
        return None
    d = df1.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    d["vwap_cum"] = _day_vwap_series(d)

    df15 = add_15min_indicators(resample_to_15min(d))
    if df15 is None or df15.empty:
        return None
    df15 = df15.copy()
    df15["time"] = pd.to_datetime(df15["time"], errors="coerce")

    day = d["time"].iloc[0].normalize()
    last_min_ts = d["time"].iloc[-1]

    for _, bar in df15.iterrows():
        label = bar["time"]              # 15m bar 起始标签（floor）
        close_ts = label + pd.Timedelta(minutes=15)  # 该 bar 收盘时刻
        # 该 15m 窗口必须已完整收盘（当日最后一分钟 >= 收盘时刻），否则是未收盘根，跳过（防未来）
        if close_ts > last_min_ts + pd.Timedelta(minutes=1):
            continue
        c = bar.get("close")
        ema8 = bar.get("ema_fast_15m")
        macd = bar.get("macd_15m")
        sig = bar.get("macd_signal_15m")
        hist = bar.get("macd_hist_15m")
        volr = bar.get("vol_ratio_15m")
        if any(pd.isna(x) for x in (c, ema8, macd, sig, volr)):
            continue
        ema_ok = float(c) > float(ema8)
        macd_ok = (float(macd) > float(sig)) and (float(hist) > 0 if pd.notna(hist) else True)
        if require == "ema8":
            trig = ema_ok
        elif require == "macd":
            trig = macd_ok
        else:
            trig = ema_ok or macd_ok
        if not trig:
            continue
        if float(volr) <= vol_min:
            continue
        # VWAP 闸：确认时点价 >= 当日累计 VWAP（不追在均价之下的弱势位）
        vw_rows = d[d["time"] <= close_ts]
        if vw_rows.empty:
            continue
        vwap = float(vw_rows["vwap_cum"].iloc[-1]) if pd.notna(vw_rows["vwap_cum"].iloc[-1]) else None
        entry_px = float(vw_rows["close"].iloc[-1])
        if vwap is not None and entry_px < vwap:
            continue
        # 当日分钟线收盘（不复权，与 entry_px 同源）——供跨复权口径归一
        day_close_raw = float(d["close"].iloc[-1])
        return {
            "entry_time": str(close_ts),
            "entry_price": round(entry_px, 4),
            "day_close_raw": round(day_close_raw, 4),
            "vwap": round(vwap, 4) if vwap is not None else None,
            "vol_ratio_15m": round(float(volr), 3),
            "trigger": "ema8" if ema_ok else "macd",
        }
    return None


def _fwd_from_daily(daily: pd.DataFrame, t_date: str, entry_px: float) -> dict:
    """从日线 T 日往后取 T+1/T+3/T+5 收盘，相对 entry_px 的收益。无未来（只用 <= 已知日线）。
    返回 {fwd1,fwd3,fwd5, maxdd5}（maxdd5=T+1..T+5 最低收盘相对 entry 的最大浮亏）。"""
    d = daily.sort_values("date").reset_index(drop=True)
    idx = d.index[d["date"].astype(str) == str(t_date)]
    if len(idx) == 0 or entry_px <= 0:
        return {}
    i = int(idx[0])
    out = {}
    for label, k in (("fwd1", 1), ("fwd3", 3), ("fwd5", 5)):
        j = i + k
        out[label] = round(float(d["close"].iloc[j]) / entry_px - 1, 5) if j < len(d) else None
    lows = [float(d["close"].iloc[i + k]) for k in range(1, 6) if i + k < len(d)]
    out["maxdd5"] = round(min(lows) / entry_px - 1, 5) if lows else None
    return out


def _load_candidates(explicit):
    if explicit:
        return sorted(set(explicit))
    wl = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8")) if WATCHLIST_FILE.exists() else {}
    return sorted({c for c, v in (wl.get("stocks", {}) or {}).items()
                   if isinstance(v, dict) and not c.startswith("_example")
                   and v.get("status") in ("monitoring", "signal")})


def _agg(rows, key):
    """对一组事件按 fwd1/3/5/maxdd5 求均值、胜率。key 用于打印。"""
    def _stat(field):
        vals = [r[field] for r in rows if r.get(field) is not None]
        if not vals:
            return None, None
        arr = np.array(vals, dtype=float)
        return round(float(arr.mean()), 4), round(float((arr > 0).mean()), 4)
    m1, w1 = _stat("fwd1")
    m3, w3 = _stat("fwd3")
    m5, w5 = _stat("fwd5")
    dd, _ = _stat("maxdd5")
    return {"n": len(rows), "fwd1": m1, "win1": w1, "fwd3": m3, "win3": w3,
            "fwd5": m5, "win5": w5, "maxdd5": dd}


def main():
    ap = argparse.ArgumentParser(description="建仓信号日内确认层验证")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--codes", nargs="*", default=None)
    ap.add_argument("--vol-min", type=float, default=DEFAULT_VOL_MIN)
    ap.add_argument("--require", choices=["ema8", "macd", "either"], default=DEFAULT_REQUIRE)
    args = ap.parse_args()

    end = args.end or pd.Timestamp.now().strftime("%Y-%m-%d")
    start = args.start or (pd.Timestamp(end) - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    codes = _load_candidates(args.codes)
    days = _trading_days(start, end)
    print(f"[universe] {len(codes)} 候选 ｜ {start}~{end}（{len(days)} 工作日）"
          f" ｜ require={args.require} vol_min={args.vol_min}")

    try:
        pro = _iri_tushare_pro()
    except Exception as e:
        print(f"[FATAL] tushare 初始化失败（本机需 TUSHARE_TOKEN）：{type(e).__name__}: {e}")
        return

    arms = {"A_close": [], "B_confirm": [], "C_range": []}
    nofill = 0        # GO 日但当日无确认（B 臂无法成交）
    go_days = 0
    range_release_days = 0

    for ci, code in enumerate(codes, 1):
        daily = fetch_daily_kline(code)
        if daily is None or daily.empty or len(daily) < 66:
            continue
        daily = daily.sort_values("date").reset_index(drop=True)
        daily["date"] = daily["date"].astype(str)
        ts_code = _stock_code_to_ts(code)
        for day in days:
            if day not in set(daily["date"]):
                continue  # 非交易日 / 该股停牌
            try:
                tv = timing_verdict(code, day)
            except Exception:
                continue
            regime = tv.get("regime")
            f = tv.get("features") or {}
            t_close_rows = daily[daily["date"] == day]
            if t_close_rows.empty:
                continue
            t_close = float(t_close_rows["close"].iloc[0])

            # ---- C 臂：range 放行候选（当前判 watch_signal，signal 结构性不可达）----
            if regime == "range" and bool(f.get("trend_multihead")) and float(f.get("drawdown") or 0) >= -0.03:
                fwd = _fwd_from_daily(daily, day, t_close)
                if fwd:
                    range_release_days += 1
                    arms["C_range"].append({"code": code, "date": day,
                                            "entry_price": round(t_close, 4), **fwd})

            # ---- A/B 臂：仅在系统真正判 GO 的日子 ----
            if not tv.get("go"):
                continue
            go_days += 1
            fwd_a = _fwd_from_daily(daily, day, t_close)
            if fwd_a:
                arms["A_close"].append({"code": code, "date": day,
                                        "entry_price": round(t_close, 4), **fwd_a})
            # B 臂：当日 1 分钟找右侧确认
            df1 = _day_df(ts_code, day, pro)
            conf = find_confirm_entry(df1, args.vol_min, args.require) if (df1 is not None and not df1.empty) else None
            if conf is None:
                nofill += 1
                continue
            # ⚠️ 复权口径归一（2026-08-25 修）：日线 fetch_daily_kline 是 qfq 前复权，
            #   tushare stk_mins 是不复权原始价。B 臂确认价(不复权)不能直接喂 _fwd_from_daily(qfq)。
            #   做法：日内让价 = 确认价 / 当日分钟线收盘 - 1（同为不复权，同源，真实无偏），
            #   再把这个让价比例施加到 qfq 日线收盘上 → B 臂的 qfq 等效成交价，与 A 臂完全同空间可比。
            day_close_raw = conf.get("day_close_raw")
            slip = round(conf["entry_price"] / day_close_raw - 1, 5) if day_close_raw else None
            if slip is None:
                nofill += 1
                continue
            entry_qfq = round(t_close * (1 + slip), 4)  # qfq 空间的 B 臂等效成交价
            fwd_b = _fwd_from_daily(daily, day, entry_qfq)
            if fwd_b:
                arms["B_confirm"].append({"code": code, "date": day, **conf,
                                          "close_price": round(t_close, 4),
                                          "entry_qfq": entry_qfq,
                                          "intraday_slip": slip, **fwd_b})
        print(f"  [{ci}/{len(codes)}] {code} 累计 GO={go_days} B成交={len(arms['B_confirm'])} "
              f"NO-FILL={nofill} range放行={range_release_days}")

    # ── 输出 ──
    out_dir = OUT_ROOT / f"intraday_confirm_{start}_{end}"
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "events.jsonl", "w", encoding="utf-8") as fjs:
        for arm, rows in arms.items():
            for r in rows:
                fjs.write(json.dumps({"arm": arm, **r}, ensure_ascii=False) + "\n")

    stats = {arm: _agg(rows, arm) for arm, rows in arms.items()}
    fill_rate = round(len(arms["B_confirm"]) / go_days, 4) if go_days else None
    slips = [r["intraday_slip"] for r in arms["B_confirm"] if r.get("intraday_slip") is not None]
    avg_slip = round(float(np.mean(slips)), 5) if slips else None

    def _fmt(x):
        return f"{x:+.2%}" if isinstance(x, float) else "—"

    lines = [
        f"# 建仓信号日内确认层验证（{start}~{end}）", "",
        f"候选 {len(codes)} 只 ｜ GO 日 {go_days} ｜ B臂成交 {len(arms['B_confirm'])}（成交率 "
        f"{_fmt(fill_rate) if fill_rate is not None else '—'}）｜ NO-FILL {nofill} ｜ range放行样本 {range_release_days}",
        f"确认口径 require={args.require} · vol_min={args.vol_min} · B臂日内平均让价 "
        f"{_fmt(avg_slip) if avg_slip is not None else '—'}（>0=确认价比当日收盘贵，已做复权口径归一）", "",
        "| 臂 | 含义 | n | fwd1 | 胜1 | fwd3 | 胜3 | fwd5 | 胜5 | 平均maxdd5 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    meaning = {
        "A_close": "GO日收盘价买入(现行signal隐含成交)",
        "B_confirm": "GO日+日内右侧确认后买入",
        "C_range": "震荡市放行候选(收盘价,现被判watch_signal)",
    }
    for arm in ("A_close", "B_confirm", "C_range"):
        s = stats[arm]
        w = lambda x: f"{x:.0%}" if isinstance(x, float) else "—"  # noqa: E731
        lines.append(f"| {arm} | {meaning[arm]} | {s['n']} | {_fmt(s['fwd1'])} | {w(s['win1'])} | "
                     f"{_fmt(s['fwd3'])} | {w(s['win3'])} | {_fmt(s['fwd5'])} | {w(s['win5'])} | {_fmt(s['maxdd5'])} |")
    lines += [
        "", "## 读法",
        "- **B vs A**：确认层有没有价值——B 的 fwd/胜率若显著高于 A，且 maxdd5 更浅，说明「等日内右侧确认」值得做；",
        "  代价是成交率<100%（NO-FILL 那部分 GO 日被放弃）。若 B≈A，说明确认层是噪音，不值得加复杂度。",
        "- **日内让价**：B 平均让价若明显>0，说明确认买得更贵——要用更高的 fwd 覆盖这个成本才算净赚。",
        "- **C 臂**：震荡市放行的质量。C 的 fwd/胜率若不比 A 差，支持「range 市放行观察态 signal」；若明显更差，支持维持降频。",
        "", "> 无未来函数：日线截止 T 日；15m 指标只用已收盘 bar；VWAP 用当日累计。B 臂成交价严格为确认 bar 收盘（当日内）。",
    ]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n" + "\n".join(lines))
    print(f"\n[OK] → {out_dir}")


if __name__ == "__main__":
    main()
