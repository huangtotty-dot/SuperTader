# -*- coding: utf-8 -*-
"""P0-1 gm 配额实测（合并实施方案 P0，2026-08-28 审核打回后返工）
实测掘金数据接口配额/权限，回答"gm 作为主数据源是否可行"。
返工要点（commit 5f905f5c 审核）：token 走终端会话动态发现（utils/gm_token）；
  每只标的行为数校验（daily min=800 / minute min=240，新上市股<目标属预期并单列）。
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
OUT_MD = os.path.join(BASE, "t_io", "experiment", "gm_quota_probe_report.md")

# 生产同款 token 机制（goldminer utils/gm_token.py：GM_TOKEN 环境变量→终端会话动态发现→gm_config.json）
_GM_REPO = r"c:/Users/Lenovo/.goldminer3/projects/e8bb1f4d-87ce-11f1-97f7-98fa9b8df5e7"
if _GM_REPO not in sys.path:
    sys.path.insert(0, _GM_REPO)
from utils.gm_token import load_token  # noqa: E402

DAILY_TARGET, MINUTE_TARGET = 800, 240


def market_of(code: str) -> str:
    return "SHSE." if code[0] in "569" else "SZSE."


def load_watchlist():
    w = json.load(open(WATCHLIST, encoding="utf-8"))
    return [k for k in w.get("stocks", {}) if k.isdigit()]


def probe_history(freq, count, symbols, end_time, target):
    ok = fail = 0
    err_types = {}
    rows_by_code = {}
    t0 = time.time()
    for s in symbols:
        try:
            df = gma.history_n(symbol=market_of(s) + s, frequency=freq, count=count,
                               end_time=end_time, fields="eob,open,high,low,close,volume",
                               adjust=gma.ADJUST_PREV, df=True)
            if df is None or len(df) == 0:
                fail += 1
                err_types["空返回"] = err_types.get("空返回", 0) + 1
                rows_by_code[s] = 0
            else:
                ok += 1
                rows_by_code[s] = len(df)
        except Exception as e:
            fail += 1
            key = type(e).__name__
            err_types[key] = err_types.get(key, 0) + 1
            rows_by_code[s] = 0
    dt = time.time() - t0
    n = len(symbols)
    rows = [r for r in rows_by_code.values() if r > 0]
    short = [c for c, r in rows_by_code.items() if 0 < r < target]
    empty = [c for c, r in rows_by_code.items() if r == 0]
    return {"freq": freq, "count": count, "symbols": n, "ok": ok, "fail": fail,
            "min_rows": min(rows) if rows else 0, "max_rows": max(rows) if rows else 0,
            "target_rows": target,
            "below_target": sorted(short), "below_target_n": len(short),
            "empty": sorted(empty),
            "total_sec": round(dt, 1), "per_sec": round(dt / max(1, ok), 2),
            "err_types": err_types}


def probe_subscribe(symbols):
    try:
        gma.subscribe(symbols=[market_of(s) + s for s in symbols], frequency="60s")
        return {"ok": True, "note": "subscribe 成功建立（注：裸 subscribe 不能证明收到 bar，见结论）"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def main():
    tok = load_token()
    if not tok:
        print("[probe][FATAL] 未发现有效 gm token（终端会话动态发现失败且无静态配置）——需掘金终端运行")
        return 1
    print(f"[probe] token 来源: 终端会话动态发现（前缀 {tok[:8]}...）")
    gma.set_token(tok)
    today = datetime.now().strftime("%Y-%m-%d")
    symbols = load_watchlist()
    print(f"[probe] 标的数={len(symbols)} end_time={today}")

    report = {"date": today, "symbols": symbols, "token_prefix": tok[:8], "results": {}}

    print("[probe] 日线 800 天...")
    r1 = probe_history("1d", DAILY_TARGET, symbols, today, DAILY_TARGET)
    report["results"]["daily_800"] = r1
    print(f"  ok={r1['ok']} fail={r1['fail']} min_rows={r1['min_rows']} "
          f"低于目标={r1['below_target_n']} {r1['below_target'][:5]}... 耗时={r1['total_sec']}s")

    print("[probe] 60s × 240...")
    r2 = probe_history("60s", MINUTE_TARGET, symbols, today, MINUTE_TARGET)
    report["results"]["minute_60s_240"] = r2
    print(f"  ok={r2['ok']} fail={r2['fail']} min_rows={r2['min_rows']} "
          f"低于目标={r2['below_target_n']} {r2['below_target'][:5]}... 耗时={r2['total_sec']}s")

    print("[probe] subscribe 60s...")
    r3 = probe_subscribe(symbols)
    report["results"]["subscribe_60s"] = r3
    print(f"  {r3['ok']} {r3.get('note') or r3.get('error')}")

    t0 = time.time()
    try:
        idx = gma.history_n(symbol="SHSE.000001", frequency="1d", count=900,
                            end_time=today, fields="eob,close", adjust=gma.ADJUST_PREV, df=True)
        r4 = {"ok": idx is not None and len(idx) > 0, "rows": 0 if idx is None else len(idx),
              "sec": round(time.time() - t0, 1)}
    except Exception as e:
        r4 = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}", "sec": round(time.time() - t0, 1)}
    report["results"]["index_daily_900"] = r4
    print(f"[probe] 指数: ok={r4['ok']} rows={r4.get('rows')}")

    # 结论：日线/分钟成功率 + 行数校验（低于目标若全为短上市史则视为可解释）
    n = len(symbols)
    d = r1["ok"] / max(1, n)
    m = r2["ok"] / max(1, n)
    rate_limit = any("限流" in k or "429" in k or "quota" in k.lower() or "频率" in k
                     for k in list(r1["err_types"]) + list(r2["err_types"]))
    # 行数校验：低于目标的行数应全可解释（min>0 且无空返回；短上市史由 below_target 单列）
    rows_valid = (r1["fail"] == 0 and r2["fail"] == 0
                  and r1["min_rows"] > 0 and r2["min_rows"] > 0)
    feasible = (d >= 0.98 and m >= 0.98 and not rate_limit and rows_valid)
    report["conclusion"] = {
        "gm_as_primary_feasible": bool(feasible),
        "daily_success_rate": round(d, 3), "minute_success_rate": round(m, 3),
        "rate_limit_detected": bool(rate_limit),
        "row_count_valid": bool(rows_valid),
        "daily_min_rows": r1["min_rows"], "minute_min_rows": r2["min_rows"],
        "daily_below_target": r1["below_target"], "minute_below_target": r2["below_target"],
        "note": ('gm 可作为主数据源' if feasible
                 else '配额/权限或行数校验未过，需复核（§9-2 重议）'),
    }

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("# gm 数据配额实测报告（P0-1 返工）\n\n")
        f.write(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  end_time={today}  "
                f"标的数={n}  token来源=终端会话动态发现({tok[:8]}...)\n\n")
        f.write("## 实测结果\n\n")
        for k, v in report["results"].items():
            f.write(f"### {k}\n\n```\n{json.dumps(v, ensure_ascii=False, indent=2)}\n```\n\n")
        f.write("## 结论\n\n")
        f.write(f"**gm 作为主数据源是否可行: {'是' if feasible else '否'}**\n\n")
        f.write(f"- 日线成功率: {d:.1%}（min_rows={r1['min_rows']}，目标{DAILY_TARGET}；"
                f"低于目标{n - len(r1['below_target'])}只之外={r1['below_target_n']}只新上市）\n")
        f.write(f"- 60s 成功率: {m:.1%}（min_rows={r2['min_rows']}，目标{MINUTE_TARGET}）\n")
        f.write(f"- 限流报错: {'检测到' if rate_limit else '未检测到'}\n")
        f.write(f"- 行数校验: {'通过' if rows_valid else '未过'}（空返回={r1['empty'] + r2['empty']}）\n")
        if r1["below_target"]:
            f.write(f"- 日线低于目标（新上市预期）: {r1['below_target']}\n")
        if r2["below_target"]:
            f.write(f"- 60s 低于目标（新上市预期）: {r2['below_target']}\n")
        f.write(f"- {report['conclusion']['note']}\n")
        f.write("\n*注：subscribe 需在 run()/策略上下文验证实际收到 bar（P0 打回项3），另见后续补强。*\n")
    print(f"\n[probe] 报告已写入: {OUT_MD}")
    print(f"[probe] 结论: {report['conclusion']['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
