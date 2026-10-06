# -*- coding: utf-8 -*-
"""重大指数「是否存在规律」实验（2026-10-06）。

对 9 个宽基/成长指数（长历史）检验三类「规律」：
  A) 斐波那契时间周期：相邻拐点间距 Δ 是否比随机更集中在斐波那契数（含非fib对照位）。
  B) 频谱周期：对数收益去除慢趋势后的周期图主峰，是否显著高于白噪声零假设；主周期是否落在 fib。
  C) 收益自相关：lag 1..20 的 ACF 是否显著（动量/反转）。

零假设统一用**匹配波动的随机游走**（A/C）或**白噪声/相位随机化**（B）。全离线优先：先读
`index_series.json`（若缺则网络拉一次并存本地）。运行：python t_io/validation/fib_time/run_index_experiment.py
"""
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from analysis.divergence import _local_extrema  # noqa: E402

_HERE = Path(__file__).resolve().parent
_SERIES_FP = _HERE / "index_series.json"

FIB = [1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144]
CONTROL = [4, 6, 9, 11, 15, 19, 25, 40, 60, 100]
INDICES = {
    "sh000001": "上证综指", "sz399001": "深证成指", "sz399006": "创业板指",
    "sh000300": "沪深300", "sh000688": "科创50", "sh000905": "中证500",
    "sh000852": "中证1000", "sz399106": "深证综指", "sh000016": "上证50",
}


def load_or_fetch():
    if _SERIES_FP.exists():
        return {k: (np.array(v["close"], float), v["dates"]) for k, v in json.loads(
            _SERIES_FP.read_text(encoding="utf-8")).items()}
    from core.market_data import get_provider
    from datetime import datetime
    prov = get_provider()
    end = datetime.now().strftime("%Y-%m-%d")
    out = {}
    for sym in INDICES:
        try:
            df = prov.index_daily(sym, days=6500, end_date=end)   # 传 end_date ⇒ 不回写共享缓存
            if df is None or df.empty:
                continue
            out[sym] = {"close": [float(x) for x in df["close"]],
                        "dates": [str(x)[:10] for x in df["date"]]}
            print(f"  {sym} {INDICES[sym]}: {len(df)} 根  {out[sym]['dates'][0]}..{out[sym]['dates'][-1]}")
        except Exception as e:
            print(f"  {sym} 拉取失败: {str(e)[:80]}")
    _SERIES_FP.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return {k: (np.array(v["close"], float), v["dates"]) for k, v in out.items()}


def turning_points(closes, n_bars=3):
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


def rate_ratio(real, null, positions, hi=200):
    hr = np.bincount(np.clip(real, 0, hi), minlength=hi + 1).astype(float)
    hn = np.bincount(np.clip(null, 0, hi), minlength=hi + 1).astype(float)
    nr, nn = len(real), len(null)
    logs = []
    for p in positions:
        if p >= len(hr) or p >= len(hn):
            continue
        rr, rn = hr[p] / nr, hn[p] / nn
        if rr > 0 and rn > 0:
            logs.append(math.log(rr / rn))
    return (math.exp(sum(logs) / len(logs)) if logs else float("nan"))


def surrogate(closes, rng):
    r = np.diff(np.log(closes))
    sd = float(np.std(r)) or 1e-4
    steps = rng.normal(0.0, sd, len(closes) - 1)
    p = np.empty(len(closes)); p[0] = closes[0]; p[1:] = closes[0] * np.exp(np.cumsum(steps))
    return p


def test_fib(closes, rng, n_bars=3, n_sur=3):
    seq = turning_points(closes, n_bars)
    dl = deltas_of(seq)
    if len(dl) < 20:
        return None
    sdl = []
    for _ in range(n_sur):
        sdl.extend(deltas_of(turning_points(surrogate(closes, rng), n_bars)))
    return {
        "n": len(dl), "mean": float(np.mean(dl)),
        "fib_ratio": rate_ratio(dl, sdl, FIB),
        "ctl_ratio": rate_ratio(dl, sdl, CONTROL),
    }


