# -*- coding: utf-8 -*-
"""把 603838 四通股份 归到「算力金属 / 锆」（2026-10-23 owner 选 A）。

## 为什么是「算力金属 / 锆」

* 公司画像：广东四通集团，2026-10-20 **摘星脱帽**（原 *ST四通）。三块业务 =
  家居生活陶瓷（日用 26% + 卫生 9%）、陶瓷色釉料（40.9%）、**锆钛矿精炼**
  （子公司大连壹唯：锆英砂 / 锆中矿 / 钛精矿 / 独居石 / 金红石；2025 矿产品收入
  1.20 亿、+201.9%）。
* 本仓**没有**「有色/小金属/稀土」大类，`算力金属` 就是那个资源桶 —— 里面全按**金属名**
  做细分：铜(16)、钨(7)、锡(7)、锑(5)、镓(5)、锗(3)、锂矿资源(3)…
  四通的增量故事正是锆钛矿 ⇒ 按同构惯例新建细分 **锆**（此前不存在）。
* 备选（未采用）：`其他关键材料及设备`（丢辨识度）/ `大消费`（2026 的驱动是矿不是消费）。

## 为什么"打个标签就能进猎手池"

猎手 `modules/data_loader.py` **成对**读 `jiuyan_category` / `jiuyan_concept`，
`main.py:70` 再按「韭研概念非空」过滤。603838 原先是 `manual_add` 且两个字段全空
⇒ 扫不到。本脚本按铁律「**只增不改 · 全空才写主类目**」各写一次；已有任何槽位就不写。

用法：``python stock_hunter/tag_603838_zircon_20261023.py [--apply]``
"""
import json
import shutil
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parents[1]
SRC = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BAK = SRC.with_suffix(".json.bak_20261023_603838")

CODE = "603838"
CATEGORY = "算力金属"
CONCEPT = "锆"


def _has_any(rec, kind):
    if str(rec.get(kind) or "").strip():
        return True
    return any(str(rec.get(f"{kind}{i}") or "").strip() for i in range(1, 10))


def _pool_size(data):
    """猎手池口径：category 与 concept 都非空（= loader 真的产出了一对 cat/con）。"""
    return sum(1 for r in data.values()
               if isinstance(r, dict) and _has_any(r, "jiuyan_category")
               and _has_any(r, "jiuyan_concept"))


def _members(data, cat, con):
    out = []
    for c, rec in data.items():
        if not isinstance(rec, dict):
            continue
        cats, cons = [], []
        for k in ["jiuyan_category"] + [f"jiuyan_category{i}" for i in range(1, 10)]:
            v = str(rec.get(k) or "").strip()
            if v:
                cats += [x.strip() for x in v.split("|") if x.strip()]
        for k in ["jiuyan_concept"] + [f"jiuyan_concept{i}" for i in range(1, 10)]:
            v = str(rec.get(k) or "").strip()
            if v:
                cons += [x.strip() for x in v.split("|") if x.strip()]
        if cat in cats and con in cons:
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
    for k in ("name", "jiuyan_category", "jiuyan_concept", "sector", "primary_source"):
        print(f"  {k:18s} = {rec.get(k)!r}")

    if _has_any(rec, "jiuyan_category") or _has_any(rec, "jiuyan_concept"):
        print("\n⛔ 该票已有 category/concept 槽位 ⇒ 按「只增不改」不动它。")
        return 2

    print(f"\n将写入：jiuyan_category = {CATEGORY!r} · jiuyan_concept = {CONCEPT!r}")
    print(f"{CONCEPT} 现有成员 {len(_members(data, CATEGORY, CONCEPT))} 只（本次为**新建细分**）")

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
    print(f"{CATEGORY}/{CONCEPT} 成员：{len(_members(json.loads(SRC.read_text(encoding='utf-8')), CATEGORY, CONCEPT))} 只")
    return 0


if __name__ == "__main__":
    sys.exit(main())
