# -*- coding: utf-8 -*-
"""2026-11-09 owner：「大消费_冰雪经济删除（和冰雪经济重合了）；把图片里的白酒补充到
大消费的『白酒』概念」。

## ① 删除 大消费 / 大消费_冰雪经济（8 只）
截图「大消费_冰雪经济」组 = 000530 冰山冷热 / 000888 峨眉山A / 002159 三特索道 /
301010 晶雪节能 / 600593 大连圣亚 / 600881 亚泰集团 / 601188 龙江交通 / 603099 长白山。
其中 5 只已经另有独立的「冰雪经济」类目；**000888/002159/600881 没有**
⇒ 删掉这一对后它们将没有任何韭研概念（会掉出猎手池），脚本会明确列出。

## ② 补充 大消费 / 白酒：东财「白酒」板块 19 只中缺的 11 只
已有 8 只（000568 泸州老窖 / 000860 顺鑫农业 / 600199 金种子酒 / 600809 山西汾酒 /
603198 迎驾贡酒 / 603369 今世缘 / 603589 口子窖 / 603919 金徽酒）不动；
缺的 11 只：
  000995 皇台酒业 / 000596 古井贡酒 / 600779 水井坊 / 000799 酒鬼酒 / 600702 舍得酒业
  → 这 5 只原本是 大消费_酒类：**保留酒类、追加白酒**（只增不改；如需「移入」再说）
  002646 天佑德酒 / 600197 伊力特 / 600519 贵州茅台 / 000858 五粮液 / 600559 老白干酒 /
  002304 洋河股份  → 原本无韭研概念（补标签后进猎手池）

写前备份到 backup/；幂等；每只票打 `jiuyan_baijiu_snow_20261109` 审计标记。
运行：`python tag_baijiu_snow_20261109.py [--apply]`
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"
NOTE_KEY = "jiuyan_baijiu_snow_20261109"
CAT = "大消费"
DROP_TOKEN = "大消费_冰雪经济"
BAIJIU = "白酒"

REMOVE_SNOW = {
    "000530": "冰山冷热", "000888": "峨眉山A", "002159": "三特索道",
    "301010": "晶雪节能", "600593": "大连圣亚", "600881": "亚泰集团",
    "601188": "龙江交通", "603099": "长白山",
}
ADD_BAIJIU = {
    "000995": "皇台酒业", "000596": "古井贡酒", "600779": "水井坊",
    "000799": "酒鬼酒", "600702": "舍得酒业",
    "002646": "天佑德酒", "600197": "伊力特", "600519": "贵州茅台",
    "000858": "五粮液", "600559": "老白干酒", "002304": "洋河股份",
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


def has_baijiu(pairs: list) -> bool:
    return any(c == CAT and BAIJIU in _toks(cn) for c, cn in pairs)


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    drops, adds, empty_after, missing = [], [], [], []

    for code, name in REMOVE_SNOW.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            missing.append((code, name, "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        keep = [(c, cn) for c, cn in pairs if not (c == CAT and DROP_TOKEN in _toks(cn))]
        if len(keep) == len(pairs):
            continue                      # 幂等：已经没有这一对
        drops.append((code, name, pairs, keep))
        if not keep and not any(cn for _, cn in keep):
            empty_after.append((code, name))

    for code, name in ADD_BAIJIU.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            missing.append((code, name, "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        if has_baijiu(pairs):
            continue                      # 幂等：已有白酒
        adds.append((code, name, pairs))

    print(f"① 删除 {CAT}/{DROP_TOKEN}：{len(drops)} 只")
    for code, name, before, after in drops:
        print(f"  {code} {name}: {before} -> {after}")
    if empty_after:
        print(f"  ⚠️ 删后无任何韭研概念（将掉出猎手池）：{len(empty_after)} 只 "
              + "、".join(f"{c} {n}" for c, n in empty_after))
    print(f"\n② 补充 {CAT}/{BAIJIU}：{len(adds)} 只")
    for code, name, before in adds:
        print(f"  {code} {name}: {before} -> +('{CAT}','{BAIJIU}')")
    if missing:
        print("\n⚠️ 未处理（不在池）：", missing)
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, before, keep in drops:
        e = wl[code]
        put_pairs(e, keep, now)
        rs = e.get("jiuyan_concept_reason")
        if isinstance(rs, dict):
            for t in _toks(DROP_TOKEN):
                rs.pop(t, None)
            rs.pop(DROP_TOKEN, None)
            e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 删除 {CAT}/{DROP_TOKEN}"
        changed += 1
    for code, name, before in adds:
        e = wl[code]
        out = [(c, cn) for c, cn in before] + [(CAT, BAIJIU)]
        put_pairs(e, out, now)
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        rs[BAIJIU] = f"{name}：白酒（东财白酒板块 19 只补齐）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 补充 {CAT}/{BAIJIU}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_baijiu_snow_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_baijiu_snow_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
