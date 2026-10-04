# -*- coding: utf-8 -*-
"""30min 趋势判定 — 三层状态机（方案 §2）。

逐根已收盘 30min bar 执行：ADX(14) 闸门 → EMA20/60 方向 → 2 根确认 + Supertrend 退出。
输出状态 ∈ {BULL, BEAR, RANGE}，附 confidence（由回归 R² 判定）。

文档歧义的处置（写死在此、注释标明）：
  · adx_turning_down（§2.5 引用但未定义，且与 §2.3 滞回冲突）：**不实现单根拐头关闸**，
    关闸只认 adx < ADX_OFF，以保住 §2.3「18~22 灰区维持原状态」的滞回。
  · weight 与确认计数冲突（§2.5 与 §4.1）：**只有 weight == 1.0 的 bar 才计入并由其触发确认**
    （首根/末根/午休首根 均为 0.5，不计入），取「结构性降权」的简化解。
  · §4.4 缺口 24 根回滚：本期不实现（回滚逻辑留待 §7 回测阶段，避免半吊子事件日志）。
"""
import os
import sys
from dataclasses import dataclass

import numpy as np
import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)


@dataclass
class Trend30Config:
    adx_on: float = 22.0
    adx_off: float = 18.0
    confirm_bars: int = 2
    spread_dead: float = 0.0005     # EMA 粘合带 0.05%
    spread_flip: float = 0.001      # 翻转要求发散度 0.1%
    st_n: int = 10
    st_mult: float = 2.0
    vol_freeze_ratio: float = 2.0
    vol_freeze_bars: int = 24
    vol_thin_ratio: float = 0.7
    gap_watch_bars: int = 24
    gap_pct: float = 0.005
    reg_n: int = 20
    r2_min: float = 0.6
    er_pulse: float = 0.2           # ER<0.2 → 疑似单根脉冲，确认需求 +1
    min_bars: int = 60              # 低于此根数不判定（adapter 据此回退）


def _opposite(state: str) -> str:
    return "BEAR" if state == "BULL" else "BULL"


