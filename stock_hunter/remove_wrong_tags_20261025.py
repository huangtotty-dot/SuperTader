# -*- coding: utf-8 -*-
"""摘掉最后 14 只错标（owner 2026-10-25「执行」）。

只删指定的「类目/概念」对，同一只票的其它标签保留；每只留 `jiuyan_tag_removed_20261025` 备查。
复合槽位（如 金风科技「商业航天|煤化工|电力」）按 token 位置精确删除。

用法：``python stock_hunter/remove_wrong_tags_20261025.py [--apply]``
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261025_removewrong"
MARK = "2026-10-25 16:00:00"

# code -> [(类目, 概念, 实际主营)]
TARGETS = {
    "002654": [("半导体", "存储模组", "LED 业务")],
    "605598": [("太空光伏", "太空光伏组件", "岩土工程")],
    "002685": [("半导体", "算力芯片", "集装箱装卸设备")],
    "603306": [("半导体", "半导体材料", "汽车安全气囊布")],
    "603025": [("液冷", "压缩机", "智能装备电控系统"),
               ("液冷", "磁悬浮压缩机", "智能装备电控系统"),
               ("PCB", "电子布", "智能装备电控系统")],
    "300750": [("太空光伏", "太空光伏组件", "动力电池")],
    "603488": [("军工", "军工电子", "电梯门系统")],
    "605287": [("AI应用", "AI影视短剧", "装饰装修")],
    "603137": [("半导体", "存储模组", "幕墙门窗工程")],
    "688611": [("太空光伏", "太空光伏组件", "电网智能感知")],
    "300129": [("商业航天", "火箭", "风电塔筒")],
    "002799": [("AI应用", "AI营销", "医药包装")],
    "003018": [("液冷", "液冷模组", "包装产品")],
    "002202": [("商业航天", "火箭", "风电整机")],
}


def slots(rec):
    out = []
    for i in list(range(1, 10)) + [0]:
        k = "jiuyan_category" if i == 0 else f"jiuyan_category{i}"
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        cat = rec.get(k)
        if isinstance(cat, str) and cat.strip():
            out.append((i, k, ck, cat, rec.get(ck)))
    return out


def toks(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [t.strip() for t in str(v or "").split("|") if t.strip()]


def has_concept(rec):
    for i in list(range(1, 10)) + [0]:
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        v = rec.get(ck)
        if isinstance(v, list) and any(str(x).strip() for x in v):
            return True
        if isinstance(v, str) and v.strip():
            return True
    return False


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))

    def pool(d):
        return sum(1 for v in d.values() if isinstance(v, dict) and has_concept(v))

    before = pool(data)
    log, miss = [], []
    for code, pairs in TARGETS.items():
        rec = data.get(code)
        if not isinstance(rec, dict):
            miss.append((code, "不在表")); continue
        removed = 0
        for cat, con, real in pairs:
            for slot, k, ck, catv, conv in slots(rec):
                cats, cons = toks(catv), toks(conv)
                keep_c, keep_n = [], []
                for idx in range(max(len(cats), len(cons))):
                    c = cats[idx] if idx < len(cats) else ""
                    v = cons[idx] if idx < len(cons) else ""
                    if c == cat and v == con:
                        removed += 1
                        continue
                    if c:
                        keep_c.append(c)
                    if v:
                        keep_n.append(v)
                if len(keep_c) == len(cats) and len(keep_n) == len(cons):
                    continue
                if not keep_c:
                    rec.pop(k, None); rec.pop(ck, None)
                else:
                    rec[k] = "|".join(keep_c)
                    rec[ck] = keep_n[0] if len(keep_n) == 1 else "|".join(keep_n)
        if not removed:
            miss.append((code, "未找到标签")); continue
        rec["jiuyan_tag_removed_20261025"] = "、".join(f"{c}/{n}（实际主营：{r}）" for c, n, r in pairs)
        if has_concept(rec):
            rec["jiuyan_tag_verdict"] = "已摘牌"
            d = rec.get("jiuyan_concept_reason")
            if isinstance(d, dict):
                for _, n, _ in pairs:
                    d.pop(n, None)
                if not d:
                    rec.pop("jiuyan_concept_reason", None)
        else:
            for f in ("jiuyan_tag_verdict", "jiuyan_tag_evidence", "jiuyan_concept_reason"):
                rec.pop(f, None)
        rec["updated_at"] = MARK
        log.append((code, rec.get("name"), len(pairs), has_concept(rec)))

    print(f"摘牌 {len(log)} 只（未命中 {len(miss)}）")
    for code, name, n, still in log:
        print(f"  {code} {name:<7s} 摘掉 {n} 对  {'（保留其它标签）' if still else '（退出猎手池）'}")
    if miss:
        print("未命中：", miss)
    print(f"池子：{before} → {pool(data)}（退出 {before - pool(data)} 只）")
    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
