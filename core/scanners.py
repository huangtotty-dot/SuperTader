# -*- coding: utf-8 -*-
"""全市场突破/回站扫描的纯算法与进程内结果缓存。

行情、磁盘索引和股票元数据均由调用方注入；本模块顶层不导入 GUI、配置或行情提供方。
"""
from datetime import datetime
import threading
import time as _time_mod

from core.chart_analytics import _detect_boxes


# 突破与回站扫描共用状态锁；结果缓存按日存取，强制重扫时由调用方显式删除。
_BREAKOUT_LOCK = threading.Lock()
_breakout_cache = {}
_reclaim_cache = {}


def _bk_cache_get(key):
    with _BREAKOUT_LOCK:
        return _breakout_cache.get(key)


def _bk_cache_put(key, val):
    with _BREAKOUT_LOCK:
        _breakout_cache[key] = val


def _bk_cache_pop(key):
    """调用方持有 _BREAKOUT_LOCK 时删除指定日期的突破缓存。"""
    _breakout_cache.pop(key, None)


def _rc_cache_get(key):
    with _BREAKOUT_LOCK:
        return _reclaim_cache.get(key)


def _rc_cache_put(key, val):
    with _BREAKOUT_LOCK:
        _reclaim_cache[key] = val


def _rc_cache_pop(key):
    """调用方持有 _BREAKOUT_LOCK 时删除指定日期的回站缓存。"""
    _reclaim_cache.pop(key, None)


def _bk_cached_df(code, days, target=None, *, cache_dir, min_bars, load_json, tdx_df):
    """本地日线缓存兜底（2026-10-25）。

    GM 批量取数不可用时（owner 报「经常跑不出结果」），过去整批记 no_data ⇒ 全池 0 命中。
    猎手本身每天在读这份缓存，所以直接拿它当兜底：字段与 daily_many 同形
    （date/open/high/low/close/volume），`_detect_boxes` 与判定口径不用改。
    """
    p = cache_dir / f"{code}.json"
    try:
        rows = (load_json(p, {}) or {}).get("rows") or []
    except Exception:
        rows = []
    if len(rows) < min_bars + 1 or (target and str(rows[-1].get("date"))[:10] < target):
        # JSON 缓存缺目标日（或太短）⇒ 用通达信 parquet 顶（不复权，但同一序列内部自洽）
        t = tdx_df(code, days)
        if t is not None and len(t) >= min_bars + 1:
            if not target or str(t["date"].iloc[-1]) >= target:
                return t
    try:
        import pandas as pd
        df = pd.DataFrame(rows[-days:])
        need = {"date", "open", "high", "low", "close", "volume"}
        if not need.issubset(df.columns):
            return None
        df["date"] = df["date"].astype(str)
        return df
    except Exception:
        return None


def _box_top_for(hist, *, min_bars=30):
    """as-of 上一交易日的箱体上沿：取**含昨收**那只箱体（rel==0）的 high。

    `_detect_boxes` 在 `low <= last_close <= high` 时置 rel=0；此处 last_close 即昨收，
    故 rel==0 的箱体正是"昨收还在里面"的那只 —— 它的上沿就是今日要突破的线。
    必须用**排除当日**的切片：带上当日跳空的话上沿会被抬高，今天反而判不出突破。
    """
    if hist is None or len(hist) < min_bars:
        return None
    for b in _detect_boxes(hist):
        if b.get("rel") == 0 and b.get("high"):
            return float(b["high"])
    return None


