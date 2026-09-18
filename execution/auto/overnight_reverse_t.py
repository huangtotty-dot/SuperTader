# -*- coding: utf-8 -*-
"""overnight_reverse_t.py — B7 尾盘反T通道（OVERNIGHT_REVERSE_T）信号检测 + 影子台账（草案）。

⚠️ 草案状态（2026-09-15 设计师_D4 → B7影子施工1/3 加固）：
- 本文件为周六评审用代码草案，**未接入任何生产链路**（gm_main / sell_channels 均未 import 本模块）。
- 不 import gm.api，不依赖 production 模块，纯标准库，可独立 py_compile / 离线自测。
- 影子模式下只写 `t_io/logs/b7_shadow_{date}.jsonl`（生产接入时的默认路径）；
  自测一律写 tempfile 临时目录，不触碰 t_io 生产目录。

2026-09-15 B7影子施工1/3（模块加固+持久化，owner 拍板口径见 doc/solutions/2026-09-15_B7尾盘反T通道施工方案.md §7.1）：
- 事件链式配对：b7_signal / b7_virtual_sell / b7_virtual_buyback 统一 chain_id（f"{code}_{sell_date}"）；
- compute_virtual_qty：min(pos_qty, base_ref×50%) 向下整百（拍板口径①）；
- detect_signal 守卫加强为 pos_qty >= base_ref > 0（归位完成语义）；
- B7CircuitBreaker 支持 to_dict/from_dict + on_change 持久化回调（拍板口径②：状态跨进程持久化）；
- 影子验收：mean_net_vs_offline 不再参与 20% 漂移告警（只保留净收益转负告警）。

策略依据：doc/experiment/2026-09-15_B7隔夜反T策略化实验.md
  唯一过闸 cell：S1（尾盘30min 涨幅 >1%，c14:55/c14:30）× 次日开盘接回，
  费后净均 +0.638%/笔、胜率 64.1%、优于随机 +0.70pp、OOS +0.791%（n=196）、TOP3 贡献 27.8%。
  风险：36% 卖飞、次日高开≥1% 日（72/421）平均 −2.94%、最长连亏 11 笔。

费用口径（与实验/生产一致）：卖出 0.00121（GM 全成本），买入 0.00015，双边 0.136%。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime

# ── 通道常量 ──────────────────────────────────────────────────────────────
CHANNEL = "OVERNIGHT_REVERSE_T"        # 卖出通道名（新通道，影子模式只落日志）
BUYBACK_CHANNEL = "B7_BUYBACK"         # 次日开盘接回通道名

SIGNAL_THRESHOLD = 0.01                # S1：尾盘30min涨幅 > 1%
REF_BAR_HHMM = "14:30"                 # 基准价 bar（c14:30）
SELL_BAR_HHMM = "14:55"                # 信号判定/卖出 bar（c14:55，≤14:55 口径）
SELL_FEE = 0.00121                     # 卖出全成本（佣金+印花税+过户费）
BUY_FEE = 0.00015                      # 买入佣金
CIRCUIT_BREAKER_N = 4                  # 连亏熔断闸（建议值，owner 拍板项；依据 E1 连亏分布）

# 离线实验基准值（影子期一致性漂移对照锚点）
OFFLINE_MEAN_NET_PCT = 0.00638         # 单笔费后净均 +0.638%
OFFLINE_WIN_RATE = 0.641               # 胜率 64.1%（= 次日低开率 270/421）
OFFLINE_NEXT_GAP_MEAN_PCT = -0.0077    # 信号日次日 gap 均值 ≈ −0.77%（收涨/未收涨两组 −0.789/−0.723 之间）
OFFLINE_FLY_RATE = 0.36                # 卖飞率（次日高开占比）≈36%
OFFLINE_FREQ_PER_STOCK_MONTH = 0.99    # 信号频率 0.99 次/票/月
DRIFT_ALARM_RATIO = 0.20               # 一致性漂移 >20% 告警


# ── 工具 ──────────────────────────────────────────────────────────────────
def _bar_hhmm(bar: dict) -> str:
    """从 bar 提取 'HH:MM'。兼容 'HH:MM' / 'HH:MM:SS' / 'YYYY-MM-DD HH:MM[:SS]'。"""
    t = str(bar.get("time") or bar.get("eob") or "")
    if " " in t:
        t = t.rsplit(" ", 1)[-1]
    return t[:5]


def find_close_at(bars: list, hhmm: str) -> float | None:
    """取指定 HH:MM bar 的收盘价；无该 bar 时取此前最近 bar（保守：绝不取未来 bar）。"""
    best = None
    for b in bars:
        hm = _bar_hhmm(b)
        if hm and hm <= hhmm:
            c = b.get("close")
            if c is not None and float(c) > 0:
                best = float(c)
    return best


def compute_tail30_pct(bars: list, now_close: float | None = None) -> float | None:
    """尾盘30min涨幅 = c14:55 / c14:30 − 1（严格 ≤14:55 口径，与实验 S1 一致）。

    bars 须为当日分钟 bar 序列（任一元素含 time/close 即可）；数据不足返回 None。

    ⚠️ **now_close 的由来与更正（2026-09-18 审计）**：
    该参数原是为修「`bar_cache` 不含当前 bar、14:55 评估时末根是 14:54」而加。**该诊断是错的**：
    它来自一个放在 `on_bar` 逐票循环**顶端**的观测，而 `bar_cache` 的 append 发生在
    `_dedup_bar` 之后、**决策代码之前** → 真实决策点 cache 末根**就是当前 bar**。
    实调用点审计（12/12 样本）：`cache末根=14:55 n=235 find('14:55')取到价 == cp`。
    ⇒ **引擎本来就是对的，本参数是惰性的**（传 `now_close=cp` 与不传等价）。
    保留仅为显式化"判定价 = 成交价"的意图，调用方传当前 bar 收盘价即可。
    """
    c_ref = find_close_at(bars, REF_BAR_HHMM)
    c_now = float(now_close) if now_close else find_close_at(bars, SELL_BAR_HHMM)
    if c_ref is None or c_now is None or c_ref <= 0:
        return None
    return c_now / c_ref - 1.0


def make_chain_id(code: str, sell_date: str) -> str:
    """B7 链唯一标识：f"{code}_{sell_date}"（同票同日唯一一条 B7 链）。"""
    return f"{code}_{sell_date}"


def compute_virtual_qty(pos_qty: int, base_ref: int) -> int:
    """B7 单次虚拟/真实卖出量（owner 拍板口径①：≤ 实际持仓 且 ≤ base_ref 的 50%，向下整百）。

    min(pos_qty, int(base_ref*0.5)) // 100 * 100；不足 100 股返回 0（不开链）。
    """
    cap = min(int(pos_qty), int(int(base_ref) * 0.5))
    if cap <= 0:
        return 0
    return cap // 100 * 100


# ── 信号检测 ──────────────────────────────────────────────────────────────
def detect_signal(code: str, name: str, bars: list,
                  pos_qty: int = 0, base_ref: int = 0,
                  has_awaiting_buyback: bool = False,
                  now: datetime | None = None,
                  now_close: float | None = None,
                  relax_base_guard: bool = False) -> dict | None:
    """B7 信号检测（14:55 bar 调用一次）。触发返回标准信号 dict，否则 None。

    生产接入时的前置守卫（本函数只做检测，守卫由通道层执行，此处留作口径说明）：
      1) 仅 14:55 bar 评估（与 TAIL 归位 14:50 起同 bar 互斥：TAIL 已执行则跳过）；
      2) pos_qty >= base_ref > 0（归位已完成语义：B7 卖的是底仓的隔夜敞口，非超仓；
         2026-09-15 施工1/3 由 pos_qty>0 加强为归位完成口径，与 gm_main 回测接线一致）；
      3) 该票无 pending 的日内反T回补义务（awaiting_buyback）——避免两套回补链互撞；
      4) 连亏熔断未触发（见 B7CircuitBreaker）。
    """
    # relax_base_guard（2026-09-17，**仅评估用**）：归位护栏 pos_qty >= base_ref 会挡掉
    # **恰恰最好的日子**——尾盘急拉越猛 ⇒ 日内波动越大 ⇒ T 腿越可能还开着 ⇒ 持仓不在底仓。
    # 实测：窗口内被它挡掉的 000988 04-13 缺口 **−4.031%**（全窗口最大）。
    # 放宽后只要求有底仓（pos_qty>0），卖出量仍由调用方按 min(持仓, 底仓额度) 收口。
    # 默认 False = 生产口径逐字不变。
    if relax_base_guard:
        if not (int(pos_qty) > 0 and int(base_ref) > 0):
            return None
    elif not (int(pos_qty) >= int(base_ref) > 0):
        return None
    if has_awaiting_buyback:
        return None
    tail30 = compute_tail30_pct(bars, now_close=now_close)
    if tail30 is None or tail30 <= SIGNAL_THRESHOLD:
        return None

    ts = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    c_ref = find_close_at(bars, REF_BAR_HHMM)
    c_now = float(now_close) if now_close else find_close_at(bars, SELL_BAR_HHMM)
    return {
        "code": code,
        "name": name,
        "action": CHANNEL,
        "price": round(c_now, 4),
        "score": 70.0,                 # 低于 PANIC/TRAIL/TREND_EXIT/TARGET，高于常规做T；影子期不消费
        "reasons": [f"B7尾盘反T: tail30={tail30:+.2%} > {SIGNAL_THRESHOLD:.0%} (c14:30={c_ref:.4f}→c14:55={c_now:.4f})"],
        "factors": {
            "tail30_pct": round(tail30 * 100, 4),
            "c1430": round(c_ref, 4),
            "c1455": round(c_now, 4),
            "sell_bar": SELL_BAR_HHMM,
            "ref_bar": REF_BAR_HHMM,
        },
        "time": ts,
    }


def virtual_net_pct(sell_px: float, buyback_px: float) -> float:
    """虚拟费后净收益（实验口径）：(卖×(1−0.00121) − 接回×(1+0.00015)) / 卖。"""
    if sell_px <= 0:
        return 0.0
    return (sell_px * (1 - SELL_FEE) - buyback_px * (1 + BUY_FEE)) / sell_px


# ── 连亏熔断 ──────────────────────────────────────────────────────────────
class B7CircuitBreaker:
    """连亏熔断：全池连续亏 N 笔（默认 4，owner 拍板项）→ 通道暂停。

    依据：E1 全样本 421 笔最长连亏 11 笔（10.9 个月极值），S2/S4 最长仅 3 笔；
    胜率 64.1% 下连亏 4 笔的概率 ≈ 0.359^4 ≈ 1.7%/段，属低成本「市场状态可能漂移」告警。

    2026-09-15 施工1/3（拍板口径②：状态必须跨进程持久化）：
    - to_dict()/from_dict() 序列化往返，供引擎侧落盘（t_engine_auto.record_b7_circuit）；
    - 可选 on_change 回调：record()/reset() 状态变更后以 to_dict() 结果调用一次；
    - 默认 on_change=None 时保持纯内存行为，与草案版完全一致。
    """

    def __init__(self, n: int = CIRCUIT_BREAKER_N, on_change=None):
        self.n = int(n)
        self.consecutive_losses = 0
        self.tripped = False
        self._on_change = on_change

    def _notify(self):
        if callable(self._on_change):
            try:
                self._on_change(self.to_dict())
            except Exception:
                pass  # 持久化回调 fail-open，不影响熔断本体

    def to_dict(self) -> dict:
        return {"n": self.n, "consecutive_losses": self.consecutive_losses,
                "tripped": bool(self.tripped)}

    @classmethod
    def from_dict(cls, state: dict, on_change=None) -> "B7CircuitBreaker":
        """从 to_dict() 结果恢复（容错：非 dict/缺字段时回退默认新实例）。"""
        state = state if isinstance(state, dict) else {}
        cb = cls(n=state.get("n", CIRCUIT_BREAKER_N), on_change=on_change)
        cb.consecutive_losses = int(state.get("consecutive_losses", 0) or 0)
        cb.tripped = bool(state.get("tripped", False))
        return cb

    def record(self, net_pct: float) -> bool:
        """登记一笔虚拟/实盘净收益（小数）。返回当前是否熔断。"""
        if net_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if self.consecutive_losses >= self.n:
            self.tripped = True
        self._notify()
        return self.tripped

    def reset(self):
        """人工复盘后手动 reset（owner 拍板：不自动复活）。"""
        self.consecutive_losses = 0
        self.tripped = False
        self._notify()


# ── 影子台账 ──────────────────────────────────────────────────────────────
class B7ShadowLedger:
    """影子模式台账：信号与虚拟成交追加写 b7_shadow_{date}.jsonl，绝不下单。

    生产接入默认 log_dir = t_io/logs/；自测/回放必须显式传入临时目录。
    事件类型（2026-09-15 施工1/3：b7_skip 无链，其余三类统一 chain_id = f"{code}_{sell_date}"）：
      b7_signal          信号触发留痕（含 chain_id）
      b7_virtual_sell    虚拟卖出（14:55 收盘价 × 虚拟量；含 chain_id + sell_date）
      b7_virtual_buyback 次日开盘虚拟接回 + 费后净收益结算（含 chain_id + buy_date/sell_date）
      b7_skip            触发但被守卫拦截（熔断/互斥），复盘对照用（无链，不加 chain_id）
    """

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)

    def _path(self, date: str) -> str:
        return os.path.join(self.log_dir, f"b7_shadow_{date}.jsonl")

    def _append(self, date: str, event: dict):
        event = dict(event)
        event.setdefault("ts", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with open(self._path(date), "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_signal(self, sig: dict, pos_qty: int, virtual_qty: int,
                      chain_id: str = ""):
        date = sig.get("time", "")[:10]
        self._append(date, {"event": "b7_signal", "sig": sig,
                            "chain_id": chain_id or make_chain_id(sig["code"], date),
                            "pos_qty": int(pos_qty), "virtual_qty": int(virtual_qty)})

    def record_skip(self, code: str, date: str, reason: str, tail30_pct=None):
        self._append(date, {"event": "b7_skip", "code": code,
                            "reason": reason, "tail30_pct": tail30_pct})

    def record_virtual_sell(self, sig: dict, virtual_qty: int,
                            chain_id: str = "", sell_date: str = "") -> dict:
        """虚拟卖出：以信号价（c14:55）成交，返回待结算条目（含 chain_id/sell_date 供配对）。"""
        date = sig.get("time", "")[:10]
        sell_date = sell_date or date
        entry = {"event": "b7_virtual_sell", "code": sig["code"],
                 "chain_id": chain_id or make_chain_id(sig["code"], sell_date),
                 "qty": int(virtual_qty), "sell_px": sig["price"],
                 "sell_date": sell_date,
                 "tail30_pct": sig["factors"]["tail30_pct"]}
        self._append(date, entry)
        return entry

    def record_virtual_buyback(self, sell_entry: dict, buy_date: str,
                               open_px: float, prev_close: float,
                               chain_id: str = "") -> dict:
        """次日开盘虚拟接回并结算：净收益 + 隔夜 gap（对照实盘走势用）。

        chain_id 缺省从 sell_entry 继承；buy_date/sell_date 显式落事件，供链式配对复盘。"""
        sell_px = float(sell_entry["sell_px"])
        net = virtual_net_pct(sell_px, open_px)
        gap = (open_px / prev_close - 1.0) if prev_close > 0 else None
        sell_date = sell_entry.get("sell_date", "")
        entry = {"event": "b7_virtual_buyback", "code": sell_entry["code"],
                 "chain_id": chain_id or sell_entry.get("chain_id")
                 or make_chain_id(sell_entry["code"], sell_date),
                 "qty": sell_entry["qty"], "buy_px": float(open_px),
                 "sell_px": sell_px, "net_pct": round(net * 100, 4),
                 "buy_date": buy_date, "sell_date": sell_date,
                 "overnight_gap_pct": round(gap * 100, 4) if gap is not None else None,
                 "win": net > 0}
        self._append(buy_date, entry)
        return entry


# ── 影子期验收口径 ────────────────────────────────────────────────────────
def shadow_acceptance_check(events: list, pool_size: int, trading_days: int) -> dict:
    """影子期（2 周）验收：信号数 / 虚拟净收益 / 与离线实验一致性漂移。

    输入 events 为 b7_shadow_*.jsonl 合并后的事件 list（dict）。
    返回 {n_signals, win_rate, mean_net_pct, fly_rate, freq_per_stock_month,
          drift: {指标: 相对漂移}, alarms: [...], pass: bool}。
    注意：2 周 n≈19 笔不足以做均值显著性检验，验收以分布形态对照为主（诚实声明）。
    """
    sells = [e for e in events if e.get("event") == "b7_virtual_sell"]
    settles = [e for e in events if e.get("event") == "b7_virtual_buyback"]
    n = len(settles)
    alarms = []

    wins = sum(1 for e in settles if e.get("win"))
    win_rate = wins / n if n else None
    nets = [float(e["net_pct"]) / 100 for e in settles]
    mean_net = sum(nets) / n if n else None
    gaps = [float(e["overnight_gap_pct"]) / 100 for e in settles
            if e.get("overnight_gap_pct") is not None]
    fly_rate = (sum(1 for g in gaps if g > 0) / len(gaps)) if gaps else None

    months = trading_days / 21.0 if trading_days else 1.0
    freq = (len(sells) / pool_size / months) if pool_size else None

    def _drift(actual, expect):
        if actual is None or expect == 0:
            return None
        return abs(actual - expect) / abs(expect)

    drift = {
        "win_rate_vs_offline": _drift(win_rate, OFFLINE_WIN_RATE),
        "fly_rate_vs_offline": _drift(fly_rate, OFFLINE_FLY_RATE),
        "freq_vs_offline": _drift(freq, OFFLINE_FREQ_PER_STOCK_MONTH),
        # 2026-09-15 施工1/3：mean_net_vs_offline 保留计算供对照展示，但不参与 20% 漂移告警——
        # 方案 §3.2 对净收益只要求「不得转负」，2 周小样本不做均值漂移显著性检验（诚实声明）。
        "mean_net_vs_offline": _drift(mean_net, OFFLINE_MEAN_NET_PCT),
    }
    for k, v in drift.items():
        if k == "mean_net_vs_offline":
            continue
        if v is not None and v > DRIFT_ALARM_RATIO:
            alarms.append(f"漂移告警 {k}: {v:.0%} > {DRIFT_ALARM_RATIO:.0%}")
    if mean_net is not None and mean_net < 0:
        alarms.append(f"影子期虚拟净均转负: {mean_net:+.3%}（离线 +{OFFLINE_MEAN_NET_PCT:.3%}）")

    # 信号数合理性：验收区间 = 预期的 0.4×~2.5×（全池预期 ≈ 0.99×池子×(10/21)；
    # 39票池2周 ≈ 19 笔 → 区间 ≈ 8~48 笔，方案 §3.2 口径）
    expected = OFFLINE_FREQ_PER_STOCK_MONTH * pool_size * months
    if len(sells) < max(3, expected * 0.4) or len(sells) > expected * 2.5:
        alarms.append(f"信号数异常: {len(sells)} vs 预期≈{expected:.0f}")

    return {"n_signals": len(sells), "n_settled": n,
            "win_rate": win_rate, "mean_net_pct": mean_net,
            "fly_rate": fly_rate, "freq_per_stock_month": freq,
            "drift": drift, "alarms": alarms, "pass": not alarms}


# ── 离线自测（python execution/auto/overnight_reverse_t.py）────────────────
def _mk_bars(c1430: float, c1455: float) -> list:
    """合成当日分钟 bar：只关心 14:30 与 14:55 两点，中间线性过渡。"""
    bars = [{"time": "2026-09-15 09:30", "close": c1430}]
    for m in range(31, 56):
        frac = (m - 30) / 25.0
        bars.append({"time": f"2026-09-15 14:{m:02d}",
                     "close": round(c1430 + (c1455 - c1430) * frac, 4)})
    bars.insert(1, {"time": "2026-09-15 14:30", "close": c1430})
    return bars


def _selftest() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    fails = []
    n_checks = [0]

    def check(name, cond):
        n_checks[0] += 1
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        if not cond:
            fails.append(name)

    print("== 1. 信号触发/不触发（守卫：pos_qty >= base_ref > 0） ==")
    _now = datetime(2026, 9, 15, 14, 55)  # 自测时间注入，不读系统时钟
    sig = detect_signal("600000", "测试票", _mk_bars(10.00, 10.15),  # +1.5%
                        pos_qty=1000, base_ref=1000, now=_now)
    check("tail30=+1.5% 触发", sig is not None and sig["action"] == CHANNEL)
    check("信号价=c14:55", sig and abs(sig["price"] - 10.15) < 1e-6)
    sig2 = detect_signal("600000", "测试票", _mk_bars(10.00, 10.05),  # +0.5%
                         pos_qty=1000, base_ref=1000)
    check("tail30=+0.5% 不触发", sig2 is None)
    sig3 = detect_signal("600000", "测试票", _mk_bars(10.00, 10.15),
                         pos_qty=1000, base_ref=1000, has_awaiting_buyback=True)
    check("有回补义务互斥不触发", sig3 is None)
    check("空仓不触发", detect_signal("600000", "t", _mk_bars(10, 10.2), 0, 1000) is None)
    check("缺bar不触发", detect_signal("600000", "t", [], 1000, 1000) is None)
    # 施工1/3③：归位未完成（pos_qty < base_ref）不触发
    check("归位未完成不触发(pos<base)",
          detect_signal("600000", "t", _mk_bars(10, 10.2), 800, 1000) is None)
    check("base_ref=0 不触发",
          detect_signal("600000", "t", _mk_bars(10, 10.2), 1000, 0) is None)
    check("超仓可归位完成触发(pos>base)",
          detect_signal("600000", "t", _mk_bars(10, 10.2), 1200, 1000) is not None)

    print("== 2. 虚拟净收益（实验口径） ==")
    net_down = virtual_net_pct(10.15, 9.95)     # 次日低开 → 应赚
    net_up = virtual_net_pct(10.15, 10.30)      # 次日高开 → 应亏
    check(f"低开接回为正 ({net_down:+.3%})", net_down > 0)
    check(f"高开接回为负 ({net_up:+.3%})", net_up < 0)
    check("费用≈双边0.136%", abs(virtual_net_pct(10.0, 10.0) - (-0.00136)) < 1e-5)

    print("== 3. compute_virtual_qty（拍板口径：≤持仓且≤base_ref 50%，向下整百） ==")
    check("持仓1000/base1000 → 500", compute_virtual_qty(1000, 1000) == 500)
    check("持仓300/base1000 → 300（持仓<半仓）", compute_virtual_qty(300, 1000) == 300)
    check("持仓550/base2000 → 500（向下整百）", compute_virtual_qty(550, 2000) == 500)
    check("持仓99/base1000 → 0（不足100不开链）", compute_virtual_qty(99, 1000) == 0)
    check("持仓10000/base150 → 0（半仓75向下整百=0）", compute_virtual_qty(10000, 150) == 0)
    check("持仓0/base1000 → 0", compute_virtual_qty(0, 1000) == 0)
    check("持仓250/base300 → 100（min(250,150)=150→100）", compute_virtual_qty(250, 300) == 100)

    print("== 4. 连亏熔断 N=4 + 持久化序列化 ==")
    cb = B7CircuitBreaker(4)
    for i in range(3):
        check(f"第{i+1}笔亏未熔断", cb.record(-0.005) is False)
    check("第4笔亏熔断", cb.record(-0.005) is True)
    cb.reset()
    cb.record(-0.005); cb.record(0.01); cb.record(-0.005)
    check("盈利中断连亏计数", cb.consecutive_losses == 1 and not cb.tripped)
    # 施工1/3④：to_dict/from_dict 往返
    cb2 = B7CircuitBreaker(4)
    cb2.record(-0.005); cb2.record(-0.005)
    rt = B7CircuitBreaker.from_dict(cb2.to_dict())
    check("to_dict/from_dict 往返一致",
          rt.to_dict() == cb2.to_dict() and rt.consecutive_losses == 2)
    # from_dict 容错：脏输入回退默认
    cb_bad = B7CircuitBreaker.from_dict("garbage")
    check("from_dict 脏输入回退默认", cb_bad.consecutive_losses == 0 and not cb_bad.tripped)
    # 施工1/3④：on_change 回调在 record/reset 后触发
    captured = []
    cb3 = B7CircuitBreaker(4, on_change=captured.append)
    cb3.record(-0.005)
    check("record 触发 on_change",
          len(captured) == 1 and captured[0]["consecutive_losses"] == 1)
    cb3.reset()
    check("reset 触发 on_change",
          len(captured) == 2 and captured[1]["consecutive_losses"] == 0
          and captured[1]["tripped"] is False)
    # 默认无回调保持纯内存（不抛异常）
    B7CircuitBreaker(4).record(-0.005)
    check("默认 on_change=None 纯内存可用", True)

    print("== 5. 影子台账（chain_id 配对；临时目录，不碰 t_io） ==")
    with tempfile.TemporaryDirectory() as td:
        led = B7ShadowLedger(td)
        cid = make_chain_id(sig["code"], "2026-09-15")
        check("chain_id 格式 code_sell_date", cid == "600000_2026-09-15")
        led.record_signal(sig, pos_qty=1000, virtual_qty=300, chain_id=cid)
        entry = led.record_virtual_sell(sig, virtual_qty=300, chain_id=cid)
        check("virtual_sell 含 sell_date", entry.get("sell_date") == "2026-09-15")
        check("virtual_sell 死字段 settled 已删", "settled" not in entry)
        settle = led.record_virtual_buyback(entry, "2026-09-16", open_px=9.95,
                                            prev_close=10.20)
        check("虚拟接回结算 win=True", settle["win"] is True)
        check("结算净收益>0", settle["net_pct"] > 0)
        check("buyback 继承 chain_id 配对", settle["chain_id"] == cid)
        check("buyback 含 buy_date/sell_date",
              settle["buy_date"] == "2026-09-16" and settle["sell_date"] == "2026-09-15")
        path = led._path("2026-09-15")
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        check("jsonl 落盘 2 条", len(lines) == 2)
        day15 = [json.loads(x) for x in lines]
        check("落盘 signal/sell 均带 chain_id",
              all(e.get("chain_id") == cid for e in day15))
        path16 = led._path("2026-09-16")
        with open(path16, encoding="utf-8") as f:
            day16 = [json.loads(x) for x in f.readlines()]
        check("次日 buyback 落盘带 chain_id", day16[0].get("chain_id") == cid)
        events = day15 + day16

    print("== 6. 影子期验收口径（施工1/3⑤：净收益只看转负，不做均值漂移告警） ==")
    check(f"单样本验收可运行（n=1 触发信号数告警属预期）",
          isinstance(shadow_acceptance_check(events, pool_size=39, trading_days=10), dict))
    # 构造一批与离线实验一致的样本（胜率65%/卖飞率35%/净均≈0.63%）→ 不应有漂移告警
    synth = []
    for i in range(20):
        win = i % 20 < 13  # 13/20 = 65% 胜率
        synth.append({"event": "b7_virtual_sell", "code": "600000"})
        synth.append({"event": "b7_virtual_buyback", "code": "600000",
                      "net_pct": 1.5 if win else -1.0,          # 净均=(13×1.5−7×1.0)/20≈0.63%
                      "overnight_gap_pct": -0.8 if win else 0.8, "win": win})
    ok = shadow_acceptance_check(synth, pool_size=39, trading_days=10)
    check(f"一致样本 pass={ok['pass']} alarms={ok['alarms']}", ok["pass"])
    # 净收益为正但均值漂移 >20%（如净均 +1.5% vs 离线 +0.638%）→ 不告警（施工1/3⑤ 修正口径）
    hot = []
    for i in range(20):
        win = i % 20 < 13
        hot.append({"event": "b7_virtual_sell", "code": "600000"})
        hot.append({"event": "b7_virtual_buyback", "code": "600000",
                    "net_pct": 3.0 if win else 0.5,   # 净均≈+2.1%，漂移>>20% 但为正
                    "overnight_gap_pct": -0.8 if win else 0.8, "win": win})
    hotr = shadow_acceptance_check(hot, pool_size=39, trading_days=10)
    check("净收益正向大漂移不告警(mean_net 剔除漂移检验)", hotr["pass"])
    check("mean_net_vs_offline 仍计算供对照",
          hotr["drift"]["mean_net_vs_offline"] is not None
          and hotr["drift"]["mean_net_vs_offline"] > DRIFT_ALARM_RATIO)
    # 全亏样本 → 净均转负告警
    bad = [{"event": "b7_virtual_buyback", "net_pct": -1.5,
            "overnight_gap_pct": 1.2, "win": False} for _ in range(10)]
    bad += [{"event": "b7_virtual_sell"} for _ in range(10)]
    ng = shadow_acceptance_check(bad, pool_size=39, trading_days=10)
    check("全亏样本净均转负告警", not ng["pass"]
          and any("转负" in a for a in ng["alarms"]))

    print(f"\n自测结果: {n_checks[0]} 项断言，"
          f"{'全部通过' if not fails else '失败 ' + str(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
