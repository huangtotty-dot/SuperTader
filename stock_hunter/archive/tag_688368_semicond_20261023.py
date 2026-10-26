# -*- coding: utf-8 -*-
"""把 688368 晶丰明源 归到「半导体 / 功率半导体」（2026-10-23 owner）。

owner：「查询下 688368 应该归类到韭研概念中的哪一个，归类之后要跑选股猎手可以找到它」。

## 为什么是「半导体 / 功率半导体」

* 公司画像（东财/同花顺口径）：上海晶丰明源半导体，**电源管理 + 驱动类芯片设计**（Fabless），
  申万行业 = 电子-半导体-模拟芯片设计；四条产品线 = LED 照明驱动、充电及电源管理、AC/DC、高性能
  计算电源（大电流 DC/DC，面向 AI 数据中心）。
* 本仓的韭研品类里**没有**「模拟芯片 / 电源管理芯片 / 汽车芯片」这些细分，最贴近的现成品类是
  **功率半导体**（13 只）——同板块里就有 **上海贝岭**（电源管理 + 模拟 IC）与 **士兰微**（含电源 IC），
  属于同一类"功率/电源类芯片设计"标的；放进 半导体材料/设备/存储/算力芯片 都明显不对。

## 为什么"打个标签就能进猎手池"

猎手 `modules/data_loader.py` 是**成对**读 `jiuyan_category` / `jiuyan_concept` 的：

    if cat and str(cat).strip():   # category 空 ⇒ 这一对整体不产出
        pairs.append((cat, concept))

而池子的口径是「韭研概念非空」。688368 现在是 **category 与 concept 全空** ⇒ 猎手根本不扫它。
本脚本按铁律「**只增不改 · 全空才写主类目**」把两个字段各写一次：已有任何 category 槽位就**不写**。

⚠️ 如果要让它同时出现在别的板块下，得按多类目对（cat2/con2）追加 —— 那是另一个决定，本脚本不做。

用法：``python stock_hunter/tag_688368_semicond_20261023.py``        只打印（dry-run）
      ``python stock_hunter/tag_688368_semicond_20261023.py --apply``  备份后写入
"""
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BAK = SRC.with_suffix(".json.bak_20261023_688368")

CODE = "688368"
CATEGORY = "半导体"
CONCEPT = "功率半导体"


def _slots(rec, kind):
    """[(字段名, 值)] —— 主字段 + 1..9 槽位，全部按字符串看。"""
    out = [(kind, str(rec.get(kind) or "").strip())]
    for i in range(1, 10):
        out.append((f"{kind}{i}", str(rec.get(f"{kind}{i}") or "").strip()))
    return out


def _has_any(rec, kind):
    return any(v for _, v in _slots(rec, kind))


def _pool_size(data):
    """猎手池口径：category 与 concept 都非空（= loader 真的产出了一对 cat/con）。"""
    n = 0
    for rec in data.values():
        if not isinstance(rec, dict):
            continue
        if _has_any(rec, "jiuyan_category") and _has_any(rec, "jiuyan_concept"):
            n += 1
    return n


def _members(data, cat, con):
    out = []
    for c, rec in data.items():
        if not isinstance(rec, dict):
            continue
        cons = str(rec.get("jiuyan_concept") or "")
        cats = str(rec.get("jiuyan_category") or "")
        for i in range(1, 10):
            cons = "|".join(x for x in (cons, str(rec.get(f"jiuyan_concept{i}") or "")) if x)
            cats = "|".join(x for x in (cats, str(rec.get(f"jiuyan_category{i}") or "")) if x)
        if con in [x.strip() for x in cons.split("|") if x.strip()] and \
                cat in [x.strip() for x in cats.split("|") if x.strip()]:
            out.append(f"{rec.get('name') or ''}({c})")
    return out


def main():
    apply = "--apply" in sys.argv
    data = json.loads(SRC.read_text(encoding="utf-8"))
    rec = data.get(CODE)
    if not isinstance(rec, dict):
        print(f"❌ {CODE} 不在 {SRC.name} 里")
        return 1

    before = _pool_size(data)
    print(f"猎手池（cat 与 con 都非空）：{before} 只")
    print(f"\n{CODE} 现状：")
    for k in ("name", "business_summary", "jiuyan_category", "jiuyan_concept", "sector"):
        print(f"  {k:18s} = {rec.get(k)!r}")

    if _has_any(rec, "jiuyan_category"):
        print("\n⛔ 该票已有 category 槽位 ⇒ 按「只增不改」不动它（要换板块得先明确移出哪一条）。")
        return 2
    if _has_any(rec, "jiuyan_concept"):
        print("\n⛔ 该票已有 concept 槽位但无 category —— 这是「下沉」场景，本脚本不处理，请人工确认。")
        return 2

    print(f"\n将写入：jiuyan_category = {CATEGORY!r} · jiuyan_concept = {CONCEPT!r}")
    members = _members(data, CATEGORY, CONCEPT)
    print(f"{CONCEPT} 现有成员 {len(members)} 只：" + "、".join(members))

    if not apply:
        print("\n（dry-run）加 --apply 才会写入。")
        return 0

    shutil.copy2(SRC, BAK)
    rec["jiuyan_category"] = CATEGORY
    rec["jiuyan_concept"] = CONCEPT
    tmp = SRC.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(SRC)

    after = _pool_size(json.loads(SRC.read_text(encoding="utf-8")))
    print(f"\n✅ 已写入（备份 {BAK.name}）")
    print(f"猎手池：{before} → {after} 只")
    print(f"{CONCEPT} 成员数：{len(members)} → {len(members) + 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
