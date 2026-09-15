# -*- coding: utf-8 -*-
"""
test_buyback_cap.py — P0-1 回补量封顶纯函数 _buyback_cap_qty 离线仿真验证
==========================================================================
2026-09-15 阶段0-7（诊断D3/D4）：gm_main.py:79-96 `_buyback_cap_qty` 上线后 0 笔实证，
本脚本离线覆盖边界：正常量 / 超量钳制 / 零 / 负数 / 非整百 / armed 缺失 / 非法 sell_qty /
audit_fn 异常，防 09-11 588170 armed 1100 实买 21000≈19.1× 失控复发。

实现说明：gm_main.py 顶层 import gm SDK（仅用户 Python 有），managed python 下无法直接
import。本脚本用 ast 从源码提取 `_buyback_cap_qty` 函数节点单独编译执行（命名空间仅注入
datetime），不触发 gm_main 顶层副作用。函数逻辑若有改动，本测试自动跟随最新源码。

用法（managed python，离线，不碰 t_io/bridge|state|logs 生产文件）：
    python t_io/validation/buyback_cap/test_buyback_cap.py
产物：同目录 test_report.json（机读测试报告）
"""
import ast
import json
import os
import sys
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))  # <superTrader>/
GM_MAIN = os.path.join(ROOT, "execution", "auto", "gm_main.py")
REPORT_PATH = os.path.join(HERE, "test_report.json")
FUNC_NAME = "_buyback_cap_qty"


