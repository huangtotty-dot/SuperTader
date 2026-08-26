# -*- coding: utf-8 -*-
"""
精准建仓买入框架 (precise_entry_framework.py)
根据用户观点构建的三层递进式买入条件

核心逻辑（用户提出的三层筛选）：
  L1. 不追高 — 放量涨停后要冷静，等待回踩与缩量确认
  L2. 缩量支撑 — 冲高回踩某重要支撑不破，相比前期平均量能显著缩量
  L3. 日内共振 — 当日5分钟+15分钟线形成买点共振，方可入场

用法：
  from precise_entry_framework import PreciseEntryValidator
  v = PreciseEntryValidator("002451")
  result = v.check_ready_to_buy("2026-08-25")
  print(result)
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple, Optional

import numpy as np
import pandas as pd

# 路径与基础数据
BASE = Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(BASE))

from core.position_builder import (
    fetch_daily_kline, load_snapshot_df,
    resample_to_5min, add_5min_indicators,
    resample_to_15min, add_15min_indicators
)


class PreciseEntryValidator:
    """精准买入验证器 — 三层递进式筛选"""

    def __init__(self, code: str):
        self.code = code
        self.daily_df = None
        self.intraday_1m = None

    def _load_data(self, date_str: str):
        """加载日线 + 分钟线数据"""
        self.daily_df = fetch_daily_kline(self.code)
        self.intraday_1m, _, _ = load_snapshot_df(self.code, date_str)

    # ========================================================================
    # L1. 不追高检测 — 放量涨停后的风险识别
    # ========================================================================

    def check_l1_no_chase_high(self, date_str: str) -> Dict:
        """
        L1 检测：识别放量涨停，评估追高风险

        返回 {
          "level": "safe" | "warning" | "danger",
          "reason": 描述,
          "checklist": {
            "recent_max_volume": 最近最大成交量,
            "is_volume_spike": 是否最近首次大幅放量,
            "recent_gain": 最近N天涨幅,
            "today_gain": 今天涨幅,
            "today_volume_ratio": 今日成交量倍数,
            "risk_level": 风险等级,
          }
        }
        """
        if self.daily_df is None or self.daily_df.empty:
            return {"level": "unknown", "reason": "日线数据缺失"}

        df = self.daily_df[self.daily_df["date"].astype(str) <= str(date_str)].copy()
        if len(df) < 20:
            return {"level": "unknown", "reason": "日线不足20根"}

        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        df["high"] = df["high"].astype(float)

        # 计算关键指标
        recent_20 = df.tail(20)
        ma20_vol = recent_20["volume"].mean()

        latest = df.iloc[-1]
        today_vol = latest["volume"]
        today_close = latest["close"]
        today_high = latest["high"]

        # 昨日数据
        if len(df) >= 2:
            yesterday_close = df.iloc[-2]["close"]
            today_gain = (today_close - yesterday_close) / yesterday_close
        else:
            today_gain = 0

        # 最近3天涨幅
        recent_3days = df.tail(3)
        three_day_gain = (recent_3days["close"].iloc[-1] - recent_3days["close"].iloc[0]) / recent_3days["close"].iloc[0]

        # 成交量倍数
        vol_ratio = today_vol / ma20_vol if ma20_vol > 0 else 0

        # 最近最大成交量（去掉今天）
        recent_max_vol_excl_today = recent_20.iloc[:-1]["volume"].max()
        is_first_huge_spike = today_vol > recent_max_vol_excl_today * 1.5

        # 判定风险等级
        checklist = {
            "recent_max_volume": float(recent_max_vol_excl_today),
            "today_volume": float(today_vol),
            "is_volume_spike": bool(is_first_huge_spike),
            "today_gain_pct": float(today_gain * 100),
            "three_day_gain_pct": float(three_day_gain * 100),
            "today_volume_ratio": float(vol_ratio),
            "ma20_volume": float(ma20_vol),
        }

        # 风险评估逻辑
        risk_score = 0
        reasons = []

        if is_first_huge_spike:
            risk_score += 40
            reasons.append(f"⚠️ 最近首次放大量({vol_ratio:.2f}x)")

        if today_gain > 0.08:  # 8%以上
            risk_score += 30
            reasons.append(f"⚠️ 单日大幅上涨({today_gain*100:.1f}%)")

        if three_day_gain > 0.15:  # 3天涨15%以上
            risk_score += 20
            reasons.append(f"⚠️ 短期快速拉升({three_day_gain*100:.1f}%)")

        if vol_ratio > 2.0:
            risk_score += 15
            reasons.append(f"⚠️ 成交量爆表({vol_ratio:.2f}x)")

        # 判定等级
        if risk_score >= 70:
            level = "danger"
            main_reason = "🔴 追高极高风险，强烈建议等待"
        elif risk_score >= 40:
            level = "warning"
            main_reason = "⚠️ 短期追高风险较高，建议观察"
        else:
            level = "safe"
            main_reason = "✅ 当前无明显追高风险"

        return {
            "level": level,
            "reason": main_reason,
            "risk_score": risk_score,
            "detail_reasons": reasons,
            "checklist": checklist,
        }

    # ========================================================================
    # L2. 缩量支撑检测 — 回踩确认与量能萎缩
    # ========================================================================

    def check_l2_pullback_consolidation(self, date_str: str) -> Dict:
        """
        L2 检测：检查是否出现"回踩支撑+缩量确认"的组合

        判据：
          1. 前期放量涨停后，是否出现回踩
          2. 回踩到达的支撑位是否站稳
          3. 相比放量前的平均成交量是否显著缩量（<0.8倍）
          4. 近3-5日成交量是否逐步萎缩（趋势缩量）

        返回 {
          "is_consolidating": bool,
          "support_level": float,
          "support_hold": bool,
          "consolidation_days": int,
          "volume_shrink_ratio": float,
          "trend_shrinking": bool,
        }
        """
        if self.daily_df is None or self.daily_df.empty:
            return {"is_consolidating": False, "reason": "日线数据缺失"}

        df = self.daily_df[self.daily_df["date"].astype(str) <= str(date_str)].copy()
        if len(df) < 30:
            return {"is_consolidating": False, "reason": "日线不足30根"}

        df["close"] = df["close"].astype(float)
        df["low"] = df["low"].astype(float)
        df["high"] = df["high"].astype(float)
        df["volume"] = df["volume"].astype(float)

        recent_30 = df.tail(30).copy()

        # 找出最近的放量涨停日（基准）
        recent_30["ma10_vol"] = recent_30["volume"].rolling(10).mean()
        recent_30["vol_ratio"] = recent_30["volume"] / recent_30["ma10_vol"]
        recent_30["daily_gain"] = recent_30["close"].pct_change()

        # 搜索"放量+上涨"的日子（倒序查找最近的）
        spike_candidates = recent_30[(recent_30["vol_ratio"] > 1.5) & (recent_30["daily_gain"] > 0.05)]

        if spike_candidates.empty:
            return {
                "is_consolidating": False,
                "reason": "最近无明显放量涨停",
                "checklist": {"found_spike": False}
            }

        # 最近一次放量涨停的位置
        spike_idx = spike_candidates.index[-1]
        spike_date = recent_30.loc[spike_idx, "date"]
        spike_high = recent_30.loc[spike_idx, "high"]
        spike_vol = recent_30.loc[spike_idx, "volume"]

        # 放量后的日子（取后续3-5天）
        after_spike = recent_30.loc[spike_idx:].tail(6)  # 包含当日+后5日
        if len(after_spike) < 2:
            return {
                "is_consolidating": False,
                "reason": "放量涨停后数据不足",
                "spike_date": spike_date,
            }

        # 支撑位 = 放量后这段时间的最低点
        support = after_spike["low"].min()
        latest_close = after_spike["close"].iloc[-1]
        support_hold = latest_close >= support * 0.99  # 允许1%的跌破

        # 缩量检测
        # 对标：放量前10天的平均成交量
        before_spike = recent_30.loc[:spike_idx].tail(11)
        baseline_vol = before_spike.iloc[:-1]["volume"].mean()  # 去掉最后一天（可能已经开始放量）

        # 放量后这段时间的平均成交量
        after_vol = after_spike.iloc[1:]["volume"].mean()  # 去掉放量当日
        shrink_ratio = after_vol / baseline_vol if baseline_vol > 0 else 1.0

        # 趋势缩量（后续5日逐步萎缩）
        recent_vols = after_spike.iloc[1:]["volume"].values  # 后5日
        is_trend_shrinking = all(recent_vols[i] >= recent_vols[i+1] * 0.8 for i in range(len(recent_vols)-1))

        consolidation_days = len(after_spike) - 1  # 不含放量当日

        checklist = {
            "spike_date": spike_date,
            "spike_high": float(spike_high),
            "spike_volume": float(spike_vol),
            "support_level": float(support),
            "latest_close": float(latest_close),
            "support_hold": bool(support_hold),
            "baseline_volume": float(baseline_vol),
            "after_avg_volume": float(after_vol),
            "shrink_ratio": float(shrink_ratio),
            "trend_shrinking": bool(is_trend_shrinking),
            "consolidation_days": consolidation_days,
        }

        # 综合判定
        is_consolidating = (support_hold and shrink_ratio < 0.8 and is_trend_shrinking)

        return {
            "is_consolidating": is_consolidating,
            "support_level": float(support),
            "support_hold": bool(support_hold),
            "consolidation_days": consolidation_days,
            "volume_shrink_ratio": float(shrink_ratio),
            "trend_shrinking": bool(is_trend_shrinking),
            "checklist": checklist,
        }

    # ========================================================================
    # L3. 日内共振检测 — 5分钟+15分钟线共振
    # ========================================================================

    def check_l3_intraday_resonance(self, date_str: str, vol_min: float = 1.2) -> Dict:
        """
        L3 检测：日内5分钟+15分钟线共振检测

        判据（参考 position_builder.check_intraday_confirm）：
          - 15m close > EMA8
          - 15m vol_ratio > vol_min
          - 15m close >= 当日VWAP

        返回 {
          "resonance": bool,
          "15m_above_ema8": bool,
          "15m_volume_confirm": bool,
          "15m_above_vwap": bool,
          "checklist": {...}
        }
        """
        if self.intraday_1m is None or self.intraday_1m.empty:
            return {
                "resonance": False,
                "reason": "日内分钟数据缺失",
                "insufficient": True,
            }

        d = self.intraday_1m.copy()
        d["time"] = pd.to_datetime(d["time"], errors="coerce")
        d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

        if len(d) < 50:
            return {
                "resonance": False,
                "reason": "日内分钟数据不足",
                "insufficient": True,
                "bars_count": len(d),
            }

        # 计算当日VWAP
        if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
            cum_amt = d["amount"].fillna(0).cumsum()
        else:
            cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
        cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
        d["vwap_cum"] = cum_amt / cum_vol

        # 转15分钟线
        d15 = add_15min_indicators(resample_to_15min(d))
        if d15 is None or d15.empty:
            return {
                "resonance": False,
                "reason": "15分钟重采样失败",
                "insufficient": True,
            }

        d15["time"] = pd.to_datetime(d15["time"], errors="coerce")
        last_min_ts = d["time"].iloc[-1]

        # 取最新一根【已收盘】15m bar
        closed = d15[(d15["time"] + pd.Timedelta(minutes=15)) <= (last_min_ts + pd.Timedelta(minutes=1))]
        if closed.empty:
            return {
                "resonance": False,
                "reason": "尚无已收盘15分钟bar",
                "insufficient": True,
            }

        bar = closed.iloc[-1]
        c15 = bar.get("close")
        ema8_15m = bar.get("ema_fast_15m")
        volr = bar.get("vol_ratio_15m")

        if any(pd.isna(x) for x in (c15, ema8_15m, volr)):
            return {
                "resonance": False,
                "reason": "15分钟指标NaN",
                "insufficient": True,
            }

        # VWAP确认
        close_ts = bar["time"] + pd.Timedelta(minutes=15)
        vw_rows = d[d["time"] <= close_ts]
        vwap = float(vw_rows["vwap_cum"].iloc[-1]) if (not vw_rows.empty and pd.notna(vw_rows["vwap_cum"].iloc[-1])) else None

        # 三项判据
        ema_ok = float(c15) > float(ema8_15m)
        vol_ok = float(volr) > vol_min
        vwap_ok = (vwap is None) or (float(c15) >= vwap)

        resonance = ema_ok and vol_ok and vwap_ok

        checklist = {
            "15m_close": float(c15),
            "ema8_15m": float(ema8_15m),
            "close_above_ema8": bool(ema_ok),
            "15m_vol_ratio": float(volr),
            "vol_ratio_above_min": bool(vol_ok),
            "vwap": float(vwap) if vwap is not None else None,
            "close_above_vwap": bool(vwap_ok),
        }

        return {
            "resonance": bool(resonance),
            "15m_above_ema8": bool(ema_ok),
            "15m_volume_confirm": bool(vol_ok),
            "15m_above_vwap": bool(vwap_ok),
            "checklist": checklist,
        }

    # ========================================================================
    # 综合判定
    # ========================================================================

    def check_ready_to_buy(self, date_str: str) -> Dict:
        """
        综合三层检测，给出"是否可以买入"的结论

        返回 {
          "ready_to_buy": bool,
          "verdict": "buy_now" | "wait_consolidation" | "avoid_chase" | "insufficient_data",
          "summary": 一句话总结,
          "l1": {...},
          "l2": {...},
          "l3": {...},
        }
        """
        self._load_data(date_str)

        l1 = self.check_l1_no_chase_high(date_str)
        l2 = self.check_l2_pullback_consolidation(date_str)
        l3 = self.check_l3_intraday_resonance(date_str)

        # 判定逻辑
        if l1["level"] == "danger":
            verdict = "avoid_chase"
            ready = False
            summary = f"❌ L1 追高风险过高，不建议买入"
        elif l1["level"] == "warning":
            if l2.get("is_consolidating"):
                if l3.get("resonance"):
                    verdict = "buy_now"
                    ready = True
                    summary = f"✅ 三层确认通过，可以买入（{l3['checklist']['15m_close']:.3f}）"
                else:
                    verdict = "wait_resonance"
                    ready = False
                    summary = f"⏳ L2通过但日内未共振，继续等待L3信号"
            else:
                verdict = "wait_consolidation"
                ready = False
                reason = []
                if not l2.get("support_hold"):
                    reason.append("支撑未站稳")
                if l2.get("volume_shrink_ratio", 1.0) >= 0.8:
                    reason.append("成交量未显著缩量")
                if not l2.get("trend_shrinking"):
                    reason.append("缩量趋势未确立")
                reason_str = "、".join(reason) if reason else "未满足L2条件"
                summary = f"⏳ L2未通过({reason_str})，继续等待"
        else:  # l1 safe
            if l2.get("is_consolidating") and l3.get("resonance"):
                verdict = "buy_now"
                ready = True
                summary = f"✅ 全绿！可以安心买入"
            elif l2.get("is_consolidating"):
                verdict = "wait_resonance"
                ready = False
                summary = f"⏳ L2通过，等待日内L3共振"
            else:
                verdict = "wait_consolidation"
                ready = False
                summary = f"⏳ 等待缩量巩固，未形成L2条件"

        return {
            "ready_to_buy": ready,
            "verdict": verdict,
            "summary": summary,
            "l1": l1,
            "l2": l2,
            "l3": l3,
        }


# ============================================================
# CLI
# ============================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="精准买入检测")
    parser.add_argument("--code", default="002451", help="股票代码")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    args = parser.parse_args()

    validator = PreciseEntryValidator(args.code)
    result = validator.check_ready_to_buy(args.date or datetime.now().strftime("%Y-%m-%d"))

    print("=" * 100)
    print(f"精准买入检测结果 {args.code} @ {args.date or 'today'}")
    print("=" * 100)
    print()
    print(f"【综合判定】{result['summary']}")
    print()
    print(f"【L1 - 追高风险检测】{result['l1'].get('reason', '无')}")
    if "risk_score" in result["l1"]:
        print(f"  风险评分: {result['l1']['risk_score']}/100")
        for reason in result["l1"].get("detail_reasons", []):
            print(f"  - {reason}")
    print()

    print(f"【L2 - 缩量支撑检测】")
    if result["l2"].get("is_consolidating"):
        print(f"  ✅ 巩固中 ({result['l2'].get('consolidation_days')}天)")
    else:
        print(f"  ❌ {result['l2'].get('reason', '未启动')}")
    if "checklist" in result["l2"]:
        print(f"  支撑位: {result['l2']['checklist'].get('support_level', '?'):.2f}")
        print(f"  缩量比: {result['l2']['checklist'].get('shrink_ratio', 1.0):.2f}x")
    print()

    print(f"【L3 - 日内共振检测】")
    if result["l3"].get("resonance"):
        print(f"  ✅ 共振通过")
    else:
        print(f"  ❌ {result['l3'].get('reason', '未共振')}")
    print()

    print("=" * 100)


if __name__ == "__main__":
    main()
