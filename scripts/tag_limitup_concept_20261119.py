# -*- coding: utf-8 -*-
"""2026-11-19 涨停复盘 → 补齐概念板块（owner：「查询概念板块有无遗漏 如有请补充」）。

口径同 11-17/11-18：**只增不改**（已有分类不动、顺序不动；只补空概念或追加一对）。
词表沿用现有标签。

仍不动的（留报告，属公告/其他类或业务归属待定）：
  000656 金科股份（公告：回购+地产）、603813 原尚股份（汽车/氢能物流）。
改前自动备份 watchlist_concept.json → backup/。
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

FILL = {
    "002077": ("半导体", "封装测试"),   # 大港股份：子公司艾科半导体（封测）
    "301348": ("半导体", "封装测试"),   # 蓝箭电子：半导体封装测试
}
APPEND = {
    "000711": ("半导体", "半导体材料-靶材与蒸镀材料"),       # 京蓝科技：7N 高纯铝/ITO 靶材
    "002165": ("半导体", "半导体材料-光刻胶与配套试剂"),     # 红宝丽：光刻胶清洗剂/环氧丙烷
    "605178": ("半导体", "存储模组与系统"),                  # 时空科技：拟并购嘉合劲威（存储模组）
    "002418": ("超节点", "液冷散热"),                        # 康盛股份：液冷+千岛湖智造基地
    "002579": ("超节点", "PCB"),                             # 中京电子：PCB
    "002420": ("机器人", "结构件与材料"),                    # 毅昌科技：结构件+机器人
}


def main():
    wl = json.loads(WL.read_text(encoding="utf-8"))
    bak = BASE / "backup" / f"watchlist_concept_before_limitup_tag_{datetime.now():%Y%m%d_%H%M%S}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WL, bak)
    log = []
    for code, (cat, con) in FILL.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            log.append(f"跳过 {code}：不在 watchlist"); continue
        pairs = get_pairs(e)
        hit = [i for i, (c, p) in enumerate(pairs) if c == cat]
        if not hit:
            log.append(f"跳过 {code}：没有 {cat} 分类"); continue
        i = hit[0]
        if pairs[i][1].strip():
            log.append(f"跳过 {code}：{cat} 已有概念"); continue
        pairs[i] = (cat, con)
        put_pairs(e, pairs)
        log.append(f"{code} {e.get('name')}：{cat} 概念补为「{con}」")
    for code, (cat, con) in APPEND.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            log.append(f"跳过 {code}：不在 watchlist"); continue
        pairs = get_pairs(e)
        if any(c == cat for c, _ in pairs):
            log.append(f"跳过 {code}：已有 {cat} 分类"); continue
        put_pairs(e, pairs + [(cat, con)])
        log.append(f"{code} {e.get('name')}：追加 {cat}/「{con}」")
    wl["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    WL.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(log))
    print(f"[backup] {bak}")


if __name__ == "__main__":
    main()
