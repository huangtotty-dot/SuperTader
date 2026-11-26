# -*- coding: utf-8 -*-
"""H4 假说离线回测 · D1：窗口清单 + 候选票清单 + 数据质检。

排期文档：`doc/solutions/2026-11-25_H4离线回测施工排期.md`（owner 2026-11-25 拍板立项）
口径真源：复盘清单 H4 登记条——
  · 窗口：弱转强拐点 = sent_ma 从 <40 上穿 ≥40 的跨越日；早期 = 跨越日起 3 个交易日内
  · 候选：窗口内「收盘价站上 MA5」的全市场票（与 OSC 宇宙同池）
  · 温度变体：综合温度 comp 同口径各跑一遍（联动议题③）

实现要点：
  · 宇宙 = OSC 全市场口径（`prescreen.full_market_meta`：剔 ST/退市/北交所/B股）
    ∩ 本地日K缓存（`t_io/cache/daily_kline/`）；板块标签取 `universe_board_map`
  · 「站上 MA5」= fresh cross：今日 close > MA5 且 昨收 ≤ 昨MA5（攻击 5 日线口径，
    非「一直在上方」）；dev20_prev = 昨收/昨MA20 − 1（OSC 侧超跌判据备用）
  · 交易日历 = 温度序列日期 ∪ 指数缓存（sh000300）日期（温度序列末尾日后的窗口
    第 2/3 天也能取到）
  · 前视纪律：候选判定只用 T 日及之前的收盘；入场日 = 信号日次日（本工序只记录不下单）

产物（全部落 `t_io/validation/h4_backtest/`，不碰生产）：
  · windows.json    —— 窗口清单（series=sent_ma/comp 各一份）
  · candidates.csv  —— 候选票明细（每窗口内同票只记首次 cross）
  · qc_report.json  —— 数据质检（覆盖率/内部一致性/外部抽检 best-effort）
"""
from __future__ import annotations

import bisect
import csv
import json
import os
import random
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# prescreen 包 __init__ 会拉起 gateway→framework（GM 终端限定），离线环境不可走包导入；
# 按文件路径直载模块，绕开包级副作用（prescreen.py 自身是纯逻辑，无 GM 依赖）。
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "osc_prescreen_offline",
    os.path.join(_ROOT, "execution", "auto", "oversold_gap_confirm", "prescreen.py"))
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
full_market_meta = _mod.full_market_meta
universe_board_map = _mod.universe_board_map

TEMP_FP = os.path.join(_ROOT, "t_io", "index_regime", "composite_temp.json")
CACHE_DIR = os.path.join(_ROOT, "t_io", "cache", "daily_kline")
IDX_FP = os.path.join(CACHE_DIR, "index_sh000300.json")
OUT_DIR = os.path.join(_ROOT, "t_io", "validation", "h4_backtest")

WINDOW_DAYS = 3          # 跨越日 + 其后 2 个交易日
CROSS_LO, CROSS_HI = 40.0, 40.0   # <40 → ≥40


def _load_temp_series():
    """温度序列 → (dates_asc, {date: sent_ma}, {date: comp})。"""
    with open(TEMP_FP, encoding="utf-8") as f:
        raw = json.load(f)
    dates = sorted(raw.keys())
    sent = {d: (raw[d] or {}).get("sent_ma") for d in dates}
    comp = {d: (raw[d] or {}).get("comp") for d in dates}
    return dates, sent, comp


