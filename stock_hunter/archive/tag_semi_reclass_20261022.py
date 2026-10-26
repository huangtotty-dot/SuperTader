# -*- coding: utf-8 -*-
"""半导体概念梳理（2026-10-22 owner 六条）。

1) 太极实业 600667：**不做半导体设备**。主营 = 存储封测（海太半导体，SK 海力士后工序代工，
   占营收 ~15%）+ 工程技术服务（十一科技，占 ~76%）+ 光伏电站。
   ⇒ 「半导体设备」删除，改「半导体洁净室 + 存储封测」。
3) 深桑达A 000032：**不做半导体设备**。主营 = 高科技产业服务（洁净室整体解决方案，占 ~97%，是
   半导体**厂房**的建设者：中芯国际/长鑫/长存/华虹…）+ 数字与信息服务（中国电子云/数据要素）。
   ⇒ 「半导体设备」删除，改「半导体洁净室」；顺带把 6) 的「数据要素_数据存储」从半导体对里去掉。
5) 上海电气 601727：**半导体设备不是上市公司主营**（能源装备/工业装备/集成服务）。市场把它当
   半导体概念，靠的是**控股集团**旗下的上海微电子（光刻机，不在 601727 体内）与集团对理想万里晖
   （HJT 光伏设备）的投资 ⇒ 「半导体设备」标签删除。
4) 「半导体设备」拆 **前道 / 后道**：前道=晶圆制造（光刻/刻蚀/薄膜/注入/清洗/CMP/量检测/零部件），
   后道=封装测试（划片/固晶/键合/塑封/测试机/分选机/探针台）。已带 `-测试设备/-封装设备/-HBM检测/
   -存储测试` 子标签的本身就是后道细项，保持不动；只给**裸「半导体设备」**补前/后道。
   存疑的（配套设备/厂务/服务/概念性延伸）**不硬分**，原样保留并在输出里列出来等 owner 定。

用法：``python stock_hunter/tag_semi_reclass_20261022.py [--apply]``（默认 dry-run）
"""
import json
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261022_semi"

# ---- 单只票的定点修正：code -> {slot: 新概念}（slot 0 = 不带编号的旧字段）----
FIX = {
    "600667": {1: ["半导体洁净室", "存储封测"]},        # 太极实业
    "000032": {1: ["半导体洁净室"]},                    # 深桑达A
}
# 整个 pair 删掉（该票没有别的半导体业务可挂）
DROP_PAIR = {"601727": "半导体"}                        # 上海电气

# ---- 前道 / 后道（只列**裸「半导体设备」**且业务明确的）----
FRONT = {
    "002371": "北方华创", "688012": "中微公司", "688072": "拓荆科技", "688082": "盛美上海",
    "688120": "华海清科", "688037": "芯源微", "688147": "微导纳米", "688729": "屹唐股份",
    "688361": "中科飞测", "688630": "芯碁微装", "603690": "至纯科技", "600641": "先导基电",
    "688605": "先锋精科", "002338": "奥普光电", "300567": "精测电子",
}
BACK = {
    "300604": "长川科技", "688200": "华峰测控", "603061": "金海通", "301369": "联动科技",
    "301629": "矽电股份", "688661": "和林微纳", "688627": "精智达", "688383": "新益昌",
    "688419": "耐科装备", "600520": "三佳科技", "603283": "赛腾股份", "300480": "光力科技",
    "688516": "奥特维", "688392": "骄成超声",
}
# ---- 存疑：不硬分，列出来等 owner 定 ----
DOUBT = {
    "600619": "海立股份：为上海微电子做光刻机冷却配套，占比极小",
    "603324": "盛剑科技：半导体厂废气治理**系统**（厂务），非前道/后道工艺设备",
    "603601": "再升科技：洁净室过滤材料（材料，非设备）",
    "301297": "富乐德：半导体设备零部件清洗/再生**服务**",
    "688610": "埃科光电：工业相机/图像采集（检测设备的配套部件）",
    "688665": "四方光电：气体传感器/气体监测（厂务配套）",
    "301226": "祥明智能：洁净室 FFU 用微特电机/风机（厂房配套，非晶圆设备）",
    "600895": "张江高科：园区地产 + 半导体产业投资",
    "300293": "蓝英装备：轮胎成型机/工业自动化，半导体占比很小",
    "300457": "赢合科技：锂电设备为主",
}


