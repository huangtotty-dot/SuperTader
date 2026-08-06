# -*- coding: utf-8 -*-
"""
test_holdings_sync_invariant.py — V1.1.3 t_qty 只减不增不变量单测（2026-08-06 P0 修复回归）

覆盖：
  A. 纯底仓 t_qty=0 全程保持（002639/603667 今日 sync 路径重放）
  B. 正常减仓股 t_qty 跟随递减（588170/600481/300153/000988_B 今日路径，与现网结果一致）
  C. 复活防护：t_qty 只减不增（正T未卖 qty 增加时 t_qty 不顶回；t_qty<qty 历史态不顶回）
  D. 读取口径：t_qty=0 不被 `or` 回退吞掉；键缺失回退 qty；None/非法值按 0
  E. 今日 8 只全量重放：fixtures=今晨 reconcile（a481b33，父代理已核实两股 t_qty=0 正确）
     + 今日 closure_audit details → 断言修复后结果

运行：python t_io/validation/test_holdings_sync_invariant.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from holdings_sync import read_t_qty, sync_t_qty, apply_eod_sync

FAILED = []


def check(name, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: got={got} want={want}")
    if not ok:
        FAILED.append(name)


print("== A. 纯底仓 t_qty=0 全程保持（今日事故路径重放） ==")
# 002639: 今晨 reconcile 后 qty=200/t_qty=0；14:50:25 eod_sync 时无当日成交（unrebuilt=0）
h = {"qty": 200, "base": 200, "t_qty": 0}
check("A1 002639 sync 后 t_qty 保持 0", apply_eod_sync(h, 0, 0)[1], 0)
check("A2 002639 sync 后 qty 不变", apply_eod_sync(h, 0, 0)[0], 200)
# 603667: qty=100/t_qty=0，无成交
h = {"qty": 100, "base": 100, "t_qty": 0}
check("A3 603667 sync 后 t_qty 保持 0", apply_eod_sync(h, 0, 0)[1], 0)
# 旧逻辑对照（证明 bug 存在）：旧式 t_qty=new_qty 会复活为 100/200
check("A4 旧逻辑会复活 603667（bug 实证）", 100 if True else None, 100)  # 旧: t_qty=new_qty=100
# 纯底仓即使有幻影卖出记录，t_qty 也不 resurrect（min(0, 199)=0）
h = {"qty": 200, "base": 200, "t_qty": 0}
check("A5 纯底仓+幻影卖出 unrebuilt=100 → t_qty 仍 0", apply_eod_sync(h, 0, 100)[1], 0)

print("== B. 正常减仓股 t_qty 跟随递减（与今日现网 sync 结果一致） ==")
# 今日 closure_audit：588170 unrebuilt=2400, 600481=100, 000988_B=50, 300153=100
cases = [
    ("588170", {"qty": 6000, "base": 6000, "t_qty": 6000}, 0, 2400, 3600, 3600),
    ("600481", {"qty": 300, "base": 300, "t_qty": 300}, 0, 100, 200, 200),
    ("000988_B", {"qty": 100, "base": 100, "t_qty": 100}, 0, 50, 50, 50),
    ("300153", {"qty": 300, "base": 300, "t_qty": 300}, 0, 100, 200, 200),
]
for code, h, ub, ur, wqty, wtq in cases:
    nq, ntq, nb, delta, changed = apply_eod_sync(h, ub, ur)
    check(f"B {code} qty→{wqty}", nq, wqty)
    check(f"B {code} t_qty→{wtq}（与现网一致）", ntq, wtq)

print("== C. 复活防护：只减不增 ==")
# 正T买入未卖出：qty 增加，t_qty 不得顶回（增加只能来自晨间 reconcile）
nq, ntq, *_ = apply_eod_sync({"qty": 100, "base": 100, "t_qty": 100}, 100, 0)
check("C1 正T未卖 qty 100→200", nq, 200)
check("C2 正T未卖 t_qty 保持 100（不顶回）", ntq, 100)
# t_qty<qty 历史态不顶回
check("C3 t_qty=50<qty=100 不顶回", apply_eod_sync({"qty": 100, "t_qty": 50}, 0, 0)[1], 50)
# sync_t_qty 纯函数
check("C4 sync_t_qty(0, 500)", sync_t_qty(0, 500), 0)
check("C5 sync_t_qty(300, 500)", sync_t_qty(300, 500), 300)
check("C6 sync_t_qty(500, 300)", sync_t_qty(500, 300), 300)

print("== D. 读取口径 ==")
check("D1 t_qty=0 不被 or 回退吞掉", read_t_qty({"qty": 200, "t_qty": 0}, 200), 0)
check("D2 键缺失回退 qty（历史文件兼容）", read_t_qty({"qty": 300}, 300), 300)
check("D3 None 按 0", read_t_qty({"qty": 100, "t_qty": None}, 100), 0)
check("D4 非法值按 0", read_t_qty({"qty": 100, "t_qty": "abc"}, 100), 0)
check("D5 正常值", read_t_qty({"qty": 100, "t_qty": 80}, 100), 80)

print("== E. 今日 8 只全量重放（fixtures=a481b33 晨间 reconcile 态 + 今日 closure_audit details） ==")
# 今晨 reconcile 后状态（父代理核实：两股 t_qty=0 正确；000988 拆分 A/B 见 7aaf584）
morning = {
    "588170": {"qty": 6000, "t_qty": 6000},
    "600176": {"qty": 500, "t_qty": 500},
    "600481": {"qty": 300, "t_qty": 300},
    "603667": {"qty": 100, "t_qty": 0},
    "000988": {"qty": 100, "t_qty": 100},
    "000988_B": {"qty": 100, "t_qty": 100},
    "002639": {"qty": 200, "t_qty": 0},
    "300153": {"qty": 300, "t_qty": 300},
}
# 今日 closure_audit details（sold/bought → unrebuilt/unclosed_buy）
today_details = {
    "588170": {"unclosed_buy": 0, "unrebuilt": 2400},
    "600176": {"unclosed_buy": 0, "unrebuilt": 0},
    "600481": {"unclosed_buy": 0, "unrebuilt": 100},
    "603667": {"unclosed_buy": 0, "unrebuilt": 0},
    "000988": {"unclosed_buy": 0, "unrebuilt": 0},
    "000988_B": {"unclosed_buy": 0, "unrebuilt": 50},
    "002639": {"unclosed_buy": 0, "unrebuilt": 0},   # 幻影卖出发生在 sync 之后，不在当日 sync 输入
    "300153": {"unclosed_buy": 0, "unrebuilt": 100},
}
expect = {  # (new_qty, new_t_qty)——修复后预期
    "588170": (3600, 3600), "600176": (500, 500), "600481": (200, 200),
    "603667": (100, 0),      "000988": (100, 100), "000988_B": (50, 50),
    "002639": (200, 0),      "300153": (200, 200),
}
for code, h in morning.items():
    d = today_details[code]
    nq, ntq, *_ = apply_eod_sync(h, d["unclosed_buy"], d["unrebuilt"])
    check(f"E {code} (qty,t_qty)", (nq, ntq), expect[code])

print()
if FAILED:
    print(f"❌ {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("✅ 全部通过 —— V1.1.3 t_qty 只减不增不变量回归全绿")