def test_spectrum(closes):
    """去慢趋势后周期图：主峰周期 vs 相位随机化零假设。"""
    x = np.log(closes)
    x = x - x.mean()
    # 去极慢趋势（保留周期成分）：减去 ~250 根移动平均
    k = 250
    if len(x) < 3 * k:
        return None
    ma = np.convolve(x, np.ones(k) / k, mode="same")
    d = x - ma
    d = d[k:len(d) - k]
    n = len(d)
    f = np.fft.rfft(d * np.hanning(n))
    P = (np.abs(f) ** 2)[1:]
    freqs = np.fft.rfftfreq(n, d=1.0)[1:]
    periods = 1.0 / freqs
    # 只关心 20~1000 根的周期
    m = (periods >= 20) & (periods <= 1000)
    if not m.any():
        return None
    P_m, per_m = P[m], periods[m]
    pk = int(np.argmax(P_m)); top_period = per_m[pk]; top_power = P_m[pk]
    # 零假设：相位随机化 200 次 → 主峰统计量分布
    rng = np.random.default_rng(7)
    null = []
    F = np.fft.rfft(d)
    for _ in range(200):
        ph = np.angle(F)
        rp = rng.uniform(-math.pi, math.pi, len(ph)); rp[0] = ph[0]
        rs = np.real(np.fft.irfft(np.abs(F) * np.exp(1j * rp), n=n))
        Pf = (np.abs(np.fft.rfft(rs * np.hanning(n))) ** 2)[1:]
        null.append(Pf[m].max())
    null = np.array(null)
    pval = float((np.sum(null >= top_power) + 1) / (len(null) + 1))
    return {"top_period": float(top_period), "p": pval}


def test_acf(closes, max_lag=20):
    r = np.diff(np.log(closes))
    r = r - r.mean()
    n = len(r)
    denom = np.sum(r * r) or 1.0
    acf = [float(np.sum(r[:n - k] * r[k:]) / denom) for k in range(1, max_lag + 1)]
    band = 1.96 / math.sqrt(n)
    return acf, band


def main():
    print("载入指数长历史…")
    series = load_or_fetch()
    print(f"指数 {len(series)} 个\n")
    rng = np.random.default_rng(20261006)

    print("========== A) 斐波那契时间周期（率比：fib位 vs 非fib对照位） ==========")
    print(f"{'指数':<10}{'根数':>6}{'Δ数':>6}{'Δ均值':>7}{'fib率比':>9}{'对照率比':>9}")
    agg_f, agg_c = [], []
    for sym, (closes, _) in series.items():
        r = test_fib(closes, rng)
        if not r:
            continue
        agg_f.append(r["fib_ratio"]); agg_c.append(r["ctl_ratio"])
        print(f"{INDICES[sym]:<10}{len(closes):>6}{r['n']:>6}{r['mean']:>7.1f}"
              f"{r['fib_ratio']:>9.3f}{r['ctl_ratio']:>9.3f}")
    if agg_f:
        print(f"  汇总：fib 率比均值={np.mean(agg_f):.3f}  对照率比均值={np.mean(agg_c):.3f}  "
              f"⇒ {'fib 更高(待考)' if np.mean(agg_f) > np.mean(agg_c) else 'fib 不高于对照 ⇒ 无特异性'}")

    print("\n========== B) 频谱主周期（相位随机化零假设） ==========")
    for sym, (closes, _) in series.items():
        r = test_spectrum(closes)
        if not r:
            continue
        flag = " <fib周期" if any(abs(r["top_period"] - f) / f < 0.05 for f in FIB) else ""
        print(f"{INDICES[sym]:<10} 主周期≈{r['top_period']:7.1f} 根   p={r['p']:.3f}"
              f"  {'显著' if r['p'] < 0.05 else '不显著'}{flag}")

    print("\n========== C) 收益自相关（±1.96/√n 带宽） ==========")
    for sym, (closes, _) in series.items():
        acf, band = test_acf(closes)
        sig = [k + 1 for k, a in enumerate(acf) if abs(a) > band]
        print(f"{INDICES[sym]:<10} lag1={acf[0]:+.3f} lag2={acf[1]:+.3f} lag5={acf[4]:+.3f} "
              f"带宽±{band:.3f}  显著lag={sig if sig else '无'}")


if __name__ == "__main__":
    main()
