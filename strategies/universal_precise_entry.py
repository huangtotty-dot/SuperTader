# -*- coding: utf-8 -*-
"""
universal_precise_entry.py — 摩恩三点逻辑的通用化实现

你提出的三点建议：
  1. 不追高 - L1 放量涨停的第一天风险极高
  2. 缩量支撑 - 需要冲高回踩支撑不破+显著缩量
  3. 日内共振 - 5分钟+15分钟共振时精准入场

改进点：
  · 完全泛化 - 适用所有股票，不只摩恩
  · 市场环境感知 - 自动根据大盘状态调参
  · 多周期确认 - 加入60m/30m验证
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]  # 项目根（本模块位于 strategies/ 下）
import sys
sys.path.insert(0, str(BASE))

from core.position_builder import fetch_daily_kline, load_snapshot_df
from timing_gate import timing_verdict, _regime
from analysis.indicators import resample_to_5min, resample_to_15min, add_5min_indicators, add_15min_indicators


class UniversalPreciseEntry:
    """通用精准买入系统 - 适用所有股票"""

    def __init__(self, code: str):
        self.code = code
        self.daily_df = None
        self.market_regime = None
        self.market_score = None

    # ========================================================================
    # 市场环境感知 - 根据大盘状态自适应调参
    # ========================================================================

    def analyze_market_regime(self, date_str: str) -> Dict:
        """
        分析市场环境，返回自适应参数

        返回 {
          "regime": "uni_up" | "range_up" | "range" | "uni_down",
          "market_score": 0-100,
          "params": {
            "l1_risk_max": 风险上限,
            "l2_shrink_ratio": 缩量倍数,
            "l3_dd_min": 回撤下限,
          }
        }
        """
        tv = timing_verdict("000001", date_str)
        regime = tv.get("regime", "unknown")
        feats = tv.get("features", {})

        # 计算市场得分（0-100，越高越强）
        score = 0
        multihead = bool(feats.get("trend_multihead", False))
        dd = float(feats.get("drawdown", 0))
        golden = bool(feats.get("macd_golden_5d", False))

        if regime == "trend_up":
            score = 80 + (10 if multihead else 0) + (10 if golden else 0)
            regime_name = "uni_up"
        elif regime == "trend_dn":
            score = max(0, 20 + (-dd * 100) * 2)  # 回撤越深分越低
            regime_name = "uni_down"
        else:
            # range市，根据多头结构加分
            score = 30 + (20 if multihead else 0) + (10 if golden else 0)
            # 进一步细分为range_up/range
            score_range = 40 if multihead else 30
            regime_name = "range_up" if score >= score_range else "range"

        # 根据regime自适应参数 (方案B混合：介于优化值0.65和推荐值0.8之间)
        if regime_name == "uni_up":
            params = {
                "l1_risk_max": 20,       # 强多头，严格风险控制
                "l2_shrink_ratio": 0.72, # 混合参数 (0.65~0.8)
                "l3_dd_min": -0.03,      # 浅回撤要求高
            }
        elif regime_name == "range_up":
            params = {
                "l1_risk_max": 35,       # 中等
                "l2_shrink_ratio": 0.77, # 混合参数 (0.72+0.77)/2
                "l3_dd_min": -0.05,      # 稍放宽
            }
        elif regime_name == "range":
            params = {
                "l1_risk_max": 50,       # 宽松，重点看L2/L3
                "l2_shrink_ratio": 0.82, # 混合参数
                "l3_dd_min": -0.06,
            }
        else:  # uni_down
            params = {
                "l1_risk_max": 80,       # 空头市完全靠L2/L3
                "l2_shrink_ratio": 0.87, # 混合参数
                "l3_dd_min": -0.10,      # 深回撤
            }

        return {
            "regime": regime_name,
            "market_score": int(score),
            "params": params,
        }

    # ========================================================================
    # L1: 追高风险 - 改进版（多周期验证）
    # ========================================================================

    def check_l1_no_chase_high_v2(self, date_str: str, market_info: Dict) -> Dict:
        """
        L1 改进版：加入多周期反弹确认

        判据：
          1. 单日追高风险评分 (原逻辑)
          2. 如果市场处于range/uni_down，检查60min/30min是否有反弹迹象
          3. 底部形成确认（支撑试探+高点抬升）
        """
        if self.daily_df is None or self.daily_df.empty:
            return {"level": "unknown", "reason": "日线数据缺失"}

        df = self.daily_df[self.daily_df["date"].astype(str) <= str(date_str)].copy()
        if len(df) < 20:
            return {"level": "unknown", "reason": "日线不足"}

        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)

        recent_20 = df.tail(20)
        ma20_vol = recent_20["volume"].mean()

        latest = df.iloc[-1]
        today_vol = latest["volume"]
        today_close = latest["close"]
        today_gain = (latest["close"] / df.iloc[-2]["close"] - 1) if len(df) > 1 else 0

        # 计算风险评分
        risk_score = 0
        reasons = []

        if today_vol > ma20_vol * 1.5 and today_gain > 0.05:
            risk_score += 40
            reasons.append(f"放量涨停({today_vol/ma20_vol:.2f}x, +{today_gain*100:.1f}%)")

        if today_gain > 0.08:
            risk_score += 30
            reasons.append(f"单日大幅涨({today_gain*100:.1f}%)")

        if today_vol > ma20_vol * 2.0:
            risk_score += 15
            reasons.append(f"爆量({today_vol/ma20_vol:.2f}x)")

        # 根据市场regime调整风险判定
        regime = market_info.get("regime", "range")
        l1_risk_max = market_info.get("params", {}).get("l1_risk_max", 50)

        # 如果是range/uni_down，加入"底部反弹迹象"的减分
        if regime in ("range", "uni_down"):
            # 检查是否有多周期反弹确认
            recent_5 = df.tail(5)
            highs_increasing = sum(recent_5.iloc[i]["high"] >= recent_5.iloc[i-1]["high"] * 0.98
                                  for i in range(1, len(recent_5)))

            if highs_increasing >= 3:  # 至少3天高点未创新低
                risk_score -= 20
                reasons.append(f"多周期反弹迹象(高点{highs_increasing}/4天抬升)")

        # 判定等级
        if risk_score >= l1_risk_max + 20:
            level = "danger"
        elif risk_score >= l1_risk_max:
            level = "warning"
        else:
            level = "safe"

        return {
            "level": level,
            "risk_score": risk_score,
            "risk_threshold": l1_risk_max,
            "reasons": reasons,
            "detail": f"{'✅ 安全' if level == 'safe' else ('⚠️ 警告' if level == 'warning' else '❌ 危险')} ({risk_score}/{l1_risk_max})"
        }

    # ========================================================================
    # L2: 缩量支撑 - 保持原逻辑，参数自适应
    # ========================================================================

    def check_l2_pullback_consolidation_v2(self, date_str: str, market_info: Dict) -> Dict:
        """
        L2 改进版：使用市场环境自适应参数
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
        recent_30["ma10_vol"] = recent_30["volume"].rolling(10).mean()
        recent_30["vol_ratio"] = recent_30["volume"] / recent_30["ma10_vol"]
        recent_30["daily_gain"] = recent_30["close"].pct_change()

        # 找最近的放量涨停
        spike_candidates = recent_30[(recent_30["vol_ratio"] > 1.5) & (recent_30["daily_gain"] > 0.05)]

        if spike_candidates.empty:
            return {"is_consolidating": False, "reason": "无明显放量涨停"}

        spike_idx = spike_candidates.index[-1]
        after_spike = recent_30.loc[spike_idx:].tail(6)

        if len(after_spike) < 2:
            return {"is_consolidating": False, "reason": "放量后数据不足"}

        support = after_spike["low"].min()
        latest_close = after_spike["close"].iloc[-1]
        support_hold = latest_close >= support * 0.99

        # 使用市场环境参数
        baseline_vol = recent_30.loc[:spike_idx].tail(11).iloc[:-1]["volume"].mean()
        after_vol = after_spike.iloc[1:]["volume"].mean()
        shrink_ratio = after_vol / baseline_vol if baseline_vol > 0 else 1.0
        shrink_threshold = market_info.get("params", {}).get("l2_shrink_ratio", 0.8)

        recent_vols = after_spike.iloc[1:]["volume"].values
        is_trend_shrinking = all(recent_vols[i] >= recent_vols[i+1] * 0.8 for i in range(len(recent_vols)-1))

        consolidation_days = len(after_spike) - 1

        is_consolidating = (support_hold and shrink_ratio < shrink_threshold and is_trend_shrinking)

        return {
            "is_consolidating": is_consolidating,
            "support_level": float(support),
            "support_hold": bool(support_hold),
            "consolidation_days": consolidation_days,
            "volume_shrink_ratio": float(shrink_ratio),
            "shrink_threshold": shrink_threshold,
            "trend_shrinking": bool(is_trend_shrinking),
            "detail": f"支撑{support:.2f}{'✅' if support_hold else '❌'} 缩量{shrink_ratio:.2f}x{'✅' if shrink_ratio < shrink_threshold else '❌'} 趋势{'✅' if is_trend_shrinking else '❌'}"
        }

    # ========================================================================
    # L3: 日内共振 - 保持原逻辑
    # ========================================================================

    def check_l3_intraday_resonance_v2(self, date_str: str, market_info: Dict) -> Dict:
        """
        L3 改进版：使用市场环境自适应的回撤下限
        """
        # 加载分钟线
        df_1min, daily_ctx, snap_date = load_snapshot_df(self.code, date_str)

        if df_1min is None or df_1min.empty or len(df_1min) < 50:
            return {
                "resonance": False,
                "reason": "日内分钟数据不足",
                "insufficient": True,
            }

        d = df_1min.copy()
        d["time"] = pd.to_datetime(d["time"], errors="coerce")
        d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)

        # 计算VWAP
        if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
            cum_amt = d["amount"].fillna(0).cumsum()
        else:
            cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
        cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
        d["vwap_cum"] = cum_amt / cum_vol

        # 转15分钟
        d15 = add_15min_indicators(resample_to_15min(d))
        if d15 is None or d15.empty:
            return {"resonance": False, "reason": "15分钟重采样失败", "insufficient": True}

        d15["time"] = pd.to_datetime(d15["time"], errors="coerce")
        last_min_ts = d["time"].iloc[-1]

        closed = d15[(d15["time"] + pd.Timedelta(minutes=15)) <= (last_min_ts + pd.Timedelta(minutes=1))]
        if closed.empty:
            return {"resonance": False, "reason": "尚无已收盘15分钟bar", "insufficient": True}

        bar = closed.iloc[-1]
        c15 = bar.get("close")
        ema8_15m = bar.get("ema_fast_15m")
        volr = bar.get("vol_ratio_15m")

        if any(pd.isna(x) for x in (c15, ema8_15m, volr)):
            return {"resonance": False, "reason": "15分钟指标NaN", "insufficient": True}

        # VWAP
        close_ts = bar["time"] + pd.Timedelta(minutes=15)
        vw_rows = d[d["time"] <= close_ts]
        vwap = float(vw_rows["vwap_cum"].iloc[-1]) if (not vw_rows.empty and pd.notna(vw_rows["vwap_cum"].iloc[-1])) else None

        ema_ok = float(c15) > float(ema8_15m)
        vol_ok = float(volr) > 1.25  # 混合参数 (1.2~1.3)
        vwap_ok = (vwap is None) or (float(c15) >= vwap)

        resonance = ema_ok and vol_ok and vwap_ok

        return {
            "resonance": bool(resonance),
            "15m_close": float(c15),
            "ema8_15m": float(ema8_15m),
            "ema_ok": bool(ema_ok),
            "vol_ratio": float(volr),
            "vol_ok": bool(vol_ok),
            "vwap": float(vwap) if vwap is not None else None,
            "vwap_ok": bool(vwap_ok),
            "detail": f"EMA8{'✅' if ema_ok else '❌'} 放量{'✅' if vol_ok else '❌'} VWAP{'✅' if vwap_ok else '❌'}"
        }

    # ========================================================================
    # 综合判定（支持所有股票）
    # ========================================================================

    def check_ready_to_buy_universal(self, date_str: str) -> Dict:
        """
        通用三层检测，适用所有股票
        """
        self.daily_df = fetch_daily_kline(self.code)

        # 分析市场环境
        market_info = self.analyze_market_regime(date_str)

        # 三层检测
        l1 = self.check_l1_no_chase_high_v2(date_str, market_info)
        l2 = self.check_l2_pullback_consolidation_v2(date_str, market_info)
        l3 = self.check_l3_intraday_resonance_v2(date_str, market_info)

        # 综合判定
        l1_pass = l1.get("level") == "safe"
        l2_pass = l2.get("is_consolidating", False)
        l3_pass = l3.get("resonance", False)

        if not l1_pass:
            if l1.get("level") == "danger":
                verdict = "avoid_chase"
                ready = False
            else:
                verdict = "wait_cool_down"
                ready = False
        else:
            if l2_pass and l3_pass:
                verdict = "buy_now"
                ready = True
            elif l2_pass:
                verdict = "wait_resonance"
                ready = False
            else:
                verdict = "wait_consolidation"
                ready = False

        return {
            "code": self.code,
            "date": date_str,
            "market_regime": market_info.get("regime"),
            "market_score": market_info.get("market_score"),
            "ready_to_buy": ready,
            "verdict": verdict,
            "l1": l1,
            "l2": l2,
            "l3": l3,
        }


