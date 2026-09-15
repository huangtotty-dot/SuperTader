# -*- coding: utf-8 -*-
"""test_b7_shadow_glue.py — B7 影子通道接线纯逻辑测试（接入专员_B7 · 2026-09-15 施工4/4）

gm_main 依赖 GM SDK 不可直接 import → 影子判定/结算中可抽离的纯逻辑已抽至
execution/auto/b7_shadow_glue.py，本套件用合成数据直测：
1.  guard_decision：tail30 为 None / ≤1% → none（什么都不记，不是 skip）
2.  guard_decision：五类守卫各自产生对应 skip reason
3.  guard_decision：skip reason 固定优先级（多守卫同中时取高优先级）
4.  guard_decision：全过 → go
5.  qty_gate：虚拟量 <100 → qty_below_100
6.  armed_chain_id：同票 armed 链检测（settled/voided 忽略、他票隔离）
7.  find_due_chains：当日链不结算 / 次日到期结算 / 终态链忽略 / 他票隔离
8.  sell_entry_from_chain + B7ShadowLedger 结算配对 round-trip（chain_id 一致、净收益/win 正确）
9.  与 overnight_reverse_t 全链路集成：detect_signal → compute_virtual_qty → 台账 → 结算
10. 熔断器 from_dict(engine.b7_circuit 口径) 恢复 + record 经 on_change 回写

运行：python t_io/validation/b7_shadow/test_b7_shadow_glue.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution" / "auto"))

import overnight_reverse_t as ort  # noqa: E402
import b7_shadow_glue as glue      # noqa: E402

fails = []
n_checks = [0]


def check(name, cond):
    n_checks[0] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        fails.append(name)


def _chain(cid, code, sell_date, qty=500, sell_px=10.15, status="armed"):
    return {"chain_id": cid, "code": code, "qty": qty, "sell_px": sell_px,
            "sell_date": sell_date, "due_buy_date": "", "status": status,
            "void_reason": ""}


print("== 1. guard_decision：tail30 未过闸 → none（不留痕） ==")
_base = dict(breaker_tripped=False, has_awaiting_buyback=False,
             pos_qty=1000, base_ref=1000, protect_sold_today=False, in_b7_chain=False)
check("tail30=None → none", glue.guard_decision(tail30=None, **_base) == ("none", ""))
check("tail30=+0.5% → none", glue.guard_decision(tail30=0.005, **_base) == ("none", ""))
check("tail30=恰好+1% → none（严格大于）",
      glue.guard_decision(tail30=0.01, **_base) == ("none", ""))

print("== 2. guard_decision：五类守卫各自 skip reason ==")
check("熔断 → breaker_tripped",
      glue.guard_decision(tail30=0.015, **{**_base, "breaker_tripped": True})
      == ("skip", "breaker_tripped"))
check("有回补义务 → has_awaiting_buyback",
      glue.guard_decision(tail30=0.015, **{**_base, "has_awaiting_buyback": True})
      == ("skip", "has_awaiting_buyback"))
check("归位未完成(pos<base) → pos_below_base",
      glue.guard_decision(tail30=0.015, **{**_base, "pos_qty": 800})
      == ("skip", "pos_below_base"))
check("base_ref=0 → pos_below_base",
      glue.guard_decision(tail30=0.015, **{**_base, "base_ref": 0})
      == ("skip", "pos_below_base"))
check("当日保护类卖出 → protect_sell_today",
      glue.guard_decision(tail30=0.015, **{**_base, "protect_sold_today": True})
      == ("skip", "protect_sell_today"))
check("已有 B7 链 → in_b7_chain",
      glue.guard_decision(tail30=0.015, **{**_base, "in_b7_chain": True})
      == ("skip", "in_b7_chain"))

print("== 3. guard_decision：skip reason 固定优先级 ==")
check("熔断+回补义务 → breaker_tripped 优先",
      glue.guard_decision(tail30=0.015, **{**_base, "breaker_tripped": True,
                                           "has_awaiting_buyback": True})
      == ("skip", "breaker_tripped"))
check("回补义务+归位未完成 → has_awaiting_buyback 优先",
      glue.guard_decision(tail30=0.015, **{**_base, "has_awaiting_buyback": True,
                                           "pos_qty": 800})
      == ("skip", "has_awaiting_buyback"))
check("归位未完成+保护卖出 → pos_below_base 优先",
      glue.guard_decision(tail30=0.015, **{**_base, "pos_qty": 800,
                                           "protect_sold_today": True})
      == ("skip", "pos_below_base"))
check("保护卖出+已有链 → protect_sell_today 优先",
      glue.guard_decision(tail30=0.015, **{**_base, "protect_sold_today": True,
                                           "in_b7_chain": True})
      == ("skip", "protect_sell_today"))

print("== 4. guard_decision：全过 → go ==")
check("tail30=+1.5% 全过 → go",
      glue.guard_decision(tail30=0.015, **_base) == ("go", ""))
check("超仓(pos>base)归位完成 → go",
      glue.guard_decision(tail30=0.015, **{**_base, "pos_qty": 1200}) == ("go", ""))

print("== 5. qty_gate：虚拟量闸 ==")
check("500 股 → go", glue.qty_gate(500) == ("go", ""))
check("99 股 → qty_below_100", glue.qty_gate(99) == ("skip", "qty_below_100"))
check("0 股 → qty_below_100", glue.qty_gate(0) == ("skip", "qty_below_100"))

print("== 6. armed_chain_id：同票 armed 链检测 ==")
_chains = {
    "600000_2026-09-15": _chain("600000_2026-09-15", "600000", "2026-09-15"),
    "600001_2026-09-14": _chain("600001_2026-09-14", "600001", "2026-09-14",
                                status="settled"),
    "600002_2026-09-14": _chain("600002_2026-09-14", "600002", "2026-09-14",
                                status="voided"),
}
check("命中 armed 链 → chain_id",
      glue.armed_chain_id(_chains, "600000") == "600000_2026-09-15")
check("settled 终态忽略 → None", glue.armed_chain_id(_chains, "600001") is None)
check("voided 终态忽略 → None", glue.armed_chain_id(_chains, "600002") is None)
check("他票隔离 → None", glue.armed_chain_id(_chains, "600003") is None)
check("空表 → None", glue.armed_chain_id({}, "600000") is None)
check("None 输入容错 → None", glue.armed_chain_id(None, "600000") is None)

print("== 7. find_due_chains：结算配对（bar 日期 > sell_date） ==")
_due_chains = {
    "600000_2026-09-15": _chain("600000_2026-09-15", "600000", "2026-09-15"),
    "600000_2026-09-14": _chain("600000_2026-09-14", "600000", "2026-09-14"),
    "600000_2026-09-13": _chain("600000_2026-09-13", "600000", "2026-09-13",
                                status="settled"),
    "600001_2026-09-14": _chain("600001_2026-09-14", "600001", "2026-09-14"),
}
check("当日 armed 链不结算（14:55 卖出当日）",
      glue.find_due_chains({"600000_2026-09-15": _chains["600000_2026-09-15"]},
                           "600000", "2026-09-15") == [])
_due = glue.find_due_chains(_due_chains, "600000", "2026-09-15")
check("次日到期：昨日 armed 链结算、当日链不结算、settled 忽略",
      [cid for cid, _ in _due] == ["600000_2026-09-14"])
check("他票隔离", glue.find_due_chains(_due_chains, "600009", "2026-09-15") == [])
check("跨周末：周五卖 → 周一首根 bar 结算",
      [cid for cid, _ in glue.find_due_chains(
          {"600000_2026-09-11": _chain("600000_2026-09-11", "600000", "2026-09-11")},
          "600000", "2026-09-14")] == ["600000_2026-09-11"])

print("== 8. sell_entry_from_chain + B7ShadowLedger 结算配对 round-trip ==")
with tempfile.TemporaryDirectory() as td:
    led = ort.B7ShadowLedger(td)
    now = datetime(2026, 9, 15, 14, 55)      # 时间注入，不读系统时钟
    bars = [{"time": "2026-09-15 14:30", "close": 10.00},
            {"time": "2026-09-15 14:55", "close": 10.15}]
    sig = ort.detect_signal("600000", "测试票", bars, pos_qty=1000, base_ref=1000, now=now)
    check("信号触发（集成前置）", sig is not None)
    cid = ort.make_chain_id("600000", "2026-09-15")
    vqty = ort.compute_virtual_qty(1000, 1000)
    led.record_signal(sig, pos_qty=1000, virtual_qty=vqty, chain_id=cid)
    sell_entry = led.record_virtual_sell(sig, vqty, chain_id=cid, sell_date="2026-09-15")
    # 模拟引擎侧挂账 → 次日从 armed 链还原 sell_entry 结算（glue 配对逻辑）
    armed = _chain(cid, "600000", "2026-09-15", qty=vqty, sell_px=sig["price"])
    entry2 = glue.sell_entry_from_chain(armed)
    check("配对 sell_entry 与原 sell 一致（code/qty/sell_px/chain_id）",
          entry2["code"] == sell_entry["code"] and entry2["qty"] == sell_entry["qty"]
          and abs(entry2["sell_px"] - sell_entry["sell_px"]) < 1e-9
          and entry2["chain_id"] == sell_entry["chain_id"])
    settle = led.record_virtual_buyback(entry2, "2026-09-16", open_px=9.95,
                                        prev_close=entry2["sell_px"], chain_id=cid)
    check("结算 chain_id 配对一致", settle["chain_id"] == cid)
    check("低开接回 win=True 且 net>0", settle["win"] is True and settle["net_pct"] > 0)
    check("buy/sell_date 落事件",
          settle["buy_date"] == "2026-09-16" and settle["sell_date"] == "2026-09-15")
    with open(led._path("2026-09-16"), encoding="utf-8") as f:
        day16 = [json.loads(x) for x in f.readlines()]
    check("接回事件落盘 1 条且带 chain_id",
          len(day16) == 1 and day16[0].get("chain_id") == cid)

print("== 9. 全链路集成：detect_signal → 守卫链 → 虚拟量 → 台账 → 次日结算 ==")
with tempfile.TemporaryDirectory() as td:
    led = ort.B7ShadowLedger(td)
    day_bars = [{"time": "2026-09-15 09:30", "close": 10.00},
                {"time": "2026-09-15 14:30", "close": 10.00},
                {"time": "2026-09-15 14:55", "close": 10.20}]   # tail30=+2%
    tail30 = ort.compute_tail30_pct(day_bars)
    check("tail30=+2%", tail30 is not None and abs(tail30 - 0.02) < 1e-9)
    dec, reason = glue.guard_decision(tail30=tail30, **_base)
    check("守卫链 go", (dec, reason) == ("go", ""))
    sig = ort.detect_signal("600000", "测试票", day_bars, pos_qty=1000,
                            base_ref=1000, now=now)
    vqty = ort.compute_virtual_qty(1000, 1000)
    check("虚拟量=500（base_ref 50% 口径）", vqty == 500)
    check("qty_gate go", glue.qty_gate(vqty) == ("go", ""))
    cid = ort.make_chain_id("600000", "2026-09-15")
    led.record_signal(sig, 1000, vqty, chain_id=cid)
    led.record_virtual_sell(sig, vqty, chain_id=cid, sell_date="2026-09-15")
    # 次日：armed 链 → find_due → sell_entry_from_chain → record_virtual_buyback
    armed_map = {cid: _chain(cid, "600000", "2026-09-15", qty=vqty, sell_px=sig["price"])}
    due = glue.find_due_chains(armed_map, "600000", "2026-09-16")
    check("次日首根 bar 找到到期链", len(due) == 1 and due[0][0] == cid)
    net_expect = ort.virtual_net_pct(sig["price"], 10.35)
    settle = led.record_virtual_buyback(glue.sell_entry_from_chain(due[0][1]),
                                        "2026-09-16", open_px=10.35,
                                        prev_close=sig["price"], chain_id=cid)
    check("结算净收益与 virtual_net_pct 一致",
          abs(settle["net_pct"] - round(net_expect * 100, 4)) < 1e-6)
    check("高开接回 win=False（卖飞：10.35 > 卖价10.20）", settle["win"] is False)

print("== 10. 熔断器：engine.b7_circuit 口径恢复 + on_change 回写 ==")
# 模拟引擎侧持久化态（t_engine_auto.b7_circuit：无 n 字段）
eng_state = {"consecutive_losses": 3, "tripped": False}
captured = []
cb = ort.B7CircuitBreaker.from_dict(eng_state, on_change=captured.append)
check("from_dict 恢复连亏计数", cb.consecutive_losses == 3 and not cb.tripped)
check("恢复后守卫链熔断未触发 → 不拦截",
      glue.guard_decision(tail30=0.015, **{**_base, "breaker_tripped": cb.tripped})
      == ("go", ""))
tripped = cb.record(-0.005)          # 第 4 笔连亏 → 熔断
check("第 4 笔连亏熔断", tripped is True and cb.tripped)
check("on_change 回写收到 to_dict 口径",
      len(captured) == 1 and captured[0]["consecutive_losses"] == 4
      and captured[0]["tripped"] is True)
check("熔断后守卫链拦截 → breaker_tripped",
      glue.guard_decision(tail30=0.015, **{**_base, "breaker_tripped": cb.tripped})
      == ("skip", "breaker_tripped"))
cb.reset()
check("手动 reset 后恢复 + on_change 回写",
      not cb.tripped and cb.consecutive_losses == 0
      and captured[-1]["tripped"] is False)

print(f"\n测试结果: {n_checks[0]} 项断言，"
      f"{'全部通过' if not fails else '失败 ' + str(fails)}")
sys.exit(0 if not fails else 1)
