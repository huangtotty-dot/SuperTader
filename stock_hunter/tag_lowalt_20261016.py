# -*- coding: utf-8 -*-
"""低空经济概念标签追加（2026-10-16，韭研公社「低空经济/亿航智能(250330)」截图 1 张）。

同族脚本：`tag_agri_20261010.py` / `tag_coalchem_20261010.py` / `tag_cybersec_20261010.py`。

## 口径

`jiuyan_category` = **大类目**（如「商业航天」），`jiuyan_concept` = **其下细分**（如「火箭」）。
而 `prescreen.sym2board_map()` 只取 **`cats[0]`** —— 有 `jiuyan_category1` 就用它，
否则按 `|` 拆 `jiuyan_category` 取第一个 ⇒ **一股一个板块**。

⚠️ **本脚本一律"只增不改"**：

| 现状 | 做法 |
|---|---|
| 既无 `jiuyan_category` 也无 `jiuyan_category1`（= 本来没有板块） | 填 `低空经济`，细分写进 `jiuyan_concept`（照「商业航天」写法） |
| 已有板块（两者任一非空） | **主类目一个字不碰**；`低空经济` 写进**第一个空次级槽**（`jiuyan_category2..9`） |
| `jiuyan_concept` 非空 | **不覆盖** |

为什么不能直接覆盖主类目：这些票会**换板块** ⇒ 改变 `prescreen` 的「板块涨停家数」排名，
而那是 **OSC 实盘选股的输入**（owner 2026-10-16：只要求"增加一个概念"）。

**ST/*ST 剔除**（owner 2026-09-01 拍板）：本批名单里**没有** ST/退市票，故无需剔除。

## 未纳入（名单里 5 只**不在 watchlist** 里）

天宜上佳(688033) · 东方时尚(603377) · 中化岩土(002542) · 航天宏图(688066) · 岭南股份(002717)

截图另有「信科移动」，表内实名是 **信科移动-U(688387)**（未盈利标的带 -U 后缀），已按实名纳入。

## 用法

    python stock_hunter/tag_lowalt_20261016.py            # 干跑，只打印
    python stock_hunter/tag_lowalt_20261016.py --apply    # 真写（先备份 .bak_*_lowalt）

幂等：重复跑只补仍旧为空的槽，不会重复写。
"""
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")

CAT = "低空经济"
# (代码, 截图名称, 细分标签)。细分标签来自截图里的**行标签**（多行的用 | 连接）。
STOCKS = [
    ("000099", "中信海直", "旅游|应急"),
    ("000420", "吉林化纤", "制造链|碳纤维"),
    ("000524", "岭南控股", "零部件|旅游"),
    ("000701", "厦门信达", "应急"),
    ("000738", "航发控制", "动力系统"),
    ("000801", "四川九洲", "系统服务"),
    ("000868", "安凯客车", "制造链"),
    ("001696", "宗申动力", "零部件|动力系统"),
    ("002006", "精工科技", "碳纤维"),
    ("002023", "海特高新", "eVTOL|动力系统"),
    ("002085", "万丰奥威", "eVTOL"),
    ("002097", "山河智能", "eVTOL"),
    ("002111", "威海广泰", "运维|应急"),
    ("002120", "韵达股份", "物流"),
    ("002253", "川大智胜", "系统服务"),
    ("002352", "顺丰控股", "物流"),
    ("002363", "隆基机械", "零部件"),
    ("002376", "新北洋", "物流"),
    ("002389", "航天彩虹", "无人机"),
    ("002419", "天虹股份", "物流"),
    ("002520", "日发精机", "零部件"),
    ("002590", "万安科技", "零部件"),
    ("002660", "茂硕电源", "无人机|农业"),
    ("002669", "康达新材", "碳纤维"),
    ("002682", "龙洲股份", "电力"),
    ("002708", "光洋股份", "零部件"),
    ("002735", "王子新材", "零部件"),
    ("002957", "科瑞技术", "物流"),
    ("002984", "森麒麟", "零部件"),
    ("002999", "天禾股份", "农业"),
    ("003004", "声迅股份", "空管|系统服务"),
    ("300107", "建新股份", "eVTOL"),
    ("300284", "苏交科", "智慧灯杆|培训"),
    ("300411", "金盾股份", "eVTOL"),
    ("300424", "航新科技", "运维"),
    ("300484", "蓝海华腾", "动力系统"),
    ("300542", "新晨科技", "运维|气象探测"),
    ("300681", "英搏尔", "制造链|动力系统"),
    ("300690", "双一科技", "碳纤维"),
    ("300719", "安达维尔", "碳纤维"),
    ("300771", "智莱科技", "物流"),
    ("300859", "西域旅游", "运营链|旅游"),
    ("300900", "广联航空", "无人机|碳纤维"),
    ("300975", "商络电子", "eVTOL"),
    ("300989", "蕾奥规划", "解决方案"),
    ("301002", "崧盛股份", "智慧灯杆"),
    ("301091", "深城交", "空管|解决方案|智慧灯杆"),
    ("301248", "杰创智能", "系统服务"),
    ("301398", "星源卓镁", "零部件"),
    ("600038", "中直股份", "eVTOL"),
    ("600562", "国睿科技", "空管"),
    ("600576", "祥源文旅", "运营链"),
    ("600580", "卧龙电驱", "动力系统"),
    ("600843", "上工申贝", "运动飞机"),
    ("600862", "中航高科", "碳纤维"),
    ("600990", "四创电子", "运维"),
    ("601238", "广汽集团", "制造链"),
    ("603018", "华设集团", "服务|解决方案"),
    ("603261", "立航科技", "无人机"),
    ("603308", "应流股份", "零部件|动力系统"),
    ("603679", "华体科技", "智慧灯杆"),
    ("603879", "永悦科技", "物流"),
    ("603950", "长源东谷", "零部件"),
    ("605001", "威奥股份", "碳纤维"),
    ("605056", "咸亨国际", "服务"),
    ("605178", "时空科技", "智慧灯杆"),
    ("688070", "纵横股份", "无人机|农业"),
    ("688297", "中无人机", "无人机"),
    ("688387", "信科移动-U", "智慧灯杆"),
    ("688522", "纳睿雷达", "空管"),
    ("688567", "孚能科技", "零部件"),
    ("688631", "莱斯信息", "空管|系统服务"),
]


