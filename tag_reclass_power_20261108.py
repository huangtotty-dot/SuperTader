# -*- coding: utf-8 -*-
"""2026-11-08 owner：「韭研概念的电力板块，二级分类很多是重复，请仔细核实重新分类」。

## 现状（核实）
227 只电力股 / **18 个二级细分**；199 只单概念，24 只双概念，4 只三概念 ⇒ 同一只票在
不同细分下被重复计数（板块看起来比实际大）。重复主要来自三处：
  ① 「变压器（含SST）」与「固态变压器SST」——**名字里已经含 SST，却又单列一类**；
  ② 「智能电网-输变配电 / 用户侧 / 虚拟电厂」——三套口径互相重叠（积成、国电南瑞、四方、
     许继等同时挂在 2~3 个下）；
  ③ 「电力设备」是个大杂烩，和「输电设备（绝缘子）/检测设备/配套相关/AIDC相关」重叠。

## 重新分类（**一只票只归一个电力细分**，18 → 9）
  电力运营        发电/电网运营商
  电力建设        EPC / 施工 / 设计（中国电建、中国能建、永福、苏文）
  风电设备        风电整机与零部件（31 只，保持独立，不并入发电设备）
  发电设备        燃气轮机 + 火电灵活性改造 + SOFC（+ 其他发电侧设备）
  变压器/SST     原「变压器（含SST）」+「固态变压器SST」合并
  高压直流电源HVDC 数据中心/电力电子电源（保持独立）
  储能            储能系统与电芯集成
  智能电网        原「输变配电 / 用户侧 / 虚拟电厂」三合一（自动化、用电信息、能源管理）
  电力设备        线缆/铁塔/开关柜/绝缘子/检测/配套等其余一次设备

## 口径
· owner 明确要求「重新分类」⇒ 允许**改** concept（同 2026-10-19「移入某概念」例外）；
  大类始终是 `电力`，其它大类（电池/AI应用…）不动；
· `jiuyan_concept_reason` 的键跟随新概念迁移（原文保留）；
· 写前备份；脚本幂等；每只票打 `jiuyan_power_reclass_20261108` 审计标记。

运行：
  python tag_reclass_power_20261108.py            # 预览（不写）
  python tag_reclass_power_20261108.py --apply    # 落盘
"""
from __future__ import annotations

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
CAT = "电力"
NOTE_KEY = "jiuyan_power_reclass_20261108"

# 旧细分 → 新细分（未列出的旧名视为未知，脚本会报出来）
MERGE = {
    "电力运营": "电力运营",
    "电力建设": "电力建设",
    "风电设备": "风电设备",
    "燃气轮机": "发电设备",
    "火电灵活性改造": "发电设备",
    "SOFC": "发电设备",
    "检测设备": "电力设备",
    "变压器（含SST）": "变压器/SST",
    "固态变压器SST": "变压器/SST",
    "高压直流电源HVDC": "高压直流电源HVDC",
    "输电设备（绝缘子）": "电力设备",
    "配套相关": "电力设备",
    "AIDC相关": "电力设备",
    "电力设备": "电力设备",
    "智能电网-输变配电": "智能电网",
    "智能电网-用户侧": "智能电网",
    "智能电网-虚拟电厂": "智能电网",
    "储能": "储能",
}
ORDER = ["电力运营", "电力建设", "风电设备", "发电设备", "变压器/SST",
         "高压直流电源HVDC", "储能", "智能电网", "电力设备"]

# 多概念票：按主营指定唯一归属
OVERRIDE = {
    "000400": "智能电网",       # 许继电气（输变配电+用户侧）
    "000682": "智能电网",       # 东方电子（用户侧+虚拟电厂）
    "000875": "电力运营",       # 电投绿能（虚拟电厂+电力运营）
    "002090": "智能电网",       # 金智科技
    "002121": "智能电网",       # 科陆电子（用电信息/储能）
    "002339": "智能电网",       # 积成电子（三合一）
    "002364": "高压直流电源HVDC",  # 中恒电气（电力电源/SST）
    "300040": "电力设备",       # 九洲集团（成套设备）
    "300360": "智能电网",       # 炬华科技（智能电表）
    "300712": "电力建设",       # 永福股份（勘察设计/EPC）
    "300880": "智能电网",       # 迦南智能（电表）
    "300882": "智能电网",       # 万胜智能（电表）
    "300982": "电力建设",       # 苏文电能（电力 EPC）
    "600089": "变压器/SST",     # 特变电工（变压器）
    "600268": "智能电网",       # 国电南自
    "600312": "电力设备",       # 平高电气（开关）
    "600406": "智能电网",       # 国电南瑞
    "601126": "智能电网",       # 四方股份（二次设备）
    "601179": "变压器/SST",     # 中国西电
    "601222": "智能电网",       # 林洋能源（电表/用电）
    "601567": "智能电网",       # 三星电气（电表）
    "601868": "电力建设",       # 中国能建
    "601877": "电力设备",       # 正泰电器（低压电器）
    "603097": "变压器/SST",     # 江苏华辰（变压器）
    "603530": "电力设备",       # 神马电力（绝缘子）
    "603556": "智能电网",       # 海兴电力（电表出口）
    "605162": "电力运营",       # 新中港（热电联产）
    "688676": "变压器/SST",     # 金盘科技（变压器）
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


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan, unknown, skipped = [], [], 0
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        pcon = [con for c, con in pairs if c == CAT]
        if not pcon:
            continue
        if code in OVERRIDE:
            new = OVERRIDE[code]
        else:
            mapped = {MERGE.get(con, "") for con in pcon if con}
            unknown += [con for con in pcon if con and con not in MERGE]
            mapped.discard("")
            if len(mapped) != 1:
                plan.append((code, e.get("name"), pcon, None))   # 需人工
                continue
            new = mapped.pop()
        if new is None:
            continue
        if pcon == [new]:
            skipped += 1
            continue
        plan.append((code, e.get("name"), pcon, new))

    # 预览统计
    print(f"电力：待改 {sum(1 for p in plan if p[3])} 只；已是目标 {skipped} 只；"
          f"需人工 {sum(1 for p in plan if not p[3])} 只；未知旧名 {sorted(set(unknown))}")
    print("新分类预期分布（按计划）：")
    after = Counter(p[3] for p in plan if p[3])
    print("  ", {k: after.get(k, 0) for k in ORDER})
    for p in plan:
        if not p[3]:
            print("  ! 需人工:", p)

    if not apply:
        print("\n（预览）加 --apply 落盘")
        for p in plan[:15]:
            print(f"  {p[0]} {p[1]}: {'|'.join(p[2])} → {p[3]}")
        return 0

    changed = []
    for code, name, old_con, new in plan:
        if not new:
            continue
        e = wl[code]
        pairs = get_pairs(e)
        out, done = [], False
        for c, con in pairs:
            if c == CAT:
                if not done:
                    out.append((CAT, new))
                    done = True
                continue
            out.append((c, con))
        put_pairs(e, out, now)
        rs = e.get("jiuyan_concept_reason")
        if isinstance(rs, dict):
            kept = {}
            for k, v in rs.items():
                kept[new if k in old_con else k] = v
            e["jiuyan_concept_reason"] = kept
        e[NOTE_KEY] = f"{now} 电力二级细分重整：{'|'.join(old_con)} → {new}"
        changed.append((code, name, old_con, new))
    if changed:
        BACKUP.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_power_reclass_{stamp}.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)
    print(f"\n已改 {len(changed)} 只；备份 backup/watchlist_jiuyan_before_power_reclass_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
