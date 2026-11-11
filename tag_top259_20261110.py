# -*- coding: utf-8 -*-
"""2026-11-10 owner：「帮我逐一查询图片中的股票，然后归类到对应的韭研概念中去」。

数据源 = owner 的 7 张涨幅榜截图（259 只，代码/名称/所属行业）。核对结果：
  · 259 只全部在 watchlist（0 只不在库）；2 只名称差异：300952 恒辉安防（截图写恒锋安防）、
    301697 N贝特利（截图写贝特利，watchlist 带 N）。
  · **147 只已有韭研概念**（本次不动，只增不改）。
  · **112 只无任何韭研概念**；本次按「所属行业 + 主营业务」归类其中 **99 只**到现有韭研概念。
  · 13 只无对应韭研概念，保持空白（家居/造纸/包装/环保/运输/仪器分销）：
    002631 德尔未来、603058 永吉股份、600793 宜宾纸业、002303 美盈森、301336 趣睡科技、
    300961 深水海纳（环保概念已取消）、300616 尚品宅配、300952 恒辉安防、603818 曲美家居、
    002819 东方中科、001211 双枪科技、603687 大胜达、002357 富临运业。

写前备份；幂等；每只票打 `jiuyan_top259_20261110` 审计标记。
运行：`python tag_top259_20261110.py [--apply]`
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
NOTE_KEY = "jiuyan_top259_20261110"

# code: (wl_name, category, concept, 依据/行业)
MAP = {
    # ── AI应用 ──
    "300264": ("佳创视讯", "AI应用", "AI视觉", "IT服务/大视频解决方案"),
    "300746": ("汉嘉数智", "AI应用", "AI设计", "IT服务/专业技术服务"),
    "002425": ("凯撒文化", "AI应用", "AI游戏", "游戏"),
    "300291": ("百纳千成", "AI应用", "AI影视短剧", "影视院线"),
    "002343": ("慈文传媒", "AI应用", "AI影视短剧", "影视院线"),
    "603602": ("纵横通信", "AI应用", "AI营销", "文化传媒/数字营销"),
    "002905": ("金逸影视", "AI应用", "AI影视短剧", "影视院线"),
    "605398": ("新炬网络", "AI应用", "AI运维", "IT服务/运维"),
    "300494": ("盛天网络", "AI应用", "AI游戏", "游戏"),
    "300299": ("富春股份", "AI应用", "AI游戏", "游戏/互联网"),
    "300467": ("迅游科技", "AI应用", "AI游戏", "游戏/互联网"),
    "601999": ("出版传媒", "AI应用", "AI出版", "新闻和出版业"),
    "300251": ("光线传媒", "AI应用", "AI影视短剧", "影视院线"),
    "301231": ("荣信文化", "AI应用", "AI出版", "少儿图书出版"),
    "300559": ("佳发教育", "AI应用", "AI教育", "教育信息化软件"),
    # ── 数据要素 ──
    "301428": ("世纪恒通", "数据要素", "数据要素_交通数据", "通信服务/高速ETC"),
    "300552": ("万集科技", "数据要素", "数据要素_交通数据", "ETC/车联网"),
    "002869": ("金溢科技", "数据要素", "数据要素_交通数据", "ETC/V2X"),
    # ── 云计算 ──
    "300608": ("思特奇", "云计算", "PaaS-应用开发与运维", "电信软件/云"),
    # ── 半导体 ──
    "603375": ("盛景微", "半导体", "芯片设计-MCU与嵌入式", "电子雷管控制芯片"),
    # ── 消费电子 ──
    "301314": ("科瑞思", "消费电子", "被动元件", "网络变压器/磁性元件"),
    "300951": ("博硕科技", "消费电子", "消费电子", "精密功能件"),
    "301578": ("辰奕智能", "消费电子", "消费电子", "智能遥控器"),
    "301387": ("光大同创", "消费电子", "消费电子", "消费电子防护/功能件"),
    "300752": ("隆利科技", "消费电子", "光学显示", "背光显示模组"),
    "300808": ("久量股份", "消费电子", "LED", "LED照明"),
    "002861": ("瀛通通讯", "消费电子", "消费电子", "声学/线材"),
    "300976": ("达瑞电子", "消费电子", "消费电子", "消费电子功能件"),
    "301067": ("显盈科技", "消费电子", "消费电子", "信号转换/拓展坞"),
    "002635": ("安洁科技", "消费电子", "消费电子", "消费电子功能件"),
    "301086": ("鸿富瀚", "消费电子", "消费电子", "消费电子散热/功能件"),
    "300323": ("华灿光电", "消费电子", "LED", "LED芯片/外延片"),
    "301383": ("天键股份", "消费电子", "消费电子", "耳机等智能音频"),
    "002012": ("凯恩股份", "消费电子", "被动元件", "电解电容器纸"),
    "301285": ("鸿日达", "消费电子", "消费电子", "精密连接器/结构件"),
    "603303": ("得邦照明", "消费电子", "LED", "照明设备"),
    "300790": ("宇瞳光学", "消费电子", "AI眼镜", "光学镜头/AI眼镜"),
    "603685": ("晨丰科技", "消费电子", "LED", "LED照明结构件"),
    "300269": ("联建光电", "消费电子", "LED", "LED显示"),
    "001326": ("联域股份", "消费电子", "LED", "LED照明"),
    "301567": ("贝隆精密", "消费电子", "消费电子", "精密结构件"),
    "301045": ("天禄科技", "消费电子", "光学显示", "导光板"),
    "300632": ("光莆股份", "消费电子", "LED", "LED/UV"),
    "002723": ("小崧股份", "消费电子", "LED", "LED照明/小家电"),
    # ── 光通信 ──
    "300331": ("苏大维格", "光通信", "光通信材料-光学与晶体", "微纳光学"),
    "300710": ("万隆光电", "光通信", "光传输与接入设备", "广电传输设备"),
    # ── PCB ──
    "001232": ("嘉立创", "PCB", "PCB设计与SMT服务", "PCB打样/PCBA"),
    # ── 机器人 ──
    "301255": ("通力科技", "机器人", "核心零部件-传动", "减速器"),
    "300885": ("海昌新材", "机器人", "结构件与材料", "粉末冶金零件"),
    "300508": ("维宏股份", "机器人", "电机电控与感知", "运动控制/伺服"),
    "300154": ("瑞凌股份", "机器人", "智能装备与机床", "焊接设备"),
    "002021": ("中捷资源", "机器人", "智能装备与机床", "缝纫机"),
    "001226": ("拓山重工", "机器人", "结构件与材料", "工程机械零部件"),
    "002209": ("达意隆", "机器人", "智能装备与机床", "包装机械"),
    "301312": ("智立方", "机器人", "智能装备与机床", "自动化设备"),
    "301279": ("金道科技", "机器人", "智能装备与机床", "叉车变速箱/工程机械"),
    "301502": ("华阳智能", "机器人", "电机电控与感知", "微特电机"),
    "300549": ("优德精密", "机器人", "结构件与材料", "精密模具/零部件"),
    "300382": ("斯莱克", "机器人", "智能装备与机床", "金属包装设备/机器人零部件"),
    # ── 化工 / 石油 ──
    "301591": ("肯特股份", "化工", "改性材料", "工程塑料/PEEK"),
    "000782": ("恒申新材", "化工", "化学纤维", "化学纤维"),
    "002324": ("普利特", "化工", "改性材料", "改性塑料"),
    "300599": ("雄塑科技", "化工", "塑料制品", "塑料管材"),
    "002825": ("纳尔股份", "化工", "塑料制品", "塑料/数码喷印"),
    "301395": ("仁信新材", "化工", "基础化工", "聚苯乙烯"),
    "300920": ("润阳科技", "化工", "塑料制品", "泡沫塑料"),
    "301057": ("汇隆新材", "化工", "化学纤维", "化学纤维"),
    "000593": ("德龙汇能", "化工", "天然气", "城市燃气"),
    "603637": ("镇海股份", "石油", "炼油化工", "石化工程设计"),
    # ── 有色金属 ──
    "002160": ("常铝股份", "有色金属", "铝", "铝加工"),
    "605208": ("永茂泰", "有色金属", "铝", "再生铝/汽车零部件"),
    # ── 电力 ──
    "300670": ("大烨智能", "电力", "智能电网", "电网设备"),
    "002953": ("日丰股份", "电力", "电缆", "电缆"),
    "002729": ("好利科技", "电力", "电力电子与配套", "熔断器"),
    "300069": ("金利华电", "电力", "绝缘子", "绝缘子"),
    "300811": ("铂科新材", "电力", "电力电子与配套", "金属软磁粉芯"),
    # ── 汽车 ──
    "300825": ("阿尔特", "汽车", "整车", "整车及平台研发"),
    "002870": ("香山股份", "汽车", "零部件", "汽车零部件/智能座舱"),
    "002664": ("信质集团", "汽车", "零部件", "电机定转子"),
    "301225": ("恒勃股份", "汽车", "零部件", "汽车热管理"),
    "603418": ("友升股份", "汽车", "零部件", "汽车铝材"),
    "301020": ("密封科技", "汽车", "零部件", "密封件"),
    "300816": ("艾可蓝", "汽车", "零部件", "尾气后处理"),
    # ── 光伏 ──
    "300051": ("琏升科技", "光伏", "电池与组件", "HJT电池"),
    # ── 医药 ──
    "300753": ("爱朋医疗", "医药", "医疗器械", "医疗器械"),
    "301075": ("多瑞医药", "医药", "化学制药", "化学制药"),
    # ── 大消费 ──
    "300911": ("亿田智能", "大消费", "大消费_小家电", "集成灶"),
    "603215": ("比依股份", "大消费", "大消费_小家电", "空气炸锅/小家电"),
    "002614": ("奥佳华", "大消费", "大消费_个护电器", "按摩器具"),
    "603677": ("奇精机械", "大消费", "大消费_小家电", "家电零部件"),
    "603608": ("天创时尚", "大消费", "大消费_服装", "时尚鞋履"),
    "301066": ("万事利", "大消费", "大消费_服装", "丝绸纺织"),
    "002875": ("安奈儿", "大消费", "大消费_服装", "童装"),
    "300464": ("星徽股份", "大消费", "大消费_百货零售", "跨境电商/零售"),
    # ── 地产 ──
    "000573": ("粤宏远A", "地产", "房地产开发", "房地产"),
    "002062": ("宏润建设", "地产", "地产基建", "市政/轨交施工"),
    "002963": ("豪尔赛", "地产", "地产基建", "照明工程"),
    # ── 低空 / 商业航天 ──
    "300177": ("中海达", "低空经济", "无人机", "卫星导航/无人机"),
    "605598": ("上海港湾", "商业航天", "地面与数据应用", "岩土工程/航天综合服务"),
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

    dist = Counter((p[2], p[3]) for p in plan)
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
        rs[con] = f"{name}：{why}（2026-11-10 涨幅榜归类）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 涨幅榜归类 → {cat}/{con}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_top259_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_top259_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
