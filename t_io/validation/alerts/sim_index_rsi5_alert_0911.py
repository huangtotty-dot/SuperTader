# -*- coding: utf-8 -*-
"""sim_index_rsi5_alert_0911.py — 指数5分钟RSI超卖预警 · 09-11 数据模拟推送（2026-09-11）

用途：用 2026-09-11 当日分钟数据复算四指数 5m RSI，取各自**首次 <阈值**的时点，
按线上同款「独立醒目卡」推送飞书（标题带 🧪【模拟测试】），验证格式与链路。
运行：python t_io/validation/alerts/sim_index_rsi5_alert_0911.py
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analysis.index_regime_intraday import fetch_index_minutes_live  # noqa: E402
from analysis.indicators import resample_to_5min, add_5min_indicators  # noqa: E402
from config import INDEX_RSI5M_ALERT, build_index_rsi5_alert_card, send_feishu_payload  # noqa: E402

NAMES = {"sh000001": "上证指数", "sz399001": "深证成指", "sz399006": "创业板指", "sh000688": "科创50"}
TH = float(INDEX_RSI5M_ALERT.get("threshold", 20))
WARM = 8   # 与线上一致：≥8 根 5m bar 才判（防首根假 0）

hits, info = [], []
for ic in INDEX_RSI5M_ALERT.get("indices", []):
    try:
        m = fetch_index_minutes_live(ic)
        df5 = add_5min_indicators(resample_to_5min(m))
    except Exception as e:
        info.append(f"{NAMES.get(ic, ic)} 数据不可用: {str(e)[:50]}")
        continue
    if df5 is None or df5.empty:
        continue
    _rsi = df5["rsi_5m_p6"]
    _first = None
    for i in range(WARM, len(df5)):
        v = _rsi.iloc[i]
        if v == v and float(v) < TH:
            _first = (str(df5["time"].iloc[i])[-8:-3], float(v))
            break
    if _first:
        hits.append({"msg": f"{NAMES.get(ic, ic)} 5分钟RSI={_first[1]:.1f}<{TH:g}（超卖） @{_first[0]}"})
    info.append(f"{NAMES.get(ic, ic)}: {'触发@' + _first[0] if _first else '未触发'}")

print("09-11 复算：", " | ".join(info))
print(f"命中 {len(hits)} 个指数，推送模拟卡…")
if hits:
    ok = send_feishu_payload(
        payload=build_index_rsi5_alert_card(hits, when="2026-09-11（模拟测试·收盘后复算）", sim=True),
        success_log="✅ 指数5m超卖预警(模拟)已推送",
        error_prefix="指数5m超卖预警(模拟)推送",
    )
    print("push ok:", ok)
else:
    print("当日无 <阈值 命中，未推送")
