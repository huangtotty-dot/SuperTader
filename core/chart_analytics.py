# -*- coding: utf-8 -*-
"""K 线图表纯算法：箱体、通道、支撑压力与黄金分割。

本模块不加载行情提供方；numpy/pandas 及分形算法均在使用时局部导入。
"""

# 黄金分割（斐波那契回撤/扩展）参数 —— 按周期分档，越长的周期要求越大的摆动幅度
_FIB_LOOKBACK = {"daily": 120, "weekly": 120, "monthly": 60,
                 "min5": 160, "min10": 160, "min30": 160, "min60": 160}
_FIB_FRACTAL_N = {"daily": 3, "weekly": 2, "monthly": 1,
                  "min5": 2, "min10": 2, "min30": 2, "min60": 2}
_FIB_MIN_AMP = {"daily": 5.0, "weekly": 8.0, "monthly": 12.0,
                "min5": 3.0, "min10": 3.0, "min30": 3.0, "min60": 3.0}
_FIB_RETRACE = [0.236, 0.382, 0.5, 0.618, 0.786]
_FIB_EXTENSION = [1.272, 1.618]
_MIN_PERIODS = ("min5", "min10", "min30", "min60")


def _detect_boxes(daily):
    """检测箱体（P1修复）：严格触及标准 + 优化置信分。
    分位数(88/12)定义初期边界，严格触及验证，置信分=触及质量+时间持久度+宽度合理性。

    P1修复内容：
    1. 触及标准从松散(±0.8-8.8%)改为精确：必须在边界±0.5%内触及
    2. 置信分权重调整：触及质量优先于触及次数
    3. 宽度范围分类：<5%(微箱) 5-12%(正常) 12-22%(宽幅)，权重不同
    4. 合并逻辑改进：避免过度合并历史箱体
    """
    import numpy as np
    d = daily
    if len(d) < 30:
        return []

    recent = d.tail(150).reset_index(drop=True)
    closes = recent["close"].values
    highs = recent["high"].values
    lows = recent["low"].values
    dates = recent["date"].values
    n = len(recent)
    last_close = float(closes[-1])
    last_date = dates[-1]

    WIN = 30
    # 向量化滑窗：分位数/斜率/触及全窗口一次算完（与逐窗循环结果一致，快 ~15x）
    from numpy.lib.stride_tricks import sliding_window_view
    wh = sliding_window_view(highs, WIN)
    wl = sliding_window_view(lows, WIN)
    wc = sliding_window_view(closes, WIN)
    ups = np.percentile(wh, 88, axis=1)
    dns = np.percentile(wl, 12, axis=1)
    _xc = np.arange(WIN) - (WIN - 1) / 2.0
    _denom = float(np.sum(_xc * _xc))
    _slopes = (wc @ _xc) / _denom
    _means = wc.mean(axis=1)
    _rel_slopes = np.abs(_slopes) / np.where(_means == 0, 1e-9, _means)
    # P1修复：触及标准从±0.8-8.8%改为±0.5%(更精确)
    _up_touches = np.sum(wh >= (ups * 0.995)[:, None], axis=1)  # 99.5% 以上算"精确触及"
    _dn_touches = np.sum(wl <= (dns * 1.005)[:, None], axis=1)  # 100.5% 以下算"精确触及"
    _widths = (ups - dns) / np.where(_means == 0, 1e-9, _means) * 100
    # 滑窗收集候选箱体（用区间位置唯一标识，避免重复）
    boxes = {}
    for start in range(0, n - WIN + 1, 3):
        up = float(ups[start])
        dn = float(dns[start])
        rel_slope = float(_rel_slopes[start])
        up_touch = int(_up_touches[start])
        dn_touch = int(_dn_touches[start])
        width_pct = float(_widths[start])
        # P1修复：候选条件更严格 — 横盘<0.3%/天(rather than 0.5%) + 宽度3-22% + 双边精确触及≥2
        if rel_slope < 0.003 and 3.0 <= width_pct <= 22.0 and up_touch >= 2 and dn_touch >= 2:
            key = (round(up, 3), round(dn, 3))
            # P1修复：置信分优化 — 触及质量(precision)优先于触及次数
            # 触及质量分 = (精确触及数 * 2)，其次是横盘度，最后是宽度适中
            touch_quality = (up_touch + dn_touch) * 2.0  # 优先权最高
            flatness = max(0, (0.003 - rel_slope) / 0.003) * 2.0  # 越横盘越好
            # 宽度权重分化：正常箱体(5-15%)得分最高
            if 5 <= width_pct <= 15:
                width_score = 1.5
            elif 3 <= width_pct < 5 or 15 < width_pct <= 22:
                width_score = 0.5
            else:
                width_score = 0
            conf = touch_quality + flatness + width_score
            if key not in boxes or conf > boxes[key]["conf"]:
                s = dates[start].strftime("%Y-%m-%d") if hasattr(dates[start], "strftime") else str(dates[start])[:10]
                e = dates[start + WIN - 1].strftime("%Y-%m-%d") if hasattr(dates[start+WIN-1], "strftime") else str(dates[start+WIN-1])[:10]
                boxes[key] = {"start": s, "end": e, "low": round(dn, 3), "high": round(up, 3),
                              "touches": (up_touch, dn_touch), "width": round(width_pct, 1),
                              "conf": round(conf, 1), "rel": 0}

    # 关联现价关系 + 刚突破判定（改进）
    # 刚突破的定义现在基于箱体宽度动态调整，而不是固定的20天+15%
    result = []
    today_days = 999
    try:
        today_days = int(pd.Timestamp(last_date).timestamp() / 86400 - pd.Timestamp(dates[0]).timestamp() / 86400)
    except Exception:
        pass
    for key, b in boxes.items():
        end_date = b["end"]
        try:
            end_dt = pd.Timestamp(end_date)
            days_since = int((pd.Timestamp(last_date) - end_dt).days)
        except Exception:
            days_since = 999

        # P1修复：刚突破判定改为动态，基于箱体宽度
        width = b["width"]
        if width < 5:
            # 微箱体(宽度<5%)：突破后5天内+10%以内算"刚突破"
            recently_broke = days_since <= 5 and (last_close - b["high"]) / b["high"] < 0.10 if b["high"] else False
        elif width < 15:
            # 正常箱体(5-15%)：突破后10天内+12%以内算"刚突破"
            recently_broke = days_since <= 10 and (last_close - b["high"]) / b["high"] < 0.12 if b["high"] else False
        else:
            # 宽幅箱体(>15%)：突破后15天内+15%以内算"刚突破"
            recently_broke = days_since <= 15 and (last_close - b["high"]) / b["high"] < 0.15 if b["high"] else False

        if b["low"] <= last_close <= b["high"]:
            b["rel"] = 0  # 现价在箱体内
        elif last_close > b["high"] and recently_broke:
            b["rel"] = 1  # 刚突破上方
        elif last_close > b["high"]:
            b["rel"] = -1  # 上方历史箱体
        else:
            b["rel"] = -2  # 下方历史箱体
        # 距现价距离（用于排序）
        b["dist"] = abs(b["center"] if "center" in b else (b["high"] + b["low"]) / 2 - last_close)
        result.append(b)

    # 合并重叠箱体（P1改进：更严格的重叠条件，避免过度合并）
    def overlap(a, b):
        price_overlap = min(a["high"], b["high"]) - max(a["low"], b["low"])
        price_span = min(a["high"] - a["low"], b["high"] - b["low"])
        # P1修复：价格重叠从>50%改为>80%（更严格，保留历史分阶段特征）
        price_overlap_pct = price_overlap / max(price_span, 1e-9) if price_span > 0 else 0
        # 时间重叠条件也更严格：不仅要有交集，还要至少重叠5天
        try:
            s1 = pd.Timestamp(a["start"])
            e1 = pd.Timestamp(a["end"])
            s2 = pd.Timestamp(b["start"])
            e2 = pd.Timestamp(b["end"])
            overlap_days = (min(e1, e2) - max(s1, s2)).days
            t_overlap = overlap_days >= 5
        except Exception:
            t_overlap = a["end"] > b["start"] and b["end"] > a["start"]
        return price_overlap_pct > 0.8 and t_overlap

    merged = []
    for b in result:
        hit = None
        for m in merged:
            if overlap(m, b):
                hit = m
                break
        if hit:
            hit["low"] = min(hit["low"], b["low"])
            hit["high"] = max(hit["high"], b["high"])
            hit["start"] = min(hit["start"], b["start"])
            hit["end"] = max(hit["end"], b["end"])
            hit["conf"] = round(hit["conf"] + b["conf"], 1)
            hit["touches"] = (max(hit["touches"][0], b["touches"][0]), max(hit["touches"][1], b["touches"][1]))
        else:
            merged.append(dict(b))

    # 重算 rel + center + days + 近期有效性
    import datetime as _dt
    # 2026-10-23：「近期」必须以**这次判定的 as-of 日**（切片末根）为基准，不能用 wall-clock。
    # 原来写 `datetime.now()`，于是对历史切片判箱时，凡是 45 天前结束的箱体全被丢掉 ⇒
    # 猎手「🔄 扫描该日」选 45 天以前的日期**永远扫不出突破**（静默返回空），
    # 回测长窗口也被截成最后约 20 个交易日（第一轮箱体研究就吃了这个）。
    # 实时路径切片末根=今日，与原行为完全一致。
    try:
        now_dt = _dt.datetime.strptime(str(last_date)[:10], "%Y-%m-%d")
    except Exception:
        now_dt = _dt.datetime.now()
    recent_valid = []
    for b in merged:
        b["center"] = round((b["high"] + b["low"]) / 2, 3)
        try:
            s = _dt.datetime.strptime(b["start"][:10], "%Y-%m-%d")
            e = _dt.datetime.strptime(b["end"][:10], "%Y-%m-%d")
            b["days"] = (e - s).days
            b["days_since_end"] = (now_dt - e).days
        except Exception:
            b["days"] = 0
            b["days_since_end"] = 999
        # 只保留近期箱体（结束距今 ≤45 天），远历史箱体无参考意义
        if b["days_since_end"] > 45:
            continue
        if b["low"] <= last_close <= b["high"]:
            b["rel"] = 0
        elif last_close > b["high"]:
            b["rel"] = -1
        else:
            b["rel"] = -2
        recent_valid.append(b)

    # 排序：现价箱体(rel=0) > 上方历史(rel=-1) > 下方历史(rel=-2)；再按置信分
    recent_valid.sort(key=lambda b: (
        0 if b["rel"] == 0 else 1 if b["rel"] == -1 else 2,
        -b["conf"]))

    # P1改进：为K线图添加箱体质量评分信息
    for b in recent_valid:
        # 质量评分维度
        width = b["width"]
        touches = b["touches"]
        days = b["days"]

        # 宽度评级
        if width < 5:
            width_grade = "微"  # 微箱体，敏感但容易假突破
        elif width < 15:
            width_grade = "优"  # 正常箱体，最稳定
        else:
            width_grade = "宽"  # 宽幅箱体，波动大

        # 触及质量评级（基于精确触及次数）
        touch_quality = touches[0] + touches[1]
        if touch_quality >= 6:
            touch_grade = "极强"  # 4+次精确触及，非常稳定
        elif touch_quality >= 4:
            touch_grade = "强"    # 2-3次精确触及，较稳定
        else:
            touch_grade = "弱"    # 1次精确触及，可能噪音

        # 综合质量评分（1-10分）
        quality_score = (touch_quality * 1.5 + max(0, 15 - width) * 0.3 + max(0, 30 - days) * 0.1)
        quality_score = min(10, max(1, round(quality_score, 1)))

        b["width_grade"] = width_grade
        b["touch_grade"] = touch_grade
        b["quality_score"] = quality_score
        # 用于K线图显示的关键字段
        b["display"] = f"{width_grade}箱({width:.1f}%) 触及{touch_grade}({touch_quality}次) 质量{quality_score}/10"

    return recent_valid[:3]


