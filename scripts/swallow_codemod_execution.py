# -*- coding: utf-8 -*-
"""execution/ 静默吞点计数化 codemod（2026-11-23 P1-6 三批）

范围：execution/**/*.py（排除 __pycache__）。把「except 块体仅一条 pass/continue/return/break」
的静默吞点转换为 `_swallow("exec.<模块>.<函数>"[, e])` 计数：
- pass 仅一句      → 替换 pass 行为计数调用（与 gui 批同形态）
- continue/return/break 仅一句 → 在该语句**前一行**插入计数调用
- 同行形态（except X: pass）→ 行内替换；同行 continue/return/break → 拆三行
行为与原来完全等价（diag_metrics 自身绝不抛），但从此可计数、可见。

导入注入：
- daily_sentiment.py（已有顶层 from core.* 导入）→ 追加普通 import；
- 其余 execution/auto 文件 → try/except ImportError 兜底块（路径未就绪环境降级空操作，
  绝不因计数器崩主链；与 gm_main 宿主路径治理哲学一致）。
- 注入锚点 = 文件最后一个**顶层** import/from 语句之后；body 编辑自底向上应用，
  注入最后做（锚点在所有 body 编辑点之上，原行号仍有效）。

幂等：AST 体态含 _swallow 调用的块不再匹配单语句形态，天然跳过。

用法：
    python scripts/swallow_codemod_execution.py --dry-run   # 只出报告
    python scripts/swallow_codemod_execution.py             # 实改（逐文件备份到 backup/）
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXEC_DIR = ROOT / "execution"
BACKUP = ROOT / "backup"

PLAIN_IMPORT_FILES = {"daily_sentiment.py"}  # 已有顶层 from core.* 导入，直接普通 import
FALLBACK_BLOCK = '''try:
    from core.diag_metrics import swallow as _swallow
except ImportError:  # 路径未就绪的极端环境：计数降级为空操作，绝不崩主链（P1-6）
    def _swallow(*_a, **_k):
        return None'''
PLAIN_LINE = "from core.diag_metrics import swallow as _swallow"

TOP_IMPORT_RE = re.compile(r"^(?:import\s+\S|from\s+\S+\s+import\s)")
INLINE_HDR_RE = re.compile(r"^(\s*except\b[^:]*)[ \t]*:[ \t]*(\S.*)$")


def tag_prefix(rel: Path) -> str:
    """execution/auto/_gm/gm_bridge/ops_guard.py → exec.gm_bridge.ops_guard
    execution/auto/gm_main.py → exec.gm_main；execution/daily_sentiment.py → exec.daily_sentiment"""
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "auto":
        parts = parts[1:]
    if parts and parts[0] == "_gm":
        parts = parts[1:]
    return "exec." + ".".join(parts)


def collect_sites(tree):
    """返回 [(handler, stmt, kind, func)]，kind ∈ pass/continue/return/break。"""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if len(node.body) != 1:
            continue
        stmt = node.body[0]
        kind = None
        if isinstance(stmt, ast.Pass):
            kind = "pass"
        elif isinstance(stmt, ast.Continue):
            kind = "continue"
        elif isinstance(stmt, ast.Return):
            kind = "return"
        elif isinstance(stmt, ast.Break):
            kind = "break"
        if kind is None:
            continue
        p = parents.get(node)
        func = "<module>"
        while p is not None:
            if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = p.name
                break
            p = parents.get(p)
        sites.append((node, stmt, kind, func))
    return sites


def find_import_anchor(lines):
    """最后一个顶层 import/from 语句的**结束**行号（0 基，括号/反斜杠续行已含）。"""
    anchor = None
    i, n = 0, len(lines)
    while i < n:
        if TOP_IMPORT_RE.match(lines[i]):
            depth = 0
            j = i
            while j < n:
                code = lines[j].split("#", 1)[0]
                depth += code.count("(") + code.count("[") + code.count("{")
                depth -= code.count(")") + code.count("]") + code.count("}")
                cont = lines[j].rstrip().endswith("\\")
                j += 1
                if depth <= 0 and not cont:
                    break
            anchor = j - 1
            i = j
        else:
            i += 1
    return anchor


STD_CMT = "  # 原静默吞点：计数化（P1-6 三批），行为等价"


def build_edits(lines, sites, prefix):
    """返回 (edits, skipped)。edit = (anchor0, mode, payload)；mode ∈ replace/insert/split。"""
    edits, skipped = [], []
    for handler, stmt, kind, func in sites:
        tag = f"{prefix}.{func}"
        bind = handler.name  # py3: str 或 None
        naked = f'_swallow("{tag}"' + (f", {bind})" if bind else ")")
        call = naked + STD_CMT
        h0 = handler.lineno - 1
        s0 = stmt.lineno - 1
        if s0 == h0:  # 同行形态
            m = INLINE_HDR_RE.match(lines[h0])
            if not m:
                skipped.append((h0 + 1, "同行形态头部分析失败"))
                continue
            hdr, tail_stmt = m.group(1), m.group(2)
            if kind == "pass":
                pm = re.match(r"^(.*?:)\s*pass(\s*(?:#.*))?$", lines[h0])
                if not pm:
                    skipped.append((h0 + 1, "同行 pass 替换未命中"))
                    continue
                edits.append((h0, "replace", pm.group(1) + " " + naked + (pm.group(2) or STD_CMT)))
            else:
                stmt_indent = handler.col_offset + 4
                pad = " " * stmt_indent
                edits.append((h0, "split", [hdr + ":", pad + call, pad + tail_stmt]))
            continue
        # 换行形态
        stmt_line = lines[s0]
        stripped = stmt_line.strip()
        if kind == "pass":
            pm = re.match(r"^(\s*)pass(\s*(?:#.*)?)$", stmt_line)  # 允许尾随注释（保留）
            if not pm:
                skipped.append((s0 + 1, f"语句行不是纯 pass: {stripped[:40]!r}"))
                continue
            edits.append((s0, "replace", pm.group(1) + naked + (pm.group(2) or STD_CMT)))
            continue
        expect = {"continue": "continue", "break": "break"}.get(kind)
        if expect is not None and not stripped.startswith(expect):
            skipped.append((s0 + 1, f"语句行不是纯 {expect}: {stripped[:40]!r}"))
            continue
        if kind == "return" and not stripped.startswith("return"):
            skipped.append((s0 + 1, f"语句行非 return: {stripped[:40]!r}"))
            continue
        ind = stmt_line[: len(stmt_line) - len(stmt_line.lstrip())]
        edits.append((s0, "insert", ind + call))
    return edits, skipped


def process_file(fp: Path, dry: bool):
    rel = fp.relative_to(EXEC_DIR)
    src = fp.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return {"file": str(rel), "error": f"SyntaxError: {e}"}
    lines = src.splitlines()
    sites = collect_sites(tree)
    if not sites:
        return {"file": str(rel), "sites": 0}
    prefix = tag_prefix(rel)
    edits, skipped = build_edits(lines, sites, prefix)
    if not edits:
        return {"file": str(rel), "sites": len(sites), "converted": 0,
                "skipped": skipped}

    new_lines = list(lines)
    for anchor, mode, payload in sorted(edits, key=lambda e: -e[0]):
        if mode == "replace":
            new_lines[anchor] = payload
        elif mode == "insert":
            new_lines.insert(anchor, payload)
        elif mode == "split":
            new_lines[anchor:anchor + 1] = payload

    # 导入注入。锚点位移修正：body 编辑中 anchor ≤ imp_at 的 insert(+1)/split(+2) 会把
    # 注入点往下顶，须按净增行数补偿（replace 不移位）。
    need_import = "core.diag_metrics" not in src
    imp_at = None
    if need_import:
        imp_at = find_import_anchor(lines)
        shift = sum(1 if mode == "insert" else 2 if mode == "split" else 0
                    for anchor, mode, _ in edits if anchor <= imp_at)
        block = PLAIN_LINE if fp.name in PLAIN_IMPORT_FILES else FALLBACK_BLOCK
        if imp_at is None:
            # 无顶层 import：插到模块 docstring 之后（或文件头）
            imp_at = -1
            shift = 0
        new_lines[imp_at + 1 + shift:imp_at + 1 + shift] = block.splitlines()

    out = "\n".join(new_lines) + ("\n" if src.endswith("\n") else "")
    # 语法自检（不写盘）
    ast.parse(out)

    result = {"file": str(rel), "sites": len(sites), "converted": len(edits),
              "skipped": skipped, "import_injected": need_import,
              "import_anchor": (imp_at + 1) if imp_at is not None else None,
              "kinds": {}}
    for _, stmt, kind, _ in sites:
        result["kinds"][kind] = result["kinds"].get(kind, 0) + 1
    if not dry:
        BACKUP.mkdir(exist_ok=True)
        bak = BACKUP / f"{fp.stem}_before_swallow_exec_{datetime.now():%Y%m%d_%H%M}.py"
        shutil.copy2(fp, bak)
        fp.write_text(out, encoding="utf-8")
        result["backup"] = bak.name
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files = sorted(p for p in EXEC_DIR.rglob("*.py") if "__pycache__" not in p.parts)
    results = []
    for fp in files:
        results.append(process_file(fp, a.dry_run))

    total = sum(r.get("converted", 0) for r in results)
    total_sites = sum(r.get("sites", 0) for r in results)
    print(f"扫描 {len(files)} 个文件；吞点 {total_sites} 处；"
          f"{'[DRY] 将转换' if a.dry_run else '✓ 已转换'} {total} 处")
    for r in results:
        if r.get("converted"):
            print(f"  {r['file']}: {r['converted']}/{r['sites']} "
                  f"kinds={r['kinds']} import={'新注入' if r.get('import_injected') else '已有'}")
            for ln, why in r.get("skipped") or []:
                print(f"    跳过 行{ln}: {why}")
        elif r.get("error"):
            print(f"  {r['file']}: {r['error']}")
    (ROOT / "tmp").mkdir(exist_ok=True)
    (ROOT / "tmp" / "swallow_exec_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("报告 → tmp/swallow_exec_report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
