# -*- coding: utf-8 -*-
"""
S1 每日选股输出器（前向状态机） · s1_daily.py
==============================================
研究线 S1 终选策略 **N=4 / M=8 / min_hold=1 / TP 臂 A / score_eq** 的
生产日频输出器。规则真源 = 同目录 s1_sim.py 的 ``s1_run_sim``（只读 import，
本文件逐行对齐其槽位/缓冲/最短持有/止盈臂/停牌顺延语义，不重写规则）。

时间线（与 s1_run_sim 完全一致）：
  - 信号在交易日 T 收盘后评估（T 收盘价比成本、当日 rank）；
  - 计划在 T+1 开盘执行（先卖后买，卖出回款当日可用 proceeds_lag=0）；
  - 生产节奏：T 晚间（W1 数据刷新到 T 之后）跑 ``--date T+1``，
    输出 picks_{T+1}.json，供 W3 执行器在 T+1 开盘下单。

状态：t_io/state/s1_book/state.json
  持仓（代码/股数/成本/入场日/止盈档位/峰值）、现金、asof（最近已评估收盘日）、
  pending（已生成待执行计划）。状态记账在下一晚补账时用**实际开盘价**成交，
  与仿真口径一致；真实成交差异（部分成交/涨跌停堵单）由执行器对账，本器
  次日重新评估即自动顺延/撤销。

CLI：
  python s1_daily.py --date 2026-10-12            # 生成该交易日开盘计划
  python s1_daily.py --date 2026-10-12 --bootstrap # 空仓 bootstrap（首日为 Top-4）
  python s1_daily.py --show                        # 查看当前账本
  python s1_daily.py --replay --start 2024-06-03 --end 2026-09-17
      # 硬验收：与 s1_run_sim 同窗逐笔对拍（换入/换出日期与标的须 100% 一致）

费率：
  replay 模式用仿真口径 FEE=0.000345（双边），保证与 s1_run_sim 逐笔一致；
  生产模式（--date/--bootstrap）用 core/cost_model.py 股票口径
  （买 0.00954% / 卖 0.05954%），仅影响股数与现金，不影响换入/换出决策。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]                      # E:\superTrader
sys.path.insert(0, str(HERE))                    # s1_sim（同目录，只读 import）
sys.path.insert(0, str(REPO_ROOT))               # core.cost_model

from s1_sim import FEE as SIM_FEE, INIT_CASH, STOP_LOSS, TP_TIERS, load_data, s1_run_sim  # noqa: E402
from core.cost_model import FEE_BUY_STOCK, FEE_SELL_STOCK                                   # noqa: E402

STATE_DIR = REPO_ROOT / "t_io" / "state" / "s1_book"
PICKS_DIR = STATE_DIR / "picks"
STATE_PATH = STATE_DIR / "state.json"

PARAMS = dict(n=4, m=8, min_hold=1, tp_arm="A", score="score_eq")
SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 排名（与 s1_run_sim 预计算逐行同构：值大者 rank 小，NaN -> inf，mergesort 保稳）
# ---------------------------------------------------------------------------
def rank_of_row(score_row: np.ndarray) -> np.ndarray:
    n = score_row.shape[0]
    r = np.full(n, np.inf, dtype=np.float64)
    ok = np.isfinite(score_row)
    if ok.sum() == 0:
        return r
    order = np.argsort(-score_row[ok], kind="mergesort")
    rr = np.empty(ok.sum(), dtype=np.float64)
    rr[order] = np.arange(1, ok.sum() + 1)
    r[np.flatnonzero(ok)] = rr
    return r


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
class Book:
    """S1 前向状态机账本。positions 以 6 位代码为键。

    pos 字段：qty 股数 / cost 买入开盘价（不含费）/ entry_date 入场交易日 /
              tiers 止盈臂A已触发档数 / peak 入场以来收盘峰值（None=-inf）。
    pending：上一收盘评估生成、待下一交易日开盘执行的计划：
      {"for_date": str|None, "sells": [{code,qty,reason}...], "buys": [{code,target}...]}
    """

    def __init__(self) -> None:
        self.cash: float = 0.0
        self.positions: dict[str, dict] = {}
        self.asof: str | None = None              # 最近已评估收盘日 YYYY-MM-DD
        self.pending: dict = {"for_date": None, "sells": [], "buys": []}

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return dict(
            version=SCHEMA_VERSION,
            params=dict(PARAMS),
            asof=self.asof,
            cash=round(self.cash, 4),
            positions={c: dict(qty=p["qty"], cost=p["cost"],
                               entry_date=p["entry_date"], tiers=p["tiers"],
                               peak=p["peak"])
                       for c, p in self.positions.items()},
            pending=self.pending,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Book":
        b = cls()
        b.cash = float(d["cash"])
        b.asof = d.get("asof")
        b.positions = {c: dict(qty=int(p["qty"]), cost=float(p["cost"]),
                               entry_date=str(p["entry_date"]),
                               tiers=int(p["tiers"]),
                               peak=(None if p["peak"] is None else float(p["peak"])))
                       for c, p in d.get("positions", {}).items()}
        b.pending = d.get("pending", {"for_date": None, "sells": [], "buys": []})
        return b

    def save(self, path: Path = STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path = STATE_PATH) -> "Book":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# 开盘执行（对齐 s1_run_sim L94-134：先卖后买；缺价/停牌 -> 该单作废，持仓保留，
# 当日收盘重新评估 = 顺延语义）
# ---------------------------------------------------------------------------
def apply_open(book: Book, date_str: str, opn_row: np.ndarray,
               idx_of: dict[str, int], fee_buy: float, fee_sell: float) -> list[dict]:
    trades: list[dict] = []
    pend = book.pending
    # ── 先卖 ──
    for s in pend.get("sells", []):
        c = s["code"]
        p = book.positions.get(c)
        i = idx_of.get(c)
        px = opn_row[i] if i is not None else np.nan
        if p is None or not np.isfinite(px) or px <= 0:
            continue                               # 停牌/缺价：卖单作废旧计划，收盘重评=顺延
        q = min(int(s["qty"]), p["qty"])
        proceeds = q * px * (1.0 - fee_sell)       # S1 口径：回款当日可用
        book.cash += proceeds
        p["qty"] -= q
        trades.append(dict(date=date_str, code=c, side="sell", qty=q,
                           px=float(px), reason=s["reason"]))
        if p["qty"] <= 0:
            del book.positions[c]
    # ── 后买 ──
    for b in pend.get("buys", []):
        c = b["code"]
        i = idx_of.get(c)
        px = opn_row[i] if i is not None else np.nan
        if not np.isfinite(px) or px <= 0:
            continue                               # 停牌/缺价：买单作废，空槽次日重评
        target = float(b["target"])
        q = int(min(target, book.cash) / (px * (1.0 + fee_buy)) / 100.0) * 100
        if q <= 0:
            continue
        book.cash -= q * px * (1.0 + fee_buy)
        book.positions[c] = dict(qty=q, cost=float(px), entry_date=date_str,
                                 tiers=0, peak=None)
        trades.append(dict(date=date_str, code=c, side="buy", qty=q,
                           px=float(px), reason="ROT_IN"))
    book.pending = {"for_date": None, "sells": [], "buys": []}
    return trades


# ---------------------------------------------------------------------------
# 收盘评估（对齐 s1_run_sim L136-215：更新峰值 → 兜底止损 → 止盈臂A →
# 排名调出（缓冲+最短持有）→ 换入计划）
# ---------------------------------------------------------------------------
def evaluate_close(book: Book, date_str: str, cls_row: np.ndarray,
                   rank_row: np.ndarray, idx_of: dict[str, int],
                   dates: pd.DatetimeIndex, n_slots: int, buffer_m: int,
                   min_hold: int) -> dict:
    """返回 plan dict：{sells:[{code,qty,reason}], buys:[{code,target}], nav, holds}。"""
    day_idx = dates.get_loc(pd.Timestamp(date_str))
    # ── 更新收盘峰值 ──
    for c, p in book.positions.items():
        i = idx_of.get(c)
        px = cls_row[i] if i is not None else np.nan
        if np.isfinite(px):
            p["peak"] = px if p["peak"] is None else max(p["peak"], px)
    # ── 净值 ──
    hv = 0.0
    for c, p in book.positions.items():
        i = idx_of.get(c)
        px = cls_row[i] if i is not None else np.nan
        if np.isfinite(px):
            hv += p["qty"] * px
    nav = book.cash + hv
    # ── 卖出评估 ──
    exits: dict[str, tuple[int, str]] = {}
    for c, p in list(book.positions.items()):
        i = idx_of.get(c)
        px = cls_row[i] if i is not None else np.nan
        if not np.isfinite(px):
            continue                               # 无收盘价：当日不评估止盈止损（排名调出照跑）
        ret = px / p["cost"] - 1.0
        # 1) 兜底止损（强制，不受最短持有约束）
        if ret <= STOP_LOSS:
            exits[c] = (p["qty"], "SL12")
            continue
        # 2) 止盈臂 A：5% 卖 1/3、8% 再卖 1/3、10% 清仓（单日跨档连触）
        q_sell = 0
        for k, (th, frac) in enumerate(TP_TIERS):
            if k < p["tiers"]:
                continue
            if ret >= th:
                p["tiers"] = k + 1
                if frac >= 1.0:
                    q_sell = p["qty"]
                    break
                q_sell += max(int(p["qty"] * frac / 100) * 100, 100)
        if q_sell > 0:
            exits[c] = (min(q_sell, p["qty"]), "TP_FIX")
    # 3) 排名调出（缓冲带 + 最短持有；已全仓止盈/止损者跳过）
    for c, p in book.positions.items():
        if c in exits and exits[c][0] >= p["qty"]:
            continue
        entry_idx = dates.get_loc(pd.Timestamp(p["entry_date"]))
        held = day_idx - entry_idx                 # 按交易日计（与 s1_run_sim 的 d-entry_d 同义）
        i = idx_of.get(c)
        rk = rank_row[i] if i is not None else np.inf
        if held >= min_hold and rk > buffer_m:
            exits[c] = (p["qty"], "ROT")
    sells = [dict(code=c, qty=q, reason=r) for c, (q, r) in exits.items()]
    # ── 换入计划 ──
    remaining = sum(1 for c, p in book.positions.items()
                    if not (c in exits and exits[c][0] >= p["qty"]))
    free_slots = n_slots - remaining
    buys: list[dict] = []
    if free_slots > 0:
        slot_target = nav / n_slots
        held_cols = set(book.positions.keys())
        cand = [c for c in (c for c, i in idx_of.items() if rank_row[i] <= n_slots)
                if c not in held_cols]
        cand.sort(key=lambda c: rank_row[idx_of[c]])
        for c in cand[:free_slots]:
            buys.append(dict(code=c, target=slot_target))
    # ── hold 明细（复盘用）──
    holds = []
    for c, p in book.positions.items():
        i = idx_of.get(c)
        rk = rank_row[i] if i is not None else np.inf
        px = cls_row[i] if i is not None else np.nan
        holds.append(dict(
            code=c, qty=p["qty"], entry_date=p["entry_date"], cost=p["cost"],
            rank=(None if not np.isfinite(rk) else int(rk)),
            last_close=(None if not np.isfinite(px) else float(px)),
            unrealized_ret=(None if not np.isfinite(px) else px / p["cost"] - 1.0),
            tiers=p["tiers"],
            exiting=(c in exits),
        ))
    return dict(sells=sells, buys=buys, nav=nav, holds=holds)


# ---------------------------------------------------------------------------
# 对拍 replay（硬验收）：同一窗口内 本状态机 vs s1_run_sim
# ---------------------------------------------------------------------------
def replay(start: str, end: str, verbose: bool = True) -> dict:
    data = load_data(PARAMS["score"])
    dates: pd.DatetimeIndex = data["dates"]
    opn, cls, score = data["opn"], data["cls"], data["score"]
    col_of: dict[int, str] = data["col_of"]
    idx_of = {c: i for i, c in col_of.items()}

    t0, t1 = pd.Timestamp(start), pd.Timestamp(end)
    i0 = dates.get_loc(dates[dates >= t0][0])
    i1 = dates.get_loc(dates[dates <= t1][-1])
    win_dates = dates[i0:i1 + 1]

    # ── 基准：s1_run_sim 同窗（数组切片；rank 行内计算不受切片影响）──
    ref = s1_run_sim("parity_ref", PARAMS["tp_arm"], score[i0:i1 + 1],
                     opn[i0:i1 + 1], cls[i0:i1 + 1], col_of, win_dates,
                     n_slots=PARAMS["n"], buffer_m=PARAMS["m"],
                     min_hold=PARAMS["min_hold"], proceeds_lag=0)

    # ── 被测：本状态机逐日前向（空仓起步）──
    book = Book()
    book.cash = INIT_CASH
    my_trades: list[dict] = []
    for d in range(i0, i1 + 1):
        date_str = str(dates[d].date())
        # 开盘执行昨日计划（首日 pending 为空 = bootstrap 空仓）
        my_trades += apply_open(book, date_str, opn[d], idx_of,
                                fee_buy=SIM_FEE, fee_sell=SIM_FEE)
        # 收盘评估生成次日计划（末日计划不落交易，与 sim 的 d+1<n_days 一致）
        if d < i1:
            plan = evaluate_close(book, date_str, cls[d], rank_of_row(score[d]),
                                  idx_of, dates, PARAMS["n"], PARAMS["m"],
                                  PARAMS["min_hold"])
            nxt = str(dates[d + 1].date())
            book.pending = dict(for_date=nxt, sells=plan["sells"],
                                buys=[dict(code=b["code"], target=b["target"])
                                      for b in plan["buys"]])

    ref_tr = ref["trades"]
    ref_keys = {(str(r.date.date()), r.code, r.side): (int(r.qty), float(r.px))
                for r in ref_tr.itertuples()}
    my_keys = {(t["date"], t["code"], t["side"]): (t["qty"], t["px"])
               for t in my_trades}

    missing_in_mine = sorted(ref_keys.keys() - my_keys.keys())
    extra_in_mine = sorted(my_keys.keys() - ref_keys.keys())
    common = ref_keys.keys() & my_keys.keys()
    qty_mismatch = [k for k in sorted(common)
                    if ref_keys[k][0] != my_keys[k][0]]
    px_diffs = [abs(ref_keys[k][1] - my_keys[k][1]) / ref_keys[k][1]
                for k in common if ref_keys[k][1] > 0]

    result = dict(
        window=[str(win_dates[0].date()), str(win_dates[-1].date())],
        n_days=len(win_dates),
        params=dict(PARAMS),
        ref=dict(n_trades=len(ref_keys),
                 final_nav=float(ref["nav"].iloc[-1]),
                 ann_ret=None, sharpe=None),
        mine=dict(n_trades=len(my_keys),
                  final_nav=float(book.cash + sum(
                      p["qty"] * cls[i1, idx_of[c]]
                      for c, p in book.positions.items()
                      if np.isfinite(cls[i1, idx_of[c]])))),
        key_match_pct=(100.0 * len(common) / max(len(ref_keys), 1)),
        n_missing_in_mine=len(missing_in_mine),
        n_extra_in_mine=len(extra_in_mine),
        n_qty_mismatch=len(qty_mismatch),
        max_rel_px_diff=(max(px_diffs) if px_diffs else 0.0),
        missing_in_mine=[list(map(str, k)) for k in missing_in_mine[:20]],
        extra_in_mine=[list(map(str, k)) for k in extra_in_mine[:20]],
        qty_mismatch=[[*map(str, k), ref_keys[k][0], my_keys[k][0]]
                      for k in qty_mismatch[:20]],
        passed=(len(missing_in_mine) == 0 and len(extra_in_mine) == 0),
    )
    if verbose:
        print(f"[replay] 窗口 {result['window'][0]}~{result['window'][1]} "
              f"({result['n_days']} 交易日)")
        print(f"[replay] 基准 s1_run_sim : trades={result['ref']['n_trades']} "
              f"final_nav={result['ref']['final_nav']:.2f}")
        print(f"[replay] 本状态机        : trades={result['mine']['n_trades']} "
              f"final_nav={result['mine']['final_nav']:.2f}")
        print(f"[replay] (date,code,side) 一致率: {result['key_match_pct']:.4f}%  "
              f"missing={result['n_missing_in_mine']} extra={result['n_extra_in_mine']} "
              f"qty_mismatch={result['n_qty_mismatch']} "
              f"max_px_reldiff={result['max_rel_px_diff']:.2e}")
        print(f"[replay] 换入/换出日期与标的 100% 一致: "
              f"{'PASS' if result['passed'] else 'FAIL'}")
    return result


# ---------------------------------------------------------------------------
# 生产计划生成
# ---------------------------------------------------------------------------
def gm_symbol(code: str) -> str:
    if code.startswith("6"):
        return f"SHSE.{code}"
    if code[0] in "03":
        return f"SZSE.{code}"
    if code[0] in "48":
        return f"BJSE.{code}"
    return code


def _limit_risk(code: str, pct: float | None) -> str | None:
    """启发式涨跌停风险标记（无涨跌停价数据，按板块幅度阈值估；权威判定在执行器）。"""
    if pct is None or not np.isfinite(pct):
        return None
    th = 0.195 if code.startswith(("30", "68")) else 0.295 if code[0] in "48" else 0.098
    if pct >= th:
        return "up"
    if pct <= -th:
        return "down"
    return None


def generate_picks(trade_date: str, bootstrap: bool = False,
                   cash0: float = INIT_CASH, fee_buy: float = FEE_BUY_STOCK,
                   fee_sell: float = FEE_SELL_STOCK) -> tuple[dict | None, int]:
    """生成 trade_date 开盘执行计划。返回 (picks_dict|None, exit_code)。"""
    data = load_data(PARAMS["score"])
    dates: pd.DatetimeIndex = data["dates"]
    opn, cls, score = data["opn"], data["cls"], data["score"]
    col_of: dict[int, str] = data["col_of"]
    idx_of = {c: i for i, c in col_of.items()}

    td = pd.Timestamp(trade_date)
    prior = dates[dates < td]
    if len(prior) == 0:
        print(f"[FAIL-CLOSED] 数据中不存在 {trade_date} 之前的交易日分数，不出名单。")
        return None, 2
    eval_ts = prior[-1]                            # 信号收盘日 = 数据内最近一个 <D 的交易日
    eval_date = str(eval_ts.date())

    # ── fail-closed：信号日分数全缺 ──
    d_eval = dates.get_loc(eval_ts)
    if not np.isfinite(score[d_eval]).any():
        print(f"[FAIL-CLOSED] 信号日 {eval_date} 分数全为 NaN（数据缺口），"
              f"不出名单并报警。请先跑 W1 数据刷新。")
        return None, 2

    # ── 载入/初始化账本 ──
    warnings: list[str] = []
    if bootstrap:
        book = Book()
        book.cash = float(cash0)
        book.asof = eval_date                      # 直接锚定信号日，不回放历史
    else:
        if not STATE_PATH.exists():
            print(f"[FAIL-CLOSED] 状态文件不存在：{STATE_PATH}。"
                  f"首日运行请加 --bootstrap。")
            return None, 2
        book = Book.load()
        if book.asof is None:
            print("[FAIL-CLOSED] 状态文件 asof 为空，请用 --bootstrap 重建。")
            return None, 2
        if pd.Timestamp(book.asof) >= eval_ts:
            if book.pending.get("for_date") == trade_date:
                print(f"[idempotent] {trade_date} 计划已生成过，按账本 pending 重发。")
                picks = _emit_picks(book, trade_date, eval_date, book.pending,
                                    data, idx_of, fee_buy, fee_sell,
                                    warnings, bootstrap=False)
                return picks, 0
            print(f"[FAIL-CLOSED] 账本 asof={book.asof} 不早于信号日 {eval_date} "
                  f"且 pending 不属于 {trade_date}，状态与数据不一致，人工核查。")
            return None, 2

    # ── 追账：asof < t <= eval_date 的每个交易日，开盘成交 pending → 收盘重评 ──
    if not bootstrap:
        catch = dates[(dates > pd.Timestamp(book.asof)) & (dates <= eval_ts)]
        for t in catch:
            d = dates.get_loc(t)
            ts_str = str(t.date())
            if book.pending.get("sells") or book.pending.get("buys"):
                if book.pending.get("for_date") not in (None, ts_str):
                    warnings.append(
                        f"pending 标签 {book.pending.get('for_date')} 与实际补账日 "
                        f"{ts_str} 不一致，按数据内下一交易日成交。")
                apply_open(book, ts_str, opn[d], idx_of, fee_buy, fee_sell)
            plan = evaluate_close(book, ts_str, cls[d], rank_of_row(score[d]),
                                  idx_of, dates, PARAMS["n"], PARAMS["m"],
                                  PARAMS["min_hold"])
            book.pending = dict(for_date=None, sells=plan["sells"],
                                buys=[dict(code=b["code"], target=b["target"])
                                      for b in plan["buys"]])
            book.asof = ts_str
    else:
        # bootstrap：空仓已在信号日锚定，仅做信号日收盘评估
        plan = evaluate_close(book, eval_date, cls[d_eval],
                              rank_of_row(score[d_eval]), idx_of, dates,
                              PARAMS["n"], PARAMS["m"], PARAMS["min_hold"])
        book.pending = dict(for_date=None, sells=plan["sells"],
                            buys=[dict(code=b["code"], target=b["target"])
                                  for b in plan["buys"]])

    book.pending["for_date"] = trade_date
    picks = _emit_picks(book, trade_date, eval_date, book.pending,
                        data, idx_of, fee_buy, fee_sell, warnings,
                        bootstrap=bootstrap)
    book.save()
    return picks, 0


def _emit_picks(book: Book, trade_date: str, eval_date: str, pending: dict,
                data: dict, idx_of: dict[str, int], fee_buy: float,
                fee_sell: float, warnings: list[str], bootstrap: bool) -> dict:
    dates: pd.DatetimeIndex = data["dates"]
    cls, score = data["cls"], data["score"]
    d_eval = dates.get_loc(pd.Timestamp(eval_date))
    rk = rank_of_row(score[d_eval])
    prev_close = cls[d_eval - 1] if d_eval >= 1 else np.full(cls.shape[1], np.nan)

    def ref_px(c: str) -> float | None:
        i = idx_of.get(c)
        v = cls[d_eval, i] if i is not None else np.nan
        return float(v) if np.isfinite(v) else None

    def pct_chg(c: str) -> float | None:
        i = idx_of.get(c)
        if i is None:
            return None
        a, b = cls[d_eval, i], prev_close[i]
        return float(a / b - 1.0) if np.isfinite(a) and np.isfinite(b) and b > 0 else None

    # 净值估计（信号日收盘口径）
    nav = book.cash
    for c, p in book.positions.items():
        i = idx_of.get(c)
        px = cls[d_eval, i] if i is not None else np.nan
        if np.isfinite(px):
            nav += p["qty"] * px

    sells = []
    for s in pending.get("sells", []):
        c = s["code"]
        rp = ref_px(c)
        if rp is None:
            warnings.append(f"{c} 信号日无价格（疑似停牌），卖单开盘可能无法成交，"
                            f"未成交则次日自动重评顺延。")
        sells.append(dict(symbol=c, gm_symbol=gm_symbol(c), qty=int(s["qty"]),
                          reason=s["reason"], ref_price=rp,
                          limit_risk=_limit_risk(c, pct_chg(c))))
    buys = []
    for b in pending.get("buys", []):
        c = b["code"]
        rp = ref_px(c)
        i = idx_of.get(c)
        q_est = (int(min(b["target"], nav) / (rp * (1.0 + fee_buy)) / 100.0) * 100
                 if rp else 0)
        if rp is None:
            warnings.append(f"{c} 信号日无价格（疑似停牌），买单可能无法成交。")
        buys.append(dict(symbol=c, gm_symbol=gm_symbol(c),
                         target_weight=round(1.0 / PARAMS["n"], 6),
                         target_value=round(float(b["target"]), 2),
                         qty_estimate=q_est, ref_price=rp,
                         reason="ROT_IN",
                         rank=(None if i is None or not np.isfinite(rk[i]) else int(rk[i])),
                         score=(None if i is None or not np.isfinite(score[d_eval, i])
                                else float(score[d_eval, i])),
                         limit_risk=_limit_risk(c, pct_chg(c))))
    holds = []
    for c, p in book.positions.items():
        i = idx_of.get(c)
        exiting = any(s["code"] == c for s in pending.get("sells", []))
        holds.append(dict(
            symbol=c, gm_symbol=gm_symbol(c), qty=p["qty"],
            entry_date=p["entry_date"], cost=p["cost"],
            rank=(None if i is None or not np.isfinite(rk[i]) else int(rk[i])),
            score=(None if i is None or not np.isfinite(score[d_eval, i])
                   else float(score[d_eval, i])),
            unrealized_ret=(None if ref_px(c) is None else ref_px(c) / p["cost"] - 1.0),
            exiting=exiting))

    picks = dict(
        schema_version=SCHEMA_VERSION,
        date=trade_date,
        eval_date=eval_date,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        status="OK",
        sells=sells, buys=buys, holds=holds,
        meta=dict(
            nav_estimate=round(float(nav), 2),
            cash=round(float(book.cash), 2),
            bootstrap=bool(bootstrap),
            params=dict(PARAMS),
            fee=dict(buy=fee_buy, sell=fee_sell, source="core/cost_model.py"),
            slot_weight=round(1.0 / PARAMS["n"], 6),
            data_last_date=str(dates[-1].date()),
            warnings=warnings,
            notes=[
                "开盘成交：先卖后买，卖出回款当日可用于买入。",
                "buys.qty_estimate 以信号日收盘价估算；执行器须在开盘用实际开盘价按 "
                "q=int(min(target_value,可用现金)/(px*(1+fee_buy))/100)*100 重算。",
                "停牌（无开盘价）单自动作废，持仓保留，次日收盘重新评估=顺延。",
                "limit_risk 为按板块幅度阈值的启发式标记；涨跌停权威判定在执行器，"
                "堵单不成交则次日自动重评。",
            ],
        ),
    )
    PICKS_DIR.mkdir(parents=True, exist_ok=True)
    path = PICKS_DIR / f"picks_{trade_date}.json"
    path.write_text(json.dumps(picks, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[s1_daily] picks -> {path}")
    print(f"[s1_daily] 信号日={eval_date} nav≈{nav:,.0f} 现金={book.cash:,.0f} "
          f"卖{len(sells)} 买{len(buys)} 持{len(holds)}")
    for s in sells:
        print(f"  SELL {s['symbol']} x{s['qty']}  {s['reason']}")
    for b in buys:
        print(f"  BUY  {b['symbol']} rank={b['rank']} target={b['target_value']:,.0f}")
    for w in warnings:
        print(f"  [warn] {w}")
    return picks


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def show() -> int:
    if not STATE_PATH.exists():
        print(f"状态文件不存在：{STATE_PATH}")
        return 1
    book = Book.load()
    d = book.to_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="S1 每日选股输出器（N4/M8/H1/TP-A）")
    ap.add_argument("--date", type=str, default=None,
                    help="生成该交易日（YYYY-MM-DD）开盘执行计划")
    ap.add_argument("--bootstrap", action="store_true",
                    help="空仓起步：信号日直接买 rank Top-N（首日）")
    ap.add_argument("--cash", type=float, default=INIT_CASH,
                    help="bootstrap 初始资金（默认 1,000,000）")
    ap.add_argument("--show", action="store_true", help="打印当前账本后退出")
    ap.add_argument("--replay", action="store_true",
                    help="硬验收：与 s1_run_sim 同窗逐笔对拍")
    ap.add_argument("--start", type=str, default="2024-06-03")
    ap.add_argument("--end", type=str, default="2026-09-17")
    args = ap.parse_args()

    if args.show:
        sys.exit(show())

    if args.replay:
        res = replay(args.start, args.end)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        out = STATE_DIR / f"replay_parity_{res['window'][0]}_{res['window'][1]}.json"
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[replay] 对拍报告 -> {out}")
        sys.exit(0 if res["passed"] else 3)

    if args.date:
        picks, code = generate_picks(args.date, bootstrap=args.bootstrap,
                                     cash0=args.cash)
        sys.exit(code)

    ap.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
