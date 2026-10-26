"""半导体·存储概念系统性整治（2026-10-20 晚，owner 指令）。

背景：owner 发现半导体分类下「存储」概念公司多而杂，且漏掉长鑫科技。
三路研究结论（2026-10-20 网络核查，公告/年报/财新/证券时报等）：
  tmp/存储_长鑫专项.json   —— 长鑫科技 688825 已于 2026-07-27 科创板上市
                             （全球第四大 DRAM 原厂、国产唯一大规模量产 IDM），
                             表内条目为无标签占位，需补标签+龙头
  tmp/存储_设计制造.json   —— 设计/接口/主控环节龙头中军 + 12 只成色核验
  tmp/存储_模组封测.json   —— 模组/封测/分销/整机环节龙头中军 + 16 只成色核验

施工口径：
  - 环节标签统一：存储芯片-DRAM / 存储芯片-车规存储 / 存储芯片-NOR Flash /
    存储芯片-SLC NAND / 存储芯片-EEPROM / 存储接口 / 存储主控 /
    存储模组 / 存储封测 / 存储分销 / 存储系统；相关设备材料用
    半导体设备-存储测试 / 半导体设备-HBM检测 / 半导体材料-HBM塑封料；
  - 角色写独立字段 jiuyan_role + jiuyan_role_reason（同 10-20 机器人/军工口径）；
  - 「剔除」= 摘除半导体对或删存储 token，逐票留 jiuyan_exclude_note_2026_10_20；
  - 紫光国微的 龙头 role 是军工身份给的，摘半导体对时**保留**；
  - 成都华微改判军工（特种IC平台，自述存储仅特种 NOR/EEPROM/SRAM）；
  - 顺带修正 300223 名称「君正股份」→「北京君正」；全表完全相同槽对去重
    （朗科/协创的 半导体/半导体 重复槽）。

用法：python tag_storage_fix_20261020.py          # 干跑
      python tag_storage_fix_20261020.py --apply  # 真写
幂等：重复运行无新增变更。
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WL = ROOT / "watchlist_jiuyan.json"
BAK = ROOT / "watchlist_jiuyan.backup_2026-10-20_存储整治.json"

APPLY = "--apply" in sys.argv

changes = []


def norm_tokens(v):
    if v is None:
        return []
    src = v if isinstance(v, (list, tuple)) else str(v).split("|")
    return [str(x).strip() for x in src if str(x).strip()]


def dump_tokens(tokens):
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0]
    return list(tokens)


def pairs_of(rec):
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


def fix_pair_concept(rec, code, cat, new_tokens):
    pairs = pairs_of(rec)
    for p in pairs:
        if p[0] == cat:
            if p[1] != new_tokens:
                mark(code, f"{cat}.concept", dump_tokens(p[1]), dump_tokens(new_tokens))
                p[1] = list(new_tokens)
                write_pairs(rec, pairs)
            return True
    return False


def drop_pair(rec, code, cat, reason, keep_role=False):
    pairs = pairs_of(rec)
    left = [p for p in pairs if p[0] != cat]
    if len(left) == len(pairs):
        return False
    mark(code, f"剔除.{cat}", [p[0] for p in pairs], [p[0] for p in left] or "(空)")
    write_pairs(rec, left)
    rec["jiuyan_exclude_note_2026_10_20"] = reason
    if not keep_role:
        rec.pop("jiuyan_role", None)
        rec.pop("jiuyan_role_reason", None)
    return True


def add_pair(rec, code, cat, concept_str, note=None):
    pairs = pairs_of(rec)
    if any(p[0] == cat for p in pairs):
        return False
    tokens = [t.strip() for t in concept_str.split("|") if t.strip()]
    pairs.append([cat, tokens])
    write_pairs(rec, pairs)
    mark(code, f"补入.{cat}", "", concept_str)
    if note:
        rec.setdefault("jiuyan_concept_note", note)
    return True


# ---------- 角色标记（研究核实，理由一句话） ----------
ROLE = {
    # 龙头
    "688825": ("龙头", "中国第一/全球第四大DRAM原厂(IDM)，国产DRAM唯一大规模量产，2026-07-27科创板上市"),
    "603986": ("龙头", "利基NOR全球第二(18.5%)、利基DRAM国内唯一规模化设计，与长鑫深度绑定"),
    "688008": ("龙头", "内存接口芯片全球第一（市占36.8%，DDR5份额>50%）"),
    "300223": ("龙头", "车规存储龙头：车规SRAM全球第一、利基DRAM全球第六国内第一"),
    "301308": ("龙头", "存储模组绝对龙头：2026H1净利105.77亿，A股模组唯一半年破百亿"),
    "688525": ("龙头", "端侧AI存储+封测一体龙头（eMMC/UFS/存储卡，自有封测厂）"),
    "000021": ("龙头", "存储封测龙头：沛顿承接长鑫超半数委外封测，大基金二期参股合肥沛顿"),
    "600667": ("龙头", "存储封测龙头：海太独家承接SK海力士DRAM后工序至2030年"),
    "300475": ("龙头", "SK海力士HBM唯一官方代理分销商，2026H1营收602亿"),
    "688449": ("龙头", "SSD主控独立第三方全球第二（约25-30%）、大陆第一"),
    "688123": ("龙头", "EEPROM全球前三、DDR5 SPD全球第二（市占40%+）"),
    "300302": ("龙头", "A股唯一纯企业级存储系统厂商，信创/军工领先"),
    # 中军
    "688766": ("中军", "NOR Flash全球第六，利基存储第二梯队"),
    "688110": ("中军", "SLC NAND国内前三、1xnm制程领先"),
    "688385": ("中军", "EEPROM国内第一（FPGA为主业，特种+存储双线）"),
    "001309": ("中军", "主控自研+AI服务器SSD，模组第二梯队"),
    "001287": ("中军", "境内最大电子元器件分销商，存储器收入占42.5%"),
}

# ---------- 半导体槽 concept 改写（目标 tokens） ----------
FIX = {
    "603986": ["存储芯片-NOR Flash", "存储芯片-DRAM"],
    "300223": ["存储芯片-车规存储"],
    "688416": ["存储芯片-NOR Flash"],          # 原误标 DRAM；NOR 占营收83%
    "688110": ["存储芯片-SLC NAND"],
    "688123": ["存储芯片-EEPROM"],
    "688766": ["存储芯片-NOR Flash"],
    "688385": ["存储芯片-EEPROM"],
    "688008": ["存储接口"],
    "688449": ["存储主控"],
    "300857": ["存储模组"],                     # 原 存储芯片|存储芯片 重复
    "300042": ["存储模组"],                     # 原 DRAM 误标+重复槽
    "688525": ["存储模组", "存储封测"],
    "000021": ["存储封测"],
    "600667": ["半导体设备", "存储封测"],
    "300475": ["存储分销"],
    "300302": ["存储系统"],
    "300672": ["端侧AI芯片"],                   # 删存储（固态存储占营收仅0.56%）
    "002409": ["半导体材料"],                   # 删存储（前驱体材料商口径）
    "688300": ["半导体材料"],                   # 删存储（硅微粉填料；对齐 legacy）
    "603283": ["半导体设备-HBM检测"],
    "688419": ["半导体设备-封装设备"],          # 删存储（HBM塑封设备尚在试样）
    "688627": ["半导体设备-存储测试"],
    "688535": ["半导体材料-HBM塑封料"],
}
FIX_NOTE = {
    "300672": "剔除存储标签：2026H1固态存储仅占营收0.56%，主业已转智慧视觉/端侧AI",
    "002409": "剔除存储标签：前驱体/电子特气材料商口径（供存储厂但非存储厂）",
    "688300": "剔除存储标签：硅微粉封装填料商口径",
    "688419": "剔除存储标签：HBM塑封设备试样阶段无批量订单",
    "300857": "标签去重+归位：数据存储设备制造归存储模组",
    "300042": "标签去重+纠错：U盘/模组厂，原DRAM标签系误标",
    "688416": "纠错：主业NOR Flash(占营收83%)，原DRAM标签系误标",
    "688627": "精确化：存储测试设备商，绑定长鑫",
    "688535": "精确化：唯一量产HBM专用GMC塑封料",
}

# ---------- 整对剔除 / 改判 ----------
DROP = {  # code: (摘的板块, 理由, keep_role)
    "688228": ("半导体", "剔除：主业AI大模型/政务软件，无存储硬件收入（蹭概念）", False),
    "300991": ("半导体", "剔除：连接器主业，存储互连仅占收入23%（连接器在超节点槽）", False),
    "002049": ("半导体", "剔除存储标签：特种存储器属特种IC口径，军工标签已覆盖；军工龙头role保留", True),
}
# 成都华微改判军工
REJUDGE = {"688709": ("军工", ["军工电子-特种集成电路"],
                      "改判：中国电子旗下特种IC平台，自述存储仅特种NOR/EEPROM/SRAM，原DRAM标签系误标")}

# ---------- 补标签（票已在表） ----------
ADD = {  # code: (category, concept, note)
    "688825": ("半导体", "存储芯片-DRAM", "补标签：2026-07-27科创板上市，全球第四大DRAM原厂"),
    "002654": ("半导体", "存储模组", "补标签：长江存储嫡系模组+封测，2026H1存储营收+54.9%"),
    "001287": ("半导体", "存储分销", "补标签：境内最大电子元器件分销商"),
    "301099": ("半导体", "存储分销", "补标签：海力士/三星存储分销占营收70%+"),
    "000062": ("半导体", "存储分销", "追加：2026H1存储出货额+330%，占分销收入超40%"),
    "300975": ("半导体", "存储分销", "追加：长鑫DRAM核心一级代理，占其收入25-35%"),
}
ADD_ROLE = {"688825": ROLE["688825"], "001287": ROLE["001287"]}


def main():
    wl = json.loads(WL.read_text(encoding="utf-8"))

    # ---------- 0. 名称修正 ----------
    rec = wl.get("300223")
    if isinstance(rec, dict) and rec.get("name") != "北京君正":
        mark("300223", "name", rec.get("name"), "北京君正")
        rec["name"] = "北京君正"

    # ---------- 1. 全表完全相同槽对去重（朗科/协创的重复槽） ----------
    n_dedup = 0
    for code, rec in wl.items():
        if not isinstance(rec, dict):
            continue
        pairs = pairs_of(rec)
        seen, out = set(), []
        for p in pairs:
            k = (p[0], tuple(p[1]))
            if k not in seen:
                seen.add(k)
                out.append(p)
        if len(out) != len(pairs):
            mark(code, "槽位去重", [p[0] for p in pairs], [p[0] for p in out])
            write_pairs(rec, out)
            n_dedup += 1
    print(f"[1] 槽位去重: {n_dedup} 只")

    # ---------- 2. concept 修正 ----------
    n_fix = 0
    for code, tokens in FIX.items():
        rec = wl.get(code)
        if isinstance(rec, dict) and fix_pair_concept(rec, code, "半导体", tokens):
            n_fix += 1
            if code in FIX_NOTE:
                rec["jiuyan_concept_note"] = FIX_NOTE[code]
    print(f"[2] concept 修正: {n_fix} 只")

    # ---------- 3. 整对剔除 / 改判 ----------
    for code, (cat, reason, keep_role) in DROP.items():
        rec = wl.get(code)
        if isinstance(rec, dict):
            drop_pair(rec, code, cat, reason, keep_role=keep_role)
    for code, (cat, tokens, reason) in REJUDGE.items():
        rec = wl.get(code)
        if not isinstance(rec, dict):
            continue
        pairs = pairs_of(rec)
        left = [p for p in pairs if p[0] != "半导体"]
        if len(left) != len(pairs) or not any(p[0] == cat for p in pairs):
            left.append([cat, list(tokens)])
            mark(code, "改判", "半导体/存储芯片-DRAM", f"{cat}/{dump_tokens(tokens)}")
            write_pairs(rec, left)
            rec["jiuyan_exclude_note_2026_10_20"] = reason

    # ---------- 4. 补标签 ----------
    n_add = 0
    for code, (cat, concept, note) in ADD.items():
        rec = wl.get(code)
        if not isinstance(rec, dict):
            mark(code, "补入.缺票", "", f"{cat}/{concept}")
            continue
        if add_pair(rec, code, cat, concept, note):
            n_add += 1
    print(f"[4] 补标签: {n_add} 只")

    # ---------- 5. 角色标记 ----------
    n_role = 0
    for code, (role, reason) in ROLE.items():
        rec = wl.get(code)
        if not isinstance(rec, dict):
            mark(code, "role.缺票", "", role)
            continue
        before = rec.get("jiuyan_role")
        set_role(rec, code, role, reason)
        if rec.get("jiuyan_role") != before or not before:
            n_role += 1
    print(f"[5] 角色标记: {n_role} 只")

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
        print("\n-- 全量预览 --")
        for c in changes:
            print(" ", c[0], c[1], "|", c[2], "→", c[3])


if __name__ == "__main__":
    main()
