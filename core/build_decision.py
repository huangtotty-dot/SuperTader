# -*- coding: utf-8 -*-
"""
core/build_decision.py — 建仓判定决策核（P3 建仓加仓同源：双侧单一真源）

方案A（2026-08-15 owner 拍板）建仓判定 = 时机门控 go（regime 条件化）+ 否决因子 + W35 日内确认。
本模块是该判定的**纯函数决策核**：无 IO、无 superTrader/goldminer 环境依赖，仅用标准库 + numpy/pandas。

消费方：
  · superTrader：core/timing_gate.py（数据获取后委托本模块决策）、core/position_builder.py
    （verdict 映射 + W35 日内确认委托本模块）
  · goldminer：signals/build_gate.py 经 SUPERTRADER_ROOT 以 importlib 绝对路径加载本文件
    （与 config/auto_pool.py 同一跨仓消费模式）

纪律：
  · 本模块函数是双侧 verdict 一致性的唯一来源——改动必须双侧同步验证（scripts/build_verdict_parity.py）。
  · 禁止在本模块加 IO/网络/文件读写；数据获取留在两侧各自的 adapter。
  · 量能只用比值（vol_ratio20），股/手单位无关。
"""
import numpy as np
import pandas as pd

# 与 config.ENTRY_TIMING_PARAMS 同值（goldminer 侧无法 import superTrader config.py——其顶层
# import requests 等重依赖；以此为本模块内嵌兜底）。同步由单测守卫：
# t_io/validation/build_decision/test_build_decision.py::test_default_params_match_config
DEFAULT_TIMING_PARAMS = {
    "enabled": True,
    "regime_ma60": True,
    "regime_up_buffer": 1.005,
    "trend_up_drawdown_min": -0.03,
    "trend_dn_drawdown_max": -0.10,
    "trend_dn_rsi_max": 20.0,
    "intraday_confirm_gate": True,
    "intraday_confirm_vol_min": 1.2,
    "veto_vol_spike": 3.0,
    "veto_dist_ma60_max": 0.20,
    "ma5_below_penalty": 70,          # 2026-10-08：未站上5日线 → 综合得分扣减
    "ma5_below_block_signal": True,   # 破 MA5 时把 signal 压成 approaching
    # 2026-10-15 owner：「站上 MA20」作为**硬性约束**——不站上则综合得分悬崖式扣减。
    # 为什么光靠 +权重(W_MA20=3)不够：那 +30 分会被**逆向**的「日线阶段」项（④下跌 +30）
    # 正好抵消（实测 艾力斯 破位+④下跌 = 长江电力 站上+企稳 = 50 分，分不开）。
    "ma20_below_penalty": 40,     # 不站上 MA20 → 扣减（比 MA5 那条 70 轻一档）
    # 2026-10-15 owner：「GO 与得分口径必须一致」——GO 也要求站上 MA20（否则出现"GO 但得分 0"）
    # ⚠️ 打开它 = **废止 trend_dn 的抄底腿**（深回撤必然跌破 MA20）。设为 False 可回退。
    "go_require_above_ma20": True,
    # 同上：GO 也要求站上 MA5（否则"GO 但 0 分"仍有残留——破 MA5 的 −70 悬崖）
    "go_require_above_ma5": True,
}


# ═══════════════════════════════════════════
# 市场状态（指数 vs MA60，无未来函数）
# ═══════════════════════════════════════════

def regime_from_index_daily(df: pd.DataFrame, date_str: str, params: dict = None) -> dict:
    """指数日线 → 市场状态。df 需含 date/close 列；按 <=date_str 截断（调用方保证无未来）。
    返回 {regime, close, ma60, ratio}；数据 <61 行 → regime=unknown。"""
    p = params or DEFAULT_TIMING_PARAMS
    idx = df[df["date"].astype(str) <= str(date_str)]
    if len(idx) < 61:
        return {"regime": "unknown", "close": None, "ma60": None}
    close = float(idx["close"].iloc[-1])
    ma60 = float(idx["close"].astype(float).rolling(60).mean().iloc[-1])
    up_buffer = float(p.get("regime_up_buffer", 1.005))
    if close > ma60 * up_buffer:
        regime = "trend_up"
    elif close < ma60 * 0.97:
        regime = "trend_dn"
    else:
        regime = "range"
    return {"regime": regime, "close": close, "ma60": round(ma60, 3),
            "ratio": round(close / ma60, 4)}