def _load_index_calendar():
    """指数缓存日期（补温度序列末尾之后的交易日）。"""
    try:
        with open(IDX_FP, encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("rows") or []
        return [r["date"] for r in rows if r.get("date")]
    except Exception:
        return []


def _find_windows(cal, series, tag):
    """跨越日（前一日 <40、当日 ≥40）→ 窗口 {window_id, series, cross_date, days, temps}。"""
    wins = []
    pos = {d: i for i, d in enumerate(cal)}
    sdates = [d for d in cal if series.get(d) is not None]
    for i in range(1, len(sdates)):
        d0, d1 = sdates[i - 1], sdates[i]
        v0, v1 = series[d0], series[d1]
        if v0 < CROSS_LO and v1 >= CROSS_HI:
            j = pos[d1]
            days = cal[j:j + WINDOW_DAYS]
            wins.append({
                "window_id": f"{tag}_{d1}",
                "series": tag,
                "cross_date": d1,
                "days": days,
                "complete": len(days) == WINDOW_DAYS,
                "temp_path": {d: series.get(d) for d in cal[max(0, j - 2):j + WINDOW_DAYS]},
            })
    return wins


def _scan_candidates(day_set, universe, board_map):
    """一遍过全池：对每个窗口日判定 fresh MA5 cross。返回 (rows, stats)。"""
    rows, stats = [], {"files_ok": 0, "files_missing": 0, "files_bad": 0,
                       "short_history": 0, "scanned_days": len(day_set)}
    for code in sorted(universe):
        fp = os.path.join(CACHE_DIR, f"{code}.json")
        if not os.path.exists(fp):
            stats["files_missing"] += 1
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                krows = (json.load(f) or {}).get("rows") or []
        except Exception:
            stats["files_bad"] += 1
            continue
        if len(krows) < 25:
            stats["short_history"] += 1
            continue
        dates = [r["date"] for r in krows]
        closes = [float(r["close"]) for r in krows]
        # 防御性：缓存按日期升序（现网如此）；若不升序则排序
        if dates != sorted(dates):
            order = sorted(range(len(dates)), key=lambda i: dates[i])
            dates = [dates[i] for i in order]
            closes = [closes[i] for i in order]
        stats["files_ok"] += 1
        for day in day_set:
            i = bisect.bisect_left(dates, day)
            if i >= len(dates) or dates[i] != day or i < 20:
                continue
            ma5 = sum(closes[i - 4:i + 1]) / 5.0
            ma5_prev = sum(closes[i - 5:i]) / 5.0
            if not (closes[i] > ma5 and closes[i - 1] <= ma5_prev):
                continue
            ma20_prev = sum(closes[i - 20:i]) / 20.0
            prev_close = closes[i - 1]
            rows.append({
                "date": day, "code": code,
                "close": round(closes[i], 4), "ma5": round(ma5, 4),
                "prev_close": round(prev_close, 4),
                "ma20_prev": round(ma20_prev, 4),
                "dev20_prev": round(prev_close / ma20_prev - 1.0, 6) if ma20_prev else None,
            })
    return rows, stats


def _qc_internal(cand_rows, n=5):
    """独立重算抽查：用另一段逻辑重算 MA5 cross，验证主扫描没写错。"""
    samples = random.sample(cand_rows, min(n, len(cand_rows)))
    out = []
    for s in samples:
        fp = os.path.join(CACHE_DIR, f"{s['code']}.json")
        with open(fp, encoding="utf-8") as f:
            krows = (json.load(f) or {}).get("rows") or []
        dates = [r["date"] for r in krows]
        i = dates.index(s["date"])
        closes = [float(r["close"]) for r in krows]
        ma5 = sum(closes[i - 4:i + 1]) / 5.0
        ma5_prev = sum(closes[i - 5:i]) / 5.0
        ok = (abs(ma5 - s["ma5"]) < 1e-3 and closes[i] > ma5
              and closes[i - 1] <= ma5_prev)
        out.append({"code": s["code"], "date": s["date"], "ok": bool(ok),
                    "ma5_recalc": round(ma5, 4), "ma5_scan": s["ma5"]})
    return out


def _qc_external(cand_rows, n=10):
    """外部抽检（best-effort）：腾讯 fqkline 收盘价 vs 缓存收盘价。

    fqkline 端点历史上被 WAF 拦过（候选㊳ 背景）——失败不阻塞，如实记录。
    """
    res = {"attempted": 0, "ok": 0, "mismatch": [], "error": None}
    try:
        from core.market_data.http_pool import pooled_get
    except Exception as e:
        res["error"] = f"http_pool 不可用: {e}"
        return res
    recent = sorted(cand_rows, key=lambda r: r["date"])[-200:]
    samples = random.sample(recent, min(n, len(recent))) if recent else []
    for s in samples:
        res["attempted"] += 1
        code, day = s["code"], s["date"]
        sym = ("sh" if code[0] == "6" else "sz") + code
        path = (f"/appstock/app/fqkline/get"
                f"?param={sym},day,{day},{day},5,qfq")
        try:
            r = pooled_get("web.ifzq.gtimg.cn", path, 6, {}, encoding="utf-8")
            j = json.loads(r)
            node = j["data"][sym]
            bars = node.get("qfqday") or node.get("day") or []
            hit = [b for b in bars if str(b[0]) == day]
            if not hit:
                res["mismatch"].append({"code": code, "date": day, "why": "无该日bar"})
                continue
            tx_close = float(hit[0][2])
            if abs(tx_close - s["close"]) / max(tx_close, 1e-9) > 0.005:
                res["mismatch"].append({"code": code, "date": day,
                                        "cache": s["close"], "tencent": tx_close})
            else:
                res["ok"] += 1
        except json.JSONDecodeError:
            res["error"] = ("fqkline 返回非 JSON（WAF 拦截，候选㊳ 已知背景）⇒ 外部抽检不可用；"
                            "可靠性改由 ㊴ 全池校验 + 内部一致性抽查承担")
            break
        except Exception as e:
            res["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            break
    return res


def main():
    t0 = time.time()
    random.seed(20261126)
    os.makedirs(OUT_DIR, exist_ok=True)

    print("== D1: 加载温度序列与交易日历 ==")
    temp_dates, sent, comp = _load_temp_series()
    cal = sorted(set(temp_dates) | set(_load_index_calendar()))
    print(f"   温度序列 {temp_dates[0]}~{temp_dates[-1]} ({len(temp_dates)} 天)；"
          f"交易日历扩到 {cal[-1]} ({len(cal)} 天)")

    wins = _find_windows(cal, sent, "sent_ma") + _find_windows(cal, comp, "comp")
    print(f"   窗口数：sent_ma {sum(1 for w in wins if w['series']=='sent_ma')} 个 / "
          f"comp {sum(1 for w in wins if w['series']=='comp')} 个")

    day_set = sorted({d for w in wins for d in w["days"]})
    print(f"   去重窗口日 {len(day_set)} 天")

    print("== D1: 加载 OSC 宇宙（全市场口径） ==")
    meta = full_market_meta()
    board_map = universe_board_map(full_market=True)
    universe = sorted(meta.keys())
    print(f"   宇宙 {len(universe)} 只（含板块标签 {len(board_map)} 只）")

    print("== D1: 全池扫描 fresh MA5 cross（约 1~3 分钟） ==")
    cand_rows, stats = _scan_candidates(day_set, set(universe), board_map)
    print(f"   扫描完成 {time.time()-t0:.0f}s：{stats}；候选行 {len(cand_rows)}")

    # 挂窗口归属 + 入场日；窗口内同票只记首次 cross
    day2win = {}
    for w in wins:
        for d in w["days"]:
            day2win.setdefault((w["window_id"], d), w)
    cal_pos = {d: i for i, d in enumerate(cal)}
    out_rows = []
    per_win_seen = {}
    for r in sorted(cand_rows, key=lambda x: (x["date"], x["code"])):
        for w in wins:
            if r["date"] not in w["days"]:
                continue
            key = (w["window_id"], r["code"])
            if key in per_win_seen:
                continue
            per_win_seen[key] = True
            j = cal_pos.get(r["date"])
            entry = cal[j + 1] if j is not None and j + 1 < len(cal) else ""
            out_rows.append({
                "window_id": w["window_id"], "series": w["series"],
                "cross_date": w["cross_date"], "signal_date": r["date"],
                "entry_date": entry, "code": r["code"],
                "name": (meta.get(r["code"]) or {}).get("name", ""),
                "board": board_map.get(r["code"], ""),
                "close": r["close"], "ma5": r["ma5"],
                "prev_close": r["prev_close"], "ma20_prev": r["ma20_prev"],
                "dev20_prev": r["dev20_prev"],
            })

    print("== D1: 数据质检 ==")
    qc = {"universe": len(universe), "scan_stats": stats,
          "candidate_rows": len(out_rows),
          "per_window": {w["window_id"]: {
              "days": w["days"], "complete": w["complete"],
              "candidates": sum(1 for r in out_rows if r["window_id"] == w["window_id"]),
          } for w in wins},
          "internal_recheck": _qc_internal(cand_rows)}
    print(f"   内部一致性抽查 {len(qc['internal_recheck'])} 条："
          f"{sum(1 for x in qc['internal_recheck'] if x['ok'])}/{len(qc['internal_recheck'])} 通过")
    qc["external_tencent"] = _qc_external(cand_rows)
    ext = qc["external_tencent"]
    print(f"   腾讯外部抽检：attempted={ext['attempted']} ok={ext['ok']} "
          f"mismatch={len(ext['mismatch'])} error={ext['error']}")

    with open(os.path.join(OUT_DIR, "windows.json"), "w", encoding="utf-8") as f:
        json.dump(wins, f, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT_DIR, "candidates.csv"), "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else
                            ["window_id", "series", "cross_date", "signal_date",
                             "entry_date", "code", "name", "board", "close", "ma5",
                             "prev_close", "ma20_prev", "dev20_prev"])
        wr.writeheader()
        wr.writerows(out_rows)
    with open(os.path.join(OUT_DIR, "qc_report.json"), "w", encoding="utf-8") as f:
        json.dump(qc, f, ensure_ascii=False, indent=1)

    print(f"\n== D1 完成（{time.time()-t0:.0f}s） ==")
    print(f"   windows.json: {len(wins)} 窗口")
    print(f"   candidates.csv: {len(out_rows)} 行")
    for w in wins:
        n = qc["per_window"][w["window_id"]]["candidates"]
        print(f"     {w['window_id']:<24} days={w['days']} candidates={n}")


if __name__ == "__main__":
    main()
