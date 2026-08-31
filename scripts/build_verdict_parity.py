# -*- coding: utf-8 -*-
"""
scripts/build_verdict_parity.py — P3 验收：同一交易日双侧（manual 扫描链 / auto 闸链）verdict 一致性

双侧定义（合并实施方案 P3 + Q&A#3：公共建仓判定 = position_builder + timing_gate）：
  · manual 侧（生产口径）：core/timing_gate.timing_verdict（facade 数据路径：cache-first + forming bar）
    → core/build_decision.verdict_from_timing —— 即 core/position_builder.scan_stock 的方案A 判定链
  · auto 侧（模拟 goldminer/新终端数据路径）：gm 直拉（GmProvider，绕过 facade 缓存与 forming bar）
    → core/build_decision 同一决策核

判定一致的内涵：同一决策核（单一真源）+ 两条数据路径产出等价特征 → verdict/go/veto 逐票相同。
EOD 口径（历史日）：W35 日内确认闸门不生效（两侧同），m5 层不参与。

用法：python scripts/build_verdict_parity.py --date 2026-08-27 [--codes 600481,000988]
退出码：0=全票一致；1=存在分歧或数据不可用。
"""
import argparse
import importlib.util
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)


def _load_auto_pool():
    spec = importlib.util.spec_from_file_location("auto_pool", os.path.join(BASE, "config", "auto_pool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def manual_chain(code, date_str, etp):
    """manual 侧生产判定链（= scan_stock 的方案A 分支，EOD 口径无 W35 门）。"""
    from core.timing_gate import timing_verdict
    from core import build_decision as bd
    tv = timing_verdict(code, date_str)
    f = tv.get("features") or {}
    verdict, score = bd.verdict_from_timing(bool(tv.get("go")), tv.get("regime", "range"), f, not f)
    return {"verdict": verdict, "go": bool(tv.get("go")), "veto": tv.get("veto") or [],
            "regime": tv.get("regime"), "score": score, "features": f}


def _idx_forming(idx_df):
    """指数日线补当日 forming bar（与 execution/auto/gm_main._append_index_forming 同语义，2026-08-31）。
    gm.history_n 盘中不含当日指数 bar；用 gm.current(SHSE.000001) 实时快照补当日 OHLC。
    否则 auto_chain 用昨日收盘判 regime，与手动链(facade 补 forming)盘中不一致。"""
    from datetime import datetime
    import pandas as pd
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.weekday() >= 5 or not ("09:15" <= now.strftime("%H:%M") <= "16:00"):
        return idx_df
    if idx_df is None or idx_df.empty or str(idx_df["date"].iloc[-1]) >= today:
        return idx_df
    try:
        from gm.api import current
        rows = current("SHSE.000001")
    except Exception:
        return idx_df
    if not rows:
        return idx_df
    r = rows[0] if isinstance(rows, list) else rows
    px = float(r.get("price") or 0)
    if px <= 0:
        return idx_df
    fb = pd.DataFrame([{
        "date": today,
        "open": float(r.get("open") or px), "high": float(r.get("high") or px),
        "low": float(r.get("low") or px), "close": px,
        "volume": float(r.get("cum_volume") or 0) / 100.0,  # 股 → 手
    }])
    return pd.concat([idx_df, fb], ignore_index=True)


def auto_chain(code, gm_symbol, date_str, etp):
    """auto 侧判定链（P4-6：走 execution/auto/build_decision_auto 真实代码路径，EOD 口径 df_1min=None）。

    与 execution/auto/gm_main.py BASE 建仓块同款决策适配器（core/build_decision 单一真源）。"""
    from core.market_data.gm_provider import GmProvider
    from execution.auto import build_decision_auto as bda
    gp = GmProvider()
    df = gp.daily(code, days=200)  # provider 契约：内部 6 位码，codec 内部转 GM 格式
    idx = _idx_forming(gp.index_daily("sh000001", days=200))  # 2026-08-31: 补当日指数 forming（与引擎一致）
    if df is None or df.empty or idx is None or idx.empty:
        return {"verdict": "data_unavailable", "go": None, "veto": [], "regime": None,
                "score": None, "features": {}}
    dec = bda.decide(df, idx, date_str, params=etp, df_1min=None)
    return {"verdict": dec["verdict"], "go": dec["go"], "veto": dec["veto"],
            "regime": dec["regime"], "score": dec["score"], "features": dec.get("features", {}),
            "data_insufficient": dec.get("data_insufficient", False)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--codes", default=None, help="逗号分隔；缺省=auto 池全部 17 票")
    args = ap.parse_args()

    from config import ENTRY_TIMING_PARAMS as etp
    pool = _load_auto_pool()
    codes = args.codes.split(",") if args.codes else pool.auto_pool_codes()

    rows, diffs = [], 0
    for code in codes:
        gm_sym = pool.AUTO_POOL.get(code, {}).get("gm_symbol", code)
        try:
            m = manual_chain(code, args.date, etp)
        except Exception as e:
            m = {"verdict": f"error:{type(e).__name__}", "go": None, "veto": [], "regime": None, "score": None}
        try:
            a = auto_chain(code, gm_sym, args.date, etp)
        except Exception as e:
            a = {"verdict": f"error:{type(e).__name__}", "go": None, "veto": [], "regime": None, "score": None}
        match = (m["verdict"] == a["verdict"] and m["go"] == a["go"]
                 and sorted(m["veto"]) == sorted(a["veto"]) and m["regime"] == a["regime"])
        if not match:
            diffs += 1
        rows.append({"code": code, "match": match,
                     "manual": {k: m[k] for k in ("verdict", "go", "veto", "regime", "score")},
                     "auto": {k: a[k] for k in ("verdict", "go", "veto", "regime", "score")}})
        mark = "OK " if match else "DIFF"
        print(f"[{mark}] {code}  manual={m['verdict']}(go={m['go']},veto={m['veto']})  "
              f"auto={a['verdict']}(go={a['go']},veto={a['veto']})  regime={m['regime']}/{a['regime']}")

    report = {"date": args.date, "total": len(rows), "diffs": diffs, "rows": rows}
    out = os.path.join(BASE, "t_io", "validation", f"build_verdict_parity_{args.date}.json")
    with open(out, "w", encoding="utf-8") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=1, default=str)
    print(f"\n一致 {len(rows) - diffs}/{len(rows)}，分歧 {diffs}；报告: {out}")
    sys.exit(1 if diffs else 0)


if __name__ == "__main__":
    main()
