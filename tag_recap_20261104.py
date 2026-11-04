# -*- coding: utf-8 -*-
"""2026-11-04 owner：按「韭研公社全天涨停复盘简图（11.04）」补韭研概念遗漏。

来源：`tmp/recap_20261104.json`（118 只涨停，15 个复盘板块）+ 对账报告。
真·漏票（无任何韭研标签）**27 只**，本脚本按复盘关键词逐只补；板块/公告类按真实题材归类。

口径（守 `jiuyan_tagging_convention`）：只增不改；细分概念尽量复用现有词表；
「次新/公告/其他」不作为分类写入，但个股按真实题材归到对应分类。

写前备份到 `backup/watchlist_jiuyan_before_recap_20261104.json`；脚本幂等。
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
BASE = Path(__file__).resolve().parent
WL = BASE / "stock_hunter" / "watchlist_jiuyan.json"
BACKUP = BASE / "backup"

ADD = {
    # ── 半导体 *24 ──
    "002081": ("半导体", "洁净室与厂务工程"),          # 金螳螂：半导体洁净室
    "688652": ("半导体", "半导体设备-其他专用设备"),    # 京仪装备
    "688809": ("半导体", "封装测试"),                  # 强一股份：探针卡
    "688536": ("半导体", "芯片设计-模拟与电源管理"),    # 思瑞浦
    "688409": ("半导体", "设备核心零部件"),            # 富创精密
    # ── PCB *13 ──
    "603115": ("消费电子", "被动元件"),                # 海星股份：电极箔（电容材料）
    # ── 黄金 *13 ──
    "001360": ("有色金属", "黄金_金矿"),               # 南矿集团：金矿设备
    # ── 机器人 *11 ──
    "603955": ("机器人", "本体及应用"),                # 大千生态：羽毛球机器人
    "001282": ("机器人", "结构件"),                    # 三联锻造
    "603048": ("机器人", "结构件"),                    # 浙江黎明：冷锻
    # ── AI硬件 *11 ──
    "301070": ("超节点", "其他"),                      # 开勒股份：模型服务平台
    "002674": ("超节点", "其他"),                      # 兴业科技（复盘归 AI硬件）
    "002980": ("超节点", "其他"),                      # 华盛昌：光模块检测
    "002222": ("光通信", "光器件-无源器件"),           # 福晶科技：光学晶体
    # ── 次新 *7（按真实题材归位）──
    "601123": ("有色金属", "其他"),                    # N马矿：铁矿/钼矿
    "301697": ("半导体", "半导体材料-封装材料"),        # N贝特利：电子浆料
    "001233": ("汽车", "轮胎"),                        # 海安集团：全钢轮胎
    "301583": ("半导体", "设备核心零部件"),            # 托伦斯
    "920298": ("半导体", "设备核心零部件"),            # 腾信精密
    "688835": ("半导体", "设备核心零部件"),            # 高凯技术：MFC
    # ── AI影视/传媒 *6 ──
    "300788": ("AI应用", "AI出版"),                    # 中信出版
    # ── 光伏 *6 ──
    "002529": ("光伏", "光伏组件"),                    # 海源复材
    "920770": ("光伏", "光伏电站"),                    # 艾能聚
    "688517": ("光伏", "光储充"),                      # 金冠电气
    # ── 电池产业链 *3 ──
    "002992": ("电池", "其他关键材料及设备"),          # 宝明科技：固态电池铜箔
    # ── 其他 *6 ──
    "002565": ("商业航天", "太空算力"),                # 顺灏股份：轨道辰光
    "002876": ("消费电子", "光学显示"),                # 三利谱：TAC膜
}


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
    lc = norm(e.get("jiuyan_category"))
    lp = norm(e.get("jiuyan_concept"))
    return [(t.strip(), lp) for t in lc.split("|") if t.strip()] if lc else []


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


def main() -> int:
    wl = json.loads(WL.read_text(encoding="utf-8"))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changed, skipped = [], []

    for code, (cat, con) in ADD.items():
        e = wl.get(code)
        if not isinstance(e, dict):
            skipped.append((code, "", "不在 watchlist"))
            continue
        pairs = get_pairs(e)
        cats = [c for c, _ in pairs]
        if cat in cats:
            skipped.append((code, e.get("name"), f"已含 {cat}"))
            continue
        put_pairs(e, pairs + [(cat, con)], now)
        changed.append((code, e.get("name"), cat, con, cats))

    if changed:
        BACKUP.mkdir(exist_ok=True)
        shutil.copy2(WL, BACKUP / "watchlist_jiuyan_before_recap_20261104.json")
        tmp = WL.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(WL)

    print(f"补齐 {len(changed)} 只；跳过 {len(skipped)} 只")
    for code, name, cat, con, cats in changed:
        print(f"  + {code} {name}: {cat}/{con}   （原: {'|'.join(cats) or '—'}）")
    for code, name, why in skipped:
        print(f"  = {code} {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
