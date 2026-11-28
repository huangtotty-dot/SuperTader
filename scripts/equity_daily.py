# -*- coding: utf-8 -*-
"""equity_daily.py — 每日净值/αβ 计算（2026-09-27 复盘清单 V2.0 §八；2026-09-29 口径切换 C-06）。

口径（钉死，不得改）：
- 跟踪对象 = 仿真账户（GM 掘金模拟账户，策略验证对象），不再用实盘台账。
- 净值法：equity = Σ(持仓 qty × EOD 收盘价) + 账户现金；禁止持仓市值法冒充净值法。
- 主数据源 = t_io/bridge/heartbeat_YYYY-MM-DD.jsonl 当日最后一行
  （cash: float；positions: {gm_symbol: {qty, cost}}，gm_symbol 形如 "SHSE.588170"→取点号后 6 位码）。
- EOD 重估价：缓存须通过 saved_at、OHLC、forming、成交观测最高价校验；运行日的缓存当日行
  另须带受控 ``eod_verified`` 证明。缓存不可用时回退腾讯日线/quote；仅剩残缺缓存值时保留
  ``stale_bar_warn`` 诊断，但不得进入正式净值。
- 任一持仓缺合法 EOD 价：``data_status=partial_position_valuation``，正式
  ``market_value/equity/account_ret/alpha`` 全为 null；缺票列入 ``missing_prices``，已定价部分只写入
  ``partial_market_value`` 供诊断，禁止把现金+部分市值冒充完整净值。
- account_ret = (equity - flow_adjust) / prev_equity - 1；alpha = account_ret - benchmark_ret。
- 主基准沪深300 sh000300，辅基准科创50 sh000688；费后统一（t0_realized 取费后 est_pnl）。
- heartbeat 缺失 / positions 字段缺失 / cash 缺失时正式收益置 null；``positions={}`` 是合法空仓，
  市值=0、equity=cash，不与数据源故障混淆。
- prev_equity 链只接续仿真口径文件（source 含「仿真账户口径」标记）；旧实盘口径文件不可接续。
- 探针遗留仓兼容（一次性）：slippage_probe/state.json 的 base 持仓（300166×8400 + 603629×1000）
  仅在 [base 建仓日, 2026-09-29 清退日] 窗口内补入市值（heartbeat 只含 AUTO_POOL 引擎跟踪票），source 标注。
- 09-09 心跳丢票补丁（一次性，owner 09-29 授权）：仅 2026-09-09 若 heartbeat 末行缺 588170
  或 qty<70000，按缺口补足 70000 股走同链路 EOD 重估（fill 事件 14:40:31 证据），source 标注。

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
from datetime import datetime

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
# 探针遗留仓清退判定：以 unwind_report 真实落地为准（原固定截止日 09-30 实证失效——
# 09-30 上午清退未执行，遗留仓仍在账户里，不补则市值漏计 ~19.7 万）
_PROBE_STATE_DIR = os.path.dirname(_PROBE_STATE)


def _probe_unwound_upto(date: str):
    """返回最后一个含 sell 动作的 unwind_report 日期（≤date）；无 → None（未清退）。"""
    import glob as _g
    for fp in sorted(_g.glob(os.path.join(_PROBE_STATE_DIR, "unwind_report_*.json")),
                     reverse=True):
        d = os.path.basename(fp)[len("unwind_report_"):-len(".json")]
        if d > date:
            continue
        try:
            rep = _load_json(fp)
        except Exception:
            continue
        if any((a or {}).get("action") == "sell" for a in (rep.get("actions") or [])):
            return d
    return None

sys.path.insert(0, _BASE)
from core.market_data.tencent_provider import TencentProvider  # noqa: E402


def _load_json(fp):
    with open(fp, encoding="utf-8") as f:
        return json.load(f)


def force_refresh_index(index: str, date: str = None):
    """按目标日重取指数：统一走 GM→腾讯→东财门面，缺目标日不得冒称刷新成功。

    W48：旧实现删除缓存后直连腾讯，非空旧缓存会绕过东财备用源。现在带 ``end_date``
    发起历史口径请求（不写共享长缓存），目标日仍缺失才尝试 quote 补丁；quote 也明确
    标为待独立核验，不再输出无依据的「强制刷新生效」。"""
    from core.market_data.facade import MarketDataFacade
    idx = str(index).lower()
    try:
        df = MarketDataFacade().index_daily(idx, days=800, end_date=date, require_date=True)
    except Exception as exc:
        df = None
        source = "error:%s" % type(exc).__name__
    else:
        source = (df.attrs.get("source") if df is not None else None) or "empty"
    has_target = bool(date and df is not None and not df.empty
                      and (df["date"].astype(str).str[:10] == date).any())
    if date and not has_target:
        qdf, qnote = _quote_patch_index(idx, date)
        if qdf is not None:
            return qdf, qnote + ";目标日待独立源核验"
        return df, f"{idx}:benchmark_unavailable,目标日={date},source={source}"
    last = str(df.iloc[-1]["date"]) if df is not None and not df.empty else "无数据"
    return df, f"{idx}:source={source},目标日={date or '未指定'},末行={last}"


def _quote_patch_index(idx: str, date: str):
    """指数缓存当日行 quote 补丁。补丁成功返回 (df, note)，否则 (None, None)。"""
    if not date:
        return None, None
    try:
        from core.market_data.tencent_provider import quote_daily_bar
        bar = quote_daily_bar(idx)
    except Exception:
        return None, None
    if not bar or bar.get("date") != date:
        return None, None
    row = _quote_patch_cache_file(os.path.join(_KLINE_DIR, f"index_{idx}.json"), bar, date)
    if row is None:
        return None, None
    try:
        d = _load_json(os.path.join(_KLINE_DIR, f"index_{idx}.json"))
        import pandas as pd
        df = pd.DataFrame(d.get("rows") or [])
        return df, f"{idx}:fqkline断供→quote快照补丁当日行close={bar['close']}"
    except Exception:
        return None, None


def _quote_patch_cache_file(cache_fp: str, bar: dict, date: str):
    """把 quote bar 写进日线缓存（替换同日旧行，原子写）。成功返回行 dict，失败 None。
    副作用即收益：GUI/K线弹窗等其它读缓存处同步被修复。"""
    try:
        d = _load_json(cache_fp) if os.path.exists(cache_fp) else {"rows": []}
        rows = [r for r in (d.get("rows") or []) if str(r.get("date")) != date]
        row = {"date": bar["date"], "open": bar["open"], "close": bar["close"],
               "high": bar["high"], "low": bar["low"], "volume": bar["volume"],
               "amount": bar["amount"], "src": "tencent_quote"}
        rows.append(row)
        rows.sort(key=lambda r: str(r.get("date")))
        d["rows"] = rows
        d["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " quote_patch"
        tmp = cache_fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, cache_fp)
        return row
    except Exception:
        return None


def _quote_patch_stock(code: str, date: str):
    """个股缓存当日行 quote 补丁（候选㊳）：fqkline 断供时用 qt.gtimg.cn 快照
    补齐当日 EOD 收盘。补丁成功返回 close，否则 None（缓存同步被修复）。"""
    try:
        from core.market_data.tencent_provider import quote_daily_bar
        bar = quote_daily_bar(code)
    except Exception:
        return None
    if not bar or bar.get("date") != date:
        return None
    if _quote_patch_cache_file(os.path.join(_KLINE_DIR, f"{code}.json"), bar, date) is None:
        return None
    return float(bar["close"])


def index_ret(df, date: str):
    """指数日收益；目标日缺失或零量平线占位时 fail-closed，不算伪 α/β。"""
    if df is None or df.empty:
        return None, "benchmark_unavailable:指数缓存为空"
    from core.market_data.facade import MarketDataFacade
    if not MarketDataFacade._index_date_valid(df, date):
        return None, f"benchmark_unverified:指数无{date}有效K线或为零量占位行"
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


def _cache_today_bar(code: str, date: str):
    """缓存当日 bar、落盘时间及元数据；无缓存/无当日行返回 (None, None, None)。"""
    fp = os.path.join(_KLINE_DIR, f"{code}.json")
    if not os.path.exists(fp):
        return None, None, None
    try:
        d = _load_json(fp)
    except Exception:
        return None, None, None
    row = next((r for r in d.get("rows") or [] if str(r.get("date")) == date), None)
    return row, d.get("saved_at"), d


def _observed_day_high(code: str, date: str):
    """当日已观测最高价（成交真源：events 当日 fill 的 price/fill_vwap）。无观测返回 None。"""
    fp = os.path.join(_BRIDGE_DIR, f"events_{date.replace('-', '')}.jsonl")
    if not os.path.exists(fp):
        return None
    obs = None
    with open(fp, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            if e.get("event") != "fill" or str(e.get("code")) != code:
                continue
            for k in ("price", "fill_vwap"):
                px = e.get(k)
                if isinstance(px, (int, float)) and px > 0:
                    obs = max(obs, float(px)) if obs is not None else float(px)
    return obs


def _bar_complete(code: str, date: str, row, saved_at, cached=None) -> bool:
    """缓存当日 bar 完整性校验；非法 OHLC、forming、当日无证明一律拒绝。

    历史回填保留旧的“无法证伪则放行”口径，但仍须通过 OHLC/volume 合法性及成交观测检查。
    只有目标日就是运行日时，才额外要求受控 ``eod_verified`` 证明。
    """
    import math
    if not row:
        return False
    ts = str(saved_at or "")
    if ts and ts < f"{date} 15:00":
        return False
    meta = cached or {}
    if date in {str(x)[:10] for x in (meta.get("forming") or [])}:
        return False
    if date == datetime.now().strftime("%Y-%m-%d"):
        verified_raw = meta.get("eod_verified") or []
        if isinstance(verified_raw, dict):
            verified = {str(k)[:10] for k, v in verified_raw.items() if v}
        else:
            verified = {str(x)[:10] for x in verified_raw}
        if date not in verified:
            return False
    try:
        o = float(row.get("open"))
        h = float(row.get("high"))
        low = float(row.get("low"))
        close = float(row.get("close"))
        vol = float(row.get("volume") or 0)
        vals = (o, h, low, close, vol)
        if not all(math.isfinite(v) for v in vals) or min(o, h, low, close) <= 0 or vol < 0:
            return False
        if h < max(o, close, low) or low > min(o, close, h):
            return False
    except (TypeError, ValueError):
        return False
    obs = _observed_day_high(code, date)
    return not (obs is not None and h < obs - 1e-9)


def _force_refresh_daily(code: str):
    """残缺 bar 强制换源：备份→删缓存→daily()（绕过 daily_cache 15 分钟新鲜度门控）→
    网络失败恢复旧缓存兜底。与 force_refresh_index 同 pattern。"""
    cache_fp = os.path.join(_KLINE_DIR, f"{code}.json")
    bak = None
    if os.path.exists(cache_fp):
        try:
            with open(cache_fp, encoding="utf-8") as f:
                bak = f.read()
            os.remove(cache_fp)
        except Exception:
            bak = None
    try:
        df = TencentProvider().daily(code)
    except Exception:
        df = None
    if (df is None or df.empty) and bak is not None:
        try:
            with open(cache_fp, "w", encoding="utf-8") as f:
                f.write(bak)
        except Exception:
            pass
    return df


def _tencent_close(code: str, date: str, force: bool = False):
    """腾讯日线回退：拉取（并刷新缓存）后取 date 当日收盘。返回 (close, used_date) 或 (None, None)。
    force=True 时绕过本地缓存新鲜度门控强制网络重取（残缺 bar 换源用）。"""
    if force:
        df = _force_refresh_daily(code)
    else:
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
    """EOD 重估价解析：缓存当日(须过 bar 完整性校验) → 腾讯日线 → 缓存向前携带 → None。
    返回 (close, via)；via ∈ {cache, tencent, tencent(stale_bar_warn…), stale_bar_warn:cache(…),
    cache-carry(used_date), None}。残缺 bar 换源失败时带 stale_bar_warn 告警沿用并在 source 可见。"""
    close, used = _kline_close(code, date)
    if close is not None and used == date:
        row, saved_at, cached = _cache_today_bar(code, date)
        if _bar_complete(code, date, row, saved_at, cached):
            return close, "cache"
        # 残缺 bar 冒充 EOD（2026-10-19 600127 事故）→ 强制腾讯重取；仍不行带告警沿用
        obs = _observed_day_high(code, date)
        warn = f"stale_bar_warn:cache(saved_at={saved_at},high={(row or {}).get('high')},obs={obs})"
        t_close, t_used = _tencent_close(code, date, force=True)
        if t_close is not None and t_used == date:
            return t_close, f"tencent({warn})"
        if t_close is not None:
            return t_close, f"tencent-carry({t_used},{warn})"
        q = _quote_patch_stock(code, date)  # 候选㊳: fqkline 断供 → qt 快照兜底
        if q is not None:
            return q, f"quote({warn})"
        return close, warn
    # 缓存缺失或陈旧（无当日行）→ 腾讯日线回退
    t_close, t_used = _tencent_close(code, date)
    if t_close is not None and t_used == date:
        return t_close, "tencent"
    if t_close is not None:
        return t_close, f"tencent-carry({t_used})"
    q = _quote_patch_stock(code, date)  # 候选㊳: fqkline 断供 → qt 快照兜底
    if q is not None:
        return q, "quote"
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
    if not start or str(start) > date:
        return 0.0, [], []
    if _probe_unwound_upto(date):
        return 0.0, [], []   # 已清退（有 sell 报告的 unwind_report 落地），不再补
    mv, parts, missing = 0.0, [], []
    for code, qty in base.items():
        code = str(code).split("_")[0].split(".")[-1]
        qty = int(qty or 0)
        if not qty or code in hb_codes:
            continue
        close, via = _eod_close(code, date)
        if close is None or str(via or "").startswith("stale_bar_warn"):
            missing.append(code)
            continue
        mv += qty * close
        parts.append(f"{code}×{qty}@{close}({via})")
    return mv, parts, missing


def patch_0909_addon(date: str, positions: dict):
    """09-09 心跳丢票一次性补丁（owner 2026-09-29 授权）：
    events_20260909.jsonl 14:40:31 有 fill BUY 588170×70000@0.917（pos_after=70000，≈6.4万），
    但 heartbeat 当日末行 positions 漏掉该票 → 市值少计，09-09/09-10 α 呈 V 形假象。
    仅对 date=="2026-09-09"：若末行 positions 缺 588170 或 qty<70000，按缺口补足 70000 股，
    走与主流程同链路 _eod_close 重估。与既有持仓去重、与 probe_addon 正交。
    返回 (mv, part, missing, via)；不触发返回 (0.0, None, [], None)。"""
    if date != "2026-09-09":
        return 0.0, None, [], None
    target = 70000
    have = 0
    for gm_sym, p in (positions or {}).items():
        code = str(gm_sym).split(".")[-1].split("_")[0]
        if code == "588170":
            have = int((p or {}).get("qty") or 0)
            break
    deficit = target - have
    if deficit <= 0:
        return 0.0, None, [], None
    close, via = _eod_close("588170", date)
    if close is None:
        return 0.0, None, ["588170"], None
    return deficit * close, f"09-09心跳丢票补丁:588170×{deficit}@{close}({via},fill事件14:40:31)", [], via


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


def _apply_fill_overlay(date: str, positions: dict):
    """fill 回报覆盖 heartbeat 台账（就地修改 positions）。返回 (overlay_notes, ev_name)。

    依据（2026-09-30 复盘实证）：heartbeat positions 是引擎台账，ALIGN 卖出曾不扣减
    （d97cdf0 前的 bug）⇒ 台账残影压住账户真值；fill 事件的 pos_after 来自 GM 成交回报，
    是真值 ⇒ 以末笔 fill 覆盖。
    P0(2026-10-13 实证)：台账还会**丢新票**——10-12 早盘买入 605198×1800/688003×1700，
    全天 heartbeat positions={}（同 09-09 丢 588170 一类，现金流水与 fill 完全吻合）。
    两处加固：
      ① 覆盖对象从「仅台账已有票」扩到「fill 证实但台账缺失的票补入」；
      ② events 取 ≤date 全部文件按时间序合并「逐票末笔 fill」（10-13 实证：当日 events
         只有 risk 记录零 fill，持仓只经 fill 变动 ⇒ 逐票最近一次 fill 的 pos_after
         仍是该票真值；10-13 的持仓真值即 10-12 末笔 fill 状态）。
    """
    _ev_day = date.replace("-", "")
    _ev_fps = sorted(fp for fp in glob.glob(os.path.join(_BRIDGE_DIR, "events_*.jsonl"))
                     if os.path.basename(fp)[len("events_"):-len(".jsonl")] <= _ev_day)
    last_fill = {}   # code → (pos_after, src_file)：逐票最近一次 fill（后文件覆盖先文件）
    for ev_fp in _ev_fps:
        try:
            with open(ev_fp, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    if e.get("event") == "fill" and e.get("pos_after") is not None:
                        last_fill[str(e.get("code"))] = (int(e.get("pos_after")),
                                                         os.path.basename(ev_fp))
        except Exception:
            continue
    overlay = []
    src_name = None
    for c, (pa, src) in last_fill.items():
        hit = None
        for gs in list(positions.keys()):
            if str(gs).split(".")[-1].split("_")[0] == c:
                hit = gs
                break
        if hit is None:
            if pa > 0:  # fill 证实持仓但台账缺失 → 补入（丢票修复，qty 是唯一被消费字段）
                mkt = "SHSE" if c[0] in "56" else "SZSE"
                positions[f"{mkt}.{c}"] = {"qty": pa, "cost": 0.0}
                overlay.append(f"{c}:台账缺失→补入{pa}")
                src_name = max(src_name, src) if src_name else src
            continue
        old = int((positions[hit] or {}).get("qty") or 0)
        if pa != old:
            overlay.append(f"{c}:{old}→{pa}")
            src_name = max(src_name, src) if src_name else src
            if pa > 0:
                positions[hit] = {**(positions[hit] or {}), "qty": pa}
            else:
                positions.pop(hit, None)
    return overlay, src_name


def _reject_unfreeze_cash(date: str, hb: dict):
    """收盘后拒单/撤单解冻回补（2026-10-14 实弹事故根修）。

    事故：OSC 两笔限价买单（600097×9800@10.20、000823×4000@24.55）全天未成交、
    ≈19.8 万冻结，15:00 才被拒（status=12），而 heartbeat 末行现金快照写在 14:58
    → 现金仍含冻结 → equity 虚亏 43.5%（真实约 -1.1%）。
    口径（钉死）：
    - 仅当拒单/撤单事件的 _ts 晚于 heartbeat 末行 _ts（解冻发生在现金快照之后）
      才回补 qty×委托价；早于快照的拒单（S1 时代 status=8 盘中即时拒单，资金当时
      即解冻、末行现金已含）不回补 → 天然幂等、不重补旧事件。
    - 只回补 BUY 侧（SELL 拒单解冻的是持仓不是现金）。
    - 委托价取自当日同 code BUY order 事件；无 order 记录/无价格 → 跳过不硬算。
    返回 (amount, parts)。
    """
    hb_ts = (hb or {}).get("_ts")
    if not isinstance(hb_ts, (int, float)):
        return 0.0, []
    fp = os.path.join(_BRIDGE_DIR, f"events_{date.replace('-', '')}.jsonl")
    if not os.path.exists(fp):
        return 0.0, []

    def _is_buy(e):
        side = e.get("side")
        if side:
            return side == "BUY"
        raw = e.get("raw")
        if isinstance(raw, str):  # raw 是 GM 回报 dict 的 repr，side:1=BUY
            try:
                import ast
                return (ast.literal_eval(raw) or {}).get("side") == 1
            except Exception:
                return False
        return False

    order_px = {}   # code → 最近一次 BUY 委托价
    restores = {}   # code → 收盘后被拒/撤未成交 qty 累计
    with open(fp, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            code = str(e.get("code") or "")
            if not code:
                continue
            if e.get("event") == "order" and _is_buy(e):
                px = e.get("price")
                if isinstance(px, (int, float)) and px > 0:
                    order_px[code] = float(px)
            elif (e.get("event") in ("reject", "cancel") and _is_buy(e)
                  and isinstance(e.get("_ts"), (int, float)) and e["_ts"] > hb_ts):
                restores[code] = restores.get(code, 0) + int(e.get("qty") or 0)
    amount, parts = 0.0, []
    for code, qty in sorted(restores.items()):
        px = order_px.get(code)
        if not qty or px is None:
            continue
        amount += qty * px
        parts.append(f"{code}×{qty}@{px}")
    return amount, parts


def compute(date: str, prev_override=None, extra_note: str = None) -> dict:
    """单日计算；任一持仓缺合法 EOD 价时，正式净值/收益/α 全部 fail-closed。"""
    notes = [_SIM_TAG + "(heartbeat尾行+K线重估)"]
    if extra_note:
        notes.append(extra_note)
    df_main, note1 = force_refresh_index(_BENCH_MAIN, date)
    df_aux, note2 = force_refresh_index(_BENCH_AUX, date)
    notes += [note1, note2]
    benchmark_ret, b_note = index_ret(df_main, date)
    benchmark_aux_ret, ba_note = index_ret(df_aux, date)
    notes.append(f"主基准sh000300:{b_note};辅基准sh000688:{ba_note}")

    hb = heartbeat_tail(date)
    cash, market_value, partial_market_value = None, None, None
    missing_prices = []
    if hb is None:
        notes.append(f"无heartbeat_{date}.jsonl(仿真口径主数据源缺失)→equity/account_ret/alpha置null,不硬算")
    else:
        _raw_pos = hb.get("positions", None)
        pos_field_missing = _raw_pos is None
        positions = _raw_pos or {}
        raw_cash = hb.get("cash")
        cash = float(raw_cash) if isinstance(raw_cash, (int, float)) else None
        if cash is not None:
            _unfreeze, _unfreeze_parts = _reject_unfreeze_cash(date, hb)
            if _unfreeze > 0:
                cash += _unfreeze
                notes.append(f"收盘后拒单解冻回补{len(_unfreeze_parts)}笔合计{_unfreeze:.2f}"
                             f"({','.join(_unfreeze_parts)})→cash已含解冻")
        _overlay, _ev_name = _apply_fill_overlay(date, positions)
        if _overlay:
            notes.append(f"fill回报覆盖heartbeat台账({_ev_name}):{','.join(_overlay)}")
        if pos_field_missing:
            notes.append("heartbeat末行无positions字段(数据源故障)→equity/account_ret/alpha置null,杜绝现金流静默错误")
        elif not positions:
            market_value = 0.0
            notes.append("positions={}→合法空仓态(全仓现金)，市值=0，equity=cash")
        else:
            mv, n, via_n = 0.0, 0, {}
            hb_codes = set()
            for gm_sym, p in positions.items():
                code = str(gm_sym).split(".")[-1].split("_")[0]
                hb_codes.add(code)
                qty = (p or {}).get("qty") or 0
                if not qty:
                    continue
                close, via = _eod_close(code, date)
                if close is None or str(via or "").startswith("stale_bar_warn"):
                    missing_prices.append(code)
                    if close is not None:
                        notes.append(f"{code}仅有残缺缓存诊断值({via})→不得进入正式净值")
                    continue
                mv += qty * close
                n += 1
                via_n[via] = via_n.get(via, 0) + 1
            p_mv, p_parts, p_missing = probe_addon(date, hb_codes)
            if p_parts:
                mv += p_mv
                notes.append(f"探针遗留仓补入{len(p_parts)}笔:{','.join(p_parts)}")
            missing_prices += p_missing
            a_mv, a_part, a_missing, a_via = patch_0909_addon(date, positions)
            if a_part and str(a_via or "").startswith("stale_bar_warn"):
                missing_prices.append("588170")
                notes.append("09-09心跳丢票补丁仅拿到残缺缓存值→不进入正式净值")
            elif a_part:
                mv += a_mv
                n += 1
                via_n[a_via] = via_n.get(a_via, 0) + 1
                notes.append(a_part)
            missing_prices += a_missing
            missing_prices = sorted(set(missing_prices))
            via_s = ",".join(f"{k}×{v}" for k, v in sorted(via_n.items()))
            if missing_prices:
                partial_market_value = mv
                market_value = None
                notes.append(
                    f"持仓缺合法EOD价{len(missing_prices)}只:{','.join(missing_prices[:8])}；"
                    f"正式market_value/equity/account_ret/alpha置null，"
                    f"partial_market_value={mv:.2f}仅供诊断")
            else:
                market_value = mv
                notes.append(f"市值=heartbeat尾行{n}只×EOD收盘({via_s})")
            notes.append(f"cash={cash:.2f}(heartbeat末行)" if cash is not None
                         else "cash缺失(heartbeat末行无cash字段)→equity/account_ret/alpha置null,不硬算")

    if cash is not None and market_value is not None:
        equity = market_value + float(cash)
    else:
        equity = None

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

    t0, t0_cnt = t0_realized(date)
    notes.append(f"t0_realized={'无记录' if t0_cnt == 0 else f'{t0:.2f}({t0_cnt}条审计)'}")
    data_status = ("partial_position_valuation" if missing_prices
                   else ("complete" if equity is not None else "unavailable"))
    return {
        "date": date,
        "data_status": data_status,
        "equity": equity,
        "cash": cash,
        "market_value": round(market_value, 2) if market_value is not None else None,
        "partial_market_value": (round(partial_market_value, 2)
                                 if partial_market_value is not None else None),
        "missing_prices": missing_prices,
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
