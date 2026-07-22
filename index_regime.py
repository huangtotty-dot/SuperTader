# -*- coding: utf-8 -*-
"""
index_regime.py — 大盘态势判定模块 V2.2.4（日线级：streak 主导打分 + K日跃迁 + SHARP 指标锐化 + 结构分强化 + 分数转折 + 规则修正层 + 磁滞状态机）

V2.2.4 结构性修正（价格结构强化 + 分数转折 + SHARP 锐化分语义回归"关键转折日"，2026-07-18）：
  V2.2 全量测试（108 交易日）暴露 4 个结构性问题，逐条修正（全部参数化）：
  C1【同向触发抑制】锐化监测范围按状态收窄：uni_up 只监测空头锐化、uni_down 只监测
     多头锐化、range 双向监测（开关 sharp_suppress_same_dir）。同向锐化分仍照常计算并
     保留在 detail.sharp（suppressed=true / suppressed_side / suppressed_value /
     sharp_s_raw 记录原始值），但不计入 S、不参与触发、不重置衰减——修复 V2.2 中
     uni_up 里"5日新高+站上MA5/10"=13 分每日过线（108 天触发 64 次、方向正确率仅
     58%）的同向延续误触发；衰减 carry 由此才能真正逐日 ×sharp_decay 生效。
  C2【补位缠绕门控】锐化补位 K-up 跃迁（ku_fill）必须满足近 k_cross_bg_days 日
     MA5/MA10 交叉>=k_cross_bg（复用 K-day 缠绕背景参数，交叉次数经 kctx 由
     _ir_kday_eval 传入）；空头补位（anchor_fill）对称要求缠绕背景（开关
     sharp_fill_cross_bg）。缠绕背景不足时补位放弃（k_link 标 ku_fill_blocked /
     anchor_fill_blocked），锚点/跃迁状态不改动，但 sharp_s 本身仍计入 S（触发
     事实不变）。与 K-day 同日触发（with_k_up/k_down）不受影响（K-day 自带门控）
     ——修复 V2.2 中 ku_fill 绕过 cross15>=2 检查导致 06-18 假入 uni_up。
  C3【触发线上移】sharp_trigger 20 → 28（= sharp_net 15.4/22，整数分即 >=16：
     必须带量能确认或更高档组合才算转折），同向抑制与缠绕门控之上再压假阳性。
  C4【S 量程封顶】全部加法项（含 SHARP、E5）与 Hurst/衰竭计入、EMA 之后，
     clip 到 [-s_clip_max, +s_clip_max]（默认 ±100）；detail.pipeline 记录
     s_pre_clip——修复 V2.2 中 05-08 S=104.61 破量程。
  R0 豁免范围同步收窄：仅真实 K-day 或 sharp 转折触发（|sharp_s|>=sharp_trigger）
  当日豁免（C1/C3 后自然满足，03-24~04-07 R0 命中由 8/10 恢复 >=9/10）。

V2.2.4 补丁（2026-07-18，价格结构强化）：
  C7【价格结构强化】新增 MA5 持续站稳/站下确认、MA20/MA60 破位与触线回落的结构分。
     规则层在 MA60 关键位被击穿时可直接把 range 拉向 uni_down；持续站稳 MA5 的场景则
     更快确认 uni_up。目标是让“参考前几日的信息”真正参与当日判断，避免关键转折
     被 EMA/磁滞拖成震荡。

V2.2.2 补丁（2026-07-18，SHARP 聚合修复）：
  C5【档内子项聚合修复】V2.2~V2.2.1 的 up/dn 聚合误只用档基分 bo+量能+均线，
     档内子项（收破确认 cc、竞价跳空 gap）算出了写入 detail.parts 却未参与聚合：
     波动侧实际封顶 5（规格 9）、单侧实际满分 18（规格 22），04-08 sharp_up
     应为 9+0+8=17 实测仅 13。本版修复为 bo+cc+gap+vol+ma 全量聚合。
  C6【触发线 28→32】聚合修复后"满档突破 9 + 均线 8（无量能）"= 17/22 →
     sharp_s=30.9，28 档会被任何强势日触达（本窗 ~20 日，含 04-01 震荡窗口内、
     06-15 假入场区两处有害触发），与"只服务关键转折日"语义冲突。32（=
     sharp_net 17.6/22）使触发必须带量能确认（9+3+8=20→36.4 / 9+5+8=22→40）。
     本窗 vol_ratio 峰值 1.1825<1.2 → 0 触发，108 日逐日分与 V2.1/V2.2.1
     基线保持一致；无量能满档突破日（04-08/05-14）本就由 K-day 覆盖，
     SHARP 专职"量能确认的 N 日极值转折"补位，机制武装待命。

V2.2 变更点（指标锐化分 SHARP，2026-07-18）：
  用户需求原话："在关键趋势发生转折时，需要对关键日的分数增加指标锐化分数重新累积。"
  1. 锐化分组成（每日基于 OHLC 可算，前序窗口不含当日；多头示例，空头完全对称）：
     - 波动突破侧（0~9，用户规格表原值）：high>max(前5日 high) → 5日档 5 分，
       档内 close>max(前5日 close) +2、open>prev_close（竞价高开口径）+2（5日档满 9）；
       未达 5 日档时 high>max(前3日 high) → 3日档 3 分，档内 close +1、gap +2（3日档满 6）；
       两档不叠加取高（档内子项以档成立为前提；5日档达成时 3日档必然达成，取总分较高档）。
     - 量能确认侧（0~5）【默认补全：用户规格表第 2 项被截断】：
       vol_ma5/vol_ma20>=sharp_vol_15(1.5) → +5；>=sharp_vol_12(1.2) → +3；取高。
       口径与 R0 的 vol_ratio 一致（上证 volume MA5/MA20）。量能是无方向市场级指标，
       双向同计（同日双侧>5 的 conflict 净值机制已对冲该对称计入的影响）。
     - 均线状态侧（0~8）【默认补全：用户规格表第 3 项被截断】：
       close>MA5 → +4、close>MA10 再 +4；空头对称 close<MA5 +4、<MA10 再 +4。
     sharp_up / sharp_down 各 0~sharp_full(22)；sharp_net：同日双侧>5 → conflict=true
     取净值（up−down），否则取绝对值较大侧带符号值。
  2. 映射与计入：sharp_s = sharp_net/sharp_full×40（±40 封顶），规则层加法项
     （与 E5 同层：R0 之后、Hurst/EMA 之前，s_sharp = s_e5 + decayed_s）。
     触发-衰减状态机：|sharp_s|>=sharp_trigger(32，即 sharp_net>=17.6/22，V2.2.2 起) 当日触发 age=0
     全额计入；其后每交易日 ×sharp_decay(0.5) 衰减；新触发重置（含反向）；
     状态切换清零（prev_regime 与触发日 regime 不同 → 携带清零）；|decayed|<0.5 自然熄灭。
     非触发日的 fresh sharp_s 不直接计入（仅携带衰减贡献），detail 照常输出便于复盘。
  3. 转折触发器 ↔ K-day 联动（监测范围：全状态 range/uni_up/uni_down 双向监测——
     uni_up 中的空头锐化正是 05-14 类转折）【V2.2.1 C1 已收窄：uni_up 只监测空头、
     uni_down 只监测多头、range 双向】：
     - 多头锐化触发：若 K-up 未触发则补位（k_up 置位 via=sharp_up；streak>0 时
       eff_days=real+k_boost 跃迁；豁免 R0 与 EMA；RANGE 中 S>=+25 允许单日确认进
       uni_up，参数 sharp_fast_enter 可关）；存续空头锚点解除（对齐 K-up 强反转语义）。
     - 空头锐化触发：若 K-down 未触发则补位（激活空头锚点 via=sharp_down：
       below_days=1(当日收破MA5)否则 0，anchor40=-curve(below+k_boost) 与 streak 分
       取更负值 = "streak 清零+空头锚点"；豁免 R0 与 EMA；清除 K-up 跃迁）。
       空头默认不设 uni_down 单日确认（对齐 V2.1 K-down 沿用现有退出规则；
       参数 sharp_fast_enter_down 默认 False 预留）。
     - 与 K-day 同日同向触发：T1 跃迁取 max（同 boost 参数，幂等），sharp_s 照常叠加；
       反向冲突（K-up×空头锐化 / K-down×多头锐化）：T1 以 K-day 为准，sharp_s 照常叠加，
       detail.sharp.k_link 标注。
  4. 输出：detail.sharp={sharp_up,sharp_down,sharp_net,sharp_s,tier{up,down},
     parts{up:{breakout,close_confirm,gap,vol,ma},down:{...}},age_days,decayed_s,
     triggered,conflict,k_link,carry_active}；fired_rules 追加 SHARP_UP/SHARP_DOWN
     （该侧存在突破档或量能确认且得分主导时）/SHARP_TRIGGER（|sharp_s|>=触发线）。
  5. 锐化携带状态随 state.json 持久化（st["sharp"]），history 逐日快照支持幂等重跑回卷。
  6. IR_DEFAULT_PARAMS 追加 sharp_* 全参数（规格表分值/阈值全部参数化，便于事后修正）。

V2.1 变更点（关键日跃迁机制 K-day，2026-07-18）：
  1. K-up【多头启动日】：收盘同时站上 MA5/MA10 且最小余量>=k_margin（0.3%）、昨日未同时
     站上（首次）、当日涨幅>=k_up_pct（1.0%，校准 04-08 实测 +2.70%）、近 k_cross_bg_days(15)
     日 MA5/MA10 交叉>=k_cross_bg(2)（缠绕背景——区分 04-08 真信号与 06-16 金叉假信号：
     06-12/06-15 cross15=0 且 MA5<MA10 双重排除）、MA5>=MA10。
     效果：T1 streak 等效天数 +k_boost（校准定 9，第 1 天按第 10 天档 36/40 计，
     effective_days=real+boost，曲线自然封顶 13 天档）；当日豁免 R0 压缩与 EMA 平滑
     （关键日优先级：用户红线"关键日特征应使分数当日迅速弹上去，后续基于当日继续累积"）；
     状态机：RANGE 中 K-up 当日 S>=enter_threshold 允许当日确认进 uni_up（跳过连续 2 日）。
     校准说明：建议值 boost=+4 在 04-08 实测仅 +2.7 跳升（环境分 E=-27.76 与 Hurst 放大
     1.166 双重拖累，且 EMA 半衰稀释），不满足验收 +10；校准后 k_boost=9 且 K 日豁免 EMA，
     实测 04-08 S 跳升 +11.1。k_boost 越大 T1 越早触顶（real+9 第 4 天即满级 40），
     属机制固有代价，已在触发清单全窗口核查假阳性（仅 04-08 触发）。
  2. K-down【空头启动日】：此前连续>=k_ma5_up_days(3) 日收盘在 MA5 上方、当日跌幅
     <=-k_down_pct（1.0%，校准 05-14 实测 -1.52%）收破 MA5、背景为多头 streak>=
     k_bull_streak_bg(8) 或前一状态 uni_up。
     效果：T1 多头 streak 分清零（与 R1 叠加取更劣），并设空头锚点：自当日起按"收盘持续
     位于 MA5 下方天数"负向累积，anchor40 = -曲线(min(below_days+k_boost, 13档))
     （不等 MA5<MA10 死叉；收复 MA5 连续 k_anchor_recover_days(2) 日解除锚点；
     死叉出现后由正常空头 streak 无缝接管——T1 取锚点与正常 streak 分的更负值）；
     当日同样豁免 R0 与 EMA。状态机沿用现有退出规则（S<exit_threshold 当日退出 /
     R2 破 MA10 立即退出），uni_up 中 K-down 当日 S<+15 → 当日退出自然生效。
     校准说明：字面"-8 起步档"实测 05-14 S 仅下坠 9.5 分（E=+54 环境分顶着且 EMA 稀释），
     不满足验收 25 分；改为与 K-up 对称的 boost 跃迁（首日即 -curve(1+9)=-36），
     实测 05-14 S 下坠 38.4 分、05-15 R2 退出 uni_up、05-15~05-19 持续负向累积。
  3. 输出：detail.key_day={type:"k_up"/"k_down"/null, boost, anchor_active, anchor_days,
     reason}，fired_rules 追加 K_UP / K_DOWN / K_ANCHOR（锚点存续日）。
  4. 锚点状态随 state.json 持久化（st["k_anchor"]），history 逐日快照支持幂等重跑回卷。
  5. IR_DEFAULT_PARAMS 追加 k_up_pct / k_down_pct / k_margin / k_boost / k_cross_bg /
     k_cross_bg_days / k_ma5_up_days / k_bull_streak_bg / k_anchor_recover_days。

V2.0 变更点（依据 feature_study_v2.md 数据特征研究，2026-07-18）：
  1. 趋势维度重构：新 T1【MA5>MA10 多头 streak 累积分】权重 40%（最大因子），
     分段线性锚点 第1/3/5/8/10/13天 = +8/+16/+24/+32/+36/+40 封顶，空头对称；
     金叉起算、死叉清零转向。T2 ADX 20% / T3 回归斜率×R² 20% / T4 Kaufman ER 10% /
     T5 Aroon 10%。删除 V1 的均线排列分与 CHOP（研究证伪：震荡段 CHOP 仅 32~46，
     教科书阈值失效；ADX<20 / BBW 收窄同失效）。
  2. 新增状态修正规则层（加权分之后、状态机之前）：
     - R0【震荡三元组压缩】cross20>=3 且 量比5/20<1.0 且 pos20∈[15,65] → S×0.6
       （研究中该三元组对震荡段 100% 命中、趋势段 0% 误判）；
     - R1【streak 内破位减分】多头 streak 中收破 MA5 → 当日 streak 分减半（不退出），
       收复 MA5 自动恢复；|streak|>=20 晚期警示：R1 惩罚 ×2（扣减 50%→100%）；
     - R2【破位快速退出】uni_up 中收破 MA10 → 当日立即退出到 range（跳过连续确认）；
       uni_down 中收复 MA10 → 立即退出到 range。研究红线：空头 streak 中收复 MA10
       是反抽噪音，故 uni_up 入场必须 streak>0、uni_down 入场必须 streak<0。
  3. E5 数据源替换：主源换为同花顺涨停聚焦 dataapi（一次请求给齐 涨停/跌停/炸板/
     炸板率，历史可回溯 >=8 个月），东财三池保留作近 3 周兜底；炸板率直接用 THS
     返回的 zb_rate = zb_count / zt_touched。非交易日 THS 返回全 0（不采信）——
     模块只在交易日 as_of 调用（日线切片自带交易日历）。
  4. 评估时点模式：detect_index_regime(as_of=None, force=False, mode="eod")
     - "eod"（默认）：截至 as_of 收盘；
     - "morning"：早盘用，自动对齐到 as_of 之前最近一个已完成交易日，输出
       detail.recent_days（最近 3 个交易日的 [{date, regime, score}]）；
     - "tail"：14:30 后盘中用，as_of 默认当天（腾讯日线含 forming bar），输出
       estimate=true，且不写 state.json / trace（保持 EOD 状态机纯净）。
     签名其余部分与返回结构保持与 V1 一致。
  5. IR_DEFAULT_PARAMS 全量更新（streak 曲线锚点、R0 三阈值、R1/晚期倍数、
     e5_source="ths" 开关等），保留 globals().get('INDEX_REGIME_PARAMS') 合并机制。

V1 保留骨架：
  - 综合分 S = (趋势T×0.60 + 环境E×0.40) → R0压缩 → E5触发修正 → Hurst置信乘数
    → 衰竭修正 → EMA(3日) → 磁滞状态机（|S|>=25 连续2日入场；|S|回穿15 立即退出；
    两单边态不可直接互跳）。
  - 环境维度 E：E1 涨跌家数强度+ADL(35) / E2 NH-NL(25，数据层降级中) /
    E3 量能确认(25) / E4 QVIX(15)。

工程形态（风格B）：
  - 顶部自带全部 import，可独立运行（python index_regime.py --date 2026-07-17 --json），
    也可被 main.py exec 进共享命名空间或被回测脚本标准 import。
  - 命名红线：不重新定义 PARAMS / log / _now / fetch_minute_bar 等宿主已有名字；
    模块内顶层私有名字一律 `_ir_` 前缀，公开接口仅 IndexRegime 与 4 个接口函数。
  - 宿主兼容：_ir_now 优先取宿主 _now（支持 SIM_NOW 注入回测），_ir_log 优先取宿主 log。

数据层（2026-07 实测结论）：
  - 指数日线【主】腾讯 fqkline（sh000001 / sz399001），【备】ak.stock_zh_index_daily；
    行内第 7 列为 amount 则取之，否则成交额退化为 volume（两市求和口径一致即可）。
  - QVIX：ak.index_option_50etf_qvix() → 备 index_option_300etf_qvix() → 本地 HV20 分位保底。
  - 涨停/跌停/炸板：【主】同花顺 dataapi limit_up_pool（>=8 个月历史，按日期缓存），
    【备】东财 ak.stock_zt_pool_em / _dtgc_em / _zbgc_em（仅近 ~3 周）。
  - E1 涨跌家数：当日 ak.stock_zh_a_spot_em() 自算 + 每日落库 breadth_{date}.json；
    历史日期仅读本地落库；不足 10 日标 partial，0 日降级。
  - E2 NH-NL：ak.stock_a_high_low_statistics 已失效 → 默认降级（score=0, degraded），
    打分函数完整保留，TODO(三期)：自建 60 日新高新低宽度库后接通。

降级与权重归一化：任一指标数据不可得 → score=0 且 detail 标 degraded；合成维度分时
对未降级指标按权重重新归一化，degraded 列表写入输出 JSON。宁缺毋崩。
"""

from enum import Enum
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
import argparse
import json
import logging
import math
import os
import random
import time

import numpy as np
import pandas as pd

# ============================================================================
# 宿主兼容层（风格B）：时间 / 日志 —— 绝不重定义宿主已有名字
# ============================================================================

_ir_now = globals().get("_now") or datetime.now          # 宿主 exec 环境有 _now（SIM_NOW 注入）
_ir_log = globals().get("log")                            # 宿主 exec 环境有 log
if _ir_log is None:                                       # 独立运行时自建 logging
    _ir_log = logging.getLogger("index_regime")
    if not _ir_log.handlers:
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%H:%M:%S"))
        _ir_log.addHandler(_h)
        _ir_log.setLevel(logging.INFO)

# ============================================================================
# 参数（宿主可用全局 INDEX_REGIME_PARAMS dict 注入覆盖）
# ============================================================================

