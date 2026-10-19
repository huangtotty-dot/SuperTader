# -*- coding: utf-8 -*-
"""韭研「半导体」概念清理：去掉 `中军` / `龙头` 这类**分层标签**（2026-10-19，owner）。

owner 口径：半导体下已经有 `半导体设备` / `半导体材料` / `封测` 等真实细分，
`中军` / `龙头` 只是分层描述（如 `封测|龙头-AMD封测主力`），**纯属多余**，需要去掉。

## 影响面（先查后改）

扫描 `watchlist_jiuyan.json`：半导体 category 下含 `中军`/`龙头` token 的票 **29 只**，
全部是 legacy 字段、且每只的 concept 都是「**真实细分 + 一个分层标签**」两段：

    封测|龙头-AMD封测主力      → 封测
    半导体设备|中军-平台型设备龙头 → 半导体设备
    ...

⇒ 去掉标签后 **每只都仍有真实细分**，因此：
  · 猎手池（韭研概念非空）**不会少票**；
  · `sym2board_map`（只读 category）**完全不受影响**，29 只仍在「半导体」板块。
脚本会在写盘前再次断言「清理后 concept 非空」，任一为空则拒绝写盘。

## 用法

    python stock_hunter/tag_semi_drop_midleader_20261019.py            # 干跑
    python stock_hunter/tag_semi_drop_midleader_20261019.py --apply    # 真写（备份到 tmp/）
"""
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")

SUFFIXES = ("中军", "龙头")


def is_layer_token(tok: str) -> bool:
    t = str(tok or "").strip()
    return t.startswith("中军") or t.startswith("龙头")


def slots(v):
    return [(i, str(v.get("jiuyan_category%d" % i) or "").strip(),
             str(v.get("jiuyan_concept%d" % i) or "").strip())
            for i in range(1, 10)
            if str(v.get("jiuyan_category%d" % i) or "").strip()]


def clean(con: str) -> str:
    toks = [t.strip() for t in str(con or "").split("|") if t.strip()]
    return "|".join(t for t in toks if not is_layer_token(t))


def main():
    apply = "--apply" in sys.argv
    d = json.load(open(PATH, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    todo, skipped, wrong = [], [], []

    for code, v in sorted(d.items()):
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        sl = slots(v)
        # 本批 29 只全部是 legacy；出现编号槽位的票**拒绝自动改**，交人工确认
        if sl:
            for i, cat, con in sl:
                if cat == "半导体" and any(is_layer_token(t) for t in con.split("|")):
                    wrong.append((code, name, "编号槽位 cat%d/半导体 含分层标签，拒绝自动改" % i))
            continue
        cats = [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]
        con = str(v.get("jiuyan_concept") or "").strip()
        if "半导体" not in cats or not any(is_layer_token(t) for t in con.split("|")):
            continue
        new = clean(con)
        if not new:
            wrong.append((code, name, "清理后会变空（拒绝）: %r" % con))
            continue
        if new == con:
            skipped.append((code, name, "无变化"))
            continue
        todo.append((code, name, cats, con, new))

    print("=" * 78)
    print("半导体「中军/龙头」分层标签清理（%s）" % ("真写" if apply else "干跑"))
    print("=" * 78)
    for code, name, cats, con, new in todo:
        print("%s %-8s %s\n    %r → %r" % (code, name, "|".join(cats), con, new))
    print("-" * 78)
    print("待清理 %d 只 · 跳过 %d · 拒绝 %d" % (len(todo), len(skipped), len(wrong)))
    for c, n, why in wrong:
        print("  ⚠️ %s %-8s %s" % (c, n, why))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    if wrong:
        print("\n有拒绝项 ⇒ 不写盘（先人工确认）")
        return

    for code, name, cats, con, new in todo:
        v = d[code]
        v["jiuyan_concept"] = new
        v["jiuyan_cleanup_note_2026_10_19"] = "去掉分层标签：%r → %r" % (con, new)
        v["updated_at"] = now

    # 写盘前自证：所有半导体 pair 清理后非空
    for code, v in d.items():
        if not isinstance(v, dict):
            continue
        sl = slots(v)
        pairs = ([(c, n) for _, c, n in sl] if sl else
                 [(c, str(v.get("jiuyan_concept") or "").strip())
                  for c in [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]])
        for c, n in pairs:
            if c == "半导体" and not str(n).strip():
                raise SystemExit("自证失败：%s 半导体 concept 为空，拒绝写盘" % code)

    bdir = os.path.join(ROOT, "tmp")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, "watchlist_jiuyan.json.bak_%s_drop_midleader"
                       % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(PATH, bak)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)
    print("\n已写入 %s（indent=1）" % PATH)
    print("备份 %s" % bak)


if __name__ == "__main__":
    main()
