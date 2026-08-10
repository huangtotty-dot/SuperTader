# -*- coding: utf-8 -*-
"""compare_v121.py — V1.2.1 冒烟比对终版：参照块重跑追加去重（前缀窗口）+ 全量字节比对

参照块 C1p_600481_20260622 缺陷（W33 决赛重试未清 trace，同 C1p_000988_20260323）：
06-22/23 = 474 行双跑（2×237），06-24 = 316 行（237+79 部分重跑）→ 取前缀 237 行比对。
"""
import hashlib
import sys
from pathlib import Path

REF = Path(r"E:\06_T\t_io\validation\w32_c1p\parts\C1p_600481_20260622")
OUT = Path(r"E:\06_T\t_io\validation\v121_smoke\unfreeze_600481")


def md5_lines(fp):
    return [hashlib.md5(l.encode("utf-8")).hexdigest() for l in
            Path(fp).read_text(encoding="utf-8").splitlines()]


def md5(fp):
    return hashlib.md5(Path(fp).read_bytes()).hexdigest()


def main():
    ok = True
    for p in sorted(REF.glob("decision_trace_*.jsonl")):
        a, b = md5_lines(p), md5_lines(OUT / p.name)
        if a == b:
            print("✓", p.name, "逐字节一致")
            continue
        if len(a) > len(b) and a[: len(b)] == b:
            print(f"✓ {p.name} 参照 {len(a)} 行含重跑追加，前缀 {len(b)} 行逐字节一致")
            continue
        ok = False
        print("✗", p.name, f"内容分歧（ref {len(a)} / new {len(b)}）")
    for name in ("trend_timeline_v102.jsonl", "signals_v102.jsonl", "capped_buys_v102.jsonl",
                 "summary_v102.json", "virtual_trades.json"):
        p, q = REF / name, OUT / name
        if not p.exists() and not q.exists():
            continue
        same = p.exists() and q.exists() and md5(p) == md5(q)
        ok &= same
        print(("✓" if same else "✗"), name, "逐字节一致" if same else "不一致")
    print("V121_SMOKE_COMPARE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
