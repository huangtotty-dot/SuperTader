# -*- coding: utf-8 -*-
"""
signal_outcome_tracker.py — 建仓信号有效性追踪（fix P0-15 验证管线，新建文件，不改任何现有代码）

用法：
    python t_io/validation/signal_outcome_tracker.py            # 处理 traces/ 下全部 position_builder_*.jsonl
    python t_io/validation/signal_outcome_tracker.py --days 3   # 只处理最近 N 个 trace 日
    python t_io/validation/signal_outcome_tracker.py --min-samples 20   # 自定义样本充足阈值

流程：
    1. 读取 t_io/traces/position_builder_*.jsonl，按 (code, 日期) 聚合，取当日最后一次扫描的
       最终 verdict（signal/approaching/weak）与当时价格（trace 内 price，盘中扫描时为现价）。
    2. 对 verdict ∈ {signal, approaching} 的记录，先查 t_io/cache/daily_kline/<code>.json 本地日线缓存，
       缓存缺少目标日期时回退腾讯 qfq 日线接口（web.ifzq.gtimg.cn，与 daily_review.py 同源），
       取信号日之后第 1/3/5 个交易日收盘价，计算持有收益 = close(T+N)/入场价 - 1。
    3. 沪深300（sh000300）同期收益作基准，标注每条记录是否跑赢指数。
    4. 输出：
       - t_io/validation/signal_outcomes.json          机读明细 + 汇总
       - t_io/validation/signal_outcomes_summary.md    人读汇总（样本数 / 各 verdict 平均收益 / 胜率）
    5. 样本数 < --min-samples（默认 20）时汇总显式标注『样本不足，仅供 shadow 观察』。

降级约定（不崩溃）：
    - 日线获取失败 / 目标交易日尚未到来 → 该 horizon 记 "unavailable"，附 reason；
    - 基准指数获取失败 → beat_benchmark 记 null；
    - trace 内 price 缺失 → 尝试用信号日收盘替代，仍无则整段收益 unavailable。

GUI 复用：
    from t_io.validation.signal_outcome_tracker import sample_sufficient
    sample_sufficient() -> bool   # False 时 GUI 应挂『未验证』水印（signal 的 T+5 样本 < 20）
"""

import argparse
import glob
import json
import os
import sys
import urllib.request
from datetime import datetime

# ---- 路径推导：脚本位于 <root>/t_io/validation/，根目录为上两级 ----
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TRACES_DIR = os.path.join(ROOT, "t_io", "traces")
KLINE_CACHE_DIR = os.path.join(ROOT, "t_io", "cache", "daily_kline")
OUT_DIR = os.path.join(ROOT, "t_io", "validation")
OUT_JSON = os.path.join(OUT_DIR, "signal_outcomes.json")
OUT_MD = os.path.join(OUT_DIR, "signal_outcomes_summary.md")

HORIZONS = (1, 3, 5)                      # T+N 持有期
# B-4(2026-08-21): 加入 range 市观察态 watch_signal 累计样本（解决 0/20 样本不足）
TRACKED_VERDICTS = ("signal", "approaching", "watch_signal")   # 需要计算收益的 verdict
ALL_VERDICTS = ("signal", "approaching", "watch_signal", "weak")
BENCHMARK_CODE = "sh000300"               # 沪深300
MIN_SAMPLES_DEFAULT = 20
TX_TIMEOUT = 12
UA = {"User-Agent": "Mozilla/5.0"}


# ---------------------------------------------------------------- 日线获取
def _mkt_code(code: str) -> str:
    """6 位代码 → 腾讯市场前缀（指数/沪市 sh，其余 sz）"""
    if code.startswith(("5", "6", "9")):
        return "sh" + code
    return "sz" + code


def fetch_kline_tx(code: str, n: int = 60):
    """腾讯 qfq 日线 → [{'date','close',...}, ...]；失败返回 None（区分于空）"""
    mkt = code if code.startswith(("sh", "sz")) else _mkt_code(code)
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={mkt},day,,,{n},qfq")
    try:
        req = urllib.request.Request(url, headers=UA)
        d = json.loads(urllib.request.urlopen(req, timeout=TX_TIMEOUT).read().decode())
        node = d["data"][mkt]
        rows = node.get("qfqday") or node.get("day") or []
        out = []
        for r in rows:
            # 腾讯行格式: [date, open, close, high, low, volume, ...]
            out.append({"date": r[0], "open": float(r[1]), "close": float(r[2]),
                        "high": float(r[3]), "low": float(r[4])})
        return out or None
    except Exception:
        return None


