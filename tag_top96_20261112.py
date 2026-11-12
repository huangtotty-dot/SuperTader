# -*- coding: utf-8 -*-
"""2026-11-12 owner：「查询图片中的个股 看下在韭研概念是否有遗漏 逐一查询后补充上去」。

截图 = 96 只涨幅榜。核对结果：
  · 96 只全部在 watchlist；1 只名称差异：301660 粤芯半导-U（watchlist 实名=粤芯半导）。
  · **73 只已有韭研概念**（本次不动，只增不改）。
  · **23 只无任何概念**；按行业+主营补 19 只到现有韭研概念。
  · 4 只无对应概念、保持空白：600569 安阳钢铁（钢铁）、301049 超越科技（环保已取消）、
    603499 翔港科技（包装印刷）、603967 中创物流（货运代理）。

写前备份；幂等；每只票打 `jiuyan_top96_20261112` 审计标记。
运行：`python tag_top96_20261112.py [--apply]`
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
NOTE_KEY = "jiuyan_top96_20261112"

MAP = {
    "301618": ("长联科技", "化工", "涂料颜料", "水性印花材料/色浆"),
    "301357": ("北方长龙", "军工", "材料与零部件", "军用车辆内饰/复合材料"),
    "002205": ("国统股份", "地产", "地产基建", "PCCP输水管道"),
    "605222": ("起帆电缆", "电力", "电缆", "电线电缆"),
    "002628": ("成都路桥", "地产", "地产基建", "路桥施工"),
    "002693": ("双成药业", "医药", "化学制药", "多肽药物"),
    "301717": ("超纯应材", "半导体", "半导体材料-电子特气与湿化学品", "半导体超纯材料"),
    "301660": ("粤芯半导", "半导体", "晶圆制造与IDM", "12英寸晶圆代工"),
    "301133": ("金钟股份", "汽车", "零部件", "汽车零部件"),
    "300637": ("扬帆新材", "化工", "基础化工", "光引发剂/精细化工"),
    "002647": ("仁东控股", "金融", "多元金融", "多元金融"),
    "300300": ("海峡创新", "AI应用", "AI智慧城市", "智慧城市/软件"),
    "003013": ("地铁设计", "地产", "地产基建", "轨道交通设计"),
    "300629": ("新劲刚", "军工", "军工电子与元器件", "军工微波电路/电子"),
    "301165": ("锐捷网络", "超节点", "交换机", "网络设备/交换机"),
    "603268": ("松发股份", "军工", "舰船与海洋防务", "船舶及高端装备"),
    "605166": ("聚合顺", "化工", "化学纤维", "尼龙切片/化纤"),
    "002685": ("华东重机", "机器人", "智能装备与机床", "集装箱起重机/数控机床"),
    "603318": ("水发燃气", "化工", "天然气", "LNG/城镇燃气"),
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


def has_pair(pairs: list, cat: str, con: str) -> bool:
    return any(c == cat and con in _toks(cn) for c, cn in pairs)


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan, skip, bad = [], [], []

    for code, (name, cat, con, why) in MAP.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            bad.append((code, name, "不在 watchlist")); continue
        wl_name = str(e.get("name") or "")
        if wl_name != name:
            bad.append((code, name, f"名称不符 watchlist={wl_name}")); continue
        pairs = get_pairs(e)
        if has_pair(pairs, cat, con):
            skip.append(code); continue
        plan.append((code, name, cat, con, why, pairs))

    print(f"待补 {len(plan)} 只；已存在跳过 {len(skip)} 只；异常 {len(bad)}")
    for b in bad:
        print("  ⚠️", b)
    print("\n按类目：")
    for cat, n in Counter(p[2] for p in plan).most_common():
        print(f"  {cat}: {n}")
    print("\n明细：")
    for code, name, cat, con, why, before in plan:
        print(f"  {code} {name}: {before} -> +('{cat}','{con}')  [{why}]")

    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, cat, con, why, before in plan:
        e = wl[code]
        put_pairs(e, before + [(cat, con)], now)
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        rs[con] = f"{name}：{why}（2026-11-12 涨幅榜补充）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 涨幅榜补充 → {cat}/{con}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_top96_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_top96_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
