# -*- coding: utf-8 -*-
"""[已弃用 2026-10-04] 按 owner 2026-09-14 截图设置目标底仓（base）——一次性历史脚本。

持仓真源已拆分为 holdings_manual.json / holdings_auto.json，本脚本硬编码的 09-15 目标值
早已过时，且旧 holdings.json 已删除。**请勿再运行**——目标底仓现经 GUI「自动盘」页或
`src/holdings_repo.save_auto` 维护。保留此文件仅为审计追溯。

用法：python scripts/set_target_base_20260915.py（会直接拒绝运行）
"""
import argparse
import json
import os
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS = os.path.join(ROOT, "t_io", "state", "holdings.json")  # 已删除（拆分后）

TARGET = {
    "588170": 74500, "600176": 1600, "600481": 100, "603667": 500,
    "000988": 300, "002451": 2800, "002639": 900, "300054": 600, "300153": 500,
}
COST = {
    "588170": 0.927, "600176": 42.913, "600481": 28.216, "603667": 50.569,
    "000988": 99.522, "002451": 7.871, "002639": 13.308, "300054": 70.429,
    "300153": 22.514,
}


def main():
    print("[弃用] 持仓已拆分为 holdings_manual/auto.json，旧 holdings.json 不存在。")
    print("       目标底仓请经 GUI「自动盘」页或 src/holdings_repo.save_auto 维护。")
    print("       本一次性脚本不再执行。")
    return 1


def _legacy_main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    with open(HOLDINGS, encoding="utf-8") as f:
        h = json.load(f)
    print(f"{'code':8}{'名称':10}{'旧qty':>8}{'旧base':>8} → {'新qty':>8}{'新base':>8}{'新cost':>9}")
    for c, tgt in sorted(TARGET.items()):
        p = h.setdefault(c, {})
        print(f"{c:8}{str(p.get('name',''))[:8]:10}{str(p.get('qty')):>8}{str(p.get('base')):>8} → "
              f"{tgt:>8}{tgt:>8}{COST.get(c,0):>9}")
        p["qty"] = int(tgt)
        p["base"] = int(tgt)
        p["cost"] = float(COST.get(c, p.get("cost") or 0))
    # 截图未出现的候选票：base 归零（不再是目标），qty 保持 0
    for c, p in h.items():
        if str(c).startswith("_") or not isinstance(p, dict):
            continue
        if c not in TARGET:
            p["base"] = 0
            p["qty"] = 0
    if not args.write:
        print("\n[dry-run] 未落盘；加 --write 执行（先备份）")
        return 0
    bak = HOLDINGS + f".pre_align_{datetime.now().strftime('%Y-%m-%d')}"
    shutil.copy2(HOLDINGS, bak)
    tmp = HOLDINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HOLDINGS)
    print(f"\n[OK] 已写入 {len(TARGET)} 票目标；备份 {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
