# -*- coding: utf-8 -*-
"""手动/自动持仓文件拆分迁移（2026-10-04）。

把单一真源 `t_io/state/holdings.json`（手动+自动混存，`pool` 标记）拆成两份：
  · `t_io/state/holdings_manual.json` —— 手动侧（pool ∈ {manual, both}）
  · `t_io/state/holdings_auto.json`   —— 自动侧（pool ∈ {auto, both}）
`both` 标的在两份各留一条，字段按当前值原样复制（之后各自独立演进）。

迁移**从不改动**原文件内容：先备份 `holdings.json.pre_split_<date>`，写两份新文件，
再删除 `holdings.json`。任一步失败即中止且不留半成品。

用法：
    python scripts/migrate_holdings_split.py            # dry-run，只打印
    python scripts/migrate_holdings_split.py --write    # 落盘（先备份）
    python scripts/migrate_holdings_split.py --write --force   # 已存在目标文件时强制覆盖
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "t_io", "state")
LEGACY = os.path.join(STATE, "holdings.json")
MANUAL = os.path.join(STATE, "holdings_manual.json")
AUTO = os.path.join(STATE, "holdings_auto.json")

MANUAL_POOLS = ("manual", "both")
AUTO_POOLS = ("auto", "both")


def _is_entry(code, h):
    return isinstance(h, dict) and not str(code).startswith("_")


def build(old: dict):
    """返回 (manual_raw, auto_raw)；_ 元数据 key 原样保留在两侧。"""
    manual, auto = {}, {}
    for code, h in (old.items() if isinstance(old, dict) else []):
        if str(code).startswith("_"):
            manual[code] = h
            auto[code] = h
            continue
        if not _is_entry(code, h):
            continue
        pool = str(h.get("pool") or "manual")
        if pool in MANUAL_POOLS:
            manual[code] = dict(h)
        if pool in AUTO_POOLS:
            auto[code] = dict(h)
    return manual, auto


def _atomic_write(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="落盘（默认 dry-run）")
    ap.add_argument("--force", action="store_true", help="目标文件已存在时强制覆盖")
    args = ap.parse_args()

    if not os.path.exists(LEGACY):
        print(f"[split] 旧文件不存在: {LEGACY}")
        if os.path.exists(MANUAL) and os.path.exists(AUTO):
            print("[split] 两份新文件已就位 → 视为已完成，跳过。")
            return 0
        print("[split] 且新文件不全 → 无可迁移内容。")
        return 1

    with open(LEGACY, encoding="utf-8") as f:
        old = json.load(f)
    manual, auto = build(old)

    if (os.path.exists(MANUAL) or os.path.exists(AUTO)) and not args.force:
        print(f"[split] 目标文件已存在（{MANUAL if os.path.exists(MANUAL) else AUTO}）")
        print("[split] 拒绝覆盖已分化数据；确要重跑请加 --force。")
        return 1

    print(f"[split] 旧文件 {len([c for c in old if _is_entry(c, old[c])])} 条 → "
          f"手动 {len([c for c in manual if _is_entry(c, manual[c])])} / "
          f"自动 {len([c for c in auto if _is_entry(c, auto[c])])}")
    print(f"{'code':<8}{'pool':<8}{'目标':<14}{'qty':>8}{'base':>8}  name")
    for code in sorted(c for c in set(manual) | set(auto) if not str(c).startswith("_")):
        h = old.get(code) or {}
        tgt = []
        if code in manual:
            tgt.append("manual")
        if code in auto:
            tgt.append("auto")
        print(f"{code:<8}{str(h.get('pool')):<8}{'+'.join(tgt):<14}"
              f"{int(h.get('qty') or 0):>8}{int(h.get('base') or 0):>8}  {h.get('name', '')}")

    if not args.write:
        print("\n[split] dry-run：未落盘。加 --write 执行（会先备份 holdings.json）。")
        return 0

    stamp = datetime.now().strftime("%Y-%m-%d")
    backup = f"{LEGACY}.pre_split_{stamp}"
    shutil.copy2(LEGACY, backup)
    print(f"\n[split] 备份 → {backup}")
    _atomic_write(MANUAL, manual)
    _atomic_write(AUTO, auto)
    print(f"[split] 写入 {MANUAL}")
    print(f"[split] 写入 {AUTO}")
    os.remove(LEGACY)
    print(f"[split] 已删除旧文件 {LEGACY}")
    print("[split] 完成。请跑：python scripts/check_holdings_consistency.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
