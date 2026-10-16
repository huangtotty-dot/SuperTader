# -*- coding: utf-8 -*-
"""S3 监控线：选股因子 roll60 IC 衰减监控（增量日更，供每日复盘调用）。

背景：D1 衰减归因（doc/experiment/2026-10-10_S2_后半段衰减归因.md）发现
短名单选股因子 2026-02-24 后结构性衰减、2026-08 起修复。防御机制：
    某因子 roll60 IC < 0 持续 >= 20 个交易日 → 告警（建议该因子权重减半）
    连续 < 0 达 10 日 → 预警（提前关注）
本脚本把这个规则工程化为可每日增量运行的监控线。

IC 口径（与 fullhist_robustness_2026-10-09 / decay_diag 完全一致，不为结果调参）：
    因子：REV10 / GAP / AMOUNT_CHG_inv / PRICE_POS60_inv（方向统一做多）
    前瞻收益 buy_open h=5：open(t+6)/open(t+1)-1（T+1 开盘买，持有 5 日后开盘卖）
    过滤器：上市>=120 交易日 + AMOUNT20>=5000 万 + 剔停牌(volume=0) + 剔ST
    RankIC：按日截面 Spearman（组内平均秩 + pearson），覆盖率下限 30 只
    roll60：rolling(60, min_periods=40).mean()（同 decay_diag）

复用（只读 import，不修改）：
    ic_layer.load_panel / evaluate_factor
    volatility_screen._prep_panel / load_universe
    daily_selection_screen.compute_factor_series / build_tradable_mask /
        apply_filter / to_factor_frame

CLI（单命令 <300s；首次全量重算约 2~4 分钟，日常增量走窗口面板缓存秒级）：
    python ic_monitor.py --date 2026-09-17              # 增量更新到指定日
    python ic_monitor.py --date 2026-09-17 --rebuild    # 清空缓存全量重算
    python ic_monitor.py --date 2026-09-17 --validate   # 与官方IC/decay_diag对拍
    python ic_monitor.py --start 2024-03-05 --warn 10 --alarm 20

产物（t_io/validation/factor_mining/results/ic_monitor/）：
    ic_daily_h5.csv        日度 IC 存储（宽表，增量追加）
    roll60_ic.csv          roll60 IC 序列 + 各因子连续<0天数
    monitor_status.json    最新告警状态（复盘程序读这个）
    ic_roll60_monitor.png  四因子 roll60 走势图（中文标注）
    validation.json        --validate 对拍结果
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ic_layer import evaluate_factor, load_panel  # noqa: E402
from volatility_screen import _prep_panel, load_universe  # noqa: E402
from daily_selection_screen import (  # noqa: E402
    apply_filter,
    build_tradable_mask,
    compute_factor_series,
    to_factor_frame,
)

WORKSPACE = Path(__file__).resolve().parents[3]
PANEL_DIR = WORKSPACE / "t_io" / "validation" / "xsection" / "panel"
OUT = Path(__file__).resolve().parent / "results" / "ic_monitor"
FH_DIR = Path(__file__).resolve().parent / "results" / "fullhist_robustness_2026-10-09"
DECAY_DIR = Path(__file__).resolve().parent / "results" / "decay_diag"

FACTORS = ["REV10", "GAP", "AMOUNT_CHG_inv", "PRICE_POS60_inv"]
H = 5                      # 前瞻持有期（buy_open h5）
FWD_NEED = 1 + H           # t 日 IC 需要 t+1..t+6 共 6 行后续 open
MIN_COVERAGE = 30          # 截面覆盖率下限（同官方）
ROLL_WIN = 60
ROLL_MINP = 40             # 同 decay_diag
WARMUP_CAL_DAYS = 300      # 面板截断缓冲（>120 交易日上市闸 + 60 日因子窗）

IC_STORE = OUT / "ic_daily_h5.csv"
ROLL_CSV = OUT / "roll60_ic.csv"
STATUS_JSON = OUT / "monitor_status.json"
PNG_OUT = OUT / "ic_roll60_monitor.png"
TMP = OUT / "_tmp"
PANEL_CKPT = TMP / "panel_win.parquet"
PANEL_META = TMP / "panel_win_meta.json"

# D1 归因确认的结构拐点（仅用于图上标注，不参与判定逻辑）
BP_ANNOTATE = "2026-02-24"


# ---------------------------------------------------------------------------
# 面板加载（带窗口缓存：日常增量运行不必每次读 9.7M 行全量分片）
# ---------------------------------------------------------------------------
def _shards_newer_than_ckpt() -> bool:
    """任一面板分片比缓存新 → 缓存失效（面板被刷新过）。"""
    if not PANEL_CKPT.exists():
        return True
    ckpt_mt = PANEL_CKPT.stat().st_mtime
    shard_dir = PANEL_DIR / "shards"
    files = list(shard_dir.glob("*.parquet")) if shard_dir.is_dir() else []
    files += [p for p in PANEL_DIR.glob("*.parquet") if p.name != "universe.parquet"]
    return any(p.stat().st_mtime > ckpt_mt for p in files)


def _load_window_panel(need_start: pd.Timestamp, need_end: pd.Timestamp) -> pd.DataFrame:
    """返回 [need_start - 已在截断时处理, need_end] 覆盖的预处理后窗口面板。

    缓存命中条件：缓存起点 <= need_start 且缓存终点 >= need_end，
    且面板分片不比缓存新。缓存未命中：全量 load_panel 后按 need_start
    截断并写缓存。
    """
    if PANEL_CKPT.exists() and PANEL_META.exists():
        meta = json.loads(PANEL_META.read_text(encoding="utf-8"))
        if (pd.Timestamp(meta["date_min"]) <= need_start
                and pd.Timestamp(meta["date_max"]) >= need_end
                and not _shards_newer_than_ckpt()):
            df = pd.read_parquet(PANEL_CKPT)
            df["date"] = pd.to_datetime(df["date"])
            print(f"[panel] 命中窗口缓存 {meta['date_min']}~{meta['date_max']} "
                  f"({len(df):,} 行)", flush=True)
            return df
        print("[panel] 缓存窗口不覆盖本次需求，重建 ...", flush=True)

    t0 = time.perf_counter()
    print(f"[panel] 全量加载 <- {PANEL_DIR}", flush=True)
    panel = _prep_panel(load_panel(str(PANEL_DIR)))
    print(f"[panel] {panel['symbol'].nunique()} 只, {len(panel):,} 行, "
          f"{panel['date'].min().date()}~{panel['date'].max().date()} "
          f"({time.perf_counter() - t0:.0f}s)", flush=True)
    win = panel[panel["date"] >= need_start].reset_index(drop=True)
    TMP.mkdir(parents=True, exist_ok=True)
    win.to_parquet(PANEL_CKPT, index=False)
    PANEL_META.write_text(json.dumps(
        {"date_min": str(win["date"].min().date()),
         "date_max": str(win["date"].max().date()),
         "rows": int(len(win))}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[panel] 窗口截断至 >= {need_start.date()} 并写缓存 "
          f"({len(win):,} 行)", flush=True)
    return win


# ---------------------------------------------------------------------------
# 日度 IC 增量计算
# ---------------------------------------------------------------------------
def _compute_ic(panel: pd.DataFrame, universe, date_min: pd.Timestamp,
                date_max: pd.Timestamp) -> pd.DataFrame:
    """在窗口面板上算四因子 buy_open h5 日度 RankIC，返回宽表（索引=date）。"""
    t0 = time.perf_counter()
    factors = compute_factor_series(panel)
    mask = build_tradable_mask(panel, factors, universe)
    factors_f = apply_filter(factors, mask)
    factors_f["AMOUNT_CHG_inv"] = -factors_f["AMOUNT_CHG"]
    factors_f["PRICE_POS60_inv"] = -factors_f["PRICE_POS60"]
    print(f"[factor] 因子+过滤完成 ({time.perf_counter() - t0:.0f}s), "
          f"可交易 {int(mask.sum()):,}/{len(mask):,} 行", flush=True)

    out = {}
    for name in FACTORS:
        t1 = time.perf_counter()
        fdf = to_factor_frame(factors_f, name)
        fdf = fdf[(fdf["date"] >= date_min) & (fdf["date"] <= date_max)]
        ev = evaluate_factor(fdf, panel, horizons=(H,), min_coverage=MIN_COVERAGE)
        ic = ev[H]["buy_open"]["ic_series"]
        ic = ic[(ic.index >= date_min) & (ic.index <= date_max)]
        out[name] = ic
        print(f"[ic] {name}: {len(ic)} 日, mean={ic.mean():+.4f} "
              f"({time.perf_counter() - t1:.0f}s)", flush=True)
    df = pd.DataFrame(out)
    df.index.name = "date"
    return df.sort_index()


def update_ic_store(as_of: pd.Timestamp, start: pd.Timestamp,
                    rebuild: bool) -> tuple[pd.DataFrame, pd.Timestamp]:
    """增量维护日度 IC 存储。返回 (store 宽表, 最新可算 IC 日期)。"""
    store = pd.DataFrame()
    if IC_STORE.exists() and not rebuild:
        store = pd.read_csv(IC_STORE, index_col=0, parse_dates=True)
        store = store.reindex(columns=FACTORS)

    # 目标 IC 末端：as_of 往前 6 个面板交易日（buy_open h5 需要 t+6 open）
    # 需要面板交易日历；窗口面板加载后确定（need_end=as_of，面板刷新会触发缓存重建）
    first_missing = start if store.empty else store.index.max() + pd.Timedelta(days=1)
    need_start = first_missing - pd.Timedelta(days=WARMUP_CAL_DAYS)
    panel = _load_window_panel(need_start, as_of)

    cal = pd.DatetimeIndex(sorted(panel["date"].unique()))
    cal = cal[cal <= min(as_of, cal.max())]
    if len(cal) < FWD_NEED + 1:
        raise RuntimeError(f"面板交易日不足（{len(cal)} 天），无法计算 h{H} 前瞻 IC")
    ic_end = cal[-(FWD_NEED + 1)]          # 最后一个有完整 t+1..t+6 前瞻窗的日子
    print(f"[cal] 面板末端 {cal.max().date()}, as_of={as_of.date()}, "
          f"最新可算 IC 日 = {ic_end.date()}", flush=True)

    if store.empty:
        missing_min, missing_max = start, ic_end
    else:
        missing_min = max(store.index.max() + pd.Timedelta(days=1), start)
        missing_max = ic_end

    if missing_min <= missing_max:
        new_ic = _compute_ic(panel, load_universe(PANEL_DIR),
                             pd.Timestamp(missing_min), pd.Timestamp(missing_max))
        if len(new_ic):
            store = pd.concat([store, new_ic])
            store = store[~store.index.duplicated(keep="last")].sort_index()
            print(f"[store] 新增 {len(new_ic)} 日 "
                  f"({new_ic.index.min().date()}~{new_ic.index.max().date()})", flush=True)
    else:
        print("[store] 无新增交易日，直接复用既有存储", flush=True)

    store = store[store.index >= start]
    if store.empty:
        raise RuntimeError(
            f"IC 存储为空：监控起点 {start.date()} 晚于最新可算 IC 日 "
            f"{ic_end.date()}，请检查 --start/--date 参数")
    OUT.mkdir(parents=True, exist_ok=True)
    store.to_csv(IC_STORE, encoding="utf-8")
    print(f"[store] 存储 {len(store)} 日 -> {IC_STORE}", flush=True)
    return store, ic_end


# ---------------------------------------------------------------------------
# roll60 + 连续<0 计数 + 告警状态
# ---------------------------------------------------------------------------
def _consec_neg(s: pd.Series) -> pd.Series:
    """每个时点上「截至当日 roll60 连续 <0 的天数」（NaN/正数都清零）。"""
    neg = (s < 0).astype(int)
    # 经典连续计数：按「非负段」分组累加
    grp = neg.ne(neg.shift()).cumsum()
    return neg.groupby(grp).cumsum().where(neg == 1, 0)


def build_monitor(store: pd.DataFrame, ic_end: pd.Timestamp, as_of: pd.Timestamp,
                  warn_days: int, alarm_days: int) -> dict:
    roll = store.rolling(ROLL_WIN, min_periods=ROLL_MINP).mean()
    consec = pd.DataFrame({f: _consec_neg(roll[f]) for f in FACTORS})

    out_df = roll.copy()
    out_df.columns = [f"roll60_{c}" for c in out_df.columns]
    for f in FACTORS:
        out_df[f"consec_neg_{f}"] = consec[f].astype(int)
    out_df.index.name = "date"
    out_df.to_csv(ROLL_CSV, encoding="utf-8")
    print(f"[roll60] -> {ROLL_CSV}", flush=True)

    def status_of(n: int) -> str:
        if n >= alarm_days:
            return "告警"
        if n >= warn_days:
            return "预警"
        return "正常"

    factors_status = {}
    for f in FACTORS:
        r = roll[f].dropna()
        c = consec[f]
        last_date = r.index.max() if len(r) else None
        last_roll = float(r.iloc[-1]) if len(r) else None
        n_neg = int(c.loc[last_date]) if last_date is not None else 0
        # 历史统计：本监控窗内告警天数与最深连续<0
        alarm_mask = c >= alarm_days
        factors_status[f] = {
            "last_date": str(last_date.date()) if last_date is not None else None,
            "ic_last": round(float(store[f].dropna().iloc[-1]), 4) if store[f].notna().any() else None,
            "ic_mean20": round(float(store[f].dropna().iloc[-20:].mean()), 4) if store[f].notna().any() else None,
            "roll60": round(last_roll, 4) if last_roll is not None else None,
            "consec_neg_days": n_neg,
            "status": status_of(n_neg),
            "suggested_weight_mult": 0.5 if n_neg >= alarm_days else 1.0,
            "hist_alarm_days": int(alarm_mask.sum()),
            "hist_max_consec_neg": int(c.max()),
            "hist_last_alarm_date": (str(c.index[alarm_mask].max().date())
                                     if alarm_mask.any() else None),
        }

    alarm_now = [f for f, s in factors_status.items() if s["status"] == "告警"]
    warn_now = [f for f, s in factors_status.items() if s["status"] == "预警"]
    if alarm_now:
        action = f"{'/'.join(alarm_now)} roll60 IC 连续<0 ≥{alarm_days}日 → 建议对应因子权重减半"
    elif warn_now:
        action = f"{'/'.join(warn_now)} 连续<0 ≥{warn_days}日，进入预警观察"
    else:
        action = "无动作"

    status = {
        "generated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
        "as_of": str(as_of.date()),
        "ic_end": str(ic_end.date()),
        "params": {"h": H, "forward": "buy_open open(t+6)/open(t+1)-1",
                   "roll_win": ROLL_WIN, "roll_min_periods": ROLL_MINP,
                   "warn_days": warn_days, "alarm_days": alarm_days,
                   "min_coverage": MIN_COVERAGE,
                   "monitor_start": str(store.index.min().date())},
        "factors": factors_status,
        "overall": {"any_alarm": bool(alarm_now), "alarm_factors": alarm_now,
                    "warn_factors": warn_now, "action": action},
    }
    STATUS_JSON.write_text(json.dumps(status, ensure_ascii=False, indent=1),
                           encoding="utf-8")
    print(f"[status] -> {STATUS_JSON}", flush=True)
    return status


# ---------------------------------------------------------------------------
# 走势图（中文标注，CJK 字体）
# ---------------------------------------------------------------------------
def plot_monitor(store: pd.DataFrame, status: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    try:
        sys.path.insert(0, str(Path(sys.executable).resolve().parent.parent.parent))
        from daimon_runtime import setup_plot
        setup_plot()
    except Exception:
        matplotlib.rcParams["font.sans-serif"] = [
            "Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
        matplotlib.rcParams["axes.unicode_minus"] = False
    import matplotlib.pyplot as plt

    roll = store.rolling(ROLL_WIN, min_periods=ROLL_MINP).mean()
    alarm_days = status["params"]["alarm_days"]
    bp = pd.Timestamp(BP_ANNOTATE)
    colors = {"正常": "#3a7d44", "预警": "#c8862a", "告警": "#b03a2e"}

    fig, axes = plt.subplots(2, 2, figsize=(13, 8), sharex=True)
    for ax, f in zip(axes.ravel(), FACTORS):
        r = roll[f]
        ax.plot(r.index, r.values, lw=1.4, color="#33527a", label="roll60 IC")
        ax.axhline(0, color="#888", lw=0.8, ls="--")
        ax.fill_between(r.index, r.values, 0, where=(r.values < 0),
                        color="#b03a2e", alpha=0.18, interpolate=True)
        # 历史告警段（连续<0 ≥ alarm_days）标红点
        c = _consec_neg(r)
        am = c >= alarm_days
        if am.any():
            ax.scatter(r.index[am], r.values[am], s=8, color="#b03a2e",
                       zorder=3, label=f"告警段(≥{alarm_days}日)")
        if r.index.min() <= bp <= r.index.max():
            ax.axvline(bp, color="#c8862a", lw=1.0, ls=":",
                       label="结构拐点 2026-02-24")
        st = status["factors"][f]
        ax.set_title(f"{f}  ｜ 当前: {st['status']}（连续<0 {st['consec_neg_days']}日, "
                     f"roll60={st['roll60']:+.3f}）",
                     color=colors[st["status"]], fontsize=11)
        ax.legend(loc="upper left", fontsize=8)
        ax.grid(alpha=0.25)
        ax.set_ylabel("roll60 IC")
    fig.suptitle(
        f"选股因子 roll60 IC 衰减监控（buy_open h{H} RankIC，滚动{ROLL_WIN}日）"
        f"　截至 {status['ic_end']}", fontsize=13)
    fig.autofmt_xdate()
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(PNG_OUT, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"[png] -> {PNG_OUT}", flush=True)


# ---------------------------------------------------------------------------
# 对拍验证：官方 ic_daily CSV + decay_diag rolling60
# ---------------------------------------------------------------------------
def validate(store: pd.DataFrame) -> dict:
    res = {"ic_vs_official": {}, "roll60_vs_decay_diag": {}}
    roll = store.rolling(ROLL_WIN, min_periods=ROLL_MINP).mean()
    for f in FACTORS:
        off_csv = FH_DIR / f"ic_daily_{f}_h{H}.csv"
        if off_csv.exists():
            off = pd.read_csv(off_csv, index_col=0, parse_dates=True)["ic"]
            ov = store[f].dropna().index.intersection(off.dropna().index)
            if len(ov) >= 30:
                a, b = store[f].loc[ov], off.loc[ov]
                res["ic_vs_official"][f] = {
                    "overlap_days": int(len(ov)),
                    "corr": round(float(a.corr(b)), 4),
                    "mean_abs_diff": round(float((a - b).abs().mean()), 5),
                }
        dd_csv = DECAY_DIR / "rolling60_ic_4factors.csv"
        if dd_csv.exists():
            dd = pd.read_csv(dd_csv, index_col=0, parse_dates=True)[f]
            ov = roll[f].dropna().index.intersection(dd.dropna().index)
            if len(ov) >= 30:
                a, b = roll[f].loc[ov], dd.loc[ov]
                res["roll60_vs_decay_diag"][f] = {
                    "overlap_days": int(len(ov)),
                    "corr": round(float(a.corr(b)), 4),
                    "mean_abs_diff": round(float((a - b).abs().mean()), 5),
                }
    (OUT / "validation.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("[validate] " + json.dumps(res, ensure_ascii=False), flush=True)
    return res


# ---------------------------------------------------------------------------
def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="roll60 IC 衰减监控线")
    ap.add_argument("--date", default=None,
                    help="截至日期 YYYY-MM-DD（默认=面板最大交易日）")
    ap.add_argument("--start", default="2024-03-05",
                    help="监控窗起点（默认 2024-03-05，与 D1 归因主窗对齐）")
    ap.add_argument("--warn", type=int, default=10, help="预警阈值：连续<0天数")
    ap.add_argument("--alarm", type=int, default=20,
                    help="告警阈值：连续<0天数（→建议权重减半）")
    ap.add_argument("--rebuild", action="store_true", help="清空存储与面板缓存重算")
    ap.add_argument("--validate", action="store_true",
                    help="与官方 ic_daily / decay_diag rolling60 对拍")
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    if args.rebuild:
        for p in (IC_STORE, PANEL_CKPT, PANEL_META):
            if p.exists():
                p.unlink()
        print("[rebuild] 已清空 IC 存储与面板窗口缓存", flush=True)

    start = pd.Timestamp(args.start)
    if args.date:
        as_of = pd.Timestamp(args.date)
    elif PANEL_META.exists():
        as_of = pd.Timestamp(json.loads(PANEL_META.read_text(
            encoding="utf-8"))["date_max"])
    else:
        # 未跑过且无缓存：先探一次面板末端（全量加载，仅本次）
        as_of = pd.Timestamp(_prep_panel(load_panel(str(PANEL_DIR)))["date"].max())
        print(f"[cal] 未指定 --date，取面板末端 {as_of.date()}", flush=True)

    t0 = time.perf_counter()
    store, ic_end = update_ic_store(as_of, start, args.rebuild)
    status = build_monitor(store, ic_end, as_of, args.warn, args.alarm)
    if not args.no_plot:
        plot_monitor(store, status)
    if args.validate:
        validate(store)

    print("\n== 监控状态汇总 =", flush=True)
    for f in FACTORS:
        s = status["factors"][f]
        print(f"  {f:<16} roll60={s['roll60']:+.4f} 连续<0 {s['consec_neg_days']:>2}日 "
              f"-> {s['status']} (权重系数建议 x{s['suggested_weight_mult']}) "
              f"| 历史告警 {s['hist_alarm_days']} 日, 最深连负 {s['hist_max_consec_neg']} 日",
              flush=True)
    print(f"  总体: {status['overall']['action']}", flush=True)
    print(f"[done] 总耗时 {time.perf_counter() - t0:.0f}s, 产物 -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
