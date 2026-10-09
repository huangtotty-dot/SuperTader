# -*- coding: utf-8 -*-
"""G3 grid scan shard: n=3, all m/min_hold/tp combos (36 runs)."""
import json, subprocess, sys, time
from pathlib import Path

BASE = Path(__file__).resolve().parent
SPEC = BASE / "results" / "grid_spec.json"
RUNS_DIR = BASE / "results" / "s1_runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)

spec = json.loads(SPEC.read_text(encoding="utf-8"))
combos = [c for c in spec["combos"] if c["n"] == 3]
assert len(combos) == 36, f"expect 36, got {len(combos)}"

results, failures = [], []
t0 = time.time()
for i, c in enumerate(combos, 1):
    out_path = BASE / c["out"]
    cmd = [sys.executable, str(BASE / "s1_sim.py"),
           "--n", str(c["n"]), "--m", str(c["m"]),
           "--min-hold", str(c["min_hold"]), "--tp-arm", c["tp_arm"],
           "--out", str(out_path)]
    ts = time.time()
    try:
        r = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            failures.append({"run_id": c["run_id"], "reason": "nonzero_exit",
                             "rc": r.returncode, "stderr": (r.stderr or "")[-500:]})
            print(f"[{i}/36] FAIL {c['run_id']} rc={r.returncode}")
            continue
        data = json.loads(out_path.read_text(encoding="utf-8"))
        data["_run_id"] = c["run_id"]
        data["_elapsed_s"] = round(time.time() - ts, 2)
        results.append(data)
        print(f"[{i}/36] OK {c['run_id']} ({data['_elapsed_s']}s)")
    except Exception as e:
        failures.append({"run_id": c["run_id"], "reason": type(e).__name__,
                         "detail": str(e)[:500]})
        print(f"[{i}/36] FAIL {c['run_id']} {type(e).__name__}: {e}")

print(f"\nTotal: {len(results)} ok / {len(failures)} failed, {time.time()-t0:.1f}s elapsed")
agg = {"ok": len(results), "failed": failures,
       "results": results}
(BASE / "results" / "g3_shard_n3_summary.json").write_text(
    json.dumps(agg, ensure_ascii=False, indent=2), encoding="utf-8")
print("summary -> results/g3_shard_n3_summary.json")
