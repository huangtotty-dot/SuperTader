# -*- coding: utf-8 -*-
"""
analyze_threshold_ladder.py — X9 阈值阶梯对比分析
对比 65档(v108_unified) / 58档(v109_threshold/t58, 若存在) / 55档(v109_threshold/t55):
各档 A/B 核心指标 + Fisher + 拦截率(exact-ts口径) + 密度 + 分股 + 阈值效应分解 + X8 估计对照。
输出: v109_threshold/ladder_analysis.json + 控制台表
"""
import json, sys
from pathlib import Path

BASE = Path(r"E:\06_T")
CODES = ["000988", "588170", "600176", "600481", "603667"]
N_DAYS = 90

def load(root, mode):
    return [json.loads(l) for l in open(root / mode / f"signals_{mode}.jsonl", encoding="utf-8") if l.strip()]

def wr_of(sigs):
    w = sum(1 for s in sigs if s.get("settle_result") == "WIN")
    n = sum(1 for s in sigs if s.get("settle_result") in ("WIN", "FAIL"))
    return w, n, round(w / n, 4) if n else None

def fisher_two_sided(a_w, a_n, b_w, b_n):
    from math import comb
    def p(k, n1, n2, N):
        return comb(n1, k) * comb(n2, N - k) / comb(n1 + n2, N)
    n1, n2, N = a_n, b_n, a_w + b_w
    p_obs = p(a_w, n1, n2, N)
    lo, hi = max(0, N - n2), min(n1, N)
    return round(min(1.0, sum(p(k, n1, n2, N) for k in range(lo, hi + 1)
                             if p(k, n1, n2, N) <= p_obs * (1 + 1e-9))), 4)

def ladder_entry(tag, root):
    entry = {"tag": tag}
    for mode in ("baseline", "v102"):
        sigs = load(root, mode)
        w, n, wr = wr_of(sigs)
        entry[mode] = {"n": len(sigs), "win": w, "settled": n, "wr": wr,
                       "density": round(len(sigs) / (len(CODES) * N_DAYS), 3),
                       "per_code": {c: wr_of([s for s in sigs if s["code"] == c])[2] for c in CODES},
                       "per_code_n": {c: sum(1 for s in sigs if s["code"] == c) for c in CODES}}
    b, v = entry["baseline"], entry["v102"]
    entry["delta_pp"] = round((v["wr"] - b["wr"]) * 100, 2) if v["wr"] and b["wr"] else None
    entry["fisher_p"] = fisher_two_sided(b["win"], b["settled"], v["win"], v["settled"])
    bsig, vsig = load(root, "baseline"), load(root, "v102")
    v_keys = {(s["ts"], s["code"]) for s in vsig}
    only_b = [s for s in bsig if (s["ts"], s["code"]) not in v_keys]
    entry["intercepted"] = len(only_b)
    entry["intercept_fail_share"] = round(
        sum(1 for s in only_b if s["settle_result"] == "FAIL") / len(only_b), 4) if only_b else None
    return entry

def main():
    out = {"ladder": []}
    out["ladder"].append(ladder_entry("t65", BASE / "t_io/validation/v108_unified"))
    for th in ("58", "55"):
        root = BASE / f"t_io/validation/v109_threshold/t{th}"
        if (root / "v102/signals_v102.jsonl").exists():
            out["ladder"].append(ladder_entry(f"t{th}", root))
    if len(out["ladder"]) >= 2:
        ref, new = out["ladder"][0], out["ladder"][-1]
        out["decomposition"] = {
            "阈值效应_baseline_wr变化_pp": round((new["baseline"]["wr"] - ref["baseline"]["wr"]) * 100, 2),
            "趋势层效应@65_pp": ref["delta_pp"],
            "趋势层效应@新档_pp": new["delta_pp"],
            "baseline信号量_65vs新档": [ref["baseline"]["n"], new["baseline"]["n"]],
            "v102信号量_65vs新档": [ref["v102"]["n"], new["v102"]["n"]],
        }
    x8_est = {"recover": 174, "v102_n": 442, "v102_wr": 0.5477, "delta_pp": 4.77,
              "density": 0.982, "intercept_fail_share": 0.514}
    t55 = next((e for e in out["ladder"] if e["tag"] == "t55"), None)
    if t55:
        out["x8_estimate_vs_actual"] = {
            "estimate": x8_est,
            "actual": {"v102_n": t55["v102"]["n"], "v102_wr": t55["v102"]["wr"],
                       "delta_pp": t55["delta_pp"], "density": t55["v102"]["density"],
                       "intercept_fail_share": t55["intercept_fail_share"],
                       "intercepted": t55["intercepted"]},
        }
    od = BASE / "t_io/validation/v109_threshold"
    od.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(od / "ladder_analysis.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(out, ensure_ascii=False, indent=1))

if __name__ == "__main__":
    main()