def main():
    apply = "--apply" in sys.argv
    d = json.load(open(PATH, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added, slotted, unchanged, wrong = [], [], [], []

    for code, name, tags in STOCKS:
        v = d.get(code)
        if not isinstance(v, dict):
            wrong.append((code, name, "不在 watchlist"))
            continue
        real = str(v.get("name") or "")
        if real != name:
            # 名称校验为辅：对不上就**只报告不动手**（防我抄错代码）
            wrong.append((code, name, "表内实名为 %r" % real))
            continue
        if "ST" in real.upper() or "退" in real:
            wrong.append((code, name, "ST/退市，按 owner 口径剔除"))
            continue

        cat = str(v.get("jiuyan_category") or "").strip()
        cat1 = str(v.get("jiuyan_category1") or "").strip()
        con = str(v.get("jiuyan_concept") or "").strip()

        if not cat and not cat1:
            # 本来没有板块 ⇒ 低空经济当主类目（纯新增）
            v["jiuyan_category"] = CAT
            if not con:
                v["jiuyan_concept"] = tags
            v["updated_at"] = now
            added.append((code, real, tags))
        else:
            # 已有板块 ⇒ 主类目一个字不碰，低空经济进第一个空次级槽
            if CAT in [str(v.get("jiuyan_category%d" % i) or "").strip() for i in range(1, 10)]:
                unchanged.append((code, real, "已有低空经济"))
                continue
            if not cat1:
                v["jiuyan_category1"] = cat          # 原类目下沉 ⇒ cats[0] 不变
            slot = next((i for i in range(2, 10)
                         if not str(v.get("jiuyan_category%d" % i) or "").strip()), None)
            if slot is None:
                wrong.append((code, real, "次级槽用满，跳过"))
                continue
            v["jiuyan_category%d" % slot] = CAT
            v["updated_at"] = now
            slotted.append((code, real, "%s (原类目=%s，板块未变)" % (CAT, cat1 or cat)))

    print("名单 %d 只" % len(STOCKS))
    print("  ① 低空经济成为**主类目**（原本没有板块）: %d 只" % len(added))
    print("  ② 写入**次级槽**、板块归属不变        : %d 只" % len(slotted))
    if slotted:
        print("     " + "、".join("%s %s" % (c, n) for c, n, _ in slotted[:10])
              + ("…" if len(slotted) > 10 else ""))
    if unchanged:
        print("  ③ 已有低空经济、跳过: %d 只" % len(unchanged))
    if wrong:
        print("  ⚠️ 未处理 %d 只（只报告不动手）:" % len(wrong))
        for c, n, why in wrong:
            print("     %s %-8s %s" % (c, n, why))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    shutil.copy2(PATH, PATH + ".bak_%s_lowalt" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)
    print("\n已写入 %s（indent=1，与原文一致；已备份）" % PATH)


if __name__ == "__main__":
    main()
