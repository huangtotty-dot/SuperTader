# -*- coding: utf-8 -*-
"""2026-11-13 owner：「根据图片中的涨停个股更新主线广度，并核对韭研概念是否遗漏，如有补充」。

截图 = 韭研公社 11.13 全天涨停复盘简图（37 只涨停，7 个板块）。

核对结果（`scripts/daily_recap_ingest.py --dry-run` 出的 20 条）：
  · **37 只全部在 watchlist**，名称零差异。
  · 已有概念且与本板块一致 17 只（不动）。
  · **补 17 只**（只增不改）：
      - 11 只原本无任何概念（不在猎手池）→ 按主营/涨停原因新建；
      - 6 只已有别的类目、本板块缺失 → 追加一对（原类目顺序不变）。
  · **3 只刻意不动**：
      - 002853 皮阿诺（家具制造，蹭「初芯系接任董事长」，无匹配概念 ⇒ 保持空白，与 11-12 惯例一致）
      - 605255 天普股份（已有『超节点』，AI 角度已覆盖）
      - 603286 日盈电子（已有『物理AI』，机器人角度已覆盖）

写前备份；幂等（已存在则跳过）；每只打 `jiuyan_recap_20261113` 审计标记。
运行：`python tag_recap_20261113.py [--apply]`
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
NOTE_KEY = "jiuyan_recap_20261113"

# code: (名称, 韭研一级类目, 二级概念, 依据)
MAP = {
    # ── 原本无任何概念（不在猎手池） ──
    "603103": ("横店影视", "AI应用", "AI影视短剧", "影视投资/制作/发行/院线"),
    "002739": ("儒意电影", "AI应用", "AI影视短剧", "电影制片+发行"),
    "600977": ("中国电影", "AI应用", "AI影视短剧", "电影发行放映/《流浪地球3》出品"),
    "600651": ("飞乐音响", "半导体", "封装测试", "芯片测试（仪电系）"),
    "603686": ("福龙马", "机器人", "本体与整机", "环卫装备整机/环卫机器人"),
    "605318": ("法狮龙", "算力租赁", "算力租赁", "算力租赁题材（主业集成吊顶）"),
    "002819": ("东方中科", "半导体", "半导体设备-量测与检测", "网络分析仪(VNA)/电子测量仪器"),
    "002748": ("世龙实业", "化工", "基础化工", "氯化亚砜/精细化工"),
    "000766": ("通化金马", "医药", "化学制药", "阿尔茨海默创新药（化学制药）"),
    "001393": ("维通利", "电力", "电力电子与配套", "电连接器/母排，下游新能源车与电力"),
    # ── 已有别的类目 → 追加本板块 ──
    "000665": ("湖北广电", "AI应用", "AI影视短剧", "短剧（长江云）；已有算力租赁"),
    "001358": ("兴欣新材", "半导体", "半导体材料-光刻胶与配套试剂", "光刻胶材料；已有化工"),
    "603928": ("兴业股份", "半导体", "半导体材料-光刻胶与配套试剂", "光刻胶用树脂；已有化工"),
    "002685": ("华东重机", "半导体", "芯片设计-CPU/GPU/AI计算", "控股国产GPU公司；已有机器人"),
    "603767": ("中马传动", "机器人", "核心零部件-传动", "车载/机器人减速器；已有大农业"),
    "600410": ("华胜天成", "算力租赁", "算力租赁", "算力/智算中心；已有云计算|超节点"),
    "600467": ("好当家", "大消费", "大消费_预制菜", "水产品/预制菜；已有大农业"),
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
    """重写 1..9 对 —— **顺序保持不变**（只追加），别让 cats[0] 翻掉。"""
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
            bad.append((code, name, "不在 watchlist"))
            continue
        wl_name = str(e.get("name") or "")
        if wl_name != name:
            bad.append((code, name, f"名称不符 watchlist={wl_name}"))
            continue
        pairs = get_pairs(e)
        if has_pair(pairs, cat, con):
            skip.append(code)
            continue
        plan.append((code, name, cat, con, why, pairs))

    print(f"待补 {len(plan)} 只；已存在跳过 {len(skip)} 只；异常 {len(bad)}")
    for b in bad:
        print("  ⚠️", b)
    print("\n按类目：")
    for cat, n in Counter(p[2] for p in plan).most_common():
        print(f"  {cat}: {n}")
    print("\n明细：")
    for code, name, cat, con, why, before in plan:
        kind = "新建" if not before else "追加"
        print(f"  {code} {name}: {before} -> +('{cat}','{con}')  [{kind}｜{why}]")

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
        rs[con] = f"{name}：{why}（2026-11-13 涨停复盘补充）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 涨停复盘补充 → {cat}/{con}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_recap_20261113_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_recap_20261113_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
