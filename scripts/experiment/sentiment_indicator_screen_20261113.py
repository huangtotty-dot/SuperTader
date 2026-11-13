# -*- coding: utf-8 -*-
"""市场情绪指标筛选（2026-11-13）。

问题：「站上20线公司占比」下面要加一块「市场情绪」，用哪个指标？
做法：同花顺涨跌停/炸板缓存（`t_io/index_regime/e5_ths_cache.json`，328 个交易日）
      × 全 A 等权日收益（本地日线缓存自算），检验候选指标对未来 1/3/5/10/20 日
      等权市场收益的排序能力与分位表现；再做残差 IC（扣掉当日涨跌）与稳健性。

结论（详见 doc/research/2026-11-13_市场情绪指标筛选.md）：
  · 涨停家数 / 触板总数 对**未来 20 日**负相关（rank IC −0.16~−0.26，两年同向），
    扣掉当日涨跌后更强 ⇒ 不是日内反转的换皮；
  · 炸板率单指标无信息；三项简单合成反而洗掉信号；
  · 5 日尺度无信号；非重叠样本仅 16 点（不显著）⇒ 只作情绪温度计，不做交易信号。

运行：python scripts/experiment/sentiment_indicator_screen_20261113.py
数据全本地，零网络；约 1 分钟（读 5500 个日线缓存）。
"""
import json
import pathlib
import sys

import numpy as np

# Windows 终端默认 GBK：输出里的 ⇒ / ★ 会 UnicodeEncodeError（与 gui.py 同一处理）
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = pathlib.Path(__file__).resolve().parents[2]
DAILY = BASE / "t_io" / "cache" / "daily_kline"
E5 = BASE / "t_io" / "index_regime" / "e5_ths_cache.json"


def _is_stock(c):
    c = str(c)
    if len(c) != 6 or not c.isdigit() or c[0] in "15":
        return False
    if c[:2] in ("51", "56", "58", "50", "52", "11", "12", "13"):
        return False
    return c[:2] in ("60", "00", "30", "68") or c[0] in "48" or c[:3] == "920"


def load_market():
    """→ dates(升序交易日), ew(当日全 A 等权收益)。剔除复权/新股伪影（|pct|>50%）。"""
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
    return dates, np.array([np.mean(per[d]) for d in dates])


def fwd(ew, i, h):
    return float(np.prod(1 + ew[i + 1:i + 1 + h]) - 1) if i + h < len(ew) else np.nan


def rank(x):
    r = np.argsort(np.argsort(np.asarray(x, float))).astype(float)
    return (r - r.mean()) / (r.std() or 1)


def resid(y, x):
    x = np.column_stack([np.ones(len(x)), x])
    return y - x @ np.linalg.lstsq(x, y, rcond=None)[0]


def spearman(a, b):
    a = np.asarray(a, float); b = np.asarray(b, float)
    m = ~(np.isnan(a) | np.isnan(b))
    if m.sum() < 30:
        return np.nan
    ra, rb = rank(a[m]), rank(b[m])
    return float((ra * rb).sum() / len(ra))


def tstat(x):
    x = np.asarray(x, float); x = x[~np.isnan(x)]
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 4 else np.nan


def qspread(xs, ys, q=(0.2, 0.8)):
    m = ~(np.isnan(xs) | np.isnan(ys))
    a, y = np.asarray(xs)[m], np.asarray(ys)[m]
    lo, hi = a <= np.quantile(a, q[0]), a >= np.quantile(a, q[1])
    d = y[hi].mean() - y[lo].mean()
    se = np.sqrt(y[hi].var(ddof=1) / hi.sum() + y[lo].var(ddof=1) / lo.sum())
    return d, (d / se if se else np.nan), int(len(a))


