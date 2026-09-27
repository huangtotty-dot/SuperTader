# -*- coding: utf-8 -*-
"""equity_daily.py — 每日净值/αβ 计算（2026-09-27 复盘清单 V2.0 §八）。

口径（钉死，不得改）：
- 净值法：equity = 持仓市值(EOD 收盘重估) + 现金；禁止持仓市值法冒充净值法。
- account_ret = (equity - flow_adjust) / prev_equity - 1；alpha = account_ret - benchmark_ret。
- 主基准沪深300 sh000300，辅基准科创50 sh000688；费后统一（t0_realized 取费后 est_pnl）。
- cash 取不到时 equity/account_ret/alpha 置 null 并在 source 标注，不许硬算。

产出：t_io/metrics/equity_daily_{date}.json（幂等，重复跑覆盖）。

CLI：python scripts/equity_daily.py --date YYYY-MM-DD
"""
import argparse
import glob
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATE_DIR = os.path.join(_BASE, "t_io", "state")
_METRICS_DIR = os.path.join(_BASE, "t_io", "metrics")
_KLINE_DIR = os.path.join(_BASE, "t_io", "cache", "daily_kline")
_CLOSURE_AUDIT = os.path.join(_BASE, "t_io", "logs", "closure_audit.jsonl")

_BENCH_MAIN = "sh000300"   # 沪深300（主基准）
_BENCH_AUX = "sh000688"    # 科创50（辅基准）

sys.path.insert(0, _BASE)
from core.market_data.tencent_provider import TencentProvider  # noqa: E402


