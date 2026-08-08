# -*- coding: utf-8 -*-
"""semantic_compare_v120.py — V1.2.0 冒烟三块 vs 决赛参照：语义级比对留证

背景（诊断结论 2026-08-08）：
1. signals.threshold: 生产 config STOCK_PARAMS int 36 vs 决赛 env 注入 float 36.0 —— 纯序列化类型差
   （本轮已将 config 5 码改为 36.0，但冒烟块为改动前产物，语义比对按值判定 36==36.0）
2. trace NaN vs null: utils._append_jsonl 于 08-07 之后、V1.2.0 之外引入 _json_safe 包裹
   （证据: 生产 t_io/traces/decision_trace_2026-08-07.jsonl 仍为 NaN 字面量）—— 序列化层强化，非行为差
3. C1p_000988_20260323 trace_0325 参照块 474 行 = 双跑追加（第 238 行起重复整日）—— 参照块缺陷，
   对参照去重（取前半）后与冒烟单跑 237 行语义比对
"""
import json
import math
import sys
from pathlib import Path

BASE = Path(r"E:\06_T")
CASES = [
    ("A_prod_000988 vs C1p_000988_20260323", BASE / "t_io/validation/v120_smoke/A_prod_000988",
     BASE / "t_io/validation/w32_c1p/parts/C1p_000988_20260323"),
    ("A_prod_588170 vs C1p_588170_20260511", BASE / "t_io/validation/v120_smoke/A_prod_588170",
     BASE / "t_io/validation/w32_c1p/parts/C1p_588170_20260511"),
    ("B_compat_600481 vs ctl_600481_20260622", BASE / "t_io/validation/v120_smoke/B_compat_600481",
     BASE / "t_io/validation/w32_c1/parts/ctl_600481_20260622"),
]


def _eq(a, b, path=""):
    """递归语义相等：NaN≡None≡NaN，int/float 按值比较（36==36.0）。"""
    if isinstance(a, float) and math.isnan(a):
        a = None
    if isinstance(b, float) and math.isnan(b):
        b = None
    if a is None or b is None:
        return a is None and b is None, path
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (float(a) == float(b)), path
    if type(a) is not type(b):
        return False, path
    if isinstance(a, dict):
        if set(a) != set(b):
            return False, path + f".keys({sorted(set(a) ^ set(b))})"
        for k in a:
            ok, p = _eq(a[k], b[k], f"{path}.{k}")
            if not ok:
                return False, p
        return True, path
    if isinstance(a, list):
        if len(a) != len(b):
            return False, path + f".len({len(a)}!={len(b)})"
        for i, (x, y) in enumerate(zip(a, b)):
            ok, p = _eq(x, y, f"{path}[{i}]")
            if not ok:
                return False, p
        return True, path
    return a == b, path


def _load_jsonl(fp):
    # json.loads 默认接受 NaN/Infinity 字面量（parse_constant），两侧均可解析
    return [json.loads(line) for line in fp.read_text(encoding="utf-8").splitlines() if line.strip()]


def _cmp_jsonl(name, q, p, diffs):
    a, b = _load_jsonl(q), _load_jsonl(p)
    if len(b) > len(a):
        # 参照块可能存在双跑/部分重跑追加（harness 重试未清 trace）：尝试前缀/后缀窗口取完整单跑
        matched = None
        for label, cand in (("前缀", b[: len(a)]), ("后缀", b[-len(a):])):
            if all(_eq(x, y)[0] for x, y in zip(a, cand)):
                matched = label
                break
        if matched:
            print(f"  NOTE {name} {p.name}: 参照 {len(b)} 行含重跑追加，取{matched}窗口 {len(a)} 行语义一致")
            return
        diffs.append(f"{p.name}: 行数 {len(a)} vs {len(b)} 且前缀/后缀窗口均不语义一致")
        return
    if len(a) != len(b):
        diffs.append(f"{p.name}: 行数 {len(a)} vs {len(b)}")
        return
    for i, (x, y) in enumerate(zip(a, b)):
        ok, path = _eq(x, y)
        if not ok:
            diffs.append(f"{p.name}:L{i + 1} 字段{path}: {json.dumps(x, ensure_ascii=False)[:120]} ...")
            if len(diffs) > 5:
                return


def _cmp_json(name, q, p, diffs):
    a = json.loads(q.read_text(encoding="utf-8"))
    b = json.loads(p.read_text(encoding="utf-8"))
    ok, path = _eq(a, b)
    if not ok:
        diffs.append(f"{p.name}: 字段{path} 不等")


def main():
    all_ok = True
    for name, od, ref in CASES:
        diffs = []
        for p in sorted(ref.glob("*.jsonl")) + sorted(ref.glob("*.json")):
            q = od / p.name
            if not q.exists():
                diffs.append(f"{p.name}: 冒烟侧缺失")
                continue
            if p.suffix == ".jsonl":
                _cmp_jsonl(name, q, p, diffs)
            else:
                _cmp_json(name, q, p, diffs)
        extra = [q.name for q in od.glob("*") if not (ref / q.name).exists()]
        if extra:
            diffs.append(f"冒烟侧多出文件: {extra}")
        if diffs:
            all_ok = False
            print("FAIL", name)
            for d in diffs[:8]:
                print("   ", d)
        else:
            print("PASS", name)
    print("V120_SMOKE_SEMANTIC_COMPARE:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
