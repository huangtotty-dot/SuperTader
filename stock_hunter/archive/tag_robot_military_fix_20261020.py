"""机器人 + 军工板块系统性整治（2026-10-20，owner 指令）。

背景：owner 发现 GUI 中机器人/军工板块公司太多、分层不清，要求：
  1. 核实真正的龙头/中军并在相应列改正；
  2. 二级概念简化；
  3. 补入宇树科技（688836，2026-08-19 科创板上市）。

依据（两份研究结论，网络核查 2025-2026 公开研报/公告）：
  tmp/机器人_研究结论.json —— 17 家角色标记 / 19 条概念修正 / 减速器合并 / 补 7 家
  tmp/军工_研究结论.json   —— 30 家角色标记 / 45 条概念修正 / 6 组合并 / 补 12 家

施工口径（owner 未示处按合理默认，报告列明可回滚）：
  - 角色写**独立字段** `jiuyan_role`（龙头/中军）+ `jiuyan_role_reason`，
    **不**再塞 jiuyan_concept（2026-10-19 清理分层标签污染的教训）；
  - 「剔除」= 摘除该板块的 (category, concept) 对；票无剩余板块对时
    legacy category/concept 置空（掉出打分池），逐票留 `jiuyan_exclude_note_2026_10_20`；
  - 「军工属性弱」类 fix：concept 写干净值 + `jiuyan_concept_note` 注记，不剔除；
  - 合并兜底（fixes 未覆盖的残留 token）：分支系统/基础配套产品→军工电子；
    火箭军/火箭→导弹；国家队→删除；谐波减速器/行星减速器→减速器；
  - 顺带清理 10-19 漏网的 concept 字段残留「龙头-/中军-」token（全表，58 只）。

用法：python tag_robot_military_fix_20261020.py        # 干跑
      python tag_robot_military_fix_20261020.py --apply # 真写
幂等：重复运行无新增变更。
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WL = ROOT / "watchlist_jiuyan.json"
BAK = ROOT / "watchlist_jiuyan.backup_2026-10-20_机器人军工整治.json"
TMP = ROOT.parent / "tmp"

APPLY = "--apply" in sys.argv
TODAY = "2026-10-20"

# ---------- 研究结论载入 ----------
robot = json.loads((TMP / "机器人_研究结论.json").read_text(encoding="utf-8"))
mil = json.loads((TMP / "军工_研究结论.json").read_text(encoding="utf-8"))

# missing 中确认的三家军工主机厂级龙头（reason 取自研究结论 missing 条目）
MISSING_ROLE = {"302132", "002179", "600038", "688836"}

# 军工合并兜底（fix 未覆盖的残留 token）
MIL_TOKEN_MAP = {"分支系统": "军工电子", "基础配套产品": "军工电子",
                 "火箭军": "导弹", "火箭": "导弹"}
MIL_TOKEN_DROP = {"国家队"}
# 机器人合并兜底
ROBOT_TOKEN_MAP = {"谐波减速器": "减速器", "行星减速器": "减速器"}

# 剔除票（摘除该板块对）
ROBOT_DROP = {"600038", "603859"}   # 中直股份→搬到军工；能科科技→移出
MIL_DROP = {"002368", "600271", "000625", "600006", "600262"}
EXCLUDE_REASON = {
    "603859": "剔除：工业软件/数字孪生主业，与机器人硬件产业链关联弱",
    "002368": "剔除：政务信息化/信创主业，军品收入占比极低",
    "600271": "剔除：税控/政务信息化主业，军工属性弱",
    "000625": "剔除：乘用车主业，军工资产不在上市公司体内",
    "600006": "剔除：轻型商用车主业，军品占比无公开支撑",
    "600262": "剔除：矿用自卸车民品为主，军品占比很小",
}

# 「军工属性弱」fix 的干净标签（研究结论 new 值含括号注释，拆为 标签+注记）
MIL_WEAK_CLEAN = {
    "300627": ("卫星导航", "军工属性弱：高精度定位以农机/测绘民用为主"),
    "002222": ("激光晶体", "军工属性弱：非线性光学晶体以民用为主"),
    "688070": ("工业无人机", "军工属性弱：以测绘/巡检民用为主"),
    "300024": ("工业机器人", "军工属性弱：新松主业工业机器人/半导体自动化设备"),
}
# 航天动力单独修正（兜底规则会把它错标成纯「导弹」）
MIL_EXTRA_FIX = {"600343": "航天发动机|导弹动力"}

# 新增/补标签（票均已存在于 watchlist，仅补板块对）
ADD = {
    # 机器人
    "688836": ("机器人", "本体及应用"),
    "688585": ("机器人", "本体及应用"),
    "300024": ("机器人", "本体及应用"),
    "300660": ("机器人", "电机"),
    "600580": ("机器人", "电机"),
    "301510": ("机器人", "控制器"),
    "688722": ("机器人", "腱绳材料"),
    # 军工
    "302132": ("军工", "战斗机主机厂|空军|军贸"),
    "002179": ("军工", "军工电子|军用连接器"),
    "600879": ("军工", "导弹|航天配套|无人机"),
    "603267": ("军工", "军工电子|军用MLCC"),
    "688375": ("军工", "雷达|T/R组件|导弹"),
    "688708": ("军工", "军工材料|隐身材料"),
    "688333": ("军工", "军工材料|金属3D打印"),
    "688543": ("军工", "导弹|弹药"),
    "301050": ("军工", "导弹|精确制导"),
    "600118": ("军工", "卫星"),
    "688563": ("军工", "军工材料"),
}
ADD_REASON = {m["code"]: m["reason"] for m in robot["missing"] + mil["missing"]}

changes = []


def norm_tokens(v):
    """concept 值 → token 列表（兼容 str | 串 / list）。"""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        src = v
    else:
        src = str(v).split("|")
    return [str(x).strip() for x in src if str(x).strip()]


def dump_tokens(tokens):
    """多标签用 list（医药整合起的新惯例），单标签用 str。"""
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return list(tokens)


def pairs_of(rec):
    """读取 (category, concept_tokens) 编号槽对；无编号槽回退 legacy。"""
    pairs = []
    for i in range(1, 10):
        c = str(rec.get(f"jiuyan_category{i}") or "").strip()
        if c:
            pairs.append([c, norm_tokens(rec.get(f"jiuyan_concept{i}"))])
    if not pairs:
        c0 = str(rec.get("jiuyan_category") or "").strip()
        if c0:
            for c in c0.split("|"):
                c = c.strip()
                if c:
                    pairs.append([c, norm_tokens(rec.get("jiuyan_concept"))])
    return pairs


def write_pairs(rec, pairs):
    """回写编号槽 + 镜像 legacy 字段（与 data_loader 读取口径对齐）。"""
    for i in range(1, 10):
        rec.pop(f"jiuyan_category{i}", None)
        rec.pop(f"jiuyan_concept{i}", None)
    if not pairs:
        rec["jiuyan_category"] = ""
        rec["jiuyan_concept"] = ""
        return
    for i, (c, tokens) in enumerate(pairs, 1):
        rec[f"jiuyan_category{i}"] = c
        rec[f"jiuyan_concept{i}"] = dump_tokens(tokens)
    # legacy 镜像 = 第一对（data_loader 仅在不带编号槽时回退，写上保持一致性）
    rec["jiuyan_category"] = pairs[0][0]
    rec["jiuyan_concept"] = dump_tokens(pairs[0][1])


def mark(code, what, old, new):
    changes.append((code, what, str(old)[:120], str(new)[:120]))


def set_role(rec, code, role, reason):
    if rec.get("jiuyan_role") == role and rec.get("jiuyan_role_reason") == reason:
        return
    mark(code, "role", rec.get("jiuyan_role"), f"{role}｜{reason[:40]}")
    rec["jiuyan_role"] = role
    rec["jiuyan_role_reason"] = reason


def fix_pair_concept(rec, code, cat, new_tokens, note=None):
    """把指定板块的 concept 改写为 new_tokens。"""
    pairs = pairs_of(rec)
    for p in pairs:
        if p[0] == cat:
            old = dump_tokens(p[1])
            if p[1] != new_tokens:
                mark(code, f"{cat}.concept", old, dump_tokens(new_tokens))
                p[1] = list(new_tokens)
                write_pairs(rec, pairs)
            if note:
                rec["jiuyan_concept_note"] = note
            return True
    return False


def drop_pair(rec, code, cat, reason):
    pairs = pairs_of(rec)
    left = [p for p in pairs if p[0] != cat]
    if len(left) == len(pairs):
        return False
    mark(code, f"剔除.{cat}", [p[0] for p in pairs], [p[0] for p in left] or "(空)")
    write_pairs(rec, left)
    rec["jiuyan_exclude_note_2026_10_20"] = reason
    # 摘除后不再保留 role
    rec.pop("jiuyan_role", None)
    rec.pop("jiuyan_role_reason", None)
    return True


def add_pair(rec, code, cat, concept_str, reason=None):
    pairs = pairs_of(rec)
    if any(p[0] == cat for p in pairs):
        return False  # 已有该板块对 → 走 fix，不重复加
    tokens = [t.strip() for t in concept_str.split("|") if t.strip()]
    pairs.append([cat, tokens])
    write_pairs(rec, pairs)
    mark(code, f"补入.{cat}", "", concept_str)
    if reason:
        rec.setdefault("jiuyan_concept_note", reason)
    return True


def clean_layer_tokens(rec, code):
    """清理 concept 里残留的 龙头-/中军- token（10-19 漏网，全表适用）。"""
    def scrub(tokens):
        return [t for t in tokens if not t.startswith(("龙头-", "中军-")) and t not in ("龙头", "中军")]
    touched = False
    for k in ["jiuyan_concept"] + [f"jiuyan_concept{i}" for i in range(1, 10)]:
        if k not in rec:
            continue
        tokens = norm_tokens(rec.get(k))
        new = scrub(tokens)
        if new != tokens:
            mark(code, f"清理分层残留.{k}", dump_tokens(tokens), dump_tokens(new))
            rec[k] = dump_tokens(new)
            touched = True
    return touched


def main():
    wl = json.loads(WL.read_text(encoding="utf-8"))

    # ---------- 0. 全表分层标签残留清理 ----------
    n_clean = 0
    for code, rec in wl.items():
        if isinstance(rec, dict) and clean_layer_tokens(rec, code):
            n_clean += 1
    print(f"[0] 分层标签残留清理: {n_clean} 只")

    # ---------- 1. 角色标记 ----------
    n_role = 0
    for src in (robot["role_marks"], mil["role_marks"]):
        for code, r in src.items():
            rec = wl.get(code)
            if not isinstance(rec, dict):
                mark(code, "role.缺票", "", r["name"])
                continue
            set_role(rec, code, r["role"], r["reason"])
            n_role += 1
    print(f"[1] 角色标记写入: {n_role} 只")

    # ---------- 2. 机器人 concept 修正 ----------
    for code, fx in robot["concept_fixes"].items():
        rec = wl.get(code)
        if not isinstance(rec, dict):
            continue
        new = fx["new"]
        if new == "剔除":
            continue  # 剔除走第 4 步统一处理
        fix_pair_concept(rec, code, "机器人", [t for t in new.split("|") if t])

    # ---------- 3. 军工 concept 修正 ----------
    for code, fx in mil["concept_fixes"].items():
        rec = wl.get(code)
        if not isinstance(rec, dict):
            continue
        new = fx["new"]
        if new.startswith("剔除"):
            continue
        if code in MIL_WEAK_CLEAN:
            clean, note = MIL_WEAK_CLEAN[code]
            fix_pair_concept(rec, code, "军工", [clean], note=note)
        else:
            fix_pair_concept(rec, code, "军工", [t for t in new.split("|") if t])
    for code, new in MIL_EXTRA_FIX.items():
        rec = wl.get(code)
        if isinstance(rec, dict):
            fix_pair_concept(rec, code, "军工", new.split("|"))

    # ---------- 4. 剔除 ----------
    for code in ROBOT_DROP:
        rec = wl.get(code)
        if isinstance(rec, dict) and code != "600038":  # 中直股份单独搬军工
            drop_pair(rec, code, "机器人", EXCLUDE_REASON.get(code, "剔除"))
    for code in MIL_DROP:
        rec = wl.get(code)
        if isinstance(rec, dict):
            drop_pair(rec, code, "军工", EXCLUDE_REASON.get(code, "剔除"))

    # ---------- 5. 合并兜底（板块内未被 fix 覆盖的残留 token） ----------
    n_merge = 0
    for code, rec in wl.items():
        if not isinstance(rec, dict):
            continue
        pairs = pairs_of(rec)
        touched = False
        for p in pairs:
            new = []
            for t in p[1]:
                if p[0] == "机器人" and t in ROBOT_TOKEN_MAP:
                    t = ROBOT_TOKEN_MAP[t]
                    touched = True
                elif p[0] == "军工":
                    if t in MIL_TOKEN_DROP:
                        touched = True
                        continue
                    if t in MIL_TOKEN_MAP:
                        t = MIL_TOKEN_MAP[t]
                        touched = True
                if t not in new:
                    new.append(t)
            if new != p[1]:
                mark(code, f"合并兜底.{p[0]}", dump_tokens(p[1]), dump_tokens(new))
                p[1] = new
                touched = True
        if touched:
            write_pairs(rec, pairs)
            n_merge += 1
    print(f"[5] 合并兜底改写: {n_merge} 只")

    # ---------- 6. 中直股份搬家：摘机器人对，军工对改写 ----------
    rec = wl.get("600038")
    if isinstance(rec, dict):
        if drop_pair(rec, "600038", "机器人", "错标机器人（主业直升机），搬入军工"):
            rec.pop("jiuyan_exclude_note_2026_10_20", None)  # 不是剔除出池，是搬家
        fix_pair_concept(rec, "600038", "军工", ["直升机主机厂", "空军", "陆军"])
        if "jiuyan_role" not in rec:
            set_role(rec, "600038", "龙头", ADD_REASON.get("600038", "国内直升机总装唯一上市平台"))

    # ---------- 7. 补标签 ----------
    n_add = 0
    for code, (cat, concept) in ADD.items():
        rec = wl.get(code)
        if not isinstance(rec, dict):
            mark(code, "补入.缺票", "", f"{cat}/{concept}")
            continue
        if add_pair(rec, code, cat, concept, ADD_REASON.get(code)):
            n_add += 1
        if code in MISSING_ROLE and "jiuyan_role" not in rec:
            role_reason = ADD_REASON.get(code, "")
            set_role(rec, code, "龙头", role_reason)
    print(f"[7] 补标签: {n_add} 只")

    # ---------- 汇总 ----------
    print(f"\n变更 {len(changes)} 条（{'真写' if APPLY else '干跑'}）")
    from collections import Counter
    for k, v in Counter(c[1].split(".")[0] for c in changes).most_common():
        print(f"  {k}: {v}")

    if APPLY:
        shutil.copy2(WL, BAK)
        WL.write_text(json.dumps(wl, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"已写回 {WL.name}（备份 {BAK.name}）")
    else:
        print("\n-- 抽样预览 --")
        for c in changes[:25]:
            print(" ", c[0], c[1], "|", c[2], "→", c[3])


if __name__ == "__main__":
    main()
