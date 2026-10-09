# -*- coding: utf-8 -*-
"""G4: run S1 grid slice n=4 (36 combos) from grid_spec.json, log failures."""
import json, subprocess, sys, time
from pathlib import Path

BASE = Path(__file__).parent
spec = json.load(open(BASE / "results" / "grid_spec.json", encoding="utf-8"))
slice_ = [c for c in spec["combos"] if c["n"] == 4]
assert len(slice_) == 36, f"expected 36, got {len(slice_)}"

results, failures = [], []
t0 = time.time()
for i, c in enumerate(slice_, 1):
    out = BASE / c["out"]
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(BASE / "s1_sim.py"),
           "--n", str(c["n"]), "--m", str(c["m"]),
           "--min-hold", str(c["min_hold"]), "--tp-arm", c["tp_arm"],
           "--out", str(out)]
    ts = time.time()
    try:
        p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True, timeout=240)
        if p.returncode != 0:
            failures.append({"run_id": c["run_id"], "reason": f"rc={p.returncode}: {(p.stderr or p.stdout)[-500:]}"})
            print(f"[{i}/36] FAIL {c['run_id']} rc={p.returncode}", flush=True)
            continue
        d = json.load(open(out, encoding="utf-8"))
        d["_elapsed"] = round(time.time() - ts, 2)
        results.append(d)
        print(f"[{i}/36] OK {c['run_id']} sharpe={d['sharpe']:.3f} ann={d['ann_ret']:.3f} ({d['_elapsed']}s)", flush=True)
    except subprocess.TimeoutExpired:
        failures.append({"run_id": c["run_id"], "reason": "timeout>240s"})
        print(f"[{i}/36] FAIL {c['run_id']} timeout", flush=True)
    except Exception as e:
        failures.append({"run_id": c["run_id"], "reason": f"{type(e).__name__}: {e}"})
        print(f"[{i}/36] FAIL {c['run_id']} {e}", flush=True)

summary = {
    "slice": "n=4", "total": len(slice_), "ok": len(results), "fail": len(failures),
    "wall_s": round(time.time() - t0, 1),
    "failures": failures,
    "runs": [{k: d[k] for k in ("tag", "n", "m", "min_hold", "tp_arm", "ann_ret", "max_dd",
                                "sharpe", "win_rate", "rot_count", "n_trades", "fee_total",
                                "final_nav", "avg_exposure")} for d in results],
}
sp = BASE / "results" / "s1_grid_n4_summary.json"
json.dump(summary, open(sp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"\nDONE ok={len(results)}/36 fail={len(failures)} wall={summary['wall_s']}s -> {sp}")