# ═══════════════════════════════════════════
# 个股日线时机特征（截止 date_str，无未来）
# ═══════════════════════════════════════════

def features_from_daily(df: pd.DataFrame, date_str: str) -> dict:
    """个股日线 → 时机特征。df 需含 date/open/high/low/close/volume 列；<61 行 → {}（数据不足）。"""
    if df is None or df.empty:
        return {}
    df = df.sort_values("date").reset_index(drop=True)
    df["date"] = df["date"].astype(str)
    sub = df[df["date"] <= str(date_str)]
    if len(sub) < 61:
        return {}
    c = sub["close"].astype(float)
    h = sub["high"].astype(float)
    price = float(c.iloc[-1])
    ma5 = float(c.rolling(5).mean().iloc[-1])
    ma20 = float(c.rolling(20).mean().iloc[-1])
    ma60 = float(c.rolling(60).mean().iloc[-1])
    rec_high = float(h.tail(20).max())
    # MACD 金叉（近5日）
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    dif = e12 - e26
    dea = dif.ewm(span=9, adjust=False).mean()
    golden = bool(((dif > dea) & (dif.shift(1) <= dea.shift(1))).tail(5).any())
    # RSI(14)（空头抄底超卖极值用）— Wilder 平滑（2026-09-21 统一口径；
    # 真相源 analysis/indicators.py::wilder_rsi。原为 rolling(14).mean() 简单均值）
    _delta = c.diff()
    _gain = _delta.clip(lower=0).ewm(alpha=1.0 / 14, adjust=False).mean()
    _loss = (-_delta.clip(upper=0)).ewm(alpha=1.0 / 14, adjust=False).mean()
    _rsi = float((100 - 100 / (1 + _gain / _loss.replace(0, float("nan")))).iloc[-1]) if _loss.iloc[-1] and _loss.iloc[-1] > 0 else 50.0
    # 2026-08-27 因子挖掘（127万股票-日）：两个否决因子的特征
    _vol_ratio20 = None
    if "volume" in sub.columns:
        _v = pd.to_numeric(sub["volume"], errors="coerce")
        _v20 = float(_v.rolling(20).mean().iloc[-1]) if len(_v) >= 20 else float("nan")
        if _v20 and _v20 > 0:
            _vol_ratio20 = round(float(_v.iloc[-1]) / _v20, 2)
    # 日线四阶段（2026-10-11）：与 `core/position_structure.stage_daily` **同规则**。
    # 此处内联而非 import：goldminer 以 SUPERTRADER_ROOT 绝对路径 importlib 加载本文件，
    # 不能反向依赖 core 包。一致性由单测守卫（tests/phase3/test_pb_position_structure.py）。
    _m20, _m60 = float(ma20), float(ma60)                   # 上方已转为 float
    _ma20_5ago = float(c.rolling(20).mean().iloc[-6])
    _up20 = _m20 > _ma20_5ago
    _dn20 = _m20 < _ma20_5ago
    # 2026-10-15（owner）：关键支撑企稳 = 收盘站上 MA60 且近 3 日最低价都没跌破 MA60。
    # 取「近 3 日**最低价**」而不是只看收盘：要的是"整段都站在支撑上"（真企稳），
    # 而不是"最后一根恰好收在上面"（可能是刚反抽）。缺 low 列 ⇒ False（fail-closed）。
    _support_hold = False
    if "low" in sub.columns and len(sub) >= 3:
        _low3 = pd.to_numeric(sub["low"], errors="coerce").tail(3).min()
        _m60_now = float(c.rolling(60).mean().iloc[-1])
        _support_hold = bool(_m60_now > 0 and _low3 == _low3
                             and price > _m60_now and float(_low3) >= _m60_now)
    _stage = 1                                              # ①蓄势（默认）
    if price > _m20 > _m60 and _up20:
        _stage = 2                                          # ②抬升
    if _m20 > _m60 and price < _m20:
        _stage = 3                                          # ③派发
    if price < _m20 < _m60 and _dn20:
        _stage = 4                                          # ④下跌
    return {
        "price": round(price, 3),
        # 2026-10-15（owner 得分细则重构）：「关键支撑企稳」——**regime 无关**，
        # 用来顶替原来那个随大盘 regime 变阈值的「回撤到位」。见 timing_score docstring。
        # 支撑 = MA60（长期支撑）；企稳 = 收盘站上 MA60 **且**近 3 个交易日最低价都没跌破它。
        "support_hold": bool(_support_hold),
        "stage_daily_code": _stage,
        "trend_multihead": bool(price > ma20 and price > ma60),
        "above_ma60": bool(price > ma60),
        "drawdown": round(price / rec_high - 1, 4) if rec_high > 0 else 0.0,
        "macd_golden_5d": golden,
        "rsi": round(_rsi, 1),
        "ma5": round(ma5, 3),
        "above_ma5": bool(price > ma5),
        # 2026-10-14：**纯新增**（不参与任何打分）—— GUI「20日线观察」列用。
        # 得分里的 −70 扣分仍走 above_ma5（owner 决定保留），两者**刻意不联动**。
        "above_ma20": bool(price > ma20),
        "ma20": round(ma20, 3), "ma60": round(ma60, 3),
        "vol_ratio20": _vol_ratio20,
        "dist_ma60": round(price / ma60 - 1, 4),
        # F-1(2026-09-04): 特征末 bar 日期——供消费方校验"当日形成 bar 是否已入"(陈旧日线 fail-closed)
        "last_bar_date": str(sub["date"].iloc[-1]),
    }


