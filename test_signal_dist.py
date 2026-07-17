# -*- coding: utf-8 -*-
import os, sys, json

os.chdir(r"E:\06_T")
sys.path.insert(0, r"E:\06_T")

os.environ['ENABLE_STRATEGY_PULLBACK_BUY'] = '1'
os.environ['ENABLE_STRATEGY_BOX_BOTTOM_BUY'] = '1'

import config, stock_pool, data_fetcher, signal_detector

pool = stock_pool.load_a_share_pool()
test_codes = [
    '000001','000009','002371','600519','300750','002230','300059','000858','002594','000002',
    '300498','002475','300760','002415','600276','000651','002714','300274','600036','000568',
    '002142','000333','600887','002352','600009','002007','600585','600031','002049','300122',
    '002460','300014','600703','000963','002271','600690','300408','002032','600872','300433',
    '002821','002601','000895','002038','300601','600132','000860','002507','300296'
]

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

with open(r"E:\06_T\test_result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print("Done. Written to E:\\06_T\\test_result.json")