def _detect_channel(daily):
    """检测上行/下行通道：最近 40 日高点/低点线性回归 → 上下轨。
    斜率>0 上行通道，<0 下行通道。返回双轨线端点 + 方向。"""
    import numpy as np
    d = daily
    if len(d) < 25:
        return {"direction": "flat", "slope": 0, "up_line": [], "dn_line": []}
    recent = d.tail(40).reset_index(drop=True)
    n = len(recent)
    x = np.arange(n)
    highs = recent["high"].values
    lows = recent["low"].values

    def regline(vals):
        slope, intercept = np.polyfit(x, vals, 1)
        return slope, intercept, slope * (n - 1) + intercept

    up_slope, up_i, up_end = regline(highs)
    dn_slope, dn_i, dn_end = regline(lows)

    slope = (up_slope + dn_slope) / 2
    # 归一化斜率：每日变化 / 均价，>0.15%/天 = 上行
    avg_price = float(recent["close"].mean()) or 1e-9
    norm_slope = slope / avg_price
    direction = "up" if norm_slope > 0.0015 else ("down" if norm_slope < -0.0015 else "flat")

    # 通道方向反转检测：用更早的 40 日（第 40~80 根往前）回归，与当前方向对比
    reversal = None
    try:
        if len(d) >= 80:
            prev = d.iloc[-80:-40].reset_index(drop=True)
            if len(prev) >= 25:
                px = np.arange(len(prev))
                ph = prev["high"].values
                pl = prev["low"].values
                p_slope, _ = np.polyfit(px, ph, 1)
                p_slope2, _ = np.polyfit(px, pl, 1)
                p_norm = (p_slope + p_slope2) / 2 / (float(prev["close"].mean()) or 1e-9)
                prev_dir = "up" if p_norm > 0.0015 else ("down" if p_norm < -0.0015 else "flat")
                if direction in ("up", "down") and prev_dir in ("up", "down") and direction != prev_dir:
                    reversal = "up_to_down" if prev_dir == "up" else "down_to_up"
    except Exception:
        reversal = None

    # 趋势描述补充
    start_price = float(recent["close"].iloc[0])
    end_price = float(recent["close"].iloc[-1])
    ret_40d = (end_price - start_price) / start_price * 100 if start_price else 0
    # 现价在通道中的位置（0=下轨, 100=上轨）：用回归线在当前 x(n-1) 处的值
    up_here, dn_here = up_end, dn_end
    pos_pct = (end_price - dn_here) / (up_here - dn_here) * 100 if up_here != dn_here else 50

    return {
        "direction": direction,
        "slope": round(float(slope), 4),
        "norm_slope_pct": round(norm_slope * 100, 3),
        "up_line": [round(float(up_i), 3), round(float(up_end), 3)],
        "dn_line": [round(float(dn_i), 3), round(float(dn_end), 3)],
        "ret_40d": round(float(ret_40d), 1),
        "pos_pct": round(max(0, min(100, float(pos_pct))), 0),
        "reversal": reversal,   # up_to_down / down_to_up / None
    }


