# -*- coding: utf-8 -*-
"""
merge_ab_unified.py — X7 统一口径合并
策略: 逐 (mode, code, seg) 选块 —— v108_unified/parts 有则用(统一口径重跑块),
否则复用 v107_ab_expanded/parts(该块日期全部在各股首个快照日之前, 原数据本就是 Tushare,
且 det_v108_ts vs det_v107_a 单日校验证明字节级一致)。
严格校验: signals JSONL 逐行可解析; 日期 ⊆ 周段; 每 (mode,code) 恰好 21 块。
产出: v108_unified/{mode}/{signals,summary,trend_timeline}_mode.{jsonl,json} + 块来源清单 provenance.json
"""
import json, re, shutil, sys
from collections import Counter
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT_U = BASE / "t_io/validation/v108_unified"
ROOT_E = BASE / "t_io/validation/v107_ab_expanded"
CODES = ["000988", "588170", "600176", "600481", "603667"]
MODES = ["baseline", "v102"]

sys.path.insert(0, str(BASE / "t_io/validation"))
from run_ab_expanded import SEGS  # noqa: E402

def pick_part(mode, code, s):
    name = f"{mode}_{code}_{s.replace('-', '')}"
    u = ROOT_U / "parts" / name
    if (u / f"signals_{mode}.jsonl").exists():
        return u, "unified"
    e = ROOT_E / "parts" / name
    if (e / f"signals_{mode}.jsonl").exists():
        return e, "expanded_reuse"
    return None, "MISSING"

def load_jsonl_strict(fp):
    rows = []
    with open(fp, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if line:
                rows.append(json.loads(line))  # 严格: 解析失败直接抛
    return rows

def main():
    errors = []
    provenance = {}
    for mode in MODES:
        all_sigs, all_tl = [], []
        day_bars_sources = {}
        for code in CODES:
            n_unified = n_reuse = 0
            for s, e in SEGS:
                pd_, src = pick_part(mode, code, s)
                if pd_ is None:
                    errors.append(f"MISSING {mode} {code} {s}")
                    continue
                sigs = load_jsonl_strict(pd_ / f"signals_{mode}.jsonl")
                bad_dates = sorted({x["ts"][:10] for x in sigs if not (s <= x["ts"][:10] <= e)})
                if bad_dates:
                    errors.append(f"RANGE_VIOLATION {pd_.name}: {bad_dates}")
                all_sigs.extend(sigs)
                tl_fp = pd_ / f"trend_timeline_{mode}.jsonl"
                if tl_fp.exists():
                    all_tl.extend(load_jsonl_strict(tl_fp))
                if src == "unified":
                    n_unified += 1
                else:
                    n_reuse += 1
            provenance[f"{mode}_{code}"] = {"unified": n_unified, "expanded_reuse": n_reuse}
        # 去重校验
        dup = {k: v for k, v in Counter((x["ts"], x["code"]) for x in all_sigs).items() if v > 1}
        if dup:
            errors.append(f"DUPLICATE {mode}: {len(dup)} pairs e.g. {list(dup)[:3]}")
        od = ROOT_U / mode
        od.mkdir(parents=True, exist_ok=True)
        all_sigs.sort(key=lambda x: (x["ts"], x["code"]))
        with open(od / f"signals_{mode}.jsonl", "w", encoding="utf-8") as f:
            for x in all_sigs:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        all_tl.sort(key=lambda x: (x.get("ts", ""), x.get("code", "")))
        with open(od / f"trend_timeline_{mode}.jsonl", "w", encoding="utf-8") as f:
            for x in all_tl:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
        wins = sum(1 for x in all_sigs if x.get("settle_result") == "WIN")
        settled = sum(1 for x in all_sigs if x.get("settle_result") in ("WIN", "FAIL"))
        print(f"[{mode}] sig={len(all_sigs)} settled={settled} wr={wins/settled if settled else 0:.4f}")

    if errors:
        print("ERRORS:")
        for e in errors:
            print(" ", e)
        json.dump(provenance, open(ROOT_U / "provenance.json", "w", encoding="utf-8"), indent=1)
        return 1
    json.dump(provenance, open(ROOT_U / "provenance.json", "w", encoding="utf-8"), indent=1)
    print("provenance:", json.dumps(provenance))
    print("MERGE_OK (summary/p1/closed_loop 由 summarize_unified.py 重算)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
