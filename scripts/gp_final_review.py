# -*- coding: utf-8 -*-
"""GP 终审批量 runner（G1 终审 · 2026-09-21）

从双 seed 台账按预注册规则挑候选，逐一走 gp_miner.py --review
（eval_factor 固定规则裁决：完整交易模拟 + 随机基线 + 预注册闸门）。

选拔规则（写入结果 meta 备查）：
  MC 裁决 == PASS 且（费后抽检 net_mean > 0 或 OOS_IC > 0.1 或 fitness >= 0.28）
  双 seed 合并、按表达式字符串去重，上限 10 个。

产物：t_io/validation/factor_mining/results/gp_mine/final_review_2026-09-21.json
由临时 manual Automation 承载，跑完即删。
"""
import json
import os
import subprocess
import sys
import time

ROOT = r"E:\superTrader"
FM = os.path.join(ROOT, "t_io", "validation", "factor_mining")
MINER = os.path.join(FM, "gp_miner.py")
OUT = os.path.join(FM, "results", "gp_mine", "final_review_2026-09-21.json")
CAP = 10
PER_REVIEW_TIMEOUT = 1200  # 单候选 20 分钟上限

RULE = "mc==PASS and (fee.net_mean>0 or oos_ic>0.1 or fitness>=0.28); dedup by expr; cap 10"


def pick_shortlist():
    seen, picked = set(), []
    for seed in (0, 1):
        path = os.path.join(FM, "results", "gp_mine", f"gp_ledger_seed{seed}.json")
        d = json.load(open(path, encoding="utf-8"))
        for c in d["candidates"]:
            expr = c["expr"]
            if expr in seen:
                continue
            mc = (c.get("mc") or {}).get("verdict")
            fee = (c.get("fee") or {}).get("net_mean")
            oos = c.get("oos_ic")
            ok = (mc == "PASS") and (
                (fee is not None and fee > 0)
                or (oos is not None and oos > 0.1)
                or (c.get("fitness") or 0) >= 0.28
            )
            if ok:
                seen.add(expr)
                picked.append({
                    "seed": seed, "expr": expr,
                    "sign": int(c.get("sign_hint") or 1),
                    "fitness": c.get("fitness"), "is_ic": c.get("is_ic"),
                    "oos_ic": oos, "fee_net": fee,
                })
    picked.sort(key=lambda x: -(x["fitness"] or 0))
    return picked[:CAP]


def run_one(item):
    cmd = [sys.executable, MINER, "--review", item["expr"],
           "--sign", str(item["sign"]), "--exit", "hold"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, cwd=FM,
                           timeout=PER_REVIEW_TIMEOUT, encoding="utf-8",
                           errors="replace",
                           creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        out = p.stdout or ""
        i = out.rfind("\n{")
        payload = json.loads(out[i + 1:]) if i >= 0 else {"parse_error": True, "stdout_tail": out[-800:], "stderr_tail": (p.stderr or "")[-400:]}
        payload["_rc"] = p.returncode
    except subprocess.TimeoutExpired:
        payload = {"review_error": f"timeout {PER_REVIEW_TIMEOUT}s"}
    except Exception as e:  # noqa
        payload = {"review_error": str(e)}
    payload["_sec"] = round(time.time() - t0, 1)
    return payload


def run(ctx=None):
    sys.stdout.reconfigure(encoding="utf-8")
    shortlist = pick_shortlist()
    print(f"[review] 候选 {len(shortlist)} 个", flush=True)
    results = []
    for i, item in enumerate(shortlist, 1):
        print(f"[review] ({i}/{len(shortlist)}) seed{item['seed']} "
              f"fit={item['fitness']:.3f} {item['expr'][:50]}", flush=True)
        r = run_one(item)
        r.update({k: item[k] for k in ("seed", "expr", "sign", "fitness",
                                        "is_ic", "oos_ic", "fee_net")})
        results.append(r)
        verdict = r.get("verdict") or r.get("gate") or r.get("review_error") or "?"
        print(f"[review]   -> {verdict} ({r['_sec']}s)", flush=True)
        # 增量落盘，防中途崩溃丢全部
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump({"meta": {"rule": RULE, "date": "2026-09-21",
                                "exit": "hold", "n": len(shortlist)},
                       "results": results}, f, ensure_ascii=False, indent=1)
    return {"artifact": {"n_reviewed": len(results), "out": OUT,
                         "verdicts": [r.get("verdict") or r.get("review_error") for r in results]}}


if __name__ == "__main__":
    print(json.dumps(run({}), ensure_ascii=False, indent=2)[:2000])
