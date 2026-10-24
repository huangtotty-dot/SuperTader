# -*- coding: utf-8 -*-
"""每日韭研涨停复盘 → 广度入库 + 猎手池漏票对账（2026-10-24）

用法：
  # 1) 只入库 + 对账（推荐先跑这个）
  python scripts/daily_recap_ingest.py --file tmp/recap_20261024.json

  # 2) 只看对账、不写库
  python scripts/daily_recap_ingest.py --file tmp/recap_20261024.json --dry-run

  # 3) 入库 + 对账 + 按 JSON 里的 target 自动补板块（只增不改）
  python scripts/daily_recap_ingest.py --file tmp/recap_20261024.json --apply

输入 JSON：
{
  "date": "2026-10-24",
  "source": "韭研公社全天涨停复盘简图",
  "sections": {
    "AI应用": [
      {"code": "002474", "name": "榕基软件", "board": 1},
      ...
    ],
    "半导体": 6,                                  // 只给家数
    "数据要素": {"count": 9, "max_board": 3,        // 或显式 count + members
                 "members": [{"code":"002912","name":"中新赛克","board":1}]},
    "AI影视": {"members": [...], "target": {"category": "AI应用", "concept": "AI影视短剧"}}
  }
}

输出：
  · stock_hunter/data/sector_breadth.json         每日广度（cumulative）
  · stock_hunter/data/recap_reconcile_{date}.md   对账报告（漏票/错标候选）
  · --apply 时改 stock_hunter/watchlist_jiuyan.json（改前备份 backup/）
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "stock_hunter"))

from modules import sector_phase as sp  # noqa: E402

WL_FP = BASE / "stock_hunter" / "watchlist_jiuyan.json"
STORE_FP = sp.FP
BACKUP_DIR = BASE / "backup"
DATA_DIR = BASE / "stock_hunter" / "data"
NOW = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── 通用 ──────────────────────────────────────────────────────
def _norm(x):
    if x is None:
        return ""
    if isinstance(x, (list, tuple)):
        return "|".join(str(v).strip() for v in x if str(v).strip())
    return str(x).strip()


def get_pairs(e):
    out = []
    for k in range(1, 10):
        c = (e.get(f"jiuyan_category{k}") or "").strip()
        if c:
            out.append((c, _norm(e.get(f"jiuyan_concept{k}"))))
    if out:
        return out
    lc = (e.get("jiuyan_category") or "").strip()
    lp = _norm(e.get("jiuyan_concept"))
    return [(t.strip(), lp) for t in lc.split("|") if t.strip()] if lc else []


def put_pairs(e, pairs):
    for k in range(1, 10):
        e.pop(f"jiuyan_category{k}", None)
        e.pop(f"jiuyan_concept{k}", None)
    for k, (c, p) in enumerate(pairs, start=1):
        e[f"jiuyan_category{k}"] = c
        if p:
            e[f"jiuyan_concept{k}"] = p
    e["jiuyan_category"] = "|".join(c for c, _ in pairs)
    e["jiuyan_concept"] = "|".join(p for _, p in pairs if p)
    e["updated_at"] = NOW


def normalize_sections(raw: dict) -> "OrderedDict[str, dict]":
    out = OrderedDict()
    for sec, val in (raw or {}).items():
        sec = str(sec).strip()
        if not sec:
            continue
        if isinstance(val, (int, float)):
            out[sec] = {"count": int(val), "max_board": 0, "members": [], "target": None}
            continue
        if isinstance(val, list):
            members = []
            for m in val:
                if isinstance(m, dict) and m.get("code"):
                    members.append({"code": str(m["code"]).zfill(6),
                                    "name": str(m.get("name") or ""),
                                    "board": int(m.get("board") or 0)})
            out[sec] = {"count": len(members), "max_board": max([m["board"] for m in members] or [0]),
                        "members": members, "target": None}
            continue
        if isinstance(val, dict):
            members = []
            for m in (val.get("members") or []):
                if isinstance(m, dict) and m.get("code"):
                    members.append({"code": str(m["code"]).zfill(6),
                                    "name": str(m.get("name") or ""),
                                    "board": int(m.get("board") or 0)})
            cnt = int(val.get("count") or len(members))
            mb = int(val.get("max_board") or max([m["board"] for m in members] or [0]))
            out[sec] = {"count": cnt, "max_board": mb, "members": members,
                        "target": val.get("target")}
    return out


# ── 入库 ──────────────────────────────────────────────────────
def ingest(date: str, sections: dict, source: str = "", dry_run: bool = False) -> dict:
    store = sp.load_store()
    days = store.setdefault("days", {})
    rec = days.setdefault(date, {})
    secs = rec.setdefault("sections", {})
    for name, v in sections.items():
        old = secs.get(name) or {}
        members = v.get("members") or old.get("members") or []
        secs[name] = {
            "count": int(v.get("count") or 0),
            "max_board": int(v.get("max_board") or 0),
            "members": members,
        }
    rec["source"] = source or rec.get("source", "")
    rec["ingested_at"] = NOW
    store["updated_at"] = max(days) if days else date
    if not dry_run:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if STORE_FP.exists():
            shutil.copy2(STORE_FP, BACKUP_DIR / f"sector_breadth_before_{date}.json")
        STORE_FP.parent.mkdir(parents=True, exist_ok=True)
        STORE_FP.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        sp._cache.update({"mtime": None, "data": {}})   # 让同进程后续读到新数据
    return store


# ── 对账 ──────────────────────────────────────────────────────
def reconcile(date: str, sections: dict, wl: dict, apply: bool = False) -> dict:
    issues = []
    applied = []
    for sec, v in sections.items():
        members = v.get("members") or []
        if not members:
            continue
        target = v.get("target") or {}
        target_cat = (target.get("category") or sp.SECTION_CATEGORY.get(sec) or "").strip()
        target_con = (target.get("concept") or "").strip()
        for m in members:
            code, name, board = m["code"], m.get("name", ""), m.get("board", 0)
            e = wl.get(code)
            if not isinstance(e, dict):
                issues.append({"section": sec, "code": code, "name": name, "board": board,
                               "kind": "不在 watchlist（非A股/代码错）", "detail": ""})
                continue
            nm = e.get("name") or name
            pairs = get_pairs(e)
            if not pairs:
                issues.append({"section": sec, "code": code, "name": nm, "board": board,
                               "kind": "不在猎手池（无韭研概念）", "detail": ""})
                if apply and target_cat:
                    put_pairs(e, [(target_cat, target_con)])
                    applied.append((code, nm, "新建", target_cat, target_con))
                continue
            cats = [c for c, _ in pairs]
            if target_cat and target_cat not in cats:
                detail = (f"建议补 {target_cat}/{target_con}" if target_con
                          else f"建议补 {target_cat}（概念待定）")
                issues.append({"section": sec, "code": code, "name": nm, "board": board,
                               "kind": f"板块缺失（现: {'|'.join(cats)}）", "detail": detail})
                if apply and target_cat and target_con:
                    # 只增：追加一对，不覆盖
                    put_pairs(e, pairs + [(target_cat, target_con)])
                    applied.append((code, nm, "追加", target_cat, target_con))

    # 成员录全性提示
    short = []
    for sec, v in sections.items():
        n, cnt = len(v.get("members") or []), int(v.get("count") or 0)
        if v.get("members") and n < cnt:
            short.append(f"{sec}: 成员仅录 {n}/{cnt}（对账只覆盖已录部分）")

    return {"date": date, "issues": issues, "applied": applied, "short": short}


def write_report(rep: dict, snap: dict) -> Path:
    p = DATA_DIR / f"recap_reconcile_{rep['date']}.md"
    lines = [f"# 韭研复盘对账 {rep['date']}", "",
             f"生成：{NOW}", ""]
    if rep["short"]:
        lines += ["## 录入提示", ""] + [f"- {x}" for x in rep["short"]] + [""]
    lines += ["## 漏票 / 错标候选", ""]
    if not rep["issues"]:
        lines.append("无。所有已录成员都在猎手池且板块归属正确。")
    else:
        lines += ["| 板块 | 代码 | 名称 | 板数 | 问题 | 建议 |",
                  "|---|---|---|---:|---|---|"]
        for it in rep["issues"]:
            lines.append(f"| {it['section']} | {it['code']} | {it['name']} | {it['board']} | "
                         f"{it['kind']} | {it['detail']} |")
        lines += ["",
                  "> 注：复盘板块「AI应用」包含 AI安全 子类；安全类个股（如绿盟科技/启明星辰）"
                  "归「网络安全/AI安全」是对的，不需要补 AI应用。"
                  "「板块缺失」只是提示该票的韭研归类与复盘板块名不同，不等于错标；"
                  "「不在猎手池」才是真正的漏票候选。"]
    if rep["applied"]:
        lines += ["", "## 已自动补板块（--apply）", "",
                  "| 代码 | 名称 | 动作 | 分类 | 概念 |", "|---|---|---|---|---|"]
        for a in rep["applied"]:
            lines.append(f"| {a[0]} | {a[1]} | {a[2]} | {a[3]} | {a[4]} |")
    lines += ["", "## 入库后的板块阶段", "",
              "| 主题 | 今日 | 阶段 | 3日变化 | 5日峰值 | 最高板 |",
              "|---|---:|---|---:|---:|---:|"]
    for t in snap.get("themes", []):
        lines.append(f"| {t['theme']} | {t['count']} | {t['phase']} | {t['delta3']:+d} | "
                     f"{t['peak5']} | {t['max_board']} |")
    lines += ["", "### AI链内部分支", "",
              "| 分支 | 今日 | 阶段 | 3日变化 | 5日峰值 |", "|---|---:|---|---:|---:|"]
    for t in snap.get("ai_chain", []):
        lines.append(f"| {t.get('label') or '|'.join(t.get('sections') or [])} | {t['count']} | "
                     f"{t['phase']} | {t['delta3']:+d} | {t['peak5']} |")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="当日复盘 JSON")
    ap.add_argument("--dry-run", action="store_true", help="不写库/不写 watchlist")
    ap.add_argument("--apply", action="store_true", help="按 section.target 自动补板块（只增）")
    ap.add_argument("--chart", action="store_true", help="入库后重画主线广度图（每日复盘用）")
    args = ap.parse_args()

    raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
    date = str(raw.get("date") or "").strip()
    if len(date) != 10:
        raise SystemExit("JSON 缺少 date（YYYY-MM-DD）")
    sections = normalize_sections(raw.get("sections") or {})
    if not sections:
        raise SystemExit("JSON 缺少 sections")

    wl = json.loads(WL_FP.read_text(encoding="utf-8"))
    if args.apply and not args.dry_run:
        shutil.copy2(WL_FP, BACKUP_DIR / f"watchlist_jiuyan_before_recap_{date}.json")

    store = ingest(date, sections, raw.get("source", ""), dry_run=args.dry_run)
    rep = reconcile(date, sections, wl, apply=(args.apply and not args.dry_run))
    snap = sp.snapshot(asof=date)

    if args.apply and not args.dry_run and rep["applied"]:
        WL_FP.write_text(json.dumps(wl, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[ingest] {date} sections={len(sections)} "
          f"counts={ {k: v['count'] for k, v in sections.items()} }")
    if rep["short"]:
        print("[提示]", "；".join(rep["short"]))
    if rep["issues"]:
        print(f"[对账] 发现 {len(rep['issues'])} 个漏票/错标候选：")
        for it in rep["issues"]:
            print(f"  - {it['section']} {it['code']} {it['name']} ({it['board']}板): {it['kind']}")
    else:
        print("[对账] 无漏票/错标候选")
    if rep["applied"]:
        print(f"[apply] 已更新 {len(rep['applied'])} 只：")
        for a in rep["applied"]:
            print(f"  - {a[0]} {a[1]} {a[2]} {a[3]}/{a[4]}")

    if args.dry_run:
        print("[dry-run] 未写库、未改 watchlist")
    else:
        rp = write_report(rep, snap)
        print(f"[report] {rp}")
    print("[phase] 主题：", " / ".join(f"{t['theme']}={t['phase']}({t['count']})"
                                         for t in snap["themes"]))
    print("[phase] AI链：", " / ".join(f"{t.get('label')}={t['phase']}({t['count']})"
                                        for t in snap["ai_chain"]))

    if args.chart and not args.dry_run:
        try:
            sys.path.insert(0, str(BASE / "scripts"))
            from daily_breadth_chart import render
            outs = render(asof=date)
            print("[chart]", " / ".join(str(o) for o in outs))
        except Exception as e:
            print(f"[chart] 失败：{e}")


if __name__ == "__main__":
    main()
