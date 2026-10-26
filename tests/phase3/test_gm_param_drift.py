# -*- coding: utf-8 -*-
"""_gm/config/params.py 与 config.py 的只读参数漂移守卫。

不 import 任何生产配置；全部数据来自 scripts.audit_gm_param_drift 的 AST 提取。
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_gm_param_drift import (  # noqa: E402
    CONSISTENT,
    DRIFT,
    FORMAT_ONLY,
    ParamEntry,
    _jsonable,
    audit_parameters,
    compare_parameters,
)

# 2026-10-26 实测白名单。值逐项记录当前现状；这些条目只获准存在，绝不表示建议修改。
KNOWN_DRIFT = {
    'PARAMS::commission_rate': '_gm=0.00015；config.py=0.00025',
    'PARAMS::daily_context_min_rows': '_gm=65；config.py=66',
    'PARAMS::daily_ma_breakdown_gap': '_gm=0.015；config.py=0.03',
    'PARAMS::daily_ma_hard_breakdown_gap': '_gm=0.035；config.py=0.05',
    'PARAMS::daily_overheat_day_ret': '_gm=0.065；config.py=0.05',
    'PARAMS::daily_overheat_ma10_gap': '_gm=0.08；config.py=0.05',
    'PARAMS::daily_overheat_ma20_gap': '_gm=0.12；config.py=0.08',
    'PARAMS::max_single_position_pct': '_gm=0.8；config.py=0.3',
    'PARAMS::max_t_cycles_per_stock': '_gm=4；config.py=8',
    'PARAMS::notify_sell_early_threshold': '_gm=75；config.py=65',
    'PARAMS::notify_sell_panic_threshold': '_gm=60；config.py=50',
    'PARAMS::notify_sell_threshold': '_gm=65；config.py=55',
    'PARAMS::rsi_period': '_gm=14；config.py=6',
    'PARAMS::trend_today_ret_threshold': '_gm=0.025；config.py=0.008',
    'STOCK_PARAMS::000988::allow_overheated_buy': '仅 config.py 存在',
    'STOCK_PARAMS::000988::awaiting_buyback_score_boost': '仅 _gm 存在',
    'STOCK_PARAMS::000988::awaiting_buyback_vwap_gap': '仅 _gm 存在',
    'STOCK_PARAMS::000988::breakdown_risk_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::000988::buy_confirm_min_score': '仅 _gm 存在',
    'STOCK_PARAMS::000988::cooldown_minutes': '仅 _gm 存在',
    'STOCK_PARAMS::000988::daily_trade_limit': '仅 _gm 存在',
    'STOCK_PARAMS::000988::high_buy_score_bypass': '仅 _gm 存在',
    'STOCK_PARAMS::000988::high_buy_score_threshold': '仅 _gm 存在',
    'STOCK_PARAMS::000988::high_buy_score_vwap_gap': '仅 _gm 存在',
    'STOCK_PARAMS::000988::max_buy_times_per_stock': '仅 _gm 存在',
    'STOCK_PARAMS::000988::max_micro_profit_per_day': '仅 _gm 存在',
    'STOCK_PARAMS::000988::max_t_cycles_per_stock': '仅 _gm 存在',
    'STOCK_PARAMS::000988::min_profit_per_t': '仅 _gm 存在',
    'STOCK_PARAMS::000988::min_profit_space': '仅 _gm 存在',
    'STOCK_PARAMS::000988::morning_no_sell_min_ret': '仅 _gm 存在',
    'STOCK_PARAMS::000988::morning_no_sell_until': '仅 _gm 存在',
    'STOCK_PARAMS::000988::notify_buy_threshold': '_gm=43；config.py=36.0',
    'STOCK_PARAMS::000988::notify_sell_threshold': '_gm=63；config.py=55',
    'STOCK_PARAMS::000988::open_dip_buy_penalty': '仅 _gm 存在',
    'STOCK_PARAMS::000988::open_dip_decline_threshold': '仅 _gm 存在',
    'STOCK_PARAMS::000988::rsi_overbought': '仅 _gm 存在',
    'STOCK_PARAMS::000988::rsi_oversold': '仅 _gm 存在',
    'STOCK_PARAMS::000988::sell_holding_min_minutes': '仅 _gm 存在',
    'STOCK_PARAMS::000988::stand_down_min_amplitude': '仅 _gm 存在',
    'STOCK_PARAMS::000988::stock_first_add_pct': '仅 _gm 存在',
    'STOCK_PARAMS::000988::stock_rebuild_strong_pct': '仅 _gm 存在',
    'STOCK_PARAMS::000988::take_profit_pct': '仅 _gm 存在',
    'STOCK_PARAMS::000988::take_profit_time_after': '仅 _gm 存在',
    'STOCK_PARAMS::000988::vol_ratio_confirm': '仅 _gm 存在',
    'STOCK_PARAMS::000988::vwap_buy_deviation': '仅 _gm 存在',
    'STOCK_PARAMS::002451::m2_amount20_min': '仅 _gm 存在',
    'STOCK_PARAMS::002451::m2_lot_value_min': '仅 _gm 存在',
    'STOCK_PARAMS::002451::notify_buy_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::002451::notify_sell_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::002451::stock_qty_base_pct': '仅 config.py 存在',
    'STOCK_PARAMS::002451::stock_qty_strong_pct': '仅 config.py 存在',
    'STOCK_PARAMS::300153::allow_overheated_buy': '仅 config.py 存在',
    'STOCK_PARAMS::588170::m2_amount20_min': '仅 _gm 存在',
    'STOCK_PARAMS::588170::m2_amp20_min': '仅 _gm 存在',
    'STOCK_PARAMS::588170::m2_lot_value_min': '仅 _gm 存在',
    'STOCK_PARAMS::588170::max_sell_times_per_stock': '仅 config.py 存在',
    'STOCK_PARAMS::588170::notify_buy_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::588170::notify_sell_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::588170::stock_qty_base_pct': '仅 config.py 存在',
    'STOCK_PARAMS::588170::stock_qty_strong_pct': '仅 config.py 存在',
    'STOCK_PARAMS::588170::type': '仅 _gm 存在',
    'STOCK_PARAMS::600176::allow_breakdown_buy': '仅 config.py 存在',
    'STOCK_PARAMS::600176::allow_overheated_buy': '仅 config.py 存在',
    'STOCK_PARAMS::600176::max_sell_times_per_stock': '仅 config.py 存在',
    'STOCK_PARAMS::600176::notify_buy_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::600176::notify_sell_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::600176::stock_qty_base_pct': '仅 config.py 存在',
    'STOCK_PARAMS::600176::stock_qty_strong_pct': '仅 config.py 存在',
    'STOCK_PARAMS::600481::bullish_reversal_min_pct': '仅 config.py 存在',
    'STOCK_PARAMS::600481::m2_amount20_min': '仅 _gm 存在',
    'STOCK_PARAMS::600481::m2_lot_value_min': '仅 _gm 存在',
    'STOCK_PARAMS::600481::notify_buy_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::600481::notify_sell_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::600481::stock_qty_base_pct': '仅 config.py 存在',
    'STOCK_PARAMS::600481::stock_qty_strong_pct': '仅 config.py 存在',
    'STOCK_PARAMS::603667::notify_buy_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::603667::notify_sell_threshold': '仅 config.py 存在',
    'STOCK_PARAMS::603667::stock_qty_base_pct': '仅 config.py 存在',
    'STOCK_PARAMS::603667::stock_qty_strong_pct': '仅 config.py 存在',
}

# 当前顶层重叠键基线。若未来某键仅在一侧被删，动态交集本身会漏报；
# 此基线让“对照关系消失”直接失败，避免借删键绕过漂移守卫。
BASELINE_TOP_LEVEL_KEYS = frozenset("""
INDEX_REGIME_PARAMS::adl_lookback
INDEX_REGIME_PARAMS::adx_len
INDEX_REGIME_PARAMS::aroon_len
INDEX_REGIME_PARAMS::aroon_smooth
INDEX_REGIME_PARAMS::atr_len
INDEX_REGIME_PARAMS::bias_len
INDEX_REGIME_PARAMS::breadth_ema_days
INDEX_REGIME_PARAMS::e5_dt_count
INDEX_REGIME_PARAMS::e5_dt_delta
INDEX_REGIME_PARAMS::e5_s_threshold
INDEX_REGIME_PARAMS::e5_zb_factor
INDEX_REGIME_PARAMS::e5_zb_ratio
INDEX_REGIME_PARAMS::e5_zt_count
INDEX_REGIME_PARAMS::e5_zt_delta
INDEX_REGIME_PARAMS::enter_confirm_days
INDEX_REGIME_PARAMS::enter_threshold
INDEX_REGIME_PARAMS::env_weight
INDEX_REGIME_PARAMS::er_len
INDEX_REGIME_PARAMS::er_smooth
INDEX_REGIME_PARAMS::exhaust_bias_pct
INDEX_REGIME_PARAMS::exhaust_factor
INDEX_REGIME_PARAMS::exhaust_reg_len
INDEX_REGIME_PARAMS::exhaust_slope_pct
INDEX_REGIME_PARAMS::exit_threshold
INDEX_REGIME_PARAMS::full_above_ma5_bonus
INDEX_REGIME_PARAMS::full_above_ma5_confirm_days
INDEX_REGIME_PARAMS::full_above_ma5_hard_up
INDEX_REGIME_PARAMS::http_retry
INDEX_REGIME_PARAMS::http_retry_sleep
INDEX_REGIME_PARAMS::http_timeout
INDEX_REGIME_PARAMS::hurst_smooth
INDEX_REGIME_PARAMS::hurst_window
INDEX_REGIME_PARAMS::index_symbol_sh
INDEX_REGIME_PARAMS::index_symbol_sz
INDEX_REGIME_PARAMS::k_anchor_recover_days
INDEX_REGIME_PARAMS::k_boost
INDEX_REGIME_PARAMS::k_bull_streak_bg
INDEX_REGIME_PARAMS::k_cross_bg
INDEX_REGIME_PARAMS::k_cross_bg_days
INDEX_REGIME_PARAMS::k_down_pct
INDEX_REGIME_PARAMS::k_ma5_up_days
INDEX_REGIME_PARAMS::k_margin
INDEX_REGIME_PARAMS::k_up_pct
INDEX_REGIME_PARAMS::kline_count_sh
INDEX_REGIME_PARAMS::kline_count_sz
INDEX_REGIME_PARAMS::late_penalty_mult
INDEX_REGIME_PARAMS::ma5_slope_eps_pct
INDEX_REGIME_PARAMS::ma60_break_ma5_slope_down_hard
INDEX_REGIME_PARAMS::min_bars
INDEX_REGIME_PARAMS::pct_lookback
INDEX_REGIME_PARAMS::qvix_low_pct
INDEX_REGIME_PARAMS::qvix_panic_pct
INDEX_REGIME_PARAMS::qvix_panic_ret20
INDEX_REGIME_PARAMS::qvix_pct_lookback
INDEX_REGIME_PARAMS::r0_cross_min
INDEX_REGIME_PARAMS::r0_factor
INDEX_REGIME_PARAMS::r0_pos_hi
INDEX_REGIME_PARAMS::r0_pos_lo
INDEX_REGIME_PARAMS::r0_vol_max
INDEX_REGIME_PARAMS::r1_half_factor
INDEX_REGIME_PARAMS::reg_len
INDEX_REGIME_PARAMS::s_clip_max
INDEX_REGIME_PARAMS::score_cache_ttl
INDEX_REGIME_PARAMS::score_drop_turn_threshold
INDEX_REGIME_PARAMS::score_rise_turn_threshold
INDEX_REGIME_PARAMS::score_turn_hard_enabled
INDEX_REGIME_PARAMS::sharp_bo3_close
INDEX_REGIME_PARAMS::sharp_bo3_high
INDEX_REGIME_PARAMS::sharp_bo5_close
INDEX_REGIME_PARAMS::sharp_bo5_high
INDEX_REGIME_PARAMS::sharp_decay
INDEX_REGIME_PARAMS::sharp_fast_enter
INDEX_REGIME_PARAMS::sharp_fast_enter_down
INDEX_REGIME_PARAMS::sharp_fill_cross_bg
INDEX_REGIME_PARAMS::sharp_full
INDEX_REGIME_PARAMS::sharp_gap
INDEX_REGIME_PARAMS::sharp_ma10
INDEX_REGIME_PARAMS::sharp_ma5
INDEX_REGIME_PARAMS::sharp_map_max
INDEX_REGIME_PARAMS::sharp_suppress_same_dir
INDEX_REGIME_PARAMS::sharp_trigger
INDEX_REGIME_PARAMS::sharp_vol_12
INDEX_REGIME_PARAMS::sharp_vol_15
INDEX_REGIME_PARAMS::sharp_vol_hi_score
INDEX_REGIME_PARAMS::sharp_vol_lo_score
INDEX_REGIME_PARAMS::smooth_ema_days
INDEX_REGIME_PARAMS::streak_cap
INDEX_REGIME_PARAMS::streak_curve
INDEX_REGIME_PARAMS::streak_late_day
INDEX_REGIME_PARAMS::struct_hard_before_r2
INDEX_REGIME_PARAMS::trend_weight
INDEX_REGIME_PARAMS::vol_fade_factor
INDEX_REGIME_PARAMS::vol_ratio_high
INDEX_REGIME_PARAMS::vol_ratio_low
INDEX_REGIME_PARAMS::w_adx
INDEX_REGIME_PARAMS::w_aroon
INDEX_REGIME_PARAMS::w_breadth
INDEX_REGIME_PARAMS::w_er
INDEX_REGIME_PARAMS::w_ma_streak
INDEX_REGIME_PARAMS::w_nhnl
INDEX_REGIME_PARAMS::w_qvix
INDEX_REGIME_PARAMS::w_reg_r2
INDEX_REGIME_PARAMS::w_structure
INDEX_REGIME_PARAMS::w_volume
PARAMS::bb_period
PARAMS::bb_std
PARAMS::cache_ttl_seconds
PARAMS::commission_rate
PARAMS::daily_cache_ttl_seconds
PARAMS::daily_context_min_rows
PARAMS::daily_ma_breakdown_gap
PARAMS::daily_ma_hard_breakdown_gap
PARAMS::daily_ma_support_loose_gap
PARAMS::daily_overheat_day_ret
PARAMS::daily_overheat_ma10_gap
PARAMS::daily_overheat_ma20_gap
PARAMS::ema_fast_period
PARAMS::ema_slow_period
PARAMS::idle_log_minutes
PARAMS::index_regime_intraday_lock
PARAMS::max_sell_times_per_stock
PARAMS::max_single_position_pct
PARAMS::max_t_cycles_per_stock
PARAMS::min_amplitude
PARAMS::morning_no_sell_until
PARAMS::notify_buy_threshold
PARAMS::notify_sell_early_threshold
PARAMS::notify_sell_panic_threshold
PARAMS::notify_sell_threshold
PARAMS::poll_interval
PARAMS::rsi_period
PARAMS::sell_floor_ratio
PARAMS::stock_first_add_pct
PARAMS::stock_min_trade_unit
PARAMS::stock_qty_base_pct
PARAMS::stock_qty_strong_pct
PARAMS::stock_qty_weak_pct
PARAMS::trend_today_ret_threshold
""".split())


@pytest.fixture(scope="module")
def audit_result():
    return audit_parameters()


def test_no_new_drift_outside_known_whitelist(audit_result):
    actual = {item.gm_key for item in audit_result["comparisons"] if item.status == DRIFT}
    new_drift = actual - KNOWN_DRIFT.keys()
    assert not new_drift, "发现白名单外的新参数漂移（请只审计，勿自动改参）：\n" + "\n".join(sorted(new_drift))

    # 收敛是好事，不强制失败；明确提示维护者清理白名单，防止白名单永久腐化。
    converged = KNOWN_DRIFT.keys() - actual
    if converged:
        warnings.warn(
            "以下已知漂移已消失，可从 KNOWN_DRIFT 移除：\n" + "\n".join(sorted(converged)),
            UserWarning,
            stacklevel=1,
        )


def test_top_level_comparison_keys_cannot_disappear(audit_result):
    current = {
        item.gm_key for item in audit_result["comparisons"]
        if item.mapping == "同名" and not item.gm_key.startswith("STOCK_PARAMS::")
    }
    missing = BASELINE_TOP_LEVEL_KEYS - current
    assert not missing, "原有顶层对照键已单侧消失；这属于需审查的新漂移：\n" + "\n".join(sorted(missing))


def test_ast_extraction_is_complete(audit_result):
    skipped = audit_result["gm_skipped"] + audit_result["config_skipped"]
    assert not skipped, f"存在无法静态提取的键：{skipped}"
    assert len(audit_result["gm_values"]) > 200
    assert len(audit_result["config_values"]) > 150


def test_time_percent_and_path_semantic_normalisation(audit_result):
    alias = next(item for item in audit_result["comparisons"] if item.mapping == "显式语义映射")
    assert alias.gm_key == "PARAMS::osc_sell_time_hhmm"
    assert alias.config_key == "SENTIMENT_PARAMS::force_flat_time"
    assert alias.status == FORMAT_ONLY

    gm = {
        "PARAMS::fee": ParamEntry("PARAMS::fee", 0.005, 1),
        "PARAMS::report_gen_dir": ParamEntry("PARAMS::report_gen_dir", r"E:\Report\gen", 2),
        "PARAMS::threshold": ParamEntry("PARAMS::threshold", 0.01, 3),
    }
    config = {
        "PARAMS::fee": ParamEntry("PARAMS::fee", "0.5%", 10),
        "PARAMS::report_gen_dir": ParamEntry("PARAMS::report_gen_dir", "e:/report/gen", 20),
        "PARAMS::threshold": ParamEntry("PARAMS::threshold", 0.02, 30),
    }
    result = {item.gm_key: item.status for item in compare_parameters(gm, config)
              if item.gm_key in gm}
    assert result == {
        "PARAMS::fee": FORMAT_ONLY,
        "PARAMS::report_gen_dir": FORMAT_ONLY,
        "PARAMS::threshold": DRIFT,
    }


def test_audit_is_deterministic(audit_result):
    repeated = audit_parameters()
    assert _jsonable(audit_result) == _jsonable(repeated)
