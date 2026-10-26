# -*- coding: utf-8 -*-
"""新增 6 个一级分类，并把 07-09~10-23 涨停缺口里的票归进去（owner 2026-10-24「增加」）。

新增一级分类（此前池内没有，导致这些票再怎么涨停也进不了猎手池）：

    地产 / 汽车 / 煤炭 / 有色金属 / 化工 / 消费电子

作用范围**只限缺口清单**（`doc/research/2026-10-24_涨停股标签缺口清单.csv`，
07-09~10-23 涨停但当前没有韭研概念的 1022 只），不动任何已有标签、不动池外其它票。

规则（行业优先，主营关键词只用来①改派到已有一级分类 ②细分概念）：

  1. 电力/热力生产供应 → 电力/电力运营（不能被「电力设备运行」这类词改派）
  2. 军工电子 → 军工/军工电子
  3. 医药制造/卫生/器械 → 医药/化学制药|医疗器械
  4. 电池/能源金属/锂电材料 → 锂电池/其他关键材料及设备
  5. PCB/覆铜板/线路板 → PCB/PCB；光模块/光纤/光缆 → 光通信/光通信
  6. 电网设备/电气机械 + 电缆变压器关键词 → 电力/电力设备
  7. 其余按行业落新分类：地产 / 汽车 / 煤炭 / 有色金属 / 消费电子 / 化工

未命中（建材、公用事业、纺织、造纸、商贸、运输、专用设备…）继续留池外。

用法：``python stock_hunter/tag_limitup_gap_newcats_20261024.py [--apply]``（默认 dry-run）
"""
import csv
import json
import os
import shutil
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
GAP = r"E:\superTrader\doc\research\2026-10-24_涨停股标签缺口清单.csv"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261024_newcats"
MARK = "2026-10-24 12:30:00"

CHEM_FIX = [
    (("农药", "除草剂", "杀虫剂"), "农药"),
    (("化肥", "复合肥", "尿素", "磷肥"), "化肥"),
    (("涂料", "油墨", "颜料", "色母"), "涂料颜料"),
    (("染料", "印染"), "染料"),
    (("聚酰亚胺", "PI膜", "PET", "薄膜", "膜材料"), "薄膜材料"),
    (("改性", "聚氨酯", "树脂", "塑料"), "改性材料"),
    (("有机硅", "硅烷", "硅橡胶"), "有机硅"),
    (("民爆", "炸药", "雷管"), "民爆"),
    (("氟", "制冷剂"), "氟化工"),
    (("炭黑",), "炭黑"),
    (("催化剂",), "催化剂"),
]


# 人工核过的覆盖（business_summary 大多是「主营XX制造业」的空话，只能靠公司本身）
# value=None ⇒ 不进池（家居/卫浴/科创服务等，池内没有对应分类）
OVERRIDE = {
    # 化工细化
    "高争民爆": ("化工", "民爆"), "江南化工": ("化工", "民爆"),
    "保利联合": ("化工", "民爆"), "北化股份": ("化工", "民爆"),
    "金正大": ("化工", "化肥"), "云图控股": ("化工", "化肥"), "澄星股份": ("化工", "化肥"),
    "黑猫股份": ("化工", "炭黑"), "联科科技": ("化工", "炭黑"),
    "吉华集团": ("化工", "染料"), "闰土股份": ("化工", "染料"),
    "福莱蒽特": ("化工", "染料"), "万丰股份": ("化工", "染料"),
    "利尔化学": ("化工", "农药"), "联化科技": ("化工", "农药"),
    "乐通股份": ("化工", "涂料颜料"), "坤彩科技": ("化工", "涂料颜料"),
    "东方材料": ("化工", "涂料颜料"),
    "肯特催化": ("化工", "催化剂"), "鼎际得": ("化工", "催化剂"),
    "汇得科技": ("化工", "改性材料"), "光华股份": ("化工", "改性材料"),
    "星辉环材": ("化工", "改性材料"),
    "道明光学": ("化工", "薄膜材料"), "乐凯胶片": ("化工", "薄膜材料"),
    "晶华新材": ("化工", "薄膜材料"),
    "桐昆股份": ("化工", "化学纤维"), "尤夫股份": ("化工", "化学纤维"),
    "华西股份": ("化工", "化学纤维"), "天富龙": ("化工", "化学纤维"),
    "海阳科技": ("化工", "化学纤维"),
    # 化工行业里的非化工
    "拉芳家化": ("大消费", "大消费_日化"), "登康口腔": ("大消费", "大消费_日化"),
    "青岛金王": ("大消费", "大消费_日化"),
    "东方铁塔": ("电力", "电力设备"),
    "中红医疗": ("医药", "医疗器械"), "蓝帆医疗": ("医药", "医疗器械"),
    "天普股份": ("汽车", "零部件"),
    "朗迪集团": None, "力合科创": None, "爱丽家居": None, "瑞尔特": None,
    # 医药细化
    "科华生物": ("医药", "医疗器械"), "基蛋生物": ("医药", "医疗器械"),
    "浩欧博": ("医药", "医疗器械"), "正川股份": ("医药", "医疗器械"),
    "沃华医药": ("中药", "中药品牌"), "众生药业": ("中药", "中药品牌"),
    "益盛药业": ("中药", "中药品牌"), "康惠股份": ("中药", "中药品牌"),
    "古汉医药": ("中药", "中药品牌"), "方盛制药": ("中药", "中药品牌"),
    "中源协和": ("医药", "生物医药"), "卫光生物": ("医药", "生物医药"),
    "甘李药业": ("医药", "生物医药"), "迪哲医药-U": ("医药", "创新药"),
    # 汽车整车 / 军工边界
    "众泰汽车": ("汽车", "整车"), "江淮汽车": ("汽车", "整车"),
    "海马汽车": ("汽车", "整车"), "金杯汽车": ("汽车", "整车"),
    "金龙汽车": ("汽车", "整车"), "北汽蓝谷": ("汽车", "整车"),
    "成飞集成": ("军工", "军工配套"), "建设工业": ("军工", "军工配套"),
    # 有色细分
    "中国稀土": ("有色金属", "稀土"), "宝武镁业": ("有色金属", "镁"),
    "闽发铝业": ("有色金属", "铝"), "深圳新星": ("有色金属", "铝"),
    "罗普斯金": ("有色金属", "铝"), "鑫科材料": ("有色金属", "铜"),
    "精艺股份": ("有色金属", "铜"),
    # 地产细分
    "世联行": ("地产", "房产服务"), "南都物业": ("地产", "物业服务"),
}