IR_DEFAULT_PARAMS: Dict[str, Any] = {
    # —— 合成与状态机 ——
    "trend_weight": 0.60,
    "env_weight": 0.40,
    "enter_threshold": 25.0,          # 单边入场阈值 |S|
    "exit_threshold": 15.0,           # 单边退出阈值 |S|（立即生效）
    "enter_confirm_days": 2,          # 入场需连续 N 个交易日越过阈值（含当日）
    "smooth_ema_days": 3,             # S 的 EMA 平滑窗口
    "hurst_window": 120,              # Hurst R/S 窗口
    "hurst_smooth": 20,               # H 的平滑日均窗口
    "exhaust_slope_pct": 0.80,        # 衰竭：|120日斜率| > 近一年 80% 分位
    "exhaust_bias_pct": 0.90,         # 衰竭：BIAS20 > 近一年 90% 分位
    "exhaust_factor": 0.7,            # 衰竭修正系数
    "score_cache_ttl": 1800,          # 内存缓存 TTL（秒），复用 daily_cache_ttl_seconds 语义
    # —— 趋势维度内权重（V2，合计 1.00；T1 streak 为主导因子）——
    "w_ma_streak": 0.35, "w_structure": 0.15, "w_adx": 0.18, "w_reg_r2": 0.17,
    "w_er": 0.08, "w_aroon": 0.07,
    # —— 环境维度内权重（合计 1.00）——
    "w_breadth": 0.35, "w_nhnl": 0.25, "w_volume": 0.25, "w_qvix": 0.15,
    # —— T1 MA streak 累积分曲线（分段线性锚点：第k天 → 分值，±40 封顶）——
    "streak_curve": ((1, 8.0), (3, 16.0), (5, 24.0), (8, 32.0), (10, 36.0), (13, 40.0)),
    "streak_cap": 40.0,               # 累积分封顶（第13天起满级）
    "streak_late_day": 20,            # |streak|>=20 → 晚期警示（不再加分，R1 惩罚加倍）
    "late_penalty_mult": 2.0,         # 晚期警示下 R1 扣减比例倍数（0.5×2=1.0 → 清零）
    "r1_half_factor": 0.5,            # R1：streak 内收破 MA5 → 当日累积分扣减比例
    # —— 止跌退出（站上 MA5 + 阳线确认 → 趋势切换，优先于分数恢复）——
    "uni_down_exit_above_ma5": True,  # uni_down 中收盘站上 MA5 且至少 1 根阳线 → 切 range
    # —— 首次站上 MA5 恢复加分（deep streak 止跌初期 low-buffer 加分，弥补 above_ma5_days>=3 高阈值空窗）——
    "ma5_recover_streak_min": -5,     # 最小 |streak| 才触发恢复（< -5 即 deep streak 才算）
    "ma5_recover_bonus_base": 5.0,    # 恢复基础加分
    "ma5_recover_bonus_streak": 0.3,  # 每单位 |streak| 额外加分系数
    "ma5_recover_bonus_pos20": 0.12,  # 每单位 pos20 额外加分系数（价格从谷底回升越多加越多）
    "ma5_recover_bonus_cap": 15.0,    # 恢复加分封顶
    # —— R0 震荡三元组压缩（研究中震荡段 100% 命中 / 趋势段 0% 误判）——
    "r0_cross_min": 3,                # 近20日 MA5/MA10 交叉次数下限
    "r0_vol_max": 1.15,                # 量比 vol_MA5/vol_MA20 上限（缩量）
    "r0_pos_lo": 15.0, "r0_pos_hi": 65.0,   # 20日价格区间位置带
    "r0_factor": 0.6,                 # 命中 → 总分 ×0.6
    # —— V2.1 K-day 关键日跃迁（校准自 sh000001_daily_features.csv，2026-07-18）——
    "k_up_pct": 1.0,                  # K-up 大阳线当日涨幅下限%（04-08 实测 +2.70%）
    "k_down_pct": 1.0,                # K-down 大阴线当日跌幅下限%（05-14 实测 -1.52%）
    "k_margin": 0.3,                  # K-up 收盘站上两线的最小余量%（04-08 实测 1.74%；06-12 仅 0.08% 排除）
    "k_boost": 9,                     # K日 streak 等效天数跃迁（第1天按第10天档 36/40 计；校准：+4 不足以过验收锚点）
    "k_cross_bg": 2,                  # K-up 缠绕背景：近 k_cross_bg_days 日交叉次数下限
    "k_cross_bg_days": 15,            # 缠绕背景回看窗（04-08 cross15=3；06-16 段 cross15<=1 排除）
    "k_ma5_up_days": 3,               # K-down 前置：此前连续站上 MA5 天数下限（05-14 实测 8）
    "k_bull_streak_bg": 8,            # K-down 背景：多头 streak 下限（05-14=24，03-03=9）
    "k_anchor_recover_days": 2,       # 空头锚点解除：收复 MA5 连续天数
    # —— V2.2.4 价格结构强化（MA5 持续站稳/跌破 + MA20/MA60 关键线破位）——
    "ma5_persist_days": 3,           # 连续站上/站下 MA5 的确认天数
    "ma20_break_bonus": 8,           # 跌破/收复 MA20 的结构分加减
    "ma60_break_bonus": 18,          # 跌破/收复 MA60 的结构分加减
    "ma_touch_bonus": 6,             # 触及关键均线后收回/回落的结构分
    "ma5_slope_eps_pct": 0.0,        # MA5 斜率阈值（绝对值小于视为平）
    "full_above_ma5_confirm_days": 2,# 全 K 站上 MA5 的确认天数
    "full_above_ma5_bonus": 8,       # 全 K 站上 MA5 的结构加分
    "ma60_break_ma5_slope_down_hard": True,  # MA60 破位 + MA5 下行硬切下行
    "full_above_ma5_hard_up": True,  # 全 K 站上 MA5 硬切上行
    "struct_hard_before_r2": True,   # 结构硬转向优先于 R2 退出
    "score_drop_turn_threshold": 15.0,# 分数单日下坠转折阈值
    "score_rise_turn_threshold": 15.0,# 分数单日上冲转折阈值
    "score_turn_hard_enabled": True,  # 分数突变硬转向开关
    # —— V2.2 指标锐化分 SHARP（转折日锐化 + 触发-衰减携带；规格表分值全部参数化）——
    "sharp_full": 22,                 # 锐化满分（波动9 + 量能5 + 均线8）
    "sharp_map_max": 40.0,            # sharp_s = sharp_net/sharp_full × 40（±40 封顶）
    "sharp_trigger": 32.0,            # 【V2.2.2 C6】转折触发线 |sharp_s|（= sharp_net 17.6/22，整数分>=18：聚合修复后满档突破9+均线8=17→30.9 被拦，触发必须带量能确认 9+3+8=20→36.4；V2.2.1 为 28）
    "structure_trigger": 10.0,        # 价格结构强信号阈值（用于日线主判定/盘中 hint）
    "sharp_decay": 0.5,               # 触发后每交易日衰减系数（age+1 → ×0.5）
    "sharp_suppress_same_dir": True,  # 【V2.2.1 C1】同向触发抑制：uni_up 只监测空头锐化、uni_down 只监测多头锐化、range 双向
    "sharp_fill_cross_bg": True,      # 【V2.2.1 C2】补位缠绕门控：ku_fill/anchor_fill 需近 k_cross_bg_days 日交叉>=k_cross_bg
    "s_clip_max": 100.0,              # 【V2.2.1 C4】S 量程封顶：EMA 之后 clip 到 ±s_clip_max（detail.pipeline 记 s_pre_clip）
    # 波动突破侧（用户规格表原值；两档不叠加取高，档内子项以档成立为前提）
    "sharp_bo5_high": 5,              # 5日档：high > max(前5日 high)
    "sharp_bo5_close": 2,             # 5日档内：close > max(前5日 close)
    "sharp_bo3_high": 3,              # 3日档：high > max(前3日 high)（未达5日档时）
    "sharp_bo3_close": 1,             # 3日档内：close > max(前3日 close)
    "sharp_gap": 2,                   # 档内竞价高开/低开：open vs prev_close（严格大于/小于）
    # 量能确认侧【默认补全：用户规格表第 2 项被截断，阈值/分值参数化待校正】
    "sharp_vol_15": 1.5,              # vol_ma5/vol_ma20 高档（+sharp_vol_hi_score）
    "sharp_vol_12": 1.2,              # vol_ma5/vol_ma20 低档（+sharp_vol_lo_score）
    "sharp_vol_hi_score": 5,
    "sharp_vol_lo_score": 3,
    # 均线状态侧【默认补全：用户规格表第 3 项被截断】
    "sharp_ma5": 4,                   # close 站上/跌破 MA5
    "sharp_ma10": 4,                  # close 站上/跌破 MA10（与 MA5 叠加）
    # K-day 联动开关
    "sharp_fast_enter": True,         # 多头锐化触发：RANGE 中 S>=enter 允许单日确认进 uni_up
    "sharp_fast_enter_down": False,   # 空头锐化单日确认进 uni_down（默认关，对齐 K-down 沿用退出规则）
    # —— 指标窗口 ——
    "atr_len": 14, "adx_len": 14,
    "reg_len": 40,                                   # T3 回归窗口
    "er_len": 10, "er_smooth": 5,                    # T4
    "aroon_len": 25, "aroon_smooth": 3,              # T5
    "exhaust_reg_len": 120, "bias_len": 20,
    "pct_lookback": 250,                             # 各类“近一年分位”的回看窗
    "qvix_pct_lookback": 750,                        # QVIX/HV20 近三年分位回看窗
    # —— E1 广度 ——
    "breadth_ema_days": 10, "adl_lookback": 60,
    # —— E3 量能打分档位 ——
    "vol_ratio_high": 1.2, "vol_ratio_low": 0.8, "vol_fade_factor": 0.4,
    # —— E4 QVIX 阈值 ——
    "qvix_panic_pct": 0.85, "qvix_low_pct": 0.20, "qvix_panic_ret20": -0.05,
    # —— E5 涨跌停规则 ——
    "e5_dt_count": 30, "e5_dt_delta": -10.0,
    "e5_zt_count": 80, "e5_zt_delta": 10.0,
    "e5_zb_ratio": 0.45, "e5_zb_factor": 0.9,
    "e5_s_threshold": 15.0,
    # —— E5 数据源（V2：同花顺主源 + 东财近3周兜底）——
    "e5_source": "ths",               # "ths" 同花顺涨停聚焦（>=8个月历史）；"em" 强制东财
    "e5_em_fallback": True,           # THS 失败/全0 时东财三池兜底（仅近 ~3 周有效）
    "e5_ths_cache": None,             # THS 按日期缓存文件；None → <state_dir>/e5_ths_cache.json
    # —— 数据与 IO ——
    "index_symbol_sh": "sh000001", "index_symbol_sz": "sz399001",
    "kline_count_sh": 900, "kline_count_sz": 450,
    "http_timeout": 15, "http_retry": 4, "http_retry_sleep": 2.0,
    "state_dir": None,                               # None → 默认 <BASE_DIR|E:\06_T>\t_io\index_regime
    "min_bars": 150,                                 # 趋势指标所需最少日线数（不足则逐项降级）
}


def _ir_params() -> Dict[str, Any]:
    """参数解析：默认值 ← 宿主注入的全局 INDEX_REGIME_PARAMS（若存在）"""
    p = dict(IR_DEFAULT_PARAMS)
    host_p = globals().get("INDEX_REGIME_PARAMS")
    if isinstance(host_p, dict):
        p.update(host_p)
    return p


def _ir_state_dir(p: Dict[str, Any]) -> str:
    """状态目录优先级：环境变量 IR_STATE_DIR > 参数 state_dir > 宿主 BASE_DIR > E:\\06_T 默认"""
    env = os.environ.get("IR_STATE_DIR")
    if env:
        return env
    if p.get("state_dir"):
        return p["state_dir"]
    base = globals().get("BASE_DIR") or r"E:\06_T"
    return os.path.join(base, "t_io", "index_regime")


# ============================================================================
# 状态枚举与公开接口契约
# ============================================================================

class IndexRegime(Enum):
    UNI_DOWN = "uni_down"   # 单边下行
    RANGE = "range"         # 横盘震荡
    UNI_UP = "uni_up"       # 单边上涨


_IR_REGIME_NAMES = {
    IndexRegime.UNI_DOWN: "单边下行",
    IndexRegime.RANGE: "横盘震荡",
    IndexRegime.UNI_UP: "单边上涨",
}

_IR_POSITION_FACTORS = {
    IndexRegime.UNI_DOWN: 0.6,
    IndexRegime.RANGE: 1.0,
    IndexRegime.UNI_UP: 1.1,
}

_IR_MODES = ("eod", "morning", "tail")


def index_regime_name(regime) -> str:
    """状态中文名（飞书推送用）"""
    try:
        r = regime if isinstance(regime, IndexRegime) else IndexRegime(str(regime))
    except Exception:
        return "未知"
    return _IR_REGIME_NAMES.get(r, "未知")


def get_regime_position_factor(regime) -> float:
    """活动仓系数（供 position_sizer）：uni_down 0.6 / range 1.0 / uni_up 1.1"""
    try:
        r = regime if isinstance(regime, IndexRegime) else IndexRegime(str(regime))
    except Exception:
        return 1.0
    return _IR_POSITION_FACTORS.get(r, 1.0)


def push_index_regime_context(ctx: dict) -> None:
    """把 detect 结果发布到宿主共享命名空间（若目标全局存在）。

    - globals()["INDEX_REGIME_CONTEXT"]：整包更新（仿 PREOPEN_CONTEXT 范式）；
    - globals()["SESSION_CONTEXT"]：同步 index_regime / index_regime_score 两个键，
      供 signal_engine / position_sizer 读取（设计文档 5.3）。
    独立运行时两者皆不存在，静默跳过。
    """
    g = globals()
    try:
        tgt = g.get("INDEX_REGIME_CONTEXT")
        if isinstance(tgt, dict) and isinstance(ctx, dict):
            tgt.clear()
            tgt.update(ctx)
    except Exception as e:  # 发布失败不影响主流程
        _ir_log.warning(f"[index_regime] push INDEX_REGIME_CONTEXT 失败: {e}")
    try:
        sess = g.get("SESSION_CONTEXT")
        if isinstance(sess, dict) and isinstance(ctx, dict):
            sess["index_regime"] = ctx.get("regime")
            sess["index_regime_score"] = ctx.get("score")
    except Exception as e:
        _ir_log.warning(f"[index_regime] sync SESSION_CONTEXT 失败: {e}")


# ============================================================================
# 小工具
# ============================================================================

def _ir_f(x, nd: int = 4) -> Optional[float]:
    """安全转 float（numpy 标量 → python float；NaN/inf → None）"""
    try:
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, nd)
    except Exception:
        return None


def _ir_sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


def _ir_clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _ir_pct_rank(values, x: float) -> Optional[float]:
    """x 在 values 中的分位（0~1，含自身口径），数据不足返回 None"""
    arr = np.asarray(list(values), dtype=float)
    arr = arr[~np.isnan(arr)]
    if arr.size < 20 or x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return float((arr <= x).mean())


def _ir_json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        v = float(o)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(o, (np.ndarray,)):
        return o.tolist()
    if isinstance(o, (datetime,)):
        return o.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(o, Enum):
        return o.value
    return str(o)


# ============================================================================
# 状态持久化（state.json / breadth 落库 / traces jsonl）
# ============================================================================

def _ir_state_path(d: str) -> str:
    return os.path.join(d, "state.json")


def _ir_default_k_anchor() -> Dict[str, Any]:
    """K-down 空头锚点持久化状态（V2.1）。active=False 即无锚点。"""
    return {"active": False, "below_days": 0, "recover_days": 0, "start_date": None}


def _ir_default_k_up() -> Dict[str, Any]:
    """K-up 跃迁持久化状态（V2.1）。active=True 且多头 streak 存续期间，
    T1 等效天数 = real + boost（曲线封顶后自然并入）；streak 中断/转向即清除。"""
    return {"active": False, "boost": 0, "start_date": None}


def _ir_default_sharp() -> Dict[str, Any]:
    """V2.2 锐化触发-衰减携带状态。active=False 即无携带。
    value=触发日 sharp_s（带符号锁存值）；age=触发后经过的交易日数；
    regime_at_trigger=触发当日状态机迁移后的状态（状态切换清零的基准，
    由调用方在当日迁移完成后回填）。"""
    return {"active": False, "value": 0.0, "age": 0, "direction": 0,
            "trigger_date": None, "regime_at_trigger": None}


def _ir_default_state() -> Dict[str, Any]:
    # 设计契约字段：last_regime / days_in_regime / score_history / last_date
    # history 为实现扩展（支撑 EMA 续算、幂等重跑回卷），不影响外部读取契约字段。
    return {
        "last_regime": IndexRegime.RANGE.value,
        "days_in_regime": 0,
        "score_history": [],          # 最近10个交易日 [{date, S}]
        "last_date": None,
        "history": [],                # 最近40个交易日 {date,S,sadj,regime,days_in_regime,k_anchor?}
        "k_anchor": _ir_default_k_anchor(),   # V2.1 空头锚点状态
        "k_up": _ir_default_k_up(),           # V2.1 K-up 跃迁状态
        "sharp": _ir_default_sharp(),         # V2.2 锐化触发-衰减携带状态
    }


def _ir_load_state(d: str) -> Dict[str, Any]:
    path = _ir_state_path(d)
    st = _ir_default_state()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict):
                st.update(old)
    except Exception as e:
        _ir_log.warning(f"[index_regime] state.json 读取失败，按初始状态继续: {e}")
    if not isinstance(st.get("history"), list):
        st["history"] = []
    if not isinstance(st.get("score_history"), list):
        st["score_history"] = []
    if not isinstance(st.get("k_anchor"), dict):      # V2 旧 state.json 兼容
        st["k_anchor"] = _ir_default_k_anchor()
    if not isinstance(st.get("k_up"), dict):
        st["k_up"] = _ir_default_k_up()
    if not isinstance(st.get("sharp"), dict):         # V2.1 旧 state.json 兼容
        st["sharp"] = _ir_default_sharp()
    return st


def _ir_save_state(d: str, st: Dict[str, Any]) -> None:
    try:
        os.makedirs(d, exist_ok=True)
        path = _ir_state_path(d)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2, default=_ir_json_default)
        os.replace(tmp, path)
    except Exception as e:
        _ir_log.warning(f"[index_regime] state.json 写入失败: {e}")


def _ir_rewind_state(st: Dict[str, Any], date_str: str) -> Dict[str, Any]:
    """幂等重跑：若 history 中已存在 date_str（或之后）的记录，回卷到其前一交易日快照。"""
    hist = st.get("history") or []
    cut = None
    for i, rec in enumerate(hist):
        if rec.get("date") == date_str:
            cut = i
            break
    if cut is None:
        return st
    hist = hist[:cut]
    st["history"] = hist
    if hist:
        last = hist[-1]
        st["last_regime"] = last.get("regime", IndexRegime.RANGE.value)
        st["days_in_regime"] = int(last.get("days_in_regime", 0))
        st["last_date"] = last.get("date")
        ka = last.get("k_anchor")                      # V2.1：锚点状态随快照回卷
        st["k_anchor"] = dict(ka) if isinstance(ka, dict) else _ir_default_k_anchor()
        ku = last.get("k_up")                          # V2.1：K-up 跃迁状态随快照回卷
        st["k_up"] = dict(ku) if isinstance(ku, dict) else _ir_default_k_up()
        sh = last.get("sharp")                         # V2.2：锐化携带状态随快照回卷
        st["sharp"] = dict(sh) if isinstance(sh, dict) else _ir_default_sharp()
    else:
        st.update(_ir_default_state())
        st["history"] = []
    st["score_history"] = [{"date": r.get("date"), "S": r.get("S")} for r in hist[-10:]]
    return st


def _ir_append_trace(d: str, date_str: str, record: Dict[str, Any]) -> None:
    """每日完整评分记录追加到 traces/index_regime_{date}.jsonl（仿 shadow_signals 惯例）"""
    try:
        tdir = os.path.join(d, "traces")
        os.makedirs(tdir, exist_ok=True)
        path = os.path.join(tdir, f"index_regime_{date_str}.jsonl")
        rec = {"ts": _ir_now().strftime("%Y-%m-%d %H:%M:%S"), **record}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=_ir_json_default) + "\n")
    except Exception as e:
        _ir_log.warning(f"[index_regime] trace 写入失败: {e}")