def _calc_support_resistance(daily):
    """按现价强制分类 + 聚类合并 + 强度排序 → 每个方向保留最重要的 3 个。"""
    import pandas as pd
    d = daily
    last_close = float(d["close"].iloc[-1])
    H, L, C = float(d["high"].iloc[-1]), float(d["low"].iloc[-1]), last_close
    PP = (H + L + C) / 3
    candidates = []  # (price, label, strength)

    def add(price, label, strength):
        if not price or price <= 0 or price is None: return
        candidates.append((round(float(price), 2), label, strength))

    # pivot 中枢（注意: R1=2PP-L 才是上方压力, S1=2PP-H 是下方支撑, 但都可能失效）
    add(2 * PP - L, "R1中枢", 2)
    add(2 * PP - H, "S1中枢", 2)
    # 均线（近价均线更有效）
    for n, label, base_strength in ((20, "MA20", 2), (60, "MA60", 2), (180, "MA180", 1)):
        val = d[f"ma{n}"].iloc[-1]
        if not pd.isna(val):
            add(float(val), label, base_strength)
    # 前高/前低（120日局部极值）
    recent = d.tail(120)
    for idx in range(5, len(recent) - 1):
        r = recent.iloc[idx]
        win = recent.iloc[max(0, idx - 3):idx + 4]
        if r["high"] == win["high"].max() and float(r["high"]) > 0:
            add(float(r["high"]), "前高", 2)
        if r["low"] == win["low"].min() and float(r["low"]) > 0:
            add(float(r["low"]), "前低", 2)
    # 量密集区（成交量大的价位，参考性强）
    top_vol = d.nlargest(8, "volume")
    for _, r in top_vol.iterrows():
        add(float(r["close"]), "量密集", 3)

    # 1) 按现价强制分类 + 过滤太近的
    sep = last_close * 0.003  # 0.3% 内忽略
    sup_cand = [(p, l, s) for p, l, s in candidates if p < last_close - sep]
    res_cand = [(p, l, s) for p, l, s in candidates if p > last_close + sep]

    # 2) 聚类合并：价格差 <1.5% 的归一组，取强度最高 + 距现价最近的代表
    def cluster(items):
        items = sorted(items, key=lambda x: x[0])
        groups = []
        for p, l, s in items:
            if groups and abs(p - groups[-1][0]) / groups[-1][0] < 0.015:
                gp, gl, gs = groups[-1]
                new_s = max(s, gs)
                rep_p = p if abs(p - last_close) < abs(gp - last_close) else gp
                groups[-1] = (rep_p, gl if gs >= s else l, new_s)
            else:
                groups.append((p, l, s))
        return groups

    sup_cand = cluster(sup_cand)
    res_cand = cluster(res_cand)

    # 3) 强度 = 原始强度 + 距现价近的加成（近的更有参考价值）
    def score(item):
        p, l, s = item
        dist_pct = abs(p - last_close) / last_close * 100
        near_bonus = max(0, 3 - dist_pct * 0.15)  # 距现价越近加分越多
        return s * 0.6 + near_bonus

    sup_cand.sort(key=score, reverse=True)
    res_cand.sort(key=score, reverse=True)

    def fmt(items):
        # 每方向只保留 score 最高的 1 个（最重要）
        if not items:
            return []
        best = max(items, key=lambda it: score(it))
        p, l, s = best
        return [{"price": p, "label": l, "strength": s}]

    return {"supports": fmt(sup_cand), "resistances": fmt(res_cand)}