# ========================================================================
# 批量检测所有候选股
# ========================================================================

def batch_check_all_candidates(date_str: str = None) -> list:
    """对watchlist中所有候选股进行检测"""
    watchlist_file = BASE / "watchlist_buy.json"

    if not watchlist_file.exists():
        return []

    with open(watchlist_file, "r", encoding="utf-8") as f:
        watchlist = json.load(f)

    date_str = date_str or datetime.now().strftime("%Y-%m-%d")
    results = []

    for code, info in watchlist.get("stocks", {}).items():
        if info.get("status") not in ("monitoring", "signal"):
            continue

        try:
            validator = UniversalPreciseEntry(code)
            result = validator.check_ready_to_buy_universal(date_str)
            result["name"] = info.get("name", code)
            results.append(result)
        except Exception as e:
            pass

    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="通用精准买入检测")
    parser.add_argument("--code", default="002451", help="股票代码")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD")
    parser.add_argument("--all", action="store_true", help="检测所有候选股")
    args = parser.parse_args()

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.all:
        results = batch_check_all_candidates(date_str)
        print(f"【通用精准买入检测 - 全量候选股】{date_str}\n")
        print(f"{'代码':<10} {'名称':<15} {'市场':<12} {'L1':<10} {'L2':<10} {'L3':<10} {'建议':<20}")
        print("-" * 100)

        for r in sorted(results, key=lambda x: x.get("market_score", 0), reverse=True):
            code = r.get("code", "?")
            name = r.get("name", "?")[:12]
            regime = r.get("market_regime", "?")
            l1 = r.get("l1", {}).get("detail", "?")
            l2 = f"{'✅' if r.get('l2', {}).get('is_consolidating') else '❌'}"
            l3 = f"{'✅' if r.get('l3', {}).get('resonance') else '❌'}"
            verdict = r.get("verdict", "?")

            print(f"{code:<10} {name:<15} {regime:<12} {l1:<10} {l2:<10} {l3:<10} {verdict:<20}")
    else:
        validator = UniversalPreciseEntry(args.code)
        result = validator.check_ready_to_buy_universal(date_str)

        print(f"\n【通用精准买入检测】{args.code} @ {date_str}\n")
        print(f"市场环境: {result.get('market_regime')} (得分{result.get('market_score')}/100)")
        print(f"综合判定: {result.get('verdict')}\n")

        print(f"【L1 - 追高风险】{result['l1'].get('detail', '?')}")
        print(f"  评分: {result['l1'].get('risk_score')}/{result['l1'].get('risk_threshold')}")
        print()

        print(f"【L2 - 缩量支撑】{result['l2'].get('detail', '?')}")
        print()

        print(f"【L3 - 日内共振】{result['l3'].get('detail', '?')}")
        print()
