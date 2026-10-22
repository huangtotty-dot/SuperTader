# -*- coding: utf-8 -*-
"""按 2026-10-21 涨停板（50 只）补/修韭研标签 —— **只合入现有 30 个板块，不新建概念**。

owner 2026-10-22：「不要轻易建立新的概念，最好能合入现在的板块」。

实测 50 只里 **22 只完全没有标签**（=不进猎手池），另有 3 处标签与主营/涨停原因明显不符。
本脚本把能合入现有一级板块的补上；合不进去的（水泥/化纤/太空算力等）**不动**，列在输出里。

铁律：只增不改 —— 已有标签保留，新概念追加到下一个空槽；只有不带编号的老字段时先**镜像下沉**
（原类目进 1 号槽），否则该票会整体掉出猎手池。

用法：``python stock_hunter/tag_limitup_20261021.py [--apply]``（默认 dry-run）
"""
import json
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261021_limitup"

# code -> [(现有大类目, 细分)] —— 依据 = 涨停原因 + 主营
ADD = {
    "001216": [("PCB", "MLCC")],                       # 华瓷股份：MLCC 粉体（电子陶瓷）
    "600715": [("AI应用", "AI应用")],                   # 文投控股：AI 漫剧+游戏
    "002724": [("军工", "军工配套")],                   # 海洋王：专业照明/军品照明
    "600252": [("医药", "中药")],                       # 中恒集团：中药创新药
    "000518": [("医药", "化学制药")],                   # 四环生物：生物医药
    "601566": [("大消费", "大消费_服装")],              # 九牧王：男裤/羊毛羊绒
    "300296": [("机器人", "本体及应用")],               # 利亚德：人形机器人+Micro LED
    "002181": [("AI应用", "短剧")],                     # 粤传媒：微短剧
    "600271": [("数据要素", "数据要素_其他行业数据"), ("AI应用", "AI应用")],  # 航天信息：数字财税+数据要素
    "002368": [("数据要素", "数据要素_数据存储")],      # 太极股份：数据库+云数据服务
    "002835": [("AI应用", "AI应用")],                   # 同为股份：AI 安防
    "301102": [("AI应用", "AI应用")],                   # 兆讯传媒：AI 创意+高铁传媒
    "002242": [("大消费", "大消费_小家电")],            # 九阳股份：厨房小家电+机器人投资
    "000788": [("医药", "化学制药")],                   # 北大医药：化学制药
    "002347": [("机器人", "本体及应用")],               # 泰尔股份：工业机器人+冶金装备
    "603696": [("大消费", "大消费_调味品")],            # 安记食品：复合调味品
    "600178": [("低空经济", "动力系统")],               # 东安动力：飞行汽车+增程器
    "002313": [("AI应用", "AI应用")],                   # 日海智能：端侧AI+无线模组+机器人
    "603999": [("AI应用", "AI应用")],                   # 读者传媒：数字出版+AI
    "002811": [("AI应用", "AI应用")],                   # 郑中设计：AI 设计
    # 已有标签的补充（只增）
    "603721": [("AI应用", "AI应用")],                   # 中广天择：AI 语料/AI 漫剧（已有 数据要素）
    "603636": [("AI应用", "AI应用"), ("数据要素", "数据要素_数据确权")],   # 南威软件：AI 短剧+政务智能体
    "603236": [("机器人", "其他")],                     # 移远通信：端侧AI+机器人+通信模组
}

# 明确标错：摘掉旧对，改挂新的（owner：「错误就更正」）
MOVE = {
    # 永达股份是**盾构机/风电锻件**，与「液冷快换接头」无关（那是另一家公司）
    "001239": {"from": "液冷", "to": [("电力", "电力设备")]},
    # 恒大高新是**电力环保+钠电材料**，与「短剧」无关
    "002591": {"from": "AI应用", "to": [("锂电池", "其他关键材料及设备")]},
    # 天威视讯是**广电网络+数据中心+网络安全**，与「短剧」无关
    "002238": {"from": "AI应用", "to": [("网络安全", "数据要素_网络可视化监管")]},
}

