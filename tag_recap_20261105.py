# -*- coding: utf-8 -*-
"""2026-11-05 owner：按「韭研公社午盘涨停复盘简图（11.05）」补韭研概念遗漏。

来源：`tmp/recap_20261105.json`（61 只涨停，11 个复盘板块）。
逐只对账后：**真·漏票（无任何韭研标签）10 只**；另有 20 只「归类与复盘板块不同」
（如 闽发铝业 现 有色金属、复盘归 光伏）——按既有惯例**只提示、不改**（多题材，不是缺失）。

口径（守 `jiuyan_tagging_convention`）：只增不改；细分概念优先复用现有词表；
「其他」段按个股真实题材归类。写前备份；脚本幂等。
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
    # ── 光伏 3 ──
    "002135": ("光伏", "光伏建筑一体化"),          # 东南网架：BIPV/光伏建筑一体化
    "603098": ("光伏", "光储充"),                  # 森特股份：BIPV+光储充
    "920007": ("光伏", "光伏支架"),                # 酉立智能：光伏跟踪支架
    # ── 半导体 2 ──
    "001269": ("半导体", "半导体材料-硅片与硅部件"),  # 欧晶科技：石英坩埚
    "301209": ("半导体", "半导体材料-光刻胶与配套试剂"),  # 联合化学：光刻胶/光刻机
    # ── 机器人 1 ──
    "603990": ("机器人", "本体及应用"),              # 麦迪科技：康养机器人
    # ── 端侧AI 1（按真实题材归消费电子：AI眼镜/声学）──
    "002045": ("消费电子", "AI眼镜"),                # 国光电器：AI眼镜+AI手机+机器人声学
    # ── 其他 3（按真实题材）──
    "002307": ("地产", "地产基建"),                  # 北新路桥：基建/桥梁工程
    "002318": ("军工", "军工材料-其他关键材料"),     # 久立特材：核电/大飞机用不锈钢管
    "300049": ("医药", "医疗服务"),                  # 福瑞医科：肝纤维化治疗/医疗服务
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
        if any(c == cat for c, _ in pairs):
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, pairs))
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_recap_20261105_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"补齐 {len(changed)} 只；跳过 {len(skipped)} 只")
    for code, name, cat, con, old in changed:
        print(f"  + {code} {name}: {cat}/{con}（原: {'|'.join(c for c,_ in old) or '—'}）")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
