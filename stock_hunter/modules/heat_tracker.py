# -*- coding: utf-8 -*-
"""
板块热度追踪模块 v2
职责：
1. 无条件每日落盘历史板块数据（不再依赖飞书推送）
2. 计算板块热度分（0-80；2026-10-10 起：量能放大由 +20 分翻转为 −10~0 扣分）
3. 输出趋势标签：🔥加速 / 📈升温 / ➡️平稳 / 📉退潮 / 🧊冰点

v2 改动（vs v1 fix1 审核）：
- 量能放大 Bug 1：去掉 `if past_entries` 门控，首次运行也能回查前5日
- 量能放大 Bug 2：统一字段名 `成交额总额` + 统一单位为元
- 个股去重：按代码去重后再算广度/成交额，多概念板块不重复计数
- 趋势公式：线性回归斜率识别过去方向 + 今日vs昨日识别边际变化
- 冷启动：`"热度分" in s` 替代 `s.get("热度分", 0) > 0`
"""
import os
import json
import pandas as pd
from typing import Dict, List, Optional


def _avg(values: list) -> float:
    """安全计算平均值，空列表返回 0"""
    return sum(values) / len(values) if values else 0.0


# ---- 热度分权重（2026-10-10 重排，合计仍为 100）----
# 依据见 `doc/research/2026-10-10_板块强弱度_成分股数量差异实验.md`（含独立复核）。
# 目标 = 未来 1/5 日板块**超额**收益（减当日横截面均值），每日横截面算 IC 再跨日平均。
#
# ⚠️ **两套成分口径的复核结果不同，以下只写两边都成立的部分**：
#   九眼概念口径（22 组，500 天）      ：涨停率 0.0454(t=3.90) / 0.0389(t=3.31)
#   行业口径（124 组，201 天，独立复核）：涨停率 0.0370(t=3.28) / 0.0394(t=3.40)
#   ⇒ **涨停率（涨停密度）是唯一在两种口径、两个持有期上都 t>3 的口径** —— 30 分保留。
#   上涨广度 两边都是最弱之一（0.0011(t=0.08) / 0.0017(t=0.09)）⇒ 20 → 10 分。
#   龙头集中度 两边全横截面都为正（概念 0.0289/0.0393；行业 0.0325(t=1.96)/0.0365(t=2.14)），
#      且现有分项**全是广度**、没有「集中度」维度 ⇒ 新增 10 分。
#      ⚠️ 但它「专治大板块」这个说法**没有复现**：概念口径大板块组 +0.0390，
#      行业口径大板块组 **−0.0048(t=−0.22)**。故新增它的理由只是「全横截面为正 + 维度互补」，
#      **不是**「大板块缺它」。同理「信号只在小板块」也只是方向复现（小组 t=3.14/3.46
#      vs 大组 t=0.80），概念口径那个「大板块精确为零」是口径特有的，勿当普适结论。
#   量能放大 横截面 IC ≈ 0（−0.0018/−0.0035，概念口径 0.0073/0.0075），但它是**时序**概念
#      （本板块今日 vs **自身**前 5 日），横截面 IC 测不到它的用途 ⇒ **不动**（未验证，非已否决）。
#   前排强度 未单独测（与涨停密度同源）⇒ 不动。
#
# ---- 2026-10-10 补做：量能放大的**时序**成分（它是否被放错位置）----
# `tmp/exp_amp_timeseries.py`（组内固定效应，把 amp 与 fwd 都先按板块再按日去均值）：
#   行业口径 124 组/日 403 天：组内 corr(amp, fwd) = **−0.0335 (t=−6.90, n=42,367)**
#   概念口径  22 组/日 同窗口：                 = **−0.0294 (t=−2.71)**
#   控制「板块当日涨幅」后 amp 净系数：**−0.411pp/1x (t=−8.52)** / −0.472pp/1x (t=−3.69)
#     （当日涨幅系数为**正** +7.30pp/1% t=+6.06 ⇒ 动量是正的；放量在动量之上**独立为负**）
#   事件研究（上穿阈值后 5 日收益 − 非事件日）：
#     上穿 1.5x: Δ=+0.092pp (t=0.89)  ← 无信号
#     上穿 2.0x: Δ=−0.162pp (t=−0.83)
#     上穿 3.0x: Δ=**−1.287pp (t=−2.61)**
# ⇒ **量能放大不是"被冤枉"，是符号反的**：放量之后收益更低，而原实现给高放量 +20 分
#   ⇒ 它在**奖励**预测下跌的条件。已改为**只扣分**（见 _W_AMP_PENALTY）。
_W_LIMIT_DENSITY = 30.0
_W_UP_BREADTH = 10.0          # 20 → 10（2026-10-10）
_W_STRONG_BREADTH = 20.0
_W_AMP_PENALTY = 10.0         # 量能放大：原 +20 分（0~20）→ 改为 −10~0 的**扣分**
_AMP_PENALTY_FROM = 1.5       # 放量 <1.5x 不扣（事件研究实测该区间无信号）
_W_FRONT = 10.0
_W_LEAD = 10.0                # 2026-10-10 新增
# ⚠️ 上限因此由 100 降为 **80**（30+10+20+0+10+10）。**未**把那 20 分重新分配——
#    重新分配需要为其它分项的函数形式（斜率/封顶）做没有证据的猜测，而热度分只用于
#    **相对排序**与**自身历史斜率**，绝对值不影响排序。代价：切换点附近「趋势」字段
#    会有约 5 天的短暂失真（自身历史序列在新旧标尺交界处）。
#    绝对阈值检查：全仓无「热度分 > X」类判断（只有 argmax/argmin 取最高最低板块）⇒ 安全。
# 龙头集中度(%) → 分：实测 P10=2.8%→2.2分、中位=5.9%→4.7分、P90=11.0%→8.8分
_LEAD_K = 0.8
#
# ⚠️ 证据强度：t≈2-4 属边际；成分股用**当前** watchlist 回溯历史 ⇒ 有前视偏差（且部分概念标签
#    是 2026-08 之后才建的，对早期窗口不成立），故只做「换 10 分」这一最小改动，
#    **不**按 IC 大小重排全部权重（IC 在持有期之间会变号）。


