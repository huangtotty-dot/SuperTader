# -*- coding: utf-8 -*-
"""把同花顺「光刻机」「光刻胶」两个概念板块的个股补充到半导体一级分类（2026-11-17 owner）。

口径（沿用打标签惯例，**只增不改**）：
  · 只处理**没有**「半导体」分类的票：追加一对（半导体, 光刻机/光刻胶）；
    已有半导体分类的（22 + 19 只）**不动** —— 它们的子概念更细，覆盖它反而更差；
  · 两个板块都出现的 7 只（茂莱光学/国林科技/联合化学/思泰克/久日新材/华特气体/
    芯碁微装）概念写「光刻机|光刻胶」（沿用 concept 的 `|` 多值写法）；
  · 不在 watchlist 的（*ST和科 002816 / *ST京化 600889，ST 不入池）跳过并打印；
  · 改前备份 watchlist_concept.json → backup/。

名单来源：owner 提供的同花顺板块成分截图（光刻机 80 只 / 光刻胶 52 只）。
"""
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))

from daily_recap_ingest import get_pairs, put_pairs  # noqa: E402

WL = BASE / "stock_hunter" / "watchlist_concept.json"

GJK = """920489 301209 300786 300721 688502 002584 603078 603931 603324 300346
300905 000859 300576 301256 002409 300758 300236 300398 688720 920015
603650 300655 002453 688037 002643 920117 300054 300537 603155 603125
688727 300801 688181 603823 300429 920193 300200 688550 301036 688545
002559 688268 300522 000422 688019 003022 688757 688571 603120 688093
688179 688157 002455 300797 300497 605589 301568 603928 688602 688199
600587 688669 688630 301599 601208 688503 300700 300203 300446 300538
002768 300412 688056 300535 300806 688125 688378 603306 001358 300063""".split()

GJJ = """301209 300786 301421 688502 603297 600619 002889 300293 688376 920690
300666 688010 600895 301611 002549 300260 688605 300331 688146 688835
603005 300219 688401 300456 301717 003043 688127 688268 688409 603360
301095 301297 688307 600850 301568 688199 688079 688630 002008 300414
603738 002816 300809 603800 003031 603773 600889 301128 688195 300990
600353 688167""".split()


def main():
    wl = json.loads(WL.read_text(encoding="utf-8"))
    bak = BASE / "backup" / f"watchlist_concept_before_litho_{datetime.now():%Y%m%d_%H%M%S}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WL, bak)

    both = set(GJK) & set(GJJ)
    plan = {}
    for c in GJK:
        plan[c] = "光刻机|光刻胶" if c in both else "光刻机"
    for c in GJJ:
        if c not in plan:
            plan[c] = "光刻胶"

    added, skipped_has, skipped_missing = [], [], []
    for c, con in sorted(plan.items()):
        e = wl.get(c)
        if not isinstance(e, dict):
            skipped_missing.append(c)
            continue
        pairs = get_pairs(e)
        if any(g == "半导体" for g, _ in pairs):
            skipped_has.append(c)
            continue
        put_pairs(e, pairs + [("半导体", con)])
        added.append((c, e.get("name"), con))
    wl["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WL.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"新增半导体标签 {len(added)} 只：")
    for c, n, con in added:
        print(f"  {c} {n} → 半导体/{con}")
    print(f"已有半导体、按只增不改跳过 {len(skipped_has)} 只")
    print(f"不在 watchlist 跳过 {len(skipped_missing)} 只：{skipped_missing}")
    print(f"[backup] {bak}")


if __name__ == "__main__":
    main()