def main():
    dates, ew = load_market()
    sent = json.load(open(E5, encoding="utf-8"))
    S = {f"{k[:4]}-{k[4:6]}-{k[6:]}": v for k, v in sent.items()}
    both = [d for d in dates if d in S]
    idx = {d: i for i, d in enumerate(dates)}
    zt = np.array([S[d]["zt_count"] for d in both], float)
    dt_ = np.array([S[d]["dt_count"] for d in both], float)
    zb = np.array([(S[d]["zb_rate"] or 0) for d in both], float)
    touch = np.array([S[d]["zt_touched"] or 0 for d in both], float)
    print(f"样本 {len(both)} 天 {both[0]} → {both[-1]}；市场等权日收益 均值 {ew.mean()*100:.3f}% "
          f"年化波动 {ew.std()*np.sqrt(244)*100:.1f}%")

    def rollz(x, w=60):
        out = np.full(len(x), np.nan)
        for j in range(len(x)):
            seg = x[max(0, j - w + 1):j + 1]
            if len(seg) >= 20 and seg.std() > 0:
                out[j] = (x[j] - seg.mean()) / seg.std()
        return out

    table = {"涨停家数": zt, "跌停家数": dt_, "净涨停": zt - dt_, "触板总数": touch,
             "炸板率": zb, "涨停z(60)": rollz(zt), "触板z(60)": rollz(touch),
             "合成温度": rollz(zt) - rollz(dt_) - 0.5 * rollz(zb)}

    print("\n=== Spearman IC（指标 vs 未来 H 日等权收益）===")
    print(f"{'指标':<12}" + "".join(f"{'H=' + str(h):>9}" for h in (1, 3, 5, 10, 20)))
    fwd_by_h = {h: np.array([fwd(ew, idx[d], h) for d in both]) for h in (1, 3, 5, 10, 20)}
    for name, xs in table.items():
        print(f"{name:<12}" + "".join(f"{spearman(xs, fwd_by_h[h]):>9.3f}" for h in (1, 3, 5, 10, 20)))

    print("\n=== H=20 分位（5 组）等权收益 ===")
    for name in ("涨停家数", "触板总数", "净涨停", "炸板率", "合成温度"):
        xs, f = table[name], fwd_by_h[20]
        m = ~(np.isnan(xs) | np.isnan(f))
        a, y = xs[m], f[m]
        g = np.digitize(a, np.quantile(a, [0.2, 0.4, 0.6, 0.8]))
        line = " ".join(f"Q{k+1}:{y[g == k].mean()*100:+.2f}%(t={tstat(y[g == k]):+.1f})" for k in range(5))
        d, t, n = qspread(xs, f)
        print(f"{name:<10} {line}  ⇒ Q5-Q1 {d*100:+.2f}pp t={t:+.2f} (n={n})")

    print("\n=== 残差 IC：扣掉当日等权收益后还剩多少信息（H=5）===")
    today = np.array([ew[idx[d]] for d in both])
    for name in ("涨停家数", "触板总数", "炸板率", "合成温度"):
        xs = table[name]
        m = ~(np.isnan(xs) | np.isnan(fwd_by_h[5]))
        raw = spearman(xs[m], fwd_by_h[5][m])
        rx = resid(rank(xs[m]), today[m][:, None])
        ry = resid(rank(fwd_by_h[5][m]), today[m][:, None])
        print(f"{name:<10} 原始 {raw:+.3f}  扣当日涨跌后 {(rank(rx) * rank(ry)).sum() / len(rx):+.3f}")

    print("\n=== 稳健性（H=20 的 Q5-Q1）===")
    half = len(both) // 2
    for name in ("涨停家数", "触板总数"):
        xs, f = table[name], fwd_by_h[20]
        a, ta, _ = qspread(xs[:half], f[:half])
        b, tb, _ = qspread(xs[half:], f[half:])
        hi, lo = np.quantile(xs, 0.95), np.quantile(xs, 0.05)
        m = (xs <= hi) & (xs >= lo)
        c, tc, _ = qspread(xs[m], f[m])
        d, td, n = qspread(xs[::20], f[::20])
        print(f"{name:<8} 前半 {a*100:+.2f}pp(t={ta:+.2f}) | 后半 {b*100:+.2f}pp(t={tb:+.2f}) | "
              f"截尾 {c*100:+.2f}pp(t={tc:+.2f}) | 非重叠(每20日) {d*100:+.2f}pp(t={td:+.2f}, n={n})")

    print("\n=== 极端日：涨停家数 60 日 z ≤ −1.5（冰点）vs ≥ 1.5（高潮）===")
    z = rollz(zt)
    for lo, hi, tag in ((-99, -1.5, "冰点"), (1.5, 99, "高潮")):
        m = (z >= lo) & (z <= hi) & ~np.isnan(z)
        if m.sum() < 5:
            print(f"{tag}: n={int(m.sum())} 样本不足")
            continue
        out = []
        for H in (1, 5, 20):
            v = fwd_by_h[H][m]
            v = v[~np.isnan(v)]
            out.append(f"H={H}: {v.mean()*100:+.2f}%(t={tstat(v):+.2f})")
        print(f"{tag} n={int(m.sum())}  " + "  ".join(out))


if __name__ == "__main__":
    sys.exit(main())