def dd_threshold_ok(drawdown: float, regime: str) -> bool:
    """回撤到位判定（B-4: range 市观察态回撤用多头口径 ≥-3%）。"""
    if regime == "trend_up":
        return drawdown >= -0.03
    if regime == "trend_dn":
        return drawdown < -0.10
    return drawdown >= -0.03


# ═══════════════════════════════════════════
# 时机决策（regime 条件化 go + 否决因子）
# ═══════════════════════════════════════════

def timing_decision(features: dict, regime: str, params: dict = None) -> dict:
    """时机门控决策。返回 {go, veto, reasons}。features={}（数据不足）由调用方先行拦截。"""
    p = params or DEFAULT_TIMING_PARAMS
    f = features
    reasons = []
    vetoes = []
    if regime == "trend_up":
        # 多头趋势 → 追强；两个硬否决（仅追强侧；抄底侧爆量是恐慌出清常态，不否决）
        _vol_max = float(p.get("veto_vol_spike", 3.0))
        _dist_max = float(p.get("veto_dist_ma60_max", 0.20))
        _vr = f.get("vol_ratio20")
        if _vr is not None and _vr >= _vol_max:
            vetoes.append(f"爆量{_vr:g}倍≥{_vol_max:g}")
        _dm = f.get("dist_ma60")
        if _dm is not None and _dm > _dist_max:
            vetoes.append(f"偏离MA60{_dm:+.1%}>{_dist_max:+.0%}")
        cond = f["trend_multihead"] and f["drawdown"] >= -0.03 and not vetoes
        reasons.append(f"多头趋势: 追强(多头{'✓' if f['trend_multihead'] else '✗'}+浅回撤{'✓' if f['drawdown']>=-0.03 else '✗'})")
        if vetoes:
            reasons.append(f"否决: {'、'.join(vetoes)}")
        if f["macd_golden_5d"]:
            reasons.append("MACD金叉近5日 ✓（加分）")
        go = cond
    elif regime == "trend_dn":
        # 空头趋势 → 抄底超跌极值（深回撤 + RSI 深度超卖）
        _rsi_lim = float(p.get("trend_dn_rsi_max", 20))
        _dd_ok = f["drawdown"] < -0.10
        _rsi_ok = (f.get("rsi") or 50) < _rsi_lim
        cond = _dd_ok and _rsi_ok
        reasons.append(f"空头趋势: 抄底(深回撤{'✓' if _dd_ok else '✗'} + RSI极值{'✓' if _rsi_ok else '✗'} "
                       f"rsi={f.get('rsi')} drawdown={f['drawdown']:.1%})")
        go = cond
    else:
        # 震荡/unknown → 降频
        go = False
        reasons.append("震荡市: 降频，暂不建仓/加仓")
    # 2026-10-15 owner：「**GO 与得分口径必须一致**」——站上 MA20 是硬性前提。
    # 没有这条会出现「GO 但得分 0」，两列互相打脸（2026-10-15 实测：13 只 GO **全部** 0 分，
    # 因为空头市 GO 走的是「抄底」=要求深回撤/跌破均线，而得分要求站上 MA20，方向天然相反）。
    # 效果：trend_up 无变化（trend_multihead 已隐含站上 MA20）；**trend_dn 的抄底腿被废止**
    #       （深回撤 ⇒ 必然跌破 MA20 ⇒ 永远不 GO）；range 本来就是 False。
    if bool(p.get("go_require_above_ma20", True)) and f.get("above_ma20") is not True:
        if go:
            reasons.append("未站上MA20(硬约束): 得分口径要求站上MA20 ⇒ 不给GO")
        go = False
    # 同理 MA5：`trend_multihead`（站上 MA20&MA60）**不蕴含**站上 MA5（刚回踩就可能破），
    # 而破 MA5 有 −70 悬崖 ⇒ 只闸 MA20 仍有残留的「GO 但 0 分」（实测 2026-08-31：248 只里
    # 还有 14 只）。要"GO ⟹ 得分>0"完全成立，MA5 也必须一起闸。
    if bool(p.get("go_require_above_ma5", True)) and f.get("above_ma5") is not True:
        if go:
            reasons.append("未站上MA5(硬约束): 得分有 −70 悬崖 ⇒ 不给GO")
        go = False
    return {"go": bool(go), "veto": vetoes, "reasons": reasons}


