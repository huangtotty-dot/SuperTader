# -*- coding: utf-8 -*-
"""2026-11-09 owner：「军工的分类也很凌乱，请继续优化」。

## 现状（核实）
军工 91 条成员散在 **31 个细分**，且 **50 只票跨 2~3 个细分**（振华科技同时挂
电源/芯片/被动元件；高德红外挂弹药+红外…），分组页一眼看过去全是重叠。

## 重新整合（31 → 7，按产业链环节）
  ① **航空装备**：整机（固定翼/直升机）、发动机与燃机、机载/航电、结构件与锻铸件、地面保障
  ② **航天与导弹**：航天装备/分系统、导弹与精确制导、弹药与特种装备
  ③ **舰船与海洋防务**：船舶总装、舰船动力与推进、水声与海洋电子
  ④ **军工电子与元器件**：军工芯片/特种 IC、被动元件与连接器、电源与电机电气
  ⑤ **军工信息化与感知**：通信与指挥控制、导航与惯导、雷达预警、红外光电与激光、
     电子对抗、军事仿真与软件
  ⑥ **材料与零部件**：钛合金/高温合金、碳纤维复合材料、超材料与隐身、特种玻璃石英、
     其他关键材料、通用零部件
  ⑦ **地面装备与无人系统**：地面武器与装甲、枪械单兵、核化生防御、军用无人装备与机器人

一只票**只归一个分组**：优先按"平台/总装"口径（航空>航天>舰船>地面>信息化>电子>材料），
再按主营做个股覆盖（高德红外/睿创微纳→信息化、航天电子/国博/振芯→电子元器件、
北斗星通/国睿/四创→信息化、天海防务/中国海防/中科海讯→舰船…）。

写前备份；幂等；每只票打 `jiuyan_mil_reagg_20261109` 审计标记。
运行：`python tag_reagg_mil_20261109.py [--apply]`
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
NOTE_KEY = "jiuyan_mil_reagg_20261109"
CAT = "军工"

G_AIR = "航空装备"
G_SPACE = "航天与导弹"
G_SEA = "舰船与海洋防务"
G_ELEC = "军工电子与元器件"
G_INFO = "军工信息化与感知"
G_MAT = "材料与零部件"
G_GROUND = "地面装备与无人系统"

MAP = {
    "航空整机-固定翼": G_AIR, "航空整机-直升机": G_AIR,
    "航空发动机与燃气轮机": G_AIR, "航空机载系统与航电": G_AIR,
    "航空结构件与锻铸件": G_AIR, "航空地面保障装备": G_AIR,
    "航天装备与分系统": G_SPACE, "导弹与精确制导": G_SPACE, "弹药与特种装备": G_SPACE,
    "船舶总装与海洋防务": G_SEA, "舰船动力与推进": G_SEA, "水声与海洋电子": G_SEA,
    "军工芯片与特种集成电路": G_ELEC, "军工被动元件与连接器": G_ELEC,
    "军工电源与电机电气": G_ELEC,
    "军用通信与指挥控制": G_INFO, "军用导航与惯导": G_INFO, "雷达与预警探测": G_INFO,
    "红外光电与激光": G_INFO, "电子对抗与蓝军仿真": G_INFO, "军事仿真与军工软件": G_INFO,
    "军工材料-钛合金与高温合金": G_MAT, "军工材料-其他关键材料": G_MAT,
    "军工材料-超材料与隐身材料": G_MAT, "军工材料-碳纤维与复合材料": G_MAT,
    "军工材料-特种玻璃与石英": G_MAT, "军工配套-通用零部件": G_MAT,
    "地面武器与装甲车辆": G_GROUND, "枪械与单兵装备": G_GROUND,
    "核化生防御与应急救援": G_GROUND, "军用无人装备与机器人": G_GROUND,
}
PRIORITY = [G_AIR, G_SPACE, G_SEA, G_GROUND, G_INFO, G_ELEC, G_MAT]
GROUPS = set(PRIORITY)
# 跨细分票按主营覆盖
OVERRIDE = {
    "002414": G_INFO,    # 高德红外：红外（不是弹药）
    "688002": G_INFO,    # 睿创微纳：红外探测器
    "600184": G_INFO,    # 光电股份：光电/制导
    "600879": G_ELEC,    # 航天电子：航天电子/测控/连接器
    "688375": G_ELEC,    # 国博电子：T/R 组件
    "300101": G_ELEC,    # 振芯科技：北斗芯片
    "002151": G_INFO,    # 北斗星通：导航
    "600562": G_INFO,    # 国睿科技：雷达
    "600990": G_INFO,    # 四创电子：雷达
    "002465": G_INFO,    # 海格通信：通信/导航
    "600435": G_INFO,    # 北方导航：导航/制导
    "688011": G_INFO,    # 新光光电：光电
    "301302": G_INFO,    # 华如科技：仿真
    "000547": G_INFO,    # 航天发展：电子对抗
    "300581": G_AIR,     # 晨曦航空：机载
    "600372": G_AIR,     # 中航机载：机载系统
    "688333": G_AIR,     # 铂力特：航空/航天增材（按航空口径）
    "300008": G_SEA,     # 天海防务：船舶
    "600764": G_SEA,     # 中国海防：水声
    "300810": G_SEA,     # 中科海讯：水声
    "002519": G_GROUND,  # 银河电子：装甲车辆电源/地面
    "600967": G_GROUND,  # 内蒙一机：装甲车辆
    "000576": G_SPACE,   # 甘化科工：弹药为主
    "600590": G_ELEC,    # 泰豪科技：军工电源（不是通信）
    "000561": G_INFO,    # 烽火电子：军用无线通信（不是航空机载）
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
    plan, unknown = [], []
    for code, e in wl.items():
        if not isinstance(e, dict):
            continue
        pairs = get_pairs(e)
        mp = [(c, cn) for c, cn in pairs if c == CAT]
        if not mp:
            continue
        toks = []
        for _, cn in mp:
            toks.extend(_toks(cn))
        grp = OVERRIDE.get(code)
        if not grp:
            mapped = {MAP.get(t) for t in toks if MAP.get(t)}
            if not mapped:
                # 幂等：细分已经是新分组名（重跑）⇒ 跳过，绝不把新分组当成未知名再聚合
                if toks and all(t in GROUPS for t in toks):
                    continue
                unknown.append((code, e.get("name"), toks))
                continue
            grp = next(g for g in PRIORITY if g in mapped)
        else:
            # 覆盖项在重跑时同样跳过（原细分已被覆盖成新分组名）
            if toks and all(t in GROUPS for t in toks):
                continue
        plan.append((code, e.get("name"), grp, toks, pairs))
    dist = Counter(p[2] for p in plan)
    print(f"军工 {len(plan)} 只 → 新分组：")
    for k, v in dist.most_common():
        print(f"  {k}: {v}")
    if unknown:
        print("未匹配旧名：", unknown[:5])
    multi = [p for p in plan if len({MAP.get(t) for t in p[3] if MAP.get(t)}) > 1]
    print(f"\n跨细分票 {len(multi)} 只（已按覆盖/优先级取一个），抽查：")
    for p in multi[:10]:
        print(f"  {p[0]} {p[1]}: {' / '.join(dict.fromkeys(p[3]))[:46]} → {p[2]}")
    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, grp, toks, pairs in plan:
        e = wl[code]
        # ⚠️ 军工槽必须原地替换：cats[0] 是 OSC 选股输入，把它挪到末尾会让
        # 25 只票的主板块从「军工」变成商业航天/低空经济…（实测）
        out, done = [], False
        for c, cn in pairs:
            if c != CAT:
                out.append((c, cn))
            elif not done:
                out.append((CAT, grp))
                done = True
        put_pairs(e, out, now)
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        for t in toks:
            rs.pop(t, None)
        if toks:
            rs[grp] = f"{name}：{' / '.join(dict.fromkeys(toks))[:52]}（{grp}）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 军工聚合 → {grp}"
        changed += 1
    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_mil_reagg_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_mil_reagg_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
