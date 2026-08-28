# coding=utf-8
"""
tests/test_wp_b19.py — WP-B19-rev(2026-08-28): -8% 硬止损通道 + O-12 pos_key容差 + O-13 推送节流 验证

背景：
  0819 原决策"跌破五日均线应该直接离场"已替换为 0828 决策：-8% 硬止损（HARD_STOP_EXIT）
  全离 + 当日禁买。依据 exit_strategy_mining_20260828 报告，硬止损在日线 close 模拟下
  显著优于 MA5 静态破位（mean_ret +5.6% vs +2.7%，max_mae -18.8% vs -18.6%）。
  同包 O-12 pos_key 成本容差、O-13 watcher 同票同 kind 风控推送节流。

验证范围（8 场景）：
  T1  亏损 -10%（<-8%）持仓 500 全可用 → HARD_STOP_EXIT 500 全离，信号在 TRAIL 之前生成
  T2  硬止损触发日 BUY_LOW / BASE 买入意图 → 全部 hard_stop_block 拦截留痕
  T3  部分仓位 T+1 锁定 → 卖可用部分；次日仍触发硬止损 → 续卖剩余
  T4  次日价格回升（未触发硬止损）→ 买入禁令自动解除；无新 HARD_STOP_EXIT
  T5  同日 HARD_STOP_EXIT + TRAIL 触发 → 仅 HARD_STOP_EXIT 成交（优先级 P0），无 TRAIL_SELL
  T6  HARD_STOP_EXIT 成交后 → 无 buyback_armed 事件（f）
  T7  O-12: pos_key 成本差 +0.002 / +0.04（容差内）→ 恢复+指纹更新；+8% → 作废留痕
  T8  O-13: 同票同 kind 30min 内 3 次 entry_gate → 仅 1 条推送（事件流照写）

运行（逐文件运行，不要用 unittest discover——本项目双模块 import 会假失败）:
  C:/Users/Lenovo/AppData/Local/Programs/Python/Python311/python.exe tests/test_wp_b19.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pandas as pd

_ST = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
_AUTO = os.path.join(_ST, "execution", "auto")
for _p in (_ST, _AUTO, os.path.join(_AUTO, "_gm")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import gm_bridge.writer as writer
TMP = tempfile.mkdtemp(prefix="gmwpb19_test_")
writer.BRIDGE_DIR = TMP

import signals.engine as se  # noqa: E402
from signals.engine import SignalEngine  # noqa: E402
from data.indicators import Signal  # noqa: E402
import gm_main as main  # noqa: E402
import sell_state, sell_channels
import gm_bridge.watcher as watcher  # noqa: E402

main._AUDIT_LOG_PATH = os.path.join(TMP, "backtrace_b19.jsonl")
main._audit_file = None
sell_state.SELL_STATE_PATH = os.path.join(TMP, "sell_state_b19.json")
watcher.BRIDGE_DIR = TMP

CODE = "603667"
GM_SYM = main.STOCKS[CODE]
MA5 = 56.0

TODAY = datetime.now().strftime("%Y%m%d")
EVENTS_PATH = os.path.join(TMP, f"events_{TODAY}.jsonl")

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {detail}")


def make_df(price, t, n=30, vwap=None):
    vwap = vwap if vwap is not None else price
    end = pd.Timestamp(t)
    rows = []
    for i in range(n):
        ts = end - timedelta(minutes=n - 1 - i)
        c = price if i == n - 1 else price * 0.999
        rows.append({
            "time": ts, "open": c, "high": c + 0.03, "low": c - 0.03,
            "close": c, "volume": 10000, "amount": c * 10000,
            "vwap": vwap, "rsi": 50.0, "bb_pct": 0.5,
            "macd_hist": 0.0, "ema_spread": 0.0, "range_pos": 0.5,
            "vol_ratio": 1.0, "mom5": 0.0, "lower_shadow": 0.0,
            "upper_shadow": 0.0, "day_amplitude": 0.02,
            "date": str(ts.date()), "prev_high": price * 1.02,
        })
    return pd.DataFrame(rows)


DAILY_CTX = {"index_regime": "range", "intraday_alerts": [], "daily_atr": 0.02,
             "daily_ma5": MA5, "_m2_pool_pass": True}


def read_events():
    if not os.path.exists(EVENTS_PATH):
        return []
    with open(EVENTS_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def read_audit():
    if not os.path.exists(main._AUDIT_LOG_PATH):
        return []
    with open(main._AUDIT_LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def rm_state():
    if os.path.exists(sell_state.SELL_STATE_PATH):
        os.remove(sell_state.SELL_STATE_PATH)


def clear_events():
    if os.path.exists(EVENTS_PATH):
        os.remove(EVENTS_PATH)


def make_ctx(now_str, qty=800, cost=50.0, base_ref=None, trend="TREND_RANGE"):
    eng = SignalEngine()
    eng._last_feats[CODE] = {"profit_pct": 0.0}
    eng.evaluate = lambda *a, **k: (0.0, 0.0, None)
    dt = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    ctx = SimpleNamespace(
        now=dt,
        cur_date=None,
        daily_buy_count={}, daily_sell_count={}, daily_trade_price={},
        engine=eng,
        _last_ir_date=dt.date(),
        mode=None,
        _last_bar_eob={},
        bar_cache={},
        manual_position={GM_SYM: {"name": "五洲新春", "qty": qty, "available": qty,
                                  "t_qty": qty, "cost": cost,
                                  "type": "stock", "pre_close": cost}},
        executed_orders={},
        _base_ordered=set(),
        _base_settled={CODE},
        _inflight_sell={},
        sizer=SimpleNamespace(calc_sell_qty=lambda *a, **k: 200,
                              calc_buy_qty=lambda *a, **k: 100),
        latest_pre_close={CODE: cost},
        total_trade_count=0,
        rejected_order_count=0,
        audit_records=[],
        last_index_regime="range",
        last_index_score=0.0,
        _daily_ctx_cache_map={},
        _pending_sell_action={},
        _day_open={},
    )
    ctx._trend_override = trend
    if base_ref is not None:
        setattr(ctx, f"_base_ref_{CODE}", base_ref)
    return ctx


def drive_bar(ctx, now_str, close, trend=None, ma5=MA5):
    ctx.now = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
    df = make_df(close, now_str)
    if trend is None:
        trend = getattr(ctx, "_trend_override", "TREND_RANGE")
    calls = []
    with mock.patch.object(main, "_build_bar_df", return_value=df), \
         mock.patch.object(main, "_refresh_daily_ctx",
                           return_value=dict(DAILY_CTX, _stock_trend_state=trend, daily_ma5=ma5)), \
         mock.patch.object(main, "_get_holding",
                           side_effect=lambda c, cd, s: dict(c.manual_position[s])), \
         mock.patch.object(main, "_base_topup_qty", return_value=0), \
         mock.patch.object(main.ops_guard, "ensure_watcher", return_value=None), \
         mock.patch.object(main, "check_kill_switch", return_value=False), \
         mock.patch.object(main, "order_volume", side_effect=lambda **kw: calls.append(kw)):
        main.on_bar(ctx, [{"symbol": GM_SYM, "eob": now_str + ":00",
                           "open": close, "high": close + 0.03, "low": close - 0.03,
                           "close": close, "volume": 10000, "amount": close * 10000}])
    return calls


def sell_calls(calls):
    return [c for c in calls if c.get("side") == main.OrderSide_Sell]


def buy_low_sig(price, score=70.0):
    return Signal(code=CODE, name="五洲新春", action="BUY_LOW",
                  price=price, score=score, reasons=["b19-test"])


def live_ctx(qty, cost, now):
    eng = SignalEngine()
    return SimpleNamespace(
        mode=main.MODE_LIVE,
        now=now,
        engine=eng,
        manual_position={GM_SYM: {"name": "五洲新春", "qty": qty, "available": qty,
                                  "t_qty": qty, "cost": cost,
                                  "type": "stock", "pre_close": cost}},
    )


# ══ T1: 亏损 -10%（<-8% 硬止损）→ HARD_STOP_EXIT 全离 500（TRAIL 之前生成） ══
clear_events()
c1 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
calls1 = drive_bar(c1, "2026-08-19 09:36:00", 45.0, ma5=MA5)   # cp 45 < cost 50 × 0.92
s1 = sell_calls(calls1)
sig1 = [e for e in read_events() if e.get("event") == "signal" and e.get("action") == "HARD_STOP_EXIT"]
check("T1a HARD_STOP_EXIT 全离 500 股（非 sizer 定量）",
      len(s1) == 1 and s1[0]["volume"] == 500, f"sells={[(c.get('volume')) for c in s1]}")
check("T1b 信号事件 action=HARD_STOP_EXIT 且无 TRAIL_SELL（优先级 P0）",
      len(sig1) == 1 and not [e for e in read_events()
                              if e.get("event") == "signal" and e.get("action") == "TRAIL_SELL"],
      f"sig={sig1 and sig1[0].get('action')}")
check("T1c 硬止损触发日标记 _hard_stop_today",
      getattr(c1, "_hard_stop_today", {}).get(CODE) == "2026-08-19",
      f"hard_stop={getattr(c1, '_hard_stop_today', {}).get(CODE)}")

# ══ T2: 硬止损触发日禁一切买入（BUY_LOW 被 HARD_STOP_EXIT 覆盖 / BASE 拦截留痕） ══
clear_events()
c2 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
c2.engine.evaluate = lambda *a, **k: (70.0, 0.0, buy_low_sig(45.0))
calls2 = drive_bar(c2, "2026-08-19 09:36:00", 45.0, ma5=MA5)
buys2 = [c for c in calls2 if c.get("side") == main.OrderSide_Buy]
sig2 = [e for e in read_events() if e.get("event") == "signal"]
check("T2a 硬止损触发日 BUY_LOW 无买入成交（HARD_STOP_EXIT 优先级 P0 覆盖禁买）",
      len(buys2) == 0, f"buys={len(buys2)}")
check("T2b 硬止损触发日信号为 HARD_STOP_EXIT（非 BUY_LOW）",
      any(e.get("action") == "HARD_STOP_EXIT" for e in sig2)
      and not any(e.get("action") == "BUY_LOW" for e in sig2),
      f"actions={[e.get('action') for e in sig2]}")

clear_events()
c2b = make_ctx("2026-08-19 09:36:00", qty=0, cost=50.0, base_ref=500)
c2b._base_settled = set()   # 未建仓 → 走 BASE 路径
c2b._hard_stop_today = {CODE: "2026-08-19"}  # 模拟当日已触发硬止损
calls2b = drive_bar(c2b, "2026-08-19 09:36:00", 45.0, ma5=MA5)
check("T2c 硬止损触发日 BASE 建仓被拦", len(calls2b) == 0, f"orders={len(calls2b)}")
risk2b = [e for e in read_events() if e.get("kind") == "hard_stop_block"]
check("T2d BASE 侧 hard_stop_block 留痕", len(risk2b) >= 1, f"risk={len(risk2b)}")

# ══ T3: T+1 锁定——卖可用部分，次日仍触发硬止损续卖剩余 ══
clear_events()
c3 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
c3.manual_position[GM_SYM]["available"] = 200   # 当日买 300 锁定
calls3 = drive_bar(c3, "2026-08-19 09:36:00", 45.0, ma5=MA5)
s3 = sell_calls(calls3)
check("T3a 硬止损日卖可用 200（T+1 锁定部分不卖）",
      len(s3) == 1 and s3[0]["volume"] == 200, f"sells={[c.get('volume') for c in s3]}")
# 次日仍满足硬止损：先模拟 T3a 的 200 卖出成交（释放 F9 在途额度），pos=300 全可用 → 续卖 300
main.on_order_status(c3, {"symbol": GM_SYM, "status": 3, "volume": 200,
                          "side": 2, "price": 45.0, "filled_vwap": 45.0})
c3.manual_position[GM_SYM]["qty"] = 300
c3.manual_position[GM_SYM]["available"] = 300
c3.manual_position[GM_SYM]["t_qty"] = 300
calls3b = drive_bar(c3, "2026-08-20 09:36:00", 45.0, ma5=MA5)
s3b = sell_calls(calls3b)
check("T3b 次日仍触发硬止损续卖剩余 300",
      len(s3b) == 1 and s3b[0]["volume"] == 300, f"sells={[c.get('volume') for c in s3b]}")

# ══ T4: 次日价格回升（未触发硬止损）→ 禁解除，无新 HARD_STOP_EXIT ══
clear_events()
c4 = make_ctx("2026-08-20 09:36:00", qty=300, cost=50.0, base_ref=500)
calls4 = drive_bar(c4, "2026-08-20 09:36:00", 52.0, ma5=MA5)   # profit=+4%，不触发 -8%
check("T4a 未触发硬止损 → 无 HARD_STOP_EXIT/卖出", len(sell_calls(calls4)) == 0, f"sells={len(calls4)}")
c4.engine.evaluate = lambda *a, **k: (70.0, 0.0, buy_low_sig(52.0))
calls4b = drive_bar(c4, "2026-08-20 09:37:00", 52.0, ma5=MA5)
sig4 = [e for e in read_events() if e.get("event") == "signal" and e.get("action") == "BUY_LOW"]
risk4 = [e for e in read_events() if e.get("kind") == "hard_stop_block"]
check("T4b 未触发日 BUY_LOW 信号照写（禁解除）", len(sig4) == 1, f"sig={len(sig4)}")
check("T4c 未触发日无新增 hard_stop_block 拦截", len(risk4) == 0, f"risk={len(risk4)}")

# ══ T5: 同日 HARD_STOP_EXIT + TRAIL 回撤 → 仅 HARD_STOP_EXIT（优先级 P0） ══
clear_events()
c5 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
c5.manual_position[GM_SYM]["_trail_state"] = "ARMED"
c5.manual_position[GM_SYM]["_trail_peak"] = 49.0   # 从 49 峰值回撤到 45 ≈ 8.2%，TRAIL 本应触发
calls5 = drive_bar(c5, "2026-08-19 09:36:00", 45.0, ma5=MA5)   # profit=-10% 同时触发硬止损
check("T5a 仅 HARD_STOP_EXIT 成交一次（全离 500）",
      len(sell_calls(calls5)) == 1 and sell_calls(calls5)[0]["volume"] == 500,
      f"sells={[c.get('volume') for c in sell_calls(calls5)]}")
sig5 = [e for e in read_events() if e.get("event") == "signal" and e.get("action") == "TRAIL_SELL"]
check("T5b 无 TRAIL_SELL 信号（硬止损优先级最高）", len(sig5) == 0, f"trail_sig={len(sig5)}")

# ══ T6: HARD_STOP_EXIT 成交不生成 buyback（f） ══
clear_events()
c6 = make_ctx("2026-08-19 09:36:00", qty=500, cost=50.0, base_ref=500)
drive_bar(c6, "2026-08-19 09:36:00", 45.0, ma5=MA5)   # HARD_STOP_EXIT 500 卖 → pending_sell_action
main.on_order_status(c6, {"symbol": GM_SYM, "status": 3, "volume": 500,
                          "side": 2, "price": 45.0, "filled_vwap": 45.0})
arm6 = [e for e in read_events() if e.get("event") == "buyback_armed"]
check("T6a HARD_STOP_EXIT 成交后无 buyback_armed 事件", len(arm6) == 0, f"armed={len(arm6)}")
check("T6b 不生成回补记忆（awaiting_buyback 无）", CODE not in c6.engine.awaiting_buyback)

# ══ T7: O-12 pos_key 成本容差匹配 ══
# 用真实墙钟作为模拟日期：arm 的 expire_date = 卖出日+3交易日，而 _sell_state_restore
# 用 datetime.now() 判过期。若固定模拟旧日期（如 2026-08-19），随真实日期推移会
# 落入"已过期"分支导致恢复被作废（date rot），故卖出时间一律取当下。
_SELL_NOW = datetime.now()
se.SIM_NOW = _SELL_NOW   # 先于 arm：expire_date 按真实当下 +3 交易日计算
rm_state(); clear_events()
c_p = live_ctx(800, 50.0, _SELL_NOW)
c_p.engine.awaiting_buyback[CODE] = c_p.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_p, CODE, GM_SYM)
c_r = live_ctx(800, 50.002, _SELL_NOW)   # +0.002 容差内
main._sell_state_restore(c_r)
st7 = main._sell_state_load().get(CODE, {})
check("T7a +0.002 容差内 → 恢复 + 指纹静默更新",
      CODE in c_r.engine.awaiting_buyback and st7.get("pos_key") == "800@50.0020",
      f"ab={CODE in c_r.engine.awaiting_buyback} pos_key={st7.get('pos_key')}")
check("T7b pos_key_tolerance 留痕",
      any(a.get("event") == "pos_key_tolerance" for a in read_audit()))

# +0.04 容差内
rm_state()
c_p2 = live_ctx(800, 50.0, _SELL_NOW)
c_p2.engine.awaiting_buyback[CODE] = c_p2.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_p2, CODE, GM_SYM)
c_r2 = live_ctx(800, 50.04, _SELL_NOW)   # +0.04 容差内(max 0.05)
main._sell_state_restore(c_r2)
check("T7c +0.04 容差内 → 恢复", CODE in c_r2.engine.awaiting_buyback)

# +8% 超容差 → 作废
rm_state()
c_p3 = live_ctx(800, 50.0, _SELL_NOW)
c_p3.engine.awaiting_buyback[CODE] = c_p3.engine.arm_awaiting_buyback(CODE, 52.14, 300, "SELL_HIGH")
main._sell_state_persist(c_p3, CODE, GM_SYM)
c_r3 = live_ctx(800, 54.0, _SELL_NOW)   # +8% 超容差
main._sell_state_restore(c_r3)
check("T7d +8% 超容差 → 作废不恢复",
      CODE not in c_r3.engine.awaiting_buyback and CODE not in main._sell_state_load())

# ══ T8: O-13 watcher 同票同 kind 推送节流 ══
pushes = []
watcher._push = lambda title, content, level="info": pushes.append((title, level))
watcher._last_push.clear()
rec = {"event": "risk", "code": CODE, "kind": "entry_gate",
       "detail": f"cp=54 < ma5={MA5}", "time": "2026-08-19 09:36:00"}
for _ in range(3):
    watcher.handle_event(rec)
check("T8 同票同 kind 30min 内 3 次 → 仅 1 条推送", len(pushes) == 1, f"pushes={len(pushes)}")
# 30 分钟后可再推（键存时间戳）
watcher._last_push.clear()   # 模拟窗口过去
for _ in range(2):
    watcher.handle_event(rec)
check("T8b 节流窗口过后可再推", len(pushes) == 2, f"pushes={len(pushes)}")

# ── 汇总 ──
passed = sum(1 for _, ok, _ in results if ok)
print(f"\n===== {passed}/{len(results)} PASS =====")
sys.exit(0 if passed == len(results) else 1)