# ═══════════════════════════════════════════
# verdict 映射（scan_stock 生产口径）
# ═══════════════════════════════════════════

_STAGE_REV_ORD = {4: 3, 3: 2, 1: 1, 2: 0}   # ④下跌最高 … ②抬升最低（证据：两半窗同号）

# 2026-10-15 owner：「站上 MA20」在综合得分里的权重（其余项均 ±1）。
_W_MA20 = 3

# ⚠️ **go/得分口径的版本戳**（2026-10-15 加）：任何会改变 `timing_decision` 的 go 判定、
#    或 `timing_score` 数值的改动，**必须把这里改一下**。
#    理由：下游有**按日缓存**（如 GUI 的「每日 GO 只数」回算缓存 `t_io/cache/hunter_go_daily.json`、
#    以及 position_builder 的 trace 快照）。缓存只按"有没有/结构版本"判要不要重算，
#    **不跟规则走** ⇒ 改了口径不 bump，界面会长期显示旧口径的数（2026-10-15 实测：
#    总览说 GO 0 只、GO 历史图却说 13 只，两处打脸）。
GO_RULES_VERSION = "2026-10-15a"


def timing_score(features: dict, trend_ok: bool, golden_ok: bool,
                 data_insufficient: bool = False) -> int:
    """建仓「综合得分」（0~100）。

    ## 2026-10-15 owner 重构（两条要求）
    ① **取消大盘在得分中的计算**：原式「回撤到位」项是 `dd_threshold_ok(drawdown, regime)`，
       阈值**随 regime 变** ⇒ 大盘仍从这条路**间接进分**。实测同一只票（回撤 −12%）：
       `trend_dn` 得 **16**、`trend_up`/`range` 得 **30**。
       ⇒ 该项**整项替换为 regime-无关**的「关键支撑企稳」(`support_hold`)。
    ② **「站上 MA20」加大权重**：作为独立正项进分，权重 **`_W_MA20 = 3`**（其余项均 ±1）。
       方向取**正向**（站上=加分）——依据 owner 现场：得分最高的是"破位+④下跌"的票，
       他要把「站上 MA20」这条拉起来。

    ## 新式
        raw = −(多头结构) −(MACD金叉) −(站上MA5) + 3×(站上MA20) + (支撑企稳) + 日线阶段分
        score = round((raw + 3) / 10 × 100)          # raw ∈ [−3, 7] → 0~100

    ⚠️ 与旧式相比，本式**方向是混合的**：`−多头结构/−金叉/−站上MA5` 仍是 2026-10-11 那批
    **逆向**分量（实测"看起来越强反而越差"），而 `+站上MA20/+支撑企稳` 是 owner 新加的正向项。
    这是 owner 的明确选择，不是笔误；若实测发现两端互相抵消，应按 owner 再拍。

    ## 2026-10-11 的实测依据（保留，供追溯）
    原式 = 30×市场有方向 + 30×多头结构 + 30×回撤到位 + 10×MACD金叉 − 70×破MA5。

    实测（977 只 × 2265 日；**横截面 IC**，h1 定符号 → h2 **样本外**检验）：
      · **四个分量全为负 IC**，且两个半窗符号一致：
        多头结构 −0.048(t=−16.5)、站上MA5 −0.018(t=−6.9)、回撤到位 −0.005、MACD金叉 −0.006
        ⇒ "看起来越强" 反而后续越差（与本仓既有结论一致：超跌是 alpha）。
      · 日线阶段：②抬升 −0.042(t=−15.1)、④下跌 +0.034(t=+11.9)，两半窗一致；
        ①蓄势会翻号（+0.006/−0.015）⇒ 作基准 0，不当信号。
      · **市场项对当日全体个股相同 ⇒ 横截面 IC 恒为 0** ⇒ 不进此分（它只驱动 go/verdict 的择时）。
    结果：全窗 IC 由 **−0.0286 → +0.0188**（H=20；保留 MA5 扣分口径；不保留则为 +0.0375）。

    ⚠️ **0~100 只是线性外观映射**（raw∈[−4,3] → 0~100）；被检验过的是**排序**，不是这些数字本身。
    ⚠️ 数据不足 → 返回 0（fail-closed：不得因缺数据反而拿到中高分）。
    """
    if data_insufficient:
        return 0
    f = features or {}
    stage = int(f.get("stage_daily_code") or 1)
    raw = (-(1 if trend_ok else 0)
           - (1 if golden_ok else 0)
           - (1 if f.get("above_ma5") is True else 0)
           + _W_MA20 * (1 if f.get("above_ma20") is True else 0)      # 2026-10-15：加大权重项
           + (1 if f.get("support_hold") is True else 0)              # 2026-10-15：替代原「回撤到位」
           + _STAGE_REV_ORD.get(stage, 0))
    return int(round((raw + 3) / 10.0 * 100))


