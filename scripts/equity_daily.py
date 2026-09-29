# -*- coding: utf-8 -*-
"""equity_daily.py — 每日净值/αβ 计算（2026-09-27 复盘清单 V2.0 §八；2026-09-29 口径切换 C-06）。

口径（钉死，不得改）：
- 跟踪对象 = 仿真账户（GM 掘金模拟账户，策略验证对象），不再用实盘台账。
- 净值法：equity = Σ(持仓 qty × EOD 收盘价) + 账户现金；禁止持仓市值法冒充净值法。
- 主数据源 = t_io/bridge/heartbeat_YYYY-MM-DD.jsonl 当日最后一行
  （cash: float；positions: {gm_symbol: {qty, cost}}，gm_symbol 形如 "SHSE.588170"→取点号后 6 位码）。
- EOD 重估价：优先 t_io/cache/daily_kline/{code}.json 当日 close；缓存缺失/陈旧回退腾讯日线；
  仍无则在 source 标注缺失票并剔除（不硬算）。
- account_ret = (equity - flow_adjust) / prev_equity - 1；alpha = account_ret - benchmark_ret。
- 主基准沪深300 sh000300，辅基准科创50 sh000688；费后统一（t0_realized 取费后 est_pnl）。
- heartbeat 缺失 / positions 为空 / cash 缺失时 equity/account_ret/alpha 置 null 并在 source 标注，不许硬算。
- prev_equity 链只接续仿真口径文件（source 含「仿真账户口径」标记）；旧实盘口径文件不可接续。
- 探针遗留仓兼容（一次性）：slippage_probe/state.json 的 base 持仓（300166×8400 + 603629×1000）
  仅在 [base 建仓日, 2026-09-29 清退日] 窗口内补入市值（heartbeat 只含 AUTO_POOL 引擎跟踪票），source 标注。

产出：t_io/metrics/equity_daily_{date}.json（幂等，重复跑覆盖）。

CLI：
  python scripts/equity_daily.py --date YYYY-MM-DD     # 单日计算
  python scripts/equity_daily.py --backfill            # 仿真口径回填：逐日重算全部有 heartbeat 的历史
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
_BRIDGE_DIR = os.path.join(_BASE, "t_io", "bridge")
_CLOSURE_AUDIT = os.path.join(_BASE, "t_io", "logs", "closure_audit.jsonl")
_PROBE_STATE = os.path.join(_BASE, "t_io", "validation", "slippage_probe", "state.json")

_BENCH_MAIN = "sh000300"   # 沪深300（主基准）
_BENCH_AUX = "sh000688"    # 科创50（辅基准）

# 仿真口径标记：prev_equity 链只认带此标记的历史文件（旧实盘口径不可接续）
_SIM_TAG = "仿真账户口径"
# 探针遗留仓清退日（约定 2026-09-30 上午清退，最后持有日 09-29）
_PROBE_CLEAR_DATE = "2026-09-29"

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


def _tencent_close(code: str, date: str):
    """腾讯日线回退：拉取（并刷新缓存）后取 date 当日收盘。返回 (close, used_date) 或 (None, None)。"""
    try:
        df = TencentProvider().daily(code)
    except Exception:
        return None, None
    if df is None or df.empty:
        return None, None
    best = None
    for r in df.itertuples():
        d = str(r.date)
        if d <= date:
            best = (d, float(r.close))
        if d == date:
            return float(r.close), d
    return (best[1], best[0]) if best else (None, None)


def _eod_close(code: str, date: str):
    """EOD 重估价解析：缓存当日 → 腾讯日线 → 缓存向前携带 → None。
    返回 (close, via)；via ∈ {cache, tencent, cache-carry(used_date), None}。"""
    close, used = _kline_close(code, date)
    if close is not None and used == date:
        return close, "cache"
    # 缓存缺失或陈旧（无当日行）→ 腾讯日线回退
    t_close, t_used = _tencent_close(code, date)
    if t_close is not None and t_used == date:
        return t_close, "tencent"
    if t_close is not None:
        return t_close, f"tencent-carry({t_used})"
    if close is not None:
        return close, f"cache-carry({used})"
    return None, None


def heartbeat_tail(date: str):
    """heartbeat_YYYY-MM-DD.jsonl 当日最后一行（末行非空且可解析）。无文件/解析失败返回 None。"""
    fp = os.path.join(_BRIDGE_DIR, f"heartbeat_{date}.jsonl")
    if not os.path.exists(fp):
        return None
    last = None
    with open(fp, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                last = json.loads(line)
            except Exception:
                continue
    return last


def probe_addon(date: str, hb_codes: set):
    """探针遗留仓一次性兼容：state.json 的 base 持仓在 [建仓日, 清退日] 窗口内补入市值。
    返回 (mv, parts, missing)；窗口外/无 state.json 返回 (0.0, [], [])。"""
    if not os.path.exists(_PROBE_STATE):
        return 0.0, [], []
    try:
        st = _load_json(_PROBE_STATE)
    except Exception:
        return 0.0, [], []
    base = st.get("base") or {}
    if not base:
        return 0.0, [], []
    start = st.get("_snap_date") or st.get("_base_attempt_date")
    if not start or not (str(start) <= date <= _PROBE_CLEAR_DATE):
        return 0.0, [], []
    mv, parts, missing = 0.0, [], []
    for code, qty in base.items():
        code = str(code).split("_")[0].split(".")[-1]
        qty = int(qty or 0)
        if not qty or code in hb_codes:
            continue
        close, via = _eod_close(code, date)
        if close is None:
            missing.append(code)
            continue
        mv += qty * close
        parts.append(f"{code}×{qty}@{close}({via})")
    return mv, parts, missing


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
    """最近一个 equity 非 null 的仿真口径历史文件（早于当日）。旧实盘口径文件跳过（不可接续）。"""
    fps = sorted(glob.glob(os.path.join(_METRICS_DIR, "equity_daily_*.json")))
    for fp in reversed(fps):
        d = os.path.basename(fp)[len("equity_daily_"):-len(".json")]
        if d >= date:
            continue
        try:
            rec = _load_json(fp)
        except Exception:
            continue
        if _SIM_TAG not in str(rec.get("source") or ""):
            continue
        eq = rec.get("equity")
        if eq is not None:
            return float(eq), d
    return None, None


def compute(date: str, prev_override=None, extra_note: str = None) -> dict:
    """单日计算。prev_override=(value, date) 时跳过文件链（回填模式内部链接用）。"""
    notes = [_SIM_TAG + "(heartbeat尾行+K线重估)"]
    if extra_note:
        notes.append(extra_note)
    # 1) 基准强制刷新（先于一切计算）
    df_main, note1 = force_refresh_index(_BENCH_MAIN)
    df_aux, note2 = force_refresh_index(_BENCH_AUX)
    notes += [note1, note2]
    benchmark_ret, b_note = index_ret(df_main, date)
    benchmark_aux_ret, ba_note = index_ret(df_aux, date)
    notes.append(f"主基准sh000300:{b_note};辅基准sh000688:{ba_note}")

    # 2) 仿真账户持仓市值 + 现金：heartbeat 当日末行 positions/cash + K线 EOD 重估
    hb = heartbeat_tail(date)
    cash, market_value = None, None
    if hb is None:
        notes.append(f"无heartbeat_{date}.jsonl(仿真口径主数据源缺失)→equity/account_ret/alpha置null,不硬算")
    else:
        positions = hb.get("positions") or {}
        raw_cash = hb.get("cash")
        cash = float(raw_cash) if isinstance(raw_cash, (int, float)) else None
        if not positions:
            notes.append("heartbeat末行positions为空→equity/account_ret/alpha置null,杜绝现金流静默错误")
        else:
            mv, n, missing, via_n = 0.0, 0, [], {}
            hb_codes = set()
            for gm_sym, p in positions.items():
                code = str(gm_sym).split(".")[-1].split("_")[0]
                hb_codes.add(code)
                qty = (p or {}).get("qty") or 0
                if not qty:
                    continue
                close, via = _eod_close(code, date)
                if close is None:
                    missing.append(code)
                    continue
                mv += qty * close
                n += 1
                via_n[via] = via_n.get(via, 0) + 1
            # 探针遗留仓兼容补丁（仅 ≤ 清退日窗口）
            p_mv, p_parts, p_missing = probe_addon(date, hb_codes)
            if p_parts:
                mv += p_mv
                notes.append(f"探针遗留仓补入{len(p_parts)}笔:{','.join(p_parts)}")
            missing += p_missing
            via_s = ",".join(f"{k}×{v}" for k, v in sorted(via_n.items()))
            market_value = mv
            note = f"市值=heartbeat尾行{n}只×EOD收盘({via_s})"
            if missing:
                note += f",无收盘价剔除{len(missing)}只:{','.join(missing[:5])}"
            notes.append(note)
            notes.append(f"cash={cash:.2f}(heartbeat末行)" if cash is not None
                         else "cash缺失(heartbeat末行无cash字段)→equity/account_ret/alpha置null,不硬算")

    # 3) 净值法：cash/市值缺失 → equity/account_ret/alpha 全 null（禁止市值法冒充）
    if cash is not None and market_value is not None:
        equity = market_value + float(cash)
    else:
        equity = None

    # 4) prev_equity / account_ret / alpha
    if prev_override is not None:
        prev_equity, prev_date = prev_override
        if prev_equity is not None:
            notes.append(f"prev_equity取自{prev_date}(回填链)")
    else:
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
        "market_value": round(market_value, 2) if market_value is not None else None,
        "prev_equity": prev_equity,
        "account_ret": account_ret,
        "benchmark_ret": benchmark_ret,
        "benchmark_aux_ret": benchmark_aux_ret,
        "alpha": alpha,
        "t0_realized": t0,
        "flow_adjust": flow_adjust,
        "source": " | ".join(notes),
    }


def _write(out: dict):
    os.makedirs(_METRICS_DIR, exist_ok=True)
    fp = os.path.join(_METRICS_DIR, f"equity_daily_{out['date']}.json")
    with open(fp, "w", encoding="utf-8") as f:
        f.write(json.dumps(out, ensure_ascii=False, indent=2))
    return fp


def backfill():
    """仿真口径回填：逐日重算全部有 heartbeat 的历史并覆写，prev_equity 在回填序列内部链接。"""
    fps = sorted(glob.glob(os.path.join(_BRIDGE_DIR, "heartbeat_*.jsonl")))
    dates = [os.path.basename(fp)[len("heartbeat_"):-len(".jsonl")] for fp in fps]
    prev = (None, None)
    done = []
    for d in dates:
        out = compute(d, prev_override=prev, extra_note="仿真口径回填")
        fp = _write(out)
        done.append((d, out.get("equity"), out.get("alpha")))
        print(f"[backfill] {d} → {fp} equity={out.get('equity')} alpha={out.get('alpha')}", flush=True)
        if out.get("equity") is not None:
            prev = (float(out["equity"]), d)
    print(f"[backfill] 完成 {len(done)} 日: {dates[0] if dates else '—'} ~ {dates[-1] if dates else '—'}")
    return done


def main():
    ap = argparse.ArgumentParser(description="每日净值/αβ 计算（仿真账户口径，净值法，费后）")
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--backfill", action="store_true", help="仿真口径回填全部 heartbeat 历史")
    args = ap.parse_args()
    if args.backfill:
        backfill()
        return
    if not args.date:
        ap.error("--date 或 --backfill 必填其一")
    out = compute(args.date)
    fp = _write(out)
    print(f"[equity_daily] {args.date} 已落盘: {fp}")
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
