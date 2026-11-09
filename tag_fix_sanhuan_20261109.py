# -*- coding: utf-8 -*-
"""2026-11-09 owner：「三环集团归于电力，请查询合理性」→ 确认不合理后「更改」。

核实：
  · 三环集团 300408 主营**电子元件及其基础材料**（MLCC、陶瓷插芯、陶瓷封装基座、电子陶瓷），
    东财行业 = 元件/CPO/消费电子/5G；
  · 「电力/发电设备」这条来自早期 **SOFC**（固体氧化物燃料电池）标签 —— 三环只是给 SOFC 供
    电解质隔膜板（材料），不是发电设备制造商；
  · 该股自己的光通信纠错判词已明确：「MLCC、SOFC…与本次光通信标签无关…删除 MLCC、SOFC 等
    旧交叉污染」⇒ 电力那条是遗留。

改法：
  ① 删除 `电力 / 发电设备`；
  ② 补 `消费电子 / 被动元件`（MLCC/电子陶瓷核心业务，同细分已有 海星股份、艾华集团）；
  ③ 光通信的两个标签保留；`jiuyan_concept_reason` 里删掉「发电设备/SOFC」理由、补「被动元件」。

写前备份；幂等。
运行：`python tag_fix_sanhuan_20261109.py [--apply]`
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
CODE = "300408"
DROP = ("电力", "发电设备")
ADD = ("消费电子", "被动元件")
REASON = ("三环集团：电子元件及基础材料（MLCC/电子陶瓷/陶瓷插芯）；"
          "SOFC 电解质隔膜板只是材料供应，不构成发电设备 ⇒ 移出电力")
NOTE_KEY = "jiuyan_sanhuan_fix_20261109"


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
    e = wl.get(CODE)
    if not isinstance(e, dict):
        print(f"{CODE} 不在 watchlist")
        return 1
    pairs = get_pairs(e)
    print("现状:", pairs)
    has_drop = DROP in pairs
    has_add = any(c == ADD[0] for c, _ in pairs)
    if not has_drop and has_add:
        print("已是目标状态，无需改动")
        return 0
    out = [(c, p) for c, p in pairs if (c, p) != DROP]
    if not has_add:
        out.append(ADD)
    print("目标:", out)
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    put_pairs(e, out, now)
    rs = e.get("jiuyan_concept_reason")
    if isinstance(rs, dict):
        for k in ("发电设备", "SOFC"):
            rs.pop(k, None)
        rs[ADD[1]] = REASON
        e["jiuyan_concept_reason"] = rs
    e[NOTE_KEY] = f"{now} 移出电力/发电设备（SOFC 只是材料供应），补 消费电子/被动元件"
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_sanhuan_fix_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print("已改；备份 backup/watchlist_jiuyan_before_sanhuan_fix_*.json")
    print("新标签:", get_pairs(e))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
