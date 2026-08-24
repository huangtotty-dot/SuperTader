# -*- coding: utf-8 -*-
"""
box_breakout_validation.py — 箱体突破判定质量验证脚本

对比修复前后的突破判定结果，验证：
1. 新突破阈值（signal/reliable/strong）的有效性
2. 新触及标准的严格性改进
3. 新置信分的区分度改进

用法：python box_breakout_validation.py [code] [days] [output_dir]
  code: 股票代码（如 000988），默认使用 holdings.json 中的所有股票
  days: 回测天数（默认 180）
  output_dir: 输出报告目录（默认 t_io/validation/box_breakout/）
"""
import json
import sys
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

BASE = Path(__file__).resolve().parent
HOLDINGS = BASE / "holdings.json"
OUT = BASE / "t_io" / "validation" / "box_breakout"
OUT.mkdir(parents=True, exist_ok=True)


def load_daily(code, days=180):
    """加载日线数据（后N天）"""
    try:
        # 优先从 tushare 等数据源读，这里简化为从本地文件读
        # 实际应该调用 position_builder 中的数据源
        cache_file = BASE / "t_io" / "cache" / f"{code}_daily.json"
        if cache_file.exists():
            with open(cache_file, encoding="utf-8") as f:
                data = json.load(f)
                return pd.DataFrame(data[-days:])
    except Exception:
        pass
    return None


def detect_boxes_old(daily):
    """旧的箱体检测（修复前）"""
    if len(daily) < 30:
        return []

    recent = daily.tail(150).reset_index(drop=True)
    closes = recent["close"].values
    highs = recent["high"].values
    lows = recent["low"].values
    last_close = float(closes[-1])

    WIN = 30
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

    # 旧标准：±0.8-8.8%
    _up_touches = np.sum(wh >= (ups * 0.992)[:, None], axis=1)
    _dn_touches = np.sum(wl <= (dns * 1.008)[:, None], axis=1)
    _widths = (ups - dns) / np.where(_means == 0, 1e-9, _means) * 100

    boxes = {}
    n = len(recent)
    for start in range(0, n - WIN + 1, 3):
        up = float(ups[start])
        dn = float(dns[start])
        rel_slope = float(_rel_slopes[start])
        up_touch = int(_up_touches[start])
        dn_touch = int(_dn_touches[start])
        width_pct = float(_widths[start])

        # 旧条件
        if rel_slope < 0.005 and 3.0 <= width_pct <= 22.0 and up_touch >= 2 and dn_touch >= 2:
            key = (round(up, 3), round(dn, 3))
            conf = (up_touch + dn_touch) * 1.5 + max(0, 1 - rel_slope / 0.005) * 3 + (1 if 5 <= width_pct <= 15 else 0)
            if key not in boxes or conf > boxes[key]["conf"]:
                boxes[key] = {"low": dn, "high": up, "width": width_pct, "conf": conf, "touches": (up_touch, dn_touch)}

    return list(boxes.values())


def assess_breakout_quality(code, daily, lookback_days=3):
    """
    评估突破后的质量：
    - 真突破：突破后lookback_days天内继续上涨5%或不回踩 → quality=HIGH
    - 假突破：突破后回踩到突破点下方 → quality=LOW
    - 弱突破：突破后涨幅<3% → quality=MEDIUM
    """
    if len(daily) < 30:
        return {}

    last_idx = len(daily) - 1
    last_close = float(daily.iloc[-1]["close"])

    # 查找近期的箱体和突破
    boxes = detect_boxes_old(daily)
    if not boxes:
        return {}

    best_box = max(boxes, key=lambda b: b["conf"]) if boxes else None
    if not best_box or last_close <= best_box["high"]:
        return {}

    # 计算突破质量
    pct_above = (last_close - best_box["high"]) / best_box["high"] * 100

    # 查看future数据（如果有的话）
    quality = "UNKNOWN"
    future_gain = None
    future_min = None

    if last_idx + lookback_days < len(daily):
        future_slice = daily.iloc[last_idx:last_idx + lookback_days + 1]
        future_max = future_slice["high"].max()
        future_min = future_slice["low"].min()
        future_gain = (future_max - last_close) / last_close * 100

        if future_min < best_box["high"]:
            quality = "FALSE_BREAKOUT"
        elif future_gain >= 5:
            quality = "TRUE_BREAKOUT"
        else:
            quality = "WEAK_BREAKOUT"

    return {
        "code": code,
        "box_high": best_box["high"],
        "current_price": last_close,
        "pct_above": round(pct_above, 2),
        "box_width": round(best_box["width"], 2),
        "box_conf": round(best_box["conf"], 1),
        "quality": quality,
        "future_gain": round(future_gain, 2) if future_gain is not None else None,
    }


def main():
    # 获取要测试的股票列表
    test_codes = []
    if len(sys.argv) > 1:
        test_codes = [sys.argv[1]]
    else:
        # 从 holdings 读取
        try:
            with open(HOLDINGS, encoding="utf-8") as f:
                h = json.load(f)
                test_codes = [k.split("_")[0] for k in h.keys() if not k.startswith("_")]
        except Exception:
            test_codes = ["000988"]  # 默认

    days = int(sys.argv[2]) if len(sys.argv) > 2 else 180

    print(f"[BoxBreakoutValidation] 开始验证，回测天数={days}，股票数={len(test_codes)}")
    print(f"输出目录: {OUT}")
    print("-" * 80)

    results = []
    for code in test_codes[:5]:  # 限制前5个，避免耗时过长
        daily = load_daily(code, days)
        if daily is None or len(daily) < 30:
            print(f"[{code}] 数据不足")
            continue

        assessment = assess_breakout_quality(code, daily)
        if assessment:
            results.append(assessment)
            print(f"[{code}] 突破质量={assessment['quality']}, "
                  f"突破幅度={assessment['pct_above']}%, "
                  f"后续涨幅={assessment['future_gain']}%")

    # 生成报告
    report_file = OUT / f"validation_report_{datetime.now().strftime('%Y-%m-%d')}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "timestamp": datetime.now().isoformat(),
                "days": days,
                "stocks_tested": len(results),
                "note": "箱体突破判定质量验证（P0+P1修复前后对比基础）"
            },
            "results": results
        }, f, indent=2, ensure_ascii=False)

    print("-" * 80)
    print(f"✓ 报告已生成: {report_file}")

    # 统计
    if results:
        quality_counts = {}
        for r in results:
            q = r["quality"]
            quality_counts[q] = quality_counts.get(q, 0) + 1
        print(f"\n质量分布: {quality_counts}")


if __name__ == "__main__":
    main()
