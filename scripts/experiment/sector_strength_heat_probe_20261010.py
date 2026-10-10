"""验证 heat_tracker 新增「龙头集中度」分项：权重合计、映射、真实调用、排序影响。

用法：PYTHONIOENCODING=utf-8 python tmp/probe_heat_lead.py
"""

# --- 仓库根自解析（入库规范：勿硬编码本机路径）---
import os as _os
BASE = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, BASE)
sys.path.insert(0, _os.path.join(BASE, "stock_hunter"))

import modules.heat_tracker as ht  # noqa: E402

# ⚠️ 隔离：`compute_heat_scores` 会**无条件**把当日热度写进真实历史文件
# （`_save_sector_history` → `save_daily_summary` → `history/daily_summary.json`）。
# 本探针用过日期 "2099-01-01/02"，曾把 `X_龙头强其余平` 之类的假板块写进**生产数据**。
# 故先把历史路径改到 tmp/ 下的沙箱，绝不碰生产文件。
_SANDBOX = Path(__file__).with_name("_heat_probe_history.json")
ht._history_path = lambda: str(_SANDBOX)

ok = True


def check(c, m):
    global ok
    print(("  PASS  " if c else "  FAIL  ") + m)
    if not c:
        ok = False


print("=== 1) 权重合计（上限由 100 降为 80）===")
tot = (ht._W_LIMIT_DENSITY + ht._W_UP_BREADTH + ht._W_STRONG_BREADTH
       + ht._W_FRONT + ht._W_LEAD)
print(f"  正项 {ht._W_LIMIT_DENSITY:.0f}+{ht._W_UP_BREADTH:.0f}+{ht._W_STRONG_BREADTH:.0f}"
      f"+{ht._W_FRONT:.0f}+{ht._W_LEAD:.0f} = {tot:.0f}（量能放大改为 −{ht._W_AMP_PENALTY:.0f}~0 扣分）")
check(tot == 80.0, "正项合计 = 80（上限已由 100 降为 80）")

print("\n=== 1b) 量能放大符号已翻转 ===")
def amp_score(amp):
    return -min(max(amp - ht._AMP_PENALTY_FROM, 0.0) * 10.0, ht._W_AMP_PENALTY)
for amp, want in ((0.8, 0.0), (1.5, 0.0), (2.0, -5.0), (2.5, -10.0), (5.0, -10.0)):
    got = amp_score(amp)
    check(abs(got - want) < 1e-9, f"amp={amp}x → {got:.1f} 分（期望 {want}）")
check(amp_score(2.0) < amp_score(1.0), "高放量 < 低放量（符号已翻转为**扣分**）")

print("\n=== 2) 龙头集中度映射（按实测分位设的标）===")
for pct, want in ((2.8, 2.2), (5.9, 4.7), (11.0, 8.8)):
    got = min(max(pct * ht._LEAD_K, 0.0), ht._W_LEAD)
    print(f"  lead={pct:5.1f}% → {got:4.1f} 分（目标≈{want}）")
    check(abs(got - want) <= 0.15, f"lead={pct}% 映射合理")
check(min(max(-5.0 * ht._LEAD_K, 0.0), ht._W_LEAD) == 0.0, "负 lead 归零（lead 恒正，防御性）")
check(min(max(99.0 * ht._LEAD_K, 0.0), ht._W_LEAD) == ht._W_LEAD, "极大 lead 封顶 10")


def mk_sector(rets, limits=None, amounts=None):
    n = len(rets)
    limits = limits or [0] * n
    amounts = amounts or [1e8] * n
    return pd.DataFrame({
        "代码": [f"{600000 + i}" for i in range(n)],
        "涨跌幅": rets, "涨停": limits, "成交额": amounts,
        "D1强势形态且新高": [0] * n, "D2强势形态": [0] * n,
    })


print("\n=== 3) 真实调用 compute_heat_scores（含新分项，不得崩）===")
# A: 龙头强、其余弱（集中度高）   B: 全体同涨（集中度低）
secs = {
    "A_龙头驱动": mk_sector([10.0, 9.8, 9.5] + [0.5] * 17),
    "B_普涨": mk_sector([2.0] * 20),
    "C_全跌": mk_sector([-3.0] * 20),
}
summary = pd.DataFrame({"板块": list(secs), "股票数量": [20, 20, 20]})
out = ht.compute_heat_scores(summary, secs, "2099-01-01")
print(out[["板块", "热度分", "龙头集中度分", "上涨广度分", "强涨广度分", "涨停密度分"]]
      .to_string(index=False))
check("龙头集中度分" in out.columns, "输出含「龙头集中度分」列")
a = out.set_index("板块")
check(a.loc["A_龙头驱动", "龙头集中度分"] > a.loc["B_普涨", "龙头集中度分"],
      "龙头驱动板块的集中度分 > 普涨板块")
check(a.loc["B_普涨", "龙头集中度分"] == 0.0, "等涨幅普涨 → 集中度 0（Top3==其余）")

print("\n=== 4) 与旧口径（上涨广度 20 分、无集中度）的排序差异 ===")


def old_score(rets, limits):
    n = len(rets)
    up = sum(1 for r in rets if r > 0) / n
    strong = sum(1 for r in rets if r > 3) / n
    lim = sum(limits) / n
    return min(lim * 150, 30) + up * 20 + strong * 20
    # 注：只比「受本次改动影响的两项」，量能/前排不变故略去（不影响相对排序）


secs2 = {
    "X_龙头强其余平": mk_sector([10.0, 9.5, 9.0] + [0.0] * 17),
    "Y_普涨无龙头": mk_sector([3.0] * 20),
}
s2 = pd.DataFrame({"板块": list(secs2), "股票数量": [20, 20]})
o2 = ht.compute_heat_scores(s2, secs2, "2099-01-02")
newv = dict(zip(o2["板块"], o2["热度分"] - o2["量能放大分"] - o2["前排强度分"]))
oldv = {k: old_score(v["涨跌幅"].tolist(), v["涨停"].tolist()) for k, v in secs2.items()}
print(f"  旧口径（受影响两项）: {oldv}")
print(f"  新口径（受影响三项）: { {k: round(v,1) for k,v in newv.items()} }")
print(f"  旧排序: {sorted(oldv, key=lambda k: -oldv[k])}")
print(f"  新排序: {sorted(newv, key=lambda k: -newv[k])}")
check(sorted(oldv, key=lambda k: -oldv[k]) != sorted(newv, key=lambda k: -newv[k]),
      "新旧口径确实给出不同排序（证明改动可观察）")

print("\n判定:", "PASS" if ok else "FAIL")
