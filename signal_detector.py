# -*- coding: utf-8 -*-
"""
信号检测模块
"""
from typing import Tuple, List, Dict

import pandas as pd
import numpy as np

from config import (
    log, get_strategy_enabled, ENABLE_MA_NEAR_60, ENABLE_MA_NEAR_150,
    MA_NEAR_PCT, VERBOSE_SCAN_LOG
)

def detect_box_pattern(df: pd.DataFrame, current_close: float = None) -> Tuple[float, float, int, float]:
    """改进的箱体识别算法 - 优先返回最近且已被价格确认的箱体"""
    if len(df) < 20:
        return 0, 0, 0, 0

    best_box = None
    best_score = 0
    close_price = float(current_close) if current_close is not None else (float(df.iloc[-1]['close']) if 'close' in df.columns else 0.0)

    for period in [20, 30, 40, 50, 60, 80, 100]:
        if len(df) < period:
            continue

        recent = df.iloc[-period:]
        box_high = recent['high'].max()
        box_low = recent['low'].min()
        if box_low <= 0:
            continue

        box_width = box_high - box_low
        box_width_ratio = box_width / box_low
        box_days = sum(1 for i in range(len(recent))
                      if recent.iloc[i]['high'] <= box_high
                      and recent.iloc[i]['low'] >= box_low)

        if box_width_ratio <= 0.50 or box_days < 12:
            continue

        # 若当前价格已经突破当前周期箱顶，优先返回最短且成立的箱体
        if close_price > box_high * 0.98:
            return box_low, box_high, box_days, box_width_ratio

        touch_score = (close_price / box_high) if box_high > 0 and close_price > 0 else 0.0
        recency_score = 1.0 / period
        box_quality = (box_width_ratio * (box_days / period)) * (0.7 + 0.3 * touch_score) + recency_score

        if box_quality > best_score:
            best_score = box_quality
            best_box = (box_low, box_high, box_days, box_width_ratio)

    if best_box:
        return best_box
    return 0, 0, 0, 0

def detect_limit_up_board(df: pd.DataFrame, code: str = "") -> Tuple[bool, int]:
    """检测涨停板并判断几连板
    Returns: (is_limit_up, consecutive_days)
    """
    if df is None or df.empty or len(df) < 1:
        return False, 0

    try:
        df_check = df.copy()
        df_check['close'] = pd.to_numeric(df_check['close'], errors='coerce')
        df_check['open'] = pd.to_numeric(df_check['open'], errors='coerce')

        if df_check.empty or df_check['close'].isna().all():
            return False, 0

        # 检测今日是否涨停
        today = df_check.iloc[-1]
        today_close = float(today['close'])
        today_open = float(today['open'])

        if len(df_check) < 2 or pd.isna(today['close']):
            return False, 0

        yesterday = df_check.iloc[-2]
        yesterday_close = float(yesterday['close'])

        if yesterday_close <= 0:
            return False, 0

        today_pct_change = (today_close - yesterday_close) / yesterday_close * 100
        is_limit_up_today = today_pct_change >= 9.8

        if not is_limit_up_today:
            return False, 0

        # 计算连续涨停天数
        consecutive_days = 1
        for i in range(len(df_check) - 2, -1, -1):
            if i == 0:
                break
            prev_day = df_check.iloc[i]
            prev_prev_day = df_check.iloc[i - 1]

            prev_close = float(prev_day['close'])
            prev_prev_close = float(prev_prev_day['close'])

            if prev_prev_close <= 0:
                break

            pct = (prev_close - prev_prev_close) / prev_prev_close * 100
            if pct >= 9.8:
                consecutive_days += 1
            else:
                break

        return True, consecutive_days
    except Exception as e:
        if VERBOSE_SCAN_LOG:
            log.debug(f"涨停板检测异常 {code}: {str(e)}")
        return False, 0