def _breakout_probe_one(code, df, date=None, *, min_bars=30, min_pct=0.3,
                        max_pct=3.0, max_chg=0.07):
    """单只「当日有效突破」判定，命中返回 dict，否则 None。

    口径（owner 2026-09-29）：上沿取自 as-of 上一交易日的箱体，命中条件
    `昨收 ≤ 上沿 < 今价` 且幅度 ∈ [0.3%, 8%]。

    两道**必须**的闸：
      - **交易日闸**：`df` 末根必须就是今日。`daily_many` 只在交易日 09:15-23:59 且快照
        ts_date=当日 时才补 forming bar，故周末/盘前末根=上一交易日 → 此处返回 None。
        没有这道闸，周六会把**周五**的突破当成"今天"报出去。
      - **长度闸**：切片后需 ≥ `_BK_MIN_BARS` 根（恰好 30 根全量的票切完只剩 29，
        `_detect_boxes` 会静默返回 [] 而无声漏掉）。
    """
    if df is None or df.empty:
        return None
    today = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    # 2026-10-09：支持**任选日期**扫描——先按目标日 as-of 切片（去掉目标日之后的 bar，
    # 否则历史日扫描会用到"今天"的末根 = 前视），再要求切片末根**恰好等于目标日**。
    df = df[df["date"].astype(str) <= today]
    if df.empty or str(df["date"].iloc[-1]) != today:
        return None
    if len(df) < min_bars + 1:
        return None
    box_top = _box_top_for(df.iloc[:-1], min_bars=min_bars)
    if not box_top:
        return None
    prev_close = float(df["close"].iloc[-2])
    cur = float(df["close"].iloc[-1])
    # 对 rel==0 的箱体 `low ≤ 昨收 ≤ high` 恒成立 ⇒ `prev_close <= box_top` 恒真，
    # 真正起作用的是"今价站上上沿"。两半都留着以对齐 owner 的表述口径。
    if not (prev_close <= box_top < cur):
        return None
    pct = (cur - box_top) / box_top * 100
    if not (min_pct <= pct <= max_pct):
        return None
    # 当日涨幅闸（2026-10-25）：>7% 档 T+5 超额 −0.58%，是分档里最差的一组 ⇒ 直接剔除
    chg = (cur / prev_close - 1.0) if prev_close > 0 else 0.0
    if chg > max_chg:
        return None
    # 量比（今日量 / 过去 20 日均量）：研究里放量是反指、缩量档唯一不亏 ⇒ 只做展示与排序，不做放量确认
    vol_ratio = None
    try:
        vol = [float(x) for x in df["volume"].tolist()]
        if len(vol) >= 21:
            win = vol[-21:]          # 当日 + 前 20 日
            # 单位一致性闸（2026-10-25）：日线缓存在 10-22 做过一次成交量单位迁移，
            # 前后段可能一手一股混在一起（实测 600887 相邻日差 27 倍）⇒ 这种票的量比不报，
            # 否则会把"换了单位"误判成"缩量"，反而给质量分加分。
            _ok = all(a > 0 and b > 0 and 0.1 <= b / a <= 10.0
                      for a, b in zip(win, win[1:]))
            if _ok:
                base = sum(win[:-1]) / 20.0
                if base > 0:
                    vol_ratio = round(win[-1] / base, 2)
    except Exception:
        vol_ratio = None
    q = 0
    if vol_ratio is not None and vol_ratio < 1.0:
        q += 2
    if pct <= 1.5:
        q += 2
    if chg <= 0.03:
        q += 1
    return {"price": round(cur, 3), "prev_close": round(prev_close, 3),
            "box_top": round(box_top, 3), "pct_above": round(pct, 2),
            "day_chg": round(chg * 100, 2), "vol_ratio": vol_ratio, "quality": q}