def _calc_fibonacci(daily, period="daily"):
    """黄金分割：自动锚定该周期**最显著的一段摆动** → 回撤位/扩展位。

    锚点选择：回看 N 根（见 _FIB_LOOKBACK，daily=120 与前端默认视窗对齐）
    → 分形找摆动点（复用 analysis.divergence._local_extrema）
    → 合成"高-低"交替序列 → 滤掉幅度不足的噪声摆动 → 取幅度最大的一段。
    全部不达标时降级为回看区间的最高/最低价（fallback=True）。

    比例位公式（L=摆动低点, H=摆动高点, rng=H-L）：
      上涨（低在前）：回撤 H-rng*ratio，扩展 L+rng*ext
      下跌（高在前）：回撤 L+rng*ratio，扩展 H-rng*ext
    side 按现价分：低于现价=支撑，高于现价=阻力。
    """
    from analysis.divergence import _local_extrema

    n_bars = _FIB_FRACTAL_N.get(period, 3)
    lookback = _FIB_LOOKBACK.get(period, 250)
    min_amp = _FIB_MIN_AMP.get(period, 5.0)
    if daily is None or len(daily) < 2 * n_bars + 5:
        return {"available": False, "reason": "样本不足"}

    d = daily.tail(lookback).reset_index(drop=True)
    n = len(d)
    if n < 2 * n_bars + 5:
        return {"available": False, "reason": "样本不足"}
    # 锚点索引要相对**完整周期序列**（前端 period.dates 是全量），故补齐偏移
    off = len(daily) - n
    highs = d["high"].astype(float).values
    lows = d["low"].astype(float).values
    # 分钟周期的锚点日期要带时间，否则与日线格式混同、图上标不出取自哪根
    _dfmt = "%Y-%m-%d %H:%M" if period in _MIN_PERIODS else "%Y-%m-%d"
    dates = [x.strftime(_dfmt) for x in d["date"]]

    peaks, troughs = _local_extrema(highs, lows, n_bars)

    # 1) 合成交替序列：同类型相邻只保留更极端者（否则两者属同一段走势，不构成摆动）
    piv = sorted([(i, "H") for i in peaks] + [(i, "L") for i in troughs])
    seq = []
    for idx, kind in piv:
        px = float(highs[idx]) if kind == "H" else float(lows[idx])
        if seq and seq[-1][1] == kind:
            if (px > seq[-1][2]) if kind == "H" else (px < seq[-1][2]):
                seq[-1] = (idx, kind, px)
            continue
        seq.append((idx, kind, px))

    def amp(lo_pt, hi_pt):
        base = lo_pt[2]
        return (hi_pt[2] - lo_pt[2]) / base * 100 if base else 0.0

    # 2) 相邻两点对 → 规范成 (低点, 高点) + 方向；3) 过滤幅度不足的噪声摆动
    cands = []
    for a, b in zip(seq, seq[1:]):
        lo_pt, hi_pt = (a, b) if a[1] == "L" else (b, a)
        if hi_pt[2] <= lo_pt[2]:
            continue
        cands.append((lo_pt, hi_pt, "up" if a[1] == "L" else "down", amp(lo_pt, hi_pt)))

    fallback = False
    ok = [c for c in cands if c[3] >= min_amp]
    if ok:
        # 幅度最大者；幅度接近(差<2%)时取更近的一段
        best = max(c[3] for c in ok)
        lo_pt, hi_pt, direction, amplitude = max(
            [c for c in ok if c[3] >= best * 0.98], key=lambda c: c[1][0])
    elif cands:
        lo_pt, hi_pt, direction, amplitude = max(cands, key=lambda c: c[3])
        fallback = True
    else:
        # 无交替摆动点（单边行情）→ 降级为回看区间最高/最低
        hi_i, lo_i = int(d["high"].idxmax()), int(d["low"].idxmin())
        lo_pt, hi_pt = (lo_i, "L", float(lows[lo_i])), (hi_i, "H", float(highs[hi_i]))
        direction = "up" if lo_i < hi_i else "down"
        amplitude = amp(lo_pt, hi_pt)
        fallback = True

    L, H = lo_pt[2], hi_pt[2]
    rng = H - L
    if rng <= 0:
        return {"available": False, "reason": "摆动幅度为零"}

    cur = float(d["close"].iloc[-1])
    levels = []

    def add(ratio, price, kind):
        price = round(float(price), 3)
        if price <= 0:
            return  # 大幅下跌摆动时扩展位可能算到 0 以下，无意义 → 丢弃
        levels.append({
            "ratio": ratio, "price": price, "kind": kind,
            "label": f"{ratio * 100:.1f}%".replace(".0%", "%"),
            "side": "support" if price < cur else "resistance",
            "golden": abs(ratio - 0.618) < 1e-9,
        })

    up = direction == "up"
    for rt in _FIB_RETRACE:
        add(rt, (H - rng * rt) if up else (L + rng * rt), "retracement")
    for ex in _FIB_EXTENSION:
        add(ex, (L + rng * ex) if up else (H - rng * ex), "extension")

    return {
        "available": True,
        "direction": direction,
        "fallback": fallback,
        "swing": {
            "low": {"price": round(L, 3), "index": int(lo_pt[0]) + off, "date": dates[lo_pt[0]]},
            "high": {"price": round(H, 3), "index": int(hi_pt[0]) + off, "date": dates[hi_pt[0]]},
            "amplitude_pct": round(amplitude, 2),
            "bars": int(abs(hi_pt[0] - lo_pt[0])),
            "lookback": int(n),
        },
        "levels": levels,
    }
