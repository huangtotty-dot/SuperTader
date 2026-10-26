# -*- coding: utf-8 -*-
"""全板块清理 `中军` / `龙头` 分层标签（2026-10-19，owner：「其它板块也都有，也要修掉」）。

半导体那 29 只已由 `tag_semi_drop_midleader_20261019.py` 处理；本脚本扫**全部板块**，
把 concept 字符串里 `中军*` / `龙头*` 的 token 去掉，保留真实细分。

## 为什么可以安全删

- 这些 token 都是 concept 字符串里的**第二段**（如 `电力设备|中军-特高压中军`），
  扫描结果：261 只、0 只会在清理后变空；
- 只改 concept、**category 一个字不碰** ⇒ `sym2board_map`（OSC 板块）不变；
- 清理后 concept 仍非空 ⇒ 猎手池不掉票。
脚本写盘前会再断言一遍，任一为空即拒绝写盘。

## 用法

    python stock_hunter/tag_drop_midleader_all_20261019.py            # 干跑
    python stock_hunter/tag_drop_midleader_all_20261019.py --apply    # 真写（备份 tmp/）
"""
import collections
import datetime
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "stock_hunter", "watchlist_jiuyan.json")


def is_layer(tok: str) -> bool:
    t = str(tok or "").strip()
    return t.startswith("中军") or t.startswith("龙头")


def clean(con: str) -> str:
    toks = [t.strip() for t in str(con or "").split("|") if t.strip()]
    return "|".join(t for t in toks if not is_layer(t))


def slots(v):
    return [(i, str(v.get("jiuyan_category%d" % i) or "").strip(),
             str(v.get("jiuyan_concept%d" % i) or "").strip())
            for i in range(1, 10)
            if str(v.get("jiuyan_category%d" % i) or "").strip()]


def main():
    apply = "--apply" in sys.argv
    d = json.load(open(PATH, encoding="utf-8"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    todo, wrong = [], []
    by_cat = collections.Counter()

    for code, v in sorted(d.items()):
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        sl = slots(v)
        if sl:
            changed = []
            for i, cat, con in sl:
                new = clean(con)
                if new != con:
                    if not new:
                        wrong.append((code, name, "cat%d 清理后为空" % i))
                    else:
                        changed.append((i, cat, con, new))
                        by_cat[cat] += 1
            if changed:
                todo.append((code, name, "slots", changed))
        else:
            cats = [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]
            con = str(v.get("jiuyan_concept") or "").strip()
            new = clean(con)
            if new != con:
                if not new:
                    wrong.append((code, name, "legacy 清理后为空"))
                else:
                    todo.append((code, name, "legacy", (cats, con, new)))
                    for c in cats:
                        by_cat[c] += 1

    print("=" * 78)
    print("全板块「中军/龙头」分层标签清理（%s）" % ("真写" if apply else "干跑"))
    print("=" * 78)
    print("涉及 %d 只票；按板块分布（前 20）：" % len(todo))
    for c, n in by_cat.most_common(20):
        print("  %-12s %d" % (c, n))
    print("\n示例（前 12 只）：")
    for code, name, kind, payload in todo[:12]:
        if kind == "legacy":
            cats, con, new = payload
            print("  %s %-8s %s\n      %r → %r" % (code, name, "|".join(cats), con, new))
        else:
            print("  %s %-8s" % (code, name))
            for i, cat, con, new in payload:
                print("      cat%d/%s: %r → %r" % (i, cat, con, new))
    print("-" * 78)
    print("待清理 %d 只 · 拒绝 %d" % (len(todo), len(wrong)))
    for c, n, why in wrong:
        print("  ⚠️ %s %-8s %s" % (c, n, why))

    if not apply:
        print("\n（干跑，未写。加 --apply 真写）")
        return
    if wrong:
        print("\n有拒绝项 ⇒ 不写盘（先人工确认）")
        return

    for code, name, kind, payload in todo:
        v = d[code]
        if kind == "legacy":
            cats, con, new = payload
            v["jiuyan_concept"] = new
            v["jiuyan_cleanup_note_2026_10_19_all"] = "去掉分层标签：%r → %r" % (con, new)
        else:
            for i, cat, con, new in payload:
                v["jiuyan_concept%d" % i] = new
                v.setdefault("jiuyan_cleanup_note_2026_10_19_all", []).append(
                    "cat%d/去掉分层标签：%r → %r" % (i, con, new))
        v["updated_at"] = now

    # 写盘前自证：**只检查本次改过的票**，任一 (cat, concept) 不得为空。
    # （全库检查会被既有脏数据挡住——如 000537 本来就有 category 但 concept 为空，
    #   那不是本次清理造成的，不该阻断写盘。）
    for code, name, kind, payload in todo:
        v = d[code]
        sl = slots(v)
        pairs = ([(c, n) for _, c, n in sl] if sl else
                 [(c, str(v.get("jiuyan_concept") or "").strip())
                  for c in [p.strip() for p in str(v.get("jiuyan_category") or "").split("|") if p.strip()]])
        for c, n in pairs:
            if not str(n).strip():
                raise SystemExit("自证失败：%s (%s) 清理后 concept 为空，拒绝写盘" % (code, c))

    bdir = os.path.join(ROOT, "tmp")
    os.makedirs(bdir, exist_ok=True)
    bak = os.path.join(bdir, "watchlist_jiuyan.json.bak_%s_drop_midleader_all"
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