def _scan_breakout(codes, state, date=None, *, jy, daily_many, retry_daily_many,
                   market_of, cache_covers, cache_last, cached_df, probe_one,
                   stock_concepts, stock_industry, concept_descs, em_boards,
                   batch, scan_bars, retry_wait):
    """全池扫描「当日有效突破」。state 非空时更新进度（done/total/found/stocks/no_data）。

    2026-09-29 重写要点：
      - 取数改 `facade.daily_many`（GM `history` 列表式，350 只/批 + 批量 forming bar），
        取代逐只 `load_stock_tags_batch` —— 后者走 GM **单线程串行** + 90s 批超时**静默丢票**，
        全池 5000+ 只不可行。
      - 判定改「当日有效突破」，取代旧 `向上突破`（旧标签只看"当前价在箱体上沿之上"，
        没有任何当日成分 ⇒ 三周前突破的票每天照旧被扫出来）。
      - 北交所：`daily_many` 分流到 bj_daily，取不到的码**不出现在返回里** ⇒ 此处计入 no_data
        如实汇报，而不是静默当成"没突破"。
    """
    breakouts = []
    total = len(codes)
    dt_target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    rest_total = sum(1 for c in codes if market_of(c) != "BJ")
    # no_data 拆分成因（2026-10-09）：整池取数失败（GM 不可达）≠ 北交所取不到 ≠ 无当日 bar。
    no_data = rest_missing = bj_missing = stale = ok = cached_used = 0
    BATCH = batch
    _waited = False
    # 缓存能覆盖目标日 ⇒ 直接走缓存，不碰 GM（GM 不可达时会整批阻塞好几分钟）
    _cov = cache_covers(codes, dt_target)
    _gm_off = _cov >= 0.8
    if _gm_off:
        print(f"[INFO] breakout: 本地日线缓存覆盖 {_cov:.0%}，本轮只用缓存", flush=True)
    for i in range(0, total, BATCH):
        chunk = codes[i:i + BATCH]
        if _gm_off:
            frames = {}
        else:
            frames = daily_many(chunk)
        # ⚠️ `_gm_off=True` 时 frames 本来就是空的（我们主动没调 GM）⇒ 不能再进"等窗重试"，
        # 否则白等 60s（2026-10-25 实测：7 只票扫了 69.8s，全耗在这）。
        if not frames and chunk and not _waited and not _gm_off:
            # 整批空 ⇒ 疑 GM 冷却窗/瞬时不可达（facade 60s 冷却内后续批全跳过）→ 等窗后重试一次
            # （只等一次，避免 GM 真下线时逐批 62s 打转；仍空则由 fetch_failed 如实汇报）
            _time_mod.sleep(retry_wait)
            _waited = True
            try:
                frames = retry_daily_many(chunk)
            except Exception:
                frames = {}
        _from_gm = 0
        for _n, code in enumerate(chunk):
            # 2026-10-26（owner 报「跑突破箱体卡顿」）：每 64 只显式让出一次，
            # 主线程（10s 轮询 / 图表重绘）不会被连续占死。全池 5400 只 ⇒ 84 次 × 3ms ≈ 0.25s。
            if _n and _n % 64 == 0:
                _time_mod.sleep(0.003)
            fr = frames.get(code)
            if fr is not None and not getattr(fr, "empty", True):
                _from_gm += 1
            if fr is None or getattr(fr, "empty", True):
                # 2026-10-25：GM 不可用时的兜底——读本地日线缓存（猎手每天本来就在读它）。
                # 先只读末根日期，末根早于目标日 ⇒ 直接算 stale，不建 DataFrame（全池扫描的主要开销）。
                # 2026-10-26：把目标日传下去 —— 缓存文件头已经够新时，注入方**不必再查通达信**。
                _cl = cache_last(code, dt_target)
                if _cl is not None and _cl == dt_target:
                    fr = cached_df(code, scan_bars, dt_target)
                    if fr is not None:
                        cached_used += 1
                elif _cl is not None and _cl > dt_target:
                    fr = cached_df(code, scan_bars, dt_target)
                    if fr is not None:
                        cached_used += 1
                elif _cl is not None:
                    stale += 1
                    continue
            if fr is None or getattr(fr, "empty", True):
                no_data += 1
                if market_of(code) == "BJ":
                    bj_missing += 1
                else:
                    rest_missing += 1
                continue
            # 有帧但**无当日 bar** → 未就绪/非交易日/停牌（区别于"取数失败"）
            # 2026-10-25 快路径：末根就是目标日时不用再切片（全池扫时这一步占大头）
            _dates = fr["date"].astype(str)
            _last_d = str(_dates.iloc[-1])
            if _last_d == dt_target:
                _sl = fr
            elif _last_d < dt_target:
                stale += 1
                continue
            else:
                _sl = fr[_dates <= dt_target]
                if _sl.empty or str(_sl["date"].iloc[-1]) != dt_target:
                    stale += 1
                    continue
            ok += 1
            hit = probe_one(code, fr, date)
            if not hit:
                continue
            info = jy.get(code) if isinstance(jy.get(code), dict) else None
            nm = (info or {}).get("name", code)
            concepts = stock_concepts(info)
            csrc = "offline" if concepts else "none"
            if not concepts:
                # 离线两源为空：东财磁盘缓存层补拉（仅命中票，~30只/天，fail-open 不阻塞扫描）
                concepts = stock_concepts(info, em_boards=em_boards(code))
                if concepts:
                    csrc = "em"
            breakouts.append({"code": code, "name": nm,
                              "industry": stock_industry(info),
                              "concepts": concepts,
                              "concepts_source": csrc,
                              "concept_descs": concept_descs(info, concepts),
                              "tags": [{"label": "当日有效突破", "color": "up"}], **hit})
        if state is not None:
            with _BREAKOUT_LOCK:
                state.update({"done": min(i + BATCH, total), "found": len(breakouts),
                              "stocks": list(breakouts), "no_data": no_data,
                              "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                              "stale": stale, "ok": ok})
        if i == 0 and not _gm_off and _from_gm == 0 and cached_used > 0:
            _gm_off = True   # 首批 GM 全空但缓存顶上了 ⇒ 后面直接走缓存，不再逐批等 GM
            print("[INFO] breakout: GM 批量不可用，本轮改用本地日线缓存", flush=True)
        _time_mod.sleep(0.05)   # 2026-10-08：批间显式让出，避免连续持 GIL 把主线程饿死
    # 2026-10-25：按质量分排序（缩量 + 小幅突破 + 当日涨幅温和），
    # **不再按"超出上沿越多越前"**——研究显示幅度 3~5% 档最差，原排序正好把差的排前面。
    breakouts.sort(key=lambda x: (-(x.get("quality") or 0),
                                  (x.get("vol_ratio") if x.get("vol_ratio") is not None else 9.9),
                                  x.get("pct_above") or 0))
    # 整池不可用判据（任一成立即视为"取数失败，结果不可信"）：
    #   ① 无一只带当日 bar（ok==0）—— GM 整池不可达 / 非交易日 / 行情未就绪；
    #   ② 非北交所码大面积无帧（≥50%）—— GM `daily_batch` 不可达（批量路径无腾讯兜底）。
    fetch_failed = total > 0 and (ok == 0
                                  or (rest_total > 0 and rest_missing >= 0.5 * rest_total))
    if state is not None:
        with _BREAKOUT_LOCK:
            state.update({"done": total, "found": len(breakouts),
                          "stocks": list(breakouts), "no_data": no_data,
                          "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                          "stale": stale, "ok": ok, "cached": cached_used,
                          "fetch_failed": bool(fetch_failed)})
    return breakouts


def _reclaim_probe_one(code, df, date=None):
    """单只「刚刚站上5日线」判定（口径单一源 core/ma_reclaim.reclaim5_frame），命中返回 dict 否则 None。"""
    from core.ma_reclaim import reclaim5_frame
    target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    return reclaim5_frame(df, target)


def _scan_reclaim(codes, state, date=None, *, jy, daily_many, market_of, probe_one,
                  stock_concepts, stock_industry, concept_descs, em_boards,
                  batch, retry_wait):
    """全市场扫描「刚刚站上5日线」。state 非空时更新进度（分类同突破扫描）。"""
    hits = []
    total = len(codes)
    dt_target = str(date) if date else datetime.now().strftime("%Y-%m-%d")
    rest_total = sum(1 for c in codes if market_of(c) != "BJ")
    no_data = rest_missing = bj_missing = stale = ok = cached_used = 0
    BATCH = batch
    _waited = False
    for i in range(0, total, BATCH):
        chunk = codes[i:i + BATCH]
        try:
            frames = daily_many(chunk)
        except Exception as e:      # 兜底：单批异常绝不终止整轮
            print(f"[WARN] reclaim daily_many 失败({len(chunk)}只): "
                  f"{type(e).__name__}: {str(e)[:80]}", flush=True)
            frames = {}
        if not frames and chunk and not _waited:
            _time_mod.sleep(retry_wait)
            _waited = True
            try:
                frames = daily_many(chunk)
            except Exception:
                frames = {}
        for code in chunk:
            fr = frames.get(code)
            if fr is None or getattr(fr, "empty", True):
                no_data += 1
                if market_of(code) == "BJ":
                    bj_missing += 1
                else:
                    rest_missing += 1
                continue
            _sl = fr[fr["date"].astype(str) <= dt_target]
            if _sl.empty or str(_sl["date"].iloc[-1]) != dt_target:
                stale += 1
                continue
            ok += 1
            hit = probe_one(code, fr, date)
            if not hit:
                continue
            info = jy.get(code) if isinstance(jy.get(code), dict) else None
            nm = (info or {}).get("name", code)
            concepts = stock_concepts(info)
            csrc = "offline" if concepts else "none"
            if not concepts:
                concepts = stock_concepts(info, em_boards=em_boards(code))
                if concepts:
                    csrc = "em"
            hits.append({"code": code, "name": nm,
                         "industry": stock_industry(info),
                         "concepts": concepts, "concepts_source": csrc,
                         "concept_descs": concept_descs(info, concepts),
                         "tags": [{"label": "站上5日线", "color": "up"}], **hit})
        if state is not None:
            with _BREAKOUT_LOCK:
                state.update({"done": min(i + BATCH, total), "found": len(hits),
                              "stocks": list(hits), "no_data": no_data,
                              "rest_no_data": rest_missing, "bj_no_data": bj_missing,
                              "stale": stale, "ok": ok})
        _time_mod.sleep(0.05)
    hits.sort(key=lambda x: -(x.get("dev5_pct") if x.get("dev5_pct") is not None else -1e9))
    fetch_failed = total > 0 and (ok == 0
                                  or (rest_total > 0 and rest_missing >= 0.5 * rest_total))
    if state is not None:
        with _BREAKOUT_LOCK:
            state.update({"done": total, "found": len(hits), "stocks": list(hits),
                          "no_data": no_data, "rest_no_data": rest_missing,
                          "bj_no_data": bj_missing, "stale": stale, "ok": ok,
                          "fetch_failed": bool(fetch_failed)})
    return hits
