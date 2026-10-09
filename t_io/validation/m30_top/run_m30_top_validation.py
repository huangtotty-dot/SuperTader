# -*- coding: utf-8 -*-
"""30min 顶部特征 T1–T4 前瞻有效性验证（2026-10-09，**并行于面板上线**）。

背景：持仓体检表把「风险/提醒」改为 30min 顶部特征 T1–T4（`analysis/m30_features.py`），
owner 选择「先接入显示、并行验证」。本脚本用**离线大面板**回答：这些特征出现后，
未来 k 根 30min bar 的收益/下跌比例是否显著优于**全样本基线**。

面板：`t_io/cache/tushare_mins/*_30min_d540.json`（981 只 × 540 天，2025-03~2026-09）。
口径与面板**完全一致**（直接复用 `m30_features.scan_top_features`，阈值不漂移）。

方法：
  - 逐 (股票, bar) 评估 T1–T4；取**上升沿**（f[i] 且非 f[i-1]）为事件，避免状态型特征重复计数。
  - 前瞻：fwd_ret = close[i+H]/close[i] − 1，H ∈ {4,8,16} 根（≈0.5/1/2 交易日，8 根/日）。
  - 基线：全样本可评估 bar（i≥60 且 i+H<n）的 下跌占比 / 均收益。
  - 判据：lift = P(跌|事件) − P(跌|基线)，z = (p̂−p0)/sqrt(p0(1−p0)/n)。

⚠️ 局限：仅 981 只（幸存者偏差）、单一数据源，结论为**内部参考**，不构成交易依据。

用法：
  python t_io/validation/m30_top/run_m30_top_validation.py            # 全量（~数分钟）
  python t_io/validation/m30_top/run_m30_top_validation.py --limit 60 # 快速抽样
"""
import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[3]
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from analysis import m30_features as mf  # noqa: E402
from analysis import divergence as dv  # noqa: E402

PANEL = BASE / "t_io" / "cache" / "tushare_mins"
OUT_DIR = Path(__file__).resolve().parent
HORIZONS = (4, 8, 16)
KEYS = ("T1", "T2", "T3", "T4", "ANY", "CNT2", "CNT3", "DIV30")


def _load_rows(fp):
    try:
        d = json.loads(Path(fp).read_text(encoding="utf-8"))
        rows = d.get("rows") or []
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").drop_duplicates("time").reset_index(drop=True)
        need = {"open", "high", "low", "close", "volume"}
        return df if need.issubset(df.columns) else None
    except Exception:
        return None


def _trigger_masks(df):
    """返回 {key: bool[n]} 上升沿掩码（DIV30 = 项目口径 30min 顶背离事件点）。"""
    n = len(df)
    feats = mf.scan_top_features(df)
    F = {k: np.zeros(n, bool) for k in ("T1", "T2", "T3", "T4")}
    cnt = np.zeros(n, int)
    for f in feats:
        i = f["index"]
        for k in ("T1", "T2", "T3", "T4"):
            F[k][i] = f[k.lower()]
        cnt[i] = f["count"]
    stairs = {"ANY": cnt >= 1, "CNT2": cnt >= 2, "CNT3": cnt >= 3}
    rising = {}
    for k in ("T1", "T2", "T3", "T4"):
        rising[k] = F[k] & ~np.r_[False, F[k][:-1]]
    for k, s in stairs.items():
        rising[k] = s & ~np.r_[False, s[:-1]]
    # 项目口径顶背离：直接是离散事件点（非状态），不需上升沿
    div = np.zeros(n, bool)
    try:
        for e in dv.detect_divergence_events(df):
            if e.get("type") == "顶":
                div[int(e["index"])] = True
    except Exception:
        pass
    rising["DIV30"] = div
    return rising


def _accumulate(df, acc, base_acc):
    """把单只股票的命中/收益累加进 acc（特征×周期）与 base_acc（基线×周期）。"""
    closes = df["close"].astype(float).values
    n = len(closes)
    masks = _trigger_masks(df)
    start = mf.MIN_BARS
    for H in HORIZONS:
        hi = n - H
        if hi <= start:
            continue
        elig = np.zeros(n, bool)
        elig[start:hi] = True
        fwd = np.full(n, np.nan)
        fwd[start:hi] = closes[start + H:hi + H] / closes[start:hi] - 1.0
        ok = elig & ~np.isnan(fwd)
        b = base_acc[H]
        b["n"] += int(ok.sum())
        b["down"] += int((fwd[ok] < 0).sum())
        b["ret"] += float(fwd[ok].sum())
        for k in KEYS:
            m = masks.get(k)
            if m is None:
                continue
            sel = m & ok
            ne = int(sel.sum())
            if ne == 0:
                continue
            a = acc[k][H]
            a["n"] += ne
            a["down"] += int((fwd[sel] < 0).sum())
            a["ret"] += float(fwd[sel].sum())
            a["dd"] += float(np.minimum(fwd[sel], 0).sum())


