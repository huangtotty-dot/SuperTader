# -*- coding: utf-8 -*-
"""2026-11-04 owner：「交运板块取消」。

背景：2026-10-31 `tag_recap_rank_20261031.py` 为复盘里的「航运/港口/高速公路」
新增了一级分类 **交运**（4 只），并把它接进 `sector_phase` 的板块映射与主题。
owner 现在决定取消该板块 ⇒
  ① 把这 4 只票的 **交运** 标签对整对摘掉（只摘 category=交运 的对，其余标签不动）；
     摘完概念为空 ⇒ 它们自然退出猎手打分池（池 = 概念非空）。
  ② 从 `stock_hunter/modules/sector_phase.py` 移除「交运」板块与「交运/航运」主题映射。

写前备份 `backup/watchlist_jiuyan_before_cancel_jiaoyun_<ts>.json`；脚本幂等。
口径守 `jiuyan_tagging_convention`：成对写 cat/con、编号字段一起重排。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"
DROP_CAT = "交运"


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
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed = []
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        if not any(c == DROP_CAT for c, _ in pairs):
            continue
        kept = [(c, p) for c, p in pairs if c != DROP_CAT]
        put_pairs(e, kept, now)
        changed.append((code, e.get("name"), len(pairs), kept))
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_cancel_jiaoyun_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"取消「{DROP_CAT}」：改动 {len(changed)} 只")
    for code, name, n0, kept in changed:
        print(f"  - {code} {name}: {n0} 对 → {len(kept)} 对 {kept if kept else '（清空，退出猎手池）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
