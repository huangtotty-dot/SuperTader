# -*- coding: utf-8 -*-
"""斐波那契时间周期（Fibonacci time zones）有效性实验（2026-10-06）。

**待验证假设 H1**：相邻摆动拐点之间的间距 Δ（bar 数）比随机更集中在斐波那契数上。
    —— 即「上涨/下跌/震荡腿的长度」是否倾向 1,2,3,5,8,13,21,34,55,89,144。

**零假设**：同长度、匹配每根波动的**随机游走**，跑**同一套**摆动检测 → Δ 的斐波那契命中率。
    —— 用匹配随机游走当对照，而不是「命中率 vs 0」，因为随机序列也会偶然命中。

摆动检测：在**收盘价路径**上复用 `analysis.divergence._local_extrema`（真实与零假设同一口径）。
面板：`t_io/cache/daily_kline/*.json`（本地缓存，**全离线**）。

产出：
  · Δ 的斐波那契命中率：真实 vs 零分布，z 检验 + 95%CI
  · 每只票的「真实命中率 - 其自身代理命中率」配对检验（避免伪重复）
  · 时间前半 / 后半分段，看稳定性
运行：python t_io/validation/fib_time/run_experiment.py [--codes 2000] [--tol 1]
"""
import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import numpy as np

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from analysis.divergence import _local_extrema  # noqa: E402

_DAILY_DIR = _ROOT / "t_io" / "cache" / "daily_kline"
FIB = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144]


def load_series(max_codes, min_bars):
    """读本地日线缓存 → [(code, closes, dates)]。"""
    files = sorted(_DAILY_DIR.glob("[0-9]*.json"))
    random.Random(20261006).shuffle(files)
    out = []
    for fp in files:
        if len(out) >= max_codes:
            break
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
            rows = d.get("rows") or []
            if len(rows) < min_bars:
                continue
            closes = np.array([float(r["close"]) for r in rows], dtype=float)
            dates = [str(r["date"]) for r in rows]
            if (closes <= 0).any():
                continue
            out.append((fp.stem, closes, dates))
        except Exception:
            continue
    return out


def turning_points(closes, n_bars=3):
    """收盘价路径上的拐点索引（交替 H/L，去相邻同类只留极值者）。"""
    peaks, troughs = _local_extrema(closes, closes, n_bars)
    piv = sorted([(i, "H") for i in peaks] + [(i, "L") for i in troughs])
    seq = []
    for idx, kind in piv:
        px = closes[idx]
        if seq and seq[-1][1] == kind:
            if (px > seq[-1][2]) if kind == "H" else (px < seq[-1][2]):
                seq[-1] = (idx, kind, px)
            continue
        seq.append((idx, kind, px))
    return seq


def deltas_of(seq):
    return [seq[i + 1][0] - seq[i][0] for i in range(len(seq) - 1)]


def fib_hit(deltas, tol):
    """Δ 落在某个斐波那契数 ±tol 内的比例。"""
    if not deltas:
        return None
    hit = sum(1 for x in deltas if any(abs(x - f) <= tol for f in FIB))
    return hit / len(deltas)


def surrogate_closes(closes, rng):
    """匹配每根对数收益波动的随机游走（零漂移），同长度同起点。"""
    r = np.diff(np.log(closes))
    sd = float(np.std(r)) or 1e-4
    steps = rng.normal(0.0, sd, size=len(closes) - 1)
    path = np.empty(len(closes))
    path[0] = closes[0]
    path[1:] = closes[0] * np.exp(np.cumsum(steps))
    return path