# 合不进现有板块、且没有明显归属的 —— 不动，列给 owner
UNPLACED = {
    "600802": ("福建水泥", "水泥熟料+重点工程+福建国资"),
    "603332": ("苏州龙杰", "涤纶长丝+仿皮革纤维"),
    "002565": ("顺灏股份", "太空算力+新型烟草+环保包装（现挂 太空光伏/太阳翼结构，存疑）"),
}


def _pairs(r):
    out = []
    for i in range(1, 10):
        c = str(r.get(f"jiuyan_category{i}") or "").strip()
        if c:
            v = r.get(f"jiuyan_concept{i}")
            out.append((i, c, [str(x) for x in (v if isinstance(v, list) else [v])] if v else []))
    if not out:
        c0 = str(r.get("jiuyan_category") or "").strip()
        if c0:
            v = r.get("jiuyan_concept")
            out.append((0, c0, [str(x) for x in (v if isinstance(v, list) else [v])] if v else []))
    return out


def _toks(cons):
    return [t for c in cons for t in str(c).split("|") if t]


def _write_pair(rec, slot, cat, toks):
    val = toks if len(toks) > 1 else (toks[0] if toks else "")
    if slot == 0:
        rec["jiuyan_category"] = cat
        rec["jiuyan_concept"] = val
    else:
        rec[f"jiuyan_category{slot}"] = cat
        rec[f"jiuyan_concept{slot}"] = val


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    log, skipped = [], []

    def _ensure(rec, code, cat, con):
        ps = _pairs(rec)
        for _s, c, cons in ps:
            if c == cat and con in _toks(cons):
                skipped.append((code, rec.get("name"), f"已有 {cat}/{con}"))
                return
        if not ps:
            _write_pair(rec, 0, cat, [con])
            log.append((code, rec.get("name"), f"（无标签）→ {cat}/{con}"))
            return
        if len(ps) == 1 and ps[0][0] == 0:
            _, c0, cons0 = ps[0]
            _write_pair(rec, 1, c0, _toks(cons0))       # 镜像下沉
            _write_pair(rec, 2, cat, [con])
            log.append((code, rec.get("name"), f"下沉 {c0}→slot1，新增 {cat}/{con}→slot2"))
            return
        used = {s for s, _, _ in ps}
        slot = next((i for i in range(1, 10) if i not in used), None)
        if slot is None:
            skipped.append((code, rec.get("name"), "槽位已满"))
            return
        _write_pair(rec, slot, cat, [con])
        log.append((code, rec.get("name"), f"追加 {cat}/{con}→slot{slot}"))

    for code, items in ADD.items():
        rec = data.get(code)
        if not rec:
            skipped.append((code, "?", "不在 watchlist")); continue
        for cat, con in items:
            _ensure(rec, code, cat, con)

    for code, mv in MOVE.items():
        rec = data.get(code)
        if not rec:
            continue
        for slot, c, cons in _pairs(rec):
            if c == mv["from"]:
                if slot == 0:
                    rec.pop("jiuyan_category", None); rec.pop("jiuyan_concept", None)
                else:
                    rec.pop(f"jiuyan_category{slot}", None); rec.pop(f"jiuyan_concept{slot}", None)
                log.append((code, rec.get("name"), f"更正：摘掉「{mv['from']}」({_toks(cons)})"))
                break
        for cat, con in mv["to"]:
            _ensure(rec, code, cat, con)

    print(f"改动 {len(log)} 处：")
    for x in log:
        print(f"  {x[0]} {x[1]}  {x[2]}")
    if skipped:
        print(f"\n跳过（已存在）{len(skipped)} 处")
    print("\n合不进现有板块、未动（等 owner 定）：")
    for c, (n, why) in UNPLACED.items():
        print(f"  {c} {n} —— {why}")

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
