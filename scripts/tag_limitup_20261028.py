# -*- coding: utf-8 -*-
"""按 owner 的 2026-10-28 午盘涨停复盘简图，给涨停个股补韭研概念标签。

**铁律：只增不改**（见 memory `jiuyan_tagging_convention`）
  · 任何槽位已经有的值**一律不动**；
  · 主字段 (`jiuyan_category` / `jiuyan_concept`) 全空才写主字段，否则写到
    **最后一个非空槽之后**的空槽，且 `jiuyan_categoryN` / `jiuyan_conceptN` **必须成对写**
    （只写半边会让这只票整个掉出猎手池 —— lowalt 那次伤过 8 只）；
  · 只补「今天涨停的关键词里有、而韭研标签里没有」的题材；纯业绩/股权转让这类
    **非题材**不写（与 MERGES 把 公告/其他 视为非题材的惯例一致）。

运行：python scripts/tag_limitup_20261028.py            # 预演，只打印
      python scripts/tag_limitup_20261028.py --apply    # 落盘
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
FP = BASE / "stock_hunter" / "watchlist_jiuyan.json"
NOTE = "owner 2026-10-28 午盘涨停复盘简图"

# ⚠️ **刻意不补**（2026-10-27 的清洗审计已按「仅研发/送样、无量产收入」摘除，
#    今天再加回来等于推翻前一天的结论。图上它们确实归在机器人/物理AI，需 owner 拍板）：
#    · 002931 锋龙股份（割草机器人=参股优必选）
#    · 002850 科达利（谐波减速器仍处样品验证）
#    · 688585 上纬新材（物理AI=智元取得控制权，非自身业务）

# (代码, 名称, 大类, 细分, 图上依据)
ADD = [
    # ---------- 完全空白（任何槽位都没有标签）----------
    ("603037", "凯众股份", "机器人", "结构件", "机器人+聚氨酯材料"),
    ("002526", "山东矿机", "机器人", "自动化集成", "焊接机器人+智慧矿山"),
    ("301488", "豪恩汽电", "机器人", "传感器", "机器人+智能驾驶(超声波雷达)"),
    ("003028", "振邦智能", "机器人", "智能控制", "人形机器人(深圳)+小米汽车"),
    ("605288", "凯迪股份", "机器人", "结构件", "机器人零部件+智能医疗"),
    ("603992", "松霖科技", "机器人", "本体及应用", "机器人+养老"),
    ("603633", "徕木股份", "机器人", "结构件", "机器人+铜缆连接器"),
    ("920896", "旺成科技", "机器人", "结构件", "人形机器人+汽车传动零部件"),
    ("603109", "神驰机电", "机器人", "电机", "机器人+柴油发电机"),
    ("603920", "世运电路", "机器人", "PCB", "机器人PCB+AI服务器PCB"),
    ("002048", "宁波华翔", "机器人", "材料", "机器人+智元+PEKK材料"),
    ("603015", "弘讯科技", "机器人", "智能控制", "机器人+MCU芯片"),
    ("300780", "德恩精工", "机器人", "本体及应用", "关节机器人+商业航天"),
    ("688045", "必易微", "机器人", "芯片", "机器人+GPU散热+模拟芯片"),
    ("605068", "明新旭腾", "机器人", "电子皮肤", "人形机器人+电子皮肤"),
    ("301076", "新瀚新材", "机器人", "材料", "机器人+DFBP(PEKK上游)"),
    ("002283", "天润工业", "机器人", "本体及应用", "工业机器人+燃气发动机"),
    ("000410", "沈阳机床", "机器人", "加工设备", "机器人+工业母机"),
    ("001400", "江顺科技", "物理AI", "物理AI_具身本体", "具身智能大小脑"),
    ("002611", "东方精工", "物理AI", "物理AI_具身本体", "物理AI+人形机器人"),
    ("688039", "当虹科技", "物理AI", "物理AI_数据采集", "数据采集+AI视频"),
    ("001339", "智微智能", "物理AI", "物理AI_数据采集", "物理AI(数据采集)+智元"),
    ("603226", "菲林格尔", "PCB", "PCB设备", "PCB设备+智能家居"),
    ("002741", "光华科技", "PCB", "PCB化学品-电镀与湿制程", "PCB药水+固态电池"),
    ("603006", "联明股份", "机器人", "结构件", "间接投资机器人+特斯拉"),
    ("603897", "长城科技", "机器人", "线束", "人形机器人线束+电磁线"),
    ("301359", "东南电子", "消费电子", "微动开关", "微动开关+新能源汽车"),
    ("603868", "飞科电器", "大消费", "大消费_个护电器", "个人护理电器"),
    ("603219", "富佳股份", "机器人", "本体及应用", "粮仓机器人+人乐聚机器人"),
    ("603612", "索通发展", "电池", "固态电解质", "固态电解质+预焙阳极"),
    ("000670", "盈方微", "半导体", "芯片分销", "存储芯片+芯片分销"),
    ("001296", "长江材料", "机器人", "材料", "3D打印砂+石英矿+人形机器人"),

    # ---------- 已有标签但与今天题材不符 → 追加一槽（不动原值）----------
    ("605333", "沪光股份", "机器人", "线束", "机器人+赛力斯供应商"),
    ("600173", "卧龙新能", "机器人", "电机", "机器人+风电+储能"),
    ("603626", "科森科技", "机器人", "结构件", "TSL机器人+折叠屏+固态电池"),
    ("601956", "东贝集团", "机器人", "结构件", "人形机器人+制冷压缩机"),
    ("002915", "中欣氟材", "机器人", "材料", "机器人+DFBP(PEKK上游)"),
    ("002265", "建设工业", "机器人", "本体及应用", "机器人+军工"),
    ("603179", "新泉股份", "机器人", "结构件", "机器人+汽车饰件"),
    ("301529", "福赛科技", "机器人", "材料", "机器人+PEKK注塑"),
    ("301119", "正强股份", "机器人", "结构件", "机器人+汽车转向系统"),
    ("603040", "新坐标", "机器人", "丝杠", "机器人+丝杠送样"),
    ("603926", "铁流股份", "机器人", "结构件", "机器人(特斯拉)+空心电机轴"),
    ("002962", "五方光电", "机器人", "机器视觉", "机器人视觉+手势识别"),
    ("300613", "富瀚微", "机器人", "芯片", "机器人+IPC芯片"),
    ("603699", "纽威股份", "液冷", "阀门", "液冷阀门+核电站门"),
]


def _slots(e: dict):
    """返回 (已有 cat 值集合, 已有 con 值集合, 最大已用槽位号)。"""
    cats, cons = [], []
    if e.get("jiuyan_category"):
        cats.append(str(e["jiuyan_category"]))
    if e.get("jiuyan_concept"):
        cons.append(str(e["jiuyan_concept"]))
    top = 0
    for i in range(1, 12):
        c, n = e.get(f"jiuyan_category{i}"), e.get(f"jiuyan_concept{i}")
        if c or n:
            top = max(top, i)
        if c:
            cats.append(str(c))
        if n:
            cons.append(str(n))
    return cats, cons, top


def main() -> int:
    apply_ = "--apply" in sys.argv
    raw = FP.read_text(encoding="utf-8")
    d = json.loads(raw)
    changed = skipped = 0
    rows = []
    for code, name, cat, con, why in ADD:
        e = d.get(code)
        if e is None:
            rows.append((code, name, "库中无此代码", "", ""))
            continue
        cats, cons, top = _slots(e)
        have_cat = any(cat in c.split("|") for c in cats)
        # 已有同一大类且细分已覆盖 → 跳过
        if have_cat and any(con in n for n in cons):
            skipped += 1
            rows.append((code, name, "已覆盖，跳过", f"{cat}|{con}", ""))
            continue
        if have_cat and top == 0 and not e.get("jiuyan_category"):
            # 主字段空但槽位 0 已用（不可能，兜底）
            pass
        if not cats and not cons:
            e["jiuyan_category"] = cat
            e["jiuyan_concept"] = con
            slot_txt = "主字段"
        else:
            i = top + 1
            e[f"jiuyan_category{i}"] = cat
            e[f"jiuyan_concept{i}"] = con
            slot_txt = f"槽位{i}"
        e["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        e["jiuyan_tag_source"] = NOTE
        changed += 1
        rows.append((code, name, slot_txt, f"{cat}|{con}", why))

    w = max(len(r[1]) for r in rows)
    print(f"{'代码':<7}{'名称':<{w+2}}{'写入位置':<9}{'新标签':<28}依据")
    for code, name, pos, tag, why in rows:
        print(f"{code:<7}{name:<{w+2}}{pos:<9}{tag:<28}{why}")
    print(f"\n共 {len(ADD)} 条：写入 {changed}，跳过 {skipped}")
    if not apply_:
        print("（预演，未落盘。加 --apply 执行）")
        return 0
    bak = FP.with_suffix(f".json.bak_{datetime.now():%Y%m%d_%H%M%S}")
    shutil.copy2(FP, bak)
    tmp = FP.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(FP)
    print(f"已落盘；备份 {bak.name}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
