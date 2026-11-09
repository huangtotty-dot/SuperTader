# -*- coding: utf-8 -*-
"""2026-11-09 owner：「商业航天的板块分类太散了，重新分类，注意聚合性」。

## 现状（核实）
102 条成员散在 **68 个细分**里，绝大多数只有 1 只票（航天紧固件/星载芯片/空间电源/
卫星网络安全/火箭环形锻件…），而且部分槽位里存的是 `['卫星总体与制造', '卫星平台…']`
这种**列表字符串**（历史脚本把 list 的 repr 写进了 concept）⇒ 分组页一堆碎块。

## 重新分类（68 → 5，按产业链环节聚合）
  ① **火箭与发射**：火箭总体/动力/结构/紧固件/锻件/推进 + 发射场与地面保障
  ② **卫星制造与载荷**：卫星总体/平台/结构/太阳翼/电源 + 星载电子/芯片/射频 + 载荷/连接器/测控组件
  ③ **卫星通信与运营**：卫星通信终端与天线、卫星运营、卫星互联网/网络、网络安全
  ④ **地面与数据应用**：地面站/测运控、遥感和卫星数据、测绘、测控与信息安全
  ⑤ **航天材料与配套**：材料/增材/复合材料、通用零部件、环境试验与检测、股权与产业投资

## 移出商业航天（主营不是航天）
  · 603698 航天工程（航天科技集团下属**煤气化**装备）→ 化工/基础化工
  · 600261 阳光照明（LED 照明）→ 消费电子/LED
  · 600481 双良节能（换热/多晶硅还原炉/单晶硅片）→ 光伏/太空光伏设备

写前备份；幂等；每只票打 `jiuyan_space_reagg_20261109` 审计标记。
运行：`python tag_reagg_space_20261109.py [--apply]`
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
NOTE_KEY = "jiuyan_space_reagg_20261109"
CAT = "商业航天"

G_ROCKET = "火箭与发射"
G_SAT = "卫星制造与载荷"
G_COMM = "卫星通信与运营"
G_GROUND = "地面与数据应用"
G_SUPPLY = "航天材料与配套"

# 关键词 → 分组（按顺序匹配；越靠前优先级越高）
RULES = [
    (G_ROCKET, ("火箭", "发射场", "推进", "紧固件", "锻件", "发动机")),
    (G_COMM, ("卫星通信", "终端与天线", "卫星运营", "互联网", "网络", "网络安全", "射频前端")),
    (G_SAT, ("卫星总体", "卫星平台", "卫星结构", "太阳翼", "空间电源", "星载",
             "卫星载荷", "航天电子", "连接器", "测控组件")),
    (G_GROUND, ("地面站", "遥感", "卫星数据", "测绘", "测控与信息安全")),
    (G_SUPPLY, ("材料", "复合", "配套", "检测", "试验", "股权", "投资", "航天配套")),
]
# 个股覆盖（按主营）
OVERRIDE = {
    "688387": G_COMM,    # 信科移动-U：通信设备（载荷+网络+终端）
    "600343": G_ROCKET,  # 航天动力：泵/流体（火箭推进）
    "688818": G_SAT,     # 电科蓝天：空间电源
}
# 移出商业航天：(新大类, 新概念)
MOVE = {
    "603698": ("化工", "基础化工"),          # 航天工程：煤气化装备
    "600261": ("消费电子", "LED"),           # 阳光照明：LED 照明
    "600481": ("光伏", "太空光伏设备"),      # 双良节能：换热/还原炉/硅片
}


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


def classify(tokens: list) -> str:
    for group, kws in RULES:
        for t in tokens:
            if any(k in t for k in kws):
                return group
    return G_SUPPLY


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan = []
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        sp = [(c, cn) for c, cn in pairs if c == CAT]
        if not sp:
            continue
        if code in MOVE:
            plan.append((code, e.get("name"), "move", MOVE[code], pairs, sp))
            continue
        toks = []
        for _, cn in sp:
            toks.extend(_toks(cn))
        grp = OVERRIDE.get(code) or classify(toks)
        plan.append((code, e.get("name"), "group", grp, pairs, sp))
    dist = Counter(p[3] if p[2] == "group" else "（移出：" + "/".join(p[3]) + "）" for p in plan)
    print(f"商业航天 成员 {len(plan)} 只 → 新分组：")
    for k, v in dist.most_common():
        print(f"  {k}: {v}")
    print("\n需人工/覆盖的：")
    for p in plan:
        if p[0] in OVERRIDE or p[2] == "move":
            print(f"  {p[0]} {p[1]}: {' → '.join(p[3]) if p[2] == 'move' else p[3]}（原：{' / '.join(t for _, cn in p[5] for t in _toks(cn))}）")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, kind, val, pairs, sp in plan:
        e = wl[code]
        if kind == "move":
            new_cat, new_con = val
            out = [(new_cat, new_con)] + [(c, cn) for c, cn in pairs if c != CAT]
        else:
            out = [(c, cn) for c, cn in pairs if c != CAT] + [(CAT, val)]
        put_pairs(e, out, now)
        # 理由：旧细分键作废，给聚合后的分组写一句「原名：原细分（新分组）」
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        _old_toks = []
        for _, cn in sp:
            _old_toks.extend(_toks(cn))
        for k in _old_toks:
            rs.pop(k, None)
        if kind == "group" and _old_toks:
            rs[val] = f"{e.get('name') or code}：{' / '.join(dict.fromkeys(_old_toks))[:60]}（{val}）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 商业航天聚合：{val if kind == 'group' else '/'.join(val)}"
        changed += 1
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_space_reagg_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_space_reagg_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
