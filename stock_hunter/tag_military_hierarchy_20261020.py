"""军工板块二级概念层级化（2026-10-20，接续 tag_robot_military_fix_20261020.py）。

问题：45 条精确化修正后军工概念从 27 个涨到 64 个，其中 44 个只有 1 只票，
GUI 细分分组碎成一地。本脚本把长尾概念挂到大类前缀下（`大类-细分`），
GUI 分组逻辑 `concept.split("-")[0]` 会自动把它们归进大类组。

规则（保守）：只改写映射表中明确列出的 token；≥2 只票的独立概念保持不动；
军种标签（空军/海军/陆军/军贸）不动。

用法：python tag_military_hierarchy_20261020.py [--apply]
幂等。
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WL = ROOT / "watchlist_jiuyan.json"
BAK = ROOT / "watchlist_jiuyan.backup_2026-10-20_军工层级化.json"
APPLY = "--apply" in sys.argv

# token → 层级名。只列长尾（1 只）与可归并的；未列出的一律不动。
HIERARCHY = {
    # 军工电子大类
    "军用MLCC": "军工电子-军用MLCC",
    "军用连接器": "军工电子-军用连接器",
    "钽电容": "军工电子-钽电容",
    "T/R组件": "军工电子-T/R组件",
    "FPGA": "军工电子-FPGA",
    "特种集成电路": "军工电子-特种集成电路",
    "军工芯片": "军工电子-军工芯片",
    "电子对抗": "军工电子-电子对抗",
    "网络信息安全": "军工电子-网络信息安全",
    "军用通信": "军工电子-军用通信",
    "海军电子": "军工电子-海军电子",
    "水声电子": "军工电子-水声电子",
    "激光晶体": "军工电子-激光晶体",
    # 军工材料大类
    "钛材": "军工材料-钛材",
    "碳纤维": "军工材料-碳纤维",
    "隐身材料": "军工材料-隐身材料",
    "石英材料": "军工材料-石英材料",
    "金属3D打印": "军工材料-金属3D打印",
    # 导弹大类
    "精确制导": "导弹-精确制导",
    "弹药": "导弹-弹药",
    "导弹动力": "导弹-动力",
    "光学制导": "导弹-光学制导",
    "导弹配套": "导弹-配套",
    "制导控制": "导弹-制导控制",
    # 航空装备大类
    "航空锻件": "航空装备-航空锻件",
    "环形锻件": "航空装备-环形锻件",
    "机载系统": "航空装备-机载系统",
    "控制系统": "航空装备-航发控制系统",
    "战斗机主机厂": "航空装备-战斗机主机厂",
    "直升机主机厂": "航空装备-直升机主机厂",
    # 航天大类
    "航天配套": "航天-航天配套",
    "航天发动机": "航天-航天发动机",
    "航天科工": "航天-航天科工装备",
    "特种装备": "航天-特种装备",
    # 卫星大类
    "卫星-有效载荷": "卫星-有效载荷",
    "北斗导航": "卫星-北斗导航",
    "卫星导航": "卫星-卫星导航",
    "卫星应用": "卫星-卫星应用",
    # 海军大类
    "舰船电力推进": "海军-舰船电力推进",
    "电磁发射": "海军-电磁发射",
    "水下防务": "海军-水下防务",
    # 陆军大类
    "陆军装备配套": "陆军-装备配套",
}

changes = []


def norm_tokens(v):
    if v is None:
        return []
    src = v if isinstance(v, (list, tuple)) else str(v).split("|")
    return [str(x).strip() for x in src if str(x).strip()]


def dump_tokens(tokens):
    if not tokens:
        return ""
    return tokens[0] if len(tokens) == 1 else list(tokens)


def main():
    wl = json.loads(WL.read_text(encoding="utf-8"))
    n = 0
    for code, rec in wl.items():
        if not isinstance(rec, dict):
            continue
        keys = [f"jiuyan_concept{i}" for i in range(1, 10)] + ["jiuyan_concept"]
        cats = {f"jiuyan_concept{i}": str(rec.get(f"jiuyan_category{i}") or "").strip()
                for i in range(1, 10)}
        cats["jiuyan_concept"] = str(rec.get("jiuyan_category") or "").strip()
        touched = False
        for k in keys:
            if k not in rec or cats.get(k) != "军工":
                continue
            tokens = norm_tokens(rec.get(k))
            new = []
            for t in tokens:
                t2 = HIERARCHY.get(t, t)
                if t2 not in new:
                    new.append(t2)
            if new != tokens:
                changes.append((code, rec.get("name"), dump_tokens(tokens), dump_tokens(new)))
                rec[k] = dump_tokens(new)
                touched = True
        if touched:
            n += 1
    print(f"军工层级化改写 {n} 只 / {len(changes)} 处（{'真写' if APPLY else '干跑'}）")
    for c in changes:
        print(" ", c[0], c[1], "|", c[2], "→", c[3])
    if APPLY and n:
        shutil.copy2(WL, BAK)
        WL.write_text(json.dumps(wl, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"已写回（备份 {BAK.name}）")


if __name__ == "__main__":
    main()
