# -*- coding: utf-8 -*-
"""2026-11-11 owner：「光通信的分类不够简洁 请重新进行分类」。

## 现状（核实）
光通信 **52 只**，细分 **20 个**（光缆与线缆/光纤预制棒与光纤/光模块-数通与AI算力/
光模块-电信与传输/CPO与硅光集成/光器件-无源器件/光器件-有源器件/MPO\MTP与高密度连接/
光连接器与陶瓷插芯/光芯片-激光器与探测器/光芯片-PLC与AWG/光通信材料-光学与晶体/
光通信材料-封装与胶材/光通信设备-测试与生产/光传输与接入设备/光网络系统与工程服务/
特种光纤与光纤环/光通信配套-温控器件/光通信配套-精密结构件/时钟晶振与频率器件），
且多数票跨 2~5 个细分，分组页全是重叠。

## 重新整合（20 → 5，按产业链环节）
  ① **光模块与CPO**：光模块（数通/AI算力、电信/传输）、CPO 与硅光集成
  ② **光芯片与光器件**：光芯片（激光器/探测器、PLC/AWG）、光器件（有源/无源）、
     光学与晶体材料（福晶/苏大维格/天通/云南锗业）
  ③ **光纤光缆**：光纤预制棒与光纤、光缆与线缆、特种光纤与光纤环
  ④ **光连接与配套**：光连接器与陶瓷插芯、MPO/MTP 与高密度连接、精密结构件、
     温控器件、封装与胶材、时钟晶振与频率器件
  ⑤ **设备与工程**：光通信设备-测试与生产、光传输与接入设备、光网络系统与工程服务

一只票**只归一个分组**：默认按 模块>芯片器件>光纤光缆>连接配套>设备工程 取一个，
再按主营做个股覆盖（长飞/亨通等光纤厂归光纤光缆、烽火/罗博特科归设备工程、
太辰光/蘅东光/华脉/吴通归连接配套、天孚/光库/长光华芯归芯片器件…）。

⚠️ 原地替换 光通信槽（不挪位置，保住 cats[0]）；其他类目不动；幂等。
写前备份；每只票打 `jiuyan_optic_reagg_20261111` 审计标记。
运行：`python tag_reagg_optic_20261111.py [--apply]`
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
NOTE_KEY = "jiuyan_optic_reagg_20261111"
CAT = "光通信"

G_MOD = "光模块与CPO"
G_CHIP = "光芯片与光器件"
G_FIBER = "光纤光缆"
G_CONN = "光连接与配套"
G_EQUIP = "设备与工程"
GROUPS = [G_MOD, G_CHIP, G_FIBER, G_CONN, G_EQUIP]

MAP = {
    "光模块-数通与AI算力": G_MOD, "光模块-电信与传输": G_MOD, "CPO与硅光集成": G_MOD,
    "光芯片-激光器与探测器": G_CHIP, "光芯片-PLC与AWG": G_CHIP,
    "光器件-无源器件": G_CHIP, "光器件-有源器件": G_CHIP,
    "光通信材料-光学与晶体": G_CHIP,
    "光纤预制棒与光纤": G_FIBER, "光缆与线缆": G_FIBER, "特种光纤与光纤环": G_FIBER,
    "光连接器与陶瓷插芯": G_CONN, "MPO/MTP与高密度连接": G_CONN,
    "光通信配套-精密结构件": G_CONN, "光通信配套-温控器件": G_CONN,
    "光通信材料-封装与胶材": G_CONN, "时钟晶振与频率器件": G_CONN,
    "光通信设备-测试与生产": G_EQUIP, "光传输与接入设备": G_EQUIP,
    "光网络系统与工程服务": G_EQUIP,
}
PRIORITY = [G_MOD, G_CHIP, G_FIBER, G_CONN, G_EQUIP]

# 跨细分票按主营覆盖（默认优先级会取错核心业务的）
OVERRIDE = {
    "601869": G_FIBER,   # 长飞光纤：光纤光缆（不是模块）
    "600498": G_EQUIP,   # 烽火通信：通信设备/传输
    "603042": G_CONN,    # 华脉科技：光配线/连接
    "300292": G_CONN,    # 吴通控股：连接器/MPO
    "300570": G_CONN,    # 太辰光：MPO/连接器
    "920045": G_CONN,    # 蘅东光：光连接器/MPO
    "300394": G_CHIP,    # 天孚通信：光器件
    "300620": G_CHIP,    # 光库科技：光器件
    "688048": G_CHIP,    # 长光华芯：光芯片
    "688025": G_EQUIP,   # 杰普特：激光/测试设备
    "300757": G_EQUIP,   # 罗博特科：CPO 耦合/测试设备
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
    plan, unknown, bad = [], [], []

    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        mp = [(i, cn) for i, (c, cn) in enumerate(pairs) if c == CAT]
        if not mp:
            continue
        toks = []
        for _, cn in mp:
            toks.extend(_toks(cn))
        toks = list(dict.fromkeys(toks))
        if toks and all(t in GROUPS for t in toks):
            continue                        # 幂等：已经是新分组
        grp = OVERRIDE.get(code)
        if not grp:
            mapped = {MAP.get(t) for t in toks if MAP.get(t)}
            if not mapped:
                unknown.append((code, e.get("name"), toks))
                continue
            grp = next(g for g in PRIORITY if g in mapped)
        # 可能有多条 光通信 槽（历史脏数据）：合并成第一条位置，其余删掉
        first_pos = mp[0][0]
        out, placed = [], False
        for i, (c, cn) in enumerate(pairs):
            if c != CAT:
                out.append((c, cn))
            elif not placed:
                out.append((CAT, grp))
                placed = True
        plan.append((code, e.get("name"), grp, toks, pairs, out))

    dist = Counter(p[2] for p in plan)
    print(f"光通信 {len(plan)} 只 → 新分组：")
    for g, n in dist.most_common():
        print(f"  {g}: {n}")
    if unknown:
        print("未匹配旧名（未改）：", unknown)
    print("\n明细：")
    for code, name, grp, toks, before, out in plan:
        print(f"  {code} {name}: {' / '.join(toks)[:52]} -> {grp}")

    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, grp, toks, before, out in plan:
        e = wl[code]
        put_pairs(e, out, now)
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        for t in toks:
            rs.pop(t, None)
        rs[grp] = f"{name}：{' / '.join(toks)[:52]}（{grp}）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 光通信聚合 → {grp}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_optic_reagg_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_optic_reagg_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
