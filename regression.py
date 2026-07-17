# -*- coding: utf-8 -*-
"""
验证与回归模块
"""
import os
import time
from datetime import datetime

import pandas as pd

from config import log, DAILY_AMOUNT_MIN, QT_SNAPSHOT_ENABLED

from data_fetcher import fetch_data, ensure_amount_column, pick_target_row, compare_sample_sources, log_amount_check
from signal_detector import check_strategies

def validate_trading_date(date_str: str) -> bool:
    try:
        dt = datetime.strptime(date_str, "%Y%m%d")
        if dt.weekday() >= 5:
            return False
        holidays = ["20250101", "20250102", "20250103", "20250429", "20250430", "20250501", "20250502", "20250503", "20250818"]
        if date_str in holidays:
            return False
        return True
    except:
        return False

def run_amount_regression():
    # 如果启用了QT快照，跳过回归测试
    # 原因：QT快照只在当天有效，历史日期无法获取QT快照
    # 回归测试原本是为了验证volume*close的填充逻辑，现已移除
    if QT_SNAPSHOT_ENABLED:
        log.info("✓ 已启用QT快照模式，成交额数据来自QT快照，跳过回归测试")
        return True

    mode = os.getenv("AMOUNT_REGRESSION_MODE", "unit").strip().lower()
    unit_samples = [
        {"code": "688112", "name": "鼎阳科技", "date": "20260408", "expect_signal": True},
        {"code": "688337", "name": "普源精电", "date": "20260408", "expect_signal": True},
        {"code": "688455", "name": "科捷智能", "date": "20260408", "expect_signal": True},
        {"code": "688628", "name": "优利德", "date": "20260408", "expect_signal": True},
        {"code": "688693", "name": "锴威特", "date": "20260408", "expect_signal": True},
        {"code": "688702", "name": "盛科通信-U", "date": "20260408", "expect_signal": True},
        {"code": "688001", "name": "华兴源创", "date": "20260408", "expect_signal": True},
        {"code": "688700", "name": "东威科技", "date": "20260408", "expect_signal": True},
        {"code": "688127", "name": "蓝特光学", "date": "20260408", "expect_signal": True},
        {"code": "002460", "name": "赣锋锂业", "date": "20260408", "expect_signal": True},
        {"code": "600105", "name": "永鼎股份", "date": "20260408", "expect_signal": True},
        {"code": "603778", "name": "国晟科技", "date": "20260408", "expect_signal": True},
        {"code": "002222", "name": "福晶科技", "date": "20260408", "expect_signal": True},
    ]
    threshold_samples = [
        {"code": "300308", "name": "中际旭创", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": True, "expect_signal": True},
        {"code": "300502", "name": "新易盛", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": True, "expect_signal": True},
        {"code": "688702", "name": "盛科通信-U", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": False, "expect_signal": True},
        {"code": "688700", "name": "东威科技", "date": "20260408", "threshold": DAILY_AMOUNT_MIN, "expect_pass": False, "expect_signal": True},
    ]
    samples = unit_samples if mode == "unit" else threshold_samples
    print(f"\n🧪 成交额回归测试开始 [{mode}]\n")
    failed = []
    lines = []
    regression_deadline = time.time() + 180
    for sample in samples:
        if time.time() > regression_deadline:
            print("\n⚠️ 回归超时，提前结束，避免阻塞启动")
            failed.append({"code": "timeout", "reason": "regression_deadline_exceeded", "raw": 0, "normalized": 0, "threshold": DAILY_AMOUNT_MIN})
            break
        code = sample["code"]
        name = sample["name"]
        target_date = sample["date"]
        threshold = sample.get("threshold", DAILY_AMOUNT_MIN)
        df = fetch_data(code, target_date, scan_mode="daily")
        if df.empty:
            msg = f"❌ {code} {name} | reason=data_empty | date={target_date}"
            print(msg)
            lines.append(msg)
            failed.append({"code": code, "reason": "data_empty"})
            continue
        df = ensure_amount_column(df, code=code)
        target_row, _ = pick_target_row(df, target_date, code=code)
        if target_row.empty:
            msg = f"❌ {code} {name} | reason=target_row_missing | date={target_date}"
            print(msg)
            lines.append(msg)
            failed.append({"code": code, "reason": "target_row_missing"})
            continue
        raw_amount = float(target_row.iloc[-1].get("amount_raw", 0) or 0)
        normalized_amount = float(target_row.iloc[-1].get("amount", 0) or 0)
        unit_ratio = normalized_amount / raw_amount if raw_amount > 0 else 0.0
        log_amount_check(code, name, raw_amount, normalized_amount, threshold, float('inf'), scan_mode="daily")
        sig_type, reason = check_strategies(df, enable_momentum_strategies=True)
        passed = normalized_amount >= threshold
        pass_reason = "threshold_ok" if passed else "threshold_fail"
        if mode == "unit":
            threshold_ok = True
            unit_ok = raw_amount == 0 or (0.8 <= unit_ratio <= 1.2)
        else:
            threshold_ok = passed == sample["expect_pass"]
            unit_ok = raw_amount == 0 or (0.8 <= unit_ratio <= 1.2)
        signal_ok = bool(sig_type) == sample["expect_signal"]
        signal_reason = "signal_ok" if signal_ok else f"signal_mismatch:{sig_type or 'None'}"
        line = (
            f"{code} {name} | raw={raw_amount/100000000:.2f}亿 | normalized={normalized_amount/100000000:.2f}亿 | "
            f"ratio={unit_ratio:.2f}x | threshold={threshold/100000000:.2f}亿 | pass={passed} | expected_pass={sample.get('expect_pass', 'N/A')} | signal={sig_type or 'None'}"
        )
        if not unit_ok:
            failed.append({"code": code, "reason": f"unit_mismatch:{unit_ratio:.2f}x", "raw": raw_amount, "normalized": normalized_amount, "threshold": threshold})
        print(line)
        lines.append(line)
        sources = compare_sample_sources(code, target_date, df)
        if sources:
            lines.append(f"COMPARE {code} sources={len(sources)}")
            for snap in sources:
                src = snap.get("source", "unknown")
                src_raw = float(snap.get("raw_amount", 0) or 0)
                src_norm = float(snap.get("normalized_amount", 0) or 0)
                src_derived = float(snap.get("derived_amount", 0) or 0)
                src_volume = float(snap.get("volume", 0) or 0)
                src_close = float(snap.get("close", 0) or 0)
                src_ratio = (src_norm / raw_amount) if raw_amount > 0 else 0.0
                src_line = (
                    f"  - {src} | date={snap.get('date', '')} | close={src_close:.2f} | volume={src_volume:.0f} | "
                    f"raw={src_raw/100000000:.2f}亿 | normalized={src_norm/100000000:.2f}亿 | derived={src_derived/100000000:.2f}亿 | ratio_vs_main={src_ratio:.2f}x"
                )
                print(src_line)
                lines.append(src_line)
        if not threshold_ok:
            failed.append({"code": code, "reason": pass_reason, "raw": raw_amount, "normalized": normalized_amount, "threshold": threshold})
        if not signal_ok:
            failed.append({"code": code, "reason": signal_reason, "raw": raw_amount, "normalized": normalized_amount, "threshold": threshold})
    synthetic = pd.DataFrame([
        {"date": "2026-04-08", "open": 10.0, "close": 10.5, "high": 10.6, "low": 9.8, "volume": 1000000, "amount": 10500000},
        {"date": "2026-04-09", "open": 10.5, "close": 10.7, "high": 10.8, "low": 10.4, "volume": 1200000, "amount": 0},
    ])
    synthetic = ensure_amount_column(synthetic, code="000001")
    syn_raw = float(synthetic.iloc[-1].get("amount_raw", 0) or 0)
    syn_norm = float(synthetic.iloc[-1].get("amount", 0) or 0)
    syn_pass = syn_norm >= DAILY_AMOUNT_MIN
    synthetic_line = f"SYNTHETIC | raw={syn_raw/100000000:.2f}亿 | normalized={syn_norm/100000000:.2f}亿 | threshold={DAILY_AMOUNT_MIN/100000000:.2f}亿 | pass={syn_pass}"
    print(synthetic_line)
    lines.append(synthetic_line)
    if syn_norm <= 0:
        failed.append({"code": "synthetic", "reason": "amount_fill_failed", "raw": syn_raw, "normalized": syn_norm, "threshold": DAILY_AMOUNT_MIN})

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_path = os.path.join(LOG_DIR, f"amount_regression_{stamp}.log")
    try:
        with open(result_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")
        print(f"\n📝 回归结果已写入: {result_path}")
    except Exception as e:
        print(f"\n⚠️ 回归结果写入失败: {e}")
    if failed:
        print("\n❌ 回归未通过，失败明细：")
        for item in failed:
            code = item.get("code", "unknown")
            reason = item.get("reason", "unknown")
            raw_amount = float(item.get("raw", 0) or 0)
            normalized_amount = float(item.get("normalized", 0) or 0)
            threshold = float(item.get("threshold", 0) or 0)
            print(
                f"  - {code} | reason={reason} | raw={raw_amount/100000000:.2f}亿 | "
                f"normalized={normalized_amount/100000000:.2f}亿 | threshold={threshold/100000000:.2f}亿"
            )
        print(f"📝 失败日志位置: {result_path}")
        return False
    print("\n✅ 成交额回归通过")
    return True