def check_strategies(df: pd.DataFrame, enable_momentum_strategies: bool = True) -> Tuple[str, str]:
    try:
        # 动态读取策略开关（这样可以使用配置文件中的设置）
        ENABLE_STRATEGY_BREAKTHROUGH = get_strategy_enabled("BREAKTHROUGH")
        ENABLE_STRATEGY_MA_CLUSTER = get_strategy_enabled("MA_CLUSTER")
        ENABLE_STRATEGY_A_AREA = get_strategy_enabled("A_AREA")
        ENABLE_STRATEGY_B_AREA = get_strategy_enabled("B_AREA")
        ENABLE_STRATEGY_BOX_BOTTOM = get_strategy_enabled("BOX_BOTTOM")
        ENABLE_STRATEGY_BOX_TOP = get_strategy_enabled("BOX_TOP")
        ENABLE_STRATEGY_BOX_INTERNAL = get_strategy_enabled("BOX_INTERNAL")
        ENABLE_STRATEGY_HISTORY_BREAK = get_strategy_enabled("HISTORY_BREAK")
        ENABLE_STRATEGY_MILD_TREND = get_strategy_enabled("MILD_TREND")
        ENABLE_STRATEGY_MA_NEAR = get_strategy_enabled("MA_NEAR")

        if len(df) < 60: return None, ""

        df['ma10'] = df['close'].rolling(10).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma30'] = df['close'].rolling(30).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        df['ma150'] = df['close'].rolling(150).mean()

        df['vol_ma20_past'] = df['volume'].shift(1).rolling(20).mean()
        df['vol_ratio'] = df['volume'] / df['vol_ma20_past'].replace(0, 1)

        df['is_up'] = df['close'] > df['open']
        df['up_vol'] = np.where(df['is_up'], df['volume'], 0)
        df['down_vol'] = np.where(~df['is_up'], df['volume'], 0)
        df['power_ratio'] = df['up_vol'].rolling(20).sum() / (df['down_vol'].rolling(20).sum() + 1)

        today = df.iloc[-1]
        yest = df.iloc[-2]
        price = today['close']
        pct_change = (price - yest['close']) / yest['close']
        is_yang = today['close'] > today['open']

        past_15_close_max = df['close'].iloc[-16:-1].max()
        is_breaking_platform = price > past_15_close_max
        is_buyable = (0.02 < pct_change < 0.065)

        if ENABLE_STRATEGY_BREAKTHROUGH and enable_momentum_strategies and is_breaking_platform and is_buyable and is_yang and (today['vol_ratio'] > 1.25) and (today['power_ratio'] > 1.15):
            return "👑 突破先手", f"温和突破近15日平台！放量({today['vol_ratio']:.1f}倍)，资金底座扎实(阳线动能{today['power_ratio']:.2f})，抓主升起涨第一天。"

        ma60_is_down = today['ma60'] < df.iloc[-5]['ma60']
        if ma60_is_down and (price < today['ma60'] * 0.90): return None, ""

        ma10 = float(today['ma10']) if pd.notna(today['ma10']) else 0.0
        ma20 = float(today['ma20']) if pd.notna(today['ma20']) else 0.0
        ma30 = float(today['ma30']) if pd.notna(today['ma30']) else 0.0
        ma60 = float(today['ma60']) if pd.notna(today['ma60']) else 0.0
        ma_vals = [m for m in (ma10, ma20, ma30, ma60) if m > 0]
        ma_cluster_ratio = (max(ma_vals) - min(ma_vals)) / ma30 if ma_vals and ma30 > 0 else 999.0
        ma_cluster_center = sum(ma_vals) / len(ma_vals) if ma_vals else 0.0
        near_cluster_center = ma_cluster_center > 0 and abs(price - ma_cluster_center) / ma_cluster_center <= 0.04
        above_short_mas = (price > ma20) and (price > ma30)
        ma30_is_up = ma30 > float(df.iloc[-10]['ma30']) if pd.notna(df.iloc[-10]['ma30']) else False
        ma60_neutral_or_up = (ma60 > 0) and (ma60 >= float(df.iloc[-5]['ma60']) * 0.995 or price > ma60)
        clustered_ma = ma_cluster_ratio <= 0.06
        cluster_launch = clustered_ma and near_cluster_center and above_short_mas and ma30_is_up and ma60_neutral_or_up
        cluster_ignition = (0.005 < pct_change < 0.06) and (today['vol_ratio'] >= 0.98) and (today['power_ratio'] >= 0.95)

        if ENABLE_STRATEGY_MA_CLUSTER and enable_momentum_strategies and cluster_launch and cluster_ignition:
            return "🧲 均线粘合", f"短中期均线粘合({ma_cluster_ratio*100:.1f}%)，价格贴近均线中心，温和放量({today['vol_ratio']:.1f}倍)且阳线动能{today['power_ratio']:.2f}，等待粘合后转强。"

        base_clustered = abs(ma20 - ma30) / ma30 < 0.05 if ma30 > 0 else False
        ma10_turn_up = ma10 > float(yest['ma10']) if pd.notna(yest['ma10']) else False
        mild_ignition = (0.005 < pct_change < 0.06) and (today['vol_ratio'] > 1.05)

        if ENABLE_STRATEGY_A_AREA and enable_momentum_strategies and base_clustered and ma10_turn_up and mild_ignition and (price > ma20) and (today['power_ratio'] > 1.0):
            return "🚀 A区初显", f"均线底座打牢，今日温和放量({today['vol_ratio']:.1f}倍)初次点火，涨幅{pct_change*100:.1f}%，最佳的左侧转右侧潜伏点。"

        # === B区策略（低吸相关）优化 ===
        ma30_is_up = ma30 > float(df.iloc[-5]['ma30']) if pd.notna(df.iloc[-5]['ma30']) else False
        near_ma30 = abs(price - ma30) / ma30 < 0.05 if ma30 > 0 else False  # 放宽从0.04到0.05
        near_ma20 = abs(price - ma20) / ma20 < 0.05 if ma20 > 0 else False  # 放宽从0.04到0.05
        near_ma60 = abs(price - ma60) / ma60 < 0.05 if ma60 > 0 else False
        
        # 支撑位：至少靠近一条均线
        near_any_ma = near_ma20 or near_ma30 or near_ma60
        
        if ENABLE_STRATEGY_B_AREA and enable_momentum_strategies and ma30_is_up and near_any_ma:
            # 【B区起航】反包型：缩量洗盘后放量反包
            recent_shrink = df.iloc[-3]['vol_ratio'] < 0.8 or yest['vol_ratio'] < 0.8
            if recent_shrink and (pct_change > 0.005) and (today['vol_ratio'] > 1.05):
                return "🔥 B区起航", f"洗盘动作结束，今日温和放量({today['vol_ratio']:.1f}倍)反包，进攻线半空重新归位仰头。"
            
            # 【B区潜伏】缩量回踩型：放宽到vol_ratio<0.7，不再强制阴线（十字星也可）
            # 支持：1) 缩量阴线  2) 缩量十字星（实体很小）
            body_pct = abs(today['close'] - today['open']) / price  # K线实体比例
            is_doji = body_pct < 0.005  # 十字星（实体小于0.5%）
            is_shrinking = today['vol_ratio'] < 0.7  # 放宽从0.5到0.7
            is_near_support = near_ma20 or near_ma30 or near_ma60
            
            if is_shrinking and is_near_support and (not is_yang or is_doji):
                support = "MA20" if near_ma20 else ("MA30" if near_ma30 else "MA60")
                if is_doji:
                    return "⚖️ B区潜伏", f"中期趋势向上，回踩{support}缩量十字星({today['vol_ratio']:.2f}倍)，变盘信号，抛压枯竭。"
                else:
                    return "⚖️ B区潜伏", f"中期趋势向上，回踩{support}缩量阴线({today['vol_ratio']:.2f}倍)，抛压枯竭，密切关注明后天反转。"
        
        # === V13.0 策略：底部缩量（优化版）===
        near_ma20_loose = abs(price - today['ma20']) / today['ma20'] < 0.03 if today['ma20'] > 0 else False  # 放宽从0.02到0.03
        near_ma30_loose = abs(price - today['ma30']) / today['ma30'] < 0.10 if today['ma30'] > 0 else False  # 放宽从0.08到0.10
        near_ma60_loose = abs(price - today['ma60']) / today['ma60'] < 0.10 if today['ma60'] > 0 else False
        shrinking_volume = today['vol_ratio'] < 0.95  # 放宽从0.9到0.95
        ma30_uptrend = today['ma30'] > df.iloc[-10]['ma30']
        power_not_weak = today['power_ratio'] > 0.85  # 大幅放宽从1.12到0.85（只需不极度弱势）
        body_pct = abs(today['close'] - today['open']) / price
        
        if ENABLE_STRATEGY_BOX_BOTTOM and enable_momentum_strategies and (near_ma20_loose or near_ma30_loose or near_ma60_loose) and shrinking_volume and ma30_uptrend and power_not_weak:
            support = "MA20" if near_ma20_loose else ("MA30" if near_ma30_loose else "MA60")
            if body_pct < 0.005:
                return "💎 底部缩量", f"底部区域缩量十字星，{support}上升趋势，抛压枯竭，变盘信号。"
            else:
                return "💎 底部缩量", f"底部区域缩量整理，{support}上升趋势，资金底座不弱(动能{today['power_ratio']:.2f})，蓄势待发。"
        
        # === 新增：回踩低吸策略（V17.11）===
        # 当价格在上升趋势中，回调到MA20/MA30/MA60，且缩量企稳时给出低吸信号
        pullback_to_ma20 = (price < ma20 * 1.02) and (price > ma20 * 0.97) if ma20 > 0 else False
        pullback_to_ma30 = (price < ma30 * 1.02) and (price > ma30 * 0.97) if ma30 > 0 else False
        pullback_to_ma60 = (price < ma60 * 1.02) and (price > ma60 * 0.97) if ma60 > 0 else False
        
        # 确认上升趋势：MA30向上，且价格仍在MA60之上（或偏离不大）
        uptrend_confirmed = ma30_is_up and (price > ma60 * 0.90 if ma60 > 0 else True)
        
        # 缩量确认：近3日至少有一天缩量，且今日成交量不超过1.2倍
        recent_vol_shrink = (df.iloc[-3]['vol_ratio'] < 0.85) or (yest['vol_ratio'] < 0.85) or (today['vol_ratio'] < 0.85)
        vol_not_explosive = today['vol_ratio'] < 1.2
        
        # 价格没有暴跌：今日跌幅不超过3%
        not_crashing = pct_change > -0.03
        
        # 新增策略开关
        ENABLE_STRATEGY_PULLBACK_BUY = get_strategy_enabled("PULLBACK_BUY")
        
        if ENABLE_STRATEGY_PULLBACK_BUY and enable_momentum_strategies and uptrend_confirmed and (pullback_to_ma20 or pullback_to_ma30 or pullback_to_ma60) and recent_vol_shrink and vol_not_explosive and not_crashing:
            support = "MA20" if pullback_to_ma20 else ("MA30" if pullback_to_ma30 else "MA60")
            shrink_note = f"近3日缩量({df.iloc[-3]['vol_ratio']:.1f}倍)" if df.iloc[-3]['vol_ratio'] < 0.85 else f"昨日缩量({yest['vol_ratio']:.1f}倍)"
            return "🎯 回踩低吸", f"上升趋势确认，回调{support}获得支撑，{shrink_note}，今日跌幅{pct_change*100:.1f}%可控，低吸窗口。"
        
        # === 新增：箱体底部低吸（V17.11）===
        # 识别箱体后，当价格回踩箱体底部且缩量时给出低吸信号
        ENABLE_STRATEGY_BOX_BOTTOM_BUY = get_strategy_enabled("BOX_BOTTOM_BUY")
        box_source = df.iloc[:-1] if len(df) > 1 else df
        box_low, box_high, box_days, box_width_ratio = detect_box_pattern(box_source, current_close=price)
        
        if ENABLE_STRATEGY_BOX_BOTTOM_BUY and box_low > 0 and box_high > 0 and box_days >= 10 and box_width_ratio > 0.08:
            near_box_bottom = (price < box_low * 1.03) and (price > box_low * 0.97)
            box_bottom_shrink = today['vol_ratio'] < 0.85
            box_ma30_up = ma30 > float(df.iloc[-5]['ma30']) if pd.notna(df.iloc[-5]['ma30']) else False
            
            if near_box_bottom and box_bottom_shrink and box_ma30_up and (pct_change > -0.03):
                return "🎯 箱底低吸", f"箱体({box_low:.2f}-{box_high:.2f})底部缩量回踩，{box_days}天整理，抛压枯竭，低吸良机。"
        
        # V15.0 策略：箱体突破（先看前一日箱体，再判断今天是否突破）
        box_source = df.iloc[:-1] if len(df) > 1 else df
        box_low, box_high, box_days, box_width_ratio = detect_box_pattern(box_source, current_close=price)
        
        # 新增：历史突破策略（突破历史高点）
        past_90_high = df['high'].iloc[-91:-1].max() if len(df) > 90 else df['high'].iloc[:-1].max()
        is_breaking_history = price > past_90_high * 1.01
        history_breakout_momentum_ok = (today['power_ratio'] > 1.05) or ((pct_change >= 0.05) and (today['vol_ratio'] >= 0.95))
        
        is_positive_day = price > yest['close']
        
        if box_low > 0 and box_high > 0:
            is_breaking_box_up = price > box_high * 0.99
            has_box_history = box_width_ratio > 0.08
            has_box_consolidation = box_days >= 10
            breakout_momentum_ok = (today['power_ratio'] > 1.0) or ((pct_change >= 0.04) and (today['vol_ratio'] >= 0.85))
            
            if ENABLE_STRATEGY_BOX_TOP and is_breaking_box_up and has_box_history and has_box_consolidation and is_positive_day and (today['vol_ratio'] > 0.8) and breakout_momentum_ok:
                space_potential = (box_width_ratio) * 100
                return "⭐ 箱体突破", f"箱顶突破({box_low:.2f}-{box_high:.2f})确认！{box_days}天整理后放量上破，后续空间{space_potential:.1f}%，更偏结构突破。"
            
            box_internal_accel = (
                is_yang
                and (price < box_high * 0.98)
                and (pct_change >= 0.05)
                and (pct_change < 0.12)
                and (today['vol_ratio'] >= 0.9)
                and (today['power_ratio'] >= 1.15)
                and box_days >= 18
            )
            if ENABLE_STRATEGY_BOX_INTERNAL and enable_momentum_strategies and box_internal_accel:
                return "💥 箱内加速", f"箱体内加速({pct_change*100:.1f}%)，放量({today['vol_ratio']:.1f}倍)且动能充足(阳线动能{today['power_ratio']:.2f})，仍在箱体内部偏强运行。"
        
        if ENABLE_STRATEGY_HISTORY_BREAK and enable_momentum_strategies and is_breaking_history and is_positive_day and (today['vol_ratio'] > 0.9) and history_breakout_momentum_ok:
            space_potential = ((price - past_90_high) / past_90_high) * 100
            return "🚀 历史突破", f"突破{90}日历史高点({past_90_high:.2f})并站稳！放量({today['vol_ratio']:.1f}倍)，资金动能充足(阳线动能{today['power_ratio']:.2f})，更偏趋势新高。"
        
        mild_trend = (
            is_yang
            and (pct_change >= 0.03)
            and (pct_change < 0.065)
            and (today['close'] > today['ma10'])
            and (today['close'] > today['ma20'])
            and (today['vol_ratio'] >= 0.8)
            and (today['power_ratio'] >= 0.4)
            and (today['power_ratio'] < 1.2)
        )
        if ENABLE_STRATEGY_MILD_TREND and enable_momentum_strategies and mild_trend:
            return "⚪ 温和抬升", f"温和放量上行({pct_change*100:.1f}%)，站上短中期均线，资金动能不强但趋势延续，适合低噪音跟踪。"
        
        # === 均线邻近策略：增加缩量条件，避免信号泛滥 ===
        ma60_near = ENABLE_STRATEGY_MA_NEAR and ENABLE_MA_NEAR_60 and pd.notna(today.get('ma60')) and today['ma60'] > 0 and abs(price - today['ma60']) / today['ma60'] <= MA_NEAR_PCT
        ma150_near = ENABLE_STRATEGY_MA_NEAR and ENABLE_MA_NEAR_150 and pd.notna(today.get('ma150')) and today['ma150'] > 0 and abs(price - today['ma150']) / today['ma150'] <= MA_NEAR_PCT
        if ma60_near or ma150_near:
            # 只有当今日缩量（<1.0）或均线粘合时才发出，避免大涨大跌时的噪音
            near_ma_with_context = (today['vol_ratio'] < 1.0) or (abs(price - today['ma20']) / today['ma20'] < 0.03 if today['ma20'] > 0 else False)
            if near_ma_with_context:
                parts = []
                if ma60_near:
                    parts.append(f"MA60偏离{abs(price - today['ma60']) / today['ma60'] * 100:.1f}%")
                if ma150_near:
                    parts.append(f"MA150偏离{abs(price - today['ma150']) / today['ma150'] * 100:.1f}%")
                return "🧭 均线邻近", f"价格靠近{'、'.join(parts)}，缩量企稳，适合单独观察。"
        
        return None, ""
    except Exception as e:
        log.debug(f"策略检查异常: {str(e)}")
        return None, ""

