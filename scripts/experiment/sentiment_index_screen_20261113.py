# -*- coding: utf-8 -*-
"""情绪「合成指数」定权研究（2026-11-13，第二轮）。

owner：「最后根据加权合成一个指数，因为我看那么多指标容易乱，再仔细研究下」。

第一轮（sentiment_indicator_screen_20261113.py）结论：所有简单合成都不如单看
「触板总数」（H=20 IC −0.259）。这一轮回答：如果一定要合成，怎么定权、样本外稳不稳、
以及"单看最强项 vs 合成"到底差多少。

结论：合成 = 可读性工具，不是更好的信号（触板 3.35pp → 合成 1.39pp）。
生产权重取 0.5 触板 / 0.3 封板质量 / 0.2 跌停（见 t_gui.load_market_sentiment）。
"""
import json
import pathlib
import sys

import numpy as np

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = next(p for p in pathlib.Path(__file__).resolve().parents if (p / "t_io").is_dir())
DAILY = BASE / "t_io" / "cache" / "daily_kline"
E5 = BASE / "t_io" / "index_regime" / "e5_ths_cache.json"
CACHE = BASE / "tmp" / "_mkt_series.json"   # 扫描缓存（删掉即重算）


def _is_stock(c):
    c = str(c)
    if len(c) != 6 or not c.isdigit() or c[0] in "15":
        return False
    if c[:2] in ("51", "56", "58", "50", "52", "11", "12", "13"):
        return False
    return c[:2] in ("60", "00", "30", "68") or c[0] in "48" or c[:3] == "920"


def load_market():
    if CACHE.exists():
        d = json.loads(CACHE.read_text(encoding="utf-8"))
        return d["dates"], np.array(d["ew"]), np.array(d["adv"])
    per = {}
    for p in DAILY.glob("*.json"):
        if not _is_stock(p.stem):
            continue
        try:
            rows = json.loads(p.read_text(encoding="utf-8")).get("rows") or []
        except Exception:
            continue
        prev = None
        for r in rows[-430:]:
            try:
                c = float(r.get("close") or 0)
            except (TypeError, ValueError):
                prev = None
                continue
            if prev and prev > 0 and c > 0:
                pct = c / prev - 1
                if -0.5 < pct < 1.0:
                    per.setdefault(str(r.get("date"))[:10], []).append(pct)
            prev = c
    dates = sorted(d for d in per if len(per[d]) >= 300)
    ew = [float(np.mean(per[d])) for d in dates]
    adv = [float(np.mean(np.array(per[d]) > 0)) for d in dates]
    CACHE.write_text(json.dumps({"dates": dates, "ew": ew, "adv": adv}), encoding="utf-8")
    return dates, np.array(ew), np.array(adv)


def fwd(ew, i, h):
    return float(np.prod(1 + ew[i + 1:i + 1 + h]) - 1) if i + h < len(ew) else np.nan


def rank(x):
    r = np.argsort(np.argsort(np.asarray(x, float))).astype(float)
    return (r - r.mean()) / (r.std() or 1)


def spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = ~(np.isnan(a) | np.isnan(b))
    if m.sum() < 30:
        return np.nan
    ra, rb = rank(a[m]), rank(b[m])
    return float((ra * rb).sum() / len(ra))


def qspread(xs, ys):
    m = ~(np.isnan(xs) | np.isnan(ys))
    a, y = np.asarray(xs)[m], np.asarray(ys)[m]
    lo, hi = a <= np.quantile(a, 0.2), a >= np.quantile(a, 0.8)
    d = y[hi].mean() - y[lo].mean()
    se = np.sqrt(y[hi].var(ddof=1) / hi.sum() + y[lo].var(ddof=1) / lo.sum())
    return d, (d / se if se else np.nan)


def pct_rank(x, w=60):
    out = np.full(len(x), np.nan)
    for j in range(len(x)):
        seg = x[max(0, j - w + 1):j + 1]
        if len(seg) >= 20:
            out[j] = float(np.sum(seg <= x[j]) / len(seg))
    return out


