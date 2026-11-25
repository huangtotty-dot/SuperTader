# -*- coding: utf-8 -*-
"""修复「过去某天还挂着 forming」的日线缓存（2026-11-03 事故）。

事故：盘中 09:31 写下的**快照根**（close=当时价、量只有零头）带 `forming` 标记落盘；
次日 `is_unconfirmed_today_bar` 只认"末行==今天"，不再命中 ⇒ 半成品被当收盘
（实测 2374 个缓存文件带 forming、2102 只收盘价与实际不符，11-02 K 线全错）。

本模块提供：
  · `find_forming_tails(date=None)` → [(code, fp, last_date, old_close)]；
  · `fix_forming_tails(...)` → 逐只重新拉**腾讯前复权日线**，`merge_daily_cache`
    覆盖该日并清 forming；返回统计。

调用方：
  · `scripts/ops/fix_forming_daily_bar.py`（手动/全量）
  · `main.py` 15:40 通达信校准之后的兜底（TDX 没更新时靠这条）
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
CACHE = BASE / "t_io" / "cache" / "daily_kline"

_lock = threading.Lock()


def find_forming_tails(date=None, cache_dir=None):
    """扫缓存 → [(code, fp, last_date, old_close)]：末行是**过去某天**且仍在该文件的 forming 里。"""
    root = Path(cache_dir) if cache_dir else CACHE
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for fp in sorted(root.glob("*.json")):
        c = fp.stem
        if not c.isdigit():
            continue
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = d.get("rows") or []
        if not rows:
            continue
        last = str(rows[-1].get("date") or "")[:10]
        form = {str(x) for x in (d.get("forming") or [])}
        if not last or last >= today or last not in form:
            continue
        if date and last != date:
            continue
        out.append((c, fp, last, float(rows[-1].get("close") or 0)))
    return out


def _snapshot_zero_turnover(date, code):
    """当日市场快照里该票成交额是否 ≤ 0（= 停牌/无成交）。快照里没有这只则返回 False。"""
    try:
        import csv as _csv
        fp = BASE / "stock_hunter" / "data" / f"market_{str(date).replace('-', '')}.csv"
        with open(fp, encoding="utf-8-sig", newline="") as f:
            for x in _csv.DictReader(f):
                if str(x.get("代码", "")).strip().zfill(6) != str(code):
                    continue
                try:
                    return float(x.get("成交额") or 0) <= 0
                except Exception:
                    return False
    except Exception:
        pass
    return False


def _cached_bar(fp, target):
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        for r in (d.get("rows") or []):
            if str(r.get("date"))[:10] == target:
                return r
    except Exception:
        pass
    return None


def _clear_forming_only(fp, code, target, log_fp, why):
    """只清 forming 标记、保留那根 bar（停牌日的平 bar 是正常的，腾讯也这么给）。"""
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        form = [x for x in (d.get("forming") or []) if str(x)[:10] != target]
        if len(form) == len(d.get("forming") or []):
            return code, "no_target_bar", why, None
        d["forming"] = form
        from core.utils import write_json_atomic
        write_json_atomic(str(fp), d)
        _log(log_fp, code, target, None, None, "cleared")
        return code, "cleared", why, None
    except Exception as e:
        return code, "drop_err", f"{type(e).__name__}: {str(e)[:60]}", None


def _unconfirmed(fp, code, target, old_close, log_fp, why):
    """供应商拿不到目标日时：停牌（量 0 / 快照成交额 0）→ 只清标记；否则删掉无法确认的假 bar。"""
    bar = _cached_bar(fp, target)
    try:
        vol = float((bar or {}).get("volume") or 0)
    except Exception:
        vol = 0.0
    if vol <= 0 or _snapshot_zero_turnover(target, code):
        return _clear_forming_only(fp, code, target, log_fp, why + "|suspended")
    return _drop_bar(fp, code, target, old_close, log_fp, why)


def _drop_bar(fp, code, target, old_close, log_fp, why):
    """删掉目标日的假 bar 并清 forming（停牌/无成交时用）。"""
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        rows = [r for r in (d.get("rows") or []) if str(r.get("date"))[:10] != target]
        if len(rows) == len(d.get("rows") or []):
            return code, "no_target_bar", why, None
        d["rows"] = rows
        d["forming"] = [x for x in (d.get("forming") or []) if str(x)[:10] != target]
        from core.utils import write_json_atomic
        write_json_atomic(str(fp), d)
        _log(log_fp, code, target, old_close, None, "dropped")
        return code, "dropped", why, None
    except Exception as e:
        return code, "drop_err", f"{type(e).__name__}: {str(e)[:60]}", None


def _log(log_fp, code, target, old_close, new_close, status):
    if not log_fp:
        return
    try:
        with _lock:
            Path(log_fp).parent.mkdir(parents=True, exist_ok=True)
            with open(log_fp, "a", encoding="utf-8") as f:
                f.write(json.dumps({"code": code, "target": target,
                                    "old_close": old_close, "new_close": new_close,
                                    "status": status,
                                    "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                                   ensure_ascii=False) + "\n")
    except Exception:
        pass


def fix_one(item, log_fp=None):
    """修一只：拉腾讯日线（只保留目标日及之前）→ merge（真数据覆盖 + 清 forming）→ 复核。"""
    code, fp, target, old_close = item
    from core.market_data.tencent_provider import daily_kline_raw, merge_daily_cache
    try:
        df = daily_kline_raw(code, days=900, qfq=True)
    except Exception as e:
        return code, "fetch_err", f"{type(e).__name__}: {str(e)[:60]}", None
    if df is None or getattr(df, "empty", True):
        # 腾讯也没有数据：停牌（量 0 / 快照成交额 0）⇒ 清标记保留平 bar；否则删掉假 bar
        return _unconfirmed(fp, code, target, old_close, log_fp, "provider_empty")
    try:
        df = df[df["date"].astype(str).str[:10] <= target].reset_index(drop=True)
    except Exception:
        return code, "filter_err", "", None
    if df.empty or str(df["date"].iloc[-1])[:10] != target:
        # 腾讯也没有这一天（停牌/当天无成交）⇒ 那根 bar 无法确认：停牌就清标记保留，否则删掉
        return _unconfirmed(fp, code, target, old_close, log_fp, "provider_has_no_bar")
    new_close = float(df["close"].iloc[-1])
    try:
        merge_daily_cache(code, df, forming_date=None)
    except Exception as e:
        return code, "merge_err", f"{type(e).__name__}: {str(e)[:60]}", new_close
    try:
        d2 = json.loads(fp.read_text(encoding="utf-8"))
        r2 = (d2.get("rows") or [])[-1]
        ok = (str(r2.get("date"))[:10] == target
              and abs(float(r2.get("close") or 0) - new_close) < 1e-6
              and target not in {str(x) for x in (d2.get("forming") or [])})
    except Exception:
        ok = False
    _log(log_fp, code, target, old_close, new_close, "fixed" if ok else "verify_fail")
    return code, ("fixed" if ok else "verify_fail"), "", new_close


def fix_forming_tails(codes=None, date=None, workers=4, limit=0, cache_dir=None, log_fp=None):
    """批量修复。`codes` 限制代码集合；`limit` 限制本次最多修多少只；返回统计 dict。"""
    targets = find_forming_tails(date=date, cache_dir=cache_dir)
    if codes:
        cs = {str(c).split("_")[0].zfill(6) for c in codes}
        targets = [t for t in targets if t[0] in cs]
    if limit and len(targets) > int(limit):
        targets = targets[:int(limit)]
    stats = {"total": len(targets), "fixed": 0, "dropped": 0, "cleared": 0, "verify_fail": 0,
             "fetch_err": 0, "fetch_empty": 0, "no_target_bar": 0,
             "merge_err": 0, "filter_err": 0, "drop_err": 0}
    if not targets:
        return stats
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for _code, st, _detail, _newc in ex.map(lambda it: fix_one(it, log_fp), targets):
            stats[st] = stats.get(st, 0) + 1
    return stats


# ---------------------------------------------------------------------------
# 候选㊴（2026-11-25 owner 批准）：「今日行」快照污染的 EOD 校验修复
#
# 事故：11-25 复盘发现 300508/300354 的**当日行**是早盘 forming 快照
# （300508 收 43.57/量 1.4 万，真实收 40.09/量 10.1 万；300354 假收 27.58 vs
# 真实 24.72），且 forming 标记已被清空、saved_at 是盘后 15:15 ——
# `find_forming_tails` 只修「过去某天仍挂 forming」的，命中不了这种
# 「今日行 + 标记已清」的污染；H4 假说结算脚本直接读缓存，300354 的假收盘
# 足以把「破 MA5 卖出」翻转成「继续持有」。
#
# 对策：盘后（≥15:10，快照已结算）扫「末行==今天」的缓存，逐只用
# `quote_daily_bar`（qt.gtimg.cn 当日结算快照）比对，close 偏差 >0.1% 或
# volume 偏差 >1% 就整行补丁（与 equity_daily._quote_patch_cache_file 同口径，
# 打 `src=tencent_quote` 标记）。
# ---------------------------------------------------------------------------

def find_today_rows(date=None, cache_dir=None, mtime_today_only=True):
    """扫缓存 → [(code, fp, close, volume)]：末行==目标日（默认今天）的行。

    `mtime_today_only`：只看今天被写动过的文件（没被写动的没有污染窗口，省一轮全量读盘）。
    """
    root = Path(cache_dir) if cache_dir else CACHE
    target = str(date or datetime.now().strftime("%Y-%m-%d"))[:10]
    today = datetime.now().strftime("%Y-%m-%d")
    out = []
    for fp in sorted(root.glob("*.json")):
        c = fp.stem
        if not c.isdigit():
            continue
        try:
            if mtime_today_only and target == today:
                if datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d") != today:
                    continue
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        rows = d.get("rows") or []
        if not rows:
            continue
        last = rows[-1]
        if str(last.get("date") or "")[:10] != target:
            continue
        if last.get("src") == "tencent_quote":
            continue  # 已是补丁行（幂等：重跑直接跳过）
        out.append((c, fp, float(last.get("close") or 0), float(last.get("volume") or 0)))
    return out


def _fix_today_row_one(item, target, dry_run, log_fp):
    """校验一只：quote 快照比对 → 不一致就整行补丁。返回 (code, status, detail, new_close)。"""
    code, fp, old_close, old_vol = item
    try:
        from core.market_data.tencent_provider import quote_daily_bar
        bar = quote_daily_bar(code)
    except Exception as e:
        return code, "fetch_err", f"{type(e).__name__}: {str(e)[:60]}", None
    if not bar or str(bar.get("date") or "")[:10] != target:
        return code, "provider_no_bar", str((bar or {}).get("date") or ""), None
    try:
        new_close, new_vol = float(bar["close"]), float(bar.get("volume") or 0)
    except Exception:
        return code, "provider_bad_bar", "", None
    close_ok = old_close > 0 and abs(new_close - old_close) / old_close <= 0.001
    vol_ok = old_vol > 0 and abs(new_vol - old_vol) / old_vol <= 0.01
    if close_ok and vol_ok:
        return code, "ok", "", new_close
    if new_vol <= 0:
        # 快照量 0：停牌/数据异常，不拿它盖缓存
        return code, "provider_zero_vol", f"cache_close={old_close}", None
    if dry_run:
        return code, "would_patch", f"close {old_close}->{new_close} vol {old_vol:.0f}->{new_vol:.0f}", new_close
    try:
        d = json.loads(fp.read_text(encoding="utf-8"))
        rows = d.get("rows") or []
        if not rows or str(rows[-1].get("date") or "")[:10] != target:
            return code, "race_skip", "", new_close
        rows[-1] = {"date": target, "open": float(bar["open"]), "close": new_close,
                    "high": float(bar["high"]), "low": float(bar["low"]),
                    "volume": new_vol, "amount": float(bar.get("amount") or 0),
                    "src": "tencent_quote",
                    "repaired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        d["rows"] = rows
        # 今日行已被真数据确认 ⇒ 清掉今日 forming 标记（若有）
        d["forming"] = [x for x in (d.get("forming") or []) if str(x)[:10] != target]
        d["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " today_bar_repair"
        from core.utils import write_json_atomic
        write_json_atomic(str(fp), d)
    except Exception as e:
        return code, "patch_err", f"{type(e).__name__}: {str(e)[:60]}", new_close
    _log(log_fp, code, target, old_close, new_close, "today_patched")
    return code, "patched", f"close {old_close}->{new_close} vol {old_vol:.0f}->{new_vol:.0f}", new_close


def fix_today_rows(codes=None, date=None, workers=6, limit=0, cache_dir=None,
                   log_fp=None, dry_run=False, mtime_today_only=True):
    """批量校验修复「今日行」。`codes` 限定代码集合；`dry_run` 只报告不写盘；返回统计 dict。"""
    target = str(date or datetime.now().strftime("%Y-%m-%d"))[:10]
    targets = find_today_rows(date=target, cache_dir=cache_dir,
                              mtime_today_only=mtime_today_only)
    if codes:
        cs = {str(c).split("_")[0].zfill(6) for c in codes}
        targets = [t for t in targets if t[0] in cs]
    if limit and len(targets) > int(limit):
        targets = targets[:int(limit)]
    stats = {"date": target, "total": len(targets), "ok": 0, "patched": 0, "would_patch": 0,
             "fetch_err": 0, "provider_no_bar": 0, "provider_bad_bar": 0,
             "provider_zero_vol": 0, "patch_err": 0, "race_skip": 0}
    if not targets:
        return stats
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
        for _code, st, _detail, _newc in ex.map(
                lambda it: _fix_today_row_one(it, target, dry_run, log_fp), targets):
            stats[st] = stats.get(st, 0) + 1
    return stats
