# -*- coding: utf-8 -*-
import os, sys, json, random

os.chdir(r"E:\06_T")
sys.path.insert(0, r"E:\06_T")

os.environ['ENABLE_STRATEGY_PULLBACK_BUY'] = '1'
os.environ['ENABLE_STRATEGY_BOX_BOTTOM_BUY'] = '1'

import config, stock_pool, data_fetcher, signal_detector

random.seed(42)
pool = stock_pool.load_a_share_pool()
all_codes = list(pool.keys())
test_codes = random.sample(all_codes, 200)

counts = {}
total = 0
details = []
for code in test_codes:
    try:
        df = data_fetcher.fetch_data(code, '20260616', 'daily')
        if df is not None and len(df) >= 60:
            sig_type, reason = signal_detector.check_strategies(df, enable_momentum_strategies=True)
            total += 1
            if sig_type:
                counts[sig_type] = counts.get(sig_type, 0) + 1
                details.append(f"{code}: {sig_type}")
            else:
                counts['no_signal'] = counts.get('no_signal', 0) + 1
    except Exception as e:
        counts['error'] = counts.get('error', 0) + 1

result = {
    "total": total,
    "counts": counts,
    "details": details,
    "summary": {k: f"{v} ({v/total*100:.1f}%)" for k, v in counts.items() if total}
}

with open(r"E:\06_T\test_result_200.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"Done. Sampled {total} stocks, signals written to E:\\06_T\\test_result_200.json")
