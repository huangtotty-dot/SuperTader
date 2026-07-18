# -*- coding: utf-8 -*-
"""index_regime 回测脚本

按日期区间逐日调用 detect_index_regime(as_of=..., mode='eod')，并输出
日报/汇总 JSON 与 CSV。默认使用独立 state_dir，避免污染生产 state.json。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

import index_regime as ir


ROOT = Path(__file__).resolve().parent
SRC_STATE_DIR = ROOT / "t_io" / "index_regime"
DEFAULT_OUT_DIR = ROOT / "t_io" / "index_regime_backtests"


def _copy_seed_files(src: Path, dst: Path) -> List[str]:
    dst.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []
    for p in src.glob("breadth_*.json"):
        shutil.copy2(p, dst / p.name)
        copied.append(p.name)
    e5 = src / "e5_ths_cache.json"
    if e5.exists():
        shutil.copy2(e5, dst / e5.name)
        copied.append(e5.name)
    return copied


def _iter_dates(start: str, end: str, calendar: str) -> List[str]:
    if calendar == "all":
        idx = pd.date_range(start, end, freq="D")
    else:
        idx = pd.bdate_range(start, end)
    return [d.strftime("%Y-%m-%d") for d in idx]


def run_backtest(start: str, end: str, out_dir: Path, calendar: str = "bdays") -> Dict[str, Any]:
    backtest_state_dir = out_dir / f"state_{start}_to_{end}"
    if backtest_state_dir.exists():
        shutil.rmtree(backtest_state_dir)
    seed_files = _copy_seed_files(SRC_STATE_DIR, backtest_state_dir)

    # 独立回测状态目录，不污染当前运行状态
    ir.INDEX_REGIME_PARAMS = dict(ir.IR_DEFAULT_PARAMS)
    ir.INDEX_REGIME_PARAMS["state_dir"] = str(backtest_state_dir)

    dates = _iter_dates(start, end, calendar)
    daily: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for ds in dates:
        try:
            regime, score, ctx = ir.detect_index_regime(as_of=ds, force=True, mode="eod")
            fatal = ctx.get("detail", {}).get("fatal") if isinstance(ctx.get("detail"), dict) else None
            if fatal:
                skipped.append({"date": ds, "reason": str(fatal)})
                continue
            daily.append({
                "date": ds,
                "regime": ctx.get("regime"),
                "regime_name": ctx.get("regime_name"),
                "score": float(ctx.get("score", 0.0)),
                "score_raw": float(ctx.get("score_raw", 0.0)),
                "trend_score": float(ctx.get("trend_score", 0.0)),
                "env_score": float(ctx.get("env_score", 0.0)),
                "days_in_regime": int(ctx.get("days_in_regime", 0) or 0),
                "gate_advice": ctx.get("gate_advice"),
                "degraded": ",".join(ctx.get("degraded") or []),
                "fired_rules": "|".join(ctx.get("detail", {}).get("fired_rules") or []),
                "note": ctx.get("detail", {}).get("state", {}).get("note"),
            })
        except Exception as e:
            skipped.append({"date": ds, "reason": f"{type(e).__name__}: {e}"})

    if not daily:
        raise RuntimeError("no backtest rows produced")

    df = pd.DataFrame(daily)
    segments: List[Dict[str, Any]] = []
    cur: Dict[str, Any] | None = None
    for _, r in df.iterrows():
        if cur is None or r["regime"] != cur["regime"]:
            cur = {
                "regime": r["regime"],
                "regime_name": r["regime_name"],
                "start": r["date"],
                "end": r["date"],
                "days": 1,
                "scores": [float(r["score"])],
            }
            segments.append(cur)
        else:
            cur["end"] = r["date"]
            cur["days"] += 1
            cur["scores"].append(float(r["score"]))
    for s in segments:
        scores = s.pop("scores")
        s["avg_score"] = round(sum(scores) / len(scores), 2)

    summary: Dict[str, Any] = {
        "period": {"start": start, "end": end, "calendar": calendar},
        "evaluated_days": int(len(df)),
        "skipped_days": int(len(skipped)),
        "regime_counts": df["regime"].value_counts().to_dict(),
        "gate_advice_counts": df["gate_advice"].value_counts().to_dict(),
        "avg_score": round(float(df["score"].mean()), 2),
        "median_score": round(float(df["score"].median()), 2),
        "max_score": round(float(df["score"].max()), 2),
        "min_score": round(float(df["score"].min()), 2),
        "avg_trend_score": round(float(df["trend_score"].mean()), 2),
        "avg_env_score": round(float(df["env_score"].mean()), 2),
        "avg_days_in_regime": round(float(df["days_in_regime"].mean()), 2),
        "degraded_days": int(df["degraded"].astype(str).ne("").sum()),
        "seed_files": len(seed_files),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    report_base = out_dir / f"index_regime_backtest_{start}_to_{end}"
    json_path = report_base.with_suffix(".json")
    csv_path = report_base.with_suffix(".csv")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {"summary": summary, "segments": segments, "daily": daily, "skipped": skipped},
            f,
            ensure_ascii=False,
            indent=2,
        )
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    return {
        "summary": summary,
        "segments": segments,
        "daily": daily,
        "skipped": skipped,
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "state_dir": str(backtest_state_dir),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="index_regime 区间回测")
    ap.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="报告输出目录")
    ap.add_argument("--calendar", choices=["bdays", "all"], default="bdays", help="日期日历：工作日或自然日")
    args = ap.parse_args()

    result = run_backtest(args.start, args.end, Path(args.out_dir), args.calendar)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
