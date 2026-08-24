import json
from datetime import datetime

with open('t_io/state/holdings_daily_2026-08-24.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

# 截图精确数据
live = {
    "515180": {
        "name": "红利ETF易方达", "account": "账户A", "type": "etf",
        "qty": 14000, "base": 14000, "t_qty": 0,
        "cost": 1.430, "pre_close": 1.433, "price": 1.453,
        "change_pct": 1.40, "pnl_pct": 1.557, "pnl_amt": 317.0, "offline": False
    },
    "002639": {
        "name": "雪人集团", "account": "账户A", "type": "stock",
        "qty": 100, "base": 100, "t_qty": 0,
        "cost": 10.807, "pre_close": 11.59, "price": 11.480,
        "change_pct": -0.95, "pnl_pct": 6.229, "pnl_amt": 67.32, "offline": False
    },
    "588170": {
        "name": "科创半导体ETF华夏", "account": "账户B", "type": "etf",
        "qty": 1500, "base": 1000, "t_qty": 500,
        "cost": 0.478, "pre_close": 1.012, "price": 1.016,
        "change_pct": 0.40, "pnl_pct": 112.552, "pnl_amt": 806.95, "offline": False
    },
    "600481": {
        "name": "双良节能", "account": "账户B", "type": "stock",
        "qty": 100, "base": 100, "t_qty": 0,
        "cost": 28.216, "pre_close": 4.21, "price": 4.190,
        "change_pct": -0.48, "pnl_pct": -85.150, "pnl_amt": -2402.62, "offline": False
    }
}

# 已清仓
cleared = {"600176", "603667", "000988", "000988_B"}

new_holdings = []
seen = set()

total_value = 0.0
total_cost = 0.0
total_pnl = 0.0

for h in data.get("holdings", []):
    code = h["code"]
    if code in seen:
        continue
    seen.add(code)

    if code in live:
        # 完全替换为截图精确数据
        new_h = {"code": code, **live[code]}
        new_holdings.append(new_h)
        total_value += new_h["price"] * new_h["qty"]
        if new_h["cost"] is not None:
            total_cost += new_h["cost"] * new_h["qty"]
        if new_h["pnl_amt"] is not None:
            total_pnl += new_h["pnl_amt"]
    elif code in cleared:
        h["qty"] = 0
        h["base"] = 0
        h["t_qty"] = 0
        h["pnl_amt"] = None
        h["offline"] = True
        new_holdings.append(h)
    else:
        new_holdings.append(h)

# 如果 000988_B 不在原数组中，手动追加
if "000988_B" not in seen:
    new_holdings.append({
        "code": "000988_B", "name": "华工科技", "account": "账户B",
        "type": "stock", "qty": 0, "base": 0, "t_qty": 0,
        "cost": None, "pre_close": None, "price": None,
        "change_pct": None, "pnl_pct": None, "pnl_amt": None, "offline": True
    })

data["holdings"] = new_holdings
data["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
data["summary"] = {
    "total_value": round(total_value, 1),
    "total_cost": round(total_cost, 1),
    "total_pnl": round(total_pnl, 2),
    "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0.0
}

with open('t_io/state/holdings_daily_2026-08-24.json', 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print("Fixed holdings_daily_2026-08-24.json")
print(f"Summary: {data['summary']}")
