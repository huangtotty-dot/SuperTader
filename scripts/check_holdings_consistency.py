# -*- coding: utf-8 -*-
"""scripts/check_holdings_consistency.py — 持仓单一真源一致性守卫（2026-08-30）

持仓信息合并成 t_io/state/holdings.json 单一真源后，手动链/自动链/回测都从它派生。
本脚本校验派生关系不漂移（用户手改 holdings.json 后跑一遍，漏改/孤岛立即暴露）：

  1) 全集一致：auto 池 ∪ 持有 == holdings.json 全量（无孤岛条目）
  2) mirror ⊆ auto：挂了目标底仓(mirror_qty>0)的码必须属于 auto 池
  3) pool 与 is_manual 语义一致
  4) auto 池每只都有非空 gm_symbol
  5) auto_pool.py 的 AUTO_POOL 与 holdings_repo.load_auto_pool 一致

用法：python scripts/check_holdings_consistency.py（退出码 0=通过，1=漂移）
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.holdings_repo import load_full, load_held, load_auto_pool, load_mirror_holdings

# config 是目录非 package，按绝对路径加载 auto_pool（与 goldminer/position_builder 同款）
_spec = importlib.util.spec_from_file_location(
    "auto_pool", os.path.join(_ROOT, "config", "auto_pool.py"))
_ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ap)


def main() -> int:
    full = load_full()
    held = load_held()
    auto = load_auto_pool()
    mirror = load_mirror_holdings()
    errs = []

    union = set(held) | set(auto)
    if union != set(full):
        errs.append(f"孤岛条目: full-union={sorted(set(full) - union)} "
                    f"union-full={sorted(union - set(full))}")

    if not set(mirror) <= set(auto):
        errs.append(f"mirror 不在 auto 池: {sorted(set(mirror) - set(auto))}")

    if set(_ap.AUTO_POOL) != set(auto):
        errs.append(f"AUTO_POOL 与 load_auto_pool 不一致: "
                    f"{sorted(set(_ap.AUTO_POOL) ^ set(auto))}")

    for c, h in full.items():
        if (str(h.get("pool") or "") == "manual") != _ap.is_manual(c):
            errs.append(f"{c}: pool={h.get('pool')} 但 is_manual={_ap.is_manual(c)}")

    for c in auto:
        if not auto[c].get("gm_symbol"):
            errs.append(f"{c} 缺 gm_symbol")

    print(f"full={len(full)} held={len(held)} auto={len(auto)} mirror={len(mirror)}")
    if errs:
        print("RESULT: FAIL")
        for e in errs:
            print(" -", e)
        return 1
    print("RESULT: PASS（持仓单一真源派生一致，无孤岛/无重复清单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