class Trend30StateMachine:
    def __init__(self, cfg: Trend30Config = None):
        self.cfg = cfg or Trend30Config()
        self.snapshot = {"state": "RANGE", "confidence": "low", "adx": None,
                         "bar_time": None, "n_bars": 0, "gate_ok": False,
                         "confirm_cnt": 0, "candidate": None}

    def _exit_broken(self, state: str, st_dir) -> bool:
        """Supertrend 反向破位（快速退出线）。BULL 需 st_dir=+1，BEAR 需 -1。"""
        try:
            d = float(st_dir)
        except (TypeError, ValueError):
            return False
        if np.isnan(d):
            return False
        return (state == "BULL" and d < 0) or (state == "BEAR" and d > 0)

    def run(self, df: pd.DataFrame) -> pd.DataFrame:
        cfg = self.cfg
        if df is None or df.empty:
            return df if df is not None else pd.DataFrame()
        df = df.copy().reset_index(drop=True)
        n = len(df)

        def _col(name, default=np.nan):
            return (df[name].to_numpy(dtype=float) if name in df.columns
                    else np.full(n, default, dtype=float))

        close_a = df["close"].to_numpy(dtype=float)
        _open = (df["open"].to_numpy(dtype=float) if "open" in df.columns else close_a)
        _close_prev = np.concatenate([[np.nan], close_a[:-1]]) if n else np.array([])
        adx_a, rising_a = _col("adx"), _col("adx_rising", 0.0)
        ema20_a, ema60_a, spread_a = _col("ema20"), _col("ema60"), _col("ema_spread")
        st_dir_a, atr_a, w_a = _col("st_dir", 1.0), _col("atr_ratio"), _col("weight", 1.0)
        lim_a, er_a, r2_a = _col("is_limit_locked", 0.0), _col("er10"), _col("r2")
        _vol = (df["volume"].to_numpy(dtype=float) if "volume" in df.columns else None)
        _volma = (df["volume"].rolling(20, min_periods=5).mean().to_numpy()
                  if "volume" in df.columns else None)
        states, cands, cnts, confs, gates, freezes = ([None] * n, [None] * n,
                                                      [0] * n, ["low"] * n, [False] * n, [0] * n)
        gap_act, gap_dir = [False] * n, [0] * n
        st, cand, cnt, freeze = "RANGE", None, 0, 0
        gap = None   # §4.4 活动缺口 {dir, ref, start, pre_state}
        for i in range(n):
            adx = adx_a[i]
            gates[i] = False
            if np.isnan(adx) or i < 2:
                states[i], cands[i], cnts[i], freezes[i] = st, cand, cnt, freeze
                confs[i] = confs[i - 1] if i > 0 else "low"
                continue
            if lim_a[i] != 0:
                # 涨跌停 bar：不计确认、不翻转，状态顺延
                states[i], cands[i], cnts[i], freezes[i] = st, cand, cnt, freeze
                confs[i] = confs[i - 1] if i > 0 else "low"
                continue

            # ── §4.4 缺口观察窗 + 假突破回滚 ──────────────────────────────
            _pc = _close_prev[i]
            if i >= 1 and _pc is not None and not np.isnan(_pc) and _pc > 0:
                _g = (_open[i] - _pc) / _pc
                if abs(_g) > cfg.gap_pct:
                    # 新缺口（覆盖旧活动缺口），记住翻转前状态供回滚
                    gap = {"dir": 1 if _g > 0 else -1, "ref": float(_pc),
                           "start": i, "pre_state": st}
            if gap is not None:
                _c = close_a[i]
                _d = gap["dir"]
                _filled = (not np.isnan(_c)) and ((_d > 0 and _c <= gap["ref"])
                                                  or (_d < 0 and _c >= gap["ref"]))
                if _filled and (i - gap["start"]) <= cfg.gap_watch_bars:
                    # 窗内快速回补 = 假突破 → 若曾因该方向翻转，退回 pre_state 并清确认
                    _want = "BULL" if _d > 0 else "BEAR"
                    if st == _want and st != gap["pre_state"]:
                        st, cnt, cand = gap["pre_state"], 0, None
                    gap = None
                elif (i - gap["start"]) > cfg.gap_watch_bars:
                    gap = None   # 窗满未回补 = 突破有效，事件结束
            gap_act[i] = gap is not None
            gap_dir[i] = gap["dir"] if gap else 0

            gate_ok = bool(adx > cfg.adx_on and rising_a[i] == 1.0)
            gates[i] = gate_ok
            # 滞回（§2.3）：只在 ADX 跌破 ADX_OFF 时关闸，18~22 灰区维持原状态。
            # （§2.4 另列「adx 拐头」为 gate_ok 消失触发，与 §2.3 的滞回说明冲突；
            #   按 §2.3「滞回是防抖动核心」取值，不实现单根拐头关闸。）
            gate_off = bool(adx < cfg.adx_off)

            spread = spread_a[i]
            bull_dir = bool(ema20_a[i] > ema60_a[i] and close_a[i] > ema20_a[i]
                            and (not np.isnan(spread)) and spread > cfg.spread_flip)
            bear_dir = bool(ema20_a[i] < ema60_a[i] and close_a[i] < ema20_a[i]
                            and (not np.isnan(spread)) and spread < -cfg.spread_flip)
            raw = "BULL" if bull_dir else ("BEAR" if bear_dir else None)

            # §4.4 缺口加速：窗内缺口方向与 raw 同向 → 确认需求 −1；缺口+放量(≥1.5×MA20vol)
            # + 同向 → 该根直计 1 根确认。
            gap_aligned = (gap is not None and raw is not None
                           and raw == ("BULL" if gap["dir"] > 0 else "BEAR"))
            gap_bonus = 0
            if (gap_aligned and _volma is not None and _vol is not None
                    and not np.isnan(_volma[i]) and _volma[i] > 0
                    and _vol[i] >= 1.5 * _volma[i]):
                gap_bonus = 1

            # 波动率突变 → 冻结（冻结期内只允许退出，不允许进/翻）
            rr = atr_a[i]
            if (not np.isnan(rr)) and rr > cfg.vol_freeze_ratio and freeze <= 0:
                freeze = cfg.vol_freeze_bars
            if freeze > 0:
                freeze -= 1
                if self._exit_broken(st, st_dir_a[i]):
                    st, cnt, cand = "RANGE", 0, None
                states[i], cands[i], cnts[i], freezes[i] = st, cand, cnt, freeze
                confs[i] = confs[i - 1] if i > 0 else "low"
                continue

            w = w_a[i]
            er = er_a[i]
            req = cfg.confirm_bars + (1 if (not np.isnan(er) and er < cfg.er_pulse) else 0)
            req = max(1, req - (1 if gap_aligned else 0))

            if st == "RANGE":
                if gate_ok and raw is not None:
                    cnt = cnt + 1 if raw == cand else 1
                    cnt += gap_bonus
                    cand = raw
                    if cnt >= req and w == 1.0:
                        st, cnt = raw, 0
                else:
                    cnt, cand = 0, None
            else:  # BULL / BEAR
                if self._exit_broken(st, st_dir_a[i]):
                    st, cnt = "RANGE", 0
                elif gate_off:
                    st, cnt = "RANGE", 0
                elif raw == _opposite(st):
                    if w == 1.0:
                        cnt += 1
                    if cnt >= cfg.confirm_bars:
                        st, cnt = _opposite(st), 0
                else:
                    cnt = 0

            r2 = r2_a[i]
            conf = "high" if (not np.isnan(r2) and r2 >= cfg.r2_min) else "low"
            states[i], cands[i], cnts[i], freezes[i], confs[i] = st, cand, cnt, freeze, conf

        out = df.copy()
        out["state"] = states
        out["candidate"] = cands
        out["confirm_cnt"] = cnts
        out["confidence"] = confs
        out["gate_ok"] = gates
        out["freeze_remaining"] = freezes
        out["gap_active"] = gap_act
        out["gap_dir"] = gap_dir
        last = out.iloc[n - 1] if n else None
        self.snapshot = {
            "state": st, "confidence": confs[-1] if n else "low",
            "adx": (None if last is None or pd.isna(last.get("adx")) else round(float(last["adx"]), 2)),
            "bar_time": (None if last is None else str(last.get("time"))),
            "n_bars": n, "gate_ok": gates[-1] if n else False,
            "confirm_cnt": cnts[-1] if n else 0, "candidate": cands[-1] if n else None,
        }
        return out

    def current(self) -> dict:
        return dict(self.snapshot)