def _linear_slope(y: list) -> float:
    """对时间序列 y 做线性回归斜率（最小二乘法），返回每步变化量，空或单值返回 0"""
    n = len(y)
    if n < 2:
        return 0.0
    x = [i for i in range(n)]
    mean_x = _avg(x)
    mean_y = _avg(y)
    numerator = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    denominator = sum((xi - mean_x) ** 2 for xi in x)
    return numerator / denominator if denominator != 0 else 0.0


# ---- 热度分口径版本（2026-10-10 引入）----
# **规则：只要改了任一权重、或某个分项的函数形式（斜率/封顶/符号），就必须 +1。**
# 为什么必须：`趋势`（🔥加速/📈升温/➡️平稳/📉退潮/🧊冰点）是对**该板块自身近 5 日热度分**
# 做线性回归取斜率。若这 5 天横跨了新旧两套标尺，斜率就是假的——表现为切换点附近
# 全体板块被误判成"退潮"，且会持续约 5 个交易日。
# `_get_past_scores` 只取与当前版本**相同**的历史分值；不匹配则视为缺失，
# 于是 `_compute_trend` 走"样本不足"分支返回 ➡️平稳 —— 宁可**显式不知道**，也不要**算错**。
# 自愈：连续跑满 5 个交易日后，新版本历史自然攒够。
# 历史包袱：所有旧条目没有该字段 ⇒ 一律按 v1 处理（与当前 v2 不兼容，会被跳过）。
_HEAT_VERSION = 2   # v1 = 2026-10-10 之前（量能放大 +0~20 分、上涨广度 20 分、无龙头集中度）
                    # v2 = 2026-10-10 起（量能放大改为 −10~0 扣分、上涨广度 10 分、新增龙头集中度 10 分）


def _history_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "history", "daily_summary.json")


