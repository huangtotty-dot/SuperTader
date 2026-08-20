# -*- coding: utf-8 -*-
"""C20 竞价背离离线统计（周六评审预研第2项）

目的：为"竞价缺口纳入早盘基调"的阈值定稿提供数据。
口径：
  - 每日持仓 open_gap 中位数（来自 t_io/preopen/preopen_YYYY-MM-DD.json，
    code_snapshots[code].open_gap/prev_close/price；剔除 open_gap==0 且
    price==prev_close 的疑似缺数行；当日全缺则跳过）
  - top20 字段（仅 08-19 起新口径文件有，有则记录）
  - 早盘基调 regime + S 值（t_io/logs/t_trader_sys_YYYY-MM-DD.log 中
    "早盘大盘基调已推送: {regime} S={score}" 行）
  - 当日实际涨跌：上证指数（腾讯接口 sh000001）收盘价涨跌幅 %
判定规则（按 C20 设计稿）：
  前提 = 基调偏多（单边上涨 / 强势 / 震荡偏强 等含"涨/强"字样）
  Level1 = 持仓缺口中位数 <= -1.0%  或 top20 偏空(top20_bias<0)
  Level2 = 持仓缺口中位数 <= -2.5%  或（top20 强偏空 且 持仓缺口中位数<0）
输出：统计汇总 + 逐日明细，写 doc/每日复盘/C20_竞价背离离线统计_20260820.md
"""
import json
import re
import sys
import urllib.request
from pathlib import Path

BASE = Path(r"E:\superTrader")
PREOPEN_DIR = BASE / "t_io" / "preopen"
LOG_DIR = BASE / "t_io" / "logs"
OUT_MD = BASE / "doc" / "每日复盘" / "C20_竞价背离离线统计_20260820.md"

BULLISH = ("单边上涨", "强势", "震荡偏强", "偏多")