def _load_json(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def force_refresh_index(index: str):
    """强制刷新指数日线缓存（根治「用到才拉」停在旧日期的问题）：
    先备份并删除缓存文件再调 index_daily，绕过 weekday/09:15 的 need_refresh 门控，
    保证每次都发起网络拉取；网络失败则恢复旧缓存兜底。返回 (df, note)。"""
    idx = str(index).lower()
    cache_fp = os.path.join(_KLINE_DIR, f"index_{idx}.json")
    bak = None
    if os.path.exists(cache_fp):
        try:
            with open(cache_fp, encoding="utf-8") as f:
                bak = f.read()
            os.remove(cache_fp)
        except Exception:
            bak = None
    df = TencentProvider().index_daily(idx)
    if df.empty and bak is not None:
        try:
            with open(cache_fp, "w", encoding="utf-8") as f:
                f.write(bak)
        except Exception:
            pass
        df = TencentProvider().index_daily(idx)
        return df, f"{idx}:网络拉取失败,回退旧缓存"
    last = str(df.iloc[-1]["date"]) if not df.empty else "无数据"
    return df, f"{idx}:强制刷新生效,缓存末行={last}"


def index_ret(df, date: str):
    """指数日收益 = close(date)/close(前一交易日) - 1。无当日K线返回 None。"""
    if df is None or df.empty:
        return None, "指数缓存为空"
    rows = [(str(r.date), float(r.close)) for r in df.itertuples()]
    rows.sort(key=lambda x: x[0])
    for i, (d, c) in enumerate(rows):
        if d == date:
            if i == 0:
                return None, "无前一日K线"
            return c / rows[i - 1][1] - 1.0, f"ok({rows[i-1][0]}→{d})"
    return None, f"指数无{date}当日K线(末行={rows[-1][0]})"


def market_value_from_snapshot(snap):
    """快照市值 = Σ qty*price（快照 price 即 EOD 收盘重估价）。"""
    mv, n = 0.0, 0
    for h in snap.get("holdings", []):
        qty, price = h.get("qty") or 0, h.get("price")
        if qty and price:
            mv += qty * price
            n += 1
    return mv, n


def _kline_close(code: str, date: str):
    """个股缓存收盘价：优先当日；非交易日向前携带最近收盘价。返回 (close, used_date)。"""
    fp = os.path.join(_KLINE_DIR, f"{code}.json")
    if not os.path.exists(fp):
        return None, None
    try:
        rows = _load_json(fp).get("rows") or []
    except Exception:
        return None, None
    best = None
    for r in rows:
        d = str(r.get("date", ""))
        if d <= date:
            best = (d, float(r["close"]))
        if d == date:
            return float(r["close"]), d
    return (best[1], best[0]) if best else (None, None)


def market_value_from_holdings_json(date: str):
    """兜底：holdings.json 当前持仓 + daily_kline 缓存收盘价重估（当日缺则向前携带）。"""
    hp = os.path.join(_STATE_DIR, "holdings.json")
    holdings = _load_json(hp)
    mv, n, carried, missing = 0.0, 0, 0, []
    for code, h in holdings.items():
        qty = h.get("qty") or 0
        if not qty:
            continue
        close, used = _kline_close(str(code).split("_")[0], date)
        if close is None:
            missing.append(code)
            continue
        mv += qty * close
        n += 1
        if used != date:
            carried += 1
    return mv, n, carried, missing


def t0_realized(date: str):
    """closure_audit.jsonl 当日记录 est_pnl 合计（费后口径）；无记录返回 None。"""
    if not os.path.exists(_CLOSURE_AUDIT):
        return None, 0
    total, cnt = 0.0, 0
    with open(_CLOSURE_AUDIT, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("date") != date:
                continue
            cnt += 1
            for d in rec.get("details") or []:
                total += float(d.get("est_pnl") or 0.0)
    return (total if cnt else None), cnt


def find_prev_equity(date: str):
    """最近一个 equity 非 null 的历史文件（早于当日）。"""
    fps = sorted(glob.glob(os.path.join(_METRICS_DIR, "equity_daily_*.json")))
    for fp in reversed(fps):
        d = os.path.basename(fp)[len("equity_daily_"):-len(".json")]
        if d >= date:
            continue
        try:
            eq = _load_json(fp).get("equity")
        except Exception:
            continue
        if eq is not None:
            return float(eq), d
    return None, None


def compute(date: str) -> dict:
    notes = []
    # 1) 基准强制刷新（先于一切计算）
    df_main, note1 = force_refresh_index(_BENCH_MAIN)
    df_aux, note2 = force_refresh_index(_BENCH_AUX)
    notes += [note1, note2]
    benchmark_ret, b_note = index_ret(df_main, date)
    benchmark_aux_ret, ba_note = index_ret(df_aux, date)
    notes.append(f"主基准sh000300:{b_note};辅基准sh000688:{ba_note}")

    # 2) 持仓市值：优先 eod=true 快照；否则快照存在仍用快照价(EOD重估)；无快照回退 holdings.json+K线
    snap_fp = os.path.join(_STATE_DIR, f"holdings_daily_{date}.json")
    cash = None
    if os.path.exists(snap_fp):
        snap = _load_json(snap_fp)
        market_value, n = market_value_from_snapshot(snap)
        cash = snap.get("cash")
        tag = "eod=true快照" if snap.get("eod") is True else f"live快照(updated_at={snap.get('updated_at')},未标eod)"
        notes.append(f"市值=holdings_daily({tag},{n}只)")
    else:
        market_value, n, carried, missing = market_value_from_holdings_json(date)
        extra = f",向前携带收盘{carried}只" if carried else ""
        if missing:
            extra += f",无缓存剔除{len(missing)}只:{','.join(missing[:5])}"
        notes.append(f"市值=holdings.json+K线重估({n}只{extra})")

    # 3) 净值法：cash 缺失 → equity/account_ret/alpha 全 null（禁止市值法冒充）
    if cash is not None:
        equity = market_value + float(cash)
    else:
        equity = None
        notes.append("cash=null(快照无cash字段)→equity/account_ret/alpha置null,禁止市值法硬算")

    # 4) prev_equity / account_ret / alpha
    prev_equity, prev_date = find_prev_equity(date)
    if prev_equity is not None:
        notes.append(f"prev_equity取自{prev_date}")
    flow_adjust = 0.0
    account_ret = None
    if equity is not None and prev_equity:
        account_ret = (equity - flow_adjust) / prev_equity - 1.0
    alpha = None
    if account_ret is not None and benchmark_ret is not None:
        alpha = account_ret - benchmark_ret

    # 5) T0 已实现盈亏（费后）
    t0, t0_cnt = t0_realized(date)
    notes.append(f"t0_realized={'无记录' if t0_cnt == 0 else f'{t0:.2f}({t0_cnt}条审计)'}")

    return {
        "date": date,
        "equity": equity,
        "cash": cash,
        "market_value": round(market_value, 2),
        "prev_equity": prev_equity,
        "account_ret": account_ret,
        "benchmark_ret": benchmark_ret,
        "benchmark_aux_ret": benchmark_aux_ret,
        "alpha": alpha,
        "t0_realized": t0,
        "flow_adjust": flow_adjust,
        "source": " | ".join(notes),
    }


def main():
    ap = argparse.ArgumentParser(description="每日净值/αβ 计算（净值法，费后）")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = ap.parse_args()
    out = compute(args.date)
    os.makedirs(_METRICS_DIR, exist_ok=True)
    fp = os.path.join(_METRICS_DIR, f"equity_daily_{args.date}.json")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"[equity_daily] {args.date} 已落盘: {fp}")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
