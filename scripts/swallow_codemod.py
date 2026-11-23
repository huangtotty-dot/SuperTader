# -*- coding: utf-8 -*-
"""gui.py 裸吞点计数化 codemod（2026-11-23 P1-6 次批）

把 gui.py 中「except 块体只有 pass」的裸吞点机械转换为 `_swallow("gui.<所在函数>"[, e])`，
行为与 pass 完全等价（core/diag_metrics.swallow 自身绝不抛），但从此可计数、可见。

纪律：
- 只动「块体仅 pass」的点；块体含其它语句（pass 之后还有代码）的一律跳过并列入报告；
- `except ... as e:` 带绑定的把 e 传进去（last_exc 可见异常类型）；
- 模块级（无所属函数）打 "gui.module"；嵌套函数取最近一层 def；
- 幂等：pass 行后已有 _swallow 的跳过。

用法：
    python scripts/swallow_codemod.py --dry-run     # 只出报告，不改文件
    python scripts/swallow_codemod.py               # 实改 gui.py（先备份到 backup/）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "gui.py"
BACKUP = ROOT / "backup"

DEF_RE = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)")
EXCEPT_RE = re.compile(r"^(\s*)except\b(.*)$")


def find_bare_sites(lines):
    """返回 [(except_lineno0, pass_lineno0, indent, has_bind, bind_name, inline)]。

    inline=True 表示 `except ...: pass` 同行形态（此时 pass_lineno0 == except_lineno0）。
    块体不止 pass 的跳过并记入 skipped。
    """
    sites, skipped = [], []
    i, n = 0, len(lines)
    while i < n:
        m = EXCEPT_RE.match(lines[i])
        if not m:
            i += 1
            continue
        indent, tail = m.group(1), m.group(2)
        # 同行形态：except ...: pass
        mm = re.match(r"^:\s*pass\s*(#.*)?$", tail)
        if mm:
            sites.append((i, i, indent, tail))
            i += 1
            continue
        if not tail.rstrip().endswith(":"):
            i += 1
            continue
        # 换行形态：找下一非空行
        j = i + 1
        while j < n and not lines[j].strip():
            j += 1
        if j < n and lines[j].strip() == "pass":
            pass_ind = lines[j][: len(lines[j]) - len(lines[j].lstrip())]
            if len(pass_ind) <= len(indent):
                i += 1
                continue
            # pass 必须是被除非空行外的唯一语句：pass 之后第一行非空行应 dedent 到
            # 不超过 except 的缩进（否则块内还有别的代码，跳过人工看）
            k = j + 1
            while k < n and not lines[k].strip():
                k += 1
            only_pass = True
            if k < n:
                next_ind = len(lines[k]) - len(lines[k].lstrip())
                if next_ind > len(indent):
                    only_pass = False
            if only_pass:
                sites.append((i, j, indent, tail))
            else:
                skipped.append((i + 1, "pass 后仍有代码"))
            i = j
        i += 1
    return sites, skipped


def enclosing_func(lines, lineno0):
    """向上找缩进更小的最近 def；找不到返回 'module'。"""
    for k in range(lineno0, -1, -1):
        m = DEF_RE.match(lines[k])
        if m:
            def_ind = len(m.group(1))
            cur_ind = len(lines[lineno0]) - len(lines[lineno0].lstrip())
            if def_ind < cur_ind:
                return m.group(2)
    return "module"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    src = GUI.read_text(encoding="utf-8")
    lines = src.splitlines()
    sites, skipped = find_bare_sites(lines)

    edits = []  # (pass_lineno0, old, new, tag)
    for exc_i, pass_i, indent, tail in sites:
        func = enclosing_func(lines, exc_i)
        tag = f"gui.{func}"
        # 绑定变量提取：except Exception as e: / except (A, B) as e:
        bm = re.search(r"\bas\s+(\w+)\s*:?\s*$", tail.rstrip().rstrip(":"))
        bind = bm.group(1) if bm else None
        call = f'_swallow("{tag}"' + (f", {bind})" if bind else ")") + (
            "  # 原裸 pass：计数化（P1-6），行为等价")
        if pass_i == exc_i:  # 同行形态 except ...: pass
            head = lines[exc_i][: lines[exc_i].index("except")]
            body = lines[exc_i][lines[exc_i].index("except"):]
            new_body = re.sub(r":\s*pass\s*(#.*)?$", ": " + call, body)
            edits.append((pass_i, lines[exc_i], head + new_body, tag))
        else:
            pass_ind = lines[pass_i][: len(lines[pass_i]) - len(lines[pass_i].lstrip())]
            edits.append((pass_i, lines[pass_i], pass_ind + call, tag))

    report = {
        "total_sites": len(sites),
        "skipped": [{"line": ln, "why": why} for ln, why in skipped],
        "tags": {},
    }
    for _, _, _, tag in edits:
        report["tags"][tag] = report["tags"].get(tag, 0) + 1

    if a.dry_run:
        print(f"裸吞点 {len(sites)} 处；跳过 {len(skipped)} 处")
        for ln, why in skipped:
            print(f"  跳过 行{ln}: {why}")
        print(f"涉及函数 {len(report['tags'])} 个；top10:")
        for tag, c in sorted(report["tags"].items(), key=lambda x: -x[1])[:10]:
            print(f"  {tag}: {c}")
        (ROOT / "tmp" / "swallow_codemod_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
        print("报告 → tmp/swallow_codemod_report.json")
        return 0

    if not edits:
        print("无可转换点"); return 0
    BACKUP.mkdir(exist_ok=True)
    bak = BACKUP / f"gui_before_swallow_{datetime.now():%Y%m%d_%H%M}.py"
    shutil.copy2(GUI, bak)
    new_lines = list(lines)
    for lineno0, old, new, tag in edits:
        assert new_lines[lineno0] == old, f"行 {lineno0+1} 内容漂移: {new_lines[lineno0]!r} != {old!r}"
        new_lines[lineno0] = new
    GUI.write_text("\n".join(new_lines) + ("\n" if src.endswith("\n") else ""),
                   encoding="utf-8")
    print(f"✓ 转换 {len(edits)} 处（跳过 {len(skipped)} 处人工看）；备份 → {bak.name}")
    for ln, why in skipped:
        print(f"  跳过 行{ln}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