def verdict_from_timing(go: bool, regime: str, features: dict, data_insufficient: bool = False,
                        params: dict = None) -> tuple:
    """go/regime/features → (verdict, score)。
    go→signal；range+多头结构+浅回撤→watch_signal（只留痕不推送）；有方向且结构/回撤过一→approaching；否则 weak。
    data_insufficient（features={}）时结构/回撤/金叉一律不通过（数据失败不得伪装成条件通过）。
    params：ENTRY_TIMING_PARAMS（可选）；用于 ma5_below_penalty 减分阈值。"""
    f = features or {}
    _dir_ok = regime in ("trend_up", "trend_dn")
    _trend = bool(f.get("trend_multihead"))
    if data_insufficient:
        _dd_ok = False
    else:
        _dd_ok = dd_threshold_ok(float(f["drawdown"]), regime) if "drawdown" in f else False
    _golden = bool(f.get("macd_golden_5d"))
    # 2026-10-11 重构：证据化横截面分（见 timing_score）。市场项不再进分（IC 恒为 0）。
    # 2026-10-15：**不再传 `_dd_ok`** —— 它随 regime 变，是把大盘带进得分的唯一通路。
    # `_dd_ok` 本身仍算（门控/verdict 要用），只是不进分。
    _score = timing_score(f, _trend, _golden, data_insufficient)
    _p = params or DEFAULT_TIMING_PARAMS
    # 2026-10-08 owner：**未站上5日线**（收盘 ≤ MA5）→ ①综合得分大幅扣减 ②不给 signal（压成 approaching）。
    # ⚠️ 2026-10-11：本扣分**按 owner 决定保持不变**（先观察）。实测它把证据化增益砍掉约一半
    #    （H=20 IC：+0.0375 → +0.0188），且把 51.8% 的股票·日压到 0~30。
    #    是否放宽/反向，待观察列数据再定，勿擅自改。
    _below_ma5 = f.get("above_ma5") is False
    if _below_ma5:
        _score = max(0, _score - int(_p.get("ma5_below_penalty", 70)))
    # 2026-10-15 owner：「站上 MA20」硬性约束 —— 不站上则悬崖式扣减。
    # 与上面 MA5 那条同款、但幅度轻一档；两条可叠加（都破 ⇒ 更沉底）。
    if f.get("above_ma20") is False:
        _score = max(0, _score - int(_p.get("ma20_below_penalty", 40)))
    if go:
        _v = "signal"
    elif regime == "range" and _trend and _dd_ok:
        _v = "watch_signal"
    elif _dir_ok and (_trend or _dd_ok):
        _v = "approaching"
    else:
        _v = "weak"
    if _below_ma5 and _v == "signal" and bool(_p.get("ma5_below_block_signal", True)):
        _v = "approaching"
    return _v, _score


