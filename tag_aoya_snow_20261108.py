# -*- coding: utf-8 -*-
"""2026-11-08 owner：把「奥雅股份 300949」加入 **冰雪经济** 板块。

依据（2026-11-08 核对）：
  · 公司 = 深圳奥雅设计，景观/文旅设计与运营策划，不是冰雪设备制造商；
  · 中山「冬夏同在」室内冰雪主题乐园：整体运营策划 + 建筑/景观/室内一体化设计；
  · 北京八角新乐园（冬奥景观大道沿线）早年为公司设计；
  · 董秘互动明确「高度重视冰雪经济」，初步建立以「冰雪文化」为魂的产品模型。
⇒ 细分概念取现有词表里的 **旅游消费**（同板块的 万科A/天府文旅/中兴商业 用的就是它），
  而不是「场地设备」（那是 冰山冷热/英派斯/中工国际 这类设备/工程口径）。

口径守 `jiuyan_tagging_convention`：只增不改、成对写 cat/con、写前备份、脚本幂等。
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
ADD = {"300949": ("冰雪经济", "旅游消费")}      # 奥雅股份


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
    lc = norm(e.get("jiuyan_category"))
    lp = norm(e.get("jiuyan_concept"))
    return [(t.strip(), lp) for t in lc.split("|") if t.strip()] if lc else []


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
    changed, skipped = [], []
    for code, (cat, con) in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if any(c == cat for c, _ in pairs):
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, pairs))
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_aoya_snow_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"新增 {len(changed)} 只；跳过 {len(skipped)} 只")
    for code, name, cat, con, old in changed:
        print(f"  + {code} {name}: {cat}/{con}（原: {'|'.join(c for c,_ in old) or '—'}）")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
