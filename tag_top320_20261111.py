# -*- coding: utf-8 -*-
"""2026-11-11 owner：「继续根据图片中的个股信息 逐个查询补充到对应的韭研概念中」。

数据源 = owner 的 10 张涨幅榜截图（320 只，代码/名称/所属行业）。核对结果：
  · 320 只全部在 watchlist（0 只不在库）；3 只名称差异：
    300223 北京君正（截图写君正股份）、603061 金海通（截图写海通发展）、301716 N鸿富诚（截图写鸿富诚）。
  · **213 只已有韭研概念**（本次不动，只增不改）。
  · **107 只无任何韭研概念**；本次按「所属行业 + 主营业务」归类其中 **88 只**到现有韭研概念。
  · 19 只无对应韭研概念，保持空白（家居/造纸/包装/环保/钢铁/轨交/五金/部分化工…）：
    301049 超越科技、600103 青山纸业、002110 三钢闽光、300616 尚品宅配、301043 绿岛风、
    301125 腾亚精工、300749 顶固集创、300906 日月明、300883 龙利得、603818 曲美家居、
    301448 开创电气、301127 武汉天源、001211 双枪科技、002631 德尔未来、600611 大众交通、
    301265 华新科技、300858 科拓生物、301107 瑜欣电子、300321 同大股份。

写前备份；幂等；每只票打 `jiuyan_top320_20261111` 审计标记。
运行：`python tag_top320_20261111.py [--apply]`
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
NOTE_KEY = "jiuyan_top320_20261111"

# code: (wl_name, category, concept, 依据)
MAP = {
    # ── 汽车 ──
    "300473": ("德尔股份", "汽车", "零部件", "汽车转向/电泵"),
    "603725": ("天安新材", "汽车", "零部件", "汽车内饰饰面材料"),
    "603586": ("金麒麟", "汽车", "零部件", "刹车片/制动摩擦材料"),
    "002593": ("日上集团", "汽车", "轮毂", "车轮/钢圈"),
    "605228": ("神通科技", "汽车", "零部件", "汽车塑料件"),
    "603158": ("腾龙股份", "汽车", "零部件", "汽车热管理"),
    "002813": ("路畅科技", "汽车", "零部件", "车载导航/智能驾驶"),
    "603786": ("科博达", "汽车", "零部件", "汽车电子"),
    "301499": ("维科精密", "汽车", "零部件", "汽车电子精密件"),
    "300652": ("雷迪克", "汽车", "零部件", "汽车轴承"),
    "002434": ("万里扬", "汽车", "零部件", "变速器"),
    # ── 机器人 ──
    "300836": ("佰奥智能", "机器人", "智能装备与机床", "智能制造装备"),
    "002633": ("申科股份", "机器人", "核心零部件-传动", "滑动轴承"),
    "603135": ("中重科技", "机器人", "智能装备与机床", "冶金智能装备"),
    "002779": ("中坚科技", "机器人", "本体与整机", "园林机械/割草机器人"),
    "301512": ("智信精密", "机器人", "智能装备与机床", "自动化设备/苹果链"),
    "603275": ("众辰科技", "机器人", "电机电控与感知", "变频器/伺服"),
    "601177": ("杭齿前进", "机器人", "核心零部件-传动", "减速器/齿轮箱"),
    "301360": ("荣旗科技", "机器人", "电机电控与感知", "机器视觉检测设备"),
    "603095": ("越剑智能", "机器人", "智能装备与机床", "高端纺机装备"),
    "600815": ("厦工股份", "机器人", "智能装备与机床", "工程机械/装载机"),
    "605389": ("长龄液压", "机器人", "核心零部件-传动", "液压件/减速器"),
    "603166": ("福达股份", "机器人", "核心零部件-传动", "曲轴/减速器/人形机器人"),
    "300415": ("伊之密", "机器人", "智能装备与机床", "注塑机/压铸机"),
    "300916": ("朗特智能", "机器人", "电机电控与感知", "智能控制器"),
    "603270": ("金帝股份", "机器人", "核心零部件-传动", "轴承保持架/精密件"),
    # ── AI应用 ──
    "002919": ("名臣健康", "AI应用", "AI游戏", "游戏/互联网"),
    "300935": ("盈建科", "AI应用", "AI设计", "建筑结构设计软件"),
    "301365": ("矩阵股份", "AI应用", "AI设计", "室内设计/AIGC"),
    "002990": ("盛视科技", "AI应用", "AI视觉", "智慧口岸/AI视觉"),
    # ── 半导体 ──
    "301666": ("大普微-UW", "半导体", "存储模组与系统", "企业级SSD"),
    "301319": ("唯特偶", "半导体", "半导体材料-封装材料", "微电子焊接材料/先进封装"),
    # ── 消费电子 ──
    "300939": ("秋田微", "消费电子", "光学显示", "液晶显示/触控"),
    "301183": ("东田微", "消费电子", "光学元件", "精密光学元器件/CPO"),
    "301362": ("民爆光电", "消费电子", "LED", "LED照明"),
    "300868": ("杰美特", "消费电子", "消费电子", "手机保护类配件"),
    "300956": ("英力股份", "消费电子", "消费电子", "消费电子结构件"),
    "301716": ("N鸿富诚", "消费电子", "消费电子", "导热界面材料"),
    # ── 光通信 ──
    "301419": ("阿莱德", "光通信", "光通信配套-温控器件", "射频/EMI/导热器件"),
    "002902": ("铭普光磁", "光通信", "光器件-无源器件", "磁性元件/光通信"),
    "001267": ("汇绿生态", "光通信", "光模块-数通与AI算力", "通信设备/CPO"),
    "002796": ("世嘉科技", "光通信", "光模块-数通与AI算力", "通信设备/CPO"),
    "300310": ("宜通世纪", "光通信", "光网络系统与工程服务", "通信网络服务"),
    # ── PCB ──
    "300852": ("四会富仕", "PCB", "PCB-通用刚性板", "PCB"),
    "301282": ("金禄电子", "PCB", "PCB-通用刚性板", "PCB"),
    "301041": ("金百泽", "PCB", "PCB设计与SMT服务", "PCB/PCBA"),
    "300400": ("劲拓股份", "PCB", "PCB设备-检测与测试", "PCB焊接/检测设备"),
    "301630": ("同宇新材", "PCB", "树脂-环氧与碳氢", "电子树脂"),
    "605006": ("山东玻纤", "PCB", "电子布与玻纤材料", "玻璃纤维"),
    # ── 电力 ──
    "002857": ("三晖电气", "电力", "智能电网", "电能表/仪器仪表"),
    "300933": ("中辰股份", "电力", "电缆", "电缆"),
    "603988": ("中电电机", "电力", "发电设备（整机与辅机）", "大中型电机"),
    "300960": ("通业科技", "电力", "电力电子与配套", "轨交电源/电气"),
    "301390": ("经纬股份", "电力", "电力建设", "电力设计/勘察"),
    "000631": ("顺发恒能", "电力", "电力运营", "风电/新能源运营"),
    # ── 光伏 ──
    "600876": ("凯盛新能", "光伏", "辅材与结构", "光伏玻璃"),
    "301046": ("能辉科技", "光伏", "电站与应用", "光伏电站EPC"),
    # ── 化工 ──
    "000545": ("金浦钛业", "化工", "基础化工", "钛白粉"),
    "600172": ("黄河旋风", "化工", "超硬材料", "超硬材料/培育钻石"),
    "300179": ("四方达", "化工", "超硬材料", "超硬材料/培育钻石"),
    "002895": ("川恒股份", "化工", "基础化工", "磷化工"),
    "301037": ("保立佳", "化工", "涂料颜料", "乳液/涂料"),
    "301585": ("蓝宇股份", "化工", "涂料颜料", "数码喷印墨水"),
    "603722": ("阿科力", "化工", "基础化工", "聚醚胺/COC材料"),
    "603332": ("苏州龙杰", "化工", "化学纤维", "化学纤维"),
    "300538": ("同益股份", "化工", "塑料制品", "工程塑料分销"),
    "000407": ("胜利股份", "化工", "天然气", "天然气"),
    "603080": ("新疆火炬", "化工", "天然气", "城市燃气"),
    # ── 有色金属 ──
    "300835": ("龙磁科技", "有色金属", "稀土", "磁性材料/稀土永磁"),
    # ── 电池 ──
    "301222": ("浙江恒威", "电池", "其他关键材料及设备", "锌锰电池"),
    # ── 大农业 ──
    "300804": ("广康生化", "大农业", "农药", "农药"),
    # ── 医药 ──
    "300501": ("海顺新材", "医药", "药用包材", "药用包装材料"),
    "600538": ("国发股份", "医药", "医药商业", "医药批发"),
    # ── 大消费 ──
    "002154": ("报喜鸟", "大消费", "大消费_服装", "服装"),
    "603877": ("太平鸟", "大消费", "大消费_服装", "服装"),
    "603116": ("红蜻蜓", "大消费", "大消费_服装", "鞋履"),
    "002397": ("梦洁股份", "大消费", "大消费_服装", "家纺"),
    "002486": ("嘉麟杰", "大消费", "大消费_服装", "纺织服装"),
    "002394": ("联发股份", "大消费", "大消费_服装", "纺织"),
    "603150": ("万朗磁塑", "大消费", "大消费_小家电", "家电零部件"),
    "301008": ("宏昌科技", "大消费", "大消费_小家电", "家电零部件"),
    "301135": ("瑞德智能", "大消费", "大消费_小家电", "家电智能控制器"),
    # ── 地产 ──
    "600629": ("华建集团", "地产", "地产基建", "建筑设计"),
    "002713": ("东易日盛", "地产", "地产基建", "家装"),
    "300844": ("山水比德", "地产", "地产基建", "景观设计"),
    "300986": ("志特新材", "地产", "地产基建", "铝模板"),
    "300517": ("海波重科", "地产", "地产基建", "桥梁钢结构"),
    # ── 云计算 ──
    "002148": ("北纬科技", "云计算", "云通信与消息服务", "企业短信/物联网"),
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
            bad.append((code, name, "不在 watchlist")); continue
        wl_name = str(e.get("name") or "")
        if wl_name != name:
            bad.append((code, name, f"名称不符 watchlist={wl_name}")); continue
        pairs = get_pairs(e)
        if has_pair(pairs, cat, con):
            skip.append(code); continue
        plan.append((code, name, cat, con, why, pairs))

    print(f"待归类 {len(plan)} 只；已是目标概念跳过 {len(skip)} 只；异常 {len(bad)}")
    for b in bad:
        print("  ⚠️", b)
    print("\n按类目：")
    for cat, n in Counter(p[2] for p in plan).most_common():
        print(f"  {cat}: {n}")
    print("\n明细：")
    for code, name, cat, con, why, before in plan:
        print(f"  {code} {name}: {before} -> +('{cat}','{con}')  [{why}]")

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
        rs[con] = f"{name}：{why}（2026-11-11 涨幅榜归类）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 涨幅榜归类 → {cat}/{con}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_top320_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_top320_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
