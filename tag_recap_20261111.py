# -*- coding: utf-8 -*-
"""2026-11-11 owner：「更新今天的主线广度 同时检查对应的个股补充到对应的韭研概念中」。

复盘简图（11.11 全天）已 ingest 到 sector_breadth.json。本脚本只处理对账发现的
**韭研概念缺口**（`scripts/daily_recap_ingest.py` 的 reconcile 列表）：

  · 无韭研概念（3 只，补进猎手池）：600103 青山纸业、920157 宇特光电、688135 利扬芯片
  · 已在池但缺复盘对应板块（26 只）：按复盘分区补一对（只增不改，追加在末尾）

⚠️ 例外（不补，报告中说明）：
  · 605488 福莱新材 / 001339 智微智能：已有「物理AI」类目（reconcile 的 target 数据要素是
    映射表口径，不加重复类目）
  · 002313 日海智能：已有物理AI；「端侧AI」在 AI应用 下没有对应子概念，不硬造
  · 002110 三钢闽光：复盘落在「其他」，无对应题材概念
  · 000708→000678 襄阳轴承：截图印错代码（000708 是中信特钢），已按 000678 入库

写前备份；幂等；每只票打 `jiuyan_recap_20261111` 审计标记。
运行：`python tag_recap_20261111.py [--apply]`
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"
NOTE_KEY = "jiuyan_recap_20261111"

# code: (wl_name, category, concept, 依据/复盘分区)
MAP = {
    # ── 机器人（复盘 20 只里缺机器人板块的）──
    "002593": ("日上集团", "机器人", "结构件与材料", "复盘·机器人：汽车轮毂/钢结构"),
    "002362": ("汉王科技", "机器人", "电机电控与感知", "复盘·机器人：AI嗅觉/机器视觉"),
    "002148": ("北纬科技", "机器人", "核心零部件-传动", "复盘·机器人：谐波减速器/电磁制动器"),
    "603730": ("岱美股份", "机器人", "结构件与材料", "复盘·机器人：汽车内饰/特斯拉"),
    "300473": ("德尔股份", "机器人", "电机电控与感知", "复盘·机器人：电动泵/固态电池"),
    # ── 光通信（复盘 14 只里缺口）──
    "600103": ("青山纸业", "光通信", "光模块与CPO", "复盘·光通信：光模块(恒宝通)"),
    "920157": ("宇特光电", "光通信", "光连接与配套", "复盘·光通信：光连接器件"),
    "603725": ("天安新材", "光通信", "光模块与CPO", "复盘·光通信：CPO上游/电子皮肤"),
    "603330": ("天洋新材", "光通信", "光连接与配套", "复盘·光通信：光模块电子胶"),
    "002980": ("华盛昌", "光通信", "设备与工程", "复盘·光通信：光模块检测"),
    "002957": ("科瑞技术", "光通信", "设备与工程", "复盘·光通信：光耦合设备"),
    "301486": ("致尚科技", "光通信", "光模块与CPO", "复盘·光通信：光封装/CPO"),
    "002975": ("博杰股份", "光通信", "光芯片与光器件", "复盘·光通信：磷化铟/测试"),
    # ── AI硬件 → 超节点 ──
    "605255": ("天普股份", "超节点", "其他", "复盘·AI硬件：中昊芯英TPU"),
    "002913": ("奥士康", "超节点", "PCB", "复盘·AI硬件：AI服务器HDI"),
    "000631": ("顺发恒能", "超节点", "电源", "复盘·AI硬件：800MW燃机项目"),
    "002463": ("沪电股份", "超节点", "PCB", "复盘·AI硬件：AI服务器"),
    "603228": ("景旺电子", "超节点", "PCB", "复盘·AI硬件：光模块/HDI/AI服务器"),
    "300990": ("同飞股份", "超节点", "液冷散热", "复盘·AI硬件：液冷/温控"),
    "002886": ("沃特股份", "超节点", "CCL", "复盘·AI硬件：PTFE薄膜/铜连接"),
    # ── 物理AI ──
    "603466": ("风语筑", "物理AI", "物理AI_数据采集", "复盘·物理AI：数据采集"),
    "002076": ("星光股份", "物理AI", "物理AI_数据采集", "复盘·物理AI：数据采集"),
    "002278": ("神开股份", "物理AI", "物理AI_具身本体", "复盘·物理AI：深海机器人"),
    # ── 半导体 ──
    "600076": ("康欣新材", "半导体", "半导体设备-其他专用设备", "复盘·半导体：修复设备"),
    "600228": ("返利科技", "半导体", "半导体设备-其他专用设备", "复盘·半导体：成立设备公司"),
    "688135": ("利扬芯片", "半导体", "封装测试", "复盘·半导体：算力测试芯片"),
    # ── 端侧AI / 算力 ──
    "002861": ("瀛通通讯", "消费电子", "AI眼镜", "复盘·端侧AI：智能眼镜/AI耳机"),
    "002713": ("东易日盛", "算力租赁", "算力租赁", "复盘·算力：算力产业"),
    "600162": ("香江控股", "算力租赁", "算力租赁", "复盘·算力：算力服务"),
}


def _toks(cn: str) -> list:
    s = str(cn or "").strip()
    if s.startswith("["):
        try:
            return [str(x).strip() for x in ast.literal_eval(s) if str(x).strip()]
        except Exception:
            return [s]
    return [x.strip() for x in s.split("|") if x.strip()]


def norm(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return "|".join(str(v).strip() for v in x if str(v).strip())
    return str(x).strip()


def get_pairs(e: dict) -> list:
    out = []
    for k in range(1, 10):
        c = norm(e.get(f"jiuyan_category{k}"))
        if c:
            out.append((c, norm(e.get(f"jiuyan_concept{k}"))))
    if out:
        return out
    cats = [c.strip() for c in norm(e.get("jiuyan_category")).split("|") if c.strip()]
    cons = [c.strip() for c in norm(e.get("jiuyan_concept")).split("|") if c.strip()]
    return [(c, cons[i] if i < len(cons) else "") for i, c in enumerate(cats)]


def put_pairs(e: dict, pairs: list, now: str) -> None:
    for k in range(1, 10):
        e.pop(f"jiuyan_category{k}", None)
        e.pop(f"jiuyan_concept{k}", None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f"jiuyan_category{k}"] = c
        if p:
            e[f"jiuyan_concept{k}"] = p
    e["jiuyan_category"] = "|".join(c for c, _ in pairs)
    e["jiuyan_concept"] = "|".join(p for _, p in pairs if p)
    e["updated_at"] = now


def has_pair(pairs: list, cat: str, con: str) -> bool:
    return any(c == cat and con in _toks(cn) for c, cn in pairs)


def main() -> int:
    apply = "--apply" in sys.argv
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plan, skip, bad = [], [], []

    for code, (name, cat, con, why) in MAP.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            bad.append((code, name, "不在 watchlist")); continue
        wl_name = str(e.get("name") or "")
        if wl_name != name:
            bad.append((code, name, f"名称不符 watchlist={wl_name}")); continue
        pairs = get_pairs(e)
        if has_pair(pairs, cat, con):
            skip.append(code); continue
        plan.append((code, name, cat, con, why, pairs))

    print(f"待补 {len(plan)} 只；已存在跳过 {len(skip)} 只；异常 {len(bad)}")
    for b in bad:
        print("  ⚠️", b)
    print("\n按类目：")
    for cat, n in Counter(p[2] for p in plan).most_common():
        print(f"  {cat}: {n}")
    print("\n明细：")
    for code, name, cat, con, why, before in plan:
        print(f"  {code} {name}: {before} -> +('{cat}','{con}')  [{why}]")

    if not apply:
        print("\n（预览）加 --apply 落盘")
        return 0

    changed = 0
    for code, name, cat, con, why, before in plan:
        e = wl[code]
        put_pairs(e, before + [(cat, con)], now)
        rs = e.get("jiuyan_concept_reason")
        if not isinstance(rs, dict):
            rs = {}
        rs[con] = f"{name}：{why}（2026-11-11 复盘补充）"
        e["jiuyan_concept_reason"] = rs
        e[NOTE_KEY] = f"{now} 复盘补充 → {cat}/{con}"
        changed += 1

    BACKUP.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(WL, BACKUP / f"watchlist_jiuyan_before_recap_20261111_{stamp}.json")
    tmp = WL.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(WL)
    print(f"\n已改 {changed} 只；备份 backup/watchlist_jiuyan_before_recap_20261111_*.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
