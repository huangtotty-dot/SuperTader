# -*- coding: utf-8 -*-
"""超跌跳空确认 · 时点钩子（包内模块 `hooks`）—— 从 gm_main 搬入（2026-10-12）。

## 六个时点（都是**每日一次**，仅 MODE_LIVE）

    init   prescreen_init    P0-3（2026-10-26）：盘前预筛**主路径前移到 init**（事件循环开始前
                             跑完全市场批量取数；原来在 09:31 钩子里跑，实测 169s~472s、
                             最差 1344.5s，阻塞期间 fill 回调排队、心跳停写——10-15/17/18/22
                             的 09:43 心跳中断红告警根因）。预筛只用 D-1 及之前日线、
                             与盘中行情无关 ⇒ 前移完全等价。fail-open：失败不阻止启动。
    09:31  prepare           盘前预筛**兜底**（init 已成功 ⇒ 跳过；init 未跑/失败 ⇒ 照旧执行）
    10:00  try_morning_sell  D+1 持仓早盘冲高 ≥ 入场价×(1+target) → 卖（**先于买侧**）
    10:00  try_buy           10:00 棒 → 决策核 → gap top-N → 下单
    10:08  try_backfill #1   未成交 → 撤单 → 候补复检 → 顺移（2026-10-14 新增）
    10:14  try_backfill #2   同上第二轮；**10:20 后不再开新仓**
    14:50  try_sell          SL5只深 close 止损 + 满 N 日到期平仓
                             （P1-7，2026-10-26：每日一次标记改「成功才置位」——
                             抛异常 ⇒ 下一根 bar 重试，上限 3 次 / 最晚 14:58，
                             耗尽 ⇒ 放弃 + risk 告警；早盘冲高卖同型，最晚 10:30）

由 `on_bar(rt, context, now, today, t)` 统一分发：内部做**时间窗 + 每日一次 + 模式**闸，
每个钩子各自 try/except（一条钩子炸不影响别的，也不阻断主循环）。

## 依赖注入

本模块**不 import gm.api**（仓规：只有 gm_main 能）。全部掘金能力与宿主服务经 `Runtime`（`rt`）
传入：`rt.gm.*`（原语）/ `rt.sdk_call` / `rt.gateway(context)` / `rt.param` / `rt.audit` /
`rt.risk` / `rt.log` / `rt.book_dir` / `rt.kill_switch` / `rt.is_live`。
执行核与预筛是同包模块（`executor` / `prescreen`），决策核是同包 `decision`，
买不进替补的纯函数在 `backfill`。

## ⚠️ 为什么补单做成**新时点钩子**而不是在 `try_buy` 里循环

`_place_and_poll` 会 sleep；要在 `try_buy` 里等到 10:08 就得阻塞约 480s，而
`gm_main` 顶部注释（2026-09-11 事故）明写 **`on_bar` 阻塞会连坐 SDK 事件派发线程、让 fill 永久排队**。
bar 循环本就 60s 一跳 ⇒ 10:08/10:14 是**免费定时器**，且与首轮解耦后任一补单轮失败都不影响首轮留痕。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import time as _time
from collections import Counter
from datetime import time as dtime
from functools import partial

import pandas as pd

from . import backfill, executor, preplace_probe, prescreen

# 每日一次状态（原先散在 gm_main 的模块级全局 `_OSC_*_DONE_DATE` + `context._osc_candidates`）
_STATE = {"date": None, "prepare": False, "buy": False, "sell": False,
          "morning": False, "candidates": {},
          # P1-7（2026-10-26）：卖侧「成功才置位」的重试计数（见 on_bar 卖侧两格）
          "sell_retries": 0, "morning_retries": 0,
          # ── 买不进替补（2026-10-14）──
          "fills": None,      # 当日首轮 fills dict（额度账 committed 由它算）
          "pending": [],      # 待补单（未成交买单）
          "spares": [],       # 顺移候补（gap 降序）
          "done_codes": set(),  # 今日已碰过的 code（防重复顺移/追价）
          "backfill": {},     # {round_idx: True} 每日一次标记
          "buy_done": False,  # 首轮已执行（落盘，防进程重启后重复下单）
          # 盘前宇宙/板块档位（2026-10-17）：只给「每日选股记录」落盘用
          "prescreen_meta": {},
          # P0-3（2026-10-26）：init 前移主路径的标记
          "prepare_via": None,        # "init" = init 阶段已成功（09:31 钩子据此跳过）
          "prepare_skip_logged": False,  # 09:31 钩子「跳过」日志只打一行（不刷屏）
          # 预挂目标限价 S0 影子探测（2026-10-19，只记录不下单）
          "probe_plan": False, "probe_final": False,
          }


def reset_day(today, preserve_prescreen: bool = False) -> None:
    """按日重置；仅 init 后的首次重置可保留已完成的预筛结果。"""
    _keep = None
    if (preserve_prescreen and _STATE.get("prepare")
            and _STATE.get("prepare_via") == "init"):
        _keep = {k: _STATE.get(k) for k in
                 ("candidates", "prepare", "prepare_via", "prescreen_meta",
                  "prepare_skip_logged")}
    _STATE.update(date=today, prepare=False, buy=False, sell=False,
                  morning=False, candidates={}, fills=None, pending=[], spares=[],
                  done_codes=set(), backfill={}, buy_done=False, prescreen_meta={},
                  prepare_via=None, prepare_skip_logged=False,
                  sell_retries=0, morning_retries=0,
                  probe_plan=False, probe_final=False)
    if _keep is not None:
        _STATE.update(_keep)


def _write_prescreen(rt, date, cand, meta, ok=True, last_attempt_ok=None,
                     degraded=False, last_attempt_error=None) -> str | None:
    """盘前候选名单 → `prescreen_{date}.json`（2026-10-17，供 GUI「每日选股记录」）。

    ⚠️ **绝不抛**：这是纯展示留痕，写失败不该影响预筛/下单（fail-closed 语义不在这里）。
    ⚠️ 原子写（tmp+replace）：GUI 可能正在读，普通写会让它读到半截 JSON。
    `ok=False`（P0-3）：本次预筛**异常失败**的标记——`_read_prescreen_cache` 据此
    不把这份空名单当「当日新鲜结果」（否则 init 失败写出的空缓存会在重启时骗过兜底）。
    若失败时仍有旧候选，`ok` 保持有效结果语义，另记本次尝试与降级状态。
    """
    try:
        fp = os.path.join(str(rt.book_dir), "prescreen_%s.json" % str(date))
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        payload = {"date": str(date),
                   "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   "ok": bool(ok),
                   "universe": (meta or {}).get("universe", ""),
                   "board_top_n": (meta or {}).get("board_top_n"),
                   "candidates_n": len(cand or {}),
                   "candidates": {str(c): {"prev_close": v.get("prev_close"),
                                           "ma20": v.get("ma20"),
                                           "board": v.get("board", ""),
                                           "name": v.get("name", "")}
                                  for c, v in (cand or {}).items() if isinstance(v, dict)}}
        if last_attempt_ok is not None:
            payload.update(last_attempt_ok=bool(last_attempt_ok),
                           degraded=bool(degraded),
                           last_attempt_error=last_attempt_error)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, allow_nan=False)
        os.replace(tmp, fp)
        return fp
    except Exception as e:
        try:
            rt.log(f"[OSC] ⚠️ 盘前候选留痕写入失败（不阻断）: {e}")
        except Exception:
            pass
        return None


# ── 当日状态落盘（2026-10-14）────────────────────────────────────────────
# 为什么必须落盘：`_STATE` 在内存里，掘金策略中途重启就全没了 ⇒
#   ① 挂单会一直冻结到 15:00（**正是 10-14 的事故形态**）；
#   ② `buy` 标记被清 ⇒ 下一根 bar 会**再跑一次首轮**（可能重复下单）。
# 落在账本目录（与 `fills_{date}.json` 同处），文件名 `day_state_{date}.json`。
def _day_state_fp(rt, date):
    return os.path.join(str(rt.book_dir), "day_state_%s.json" % str(date))


def save_day_state(rt, date) -> bool:
    """把补单相关状态落盘。异常吞掉（落盘失败不该拖垮主循环），返回是否成功。"""
    try:
        fp = _day_state_fp(rt, date)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"date": str(date), "buy_done": bool(_STATE.get("buy_done")),
                       "pending": _STATE.get("pending") or [],
                       "spares": _STATE.get("spares") or [],
                       "done_codes": sorted(_STATE.get("done_codes") or []),
                       "backfill": {str(k): v for k, v in (_STATE.get("backfill") or {}).items()}},
                      f, ensure_ascii=False)
        os.replace(tmp, fp)
        return True
    except Exception:
        return False


def load_day_state(rt, date) -> bool:
    """回读当日状态（仅当**同日**且内存里还没有时）。返回是否载入。"""
    if _STATE.get("pending") or _STATE.get("buy_done"):
        return False
    try:
        fp = _day_state_fp(rt, date)
        if not os.path.exists(fp):
            return False
        with open(fp, encoding="utf-8") as f:
            d = json.load(f) or {}
        if str(d.get("date")) != str(date):
            return False
        _STATE["buy_done"] = bool(d.get("buy_done"))
        _STATE["pending"] = list(d.get("pending") or [])
        _STATE["spares"] = list(d.get("spares") or [])
        _STATE["done_codes"] = set(d.get("done_codes") or [])
        # ⚠️ 2026-10-16 修：**合并**而不是覆盖轮次标记。`on_bar` 在调 `try_backfill` **之前**
        #    就把本轮标 True；而 `try_backfill` 开头的 `load_day_state` 若整体覆盖，
        #    会把刚打的标记冲掉 ⇒ 同一轮在后续 bar 被**重复触发**（生产上 buy_done=True
        #    恰好挡住，10-16 单测以 buy_done=False + 空 pending 复现了这条潜伏路径）。
        _bf = _STATE.setdefault("backfill", {})
        for k, v in (d.get("backfill") or {}).items():
            _bf.setdefault(int(k), v)
        return True
    except Exception:
        return False


def enabled(rt) -> bool:
    return bool(rt.param("osc_enabled", False))


def sell_enabled(rt) -> bool:
    return bool(rt.param("osc_sell_enabled", True))


def prepare(rt, context, now, today):
    """盘前预筛（每日一次）。返回是否**成功跑完**（异常 ⇒ False；0 只候选仍是成功）。

    P0-3（2026-10-26）起有两条入口：init 前移主路径（`prescreen_init`，开盘前跑）与
    09:31 钩子兜底。无旧候选时取数失败仍 fail-closed；已有候选则降级保留。
    """
    _top = int(rt.param("osc_board_top_n", 5))
    # 宇宙总闸（2026-10-14）：True=全市场扩展（当前行为）；False=退回猎手池。
    # 存在的理由：扩展只有行为证据、**没有通过的回测证据**（见 prescreen.universe_board_map 注释）
    # ⇒ 要一个当天可翻、不用改代码也不用动数据文件（industry_map.csv）的回退闸。
    _full = bool(rt.param("osc_universe_full_market", True))
    _t0 = _time.time()
    rt.log(f"[OSC] ── 盘前预筛开始 ──（前{_top}热门板块 ∩ "
           f"{'全市场' if _full else '猎手池(已回退)'} ∩ 昨收<MA20）")
    _ok = True
    _error = None
    _previous = dict(_STATE.get("candidates") or {})
    try:
        _cand = prescreen.premarket_candidates(_top, str(today), full_market=_full)
    except Exception as _e:
        _error = str(_e)
        rt.log(f"[OSC] 盘前预筛失败: {_e}")
        rt.audit({"event": "osc_prepare_fail", "date": str(today),
                      "error": _error[:200]})
        _cand = _previous
        _ok = False
        if _previous:
            rt.log(f"[OSC] 保留此前 {len(_previous)} 只有效候选（degraded）")
    _STATE["candidates"] = _cand
    _el = _time.time() - _t0
    rt.log(f"[OSC] 盘前超跌候选 {len(_cand)} 只（前{_top}板块内 且 昨收<MA20）· 耗时 {_el:.1f}s")
    if _cand:
        # 板块分布 + 最超跌样例：让人一眼看出"池子长什么样"，而不是只有一个数字
        _bc = Counter(v.get("board", "") for v in _cand.values())
        rt.log("[OSC]   板块分布: " + " / ".join(f"{b} {n}只" for b, n in _bc.most_common(8)))
        _deep = sorted(_cand.items(),
                       key=lambda kv: (kv[1].get("prev_close", 0) / (kv[1].get("ma20") or 1))
                       if kv[1].get("ma20") else 0)[:5]
        rt.log("[OSC]   最超跌 5 只(昨收/MA20-1): " + " / ".join(
            f"{c} {v['prev_close'] / v['ma20'] - 1:+.1%}" for c, v in _deep if v.get("ma20")))
    else:
        rt.log("[OSC]   [!] 0 只候选 -> 今日买侧不下单（fail-closed）")
    rt.audit({"event": "osc_prepare", "date": str(today), "candidates": len(_cand),
              "universe": "full_market" if _full else "hunter_pool"})
    # 2026-10-17：盘前候选**名单**落盘（此前只有审计里的一个数量，名单只在内存
    # `_STATE["candidates"]` ⇒ 进程重启即丢，GUI 也无从回看）。供「每日选股记录」用。
    _STATE["prescreen_meta"] = {"universe": "full_market" if _full else "hunter_pool",
                                "board_top_n": _top}
    if _ok:
        _write_prescreen(rt, today, _cand, _STATE["prescreen_meta"], ok=True)
    else:
        _write_prescreen(rt, today, _cand, _STATE["prescreen_meta"],
                         ok=bool(_cand), last_attempt_ok=False,
                         degraded=bool(_cand), last_attempt_error=_error)
    return _ok


def _read_prescreen_cache(rt, today):
    """读 `{book_dir}/prescreen_{today}.json`——**仅当 date==今日 且 ok 不为 False** 才算新鲜。

    返回候选 dict（合法的「0 只候选」结果返回 {}）；文件缺失/读坏/隔日/失败标记 ⇒ None。
    配套 P0-3：进程盘中重启时 init 预筛直接装载缓存（秒级），不再重复全市场批量取数；
    顺带恢复 `prescreen_meta`（重启后 10:00 买侧的 prescreen_meta 留痕不断档）。
    """
    try:
        fp = os.path.join(str(rt.book_dir), "prescreen_%s.json" % str(today))
        if not os.path.exists(fp):
            return None
        with open(fp, encoding="utf-8") as f:
            d = json.load(f) or {}
        if str(d.get("date")) != str(today):
            return None
        if d.get("ok") is False:            # 失败写出的空名单不算「新鲜结果」（2026-10-26 P0-3）
            return None
        cand = {}
        for c, v in (d.get("candidates") or {}).items():
            if not isinstance(v, dict):
                continue
            try:
                pc, ma = float(v.get("prev_close")), float(v.get("ma20"))
            except (TypeError, ValueError):
                continue
            if pc > 0 and ma > 0:
                cand[str(c)] = {"prev_close": pc, "ma20": ma,
                                "board": v.get("board", ""), "name": v.get("name", "")}
        _STATE["prescreen_meta"] = {"universe": d.get("universe", ""),
                                    "board_top_n": d.get("board_top_n")}
        return cand
    except Exception:
        return None


def prescreen_init(rt, today=None) -> bool:
    """P0-3 实时性修复（2026-10-26）：盘前预筛从 09:31 事件钩子**前移到策略 init**。

    等价性：`prescreen.premarket_candidates` 只用 D-1 及之前的完整日线（date 闸剔除
    当日 forming bar），与盘中行情无关 ⇒ init（开盘前、事件循环开始前）跑与 09:31 跑
    结果一致；而 init 阶段跑 169s~472s 的全市场批量取数**不会**连坐 fill 派发与心跳。

    语义：
      · 有当日新鲜缓存（重启场景）⇒ **只装载不重跑**，置 `prepare`/`prepare_via="init"`；
      · 无缓存 ⇒ 现场执行 `prepare`；成功（含 0 只候选）同样置位，09:31 钩子跳过；
      · 失败/异常 ⇒ **不动任何状态**（fail-open：绝不阻止策略启动），
        09:31 钩子看到 `prepare` 未置位会照旧执行（兜底重跑一次）。
    返回是否拿到当日可用结果。
    """
    if not enabled(rt):
        return False
    _today = str(today or _dt.date.today())
    _t0 = _time.time()
    _ok = False
    _src = "init_run"
    try:
        _cached = _read_prescreen_cache(rt, _today)
        if _cached is not None:
            _STATE["candidates"] = _cached
            _ok = True
            _src = "cache"
            rt.log(f"[OSC] init 预筛：发现当日新鲜缓存（{_today}）⇒ 直接装载 "
                   f"{len(_cached)} 只候选（进程重启补载，不再重复取数）")
        else:
            rt.log("[OSC] init 预筛：无当日缓存 ⇒ 开盘前现场执行"
                   "（P0-3 主路径，事件循环开始前完成，不再占用 09:31 事件线程）")
            _ok = bool(prepare(rt, None, None, _today))
    except Exception as _e:
        rt.log(f"[OSC] ⚠️ init 预筛异常: {_e}")
        _ok = False
    _el = _time.time() - _t0
    if _ok:
        _STATE["prepare"] = True
        _STATE["prepare_via"] = "init"
        rt.log(f"[OSC] init 预筛完成：{len(_STATE.get('candidates') or {})} 只候选 · "
               f"来源 {_src} · 总耗时 {_el:.1f}s（09:31 钩子将跳过）")
        try:
            rt.audit({"event": "osc_prescreen_init", "date": _today, "source": _src,
                      "candidates": len(_STATE.get("candidates") or {}),
                      "elapsed_sec": round(_el, 1)})
        except Exception:
            pass
        return True
    rt.log("[OSC] ⚠️ init 预筛未成功（fail-open：策略照常启动，09:31 钩子兜底重跑一次）")
    try:
        rt.audit({"event": "osc_prescreen_init_fail", "date": _today,
                  "elapsed_sec": round(_el, 1)})
    except Exception:
        pass
    return False



def bars_from_10am_df(df, codes, now):
    """GM history(df=True) → {code: 10:00棒的 low/close/amount/volume}。防御式。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    try:
        df = df.copy()
        df["_code"] = df["symbol"].astype(str).str.split(".").str[-1]
        df["_hm"] = pd.to_datetime(df["eob"]).dt.strftime("%H:%M:%S")
        df = df[df["_hm"] == "10:00:00"]
        for c in codes:
            _row = df[df["_code"] == c]
            if _row.empty:
                continue
            _r = _row.iloc[0]
            try:
                low = float(_r.get("low") or 0)
                close = float(_r.get("close") or 0)
                amount = float(_r.get("amount") or 0)
                volume = float(_r.get("volume") or 0)
            except Exception:
                continue
            if low > 0 and close > 0:
                out[c] = {"low": low, "close": close, "amount": amount, "volume": volume}
    except Exception:
        pass
    return out



