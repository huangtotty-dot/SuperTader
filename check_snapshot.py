import json

for code in ['600481', '588170']:
    try:
        with open(f't_io/minute_snapshots/2026/08/{code}_2026-08-24.json', 'r', encoding='utf-8') as f:
            d = json.load(f)
        print(f"{code} keys: {list(d.keys())}")
        for k, v in d.items():
            if k != 'data':
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: {type(v)} len={len(v) if hasattr(v, '__len__') else 'N/A'}")
                if isinstance(v, list) and v:
                    print(f"    第一条: {v[0]}")
                elif isinstance(v, dict):
                    print(f"    keys: {list(v.keys())[:5]}")
    except Exception as e:
        print(f"{code} error: {e}")
    print()
