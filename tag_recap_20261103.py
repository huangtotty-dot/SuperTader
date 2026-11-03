# -*- coding: utf-8 -*-
"""2026-11-03 owner：按「韭研公社午盘涨停复盘简图（11.03）」补韭研概念遗漏。

来源：`tmp/recap_20261103.json`（52 只涨停，9 个复盘板块）+ `scripts/daily_recap_ingest.py`
的对账报告。对账 25 条候选，去掉「板块缺失（已有标签、只是归类名不同）」后：

  · 真·漏票（无任何韭研标签）**15 只** → 本脚本全部补上；
  · 公告/其他里题材明确的按复盘关键词归到真实分类（与 10-30 / 11-02 的口径一致）。

口径（守 `jiuyan_tagging_convention`）：只增不改；已有一级分类的票跳过；
细分概念尽量复用现有词表（大消费_服装 / 大消费_文旅旅游 / 本体及应用 / 材料 /
其他关键材料及设备 / 电力设备 / 地产基建 / 固废治理），没有对应词才新起。

写前备份到 `backup/watchlist_jiuyan_before_recap_20261103.json`；脚本幂等。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"

ADD = {
    # ── 大消费 *15 ──
    "002029": ("大消费", "大消费_服装"),        # 七匹狼：服装家纺
    "600358": ("大消费", "大消费_文旅旅游"),    # 国旅联合：温泉/旅游
    "603402": ("大消费", "大消费_文旅旅游"),    # 陕西旅游：旅游
    "600630": ("大消费", "大消费_服装"),        # 龙头股份：纺织服装
    # ── 电池产业链 *10 ──
    "002058": ("电池", "其他关键材料及设备"),   # 紫竹高科：固态电池铝塑膜
    "605378": ("电池", "其他关键材料及设备"),   # 野马电池：锌锰电池
    "301513": ("电池", "其他关键材料及设备"),   # 尚水智能：锂电制浆/固态电池设备
    # ── 机器人 *4 ──
    "002105": ("机器人", "本体及应用"),         # 信隆健康：外骨骼机器人
    "600558": ("机器人", "材料"),               # 大西洋：焊接材料
    "001396": ("机器人", "本体及应用"),         # 誉帆科技：管网检测机器人
    # ── 军工 *2 ──
    "301311": ("军工", "军用无人装备与机器人"), # 昆船智能：中船系智能物流/AGV
    # ── 公告 *4（按关键词归到真实分类）──
    "603289": ("半导体", "半导体设备-其他专用设备"),  # 泰瑞机器：拟收购半导体公司
    "002692": ("电力", "电力设备"),                   # 远程股份：电缆
    # ── 其他 *6 ──
    "600802": ("地产", "地产基建"),             # 福建水泥：水泥
    "000151": ("环保", "固废治理"),             # 中成股份：三废处理
}


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
    lc = norm(e.get("jiuyan_category"))
    lp = norm(e.get("jiuyan_concept"))
    return [(t.strip(), lp) for t in lc.split("|") if t.strip()] if lc else []


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


def main() -> int:
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed, skipped = [], []

    for code, (cat, con) in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        cats = [c for c, _ in pairs]
        if cat in cats:
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, cats))

    if changed:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / "watchlist_jiuyan_before_recap_20261103.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"补齐 {len(changed)} 只；跳过 {len(skipped)} 只")
    for code, name, cat, con, cats in changed:
        print(f"  + {code} {name}: {cat}/{con}   （原: {'|'.join(cats) or '—'}）")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