def z_test(p1, n1, p0, n0):
    """两比例 z 检验（观测命中率 p1 vs 零假设命中率 p0）。"""
    if n1 == 0 or n0 == 0:
        return 0.0, 1.0
    se = math.sqrt(p0 * (1 - p0) / n1 + p0 * (1 - p0) / n0) or 1e-9
    z = (p1 - p0) / se
    p = 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))
    return z, p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", type=int, default=2000)
    ap.add_argument("--min-bars", type=int, default=500)
    ap.add_argument("--tol", type=int, default=1, help="斐波那契数容差（bar）")
    ap.add_argument("--n-bars", type=int, default=3, help="分形确认根数")
    ap.add_argument("--surrogates", type=int, default=3, help="每只票的随机游走条数")
    args = ap.parse_args()

    print(f"载入面板（codes<={args.codes}, min_bars>={args.min_bars}）…")
    series = load_series(args.codes, args.min_bars)
    print(f"  实际标的数：{len(series)}")

    rng_global = np.random.default_rng(20261006)
    real_deltas, null_deltas = [], []
    per_code = []            # (真实命中率, 代理命中率, 真实Δ数)
    real_deltas_early, real_deltas_late = [], []

    for k, (code, closes, dates) in enumerate(series):
        seq = turning_points(closes, args.n_bars)
        dl = deltas_of(seq)
        if len(dl) < 5:
            continue
        real_deltas.extend(dl)
        # 时间分段（用拐点日期）
        mid = len(closes) // 2
        dl_early = [seq[i + 1][0] - seq[i][0] for i in range(len(seq) - 1) if seq[i][0] < mid]
        dl_late = [seq[i + 1][0] - seq[i][0] for i in range(len(seq) - 1) if seq[i][0] >= mid]
        real_deltas_early.extend(dl_early)
        real_deltas_late.extend(dl_late)
        # 代理
        sdl = []
        for _ in range(args.surrogates):
            sp = surrogate_closes(closes, rng_global)
            sseq = turning_points(sp, args.n_bars)
            sdl.extend(deltas_of(sseq))
        null_deltas.extend(sdl)
        if sdl:
            per_code.append((fib_hit(dl, args.tol), fib_hit(sdl, args.tol), len(dl)))

    CONTROL = [4, 6, 9, 11, 15, 19, 25, 40, 60, 100]   # 非斐波那契对照位

    def ratio_at(real, null, positions, hi=200):
        """逐位置 p 的 (真实率 / 零假设率) 的几何均值——与位置密度无关，只看「真实相对随机在
        该位置是否系统性偏多」。"""
        h_r = np.bincount(np.clip(real, 0, hi), minlength=hi + 1).astype(float)
        h_n = np.bincount(np.clip(null, 0, hi), minlength=hi + 1).astype(float)
        nr, nn = len(real), len(null)
        logs = []
        for p in positions:
            if p >= len(h_r) or p >= len(h_n):
                continue
            rr, rn = h_r[p] / nr, h_n[p] / nn
            if rr > 0 and rn > 0:
                logs.append(math.log(rr / rn))
        return (math.exp(sum(logs) / len(logs)) if logs else float("nan")), len(logs)

    p1 = fib_hit(real_deltas, args.tol)
    p0 = fib_hit(null_deltas, args.tol)
    n1, n0 = len(real_deltas), len(null_deltas)
    z, p = z_test(p1, n1, p0, n0)
    se1 = math.sqrt(p1 * (1 - p1) / n1)
    print("\n============ 粗口径（Δ 落在 fib±tol，天花板效应大，仅参考） ============")
    print(f"真实 p1={p1:.4f}(n={n1})  零假设 p0={p0:.4f}(n={n0})  Δp={(p1-p0)*100:+.2f}pp  "
          f"z={z:+.2f} p={p:.4f}")
    print(f"Δ 均值 真实={np.mean(real_deltas):.2f}  零假设={np.mean(null_deltas):.2f}  "
          f"中位 真实={np.median(real_deltas):.0f}")

    rf, kf = ratio_at(real_deltas, null_deltas, FIB)
    rc, kc = ratio_at(real_deltas, null_deltas, CONTROL)
    NONFIB = [p for p in range(1, 61) if p not in FIB]
    rnf, knf = ratio_at(real_deltas, null_deltas, NONFIB)
    print("\n============ 关键检验：真实/随机的「率比」 —— fib 位 是否系统性偏高 ============")
    print(f"斐波那契位 率比={rf:.3f} (k={kf})")
    print(f"非fib对照位 率比={rc:.3f} (k={kc})")
    print(f"全部非fib位 率比={rnf:.3f} (k={knf})   ← 这是基线：若 fib 无特异性，rf 应≈此值")
    print("判读：fib 率比 ≈ 非fib 率比 ⇒ 无斐波那契特异性；fib 明显更高才有。")

    pe = fib_hit(real_deltas_early, args.tol)
    pl = fib_hit(real_deltas_late, args.tol)
    print("\n------ 时间分段（粗口径命中率） ------")
    print(f"前半 {pe:.4f}(n={len(real_deltas_early)})  后半 {pl:.4f}(n={len(real_deltas_late)})  "
          f"差={((pl or 0) - (pe or 0)) * 100:+.2f}pp")

    if per_code:
        diffs = np.array([a - b for a, b, _ in per_code if a is not None and b is not None])
        n = len(diffs)
        mean = float(np.mean(diffs))
        sd = float(np.std(diffs, ddof=1)) if n > 1 else 0.0
        t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
        win = int(np.sum(diffs > 0))
        print("\n------ 逐票配对（真实命中率 减 代理命中率） ------")
        print(f"样本 {n} 只  均值={mean*100:+.2f}pp  t={t:+.2f}  真实更高者 {win}/{n}={win/n*100:.1f}%")

    print("\n------ Δ 分布（真实 vs 零假设，前 40 根） ------")
    hr = np.bincount(np.clip(real_deltas, 0, 40), minlength=41)
    hn = np.bincount(np.clip(null_deltas, 0, 40), minlength=41)
    for lo in range(1, 41):
        flag = " <fib" if lo in FIB else ""
        br = "#" * int(round(hr[lo] / max(1, hr.max()) * 24))
        bn = "." * int(round(hn[lo] / max(1, hn.max()) * 24))
        print(f"{lo:3d} R{hr[lo]:6d} {br:<24s} N{hn[lo]:6d} {bn:<24s}{flag}")


if __name__ == "__main__":
    main()