def main():
    dates, ew, adv = load_market()
    sent = json.load(open(E5, encoding="utf-8"))
    S = {f"{k[:4]}-{k[4:6]}-{k[6:]}": v for k, v in sent.items()}
    both = [d for d in dates if d in S]
    idx = {d: i for i, d in enumerate(dates)}
    zt = np.array([S[d]["zt_count"] for d in both], float)
    dt_ = np.array([S[d]["dt_count"] for d in both], float)
    zb = np.array([(S[d]["zb_rate"] or 0) for d in both], float)
    touch = np.array([S[d]["zt_touched"] or 0 for d in both], float)
    ad = np.array([adv[idx[d]] for d in both])
    f20 = np.array([fwd(ew, idx[d], 20) for d in both])
    p_zt, p_dt, p_zb, p_touch, p_ad = (pct_rank(v) for v in (zt, dt_, zb, touch, ad))

    def mk(*pairs):
        tot = sum(w for _, w in pairs)
        mat = np.column_stack([np.nan_to_num(x, nan=0.5) for x, _ in pairs])
        return 100.0 * (mat @ np.array([w for _, w in pairs])) / tot

    cands = {
        "① 触板总数（最优单指标）": mk((p_touch, 1)),
        "② 涨停+封板质量": mk((p_zt, 2), (1 - p_zb, 1)),
        "③ 涨停+炸板+跌停(4:3:3)": mk((p_zt, 4), (1 - p_zb, 3), (1 - p_dt, 3)),
        "④ 触板+炸板+跌停(5:3:2)": mk((p_touch, 5), (1 - p_zb, 3), (1 - p_dt, 2)),
        "⑤ 四项等权": mk((p_zt, 1), (1 - p_dt, 1), (1 - p_zb, 1), (p_ad, 1)),
        "⑥ 纯涨停分位": mk((p_zt, 1)),
    }
    print(f"样本 {len(both)} 天 {both[0]} → {both[-1]}")
    print(f"\n{'构造':<24}{'IC(H=20)':>10}{'Q5-Q1':>12}{'t':>7}   最新值")
    for name, ix in cands.items():
        d, t = qspread(ix, f20)
        print(f"{name:<24}{spearman(ix, f20):>10.3f}{d*100:>10.2f}pp{t:>7.2f}   {ix[-1]:5.1f}")

    # 走查：不用任何未来信息 —— 指数权重全固定，这里只算「滚动样本外」的 IC（其实权重固定=全样本可用不同期）
    print("\n=== 分半窗（前后各 164 天）===")
    half = len(both) // 2
    for name, ix in cands.items():
        d1, t1 = qspread(ix[:half], f20[:half])
        d2, t2 = qspread(ix[half:], f20[half:])
        print(f"{name:<24} 前半 IC {spearman(ix[:half], f20[:half]):+.3f} ({d1*100:+.2f}pp t={t1:+.2f})"
              f" | 后半 IC {spearman(ix[half:], f20[half:]):+.3f} ({d2*100:+.2f}pp t={t2:+.2f})")

    # 极端段：指数最低 20% / 最高 20% 的后 20 日
    print("\n=== 指数极端分位（H=20）===")
    for name in ("① 触板总数（最优单指标）", "④ 触板+炸板+跌停(5:3:2)", "⑤ 四项等权"):
        ix = cands[name]
        m = ~np.isnan(ix) & ~np.isnan(f20)
        a = ix[m]
        lo, hi = np.quantile(a, 0.2), np.quantile(a, 0.8)
        lowv = f20[m][a <= lo]; highv = f20[m][a >= hi]
        print(f"{name:<24} 冰点(≤{lo:.0f}) {lowv.mean()*100:+.2f}% (n={len(lowv)})"
              f" | 高潮(≥{hi:.0f}) {highv.mean()*100:+.2f}% (n={len(highv)})")

    # 指数与分量相关
    print("\n=== ④ 与分量相关性 ===")
    ix = cands["④ 触板+炸板+跌停(5:3:2)"]
    for n, v in (("涨停", p_zt), ("触板", p_touch), ("炸板率", p_zb), ("跌停", p_dt), ("上涨占比", p_ad)):
        print(f"  {n:<6} {spearman(ix, v):+.2f}")

    # 最近 10 天
    print("\n=== 最近 10 天（④）===")
    for j in range(len(both) - 10, len(both)):
        print(f"  {both[j]}  指数 {cands['④ 触板+炸板+跌停(5:3:2)'][j]:5.1f}"
              f"   涨停{int(zt[j]):3d} 跌停{int(dt_[j]):3d} 炸板{zb[j]*100:5.1f}% 触板{int(touch[j]):3d}")


if __name__ == "__main__":
    sys.exit(main())