def load_history() -> dict:
    path = _history_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_daily_summary(date_str: str, sectors: list) -> None:
    """无条件保存当日板块数据到 history"""
    path = _history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    history = load_history()
    history[date_str] = sectors
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def compute_heat_scores(
    summary_df: pd.DataFrame,
    sectors_dict: Dict[str, pd.DataFrame],
    date_str: str,
) -> pd.DataFrame:
    """
    计算各板块热度分（0-80，2026-10-10 起；量能放大改为扣分），合并到 summary_df 并返回。

    热度分构成：
      - 涨停密度：30 分（涨停数占总股票比）
      - 上涨广度：20 分（上涨家数占比，基于涨跌幅>0）
      - 强涨广度：20 分（涨幅>3%家数占比）
      - 量能放大：20 分（今日成交额 / 5日均额）
      - 前排强度：10 分（D1/D2 前排率）
    """
    if summary_df is None or summary_df.empty or "板块" not in summary_df.columns:
        return summary_df

    history = load_history()
    # 提前计算日期索引（不带门控）
    sorted_dates = sorted(history.keys())
    today_idx = sorted_dates.index(date_str) if date_str in sorted_dates else len(sorted_dates)
    heat_rows = []

    for _, row in summary_df.iterrows():
        sector_name = str(row.get("板块", ""))
        if not sector_name or sector_name not in sectors_dict:
            heat_rows.append(_empty_heat_row(sector_name))
            continue

        df = sectors_dict[sector_name]

        # ---- 按代码去重（同股多概念只计一次）----
        if "代码" in df.columns:
            df_dedup = df.drop_duplicates(subset=["代码"])
        else:
            df_dedup = df
        total = max(len(df_dedup), 1)

        # ---- 原始指标（使用去重后的 df）----
        # 4.1: 涨停密度用去重后 df 的"涨停"字段，避免同股多行重复计数
        if "涨停" in df_dedup.columns:
            limit_up_count = int(df_dedup["涨停"].sum())
        else:
            limit_up_count = int(row.get("涨停数", 0))

        # 涨跌幅列（data_loader 已统一为涨跌幅，旧CSV数据仍用涨停率%）
        change_col = "涨跌幅" if "涨跌幅" in df_dedup.columns else ("涨停率%" if "涨停率%" in df_dedup.columns else None)
        if change_col and change_col in df_dedup.columns:
            up_count = int((df_dedup[change_col] > 0).sum())
            strong_up_count = int((df_dedup[change_col] > 3).sum())
        else:
            up_count = 0
            strong_up_count = 0

        amount_total = float(df_dedup.get("成交额", 0).sum() or 0)
        front_count = int(
            ((df_dedup.get("D1强势形态且新高", 0) > 0) | (df_dedup.get("D2强势形态", 0) > 0)).sum()
        )

        up_ratio = up_count / total
        strong_up_ratio = strong_up_count / total
        front_ratio = front_count / total

        # ---- 成交额放大倍数：今日 / 前5日均额（Bug 1+2 修复）----
        past_amounts = []
        for i in range(max(0, today_idx - 5), today_idx):
            day_key = sorted_dates[i]
            day_data = history.get(day_key, [])
            for s in day_data:
                if s.get("板块") == sector_name and s.get("成交额总额", 0) > 0:
                    past_amounts.append(s["成交额总额"])  # 统一字段名：成交额总额，单位为元
        avg_amount_5d = _avg(past_amounts) if past_amounts else amount_total
        amount_amplify = amount_total / max(avg_amount_5d, 1)

        # ---- 龙头集中度 (0-10)：Top3 成员涨幅均值 − 其余成员涨幅均值 ----
        # 为什么加它：现有分项**全是广度**（涨停密度/上涨/强涨/前排），没有「集中度」维度，
        # 而它在**全横截面**上两种成分口径、两个持有期都为正（行业 0.0325(t=1.96)/0.0365(t=2.14)；
        # 概念 0.0289(t=2.29)/0.0393(t=3.33)）⇒ 是**维度互补**的边际增益。
        # ⚠️ 注意：它「专治大板块」的说法**未复现**（行业口径大板块组 −0.0048），别按那个理由用。
        # 注：lead **恒为正**（Top3 均值必然 ≥ 其余均值），故它衡量「龙头领先多少」而非「是否领先」。
        lead_score = 0.0
        if change_col and change_col in df_dedup.columns:
            _chg = [float(v) for v in df_dedup[change_col].tolist() if pd.notna(v)]
            if len(_chg) > 4:
                _s = sorted(_chg, reverse=True)
                _lead_pct = sum(_s[:3]) / 3.0 - sum(_s[3:]) / len(_s[3:])
                lead_score = min(max(_lead_pct * _LEAD_K, 0.0), _W_LEAD)

        # ---- 热度分计算 ----
        # 涨停密度 (0-30)
        limit_density = limit_up_count / total
        limit_score = min(limit_density * 150, _W_LIMIT_DENSITY)

        # 上涨广度 (0-10，2026-10-10 由 20 下调)
        up_score = up_ratio * _W_UP_BREADTH

        # 强涨广度 (0-20)
        strong_up_score = strong_up_ratio * _W_STRONG_BREADTH

        # 量能放大 (**扣分** −10~0，2026-10-10 由 +20 分翻转)
        # 原式 `min(max((amp-1)*10,0),20)` 给高放量 +20 分，而实测放量之后收益**更低**
        # （组内 corr −0.0335 t=−6.90；控制当日涨幅后 −0.411pp/1x t=−8.52）⇒ 原式在奖励
        # 预测下跌的条件。现改为：放量 <1.5x 不扣（实测该区间无信号），之上每 1x 扣 10 分、封顶 10。
        amount_score = -min(max(amount_amplify - _AMP_PENALTY_FROM, 0.0) * 10.0, _W_AMP_PENALTY)

        # 前排强度 (0-10)
        front_score = front_ratio * _W_FRONT

        heat_score = round(min(
            limit_score + up_score + strong_up_score + amount_score + front_score
            + lead_score, 100
        ), 1)

        # ---- 趋势计算 ----
        trend = _compute_trend(sector_name, heat_score, history, sorted_dates, today_idx)

        heat_rows.append({
            "板块": sector_name,
            "热度分": heat_score,
            "趋势": trend,
            "涨停密度分": round(limit_score, 1),
            "上涨广度分": round(up_score, 1),
            "强涨广度分": round(strong_up_score, 1),
            "量能放大分": round(amount_score, 1),
            "前排强度分": round(front_score, 1),
            "龙头集中度分": round(lead_score, 1),
            "上涨家数占比%": round(up_ratio * 100, 1),
            "涨幅>3%占比%": round(strong_up_ratio * 100, 1),
            "成交额放大倍数": round(amount_amplify, 2),
            "成交额总额(亿)": round(amount_total / 1e8, 2),
        })

    heat_df = pd.DataFrame(heat_rows)

    # 合并到 summary_df
    merged = summary_df.merge(heat_df, on="板块", how="left")

    # 重组列顺序
    preferred_cols = ["排名", "板块", "热度分", "趋势", "细分数量", "股票数量",
                      "平均分", "前排率%", "后排率%", "涨停数",
                      "龙头集中度分", "上涨家数占比%", "涨幅>3%占比%",
                      "成交额放大倍数", "成交额总额(亿)",
                      "最强细分", "最强细分得分", "前三强"]
    cols = [c for c in preferred_cols if c in merged.columns] + \
           [c for c in merged.columns if c not in preferred_cols]
    merged = merged[cols]

    # 按热度分降序重排排名
    if "热度分" in merged.columns:
        merged = merged.sort_values("热度分", ascending=False).reset_index(drop=True)
        merged["排名"] = range(1, len(merged) + 1)

    # 无条件保存历史（含扩展字段）
    _save_sector_history(date_str, merged, heat_rows)

    return merged