# ═══════════════════════════════════════════
# W35 日内右侧确认（15m 站上EMA8 + 放量 + 站上VWAP）
# ═══════════════════════════════════════════

def _resample_15min(df: pd.DataFrame) -> pd.DataFrame:
    """1分钟 → 15分钟聚合（与 analysis/indicators.resample_to_15min 同口径）。"""
    if df.empty or len(df) < 15:
        return pd.DataFrame()
    df = df.copy()
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    df["time_15m"] = df["time"].dt.floor("15min")
    agg = df.groupby("time_15m").agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum", "amount": "sum",
    }).reset_index()
    return agg.rename(columns={"time_15m": "time"})


def _add_15min_confirm_cols(df15: pd.DataFrame) -> pd.DataFrame:
    """W35 确认所需的两列（与 analysis/indicators.add_15min_indicators 中同名列同口径）。"""
    if df15.empty or len(df15) < 3:
        return df15
    c = df15["close"]
    df15["ema_fast_15m"] = c.ewm(span=8, adjust=False).mean()
    df15["vol_ma4_15m"] = df15["volume"].rolling(4, min_periods=1).mean()
    df15["vol_ratio_15m"] = df15["volume"] / df15["vol_ma4_15m"].replace(0, np.nan)
    return df15


def intraday_confirm(df_1min, vol_min: float = 1.2) -> tuple:
    """当日盘中右侧买点确认。返回 (passed, detail, insufficient)。

    无未来函数：只用截止最新一根【已收盘】15m bar 的数据；未收盘的当前根不参与。
    df_1min 为当日 1 分钟线（time/open/high/low/close/volume/amount）。数据不足时 insufficient=True。
    """
    if df_1min is None or df_1min.empty or len(df_1min) < 20:
        return False, "日内分钟数据不足", True
    d = df_1min.copy()
    d["time"] = pd.to_datetime(d["time"], errors="coerce")
    d = d.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)
    # 当日累计 VWAP（Σamount/Σvol；缺 amount 用 close*vol 代理）
    if "amount" in d.columns and d["amount"].fillna(0).sum() > 0:
        cum_amt = d["amount"].fillna(0).cumsum()
    else:
        cum_amt = (d["close"] * d["volume"].fillna(0)).cumsum()
    cum_vol = d["volume"].fillna(0).cumsum().replace(0, np.nan)
    d["vwap_cum"] = cum_amt / cum_vol

    df15 = _add_15min_confirm_cols(_resample_15min(d))
    if df15 is None or df15.empty:
        return False, "15分钟数据不足", True
    df15 = df15.copy()
    df15["time"] = pd.to_datetime(df15["time"], errors="coerce")
    last_min_ts = d["time"].iloc[-1]
    # 取最新一根【已收盘】15m bar（收盘时刻 <= 当日最新分钟+1min）
    closed = df15[(df15["time"] + pd.Timedelta(minutes=15)) <= (last_min_ts + pd.Timedelta(minutes=1))]
    if closed.empty:
        return False, "尚无已收盘15分钟bar", True
    bar = closed.iloc[-1]
    c = bar.get("close")
    ema8 = bar.get("ema_fast_15m")
    volr = bar.get("vol_ratio_15m")
    if any(pd.isna(x) for x in (c, ema8, volr)):
        return False, "15分钟指标NaN", True
    close_ts = bar["time"] + pd.Timedelta(minutes=15)
    vw_rows = d[d["time"] <= close_ts]
    vwap = float(vw_rows["vwap_cum"].iloc[-1]) if (not vw_rows.empty and pd.notna(vw_rows["vwap_cum"].iloc[-1])) else None
    ema_ok = float(c) > float(ema8)
    vol_ok = float(volr) > vol_min
    vwap_ok = (vwap is None) or (float(c) >= vwap)
    passed = ema_ok and vol_ok and vwap_ok
    detail = (f"15分钟确认: 站上EMA8={ema_ok}(c={float(c):.3f}/ema8={float(ema8):.3f}) "
              f"放量={vol_ok}(量比{float(volr):.2f}>{vol_min}) 站上VWAP={vwap_ok}"
              f"{f'(vwap={vwap:.3f})' if vwap is not None else ''}")
    return passed, detail, False
