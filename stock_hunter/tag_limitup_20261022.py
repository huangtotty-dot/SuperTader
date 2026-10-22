# -*- coding: utf-8 -*-
"""按 2026-10-22 涨停板（33 只）补/修韭研标签。

owner 传了两张涨停板截图（含「涨停原因」），要求核对韭研概念有没有漏或错。
实测：**21 只完全没标签**（=不进猎手池），另有 3 只标签与主营/涨停原因不符。

本脚本只处理**能落到已有大类目**上的：
  机器人 / 电力 / 军工 / 数据要素 / AI应用 / 光通信 / 锂电池 / 液冷 / 大消费 / 煤化工

口径（沿用本仓铁律）：
  · 只增不改：已有标签一律保留，新概念追加到**下一个空槽**；
  · 只有老字段（不带编号）时**先下沉**：原类目原样镜像进 1 号槽，新类目进 2 号槽
    —— 漏了镜像会让票整个掉出猎手池；
  · 唯一例外是**明确标错**的（哈空调在「冰雪经济/旅游消费」下、博汇股份液冷概念只写"其他"），
    按 owner「错误就更正」处理，脚本里单独列出。

用法：``python stock_hunter/tag_limitup_20261022.py [--apply]``（默认 dry-run）
"""
import json
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261022_limitup"

# code -> [(大类目, 细分概念)]  —— 依据 = 今日涨停原因 + 主营核对
ADD = {
    # ── 人形机器人链（今日 9 只）─────────────────────────────────────────
    "301023": [("机器人", "微特减速电机")],      # 奕帆传动：微特减速电机+伺服电机
    "603370": [("机器人", "精密铁芯")],          # 华新精科：精密铁芯
    "603350": [("机器人", "电机")],              # 安乃达：两轮电驱→具身智能关节电机
    "603089": [("机器人", "柔性触感")],          # 正裕工业：机器人零部件+柔性触感
    "301502": [("机器人", "无框电机")],          # 华阳智能：机器人关节+无框电机
    "002249": [("机器人", "电机")],              # 大洋电机：机器人关节+电机
    "603933": [("机器人", "工业自动化")],        # 睿能科技：工业自动化+IC分销
    "603528": [("机器人", "本体及应用")],        # 多伦科技：智慧交管+人形机器人
    "603280": [("机器人", "本体及应用")],        # 南方路机：工程搅拌+智能机器人
    # ── 电力 / 军工 ──────────────────────────────────────────────────
    "600192": [("电力", "电力设备")],            # 长城电工：输配电设备+核电项目
    "002535": [("军工", "军工配套")],            # 林州重机：煤矿机械+军工配套
    # ── 数据要素 / AI ────────────────────────────────────────────────
    "603138": [("数据要素", "数据要素_数据存储")],   # 海量数据：数据库+AI应用
    "002076": [("数据要素", "数据要素_其他行业数据")],  # 星光股份：造价通平台（数据要素）
    "601999": [("AI应用", "AI应用")],            # 出版传媒：AI出版
    # ── 光通信 / 锂电 ───────────────────────────────────────────────
    "603330": [("光通信", "CPO/光模块")],        # 天洋新材：电子胶+光模块胶
    "600293": [("锂电池", "电解液")],            # 三峡新材：锂电电解液+汽车玻璃
}

# 明确标错：把某只票从 (cat) 下摘掉、改挂到 NEW
MOVE = {
    "600202": {"from": "冰雪经济", "to": [("电力", "电力设备")]},   # 哈空调：空冷器（电力/石化）
}
# 概念词替换（同一对里换细分）：code -> (cat, old_concept, new_concept)
REPLACE = {
    "300839": ("液冷", "其他", "浸没式冷却液"),   # 博汇股份：液冷系统/浸没式冷却液（无锡极致液冷）
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
    if slot == 0:
        rec["jiuyan_category"] = cat
        rec["jiuyan_concept"] = toks if len(toks) > 1 else (toks[0] if toks else "")
    else:
        rec[f"jiuyan_category{slot}"] = cat
        rec[f"jiuyan_concept{slot}"] = toks if len(toks) > 1 else (toks[0] if toks else "")


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))
    log, skipped = [], []

    def _ensure(rec, code, cat, con):
        ps = _pairs(rec)
        for slot, c, cons in ps:
            if c == cat and con in _toks(cons):
                skipped.append((code, rec.get("name"), f"已有 {cat}/{con}"))
                return
        if not ps:
            _write_pair(rec, 0, cat, [con])
            log.append((code, rec.get("name"), f"（无标签）→ {cat}/{con}"))
            return
        # 只有不带编号的老字段 ⇒ 先下沉镜像到 1 号槽，新类目进 2 号槽
        if len(ps) == 1 and ps[0][0] == 0:
            _, c0, cons0 = ps[0]
            _write_pair(rec, 1, c0, _toks(cons0))          # 镜像
            _write_pair(rec, 2, cat, [con])
            log.append((code, rec.get("name"),
                        f"下沉 {c0}→slot1（镜像），新增 {cat}/{con}→slot2"))
            return
        used = {s for s, _, _ in ps}
        slot = next((i for i in range(1, 10) if i not in used), None)
        if slot is None:
            skipped.append((code, rec.get("name"), "槽位已满(9)"))
            return
        _write_pair(rec, slot, cat, [con])
        log.append((code, rec.get("name"), f"追加 {cat}/{con}→slot{slot}"))

    for code, items in ADD.items():
        rec = data.get(code)
        if not rec:
            skipped.append((code, "?", "不在 watchlist"))
            continue
        for cat, con in items:
            _ensure(rec, code, cat, con)

    for code, mv in MOVE.items():
        rec = data.get(code)
        if not rec:
            continue
        for slot, c, cons in _pairs(rec):
            if c == mv["from"]:
                # 摘掉旧类目：整槽清空
                if slot == 0:
                    rec.pop("jiuyan_category", None)
                    rec.pop("jiuyan_concept", None)
                else:
                    rec.pop(f"jiuyan_category{slot}", None)
                    rec.pop(f"jiuyan_concept{slot}", None)
                log.append((code, rec.get("name"),
                            f"更正：摘掉「{mv['from']}」({_toks(cons)})"))
                break
        for cat, con in mv["to"]:
            _ensure(rec, code, cat, con)

    for code, (cat, old, new) in REPLACE.items():
        rec = data.get(code)
        if not rec:
            continue
        for slot, c, cons in _pairs(rec):
            if c != cat:
                continue
            toks = _toks(cons)
            if old not in toks:
                continue
            toks = [new if t == old else t for t in toks]
            _write_pair(rec, slot, c, toks)
            log.append((code, rec.get("name"), f"更正概念：{cat} {old} → {new}"))

    print(f"改动 {len(log)} 处：")
    for x in log:
        print(f"  {x[0]} {x[1]}  {x[2]}")
    if skipped:
        print(f"\n跳过 {len(skipped)} 处：")
        for x in skipped:
            print(f"  {x[0]} {x[1]}  {x[2]}")

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
