# -*- coding: utf-8 -*-
"""2026-11-08 owner：「电力运营 和 智能电网再次核对一遍，尤其电力运营，个股理由进一步完善」。

## 核对（按公司简介逐只）
电力运营 64 只里发现 2 只明显不是「发电/供热运营」：
  · 002617 露笑科技：碳化硅衬底（第三代半导体材料）→ 电力/电力运营 ⇒ **半导体/半导体材料-硅片与硅部件**
  · 002256 兆新股份：精细化工（环保涂料/气雾剂）→ 电力/电力运营 ⇒ **化工/涂料颜料**
  · 002480 新筑股份：轨道交通车辆（含少量光热发电）——**没有合适去处，暂留并标注**（见 NOTES）
智能电网 34 只复核未发现新的明显错位（南网能源/芯能/未来电器/天正/智光/南网科技已在上一轮纠正）。

## 个股理由（这次的重点）
给 **电力运营 / 智能电网** 的每一只票写 `jiuyan_concept_reason[概念]`，格式：
    `<名称>：<主营一句话>（<类型>）`
类型按主营关键词判定，例如：
    长江电力：大型水电运营（发电运营）
    涪陵电力：电网运营业务（供电/电网运营）
    新中港：蒸汽产品（供热供能）
    国电南瑞：电网设备相关业务（电网自动化）
    炬华科技：能源物联网设备（智能计量）
    朗新科技：能源服务与运营的AI（能源管理）
理由会出现在：猎手概念 chip 悬停、公司资料「题材关联」一栏。

写前备份；幂等；只写这两类的理由与上述 2 只的归类，不动其它字段。
运行：`python tag_reasons_power_20261108.py [--apply]`
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
PROFILE = BASE / "t_io" / "cache" / "company_profile.json"
BACKUP = BASE / "backup"
NOTE_KEY = "jiuyan_power_reason_20261108"

FIX = {  # 本轮核对发现的两只错位（旧大类, 旧概念, 新大类, 新概念）
    "002617": ("电力", "电力运营", "半导体", "半导体材料-硅片与硅部件"),
    "002256": ("电力", "电力运营", "化工", "涂料颜料"),
}
NOTES = {
    "002480": "新筑股份：主营轨道交通车辆制造（含少量光热发电），暂留电力运营待定",
}
TARGETS = ("电力运营", "智能电网")
# 简介缺失/太泛的，手工给一句话（依据：11-06 复盘关键词 + 公开资料）
MANUAL_TITLE = {
    "000595": "新能源发电、储能电站并网，宁夏国资背景",
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
    cats = [c.strip() for c in norm(e.get("jiuyan_category")).split("|") if c.strip()]
    cons = [c.strip() for c in norm(e.get("jiuyan_concept")).split("|") if c.strip()]
    return [(c, cons[i] if i < len(cons) else "") for i, c in enumerate(cats)]


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


def _title(code: str, e: dict, pf: dict) -> str:
    if code in MANUAL_TITLE:
        return MANUAL_TITLE[code]
    p = pf.get(code) or {}
    t = str(p.get("brief_title") or "").strip()
    if not t:
        t = str(p.get("brief") or "").strip()[:26]
    if not t:
        t = str(e.get("business_summary") or "").strip()
    if not t or t.startswith("主营"):
        t = str(e.get("sector") or "").split("/")[0].strip()
    if len(t) > 24:
        t = t[:23] + "…"
    return t or "主营未收录"


def _kind(concept: str, text: str) -> str:
    t = text
    if concept == "电力运营":
        if any(k in t for k in ("水电", "火电", "风电", "光伏发电", "核电", "发电", "电力生产")):
            return "发电运营"
        if any(k in t for k in ("供热", "热电", "蒸汽", "热力", "供能", "燃气")):
            return "供热供能"
        if any(k in t for k in ("供电", "电网运营", "配电网")):
            return "供电/电网运营"
        if any(k in t for k in ("综合能源", "节能服务", "能源服务")):
            return "综合能源服务"
        return "电力运营"
    # 智能电网
    if any(k in t for k in ("电表", "计量", "用电信息", "能效管理")):
        return "智能计量/用电侧"
    if any(k in t for k in ("虚拟电厂", "能源管理", "能源服务", "功率预测", "数字化")):
        return "能源管理/虚拟电厂"
    if any(k in t for k in ("自动化", "二次", "调度", "变配电", "配用电")):
        return "电网自动化/二次设备"
    if any(k in t for k in ("软件", "信息化", "BIM", "数智", "物联网", "平台")):
        return "电力信息化"
    return "电网设备与服务"


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    pf = json.loads(PROFILE.read_text(encoding="utf-8")) if PROFILE.exists() else {}
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1) 两只错位
    moved = []
    for code, (fc, fcon, tc, tcon) in FIX.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        if (fc, fcon) not in pairs:
            print(f"  ! {code} {e.get('name')} 没有 {fc}/{fcon}，跳过")
            continue
        out = [(tc, tcon) if (c, p) == (fc, fcon) else (c, p) for c, p in pairs]
        moved.append((code, e.get("name"), f"{fc}/{fcon}", f"{tc}/{tcon}"))
        if apply:
            put_pairs(e, out, now)
            e["jiuyan_power_reclass_note_20261108"] = f"{now} {fc}/{fcon} → {tc}/{tcon}（按公司简介核实）"

    # 2) 理由
    reasons = []
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        cons = [con for c, con in get_pairs(e) if c == "电力" and con in TARGETS]
        for con in cons:
            title = _title(code, e, pf)
            kind = _kind(con, title + " " + str(e.get("sector") or ""))
            txt = f"{e.get('name') or code}：{title}（{kind}）"
            old = (e.get("jiuyan_concept_reason") or {}).get(con)
            if old == txt:
                continue
            reasons.append((code, e.get("name"), con, txt, old))
            if apply:
                rs = e.get("jiuyan_concept_reason")
                if not isinstance(rs, dict):
                    rs = {}
                rs[con] = txt
                e["jiuyan_concept_reason"] = rs
                e[NOTE_KEY] = now

    print(f"错位调整 {len(moved)} 只；理由更新 {len(reasons)} 条")
    for m in moved:
        print(f"  {m[0]} {m[1]}: {m[2]} → {m[3]}")
    for r in reasons[:8]:
        print(f"  {r[0]} {r[1]} [{r[2]}]: {r[3]}" + (f"   （原：{r[4]}）" if r[4] else ""))
    if len(reasons) > 8:
        print(f"  … 其余 {len(reasons)-8} 条")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    if moved or reasons:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_power_reasons_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"已落盘；备份 backup/watchlist_jiuyan_before_power_reasons_*.json")
    if NOTES:
        print("待定：", "；".join(NOTES.values()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
