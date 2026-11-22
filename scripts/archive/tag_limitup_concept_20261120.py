# -*- coding: utf-8 -*-
"""2026-11-20 涨停复盘 → 补齐概念板块（owner：「检查概念板块有无遗漏的个股」）。

口径同 11-17/18/19：**只增不改**（补空概念 / 追加一对，已有分类不挪位）。

仍不动的（留报告）：
  风语筑（现 AI应用/物理AI，复盘归「机器人」但 AI应用口径也对）、
  凡拓数创（数据要素，机器人只是叙事标签）、德龙汇能（天然气主业）、
  荣联科技（公告：存储集采/阿里云，数据要素已有）。
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
    "002199": ("超节点", "其他"),                    # 东晶电子：石英晶体频率器件（AI 服务器时钟）
    "300812": ("半导体", "半导体设备-其他专用设备"),  # 易天股份：先进封装/光电显示设备
    "002749": ("大农业", "农药"),                    # 国光股份：植物生长调节剂
}
APPEND = {
    # AI 硬件（PCB 链）：与 11-19 中京电子同口径，挂到超节点/PCB
    "002741": ("超节点", "PCB"),
    "000823": ("超节点", "PCB"),
    "605058": ("超节点", "PCB"),
    "301132": ("超节点", "PCB"),
    "603386": ("超节点", "PCB"),
    "603004": ("半导体", "半导体材料-光刻胶与配套试剂"),  # 鼎龙科技：光刻胶+键绳材料
    "603948": ("半导体", "半导体材料-电子特气与湿化学品"),  # 建业股份：电子级异丙醇/三乙胺
    "605018": ("机器人", "核心零部件-传动"),                # 长华集团：人形机器人滚珠丝杠
    "603960": ("机器人", "智能装备与机床"),                # 克来机电：汽车电子装备/机器人
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
