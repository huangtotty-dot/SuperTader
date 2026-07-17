import sys, re

lines = sys.stdin.readlines()
filtered = [l for l in lines if re.search(r'10:39|13:1[46]|13:34|13:59|SELL_HIGH|BUY_LOW|final_sell_score|peak_confirmed|awaiting_buyback', l)]
print(''.join(filtered))