def extract_func():
    """ast 提取 gm_main._buyback_cap_qty（绕开 gm SDK 顶层 import）。返回 (函数对象, 源码行号)。"""
    with open(GM_MAIN, "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src, filename=GM_MAIN)
    node = next(n for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == FUNC_NAME)
    lineno = node.lineno
    mod = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    ns = {"datetime": datetime}  # 函数内仅 audit 记录用到 datetime.now()
    exec(compile(mod, GM_MAIN, "exec"), ns)
    return ns[FUNC_NAME], lineno


def run_tests():
    cap, src_lineno = extract_func()
    cases = []

    def audit_spy(store):
        def _fn(rec):
            store.append(rec)
        return _fn

    def check(name, got, expect, note="", extra=None):
        ok = got == expect
        cases.append({"case": name, "pass": ok, "got": got, "expect": expect,
                      "note": note, **(extra or {})})
        return ok

    # ① 正常量：qty ≤ cap，不钳制、不审计
    logs = []
    r = cap(500, {"sell_qty": 1100}, audit_fn=audit_spy(logs), code="588170")
    check("正常量不钳制", (r, len(logs)), (500, 0),
          "qty=500 ≤ armed sell_qty=1100，原样返回且无审计事件")

    # ② 超量钳制（09-11 事故场景：armed 1100 / sizer 出 21000≈19.1×）
    logs = []
    r = cap(21000, {"sell_qty": 1100}, audit_fn=audit_spy(logs), code="588170")
    ev_ok = (len(logs) == 1 and logs[0].get("event") == "buyback_capped"
             and logs[0].get("qty_before") == 21000 and logs[0].get("qty_after") == 1100
             and logs[0].get("sell_qty") == 1100 and logs[0].get("code") == "588170")
    check("超量钳制到armed量", (r, ev_ok), (1100, True),
          "qty=21000 > cap=1100 → 钳到 1100 并写 buyback_capped 审计",
          extra={"audit": logs})

    # ③ 恰好等于帽：不钳制（条件是 int(qty) > cap，严格大于）
    logs = []
    r = cap(1100, {"sell_qty": 1100}, audit_fn=audit_spy(logs))
    check("等于帽不钳制", (r, len(logs)), (1100, 0), "qty==cap 边界，原样返回")

    # ④ qty=0：不动
    r = cap(0, {"sell_qty": 1100})
    check("零量原样返回", r, 0, "qty=0 < cap，不钳制")

    # ⑤ 负数 qty：函数无符号校验，负数 < cap 原样返回（语义交调用侧保证）
    r = cap(-100, {"sell_qty": 1100})
    check("负数原样返回", r, -100, "qty=-100：int(-100)>1100 不成立 → 原样返回（记录现状语义）")

    # ⑥ 非整百 qty（超帽）：钳到 cap（cap 本身为 armed 卖出量，整百由 armed 侧保证）
    logs = []
    r = cap(1550, {"sell_qty": 1100}, audit_fn=audit_spy(logs))
    check("非整百超帽钳制", (r, len(logs)), (1100, 1), "qty=1550 > 1100 → 1100")

    # ⑦ 非整百 qty（未超帽）：不凑整、原样返回（函数不管整百规则）
    r = cap(150, {"sell_qty": 1100})
    check("非整百未超帽原样", r, 150, "qty=150 ≤ 1100 → 原样，不凑整百")

    # ⑧ armed 缺失（None）：不钳制（回补无记忆 = 非回补场景，量由其他闸控）
    r = cap(21000, None)
    check("armed缺失(None)不钳制", r, 21000, "ab_now=None → 直接返回 qty")

    # ⑨ armed 为空 dict（无 sell_qty 键）：cap=int(None or 0)=0 <100 → 不钳制
    r = cap(21000, {})
    check("armed空dict不钳制", r, 21000, "sell_qty 缺失 → cap=0 < 100 门限，不钳制")

    # ⑩ sell_qty < 100 门限：不钳制（碎股/零帽场景不误伤正常单）
    r = cap(21000, {"sell_qty": 50})
    check("cap低于100不钳制", r, 21000, "sell_qty=50 < 100 门限 → 不钳制（记录现状语义）")

    # ⑪ sell_qty=0：同上门限逻辑
    r = cap(21000, {"sell_qty": 0})
    check("cap为0不钳制", r, 21000, "sell_qty=0 < 100 门限 → 不钳制")

    # ⑫ sell_qty 为数字字符串：int("1100") 可解析 → 正常钳制
    logs = []
    r = cap(21000, {"sell_qty": "1100"}, audit_fn=audit_spy(logs))
    check("字符串sell_qty可解析", (r, len(logs)), (1100, 1), 'sell_qty="1100" → int() 解析成功并钳制')

    # ⑬ sell_qty 非法值：int("abc") 抛异常 → except 兜底原样返回（fail-open）
    r = cap(21000, {"sell_qty": "abc"})
    check("非法sell_qty原样返回", r, 21000, 'sell_qty="abc" → int() 异常被吞，fail-open 返回 qty')

    # ⑭ audit_fn 自身抛异常：不影响返回 cap（审计失败不阻断交易闸）
    def bad_audit(rec):
        raise RuntimeError("audit boom")
    r = cap(21000, {"sell_qty": 1100}, audit_fn=bad_audit)
    check("audit异常仍钳制", r, 1100, "audit_fn 抛异常被内层 except 吞掉，返回 cap 不受影响")

    # ⑮ ab_now 为 truthy 非 dict（.get 抛异常）：except 兜底 fail-open
    r = cap(21000, object())
    check("armed非法类型fail-open", r, 21000, "ab_now=object() → .get 异常被吞，原样返回")

    # ⑯ audit_fn=None（默认）：超帽仍钳制，仅跳过审计
    r = cap(21000, {"sell_qty": 1100})
    check("无audit_fn仍钳制", r, 1100, "audit_fn 缺省 None → 钳制生效、无审计事件")

    n_pass = sum(1 for c in cases if c["pass"])
    report = {
        "test": "buyback_cap_qty 离线仿真验证（阶段0-7）",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "function": f"execution/auto/gm_main.py:{src_lineno} {FUNC_NAME}",
        "extract_method": "ast 提取函数节点单独编译（绕开 gm SDK 顶层 import）",
        "total": len(cases), "passed": n_pass, "failed": len(cases) - n_pass,
        "all_pass": n_pass == len(cases),
        "cases": cases,
        "semantics_notes": [
            "钳制条件: cap=int(ab_now['sell_qty']) >= 100 且 int(qty) > cap（严格大于，等于不钳）",
            "负数/非整百 qty 不做符号与凑整校验，原样返回（语义由调用侧保证）",
            "sell_qty < 100 视为无效帽不钳制（防碎股/零帽误伤）",
            "全程 fail-open：任何异常 → 原样返回 qty，绝不阻断交易",
        ],
    }
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    print(f"函数来源: gm_main.py:{src_lineno} {FUNC_NAME}")
    for c in cases:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}] {c['case']}: got={c['got']} expect={c['expect']} — {c['note']}")
    print(f"\n合计 {n_pass}/{len(cases)} 通过 → {'✅ ALL PASS' if report['all_pass'] else '❌ 有失败'}")
    print(f"报告: {REPORT_PATH}")
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    sys.exit(run_tests())