def _compute_trend(sector: str, current_score: float, history: dict,
                   sorted_dates: list, today_idx: int) -> str:
    """
    计算趋势标签 v2 — 线性回归斜率 + 今日 vs 昨日

    两信号结合：
    1. 对前 5 日热度分做线性回归 → 斜率识别过去方向
    2. 今日 vs 昨日 → 识别当日边际变化
    3. 综合判标签
    阈值：斜率 >3 → 加速，>1 → 升温，[-1,1] → 平稳，<-3 → 冰点，之间 → 退潮
    """
    scores_5d = _get_past_scores(sector, history, sorted_dates, today_idx - 1, 5)
    yesterday_score = scores_5d[-1] if scores_5d else None

    if not scores_5d or len(scores_5d) < 2:
        # 4.3: 冷启动阈值与主分支统一（±3/±1）
        if yesterday_score is not None:
            delta = current_score - yesterday_score
            if delta >= 3:
                return "📈升温"
            elif delta <= -3:
                return "📉退潮"
            elif abs(delta) < 1:
                return "➡️平稳"
            else:
                return "📈升温" if delta > 0 else "📉退潮"
        return "➡️平稳"

    # 1. 线性回归斜率：把今日也纳入回归序列
    all_scores = scores_5d + [current_score]
    reg_slope = _linear_slope(all_scores)

    # 2. 今日 vs 昨日（边际变化）
    today_vs_yesterday = current_score - yesterday_score if yesterday_score is not None else 0

    # 3. 综合分
    combined = reg_slope * 0.6 + today_vs_yesterday * 0.4

    # 4.2: 高位滞涨降档 — 回归斜率显示强势上行，但今日边际近乎停滞或反转
    if reg_slope > 2 and today_vs_yesterday <= 0:
        combined -= 2

    if combined >= 3:
        return "🔥加速"
    elif combined >= 1:
        return "📈升温"
    elif combined >= -1:
        return "➡️平稳"
    elif combined >= -3:
        return "📉退潮"
    else:
        return "🧊冰点"


