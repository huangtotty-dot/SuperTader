# -*- coding: utf-8 -*-
"""t_io/state/holdings.json 统一迁移（2026-09-14）—— 一份真源 + 目标底仓并表。

背景（见 doc 方案 / commit 32e63d46 之后）：
- holdings.json 是**旧手动盘遗留**，qty 停在手动账户（600176=1600/002451=2800/588170=74500），
  而唯一真实账户（模拟盘）实际是 800/1100/70000（owner 截图 + 引擎 INIT 对账双向确认）。
- 截图 reconcile 随 manual 做T 下线，**已无任何代码写 qty** → 持仓数据冻结成僵尸。
- 目标底仓住在 config/auto_pool.AUTO_MIRROR_OVERRIDE，与实际持仓分居两处（"两个表说不同的话"的根源）。

本迁移把三者并成一份：身份 + 实际持仓（qty/cost）+ 目标底仓（base），并删掉手动盘遗留字段。

用法：
    python scripts/migrate_holdings_unified.py            # dry-run，只打印
    python scripts/migrate_holdings_unified.py --write    # 落盘（先备份）
"""
import argparse
import importlib.util
import json
import os
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOLDINGS = os.path.join(ROOT, "t_io", "state", "holdings.json")

# 账户真实持仓（owner 2026-09-14 模拟盘截图；引擎 INIT 对账逐笔吻合）
ACCOUNT_QTY = {
    "300054": (100, 67.53), "600481": (100, 4.32), "002451": (1100, 7.85),
    "588170": (70000, 0.914), "600176": (800, 42.96), "603667": (500, 49.86),
}
# 只保留这 8 个字段；其余（account/available_qty/frozen_qty/t_qty/virtual_qty/mirror_qty/mirror_cost）删除
KEEP = ("name", "gm_symbol", "type", "pool", "qty", "cost", "base", "pre_close")


def _load_override():
    spec = importlib.util.spec_from_file_location("ap", os.path.join(ROOT, "config", "auto_pool.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return getattr(m, "AUTO_MIRROR_OVERRIDE", {}) or {}


def build():
    with open(HOLDINGS, encoding="utf-8") as f:
        old = json.load(f)
    ov = _load_override()
    out, changes = {}, []
    for code, p in old.items():
        if not isinstance(p, dict) or str(code).startswith("_"):
            continue
        qty, cost = ACCOUNT_QTY.get(code, (0, 0.0))
        base = ov.get(code, p.get("base") or p.get("qty") or 0)
        entry = {k: p.get(k) for k in KEEP if k in p}
        entry.update({"qty": int(qty), "cost": float(cost), "base": int(base or 0)})
        entry.setdefault("qty", 0)
        entry.setdefault("base", 0)
        entry.setdefault("cost", 0.0)
        dropped = sorted(set(p) - set(KEEP))
        out[code] = entry
        changes.append((code, p.get("qty"), qty, p.get("base"), base, dropped))
    return out, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="落盘（缺省 dry-run）")
    args = ap.parse_args()
    new, changes = build()
    print(f"{'code':8}{'旧qty':>8}{'新qty':>8}{'旧base':>8}{'新base':>8}  删除字段")
    for c, oq, nq, ob, nb, dr in changes:
        print(f"{c:8}{str(oq):>8}{nq:>8}{str(ob):>8}{nb:>8}  {','.join(dr) if dr else '-'}")
    if not args.write:
        print("\n[dry-run] 未落盘；加 --write 执行（会先备份 holdings.json.pre_unify_<date>）")
        return 0
    bak = HOLDINGS + f".pre_unify_{datetime.now().strftime('%Y-%m-%d')}"
    shutil.copy2(HOLDINGS, bak)
    tmp = HOLDINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(new, f, ensure_ascii=False, indent=2)
    os.replace(tmp, HOLDINGS)
    print(f"\n[OK] 已迁移 {len(new)} 票 → {HOLDINGS}\n     备份: {bak}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
