# -*- coding: utf-8 -*-
"""
analyze_ab_expanded.py — X6 扩样本 A/B 统计分析
输入: v108_unified/{baseline,v102}/signals_*.jsonl + summary_*.json
输出: v108_unified/analysis.json (报告取数依据)
"""
import json
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(r"E:\06_T")
ROOT = BASE / "t_io/validation/v108_unified"
CODES = ["000988", "588170", "600176", "600481", "603667"]
N_DAYS = 90  # 2026-03-16 ~ 2026-07-24 实际交易日


def load(mode):
    sigs = [json.loads(l) for l in open(ROOT / mode / f"signals_{mode}.jsonl", encoding="utf-8") if l.strip()]
    summary = json.load(open(ROOT / mode / f"summary_{mode}.json", encoding="utf-8"))
    return sigs, summary


def wr(sigs):
    wins = sum(1 for s in sigs if s.get("settle_result") == "WIN")
    settled = sum(1 for s in sigs if s.get("settle_result") in ("WIN", "FAIL"))
    return {"n": len(sigs), "win": wins, "settled": settled,
            "wr": round(wins / settled, 4) if settled else None}


def fisher_two_sided(a_w, a_n, b_w, b_n):
    """Fisher 精确检验(双侧), 复用 audit_v106_recompute 口径"""
    from math import comb
    def p(k, n1, n2, N):
        return comb(n1, k) * comb(n2, N - k) / comb(n1 + n2, N)
    n1, n2, N = a_n, b_n, a_w + b_w
    p_obs = p(a_w, n1, n2, N)
    lo = max(0, N - n2)
    hi = min(n1, N)
    tot = sum(p(k, n1, n2, N) for k in range(lo, hi + 1) if p(k, n1, n2, N) <= p_obs * (1 + 1e-9))
    return round(min(1.0, tot), 4)


out = {}
data = {}
for mode in ("baseline", "v102"):
    sigs, summary = load(mode)
    data[mode] = sigs
    # action 分布
    actions = Counter(s["action"] for s in sigs)
    # 密度: 每股每日
    per_code_days = defaultdict(set)
    for s in sigs:
        per_code_days[s["code"]].add(s["ts"][:10])
    density = {c: round(sum(1 for s in sigs if s["code"] == c) / N_DAYS, 3) for c in CODES}
    # 分股胜率 + 买卖分层
    per_code = {}
    for c in CODES:
        cs = [s for s in sigs if s["code"] == c]
        buy = [s for s in cs if s["action"].startswith("BUY")]
        sell = [s for s in cs if s["action"].startswith("SELL")]
        per_code[c] = {"all": wr(cs), "buy": wr(buy), "sell": wr(sell)}
    out[mode] = {
        "total": wr(sigs),
        "actions": dict(actions),
        "density": density,
        "per_code": per_code,
        "closed_loop_pairs": summary.get("closed_loop", {}).get("total_closed_pairs"),
        "closed_loop_pnl": summary.get("closed_loop", {}).get("total_net_pnl"),
        "p1": summary.get("p1_metrics"),
    }

# churn: (ts, code) 集合
bset = {(s["ts"], s["code"]) for s in data["baseline"]}
vset = {(s["ts"], s["code"]) for s in data["v102"]}
bmap = {(s["ts"], s["code"]): s for s in data["baseline"]}
vmap = {(s["ts"], s["code"]): s for s in data["v102"]}
common = bset & vset
only_b = bset - vset
only_v = vset - bset
out["churn"] = {
    "common_n": len(common),
    "common_wr_baseline": wr([bmap[k] for k in common]),
    "common_wr_v102": wr([vmap[k] for k in common]),
    "only_baseline": wr([bmap[k] for k in only_b]),
    "only_v102": wr([vmap[k] for k in only_v]),
    # 逆势拦截率: 仅 baseline 信号中 settle=FAIL 占比
    "intercept_fail_share": round(
        sum(1 for k in only_b if bmap[k].get("settle_result") == "FAIL") / len(only_b), 4) if only_b else None,
}

# Fisher: 总胜率 + 卖出侧
def settled_wf(sigs):
    s = [x for x in sigs if x.get("settle_result") in ("WIN", "FAIL")]
    return sum(1 for x in s if x["settle_result"] == "WIN"), len(s)

bw, bn = settled_wf(data["baseline"])
vw, vn = settled_wf(data["v102"])
bs = [s for s in data["baseline"] if s["action"].startswith("SELL")]
vs = [s for s in data["v102"] if s["action"].startswith("SELL")]
bsw, bsn = settled_wf(bs)
vsw, vsn = settled_wf(vs)
out["fisher"] = {
    "overall": {"baseline": [bw, bn], "v102": [vw, vn], "p": fisher_two_sided(bw, bn, vw, vn)},
    "sell_side": {"baseline": [bsw, bsn], "v102": [vsw, vsn], "p": fisher_two_sided(bsw, bsn, vsw, vsn)},
}

json.dump(out, open(ROOT / "analysis.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(json.dumps(out, ensure_ascii=False, indent=2))
