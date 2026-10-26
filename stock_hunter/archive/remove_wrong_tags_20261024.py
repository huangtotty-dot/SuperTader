# -*- coding: utf-8 -*-
"""摘掉 24 只「错标」（owner 2026-10-24「执行」）。

只删**指定的那一个 类目/概念 对**，同一只票的其它标签一律保留。
判词来自前几批人工核对（audit_boards_batch1/2/3 + 数据要素），错标理由见各批报告。

处理规则：
  · 类目/概念按 `|` 拆成 token 列表，按**位置**配对删除（复合标签如 `AI应用|数据要素` 只删对应那一段）；
  · 槽位删空 ⇒ 移除该槽的 category/concept 字段；
  · 删完若该票已无任何概念 ⇒ 连带清掉 `jiuyan_tag_verdict` / `jiuyan_tag_evidence`（它已不在池内）；
    若还有别的标签 ⇒ 判词改成「已摘牌」，避免继续显示成错标；
  · 每只都记 `jiuyan_tag_removed_20261024` 备查（可回滚）。

用法：``python stock_hunter/remove_wrong_tags_20261024.py [--apply]``
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

SRC = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json"
BAK = r"E:\superTrader\stock_hunter\watchlist_jiuyan.json.bak_20261024_removewrong"
MARK = "2026-10-24 21:00:00"

TARGETS = {
    "603776": ("大农业", "农业机器人", "共享出行"),
    "002514": ("大农业", "农业机器人", "光伏组件"),
    "300107": ("低空经济", "eVTOL", "化工中间体"),
    "002831": ("液冷", "快换接头", "包装印刷"),
    "000570": ("电池", "其他关键材料及设备", "柴油机"),
    "000790": ("电池", "电解液", "中成药"),
    "002004": ("电池", "电解液", "农化+医药"),
    "002734": ("电池", "电解液", "农药原药"),
    "002748": ("电池", "电解液", "发泡剂/烧碱"),
    "600293": ("电池", "电解液", "平板玻璃"),
    "688075": ("电池", "正极材料", "POCT试剂"),
    "002701": ("冰雪经济", "培训", "金属包装"),
    "002406": ("超节点", "昇腾部件伙伴", "传动轴"),
    "301077": ("超节点", "昇腾生态运营伙伴", "反光材料"),
    "301011": ("电力", "智能电网-用户侧", "游戏游艺设备"),
    "000565": ("煤化工", "煤化工", "工业涂料"),
    "600984": ("煤化工", "煤化工", "工程机械租赁"),
    "301589": ("商业航天", "火箭", "LED显示控制"),
    "600761": ("商业航天", "spacex-供应商", "叉车"),
    "002725": ("光通信", "光芯片", "铝合金车轮"),
    "600707": ("大消费", "大消费_百货零售", "显示面板"),
    "603988": ("黄金", "黄金_伴生金矿", "电机"),
    "002565": ("太空光伏", "太阳翼结构", "环保纸/新型烟草"),
    "600477": ("太空光伏", "太空光伏组件", "钢结构"),
}


def _slots(rec):
    out = []
    for i in list(range(1, 10)) + [0]:
        k = "jiuyan_category" if i == 0 else f"jiuyan_category{i}"
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        cat = rec.get(k)
        if isinstance(cat, str) and cat.strip():
            out.append((i, k, ck, cat, rec.get(ck)))
    return out


def _tokens(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [t.strip() for t in str(v or "").split("|") if t.strip()]


def _has_concept(rec):
    for i in list(range(1, 10)) + [0]:
        ck = "jiuyan_concept" if i == 0 else f"jiuyan_concept{i}"
        v = rec.get(ck)
        if isinstance(v, list):
            if any(str(x).strip() for x in v):
                return True
        elif isinstance(v, str) and v.strip():
            return True
    return False


def main():
    apply = "--apply" in sys.argv
    data = json.load(open(SRC, encoding="utf-8"))

    def _pool(d):
        return sum(1 for v in d.values() if isinstance(v, dict) and _has_concept(v))

    before = _pool(data)
    log, miss = [], []
    for code, (cat, con, real) in TARGETS.items():
        rec = data.get(code)
        if not isinstance(rec, dict):
            miss.append((code, "不在表"))
            continue
        removed = 0
        for slot, k, ck, catv, conv in _slots(rec):
            cats = _tokens(catv)
            cons = _tokens(conv)
            keep_c, keep_n = [], []
            n = max(len(cats), len(cons))
            for idx in range(n):
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
                continue          # 这个槽没命中
            if not keep_c:
                rec.pop(k, None)
                rec.pop(ck, None)
            else:
                rec[k] = "|".join(keep_c)
                rec[ck] = keep_n[0] if len(keep_n) == 1 else "|".join(keep_n)
        if not removed:
            miss.append((code, f"{cat}/{con} 未找到"))
            continue
        rec["jiuyan_tag_removed_20261024"] = f"{cat}/{con}（实际主营：{real}）"
        if _has_concept(rec):
            rec["jiuyan_tag_verdict"] = "已摘牌"
            d = rec.get("jiuyan_concept_reason")
            if isinstance(d, dict):
                d.pop(con, None)
                if not d:
                    rec.pop("jiuyan_concept_reason", None)
        else:
            rec.pop("jiuyan_tag_verdict", None)
            rec.pop("jiuyan_tag_evidence", None)
            rec.pop("jiuyan_concept_reason", None)
            rec.pop("jiuyan_concept_desc", None)
        rec["updated_at"] = MARK
        log.append((code, rec.get("name"), f"{cat}/{con}", _has_concept(rec)))

    print(f"摘牌 {len(log)} 只（未命中 {len(miss)}）")
    for code, name, pair, still in log:
        print(f"  {code} {name:<7s} 摘掉 {pair}  {'（池内仍有其它标签）' if still else '（已无任何标签 ⇒ 退出猎手池）'}")
    if miss:
        print("未命中：", miss)
    print("池子：%s → %s（退出 %s 只）" % (before, _pool(data), before - _pool(data)))

    if not apply:
        print("\n(dry-run，未写文件；加 --apply 执行)")
        return
    if not os.path.exists(BAK):
        shutil.copy2(SRC, BAK)
    json.dump(data, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已写入。备份: {BAK}")


if __name__ == "__main__":
    main()
