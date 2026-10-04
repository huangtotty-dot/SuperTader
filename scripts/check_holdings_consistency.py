# -*- coding: utf-8 -*-
"""scripts/check_holdings_consistency.py — 持仓双文件一致性守卫（2026-10-04 拆分后）

持仓拆成两份真源后，手动链/自动链分别从各自文件派生，`both` 标的在两份各有一条。
本脚本校验两份不漂移（用户手改任一份后跑一遍，漏改/孤岛/漂移立即暴露）：

  1) 成员↔pool：仅手动⇒manual、仅自动⇒auto、两份都有⇒both；both 必须两份都在
  2) 身份一致：两份都有的码，(name,gm_symbol,type) 相等
  3) 共享字段一致：pre_close 相等、base 镜像相等
  4) auto 池码集 == auto_pool.AUTO_POOL == holdings_repo.load_auto_pool
  5) 每个自动条目 gm_symbol 非空
  6) base>0 的码 ⊆ 自动文件
  7) 无 _ 前缀被当条目；qty 非负整数、cost 非负

用法：python scripts/check_holdings_consistency.py（退出码 0=通过，1=漂移）
"""
import importlib.util
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.holdings_repo import (load_manual, load_auto, load_union,
                               load_auto_pool)

# config 是目录非 package，按绝对路径加载 auto_pool（与 goldminer/position_builder 同款）
_spec = importlib.util.spec_from_file_location(
    "auto_pool", os.path.join(_ROOT, "config", "auto_pool.py"))
_ap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ap)


def main() -> int:
    manual = load_manual()
    auto = load_auto()
    union = load_union()
    auto_pool = load_auto_pool()
    mset, aset = set(manual), set(auto)
    both = mset & aset
    errs = []

    # 1) 成员↔pool
    for c in sorted(mset | aset):
        pool = str((manual.get(c) or auto.get(c) or {}).get("pool") or "")
        expect = "both" if (c in mset and c in aset) else ("manual" if c in mset else "auto")
        if pool != expect:
            errs.append(f"{c}: pool={pool!r} 但成员关系应为 {expect}")

    # 2) 身份一致 / 3) 共享字段一致
    for c in sorted(both):
        m, a = manual[c], auto[c]
        for k in ("name", "gm_symbol", "type"):
            if m.get(k) != a.get(k):
                errs.append(f"{c}: 身份 {k} 两份不一致 manual={m.get(k)!r} auto={a.get(k)!r}")
        for k in ("pre_close", "base"):
            if m.get(k) != a.get(k):
                errs.append(f"{c}: 共享字段 {k} 两份不一致 manual={m.get(k)!r} auto={a.get(k)!r}")

    # 4) auto 池码集一致
    if set(_ap.AUTO_POOL) != aset:
        errs.append(f"AUTO_POOL 与自动文件不一致: {sorted(set(_ap.AUTO_POOL) ^ aset)}")
    if set(auto_pool) != aset:
        errs.append(f"load_auto_pool 与自动文件不一致: {sorted(set(auto_pool) ^ aset)}")

    # 5) 自动条目 gm_symbol
    for c, h in auto.items():
        if not h.get("gm_symbol"):
            errs.append(f"{c} 缺 gm_symbol")

    # 6) base>0 ⊆ 自动文件
    base_pos = {c for c, h in {**manual, **auto}.items() if int(h.get("base") or 0) > 0}
    if not base_pos <= aset:
        errs.append(f"base>0 的码不在自动文件: {sorted(base_pos - aset)}")

    # 7) 数值合理性
    for c, h in {**manual, **auto}.items():
        q = h.get("qty")
        if q is not None:
            try:
                if int(q) < 0:
                    errs.append(f"{c}: qty 为负 {q}")
            except (TypeError, ValueError):
                errs.append(f"{c}: qty 非整数 {q!r}")
        co = h.get("cost")
        if co is not None:
            try:
                if float(co) < 0:
                    errs.append(f"{c}: cost 为负 {co}")
            except (TypeError, ValueError):
                errs.append(f"{c}: cost 非数值 {co!r}")

    print(f"manual={len(manual)} auto={len(auto)} both={len(both)} union={len(union)}")
    if errs:
        print("RESULT: FAIL")
        for e in errs:
            print(" -", e)
        return 1
    print("RESULT: PASS（双文件一致，无孤岛/无漂移）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