def _pairs(r):
    """按 data_loader 口径取 (slot, cat, [concepts...])；slot 0 = 不带编号。"""
    out = []
    for i in range(1, 10):
        c = str(r.get(f"jiuyan_category{i}") or "").strip()
        if c:
            v = r.get(f"jiuyan_concept{i}")
            cons = [str(x) for x in (v if isinstance(v, list) else [v])] if v else []
            out.append((i, c, cons))
    if not out:
        c0 = str(r.get("jiuyan_category") or "").strip()
        if c0:
            v = r.get("jiuyan_concept")
            cons = [str(x) for x in (v if isinstance(v, list) else [v])] if v else []
            out.append((0, c0, cons))
    return out


def _tokens(cons):
    return [t for c in cons for t in str(c).split("|") if t]


def _set_concepts(rec, slot, toks):
    """写回某个槽位的概念；保持 data_loader 吃的形态（list 或多 token 时用 '|' 串）。"""
    val = toks if len(toks) > 1 else (toks[0] if toks else "")
    if slot == 0:
        rec["jiuyan_concept"] = val
    else:
        rec[f"jiuyan_concept{slot}"] = val


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    changes, doubts_hit = [], []

    # ① 定点修正 + ② 删除整对
    for code, slots in FIX.items():
        rec = data.get(code)
        if not rec:
            continue
        for slot, toks in slots.items():
            old = rec.get("jiuyan_concept" if slot == 0 else f"jiuyan_concept{slot}")
            _set_concepts(rec, slot, toks)
            changes.append((code, rec.get("name"), f"slot{slot}", old, toks))

    for code, drop_cat in DROP_PAIR.items():
        rec = data.get(code)
        if not rec:
            continue
        for i, cat, cons in _pairs(rec):
            if cat != drop_cat:
                continue
            old = rec.get("jiuyan_concept" if i == 0 else f"jiuyan_concept{i}")
            if i == 0:
                rec.pop("jiuyan_category", None)
                rec.pop("jiuyan_concept", None)
            else:
                rec.pop(f"jiuyan_category{i}", None)
                rec.pop(f"jiuyan_concept{i}", None)
            changes.append((code, rec.get("name"), f"slot{i}",
                            f"{cat}/{old}", "（整对删除）"))

    # ③ 前道/后道：只动**裸「半导体设备」**
    for code, rec in data.items():
        for i, cat, cons in _pairs(rec):
            if "半导体" not in cat:
                continue
            toks = _tokens(cons)
            if "半导体设备" not in toks:
                continue
            if code in FRONT:
                new = "半导体设备-前道"
            elif code in BACK:
                new = "半导体设备-后道"
            else:
                if code in DOUBT:
                    doubts_hit.append((code, rec.get("name"), DOUBT[code]))
                continue
            toks = [new if t == "半导体设备" else t for t in toks]
            old = rec.get("jiuyan_concept" if i == 0 else f"jiuyan_concept{i}")
            _set_concepts(rec, i, toks)
            changes.append((code, rec.get("name"), f"slot{i}", old, toks))

    print(f"共 {len(changes)} 处改动：")
    for c in changes:
        print(f"  {c[0]} {c[1]} {c[2]}: {c[3]!r} → {c[4]!r}")
    print(f"\n存疑未动（{len(doubts_hit)} 只，等 owner 定）：")
    for d in doubts_hit:
        print(f"  {d[0]} {d[1]} —— {d[2]}")

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