def load_kline_cache(code: str):
    """本地日线缓存 t_io/cache/daily_kline/<code>.json → rows 或 None"""
    fp = os.path.join(KLINE_CACHE_DIR, f"{code}.json")
    if not os.path.exists(fp):
        return None
    try:
        d = json.load(open(fp, encoding="utf-8"))
        rows = d.get("rows") or []
        return [{"date": r["date"], "open": float(r["open"]), "close": float(r["close"]),
                 "high": float(r["high"]), "low": float(r["low"])} for r in rows] or None
    except Exception:
        return None


def get_kline(code: str):
    """合并本地缓存 + 腾讯在线（在线补缓存没有的最新日期）；全失败返回 (None, 'all_failed')"""
    cache_rows = load_kline_cache(code)
    tx_rows = fetch_kline_tx(code)
    if tx_rows:
        merged = {r["date"]: r for r in (cache_rows or [])}
        src = "cache+tencent" if cache_rows else "tencent"
        for r in tx_rows:                       # 在线数据覆盖同日缓存（更新）
            merged[r["date"]] = r
        rows = [merged[k] for k in sorted(merged)]
        return rows, src
    if cache_rows:
        return cache_rows, "cache"
    return None, "all_failed"


# ---------------------------------------------------------------- 收益计算
def calc_horizon_returns(rows, signal_date: str, entry_price):
    """
    rows: 按日期升序日线；signal_date: 'YYYY-MM-DD'；entry_price: 入场价（可为 None）
    返回 {1: {...}, 3: {...}, 5: {...}}，每项 {'close','ret','status','reason'}
    口径说明：trace 内 price 为当时现价（未复权），与 qfq 日线可能存在复权口径差；
    若 rows 中含信号日收盘，优先以其为基准计算 ret，entry_price 仅作展示参考。
    """
    dates = [r["date"] for r in rows]
    closes = {r["date"]: r["close"] for r in rows}
    # 定位信号日：序列中最后一个 <= signal_date 的交易日
    idx = None
    for i, d in enumerate(dates):
        if d <= signal_date:
            idx = i
        else:
            break
    if idx is None:
        return {h: {"close": None, "ret": None, "status": "unavailable",
                    "reason": "日线序列早于信号日"} for h in HORIZONS}
    base_close = closes[dates[idx]]
    base = base_close if base_close else entry_price
    if not base:
        return {h: {"close": None, "ret": None, "status": "unavailable",
                    "reason": "无入场价且信号日收盘缺失"} for h in HORIZONS}
    res = {}
    for h in HORIZONS:
        j = idx + h
        if j < len(dates):
            c = closes[dates[j]]
            # 退出侧(A-1): 持有期内(信号日后第1..N个交易日)最低价相对入场价的最大浮亏
            seg = rows[idx + 1:j + 1]
            min_low = min((r["low"] for r in seg if r.get("low")), default=None) if seg else None
            res[h] = {"date": dates[j], "close": c,
                      "ret": round(c / base - 1, 4), "status": "ok", "reason": "",
                      "max_drawdown": round(min_low / base - 1, 4) if min_low else None}
        else:
            res[h] = {"date": None, "close": None, "ret": None,
                      "status": "unavailable", "reason": "目标交易日数据未到期或获取失败",
                      "max_drawdown": None}
    return res


