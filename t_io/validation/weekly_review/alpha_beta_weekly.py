#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""α/β 周聚合脚本（复盘清单 V2.0 §4.5 周聚合 / §八 口径定义）

口径（钉死，不得改）：
- 净值法：equity = 持仓市值(EOD收盘重估) + 现金（上游施工1 落盘）
- account_ret = (equity - flow_adjust) / prev_equity - 1
- alpha = account_ret - benchmark_ret（主基准沪深300 sh000300，辅基准科创50 sh000688）
- 费后统一；禁止持仓市值法；cash 取不到时 equity/alpha 为 null → 本脚本跳过并计数

输入：t_io/metrics/equity_daily_{date}.json（契约见 MODULE_DOC 下方 CONTRACT）
输出：t_io/validation/weekly_review/alpha_beta_weekly_{ISOweek}.json + .md（幂等覆盖）

CLI：
    python alpha_beta_weekly.py --week 2026-W39
    python alpha_beta_weekly.py --date 2026-09-23   # 自动取所在 ISO 周
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 本文件位于 E:\superTrader\t_io\validation\weekly_review\alpha_beta_weekly.py
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_METRICS_DIR = ROOT / "t_io" / "metrics"
DEFAULT_OUT_DIR = ROOT / "t_io" / "validation" / "weekly_review"

CONTRACT_FIELDS = [
    "date", "equity", "cash", "market_value", "prev_equity",
    "account_ret", "benchmark_ret", "benchmark_aux_ret",
    "alpha", "t0_realized", "flow_adjust", "source",
]

ATTRIBUTION_METHOD = (
    "粗拆占位：底仓浮动 = Σ(account_ret×equity) − Σt0_realized − Σflow_adjust，"
    "非精确归因（ret×equity 用当日收盘净值近似），仅作三拆方向参考"
)


def parse_iso_week(week_str: str):
    """'2026-W39' -> (year, week)"""
    part = week_str.strip().upper().replace("W", "").split("-")
    if len(part) != 2:
        raise ValueError(f"ISO 周格式错误: {week_str}，应为 YYYY-Www")
    return int(part[0]), int(part[1])


def week_dates(year: int, week: int):
    return [date.fromisocalendar(year, week, dow) for dow in range(1, 8)]


def iso_week_of(d: date):
    iso = d.isocalendar()
    return iso[0], iso[1]


def load_daily(metrics_dir: Path, d: date):
    fp = metrics_dir / f"equity_daily_{d.isoformat()}.json"
    if not fp.exists():
        return None
    with fp.open("r", encoding="utf-8") as f:
        return json.load(f)


def row_usable(rec: dict) -> bool:
    """cash 取不到 → equity/account_ret/alpha 为 null，整行跳过（§八：不许硬算）"""
    return all(rec.get(k) is not None for k in ("equity", "cash", "account_ret", "alpha"))


def collect_range(metrics_dir: Path, dates):
    """返回 (valid_rows, skipped_no_cash, files_found)，rows 按日期升序"""
    valid, skipped, found = [], 0, 0
    for d in dates:
        rec = load_daily(metrics_dir, d)
        if rec is None:
            continue
        found += 1
        if row_usable(rec):
            valid.append(rec)
        else:
            skipped += 1
    valid.sort(key=lambda r: r["date"])
    return valid, skipped, found


def geo_cum(rows, key):
    """几何累计收益 prod(1+r)-1；无数据返回 None"""
    if not rows:
        return None
    acc = 1.0
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        acc *= 1.0 + v
    return acc - 1.0


def max_drawdown(equity_series):
    """基于 equity 序列的最大回撤（负数）；不足2点返回 None"""
    pts = [e for e in equity_series if e is not None]
    if len(pts) < 2:
        return None
    peak = pts[0]
    mdd = 0.0
    for e in pts:
        peak = max(peak, e)
        if peak > 0:
            mdd = min(mdd, e / peak - 1.0)
    return mdd