def _ir_breadth_path(d: str, date_str: str) -> str:
    return os.path.join(d, f"breadth_{date_str}.json")


def _ir_save_breadth(d: str, date_str: str, patch: Dict[str, Any]) -> None:
    """落库当日广度快照（涨跌家数 / 涨跌停池），按日期文件合并写入"""
    try:
        os.makedirs(d, exist_ok=True)
        path = _ir_breadth_path(d, date_str)
        old: Dict[str, Any] = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f) or {}
        old.update({k: v for k, v in patch.items() if v is not None})
        old["date"] = date_str
        old["saved_at"] = _ir_now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(old, f, ensure_ascii=False, indent=2, default=_ir_json_default)
    except Exception as e:
        _ir_log.warning(f"[index_regime] breadth 落库失败 {date_str}: {e}")


def _ir_load_breadth_series(d: str, date_str: str) -> List[Dict[str, Any]]:
    """读取所有 ≤ date_str 的本地广度快照（按日期升序）"""
    out: List[Dict[str, Any]] = []
    try:
        if not os.path.isdir(d):
            return out
        for fn in os.listdir(d):
            if not (fn.startswith("breadth_") and fn.endswith(".json")):
                continue
            dt = fn[len("breadth_"):-len(".json")]
            if dt > date_str:
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as f:
                    rec = json.load(f)
                if isinstance(rec, dict) and rec.get("up_ratio") is not None:
                    out.append(rec)
            except Exception:
                continue
    except Exception:
        pass
    out.sort(key=lambda r: r.get("date", ""))
    return out


# ============================================================================
# 数据获取层（全部外部调用集中于此，带重试 + 降级链）
# ============================================================================

def _ir_http_get_json(url: str, p: Dict[str, Any]) -> Optional[dict]:
    import requests
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://finance.qq.com/",
    }
    max_retry = max(1, int(p["http_retry"]))
    for attempt in range(max_retry):
        try:
            r = requests.get(url, headers=headers, timeout=int(p["http_timeout"]))
            r.raise_for_status()
            return r.json()
        except Exception as e:
            is_last = attempt + 1 >= max_retry
            if is_last:
                _ir_log.warning(f"[index_regime] http 全部{max_retry}次重试失败，最后错误: {type(e).__name__}")
            else:
                _ir_log.debug(f"[index_regime] http 重试{attempt + 1}/{max_retry}: {type(e).__name__}")
                time.sleep(float(p["http_retry_sleep"]))
    return None


def _ir_call_with_timeout(func, timeout_s: float):
    """硬限时执行外部调用（akshare 内部 requests 无超时，曾实测挂起 90s+）。

    守护线程 + join(timeout)：超时/异常返回 (None, err)，正常返回 (value, None)。
    超时线程被遗弃但仍为 daemon，不阻塞进程退出。
    """
    import threading
    box: Dict[str, Any] = {}

    def _runner():
        try:
            box["v"] = func()
        except Exception as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(max(1.0, float(timeout_s)))
    if t.is_alive():
        return None, TimeoutError(f"call exceeded {timeout_s}s")
    if "e" in box:
        return None, box["e"]
    return box.get("v"), None


def _ir_fetch_index_daily_tx(symbol: str, end_date: str, count: int, p: Dict[str, Any]) -> Optional[pd.DataFrame]:
    """腾讯 fqkline（主源）。行格式 [date, open, close, high, low, volume, (amount), ...]"""
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    start = (end_dt - timedelta(days=int(count * 1.6) + 40)).strftime("%Y-%m-%d")
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={symbol},day,{start},{end_date},{count},qfq")
    js = _ir_http_get_json(url, p)
    if not js:
        return None
    try:
        node = js["data"][symbol]
        rows = node.get("qfqday") or node.get("day") or []
        recs = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue  # 跳过末位元数据 dict 等非K线行
            try:
                amt = float(row[6]) if len(row) >= 7 and str(row[6]).replace(".", "").isdigit() else float(row[5])
                recs.append({
                    "date": str(row[0])[:10],
                    "open": float(row[1]), "close": float(row[2]),
                    "high": float(row[3]), "low": float(row[4]),
                    "volume": float(row[5]), "amount": amt,
                })
            except Exception:
                continue
        if not recs:
            return None
        df = pd.DataFrame(recs).drop_duplicates(subset="date", keep="last")
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception as e:
        _ir_log.info(f"[index_regime] 腾讯 {symbol} 解析失败: {e}")
        return None


def _ir_fetch_index_daily_ak(symbol: str, end_date: str, timeout_s: float = 20.0) -> Optional[pd.DataFrame]:
    """akshare 新浪日线（备源，无 amount → amount 退化 volume），硬限时防挂起"""
    def _call():
        import akshare as ak
        return ak.stock_zh_index_daily(symbol=symbol)

    df, err = _ir_call_with_timeout(_call, timeout_s)
    if err is not None:
        _ir_log.info(f"[index_regime] akshare {symbol} 日线失败: {type(err).__name__}")
        return None
    try:
        if df is None or len(df) == 0:
            return None
        df = df.copy()
        df["date"] = df["date"].astype(str).str[:10]
        for c in ("open", "high", "low", "close", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["amount"] = df["volume"]
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        return df[df["date"] <= end_date].reset_index(drop=True)
    except Exception as e:
        _ir_log.info(f"[index_regime] akshare {symbol} 日线失败: {type(e).__name__}")
        return None


def _ir_fetch_index_daily(symbol: str, end_date: str, count: int, p: Dict[str, Any]) -> Tuple[Optional[pd.DataFrame], str]:
    df = _ir_fetch_index_daily_tx(symbol, end_date, count, p)
    if df is not None and len(df) > 0:
        return df[df["date"] <= end_date].reset_index(drop=True), "tencent"
    df = _ir_fetch_index_daily_ak(symbol, end_date)
    if df is not None and len(df) > 0:
        return df, "akshare_sina"
    return None, "unavailable"


def _ir_fetch_qvix(end_date: str, p: Dict[str, Any]) -> Tuple[Optional[pd.Series], str]:
    """50ETF QVIX（主）→ 300ETF QVIX（备）。返回 (按日期升序的 close Series, source)，硬限时"""
    for func_name, tag in (("index_option_50etf_qvix", "qvix50"), ("index_option_300etf_qvix", "qvix300")):
        def _call(fname=func_name):
            import akshare as ak
            func = getattr(ak, fname, None)
            if func is None:
                raise AttributeError(fname)
            return func()

        df, err = _ir_call_with_timeout(_call, float(p["http_timeout"]))
        if err is not None:
            _ir_log.info(f"[index_regime] {func_name} 失败: {type(err).__name__}")
            continue
        try:
            if df is None or len(df) == 0:
                continue
            df = df.copy()
            df["date"] = df["date"].astype(str).str[:10]
            close = pd.to_numeric(df["close"], errors="coerce")
            s = pd.Series(close.values, index=df["date"].values).dropna().sort_index()
            s = s[s.index <= end_date]
            if len(s) > 0:
                return s, tag
        except Exception as e:
            _ir_log.info(f"[index_regime] {func_name} 解析失败: {type(e).__name__}")
    return None, "unavailable"


def _ir_fetch_spot_breadth(p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """当日全市场涨跌家数（ak.stock_zh_a_spot_em 自算；东财接口有 SSL 抖动史，
    单次硬限时 http_timeout，总重试 http_retry 次）"""
    def _call():
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        if df is None or len(df) == 0 or "涨跌幅" not in df.columns:
            raise ValueError("spot_em 返回异常")
        chg = pd.to_numeric(df["涨跌幅"], errors="coerce")
        up = int((chg > 0).sum())
        down = int((chg < 0).sum())
        flat = int((chg == 0).sum())
        if up + down <= 0:
            raise ValueError("涨跌家数全零")
        return {"up": up, "down": down, "flat": flat,
                "up_ratio": up / (up + down), "source": "spot_em"}

    max_retry = max(1, int(p["http_retry"]))
    base_sleep = float(p["http_retry_sleep"])
    for attempt in range(max_retry):
        rec, err = _ir_call_with_timeout(_call, float(p["http_timeout"]))
        if err is None:
            return rec
        is_last = attempt + 1 >= max_retry
        if is_last:
            _ir_log.warning(f"[index_regime] spot_em 全部{max_retry}次重试失败，最后错误: {type(err).__name__}")
        else:
            _ir_log.debug(f"[index_regime] spot_em 重试{attempt + 1}/{max_retry}: {type(err).__name__}")
            time.sleep(base_sleep * (attempt + 1))  # 指数退避
    return None


# —— E5 涨跌停池：同花顺涨停聚焦（主，V2）+ 东财三池（备，近3周）——

_IR_THS_URL = "http://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
_IR_THS_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36",
]


def _ir_e5_cache_path(p: Dict[str, Any]) -> str:
    """THS 按日期缓存路径：参数 e5_ths_cache 优先，默认 <state_dir>/e5_ths_cache.json"""
    c = p.get("e5_ths_cache")
    if c:
        return str(c)
    return os.path.join(_ir_state_dir(p), "e5_ths_cache.json")


def _ir_fetch_limit_counts_ths(date_yyyymmdd: str, timeout: float = 12.0,
                               retries: int = 2, cache_path: Optional[str] = None
                               ) -> Optional[Dict[str, Any]]:
    """同花顺涨停聚焦 dataapi（移植自 e5_ths_source.py，生产就绪）。

    一次请求给齐：收盘涨停/跌停家数、炸板家数(open_num)、触板总数(history_num)、
    炸板率 zb_rate=zb/touched。历史深度实测 >=8 个月。
    成功返回 dict；彻底失败返回 None（调用方走降级）。非交易日 today 全 0（勿采信）。
    cache_path：按日期 append-only 缓存成功结果，避免重复请求触发反爬。
    """
    import requests
    cache: Dict[str, Any] = {}
    if cache_path:
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            hit = cache.get(date_yyyymmdd)
            if isinstance(hit, dict) and hit.get("zt_count") is not None:
                return hit
        except Exception:
            cache = {}

    params = {"page": 1, "limit": 1, "date": date_yyyymmdd, "_": int(time.time() * 1000)}
    for attempt in range(max(1, int(retries) + 1)):
        headers = {
            "User-Agent": random.choice(_IR_THS_UAS),
            "Referer": "http://data.10jqka.com.cn/",
            "cookie": "v=AaBb1234567890abcdefghijklmnopqrstuv",  # 任意值即可过网关
        }
        try:
            r = requests.get(_IR_THS_URL, params=params, headers=headers, timeout=timeout)
            if r.status_code != 200 or not r.text:
                _ir_log.info(f"[index_regime] ths_pool({date_yyyymmdd}) http {r.status_code}")
                time.sleep(0.5 + attempt)
                continue
            j = json.loads(r.text)
            if j.get("status_code") != 0:
                _ir_log.info(f"[index_regime] ths_pool({date_yyyymmdd}) status={j.get('status_code')}")
                time.sleep(0.5 + attempt)
                continue
            data = j.get("data") or {}
            up = (data.get("limit_up_count") or {}).get("today") or {}
            down = (data.get("limit_down_count") or {}).get("today") or {}
            zt, dt = up.get("num"), down.get("num")
            if zt is None or dt is None:
                time.sleep(0.5 + attempt)
                continue
            touched, zb = up.get("history_num"), up.get("open_num")
            out = {
                "date": date_yyyymmdd,
                "zt_count": int(zt),
                "dt_count": int(dt),
                "zb_count": (int(zb) if zb is not None else None),
                "zt_touched": (int(touched) if touched is not None else None),
                "zb_rate": (round(zb / touched, 4) if (zb is not None and touched) else None),
                "seal_rate": up.get("rate"),
                "source": "ths_limit_up_pool",
            }
            if cache_path:
                cache[date_yyyymmdd] = out
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(cache_path)), exist_ok=True)
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(cache, f, ensure_ascii=False, indent=1)
                except Exception:
                    pass
            return out
        except Exception as e:  # 超时/SSL/JSON 等
            _ir_log.info(f"[index_regime] ths_pool({date_yyyymmdd}) {type(e).__name__}")
            time.sleep(0.5 + attempt)
    return None