def _metrics(acc, base_acc):
    out = {}
    for k in KEYS:
        out[k] = {}
        for H in HORIZONS:
            a = acc[k][H]
            b = base_acc[H]
            nb = max(b["n"], 1)
            p0 = b["down"] / nb
            ne = a["n"]
            if ne == 0:
                out[k][H] = {"n": 0}
                continue
            ph = a["down"] / ne
            se = math.sqrt(max(p0 * (1 - p0), 1e-9) / ne)
            out[k][H] = {
                "n": ne, "hit": round(ph, 4), "base": round(p0, 4),
                "lift": round(ph - p0, 4), "z": round((ph - p0) / se, 2) if se > 0 else None,
                "ret": round(a["ret"] / ne, 5),
                "base_ret": round(b["ret"] / nb, 5),
            }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 只（快速抽样）")
    ap.add_argument("--out", default="", help="报告输出目录（默认本目录）")
    args = ap.parse_args()

    files = sorted(glob.glob(str(PANEL / "*_30min_d540.json")))
    if args.limit:
        files = files[:args.limit]
    if not files:
        print("未找到 *_30min_d540.json 面板"); return

    acc = {k: {H: {"n": 0, "down": 0, "ret": 0.0, "dd": 0.0} for H in HORIZONS} for k in KEYS}
    base_acc = {H: {"n": 0, "down": 0, "ret": 0.0} for H in HORIZONS}
    used = 0
    for i, fp in enumerate(files):
        df = _load_rows(fp)
        if df is None or len(df) < mf.MIN_BARS + max(HORIZONS):
            continue
        try:
            _accumulate(df, acc, base_acc)
            used += 1
        except Exception:
            continue
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{len(files)}")

    m = _metrics(acc, base_acc)
    base = {H: {"n": base_acc[H]["n"], "down": round(base_acc[H]["down"] / max(base_acc[H]["n"], 1), 4),
                "ret": round(base_acc[H]["ret"] / max(base_acc[H]["n"], 1), 5)} for H in HORIZONS}
    summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "universe_files": len(files), "used": used, "horizons": list(HORIZONS),
               "baseline": base, "features": m}

    out_dir = Path(args.out) if args.out else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary_m30_top.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 报告 markdown ----
    LABELS = dict(mf.FEATURE_LABELS)
    LABELS.update({"ANY": "任意≥1", "CNT2": "共振≥2", "CNT3": "共振≥3", "DIV30": "项目口径30m顶背离"})
    L = []
    L.append("# 30min 顶部特征 T1–T4 前瞻有效性验证（2026-10-09）")
    L.append("")
    L.append(f"- 生成时间：{summary['generated_at']}")
    L.append(f"- 面板：`t_io/cache/tushare_mins/*_30min_d540.json`（{used}/{len(files)} 只有效）")
    L.append("- 事件=特征**上升沿**；前瞻 fwd = close[i+H]/close[i]−1（8 根/日）。")
    L.append("- ✅/❌ 判据：lift>0 且 |z|≳2 视为有区分度；否则≈随机（与仓储既有结论一致）。")
    L.append("- ⚠️ 仅 981 只（幸存者偏差），结论为内部参考，非交易依据。")
    L.append("")
    L.append("## 基线（全样本）")
    L.append("")
    L.append("| H(根) | 样本 | 下跌占比 | 均收益 |")
    L.append("|---|---|---|---|")
    for H in HORIZONS:
        b = base[H]
        L.append(f"| {H} | {b['n']} | {b['down']:.1%} | {b['ret']:+.3%} |")
    L.append("")
    for k in KEYS:
        L.append(f"## {k} · {LABELS.get(k, k)}")
        L.append("")
        L.append("| H(根) | 事件数 | 下跌占比 | 基线 | lift | z | 均收益 |")
        L.append("|---|---|---|---|---|---|---|")
        for H in HORIZONS:
            r = m[k][H]
            if not r.get("n"):
                L.append(f"| {H} | 0 | — | — | — | — | — |")
                continue
            flag = "✅" if (r["lift"] > 0 and (r["z"] or 0) >= 2) else ("⚠️" if r["lift"] > 0 else "❌")
            L.append(f"| {H} | {r['n']} | {r['hit']:.1%} | {r['base']:.1%} | "
                     f"{r['lift']:+.2%} {flag} | {r['z']} | {r['ret']:+.3%} |")
        L.append("")
    (out_dir / "报告_m30_top.md").write_text("\n".join(L), encoding="utf-8")
    print(f"完成：used={used}, 报告 → {out_dir/'报告_m30_top.md'}")


if __name__ == "__main__":
    main()