def build_report(year: int, week: int, metrics_dir: Path):
    dates = week_dates(year, week)
    rows, skipped, found = collect_range(metrics_dir, dates)

    daily = []
    for r in rows:
        daily.append({
            "date": r["date"],
            "equity": r["equity"],
            "account_ret": r["account_ret"],
            "benchmark_ret": r.get("benchmark_ret"),
            "benchmark_aux_ret": r.get("benchmark_aux_ret"),
            "alpha": r["alpha"],
            "t0_realized": r.get("t0_realized"),
        })

    # --- 周累计（几何） ---
    acc_cum = geo_cum(rows, "account_ret")
    bench_cum = geo_cum(rows, "benchmark_ret")
    bench_aux_cum = geo_cum(rows, "benchmark_aux_ret")
    alpha_cum = (acc_cum - bench_cum) if (acc_cum is not None and bench_cum is not None) else None

    # --- α 归因三拆（占位粗拆） ---
    t0_vals = [r.get("t0_realized") for r in rows]
    t0_null_n = sum(1 for v in t0_vals if v is None)
    t0_sum = sum(v for v in t0_vals if v is not None) if any(v is not None for v in t0_vals) else None
    flow_sum = sum(r.get("flow_adjust") or 0.0 for r in rows)
    gross_pnl = sum(r["account_ret"] * r["equity"] for r in rows) if rows else None
    base_float = (
        gross_pnl - (t0_sum or 0.0) - flow_sum if gross_pnl is not None else None
    )

    # --- 滚动 4 周趋势（跨周读历史文件，含本周） ---
    all_dates = []
    for w_off in range(3, -1, -1):
        # ISO 周的周一往前推 7*w_off 天仍落在对应 ISO 周
        monday = dates[0]
        from datetime import timedelta
        wk_monday = monday - timedelta(days=7 * w_off)
        wy, ww = iso_week_of(wk_monday)
        all_dates.extend(week_dates(wy, ww))
    rows_4w, skipped_4w, found_4w = collect_range(metrics_dir, all_dates)
    alphas_4w = [r["alpha"] for r in rows_4w]
    alpha_mean = sum(alphas_4w) / len(alphas_4w) if alphas_4w else None
    alpha_pos_ratio = (
        sum(1 for a in alphas_4w if a > 0) / len(alphas_4w) if alphas_4w else None
    )
    mdd_4w = max_drawdown([r["equity"] for r in rows_4w])

    return {
        "week": f"{year}-W{week:02d}",
        "date_range": [dates[0].isoformat(), dates[-1].isoformat()],
        "files_found": found,
        "valid_days": len(rows),
        "skipped_no_cash": skipped,
        "daily": daily,
        "weekly": {
            "account_cum_ret": acc_cum,
            "benchmark_cum_ret": bench_cum,
            "benchmark_aux_cum_ret": bench_aux_cum,
            "alpha_cum": alpha_cum,
            "alpha_cum_method": "account_cum_ret − benchmark_cum_ret（各自几何累计后相减）",
        },
        "attribution": {
            "t0_realized_sum": t0_sum,
            "t0_realized_null_days": t0_null_n,
            "base_float_pnl_approx": base_float,
            "flow_adjust_sum": flow_sum,
            "method": ATTRIBUTION_METHOD,
        },
        "rolling_4w": {
            "alpha_mean": alpha_mean,
            "alpha_pos_ratio": alpha_pos_ratio,
            "max_drawdown": mdd_4w,
            "valid_days": len(rows_4w),
            "skipped_no_cash": skipped_4w,
            "files_found": found_4w,
        },
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": "alpha_beta_weekly.py（复盘清单V2.0 §4.5/§八；净值法；费后；主基准sh000300辅sh000688）",
    }


def fmt_pct(v):
    return "—" if v is None else f"{v * 100:+.3f}%"


def fmt_num(v, nd=2):
    return "—" if v is None else f"{v:.{nd}f}"


