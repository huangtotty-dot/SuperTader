# -*- coding: utf-8 -*-
"""P0-1 gm 配额实测（合并实施方案 P0，2026-08-28）
实测掘金数据接口配额/权限，回答"gm 作为主数据源是否可行"。
产出: t_io/experiment/gm_quota_probe_report.md
"""
import json
import os
import sys
import time
from datetime import datetime

import gm.api as gma

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WATCHLIST = os.path.join(BASE, "t_io", "state", "watchlist_buy.json")
GM_CONFIG = os.path.join(BASE, "t_io", "state", "gm_config.json")
OUT_MD = os.path.join(BASE, "t_io", "experiment", "gm_quota_probe_report.md")


def market_of(code: str) -> str:
    return "SHSE." if code[0] in "569" else "SZSE."


def load_watchlist():
    w = json.load(open(WATCHLIST, encoding="utf-8"))
    return [k for k in w.get("stocks", {}) if k.isdigit()]


def load_token():
    return json.load(open(GM_CONFIG, encoding="utf-8"))["token"]


def probe_history(freq, count, symbols, end_time):
    ok = fail = 0
    err_types = {}
    first_n = None
    t0 = time.time()
    for s in symbols:
        try:
            df = gma.history_n(symbol=market_of(s) + s, frequency=freq, count=count,
                               end_time=end_time, fields="eob,open,high,low,close,volume",
                               adjust=gma.ADJUST_PREV, df=True)
            if df is None or len(df) == 0:
                fail += 1
                err_types["空返回"] = err_types.get("空返回", 0) + 1
            else:
                ok += 1
                if first_n is None:
                    first_n = len(df)
        except Exception as e:
            fail += 1
            key = type(e).__name__
            err_types[key] = err_types.get(key, 0) + 1
    dt = time.time() - t0
    return {"freq": freq, "count": count, "symbols": len(symbols), "ok": ok, "fail": fail,
            "first_rows": first_n, "total_sec": round(dt, 1),
            "per_sec": round(dt / max(1, ok), 2), "err_types": err_types}


def probe_subscribe(symbols):
    try:
        gma.subscribe(symbols=[market_of(s) + s for s in symbols], frequency="60s")
        return {"ok": True, "note": "subscribe 成功建立"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def main():
    gma.set_token(load_token())
    today = datetime.now().strftime("%Y-%m-%d")
    symbols = load_watchlist()
    print(f"[probe] 标的数={len(symbols)} end_time={today}")

    report = {"date": today, "symbols": symbols, "results": {}}

    # 1. 日线 800 天 × 41 只
    print("[probe] 日线 800 天...")
    r1 = probe_history("1d", 800, symbols, today)
    report["results"]["daily_800"] = r1
    print(f"  ok={r1['ok']} fail={r1['fail']} 耗时={r1['total_sec']}s 均单={r1['per_sec']}s errs={r1['err_types']}")

    # 2. 60s × 240（盘前预取量）
    print("[probe] 60s × 240...")
    r2 = probe_history("60s", 240, symbols, today)
    report["results"]["minute_60s_240"] = r2
    print(f"  ok={r2['ok']} fail={r2['fail']} 耗时={r2['total_sec']}s 均单={r2['per_sec']}s errs={r2['err_types']}")

    # 3. subscribe 60s（终端内能否建立）
    print("[probe] subscribe 60s...")
    r3 = probe_subscribe(symbols)
    report["results"]["subscribe_60s"] = r3
    print(f"  ok={r3['ok']} note={r3.get('note') or r3.get('error')}")

    # 4. 指数日线 900 天
    print("[probe] 指数日线 900 天...")
    t0 = time.time()
    try:
        idx = gma.history_n(symbol="SHSE.000001", frequency="1d", count=900,
                            end_time=today, fields="eob,close", adjust=gma.ADJUST_PREV, df=True)
        r4 = {"ok": len(idx) > 0, "rows": 0 if idx is None else len(idx),
              "sec": round(time.time() - t0, 1)}
    except Exception as e:
        r4 = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "sec": round(time.time() - t0, 1)}
    report["results"]["index_daily_900"] = r4
    print(f"  ok={r4['ok']} rows={r4.get('rows')} err={r4.get('error')}")

    # 结论判定
    d = r1["ok"] / max(1, len(symbols))
    m = r2["ok"] / max(1, len(symbols))
    rate_limit = any("限流" in k or "429" in k or "quota" in k.lower() or "频率" in k for k in list(r1["err_types"]) + list(r2["err_types"]))
    feasible = (d >= 0.98 and m >= 0.98 and not rate_limit)
    report["conclusion"] = {
        "gm_as_primary_feasible": bool(feasible),
        "daily_success_rate": round(d, 3),
        "minute_success_rate": round(m, 3),
        "rate_limit_detected": bool(rate_limit),
        "note": ('gm 可作为主数据源' if feasible
                 else '配额/权限不足，需改 gm 供 auto 侧、腾讯供 manual 侧（方案 §9-2 重议）'),
    }

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# gm 数据配额实测报告（P0-1）\n\n")
        f.write(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  end_time={today}  标的数={len(symbols)}\n\n")
        f.write("## 实测结果\n\n")
        for k, v in report["results"].items():
            f.write(f"### {k}\n\n```\n{json.dumps(v, ensure_ascii=False, indent=2)}\n```\n\n")
        f.write("## 结论\n\n")
        f.write(f"**gm 作为主数据源是否可行: {'是' if feasible else '否'}**\n\n")
        f.write(f"- 日线成功率: {d:.1%}\n- 60s 成功率: {m:.1%}\n")
        f.write(f"- 限流报错: {'检测到' if rate_limit else '未检测到'}\n")
        f.write(f"- {report['conclusion']['note']}\n")
        f.write('\n*若结论为配额不足 → 停下找用户，数据主源改为 gm 供 auto 侧、腾讯供 manual 侧（方案 §9-2 重议）。*\n')
    print(f"\n[probe] 报告已写入: {OUT_MD}")
    print(f"[probe] 结论: {report['conclusion']['note']}")


if __name__ == "__main__":
    main()