def _has(rec):
    for k, v in rec.items():
        if k == "jiuyan_concept" or (k.startswith("jiuyan_concept") and k[-1].isdigit()):
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, list) and any(str(x).strip() for x in v):
                return True
    return False


def classify(ind, biz, name=""):
    s = ind or ""
    if name in OVERRIDE:
        v = OVERRIDE[name]
        return None if v is None else (v[0], v[1], "人工核过")
    if any(k in s for k in ("电力", "热力生产和供应")):
        return "电力", "电力运营", f"行业{s}"
    if "军工" in s:
        return "军工", "军工电子", f"行业{s}"
    if "卫生" in s:
        return "医药", "医疗服务", f"行业{s}"
    if any(k in s for k in ("医药制造", "医疗器械")):
        con = "医疗器械" if ("器械" in biz or "器械" in s) else "化学制药"
        return "医药", con, f"行业{s}"
    if any(k in s for k in ("电池", "能源金属")) or (
            any(k in s for k in ("有色金属冶炼", "化学原料", "专用设备制造"))
            and any(k in biz for k in ("正极", "负极", "电解液", "隔膜", "六氟", "碳酸锂", "磷酸铁锂"))):
        return "锂电池", "其他关键材料及设备", "锂电材料"
    if any(k in biz for k in ("印制电路", "覆铜板", "线路板", "PCB")):
        return "PCB", "PCB", "主营PCB"
    if any(k in s for k in ("通信设备", "元件", "光通信")) and \
            any(k in biz for k in ("光模块", "光纤", "光缆", "光芯片")):
        return "光通信", "光通信", "主营光通信"
    if any(k in s for k in ("电网设备", "电气机械和器材")) and \
            any(k in biz for k in ("电缆", "电线", "变压器", "开关柜", "输配电", "特高压")):
        return "电力", "电力设备", "主营电力设备"
    if any(k in s for k in ("房地产业", "房地产开发", "房地产服务", "房产服务")):
        con = "房产服务" if ("经纪" in biz or "服务" in s) else "房地产开发"
        return "地产", con, f"行业{s}"
    if "汽车制造业" in s:
        return "汽车", "零部件", f"行业{s}"
    if any(k in s for k in ("汽车零部件", "汽车服务")):
        return "汽车", "零部件", f"行业{s}"
    if any(k in s for k in ("煤炭开采", "焦炭", "煤")):
        return "煤炭", "煤炭开采", f"行业{s}"
    if any(k in s for k in ("有色金属冶炼", "有色金属矿采选", "金属新材料", "小金属", "能源金属")):
        return "有色金属", "其他", f"行业{s}"
    if "消费电子" in s:
        return "消费电子", "消费电子", f"行业{s}"
    if "光学光电子" in s:
        return "消费电子", "光学显示", f"行业{s}"
    if any(k in s for k in ("化学原料和化学制品", "化学制品", "化学纤维", "橡胶和塑料", "农药", "化肥")):
        for keys, con in CHEM_FIX:
            if any(k in biz for k in keys):
                return "化工", con, f"行业{s}/主营"
        return "化工", "基础化工", f"行业{s}"
    return None


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    gap = [r["代码"] for r in csv.DictReader(open(GAP, encoding="utf-8-sig"))]
    props, miss = [], []
    for code in gap:
        rec = data.get(code)
        if not isinstance(rec, dict) or _has(rec):
            continue
        got = classify((rec.get("sector") or "").split("/")[0], rec.get("business_summary") or "",
                       rec.get("name") or "")
        if not got:
            miss.append((code, rec.get("name") or "?", (rec.get("sector") or "空").split("/")[0]))
            continue
        props.append((code, rec.get("name") or "?", got[0], got[1], got[2]))

    grp = {}
    for p in props:
        grp.setdefault((p[2], p[3]), []).append(p)
    print(f"可归类 {len(props)} 只：")
    for key in sorted(grp, key=lambda k: -len(grp[k])):
        g = grp[key]
        print(f"\n[{key[0]}/{key[1]}] {len(g)} 只：")
        print("   " + "、".join(f"{p[1]}" for p in g))
    print("\n按大类目：", Counter(p[2] for p in props).most_common())
    print(f"\n未命中 {len(miss)} 只（留池外），行业分布：",
          Counter(m[2] for m in miss).most_common(8))

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    for code, name, cat, con, why in props:
        rec = data[code]
        rec["jiuyan_category"] = cat
        rec["jiuyan_concept"] = con
        rec["updated_at"] = MARK
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入 {len(props)} 只。备份: {BAK}")


if __name__ == "__main__":
    main()