def render_md(rep: dict) -> str:
    L = []
    L.append(f"# α/β 周聚合 · {rep['week']}")
    L.append("")
    L.append(f"- 区间：{rep['date_range'][0]} ~ {rep['date_range'][1]}")
    L.append(f"- 文件命中 {rep['files_found']} 天，有效 {rep['valid_days']} 天，"
             f"skipped_no_cash: {rep['skipped_no_cash']}")
    L.append(f"- 口径：净值法（equity=持仓市值EOD重估+现金），费后，"
             f"主基准沪深300(sh000300)，辅基准科创50(sh000688)；现金缺失行跳过不硬算")
    L.append("")
    L.append("## 每日明细")
    L.append("")
    L.append("| 日期 | equity | r_t(账户) | β(沪深300) | α | t0_realized |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for d in rep["daily"]:
        L.append(
            f"| {d['date']} | {fmt_num(d['equity'])} | {fmt_pct(d['account_ret'])} "
            f"| {fmt_pct(d['benchmark_ret'])} | {fmt_pct(d['alpha'])} "
            f"| {fmt_num(d['t0_realized'])} |"
        )
    w = rep["weekly"]
    L.append(f"| **周累计** | — | **{fmt_pct(w['account_cum_ret'])}** "
             f"| **{fmt_pct(w['benchmark_cum_ret'])}** | **{fmt_pct(w['alpha_cum'])}** "
             f"| **{fmt_num(rep['attribution']['t0_realized_sum'])}** |")
    L.append("")
    L.append("## α 归因三拆（占位粗拆）")
    L.append("")
    a = rep["attribution"]
    L.append("| 分项 | 金额 | 说明 |")
    L.append("|---|---:|---|")
    L.append(f"| 做T差价 t0_realized | {fmt_num(a['t0_realized_sum'])} | "
             f"null 天数 {a['t0_realized_null_days']} |")
    L.append(f"| 底仓浮动(近似) | {fmt_num(a['base_float_pnl_approx'])} | 粗拆，非精确归因 |")
    L.append(f"| 基线调整 flow_adjust | {fmt_num(a['flow_adjust_sum'])} | 出入金调整 |")
    L.append("")
    L.append(f"> {a['method']}")
    L.append("")
    L.append("## 滚动 4 周趋势")
    L.append("")
    r4 = rep["rolling_4w"]
    L.append("| 指标 | 值 |")
    L.append("|---|---:|")
    L.append(f"| α 均值 | {fmt_pct(r4['alpha_mean'])} |")
    L.append(f"| α>0 日占比 | {fmt_pct(r4['alpha_pos_ratio'])} |")
    L.append(f"| 最大回撤(equity) | {fmt_pct(r4['max_drawdown'])} |")
    L.append(f"| 4周有效天数 | {r4['valid_days']}（skipped_no_cash: {r4['skipped_no_cash']}） |")
    L.append("")
    L.append(f"生成时间：{rep['generated_at']} · source: {rep['source']}")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="α/β 周聚合（复盘清单 V2.0 §4.5/§八）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--week", help="ISO 周，如 2026-W39")
    g.add_argument("--date", help="任一日期，自动取所在 ISO 周，如 2026-09-23")
    ap.add_argument("--metrics-dir", default=str(DEFAULT_METRICS_DIR),
                    help="equity_daily_*.json 所在目录（默认 t_io/metrics）")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="输出目录（默认 t_io/validation/weekly_review）")
    args = ap.parse_args()

    if args.week:
        year, week = parse_iso_week(args.week)
    else:
        year, week = iso_week_of(date.fromisoformat(args.date))
    week_label = f"{year}-W{week:02d}"

    metrics_dir = Path(args.metrics_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rep = build_report(year, week, metrics_dir)

    json_path = out_dir / f"alpha_beta_weekly_{week_label}.json"
    md_path = out_dir / f"alpha_beta_weekly_{week_label}.md"
    json_path.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_md(rep), encoding="utf-8")

    print(f"[alpha_beta_weekly] 周={week_label} 文件命中={rep['files_found']} "
          f"有效天数={rep['valid_days']} skipped_no_cash={rep['skipped_no_cash']}")
    print(f"[alpha_beta_weekly] 周累计: 账户={fmt_pct(rep['weekly']['account_cum_ret'])} "
          f"基准={fmt_pct(rep['weekly']['benchmark_cum_ret'])} "
          f"α={fmt_pct(rep['weekly']['alpha_cum'])}")
    print(f"[alpha_beta_weekly] 落盘: {json_path}")
    print(f"[alpha_beta_weekly] 落盘: {md_path}")


if __name__ == "__main__":
    main()