def fetch_index_daily(symbol="sh000001") -> dict:
    """腾讯指数日线，返回 {date: close}"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "all_proxy"):
        import os
        os.environ.pop(k, None)
    url = f"https://ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,800,qfq"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://finance.qq.com/"})
    raw = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="ignore")
    data = json.loads(raw)
    node = data.get("data", {}).get(symbol, {})
    kline = node.get("day") or node.get("qfqday") or []
    return {i[0]: {"open": float(i[1]), "close": float(i[2])} for i in kline if len(i) >= 3}


def parse_auction(fp: Path):
    """auction_YYYY-MM-DD.json：取最后一个时段快照的 pct_vs_preclose 中位数(%)。
    返回 (median_pct, slot, n) 或 None"""
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None
    snaps = d.get("snapshots") or {}
    if not snaps:
        return None
    slot = sorted(snaps.keys())[-1]
    rows = snaps[slot].get("rows") or {}
    vals = []
    for c, r in rows.items():
        v = r.get("pct_vs_preclose")
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 == 1 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return med, slot, n


def parse_top20_from_log(fp: Path):
    """日志中 '竞价额Top20：涨X/跌Y' 行：取 09:24-09:32 窗口内最后一条。
    返回 (up, down, time_str) 或 None"""
    try:
        txt = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    best = None
    for m in re.finditer(r"(\d{2}:\d{2}):\d{2}\s*\[INFO\]\s*竞价额Top20：涨(\d+)/跌(\d+)", txt):
        t = m.group(1)
        if "09:24" <= t <= "09:32":
            best = (int(m.group(2)), int(m.group(3)), t)
    return best


def parse_preopen(fp: Path):
    """返回 (gap_median_pct, n_valid, n_total, top20_bias_or_None)
    open_gap 为小数（0.0558=5.58%），此处转成百分比。
    剔除 prev_close<=0 的缺数行（7月老文件常见 prev=0.0 导致 gap=0）。"""
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return None
    snaps = d.get("code_snapshots") or {}
    gaps = []
    for c, s in snaps.items():
        gap = s.get("open_gap")
        prev = s.get("prev_close")
        if gap is None or prev is None:
            continue
        try:
            prev = float(prev)
        except (TypeError, ValueError):
            continue
        if prev <= 0:
            continue  # 缺前收 → gap 不可信
        gaps.append(float(gap) * 100.0)
    if not gaps:
        return None
    gaps.sort()
    n = len(gaps)
    med = gaps[n // 2] if n % 2 == 1 else (gaps[n // 2 - 1] + gaps[n // 2]) / 2
    top20_bias = None
    auction = d.get("auction_summary") or {}
    if "top20_bias" in auction:
        _bias_map = {"strong_bearish": -2.0, "bearish": -1.0,
                     "neutral": 0.0, "bullish": 1.0, "strong_bullish": 2.0}
        _v = auction.get("top20_bias")
        if isinstance(_v, str):
            top20_bias = _bias_map.get(_v.strip().lower())
        else:
            try:
                top20_bias = float(_v)
            except (TypeError, ValueError):
                top20_bias = None
    elif "strong_open_count" in auction:
        # 老口径：用 strong-weak 占 snapshot 比例作偏空/偏多代理
        try:
            sc = float(auction.get("strong_open_count") or 0)
            wc = float(auction.get("weak_open_count") or 0)
            tot = float(auction.get("snapshot_count") or len(snaps) or 1)
            top20_bias = (sc - wc) / tot
        except (TypeError, ValueError):
            top20_bias = None
    return med, n, len(snaps), top20_bias


def parse_regime(fp: Path):
    """从日志抓 '早盘大盘基调已推送: {regime} S={score}'，返回 (regime, score)"""
    try:
        txt = fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    m = re.search(r"早盘大盘基调已推送[:：]\s*(\S+)\s*S=([-\d.]+)", txt)
    if not m:
        return None
    return m.group(1), float(m.group(2))


def main():
    index = fetch_index_daily("sh000001")
    # 日期全集 = preopen ∪ auction
    dates = sorted({fp.stem.replace("preopen_", "") for fp in PREOPEN_DIR.glob("preopen_*.json")}
                   | {fp.stem.replace("auction_", "") for fp in PREOPEN_DIR.glob("auction_*.json")})
    rows = []
    for date in dates:
        po_fp = PREOPEN_DIR / f"preopen_{date}.json"
        au_fp = PREOPEN_DIR / f"auction_{date}.json"
        po = parse_preopen(po_fp) if po_fp.exists() else None
        au = parse_auction(au_fp) if au_fp.exists() else None
        if not po and not au:
            continue
        # 缺口主源：auction 末时段快照（真实撮合价）；否则 preopen code_snapshots
        if au:
            med, n_valid, n_total, gap_src = au[0], au[2], au[2], f"auction@{au[1]}"
            top20_bias = po[3] if po else None
        else:
            med, n_valid, n_total, top20_bias = po
            gap_src = "preopen"
        log_fp = LOG_DIR / f"t_trader_sys_{date}.log"
        rg = parse_regime(log_fp) if log_fp.exists() else None
        regime, score = rg if rg else (None, None)
        # top20 补充源：日志 09:24-09:32 的 Top20 涨跌数 → bias 等级
        top20_log = parse_top20_from_log(log_fp) if log_fp.exists() else None
        if top20_bias is None and top20_log:
            up, down, _t = top20_log
            tot = up + down
            if tot >= 10:
                r = (up - down) / tot
                top20_bias = 2.0 if r >= 0.6 else 1.0 if r >= 0.2 else \
                    -2.0 if r <= -0.6 else -1.0 if r <= -0.2 else 0.0
        idx = index.get(date)
        actual = None
        if idx:
            dts = sorted(index.keys())
            i = dts.index(date)
            if i > 0:
                prev_close = index[dts[i - 1]]["close"]
                actual = (idx["close"] / prev_close - 1) * 100
        rows.append({
            "date": date, "gap_med": med, "n_valid": n_valid, "n_total": n_total,
            "gap_src": gap_src, "top20_bias": top20_bias,
            "top20_log": top20_log,
            "regime": regime, "score": score, "actual_pct": actual,
        })

    def is_bullish(r):
        return r["regime"] and any(k in r["regime"] for k in BULLISH)

    def lv1(r):
        if r["gap_med"] <= -1.0:
            return True
        if r["top20_bias"] is not None and r["top20_bias"] < 0:
            return True
        return False

    def lv2(r):
        if r["gap_med"] <= -2.5:
            return True
        if (r["top20_bias"] is not None and r["top20_bias"] <= -0.5
                and r["gap_med"] < 0):
            return True
        return False

    valid = [r for r in rows if r["actual_pct"] is not None]
    down_days = [r for r in valid if r["actual_pct"] < 0]
    baseline = len(down_days) / len(valid) * 100 if valid else 0

    bull = [r for r in valid if is_bullish(r)]
    def hit_stats(pred, pool):
        hits = [r for r in pool if pred(r)]
        dn = [r for r in hits if r["actual_pct"] < 0]
        return hits, (len(dn) / len(hits) * 100 if hits else None)

    l1_hits, l1_down = hit_stats(lv1, bull)
    l2_hits, l2_down = hit_stats(lv2, bull)

    # 备选阈值扫描（持仓缺口单边触发，看多基调池）
    scan = {}
    for th in (-0.5, -1.0, -1.5, -2.0, -2.5):
        hits = [r for r in bull if r["gap_med"] <= th]
        dn = [r for r in hits if r["actual_pct"] < 0]
        scan[th] = (len(hits), (len(dn) / len(hits) * 100 if hits else None))

    # 输出
    L = []
    L.append("# C20 竞价背离离线统计（2026-08-20 生成）\n")
    L.append(f"- 样本：{len(rows)} 个交易日（preopen 可得），其中 {len(valid)} 日有当日涨跌数据")
    L.append(f"- 全样本当日下跌占比（基线）：{baseline:.1f}%（{len(down_days)}/{len(valid)}）")
    L.append(f"- 看多基调日（{ '/'.join(BULLISH) }）：{len(bull)} 日\n")
    L.append("## 设计稿规则命中\n")
    L.append(f"- Level1 命中 {len(l1_hits)} 日，当日下跌占比 "
             f"{f'{l1_down:.0f}%' if l1_down is not None else 'NA'}")
    L.append(f"- Level2 命中 {len(l2_hits)} 日，当日下跌占比 "
             f"{f'{l2_down:.0f}%' if l2_down is not None else 'NA'}\n")
    L.append("## 持仓缺口阈值扫描（看多基调日，单边触发）\n")
    L.append("| 阈值 | 命中日数 | 当日下跌占比 |")
    L.append("|---|---|---|")
    for th, (nh, pd_) in scan.items():
        L.append(f"| {th}% | {nh} | {f'{pd_:.0f}%' if pd_ is not None else 'NA'} |")
    L.append("\n## 逐日明细\n")
    L.append("| 日期 | 缺口中位% | 来源 | top20 | Top20日志(涨/跌) | 基调 | S | 当日涨跌% | L1 | L2 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        act = f"{r['actual_pct']:+.2f}" if r["actual_pct"] is not None else "NA"
        t20 = f"{r['top20_bias']:+.0f}" if isinstance(r["top20_bias"], (int, float)) else "-"
        tl = f"{r['top20_log'][0]}/{r['top20_log'][1]}@{r['top20_log'][2]}" if r["top20_log"] else "-"
        sc = f"{r['score']:.0f}" if r["score"] is not None else "-"
        L.append(f"| {r['date']} | {r['gap_med']:+.2f} | {r['gap_src']} "
                 f"| {t20} | {tl} | {r['regime'] or '-'} | {sc} | {act} "
                 f"| {'Y' if lv1(r) else ''} | {'Y' if lv2(r) else ''} |")
    OUT_MD.write_text("\n".join(L), encoding="utf-8")
    print(f"样本 {len(rows)} 日 / 有效 {len(valid)} 日 / 基线下跌 {baseline:.1f}%")
    print(f"看多基调 {len(bull)} 日；L1 命中 {len(l1_hits)} 下跌占比 {l1_down}; "
          f"L2 命中 {len(l2_hits)} 下跌占比 {l2_down}")
    print(f"输出 -> {OUT_MD}")


if __name__ == "__main__":
    main()
