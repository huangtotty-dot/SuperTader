# -*- coding: utf-8 -*-
"""2026-11-08 owner：「大农业板块的『北交所』次级概念取消，里面的个股合入其它次级板块」。

6 只票都只有一对标签 `大农业 / 北交所`（「北交所」只是板块标记，不是业务），
按各自主营归到 **大农业 现有词表** 里的细分（不新造概念）：

  920087 秋乐种业  玉米/小麦种子          → 种业-玉米
  920403 康农种业  杂交玉米种子            → 种业-玉米
  920786 骑士乳业  生鲜乳（奶牛/乳品）      → 牛
  920895 花溪科技  打捆机/玉米割台等农机    → 农机
  920866 绿亨科技  农药制剂                → 农药
  920964 润农节水  节水灌溉装备与工程       → 农机

口径守 `jiuyan_tagging_convention`（2026-10-19「移入某概念」例外）：
类别不变（仍是 大农业）、概念数不减少、cat/con 成对写、`jiuyan_concept_reason`
的键跟着概念名一起迁移；写前备份；脚本幂等。

⚠️ 大消费板块里还有 5 只 `大消费 / 大消费_北交所`（太湖雪/海达尔/美之高/盖世食品/鸿智科技），
owner 本次只点了大农业，**没动**。
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

CAT = "大农业"
DROP_CON = "北交所"
REASSIGN = {
    "920087": "种业-玉米",
    "920403": "种业-玉米",
    "920786": "牛",
    "920895": "农机",
    "920866": "农药",
    "920964": "农机",
}
NOTE_KEY = "jiuyan_beijiaosuo_reassigned_20261108"


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


def put_pairs(e: dict, pairs: list, now: str, had_slots: bool) -> None:
    for k in range(1, 10):
        e.pop(f"jiuyan_category{k}", None)
        e.pop(f"jiuyan_concept{k}", None)
    if had_slots:
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
    for code, new_con in REASSIGN.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if (CAT, DROP_CON) not in pairs:
            skipped.append((code, e.get("name"), f"没有 {CAT}/{DROP_CON}（现已 {'|'.join(c for c,_ in pairs) or '空'}）"))
            continue
        had_slots = any(e.get(f"jiuyan_category{k}") for k in range(1, 10))
        kept = [(c, new_con if (c == CAT and p == DROP_CON) else p) for c, p in pairs]
        put_pairs(e, kept, now, had_slots)
        # 理由的键跟着概念名迁移（保留原文，去掉「北交所只是板块标记」这句说明可留）
        rs = e.get("jiuyan_concept_reason")
        if isinstance(rs, dict) and DROP_CON in rs:
            rs[new_con] = rs.pop(DROP_CON)
            e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 由 {CAT}/{DROP_CON} 改派为 {CAT}/{new_con}（owner 2026-11-08）"
        changed.append((code, e.get("name"), new_con, pairs))
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_beijiaosuo_reassign_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"改派 {len(changed)} 只；跳过 {len(skipped)} 只")
    for code, name, con, old in changed:
        print(f"  {code} {name}: 北交所 → {con}（原 {old}）")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