def _ir_fetch_limit_pools_em(date_yyyymmdd: str, p: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """东财三池（V1 原路径，保留作近 ~3 周兜底）。跌停池仅近30交易日，
    超期 akshare 抛 ValueError → None（降级）；每个调用硬限时 http_timeout"""
    out: Dict[str, Optional[int]] = {"zt_count": None, "dt_count": None, "zb_count": None}
    for func_name, key in (("stock_zt_pool_em", "zt_count"),
                           ("stock_zt_pool_dtgc_em", "dt_count"),
                           ("stock_zt_pool_zbgc_em", "zb_count")):
        def _call(fname=func_name):
            import akshare as ak
            func = getattr(ak, fname, None)
            if func is None:
                raise AttributeError(fname)
            return func(date=date_yyyymmdd)

        df, err = _ir_call_with_timeout(_call, float(p["http_timeout"]))
        if err is not None:
            _ir_log.info(f"[index_regime] {func_name}({date_yyyymmdd}) 失败: {type(err).__name__}")
            continue
        out[key] = int(len(df)) if df is not None else 0
    return out


def _ir_fetch_limit_pools(date_yyyymmdd: str, p: Dict[str, Any]) -> Dict[str, Any]:
    """E5 涨跌停池统一入口（V2）。

    主：同花顺 limit_up_pool（e5_source="ths"，含 zb_rate，>=8 个月历史）；
    备：东财三池（e5_em_fallback=True 时，近 ~3 周交叉校验/兜底）。
    返回 {"zt_count","dt_count","zb_count","zb_rate","source"}，全缺则计数全 None。
    """
    out: Dict[str, Any] = {"zt_count": None, "dt_count": None, "zb_count": None,
                           "zb_rate": None, "source": None}
    if str(p.get("e5_source", "ths")).lower() == "ths":
        rec = _ir_fetch_limit_counts_ths(date_yyyymmdd,
                                         timeout=float(p["http_timeout"]),
                                         retries=int(p["http_retry"]),
                                         cache_path=_ir_e5_cache_path(p))
        if rec is not None:
            zt, dt = rec.get("zt_count"), rec.get("dt_count")
            if not (zt == 0 and dt == 0):        # 全 0 = 非交易日/异常返回，不采信
                out.update(zt_count=zt, dt_count=dt, zb_count=rec.get("zb_count"),
                           zb_rate=rec.get("zb_rate"), source="ths_limit_up_pool")
                return out
            _ir_log.info(f"[index_regime] ths_pool({date_yyyymmdd}) 全 0（非交易日?），转兜底")
        else:
            _ir_log.info(f"[index_regime] ths_pool({date_yyyymmdd}) 失败，转东财兜底")
    if bool(p.get("e5_em_fallback", True)):
        em = _ir_fetch_limit_pools_em(date_yyyymmdd, p)
        for k in ("zt_count", "dt_count", "zb_count"):
            if em.get(k) is not None:
                out[k] = em[k]
        if any(em.get(k) is not None for k in ("zt_count", "dt_count", "zb_count")):
            out["source"] = "em_pools"
    return out


def _ir_fetch_nhnl(date_str: str, p: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """60日新高新低家数。

    ak.stock_a_high_low_statistics 在当前 akshare 版本（1.18.60）已失效，
    数据层默认走降级：返回 None → E2 score=0 且 detail 标 degraded。
    TODO(三期)：自建 60 日新高新低宽度库（可复用三度猎手个股数据），
    接通后返回 {"nh60": int, "nl60": int, "diff_series": [(date, nh-nl), ...]}。
    """
    return None


# ============================================================================
# 指标数学库（全部手写 numpy/pandas，不引入 talib）
# ============================================================================

def _ir_ma(s: pd.Series, w: int) -> pd.Series:
    return s.rolling(w, min_periods=w).mean()


def _ir_tr(df: pd.DataFrame) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def _ir_atr_wilder(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder 平滑 ATR（alpha=1/n）"""
    return _ir_tr(df).ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _ir_adx(df: pd.DataFrame, n: int) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """ADX(14) + ±DI，Wilder 平滑。返回 (adx, plus_di, minus_di)"""
    h, l = df["high"], df["low"]
    up_move = h.diff()
    down_move = -l.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    atr = _ir_atr_wilder(df, n).replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean() / atr
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx = dx.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()
    return adx, plus_di, minus_di


def _ir_linreg_slope_r2(y: np.ndarray) -> Tuple[float, float]:
    """一元线性回归：返回 (slope, R²)。y 为一维收盘价序列"""
    n = len(y)
    if n < 3 or np.isnan(y).any():
        return 0.0, 0.0
    x = np.arange(n, dtype=float)
    b, a = np.polyfit(x, y, 1)
    y_hat = b * x + a
    ss_res = float(((y - y_hat) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return float(b), float(_ir_clip(r2, 0.0, 1.0))


def _ir_er(close: pd.Series, n: int, smooth: int) -> Tuple[pd.Series, pd.Series]:
    """Kaufman 效率系数 ER(n) 及其 smooth 日均值"""
    change = (close - close.shift(n)).abs()
    vol = close.diff().abs().rolling(n, min_periods=n).sum()
    er = (change / vol.replace(0.0, np.nan)).clip(0.0, 1.0)
    return er, er.rolling(smooth, min_periods=smooth).mean()


def _ir_aroon_osc(df: pd.DataFrame, n: int) -> pd.Series:
    """Aroon 振荡器(n) = AroonUp − AroonDown，天然 ∈ [-100,+100]（窗口 n+1 个点）"""
    def _up(v: np.ndarray) -> float:
        return 100.0 * float(np.argmax(v)) / n          # 最高点越靠右（新）值越大
    def _down(v: np.ndarray) -> float:
        return 100.0 * float(np.argmin(v)) / n
    up = df["high"].rolling(n + 1, min_periods=n + 1).apply(_up, raw=True)
    down = df["low"].rolling(n + 1, min_periods=n + 1).apply(_down, raw=True)
    return up - down


def _ir_hurst_rs_one(closes: np.ndarray) -> Optional[float]:
    """R/S 法估算单窗 Hurst 指数。closes 长度 = hurst_window（120）"""
    rets = np.diff(np.log(np.asarray(closes, dtype=float)))
    n = rets.size
    if n < 60 or np.isnan(rets).any():
        return None
    lags = [10, 15, 20, 30, 40, 60]
    xs, ys = [], []
    for lag in lags:
        k = n // lag
        if k < 2:
            continue
        rs_list = []
        for i in range(k):
            seg = rets[i * lag:(i + 1) * lag]
            z = np.cumsum(seg - seg.mean())
            r = float(z.max() - z.min())
            s = float(seg.std(ddof=1))
            if s > 0 and r > 0:
                rs_list.append(r / s)
        if rs_list:
            xs.append(math.log(lag))
            ys.append(math.log(float(np.mean(rs_list))))
    if len(xs) < 3:
        return None
    h = float(np.polyfit(np.array(xs), np.array(ys), 1)[0])
    return _ir_clip(h, 0.0, 1.0)


def _ir_hurst_bar(close: pd.Series, window: int, smooth: int) -> Tuple[Optional[float], Optional[float]]:
    """滚动 window 日 R/S Hurst，取末 smooth 日均值 H̄。返回 (H̄, H_today)"""
    need = window + smooth - 1
    if len(close) < need:
        return None, None
    arr = close.values.astype(float)
    hs = []
    tail = smooth + 1  # 多算1个防 NaN 空洞
    for end in range(len(arr) - tail + 1, len(arr) + 1):
        h = _ir_hurst_rs_one(arr[end - window:end])
        if h is not None:
            hs.append(h)
    if not hs:
        return None, None
    h_bar = float(np.mean(hs[-smooth:]))
    return h_bar, hs[-1]


# ============================================================================
# V2 特征层：MA5/MA10 streak + R0 三元组（口径与 feature_study_v2.md §1 一致）
# ============================================================================

def _ir_streak_features(df: pd.DataFrame, p: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """MA streak / cross20 / vol_ratio / pos20 末日值。

    - streak：MA5>MA10 连续天数（正，多头）；MA5<MA10 连续天数（负，空头）；
      金叉起算、死叉清零转向，diff=0/NaN 当日清零。
    - cross20：近 20 日 MA5/MA10 交叉次数（缠绕度）。
    - vol_ratio：成交量 MA5 / MA20（上证日线 volume 口径）。
    - pos20：(close − LL20) / (HH20 − LL20) × 100。
    """
    out: Dict[str, Any] = {"ok": False, "streak": None, "cross20": None,
                           "vol_ratio": None, "pos20": None,
                           "ma5": None, "ma10": None, "ma20": None, "ma60": None,
                           "ma5_slope_pct": None, "ma5_slope_up": False, "ma5_slope_down": False,
                           "full_above_ma5": False, "full_below_ma5": False,
                           "full_above_ma5_days": None, "full_below_ma5_days": None,
                           "close_below_ma60": False, "close_above_ma60": False,
                           "close": None, "above_ma5_days": None, "below_ma5_days": None,
                           "above_ma20_days": None, "below_ma20_days": None,
                           "above_ma60_days": None, "below_ma60_days": None,
                           "up_days": None, "down_days": None,
                           "touch_ma20": False, "touch_ma60": False,
                           "break_ma20": False, "break_ma60": False}
    try:
        n = len(df)
        if n < 10:
            return out
        close, high, low, vol = df["close"], df["high"], df["low"], df["volume"]
        ma5 = close.rolling(5, min_periods=5).mean()
        ma10 = close.rolling(10, min_periods=10).mean()
        ma20 = close.rolling(20, min_periods=20).mean()
        ma60 = close.rolling(60, min_periods=60).mean()
        diff = ma5 - ma10
        sign = np.sign(diff.values.astype(float))
        streaks = np.zeros(n, dtype=int)
        cur = 0
        for i, x in enumerate(sign):
            if np.isnan(x) or x == 0:
                cur = 0
            elif x > 0:
                cur = cur + 1 if cur > 0 else 1
            else:
                cur = cur - 1 if cur < 0 else -1
            streaks[i] = cur
        out["streak"] = int(streaks[-1])
        out["ma5"] = _ir_f(ma5.iloc[-1], 2)
        out["ma10"] = _ir_f(ma10.iloc[-1], 2)
        out["close"] = _ir_f(close.iloc[-1], 2)
        if n >= 6 and not np.isnan(ma5.iloc[-2]) and ma5.iloc[-2] != 0:
            ma5_slope_pct = (float(ma5.iloc[-1]) / float(ma5.iloc[-2]) - 1.0) * 100.0
            out["ma5_slope_pct"] = _ir_f(ma5_slope_pct, 4)
            eps = float(p.get("ma5_slope_eps_pct", 0.0)) if isinstance(p, dict) else 0.0
            out["ma5_slope_up"] = bool(ma5_slope_pct > eps)
            out["ma5_slope_down"] = bool(ma5_slope_pct < -eps)
        out["full_above_ma5"] = bool(n >= 5 and float(low.iloc[-1]) > float(ma5.iloc[-1]))
        out["full_below_ma5"] = bool(n >= 5 and float(high.iloc[-1]) < float(ma5.iloc[-1]))
        if n >= 20:
            out["ma20"] = _ir_f(ma20.iloc[-1], 2)
            out["ma60"] = _ir_f(close.rolling(60, min_periods=60).mean().iloc[-1], 2)
            s_ser = pd.Series(sign, index=df.index)
            cross = ((s_ser * s_ser.shift(1)) < 0).astype(float)
            cross[s_ser.isna() | s_ser.shift(1).isna()] = np.nan
            c20 = cross.rolling(20, min_periods=20).sum().iloc[-1]
            out["cross20"] = None if (isinstance(c20, float) and math.isnan(c20)) else int(c20)
            v5 = vol.rolling(5, min_periods=5).mean().iloc[-1]
            v20 = vol.rolling(20, min_periods=20).mean().iloc[-1]
            if not (np.isnan(v5) or np.isnan(v20) or v20 <= 0):
                out["vol_ratio"] = _ir_f(v5 / v20, 4)
            hh20 = high.rolling(20, min_periods=20).max().iloc[-1]
            ll20 = low.rolling(20, min_periods=20).min().iloc[-1]
            if not (np.isnan(hh20) or np.isnan(ll20) or hh20 <= ll20):
                out["pos20"] = _ir_f((close.iloc[-1] - ll20) / (hh20 - ll20) * 100.0, 2)
            def _tail_run(series: pd.Series, cond) -> int:
                cnt = 0
                for v in reversed(series.astype(float).tolist()):
                    if cond(v):
                        cnt += 1
                    else:
                        break
                return cnt
            if n >= 60:
                out["above_ma5_days"] = _tail_run(close >= ma5, lambda x: bool(x))
                out["below_ma5_days"] = _tail_run(close < ma5, lambda x: bool(x))
                out["above_ma20_days"] = _tail_run(close >= ma20, lambda x: bool(x))
                out["below_ma20_days"] = _tail_run(close < ma20, lambda x: bool(x))
                out["above_ma60_days"] = _tail_run(close >= ma60, lambda x: bool(x))
                out["below_ma60_days"] = _tail_run(close < ma60, lambda x: bool(x))
                out["full_above_ma5_days"] = _tail_run(low > ma5, lambda x: bool(x))
                out["full_below_ma5_days"] = _tail_run(high < ma5, lambda x: bool(x))
                out["up_days"] = _tail_run(close > close.shift(1), lambda x: bool(x))
                out["down_days"] = _tail_run(close < close.shift(1), lambda x: bool(x))
                prev_ma20 = float(ma20.iloc[-2]) if n >= 21 and not np.isnan(ma20.iloc[-2]) else np.nan
                prev_ma60 = float(ma60.iloc[-2]) if n >= 61 and not np.isnan(ma60.iloc[-2]) else np.nan
                if not np.isnan(prev_ma20):
                    out["touch_ma20"] = bool(float(low.iloc[-1]) <= prev_ma20 <= float(high.iloc[-1]))
                    out["break_ma20"] = bool(float(close.iloc[-1]) < float(ma20.iloc[-1])
                                              and float(close.iloc[-2]) >= prev_ma20)
                if not np.isnan(prev_ma60):
                    out["touch_ma60"] = bool(float(low.iloc[-1]) <= prev_ma60 <= float(high.iloc[-1]))
                    out["break_ma60"] = bool(float(close.iloc[-1]) < float(ma60.iloc[-1])
                                              and float(close.iloc[-2]) >= prev_ma60)
        if n >= 60:
            out["close_below_ma60"] = bool(float(close.iloc[-1]) < float(ma60.iloc[-1]))
            out["close_above_ma60"] = bool(float(close.iloc[-1]) > float(ma60.iloc[-1]))
        out["ok"] = True
    except Exception as e:
        _ir_log.info(f"[index_regime] streak 特征计算失败: {type(e).__name__}: {e}")
    return out


def _ir_structure_score(df: pd.DataFrame, feat: Dict[str, Any], p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    """价格结构附加分：强调 MA5 持续站稳/失守，以及 MA20/MA60 关键位破位。"""
    if not feat.get("ok") or feat.get("ma5") is None or feat.get("ma20") is None:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "structure 特征不足"}, True
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    ma5 = close.rolling(5, min_periods=5).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma60 = close.rolling(60, min_periods=60).mean()
    last = len(df) - 1
    c = float(close.iloc[last])
    m5 = float(ma5.iloc[last])
    m20 = float(ma20.iloc[last])
    m60 = float(ma60.iloc[last]) if len(df) >= 60 and not np.isnan(ma60.iloc[last]) else None
    pc = float(close.iloc[last - 1]) if last >= 1 else c
    pm20 = float(ma20.iloc[last - 1]) if last >= 1 and not np.isnan(ma20.iloc[last - 1]) else None
    pm60 = float(ma60.iloc[last - 1]) if last >= 1 and len(df) >= 61 and not np.isnan(ma60.iloc[last - 1]) else None

    def _run(cond_series: pd.Series) -> int:
        nrun = 0
        for v in reversed(cond_series.astype(bool).tolist()):
            if v:
                nrun += 1
            else:
                break
        return nrun

    above5 = _run(close > ma5)
    below5 = _run(close < ma5)
    full_above5 = int(feat.get("full_above_ma5_days") or 0)
    full_below5 = int(feat.get("full_below_ma5_days") or 0)
    ma5_slope_down = bool(feat.get("ma5_slope_down"))
    ma5_slope_up = bool(feat.get("ma5_slope_up"))
    above20 = _run(close > ma20)
    below20 = _run(close < ma20)
    above60 = _run(close > ma60) if m60 is not None else 0
    below60 = _run(close < ma60) if m60 is not None else 0

    score = 0.0
    reasons = []

    if above5 >= int(p["ma5_persist_days"]):
        boost = min(12.0, 4.0 + (above5 - int(p["ma5_persist_days"])) * 2.0)
        score += boost
        reasons.append(f"above_ma5({above5})=+{boost:.1f}")
    if below5 >= int(p["ma5_persist_days"]):
        bump = min(12.0, 4.0 + (below5 - int(p["ma5_persist_days"])) * 2.0)
        score -= bump
        reasons.append(f"below_ma5({below5})=-{bump:.1f}")
    if full_above5 >= int(p.get("full_above_ma5_confirm_days", 2)):
        boost = min(16.0, float(p.get("full_above_ma5_bonus", 8)) + (full_above5 - int(p.get("full_above_ma5_confirm_days", 2))) * 2.0)
        score += boost
        reasons.append(f"full_above_ma5({full_above5})=+{boost:.1f}")
    if full_below5 >= int(p.get("full_above_ma5_confirm_days", 2)):
        bump = min(16.0, float(p.get("full_above_ma5_bonus", 8)) + (full_below5 - int(p.get("full_above_ma5_confirm_days", 2))) * 2.0)
        score -= bump
        reasons.append(f"full_below_ma5({full_below5})=-{bump:.1f}")

    if c > m20:
        score += 2.0 if above20 >= int(p["ma5_persist_days"]) else 0.0
    elif c < m20:
        if pm20 is not None and pc >= pm20:
            score -= float(p["ma20_break_bonus"])
            reasons.append("break_ma20")
        elif below20 >= int(p["ma5_persist_days"]):
            score -= max(2.0, float(p["ma20_break_bonus"]) * 0.5)
            reasons.append(f"below_ma20({below20})")

    if m60 is not None:
        if c > m60:
            if pm60 is not None and pc <= pm60:
                score += float(p["ma60_break_bonus"])
                reasons.append("reclaim_ma60")
            elif above60 >= int(p["ma5_persist_days"]):
                score += max(4.0, float(p["ma60_break_bonus"]) * 0.5)
                reasons.append(f"above_ma60({above60})")
        elif c < m60:
            if pm60 is not None and pc >= pm60:
                score -= float(p["ma60_break_bonus"])
                reasons.append("break_ma60")
            elif below60 >= 2:
                score -= max(8.0, float(p["ma60_break_bonus"]) * 0.75)
                reasons.append(f"below_ma60({below60})")
            if ma5_slope_down:
                score -= max(float(p["ma60_break_bonus"]), 22.0)
                reasons.append("ma60_break_ma5_slope_down")

        if float(low.iloc[last]) <= m20 <= float(high.iloc[last]) and c < m20:
            score -= float(p["ma_touch_bonus"])
            reasons.append("touch_reject_ma20")
        if float(low.iloc[last]) <= m60 <= float(high.iloc[last]) and c < m60:
            score -= float(p["ma_touch_bonus"]) + 2.0
            reasons.append("touch_reject_ma60")

    score = _ir_clip(score, -40.0, 40.0)
    detail = {
        "score": _ir_f(score, 2),
        "above_ma5_days": above5, "below_ma5_days": below5,
        "above_ma20_days": above20, "below_ma20_days": below20,
        "above_ma60_days": above60 if m60 is not None else None,
        "below_ma60_days": below60 if m60 is not None else None,
        "touch_ma20": bool(feat.get("touch_ma20")),
        "touch_ma60": bool(feat.get("touch_ma60")),
        "break_ma20": bool(feat.get("break_ma20")),
        "break_ma60": bool(feat.get("break_ma60")),
        "ma5_slope_pct": feat.get("ma5_slope_pct"),
        "ma5_slope_up": ma5_slope_up,
        "ma5_slope_down": ma5_slope_down,
        "full_above_ma5": bool(feat.get("full_above_ma5")),
        "full_below_ma5": bool(feat.get("full_below_ma5")),
        "full_above_ma5_days": full_above5,
        "full_below_ma5_days": full_below5,
        "close_below_ma60": bool(feat.get("close_below_ma60")),
        "close_above_ma60": bool(feat.get("close_above_ma60")),
        "reasons": reasons,
    }
    return score, detail, False


def _ir_streak_curve_value(k: int, p: Dict[str, Any]) -> float:
    """streak 第 k 天累积分（分段线性锚点插值，k>=13 → streak_cap 封顶）"""
    if k <= 0:
        return 0.0
    pts = sorted((int(d), float(s)) for d, s in p["streak_curve"])
    cap = float(p["streak_cap"])
    if k >= pts[-1][0]:
        return cap
    d0, s0 = 0, 0.0
    for d, s in pts:
        if k <= d:
            return s0 + (s - s0) * (k - d0) / float(d - d0)
        d0, s0 = d, s
    return cap


def _ir_kday_eval(df: pd.DataFrame, feat: Dict[str, Any], prev_regime: IndexRegime,
                  anchor_state: Optional[Dict[str, Any]], up_state: Optional[Dict[str, Any]],
                  p: Dict[str, Any]
                  ) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """V2.1 关键日（K-day）判定 + 空头锚点/K-up跃迁状态推进。

    返回 (key_day, new_anchor_state, new_up_state, kctx)：
    - key_day：detail 输出契约 {type, boost, anchor_active, anchor_days, reason}；
    - new_anchor_state / new_up_state：推进后的持久化状态（由调用方写入 state.json）；
    - kctx：T1 打分上下文 {k_up_eff_days, anchor40}（均为 None 表示不介入）。

    K-up（多头启动日）五条件：
      (a) 收盘同时站上 MA5/MA10 且最小余量>=k_margin%；(b) 昨日未同时站上（首次）；
      (c) 当日涨幅>=k_up_pct%；(d) 近 k_cross_bg_days 日 MA5/MA10 交叉>=k_cross_bg（缠绕背景）；
      (e) MA5>=MA10。
      跃迁在多头 streak 存续期间持续：effective_days = real + boost（曲线封顶后自然并入），
      streak 中断/转向或 K-down 触发即清除。
    K-down（空头启动日）三条件：
      (a) 此前连续>=k_ma5_up_days 日收盘在 MA5 上方；(b) 当日跌幅<=-k_down_pct% 且收破 MA5；
      (c) 背景：多头 streak>=k_bull_streak_bg 或前一状态为 uni_up。
    锚点推进：K-down 当日激活（below=1）；存续期 close<MA5 → below+1，
      close>=MA5 → recover+1，recover>=k_anchor_recover_days → 解除；K-up 当日直接解除。
    """
    key_day: Dict[str, Any] = {"type": None, "boost": int(p["k_boost"]),
                               "anchor_active": False, "anchor_days": 0, "reason": None}
    ka = dict(anchor_state) if isinstance(anchor_state, dict) else _ir_default_k_anchor()
    for k_ in ("active", "below_days", "recover_days"):
        ka.setdefault(k_, _ir_default_k_anchor()[k_])
    ka.setdefault("start_date", None)
    ku = dict(up_state) if isinstance(up_state, dict) else _ir_default_k_up()
    ku.setdefault("active", False)
    ku.setdefault("boost", int(p["k_boost"]))
    ku.setdefault("start_date", None)
    kctx: Dict[str, Any] = {"k_up_eff_days": None, "anchor40": None,
                            "cross_bg_n": None}      # V2.2.1 C2：缠绕背景交叉次数供锐化补位门控复用

    n = len(df)
    if n < 21 or not feat.get("ok"):
        key_day["anchor_active"] = bool(ka.get("active"))
        key_day["anchor_days"] = int(ka.get("below_days", 0))
        return key_day, ka, ku, kctx
    try:
        close = df["close"].values.astype(float)
        ma5 = df["close"].rolling(5, min_periods=5).mean().values.astype(float)
        ma10 = df["close"].rolling(10, min_periods=10).mean().values.astype(float)
        i = n - 1
        c, m5, m10 = close[i], ma5[i], ma10[i]
        pc, pm5, pm10 = close[i - 1], ma5[i - 1], ma10[i - 1]
        if any(math.isnan(x) for x in (c, m5, m10, pc, pm5, pm10)):
            raise ValueError("MA NaN")
        pct_chg = (c / pc - 1.0) * 100.0
        above_both = bool(c > m5 and c > m10)
        prev_above_both = bool(pc > pm5 and pc > pm10)
        margin_min = min((c / m5 - 1.0) * 100.0, (c / m10 - 1.0) * 100.0)
        # 近 k_cross_bg_days 日交叉次数（含当日）
        sign = np.sign(ma5 - ma10)
        w = int(p["k_cross_bg_days"])
        seg = sign[max(0, n - w):n]
        seg = seg[~np.isnan(seg)]
        cross_n = int(sum(1 for a, b in zip(seg[:-1], seg[1:]) if a * b < 0)) if len(seg) >= 2 else 0
        kctx["cross_bg_n"] = cross_n                 # V2.2.1 C2：供锐化补位缠绕门控复用
        # 此前连续站上 MA5 天数（截至昨日）
        prev_ma5_up = 0
        j = i - 1
        while j >= 0 and not math.isnan(ma5[j]) and close[j] > ma5[j]:
            prev_ma5_up += 1
            j -= 1
        streak = int(feat["streak"]) if feat.get("streak") is not None else 0

        # —— K-down 判定（先：uni_up 中的破位优先级最高）——
        k_down = bool(prev_ma5_up >= int(p["k_ma5_up_days"])
                      and pct_chg <= -float(p["k_down_pct"]) and c < m5
                      and (streak >= int(p["k_bull_streak_bg"]) or prev_regime == IndexRegime.UNI_UP))
        # —— K-up 判定 ——
        k_up = bool(above_both and not prev_above_both
                    and margin_min >= float(p["k_margin"])
                    and pct_chg >= float(p["k_up_pct"])
                    and cross_n >= int(p["k_cross_bg"])
                    and m5 >= m10)

        if k_down:
            ka = {"active": True, "below_days": 1, "recover_days": 0,
                  "start_date": str(df["date"].iloc[-1])}
            ku = _ir_default_k_up()                    # K-down 当日清除 K-up 跃迁
            key_day["type"] = "k_down"
            key_day["reason"] = (f"pct={pct_chg:.2f}%<=-{p['k_down_pct']}%, close<MA5, "
                                 f"prev_ma5_up={prev_ma5_up}, streak={streak}, prev_regime={prev_regime.value}")
        elif k_up:
            ka = _ir_default_k_anchor()                # K-up 当日解除空头锚点（强反转信号）
            ku = {"active": True, "boost": int(p["k_boost"]),
                  "start_date": str(df["date"].iloc[-1])}
            key_day["type"] = "k_up"
            key_day["reason"] = (f"pct=+{pct_chg:.2f}%>={p['k_up_pct']}%, margin={margin_min:.2f}%>="
                                 f"{p['k_margin']}%, cross{w}={cross_n}>={p['k_cross_bg']}, MA5>=MA10, 首次同站上")
        else:
            if ka.get("active"):
                if c < m5:
                    ka["below_days"] = int(ka.get("below_days", 0)) + 1
                    ka["recover_days"] = 0
                else:
                    ka["recover_days"] = int(ka.get("recover_days", 0)) + 1
                    if ka["recover_days"] >= int(p["k_anchor_recover_days"]):
                        ka = _ir_default_k_anchor()
            # K-up 跃迁存续检查：多头 streak 中断/转向 → 清除（等效天数并入曲线即失效）
            if ku.get("active") and streak <= 0:
                ku = _ir_default_k_up()

        # —— K-up 跃迁（触发日 + 多头 streak 存续期）：effective_days = real + boost ——
        if ku.get("active") and streak > 0:
            kctx["k_up_eff_days"] = streak + int(ku.get("boost", p["k_boost"]))
            key_day["k_up_eff_days"] = kctx["k_up_eff_days"]

        # —— 锚点存续 → T1 负向累积值（±40 量程）——
        if ka.get("active"):
            below = int(ka.get("below_days", 0))
            eff = below + int(p["k_boost"])             # 曲线自然封顶 13 天档
            kctx["anchor40"] = -_ir_streak_curve_value(eff, p)
            key_day["anchor_active"] = True
            key_day["anchor_days"] = below
        if key_day["type"] is None and not ka.get("active"):
            key_day["anchor_active"] = False
            key_day["anchor_days"] = 0
    except Exception as e:
        _ir_log.info(f"[index_regime] K-day 评估失败（按无K日继续）: {type(e).__name__}: {e}")
        key_day = {"type": None, "boost": int(p["k_boost"]),
                   "anchor_active": bool(ka.get("active")), "anchor_days": int(ka.get("below_days", 0)),
                   "reason": f"eval_error:{type(e).__name__}"}
        kctx = {"k_up_eff_days": None, "anchor40": None, "cross_bg_n": None}
    return key_day, ka, ku, kctx


def _ir_sharp_eval(df: pd.DataFrame, feat: Dict[str, Any],
                   carry_state: Optional[Dict[str, Any]], prev_regime_value: str,
                   p: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], float]:
    """V2.2 指标锐化分（SHARP）每日评估 + 触发-衰减携带推进（V2.2.1 C1 同向抑制）。

    返回 (sharp_detail, new_carry, add_s)：
    - sharp_detail：detail.sharp 输出契约（结构见模块头 V2.2 节；V2.2.1 追加
      suppressed / suppressed_side / suppressed_value / monitor / sharp_s_raw /
      sharp_net_raw）；
    - new_carry：推进后的携带状态（由调用方写入 state.json；触发日的
      regime_at_trigger 由调用方在状态机迁移完成后回填）；
    - add_s：当日实际计入 S 的分值（触发日 = sharp_s 全额；携带日 = 锁存值
      × sharp_decay^age；无携带/非触发 = 0）。

    计分（多头示例，空头完全对称；前序窗口不含当日）：
      波动突破侧 0~9（两档不叠加取高，档内子项以档成立为前提）；
      量能确认侧 0~5【默认补全】（无方向，双向同计）；
      均线状态侧 0~8【默认补全】。
    V2.2.1 C1 同向触发抑制（sharp_suppress_same_dir）：uni_up 只监测空头锐化
      （up 侧 suppressed，有效 net = -dn）、uni_down 只监测多头锐化（有效 net = +up）、
      range 双向（维持 conflict 净值机制）。被抑制侧分值仍照常计算并保留在
      detail.sharp（suppressed=true 等字段，sharp_s_raw 记原始值），但不计入 S、
      不参与触发、不重置衰减——同向延续日不再反复重置 carry，衰减真正生效。
    触发-衰减：|sharp_s|>=sharp_trigger → 触发（age=0 全额）；其后每交易日
      ×sharp_decay；新触发重置；prev_regime 与触发日 regime 不同 → 状态切换清零；
      |add_s|<0.5 → 自然熄灭。纯本地 OHLC/feat 计算，无网络依赖；异常按无锐化降级。
    """
    carry = dict(carry_state) if isinstance(carry_state, dict) else _ir_default_sharp()
    for k_, v_ in _ir_default_sharp().items():
        carry.setdefault(k_, v_)
    detail: Dict[str, Any] = {
        "sharp_up": 0, "sharp_down": 0, "sharp_net": 0, "sharp_s": 0.0,
        "tier": {"up": None, "down": None},
        "parts": {"up": {"breakout": 0, "close_confirm": 0, "gap": 0, "vol": 0, "ma": 0},
                  "down": {"breakout": 0, "close_confirm": 0, "gap": 0, "vol": 0, "ma": 0}},
        "age_days": None, "decayed_s": 0.0, "triggered": False, "conflict": False,
        "carry_active": bool(carry.get("active")), "k_link": None,
        "vol_ratio": feat.get("vol_ratio"), "carry_cleared": None,
        "suppressed": False, "suppressed_side": None, "suppressed_value": 0,
        "monitor": "both", "sharp_s_raw": 0.0, "sharp_net_raw": 0,
    }
    add_s = 0.0
    n = len(df)
    if n < 12 or not feat.get("ok"):
        detail["note"] = "bars<12 或 streak 特征不足，锐化按 0 处理"
        return detail, carry, add_s
    try:
        o = float(df["open"].iloc[-1])
        h = float(df["high"].iloc[-1])
        lo = float(df["low"].iloc[-1])
        c = float(df["close"].iloc[-1])
        pc = float(df["close"].iloc[-2])
        w5 = df.iloc[-6:-1]                                # 前 5 日（不含当日）
        w3 = df.iloc[-4:-1]                                # 前 3 日（不含当日）
        hh5, hc5 = float(w5["high"].max()), float(w5["close"].max())
        ll5, lc5 = float(w5["low"].min()), float(w5["close"].min())
        hh3, hc3 = float(w3["high"].max()), float(w3["close"].max())
        ll3, lc3 = float(w3["low"].min()), float(w3["close"].min())
        ma5, ma10 = feat.get("ma5"), feat.get("ma10")
        gap_up = bool(o > pc)                              # 竞价高开口径：open vs prev_close
        gap_dn = bool(o < pc)

        # —— 波动突破侧（0~9）：5日档/3日档不叠加取高；档内子项以档成立为前提 ——
        def _up_tier() -> Tuple[int, int, int, Optional[str]]:
            bo5 = int(p["sharp_bo5_high"]) if h > hh5 else 0
            t5 = (bo5,
                  int(p["sharp_bo5_close"]) if (bo5 and c > hc5) else 0,
                  int(p["sharp_gap"]) if (bo5 and gap_up) else 0)
            bo3 = int(p["sharp_bo3_high"]) if h > hh3 else 0
            t3 = (bo3,
                  int(p["sharp_bo3_close"]) if (bo3 and c > hc3) else 0,
                  int(p["sharp_gap"]) if (bo3 and gap_up) else 0)
            s5, s3 = sum(t5), sum(t3)
            if s5 == 0 and s3 == 0:
                return 0, 0, 0, None
            return (*t5, "5d") if s5 >= s3 else (*t3, "3d")

        def _dn_tier() -> Tuple[int, int, int, Optional[str]]:
            bo5 = int(p["sharp_bo5_high"]) if lo < ll5 else 0
            t5 = (bo5,
                  int(p["sharp_bo5_close"]) if (bo5 and c < lc5) else 0,
                  int(p["sharp_gap"]) if (bo5 and gap_dn) else 0)
            bo3 = int(p["sharp_bo3_high"]) if lo < ll3 else 0
            t3 = (bo3,
                  int(p["sharp_bo3_close"]) if (bo3 and c < lc3) else 0,
                  int(p["sharp_gap"]) if (bo3 and gap_dn) else 0)
            s5, s3 = sum(t5), sum(t3)
            if s5 == 0 and s3 == 0:
                return 0, 0, 0, None
            return (*t5, "5d") if s5 >= s3 else (*t3, "3d")

        bo_u, cc_u, gap_u_sc, tier_u = _up_tier()
        bo_d, cc_d, gap_d_sc, tier_d = _dn_tier()

        # —— 量能确认侧（0~5）【默认补全】：vol_ma5/vol_ma20 取高档，无方向双向同计 ——
        vol_sc = 0
        vr = feat.get("vol_ratio")
        if vr is not None:
            if float(vr) >= float(p["sharp_vol_15"]):
                vol_sc = int(p["sharp_vol_hi_score"])
            elif float(vr) >= float(p["sharp_vol_12"]):
                vol_sc = int(p["sharp_vol_lo_score"])

        # —— 均线状态侧（0~8）【默认补全】——
        ma_u = ma_d = 0
        if ma5 is not None and ma10 is not None:
            # V2.2.5: 加入 0.2% 容差，避免暴力反弹刚触及均线时漏分
            _ma5_t = ma5 * 0.998
            _ma10_t = ma10 * 0.998
            if c > ma5:
                ma_u += int(p["sharp_ma5"])
            elif c > _ma5_t:
                ma_u += int(p["sharp_ma5"]) // 2  # 触及附近给半额分
            if c > ma10:
                ma_u += int(p["sharp_ma10"])
            elif c > _ma10_t:
                ma_u += int(p["sharp_ma10"]) // 2
            if c < ma5:
                ma_d += int(p["sharp_ma5"])
            elif c < ma5 * 1.002:
                ma_d += int(p["sharp_ma5"]) // 2
            if c < ma10:
                ma_d += int(p["sharp_ma10"])
            elif c < ma10 * 1.002:
                ma_d += int(p["sharp_ma10"]) // 2

        up = bo_u + cc_u + gap_u_sc + vol_sc + ma_u   # 【V2.2.2 C5】档内子项(收破cc/竞价gap)计入聚合
        dn = bo_d + cc_d + gap_d_sc + vol_sc + ma_d   # 【V2.2.2 C5】对称修复（原误只用档基分 bo，波动侧实际封顶 5/规格 9）
        conflict_raw = bool(up > 5 and dn > 5)             # 同日双侧>5 → 取净值
        net_raw = (up - dn) if conflict_raw else (up if up >= dn else -dn)

        # —— V2.2.1 C1 同向触发抑制：uni_up 只监测空头锐化、uni_down 只监测多头锐化、
        #    range 双向。被抑制侧不计入 S、不参与触发、不重置衰减（分值保留在 detail）——
        suppressed_side: Optional[str] = None
        if bool(p.get("sharp_suppress_same_dir", True)):
            if prev_regime_value == IndexRegime.UNI_UP.value and up > 0:
                suppressed_side = "up"
            elif prev_regime_value == IndexRegime.UNI_DOWN.value and dn > 0:
                suppressed_side = "down"
        if suppressed_side == "up":
            monitor, conflict, net = "down_only", False, -dn
        elif suppressed_side == "down":
            monitor, conflict, net = "up_only", False, up
        else:
            monitor, conflict, net = "both", conflict_raw, net_raw

        full = float(p["sharp_full"])
        map_max = float(p["sharp_map_max"])
        sharp_s_raw = _ir_clip(net_raw / full * map_max, -map_max, map_max) if full > 0 else 0.0
        sharp_s = _ir_clip(net / full * map_max, -map_max, map_max) if full > 0 else 0.0
        triggered = bool(abs(sharp_s) >= float(p["sharp_trigger"]))

        # —— 触发-衰减携带推进 ——
        age_days: Optional[int] = None
        if triggered:
            carry = {"active": True, "value": sharp_s, "age": 0,
                     "direction": _ir_sign(sharp_s),
                     "trigger_date": str(df["date"].iloc[-1]),
                     "regime_at_trigger": None}            # 调用方迁移后回填
            add_s = sharp_s
            age_days = 0
        elif carry.get("active"):
            if carry.get("regime_at_trigger") and prev_regime_value != carry.get("regime_at_trigger"):
                carry = _ir_default_sharp()                # 状态切换清零
                detail["carry_cleared"] = "regime_switch"
            else:
                carry["age"] = int(carry.get("age", 0)) + 1
                age_days = carry["age"]
                add_s = float(carry.get("value", 0.0)) * (float(p["sharp_decay"]) ** age_days)
                if abs(add_s) < 0.5:                       # 衰减自然熄灭
                    carry = _ir_default_sharp()
                    detail["carry_cleared"] = "decayed_out"
                    add_s = 0.0

        detail.update({
            "sharp_up": int(up), "sharp_down": int(dn), "sharp_net": int(net),
            "sharp_s": _ir_f(sharp_s, 2),
            "tier": {"up": tier_u, "down": tier_d},
            "parts": {
                "up": {"breakout": bo_u, "close_confirm": cc_u, "gap": gap_u_sc,
                       "vol": vol_sc, "ma": ma_u},
                "down": {"breakout": bo_d, "close_confirm": cc_d, "gap": gap_d_sc,
                         "vol": vol_sc, "ma": ma_d},
            },
            "age_days": age_days, "decayed_s": _ir_f(add_s, 2),
            "triggered": triggered, "conflict": conflict,
            "carry_active": bool(carry.get("active")),
            "carry_value": _ir_f(carry.get("value"), 2) if carry.get("active") else None,
            "suppressed": bool(suppressed_side),
            "suppressed_side": suppressed_side,
            "suppressed_value": int(up if suppressed_side == "up"
                                    else dn if suppressed_side == "down" else 0),
            "monitor": monitor,
            "sharp_s_raw": _ir_f(sharp_s_raw, 2),
            "sharp_net_raw": int(net_raw),
        })
    except Exception as e:
        _ir_log.info(f"[index_regime] SHARP 评估失败（按无锐化继续）: {type(e).__name__}: {e}")
        detail["note"] = f"eval_error:{type(e).__name__}"
        add_s = 0.0
    return detail, carry, add_s


# ============================================================================
# 趋势维度打分（T1~T5，各自输出 [-100,+100] + detail；数据不足 → degraded）
# ============================================================================

def _ir_score_ma_streak(feat: Dict[str, Any], p: Dict[str, Any],
                        kctx: Optional[Dict[str, Any]] = None) -> Tuple[float, Dict[str, Any], bool]:
    """T1【MA5>MA10 streak 累积分】（V2 主导因子，权重 40%，量程 ±40 归一到 ±100）。

    - 分段线性曲线：第1/3/5/8/10/13天 = +8/+16/+24/+32/+36/+40 封顶，空头对称；
    - R1：多头 streak 中收盘跌破 MA5 → 当日累积分减半（不退出）；收复 MA5 自动恢复；
      空头 streak 中收复 MA5 对称减半；
    - 晚期警示：|streak|>=streak_late_day → R1 惩罚 ×late_penalty_mult（扣减 100%）。
    - V2.1 K-up 跃迁：等效 streak 天数 = real + k_boost（曲线封顶），当日分数跃迁；
    - V2.1 K-down 空头锚点：kctx.anchor40（<=0）与正常 streak 分取更负值（死叉后无缝接管）。
    - V2.2.4 价格结构强化：连续站上/站下 MA5 以及 MA20/MA60 破位会额外强化方向分。
    输出分按 ±40 → ±100 线性放大，与其余趋势因子同量程参与加权。
    """
    if not feat.get("ok") or feat.get("streak") is None:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "streak 特征不足(bars<10)"}, True
    streak = int(feat["streak"])
    k = abs(streak)
    sgn = _ir_sign(streak)
    base = _ir_streak_curve_value(k, p) if k > 0 else 0.0
    late = bool(k >= int(p["streak_late_day"]) and k > 0)
    keep = 1.0
    r1 = False
    close, ma5 = feat.get("close"), feat.get("ma5")
    if k > 0 and close is not None and ma5 is not None:
        broke = (streak > 0 and close < ma5) or (streak < 0 and close > ma5)
        if broke:
            r1 = True
            mult = float(p["late_penalty_mult"]) if late else 1.0
            keep = max(0.0, 1.0 - float(p["r1_half_factor"]) * mult)
    raw40 = sgn * base * keep                          # ±40 量程
    k_eff_days = None
    anchor40 = None
    if kctx:
        # K-up 跃迁：第 1 天按第 (1+k_boost) 天档计，随后 real+boost 自然并入曲线
        if kctx.get("k_up_eff_days") is not None:
            k_eff_days = int(kctx["k_up_eff_days"])
            raw40 = _ir_streak_curve_value(k_eff_days, p)   # K-up 当日无 R1（收在 MA5 上方）
        # 空头锚点：与正常/R1 后 streak 分取更负值（K-down 当日多头分清零后仍被锚点压负）
        if kctx.get("anchor40") is not None:
            anchor40 = float(kctx["anchor40"])
            raw40 = min(raw40, anchor40)
    score = _ir_clip(raw40 / float(p["streak_cap"]) * 100.0, -100.0, 100.0)
    detail = {"score": _ir_f(score, 2), "streak": streak,
              "curve_score40": _ir_f(sgn * base, 2),   # 曲线分（R1 前，±40 量程）
              "streak_score40": _ir_f(raw40, 2),       # R1/K日 后实际分（±40 量程）
              "late_warning": late, "r1_hit": r1, "keep_factor": _ir_f(keep, 3),
              "ma5": ma5, "ma10": feat.get("ma10"),
              "close_vs_ma5_pct": _ir_f((close / ma5 - 1.0) * 100.0, 2)
              if (close is not None and ma5) else None}
    if k_eff_days is not None:
        detail["k_eff_days"] = k_eff_days
    if anchor40 is not None:
        detail["anchor40"] = _ir_f(anchor40, 2)
    return score, detail, False


def _ir_score_adx(df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    n = int(p["adx_len"])
    if len(df) < 3 * n + 2:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "bars<ADX"}, True
    adx, pdi, mdi = _ir_adx(df, n)
    a, u, d = adx.iloc[-1], pdi.iloc[-1], mdi.iloc[-1]
    if np.isnan(a) or np.isnan(u) or np.isnan(d):
        return 0.0, {"score": 0.0, "degraded": True, "reason": "ADX NaN"}, True
    score = _ir_sign(u - d) * min(a, 50.0) / 50.0 * 100.0
    if a < 20.0:
        score *= a / 20.0                              # ±DI 缠绕区衰减，不表态
    score = _ir_clip(score, -100.0, 100.0)
    detail = {"score": _ir_f(score, 2), "adx": _ir_f(a, 2),
              "plus_di": _ir_f(u, 2), "minus_di": _ir_f(d, 2)}
    return score, detail, False


def _ir_score_reg_r2(df: pd.DataFrame, atr: pd.Series, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    n = int(p["reg_len"])
    if len(df) < n + int(p["atr_len"]) + 2:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "bars<REG"}, True
    y = df["close"].tail(n).values.astype(float)
    b, r2 = _ir_linreg_slope_r2(y)
    a = atr.iloc[-1]
    if a is None or np.isnan(a) or a <= 0:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "ATR NaN"}, True
    slope_atr = b / float(a)
    slope_norm = _ir_clip(slope_atr, -2.0, 2.0) / 2.0
    score = _ir_clip(slope_norm * r2 * 100.0, -100.0, 100.0)
    detail = {"score": _ir_f(score, 2), "slope_atr": _ir_f(slope_atr, 3), "r2": _ir_f(r2, 3)}
    return score, detail, False


def _ir_score_er(df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    n, sm = int(p["er_len"]), int(p["er_smooth"])
    if len(df) < n + sm + 2:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "bars<ER"}, True
    close = df["close"]
    _, er_bar = _ir_er(close, n, sm)
    eb = er_bar.iloc[-1]
    d10 = close.iloc[-1] - close.iloc[-1 - n]
    if np.isnan(eb):
        return 0.0, {"score": 0.0, "degraded": True, "reason": "ER NaN"}, True
    score = _ir_clip(_ir_sign(d10) * (2.0 * float(eb) - 1.0) * 100.0, -100.0, 100.0)
    detail = {"score": _ir_f(score, 2), "er_bar": _ir_f(eb, 3)}
    return score, detail, False


def _ir_score_aroon(df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    n = int(p["aroon_len"])
    if len(df) < n + int(p["aroon_smooth"]) + 2:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "bars<AROON"}, True
    osc = _ir_aroon_osc(df, n)
    osc_e = osc.ewm(span=int(p["aroon_smooth"]), adjust=False).mean().iloc[-1]
    if np.isnan(osc_e):
        return 0.0, {"score": 0.0, "degraded": True, "reason": "AROON NaN"}, True
    score = _ir_clip(float(osc_e), -100.0, 100.0)
    return score, {"score": _ir_f(score, 2)}, False


# ============================================================================
# 环境维度打分（E1~E4）+ E5 触发式规则
# ============================================================================

def _ir_score_breadth(state_dir: str, date_str: str, is_today: bool,
                      df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    """E1 涨跌家数强度 + ADL 背离（权重35%）。

    当日：ak.stock_zh_a_spot_em() 实时统计并落库；历史：仅读本地 breadth_*.json。
    10 日 EMA 不足 10 日用已有天数并标 partial=True；0 日 → degraded。
    """
    today_rec = None
    if is_today:
        today_rec = _ir_fetch_spot_breadth(p)
        if today_rec:
            _ir_save_breadth(state_dir, date_str, today_rec)
    series = _ir_load_breadth_series(state_dir, date_str)
    if today_rec and (not series or series[-1].get("date") != date_str):
        series.append({**today_rec, "date": date_str})
    ratios = [float(r["up_ratio"]) for r in series if r.get("up_ratio") is not None]
    if not ratios:
        return 0.0, {"score": 0.0, "degraded": True,
                     "reason": "无本地广度落库且实时源不可用"}, True
    win = ratios[-int(p["breadth_ema_days"]):]
    alpha = 2.0 / (int(p["breadth_ema_days"]) + 1.0)
    ema = win[0]
    for v in win[1:]:
        ema = alpha * v + (1 - alpha) * ema
    partial = len(win) < int(p["breadth_ema_days"])
    score = _ir_clip((ema - 0.5) * 2.0 * 100.0, -100.0, 100.0)
    # ADL 背离（需 ≥60 个落库点，上线初期积累不足 → na，不强行表态）
    adl_div = "na"
    lb = int(p["adl_lookback"])
    if len(series) >= lb and len(df) >= lb:
        adv = np.array([float(r.get("up", 0)) - float(r.get("down", 0)) for r in series[-lb:]])
        adl = np.cumsum(adv)
        px = df["close"].tail(lb).values.astype(float)
        if len(px) == lb and not np.isnan(px).any():
            px_new_high = px[-1] >= px.max() - 1e-9
            px_new_low = px[-1] <= px.min() + 1e-9
            adl_new_high = adl[-1] >= adl.max() - 1e-9
            adl_rising_from_low = adl[-1] > adl[:-1].min() if lb > 1 else False
            if px_new_high and not adl_new_high:
                adl_div = "top"
                score *= 0.5                            # 顶背离削弱
            elif px_new_low and adl_rising_from_low:
                adl_div = "bottom"
                score = _ir_clip(score + 30.0, -100.0, 100.0)   # 底背离反向修正
    detail = {"score": _ir_f(score, 2), "up_ratio_ema10": _ir_f(ema, 4),
              "adl_diverge": adl_div, "partial": partial, "days": len(win)}
    return score, detail, False


def _ir_score_nhnl_value(nh60: float, nl60: float, median_abs: float, low250_zone: bool) -> float:
    """E2 NH−NL 打分（数据层当前默认降级，本函数为三期接通预留的完整实现）。

    (NH60−NL60) / 其近一年绝对值中位数，clip [-100,+100]；
    特殊规则：NL60>400 且指数处250日低位区 → 恐慌修正，分数向 0 收敛（×0.25）。
    注：量纲缩放系数待三期接通真实数据后按分布校准。
    """
    if median_abs is None or median_abs <= 0:
        base = 0.0
    else:
        base = _ir_clip((nh60 - nl60) / median_abs, -100.0, 100.0)
    if nl60 > 400 and low250_zone:
        base *= 0.25
    return _ir_clip(base, -100.0, 100.0)


def _ir_score_nhnl(date_str: str, df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    """E2 数据层：ak.stock_a_high_low_statistics 已失效 → 默认降级（保留完整打分函数）"""
    rec = _ir_fetch_nhnl(date_str, p)
    if rec is None:
        return 0.0, {"score": 0.0, "degraded": True,
                     "reason": "stock_a_high_low_statistics 已失效，待三期自建"}, True
    # —— 三期接通后的完整路径（当前不可达）——
    diffs = [d for _, d in rec.get("diff_series", [])]
    med = float(np.median(np.abs(diffs))) if diffs else 0.0
    close = df["close"]
    low250 = bool(len(close) >= 250 and close.iloc[-1] <= close.tail(250).quantile(0.2))
    score = _ir_score_nhnl_value(rec["nh60"], rec["nl60"], med, low250)
    return score, {"score": _ir_f(score, 2), "nh60": rec["nh60"], "nl60": rec["nl60"]}, False


def _ir_score_volume(df: pd.DataFrame, amt: pd.Series, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    """E3 量能确认分（权重25%）：两市成交额近一年分位 P × MA5/MA20 量比 R 联合"""
    a = amt.dropna()
    if len(a) < int(p["pct_lookback"]) // 2 or len(df) < 45:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "成交额序列不足"}, True
    lb = int(p["pct_lookback"])
    amt_pct = _ir_pct_rank(a.tail(lb).values, float(a.iloc[-1]))
    ma5 = a.rolling(5, min_periods=5).mean().iloc[-1]
    ma20 = a.rolling(20, min_periods=20).mean().iloc[-1]
    if amt_pct is None or np.isnan(ma5) or np.isnan(ma20) or ma20 <= 0:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "成交额分位/均线 NaN"}, True
    r = float(ma5 / ma20)
    close = df["close"]
    ret20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else 0.0
    sign = _ir_sign(ret20)
    mag = 60.0 + 40.0 * amt_pct                       # 放量幅度 → 60~100
    hi, lo = float(p["vol_ratio_high"]), float(p["vol_ratio_low"])
    if r > hi:
        f = 1.0                                       # 放量：方向全额表态
    elif r < lo:
        f = float(p["vol_fade_factor"])               # 缩量：方向分 ×0.4 衰减
    else:
        f = float(p["vol_fade_factor"]) + (r - lo) / (hi - lo) * (1.0 - float(p["vol_fade_factor"]))
    score = _ir_clip(sign * mag * f, -100.0, 100.0)
    detail = {"score": _ir_f(score, 2), "amt_pct_1y": _ir_f(amt_pct, 3),
              "vol_ma_ratio": _ir_f(r, 3), "ret20": _ir_f(ret20, 4)}
    return score, detail, False


def _ir_hv20_series(close: pd.Series) -> pd.Series:
    logret = np.log(close / close.shift(1))
    return logret.rolling(20, min_periods=20).std(ddof=1) * math.sqrt(252.0) * 100.0


def _ir_score_qvix(df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    """E4 波动率分（权重15%）：QVIX 近三年分位；接口异常自动切 HV20 本地分位保底。
    HV20 保底是设计文档 6.2 的正式降级链产物（有效打分，不算 degraded），
    仅在 detail.source 标注；只有 QVIX 与 HV20 都失败才 degraded。"""
    end_date = str(df["date"].iloc[-1])
    close = df["close"]
    ret20 = float(close.iloc[-1] / close.iloc[-21] - 1.0) if len(close) >= 21 else 0.0
    lb = int(p["qvix_pct_lookback"])
    qvix_s, src = _ir_fetch_qvix(end_date, p)
    fallback = False
    if qvix_s is not None and len(qvix_s) >= lb // 2:
        cur = float(qvix_s.iloc[-1])
        pct = _ir_pct_rank(qvix_s.tail(lb).values, cur)
    else:                                              # HV20 保底（纯本地，永不失败）
        hv = _ir_hv20_series(close).dropna()
        if len(hv) < 60:
            return 0.0, {"score": 0.0, "degraded": True, "reason": "QVIX/HV20 均不足"}, True
        cur = float(hv.iloc[-1])
        pct = _ir_pct_rank(hv.tail(min(lb, len(hv))).values, cur)
        src = "hv20_fallback"
        fallback = True
    if pct is None:
        return 0.0, {"score": 0.0, "degraded": True, "reason": "QVIX 分位 NaN"}, True
    score = 0.0
    rule = "neutral"
    if pct > float(p["qvix_panic_pct"]) and ret20 < float(p["qvix_panic_ret20"]):
        score, rule = -30.0, "panic_capitulation"     # 恐慌 + 急跌 → 恐慌加剧，维持负面评价
    elif pct < float(p["qvix_low_pct"]) and 0.0 < ret20 < 0.10:
        score, rule = 20.0, "low_vol_grind_up"         # 低波慢牛 +20
    detail = {"score": _ir_f(score, 2), "qvix_pct_3y": _ir_f(pct, 3),
              "qvix": _ir_f(cur, 2), "source": src, "rule": rule}
    if fallback:
        detail["fallback"] = True
        detail["note"] = "QVIX 接口异常，HV20 本地分位保底（设计内降级链，非 degraded）"
    return score, detail, False


def _ir_e5_adjust(s_pre: float, pools: Dict[str, Any], p: Dict[str, Any]) -> Tuple[float, Dict[str, Any], bool]:
    """E5 涨跌停情绪规则（触发式，不进加权）：跌停>30 且 S<-15 → −10；
    涨停>80 且 S>+15 → +10；炸板率>45% → ×0.9（V2 直接用 THS zb_rate=zb/touched；
    缺失时回退旧口径 zb/(zt+zb)）。三类数据全缺 → degraded"""
    zt, dt, zb = pools.get("zt_count"), pools.get("dt_count"), pools.get("zb_count")
    if zt is None and dt is None and zb is None:
        return s_pre, {"degraded": True, "reason": "涨跌停池全部不可用"}, True
    delta = 0.0
    factor = 1.0
    fired = []
    th = float(p["e5_s_threshold"])
    if dt is not None and dt > int(p["e5_dt_count"]) and s_pre < -th:
        delta += float(p["e5_dt_delta"])
        fired.append(f"dt>{p['e5_dt_count']}")
    if zt is not None and zt > int(p["e5_zt_count"]) and s_pre > th:
        delta += float(p["e5_zt_delta"])
        fired.append(f"zt>{p['e5_zt_count']}")
    zb_ratio = pools.get("zb_rate")
    zb_rate_src = "ths" if zb_ratio is not None else None
    if zb_ratio is None and zt is not None and zb is not None and (zt + zb) > 0:
        zb_ratio = zb / (zt + zb)
        zb_rate_src = "em_fallback_formula"
    if zb_ratio is not None and zb_ratio > float(p["e5_zb_ratio"]):
        factor *= float(p["e5_zb_factor"])
        fired.append(f"zb_ratio>{p['e5_zb_ratio']}")
    out = s_pre * factor + delta
    detail = {"zt_count": zt, "dt_count": dt, "zb_count": zb,
              "zb_ratio": _ir_f(zb_ratio, 3) if zb_ratio is not None else None,
              "zb_rate_src": zb_rate_src, "source": pools.get("source"),
              "delta": _ir_f(delta, 1), "factor": _ir_f(factor, 3), "fired": fired}
    return out, detail, False


# ============================================================================
# V2 规则修正层：R0 震荡三元组压缩（加权分之后、状态机之前）
# ============================================================================

def _ir_r0_compress(s_pre: float, feat: Dict[str, Any], p: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """R0【震荡三元组压缩】：cross20>=r0_cross_min 且 vol_ratio<r0_vol_max 且
    pos20∈[r0_pos_lo, r0_pos_hi] → 总分 ×r0_factor。
    研究依据：该三元组在 03-24~04-07 震荡段 100% 命中、上涨/下跌段 0% 误判。"""
    c20, vr, pos = feat.get("cross20"), feat.get("vol_ratio"), feat.get("pos20")
    hit = bool(c20 is not None and vr is not None and pos is not None
               and int(c20) >= int(p["r0_cross_min"])
               and float(vr) < float(p["r0_vol_max"])
               and float(p["r0_pos_lo"]) <= float(pos) <= float(p["r0_pos_hi"]))
    factor = float(p["r0_factor"]) if hit else 1.0
    detail = {"cross20": c20, "vol_ratio": _ir_f(vr, 3), "pos20": _ir_f(pos, 1),
              "hit": hit, "factor": factor}
    return s_pre * factor, detail


# ============================================================================
# 衰竭识别 + Hurst 置信乘数
# ============================================================================

def _ir_exhaust_check(df: pd.DataFrame, p: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
    """衰竭条件（满足其一）：|120日回归斜率| > 近一年80%分位；或 BIAS20 > 近一年90%分位"""
    close = df["close"]
    lb = int(p["pct_lookback"])
    rl = int(p["exhaust_reg_len"])
    detail: Dict[str, Any] = {}
    cond_slope = cond_bias = False
    if len(close) >= rl + 30:
        # 近一年 |slope120| 分布（逐窗 polyfit，约 250 次，开销可接受）
        n_win = min(lb, len(close) - rl + 1)
        arr = close.values.astype(float)
        slopes = []
        for end in range(len(arr) - n_win + 1, len(arr) + 1):
            b, _ = _ir_linreg_slope_r2(arr[end - rl:end])
            slopes.append(abs(b))
        cur = slopes[-1]
        pct = _ir_pct_rank(slopes, cur)
        cond_slope = bool(pct is not None and pct >= float(p["exhaust_slope_pct"]))
        detail["slope120_abs"] = _ir_f(cur, 4)
        detail["slope120_pct_1y"] = _ir_f(pct, 3)
    else:
        detail["slope120_pct_1y"] = None
    bw = int(p["bias_len"])
    if len(close) >= bw + 30:
        ma = _ir_ma(close, bw)
        bias = (close / ma - 1.0) * 100.0
        bias = bias.dropna()
        cur_b = float(bias.iloc[-1])
        pct_b = _ir_pct_rank(bias.tail(lb).values, cur_b)
        cond_bias = bool(pct_b is not None and pct_b >= float(p["exhaust_bias_pct"]))
        detail["bias20"] = _ir_f(cur_b, 2)
        detail["bias20_pct_1y"] = _ir_f(pct_b, 3)
    else:
        detail["bias20_pct_1y"] = None
    detail["cond_slope"] = cond_slope
    detail["cond_bias"] = cond_bias
    return (cond_slope or cond_bias), detail


# ============================================================================
# 磁滞状态机（V2：R2 破位快速通道 + 入场 streak 方向门控）
# ============================================================================

def _ir_step_regime(prev_regime: IndexRegime, s_today: float, s_prev: Optional[float],
                    p: Dict[str, Any], feat: Dict[str, Any],
                    key_day_type: Optional[str] = None,
                    sharp_dir: int = 0,
                    score_delta: Optional[float] = None) -> Tuple[IndexRegime, str]:
    """单步迁移。返回 (新状态, note)。s_prev 为前一交易日最终分（相邻才可确认入场）。

    V2 新增：
    - R2 破位快速通道：uni_up 中收破 MA10 → 当日立即退出 range；
      uni_down 中收复 MA10 → 当日立即退出 range（跳过连续确认）。
    - 入场 streak 门控（研究红线：空头 streak 中收复 MA10 是反抽噪音）：
      uni_up 入场必须 streak>0；uni_down 入场必须 streak<0。
    V2.1 新增：
    - K-up 当日快速入场：RANGE 中触发 K-up 且 S>=enter_threshold → 当日确认进 uni_up
      （跳过连续 2 日确认；K-up 五条件本身已含 MA5>=MA10 方向证据，等效 streak>0 门控）。
    - K-down 当日沿用现有退出规则（S<exit_threshold 立即退出 / R2 破 MA10 立即退出）。
    V2.2 新增：
    - 多头锐化触发快速入场：RANGE 中 SHARP 多头触发（sharp_dir>0）且 S>=enter_threshold
      → 当日确认进 uni_up（补位 K-up 语义：突破档本身即方向证据，豁免 streak 门控；
      参数 sharp_fast_enter 可关）。空头锐化不设 uni_down 单日确认（对齐 K-down 沿用
      现有退出规则；参数 sharp_fast_enter_down 默认 False 预留）。
    """
    enter = float(p["enter_threshold"])
    exit_ = float(p["exit_threshold"])
    close = feat.get("close")
    ma10 = feat.get("ma10")
    streak = int(feat["streak"]) if feat.get("streak") is not None else 0
    score_drop_th = float(p.get("score_drop_turn_threshold", 15.0))
    score_rise_th = float(p.get("score_rise_turn_threshold", 15.0))
    hard_turn_enabled = bool(p.get("score_turn_hard_enabled", True))
    # 0) 结构性硬转向（优先于 R2）
    full_above5_days = int(feat.get("full_above_ma5_days") or 0)
    full_below5_days = int(feat.get("full_below_ma5_days") or 0)
    above5_days = int(feat.get("above_ma5_days") or 0)
    below5_days = int(feat.get("below_ma5_days") or 0)
    hard_down_ma60_slope = bool(p.get("ma60_break_ma5_slope_down_hard", True)) and bool(
        feat.get("close_below_ma60") or feat.get("break_ma60")
    ) and bool(feat.get("ma5_slope_down"))
    hard_up_full_ma5 = bool(p.get("full_above_ma5_hard_up", True)) and full_above5_days >= int(
        p.get("full_above_ma5_confirm_days", 2)
    )
    if bool(p.get("struct_hard_before_r2", True)):
        if hard_down_ma60_slope:
            if prev_regime == IndexRegime.UNI_UP:
                return IndexRegime.RANGE, "struct_down_ma60_ma5_slope_to_range"
            elif prev_regime == IndexRegime.RANGE:
                return IndexRegime.UNI_DOWN, "struct_down_ma60_ma5_slope"
        if hard_up_full_ma5 and s_today >= -enter:
            if prev_regime == IndexRegime.UNI_DOWN:
                return IndexRegime.RANGE, f"struct_up_full_ma5_to_range(full_above5={full_above5_days})"
            elif prev_regime == IndexRegime.RANGE:
                return IndexRegime.UNI_UP, f"struct_up_full_ma5(full_above5={full_above5_days})"
    # 1) R2 破位快速通道（当日生效）
    if prev_regime == IndexRegime.UNI_UP and close is not None and ma10 is not None and close < ma10:
        return IndexRegime.RANGE, f"r2_exit_up(close={close}<MA10={ma10})"
    if prev_regime == IndexRegime.UNI_DOWN and close is not None and ma10 is not None and close > ma10:
        return IndexRegime.RANGE, f"r2_exit_down(close={close}>MA10={ma10})"
    # 1) 阈值退出（立即生效，不需连续确认）
    if prev_regime == IndexRegime.UNI_UP and s_today < exit_:
        return IndexRegime.RANGE, f"exit_up(S={s_today:.1f}<{exit_:.0f})"
    if prev_regime == IndexRegime.UNI_DOWN and s_today > -exit_:
        return IndexRegime.RANGE, f"exit_down(S={s_today:.1f}>-{exit_:.0f})"
    # 1.4) 止跌退出（uni_down 中收盘站上 MA5 + 至少 1 根阳线 → 切 range；
    #      连跌后"close > MA5"是价格行为最直接的止跌证据，结合阳线确认防假反弹；
    #      优于纯数阳线天数 — 避免弱势反弹（收盘仍在 MA5 下）误判为趋势逆转）
    _above5 = int(feat.get("above_ma5_days") or 0)
    _updays = int(feat.get("up_days") or 0)
    if prev_regime == IndexRegime.UNI_DOWN and bool(p.get("uni_down_exit_above_ma5", True)) and _above5 >= 1 and _updays >= 1:
        return IndexRegime.RANGE, f"stabilize_exit(above_ma5={_above5},up_days={_updays})"
    # 1.5) V2.1 K-up 当日快速入场（跳过连续 2 日确认）
    if prev_regime == IndexRegime.RANGE and key_day_type == "k_up" and s_today >= enter:
        return IndexRegime.UNI_UP, f"k_up_enter(S={s_today:.1f}>={enter:.0f})"
    # 1.6) V2.2 锐化触发快速入场（多头补位 K-up；空头默认关闭，对齐 K-down 沿用退出规则）
    if (prev_regime == IndexRegime.RANGE and sharp_dir > 0
            and bool(p.get("sharp_fast_enter", True)) and s_today >= enter):
        return IndexRegime.UNI_UP, f"sharp_up_enter(S={s_today:.1f}>={enter:.0f})"
    if (prev_regime == IndexRegime.RANGE and sharp_dir < 0
            and bool(p.get("sharp_fast_enter_down", False)) and s_today <= -enter):
        return IndexRegime.UNI_DOWN, f"sharp_down_enter(S={s_today:.1f}<=-{enter:.0f})"
    # 2) 结构性硬转向：MA60 破位/收复、MA5 持续站稳/失守优先于磁滞
    if prev_regime != IndexRegime.UNI_DOWN:
        if (feat.get("touch_ma60") and (feat.get("below_ma5_days") or 0) >= 2
                and (feat.get("below_ma20_days") or 0) >= 2):
            return IndexRegime.UNI_DOWN, (
                f"struct_down_ma60_reject(touch60={feat.get('touch_ma60')},"
                f"below5={feat.get('below_ma5_days')},below20={feat.get('below_ma20_days')})"
            )
        if feat.get("break_ma60") or (feat.get("below_ma60_days") or 0) >= 2:
            return IndexRegime.UNI_DOWN, f"struct_down_ma60(below60={feat.get('below_ma60_days')},break60={feat.get('break_ma60')})"
        if (feat.get("below_ma5_days") or 0) >= int(p.get("ma5_persist_days", 3)) and s_today <= enter:
            return IndexRegime.UNI_DOWN, f"struct_down_ma5(below5={feat.get('below_ma5_days')})"
    if prev_regime != IndexRegime.UNI_UP:
        if feat.get("break_ma60") is False and (feat.get("above_ma60_days") or 0) >= 2:
            pass
        if (feat.get("above_ma5_days") or 0) >= int(p.get("ma5_persist_days", 3)) and s_today >= -enter:
            return IndexRegime.UNI_UP, f"struct_up_ma5(above5={feat.get('above_ma5_days')})"
        if (feat.get("full_above_ma5_days") or 0) >= int(p.get("full_above_ma5_confirm_days", 2)) and s_today >= -enter:
            return IndexRegime.UNI_UP, f"struct_up_full_ma5(full_above5={feat.get('full_above_ma5_days')})"
    if hard_turn_enabled and score_delta is not None:
        if score_delta <= -score_drop_th and (
                feat.get("close_below_ma60") or feat.get("break_ma60") or (feat.get("below_ma5_days") or 0) >= 2):
            return IndexRegime.UNI_DOWN, f"score_turn_down(delta={score_delta:.2f},th={score_drop_th:.1f})"
        if score_delta >= score_rise_th and (
                feat.get("full_above_ma5") or (feat.get("above_ma5_days") or 0) >= int(p.get("ma5_persist_days", 3))):
            return IndexRegime.UNI_UP, f"score_turn_up(delta={score_delta:.2f},th={score_rise_th:.1f})"
    # 3) 入场（仅自 RANGE，需前一交易日同向越阈 + streak 方向门控；两单边态不可直接互跳）
    if prev_regime == IndexRegime.RANGE:
        if s_today >= enter:
            if streak <= 0:
                return IndexRegime.RANGE, f"block_up(streak={streak}<=0)"
            if s_prev is not None and s_prev >= enter:
                return IndexRegime.UNI_UP, f"enter_up(S={s_today:.1f},prev={s_prev:.1f})"
            return IndexRegime.RANGE, "pending_up_confirm"
        if s_today <= -enter:
            if streak >= 0:
                return IndexRegime.RANGE, f"block_down(streak={streak}>=0)"
            if s_prev is not None and s_prev <= -enter:
                return IndexRegime.UNI_DOWN, f"enter_down(S={s_today:.1f},prev={s_prev:.1f})"
            return IndexRegime.RANGE, "pending_down_confirm"
    return prev_regime, "hold"


def _ir_gate_advice(regime: IndexRegime, qvix_detail: Dict[str, Any],
                    pools: Dict[str, Any]) -> str:
    """gate 建议（设计文档第8节联动矩阵的简码，供 signal_engine/position_sizer 消费）"""
    if regime == IndexRegime.UNI_UP:
        return "trend_up_hold"          # 正T优先、买入门控放宽、减少卖飞
    if regime == IndexRegime.UNI_DOWN:
        pct = qvix_detail.get("qvix_pct_3y")
        dt = pools.get("dt_count")
        # 恐慌极值行：QVIX 分位>85% 且大面积跌停（NL60>400 的可用代理）
        if pct is not None and pct > 0.85 and dt is not None and dt > 100:
            return "panic_capitulation_watch"   # 杀跌末端：停止割肉、暂停反T、反向预警
        return "defensive_t"            # 防守：买入收紧、禁止追跌
    return "normal_t"                   # 黄金做T环境


# ============================================================================
# 主引擎
# ============================================================================

_IR_MEM_CACHE: Dict[str, Tuple[float, IndexRegime, float, Dict[str, Any]]] = {}


class _IndexRegimeEngine:
    """大盘态势判定引擎（单例）"""

    def detect(self, as_of: Optional[str] = None, force: bool = False,
               mode: str = "eod") -> Tuple[IndexRegime, float, Dict[str, Any]]:
        p = _ir_params()
        state_dir = _ir_state_dir(p)
        target = (as_of or _ir_now().strftime("%Y-%m-%d"))[:10]
        mode = str(mode or "eod").lower()
        if mode not in _IR_MODES:
            raise ValueError(f"mode 必须是 {_IR_MODES} 之一，收到: {mode!r}")

        # —— 内存缓存（TTL=score_cache_ttl，key=mode+请求日期）——
        cache_key = f"{mode}:{target}"
        if not force and cache_key in _IR_MEM_CACHE:
            ts, r, s, ctx = _IR_MEM_CACHE[cache_key]
            if (time.time() - ts) < float(p["score_cache_ttl"]):
                return r, s, ctx

        try:
            regime, score, ctx = self._detect_inner(target, state_dir, p, mode)
        except Exception as e:  # 宁缺毋崩
            _ir_log.exception(f"[index_regime] detect 异常: {e}")
            if os.environ.get("IR_DEBUG"):
                raise
            regime, score = IndexRegime.RANGE, 0.0
            ctx = {
                "date": target, "regime": regime.value, "regime_name": index_regime_name(regime),
                "score": 0.0, "score_raw": 0.0, "trend_score": 0.0, "env_score": 0.0,
                "hurst_mult": 1.0, "exhaust_flag": False, "days_in_regime": 0,
                "detail": {}, "degraded": ["internal_error"], "error": f"{type(e).__name__}: {e}",
                "gate_advice": "normal_t", "mode": mode,
            }
            if mode == "tail":
                ctx["estimate"] = True
        _IR_MEM_CACHE[cache_key] = (time.time(), regime, score, ctx)
        return regime, score, ctx

    # ------------------------------------------------------------------
    def _detect_inner(self, target: str, state_dir: str, p: Dict[str, Any],
                      mode: str = "eod") -> Tuple[IndexRegime, float, Dict[str, Any]]:
        degraded: List[str] = []

        # 1) 指数日线（上证主 + 深证成指的成交额腿）
        df, px_src = _ir_fetch_index_daily(p["index_symbol_sh"], target, int(p["kline_count_sh"]), p)
        if df is None or len(df) == 0:
            ctx = self._degenerate_ctx(target, "指数日线主备源均不可用", mode)
            return IndexRegime.RANGE, 0.0, ctx
        df = df[df["date"] <= target].reset_index(drop=True)
        if len(df) == 0:
            ctx = self._degenerate_ctx(target, f"无 ≤{target} 的指数日线", mode)
            return IndexRegime.RANGE, 0.0, ctx

        df_sz, sz_src = _ir_fetch_index_daily(p["index_symbol_sz"], target, int(p["kline_count_sz"]), p)

        # —— morning 模式：对齐到 as_of 之前最近一个已完成交易日，并补齐前 2 日 ——
        if mode == "morning":
            prior = df[df["date"] < target]["date"]
            if len(prior) == 0:
                ctx = self._degenerate_ctx(target, f"无 <{target} 的已完成交易日", mode)
                return IndexRegime.RANGE, 0.0, ctx
            eff = str(prior.iloc[-1])
            for d in [str(x) for x in prior.iloc[-3:-1]]:   # 补齐 recent_days 所需历史
                self._detect_inner(d, state_dir, p, "eod")
            df = df[df["date"] <= eff].reset_index(drop=True)
            if df_sz is not None:
                df_sz = df_sz[df_sz["date"] <= eff].reset_index(drop=True)

        date_str = str(df["date"].iloc[-1])          # 对齐后的有效交易日
        close = df["close"]

        amt = self._two_market_amount(df, df_sz)
        if df_sz is None:
            degraded.append("amount_sz")

        today_str = _ir_now().strftime("%Y-%m-%d")
        is_today = date_str >= today_str

        # 2) V2 特征层（streak / cross20 / vol_ratio / pos20）+ V2.1 K-day 评估
        feat = _ir_streak_features(df, p)
        detail: Dict[str, Any] = {}

        # 状态前置加载（K-day 判定需要 prev_regime 与锚点状态；幂等重跑先回卷）
        st = _ir_load_state(state_dir)
        st = _ir_rewind_state(st, date_str)
        hist = st.get("history") or []
        prev_rec = hist[-1] if hist else None
        prev_td = self._prev_trading_day(df, date_str)
        prev_adjacent = bool(prev_rec and prev_td and prev_rec.get("date") == prev_td)
        prev_regime = IndexRegime(st.get("last_regime", IndexRegime.RANGE.value)) \
            if prev_rec else IndexRegime.RANGE
        prev_days = int(st.get("days_in_regime", 0)) if prev_rec else 0

        # K-day 判定 + 空头锚点/K-up跃迁推进（纯本地特征计算，无网络依赖）
        key_day, k_anchor, k_up_state, kctx = _ir_kday_eval(
            df, feat, prev_regime, st.get("k_anchor"), st.get("k_up"), p)
        detail["key_day"] = key_day
        k_type = key_day.get("type")

        # V2.2 锐化评估 + 转折触发器 ↔ K-day 联动补位（纯本地特征，无网络依赖）
        # 【V2.2.1 C2】补位缠绕门控：ku_fill / anchor_fill 需近 k_cross_bg_days 日
        # 交叉>=k_cross_bg（复用 K-day 参数，交叉次数由 kctx 传入）；与 K-day 同日
        # 触发（with_k_up/k_down）不受影响（K-day 自带门控）；被拦时 sharp_s 仍计入
        # S、R0/EMA 豁免保留（触发事实不变），仅放弃锚点/跃迁结构补位。
        _cbg_n = kctx.get("cross_bg_n")
        _cbg_ok = bool(_cbg_n is not None and int(_cbg_n) >= int(p["k_cross_bg"]))
        _cbg_txt = f"cross{p['k_cross_bg_days']}={_cbg_n}<{p['k_cross_bg']},缠绕背景不足"
        _fill_gate = bool(p.get("sharp_fill_cross_bg", True))
        sharp, sharp_carry, sharp_add = _ir_sharp_eval(
            df, feat, st.get("sharp"), prev_regime.value, p)
        detail["sharp"] = sharp
        sharp_trig = bool(sharp.get("triggered"))
        sharp_dir = _ir_sign(float(sharp.get("sharp_s") or 0.0)) if sharp_trig else 0
        if sharp_trig and sharp_dir > 0:
            if k_type == "k_down":
                # 反向冲突：T1 以 K-down 为准，sharp_s 照常叠加
                sharp["k_link"] = "conflict_k_down(T1以K-down为准,sharp_s照常叠加)"
            elif (not k_up_state.get("active")) and _fill_gate and not _cbg_ok:
                # V2.2.1 C2：ku_fill 缠绕背景不足 → 放弃补位（锚点/跃迁不改动）
                sharp["k_link"] = f"ku_fill_blocked({_cbg_txt};sharp_s照常计入)"
            else:
                if k_anchor.get("active"):            # 多头锐化=强反转证据 → 解除空头锚点
                    k_anchor = _ir_default_k_anchor()
                    kctx["anchor40"] = None
                if not k_up_state.get("active"):      # K-up 补位（boost 跃迁）
                    k_up_state = {"active": True, "boost": int(p["k_boost"]),
                                  "start_date": date_str, "via": "sharp_up"}
                    sharp["k_link"] = "ku_fill(boost跃迁+豁免R0/EMA+单日确认)"
                else:
                    sharp["k_link"] = ("with_k_up(T1取max,sharp_s照常叠加)" if k_type == "k_up"
                                       else "ku_active(T1取max,sharp_s照常叠加)")
                _stk_sh = int(feat.get("streak") or 0)
                if k_up_state.get("active") and _stk_sh > 0:
                    kctx["k_up_eff_days"] = _stk_sh + int(k_up_state.get("boost", p["k_boost"]))
        elif sharp_trig and sharp_dir < 0:
            if k_type == "k_up":
                # 反向冲突：T1 以 K-up 为准，sharp_s 照常叠加
                sharp["k_link"] = "conflict_k_up(T1以K-up为准,sharp_s照常叠加)"
            else:
                if k_up_state.get("active"):          # 空头锐化 → 清除 K-up 跃迁
                    k_up_state = _ir_default_k_up()
                    kctx["k_up_eff_days"] = None
                if not k_anchor.get("active"):        # 空头锚点补位（streak 清零+锚点）
                    if _fill_gate and not _cbg_ok:
                        # V2.2.1 C2：anchor_fill 缠绕背景不足 → 放弃补位（对称门控）
                        sharp["k_link"] = f"anchor_fill_blocked({_cbg_txt};sharp_s照常计入)"
                    else:
                        _c_sh, _m5_sh = feat.get("close"), feat.get("ma5")
                        _below_sh = 1 if (_c_sh is not None and _m5_sh is not None
                                          and _c_sh < _m5_sh) else 0
                        k_anchor = {"active": True, "below_days": _below_sh,
                                    "recover_days": 0 if _below_sh else 1,
                                    "start_date": date_str, "via": "sharp_down"}
                        kctx["anchor40"] = -_ir_streak_curve_value(
                            _below_sh + int(p["k_boost"]), p)
                        sharp["k_link"] = "anchor_fill(streak清零+空头锚点+豁免R0/EMA)"
                else:
                    sharp["k_link"] = ("with_k_down(T1取max,sharp_s照常叠加)" if k_type == "k_down"
                                       else "anchor_active(sharp_s照常叠加)")
        # 联动合并后同步 key_day 快照字段（与 kctx/锚点终态一致）
        if kctx.get("k_up_eff_days") is not None:
            key_day["k_up_eff_days"] = kctx["k_up_eff_days"]
        else:
            key_day.pop("k_up_eff_days", None)
        key_day["anchor_active"] = bool(k_anchor.get("active"))
        key_day["anchor_days"] = int(k_anchor.get("below_days", 0)) if k_anchor.get("active") else 0

        # V2.2：反向 K-day 清零反向锐化携带（K-down=强空头转折 → 清多头携带余温，
        # K-up 对称；否则转折日 S 会被前一日反向锐化衰减值垫住，05-14 类场景实测失真）
        if k_type == "k_down" and sharp_carry.get("active") \
                and int(sharp_carry.get("direction") or 0) > 0:
            sharp_carry = _ir_default_sharp()
            sharp_add = 0.0
            sharp["decayed_s"] = 0.0
            sharp["carry_active"] = False
            sharp["carry_cleared"] = "k_down_clear"
        elif k_type == "k_up" and sharp_carry.get("active") \
                and int(sharp_carry.get("direction") or 0) < 0:
            sharp_carry = _ir_default_sharp()
            sharp_add = 0.0
            sharp["decayed_s"] = 0.0
            sharp["carry_active"] = False
            sharp["carry_cleared"] = "k_up_clear"

        # 趋势维度 T（五项，归一化）
        atr = _ir_atr_wilder(df, int(p["atr_len"]))
        t_comp: Dict[str, Tuple[float, bool, float]] = {}
        for key, func, wkey in (
            ("ma_streak", lambda: _ir_score_ma_streak(feat, p, kctx), "w_ma_streak"),
            ("structure", lambda: _ir_structure_score(df, feat, p), "w_structure"),
            ("adx", lambda: _ir_score_adx(df, p), "w_adx"),
            ("reg_r2", lambda: _ir_score_reg_r2(df, atr, p), "w_reg_r2"),
            ("er", lambda: _ir_score_er(df, p), "w_er"),
            ("aroon", lambda: _ir_score_aroon(df, p), "w_aroon"),
        ):
            try:
                s, d, dg = func()
            except Exception as e:
                s, d, dg = 0.0, {"score": 0.0, "degraded": True, "reason": f"{type(e).__name__}"}, True
            t_comp[key] = (s, dg, float(p[wkey]))
            detail[key] = d
            if dg:
                degraded.append(key)
        trend_score = self._weighted(t_comp)

        # 3) 环境维度 E（四项，同上归一化）
        e_comp: Dict[str, Tuple[float, bool, float]] = {}
        s_b, d_b, dg_b = _ir_score_breadth(state_dir, date_str, is_today, df, p)
        e_comp["breadth"] = (s_b, dg_b, float(p["w_breadth"])); detail["breadth"] = d_b
        s_n, d_n, dg_n = _ir_score_nhnl(date_str, df, p)
        e_comp["nhnl"] = (s_n, dg_n, float(p["w_nhnl"])); detail["nhnl"] = d_n
        s_v, d_v, dg_v = _ir_score_volume(df, amt, p)
        e_comp["volume"] = (s_v, dg_v, float(p["w_volume"])); detail["volume"] = d_v
        s_q, d_q, dg_q = _ir_score_qvix(df, p)
        e_comp["qvix"] = (s_q, dg_q, float(p["w_qvix"])); detail["qvix"] = d_q
        for k, (_, dg_, _) in e_comp.items():
            if dg_:
                degraded.append(k)
        env_score = self._weighted(e_comp)

        # 4) 合成管线：S_raw → R0压缩(K日/锐化触发豁免) → E5 → SHARP加法 → Hurst → 衰竭 → EMA(3, 豁免) → clip±s_clip_max(V2.2.1 C4)
        s_raw = float(p["trend_weight"]) * trend_score + float(p["env_weight"]) * env_score

        s_r0, d_r0 = _ir_r0_compress(s_raw, feat, p)
        if k_type and d_r0.get("hit"):                 # V2.1：K日当日豁免 R0 压缩（关键日优先级高于震荡压缩）
            s_r0 = s_raw
            d_r0 = {**d_r0, "factor": 1.0, "k_exempt": True}
        elif sharp_trig and d_r0.get("hit"):           # V2.2：锐化触发当日豁免 R0（转折日同优先级；V2.2.1 经 C1/C3 收窄为仅真实转折触发日）
            s_r0 = s_raw
            d_r0 = {**d_r0, "factor": 1.0, "sharp_exempt": True}
        detail["range_triple"] = d_r0

        pools = _ir_fetch_limit_pools(date_str.replace("-", ""), p)
        s_e5, d_e5, dg_e5 = _ir_e5_adjust(s_r0, pools, p)
        detail["limit_pool"] = d_e5
        if dg_e5:
            degraded.append("limit_pool")
        if any(pools.get(k) is not None for k in ("zt_count", "dt_count", "zb_count")):
            _ir_save_breadth(state_dir, date_str, pools)   # 涨跌停池随广度快照落库

        # V2.2：锐化规则层加法项（与 E5 同层：R0 之后、Hurst/EMA 之前；
        # 触发日全额、携带日衰减值、无携带为 0）
        s_sharp = s_e5 + sharp_add

        h_bar, h_today = _ir_hurst_bar(close, int(p["hurst_window"]), int(p["hurst_smooth"]))
        hurst_mult = 1.0 if h_bar is None else _ir_clip(1.0 + (h_bar - 0.5), 0.8, 1.2)
        detail["hurst"] = {"h_bar": _ir_f(h_bar, 3), "h_today": _ir_f(h_today, 3),
                           "mult": _ir_f(hurst_mult, 3), "note": None if h_bar is not None else "insufficient_bars"}

        s_adj = s_sharp * hurst_mult
        exhaust_flag, d_ex = _ir_exhaust_check(df, p)
        detail["exhaust"] = d_ex
        if exhaust_flag:
            s_adj *= float(p["exhaust_factor"])

        # 5) 状态机 + EMA 平滑（状态已于第2段前置加载并回卷；K日/锐化触发当日豁免 EMA，
        #    关键日分数不与前日平滑，"后续基于当日继续累积"）
        alpha = 2.0 / (int(p["smooth_ema_days"]) + 1.0)
        sharp_ema_bypass = bool(sharp_trig)            # V2.2：锐化触发当日豁免 EMA
        k_ema_bypass = bool(k_type) or sharp_ema_bypass
        if k_ema_bypass:
            s_final = s_adj
            s_prev = float(prev_rec["S"]) if (prev_adjacent and prev_rec.get("S") is not None) else None
        elif prev_adjacent and prev_rec.get("S") is not None:
            s_final = alpha * s_adj + (1.0 - alpha) * float(prev_rec["S"])
            s_prev = float(prev_rec["S"])
        else:
            s_final = s_adj
            s_prev = None

        # V2.2.1 C4：S 量程封顶——全部加法项（SHARP/E5）+ Hurst/衰竭 + EMA 之后
        # clip 到 ±s_clip_max（修复 V2.2 05-08 S=104.61 破量程；s_pre_clip 入 detail）
        s_pre_clip = s_final
        _s_clip_max = float(p.get("s_clip_max", 100.0))
        s_final = _ir_clip(s_final, -_s_clip_max, _s_clip_max)

        score_delta = None
        if prev_adjacent and prev_rec and prev_rec.get("S") is not None:
            score_delta = float(s_final) - float(prev_rec.get("S"))

        structure_detail = detail.get("structure", {}) or {}
        structure_score = float(structure_detail.get("score") or 0.0)
        # 首次站上 MA5 快速恢复：deep streak 止跌初期 low-buffer 加分（above_ma5_days=1~2 间的空窗）
        _streak_r = int(feat.get("streak") or 0)
        _rm_min = int(p.get("ma5_recover_streak_min", -5))
        if _streak_r <= _rm_min and (feat.get("above_ma5_days") or 0) >= 1 \
                and (feat.get("above_ma5_days") or 0) < int(p.get("ma5_persist_days", 3)):
            if s_final < 0:
                _rb_b = float(p.get("ma5_recover_bonus_base", 5.0))
                _rb_s = float(p.get("ma5_recover_bonus_streak", 0.3))
                _rb_p = float(p.get("ma5_recover_bonus_pos20", 0.12))
                _rb_c = float(p.get("ma5_recover_bonus_cap", 15.0))
                _recover_bonus = min(_rb_c, _rb_b + abs(_streak_r) * _rb_s + (feat.get("pos20") or 0) * _rb_p)
                s_final += _recover_bonus
        new_regime, note = _ir_step_regime(prev_regime, s_final,
                                           s_prev if prev_adjacent else None, p, feat,
                                           key_day_type=k_type, sharp_dir=sharp_dir,
                                           score_delta=score_delta)
        days_in_regime = prev_days + 1 if (new_regime == prev_regime and prev_adjacent) else 1

        # stabilize_exit → 清除空头锚点（退出 uni_down 时清理锚点状态，防止污染后续 range 期的分数）
        if note.startswith("stabilize_exit"):
            k_anchor = _ir_default_k_anchor()
            kctx["anchor40"] = None

        gate = _ir_gate_advice(new_regime, detail.get("qvix", {}), pools)
        structure_trigger = float(p.get("structure_trigger", 10.0))
        structure_strong = abs(structure_score) >= structure_trigger

        # V2/V2.1/V2.2 触发规则汇总（便于复盘/推送）
        fired_rules: List[str] = []
        if k_type == "k_up":
            fired_rules.append("K_UP")
        if k_type == "k_down":
            fired_rules.append("K_DOWN")
        if not k_type and key_day.get("anchor_active"):
            fired_rules.append("K_ANCHOR")           # 空头锚点存续日（非触发日）
        if note.startswith("struct_down_ma60_ma5_slope"):
            fired_rules.append("STRUCT_DOWN_MA60_MA5_SLOPE")
        if note.startswith("struct_up_full_ma5"):
            fired_rules.append("STRUCT_UP_FULL_MA5")
        if structure_strong:
            fired_rules.append("STRUCTURE_STRONG")
        if note.startswith("score_turn_down"):
            fired_rules.append("SCORE_TURN_DOWN")
        if note.startswith("score_turn_up"):
            fired_rules.append("SCORE_TURN_UP")
        if d_r0.get("hit") and not d_r0.get("k_exempt") and not d_r0.get("sharp_exempt"):
            fired_rules.append("R0")
        if detail.get("ma_streak", {}).get("r1_hit"):
            fired_rules.append("R1")
        if note.startswith("r2_"):
            fired_rules.append("R2")
        if note.startswith("stabilize_exit"):
            fired_rules.append("STABILIZE_EXIT")
        if detail.get("ma_streak", {}).get("late_warning"):
            fired_rules.append("LATE")
        # V2.2 锐化规则：SHARP_UP/DOWN（该侧存在突破档或量能确认且得分主导）+ SHARP_TRIGGER
        # 【V2.2.1 C1】被抑制侧不出规则；监测侧在监测范围内主导（有分且有突破/量能）即出
        _su = int(sharp.get("sharp_up") or 0)
        _sd = int(sharp.get("sharp_down") or 0)
        _pu = (sharp.get("parts") or {}).get("up") or {}
        _pd = (sharp.get("parts") or {}).get("down") or {}
        _supp_side = sharp.get("suppressed_side")
        _up_fire = _su > _sd and int(_pu.get("breakout") or 0) + int(_pu.get("vol") or 0) > 0
        _dn_fire = _sd > _su and int(_pd.get("breakout") or 0) + int(_pd.get("vol") or 0) > 0
        if _supp_side == "up":                       # uni_up：只监测空头锐化
            _up_fire = False
            _dn_fire = _sd > 0 and int(_pd.get("breakout") or 0) + int(_pd.get("vol") or 0) > 0
        elif _supp_side == "down":                   # uni_down：只监测多头锐化
            _dn_fire = False
            _up_fire = _su > 0 and int(_pu.get("breakout") or 0) + int(_pu.get("vol") or 0) > 0
        if _up_fire:
            fired_rules.append("SHARP_UP")
        if _dn_fire:
            fired_rules.append("SHARP_DOWN")
        if sharp_trig:
            fired_rules.append("SHARP_TRIGGER")

        ctx: Dict[str, Any] = {
            "date": date_str,
            "as_of": target,
            "mode": mode,
            "regime": new_regime.value,
            "regime_name": index_regime_name(new_regime),
            "score": _ir_f(s_final, 2),
            "score_raw": _ir_f(s_adj, 2),
            "trend_score": _ir_f(trend_score, 2),
            "env_score": _ir_f(env_score, 2),
            "hurst_mult": _ir_f(hurst_mult, 3),
            "exhaust_flag": bool(exhaust_flag),
            "days_in_regime": days_in_regime,
            "detail": detail,
            "degraded": degraded,
            "gate_advice": gate,
        }
        ctx["detail"]["fired_rules"] = fired_rules
        score_drop_th = float(p.get("score_drop_turn_threshold", 15.0))
        score_rise_th = float(p.get("score_rise_turn_threshold", 15.0))
        hard_turn_enabled = bool(p.get("score_turn_hard_enabled", True))
        ctx["detail"]["pipeline"] = {
            "s_blend": _ir_f(s_raw, 2), "s_r0": _ir_f(s_r0, 2), "s_e5": _ir_f(s_e5, 2),
            "s_sharp": _ir_f(s_sharp, 2), "sharp_add": _ir_f(sharp_add, 2),
            "s_adj": _ir_f(s_adj, 2), "s_pre_clip": _ir_f(s_pre_clip, 2),
            "s_final": _ir_f(s_final, 2),
            "ema_seeded": bool(prev_adjacent), "px_source": px_src,
            "k_day_ema_bypass": k_ema_bypass, "sharp_ema_bypass": sharp_ema_bypass,
            "structure_score": _ir_f(structure_score, 2),
            "structure_trigger": _ir_f(structure_trigger, 2),
            "structure_strong": bool(structure_strong),
            "score_delta": _ir_f(score_delta, 3) if score_delta is not None else None,
            "score_turn_threshold": {
                "drop": _ir_f(score_drop_th, 2),
                "rise": _ir_f(score_rise_th, 2),
                "enabled": hard_turn_enabled,
            },
        }
        ctx["detail"]["state"] = {
            "prev_regime": prev_regime.value, "note": note,
            "prev_date": prev_rec.get("date") if prev_rec else None,
            "prev_adjacent": prev_adjacent,
        }

        if mode == "tail":
            # 盘中估值模式：含 forming bar，只读不写，保持 EOD 状态机纯净
            ctx["estimate"] = True
            ctx["detail"]["estimate"] = True
            ctx["detail"]["estimate_note"] = "tail 模式：当日K线未完成（forming bar），不写入 state/trace"
            return new_regime, float(ctx["score"]), ctx

        # 6) 落库：state.json 契约字段 + history 扩展（含 k_anchor/k_up/sharp 快照）+ trace jsonl
        if sharp_trig:
            sharp_carry["regime_at_trigger"] = new_regime.value   # 触发日回填（状态切换清零基准）
        hist.append({"date": date_str, "S": _ir_f(s_final, 4), "sadj": _ir_f(s_adj, 4),
                     "regime": new_regime.value, "days_in_regime": days_in_regime,
                     "k_anchor": dict(k_anchor), "k_up": dict(k_up_state),
                     "sharp": dict(sharp_carry)})
        st["history"] = hist[-40:]
        st["last_regime"] = new_regime.value
        st["days_in_regime"] = days_in_regime
        st["last_date"] = date_str
        st["k_anchor"] = dict(k_anchor)
        st["k_up"] = dict(k_up_state)
        st["sharp"] = dict(sharp_carry)
        st["score_history"] = [{"date": r["date"], "S": r["S"]} for r in st["history"][-10:]]
        _ir_save_state(state_dir, st)
        _ir_append_trace(state_dir, date_str, ctx)

        if mode == "morning":
            ctx["detail"]["recent_days"] = [
                {"date": r.get("date"), "regime": r.get("regime"), "score": r.get("S")}
                for r in (st.get("history") or [])[-3:]
            ]
        return new_regime, float(ctx["score"]), ctx

    # ------------------------------------------------------------------
    @staticmethod
    def _weighted(comp: Dict[str, Tuple[float, bool, float]]) -> float:
        """维度内加权合成：未降级指标按权重重新归一化（降级项剔除，避免总分被压向0）"""
        num = sum(w * s for s, dg, w in comp.values() if not dg)
        den = sum(w for _, dg, w in comp.values() if not dg)
        if den <= 0:
            return 0.0
        return _ir_clip(num / den, -100.0, 100.0)

    @staticmethod
    def _two_market_amount(df_sh: pd.DataFrame, df_sz: Optional[pd.DataFrame]) -> pd.Series:
        """两市成交额 = 上证 + 深证成指（腾讯行内无 amount 列时退化为 volume，口径自洽）"""
        a_sh = pd.Series(df_sh["amount"].values, index=df_sh["date"].values)
        if df_sz is None or len(df_sz) == 0:
            return a_sh
        a_sz = pd.Series(df_sz["amount"].values, index=df_sz["date"].values)
        total = a_sh.add(a_sz, fill_value=0.0)
        total[a_sh.index.difference(a_sz.index)] = a_sh[a_sh.index.difference(a_sz.index)]
        return total.reindex(a_sh.index)

    @staticmethod
    def _prev_trading_day(df: pd.DataFrame, date_str: str) -> Optional[str]:
        dates = df["date"].values
        idx = np.searchsorted(dates, date_str)
        if idx <= 0:
            return None
        return str(dates[idx - 1])

    @staticmethod
    def _degenerate_ctx(target: str, reason: str, mode: str = "eod") -> Dict[str, Any]:
        ctx = {
            "date": target, "as_of": target, "mode": mode,
            "regime": IndexRegime.RANGE.value, "regime_name": index_regime_name(IndexRegime.RANGE),
            "score": 0.0, "score_raw": 0.0, "trend_score": 0.0, "env_score": 0.0,
            "hurst_mult": 1.0, "exhaust_flag": False, "days_in_regime": 0,
            "detail": {"fatal": reason}, "degraded": ["index_daily"],
            "gate_advice": "data_unavailable",
        }
        if mode == "tail":
            ctx["estimate"] = True
        return ctx


_IR_ENGINE: Optional[_IndexRegimeEngine] = None


def _ir_get_engine() -> _IndexRegimeEngine:
    global _IR_ENGINE
    if _IR_ENGINE is None:
        _IR_ENGINE = _IndexRegimeEngine()
    return _IR_ENGINE


def detect_index_regime(as_of: str = None, force: bool = False, mode: str = "eod") -> tuple:
    """主入口：返回 (IndexRegime, 综合分, 明细dict)。

    as_of=None → 今天（宿主 _now()，支持 SIM_NOW 回测注入）；否则 'YYYY-MM-DD'。
    force=True 绕过内存缓存（TTL=1800s，key=mode+as_of 日期）。
    mode="eod"（默认，截至 as_of 收盘）/ "morning"（早盘：对齐到 as_of 之前最近
    已完成交易日，detail.recent_days 附最近 3 日 [{date,regime,score}]）/
    "tail"（14:30 后盘中：含 forming bar，estimate=true，不写 state/trace）。
    """
    return _ir_get_engine().detect(as_of, force, mode)


# ============================================================================
# CLI
# ============================================================================

def _ir_cli() -> None:
    ap = argparse.ArgumentParser(description="大盘态势判定 V2.2.4（streak主导打分+K日跃迁+SHARP锐化+价格结构强化+规则层+状态机）")
    ap.add_argument("--date", default=None, help="判定日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON 输出")
    ap.add_argument("--force", action="store_true", help="绕过内存缓存")
    ap.add_argument("--mode", default="eod", choices=list(_IR_MODES),
                    help="评估时点：eod 收盘(默认) / morning 早盘(前一完成日) / tail 盘中估值")
    args = ap.parse_args()

    regime, score, ctx = detect_index_regime(as_of=args.date, force=args.force, mode=args.mode)
    if args.json:
        print(json.dumps(ctx, ensure_ascii=False, indent=2, default=_ir_json_default))
    else:
        print(f"日期        : {ctx.get('date')}  (as_of={ctx.get('as_of')}, mode={ctx.get('mode')}"
              f"{', estimate' if ctx.get('estimate') else ''})")
        print(f"状态        : {ctx.get('regime_name')} ({ctx.get('regime')})  持续 {ctx.get('days_in_regime')} 日")
        print(f"综合分 S    : {ctx.get('score')}  (raw={ctx.get('score_raw')}, "
              f"T={ctx.get('trend_score')}, E={ctx.get('env_score')}, "
              f"Hurst×{ctx.get('hurst_mult')}, 衰竭={ctx.get('exhaust_flag')})")
        print(f"规则触发    : {ctx.get('detail', {}).get('fired_rules')}")
        print(f"降级项      : {ctx.get('degraded')}")
        print(f"gate_advice : {ctx.get('gate_advice')}")

    # eod 模式落盘 sentiment_daily.csv（供 main.py/复盘工具消费）
    if args.mode == "eod":
        try:
            from daily_sentiment import save_sentiment_record  # 延迟导入避免循环
            _dt = (ctx.get("detail") or {}).get("limit_pool") or {}
            _kd = (ctx.get("detail") or {}).get("key_day") or {}
            _dc = int(_dt.get("dt_count") or 0)
            rec = {
                "date": ctx.get("date"), "regime": ctx.get("regime"),
                "regime_name": ctx.get("regime_name"), "score_S": ctx.get("score"),
                "z_S": None, "top3_avg": None, "z_top3": None, "top3_names": [],
                "k_day_type": _kd.get("type") or "",
                "index_pct": None, "dt_count": _dc,
                "systemic_risk": _dc > 30,
                "decision_summary": ctx.get("gate_advice", ""),
            }
            save_sentiment_record(rec)
        except Exception:
            pass


if __name__ == "__main__":
    _ir_cli()
