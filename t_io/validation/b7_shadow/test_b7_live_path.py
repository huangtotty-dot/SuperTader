# -*- coding: utf-8 -*-
"""test_b7_live_path.py — B7 实单通道纯逻辑测试（B7实单施工 · 2026-09-15）

owner 2026-09-15 17:23 拍板：B7 尾盘反T跳过影子期直接上实单。
gm_main 依赖 GM SDK 不可直接 import → 实单路径的可抽离纯逻辑集中在
execution/auto/b7_shadow_glue.py（dispatch_mode / clamp_available_qty /
buyback_retry_step），本套件用合成数据 + 真实引擎（SignalEngine，经
SUPERTRADER_BUYBACK_STATE_PATH 重定向到临时目录，绝不碰生产 buyback_chains.json）直测：

1.  dispatch_mode：实单/影子互斥分派（live 严格优先，全关 → off）
2.  clamp_available_qty：T+1 可用量钳制（available=None 按 pos_qty / in-flight 扣减 /
    整百取整 / 不足 100 → no_available）
3.  buyback_retry_step：下单成功 → settle 清零；失败累进 retry；>30 次 → void
4.  实单卖出纯逻辑判定流串联：guard_decision → detect_signal → compute_virtual_qty
    → qty_gate → clamp_available_qty（含 no_available 进 SKIP_REASONS 全集）
5.  链生命周期（真实引擎持久化 API）：sell→armed→次日到期→buyback→settled，
    终态留痕 + 跨进程恢复 + void + 熔断状态落盘恢复
6.  连亏 4 笔费后为负熔断：breaker.record(virtual_net_pct<0)×4 → tripped，
    on_change 落盘 → 新实例恢复 tripped=True（拍板口径②跨进程持久化）

运行：python t_io/validation/b7_shadow/test_b7_live_path.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "execution" / "auto"))
sys.path.insert(0, str(REPO_ROOT / "execution" / "auto" / "_gm"))

# 引擎落盘重定向到临时目录（生产 buyback_chains.json 绝不触碰）——须在 import 引擎前设置
_TMP = tempfile.TemporaryDirectory(prefix="b7_live_test_")
os.environ["SUPERTRADER_BUYBACK_STATE_PATH"] = os.path.join(_TMP.name, "buyback_chains.json")

import overnight_reverse_t as ort   # noqa: E402
import b7_shadow_glue as glue       # noqa: E402
import t_engine_auto as tea         # noqa: E402

fails = []
n_checks = [0]


def check(name, cond):
    n_checks[0] += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        fails.append(name)


def _tail30_bars(base=10.0, gain=0.02):
    """合成当日分钟 bar：14:28~14:30 平走 base（c14:30 参照），14:50~14:55 拉 +gain
    （c14:55 信号价；tail30 = c14:55/c14:30 − 1 > 1% 触发口径）。"""
    bars = []
    for hm in ("14:28", "14:29", "14:30"):
        bars.append({"time": f"2026-09-16 {hm}:00", "open": base, "high": base,
                     "low": base, "close": base, "volume": 10000, "amount": base * 10000})
    px = base * (1 + gain)
    for i in range(6):
        bars.append({"time": f"2026-09-16 14:{50 + i}:00", "open": base, "high": px,
                     "low": base, "close": px, "volume": 10000, "amount": px * 10000})
    return bars


print("== 1. dispatch_mode：实单/影子互斥分派（live > shadow） ==")
check("live+shadow → live（实单严格优先）",
      glue.dispatch_mode(True, True) == "live")
check("仅 live → live", glue.dispatch_mode(True, False) == "live")
check("仅 shadow → shadow", glue.dispatch_mode(False, True) == "shadow")
check("全关 → off", glue.dispatch_mode(False, False) == "off")

print("== 2. clamp_available_qty：T+1 可用量钳制 ==")
check("available=None 按 pos_qty 计（500→500）",
      glue.clamp_available_qty(500, pos_qty=1000, available=None, inflight=0) == (500, ""))
check("available 钳制（可用 300 < 需求 500 → 300）",
      glue.clamp_available_qty(500, pos_qty=1000, available=300, inflight=0) == (300, ""))
check("inflight 在途扣减（可用 500 - 在途 200 → 300）",
      glue.clamp_available_qty(500, pos_qty=1000, available=500, inflight=200) == (300, ""))
check("整百向下取整（可用 450 → 400）",
      glue.clamp_available_qty(500, pos_qty=1000, available=450, inflight=0) == (400, ""))
check("不足 100 → (0, no_available)",
      glue.clamp_available_qty(500, pos_qty=1000, available=150, inflight=100) == (0, "no_available"))
check("pos_qty 与 available 取小（pos=200 < avail=900 → 200）",
      glue.clamp_available_qty(500, pos_qty=200, available=900, inflight=0) == (200, ""))
check("在途冻结超过可用 → no_available（不出现负量）",
      glue.clamp_available_qty(500, pos_qty=1000, available=300, inflight=400) == (0, "no_available"))
check("no_available 已进 SKIP_REASONS 全集", "no_available" in glue.SKIP_REASONS)

print("== 3. buyback_retry_step：接回重试决策 ==")
check("下单成功 → (settle, 0)", glue.buyback_retry_step(7, True) == ("settle", 0))
check("首次失败 → (retry, 1)", glue.buyback_retry_step(0, False) == ("retry", 1))
check("第 30 次失败 → (retry, 30)（未超上限）",
      glue.buyback_retry_step(29, False) == ("retry", 30))
check("第 31 次失败 → (void, 31)（>30 转 void）",
      glue.buyback_retry_step(30, False) == ("void", 31))
check("上限参数可注入（max_retry=2，第 3 次 → void）",
      glue.buyback_retry_step(2, False, max_retry=2) == ("void", 3))

print("== 4. 实单卖出纯逻辑判定流串联（guard → signal → qty → clamp） ==")
CODE = "600176"
sig = ort.detect_signal(CODE, "中国巨石", _tail30_bars(), pos_qty=1000, base_ref=1000,
                        now=None)
tail30 = ort.compute_tail30_pct(_tail30_bars())
dec, reason = glue.guard_decision(
    tail30=tail30, breaker_tripped=False, has_awaiting_buyback=False,
    pos_qty=1000, base_ref=1000, protect_sold_today=False, in_b7_chain=False)
check("tail30>1% 全过 → go", dec == "go" and reason == "")
check("detect_signal 复核通过（sig 非 None，price=c14:55）",
      sig is not None and sig["price"] > 0)
vq = ort.compute_virtual_qty(1000, 1000)
check("compute_virtual_qty = min(pos, base*0.5) 整百 = 500", vq == 500)
check("qty_gate(500) → go", glue.qty_gate(vq) == ("go", ""))
q, r = glue.clamp_available_qty(vq, pos_qty=1000, available=1000, inflight=0)
check("clamp 后 qty=500 可下单", q == 500 and r == "")
# 守卫拦截场景：触发但被可用量拦 → skip no_available（实单独有）
q2, r2 = glue.clamp_available_qty(vq, pos_qty=1000, available=0, inflight=0)
check("可用量为 0 → skip no_available（实单独有拦截）", q2 == 0 and r2 == "no_available")
# 守卫拦截场景：已有 armed 链 → in_b7_chain（与影子同口径）
dec2, reason2 = glue.guard_decision(
    tail30=tail30, breaker_tripped=False, has_awaiting_buyback=False,
    pos_qty=1000, base_ref=1000, protect_sold_today=False, in_b7_chain=True)
check("已有 armed 链 → skip in_b7_chain", (dec2, reason2) == ("skip", "in_b7_chain"))

print("== 5. 链生命周期（真实引擎持久化 API，落盘已重定向临时目录） ==")
eng = tea.SignalEngine()
eng.b7_overnight_chains.clear()
SELL_DATE, NEXT_DATE = "2026-09-16", "2026-09-17"
cid = ort.make_chain_id(CODE, SELL_DATE)
eng.arm_b7_chain(cid, CODE, 500, 10.20, SELL_DATE)
check("arm 后链 armed 挂账", eng.b7_overnight_chains[cid]["status"] == "armed")
check("armed_chain_id 检出同票链", glue.armed_chain_id(eng.b7_overnight_chains, CODE) == cid)
check("当日（sell_date）不结算", glue.find_due_chains(eng.b7_overnight_chains, CODE, SELL_DATE) == [])
due = glue.find_due_chains(eng.b7_overnight_chains, CODE, NEXT_DATE)
check("次日首根 bar 到期（1 条）", len(due) == 1 and due[0][0] == cid)
check("他票隔离", glue.find_due_chains(eng.b7_overnight_chains, "000001", NEXT_DATE) == [])
# 模拟重启：新实例从磁盘恢复 armed 链（拍板口径：实单不允许内存链重启即丢）
eng2 = tea.SignalEngine()
check("跨进程恢复 armed 链", eng2.b7_overnight_chains.get(cid, {}).get("status") == "armed")
# 次日 open 价接回 → settle
sell_entry = glue.sell_entry_from_chain(eng2.b7_overnight_chains[cid])
check("sell_entry 配对字段齐全（qty/sell_px/chain_id）",
      sell_entry["qty"] == 500 and abs(sell_entry["sell_px"] - 10.20) < 1e-9
      and sell_entry["chain_id"] == cid)
eng2.settle_b7_chain(cid, buy_px=10.05, buy_date=NEXT_DATE)
check("settle 后链移出挂账表", cid not in eng2.b7_overnight_chains)
check("armed_chain_id 不再检出", glue.armed_chain_id(eng2.b7_overnight_chains, CODE) is None)
# void 路径：HARD_STOP/拒单/超时作废
cid2 = ort.make_chain_id(CODE, "2026-09-18")
eng2.arm_b7_chain(cid2, CODE, 500, 10.30, "2026-09-18")
eng2.void_b7_chain(cid2, reason="hard_stop_exit")
check("void 后链移出挂账表（HARD_STOP 同款处置对实单链生效）",
      cid2 not in eng2.b7_overnight_chains)

print("== 6. 连亏 4 笔费后为负熔断（跨进程持久化，人工 reset） ==")
eng3 = tea.SignalEngine()
eng3.b7_overnight_chains.clear()
eng3.b7_circuit = {"consecutive_losses": 0, "tripped": False}
breaker = ort.B7CircuitBreaker.from_dict(
    eng3.b7_circuit, on_change=lambda s: eng3.record_b7_circuit(s))
# 4 笔费后为负：卖 10.20 接回 10.35（高开反T亏损）
for _ in range(4):
    breaker.record(ort.virtual_net_pct(10.20, 10.35))
check("连亏 4 笔费后为负 → 熔断触发", breaker.tripped)
check("熔断状态已经 on_change 落盘", eng3.b7_circuit.get("tripped") is True
      and int(eng3.b7_circuit.get("consecutive_losses", 0)) >= 4)
# 模拟重启：新实例恢复 tripped（人工 reset 前不自动复活）
eng4 = tea.SignalEngine()
breaker2 = ort.B7CircuitBreaker.from_dict(eng4.b7_circuit)
check("跨进程恢复 tripped=True（不自动复活）", breaker2.tripped)
# 人工 reset 口径验证
breaker2.reset()
check("人工 reset 后熔断解除", not breaker2.tripped)

print("== 7. B1 修复回归：B7 卖成交后 awaiting_buyback 无该 code 挂账 ==")
# 镜像 gm_main 成交回调时序：B7 卖单成交 → generic record_trade_action 以 SELL_HIGH
# 武装回补记忆（L3330 附近）→ B7 分支 clear_awaiting_buyback(reason="b7_overnight")
# 在其后确定性覆盖（与 HARD_STOP_EXIT/T_LEG_CLOSE 同款先例）
eng5 = tea.SignalEngine()
eng5.b7_overnight_chains.clear()
CODE7 = "600176"
cid7 = ort.make_chain_id(CODE7, "2026-09-16")
eng5.arm_b7_chain(cid7, CODE7, 500, 10.20, "2026-09-16")   # B7 下单成功挂账在前
_rta7 = eng5.record_trade_action(CODE7, "SELL_HIGH", 500, 10.20)  # generic arm（隐患复现）
check("generic SELL_HIGH 成交会武装 awaiting_buyback（B1 隐患前提成立）",
      bool(eng5.awaiting_buyback.get(CODE7)))
eng5.clear_awaiting_buyback(CODE7, reason="b7_overnight")          # B7 分支 clear（修复）
check("B7 分支 clear 后 awaiting_buyback 无挂账 → 14:56 数量不变硬约束不触发",
      CODE7 not in eng5.awaiting_buyback)
check("clear 不误伤 B7 隔夜链（次日接回链独立存活）",
      glue.armed_chain_id(eng5.b7_overnight_chains, CODE7) == cid7)
check("次日该链仍到期可接回", len(glue.find_due_chains(eng5.b7_overnight_chains, CODE7, "2026-09-17")) == 1)

print("== 8. B2 修复回归：有 armed B7 链的 code 不进 open_align 买入清单 ==")
_chains8 = {
    "c1": {"code": "600176", "status": "armed", "sell_date": "2026-09-16",
           "qty": 500, "sell_px": 10.2},
    "c2": {"code": "000001", "status": "armed", "sell_date": "2026-09-17",
           "qty": 300, "sell_px": 5.0},
    "c3": {"code": "600481", "status": "voided", "sell_date": "2026-09-16",
           "qty": 100, "sell_px": 3.0},
}
check("昨日卖出已到接回日的 armed 链 → 排除（open_align 买入不碰）",
      glue.open_align_buy_excluded(_chains8, "600176", "2026-09-17") is True)
check("当日新 armed 链（sell_date == today，未到期）→ 不排除",
      glue.open_align_buy_excluded(_chains8, "000001", "2026-09-17") is False)
check("voided 终态链 → 不排除（缺口归 open_align 正常补）",
      glue.open_align_buy_excluded(_chains8, "600481", "2026-09-17") is False)
check("无链 code → 不排除",
      glue.open_align_buy_excluded(_chains8, "300054", "2026-09-17") is False)
check("chains=None（engine 缺属性 getattr 双保险）→ 不排除（fail-open）",
      glue.open_align_buy_excluded(None, "600176", "2026-09-17") is False)

print("== 9. B3 核实回归：买入成交回调按订单号清除接回重挂快照 ==")
# 核实结论：gm_main 买入成交回调已有清除逻辑（L3351-3361，施工报告正确、审计快照
# 过时），此处镜像其键匹配语义做行为回归（防后续改动把清除逻辑改丢）
def _mirror_fill_clear_snapshot(pending, code, order):
    """镜像 gm_main L3351-3361：order_ids 为空（未取到单号）或订单号命中 → 清除。"""
    if code in pending:
        _oid = str(order.get("cl_ord_id") or order.get("id")
                   or order.get("order_id") or "")
        _ids = pending[code].get("order_ids") or set()
        if not _ids or _oid in _ids:
            pending.pop(code, None)

_p9 = {"600176": {"chain": {"chain_id": "c9"}, "order_ids": {"oid-1"},
                  "date": "2026-09-17"}}
_mirror_fill_clear_snapshot(_p9, "600176", {"id": "oid-9"})
check("非本接回单的买入成交 → 快照保留（不误清）", "600176" in _p9)
_mirror_fill_clear_snapshot(_p9, "600176", {"cl_ord_id": "oid-1"})
check("本接回单成交（订单号命中）→ 快照清除 → 后续买拒单不会误重挂幽灵链",
      "600176" not in _p9)
_p9b = {"600176": {"chain": {"chain_id": "c9b"}, "order_ids": set(),
                   "date": "2026-09-17"}}
_mirror_fill_clear_snapshot(_p9b, "600176", {"id": "anything"})
check("order_ids 为空（下单时未取到单号）→ 任意买入成交兜底清除",
      "600176" not in _p9b)

print("== 10. B4/B8 修复回归：接回重发前对账决策 + limit_clamped 枚举 ==")
check("当日已有买单成交 → settle_estimated（不重发，同一链不会买两次）",
      glue.buyback_recon_decide(inflight_buy=False, filled_buy_today=True,
                                pos_now=1000, sell_pos_after=500,
                                chain_qty=500) == "settle_estimated")
check("持仓已较卖出后恢复 ≥ 链上 qty → settle_estimated",
      glue.buyback_recon_decide(inflight_buy=False, filled_buy_today=False,
                                pos_now=1000, sell_pos_after=500,
                                chain_qty=500) == "settle_estimated")
check("持仓恢复不足（+400 < 500）→ order（正常重发）",
      glue.buyback_recon_decide(inflight_buy=False, filled_buy_today=False,
                                pos_now=900, sell_pos_after=500,
                                chain_qty=500) == "order")
check("基线未知（sell_pos_after=None）仅凭在途买单 → skip_inflight（不重发不计重试）",
      glue.buyback_recon_decide(inflight_buy=True, filled_buy_today=False,
                                pos_now=None, sell_pos_after=None,
                                chain_qty=500) == "skip_inflight")
check("在途买单但持仓已恢复 → settle_estimated 优先（已满足 > 在途等待）",
      glue.buyback_recon_decide(inflight_buy=True, filled_buy_today=False,
                                pos_now=1000, sell_pos_after=500,
                                chain_qty=500) == "settle_estimated")
check("无成交/无恢复/无在途 → order（维持原重发行为）",
      glue.buyback_recon_decide(inflight_buy=False, filled_buy_today=False,
                                pos_now=500, sell_pos_after=500,
                                chain_qty=500) == "order")
check("limit_clamped 已进 SKIP_REASONS（B8 卖侧 skip reason，append-only）",
      "limit_clamped" in glue.SKIP_REASONS
      and glue.SKIP_REASONS[:7] == ("breaker_tripped", "has_awaiting_buyback",
                                    "pos_below_base", "protect_sell_today",
                                    "in_b7_chain", "qty_below_100", "no_available"))

print()
print(f"合计 {n_checks[0]} 项检查，失败 {len(fails)} 项")
if fails:
    print("失败项：")
    for f in fails:
        print(f"  - {f}")
    sys.exit(1)
print("全部通过 ✓")
