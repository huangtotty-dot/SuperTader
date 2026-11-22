# -*- coding: utf-8 -*-
"""2026-11-18 涨停复盘 → 补齐概念板块（owner：「检查概念板块有无遗漏」）。

口径同 11-17 版：**只增不改**
  · ①分类存在但概念为空 → 只补概念；②分类不存在 → 末尾追加一对；
  · 词表沿用现有标签（大消费_小家电 / 大消费_服装）。

对账后仍不动的（留报告）：002194 武汉凡谷（射频器件，现有分类无合适归属）、
000711 京蓝科技（7N 高纯铝/ITO 靶材，归类待 owner 定）。
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
    "600336": ("大消费", "大消费_小家电"),   # 澳柯玛：制冷家电（apply 已建了空概念的大消费）
}
APPEND = {
    "600839": ("大消费", "大消费_小家电"),   # 四川长虹：家电出口主线（复盘归「家电」）
    "002042": ("大消费", "大消费_服装"),     # 华孚时尚：色纺纱/纺织服装
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