def try_buy(rt, context, now) -> int:
    """10:00 超跌跳空确认买入（每日一次）。返回已下单腿数。fail-closed：候选/棒缺失当日不下单。"""
    _exec = executor
    # KILL_SWITCH（人工红按钮，存在即禁新开仓）：2026-10-12 之前只有已删除的旧引擎读它，
    # 删后由本条（**本策略唯一的开仓路径**）承接，避免整个系统失去这道人工闸。
    try:
        if rt.kill_switch():
            rt.log(f"[OSC] [X] KILL_SWITCH 存在 -> 今日不开新仓")
            rt.audit({"event": "osc_skip", "reason": "kill_switch", "time": str(now)})
            return 0
    except Exception:
        pass
    _cand = dict(_STATE["candidates"])
    if not _cand:
        rt.audit({"event": "osc_skip", "reason": "no_candidates", "time": str(now)})
        try:
            _cash0 = float(rt.gateway(context).get_cash() or 0)
        except Exception:
            _cash0 = 0.0
        _res = _exec._write_fills(
            str(now.date()), "live", now.strftime("%Y-%m-%d %H:%M:%S"),
            [], [], _cash0, 0.0, dict(_exec.DEFAULTS), rt.book_dir,
            note="no_picks", spares=[], skip_reason="no_candidates")
        _stamp_after_buy(rt, now, _res, {})
        return 0
    _pc, _ma = prescreen.split_into_prev_close_ma20(_cand)
    _board = {c: v.get("board", "") for c, v in _cand.items()}
    _names = {c: (v.get("name") or "") for c, v in _cand.items()}   # 仅用于判定过程显示
    _codes = sorted(_cand)
    _syms = [rt.code_to_gm(c) for c in _codes]
    _start = now.strftime("%Y-%m-%d 09:25:00")
    _end = now.strftime("%Y-%m-%d 10:01:00")
    try:
        _df = rt.sdk_call("osc_10am_bars", partial(
            rt.gm.history, symbol=_syms, frequency="1800s", start_time=_start, end_time=_end,
            fields="symbol,eob,open,high,low,close,volume,amount",
            adjust=rt.gm.ADJUST_PREV, df=True))
    except Exception as _e:
        rt.log(f"[OSC] 10:00 棒取数失败（fail-closed，当日不下单）: {_e}")
        rt.audit({"event": "osc_fetch_fail", "error": str(_e)[:200], "time": str(now)})
        return 0
    _bars = bars_from_10am_df(_df, _codes, now)
    rt.log(f"[OSC] 10:00 取数: 拿到 {len(_bars)}/{len(_codes)} 只的 10:00 棒"
           + (f"（缺 {len(_codes) - len(_bars)} 只，fail-closed 跳过）" if len(_bars) < len(_codes) else ""))
    if not _bars:
        rt.audit({"event": "osc_skip", "reason": "no_bars", "time": str(now)})
        return 0
    _book = rt.book_dir
    _params = {
        "top_n": int(rt.param("osc_top_n", 4)),
        "single_budget": float(rt.param("osc_single_budget", 100000.0)),
        "daily_budget": float(rt.param("osc_daily_budget", 400000.0)),
        "cash_headroom": float(rt.param("osc_cash_headroom", 0.95)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = executor.run_buy_exec(rt.gateway(context), str(now.date()),
                                 prev_close=_pc, ma20=_ma, bars=_bars, board=_board,
                                 allowed_boards=None, book_dir=_book, params=_params,
                                 dry_run=False, log=print, names=_names,
                                 prescreen_meta=_STATE.get("prescreen_meta") or {})
        _sm = (_res or {}).get("summary") or {}
        rt.audit({"event": "osc_buy_exec", "date": str(now.date()),
                      "candidates": _sm.get("candidates"), "buys": _sm.get("buys_placed"),
                      "rejected": _sm.get("rejected_count"),
                      "picks": [p["code"] for p in (_res or {}).get("picks") or []]})
        _pk = (_res or {}).get("picks") or []
        if _pk:
            rt.log("[OSC] 10:00 选中(gap 降序): " + " / ".join(
                f"{p['code']} gap{p['gap']:+.1%}" for p in _pk))
        else:
            rt.log("[OSC] 10:00 无信号触发（缺口未守住/未站上昨收或 VWAP）-> 今日不买")
        rt.log(f"[OSC] 10:00 买侧执行: {json.dumps(_sm, ensure_ascii=False)}")
        _stamp_after_buy(rt, now, _res, _pc)
        return int(_sm.get("buys_placed") or 0)
    except Exception as _e:
        rt.log(f"[OSC] 买侧执行失败（不阻断主循环）: {_e}")
        try:
            rt.risk(str(now), "osc_exec_error", f"超跌跳空确认执行异常: {str(_e)[:200]}", code="")
        except Exception:
            pass
        return 0


def _stamp_after_buy(rt, now, res, prev_close) -> None:
    """首轮买侧跑完 → 把补单所需状态存进 `_STATE` 并落盘（2026-10-14）。

    存什么：
      · `fills`      —— 额度账真源（`backfill.committed_notional` 用它算"已占用名义"）
      · `pending`    —— 未成交买单（撤单对象）
      · `spares`     —— 顺移候补（executor 落盘的那份）
      · `done_codes` —— 今日已碰过的票（防重复顺移/追价）
      · `buy_done`   —— 首轮已执行（**落盘**，防进程重启后重复下单）

    无候补 / 无未成交时也照样落盘 `buy_done` —— 那是"今日首轮已完成"的证据。
    """
    try:
        _res = res or {}
        _fills = dict(_res)
        # fills 里的 buys 才够重建待补单；`_write_fills` 已带 order_id/qty_remaining
        try:
            _pend = backfill.pending_from_fills(_fills)
        except Exception:
            _pend = []
        _spares = list(_res.get("spares") or [])
        # 无未成交 ⇒ 没有可撤的、也没必要顺移（首轮全成）
        if not _pend:
            _spares = []
        _done = set(_STATE.get("done_codes") or ())
        for p in (_res.get("picks") or []):
            if p.get("code"):
                _done.add(str(p["code"]))
        for b in (_res.get("buys") or []):
            if b.get("code"):
                _done.add(str(b["code"]))
        _STATE["fills"] = _fills
        _STATE["pending"] = _pend
        _STATE["spares"] = _spares
        _STATE["done_codes"] = _done
        _STATE["buy_done"] = True
        save_day_state(rt, str(now.date()))
        if _pend:
            rt.log("[OSC] 10:00 未成交 %d 只（%s）→ 等候补轮（%d 只候补、%s 起）" % (
                len(_pend), "/".join(p["code"] for p in _pend), len(_spares),
                _hhmm_str(backfill_times(rt)[0]) if backfill_times(rt) else "-"))
        elif _spares:
            rt.log("[OSC] 10:00 首轮全部成交，无需补单（已备候补 %d 只）" % len(_spares))
    except Exception as _e:
        rt.log(f"[OSC] ⚠️ 补单状态登记失败（不阻断）: {_e}")


def backfill_times(rt) -> list:
    """补单轮次时点列表（由参数派生）。总闸关闭 ⇒ 返回空表（等于没有这个机制）。"""
    if not bool(rt.param("osc_backfill_enabled", False)):
        return []
    try:
        first = int(rt.param("osc_backfill_first_hhmm", 1008))
        gap = int(rt.param("osc_backfill_interval_min", 6))
        rounds = int(rt.param("osc_backfill_rounds", 2))
    except (TypeError, ValueError):
        return []
    out = []
    for i in range(max(0, rounds)):
        hhmm = first + i * gap
        out.append(dtime(hhmm // 100, hhmm % 100))
    return out


def backfill_deadline(rt):
    """补单截止时点（**此点之后不再开新仓**，与"最多 N 轮"双保险）。"""
    try:
        v = int(rt.param("osc_backfill_deadline_hhmm", 1020))
    except (TypeError, ValueError):
        return dtime(10, 20)
    return dtime(v // 100, v % 100)


def _hhmm_str(t):
    return "%02d:%02d" % (t.hour, t.minute)


def fetch_current_snapshot(rt, codes, now) -> dict:
    """盘中实时快照（**一次调用拿全复检三样**）：{code: {price, vwap, ask_v}}。

    用 `gm.current` 而不是 `history(1800s)`：前者一次往返同时给出现价、**当日累计量额**
    （`cum_amount`/`cum_volume`）与盘口 `quotes[0].ask_v` —— 补单是加在轮询里的，
    多打一次接口就是多一次冻结风险（本仓对轮询速度是硬约束）。
    取数失败/字段缺失 → 该 code 不入表 ⇒ 复检 fail-closed 跳过。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    syms = [rt.code_to_gm(c) for c in codes]
    try:
        rows = rt.sdk_call("osc_backfill_current",
                           partial(rt.gm.current, symbols=syms))
    except Exception as _e:
        rt.log(f"[OSC] ⚠️ 补单取数失败（本轮放弃补单）: {_e}")
        return {}
    out = {}
    for r in (rows if isinstance(rows, (list, tuple)) else [rows]):
        if r is None:
            continue
        try:
            sym = (r.get("symbol") if isinstance(r, dict) else getattr(r, "symbol", None))
            if not sym:
                continue
            code = str(sym).split(".")[-1]
            def _g(k):
                return r.get(k) if isinstance(r, dict) else getattr(r, k, None)
            price = float(_g("price") or 0)
            if price <= 0:
                continue
            q0 = (_g("quotes") or [{}])
            q0 = q0[0] if q0 else {}
            out[str(code)] = {
                "price": price,
                "vwap": backfill.day_vwap(_g("cum_amount"), _g("cum_volume")),
                "ask_v": (q0.get("ask_v") if isinstance(q0, dict)
                          else getattr(q0, "ask_v", None)),
            }
        except Exception:
            continue
    return out


def try_backfill(rt, context, now, today, round_idx) -> int:
    """一轮补单：**未成交 ⇒ 撤单 ⇒ 候补复检 ⇒ 顺移**（2026-10-14）。

    返回本轮新下单腿数。全程 fail-closed：取数失败/无候补/额度不足 ⇒ 这轮不动（既不撤也不买）。
    """
    if not bool(rt.param("osc_backfill_enabled", False)):
        return 0
    try:
        if rt.kill_switch():
            rt.log("[OSC] [X] KILL_SWITCH 存在 -> 补单轮中止（不撤单、不开新仓）")
            rt.audit({"event": "osc_backfill_skip", "reason": "kill_switch",
                      "round": int(round_idx), "time": str(now)})
            return 0
    except Exception:
        pass
    load_day_state(rt, str(now.date()))
    _pend = list(_STATE.get("pending") or [])
    if not _pend:
        return 0                              # 首轮全成 / 无挂单 ⇒ 空转（不写日志刷屏）
    # 候补按 done_codes 剪枝（2026-10-16 生产施工）：上一轮已顺移买过的票**不得再买**——
    # 「顺移下一名」的语义是往候补列表**下游**走，不是撤了上一轮的挂单再原票追回
    # （那是方案 A 的追价，owner 已否决）。done_codes 在首轮登记与每轮下单后更新。
    _done0 = set(_STATE.get("done_codes") or ())
    _spares = [s for s in (_STATE.get("spares") or []) if str(s.get("code")) not in _done0]
    _STATE["spares"] = _spares
    _fills = _STATE.get("fills") or {}
    if not _spares:
        rt.log(f"[OSC] 补单第{int(round_idx) + 1}轮：有 {len(_pend)} 只未成交但**候补已耗尽** "
               f"⇒ 只撤单不再开仓")
    _codes = [p["code"] for p in _pend] + [s["code"] for s in _spares]
    _pc = prescreen.split_into_prev_close_ma20(_STATE.get("candidates") or {})[0]
    _quotes = fetch_current_snapshot(rt, _codes, now)
    if not _quotes:
        rt.log(f"[OSC] 补单第{int(round_idx) + 1}轮：取数失败 ⇒ 本轮放弃（挂单保持不动）")
        rt.audit({"event": "osc_backfill_skip", "reason": "no_quotes",
                  "round": int(round_idx), "time": str(now)})
        return 0
    _params = {
        "single_budget": float(rt.param("osc_single_budget", 100000.0)),
        "daily_budget": float(rt.param("osc_daily_budget", 400000.0)),
        "cash_headroom": float(rt.param("osc_cash_headroom", 0.95)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = backfill.run_backfill_exec(
            rt.gateway(context), str(now.date()), _fills, _pend, _spares, _pc, _quotes,
            positions=None, params=_params, dry_run=False,
            sleep_fn=_time.sleep, log=print)
    except Exception as _e:
        rt.log(f"[OSC] 补单第{int(round_idx) + 1}轮执行失败（不阻断主循环）: {_e}")
        return 0

    _cr = _res.get("cancel") or {}
    _placed = _res.get("placed") or []
    rt.log("[OSC] 补单第%d轮: 撤单确认 %d / 未确认 %d / 失败 %d · 释放额度 %s · "
           "顺移下单 %d 只 · 跳过 %s" % (
               int(round_idx) + 1, len(_cr.get("released") or []),
               len(_cr.get("unconfirmed") or []), len(_cr.get("failed") or []),
               _res.get("cash_budget"), len(_placed),
               ",".join("%s:%s" % (s["code"], s["reason"]) for s in
                        (_res.get("skipped") or [])[:6]) or "-"))
    rt.audit({"event": "osc_backfill", "date": str(now.date()), "round": int(round_idx),
              "released": _cr.get("released"), "unconfirmed": _cr.get("unconfirmed"),
              "failed": _cr.get("failed"), "cash_budget": _res.get("cash_budget"),
              "placed": [{"code": p["code"], "qty": p["qty"], "status": p["status"]}
                         for p in _placed],
              "skipped": _res.get("skipped"), "note": _res.get("note"),
              "time": str(now)})

    # ── 留痕 + 入账（2026-10-16 生产施工）────────────────────────────────
    # ① 本轮结果并入 fills_{date}.json：buys 带 round/shift_from，撤单明细进 backfill_rounds；
    # ② 顺移买入**并入持仓账本**——不并的话，顺移买到的票 14:50 止损/次日早盘冲高卖
    #    根本看不见（卖侧真源 = positions.json）⇒ 孤儿持仓。纪律与首轮一致：
    #    「挂单即入账 + 每次卖前按账户夹紧」（executor.reconcile_book）。
    _shift_from = list(_cr.get("released") or [])
    _gap_of = {str(s.get("code")): s.get("gap") for s in _spares}
    _recs = None
    try:
        _recs = executor.merge_backfill_fills(
            rt.book_dir, str(now.date()), int(round_idx) + 1, _res,
            placed=[dict(p, gap=_gap_of.get(str(p.get("code")))) for p in _placed],
            shift_from=_shift_from, log=print)
    except Exception as _e:
        rt.log(f"[OSC] ⚠️ 补单轮 fills 留痕失败（不阻断）: {_e}")
    if _recs:
        try:
            _pc_all, _ma_all = prescreen.split_into_prev_close_ma20(
                _STATE.get("candidates") or {})
            _picks_like = [{"code": str(s.get("code")), "board": s.get("board", "")}
                           for s in _spares]
            executor.register_buys_to_book(rt.book_dir, str(now.date()), _recs,
                                           _picks_like, _pc_all, _ma_all, print)
        except Exception as _e:
            rt.log(f"[OSC] ⚠️ 顺移买入入账失败（不阻断；卖前对账会夹紧）: {_e}")

    # 状态推进：撤单确认的不再挂账；本轮下单的票若仍未成交，进下一轮 pending
    _released = set(_cr.get("released") or [])
    for _f in (_cr.get("filled") or []):      # 竞态成交的委托也算"已了结"，不再挂账
        if _f.get("code"):
            _released.add(_f["code"])
    _done = set(_STATE.get("done_codes") or ())
    _next_pend = []
    for p in _pend:
        if p["code"] in _released:
            continue                          # 已撤成 / 已竞态成交 ⇒ 不再挂账
        _next_pend.append(p)                  # 未确认的撤单**继续挂账**（额度不释放）
    for p in _placed:
        _done.add(str(p["code"]))
        if p.get("qty_remaining") and p.get("order_id") and p.get("status") not in (
                "filled", "rejected", "expired", "cancelled", "error"):
            _next_pend.append({"code": p["code"], "order_id": p["order_id"],
                               "gap": None, "price": float(p.get("price") or 0),
                               "qty_placed": int(p.get("qty") or 0),
                               "filled_qty": int(p.get("filled_qty") or 0),
                               "qty_remaining": int(p.get("qty_remaining") or 0),
                               "ref_price": float(p.get("filled_price")
                                                  or p.get("price") or 0)})
    _STATE["pending"] = _next_pend
    _STATE["done_codes"] = _done
    save_day_state(rt, str(now.date()))
    return len(_placed)


def sell_codes(rt) -> list:
    """当前超跌跳空确认账本在持仓的 6 位码列表（空=无需卖侧检查）。"""
    try:
        _exec = executor
        _book = rt.book_dir
        return sorted((executor.load_book(_book).get("open") or {}).keys())
    except Exception:
        return []


def latest_close_from_df(df, codes, now):
    """GM history(df=True) → {code: 当日最新一根 bar 的 close}。防御式。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    try:
        df = df.copy()
        df["_code"] = df["symbol"].astype(str).str.split(".").str[-1]
        df = df.sort_values("eob")
        for c in codes:
            _row = df[df["_code"] == c]
            if _row.empty:
                continue
            _r = _row.iloc[-1]
            try:
                close = float(_r.get("close") or 0)
            except Exception:
                continue
            if close > 0:
                out[c] = close
    except Exception:
        pass
    return out



def sell_time(rt):
    """卖侧检查时点（dtime）。"""
    _hhmm = int(rt.param("osc_sell_time_hhmm", 1450))
    return dtime(_hhmm // 100, _hhmm % 100)


# ── P1-7（2026-10-26）：卖侧每日一次标记「成功才置位」──────────────────────
# 缺陷原型（10-26 性能审计 H7）：标记原先在钩子执行**前置位**
# （`_STATE["sell"] = True` 先于 try_sell）——钩子一抛异常，卖侧**当日永久缺席**
# （与历史「14:55 尾盘归位未执行」事故同型；卖侧是风控腿，缺席 = 止损/到期不执行）。
# 现改为：执行成功才置位；抛异常 ⇒ 不置位 + 计一次重试，下一根 60s bar 再试；
# **重试上限 + 最晚时限双保险**防死循环；耗尽 ⇒ 置位放弃 + risk 告警（让 watcher 推出去）。
SELL_RETRY_MAX = 3          # 14:50 卖侧：首次失败后的最大重试次数（60s bar ⇒ ~3 分钟内耗尽）


def sell_retry_deadline(rt):
    """卖侧重试最晚时限（过了不再重试；默认 14:58，给 14:50 首试留 ~8 分钟窗）。

    注意：首试**不受此时限限制**（14:59 才启动的进程也该卖一次）；时限只闸重试。
    """
    try:
        v = int(rt.param("osc_sell_retry_deadline_hhmm", 1458))
    except (TypeError, ValueError):
        return dtime(14, 58)
    return dtime(v // 100, v % 100)


def try_sell(rt, context, now, today) -> int:
    """14:50 超跌跳空确认卖侧（每日一次）：SL5只深 close 止损 + 到期平仓。返回已下单腿数。

    P1-7（2026-10-26）：取数/执行失败**记证后 re-raise** 给 on_bar——由它回滚每日标记
    并在下一根 bar 重试（原 fail-closed「一次失败当日不卖」= 卖侧当日永久缺席）。
    正常路径（含「无持仓」「无价格」）依旧返回 int，不抛。
    """
    _exec = executor
    _reconcile_book(rt, context)                 # ⚠️ 必须先对账（同上）
    _codes = sell_codes(rt)
    if not _codes:
        rt.log("[OSC] 14:50 卖侧: 账本无持仓，跳过")
        return 0
    rt.log(f"[OSC] 14:50 卖侧: 持仓 {len(_codes)} 只 → {_codes}")
    _syms = [rt.code_to_gm(c) for c in _codes]
    _start = now.strftime("%Y-%m-%d 09:25:00")
    _end = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _df = rt.sdk_call("osc_sell_bars", partial(
            rt.gm.history, symbol=_syms, frequency="60s", start_time=_start, end_time=_end,
            fields="symbol,eob,close", adjust=rt.gm.ADJUST_PREV, df=True))
    except Exception as _e:
        rt.log(f"[OSC] 卖侧现价取数失败（本次放弃，下一根 bar 重试）: {_e}")
        rt.audit({"event": "osc_sell_fetch_fail", "error": str(_e)[:200], "time": str(now)})
        raise
    _prices = latest_close_from_df(_df, _codes, now)
    if not _prices:
        rt.audit({"event": "osc_sell_skip", "reason": "no_prices", "time": str(now)})
        return 0
    _book = rt.book_dir
    _params = {
        "hold_days": int(rt.param("osc_hold_days", 5)),
        "stop_pct": float(rt.param("osc_stop_pct", 0.05)),
        "stop_deep_dev20": float(rt.param("osc_stop_deep_dev20", -0.07)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = executor.run_sell_exec(rt.gateway(context), str(today), _prices,
                                  book_dir=_book, params=_params, dry_run=False, log=print)
        _sm = (_res or {}).get("summary") or {}
        rt.audit({"event": "osc_sell_exec", "date": str(today),
                      "sells": _sm.get("sells_placed"), "stops": _sm.get("stop_count"),
                      "expired": _sm.get("expired_count"),
                      "open_after": _sm.get("open_after"),
                      "rejected": _sm.get("rejected_count")})
        rt.log(f"[OSC] 卖侧执行: {json.dumps(_sm, ensure_ascii=False)}")
        return int(_sm.get("sells_placed") or 0)
    except Exception as _e:
        rt.log(f"[OSC] 卖侧执行失败（本次放弃，下一根 bar 重试）: {_e}")
        try:
            rt.risk(str(now), "osc_sell_error", f"超跌跳空确认卖侧异常: {str(_e)[:200]}", code="")
        except Exception:
            pass
        raise


def morning_high_from_df(df, codes, now):
    """GM history(df=True) → {code: 早盘(09:30~当前)最高价}。防御式，取 high 列最大值。"""
    out = {}
    if df is None or len(df) == 0:
        return out
    try:
        df = df.copy()
        df["_code"] = df["symbol"].astype(str).str.split(".").str[-1]
        for c in codes:
            _row = df[df["_code"] == c]
            if _row.empty:
                continue
            try:
                hi = float(_row["high"].max())
            except Exception:
                continue
            if hi > 0:
                out[c] = hi
    except Exception:
        pass
    return out


def morning_sell_time(rt):
    """早盘冲高卖检查时点（dtime）。"""
    _hhmm = int(rt.param("osc_morning_sell_time_hhmm", 1000))
    return dtime(_hhmm // 100, _hhmm % 100)


MORNING_SELL_RETRY_MAX = 3      # P1-7：早盘冲高卖首次失败后的最大重试次数


def morning_sell_retry_deadline(rt):
    """早盘冲高卖重试最晚时限（默认 10:30；「早盘冲高」语义只适用于上午，防全天死循环）。

    与卖侧同款：首试不受时限限制，时限只闸重试。
    """
    try:
        v = int(rt.param("osc_morning_sell_retry_deadline_hhmm", 1030))
    except (TypeError, ValueError):
        return dtime(10, 30)
    return dtime(v // 100, v % 100)


# ── 预挂目标限价 · S0 影子探测（2026-10-19，只记录不下单）────────────────
# 设计：doc/research/2026-10-19_预挂目标限价_paper实验设计.md §六 S0
# 开关默认关；开启后 09:31 写计划、10:00 用 60s 棒回填触及/参照价，不碰任何委托。
def preplace_probe_enabled(rt) -> bool:
    try:
        return bool(rt.param("osc_preplace_probe_enabled", False))
    except Exception:
        return False


def probe_preplace_plan(rt, now, today) -> int:
    """09:31：把当日 D+1 持仓的目标限价落盘（时间戳证明"盘前已知"）。"""
    if not preplace_probe_enabled(rt):
        return 0
    try:
        _book = executor.load_book(rt.book_dir)
        _target = float(rt.param("osc_pop_target_pct", 0.03))
        _rows = preplace_probe.build_plan(_book.get("open") or {}, today,
                                          target_pct=_target)
        preplace_probe.write(rt.book_dir, today, "plan", _rows, _target)
        rt.audit({"event": "osc_preplace_probe_plan", "date": str(today),
                  "n": len(_rows), "codes": [r["code"] for r in _rows],
                  "time": str(now)})
        if _rows:
            rt.log("[OSC] 预挂探测(计划): %s" % " · ".join(
                "%s@%.2f" % (r["code"], r["limit_px"]) for r in _rows))
        return len(_rows)
    except Exception as _e:
        rt.log(f"[OSC] 预挂探测(计划)失败（不影响主循环）: {_e}")
        return 0


def probe_preplace_final(rt, context, now, today) -> int:
    """10:00：只用 60s 行情回填——触及时点 / 10:00 参照价 / 保守成交口径。"""
    if not preplace_probe_enabled(rt):
        return 0
    try:
        _target = float(rt.param("osc_pop_target_pct", 0.03))
        _payload = preplace_probe.read(rt.book_dir, today) or {}
        _rows = _payload.get("rows") if _payload.get("phase") == "plan" else None
        if not _rows:                      # 进程在 09:31 之后才启动：重建计划（无前视）
            _book = executor.load_book(rt.book_dir)
            _rows = preplace_probe.build_plan(_book.get("open") or {}, today,
                                              target_pct=_target)
        if not _rows:
            preplace_probe.write(rt.book_dir, today, "final", [], _target,
                                 extra={"note": "no_d1_positions"})
            return 0
        _codes = [r["code"] for r in _rows]
        _syms = [rt.code_to_gm(c) for c in _codes]
        _start = now.strftime("%Y-%m-%d 09:25:00")
        _end = now.strftime("%Y-%m-%d %H:%M:%S")
        _df = rt.sdk_call("osc_preplace_probe_bars", partial(
            rt.gm.history, symbol=_syms, frequency="60s", start_time=_start, end_time=_end,
            fields="symbol,eob,high,low,close", adjust=rt.gm.ADJUST_PREV, df=True))
        _cut = _dt.datetime.combine(now.date(), morning_sell_time(rt))
        _enriched = preplace_probe.enrich(_rows, _df, cutoff=_cut)
        preplace_probe.write(rt.book_dir, today, "final", _enriched, _target,
                             extra={"cutoff": _cut.strftime("%Y-%m-%d %H:%M:%S")})
        rt.audit({"event": "osc_preplace_probe_final", "date": str(today),
                  "n": len(_enriched),
                  "touched": sum(1 for r in _enriched if r.get("touch_time")),
                  "conservative": sum(1 for r in _enriched if r.get("conservative_fill")),
                  "time": str(now)})
        rt.log("[OSC] 预挂探测(完成): " + " · ".join(
            "%s 目标%.2f 触及=%s 10:00=%.2f" %
            (r["code"], r["limit_px"], r.get("touch_time") or "否",
             r.get("px_10am_ref") or 0) for r in _enriched))
        return len(_enriched)
    except Exception as _e:
        rt.log(f"[OSC] 预挂探测(完成)失败（不影响主循环）: {_e}")
        try:
            rt.audit({"event": "osc_preplace_probe_fail", "error": str(_e)[:200],
                      "time": str(now)})
        except Exception:
            pass
        return 0


def _reconcile_book(rt, context) -> None:
    """卖前先把账本按**账户实际持仓**夹紧（挂单未成交/部分成交都会让账本虚高）。

    见 `executor.reconcile_book` 的说明 —— 上线首日实测出现过「账本 5 只 / 账户 3 只」。
    """
    try:
        _gw = rt.gateway(context)
        _acct = {str(c).split(".")[-1]: int((v or {}).get("qty") or 0)
                 for c, v in (_gw.get_positions() or {}).items()}
        _n = executor.reconcile_book(rt.book_dir, _acct)
        if _n:
            rt.log(f"[OSC] 账本对账: 按账户夹紧/剔除 {_n} 笔（挂单未成交 or 部分成交）")
    except Exception as _e:
        rt.log(f"[OSC] 账本对账失败（不阻断本次卖出）: {_e}")


def try_morning_sell(rt, context, now, today) -> int:
    """10:00 超跌跳空确认早盘冲高卖（每日一次，先于买侧）：D+1 持仓早盘冲 ≥ entry×(1+target) 就卖。

    只卖 days_held==0（次日）持仓；更老持仓不动，交 14:50 止损/到期。与买侧同刻触发，
    但钩子排在买侧之前，先卖回笼现金再买。
    """
    _exec = executor
    _reconcile_book(rt, context)                 # ⚠️ 必须先对账：挂单未成交会让账本虚高
    _codes = sell_codes(rt)
    if not _codes:
        rt.log("[OSC] 10:00 早盘冲高卖: 账本无持仓，跳过")
        return 0
    rt.log(f"[OSC] 10:00 早盘冲高卖: 持仓 {len(_codes)} 只 → {_codes}")
    _syms = [rt.code_to_gm(c) for c in _codes]
    _start = now.strftime("%Y-%m-%d 09:25:00")
    _end = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        _df = rt.sdk_call("osc_morning_high_bars", partial(
            rt.gm.history, symbol=_syms, frequency="1800s", start_time=_start, end_time=_end,
            fields="symbol,eob,high", adjust=rt.gm.ADJUST_PREV, df=True))
    except Exception as _e:
        rt.log(f"[OSC] 早盘最高价取数失败（本次放弃，下一根 bar 重试）: {_e}")
        rt.audit({"event": "osc_morning_sell_fetch_fail", "error": str(_e)[:200], "time": str(now)})
        raise
    _highs = morning_high_from_df(_df, _codes, now)
    if not _highs:
        rt.audit({"event": "osc_morning_sell_skip", "reason": "no_highs", "time": str(now)})
        return 0
    _book = rt.book_dir
    _params = {
        "pop_target_pct": float(rt.param("osc_pop_target_pct", 0.03)),
        "order_poll_rounds": int(rt.param("osc_order_poll_rounds", 3)),
        "order_poll_sleep_sec": float(rt.param("osc_order_poll_sleep_sec", 2.0)),
    }
    try:
        _res = executor.run_morning_sell_exec(rt.gateway(context), str(today), _highs,
                                          book_dir=_book, params=_params, dry_run=False, log=print)
        _sm = (_res or {}).get("summary") or {}
        rt.audit({"event": "osc_morning_sell_exec", "date": str(today),
                      "sells": _sm.get("sells_placed"), "pops": _sm.get("pop_count"),
                      "open_after": _sm.get("open_after"),
                      "rejected": _sm.get("rejected_count")})
        rt.log(f"[OSC] 早盘冲高卖执行: {json.dumps(_sm, ensure_ascii=False)}")
        return int(_sm.get("sells_placed") or 0)
    except Exception as _e:
        rt.log(f"[OSC] 早盘冲高卖执行失败（本次放弃，下一根 bar 重试）: {_e}")
        try:
            rt.risk(str(now), "osc_morning_sell_error", f"超跌跳空确认早盘卖侧异常: {str(_e)[:200]}", code="")
        except Exception:
            pass
        raise

# ════════════════════════ 分发（宿主唯一入口）════════════════════════

def on_bar(rt, context, now, today, t) -> None:
    """四个钩子的时间窗 + 每日一次 + 模式闸。异常一律吞掉（不阻断主循环）。"""
    if not enabled(rt):
        return
    live = rt.is_live(context)
    # 09:31 盘前预筛（候选要先于 10:00 备好）
    # P0-3（2026-10-26）：主路径已前移到 init（`prescreen_init`，由宿主 gm_main.init 调）。
    # 本钩子保留为**兜底**：`prepare` 未置位（init 未跑/失败，或进程盘中重启且当日无结果）
    # 时照旧执行；init 已成功（`prepare_via=="init"`，含当日新鲜缓存装载）时跳过——
    # 不再在事件派发线程上重复跑全市场批量取数（169s~472s 阻塞的原始事故现场）。
    if live and t >= dtime(9, 31) and not _STATE["prepare"]:
        _STATE["prepare"] = True
        try:
            prepare(rt, context, now, today)
        except Exception as _e:
            rt.log(f"[OSC] 盘前预筛失败（不阻断主循环）: {_e}")
    elif (live and t >= dtime(9, 31) and _STATE.get("prepare_via") == "init"
          and not _STATE.get("prepare_skip_logged")):
        _STATE["prepare_skip_logged"] = True
        rt.log("[OSC] 09:31 预筛钩子：init 阶段已成功（候选已就绪）⇒ 跳过兜底重跑")
    # 09:31 预挂目标限价 S0 探测（只落盘计划，不下单）
    if live and t >= dtime(9, 31) and not _STATE["probe_plan"] and preplace_probe_enabled(rt):
        _STATE["probe_plan"] = True
        try:
            probe_preplace_plan(rt, now, today)
        except Exception as _e:
            rt.log(f"[OSC] 预挂探测(计划)失败（不阻断主循环）: {_e}")
    # 10:00 早盘冲高卖（**先于买侧**：先回笼现金再买）
    # P1-7（2026-10-26）：每日一次标记改为「成功才置位」——原前置位写法令
    # 钩子一抛异常就当日上午永久缺席。抛异常 ⇒ 不置位 + 计重试，下一根 bar 再试；
    # 重试上限/最晚时限（默认 10:30）耗尽 ⇒ 置位放弃 + risk 告警。首试不受时限限制。
    if (live and sell_enabled(rt) and t >= morning_sell_time(rt)
            and not _STATE["morning"]):
        _mr = int(_STATE.get("morning_retries") or 0)
        if _mr > 0 and (_mr >= MORNING_SELL_RETRY_MAX
                        or t > morning_sell_retry_deadline(rt)):
            _STATE["morning"] = True
            rt.log(f"[OSC] 10:00 早盘冲高卖：已重试 {_mr} 次仍失败 / 已过重试时限 ⇒ 今日放弃"
                   f"（D+1 持仓交 14:50 卖侧兜底）")
            try:
                rt.risk(str(now), "osc_morning_sell_giveup",
                        f"早盘冲高卖重试 {_mr} 次仍失败，今日放弃该钩子", code="")
            except Exception:
                pass
        else:
            try:
                try_morning_sell(rt, context, now, today)
                _STATE["morning"] = True
            except Exception as _e:
                _STATE["morning_retries"] = _mr + 1
                rt.log(f"[OSC] 10:00 早盘冲高卖失败（第 {_mr + 1} 次，下一根 bar 重试，"
                       f"不阻断主循环）: {_e}")
    # 10:00 买侧
    # ⚠️ `buy_done` 是**落盘**标记：进程在 10:00 之后重启时 `_STATE["buy"]` 已被 `reset_day`
    #    清掉，下一根 bar 会**再跑一次首轮**（可能重复下单）。回读当日状态后据此跳过。
    # P1-7 审视结论（2026-10-26）：买侧**刻意保留前置位**，不做失败重试——try_buy 内部
    #    部分下单后才抛异常的场景下，重试 = 重复下单（卖侧重试是减仓、幂等方向安全；
    #    买侧重试是加仓、方向不安全）。候选/取数失败本就是 fail-closed「当日不下单」设计。
    if live and t >= dtime(10, 0) and not _STATE["buy"]:
        load_day_state(rt, today)
        if _STATE.get("buy_done"):
            _STATE["buy"] = True
            rt.log("[OSC] 10:00 买侧：当日状态显示首轮已执行（重启回读）⇒ 跳过，不重复下单")
        else:
            _STATE["buy"] = True
            try:
                try_buy(rt, context, now)
            except Exception as _e:
                rt.log(f"[OSC] 10:00 买侧失败（不阻断主循环）: {_e}")
    # 10:00 预挂目标限价 S0 探测完成（**排在卖侧/买侧之后**：交易时效优先；只读行情）
    if (live and preplace_probe_enabled(rt) and t >= morning_sell_time(rt)
            and not _STATE["probe_final"]):
        _STATE["probe_final"] = True
        try:
            probe_preplace_final(rt, context, now, today)
        except Exception as _e:
            rt.log(f"[OSC] 预挂探测(完成)失败（不阻断主循环）: {_e}")
    # 10:08 / 10:14 补单轮（买不进替补，2026-10-14）—— 这两格是"每日一次 + 时间窗"
    for _i, _bt in enumerate(backfill_times(rt)):
        if (live and _STATE.get("buy") and t < backfill_deadline(rt)
                and t >= _bt and not (_STATE["backfill"] or {}).get(_i)):
            _STATE.setdefault("backfill", {})[_i] = True
            try:
                try_backfill(rt, context, now, today, _i)
            except Exception as _e:
                rt.log(f"[OSC] 补单第{_i + 1}轮失败（不阻断主循环）: {_e}")
            try:
                save_day_state(rt, today)
            except Exception:
                pass
    # 14:50 止损/到期
    # P1-7（2026-10-26）：每日一次标记改为「成功才置位」——卖侧是风控腿，前置位写法
    # 令 try_sell 一抛异常就当日永久缺席（与历史「14:55 尾盘归位未执行」事故同型）。
    # 抛异常 ⇒ 不置位 + 计重试，下一根 bar 再试；重试上限（SELL_RETRY_MAX）/
    # 最晚时限（默认 14:58）耗尽 ⇒ 置位放弃 + risk 告警。首试不受时限限制
    # （14:59 才启动的进程也该卖一次）。
    if (live and sell_enabled(rt) and t >= sell_time(rt) and not _STATE["sell"]):
        _sr = int(_STATE.get("sell_retries") or 0)
        if _sr > 0 and (_sr >= SELL_RETRY_MAX or t > sell_retry_deadline(rt)):
            _STATE["sell"] = True
            rt.log(f"[OSC] 14:50 卖侧：已重试 {_sr} 次仍失败 / 已过 {_hhmm_str(sell_retry_deadline(rt))} "
                   f"重试时限 ⇒ 今日放弃（持仓未平，需人工介入）")
            try:
                rt.risk(str(now), "osc_sell_giveup",
                        f"14:50 卖侧重试 {_sr} 次仍失败，今日放弃：止损/到期未执行，持仓过夜",
                        code="")
            except Exception:
                pass
        else:
            try:
                try_sell(rt, context, now, today)
                _STATE["sell"] = True
            except Exception as _e:
                _STATE["sell_retries"] = _sr + 1
                rt.log(f"[OSC] 14:50 卖侧失败（第 {_sr + 1} 次，下一根 bar 重试，"
                       f"不阻断主循环）: {_e}")

    # ── 周期心跳（每 30 分钟一格）────────────────────────────────────────────
    # 为什么要有：四个钩子都是"每日一次"，盘前 09:31 到 10:00、10:00 到 14:50 之间**一行日志都没有**
    # ⇒ owner 2026-10-14 反馈「log 更新太少，不知道它具体在干什么」。这里每半小时报一行"我还活着 +
    # 手里有什么 + 今天做完哪些动作"，把信息真空填掉，且**不碰任何判定**。
    # 60s 的 bar 循环里靠**槽位去重**（不然每个 bar 打一行、240 行/天）。
    if live and t >= dtime(9, 31):
        _slot = "%02d:%s" % (now.hour, "00" if now.minute < 30 else "30")
        if _STATE.get("hb_slot") != _slot:
            _STATE["hb_slot"] = _slot
            try:
                _done = [k for k in ("prepare", "morning", "buy", "backfill", "sell")
                         if _STATE.get(k)]
                _t = {"prepare": "预筛", "morning": "早盘卖", "buy": "买侧",
                      "backfill": "补单", "sell": "收盘卖"}
                rt.log(f"[OSC] {_slot} 心跳 · 持仓 {len(sell_codes(rt))} 只 · "
                       f"今日已完成 {'/'.join(_t[k] for k in _done) if _done else '无'}")
            except Exception:
                pass