def _get_past_scores(sector: str, history: dict, sorted_dates: list,
                     end_idx: int, count: int) -> list:
    """
    获取板块历史热度分（最近 count 个有效交易日）
    P1 修复：过滤条件改为 `"热度分" in s`（字段存在性），而非值非零
    """
    scores = []
    for i in range(max(0, end_idx - count + 1), end_idx + 1):
        date_key = sorted_dates[i]
        day_data = history.get(date_key, [])
        for s in day_data:
            if s.get("板块") == sector and "热度分" in s:
                # 版本闸（2026-10-10）：只取与当前 _HEAT_VERSION 相同的分值。跨版本标尺不可比
                # （权重与函数形式都变过），混在一起回归出的斜率是假的。旧条目无该字段 ⇒ 按 v1 跳过。
                try:
                    _v = int(s.get("热度版本", 1) or 1)
                except Exception:
                    _v = 1
                if _v != _HEAT_VERSION:
                    continue
                scores.append(s["热度分"])
    return scores


def _save_sector_history(date_str: str, summary_df: pd.DataFrame, heat_rows: list) -> None:
    """保存带热度分的完整板块历史。成交额统一存原始单位元，字段名 `成交额总额`"""
    sectors = []
    for hr in heat_rows:
        sector_name = hr.get("板块", "")
        match = summary_df[summary_df["板块"] == sector_name] \
            if "板块" in summary_df.columns else pd.DataFrame()

        # 成交额还原为元（热力行存的是亿，summary 原始无此列则从 heat_row 反推）
        amount_yi = float(hr.get("成交额总额(亿)", 0) or 0)
        amount_yuan = amount_yi * 1e8

        entry = {
            "板块": sector_name,
            "热度分": hr.get("热度分", 0),
            # 口径版本（2026-10-10）：改权重/函数形式必须 +1（见 _HEAT_VERSION 注释）。
            # `_get_past_scores` 用它把跨版本的分值挡在趋势回归之外。
            "热度版本": _HEAT_VERSION,
            "趋势": hr.get("趋势", "➡️平稳"),
            "排名": int(match.iloc[0]["排名"]) if not match.empty and "排名" in match.columns else 0,
            "平均分": float(match.iloc[0]["平均分"]) if not match.empty and "平均分" in match.columns else 0,
            "涨停数": int(hr.get("涨停数", 0)) if "涨停数" in hr else (
                int(match.iloc[0]["涨停数"]) if not match.empty and "涨停数" in match.columns else 0
            ),
            "股票数量": int(hr.get("股票数量", 0)) if "股票数量" in hr else (
                int(match.iloc[0]["股票数量"]) if not match.empty and "股票数量" in match.columns else 0
            ),
            "前排率%": float(hr.get("前排率%", 0)) if "前排率%" in hr else 0,
            "成交额总额": amount_yuan,   # 单位：元（Bug 2 修复）
            "成交额放大倍数": hr.get("成交额放大倍数", 1.0),
            "上涨家数占比%": hr.get("上涨家数占比%", 0),
        }
        sectors.append(entry)
    save_daily_summary(date_str, sectors)


def _empty_heat_row(sector_name: str) -> dict:
    return {
        "板块": sector_name,
        "热度分": 0,
        "趋势": "➡️平稳",
        "涨停密度分": 0, "上涨广度分": 0, "强涨广度分": 0,
        "量能放大分": 0, "前排强度分": 0,
        "上涨家数占比%": 0, "涨幅>3%占比%": 0,
        "成交额放大倍数": 1.0, "成交额总额(亿)": 0,
    }
