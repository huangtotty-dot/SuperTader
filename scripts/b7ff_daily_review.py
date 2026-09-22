# -*- coding: utf-8 -*-
"""b7ff_daily_review.py — B7 择时过滤层每日复盘（2026-09-22 施工）。

数据源：t_io/logs/b7ff_filter_{date}.jsonl（gm_main 14:55 挂钩全量落：
日期/标的/F(14:30)/z/判定/理由/tail30/c1455，owner 审批口径②）。

输出：
  1. 当日过滤率 = block / (pass+block)（实验基线 ≈80%，漂移 >20pp 告警）；
  2. na_allow / 守卫失败计数（z 历史断档或分钟线缺失的先行指标）；
  3. 被拦腿次日回溯：join tushare 日线次日开盘价，按实验口径算「如果做了」的
     虚拟费后净收益 = (c1455×(1−0.00121) − 次日开盘×(1+0.00015)) / c1455；
     被拦腿回溯净均显著为正 = 过滤层拦错（退化信号）。

用法（需用户 Python，tushare）：
  "$DAIMON_USER_PYTHON" scripts/b7ff_daily_review.py --date 2026-09-23
  不加 --date 默认复盘昨天。次日回溯需要次日日线已出 → 建议次日盘后跑。
"""
import argparse
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

import pandas as pd              # noqa: E402
import tushare as ts             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT, "t_io", "logs")
TOKEN = "9d15f39266cbbf8a1e5efa1525d7a4d4d1dbc62ec8cbce167d642def"  # 同 fetch_warmup_minutes.py:15
SELL_FEE, BUY_FEE = 0.00121, 0.00015   # 实验/E1 费用口径
BASELINE_FILTER_RATE = 0.805           # 实验：1 − 82/421
DRIFT_ALARM = 0.20                     # 过滤率漂移告警阈值（方案 §6.2）


def ts_code_of(code):
    return f"{code}.SH" if code.startswith(("6", "5")) else f"{code}.SZ"


def load_log(date):
    path = os.path.join(LOG_DIR, f"b7ff_filter_{date}.jsonl")
    if not os.path.exists(path):
        return []
    return [json.loads(x) for x in open(path, encoding="utf-8") if x.strip()]


def next_open_map(codes, date):
    """每票 date 之后最近一个交易日的开盘价（tushare daily）。"""
    out = {}
    for c in sorted(codes):
        try:
            df = pro.daily(ts_code=ts_code_of(c), start_date=date.replace("-", ""),
                           limit=5)
            if df is None or df.empty:
                continue
            df = df.sort_values("trade_date")
            nxt = df[df["trade_date"] > date.replace("-", "")]
            if not nxt.empty:
                out[c] = float(nxt.iloc[0]["open"])
        except Exception as e:
            print(f"  [回溯] {c} 日线拉取失败: {repr(e)[:80]}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="复盘日 YYYY-MM-DD（默认昨天）")
    args = ap.parse_args()
    date = args.date or str(pd.Timestamp.now().normalize()
                            - pd.Timedelta(days=1))[:10]

    recs = load_log(date)
    if not recs:
        print(f"[{date}] 无过滤层日志（开关 off 或当日无 14:55 评估）: "
              f"{os.path.join(LOG_DIR, f'b7ff_filter_{date}.jsonl')}")
        return 0

    n = len(recs)
    n_pass = sum(1 for r in recs if r.get("decision") == "pass")
    n_block = sum(1 for r in recs if r.get("decision") == "block")
    n_na = sum(1 for r in recs if r.get("decision") == "na_allow")
    n_guard_fail = sum(1 for r in recs
                       if (r.get("guard") or {}).get("guard") == "align_fail")
    # 过滤率只对「信号腿」（tail30 达标）有意义
    sig_legs = [r for r in recs
                if r.get("tail30_pct") is not None and r["tail30_pct"] > 1.0]
    sig_block = [r for r in sig_legs if r.get("decision") == "block"]
    sig_pass = [r for r in sig_legs if r.get("decision") == "pass"]
    rate = (len(sig_block) / len(sig_legs)) if sig_legs else None

    print(f"══ B7 过滤层日复盘 {date} ══")
    print(f"评估条目={n}  pass={n_pass}  block={n_block}  na_allow={n_na}  "
          f"守卫失败={n_guard_fail}")
    if rate is not None:
        drift = abs(rate - BASELINE_FILTER_RATE)
        flag = " ⚠️漂移告警" if drift > DRIFT_ALARM else ""
        print(f"信号腿={len(sig_legs)}  拦截={len(sig_block)}  放行={len(sig_pass)}  "
              f"过滤率={rate:.1%}（基线 {BASELINE_FILTER_RATE:.1%}，漂移 {drift:.1%}）{flag}")
    else:
        print("当日无 tail30 达标信号腿，过滤率不适用")

    # 被拦腿次日回溯（需要 c1455 + 次日开盘价）
    blocked = [r for r in sig_block if r.get("c1455")]
    if not blocked:
        print("无被拦信号腿，回溯跳过")
        return 0
    opens = next_open_map({r["code"] for r in blocked}, date)
    nets = []
    print("── 被拦腿回溯（如果做了的虚拟费后净收益）──")
    for r in blocked:
        op = opens.get(r["code"])
        if op is None:
            print(f"  {r['code']}: 次日开盘价缺失，跳过")
            continue
        sell = float(r["c1455"])
        net = (sell * (1 - SELL_FEE) - op * (1 + BUY_FEE)) / sell
        nets.append(net)
        print(f"  {r['code']}: z={r.get('z')} c1455={sell} 次日开={op} "
              f"虚拟净={net * 100:+.3f}% {'←拦错(少赚)' if net > 0 else '←拦对(避亏)'}")
    if nets:
        mean_net = sum(nets) / len(nets)
        verdict = ("⚠️ 被拦腿虚拟净均为正，过滤层可能拦错，需复盘"
                   if mean_net > 0 else "✅ 被拦腿虚拟净均 ≤0，拦截方向正确")
        print(f"被拦腿 n={len(nets)} 虚拟净均={mean_net * 100:+.3f}% → {verdict}")
    return 0


if __name__ == "__main__":
    ts.set_token(TOKEN)
    pro = ts.pro_api()
    raise SystemExit(main())
