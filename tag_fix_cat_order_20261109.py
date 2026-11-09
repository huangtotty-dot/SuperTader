# -*- coding: utf-8 -*-
"""2026-11-09 修复：商业航天/光伏/机器人 三轮回聚把**分类顺序**也改了。

## 问题（核实）
`tag_reagg_{space,pv,robot}_20261109.py` 的落盘写法是
`out = [(c, cn) for c, cn in pairs if c != CAT] + [(CAT, grp)]`
——把目标类目**挪到末尾**。对「目标类目本来排在第一位」的票，`cats[0]` 因此翻到
后面的类目（如 000547 航天发展：商业航天→军工；002126 银轮股份：机器人→液冷）。
`cats[0]` 是 OSC 选股 / `sym2board_map` / `_jiuyan_board` 的**主板块输入**，
顺序一变主板块就变 ⇒ 板块涨停家数排名随之改变。**本轮军工脚本已改成原地替换。**

实际影响：与 18:31:21 的基线比，**35 只**票的类目顺序被改（cats[0] 全部跟着变）。

## 修法
以 `backup/watchlist_jiuyan_before_space_reagg_20261109_183121.json`（当天三轮回聚之前）
的**类目顺序**为准，只重排顺序、**沿用当前的概念值**（本轮军工/机器人等新分组保留）。
- 类目集合与基线不同的 3 只（600261/600481/603698）**不动**：那是去重/改派的**有意**结果，
  不是顺序问题（基线里的商业航天已按业务被移除）。
- 写前备份到 `backup/`；幂等（重跑顺序已一致 ⇒ 0 只）。

运行：`python tag_fix_cat_order_20261109.py [--apply]`
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"
BASE_FILE = BACKUP / "watchlist_jiuyan_before_space_reagg_20261109_183121.json"
NOTE_KEY = "jiuyan_cat_order_fix_20261109"


def _toks(cn: str) -> list:
    s = str(cn or "").strip()
    if s.startswith("["):
        try:
            return [str(x).strip() for x in ast.literal_eval(s) if str(x).strip()]
        except Exception:
            return [s]
    return [x.strip() for x in s.split("|") if x.strip()]


def norm(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return "|".join(str(v).strip() for v in x if str(v).strip())
    return str(x).strip()


def get_pairs(e: dict) -> list:
    out = []
    for k in range(1, 10):
        c = norm(e.get(f"jiuyan_category{k}"))
        if c:
            out.append((c, norm(e.get(f"jiuyan_concept{k}"))))
    if out:
        return out
    cats = [c.strip() for c in norm(e.get("jiuyan_category")).split("|") if c.strip()]
    cons = [c.strip() for c in norm(e.get("jiuyan_concept")).split("|") if c.strip()]
    return [(c, cons[i] if i < len(cons) else "") for i, c in enumerate(cats)]


def put_pairs(e: dict, pairs: list, now: str) -> None:
    for k in range(1, 10):
        e.pop(f"jiuyan_category{k}", None)
        e.pop(f"jiuyan_concept{k}", None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f"jiuyan_category{k}"] = c
        if p:
            e[f"jiuyan_concept{k}"] = p
    e["jiuyan_category"] = "|".join(c for c, _ in pairs)
    e["jiuyan_concept"] = "|".join(p for _, p in pairs if p)
    e["updated_at"] = now


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    base = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan, mismatch = [], []
    for code, e in wl.items():
        if not isinstance(e, dict) or not isinstance(base.get(code), dict):
            continue
        b, c = get_pairs(base[code]), get_pairs(e)
        if not b:
            continue
        bc, cc = [x for x, _ in b], [x for x, _ in c]
        if Counter(bc) != Counter(cc):
            if bc != cc:
                mismatch.append((code, e.get("name"), bc, cc))
            continue
        if bc == cc:
            continue
        # 按基线类目顺序重排，概念沿用当前值；同类目多条时按序各取一条
        pool = list(c)
        out = []
        for cat in bc:
            for i, (ccat, con) in enumerate(pool):
                if ccat == cat:
                    out.append((cat, con))
                    pool.pop(i)
                    break
        out.extend(pool)
        plan.append((code, e.get("name"), bc, cc, out))
    print(f"类目顺序与基线不同：{len(plan)} 只")
    for code, name, bc, cc, out in plan[:12]:
        print(f"  {code} {name}: {cc} -> {[x for x, _ in out]}")
    if mismatch:
        print(f"类目集合不同（不动，属去重/改派的有意结果）：{len(mismatch)} 只")
        for m in mismatch:
            print(f"  {m[0]} {m[1]}: {m[2]} -> {m[3]}")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, bc, cc, out in plan:
        e = wl[code]
        put_pairs(e, out, now)
        e[NOTE_KEY] = f"{now} 恢复分类顺序 → {[x for x, _ in out]}"
        changed += 1
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_cat_order_fix_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_cat_order_fix_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