# ---------------------------------------------------------------- trace 聚合
def load_final_verdicts(days=None):
    """
    读取 position_builder_*.jsonl，按 (code, 日期) 取当日最后一条扫描记录。
    返回 list[dict]：{code,name,date,scan_time,verdict,price,score}
    """
    files = sorted(glob.glob(os.path.join(TRACES_DIR, "position_builder_*.jsonl")))
    if days:
        files = files[-days:]
    latest = {}   # (code, date) -> record
    for fp in files:
        for line in open(fp, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            code, st = r.get("code"), r.get("scan_time")
            if not code or not st:
                continue
            date = st[:10]
            key = (code, date)
            if key not in latest or st > latest[key]["scan_time"]:
                latest[key] = {"code": code, "name": r.get("name", ""),
                               "date": date, "scan_time": st,
                               "verdict": r.get("verdict"),
                               "price": r.get("price"),
                               "score": r.get("composite_score")}
    return [latest[k] for k in sorted(latest)]


# ---------------------------------------------------------------- 汇总
def summarize(records):
    """按 verdict 分组统计样本数 / 平均收益 / 胜率（仅 status=ok 的 horizon 参与）"""
    summary = {}
    for v in ALL_VERDICTS:
        group = [r for r in records if r["verdict"] == v]
        entry = {"samples": len(group), "horizons": {}}
        for h in HORIZONS:
            rets = [r["returns"][str(h)]["ret"] for r in group
                    if r.get("returns") and r["returns"][str(h)]["status"] == "ok"]
            beats = [r["returns"][str(h)]["beat_benchmark"] for r in group
                     if r.get("returns") and r["returns"][str(h)]["status"] == "ok"
                     and r["returns"][str(h)]["beat_benchmark"] is not None]
            mdd = [r["returns"][str(h)]["max_drawdown"] for r in group
                   if r.get("returns") and r["returns"][str(h)].get("max_drawdown") is not None]
            entry["horizons"][str(h)] = {
                "n": len(rets),
                "avg_ret": round(sum(rets) / len(rets), 4) if rets else None,
                "win_rate": round(sum(1 for x in rets if x > 0) / len(rets), 4) if rets else None,
                "beat_benchmark_rate": round(sum(1 for x in beats if x) / len(beats), 4) if beats else None,
                "avg_max_drawdown": round(sum(mdd) / len(mdd), 4) if mdd else None,
            }
        summary[v] = entry
    return summary


def sample_sufficient(out_json_path: str = OUT_JSON,
                      min_samples: int = MIN_SAMPLES_DEFAULT,
                      horizon: int = 5) -> bool:
    """
    供 GUI 复用：signal 级别、指定 horizon（默认 T+5）收益已验证的样本数 >= min_samples 才返回 True。
    False 时 GUI 应挂『未验证』水印。outcomes 文件缺失/损坏一律返回 False（宁可挂水印）。
    """
    try:
        d = json.load(open(out_json_path, encoding="utf-8"))
        recs = [r for r in d.get("records", [])
                if r.get("verdict") == "signal"
                and (r.get("returns") or {}).get(str(horizon), {}).get("status") == "ok"]
        return len(recs) >= min_samples
    except Exception:
        return False


# ---------------------------------------------------------------- 主流程
def run_settle(days=None, min_samples=MIN_SAMPLES_DEFAULT):
    """可编程结算入口（A-1：供 daily_review 每日调用）。
    读 position_builder trace 全量重算 signal_outcomes.json，含退出侧 max_drawdown。"""
    os.makedirs(OUT_DIR, exist_ok=True)
    verdicts = load_final_verdicts(days=days)
    print(f"[tracker] 最终 verdict 记录 {len(verdicts)} 条"
          f"（signal/approaching/weak 各 "
          f"{sum(1 for r in verdicts if r['verdict']=='signal')}/"
          f"{sum(1 for r in verdicts if r['verdict']=='approaching')}/"
          f"{sum(1 for r in verdicts if r['verdict']=='weak')}）")

    # 基准：沪深300 日线（一次获取，全记录复用）
    bench_rows, bench_src = get_kline(BENCHMARK_CODE)
    if bench_rows:
        print(f"[tracker] 沪深300 基准日线 {len(bench_rows)} 行（{bench_src}）")
    else:
        print(f"[tracker][warn] 沪深300 基准获取失败，beat_benchmark 记 null")

    records = []
    kline_memo = {}   # code -> (rows, src) 当日进程内缓存，避免重复请求
    for rec in verdicts:
        out = dict(rec)
        if rec["verdict"] not in TRACKED_VERDICTS:
            out["returns"] = None           # weak 不计算收益（任务口径：仅 signal/approaching）
            records.append(out)
            continue
        code = rec["code"]
        if code not in kline_memo:
            kline_memo[code] = get_kline(code)
        rows, src = kline_memo[code]
        out["kline_source"] = src
        if not rows:
            out["returns"] = {str(h): {"date": None, "close": None, "ret": None,
                                       "status": "unavailable",
                                       "reason": "日线缓存与腾讯接口均失败"} for h in HORIZONS}
            records.append(out)
            continue
        rets = calc_horizon_returns(rows, rec["date"], rec.get("price"))
        # 基准对照：同窗口沪深300 收益
        if bench_rows:
            brets = calc_horizon_returns(bench_rows, rec["date"], None)
            for h in HORIZONS:
                b = brets[h]
                if rets[h]["status"] == "ok" and b["status"] == "ok":
                    rets[h]["benchmark_ret"] = b["ret"]
                    rets[h]["beat_benchmark"] = rets[h]["ret"] > b["ret"]
                else:
                    rets[h]["benchmark_ret"] = None
                    rets[h]["beat_benchmark"] = None
        else:
            for h in HORIZONS:
                rets[h]["benchmark_ret"] = None
                rets[h]["beat_benchmark"] = None
        out["returns"] = {str(h): rets[h] for h in HORIZONS}
        records.append(out)

    summary = summarize(records)
    sig_verified = sum(1 for r in records if r["verdict"] == "signal"
                       and r.get("returns") and r["returns"]["5"]["status"] == "ok")

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "params": {"days": days, "min_samples": min_samples,
                   "horizons": list(HORIZONS), "benchmark": "沪深300(sh000300)"},
        "note": "入场价为 trace 内当时价格（盘中扫描时为现价）；"
                "持有收益以信号日收盘为基准计算；max_drawdown=持有期内最低价相对入场价最大浮亏；"
                "unavailable=数据未到期或获取失败",
        "signal_verified_samples_t5": sig_verified,
        "sample_sufficient": sig_verified >= min_samples,
        "summary": summary,
        "records": records,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # ---- markdown 汇总 ----
    L = ["# 建仓信号有效性追踪汇总", "",
         f"生成时间：{payload['generated_at']}　基准：沪深300　样本充足阈值：{min_samples}", ""]
    if sig_verified < min_samples:
        L += [f"> ⚠️ **样本不足，仅供 shadow 观察**（signal 的 T+5 已验证样本 "
              f"{sig_verified}/{min_samples}）", ""]
    L += ["| verdict | 样本数 | horizon | 有效n | 平均收益 | 胜率 | 跑赢基准率 | 平均最大浮亏 |",
          "|---|---|---|---|---|---|---|---|"]
    for v in ALL_VERDICTS:
        s = summary[v]
        for h in HORIZONS:
            hz = s["horizons"][str(h)]
            fmt = lambda x: ("%.2f%%" % (x * 100)) if x is not None else "—"
            L.append(f"| {v} | {s['samples']} | T+{h} | {hz['n']} | "
                     f"{fmt(hz['avg_ret'])} | {fmt(hz['win_rate'])} | "
                     f"{fmt(hz['beat_benchmark_rate'])} | {fmt(hz['avg_max_drawdown'])} |")
    L += ["", f"明细机读文件：`t_io/validation/signal_outcomes.json`（{len(records)} 条记录）", ""]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(L))

    print(f"[tracker] 已写出 {OUT_JSON}")
    print(f"[tracker] 已写出 {OUT_MD}")
    print(f"[tracker] signal T+5 已验证样本 {sig_verified}/{min_samples} → "
          f"sample_sufficient={sig_verified >= min_samples}")
    return payload


def main():
    ap = argparse.ArgumentParser(description="建仓信号有效性追踪（T+1/T+3/T+5 对照）")
    ap.add_argument("--days", type=int, default=None, help="只处理最近 N 个 trace 日（默认全部）")
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES_DEFAULT, help="样本充足阈值（默认 20）")
    args = ap.parse_args()
    run_settle(days=args.days, min_samples=args.min_samples)
    return 0


if __name__ == "__main__":
    sys.exit(main())
