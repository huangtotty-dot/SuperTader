# -*- coding: utf-8 -*-
"""2026-11-17 涨停复盘 → 补齐概念板块（owner：「检查概念板块有无遗漏」）。

口径：**只增不改**
  · 已存在的一级分类不动、顺序不动（消费者取 cats[0]，挪位会改行为）；
  · 只做两种动作：①给「分类存在但概念为空」的补概念；②追加复盘板块对应的一对；
  · 分类名/概念名全部沿用现有词表（半导体材料-光刻胶与配套试剂 / 半导体设备-其他专用设备 /
    半导体分销与供应链 / 设备核心零部件 / 智能电网 / 智能装备与机床）。

依据：韭研公社 11-17 全天涨停复盘简图（tmp/recap_20261117.json）的「涨停关键词」+ sector 字段。
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

# ① 补空概念：分类已在，只把空 concept 填上（不改分类、不改位置）
FILL = {
    "600619": ("半导体", "半导体设备-其他专用设备"),   # 光刻机冷却系统（合作上海微电子）
    "002889": ("半导体", "半导体分销与供应链"),        # 光刻机寄售维修+物流
    "603297": ("半导体", "设备核心零部件"),            # 光刻机物理AI/激光雷达核心部件（光学件）
    "603030": ("半导体", "半导体设备-其他专用设备"),   # 光刻光源+AI服务器
    "920489": ("半导体", "半导体材料-光刻胶与配套试剂"),  # 光刻胶重要单体
    "002451": ("电力", "智能电网"),                    # 智能电网+变压器电磁线
}
# ② 追加一对（分类不存在时补到末尾，已有分类绝不重复/不挪位）
APPEND = {
    "002584": ("半导体", "半导体材料-光刻胶与配套试剂"),  # 光刻胶配套试剂+PCB化学试剂
    "002535": ("机器人", "智能装备与机床"),              # 机器人（机器视觉）+煤炭机械
    "002855": ("消费电子", "消费电子"),                  # Apple 链结构件+供货华为
}


def main():
    wl = json.loads(WL.read_text(encoding="utf-8"))
    bak = BASE / "backup" / f"watchlist_concept_before_limitup_tag_{datetime.now():%Y%m%d_%H%M%S}.json"
    bak.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(WL, bak)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log = []
    for code, (cat, con) in FILL.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            log.append(f"跳过 {code}：不在 watchlist")
            continue
        pairs = get_pairs(e)
        hit = [i for i, (c, p) in enumerate(pairs) if c == cat]
        if not hit:
            log.append(f"跳过 {code}：没有 {cat} 分类（不在本次口径内）")
            continue
        i = hit[0]
        if pairs[i][1].strip():
            log.append(f"跳过 {code}：{cat} 已有概念「{pairs[i][1]}」（只增不改）")
            continue
        pairs[i] = (cat, con)
        put_pairs(e, pairs)
        log.append(f"{code} {e.get('name')}：{cat} 概念补为「{con}」")
    for code, (cat, con) in APPEND.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            log.append(f"跳过 {code}：不在 watchlist")
            continue
        pairs = get_pairs(e)
        if any(c == cat for c, _ in pairs):
            log.append(f"跳过 {code}：已有 {cat} 分类")
            continue
        put_pairs(e, pairs + [(cat, con)])
        log.append(f"{code} {e.get('name')}：追加 {cat}/「{con}」")
    wl["updated_at"] = now
    WL.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(log))
    print(f"[backup] {bak}")


if __name__ == "__main__":
    main()
